#!/usr/bin/env bash
set -euo pipefail

# End-to-end deploy of LZ harness contracts on L1 + L2 and peer wiring.
#
# Usage:
#   script/deploy_lz_harness_e2e.sh [--broadcast] [.env.l1.testnet] [.env.l2.testnet]
#
# Defaults:
#   .env.l1.testnet and .env.l2.testnet in repo root.
#
# By default runs a dry-run (no onchain tx). Pass --broadcast to actually send txs.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
L1_ENV_FILE="$ROOT_DIR/.env.l1.testnet"
L2_ENV_FILE="$ROOT_DIR/.env.l2.testnet"
BROADCAST=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --broadcast)
      BROADCAST=1
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Usage:
  script/deploy_lz_harness_e2e.sh [--broadcast] [l1_env_file] [l2_env_file]

Options:
  --broadcast   Execute onchain txs (default is dry-run)
  -h, --help    Show this help
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

require_cmd forge
require_cmd cast
require_cmd python3

load_env_with_prefix() {
  local file="$1"
  local prefix="$2"

  if [[ ! -f "$file" ]]; then
    echo "[error] env file not found: $file" >&2
    exit 1
  fi

  while IFS= read -r line || [[ -n "$line" ]]; do
    # trim leading/trailing whitespace
    line="${line#${line%%[![:space:]]*}}"
    line="${line%${line##*[![:space:]]}}"

    [[ -z "$line" || "$line" == \#* ]] && continue
    [[ "$line" != *=* ]] && continue

    local key="${line%%=*}"
    local value="${line#*=}"

    key="${key#${key%%[![:space:]]*}}"
    key="${key%${key##*[![:space:]]}}"
    value="${value#${value%%[![:space:]]*}}"
    value="${value%${value##*[![:space:]]}}"

    # strip optional quotes
    if [[ "$value" == \"*\" && "$value" == *\" ]]; then
      value="${value:1:${#value}-2}"
    elif [[ "$value" == \'*\' ]]; then
      value="${value:1:${#value}-2}"
    fi

    export "${prefix}_${key}=$value"
  done < "$file"
}

must_have() {
  local var="$1"
  if [[ -z "${!var:-}" ]]; then
    echo "[error] missing required variable: $var" >&2
    exit 1
  fi
}

json_get_harness() {
  local json_file="$1"
  python3 - "$json_file" <<'PY'
import json,sys
with open(sys.argv[1], 'r', encoding='utf-8') as f:
    data=json.load(f)
print(data['addrs']['lzHarness'])
PY
}

resolve_account_address() {
  local account_name="$1"
  cast wallet address --account "$account_name"
}

deploy_one_side() {
  local side="$1" # L1 or L2
  local rpc_url_var="${side}_RPC_URL"
  local account_var="${side}_ACCOUNT"
  local out_var="${side}_OUTPUT_JSON"
  local admin_var="${side}_ADMIN"
  local remote_eid_var="${side}_REMOTE_EID"
  local endpoint_var="${side}_LZ_ENDPOINT"

  must_have "$rpc_url_var"
  must_have "$account_var"
  must_have "$out_var"
  must_have "$remote_eid_var"

  local admin_address="${!admin_var:-}"
  if [[ -z "$admin_address" ]]; then
    admin_address="$(resolve_account_address "${!account_var}")"
  fi

  local output_json="${!out_var}"
  [[ "$output_json" = /* ]] || output_json="$ROOT_DIR/$output_json"
  mkdir -p "$(dirname "$output_json")"

  echo "[info] deploying $side harness..."
  echo "[info] $side admin: $admin_address"

  (
    cd "$ROOT_DIR"
    export ADMIN="$admin_address"
    export REMOTE_EID="${!remote_eid_var}"
    export OUTPUT_JSON="$output_json"
    if [[ -n "${!endpoint_var:-}" ]]; then
      export LZ_ENDPOINT="${!endpoint_var}"
    else
      unset LZ_ENDPOINT || true
    fi

    if [[ "$BROADCAST" -eq 1 ]]; then
      forge script script/DeployLZHarness.s.sol:DeployLZHarness \
        --rpc-url "${!rpc_url_var}" \
        --account "${!account_var}" \
        --broadcast
    else
      forge script script/DeployLZHarness.s.sol:DeployLZHarness \
        --rpc-url "${!rpc_url_var}" \
        --account "${!account_var}"
    fi
  )

  if [[ ! -f "$output_json" ]]; then
    echo "[error] expected output json missing: $output_json" >&2
    exit 1
  fi

  local harness
  harness="$(json_get_harness "$output_json")"
  echo "[info] $side harness deployed at: $harness"
  export "${side}_HARNESS=$harness"
}

wire_peers() {
  must_have L1_HARNESS
  must_have L2_HARNESS
  must_have L1_RPC_URL
  must_have L2_RPC_URL
  must_have L1_ACCOUNT
  must_have L2_ACCOUNT
  must_have L1_REMOTE_EID
  must_have L2_REMOTE_EID

  local l2_peer_b32
  local l1_peer_b32
  l2_peer_b32="$(cast --to-bytes32 "$L2_HARNESS")"
  l1_peer_b32="$(cast --to-bytes32 "$L1_HARNESS")"

  if [[ "$BROADCAST" -eq 1 ]]; then
    echo "[info] wiring L1 peer -> L2"
    cast send "$L1_HARNESS" \
      "setPeer(uint32,bytes32)" "$L1_REMOTE_EID" "$l2_peer_b32" \
      --rpc-url "$L1_RPC_URL" \
      --account "$L1_ACCOUNT" >/dev/null

    echo "[info] wiring L2 peer -> L1"
    cast send "$L2_HARNESS" \
      "setPeer(uint32,bytes32)" "$L2_REMOTE_EID" "$l1_peer_b32" \
      --rpc-url "$L2_RPC_URL" \
      --account "$L2_ACCOUNT" >/dev/null
  else
    echo "[dry-run] skipping onchain setPeer txs"
    echo "[dry-run] would call on L1: setPeer($L1_REMOTE_EID, $l2_peer_b32)"
    echo "[dry-run] would call on L2: setPeer($L2_REMOTE_EID, $l1_peer_b32)"
  fi
}

main() {
  echo "[info] mode: $([[ "$BROADCAST" -eq 1 ]] && echo "broadcast" || echo "dry-run")"
  echo "[info] using env files:"
  echo "  L1: $L1_ENV_FILE"
  echo "  L2: $L2_ENV_FILE"

  load_env_with_prefix "$L1_ENV_FILE" "L1"
  load_env_with_prefix "$L2_ENV_FILE" "L2"

  deploy_one_side "L1"
  deploy_one_side "L2"
  wire_peers

  cat <<EOF

[done] LZ harness flow completed.

L1 harness: $L1_HARNESS
L2 harness: $L2_HARNESS

Outputs:
- ${L1_OUTPUT_JSON}
- ${L2_OUTPUT_JSON}

Next checks (example):
- cast call "$L1_HARNESS" "remoteEid()(uint32)" --rpc-url "$L1_RPC_URL"
- cast call "$L2_HARNESS" "remoteEid()(uint32)" --rpc-url "$L2_RPC_URL"
EOF
}

main "$@"
