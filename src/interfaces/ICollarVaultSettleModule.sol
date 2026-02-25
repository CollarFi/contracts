// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface ICollarVaultSettleModule {
    function settleLoan(uint256 loanId, uint8 outcome, bytes32 lzGuid) external;
    function tryConvertReadyLoan(uint256 loanId) external returns (bool converted);
    function repayVariableLoan(uint256 loanId, uint256 amount) external returns (uint256 repaid, bool closed);
    function withdrawVariableCollateral(uint256 loanId, uint256 amount)
        external
        returns (uint256 withdrawn, bool closed);
}
