// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Script.sol";
import {MessageHashUtils} from "openzeppelin/utils/cryptography/MessageHashUtils.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {ICollarTSA} from "../src/interfaces/ICollarTSA.sol";
import {ICollarLoanStore} from "../src/interfaces/ICollarLoanStore.sol";

import {IActionVerifier} from "v2-matching/src/interfaces/IActionVerifier.sol";
import {IMatchingModule} from "v2-matching/src/interfaces/IMatchingModule.sol";
import {IRfqModule} from "v2-matching/src/interfaces/IRfqModule.sol";
import {OptionEncoding} from "lyra-utils/encoding/OptionEncoding.sol";

interface ICollarTSAExtended is ICollarTSA {
    function isSigner(address signer) external view returns (bool);
    function isSubmitter(address submitter) external view returns (bool);
    function setSigner(address signer, bool isSigner_) external;
    function setSubmitter(address submitter, bool isSubmitter_) external;

    function initiateDeposit(uint256 amountAssets, address receiver) external returns (uint256);
    function processDeposit(uint256 depositId) external;

    function getSubAccountStats()
        external
        view
        returns (uint256 shortCalls, uint256 baseBalance, int256 cashBalance, uint256 longPuts, uint256 optionPositions);
}

interface IMatchingSmoke {
    function verifyAndMatch(
        IActionVerifier.Action[] calldata actions,
        bytes[] calldata signatures,
        bytes calldata actionData
    ) external;
    function domainSeparator() external view returns (bytes32);
    function getActionHash(IActionVerifier.Action calldata action) external view returns (bytes32);
    function tradeExecutors(address executor) external view returns (bool);
}

interface ICollarReceiverLike {
    function tsa() external view returns (address);
    function loanStore() external view returns (address);
}

interface ICollarLoanStoreWriter is ICollarLoanStore {
    function recordCollateral(uint256 loanId, address collateralAsset, uint256 collateralAmount) external;
}

