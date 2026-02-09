// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IBridgeAdapter} from "../interfaces/IBridgeAdapter.sol";
import {ISocketBridge} from "../interfaces/ISocketBridge.sol";
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
    ISocketBridge public immutable bridge;
    ISocketVault public immutable socketVault;

    constructor(BridgeType bridgeType_, address bridge_, address socketVault_) {
        if (bridgeType_ == BridgeType.NONE) revert("SBA:invalid-type");
        bridgeType = bridgeType_;
        bridge = ISocketBridge(bridge_);
        socketVault = ISocketVault(socketVault_);
    }

    function estimateFee(address connector, uint256 msgGasLimit, uint256 payloadSize)
        external
        view
        override
        returns (uint256)
    {
        if (bridgeType == BridgeType.NEW) {
            return ISocketBridgeWithFees(address(bridge)).getMinFees(connector, msgGasLimit, payloadSize);
        }
        return socketVault.getMinFees(connector, msgGasLimit);
    }

    function bridge(
        address receiver,
        uint256 amount,
        uint256 msgGasLimit,
        address connector,
        bytes calldata extraData,
        bytes calldata options
    ) external payable override {
        if (bridgeType == BridgeType.NEW) {
            bridge.bridge{value: msg.value}(receiver, amount, msgGasLimit, connector, extraData, options);
        } else {
            socketVault.depositToAppChain{value: msg.value}(receiver, amount, msgGasLimit, connector);
        }
    }
}
