// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Script.sol";

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";

import {IAllowanceTransfer} from "permit2/src/interfaces/IAllowanceTransfer.sol";

import {CollarVault} from "../src/CollarVault.sol";
import {CollarLiquidityVault} from "../src/CollarLiquidityVault.sol";
import {CollarVaultMessenger} from "../src/bridge/CollarVaultMessenger.sol";
import {SocketBridgeAdapter} from "../src/bridge/SocketBridgeAdapter.sol";
import {CollarVaultFinalizeModule} from "../src/modules/CollarVaultFinalizeModule.sol";
import {CollarVaultSettleModule} from "../src/modules/CollarVaultSettleModule.sol";
import {IEulerAdapter} from "../src/interfaces/IEulerAdapter.sol";
import {ILiquidityVault} from "../src/interfaces/ILiquidityVault.sol";
import {ICollarVaultMessenger} from "../src/interfaces/ICollarVaultMessenger.sol";
import {IBridgeAdapter} from "../src/interfaces/IBridgeAdapter.sol";
import {EulerAdapterMock} from "../src/mocks/EulerAdapterMock.sol";
import {LZEndpointV2Mock} from "../src/mocks/LZEndpointV2Mock.sol";

/// @dev Deploy L1 components.
///
/// Safe defaults:
/// - simulation mode unless BROADCAST=true
/// - env-driven inputs only
///
/// Required env vars:
/// - TREASURY (address)
/// - OUTPUT_JSON (string)
///
/// Optional env vars:
/// - ADMIN (address, default broadcaster; deploy runner derives from ACCOUNT keystore)
///
/// Optional env vars:
/// - BROADCAST (bool, default false)
/// - VAULT_OWNER (address, default ADMIN)
/// - PERMIT2 (address, default mainnet Permit2)
/// - L2_RECIPIENT (address, default ADMIN)
/// - LIQUIDITY_VAULT (address, if unset script deploys CollarLiquidityVault)
/// - USDC_ASSET (address, required only when LIQUIDITY_VAULT unset)
/// - EULER_ADAPTER (address, if unset script deploys EulerAdapterMock as placeholder)
/// - LZ_ENDPOINT (address, if unset script deploys LZEndpointV2Mock)
/// - REMOTE_EID (uint32, default 0)
/// - WETH_ASSET/WETH_SOCKET_* + WETH_MSG_GAS_LIMIT/WETH_PAYLOAD_SIZE for optional socket config
contract DeployL1 is Script {
    function run() external {
        bool broadcast = vm.envOr("BROADCAST", false);

        address admin = vm.envOr("ADMIN", msg.sender);
        address treasury = vm.envAddress("TREASURY");

        address vaultOwner = vm.envOr("VAULT_OWNER", admin);

        address defaultPermit2 = 0x000000000022D473030F116dDEE9F6B43aC78BA3;
        address permit2 = vm.envOr("PERMIT2", defaultPermit2);

        address l2Recipient = vm.envOr("L2_RECIPIENT", admin);

        address liquidityVault = vm.envOr("LIQUIDITY_VAULT", address(0));
        address usdcAsset = vm.envOr("USDC_ASSET", address(0));

        // Placeholder adapter only; no Euler deploy/integration in this flow.
        address eulerAdapter = vm.envOr("EULER_ADAPTER", address(0));

        address lzEndpoint = vm.envOr("LZ_ENDPOINT", address(0));
        uint32 remoteEid = uint32(vm.envOr("REMOTE_EID", vm.envOr("L2_EID", uint256(0))));

        address wethAsset = vm.envOr("WETH_ASSET", address(0));
        address wethSocketVault = vm.envOr("WETH_SOCKET_VAULT", address(0));
        address wethSocketBridge = vm.envOr("WETH_SOCKET_BRIDGE", address(0));
        address wethSocketConnector = vm.envOr("WETH_SOCKET_CONNECTOR", address(0));
        uint256 wethMsgGasLimit = vm.envOr("WETH_MSG_GAS_LIMIT", uint256(100_000));
        uint256 wethPayloadSize = vm.envOr("WETH_PAYLOAD_SIZE", uint256(161));

        if (broadcast) vm.startBroadcast();

        if (liquidityVault == address(0)) {
            if (usdcAsset == address(0)) revert("USDC_ASSET required when LIQUIDITY_VAULT is unset");
            CollarLiquidityVault lv =
                new CollarLiquidityVault(IERC20(usdcAsset), "Collar Liquidity Vault", "cLV", admin);
            liquidityVault = address(lv);
        }

        if (eulerAdapter == address(0)) {
            eulerAdapter = address(new EulerAdapterMock());
        }

        if (lzEndpoint == address(0)) {
            lzEndpoint = address(new LZEndpointV2Mock());
        }

        // Deploy implementation + atomically initialize proxy in constructor calldata.
        address vaultImpl = address(new CollarVault());
        bytes memory initData = abi.encodeCall(
            CollarVault.initialize,
            (
                vaultOwner,
                ILiquidityVault(liquidityVault),
                IEulerAdapter(eulerAdapter),
                IAllowanceTransfer(permit2),
                l2Recipient,
                treasury
            )
        );
        address vaultProxy = address(new ERC1967Proxy(vaultImpl, initData));
        CollarVault vault = CollarVault(payable(vaultProxy));

        CollarVaultMessenger messenger = new CollarVaultMessenger(admin, address(vault), lzEndpoint, remoteEid);
        CollarVaultFinalizeModule finalizeModule = new CollarVaultFinalizeModule();
        CollarVaultSettleModule settleModule = new CollarVaultSettleModule();
        vault.setLZMessenger(ICollarVaultMessenger(address(messenger)));
        vault.setFinalizeModule(address(finalizeModule));
        vault.setSettleModule(address(settleModule));

        address wethAdapter = address(0);
        if (wethAsset != address(0) && wethSocketConnector != address(0)) {
            SocketBridgeAdapter.BridgeType bridgeType = SocketBridgeAdapter.BridgeType.NONE;
            if (wethSocketVault != address(0)) {
                bridgeType = SocketBridgeAdapter.BridgeType.OLD;
            } else if (wethSocketBridge != address(0)) {
                bridgeType = SocketBridgeAdapter.BridgeType.NEW;
            }

            if (bridgeType != SocketBridgeAdapter.BridgeType.NONE) {
                SocketBridgeAdapter adapter = new SocketBridgeAdapter(
                    bridgeType,
                    wethSocketBridge,
                    wethSocketVault,
                    wethSocketConnector,
                    wethMsgGasLimit,
                    wethPayloadSize,
                    "",
                    ""
                );
                wethAdapter = address(adapter);
                vault.setSocketVaultConfig(wethAsset, IBridgeAdapter(wethAdapter));
            }
        }

        if (broadcast) vm.stopBroadcast();

        string memory outPath = vm.envString("OUTPUT_JSON");

        string memory json;
        json = vm.serializeAddress("addrs", "l1Vault", address(vault));
        json = vm.serializeAddress("addrs", "l1VaultProxy", address(vault));
        json = vm.serializeAddress("addrs", "l1VaultImplementation", vaultImpl);
        json = vm.serializeAddress("addrs", "l1Messenger", address(messenger));
        json = vm.serializeAddress("addrs", "l1FinalizeModule", address(finalizeModule));
        json = vm.serializeAddress("addrs", "l1SettleModule", address(settleModule));
        json = vm.serializeAddress("addrs", "l1LiquidityVault", liquidityVault);
        json = vm.serializeAddress("addrs", "l1EulerAdapter", eulerAdapter);
        json = vm.serializeAddress("addrs", "l1Permit2", permit2);
        json = vm.serializeAddress("addrs", "l1WethAdapter", wethAdapter);
        vm.writeJson(json, outPath);

        console2.log("broadcast", broadcast);
        console2.log("L1 vault proxy", address(vault));
        console2.log("L1 vault implementation", vaultImpl);
        console2.log("L1 messenger", address(messenger));
        console2.log("L1 finalizeModule", address(finalizeModule));
        console2.log("L1 settleModule", address(settleModule));
        console2.log("L1 liquidityVault", liquidityVault);
        console2.log("L1 eulerAdapter placeholder", eulerAdapter);
        console2.log("L1 permit2", permit2);
        if (wethAdapter != address(0)) {
            console2.log("L1 WETH adapter", wethAdapter);
        }
        console2.log("Wrote", outPath);
    }
}
