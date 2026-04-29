// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IAllowanceTransfer} from "permit2/src/interfaces/IAllowanceTransfer.sol";
import {DeployPermit2} from "permit2/test/utils/DeployPermit2.sol";

import {CollarLiquidityVault} from "../../src/CollarLiquidityVault.sol";
import {CollarVault, ILiquidityVault} from "../../src/CollarVault.sol";
import {ILendingAdapter} from "../../src/interfaces/ILendingAdapter.sol";
import {IMarginEngine} from "../../src/interfaces/IMarginEngine.sol";
import {IMarginEngineRfqRouter} from "../../src/interfaces/IMarginEngineRfqRouter.sol";
import {VariableLoanPosition} from "../../src/adapters/VariableLoanPosition.sol";
import {MockERC20} from "../mocks/MockERC20.sol";
import {MockEulerAdapter} from "../mocks/MockEulerAdapter.sol";
import {LatestMarginEngineHarness} from "../mocks/latest/LatestMarginEngineHarness.sol";
import {LatestMarginEngineRfqRouterHarness} from "../mocks/latest/LatestMarginEngineRfqRouterHarness.sol";

/// @dev Remaining harness-vs-production notes are documented in
/// docs/testing/latest-margin-engine-harness.md.
contract LatestMarginEngineHarnessFlowsTest is Test {
    uint256 internal borrowerKey = 0xB0B0;
    uint256 internal rfqSignerKey = 0xA11CE;

    address internal borrower;
    address internal rfqSigner;
    address internal keeper = address(0xCAFE);
    address internal marketMaker = address(0xBEEF);
    address internal treasury = address(0xFEE1);
    address internal feeRecipient = address(0xFEE2);
    address internal engineAdmin = address(0xAAA1);
    address internal engineUpgrader = address(0xAAA2);
    address internal engineProtocolOwner = address(0xAAA3);
    address internal routerAdmin = address(0xBBB1);
    address internal routerUpgrader = address(0xBBB2);

    MockERC20 internal usdc;
    MockERC20 internal wbtc;
    CollarLiquidityVault internal liquidityVault;
    MockEulerAdapter internal lendingAdapter;
    LatestMarginEngineHarness internal marginEngine;
    LatestMarginEngineRfqRouterHarness internal marginEngineRfqRouter;
    IAllowanceTransfer internal permit2;
    CollarVault internal vault;

    function setUp() public {
        borrower = vm.addr(borrowerKey);
        rfqSigner = vm.addr(rfqSignerKey);

        usdc = new MockERC20("USD Coin", "USDC", 6);
        wbtc = new MockERC20("Wrapped BTC", "WBTC", 8);
        liquidityVault = new CollarLiquidityVault(usdc, "Collar USDC", "cUSDC", address(this));
        lendingAdapter = new MockEulerAdapter(address(wbtc), address(usdc));
        permit2 = IAllowanceTransfer(new DeployPermit2().deployPermit2());

        marginEngine = _deployEngine();
        marginEngineRfqRouter = _deployRouter();

        vm.prank(engineProtocolOwner);
        marginEngine.setRfqRouter(address(marginEngineRfqRouter));
        vm.prank(engineAdmin);
        marginEngine.setMarketMaker(marketMaker, true);
        vm.prank(engineAdmin);
        marginEngine.setOracleUpdater(address(this), true);

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

        vm.prank(engineAdmin);
        marginEngine.setProtocolOwner(address(vault));

        vault.setTreasuryConfig(treasury, 2_000);
        vault.setCollateralConfig(address(wbtc), true, 1e8, address(wbtc));
        vault.setVariableLoanPositionImplementation(address(new VariableLoanPosition()));
        vault.setOriginationFeeApr(0.1e18);
        vault.setMaxRollLtv(0.999e18);
        vault.grantRole(vault.KEEPER_ROLE(), keeper);
        vault.grantRole(vault.RFQ_SIGNER_ROLE(), rfqSigner);
        vault.setMarginEngineRfqRouter(IMarginEngineRfqRouter(address(marginEngineRfqRouter)));

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

    function testLatestHarnessProxyInitializationReflectsCanonicalWiring() public view {
        assertEq(address(vault.marginEngine()), address(marginEngine));
        assertEq(address(vault.marginEngineRfqRouter()), address(marginEngineRfqRouter));
        assertEq(address(marginEngine.rfqRouter()), address(marginEngineRfqRouter));
        assertEq(address(marginEngineRfqRouter.engine()), address(marginEngine));
        assertTrue(marginEngine.hasRole(marginEngine.DEFAULT_ADMIN_ROLE(), engineAdmin));
        assertTrue(marginEngine.hasRole(marginEngine.UPGRADER_ROLE(), engineUpgrader));
        assertTrue(marginEngineRfqRouter.hasRole(marginEngineRfqRouter.DEFAULT_ADMIN_ROLE(), routerAdmin));
        assertTrue(marginEngineRfqRouter.hasRole(marginEngineRfqRouter.UPGRADER_ROLE(), routerUpgrader));
    }

    function testPrepareRolloverCallBucketAgainstLatestHarness() public {
        (uint256 loanId,) = _createFinalizedLoan(1e8, 21_000e6, 26_000e6, 20_000e6, 30 days);
        uint64 newMaturity = uint64(block.timestamp + 45 days);
        uint256 newCallStrike = 28_000e6;

        vm.prank(engineAdmin);
        bytes32 expectedInstrumentId = marginEngine.registerInstrument(
            address(wbtc), address(usdc), address(wbtc), newMaturity, newCallStrike, IMarginEngine.OptionType.Call
        );

        vm.prank(keeper);
        (bytes32 callInstrumentId, uint256 callBucketId) =
            vault.prepareRolloverCallBucket(loanId, newMaturity, newCallStrike);

        assertEq(callInstrumentId, expectedInstrumentId);
        (bytes32 bucketInstrumentId, uint8 bucketType, address owner,,,,,,,,,,,,,) = marginEngine.buckets(callBucketId);
        assertEq(uint256(bucketType), 1);
        assertEq(bucketInstrumentId, expectedInstrumentId);
        assertEq(owner, address(vault));
    }

    function testE2E_OriginationAndPutSettlementAgainstLatestHarness() public {
        (uint256 loanId,) = _createFinalizedLoan(1e8, 21_000e6, 26_000e6, 20_000e6, 30 days);
        CollarVault.Loan memory loan = vault.getLoan(loanId);

        vm.warp(block.timestamp + 30 days + 1);
        marginEngine.updateInstrumentOracle(loan.putInstrumentId, 0, 0, 18_000e6);
        marginEngine.updateInstrumentOracle(loan.callInstrumentId, 0, 0, 18_000e6);
        usdc.approve(address(vault), type(uint256).max);

        vault.settleLoan(loanId, CollarVault.SettlementOutcome.PutITM);

        CollarVault.Loan memory settledLoan = vault.getLoan(loanId);
        assertEq(uint256(settledLoan.state), uint256(CollarVault.LoanState.CLOSED));
    }

    function _deployEngine() internal returns (LatestMarginEngineHarness engine) {
        LatestMarginEngineHarness implementation = new LatestMarginEngineHarness();
        ERC1967Proxy proxy = new ERC1967Proxy(
            address(implementation),
            abi.encodeCall(
                LatestMarginEngineHarness.initialize,
                (engineAdmin, engineUpgrader, engineProtocolOwner, address(usdc), uint64(1 days), uint64(2 days))
            )
        );
        engine = LatestMarginEngineHarness(address(proxy));
    }

    function _deployRouter() internal returns (LatestMarginEngineRfqRouterHarness router) {
        LatestMarginEngineRfqRouterHarness implementation = new LatestMarginEngineRfqRouterHarness();
        ERC1967Proxy proxy = new ERC1967Proxy(
            address(implementation),
            abi.encodeCall(
                LatestMarginEngineRfqRouterHarness.initialize, (routerAdmin, routerUpgrader, address(marginEngine))
            )
        );
        router = LatestMarginEngineRfqRouterHarness(address(proxy));
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
        CollarVault.DepositParams memory params = CollarVault.DepositParams({
            collateralAsset: address(wbtc),
            collateralAmount: collateralAmount,
            maturity: block.timestamp + tenor,
            putStrike: putStrike,
            borrowAmount: borrowAmount
        });
        rfq = CollarVault.BaselineRfq({
            loanId: 0,
            collateralAsset: params.collateralAsset,
            collateralAmount: params.collateralAmount,
            maturity: uint64(params.maturity),
            putStrike: params.putStrike,
            callStrike: callStrike,
            borrowAmount: params.borrowAmount,
            minNetInterest: 0,
            rfqExpiry: uint64(block.timestamp + 1 days),
            borrower: borrower,
            nonce: uint256(keccak256(abi.encode(collateralAmount, putStrike, callStrike, tenor, block.timestamp)))
        });

        bytes32 digest = vault.hashBaselineRfq(rfq);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(rfqSignerKey, digest);

        vm.startPrank(borrower);
        wbtc.approve(address(vault), collateralAmount);
        loanId =
            vault.createDepositWithMandate(params, rfq, abi.encodePacked(r, s, v), uint64(block.timestamp + 1 days));
        vm.stopPrank();

        rfq.loanId = loanId;
    }

    function _preparePutBucket(CollarVault.BaselineRfq memory rfq, uint256 quantity)
        internal
        returns (uint256 putBucketId)
    {
        vm.prank(engineAdmin);
        bytes32 putInstrumentId = marginEngine.registerInstrument(
            address(wbtc), address(usdc), address(usdc), rfq.maturity, rfq.putStrike, IMarginEngine.OptionType.Put
        );
        vm.prank(engineAdmin);
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
}
