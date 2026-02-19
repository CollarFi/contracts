// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface ICollarVaultSettleModule {
    function settleLoan(uint256 loanId, uint8 outcome, bytes32 lzGuid) external;
    function convertToVariable(uint256 loanId, bytes32 lzGuid) external;
}
