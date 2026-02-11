#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   MAINNET_RPC_URL=... OP_RPC_URL=... ./keeper/scripts/devnet.sh
#
# Starts two anvil forks, deploys L1+L2, writes keeper/.env, and runs the keeper.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
KEEPER_DIR="$ROOT_DIR/keeper"

: "${MAINNET_RPC_URL:?set MAINNET_RPC_URL}"
: "${OP_RPC_URL:?set OP_RPC_URL}"

L1_PORT="${L1_PORT:-8545}"
L2_PORT="${L2_PORT:-9545}"
ADMIN="${ADMIN:-0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266}"

# Default: Euler Earn USDC
EULER_EARN_USDC="${EULER_EARN_USDC:-0x3B4802FDb0E5d74aA37d58FD77d63e93d4f9A4AF}"

PIDS=()
cleanup() {
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

need anvil
need forge
need jq

mkdir -p "$KEEPER_DIR/out"

echo "==> starting L1 anvil fork on :$L1_PORT"
anvil --fork-url "$MAINNET_RPC_URL" --port "$L1_PORT" >"$KEEPER_DIR/out/anvil-l1.log" 2>&1 &
PIDS+=("$!")

echo "==> starting L2 anvil fork (OP) on :$L2_PORT"
anvil --fork-url "$OP_RPC_URL" --port "$L2_PORT" >"$KEEPER_DIR/out/anvil-l2.log" 2>&1 &
PIDS+=("$!")

# Wait a moment for anvils to come up
sleep 1

echo "==> deploying L1"
(
  cd "$ROOT_DIR"
  export RPC_URL="http://127.0.0.1:$L1_PORT"
  export ADMIN
  export VAULT_OWNER="$ADMIN"
  export TREASURY="$ADMIN"
  export EULER_EARN_USDC
  export L2_RECIPIENT="$ADMIN"
  export L2_EID="${L2_EID:-0}"
  export OUTPUT_JSON="$KEEPER_DIR/out/l1-addresses.json"

  forge script script/DeployL1.s.sol:DeployL1 --rpc-url "$RPC_URL" --broadcast -v
)

L1_MESSENGER="$(jq -r .addrs.l1Messenger "$KEEPER_DIR/out/l1-addresses.json")"
L1_VAULT="$(jq -r .addrs.l1Vault "$KEEPER_DIR/out/l1-addresses.json")"

echo "==> deploying L2"
(
  cd "$ROOT_DIR"
  export RPC_URL="http://127.0.0.1:$L2_PORT"
  export ADMIN
  export L1_MESSENGER
  export L1_VAULT
  export L1_EID="${L1_EID:-0}"
  export OUTPUT_JSON="$KEEPER_DIR/out/l2-addresses.json"

  forge script script/DeployL2.s.sol:DeployL2 --rpc-url "$RPC_URL" --broadcast -v
)

L2_RECEIVER="$(jq -r .addrs.l2Receiver "$KEEPER_DIR/out/l2-addresses.json")"

echo "==> writing keeper/.env"
cat >"$KEEPER_DIR/.env" <<EOF
L1_RPC_URL=http://127.0.0.1:$L1_PORT
L2_RPC_URL=http://127.0.0.1:$L2_PORT

ANVIL_MNEMONIC="test test test test test test test test test test test junk"
L1_ACCOUNT_INDEX=0
L2_ACCOUNT_INDEX=0

L1_COLLAR_VAULT=$L1_VAULT
L1_LZ_MESSENGER=$L1_MESSENGER
L2_TSA_RECEIVER=$L2_RECEIVER

L1_START_BLOCK=0
L2_START_BLOCK=0
POLL_MS=3000
L2_MAX_VALUE_WEI=0
EOF

echo "Wrote $KEEPER_DIR/.env"

echo "==> starting keeper"
cd "$KEEPER_DIR"
exec pnpm dev
