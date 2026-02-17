// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";

import {CollarVaultShared} from "./CollarVaultShared.sol";
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
        uint256 newCallStrike,
        uint256 newPutStrike
    ) external {
        CollarVaultShared.CollarVaultStorage storage $ = CollarVaultShared.getStorage();
        CollarVaultShared.Loan storage loan = $.loans[loanId];
        if (loan.state != CollarVaultShared.LoanState.ACTIVE_ZERO_COST) revert CV_InvalidState();
        if (block.timestamp >= loan.maturity) revert CV_InvalidState();
        if (mandate.loanId != loanId || mandate.borrower != loan.borrower) revert CV_InvalidMessage();
        if (mandate.deadline < block.timestamp || mandate.newMaturity <= block.timestamp) revert CV_InvalidState();
        if (mandate.newMaturity <= loan.maturity) revert CV_InvalidInput();
        if (newCallStrike < mandate.minCallStrike || newPutStrike > mandate.maxPutStrike) revert CV_InvalidInput();
        if (mandate.maxInterestApr < $.originationFeeApr) revert CV_InvalidInput();

        bytes32 mandateHash = ICollateralVaultRolloverHash(address(this)).hashRolloverMandate(mandate);
        if ($.usedRolloverMandates[mandateHash]) revert CV_InvalidMessage();
        address signer = ECDSA.recover(mandateHash, mandateSig);
        if (signer != loan.borrower) revert CV_Unauthorized();
        $.usedRolloverMandates[mandateHash] = true;

        uint256 oldInterest = loan.interestOwed;
        uint256 accrued = _quoteInterest(loan.principal, loan.interestApr, loan.startTime, block.timestamp);
        uint256 rolledInterest = oldInterest > accrued ? oldInterest - accrued : 0;
        uint256 newInterest = _quoteInterest(loan.principal, $.originationFeeApr, block.timestamp, mandate.newMaturity);

        uint256 oldMaturity = loan.maturity;
        uint256 oldCallStrike = loan.callStrike;
        uint256 oldPutStrike = loan.putStrike;

        loan.maturity = mandate.newMaturity;
        loan.callStrike = newCallStrike;
        loan.putStrike = newPutStrike;
        loan.startTime = block.timestamp;
        loan.interestApr = $.originationFeeApr;
        loan.interestOwed = rolledInterest + newInterest;

        emit LoanRolledOver(
            loanId,
            oldMaturity,
            mandate.newMaturity,
            oldInterest,
            loan.interestOwed,
            oldCallStrike,
            newCallStrike,
            oldPutStrike,
            newPutStrike,
            $.originationFeeApr
        );
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
}
