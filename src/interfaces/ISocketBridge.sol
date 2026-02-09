// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface ISocketBridge {
    function bridge(
        address receiver_,
        uint256 amount_,
        uint256 msgGasLimit_,
        address connector_,
        bytes calldata extraData_,
        bytes calldata options_
    ) external payable;
}

interface ISocketBridgeWithFees is ISocketBridge {
    function getMinFees(address connector_, uint256 msgGasLimit_, uint256 payloadSize_)
        external
        view
        returns (uint256 totalFees);
}
