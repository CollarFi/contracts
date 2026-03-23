# Deployments

Deployment artifacts are organized by `chain_id`:

- `deployments/<chain_id>/l1.json`
- `deployments/<chain_id>/l2.json`

## Generate from Foundry broadcast logs
Deployment JSON is now written directly by the Python deployers:

```bash
uv run python ops/deploy_l1.py --env testnet --broadcast
uv run python ops/deploy_l2.py --env testnet --broadcast
```

The deployers write normalized files under `deployments/<chain_id>/`.

## Notes

- Existing flat files (e.g. `deployments/l1-default.json`) are legacy outputs.
- The old Solidity deploy scripts and broadcast-log exporter were removed.
- New deploy scripts default to chain-id output paths when `OUTPUT_JSON` is not provided.
