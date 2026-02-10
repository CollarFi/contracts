// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";

import {ICollarLoanStore} from "./interfaces/ICollarLoanStore.sol";

/// @notice Canonical per-loan accounting/state on L2.
/// @dev Written by CollarTSAReceiver (from LZ messages) and read by CollarTSA (during RFQ validation).
contract CollarLoanStore is AccessControl, ICollarLoanStore {
    bytes32 public constant WRITER_ROLE = keccak256("WRITER_ROLE");

    mapping(uint256 => Loan) internal _loans;

    event MandateRecorded(uint256 indexed loanId, address borrower, uint256 borrowAmount);
    event CollateralRecorded(uint256 indexed loanId, address collateralAsset, uint256 collateralAmount);
    event LoanConsumed(uint256 indexed loanId);

    error CLS_InvalidLoanId();
    error CLS_AlreadyConsumed();
    error CLS_Mismatch();

    constructor(address admin) {
        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        _grantRole(WRITER_ROLE, admin);
    }

    function getLoan(uint256 loanId) external view returns (Loan memory loan) {
        return _loans[loanId];
    }

    function recordMandate(
        uint256 loanId,
        address borrower,
        address collateralAsset,
        uint256 borrowAmount,
        uint256 minCallStrike,
        uint256 maxPutStrike,
        uint64 maturity,
        uint64 deadline
    ) external onlyRole(WRITER_ROLE) {
        if (loanId == 0) {
            revert CLS_InvalidLoanId();
        }

        Loan storage loan = _loans[loanId];
        if (loan.consumed) {
            revert CLS_AlreadyConsumed();
        }

        // If already set, require identical values.
        if (loan.borrower != address(0) && loan.borrower != borrower) {
            revert CLS_Mismatch();
        }
        if (loan.borrowAmount != 0 && loan.borrowAmount != borrowAmount) {
            revert CLS_Mismatch();
        }
        if (loan.maturity != 0 && loan.maturity != maturity) {
            revert CLS_Mismatch();
        }
        if (loan.deadline != 0 && loan.deadline != deadline) {
            revert CLS_Mismatch();
        }
        if (loan.minCallStrike != 0 && loan.minCallStrike != minCallStrike) {
            revert CLS_Mismatch();
        }
        if (loan.maxPutStrike != 0 && loan.maxPutStrike != maxPutStrike) {
            revert CLS_Mismatch();
        }

        // Collateral asset can be set either by deposit or mandate. Require consistency.
        if (loan.collateralAsset != address(0) && loan.collateralAsset != collateralAsset) {
            revert CLS_Mismatch();
        }

        loan.borrower = borrower;
        loan.borrowAmount = borrowAmount;
        loan.minCallStrike = minCallStrike;
        loan.maxPutStrike = maxPutStrike;
        loan.maturity = maturity;
        loan.deadline = deadline;
        if (loan.collateralAsset == address(0)) {
            loan.collateralAsset = collateralAsset;
        }

        emit MandateRecorded(loanId, borrower, borrowAmount);
    }

    function recordCollateral(uint256 loanId, address collateralAsset, uint256 collateralAmount)
        external
        onlyRole(WRITER_ROLE)
    {
        if (loanId == 0) {
            revert CLS_InvalidLoanId();
        }

        Loan storage loan = _loans[loanId];
        if (loan.consumed) {
            revert CLS_AlreadyConsumed();
        }

        if (loan.collateralAsset != address(0) && loan.collateralAsset != collateralAsset) {
            revert CLS_Mismatch();
        }
        if (loan.collateralAmount != 0 && loan.collateralAmount != collateralAmount) {
            revert CLS_Mismatch();
        }

        loan.collateralAsset = collateralAsset;
        loan.collateralAmount = collateralAmount;

        emit CollateralRecorded(loanId, collateralAsset, collateralAmount);
    }

    function markConsumed(uint256 loanId) external onlyRole(WRITER_ROLE) {
        if (loanId == 0) {
            revert CLS_InvalidLoanId();
        }

        Loan storage loan = _loans[loanId];
        if (loan.consumed) {
            revert CLS_AlreadyConsumed();
        }
        loan.consumed = true;
        emit LoanConsumed(loanId);
    }
}
