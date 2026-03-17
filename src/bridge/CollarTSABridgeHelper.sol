// SPDX-License-Identifier: GPL-3.0-only
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {IBridgeAdapter} from "../interfaces/IBridgeAdapter.sol";
import {CollarTSAStorageLib} from "../libraries/CollarTSAStorageLib.sol";

contract CollarTSABridgeHelper {
    error CTSA_InvalidConfig();
    error CTSA_InsufficientValue();

    function bridgeToL1(address asset, uint256 amount, address receiver)
        external
        payable
        returns (bytes32 socketMessageId)
    {
        CollarTSAStorageLib.CollarTSAStorage storage $ = CollarTSAStorageLib.get();
        IBridgeAdapter adapter = $.bridge.socketBridgeConfigs[asset];
        if (msg.sender != $.bridge.bridgeCoordinator || receiver == address(0) || address(adapter) == address(0)) {
            revert CTSA_InvalidConfig();
        }

        uint256 fee = adapter.estimateFee();
        if (msg.value != fee) {
            revert CTSA_InsufficientValue();
        }

        socketMessageId = adapter.messageId();
        IERC20(asset).approve(address(adapter), amount);
        adapter.bridge{value: fee}(receiver, amount);
    }
}
