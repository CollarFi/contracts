// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IRfqModule} from "v2-matching/src/interfaces/IRfqModule.sol";

interface IRfqVerifier {
    struct ParsedRfq {
        bool isSpot;
        bool isTaker;
        uint256 loanId;
        IRfqModule.TradeData spotTrade;
        IRfqModule.TradeData callTrade;
        IRfqModule.TradeData putTrade;
        IRfqModule.TradeData[] optionTrades;
        uint256 callExpiry;
        uint256 callStrike;
        uint256 putExpiry;
        uint256 putStrike;
    }

    function parseAndValidate(
        bytes calldata actionData,
        bytes calldata extraData,
        address wrappedDepositAsset,
        address optionAsset
    ) external pure returns (ParsedRfq memory parsed);
}
