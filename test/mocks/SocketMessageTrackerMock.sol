// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @dev Minimal ISocketMessageTracker-like mock.
contract SocketMessageTrackerMock {
    mapping(bytes32 => bool) public messageExecuted;

    function setExecuted(bytes32 messageId, bool executed) external {
        messageExecuted[messageId] = executed;
    }
}
