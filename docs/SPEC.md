# CollarFi Protocol Technical Specification

Version 2.0 — current same-network implementation

## 1. Overview

This specification describes the **current implementation** in this repository.

CollarFi originates fixed-maturity USDC loans against supported collateral assets. A newly finalized loan is a **zero-cost collar loan** held entirely on one network:

- the borrower posts collateral to `CollarVault`,
- the lender side is funded by `CollarLiquidityVault`,
- the downside hedge is a long put held by `CollarVault` in the local `margin-engine`,
- the upside cap is a covered call written from `CollarVault` collateral in the local `margin-engine`.

The active implementation has four live loan states:

1. `ACTIVE_ZERO_COST`
2. `READY_FOR_VARIABLE`
3. `ACTIVE_VARIABLE`
4. `CLOSED`

There is no cross-chain execution path in this implementation.

## 2. Contracts, actors, and roles

### 2.1 Core contracts

| Component | Current responsibility |
| --- | --- |
| `CollarVault` | Borrower-facing vault. Holds pending collateral, accepts signed baseline RFQs, finalizes zero-cost loans, settles matured loans, prepares and executes same-network rollovers, and manages variable-loan transitions. |
| `CollarLiquidityVault` | ERC4626 lender pool for the USDC asset. Tracks idle liquidity, reserved principal, and active loan principal. Can optionally place idle assets in an external ERC4626 yield vault. |
| `margin-engine` | Local options primitive used for put buckets, covered-call buckets, oracle finalization, and bucket redemption. |
| `ILendingAdapter` + cloned `IVariableLoanPosition` | Variable-rate continuation path used after neutral expiry. |

### 2.2 External actors

| Actor | Current responsibility |
| --- | --- |
| Borrower | Creates pending deposits, accepts or refreshes mandates, may cancel pending deposits when no live mandate remains, receives principal, signs rollover mandates, repays active variable debt, and withdraws variable collateral. |
| RFQ signer | Address with `RFQ_SIGNER_ROLE` that signs baseline origination RFQs. |
| Keeper | Address with `KEEPER_ROLE` that finalizes pending loans, prepares rollover call buckets, and executes rollovers. |
| Settlement caller | Any caller may settle matured zero-cost loans, attempt ready-to-variable conversion, or close a ready loan by repayment. |
| Call buyer / market maker | Receives long-call tokens on origination and participates in rollover RFQ execution. |

### 2.3 Access-controlled roles

`CollarVault` defines:

- `DEFAULT_ADMIN_ROLE`
- `KEEPER_ROLE`
- `PARAMETER_ROLE`
- `PAUSER_ROLE`
- `RFQ_SIGNER_ROLE`

`PARAMETER_ROLE` controls current vault configuration, including:

- treasury address and `treasuryBps`,
- `originationFeeApr`,
- `maxTotalPrincipal`,
- `maxRollLtv`,
- `maxMandateDuration`,
- `readyLoanCloseGracePeriod`,
- enabled collateral assets, strike scales, and mapped engine assets,
- the local `margin-engine`,
- the local `MarginEngineRfqRouter`,
- the lending adapter,
- the variable-loan-position implementation clone target.

`CollarLiquidityVault` defines:

- `DEFAULT_ADMIN_ROLE`
- `VAULT_ROLE`
- `PARAMETER_ROLE`

## 3. Collateral, strike scale, and engine assets

A collateral asset is usable only when `CollarVault` has current config for it:

- `_collateralAllowed[asset] == true`
- `_strikeScale[asset] != 0`
- `_engineAsset[asset]` points to the local `margin-engine` underlying asset used for option instruments.

All option pricing and settlement math uses the per-collateral strike scale stored in `CollarVault`.

## 4. Margin-engine position model

### 4.1 Origination positions

A newly finalized zero-cost loan uses:

- one exact **put bucket** already funded in `margin-engine`, and
- one newly created **covered-call bucket** created by `CollarVault` during finalization.

At finalization:

- the put bucket must reference the exact put instrument for the pending deposit,
- the put bucket must have `outstandingQuantity == collateralAmount`,
- `CollarVault` must already hold the exact long-put quantity,
- `CollarVault` creates the covered-call bucket for the exact call instrument,
- `CollarVault` deposits the full collateral amount into that covered-call bucket,
- the long-call token is issued to `FinalizeLoanParams.callBuyer`,
- the capped-underlying token is issued to `CollarVault`.

