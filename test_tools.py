#!/usr/bin/env python3
"""
Biomni Tool Health Check — Network Edition (Real Args)
======================================================
Tests every tool against the live Biomni server via HTTP using realistic
arguments from test_tool_args.json.

Three levels of checks per tool:
  1. REGISTERED  — tool appears in the server's /list-tools response
  2. CALLABLE    — POST /call-tool returns 2xx with real arguments
  3. SCHEMA      — server schema matches biomni_tools.json (what the model sees)

Argument source priority:
  1. test_tool_args.json  — hand-crafted realistic arguments
  2. Auto-generated stubs — fallback for tools not in the fixture

Usage:
    python test_tools.py                        # full report
    python test_tools.py --json                 # machine-readable JSON
    python test_tools.py --timeout 30           # per-tool timeout (default 30s)
    python test_tools.py --base-url http://host:port
    python test_tools.py --skip-call            # only check registration + schema
    python test_tools.py --tools query_arxiv,blast_sequence  # test specific tools
    python test_tools.py --stubs-only           # ignore fixture, use auto-stubs
"""

import argparse
import json
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
except ImportError:
    sys.exit("requests is required: pip install requests")


# ── Fixture loading ─────────────────────────────────────────────────────────

def load_fixture_args(fixture_path: Path) -> dict:
    """Load hand-crafted tool arguments from test_tool_args.json."""
    if not fixture_path.exists():
        return {}
    with open(fixture_path) as f:
        return json.load(f)


# ── Stub argument generators (fallback) ─────────────────────────────────────

def _stub_value(prop_schema: dict, param_name: str) -> object:
    """Generate a minimal stub value for a parameter based on its schema type."""
    t = prop_schema.get("type", "string")
    name = param_name.lower()

    if "smiles" in name:
        return "CC(=O)Oc1ccccc1C(=O)O"
    if "sequence" in name and "amino" not in name and "protein" not in name:
        return "ATCGATCGATCGATCGATCG"
    if "amino" in name or ("protein" in name and "sequence" in name):
        return "MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTKTYFPHFDLSH"
    if name in ("doi",):
        return "10.1038/s41586-020-2649-2"
    if name in ("query", "prompt", "question", "search_term", "query_term"):
        return "TP53 tumor suppressor"
    if name in ("drug_name", "target_drug"):
        return "aspirin"
    if name in ("gene_name", "gene_symbol"):
        return "TP53"
    if name in ("uniprot_id",):
        return "P04637"
    if name in ("token",):
        return "test_token"
    if "url" in name:
        return "https://example.com"
    if "email" in name:
        return "test@example.com"
    if "pdb" in name and "file" in name or "path" in name:
        return "/tmp/test.pdb"
    if "path" in name or "file" in name or "dir" in name:
        return "/tmp/biomni_test"
    if name in ("organism", "species"):
        return "Homo sapiens"
    if name in ("tissue_type",):
        return "liver"
    if name in ("target_cell_type",):
        return "macrophages"
    if name in ("disease_name",):
        return "asthma"
    if name in ("data_lake_path",):
        return "/tmp/datalake"
    if "identifier" in name:
        return "pUC19"
    if "chromosome" in name or name in ("coord_chrom",):
        return "chr1"

    if t == "string":
        return "test"
    if t == "integer":
        return 1
    if t == "number":
        return 1.0
    if t == "boolean":
        return True
    if t == "array":
        return []
    if t == "object":
        return {}
    return "test"


def build_stub_args(tool_schema: dict) -> dict:
    """Build a minimal argument dict from a tool's inputSchema."""
    input_schema = tool_schema.get("inputSchema", {})
    properties = input_schema.get("properties", {})
    required = set(input_schema.get("required", []))

    args = {}
    for pname, pschema in properties.items():
        if pname in required:
            args[pname] = _stub_value(pschema, pname)
    return args


def get_tool_args(tool_name: str, tool_schema: dict, fixture: dict, use_stubs: bool) -> tuple[dict, str]:
    """Return (args_dict, source_label) for a tool call."""
    if not use_stubs and tool_name in fixture:
        return fixture[tool_name], "fixture"
    return build_stub_args(tool_schema), "auto-stub"


