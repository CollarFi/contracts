// SPDX-License-Identifier: GPL-3.0-only
pragma solidity ^0.8.20;

import {IBridgeAdapter} from "../interfaces/IBridgeAdapter.sol";

library CollarTSABridgeLib {
    struct BridgeConfigStorage {
        address bridgeCoordinator;
        mapping(address asset => IBridgeAdapter adapter) socketBridgeConfigs;
    }

    event BridgeConfigUpdated(address indexed asset, address indexed adapter);
    event BridgeCoordinatorUpdated(address indexed coordinator);

    error CTSA_InvalidConfig();

    function setSocketBridgeConfig(BridgeConfigStorage storage $, address asset, IBridgeAdapter adapter) public {
        if (asset == address(0)) {
            revert CTSA_InvalidConfig();
        }
        $.socketBridgeConfigs[asset] = adapter;
        emit BridgeConfigUpdated(asset, address(adapter));
    }

    function setBridgeCoordinator(BridgeConfigStorage storage $, address coordinator) public {
        $.bridgeCoordinator = coordinator;
        emit BridgeCoordinatorUpdated(coordinator);
    }

    function estimateBridgeFees(BridgeConfigStorage storage $, address asset) public view returns (uint256) {
        IBridgeAdapter adapter = $.socketBridgeConfigs[asset];
        if (address(adapter) == address(0)) {
            revert CTSA_InvalidConfig();
        }
        return adapter.estimateFee();
    }
}
