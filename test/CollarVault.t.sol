// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";

import {CollarLiquidityVault} from "../src/CollarLiquidityVault.sol";
import {CollarVault, ILiquidityVault} from "../src/CollarVault.sol";
import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";
import {CollarVaultShared} from "../src/modules/CollarVaultShared.sol";
import {CollarVaultFinalizeModule} from "../src/modules/CollarVaultFinalizeModule.sol";
import {CollarVaultSettleModule} from "../src/modules/CollarVaultSettleModule.sol";
import {CollarLZMessages} from "../src/bridge/CollarLZMessages.sol";
import {ICollarVaultMessenger} from "../src/interfaces/ICollarVaultMessenger.sol";
import {IEulerAdapter} from "../src/interfaces/IEulerAdapter.sol";
import {IBridgeAdapter} from "../src/interfaces/IBridgeAdapter.sol";

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
    CollarLiquidityVault internal liquidityVault;
    MockBridge internal bridge;
    MockBridgeAdapter internal adapter;
    MockEulerAdapter internal eulerAdapter;
    CollarVault internal vault;
    MockLZMessenger internal messenger;
    CollarVaultFinalizeModule internal finalizeModule;
    CollarVaultSettleModule internal settleModule;

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
        adapter = new MockBridgeAdapter();
        eulerAdapter = new MockEulerAdapter();
        messenger = new MockLZMessenger();
        finalizeModule = new CollarVaultFinalizeModule();
        settleModule = new CollarVaultSettleModule();
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
                IEulerAdapter(address(eulerAdapter)),
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

        vault.setCollateralConfig(address(wbtc), true, 1e8);
        // Unit-test setup uses same asset on both sides.
        vault.setL2MessageAsset(address(wbtc), address(wbtc));
        vault.setSocketVaultConfig(address(wbtc), IBridgeAdapter(address(adapter)));
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

        CollarVaultShared.Loan memory loan = vault.getLoan(loanId);
        assertEq(uint256(loan.state), uint256(CollarVaultShared.LoanState.ACTIVE_ZERO_COST));
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

    function sendMessage(CollarLZMessages.Message calldata message) external payable returns (MessagingReceipt memory) {
        bytes32 guid = _nextGuid(message.loanId, message.action);
        lastSentMessage = message;
        return MessagingReceipt({guid: guid, nonce: nonce, fee: MessagingFee(msg.value, 0)});
    }

    function sendMessageAutoFee(CollarLZMessages.Message calldata message, address)
        external
        payable
        returns (bytes32 guid)
    {
        guid = _nextGuid(message.loanId, message.action);
        lastSentMessage = message;
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
    ) external pure returns (uint256 callStrike, uint256 putStrike) {
        require(tradeMessage.action == CollarLZMessages.Action.TradeConfirmed, "bad action");
        require(tradeMessage.loanId == expectedLoanId, "bad loan");
        require(tradeMessage.recipient == expectedRecipient, "bad recipient");
        require(expectedSubaccountId == 0 || tradeMessage.subaccountId == expectedSubaccountId, "bad subaccount");

        uint64 expiry;
        (callStrike, putStrike, expiry) = abi.decode(tradeMessage.data, (uint256, uint256, uint64));
        require(expiry == expectedMaturity, "bad maturity");
        require(callStrike >= minCallStrike && putStrike <= maxPutStrike, "bad strikes");
    }

    function validateOriginationFee(CollarLZMessages.Message calldata lzMessage, uint256 feeAmount, address usdcAsset)
        external
        pure
    {
        if (feeAmount == 0) {
            require(lzMessage.amount == 0, "unexpected fee");
            return;
        }
        require(lzMessage.asset == usdcAsset, "bad fee asset");
        require(lzMessage.amount == feeAmount, "bad fee amount");
        require(lzMessage.socketMessageId != bytes32(0), "missing fee socket id");
    }

    function setMessage(bytes32 guid, CollarLZMessages.Message memory message) external {
        _receivedMessages[guid] = message;
    }
}
