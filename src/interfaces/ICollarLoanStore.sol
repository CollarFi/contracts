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
        uint64 maturity;
        uint64 deadline;

        // Set by DepositIntent/DepositConfirmed
        address collateralAsset;
        uint256 collateralAmount;

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
        uint64 maturity,
        uint64 deadline
    ) external;

    function recordCollateral(uint256 loanId, address collateralAsset, uint256 collateralAmount) external;

    function markConsumed(uint256 loanId) external;
}
