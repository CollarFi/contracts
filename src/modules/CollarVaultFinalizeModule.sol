// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

import {IEulerAdapter} from "../interfaces/IEulerAdapter.sol";
import {IBridgeAdapter} from "../interfaces/IBridgeAdapter.sol";
import {CollarLZMessages} from "../bridge/CollarLZMessages.sol";
import {ICollarVaultMessenger} from "../interfaces/ICollarVaultMessenger.sol";
import {ILiquidityVault} from "../interfaces/ILiquidityVault.sol";
import {ICollarVaultFinalizeModule} from "../interfaces/ICollarVaultFinalizeModule.sol";

contract CollarVaultFinalizeModule is ICollarVaultFinalizeModule {
    using SafeERC20 for IERC20;

    uint256 public constant YEAR = 365 days;
    uint256 public constant MAX_BPS = 10_000;

    enum LoanState {
        NONE,
        ACTIVE_ZERO_COST,
        CLOSED
    }

    struct Loan {
        address borrower;
        address collateralAsset;
        uint256 collateralAmount;
        uint256 maturity;
        uint256 putStrike;
        uint256 callStrike;
        uint256 principal;
        uint256 subaccountId;
        LoanState state;
        uint256 startTime;
        uint256 originationFeeApr;
        uint256 variableDebt;
    }

    struct PendingDeposit {
        address borrower;
        address collateralAsset;
        uint256 collateralAmount;
        uint256 maturity;
        uint256 putStrike;
        uint256 borrowAmount;
    }

    struct SocketBridgeConfig {
        IBridgeAdapter adapter;
    }

    struct Mandate {
        address borrower;
        address collateralAsset;
        uint256 collateralAmount;
        uint64 maturity;
        uint64 deadline;
        uint256 borrowAmount;
        uint256 minCallStrike;
        uint256 maxPutStrike;
        bool sentToL2;
    }

    struct CollarVaultStorage {
        ILiquidityVault liquidityVault;
        IERC20 usdc;
        mapping(address => SocketBridgeConfig) socketBridgeConfigs;
        IEulerAdapter eulerAdapter;
        address l2Recipient;
        address treasury;
        uint256 treasuryBps;
        uint256 originationFeeApr;
        uint256 maxTotalPrincipal;
        uint256 totalCommittedPrincipal;
        uint256 deriveSubaccountId;
        uint256 nextLoanId;
        mapping(uint256 => Loan) loans;
        mapping(uint256 => PendingDeposit) pendingDeposits;
        mapping(uint256 => Mandate) mandates;
        mapping(bytes32 => bool) usedBaselineRfqs;
        mapping(uint256 => bool) tradeConfirmed;
        mapping(uint256 => bool) collateralActivated;
        mapping(uint256 => bool) returnRequested;
        mapping(address => bool) collateralAllowed;
        mapping(address => uint256) strikeScale;
        ICollarVaultMessenger lzMessenger;
        mapping(bytes32 => bool) lzMessageConsumed;
        address finalizeModule;
        address settleModule;
    }

    bytes32 private constant CollarVaultStorageLocation =
        0x44df88ba167ccae38168bf10e759327f11cfe194bbb6b4faf1c1a932243f4100;

    function _getCollarVaultStorage() private pure returns (CollarVaultStorage storage $) {
        assembly {
            $.slot := CollarVaultStorageLocation
        }
    }

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
        CollarVaultStorage storage $ = _getCollarVaultStorage();
        PendingDeposit memory pending = $.pendingDeposits[loanId];
        if (pending.borrower == address(0)) {
            revert CV_PendingDepositNotFound();
        }

        Mandate memory mandate = $.mandates[loanId];
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

        $.loans[finalizedLoanId] = Loan({
            borrower: mandate.borrower,
            collateralAsset: pending.collateralAsset,
            collateralAmount: pending.collateralAmount,
            maturity: pending.maturity,
            putStrike: putStrike,
            callStrike: callStrike,
            principal: pending.borrowAmount,
            subaccountId: $.deriveSubaccountId,
            state: LoanState.ACTIVE_ZERO_COST,
            startTime: block.timestamp,
            originationFeeApr: $.originationFeeApr,
            variableDebt: 0
        });

        uint256 feeAmount = _quoteOriginationFee(pending.borrowAmount, pending.maturity);
        if (feeAmount > 0) {
            uint256 treasuryCut = Math.mulDiv(feeAmount, $.treasuryBps, MAX_BPS);
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
        CollarVaultStorage storage $ = _getCollarVaultStorage();
        if ($.originationFeeApr == 0) {
            return 0;
        }
        if (maturity <= block.timestamp) {
            return 0;
        }
        uint256 duration = maturity - block.timestamp;
        uint256 annualFee = Math.mulDiv(borrowAmount, $.originationFeeApr, 1e18);
        return Math.mulDiv(annualFee, duration, YEAR);
    }

    function _loadLZMessage(bytes32 guid) internal view returns (CollarLZMessages.Message memory message) {
        CollarVaultStorage storage $ = _getCollarVaultStorage();
        if ($.lzMessageConsumed[guid]) {
            revert CV_LZMessageMismatch();
        }

        message = $.lzMessenger.receivedMessage(guid);
        if (message.loanId == 0) {
            revert CV_LZMessageMismatch();
        }
    }
}
