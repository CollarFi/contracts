// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {IAccessControl} from "@openzeppelin/contracts/access/IAccessControl.sol";

import {CollarLZMessages} from "../bridge/CollarLZMessages.sol";
import {ICollarVaultFinalizeModule} from "../interfaces/ICollarVaultFinalizeModule.sol";
import {CollarVaultShared} from "./CollarVaultShared.sol";

interface ICollarVaultBaselineRfqHash {
    function hashBaselineRfq(ICollarVaultFinalizeModule.BaselineRfq memory rfq) external view returns (bytes32);
}

contract CollarVaultFinalizeModule is ICollarVaultFinalizeModule {
    using SafeERC20 for IERC20;

    bytes32 internal constant RFQ_SIGNER_ROLE = keccak256("RFQ_SIGNER_ROLE");

    error CV_InvalidConfig();
    error CV_InvalidInput();
    error CV_InvalidState();
    error CV_Unauthorized();
    error CV_InvalidMessage();
    error CV_NotFound();
    error CV_InsufficientValue();

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

    event MandateAccepted(
        uint256 indexed loanId,
        address indexed borrower,
        uint64 maturity,
        uint256 borrowAmount,
        uint256 minCallStrike,
        uint256 maxPutStrike,
        uint256 minNetInterest,
        uint64 deadline,
        bytes32 lzGuid
    );

    function acceptMandate(uint256 loanId, BaselineRfq calldata rfq, bytes calldata rfqSig, uint64 deadline)
        external
        payable
        returns (bytes32 lzGuid)
    {
        return _acceptMandate(loanId, rfq, rfqSig, deadline, msg.value, false);
    }

    /// @notice Accepts a mandate with explicit borrower address (for internal use via delegatecall).
    /// @dev If rfq.loanId == 0, it is treated as a sentinel and replaced with the provided loanId.
    function acceptMandateInternal(
        uint256 loanId,
        address borrower,
        BaselineRfq calldata rfq,
        bytes calldata rfqSig,
        uint64 deadline,
        uint256 ethForLz
    ) external payable returns (bytes32 lzGuid) {
        // Silence unused variable warning - borrower is msg.sender in delegatecall
        borrower;
        return _acceptMandate(loanId, rfq, rfqSig, deadline, ethForLz, true);
    }

    function _acceptMandate(
        uint256 loanId,
        BaselineRfq calldata rfq,
        bytes calldata rfqSig,
        uint64 deadline,
        uint256 ethForLz,
        bool isInternal
    ) internal returns (bytes32 lzGuid) {
        address borrower = msg.sender;
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        CollarVaultShared.PendingDeposit memory pending = $.pendingDeposits[loanId];
        if (pending.borrower == address(0)) {
            revert CV_NotFound();
        }
        if (pending.borrower != borrower) {
            revert CV_Unauthorized();
        }

        if (address($.lzMessenger) == address(0) || $.deriveSubaccountId == 0) {
            revert CV_InvalidConfig();
        }
        if (deadline <= block.timestamp) {
            revert CV_InvalidState();
        }
        if (deadline > block.timestamp + $.maxMandateDuration) {
            revert CV_InvalidState();
        }

        CollarVaultShared.Mandate memory existing = $.mandates[loanId];
        bool hadMandate = existing.borrower != address(0);
        if (hadMandate && block.timestamp < existing.deadline) {
            revert CV_InvalidState();
        }

        // For external acceptMandate, RFQ must bind exact loanId.
        // For createDepositWithMandate (internal flow), rfq.loanId == 0 sentinel is allowed.
        if (isInternal) {
            if (rfq.loanId != 0 && rfq.loanId != loanId) {
                revert CV_InvalidMessage();
            }
        } else if (rfq.loanId != loanId) {
            revert CV_InvalidMessage();
        }
        // Verify RFQ borrower matches the actual borrower (msg.sender in both direct and delegatecall)
        if (rfq.borrower != address(0) && rfq.borrower != borrower) {
            revert CV_Unauthorized();
        }
        if (rfq.rfqExpiry < block.timestamp) {
            revert CV_InvalidState();
        }
        if (
            rfq.collateralAsset != pending.collateralAsset || rfq.collateralAmount != pending.collateralAmount
                || rfq.maturity != uint64(pending.maturity) || rfq.putStrike != pending.putStrike
                || rfq.borrowAmount != pending.borrowAmount
        ) {
            revert CV_InvalidMessage();
        }
        if (rfq.callStrike == 0) {
            revert CV_InvalidInput();
        }

        bytes32 rfqHash = ICollarVaultBaselineRfqHash(address(this)).hashBaselineRfq(rfq);
        if ($.usedBaselineRfqs[rfqHash]) {
            revert CV_InvalidMessage();
        }
        address signer = ECDSA.recover(rfqHash, rfqSig);
        if (!IAccessControl(address(this)).hasRole(RFQ_SIGNER_ROLE, signer)) {
            revert CV_Unauthorized();
        }
        $.usedBaselineRfqs[rfqHash] = true;

        if (!hadMandate) {
            _commitPrincipal(pending.borrowAmount);
            $.liquidityVault.reservePrincipal(loanId, pending.borrowAmount);
        } else if (existing.maxNegativeC > 0) {
            $.liquidityVault.release(loanId);
        }
        if (rfq.maxNegativeC > 0) {
            $.liquidityVault.reserve(loanId, rfq.maxNegativeC);
        }
        if (pending.maturity > type(uint64).max) {
            revert CV_InvalidInput();
        }

        uint256 minCallStrike = rfq.callStrike;
        uint256 maxPutStrike = rfq.putStrike;
        uint256 fixedInterest =
            _quoteInterest(pending.borrowAmount, $.originationFeeApr, block.timestamp, pending.maturity);

        $.mandates[loanId] = CollarVaultShared.Mandate({
            borrower: pending.borrower,
            collateralAsset: pending.collateralAsset,
            collateralAmount: pending.collateralAmount,
            maturity: uint64(pending.maturity),
            deadline: deadline,
            borrowAmount: pending.borrowAmount,
            minCallStrike: minCallStrike,
            maxPutStrike: maxPutStrike,
            minNetInterest: rfq.minNetInterest,
            fixedInterest: fixedInterest,
            maxNegativeC: rfq.maxNegativeC,
            sentToL2: true
        });

        lzGuid = _sendMandateCreated(
            loanId,
            pending,
            minCallStrike,
            maxPutStrike,
            rfq.minNetInterest,
            fixedInterest,
            rfq.maxNegativeC,
            deadline,
            ethForLz
        );

        emit MandateAccepted(
            loanId,
            pending.borrower,
            uint64(pending.maturity),
            pending.borrowAmount,
            minCallStrike,
            maxPutStrike,
            rfq.minNetInterest,
            deadline,
            lzGuid
        );
    }

    function finalizeLoan(uint256 loanId, bytes32 depositGuid, bytes32 tradeGuid)
        external
        returns (uint256 finalizedLoanId)
    {
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        CollarVaultShared.PendingDeposit memory pending = $.pendingDeposits[loanId];
        if (pending.borrower == address(0)) {
            revert CV_NotFound();
        }

        CollarVaultShared.Mandate memory mandate = $.mandates[loanId];
        if (mandate.borrower == address(0)) {
            revert CV_NotFound();
        }
        if (mandate.borrower != pending.borrower) {
            revert CV_Unauthorized();
        }
        if (block.timestamp > mandate.deadline) {
            revert CV_InvalidState();
        }
        // Borrower signs fixedInterest; no APR revalidation required at finalize.

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

        (uint256 callStrike, uint256 putStrike, int256 realizedC) = $.lzMessenger
            .validateTradeConfirmedForFinalize(
                tradeMessage,
                finalizedLoanId,
                address(this),
                $.deriveSubaccountId,
                mandate.minCallStrike,
                mandate.maxPutStrike,
                mandate.maturity
            );

        int256 totalEconomics = int256(mandate.fixedInterest) + realizedC;
        if (totalEconomics < int256(mandate.minNetInterest)) {
            revert CV_InsufficientValue();
        }

        uint256 realizedDeficit = totalEconomics < 0 ? uint256(-totalEconomics) : 0;
        if (realizedDeficit > mandate.maxNegativeC) {
            revert CV_InsufficientValue();
        }

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
            interestApr: $.originationFeeApr,
            interestOwed: mandate.fixedInterest,
            variableDebt: 0
        });

        $.liquidityVault.borrowReserved(finalizedLoanId, pending.borrowAmount);
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

    function _sendMandateCreated(
        uint256 loanId,
        CollarVaultShared.PendingDeposit memory pending,
        uint256 minCallStrike,
        uint256 maxPutStrike,
        uint256 minNetInterest,
        uint256 fixedInterest,
        uint256 maxNegativeC,
        uint64 deadline,
        uint256 ethForLz
    ) internal returns (bytes32 lzGuid) {
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        bytes memory mandateData = abi.encode(
            pending.borrower,
            minCallStrike,
            maxPutStrike,
            minNetInterest,
            fixedInterest,
            maxNegativeC,
            uint64(pending.maturity),
            deadline
        );

        lzGuid = $.lzMessenger.sendMandateCreatedAutoFee{value: ethForLz}(
            loanId,
            pending.collateralAsset,
            pending.borrowAmount,
            address(this),
            $.deriveSubaccountId,
            mandateData,
            msg.sender
        );
    }

    function _quoteInterest(uint256 principal, uint256 apr, uint256 start, uint256 end)
        internal
        pure
        returns (uint256)
    {
        if (apr == 0 || end <= start) {
            return 0;
        }
        uint256 annualFee = (principal * apr) / 1e18;
        return (annualFee * (end - start)) / CollarVaultShared.YEAR;
    }

    function _commitPrincipal(uint256 amount) internal {
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        if (amount == 0) {
            return;
        }
        uint256 cap = $.maxTotalPrincipal;
        if (cap != 0 && $.totalCommittedPrincipal + amount > cap) {
            revert CV_InvalidState();
        }
        $.totalCommittedPrincipal += amount;
    }

    function _loadLZMessage(bytes32 guid) internal view returns (CollarLZMessages.Message memory message) {
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        if ($.lzMessageConsumed[guid]) {
            revert CV_InvalidMessage();
        }

        message = $.lzMessenger.receivedMessage(guid);
        if (message.loanId == 0) {
            revert CV_InvalidMessage();
        }
    }
}
