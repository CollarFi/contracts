# Deployments

Deployment artifacts are organized by `chain_id`:

- `deployments/<chain_id>/l1.json`
- `deployments/<chain_id>/l2.json`

## Generate from Foundry broadcast logs

Use:

```bash
python3 script/export_deployments.py <chain_id> l1
python3 script/export_deployments.py <chain_id> l2
```

The exporter reads:

- `broadcast/DeployL1.s.sol/<chain_id>/run-latest.json`
- `broadcast/DeployL2.s.sol/<chain_id>/run-latest.json`

and writes the normalized deployment json file under `deployments/<chain_id>/`.

## Notes

- Existing flat files (e.g. `deployments/l1-default.json`) are legacy outputs.
- New deploy scripts default to chain-id output paths when `OUTPUT_JSON` is not provided.
