// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

import {CollarLZMessages} from "../bridge/CollarLZMessages.sol";
import {ICollarVaultSettleModule} from "../interfaces/ICollarVaultSettleModule.sol";
import {CollarVaultShared} from "./CollarVaultShared.sol";

contract CollarVaultSettleModule is ICollarVaultSettleModule {
    using SafeERC20 for IERC20;
    error CV_InvalidState();
    error CV_InvalidInput();

    event LoanSettled(uint256 indexed loanId, CollarVaultShared.SettlementOutcome outcome, uint256 settlementAmount);
    event SettlementShortfall(uint256 indexed loanId, uint256 shortfall);
    event LoanReadyForVariable(uint256 indexed loanId, uint256 requiredDebt);
    event LoanConverted(uint256 indexed loanId, uint256 variableDebt);
    event LoanClosed(uint256 indexed loanId);

    function settleLoan(uint256 loanId, uint8 outcomeRaw, bytes32 lzGuid) external {
        CollarVaultShared.SettlementOutcome outcome = CollarVaultShared.SettlementOutcome(outcomeRaw);
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        CollarVaultShared.Loan storage loan = $.loans[loanId];
        if (loan.state != CollarVaultShared.LoanState.ACTIVE_ZERO_COST) {
            revert CV_InvalidState();
        }
        if (block.timestamp < loan.maturity) {
            revert CV_InvalidState();
        }

        CollarLZMessages.Message memory lzMessage = _consumeLZMessage(lzGuid);
        uint256 settlementAmount = 0;

        if (outcome == CollarVaultShared.SettlementOutcome.Neutral) {
            $.lzMessenger
                .validateCollateralReturned(
                    lzMessage, loanId, loan.collateralAsset, loan.collateralAmount, address(this), $.deriveSubaccountId
                );
            _markReadyForVariable(loanId, lzMessage.amount);
            emit LoanSettled(loanId, outcome, settlementAmount);
            return;
        }

        settlementAmount = $.lzMessenger.validateSettlementReport(lzMessage, loanId, address($.usdc), address(this));

        uint256 totalDue = loan.principal + loan.interestOwed;
        uint256 shortfall = settlementAmount < totalDue ? totalDue - settlementAmount : 0;
        if (shortfall > 0) {
            $.liquidityVault.consume(loanId, shortfall);
            settlementAmount += shortfall;
            emit SettlementShortfall(loanId, shortfall);
        }

        uint256 principalRepay = settlementAmount > loan.principal ? loan.principal : settlementAmount;
        if (principalRepay > 0) {
            $.usdc.safeIncreaseAllowance(address($.liquidityVault), principalRepay);
            $.liquidityVault.repay(principalRepay);
        }
        if (principalRepay < loan.principal) {
            uint256 principalShortfall = loan.principal - principalRepay;
            $.liquidityVault.writeOff(principalShortfall);
            emit SettlementShortfall(loanId, principalShortfall);
        }

        uint256 interestPaid =
            settlementAmount > loan.principal ? Math.min(settlementAmount - loan.principal, loan.interestOwed) : 0;
        if (interestPaid > 0) {
            $.usdc.safeTransfer(address($.liquidityVault), interestPaid);
        }

        uint256 excess = settlementAmount > totalDue ? settlementAmount - totalDue : 0;

        if (excess > 0) {
            if (outcome == CollarVaultShared.SettlementOutcome.PutITM) {
                uint256 treasuryCut = Math.mulDiv(excess, $.treasuryBps, CollarVaultShared.MAX_BPS);
                uint256 vaultCut = excess - treasuryCut;
                if (treasuryCut > 0) {
                    $.usdc.safeTransfer($.treasury, treasuryCut);
                }
                if (vaultCut > 0) {
                    $.usdc.safeTransfer(address($.liquidityVault), vaultCut);
                }
            } else if (outcome == CollarVaultShared.SettlementOutcome.CallITM) {
                $.usdc.safeTransfer(loan.borrower, excess);
            }
        }

        _releaseCommittedPrincipal(loan.principal);
        _releaseReserve(loanId);
        loan.state = CollarVaultShared.LoanState.CLOSED;
        emit LoanSettled(loanId, outcome, settlementAmount);
        emit LoanClosed(loanId);
    }

    function convertToVariable(uint256 loanId, bytes32 lzGuid) external {
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        CollarVaultShared.Loan storage loan = $.loans[loanId];
        if (loan.state != CollarVaultShared.LoanState.ACTIVE_ZERO_COST) {
            revert CV_InvalidState();
        }
        if (block.timestamp < loan.maturity) {
            revert CV_InvalidState();
        }

        CollarLZMessages.Message memory lzMessage = _consumeLZMessage(lzGuid);
        $.lzMessenger
            .validateCollateralReturned(
                lzMessage, loanId, loan.collateralAsset, loan.collateralAmount, address(this), $.deriveSubaccountId
            );
        _markReadyForVariable(loanId, lzMessage.amount);
    }

    function tryConvertReadyLoan(uint256 loanId) external returns (bool converted) {
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        CollarVaultShared.Loan storage loan = $.loans[loanId];
        if (loan.state != CollarVaultShared.LoanState.READY_FOR_VARIABLE) {
            revert CV_InvalidState();
        }
        converted = _convertToVariableIfLiquid(loanId);
    }

    function _markReadyForVariable(uint256 loanId, uint256 collateralAmount) internal {
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        CollarVaultShared.Loan storage loan = $.loans[loanId];
        if (collateralAmount != loan.collateralAmount) {
            revert CV_InvalidInput();
        }
        loan.state = CollarVaultShared.LoanState.READY_FOR_VARIABLE;
        emit LoanReadyForVariable(loanId, loan.principal + loan.interestOwed);
    }

    function _convertToVariableIfLiquid(uint256 loanId) internal returns (bool converted) {
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        CollarVaultShared.Loan storage loan = $.loans[loanId];
        uint256 totalDue = loan.principal + loan.interestOwed;
        if ($.lendingAdapter.availableLiquidity(address($.usdc)) < totalDue) {
            return false;
        }

        _releaseCommittedPrincipal(loan.principal);
        _releaseReserve(loanId);
        IERC20(loan.collateralAsset).safeIncreaseAllowance(address($.lendingAdapter), loan.collateralAmount);
        $.lendingAdapter.depositCollateral(loan.collateralAsset, loan.collateralAmount, address(this));
        $.lendingAdapter.borrow(address($.usdc), totalDue, address(this), address(this));
        $.usdc.safeIncreaseAllowance(address($.liquidityVault), loan.principal);
        $.liquidityVault.repay(loan.principal);
        if (loan.interestOwed > 0) {
            $.usdc.safeTransfer(address($.liquidityVault), loan.interestOwed);
        }
        loan.state = CollarVaultShared.LoanState.ACTIVE_VARIABLE;
        loan.variableDebt = totalDue;
        emit LoanConverted(loanId, totalDue);
        return true;
    }

    function repayVariableLoan(uint256 loanId, uint256 amount) external returns (uint256 repaid, bool closed) {
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        CollarVaultShared.Loan storage loan = $.loans[loanId];
        if (loan.state != CollarVaultShared.LoanState.ACTIVE_VARIABLE) {
            revert CV_InvalidState();
        }
        uint256 debt = loan.variableDebt;
        repaid = amount > debt ? debt : amount;

        $.usdc.safeIncreaseAllowance(address($.lendingAdapter), repaid);
        $.lendingAdapter.repay(address($.usdc), repaid, address(this));

        uint256 remaining = debt - repaid;
        loan.variableDebt = remaining;
        if (remaining == 0) {
            $.lendingAdapter
                .withdrawCollateral(loan.collateralAsset, loan.collateralAmount, address(this), loan.borrower);
            loan.state = CollarVaultShared.LoanState.CLOSED;
            emit LoanClosed(loanId);
            closed = true;
        }
    }

    function _releaseCommittedPrincipal(uint256 amount) internal {
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        if (amount == 0) {
            return;
        }
        $.totalCommittedPrincipal -= amount;
    }

    function _releaseReserve(uint256 loanId) internal {
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        try $.liquidityVault.release(loanId) {} catch {}
    }

    function _consumeLZMessage(bytes32 guid) internal returns (CollarLZMessages.Message memory message) {
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        if ($.lzMessageConsumed[guid]) {
            revert CV_InvalidState();
        }
        message = $.lzMessenger.receivedMessage(guid);
        if (message.loanId == 0) {
            revert CV_InvalidState();
        }
        $.lzMessageConsumed[guid] = true;
    }
}