### 4.2 Rollover positions

A rollover keeps the old zero-cost loan live until a synchronous RFQ execution replaces its option legs.

For the rolled loan:

- the **new call bucket** must always be pre-created by `prepareRolloverCallBucket(...)` and must be empty before execution,
- the **new put leg** may arrive in one of two supported ways:
  1. `Mint` into a bucket that underwrites exactly the rolled quantity, or
  2. `Transfer` from maker inventory in an already-funded put bucket.

Because inventory transfer is supported, the current implementation does **not** require the new put bucket to be exclusive to one loan during rollover.

## 5. Origination mechanics

### 5.1 Pending deposit creation

The borrower starts by calling one of:

- `createDepositWithMandate(...)`, or
- `createDepositWithMandatePermit(...)`.

Both paths create a `PendingDeposit` with:

- borrower,
- collateral asset,
- collateral amount,
- maturity,
- requested put strike,
- requested borrow amount.

Validation at this step:

- collateral asset must be enabled,
- collateral amount must be non-zero,
- maturity must be strictly in the future,
- put strike must be non-zero,
- borrow amount must be non-zero.

The Permit2 path additionally validates that the permit targets the collateral asset, the vault as spender, and at least the requested amount.

### 5.2 Baseline RFQ acceptance / refresh

A borrower accepts a live quote by calling `acceptMandate(...)`, or does so atomically inside `createDepositWithMandate(...)` / `createDepositWithMandatePermit(...)`.

The signed object is `BaselineRfq`:

- `loanId`
- `collateralAsset`
- `collateralAmount`
- `maturity`
- `putStrike`
- `callStrike`
- `borrowAmount`
- `minNetInterest`
- `rfqExpiry`
- `borrower`
- `nonce`

Current acceptance mechanics:

- the borrower must be `msg.sender`,
- the chosen mandate deadline must be in the future and within `maxMandateDuration`,
- an existing mandate may only be replaced once its deadline has been reached or passed,
- the RFQ must match the pending deposit terms exactly,
- for the atomic create+accept flows, `rfq.loanId` may be `0` as a wildcard sentinel or may equal the newly created loan id,
- for standalone `acceptMandate(...)`, `rfq.loanId` must equal the existing pending loan id,
- `rfq.borrower` may be `address(0)` as a wildcard or may equal the pending borrower,
- the recovered signer must have `RFQ_SIGNER_ROLE`,
- the RFQ hash is one-time-use replay protection,
- if this is the first accepted mandate for the pending deposit, principal is reserved in `CollarLiquidityVault`,
- fixed interest is quoted from `originationFeeApr` over `block.timestamp -> maturity`,
- the roll-safety LTV bound is enforced against the pending deposit collateral and put strike,
- `fixedInterest` must be at least `rfq.minNetInterest`.

The stored mandate records:

- borrower,
- collateral asset and amount,
- maturity,
- borrower-selected deadline,
- borrow amount,
- opening call strike,
- opening put strike,
- minimum net interest,
- fixed interest,
- `maxRollLtv` snapshot.

### 5.3 Pending deposit cancellation

The borrower may call `requestCollateralReturn(...)` to unwind a pending deposit.

Current implementation permits this only when:

- the pending deposit exists,
- the caller is the borrower,
- there is no live mandate, meaning either:
  - no mandate exists, or
  - the stored mandate deadline has already passed.

If a committed mandate existed, the reserved principal is released from `CollarLiquidityVault` before collateral is returned to the borrower.

### 5.4 Loan finalization

A keeper completes origination by calling:

- `finalizeLoan(uint256 loanId, FinalizeLoanParams calldata params)`

where `FinalizeLoanParams` contains:

- `putBucketId`
- `callBuyer`

Current finalization checks:

- pending deposit exists,
- mandate exists,
- mandate deadline has not passed,
- `callBuyer != address(0)` and `putBucketId != 0`,
- computed put and call instruments match the pending deposit + mandate terms,
- the put bucket matches the exact put instrument and exact loan quantity,
- `CollarVault` holds the exact long-put quantity.

On success:

