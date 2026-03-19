// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IBridgeAdapter} from "../interfaces/IBridgeAdapter.sol";
import {ISocketWithdrawController} from "../interfaces/ISocketWithdrawController.sol";
import {ISocketConnectorShared, ISocketCoreShared} from "../interfaces/ISocketAdapterShared.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

contract SocketBridgeAdapterL2Compat is IBridgeAdapter {
    using SafeERC20 for IERC20;

    IERC20 public immutable asset;
    ISocketWithdrawController public immutable socketController;
    ISocketConnectorShared public immutable connector;
    ISocketCoreShared public immutable socket;

    uint256 public immutable messageIdPrefix;
    uint256 public immutable msgGasLimit;

    constructor(address asset_, address controller_, address connector_, uint256 msgGasLimit_) {
        if (asset_ == address(0)) revert("SBA_L2:zero-asset");
        if (controller_ == address(0)) revert("SBA_L2:zero-controller");
        if (connector_ == address(0)) revert("SBA_L2:zero-connector");

        asset = IERC20(asset_);
        socketController = ISocketWithdrawController(controller_);
        connector = ISocketConnectorShared(connector_);
        msgGasLimit = msgGasLimit_;

        address socket_ = ISocketConnectorShared(connector_).socket__();
        if (socket_ == address(0)) revert("SBA_L2:zero-socket");
        socket = ISocketCoreShared(socket_);

        uint32 siblingChainSlug = ISocketConnectorShared(connector_).siblingChainSlug();
        if (siblingChainSlug == 0) revert("SBA_L2:zero-sibling-slug");

        (address siblingPlug,,,,) = ISocketCoreShared(socket_).getPlugConfig(connector_, siblingChainSlug);
        if (siblingPlug == address(0)) revert("SBA_L2:zero-sibling-plug");

        messageIdPrefix =
            (uint256(ISocketCoreShared(socket_).chainSlug()) << 224) | (uint256(uint160(siblingPlug)) << 64);
    }

    function messageId() external view override returns (bytes32) {
        return bytes32(messageIdPrefix | uint256(socket.globalMessageCount()));
    }

    function estimateFee() external view override returns (uint256) {
        return socketController.getMinFees(address(connector), msgGasLimit);
    }

    function bridge(address receiver, uint256 amount) external payable override {
        asset.safeTransferFrom(msg.sender, address(this), amount);
        asset.forceApprove(address(socketController), amount);
        socketController.withdrawFromAppChain{value: msg.value}(receiver, amount, msgGasLimit, address(connector));
    }
}
