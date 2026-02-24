// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract FixedPriceOracleMock {
    uint256 public immutable fixedPrice;

    constructor(uint256 fixedPrice_) {
        fixedPrice = fixedPrice_;
    }

    function price() external view returns (uint256) {
        return fixedPrice;
    }
}
