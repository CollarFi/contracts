// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";
import {IAllowanceTransfer} from "permit2/src/interfaces/IAllowanceTransfer.sol";
import {DeployPermit2} from "permit2/test/utils/DeployPermit2.sol";

import {CollarLiquidityVault} from "../../src/CollarLiquidityVault.sol";
import {CollarVault, ILiquidityVault} from "../../src/CollarVault.sol";
import {ILendingAdapter} from "../../src/interfaces/ILendingAdapter.sol";
import {IMarginEngine} from "../../src/interfaces/IMarginEngine.sol";
import {VariableLoanPosition} from "../../src/adapters/VariableLoanPosition.sol";
import {MockERC20} from "../mocks/MockERC20.sol";
import {MockEulerAdapter} from "../mocks/MockEulerAdapter.sol";
import {MockMarginEngine} from "../mocks/MockMarginEngine.sol";

contract SameNetworkMarginEngineFlowsTest is Test {
    MockERC20 internal usdc;
    MockERC20 internal wbtc;
    CollarLiquidityVault internal liquidityVault;
    MockEulerAdapter internal lendingAdapter;
    MockMarginEngine internal marginEngine;
    CollarVault internal vault;

    uint256 internal borrowerKey = 0xB0B0;
    uint256 internal signerKey = 0xA11CE;
    address internal borrower;
    address internal signer;
    address internal keeper = address(0xCAFE);
    address internal maker = address(0xBEEF);

    function setUp() public {
        borrower = vm.addr(borrowerKey);
        signer = vm.addr(signerKey);

        usdc = new MockERC20("USD Coin", "USDC", 6);
        wbtc = new MockERC20("Wrapped BTC", "WBTC", 8);
        liquidityVault = new CollarLiquidityVault(usdc, "Collar USDC", "cUSDC", address(this));
        lendingAdapter = new MockEulerAdapter(address(wbtc), address(usdc));
        marginEngine = new MockMarginEngine(address(usdc), address(this));

        CollarVault impl = new CollarVault();
        bytes memory init = abi.encodeCall(
            CollarVault.initialize,
            (
                address(this),
                ILiquidityVault(address(liquidityVault)),
                ILendingAdapter(address(lendingAdapter)),
                IAllowanceTransfer(new DeployPermit2().deployPermit2()),
                address(marginEngine),
                address(0xFEE1)
            )
        );
        vault = CollarVault(payable(address(new ERC1967Proxy(address(impl), init))));

        liquidityVault.grantRole(liquidityVault.VAULT_ROLE(), address(vault));
        marginEngine.setProtocolOwner(address(vault));
        marginEngine.setMarketMaker(maker, true);
        marginEngine.setOracleUpdater(address(this), true);

        vault.grantRole(vault.KEEPER_ROLE(), keeper);
        vault.grantRole(vault.RFQ_SIGNER_ROLE(), signer);
        vault.setCollateralConfig(address(wbtc), true, 1e8, address(wbtc));
        vault.setVariableLoanPositionImplementation(address(new VariableLoanPosition()));
        vault.setOriginationFeeApr(0.1e18);
        vault.setMaxRollLtv(0.999e18);

        usdc.mint(address(this), 2_000_000e6);
        usdc.approve(address(liquidityVault), type(uint256).max);
        liquidityVault.deposit(2_000_000e6, address(this));
        usdc.mint(address(this), 1_000_000e6);
        usdc.mint(address(lendingAdapter), 1_000_000e6);
        lendingAdapter.setLiquidity(1_000_000e6);
        usdc.mint(maker, 1_000_000e6);
        wbtc.mint(borrower, 10e8);
    }

    function testE2E_NeutralFlowToVariableRepayAndWithdraw() public {
        uint256 loanId = _openLoan(24_000e6);

        vm.warp(block.timestamp + 30 days);
        vault.settleLoan(loanId, CollarVault.SettlementOutcome.Neutral);
        assertEq(uint256(vault.getLoan(loanId).state), uint256(CollarVault.LoanState.READY_FOR_VARIABLE));

        assertTrue(vault.tryConvertReadyLoan(loanId));
        CollarVault.Loan memory variableLoan = vault.getLoan(loanId);
        assertEq(uint256(variableLoan.state), uint256(CollarVault.LoanState.ACTIVE_VARIABLE));

        uint256 debt = variableLoan.variableDebt;
        usdc.mint(borrower, debt);
        vm.startPrank(borrower);
        usdc.approve(address(vault), debt);
        vault.repayVariableLoan(loanId, debt);
        vault.withdrawVariableCollateral(loanId, vault.getLoan(loanId).collateralAmount);
        vm.stopPrank();

        assertEq(uint256(vault.getLoan(loanId).state), uint256(CollarVault.LoanState.CLOSED));
    }

    function testE2E_CallItmSettlement() public {
        uint256 loanId = _openLoan(32_000e6);
        vm.warp(block.timestamp + 30 days);
        usdc.approve(address(vault), type(uint256).max);
        vault.settleLoan(loanId, CollarVault.SettlementOutcome.CallITM);
        assertEq(uint256(vault.getLoan(loanId).state), uint256(CollarVault.LoanState.CLOSED));
    }

    function testE2E_PutItmSettlement() public {
        uint256 loanId = _openLoan(0);
        vm.warp(block.timestamp + 30 days);
        vault.settleLoan(loanId, CollarVault.SettlementOutcome.PutITM);
        assertEq(uint256(vault.getLoan(loanId).state), uint256(CollarVault.LoanState.CLOSED));
    }

    function _openLoan(uint256 finalSpot) internal returns (uint256 loanId) {
        CollarVault.DepositParams memory params = CollarVault.DepositParams({
            collateralAsset: address(wbtc),
            collateralAmount: 1e8,
            maturity: block.timestamp + 30 days,
            putStrike: 21_000e6,
            borrowAmount: 20_000e6
        });
        CollarVault.BaselineRfq memory rfq = CollarVault.BaselineRfq({
            loanId: 0,
            collateralAsset: address(wbtc),
            collateralAmount: params.collateralAmount,
            maturity: uint64(params.maturity),
            putStrike: params.putStrike,
            callStrike: 26_000e6,
            borrowAmount: params.borrowAmount,
            minNetInterest: 0,
            rfqExpiry: uint64(block.timestamp + 1 days),
            borrower: borrower,
            nonce: uint256(finalSpot) + 77
        });
        bytes32 digest = vault.hashBaselineRfq(rfq);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(signerKey, digest);

        vm.startPrank(borrower);
        wbtc.approve(address(vault), 1e8);
        loanId =
            vault.createDepositWithMandate(params, rfq, abi.encodePacked(r, s, v), uint64(block.timestamp + 1 days));
        vm.stopPrank();

        rfq.loanId = loanId;
        bytes32 putInstrumentId = marginEngine.registerInstrument(
            address(wbtc), address(usdc), address(usdc), rfq.maturity, rfq.putStrike, IMarginEngine.OptionType.Put
        );
        bytes32 callInstrumentId = marginEngine.registerInstrument(
            address(wbtc), address(usdc), address(wbtc), rfq.maturity, rfq.callStrike, IMarginEngine.OptionType.Call
        );

        vm.startPrank(maker);
        uint256 putBucketId = marginEngine.createPutBucket(putInstrumentId, maker);
        usdc.approve(address(marginEngine), type(uint256).max);
        marginEngine.depositPutCollateral(putBucketId, 1_000_000e6);
        marginEngine.issuePut(putBucketId, 1e8, address(vault));
        vm.stopPrank();

        vm.prank(keeper);
        vault.finalizeLoan(loanId, CollarVault.FinalizeLoanParams({putBucketId: putBucketId, callBuyer: maker}));

        vm.warp(block.timestamp + 30 days);
        marginEngine.updateInstrumentOracle(putInstrumentId, 0, 0, finalSpot);
        marginEngine.updateInstrumentOracle(callInstrumentId, 0, 0, finalSpot);
        vm.warp(block.timestamp - 30 days);
    }
}
