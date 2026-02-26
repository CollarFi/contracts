// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {AccessControlUpgradeable} from "openzeppelin-upgradeable/access/AccessControlUpgradeable.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {PausableUpgradeable} from "openzeppelin-upgradeable/utils/PausableUpgradeable.sol";
import {ReentrancyGuardUpgradeable} from "openzeppelin-upgradeable/utils/ReentrancyGuardUpgradeable.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {EIP712Upgradeable} from "openzeppelin-upgradeable/utils/cryptography/EIP712Upgradeable.sol";
import {Initializable} from "openzeppelin-upgradeable/proxy/utils/Initializable.sol";
import {IAllowanceTransfer} from "permit2/src/interfaces/IAllowanceTransfer.sol";

import {ILendingAdapter} from "./interfaces/ILendingAdapter.sol";
import {IBridgeAdapter} from "./interfaces/IBridgeAdapter.sol";
import {CollarLZMessages} from "./bridge/CollarLZMessages.sol";
import {ICollarVaultMessenger} from "./interfaces/ICollarVaultMessenger.sol";
import {ICollarVaultFinalizeModule} from "./interfaces/ICollarVaultFinalizeModule.sol";
import {ICollarVaultSettleModule} from "./interfaces/ICollarVaultSettleModule.sol";
import {ICollarVaultRolloverModule} from "./interfaces/ICollarVaultRolloverModule.sol";
import {ILiquidityVault} from "./interfaces/ILiquidityVault.sol";
import {CollarVaultShared} from "./modules/CollarVaultShared.sol";

