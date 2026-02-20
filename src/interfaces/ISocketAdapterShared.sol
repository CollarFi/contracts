// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ISocketBridge} from "./ISocketBridge.sol";

interface ISocketBridgeWithFees is ISocketBridge {
    function getMinFees(address connector_, uint256 msgGasLimit_, uint256 payloadSize_)
        external
        view
        returns (uint256 totalFees);
}

interface ISocketConnectorShared {
    function socket__() external view returns (address);
    function siblingChainSlug() external view returns (uint32);
}

interface ISocketCoreShared {
    function chainSlug() external view returns (uint32);
    function globalMessageCount() external view returns (uint64);
    function getPlugConfig(address plugAddress_, uint32 siblingChainSlug_)
        external
        view
        returns (address siblingPlug, address, address, address, address);
}
