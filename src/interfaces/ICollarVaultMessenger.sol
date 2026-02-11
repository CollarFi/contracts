// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {CollarLZMessages} from "../bridge/CollarLZMessages.sol";
import {
    MessagingFee,
    MessagingReceipt
} from "@layerzerolabs/lz-evm-protocol-v2/contracts/interfaces/ILayerZeroEndpointV2.sol";

interface ICollarVaultMessenger {
    function defaultOptions() external view returns (bytes memory);

    function quoteMessage(CollarLZMessages.Message calldata message, bytes calldata options)
        external
        view
        returns (MessagingFee memory fee);

    function sendMessage(CollarLZMessages.Message calldata message)
        external
        payable
        returns (MessagingReceipt memory receipt);

    /// @notice Quote then send a message, refunding any excess native fee to `refundTo`.
    function sendMessageAutoFee(CollarLZMessages.Message calldata message, address refundTo)
        external
        payable
        returns (bytes32 guid);

    function sendDepositIntentAutoFee(
        uint256 loanId,
        address asset,
        uint256 amount,
        address recipient,
        uint256 subaccountId,
        bytes32 socketMessageId,
        address refundTo
    ) external payable returns (bytes32 guid);

    function sendMandateCreatedAutoFee(
        uint256 loanId,
        address asset,
        uint256 borrowAmount,
        address recipient,
        uint256 subaccountId,
        bytes calldata mandateData,
        address refundTo
    ) external payable returns (bytes32 guid);

    function sendReturnRequestAutoFee(
        uint256 loanId,
        address asset,
        uint256 amount,
        address recipient,
        uint256 subaccountId,
        address refundTo
    ) external payable returns (bytes32 guid);

    function validateDepositConfirmed(
        CollarLZMessages.Message calldata lzMessage,
        address pendingBorrower,
        address expectedBorrower,
        address pendingCollateralAsset,
        uint256 pendingCollateralAmount,
        address expectedRecipient,
        uint256 expectedSubaccountId
    ) external pure returns (uint256 loanId);

    function validateTradeConfirmedForFinalize(
        CollarLZMessages.Message calldata tradeMessage,
        uint256 expectedLoanId,
        address expectedRecipient,
        uint256 expectedSubaccountId,
        uint256 minCallStrike,
        uint256 maxPutStrike,
        uint64 expectedMaturity
    ) external pure returns (uint256 callStrike, uint256 putStrike);

    function validateTradeConfirmedMarker(
        CollarLZMessages.Message calldata lzMessage,
        address expectedRecipient,
        uint256 expectedSubaccountId
    ) external pure returns (uint256 loanId);

    function validateCollateralReturned(
        CollarLZMessages.Message calldata lzMessage,
        uint256 loanId,
        address collateralAsset,
        uint256 collateralAmount,
        address expectedRecipient,
        uint256 expectedSubaccountId
    ) external pure;

    function validateSettlementReport(
        CollarLZMessages.Message calldata lzMessage,
        uint256 loanId,
        address usdcAsset,
        address expectedRecipient
    ) external pure returns (uint256 settlementAmount);

    function validateOriginationFee(CollarLZMessages.Message calldata lzMessage, uint256 feeAmount, address usdcAsset)
        external
        pure;

    function receivedMessage(bytes32 guid) external view returns (CollarLZMessages.Message memory message);
}
