#!/usr/bin/env bash
# Deploy the Assay server to a Linux host with Docker, over SSH, from this checkout (tracked files only).
# Normally not needed: every green CI run on main deploys itself (.github/workflows/deploy.yml).
#
#   deploy/deploy.sh <ssh-host> <dir> <public-url>
#   deploy/deploy.sh grace /home/personal/qa-end-2-end http://46.225.104.43:8080
set -euo pipefail
HOST=${1:?ssh host}; DIR=${2:?remote directory}; URL=${3:?public url, the address CI and browsers use}
cd "$(git rev-parse --show-toplevel)"
STAGE="/tmp/assay-src-$$"
echo "→ shipping $(git ls-files | wc -l | tr -d ' ') tracked files to $HOST"
ssh "$HOST" "rm -rf '$STAGE' && mkdir -p '$STAGE'"
git ls-files -z | COPYFILE_DISABLE=1 xargs -0 tar --no-mac-metadata -cf - | ssh "$HOST" "tar -xf - -C '$STAGE'"
git rev-parse --short HEAD | ssh "$HOST" "cat > '$STAGE/.deployed-commit'"
ssh "$HOST" "PORT=${PORT:-8080} bash '$STAGE/deploy/install.sh' '$STAGE' '$DIR' '$URL'; rm -rf '$STAGE'"
