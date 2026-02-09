// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {ISocketBridge} from "../../src/interfaces/ISocketBridge.sol";

contract MockBridge is ISocketBridge {
    IERC20 public immutable token;

    event Bridged(
        address indexed asset, address indexed sender, address indexed receiver, uint256 amount, bytes extraData
    );

    constructor(IERC20 token_) {
        token = token_;
    }

    function bridge(address receiver_, uint256 amount_, uint256, address, bytes calldata extraData_, bytes calldata)
        external
        payable
        override
    {
        token.transferFrom(msg.sender, address(this), amount_);
        emit Bridged(address(token), msg.sender, receiver_, amount_, extraData_);
    }
}
