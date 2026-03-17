// SPDX-License-Identifier: GPL-3.0-only
pragma solidity ^0.8.20;

import {IDepositModule} from "v2-matching/src/interfaces/IDepositModule.sol";
import {IWithdrawalModule} from "v2-matching/src/interfaces/IWithdrawalModule.sol";
import {ITradeModule} from "v2-matching/src/interfaces/ITradeModule.sol";
import {IRfqModule} from "v2-matching/src/interfaces/IRfqModule.sol";
import {IOptionAsset} from "v2-core/src/interfaces/IOptionAsset.sol";
import {ISpotFeed} from "v2-core/src/interfaces/ISpotFeed.sol";

import {IRfqVerifier} from "../interfaces/IRfqVerifier.sol";
import {ICollarTsaRfqDelegateModule} from "../interfaces/ICollarTsaRfqDelegateModule.sol";
import {ICollarTSA} from "../interfaces/ICollarTSA.sol";
import {IOptionRiskVerifier} from "../interfaces/IOptionRiskVerifier.sol";
import {CollarTSABridgeLib} from "./CollarTSABridgeLib.sol";

library CollarTSAStorageLib {
    /// @custom:storage-location erc7201:lyra.storage.CollarTSA
    struct CollarTSAStorage {
        IDepositModule depositModule;
        IWithdrawalModule withdrawalModule;
        ITradeModule tradeModule;
        IRfqModule rfqModule;
        IOptionAsset optionAsset;
        ISpotFeed baseFeed;
        IRfqVerifier rfqVerifier;
        ICollarTsaRfqDelegateModule rfqDelegateModule;
        ICollarTSA.CollarTSAParams params;
        ICollarTSA.CollateralManagementParams collateralManagementParams;
        bytes32 lastSeenHash;
        address loanStore;
        IOptionRiskVerifier optionRiskVerifier;
        CollarTSABridgeLib.BridgeConfigStorage bridge;
        mapping(uint256 => uint256) withdrawExecutionNonce;
    }

    // keccak256(abi.encode(uint256(keccak256("lyra.storage.CollarTSA")) - 1)) & ~bytes32(uint256(0xff))
    bytes32 internal constant STORAGE_LOCATION = 0x62b72349c5c9dfc4c2d0e5f1b0600421e6f0d0f8ac3a0ffdf4c4c0b7d4b4b000;

    function get() internal pure returns (CollarTSAStorage storage $) {
        assembly {
            $.slot := STORAGE_LOCATION
        }
    }
}
