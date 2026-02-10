#!/usr/bin/env bash
set -euo pipefail

# Show current LZ harness wiring + receive state on both chains.
#
# Usage:
#   script/lz_harness_status.sh [l1_env_file] [l2_env_file]

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
L1_ENV_FILE="${1:-$ROOT_DIR/.env.l1.testnet}"
L2_ENV_FILE="${2:-$ROOT_DIR/.env.l2.testnet}"

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "[error] missing required command: $1" >&2
    exit 1
  }
}

require_cmd cast
require_cmd python3

load_env_with_prefix() {
  local file="$1"
  local prefix="$2"
  [[ -f "$file" ]] || { echo "[error] env file not found: $file" >&2; exit 1; }

  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#${line%%[![:space:]]*}}"
    line="${line%${line##*[![:space:]]}}"
    [[ -z "$line" || "$line" == \#* || "$line" != *=* ]] && continue
    local key="${line%%=*}"
    local value="${line#*=}"
    key="${key#${key%%[![:space:]]*}}"; key="${key%${key##*[![:space:]]}}"
    value="${value#${value%%[![:space:]]*}}"; value="${value%${value##*[![:space:]]}}"
    if [[ "$value" == \"*\" && "$value" == *\" ]]; then value="${value:1:${#value}-2}"; fi
    if [[ "$value" == \'*\' ]]; then value="${value:1:${#value}-2}"; fi
    export "${prefix}_${key}=$value"
  done < "$file"
}

must_have() {
  local var="$1"
  [[ -n "${!var:-}" ]] || { echo "[error] missing required variable: $var" >&2; exit 1; }
}

json_get_harness() {
  local json_file="$1"
  python3 - "$json_file" <<'PY'
import json,sys
with open(sys.argv[1], 'r', encoding='utf-8') as f:
    print(json.load(f)['addrs']['lzHarness'])
PY
}

resolve_harness() {
  local prefix="$1"
  local out_var="${prefix}_OUTPUT_JSON"
  must_have "$out_var"
  local out_path="${!out_var}"
  [[ "$out_path" = /* ]] || out_path="$ROOT_DIR/$out_path"
  [[ -f "$out_path" ]] || { echo "[error] missing deployment output: $out_path" >&2; exit 1; }
  json_get_harness "$out_path"
}

print_side() {
  local label="$1"
  local rpc_var="$2"
  local remote_eid_var="$3"
  local src_eid_var="$4"
  local harness="$5"
  local peer_remote="$6"

  local rpc="${!rpc_var}"
  local remote_eid="${!remote_eid_var}"
  local src_eid="${!src_eid_var}"

  local configured_remote
  configured_remote="$(cast call "$harness" "remoteEid()(uint32)" --rpc-url "$rpc")"
  local peer
  peer="$(cast call "$harness" "peers(uint32)(bytes32)" "$remote_eid" --rpc-url "$rpc")"
  local last_guid
  last_guid="$(cast call "$harness" "lastReceivedGuid()(bytes32)" --rpc-url "$rpc")"
  local last_nonce
  last_nonce="$(cast call "$harness" "lastNonceBySourceEid(uint32)(uint64)" "$src_eid" --rpc-url "$rpc")"

  cat <<EOF
[$label]
  harness:                $harness
  remoteEid(config):      $configured_remote
  expected remoteEid:     $remote_eid
  peer(remoteEid):        $peer
  expected peer(bytes32): $peer_remote
  lastReceivedGuid:       $last_guid
  lastNonce(srcEid=$src_eid): $last_nonce
EOF
}

main() {
  load_env_with_prefix "$L1_ENV_FILE" "L1"
  load_env_with_prefix "$L2_ENV_FILE" "L2"

  must_have L1_RPC_URL
  must_have L2_RPC_URL
  must_have L1_REMOTE_EID
  must_have L2_REMOTE_EID

  local l1_harness l2_harness
  l1_harness="$(resolve_harness L1)"
  l2_harness="$(resolve_harness L2)"

  local l1_peer_b32 l2_peer_b32
  l1_peer_b32="$(cast --to-bytes32 "$l1_harness")"
  l2_peer_b32="$(cast --to-bytes32 "$l2_harness")"

  print_side "L1" "L1_RPC_URL" "L1_REMOTE_EID" "L2_REMOTE_EID" "$l1_harness" "$l2_peer_b32"
  echo
  print_side "L2" "L2_RPC_URL" "L2_REMOTE_EID" "L1_REMOTE_EID" "$l2_harness" "$l1_peer_b32"
}

main "$@"
