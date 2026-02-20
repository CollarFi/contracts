// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IBridgeAdapter} from "../interfaces/IBridgeAdapter.sol";
import {ISocketVault} from "../interfaces/ISocketVault.sol";
import {ISocketConnectorShared, ISocketCoreShared} from "../interfaces/ISocketAdapterShared.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

contract SocketBridgeAdapterOld is IBridgeAdapter {
    using SafeERC20 for IERC20;

    IERC20 public immutable asset;
    ISocketVault public immutable socketVault;
    ISocketConnectorShared public immutable connector;
    ISocketCoreShared public immutable socket;

    uint256 public immutable messageIdPrefix;
    uint256 public immutable msgGasLimit;

    constructor(address asset_, address socketVault_, address connector_, uint256 msgGasLimit_) {
        if (asset_ == address(0)) revert("SBA_OLD:zero-asset");
        if (socketVault_ == address(0)) revert("SBA_OLD:zero-vault");
        if (connector_ == address(0)) revert("SBA_OLD:zero-connector");

        asset = IERC20(asset_);
        socketVault = ISocketVault(socketVault_);
        connector = ISocketConnectorShared(connector_);
        msgGasLimit = msgGasLimit_;

        address socket_ = ISocketConnectorShared(connector_).socket__();
        if (socket_ == address(0)) revert("SBA_OLD:zero-socket");
        socket = ISocketCoreShared(socket_);

        uint32 siblingChainSlug = ISocketConnectorShared(connector_).siblingChainSlug();
        if (siblingChainSlug == 0) revert("SBA_OLD:zero-sibling-slug");

        (address siblingPlug,,,,) = ISocketCoreShared(socket_).getPlugConfig(connector_, siblingChainSlug);
        if (siblingPlug == address(0)) revert("SBA_OLD:zero-sibling-plug");

        messageIdPrefix =
            (uint256(ISocketCoreShared(socket_).chainSlug()) << 224) | (uint256(uint160(siblingPlug)) << 64);
    }

    function messageId() external view override returns (bytes32) {
        return bytes32(messageIdPrefix | uint256(socket.globalMessageCount()));
    }

    function estimateFee() external view override returns (uint256) {
        return socketVault.getMinFees(address(connector), msgGasLimit);
    }

    function bridge(address receiver, uint256 amount) external payable override {
        asset.safeTransferFrom(msg.sender, address(this), amount);
        asset.forceApprove(address(socketVault), amount);
        socketVault.depositToAppChain{value: msg.value}(receiver, amount, msgGasLimit, address(connector));
    }
}
