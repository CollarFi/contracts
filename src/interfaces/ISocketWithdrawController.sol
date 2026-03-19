// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface ISocketWithdrawController {
    function withdrawFromAppChain(address receiver_, uint256 burnAmount_, uint256 msgGasLimit_, address connector_)
        external
        payable;

    function getMinFees(address connector_, uint256 msgGasLimit_) external view returns (uint256 totalFees);
}
