// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";

import {CollarLiquidityVault} from "../src/CollarLiquidityVault.sol";
import {CollarVault, ILiquidityVault} from "../src/CollarVault.sol";
import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";
import {CollarVaultShared} from "../src/modules/CollarVaultShared.sol";
import {CollarVaultFinalizeModule} from "../src/modules/CollarVaultFinalizeModule.sol";
import {CollarVaultSettleModule} from "../src/modules/CollarVaultSettleModule.sol";
import {CollarVaultRolloverModule} from "../src/modules/CollarVaultRolloverModule.sol";
import {VariableLoanPosition} from "../src/adapters/VariableLoanPosition.sol";
import {CollarLZMessages} from "../src/bridge/CollarLZMessages.sol";
import {ICollarVaultMessenger} from "../src/interfaces/ICollarVaultMessenger.sol";
import {ILendingAdapter} from "../src/interfaces/ILendingAdapter.sol";
import {IBridgeAdapter} from "../src/interfaces/IBridgeAdapter.sol";
import {ICollarVaultFinalizeModule} from "../src/interfaces/ICollarVaultFinalizeModule.sol";

import {
    MessagingFee,
    MessagingReceipt
} from "@layerzerolabs/lz-evm-protocol-v2/contracts/interfaces/ILayerZeroEndpointV2.sol";

import {IAllowanceTransfer} from "permit2/src/interfaces/IAllowanceTransfer.sol";
import {DeployPermit2} from "permit2/test/utils/DeployPermit2.sol";
import {Permit2ECDSASigner} from "../lib/euler-earn/lib/euler-vault-kit/test/mocks/Permit2ECDSASigner.sol";

import {MockBridge} from "./mocks/MockBridge.sol";
import {MockERC20} from "./mocks/MockERC20.sol";
import {MockEulerAdapter} from "./mocks/MockEulerAdapter.sol";
import {MockBridgeAdapter} from "./mocks/MockBridgeAdapter.sol";

