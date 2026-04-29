// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Initializable} from "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import {UUPSUpgradeable} from "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";
import {AccessControlUpgradeable} from "@openzeppelin/contracts-upgradeable/access/AccessControlUpgradeable.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {IMarginEngineRfqRouter} from "../../../src/interfaces/IMarginEngineRfqRouter.sol";
import {LatestMarginEngineHarness} from "./LatestMarginEngineHarness.sol";

contract LatestMarginEngineRfqRouterHarness is
    Initializable,
    UUPSUpgradeable,
    AccessControlUpgradeable,
    IMarginEngineRfqRouter
{
    using SafeERC20 for IERC20;

    bytes32 public constant UPGRADER_ROLE = keccak256("UPGRADER_ROLE");

    LatestMarginEngineHarness public engine;
    uint256 public protocolFeeBps;
    address public feeRecipient;
    bytes32 public lastQuoteHash;

    error InvalidRecipient();
    error InvalidProtocolFeeConfig();
    error InvalidQuoteAsset();
    error InvalidTaker();
    error InvalidExecutor();
    error QuoteExpired();
    error InvalidAction();
    error InvalidBucket();
    error InvalidInstrument();

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() {
        _disableInitializers();
    }

    function initialize(address admin, address upgrader, address engine_) external initializer {
        __AccessControl_init();
        if (admin == address(0) || upgrader == address(0) || engine_ == address(0)) revert InvalidRecipient();

        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        _grantRole(UPGRADER_ROLE, upgrader);
        engine = LatestMarginEngineHarness(engine_);
    }

    function setProtocolFeeConfig(uint256 protocolFeeBps_, address feeRecipient_)
        external
        onlyRole(DEFAULT_ADMIN_ROLE)
    {
        if (protocolFeeBps_ > 10_000 || feeRecipient_ == address(0)) {
            revert InvalidProtocolFeeConfig();
        }
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
        if (quote.quoteAsset != engine.usdc()) revert InvalidQuoteAsset();
        if (block.timestamp > quote.validUntil) revert QuoteExpired();
        if (params.taker == address(0)) revert InvalidTaker();
        if (quote.authorizedExecutor != address(0) && msg.sender != quote.authorizedExecutor) revert InvalidExecutor();
        if (quote.taker != address(0) && quote.taker != params.taker) revert InvalidTaker();

        _validateActions(quote);

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

        uint256 grossVolume = _settlePremiums(quote, params.taker);
        uint256 protocolFee = (grossVolume * protocolFeeBps) / 10_000;
        if (protocolFee != 0) IERC20(quote.quoteAsset).safeTransferFrom(params.taker, feeRecipient, protocolFee);

        _executeActions(quote);
    }

    function _validateActions(Quote calldata quote) internal view {
        if (quote.actions.length == 0) revert InvalidAction();

        for (uint256 index = 0; index < quote.actions.length; ++index) {
            Action calldata action = quote.actions[index];
            if (action.quantity == 0 || action.maker == address(0)) revert InvalidAction();

            (
                bytes32 bucketInstrumentId,
                address bucketOwner,
                bool settled,
                bool closed,
                address quoteAsset,
                uint64 expiry,
                uint8 instrumentProductKind,
                uint8 bucketProductKind
            ) = engine.getRfqActionMetadata(action.bucketId);

            if (bucketOwner == address(0) || settled || closed) revert InvalidBucket();
            if (bucketInstrumentId != action.instrumentId) revert InvalidInstrument();
            if (quoteAsset != quote.quoteAsset) revert InvalidQuoteAsset();
            if (block.timestamp >= expiry) revert QuoteExpired();

            uint8 expectedProductKind = action.instrumentType == InstrumentType.Put ? uint8(0) : uint8(1);
            if (instrumentProductKind != expectedProductKind) revert InvalidInstrument();
            if (bucketProductKind != expectedProductKind) revert InvalidBucket();
        }
    }

    function _settlePremiums(Quote calldata quote, address taker) internal returns (uint256 grossVolume) {
        for (uint256 index = 0; index < quote.actions.length; ++index) {
            Action calldata action = quote.actions[index];
            grossVolume += action.quoteAmount;
            if (action.quoteAmount == 0) continue;

            if (action.side == Side.Buy) {
                IERC20(quote.quoteAsset).safeTransferFrom(taker, action.maker, action.quoteAmount);
            } else {
                IERC20(quote.quoteAsset).safeTransferFrom(action.maker, taker, action.quoteAmount);
            }
        }
    }

    function _executeActions(Quote calldata quote) internal {
        for (uint256 index = 0; index < quote.actions.length; ++index) {
            Action calldata action = quote.actions[index];
            (,,,,,,, uint8 bucketProductKind) = engine.getRfqActionMetadata(action.bucketId);

            if (bucketProductKind == uint8(0)) {
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

    function _authorizeUpgrade(address) internal override onlyRole(UPGRADER_ROLE) {}
}