contract SmokeL2Trade is Script {
    using MessageHashUtils for bytes32;

    struct Cfg {
        address tsa;
        address receiver;
        address loanStore;
        address matching;
        address collateralAsset;
        address optionAsset;
        address rfqModule;
        uint256 tsaNonce;
        uint256 makerNonce;
        uint256 makerSubaccount;
        uint256 tsaSubaccount;
        uint256 loanId;
        uint256 collateralAmount;
        uint256 borrowAmount;
        uint256 expiry;
        uint256 deadline;
        uint256 callStrike;
        uint256 putStrike;
        uint256 callPrice;
        uint256 putPrice;
        uint256 tradeAmount;
        bool runDeposit;
        bool seedCollateral;
        bool configureSigner;
        bool configureSubmitter;
        address signer;
        address submitter;
    }

    function run() external {
        Cfg memory cfg = _load();

        ICollarTSAExtended tsa = ICollarTSAExtended(cfg.tsa);
        ICollarReceiverLike receiver = ICollarReceiverLike(cfg.receiver);
        ICollarLoanStoreWriter loanStore = ICollarLoanStoreWriter(cfg.loanStore);
        IMatchingSmoke matching = IMatchingSmoke(cfg.matching);

        _preflight(cfg, tsa, receiver, matching);

        uint256 adminPk = vm.envUint("L2_SMOKE_ADMIN_PK");
        uint256 signerPk = vm.envUint("L2_SMOKE_SIGNER_PK");
        uint256 executorPk = vm.envUint("L2_SMOKE_EXECUTOR_PK");
        uint256 makerPk = vm.envUint("L2_SMOKE_MAKER_PK");

        if (cfg.configureSigner) {
            vm.startBroadcast(adminPk);
            tsa.setSigner(cfg.signer, true);
            vm.stopBroadcast();
            require(tsa.isSigner(cfg.signer), "signer setup failed");
        }
        if (cfg.configureSubmitter) {
            vm.startBroadcast(adminPk);
            tsa.setSubmitter(cfg.submitter, true);
            vm.stopBroadcast();
            require(tsa.isSubmitter(cfg.submitter), "submitter setup failed");
        }

        vm.startBroadcast(adminPk);
        loanStore.recordMandate(
            cfg.loanId,
            tx.origin,
            cfg.collateralAsset,
            cfg.borrowAmount,
            cfg.callStrike,
            cfg.putStrike,
            uint64(cfg.expiry),
            uint64(cfg.deadline)
        );
        if (cfg.seedCollateral) {
            loanStore.recordCollateral(cfg.loanId, cfg.collateralAsset, cfg.collateralAmount);
        }
        vm.stopBroadcast();

        if (cfg.runDeposit) {
            _runDeposit(cfg, tsa, adminPk);
        }

        (uint256 shortCallsBefore, uint256 baseBefore, int256 cashBefore,, uint256 posBefore) = tsa.getSubAccountStats();

        IRfqModule.TradeData[] memory trades = new IRfqModule.TradeData[](2);
        trades[0] = IRfqModule.TradeData({
            asset: cfg.optionAsset,
            subId: OptionEncoding.toSubId(cfg.expiry, cfg.callStrike, true),
            price: cfg.callPrice,
            amount: int256(cfg.tradeAmount)
        });
        trades[1] = IRfqModule.TradeData({
            asset: cfg.optionAsset,
            subId: OptionEncoding.toSubId(cfg.expiry, cfg.putStrike, false),
            price: cfg.putPrice,
            amount: -int256(cfg.tradeAmount)
        });

        IRfqModule.RfqOrder memory makerOrder = IRfqModule.RfqOrder({maxFee: 0, trades: trades});
        IRfqModule.TakerOrder memory takerOrder =
            IRfqModule.TakerOrder({orderHash: keccak256(abi.encode(trades)), maxFee: 0});

        IActionVerifier.Action[] memory actions = new IActionVerifier.Action[](2);
        bytes[] memory sigs = new bytes[](2);

        address maker = vm.addr(makerPk);
        actions[0] = IActionVerifier.Action({
            subaccountId: cfg.makerSubaccount,
            nonce: cfg.makerNonce,
            module: IMatchingModule(cfg.rfqModule),
            data: abi.encode(makerOrder),
            expiry: block.timestamp + 20 minutes,
            owner: maker,
            signer: maker
        });
        sigs[0] = _signMatchingAction(matching, actions[0], makerPk);

        actions[1] = IActionVerifier.Action({
            subaccountId: cfg.tsaSubaccount,
            nonce: cfg.tsaNonce,
            module: IMatchingModule(cfg.rfqModule),
            data: abi.encode(takerOrder),
            expiry: block.timestamp + 10 minutes,
            owner: cfg.tsa,
            signer: cfg.tsa
        });
        sigs[1] = bytes("");

        vm.startBroadcast(signerPk);
        tsa.signActionData(actions[1], abi.encode(cfg.loanId, abi.encode(trades)));
        vm.stopBroadcast();

        IRfqModule.FillData memory fill = IRfqModule.FillData({
            makerAccount: cfg.makerSubaccount,
            makerFee: 0,
            takerAccount: cfg.tsaSubaccount,
            takerFee: 0,
            managerData: bytes("")
        });

        vm.startBroadcast(executorPk);
        matching.verifyAndMatch(actions, sigs, abi.encode(fill));
        vm.stopBroadcast();

        (uint256 shortCallsAfter, uint256 baseAfter, int256 cashAfter, uint256 longPutsAfter, uint256 posAfter) =
            tsa.getSubAccountStats();

        require(shortCallsAfter >= shortCallsBefore + cfg.tradeAmount, "short calls not added");
        require(longPutsAfter >= cfg.tradeAmount, "long puts missing");
        require(posAfter >= posBefore + 2, "position shape invalid");

        ICollarTSA.CollarTSAParams memory p = tsa.getCollarTSAParams();
        require(cashAfter >= p.maxNegCash, "cash guard broken");
        require(baseAfter <= baseBefore, "base should not increase after collar");

        bool replayed;
        vm.startBroadcast(executorPk);
        try matching.verifyAndMatch(actions, sigs, abi.encode(fill)) {
            replayed = true;
        } catch {}
        vm.stopBroadcast();
        require(!replayed, "replay unexpectedly succeeded");

        console2.log("SmokeL2Trade OK");
        console2.log("loanId", cfg.loanId);
        console2.log("tsa", cfg.tsa);
        console2.log("receiver", cfg.receiver);
        console2.log("loanStore", cfg.loanStore);
        console2.log("baseBefore", baseBefore);
        console2.log("baseAfter", baseAfter);
        console2.log("cashBefore", cashBefore);
        console2.log("cashAfter", cashAfter);
    }

    function _runDeposit(Cfg memory cfg, ICollarTSAExtended tsa, uint256 adminPk) internal {
        uint256 bal = IERC20(cfg.collateralAsset).balanceOf(vm.addr(adminPk));
        require(bal >= cfg.collateralAmount, "insufficient collateral token balance for deposit");

        vm.startBroadcast(adminPk);
        IERC20(cfg.collateralAsset).approve(cfg.tsa, cfg.collateralAmount);
        uint256 depositId = tsa.initiateDeposit(cfg.collateralAmount, vm.addr(adminPk));
        tsa.processDeposit(depositId);
        vm.stopBroadcast();
    }

    function _preflight(Cfg memory cfg, ICollarTSAExtended tsa, ICollarReceiverLike receiver, IMatchingSmoke matching)
        internal
        view
    {
        require(receiver.tsa() == cfg.tsa, "receiver->tsa mismatch");
        require(receiver.loanStore() == cfg.loanStore, "receiver->loanStore mismatch");

        (
            address baseFeed,
            address depositModule,
            address withdrawalModule,
            address tradeModule,
            address rfqModule,
            address optionAsset
        ) = tsa.getCollarTSAAddresses();

        require(baseFeed != address(0), "baseFeed unset");
        require(depositModule != address(0), "depositModule unset");
        require(withdrawalModule != address(0), "withdrawalModule unset");
        require(tradeModule != address(0), "tradeModule unset");
        require(rfqModule == cfg.rfqModule, "rfqModule mismatch");
        require(optionAsset == cfg.optionAsset, "optionAsset mismatch");

        require(matching.tradeExecutors(vm.addr(vm.envUint("L2_SMOKE_EXECUTOR_PK"))), "executor not authorized");
    }

    function _signMatchingAction(IMatchingSmoke matching, IActionVerifier.Action memory action, uint256 pk)
        internal
        view
        returns (bytes memory)
    {
        bytes32 digest = MessageHashUtils.toTypedDataHash(matching.domainSeparator(), matching.getActionHash(action));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, digest);
        return bytes.concat(r, s, bytes1(v));
    }

    function _load() internal view returns (Cfg memory cfg) {
        cfg.tsa = vm.envAddress("L2_SMOKE_TSA");
        cfg.receiver = vm.envAddress("L2_SMOKE_RECEIVER");
        cfg.loanStore = vm.envAddress("L2_SMOKE_LOAN_STORE");
        cfg.matching = vm.envAddress("L2_SMOKE_MATCHING");
        cfg.collateralAsset = vm.envAddress("L2_SMOKE_COLLATERAL_ASSET");
        cfg.optionAsset = vm.envAddress("L2_SMOKE_OPTION_ASSET");
        cfg.rfqModule = vm.envAddress("L2_SMOKE_RFQ_MODULE");

        cfg.tsaSubaccount = vm.envUint("L2_SMOKE_TSA_SUBACCOUNT");
        cfg.makerSubaccount = vm.envUint("L2_SMOKE_MAKER_SUBACCOUNT");
        cfg.tsaNonce = vm.envUint("L2_SMOKE_TSA_NONCE");
        cfg.makerNonce = vm.envUint("L2_SMOKE_MAKER_NONCE");

        cfg.loanId = vm.envUint("L2_SMOKE_LOAN_ID");
        cfg.collateralAmount = vm.envUint("L2_SMOKE_COLLATERAL_AMOUNT");
        cfg.borrowAmount = vm.envUint("L2_SMOKE_BORROW_AMOUNT");

        cfg.expiry = vm.envUint("L2_SMOKE_EXPIRY");
        cfg.deadline = vm.envOr("L2_SMOKE_DEADLINE", cfg.expiry - 60);

        cfg.callStrike = vm.envUint("L2_SMOKE_CALL_STRIKE");
        cfg.putStrike = vm.envUint("L2_SMOKE_PUT_STRIKE");
        cfg.callPrice = vm.envUint("L2_SMOKE_CALL_PRICE");
        cfg.putPrice = vm.envUint("L2_SMOKE_PUT_PRICE");
        cfg.tradeAmount = vm.envUint("L2_SMOKE_TRADE_AMOUNT");

        cfg.runDeposit = vm.envOr("L2_SMOKE_RUN_DEPOSIT", false);
        cfg.seedCollateral = vm.envOr("L2_SMOKE_SEED_COLLATERAL", true);
        cfg.configureSigner = vm.envOr("L2_SMOKE_CONFIGURE_SIGNER", false);
        cfg.configureSubmitter = vm.envOr("L2_SMOKE_CONFIGURE_SUBMITTER", false);
        cfg.signer = vm.envOr("L2_SMOKE_SIGNER", vm.addr(vm.envUint("L2_SMOKE_SIGNER_PK")));
        cfg.submitter = vm.envOr("L2_SMOKE_SUBMITTER", address(0));
    }
}
