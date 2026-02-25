// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

import {ILendingAdapter} from "../../src/interfaces/ILendingAdapter.sol";

contract MockEulerAdapter is ILendingAdapter {
    using SafeERC20 for IERC20;

    mapping(address => uint256) public collateralBalances;
    mapping(address => uint256) public debts;
    uint256 public liquidity;

    IERC20 public immutable collateralAsset;
    IERC20 public immutable debtAsset;

    error MEA_InsufficientCollateral();
    error MEA_RepayTooMuch();

    constructor(address collateralAsset_, address debtAsset_) {
        collateralAsset = IERC20(collateralAsset_);
        debtAsset = IERC20(debtAsset_);
    }

    function depositCollateral(uint256 amount, address onBehalfOf) external override {
        collateralAsset.safeTransferFrom(msg.sender, address(this), amount);
        collateralBalances[onBehalfOf] += amount;
    }

    function withdrawCollateral(uint256 amount, address onBehalfOf, address to) external override {
        uint256 balance = collateralBalances[onBehalfOf];
        if (amount > balance) {
            revert MEA_InsufficientCollateral();
        }
        collateralBalances[onBehalfOf] = balance - amount;
        collateralAsset.safeTransfer(to, amount);
    }

    function borrow(uint256 amount, address onBehalfOf, address to) external override {
        if (liquidity != 0) {
            require(liquidity >= amount, "insufficient-liquidity");
            liquidity -= amount;
        }
        debts[onBehalfOf] += amount;
        debtAsset.safeTransfer(to, amount);
    }

    function repay(uint256 amount, address onBehalfOf) external override {
        uint256 debt = debts[onBehalfOf];
        if (amount > debt) {
            revert MEA_RepayTooMuch();
        }
        debtAsset.safeTransferFrom(msg.sender, address(this), amount);
        debts[onBehalfOf] = debt - amount;
        liquidity += amount;
    }

    function setLiquidity(uint256 amount) external {
        liquidity = amount;
    }

    function availableLiquidity() external view override returns (uint256) {
        if (liquidity != 0) return liquidity;
        return debtAsset.balanceOf(address(this));
    }

    function currentDebt(address onBehalfOf) external view override returns (uint256) {
        return debts[onBehalfOf];
    }

    function currentCollateral(address onBehalfOf) external view override returns (uint256) {
        return collateralBalances[onBehalfOf];
    }
}
