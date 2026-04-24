// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

import {MockCowSettlement} from "./MockCowSettlement.sol";

contract MockCowVaultRelayer {
    using SafeERC20 for IERC20;

    function fillOrder(
        address settlement,
        address sellToken,
        address buyToken,
        address owner,
        uint256 sellAmount,
        uint256 buyAmount,
        bytes32 digest,
        bytes calldata orderUid
    ) external {
        IERC20(sellToken).safeTransferFrom(owner, settlement, sellAmount);
        MockCowSettlement(settlement).recordFill(owner, buyToken, buyAmount, digest, orderUid, sellAmount);
    }
}
