// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ILendingAdapter} from "../interfaces/ILendingAdapter.sol";

/// @dev Minimal mock for local/fork dev. No real lending occurs.
contract EulerAdapterMock is ILendingAdapter {
    event DepositCollateral(address asset, uint256 amount, address onBehalfOf);
    event WithdrawCollateral(address asset, uint256 amount, address onBehalfOf, address to);
    event Borrow(address asset, uint256 amount, address onBehalfOf, address to);
    event Repay(address asset, uint256 amount, address onBehalfOf);

    function depositCollateral(address asset, uint256 amount, address onBehalfOf) external {
        emit DepositCollateral(asset, amount, onBehalfOf);
    }

    function withdrawCollateral(address asset, uint256 amount, address onBehalfOf, address to) external {
        emit WithdrawCollateral(asset, amount, onBehalfOf, to);
    }

    function borrow(address asset, uint256 amount, address onBehalfOf, address to) external {
        emit Borrow(asset, amount, onBehalfOf, to);
    }

    function repay(address asset, uint256 amount, address onBehalfOf) external {
        emit Repay(asset, amount, onBehalfOf);
    }

    function availableLiquidity(address) external pure returns (uint256) {
        return type(uint256).max;
    }
}
