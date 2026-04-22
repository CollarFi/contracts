# CollarFi Contracts

This worktree updates CollarFi from the old Derive + bridge + cross-chain architecture to a same-network architecture built around the in-house margin engine.

## Core shape

- `src/CollarVault.sol`: borrower-facing vault with direct same-chain margin-engine settlement.
- `src/CollarLiquidityVault.sol`: lender USDC pool.
- `src/adapters/*`: variable-rate loan adapters used for neutral-expiry conversion.
- `docs/SPEC.md`: canonical protocol behavior.

The active origination and settlement path no longer depends on:

- Derive subaccounts,
- bridge adapters,
- LayerZero messaging,
- L1/L2 receiver handshakes.

## Local commands

```bash
forge build
forge test -vv
forge test --match-path test/e2e/SameNetworkMarginEngineFlows.t.sol -vv
forge fmt
```

## Notes

- `docs/SPEC.md` is the source of truth for behavior.
- The same-network integration now supports pre-maturity rollover through `MarginEngineRfqRouter` using a validated 4-leg unwind/open package.
- `finalizeRollover(...)` remains part of the legacy ABI only; same-network rollover executes synchronously in a single transaction.
- Legacy bridge-oriented contracts may still exist in the tree for migration/reference, but they are not part of the active core flow.
