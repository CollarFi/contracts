// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";

import {IMarginEngine} from "../../src/interfaces/IMarginEngine.sol";

contract MockClaimToken is ERC20 {
    address public immutable engine;

    constructor(string memory name_, string memory symbol_, address engine_) ERC20(name_, symbol_) {
        engine = engine_;
    }

    function mint(address to, uint256 amount) external {
        require(msg.sender == engine, "engine");
        _mint(to, amount);
    }

    function burn(address from, uint256 amount) external {
        require(msg.sender == engine, "engine");
        _burn(from, amount);
    }
}

contract MockMarginEngine is IMarginEngine {
    using SafeERC20 for IERC20;

    struct Instrument {
        address underlying;
        address quoteAsset;
        address collateralAsset;
        uint64 expiry;
        uint256 strike;
        uint256 quantityScale;
        uint8 optionType;
        bool exists;
    }

    struct OracleState {
        uint256 midMark;
        uint256 closeoutMark;
        uint256 spotPrice;
        uint64 markUpdatedAt;
        uint64 spotUpdatedAt;
        bool settlementFinalized;
        uint256 finalSpotPrice;
        uint64 finalizedAt;
    }

    struct Bucket {
        bytes32 instrumentId;
        uint8 bucketType;
        address owner;
        uint256 collateralBalance;
        uint256 outstandingQuantity;
        address primaryToken;
        address secondaryToken;
        bool settled;
        bool closed;
        uint256 settlementCollateral;
        uint256 settlementTotalEntitlement;
        uint256 settlementPrimaryRateNumerator;
        uint256 settlementPrimaryRateDenominator;
        uint256 settlementSecondaryRateNumerator;
        uint256 settlementSecondaryRateDenominator;
        uint256 redeemedCollateral;
    }

    address public immutable usdc;
    address public admin;
    address public protocolOwner;
    uint256 public nextBucketId = 1;

    mapping(address => bool) public isWhitelistedMarketMaker;
    mapping(address => bool) public isOracleUpdater;
    mapping(bytes32 => Instrument) public instruments;
    mapping(bytes32 => OracleState) public oracleStates;
    mapping(uint256 => Bucket) public buckets;

    error InvalidInstrument();
    error InvalidBucket();
    error InvalidState();
    error InvalidRecipient();
    error Unauthorized();
    error InsufficientCollateral();

    constructor(address usdc_, address protocolOwner_) {
        usdc = usdc_;
        admin = msg.sender;
        protocolOwner = protocolOwner_;
    }

    function computeInstrumentId(
        address underlying,
        address quoteAsset,
        address collateralAsset,
        uint64 expiry,
        uint256 strike,
        OptionType optionType
    ) public pure returns (bytes32) {
        return keccak256(abi.encode(underlying, quoteAsset, collateralAsset, expiry, strike, optionType));
    }

    function registerInstrument(
        address underlying,
        address quoteAsset,
        address collateralAsset,
        uint64 expiry,
        uint256 strike,
        OptionType optionType
    ) external returns (bytes32 instrumentId) {
        if (msg.sender != admin) revert Unauthorized();
        if (underlying == address(0) || quoteAsset == address(0) || collateralAsset == address(0)) {
            revert InvalidInstrument();
        }
        if (expiry <= block.timestamp || strike == 0) revert InvalidInstrument();

        instrumentId = computeInstrumentId(underlying, quoteAsset, collateralAsset, expiry, strike, optionType);
        if (instruments[instrumentId].exists) revert InvalidInstrument();

        uint256 quantityScale = 10 ** IERC20Metadata(underlying).decimals();
        instruments[instrumentId] = Instrument({
            underlying: underlying,
            quoteAsset: quoteAsset,
            collateralAsset: collateralAsset,
            expiry: expiry,
            strike: strike,
            quantityScale: quantityScale,
            optionType: uint8(optionType),
            exists: true
        });
    }

    function setProtocolOwner(address newOwner) external {
        if (msg.sender != admin || newOwner == address(0)) revert Unauthorized();
        protocolOwner = newOwner;
    }

    function setMarketMaker(address account, bool allowed) external {
        if (msg.sender != admin) revert Unauthorized();
        isWhitelistedMarketMaker[account] = allowed;
    }

    function setOracleUpdater(address account, bool allowed) external {
        if (msg.sender != admin) revert Unauthorized();
        isOracleUpdater[account] = allowed;
    }

    function updateInstrumentOracle(bytes32 instrumentId, uint256 midMark, uint256 closeoutMark, uint256 spotPrice)
        external
    {
        if (msg.sender != admin && !isOracleUpdater[msg.sender]) revert Unauthorized();
        if (!instruments[instrumentId].exists) revert InvalidInstrument();
        OracleState storage state = oracleStates[instrumentId];
        state.midMark = midMark;
        state.closeoutMark = closeoutMark;
        state.spotPrice = spotPrice;
        state.markUpdatedAt = uint64(block.timestamp);
        state.spotUpdatedAt = uint64(block.timestamp);
    }

    function finalizeInstrumentSettlement(bytes32 instrumentId) external {
        Instrument memory instrument = instruments[instrumentId];
        OracleState storage state = oracleStates[instrumentId];
        if (!instrument.exists || block.timestamp < instrument.expiry || state.settlementFinalized) {
            revert InvalidState();
        }
        state.settlementFinalized = true;
        state.finalSpotPrice = state.spotPrice;
        state.finalizedAt = uint64(block.timestamp);
    }

    function createPutBucket(bytes32 instrumentId, address owner) external returns (uint256 bucketId) {
        if (!instruments[instrumentId].exists || instruments[instrumentId].optionType != uint8(OptionType.Put)) {
            revert InvalidInstrument();
        }
        if (!isWhitelistedMarketMaker[owner]) revert Unauthorized();
        if (msg.sender != owner && msg.sender != admin) revert Unauthorized();

        bucketId = nextBucketId++;
        buckets[bucketId] = Bucket({
            instrumentId: instrumentId,
            bucketType: uint8(IMarginEngine.BucketType.Put),
            owner: owner,
            collateralBalance: 0,
            outstandingQuantity: 0,
            primaryToken: address(new MockClaimToken("Mock Long Put", "MLPUT", address(this))),
            secondaryToken: address(0),
            settled: false,
            closed: false,
            settlementCollateral: 0,
            settlementTotalEntitlement: 0,
            settlementPrimaryRateNumerator: 0,
            settlementPrimaryRateDenominator: 0,
            settlementSecondaryRateNumerator: 0,
            settlementSecondaryRateDenominator: 0,
            redeemedCollateral: 0
        });
    }

    function depositPutCollateral(uint256 bucketId, uint256 amount) external {
        Bucket storage bucket = buckets[bucketId];
        if (bucket.owner == address(0) || bucket.bucketType != uint8(IMarginEngine.BucketType.Put)) {
            revert InvalidBucket();
        }
        IERC20(usdc).safeTransferFrom(msg.sender, address(this), amount);
        bucket.collateralBalance += amount;
    }

    function issuePut(uint256 bucketId, uint256 quantity, address recipient) external {
        Bucket storage bucket = buckets[bucketId];
        if (bucket.owner == address(0) || bucket.bucketType != uint8(IMarginEngine.BucketType.Put)) {
            revert InvalidBucket();
        }
        if (msg.sender != bucket.owner || recipient == address(0)) revert Unauthorized();
        Instrument memory instrument = instruments[bucket.instrumentId];

        uint256 required = Math.mulDiv(
            bucket.outstandingQuantity + quantity, instrument.strike, instrument.quantityScale, Math.Rounding.Ceil
        );
        if (bucket.collateralBalance < required) revert InsufficientCollateral();

        bucket.outstandingQuantity += quantity;
        MockClaimToken(bucket.primaryToken).mint(recipient, quantity);
    }

    function createCoveredCallBucket(uint256) external pure returns (uint256) {
        revert InvalidInstrument();
    }

    function createCoveredCallBucket(bytes32 instrumentId) external returns (uint256 bucketId) {
        if (msg.sender != protocolOwner) revert Unauthorized();
        if (!instruments[instrumentId].exists || instruments[instrumentId].optionType != uint8(OptionType.Call)) {
            revert InvalidInstrument();
        }

        bucketId = nextBucketId++;
        buckets[bucketId] = Bucket({
            instrumentId: instrumentId,
            bucketType: uint8(IMarginEngine.BucketType.CoveredCall),
            owner: protocolOwner,
            collateralBalance: 0,
            outstandingQuantity: 0,
            primaryToken: address(new MockClaimToken("Mock Long Call", "MLCALL", address(this))),
            secondaryToken: address(new MockClaimToken("Mock Capped Underlying", "MCAP", address(this))),
            settled: false,
            closed: false,
            settlementCollateral: 0,
            settlementTotalEntitlement: 0,
            settlementPrimaryRateNumerator: 0,
            settlementPrimaryRateDenominator: 0,
            settlementSecondaryRateNumerator: 0,
            settlementSecondaryRateDenominator: 0,
            redeemedCollateral: 0
        });
    }

    function issueCoveredCall(
        uint256 bucketId,
        uint256 collateralAmount,
        address longCallRecipient,
        address cappedRecipient
    ) external {
        Bucket storage bucket = buckets[bucketId];
        if (
            msg.sender != protocolOwner || bucket.owner == address(0)
                || bucket.bucketType != uint8(IMarginEngine.BucketType.CoveredCall)
        ) {
            revert Unauthorized();
        }
        if (longCallRecipient == address(0) || cappedRecipient == address(0)) revert InvalidRecipient();

        address collateralAsset = instruments[bucket.instrumentId].collateralAsset;
        IERC20(collateralAsset).safeTransferFrom(msg.sender, address(this), collateralAmount);
        bucket.collateralBalance += collateralAmount;
        bucket.outstandingQuantity += collateralAmount;
        MockClaimToken(bucket.primaryToken).mint(longCallRecipient, collateralAmount);
        MockClaimToken(bucket.secondaryToken).mint(cappedRecipient, collateralAmount);
    }

    function settleBucket(uint256 bucketId) external {
        Bucket storage bucket = buckets[bucketId];
        if (bucket.owner == address(0) || bucket.settled || bucket.closed) revert InvalidBucket();

        Instrument memory instrument = instruments[bucket.instrumentId];
        OracleState memory state = oracleStates[bucket.instrumentId];
        if (!state.settlementFinalized) revert InvalidState();

        bucket.settled = true;
        bucket.settlementCollateral = bucket.collateralBalance;

        if (bucket.bucketType == uint8(IMarginEngine.BucketType.Put)) {
            bucket.settlementPrimaryRateNumerator =
                Math.max(instrument.strike, state.finalSpotPrice) - state.finalSpotPrice;
            bucket.settlementPrimaryRateDenominator = instrument.quantityScale;
            bucket.settlementTotalEntitlement = Math.mulDiv(
                bucket.outstandingQuantity,
                bucket.settlementPrimaryRateNumerator,
                bucket.settlementPrimaryRateDenominator
            );
            return;
        }

        if (state.finalSpotPrice == 0) {
            bucket.settlementPrimaryRateNumerator = 0;
            bucket.settlementPrimaryRateDenominator = 1;
            bucket.settlementSecondaryRateNumerator = 1;
            bucket.settlementSecondaryRateDenominator = 1;
            bucket.settlementTotalEntitlement = bucket.outstandingQuantity;
            return;
        }

        bucket.settlementPrimaryRateNumerator = Math.max(state.finalSpotPrice, instrument.strike) - instrument.strike;
        bucket.settlementPrimaryRateDenominator = state.finalSpotPrice;
        bucket.settlementSecondaryRateNumerator = Math.min(state.finalSpotPrice, instrument.strike);
        bucket.settlementSecondaryRateDenominator = state.finalSpotPrice;

        uint256 longEntitlement = Math.mulDiv(
            ERC20(bucket.primaryToken).totalSupply(),
            bucket.settlementPrimaryRateNumerator,
            bucket.settlementPrimaryRateDenominator
        );
        uint256 cappedEntitlement = Math.mulDiv(
            ERC20(bucket.secondaryToken).totalSupply(),
            bucket.settlementSecondaryRateNumerator,
            bucket.settlementSecondaryRateDenominator
        );
        bucket.settlementTotalEntitlement = longEntitlement + cappedEntitlement;
    }

    function redeemPut(uint256 bucketId, uint256 quantity, address to) external returns (uint256 payout) {
        Bucket storage bucket = buckets[bucketId];
        if (!bucket.settled || bucket.bucketType != uint8(IMarginEngine.BucketType.Put) || to == address(0)) {
            revert InvalidState();
        }
        MockClaimToken(bucket.primaryToken).burn(msg.sender, quantity);

        payout = _bucketPayout(
            bucket, quantity, bucket.settlementPrimaryRateNumerator, bucket.settlementPrimaryRateDenominator
        );
        bucket.outstandingQuantity -= quantity;
        bucket.collateralBalance -= payout;
        bucket.redeemedCollateral += payout;
        IERC20(usdc).safeTransfer(to, payout);
    }

    function redeemLongCall(uint256 bucketId, uint256 quantity, address to) external returns (uint256 payout) {
        return _redeemCoveredCall(bucketId, quantity, to, true);
    }

    function redeemCappedUnderlying(uint256 bucketId, uint256 quantity, address to) external returns (uint256 payout) {
        return _redeemCoveredCall(bucketId, quantity, to, false);
    }

    function getBucketTokens(uint256 bucketId) external view returns (address primaryToken, address secondaryToken) {
        Bucket memory bucket = buckets[bucketId];
        if (bucket.owner == address(0)) revert InvalidBucket();
        return (bucket.primaryToken, bucket.secondaryToken);
    }

    function getBucketMetadata(uint256 bucketId)
        external
        view
        returns (bytes32 instrumentId, IMarginEngine.BucketType bucketType, address owner, bool settled, bool closed)
    {
        Bucket memory bucket = buckets[bucketId];
        if (bucket.owner == address(0)) revert InvalidBucket();

        instrumentId = bucket.instrumentId;
        bucketType = IMarginEngine.BucketType(bucket.bucketType);
        owner = bucket.owner;
        settled = bucket.settled;
        closed = bucket.closed;
    }

    function getInstrumentMetadata(bytes32 instrumentId)
        external
        view
        returns (
            address underlying,
            address quoteAsset,
            address collateralAsset,
            uint64 expiry,
            uint256 strike,
            uint256 quantityScale,
            IMarginEngine.OptionType optionType,
            bool exists
        )
    {
        Instrument memory instrument = instruments[instrumentId];

        underlying = instrument.underlying;
        quoteAsset = instrument.quoteAsset;
        collateralAsset = instrument.collateralAsset;
        expiry = instrument.expiry;
        strike = instrument.strike;
        quantityScale = instrument.quantityScale;
        optionType = IMarginEngine.OptionType(instrument.optionType);
        exists = instrument.exists;
    }

    function getInstrumentSettlementState(bytes32 instrumentId)
        external
        view
        returns (bool settlementFinalized, uint256 finalSpotPrice, uint64 finalizedAt)
    {
        OracleState memory state = oracleStates[instrumentId];
        settlementFinalized = state.settlementFinalized;
        finalSpotPrice = state.finalSpotPrice;
        finalizedAt = state.finalizedAt;
    }

    function getPutBucketState(uint256 bucketId)
        external
        view
        returns (uint256 collateralBalance, uint256 outstandingQuantity, address longToken)
    {
        Bucket memory bucket = buckets[bucketId];
        if (bucket.owner == address(0) || bucket.bucketType != uint8(IMarginEngine.BucketType.Put)) {
            revert InvalidBucket();
        }

        collateralBalance = bucket.collateralBalance;
        outstandingQuantity = bucket.outstandingQuantity;
        longToken = bucket.primaryToken;
    }

    function getCoveredCallBucketState(uint256 bucketId)
        external
        view
        returns (uint256 collateralBalance, uint256 coveredQuantity, address longCallToken, address writerResidualToken)
    {
        Bucket memory bucket = buckets[bucketId];
        if (bucket.owner == address(0) || bucket.bucketType != uint8(IMarginEngine.BucketType.CoveredCall)) {
            revert InvalidBucket();
        }

        collateralBalance = bucket.collateralBalance;
        coveredQuantity = bucket.outstandingQuantity;
        longCallToken = bucket.primaryToken;
        writerResidualToken = bucket.secondaryToken;
    }

    function getBucketSettlementState(uint256 bucketId)
        external
        view
        returns (
            uint256 settlementCollateral,
            uint256 settlementTotalEntitlement,
            uint256 settlementPrimaryRateNumerator,
            uint256 settlementPrimaryRateDenominator,
            uint256 settlementSecondaryRateNumerator,
            uint256 settlementSecondaryRateDenominator,
            uint256 redeemedCollateral
        )
    {
        Bucket memory bucket = buckets[bucketId];
        if (bucket.owner == address(0)) revert InvalidBucket();

        settlementCollateral = bucket.settlementCollateral;
        settlementTotalEntitlement = bucket.settlementTotalEntitlement;
        settlementPrimaryRateNumerator = bucket.settlementPrimaryRateNumerator;
        settlementPrimaryRateDenominator = bucket.settlementPrimaryRateDenominator;
        settlementSecondaryRateNumerator = bucket.settlementSecondaryRateNumerator;
        settlementSecondaryRateDenominator = bucket.settlementSecondaryRateDenominator;
        redeemedCollateral = bucket.redeemedCollateral;
    }

    function issuePutFromRfq(uint256 bucketId, uint256 quantity, address recipient) external {
        Bucket storage bucket = buckets[bucketId];
        if (bucket.owner == address(0) || bucket.bucketType != uint8(IMarginEngine.BucketType.Put)) {
            revert InvalidBucket();
        }
        if (recipient == address(0)) revert InvalidRecipient();
        Instrument memory instrument = instruments[bucket.instrumentId];

        uint256 required = Math.mulDiv(
            bucket.outstandingQuantity + quantity, instrument.strike, instrument.quantityScale, Math.Rounding.Ceil
        );
        if (bucket.collateralBalance < required) revert InsufficientCollateral();

        bucket.outstandingQuantity += quantity;
        MockClaimToken(bucket.primaryToken).mint(recipient, quantity);
    }

    function issueCoveredCallFromRfq(
        uint256 bucketId,
        uint256 collateralAmount,
        address collateralFrom,
        address longCallRecipient,
        address cappedRecipient
    ) external {
        Bucket storage bucket = buckets[bucketId];
        if (bucket.owner == address(0) || bucket.bucketType != uint8(IMarginEngine.BucketType.CoveredCall)) {
            revert InvalidBucket();
        }
        if (longCallRecipient == address(0) || cappedRecipient == address(0)) revert InvalidRecipient();

        address collateralAsset = instruments[bucket.instrumentId].collateralAsset;
        IERC20(collateralAsset).safeTransferFrom(collateralFrom, address(this), collateralAmount);
        bucket.collateralBalance += collateralAmount;
        bucket.outstandingQuantity += collateralAmount;
        MockClaimToken(bucket.primaryToken).mint(longCallRecipient, collateralAmount);
        MockClaimToken(bucket.secondaryToken).mint(cappedRecipient, collateralAmount);
    }

    function buyCoveredCallFromRfq(
        uint256 bucketId,
        uint256 quantity,
        address burnLongCallFrom,
        address burnCappedFrom,
        address collateralRecipient
    ) external returns (uint256 payout) {
        Bucket storage bucket = buckets[bucketId];
        if (bucket.owner == address(0) || bucket.bucketType != uint8(IMarginEngine.BucketType.CoveredCall)) {
            revert InvalidBucket();
        }
        if (collateralRecipient == address(0)) revert InvalidRecipient();

        MockClaimToken(bucket.primaryToken).burn(burnLongCallFrom, quantity);
        MockClaimToken(bucket.secondaryToken).burn(burnCappedFrom, quantity);

        bucket.outstandingQuantity -= quantity;
        bucket.collateralBalance -= quantity;
        payout = quantity;

        IERC20(instruments[bucket.instrumentId].collateralAsset).safeTransfer(collateralRecipient, payout);
    }

    function _redeemCoveredCall(uint256 bucketId, uint256 quantity, address to, bool longCall)
        internal
        returns (uint256 payout)
    {
        Bucket storage bucket = buckets[bucketId];
        if (!bucket.settled || bucket.bucketType != uint8(IMarginEngine.BucketType.CoveredCall) || to == address(0)) {
            revert InvalidState();
        }

        address token = longCall ? bucket.primaryToken : bucket.secondaryToken;
        uint256 numerator = longCall ? bucket.settlementPrimaryRateNumerator : bucket.settlementSecondaryRateNumerator;
        uint256 denominator =
            longCall ? bucket.settlementPrimaryRateDenominator : bucket.settlementSecondaryRateDenominator;
        MockClaimToken(token).burn(msg.sender, quantity);

        payout = _bucketPayout(bucket, quantity, numerator, denominator);
        bucket.collateralBalance -= payout;
        bucket.redeemedCollateral += payout;

        IERC20(instruments[bucket.instrumentId].collateralAsset).safeTransfer(to, payout);
    }

    function _bucketPayout(Bucket memory bucket, uint256 quantity, uint256 numerator, uint256 denominator)
        internal
        pure
        returns (uint256)
    {
        if (bucket.settlementTotalEntitlement == 0 || numerator == 0) return 0;
        uint256 contractual = Math.mulDiv(quantity, numerator, denominator);
        if (bucket.settlementCollateral >= bucket.settlementTotalEntitlement) return contractual;
        return Math.mulDiv(contractual, bucket.settlementCollateral, bucket.settlementTotalEntitlement);
    }
}
