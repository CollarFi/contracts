// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {
    MessagingFee,
    MessagingReceipt
} from "@layerzerolabs/lz-evm-protocol-v2/contracts/interfaces/ILayerZeroEndpointV2.sol";
import {IAllowanceTransfer} from "permit2/src/interfaces/IAllowanceTransfer.sol";

import {IEulerAdapter} from "./interfaces/IEulerAdapter.sol";
import {ISocketBridge} from "./interfaces/ISocketBridge.sol";
import {ISocketConnector} from "./interfaces/ISocketConnector.sol";
import {CollarLZMessages} from "./bridge/CollarLZMessages.sol";
import {ICollarVaultMessenger} from "./interfaces/ICollarVaultMessenger.sol";

interface ILiquidityVault {
    function borrow(uint256 amount) external;
    function repay(uint256 amount) external;
    function writeOff(uint256 amount) external;
    function asset() external view returns (address);
}

contract CollarVault is AccessControl, EIP712, Pausable, ReentrancyGuard {
    using SafeERC20 for IERC20;

    bytes32 public constant KEEPER_ROLE = keccak256("KEEPER_ROLE");
    bytes32 public constant EXECUTOR_ROLE = keccak256("EXECUTOR_ROLE");
    bytes32 public constant PARAMETER_ROLE = keccak256("PARAMETER_ROLE");
    bytes32 public constant PAUSER_ROLE = keccak256("PAUSER_ROLE");
    bytes32 public constant RFQ_SIGNER_ROLE = keccak256("RFQ_SIGNER_ROLE");
    uint256 public constant YEAR = 365 days;
    uint256 public constant MAX_BPS = 10_000;

    enum LoanState {
        NONE,
        ACTIVE_ZERO_COST,
        ACTIVE_VARIABLE,
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

    ILiquidityVault public liquidityVault;
    IERC20 public immutable usdc;
    IAllowanceTransfer public immutable permit2;

    struct SocketBridgeConfig {
        ISocketBridge bridge;
        ISocketConnector connector;
        uint256 msgGasLimit;
        bytes options;
        bytes extraData;
    }

    mapping(address => SocketBridgeConfig) private socketBridgeConfigs;
    IEulerAdapter public eulerAdapter;
    address public l2Recipient;
    address public treasury;
    uint256 public treasuryBps;
    uint256 public originationFeeApr;
    uint256 public maxTotalPrincipal;
    uint256 public totalCommittedPrincipal;
    uint256 public deriveSubaccountId;
    uint256 public nextLoanId = 1;

    mapping(uint256 => Loan) public loans;
    mapping(uint256 => PendingDeposit) public pendingDeposits;

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

    mapping(uint256 => Mandate) public mandates;

    mapping(bytes32 => bool) public usedBaselineRfqs;

    mapping(uint256 => bool) public tradeConfirmed;
    mapping(uint256 => bool) public collateralActivated;
    mapping(uint256 => bool) public returnRequested;
    // Quote-based RFQ flow removed: no quote replay tracking.
    mapping(address => bool) public collateralAllowed;
    mapping(address => uint256) public strikeScale;

    ICollarVaultMessenger public lzMessenger;
    mapping(bytes32 => bool) public lzMessageConsumed;

    error CV_ZeroAddress();
    error CV_InvalidAmount();
    error CV_InvalidMaturity();
    error CV_NotMatured();
    error CV_CollateralNotAllowed();
    error CV_StrikeScaleUnset();
    error CV_InvalidBorrowAmount();
    error CV_MandateExpired();
    // (removed) CV_QuoteUsed / CV_InvalidQuoteSigner
    error CV_MandateNotExpired();
    error CV_InvalidLoanState();
    error CV_InsufficientSettlement();
    error CV_TreasuryBpsTooHigh();
    error CV_NotBorrower();
    error CV_InvalidSubaccount();
    error CV_NotAuthorized();
    error CV_InsufficientBridgeFees();
    error CV_RefundFailed();
    error CV_LZMessengerNotSet();
    error CV_LZMessageNotFound();
    error CV_LZMessageConsumed();
    error CV_LZMessageMismatch();
    error CV_LZMessageRecipientMismatch();
    error CV_PendingDepositNotFound();
    error CV_PendingDepositReturnBlocked();
    error CV_MandateNotFound();
    error CV_MandateAlreadySet();
    error CV_ReturnAlreadyRequested();
    // (removed) CV_QuoteLoanIdMismatch / CV_DepositParamsMismatch
    error CV_TradeAlreadyConfirmed();
    error CV_PermitTokenMismatch();
    error CV_PermitSpenderMismatch();
    error CV_PermitAmountTooLow();
    error CV_PermitAmountOverflow();
    error CV_TotalPrincipalCapExceeded();

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
    event LoanSettled(uint256 indexed loanId, SettlementOutcome outcome, uint256 settlementAmount);
    event SettlementShortfall(uint256 indexed loanId, uint256 shortfall);
    event LoanConverted(uint256 indexed loanId, uint256 variableDebt);
    event LoanClosed(uint256 indexed loanId);
    event TreasuryUpdated(address indexed treasury, uint256 bps);
    event OriginationFeeAprUpdated(uint256 feeApr);
    event MaxTotalPrincipalUpdated(uint256 maxTotalPrincipal);
    event CollateralConfigUpdated(address indexed asset, bool allowed, uint256 strikeScale);
    event BridgeConfigUpdated(
        address indexed asset,
        address indexed bridge,
        address indexed connector,
        uint256 msgGasLimit,
        bytes options,
        bytes extraData
    );
    event L2RecipientUpdated(address indexed recipient);
    event EulerAdapterUpdated(address indexed adapter);
    event SubaccountUpdated(uint256 subaccountId);
    event RfqSignerUpdated(address indexed signer, bool allowed);
    event LZMessengerUpdated(address indexed messenger);
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

    constructor(
        address admin,
        ILiquidityVault liquidityVault_,
        address bridgeConfigAdmin_,
        IEulerAdapter eulerAdapter_,
        IAllowanceTransfer permit2_,
        address l2Recipient_,
        address treasury_
    ) EIP712("CollarVault", "1") {
        if (
            admin == address(0) || address(liquidityVault_) == address(0) || bridgeConfigAdmin_ == address(0)
                || address(eulerAdapter_) == address(0) || address(permit2_) == address(0) || l2Recipient_ == address(0)
                || treasury_ == address(0)
        ) {
            revert CV_ZeroAddress();
        }

        liquidityVault = liquidityVault_;
        usdc = IERC20(liquidityVault_.asset());
        eulerAdapter = eulerAdapter_;
        permit2 = permit2_;
        l2Recipient = l2Recipient_;
        treasury = treasury_;

        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        _grantRole(PARAMETER_ROLE, admin);
        _grantRole(KEEPER_ROLE, admin);
        _grantRole(EXECUTOR_ROLE, admin);
        _grantRole(PAUSER_ROLE, admin);
        _grantRole(PARAMETER_ROLE, bridgeConfigAdmin_);
    }

    /// @notice Request a collateral deposit via Permit2 and send a deposit intent to L2.
    function createDepositWithPermit(
        DepositParams calldata params,
        IAllowanceTransfer.PermitSingle calldata permit,
        bytes calldata permitSig
    ) external payable nonReentrant whenNotPaused returns (uint256 loanId, bytes32 socketMessageId, bytes32 lzGuid) {
        if (!collateralAllowed[params.collateralAsset]) {
            revert CV_CollateralNotAllowed();
        }
        if (params.collateralAmount == 0) {
            revert CV_InvalidAmount();
        }
        if (params.maturity <= block.timestamp) {
            revert CV_InvalidMaturity();
        }
        _validateBorrowAmount(params.collateralAsset, params.collateralAmount, params.putStrike, params.borrowAmount);
        _validatePermit(params.collateralAsset, params.collateralAmount, permit);
        if (params.collateralAmount > type(uint160).max) {
            revert CV_PermitAmountOverflow();
        }

        permit2.permit(msg.sender, permit, permitSig);
        permit2.transferFrom(msg.sender, address(this), uint160(params.collateralAmount), params.collateralAsset);

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
        PendingDeposit memory pending = pendingDeposits[loanId];
        if (pending.borrower == address(0)) {
            revert CV_PendingDepositNotFound();
        }
        if (pending.borrower != msg.sender) {
            revert CV_NotBorrower();
        }
        if (address(lzMessenger) == address(0)) {
            revert CV_LZMessengerNotSet();
        }
        if (deriveSubaccountId == 0) {
            revert CV_InvalidSubaccount();
        }
        if (deadline <= block.timestamp) {
            revert CV_MandateExpired();
        }

        // Only allow replacing an expired mandate.
        Mandate memory existing = mandates[loanId];
        bool hadMandate = existing.borrower != address(0);
        if (hadMandate && block.timestamp < existing.deadline) {
            revert CV_MandateAlreadySet();
        }

        // Validate keeper-signed baseline RFQ.
        if (rfq.loanId != loanId) {
            revert CV_LZMessageMismatch();
        }
        if (rfq.borrower != address(0) && rfq.borrower != msg.sender) {
            revert CV_NotAuthorized();
        }
        if (rfq.rfqExpiry < block.timestamp) {
            revert CV_MandateExpired();
        }
        if (
            rfq.collateralAsset != pending.collateralAsset || rfq.collateralAmount != pending.collateralAmount
                || rfq.maturity != uint64(pending.maturity) || rfq.putStrike != pending.putStrike
                || rfq.borrowAmount != pending.borrowAmount
        ) {
            revert CV_LZMessageMismatch();
        }
        if (rfq.callStrike == 0) {
            revert CV_InvalidAmount();
        }

        bytes32 rfqHash = hashBaselineRfq(rfq);
        if (usedBaselineRfqs[rfqHash]) {
            revert CV_LZMessageMismatch();
        }
        address signer = ECDSA.recover(rfqHash, rfqSig);
        if (!hasRole(RFQ_SIGNER_ROLE, signer)) {
            revert CV_NotAuthorized();
        }
        usedBaselineRfqs[rfqHash] = true;

        // Reserve liquidity once per loanId. Renewing an expired mandate does not re-commit.
        if (!hadMandate) {
            _commitPrincipal(pending.borrowAmount);
        }
        if (pending.maturity > type(uint64).max) {
            revert CV_InvalidMaturity();
        }

        uint256 minCallStrike = rfq.callStrike;
        uint256 maxPutStrike = rfq.putStrike;

        mandates[loanId] = Mandate({
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
        PendingDeposit memory pending,
        uint256 minCallStrike,
        uint256 maxPutStrike,
        uint64 deadline
    ) internal returns (bytes32 lzGuid) {
        bytes memory mandateData = abi.encode(
            pending.borrower, minCallStrike, maxPutStrike, uint64(pending.maturity), deadline
        );

        CollarLZMessages.Message memory message = CollarLZMessages.Message({
            action: CollarLZMessages.Action.MandateCreated,
            loanId: loanId,
            asset: pending.collateralAsset,
            amount: pending.borrowAmount,
            recipient: address(this),
            subaccountId: deriveSubaccountId,
            socketMessageId: bytes32(0),
            secondaryAmount: 0,
            quoteHash: bytes32(0),
            takerNonce: 0,
            data: mandateData
        });

        bytes memory options = lzMessenger.defaultOptions();
        MessagingFee memory lzFee = lzMessenger.quoteMessage(message, options);
        if (msg.value < lzFee.nativeFee) {
            revert CV_InsufficientBridgeFees();
        }

        MessagingReceipt memory receipt = lzMessenger.sendMessage{value: lzFee.nativeFee}(message);
        lzGuid = receipt.guid;

        if (msg.value > lzFee.nativeFee) {
            (bool success,) = msg.sender.call{value: msg.value - lzFee.nativeFee}("");
            if (!success) {
                revert CV_RefundFailed();
            }
        }
    }

    /// @notice Finalize a loan once deposit and RFQ trades have been confirmed on L2.
    function finalizeLoan(uint256 loanId, bytes32 depositGuid, bytes32 tradeGuid)
        external
        nonReentrant
        whenNotPaused
        onlyKeeperOrExecutor
        returns (uint256 finalizedLoanId)
    {
        PendingDeposit memory pending = pendingDeposits[loanId];
        if (pending.borrower == address(0)) {
            revert CV_PendingDepositNotFound();
        }

        Mandate memory mandate = mandates[loanId];
        if (mandate.borrower == address(0)) {
            revert CV_MandateNotFound();
        }
        if (mandate.borrower != pending.borrower) {
            revert CV_NotAuthorized();
        }
        if (block.timestamp > mandate.deadline) {
            revert CV_MandateExpired();
        }

        // Confirm deposit + trade on L2.
        CollarLZMessages.Message memory depositMessage = _loadLZMessage(depositGuid);
        CollarLZMessages.Message memory tradeMessage = _loadLZMessage(tradeGuid);

        finalizedLoanId = _validateDepositConfirmed(depositMessage, pending, mandate.borrower);

        // Validate trade confirmation.
        if (tradeMessage.action != CollarLZMessages.Action.TradeConfirmed || tradeMessage.loanId != finalizedLoanId) {
            revert CV_LZMessageMismatch();
        }
        if (tradeMessage.recipient != address(this)) {
            revert CV_LZMessageRecipientMismatch();
        }
        if (deriveSubaccountId != 0 && tradeMessage.subaccountId != deriveSubaccountId) {
            revert CV_LZMessageMismatch();
        }

        (uint256 callStrike, uint256 putStrike, uint64 expiry) =
            abi.decode(tradeMessage.data, (uint256, uint256, uint64));

        if (expiry != mandate.maturity || expiry != uint64(pending.maturity)) {
            revert CV_LZMessageMismatch();
        }
        if (callStrike < mandate.minCallStrike) {
            revert CV_LZMessageMismatch();
        }
        if (putStrike > mandate.maxPutStrike) {
            revert CV_LZMessageMismatch();
        }

        _validateOriginationFee(tradeMessage, pending.borrowAmount, pending.maturity);

        // Mark trade confirmed and consume messages.
        tradeConfirmed[finalizedLoanId] = true;
        collateralActivated[finalizedLoanId] = true;
        returnRequested[finalizedLoanId] = false;
        lzMessageConsumed[depositGuid] = true;
        lzMessageConsumed[tradeGuid] = true;

        delete pendingDeposits[finalizedLoanId];
        delete mandates[finalizedLoanId];

        // Open loan.
        loans[finalizedLoanId] = Loan({
            borrower: mandate.borrower,
            collateralAsset: pending.collateralAsset,
            collateralAmount: pending.collateralAmount,
            maturity: pending.maturity,
            putStrike: putStrike,
            callStrike: callStrike,
            principal: pending.borrowAmount,
            subaccountId: deriveSubaccountId,
            state: LoanState.ACTIVE_ZERO_COST,
            startTime: block.timestamp,
            originationFeeApr: originationFeeApr,
            variableDebt: 0
        });

        // Origination fee settlement.
        uint256 feeAmount = _quoteOriginationFee(pending.borrowAmount, pending.maturity);
        if (feeAmount > 0) {
            uint256 treasuryCut = Math.mulDiv(feeAmount, treasuryBps, MAX_BPS);
            uint256 vaultCut = feeAmount - treasuryCut;
            if (treasuryCut > 0) {
                usdc.safeTransfer(treasury, treasuryCut);
            }
            if (vaultCut > 0) {
                usdc.safeTransfer(address(liquidityVault), vaultCut);
            }
        }

        liquidityVault.borrow(pending.borrowAmount);
        usdc.safeTransfer(mandate.borrower, pending.borrowAmount);

        emit LoanCreated(
            finalizedLoanId,
            mandate.borrower,
            pending.collateralAsset,
            pending.collateralAmount,
            pending.maturity,
            putStrike,
            callStrike,
            pending.borrowAmount,
            deriveSubaccountId
        );
    }

    /// @notice Request return of a pending collateral deposit before activation/trade.
    function requestCollateralReturn(uint256 loanId)
        external
        payable
        nonReentrant
        whenNotPaused
        returns (bytes32 lzGuid)
    {
        PendingDeposit storage pending = pendingDeposits[loanId];
        if (pending.borrower == address(0)) {
            revert CV_PendingDepositNotFound();
        }
        if (pending.borrower != msg.sender) {
            revert CV_NotBorrower();
        }
        if (returnRequested[loanId]) {
            revert CV_ReturnAlreadyRequested();
        }
        if (tradeConfirmed[loanId] || collateralActivated[loanId]) {
            revert CV_PendingDepositReturnBlocked();
        }
        Mandate memory mandate = mandates[loanId];
        if (mandate.borrower != address(0) && block.timestamp < mandate.deadline) {
            revert CV_MandateNotExpired();
        }
        if (loans[loanId].state != LoanState.NONE) {
            revert CV_InvalidLoanState();
        }
        if (address(lzMessenger) == address(0)) {
            revert CV_LZMessengerNotSet();
        }
        if (deriveSubaccountId == 0) {
            revert CV_InvalidSubaccount();
        }

        CollarLZMessages.Message memory message = CollarLZMessages.Message({
            action: CollarLZMessages.Action.ReturnRequest,
            loanId: loanId,
            asset: pending.collateralAsset,
            amount: pending.collateralAmount,
            recipient: address(this),
            subaccountId: deriveSubaccountId,
            socketMessageId: bytes32(0),
            secondaryAmount: 0,
            quoteHash: bytes32(0),
            takerNonce: 0,
            data: bytes("")
        });

        bytes memory options = lzMessenger.defaultOptions();
        MessagingFee memory lzFee = lzMessenger.quoteMessage(message, options);
        if (msg.value < lzFee.nativeFee) {
            revert CV_InsufficientBridgeFees();
        }

        MessagingReceipt memory receipt = lzMessenger.sendMessage{value: lzFee.nativeFee}(message);
        lzGuid = receipt.guid;
        returnRequested[loanId] = true;

        if (msg.value > lzFee.nativeFee) {
            (bool success,) = msg.sender.call{value: msg.value - lzFee.nativeFee}("");
            if (!success) {
                revert CV_RefundFailed();
            }
        }

        emit CollateralReturnRequested(loanId, msg.sender, pending.collateralAsset, pending.collateralAmount, lzGuid);
    }

    /// @notice Finalize a returned deposit and transfer collateral to the borrower.
    function finalizeDepositReturn(uint256 loanId, bytes32 lzGuid) external nonReentrant whenNotPaused {
        CollarLZMessages.Message memory lzMessage = _consumeLZMessage(lzGuid);
        if (lzMessage.action != CollarLZMessages.Action.CollateralReturned || lzMessage.loanId != loanId) {
            revert CV_LZMessageMismatch();
        }
        if (lzMessage.recipient != address(this)) {
            revert CV_LZMessageRecipientMismatch();
        }
        if (deriveSubaccountId != 0 && lzMessage.subaccountId != deriveSubaccountId) {
            revert CV_LZMessageMismatch();
        }

        PendingDeposit memory pending = pendingDeposits[loanId];
        if (pending.borrower == address(0)) {
            revert CV_PendingDepositNotFound();
        }
        if (tradeConfirmed[loanId]) {
            revert CV_PendingDepositReturnBlocked();
        }
        if (pending.collateralAsset != lzMessage.asset || pending.collateralAmount != lzMessage.amount) {
            revert CV_LZMessageMismatch();
        }
        if (loans[loanId].state != LoanState.NONE) {
            revert CV_InvalidLoanState();
        }

        delete pendingDeposits[loanId];

        Mandate memory mandate = mandates[loanId];
        if (mandate.borrower != address(0)) {
            _releaseCommittedPrincipal(mandate.borrowAmount);
            delete mandates[loanId];
        }

        returnRequested[loanId] = false;
        IERC20(pending.collateralAsset).safeTransfer(pending.borrower, pending.collateralAmount);
        emit CollateralDepositReturned(loanId, pending.borrower, pending.collateralAsset, pending.collateralAmount);
    }

    /// @notice Settle a matured loan into one of the three collar outcomes.
    function settleLoan(uint256 loanId, SettlementOutcome outcome, bytes32 lzGuid)
        external
        nonReentrant
        whenNotPaused
        onlyKeeperOrExecutor
    {
        Loan storage loan = loans[loanId];
        if (loan.state != LoanState.ACTIVE_ZERO_COST) {
            revert CV_InvalidLoanState();
        }
        if (block.timestamp < loan.maturity) {
            revert CV_NotMatured();
        }

        CollarLZMessages.Message memory lzMessage = _consumeLZMessage(lzGuid);
        uint256 settlementAmount = 0;

        if (outcome == SettlementOutcome.Neutral) {
            if (
                lzMessage.action != CollarLZMessages.Action.CollateralReturned || lzMessage.loanId != loanId
                    || lzMessage.asset != loan.collateralAsset || lzMessage.amount != loan.collateralAmount
            ) {
                revert CV_LZMessageMismatch();
            }
            if (lzMessage.recipient != address(this)) {
                revert CV_LZMessageRecipientMismatch();
            }
            _convertToVariable(loanId, lzMessage.amount);
            emit LoanSettled(loanId, outcome, settlementAmount);
            return;
        }

        if (
            lzMessage.action != CollarLZMessages.Action.SettlementReport || lzMessage.loanId != loanId
                || lzMessage.asset != address(usdc)
        ) {
            revert CV_LZMessageMismatch();
        }
        if (lzMessage.recipient != address(this)) {
            revert CV_LZMessageRecipientMismatch();
        }
        settlementAmount = lzMessage.amount;

        uint256 shortfall = 0;
        if (settlementAmount < loan.principal) {
            shortfall = loan.principal - settlementAmount;
        }

        uint256 repayAmount = settlementAmount > loan.principal ? loan.principal : settlementAmount;
        if (repayAmount > 0) {
            usdc.safeIncreaseAllowance(address(liquidityVault), repayAmount);
            liquidityVault.repay(repayAmount);
        }
        if (shortfall > 0) {
            liquidityVault.writeOff(shortfall);
            emit SettlementShortfall(loanId, shortfall);
        }

        uint256 excess = settlementAmount > loan.principal ? settlementAmount - loan.principal : 0;

        if (excess > 0) {
            if (outcome == SettlementOutcome.PutITM) {
                uint256 treasuryCut = Math.mulDiv(excess, treasuryBps, MAX_BPS);
                uint256 vaultCut = excess - treasuryCut;
                if (treasuryCut > 0) {
                    usdc.safeTransfer(treasury, treasuryCut);
                }
                if (vaultCut > 0) {
                    usdc.safeTransfer(address(liquidityVault), vaultCut);
                }
            } else if (outcome == SettlementOutcome.CallITM) {
                usdc.safeTransfer(loan.borrower, excess);
            }
        }

        _releaseCommittedPrincipal(loan.principal);
        loan.state = LoanState.CLOSED;
        emit LoanSettled(loanId, outcome, settlementAmount);
        emit LoanClosed(loanId);
    }

    /// @notice Convert a neutral-expiry loan into a variable-rate Euler position.
    function convertToVariable(uint256 loanId, bytes32 lzGuid) external nonReentrant whenNotPaused {
        Loan storage loan = loans[loanId];
        if (loan.state != LoanState.ACTIVE_ZERO_COST) {
            revert CV_InvalidLoanState();
        }
        if (block.timestamp < loan.maturity) {
            revert CV_NotMatured();
        }
        if (msg.sender != loan.borrower && !(hasRole(KEEPER_ROLE, msg.sender) || hasRole(EXECUTOR_ROLE, msg.sender))) {
            revert CV_NotAuthorized();
        }

        CollarLZMessages.Message memory lzMessage = _consumeLZMessage(lzGuid);
        if (
            lzMessage.action != CollarLZMessages.Action.CollateralReturned || lzMessage.loanId != loanId
                || lzMessage.asset != loan.collateralAsset || lzMessage.amount != loan.collateralAmount
        ) {
            revert CV_LZMessageMismatch();
        }
        if (lzMessage.recipient != address(this)) {
            revert CV_LZMessageRecipientMismatch();
        }
        _convertToVariable(loanId, lzMessage.amount);
    }

    /// @notice Repay a variable-rate loan and return collateral to the borrower.
    function repayVariable(uint256 loanId) external nonReentrant {
        Loan storage loan = loans[loanId];
        if (loan.state != LoanState.ACTIVE_VARIABLE) {
            revert CV_InvalidLoanState();
        }
        if (msg.sender != loan.borrower) {
            revert CV_NotBorrower();
        }

        uint256 debt = loan.variableDebt;
        if (debt == 0) {
            revert CV_InvalidAmount();
        }

        usdc.safeTransferFrom(msg.sender, address(this), debt);
        usdc.safeIncreaseAllowance(address(eulerAdapter), debt);
        eulerAdapter.repay(address(usdc), debt, loan.borrower);
        eulerAdapter.withdrawCollateral(loan.collateralAsset, loan.collateralAmount, loan.borrower, loan.borrower);

        loan.state = LoanState.CLOSED;
        emit LoanClosed(loanId);
    }

    // (removed) hashQuote: quote-based flow removed.

    /// @notice Return a loan record by id.
    function getLoan(uint256 loanId) external view returns (Loan memory) {
        return loans[loanId];
    }

    /// @notice Calculate the annualized origination fee amount for a loan.
    function calculateOriginationFee(uint256 loanId) external view returns (uint256) {
        Loan storage loan = loans[loanId];
        if (loan.state == LoanState.NONE) {
            revert CV_InvalidLoanState();
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
        if (newL2Recipient == address(0)) {
            revert CV_ZeroAddress();
        }
        l2Recipient = newL2Recipient;
        emit L2RecipientUpdated(newL2Recipient);
    }

    /// @notice Configure Socket bridge settings for an asset.
    function setSocketBridgeConfig(
        address asset,
        ISocketBridge bridge,
        ISocketConnector connector,
        uint256 msgGasLimit,
        bytes calldata options,
        bytes calldata extraData
    ) external onlyRole(PARAMETER_ROLE) {
        if (asset == address(0) || address(bridge) == address(0) || address(connector) == address(0)) {
            revert CV_ZeroAddress();
        }
        socketBridgeConfigs[asset] = SocketBridgeConfig({
            bridge: bridge, connector: connector, msgGasLimit: msgGasLimit, options: options, extraData: extraData
        });
        emit BridgeConfigUpdated(asset, address(bridge), address(connector), msgGasLimit, options, extraData);
    }

    /// @notice Update the Euler adapter.
    function setEulerAdapter(IEulerAdapter newAdapter) external onlyRole(PARAMETER_ROLE) {
        if (address(newAdapter) == address(0)) {
            revert CV_ZeroAddress();
        }
        eulerAdapter = newAdapter;
        emit EulerAdapterUpdated(address(newAdapter));
    }

    /// @notice Update the Derive subaccount id used for action validation.
    function setDeriveSubaccountId(uint256 subaccountId) external onlyRole(PARAMETER_ROLE) {
        if (subaccountId == 0) {
            revert CV_InvalidSubaccount();
        }
        deriveSubaccountId = subaccountId;
        emit SubaccountUpdated(subaccountId);
    }

    /// @notice Update collateral allowlist and strike scale.
    function setCollateralConfig(address asset, bool allowed, uint256 scale) external onlyRole(PARAMETER_ROLE) {
        if (asset == address(0)) {
            revert CV_ZeroAddress();
        }
        collateralAllowed[asset] = allowed;
        strikeScale[asset] = scale;
        emit CollateralConfigUpdated(asset, allowed, scale);
    }

    /// @notice Estimate the Socket bridge fees for a transfer.
    function estimateBridgeFees(address asset, address receiver, uint256 amount) public view returns (uint256) {
        SocketBridgeConfig storage config = socketBridgeConfigs[asset];
        if (address(config.bridge) == address(0) || address(config.connector) == address(0)) {
            revert CV_ZeroAddress();
        }
        bytes memory payload = abi.encode(receiver, amount, bytes32(0), config.extraData);
        return config.connector.getMinFees(config.msgGasLimit, payload.length);
    }

    /// @notice Update treasury configuration for settlement surplus.
    function setTreasuryConfig(address newTreasury, uint256 bps) external onlyRole(PARAMETER_ROLE) {
        if (newTreasury == address(0)) {
            revert CV_ZeroAddress();
        }
        if (bps > MAX_BPS) {
            revert CV_TreasuryBpsTooHigh();
        }
        treasury = newTreasury;
        treasuryBps = bps;
        emit TreasuryUpdated(newTreasury, bps);
    }

    /// @notice Update the annualized origination fee (1e18 precision).
    function setOriginationFeeApr(uint256 feeApr) external onlyRole(PARAMETER_ROLE) {
        originationFeeApr = feeApr;
        emit OriginationFeeAprUpdated(feeApr);
    }

    /// @notice Update the maximum total committed principal (0 disables the cap).
    function setMaxTotalPrincipal(uint256 maxPrincipal) external onlyRole(PARAMETER_ROLE) {
        maxTotalPrincipal = maxPrincipal;
        emit MaxTotalPrincipalUpdated(maxPrincipal);
    }

    /// @notice Allow or revoke an RFQ signer.
    function setRfqSigner(address signer, bool allowed) external onlyRole(PARAMETER_ROLE) {
        if (signer == address(0)) {
            revert CV_ZeroAddress();
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
        if (address(messenger) == address(0)) {
            revert CV_ZeroAddress();
        }
        lzMessenger = messenger;
        emit LZMessengerUpdated(address(messenger));
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
        uint256 scale = strikeScale[collateralAsset];
        if (scale == 0) {
            revert CV_StrikeScaleUnset();
        }
        uint256 expected = Math.mulDiv(collateralAmount, putStrike, scale);
        if (expected != borrowAmount) {
            revert CV_InvalidBorrowAmount();
        }
    }

    function _validatePermit(
        address collateralAsset,
        uint256 collateralAmount,
        IAllowanceTransfer.PermitSingle calldata permit
    ) internal view {
        if (permit.details.token != collateralAsset) {
            revert CV_PermitTokenMismatch();
        }
        if (permit.spender != address(this)) {
            revert CV_PermitSpenderMismatch();
        }
        if (permit.details.amount < collateralAmount) {
            revert CV_PermitAmountTooLow();
        }
    }

    function _quoteOriginationFee(uint256 borrowAmount, uint256 maturity) internal view returns (uint256) {
        if (originationFeeApr == 0) {
            return 0;
        }
        if (maturity <= block.timestamp) {
            return 0;
        }
        uint256 duration = maturity - block.timestamp;
        uint256 annualFee = Math.mulDiv(borrowAmount, originationFeeApr, 1e18);
        return Math.mulDiv(annualFee, duration, YEAR);
    }

    function _commitPrincipal(uint256 amount) internal {
        if (amount == 0) {
            return;
        }
        uint256 cap = maxTotalPrincipal;
        if (cap != 0 && totalCommittedPrincipal + amount > cap) {
            revert CV_TotalPrincipalCapExceeded();
        }
        totalCommittedPrincipal += amount;
    }

    function _releaseCommittedPrincipal(uint256 amount) internal {
        if (amount == 0) {
            return;
        }
        totalCommittedPrincipal -= amount;
    }

    function _requestCollateralDeposit(address borrower, DepositParams calldata params)
        internal
        returns (uint256 loanId, bytes32 socketMessageId, bytes32 lzGuid)
    {
        if (!collateralAllowed[params.collateralAsset]) {
            revert CV_CollateralNotAllowed();
        }
        if (params.collateralAmount == 0) {
            revert CV_InvalidAmount();
        }
        if (params.maturity <= block.timestamp) {
            revert CV_InvalidMaturity();
        }
        if (address(lzMessenger) == address(0)) {
            revert CV_LZMessengerNotSet();
        }
        if (deriveSubaccountId == 0) {
            revert CV_InvalidSubaccount();
        }

        loanId = nextLoanId++;
        pendingDeposits[loanId] = PendingDeposit({
            borrower: borrower,
            collateralAsset: params.collateralAsset,
            collateralAmount: params.collateralAmount,
            maturity: params.maturity,
            putStrike: params.putStrike,
            borrowAmount: params.borrowAmount
        });

        SocketBridgeConfig storage config = socketBridgeConfigs[params.collateralAsset];
        if (address(config.bridge) == address(0) || address(config.connector) == address(0)) {
            revert CV_ZeroAddress();
        }
        socketMessageId = config.connector.getMessageId();

        CollarLZMessages.Message memory message = CollarLZMessages.Message({
            action: CollarLZMessages.Action.DepositIntent,
            loanId: loanId,
            asset: params.collateralAsset,
            amount: params.collateralAmount,
            recipient: address(this),
            subaccountId: deriveSubaccountId,
            socketMessageId: socketMessageId,
            secondaryAmount: 0,
            quoteHash: bytes32(0),
            takerNonce: 0,
            data: bytes("")
        });

        bytes memory options = lzMessenger.defaultOptions();
        MessagingFee memory lzFee = lzMessenger.quoteMessage(message, options);
        uint256 bridgeFee = estimateBridgeFees(params.collateralAsset, l2Recipient, params.collateralAmount);
        uint256 requiredFee = bridgeFee + lzFee.nativeFee;
        if (msg.value < requiredFee) {
            revert CV_InsufficientBridgeFees();
        }

        _bridgeToL2(params.collateralAsset, params.collateralAmount, l2Recipient);
        MessagingReceipt memory receipt = lzMessenger.sendMessage{value: lzFee.nativeFee}(message);
        lzGuid = receipt.guid;

        if (msg.value > requiredFee) {
            (bool success,) = msg.sender.call{value: msg.value - requiredFee}("");
            if (!success) {
                revert CV_RefundFailed();
            }
        }

        emit CollateralDepositRequested(
            loanId, borrower, params.collateralAsset, params.collateralAmount, params.maturity, socketMessageId, lzGuid
        );
    }

    // (removed) _confirmLoanCreation/_openLoan: quote-based flow removed.

    function _convertToVariable(uint256 loanId, uint256 collateralAmount) internal {
        Loan storage loan = loans[loanId];
        if (collateralAmount != loan.collateralAmount) {
            revert CV_InvalidAmount();
        }
        _releaseCommittedPrincipal(loan.principal);
        IERC20(loan.collateralAsset).safeIncreaseAllowance(address(eulerAdapter), collateralAmount);
        eulerAdapter.depositCollateral(loan.collateralAsset, collateralAmount, loan.borrower);
        eulerAdapter.borrow(address(usdc), loan.principal, loan.borrower, address(this));
        usdc.safeIncreaseAllowance(address(liquidityVault), loan.principal);
        liquidityVault.repay(loan.principal);
        loan.state = LoanState.ACTIVE_VARIABLE;
        loan.variableDebt = loan.principal;
        emit LoanConverted(loanId, loan.variableDebt);
    }

    function _bridgeToL2(address asset, uint256 amount, address receiver) internal {
        SocketBridgeConfig storage config = socketBridgeConfigs[asset];
        if (address(config.bridge) == address(0) || address(config.connector) == address(0)) {
            revert CV_ZeroAddress();
        }
        uint256 fee = estimateBridgeFees(asset, receiver, amount);
        if (address(this).balance < fee) {
            revert CV_InsufficientBridgeFees();
        }
        IERC20(asset).safeIncreaseAllowance(address(config.bridge), amount);
        config.bridge.bridge{value: fee}(
            receiver, amount, config.msgGasLimit, address(config.connector), config.extraData, config.options
        );
    }

    function _loadLZMessage(bytes32 guid) internal view returns (CollarLZMessages.Message memory message) {
        if (address(lzMessenger) == address(0)) {
            revert CV_LZMessengerNotSet();
        }
        if (lzMessageConsumed[guid]) {
            revert CV_LZMessageConsumed();
        }

        message = lzMessenger.receivedMessage(guid);
        if (message.loanId == 0) {
            revert CV_LZMessageNotFound();
        }
    }

    function _peekLZMessage(bytes32 guid) internal view returns (CollarLZMessages.Message memory message) {
        if (address(lzMessenger) == address(0)) {
            revert CV_LZMessengerNotSet();
        }

        message = lzMessenger.receivedMessage(guid);
        if (message.loanId == 0) {
            revert CV_LZMessageNotFound();
        }
    }

    /// @notice Record that a trade was confirmed on L2 and mark collateral activated.
    function recordTradeConfirmed(bytes32 tradeGuid) external whenNotPaused {
        CollarLZMessages.Message memory lzMessage = _peekLZMessage(tradeGuid);
        if (lzMessage.action != CollarLZMessages.Action.TradeConfirmed) {
            revert CV_LZMessageMismatch();
        }
        if (lzMessage.recipient != address(this)) {
            revert CV_LZMessageRecipientMismatch();
        }
        if (deriveSubaccountId != 0 && lzMessage.subaccountId != deriveSubaccountId) {
            revert CV_LZMessageMismatch();
        }
        if (tradeConfirmed[lzMessage.loanId]) {
            revert CV_TradeAlreadyConfirmed();
        }
        tradeConfirmed[lzMessage.loanId] = true;
        collateralActivated[lzMessage.loanId] = true;
        returnRequested[lzMessage.loanId] = false;
        emit TradeConfirmedRecorded(lzMessage.loanId, tradeGuid);
    }

    function _consumeLZMessage(bytes32 guid) internal returns (CollarLZMessages.Message memory message) {
        message = _loadLZMessage(guid);
        lzMessageConsumed[guid] = true;
    }

    function _validateDepositConfirmed(
        CollarLZMessages.Message memory lzMessage,
        PendingDeposit memory pending,
        address expectedBorrower
    ) internal view returns (uint256 loanId) {
        if (lzMessage.action != CollarLZMessages.Action.DepositConfirmed) {
            revert CV_LZMessageMismatch();
        }
        loanId = lzMessage.loanId;
        if (lzMessage.recipient != address(this)) {
            revert CV_LZMessageRecipientMismatch();
        }
        if (deriveSubaccountId != 0 && lzMessage.subaccountId != deriveSubaccountId) {
            revert CV_LZMessageMismatch();
        }
        if (lzMessage.asset != pending.collateralAsset || lzMessage.amount != pending.collateralAmount) {
            revert CV_LZMessageMismatch();
        }
        if (pending.borrower == address(0)) {
            revert CV_PendingDepositNotFound();
        }
        if (pending.borrower != expectedBorrower) {
            revert CV_NotBorrower();
        }
    }

    function _validateOriginationFee(CollarLZMessages.Message memory lzMessage, uint256 borrowAmount, uint256 maturity)
        internal
        view
    {
        uint256 feeAmount = _quoteOriginationFee(borrowAmount, maturity);
        if (feeAmount == 0) {
            if (lzMessage.amount != 0) {
                revert CV_LZMessageMismatch();
            }
            return;
        }
        if (lzMessage.asset != address(usdc)) {
            revert CV_LZMessageMismatch();
        }
        if (lzMessage.amount != feeAmount) {
            revert CV_LZMessageMismatch();
        }
        if (lzMessage.socketMessageId == bytes32(0)) {
            revert CV_LZMessageMismatch();
        }
    }

    modifier onlyKeeperOrExecutor() {
        _onlyKeeperOrExecutor();
        _;
    }

    function _onlyKeeperOrExecutor() internal view {
        if (!(hasRole(KEEPER_ROLE, msg.sender) || hasRole(EXECUTOR_ROLE, msg.sender))) {
            revert CV_NotAuthorized();
        }
    }

    receive() external payable {}
}
