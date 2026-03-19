// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Initializable} from "openzeppelin-upgradeable/proxy/utils/Initializable.sol";
import {AccessControlUpgradeable} from "openzeppelin-upgradeable/access/AccessControlUpgradeable.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {
    MessagingFee,
    MessagingReceipt,
    Origin
} from "@layerzerolabs/lz-evm-protocol-v2/contracts/interfaces/ILayerZeroEndpointV2.sol";
import {OAppUpgradeable} from "@layerzerolabs/oapp-evm-upgradeable/contracts/oapp/OAppUpgradeable.sol";

import {IERC20BasedAsset} from "v2-core/src/interfaces/IERC20BasedAsset.sol";

import {ICollarTSA} from "../interfaces/ICollarTSA.sol";
import {ICollarLoanStore} from "../interfaces/ICollarLoanStore.sol";
import {ISocketMessageTracker} from "../interfaces/ISocketMessageTracker.sol";
import {CollarLZMessages} from "./CollarLZMessages.sol";

interface IRfqNonceTracker {
    function usedNonces(address owner, uint256 nonce) external view returns (bool);
}

/// @notice L2 receiver for LayerZero metadata messages.
contract CollarTSAReceiver is Initializable, AccessControlUpgradeable, OAppUpgradeable {
    using SafeERC20 for IERC20;

    uint256 internal constant LOAN_ID_NONCE_MODULUS = 1_000_000;

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
    mapping(uint256 => bytes32) public depositIntentGuidByLoanId;
    mapping(uint256 => bytes32) public returnRequestGuidByLoanId;
    mapping(uint256 => bool) public depositConfirmed;
    mapping(uint256 => bool) public returnRequested;
    mapping(uint256 => bool) public returnCompleted;
    mapping(uint256 => bool) public collateralReturnedSent;
    mapping(uint256 => bool) public settlementReported;
    mapping(uint256 => bool) public tradeConfirmed;
    mapping(uint256 => uint256) public tradeExecutionNonceByLoanId;

    // Loan terms/collateral accounting is persisted in `loanStore`.

    event MessageReceived(bytes32 indexed guid, CollarLZMessages.Action action, uint256 indexed loanId);
    event MessageHandled(bytes32 indexed guid, CollarLZMessages.Action action, uint256 indexed loanId);
    event MessageSent(bytes32 indexed guid, CollarLZMessages.Action action, uint256 indexed loanId);
    event RemoteEidUpdated(uint32 remoteEid);
    event OptionsUpdated(bytes options);
    event SocketUpdated(address indexed socket);
    event TSAUpdated(address indexed tsa);
    event VaultRecipientUpdated(address indexed recipient);
    event TradeExecutionRecorded(uint256 indexed loanId, uint256 takerNonce);

    error CTR_InvalidPeer();
    error CTR_InvalidRecipient();
    error CTR_MessageNotFound();
    error CTR_MessageAlreadyHandled();
    error CTR_InsufficientValue();
    error CTR_SocketNotFinalized();
    error CTR_RfqModuleNotSet();
    error CTR_RfqTradeNotConfirmed();
    error CTR_TradeNotConfirmed();
    error CTR_InvalidSubaccount();
    error CTR_InvalidAsset();
    error CTR_CollateralAlreadySent();
    error CTR_ReturnAlreadyRequested();
    error CTR_ReturnAlreadyCompleted();
    error CTR_ReturnNotRequested();
    error CTR_DepositAlreadyConfirmed();
    error CTR_DepositNotExecuted();
    error CTR_ReturnRequestAfterTrade();
    error CTR_TradeNotExecuted();
    error CTR_WithdrawalNotExecuted();
    error CTR_CollateralReturnedAfterTrade();
    error CTR_SettlementAlreadyReported();
    error CTR_TradeConfirmedAfterReturn();
    error CTR_TradeAlreadyConfirmed();
    error CTR_ReturnRequestBlocksTrade();
    error CTR_LoanIdTooLargeForNonce();
    error CTR_InvalidTradeExecutionNonce();

    constructor(address endpoint_) OAppUpgradeable(endpoint_) {
        _disableInitializers();
    }

    function initialize(
        address admin,
        address endpoint_,
        ISocketMessageTracker socket_,
        ICollarTSA tsa_,
        ICollarLoanStore loanStore_,
        uint32 remoteEid_
    ) external initializer {
        if (
            admin == address(0) || endpoint_ != address(endpoint) || address(tsa_) == address(0)
                || address(loanStore_) == address(0)
        ) {
            revert CTR_InvalidPeer();
        }

        __AccessControl_init();
        __Ownable_init(admin);
        __OApp_init(admin);

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
            (
                address borrower,
                uint256 minCallStrike,
                uint256 maxPutStrike,
                uint256 minNetInterest,
                uint256 fixedInterest,
                uint256 maxRollLtv,
                uint256 strikeScale,
                uint64 maturity,
                uint64 deadline
            ) = abi.decode(
                message.data, (address, uint256, uint256, uint256, uint256, uint256, uint256, uint64, uint64)
            );

            loanStore.recordMandate(
                message.loanId,
                borrower,
                message.asset,
                message.amount,
                minCallStrike,
                maxPutStrike,
                minNetInterest,
                fixedInterest,
                maxRollLtv,
                strikeScale,
                maturity,
                deadline
            );

            handledMessages[guid] = true;
            emit MessageHandled(guid, message.action, message.loanId);
            return;
        }

        if (message.action == CollarLZMessages.Action.RolloverIntent) {
            (
                bytes32 mandateHash,
                address borrower,
                uint64 newMaturity,
                uint256 minCallStrike,
                uint256 maxPutStrike,
                uint256 minNetInterest,
                uint256 fixedInterest,
                uint256 maxRollLtv,
                uint256 strikeScale,
                uint64 deadline,
                uint256 nonce
            ) = abi.decode(
                message.data,
                (bytes32, address, uint64, uint256, uint256, uint256, uint256, uint256, uint256, uint64, uint256)
            );
            nonce;

            loanStore.recordRolloverMandate(
                message.loanId,
                borrower,
                mandateHash,
                minCallStrike,
                maxPutStrike,
                minNetInterest,
                fixedInterest,
                maxRollLtv,
                strikeScale,
                newMaturity,
                deadline
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
            (,, address wrappedDepositAsset,,,,) = tsa.getBaseTSAAddresses();
            address underlyingDepositAsset = address(IERC20BasedAsset(wrappedDepositAsset).wrappedAsset());
            if (message.asset != underlyingDepositAsset) {
                revert CTR_InvalidAsset();
            }

            IERC20(underlyingDepositAsset).safeTransfer(address(tsa), message.amount);
            loanStore.recordCollateral(message.loanId, message.asset, message.amount);
            depositIntentGuidByLoanId[message.loanId] = guid;
        } else if (message.action == CollarLZMessages.Action.ReturnRequest) {
            if (message.subaccountId != tsa.subAccount()) {
                revert CTR_InvalidSubaccount();
            }
            ICollarLoanStore.Loan memory loan = loanStore.getLoan(message.loanId);
            if (tradeConfirmed[message.loanId] || loan.tradeExecuted) {
                revert CTR_ReturnRequestAfterTrade();
            }
            if (returnCompleted[message.loanId]) {
                revert CTR_ReturnAlreadyCompleted();
            }
            if (returnRequested[message.loanId]) {
                revert CTR_ReturnAlreadyRequested();
            }
            returnRequestGuidByLoanId[message.loanId] = guid;
            returnRequested[message.loanId] = true;
            loanStore.setReturnRequested(message.loanId, true);
        }

        handledMessages[guid] = true;
        emit MessageHandled(guid, message.action, message.loanId);
    }

    function sendDepositConfirmedAfterExecution(uint256 loanId)
        external
        payable
        onlyRole(KEEPER_ROLE)
        returns (MessagingReceipt memory)
    {
        if (depositConfirmed[loanId]) {
            revert CTR_DepositAlreadyConfirmed();
        }
        if (!tsa.depositExecuted(loanId)) {
            revert CTR_DepositNotExecuted();
        }

        bytes32 guid = depositIntentGuidByLoanId[loanId];
        if (guid == bytes32(0)) {
            revert CTR_MessageNotFound();
        }

        depositConfirmed[loanId] = true;
        return _sendAck(pendingMessages[guid], CollarLZMessages.Action.DepositConfirmed, msg.value, msg.sender);
    }

    function recordTradeExecuted(uint256 loanId, uint256 takerNonce) external onlyRole(KEEPER_ROLE) {
        if (returnCompleted[loanId]) {
            revert CTR_TradeConfirmedAfterReturn();
        }
        if (tradeConfirmed[loanId]) {
            revert CTR_TradeAlreadyConfirmed();
        }

        ICollarLoanStore.Loan memory loan = loanStore.getLoan(loanId);
        if (!_loanExists(loan) || loan.consumed) {
            revert CTR_MessageNotFound();
        }
        if (loan.returnRequested) {
            revert CTR_ReturnRequestBlocksTrade();
        }

        _validateTradeExecutionNonce(loanId, takerNonce);

        uint256 recordedNonce = tradeExecutionNonceByLoanId[loanId];
        if (recordedNonce != 0) {
            if (recordedNonce != takerNonce) {
                revert CTR_InvalidTradeExecutionNonce();
            }
            return;
        }

        tradeExecutionNonceByLoanId[loanId] = takerNonce;
        loanStore.setTradeExecuted(loanId, true);
        emit TradeExecutionRecorded(loanId, takerNonce);
    }

    function sendSettlementReport(
        uint256 loanId,
        address asset,
        uint256 settlementAmount,
        uint256 collateralSold,
        bytes32 socketMessageId
    ) external payable onlyRole(KEEPER_ROLE) returns (MessagingReceipt memory) {
        return _sendSettlementReportMessage(
            loanId, asset, settlementAmount, collateralSold, socketMessageId, msg.value, msg.sender
        );
    }

    function bridgePendingReturnAndNotify(uint256 loanId, address asset, uint256 amount)
        external
        payable
        onlyRole(KEEPER_ROLE)
        returns (bytes32 socketMessageId, bytes32 lzGuid)
    {
        _requireWithdrawalExecuted(loanId);
        uint256 bridgeFee;
        (socketMessageId, bridgeFee) = _bridgeToVault(_collateralBridgeAsset(), amount);
        MessagingReceipt memory receipt = _sendCollateralReturnedMessage(
            loanId, asset, amount, socketMessageId, true, msg.value - bridgeFee, msg.sender
        );
        return (socketMessageId, receipt.guid);
    }

    function bridgeNeutralCollateralAndNotify(uint256 loanId, address asset, uint256 amount)
        external
        payable
        onlyRole(KEEPER_ROLE)
        returns (bytes32 socketMessageId, bytes32 lzGuid)
    {
        _requireWithdrawalExecuted(loanId);
        uint256 bridgeFee;
        (socketMessageId, bridgeFee) = _bridgeToVault(_collateralBridgeAsset(), amount);
        MessagingReceipt memory receipt = _sendCollateralReturnedMessage(
            loanId, asset, amount, socketMessageId, false, msg.value - bridgeFee, msg.sender
        );
        return (socketMessageId, receipt.guid);
    }

    function bridgeSettlementAndNotify(uint256 loanId, address asset, uint256 settlementAmount, uint256 collateralSold)
        external
        payable
        onlyRole(KEEPER_ROLE)
        returns (bytes32 socketMessageId, bytes32 lzGuid)
    {
        uint256 bridgeFee;
        (socketMessageId, bridgeFee) = _bridgeToVault(_cashBridgeAsset(), settlementAmount);
        MessagingReceipt memory receipt = _sendSettlementReportMessage(
            loanId, asset, settlementAmount, collateralSold, socketMessageId, msg.value - bridgeFee, msg.sender
        );
        return (socketMessageId, receipt.guid);
    }

    function sendCollateralReturned(uint256 loanId, address asset, uint256 amount, bytes32 socketMessageId)
        external
        payable
        onlyRole(KEEPER_ROLE)
        returns (MessagingReceipt memory)
    {
        return _sendCollateralReturnedMessage(
            loanId, asset, amount, socketMessageId, !tradeConfirmed[loanId], msg.value, msg.sender
        );
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
        int256 realizedC;
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
        ICollarLoanStore.Loan memory loan = loanStore.getLoan(p.loanId);
        if (!loan.tradeExecuted) {
            revert CTR_TradeNotExecuted();
        }
        if (loan.returnRequested) {
            revert CTR_ReturnRequestBlocksTrade();
        }
        _validateTradeConfirmedPreconditions(p.loanId, p.amount, p.socketMessageId, p.takerNonce);

        bool isRollover = loan.rolloverPending;
        bytes memory payload = isRollover
            ? abi.encode(
                loan.rolloverMandateHash,
                loan.borrower,
                p.callStrike,
                p.putStrike,
                loan.rolloverMinNetInterest,
                p.expiry,
                p.realizedC
            )
            : abi.encode(p.callStrike, p.putStrike, p.expiry, p.realizedC);

        CollarLZMessages.Message memory message = CollarLZMessages.Message({
            action: isRollover ? CollarLZMessages.Action.RolloverConfirmed : CollarLZMessages.Action.TradeConfirmed,
            loanId: p.loanId,
            asset: p.asset,
            amount: p.amount,
            recipient: vaultRecipient,
            subaccountId: tsa.subAccount(),
            socketMessageId: p.socketMessageId,
            secondaryAmount: 0,
            quoteHash: p.quoteHash,
            takerNonce: p.takerNonce,
            data: payload
        });

        MessagingReceipt memory receipt = _send(message, defaultOptions, msg.value, msg.sender);
        loanStore.setTradeExecuted(p.loanId, false);

        if (isRollover) {
            loanStore.clearRollover(p.loanId);
        } else {
            loanStore.markConsumed(p.loanId);
        }

        tradeConfirmed[p.loanId] = true;
        return receipt;
    }

    function _validateTradeConfirmedPreconditions(
        uint256 loanId,
        uint256 amount,
        bytes32 socketMessageId,
        uint256 takerNonce
    )
        internal
        view
    {
        uint256 recordedNonce = tradeExecutionNonceByLoanId[loanId];
        if (recordedNonce == 0 || recordedNonce != takerNonce) {
            revert CTR_InvalidTradeExecutionNonce();
        }
        _validateTradeExecutionNonce(loanId, takerNonce);
        if (amount > 0) {
            if (socketMessageId == bytes32(0)) {
                revert CTR_SocketNotFinalized();
            }
            if (address(socket) != address(0) && !socket.messageExecuted(socketMessageId)) {
                revert CTR_SocketNotFinalized();
            }
        }
    }

    function _validateTradeExecutionNonce(uint256 loanId, uint256 takerNonce) internal view {
        if (loanId >= LOAN_ID_NONCE_MODULUS) {
            revert CTR_LoanIdTooLargeForNonce();
        }
        if (takerNonce % LOAN_ID_NONCE_MODULUS != loanId) {
            revert CTR_InvalidTradeExecutionNonce();
        }

        (,,,, address rfqModule,) = tsa.getCollarTSAAddresses();
        if (rfqModule == address(0)) {
            revert CTR_RfqModuleNotSet();
        }
        if (!IRfqNonceTracker(rfqModule).usedNonces(address(tsa), takerNonce)) {
            revert CTR_RfqTradeNotConfirmed();
        }
    }

    function _loanExists(ICollarLoanStore.Loan memory loan) internal pure returns (bool) {
        return loan.borrower != address(0) || loan.borrowAmount != 0 || loan.collateralAsset != address(0)
            || loan.collateralAmount != 0 || loan.maturity != 0;
    }

    function quoteMessage(CollarLZMessages.Message calldata message, bytes calldata options)
        external
        view
        returns (MessagingFee memory fee)
    {
        return _quote(remoteEid, abi.encode(message), options, false);
    }

    function _requireWithdrawalExecuted(uint256 loanId) internal view {
        if (!tsa.withdrawExecuted(loanId)) {
            revert CTR_WithdrawalNotExecuted();
        }
    }

    function _bridgeToVault(address bridgeAsset, uint256 amount)
        internal
        returns (bytes32 socketMessageId, uint256 bridgeFee)
    {
        if (vaultRecipient == address(0)) {
            revert CTR_InvalidRecipient();
        }

        bridgeFee = tsa.estimateBridgeFees(bridgeAsset, vaultRecipient, amount);
        if (msg.value < bridgeFee) {
            revert CTR_InsufficientValue();
        }

        socketMessageId = tsa.bridgeToL1{value: bridgeFee}(bridgeAsset, amount, vaultRecipient);
    }

    function _collateralBridgeAsset() internal view returns (address) {
        (,, address wrappedDepositAsset,,,,) = tsa.getBaseTSAAddresses();
        return address(IERC20BasedAsset(wrappedDepositAsset).wrappedAsset());
    }

    function _cashBridgeAsset() internal view returns (address) {
        (,,, address cash,,,) = tsa.getBaseTSAAddresses();
        return address(IERC20BasedAsset(cash).wrappedAsset());
    }

    function _sendSettlementReportMessage(
        uint256 loanId,
        address asset,
        uint256 settlementAmount,
        uint256 collateralSold,
        bytes32 socketMessageId,
        uint256 nativeFee,
        address refundTo
    ) internal returns (MessagingReceipt memory) {
        if (vaultRecipient == address(0)) {
            revert CTR_InvalidRecipient();
        }
        if (!tradeConfirmed[loanId]) {
            revert CTR_TradeNotConfirmed();
        }
        if (settlementReported[loanId]) {
            revert CTR_SettlementAlreadyReported();
        }

        settlementReported[loanId] = true;
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

        return _send(message, defaultOptions, nativeFee, refundTo);
    }

    function _sendCollateralReturnedMessage(
        uint256 loanId,
        address asset,
        uint256 amount,
        bytes32 socketMessageId,
        bool isPendingReturn,
        uint256 nativeFee,
        address refundTo
    ) internal returns (MessagingReceipt memory) {
        if (vaultRecipient == address(0)) {
            revert CTR_InvalidRecipient();
        }
        if (collateralReturnedSent[loanId]) {
            revert CTR_CollateralAlreadySent();
        }

        if (isPendingReturn) {
            if (tradeConfirmed[loanId]) {
                revert CTR_CollateralReturnedAfterTrade();
            }
            if (!returnRequested[loanId]) {
                revert CTR_ReturnNotRequested();
            }
            if (returnCompleted[loanId]) {
                revert CTR_ReturnAlreadyCompleted();
            }
            returnCompleted[loanId] = true;
            returnRequested[loanId] = false;
        } else {
            if (!tradeConfirmed[loanId]) {
                revert CTR_TradeNotConfirmed();
            }
            if (returnRequested[loanId]) {
                revert CTR_CollateralReturnedAfterTrade();
            }
        }

        collateralReturnedSent[loanId] = true;
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

        return _send(message, defaultOptions, nativeFee, refundTo);
    }

    function _sendAck(
        CollarLZMessages.Message memory origin,
        CollarLZMessages.Action action,
        uint256 nativeFee,
        address refundTo
    ) internal returns (MessagingReceipt memory) {
        uint256 subaccountId = action == CollarLZMessages.Action.DepositConfirmed
            ? origin.subaccountId
            : tsa.subAccount();
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

        return _send(message, defaultOptions, nativeFee, refundTo);
    }

    function _send(CollarLZMessages.Message memory message, bytes memory options, uint256 nativeFee, address refundTo)
        internal
        returns (MessagingReceipt memory receipt)
    {
        bytes memory payload = abi.encode(message);
        receipt = _lzSend(remoteEid, payload, options, MessagingFee(nativeFee, 0), refundTo);
        emit MessageSent(receipt.guid, message.action, message.loanId);
    }

    function _payNative(uint256 nativeFee) internal override returns (uint256) {
        if (address(this).balance < nativeFee) {
            revert NotEnoughNative(address(this).balance);
        }
        return nativeFee;
    }
}
