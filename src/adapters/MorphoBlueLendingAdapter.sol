// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

import {ILendingAdapter} from "../interfaces/ILendingAdapter.sol";

import {Id, IMorpho, MarketParams, Market, Position} from "../../lib/morpho-blue/src/interfaces/IMorpho.sol";
import {MarketParamsLib} from "../../lib/morpho-blue/src/libraries/MarketParamsLib.sol";
import {SharesMathLib} from "../../lib/morpho-blue/src/libraries/SharesMathLib.sol";

/// @notice Morpho Blue lending adapter for a single fixed market.
/// @dev Supports one collateral token and one debt token only.
contract MorphoBlueLendingAdapter is ILendingAdapter {
    using SafeERC20 for IERC20;
    using MarketParamsLib for MarketParams;
    using SharesMathLib for uint256;

    error MBLA_InvalidConfig();
    error MBLA_NotAuthorized();

    IMorpho public immutable morpho;
    Id public immutable marketId;

    address public immutable collateralAsset;
    address public immutable debtAsset;
    address public immutable oracle;
    address public immutable irm;
    uint256 public immutable lltv;

    constructor(
        address morpho_,
        address collateralAsset_,
        address debtAsset_,
        address oracle_,
        address irm_,
        uint256 lltv_
    ) {
        if (
            morpho_ == address(0) || collateralAsset_ == address(0) || debtAsset_ == address(0) || oracle_ == address(0)
                || irm_ == address(0) || lltv_ == 0 || lltv_ >= 1e18
        ) revert MBLA_InvalidConfig();

        morpho = IMorpho(morpho_);
        collateralAsset = collateralAsset_;
        debtAsset = debtAsset_;
        oracle = oracle_;
        irm = irm_;
        lltv = lltv_;

        marketId = _marketParams().id();
    }

    function depositCollateral(uint256 amount, address onBehalfOf) external {
        IERC20(collateralAsset).safeTransferFrom(msg.sender, address(this), amount);
        IERC20(collateralAsset).forceApprove(address(morpho), amount);
        morpho.supplyCollateral(_marketParams(), amount, onBehalfOf, "");
    }

    function withdrawCollateral(uint256 amount, address onBehalfOf, address to) external {
        _requireAuthorized(onBehalfOf);
        morpho.withdrawCollateral(_marketParams(), amount, onBehalfOf, to);
    }

    function borrow(uint256 amount, address onBehalfOf, address to) external {
        _requireAuthorized(onBehalfOf);
        morpho.borrow(_marketParams(), amount, 0, onBehalfOf, to);
    }

    function repay(uint256 amount, address onBehalfOf) external {
        IERC20(debtAsset).safeTransferFrom(msg.sender, address(this), amount);
        IERC20(debtAsset).forceApprove(address(morpho), amount);
        morpho.repay(_marketParams(), amount, 0, onBehalfOf, "");
    }

    function availableLiquidity() external view returns (uint256) {
        Market memory m = morpho.market(marketId);
        if (m.totalSupplyAssets <= m.totalBorrowAssets) return 0;
        return uint256(m.totalSupplyAssets) - uint256(m.totalBorrowAssets);
    }

    function currentDebt(address onBehalfOf) external view returns (uint256) {
        Position memory p = morpho.position(marketId, onBehalfOf);
        Market memory m = morpho.market(marketId);
        return uint256(p.borrowShares).toAssetsUp(m.totalBorrowAssets, m.totalBorrowShares);
    }

    function currentCollateral(address onBehalfOf) external view returns (uint256) {
        Position memory p = morpho.position(marketId, onBehalfOf);
        return uint256(p.collateral);
    }

    function _requireAuthorized(address onBehalfOf) internal view {
        if (onBehalfOf != address(this) && !morpho.isAuthorized(onBehalfOf, address(this))) {
            revert MBLA_NotAuthorized();
        }
    }

    function _marketParams() internal view returns (MarketParams memory) {
        return
            MarketParams({loanToken: debtAsset, collateralToken: collateralAsset, oracle: oracle, irm: irm, lltv: lltv});
    }
}
