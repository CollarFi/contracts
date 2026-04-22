// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";
import {IAllowanceTransfer} from "permit2/src/interfaces/IAllowanceTransfer.sol";
import {DeployPermit2} from "permit2/test/utils/DeployPermit2.sol";

import {CollarLiquidityVault} from "../src/CollarLiquidityVault.sol";
import {CollarVault, ILiquidityVault} from "../src/CollarVault.sol";
import {ILendingAdapter} from "../src/interfaces/ILendingAdapter.sol";
import {IMarginEngine} from "../src/interfaces/IMarginEngine.sol";
import {IMarginEngineRfqRouter} from "../src/interfaces/IMarginEngineRfqRouter.sol";
import {VariableLoanPosition} from "../src/adapters/VariableLoanPosition.sol";
import {MockERC20} from "./mocks/MockERC20.sol";
import {MockEulerAdapter} from "./mocks/MockEulerAdapter.sol";
import {MockMarginEngine} from "./mocks/MockMarginEngine.sol";

contract CollarVaultTest is Test {
    uint256 internal borrowerKey = 0xB0B0;
    uint256 internal rfqSignerKey = 0xA11CE;

    address internal borrower;
    address internal rfqSigner;
    address internal keeper = address(0xCAFE);
    address internal marketMaker = address(0xBEEF);
    address internal treasury = address(0xFEE1);
    address internal rfqRouter = address(0xABCD);

    MockERC20 internal usdc;
    MockERC20 internal wbtc;
    CollarLiquidityVault internal liquidityVault;
    MockEulerAdapter internal lendingAdapter;
    MockMarginEngine internal marginEngine;
    IAllowanceTransfer internal permit2;
    CollarVault internal vault;
    VariableLoanPosition internal positionImpl;

    function setUp() public {
        borrower = vm.addr(borrowerKey);
        rfqSigner = vm.addr(rfqSignerKey);

        usdc = new MockERC20("USD Coin", "USDC", 6);
        wbtc = new MockERC20("Wrapped BTC", "WBTC", 8);
        liquidityVault = new CollarLiquidityVault(usdc, "Collar USDC", "cUSDC", address(this));
        lendingAdapter = new MockEulerAdapter(address(wbtc), address(usdc));
        marginEngine = new MockMarginEngine(address(usdc), address(this));
        permit2 = IAllowanceTransfer(new DeployPermit2().deployPermit2());
        positionImpl = new VariableLoanPosition();

        CollarVault impl = new CollarVault();
        bytes memory initData = abi.encodeCall(
            CollarVault.initialize,
            (
                address(this),
                ILiquidityVault(address(liquidityVault)),
                ILendingAdapter(address(lendingAdapter)),
                permit2,
                address(marginEngine),
                treasury
            )
        );
        vault = CollarVault(payable(address(new ERC1967Proxy(address(impl), initData))));

        liquidityVault.grantRole(liquidityVault.VAULT_ROLE(), address(vault));

        marginEngine.setProtocolOwner(address(vault));
        marginEngine.setMarketMaker(marketMaker, true);
        marginEngine.setOracleUpdater(address(this), true);

        vault.setTreasuryConfig(treasury, 2_000);
        vault.setCollateralConfig(address(wbtc), true, 1e8, address(wbtc));
        vault.setVariableLoanPositionImplementation(address(positionImpl));
        vault.setOriginationFeeApr(0.1e18);
        vault.setMaxRollLtv(0.999e18);
        vault.setReadyLoanCloseGracePeriod(2 days);
        vault.grantRole(vault.KEEPER_ROLE(), keeper);
        vault.grantRole(vault.RFQ_SIGNER_ROLE(), rfqSigner);

        usdc.mint(address(this), 2_000_000e6);
        usdc.approve(address(liquidityVault), type(uint256).max);
        liquidityVault.deposit(2_000_000e6, address(this));
        usdc.mint(address(this), 1_000_000e6);
        usdc.mint(address(lendingAdapter), 1_000_000e6);
        lendingAdapter.setLiquidity(1_000_000e6);

        usdc.mint(marketMaker, 1_000_000e6);
        wbtc.mint(borrower, 20e8);
    }

    function testCreateDepositWithMandateAndFinalize() public {
        (uint256 loanId, CollarVault.BaselineRfq memory rfq) =
            _createPendingWithMandate(1e8, 21_000e6, 26_000e6, 20_000e6, 30 days);
        uint256 putBucketId = _preparePutBucket(rfq, 1e8);

        vm.prank(keeper);
        vault.finalizeLoan(loanId, CollarVault.FinalizeLoanParams({putBucketId: putBucketId, callBuyer: marketMaker}));

        CollarVault.Loan memory loan = vault.getLoan(loanId);
        assertEq(uint256(loan.state), uint256(CollarVault.LoanState.ACTIVE_ZERO_COST));
        assertEq(loan.putBucketId, putBucketId);
        assertEq(loan.callStrike, rfq.callStrike);
        assertEq(usdc.balanceOf(borrower), rfq.borrowAmount);
    }

    function testSetMarginEngineRfqRouter() public {
        vm.expectEmit(true, false, false, true);
        emit CollarVault.MarginEngineRfqRouterUpdated(rfqRouter);

        vault.setMarginEngineRfqRouter(IMarginEngineRfqRouter(rfqRouter));

        assertEq(address(vault.marginEngineRfqRouter()), rfqRouter);
    }

    function testSetMarginEngineRfqRouterRevertsOnZeroAddress() public {
        vm.expectRevert(CollarVault.CV_InvalidConfig.selector);
        vault.setMarginEngineRfqRouter(IMarginEngineRfqRouter(address(0)));
    }

    function testSetMarginEngineRfqRouterRevertsWithoutParameterRole() public {
        vm.prank(borrower);
        vm.expectRevert();
        vault.setMarginEngineRfqRouter(IMarginEngineRfqRouter(rfqRouter));
    }

    function testPrepareRolloverCallBucket() public {
        (uint256 loanId,) = _createFinalizedLoan(1e8, 21_000e6, 26_000e6, 20_000e6, 30 days);
        uint64 newMaturity = uint64(block.timestamp + 45 days);
        uint256 newCallStrike = 28_000e6;
        bytes32 expectedInstrumentId = marginEngine.registerInstrument(
            address(wbtc), address(usdc), address(wbtc), newMaturity, newCallStrike, IMarginEngine.OptionType.Call
        );

        vm.expectEmit(true, true, true, true);
        emit CollarVault.RolloverCallBucketPrepared(loanId, expectedInstrumentId, 3);

        vm.prank(keeper);
        (bytes32 callInstrumentId, uint256 callBucketId) =
            vault.prepareRolloverCallBucket(loanId, newMaturity, newCallStrike);

        assertEq(callInstrumentId, expectedInstrumentId);
        assertEq(callBucketId, 3);
        (bytes32 bucketInstrumentId, uint8 bucketType, address owner,,,,,,,,,,,,,) = marginEngine.buckets(callBucketId);
        assertEq(uint256(bucketType), 1);
        assertEq(bucketInstrumentId, expectedInstrumentId);
        assertEq(owner, address(vault));
    }

    function testPrepareRolloverCallBucketRevertsIfInstrumentMissing() public {
        (uint256 loanId,) = _createFinalizedLoan(1e8, 21_000e6, 26_000e6, 20_000e6, 30 days);

        vm.prank(keeper);
        vm.expectRevert(CollarVault.CV_InvalidConfig.selector);
        vault.prepareRolloverCallBucket(loanId, uint64(block.timestamp + 45 days), 28_000e6);
    }

    function testPrepareRolloverCallBucketRevertsPostMaturity() public {
        (uint256 loanId,) = _createFinalizedLoan(1e8, 21_000e6, 26_000e6, 20_000e6, 2 days);
        uint64 newMaturity = uint64(block.timestamp + 10 days);
        uint256 newCallStrike = 28_000e6;
        marginEngine.registerInstrument(
            address(wbtc), address(usdc), address(wbtc), newMaturity, newCallStrike, IMarginEngine.OptionType.Call
        );

        vm.warp(block.timestamp + 3 days);
        vm.prank(keeper);
        vm.expectRevert(CollarVault.CV_InvalidState.selector);
        vault.prepareRolloverCallBucket(loanId, newMaturity, newCallStrike);
    }

    function testHashRolloverMandateRecoversBorrowerSigner() public view {
        CollarVault.RolloverMandate memory mandate = CollarVault.RolloverMandate({
            borrower: borrower,
            loanId: 42,
            newMaturity: uint64(block.timestamp + 45 days),
            minCallStrike: 28_000e6,
            maxPutStrike: 22_000e6,
            minNetInterest: 10e6,
            deadline: uint64(block.timestamp + 1 days),
            nonce: 7
        });
        bytes32 digest = vault.hashRolloverMandate(mandate);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(borrowerKey, digest);

        assertEq(ecrecover(digest, v, r, s), borrower);
    }

    function testAcceptMandateRevertsOnInvalidSignature() public {
        CollarVault.DepositParams memory params = _depositParams(1e8, 21_000e6, 20_000e6, 30 days);
        CollarVault.BaselineRfq memory rfq = _rfq(0, params, 26_000e6, 0);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(uint256(0xBAD), vault.hashBaselineRfq(rfq));

        vm.startPrank(borrower);
        wbtc.approve(address(vault), params.collateralAmount);
        vm.expectRevert(CollarVault.CV_Unauthorized.selector);
        vault.createDepositWithMandate(params, rfq, abi.encodePacked(r, s, v), uint64(block.timestamp + 1 days));
        vm.stopPrank();
    }

    function testAcceptMandateRejectsReplay() public {
        CollarVault.DepositParams memory params = _depositParams(1e8, 21_000e6, 20_000e6, 30 days);

        vm.prank(borrower);
        wbtc.approve(address(vault), params.collateralAmount * 2);

        CollarVault.BaselineRfq memory sentinel = _rfq(0, params, 26_000e6, 0);
        bytes memory sig = _signBaselineRfq(sentinel);

        vm.startPrank(borrower);
        vault.createDepositWithMandate(params, sentinel, sig, uint64(block.timestamp + 1 days));
        vm.expectRevert(CollarVault.CV_InvalidMessage.selector);
        vault.createDepositWithMandate(params, sentinel, sig, uint64(block.timestamp + 1 days));
        vm.stopPrank();
    }

    function testRequestCollateralReturnAfterMandateExpiry() public {
        (uint256 loanId,) = _createPendingWithMandate(1e8, 21_000e6, 26_000e6, 20_000e6, 30 days);
        vm.warp(block.timestamp + 2 days);

        uint256 beforeBal = wbtc.balanceOf(borrower);
        vm.prank(borrower);
        vault.requestCollateralReturn(loanId);
        assertEq(wbtc.balanceOf(borrower), beforeBal + 1e8);
        assertEq(vault.getPendingDeposit(loanId).borrower, address(0));
    }

    function testSettleNeutralMakesLoanReadyAndConvertible() public {
        uint256 loanId = _createFinalizeAndSettleSpot(1e8, 21_000e6, 26_000e6, 20_000e6, 30 days, 24_000e6);

        vm.warp(block.timestamp + 30 days);
        vault.settleLoan(loanId, CollarVault.SettlementOutcome.Neutral, bytes32(0));

        CollarVault.Loan memory readyLoan = vault.getLoan(loanId);
        assertEq(uint256(readyLoan.state), uint256(CollarVault.LoanState.READY_FOR_VARIABLE));

        bool converted = vault.tryConvertReadyLoan(loanId);
        assertTrue(converted);

        CollarVault.Loan memory variableLoan = vault.getLoan(loanId);
        assertEq(uint256(variableLoan.state), uint256(CollarVault.LoanState.ACTIVE_VARIABLE));
    }

    function testSettleCallItmPaysBorrowerExcess() public {
        uint256 loanId = _createFinalizeAndSettleSpot(1e8, 21_000e6, 26_000e6, 20_000e6, 30 days, 31_000e6);
        vm.warp(block.timestamp + 30 days);

        uint256 beforeBorrowerUsdc = usdc.balanceOf(borrower);
        usdc.approve(address(vault), type(uint256).max);
        vault.settleLoan(loanId, CollarVault.SettlementOutcome.CallITM, bytes32(0));

        CollarVault.Loan memory loan = vault.getLoan(loanId);
        assertEq(uint256(loan.state), uint256(CollarVault.LoanState.CLOSED));
        assertGt(usdc.balanceOf(borrower), beforeBorrowerUsdc);
    }

    function testSettlePutItmSplitsExcessToTreasuryAndVault() public {
        uint256 loanId = _createFinalizeAndSettleSpot(1e8, 25_000e6, 30_000e6, 20_000e6, 30 days, 0);
        vm.warp(block.timestamp + 30 days);

        uint256 treasuryBefore = usdc.balanceOf(treasury);
        uint256 vaultBefore = usdc.balanceOf(address(liquidityVault));
        vault.settleLoan(loanId, CollarVault.SettlementOutcome.PutITM, bytes32(0));

        assertGt(usdc.balanceOf(treasury), treasuryBefore);
        assertGt(usdc.balanceOf(address(liquidityVault)), vaultBefore);
    }

    function testSettleAtExactExpiryBoundary() public {
        uint256 loanId = _createFinalizeAndSettleSpot(1e8, 21_000e6, 26_000e6, 20_000e6, 30 days, 24_000e6);
        vm.warp(block.timestamp + 30 days);
        vault.settleLoan(loanId, CollarVault.SettlementOutcome.Neutral, bytes32(0));
        assertEq(uint256(vault.getLoan(loanId).state), uint256(CollarVault.LoanState.READY_FOR_VARIABLE));
    }

    function testCannotDoubleSettle() public {
        uint256 loanId = _createFinalizeAndSettleSpot(1e8, 21_000e6, 26_000e6, 20_000e6, 30 days, 31_000e6);
        vm.warp(block.timestamp + 30 days);
        usdc.approve(address(vault), type(uint256).max);
        vault.settleLoan(loanId, CollarVault.SettlementOutcome.CallITM, bytes32(0));

        vm.expectRevert(CollarVault.CV_InvalidState.selector);
        vault.settleLoan(loanId, CollarVault.SettlementOutcome.CallITM, bytes32(0));
    }

    function testSettleHugeSpotSettlementDeterministic() public {
        uint256 loanId = _createFinalizeAndSettleSpot(1e8, 21_000e6, 26_000e6, 20_000e6, 30 days, 5_000_000e6);
        vm.warp(block.timestamp + 30 days);

        usdc.approve(address(vault), type(uint256).max);
        vault.settleLoan(loanId, CollarVault.SettlementOutcome.CallITM, bytes32(0));
        assertEq(uint256(vault.getLoan(loanId).state), uint256(CollarVault.LoanState.CLOSED));
    }

    function _createFinalizeAndSettleSpot(
        uint256 collateralAmount,
        uint256 putStrike,
        uint256 callStrike,
        uint256 borrowAmount,
        uint256 tenor,
        uint256 finalSpot
    ) internal returns (uint256 loanId) {
        (loanId,) = _createFinalizedLoan(collateralAmount, putStrike, callStrike, borrowAmount, tenor);

        vm.warp(block.timestamp + tenor);
        marginEngine.updateInstrumentOracle(vault.getLoan(loanId).putInstrumentId, 0, 0, finalSpot);
        marginEngine.updateInstrumentOracle(vault.getLoan(loanId).callInstrumentId, 0, 0, finalSpot);
        vm.warp(block.timestamp - tenor);
    }

    function _createFinalizedLoan(
        uint256 collateralAmount,
        uint256 putStrike,
        uint256 callStrike,
        uint256 borrowAmount,
        uint256 tenor
    ) internal returns (uint256 loanId, CollarVault.BaselineRfq memory rfq) {
        (loanId, rfq) = _createPendingWithMandate(collateralAmount, putStrike, callStrike, borrowAmount, tenor);
        uint256 putBucketId = _preparePutBucket(rfq, collateralAmount);

        vm.prank(keeper);
        vault.finalizeLoan(loanId, CollarVault.FinalizeLoanParams({putBucketId: putBucketId, callBuyer: marketMaker}));
    }

    function _createPendingWithMandate(
        uint256 collateralAmount,
        uint256 putStrike,
        uint256 callStrike,
        uint256 borrowAmount,
        uint256 tenor
    ) internal returns (uint256 loanId, CollarVault.BaselineRfq memory rfq) {
        CollarVault.DepositParams memory params = _depositParams(collateralAmount, putStrike, borrowAmount, tenor);
        rfq = _rfq(0, params, callStrike, 0);
        bytes memory sig = _signBaselineRfq(rfq);

        vm.startPrank(borrower);
        wbtc.approve(address(vault), collateralAmount);
        (loanId,,,) = vault.createDepositWithMandate(params, rfq, sig, uint64(block.timestamp + 1 days));
        vm.stopPrank();

        rfq.loanId = loanId;
    }

    function _preparePutBucket(CollarVault.BaselineRfq memory rfq, uint256 quantity)
        internal
        returns (uint256 putBucketId)
    {
        bytes32 putInstrumentId = marginEngine.registerInstrument(
            address(wbtc), address(usdc), address(usdc), rfq.maturity, rfq.putStrike, IMarginEngine.OptionType.Put
        );
        marginEngine.registerInstrument(
            address(wbtc), address(usdc), address(wbtc), rfq.maturity, rfq.callStrike, IMarginEngine.OptionType.Call
        );
        marginEngine.updateInstrumentOracle(putInstrumentId, 0, 0, rfq.putStrike);

        vm.startPrank(marketMaker);
        putBucketId = marginEngine.createPutBucket(putInstrumentId, marketMaker);
        usdc.approve(address(marginEngine), type(uint256).max);
        marginEngine.depositPutCollateral(putBucketId, 1_000_000e6);
        marginEngine.issuePut(putBucketId, quantity, address(vault));
        vm.stopPrank();
    }

    function _depositParams(uint256 collateralAmount, uint256 putStrike, uint256 borrowAmount, uint256 tenor)
        internal
        view
        returns (CollarVault.DepositParams memory)
    {
        return CollarVault.DepositParams({
            collateralAsset: address(wbtc),
            collateralAmount: collateralAmount,
            maturity: block.timestamp + tenor,
            putStrike: putStrike,
            borrowAmount: borrowAmount
        });
    }

    function _rfq(uint256 loanId, CollarVault.DepositParams memory params, uint256 callStrike, uint256 minNetInterest)
        internal
        view
        returns (CollarVault.BaselineRfq memory)
    {
        return CollarVault.BaselineRfq({
            loanId: loanId,
            collateralAsset: params.collateralAsset,
            collateralAmount: params.collateralAmount,
            maturity: uint64(params.maturity),
            putStrike: params.putStrike,
            callStrike: callStrike,
            borrowAmount: params.borrowAmount,
            minNetInterest: minNetInterest,
            rfqExpiry: uint64(block.timestamp + 1 days),
            borrower: borrower,
            nonce: uint256(keccak256(abi.encode(loanId, callStrike, params.maturity)))
        });
    }

    function _signBaselineRfq(CollarVault.BaselineRfq memory rfq) internal view returns (bytes memory) {
        bytes32 digest = vault.hashBaselineRfq(rfq);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(rfqSignerKey, digest);
        return abi.encodePacked(r, s, v);
    }
}
