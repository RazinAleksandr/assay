#!/usr/bin/env bash
# Install or update the Assay server on this machine (Docker): copy the source into <dir>/app, build the image,
# replace the container, wait for health. Data (SQLite, analyses, admin token) stays in <dir>/data.
# Used by deploy.sh over SSH and by the CI deploy job on the self-hosted runner.
#
#   deploy/install.sh <source dir> <dir> <public-url>
set -euo pipefail
SRC=${1:?source dir}; DIR=${2:?install dir}; URL=${3:?public url}
PORT=${PORT:-8080}
mkdir -p "$DIR/data"
rm -rf "$DIR/app.next" && mkdir -p "$DIR/app.next"
(cd "$SRC" && tar --exclude=.git -cf - .) | tar -xf - -C "$DIR/app.next"
rm -rf "$DIR/app" && mv "$DIR/app.next" "$DIR/app"
docker build -q -t assay-server -f "$DIR/app/deploy/Dockerfile.server" "$DIR/app"
chown -R 10001 "$DIR/data"
docker rm -f assay-server >/dev/null 2>&1 || true
docker run -d --name assay-server --restart unless-stopped --memory 512m -p "$PORT:8080" -v "$DIR/data:/data" \
  assay-server --public-url "$URL" --config /data/server.yaml >/dev/null
for i in $(seq 1 30); do
  curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "http://127.0.0.1:$PORT/api/health" && echo
echo "assay server $(cd "$DIR/app" && cat .deployed-commit 2>/dev/null || true) live at $URL"
