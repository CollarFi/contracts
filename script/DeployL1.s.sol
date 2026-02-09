// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Script.sol";

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC4626} from "@openzeppelin/contracts/interfaces/IERC4626.sol";

import {CollarVault} from "../src/CollarVault.sol";
import {CollarLiquidityVault} from "../src/CollarLiquidityVault.sol";
import {CollarVaultMessenger} from "../src/bridge/CollarVaultMessenger.sol";
import {SocketBridgeAdapter} from "../src/bridge/SocketBridgeAdapter.sol";
import {EulerAdapterMock} from "../src/mocks/EulerAdapterMock.sol";
import {LZEndpointV2Mock} from "../src/mocks/LZEndpointV2Mock.sol";

interface IERC4626Like {
    function asset() external view returns (address);
}

/// @dev Deploy L1 components against a fork (mainnet).
///
/// You can either:
/// - supply full dependency addresses (real) OR
/// - use Euler Earn USDC as the idle-yield vault and mock the rest.
///
/// Required env vars:
/// - ADMIN (address)
/// - BRIDGE_CONFIG_ADMIN (address)
/// - TREASURY (address)
/// - OUTPUT_JSON (string)
///
/// Optional (recommended for fork dev):
/// - EULER_EARN_USDC (address)   (default: 0x3B4802FDb0E5d74aA37d58FD77d63e93d4f9A4AF)
/// - PERMIT2 (address)          (default: mainnet Permit2 from Euler metadata)
/// - LIQUIDITY_VAULT (address)  (if omitted, deploys CollarLiquidityVault)
/// - EULER_ADAPTER (address)    (if omitted, deploys EulerAdapterMock)
/// - L2_RECIPIENT (address)     (default: ADMIN)
/// - LZ_ENDPOINT (address)      (if omitted, deploys a placeholder mock endpoint)
/// - L2_EID (uint32)            (default: 0)
/// - VAULT_OWNER (address)      (defaults to ADMIN)
/// - WETH_ASSET (address)       (collateral asset to enable)
/// - WETH_SOCKET_VAULT (address)    (legacy Socket vault)
/// - WETH_SOCKET_BRIDGE (address)   (new bridge controller)
/// - WETH_SOCKET_CONNECTOR (address)
/// - WETH_MSG_GAS_LIMIT (uint256)   (default: 100_000)
/// - WETH_PAYLOAD_SIZE (uint256)    (default: 161)
contract DeployL1 is Script {
    function run() external {
        address admin = vm.envAddress("ADMIN");
        address bridgeConfigAdmin = vm.envAddress("BRIDGE_CONFIG_ADMIN");
        address treasury = vm.envAddress("TREASURY");

        address vaultOwner = vm.envOr("VAULT_OWNER", admin);

        // Defaults from Euler metadata (mainnet)
        address defaultPermit2 = 0x000000000022D473030F116dDEE9F6B43aC78BA3;
        address permit2 = vm.envOr("PERMIT2", defaultPermit2);

        address l2Recipient = vm.envOr("L2_RECIPIENT", admin);

        // If no explicit liquidity vault is provided, deploy CollarLiquidityVault and plug Euler Earn USDC.
        address liquidityVault = vm.envOr("LIQUIDITY_VAULT", address(0));
        address eulerEarnUsdc = vm.envOr("EULER_EARN_USDC", 0x3B4802FDb0E5d74aA37d58FD77d63e93d4f9A4AF);

        // Euler adapter can be mocked
        address eulerAdapter = vm.envOr("EULER_ADAPTER", address(0));

        // LayerZero endpoint can be mocked for fork dev (keeper doesn't rely on sendMessage)
        address lzEndpoint = vm.envOr("LZ_ENDPOINT", address(0));
        uint32 l2Eid = uint32(vm.envOr("L2_EID", uint256(0)));

        // Optional Socket adapter inputs (WETH only for now)
        address wethAsset = vm.envOr("WETH_ASSET", address(0));
        address wethSocketVault = vm.envOr("WETH_SOCKET_VAULT", address(0));
        address wethSocketBridge = vm.envOr("WETH_SOCKET_BRIDGE", address(0));
        address wethSocketConnector = vm.envOr("WETH_SOCKET_CONNECTOR", address(0));
        uint256 wethMsgGasLimit = vm.envOr("WETH_MSG_GAS_LIMIT", uint256(100_000));
        uint256 wethPayloadSize = vm.envOr("WETH_PAYLOAD_SIZE", uint256(161));

        vm.startBroadcast();

        if (liquidityVault == address(0)) {
            address usdc = IERC4626Like(eulerEarnUsdc).asset();
            CollarLiquidityVault lv = new CollarLiquidityVault(IERC20(usdc), "Collar Liquidity Vault", "cLV", admin);
            lv.setEulerVault(IERC4626(eulerEarnUsdc));
            liquidityVault = address(lv);
        }

        if (eulerAdapter == address(0)) {
            eulerAdapter = address(new EulerAdapterMock());
        }

        if (lzEndpoint == address(0)) {
            lzEndpoint = address(new LZEndpointV2Mock());
        }

        CollarVault vault = new CollarVault(
            vaultOwner,
            CollarVault.ILiquidityVault(liquidityVault),
            bridgeConfigAdmin,
            CollarVault.IEulerAdapter(eulerAdapter),
            CollarVault.IAllowanceTransfer(permit2),
            l2Recipient,
            treasury
        );

        CollarVaultMessenger messenger = new CollarVaultMessenger(admin, address(vault), lzEndpoint, l2Eid);

        // Wire the messenger into the vault
        vault.setLZMessenger(CollarVault.ICollarVaultMessenger(address(messenger)));

        // Optional: deploy Socket adapter + set WETH config (legacy vault or new bridge)
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
                vault.setSocketVaultConfig(wethAsset, CollarVault.IBridgeAdapter(wethAdapter));

                // TODO: setCollateralConfig + strikeScale for WETH (await Euler + asset config)
            }
        }

        vm.stopBroadcast();

        string memory outPath = vm.envString("OUTPUT_JSON");

        string memory json;
        json = vm.serializeAddress("addrs", "l1Vault", address(vault));
        json = vm.serializeAddress("addrs", "l1Messenger", address(messenger));
        json = vm.serializeAddress("addrs", "l1LiquidityVault", liquidityVault);
        json = vm.serializeAddress("addrs", "l1EulerAdapter", eulerAdapter);
        json = vm.serializeAddress("addrs", "l1Permit2", permit2);
        json = vm.serializeAddress("addrs", "l1EulerEarnUsdc", eulerEarnUsdc);
        json = vm.serializeAddress("addrs", "l1WethAdapter", wethAdapter);
        vm.writeJson(json, outPath);

        console2.log("L1 vault", address(vault));
        console2.log("L1 messenger", address(messenger));
        console2.log("L1 liquidityVault", liquidityVault);
        console2.log("L1 eulerAdapter", eulerAdapter);
        console2.log("L1 permit2", permit2);
        console2.log("L1 EulerEarn USDC", eulerEarnUsdc);
        if (wethAdapter != address(0)) {
            console2.log("L1 WETH adapter", wethAdapter);
        }
        console2.log("Wrote", outPath);
    }
}
