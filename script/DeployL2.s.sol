// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Script.sol";

import {TransparentUpgradeableProxy} from "@openzeppelin/contracts/proxy/transparent/TransparentUpgradeableProxy.sol";

import {CollarTSA} from "../src/CollarTSA.sol";
import {CollarLoanStore} from "../src/CollarLoanStore.sol";
import {CollarTSAReceiver} from "../src/bridge/CollarTSAReceiver.sol";
import {ICollarTSA} from "../src/interfaces/ICollarTSA.sol";
import {ICollarLoanStore} from "../src/interfaces/ICollarLoanStore.sol";
import {ISocketMessageTracker} from "../src/interfaces/ISocketMessageTracker.sol";
import {LZEndpointV2Mock} from "../src/mocks/LZEndpointV2Mock.sol";

import {BaseTSA} from "v2-matching/src/tokenizedSubaccounts/BaseTSA.sol";
import {ISubAccounts} from "v2-core/src/interfaces/ISubAccounts.sol";
import {DutchAuction} from "v2-core/src/liquidation/DutchAuction.sol";
import {CashAsset} from "v2-core/src/assets/CashAsset.sol";
import {IWrappedERC20Asset} from "v2-core/src/interfaces/IWrappedERC20Asset.sol";
import {ILiquidatableManager} from "v2-core/src/interfaces/ILiquidatableManager.sol";
import {IMatching} from "v2-matching/src/interfaces/IMatching.sol";
import {ISpotFeed} from "v2-core/src/interfaces/ISpotFeed.sol";
import {IDepositModule} from "v2-matching/src/interfaces/IDepositModule.sol";
import {IWithdrawalModule} from "v2-matching/src/interfaces/IWithdrawalModule.sol";
import {ITradeModule} from "v2-matching/src/interfaces/ITradeModule.sol";
import {IRfqModule} from "v2-matching/src/interfaces/IRfqModule.sol";
import {IOptionAsset} from "v2-core/src/interfaces/IOptionAsset.sol";
import {IOptionRiskVerifier} from "../src/interfaces/IOptionRiskVerifier.sol";
import {OptionRiskVerifier} from "../src/verifiers/OptionRiskVerifier.sol";
import {IRfqVerifier} from "../src/interfaces/IRfqVerifier.sol";
import {RfqVerifier} from "../src/verifiers/RfqVerifier.sol";
import {ICollarTsaRfqDelegateModule} from "../src/interfaces/ICollarTsaRfqDelegateModule.sol";
import {CollarTsaRfqDelegateModule} from "../src/modules/CollarTsaRfqDelegateModule.sol";

