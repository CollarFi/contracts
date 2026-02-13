// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IBridgeAdapter} from "../interfaces/IBridgeAdapter.sol";
import {ISocketBridge} from "../interfaces/ISocketBridge.sol";
import {ISocketBridgeWithFees, ISocketConnectorShared, ISocketCoreShared} from "../interfaces/ISocketAdapterShared.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

contract SocketBridgeAdapterNew is IBridgeAdapter {
    using SafeERC20 for IERC20;

    IERC20 public immutable asset;
    ISocketBridge public immutable socketBridge;
    ISocketConnectorShared public immutable connector;
    ISocketCoreShared public immutable socket;

    uint256 public immutable messageIdPrefix;
    uint256 public immutable msgGasLimit;
    uint256 public immutable payloadSize;
    bytes public extraData;
    bytes public options;

    constructor(
        address asset_,
        address bridge_,
        address connector_,
        uint256 msgGasLimit_,
        uint256 payloadSize_,
        bytes memory extraData_,
        bytes memory options_
    ) {
        if (asset_ == address(0)) revert("SBA_NEW:zero-asset");
        if (bridge_ == address(0)) revert("SBA_NEW:zero-bridge");
        if (connector_ == address(0)) revert("SBA_NEW:zero-connector");

        asset = IERC20(asset_);
        socketBridge = ISocketBridge(bridge_);
        connector = ISocketConnectorShared(connector_);

        address socket_ = ISocketConnectorShared(connector_).socket__();
        if (socket_ == address(0)) revert("SBA_NEW:zero-socket");
        socket = ISocketCoreShared(socket_);

        uint32 siblingChainSlug = ISocketConnectorShared(connector_).siblingChainSlug();
        if (siblingChainSlug == 0) revert("SBA_NEW:zero-sibling-slug");

        (address siblingPlug,,,,) = ISocketCoreShared(socket_).getPlugConfig(connector_, siblingChainSlug);
        if (siblingPlug == address(0)) revert("SBA_NEW:zero-sibling-plug");

        messageIdPrefix = (uint256(ISocketCoreShared(socket_).chainSlug()) << 224) | (uint256(uint160(siblingPlug)) << 64);

        msgGasLimit = msgGasLimit_;
        payloadSize = payloadSize_;
        extraData = extraData_;
        options = options_;
    }

    function messageId() external view override returns (bytes32) {
        return bytes32(messageIdPrefix | uint256(socket.globalMessageCount()));
    }

    function estimateFee() external view override returns (uint256) {
        return ISocketBridgeWithFees(address(socketBridge)).getMinFees(address(connector), msgGasLimit, payloadSize);
    }

    function bridge(address receiver, uint256 amount) external payable override {
        asset.safeTransferFrom(msg.sender, address(this), amount);
        asset.forceApprove(address(socketBridge), amount);
        socketBridge.bridge{value: msg.value}(receiver, amount, msgGasLimit, address(connector), extraData, options);
    }
}
