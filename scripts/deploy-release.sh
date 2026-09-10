#!/usr/bin/env bash
# Invoke the installed, root-owned copy of this helper after release CI passes.
set -euo pipefail
if [[ ${EUID} -ne 0 || $# -ne 2 ]]; then
  echo "Usage: sudo /opt/home-agent/current/scripts/deploy-release.sh vX.Y.Z MERGED_SHA" >&2
  exit 2
fi
tag=$1
expected=$2
if [[ ! "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ || ! "$expected" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Expected a stable version tag and full commit SHA" >&2
  exit 2
fi
repository=$(/opt/home-agent/current/venv/bin/python - <<'PY'
from home_agent.config import load_settings
print(load_settings().optimization_repository)
PY
)
install -d -o root -g root -m 0755 /opt/home-agent/staging
staging=$(mktemp -d /opt/home-agent/staging/release.XXXXXXXX)
# Keep Git's ownership checks intact: fetch the published release as root, rather than
# marking the service user's development checkout safe for root execution.
git clone --depth 1 --branch "$tag" --single-branch -- "$repository" "$staging"
actual=$(git -C "$staging" rev-parse HEAD)
if [[ "$actual" != "$expected" ]]; then
  echo "Published tag does not match the reviewed merge SHA; refusing deployment" >&2
  exit 1
fi
"$staging/scripts/update.sh"
echo "Deployed $tag at $actual; root-owned source retained at $staging"
