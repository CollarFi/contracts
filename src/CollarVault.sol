// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {AccessControlUpgradeable} from "openzeppelin-upgradeable/access/AccessControlUpgradeable.sol";
import {Initializable} from "openzeppelin-upgradeable/proxy/utils/Initializable.sol";
import {PausableUpgradeable} from "openzeppelin-upgradeable/utils/PausableUpgradeable.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {EIP712Upgradeable} from "openzeppelin-upgradeable/utils/cryptography/EIP712Upgradeable.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {IAllowanceTransfer} from "permit2/src/interfaces/IAllowanceTransfer.sol";

import {ILendingAdapter} from "./interfaces/ILendingAdapter.sol";
import {ILiquidityVault} from "./interfaces/ILiquidityVault.sol";
import {IMarginEngine} from "./interfaces/IMarginEngine.sol";
import {IMarginEngineRfqRouter} from "./interfaces/IMarginEngineRfqRouter.sol";
import {IVariableLoanPosition} from "./interfaces/IVariableLoanPosition.sol";

contract CollarVault is
    Initializable,
    AccessControlUpgradeable,
    PausableUpgradeable,
    ReentrancyGuard,
    EIP712Upgradeable
{
    using Clones for address;
    using SafeERC20 for IERC20;

    bytes32 public constant KEEPER_ROLE = keccak256("KEEPER_ROLE");
    bytes32 public constant PARAMETER_ROLE = keccak256("PARAMETER_ROLE");
    bytes32 public constant PAUSER_ROLE = keccak256("PAUSER_ROLE");
    bytes32 public constant RFQ_SIGNER_ROLE = keccak256("RFQ_SIGNER_ROLE");

    uint256 public constant YEAR = 365 days;
    uint256 public constant MAX_BPS = 10_000;

    bytes32 public constant BASELINE_RFQ_TYPEHASH = keccak256(
        "BaselineRfq(uint256 loanId,address collateralAsset,uint256 collateralAmount,uint64 maturity,uint256 putStrike,uint256 callStrike,uint256 borrowAmount,uint256 minNetInterest,uint64 rfqExpiry,address borrower,uint256 nonce)"
    );
    bytes32 public constant ROLLOVER_MANDATE_TYPEHASH = keccak256(
        "RolloverMandate(address borrower,uint256 loanId,uint64 newMaturity,uint256 minCallStrike,uint256 maxPutStrike,uint256 minNetInterest,uint64 deadline,uint256 nonce)"
    );

    enum LoanState {
        NONE,
        ACTIVE_ZERO_COST,
        READY_FOR_VARIABLE,
        ACTIVE_VARIABLE,
        CLOSED
    }

    enum SettlementOutcome {
        PutITM,
        Neutral,
        CallITM
    }

    struct DepositParams {
        address collateralAsset;
        uint256 collateralAmount;
        uint256 maturity;
        uint256 putStrike;
        uint256 borrowAmount;
    }

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

    struct RolloverMandate {
        address borrower;
        uint256 loanId;
        uint64 newMaturity;
        uint256 minCallStrike;
        uint256 maxPutStrike;
        uint256 minNetInterest;
        uint64 deadline;
        uint256 nonce;
    }

    struct PendingDeposit {
        address borrower;
        address collateralAsset;
        uint256 collateralAmount;
        uint256 maturity;
        uint256 putStrike;
        uint256 borrowAmount;
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
        uint256 minNetInterest;
        uint256 fixedInterest;
        uint256 maxRollLtv;
    }

    struct Loan {
        address borrower;
        address collateralAsset;
        uint256 collateralAmount;
        uint256 maturity;
        uint256 putStrike;
        uint256 callStrike;
        uint256 principal;
        LoanState state;
        uint256 startTime;
        uint256 interestApr;
        uint256 interestOwed;
        uint256 variableDebt;
        uint256 putBucketId;
        uint256 callBucketId;
        bytes32 putInstrumentId;
        bytes32 callInstrumentId;
    }

    struct FinalizeLoanParams {
        uint256 putBucketId;
        address callBuyer;
    }

    struct SettlementPreview {
        SettlementOutcome outcome;
        uint256 finalSpot;
        uint256 putPayout;
        uint256 collateralToBuyer;
        uint256 buyerPayment;
        uint256 totalSettlementValue;
    }

    struct RolloverQuoteResolution {
        uint256 newPutBucketId;
        uint256 newCallBucketId;
        bytes32 newPutInstrumentId;
        bytes32 newCallInstrumentId;
        uint256 newPutStrike;
        uint256 newCallStrike;
        bool newPutFromInventoryTransfer;
    }

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
        uint256 callBucketId,
        uint256 putBucketId
    );
    event LoanSettled(uint256 indexed loanId, SettlementOutcome outcome, uint256 settlementAmount);
    event SettlementShortfall(uint256 indexed loanId, uint256 shortfall);
    event LoanReadyForVariable(uint256 indexed loanId, uint256 requiredDebt);
    event LoanConverted(uint256 indexed loanId, uint256 variableDebt);
    event LoanClosed(uint256 indexed loanId);
    event TreasuryUpdated(address indexed treasury, uint256 bps);
    event OriginationFeeAprUpdated(uint256 feeApr);
    event MaxTotalPrincipalUpdated(uint256 maxTotalPrincipal);
    event MaxRollLtvUpdated(uint256 maxRollLtv);
    event MaxMandateDurationUpdated(uint64 maxMandateDuration);
    event ReadyLoanCloseGracePeriodUpdated(uint64 gracePeriod);
    event CollateralConfigUpdated(
        address indexed asset, bool allowed, uint256 strikeScale, address indexed engineAsset
    );
    event MarginEngineUpdated(address indexed marginEngine);
    event MarginEngineRfqRouterUpdated(address indexed marginEngineRfqRouter);
    event LendingAdapterUpdated(address indexed adapter);
    event VariableLoanPositionImplementationUpdated(address indexed implementation);
    event MandateAccepted(
        uint256 indexed loanId,
        address indexed borrower,
        uint64 maturity,
        uint256 borrowAmount,
        uint256 minCallStrike,
        uint256 maxPutStrike,
        uint256 minNetInterest,
        uint64 deadline
    );
    event DepositCreated(
        uint256 indexed loanId,
        address indexed borrower,
        address indexed collateralAsset,
        uint256 collateralAmount,
        uint256 maturity,
        uint256 putStrike,
        uint256 borrowAmount
    );
    event CollateralReturnRequested(
        uint256 indexed loanId, address indexed requester, address indexed collateralAsset, uint256 collateralAmount
    );
    event CollateralReturned(
        uint256 indexed loanId, address indexed borrower, address indexed collateralAsset, uint256 collateralAmount
    );
    event VariableCollateralWithdrawn(uint256 indexed loanId, uint256 amount);
    event RolloverCallBucketPrepared(
        uint256 indexed loanId, bytes32 indexed callInstrumentId, uint256 indexed callBucketId
    );
    event LoanRolledOver(
        uint256 indexed loanId,
        uint256 oldMaturity,
        uint256 newMaturity,
        uint256 oldInterestOwed,
        uint256 newInterestOwed,
        uint256 oldCallStrike,
        uint256 newCallStrike,
        uint256 oldPutStrike,
        uint256 newPutStrike,
        bytes32 quoteHash
    );

    ILiquidityVault private _liquidityVault;
    IERC20 private _usdc;
    IAllowanceTransfer private _permit2;
    IMarginEngine private _marginEngine;
    IMarginEngineRfqRouter private _marginEngineRfqRouter;
    ILendingAdapter private _lendingAdapter;
    address private _variableLoanPositionImplementation;
    address private _treasury;
    uint256 private _treasuryBps;
    uint256 private _originationFeeApr;
    uint256 private _maxTotalPrincipal;
    uint256 private _totalCommittedPrincipal;
    uint256 private _maxRollLtv;
    uint256 private _readyLoanKeeperPenaltyBps;
    uint64 private _maxMandateDuration;
    uint64 private _readyLoanCloseGracePeriod;
    uint256 private _nextLoanId;

    mapping(address => bool) private _collateralAllowed;
    mapping(address => uint256) private _strikeScale;
    mapping(address => address) private _engineAsset;

    mapping(uint256 => Loan) private _loans;
    mapping(uint256 => uint256) private _readyLoanSince;
    mapping(uint256 => address) private _variableLoanPositions;
    mapping(uint256 => PendingDeposit) private _pendingDeposits;
    mapping(uint256 => Mandate) private _mandates;
    mapping(bytes32 => bool) private _usedBaselineRfqs;
    mapping(bytes32 => bool) private _usedRolloverMandates;
    mapping(address => mapping(uint256 => bool)) private _usedRolloverMandateNonces;

    /// @notice Initialize the upgradeable vault.
    function initialize(
        address admin,
        ILiquidityVault liquidityVault_,
        ILendingAdapter lendingAdapter_,
        IAllowanceTransfer permit2_,
        address marginEngine_,
        address treasury_
    ) external initializer {
        if (
            admin == address(0) || address(liquidityVault_) == address(0) || address(lendingAdapter_) == address(0)
                || address(permit2_) == address(0) || marginEngine_ == address(0) || treasury_ == address(0)
        ) {
            revert CV_InvalidConfig();
        }

        __AccessControl_init();
        __Pausable_init();
        __EIP712_init("CollarVault", "1");

        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        _grantRole(KEEPER_ROLE, admin);
        _grantRole(PARAMETER_ROLE, admin);
        _grantRole(PAUSER_ROLE, admin);

        _liquidityVault = liquidityVault_;
        _lendingAdapter = lendingAdapter_;
        _permit2 = permit2_;
        _marginEngine = IMarginEngine(marginEngine_);
        _usdc = IERC20(liquidityVault_.asset());
        _treasury = treasury_;

        _maxRollLtv = 0.95e18;
        _maxMandateDuration = 7 days;
        _readyLoanCloseGracePeriod = 3 days;
        _nextLoanId = 1;
    }

    modifier onlyKeeper() {
        if (!hasRole(KEEPER_ROLE, msg.sender)) revert CV_Unauthorized();
        _;
    }

    /// @notice Return the liquidity vault.
    function liquidityVault() external view returns (ILiquidityVault) {
        return _liquidityVault;
    }

    /// @notice Return the USDC asset.
    function usdc() external view returns (IERC20) {
        return _usdc;
    }

    /// @notice Return the Permit2 contract.
    function permit2() external view returns (IAllowanceTransfer) {
        return _permit2;
    }

    /// @notice Return the same-network margin engine.
    function marginEngine() external view returns (IMarginEngine) {
        return _marginEngine;
    }

    /// @notice Return the same-network margin-engine RFQ router.
    function marginEngineRfqRouter() external view returns (IMarginEngineRfqRouter) {
        return _marginEngineRfqRouter;
    }

    /// @notice Return the lending adapter.
    function lendingAdapter() external view returns (ILendingAdapter) {
        return _lendingAdapter;
    }

    /// @notice Return the variable loan position implementation.
    function variableLoanPositionImplementation() external view returns (address) {
        return _variableLoanPositionImplementation;
    }

    /// @notice Return the treasury address.
    function treasury() external view returns (address) {
        return _treasury;
    }

    /// @notice Return the treasury fee bps.
    function treasuryBps() external view returns (uint256) {
        return _treasuryBps;
    }

    /// @notice Return the origination fee APR.
    function originationFeeApr() external view returns (uint256) {
        return _originationFeeApr;
    }

    /// @notice Return the max total committed principal.
    function maxTotalPrincipal() external view returns (uint256) {
        return _maxTotalPrincipal;
    }

    /// @notice Return total reserved and active zero-cost principal.
    function totalCommittedPrincipal() external view returns (uint256) {
        return _totalCommittedPrincipal;
    }

    /// @notice Return the current max roll LTV.
    function maxRollLtv() external view returns (uint256) {
        return _maxRollLtv;
    }

    /// @notice Return the next loan identifier.
    function nextLoanId() external view returns (uint256) {
        return _nextLoanId;
    }

    /// @notice Return the current max mandate duration.
    function maxMandateDuration() external view returns (uint64) {
        return _maxMandateDuration;
    }

    /// @notice Return the ready-loan grace period before keeper close.
    function readyLoanCloseGracePeriod() external view returns (uint64) {
        return _readyLoanCloseGracePeriod;
    }

    /// @notice Return whether collateral is enabled.
    function collateralAllowed(address asset) external view returns (bool) {
        return _collateralAllowed[asset];
    }

    /// @notice Return strike scale for a collateral asset.
    function strikeScale(address asset) external view returns (uint256) {
        return _strikeScale[asset];
    }

    /// @notice Return the engine-side asset mapping for a collateral asset.
    function l2MessageAsset(address asset) external view returns (address) {
        return _engineAsset[asset];
    }

    /// @notice Return a loan by identifier.
    function getLoan(uint256 loanId) external view returns (Loan memory) {
        return _loans[loanId];
    }

    /// @notice Return a pending deposit by identifier.
    function getPendingDeposit(uint256 loanId) external view returns (PendingDeposit memory) {
        return _pendingDeposits[loanId];
    }

    /// @notice Return a mandate by identifier.
    function getMandate(uint256 loanId) external view returns (Mandate memory) {
        return _mandates[loanId];
    }

    /// @notice Return when a ready loan entered READY_FOR_VARIABLE.
    function readyLoanSince(uint256 loanId) external view returns (uint256) {
        return _readyLoanSince[loanId];
    }

    /// @notice Return the dedicated variable loan position address, if any.
    function variableLoanPosition(uint256 loanId) external view returns (address) {
        return _variableLoanPositions[loanId];
    }

    /// @notice Set treasury configuration.
    function setTreasuryConfig(address treasury_, uint256 bps) external onlyRole(PARAMETER_ROLE) {
        if (treasury_ == address(0) || bps > MAX_BPS) revert CV_InvalidInput();
        _treasury = treasury_;
        _treasuryBps = bps;
        emit TreasuryUpdated(treasury_, bps);
    }

    /// @notice Set the origination APR used to compute fixed bullet interest.
    function setOriginationFeeApr(uint256 feeApr) external onlyRole(PARAMETER_ROLE) {
        _originationFeeApr = feeApr;
        emit OriginationFeeAprUpdated(feeApr);
    }

    /// @notice Cap aggregate committed principal across pending and active zero-cost loans.
    function setMaxTotalPrincipal(uint256 value) external onlyRole(PARAMETER_ROLE) {
        _maxTotalPrincipal = value;
        emit MaxTotalPrincipalUpdated(value);
    }

    /// @notice Set the roll-safe LTV buffer.
    function setMaxRollLtv(uint256 value) external onlyRole(PARAMETER_ROLE) {
        if (value == 0 || value >= 1e18) revert CV_InvalidInput();
        _maxRollLtv = value;
        emit MaxRollLtvUpdated(value);
    }

    /// @notice Set the max time between mandate acceptance and keeper finalization.
    function setMaxMandateDuration(uint64 value) external onlyRole(PARAMETER_ROLE) {
        if (value == 0) revert CV_InvalidInput();
        _maxMandateDuration = value;
        emit MaxMandateDurationUpdated(value);
    }

    /// @notice Set grace period before keeper close for ready loans.
    function setReadyLoanCloseGracePeriod(uint64 value) external onlyRole(PARAMETER_ROLE) {
        _readyLoanCloseGracePeriod = value;
        emit ReadyLoanCloseGracePeriodUpdated(value);
    }

    /// @notice Configure collateral support and strike scaling.
    function setCollateralConfig(address asset, bool allowed, uint256 scale, address engineAsset_)
        external
        onlyRole(PARAMETER_ROLE)
    {
        if (asset == address(0) || scale == 0) revert CV_InvalidInput();
        _collateralAllowed[asset] = allowed;
        _strikeScale[asset] = scale;
        _engineAsset[asset] = engineAsset_ == address(0) ? asset : engineAsset_;
        emit CollateralConfigUpdated(asset, allowed, scale, _engineAsset[asset]);
    }

    /// @notice Set the same-network margin engine.
    function setMarginEngine(IMarginEngine marginEngine_) external onlyRole(PARAMETER_ROLE) {
        if (address(marginEngine_) == address(0)) revert CV_InvalidConfig();
        _marginEngine = marginEngine_;
        emit MarginEngineUpdated(address(marginEngine_));
    }

    /// @notice Set the same-network margin-engine RFQ router.
    function setMarginEngineRfqRouter(IMarginEngineRfqRouter marginEngineRfqRouter_) external onlyRole(PARAMETER_ROLE) {
        if (address(marginEngineRfqRouter_) == address(0)) revert CV_InvalidConfig();
        _marginEngineRfqRouter = marginEngineRfqRouter_;
        emit MarginEngineRfqRouterUpdated(address(marginEngineRfqRouter_));
    }

    /// @notice Set the lending adapter used for READY -> variable conversion.
    function setLendingAdapter(ILendingAdapter adapter) external onlyRole(PARAMETER_ROLE) {
        if (address(adapter) == address(0)) revert CV_InvalidConfig();
        _lendingAdapter = adapter;
        emit LendingAdapterUpdated(address(adapter));
    }

    /// @notice Set the variable position implementation cloned for ready-loan conversion.
    function setVariableLoanPositionImplementation(address implementation) external onlyRole(PARAMETER_ROLE) {
        if (implementation == address(0)) revert CV_InvalidConfig();
        _variableLoanPositionImplementation = implementation;
        emit VariableLoanPositionImplementationUpdated(implementation);
    }

    /// @notice Pause borrower and settlement flows.
    function pause() external onlyRole(PAUSER_ROLE) {
        _pause();
    }

    /// @notice Unpause borrower and settlement flows.
    function unpause() external onlyRole(PAUSER_ROLE) {
        _unpause();
    }

    /// @notice Hash a baseline RFQ for EIP-712 signature recovery.
    function hashBaselineRfq(BaselineRfq calldata rfq) public view returns (bytes32) {
        return _hashTypedDataV4(
            keccak256(
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
                    rfq.rfqExpiry,
                    rfq.borrower,
                    rfq.nonce
                )
            )
        );
    }

    /// @notice Hash a rollover mandate for EIP-712 signature recovery.
    function hashRolloverMandate(RolloverMandate memory mandate) public view returns (bytes32) {
        return _hashTypedDataV4(
            keccak256(
                abi.encode(
                    ROLLOVER_MANDATE_TYPEHASH,
                    mandate.borrower,
                    mandate.loanId,
                    mandate.newMaturity,
                    mandate.minCallStrike,
                    mandate.maxPutStrike,
                    mandate.minNetInterest,
                    mandate.deadline,
                    mandate.nonce
                )
            )
        );
    }

    /// @notice Create the next covered-call bucket needed for a same-network rollover quote.
    function prepareRolloverCallBucket(uint256 loanId, uint64 newMaturity, uint256 newCallStrike)
        external
        onlyKeeper
        whenNotPaused
        returns (bytes32 callInstrumentId, uint256 callBucketId)
    {
        Loan storage loan = _loans[loanId];
        if (loan.state != LoanState.ACTIVE_ZERO_COST) revert CV_InvalidState();
        if (block.timestamp >= loan.maturity) revert CV_InvalidState();
        if (newCallStrike == 0 || newMaturity <= block.timestamp || newMaturity <= loan.maturity) {
            revert CV_InvalidInput();
        }

        callInstrumentId = _marginEngine.computeInstrumentId(
            _engineAsset[loan.collateralAsset],
            address(_usdc),
            _engineAsset[loan.collateralAsset],
            newMaturity,
            newCallStrike,
            IMarginEngine.OptionType.Call
        );
        _validateInstrument(callInstrumentId, loan.collateralAsset, newMaturity, newCallStrike, false);

        callBucketId = _marginEngine.createCoveredCallBucket(callInstrumentId);
        emit RolloverCallBucketPrepared(loanId, callInstrumentId, callBucketId);
    }

    /// @notice Execute a same-network rollover using a borrower mandate and a validated margin-engine RFQ quote.
    function executeRollover(
        uint256 loanId,
        RolloverMandate calldata mandate,
        bytes calldata mandateSig,
        IMarginEngineRfqRouter.Quote calldata quote,
        IMarginEngineRfqRouter.SignerSignature[] calldata signatures
    ) external nonReentrant whenNotPaused onlyKeeper returns (bytes32 quoteHash) {
        return _executeRollover(loanId, mandate, mandateSig, quote, signatures);
    }

    /// @notice Legacy rollover entrypoint retained for ABI compatibility.
    function rolloverLoan(uint256 loanId, bytes calldata quoteData, bytes calldata mandateData)
        external
        nonReentrant
        whenNotPaused
        onlyKeeper
        returns (bytes32 quoteHash)
    {
        (IMarginEngineRfqRouter.Quote memory quote, IMarginEngineRfqRouter.SignerSignature[] memory signatures) =
            abi.decode(quoteData, (IMarginEngineRfqRouter.Quote, IMarginEngineRfqRouter.SignerSignature[]));
        (RolloverMandate memory mandate, bytes memory mandateSig) = abi.decode(mandateData, (RolloverMandate, bytes));
        return _executeRollover(loanId, mandate, mandateSig, quote, signatures);
    }

    /// @notice Create a pending same-network loan request and accept a mandate atomically.
    function createDepositWithMandate(
        DepositParams calldata params,
        BaselineRfq calldata rfq,
        bytes calldata rfqSig,
        uint64 deadline
    ) external nonReentrant whenNotPaused returns (uint256 loanId) {
        IERC20(params.collateralAsset).safeTransferFrom(msg.sender, address(this), params.collateralAmount);
        loanId = _createPendingDeposit(msg.sender, params);
        _acceptMandate(loanId, rfq, rfqSig, deadline, true);
    }

    /// @notice Create a pending same-network loan request via Permit2 and accept a mandate atomically.
    function createDepositWithMandatePermit(
        DepositParams calldata params,
        BaselineRfq calldata rfq,
        bytes calldata rfqSig,
        uint64 deadline,
        IAllowanceTransfer.PermitSingle calldata permit,
        bytes calldata permitSig
    ) external nonReentrant whenNotPaused returns (uint256 loanId) {
        _validatePermit(params.collateralAsset, params.collateralAmount, permit);
        _permit2.permit(msg.sender, permit, permitSig);
        _permit2.transferFrom(msg.sender, address(this), uint160(params.collateralAmount), params.collateralAsset);

        loanId = _createPendingDeposit(msg.sender, params);
        _acceptMandate(loanId, rfq, rfqSig, deadline, true);
    }

    /// @notice Accept or refresh a borrower mandate tied to a keeper-signed baseline RFQ.
    function acceptMandate(uint256 loanId, BaselineRfq calldata rfq, bytes calldata rfqSig, uint64 deadline)
        external
        nonReentrant
        whenNotPaused
    {
        _acceptMandate(loanId, rfq, rfqSig, deadline, false);
    }

    /// @notice Finalize a pending loan directly against the same-network margin engine.
    function finalizeLoan(uint256 loanId, FinalizeLoanParams calldata params)
        external
        nonReentrant
        whenNotPaused
        onlyKeeper
        returns (uint256 finalizedLoanId)
    {
        PendingDeposit memory pending = _pendingDeposits[loanId];
        if (pending.borrower == address(0)) revert CV_NotFound();

        Mandate memory mandate = _mandates[loanId];
        if (mandate.borrower == address(0)) revert CV_NotFound();
        if (block.timestamp > mandate.deadline) revert CV_InvalidState();
        if (params.callBuyer == address(0) || params.putBucketId == 0) revert CV_InvalidInput();

        finalizedLoanId = loanId;

        bytes32 putInstrumentId = _marginEngine.computeInstrumentId(
            _engineAsset[pending.collateralAsset],
            address(_usdc),
            address(_usdc),
            uint64(pending.maturity),
            mandate.maxPutStrike,
            IMarginEngine.OptionType.Put
        );
        bytes32 callInstrumentId = _marginEngine.computeInstrumentId(
            _engineAsset[pending.collateralAsset],
            address(_usdc),
            _engineAsset[pending.collateralAsset],
            uint64(pending.maturity),
            mandate.minCallStrike,
            IMarginEngine.OptionType.Call
        );

        _validateInstrument(putInstrumentId, pending.collateralAsset, pending.maturity, mandate.maxPutStrike, true);
        _validateInstrument(callInstrumentId, pending.collateralAsset, pending.maturity, mandate.minCallStrike, false);
        _validatePutBucket(params.putBucketId, putInstrumentId, pending.collateralAmount);

        uint256 callBucketId = _marginEngine.createCoveredCallBucket(callInstrumentId);
        IERC20(pending.collateralAsset).safeIncreaseAllowance(address(_marginEngine), pending.collateralAmount);
        _marginEngine.issueCoveredCall(callBucketId, pending.collateralAmount, params.callBuyer, address(this));

        delete _pendingDeposits[loanId];
        delete _mandates[loanId];

        _loans[loanId] = Loan({
            borrower: mandate.borrower,
            collateralAsset: pending.collateralAsset,
            collateralAmount: pending.collateralAmount,
            maturity: pending.maturity,
            putStrike: mandate.maxPutStrike,
            callStrike: mandate.minCallStrike,
            principal: pending.borrowAmount,
            state: LoanState.ACTIVE_ZERO_COST,
            startTime: block.timestamp,
            interestApr: _originationFeeApr,
            interestOwed: mandate.fixedInterest,
            variableDebt: 0,
            putBucketId: params.putBucketId,
            callBucketId: callBucketId,
            putInstrumentId: putInstrumentId,
            callInstrumentId: callInstrumentId
        });

        _liquidityVault.borrowReserved(loanId, pending.borrowAmount);
        _usdc.safeTransfer(mandate.borrower, pending.borrowAmount);

        emit LoanCreated(
            loanId,
            mandate.borrower,
            pending.collateralAsset,
            pending.collateralAmount,
            pending.maturity,
            mandate.maxPutStrike,
            mandate.minCallStrike,
            pending.borrowAmount,
            callBucketId,
            params.putBucketId
        );
    }

    /// @notice Cancel a pending loan and return collateral once no live mandate remains.
    function requestCollateralReturn(uint256 loanId) external nonReentrant whenNotPaused {
        PendingDeposit memory pending = _pendingDeposits[loanId];
        if (pending.borrower == address(0)) revert CV_NotFound();
        if (pending.borrower != msg.sender) revert CV_Unauthorized();

        Mandate memory mandate = _mandates[loanId];
        if (mandate.borrower != address(0) && block.timestamp <= mandate.deadline) {
            revert CV_InvalidState();
        }

        delete _pendingDeposits[loanId];
        if (mandate.borrower != address(0)) {
            delete _mandates[loanId];
            _cancelCommittedPrincipal(loanId, pending.borrowAmount);
        }

        IERC20(pending.collateralAsset).safeTransfer(pending.borrower, pending.collateralAmount);
        emit CollateralReturnRequested(loanId, msg.sender, pending.collateralAsset, pending.collateralAmount);
        emit CollateralReturned(loanId, pending.borrower, pending.collateralAsset, pending.collateralAmount);
    }

    /// @notice Preview deterministic same-network settlement against the finalized engine oracle price.
    function previewSettlement(uint256 loanId) public view returns (SettlementPreview memory preview) {
        Loan memory loan = _loans[loanId];
        if (loan.state != LoanState.ACTIVE_ZERO_COST) revert CV_InvalidState();

        (bool putFinalized, uint256 putFinalSpot,) = _marginEngine.getInstrumentSettlementState(loan.putInstrumentId);
        (bool callFinalized, uint256 callFinalSpot,) = _marginEngine.getInstrumentSettlementState(loan.callInstrumentId);
        if (!putFinalized || !callFinalized || putFinalSpot != callFinalSpot) revert CV_InvalidState();

        preview.finalSpot = putFinalSpot;
        if (preview.finalSpot < loan.putStrike) {
            preview.outcome = SettlementOutcome.PutITM;
            preview.putPayout = Math.mulDiv(
                loan.collateralAmount, loan.putStrike - preview.finalSpot, _strikeScale[loan.collateralAsset]
            );
            preview.collateralToBuyer = loan.collateralAmount;
        } else if (preview.finalSpot > loan.callStrike) {
            preview.outcome = SettlementOutcome.CallITM;
            preview.collateralToBuyer = preview.finalSpot == 0
                ? loan.collateralAmount
                : Math.mulDiv(loan.collateralAmount, loan.callStrike, preview.finalSpot);
        } else {
            preview.outcome = SettlementOutcome.Neutral;
            preview.collateralToBuyer = 0;
        }

        if (preview.collateralToBuyer != 0) {
            preview.buyerPayment =
                Math.mulDiv(preview.collateralToBuyer, preview.finalSpot, _strikeScale[loan.collateralAsset]);
        }
        preview.totalSettlementValue = preview.putPayout + preview.buyerPayment;
    }

    /// @notice Settle an active zero-cost loan directly against the local margin engine.
    function settleLoan(uint256 loanId, SettlementOutcome expectedOutcome) external nonReentrant whenNotPaused {
        Loan storage loan = _loans[loanId];
        if (loan.state != LoanState.ACTIVE_ZERO_COST) revert CV_InvalidState();
        if (block.timestamp < loan.maturity) revert CV_InvalidState();

        _finalizeInstrumentIfNeeded(loan.putInstrumentId);
        _finalizeInstrumentIfNeeded(loan.callInstrumentId);
        _settleBucketIfNeeded(loan.putBucketId);
        _settleBucketIfNeeded(loan.callBucketId);

        SettlementPreview memory preview = previewSettlement(loanId);
        if (preview.outcome != expectedOutcome) revert CV_InvalidInput();

        if (preview.outcome == SettlementOutcome.Neutral) {
            _marginEngine.redeemCappedUnderlying(loan.callBucketId, loan.collateralAmount, address(this));
            _markReadyForVariable(loanId, loan.collateralAmount);
            emit LoanSettled(loanId, preview.outcome, 0);
            return;
        }

        uint256 usdcBalanceBefore = _usdc.balanceOf(address(this));
        uint256 collateralBalanceBefore = IERC20(loan.collateralAsset).balanceOf(address(this));

        _marginEngine.redeemCappedUnderlying(loan.callBucketId, loan.collateralAmount, address(this));
        if (preview.putPayout != 0) {
            _marginEngine.redeemPut(loan.putBucketId, loan.collateralAmount, address(this));
        }

        uint256 collateralReceived = IERC20(loan.collateralAsset).balanceOf(address(this)) - collateralBalanceBefore;
        uint256 usdcReceived = _usdc.balanceOf(address(this)) - usdcBalanceBefore;
        if (collateralReceived < preview.collateralToBuyer || usdcReceived < preview.putPayout) {
            revert CV_InvalidState();
        }

        if (preview.buyerPayment != 0) {
            _usdc.safeTransferFrom(msg.sender, address(this), preview.buyerPayment);
            IERC20(loan.collateralAsset).safeTransfer(msg.sender, preview.collateralToBuyer);
        }

        uint256 settlementAmount = preview.totalSettlementValue;
        uint256 totalDue = loan.principal + loan.interestOwed;
        if (settlementAmount < totalDue) revert CV_InsufficientValue();

        _decreaseCommittedPrincipal(loan.principal);

        _usdc.safeIncreaseAllowance(address(_liquidityVault), loan.principal);
        _liquidityVault.repay(loan.principal);
        if (loan.interestOwed != 0) {
            _usdc.safeTransfer(address(_liquidityVault), loan.interestOwed);
        }

        uint256 excess = settlementAmount - totalDue;
        if (excess != 0) {
            if (preview.outcome == SettlementOutcome.PutITM) {
                uint256 treasuryCut = Math.mulDiv(excess, _treasuryBps, MAX_BPS);
                uint256 vaultCut = excess - treasuryCut;
                if (treasuryCut != 0) _usdc.safeTransfer(_treasury, treasuryCut);
                if (vaultCut != 0) _usdc.safeTransfer(address(_liquidityVault), vaultCut);
            } else {
                _usdc.safeTransfer(loan.borrower, excess);
            }
        }

        loan.state = LoanState.CLOSED;
        emit LoanSettled(loanId, preview.outcome, settlementAmount);
        emit LoanClosed(loanId);
    }

    /// @notice Attempt to convert a ready loan into a variable-rate position.
    function tryConvertReadyLoan(uint256 loanId) external nonReentrant whenNotPaused returns (bool converted) {
        Loan storage loan = _loans[loanId];
        if (loan.state != LoanState.READY_FOR_VARIABLE) revert CV_InvalidState();
        return _convertToVariableIfLiquid(loanId);
    }

    /// @notice Allow anyone to close a ready loan by repaying the due debt.
    function settleReadyLoanByRepay(uint256 loanId) external nonReentrant whenNotPaused {
        Loan storage loan = _loans[loanId];
        if (loan.state != LoanState.READY_FOR_VARIABLE) revert CV_InvalidState();

        uint256 since = _readyLoanSince[loanId];
        if (since == 0) revert CV_InvalidState();

        uint256 totalDue = loan.principal + loan.interestOwed;
        _usdc.safeTransferFrom(msg.sender, address(this), totalDue);

        uint256 callerCollateral;
        uint256 borrowerCollateral = loan.collateralAmount;
        if (block.timestamp > since + _readyLoanCloseGracePeriod) {
            uint256 baseCollateral =
                Math.mulDiv(totalDue, _strikeScale[loan.collateralAsset], loan.putStrike, Math.Rounding.Ceil);
            callerCollateral =
                Math.mulDiv(baseCollateral, MAX_BPS + _readyLoanKeeperPenaltyBps, MAX_BPS, Math.Rounding.Ceil);
            if (callerCollateral > borrowerCollateral) callerCollateral = borrowerCollateral;
            borrowerCollateral -= callerCollateral;
        }

        _decreaseCommittedPrincipal(loan.principal);
        delete _readyLoanSince[loanId];
        delete _variableLoanPositions[loanId];

        _usdc.safeIncreaseAllowance(address(_liquidityVault), loan.principal);
        _liquidityVault.repay(loan.principal);
        if (loan.interestOwed != 0) _usdc.safeTransfer(address(_liquidityVault), loan.interestOwed);
        if (callerCollateral != 0) IERC20(loan.collateralAsset).safeTransfer(msg.sender, callerCollateral);
        if (borrowerCollateral != 0) IERC20(loan.collateralAsset).safeTransfer(loan.borrower, borrowerCollateral);

        loan.state = LoanState.CLOSED;
        emit LoanClosed(loanId);
    }

    /// @notice Repay an active variable loan.
    function repayVariableLoan(uint256 loanId, uint256 amount)
        external
        nonReentrant
        whenNotPaused
        returns (uint256 repaid, bool closed)
    {
        Loan storage loan = _loans[loanId];
        if (loan.state != LoanState.ACTIVE_VARIABLE) revert CV_InvalidState();

        address position = _variableLoanPositions[loanId];
        if (position == address(0)) revert CV_InvalidState();

        _usdc.safeTransferFrom(msg.sender, address(this), amount);
        uint256 debt = IVariableLoanPosition(position).currentDebt();
        repaid = amount > debt ? debt : amount;
        _usdc.safeIncreaseAllowance(position, repaid);
        IVariableLoanPosition(position).repay(repaid, address(this));

        uint256 remainingDebt = IVariableLoanPosition(position).currentDebt();
        uint256 remainingCollateral = IVariableLoanPosition(position).currentCollateral();
        loan.variableDebt = remainingDebt;
        loan.collateralAmount = remainingCollateral;
        if (amount > repaid) {
            _usdc.safeTransfer(msg.sender, amount - repaid);
        }

        if (remainingDebt == 0 && remainingCollateral == 0) {
            loan.state = LoanState.CLOSED;
            emit LoanClosed(loanId);
            closed = true;
        }
    }

    /// @notice Withdraw collateral from an active variable loan.
    function withdrawVariableCollateral(uint256 loanId, uint256 amount)
        external
        nonReentrant
        whenNotPaused
        returns (uint256 withdrawn, bool closed)
    {
        Loan storage loan = _loans[loanId];
        if (loan.state != LoanState.ACTIVE_VARIABLE) revert CV_InvalidState();
        if (msg.sender != loan.borrower) revert CV_Unauthorized();

        address position = _variableLoanPositions[loanId];
        if (position == address(0)) revert CV_InvalidState();

        uint256 collateralBefore = IVariableLoanPosition(position).currentCollateral();
        if (amount > collateralBefore) revert CV_InvalidInput();

        IVariableLoanPosition(position).withdraw(amount, loan.borrower);

        uint256 liveDebt = IVariableLoanPosition(position).currentDebt();
        uint256 collateralAfter = IVariableLoanPosition(position).currentCollateral();
        loan.variableDebt = liveDebt;
        loan.collateralAmount = collateralAfter;
        withdrawn = collateralBefore - collateralAfter;
        emit VariableCollateralWithdrawn(loanId, withdrawn);

        if (liveDebt == 0 && collateralAfter == 0) {
            loan.state = LoanState.CLOSED;
            emit LoanClosed(loanId);
            closed = true;
        }
    }

    function _executeRollover(
        uint256 loanId,
        RolloverMandate memory mandate,
        bytes memory mandateSig,
        IMarginEngineRfqRouter.Quote memory quote,
        IMarginEngineRfqRouter.SignerSignature[] memory signatures
    ) internal returns (bytes32 quoteHash) {
        IMarginEngineRfqRouter router = _marginEngineRfqRouter;
        if (address(router) == address(0)) revert CV_InvalidConfig();

        Loan storage loan = _loans[loanId];
        if (loan.state != LoanState.ACTIVE_ZERO_COST) revert CV_InvalidState();
        if (block.timestamp >= loan.maturity) revert CV_InvalidState();
        if (mandate.loanId != loanId || mandate.borrower != loan.borrower) revert CV_InvalidMessage();
        if (mandate.deadline < block.timestamp || mandate.newMaturity <= block.timestamp) revert CV_InvalidState();
        if (mandate.newMaturity <= loan.maturity) revert CV_InvalidInput();

        bytes32 mandateHash = hashRolloverMandate(mandate);
        if (_usedRolloverMandateNonces[loan.borrower][mandate.nonce] || _usedRolloverMandates[mandateHash]) {
            revert CV_InvalidMessage();
        }
        address signer = ECDSA.recover(mandateHash, mandateSig);
        if (signer != loan.borrower) revert CV_Unauthorized();

        RolloverQuoteResolution memory resolution = _validateRolloverQuote(loan, mandate, quote);
        Loan memory oldLoan = loan;

        uint256 accruedInterest =
            _quoteInterest(oldLoan.principal, oldLoan.interestApr, oldLoan.startTime, block.timestamp);
        uint256 remainingOldInterest =
            oldLoan.interestOwed > accruedInterest ? oldLoan.interestOwed - accruedInterest : 0;
        uint256 newInterest =
            _quoteInterest(oldLoan.principal, _originationFeeApr, block.timestamp, mandate.newMaturity);
        _enforceRollSafetyLtv(
            oldLoan.collateralAsset,
            oldLoan.collateralAmount,
            resolution.newPutStrike,
            oldLoan.principal + remainingOldInterest + newInterest,
            _maxRollLtv
        );

        _usedRolloverMandates[mandateHash] = true;
        _usedRolloverMandateNonces[loan.borrower][mandate.nonce] = true;

        int256 realizedC;
        (quoteHash, realizedC) = _executeRolloverQuote(router, oldLoan, quote, signatures);
        if (realizedC < 0 || int256(newInterest) + realizedC < int256(mandate.minNetInterest)) {
            revert CV_InsufficientValue();
        }
        if (realizedC > 0) {
            _usdc.safeTransfer(address(_liquidityVault), uint256(realizedC));
        }

        _validatePostRolloverState(loan, resolution);
        _validateOldPositionsCleared(oldLoan);

        loan.maturity = mandate.newMaturity;
        loan.putStrike = resolution.newPutStrike;
        loan.callStrike = resolution.newCallStrike;
        loan.startTime = block.timestamp;
        loan.interestApr = _originationFeeApr;
        loan.interestOwed = remainingOldInterest + newInterest;
        loan.putBucketId = resolution.newPutBucketId;
        loan.callBucketId = resolution.newCallBucketId;
        loan.putInstrumentId = resolution.newPutInstrumentId;
        loan.callInstrumentId = resolution.newCallInstrumentId;

        emit LoanRolledOver(
            loanId,
            oldLoan.maturity,
            mandate.newMaturity,
            oldLoan.interestOwed,
            loan.interestOwed,
            oldLoan.callStrike,
            resolution.newCallStrike,
            oldLoan.putStrike,
            resolution.newPutStrike,
            quoteHash
        );
    }

    function _executeRolloverQuote(
        IMarginEngineRfqRouter router,
        Loan memory loan,
        IMarginEngineRfqRouter.Quote memory quote,
        IMarginEngineRfqRouter.SignerSignature[] memory signatures
    ) internal returns (bytes32 quoteHash, int256 realizedC) {
        uint256 grossPremiumVolume;
        for (uint256 index = 0; index < quote.actions.length; ++index) {
            grossPremiumVolume += quote.actions[index].quoteAmount;
        }

        (address oldLongPutToken,) = _marginEngine.getBucketTokens(loan.putBucketId);
        (, address oldCappedToken) = _marginEngine.getBucketTokens(loan.callBucketId);
        IERC20(oldLongPutToken).safeIncreaseAllowance(address(router), loan.collateralAmount);
        IERC20(oldCappedToken).safeIncreaseAllowance(address(router), loan.collateralAmount);
        if (grossPremiumVolume != 0) {
            _usdc.safeIncreaseAllowance(address(router), grossPremiumVolume * 2);
        }
        IERC20(loan.collateralAsset).safeIncreaseAllowance(address(_marginEngine), loan.collateralAmount);

        uint256 usdcBalanceBefore = _usdc.balanceOf(address(this));
        quoteHash = router.executeRfq(quote, signatures, IMarginEngineRfqRouter.ExecutionParams({taker: address(this)}));
        uint256 usdcBalanceAfter = _usdc.balanceOf(address(this));
        realizedC = int256(usdcBalanceAfter) - int256(usdcBalanceBefore);
    }

    function _validateRolloverQuote(
        Loan storage loan,
        RolloverMandate memory mandate,
        IMarginEngineRfqRouter.Quote memory quote
    ) internal view returns (RolloverQuoteResolution memory resolution) {
        if (quote.quoteAsset != address(_usdc) || quote.validUntil < block.timestamp) revert CV_InvalidMessage();
        if (quote.taker != address(this) || quote.authorizedExecutor != address(this)) revert CV_InvalidMessage();
        if (quote.actions.length != 4) revert CV_InvalidMessage();

        _validatePutBucket(loan.putBucketId, loan.putInstrumentId, loan.collateralAmount);
        _validateActiveCallBucket(loan);

        _validateOldPutRolloverAction(loan, quote.actions[0]);
        _validateOldCallRolloverAction(loan, quote.actions[1]);

        resolution.newCallBucketId = quote.actions[2].bucketId;
        resolution.newCallInstrumentId = quote.actions[2].instrumentId;
        resolution.newCallStrike = _resolveNewCallRolloverAction(loan, mandate, quote.actions[2]);

        if (
            quote.actions[3].side != IMarginEngineRfqRouter.Side.Buy
                || quote.actions[3].instrumentType != IMarginEngineRfqRouter.InstrumentType.Put
                || quote.actions[3].quantity != loan.collateralAmount || quote.actions[3].longRecipient != address(this)
                || quote.actions[3].cappedRecipient != address(0) || quote.actions[3].cappedSource != address(0)
                || quote.actions[3].collateralRecipient != address(0)
        ) revert CV_InvalidMessage();
        if (quote.actions[3].fulfillmentType == IMarginEngineRfqRouter.FulfillmentType.Mint) {
            if (quote.actions[3].longSource != address(0)) revert CV_InvalidMessage();
            resolution.newPutFromInventoryTransfer = false;
        } else if (quote.actions[3].fulfillmentType == IMarginEngineRfqRouter.FulfillmentType.Transfer) {
            if (quote.actions[3].longSource == address(0) || quote.actions[3].maker != quote.actions[3].longSource) {
                revert CV_InvalidMessage();
            }
            resolution.newPutFromInventoryTransfer = true;
        } else {
            revert CV_InvalidMessage();
        }

        resolution.newPutBucketId = quote.actions[3].bucketId;
        resolution.newPutInstrumentId = quote.actions[3].instrumentId;
        resolution.newPutStrike = _resolveNewPutRolloverAction(loan, mandate, quote.actions[3]);
    }

    function _validatePostRolloverState(Loan storage loan, RolloverQuoteResolution memory resolution) internal view {
        _validateRolloverPutPosition(
            resolution.newPutBucketId,
            resolution.newPutInstrumentId,
            loan.collateralAmount,
            resolution.newPutFromInventoryTransfer
        );

        (bytes32 bucketInstrumentId, IMarginEngine.BucketType bucketType, address owner, bool settled, bool closed) =
            _marginEngine.getBucketMetadata(resolution.newCallBucketId);
        (uint256 collateralBalance, uint256 outstandingQuantity, address longCallToken, address cappedToken) =
            _marginEngine.getCoveredCallBucketState(resolution.newCallBucketId);
        bucketType;
        if (
            owner != address(this) || bucketInstrumentId != resolution.newCallInstrumentId
                || collateralBalance != loan.collateralAmount || outstandingQuantity != loan.collateralAmount || settled
                || closed || IERC20(longCallToken).balanceOf(address(this)) != 0
                || IERC20(cappedToken).balanceOf(address(this)) != loan.collateralAmount
        ) {
            revert CV_InvalidState();
        }
    }

    function _validateActiveCallBucket(Loan storage loan) internal view {
        (bytes32 bucketInstrumentId, IMarginEngine.BucketType bucketType, address owner, bool settled, bool closed) =
            _marginEngine.getBucketMetadata(loan.callBucketId);
        (uint256 collateralBalance, uint256 outstandingQuantity, address longCallToken, address cappedToken) =
            _marginEngine.getCoveredCallBucketState(loan.callBucketId);
        bucketType;
        if (
            owner != address(this) || bucketInstrumentId != loan.callInstrumentId
                || collateralBalance != loan.collateralAmount || outstandingQuantity != loan.collateralAmount || settled
                || closed || IERC20(longCallToken).balanceOf(address(this)) != 0
                || IERC20(cappedToken).balanceOf(address(this)) != loan.collateralAmount
        ) {
            revert CV_InvalidState();
        }
    }

    function _validateOldPositionsCleared(Loan memory loan) internal view {
        (address oldLongPutToken,) = _marginEngine.getBucketTokens(loan.putBucketId);
        if (IERC20(oldLongPutToken).balanceOf(address(this)) != 0) revert CV_InvalidState();

        (bytes32 bucketInstrumentId, IMarginEngine.BucketType bucketType, address owner, bool settled, bool closed) =
            _marginEngine.getBucketMetadata(loan.callBucketId);
        (uint256 collateralBalance, uint256 outstandingQuantity, address longCallToken, address cappedToken) =
            _marginEngine.getCoveredCallBucketState(loan.callBucketId);
        bucketType;
        longCallToken;
        if (
            owner != address(this) || bucketInstrumentId != loan.callInstrumentId || settled || closed
                || collateralBalance != 0 || outstandingQuantity != 0
                || IERC20(cappedToken).balanceOf(address(this)) != 0
        ) {
            revert CV_InvalidState();
        }
    }

    function _validateOldPutRolloverAction(Loan storage loan, IMarginEngineRfqRouter.Action memory oldPutAction)
        internal
        view
    {
        if (
            oldPutAction.side != IMarginEngineRfqRouter.Side.Sell
                || oldPutAction.instrumentType != IMarginEngineRfqRouter.InstrumentType.Put
                || oldPutAction.fulfillmentType != IMarginEngineRfqRouter.FulfillmentType.Transfer
                || oldPutAction.bucketId != loan.putBucketId || oldPutAction.instrumentId != loan.putInstrumentId
                || oldPutAction.quantity != loan.collateralAmount || oldPutAction.longSource != address(this)
                || oldPutAction.longRecipient == address(0) || oldPutAction.maker != oldPutAction.longRecipient
                || oldPutAction.cappedRecipient != address(0) || oldPutAction.cappedSource != address(0)
                || oldPutAction.collateralRecipient != address(0)
        ) revert CV_InvalidMessage();
    }

    function _validateOldCallRolloverAction(Loan storage loan, IMarginEngineRfqRouter.Action memory oldCallAction)
        internal
        view
    {
        if (
            oldCallAction.side != IMarginEngineRfqRouter.Side.Buy
                || oldCallAction.instrumentType != IMarginEngineRfqRouter.InstrumentType.Call
                || oldCallAction.fulfillmentType != IMarginEngineRfqRouter.FulfillmentType.Burn
                || oldCallAction.bucketId != loan.callBucketId || oldCallAction.instrumentId != loan.callInstrumentId
                || oldCallAction.quantity != loan.collateralAmount || oldCallAction.longRecipient != address(0)
                || oldCallAction.longSource == address(0) || oldCallAction.maker != oldCallAction.longSource
                || oldCallAction.cappedRecipient != address(0) || oldCallAction.cappedSource != address(this)
                || oldCallAction.collateralRecipient != address(this)
        ) revert CV_InvalidMessage();
    }

    function _resolveNewCallRolloverAction(
        Loan storage loan,
        RolloverMandate memory mandate,
        IMarginEngineRfqRouter.Action memory newCallAction
    ) internal view returns (uint256 newCallStrike) {
        if (
            newCallAction.side != IMarginEngineRfqRouter.Side.Sell
                || newCallAction.instrumentType != IMarginEngineRfqRouter.InstrumentType.Call
                || newCallAction.fulfillmentType != IMarginEngineRfqRouter.FulfillmentType.Mint
                || newCallAction.quantity != loan.collateralAmount || newCallAction.longRecipient == address(0)
                || newCallAction.maker != newCallAction.longRecipient || newCallAction.longSource != address(0)
                || newCallAction.cappedRecipient != address(this) || newCallAction.cappedSource != address(0)
                || newCallAction.collateralRecipient != address(0)
        ) revert CV_InvalidMessage();

        (bytes32 bucketInstrumentId, IMarginEngine.BucketType bucketType, address owner, bool settled, bool closed) =
            _marginEngine.getBucketMetadata(newCallAction.bucketId);
        (uint256 collateralBalance, uint256 outstandingQuantity, address longCallToken, address cappedToken) =
            _marginEngine.getCoveredCallBucketState(newCallAction.bucketId);
        bucketType;
        longCallToken;
        cappedToken;
        if (
            owner != address(this) || bucketInstrumentId != newCallAction.instrumentId || collateralBalance != 0
                || outstandingQuantity != 0 || settled || closed
        ) revert CV_InvalidState();

        (
            address underlying,
            address quoteAsset,
            address collateralAsset,
            uint64 expiry,
            uint256 strike,
            uint256 quantityScale,
            IMarginEngine.OptionType optionType,
            bool exists
        ) = _marginEngine.getInstrumentMetadata(newCallAction.instrumentId);
        quantityScale;
        if (
            !exists || quoteAsset != address(_usdc) || expiry != mandate.newMaturity
                || optionType != IMarginEngine.OptionType.Call || underlying != _engineAsset[loan.collateralAsset]
                || collateralAsset != _engineAsset[loan.collateralAsset] || strike < mandate.minCallStrike
        ) revert CV_InvalidMessage();
        return strike;
    }

    function _resolveNewPutRolloverAction(
        Loan storage loan,
        RolloverMandate memory mandate,
        IMarginEngineRfqRouter.Action memory newPutAction
    ) internal view returns (uint256 newPutStrike) {
        (bytes32 bucketInstrumentId, IMarginEngine.BucketType bucketType, address owner, bool settled, bool closed) =
            _marginEngine.getBucketMetadata(newPutAction.bucketId);
        (uint256 collateralBalance, uint256 outstandingQuantity, address longToken) =
            _marginEngine.getPutBucketState(newPutAction.bucketId);
        bucketType;
        collateralBalance;
        outstandingQuantity;
        longToken;
        if (owner == address(0) || bucketInstrumentId != newPutAction.instrumentId || settled || closed) {
            revert CV_InvalidState();
        }

        (
            address underlying,
            address quoteAsset,
            address collateralAsset,
            uint64 expiry,
            uint256 strike,
            uint256 quantityScale,
            IMarginEngine.OptionType optionType,
            bool exists
        ) = _marginEngine.getInstrumentMetadata(newPutAction.instrumentId);
        quantityScale;
        if (
            !exists || quoteAsset != address(_usdc) || expiry != mandate.newMaturity
                || optionType != IMarginEngine.OptionType.Put || underlying != _engineAsset[loan.collateralAsset]
                || collateralAsset != address(_usdc) || strike > mandate.maxPutStrike
        ) revert CV_InvalidMessage();
        return strike;
    }

    function _createPendingDeposit(address borrower, DepositParams calldata params) internal returns (uint256 loanId) {
        if (!_collateralAllowed[params.collateralAsset]) revert CV_InvalidConfig();
        if (params.collateralAmount == 0 || params.maturity <= block.timestamp) revert CV_InvalidInput();
        _validateBorrowRequest(params.collateralAsset, params.putStrike, params.borrowAmount);

        loanId = _nextLoanId++;
        _pendingDeposits[loanId] = PendingDeposit({
            borrower: borrower,
            collateralAsset: params.collateralAsset,
            collateralAmount: params.collateralAmount,
            maturity: params.maturity,
            putStrike: params.putStrike,
            borrowAmount: params.borrowAmount
        });

        emit DepositCreated(
            loanId,
            borrower,
            params.collateralAsset,
            params.collateralAmount,
            params.maturity,
            params.putStrike,
            params.borrowAmount
        );
    }

    function _acceptMandate(
        uint256 loanId,
        BaselineRfq calldata rfq,
        bytes calldata rfqSig,
        uint64 deadline,
        bool internalFlow
    ) internal {
        PendingDeposit memory pending = _pendingDeposits[loanId];
        if (pending.borrower == address(0)) revert CV_NotFound();
        if (pending.borrower != msg.sender) revert CV_Unauthorized();
        if (deadline <= block.timestamp || deadline > block.timestamp + _maxMandateDuration) revert CV_InvalidState();

        Mandate memory existing = _mandates[loanId];
        bool hadMandate = existing.borrower != address(0);
        if (hadMandate && block.timestamp < existing.deadline) revert CV_InvalidState();

        if (internalFlow) {
            if (rfq.loanId != 0 && rfq.loanId != loanId) revert CV_InvalidMessage();
        } else if (rfq.loanId != loanId) {
            revert CV_InvalidMessage();
        }
        if (rfq.borrower != address(0) && rfq.borrower != pending.borrower) revert CV_Unauthorized();
        if (rfq.rfqExpiry < block.timestamp) revert CV_InvalidState();
        if (
            rfq.collateralAsset != pending.collateralAsset || rfq.collateralAmount != pending.collateralAmount
                || rfq.maturity != uint64(pending.maturity) || rfq.putStrike != pending.putStrike
                || rfq.borrowAmount != pending.borrowAmount || rfq.callStrike == 0
        ) {
            revert CV_InvalidMessage();
        }

        bytes32 rfqHash = hashBaselineRfq(rfq);
        if (_usedBaselineRfqs[rfqHash]) revert CV_InvalidMessage();
        address signer = ECDSA.recover(rfqHash, rfqSig);
        if (!hasRole(RFQ_SIGNER_ROLE, signer)) revert CV_Unauthorized();
        _usedBaselineRfqs[rfqHash] = true;

        if (!hadMandate) {
            _commitPrincipal(loanId, pending.borrowAmount);
        }

        uint256 fixedInterest =
            _quoteInterest(pending.borrowAmount, _originationFeeApr, block.timestamp, pending.maturity);
        _enforceRollSafetyLtv(
            pending.collateralAsset,
            pending.collateralAmount,
            rfq.putStrike,
            pending.borrowAmount + fixedInterest,
            _maxRollLtv
        );

        if (fixedInterest < rfq.minNetInterest) revert CV_InsufficientValue();

        _mandates[loanId] = Mandate({
            borrower: pending.borrower,
            collateralAsset: pending.collateralAsset,
            collateralAmount: pending.collateralAmount,
            maturity: uint64(pending.maturity),
            deadline: deadline,
            borrowAmount: pending.borrowAmount,
            minCallStrike: rfq.callStrike,
            maxPutStrike: rfq.putStrike,
            minNetInterest: rfq.minNetInterest,
            fixedInterest: fixedInterest,
            maxRollLtv: _maxRollLtv
        });

        emit MandateAccepted(
            loanId,
            pending.borrower,
            uint64(pending.maturity),
            pending.borrowAmount,
            rfq.callStrike,
            rfq.putStrike,
            rfq.minNetInterest,
            deadline
        );
    }

    function _validateInstrument(
        bytes32 instrumentId,
        address collateralAsset,
        uint256 maturity,
        uint256 strike,
        bool isPut
    ) internal view {
        (
            address underlying,
            address quoteAsset,
            address collateral,
            uint64 expiry,
            uint256 registeredStrike,,
            IMarginEngine.OptionType optionType,
            bool exists
        ) = _marginEngine.getInstrumentMetadata(instrumentId);

        if (!exists || quoteAsset != address(_usdc) || expiry != maturity || registeredStrike != strike) {
            revert CV_InvalidConfig();
        }

        address expectedUnderlying = _engineAsset[collateralAsset];
        if (underlying != expectedUnderlying) revert CV_InvalidConfig();
        if (isPut) {
            if (collateral != address(_usdc) || optionType != IMarginEngine.OptionType.Put) {
                revert CV_InvalidConfig();
            }
        } else if (collateral != expectedUnderlying || optionType != IMarginEngine.OptionType.Call) {
            revert CV_InvalidConfig();
        }
    }

    function _validatePutBucket(uint256 bucketId, bytes32 instrumentId, uint256 expectedQuantity) internal view {
        (bytes32 bucketInstrumentId,, address owner,,) = _marginEngine.getBucketMetadata(bucketId);
        (, uint256 outstanding, address primaryToken) = _marginEngine.getPutBucketState(bucketId);
        if (owner == address(0) || bucketInstrumentId != instrumentId || outstanding != expectedQuantity) {
            revert CV_InvalidConfig();
        }
        if (IERC20(primaryToken).balanceOf(address(this)) != expectedQuantity) {
            revert CV_InvalidState();
        }
    }

    function _validateRolloverPutPosition(
        uint256 bucketId,
        bytes32 instrumentId,
        uint256 expectedQuantity,
        bool inventoryTransfer
    ) internal view {
        if (!inventoryTransfer) {
            _validatePutBucket(bucketId, instrumentId, expectedQuantity);
            return;
        }

        (bytes32 bucketInstrumentId, IMarginEngine.BucketType bucketType, address owner, bool settled, bool closed) =
            _marginEngine.getBucketMetadata(bucketId);
        (uint256 collateralBalance, uint256 outstandingQuantity, address primaryToken) =
            _marginEngine.getPutBucketState(bucketId);
        bucketType;
        collateralBalance;
        if (
            owner == address(0) || bucketInstrumentId != instrumentId || settled || closed
                || outstandingQuantity < expectedQuantity
        ) {
            revert CV_InvalidConfig();
        }
        if (IERC20(primaryToken).balanceOf(address(this)) != expectedQuantity) {
            revert CV_InvalidState();
        }
    }

    function _finalizeInstrumentIfNeeded(bytes32 instrumentId) internal {
        (bool settlementFinalized,,) = _marginEngine.getInstrumentSettlementState(instrumentId);
        if (!settlementFinalized) {
            _marginEngine.finalizeInstrumentSettlement(instrumentId);
        }
    }

    function _settleBucketIfNeeded(uint256 bucketId) internal {
        (,,, bool settled,) = _marginEngine.getBucketMetadata(bucketId);
        if (!settled) {
            _marginEngine.settleBucket(bucketId);
        }
    }

    function _markReadyForVariable(uint256 loanId, uint256 collateralAmount) internal {
        Loan storage loan = _loans[loanId];
        if (collateralAmount != loan.collateralAmount) revert CV_InvalidInput();
        loan.state = LoanState.READY_FOR_VARIABLE;
        _readyLoanSince[loanId] = block.timestamp;
        emit LoanReadyForVariable(loanId, loan.principal + loan.interestOwed);
    }

    function _convertToVariableIfLiquid(uint256 loanId) internal returns (bool converted) {
        Loan storage loan = _loans[loanId];
        uint256 totalDue = loan.principal + loan.interestOwed;

        address position = _variableLoanPositions[loanId];
        if (position == address(0)) {
            address impl = _variableLoanPositionImplementation;
            if (impl == address(0)) revert CV_InvalidConfig();
            position = impl.clone();
            IVariableLoanPosition(position)
                .initialize(
                    address(this), address(_lendingAdapter), loan.borrower, loan.collateralAsset, address(_usdc)
                );
            _variableLoanPositions[loanId] = position;
        }

        if (IVariableLoanPosition(position).availableLiquidity() < totalDue) return false;

        _decreaseCommittedPrincipal(loan.principal);

        IERC20(loan.collateralAsset).safeIncreaseAllowance(position, loan.collateralAmount);
        IVariableLoanPosition(position).open(loan.collateralAmount, totalDue, address(this), address(this));

        _usdc.safeIncreaseAllowance(address(_liquidityVault), loan.principal);
        _liquidityVault.repay(loan.principal);
        if (loan.interestOwed != 0) {
            _usdc.safeTransfer(address(_liquidityVault), loan.interestOwed);
        }

        uint256 liveDebt = IVariableLoanPosition(position).currentDebt();
        uint256 liveCollateral = IVariableLoanPosition(position).currentCollateral();
        loan.state = LoanState.ACTIVE_VARIABLE;
        loan.variableDebt = liveDebt;
        loan.collateralAmount = liveCollateral;
        delete _readyLoanSince[loanId];
        emit LoanConverted(loanId, liveDebt);
        return true;
    }

    function _validateBorrowRequest(address collateralAsset, uint256 putStrike, uint256 borrowAmount) internal view {
        if (_strikeScale[collateralAsset] == 0) revert CV_InvalidConfig();
        if (putStrike == 0 || borrowAmount == 0) revert CV_InvalidInput();
    }

    function _validatePermit(
        address collateralAsset,
        uint256 collateralAmount,
        IAllowanceTransfer.PermitSingle calldata permit
    ) internal view {
        if (
            permit.details.token != collateralAsset || permit.spender != address(this)
                || permit.details.amount < collateralAmount
        ) {
            revert CV_InvalidInput();
        }
    }

    function _commitPrincipal(uint256 loanId, uint256 amount) internal {
        if (amount == 0) return;
        uint256 newTotal = _totalCommittedPrincipal + amount;
        if (_maxTotalPrincipal != 0 && newTotal > _maxTotalPrincipal) revert CV_InsufficientValue();
        _totalCommittedPrincipal = newTotal;
        _liquidityVault.reservePrincipal(loanId, amount);
    }

    function _decreaseCommittedPrincipal(uint256 amount) internal {
        if (amount == 0) return;
        _totalCommittedPrincipal -= amount;
    }

    function _cancelCommittedPrincipal(uint256 loanId, uint256 amount) internal {
        if (amount == 0) return;
        _totalCommittedPrincipal -= amount;
        _liquidityVault.releasePrincipal(loanId);
    }

    function _quoteInterest(uint256 principal, uint256 apr, uint256 start, uint256 end)
        internal
        pure
        returns (uint256)
    {
        if (apr == 0 || end <= start) return 0;
        uint256 annualFee = (principal * apr) / 1e18;
        return (annualFee * (end - start)) / YEAR;
    }

    function _enforceRollSafetyLtv(
        address collateralAsset,
        uint256 collateralAmount,
        uint256 putStrike,
        uint256 debtAmount,
        uint256 maxRollLtv_
    ) internal view {
        if (_isRollSafetyLtvViolated(collateralAsset, collateralAmount, putStrike, debtAmount, maxRollLtv_)) {
            revert CV_InsufficientValue();
        }
    }

    function _isRollSafetyLtvViolated(
        address collateralAsset,
        uint256 collateralAmount,
        uint256 putStrike,
        uint256 debtAmount,
        uint256 maxRollLtv_
    ) internal view returns (bool) {
        if (maxRollLtv_ == 0 || maxRollLtv_ > 1e18) return true;
        uint256 scale = _strikeScale[collateralAsset];
        if (scale == 0 || putStrike == 0) return true;
        uint256 putFloorValue = Math.mulDiv(collateralAmount, putStrike, scale);
        uint256 maxDebt = Math.mulDiv(putFloorValue, maxRollLtv_, 1e18);
        return debtAmount > maxDebt;
    }
}
