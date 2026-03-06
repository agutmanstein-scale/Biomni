"""
Biomni Tool Server — HTTP API for listing and calling Biomni tools.

Request/response format matches the MCP Agent Environment API so clients
can be swapped between this server and an MCP-based environment without
code changes.  Tools are loaded from Biomni's module registry (the same
registry used by ``A1.create_mcp_server()``).

Usage:
    python -m biomni.server
    uvicorn biomni.server:app --host 0.0.0.0 --port 1984

Environment variables:
    BIOMNI_DATA_PATH     — Root data path (default: ./data)
    BIOMNI_COMMERCIAL_MODE — "true" for commercial-only datasets
    BIOMNI_MODULES       — Comma-separated module filter (default: all)
    BIOMNI_HOST          — Bind host (default: 0.0.0.0)
    BIOMNI_PORT          — Bind port (default: 1984)
"""

import importlib
import json
import logging
import os
import traceback
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from biomni.utils import read_module2api

logger = logging.getLogger("biomni.server")

# ---------------------------------------------------------------------------
# Paths derived from environment
# ---------------------------------------------------------------------------
_BASE_PATH = os.environ.get("BIOMNI_DATA_PATH", "./data")
_DATA_LAKE_PATH = os.path.join(_BASE_PATH, "biomni_data", "data_lake")

# ---------------------------------------------------------------------------
# Tool registry  (populated at startup)
# ---------------------------------------------------------------------------
_module2api: dict[str, list[dict]] = {}
_tool_index: dict[str, dict] = {}
_tool_functions: dict[str, Any] = {}
_tool_modules: dict[str, str] = {}

# Lightweight episode state for OpenEnv compatibility
_episode_id: str = ""
_step_count: int = 0
_tools_called: list = []

# #region agent log — startup diagnostics capture
_startup_diag: dict[str, Any] = {}
# #endregion


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

_TYPE_MAP = {
    "str": "string", "string": "string",
    "int": "integer", "integer": "integer",
    "float": "number", "number": "number",
    "bool": "boolean", "boolean": "boolean",
    "list": "array", "dict": "object",
}


def _resolve_json_schema_type(type_str: str) -> str:
    """Map a Python type annotation string to a JSON Schema type.

    Handles compound types like ``List[int]``, ``Dict[str, Any]``,
    ``Union[str, List[str]]``, and ``Optional[X]``.
    """
    low = type_str.strip().lower()
    if low in _TYPE_MAP:
        return _TYPE_MAP[low]
    if low.startswith("list[") or low.startswith("list "):
        return "array"
    if low.startswith("dict[") or low.startswith("dict "):
        return "object"
    if low.startswith("optional["):
        inner = type_str.strip()[len("optional["):-1]
        return _resolve_json_schema_type(inner)
    if low.startswith("union["):
        # Pick the first non-None type
        inner = type_str.strip()[len("union["):-1]
        for part in inner.split(","):
            part = part.strip()
            if part.lower() not in ("none", "nonetype"):
                return _resolve_json_schema_type(part)
    return "string"


def _param_to_json_schema_prop(param: dict) -> dict:
    prop: dict = {"type": _resolve_json_schema_type(param.get("type", "string"))}
    if "description" in param:
        prop["description"] = param["description"]
    return prop


def _schema_to_mcp_tool(name: str, schema: dict) -> dict:
    required_params = schema.get("required_parameters", [])
    optional_params = schema.get("optional_parameters", [])
    properties = {
        p["name"]: _param_to_json_schema_prop(p)
        for p in required_params + optional_params
        if "name" in p
    }
    return {
        "name": name,
        "description": schema.get("description", ""),
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": [p["name"] for p in required_params if "name" in p],
        },
    }


def _to_content_blocks(result: Any) -> list:
    if isinstance(result, str):
        text = result
    elif isinstance(result, (dict, list)):
        text = json.dumps(result, ensure_ascii=False, indent=2)
    else:
        text = str(result)
    return [{"type": "text", "text": text}]