# ── Server interaction ──────────────────────────────────────────────────────

def fetch_server_tools(base_url: str, timeout: int) -> list[dict]:
    resp = requests.post(f"{base_url}/list-tools", timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def call_tool(base_url: str, tool_name: str, tool_args: dict, timeout: int) -> dict:
    """Call a single tool and return a result dict."""
    t0 = time.time()
    try:
        resp = requests.post(
            f"{base_url}/call-tool",
            json={"tool_name": tool_name, "tool_args": tool_args},
            timeout=timeout,
        )
        elapsed = time.time() - t0
        if resp.status_code == 200:
            return {
                "status": "callable",
                "http_code": 200,
                "elapsed_s": round(elapsed, 2),
                "response_preview": str(resp.json())[:500],
            }
        else:
            detail = ""
            try:
                detail = resp.json().get("detail", "")
            except Exception:
                detail = resp.text[:500]
            return {
                "status": "ran_but_errored",
                "http_code": resp.status_code,
                "elapsed_s": round(elapsed, 2),
                "error": detail,
            }
    except requests.exceptions.Timeout:
        return {
            "status": "timeout",
            "http_code": None,
            "elapsed_s": round(time.time() - t0, 2),
            "error": f"timed out after {timeout}s",
        }
    except requests.exceptions.ConnectionError as e:
        return {
            "status": "connection_error",
            "http_code": None,
            "elapsed_s": round(time.time() - t0, 2),
            "error": str(e)[:300],
        }
    except Exception as e:
        return {
            "status": "crashed",
            "http_code": None,
            "elapsed_s": round(time.time() - t0, 2),
            "error": f"{type(e).__name__}: {e}",
        }


# ── Schema comparison ───────────────────────────────────────────────────────

def compare_schemas(json_tool: dict, server_tool: dict) -> list[str]:
    """Compare a biomni_tools.json entry to the server's schema. Return issues."""
    issues = []

    j_props = set(json_tool.get("inputSchema", {}).get("properties", {}).keys())
    s_props = set(server_tool.get("inputSchema", {}).get("properties", {}).keys())

    if j_props != s_props:
        missing_from_server = j_props - s_props
        extra_on_server = s_props - j_props
        if missing_from_server:
            issues.append(f"params in JSON but not server: {sorted(missing_from_server)}")
        if extra_on_server:
            issues.append(f"params on server but not JSON: {sorted(extra_on_server)}")

    j_req = set(json_tool.get("inputSchema", {}).get("required", []))
    s_req = set(server_tool.get("inputSchema", {}).get("required", []))
    if j_req != s_req:
        issues.append(f"required mismatch — json: {sorted(j_req)}, server: {sorted(s_req)}")

    return issues


# ── Main test runner ────────────────────────────────────────────────────────

def run_tests(base_url, timeout, skip_call, max_workers, only_tools=None, use_stubs=False):
    json_path = Path(__file__).parent / "biomni_tools.json"
    fixture_path = Path(__file__).parent / "test_tool_args.json"

    json_tools = {}
    if json_path.exists():
        with open(json_path) as f:
            json_tools = {t["name"]: t for t in json.load(f)}

    fixture = load_fixture_args(fixture_path)

    server_tool_list = fetch_server_tools(base_url, timeout)
    server_tools = {t["name"]: t for t in server_tool_list}

    if only_tools:
        test_names = [n for n in only_tools if n in server_tools or n in json_tools]
    else:
        test_names = sorted(set(list(server_tools.keys()) + list(json_tools.keys())))

    fixture_count = 0
    stub_count = 0
    results = []

    def _test_one(tool_name):
        nonlocal fixture_count, stub_count
        entry = {
            "tool_name": tool_name,
            "on_server": tool_name in server_tools,
            "in_json": tool_name in json_tools,
            "schema_issues": [],
            "call_result": None,
            "args_source": None,
        }

        if tool_name in server_tools and tool_name in json_tools:
            entry["schema_issues"] = compare_schemas(
                json_tools[tool_name], server_tools[tool_name]
            )

        if not skip_call and tool_name in server_tools:
            schema = server_tools[tool_name]
            args, source = get_tool_args(tool_name, schema, fixture, use_stubs)
            entry["tool_args"] = args
            entry["args_source"] = source
            entry["call_result"] = call_tool(base_url, tool_name, args, timeout)

        return entry

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_test_one, name): name for name in test_names}
        for future in as_completed(futures):
            results.append(future.result())

    results.sort(key=lambda r: r["tool_name"])

    fixture_count = sum(1 for r in results if r.get("args_source") == "fixture")
    stub_count = sum(1 for r in results if r.get("args_source") == "auto-stub")

    return results, json_tools, server_tools, fixture_count, stub_count


