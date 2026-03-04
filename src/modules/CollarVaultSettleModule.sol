// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {IAccessControl} from "@openzeppelin/contracts/access/IAccessControl.sol";

import {CollarLZMessages} from "../bridge/CollarLZMessages.sol";
import {ICollarVaultSettleModule} from "../interfaces/ICollarVaultSettleModule.sol";
import {IVariableLoanPosition} from "../interfaces/IVariableLoanPosition.sol";
import {CollarVaultShared} from "./CollarVaultShared.sol";
import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";

contract CollarVaultSettleModule is ICollarVaultSettleModule {
    using SafeERC20 for IERC20;
    using Clones for address;
    bytes32 internal constant KEEPER_ROLE = keccak256("KEEPER_ROLE");

    error CV_InvalidState();
    error CV_InvalidInput();
    error CV_InvalidConfig();
    error CV_Unauthorized();
    error CV_InsufficientValue();

    event LoanSettled(uint256 indexed loanId, CollarVaultShared.SettlementOutcome outcome, uint256 settlementAmount);
    event SettlementShortfall(uint256 indexed loanId, uint256 shortfall);
    event LoanReadyForVariable(uint256 indexed loanId, uint256 requiredDebt);
    event LoanConverted(uint256 indexed loanId, uint256 variableDebt);
    event ReadyLoanSettledByRepay(
        uint256 indexed loanId,
        address indexed payer,
        uint256 repaid,
        uint256 payerCollateralAmount,
        uint256 borrowerCollateralAmount
    );
    event VariableCollateralWithdrawn(uint256 indexed loanId, uint256 amount);
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
            _consumeNeutralCollateralReturned(loanId, lzMessage);
            emit LoanSettled(loanId, outcome, settlementAmount);
            return;
        }

        settlementAmount = $.lzMessenger.validateSettlementReport(lzMessage, loanId, address($.usdc), address(this));

        uint256 totalDue = loan.principal + loan.interestOwed;
        if (settlementAmount < totalDue) {
            revert CV_InsufficientValue();
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
        delete $.readyLoanSince[loanId];
        loan.state = CollarVaultShared.LoanState.CLOSED;
        emit LoanSettled(loanId, outcome, settlementAmount);
        emit LoanClosed(loanId);
    }

    function tryConvertReadyLoan(uint256 loanId) external returns (bool converted) {
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        CollarVaultShared.Loan storage loan = $.loans[loanId];
        if (loan.state != CollarVaultShared.LoanState.READY_FOR_VARIABLE) {
            revert CV_InvalidState();
        }
        converted = _convertToVariableIfLiquid(loanId);
    }

    function settleReadyLoanByRepay(uint256 loanId)
        external
        returns (uint256 repaid, uint256 callerCollateral, uint256 borrowerCollateral)
    {
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        CollarVaultShared.Loan storage loan = $.loans[loanId];
        if (loan.state != CollarVaultShared.LoanState.READY_FOR_VARIABLE) {
            revert CV_InvalidState();
        }

        uint256 readySince = $.readyLoanSince[loanId];
        if (readySince == 0 || $.readyLoanCloseGracePeriod == 0) {
            revert CV_InvalidConfig();
        }
        uint256 deadline = readySince + $.readyLoanCloseGracePeriod;

        repaid = loan.principal + loan.interestOwed;
        $.usdc.safeTransferFrom(msg.sender, address(this), repaid);

        if (msg.sender == loan.borrower) {
            if (block.timestamp > deadline) {
                revert CV_Unauthorized();
            }
            callerCollateral = loan.collateralAmount;
            borrowerCollateral = 0;
        } else {
            if (block.timestamp <= deadline || !IAccessControl(address(this)).hasRole(KEEPER_ROLE, msg.sender)) {
                revert CV_Unauthorized();
            }
            uint256 strikeScale = $.strikeScale[loan.collateralAsset];
            if (strikeScale == 0 || loan.putStrike == 0) {
                revert CV_InvalidConfig();
            }
            uint256 baseCollateral = Math.mulDiv(repaid, strikeScale, loan.putStrike, Math.Rounding.Ceil);
            callerCollateral = Math.mulDiv(
                baseCollateral,
                CollarVaultShared.MAX_BPS + $.readyLoanKeeperPenaltyBps,
                CollarVaultShared.MAX_BPS,
                Math.Rounding.Ceil
            );
            if (callerCollateral > loan.collateralAmount) {
                callerCollateral = loan.collateralAmount;
            }
            borrowerCollateral = loan.collateralAmount - callerCollateral;
        }

        _releaseCommittedPrincipal(loan.principal);
        delete $.readyLoanSince[loanId];
        delete $.variableLoanPositions[loanId];

        $.usdc.safeIncreaseAllowance(address($.liquidityVault), loan.principal);
        $.liquidityVault.repay(loan.principal);
        if (loan.interestOwed > 0) {
            $.usdc.safeTransfer(address($.liquidityVault), loan.interestOwed);
        }
        if (callerCollateral > 0) {
            IERC20(loan.collateralAsset).safeTransfer(msg.sender, callerCollateral);
        }
        if (borrowerCollateral > 0) {
            IERC20(loan.collateralAsset).safeTransfer(loan.borrower, borrowerCollateral);
        }

        loan.state = CollarVaultShared.LoanState.CLOSED;
        emit ReadyLoanSettledByRepay(loanId, msg.sender, repaid, callerCollateral, borrowerCollateral);
        emit LoanClosed(loanId);
    }

    function _consumeNeutralCollateralReturned(uint256 loanId, CollarLZMessages.Message memory lzMessage) internal {
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        CollarVaultShared.Loan storage loan = $.loans[loanId];
        $.lzMessenger
            .validateCollateralReturned(
                lzMessage, loanId, loan.collateralAsset, loan.collateralAmount, address(this), $.deriveSubaccountId
            );
        _markReadyForVariable(loanId, lzMessage.amount);
    }

    function _markReadyForVariable(uint256 loanId, uint256 collateralAmount) internal {
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        CollarVaultShared.Loan storage loan = $.loans[loanId];
        if (collateralAmount != loan.collateralAmount) {
            revert CV_InvalidInput();
        }
        loan.state = CollarVaultShared.LoanState.READY_FOR_VARIABLE;
        $.readyLoanSince[loanId] = block.timestamp;
        emit LoanReadyForVariable(loanId, loan.principal + loan.interestOwed);
    }

    function _convertToVariableIfLiquid(uint256 loanId) internal returns (bool converted) {
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        CollarVaultShared.Loan storage loan = $.loans[loanId];
        uint256 totalDue = loan.principal + loan.interestOwed;

        address position = $.variableLoanPositions[loanId];
        if (position == address(0)) {
            address impl = $.variableLoanPositionImplementation;
            if (impl == address(0)) revert CV_InvalidInput();
            position = impl.clone();
            IVariableLoanPosition(position)
                .initialize(
                    address(this), address($.lendingAdapter), loan.borrower, loan.collateralAsset, address($.usdc)
                );
            $.variableLoanPositions[loanId] = position;
        }

        if (IVariableLoanPosition(position).availableLiquidity() < totalDue) {
            return false;
        }

        _releaseCommittedPrincipal(loan.principal);

        IERC20(loan.collateralAsset).safeIncreaseAllowance(position, loan.collateralAmount);
        IVariableLoanPosition(position).open(loan.collateralAmount, totalDue, address(this), address(this));

        $.usdc.safeIncreaseAllowance(address($.liquidityVault), loan.principal);
        $.liquidityVault.repay(loan.principal);
        if (loan.interestOwed > 0) {
            $.usdc.safeTransfer(address($.liquidityVault), loan.interestOwed);
        }
        uint256 liveDebt = IVariableLoanPosition(position).currentDebt();
        uint256 liveCollateral = IVariableLoanPosition(position).currentCollateral();

        loan.state = CollarVaultShared.LoanState.ACTIVE_VARIABLE;
        loan.variableDebt = liveDebt;
        loan.collateralAmount = liveCollateral;
        delete $.readyLoanSince[loanId];
        emit LoanConverted(loanId, liveDebt);
        return true;
    }

    function repayVariableLoan(uint256 loanId, uint256 amount) external returns (uint256 repaid, bool closed) {
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        CollarVaultShared.Loan storage loan = $.loans[loanId];
        if (loan.state != CollarVaultShared.LoanState.ACTIVE_VARIABLE) {
            revert CV_InvalidState();
        }
        address position = $.variableLoanPositions[loanId];
        if (position == address(0)) revert CV_InvalidState();

        uint256 debt = IVariableLoanPosition(position).currentDebt();
        repaid = amount > debt ? debt : amount;

        $.usdc.safeIncreaseAllowance(position, repaid);
        IVariableLoanPosition(position).repay(repaid, address(this));

        uint256 remainingDebt = IVariableLoanPosition(position).currentDebt();
        uint256 remainingCollateral = IVariableLoanPosition(position).currentCollateral();
        loan.variableDebt = remainingDebt;
        loan.collateralAmount = remainingCollateral;
        if (remainingDebt == 0 && remainingCollateral == 0) {
            loan.state = CollarVaultShared.LoanState.CLOSED;
            emit LoanClosed(loanId);
            closed = true;
        }
    }

    function withdrawVariableCollateral(uint256 loanId, uint256 amount)
        external
        returns (uint256 withdrawn, bool closed)
    {
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        CollarVaultShared.Loan storage loan = $.loans[loanId];
        if (loan.state != CollarVaultShared.LoanState.ACTIVE_VARIABLE) {
            revert CV_InvalidState();
        }
        address position = $.variableLoanPositions[loanId];
        if (position == address(0)) revert CV_InvalidState();

        uint256 liveCollateralBefore = IVariableLoanPosition(position).currentCollateral();
        if (amount > liveCollateralBefore) {
            revert CV_InvalidInput();
        }

        IVariableLoanPosition(position).withdraw(amount, loan.borrower);

        uint256 liveDebt = IVariableLoanPosition(position).currentDebt();
        uint256 liveCollateralAfter = IVariableLoanPosition(position).currentCollateral();

        loan.variableDebt = liveDebt;
        loan.collateralAmount = liveCollateralAfter;
        withdrawn = liveCollateralBefore - liveCollateralAfter;
        emit VariableCollateralWithdrawn(loanId, withdrawn);

        if (liveDebt == 0 && liveCollateralAfter == 0) {
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
