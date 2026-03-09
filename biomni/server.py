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
from fastapi.responses import StreamingResponse
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


def _resolve_json_schema_type(type_str: str) -> dict:
    """Map a Python type annotation string to a JSON Schema type fragment.

    Handles compound types like List[int], Dict[str, Any], Optional[str],
    Union[str, List[str]], Tuple[int, int], and pipe unions (str|list[str]).
    """
    if not type_str:
        return {"type": "string"}

    t = type_str.strip()
    low = t.lower()

    if low in _TYPE_MAP:
        return {"type": _TYPE_MAP[low]}

    # "X or Y" patterns (e.g. "List[float] or numpy.ndarray") — take first
    if " or " in low:
        return _resolve_json_schema_type(t.split(" or ")[0].strip())

    # Pipe unions: "str|list[str]" → pick the most structured alternative
    if "|" in t and "[" not in t.split("|")[0]:
        parts = [p.strip() for p in t.split("|")]
        for p in parts:
            if p.lower().startswith(("list", "dict")):
                return _resolve_json_schema_type(p)
        return _resolve_json_schema_type(parts[0])

    # Optional[X] → resolve X (nullable at the JSON level)
    if low.startswith("optional[") and t.endswith("]"):
        inner = t[len("Optional["):-1]
        return _resolve_json_schema_type(inner)

    # Union[X, Y, ...] → pick the first non-None type
    if low.startswith("union[") and t.endswith("]"):
        inner = t[len("Union["):-1]
        parts = _split_type_args(inner)
        for p in parts:
            if p.strip().lower() not in ("none", "nonetype"):
                return _resolve_json_schema_type(p.strip())
        return {"type": "string"}

    # List[X] / list[X] → array with items
    if low.startswith(("list[", "list[")) and t.endswith("]"):
        inner = t[t.index("[") + 1:-1]
        return {"type": "array", "items": _resolve_json_schema_type(inner)}

    # Dict[K, V] / dict[K, V] → object
    if low.startswith(("dict[",)) and t.endswith("]"):
        return {"type": "object"}

    # Tuple[X, Y] → array (fixed-length)
    if low.startswith("tuple[") and t.endswith("]"):
        inner = t[len("Tuple["):-1]
        parts = _split_type_args(inner)
        return {
            "type": "array",
            "items": _resolve_json_schema_type(parts[0].strip()) if parts else {"type": "string"},
        }

    return {"type": _TYPE_MAP.get(low, "string")}