# ── Reporting ───────────────────────────────────────────────────────────────

SYMBOLS = {
    "callable": "\u2705",
    "ran_but_errored": "\u26a0\ufe0f ",
    "timeout": "\u23f0",
    "connection_error": "\u274c",
    "crashed": "\U0001f4a5",
}

SRC_TAG = {"fixture": "[real]", "auto-stub": "[stub]"}


def print_report(results, json_tools, server_tools, fixture_count, stub_count, verbose=False):
    W = 92

    print(f"\n{'=' * W}")
    print(f"  BIOMNI TOOL HEALTH CHECK — LIVE SERVER (Real Args)")
    print(f"{'=' * W}")
    print(f"  Server tools:            {len(server_tools)}")
    print(f"  biomni_tools.json:       {len(json_tools)}")
    print(f"  Tools tested:            {len(results)}")
    print(f"  Args from fixture:       {fixture_count}")
    print(f"  Args auto-generated:     {stub_count}")
    print(f"{'=' * W}\n")

    json_only = [r for r in results if r["in_json"] and not r["on_server"]]
    if json_only:
        print(f"{'':─<{W}}")
        print(f"  MODEL SEES BUT SERVER DOESN'T HAVE ({len(json_only)} tools)")
        print(f"{'':─<{W}}")
        for r in json_only:
            print(f"    {r['tool_name']}")
        print()

    server_only = [r for r in results if r["on_server"] and not r["in_json"]]
    if server_only:
        print(f"{'':─<{W}}")
        print(f"  SERVER HAS BUT MODEL CAN'T SEE ({len(server_only)} tools)")
        print(f"{'':─<{W}}")
        for r in server_only:
            print(f"    {r['tool_name']}")
        print()

    schema_issues = [r for r in results if r["schema_issues"]]
    if schema_issues:
        print(f"{'':─<{W}}")
        print(f"  SCHEMA MISMATCHES ({len(schema_issues)} tools)")
        print(f"{'':─<{W}}")
        for r in schema_issues:
            print(f"    {r['tool_name']}")
            for issue in r["schema_issues"]:
                print(f"      - {issue}")
        print()

    called = [r for r in results if r["call_result"] is not None]
    buckets = {}
    if called:
        for r in called:
            status = r["call_result"]["status"]
            buckets.setdefault(status, []).append(r)

        ok = buckets.get("callable", [])
        erred = buckets.get("ran_but_errored", [])
        timedout = buckets.get("timeout", [])
        crashed = buckets.get("connection_error", []) + buckets.get("crashed", [])

        print(f"{'':─<{W}}")
        print(f"  CALL RESULTS")
        print(f"{'':─<{W}}")
        print(f"    {SYMBOLS['callable']} Returned 200 (tool works):         {len(ok)}")
        print(f"    {SYMBOLS['ran_but_errored']}Ran but errored (needs real input):   {len(erred)}")
        print(f"    {SYMBOLS['timeout']} Timed out:                         {len(timedout)}")
        print(f"    {SYMBOLS['crashed']} Crashed / connection error:         {len(crashed)}")
        print()

        if ok:
            print(f"  {'── Returned 200 ':─<{W}}")
            for r in ok:
                cr = r["call_result"]
                tag = SRC_TAG.get(r.get("args_source", ""), "")
                print(f"    {SYMBOLS['callable']} {r['tool_name']}  ({cr['elapsed_s']}s) {tag}")
                if verbose and cr.get("response_preview"):
                    preview = cr["response_preview"][:200].replace("\n", " ")
                    print(f"       -> {preview}")
            print()

        if erred:
            print(f"  {'── Ran but errored ':─<{W}}")
            for r in erred:
                cr = r["call_result"]
                err_msg = cr.get("error", "")
                short = err_msg.split(":")
                if len(short) >= 2:
                    short_err = ":".join(short[:2])[:120]
                else:
                    short_err = err_msg[:120]
                tag = SRC_TAG.get(r.get("args_source", ""), "")
                print(f"    {SYMBOLS['ran_but_errored']}{r['tool_name']} {tag}")
                print(f"       HTTP {cr['http_code']} ({cr['elapsed_s']}s): {short_err}")
            print()

        if timedout:
            print(f"  {'── Timed out ':─<{W}}")
            for r in timedout:
                tag = SRC_TAG.get(r.get("args_source", ""), "")
                print(f"    {SYMBOLS['timeout']} {r['tool_name']}  ({r['call_result']['elapsed_s']}s) {tag}")
            print()

        if crashed:
            print(f"  {'── Crashed ':─<{W}}")
            for r in crashed:
                cr = r["call_result"]
                print(f"    {SYMBOLS['crashed']} {r['tool_name']}: {cr.get('error', '')[:120]}")
            print()

    print(f"{'=' * W}")
    print(f"  SUMMARY")
    print(f"{'=' * W}")
    print(f"  In JSON but not on server:   {len(json_only):>3}   (model hallucinates these)")
    print(f"  On server but not in JSON:   {len(server_only):>3}   (model can't use these)")
    print(f"  Schema mismatches:           {len(schema_issues):>3}")
    if called:
        print(f"  Callable (200):              {len(ok):>3}")
        print(f"  Ran but errored (expected):  {len(erred):>3}")
        print(f"  Timed out:                   {len(timedout):>3}")
        print(f"  Crashed:                     {len(crashed):>3}")
    print(f"{'=' * W}\n")

    total_issues = len(json_only) + len(server_only) + len(schema_issues) + len(
        buckets.get("timeout", [])
    ) + len(buckets.get("connection_error", [])) + len(buckets.get("crashed", []))
    return total_issues


