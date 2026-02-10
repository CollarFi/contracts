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
}
