// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";

import {CollarLiquidityVault} from "../src/CollarLiquidityVault.sol";
import {MockERC20} from "./mocks/MockERC20.sol";
import {MockERC4626} from "./mocks/MockERC4626.sol";

contract CollarLiquidityVaultTest is Test {
    MockERC20 internal usdc;
    CollarLiquidityVault internal vault;
    MockERC4626 internal yieldVault;

    address internal lender = address(0x1111);
    address internal borrower = address(0x2222);

    function setUp() public {
        usdc = new MockERC20("USD Coin", "USDC", 6);
        vault = new CollarLiquidityVault(usdc, "Collar USDC", "cUSDC", address(this));
        vault.grantRole(vault.VAULT_ROLE(), address(this));

        usdc.mint(lender, 1_000_000e6);
        vm.startPrank(lender);
        usdc.approve(address(vault), type(uint256).max);
        vault.deposit(1_000_000e6, lender);
        vm.stopPrank();

        yieldVault = new MockERC4626(usdc);
        vault.setYieldVault(yieldVault);
    }

    function testBorrowAndRepay() public {
        vault.borrow(100_000e6);
        assertEq(usdc.balanceOf(address(this)), 100_000e6);
        assertEq(vault.activeLoans(), 100_000e6);
        _assertReserveInvariant();

        usdc.approve(address(vault), 100_000e6);
        vault.repay(100_000e6);
        assertEq(vault.activeLoans(), 0);
        _assertReserveInvariant();
    }

    function testSupplyAndWithdrawYieldVault() public {
        vault.supplyToYieldVault(200_000e6);
        assertEq(usdc.balanceOf(address(yieldVault)), 200_000e6);
        _assertReserveInvariant();

        vault.withdrawFromYieldVault(50_000e6);
        assertEq(usdc.balanceOf(address(yieldVault)), 150_000e6);
        _assertReserveInvariant();
    }

    function testWithdrawPullsFromYieldVault() public {
        vault.supplyToYieldVault(900_000e6);
        vm.startPrank(lender);
        uint256 shares = vault.balanceOf(lender);
        uint256 withdrawAssets = 400_000e6;
        vault.withdraw(withdrawAssets, lender, lender);
        vm.stopPrank();

        assertLt(vault.balanceOf(lender), shares);
        assertEq(usdc.balanceOf(lender), withdrawAssets);
        _assertReserveInvariant();
    }

    function testBorrowRevertsWhenInsufficient() public {
        vm.expectRevert(CollarLiquidityVault.LV_InsufficientLiquidity.selector);
        vault.borrow(2_000_000e6);
    }

    function testSetYieldVaultRevertsWhenAssetMismatch() public {
        MockERC20 otherAsset = new MockERC20("Other", "OTHER", 6);
        MockERC4626 wrongYieldVault = new MockERC4626(otherAsset);

        vm.expectRevert(CollarLiquidityVault.LV_InvalidYieldVaultAsset.selector);
        vault.setYieldVault(wrongYieldVault);
    }

    function testSetYieldVaultRevertsWhenCurrentYieldVaultHasFunds() public {
        MockERC4626 newYieldVault = new MockERC4626(usdc);
        vault.supplyToYieldVault(1e6);

        vm.expectRevert(CollarLiquidityVault.LV_YieldVaultHasFunds.selector);
        vault.setYieldVault(newYieldVault);
    }

    function testSetYieldVaultSucceedsAfterCurrentYieldVaultDrained() public {
        MockERC4626 newYieldVault = new MockERC4626(usdc);
        vault.supplyToYieldVault(125_000e6);
        vault.withdrawFromYieldVault(125_000e6);

        vault.setYieldVault(newYieldVault);
        assertEq(address(vault.yieldVault()), address(newYieldVault));
        _assertReserveInvariant();
    }

    function testReservePrincipalPullsFromYieldVaultAndReleaseRedeposits() public {
        vault.supplyToYieldVault(900_000e6);
        vault.reservePrincipal(7, 250_000e6);
        assertEq(usdc.balanceOf(address(vault)), 250_000e6);
        assertEq(usdc.balanceOf(address(yieldVault)), 750_000e6);
        _assertReserveInvariant();

        vault.releasePrincipal(7);
        assertEq(vault.reservedPrincipal(), 0);
        assertEq(vault.reservedPrincipalByLoan(7), 0);
        assertEq(usdc.balanceOf(address(vault)), 0);
        assertEq(usdc.balanceOf(address(yieldVault)), 1_000_000e6);
        _assertReserveInvariant();
    }

    function _assertReserveInvariant() internal view {
        uint256 onHand = usdc.balanceOf(address(vault));
        assertGe(onHand, vault.reservedPrincipal());
    }
}
