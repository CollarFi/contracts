// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IBridgeAdapter {
    function estimateFee(address connector, uint256 msgGasLimit, uint256 payloadSize)
        external
        view
        returns (uint256);

    function bridge(
        address receiver,
        uint256 amount,
        uint256 msgGasLimit,
        address connector,
        bytes calldata extraData,
        bytes calldata options
    ) external payable;
}
