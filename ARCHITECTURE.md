# BiOMNI Architecture

## Overview

BiOMNI is a biomedical AI agent platform with 223+ tools spanning genomics, proteomics, drug discovery, clinical data, and more. It exposes two agent architectures:

- **Biomni GTM (ReAct)** — Function-calling agent that uses `bind_tools()` to let the LLM select and invoke tools directly
- **Biomni A1** — Code-generation agent that writes and executes Python/R/bash code using XML tags (`<think>`, `<execute>`, `<solution>`)

Both agents share the same tool registry, data lake, and Docker infrastructure.

## System Architecture

```
┌─────────────────────────────────────────────────────┐
│                    Frontend (Next.js)                │
│  ~/scaleapi/packages/professional-agents/            │
│                                                     │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐ │
│  │ Agent Cards  │  │Model Select │  │  Chat UI    │ │
│  │ GTM / A1    │  │ Opus/Sonnet │  │ + Trajectory│ │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘ │
│         └────────────────┴────────────────┘         │
│                         │                           │
│              /api/[agentType]/chat                   │
└─────────────────────────┬───────────────────────────┘
                          │ NDJSON stream
                          ▼
┌─────────────────────────────────────────────────────┐
│              Modal Function (ASGI)                   │
│  ~/Code/professional-agents/modal_biomni.py          │
│                                                     │
│  - ECR image: agent-environment:biomni-X.Y.Z        │
│  - LiteLLM proxy: litellm-proxy.ml.scale.com        │
│  - Secrets: biomni-llm-config                       │
└─────────────────────────┬───────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────┐
│              FastAPI Server (biomni/server.py)        │
│                                                     │
│  Endpoints:                                         │
│    POST /chat          → ReAct (GTM) agent          │
│    POST /chat/stream   → ReAct streaming            │
│    POST /chat/a1       → A1 code-gen agent          │
│    POST /chat/a1/stream→ A1 streaming               │
│    POST /call-tool     → Direct tool invocation     │
│    POST /list-tools    → MCP tool listing            │
│    GET  /health        → Health check               │
│                                                     │
│  ┌──────────────────────────────────────────────┐   │
│  │           Tool Registry (223 tools)           │   │
│  │  _tool_functions, _tool_index, _module2api   │   │
│  └──────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────┘
```

## Agent Architectures

### ReAct (GTM) Agent

LangGraph `StateGraph` with two nodes:

```
User prompt → [agent] → should_continue? → [tools] → [agent] → ... → END
                 │                            │
                 │  llm.bind_tools(tools)      │  Execute tool in subprocess
                 │  Returns tool_calls         │  with timeout wrapper
```

- **agent node**: Calls LLM with tools bound via `bind_tools()`. LLM returns reasoning text + tool_calls.
- **tools node**: Executes each tool call in a subprocess with configurable timeout. Returns ToolMessage.
- **Routing**: If tool_calls present → tools node; otherwise → END.
- **System prompt**: Includes plan/reflect/data_lake/library_access directives.
- **Trajectory**: Extracted from LangGraph messages — reasoning, thinking blocks, tool_calls, tool_results.

### A1 (Code-Gen) Agent

LangGraph `StateGraph` with two nodes:

```
User prompt → [generate] → routing? → [execute] → [generate] → ... → END
                  │                       │
                  │  LLM generates XML    │  Parse <execute> tag,
                  │  <think>/<execute>/   │  run Python/R/bash
                  │  <solution>           │  Return <observation>
```

- **generate node**: Calls LLM with system prompt containing all tool docs. LLM responds with XML tags:
  - `<think>` — Reasoning/planning
  - `<execute>` — Code to run (Python/R/bash)
  - `<solution>` — Final answer
- **execute node**: Parses `<execute>` block, detects language (`#!R`, `#!BASH`, or Python by default), runs with timeout, returns `<observation>`.
- **Routing**: `<execute>` → execute node; `<think>` only → generate again; `<solution>` → END.
- **Trajectory**: Extracted by parsing XML tags from AIMessage content.

