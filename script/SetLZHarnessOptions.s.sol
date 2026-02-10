// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Script.sol";
import {OptionsBuilder} from "@layerzerolabs/lz-evm-oapp-v2/contracts/oapp/libs/OptionsBuilder.sol";

interface ILZPingPongLike {
    function setDefaultOptions(bytes calldata options) external;
}

/// @dev Sets default executor options for LZ harness messages.
///
/// Required env vars:
/// - HARNESS (address)
/// - RECEIVE_GAS (uint256)
///
/// Optional:
/// - RECEIVE_VALUE (uint256) default 0
contract SetLZHarnessOptions is Script {
    using OptionsBuilder for bytes;

    function run() external {
        address harness = vm.envAddress("HARNESS");
        uint128 receiveGas = uint128(vm.envUint("RECEIVE_GAS"));
        uint128 receiveValue = uint128(vm.envOr("RECEIVE_VALUE", uint256(0)));

        bytes memory options = OptionsBuilder.newOptions().addExecutorLzReceiveOption(receiveGas, receiveValue);

        vm.startBroadcast();
        ILZPingPongLike(harness).setDefaultOptions(options);
        vm.stopBroadcast();

        console2.log("Harness", harness);
        console2.log("Receive gas", uint256(receiveGas));
        console2.log("Receive value", uint256(receiveValue));
        console2.logBytes(options);
    }
}
