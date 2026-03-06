#!/bin/bash
# Pack the working conda environment and CLI tools into tarballs, then upload to S3.
#
# Usage:
#   bash pack_for_docker.sh [ENV_NAME] [--no-upload]
#
# Produces locally:
#   biomni_e1.tar.gz     — relocatable conda environment (~5-15 GB)
#   biomni_tools.tar.gz  — CLI bioinformatics tools (PLINK2, IQ-TREE, etc.)
#
# Uploads to (unless --no-upload):
#   s3://biomni-release/docker-build/<TAG>/biomni_e1.tar.gz
#   s3://biomni-release/docker-build/<TAG>/biomni_tools.tar.gz

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_NAME="biomni_e1"
TOOLS_SRC="$SCRIPT_DIR/biomni_env/biomni_tools"
OUT_DIR="$SCRIPT_DIR"
S3_BUCKET="s3://biomni-release/docker-build"
NO_UPLOAD=false

for arg in "$@"; do
    case "$arg" in
        --no-upload) NO_UPLOAD=true ;;
        -*) ;; # ignore other flags
        *) ENV_NAME="$arg" ;;
    esac
done

if [ -f "$SCRIPT_DIR/BASE_IMAGE_TAG" ]; then
    BASE_TAG=$(cat "$SCRIPT_DIR/BASE_IMAGE_TAG" | tr -d '[:space:]')
else
    BASE_TAG="unknown"
fi

echo "=== Biomni Docker Pack ==="
echo "  Environment: $ENV_NAME"
echo "  Base tag:    $BASE_TAG"

# -----------------------------------------------------------------------
# 1. conda-pack the environment
# -----------------------------------------------------------------------
if ! conda list -n base conda-pack 2>/dev/null | grep -q conda-pack; then
    echo "Installing conda-pack..."
    conda install -n base -c conda-forge conda-pack -y
fi

CONDA_PREFIX="$(conda info --base)"
CONDA_PACK="$CONDA_PREFIX/bin/conda-pack"
if [ ! -x "$CONDA_PACK" ]; then
    echo "ERROR: conda-pack binary not found at $CONDA_PACK"
    exit 1
fi

echo "Packing conda environment '$ENV_NAME' (this takes a few minutes)..."
"$CONDA_PACK" \
    -n "$ENV_NAME" \
    -o "$OUT_DIR/biomni_e1.tar.gz" \
    --ignore-editable-packages \
    --force

echo "  -> biomni_e1.tar.gz ($(du -h "$OUT_DIR/biomni_e1.tar.gz" | cut -f1))"

# -----------------------------------------------------------------------
# 2. Pack CLI tools
# -----------------------------------------------------------------------
if [ -d "$TOOLS_SRC" ]; then
    echo "Packing CLI tools from $TOOLS_SRC ..."
    tar -czf "$OUT_DIR/biomni_tools.tar.gz" -C "$(dirname "$TOOLS_SRC")" "$(basename "$TOOLS_SRC")"
    echo "  -> biomni_tools.tar.gz ($(du -h "$OUT_DIR/biomni_tools.tar.gz" | cut -f1))"
else
    echo "WARNING: CLI tools directory not found at $TOOLS_SRC"
    echo "  Creating empty placeholder so Docker build doesn't fail."
    mkdir -p /tmp/_biomni_tools_empty/biomni_tools/bin
    tar -czf "$OUT_DIR/biomni_tools.tar.gz" -C /tmp/_biomni_tools_empty biomni_tools
    rm -rf /tmp/_biomni_tools_empty
fi

# -----------------------------------------------------------------------
# 3. Upload to S3
# -----------------------------------------------------------------------
if [ "$NO_UPLOAD" = true ]; then
    echo ""
    echo "Skipping S3 upload (--no-upload)."
else
    S3_PATH="$S3_BUCKET/$BASE_TAG"
    echo ""
    echo "Uploading tarballs to $S3_PATH/ ..."
    aws s3 cp "$OUT_DIR/biomni_e1.tar.gz" "$S3_PATH/biomni_e1.tar.gz"
    aws s3 cp "$OUT_DIR/biomni_tools.tar.gz" "$S3_PATH/biomni_tools.tar.gz"
    echo "Upload complete."
fi

echo ""
echo "=== Done ==="
echo "Tarballs ready at: $OUT_DIR"
echo ""
echo "Next steps:"
echo "  docker build -f Dockerfile.full -t biomni .                                     # no data"
echo "  docker build -f Dockerfile.full --build-arg BAKE_DATA=commercial -t biomni .    # + commercial data (~7.9 GB)"
echo "  docker build -f Dockerfile.full --build-arg BAKE_DATA=all -t biomni .           # + all data (~14 GB)"
