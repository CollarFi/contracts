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
import {ICollarVaultSettleModule} from "../interfaces/ICollarVaultSettleModule.sol";

contract CollarVaultSettleModule is ICollarVaultSettleModule {
    using SafeERC20 for IERC20;

    uint256 public constant MAX_BPS = 10_000;

    enum LoanState {
        NONE,
        ACTIVE_ZERO_COST,
        CLOSED
    }

    enum SettlementOutcome {
        PutITM,
        Neutral,
        CallITM
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

    error CV_InvalidLoanState();
    error CV_NotMatured();
    error CV_InvalidAmount();

    event LoanSettled(uint256 indexed loanId, SettlementOutcome outcome, uint256 settlementAmount);
    event SettlementShortfall(uint256 indexed loanId, uint256 shortfall);
    event LoanConverted(uint256 indexed loanId, uint256 variableDebt);
    event LoanClosed(uint256 indexed loanId);

    function settleLoan(uint256 loanId, uint8 outcomeRaw, bytes32 lzGuid) external {
        SettlementOutcome outcome = SettlementOutcome(outcomeRaw);
        CollarVaultStorage storage $ = _getCollarVaultStorage();
        Loan storage loan = $.loans[loanId];
        if (loan.state != LoanState.ACTIVE_ZERO_COST) {
            revert CV_InvalidLoanState();
        }
        if (block.timestamp < loan.maturity) {
            revert CV_NotMatured();
        }

        CollarLZMessages.Message memory lzMessage = _consumeLZMessage(lzGuid);
        uint256 settlementAmount = 0;

        if (outcome == SettlementOutcome.Neutral) {
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
            if (outcome == SettlementOutcome.PutITM) {
                uint256 treasuryCut = Math.mulDiv(excess, $.treasuryBps, MAX_BPS);
                uint256 vaultCut = excess - treasuryCut;
                if (treasuryCut > 0) {
                    $.usdc.safeTransfer($.treasury, treasuryCut);
                }
                if (vaultCut > 0) {
                    $.usdc.safeTransfer(address($.liquidityVault), vaultCut);
                }
            } else if (outcome == SettlementOutcome.CallITM) {
                $.usdc.safeTransfer(loan.borrower, excess);
            }
        }

        _releaseCommittedPrincipal(loan.principal);
        loan.state = LoanState.CLOSED;
        emit LoanSettled(loanId, outcome, settlementAmount);
        emit LoanClosed(loanId);
    }

    function _convertToVariable(uint256 loanId, uint256 collateralAmount) internal {
        CollarVaultStorage storage $ = _getCollarVaultStorage();
        Loan storage loan = $.loans[loanId];
        if (collateralAmount != loan.collateralAmount) {
            revert CV_InvalidAmount();
        }
        _releaseCommittedPrincipal(loan.principal);
        IERC20(loan.collateralAsset).safeIncreaseAllowance(address($.eulerAdapter), collateralAmount);
        $.eulerAdapter.depositCollateral(loan.collateralAsset, collateralAmount, loan.borrower);
        $.eulerAdapter.borrow(address($.usdc), loan.principal, loan.borrower, address(this));
        $.usdc.safeIncreaseAllowance(address($.liquidityVault), loan.principal);
        $.liquidityVault.repay(loan.principal);
        loan.state = LoanState.CLOSED;
        loan.variableDebt = 0;
        emit LoanConverted(loanId, loan.principal);
        emit LoanClosed(loanId);
    }

    function _releaseCommittedPrincipal(uint256 amount) internal {
        CollarVaultStorage storage $ = _getCollarVaultStorage();
        if (amount == 0) {
            return;
        }
        $.totalCommittedPrincipal -= amount;
    }

    function _consumeLZMessage(bytes32 guid) internal returns (CollarLZMessages.Message memory message) {
        CollarVaultStorage storage $ = _getCollarVaultStorage();
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