contract CollarVault is
    Initializable,
    AccessControlUpgradeable,
    PausableUpgradeable,
    ReentrancyGuardUpgradeable,
    EIP712Upgradeable
{
    using SafeERC20 for IERC20;

    bytes32 public constant KEEPER_ROLE = keccak256("KEEPER_ROLE");
    bytes32 public constant PARAMETER_ROLE = keccak256("PARAMETER_ROLE");
    bytes32 public constant PAUSER_ROLE = keccak256("PAUSER_ROLE");
    bytes32 public constant RFQ_SIGNER_ROLE = keccak256("RFQ_SIGNER_ROLE");
    uint256 public constant YEAR = CollarVaultShared.YEAR;
    uint256 public constant MAX_BPS = CollarVaultShared.MAX_BPS;

    struct DepositParams {
        address collateralAsset;
        uint256 collateralAmount;
        uint256 maturity;
        uint256 putStrike;
        uint256 borrowAmount;
    }

    // Quote-based RFQ flow has been removed; loans are now created via keeper-signed RFQ baseline + mandate + L2 TradeConfirmed.

    bytes32 public constant BASELINE_RFQ_TYPEHASH = keccak256(
        "BaselineRfq(uint256 loanId,address collateralAsset,uint256 collateralAmount,uint64 maturity,uint256 putStrike,uint256 callStrike,uint256 borrowAmount,uint256 minNetInterest,uint256 maxNegativeC,uint64 rfqExpiry,address borrower,uint256 nonce)"
    );

    bytes32 public constant ROLLOVER_MANDATE_TYPEHASH = keccak256(
        "RolloverMandate(address borrower,uint256 loanId,uint64 newMaturity,uint256 minCallStrike,uint256 maxPutStrike,uint256 minNetInterest,uint256 maxNegativeC,uint64 deadline,uint256 nonce)"
    );

    function _getCollarVaultStorage() private pure returns (CollarVaultShared.CollarVaultStorage storage $) {
        return CollarVaultShared.getStorage();
    }

    // Quote-based RFQ flow removed: no quote replay tracking.
    error CV_InvalidConfig();
    error CV_InvalidInput();
    error CV_InvalidState();
    error CV_Unauthorized();
    error CV_NotFound();
    error CV_InvalidMessage();
    error CV_InsufficientValue();

    event LoanCreated(
        uint256 indexed loanId,
        address indexed borrower,
        address indexed collateralAsset,
        uint256 collateralAmount,
        uint256 maturity,
        uint256 putStrike,
        uint256 callStrike,
        uint256 principal,
        uint256 subaccountId
    );
    event LoanSettled(uint256 indexed loanId, CollarVaultShared.SettlementOutcome outcome, uint256 settlementAmount);
    event SettlementShortfall(uint256 indexed loanId, uint256 shortfall);
    event LoanReadyForVariable(uint256 indexed loanId, uint256 requiredDebt);
    event LoanConverted(uint256 indexed loanId, uint256 variableDebt);
    event LoanClosed(uint256 indexed loanId);
    event TreasuryUpdated(address indexed treasury, uint256 bps);
    event OriginationFeeAprUpdated(uint256 feeApr);
    event MaxTotalPrincipalUpdated(uint256 maxTotalPrincipal);
    event MaxMandateDurationUpdated(uint64 maxMandateDuration);
    event CollateralConfigUpdated(
        address indexed asset, bool allowed, uint256 strikeScale, address indexed l2MessageAsset
    );
    event BridgeConfigUpdated(address indexed asset, address indexed adapter);
    event L2RecipientUpdated(address indexed recipient);
    event LendingAdapterUpdated(address indexed adapter);
    event VariableLoanPositionImplementationUpdated(address indexed implementation);
    event SubaccountUpdated(uint256 subaccountId);
    event RfqSignerUpdated(address indexed signer, bool allowed);
    event LZMessengerUpdated(address indexed messenger);
    event FinalizeModuleUpdated(address indexed module);
    event SettleModuleUpdated(address indexed module);
    event RolloverModuleUpdated(address indexed module);
    event CollateralDepositRequested(
        uint256 indexed loanId,
        address indexed borrower,
        address indexed collateralAsset,
        uint256 collateralAmount,
        uint256 maturity,
        bytes32 socketMessageId,
        bytes32 lzGuid
    );
    event CollateralReturnRequested(
        uint256 indexed loanId,
        address indexed requester,
        address indexed collateralAsset,
        uint256 collateralAmount,
        bytes32 lzGuid
    );
    event CollateralDepositReturned(
        uint256 indexed loanId, address indexed borrower, address indexed collateralAsset, uint256 collateralAmount
    );
    event TradeConfirmedRecorded(uint256 indexed loanId, bytes32 guid);
    // LoanRolledOver is emitted by CollarVaultRolloverModule.

    constructor() {
        _disableInitializers();
    }

    function initialize(
        address admin,
        ILiquidityVault liquidityVault_,
        ILendingAdapter lendingAdapter_,
        IAllowanceTransfer permit2_,
        address l2Recipient_,
        address treasury_
    ) external initializer {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        __AccessControl_init();
        __Pausable_init();
        __ReentrancyGuard_init();
        __EIP712_init("CollarVault", "1");

        if (
            admin == address(0) || address(liquidityVault_) == address(0) || address(lendingAdapter_) == address(0)
                || address(permit2_) == address(0) || l2Recipient_ == address(0) || treasury_ == address(0)
        ) {
            revert CV_InvalidConfig();
        }

        $.liquidityVault = liquidityVault_;
        $.usdc = IERC20(liquidityVault_.asset());
        $.lendingAdapter = lendingAdapter_;
        $.permit2 = permit2_;
        $.l2Recipient = l2Recipient_;
        $.treasury = treasury_;
        $.maxMandateDuration = 30 minutes;
        $.nextLoanId = 1;

        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        _grantRole(PARAMETER_ROLE, admin);
        _grantRole(KEEPER_ROLE, admin);
        _grantRole(PAUSER_ROLE, admin);
    }

    function liquidityVault() external view returns (ILiquidityVault) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        return $.liquidityVault;
    }

    function usdc() external view returns (IERC20) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        return $.usdc;
    }

    function permit2() external view returns (IAllowanceTransfer) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        return $.permit2;
    }

    function lendingAdapter() external view returns (ILendingAdapter) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        return $.lendingAdapter;
    }

    function variableLoanPositionImplementation() external view returns (address) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        return $.variableLoanPositionImplementation;
    }

    function variableLoanPosition(uint256 loanId) external view returns (address) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        return $.variableLoanPositions[loanId];
    }

    function l2Recipient() external view returns (address) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        return $.l2Recipient;
    }

    function treasury() external view returns (address) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        return $.treasury;
    }

    function treasuryBps() external view returns (uint256) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        return $.treasuryBps;
    }

    function originationFeeApr() external view returns (uint256) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        return $.originationFeeApr;
    }

    function maxTotalPrincipal() external view returns (uint256) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        return $.maxTotalPrincipal;
    }

    function totalCommittedPrincipal() external view returns (uint256) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        return $.totalCommittedPrincipal;
    }

    function maxMandateDuration() external view returns (uint64) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        return $.maxMandateDuration;
    }

    function deriveSubaccountId() external view returns (uint256) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        return $.deriveSubaccountId;
    }

    function nextLoanId() external view returns (uint256) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        return $.nextLoanId;
    }

    function loans(uint256 loanId)
        external
        view
        returns (
            address borrower,
            address collateralAsset,
            uint256 collateralAmount,
            uint256 maturity,
            uint256 putStrike,
            uint256 callStrike,
            uint256 principal,
            uint256 subaccountId,
            CollarVaultShared.LoanState state,
            uint256 startTime,
            uint256 loanInterestApr,
            uint256 interestOwed,
            uint256 variableDebt
        )
    {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        CollarVaultShared.Loan storage loan = $.loans[loanId];
        return (
            loan.borrower,
            loan.collateralAsset,
            loan.collateralAmount,
            loan.maturity,
            loan.putStrike,
            loan.callStrike,
            loan.principal,
            loan.subaccountId,
            loan.state,
            loan.startTime,
            loan.interestApr,
            loan.interestOwed,
            loan.variableDebt
        );
    }

    function pendingDeposits(uint256 loanId)
        external
        view
        returns (
            address borrower,
            address collateralAsset,
            uint256 collateralAmount,
            uint256 maturity,
            uint256 putStrike,
            uint256 borrowAmount
        )
    {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        CollarVaultShared.PendingDeposit storage pending = $.pendingDeposits[loanId];
        return (
            pending.borrower,
            pending.collateralAsset,
            pending.collateralAmount,
            pending.maturity,
            pending.putStrike,
            pending.borrowAmount
        );
    }

    function mandates(uint256 loanId)
        external
        view
        returns (
            address borrower,
            address collateralAsset,
            uint256 collateralAmount,
            uint64 maturity,
            uint64 deadline,
            uint256 borrowAmount,
            uint256 minCallStrike,
            uint256 maxPutStrike,
            uint256 minNetInterest,
            bool sentToL2
        )
    {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        CollarVaultShared.Mandate storage mandate = $.mandates[loanId];
        return (
            mandate.borrower,
            mandate.collateralAsset,
            mandate.collateralAmount,
            mandate.maturity,
            mandate.deadline,
            mandate.borrowAmount,
            mandate.minCallStrike,
            mandate.maxPutStrike,
            mandate.minNetInterest,
            mandate.sentToL2
        );
    }

    function usedBaselineRfqs(bytes32 rfqHash) external view returns (bool) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        return $.usedBaselineRfqs[rfqHash];
    }

    // pendingRollovers getter intentionally omitted to keep runtime size under EIP-170.

    function tradeConfirmed(uint256 loanId) external view returns (bool) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        return $.tradeConfirmed[loanId];
    }

    function collateralActivated(uint256 loanId) external view returns (bool) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        return $.collateralActivated[loanId];
    }

    function returnRequested(uint256 loanId) external view returns (bool) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        return $.returnRequested[loanId];
    }

    function collateralAllowed(address asset) external view returns (bool) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        return $.collateralAllowed[asset];
    }

    function strikeScale(address asset) external view returns (uint256) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        return $.strikeScale[asset];
    }

    function l2MessageAsset(address l1Asset) external view returns (address) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        return $.l2MessageAsset[l1Asset];
    }

    function lzMessenger() external view returns (ICollarVaultMessenger) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        return $.lzMessenger;
    }

    function finalizeModule() external view returns (address) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        return $.finalizeModule;
    }

    function settleModule() external view returns (address) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        return $.settleModule;
    }

    function lzMessageConsumed(bytes32 guid) external view returns (bool) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        return $.lzMessageConsumed[guid];
    }

    /// @notice Request a collateral deposit via Permit2 and send a deposit intent to L2.
    function createDepositWithPermit(
        DepositParams calldata params,
        IAllowanceTransfer.PermitSingle calldata permit,
        bytes calldata permitSig
    ) external payable nonReentrant whenNotPaused returns (uint256 loanId, bytes32 socketMessageId, bytes32 lzGuid) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        if (!$.collateralAllowed[params.collateralAsset]) {
            revert CV_InvalidConfig();
        }
        if (params.collateralAmount == 0) {
            revert CV_InvalidInput();
        }
        if (params.maturity <= block.timestamp) {
            revert CV_InvalidInput();
        }
        _validateBorrowAmount(params.collateralAsset, params.collateralAmount, params.putStrike, params.borrowAmount);
        _validatePermit(params.collateralAsset, params.collateralAmount, permit);
        if (params.collateralAmount > type(uint160).max) {
            revert CV_InvalidInput();
        }

        $.permit2.permit(msg.sender, permit, permitSig);
        $.permit2.transferFrom(msg.sender, address(this), uint160(params.collateralAmount), params.collateralAsset);

        (loanId, socketMessageId, lzGuid) = _requestCollateralDeposit(msg.sender, params);
    }

    // (removed) acceptQuote: quote-based RFQ flow replaced by keeper-signed RFQ baseline + acceptMandate.

    function hashBaselineRfq(ICollarVaultFinalizeModule.BaselineRfq memory rfq) public view returns (bytes32) {
        bytes32 structHash = keccak256(
            abi.encode(
                BASELINE_RFQ_TYPEHASH,
                rfq.loanId,
                rfq.collateralAsset,
                rfq.collateralAmount,
                rfq.maturity,
                rfq.putStrike,
                rfq.callStrike,
                rfq.borrowAmount,
                rfq.minNetInterest,
                rfq.maxNegativeC,
                rfq.rfqExpiry,
                rfq.borrower,
                rfq.nonce
            )
        );
        return _hashTypedDataV4(structHash);
    }

    function hashRolloverMandate(CollarVaultShared.RolloverMandate memory mandate) external view returns (bytes32) {
        bytes32 structHash = keccak256(
            abi.encode(
                ROLLOVER_MANDATE_TYPEHASH,
                mandate.borrower,
                mandate.loanId,
                mandate.newMaturity,
                mandate.minCallStrike,
                mandate.maxPutStrike,
                mandate.minNetInterest,
                mandate.maxNegativeC,
                mandate.deadline,
                mandate.nonce
            )
        );
        return _hashTypedDataV4(structHash);
    }

    /// @notice Accept a mandate on L1, constrained by a keeper-signed RFQ baseline.
    /// @dev Mandates must be mirrored L1->L2 via LayerZero since the TSA lives on a different network.
    /// @param deadline Timestamp after which the borrower can request collateral return.
    function acceptMandate(
        uint256 loanId,
        ICollarVaultFinalizeModule.BaselineRfq calldata rfq,
        bytes calldata rfqSig,
        uint64 deadline
    ) external payable nonReentrant whenNotPaused returns (bytes32 lzGuid) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        address module = $.finalizeModule;
        if (module == address(0)) {
            revert CV_InvalidConfig();
        }
        bytes memory ret = _delegateTo(
            module, abi.encodeCall(ICollarVaultFinalizeModule.acceptMandate, (loanId, rfq, rfqSig, deadline))
        );
        lzGuid = abi.decode(ret, (bytes32));
    }

    /// @notice Finalize a loan once deposit and RFQ trades have been confirmed on L2.
    function finalizeLoan(uint256 loanId, bytes32 depositGuid, bytes32 tradeGuid)
        external
        nonReentrant
        whenNotPaused
        onlyKeeper
        returns (uint256 finalizedLoanId)
    {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        address module = $.finalizeModule;
        if (module == address(0)) {
            revert CV_InvalidConfig();
        }
        bytes memory ret = _delegateTo(
            module, abi.encodeCall(ICollarVaultFinalizeModule.finalizeLoan, (loanId, depositGuid, tradeGuid))
        );
        finalizedLoanId = abi.decode(ret, (uint256));
    }

    /// @notice Request return of a pending collateral deposit before activation/trade.
    function requestCollateralReturn(uint256 loanId)
        external
        payable
        nonReentrant
        whenNotPaused
        returns (bytes32 lzGuid)
    {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        CollarVaultShared.PendingDeposit storage pending = $.pendingDeposits[loanId];
        if (pending.borrower == address(0)) {
            revert CV_NotFound();
        }
        if (pending.borrower != msg.sender) {
            revert CV_Unauthorized();
        }
        if ($.returnRequested[loanId]) {
            revert CV_InvalidState();
        }
        if ($.tradeConfirmed[loanId] || $.collateralActivated[loanId]) {
            revert CV_InvalidState();
        }
        CollarVaultShared.Mandate memory mandate = $.mandates[loanId];
        if (mandate.borrower != address(0) && block.timestamp < mandate.deadline) {
            revert CV_InvalidState();
        }
        if ($.loans[loanId].state != CollarVaultShared.LoanState.NONE) {
            revert CV_InvalidState();
        }
        if (address($.lzMessenger) == address(0)) {
            revert CV_InvalidConfig();
        }
        if ($.deriveSubaccountId == 0) {
            revert CV_InvalidConfig();
        }

        lzGuid = $.lzMessenger.sendReturnRequestAutoFee{value: msg.value}(
            loanId, pending.collateralAsset, pending.collateralAmount, address(this), $.deriveSubaccountId, msg.sender
        );
        $.returnRequested[loanId] = true;

        emit CollateralReturnRequested(loanId, msg.sender, pending.collateralAsset, pending.collateralAmount, lzGuid);
    }

    /// @notice Finalize a returned deposit and transfer collateral to the borrower.
    function finalizeDepositReturn(uint256 loanId, bytes32 lzGuid) external nonReentrant whenNotPaused {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        CollarLZMessages.Message memory lzMessage = _consumeLZMessage(lzGuid);

        CollarVaultShared.PendingDeposit memory pending = $.pendingDeposits[loanId];
        if (pending.borrower == address(0)) {
            revert CV_NotFound();
        }
        if ($.tradeConfirmed[loanId]) {
            revert CV_InvalidState();
        }
        $.lzMessenger
            .validateCollateralReturned(
                lzMessage,
                loanId,
                pending.collateralAsset,
                pending.collateralAmount,
                address(this),
                $.deriveSubaccountId
            );
        if ($.loans[loanId].state != CollarVaultShared.LoanState.NONE) {
            revert CV_InvalidState();
        }

        delete $.pendingDeposits[loanId];

        CollarVaultShared.Mandate memory mandate = $.mandates[loanId];
        if (mandate.borrower != address(0)) {
            _releaseCommittedPrincipal(mandate.borrowAmount);
            $.liquidityVault.releasePrincipal(loanId);
            if (mandate.maxNegativeC > 0) {
                $.liquidityVault.release(loanId);
            }
            delete $.mandates[loanId];
        }

        $.returnRequested[loanId] = false;
        IERC20(pending.collateralAsset).safeTransfer(pending.borrower, pending.collateralAmount);
        emit CollateralDepositReturned(loanId, pending.borrower, pending.collateralAsset, pending.collateralAmount);
    }

    /// @notice Settle a matured loan into one of the three collar outcomes.
    function settleLoan(uint256 loanId, CollarVaultShared.SettlementOutcome outcome, bytes32 lzGuid)
        external
        nonReentrant
        whenNotPaused
        onlyKeeper
    {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        address module = $.settleModule;
        if (module == address(0)) {
            revert CV_InvalidConfig();
        }
        _delegateTo(module, abi.encodeCall(ICollarVaultSettleModule.settleLoan, (loanId, uint8(outcome), lzGuid)));
    }

    /// @notice Execute a pre-maturity rollover for an active loan using a borrower-signed mandate.
    function executeRollover(
        uint256 loanId,
        CollarVaultShared.RolloverMandate calldata mandate,
        bytes calldata mandateSig,
        uint256 newCallStrike,
        uint256 newPutStrike
    ) external payable nonReentrant whenNotPaused onlyKeeper returns (bytes32 lzGuid) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        address module = $.rolloverModule;
        if (module == address(0)) {
            revert CV_InvalidConfig();
        }
        bytes memory ret = _delegateTo(
            module,
            abi.encodeCall(
                ICollarVaultRolloverModule.executeRollover, (loanId, mandate, mandateSig, newCallStrike, newPutStrike)
            )
        );
        lzGuid = abi.decode(ret, (bytes32));
    }

    function finalizeRollover(uint256 loanId, bytes32 confirmationGuid) external nonReentrant whenNotPaused onlyKeeper {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        address module = $.rolloverModule;
        if (module == address(0)) {
            revert CV_InvalidConfig();
        }
        _delegateTo(module, abi.encodeCall(ICollarVaultRolloverModule.finalizeRollover, (loanId, confirmationGuid)));
    }

    /// @notice Keeper-triggered retry to convert a READY_FOR_VARIABLE loan once adapter liquidity is sufficient.
    function tryConvertReadyLoan(uint256 loanId)
        external
        nonReentrant
        whenNotPaused
        onlyKeeper
        returns (bool converted)
    {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        address module = $.settleModule;
        if (module == address(0)) {
            revert CV_InvalidConfig();
        }
        bytes memory ret = _delegateTo(module, abi.encodeCall(ICollarVaultSettleModule.tryConvertReadyLoan, (loanId)));
        converted = abi.decode(ret, (bool));
    }

    /// @notice Repay an active variable loan via the vault.
    function repayVariableLoan(uint256 loanId, uint256 amount)
        external
        nonReentrant
        whenNotPaused
        returns (uint256 repaid, bool closed)
    {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        CollarVaultShared.Loan storage loan = $.loans[loanId];
        if (msg.sender != loan.borrower && !hasRole(KEEPER_ROLE, msg.sender)) {
            revert CV_Unauthorized();
        }
        if (amount == 0) {
            revert CV_InvalidInput();
        }

        $.usdc.safeTransferFrom(msg.sender, address(this), amount);

        address module = $.settleModule;
        if (module == address(0)) {
            revert CV_InvalidConfig();
        }
        bytes memory ret =
            _delegateTo(module, abi.encodeCall(ICollarVaultSettleModule.repayVariableLoan, (loanId, amount)));
        (repaid, closed) = abi.decode(ret, (uint256, bool));
    }

    /// @notice Withdraw collateral from an active variable loan position via the vault.
    function withdrawVariableCollateral(uint256 loanId, uint256 amount)
        external
        nonReentrant
        whenNotPaused
        returns (uint256 withdrawn, bool closed)
    {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        CollarVaultShared.Loan storage loan = $.loans[loanId];
        if (msg.sender != loan.borrower && !hasRole(KEEPER_ROLE, msg.sender)) {
            revert CV_Unauthorized();
        }
        if (amount == 0) {
            revert CV_InvalidInput();
        }

        address module = $.settleModule;
        if (module == address(0)) {
            revert CV_InvalidConfig();
        }
        bytes memory ret =
            _delegateTo(module, abi.encodeCall(ICollarVaultSettleModule.withdrawVariableCollateral, (loanId, amount)));
        (withdrawn, closed) = abi.decode(ret, (uint256, bool));
    }

    // (removed) hashQuote: quote-based flow removed.

    /// @notice Return a loan record by id.
    function getLoan(uint256 loanId) external view returns (CollarVaultShared.Loan memory loan_) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        return $.loans[loanId];
    }

    /// @notice Return the current interest amount owed on a loan.
    function calculateOriginationFee(uint256 loanId) external view returns (uint256) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        return $.loans[loanId].interestOwed;
    }

    /// @notice Update the L2 recipient for bridge transfers.
    function setL2Recipient(address newL2Recipient) external onlyRole(PARAMETER_ROLE) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        if (newL2Recipient == address(0)) {
            revert CV_InvalidConfig();
        }
        $.l2Recipient = newL2Recipient;
        emit L2RecipientUpdated(newL2Recipient);
    }

    /// @notice Configure Socket bridge settings for an asset using a preconfigured adapter.
    function setSocketVaultConfig(address asset, IBridgeAdapter adapter) external onlyRole(PARAMETER_ROLE) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        if (asset == address(0) || address(adapter) == address(0)) {
            revert CV_InvalidConfig();
        }
        $.socketBridgeConfigs[asset] = CollarVaultShared.SocketBridgeConfig({adapter: adapter});
        emit BridgeConfigUpdated(asset, address(adapter));
    }

    /// @notice Update the lending adapter.
    function setLendingAdapter(ILendingAdapter newAdapter) public onlyRole(PARAMETER_ROLE) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        if (address(newAdapter) == address(0)) {
            revert CV_InvalidConfig();
        }
        $.lendingAdapter = newAdapter;
        emit LendingAdapterUpdated(address(newAdapter));
    }

    function setVariableLoanPositionImplementation(address implementation) external onlyRole(PARAMETER_ROLE) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        if (implementation == address(0)) {
            revert CV_InvalidConfig();
        }
        $.variableLoanPositionImplementation = implementation;
        emit VariableLoanPositionImplementationUpdated(implementation);
    }

    /// @notice Update the Derive subaccount id used for action validation.
    function setDeriveSubaccountId(uint256 subaccountId) external onlyRole(PARAMETER_ROLE) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        if (subaccountId == 0) {
            revert CV_InvalidConfig();
        }
        $.deriveSubaccountId = subaccountId;
        emit SubaccountUpdated(subaccountId);
    }

    /// @notice Update collateral allowlist, strike scale, and L2 message asset mapping.
    /// @dev When `allowed == true`, `l2Asset` must be non-zero.
    function setCollateralConfig(address asset, bool allowed, uint256 scale, address l2Asset)
        external
        onlyRole(PARAMETER_ROLE)
    {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        if (asset == address(0) || (allowed && l2Asset == address(0))) {
            revert CV_InvalidConfig();
        }
        $.collateralAllowed[asset] = allowed;
        $.strikeScale[asset] = scale;
        $.l2MessageAsset[asset] = l2Asset;
        emit CollateralConfigUpdated(asset, allowed, scale, l2Asset);
    }

    /// @notice Estimate the Socket bridge fees for a transfer.
    function estimateBridgeFees(address asset, address, uint256) public view returns (uint256) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        CollarVaultShared.SocketBridgeConfig storage config = $.socketBridgeConfigs[asset];
        if (address(config.adapter) == address(0)) {
            revert CV_InvalidConfig();
        }
        return config.adapter.estimateFee();
    }

    /// @notice Update $.treasury configuration for settlement surplus.
    function setTreasuryConfig(address newTreasury, uint256 bps) external onlyRole(PARAMETER_ROLE) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        if (newTreasury == address(0)) {
            revert CV_InvalidConfig();
        }
        if (bps > MAX_BPS) {
            revert CV_InvalidConfig();
        }
        $.treasury = newTreasury;
        $.treasuryBps = bps;
        emit TreasuryUpdated(newTreasury, bps);
    }

    /// @notice Update the annualized origination fee (1e18 precision).
    function setOriginationFeeApr(uint256 feeApr) external onlyRole(PARAMETER_ROLE) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        $.originationFeeApr = feeApr;
        emit OriginationFeeAprUpdated(feeApr);
    }

    /// @notice Update the maximum total committed principal (0 disables the cap).
    function setMaxTotalPrincipal(uint256 maxPrincipal) external onlyRole(PARAMETER_ROLE) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        $.maxTotalPrincipal = maxPrincipal;
        emit MaxTotalPrincipalUpdated(maxPrincipal);
    }

    /// @notice Update max mandate lifetime in seconds.
    function setMaxMandateDuration(uint64 duration) external onlyRole(PARAMETER_ROLE) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        if (duration == 0) revert CV_InvalidConfig();
        $.maxMandateDuration = duration;
        emit MaxMandateDurationUpdated(duration);
    }

    /// @notice Allow or revoke an RFQ signer.
    function setRfqSigner(address signer, bool allowed) external onlyRole(PARAMETER_ROLE) {
        if (signer == address(0)) {
            revert CV_InvalidConfig();
        }
        if (allowed) {
            _grantRole(RFQ_SIGNER_ROLE, signer);
        } else {
            _revokeRole(RFQ_SIGNER_ROLE, signer);
        }
        emit RfqSignerUpdated(signer, allowed);
    }

    /// @notice Update the LayerZero messenger used to validate L2 acknowledgements.
    function setLZMessenger(ICollarVaultMessenger messenger) external onlyRole(PARAMETER_ROLE) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        if (address(messenger) == address(0)) {
            revert CV_InvalidConfig();
        }
        $.lzMessenger = messenger;
        emit LZMessengerUpdated(address(messenger));
    }

    function setFinalizeModule(address module) external onlyRole(PARAMETER_ROLE) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        if (module == address(0)) {
            revert CV_InvalidConfig();
        }
        $.finalizeModule = module;
        emit FinalizeModuleUpdated(module);
    }

    function setSettleModule(address module) external onlyRole(PARAMETER_ROLE) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        if (module == address(0)) {
            revert CV_InvalidConfig();
        }
        $.settleModule = module;
        emit SettleModuleUpdated(module);
    }

    function setRolloverModule(address module) external onlyRole(PARAMETER_ROLE) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        if (module == address(0)) {
            revert CV_InvalidConfig();
        }
        $.rolloverModule = module;
        emit RolloverModuleUpdated(module);
    }

    /// @notice Pause loan creation and settlement.
    function pause() external onlyRole(PAUSER_ROLE) {
        _pause();
    }

    /// @notice Unpause loan creation and settlement.
    function unpause() external onlyRole(PAUSER_ROLE) {
        _unpause();
    }

    // (removed) _validateQuote / _validateBorrowAmount(Quote): quote-based flow removed.

    function _validateBorrowAmount(
        address collateralAsset,
        uint256 collateralAmount,
        uint256 putStrike,
        uint256 borrowAmount
    ) internal view {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        uint256 scale = $.strikeScale[collateralAsset];
        if (scale == 0) {
            revert CV_InvalidConfig();
        }
        uint256 expected = Math.mulDiv(collateralAmount, putStrike, scale);
        if (expected != borrowAmount) {
            revert CV_InvalidInput();
        }
    }

    function _validatePermit(
        address collateralAsset,
        uint256 collateralAmount,
        IAllowanceTransfer.PermitSingle calldata permit
    ) internal view {
        if (permit.details.token != collateralAsset) {
            revert CV_InvalidInput();
        }
        if (permit.spender != address(this)) {
            revert CV_InvalidInput();
        }
        if (permit.details.amount < collateralAmount) {
            revert CV_InvalidInput();
        }
    }

    function _releaseCommittedPrincipal(uint256 amount) internal {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        if (amount == 0) {
            return;
        }
        $.totalCommittedPrincipal -= amount;
    }

    /// @notice Backward-compatible wrapper that uses full msg.value for deposit LZ message.
    function _requestCollateralDeposit(address borrower, DepositParams calldata params)
        internal
        returns (uint256 loanId, bytes32 socketMessageId, bytes32 lzGuid)
    {
        return _requestCollateralDepositWithBudget(borrower, params, msg.value);
    }

    /// @notice Requests collateral deposit with explicit ETH budget for LZ message.
    /// @param borrower The address of the borrower
    /// @param params The deposit parameters
    /// @param ethForLz The amount of ETH to use for the LZ message fee (msg.value - bridgeFee)
    function _requestCollateralDepositWithBudget(address borrower, DepositParams calldata params, uint256 ethForLz)
        internal
        returns (uint256 loanId, bytes32 socketMessageId, bytes32 lzGuid)
    {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        if (!$.collateralAllowed[params.collateralAsset]) {
            revert CV_InvalidConfig();
        }
        if (params.collateralAmount == 0) {
            revert CV_InvalidInput();
        }
        if (params.maturity <= block.timestamp) {
            revert CV_InvalidInput();
        }
        if (address($.lzMessenger) == address(0)) {
            revert CV_InvalidConfig();
        }
        if ($.deriveSubaccountId == 0) {
            revert CV_InvalidConfig();
        }

        loanId = $.nextLoanId++;
        $.pendingDeposits[loanId] = CollarVaultShared.PendingDeposit({
            borrower: borrower,
            collateralAsset: params.collateralAsset,
            collateralAmount: params.collateralAmount,
            maturity: params.maturity,
            putStrike: params.putStrike,
            borrowAmount: params.borrowAmount
        });

        CollarVaultShared.SocketBridgeConfig storage config = $.socketBridgeConfigs[params.collateralAsset];
        if (address(config.adapter) == address(0)) {
            revert CV_InvalidConfig();
        }
        socketMessageId = config.adapter.messageId();

        uint256 bridgeFee = estimateBridgeFees(params.collateralAsset, $.l2Recipient, params.collateralAmount);
        if (ethForLz < bridgeFee) {
            revert CV_InsufficientValue();
        }

        address l2MessageAsset_ = $.l2MessageAsset[params.collateralAsset];
        if (l2MessageAsset_ == address(0)) {
            revert CV_InvalidConfig();
        }

        _bridgeToL2(params.collateralAsset, params.collateralAmount, $.l2Recipient);
        lzGuid = $.lzMessenger.sendDepositIntentAutoFee{value: ethForLz - bridgeFee}(
            loanId,
            l2MessageAsset_,
            params.collateralAmount,
            address(this),
            $.deriveSubaccountId,
            socketMessageId,
            msg.sender
        );

        emit CollateralDepositRequested(
            loanId, borrower, params.collateralAsset, params.collateralAmount, params.maturity, socketMessageId, lzGuid
        );
    }

    /// @notice Creates a deposit AND accepts a mandate in a single transaction using standard ERC20 transferFrom.
    /// @dev Borrower must have approved this contract for collateral transfer beforehand.
    /// Keeper pre-signs RFQ with loanId=0 (sentinel), which the vault substitutes with actual loanId.
    /// @param params The deposit parameters
    /// @param rfq The keeper-signed baseline RFQ (with loanId=0 sentinel)
    /// @param rfqSig The RFQ signature
    /// @param deadline The mandate deadline after which borrower can request return
    /// @return loanId The assigned loan ID
    /// @return socketMessageId The Socket bridge message ID
    /// @return depositLzGuid The LayerZero GUID for the DepositIntent message
    /// @return mandateLzGuid The LayerZero GUID for the MandateCreated message
    function createDepositWithMandate(
        DepositParams calldata params,
        ICollarVaultFinalizeModule.BaselineRfq calldata rfq,
        bytes calldata rfqSig,
        uint64 deadline
    )
        external
        payable
        nonReentrant
        whenNotPaused
        returns (uint256 loanId, bytes32 socketMessageId, bytes32 depositLzGuid, bytes32 mandateLzGuid)
    {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();

        // Validate inputs
        if (!$.collateralAllowed[params.collateralAsset]) {
            revert CV_InvalidConfig();
        }
        if (params.collateralAmount == 0) {
            revert CV_InvalidInput();
        }
        if (params.maturity <= block.timestamp) {
            revert CV_InvalidInput();
        }
        _validateBorrowAmount(params.collateralAsset, params.collateralAmount, params.putStrike, params.borrowAmount);

        // Ensure finalize module is configured
        address module = $.finalizeModule;
        if (module == address(0)) {
            revert CV_InvalidConfig();
        }

        // Pull collateral via standard ERC20 transferFrom (requires prior approval)
        IERC20(params.collateralAsset).safeTransferFrom(msg.sender, address(this), params.collateralAmount);

        // Calculate ETH split: bridge fee + deposit LZ fee + mandate LZ fee.
        // We keep a simple split heuristic between both LZ messages.
        uint256 bridgeFee = estimateBridgeFees(params.collateralAsset, $.l2Recipient, params.collateralAmount);
        if (msg.value < bridgeFee) {
            revert CV_InsufficientValue();
        }
        uint256 remainingEth = msg.value - bridgeFee;
        uint256 ethForDepositLz = remainingEth / 2;
        uint256 ethForMandateLz = remainingEth - ethForDepositLz;

        // Step 1: Create the pending deposit and send DepositIntent to L2
        (loanId, socketMessageId, depositLzGuid) =
            _requestCollateralDepositWithBudget(msg.sender, params, ethForDepositLz + bridgeFee);

        // Step 2: Accept mandate via delegatecall to finalize module
        // Note: RFQ is passed as-is (with loanId=0 sentinel if used)
        // The finalize module handles sentinel loanId and verifies signature against original hash
        bytes memory ret = _delegateTo(
            module,
            abi.encodeCall(
                ICollarVaultFinalizeModule.acceptMandateInternal, (loanId, rfq, rfqSig, deadline, ethForMandateLz)
            )
        );
        mandateLzGuid = abi.decode(ret, (bytes32));

        // Any excess LZ fee budget in each send*AutoFee call is refunded by the messenger to `msg.sender`.
    }

    // (removed) _confirmLoanCreation/_openLoan: quote-based flow removed.

    function _bridgeToL2(address asset, uint256 amount, address receiver) internal {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        CollarVaultShared.SocketBridgeConfig storage config = $.socketBridgeConfigs[asset];
        if (address(config.adapter) == address(0)) {
            revert CV_InvalidConfig();
        }
        uint256 fee = estimateBridgeFees(asset, receiver, amount);
        if (address(this).balance < fee) {
            revert CV_InsufficientValue();
        }
        IERC20(asset).safeIncreaseAllowance(address(config.adapter), amount);
        config.adapter.bridge{value: fee}(receiver, amount);
    }

    function _loadLZMessage(bytes32 guid) internal view returns (CollarLZMessages.Message memory message) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        if (address($.lzMessenger) == address(0)) {
            revert CV_InvalidConfig();
        }
        if ($.lzMessageConsumed[guid]) {
            revert CV_InvalidMessage();
        }

        message = $.lzMessenger.receivedMessage(guid);
        if (message.loanId == 0) {
            revert CV_InvalidMessage();
        }
    }

    function _peekLZMessage(bytes32 guid) internal view returns (CollarLZMessages.Message memory message) {
        message = _loadLZMessage(guid);
    }

    /// @notice Record that an RFQ trade was confirmed on L2 and mark collateral activated.
    /// @dev This marker is optional; finalizeLoan also validates TradeConfirmed directly.
    function recordTradeConfirmed(bytes32 tradeGuid) external whenNotPaused {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        CollarLZMessages.Message memory lzMessage = _peekLZMessage(tradeGuid);
        uint256 loanId = $.lzMessenger.validateTradeConfirmedMarker(lzMessage, address(this), $.deriveSubaccountId);
        if ($.tradeConfirmed[loanId]) {
            revert CV_InvalidState();
        }
        $.tradeConfirmed[loanId] = true;
        $.collateralActivated[loanId] = true;
        $.returnRequested[loanId] = false;
        emit TradeConfirmedRecorded(loanId, tradeGuid);
    }

    function _consumeLZMessage(bytes32 guid) internal returns (CollarLZMessages.Message memory message) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        message = _loadLZMessage(guid);
        $.lzMessageConsumed[guid] = true;
    }

    function _delegateTo(address target, bytes memory callData) internal returns (bytes memory result) {
        bool ok;
        assembly {
            let ptr := mload(0x40)
            let data := add(callData, 0x20)
            let size := mload(callData)
            ok := delegatecall(gas(), target, data, size, 0, 0)
            let rsize := returndatasize()
            mstore(0x40, add(ptr, and(add(add(rsize, 0x20), 0x1f), not(0x1f))))
            result := ptr
            mstore(result, rsize)
            returndatacopy(add(result, 0x20), 0, rsize)
        }
        if (!ok) {
            assembly {
                revert(add(result, 0x20), mload(result))
            }
        }
    }

    modifier onlyKeeper() {
        _onlyKeeper();
        _;
    }

    function _onlyKeeper() internal view {
        if (!hasRole(KEEPER_ROLE, msg.sender)) {
            revert CV_Unauthorized();
        }
    }

    receive() external payable {}
}
