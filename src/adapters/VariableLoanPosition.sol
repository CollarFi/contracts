// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

import {ILendingAdapter} from "../interfaces/ILendingAdapter.sol";
import {IVariableLoanPosition} from "../interfaces/IVariableLoanPosition.sol";

contract VariableLoanPosition is IVariableLoanPosition {
    using SafeERC20 for IERC20;

    error VLP_AlreadyInitialized();
    error VLP_NotVault();
    error VLP_SetupFailed();

    bool public initialized;
    address public vault;

    constructor() {
        initialized = true;
    }
    address public borrower;
    address public collateralAsset;
    address public debtAsset;
    ILendingAdapter public adapter;

    modifier onlyVault() {
        if (msg.sender != vault) revert VLP_NotVault();
        _;
    }

    function initialize(
        address vault_,
        address adapter_,
        address borrower_,
        address collateralAsset_,
        address debtAsset_
    ) external {
        if (initialized) revert VLP_AlreadyInitialized();
        initialized = true;
        vault = vault_;
        adapter = ILendingAdapter(adapter_);
        borrower = borrower_;
        collateralAsset = collateralAsset_;
        debtAsset = debtAsset_;
    }

    function open(uint256 collateralAmount, uint256 debtAmount, address debtReceiver, address collateralProvider)
        external
        onlyVault
    {
        _runOpenSetup();
        IERC20(collateralAsset).safeTransferFrom(collateralProvider, address(this), collateralAmount);
        IERC20(collateralAsset).safeIncreaseAllowance(address(adapter), collateralAmount);
        adapter.depositCollateral(collateralAmount, address(this));
        adapter.borrow(debtAmount, address(this), debtReceiver);
    }

    function repay(uint256 amount, address payer) external onlyVault {
        IERC20(debtAsset).safeTransferFrom(payer, address(this), amount);
        IERC20(debtAsset).safeIncreaseAllowance(address(adapter), amount);
        adapter.repay(amount, address(this));
    }

    function withdraw(uint256 amount, address to) external onlyVault {
        adapter.withdrawCollateral(amount, address(this), to);
    }

    function availableLiquidity() external view returns (uint256) {
        return adapter.availableLiquidity();
    }

    function currentDebt() external view returns (uint256) {
        return adapter.currentDebt(address(this));
    }

    function currentCollateral() external view returns (uint256) {
        return adapter.currentCollateral(address(this));
    }

    function _runOpenSetup() internal {
        (address target, bytes memory data) = adapter.openSetup(address(this));
        if (target == address(0) || data.length == 0) return;

        (bool ok, bytes memory ret) = target.call(data);
        if (!ok) {
            if (ret.length > 0) {
                assembly ("memory-safe") {
                    revert(add(ret, 0x20), mload(ret))
                }
            }
            revert VLP_SetupFailed();
        }
    }
}
