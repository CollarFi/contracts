// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface ICollarVaultFinalizeModule {
    function finalizeLoan(uint256 loanId, bytes32 depositGuid, bytes32 tradeGuid)
        external
        returns (uint256 finalizedLoanId);
}
