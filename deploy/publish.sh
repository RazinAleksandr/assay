#!/usr/bin/env bash
# Users install Assay from the public repo RazinAleksandr/assay: the Claude Code plugin (its main), the GitHub Action
# (@v0) and the server to self-host. Every deploy mirrors this repository there: the files of HEAD, not the history,
# so both hold the same code and in-house history never goes public. Never edit that repo by hand.
#
#   deploy/publish.sh <git url> [tag]
set -euo pipefail
SRC=$(cd "$(dirname "$0")/.." && pwd)
url=${1:?usage: deploy/publish.sh <git url> [tag]} tag=${2:-v0}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
git clone --quiet "$url" "$work/repo"
cd "$work/repo"
git rm -rq --ignore-unmatch .                  # files removed here disappear from the public repo too
git -C "$SRC" archive HEAD | tar -xf -
rm -f .github/workflows/deploy.yml             # deploying the hosted service is this repository's job, not the mirror's
in_house="trading""view"                       # split so that this script does not match itself
if grep -rIil --exclude-dir=.git "$in_house" .; then
  echo "in-house content in the files above: nothing published" >&2
  exit 1
fi
git add -A
if git diff --cached --quiet; then
  echo "public repo is up to date"
else
  version=$(sed -n 's/^version *= *"\(.*\)"/\1/p' "$SRC/pyproject.toml" | head -1)
  git -c user.name="Assay release" -c user.email="assay-release@users.noreply.github.com" \
    commit --quiet -m "Assay $version ($(git -C "$SRC" rev-parse --short HEAD))"
  git push --quiet origin HEAD:main
fi
git tag -f "$tag" HEAD >/dev/null
git push --quiet -f origin "$tag"
echo "published $(git rev-parse --short HEAD) as main and $tag"
