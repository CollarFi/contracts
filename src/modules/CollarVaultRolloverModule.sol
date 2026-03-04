// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";

import {CollarVaultShared} from "./CollarVaultShared.sol";
import {CollarLZMessages} from "../bridge/CollarLZMessages.sol";
import {ICollarVaultRolloverModule} from "../interfaces/ICollarVaultRolloverModule.sol";

interface ICollateralVaultRolloverHash {
    function hashRolloverMandate(CollarVaultShared.RolloverMandate memory mandate) external view returns (bytes32);
}

contract CollarVaultRolloverModule is ICollarVaultRolloverModule {
    error CV_InvalidState();
    error CV_InvalidInput();
    error CV_InvalidMessage();
    error CV_InvalidConfig();
    error CV_Unauthorized();
    error CV_InsufficientValue();

    event LoanRolledOver(
        uint256 indexed loanId,
        uint256 oldMaturity,
        uint256 newMaturity,
        uint256 oldInterestOwed,
        uint256 newInterestOwed,
        uint256 oldCallStrike,
        uint256 newCallStrike,
        uint256 oldPutStrike,
        uint256 newPutStrike,
        uint256 interestApr
    );

    event RolloverFinalizeAnomaly(
        uint256 indexed loanId,
        bytes32 indexed confirmationGuid,
        uint256 anomalyFlags,
        uint256 callStrike,
        uint256 putStrike,
        uint256 interestApr,
        int256 realizedC
    );

    function executeRollover(
        uint256 loanId,
        CollarVaultShared.RolloverMandate calldata mandate,
        bytes calldata mandateSig,
        uint256,
        uint256
    ) external payable returns (bytes32 guid) {
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        CollarVaultShared.Loan storage loan = $.loans[loanId];
        if (loan.state != CollarVaultShared.LoanState.ACTIVE_ZERO_COST) revert CV_InvalidState();
        if (block.timestamp >= loan.maturity) revert CV_InvalidState();
        if ($.pendingRollovers[loanId].mandateHash != bytes32(0)) revert CV_InvalidState();
        if (mandate.loanId != loanId || mandate.borrower != loan.borrower) revert CV_InvalidMessage();
        if (mandate.deadline < block.timestamp || mandate.newMaturity <= block.timestamp) revert CV_InvalidState();
        if (mandate.newMaturity <= loan.maturity) revert CV_InvalidInput();

        bytes32 mandateHash = ICollateralVaultRolloverHash(address(this)).hashRolloverMandate(mandate);
        if ($.usedRolloverMandates[mandateHash]) revert CV_InvalidMessage();
        address signer = ECDSA.recover(mandateHash, mandateSig);
        if (signer != loan.borrower) revert CV_Unauthorized();

        uint256 fixedInterest =
            _quoteInterest(loan.principal, $.originationFeeApr, block.timestamp, mandate.newMaturity);
        uint256 maxRollLtv = $.maxRollLtv;
        _enforceRollSafetyLtv(
            loan.collateralAsset,
            loan.collateralAmount,
            mandate.maxPutStrike,
            loan.principal + fixedInterest,
            maxRollLtv
        );

        $.usedRolloverMandates[mandateHash] = true;
        $.pendingRollovers[loanId] = CollarVaultShared.PendingRollover({
            mandateHash: mandateHash,
            borrower: mandate.borrower,
            newMaturity: mandate.newMaturity,
            minCallStrike: mandate.minCallStrike,
            maxPutStrike: mandate.maxPutStrike,
            minNetInterest: mandate.minNetInterest,
            fixedInterest: fixedInterest,
            maxRollLtv: maxRollLtv,
            deadline: mandate.deadline,
            requestedAt: block.timestamp
        });

        uint256 strikeScale = $.strikeScale[loan.collateralAsset];
        if (strikeScale == 0) {
            revert CV_InvalidConfig();
        }

        bytes memory rolloverData = abi.encode(
            mandateHash,
            mandate.borrower,
            mandate.newMaturity,
            mandate.minCallStrike,
            mandate.maxPutStrike,
            mandate.minNetInterest,
            fixedInterest,
            maxRollLtv,
            strikeScale,
            mandate.deadline,
            mandate.nonce
        );

        guid = $.lzMessenger.sendRolloverIntentAutoFee{value: msg.value}(
            loanId, loan.collateralAsset, loan.principal, address(this), $.deriveSubaccountId, rolloverData, msg.sender
        );
    }

    function finalizeRollover(uint256 loanId, bytes32 confirmationGuid) external {
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        if ($.lzMessageConsumed[confirmationGuid]) return;

        CollarVaultShared.Loan storage loan = $.loans[loanId];
        if (loan.state != CollarVaultShared.LoanState.ACTIVE_ZERO_COST) revert CV_InvalidState();

        CollarVaultShared.PendingRollover memory pending = $.pendingRollovers[loanId];
        if (pending.mandateHash == bytes32(0)) revert CV_NotFound();

        // Read + validate authenticated confirmation message.
        CollarLZMessages.Message memory lzMessage = $.lzMessenger.receivedMessage(confirmationGuid);
        if (lzMessage.loanId == 0) revert CV_InvalidMessage();
        (uint256 callStrike, uint256 putStrike, uint256 interestApr, int256 realizedC) = $.lzMessenger
            .validateRolloverConfirmed(
                lzMessage,
                loanId,
                address(this),
                $.deriveSubaccountId,
                pending.mandateHash,
                pending.borrower,
                pending.newMaturity
            );

        uint256 oldInterest = loan.interestOwed;
        uint256 accrued = _quoteInterest(loan.principal, loan.interestApr, loan.startTime, block.timestamp);
        uint256 rolledInterest = oldInterest > accrued ? oldInterest - accrued : 0;
        uint256 newInterest = _quoteInterest(loan.principal, interestApr, block.timestamp, pending.newMaturity);

        uint256 anomalyFlags;
        if (
            (pending.minCallStrike != 0 && callStrike < pending.minCallStrike)
                || (pending.maxPutStrike != 0 && putStrike > pending.maxPutStrike)
        ) {
            anomalyFlags |= 1;
        }
        if (interestApr < $.originationFeeApr) anomalyFlags |= 2;

        int256 totalEconomics = int256(newInterest) + realizedC;
        if (totalEconomics < int256(pending.minNetInterest) || realizedC < 0) anomalyFlags |= 4;
        if (_isRollSafetyLtvViolated(
                loan.collateralAsset, loan.collateralAmount, putStrike, loan.principal + newInterest, pending.maxRollLtv
            )) {
            anomalyFlags |= 16;
        }

        uint256 oldMaturity = loan.maturity;
        uint256 oldCallStrike = loan.callStrike;
        uint256 oldPutStrike = loan.putStrike;

        loan.maturity = pending.newMaturity;
        loan.callStrike = callStrike;
        loan.putStrike = putStrike;
        loan.startTime = block.timestamp;
        loan.interestApr = interestApr;
        loan.interestOwed = rolledInterest + newInterest;

        emit LoanRolledOver(
            loanId,
            oldMaturity,
            pending.newMaturity,
            oldInterest,
            loan.interestOwed,
            oldCallStrike,
            callStrike,
            oldPutStrike,
            putStrike,
            interestApr
        );
        if (anomalyFlags != 0) {
            emit RolloverFinalizeAnomaly(
                loanId, confirmationGuid, anomalyFlags, callStrike, putStrike, interestApr, realizedC
            );
        }

        $.lzMessageConsumed[confirmationGuid] = true;
        delete $.pendingRollovers[loanId];
    }

    function _quoteInterest(uint256 principal, uint256 apr, uint256 start, uint256 end)
        internal
        pure
        returns (uint256)
    {
        if (apr == 0 || end <= start) return 0;
        uint256 annualFee = (principal * apr) / 1e18;
        return (annualFee * (end - start)) / CollarVaultShared.YEAR;
    }

    function _enforceRollSafetyLtv(
        address collateralAsset,
        uint256 collateralAmount,
        uint256 putStrike,
        uint256 debtAmount,
        uint256 maxRollLtv
    ) internal view {
        if (_isRollSafetyLtvViolated(collateralAsset, collateralAmount, putStrike, debtAmount, maxRollLtv)) {
            revert CV_InsufficientValue();
        }
    }

    function _isRollSafetyLtvViolated(
        address collateralAsset,
        uint256 collateralAmount,
        uint256 putStrike,
        uint256 debtAmount,
        uint256 maxRollLtv
    ) internal view returns (bool) {
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        if (maxRollLtv == 0 || maxRollLtv > 1e18) {
            return true;
        }

        uint256 scale = $.strikeScale[collateralAsset];
        if (scale == 0 || putStrike == 0) {
            return true;
        }

        uint256 putFloorValue = Math.mulDiv(collateralAmount, putStrike, scale);
        uint256 maxDebt = Math.mulDiv(putFloorValue, maxRollLtv, 1e18);
        return debtAmount > maxDebt;
    }
    error CV_NotFound();
}
