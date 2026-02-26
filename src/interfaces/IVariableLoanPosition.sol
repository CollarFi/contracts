// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IVariableLoanPosition {
    function initialize(address vault, address adapter, address borrower, address collateralAsset, address debtAsset)
        external;

    function open(uint256 collateralAmount, uint256 debtAmount, address debtReceiver, address collateralProvider)
        external;

    function repay(uint256 amount, address payer) external;
    function withdraw(uint256 amount, address to) external;
    function availableLiquidity() external view returns (uint256);
    function currentDebt() external view returns (uint256);
    function currentCollateral() external view returns (uint256);
}
