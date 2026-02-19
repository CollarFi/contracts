// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";

import {CollarLiquidityVault} from "../src/CollarLiquidityVault.sol";
import {MockERC20} from "./mocks/MockERC20.sol";
import {MockERC4626} from "./mocks/MockERC4626.sol";

contract CollarLiquidityVaultTest is Test {
    MockERC20 internal usdc;
    CollarLiquidityVault internal vault;
    MockERC4626 internal eulerVault;

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

        eulerVault = new MockERC4626(usdc);
        vault.setEulerVault(eulerVault);
    }

    function testBorrowAndRepay() public {
        vault.borrow(100_000e6);
        assertEq(usdc.balanceOf(address(this)), 100_000e6);
        assertEq(vault.activeLoans(), 100_000e6);

        usdc.approve(address(vault), 100_000e6);
        vault.repay(100_000e6);
        assertEq(vault.activeLoans(), 0);
    }

    function testSupplyAndWithdrawEuler() public {
        vault.supplyToEuler(200_000e6);
        assertEq(usdc.balanceOf(address(eulerVault)), 200_000e6);

        vault.withdrawFromEuler(50_000e6);
        assertEq(usdc.balanceOf(address(eulerVault)), 150_000e6);
    }

    function testWithdrawPullsFromEuler() public {
        vault.supplyToEuler(900_000e6);
        vm.startPrank(lender);
        uint256 shares = vault.balanceOf(lender);
        uint256 withdrawAssets = 400_000e6;
        vault.withdraw(withdrawAssets, lender, lender);
        vm.stopPrank();

        assertLt(vault.balanceOf(lender), shares);
        assertEq(usdc.balanceOf(lender), withdrawAssets);
    }

    function testBorrowRevertsWhenInsufficient() public {
        vm.expectRevert(CollarLiquidityVault.LV_InsufficientLiquidity.selector);
        vault.borrow(2_000_000e6);
    }

    function testReserveBlocksWithdrawableLiquidity() public {
        vault.reserve(1, 300_000e6);
        assertEq(vault.availableLiquidity(), 700_000e6);

        vm.startPrank(lender);
        assertEq(vault.maxWithdraw(lender), 700_000e6);
        vm.stopPrank();
    }

    function testConsumeAndReleaseReserve() public {
        vault.reserve(1, 200_000e6);

        vault.consume(1, 120_000e6);
        assertEq(usdc.balanceOf(address(this)), 120_000e6);
        assertEq(vault.reservedByLoan(1), 80_000e6);

        vault.release(1);
        assertEq(vault.reservedByLoan(1), 0);
        assertEq(vault.reservedLiquidity(), 0);
    }
}
