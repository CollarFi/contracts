// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface ILendingAdapter {
    /// @notice Optional setup call to execute from the position account before opening.
    /// @dev Return (address(0), "") when no setup is required.
    function openSetupCall(address onBehalfOf) external view returns (address target, bytes memory data);

    function depositCollateral(uint256 amount, address onBehalfOf) external;
    function withdrawCollateral(uint256 amount, address onBehalfOf, address to) external;
    function borrow(uint256 amount, address onBehalfOf, address to) external;
    function repay(uint256 amount, address onBehalfOf) external;
    function availableLiquidity() external view returns (uint256);
    function currentDebt(address onBehalfOf) external view returns (uint256);
    function currentCollateral(address onBehalfOf) external view returns (uint256);
}
