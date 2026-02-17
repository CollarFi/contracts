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
uv run python script/lz_harness/deploy.py

# Broadcast deploy + set options + wire peers
uv run python script/lz_harness/deploy.py --broadcast

# Dry-run L1 deployment (safe default, named foundry account/keystore)
# ADMIN is optional; deploy runner derives it from ACCOUNT (foundry keystore) if unset.
uv run python script/deploy_l1.py --env testnet

# Broadcast L1 deployment (CollarVault via ERC1967 proxy with atomic initialize)
uv run python script/deploy_l1.py --env testnet --broadcast

# Deploy L2 protocol contracts (receiver + loan store + TSA proxy) with verification
# (L1_MESSENGER/L1_VAULT optional; can wire later)
uv run python script/deploy_l2.py --env testnet --broadcast --verify --derive-registry-profile testnet

# Wire L1<->L2 LayerZero peers after both sides are deployed
uv run python script/wire_lz_peers.py --env testnet --broadcast

# Check LayerZero ULN/route config for deployed messenger/receiver using current env files
uv run python script/check_lz_uln.py --env testnet
uv run python script/check_lz_uln.py --env testnet --json

# Apply ULN config to both OApps using current effective endpoint configs (dry-run by default)
# Also enforces OApp remoteEid + defaultOptions from env (LZ_RECEIVE_GAS/LZ_RECEIVE_VALUE)
uv run python script/apply_lz_uln_config.py --env testnet
uv run python script/apply_lz_uln_config.py --env testnet --broadcast

# Unified route sync: wire peers + apply ULN config + run final check
uv run python script/ensure_lz_route.py --env testnet
uv run python script/ensure_lz_route.py --env testnet --broadcast

# Enable ETH collateral on L1 CollarVault (dry-run default; resolves vault from deployments/<CHAIN_ID>/l1.json)
uv run python script/management/enable_collateral.py --env testnet
uv run python script/management/enable_collateral.py --env testnet --broadcast

# L2 keeper: watch MessageReceived on CollarTSAReceiver and handle DepositIntent messages
# (dry-run default; stores cursor in deployments/keeper_l2_state.json)
uv run python script/management/l2_keeper_handle_messages.py --env testnet --once
uv run python script/management/l2_keeper_handle_messages.py --env testnet --broadcast

# In auto-init mode (no TSA_INIT_DATA), provide TSA init env inputs:
# SUBACCOUNTS, AUCTION, CASH, WRAPPED_DEPOSIT_ASSET, MANAGER, MATCHING,
# BASE_FEED, DEPOSIT_MODULE, WITHDRAWAL_MODULE, TRADE_MODULE, RFQ_MODULE, OPTION_ASSET.

# L1 notes:
# - No direct Euler deployment/integration in this flow (liquidity vault can run without setting Euler vault).
# - If LIQUIDITY_VAULT is not provided, set USDC_ASSET and script deploys a fresh CollarLiquidityVault.
# - BRIDGE_CONFIG_ADMIN was removed; ADMIN/VAULT_OWNER is the PARAMETER_ROLE holder at init.
# - If WETH_ASSET is set, deploy enables it as allowed collateral via setCollateralConfig(WETH_ASSET, true, WETH_STRIKE_SCALE).

# Check harness wiring/state
uv run python script/lz_harness/status.py
uv run python script/lz_harness/status.py --json

# Send ping and wait for relay
uv run python script/lz_harness/send_ping.py --from l1 --nonce 1

# Inspect route/config details (delegate/libs/config/initializable)
uv run python script/lz_harness/route_check.py
uv run python script/lz_harness/route_check.py --json
```