1. `CollarVault` creates the covered-call bucket for the call instrument,
2. `CollarVault` deposits the full collateral amount into that bucket,
3. the long call is minted to `callBuyer`,
4. the capped-underlying token is minted to `CollarVault`,
5. pending deposit and mandate records are deleted,
6. the loan is stored as `ACTIVE_ZERO_COST`,
7. reserved principal is borrowed from `CollarLiquidityVault`,
8. principal is transferred to the borrower.

The stored zero-cost loan records:

- borrower,
- collateral asset and amount,
- maturity,
- put strike,
- call strike,
- principal,
- `ACTIVE_ZERO_COST`,
- `startTime`,
- current `interestApr`,
- `interestOwed`,
- zero `variableDebt`,
- current put / call bucket ids,
- current put / call instrument ids.

## 6. Maturity settlement mechanics

### 6.1 Final spot source

`previewSettlement(...)` and `settleLoan(...)` use the **finalized** oracle states recorded in `margin-engine` for the loan's put and call instruments.

The call and put instruments must both be finalized and must report the exact same final spot.

### 6.2 Outcome rules

For final spot `S_T`, put strike `K_p`, and call strike `K_c`:

- `S_T < K_p` => `PutITM`
- `K_p <= S_T <= K_c` => `Neutral`
- `S_T > K_c` => `CallITM`

Equality is out-of-the-money for that leg.

### 6.3 Settlement preview values

For a loan quantity `Q` and strike scale `scale`, the preview logic computes:

- `putPayout = Q * (K_p - S_T) / scale` when `S_T < K_p`, else `0`
- `collateralToBuyer = 0` in `Neutral`
- `collateralToBuyer = Q` in `PutITM`
- `collateralToBuyer = Q * K_c / S_T` in `CallITM`
- `buyerPayment = collateralToBuyer * S_T / scale` when `collateralToBuyer != 0`, else `0`
- `totalSettlementValue = putPayout + buyerPayment`

### 6.4 `settleLoan(...)`

Any caller may settle a matured zero-cost loan by calling:

- `settleLoan(uint256 loanId, SettlementOutcome expectedOutcome)`

Current execution steps:

1. loan must be `ACTIVE_ZERO_COST`,
2. current block time must be at or after maturity,
3. the vault finalizes both instruments in `margin-engine` if needed,
4. the vault settles both buckets if needed,
5. the previewed outcome must equal `expectedOutcome`.

#### Neutral path

For `Neutral`:

- the vault redeems the full capped-underlying position from the call bucket,
- the loan moves to `READY_FOR_VARIABLE`,
- `readyLoanSince[loanId]` is set to `block.timestamp`.

#### ITM paths

For `PutITM` or `CallITM`:

- the vault always redeems capped underlying from the call bucket,
- the vault redeems the put only when `putPayout != 0`,
- the vault checks that redeemed balances cover the previewed collateral and put payout,
- the settlement caller only pays / receives the collateral leg when `buyerPayment != 0`:
  - vault pulls `buyerPayment` USDC from `msg.sender`,
  - vault transfers `collateralToBuyer` collateral to `msg.sender`.

The loan can only close successfully when:

- `totalSettlementValue >= principal + interestOwed`

Distribution on close:

- principal is repaid to `CollarLiquidityVault`,
- fixed interest is transferred to `CollarLiquidityVault`,
- any excess on `PutITM` is split:
  - `treasuryCut = excess * treasuryBps / 10_000`
  - remaining excess to `CollarLiquidityVault`,
- any excess on `CallITM` is transferred to the borrower.

The loan then moves to `CLOSED`.

## 7. Ready-for-variable mechanics

### 7.1 Ready state

A loan enters `READY_FOR_VARIABLE` only through neutral settlement.

At that moment:

- the full collateral amount must already have been redeemed back to the vault,
- the loan keeps its original `principal` and `interestOwed`,
- `readyLoanSince[loanId]` is recorded.

### 7.2 Variable conversion

Any caller may attempt conversion by calling:

- `tryConvertReadyLoan(uint256 loanId)`

Current conversion mechanics:

- loan must be `READY_FOR_VARIABLE`,
- total due is `principal + interestOwed`,
- if no variable position clone exists yet, the vault clones the configured `variableLoanPositionImplementation` and initializes it with:
  - vault address,
  - lending adapter,
  - borrower,
  - collateral asset,
  - USDC asset,
