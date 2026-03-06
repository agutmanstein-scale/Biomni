# BiOMNI Docker Build and Deployment

## How It Works

All BiOMNI changes are Python source edits. CI handles everything automatically:

1. Edit code in `packages/biomni/`
2. Merge to master
3. CI copies your Python source onto the existing base image (~2 min), pushes to ECR, deploys to Modal

That's it. You never need to build a conda env or Docker image locally for normal development.

## Architecture

There are two Dockerfiles:

| File | When | What | Time |
|--------|------|------|------|
| `Dockerfile` | **Every CI build** | Copies updated Python source onto existing base image | ~2 min |
| `Dockerfile.full` | **Rare, manual** | Rebuilds conda env + CLI tools from scratch | 6-10 hours |

```
  Base image (biomni-1.0.6)          ← manually built, rarely changes
       │
       ▼
  Fast build (biomni-server-<SHA>)   ← CI layers Python source on top
       │
       ▼
  Modal deploy                       ← live endpoint auto-updates
```

The base image (`biomni-1.0.6`) contains the conda environment with ~200 Python/R packages, bioinformatics CLI tools (PLINK2, IQ-TREE, BWA, etc.), and system libraries. It was built once on an EC2 instance and pushed to ECR. The `BASE_IMAGE_TAG` file pins which base image CI uses.

## CI Pipeline

Defined in `.circleci/generate_config.py`. Triggers on master merges touching `packages/biomni/`.

| Job | What it does |
|-----|-------------|
| `build_biomni-server` | `docker build --target fast` using `BASE_IMAGE_TAG` as base → pushes `agent-environment:biomni-server-<SHA>` to ECR |
| `deploy_modal_biomni-server` | `BIOMNI_IMAGE_TAG=biomni-server-<SHA> modal deploy` → updates the live Modal endpoint |

### Required CircleCI Environment Variables

| Variable | Description |
|----------|-------------|
| `MODAL_TOKEN_ID` | Modal API token ID |
| `MODAL_TOKEN_SECRET` | Modal API token secret |

Set in CircleCI project settings, not in code.

## Running the Container

### Basic

```bash
docker run --rm -p 1984:1984 <image>
```

### With LLM Configuration

Many tools use an LLM internally (e.g., database query tools that translate natural language to API calls):

```bash
docker run --rm -p 1984:1984 \
  -e BIOMNI_SOURCE=Custom \
  -e BIOMNI_CUSTOM_BASE_URL=https://your-llm-proxy.com/v1 \
  -e BIOMNI_CUSTOM_API_KEY=sk-your-key \
  -e BIOMNI_LLM=claude-sonnet-4-5 \
  -e ANTHROPIC_API_KEY=sk-your-key \
  <image>
```

### Interactive Shell

```bash
docker run --rm -it <image> bash
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `BIOMNI_DATA_PATH` | Root data directory | `./data` |
| `BIOMNI_COMMERCIAL_MODE` | `"true"` for commercial-only datasets | `false` |
| `BIOMNI_MODULES` | Comma-separated module filter | all modules |
| `BIOMNI_HOST` | Server bind host | `0.0.0.0` |
| `BIOMNI_PORT` | Server bind port | `1984` |
| `BIOMNI_LLM` | LLM model name | `claude-sonnet-4-5` |
| `BIOMNI_SOURCE` | LLM provider (`Anthropic`, `OpenAI`, `Custom`, etc.) | auto-detect |
| `BIOMNI_CUSTOM_BASE_URL` | Base URL for custom LLM proxy | none |
| `BIOMNI_CUSTOM_API_KEY` | API key for custom LLM proxy | none |
| `ANTHROPIC_API_KEY` | Direct Anthropic API key (used by some tools internally) | none |

## API Endpoints

The server exposes MCP-compatible endpoints at `http://localhost:1984`:

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check with tool count |
| `POST` | `/list-tools` | List all tools in MCP format |
| `POST` | `/call-tool` | Call a tool: `{"tool_name": "...", "tool_args": {...}}` |
| `POST` | `/step` | OpenEnv-compatible step |
| `POST` | `/reset` | OpenEnv-compatible reset |
| `GET` | `/state` | Current episode state |
| `GET` | `/tools` | Browse tools (optional `?module=` filter) |
| `GET` | `/tools/{name}` | Single tool schema |
| `GET` | `/modules` | List modules with tool counts |

## Running Tests

```bash
# Full test (calls all 195+ tools against the running server)
python test_tools.py --timeout 60 --workers 4 -v

# Registration check only (no tool calls)
python test_tools.py --skip-call

# Test specific tools
python test_tools.py --tools query_uniprot,blast_sequence,align_sequences
```

## Files

