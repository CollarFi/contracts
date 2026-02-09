// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IBridgeAdapter {
    function messageId() external view returns (bytes32);

    function estimateFee() external view returns (uint256);

    function bridge(address receiver, uint256 amount) external payable;
}
