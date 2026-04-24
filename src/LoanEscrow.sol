// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";

import {ILendingAdapter} from "./interfaces/ILendingAdapter.sol";
import {IVariableLoanPosition} from "./interfaces/IVariableLoanPosition.sol";

contract LoanEscrow {
    using SafeERC20 for IERC20;

    error LE_AlreadyInitialized();
    error LE_NotVault();
    error LE_InvalidConfig();

    bytes4 internal constant ERC1271_MAGIC_VALUE = 0x1626ba7e;
    bytes4 internal constant ERC1271_INVALID_VALUE = 0xffffffff;

    bool public initialized;
    uint256 public loanId;
    address public vault;
    address public borrower;
    address public collateralAsset;
    address public debtAsset;
    address public cowSettlement;
    address public cowVaultRelayer;
    ILendingAdapter public lendingAdapter;
    address public variableLoanPositionImplementation;
    address public variableLoanPosition;
    bytes32 public activeSettlementOrderDigest;
    uint256 public activeSettlementSellAmount;
    bytes private _activeSettlementOrderUid;

    modifier onlyVault() {
        if (msg.sender != vault) revert LE_NotVault();
        _;
    }

    constructor() {
        initialized = true;
    }

    function initialize(
        uint256 loanId_,
        address vault_,
        address borrower_,
        address collateralAsset_,
        address debtAsset_,
        address lendingAdapter_,
        address variableLoanPositionImplementation_,
        address cowSettlement_,
        address cowVaultRelayer_
    ) external {
        if (initialized) revert LE_AlreadyInitialized();
        if (
            vault_ == address(0) || borrower_ == address(0) || collateralAsset_ == address(0)
                || debtAsset_ == address(0) || lendingAdapter_ == address(0)
                || variableLoanPositionImplementation_ == address(0)
        ) revert LE_InvalidConfig();

        initialized = true;
        loanId = loanId_;
        vault = vault_;
        borrower = borrower_;
        collateralAsset = collateralAsset_;
        debtAsset = debtAsset_;
        cowSettlement = cowSettlement_;
        cowVaultRelayer = cowVaultRelayer_;
        lendingAdapter = ILendingAdapter(lendingAdapter_);
        variableLoanPositionImplementation = variableLoanPositionImplementation_;
    }

    function activeSettlementOrderUid() external view returns (bytes memory) {
        return _activeSettlementOrderUid;
    }

    function availableLiquidity() external view returns (uint256) {
        address position = variableLoanPosition;
        if (position == address(0)) return lendingAdapter.availableLiquidity();
        return IVariableLoanPosition(position).availableLiquidity();
    }

    function openVariablePosition(uint256 collateralAmount, uint256 debtAmount, address debtReceiver)
        external
        onlyVault
        returns (address position, uint256 liveDebt, uint256 liveCollateral)
    {
        position = variableLoanPosition;
        if (position == address(0)) {
            position = Clones.clone(variableLoanPositionImplementation);
            IVariableLoanPosition(position)
                .initialize(address(this), address(lendingAdapter), borrower, collateralAsset, debtAsset);
            variableLoanPosition = position;
        }

        IERC20(collateralAsset).forceApprove(position, collateralAmount);
        IVariableLoanPosition(position).open(collateralAmount, debtAmount, debtReceiver, address(this));

        liveDebt = IVariableLoanPosition(position).currentDebt();
        liveCollateral = IVariableLoanPosition(position).currentCollateral();
    }

    function repayVariableDebt(uint256 amount) external onlyVault returns (uint256 liveDebt, uint256 liveCollateral) {
        address position = variableLoanPosition;
        if (position == address(0)) revert LE_InvalidConfig();

        IERC20(debtAsset).forceApprove(position, amount);
        IVariableLoanPosition(position).repay(amount, address(this));

        liveDebt = IVariableLoanPosition(position).currentDebt();
        liveCollateral = IVariableLoanPosition(position).currentCollateral();
    }

    function withdrawCollateral(uint256 amount, address to)
        external
        onlyVault
        returns (uint256 liveDebt, uint256 liveCollateral)
    {
        address position = variableLoanPosition;
        if (position == address(0)) revert LE_InvalidConfig();

        IVariableLoanPosition(position).withdraw(amount, to);

        liveDebt = IVariableLoanPosition(position).currentDebt();
        liveCollateral = IVariableLoanPosition(position).currentCollateral();
    }

    function approveSettlementOrder(bytes32 digest, bytes calldata orderUid, uint256 sellAmount) external onlyVault {
        if (cowSettlement == address(0) || cowVaultRelayer == address(0)) revert LE_InvalidConfig();

        activeSettlementOrderDigest = digest;
        activeSettlementSellAmount = sellAmount;
        _activeSettlementOrderUid = orderUid;
        IERC20(collateralAsset).forceApprove(cowVaultRelayer, sellAmount);
    }

    function clearSettlementOrder() external onlyVault {
        if (cowVaultRelayer != address(0)) {
            IERC20(collateralAsset).forceApprove(cowVaultRelayer, 0);
        }
        delete activeSettlementOrderDigest;
        delete activeSettlementSellAmount;
        delete _activeSettlementOrderUid;
    }

    function transferToken(address token, address to, uint256 amount) external onlyVault {
        IERC20(token).safeTransfer(to, amount);
    }

    function isValidSignature(bytes32 hash, bytes calldata) external view returns (bytes4) {
        return hash == activeSettlementOrderDigest ? ERC1271_MAGIC_VALUE : ERC1271_INVALID_VALUE;
    }
}
