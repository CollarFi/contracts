# ops/ - Python operational tooling

This folder contains Python automation for deployment orchestration, LayerZero route management, and day-2 protocol operations.

`script/` is reserved for Solidity Foundry scripts (`*.s.sol`).

## Layout

- `deploy_l1.py` / `deploy_l2.py` - deployment orchestrators
- `check_lz_uln.py` - route + ULN config checker
- `apply_lz_uln_config.py` - apply ULN + OApp config (dry-run by default)
- `wire_lz_peers.py` - set LayerZero peers
- `ensure_lz_route.py` - run wire + apply + check in one flow
- `management/` - operator workflows (collateral config, preflights, keeper loop)
- `lz_harness/` - test harness helpers
- `py_lib/` - shared helper library (env/deployment/L2 discovery)

## Common usage

```bash
# L1/L2 deploy (safe by default; add --broadcast to send txs)
uv run python ops/deploy_l1.py --env testnet
uv run python ops/deploy_l2.py --env testnet

# End-to-end route setup/check
uv run python ops/ensure_lz_route.py --env testnet
uv run python ops/ensure_lz_route.py --env testnet --broadcast

# Unified preflight entrypoint (routes ULN + recipient + asset + optional message checks)
uv run python ops/preflight.py --env testnet
uv run python ops/preflight.py --env testnet --include-messages
uv run python ops/preflight.py --env testnet --json

# Ops helpers
uv run python ops/management/enable_collateral.py --env testnet
uv run python ops/management/set_l2_message_asset.py --env testnet
uv run python ops/management/l2_keeper_handle_messages.py --env testnet --once
uv run python ops/management/l1_keeper_handle_messages.py --env testnet --once --logs-rpc-url https://sepolia-rollup.arbitrum.io/rpc

# Fresh-fork E2E: deploy brand-new contracts on anvil forks and write l1-e2e/l2-e2e outputs
uv run python test/e2e/deployment_e2e.py --l1-port 8758 --l2-port 8759 --keep-anvil

# Then run fresh-loan flow against those fresh outputs + fork RPCs
uv run python test/e2e/fresh_loan_flow.py --l1-json deployments/421614/l1-e2e.json --l2-json deployments/901/l2-e2e.json --l1-rpc http://127.0.0.1:8758 --l2-rpc http://127.0.0.1:8759
```

## Defaults and safety

- Most scripts are dry-run unless `--broadcast` is set.
- `--env testnet|mainnet` resolves `.env.l1.<env>` / `.env.l2.<env>` automatically where applicable.
- Address resolution fallback order (typical): env var -> deployment output JSON -> Foundry broadcast artifact.

## `lz_harness` helpers

```bash
# Ensure harness route wiring + endpoint config (dry-run by default)
uv run python ops/lz_harness/ensure_route.py

# Broadcast only the mismatched settings
uv run python ops/lz_harness/ensure_route.py --broadcast

# Custom env files + machine-readable output
uv run python ops/lz_harness/ensure_route.py .env.l1.testnet .env.l2.testnet --json
```
