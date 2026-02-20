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
    uint256 internal constant SLOT_COUNT = 6;

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
        _assertReserveInvariant();

        usdc.approve(address(vault), 100_000e6);
        vault.repay(100_000e6);
        assertEq(vault.activeLoans(), 0);
        _assertReserveInvariant();
    }

    function testSupplyAndWithdrawEuler() public {
        vault.supplyToEuler(200_000e6);
        assertEq(usdc.balanceOf(address(eulerVault)), 200_000e6);
        _assertReserveInvariant();

        vault.withdrawFromEuler(50_000e6);
        assertEq(usdc.balanceOf(address(eulerVault)), 150_000e6);
        _assertReserveInvariant();
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
        _assertReserveInvariant();
    }

    function testBorrowRevertsWhenInsufficient() public {
        vm.expectRevert(CollarLiquidityVault.LV_InsufficientLiquidity.selector);
        vault.borrow(2_000_000e6);
    }

    function testSetEulerVaultRevertsWhenAssetMismatch() public {
        MockERC20 otherAsset = new MockERC20("Other", "OTHER", 6);
        MockERC4626 wrongEulerVault = new MockERC4626(otherAsset);

        vm.expectRevert(CollarLiquidityVault.LV_InvalidEulerAsset.selector);
        vault.setEulerVault(wrongEulerVault);
    }

    function testSetEulerVaultRevertsWhenCurrentEulerHasFunds() public {
        MockERC4626 newEulerVault = new MockERC4626(usdc);
        vault.supplyToEuler(1e6);

        vm.expectRevert(CollarLiquidityVault.LV_EulerVaultHasFunds.selector);
        vault.setEulerVault(newEulerVault);
    }

    function testSetEulerVaultSucceedsAfterCurrentEulerDrained() public {
        MockERC4626 newEulerVault = new MockERC4626(usdc);
        vault.supplyToEuler(125_000e6);
        vault.withdrawFromEuler(125_000e6);

        vault.setEulerVault(newEulerVault);
        assertEq(address(vault.eulerVault()), address(newEulerVault));
        _assertReserveInvariant();
    }

    function testReserveBlocksWithdrawableLiquidity() public {
        vault.supplyToEuler(900_000e6);
        vault.reserve(1, 300_000e6);
        assertEq(usdc.balanceOf(address(vault)), 300_000e6);
        assertEq(usdc.balanceOf(address(eulerVault)), 700_000e6);
        assertEq(vault.availableLiquidity(), 700_000e6);
        _assertReserveInvariant();

        vm.startPrank(lender);
        assertEq(vault.maxWithdraw(lender), 700_000e6);
        vm.stopPrank();
    }

    function testConsumeAndReleaseReserve() public {
        vault.supplyToEuler(800_000e6);
        vault.reserve(1, 200_000e6);
        assertEq(usdc.balanceOf(address(vault)), 200_000e6);
        assertEq(usdc.balanceOf(address(eulerVault)), 800_000e6);

        vault.consume(1, 120_000e6);
        assertEq(usdc.balanceOf(address(this)), 120_000e6);
        assertEq(vault.reservedByLoan(1), 80_000e6);
        assertEq(usdc.balanceOf(address(vault)), 80_000e6);
        _assertReserveInvariant();

        vault.release(1);
        assertEq(vault.reservedByLoan(1), 0);
        assertEq(vault.reservedLiquidity(), 0);
        assertEq(usdc.balanceOf(address(vault)), 0);
        assertEq(usdc.balanceOf(address(eulerVault)), 880_000e6);
        _assertReserveInvariant();
    }

    function testReservePrincipalPullsFromEulerAndReleaseRedeposits() public {
        vault.supplyToEuler(900_000e6);
        vault.reservePrincipal(7, 250_000e6);
        assertEq(usdc.balanceOf(address(vault)), 250_000e6);
        assertEq(usdc.balanceOf(address(eulerVault)), 750_000e6);
        _assertReserveInvariant();

        vault.releasePrincipal(7);
        assertEq(vault.reservedPrincipal(), 0);
        assertEq(vault.reservedPrincipalByLoan(7), 0);
        assertEq(usdc.balanceOf(address(vault)), 0);
        assertEq(usdc.balanceOf(address(eulerVault)), 1_000_000e6);
        _assertReserveInvariant();
    }

    function testCannotSupplyReservedLiquidityToEuler() public {
        vault.supplyToEuler(900_000e6);
        vault.reserve(1, 250_000e6);

        vm.expectRevert(CollarLiquidityVault.LV_ReservedLiquidityLocked.selector);
        vault.supplyToEuler(120_000e6);
        _assertReserveInvariant();
    }

    function testReserveRevertsWhenEulerFundsCannotBeWithdrawn() public {
        vault.supplyToEuler(900_000e6);

        uint256 eulerBalance = usdc.balanceOf(address(eulerVault));
        vm.prank(address(eulerVault));
        usdc.transfer(address(0xBEEF), eulerBalance);

        vm.expectRevert();
        vault.reserve(1, 200_000e6);
        _assertReserveInvariant();
    }

    function testFuzzReservationInvariantMultipleReserveRelease(uint256 seed, uint8 steps) public {
        uint256[SLOT_COUNT] memory reservedLiquidityBySlot;
        uint256[SLOT_COUNT] memory reservedPrincipalBySlot;
        uint256 stepsCount = bound(uint256(steps), 20, 120);

        for (uint256 i = 0; i < stepsCount; i++) {
            uint256 roll = uint256(keccak256(abi.encode(seed, i)));
            uint256 slot = (roll % SLOT_COUNT) + 1;
            uint256 op = roll % 6;

            if (op == 0) {
                uint256 freeOnHand = _freeOnHand();
                if (freeOnHand > 0) {
                    uint256 amount = bound((roll >> 32) % (freeOnHand + 1), 1, freeOnHand);
                    vault.supplyToEuler(amount);
                }
            } else if (op == 1) {
                uint256 eulerBal = usdc.balanceOf(address(eulerVault));
                if (eulerBal > 0) {
                    uint256 amount = bound((roll >> 32) % (eulerBal + 1), 1, eulerBal);
                    vault.withdrawFromEuler(amount);
                }
            } else if (op == 2) {
                if (reservedLiquidityBySlot[slot - 1] == 0) {
                    uint256 available = vault.availableLiquidity();
                    if (available > 0) {
                        uint256 amount = bound((roll >> 32) % (available + 1), 1, available);
                        vault.reserve(slot, amount);
                        reservedLiquidityBySlot[slot - 1] = amount;
                    }
                }
            } else if (op == 3) {
                uint256 existingReserve = reservedLiquidityBySlot[slot - 1];
                if (existingReserve > 0) {
                    vault.release(slot);
                    reservedLiquidityBySlot[slot - 1] = 0;
                }
            } else if (op == 4) {
                if (reservedPrincipalBySlot[slot - 1] == 0) {
                    uint256 available = vault.availableLiquidity();
                    if (available > 0) {
                        uint256 amount = bound((roll >> 32) % (available + 1), 1, available);
                        vault.reservePrincipal(slot + SLOT_COUNT, amount);
                        reservedPrincipalBySlot[slot - 1] = amount;
                    }
                }
            } else {
                uint256 existingPrincipalReserve = reservedPrincipalBySlot[slot - 1];
                if (existingPrincipalReserve > 0) {
                    vault.releasePrincipal(slot + SLOT_COUNT);
                    reservedPrincipalBySlot[slot - 1] = 0;
                }
            }

            _assertReserveInvariant();
        }
    }

    function _assertReserveInvariant() internal view {
        uint256 onHand = usdc.balanceOf(address(vault));
        uint256 totalReserved = vault.reservedLiquidity() + vault.reservedPrincipal();
        assertGe(onHand, totalReserved);
    }

    function _freeOnHand() internal view returns (uint256) {
        uint256 onHand = usdc.balanceOf(address(vault));
        uint256 totalReserved = vault.reservedLiquidity() + vault.reservedPrincipal();
        if (onHand <= totalReserved) {
            return 0;
        }
        return onHand - totalReserved;
    }
}
