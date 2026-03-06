# =============================================================================
# BiOMNI Docker Image — fast build (CI)
#
# Installs updated BiOMNI Python source on top of a pre-built base image.
# The base image already contains the conda env, bioinformatics tools, and
# system libs. CI reads BASE_IMAGE_TAG to determine the base.
#
# Usage:
#   docker build \
#     --build-arg BASE_TAG=$(cat packages/biomni/BASE_IMAGE_TAG) \
#     -t 307185671274.dkr.ecr.us-west-2.amazonaws.com/agent-environment:biomni-server-<SHA> \
#     packages/biomni/
#
# For full rebuilds (new conda env), see Dockerfile.full.
#
# Run:
#   docker run --rm -p 1984:1984 -e ANTHROPIC_API_KEY=sk-… <image>
#   docker run --rm -it <image> bash
# =============================================================================

ARG ECR_REGISTRY=307185671274.dkr.ecr.us-west-2.amazonaws.com
ARG BASE_TAG=biomni-1.0.6

FROM ${ECR_REGISTRY}/agent-environment:${BASE_TAG}

WORKDIR /biomni
COPY pyproject.toml MANIFEST.in README.md ./
COPY biomni/ ./biomni/
COPY biomni_env/ ./biomni_env/
RUN pip install --no-deps -e .

EXPOSE 1984
WORKDIR /workspace
CMD ["python", "-m", "biomni.server"]