def _split_type_args(s: str) -> list[str]:
    """Split comma-separated type arguments respecting bracket nesting."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in s:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return parts


def _param_to_json_schema_prop(param: dict) -> dict:
    prop = _resolve_json_schema_type(param.get("type", "string"))
    if "description" in param:
        prop["description"] = param["description"]
    return prop


def _coerce_arg(value: Any, declared_type: str) -> Any:
    """Best-effort coercion when a client sends a string where a richer type
    is expected (common when the schema previously emitted ``"string"`` for
    compound types).
    """
    if not isinstance(value, str) or not declared_type:
        return value

    low = declared_type.lower().strip()

    is_array = low.startswith(("list[", "list", "tuple[", "tuple"))
    is_object = low.startswith(("dict[", "dict"))
    is_int = low in ("int", "integer")
    is_float = low in ("float", "number")
    is_bool = low in ("bool", "boolean")

    # Pipe unions / "or" — check if any branch is structured
    if "|" in low or " or " in low:
        parts = low.replace(" or ", "|").split("|")
        for p in parts:
            p = p.strip()
            if p.startswith(("list", "dict", "tuple")):
                is_array = p.startswith(("list", "tuple"))
                is_object = p.startswith("dict")
                break

    if low.startswith("optional["):
        inner = declared_type.strip()[len("Optional["):-1]
        return _coerce_arg(value, inner)

    if low.startswith("union["):
        inner = declared_type.strip()[len("Union["):-1]
        parts = _split_type_args(inner)
        for p in parts:
            p = p.strip()
            if p.lower() not in ("none", "nonetype", "str", "string"):
                return _coerce_arg(value, p)
        return value

    if is_array or is_object:
        stripped = value.strip()
        if stripped and stripped[0] in "[{":
            try:
                return json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                pass
        return value

    if is_int:
        try:
            return int(value)
        except (ValueError, TypeError):
            return value

    if is_float:
        try:
            return float(value)
        except (ValueError, TypeError):
            return value

    if is_bool:
        if value.lower() in ("true", "1", "yes"):
            return True
        if value.lower() in ("false", "0", "no"):
            return False

    return value


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


def _inject_defaults(tool_name: str, tool_args: dict) -> dict:
    """Auto-inject ``data_lake_path`` for tools that declare the parameter."""
    schema = _tool_index.get(tool_name, {})
    all_params = schema.get("required_parameters", []) + schema.get("optional_parameters", [])
    param_names = {p["name"] for p in all_params if "name" in p}

    if "data_lake_path" in param_names and "data_lake_path" not in tool_args:
        tool_args = {**tool_args, "data_lake_path": _DATA_LAKE_PATH}

    return tool_args


def _coerce_tool_args(tool_name: str, tool_args: dict) -> dict:
    """Coerce string arguments to their declared types when possible."""
    schema = _tool_index.get(tool_name, {})
    all_params = schema.get("required_parameters", []) + schema.get("optional_parameters", [])
    type_map = {p["name"]: p.get("type", "string") for p in all_params if "name" in p}

    coerced = {}
    for key, value in tool_args.items():
        declared = type_map.get(key)
        if declared:
            coerced[key] = _coerce_arg(value, declared)
        else:
            coerced[key] = value
    return coerced


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
    tool_args = _coerce_tool_args(tool_name, tool_args)

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
    tool_args = _coerce_tool_args(tool_name, tool_args)

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


# ---------------------------------------------------------------------------
# Chat endpoint  (wraps the ReAct agent for conversational use)
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    messages: list[Dict[str, str]]
    config: Optional[Dict[str, Any]] = Field(default_factory=dict)


class ChatToolCall(BaseModel):
    name: str
    args: Dict[str, Any] = Field(default_factory=dict)
    result: Optional[str] = None
    error: Optional[str] = None


class TrajectoryStep(BaseModel):
    """A single step in the agent's reasoning trajectory."""
    step: int
    type: str  # "reasoning", "tool_call", "tool_result"
    content: Optional[str] = None
    tool_name: Optional[str] = None
    tool_args: Optional[Dict[str, Any]] = None
    tool_call_id: Optional[str] = None


class ChatResponse(BaseModel):
    response: str
    tool_calls: list[ChatToolCall] = Field(default_factory=list)
    trajectory: list[TrajectoryStep] = Field(default_factory=list)


# Lazy-initialized agent singleton (heavy startup, reuse across requests)
_chat_agent = None
_chat_agent_lock = None


