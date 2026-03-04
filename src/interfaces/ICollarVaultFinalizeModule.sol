// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface ICollarVaultFinalizeModule {
    struct BaselineRfq {
        uint256 loanId;
        address collateralAsset;
        uint256 collateralAmount;
        uint64 maturity;
        uint256 putStrike;
        uint256 callStrike;
        uint256 borrowAmount;
        uint256 minNetInterest;
        uint64 rfqExpiry;
        address borrower;
        uint256 nonce;
    }

    function acceptMandate(uint256 loanId, BaselineRfq calldata rfq, bytes calldata rfqSig, uint64 deadline)
        external
        payable
        returns (bytes32 lzGuid);

    /// @notice Accepts a mandate for the current msg.sender (for internal use via delegatecall).
    /// @dev Called by CollarVault to atomically accept mandate after deposit creation.
    /// If rfq.loanId == 0, it is treated as a sentinel and replaced with the provided loanId.
    function acceptMandateInternal(
        uint256 loanId,
        BaselineRfq calldata rfq,
        bytes calldata rfqSig,
        uint64 deadline,
        uint256 ethForLz
    ) external payable returns (bytes32 lzGuid);

    function finalizeLoan(uint256 loanId, bytes32 depositGuid, bytes32 tradeGuid)
        external
        returns (uint256 finalizedLoanId);
}
