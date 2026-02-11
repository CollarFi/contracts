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

    error CV_InvalidLoanState();
    error CV_NotMatured();
    error CV_InvalidAmount();

    event LoanSettled(uint256 indexed loanId, CollarVaultShared.SettlementOutcome outcome, uint256 settlementAmount);
    event SettlementShortfall(uint256 indexed loanId, uint256 shortfall);
    event LoanConverted(uint256 indexed loanId, uint256 variableDebt);
    event LoanClosed(uint256 indexed loanId);

    function settleLoan(uint256 loanId, uint8 outcomeRaw, bytes32 lzGuid) external {
        CollarVaultShared.SettlementOutcome outcome = CollarVaultShared.SettlementOutcome(outcomeRaw);
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        CollarVaultShared.Loan storage loan = $.loans[loanId];
        if (loan.state != CollarVaultShared.LoanState.ACTIVE_ZERO_COST) {
            revert CV_InvalidLoanState();
        }
        if (block.timestamp < loan.maturity) {
            revert CV_NotMatured();
        }

        CollarLZMessages.Message memory lzMessage = _consumeLZMessage(lzGuid);
        uint256 settlementAmount = 0;

        if (outcome == CollarVaultShared.SettlementOutcome.Neutral) {
            $.lzMessenger
                .validateCollateralReturned(
                    lzMessage, loanId, loan.collateralAsset, loan.collateralAmount, address(this), $.deriveSubaccountId
                );
            _convertToVariable(loanId, lzMessage.amount);
            emit LoanSettled(loanId, outcome, settlementAmount);
            return;
        }

        settlementAmount = $.lzMessenger.validateSettlementReport(lzMessage, loanId, address($.usdc), address(this));

        uint256 shortfall = 0;
        if (settlementAmount < loan.principal) {
            shortfall = loan.principal - settlementAmount;
        }

        uint256 repayAmount = settlementAmount > loan.principal ? loan.principal : settlementAmount;
        if (repayAmount > 0) {
            $.usdc.safeIncreaseAllowance(address($.liquidityVault), repayAmount);
            $.liquidityVault.repay(repayAmount);
        }
        if (shortfall > 0) {
            $.liquidityVault.writeOff(shortfall);
            emit SettlementShortfall(loanId, shortfall);
        }

        uint256 excess = settlementAmount > loan.principal ? settlementAmount - loan.principal : 0;

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
        loan.state = CollarVaultShared.LoanState.CLOSED;
        emit LoanSettled(loanId, outcome, settlementAmount);
        emit LoanClosed(loanId);
    }

    function _convertToVariable(uint256 loanId, uint256 collateralAmount) internal {
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        CollarVaultShared.Loan storage loan = $.loans[loanId];
        if (collateralAmount != loan.collateralAmount) {
            revert CV_InvalidAmount();
        }
        _releaseCommittedPrincipal(loan.principal);
        IERC20(loan.collateralAsset).safeIncreaseAllowance(address($.eulerAdapter), collateralAmount);
        $.eulerAdapter.depositCollateral(loan.collateralAsset, collateralAmount, loan.borrower);
        $.eulerAdapter.borrow(address($.usdc), loan.principal, loan.borrower, address(this));
        $.usdc.safeIncreaseAllowance(address($.liquidityVault), loan.principal);
        $.liquidityVault.repay(loan.principal);
        loan.state = CollarVaultShared.LoanState.CLOSED;
        loan.variableDebt = 0;
        emit LoanConverted(loanId, loan.principal);
        emit LoanClosed(loanId);
    }

    function _releaseCommittedPrincipal(uint256 amount) internal {
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        if (amount == 0) {
            return;
        }
        $.totalCommittedPrincipal -= amount;
    }

    function _consumeLZMessage(bytes32 guid) internal returns (CollarLZMessages.Message memory message) {
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        if ($.lzMessageConsumed[guid]) {
            revert CV_InvalidLoanState();
        }
        message = $.lzMessenger.receivedMessage(guid);
        if (message.loanId == 0) {
            revert CV_InvalidLoanState();
        }
        $.lzMessageConsumed[guid] = true;
    }
}
