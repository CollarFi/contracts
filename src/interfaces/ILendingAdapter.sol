// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface ILendingAdapter {
    function depositCollateral(uint256 amount, address onBehalfOf) external;
    function withdrawCollateral(uint256 amount, address onBehalfOf, address to) external;
    function borrow(uint256 amount, address onBehalfOf, address to) external;
    function repay(uint256 amount, address onBehalfOf) external;
    function availableLiquidity() external view returns (uint256);
    function currentDebt(address onBehalfOf) external view returns (uint256);
    function currentCollateral(address onBehalfOf) external view returns (uint256);
}
