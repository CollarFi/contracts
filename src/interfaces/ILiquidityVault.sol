// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface ILiquidityVault {
    function borrow(uint256 amount) external;
    function repay(uint256 amount) external;
    function writeOff(uint256 amount) external;
    function reserve(uint256 loanId, uint256 amount) external;
    function release(uint256 loanId) external;
    function consume(uint256 loanId, uint256 amount) external;
    function reservePrincipal(uint256 loanId, uint256 amount) external;
    function releasePrincipal(uint256 loanId) external;
    function borrowReserved(uint256 loanId, uint256 amount) external;
    function asset() external view returns (address);
}