/// @dev Deploy L2 protocol components.
///
/// Required env vars:
/// - ADMIN (address)
/// - OUTPUT_JSON (string)
///
/// Optional proxy admin owner vars:
/// - PROXY_ADMIN (address, default ADMIN)
///
/// Optional wiring vars (can be set later via script/wire_lz_peers.py and receiver admin calls):
/// - L1_MESSENGER (address)           (for setPeer)
/// - L1_VAULT (address)               (vaultRecipient)
///
/// Optional:
/// - LZ_ENDPOINT (address)            (if omitted, deploys a placeholder mock endpoint)
/// - SOCKET_TRACKER (address)         (required; real socket message tracker)
/// - LOAN_STORE (address)             (if omitted, deploys CollarLoanStore)
/// - TSA_PROXY (address)              (if omitted, deploys ERC1967 proxy)
/// - TSA_IMPLEMENTATION (address)     (if omitted and TSA_PROXY not provided, deploys CollarTSA implementation)
/// - TSA_INIT_DATA (bytes)            (optional explicit initializer calldata for TSA proxy)
/// - L1_EID (uint32)                  (default: 0)
///
/// Auto-init env vars (used when TSA_INIT_DATA is omitted and TSA_PROXY is not provided):
/// - SUBACCOUNTS, AUCTION, CASH, WRAPPED_DEPOSIT_ASSET, MANAGER, MATCHING
/// - BASE_FEED, DEPOSIT_MODULE, WITHDRAWAL_MODULE, TRADE_MODULE, RFQ_MODULE, OPTION_ASSET
/// - OPTION_RISK_VERIFIER (optional; if omitted, deploys OptionRiskVerifier)
/// - RFQ_VERIFIER (optional; if omitted, deploys RfqVerifier)
/// - TSA_INITIAL_OWNER (optional, default ADMIN)
/// - TSA_SYMBOL (optional, default "cTSA"), TSA_NAME (optional, default "Collar TSA")
///
/// Optional CollarTSA risk/signer windows (applied automatically when a new TSA proxy is deployed):
/// - TSA_MIN_SIGNATURE_EXPIRY (default: 1800 = 30 minutes)
/// - TSA_MAX_SIGNATURE_EXPIRY (default: 21600 = 6 hours)
/// - TSA_OPTION_VOL_SLIPPAGE_FACTOR (default: 0.9e18)
/// - TSA_CALL_MAX_DELTA (default: 0.4e18)
/// - TSA_MAX_NEG_CASH (default: -100e18)
/// - TSA_OPTION_MIN_TIME_TO_EXPIRY (default: 1 days)
/// - TSA_OPTION_MAX_TIME_TO_EXPIRY (default: 365 days)
/// - TSA_PUT_MAX_PRICE_FACTOR (default: 1.1e18)
/// - TSA_WORST_SPOT_SELL_PRICE (default: 0.99e18)
contract DeployL2 is Script {
    bytes32 internal constant EIP1967_ADMIN_SLOT = bytes32(uint256(keccak256("eip1967.proxy.admin")) - 1);

    function _proxyAdminOf(address proxy) internal view returns (address) {
        bytes32 raw = vm.load(proxy, EIP1967_ADMIN_SLOT);
        return address(uint160(uint256(raw)));
    }

    function _buildTsaInitData(
        address admin,
        address loanStoreAddr,
        address optionRiskVerifierAddr,
        address rfqVerifierAddr,
        address rfqDelegateModuleAddr
    ) internal view returns (bytes memory) {
        address initialOwner = vm.envOr("TSA_INITIAL_OWNER", admin);

        string memory symbol = vm.envOr("TSA_SYMBOL", string("cTSA"));
        string memory name = vm.envOr("TSA_NAME", string("Collar TSA"));

        BaseTSA.BaseTSAInitParams memory baseInitParams = BaseTSA.BaseTSAInitParams({
            subAccounts: ISubAccounts(vm.envAddress("SUBACCOUNTS")),
            auction: DutchAuction(vm.envAddress("AUCTION")),
            cash: CashAsset(vm.envAddress("CASH")),
            wrappedDepositAsset: IWrappedERC20Asset(vm.envAddress("WRAPPED_DEPOSIT_ASSET")),
            manager: ILiquidatableManager(vm.envAddress("MANAGER")),
            matching: IMatching(vm.envAddress("MATCHING")),
            symbol: symbol,
            name: name
        });

        (CollarTSA.CollarTSAParams memory tsaParams, CollarTSA.CollateralManagementParams memory collateral) =
            _buildDefaultCollarTsaParams();

        CollarTSA.CollarTSAInitParams memory collarInitParams = CollarTSA.CollarTSAInitParams({
            baseFeed: ISpotFeed(vm.envAddress("BASE_FEED")),
            depositModule: IDepositModule(vm.envAddress("DEPOSIT_MODULE")),
            withdrawalModule: IWithdrawalModule(vm.envAddress("WITHDRAWAL_MODULE")),
            tradeModule: ITradeModule(vm.envAddress("TRADE_MODULE")),
            rfqModule: IRfqModule(vm.envAddress("RFQ_MODULE")),
            optionAsset: IOptionAsset(vm.envAddress("OPTION_ASSET")),
            optionRiskVerifier: IOptionRiskVerifier(optionRiskVerifierAddr),
            rfqVerifier: IRfqVerifier(rfqVerifierAddr),
            rfqDelegateModule: ICollarTsaRfqDelegateModule(rfqDelegateModuleAddr),
            loanStore: loanStoreAddr,
            tsaParams: tsaParams,
            collateralManagementParams: collateral
        });

        return abi.encodeCall(CollarTSA.initialize, (initialOwner, baseInitParams, collarInitParams));
    }

    function _buildDefaultCollarTsaParams()
        internal
        view
        returns (CollarTSA.CollarTSAParams memory tsaParams, CollarTSA.CollateralManagementParams memory collateral)
    {
        tsaParams = CollarTSA.CollarTSAParams({
            minSignatureExpiry: vm.envOr("TSA_MIN_SIGNATURE_EXPIRY", uint256(30 minutes)),
            maxSignatureExpiry: vm.envOr("TSA_MAX_SIGNATURE_EXPIRY", uint256(6 hours)),
            optionVolSlippageFactor: vm.envOr("TSA_OPTION_VOL_SLIPPAGE_FACTOR", uint256(0.9e18)),
            callMaxDelta: vm.envOr("TSA_CALL_MAX_DELTA", uint256(0.4e18)),
            maxNegCash: vm.envOr("TSA_MAX_NEG_CASH", int256(-100e18)),
            optionMinTimeToExpiry: vm.envOr("TSA_OPTION_MIN_TIME_TO_EXPIRY", uint256(1 days)),
            optionMaxTimeToExpiry: vm.envOr("TSA_OPTION_MAX_TIME_TO_EXPIRY", uint256(365 days)),
            putMaxPriceFactor: vm.envOr("TSA_PUT_MAX_PRICE_FACTOR", uint256(1.1e18))
        });

        collateral = CollarTSA.CollateralManagementParams({
            worstSpotSellPrice: vm.envOr("TSA_WORST_SPOT_SELL_PRICE", uint256(0.99e18))
        });
    }

    function run() external {
        address admin = vm.envAddress("ADMIN");
        address proxyAdminOwner = vm.envOr("PROXY_ADMIN", admin);

        address l1Messenger = vm.envOr("L1_MESSENGER", address(0));
        address l1Vault = vm.envOr("L1_VAULT", address(0));

        address lzEndpoint = vm.envOr("LZ_ENDPOINT", address(0));
        address socketTracker = vm.envAddress("SOCKET_TRACKER");
        address loanStoreAddr = vm.envOr("LOAN_STORE", address(0));

        address tsaProxyAddr = vm.envOr("TSA_PROXY", address(0));
        address tsaImplementation = vm.envOr("TSA_IMPLEMENTATION", address(0));
        bytes memory tsaInitData = vm.envOr("TSA_INIT_DATA", bytes(""));
        address optionRiskVerifierAddr = vm.envOr("OPTION_RISK_VERIFIER", address(0));
        address rfqVerifierAddr = vm.envOr("RFQ_VERIFIER", address(0));
        address rfqDelegateModuleAddr = vm.envOr("RFQ_DELEGATE_MODULE", address(0));

        uint32 l1Eid = uint32(vm.envOr("L1_EID", uint256(0)));

        vm.startBroadcast();

        if (lzEndpoint == address(0)) {
            lzEndpoint = address(new LZEndpointV2Mock());
        }

        if (loanStoreAddr == address(0)) {
            loanStoreAddr = address(new CollarLoanStore(admin));
        }

        if (optionRiskVerifierAddr == address(0)) {
            optionRiskVerifierAddr = address(new OptionRiskVerifier());
        }
        if (rfqVerifierAddr == address(0)) {
            rfqVerifierAddr = address(new RfqVerifier());
        }
        if (rfqDelegateModuleAddr == address(0)) {
            rfqDelegateModuleAddr = address(new CollarTsaRfqDelegateModule());
        }

        if (tsaProxyAddr == address(0)) {
            if (tsaImplementation == address(0)) {
                tsaImplementation = address(new CollarTSA());
            }

            // Auto-build initializer calldata if not provided explicitly.
            if (tsaInitData.length == 0) {
                tsaInitData = _buildTsaInitData(
                    admin, loanStoreAddr, optionRiskVerifierAddr, rfqVerifierAddr, rfqDelegateModuleAddr
                );
            }

            // Deploy + initialize atomically in transparent proxy constructor.
            // ProxyAdmin owner is PROXY_ADMIN (defaults to ADMIN).
            tsaProxyAddr = address(new TransparentUpgradeableProxy(tsaImplementation, proxyAdminOwner, tsaInitData));
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

        if (l1Vault != address(0)) {
            receiver.setVaultRecipient(l1Vault);
        }
        if (l1Messenger != address(0)) {
            // Allow messages from the L1 messenger (right-aligned bytes32 encoding).
            receiver.setPeer(l1Eid, bytes32(uint256(uint160(l1Messenger))));
        }

        vm.stopBroadcast();

        string memory outPath = vm.envString("OUTPUT_JSON");

        string memory json;
        json = vm.serializeAddress("addrs", "l2Receiver", address(receiver));
        json = vm.serializeAddress("addrs", "l2SocketTracker", socketTracker);
        json = vm.serializeAddress("addrs", "l2LoanStore", loanStoreAddr);
        json = vm.serializeAddress("addrs", "l2Tsa", tsaProxyAddr);
        json = vm.serializeAddress("addrs", "l2TsaImplementation", tsaImplementation);
        json = vm.serializeAddress("addrs", "l2TsaProxyAdmin", _proxyAdminOf(tsaProxyAddr));
        json = vm.serializeAddress("addrs", "l2OptionRiskVerifier", optionRiskVerifierAddr);
        json = vm.serializeAddress("addrs", "l2RfqVerifier", rfqVerifierAddr);
        json = vm.serializeAddress("addrs", "l2RfqDelegateModule", rfqDelegateModuleAddr);
        json = vm.serializeAddress("addrs", "l2LzEndpoint", lzEndpoint);
        vm.writeJson(json, outPath);

        console2.log("L2 receiver", address(receiver));
        console2.log("L2 socketTracker", socketTracker);
        console2.log("L2 loanStore", loanStoreAddr);
        console2.log("L2 tsa(proxy)", tsaProxyAddr);
        console2.log("L2 tsaImplementation", tsaImplementation);
        console2.log("L2 tsa proxy admin", _proxyAdminOf(tsaProxyAddr));
        console2.log("L2 optionRiskVerifier", optionRiskVerifierAddr);
        console2.log("L2 rfqVerifier", rfqVerifierAddr);
        console2.log("L2 lzEndpoint", lzEndpoint);
        console2.log("Wrote", outPath);
    }
}
