// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IAllowanceTransfer} from "permit2/src/interfaces/IAllowanceTransfer.sol";

import {IEulerAdapter} from "../interfaces/IEulerAdapter.sol";
import {IBridgeAdapter} from "../interfaces/IBridgeAdapter.sol";
import {ICollarVaultMessenger} from "../interfaces/ICollarVaultMessenger.sol";
import {ILiquidityVault} from "../interfaces/ILiquidityVault.sol";

library CollarVaultShared {
    uint256 internal constant YEAR = 365 days;
    uint256 internal constant MAX_BPS = 10_000;

    enum LoanState {
        NONE,
        ACTIVE_ZERO_COST,
        CLOSED
    }

    enum SettlementOutcome {
        PutITM,
        Neutral,
        CallITM
    }

    struct Loan {
        address borrower;
        address collateralAsset;
        uint256 collateralAmount;
        uint256 maturity;
        uint256 putStrike;
        uint256 callStrike;
        uint256 principal;
        uint256 subaccountId;
        LoanState state;
        uint256 startTime;
        uint256 originationFeeApr;
        uint256 variableDebt;
    }

    struct PendingDeposit {
        address borrower;
        address collateralAsset;
        uint256 collateralAmount;
        uint256 maturity;
        uint256 putStrike;
        uint256 borrowAmount;
    }

    struct SocketBridgeConfig {
        IBridgeAdapter adapter;
    }

    struct Mandate {
        address borrower;
        address collateralAsset;
        uint256 collateralAmount;
        uint64 maturity;
        uint64 deadline;
        uint256 borrowAmount;
        uint256 minCallStrike;
        uint256 maxPutStrike;
        bool sentToL2;
    }

    struct CollarVaultStorage {
        ILiquidityVault liquidityVault;
        IERC20 usdc;
        IAllowanceTransfer permit2;
        mapping(address => SocketBridgeConfig) socketBridgeConfigs;
        IEulerAdapter eulerAdapter;
        address l2Recipient;
        address treasury;
        uint256 treasuryBps;
        uint256 originationFeeApr;
        uint256 maxTotalPrincipal;
        uint256 totalCommittedPrincipal;
        uint256 deriveSubaccountId;
        uint256 nextLoanId;
        mapping(uint256 => Loan) loans;
        mapping(uint256 => PendingDeposit) pendingDeposits;
        mapping(uint256 => Mandate) mandates;
        mapping(bytes32 => bool) usedBaselineRfqs;
        mapping(uint256 => bool) tradeConfirmed;
        mapping(uint256 => bool) collateralActivated;
        mapping(uint256 => bool) returnRequested;
        mapping(address => bool) collateralAllowed;
        mapping(address => uint256) strikeScale;
        ICollarVaultMessenger lzMessenger;
        mapping(bytes32 => bool) lzMessageConsumed;
        address finalizeModule;
        address settleModule;
    }

    // keccak256(abi.encode(uint256(keccak256("collar.storage.CollarVault")) - 1)) & ~bytes32(uint256(0xff))
    bytes32 internal constant COLLAR_VAULT_STORAGE_LOCATION =
        0x44df88ba167ccae38168bf10e759327f11cfe194bbb6b4faf1c1a932243f4100;

    function getStorage() internal pure returns (CollarVaultStorage storage $) {
        assembly {
            $.slot := COLLAR_VAULT_STORAGE_LOCATION
        }
    }
}
