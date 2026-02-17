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
import {CollarVaultRolloverModule} from "../src/modules/CollarVaultRolloverModule.sol";
import {IEulerAdapter} from "../src/interfaces/IEulerAdapter.sol";
import {ILiquidityVault} from "../src/interfaces/ILiquidityVault.sol";
import {ICollarVaultMessenger} from "../src/interfaces/ICollarVaultMessenger.sol";
import {IBridgeAdapter} from "../src/interfaces/IBridgeAdapter.sol";
import {EulerAdapterMock} from "../src/mocks/EulerAdapterMock.sol";
import {LZEndpointV2Mock} from "../src/mocks/LZEndpointV2Mock.sol";
import {OptionsBuilder} from "@layerzerolabs/lz-evm-oapp-v2/contracts/oapp/libs/OptionsBuilder.sol";

/// @dev Deploy L1 components.
contract DeployL1 is Script {
    using OptionsBuilder for bytes;

    struct EnvConfig {
        address admin;
        address treasury;
        address vaultOwner;
        address permit2;
        address l2Recipient;
        address liquidityVault;
        address usdcAsset;
        address eulerAdapter;
        address lzEndpoint;
        uint32 l2Eid;
        uint256 lzReceiveGas;
        uint256 lzReceiveValue;
        address wethAsset;
        address wethSocketVault;
        address wethSocketBridge;
        address wethSocketConnector;
        uint256 wethMsgGasLimit;
        uint256 wethPayloadSize;
        uint256 wethStrikeScale;
        address l2WrappedWethAsset;
        string outputJson;
    }

    struct Deployed {
        CollarVault vault;
        address vaultImpl;
        CollarVaultMessenger messenger;
        CollarVaultFinalizeModule finalizeModule;
        CollarVaultSettleModule settleModule;
        CollarVaultRolloverModule rolloverModule;
        address liquidityVault;
        address eulerAdapter;
        address lzEndpoint;
        address wethAdapter;
    }

    function run() external {
        EnvConfig memory cfg = _loadConfig();

        vm.startBroadcast();
        Deployed memory dep = _deploy(cfg);
        vm.stopBroadcast();

        _writeOutput(cfg, dep);
        _logSummary(cfg, dep);
    }

    function _loadConfig() internal view returns (EnvConfig memory cfg) {
        cfg.admin = vm.envOr("ADMIN", msg.sender);
        cfg.treasury = vm.envAddress("TREASURY");
        cfg.vaultOwner = vm.envOr("VAULT_OWNER", cfg.admin);

        address defaultPermit2 = 0x000000000022D473030F116dDEE9F6B43aC78BA3;
        cfg.permit2 = vm.envOr("PERMIT2", defaultPermit2);
        cfg.l2Recipient = vm.envOr("L2_RECIPIENT", cfg.admin);

        cfg.liquidityVault = vm.envOr("LIQUIDITY_VAULT", address(0));
        cfg.usdcAsset = vm.envOr("USDC_ASSET", address(0));
        cfg.eulerAdapter = vm.envOr("EULER_ADAPTER", address(0));
        cfg.lzEndpoint = vm.envOr("LZ_ENDPOINT", address(0));

        cfg.l2Eid = uint32(vm.envOr("L2_EID", vm.envOr("REMOTE_EID", uint256(0))));
        cfg.lzReceiveGas = vm.envOr("LZ_RECEIVE_GAS", uint256(0));
        cfg.lzReceiveValue = vm.envOr("LZ_RECEIVE_VALUE", uint256(0));

        cfg.wethAsset = vm.envOr("WETH_ASSET", address(0));
        cfg.wethSocketVault = vm.envOr("WETH_SOCKET_VAULT", address(0));
        cfg.wethSocketBridge = vm.envOr("WETH_SOCKET_BRIDGE", address(0));
        cfg.wethSocketConnector = vm.envOr("WETH_SOCKET_CONNECTOR", address(0));
        cfg.wethMsgGasLimit = vm.envOr("WETH_MSG_GAS_LIMIT", uint256(100_000));
        cfg.wethPayloadSize = vm.envOr("WETH_PAYLOAD_SIZE", uint256(161));
        cfg.wethStrikeScale = vm.envOr("WETH_STRIKE_SCALE", uint256(1e30));
        cfg.l2WrappedWethAsset = vm.envOr("L2_WRAPPED_WETH_ASSET", address(0));

        cfg.outputJson = vm.envString("OUTPUT_JSON");
    }

    function _deploy(EnvConfig memory cfg) internal returns (Deployed memory dep) {
        dep.liquidityVault = _ensureLiquidityVault(cfg);
        dep.eulerAdapter = _ensureEulerAdapter(cfg);
        dep.lzEndpoint = _ensureLzEndpoint(cfg);

        (dep.vault, dep.vaultImpl) = _deployVault(cfg, dep.liquidityVault, dep.eulerAdapter);
        dep.messenger = _deployMessenger(cfg, dep.vault, dep.lzEndpoint);

        dep.finalizeModule = new CollarVaultFinalizeModule();
        dep.settleModule = new CollarVaultSettleModule();
        dep.rolloverModule = new CollarVaultRolloverModule();

        dep.vault.setLZMessenger(ICollarVaultMessenger(address(dep.messenger)));
        dep.vault.setFinalizeModule(address(dep.finalizeModule));
        dep.vault.setSettleModule(address(dep.settleModule));
        dep.vault.setRolloverModule(address(dep.rolloverModule));

        if (cfg.wethAsset != address(0)) {
            if (cfg.l2WrappedWethAsset == address(0)) revert("L2_WRAPPED_WETH_ASSET required when WETH_ASSET is set");
            dep.vault.setCollateralConfig(cfg.wethAsset, true, cfg.wethStrikeScale, cfg.l2WrappedWethAsset);
        }

        dep.wethAdapter = _maybeDeployWethAdapter(cfg, dep.vault);
    }

    function _ensureLiquidityVault(EnvConfig memory cfg) internal returns (address) {
        if (cfg.liquidityVault != address(0)) return cfg.liquidityVault;
        if (cfg.usdcAsset == address(0)) revert("USDC_ASSET required when LIQUIDITY_VAULT is unset");
        return address(new CollarLiquidityVault(IERC20(cfg.usdcAsset), "Collar Liquidity Vault", "cLV", cfg.admin));
    }

    function _ensureEulerAdapter(EnvConfig memory cfg) internal returns (address) {
        if (cfg.eulerAdapter != address(0)) return cfg.eulerAdapter;
        return address(new EulerAdapterMock());
    }

    function _ensureLzEndpoint(EnvConfig memory cfg) internal returns (address) {
        if (cfg.lzEndpoint != address(0)) return cfg.lzEndpoint;
        return address(new LZEndpointV2Mock());
    }

    function _deployVault(EnvConfig memory cfg, address liquidityVault, address eulerAdapter)
        internal
        returns (CollarVault vault, address vaultImpl)
    {
        vaultImpl = address(new CollarVault());
        bytes memory initData = abi.encodeCall(
            CollarVault.initialize,
            (
                cfg.vaultOwner,
                ILiquidityVault(liquidityVault),
                IEulerAdapter(eulerAdapter),
                IAllowanceTransfer(cfg.permit2),
                cfg.l2Recipient,
                cfg.treasury
            )
        );
        address vaultProxy = address(new ERC1967Proxy(vaultImpl, initData));
        vault = CollarVault(payable(vaultProxy));
    }

    function _deployMessenger(EnvConfig memory cfg, CollarVault vault, address lzEndpoint)
        internal
        returns (CollarVaultMessenger messenger)
    {
        messenger = new CollarVaultMessenger(cfg.admin, address(vault), lzEndpoint, cfg.l2Eid);
        if (cfg.lzReceiveGas > 0) {
            bytes memory lzOptions = OptionsBuilder.newOptions()
                .addExecutorLzReceiveOption(uint128(cfg.lzReceiveGas), uint128(cfg.lzReceiveValue));
            messenger.setDefaultOptions(lzOptions);
        }
    }

    function _maybeDeployWethAdapter(EnvConfig memory cfg, CollarVault vault) internal returns (address) {
        if (cfg.wethAsset == address(0) || cfg.wethSocketConnector == address(0)) return address(0);

        SocketBridgeAdapter.BridgeType bridgeType = SocketBridgeAdapter.BridgeType.NONE;
        if (cfg.wethSocketVault != address(0)) {
            bridgeType = SocketBridgeAdapter.BridgeType.OLD;
        } else if (cfg.wethSocketBridge != address(0)) {
            bridgeType = SocketBridgeAdapter.BridgeType.NEW;
        }

        if (bridgeType == SocketBridgeAdapter.BridgeType.NONE) return address(0);

        SocketBridgeAdapter adapter = new SocketBridgeAdapter(
            bridgeType,
            cfg.wethSocketBridge,
            cfg.wethSocketVault,
            cfg.wethSocketConnector,
            cfg.wethMsgGasLimit,
            cfg.wethPayloadSize,
            "",
            ""
        );
        vault.setSocketVaultConfig(cfg.wethAsset, IBridgeAdapter(address(adapter)));
        return address(adapter);
    }

    function _writeOutput(EnvConfig memory cfg, Deployed memory dep) internal {
        string memory json;
        json = vm.serializeAddress("addrs", "l1Vault", address(dep.vault));
        json = vm.serializeAddress("addrs", "l1VaultProxy", address(dep.vault));
        json = vm.serializeAddress("addrs", "l1VaultImplementation", dep.vaultImpl);
        json = vm.serializeAddress("addrs", "l1Messenger", address(dep.messenger));
        json = vm.serializeAddress("addrs", "l1FinalizeModule", address(dep.finalizeModule));
        json = vm.serializeAddress("addrs", "l1SettleModule", address(dep.settleModule));
        json = vm.serializeAddress("addrs", "l1RolloverModule", address(dep.rolloverModule));
        json = vm.serializeAddress("addrs", "l1LiquidityVault", dep.liquidityVault);
        json = vm.serializeAddress("addrs", "l1EulerAdapter", dep.eulerAdapter);
        json = vm.serializeAddress("addrs", "l1Permit2", cfg.permit2);
        json = vm.serializeAddress("addrs", "l1WethAdapter", dep.wethAdapter);
        vm.writeJson(json, cfg.outputJson);
    }

    function _logSummary(EnvConfig memory cfg, Deployed memory dep) internal view {
        console2.log("L1 vault proxy", address(dep.vault));
        console2.log("L1 vault implementation", dep.vaultImpl);
        console2.log("L1 messenger", address(dep.messenger));
        console2.log("L1 finalizeModule", address(dep.finalizeModule));
        console2.log("L1 settleModule", address(dep.settleModule));
        console2.log("L1 rolloverModule", address(dep.rolloverModule));
        console2.log("L1 liquidityVault", dep.liquidityVault);
        console2.log("L1 eulerAdapter placeholder", dep.eulerAdapter);
        console2.log("L1 permit2", cfg.permit2);

        if (cfg.wethAsset != address(0)) {
            console2.log("L1 collateral enabled", cfg.wethAsset);
            console2.log("L1 collateral strike scale", cfg.wethStrikeScale);
            console2.log("L1->L2 message asset", cfg.l2WrappedWethAsset);
        }
        if (dep.wethAdapter != address(0)) {
            console2.log("L1 WETH adapter", dep.wethAdapter);
        }
        if (cfg.lzReceiveGas > 0) {
            console2.log("L1 messenger defaultOptions receive gas", cfg.lzReceiveGas);
            console2.log("L1 messenger defaultOptions receive value", cfg.lzReceiveValue);
        } else {
            console2.log("L1 messenger defaultOptions not set (LZ_RECEIVE_GAS=0)");
        }
        console2.log("Wrote", cfg.outputJson);
    }
}