## LLM Configuration

All LLM calls go through `biomni/llm.py:get_llm()`, which supports:

| Provider | Models | Notes |
|----------|--------|-------|
| Custom (LiteLLM) | Any model via proxy | Default in Modal deployment |
| Anthropic | Claude family | Direct API |
| OpenAI | GPT family | Direct API |
| Bedrock | Claude, Llama, etc. | AWS credentials required |
| Gemini | Gemini family | Google API |

**Reasoning effort** (`BIOMNI_REASONING_EFFORT`): Controls extended thinking for models that support it. Values: `low`, `medium`, `high`, `max`. Passed via `extra_body` for LiteLLM compatibility.

**Model selector**: The frontend can pass a `model` field in chat requests to override the default LLM per-request. Agents are pooled by model name to avoid re-initialization.

## Docker Build Pipeline

```
  Base image (biomni-1.0.X)          ← Manual rebuild, ~6-10 hours
       │                                (conda env + CLI tools)
       ▼
  Fast build (biomni-server-<SHA>)   ← CI on every merge, ~2 min
       │                                (layers Python source on base)
       ▼
  Modal deploy                       ← Auto-updates live endpoint
```

### Build Hosts

| Host | Use Case | Notes |
|------|----------|-------|
| `osworld-evaluations` | **Preferred** for fast builds | EC2, Docker 25.0, x86_64, 78GB free |
| `devbox-biomni-build` | Fallback for fast builds | Scale DevBox |
| `devbox` | Legacy fallback | Generic DevBox |

### Key Files

| File | Purpose |
|------|---------|
| `Dockerfile` | Fast CI build (~2 min) |
| `Dockerfile.full` | Full rebuild (~6-10 hours) |
| `BASE_IMAGE_TAG` | Pins base image for fast builds |
| `rebuild-base-image.sh` | Build automation script |

## Repository Layout

### Backend (`~/Code/Biomni/`)

```
biomni/
├── agent/
│   ├── a1.py              # A1 code-gen agent (3000+ lines)
│   └── react.py           # ReAct function-calling agent
├── server.py              # FastAPI HTTP server
├── llm.py                 # LLM factory (multi-provider)
├── config.py              # BiomniConfig dataclass
├── utils.py               # read_module2api, helpers
├── env_desc.py            # Data lake + library descriptions
├── tool/                  # Tool implementations by domain
│   ├── clinical_tools.py
│   ├── gene_tools.py
│   ├── protein_tools.py
│   └── ...
└── data/                  # Runtime data (data lake, benchmarks)
```

### Frontend (`~/scaleapi/packages/professional-agents/`)

```
src/
├── lib/agents.ts          # Agent registry (GTM, A1)
├── components/chat/
│   ├── chat-interface.tsx  # Main chat component
│   ├── message-bubble.tsx  # Message + trajectory rendering
│   ├── model-selector.tsx  # LLM model dropdown
│   └── tool-call-card.tsx  # Legacy tool call display
├── app/
│   ├── [agentType]/chat/   # Chat page per agent
│   └── api/[agentType]/chat/route.ts  # API proxy to Modal
```

### Deployment (`~/Code/professional-agents/`)

```
modal_biomni.py            # Modal deploy script (dev/prod)
Makefile                   # Deploy shortcuts
```

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `BIOMNI_LLM` | `claude-sonnet-4-5` | Default LLM model |
| `BIOMNI_SOURCE` | auto-detect | LLM provider |
| `BIOMNI_CUSTOM_BASE_URL` | — | LiteLLM proxy URL |
| `BIOMNI_CUSTOM_API_KEY` | — | API key for LLM proxy |
| `BIOMNI_REASONING_EFFORT` | — | Extended thinking level |
| `BIOMNI_DATA_PATH` | `./data` | Root data directory |
| `BIOMNI_COMMERCIAL_MODE` | `false` | Exclude non-commercial datasets |
| `BIOMNI_MODULES` | all | Comma-separated module filter |
| `MODAL_ENV` | `dev` | Modal environment (dev/prod) |
