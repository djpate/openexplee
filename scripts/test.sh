#!/usr/bin/env bash
# Run the test suite in Docker. Offline by default; OPENEXPLEE_LIVE=1 enables the network.
set -euo pipefail
cd "$(dirname "$0")/.."
docker build -q -f tests/Dockerfile -t openexplee:test . >/dev/null
net=(--network none)
if [ "${OPENEXPLEE_LIVE:-}" = "1" ]; then net=(); fi
exec docker run --rm "${net[@]}" \
  -e OPENEXPLEE_LIVE -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$PWD":/src -v /dev/null:/src/.env:ro -w /src \
  openexplee:test python -m pytest -q -p no:cacheprovider "$@"
