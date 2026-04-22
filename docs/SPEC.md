# CollarFi Protocol Technical Specification

Version 2.0 - Same-network margin-engine architecture

## 1. Overview

CollarFi issues fixed-maturity USDC loans against crypto collateral such as WBTC, cbBTC, WETH, and wrapped staking assets. Each zero-cost loan is hedged with a collar opened against the in-house `margin-engine` that lives on the same network as the CollarFi vault.

The protocol economics remain unchanged:

1. **Put ITM**: the put finishes in the money, protecting the lender repayment path.
2. **Neutral**: both options finish out of the money and the borrower keeps exposure by rolling into a variable-rate loan, or a deterministic close path is used if conversion is not completed.
3. **Call ITM**: the call caps borrower upside, lender principal is repaid, and any residual above debt goes to the borrower.

The major structural change from the previous design is that CollarFi no longer bridges collateral, no longer sends cross-chain messages, and no longer depends on Derive-specific subaccounts, receivers, or messenger acknowledgements. The vault, the margin engine, the collateral, and the settlement logic all execute on one network.

## 2. Components

| Component | Role |
| --- | --- |
| Borrower | Supplies collateral and receives USDC principal. |
| Lender | Deposits USDC into `CollarLiquidityVault`. |
| `CollarVault` | Core borrower-facing contract. Holds pending deposits, originates loans, owns covered-call buckets, and settles loans. |
| `margin-engine` | Same-network options primitive. Holds put collateral in USDC and covered-call collateral in underlying units. |
| Market maker | Whitelisted owner of per-loan put buckets. Issues long put claims to `CollarVault`. Receives long call claims from `CollarVault`. |
| Keeper | Finalizes loans after mandate acceptance and triggers settlement at or after maturity. |
| Lending adapter | Converts neutral expiries into variable-rate loans. |

## 3. Same-network architecture

### 3.1 Removed assumptions

The protocol no longer relies on:

- Derive exchange accounts or subaccounts,
- bridge adapters,
- LayerZero messages,
- L1/L2 receivers or messenger acknowledgements,
- asynchronous bridge confirmation for origination or settlement.

Legacy bridge-specific contracts may remain in the repository for migration reference, but they are not part of the active loan lifecycle.

### 3.2 Margin-engine position model

For auditability, the CollarFi integration uses **one put bucket and one covered-call bucket per loan**.

- The market maker creates and funds a put bucket for the exact instrument `(underlying, USDC, USDC, expiry, putStrike, Put)`.
- The market maker issues `LongPutToken` for the exact loan quantity directly to `CollarVault`.
- `CollarVault` creates a covered-call bucket for the exact instrument `(underlying, USDC, underlying, expiry, callStrike, Call)`.
- `CollarVault` deposits the borrower collateral into that covered-call bucket.
- The covered-call bucket issues:
  - `LongCallToken` to the market maker,
  - `CappedUnderlyingToken` to `CollarVault`.

This one-loan-per-bucket design intentionally avoids shared-bucket allocation accounting inside CollarFi v1.

## 4. Origination flow

### 4.1 Pending deposit creation

Borrowers originate by calling either:

- `createDepositWithMandate`, or
- `createDepositWithMandatePermit`.

The vault pulls collateral and records a `PendingDeposit`. No ETH bridge fee is required and any non-zero `msg.value` must revert.

### 4.2 Mandate acceptance

The borrower submits a keeper-signed `BaselineRfq` that binds:

- collateral asset and amount,
- maturity,
- put strike,
- call strike,
- borrow amount,
- borrower,
- RFQ expiry,
- replay-protection nonce.

The vault verifies the EIP-712 signature against `RFQ_SIGNER_ROLE`, marks the RFQ hash consumed, computes fixed interest from `originationFeeApr`, and reserves USDC principal in `CollarLiquidityVault`.

### 4.3 Finalization

After the market maker has funded and issued the exact put claims to the vault, a keeper calls `finalizeLoan` with:

- the per-loan put bucket id,
- the call buyer / long-call recipient.

`CollarVault` must verify:

- the put instrument matches the pending loan terms,
- the call instrument matches the accepted mandate,
- the put bucket underwrites the exact loan quantity,
- the vault holds the exact long put quantity,
- the roll-safe LTV bound still holds.

On success the vault creates the covered-call bucket, deposits collateral, borrows reserved USDC from `CollarLiquidityVault`, and transfers principal to the borrower.

## 5. Settlement flow

### 5.1 Oracle finalization

Settlement uses the final oracle spot from `margin-engine` for both the put and call instruments. The final spot must be finalized on-chain for both instruments and must match exactly.

### 5.2 Outcome rules

For final spot `S_T`, put strike `K_p`, and call strike `K_c`:

- `S_T < K_p` => `PutITM`
- `K_p <= S_T <= K_c` => `Neutral`
- `S_T > K_c` => `CallITM`

Strike equality is treated as out-of-the-money for that leg.

### 5.3 Deterministic same-network settlement

For ITM settlement, the vault performs two deterministic actions:

1. Redeems option claims from `margin-engine`.
2. Sells the redeemed underlying directly to the settlement caller at the finalized oracle spot.

This removes the previous off-chain spot-RFQ and bridge-report path while preserving the loan payoff logic.

For a loan quantity `Q` and strike scale `scale`:

- put payout in USDC:
  - `Q * max(K_p - S_T, 0) / scale`
- covered-call collateral sold to settlement caller:
  - `Q` when `S_T <= K_c`
  - `Q * K_c / S_T` when `S_T > K_c`
- caller payment in USDC:
  - `collateralSold * S_T / scale`

