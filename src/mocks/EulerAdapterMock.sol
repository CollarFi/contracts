// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ILendingAdapter} from "../interfaces/ILendingAdapter.sol";

/// @dev Minimal mock for local/fork dev. No real lending occurs.
contract EulerAdapterMock is ILendingAdapter {
    event DepositCollateral(uint256 amount, address onBehalfOf);
    event WithdrawCollateral(uint256 amount, address onBehalfOf, address to);
    event Borrow(uint256 amount, address onBehalfOf, address to);
    event Repay(uint256 amount, address onBehalfOf);

    function depositCollateral(uint256 amount, address onBehalfOf) external {
        emit DepositCollateral(amount, onBehalfOf);
    }

    function withdrawCollateral(uint256 amount, address onBehalfOf, address to) external {
        emit WithdrawCollateral(amount, onBehalfOf, to);
    }

    function borrow(uint256 amount, address onBehalfOf, address to) external {
        emit Borrow(amount, onBehalfOf, to);
    }

    function repay(uint256 amount, address onBehalfOf) external {
        emit Repay(amount, onBehalfOf);
    }

    function availableLiquidity() external pure returns (uint256) {
        return type(uint256).max;
    }

    function currentDebt(address) external pure returns (uint256) {
        return 0;
    }

    function currentCollateral(address) external pure returns (uint256) {
        return 0;
    }
}
