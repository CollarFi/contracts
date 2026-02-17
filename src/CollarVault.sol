// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {AccessControlUpgradeable} from "openzeppelin-upgradeable/access/AccessControlUpgradeable.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {PausableUpgradeable} from "openzeppelin-upgradeable/utils/PausableUpgradeable.sol";
import {ReentrancyGuardUpgradeable} from "openzeppelin-upgradeable/utils/ReentrancyGuardUpgradeable.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {EIP712Upgradeable} from "openzeppelin-upgradeable/utils/cryptography/EIP712Upgradeable.sol";
import {Initializable} from "openzeppelin-upgradeable/proxy/utils/Initializable.sol";
import {IAllowanceTransfer} from "permit2/src/interfaces/IAllowanceTransfer.sol";

import {IEulerAdapter} from "./interfaces/IEulerAdapter.sol";
import {IBridgeAdapter} from "./interfaces/IBridgeAdapter.sol";
import {CollarLZMessages} from "./bridge/CollarLZMessages.sol";
import {ICollarVaultMessenger} from "./interfaces/ICollarVaultMessenger.sol";
import {ICollarVaultFinalizeModule} from "./interfaces/ICollarVaultFinalizeModule.sol";
import {ICollarVaultSettleModule} from "./interfaces/ICollarVaultSettleModule.sol";
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

    struct BaselineRfq {
        uint256 loanId;
        address collateralAsset;
        uint256 collateralAmount;
        uint64 maturity;
        uint256 putStrike;
        uint256 callStrike;
        uint256 borrowAmount;
        uint64 rfqExpiry;
        address borrower;
        uint256 nonce;
    }

    bytes32 public constant BASELINE_RFQ_TYPEHASH = keccak256(
        "BaselineRfq(uint256 loanId,address collateralAsset,uint256 collateralAmount,uint64 maturity,uint256 putStrike,uint256 callStrike,uint256 borrowAmount,uint64 rfqExpiry,address borrower,uint256 nonce)"
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
    event LoanConverted(uint256 indexed loanId, uint256 variableDebt);
    event LoanClosed(uint256 indexed loanId);
    event TreasuryUpdated(address indexed treasury, uint256 bps);
    event OriginationFeeAprUpdated(uint256 feeApr);
    event MaxTotalPrincipalUpdated(uint256 maxTotalPrincipal);
    event CollateralConfigUpdated(
        address indexed asset, bool allowed, uint256 strikeScale, address indexed l2MessageAsset
    );
    event BridgeConfigUpdated(address indexed asset, address indexed adapter);
    event L2RecipientUpdated(address indexed recipient);
    event EulerAdapterUpdated(address indexed adapter);
    event SubaccountUpdated(uint256 subaccountId);
    event RfqSignerUpdated(address indexed signer, bool allowed);
    event LZMessengerUpdated(address indexed messenger);
    event FinalizeModuleUpdated(address indexed module);
    event SettleModuleUpdated(address indexed module);
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
    event MandateAccepted(
        uint256 indexed loanId,
        address indexed borrower,
        uint64 maturity,
        uint256 borrowAmount,
        uint256 minCallStrike,
        uint256 maxPutStrike,
        uint64 deadline,
        bytes32 lzGuid
    );

    constructor() {
        _disableInitializers();
    }

    function initialize(
        address admin,
        ILiquidityVault liquidityVault_,
        IEulerAdapter eulerAdapter_,
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
            admin == address(0) || address(liquidityVault_) == address(0) || address(eulerAdapter_) == address(0)
                || address(permit2_) == address(0) || l2Recipient_ == address(0) || treasury_ == address(0)
        ) {
            revert CV_InvalidConfig();
        }

        $.liquidityVault = liquidityVault_;
        $.usdc = IERC20(liquidityVault_.asset());
        $.eulerAdapter = eulerAdapter_;
        $.permit2 = permit2_;
        $.l2Recipient = l2Recipient_;
        $.treasury = treasury_;
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

    function eulerAdapter() external view returns (IEulerAdapter) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        return $.eulerAdapter;
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
            uint256 loanOriginationFeeApr,
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
            loan.originationFeeApr,
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
            mandate.sentToL2
        );
    }

    function usedBaselineRfqs(bytes32 rfqHash) external view returns (bool) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        return $.usedBaselineRfqs[rfqHash];
    }

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

    function hashBaselineRfq(BaselineRfq memory rfq) public view returns (bytes32) {
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
                rfq.rfqExpiry,
                rfq.borrower,
                rfq.nonce
            )
        );
        return _hashTypedDataV4(structHash);
    }

    /// @notice Accept a mandate on L1, constrained by a keeper-signed RFQ baseline.
    /// @dev Mandates must be mirrored L1->L2 via LayerZero since the TSA lives on a different network.
    /// @param deadline Timestamp after which the borrower can request collateral return.
    function acceptMandate(uint256 loanId, BaselineRfq calldata rfq, bytes calldata rfqSig, uint64 deadline)
        external
        payable
        nonReentrant
        whenNotPaused
        returns (bytes32 lzGuid)
    {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        CollarVaultShared.PendingDeposit memory pending = $.pendingDeposits[loanId];
        if (pending.borrower == address(0)) {
            revert CV_NotFound();
        }
        if (pending.borrower != msg.sender) {
            revert CV_Unauthorized();
        }
        if (address($.lzMessenger) == address(0)) {
            revert CV_InvalidConfig();
        }
        if ($.deriveSubaccountId == 0) {
            revert CV_InvalidConfig();
        }
        if (deadline <= block.timestamp) {
            revert CV_InvalidState();
        }

        // Only allow replacing an expired mandate.
        CollarVaultShared.Mandate memory existing = $.mandates[loanId];
        bool hadMandate = existing.borrower != address(0);
        if (hadMandate && block.timestamp < existing.deadline) {
            revert CV_InvalidState();
        }

        // Validate keeper-signed baseline RFQ.
        if (rfq.loanId != loanId) {
            revert CV_InvalidMessage();
        }
        if (rfq.borrower != address(0) && rfq.borrower != msg.sender) {
            revert CV_Unauthorized();
        }
        if (rfq.rfqExpiry < block.timestamp) {
            revert CV_InvalidState();
        }
        if (
            rfq.collateralAsset != pending.collateralAsset || rfq.collateralAmount != pending.collateralAmount
                || rfq.maturity != uint64(pending.maturity) || rfq.putStrike != pending.putStrike
                || rfq.borrowAmount != pending.borrowAmount
        ) {
            revert CV_InvalidMessage();
        }
        if (rfq.callStrike == 0) {
            revert CV_InvalidInput();
        }

        bytes32 rfqHash = hashBaselineRfq(rfq);
        if ($.usedBaselineRfqs[rfqHash]) {
            revert CV_InvalidMessage();
        }
        address signer = ECDSA.recover(rfqHash, rfqSig);
        if (!hasRole(RFQ_SIGNER_ROLE, signer)) {
            revert CV_Unauthorized();
        }
        $.usedBaselineRfqs[rfqHash] = true;

        // Reserve liquidity once per loanId. Renewing an expired mandate does not re-commit.
        if (!hadMandate) {
            _commitPrincipal(pending.borrowAmount);
        }
        if (pending.maturity > type(uint64).max) {
            revert CV_InvalidInput();
        }

        uint256 minCallStrike = rfq.callStrike;
        uint256 maxPutStrike = rfq.putStrike;

        $.mandates[loanId] = CollarVaultShared.Mandate({
            borrower: pending.borrower,
            collateralAsset: pending.collateralAsset,
            collateralAmount: pending.collateralAmount,
            maturity: uint64(pending.maturity),
            deadline: deadline,
            borrowAmount: pending.borrowAmount,
            minCallStrike: minCallStrike,
            maxPutStrike: maxPutStrike,
            sentToL2: true
        });

        lzGuid = _sendMandateCreated(loanId, pending, minCallStrike, maxPutStrike, deadline);

        emit MandateAccepted(
            loanId,
            pending.borrower,
            uint64(pending.maturity),
            pending.borrowAmount,
            minCallStrike,
            maxPutStrike,
            deadline,
            lzGuid
        );
    }

    function _sendMandateCreated(
        uint256 loanId,
        CollarVaultShared.PendingDeposit memory pending,
        uint256 minCallStrike,
        uint256 maxPutStrike,
        uint64 deadline
    ) internal returns (bytes32 lzGuid) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        bytes memory mandateData =
            abi.encode(pending.borrower, minCallStrike, maxPutStrike, uint64(pending.maturity), deadline);

        lzGuid = $.lzMessenger.sendMandateCreatedAutoFee{value: msg.value}(
            loanId,
            pending.collateralAsset,
            pending.borrowAmount,
            address(this),
            $.deriveSubaccountId,
            mandateData,
            msg.sender
        );
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

    /// @notice Convert a neutral-expiry loan into a variable-rate Euler position.
    function convertToVariable(uint256 loanId, bytes32 lzGuid) external nonReentrant whenNotPaused {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        CollarVaultShared.Loan storage loan = $.loans[loanId];
        if (loan.state != CollarVaultShared.LoanState.ACTIVE_ZERO_COST) {
            revert CV_InvalidState();
        }
        if (block.timestamp < loan.maturity) {
            revert CV_InvalidState();
        }
        if (msg.sender != loan.borrower && !hasRole(KEEPER_ROLE, msg.sender)) {
            revert CV_Unauthorized();
        }

        CollarLZMessages.Message memory lzMessage = _consumeLZMessage(lzGuid);
        $.lzMessenger
            .validateCollateralReturned(
                lzMessage, loanId, loan.collateralAsset, loan.collateralAmount, address(this), $.deriveSubaccountId
            );
        _convertToVariable(loanId, lzMessage.amount);
    }

    // (removed) hashQuote: quote-based flow removed.

    /// @notice Return a loan record by id.
    function getLoan(uint256 loanId) external view returns (CollarVaultShared.Loan memory loan_) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        CollarVaultShared.Loan storage loan = $.loans[loanId];
        loan_ = CollarVaultShared.Loan({
            borrower: loan.borrower,
            collateralAsset: loan.collateralAsset,
            collateralAmount: loan.collateralAmount,
            maturity: loan.maturity,
            putStrike: loan.putStrike,
            callStrike: loan.callStrike,
            principal: loan.principal,
            subaccountId: loan.subaccountId,
            state: CollarVaultShared.LoanState(uint8(loan.state)),
            startTime: loan.startTime,
            originationFeeApr: loan.originationFeeApr,
            variableDebt: loan.variableDebt
        });
    }

    /// @notice Calculate the annualized origination fee amount for a loan.
    function calculateOriginationFee(uint256 loanId) external view returns (uint256) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        CollarVaultShared.Loan storage loan = $.loans[loanId];
        if (loan.state == CollarVaultShared.LoanState.NONE) {
            revert CV_InvalidState();
        }
        if (loan.maturity <= loan.startTime) {
            return 0;
        }
        uint256 annualFee = Math.mulDiv(loan.principal, loan.originationFeeApr, 1e18);
        uint256 duration = loan.maturity - loan.startTime;
        return Math.mulDiv(annualFee, duration, YEAR);
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

    /// @notice Update the Euler adapter.
    function setEulerAdapter(IEulerAdapter newAdapter) external onlyRole(PARAMETER_ROLE) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        if (address(newAdapter) == address(0)) {
            revert CV_InvalidConfig();
        }
        $.eulerAdapter = newAdapter;
        emit EulerAdapterUpdated(address(newAdapter));
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

    function _quoteOriginationFee(uint256 borrowAmount, uint256 maturity) internal view returns (uint256) {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        if ($.originationFeeApr == 0) {
            return 0;
        }
        if (maturity <= block.timestamp) {
            return 0;
        }
        uint256 duration = maturity - block.timestamp;
        uint256 annualFee = Math.mulDiv(borrowAmount, $.originationFeeApr, 1e18);
        return Math.mulDiv(annualFee, duration, YEAR);
    }

    function _commitPrincipal(uint256 amount) internal {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        if (amount == 0) {
            return;
        }
        uint256 cap = $.maxTotalPrincipal;
        if (cap != 0 && $.totalCommittedPrincipal + amount > cap) {
            revert CV_InvalidState();
        }
        $.totalCommittedPrincipal += amount;
    }

    function _releaseCommittedPrincipal(uint256 amount) internal {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        if (amount == 0) {
            return;
        }
        $.totalCommittedPrincipal -= amount;
    }

    function _requestCollateralDeposit(address borrower, DepositParams calldata params)
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
        if (msg.value < bridgeFee) {
            revert CV_InsufficientValue();
        }

        address l2MessageAsset_ = $.l2MessageAsset[params.collateralAsset];
        if (l2MessageAsset_ == address(0)) {
            revert CV_InvalidConfig();
        }

        _bridgeToL2(params.collateralAsset, params.collateralAmount, $.l2Recipient);
        lzGuid = $.lzMessenger.sendDepositIntentAutoFee{value: msg.value - bridgeFee}(
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

    // (removed) _confirmLoanCreation/_openLoan: quote-based flow removed.

    function _convertToVariable(uint256 loanId, uint256 collateralAmount) internal {
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        CollarVaultShared.Loan storage loan = $.loans[loanId];
        if (collateralAmount != loan.collateralAmount) {
            revert CV_InvalidInput();
        }
        _releaseCommittedPrincipal(loan.principal);
        IERC20(loan.collateralAsset).safeIncreaseAllowance(address($.eulerAdapter), collateralAmount);
        $.eulerAdapter.depositCollateral(loan.collateralAsset, collateralAmount, loan.borrower);
        $.eulerAdapter.borrow(address($.usdc), loan.principal, loan.borrower, address(this));
        $.usdc.safeIncreaseAllowance(address($.liquidityVault), loan.principal);
        $.liquidityVault.repay(loan.principal);
        loan.state = CollarVaultShared.LoanState.CLOSED;
        loan.variableDebt = 0;
        emit LoanConverted(loanId, loan.principal);
        emit LoanClosed(loanId);
    }

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
        CollarVaultShared.CollarVaultStorage storage $ = _getCollarVaultStorage();
        if (address($.lzMessenger) == address(0)) {
            revert CV_InvalidConfig();
        }

        message = $.lzMessenger.receivedMessage(guid);
        if (message.loanId == 0) {
            revert CV_InvalidMessage();
        }
    }

    /// @notice Record that a trade was confirmed on L2 and mark collateral activated.
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
