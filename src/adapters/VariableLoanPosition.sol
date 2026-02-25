// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

import {ILendingAdapter} from "../interfaces/ILendingAdapter.sol";
import {IVariableLoanPosition} from "../interfaces/IVariableLoanPosition.sol";

interface IEVCOperatorAuth {
    function setAccountOperator(address account, address operator, bool authorized) external payable;
}

interface IMorphoOperatorAuth {
    function isAuthorized(address authorizer, address authorized) external view returns (bool);
    function setAuthorization(address authorized, bool newIsAuthorized) external;
}

contract VariableLoanPosition is IVariableLoanPosition {
    using SafeERC20 for IERC20;

    error VLP_AlreadyInitialized();
    error VLP_NotVault();

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
        _ensureAdapterOperatorAuthorization();
        IERC20(collateralAsset).safeTransferFrom(collateralProvider, address(this), collateralAmount);
        IERC20(collateralAsset).safeIncreaseAllowance(address(adapter), collateralAmount);
        adapter.depositCollateral(collateralAsset, collateralAmount, address(this));
        adapter.borrow(debtAsset, debtAmount, address(this), debtReceiver);
    }

    function repay(uint256 amount, address payer) external onlyVault {
        IERC20(debtAsset).safeTransferFrom(payer, address(this), amount);
        IERC20(debtAsset).safeIncreaseAllowance(address(adapter), amount);
        adapter.repay(debtAsset, amount, address(this));
    }

    function withdraw(uint256 amount, address to) external onlyVault {
        adapter.withdrawCollateral(collateralAsset, amount, address(this), to);
    }

    function availableLiquidity() external view returns (uint256) {
        return adapter.availableLiquidity(debtAsset);
    }

    function currentDebt() external view returns (uint256) {
        return adapter.currentDebt(debtAsset, address(this));
    }

    function currentCollateral() external view returns (uint256) {
        return adapter.currentCollateral(collateralAsset, address(this));
    }

    function _ensureAdapterOperatorAuthorization() internal {
        // Optional adapter-specific bootstrap for EVC-based adapters exposing `evc()(address)`.
        (bool evcOk, bytes memory evcOut) = address(adapter).staticcall(abi.encodeWithSignature("evc()"));
        if (evcOk && evcOut.length >= 32) {
            address evcAddr = abi.decode(evcOut, (address));
            if (evcAddr != address(0)) {
                IEVCOperatorAuth(evcAddr).setAccountOperator(address(this), address(adapter), true);
                return;
            }
        }

        // Optional bootstrap for Morpho Blue adapters exposing `morpho()(address)`.
        (bool morphoOk, bytes memory morphoOut) = address(adapter).staticcall(abi.encodeWithSignature("morpho()"));
        if (!morphoOk || morphoOut.length < 32) return;
        address morphoAddr = abi.decode(morphoOut, (address));
        if (morphoAddr == address(0)) return;

        IMorphoOperatorAuth morpho = IMorphoOperatorAuth(morphoAddr);
        if (!morpho.isAuthorized(address(this), address(adapter))) {
            morpho.setAuthorization(address(adapter), true);
        }
    }
}