Total settlement value is:

- `putPayout + callerPayment`

The vault then:

- repays principal to `CollarLiquidityVault`,
- transfers fixed interest to `CollarLiquidityVault`,
- sends `PutITM` excess to treasury and lenders according to `treasuryBps`,
- sends `CallITM` excess to the borrower.

### 5.4 Neutral outcome

When both options expire out of the money, the vault redeems `CappedUnderlyingToken` back into collateral and moves the loan into `READY_FOR_VARIABLE`.

From there:

- anyone may call `tryConvertReadyLoan` to open the variable-rate position if liquidity is available, or
- anyone may call `settleReadyLoanByRepay` to repay debt and deterministically distribute collateral after the grace period.

### 5.5 Pre-maturity same-network rollover

Borrowers may refresh an active zero-cost loan before maturity by signing an EIP-712 `RolloverMandate` that binds:

- `loanId`,
- `borrower`,
- `newMaturity`,
- `minCallStrike`,
- `maxPutStrike`,
- `minNetInterest`,
- `deadline`,
- borrower-scoped replay-protection nonce.

The keeper first prepares the new covered-call bucket with `prepareRolloverCallBucket`. This creates the concrete new call bucket that the off-chain RFQ package references.

The keeper then submits `executeRollover` with a `MarginEngineRfqRouter` quote whose taker and authorized executor are both the vault. The current same-network rollover path requires an exact 4-leg package:

1. sell the old put from the vault to the maker,
2. buy back the old call by burning the maker-held long call together with the vault-held capped token, returning underlying collateral to the vault,
3. sell the new call by minting against the pre-created vault-owned covered-call bucket,
4. buy the new put into the vault, either by minting from a funded put bucket or by maker inventory transfer.

The vault validates:

- borrower mandate signature and replay protection,
- exact bucket / instrument / quantity alignment for the old legs,
- the new call bucket exists, is vault-owned, and is empty before execution,
- the new call strike is `>= minCallStrike`,
- the new put strike is `<= maxPutStrike`,
- `newMaturity` matches the new option expiries,
- the roll-safe LTV bound still holds against the new put strike for the full rolled debt (`principal + carriedInterest + newInterest`).

Rollover economics are checked from the vault's actual USDC balance delta across `executeRfq(...)`:

- `realizedC = usdcBalanceAfter - usdcBalanceBefore`
- require `realizedC >= 0`
- require `newInterest + realizedC >= minNetInterest`
- transfer realized premium cash to `CollarLiquidityVault` immediately after validation so no rollover proceeds remain stranded on the vault balance.

On success the vault updates the active loan in place:

- replace maturity,
- replace call / put strikes,
- replace call / put bucket ids,
- replace call / put instrument ids,
- reset `startTime`,
- carry forward any unaccrued prior fixed interest and add the new fixed-interest amount for the rolled tenor.

No asynchronous confirmation step is used in the same-network architecture. `finalizeRollover(...)` remains ABI compatibility only and must not be part of the active rollover flow.

## 6. Safety checks

The implementation must enforce the following invariants.

### 6.1 Origination invariants

- Borrow amount must be non-zero.
- Put strike must be non-zero.
- Collateral asset must be explicitly enabled.
- RFQ signatures must be valid and replay-protected.
- Principal reservation must happen exactly once per pending loan.
- Finalization must only happen once.
- The roll-safe bound must hold:
  - `borrowAmount + fixedInterest <= collateralAmount * putStrike / strikeScale * maxRollLtv`

### 6.2 Settlement invariants

- A loan in `ACTIVE_ZERO_COST` must not settle before maturity.
- Put and call final spots must match.
- Settlement must not execute twice.
- `Neutral` settlement must redeem the full collateral amount before marking `READY_FOR_VARIABLE`.
- `PutITM` and `CallITM` must repay principal before distributing any excess.
- The deterministic caller payment must be computed from the finalized oracle spot only.

### 6.3 Variable-loan invariants

- Conversion must only occur from `READY_FOR_VARIABLE`.
- Variable collateral withdrawals must only be available to the borrower.
- Closing flows must not leave stale debt or stale collateral accounting.

## 7. Roles

- `DEFAULT_ADMIN_ROLE`: role administration.
- `PARAMETER_ROLE`: treasury, engine, collateral, and risk parameter updates.
- `KEEPER_ROLE`: loan finalization.
- `RFQ_SIGNER_ROLE`: authorized baseline RFQ signers.
- `PAUSER_ROLE`: emergency pause control.

## 8. Unsupported in this integration

The current same-network integration deliberately does **not** implement cross-chain functionality.

`finalizeRollover(...)` is also not part of the active same-network lifecycle. It is retained only for ABI compatibility; rollover execution is synchronous and must complete inside `executeRollover(...)`.

## 9. Clarifications / TODOs

- **Rollover quoting**: off-chain services must continue constructing the exact 4-leg unwind/open package expected by the vault validations.
- **Zero-spot covered-call edge case**: CollarFi tests cover `spot = 0`. Production integration should confirm the upstream margin-engine settlement path preserves the expected capped-underlying behavior at zero spot.
- **Shared put buckets**: the margin-engine supports shared underwriting across many consumers, but CollarFi v1 intentionally uses one bucket per loan for simpler accounting. Shared-bucket allocation may be added later if explicitly specified.

## 10. Conclusion

CollarFi now runs as a same-network architecture: collateral stays on one chain, the local margin-engine provides option claims and final settlement state, and the vault settles deterministically without cross-chain messaging or bridge acknowledgements. This preserves the borrower and lender payoff semantics while materially simplifying the system boundary and reducing operational risk.
