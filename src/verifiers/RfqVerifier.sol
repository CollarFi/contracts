// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {SafeCast} from "openzeppelin/utils/math/SafeCast.sol";
import {OptionEncoding} from "lyra-utils/encoding/OptionEncoding.sol";
import {IRfqModule} from "v2-matching/src/interfaces/IRfqModule.sol";
import {IRfqVerifier} from "../interfaces/IRfqVerifier.sol";

contract RfqVerifier is IRfqVerifier {
    using SafeCast for uint256;

    error CTSA_InvalidAsset();
    error CTSA_InvalidRfqTradeLength();
    error CTSA_InvalidRfqTradeDetails();
    error CTSA_TradeDataDoesNotMatchOrderHash();

    function parseAndValidate(
        bytes calldata actionData,
        bytes calldata extraData,
        address wrappedDepositAsset,
        address optionAsset
    ) external pure returns (ParsedRfq memory parsed) {
        bytes memory rfqExtraData = extraData;
        if (extraData.length != 0) {
            (parsed.loanId, rfqExtraData) = abi.decode(extraData, (uint256, bytes));
            if (parsed.loanId == 0) revert CTSA_InvalidRfqTradeDetails();
        }

        IRfqModule.TradeData[] memory makerTrades;
        parsed.isTaker = rfqExtraData.length != 0;

        if (!parsed.isTaker) {
            IRfqModule.RfqOrder memory makerOrder = abi.decode(actionData, (IRfqModule.RfqOrder));
            makerTrades = makerOrder.trades;
        } else {
            IRfqModule.TakerOrder memory takerOrder = abi.decode(actionData, (IRfqModule.TakerOrder));
            if (keccak256(rfqExtraData) != takerOrder.orderHash) revert CTSA_TradeDataDoesNotMatchOrderHash();
            makerTrades = abi.decode(rfqExtraData, (IRfqModule.TradeData[]));
        }

        if (makerTrades.length == 1) {
            if (makerTrades[0].asset != wrappedDepositAsset || makerTrades[0].subId != 0) {
                revert CTSA_InvalidAsset();
            }
            parsed.isSpot = true;
            parsed.spotTrade = makerTrades[0];
            return parsed;
        }

        if (makerTrades.length != 2) revert CTSA_InvalidRfqTradeLength();

        bool hasCall;
        bool hasPut;
        for (uint256 i = 0; i < makerTrades.length; i++) {
            if (makerTrades[i].asset != optionAsset) revert CTSA_InvalidAsset();

            (uint256 expiry, uint256 strike, bool isCall) = OptionEncoding.fromSubId(makerTrades[i].subId.toUint96());
            if (isCall) {
                if (hasCall) revert CTSA_InvalidRfqTradeDetails();
                parsed.callTrade = makerTrades[i];
                parsed.callExpiry = expiry;
                parsed.callStrike = strike;
                hasCall = true;
            } else {
                if (hasPut) revert CTSA_InvalidRfqTradeDetails();
                parsed.putTrade = makerTrades[i];
                parsed.putExpiry = expiry;
                parsed.putStrike = strike;
                hasPut = true;
            }
        }

        if (!hasCall || !hasPut || parsed.callExpiry != parsed.putExpiry) revert CTSA_InvalidRfqTradeDetails();
    }
}