| File | Purpose |
|------|---------|
| `Dockerfile` | Fast CI build (layers Python source on base image) |
| `Dockerfile.full` | Full manual rebuild (conda env + CLI tools from ubuntu:22.04) |
| `BASE_IMAGE_TAG` | Current base image tag for fast builds (e.g. `biomni-1.0.6`) |
| `biomni/server.py` | FastAPI HTTP server |
| `test_tools.py` | Tool health check suite |
| `test_tool_args.json` | Realistic test arguments for each tool |
| `.circleci/generate_config.py` | CI pipeline: build + modal deploy jobs |
| `.circleci/paths` | CI trigger paths |

---

## Appendix: Rebuilding the Base Image

You only need this if changing the **conda environment itself** — adding/upgrading packages in `biomni_env/*.yml`, R packages, or CLI tools. This is rare and not part of normal development.

### When to rebuild

- Adding a package to `biomni_env/environment.yml` or `bio_env.yml`
- Upgrading R or adding R packages (`r_packages.yml`, `install_r_packages.R`)
- Adding a CLI tool (`install_cli_tools.sh`)
- Changing system library dependencies

### Automated rebuild (recommended)

The `rebuild-base-image.sh` script handles the entire process: provisions a DevBox, runs the 6-10 hour conda build in tmux, packs tarballs, uploads to S3, builds the Docker image, and pushes to ECR.

```bash
cd packages/biomni

# 1. Kick off the build (creates DevBox, starts conda setup in tmux)
bash rebuild-base-image.sh start --tag biomni-1.0.7

# 2. Go do other things. Check progress whenever:
bash rebuild-base-image.sh status

# 3. Or SSH in to watch live:
ssh devbox -t 'tmux attach -t biomni-build'

# 4. When status says "BUILD COMPLETE", finish the process:
#    (packs tarballs → uploads to S3 → builds Docker image → pushes to ECR)
bash rebuild-base-image.sh finish --tag biomni-1.0.7

# 5. Commit the updated BASE_IMAGE_TAG
git add packages/biomni/BASE_IMAGE_TAG
git commit -m "Bump base image to biomni-1.0.7"
```

After merging, all CI fast builds layer on the new base.

### Where artifacts are stored

| Artifact | Location | Purpose |
|----------|----------|---------|
| Base Docker image | ECR `agent-environment:biomni-X.Y.Z` | Used by fast builds (`FROM` in Dockerfile) |
| Conda env tarball | `s3://scale-biomni-artifacts/docker-build/<tag>/biomni_e1.tar.gz` | Backup; can rebuild base image from any machine |
| CLI tools tarball | `s3://scale-biomni-artifacts/docker-build/<tag>/biomni_tools.tar.gz` | Backup; bundled into base image |

The S3 tarballs mean you can rebuild the base image from scratch on any machine without re-running the 6-10 hour conda build:

```bash
docker build -f Dockerfile.full --build-arg TARBALL_TAG=biomni-1.0.7 -t biomni:local .
```

This downloads the tarballs from S3 during `docker build`. To use local tarballs instead (e.g. on the build host), add `--build-arg TARBALL_SOURCE=local`.

### What `setup.sh` installs

| Step | File | What | Time |
|------|------|------|------|
| 1 | `environment.yml` | Python 3.11, numpy, pandas, scikit-learn, langchain, etc. | ~30 min |
| 2 | (activate) | Activates `biomni_e1` | instant |
| 3 | `bio_env.yml` | BioPython, scanpy, QIIME2, PyMOL, RDKit, etc. | ~2-4 hours |
| 4 | `r_packages.yml` | R base + common packages via conda | ~1-2 hours |
| 5 | `install_r_packages.R` | Additional R packages | ~1-2 hours |
| 6 | `install_cli_tools.sh` | PLINK2, IQ-TREE, GCTA, BWA, FastTree, MUSCLE, HOMER | ~30 min |

### Manual rebuild (alternative)

If you prefer to run each step yourself instead of using the script:

```bash
# On any Linux x86_64 host with the conda env already built:
cd packages/biomni

# Pack and upload to S3
bash pack_for_docker.sh          # packs tarballs + uploads to S3

# Build Docker image (uses local tarballs)
docker build -f Dockerfile.full --build-arg TARBALL_SOURCE=local -t biomni:local .

# Push to ECR
aws ecr get-login-password --region us-west-2 | \
  docker login --username AWS --password-stdin 307185671274.dkr.ecr.us-west-2.amazonaws.com
docker tag biomni:local 307185671274.dkr.ecr.us-west-2.amazonaws.com/agent-environment:biomni-1.0.7
docker push 307185671274.dkr.ecr.us-west-2.amazonaws.com/agent-environment:biomni-1.0.7

# Update BASE_IMAGE_TAG
echo "biomni-1.0.7" > BASE_IMAGE_TAG
```
