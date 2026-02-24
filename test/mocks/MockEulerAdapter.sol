// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

import {ILendingAdapter} from "../../src/interfaces/ILendingAdapter.sol";

contract MockEulerAdapter is ILendingAdapter {
    using SafeERC20 for IERC20;

    mapping(address => mapping(address => uint256)) public collateralBalances;
    mapping(address => uint256) public debts;
    mapping(address => uint256) public liquidity;

    error MEA_InsufficientCollateral();
    error MEA_RepayTooMuch();

    function depositCollateral(address asset, uint256 amount, address onBehalfOf) external override {
        IERC20(asset).safeTransferFrom(msg.sender, address(this), amount);
        collateralBalances[onBehalfOf][asset] += amount;
    }

    function withdrawCollateral(address asset, uint256 amount, address onBehalfOf, address to) external override {
        uint256 balance = collateralBalances[onBehalfOf][asset];
        if (amount > balance) {
            revert MEA_InsufficientCollateral();
        }
        collateralBalances[onBehalfOf][asset] = balance - amount;
        IERC20(asset).safeTransfer(to, amount);
    }

    function borrow(address asset, uint256 amount, address onBehalfOf, address to) external override {
        uint256 liq = liquidity[asset];
        if (liq != 0) {
            require(liq >= amount, "insufficient-liquidity");
            liquidity[asset] = liq - amount;
        }
        debts[onBehalfOf] += amount;
        IERC20(asset).safeTransfer(to, amount);
    }

    function repay(address asset, uint256 amount, address onBehalfOf) external override {
        uint256 debt = debts[onBehalfOf];
        if (amount > debt) {
            revert MEA_RepayTooMuch();
        }
        IERC20(asset).safeTransferFrom(msg.sender, address(this), amount);
        debts[onBehalfOf] = debt - amount;
        liquidity[asset] += amount;
    }

    function setLiquidity(address asset, uint256 amount) external {
        liquidity[asset] = amount;
    }

    function availableLiquidity(address debtAsset) external view override returns (uint256) {
        uint256 liq = liquidity[debtAsset];
        if (liq != 0) return liq;
        return IERC20(debtAsset).balanceOf(address(this));
    }

    function currentDebt(address debtAsset, address onBehalfOf) external view override returns (uint256) {
        debtAsset;
        return debts[onBehalfOf];
    }

    function currentCollateral(address collateralAsset, address onBehalfOf) external view override returns (uint256) {
        return collateralBalances[onBehalfOf][collateralAsset];
    }
}
