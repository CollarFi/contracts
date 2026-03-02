// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @notice Canonical per-loan accounting/state on L2.
interface ICollarLoanStore {
    struct Loan {
        // Set by MandateCreated
        address borrower;
        uint256 borrowAmount;
        uint256 minCallStrike;
        uint256 maxPutStrike;
        uint256 minNetInterest;
        uint256 fixedInterest;
        uint256 maxRollLtv;
        uint256 strikeScale;
        uint64 maturity;
        uint64 deadline;

        // Set by DepositIntent/DepositConfirmed
        address collateralAsset;
        uint256 collateralAmount;

        // Active rollover constraints (if rolloverPending=true)
        bool rolloverPending;
        bytes32 rolloverMandateHash;
        uint256 rolloverMinCallStrike;
        uint256 rolloverMaxPutStrike;
        uint256 rolloverMinNetInterest;
        uint256 rolloverFixedInterest;
        uint256 rolloverMaxRollLtv;
        uint256 rolloverStrikeScale;
        uint64 rolloverMaturity;
        uint64 rolloverDeadline;

        bool consumed;
    }

    function getLoan(uint256 loanId) external view returns (Loan memory loan);

    function recordMandate(
        uint256 loanId,
        address borrower,
        address collateralAsset,
        uint256 borrowAmount,
        uint256 minCallStrike,
        uint256 maxPutStrike,
        uint256 minNetInterest,
        uint256 fixedInterest,
        uint256 maxRollLtv,
        uint256 strikeScale,
        uint64 maturity,
        uint64 deadline
    ) external;

    function recordCollateral(uint256 loanId, address collateralAsset, uint256 collateralAmount) external;

    function recordRolloverMandate(
        uint256 loanId,
        address borrower,
        bytes32 mandateHash,
        uint256 minCallStrike,
        uint256 maxPutStrike,
        uint256 minNetInterest,
        uint256 fixedInterest,
        uint256 maxRollLtv,
        uint256 strikeScale,
        uint64 maturity,
        uint64 deadline
    ) external;

    function clearRollover(uint256 loanId) external;

    function markConsumed(uint256 loanId) external;
}
