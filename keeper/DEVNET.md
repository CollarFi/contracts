# Keeper devnet (forks)

You chose:
- forks (`anvil --fork-url ...`)
- L2 target: Optimism
- no raw private keys in `.env` (use anvil default accounts)

## Prereqs

- Foundry installed in WSL: <https://book.getfoundry.sh/getting-started/installation>
- RPC endpoints:
  - Ethereum mainnet RPC (for L1 fork)
  - Optimism mainnet RPC (for L2 fork)

## Fast path (recommended): one command

```bash
export MAINNET_RPC_URL=...
export OP_RPC_URL=...

# from repo root
./keeper/scripts/devnet.sh
```

This will:
- start both anvils (8545/9545)
- deploy L1+L2 (with Euler Earn USDC + mocks)
- write `keeper/.env`
- run the keeper

## Manual path

### 1) Start forks

In two terminals:

```bash
# L1 fork
anvil --fork-url "$MAINNET_RPC_URL" --port 8545

# L2 fork (Optimism)
anvil --fork-url "$OP_RPC_URL" --port 9545
```

### 2) Deploy

### L1 deploy (Vault + LZ Messenger)

Create a `.env` for forge scripts (NOT the keeper one):

```bash
export RPC_URL=http://127.0.0.1:8545
export ADMIN=0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266
export VAULT_OWNER=$ADMIN
export TREASURY=$ADMIN

# Euler Earn USDC (mainnet)
export EULER_EARN_USDC=0x3B4802FDb0E5d74aA37d58FD77d63e93d4f9A4AF

# Optional overrides:
# export LIQUIDITY_VAULT=0x...   # if you already have one
# export PERMIT2=0x000000000022D473030F116dDEE9F6B43aC78BA3
# export EULER_ADAPTER=0x...     # otherwise EulerAdapterMock is deployed

# Fork-dev defaults:
export L2_RECIPIENT=$ADMIN
export LZ_ENDPOINT=0x0000000000000000000000000000000000000000   # omitted => mock deployed
export REMOTE_EID=0

export OUTPUT_JSON=keeper/out/l1-addresses.json

forge script script/DeployL1.s.sol:DeployL1 \
  --rpc-url "$RPC_URL" \
  --broadcast
```

### L2 deploy (TSA Receiver)

```bash
export RPC_URL=http://127.0.0.1:9545
export ADMIN=0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266

# required wiring
export L1_EID=0
export L1_MESSENGER=$(jq -r .addrs.l1Messenger keeper/out/l1-addresses.json)
export L1_VAULT=$(jq -r .addrs.l1Vault keeper/out/l1-addresses.json)

# optional overrides (otherwise mocks are deployed)
# export LZ_ENDPOINT=0x...
# export SOCKET_TRACKER=0x...
# export TSA=0x...
# export TSA_SUBACCOUNT=1

export OUTPUT_JSON=keeper/out/l2-addresses.json

forge script script/DeployL2.s.sol:DeployL2 \
  --rpc-url "$RPC_URL" \
  --broadcast
```

## 3) Run the keeper

```bash
cd keeper
cp .env.example .env

# fill in:
# - L1/L2 RPC URLs
# - L1_COLLAR_VAULT / L1_LZ_MESSENGER / L2_TSA_RECEIVER (from the deploy JSONs)

pnpm dev
```
