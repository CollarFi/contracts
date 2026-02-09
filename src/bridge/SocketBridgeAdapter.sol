// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IBridgeAdapter} from "../interfaces/IBridgeAdapter.sol";
import {ISocketBridge} from "../interfaces/ISocketBridge.sol";
import {ISocketConnector} from "../interfaces/ISocketConnector.sol";
import {ISocketVault} from "../interfaces/ISocketVault.sol";

interface ISocketBridgeWithFees is ISocketBridge {
    function getMinFees(address connector_, uint256 msgGasLimit_, uint256 payloadSize_)
        external
        view
        returns (uint256 totalFees);
}

contract SocketBridgeAdapter is IBridgeAdapter {
    enum BridgeType {
        NONE,
        NEW,
        OLD
    }

    BridgeType public immutable bridgeType;
    ISocketBridge public immutable socketBridge;
    ISocketVault public immutable socketVault;
    ISocketConnector public immutable connector;
    uint256 public immutable msgGasLimit;
    uint256 public immutable payloadSize;
    bytes public extraData;
    bytes public options;

    constructor(
        BridgeType bridgeType_,
        address bridge_,
        address socketVault_,
        address connector_,
        uint256 msgGasLimit_,
        uint256 payloadSize_,
        bytes memory extraData_,
        bytes memory options_
    ) {
        if (bridgeType_ == BridgeType.NONE) revert("SBA:invalid-type");
        if (connector_ == address(0)) revert("SBA:zero-connector");
        if (bridgeType_ == BridgeType.NEW && bridge_ == address(0)) revert("SBA:zero-bridge");
        if (bridgeType_ == BridgeType.OLD && socketVault_ == address(0)) revert("SBA:zero-vault");
        bridgeType = bridgeType_;
        socketBridge = ISocketBridge(bridge_);
        socketVault = ISocketVault(socketVault_);
        connector = ISocketConnector(connector_);
        msgGasLimit = msgGasLimit_;
        payloadSize = payloadSize_;
        extraData = extraData_;
        options = options_;
    }

    function messageId() external view override returns (bytes32) {
        return connector.getMessageId();
    }

    function estimateFee() external view override returns (uint256) {
        if (bridgeType == BridgeType.NEW) {
            return ISocketBridgeWithFees(address(socketBridge)).getMinFees(
                address(connector), msgGasLimit, payloadSize
            );
        }
        return socketVault.getMinFees(address(connector), msgGasLimit);
    }

    function bridge(address receiver, uint256 amount) external payable override {
        if (bridgeType == BridgeType.NEW) {
            socketBridge.bridge{value: msg.value}(
                receiver, amount, msgGasLimit, address(connector), extraData, options
            );
        } else {
            socketVault.depositToAppChain{value: msg.value}(receiver, amount, msgGasLimit, address(connector));
        }
    }
}
