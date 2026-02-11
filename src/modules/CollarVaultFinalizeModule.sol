// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

import {CollarLZMessages} from "../bridge/CollarLZMessages.sol";
import {ICollarVaultFinalizeModule} from "../interfaces/ICollarVaultFinalizeModule.sol";
import {CollarVaultShared} from "./CollarVaultShared.sol";

contract CollarVaultFinalizeModule is ICollarVaultFinalizeModule {
    using SafeERC20 for IERC20;

    error CV_MandateExpired();
    error CV_NotAuthorized();
    error CV_LZMessageMismatch();
    error CV_PendingDepositNotFound();
    error CV_MandateNotFound();

    event LoanCreated(
        uint256 indexed loanId,
        address indexed borrower,
        address indexed collateralAsset,
        uint256 collateralAmount,
        uint256 maturity,
        uint256 putStrike,
        uint256 callStrike,
        uint256 principal,
        uint256 subaccountId
    );

    function finalizeLoan(uint256 loanId, bytes32 depositGuid, bytes32 tradeGuid)
        external
        returns (uint256 finalizedLoanId)
    {
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        CollarVaultShared.PendingDeposit memory pending = $.pendingDeposits[loanId];
        if (pending.borrower == address(0)) {
            revert CV_PendingDepositNotFound();
        }

        CollarVaultShared.Mandate memory mandate = $.mandates[loanId];
        if (mandate.borrower == address(0)) {
            revert CV_MandateNotFound();
        }
        if (mandate.borrower != pending.borrower) {
            revert CV_NotAuthorized();
        }
        if (block.timestamp > mandate.deadline) {
            revert CV_MandateExpired();
        }

        CollarLZMessages.Message memory depositMessage = _loadLZMessage(depositGuid);
        CollarLZMessages.Message memory tradeMessage = _loadLZMessage(tradeGuid);

        finalizedLoanId = $.lzMessenger
            .validateDepositConfirmed(
                depositMessage,
                pending.borrower,
                mandate.borrower,
                pending.collateralAsset,
                pending.collateralAmount,
                address(this),
                $.deriveSubaccountId
            );

        (uint256 callStrike, uint256 putStrike) = $.lzMessenger
            .validateTradeConfirmedForFinalize(
                tradeMessage,
                finalizedLoanId,
                address(this),
                $.deriveSubaccountId,
                mandate.minCallStrike,
                mandate.maxPutStrike,
                mandate.maturity
            );

        $.lzMessenger
            .validateOriginationFee(
                tradeMessage, _quoteOriginationFee(pending.borrowAmount, pending.maturity), address($.usdc)
            );

        $.tradeConfirmed[finalizedLoanId] = true;
        $.collateralActivated[finalizedLoanId] = true;
        $.returnRequested[finalizedLoanId] = false;
        $.lzMessageConsumed[depositGuid] = true;
        $.lzMessageConsumed[tradeGuid] = true;

        delete $.pendingDeposits[finalizedLoanId];
        delete $.mandates[finalizedLoanId];

        $.loans[finalizedLoanId] = CollarVaultShared.Loan({
            borrower: mandate.borrower,
            collateralAsset: pending.collateralAsset,
            collateralAmount: pending.collateralAmount,
            maturity: pending.maturity,
            putStrike: putStrike,
            callStrike: callStrike,
            principal: pending.borrowAmount,
            subaccountId: $.deriveSubaccountId,
            state: CollarVaultShared.LoanState.ACTIVE_ZERO_COST,
            startTime: block.timestamp,
            originationFeeApr: $.originationFeeApr,
            variableDebt: 0
        });

        uint256 feeAmount = _quoteOriginationFee(pending.borrowAmount, pending.maturity);
        if (feeAmount > 0) {
            uint256 treasuryCut = Math.mulDiv(feeAmount, $.treasuryBps, CollarVaultShared.MAX_BPS);
            uint256 vaultCut = feeAmount - treasuryCut;
            if (treasuryCut > 0) {
                $.usdc.safeTransfer($.treasury, treasuryCut);
            }
            if (vaultCut > 0) {
                $.usdc.safeTransfer(address($.liquidityVault), vaultCut);
            }
        }

        $.liquidityVault.borrow(pending.borrowAmount);
        $.usdc.safeTransfer(mandate.borrower, pending.borrowAmount);

        emit LoanCreated(
            finalizedLoanId,
            mandate.borrower,
            pending.collateralAsset,
            pending.collateralAmount,
            pending.maturity,
            putStrike,
            callStrike,
            pending.borrowAmount,
            $.deriveSubaccountId
        );
    }

    function _quoteOriginationFee(uint256 borrowAmount, uint256 maturity) internal view returns (uint256) {
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        if ($.originationFeeApr == 0) {
            return 0;
        }
        if (maturity <= block.timestamp) {
            return 0;
        }
        uint256 duration = maturity - block.timestamp;
        uint256 annualFee = Math.mulDiv(borrowAmount, $.originationFeeApr, 1e18);
        return Math.mulDiv(annualFee, duration, CollarVaultShared.YEAR);
    }

    function _loadLZMessage(bytes32 guid) internal view returns (CollarLZMessages.Message memory message) {
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        if ($.lzMessageConsumed[guid]) {
            revert CV_LZMessageMismatch();
        }

        message = $.lzMessenger.receivedMessage(guid);
        if (message.loanId == 0) {
            revert CV_LZMessageMismatch();
        }
    }
}
