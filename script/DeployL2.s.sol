// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Script.sol";

import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";

import {CollarTSA} from "../src/CollarTSA.sol";
import {CollarLoanStore} from "../src/CollarLoanStore.sol";
import {CollarTSAReceiver} from "../src/bridge/CollarTSAReceiver.sol";
import {ICollarTSA} from "../src/interfaces/ICollarTSA.sol";
import {ICollarLoanStore} from "../src/interfaces/ICollarLoanStore.sol";
import {ISocketMessageTracker} from "../src/interfaces/ISocketMessageTracker.sol";
import {SocketMessageTrackerMock} from "../src/mocks/SocketMessageTrackerMock.sol";
import {LZEndpointV2Mock} from "../src/mocks/LZEndpointV2Mock.sol";

/// @dev Deploy L2 protocol components.
///
/// Required env vars:
/// - ADMIN (address)
/// - L1_MESSENGER (address)           (for setPeer)
/// - L1_VAULT (address)               (vaultRecipient)
/// - OUTPUT_JSON (string)
///
/// Optional:
/// - LZ_ENDPOINT (address)            (if omitted, deploys a placeholder mock endpoint)
/// - SOCKET_TRACKER (address)         (if omitted, deploys SocketMessageTrackerMock)
/// - LOAN_STORE (address)             (if omitted, deploys CollarLoanStore)
/// - TSA_PROXY (address)              (if omitted, deploys ERC1967 proxy)
/// - TSA_IMPLEMENTATION (address)     (if omitted and TSA_PROXY not provided, deploys CollarTSA implementation)
/// - TSA_INIT_DATA (bytes)            (initializer calldata for TSA proxy, default: 0x)
/// - L1_EID (uint32)                  (default: 0)
contract DeployL2 is Script {
    function run() external {
        address admin = vm.envAddress("ADMIN");

        address l1Messenger = vm.envAddress("L1_MESSENGER");
        address l1Vault = vm.envAddress("L1_VAULT");

        address lzEndpoint = vm.envOr("LZ_ENDPOINT", address(0));
        address socketTracker = vm.envOr("SOCKET_TRACKER", address(0));
        address loanStoreAddr = vm.envOr("LOAN_STORE", address(0));

        address tsaProxyAddr = vm.envOr("TSA_PROXY", address(0));
        address tsaImplementation = vm.envOr("TSA_IMPLEMENTATION", address(0));
        bytes memory tsaInitData = vm.envOr("TSA_INIT_DATA", bytes(""));

        uint32 l1Eid = uint32(vm.envOr("L1_EID", uint256(0)));

        vm.startBroadcast();

        if (lzEndpoint == address(0)) {
            lzEndpoint = address(new LZEndpointV2Mock());
        }

        if (socketTracker == address(0)) {
            socketTracker = address(new SocketMessageTrackerMock());
        }

        if (loanStoreAddr == address(0)) {
            loanStoreAddr = address(new CollarLoanStore(admin));
        }

        if (tsaProxyAddr == address(0)) {
            if (tsaImplementation == address(0)) {
                tsaImplementation = address(new CollarTSA());
            }

            tsaProxyAddr = address(new ERC1967Proxy(tsaImplementation, tsaInitData));
        }

        CollarTSAReceiver receiver = new CollarTSAReceiver(
            admin,
            lzEndpoint,
            ISocketMessageTracker(socketTracker),
            ICollarTSA(tsaProxyAddr),
            ICollarLoanStore(loanStoreAddr),
            l1Eid
        );

        // Receiver writes mandate/collateral/consumed state into loan store.
        bytes32 writerRole = CollarLoanStore(loanStoreAddr).WRITER_ROLE();
        CollarLoanStore(loanStoreAddr).grantRole(writerRole, address(receiver));

        receiver.setVaultRecipient(l1Vault);
        // Allow messages from the L1 messenger (right-aligned bytes32 encoding).
        receiver.setPeer(l1Eid, bytes32(uint256(uint160(l1Messenger))));

        vm.stopBroadcast();

        string memory outPath = vm.envString("OUTPUT_JSON");

        string memory json;
        json = vm.serializeAddress("addrs", "l2Receiver", address(receiver));
        json = vm.serializeAddress("addrs", "l2SocketTracker", socketTracker);
        json = vm.serializeAddress("addrs", "l2LoanStore", loanStoreAddr);
        json = vm.serializeAddress("addrs", "l2Tsa", tsaProxyAddr);
        json = vm.serializeAddress("addrs", "l2TsaImplementation", tsaImplementation);
        json = vm.serializeAddress("addrs", "l2LzEndpoint", lzEndpoint);
        vm.writeJson(json, outPath);

        console2.log("L2 receiver", address(receiver));
        console2.log("L2 socketTracker", socketTracker);
        console2.log("L2 loanStore", loanStoreAddr);
        console2.log("L2 tsa(proxy)", tsaProxyAddr);
        console2.log("L2 tsaImplementation", tsaImplementation);
        console2.log("L2 lzEndpoint", lzEndpoint);
        console2.log("Wrote", outPath);
    }
}
