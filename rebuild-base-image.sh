#!/bin/bash
# Rebuild the BiOMNI base Docker image end-to-end on a DevBox.
#
# This script automates the full rebuild process:
#   1. Create a DevBox (or connect to an existing one)
#   2. Clone the repo and run the conda env setup (~6-10 hours)
#   3. Pack tarballs and upload to S3
#   4. Build the Docker image and push to ECR
#   5. Update BASE_IMAGE_TAG
#
# Usage:
#   bash rebuild-base-image.sh start [--tag biomni-1.0.7]   # kick off the build
#   bash rebuild-base-image.sh status                        # check progress
#   bash rebuild-base-image.sh finish                        # build Docker image, push to ECR
#
# Prerequisites:
#   - `sai` CLI installed (for DevBox management)
#   - AWS credentials configured (for ECR push and S3 upload)
#   - SSH config has a "devbox" host entry (set up by `sai devbox bootstrap`)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ECR_REGISTRY="307185671274.dkr.ecr.us-west-2.amazonaws.com"
ECR_REPO="agent-environment"
S3_BUCKET="s3://scale-biomni-artifacts/docker-build"
REMOTE_REPO_DIR="\$HOME/scaleapi"
REMOTE_BIOMNI_DIR="\$HOME/scaleapi/packages/biomni"
TMUX_SESSION="biomni-build"

# Default tag: bump patch from current BASE_IMAGE_TAG
CURRENT_TAG=$(cat "$SCRIPT_DIR/BASE_IMAGE_TAG" 2>/dev/null | tr -d '[:space:]' || echo "biomni-1.0.6")
DEFAULT_NEW_TAG=$(echo "$CURRENT_TAG" | awk -F. '{$NF=$NF+1; print}' OFS=.)
NEW_TAG="$DEFAULT_NEW_TAG"

# Parse args
COMMAND="${1:-help}"
shift || true
for arg in "$@"; do
    case "$arg" in
        --tag) shift; NEW_TAG="${1:-$DEFAULT_NEW_TAG}" ;;
        --tag=*) NEW_TAG="${arg#--tag=}" ;;
    esac
done

usage() {
    cat <<'EOF'
Usage: bash rebuild-base-image.sh <command> [options]

Commands:
  setup-devbox         Install Docker, AWS CLI, and deps on a fresh DevBox
  start [--tag TAG]    Create DevBox and kick off conda env build
  status               Check build progress on the DevBox
  finish [--tag TAG]   Pack tarballs, upload to S3, build Docker image, push to ECR
  fast [--tag TAG]     Fast build: copy source onto base image and push (no conda rebuild)
  help                 Show this help

Options:
  --tag TAG            Base image tag (default: auto-incremented from BASE_IMAGE_TAG)

The build takes 6-10 hours. Run 'start', go do other things, check 'status'
periodically, and run 'finish' when the conda build is done.
EOF
}

setup_devbox() {
    local DEVBOX_HOST="${1:-devbox-biomni-build}"
    echo "=== Setting up DevBox ($DEVBOX_HOST) for Docker builds ==="

    if ! ssh -o ConnectTimeout=5 "$DEVBOX_HOST" true 2>/dev/null; then
        echo "ERROR: Cannot reach $DEVBOX_HOST."
        echo "  Create one first: sai devbox create --name biomni-build --size small --team gen_ai"
        exit 1
    fi

    ssh "$DEVBOX_HOST" bash <<'SETUP_SCRIPT'
set -euo pipefail
echo "--- Installing system packages ---"
sudo apt-get update -qq
sudo apt-get install -y -qq docker.io unzip > /dev/null

echo "--- Starting Docker ---"
sudo systemctl start docker
sudo usermod -aG docker $USER

echo "--- Installing AWS CLI ---"
if ! command -v aws &>/dev/null; then
    curl -sS "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
    cd /tmp && unzip -q -o awscliv2.zip && sudo ./aws/install
fi
aws --version

echo ""
echo "=== DevBox ready ==="
echo "NOTE: AWS SSO creds must be forwarded for ECR access."
echo "  From your laptop, run:"
echo "    aws configure export-credentials --profile production-developer --format env"
echo "  Then export those vars in your SSH session, or use:"
echo "    bash rebuild-base-image.sh fast --tag <TAG>"
SETUP_SCRIPT
}