def _coerce_arg(value: Any, type_str: str) -> Any:
    """Best-effort coercion of *value* to match the declared *type_str*.

    Handles the common case where a JSON client sends a string for a
    parameter declared as ``List[…]`` or ``dict`` — we attempt to
    ``json.loads`` the string.  Also coerces numeric strings to int/float.
    """
    if value is None:
        return value
    low = type_str.strip().lower()

    # List / array types — parse JSON strings
    if low.startswith("list") or low == "array":
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                pass
        return value

    # Dict / object types
    if low.startswith("dict") or low == "object":
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                pass
        return value

    # Scalar coercion
    if low in ("int", "integer") and isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            pass
    if low in ("float", "number") and isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            pass
    if low in ("bool", "boolean") and isinstance(value, str):
        return value.lower() in ("true", "1", "yes")

    # Union / Optional — try inner types
    if low.startswith("union[") or low.startswith("optional["):
        prefix_len = len("union[") if low.startswith("union[") else len("optional[")
        inner = type_str.strip()[prefix_len:-1]
        for part in inner.split(","):
            part = part.strip()
            if part.lower() not in ("none", "nonetype"):
                coerced = _coerce_arg(value, part)
                if coerced is not value:
                    return coerced

    return value


def _inject_defaults(tool_name: str, tool_args: dict) -> dict:
    """Auto-inject ``data_lake_path`` and coerce parameter types."""
    schema = _tool_index.get(tool_name, {})
    all_params = schema.get("required_parameters", []) + schema.get("optional_parameters", [])
    param_map = {p["name"]: p for p in all_params if "name" in p}

    if "data_lake_path" in param_map and "data_lake_path" not in tool_args:
        tool_args = {**tool_args, "data_lake_path": _DATA_LAKE_PATH}

    # Coerce types based on schema declarations
    for arg_name, arg_value in list(tool_args.items()):
        param = param_map.get(arg_name)
        if param and "type" in param:
            coerced = _coerce_arg(arg_value, param["type"])
            if coerced is not arg_value:
                tool_args[arg_name] = coerced

    return tool_args


# ---------------------------------------------------------------------------
# Tool loader
# ---------------------------------------------------------------------------

def _load_tools() -> None:
    global _module2api, _tool_index, _tool_functions, _tool_modules, _startup_diag

    # #region agent log — H4: check read_module2api result
    import time as _time
    _diag: dict[str, Any] = {"load_tools_called": True, "timestamp": _time.time()}
    try:
        _module2api = read_module2api()
        _diag["read_module2api_modules"] = len(_module2api)
        _diag["read_module2api_tools"] = sum(len(v) for v in _module2api.values())
        _diag["module_names"] = list(_module2api.keys())
    except Exception as exc:
        _diag["read_module2api_error"] = f"{type(exc).__name__}: {exc}"
        _module2api = {}
    # #endregion

    modules_env = os.environ.get("BIOMNI_MODULES", "").strip()
    # #region agent log — H1/H2: capture env state
    _diag["BIOMNI_MODULES_env"] = repr(modules_env) if modules_env else "(empty)"
    _diag["BIOMNI_DATA_PATH"] = os.environ.get("BIOMNI_DATA_PATH", "(unset)")
    _diag["cwd"] = os.getcwd()
    # #endregion
    if modules_env:
        allowed = {m.strip() for m in modules_env.split(",")}
        _module2api = {k: v for k, v in _module2api.items() if k in allowed}
        _diag["filtered_modules"] = len(_module2api)

    loaded = failed = 0
    import_errors: list[str] = []
    fn_missing: list[str] = []
    for module_name, tools in _module2api.items():
        try:
            mod = importlib.import_module(module_name)
        except ImportError as exc:
            logger.warning("Could not import %s: %s", module_name, exc)
            # #region agent log — H3: capture import failures
            import_errors.append(f"{module_name}: {exc}")
            # #endregion
            failed += len(tools)
            continue

        for schema in tools:
            name = schema.get("name")
            if not name:
                continue
            fn = getattr(mod, name, None)
            if fn is None:
                logger.warning("Function %s not found in %s", name, module_name)
                fn_missing.append(f"{module_name}.{name}")
                failed += 1
                continue
            _tool_index[name] = schema
            _tool_functions[name] = fn
            _tool_modules[name] = module_name
            loaded += 1

    # #region agent log — capture final state
    _diag["loaded"] = loaded
    _diag["failed"] = failed
    _diag["import_errors"] = import_errors[:20]
    _diag["fn_missing"] = fn_missing[:20]
    _startup_diag = _diag
    _log_path = "/home/ec2-user/Biomni/.cursor/debug-596ebe.log"
    try:
        import json as _json
        with open(_log_path, "a") as _f:
            _f.write(_json.dumps({"sessionId": "596ebe", "location": "server.py:_load_tools", "message": "startup_diag", "data": _diag, "timestamp": _time.time(), "hypothesisId": "H1-H5"}) + "\n")
    except Exception:
        pass
    # #endregion

    logger.info("Loaded %d tools (%d failed) from %d modules",
                loaded, failed, len(_module2api))


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _episode_id
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s  %(name)s  %(message)s")
    logger.info("Starting Biomni tool server …")
    logger.info("Data lake path: %s", _DATA_LAKE_PATH)
    _load_tools()
    _episode_id = str(uuid.uuid4())
    logger.info("Ready — %d tools available", len(_tool_functions))
    yield
    logger.info("Shutting down Biomni tool server")


