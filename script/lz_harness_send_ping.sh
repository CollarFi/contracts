#!/usr/bin/env bash
set -euo pipefail

# Send a ping from one harness to the other and optionally wait for relay.
#
# Usage:
#   script/lz_harness_send_ping.sh --from l1|l2 --nonce <n> [--tag <hex32>] [--value <wei>] [--timeout <sec>] [l1_env] [l2_env]
#
# Notes:
# - Requires deployed harness JSON outputs from .env OUTPUT_JSON entries.
# - This script broadcasts a tx (real onchain send).

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
L1_ENV_FILE="$ROOT_DIR/.env.l1.testnet"
L2_ENV_FILE="$ROOT_DIR/.env.l2.testnet"

FROM=""
NONCE=""
TAG="0x0000000000000000000000000000000000000000000000000000000000000000"
VALUE_WEI="1000000000000000" # 0.001 native by default
TIMEOUT_SEC=180

while [[ $# -gt 0 ]]; do
  case "$1" in
    --from) FROM="$2"; shift 2 ;;
    --nonce) NONCE="$2"; shift 2 ;;
    --tag) TAG="$2"; shift 2 ;;
    --value) VALUE_WEI="$2"; shift 2 ;;
    --timeout) TIMEOUT_SEC="$2"; shift 2 ;;
    -h|--help)
      cat <<'EOF'
Usage:
  script/lz_harness_send_ping.sh --from l1|l2 --nonce <n> [--tag <hex32>] [--value <wei>] [--timeout <sec>] [l1_env] [l2_env]
EOF
      exit 0
      ;;
    *)
      if [[ "$L1_ENV_FILE" == "$ROOT_DIR/.env.l1.testnet" ]]; then
        L1_ENV_FILE="$1"
      elif [[ "$L2_ENV_FILE" == "$ROOT_DIR/.env.l2.testnet" ]]; then
        L2_ENV_FILE="$1"
      else
        echo "[error] unexpected argument: $1" >&2
        exit 1
      fi
      shift
      ;;
  esac
done

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "[error] missing required command: $1" >&2
    exit 1
  }
}
require_cmd cast
require_cmd python3