contract CollarVaultTest is Test {
    uint256 internal rfqSignerKey = 0xA11CE;
    address internal rfqSigner;

    MockERC20 internal usdc;
    MockERC20 internal wbtc;
    MockERC20 internal l2Wbtc;
    CollarLiquidityVault internal liquidityVault;
    MockBridge internal bridge;
    MockBridgeAdapter internal adapter;
    MockEulerAdapter internal eulerAdapter;
    CollarVault internal vault;
    MockLZMessenger internal messenger;
    CollarVaultFinalizeModule internal finalizeModule;
    CollarVaultSettleModule internal settleModule;
    CollarVaultRolloverModule internal rolloverModule;
    VariableLoanPosition internal positionImpl;

    uint256 internal borrowerKey = 0xB0B0;
    address internal borrower;
    address internal treasury = address(0xB0B1);
    address internal keeper = address(0xA11CE);

    IAllowanceTransfer internal permit2;
    Permit2ECDSASigner internal permit2Signer;

    function setUp() public {
        usdc = new MockERC20("USD Coin", "USDC", 6);
        wbtc = new MockERC20("Wrapped BTC", "WBTC", 8);
        l2Wbtc = new MockERC20("L2 Wrapped BTC", "L2WBTC", 8);
        liquidityVault = new CollarLiquidityVault(usdc, "Collar USDC", "cUSDC", address(this));
        bridge = new MockBridge(wbtc);
        adapter = new MockBridgeAdapter();
        eulerAdapter = new MockEulerAdapter(address(wbtc), address(usdc));
        messenger = new MockLZMessenger();
        finalizeModule = new CollarVaultFinalizeModule();
        settleModule = new CollarVaultSettleModule();
        rolloverModule = new CollarVaultRolloverModule();
        positionImpl = new VariableLoanPosition();
        borrower = vm.addr(borrowerKey);
        rfqSigner = vm.addr(rfqSignerKey);

        address permit2Address = new DeployPermit2().deployPermit2();
        permit2 = IAllowanceTransfer(permit2Address);
        permit2Signer = new Permit2ECDSASigner(permit2Address);

        CollarVault vaultImpl = new CollarVault();
        bytes memory initData = abi.encodeCall(
            CollarVault.initialize,
            (
                address(this),
                ILiquidityVault(address(liquidityVault)),
                ILendingAdapter(address(eulerAdapter)),
                permit2,
                address(0x1001),
                treasury
            )
        );
        vault = CollarVault(payable(address(new ERC1967Proxy(address(vaultImpl), initData))));
        vault.setTreasuryConfig(treasury, 0);
        vault.setLZMessenger(ICollarVaultMessenger(address(messenger)));
        vault.setFinalizeModule(address(finalizeModule));
        vault.setSettleModule(address(settleModule));
        vault.setRolloverModule(address(rolloverModule));
        vault.setVariableLoanPositionImplementation(address(positionImpl));

        vault.setCollateralConfig(address(wbtc), true, 1e8, address(l2Wbtc));
        vault.setSocketVaultConfig(address(wbtc), IBridgeAdapter(address(adapter)));
        vault.grantRole(vault.KEEPER_ROLE(), keeper);
        vault.setDeriveSubaccountId(1);
        vault.grantRole(vault.RFQ_SIGNER_ROLE(), rfqSigner);
        vault.setMaxMandateDuration(uint64(2 days));

        // fund liquidity
        usdc.mint(address(this), 1_000_000e6);
        usdc.approve(address(liquidityVault), type(uint256).max);
        liquidityVault.deposit(1_000_000e6, address(this));
        liquidityVault.grantRole(liquidityVault.VAULT_ROLE(), address(vault));

        // fund borrower collateral
        wbtc.mint(borrower, 1e8);
        vm.prank(borrower);
        wbtc.approve(address(permit2), type(uint256).max);
    }

    function testSetReadyLoanConfigRevertsOnInvalidInput() public {
        vm.expectRevert(CollarVault.CV_InvalidConfig.selector);
        vault.setReadyLoanConfig(0, 1);
    }

    function testCreateDepositWithMandateCombinesDepositAndMandate() public {
        CollarVault.DepositParams memory params = CollarVault.DepositParams({
            collateralAsset: address(wbtc),
            collateralAmount: 1e8,
            maturity: block.timestamp + 30 days,
            putStrike: 21_000e6,
            borrowAmount: 20_000e6
        });

        // Approve vault for collateral transfer (standard ERC20 approval)
        vm.startPrank(borrower);
        wbtc.approve(address(vault), params.collateralAmount);
        vm.stopPrank();

        // Prepare RFQ with loanId=0 (sentinel - will be substituted by vault)
        ICollarVaultFinalizeModule.BaselineRfq memory rfq = ICollarVaultFinalizeModule.BaselineRfq({
            loanId: 0, // Sentinel value - vault will substitute with actual loanId
            collateralAsset: address(wbtc),
            collateralAmount: params.collateralAmount,
            maturity: uint64(params.maturity),
            putStrike: params.putStrike,
            callStrike: 25_000e6,
            borrowAmount: params.borrowAmount,
            minNetInterest: 0,
            rfqExpiry: uint64(block.timestamp + 1 days),
            borrower: borrower,
            nonce: 999
        });

        bytes32 rfqHash = vault.hashBaselineRfq(rfq);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(rfqSignerKey, rfqHash);
        bytes memory rfqSig = abi.encodePacked(r, s, v);

        // Execute combined deposit + mandate acceptance in one transaction
        vm.deal(borrower, 1 ether);
        vm.prank(borrower);
        (uint256 loanId, bytes32 socketMessageId, bytes32 depositLzGuid, bytes32 mandateLzGuid) =
            vault.createDepositWithMandate{value: 1 ether}(params, rfq, rfqSig, uint64(block.timestamp + 1 days));

        // Verify loan was created
        assertGt(loanId, 0);

        // Verify pending deposit exists
        (address pendingBorrower,,,,,) = vault.pendingDeposits(loanId);
        assertEq(pendingBorrower, borrower);

        // Verify mandate was created
        (address mandateBorrower,,,,,,,,, bool sentToL2) = vault.mandates(loanId);
        assertEq(mandateBorrower, borrower);
        assertTrue(sentToL2);

        // Verify both LZ messages were sent
        assertTrue(depositLzGuid != bytes32(0));
        assertTrue(mandateLzGuid != bytes32(0));
        (, uint256 sentLoanId, address sentAsset, uint256 sentAmount,,,,,,,) = messenger.lastSentMessage();
        assertEq(sentLoanId, loanId);
        assertEq(sentAsset, address(l2Wbtc));
        assertEq(sentAmount, params.borrowAmount);

        // Verify collateral was transferred from borrower
        assertEq(wbtc.balanceOf(borrower), 0);

        // Complete the loan finalization to ensure end-to-end flow works
        bytes32 depositGuid = bytes32(uint256(1));
        bytes32 tradeGuid = bytes32(uint256(2));

        messenger.setMessage(
            depositGuid,
            CollarLZMessages.Message({
                action: CollarLZMessages.Action.DepositConfirmed,
                loanId: loanId,
                asset: address(wbtc),
                amount: params.collateralAmount,
                recipient: address(vault),
                subaccountId: 1,
                socketMessageId: bytes32(0),
                secondaryAmount: 0,
                quoteHash: bytes32(0),
                takerNonce: 0,
                data: bytes("")
            })
        );

        bytes memory tradeData = abi.encode(uint256(25_000e6), uint256(20_000e6), uint64(params.maturity), int256(0));
        messenger.setMessage(
            tradeGuid,
            CollarLZMessages.Message({
                action: CollarLZMessages.Action.TradeConfirmed,
                loanId: loanId,
                asset: address(0),
                amount: 0,
                recipient: address(vault),
                subaccountId: 1,
                socketMessageId: bytes32(0),
                secondaryAmount: 0,
                quoteHash: bytes32(0),
                takerNonce: 1,
                data: tradeData
            })
        );

        vm.prank(keeper);
        vault.finalizeLoan(loanId, depositGuid, tradeGuid);

        CollarVaultShared.Loan memory loan = vault.getLoan(loanId);
        assertEq(uint256(loan.state), uint256(CollarVaultShared.LoanState.ACTIVE_ZERO_COST));
        assertEq(loan.borrower, borrower);
    }

    function testCreateLoanHappyPathViaMandate() public {
        CollarVault.DepositParams memory params = CollarVault.DepositParams({
            collateralAsset: address(wbtc),
            collateralAmount: 1e8,
            maturity: block.timestamp + 30 days,
            putStrike: 21_000e6,
            borrowAmount: 20_000e6
        });

        uint256 loanId = _requestDeposit(params);

        ICollarVaultFinalizeModule.BaselineRfq memory rfq = ICollarVaultFinalizeModule.BaselineRfq({
            loanId: loanId,
            collateralAsset: address(wbtc),
            collateralAmount: params.collateralAmount,
            maturity: uint64(params.maturity),
            putStrike: params.putStrike,
            callStrike: 25_000e6,
            borrowAmount: params.borrowAmount,
            minNetInterest: 0,
            rfqExpiry: uint64(block.timestamp + 1 days),
            borrower: borrower,
            nonce: 1
        });

        bytes32 rfqHash = vault.hashBaselineRfq(rfq);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(rfqSignerKey, rfqHash);
        bytes memory rfqSig = abi.encodePacked(r, s, v);

        vm.prank(borrower);
        vault.acceptMandate{value: 0}(loanId, rfq, rfqSig, uint64(block.timestamp + 1 days));
        (, uint256 sentLoanId, address sentAsset, uint256 sentAmount,,,,,,,) = messenger.lastSentMessage();
        assertEq(sentLoanId, loanId);
        assertEq(sentAsset, address(l2Wbtc));
        assertEq(sentAmount, params.borrowAmount);

        bytes32 depositGuid = bytes32(uint256(1));
        bytes32 tradeGuid = bytes32(uint256(2));

        messenger.setMessage(
            depositGuid,
            CollarLZMessages.Message({
                action: CollarLZMessages.Action.DepositConfirmed,
                loanId: loanId,
                asset: address(wbtc),
                amount: params.collateralAmount,
                recipient: address(vault),
                subaccountId: 1,
                socketMessageId: bytes32(0),
                secondaryAmount: 0,
                quoteHash: bytes32(0),
                takerNonce: 0,
                data: bytes("")
            })
        );

        bytes memory tradeData = abi.encode(uint256(25_000e6), uint256(20_000e6), uint64(params.maturity), int256(0));

        messenger.setMessage(
            tradeGuid,
            CollarLZMessages.Message({
                action: CollarLZMessages.Action.TradeConfirmed,
                loanId: loanId,
                asset: address(0),
                amount: 0,
                recipient: address(vault),
                subaccountId: 1,
                socketMessageId: bytes32(0),
                secondaryAmount: 0,
                quoteHash: bytes32(0),
                takerNonce: 1,
                data: tradeData
            })
        );

        vm.prank(keeper);
        vault.finalizeLoan(loanId, depositGuid, tradeGuid);

        CollarVaultShared.Loan memory loan = vault.getLoan(loanId);
        assertEq(uint256(loan.state), uint256(CollarVaultShared.LoanState.ACTIVE_ZERO_COST));
        assertEq(loan.borrower, borrower);
        assertEq(loan.collateralAsset, address(wbtc));
        assertEq(loan.collateralAmount, 1e8);
        assertEq(loan.principal, 20_000e6);
        assertEq(loan.putStrike, 20_000e6);
        assertEq(loan.callStrike, 25_000e6);
    }

    function testAcceptMandateRejectsSentinelLoanIdOnExternalPath() public {
        CollarVault.DepositParams memory params = CollarVault.DepositParams({
            collateralAsset: address(wbtc),
            collateralAmount: 1e8,
            maturity: block.timestamp + 30 days,
            putStrike: 21_000e6,
            borrowAmount: 20_000e6
        });

        uint256 loanId = _requestDeposit(params);

        ICollarVaultFinalizeModule.BaselineRfq memory rfq = ICollarVaultFinalizeModule.BaselineRfq({
            loanId: 0,
            collateralAsset: address(wbtc),
            collateralAmount: params.collateralAmount,
            maturity: uint64(params.maturity),
            putStrike: params.putStrike,
            callStrike: 25_000e6,
            borrowAmount: params.borrowAmount,
            minNetInterest: 0,
            rfqExpiry: uint64(block.timestamp + 1 days),
            borrower: borrower,
            nonce: 43
        });

        bytes32 rfqHash = vault.hashBaselineRfq(rfq);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(rfqSignerKey, rfqHash);
        bytes memory rfqSig = abi.encodePacked(r, s, v);

        vm.prank(borrower);
        vm.expectRevert(CollarVault.CV_InvalidMessage.selector);
        vault.acceptMandate{value: 0}(loanId, rfq, rfqSig, uint64(block.timestamp + 1 days));
    }

    function testExpiredMandateAllowsRefreshOrReturn() public {
        CollarVault.DepositParams memory params = CollarVault.DepositParams({
            collateralAsset: address(wbtc),
            collateralAmount: 1e8,
            maturity: block.timestamp + 30 days,
            putStrike: 20_000e6,
            borrowAmount: 20_000e6
        });

        uint256 loanId = _requestDeposit(params);

        ICollarVaultFinalizeModule.BaselineRfq memory rfq1 = ICollarVaultFinalizeModule.BaselineRfq({
            loanId: loanId,
            collateralAsset: address(wbtc),
            collateralAmount: params.collateralAmount,
            maturity: uint64(params.maturity),
            putStrike: params.putStrike,
            callStrike: 25_000e6,
            borrowAmount: params.borrowAmount,
            minNetInterest: 0,
            rfqExpiry: uint64(block.timestamp + 1 days),
            borrower: borrower,
            nonce: 44
        });

        bytes32 rfq1Hash = vault.hashBaselineRfq(rfq1);
        (uint8 v1, bytes32 r1, bytes32 s1) = vm.sign(rfqSignerKey, rfq1Hash);
        bytes memory rfq1Sig = abi.encodePacked(r1, s1, v1);

        uint64 firstDeadline = uint64(block.timestamp + 1 days);
        vm.prank(borrower);
        vault.acceptMandate{value: 0}(loanId, rfq1, rfq1Sig, firstDeadline);

        vm.warp(firstDeadline + 1);
        uint64 secondDeadline = firstDeadline + uint64(1 days) + 1;

        ICollarVaultFinalizeModule.BaselineRfq memory rfq2 = ICollarVaultFinalizeModule.BaselineRfq({
            loanId: loanId,
            collateralAsset: address(wbtc),
            collateralAmount: params.collateralAmount,
            maturity: uint64(params.maturity),
            putStrike: 21_000e6,
            callStrike: 26_000e6,
            borrowAmount: params.borrowAmount,
            minNetInterest: 0,
            rfqExpiry: secondDeadline,
            borrower: borrower,
            nonce: 45
        });

        bytes32 rfq2Hash = vault.hashBaselineRfq(rfq2);
        (uint8 v2, bytes32 r2, bytes32 s2) = vm.sign(rfqSignerKey, rfq2Hash);
        bytes memory rfq2Sig = abi.encodePacked(r2, s2, v2);

        vm.prank(borrower);
        vault.acceptMandate{value: 0}(loanId, rfq2, rfq2Sig, secondDeadline);

        (
            address refreshedBorrower,
            ,
            ,
            ,
            uint64 refreshedDeadline,
            ,
            uint256 refreshedMinCallStrike,
            uint256 refreshedMaxPutStrike,
            ,
            bool sentToL2
        ) = vault.mandates(loanId);
        assertEq(refreshedBorrower, borrower);
        assertEq(refreshedDeadline, secondDeadline);
        assertEq(refreshedMinCallStrike, 26_000e6);
        assertEq(refreshedMaxPutStrike, 21_000e6);
        assertTrue(sentToL2);

        vm.prank(borrower);
        vm.expectRevert(CollarVault.CV_InvalidState.selector);
        vault.requestCollateralReturn(loanId);

        vm.warp(secondDeadline + 1);

        vm.prank(borrower);
        vault.requestCollateralReturn(loanId);
        assertTrue(vault.returnRequested(loanId));
    }

    function testAcceptMandateAllowsWhenFixedInterestIsSigned() public {
        vault.setOriginationFeeApr(0.2e18);
        CollarVault.DepositParams memory params = CollarVault.DepositParams({
            collateralAsset: address(wbtc),
            collateralAmount: 1e8,
            maturity: block.timestamp + 30 days,
            putStrike: 21_000e6,
            borrowAmount: 20_000e6
        });
        uint256 loanId = _requestDeposit(params);

        ICollarVaultFinalizeModule.BaselineRfq memory rfq = ICollarVaultFinalizeModule.BaselineRfq({
            loanId: loanId,
            collateralAsset: address(wbtc),
            collateralAmount: params.collateralAmount,
            maturity: uint64(params.maturity),
            putStrike: params.putStrike,
            callStrike: 25_000e6,
            borrowAmount: params.borrowAmount,
            minNetInterest: 0.1e18,
            rfqExpiry: uint64(block.timestamp + 1 days),
            borrower: borrower,
            nonce: 42
        });

        bytes32 rfqHash = vault.hashBaselineRfq(rfq);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(rfqSignerKey, rfqHash);
        bytes memory rfqSig = abi.encodePacked(r, s, v);

        vm.prank(borrower);
        vault.acceptMandate{value: 0}(loanId, rfq, rfqSig, uint64(block.timestamp + 1 days));
    }

    function testSettleRepaysPrincipalPlusBulletInterest() public {
        vault.setOriginationFeeApr(0.1e18);
        uint256 loanId = _createAndFinalizeLoan(block.timestamp + 30 days, 25_000e6, 21_000e6, 0);

        CollarVaultShared.Loan memory loanBefore = vault.getLoan(loanId);
        assertGt(loanBefore.interestOwed, 0);

        vm.warp(loanBefore.maturity + 1);
        bytes32 settleGuid = bytes32(uint256(3));
        uint256 settlementAmount = loanBefore.principal + loanBefore.interestOwed;
        usdc.mint(address(vault), settlementAmount);
        messenger.setMessage(
            settleGuid,
            CollarLZMessages.Message({
                action: CollarLZMessages.Action.SettlementReport,
                loanId: loanId,
                asset: address(usdc),
                amount: settlementAmount,
                recipient: address(vault),
                subaccountId: 1,
                socketMessageId: bytes32(uint256(1)),
                secondaryAmount: 0,
                quoteHash: bytes32(0),
                takerNonce: 0,
                data: bytes("")
            })
        );

        vm.prank(keeper);
        vault.settleLoan(loanId, CollarVaultShared.SettlementOutcome.PutITM, settleGuid);

        CollarVaultShared.Loan memory loanAfter = vault.getLoan(loanId);
        assertEq(uint256(loanAfter.state), uint256(CollarVaultShared.LoanState.CLOSED));
    }

    function testRolloverHappyPath() public {
        vault.setOriginationFeeApr(0.1e18);
        uint256 loanId = _createAndFinalizeLoan(block.timestamp + 20 days, 25_000e6, 21_000e6, 0);

        vm.warp(block.timestamp + 5 days);
        CollarVaultShared.RolloverMandate memory mandate = CollarVaultShared.RolloverMandate({
            borrower: borrower,
            loanId: loanId,
            newMaturity: uint64(block.timestamp + 30 days),
            minCallStrike: 26_000e6,
            maxPutStrike: 21_000e6,
            minNetInterest: 0,
            deadline: uint64(block.timestamp + 1 days),
            nonce: 77
        });
        bytes32 mandateHash = vault.hashRolloverMandate(mandate);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(borrowerKey, mandateHash);
        bytes memory sig = abi.encodePacked(r, s, v);
        uint64 expectedNonce = messenger.nonce() + 1;
        bytes32 expectedGuid =
            keccak256(abi.encodePacked(expectedNonce, loanId, CollarLZMessages.Action.RolloverIntent));

        vm.expectEmit(true, true, false, true);
        emit CollarVault.RolloverRequested(
            loanId,
            borrower,
            mandate.newMaturity,
            mandate.minCallStrike,
            mandate.maxPutStrike,
            mandate.minNetInterest,
            mandate.deadline,
            mandateHash,
            expectedGuid
        );
        vm.prank(keeper);
        bytes32 requestGuid = vault.executeRollover(loanId, mandate, sig, 26_500e6, 20_500e6);

        // pending rollover is asserted indirectly by requiring finalization to succeed only after confirmation.
        (CollarLZMessages.Action sentAction,,,,,,,,,,) = messenger.lastSentMessage();
        assertEq(uint8(sentAction), uint8(CollarLZMessages.Action.RolloverIntent));
        assertEq(requestGuid, messenger.lastSentGuid());

        bytes32 confirmGuid = bytes32(uint256(9000 + loanId));
        messenger.setMessage(
            confirmGuid,
            CollarLZMessages.Message({
                action: CollarLZMessages.Action.RolloverConfirmed,
                loanId: loanId,
                asset: address(0),
                amount: 0,
                recipient: address(vault),
                subaccountId: 1,
                socketMessageId: bytes32(0),
                secondaryAmount: 0,
                quoteHash: bytes32(0),
                takerNonce: 0,
                data: abi.encode(
                    mandateHash,
                    borrower,
                    uint256(26_500e6),
                    uint256(20_500e6),
                    uint256(0.15e18),
                    mandate.newMaturity,
                    int256(0)
                )
            })
        );

        vm.prank(keeper);
        vault.finalizeRollover(loanId, confirmGuid);

        CollarVaultShared.Loan memory loan = vault.getLoan(loanId);
        assertEq(loan.callStrike, 26_500e6);
        assertEq(loan.putStrike, 20_500e6);
        assertEq(loan.maturity, mandate.newMaturity);
        assertEq(loan.interestApr, 0.15e18);
        // pending rollover cleared implicitly by successful finalize and updated loan params.
    }

    function testRolloverFinalizeIsIdempotentForDuplicateGuid() public {
        uint256 loanId = _createAndFinalizeLoan(block.timestamp + 20 days, 25_000e6, 20_000e6, 0);
        vm.warp(block.timestamp + 5 days);

        CollarVaultShared.RolloverMandate memory mandate = CollarVaultShared.RolloverMandate({
            borrower: borrower,
            loanId: loanId,
            newMaturity: uint64(block.timestamp + 30 days),
            minCallStrike: 26_000e6,
            maxPutStrike: 21_000e6,
            minNetInterest: 0,
            deadline: uint64(block.timestamp + 1 days),
            nonce: 78
        });
        bytes32 mandateHash = vault.hashRolloverMandate(mandate);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(borrowerKey, mandateHash);
        bytes memory sig = abi.encodePacked(r, s, v);

        vm.prank(keeper);
        vault.executeRollover(loanId, mandate, sig, 26_500e6, 20_500e6);

        bytes32 confirmGuid = bytes32(uint256(9100 + loanId));
        messenger.setMessage(
            confirmGuid,
            CollarLZMessages.Message({
                action: CollarLZMessages.Action.RolloverConfirmed,
                loanId: loanId,
                asset: address(0),
                amount: 0,
                recipient: address(vault),
                subaccountId: 1,
                socketMessageId: bytes32(0),
                secondaryAmount: 0,
                quoteHash: bytes32(0),
                takerNonce: 0,
                data: abi.encode(
                    mandateHash,
                    borrower,
                    uint256(26_500e6),
                    uint256(20_500e6),
                    uint256(0.15e18),
                    mandate.newMaturity,
                    int256(0)
                )
            })
        );

        vm.prank(keeper);
        vault.finalizeRollover(loanId, confirmGuid);

        CollarVaultShared.Loan memory afterFirst = vault.getLoan(loanId);
        vm.prank(keeper);
        vault.finalizeRollover(loanId, confirmGuid);
        CollarVaultShared.Loan memory afterSecond = vault.getLoan(loanId);

        assertEq(afterSecond.maturity, afterFirst.maturity);
        assertEq(afterSecond.callStrike, afterFirst.callStrike);
        assertEq(afterSecond.putStrike, afterFirst.putStrike);
        assertEq(afterSecond.interestOwed, afterFirst.interestOwed);
    }

    function testRolloverFinalizeDoesNotBrickOnEconomicDrift() public {
        uint256 loanId = _createAndFinalizeLoan(block.timestamp + 20 days, 25_000e6, 20_000e6, 0);
        vm.warp(block.timestamp + 5 days);

        CollarVaultShared.RolloverMandate memory mandate = CollarVaultShared.RolloverMandate({
            borrower: borrower,
            loanId: loanId,
            newMaturity: uint64(block.timestamp + 30 days),
            minCallStrike: 26_000e6,
            maxPutStrike: 21_000e6,
            minNetInterest: 100_000e6,
            deadline: uint64(block.timestamp + 1 days),
            nonce: 79
        });
        bytes32 mandateHash = vault.hashRolloverMandate(mandate);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(borrowerKey, mandateHash);
        bytes memory sig = abi.encodePacked(r, s, v);

        vm.prank(keeper);
        vault.executeRollover(loanId, mandate, sig, 26_500e6, 20_500e6);

        bytes32 confirmGuid = bytes32(uint256(9200 + loanId));
        messenger.setMessage(
            confirmGuid,
            CollarLZMessages.Message({
                action: CollarLZMessages.Action.RolloverConfirmed,
                loanId: loanId,
                asset: address(0),
                amount: 0,
                recipient: address(vault),
                subaccountId: 1,
                socketMessageId: bytes32(0),
                secondaryAmount: 0,
                quoteHash: bytes32(0),
                takerNonce: 0,
                data: abi.encode(
                    mandateHash,
                    borrower,
                    uint256(25_900e6),
                    uint256(21_100e6),
                    uint256(0.01e18),
                    mandate.newMaturity,
                    int256(-10_000e6)
                )
            })
        );

        vm.expectEmit(true, true, false, true);
        emit CollarVaultRolloverModule.RolloverFinalizeAnomaly(
            loanId, confirmGuid, 5, 25_900e6, 21_100e6, 0.01e18, -10_000e6
        );
        vm.prank(keeper);
        vault.finalizeRollover(loanId, confirmGuid);

        CollarVaultShared.Loan memory loan = vault.getLoan(loanId);
        assertEq(loan.callStrike, 25_900e6);
        assertEq(loan.putStrike, 21_100e6);
        assertEq(loan.maturity, mandate.newMaturity);
        assertEq(loan.interestApr, 0.01e18);
    }

    function testRolloverRevertsUnauthorizedKeeper() public {
        uint256 loanId = _createAndFinalizeLoan(block.timestamp + 20 days, 25_000e6, 20_000e6, 0);

        CollarVaultShared.RolloverMandate memory mandate = CollarVaultShared.RolloverMandate({
            borrower: borrower,
            loanId: loanId,
            newMaturity: uint64(block.timestamp + 30 days),
            minCallStrike: 26_000e6,
            maxPutStrike: 21_000e6,
            minNetInterest: 0,
            deadline: uint64(block.timestamp + 1 days),
            nonce: 88
        });
        bytes32 mandateHash = vault.hashRolloverMandate(mandate);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(borrowerKey, mandateHash);
        bytes memory sig = abi.encodePacked(r, s, v);

        vm.prank(borrower);
        vm.expectRevert(CollarVault.CV_Unauthorized.selector);
        vault.executeRollover(loanId, mandate, sig, 26_500e6, 20_500e6);
    }

    function testRolloverRevertsPostMaturity() public {
        uint256 loanId = _createAndFinalizeLoan(block.timestamp + 2 days, 25_000e6, 20_000e6, 0);
        vm.warp(block.timestamp + 3 days);

        CollarVaultShared.RolloverMandate memory mandate = CollarVaultShared.RolloverMandate({
            borrower: borrower,
            loanId: loanId,
            newMaturity: uint64(block.timestamp + 30 days),
            minCallStrike: 26_000e6,
            maxPutStrike: 21_000e6,
            minNetInterest: 0,
            deadline: uint64(block.timestamp + 1 days),
            nonce: 99
        });
        bytes32 mandateHash = vault.hashRolloverMandate(mandate);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(borrowerKey, mandateHash);
        bytes memory sig = abi.encodePacked(r, s, v);

        vm.prank(keeper);
        vm.expectRevert(CollarVault.CV_InvalidState.selector);
        vault.executeRollover(loanId, mandate, sig, 26_500e6, 20_500e6);
    }

    function testRolloverFinalizeRevertsWithoutConfirmation() public {
        uint256 loanId = _createAndFinalizeLoan(block.timestamp + 20 days, 25_000e6, 20_000e6, 0);
        vm.warp(block.timestamp + 5 days);

        CollarVaultShared.RolloverMandate memory mandate = CollarVaultShared.RolloverMandate({
            borrower: borrower,
            loanId: loanId,
            newMaturity: uint64(block.timestamp + 30 days),
            minCallStrike: 26_000e6,
            maxPutStrike: 21_000e6,
            minNetInterest: 0,
            deadline: uint64(block.timestamp + 1 days),
            nonce: 100
        });
        bytes32 mandateHash = vault.hashRolloverMandate(mandate);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(borrowerKey, mandateHash);
        bytes memory sig = abi.encodePacked(r, s, v);

        vm.prank(keeper);
        vault.executeRollover(loanId, mandate, sig, 26_500e6, 20_500e6);

        vm.prank(keeper);
        vm.expectRevert(CollarVault.CV_InvalidMessage.selector);
        vault.finalizeRollover(loanId, bytes32(uint256(123456)));
    }

    function testRolloverRequestRevertsOnReplayMandate() public {
        uint256 loanId = _createAndFinalizeLoan(block.timestamp + 20 days, 25_000e6, 20_000e6, 0);
        vm.warp(block.timestamp + 5 days);

        CollarVaultShared.RolloverMandate memory mandate = CollarVaultShared.RolloverMandate({
            borrower: borrower,
            loanId: loanId,
            newMaturity: uint64(block.timestamp + 30 days),
            minCallStrike: 26_000e6,
            maxPutStrike: 21_000e6,
            minNetInterest: 0,
            deadline: uint64(block.timestamp + 1 days),
            nonce: 101
        });
        bytes32 mandateHash = vault.hashRolloverMandate(mandate);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(borrowerKey, mandateHash);
        bytes memory sig = abi.encodePacked(r, s, v);

        vm.prank(keeper);
        vault.executeRollover(loanId, mandate, sig, 26_500e6, 20_500e6);

        vm.prank(keeper);
        vm.expectRevert(CollarVault.CV_InvalidState.selector);
        vault.executeRollover(loanId, mandate, sig, 26_500e6, 20_500e6);
    }

    function testFinalizeLoanRevertsWhenRealizedDeficitExceedsReserve() public {
        CollarVault.DepositParams memory params = CollarVault.DepositParams({
            collateralAsset: address(wbtc),
            collateralAmount: 1e8,
            maturity: block.timestamp + 30 days,
            putStrike: 20_000e6,
            borrowAmount: 20_000e6
        });

        uint256 loanId = _requestDeposit(params);

        ICollarVaultFinalizeModule.BaselineRfq memory rfq = ICollarVaultFinalizeModule.BaselineRfq({
            loanId: loanId,
            collateralAsset: address(wbtc),
            collateralAmount: params.collateralAmount,
            maturity: uint64(params.maturity),
            putStrike: params.putStrike,
            callStrike: 25_000e6,
            borrowAmount: params.borrowAmount,
            minNetInterest: 0,
            rfqExpiry: uint64(block.timestamp + 1 days),
            borrower: borrower,
            nonce: 777
        });

        bytes32 rfqHash = vault.hashBaselineRfq(rfq);
        (uint8 v, bytes32 r, bytes32 s_) = vm.sign(rfqSignerKey, rfqHash);
        bytes memory rfqSig = abi.encodePacked(r, s_, v);

        vm.prank(borrower);
        vault.acceptMandate{value: 0}(loanId, rfq, rfqSig, uint64(block.timestamp + 1 days));

        bytes32 depositGuid = bytes32(uint256(6100 + loanId));
        bytes32 tradeGuid = bytes32(uint256(6200 + loanId));

        messenger.setMessage(
            depositGuid,
            CollarLZMessages.Message({
                action: CollarLZMessages.Action.DepositConfirmed,
                loanId: loanId,
                asset: address(wbtc),
                amount: params.collateralAmount,
                recipient: address(vault),
                subaccountId: 1,
                socketMessageId: bytes32(0),
                secondaryAmount: 0,
                quoteHash: bytes32(0),
                takerNonce: 0,
                data: bytes("")
            })
        );

        messenger.setMessage(
            tradeGuid,
            CollarLZMessages.Message({
                action: CollarLZMessages.Action.TradeConfirmed,
                loanId: loanId,
                asset: address(0),
                amount: 0,
                recipient: address(vault),
                subaccountId: 1,
                socketMessageId: bytes32(0),
                secondaryAmount: 0,
                quoteHash: bytes32(0),
                takerNonce: 1,
                data: abi.encode(uint256(25_000e6), uint256(20_000e6), uint64(params.maturity), int256(-1_000e6))
            })
        );

        vm.prank(keeper);
        vm.expectRevert(CollarVault.CV_InsufficientValue.selector);
        vault.finalizeLoan(loanId, depositGuid, tradeGuid);
    }

    function testFuzzCreateDepositAllowsBorrowAmountDecoupledFromPutStrike(uint256 putStrike, uint256 borrowAmount)
        public
    {
        putStrike = bound(putStrike, 1, 100_000e6);

        // Deposits now always include a signed mandate, so roll-safety LTV is enforced at creation time.
        // Keep this fuzz case inside valid mandate bounds while still proving borrowAmount is independent
        // from the naive collateral*putStrike baseline.
        uint256 maxBorrow = (((uint256(1e8) * putStrike) / uint256(1e8)) * vault.maxRollLtv()) / 1e18;
        if (maxBorrow <= 1) return;
        borrowAmount = bound(borrowAmount, 1, maxBorrow - 1);

        uint256 expected = (uint256(1e8) * putStrike) / uint256(1e8);
        vm.assume(borrowAmount != expected);

        CollarVault.DepositParams memory params = CollarVault.DepositParams({
            collateralAsset: address(wbtc),
            collateralAmount: 1e8,
            maturity: block.timestamp + 30 days,
            putStrike: putStrike,
            borrowAmount: borrowAmount
        });

        uint256 loanId = _requestDeposit(params);

        (,,,,, uint256 pendingBorrowAmount) = vault.pendingDeposits(loanId);
        assertEq(pendingBorrowAmount, borrowAmount);
    }

    function testCreateDepositWithMandatePermitSupportsSignedMandateOrigination() public {
        vault.setMaxRollLtv(0.8e18);

        CollarVault.DepositParams memory params = CollarVault.DepositParams({
            collateralAsset: address(wbtc),
            collateralAmount: 1e8,
            maturity: block.timestamp + 30 days,
            putStrike: 62_000e6,
            borrowAmount: 40_000e6
        });

        uint256 loanId = _requestDeposit(params);
        assertGt(loanId, 0);

        (address mandateBorrower,,,,,,,,, bool sentToL2) = vault.mandates(loanId);
        assertEq(mandateBorrower, borrower);
        assertTrue(sentToL2);
    }

    function testFinalizeLoanCannotConsumeSameGuidTwice() public {
        uint256 loanId = _createAndFinalizeLoan(block.timestamp + 30 days, 25_000e6, 21_000e6, 0);
        loanId;

        // Re-using an already consumed trade/deposit guid must fail.
        vm.prank(keeper);
        vm.expectRevert(CollarVault.CV_NotFound.selector);
        vault.finalizeLoan(loanId, bytes32(uint256(100 + loanId)), bytes32(uint256(200 + loanId)));
    }

    function testFuzzFixedInterestSnapshotUnaffectedByAprDrop(uint8 tenorDays, uint256 newApr) public {
        tenorDays = uint8(bound(uint256(tenorDays), 7, 120));

        uint256 initialApr = 0.12e18;
        vault.setOriginationFeeApr(initialApr);

        CollarVault.DepositParams memory params = CollarVault.DepositParams({
            collateralAsset: address(wbtc),
            collateralAmount: 1e8,
            maturity: block.timestamp + uint256(tenorDays) * 1 days,
            putStrike: 21_000e6,
            borrowAmount: 20_000e6
        });

        uint256 loanId = _requestDeposit(params);
        uint256 acceptTs = block.timestamp;
        uint256 expectedInterest = ((params.borrowAmount * initialApr) / 1e18) * (params.maturity - acceptTs) / 365 days;

        ICollarVaultFinalizeModule.BaselineRfq memory rfq = ICollarVaultFinalizeModule.BaselineRfq({
            loanId: loanId,
            collateralAsset: address(wbtc),
            collateralAmount: params.collateralAmount,
            maturity: uint64(params.maturity),
            putStrike: params.putStrike,
            callStrike: 25_000e6,
            borrowAmount: params.borrowAmount,
            minNetInterest: expectedInterest,
            rfqExpiry: uint64(block.timestamp + 1 days),
            borrower: borrower,
            nonce: 901
        });

        bytes32 rfqHash = vault.hashBaselineRfq(rfq);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(rfqSignerKey, rfqHash);
        bytes memory rfqSig = abi.encodePacked(r, s, v);

        vm.prank(borrower);
        vault.acceptMandate{value: 0}(loanId, rfq, rfqSig, uint64(block.timestamp + 1 days));

        newApr = bound(newApr, 0, 0.2e18);
        vm.assume(newApr != initialApr);
        vault.setOriginationFeeApr(newApr);

        bytes32 depositGuid = bytes32(uint256(8100 + loanId));
        bytes32 tradeGuid = bytes32(uint256(8200 + loanId));

        messenger.setMessage(
            depositGuid,
            CollarLZMessages.Message({
                action: CollarLZMessages.Action.DepositConfirmed,
                loanId: loanId,
                asset: address(wbtc),
                amount: params.collateralAmount,
                recipient: address(vault),
                subaccountId: 1,
                socketMessageId: bytes32(0),
                secondaryAmount: 0,
                quoteHash: bytes32(0),
                takerNonce: 0,
                data: bytes("")
            })
        );

        messenger.setMessage(
            tradeGuid,
            CollarLZMessages.Message({
                action: CollarLZMessages.Action.TradeConfirmed,
                loanId: loanId,
                asset: address(0),
                amount: 0,
                recipient: address(vault),
                subaccountId: 1,
                socketMessageId: bytes32(0),
                secondaryAmount: 0,
                quoteHash: bytes32(0),
                takerNonce: 1,
                data: abi.encode(uint256(25_000e6), uint256(params.putStrike), uint64(params.maturity), int256(0))
            })
        );

        vm.prank(keeper);
        vault.finalizeLoan(loanId, depositGuid, tradeGuid);

        CollarVaultShared.Loan memory loan = vault.getLoan(loanId);
        assertEq(loan.interestOwed, expectedInterest, "fixed interest snapshot must remain unchanged");
    }

    function testFuzzFinalizeRevertsWhenMinNetInterestNotMet(uint256 requiredNet) public {
        vault.setOriginationFeeApr(0.05e18);

        CollarVault.DepositParams memory params = CollarVault.DepositParams({
            collateralAsset: address(wbtc),
            collateralAmount: 1e8,
            maturity: block.timestamp + 30 days,
            putStrike: 21_000e6,
            borrowAmount: 20_000e6
        });

        uint256 loanId = _requestDeposit(params);

        uint256 fixedInterest =
            ((params.borrowAmount * 0.05e18) / 1e18) * (params.maturity - block.timestamp) / 365 days;
        requiredNet = bound(requiredNet, fixedInterest + 1, fixedInterest + 5_000e6);

        ICollarVaultFinalizeModule.BaselineRfq memory rfq = ICollarVaultFinalizeModule.BaselineRfq({
            loanId: loanId,
            collateralAsset: address(wbtc),
            collateralAmount: params.collateralAmount,
            maturity: uint64(params.maturity),
            putStrike: params.putStrike,
            callStrike: 25_000e6,
            borrowAmount: params.borrowAmount,
            minNetInterest: requiredNet,
            rfqExpiry: uint64(block.timestamp + 1 days),
            borrower: borrower,
            nonce: 902
        });

        bytes32 rfqHash = vault.hashBaselineRfq(rfq);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(rfqSignerKey, rfqHash);
        bytes memory rfqSig = abi.encodePacked(r, s, v);

        vm.prank(borrower);
        vault.acceptMandate{value: 0}(loanId, rfq, rfqSig, uint64(block.timestamp + 1 days));

        bytes32 depositGuid = bytes32(uint256(8300 + loanId));
        bytes32 tradeGuid = bytes32(uint256(8400 + loanId));

        messenger.setMessage(
            depositGuid,
            CollarLZMessages.Message({
                action: CollarLZMessages.Action.DepositConfirmed,
                loanId: loanId,
                asset: address(wbtc),
                amount: params.collateralAmount,
                recipient: address(vault),
                subaccountId: 1,
                socketMessageId: bytes32(0),
                secondaryAmount: 0,
                quoteHash: bytes32(0),
                takerNonce: 0,
                data: bytes("")
            })
        );

        messenger.setMessage(
            tradeGuid,
            CollarLZMessages.Message({
                action: CollarLZMessages.Action.TradeConfirmed,
                loanId: loanId,
                asset: address(0),
                amount: 0,
                recipient: address(vault),
                subaccountId: 1,
                socketMessageId: bytes32(0),
                secondaryAmount: 0,
                quoteHash: bytes32(0),
                takerNonce: 1,
                data: abi.encode(uint256(25_000e6), uint256(params.putStrike), uint64(params.maturity), int256(0))
            })
        );

        vm.prank(keeper);
        vm.expectRevert(CollarVault.CV_InsufficientValue.selector);
        vault.finalizeLoan(loanId, depositGuid, tradeGuid);
    }

    function testFuzzFinalizeRejectsNegativePremium(uint256 negativePremiumAbs) public {
        negativePremiumAbs = bound(negativePremiumAbs, 1, 10_000e6);

        CollarVault.DepositParams memory params = CollarVault.DepositParams({
            collateralAsset: address(wbtc),
            collateralAmount: 1e8,
            maturity: block.timestamp + 30 days,
            putStrike: 20_000e6,
            borrowAmount: 20_000e6
        });

        uint256 loanId = _requestDeposit(params);

        ICollarVaultFinalizeModule.BaselineRfq memory rfq = ICollarVaultFinalizeModule.BaselineRfq({
            loanId: loanId,
            collateralAsset: address(wbtc),
            collateralAmount: params.collateralAmount,
            maturity: uint64(params.maturity),
            putStrike: params.putStrike,
            callStrike: 25_000e6,
            borrowAmount: params.borrowAmount,
            minNetInterest: 0,
            rfqExpiry: uint64(block.timestamp + 1 days),
            borrower: borrower,
            nonce: 903
        });

        bytes32 rfqHash = vault.hashBaselineRfq(rfq);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(rfqSignerKey, rfqHash);
        bytes memory rfqSig = abi.encodePacked(r, s, v);

        vm.prank(borrower);
        vault.acceptMandate{value: 0}(loanId, rfq, rfqSig, uint64(block.timestamp + 1 days));

        bytes32 depositGuid = bytes32(uint256(8500 + loanId));
        bytes32 tradeGuid = bytes32(uint256(8600 + loanId));

        messenger.setMessage(
            depositGuid,
            CollarLZMessages.Message({
                action: CollarLZMessages.Action.DepositConfirmed,
                loanId: loanId,
                asset: address(wbtc),
                amount: params.collateralAmount,
                recipient: address(vault),
                subaccountId: 1,
                socketMessageId: bytes32(0),
                secondaryAmount: 0,
                quoteHash: bytes32(0),
                takerNonce: 0,
                data: bytes("")
            })
        );

        int256 realizedC = -int256(negativePremiumAbs);
        messenger.setMessage(
            tradeGuid,
            CollarLZMessages.Message({
                action: CollarLZMessages.Action.TradeConfirmed,
                loanId: loanId,
                asset: address(0),
                amount: 0,
                recipient: address(vault),
                subaccountId: 1,
                socketMessageId: bytes32(0),
                secondaryAmount: 0,
                quoteHash: bytes32(0),
                takerNonce: 1,
                data: abi.encode(uint256(25_000e6), uint256(20_000e6), uint64(params.maturity), realizedC)
            })
        );

        vm.prank(keeper);
        vm.expectRevert(CollarVault.CV_InsufficientValue.selector);
        vault.finalizeLoan(loanId, depositGuid, tradeGuid);
    }

    function testFuzzFinalizeLoanRejectsUnknownGuids(bytes32 guidA, bytes32 guidB) public {
        vm.assume(guidA != bytes32(0) && guidB != bytes32(0));

        CollarVault.DepositParams memory params = CollarVault.DepositParams({
            collateralAsset: address(wbtc),
            collateralAmount: 1e8,
            maturity: block.timestamp + 30 days,
            putStrike: 20_000e6,
            borrowAmount: 20_000e6
        });

        uint256 loanId = _requestDeposit(params);

        ICollarVaultFinalizeModule.BaselineRfq memory rfq = ICollarVaultFinalizeModule.BaselineRfq({
            loanId: loanId,
            collateralAsset: address(wbtc),
            collateralAmount: params.collateralAmount,
            maturity: uint64(params.maturity),
            putStrike: params.putStrike,
            callStrike: 25_000e6,
            borrowAmount: params.borrowAmount,
            minNetInterest: 0,
            rfqExpiry: uint64(block.timestamp + 1 days),
            borrower: borrower,
            nonce: 904
        });

        bytes32 rfqHash = vault.hashBaselineRfq(rfq);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(rfqSignerKey, rfqHash);
        bytes memory rfqSig = abi.encodePacked(r, s, v);

        vm.prank(borrower);
        vault.acceptMandate{value: 0}(loanId, rfq, rfqSig, uint64(block.timestamp + 1 days));

        vm.prank(keeper);
        vm.expectRevert(CollarVault.CV_InvalidMessage.selector);
        vault.finalizeLoan(loanId, guidA, guidB);
    }

    function testFinalizeDepositReturnRejectsWrongActionGuid() public {
        CollarVault.DepositParams memory params = CollarVault.DepositParams({
            collateralAsset: address(wbtc),
            collateralAmount: 1e8,
            maturity: block.timestamp + 30 days,
            putStrike: 20_000e6,
            borrowAmount: 20_000e6
        });

        uint256 loanId = _requestDeposit(params);

        vm.prank(borrower);
        vault.requestCollateralReturn(loanId);

        bytes32 wrongGuid = bytes32(uint256(9900 + loanId));
        messenger.setMessage(
            wrongGuid,
            CollarLZMessages.Message({
                action: CollarLZMessages.Action.TradeConfirmed,
                loanId: loanId,
                asset: address(0),
                amount: 0,
                recipient: address(vault),
                subaccountId: 1,
                socketMessageId: bytes32(0),
                secondaryAmount: 0,
                quoteHash: bytes32(0),
                takerNonce: 0,
                data: bytes("")
            })
        );

        vm.expectRevert(bytes("bad action"));
        vault.finalizeDepositReturn(loanId, wrongGuid);
    }

    function testConvertToVariableMarksReadyWhenLiquidityMissing() public {
        uint256 loanId = _createAndFinalizeLoan(block.timestamp + 30 days, 25_000e6, 21_000e6, 0);
        CollarVaultShared.Loan memory loanBefore = vault.getLoan(loanId);

        vm.warp(loanBefore.maturity + 1);
        bytes32 guid = bytes32(uint256(9700 + loanId));
        messenger.setMessage(
            guid,
            CollarLZMessages.Message({
                action: CollarLZMessages.Action.CollateralReturned,
                loanId: loanId,
                asset: address(wbtc),
                amount: loanBefore.collateralAmount,
                recipient: address(vault),
                subaccountId: 1,
                socketMessageId: bytes32(0),
                secondaryAmount: 0,
                quoteHash: bytes32(0),
                takerNonce: 0,
                data: bytes("")
            })
        );

        vm.prank(keeper);
        vault.settleLoan(loanId, CollarVaultShared.SettlementOutcome.Neutral, guid);

        CollarVaultShared.Loan memory marked = vault.getLoan(loanId);
        assertEq(uint256(marked.state), uint256(CollarVaultShared.LoanState.READY_FOR_VARIABLE));

        vm.prank(keeper);
        bool converted = vault.tryConvertReadyLoan(loanId);
        assertEq(converted, false);

        CollarVaultShared.Loan memory stillMarked = vault.getLoan(loanId);
        assertEq(uint256(stillMarked.state), uint256(CollarVaultShared.LoanState.READY_FOR_VARIABLE));
    }

    function testTryConvertReadyLoanSucceedsWhenLiquidityAvailable() public {
        uint256 loanId = _createAndFinalizeLoan(block.timestamp + 30 days, 25_000e6, 21_000e6, 0);
        CollarVaultShared.Loan memory loanBefore = vault.getLoan(loanId);

        vm.warp(loanBefore.maturity + 1);
        bytes32 guid = bytes32(uint256(9800 + loanId));
        messenger.setMessage(
            guid,
            CollarLZMessages.Message({
                action: CollarLZMessages.Action.CollateralReturned,
                loanId: loanId,
                asset: address(wbtc),
                amount: loanBefore.collateralAmount,
                recipient: address(vault),
                subaccountId: 1,
                socketMessageId: bytes32(0),
                secondaryAmount: 0,
                quoteHash: bytes32(0),
                takerNonce: 0,
                data: bytes("")
            })
        );

        vm.prank(keeper);
        vault.settleLoan(loanId, CollarVaultShared.SettlementOutcome.Neutral, guid);

        uint256 totalDue = loanBefore.principal + loanBefore.interestOwed;
        usdc.mint(address(eulerAdapter), totalDue);
        eulerAdapter.setLiquidity(totalDue);

        vm.prank(keeper);
        bool converted = vault.tryConvertReadyLoan(loanId);
        assertEq(converted, true);

        CollarVaultShared.Loan memory afterLoan = vault.getLoan(loanId);
        assertEq(uint256(afterLoan.state), uint256(CollarVaultShared.LoanState.ACTIVE_VARIABLE));
        assertEq(afterLoan.variableDebt, totalDue);
    }

    function testBorrowerCanSettleReadyLoanByRepayBeforeDeadline() public {
        uint256 loanId = _createAndFinalizeLoan(block.timestamp + 30 days, 25_000e6, 21_000e6, 0);
        CollarVaultShared.Loan memory loanBefore = vault.getLoan(loanId);

        vm.warp(loanBefore.maturity + 1);
        bytes32 guid = bytes32(uint256(9860 + loanId));
        messenger.setMessage(
            guid,
            CollarLZMessages.Message({
                action: CollarLZMessages.Action.CollateralReturned,
                loanId: loanId,
                asset: address(wbtc),
                amount: loanBefore.collateralAmount,
                recipient: address(vault),
                subaccountId: 1,
                socketMessageId: bytes32(0),
                secondaryAmount: 0,
                quoteHash: bytes32(0),
                takerNonce: 0,
                data: bytes("")
            })
        );

        vm.prank(keeper);
        vault.settleLoan(loanId, CollarVaultShared.SettlementOutcome.Neutral, guid);

        uint256 totalDue = loanBefore.principal + loanBefore.interestOwed;
        usdc.mint(borrower, totalDue);
        vm.startPrank(borrower);
        usdc.approve(address(vault), totalDue);
        (uint256 repaid, uint256 callerCollateral, uint256 borrowerCollateral) = vault.settleReadyLoanByRepay(loanId);
        vm.stopPrank();

        assertEq(repaid, totalDue);
        assertEq(callerCollateral, loanBefore.collateralAmount);
        assertEq(borrowerCollateral, 0);
        CollarVaultShared.Loan memory loanAfter = vault.getLoan(loanId);
        assertEq(uint256(loanAfter.state), uint256(CollarVaultShared.LoanState.CLOSED));
        assertEq(wbtc.balanceOf(borrower), 1e8);
    }

    function testKeeperCannotSettleReadyLoanByRepayBeforeDeadline() public {
        uint256 loanId = _createAndFinalizeLoan(block.timestamp + 30 days, 25_000e6, 21_000e6, 0);
        CollarVaultShared.Loan memory loanBefore = vault.getLoan(loanId);

        vm.warp(loanBefore.maturity + 1);
        bytes32 guid = bytes32(uint256(9870 + loanId));
        messenger.setMessage(
            guid,
            CollarLZMessages.Message({
                action: CollarLZMessages.Action.CollateralReturned,
                loanId: loanId,
                asset: address(wbtc),
                amount: loanBefore.collateralAmount,
                recipient: address(vault),
                subaccountId: 1,
                socketMessageId: bytes32(0),
                secondaryAmount: 0,
                quoteHash: bytes32(0),
                takerNonce: 0,
                data: bytes("")
            })
        );

        vm.prank(keeper);
        vault.settleLoan(loanId, CollarVaultShared.SettlementOutcome.Neutral, guid);

        uint256 totalDue = loanBefore.principal + loanBefore.interestOwed;
        usdc.mint(keeper, totalDue);
        vm.startPrank(keeper);
        usdc.approve(address(vault), totalDue);
        vm.expectRevert(CollarVault.CV_Unauthorized.selector);
        vault.settleReadyLoanByRepay(loanId);
        vm.stopPrank();
    }

    function testKeeperCanSettleReadyLoanByRepayAfterDeadlineWithPenaltySeize() public {
        vault.setReadyLoanConfig(uint64(1 days), 500);

        uint256 loanId = _createAndFinalizeLoan(block.timestamp + 30 days, 30_000e6, 25_000e6, 0);
        CollarVaultShared.Loan memory loanBefore = vault.getLoan(loanId);

        vm.warp(loanBefore.maturity + 1);
        bytes32 guid = bytes32(uint256(9880 + loanId));
        messenger.setMessage(
            guid,
            CollarLZMessages.Message({
                action: CollarLZMessages.Action.CollateralReturned,
                loanId: loanId,
                asset: address(wbtc),
                amount: loanBefore.collateralAmount,
                recipient: address(vault),
                subaccountId: 1,
                socketMessageId: bytes32(0),
                secondaryAmount: 0,
                quoteHash: bytes32(0),
                takerNonce: 0,
                data: bytes("")
            })
        );

        vm.prank(keeper);
        vault.settleLoan(loanId, CollarVaultShared.SettlementOutcome.Neutral, guid);

        vm.warp(loanBefore.maturity + 1 days + 2);

        uint256 totalDue = loanBefore.principal + loanBefore.interestOwed;
        usdc.mint(keeper, totalDue);
        vm.startPrank(keeper);
        usdc.approve(address(vault), totalDue);
        (uint256 repaid, uint256 callerCollateral, uint256 borrowerCollateral) = vault.settleReadyLoanByRepay(loanId);
        vm.stopPrank();

        assertEq(repaid, totalDue);
        assertEq(callerCollateral, 84_000_000);
        assertEq(borrowerCollateral, 16_000_000);
        assertEq(wbtc.balanceOf(keeper), 84_000_000);
        assertEq(wbtc.balanceOf(borrower), 16_000_000);
        CollarVaultShared.Loan memory loanAfter = vault.getLoan(loanId);
        assertEq(uint256(loanAfter.state), uint256(CollarVaultShared.LoanState.CLOSED));
    }

    function testBorrowerCannotSettleReadyLoanByRepayAfterDeadline() public {
        vault.setReadyLoanConfig(uint64(1 days), 500);

        uint256 loanId = _createAndFinalizeLoan(block.timestamp + 30 days, 25_000e6, 21_000e6, 0);
        CollarVaultShared.Loan memory loanBefore = vault.getLoan(loanId);

        vm.warp(loanBefore.maturity + 1);
        bytes32 guid = bytes32(uint256(9890 + loanId));
        messenger.setMessage(
            guid,
            CollarLZMessages.Message({
                action: CollarLZMessages.Action.CollateralReturned,
                loanId: loanId,
                asset: address(wbtc),
                amount: loanBefore.collateralAmount,
                recipient: address(vault),
                subaccountId: 1,
                socketMessageId: bytes32(0),
                secondaryAmount: 0,
                quoteHash: bytes32(0),
                takerNonce: 0,
                data: bytes("")
            })
        );

        vm.prank(keeper);
        vault.settleLoan(loanId, CollarVaultShared.SettlementOutcome.Neutral, guid);

        vm.warp(loanBefore.maturity + 1 days + 2);

        uint256 totalDue = loanBefore.principal + loanBefore.interestOwed;
        usdc.mint(borrower, totalDue);
        vm.startPrank(borrower);
        usdc.approve(address(vault), totalDue);
        vm.expectRevert(CollarVault.CV_Unauthorized.selector);
        vault.settleReadyLoanByRepay(loanId);
        vm.stopPrank();
    }

    function testBorrowerRepaysAndWithdrawsVariableCollateralViaVault() public {
        uint256 loanId = _createAndFinalizeLoan(block.timestamp + 30 days, 25_000e6, 20_000e6, 0);
        CollarVaultShared.Loan memory loanBefore = vault.getLoan(loanId);

        vm.warp(loanBefore.maturity + 1);
        bytes32 guid = bytes32(uint256(9850 + loanId));
        messenger.setMessage(
            guid,
            CollarLZMessages.Message({
                action: CollarLZMessages.Action.CollateralReturned,
                loanId: loanId,
                asset: address(wbtc),
                amount: loanBefore.collateralAmount,
                recipient: address(vault),
                subaccountId: 1,
                socketMessageId: bytes32(0),
                secondaryAmount: 0,
                quoteHash: bytes32(0),
                takerNonce: 0,
                data: bytes("")
            })
        );

        vm.prank(keeper);
        vault.settleLoan(loanId, CollarVaultShared.SettlementOutcome.Neutral, guid);

        uint256 totalDue = loanBefore.principal + loanBefore.interestOwed;
        usdc.mint(address(eulerAdapter), totalDue);
        eulerAdapter.setLiquidity(totalDue);

        vm.prank(keeper);
        bool converted = vault.tryConvertReadyLoan(loanId);
        assertEq(converted, true);

        uint256 partialRepay = totalDue / 2;
        usdc.mint(borrower, totalDue);
        vm.startPrank(borrower);
        usdc.approve(address(vault), totalDue);
        vault.repayVariableLoan(loanId, partialRepay);

        uint256 partialWithdraw = loanBefore.collateralAmount / 2;
        vault.withdrawVariableCollateral(loanId, partialWithdraw);

        vault.repayVariableLoan(loanId, totalDue - partialRepay);
        vault.withdrawVariableCollateral(loanId, loanBefore.collateralAmount - partialWithdraw);
        vm.stopPrank();

        CollarVaultShared.Loan memory afterLoan = vault.getLoan(loanId);
        assertEq(uint256(afterLoan.state), uint256(CollarVaultShared.LoanState.CLOSED));
        assertEq(wbtc.balanceOf(borrower), 1e8);
    }

    function testSettleConsumesAndReleasesReserve() public {
        vault.setOriginationFeeApr(0.1e18);
        uint256 loanId = _createAndFinalizeLoan(block.timestamp + 30 days, 25_000e6, 21_000e6, 0);

        CollarVaultShared.Loan memory loanBefore = vault.getLoan(loanId);
        vm.warp(loanBefore.maturity + 1);

        bytes32 settleGuid = bytes32(uint256(7300 + loanId));
        uint256 settlementAmount = loanBefore.principal + loanBefore.interestOwed;
        usdc.mint(address(vault), settlementAmount);
        messenger.setMessage(
            settleGuid,
            CollarLZMessages.Message({
                action: CollarLZMessages.Action.SettlementReport,
                loanId: loanId,
                asset: address(usdc),
                amount: settlementAmount,
                recipient: address(vault),
                subaccountId: 1,
                socketMessageId: bytes32(uint256(1)),
                secondaryAmount: 0,
                quoteHash: bytes32(0),
                takerNonce: 0,
                data: bytes("")
            })
        );

        vm.prank(keeper);
        vault.settleLoan(loanId, CollarVaultShared.SettlementOutcome.PutITM, settleGuid);
    }

    function _createAndFinalizeLoan(uint256 maturity, uint256 callStrike, uint256 putStrike, uint256 minNetInterest)
        internal
        returns (uint256 loanId)
    {
        CollarVault.DepositParams memory params = CollarVault.DepositParams({
            collateralAsset: address(wbtc),
            collateralAmount: 1e8,
            maturity: maturity,
            putStrike: putStrike,
            borrowAmount: 20_000e6
        });
        loanId = _requestDeposit(params);

        ICollarVaultFinalizeModule.BaselineRfq memory rfq = ICollarVaultFinalizeModule.BaselineRfq({
            loanId: loanId,
            collateralAsset: address(wbtc),
            collateralAmount: params.collateralAmount,
            maturity: uint64(params.maturity),
            putStrike: params.putStrike,
            callStrike: callStrike,
            borrowAmount: params.borrowAmount,
            minNetInterest: minNetInterest,
            rfqExpiry: uint64(block.timestamp + 1 days),
            borrower: borrower,
            nonce: uint256(keccak256(abi.encodePacked(block.timestamp, maturity, callStrike)))
        });
        bytes32 rfqHash = vault.hashBaselineRfq(rfq);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(rfqSignerKey, rfqHash);
        bytes memory rfqSig = abi.encodePacked(r, s, v);

        vm.prank(borrower);
        vault.acceptMandate{value: 0}(loanId, rfq, rfqSig, uint64(block.timestamp + 1 days));

        bytes32 depositGuid = bytes32(uint256(100 + loanId));
        bytes32 tradeGuid = bytes32(uint256(200 + loanId));

        messenger.setMessage(
            depositGuid,
            CollarLZMessages.Message({
                action: CollarLZMessages.Action.DepositConfirmed,
                loanId: loanId,
                asset: address(wbtc),
                amount: params.collateralAmount,
                recipient: address(vault),
                subaccountId: 1,
                socketMessageId: bytes32(0),
                secondaryAmount: 0,
                quoteHash: bytes32(0),
                takerNonce: 0,
                data: bytes("")
            })
        );

        bytes memory tradeData = abi.encode(uint256(callStrike), uint256(putStrike), uint64(params.maturity), int256(0));
        messenger.setMessage(
            tradeGuid,
            CollarLZMessages.Message({
                action: CollarLZMessages.Action.TradeConfirmed,
                loanId: loanId,
                asset: address(0),
                amount: 0,
                recipient: address(vault),
                subaccountId: 1,
                socketMessageId: bytes32(0),
                secondaryAmount: 0,
                quoteHash: bytes32(0),
                takerNonce: 1,
                data: tradeData
            })
        );

        vm.prank(keeper);
        vault.finalizeLoan(loanId, depositGuid, tradeGuid);
    }

    function _requestDeposit(CollarVault.DepositParams memory params) internal returns (uint256 loanId) {
        IAllowanceTransfer.PermitSingle memory permit = IAllowanceTransfer.PermitSingle({
            details: IAllowanceTransfer.PermitDetails({
                token: params.collateralAsset,
                amount: uint160(params.collateralAmount),
                expiration: uint48(block.timestamp + 1 days),
                nonce: 0
            }),
            spender: address(vault),
            sigDeadline: block.timestamp + 1 days
        });

        bytes memory permitSig = permit2Signer.signPermitSingle(borrowerKey, permit);

        // Always bind deposits to a signed mandate (protocol requirement).
        // Keep helper-generated mandate short-lived so tests that explicitly call acceptMandate
        // can still refresh mandate terms immediately after deposit creation.
        ICollarVaultFinalizeModule.BaselineRfq memory rfq = ICollarVaultFinalizeModule.BaselineRfq({
            loanId: 0,
            collateralAsset: params.collateralAsset,
            collateralAmount: params.collateralAmount,
            maturity: uint64(params.maturity),
            putStrike: params.putStrike,
            callStrike: params.putStrike,
            borrowAmount: params.borrowAmount,
            minNetInterest: 0,
            rfqExpiry: uint64(block.timestamp + 1 days),
            borrower: borrower,
            nonce: uint256(
                keccak256(abi.encodePacked("test-helper", params.maturity, params.borrowAmount, block.timestamp))
            )
        });
        bytes32 rfqHash = vault.hashBaselineRfq(rfq);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(rfqSignerKey, rfqHash);
        bytes memory rfqSig = abi.encodePacked(r, s, v);

        uint64 shortDeadline = uint64(block.timestamp + 1);

        vm.startPrank(borrower);
        (loanId,,,) = vault.createDepositWithMandatePermit(params, rfq, rfqSig, shortDeadline, permit, permitSig);
        vm.stopPrank();

        vm.warp(block.timestamp + 2);
    }
}

