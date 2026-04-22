// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {IMarginEngineRfqRouter} from "../../src/interfaces/IMarginEngineRfqRouter.sol";
import {MockMarginEngine} from "./MockMarginEngine.sol";

contract MockMarginEngineRfqRouter is IMarginEngineRfqRouter {
    using SafeERC20 for IERC20;

    MockMarginEngine public immutable engine;
    uint256 public protocolFeeBps;
    address public feeRecipient;
    bytes32 public lastQuoteHash;

    constructor(MockMarginEngine engine_, address feeRecipient_) {
        engine = engine_;
        feeRecipient = feeRecipient_;
    }

    function setProtocolFeeConfig(uint256 protocolFeeBps_, address feeRecipient_) external {
        protocolFeeBps = protocolFeeBps_;
        feeRecipient = feeRecipient_;
    }

    function hashQuote(Quote calldata quote) external pure returns (bytes32) {
        return keccak256(
            abi.encode(
                quote.taker,
                quote.authorizedExecutor,
                quote.quoteAsset,
                quote.validUntil,
                quote.nonce,
                quote.salt,
                _hashActions(quote.actions)
            )
        );
    }

    function executeRfq(Quote calldata quote, SignerSignature[] calldata, ExecutionParams calldata params)
        external
        returns (bytes32 quoteHash)
    {
        quoteHash = keccak256(
            abi.encode(
                quote.taker,
                quote.authorizedExecutor,
                quote.quoteAsset,
                quote.validUntil,
                quote.nonce,
                quote.salt,
                _hashActions(quote.actions)
            )
        );
        lastQuoteHash = quoteHash;

        uint256 grossVolume;
        for (uint256 index = 0; index < quote.actions.length; ++index) {
            Action calldata action = quote.actions[index];
            grossVolume += action.quoteAmount;

            if (action.quoteAmount == 0) continue;
            if (action.side == Side.Buy) {
                IERC20(quote.quoteAsset).safeTransferFrom(params.taker, action.maker, action.quoteAmount);
            } else {
                IERC20(quote.quoteAsset).safeTransferFrom(action.maker, params.taker, action.quoteAmount);
            }
        }

        uint256 protocolFee = (grossVolume * protocolFeeBps) / 10_000;
        if (protocolFee != 0) {
            IERC20(quote.quoteAsset).safeTransferFrom(params.taker, feeRecipient, protocolFee);
        }

        for (uint256 index = 0; index < quote.actions.length; ++index) {
            Action calldata action = quote.actions[index];
            if (action.instrumentType == InstrumentType.Put) {
                if (action.fulfillmentType == FulfillmentType.Mint) {
                    engine.issuePutFromRfq(action.bucketId, action.quantity, action.longRecipient);
                } else {
                    (address longPutToken,) = engine.getBucketTokens(action.bucketId);
                    IERC20(longPutToken).safeTransferFrom(action.longSource, action.longRecipient, action.quantity);
                }
                continue;
            }

            if (action.fulfillmentType == FulfillmentType.Mint) {
                (,, address bucketOwner,,) = engine.getBucketMetadata(action.bucketId);
                engine.issueCoveredCallFromRfq(
                    action.bucketId, action.quantity, bucketOwner, action.longRecipient, action.cappedRecipient
                );
                continue;
            }

            if (action.fulfillmentType == FulfillmentType.Transfer) {
                (address transferLongCallToken,) = engine.getBucketTokens(action.bucketId);
                IERC20(transferLongCallToken).safeTransferFrom(action.longSource, action.longRecipient, action.quantity);
                continue;
            }

            (address longCallToken, address cappedToken) = engine.getBucketTokens(action.bucketId);
            IERC20(longCallToken).safeTransferFrom(action.longSource, address(this), action.quantity);
            IERC20(cappedToken).safeTransferFrom(action.cappedSource, address(this), action.quantity);
            engine.buyCoveredCallFromRfq(
                action.bucketId, action.quantity, address(this), address(this), action.collateralRecipient
            );
        }
    }

    function _hashActions(Action[] calldata actions) internal pure returns (bytes32) {
        bytes32[] memory actionHashes = new bytes32[](actions.length);
        for (uint256 index = 0; index < actions.length; ++index) {
            Action calldata action = actions[index];
            actionHashes[index] = keccak256(
                abi.encode(
                    action.side,
                    action.instrumentType,
                    action.fulfillmentType,
                    action.bucketId,
                    action.instrumentId,
                    action.quantity,
                    action.quoteAmount,
                    action.maker,
                    action.longRecipient,
                    action.longSource,
                    action.cappedRecipient,
                    action.cappedSource,
                    action.collateralRecipient
                )
            );
        }
        return keccak256(abi.encodePacked(actionHashes));
    }
}
