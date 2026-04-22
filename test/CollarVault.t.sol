// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
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
import {MockMarginEngineRfqRouter} from "./mocks/MockMarginEngineRfqRouter.sol";

contract CollarVaultTest is Test {
    uint256 internal borrowerKey = 0xB0B0;
    uint256 internal rfqSignerKey = 0xA11CE;

    address internal borrower;
    address internal rfqSigner;
    address internal keeper = address(0xCAFE);
    address internal marketMaker = address(0xBEEF);
    address internal treasury = address(0xFEE1);
    address internal feeRecipient = address(0xFEE2);
    address internal rfqRouter = address(0xABCD);

    MockERC20 internal usdc;
    MockERC20 internal wbtc;
    CollarLiquidityVault internal liquidityVault;
    MockEulerAdapter internal lendingAdapter;
    MockMarginEngine internal marginEngine;
    MockMarginEngineRfqRouter internal marginEngineRfqRouter;
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
        marginEngineRfqRouter = new MockMarginEngineRfqRouter(marginEngine, feeRecipient);
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
        vm.prank(marketMaker);
        usdc.approve(address(marginEngineRfqRouter), type(uint256).max);
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

    function testExecuteRolloverThroughMarginEngineRfqRouter() public {
        vault.setMarginEngineRfqRouter(IMarginEngineRfqRouter(address(marginEngineRfqRouter)));
        marginEngineRfqRouter.setProtocolFeeConfig(100, feeRecipient);

        (uint256 loanId,) = _createFinalizedLoan(1e8, 21_000e6, 26_000e6, 20_000e6, 30 days);
        CollarVault.Loan memory oldLoan = vault.getLoan(loanId);

        vm.warp(block.timestamp + 5 days);

        uint64 newMaturity = uint64(block.timestamp + 40 days);
        uint256 newPutStrike = 22_000e6;
        uint256 newCallStrike = 28_000e6;
        bytes32 newPutInstrumentId = marginEngine.registerInstrument(
            address(wbtc), address(usdc), address(usdc), newMaturity, newPutStrike, IMarginEngine.OptionType.Put
        );
        bytes32 newCallInstrumentId = marginEngine.registerInstrument(
            address(wbtc), address(usdc), address(wbtc), newMaturity, newCallStrike, IMarginEngine.OptionType.Call
        );
        marginEngine.updateInstrumentOracle(newPutInstrumentId, 0, 0, newPutStrike);

        usdc.mint(marketMaker, 1_000_000e6);
        vm.startPrank(marketMaker);
        uint256 newPutBucketId = marginEngine.createPutBucket(newPutInstrumentId, marketMaker);
        marginEngine.depositPutCollateral(newPutBucketId, 1_000_000e6);
        vm.stopPrank();
        usdc.mint(marketMaker, 200e6);

        vm.prank(keeper);
        (, uint256 newCallBucketId) = vault.prepareRolloverCallBucket(loanId, newMaturity, newCallStrike);

        (address oldLongCallToken,) = marginEngine.getBucketTokens(oldLoan.callBucketId);
        vm.prank(marketMaker);
        IERC20(oldLongCallToken).approve(address(marginEngineRfqRouter), type(uint256).max);

        CollarVault.RolloverMandate memory mandate = CollarVault.RolloverMandate({
            borrower: borrower,
            loanId: loanId,
            newMaturity: newMaturity,
            minCallStrike: newCallStrike,
            maxPutStrike: newPutStrike,
            minNetInterest: 20e6,
            deadline: uint64(block.timestamp + 1 days),
            nonce: 77
        });

        IMarginEngineRfqRouter.Action[] memory actions = new IMarginEngineRfqRouter.Action[](4);
        actions[0] = IMarginEngineRfqRouter.Action({
            side: IMarginEngineRfqRouter.Side.Sell,
            instrumentType: IMarginEngineRfqRouter.InstrumentType.Put,
            fulfillmentType: IMarginEngineRfqRouter.FulfillmentType.Transfer,
            bucketId: oldLoan.putBucketId,
            instrumentId: oldLoan.putInstrumentId,
            quantity: oldLoan.collateralAmount,
            quoteAmount: 50e6,
            maker: marketMaker,
            longRecipient: marketMaker,
            longSource: address(vault),
            cappedRecipient: address(0),
            cappedSource: address(0),
            collateralRecipient: address(0)
        });
        actions[1] = IMarginEngineRfqRouter.Action({
            side: IMarginEngineRfqRouter.Side.Buy,
            instrumentType: IMarginEngineRfqRouter.InstrumentType.Call,
            fulfillmentType: IMarginEngineRfqRouter.FulfillmentType.Burn,
            bucketId: oldLoan.callBucketId,
            instrumentId: oldLoan.callInstrumentId,
            quantity: oldLoan.collateralAmount,
            quoteAmount: 20e6,
            maker: marketMaker,
            longRecipient: address(0),
            longSource: marketMaker,
            cappedRecipient: address(0),
            cappedSource: address(vault),
            collateralRecipient: address(vault)
        });
        actions[2] = IMarginEngineRfqRouter.Action({
            side: IMarginEngineRfqRouter.Side.Sell,
            instrumentType: IMarginEngineRfqRouter.InstrumentType.Call,
            fulfillmentType: IMarginEngineRfqRouter.FulfillmentType.Mint,
            bucketId: newCallBucketId,
            instrumentId: newCallInstrumentId,
            quantity: oldLoan.collateralAmount,
            quoteAmount: 40e6,
            maker: marketMaker,
            longRecipient: marketMaker,
            longSource: address(0),
            cappedRecipient: address(vault),
            cappedSource: address(0),
            collateralRecipient: address(0)
        });
        actions[3] = IMarginEngineRfqRouter.Action({
            side: IMarginEngineRfqRouter.Side.Buy,
            instrumentType: IMarginEngineRfqRouter.InstrumentType.Put,
            fulfillmentType: IMarginEngineRfqRouter.FulfillmentType.Mint,
            bucketId: newPutBucketId,
            instrumentId: newPutInstrumentId,
            quantity: oldLoan.collateralAmount,
            quoteAmount: 30e6,
            maker: marketMaker,
            longRecipient: address(vault),
            longSource: address(0),
            cappedRecipient: address(0),
            cappedSource: address(0),
            collateralRecipient: address(0)
        });

        IMarginEngineRfqRouter.Quote memory quote = IMarginEngineRfqRouter.Quote({
            taker: address(vault),
            authorizedExecutor: address(vault),
            quoteAsset: address(usdc),
            validUntil: uint64(block.timestamp + 1 hours),
            nonce: 101,
            salt: 202,
            actions: actions
        });

        IMarginEngineRfqRouter.SignerSignature[] memory signatures = new IMarginEngineRfqRouter.SignerSignature[](1);
        signatures[0] = IMarginEngineRfqRouter.SignerSignature({signer: marketMaker, signature: hex"01"});

        bytes32 mandateDigest = vault.hashRolloverMandate(mandate);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(borrowerKey, mandateDigest);

        uint256 liquidityUsdcBefore = usdc.balanceOf(address(liquidityVault));

        vm.prank(keeper);
        bytes32 quoteHash = vault.executeRollover(loanId, mandate, abi.encodePacked(r, s, v), quote, signatures);

        CollarVault.Loan memory rolledLoan = vault.getLoan(loanId);
        assertEq(rolledLoan.maturity, newMaturity);
        assertEq(rolledLoan.putStrike, newPutStrike);
        assertEq(rolledLoan.callStrike, newCallStrike);
        assertEq(rolledLoan.putBucketId, newPutBucketId);
        assertEq(rolledLoan.callBucketId, newCallBucketId);
        assertEq(rolledLoan.putInstrumentId, newPutInstrumentId);
        assertEq(rolledLoan.callInstrumentId, newCallInstrumentId);
        assertEq(rolledLoan.startTime, block.timestamp);
        assertEq(quoteHash, marginEngineRfqRouter.lastQuoteHash());

        uint256 oldAccruedInterest =
            _quoteInterest(oldLoan.principal, oldLoan.interestApr, oldLoan.startTime, block.timestamp);
        uint256 remainingOldInterest = oldLoan.interestOwed - oldAccruedInterest;
        uint256 newInterest = _quoteInterest(oldLoan.principal, 0.1e18, block.timestamp, newMaturity);
        assertEq(rolledLoan.interestOwed, remainingOldInterest + newInterest);

        (address oldLongPutToken,) = marginEngine.getBucketTokens(oldLoan.putBucketId);
        (, address oldCappedToken) = marginEngine.getBucketTokens(oldLoan.callBucketId);
        (, address newCappedToken) = marginEngine.getBucketTokens(newCallBucketId);
        (address newLongPutToken,) = marginEngine.getBucketTokens(newPutBucketId);
        assertEq(IERC20(oldLongPutToken).balanceOf(address(vault)), 0);
        assertEq(IERC20(oldCappedToken).balanceOf(address(vault)), 0);
        assertEq(IERC20(newCappedToken).balanceOf(address(vault)), oldLoan.collateralAmount);
        assertEq(IERC20(newLongPutToken).balanceOf(address(vault)), oldLoan.collateralAmount);
        assertEq(usdc.balanceOf(address(vault)), 0);
        assertEq(usdc.balanceOf(address(liquidityVault)) - liquidityUsdcBefore, 38_600_000);

        uint256 oldCallOutstanding = _bucketOutstanding(oldLoan.callBucketId);
        uint256 newCallOutstanding = _bucketOutstanding(newCallBucketId);
        uint256 newPutOutstanding = _bucketOutstanding(newPutBucketId);
        assertEq(oldCallOutstanding, 0);
        assertEq(newCallOutstanding, oldLoan.collateralAmount);
        assertEq(newPutOutstanding, oldLoan.collateralAmount);
    }

    function testExecuteRolloverRevertsOnInvalidQuoteShape() public {
        vault.setMarginEngineRfqRouter(IMarginEngineRfqRouter(address(marginEngineRfqRouter)));

        (uint256 loanId,) = _createFinalizedLoan(1e8, 21_000e6, 26_000e6, 20_000e6, 30 days);
        CollarVault.Loan memory loan = vault.getLoan(loanId);

        vm.warp(block.timestamp + 5 days);

        uint64 newMaturity = uint64(block.timestamp + 40 days);
        uint256 newPutStrike = 22_000e6;
        uint256 newCallStrike = 28_000e6;
        bytes32 newPutInstrumentId = marginEngine.registerInstrument(
            address(wbtc), address(usdc), address(usdc), newMaturity, newPutStrike, IMarginEngine.OptionType.Put
        );
        bytes32 newCallInstrumentId = marginEngine.registerInstrument(
            address(wbtc), address(usdc), address(wbtc), newMaturity, newCallStrike, IMarginEngine.OptionType.Call
        );
        marginEngine.updateInstrumentOracle(newPutInstrumentId, 0, 0, newPutStrike);

        usdc.mint(marketMaker, 1_000_000e6);
        vm.startPrank(marketMaker);
        uint256 newPutBucketId = marginEngine.createPutBucket(newPutInstrumentId, marketMaker);
        marginEngine.depositPutCollateral(newPutBucketId, 1_000_000e6);
        vm.stopPrank();
        usdc.mint(marketMaker, 200e6);

        vm.prank(keeper);
        (, uint256 newCallBucketId) = vault.prepareRolloverCallBucket(loanId, newMaturity, newCallStrike);

        CollarVault.RolloverMandate memory mandate = CollarVault.RolloverMandate({
            borrower: borrower,
            loanId: loanId,
            newMaturity: newMaturity,
            minCallStrike: newCallStrike,
            maxPutStrike: newPutStrike,
            minNetInterest: 20e6,
            deadline: uint64(block.timestamp + 1 days),
            nonce: 88
        });

        IMarginEngineRfqRouter.Action[] memory actions = new IMarginEngineRfqRouter.Action[](3);
        actions[0] = IMarginEngineRfqRouter.Action({
            side: IMarginEngineRfqRouter.Side.Sell,
            instrumentType: IMarginEngineRfqRouter.InstrumentType.Put,
            fulfillmentType: IMarginEngineRfqRouter.FulfillmentType.Transfer,
            bucketId: loan.putBucketId,
            instrumentId: loan.putInstrumentId,
            quantity: loan.collateralAmount,
            quoteAmount: 50e6,
            maker: marketMaker,
            longRecipient: marketMaker,
            longSource: address(vault),
            cappedRecipient: address(0),
            cappedSource: address(0),
            collateralRecipient: address(0)
        });
        actions[1] = IMarginEngineRfqRouter.Action({
            side: IMarginEngineRfqRouter.Side.Buy,
            instrumentType: IMarginEngineRfqRouter.InstrumentType.Put,
            fulfillmentType: IMarginEngineRfqRouter.FulfillmentType.Mint,
            bucketId: newPutBucketId,
            instrumentId: newPutInstrumentId,
            quantity: loan.collateralAmount,
            quoteAmount: 30e6,
            maker: marketMaker,
            longRecipient: address(vault),
            longSource: address(0),
            cappedRecipient: address(0),
            cappedSource: address(0),
            collateralRecipient: address(0)
        });
        actions[2] = IMarginEngineRfqRouter.Action({
            side: IMarginEngineRfqRouter.Side.Sell,
            instrumentType: IMarginEngineRfqRouter.InstrumentType.Call,
            fulfillmentType: IMarginEngineRfqRouter.FulfillmentType.Mint,
            bucketId: newCallBucketId,
            instrumentId: newCallInstrumentId,
            quantity: loan.collateralAmount,
            quoteAmount: 40e6,
            maker: marketMaker,
            longRecipient: marketMaker,
            longSource: address(0),
            cappedRecipient: address(vault),
            cappedSource: address(0),
            collateralRecipient: address(0)
        });

        IMarginEngineRfqRouter.Quote memory quote = IMarginEngineRfqRouter.Quote({
            taker: address(vault),
            authorizedExecutor: address(vault),
            quoteAsset: address(usdc),
            validUntil: uint64(block.timestamp + 1 hours),
            nonce: 303,
            salt: 404,
            actions: actions
        });

        IMarginEngineRfqRouter.SignerSignature[] memory signatures = new IMarginEngineRfqRouter.SignerSignature[](1);
        signatures[0] = IMarginEngineRfqRouter.SignerSignature({signer: marketMaker, signature: hex"01"});

        bytes32 mandateDigest = vault.hashRolloverMandate(mandate);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(borrowerKey, mandateDigest);

        vm.prank(keeper);
        vm.expectRevert(CollarVault.CV_InvalidMessage.selector);
        vault.executeRollover(loanId, mandate, abi.encodePacked(r, s, v), quote, signatures);
    }

    function testExecuteRolloverRevertsWhenCarriedInterestBreaksRollLtv() public {
        vault.setMarginEngineRfqRouter(IMarginEngineRfqRouter(address(marginEngineRfqRouter)));
        vault.setMaxRollLtv(0.998e18);

        (uint256 loanId,) = _createFinalizedLoan(1e8, 21_000e6, 26_000e6, 20_000e6, 30 days);
        CollarVault.Loan memory loan = vault.getLoan(loanId);

        vm.warp(block.timestamp + 20 days);

        uint64 newMaturity = uint64(block.timestamp + 40 days);
        uint256 newPutStrike = 20_300e6;
        uint256 newCallStrike = 28_000e6;
        bytes32 newPutInstrumentId = marginEngine.registerInstrument(
            address(wbtc), address(usdc), address(usdc), newMaturity, newPutStrike, IMarginEngine.OptionType.Put
        );
        bytes32 newCallInstrumentId = marginEngine.registerInstrument(
            address(wbtc), address(usdc), address(wbtc), newMaturity, newCallStrike, IMarginEngine.OptionType.Call
        );
        marginEngine.updateInstrumentOracle(newPutInstrumentId, 0, 0, newPutStrike);

        usdc.mint(marketMaker, 1_000_000e6);
        vm.startPrank(marketMaker);
        uint256 newPutBucketId = marginEngine.createPutBucket(newPutInstrumentId, marketMaker);
        marginEngine.depositPutCollateral(newPutBucketId, 1_000_000e6);
        vm.stopPrank();
        usdc.mint(marketMaker, 200e6);

        vm.prank(keeper);
        (, uint256 newCallBucketId) = vault.prepareRolloverCallBucket(loanId, newMaturity, newCallStrike);

        (address oldLongCallToken,) = marginEngine.getBucketTokens(loan.callBucketId);
        vm.prank(marketMaker);
        IERC20(oldLongCallToken).approve(address(marginEngineRfqRouter), type(uint256).max);

        CollarVault.RolloverMandate memory mandate = CollarVault.RolloverMandate({
            borrower: borrower,
            loanId: loanId,
            newMaturity: newMaturity,
            minCallStrike: newCallStrike,
            maxPutStrike: newPutStrike,
            minNetInterest: 20e6,
            deadline: uint64(block.timestamp + 1 days),
            nonce: 99
        });

        IMarginEngineRfqRouter.Action[] memory actions = _rolloverActions(
            loan, marketMaker, newPutBucketId, newPutInstrumentId, newCallBucketId, newCallInstrumentId
        );

        IMarginEngineRfqRouter.Quote memory quote = IMarginEngineRfqRouter.Quote({
            taker: address(vault),
            authorizedExecutor: address(vault),
            quoteAsset: address(usdc),
            validUntil: uint64(block.timestamp + 1 hours),
            nonce: 505,
            salt: 606,
            actions: actions
        });

        IMarginEngineRfqRouter.SignerSignature[] memory signatures = new IMarginEngineRfqRouter.SignerSignature[](1);
        signatures[0] = IMarginEngineRfqRouter.SignerSignature({signer: marketMaker, signature: hex"01"});

        bytes32 mandateDigest = vault.hashRolloverMandate(mandate);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(borrowerKey, mandateDigest);

        vm.prank(keeper);
        vm.expectRevert(CollarVault.CV_InsufficientValue.selector);
        vault.executeRollover(loanId, mandate, abi.encodePacked(r, s, v), quote, signatures);
    }

    function testExecuteRolloverRejectsReusedMandateNonce() public {
        vault.setMarginEngineRfqRouter(IMarginEngineRfqRouter(address(marginEngineRfqRouter)));
        marginEngineRfqRouter.setProtocolFeeConfig(100, feeRecipient);

        (uint256 loanId,) = _createFinalizedLoan(1e8, 21_000e6, 26_000e6, 20_000e6, 30 days);
        CollarVault.Loan memory firstLoan = vault.getLoan(loanId);

        vm.warp(block.timestamp + 5 days);

        uint64 firstMaturity = uint64(block.timestamp + 40 days);
        uint256 firstPutStrike = 22_000e6;
        uint256 firstCallStrike = 28_000e6;
        bytes32 firstPutInstrumentId = marginEngine.registerInstrument(
            address(wbtc), address(usdc), address(usdc), firstMaturity, firstPutStrike, IMarginEngine.OptionType.Put
        );
        bytes32 firstCallInstrumentId = marginEngine.registerInstrument(
            address(wbtc), address(usdc), address(wbtc), firstMaturity, firstCallStrike, IMarginEngine.OptionType.Call
        );
        marginEngine.updateInstrumentOracle(firstPutInstrumentId, 0, 0, firstPutStrike);

        usdc.mint(marketMaker, 1_000_000e6);
        vm.startPrank(marketMaker);
        uint256 firstPutBucketId = marginEngine.createPutBucket(firstPutInstrumentId, marketMaker);
        marginEngine.depositPutCollateral(firstPutBucketId, 1_000_000e6);
        vm.stopPrank();
        usdc.mint(marketMaker, 200e6);

        vm.prank(keeper);
        (, uint256 firstCallBucketId) = vault.prepareRolloverCallBucket(loanId, firstMaturity, firstCallStrike);

        (address firstOldLongCallToken,) = marginEngine.getBucketTokens(firstLoan.callBucketId);
        vm.prank(marketMaker);
        IERC20(firstOldLongCallToken).approve(address(marginEngineRfqRouter), type(uint256).max);

        uint256 reusedNonce = 777;
        CollarVault.RolloverMandate memory firstMandate = CollarVault.RolloverMandate({
            borrower: borrower,
            loanId: loanId,
            newMaturity: firstMaturity,
            minCallStrike: firstCallStrike,
            maxPutStrike: firstPutStrike,
            minNetInterest: 20e6,
            deadline: uint64(block.timestamp + 1 days),
            nonce: reusedNonce
        });

        IMarginEngineRfqRouter.Action[] memory firstActions = _rolloverActions(
            firstLoan, marketMaker, firstPutBucketId, firstPutInstrumentId, firstCallBucketId, firstCallInstrumentId
        );
        IMarginEngineRfqRouter.Quote memory firstQuote = IMarginEngineRfqRouter.Quote({
            taker: address(vault),
            authorizedExecutor: address(vault),
            quoteAsset: address(usdc),
            validUntil: uint64(block.timestamp + 1 hours),
            nonce: 707,
            salt: 808,
            actions: firstActions
        });
        IMarginEngineRfqRouter.SignerSignature[] memory firstSignatures =
            new IMarginEngineRfqRouter.SignerSignature[](1);
        firstSignatures[0] = IMarginEngineRfqRouter.SignerSignature({signer: marketMaker, signature: hex"01"});
        bytes32 firstDigest = vault.hashRolloverMandate(firstMandate);
        (uint8 firstV, bytes32 firstR, bytes32 firstS) = vm.sign(borrowerKey, firstDigest);

        vm.prank(keeper);
        vault.executeRollover(
            loanId, firstMandate, abi.encodePacked(firstR, firstS, firstV), firstQuote, firstSignatures
        );

        CollarVault.Loan memory secondLoan = vault.getLoan(loanId);
        uint64 secondMaturity = uint64(block.timestamp + 80 days);
        uint256 secondPutStrike = 23_000e6;
        uint256 secondCallStrike = 29_000e6;
        bytes32 secondPutInstrumentId = marginEngine.registerInstrument(
            address(wbtc), address(usdc), address(usdc), secondMaturity, secondPutStrike, IMarginEngine.OptionType.Put
        );
        bytes32 secondCallInstrumentId = marginEngine.registerInstrument(
            address(wbtc), address(usdc), address(wbtc), secondMaturity, secondCallStrike, IMarginEngine.OptionType.Call
        );
        marginEngine.updateInstrumentOracle(secondPutInstrumentId, 0, 0, secondPutStrike);

        usdc.mint(marketMaker, 1_000_000e6);
        vm.startPrank(marketMaker);
        uint256 secondPutBucketId = marginEngine.createPutBucket(secondPutInstrumentId, marketMaker);
        marginEngine.depositPutCollateral(secondPutBucketId, 1_000_000e6);
        vm.stopPrank();
        usdc.mint(marketMaker, 200e6);

        vm.prank(keeper);
        (, uint256 secondCallBucketId) = vault.prepareRolloverCallBucket(loanId, secondMaturity, secondCallStrike);

        (address secondOldLongCallToken,) = marginEngine.getBucketTokens(secondLoan.callBucketId);
        vm.prank(marketMaker);
        IERC20(secondOldLongCallToken).approve(address(marginEngineRfqRouter), type(uint256).max);

        CollarVault.RolloverMandate memory secondMandate = CollarVault.RolloverMandate({
            borrower: borrower,
            loanId: loanId,
            newMaturity: secondMaturity,
            minCallStrike: secondCallStrike,
            maxPutStrike: secondPutStrike,
            minNetInterest: 20e6,
            deadline: uint64(block.timestamp + 1 days),
            nonce: reusedNonce
        });

        IMarginEngineRfqRouter.Action[] memory secondActions = _rolloverActions(
            secondLoan,
            marketMaker,
            secondPutBucketId,
            secondPutInstrumentId,
            secondCallBucketId,
            secondCallInstrumentId
        );
        IMarginEngineRfqRouter.Quote memory secondQuote = IMarginEngineRfqRouter.Quote({
            taker: address(vault),
            authorizedExecutor: address(vault),
            quoteAsset: address(usdc),
            validUntil: uint64(block.timestamp + 1 hours),
            nonce: 909,
            salt: 1001,
            actions: secondActions
        });
        IMarginEngineRfqRouter.SignerSignature[] memory secondSignatures =
            new IMarginEngineRfqRouter.SignerSignature[](1);
        secondSignatures[0] = IMarginEngineRfqRouter.SignerSignature({signer: marketMaker, signature: hex"01"});
        bytes32 secondDigest = vault.hashRolloverMandate(secondMandate);
        (uint8 secondV, bytes32 secondR, bytes32 secondS) = vm.sign(borrowerKey, secondDigest);

        vm.prank(keeper);
        vm.expectRevert(CollarVault.CV_InvalidMessage.selector);
        vault.executeRollover(
            loanId, secondMandate, abi.encodePacked(secondR, secondS, secondV), secondQuote, secondSignatures
        );
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

    function _quoteInterest(uint256 principal, uint256 apr, uint256 start, uint256 end)
        internal
        pure
        returns (uint256)
    {
        if (apr == 0 || end <= start) return 0;
        return ((principal * apr) / 1e18) * (end - start) / 365 days;
    }

    function _bucketOutstanding(uint256 bucketId) internal view returns (uint256 outstandingQuantity) {
        (
            bytes32 instrumentId,
            uint8 bucketType,
            address owner,
            uint256 collateralBalance,
            uint256 outstanding,
            address primaryToken,
            address secondaryToken,
            bool settled,
            bool closed,
            uint256 settlementCollateral,
            uint256 settlementTotalEntitlement,
            uint256 settlementPrimaryRateNumerator,
            uint256 settlementPrimaryRateDenominator,
            uint256 settlementSecondaryRateNumerator,
            uint256 settlementSecondaryRateDenominator,
            uint256 redeemedCollateral
        ) = marginEngine.buckets(bucketId);
        instrumentId;
        bucketType;
        owner;
        collateralBalance;
        primaryToken;
        secondaryToken;
        settled;
        closed;
        settlementCollateral;
        settlementTotalEntitlement;
        settlementPrimaryRateNumerator;
        settlementPrimaryRateDenominator;
        settlementSecondaryRateNumerator;
        settlementSecondaryRateDenominator;
        redeemedCollateral;
        return outstanding;
    }

    function _rolloverActions(
        CollarVault.Loan memory loan,
        address maker,
        uint256 newPutBucketId,
        bytes32 newPutInstrumentId,
        uint256 newCallBucketId,
        bytes32 newCallInstrumentId
    ) internal view returns (IMarginEngineRfqRouter.Action[] memory actions) {
        actions = new IMarginEngineRfqRouter.Action[](4);
        actions[0] = IMarginEngineRfqRouter.Action({
            side: IMarginEngineRfqRouter.Side.Sell,
            instrumentType: IMarginEngineRfqRouter.InstrumentType.Put,
            fulfillmentType: IMarginEngineRfqRouter.FulfillmentType.Transfer,
            bucketId: loan.putBucketId,
            instrumentId: loan.putInstrumentId,
            quantity: loan.collateralAmount,
            quoteAmount: 50e6,
            maker: maker,
            longRecipient: maker,
            longSource: address(vault),
            cappedRecipient: address(0),
            cappedSource: address(0),
            collateralRecipient: address(0)
        });
        actions[1] = IMarginEngineRfqRouter.Action({
            side: IMarginEngineRfqRouter.Side.Buy,
            instrumentType: IMarginEngineRfqRouter.InstrumentType.Call,
            fulfillmentType: IMarginEngineRfqRouter.FulfillmentType.Burn,
            bucketId: loan.callBucketId,
            instrumentId: loan.callInstrumentId,
            quantity: loan.collateralAmount,
            quoteAmount: 20e6,
            maker: maker,
            longRecipient: address(0),
            longSource: maker,
            cappedRecipient: address(0),
            cappedSource: address(vault),
            collateralRecipient: address(vault)
        });
        actions[2] = IMarginEngineRfqRouter.Action({
            side: IMarginEngineRfqRouter.Side.Sell,
            instrumentType: IMarginEngineRfqRouter.InstrumentType.Call,
            fulfillmentType: IMarginEngineRfqRouter.FulfillmentType.Mint,
            bucketId: newCallBucketId,
            instrumentId: newCallInstrumentId,
            quantity: loan.collateralAmount,
            quoteAmount: 40e6,
            maker: maker,
            longRecipient: maker,
            longSource: address(0),
            cappedRecipient: address(vault),
            cappedSource: address(0),
            collateralRecipient: address(0)
        });
        actions[3] = IMarginEngineRfqRouter.Action({
            side: IMarginEngineRfqRouter.Side.Buy,
            instrumentType: IMarginEngineRfqRouter.InstrumentType.Put,
            fulfillmentType: IMarginEngineRfqRouter.FulfillmentType.Mint,
            bucketId: newPutBucketId,
            instrumentId: newPutInstrumentId,
            quantity: loan.collateralAmount,
            quoteAmount: 30e6,
            maker: maker,
            longRecipient: address(vault),
            longSource: address(0),
            cappedRecipient: address(0),
            cappedSource: address(0),
            collateralRecipient: address(0)
        });
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
