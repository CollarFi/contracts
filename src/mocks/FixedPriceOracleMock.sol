// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IPriceOracle} from "../../lib/euler-vault-kit/src/interfaces/IPriceOracle.sol";

contract FixedPriceOracleMock is IPriceOracle {
    uint256 public immutable fixedPrice;

    constructor(uint256 fixedPrice_) {
        fixedPrice = fixedPrice_;
    }

    function name() external pure returns (string memory) {
        return "FixedPriceOracleMock";
    }

    function getQuote(uint256 inAmount, address, address) external view returns (uint256 outAmount) {
        outAmount = (inAmount * fixedPrice) / 1e18;
    }

    function getQuotes(uint256 inAmount, address, address)
        external
        view
        returns (uint256 bidOutAmount, uint256 askOutAmount)
    {
        uint256 out = (inAmount * fixedPrice) / 1e18;
        return (out, out);
    }

    // Legacy helper retained for older tests/scripts.
    function price() external view returns (uint256) {
        return fixedPrice;
    }
}
