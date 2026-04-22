// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IMarginEngineRfqRouter {
    enum Side {
        Buy,
        Sell
    }

    enum InstrumentType {
        Put,
        Call
    }

    enum FulfillmentType {
        Mint,
        Transfer,
        Burn
    }

    struct Action {
        Side side;
        InstrumentType instrumentType;
        FulfillmentType fulfillmentType;
        uint256 bucketId;
        bytes32 instrumentId;
        uint256 quantity;
        uint256 quoteAmount;
        address maker;
        address longRecipient;
        address longSource;
        address cappedRecipient;
        address cappedSource;
        address collateralRecipient;
    }

    struct Quote {
        address taker;
        address authorizedExecutor;
        address quoteAsset;
        uint64 validUntil;
        uint256 nonce;
        uint256 salt;
        Action[] actions;
    }

    struct SignerSignature {
        address signer;
        bytes signature;
    }

    struct ExecutionParams {
        address taker;
    }

    function hashQuote(Quote calldata quote) external view returns (bytes32);

    function executeRfq(Quote calldata quote, SignerSignature[] calldata signatures, ExecutionParams calldata params)
        external
        returns (bytes32 quoteHash);
}
