// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface ILendingAdapter {
    function depositCollateral(address asset, uint256 amount, address onBehalfOf) external;
    function withdrawCollateral(address asset, uint256 amount, address onBehalfOf, address to) external;
    function borrow(address asset, uint256 amount, address onBehalfOf, address to) external;
    function repay(address asset, uint256 amount, address onBehalfOf) external;
}
