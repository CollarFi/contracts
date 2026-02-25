// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

import {ILendingAdapter} from "../interfaces/ILendingAdapter.sol";

interface IEVCMinimal {
    function call(address targetContract, address onBehalfOfAccount, uint256 value, bytes calldata data)
        external
        payable
        returns (bytes memory result);
    function setAccountOperator(address account, address operator, bool authorized) external payable;
    function enableCollateral(address account, address vault) external payable;
    function enableController(address account, address vault) external payable;
    function isCollateralEnabled(address account, address vault) external view returns (bool);
    function isControllerEnabled(address account, address vault) external view returns (bool);
    function isAccountOperatorAuthorized(address account, address operator) external view returns (bool);
}

interface IEVaultMinimal {
    function deposit(uint256 amount, address receiver) external returns (uint256);
    function withdraw(uint256 amount, address receiver, address owner) external returns (uint256);
    function borrow(uint256 amount, address receiver) external returns (uint256);
    function repay(uint256 amount, address receiver) external returns (uint256);
    function cash() external view returns (uint256);
    function debtOf(address account) external view returns (uint256);
    function balanceOf(address account) external view returns (uint256);
    function convertToAssets(uint256 shares) external view returns (uint256);
}

/// @notice Single-market Euler adapter: fixed collateral asset + fixed debt asset.
/// @dev Uses only subaccount 0 (`account = onBehalfOf`) and requires this adapter to be EVC operator for account.
contract EulerLendingAdapter is ILendingAdapter {
    using SafeERC20 for IERC20;

    error ELA_InvalidConfig();
    error ELA_NotOperator();

    IEVCMinimal public immutable evc;
    address public immutable collateralAsset;
    address public immutable collateralVault;
    address public immutable debtAsset;
    address public immutable debtVault;

    constructor(
        address evc_,
        address collateralAsset_,
        address collateralVault_,
        address debtAsset_,
        address debtVault_
    ) {
        if (
            evc_ == address(0) || collateralAsset_ == address(0) || collateralVault_ == address(0)
                || debtAsset_ == address(0) || debtVault_ == address(0)
        ) revert ELA_InvalidConfig();
        evc = IEVCMinimal(evc_);
        collateralAsset = collateralAsset_;
        collateralVault = collateralVault_;
        debtAsset = debtAsset_;
        debtVault = debtVault_;
    }

    function openSetup(address onBehalfOf) external view returns (address target, bytes memory data) {
        target = address(evc);
        data = abi.encodeCall(IEVCMinimal.setAccountOperator, (onBehalfOf, address(this), true));
    }

    function depositCollateral(uint256 amount, address onBehalfOf) external {
        _requireOperator(onBehalfOf);

        if (!evc.isCollateralEnabled(onBehalfOf, collateralVault)) {
            evc.enableCollateral(onBehalfOf, collateralVault);
        }

        IERC20(collateralAsset).safeTransferFrom(msg.sender, address(this), amount);
        IERC20(collateralAsset).forceApprove(collateralVault, amount);
        evc.call(collateralVault, onBehalfOf, 0, abi.encodeCall(IEVaultMinimal.deposit, (amount, onBehalfOf)));
    }

    function withdrawCollateral(uint256 amount, address onBehalfOf, address to) external {
        _requireOperator(onBehalfOf);

        evc.call(collateralVault, onBehalfOf, 0, abi.encodeCall(IEVaultMinimal.withdraw, (amount, to, onBehalfOf)));
    }

    function borrow(uint256 amount, address onBehalfOf, address to) external {
        _requireOperator(onBehalfOf);

        if (!evc.isControllerEnabled(onBehalfOf, debtVault)) {
            evc.enableController(onBehalfOf, debtVault);
        }
        evc.call(debtVault, onBehalfOf, 0, abi.encodeCall(IEVaultMinimal.borrow, (amount, to)));
    }

    function repay(uint256 amount, address onBehalfOf) external {
        _requireOperator(onBehalfOf);

        IERC20(debtAsset).safeTransferFrom(msg.sender, address(this), amount);
        IERC20(debtAsset).forceApprove(debtVault, amount);
        evc.call(debtVault, onBehalfOf, 0, abi.encodeCall(IEVaultMinimal.repay, (amount, onBehalfOf)));
    }

    function availableLiquidity() external view returns (uint256) {
        return IEVaultMinimal(debtVault).cash();
    }

    function currentDebt(address onBehalfOf) external view returns (uint256) {
        return IEVaultMinimal(debtVault).debtOf(onBehalfOf);
    }

    function currentCollateral(address onBehalfOf) external view returns (uint256) {
        IEVaultMinimal v = IEVaultMinimal(collateralVault);
        uint256 shares = v.balanceOf(onBehalfOf);
        return v.convertToAssets(shares);
    }

    function _requireOperator(address onBehalfOf) internal view {
        if (!evc.isAccountOperatorAuthorized(onBehalfOf, address(this))) revert ELA_NotOperator();
    }
}
