// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

import {MorphoBlueLendingAdapter} from "../src/adapters/MorphoBlueLendingAdapter.sol";
import {MockERC20} from "./mocks/MockERC20.sol";

import {Id, MarketParams, Position, Market} from "../lib/morpho-blue/src/interfaces/IMorpho.sol";
import {MarketParamsLib} from "../lib/morpho-blue/src/libraries/MarketParamsLib.sol";
import {SharesMathLib} from "../lib/morpho-blue/src/libraries/SharesMathLib.sol";

contract MockMorphoBlue {
    using SafeERC20 for IERC20;
    using MarketParamsLib for MarketParams;
    using SharesMathLib for uint256;

    mapping(Id => mapping(address => Position)) public position;
    mapping(Id => Market) public market;
    mapping(address => mapping(address => bool)) public isAuthorized;

    function setAuthorizationFor(address authorizer, address authorized, bool newIsAuthorized) external {
        isAuthorized[authorizer][authorized] = newIsAuthorized;
    }

    function seedLiquidity(MarketParams memory marketParams, uint256 assets) external {
        Id id = marketParams.id();
        Market storage m = market[id];
        m.totalSupplyAssets += uint128(assets);
        m.totalSupplyShares += uint128(assets);
        if (m.lastUpdate == 0) m.lastUpdate = uint128(block.timestamp);
    }

    function supplyCollateral(MarketParams memory marketParams, uint256 assets, address onBehalf, bytes memory)
        external
    {
        Id id = marketParams.id();
        if (market[id].lastUpdate == 0) market[id].lastUpdate = uint128(block.timestamp);
        position[id][onBehalf].collateral += uint128(assets);
        IERC20(marketParams.collateralToken).safeTransferFrom(msg.sender, address(this), assets);
    }

    function withdrawCollateral(MarketParams memory marketParams, uint256 assets, address onBehalf, address receiver)
        external
    {
        Id id = marketParams.id();
        Position storage p = position[id][onBehalf];
        require(p.collateral >= assets, "insufficient-collateral");
        p.collateral -= uint128(assets);
        IERC20(marketParams.collateralToken).safeTransfer(receiver, assets);
    }

    function borrow(MarketParams memory marketParams, uint256 assets, uint256, address onBehalf, address receiver)
        external
        returns (uint256 assetsBorrowed, uint256 sharesBorrowed)
    {
        Id id = marketParams.id();
        Market storage m = market[id];
        require(m.totalSupplyAssets >= m.totalBorrowAssets + assets, "insufficient-liquidity");

        uint256 shares = assets.toSharesUp(m.totalBorrowAssets, m.totalBorrowShares);
        position[id][onBehalf].borrowShares += uint128(shares);
        m.totalBorrowAssets += uint128(assets);
        m.totalBorrowShares += uint128(shares);
        if (m.lastUpdate == 0) m.lastUpdate = uint128(block.timestamp);

        IERC20(marketParams.loanToken).safeTransfer(receiver, assets);
        return (assets, shares);
    }

    function repay(MarketParams memory marketParams, uint256 assets, uint256, address onBehalf, bytes memory)
        external
        returns (uint256 assetsRepaid, uint256 sharesRepaid)
    {
        Id id = marketParams.id();
        Market storage m = market[id];
        Position storage p = position[id][onBehalf];

        uint256 shares = assets.toSharesDown(m.totalBorrowAssets, m.totalBorrowShares);
        if (shares > p.borrowShares) shares = p.borrowShares;
        uint256 clampedAssets = shares.toAssetsUp(m.totalBorrowAssets, m.totalBorrowShares);

        p.borrowShares -= uint128(shares);
        m.totalBorrowShares -= uint128(shares);
        m.totalBorrowAssets -= uint128(clampedAssets);

        IERC20(marketParams.loanToken).safeTransferFrom(msg.sender, address(this), clampedAssets);
        return (clampedAssets, shares);
    }
}

contract MorphoBlueLendingAdapterTest is Test {
    MockMorphoBlue internal morpho;
    MockERC20 internal collateral;
    MockERC20 internal usdc;
    MorphoBlueLendingAdapter internal adapter;
    MarketParams internal marketParams;

    address internal borrower = address(0xB0B);
    address internal receiver = address(0xCAFE);

    function setUp() public {
        morpho = new MockMorphoBlue();
        collateral = new MockERC20("Collateral", "COL", 18);
        usdc = new MockERC20("USD Coin", "USDC", 6);

        marketParams = MarketParams({
            loanToken: address(usdc),
            collateralToken: address(collateral),
            oracle: address(0x1111),
            irm: address(0x2222),
            lltv: 0.85e18
        });

        adapter = new MorphoBlueLendingAdapter(
            address(morpho),
            address(collateral),
            address(usdc),
            marketParams.oracle,
            marketParams.irm,
            marketParams.lltv
        );

        collateral.mint(address(this), 10 ether);
        collateral.approve(address(adapter), type(uint256).max);

        usdc.mint(address(morpho), 1_000_000e6);
        morpho.seedLiquidity(marketParams, 1_000_000e6);
        morpho.setAuthorizationFor(borrower, address(adapter), true);
    }

    function testDepositBorrowRepayAndWithdraw() public {
        adapter.depositCollateral(1 ether, borrower);
        assertEq(adapter.currentCollateral(borrower), 1 ether);

        uint256 availableBefore = adapter.availableLiquidity();
        adapter.borrow(500e6, borrower, receiver);
        assertEq(usdc.balanceOf(receiver), 500e6);
        assertLt(adapter.availableLiquidity(), availableBefore);

        uint256 debt = adapter.currentDebt(borrower);
        usdc.mint(address(this), debt);
        usdc.approve(address(adapter), type(uint256).max);
        adapter.repay(debt, borrower);
        assertLe(adapter.currentDebt(borrower), 1);

        uint256 before = collateral.balanceOf(receiver);
        adapter.withdrawCollateral(1 ether, borrower, receiver);
        assertEq(collateral.balanceOf(receiver) - before, 1 ether);
        assertEq(adapter.currentCollateral(borrower), 0);
    }

    function testRevertsWhenNotAuthorized() public {
        address stranger = address(0xD00D);
        adapter.depositCollateral(1 ether, stranger);

        vm.expectRevert(MorphoBlueLendingAdapter.MBLA_NotAuthorized.selector);
        adapter.borrow(1, stranger, receiver);

        vm.expectRevert(MorphoBlueLendingAdapter.MBLA_NotAuthorized.selector);
        adapter.withdrawCollateral(1, stranger, receiver);
    }
}