def main():
    parser = argparse.ArgumentParser(description="Biomni Tool Health Check (real args)")
    parser.add_argument("--base-url", default="http://localhost:1984",
                        help="Biomni server URL (default: http://localhost:1984)")
    parser.add_argument("--timeout", type=int, default=30,
                        help="Per-tool call timeout in seconds (default: 30)")
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="Output machine-readable JSON")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Show response previews for passing tools")
    parser.add_argument("--skip-call", action="store_true",
                        help="Only check registration and schema, skip calling tools")
    parser.add_argument("--workers", type=int, default=4,
                        help="Parallel workers for tool calls (default: 4)")
    parser.add_argument("--tools", type=str, default=None,
                        help="Comma-separated list of tool names to test (default: all)")
    parser.add_argument("--stubs-only", action="store_true",
                        help="Ignore fixture file, use auto-generated stubs only")
    args = parser.parse_args()

    only_tools = None
    if args.tools:
        only_tools = [t.strip() for t in args.tools.split(",")]

    try:
        results, json_tools, server_tools, fc, sc = run_tests(
            args.base_url, args.timeout, args.skip_call, args.workers,
            only_tools, args.stubs_only
        )
    except requests.exceptions.ConnectionError:
        sys.exit(f"Could not connect to {args.base_url}. Is the server running?")

    if args.json_output:
        output = {
            "server_tool_count": len(server_tools),
            "json_tool_count": len(json_tools),
            "fixture_args_used": fc,
            "stub_args_used": sc,
            "results": results,
        }
        print(json.dumps(output, indent=2, default=str))
    else:
        print_report(results, json_tools, server_tools, fc, sc, verbose=args.verbose)


if __name__ == "__main__":
    main()
