// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {
    MessagingFee,
    MessagingReceipt,
    Origin
} from "@layerzerolabs/lz-evm-protocol-v2/contracts/interfaces/ILayerZeroEndpointV2.sol";
import {OApp} from "@layerzerolabs/lz-evm-oapp-v2/contracts/oapp/OApp.sol";

import {CollarLZMessages} from "./CollarLZMessages.sol";

/// @notice L1 messenger for LayerZero metadata messages.
contract CollarVaultMessenger is AccessControl, OApp {
    bytes32 public constant VAULT_ROLE = keccak256("VAULT_ROLE");
    bytes32 public constant PARAMETER_ROLE = keccak256("PARAMETER_ROLE");

    uint32 public remoteEid;
    bytes public defaultOptions;

    mapping(bytes32 => CollarLZMessages.Message) private _receivedMessages;

    function receivedMessage(bytes32 guid) external view returns (CollarLZMessages.Message memory message) {
        return _receivedMessages[guid];
    }

    event MessageSent(bytes32 indexed guid, CollarLZMessages.Action action, uint256 indexed loanId);
    event MessageReceived(bytes32 indexed guid, CollarLZMessages.Action action, uint256 indexed loanId);
    event RemoteEidUpdated(uint32 remoteEid);
    event OptionsUpdated(bytes options);

    error CVM_InvalidPeer();
    error CVM_InsufficientNativeFee();
    error CV_LZMessageMismatch();

    constructor(address admin, address vault, address endpoint_, uint32 remoteEid_)
        OApp(endpoint_, admin)
        Ownable(admin)
    {
        if (admin == address(0) || vault == address(0)) {
            revert CVM_InvalidPeer();
        }
        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        _grantRole(PARAMETER_ROLE, admin);
        _grantRole(VAULT_ROLE, vault);

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

    function sendMessage(CollarLZMessages.Message calldata message)
        external
        payable
        onlyRole(VAULT_ROLE)
        returns (MessagingReceipt memory receipt)
    {
        return _send(message, defaultOptions);
    }

    function sendMessageWithOptions(CollarLZMessages.Message calldata message, bytes calldata options)
        external
        payable
        onlyRole(VAULT_ROLE)
        returns (MessagingReceipt memory receipt)
    {
        return _send(message, options);
    }

    function quoteMessage(CollarLZMessages.Message calldata message, bytes calldata options)
        external
        view
        returns (MessagingFee memory fee)
    {
        return _quote(remoteEid, abi.encode(message), options, false);
    }

    function sendMessageAutoFee(CollarLZMessages.Message calldata message, address refundTo)
        external
        payable
        onlyRole(VAULT_ROLE)
        returns (bytes32 guid)
    {
        return _sendAutoFee(message, refundTo);
    }

    function sendDepositIntentAutoFee(
        uint256 loanId,
        address asset,
        uint256 amount,
        address recipient,
        uint256 subaccountId,
        bytes32 socketMessageId,
        address refundTo
    ) external payable onlyRole(VAULT_ROLE) returns (bytes32 guid) {
        CollarLZMessages.Message memory message = CollarLZMessages.Message({
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
        return _sendAutoFee(message, refundTo);
    }

    function sendMandateCreatedAutoFee(
        uint256 loanId,
        address asset,
        uint256 borrowAmount,
        address recipient,
        uint256 subaccountId,
        bytes calldata mandateData,
        address refundTo
    ) external payable onlyRole(VAULT_ROLE) returns (bytes32 guid) {
        CollarLZMessages.Message memory message = CollarLZMessages.Message({
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
        return _sendAutoFee(message, refundTo);
    }

    function sendReturnRequestAutoFee(
        uint256 loanId,
        address asset,
        uint256 amount,
        address recipient,
        uint256 subaccountId,
        address refundTo
    ) external payable onlyRole(VAULT_ROLE) returns (bytes32 guid) {
        CollarLZMessages.Message memory message = CollarLZMessages.Message({
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
        return _sendAutoFee(message, refundTo);
    }

    function sendRolloverIntentAutoFee(
        uint256 loanId,
        address asset,
        uint256 principal,
        address recipient,
        uint256 subaccountId,
        bytes calldata rolloverData,
        address refundTo
    ) external payable onlyRole(VAULT_ROLE) returns (bytes32 guid) {
        CollarLZMessages.Message memory message = CollarLZMessages.Message({
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
        return _sendAutoFee(message, refundTo);
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
        if (lzMessage.action != CollarLZMessages.Action.DepositConfirmed) {
            revert CV_LZMessageMismatch();
        }
        loanId = lzMessage.loanId;
        if (lzMessage.recipient != expectedRecipient) {
            revert CV_LZMessageMismatch();
        }
        if (expectedSubaccountId != 0 && lzMessage.subaccountId != expectedSubaccountId) {
            revert CV_LZMessageMismatch();
        }
        if (lzMessage.asset != pendingCollateralAsset || lzMessage.amount != pendingCollateralAmount) {
            revert CV_LZMessageMismatch();
        }
        if (pendingBorrower == address(0)) {
            revert CV_LZMessageMismatch();
        }
        if (pendingBorrower != expectedBorrower) {
            revert CV_LZMessageMismatch();
        }
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
        if (tradeMessage.action != CollarLZMessages.Action.TradeConfirmed || tradeMessage.loanId != expectedLoanId) {
            revert CV_LZMessageMismatch();
        }
        if (tradeMessage.recipient != expectedRecipient) {
            revert CV_LZMessageMismatch();
        }
        if (expectedSubaccountId != 0 && tradeMessage.subaccountId != expectedSubaccountId) {
            revert CV_LZMessageMismatch();
        }

        uint64 expiry;
        (callStrike, putStrike, expiry, realizedC) = abi.decode(tradeMessage.data, (uint256, uint256, uint64, int256));
        if (expiry != expectedMaturity) {
            revert CV_LZMessageMismatch();
        }
        if (callStrike < minCallStrike || putStrike > maxPutStrike) {
            revert CV_LZMessageMismatch();
        }
    }

    function validateTradeConfirmedMarker(
        CollarLZMessages.Message calldata lzMessage,
        address expectedRecipient,
        uint256 expectedSubaccountId
    ) external pure returns (uint256 loanId) {
        if (lzMessage.action != CollarLZMessages.Action.TradeConfirmed) {
            revert CV_LZMessageMismatch();
        }
        if (lzMessage.recipient != expectedRecipient) {
            revert CV_LZMessageMismatch();
        }
        if (expectedSubaccountId != 0 && lzMessage.subaccountId != expectedSubaccountId) {
            revert CV_LZMessageMismatch();
        }
        loanId = lzMessage.loanId;
    }

    function validateCollateralReturned(
        CollarLZMessages.Message calldata lzMessage,
        uint256 loanId,
        address collateralAsset,
        uint256 collateralAmount,
        address expectedRecipient,
        uint256 expectedSubaccountId
    ) external pure {
        if (
            lzMessage.action != CollarLZMessages.Action.CollateralReturned || lzMessage.loanId != loanId
                || lzMessage.asset != collateralAsset || lzMessage.amount != collateralAmount
        ) {
            revert CV_LZMessageMismatch();
        }
        if (lzMessage.recipient != expectedRecipient) {
            revert CV_LZMessageMismatch();
        }
        if (expectedSubaccountId != 0 && lzMessage.subaccountId != expectedSubaccountId) {
            revert CV_LZMessageMismatch();
        }
    }

    function validateSettlementReport(
        CollarLZMessages.Message calldata lzMessage,
        uint256 loanId,
        address usdcAsset,
        address expectedRecipient
    ) external pure returns (uint256 settlementAmount) {
        if (
            lzMessage.action != CollarLZMessages.Action.SettlementReport || lzMessage.loanId != loanId
                || lzMessage.asset != usdcAsset
        ) {
            revert CV_LZMessageMismatch();
        }
        if (lzMessage.recipient != expectedRecipient) {
            revert CV_LZMessageMismatch();
        }
        settlementAmount = lzMessage.amount;
    }

    function validateRolloverConfirmed(
        CollarLZMessages.Message calldata lzMessage,
        uint256 loanId,
        address expectedRecipient,
        uint256 expectedSubaccountId,
        bytes32 expectedMandateHash,
        address expectedBorrower,
        uint64 expectedMaturity,
        uint256 minCallStrike,
        uint256 maxPutStrike
    ) external pure returns (uint256 callStrike, uint256 putStrike, uint256 interestApr, int256 realizedC) {
        if (lzMessage.action != CollarLZMessages.Action.RolloverConfirmed || lzMessage.loanId != loanId) {
            revert CV_LZMessageMismatch();
        }
        if (lzMessage.recipient != expectedRecipient) {
            revert CV_LZMessageMismatch();
        }
        if (expectedSubaccountId != 0 && lzMessage.subaccountId != expectedSubaccountId) {
            revert CV_LZMessageMismatch();
        }

        bytes32 mandateHash;
        address borrower;
        uint64 expiry;
        (mandateHash, borrower, callStrike, putStrike, interestApr, expiry, realizedC) =
            abi.decode(lzMessage.data, (bytes32, address, uint256, uint256, uint256, uint64, int256));

        if (mandateHash != expectedMandateHash || borrower != expectedBorrower || expiry != expectedMaturity) {
            revert CV_LZMessageMismatch();
        }
        if (callStrike < minCallStrike || putStrike > maxPutStrike) {
            revert CV_LZMessageMismatch();
        }
    }

    function validateOriginationFee(CollarLZMessages.Message calldata lzMessage, uint256 feeAmount, address usdcAsset)
        external
        pure
    {
        if (feeAmount == 0) {
            if (lzMessage.amount != 0) {
                revert CV_LZMessageMismatch();
            }
            return;
        }
        if (lzMessage.asset != usdcAsset || lzMessage.amount != feeAmount || lzMessage.socketMessageId == bytes32(0)) {
            revert CV_LZMessageMismatch();
        }
    }

    function _lzReceive(Origin calldata, bytes32 guid, bytes calldata message, address, bytes calldata)
        internal
        override
    {
        CollarLZMessages.Message memory decoded = abi.decode(message, (CollarLZMessages.Message));
        _receivedMessages[guid] = decoded;
        emit MessageReceived(guid, decoded.action, decoded.loanId);
    }

    function _send(CollarLZMessages.Message calldata message, bytes memory options)
        internal
        returns (MessagingReceipt memory receipt)
    {
        bytes memory payload = abi.encode(message);
        receipt = _lzSend(remoteEid, payload, options, MessagingFee(msg.value, 0), msg.sender);
        emit MessageSent(receipt.guid, message.action, message.loanId);
    }

    function _sendAutoFee(CollarLZMessages.Message memory message, address refundTo) internal returns (bytes32 guid) {
        bytes memory payload = abi.encode(message);
        MessagingFee memory fee = _quote(remoteEid, payload, defaultOptions, false);
        if (msg.value < fee.nativeFee) revert CVM_InsufficientNativeFee();

        MessagingReceipt memory receipt =
            _lzSend(remoteEid, payload, defaultOptions, MessagingFee(fee.nativeFee, 0), refundTo);
        emit MessageSent(receipt.guid, message.action, message.loanId);
        return receipt.guid;
    }
}
