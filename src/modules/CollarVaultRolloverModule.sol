// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";

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
    error CV_Unauthorized();

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
        if (mandate.maxInterestApr < $.originationFeeApr) revert CV_InvalidInput();

        bytes32 mandateHash = ICollateralVaultRolloverHash(address(this)).hashRolloverMandate(mandate);
        if ($.usedRolloverMandates[mandateHash]) revert CV_InvalidMessage();
        address signer = ECDSA.recover(mandateHash, mandateSig);
        if (signer != loan.borrower) revert CV_Unauthorized();

        uint256 fixedInterest =
            _quoteInterest(loan.principal, $.originationFeeApr, block.timestamp, mandate.newMaturity);

        $.usedRolloverMandates[mandateHash] = true;
        $.pendingRollovers[loanId] = CollarVaultShared.PendingRollover({
            mandateHash: mandateHash,
            borrower: mandate.borrower,
            newMaturity: mandate.newMaturity,
            minCallStrike: mandate.minCallStrike,
            maxPutStrike: mandate.maxPutStrike,
            maxInterestApr: mandate.maxInterestApr,
            maxNegativeC: mandate.maxNegativeC,
            deadline: mandate.deadline,
            requestedAt: block.timestamp
        });

        bytes memory rolloverData = abi.encode(
            mandateHash,
            mandate.borrower,
            mandate.newMaturity,
            mandate.minCallStrike,
            mandate.maxPutStrike,
            mandate.maxInterestApr,
            fixedInterest,
            mandate.maxNegativeC,
            mandate.deadline,
            mandate.nonce
        );

        guid = $.lzMessenger.sendRolloverIntentAutoFee{value: msg.value}(
            loanId, loan.collateralAsset, loan.principal, address(this), $.deriveSubaccountId, rolloverData, msg.sender
        );
    }

    function finalizeRollover(uint256 loanId, bytes32 confirmationGuid) external {
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        CollarVaultShared.Loan storage loan = $.loans[loanId];
        if (loan.state != CollarVaultShared.LoanState.ACTIVE_ZERO_COST) revert CV_InvalidState();

        CollarVaultShared.PendingRollover memory pending = $.pendingRollovers[loanId];
        if (pending.mandateHash == bytes32(0)) revert CV_NotFound();

        if ($.lzMessageConsumed[confirmationGuid]) revert CV_InvalidMessage();

        // Read + consume confirmation message.
        {
            CollarLZMessages.Message memory lzMessage = $.lzMessenger.receivedMessage(confirmationGuid);
            if (lzMessage.loanId == 0) revert CV_InvalidMessage();
            (uint256 callStrike, uint256 putStrike, uint256 interestApr) = $.lzMessenger
                .validateRolloverConfirmed(
                    lzMessage,
                    loanId,
                    address(this),
                    $.deriveSubaccountId,
                    pending.mandateHash,
                    pending.borrower,
                    pending.newMaturity,
                    pending.minCallStrike,
                    pending.maxPutStrike,
                    pending.maxInterestApr
                );

            if (interestApr < $.originationFeeApr) revert CV_InvalidInput();

            uint256 oldInterest = loan.interestOwed;
            uint256 accrued = _quoteInterest(loan.principal, loan.interestApr, loan.startTime, block.timestamp);
            uint256 rolledInterest = oldInterest > accrued ? oldInterest - accrued : 0;
            uint256 newInterest = _quoteInterest(loan.principal, interestApr, block.timestamp, pending.newMaturity);

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

    error CV_NotFound();
}
