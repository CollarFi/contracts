// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface ICowSettlement {
    function filledAmount(bytes calldata orderUid) external view returns (uint256);
}
