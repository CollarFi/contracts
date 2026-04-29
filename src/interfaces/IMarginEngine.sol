// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IMarginEngine {
    enum OptionType {
        Put,
        Call
    }

    enum BucketType {
        Put,
        CoveredCall
    }

    function computeInstrumentId(
        address underlying,
        address quoteAsset,
        address collateralAsset,
        uint64 expiry,
        uint256 strike,
        OptionType optionType
    ) external pure returns (bytes32);

    function registerInstrument(
        address underlying,
        address quoteAsset,
        address collateralAsset,
        uint64 expiry,
        uint256 strike,
        OptionType optionType
    ) external returns (bytes32 instrumentId);

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
            OptionType optionType,
            bool exists
        );

    function getInstrumentSettlementState(bytes32 instrumentId)
        external
        view
        returns (bool settlementFinalized, uint256 finalSpotPrice, uint64 finalizedAt);

    function getBucketMetadata(uint256 bucketId)
        external
        view
        returns (bytes32 instrumentId, BucketType bucketType, address owner, bool settled, bool closed);

    function getPutBucketState(uint256 bucketId)
        external
        view
        returns (uint256 collateralBalance, uint256 outstandingQuantity, address longToken);

    function getCoveredCallBucketState(uint256 bucketId)
        external
        view
        returns (uint256 collateralBalance, uint256 coveredQuantity, address longCallToken, address writerResidualToken);

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
        );

    function createCoveredCallBucket(uint256 instrumentId) external returns (uint256 bucketId);

    function createCoveredCallBucket(bytes32 instrumentId) external returns (uint256 bucketId);

    function issueCoveredCall(
        uint256 bucketId,
        uint256 collateralAmount,
        address longCallRecipient,
        address cappedRecipient
    ) external;

    function finalizeInstrumentSettlement(bytes32 instrumentId) external;

    function settleBucket(uint256 bucketId) external;

    function getBucketTokens(uint256 bucketId) external view returns (address primaryToken, address secondaryToken);

    function redeemPut(uint256 bucketId, uint256 quantity, address to) external returns (uint256 payout);

    function redeemCappedUnderlying(uint256 bucketId, uint256 quantity, address to) external returns (uint256 payout);
}
