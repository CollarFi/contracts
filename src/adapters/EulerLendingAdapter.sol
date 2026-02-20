// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

import {ILendingAdapter} from "../interfaces/ILendingAdapter.sol";

interface IEVCMinimal {
    function call(address targetContract, address onBehalfOfAccount, uint256 value, bytes calldata data)
        external
        payable
        returns (bytes memory result);
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
}

/// @notice Generic lending adapter implementation for Euler Vault Kit (EVC-based).
/// @dev Borrowers must authorize this adapter as EVC operator on at least one supported subaccount.
contract EulerLendingAdapter is ILendingAdapter, Ownable {
    using SafeERC20 for IERC20;

    error ELA_InvalidConfig();
    error ELA_UnsupportedAsset();
    error ELA_NoAuthorizedSubaccount();

    IEVCMinimal public immutable evc;

    mapping(address => address) public collateralVaultOf;
    mapping(address => address) public debtVaultOf;
    mapping(address => address) public selectedAccountOf;
    uint8[] public subaccountCandidates;

    event CollateralVaultConfigured(address indexed asset, address indexed vault);
    event DebtVaultConfigured(address indexed asset, address indexed vault);
    event SubaccountCandidatesUpdated(uint8[] candidates);
    event BorrowerAccountSelected(address indexed borrower, address indexed account, uint8 subaccountId);

    constructor(address evc_, address owner_) Ownable(owner_) {
        if (evc_ == address(0) || owner_ == address(0)) revert ELA_InvalidConfig();
        evc = IEVCMinimal(evc_);
        subaccountCandidates.push(0);
        subaccountCandidates.push(1);
        subaccountCandidates.push(2);
        subaccountCandidates.push(3);
    }

    function setCollateralVault(address asset, address vault) external onlyOwner {
        if (asset == address(0) || vault == address(0)) revert ELA_InvalidConfig();
        collateralVaultOf[asset] = vault;
        emit CollateralVaultConfigured(asset, vault);
    }

    function setDebtVault(address asset, address vault) external onlyOwner {
        if (asset == address(0) || vault == address(0)) revert ELA_InvalidConfig();
        debtVaultOf[asset] = vault;
        emit DebtVaultConfigured(asset, vault);
    }

    function setSubaccountCandidates(uint8[] calldata candidates) external onlyOwner {
        if (candidates.length == 0) revert ELA_InvalidConfig();
        delete subaccountCandidates;
        for (uint256 i = 0; i < candidates.length; i++) {
            subaccountCandidates.push(candidates[i]);
        }
        emit SubaccountCandidatesUpdated(candidates);
    }

    function depositCollateral(address asset, uint256 amount, address onBehalfOf) external {
        address collateralVault = collateralVaultOf[asset];
        if (collateralVault == address(0)) revert ELA_UnsupportedAsset();

        address account = _pickAuthorizedAccount(onBehalfOf);
        selectedAccountOf[onBehalfOf] = account;

        if (!evc.isCollateralEnabled(account, collateralVault)) {
            evc.enableCollateral(account, collateralVault);
        }

        IERC20(asset).safeTransferFrom(msg.sender, address(this), amount);
        IERC20(asset).forceApprove(collateralVault, amount);
        evc.call(collateralVault, account, 0, abi.encodeCall(IEVaultMinimal.deposit, (amount, account)));
    }

    function withdrawCollateral(address asset, uint256 amount, address onBehalfOf, address to) external {
        address collateralVault = collateralVaultOf[asset];
        if (collateralVault == address(0)) revert ELA_UnsupportedAsset();

        address account = _resolveAccount(onBehalfOf);
        evc.call(collateralVault, account, 0, abi.encodeCall(IEVaultMinimal.withdraw, (amount, to, account)));
    }

    function borrow(address asset, uint256 amount, address onBehalfOf, address to) external {
        address debtVault = debtVaultOf[asset];
        if (debtVault == address(0)) revert ELA_UnsupportedAsset();

        address account = _resolveAccount(onBehalfOf);
        if (!evc.isControllerEnabled(account, debtVault)) {
            evc.enableController(account, debtVault);
        }
        evc.call(debtVault, account, 0, abi.encodeCall(IEVaultMinimal.borrow, (amount, to)));
    }

    function repay(address asset, uint256 amount, address onBehalfOf) external {
        address debtVault = debtVaultOf[asset];
        if (debtVault == address(0)) revert ELA_UnsupportedAsset();

        address account = _resolveAccount(onBehalfOf);
        IERC20(asset).safeTransferFrom(msg.sender, address(this), amount);
        IERC20(asset).forceApprove(debtVault, amount);
        evc.call(debtVault, account, 0, abi.encodeCall(IEVaultMinimal.repay, (amount, account)));
    }

    function _resolveAccount(address borrower) internal returns (address account) {
        account = selectedAccountOf[borrower];
        if (account == address(0) || !evc.isAccountOperatorAuthorized(account, address(this))) {
            account = _pickAuthorizedAccount(borrower);
            selectedAccountOf[borrower] = account;
        }
    }

    function _pickAuthorizedAccount(address borrower) internal returns (address account) {
        uint8 selectedSubId = 0;
        for (uint256 i = 0; i < subaccountCandidates.length; i++) {
            uint8 subId = subaccountCandidates[i];
            address candidate = _subaccount(borrower, subId);
            if (evc.isAccountOperatorAuthorized(candidate, address(this))) {
                account = candidate;
                selectedSubId = subId;
                break;
            }
        }
        if (account == address(0)) revert ELA_NoAuthorizedSubaccount();
        emit BorrowerAccountSelected(borrower, account, selectedSubId);
    }

    function _subaccount(address owner, uint8 subaccountId) internal pure returns (address) {
        return address(uint160(owner) ^ uint160(subaccountId));
    }
}