fast_build() {
    echo "=== BiOMNI Fast Build ==="
    echo "  Tag: $NEW_TAG"
    echo "  Base: $CURRENT_TAG"
    echo ""

    # Check for a reachable devbox (try common names)
    local DEVBOX_HOST=""
    for host in osworld-evaluations devbox-biomni-build devbox; do
        if ssh -o ConnectTimeout=5 "$host" true 2>/dev/null; then
            DEVBOX_HOST="$host"
            break
        fi
    done

    if [ -z "$DEVBOX_HOST" ]; then
        echo "ERROR: No DevBox reachable. Create one first:"
        echo "  sai devbox create --name biomni-build --size small --team gen_ai"
        echo "  bash rebuild-base-image.sh setup-devbox"
        exit 1
    fi
    echo "Using DevBox: $DEVBOX_HOST"

    # Sync source to devbox
    echo "--- Syncing source ---"
    rsync -az --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' --exclude='data' \
        "$SCRIPT_DIR/" "$DEVBOX_HOST:~/biomni/"

    # Forward local AWS creds
    local AWS_CREDS
    AWS_CREDS=$(aws configure export-credentials --profile production-developer --format env 2>/dev/null || true)
    if [ -z "$AWS_CREDS" ]; then
        echo "ERROR: Could not export AWS credentials. Run 'aws sso login' first."
        exit 1
    fi

    # Build and push on devbox
    ssh "$DEVBOX_HOST" bash <<REMOTE_BUILD
set -euo pipefail
$AWS_CREDS
export AWS_DEFAULT_REGION=us-west-2

ECR_REGISTRY="$ECR_REGISTRY"

echo "--- ECR login ---"
aws ecr get-login-password --region us-west-2 | \\
    sudo docker login --username AWS --password-stdin \$ECR_REGISTRY

echo "--- Building ---"
cd ~/biomni
sudo docker build \\
    --build-arg BASE_TAG=$CURRENT_TAG \\
    -t \$ECR_REGISTRY/$ECR_REPO:$NEW_TAG \\
    .

echo "--- Pushing ---"
sudo docker push \$ECR_REGISTRY/$ECR_REPO:$NEW_TAG

echo ""
echo "=== Done ==="
echo "Image: \$ECR_REGISTRY/$ECR_REPO:$NEW_TAG"
REMOTE_BUILD

    echo ""
    echo "=== Fast build complete ==="
    echo "  Image: $ECR_REGISTRY/$ECR_REPO:$NEW_TAG"
}

start_build() {
    echo "=== BiOMNI Base Image Rebuild ==="
    echo "  New tag: $NEW_TAG"
    echo ""

    # Check if DevBox is reachable
    if ! ssh -o ConnectTimeout=5 devbox true 2>/dev/null; then
        echo "DevBox not reachable. Creating one..."
        echo "  Run: sai devbox create && sai devbox bootstrap"
        echo "  Then re-run this script."
        exit 1
    fi

    echo "DevBox is reachable. Setting up build..."

    # Upload the build script to the DevBox
    ssh devbox bash <<REMOTE_SCRIPT
set -euo pipefail

# Ensure repo is cloned and up to date
if [ ! -d $REMOTE_REPO_DIR ]; then
    echo "Cloning scaleapi repo..."
    git clone git@github.com:scaleapi/scaleapi.git $REMOTE_REPO_DIR
fi
cd $REMOTE_REPO_DIR
git fetch origin master
git checkout origin/master -- packages/biomni/

# Install Miniconda if needed
if ! command -v conda &> /dev/null; then
    echo "Installing Miniconda..."
    wget -q https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O /tmp/miniconda.sh
    bash /tmp/miniconda.sh -b -p \$HOME/miniconda3
    eval "\$(\$HOME/miniconda3/bin/conda shell.bash hook)"
    conda init bash
    source ~/.bashrc
fi

# Write the build script that tmux will run
cat > \$HOME/biomni-build-runner.sh <<'BUILDSCRIPT'
#!/bin/bash
set -euo pipefail
eval "\$(\$HOME/miniconda3/bin/conda shell.bash hook 2>/dev/null || true)"

cd $REMOTE_BIOMNI_DIR/biomni_env

echo "=== Starting BiOMNI conda env build at \$(date) ==="
echo "=== This will take 6-10 hours ==="

# Remove existing env if present to get a clean build
conda env remove -n biomni_e1 -y 2>/dev/null || true

NON_INTERACTIVE=1 bash setup.sh 2>&1 | tee \$HOME/biomni-setup.log

echo ""
echo "=== Conda env build completed at \$(date) ==="
echo "=== Run 'bash rebuild-base-image.sh finish' to continue ==="
touch \$HOME/biomni-build-done
BUILDSCRIPT
chmod +x \$HOME/biomni-build-runner.sh

# Remove any previous completion marker
rm -f \$HOME/biomni-build-done

# Start or restart tmux session
tmux kill-session -t $TMUX_SESSION 2>/dev/null || true
tmux new-session -d -s $TMUX_SESSION "bash \$HOME/biomni-build-runner.sh"

echo ""
echo "Build started in tmux session '$TMUX_SESSION'."
REMOTE_SCRIPT

    echo ""
    echo "=== Build kicked off ==="
    echo ""
    echo "The conda env is building on the DevBox. This takes 6-10 hours."
    echo ""
    echo "  Check progress:   bash rebuild-base-image.sh status"
    echo "  SSH and watch:    ssh devbox -t 'tmux attach -t $TMUX_SESSION'"
    echo "  When done:        bash rebuild-base-image.sh finish --tag $NEW_TAG"
}

