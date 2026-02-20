// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ILendingAdapter} from "./ILendingAdapter.sol";

/// @dev Backward-compatible alias. Prefer ILendingAdapter.
interface IEulerAdapter is ILendingAdapter {}
