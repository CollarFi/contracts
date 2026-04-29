# Latest margin-engine harness coverage

The `test/e2e/LatestMarginEngineHarnessFlows.t.sol` suite mirrors the latest cross-repo deployment seam that matters to `contracts`:

- initializer-based proxy deployment for both engine and RFQ router
- engine-side `rfqRouter` wiring instead of constructor-only assumptions
- router validation/execution through `getRfqActionMetadata(...)` rather than legacy bucket/instrument getter coupling
- same-network origination -> rollover -> settlement exercised through the vault against that harness

## Remaining gap

This harness is intentionally local to `contracts` and only mirrors the latest shape; it does **not** import or deploy the real `CollarFi/margin-engine` implementation inside this repo.

That means one cross-repo gap still remains explicit:

- a true integration test that deploys the canonical `margin-engine` proxy + canonical `MarginEngineRfqRouter` from the `margin-engine` repo and runs the same same-network flow end to end.

The local harness closes the biggest drift risk in `contracts` by matching the current initialization/wiring/metadata seam, but it is still a compatibility harness rather than the real external dependency.
