// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

import {EulerLendingAdapter} from "../src/adapters/EulerLendingAdapter.sol";
import {MockERC20} from "./mocks/MockERC20.sol";

contract MockEVault {
    using SafeERC20 for IERC20;

    IERC20 public immutable asset;
    mapping(address => uint256) public collateralOf;
    mapping(address => uint256) public debtOf;

    constructor(IERC20 asset_) {
        asset = asset_;
    }

    function deposit(uint256 amount, address receiver) external returns (uint256) {
        collateralOf[receiver] += amount;
        return amount;
    }

    function withdraw(uint256 amount, address receiver, address owner) external returns (uint256) {
        collateralOf[owner] -= amount;
        asset.safeTransfer(receiver, amount);
        return amount;
    }

    function borrow(uint256 amount, address receiver) external returns (uint256) {
        debtOf[msg.sender] += amount;
        asset.safeTransfer(receiver, amount);
        return amount;
    }

    function repay(uint256 amount, address receiver) external returns (uint256) {
        debtOf[receiver] -= amount;
        return amount;
    }
}

contract MockEVC {
    mapping(address => mapping(address => bool)) public isAccountOperatorAuthorized;
    mapping(address => mapping(address => bool)) public isCollateralEnabled;
    mapping(address => mapping(address => bool)) public isControllerEnabled;

    function setOperator(address account, address operator, bool ok) external {
        isAccountOperatorAuthorized[account][operator] = ok;
    }

    function enableCollateral(address account, address vault) external payable {
        require(isAccountOperatorAuthorized[account][msg.sender], "not operator");
        isCollateralEnabled[account][vault] = true;
    }

    function enableController(address account, address vault) external payable {
        require(isAccountOperatorAuthorized[account][msg.sender], "not operator");
        isControllerEnabled[account][vault] = true;
    }

    function call(address targetContract, address onBehalfOfAccount, uint256, bytes calldata data)
        external
        payable
        returns (bytes memory result)
    {
        require(isAccountOperatorAuthorized[onBehalfOfAccount][msg.sender], "not operator");
        (bool ok, bytes memory ret) = targetContract.call(data);
        require(ok, "call failed");
        return ret;
    }
}

contract EulerLendingAdapterTest is Test {
    MockEVC internal evc;
    MockERC20 internal collateral;
    MockERC20 internal usdc;
    MockEVault internal collateralVault;
    MockEVault internal debtVault;
    EulerLendingAdapter internal adapter;

    address internal borrower = address(0xB0B);
    address internal receiver = address(0xCAFE);

    function setUp() public {
        evc = new MockEVC();
        collateral = new MockERC20("Collateral", "COL", 18);
        usdc = new MockERC20("USD Coin", "USDC", 6);
        collateralVault = new MockEVault(IERC20(address(collateral)));
        debtVault = new MockEVault(IERC20(address(usdc)));

        adapter = new EulerLendingAdapter(address(evc), address(this));
        adapter.setCollateralVault(address(collateral), address(collateralVault));
        adapter.setDebtVault(address(usdc), address(debtVault));

        collateral.mint(address(this), 10 ether);
        collateral.approve(address(adapter), type(uint256).max);
        usdc.mint(address(debtVault), 1_000_000e6);
    }

    function testSelectsFallbackSubaccountWhenZeroIsNotAuthorized() public {
        address sub0 = _subaccount(borrower, 0);
        address sub1 = _subaccount(borrower, 1);
        evc.setOperator(sub0, address(adapter), false);
        evc.setOperator(sub1, address(adapter), true);

        adapter.depositCollateral(address(collateral), 1 ether, borrower);

        assertEq(adapter.selectedAccountOf(borrower), sub1);
        assertEq(collateralVault.collateralOf(sub1), 1 ether);
    }

    function testBorrowUsesSelectedFallbackSubaccount() public {
        address sub1 = _subaccount(borrower, 1);
        evc.setOperator(sub1, address(adapter), true);

        adapter.depositCollateral(address(collateral), 1 ether, borrower);
        adapter.borrow(address(usdc), 500e6, borrower, receiver);

        assertEq(usdc.balanceOf(receiver), 500e6);
    }

    function testRevertsWhenNoAuthorizedSubaccount() public {
        vm.expectRevert(EulerLendingAdapter.ELA_NoAuthorizedSubaccount.selector);
        adapter.depositCollateral(address(collateral), 1 ether, borrower);
    }

    function _subaccount(address owner, uint8 subaccountId) internal pure returns (address) {
        return address(uint160(owner) ^ uint160(subaccountId));
    }
}