load_env_with_prefix() {
  local file="$1"; local prefix="$2"
  [[ -f "$file" ]] || { echo "[error] env file not found: $file" >&2; exit 1; }
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#${line%%[![:space:]]*}}"; line="${line%${line##*[![:space:]]}}"
    [[ -z "$line" || "$line" == \#* || "$line" != *=* ]] && continue
    local key="${line%%=*}" value="${line#*=}"
    key="${key#${key%%[![:space:]]*}}"; key="${key%${key##*[![:space:]]}}"
    value="${value#${value%%[![:space:]]*}}"; value="${value%${value##*[![:space:]]}}"
    if [[ "$value" == \"*\" && "$value" == *\" ]]; then value="${value:1:${#value}-2}"; fi
    if [[ "$value" == \'*\' ]]; then value="${value:1:${#value}-2}"; fi
    export "${prefix}_${key}=$value"
  done < "$file"
}

must_have() { [[ -n "${!1:-}" ]] || { echo "[error] missing required variable: $1" >&2; exit 1; }; }

json_get_harness() {
  python3 - "$1" <<'PY'
import json,sys
with open(sys.argv[1], 'r', encoding='utf-8') as f:
    data=json.load(f)
if isinstance(data, dict) and 'lzHarness' in data:
    print(data['lzHarness'])
elif isinstance(data, dict) and isinstance(data.get('addrs'), dict) and 'lzHarness' in data['addrs']:
    print(data['addrs']['lzHarness'])
else:
    raise KeyError("Could not find lzHarness in deployment json")
PY
}

resolve_harness() {
  local out_file="$1"
  [[ "$out_file" = /* ]] || out_file="$ROOT_DIR/$out_file"
  [[ -f "$out_file" ]] || { echo "[error] missing deployment output: $out_file" >&2; exit 1; }
  json_get_harness "$out_file"
}

is_hex32() {
  [[ "$1" =~ ^0x[0-9a-fA-F]{64}$ ]]
}

wait_for_nonce() {
  local dst_rpc="$1" dst_harness="$2" src_eid="$3" expected_nonce="$4" timeout="$5"
  local start now last
  start="$(date +%s)"
  while true; do
    last="$(cast call "$dst_harness" "lastNonceBySourceEid(uint32)(uint64)" "$src_eid" --rpc-url "$dst_rpc")"
    if [[ "$last" =~ ^[0-9]+$ ]] && (( last >= expected_nonce )); then
      echo "[ok] destination observed nonce $last (expected >= $expected_nonce)"
      return 0
    fi
    now="$(date +%s)"
    if (( now - start > timeout )); then
      echo "[error] timeout waiting for relay. last observed nonce=$last, expected >= $expected_nonce" >&2
      return 1
    fi
    sleep 4
  done
}

main() {
  [[ "$FROM" == "l1" || "$FROM" == "l2" ]] || { echo "[error] --from must be l1 or l2" >&2; exit 1; }
  [[ "$NONCE" =~ ^[0-9]+$ ]] || { echo "[error] --nonce must be uint" >&2; exit 1; }
  is_hex32 "$TAG" || { echo "[error] --tag must be bytes32 hex" >&2; exit 1; }

  load_env_with_prefix "$L1_ENV_FILE" "L1"
  load_env_with_prefix "$L2_ENV_FILE" "L2"

  must_have L1_RPC_URL; must_have L2_RPC_URL
  must_have L1_ACCOUNT; must_have L2_ACCOUNT
  must_have L1_OUTPUT_JSON; must_have L2_OUTPUT_JSON
  must_have L1_REMOTE_EID; must_have L2_REMOTE_EID

  local l1_harness l2_harness
  l1_harness="$(resolve_harness "$L1_OUTPUT_JSON")"
  l2_harness="$(resolve_harness "$L2_OUTPUT_JSON")"

  local src_rpc src_account src_harness dst_rpc dst_harness src_eid
  if [[ "$FROM" == "l1" ]]; then
    src_rpc="$L1_RPC_URL"; src_account="$L1_ACCOUNT"; src_harness="$l1_harness"
    dst_rpc="$L2_RPC_URL"; dst_harness="$l2_harness"; src_eid="$L2_REMOTE_EID" # L2's destination eid points back to L1
  else
    src_rpc="$L2_RPC_URL"; src_account="$L2_ACCOUNT"; src_harness="$l2_harness"
    dst_rpc="$L1_RPC_URL"; dst_harness="$l1_harness"; src_eid="$L1_REMOTE_EID" # L1's destination eid points to L2
  fi

  local src_default_options
  src_default_options="$(cast call "$src_harness" "defaultOptions()(bytes)" --rpc-url "$src_rpc")"
  if [[ -z "$src_default_options" || "$src_default_options" == "0x" ]]; then
    echo "[error] source harness defaultOptions is empty; LayerZero will revert (InvalidWorkerOptions)." >&2
    echo "[hint] set default options first (via deploy script --broadcast, or SetLZHarnessOptions.s.sol)." >&2
    exit 1
  fi

  echo "[info] sending ping from $FROM"
  echo "  src harness: $src_harness"
  echo "  dst harness: $dst_harness"
  echo "  nonce: $NONCE"
  echo "  tag: $TAG"
  echo "  defaultOptions: $src_default_options"

  cast send "$src_harness" \
    "sendPing(uint64,bytes32)" "$NONCE" "$TAG" \
    --value "$VALUE_WEI" \
    --rpc-url "$src_rpc" \
    --account "$src_account" >/dev/null

  echo "[info] ping tx sent. waiting for relay up to ${TIMEOUT_SEC}s..."
  wait_for_nonce "$dst_rpc" "$dst_harness" "$src_eid" "$NONCE" "$TIMEOUT_SEC"

  local last_guid
  last_guid="$(cast call "$dst_harness" "lastReceivedGuid()(bytes32)" --rpc-url "$dst_rpc")"
  echo "[info] destination lastReceivedGuid: $last_guid"
}

main "$@"