- that clone is stored before the liquidity check,
- if adapter-side available liquidity is below `totalDue`, the function returns `false`; loan state remains `READY_FOR_VARIABLE`, but the cloned position stays stored for later reuse,
- otherwise the vault:
  - decreases committed principal,
  - transfers full collateral into the variable position,
  - opens the variable position for `collateralAmount` collateral and `totalDue` debt,
  - repays principal to `CollarLiquidityVault`,
  - transfers fixed interest to `CollarLiquidityVault`,
  - updates the loan to `ACTIVE_VARIABLE`,
  - stores live variable debt and live collateral,
  - clears `readyLoanSince[loanId]`.

### 7.3 Ready-loan repay-close

Any caller may close a ready loan directly by calling:

- `settleReadyLoanByRepay(uint256 loanId)`

Current mechanics:

- loan must be `READY_FOR_VARIABLE`,
- caller transfers `principal + interestOwed` USDC to the vault,
- principal is repaid to `CollarLiquidityVault`,
- fixed interest is transferred to `CollarLiquidityVault`,
- the collateral split depends on whether the ready-loan grace period has expired.

#### Before grace expiry

- borrower receives all remaining collateral,
- caller receives none.

#### After grace expiry

Current effective implementation gives the caller a base collateral amount:

- `baseCollateral = ceil(totalDue * strikeScale / putStrike)`

That amount is capped by the remaining collateral balance.

With the current implementation, `readyLoanKeeperPenaltyBps` is stored internally but not externally configured, so the effective multiplier is currently `1.0x`.

After distribution, the loan moves to `CLOSED` and the ready-loan timestamp is cleared.

## 8. Active variable-loan mechanics

### 8.1 Repayment

Any caller may repay an active variable loan by calling:

- `repayVariableLoan(uint256 loanId, uint256 amount)`

Current behavior:

- loan must be `ACTIVE_VARIABLE`,
- repayment amount is capped at live debt,
- any excess USDC sent beyond live debt is refunded to the caller,
- the loan stores updated live debt and live collateral after repayment,
- the loan closes only when both live debt and live collateral are zero.

### 8.2 Collateral withdrawal

Only the borrower may withdraw collateral from an active variable loan by calling:

- `withdrawVariableCollateral(uint256 loanId, uint256 amount)`

Current behavior:

- loan must be `ACTIVE_VARIABLE`,
- caller must equal `loan.borrower`,
- requested amount must not exceed current collateral,
- the vault updates stored live debt and live collateral after withdrawal,
- the loan closes only when both live debt and live collateral are zero.

## 9. Same-network rollover mechanics

### 9.1 Borrower mandate

The borrower authorizes rollover with an EIP-712 `RolloverMandate` containing:

- `borrower`
- `loanId`
- `newMaturity`
- `minCallStrike`
- `maxPutStrike`
- `minNetInterest`
- `deadline`
- `nonce`

Current replay protection is both:

- exact mandate-hash consumption, and
- borrower-scoped consumed nonce tracking.

### 9.2 Call-bucket preparation

A keeper prepares the next call bucket by calling:

- `prepareRolloverCallBucket(uint256 loanId, uint64 newMaturity, uint256 newCallStrike)`

Current requirements:

- loan must be `ACTIVE_ZERO_COST`,
- current time must still be before the current maturity,
- `newMaturity` must be strictly after both `block.timestamp` and current maturity,
- `newCallStrike` must be non-zero,
- the computed call instrument must exist and satisfy the requested collateral / expiry / strike bounds.

The function creates the concrete covered-call bucket in `margin-engine` and returns both the instrument id and bucket id.

### 9.3 RFQ quote shape

A keeper executes rollover with:

- `executeRollover(...)`, or
- the ABI-compatibility wrapper `rolloverLoan(...)`.

The current implementation requires a `MarginEngineRfqRouter` quote with:

- `quoteAsset == USDC`
- `validUntil >= block.timestamp`
- `taker == address(CollarVault)`
- `authorizedExecutor == address(CollarVault)`
- exactly **4 actions**.

The actions must be:

