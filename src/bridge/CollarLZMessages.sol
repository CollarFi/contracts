// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

library CollarLZMessages {
    enum Action {
        DepositIntent,
        ReturnRequest,
        SettlementReport,
        DepositConfirmed,
        CollateralReturned,
        TradeConfirmed,
        MandateCreated
    }

    /// @dev Generic LZ message envelope. Different actions interpret fields differently.
    ///
    /// For MandateCreated (L1 -> L2):
    /// - loanId: identifies the loan/mandate
    /// - asset: collateral asset
    /// - amount: borrowAmount (mandate size)
    /// - data: abi.encode(borrower, minCallStrike, maxPutStrike, maturity, deadline)
    struct Message {
        Action action;
        uint256 loanId;
        address asset;
        uint256 amount;
        address recipient;
        uint256 subaccountId;
        bytes32 socketMessageId;
        uint256 secondaryAmount;
        bytes32 quoteHash;
        uint256 takerNonce;
        bytes data;
    }
}
