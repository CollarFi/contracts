// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {CollarVaultShared} from "../modules/CollarVaultShared.sol";

interface ICollarVaultRolloverModule {
    function executeRollover(
        uint256 loanId,
        CollarVaultShared.RolloverMandate calldata mandate,
        bytes calldata mandateSig,
        uint256 newCallStrike,
        uint256 newPutStrike
    ) external payable returns (bytes32 guid);

    function finalizeRollover(uint256 loanId, bytes32 confirmationGuid) external;
}