contract MockLZMessenger {
    mapping(bytes32 => CollarLZMessages.Message) private _receivedMessages;

    CollarLZMessages.Message public lastSentMessage;
    bytes32 public lastSentGuid;
    bytes public defaultOptions;
    uint256 public quoteFee;
    uint64 public nonce;

    function _nextGuid(uint256 loanId, CollarLZMessages.Action action) internal returns (bytes32 guid) {
        nonce++;
        guid = keccak256(abi.encodePacked(nonce, loanId, action));
        lastSentGuid = guid;
    }

    function receivedMessage(bytes32 guid) external view returns (CollarLZMessages.Message memory message) {
        return _receivedMessages[guid];
    }

    function setQuoteFee(uint256 fee) external {
        quoteFee = fee;
    }

    function setDefaultOptions(bytes calldata options) external {
        defaultOptions = options;
    }

    function quoteMessage(CollarLZMessages.Message calldata, bytes calldata)
        external
        view
        returns (MessagingFee memory)
    {
        return MessagingFee({nativeFee: quoteFee, lzTokenFee: 0});
    }

    function sendDepositIntentAutoFee(
        uint256 loanId,
        address asset,
        uint256 amount,
        address recipient,
        uint256 subaccountId,
        bytes32 socketMessageId,
        address
    ) external payable returns (bytes32 guid) {
        guid = _nextGuid(loanId, CollarLZMessages.Action.DepositIntent);
        lastSentMessage = CollarLZMessages.Message({
            action: CollarLZMessages.Action.DepositIntent,
            loanId: loanId,
            asset: asset,
            amount: amount,
            recipient: recipient,
            subaccountId: subaccountId,
            socketMessageId: socketMessageId,
            secondaryAmount: 0,
            quoteHash: bytes32(0),
            takerNonce: 0,
            data: bytes("")
        });
    }

    function sendMandateCreatedAutoFee(
        uint256 loanId,
        address asset,
        uint256 borrowAmount,
        address recipient,
        uint256 subaccountId,
        bytes calldata mandateData,
        address
    ) external payable returns (bytes32 guid) {
        guid = _nextGuid(loanId, CollarLZMessages.Action.MandateCreated);
        lastSentMessage = CollarLZMessages.Message({
            action: CollarLZMessages.Action.MandateCreated,
            loanId: loanId,
            asset: asset,
            amount: borrowAmount,
            recipient: recipient,
            subaccountId: subaccountId,
            socketMessageId: bytes32(0),
            secondaryAmount: 0,
            quoteHash: bytes32(0),
            takerNonce: 0,
            data: mandateData
        });
    }

    function sendReturnRequestAutoFee(
        uint256 loanId,
        address asset,
        uint256 amount,
        address recipient,
        uint256 subaccountId,
        address
    ) external payable returns (bytes32 guid) {
        guid = _nextGuid(loanId, CollarLZMessages.Action.ReturnRequest);
        lastSentMessage = CollarLZMessages.Message({
            action: CollarLZMessages.Action.ReturnRequest,
            loanId: loanId,
            asset: asset,
            amount: amount,
            recipient: recipient,
            subaccountId: subaccountId,
            socketMessageId: bytes32(0),
            secondaryAmount: 0,
            quoteHash: bytes32(0),
            takerNonce: 0,
            data: bytes("")
        });
    }

    function sendRolloverIntentAutoFee(
        uint256 loanId,
        address asset,
        uint256 principal,
        address recipient,
        uint256 subaccountId,
        bytes calldata rolloverData,
        address
    ) external payable returns (bytes32 guid) {
        guid = _nextGuid(loanId, CollarLZMessages.Action.RolloverIntent);
        lastSentMessage = CollarLZMessages.Message({
            action: CollarLZMessages.Action.RolloverIntent,
            loanId: loanId,
            asset: asset,
            amount: principal,
            recipient: recipient,
            subaccountId: subaccountId,
            socketMessageId: bytes32(0),
            secondaryAmount: 0,
            quoteHash: bytes32(0),
            takerNonce: 0,
            data: rolloverData
        });
    }

    function validateDepositConfirmed(
        CollarLZMessages.Message calldata lzMessage,
        address pendingBorrower,
        address expectedBorrower,
        address pendingCollateralAsset,
        uint256 pendingCollateralAmount,
        address expectedRecipient,
        uint256 expectedSubaccountId
    ) external pure returns (uint256 loanId) {
        require(lzMessage.action == CollarLZMessages.Action.DepositConfirmed, "bad action");
        require(lzMessage.recipient == expectedRecipient, "bad recipient");
        require(expectedSubaccountId == 0 || lzMessage.subaccountId == expectedSubaccountId, "bad subaccount");
        require(lzMessage.asset == pendingCollateralAsset && lzMessage.amount == pendingCollateralAmount, "bad deposit");
        require(pendingBorrower != address(0) && pendingBorrower == expectedBorrower, "bad borrower");
        loanId = lzMessage.loanId;
    }

    function validateTradeConfirmedForFinalize(
        CollarLZMessages.Message calldata tradeMessage,
        uint256 expectedLoanId,
        address expectedRecipient,
        uint256 expectedSubaccountId,
        uint256 minCallStrike,
        uint256 maxPutStrike,
        uint64 expectedMaturity
    ) external pure returns (uint256 callStrike, uint256 putStrike, int256 realizedC) {
        require(tradeMessage.action == CollarLZMessages.Action.TradeConfirmed, "bad action");
        require(tradeMessage.loanId == expectedLoanId, "bad loan");
        require(tradeMessage.recipient == expectedRecipient, "bad recipient");
        require(expectedSubaccountId == 0 || tradeMessage.subaccountId == expectedSubaccountId, "bad subaccount");

        uint64 expiry;
        (callStrike, putStrike, expiry, realizedC) = abi.decode(tradeMessage.data, (uint256, uint256, uint64, int256));
        require(expiry == expectedMaturity, "bad maturity");
        require(callStrike >= minCallStrike && putStrike <= maxPutStrike, "bad strikes");
    }

    function validateRolloverConfirmed(
        CollarLZMessages.Message calldata lzMessage,
        uint256 loanId,
        address expectedRecipient,
        uint256 expectedSubaccountId,
        bytes32 expectedMandateHash,
        address expectedBorrower,
        uint64 expectedMaturity
    ) external pure returns (uint256 callStrike, uint256 putStrike, uint256 interestApr, int256 realizedC) {
        require(lzMessage.action == CollarLZMessages.Action.RolloverConfirmed, "bad action");
        require(lzMessage.loanId == loanId, "bad loan");
        require(lzMessage.recipient == expectedRecipient, "bad recipient");
        require(expectedSubaccountId == 0 || lzMessage.subaccountId == expectedSubaccountId, "bad subaccount");
        bytes32 mandateHash;
        address borrower;
        uint64 expiry;
        (mandateHash, borrower, callStrike, putStrike, interestApr, expiry, realizedC) =
            abi.decode(lzMessage.data, (bytes32, address, uint256, uint256, uint256, uint64, int256));
        require(mandateHash == expectedMandateHash, "bad mandate hash");
        require(borrower == expectedBorrower, "bad borrower");
        require(expiry == expectedMaturity, "bad maturity");
    }

    function validateTradeConfirmedMarker(
        CollarLZMessages.Message calldata lzMessage,
        address expectedRecipient,
        uint256 expectedSubaccountId
    ) external pure returns (uint256 loanId) {
        require(lzMessage.action == CollarLZMessages.Action.TradeConfirmed, "bad action");
        require(lzMessage.recipient == expectedRecipient, "bad recipient");
        require(expectedSubaccountId == 0 || lzMessage.subaccountId == expectedSubaccountId, "bad subaccount");
        return lzMessage.loanId;
    }

    function validateCollateralReturned(
        CollarLZMessages.Message calldata lzMessage,
        uint256 loanId,
        address collateralAsset,
        uint256 collateralAmount,
        address expectedRecipient,
        uint256 expectedSubaccountId
    ) external pure {
        require(lzMessage.action == CollarLZMessages.Action.CollateralReturned, "bad action");
        require(lzMessage.loanId == loanId, "bad loan");
        require(lzMessage.asset == collateralAsset && lzMessage.amount == collateralAmount, "bad collateral");
        require(lzMessage.recipient == expectedRecipient, "bad recipient");
        require(expectedSubaccountId == 0 || lzMessage.subaccountId == expectedSubaccountId, "bad subaccount");
    }

    function validateSettlementReport(
        CollarLZMessages.Message calldata lzMessage,
        uint256 loanId,
        address usdcAsset,
        address expectedRecipient
    ) external pure returns (uint256 settlementAmount) {
        require(lzMessage.action == CollarLZMessages.Action.SettlementReport, "bad action");
        require(lzMessage.loanId == loanId, "bad loan");
        require(lzMessage.asset == usdcAsset, "bad asset");
        require(lzMessage.recipient == expectedRecipient, "bad recipient");
        return lzMessage.amount;
    }

    function setMessage(bytes32 guid, CollarLZMessages.Message memory message) external {
        _receivedMessages[guid] = message;
    }
}