1. old put: `Sell / Put / Transfer` from the vault's current put bucket,
2. old call unwind: `Buy / Call / Burn` against the vault's current covered-call bucket,
3. new call open: `Sell / Call / Mint` against the pre-created vault-owned call bucket,
4. new put open: `Buy / Put / Mint` or `Buy / Put / Transfer` into the vault.

Current validations include:

- exact old bucket ids, instrument ids, and quantity alignment,
- new call bucket must already exist, be vault-owned, and be empty before execution,
- new call strike must be `>= mandate.minCallStrike`,
- new put strike must be `<= mandate.maxPutStrike`,
- new option expiries must equal `mandate.newMaturity`.

### 9.4 Rollover economics and state transition

Before execution, the vault computes:

- remaining old fixed interest,
- new fixed interest for `block.timestamp -> newMaturity`,
- the roll-safe LTV bound on:
  - `principal + remainingOldInterest + newInterest`.

The vault then executes the RFQ and computes:

- `realizedC = usdcBalanceAfter - usdcBalanceBefore`

Current economic checks:

- `realizedC >= 0`
- `newInterest + realizedC >= mandate.minNetInterest`

If `realizedC > 0`, the vault transfers that premium immediately into `CollarLiquidityVault`.

Post-execution the vault validates:

- the new put position exists and matches the resolved new instrument,
- the new call bucket is vault-owned and holds the expected collateral and capped token balance,
- the old long put, old capped token, and old covered-call bucket positions are fully cleared.

On success the loan is updated in place:

- new maturity,
- new put strike,
- new call strike,
- new put bucket id,
- new call bucket id,
- new put instrument id,
- new call instrument id,
- `startTime = block.timestamp`,
- `interestApr = originationFeeApr`,
- `interestOwed = remainingOldInterest + newInterest`.

The loan remains `ACTIVE_ZERO_COST` throughout the rollover and the operation is fully synchronous.

## 10. CollarLiquidityVault mechanics

`CollarLiquidityVault` is an ERC4626 vault over the USDC asset.

Current balance accounting tracks:

- local idle USDC balance,
- optional ERC4626 yield-vault balance,
- `activeLoans`,
- `reservedPrincipal`,
- `reservedPrincipalByLoan[loanId]`.

### 10.1 Reservations and borrowing

`CollarVault` uses `VAULT_ROLE` functions to manage lender liquidity:

- `reservePrincipal(loanId, amount)`
- `releasePrincipal(loanId)`
- `borrowReserved(loanId, amount)`
- `repay(amount)`
- `writeOff(amount)`

Current semantics:

- reserved principal is excluded from lender withdrawals and new borrowing availability,
- `availableLiquidity()` returns gross assets minus reserved principal,
- `totalAssets()` includes local USDC, yield-vault assets, and `activeLoans`,
- reserve coverage is enforced so local vault balance must remain at least `reservedPrincipal`.

### 10.2 Optional yield vault

The liquidity vault may be configured with an external ERC4626 yield vault over the same USDC asset.

Current parameter-role actions are:

- `setYieldVault(...)`
- `supplyToYieldVault(...)`
- `withdrawFromYieldVault(...)`

The implementation automatically pulls funds back from the yield vault when local liquidity is needed for outflows or reserve coverage.

## 11. Implemented safety properties

The current implementation enforces at least the following:

- pending deposits require enabled collateral, non-zero borrow amount, non-zero put strike, and future maturity,
- baseline RFQs are EIP-712 signed and one-time-use,
- principal reservation happens once per accepted pending loan and is released on cancellation,
- finalization is keeper-only and cannot proceed after mandate expiry,
- zero-cost settlement cannot occur before maturity,
- put and call final oracle spots must match exactly,
- settlement must cover `principal + interestOwed` before excess is distributed,
- neutral conversion requires `READY_FOR_VARIABLE` and sufficient adapter liquidity,
- borrower-only restriction applies to variable collateral withdrawals,
- rollover requires borrower signature, borrower nonce freshness, exact old-leg alignment, and a 4-action same-network quote shape,
- rollover roll-safety LTV is checked against the full rolled debt,
- `pause()` / `unpause()` gate borrower and settlement flows.

## 12. Unsupported scope

This specification does **not** define any of the following, because they are not present in the current implementation:

- cross-chain messaging,
- bridge fee estimation,
- asynchronous finalization,
- asynchronous rollover completion,
- cross-chain settlement routing.