def _get_chat_agent():
    """Lazily initialize the ReAct agent for chat.

    Uses the server's already-validated tool registry to avoid
    AttributeError from tools whose functions are missing in
    the installed biomni package (e.g. bioimaging helpers that
    only exist in newer source but not in the base Docker image).
    """
    global _chat_agent, _chat_agent_lock
    import threading
    if _chat_agent_lock is None:
        _chat_agent_lock = threading.Lock()

    with _chat_agent_lock:
        if _chat_agent is not None:
            return _chat_agent

        from biomni.agent.react import react as ReactAgent
        from biomni.config import BiomniConfig, default_config
        from biomni.llm import get_llm
        from langchain_core.tools import StructuredTool

        config = BiomniConfig()

        # Build LangChain tools directly from the server's validated
        # _tool_functions registry.  We skip api_schema_to_langchain_tool()
        # because it re-imports the module and does getattr(module, name),
        # which crashes on functions missing from the installed package.
        safe_tools = []
        for name, fn in _tool_functions.items():
            schema = _tool_index.get(name)
            if schema is None:
                continue
            try:
                tool = StructuredTool.from_function(
                    func=fn,
                    name=name,
                    description=schema.get("description", ""),
                    return_direct=True,
                )
                safe_tools.append(tool)
            except Exception as exc:
                logger.warning("Skipping tool %s for chat agent: %s", name, exc)

        logger.info("Chat agent initialized with %d tools (from %d registered)",
                     len(safe_tools), len(_tool_functions))

        agent = ReactAgent.__new__(ReactAgent)
        # Manually initialize the fields that configure() needs
        agent.path = config.path
        agent.llm = get_llm(config.llm, config=default_config)
        agent.timeout_seconds = config.timeout_seconds or 600
        agent.tools = agent._add_timeout_to_tools(safe_tools)
        agent.module2api = _module2api
        agent.use_tool_retriever = False  # tools already filtered
        from biomni.env_desc import data_lake_dict, library_content_dict
        agent.data_lake_dict = data_lake_dict
        agent.library_content_dict = library_content_dict
        agent.prompt = ""
        agent.system_prompt = ""

        agent.configure(
            plan=True,
            reflect=True,
            data_lake=True,
            library_access=True,
        )
        _chat_agent = agent
        return _chat_agent


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """
    Chat with the BiOMNI agent using natural language.

    The agent will autonomously select and chain tools to answer
    the user's question.

    Request body:
        messages: list of {role: "user"|"assistant", content: "..."}
        config: optional overrides (e.g. {"llm": "claude-opus-4-6"})

    Response:
        response: the agent's final text answer
        tool_calls: list of tools the agent called (name, args, result/error)
        thinking: agent's reasoning trace (if available)
    """
    import asyncio

    messages = request.messages
    if not messages:
        raise HTTPException(status_code=400, detail="messages list is empty")

    # Extract the latest user message as the prompt
    user_messages = [m for m in messages if m.get("role") == "user"]
    if not user_messages:
        raise HTTPException(status_code=400, detail="No user message found")

    # Build context from conversation history
    prompt_parts = []
    if len(messages) > 1:
        prompt_parts.append("Previous conversation:")
        for msg in messages[:-1]:
            role = msg.get("role", "user").capitalize()
            content = msg.get("content", "")
            prompt_parts.append(f"{role}: {content}")
        prompt_parts.append("")
        prompt_parts.append("Current question:")
    prompt_parts.append(user_messages[-1].get("content", ""))
    full_prompt = "\n".join(prompt_parts)

    try:
        agent = _get_chat_agent()

        # Run the agent and collect raw LangGraph messages for trajectory
        def run_agent_with_messages(prompt):
            config = {"recursion_limit": 50}
            inputs = {"messages": [("user", prompt)]}
            all_messages = []
            for s in agent.app.stream(inputs, stream_mode="values", config=config):
                all_messages = s["messages"]
            final_content = all_messages[-1].content if all_messages else ""
            return all_messages, final_content

        all_messages, final_content = await asyncio.to_thread(
            run_agent_with_messages, full_prompt
        )

        # Build structured trajectory and tool_calls from raw messages
        trajectory = []
        tool_calls = []
        step_num = 0

        for msg in all_messages:
            msg_type = getattr(msg, "type", "unknown")

            if msg_type == "human":
                # Skip the user input message
                continue

            elif msg_type == "ai":
                # AI message — may contain reasoning text and/or tool calls
                content = msg.content

                # Extract text reasoning (may be string or list of content blocks)
                reasoning_text = ""
                if isinstance(content, str) and content.strip():
                    reasoning_text = content
                elif isinstance(content, list):
                    text_parts = [
                        block["text"] for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                    reasoning_text = "\n".join(text_parts)

                if reasoning_text.strip():
                    step_num += 1
                    trajectory.append(TrajectoryStep(
                        step=step_num,
                        type="reasoning",
                        content=reasoning_text,
                    ))

                # Extract tool calls
                for tc in getattr(msg, "tool_calls", []):
                    step_num += 1
                    tool_name = tc.get("name", tc.get("function", {}).get("name", "unknown"))
                    tool_args = tc.get("args", {})
                    tool_id = tc.get("id", "")

                    trajectory.append(TrajectoryStep(
                        step=step_num,
                        type="tool_call",
                        tool_name=tool_name,
                        tool_args=tool_args,
                        tool_call_id=tool_id,
                    ))

                    tool_calls.append(ChatToolCall(
                        name=tool_name,
                        args=tool_args,
                    ))

            elif msg_type == "tool":
                # Tool result message
                step_num += 1
                tool_name = getattr(msg, "name", "unknown")
                result_content = msg.content
                if isinstance(result_content, str) and len(result_content) > 4000:
                    result_content = result_content[:4000] + "... [truncated]"

                trajectory.append(TrajectoryStep(
                    step=step_num,
                    type="tool_result",
                    tool_name=tool_name,
                    content=result_content,
                    tool_call_id=getattr(msg, "tool_call_id", None),
                ))

                # Match result back to the tool_call
                for tc in reversed(tool_calls):
                    if tc.name == tool_name and tc.result is None:
                        tc.result = result_content[:2000] if result_content else ""
                        break

        return ChatResponse(
            response=final_content,
            tool_calls=tool_calls,
            trajectory=trajectory,
        )

    except Exception as exc:
        logger.error("Chat endpoint failed: %s", exc)
        logger.debug(traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail=f"Agent execution failed: {type(exc).__name__}: {exc}",
        )


@app.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    """
    Streaming chat endpoint — returns NDJSON lines as the agent reasons.

    Each line is a JSON object with a ``type`` discriminator:
    - ``trajectory_step``: incremental reasoning/tool_call/tool_result
    - ``final_response``: full response + trajectory when agent finishes
    - ``error``: if something goes wrong mid-stream
    """
    import asyncio

    messages = request.messages
    if not messages:
        raise HTTPException(status_code=400, detail="messages list is empty")

    user_messages = [m for m in messages if m.get("role") == "user"]
    if not user_messages:
        raise HTTPException(status_code=400, detail="No user message found")

    # Build context from conversation history (same as /chat)
    prompt_parts = []
    if len(messages) > 1:
        prompt_parts.append("Previous conversation:")
        for msg in messages[:-1]:
            role = msg.get("role", "user").capitalize()
            content = msg.get("content", "")
            prompt_parts.append(f"{role}: {content}")
        prompt_parts.append("")
        prompt_parts.append("Current question:")
    prompt_parts.append(user_messages[-1].get("content", ""))
    full_prompt = "\n".join(prompt_parts)

    def generate():
        try:
            agent = _get_chat_agent()
            config = {"recursion_limit": 50}
            inputs = {"messages": [("user", full_prompt)]}

            step_counter = 0
            all_steps = []
            tool_calls_list = []
            final_response = ""
            processed_count = 0

            for chunk in agent.app.stream(inputs, stream_mode="values", config=config):
                chunk_messages = chunk.get("messages", [])
                # Process only new messages since last chunk
                new_messages = chunk_messages[processed_count:]
                processed_count = len(chunk_messages)

                for msg in new_messages:
                    msg_type = getattr(msg, "type", "unknown")

                    if msg_type == "human":
                        continue

                    elif msg_type == "ai":
                        content = msg.content
                        reasoning_text = ""
                        if isinstance(content, str) and content.strip():
                            reasoning_text = content
                        elif isinstance(content, list):
                            text_parts = [
                                block["text"] for block in content
                                if isinstance(block, dict) and block.get("type") == "text"
                            ]
                            reasoning_text = "\n".join(text_parts)

                        if reasoning_text.strip():
                            step_counter += 1
                            step = {"step": step_counter, "type": "reasoning", "content": reasoning_text}
                            all_steps.append(step)
                            final_response = reasoning_text
                            yield json.dumps({"type": "trajectory_step", "step": step}) + "\n"

                        for tc in getattr(msg, "tool_calls", []):
                            step_counter += 1
                            tool_name = tc.get("name", tc.get("function", {}).get("name", "unknown"))
                            tool_args = tc.get("args", {})
                            tool_id = tc.get("id", "")
                            step = {
                                "step": step_counter,
                                "type": "tool_call",
                                "tool_name": tool_name,
                                "tool_args": tool_args,
                                "tool_call_id": tool_id,
                            }
                            all_steps.append(step)
                            tool_calls_list.append({"name": tool_name, "args": tool_args})
                            yield json.dumps({"type": "trajectory_step", "step": step}) + "\n"

                    elif msg_type == "tool":
                        step_counter += 1
                        tool_name = getattr(msg, "name", "unknown")
                        result_content = msg.content
                        if isinstance(result_content, str) and len(result_content) > 4000:
                            result_content = result_content[:4000] + "... [truncated]"
                        step = {
                            "step": step_counter,
                            "type": "tool_result",
                            "tool_name": tool_name,
                            "content": result_content,
                            "tool_call_id": getattr(msg, "tool_call_id", None),
                        }
                        all_steps.append(step)
                        yield json.dumps({"type": "trajectory_step", "step": step}) + "\n"

                        # Match result back to tool_call
                        for tc in reversed(tool_calls_list):
                            if tc["name"] == tool_name and "result" not in tc:
                                tc["result"] = result_content[:2000] if result_content else ""
                                break

            # Emit final response with full trajectory
            yield json.dumps({
                "type": "final_response",
                "response": final_response,
                "trajectory": all_steps,
                "tool_calls": tool_calls_list,
            }) + "\n"

        except Exception as exc:
            logger.error("Chat stream failed: %s", exc)
            logger.debug(traceback.format_exc())
            yield json.dumps({
                "type": "error",
                "message": f"Agent execution failed: {type(exc).__name__}: {exc}",
            }) + "\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson")
