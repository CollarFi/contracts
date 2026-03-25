// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @dev Extremely thin placeholder endpoint for OApp deployments on forks.
/// It is NOT a functional LayerZero endpoint.
contract LZEndpointV2Mock {
    // accept ether
    receive() external payable {}
}
