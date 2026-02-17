# Keeper (TS) — deprecated

The previous TypeScript keeper implementation has been removed.

Use Python management scripts under:

- `script/management/l2_keeper_handle_messages.py`

Run with uv, for example:

```bash
uv run python script/management/l2_keeper_handle_messages.py --env testnet --once
uv run python script/management/l2_keeper_handle_messages.py --env testnet --broadcast
```
