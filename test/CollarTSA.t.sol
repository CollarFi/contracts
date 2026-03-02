// SPDX-License-Identifier: GPL-3.0-only
pragma solidity ^0.8.20;

import {IActionVerifier} from "v2-matching/src/interfaces/IActionVerifier.sol";
import {IRfqModule} from "v2-matching/src/interfaces/IRfqModule.sol";
import {OptionEncoding} from "lyra-utils/encoding/OptionEncoding.sol";

import {CollarTSA} from "../src/CollarTSA.sol";
import {CollarTSATestUtils} from "./utils/CollarTSATestUtils.sol";

contract CollarTSA_ValidationTests is CollarTSATestUtils {
    function setUp() public override {
        MARKET = "weth";
        super.setUp();
        deployPredeposit(address(0));
        upgradeToCollarTSA(MARKET);
        setupCollarTSA();
    }

    function testAllowsShortCallAndLongPut() public {
        _depositToTSA(10e18);
        _executeDeposit(10e18);

        uint64 expiry = uint64(block.timestamp + 7 days);
        _setForwardPrice(MARKET, expiry, 2000e18, 1e18);
        _setFixedSVIDataForExpiry(MARKET, expiry);

        IRfqModule.TradeData[] memory trades = new IRfqModule.TradeData[](2);
        trades[0] = IRfqModule.TradeData({
            asset: address(markets[MARKET].option),
            subId: OptionEncoding.toSubId(expiry, 2200e18, true),
            price: 100e18,
            amount: 1e18
        });
        trades[1] = IRfqModule.TradeData({
            asset: address(markets[MARKET].option),
            subId: OptionEncoding.toSubId(expiry, 2000e18, false),
            price: 100e18,
            amount: -1e18
        });

        IRfqModule.TakerOrder memory order =
            IRfqModule.TakerOrder({orderHash: keccak256(abi.encode(trades)), maxFee: 0});

        IActionVerifier.Action memory action = IActionVerifier.Action({
            subaccountId: tsaSubacc,
            nonce: ++tsaNonce,
            module: rfqModule,
            data: abi.encode(order),
            expiry: block.timestamp + 8 minutes,
            owner: address(tsa),
            signer: address(tsa)
        });

        _seedLoan(1, expiry);

        vm.prank(signer);
        collarTsa.signActionData(action, abi.encode(uint256(1), abi.encode(trades)));
    }

    function testAllowsCashWithdrawal() public {
        uint256 usdcAmount = 1_000e6;
        usdc.mint(address(this), usdcAmount);
        usdc.approve(address(cash), usdcAmount);
        cash.deposit(tsaSubacc, usdcAmount);

        IActionVerifier.Action memory action = IActionVerifier.Action({
            subaccountId: tsaSubacc,
            nonce: ++tsaNonce,
            module: withdrawalModule,
            data: _encodeWithdrawData(500e6, address(cash)),
            expiry: block.timestamp + 8 minutes,
            owner: address(tsa),
            signer: address(tsa)
        });

        vm.prank(signer);
        collarTsa.signActionData(action, "");
    }

    function testRejectsCashWithdrawalWhenInsufficient() public {
        uint256 usdcAmount = 100e6;
        usdc.mint(address(this), usdcAmount);
        usdc.approve(address(cash), usdcAmount);
        cash.deposit(tsaSubacc, usdcAmount);

        IActionVerifier.Action memory action = IActionVerifier.Action({
            subaccountId: tsaSubacc,
            nonce: ++tsaNonce,
            module: withdrawalModule,
            data: _encodeWithdrawData(200e6, address(cash)),
            expiry: block.timestamp + 8 minutes,
            owner: address(tsa),
            signer: address(tsa)
        });

        vm.prank(signer);
        vm.expectRevert(CollarTSA.CTSA_WithdrawalNegativeCash.selector);
        collarTsa.signActionData(action, "");
    }

    function testRejectsLongCall() public {
        _depositToTSA(1e18);
        _executeDeposit(1e18);

        uint64 expiry = uint64(block.timestamp + 7 days);
        _setForwardPrice(MARKET, expiry, 2000e18, 1e18);
        _setFixedSVIDataForExpiry(MARKET, expiry);

        IRfqModule.TradeData[] memory trades = new IRfqModule.TradeData[](2);
        trades[0] = IRfqModule.TradeData({
            asset: address(markets[MARKET].option),
            subId: OptionEncoding.toSubId(expiry, 2200e18, true),
            price: 100e18,
            amount: -1e18
        });
        trades[1] = IRfqModule.TradeData({
            asset: address(markets[MARKET].option),
            subId: OptionEncoding.toSubId(expiry, 2000e18, false),
            price: 1e18,
            amount: -1e18
        });

        IRfqModule.TakerOrder memory order =
            IRfqModule.TakerOrder({orderHash: keccak256(abi.encode(trades)), maxFee: 0});

        IActionVerifier.Action memory action = IActionVerifier.Action({
            subaccountId: tsaSubacc,
            nonce: ++tsaNonce,
            module: rfqModule,
            data: abi.encode(order),
            expiry: block.timestamp + 8 minutes,
            owner: address(tsa),
            signer: address(tsa)
        });

        _seedLoan(1, expiry);

        vm.prank(signer);
        vm.expectRevert(CollarTSA.CTSA_CanOnlyOpenShortCalls.selector);
        collarTsa.signActionData(action, abi.encode(uint256(1), abi.encode(trades)));
    }

    function testRejectsShortPut() public {
        _depositToTSA(1e18);
        _executeDeposit(1e18);

        uint64 expiry = uint64(block.timestamp + 7 days);
        _setForwardPrice(MARKET, expiry, 2000e18, 1e18);
        _setFixedSVIDataForExpiry(MARKET, expiry);

        IRfqModule.TradeData[] memory trades = new IRfqModule.TradeData[](2);
        // Maker is long put. Taker (TSA) becomes short put, which should be rejected.
        trades[0] = IRfqModule.TradeData({
            asset: address(markets[MARKET].option),
            subId: OptionEncoding.toSubId(expiry, 2200e18, true),
            price: 100e18,
            amount: 1e18
        });
        trades[1] = IRfqModule.TradeData({
            asset: address(markets[MARKET].option),
            subId: OptionEncoding.toSubId(expiry, 1800e18, false),
            price: 1e18,
            amount: 1e18
        });

        IRfqModule.TakerOrder memory order =
            IRfqModule.TakerOrder({orderHash: keccak256(abi.encode(trades)), maxFee: 0});

        IActionVerifier.Action memory action = IActionVerifier.Action({
            subaccountId: tsaSubacc,
            nonce: ++tsaNonce,
            module: rfqModule,
            data: abi.encode(order),
            expiry: block.timestamp + 8 minutes,
            owner: address(tsa),
            signer: address(tsa)
        });

        _seedLoan(1, expiry);

        vm.prank(signer);
        vm.expectRevert(CollarTSA.CTSA_OnlyLongPutsAllowed.selector);
        collarTsa.signActionData(action, abi.encode(uint256(1), abi.encode(trades)));
    }

    function testPutPriceTooHigh() public {
        _depositToTSA(1e18);
        _executeDeposit(1e18);

        uint64 expiry = uint64(block.timestamp + 7 days);
        _setForwardPrice(MARKET, expiry, 2000e18, 1e18);
        _setFixedSVIDataForExpiry(MARKET, expiry);

        CollarTSA.CollarTSAParams memory params = collarTsa.getCollarTSAParams();
        params.putMaxPriceFactor = 1e18;
        collarTsa.setCollarTSAParams(params);

        IRfqModule.TradeData[] memory trades = new IRfqModule.TradeData[](2);
        trades[0] = IRfqModule.TradeData({
            asset: address(markets[MARKET].option),
            subId: OptionEncoding.toSubId(expiry, 2200e18, true),
            price: 100e18,
            amount: 1e18
        });
        trades[1] = IRfqModule.TradeData({
            asset: address(markets[MARKET].option),
            subId: OptionEncoding.toSubId(expiry, 1800e18, false),
            price: 10_000e18,
            amount: -1e18
        });

        IRfqModule.TakerOrder memory order =
            IRfqModule.TakerOrder({orderHash: keccak256(abi.encode(trades)), maxFee: 0});

        IActionVerifier.Action memory action = IActionVerifier.Action({
            subaccountId: tsaSubacc,
            nonce: ++tsaNonce,
            module: rfqModule,
            data: abi.encode(order),
            expiry: block.timestamp + 8 minutes,
            owner: address(tsa),
            signer: address(tsa)
        });

        _seedLoan(1, expiry);

        vm.prank(signer);
        vm.expectRevert(CollarTSA.CTSA_PutPriceTooHigh.selector);
        collarTsa.signActionData(action, abi.encode(uint256(1), abi.encode(trades)));
    }

    function testRejectsTakerOrderHashMismatch() public {
        _depositToTSA(1e18);
        _executeDeposit(1e18);

        uint64 expiry = uint64(block.timestamp + 7 days);
        IRfqModule.TradeData[] memory trades = new IRfqModule.TradeData[](2);
        trades[0] = IRfqModule.TradeData({
            asset: address(markets[MARKET].option),
            subId: OptionEncoding.toSubId(expiry, 2200e18, true),
            price: 100e18,
            amount: 1e18
        });
        trades[1] = IRfqModule.TradeData({
            asset: address(markets[MARKET].option),
            subId: OptionEncoding.toSubId(expiry, 1800e18, false),
            price: 1e18,
            amount: -1e18
        });

        IRfqModule.TakerOrder memory order = IRfqModule.TakerOrder({orderHash: keccak256("mismatch"), maxFee: 0});

        IActionVerifier.Action memory action = IActionVerifier.Action({
            subaccountId: tsaSubacc,
            nonce: ++tsaNonce,
            module: rfqModule,
            data: abi.encode(order),
            expiry: block.timestamp + 8 minutes,
            owner: address(tsa),
            signer: address(tsa)
        });

        _seedLoan(1, expiry);

        vm.prank(signer);
        vm.expectRevert(CollarTSA.CTSA_TradeDataDoesNotMatchOrderHash.selector);
        collarTsa.signActionData(action, abi.encode(uint256(1), abi.encode(trades)));
    }

    function _prepareRollover(uint256 loanId, uint64 oldExpiry, uint64 newExpiry, uint256 minNetInterest) internal {
        if (loanStore.getLoan(loanId).borrower == address(0)) {
            _seedLoan(loanId, oldExpiry);
        }
        loanStore.recordRolloverMandate(
            loanId,
            address(0xB0B0),
            keccak256("rollover"),
            2100e18,
            2100e18,
            minNetInterest,
            20e18,
            1e18,
            1e18,
            newExpiry,
            uint64(block.timestamp + 1 days)
        );
    }

    function _rolloverTrades(uint64 oldExpiry, uint64 newExpiry)
        internal
        pure
        returns (IRfqModule.TradeData[] memory trades)
    {
        trades = new IRfqModule.TradeData[](4);
        trades[0] = IRfqModule.TradeData({
            asset: address(0), // overwritten by caller
            subId: OptionEncoding.toSubId(oldExpiry, 2200e18, true),
            price: 100e18,
            amount: -1e18
        });
        trades[1] = IRfqModule.TradeData({
            asset: address(0), // overwritten by caller
            subId: OptionEncoding.toSubId(oldExpiry, 2000e18, false),
            price: 1e18,
            amount: 1e18
        });
        trades[2] = IRfqModule.TradeData({
            asset: address(0), // overwritten by caller
            subId: OptionEncoding.toSubId(newExpiry, 2200e18, true),
            price: 140e18,
            amount: 1e18
        });
        trades[3] = IRfqModule.TradeData({
            asset: address(0), // overwritten by caller
            subId: OptionEncoding.toSubId(newExpiry, 2000e18, false),
            price: 50e18,
            amount: -1e18
        });
    }

    function _setOptionAsset(IRfqModule.TradeData[] memory trades) internal view {
        for (uint256 i = 0; i < trades.length; i++) {
            trades[i].asset = address(markets[MARKET].option);
        }
    }

    function _signRolloverRfq(uint256 loanId, IRfqModule.TradeData[] memory trades) internal {
        IRfqModule.TakerOrder memory order =
            IRfqModule.TakerOrder({orderHash: keccak256(abi.encode(trades)), maxFee: 0});

        IActionVerifier.Action memory action = IActionVerifier.Action({
            subaccountId: tsaSubacc,
            nonce: ++tsaNonce,
            module: rfqModule,
            data: abi.encode(order),
            expiry: block.timestamp + 8 minutes,
            owner: address(tsa),
            signer: address(tsa)
        });

        vm.prank(signer);
        collarTsa.signActionData(action, abi.encode(loanId, abi.encode(trades)));
    }

    function testAllowsRolloverWithFourLegPortfolioTransition() public {
        _depositToTSA(10e18);
        _executeDeposit(10e18);

        uint64 oldExpiry = uint64(block.timestamp + 7 days);
        uint64 newExpiry = uint64(block.timestamp + 14 days);

        _setForwardPrice(MARKET, oldExpiry, 2000e18, 1e18);
        _setFixedSVIDataForExpiry(MARKET, oldExpiry);
        _setForwardPrice(MARKET, newExpiry, 2000e18, 1e18);
        _setFixedSVIDataForExpiry(MARKET, newExpiry);

        _openCollarPosition(1e18, oldExpiry);
        _prepareRollover(1, oldExpiry, newExpiry, 10e18);

        IRfqModule.TradeData[] memory trades = _rolloverTrades(oldExpiry, newExpiry);
        _setOptionAsset(trades);

        _signRolloverRfq(1, trades);
    }

    function testRejectsRolloverMissingCloseLeg() public {
        _depositToTSA(10e18);
        _executeDeposit(10e18);

        uint64 oldExpiry = uint64(block.timestamp + 7 days);
        uint64 newExpiry = uint64(block.timestamp + 14 days);

        _setForwardPrice(MARKET, oldExpiry, 2000e18, 1e18);
        _setFixedSVIDataForExpiry(MARKET, oldExpiry);
        _setForwardPrice(MARKET, newExpiry, 2000e18, 1e18);
        _setFixedSVIDataForExpiry(MARKET, newExpiry);

        _openCollarPosition(1e18, oldExpiry);
        _prepareRollover(1, oldExpiry, newExpiry, 10e18);

        IRfqModule.TradeData[] memory trades = new IRfqModule.TradeData[](3);
        IRfqModule.TradeData[] memory full = _rolloverTrades(oldExpiry, newExpiry);
        _setOptionAsset(full);
        trades[0] = full[0];
        trades[1] = full[2];
        trades[2] = full[3];

        vm.expectRevert(bytes4(keccak256("CTSA_InvalidRfqTradeLength()")));
        _signRolloverRfq(1, trades);
    }

    function testRejectsRolloverWrongExpiryOnLeg() public {
        _depositToTSA(10e18);
        _executeDeposit(10e18);

        uint64 oldExpiry = uint64(block.timestamp + 7 days);
        uint64 newExpiry = uint64(block.timestamp + 14 days);

        _setForwardPrice(MARKET, oldExpiry, 2000e18, 1e18);
        _setFixedSVIDataForExpiry(MARKET, oldExpiry);
        _setForwardPrice(MARKET, newExpiry, 2000e18, 1e18);
        _setFixedSVIDataForExpiry(MARKET, newExpiry);

        _openCollarPosition(1e18, oldExpiry);
        _prepareRollover(1, oldExpiry, newExpiry, 10e18);

        IRfqModule.TradeData[] memory trades = _rolloverTrades(oldExpiry, newExpiry);
        _setOptionAsset(trades);
        trades[0].subId = OptionEncoding.toSubId(newExpiry, 2200e18, true);

        vm.expectRevert(CollarTSA.CTSA_InvalidRfqTradeDetails.selector);
        _signRolloverRfq(1, trades);
    }

    function testRejectsRolloverWrongDirectionOnLeg() public {
        _depositToTSA(10e18);
        _executeDeposit(10e18);

        uint64 oldExpiry = uint64(block.timestamp + 7 days);
        uint64 newExpiry = uint64(block.timestamp + 14 days);

        _setForwardPrice(MARKET, oldExpiry, 2000e18, 1e18);
        _setFixedSVIDataForExpiry(MARKET, oldExpiry);
        _setForwardPrice(MARKET, newExpiry, 2000e18, 1e18);
        _setFixedSVIDataForExpiry(MARKET, newExpiry);

        _openCollarPosition(1e18, oldExpiry);
        _prepareRollover(1, oldExpiry, newExpiry, 10e18);

        IRfqModule.TradeData[] memory trades = _rolloverTrades(oldExpiry, newExpiry);
        _setOptionAsset(trades);
        trades[3].amount = 1e18;

        vm.expectRevert(CollarTSA.CTSA_InvalidRfqTradeDetails.selector);
        _signRolloverRfq(1, trades);
    }

    function testRejectsRolloverOnEconomicsGuardFailure() public {
        _depositToTSA(10e18);
        _executeDeposit(10e18);

        uint64 oldExpiry = uint64(block.timestamp + 7 days);
        uint64 newExpiry = uint64(block.timestamp + 14 days);

        _setForwardPrice(MARKET, oldExpiry, 2000e18, 1e18);
        _setFixedSVIDataForExpiry(MARKET, oldExpiry);
        _setForwardPrice(MARKET, newExpiry, 2000e18, 1e18);
        _setFixedSVIDataForExpiry(MARKET, newExpiry);

        _openCollarPosition(1e18, oldExpiry);
        _prepareRollover(1, oldExpiry, newExpiry, 100e18);

        IRfqModule.TradeData[] memory trades = _rolloverTrades(oldExpiry, newExpiry);
        _setOptionAsset(trades);

        vm.expectRevert(CollarTSA.CTSA_InsufficientCash.selector);
        _signRolloverRfq(1, trades);
    }

    function _openCollarPosition(int256 amount, uint64 expiry) internal {
        _setForwardPrice(MARKET, expiry, 2000e18, 1e18);
        _setFixedSVIDataForExpiry(MARKET, expiry);

        IRfqModule.TradeData[] memory trades = new IRfqModule.TradeData[](2);
        trades[0] = IRfqModule.TradeData({
            asset: address(markets[MARKET].option),
            subId: OptionEncoding.toSubId(expiry, 2200e18, true),
            price: 100e18,
            amount: amount
        });
        trades[1] = IRfqModule.TradeData({
            asset: address(markets[MARKET].option),
            subId: OptionEncoding.toSubId(expiry, 2000e18, false),
            price: 100e18,
            amount: -amount
        });

        IRfqModule.RfqOrder memory order = IRfqModule.RfqOrder({maxFee: 0, trades: trades});
        IRfqModule.TakerOrder memory takerOrder =
            IRfqModule.TakerOrder({orderHash: keccak256(abi.encode(trades)), maxFee: 0});

        IActionVerifier.Action[] memory actions = new IActionVerifier.Action[](2);
        bytes[] memory signatures = new bytes[](2);

        (actions[0], signatures[0]) = _createActionAndSign(
            nonVaultSubacc,
            ++nonVaultNonce,
            address(rfqModule),
            abi.encode(order),
            block.timestamp + 1 days,
            nonVaultAddr,
            nonVaultAddr,
            nonVaultPk
        );

        actions[1] = IActionVerifier.Action({
            subaccountId: tsaSubacc,
            nonce: ++tsaNonce,
            module: rfqModule,
            data: abi.encode(takerOrder),
            expiry: block.timestamp + 8 minutes,
            owner: address(tsa),
            signer: address(tsa)
        });

        _seedLoan(1, expiry);

        vm.prank(signer);
        tsa.signActionData(actions[1], abi.encode(uint256(1), abi.encode(trades)));

        IRfqModule.FillData memory fill = IRfqModule.FillData({
            makerAccount: nonVaultSubacc, takerAccount: tsaSubacc, makerFee: 0, takerFee: 0, managerData: bytes("")
        });

        _verifyAndMatch(actions, signatures, abi.encode(fill));
    }

    function testAllowsSpotRfqSellAsTaker() public {
        _depositToTSA(2e18);
        _executeDeposit(2e18);

        IRfqModule.TradeData[] memory trades = new IRfqModule.TradeData[](1);
        trades[0] = IRfqModule.TradeData({
            asset: address(markets[MARKET].base), subId: 0, price: MARKET_REF_SPOT, amount: 1e18
        });

        IRfqModule.TakerOrder memory order =
            IRfqModule.TakerOrder({orderHash: keccak256(abi.encode(trades)), maxFee: 0});

        IActionVerifier.Action memory action = IActionVerifier.Action({
            subaccountId: tsaSubacc,
            nonce: ++tsaNonce,
            module: rfqModule,
            data: abi.encode(order),
            expiry: block.timestamp + 8 minutes,
            owner: address(tsa),
            signer: address(tsa)
        });

        _seedLoan(1, 0);

        vm.prank(signer);
        collarTsa.signActionData(action, abi.encode(uint256(1), abi.encode(trades)));
    }

    function testRejectsSpotRfqSellAsMaker() public {
        _depositToTSA(2e18);
        _executeDeposit(2e18);

        IRfqModule.TradeData[] memory trades = new IRfqModule.TradeData[](1);
        trades[0] = IRfqModule.TradeData({
            asset: address(markets[MARKET].base), subId: 0, price: MARKET_REF_SPOT, amount: 1e18
        });

        IRfqModule.RfqOrder memory order = IRfqModule.RfqOrder({maxFee: 0, trades: trades});

        IActionVerifier.Action memory action = IActionVerifier.Action({
            subaccountId: tsaSubacc,
            nonce: ++tsaNonce,
            module: rfqModule,
            data: abi.encode(order),
            expiry: block.timestamp + 8 minutes,
            owner: address(tsa),
            signer: address(tsa)
        });

        vm.prank(signer);
        vm.expectRevert(CollarTSA.CTSA_SpotRfqRequiresTaker.selector);
        collarTsa.signActionData(action, "");
    }
}
