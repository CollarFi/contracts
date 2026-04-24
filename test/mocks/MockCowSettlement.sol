// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {IERC1271} from "@openzeppelin/contracts/interfaces/IERC1271.sol";

contract MockCowSettlement {
    using SafeERC20 for IERC20;

    bytes4 internal constant ERC1271_MAGIC_VALUE = 0x1626ba7e;

    mapping(bytes32 => uint256) private _filledAmounts;

    function filledAmount(bytes calldata orderUid) external view returns (uint256) {
        return _filledAmounts[keccak256(orderUid)];
    }

    function recordFill(
        address owner,
        address buyToken,
        uint256 buyAmount,
        bytes32 digest,
        bytes calldata orderUid,
        uint256 sellAmount
    ) external {
        require(IERC1271(owner).isValidSignature(digest, "") == ERC1271_MAGIC_VALUE, "invalid order signature");

        IERC20(buyToken).safeTransfer(owner, buyAmount);
        _filledAmounts[keccak256(orderUid)] = sellAmount;
    }
}
