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

/// @notice Minimal LayerZero harness for smoke-testing cross-chain messaging before protocol wiring.
contract LZPingPong is AccessControl, OApp {
    bytes32 public constant PARAMETER_ROLE = keccak256("PARAMETER_ROLE");
    bytes32 public constant SENDER_ROLE = keccak256("SENDER_ROLE");

    enum MessageKind {
        Unknown,
        Ping,
        Ack
    }

    struct Message {
        MessageKind kind;
        uint64 nonce;
        address sender;
        uint64 sentAt;
        bytes32 tag;
        bytes32 requestGuid;
    }

    uint32 public remoteEid;
    bytes public defaultOptions;

    bytes32 public lastReceivedGuid;
    Message public lastReceived;

    mapping(bytes32 => bytes32) public messageHashByGuid;
    mapping(uint32 => uint64) public lastNonceBySourceEid;
    mapping(uint32 => bytes32) public lastSenderBySourceEid;

    function getLastReceived() external view returns (Message memory) {
        return lastReceived;
    }

    event PingSent(bytes32 indexed guid, uint32 indexed dstEid, uint64 indexed nonce, bytes32 tag);
    event AckSent(bytes32 indexed guid, uint32 indexed dstEid, uint64 indexed nonce, bytes32 requestGuid, bytes32 tag);
    event MessageReceived(
        bytes32 indexed guid,
        uint32 indexed srcEid,
        bytes32 indexed srcSender,
        MessageKind kind,
        uint64 nonce,
        bytes32 requestGuid,
        bytes32 payloadHash
    );
    event RemoteEidUpdated(uint32 remoteEid);
    event DefaultOptionsUpdated(bytes options);

    error LZPP_InvalidAdmin();
    error LZPP_NoReceivedMessage();
    error LZPP_LastMessageNotPing();

    constructor(address admin, address endpoint_, uint32 remoteEid_) OApp(endpoint_, admin) Ownable(admin) {
        if (admin == address(0)) revert LZPP_InvalidAdmin();

        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        _grantRole(PARAMETER_ROLE, admin);
        _grantRole(SENDER_ROLE, admin);

        remoteEid = remoteEid_;
    }

    function setRemoteEid(uint32 newRemoteEid) external onlyRole(PARAMETER_ROLE) {
        remoteEid = newRemoteEid;
        emit RemoteEidUpdated(newRemoteEid);
    }

    function setDefaultOptions(bytes calldata options) external onlyRole(PARAMETER_ROLE) {
        defaultOptions = options;
        emit DefaultOptionsUpdated(options);
    }

    function quoteMessage(Message calldata message, bytes calldata options)
        external
        view
        returns (MessagingFee memory fee)
    {
        return _quote(remoteEid, abi.encode(message), options, false);
    }

    function sendPing(uint64 nonce, bytes32 tag)
        external
        payable
        onlyRole(SENDER_ROLE)
        returns (MessagingReceipt memory receipt)
    {
        Message memory message = Message({
            kind: MessageKind.Ping,
            nonce: nonce,
            sender: msg.sender,
            sentAt: uint64(block.timestamp),
            tag: tag,
            requestGuid: bytes32(0)
        });

        receipt = _send(message, defaultOptions);
        emit PingSent(receipt.guid, remoteEid, nonce, tag);
    }

    function sendAck(uint64 nonce, bytes32 tag, bytes32 requestGuid)
        external
        payable
        onlyRole(SENDER_ROLE)
        returns (MessagingReceipt memory receipt)
    {
        receipt = _sendAck(nonce, tag, requestGuid);
    }

    /// @notice Convenience helper to acknowledge the latest received ping.
    function ackLastReceived() external payable onlyRole(SENDER_ROLE) returns (MessagingReceipt memory receipt) {
        if (lastReceivedGuid == bytes32(0)) revert LZPP_NoReceivedMessage();
        if (lastReceived.kind != MessageKind.Ping) revert LZPP_LastMessageNotPing();

        receipt = _sendAck(lastReceived.nonce, lastReceived.tag, lastReceivedGuid);
    }

    function _sendAck(uint64 nonce, bytes32 tag, bytes32 requestGuid)
        internal
        returns (MessagingReceipt memory receipt)
    {
        Message memory message = Message({
            kind: MessageKind.Ack,
            nonce: nonce,
            sender: msg.sender,
            sentAt: uint64(block.timestamp),
            tag: tag,
            requestGuid: requestGuid
        });

        receipt = _send(message, defaultOptions);
        emit AckSent(receipt.guid, remoteEid, nonce, requestGuid, tag);
    }

    function _lzReceive(Origin calldata origin, bytes32 guid, bytes calldata payload, address, bytes calldata)
        internal
        override
    {
        Message memory message = abi.decode(payload, (Message));
        bytes32 payloadHash = keccak256(payload);

        lastReceivedGuid = guid;
        lastReceived = message;
        messageHashByGuid[guid] = payloadHash;
        lastNonceBySourceEid[origin.srcEid] = message.nonce;
        lastSenderBySourceEid[origin.srcEid] = origin.sender;

        emit MessageReceived(
            guid, origin.srcEid, origin.sender, message.kind, message.nonce, message.requestGuid, payloadHash
        );
    }

    function _send(Message memory message, bytes memory options) internal returns (MessagingReceipt memory receipt) {
        receipt = _lzSend(remoteEid, abi.encode(message), options, MessagingFee(msg.value, 0), msg.sender);
    }
}
