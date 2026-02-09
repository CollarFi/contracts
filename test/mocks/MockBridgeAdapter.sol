// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IBridgeAdapter} from "../../src/interfaces/IBridgeAdapter.sol";

contract MockBridgeAdapter is IBridgeAdapter {
    uint256 public fee;
    bytes32 public msgId;

    function setFee(uint256 fee_) external {
        fee = fee_;
    }

    function setMessageId(bytes32 msgId_) external {
        msgId = msgId_;
    }

    function messageId() external view override returns (bytes32) {
        return msgId;
    }

    function estimateFee() external view override returns (uint256) {
        return fee;
    }

    function bridge(address, uint256) external payable override {}
}
