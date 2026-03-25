// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Script.sol";

import {LZPingPong} from "../src/bridge/harness/LZPingPong.sol";
import {LZEndpointV2Mock} from "../test/mocks/LZEndpointV2Mock.sol";

/// @dev Deploy minimal LZ harness endpoint on one chain.
///
/// Required env vars:
/// - ADMIN (address)
/// - REMOTE_EID (uint32)
/// - OUTPUT_JSON (string)
///
/// Optional:
/// - LZ_ENDPOINT (address)  (if omitted, deploys placeholder endpoint mock)
contract DeployLZHarness is Script {
    function run() external {
        address admin = vm.envAddress("ADMIN");
        uint32 remoteEid = uint32(vm.envUint("REMOTE_EID"));

        address endpoint = vm.envOr("LZ_ENDPOINT", address(0));

        vm.startBroadcast();

        if (endpoint == address(0)) {
            endpoint = address(new LZEndpointV2Mock());
        }

        LZPingPong app = new LZPingPong(admin, endpoint, remoteEid);

        vm.stopBroadcast();

        string memory outPath = vm.envString("OUTPUT_JSON");

        string memory json;
        json = vm.serializeAddress("addrs", "lzHarness", address(app));
        json = vm.serializeAddress("addrs", "lzEndpoint", endpoint);
        vm.writeJson(json, outPath);

        console2.log("LZ harness", address(app));
        console2.log("LZ endpoint", endpoint);
        console2.log("Wrote", outPath);
    }
}
