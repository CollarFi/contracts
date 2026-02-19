// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {ERC4626} from "@openzeppelin/contracts/token/ERC20/extensions/ERC4626.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC4626} from "@openzeppelin/contracts/interfaces/IERC4626.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

contract CollarLiquidityVault is ERC4626, AccessControl, ReentrancyGuard {
    using SafeERC20 for IERC20;

    bytes32 public constant VAULT_ROLE = keccak256("VAULT_ROLE");
    bytes32 public constant PARAMETER_ROLE = keccak256("PARAMETER_ROLE");

    IERC4626 public eulerVault;
    uint256 public activeLoans;
    uint256 public reservedLiquidity;
    mapping(uint256 => uint256) public reservedByLoan;

    error LV_InsufficientLiquidity();
    error LV_InvalidAmount();
    error LV_RepayExceedsDebt();
    error LV_EulerVaultNotSet();
    error LV_ReserveExists();
    error LV_ReserveMissing();
    error LV_ReserveExceeds();

    event LossRecorded(uint256 amount);
    event LiquidityReserved(uint256 indexed loanId, uint256 amount);
    event LiquidityReleased(uint256 indexed loanId, uint256 amount);
    event LiquidityConsumed(uint256 indexed loanId, uint256 amount);

    constructor(IERC20 asset_, string memory name_, string memory symbol_, address admin)
        ERC20(name_, symbol_)
        ERC4626(asset_)
    {
        if (admin == address(0)) {
            revert LV_InvalidAmount();
        }
        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        _grantRole(VAULT_ROLE, admin);
        _grantRole(PARAMETER_ROLE, admin);
    }

    /// @notice Update the Euler vault used for idle liquidity.
    function setEulerVault(IERC4626 newEulerVault) external onlyRole(PARAMETER_ROLE) {
        eulerVault = newEulerVault;
    }

    /// @notice Supply idle USDC into the Euler vault.
    function supplyToEuler(uint256 assets) external onlyRole(PARAMETER_ROLE) nonReentrant {
        if (address(eulerVault) == address(0)) {
            revert LV_EulerVaultNotSet();
        }
        IERC20(asset()).safeIncreaseAllowance(address(eulerVault), assets);
        eulerVault.deposit(assets, address(this));
    }

    /// @notice Withdraw USDC from the Euler vault back to the pool.
    function withdrawFromEuler(uint256 assets) external onlyRole(PARAMETER_ROLE) nonReentrant {
        if (address(eulerVault) == address(0)) {
            revert LV_EulerVaultNotSet();
        }
        eulerVault.withdraw(assets, address(this), address(this));
    }

    /// @notice Borrow USDC for active loans.
    function borrow(uint256 amount) external onlyRole(VAULT_ROLE) nonReentrant {
        if (amount == 0) {
            revert LV_InvalidAmount();
        }
        uint256 available = availableLiquidity();
        if (amount > available) {
            revert LV_InsufficientLiquidity();
        }
        _pullFromEulerIfNeeded(amount);
        activeLoans += amount;
        IERC20(asset()).safeTransfer(msg.sender, amount);
    }

    /// @notice Repay borrowed USDC back to the pool.
    function repay(uint256 amount) external onlyRole(VAULT_ROLE) nonReentrant {
        if (amount == 0) {
            revert LV_InvalidAmount();
        }
        if (amount > activeLoans) {
            revert LV_RepayExceedsDebt();
        }
        IERC20(asset()).safeTransferFrom(msg.sender, address(this), amount);
        activeLoans -= amount;
    }

    /// @notice Record a loan loss without transferring assets (lenders absorb the shortfall).
    function writeOff(uint256 amount) external onlyRole(VAULT_ROLE) nonReentrant {
        if (amount == 0) {
            revert LV_InvalidAmount();
        }
        if (amount > activeLoans) {
            revert LV_RepayExceedsDebt();
        }
        activeLoans -= amount;
        emit LossRecorded(amount);
    }

    /// @notice Reserve liquidity for a loan-level negative option premium budget.
    function reserve(uint256 loanId, uint256 amount) external onlyRole(VAULT_ROLE) nonReentrant {
        if (loanId == 0 || amount == 0) {
            revert LV_InvalidAmount();
        }
        if (reservedByLoan[loanId] != 0) {
            revert LV_ReserveExists();
        }
        if (amount > availableLiquidity()) {
            revert LV_InsufficientLiquidity();
        }
        reservedByLoan[loanId] = amount;
        reservedLiquidity += amount;
        emit LiquidityReserved(loanId, amount);
    }

    /// @notice Consume reserved liquidity and transfer it to the caller.
    function consume(uint256 loanId, uint256 amount) external onlyRole(VAULT_ROLE) nonReentrant {
        if (amount == 0) {
            revert LV_InvalidAmount();
        }
        uint256 reserved = reservedByLoan[loanId];
        if (reserved == 0) {
            revert LV_ReserveMissing();
        }
        if (amount > reserved) {
            revert LV_ReserveExceeds();
        }
        reservedByLoan[loanId] = reserved - amount;
        reservedLiquidity -= amount;
        _pullFromEulerIfNeeded(amount);
        IERC20(asset()).safeTransfer(msg.sender, amount);
        emit LiquidityConsumed(loanId, amount);
    }

    /// @notice Release any unused reserve for a loan.
    function release(uint256 loanId) external onlyRole(VAULT_ROLE) nonReentrant {
        uint256 reserved = reservedByLoan[loanId];
        if (reserved == 0) {
            revert LV_ReserveMissing();
        }
        delete reservedByLoan[loanId];
        reservedLiquidity -= reserved;
        emit LiquidityReleased(loanId, reserved);
    }

    /// @notice Return assets immediately available for withdrawal or borrowing.
    function availableLiquidity() public view returns (uint256) {
        uint256 gross = IERC20(asset()).balanceOf(address(this)) + _eulerAssets();
        if (gross <= reservedLiquidity) {
            return 0;
        }
        return gross - reservedLiquidity;
    }

    /// @notice Return total assets including outstanding loans and Euler balance.
    function totalAssets() public view override returns (uint256) {
        // In-flight amounts are excluded from NAV/share pricing until funds land on L1.
        return IERC20(asset()).balanceOf(address(this)) + _eulerAssets() + activeLoans;
    }

    /// @notice Return the maximum assets an owner can withdraw based on available liquidity.
    function maxWithdraw(address owner) public view override returns (uint256) {
        uint256 ownerMax = super.maxWithdraw(owner);
        uint256 available = availableLiquidity();
        return ownerMax < available ? ownerMax : available;
    }

    /// @notice Return the maximum shares an owner can redeem based on available liquidity.
    function maxRedeem(address owner) public view override returns (uint256) {
        uint256 ownerMax = super.maxRedeem(owner);
        uint256 availableShares = convertToShares(availableLiquidity());
        return ownerMax < availableShares ? ownerMax : availableShares;
    }

    function _withdraw(address caller, address receiver, address owner, uint256 assets, uint256 shares)
        internal
        override
        nonReentrant
    {
        _pullFromEulerIfNeeded(assets);
        super._withdraw(caller, receiver, owner, assets, shares);
    }

    function _pullFromEulerIfNeeded(uint256 assets) internal {
        uint256 balance = IERC20(asset()).balanceOf(address(this));
        if (assets <= balance) {
            return;
        }
        if (address(eulerVault) == address(0)) {
            revert LV_InsufficientLiquidity();
        }
        uint256 shortfall = assets - balance;
        eulerVault.withdraw(shortfall, address(this), address(this));
    }

    function _eulerAssets() internal view returns (uint256) {
        if (address(eulerVault) == address(0)) {
            return 0;
        }
        uint256 shares = eulerVault.balanceOf(address(this));
        if (shares == 0) {
            return 0;
        }
        return eulerVault.previewRedeem(shares);
    }
}
