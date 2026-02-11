// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IOptionRiskVerifier {
    struct ValidateCallParams {
        address manager;
        address optionAsset;
        uint256 expiry;
        uint256 strike;
        uint256 limitPrice;
        uint256 optionVolSlippageFactor;
        uint256 callMaxDelta;
        uint256 optionMinTimeToExpiry;
        uint256 optionMaxTimeToExpiry;
    }

    struct ValidatePutParams {
        address manager;
        address optionAsset;
        uint256 expiry;
        uint256 strike;
        uint256 limitPrice;
        uint256 optionVolSlippageFactor;
        uint256 putMaxPriceFactor;
        uint256 optionMinTimeToExpiry;
        uint256 optionMaxTimeToExpiry;
    }

    function validateCall(ValidateCallParams calldata params) external view;
    function validatePut(ValidatePutParams calldata params) external view;
}
