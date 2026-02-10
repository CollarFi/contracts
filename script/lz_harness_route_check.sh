#!/usr/bin/env bash
set -euo pipefail

# Inspect LayerZero route config for LZ harness on both chains.
#
# Usage:
#   script/lz_harness_route_check.sh [l1_env_file] [l2_env_file]
#
# It prints, for each side:
# - endpoint delegate for OApp
# - send/receive library selection
# - receive library timeout
# - getConfig blobs for config types 1/2 (executor/uln in ULN302 setups)
# - endpoint initializable/verifiable checks for expected origin

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
  [[ -n "${!var:-}" ]] || { echo "[error] missing required variable: $var" >&2; exit 1; }
}

json_get_harness() {
  local json_file="$1"
  python3 - "$json_file" <<'PY'
import json,sys
with open(sys.argv[1], 'r', encoding='utf-8') as f:
    data=json.load(f)
if isinstance(data, dict) and 'lzHarness' in data:
    print(data['lzHarness'])
elif isinstance(data, dict) and isinstance(data.get('addrs'), dict) and 'lzHarness' in data['addrs']:
    print(data['addrs']['lzHarness'])
else:
    raise KeyError('Could not find lzHarness in deployment json')
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

address_to_peer_bytes32() {
  local addr="$1"
  cast abi-encode "f(address)" "$addr"
}

call_or_na() {
  local rpc="$1"
  local to="$2"
  local sig="$3"
  shift 3

  if out="$(cast call "$to" "$sig" "$@" --rpc-url "$rpc" 2>/dev/null)"; then
    printf "%s" "$out"
  else
    printf "N/A"
  fi
}

print_side() {
  local label="$1"
  local rpc="$2"
  local endpoint="$3"
  local oapp="$4"
  local remote_eid="$5"
  local src_eid="$6"
  local src_sender_b32="$7"
  local nonce="$8"

  local delegate send_lib send_lib_default receive_lib receive_timeout
  local send_cfg1 send_cfg2 recv_cfg1 recv_cfg2 initz verif initz_next verif_next

  delegate="$(call_or_na "$rpc" "$endpoint" "delegates(address)(address)" "$oapp")"
  send_lib="$(call_or_na "$rpc" "$endpoint" "getSendLibrary(address,uint32)(address)" "$oapp" "$remote_eid")"
  send_lib_default="$(call_or_na "$rpc" "$endpoint" "isDefaultSendLibrary(address,uint32)(bool)" "$oapp" "$remote_eid")"

  receive_lib="$(call_or_na "$rpc" "$endpoint" "getReceiveLibrary(address,uint32)(address,bool)" "$oapp" "$src_eid")"
  receive_timeout="$(call_or_na "$rpc" "$endpoint" "receiveLibraryTimeout(address,uint32)(address,uint256)" "$oapp" "$src_eid")"

  local recv_lib_addr="N/A"
  if [[ "$receive_lib" != "N/A" ]]; then
    recv_lib_addr="$(printf "%s" "$receive_lib" | sed -n '1p')"
  fi

  send_cfg1="$(call_or_na "$rpc" "$endpoint" "getConfig(address,address,uint32,uint32)(bytes)" "$oapp" "$send_lib" "$remote_eid" "1")"
  send_cfg2="$(call_or_na "$rpc" "$endpoint" "getConfig(address,address,uint32,uint32)(bytes)" "$oapp" "$send_lib" "$remote_eid" "2")"

  if [[ "$recv_lib_addr" != "N/A" && "$recv_lib_addr" != "0x0000000000000000000000000000000000000000" ]]; then
    recv_cfg1="$(call_or_na "$rpc" "$endpoint" "getConfig(address,address,uint32,uint32)(bytes)" "$oapp" "$recv_lib_addr" "$src_eid" "1")"
    recv_cfg2="$(call_or_na "$rpc" "$endpoint" "getConfig(address,address,uint32,uint32)(bytes)" "$oapp" "$recv_lib_addr" "$src_eid" "2")"
  else
    recv_cfg1="N/A"
    recv_cfg2="N/A"
  fi

  initz="$(call_or_na "$rpc" "$endpoint" "initializable((uint32,bytes32,uint64),address)(bool)" "($src_eid,$src_sender_b32,$nonce)" "$oapp")"
  verif="$(call_or_na "$rpc" "$endpoint" "verifiable((uint32,bytes32,uint64),address)(bool)" "($src_eid,$src_sender_b32,$nonce)" "$oapp")"

  local next_nonce="$nonce"
  if [[ "$nonce" =~ ^[0-9]+$ ]]; then
    next_nonce=$((nonce + 1))
  fi
  initz_next="$(call_or_na "$rpc" "$endpoint" "initializable((uint32,bytes32,uint64),address)(bool)" "($src_eid,$src_sender_b32,$next_nonce)" "$oapp")"
  verif_next="$(call_or_na "$rpc" "$endpoint" "verifiable((uint32,bytes32,uint64),address)(bool)" "($src_eid,$src_sender_b32,$next_nonce)" "$oapp")"

  cat <<EOF
[$label]
  rpc:                      $rpc
  endpoint:                 $endpoint
  oapp:                     $oapp
  delegate(oapp):           $delegate

  sendLib(dstEid=$remote_eid):           $send_lib
  isDefaultSendLib:                      $send_lib_default
  send.getConfig(type=1):                $send_cfg1
  send.getConfig(type=2):                $send_cfg2

  receiveLib(srcEid=$src_eid):           $receive_lib
  receiveLibraryTimeout(srcEid=$src_eid): $receive_timeout
  recv.getConfig(type=1):                $recv_cfg1
  recv.getConfig(type=2):                $recv_cfg2

  initializable(origin{$src_eid,$src_sender_b32,$nonce}): $initz
  verifiable(origin{$src_eid,$src_sender_b32,$nonce}):    $verif
  initializable(origin{$src_eid,$src_sender_b32,$next_nonce}): $initz_next
  verifiable(origin{$src_eid,$src_sender_b32,$next_nonce}):    $verif_next
EOF
}

main() {
  load_env_with_prefix "$L1_ENV_FILE" "L1"
  load_env_with_prefix "$L2_ENV_FILE" "L2"

  must_have L1_RPC_URL
  must_have L2_RPC_URL
  must_have L1_LZ_ENDPOINT
  must_have L2_LZ_ENDPOINT
  must_have L1_REMOTE_EID
  must_have L2_REMOTE_EID

  local l1_harness l2_harness
  l1_harness="$(resolve_harness L1)"
  l2_harness="$(resolve_harness L2)"

  local l1_sender_b32 l2_sender_b32
  l1_sender_b32="$(address_to_peer_bytes32 "$l1_harness")"
  l2_sender_b32="$(address_to_peer_bytes32 "$l2_harness")"

  local l1_nonce l2_nonce
  l1_nonce="$(call_or_na "$L1_RPC_URL" "$l1_harness" "lastNonceBySourceEid(uint32)(uint64)" "$L2_REMOTE_EID")"
  l2_nonce="$(call_or_na "$L2_RPC_URL" "$l2_harness" "lastNonceBySourceEid(uint32)(uint64)" "$L1_REMOTE_EID")"

  print_side "L1 (recv from L2)" "$L1_RPC_URL" "$L1_LZ_ENDPOINT" "$l1_harness" "$L1_REMOTE_EID" "$L2_REMOTE_EID" "$l2_sender_b32" "$l1_nonce"
  echo
  print_side "L2 (recv from L1)" "$L2_RPC_URL" "$L2_LZ_ENDPOINT" "$l2_harness" "$L2_REMOTE_EID" "$L1_REMOTE_EID" "$l1_sender_b32" "$l2_nonce"
}

main "$@"
