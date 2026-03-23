## Foundry

**Foundry is a blazing fast, portable and modular toolkit for Ethereum application development written in Rust.**

Foundry consists of:

-   **Forge**: Ethereum testing framework (like Truffle, Hardhat and DappTools).
-   **Cast**: Swiss army knife for interacting with EVM smart contracts, sending transactions and getting chain data.
-   **Anvil**: Local Ethereum node, akin to Ganache, Hardhat Network.
-   **Chisel**: Fast, utilitarian, and verbose solidity REPL.

## Documentation

https://book.getfoundry.sh/

## Usage

### Local setup (important)

This repo vendors Derive's `v2-matching` as a submodule. That submodule contains two copies of `lyra-utils`, which can cause Foundry to fail locally with duplicate identifier errors after submodule updates.

Run this once after `git submodule update --init --recursive` (and again if you reset submodules):

```bash
./script/fix-local-deps.sh
```

This mirrors what CI does (prunes the duplicate and normalizes imports).


### Build

```shell
$ forge build
```

### Test

```shell
$ forge test
```

### Format

```shell
$ forge fmt
```

### Gas Snapshots

```shell
$ forge snapshot
```

### Anvil

```shell
$ anvil
```

### Deploy

```shell
$ forge script script/Counter.s.sol:CounterScript --rpc-url <your_rpc_url> --account <keystore_name>
```

### Cast

```shell
$ cast <subcommand>
```

### Help

```shell
$ forge --help
$ anvil --help
$ cast --help
```

## LayerZero harness scripts (Python + uv)

Harness tooling is now Python-based (instead of bash), with optional rich/json output.

Install/run via uv:

```bash
uv sync
```

Examples:

```bash
# Deploy both sides (dry-run by default)
uv run python ops/lz_harness/deploy.py

# Broadcast deploy + set options + wire peers
uv run python ops/lz_harness/deploy.py --broadcast

# Dry-run L1 deployment (safe default, named foundry account/keystore)
# ADMIN is optional; deploy runner derives it from ACCOUNT (foundry keystore) if unset.
# Set PROXY_ADMIN to keep proxy upgrade ownership separate from protocol admin roles.
uv run python ops/deploy_l1.py --env testnet

# Broadcast L1 deployment (CollarVault via ERC1967 proxy with atomic initialize)
# If L2_RECIPIENT is empty, deploy_l1.py auto-resolves it to the L2 receiver address via --l2-env-file.
# If WETH_ASSET is set and L2_WRAPPED_WETH_ASSET is empty in L1 env,
# deploy_l1.py auto-resolves it from L2 TSA via --l2-env-file (default: .env.l2.<env>).
# If DERIVE_SUBACCOUNT_ID is empty, deploy_l1.py also auto-resolves TSA subAccount() from L2.
# Socket adapter msg gas follows LZ_RECEIVE_GAS (legacy WETH_MSG_GAS_LIMIT remains as override fallback).
# L2 receiver lookup order: L2_RECEIVER env -> L2 OUTPUT_JSON -> DeployL2 broadcast run-latest artifact.
uv run python ops/deploy_l1.py --env testnet --broadcast

# Explicit upgrade path for existing proxies
uv run python ops/deploy_l1.py --env testnet --mode upgrade --broadcast

# Deploy L2 protocol contracts (receiver + loan store + TSA proxy) with verification
# (L1_MESSENGER/L1_VAULT optional; can wire later)
# SOCKET_TRACKER is required and must be a real socket tracker address (no mock fallback).
# Set PROXY_ADMIN in .env.l2.<env> to keep proxy upgrade ownership separate from ADMIN.
# DeployL2 applies sane CollarTSA defaults on fresh TSA deploys (signature/risk windows).
# Override with TSA_* env vars.
uv run python ops/deploy_l2.py --env testnet --broadcast --verify --derive-registry-profile testnet

# Explicit upgrade path for existing proxies
uv run python ops/deploy_l2.py --env testnet --mode upgrade --broadcast --verify --derive-registry-profile testnet

# Wire/check L1<->L2 route via the unified route command (includes peer wiring)
uv run python ops/ensure_lz_route.py --env testnet --broadcast

# Unified preflight entrypoint: recipient wiring + ULN/route + asset mapping (+ optional message scan)
uv run python ops/preflight.py --env testnet
uv run python ops/preflight.py --env testnet --include-messages
uv run python ops/preflight.py --env testnet --json

# Apply ULN config to both OApps using current effective endpoint configs (dry-run by default)
# Also enforces OApp remoteEid + defaultOptions from env (LZ_RECEIVE_GAS/LZ_RECEIVE_VALUE)
uv run python ops/apply_lz_uln_config.py --env testnet
uv run python ops/apply_lz_uln_config.py --env testnet --broadcast

# Unified route sync: wire peers + apply ULN config + run final check
uv run python ops/ensure_lz_route.py --env testnet
uv run python ops/ensure_lz_route.py --env testnet --broadcast

# Enable ETH collateral on L1 CollarVault (dry-run default; resolves vault from deployments/<CHAIN_ID>/l1.json)
uv run python ops/management/enable_collateral.py --env testnet
uv run python ops/management/enable_collateral.py --env testnet --broadcast

# Configure L1->L2 message asset mapping (L1 collateral -> L2 wrapped asset used by TSA)
uv run python ops/management/set_l2_message_asset.py --env testnet --l1-asset <L1_ASSET> --l2-asset <L2_WRAPPED_ASSET>
uv run python ops/management/set_l2_message_asset.py --env testnet --broadcast

# Preflight all route/message readiness checks through unified entrypoint
uv run python ops/preflight.py --env testnet --include-messages
uv run python ops/preflight.py --env testnet --include-messages --json

# L2 keeper: watch MessageReceived on CollarTSAReceiver and handle DepositIntent messages
# (dry-run default; stores cursor in deployments/keeper_l2_state.json)
uv run python ops/management/l2_keeper_handle_messages.py --env testnet --once
uv run python ops/management/l2_keeper_handle_messages.py --env testnet --broadcast

# In auto-init mode (no TSA_INIT_DATA), provide TSA init env inputs:
# SUBACCOUNTS, AUCTION, CASH, WRAPPED_DEPOSIT_ASSET, MANAGER, MATCHING,
# BASE_FEED, DEPOSIT_MODULE, WITHDRAWAL_MODULE, TRADE_MODULE, RFQ_MODULE, OPTION_ASSET.

# L1 notes:
# - No direct Euler deployment/integration in this flow (liquidity vault can run without setting Euler vault).
# - If LIQUIDITY_VAULT is not provided, set USDC_ASSET and script deploys a fresh CollarLiquidityVault.
# - DeployL1 ensures CollarVault has VAULT_ROLE on the configured liquidity vault (critical for reserve/borrow paths).
# - BRIDGE_CONFIG_ADMIN was removed; ADMIN/VAULT_OWNER is the PARAMETER_ROLE holder at init.
# - If WETH_ASSET is set, deploy enables it as allowed collateral via setCollateralConfig(WETH_ASSET, true, WETH_STRIKE_SCALE).
# - Set RFQ_SIGNER in .env.l1.<env> to auto-allowlist a keeper signer via grantRole(RFQ_SIGNER_ROLE, ...).

# Check harness wiring/state
uv run python ops/lz_harness/status.py
uv run python ops/lz_harness/status.py --json

# Send ping and wait for relay
uv run python ops/lz_harness/send_ping.py --from l1 --nonce 1

# Inspect route/config details (delegate/libs/config/initializable)
uv run python ops/lz_harness/route_check.py
uv run python ops/lz_harness/route_check.py --json
```
