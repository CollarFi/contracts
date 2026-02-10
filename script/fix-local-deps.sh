#!/usr/bin/env bash
set -euo pipefail

# This repo vendors Derive's v2-matching as a git submodule.
# v2-matching itself contains TWO copies of lyra-utils:
#   - lib/v2-matching/lib/lyra-utils
#   - lib/v2-matching/lib/v2-core/lib/lyra-utils
# If both are present, Foundry will often hit duplicate identifier errors.
# CI prunes the duplicate; this script makes local dev match CI.

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT_DIR"

DUP="$ROOT_DIR/lib/v2-matching/lib/lyra-utils"
CANON="$ROOT_DIR/lib/v2-matching/lib/v2-core/lib/lyra-utils"

if [[ -d "$DUP" ]]; then
  echo "[fix-local-deps] Removing duplicate lyra-utils at: $DUP"
  rm -rf "$DUP"
fi

if [[ ! -d "$CANON" ]]; then
  echo "[fix-local-deps] ERROR: canonical lyra-utils not found at: $CANON" >&2
  echo "[fix-local-deps] Did you run: git submodule update --init --recursive ?" >&2
  exit 1
fi

# Normalize imports inside v2-core so they consistently use the remapping
#   lyra-utils/=.../v2-core/lib/lyra-utils/src/
# instead of importing via "src/...".
# Idempotent: running multiple times is safe.

echo "[fix-local-deps] Rewriting v2-core lyra-utils imports (src/ -> lyra-utils/)"
# shellcheck disable=SC2016
find "$CANON" -name '*.sol' -print0 | xargs -0 sed -i 's/\("\)src\//\1lyra-utils\//g'

echo "[fix-local-deps] Done. You can now run: forge build / forge test"
