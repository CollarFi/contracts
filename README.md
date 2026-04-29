# CollarFi Contracts

This worktree contains the same-network CollarFi architecture built around the in-house margin engine.

## Core shape

- `src/CollarVault.sol`: borrower-facing vault with direct same-chain margin-engine settlement.
- `src/CollarLiquidityVault.sol`: lender USDC pool.
- `src/adapters/*`: variable-rate loan adapters used for neutral-expiry conversion.
- `docs/SPEC.md`: canonical protocol behavior.

Collateral origination, option lifecycle management, rollover, and settlement all execute on one network.

## Local commands

```bash
forge build
forge test -vv
forge test --match-path test/e2e/SameNetworkMarginEngineFlows.t.sol -vv
forge fmt
```

## Notes

- `docs/SPEC.md` is the source of truth for behavior.
- The same-network integration supports pre-maturity rollover through `MarginEngineRfqRouter` using a validated 4-leg unwind/open package.