check_status() {
    echo "=== Checking build status on DevBox ==="

    if ! ssh -o ConnectTimeout=5 devbox true 2>/dev/null; then
        echo "ERROR: DevBox not reachable."
        exit 1
    fi

    ssh devbox bash <<'REMOTE_SCRIPT'
if [ -f $HOME/biomni-build-done ]; then
    echo "STATUS: BUILD COMPLETE"
    echo ""
    echo "The conda env build finished successfully."
    echo "Run 'bash rebuild-base-image.sh finish' to build and push the Docker image."
elif tmux has-session -t biomni-build 2>/dev/null; then
    echo "STATUS: BUILDING"
    echo ""
    echo "Last 10 lines of build log:"
    tail -10 $HOME/biomni-setup.log 2>/dev/null || echo "(no log yet)"
    echo ""
    echo "To watch live: ssh devbox -t 'tmux attach -t biomni-build'"
else
    echo "STATUS: NOT RUNNING"
    echo ""
    echo "No build in progress. Run 'bash rebuild-base-image.sh start' to begin."
    if [ -f $HOME/biomni-setup.log ]; then
        echo ""
        echo "Last 5 lines of previous log:"
        tail -5 $HOME/biomni-setup.log
    fi
fi
REMOTE_SCRIPT
}

finish_build() {
    echo "=== BiOMNI Base Image: Finish ==="
    echo "  New tag: $NEW_TAG"
    echo ""

    if ! ssh -o ConnectTimeout=5 devbox true 2>/dev/null; then
        echo "ERROR: DevBox not reachable."
        exit 1
    fi

    # Check build is actually done
    if ! ssh devbox "test -f \$HOME/biomni-build-done" 2>/dev/null; then
        echo "ERROR: Conda build not complete yet."
        echo "  Run 'bash rebuild-base-image.sh status' to check progress."
        exit 1
    fi

    echo "Conda build is done. Packing, building Docker image, and pushing..."

    ssh devbox bash <<REMOTE_SCRIPT
set -euo pipefail
eval "\$(\$HOME/miniconda3/bin/conda shell.bash hook 2>/dev/null || true)"

cd $REMOTE_BIOMNI_DIR

# Update BASE_IMAGE_TAG for the new version
echo "$NEW_TAG" > BASE_IMAGE_TAG

# Pack tarballs and upload to S3
echo "=== Packing tarballs and uploading to S3 ==="
bash pack_for_docker.sh

# Build the full Docker image
echo ""
echo "=== Building Docker image ==="
docker build -f Dockerfile.full \
    --build-arg TARBALL_SOURCE=local \
    -t $ECR_REGISTRY/$ECR_REPO:$NEW_TAG .

# Test the image
echo ""
echo "=== Quick health check ==="
CONTAINER_ID=\$(docker run -d --rm -p 1984:1984 $ECR_REGISTRY/$ECR_REPO:$NEW_TAG)
sleep 10
if curl -sf http://localhost:1984/health | grep -q tools_loaded; then
    echo "Health check passed."
else
    echo "WARNING: Health check failed. Inspect the container before pushing."
fi
docker stop \$CONTAINER_ID 2>/dev/null || true

# Push to ECR
echo ""
echo "=== Pushing to ECR ==="
aws ecr get-login-password --region us-west-2 | \
    docker login --username AWS --password-stdin $ECR_REGISTRY

docker push $ECR_REGISTRY/$ECR_REPO:$NEW_TAG

echo ""
echo "=== Done ==="
echo "Image pushed: $ECR_REGISTRY/$ECR_REPO:$NEW_TAG"
REMOTE_SCRIPT

    # Update local BASE_IMAGE_TAG
    echo "$NEW_TAG" > "$SCRIPT_DIR/BASE_IMAGE_TAG"

    echo ""
    echo "=== All done ==="
    echo ""
    echo "  ECR image:  $ECR_REGISTRY/$ECR_REPO:$NEW_TAG"
    echo "  S3 tarballs: s3://scale-biomni-artifacts/docker-build/$NEW_TAG/"
    echo "  BASE_IMAGE_TAG updated to: $NEW_TAG"
    echo ""
    echo "  Next: commit and merge the BASE_IMAGE_TAG change so CI uses the new base."
    echo "    git add packages/biomni/BASE_IMAGE_TAG"
    echo "    git commit -m 'Bump base image to $NEW_TAG'"
}

case "$COMMAND" in
    setup-devbox) setup_devbox "${2:-}" ;;
    start) start_build ;;
    status) check_status ;;
    finish) finish_build ;;
    fast) fast_build ;;
    help|*) usage ;;
esac