app = FastAPI(
    title="Biomni Tool Server",
    description="HTTP API for Biomni biomedical AI tools (MCP-compatible format)",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class CallToolRequest(BaseModel):
    tool_name: str
    tool_args: Dict[str, Any] = Field(default_factory=dict)


class ShowDataRequest(BaseModel):
    server_name: str
    offset: int = 0
    limit: int = 100


class EnvResetRequest(BaseModel):
    options: Optional[Dict[str, Any]] = None


class EnvStepRequest(BaseModel):
    action: Dict[str, Any]
    timeout_s: Optional[int] = None


# ---------------------------------------------------------------------------
# Core endpoints  (match MCP Agent Environment API)
# ---------------------------------------------------------------------------

@app.get("/")
async def root() -> dict:
    return {"message": "MCP Agent Environment API"}


@app.post("/list-tools")
async def list_tools() -> list:
    return [
        _schema_to_mcp_tool(name, schema)
        for name, schema in _tool_index.items()
    ]


@app.post("/call-tool")
async def call_tool(request: CallToolRequest) -> list:
    """Call a tool by name → ``[{"type": "text", "text": "…"}]``."""
    tool_name = request.tool_name
    tool_args = request.tool_args

    if tool_name not in _tool_functions:
        raise HTTPException(
            status_code=404,
            detail=f"Tool '{tool_name}' not found. "
                   "Use POST /list-tools to see available tools.",
        )

    tool_args = _inject_defaults(tool_name, tool_args)

    schema = _tool_index[tool_name]
    missing = [
        p["name"] for p in schema.get("required_parameters", [])
        if p.get("name") not in tool_args or tool_args[p["name"]] is None
    ]
    if missing:
        raise HTTPException(status_code=422,
                            detail=f"Missing required parameters: {missing}")

    try:
        result = _tool_functions[tool_name](**tool_args)
        return _to_content_blocks(result)
    except Exception as exc:
        logger.error("Tool '%s' failed: %s", tool_name, exc)
        logger.debug(traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail=f"Tool '{tool_name}' execution failed: "
                   f"{type(exc).__name__}: {exc}",
        )


@app.post("/show-data")
async def show_data(request: ShowDataRequest) -> list:
    raise HTTPException(
        status_code=404,
        detail=f"Server '{request.server_name}' does not support show_data. "
               "Biomni tools are stateless.",
    )


@app.post("/reset-state")
async def reset_state() -> dict:
    return {
        "status": "success",
        "message": "Reset 0/0 MCP servers",
        "results": [],
        "note": "Biomni tools are stateless and do not require reset.",
    }


@app.get("/health")
async def health() -> dict:
    return {
        "status": "health_and_client_connection_ok",
        "tools_loaded": len(_tool_functions),
        "data_lake_path": _DATA_LAKE_PATH,
    }


# #region agent log — diagnostics endpoint for remote debugging
@app.get("/diagnostics")
async def diagnostics() -> dict:
    """Exposes startup state for remote debugging — remove after investigation."""
    import sys
    return {
        "tools_in_index": len(_tool_index),
        "tools_in_functions": len(_tool_functions),
        "modules_in_registry": len(_module2api),
        "module_names": list(_module2api.keys())[:30],
        "startup_diag": _startup_diag,
        "python_version": sys.version,
        "sys_path_first_5": sys.executable,
        "env": {
            "BIOMNI_MODULES": os.environ.get("BIOMNI_MODULES", "(unset)"),
            "BIOMNI_DATA_PATH": os.environ.get("BIOMNI_DATA_PATH", "(unset)"),
            "BIOMNI_SOURCE": os.environ.get("BIOMNI_SOURCE", "(unset)"),
        },
        "cwd": os.getcwd(),
        "biomni_package_location": _get_biomni_location(),
    }


def _get_biomni_location() -> str:
    try:
        import biomni
        return str(getattr(biomni, "__file__", "unknown"))
    except Exception as e:
        return f"error: {e}"
# #endregion


# ---------------------------------------------------------------------------
# OpenEnv-compatible endpoints  (/reset, /step, /state)
# ---------------------------------------------------------------------------

@app.post("/reset")
async def env_reset(request: Optional[EnvResetRequest] = None) -> dict:
    global _episode_id, _step_count, _tools_called
    _episode_id = str(uuid.uuid4())
    _step_count = 0
    _tools_called = []
    return {
        "observation": {
            "content": [{"type": "text",
                         "text": "Environment reset. Ready for new episode."}],
            "reward": None,
            "done": False,
        },
        "reward": None,
        "done": False,
    }


@app.post("/step")
async def env_step(request: EnvStepRequest) -> dict:
    """Execute a tool call as an environment step (OpenEnv-compatible)."""
    global _step_count, _tools_called

    tool_name = request.action.get("tool_name")
    tool_args = request.action.get("tool_args", {})

    if not tool_name:
        raise HTTPException(status_code=400,
                            detail="action.tool_name is required")

    if tool_name not in _tool_functions:
        raise HTTPException(
            status_code=404,
            detail=f"Tool '{tool_name}' not found. "
                   "Use POST /list-tools to see available tools.",
        )

    tool_args = _inject_defaults(tool_name, tool_args)

    schema = _tool_index[tool_name]
    missing = [
        p["name"] for p in schema.get("required_parameters", [])
        if p.get("name") not in tool_args or tool_args[p["name"]] is None
    ]
    if missing:
        raise HTTPException(status_code=422,
                            detail=f"Missing required parameters: {missing}")

    try:
        result = _tool_functions[tool_name](**tool_args)
        content = _to_content_blocks(result)
        _step_count += 1
        _tools_called.append({"tool_name": tool_name, "tool_args": tool_args})
        return {
            "observation": {"content": content, "reward": 0.0, "done": False},
            "reward": 0.0,
            "done": False,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Tool '%s' failed: %s", tool_name, exc)
        raise HTTPException(
            status_code=500,
            detail=f"Tool '{tool_name}' execution failed: "
                   f"{type(exc).__name__}: {exc}",
        )


@app.get("/state")
async def env_state() -> dict:
    if not _episode_id:
        raise HTTPException(
            status_code=400,
            detail="Environment not initialised. Call POST /reset first.",
        )
    return {
        "episode_id": _episode_id,
        "step_count": _step_count,
        "tools_called": _tools_called,
        "available_tools": list(_tool_functions.keys()),
        "seed": None,
        "options": None,
    }


# ---------------------------------------------------------------------------
# Browsing endpoints  (Biomni-specific, not in reference API)
# ---------------------------------------------------------------------------

@app.get("/tools")
async def list_tools_get(module: Optional[str] = None) -> list:
    return [
        _schema_to_mcp_tool(name, schema)
        for name, schema in _tool_index.items()
        if module is None or _tool_modules.get(name) == module
    ]


@app.get("/tools/{tool_name}")
async def get_tool(tool_name: str) -> dict:
    if tool_name not in _tool_index:
        raise HTTPException(status_code=404,
                            detail=f"Tool '{tool_name}' not found")
    return _schema_to_mcp_tool(tool_name, _tool_index[tool_name])


@app.get("/modules")
async def list_modules() -> list:
    counts: dict[str, int] = {}
    for mod in _tool_modules.values():
        counts[mod] = counts.get(mod, 0) + 1
    return [{"module": m, "tool_count": c}
            for m, c in sorted(counts.items())]


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("BIOMNI_HOST", "0.0.0.0")
    port = int(os.environ.get("BIOMNI_PORT", "1984"))
    uvicorn.run("biomni.server:app", host=host, port=port, log_level="info")
