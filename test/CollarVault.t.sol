// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";

import {CollarLiquidityVault} from "../src/CollarLiquidityVault.sol";
import {CollarVault, ILiquidityVault} from "../src/CollarVault.sol";
import {CollarLZMessages} from "../src/bridge/CollarLZMessages.sol";
import {ICollarVaultMessenger} from "../src/interfaces/ICollarVaultMessenger.sol";
import {IEulerAdapter} from "../src/interfaces/IEulerAdapter.sol";
import {ISocketBridge} from "../src/interfaces/ISocketBridge.sol";
import {ISocketConnector} from "../src/interfaces/ISocketConnector.sol";

import {
    MessagingFee,
    MessagingReceipt
} from "@layerzerolabs/lz-evm-protocol-v2/contracts/interfaces/ILayerZeroEndpointV2.sol";

import {IAllowanceTransfer} from "permit2/src/interfaces/IAllowanceTransfer.sol";
import {DeployPermit2} from "permit2/test/utils/DeployPermit2.sol";
import {Permit2ECDSASigner} from "../lib/euler-earn/lib/euler-vault-kit/test/mocks/Permit2ECDSASigner.sol";

import {MockBridge} from "./mocks/MockBridge.sol";
import {MockConnector} from "./mocks/MockConnector.sol";
import {MockERC20} from "./mocks/MockERC20.sol";
import {MockEulerAdapter} from "./mocks/MockEulerAdapter.sol";

contract CollarVaultTest is Test {
    uint256 internal rfqSignerKey = 0xA11CE;
    address internal rfqSigner;

    MockERC20 internal usdc;
    MockERC20 internal wbtc;
    CollarLiquidityVault internal liquidityVault;
    MockBridge internal bridge;
    MockConnector internal connector;
    MockEulerAdapter internal eulerAdapter;
    CollarVault internal vault;
    MockLZMessenger internal messenger;

    uint256 internal borrowerKey = 0xB0B0;
    address internal borrower;
    address internal treasury = address(0xB0B1);
    address internal keeper = address(0xA11CE);

    IAllowanceTransfer internal permit2;
    Permit2ECDSASigner internal permit2Signer;

    function setUp() public {
        usdc = new MockERC20("USD Coin", "USDC", 6);
        wbtc = new MockERC20("Wrapped BTC", "WBTC", 8);
        liquidityVault = new CollarLiquidityVault(usdc, "Collar USDC", "cUSDC", address(this));
        bridge = new MockBridge(wbtc);
        connector = new MockConnector(0);
        eulerAdapter = new MockEulerAdapter();
        messenger = new MockLZMessenger();
        borrower = vm.addr(borrowerKey);
        rfqSigner = vm.addr(rfqSignerKey);

        address permit2Address = new DeployPermit2().deployPermit2();
        permit2 = IAllowanceTransfer(permit2Address);
        permit2Signer = new Permit2ECDSASigner(permit2Address);

        vault = new CollarVault(
            address(this),
            ILiquidityVault(address(liquidityVault)),
            address(this),
            IEulerAdapter(address(eulerAdapter)),
            permit2,
            address(0x1001),
            treasury
        );
        vault.setTreasuryConfig(treasury, 0);
        vault.setLZMessenger(ICollarVaultMessenger(address(messenger)));

        vault.setCollateralConfig(address(wbtc), true, 1e8);
        vault.setSocketBridgeConfig(
            address(wbtc), ISocketBridge(address(bridge)), ISocketConnector(address(connector)), 200_000, "", ""
        );
        vault.grantRole(vault.KEEPER_ROLE(), keeper);
        vault.setDeriveSubaccountId(1);
        vault.setRfqSigner(rfqSigner, true);

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

    function testCreateLoanHappyPathViaMandate() public {
        CollarVault.DepositParams memory params = CollarVault.DepositParams({
            collateralAsset: address(wbtc),
            collateralAmount: 1e8,
            maturity: block.timestamp + 30 days,
            putStrike: 20_000e6,
            borrowAmount: 20_000e6
        });

        uint256 loanId = _requestDeposit(params);

        CollarVault.BaselineRfq memory rfq = CollarVault.BaselineRfq({
            loanId: loanId,
            collateralAsset: address(wbtc),
            collateralAmount: params.collateralAmount,
            maturity: uint64(params.maturity),
            putStrike: params.putStrike,
            callStrike: 25_000e6,
            borrowAmount: params.borrowAmount,
            rfqExpiry: uint64(block.timestamp + 1 days),
            borrower: borrower,
            nonce: 1
        });

        bytes32 rfqHash = vault.hashBaselineRfq(rfq);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(rfqSignerKey, rfqHash);
        bytes memory rfqSig = abi.encodePacked(r, s, v);

        vm.prank(borrower);
        vault.acceptMandate{value: 0}(loanId, rfq, rfqSig, uint64(block.timestamp + 1 days));

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

        bytes memory tradeData = abi.encode(uint256(25_000e6), uint256(20_000e6), uint64(params.maturity));

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

        CollarVault.Loan memory loan = vault.getLoan(loanId);
        assertEq(uint256(loan.state), uint256(CollarVault.LoanState.ACTIVE_ZERO_COST));
        assertEq(loan.borrower, borrower);
        assertEq(loan.collateralAsset, address(wbtc));
        assertEq(loan.collateralAmount, 1e8);
        assertEq(loan.principal, 20_000e6);
        assertEq(loan.putStrike, 20_000e6);
        assertEq(loan.callStrike, 25_000e6);
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

        vm.startPrank(borrower);
        (loanId,,) = vault.createDepositWithPermit(params, permit, permitSig);
        vm.stopPrank();
    }
}

contract MockLZMessenger {
    mapping(bytes32 => CollarLZMessages.Message) private _receivedMessages;

    CollarLZMessages.Message public lastSentMessage;
    bytes32 public lastSentGuid;
    bytes public defaultOptions;
    uint256 public quoteFee;
    uint64 public nonce;

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

    function sendMessage(CollarLZMessages.Message calldata message) external payable returns (MessagingReceipt memory) {
        nonce++;
        bytes32 guid = keccak256(abi.encodePacked(nonce, message.loanId, message.action));
        lastSentMessage = message;
        lastSentGuid = guid;
        return MessagingReceipt({guid: guid, nonce: nonce, fee: MessagingFee(msg.value, 0)});
    }

    function setMessage(bytes32 guid, CollarLZMessages.Message memory message) external {
        _receivedMessages[guid] = message;
    }
}
