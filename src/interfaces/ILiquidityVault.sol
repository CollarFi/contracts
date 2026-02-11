// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface ILiquidityVault {
    function borrow(uint256 amount) external;
    function repay(uint256 amount) external;
    function writeOff(uint256 amount) external;
    function asset() external view returns (address);
}
