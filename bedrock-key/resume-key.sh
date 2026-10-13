#!/usr/bin/env bash
# Compatibility wrapper. Prefer: uv run bedrock-key/admin.py resume
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec uv run --script "${HERE}/admin.py" resume "$@"
