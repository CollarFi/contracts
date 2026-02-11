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
$ forge script script/Counter.s.sol:CounterScript --rpc-url <your_rpc_url> --private-key <your_private_key>
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

# Deploy L2 protocol contracts (receiver + loan store + TSA proxy) with verification
# (L1_MESSENGER/L1_VAULT optional; can wire later)
uv run python script/deploy_l2.py --env testnet --broadcast --verify --derive-registry-profile testnet

# Wire L1<->L2 LayerZero peers after both sides are deployed
uv run python script/wire_lz_peers.py --env testnet --broadcast

# In auto-init mode (no TSA_INIT_DATA), provide TSA init env inputs:
# SUBACCOUNTS, AUCTION, CASH, WRAPPED_DEPOSIT_ASSET, MANAGER, MATCHING,
# BASE_FEED, DEPOSIT_MODULE, WITHDRAWAL_MODULE, TRADE_MODULE, RFQ_MODULE, OPTION_ASSET.

# Check harness wiring/state
uv run python script/lz_harness/status.py
uv run python script/lz_harness/status.py --json

# Send ping and wait for relay
uv run python script/lz_harness/send_ping.py --from l1 --nonce 1

# Inspect route/config details (delegate/libs/config/initializable)
uv run python script/lz_harness/route_check.py
uv run python script/lz_harness/route_check.py --json
```

## L2 deployed TSA smoke trade script

`script/SmokeL2Trade.s.sol` prepares a deterministic tiny-notional L2 collar RFQ flow aligned to Derive production execution (Derive keeper/executor calls `verifyAndMatch`, not this script by default):
- preflight wiring checks (receiver↔tsa↔loanStore + TSA module addresses)
- optional admin hooks (`setSigner` / `setSubmitter`)
- mandate seeding (`recordMandate`) and optional collateral seed (`recordCollateral`)
- optional deposit path (`initiateDeposit` + `processDeposit`)
- RFQ maker+taker action construction/signing
- machine-readable artifact output for API submission (encoded actions/signatures/fill + `verifyAndMatch` calldata)

Required env vars (default Derive keeper flow):
- `L2_SMOKE_TSA`, `L2_SMOKE_RECEIVER`, `L2_SMOKE_LOAN_STORE`, `L2_SMOKE_MATCHING`
- `L2_SMOKE_COLLATERAL_ASSET`, `L2_SMOKE_OPTION_ASSET`, `L2_SMOKE_RFQ_MODULE`
- `L2_SMOKE_TSA_SUBACCOUNT`, `L2_SMOKE_MAKER_SUBACCOUNT`, `L2_SMOKE_TSA_NONCE`, `L2_SMOKE_MAKER_NONCE`
- `L2_SMOKE_LOAN_ID`, `L2_SMOKE_COLLATERAL_AMOUNT`, `L2_SMOKE_BORROW_AMOUNT`
- `L2_SMOKE_EXPIRY`, `L2_SMOKE_CALL_STRIKE`, `L2_SMOKE_PUT_STRIKE`, `L2_SMOKE_CALL_PRICE`, `L2_SMOKE_PUT_PRICE`, `L2_SMOKE_TRADE_AMOUNT`
- `L2_SMOKE_ADMIN_PK`, `L2_SMOKE_SIGNER_PK`, `L2_SMOKE_MAKER_PK`

Optional env vars:
- `L2_SMOKE_DEADLINE` (defaults to `EXPIRY - 60`)
- `L2_SMOKE_RUN_DEPOSIT` (`true/false`, default `false`)
- `L2_SMOKE_SEED_COLLATERAL` (`true/false`, default `true`)
- `L2_SMOKE_CONFIGURE_SIGNER` (`true/false`, default `false`)
- `L2_SMOKE_CONFIGURE_SUBMITTER` (`true/false`, default `false`)
- `L2_SMOKE_SIGNER` (defaults from `L2_SMOKE_SIGNER_PK`)
- `L2_SMOKE_SUBMITTER` (required only if configuring submitter)
- `L2_SMOKE_ARTIFACT_PATH` (if set, writes the artifact JSON file)

Local-only execution mode (fork/dev testing):
- `L2_SMOKE_LOCAL_DIRECT_MATCH=true`
- `L2_SMOKE_EXECUTOR_PK` (required only in local-direct mode; script then calls `verifyAndMatch` directly and runs post-trade assertions)

Run:

```bash
forge script script/SmokeL2Trade.s.sol:SmokeL2Trade \
  --rpc-url "$L2_RPC_URL" \
  --broadcast -vvvv
```

Default run prints `SMOKE_L2_TRADE_ARTIFACTS_JSON` for Derive-side submission. If `L2_SMOKE_RUN_DEPOSIT=false`, pre-fund TSA collateral in advance (or run equivalent deposit path externally) so the RFQ collar open can pass collateral checks.
