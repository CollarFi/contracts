// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {
    MessagingFee,
    MessagingReceipt,
    Origin
} from "@layerzerolabs/lz-evm-protocol-v2/contracts/interfaces/ILayerZeroEndpointV2.sol";
import {OApp} from "@layerzerolabs/lz-evm-oapp-v2/contracts/oapp/OApp.sol";

import {IActionVerifier} from "v2-matching/src/interfaces/IActionVerifier.sol";
// forge-lint: disable-next-line(unused-import)
import {IMatchingModule} from "v2-matching/src/interfaces/IMatchingModule.sol";
import {IDepositModule} from "v2-matching/src/interfaces/IDepositModule.sol";
import {IWithdrawalModule} from "v2-matching/src/interfaces/IWithdrawalModule.sol";

import {ICollarTSA} from "../interfaces/ICollarTSA.sol";
import {ICollarLoanStore} from "../interfaces/ICollarLoanStore.sol";
import {ISocketMessageTracker} from "../interfaces/ISocketMessageTracker.sol";
import {CollarLZMessages} from "./CollarLZMessages.sol";

interface IRfqNonceTracker {
    function usedNonces(address owner, uint256 nonce) external view returns (bool);
}

/// @notice L2 receiver for LayerZero metadata messages.
contract CollarTSAReceiver is AccessControl, OApp {
    using SafeERC20 for IERC20;

    bytes32 public constant KEEPER_ROLE = keccak256("KEEPER_ROLE");
    bytes32 public constant PARAMETER_ROLE = keccak256("PARAMETER_ROLE");

    ISocketMessageTracker public socket;
    ICollarTSA public tsa;
    ICollarLoanStore public loanStore;
    address public vaultRecipient;

    uint32 public remoteEid;
    bytes public defaultOptions;

    mapping(bytes32 => CollarLZMessages.Message) public pendingMessages;
    mapping(bytes32 => bool) public handledMessages;
    mapping(uint256 => bool) public returnRequested;
    mapping(uint256 => bool) public returnCompleted;
    mapping(uint256 => bool) public tradeConfirmed;

    // Loan terms/collateral accounting is persisted in `loanStore`.

    event MessageReceived(bytes32 indexed guid, CollarLZMessages.Action action, uint256 indexed loanId);
    event MessageHandled(bytes32 indexed guid, CollarLZMessages.Action action, uint256 indexed loanId);
    event MessageSent(bytes32 indexed guid, CollarLZMessages.Action action, uint256 indexed loanId);
    event RemoteEidUpdated(uint32 remoteEid);
    event OptionsUpdated(bytes options);
    event SocketUpdated(address indexed socket);
    event TSAUpdated(address indexed tsa);
    event VaultRecipientUpdated(address indexed recipient);

    error CTR_InvalidPeer();
    error CTR_InvalidRecipient();
    error CTR_MessageNotFound();
    error CTR_MessageAlreadyHandled();
    error CTR_SocketNotFinalized();
    error CTR_RfqModuleNotSet();
    error CTR_RfqTradeNotConfirmed();
    error CTR_InvalidSubaccount();
    error CTR_ReturnAlreadyRequested();
    error CTR_ReturnAlreadyCompleted();
    error CTR_ReturnNotRequested();
    error CTR_ReturnRequestAfterTrade();
    error CTR_CollateralReturnedAfterTrade();
    error CTR_TradeConfirmedAfterReturn();
    error CTR_TradeAlreadyConfirmed();

    constructor(
        address admin,
        address endpoint_,
        ISocketMessageTracker socket_,
        ICollarTSA tsa_,
        ICollarLoanStore loanStore_,
        uint32 remoteEid_
    ) OApp(endpoint_, admin) Ownable(admin) {
        if (address(loanStore_) == address(0)) {
            revert CTR_InvalidPeer();
        }

        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        _grantRole(PARAMETER_ROLE, admin);
        _grantRole(KEEPER_ROLE, admin);

        socket = socket_;
        tsa = tsa_;
        loanStore = loanStore_;
        remoteEid = remoteEid_;
    }

    function setRemoteEid(uint32 newRemoteEid) external onlyRole(PARAMETER_ROLE) {
        remoteEid = newRemoteEid;
        emit RemoteEidUpdated(newRemoteEid);
    }

    function setDefaultOptions(bytes calldata options) external onlyRole(PARAMETER_ROLE) {
        defaultOptions = options;
        emit OptionsUpdated(options);
    }

    function setSocket(ISocketMessageTracker newSocket) external onlyRole(PARAMETER_ROLE) {
        socket = newSocket;
        emit SocketUpdated(address(newSocket));
    }

    function setTSA(ICollarTSA newTsa) external onlyRole(PARAMETER_ROLE) {
        tsa = newTsa;
        emit TSAUpdated(address(newTsa));
    }

    function setVaultRecipient(address recipient) external onlyRole(PARAMETER_ROLE) {
        if (recipient == address(0)) {
            revert CTR_InvalidRecipient();
        }
        vaultRecipient = recipient;
        emit VaultRecipientUpdated(recipient);
    }

    function _lzReceive(Origin calldata, bytes32 guid, bytes calldata message, address, bytes calldata)
        internal
        override
    {
        CollarLZMessages.Message memory decoded = abi.decode(message, (CollarLZMessages.Message));
        pendingMessages[guid] = decoded;
        emit MessageReceived(guid, decoded.action, decoded.loanId);
    }

    function handleMessage(bytes32 guid) external payable onlyRole(KEEPER_ROLE) {
        if (handledMessages[guid]) {
            revert CTR_MessageAlreadyHandled();
        }

        CollarLZMessages.Message memory message = pendingMessages[guid];
        if (message.loanId == 0) {
            revert CTR_MessageNotFound();
        }

        if (message.socketMessageId != bytes32(0) && address(socket) != address(0)) {
            if (!socket.messageExecuted(message.socketMessageId)) {
                revert CTR_SocketNotFinalized();
            }
        }

        if (message.action == CollarLZMessages.Action.MandateCreated) {
            (address borrower, uint256 minCallStrike, uint256 maxPutStrike, uint64 maturity, uint64 deadline) =
                abi.decode(message.data, (address, uint256, uint256, uint64, uint64));

            loanStore.recordMandate(
                message.loanId, borrower, message.asset, message.amount, minCallStrike, maxPutStrike, maturity, deadline
            );

            handledMessages[guid] = true;
            emit MessageHandled(guid, message.action, message.loanId);
            return;
        }

        if (message.action == CollarLZMessages.Action.DepositIntent) {
            if (message.recipient == address(0)) {
                revert CTR_InvalidRecipient();
            }
            if (message.subaccountId != tsa.subAccount()) {
                revert CTR_InvalidSubaccount();
            }

            loanStore.recordCollateral(message.loanId, message.asset, message.amount);

            _signDeposit(message);
            _sendAck(message, CollarLZMessages.Action.DepositConfirmed);
        } else if (message.action == CollarLZMessages.Action.ReturnRequest) {
            if (message.subaccountId != tsa.subAccount()) {
                revert CTR_InvalidSubaccount();
            }
            if (tradeConfirmed[message.loanId]) {
                revert CTR_ReturnRequestAfterTrade();
            }
            if (returnCompleted[message.loanId]) {
                revert CTR_ReturnAlreadyCompleted();
            }
            if (returnRequested[message.loanId]) {
                revert CTR_ReturnAlreadyRequested();
            }
            _signWithdrawal(message, guid);
            returnRequested[message.loanId] = true;
        }

        handledMessages[guid] = true;
        emit MessageHandled(guid, message.action, message.loanId);
    }

    function sendSettlementReport(
        uint256 loanId,
        address asset,
        uint256 settlementAmount,
        uint256 collateralSold,
        bytes32 socketMessageId
    ) external payable onlyRole(KEEPER_ROLE) returns (MessagingReceipt memory) {
        if (vaultRecipient == address(0)) {
            revert CTR_InvalidRecipient();
        }
        CollarLZMessages.Message memory message = CollarLZMessages.Message({
            action: CollarLZMessages.Action.SettlementReport,
            loanId: loanId,
            asset: asset,
            amount: settlementAmount,
            recipient: vaultRecipient,
            subaccountId: tsa.subAccount(),
            socketMessageId: socketMessageId,
            secondaryAmount: collateralSold,
            quoteHash: bytes32(0),
            takerNonce: 0,
            data: bytes("")
        });

        return _send(message, defaultOptions);
    }

    function sendCollateralReturned(uint256 loanId, address asset, uint256 amount, bytes32 socketMessageId)
        external
        payable
        onlyRole(KEEPER_ROLE)
        returns (MessagingReceipt memory)
    {
        if (vaultRecipient == address(0)) {
            revert CTR_InvalidRecipient();
        }
        if (tradeConfirmed[loanId]) {
            revert CTR_CollateralReturnedAfterTrade();
        }
        if (!returnRequested[loanId]) {
            revert CTR_ReturnNotRequested();
        }
        if (returnCompleted[loanId]) {
            revert CTR_ReturnAlreadyCompleted();
        }
        CollarLZMessages.Message memory message = CollarLZMessages.Message({
            action: CollarLZMessages.Action.CollateralReturned,
            loanId: loanId,
            asset: asset,
            amount: amount,
            recipient: vaultRecipient,
            subaccountId: tsa.subAccount(),
            socketMessageId: socketMessageId,
            secondaryAmount: 0,
            quoteHash: bytes32(0),
            takerNonce: 0,
            data: bytes("")
        });
        returnCompleted[loanId] = true;
        returnRequested[loanId] = false;
        return _send(message, defaultOptions);
    }

    struct TradeConfirmedParams {
        uint256 loanId;
        address asset;
        uint256 amount;
        bytes32 socketMessageId;
        bytes32 quoteHash;
        uint256 takerNonce;
        uint256 callStrike;
        uint256 putStrike;
        uint64 expiry;
    }

    function sendTradeConfirmed(TradeConfirmedParams calldata p)
        external
        payable
        onlyRole(KEEPER_ROLE)
        returns (MessagingReceipt memory)
    {
        if (vaultRecipient == address(0)) {
            revert CTR_InvalidRecipient();
        }
        if (returnCompleted[p.loanId]) {
            revert CTR_TradeConfirmedAfterReturn();
        }
        if (tradeConfirmed[p.loanId]) {
            revert CTR_TradeAlreadyConfirmed();
        }
        _validateTradeConfirmedPreconditions(p.amount, p.socketMessageId, p.takerNonce);

        CollarLZMessages.Message memory message = CollarLZMessages.Message({
            action: CollarLZMessages.Action.TradeConfirmed,
            loanId: p.loanId,
            asset: p.asset,
            amount: p.amount,
            recipient: vaultRecipient,
            subaccountId: tsa.subAccount(),
            socketMessageId: p.socketMessageId,
            secondaryAmount: 0,
            quoteHash: p.quoteHash,
            takerNonce: p.takerNonce,
            data: abi.encode(p.callStrike, p.putStrike, p.expiry)
        });

        MessagingReceipt memory receipt = _send(message, defaultOptions);

        loanStore.markConsumed(p.loanId);

        tradeConfirmed[p.loanId] = true;
        return receipt;
    }

    function _validateTradeConfirmedPreconditions(uint256 amount, bytes32 socketMessageId, uint256 takerNonce)
        internal
        view
    {
        (,,,, address rfqModule,) = tsa.getCollarTSAAddresses();
        if (rfqModule == address(0)) {
            revert CTR_RfqModuleNotSet();
        }
        if (!IRfqNonceTracker(rfqModule).usedNonces(address(tsa), takerNonce)) {
            revert CTR_RfqTradeNotConfirmed();
        }
        if (amount > 0) {
            if (socketMessageId == bytes32(0)) {
                revert CTR_SocketNotFinalized();
            }
            if (address(socket) != address(0) && !socket.messageExecuted(socketMessageId)) {
                revert CTR_SocketNotFinalized();
            }
        }
    }

    function quoteMessage(CollarLZMessages.Message calldata message, bytes calldata options)
        external
        view
        returns (MessagingFee memory fee)
    {
        return _quote(remoteEid, abi.encode(message), options, false);
    }

    function _signDeposit(CollarLZMessages.Message memory message) internal {
        ICollarTSA.CollarTSAParams memory params = tsa.getCollarTSAParams();
        (, address depositModule,,,,) = tsa.getCollarTSAAddresses();
        (,, address wrappedDepositAsset,,,,) = tsa.getBaseTSAAddresses();

        IERC20(message.asset).safeTransfer(address(tsa), message.amount);

        IDepositModule.DepositData memory depositData = IDepositModule.DepositData({
            amount: message.amount, asset: wrappedDepositAsset, managerForNewAccount: address(0)
        });

        IActionVerifier.Action memory action = IActionVerifier.Action({
            subaccountId: message.subaccountId,
            nonce: uint256(message.socketMessageId),
            module: IMatchingModule(depositModule),
            data: abi.encode(depositData),
            expiry: block.timestamp + params.minSignatureExpiry,
            owner: address(tsa),
            signer: address(tsa)
        });

        tsa.signActionData(action, bytes(""));
    }

    function _signWithdrawal(CollarLZMessages.Message memory message, bytes32 guid) internal {
        ICollarTSA.CollarTSAParams memory params = tsa.getCollarTSAParams();
        (,, address withdrawalModule,,,) = tsa.getCollarTSAAddresses();
        (,, address wrappedDepositAsset,,,,) = tsa.getBaseTSAAddresses();

        IWithdrawalModule.WithdrawalData memory withdrawalData =
            IWithdrawalModule.WithdrawalData({asset: wrappedDepositAsset, assetAmount: message.amount});

        uint256 nonce = message.socketMessageId != bytes32(0) ? uint256(message.socketMessageId) : uint256(guid);

        IActionVerifier.Action memory action = IActionVerifier.Action({
            subaccountId: message.subaccountId,
            nonce: nonce,
            module: IMatchingModule(withdrawalModule),
            data: abi.encode(withdrawalData),
            expiry: block.timestamp + params.minSignatureExpiry,
            owner: address(tsa),
            signer: address(tsa)
        });

        tsa.signActionData(action, bytes(""));
    }

    function _sendAck(CollarLZMessages.Message memory origin, CollarLZMessages.Action action) internal {
        uint256 subaccountId =
            action == CollarLZMessages.Action.DepositConfirmed ? origin.subaccountId : tsa.subAccount();
        CollarLZMessages.Message memory message = CollarLZMessages.Message({
            action: action,
            loanId: origin.loanId,
            asset: origin.asset,
            amount: origin.amount,
            recipient: origin.recipient,
            subaccountId: subaccountId,
            socketMessageId: origin.socketMessageId,
            secondaryAmount: 0,
            quoteHash: bytes32(0),
            takerNonce: 0,
            data: bytes("")
        });

        _send(message, defaultOptions);
    }

    function _send(CollarLZMessages.Message memory message, bytes memory options)
        internal
        returns (MessagingReceipt memory receipt)
    {
        bytes memory payload = abi.encode(message);
        receipt = _lzSend(remoteEid, payload, options, MessagingFee(msg.value, 0), msg.sender);
        emit MessageSent(receipt.guid, message.action, message.loanId);
    }
}
