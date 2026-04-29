// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Initializable} from "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import {UUPSUpgradeable} from "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";
import {AccessControlUpgradeable} from "@openzeppelin/contracts-upgradeable/access/AccessControlUpgradeable.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";

import {IMarginEngine} from "../../../src/interfaces/IMarginEngine.sol";

contract LatestClaimToken is ERC20 {
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

contract LatestMarginEngineHarness is Initializable, UUPSUpgradeable, AccessControlUpgradeable, IMarginEngine {
    using SafeERC20 for IERC20;

    bytes32 public constant UPGRADER_ROLE = keccak256("UPGRADER_ROLE");

    enum BucketType {
        Put,
        CoveredCall
    }

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

    address public usdc;
    address public protocolOwner;
    address public rfqRouter;
    uint64 public maxMarkAge;
    uint64 public maxSpotAge;
    uint256 public nextBucketId;

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

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() {
        _disableInitializers();
    }

    function initialize(
        address admin,
        address upgrader,
        address protocolOwner_,
        address usdc_,
        uint64 maxMarkAge_,
        uint64 maxSpotAge_
    ) external initializer {
        __AccessControl_init();
        if (admin == address(0) || upgrader == address(0) || protocolOwner_ == address(0) || usdc_ == address(0)) {
            revert InvalidRecipient();
        }

        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        _grantRole(UPGRADER_ROLE, upgrader);
        protocolOwner = protocolOwner_;
        usdc = usdc_;
        maxMarkAge = maxMarkAge_;
        maxSpotAge = maxSpotAge_;
        nextBucketId = 1;
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
    ) external onlyRole(DEFAULT_ADMIN_ROLE) returns (bytes32 instrumentId) {
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

    function setProtocolOwner(address newOwner) external onlyRole(DEFAULT_ADMIN_ROLE) {
        if (newOwner == address(0)) revert InvalidRecipient();
        protocolOwner = newOwner;
    }

    function setRfqRouter(address router) external {
        if (msg.sender != protocolOwner && !hasRole(DEFAULT_ADMIN_ROLE, msg.sender)) revert Unauthorized();
        if (router == address(0)) revert InvalidRecipient();
        rfqRouter = router;
    }

    function setMarketMaker(address account, bool allowed) external onlyRole(DEFAULT_ADMIN_ROLE) {
        isWhitelistedMarketMaker[account] = allowed;
    }

    function setOracleUpdater(address account, bool allowed) external onlyRole(DEFAULT_ADMIN_ROLE) {
        isOracleUpdater[account] = allowed;
    }

    function updateInstrumentOracle(bytes32 instrumentId, uint256 midMark, uint256 closeoutMark, uint256 spotPrice)
        external
    {
        if (!hasRole(DEFAULT_ADMIN_ROLE, msg.sender) && !isOracleUpdater[msg.sender]) revert Unauthorized();
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
        if (msg.sender != owner && !hasRole(DEFAULT_ADMIN_ROLE, msg.sender)) revert Unauthorized();

        bucketId = nextBucketId++;
        buckets[bucketId] = Bucket({
            instrumentId: instrumentId,
            bucketType: uint8(BucketType.Put),
            owner: owner,
            collateralBalance: 0,
            outstandingQuantity: 0,
            primaryToken: address(new LatestClaimToken("Latest Long Put", "LLPUT", address(this))),
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
        if (bucket.owner == address(0) || bucket.bucketType != uint8(BucketType.Put)) revert InvalidBucket();
        IERC20(usdc).safeTransferFrom(msg.sender, address(this), amount);
        bucket.collateralBalance += amount;
    }

    function issuePut(uint256 bucketId, uint256 quantity, address recipient) external {
        Bucket storage bucket = buckets[bucketId];
        if (bucket.owner == address(0) || bucket.bucketType != uint8(BucketType.Put)) revert InvalidBucket();
        if (msg.sender != bucket.owner || recipient == address(0)) revert Unauthorized();
        Instrument memory instrument = instruments[bucket.instrumentId];

        uint256 required = Math.mulDiv(
            bucket.outstandingQuantity + quantity, instrument.strike, instrument.quantityScale, Math.Rounding.Ceil
        );
        if (bucket.collateralBalance < required) revert InsufficientCollateral();

        bucket.outstandingQuantity += quantity;
        LatestClaimToken(bucket.primaryToken).mint(recipient, quantity);
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
            bucketType: uint8(BucketType.CoveredCall),
            owner: protocolOwner,
            collateralBalance: 0,
            outstandingQuantity: 0,
            primaryToken: address(new LatestClaimToken("Latest Long Call", "LLCALL", address(this))),
            secondaryToken: address(new LatestClaimToken("Latest Capped Underlying", "LCAP", address(this))),
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
                || bucket.bucketType != uint8(BucketType.CoveredCall)
        ) {
            revert Unauthorized();
        }
        if (longCallRecipient == address(0) || cappedRecipient == address(0)) revert InvalidRecipient();

        address collateralAsset = instruments[bucket.instrumentId].collateralAsset;
        IERC20(collateralAsset).safeTransferFrom(msg.sender, address(this), collateralAmount);
        bucket.collateralBalance += collateralAmount;
        bucket.outstandingQuantity += collateralAmount;
        LatestClaimToken(bucket.primaryToken).mint(longCallRecipient, collateralAmount);
        LatestClaimToken(bucket.secondaryToken).mint(cappedRecipient, collateralAmount);
    }

    function settleBucket(uint256 bucketId) external {
        Bucket storage bucket = buckets[bucketId];
        if (bucket.owner == address(0) || bucket.settled || bucket.closed) revert InvalidBucket();

        Instrument memory instrument = instruments[bucket.instrumentId];
        OracleState memory state = oracleStates[bucket.instrumentId];
        if (!state.settlementFinalized) revert InvalidState();

        bucket.settled = true;
        bucket.settlementCollateral = bucket.collateralBalance;

        if (bucket.bucketType == uint8(BucketType.Put)) {
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
        if (!bucket.settled || bucket.bucketType != uint8(BucketType.Put) || to == address(0)) revert InvalidState();
        LatestClaimToken(bucket.primaryToken).burn(msg.sender, quantity);

        payout = _bucketPayout(
            bucket, quantity, bucket.settlementPrimaryRateNumerator, bucket.settlementPrimaryRateDenominator
        );
        bucket.outstandingQuantity -= quantity;
        bucket.collateralBalance -= payout;
        bucket.redeemedCollateral += payout;
        IERC20(usdc).safeTransfer(to, payout);
    }

    function redeemCappedUnderlying(uint256 bucketId, uint256 quantity, address to) external returns (uint256 payout) {
        return _redeemCoveredCall(bucketId, quantity, to, false);
    }

    function redeemLongCall(uint256 bucketId, uint256 quantity, address to) external returns (uint256 payout) {
        return _redeemCoveredCall(bucketId, quantity, to, true);
    }

    function getBucketTokens(uint256 bucketId) external view returns (address primaryToken, address secondaryToken) {
        Bucket memory bucket = buckets[bucketId];
        if (bucket.owner == address(0)) revert InvalidBucket();
        return (bucket.primaryToken, bucket.secondaryToken);
    }

    function getBucketMetadata(uint256 bucketId)
        external
        view
        returns (bytes32 instrumentId, uint8 bucketType, address owner, bool settled, bool closed)
    {
        Bucket memory bucket = buckets[bucketId];
        if (bucket.owner == address(0)) revert InvalidBucket();
        return (bucket.instrumentId, bucket.bucketType, bucket.owner, bucket.settled, bucket.closed);
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
            uint8 optionType,
            bool exists
        )
    {
        Instrument memory instrument = instruments[instrumentId];
        return (
            instrument.underlying,
            instrument.quoteAsset,
            instrument.collateralAsset,
            instrument.expiry,
            instrument.strike,
            instrument.quantityScale,
            instrument.optionType,
            instrument.exists
        );
    }

    function getRfqActionMetadata(uint256 bucketId)
        external
        view
        returns (
            bytes32 instrumentId,
            address bucketOwner,
            bool settled,
            bool closed,
            address quoteAsset,
            uint64 expiry,
            uint8 instrumentProductKind,
            uint8 bucketProductKind
        )
    {
        Bucket memory bucket = buckets[bucketId];
        if (bucket.owner == address(0)) revert InvalidBucket();
        Instrument memory instrument = instruments[bucket.instrumentId];
        if (!instrument.exists) revert InvalidInstrument();
        return (
            bucket.instrumentId,
            bucket.owner,
            bucket.settled,
            bucket.closed,
            instrument.quoteAsset,
            instrument.expiry,
            instrument.optionType == uint8(OptionType.Put) ? uint8(0) : uint8(1),
            bucket.bucketType == uint8(BucketType.Put) ? uint8(0) : uint8(1)
        );
    }

    function issuePutFromRfq(uint256 bucketId, uint256 quantity, address recipient) external onlyRfqRouter {
        Bucket storage bucket = buckets[bucketId];
        if (bucket.owner == address(0) || bucket.bucketType != uint8(BucketType.Put)) revert InvalidBucket();
        if (recipient == address(0)) revert InvalidRecipient();
        Instrument memory instrument = instruments[bucket.instrumentId];

        uint256 required = Math.mulDiv(
            bucket.outstandingQuantity + quantity, instrument.strike, instrument.quantityScale, Math.Rounding.Ceil
        );
        if (bucket.collateralBalance < required) revert InsufficientCollateral();

        bucket.outstandingQuantity += quantity;
        LatestClaimToken(bucket.primaryToken).mint(recipient, quantity);
    }

    function issueCoveredCallFromRfq(
        uint256 bucketId,
        uint256 collateralAmount,
        address collateralFrom,
        address longCallRecipient,
        address cappedRecipient
    ) external onlyRfqRouter {
        Bucket storage bucket = buckets[bucketId];
        if (bucket.owner == address(0) || bucket.bucketType != uint8(BucketType.CoveredCall)) revert InvalidBucket();
        if (longCallRecipient == address(0) || cappedRecipient == address(0)) revert InvalidRecipient();

        address collateralAsset = instruments[bucket.instrumentId].collateralAsset;
        IERC20(collateralAsset).safeTransferFrom(collateralFrom, address(this), collateralAmount);
        bucket.collateralBalance += collateralAmount;
        bucket.outstandingQuantity += collateralAmount;
        LatestClaimToken(bucket.primaryToken).mint(longCallRecipient, collateralAmount);
        LatestClaimToken(bucket.secondaryToken).mint(cappedRecipient, collateralAmount);
    }

    function buyCoveredCallFromRfq(
        uint256 bucketId,
        uint256 quantity,
        address burnLongCallFrom,
        address burnCappedFrom,
        address collateralRecipient
    ) external onlyRfqRouter returns (uint256 payout) {
        Bucket storage bucket = buckets[bucketId];
        if (bucket.owner == address(0) || bucket.bucketType != uint8(BucketType.CoveredCall)) revert InvalidBucket();
        if (collateralRecipient == address(0)) revert InvalidRecipient();

        LatestClaimToken(bucket.primaryToken).burn(burnLongCallFrom, quantity);
        LatestClaimToken(bucket.secondaryToken).burn(burnCappedFrom, quantity);

        bucket.outstandingQuantity -= quantity;
        bucket.collateralBalance -= quantity;
        payout = quantity;

        IERC20(instruments[bucket.instrumentId].collateralAsset).safeTransfer(collateralRecipient, payout);
    }

    modifier onlyRfqRouter() {
        if (msg.sender != rfqRouter) revert Unauthorized();
        _;
    }

    function _redeemCoveredCall(uint256 bucketId, uint256 quantity, address to, bool longCall)
        internal
        returns (uint256 payout)
    {
        Bucket storage bucket = buckets[bucketId];
        if (!bucket.settled || bucket.bucketType != uint8(BucketType.CoveredCall) || to == address(0)) {
            revert InvalidState();
        }

        address token = longCall ? bucket.primaryToken : bucket.secondaryToken;
        uint256 numerator = longCall ? bucket.settlementPrimaryRateNumerator : bucket.settlementSecondaryRateNumerator;
        uint256 denominator =
            longCall ? bucket.settlementPrimaryRateDenominator : bucket.settlementSecondaryRateDenominator;
        LatestClaimToken(token).burn(msg.sender, quantity);

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

    function _authorizeUpgrade(address) internal override onlyRole(UPGRADER_ROLE) {}
}
