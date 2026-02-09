// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IBridgeAdapter} from "../../src/interfaces/IBridgeAdapter.sol";

contract MockBridgeAdapter is IBridgeAdapter {
    uint256 public fee;

    function setFee(uint256 fee_) external {
        fee = fee_;
    }

    function estimateFee(address, uint256, uint256) external view override returns (uint256) {
        return fee;
    }

    function bridge(address, uint256, uint256, address, bytes calldata, bytes calldata)
        external
        payable
        override
    {}
}
