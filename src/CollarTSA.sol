// SPDX-License-Identifier: GPL-3.0-only
pragma solidity ^0.8.20;

import {IntLib} from "lyra-utils/math/IntLib.sol";
import {OptionEncoding} from "lyra-utils/encoding/OptionEncoding.sol";
import {SafeCast} from "openzeppelin/utils/math/SafeCast.sol";
import {ECDSA} from "openzeppelin/utils/cryptography/ECDSA.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {DecimalMath} from "lyra-utils/decimals/DecimalMath.sol";
import {SignedDecimalMath} from "lyra-utils/decimals/SignedDecimalMath.sol";
import {ConvertDecimals} from "lyra-utils/decimals/ConvertDecimals.sol";

import {BaseOnChainSigningTSA} from "v2-matching/src/tokenizedSubaccounts/BaseOnChainSigningTSA.sol";
import {BaseTSA} from "v2-matching/src/tokenizedSubaccounts/BaseTSA.sol";
import {ISubAccounts} from "v2-core/src/interfaces/ISubAccounts.sol";
import {IOptionAsset} from "v2-core/src/interfaces/IOptionAsset.sol";
import {ISpotFeed} from "v2-core/src/interfaces/ISpotFeed.sol";
import {IDepositModule} from "v2-matching/src/interfaces/IDepositModule.sol";
import {IWithdrawalModule} from "v2-matching/src/interfaces/IWithdrawalModule.sol";
import {IMatching} from "v2-matching/src/interfaces/IMatching.sol";
import {ITradeModule} from "v2-matching/src/interfaces/ITradeModule.sol";
import {IRfqModule} from "v2-matching/src/interfaces/IRfqModule.sol";
import {IRfqVerifier} from "./interfaces/IRfqVerifier.sol";
import {ICollarTsaRfqDelegateModule} from "./interfaces/ICollarTsaRfqDelegateModule.sol";
import {IBridgeAdapter} from "./interfaces/IBridgeAdapter.sol";
import {ICollarTSA} from "./interfaces/ICollarTSA.sol";
import {CollarTSABridgeHelper} from "./bridge/CollarTSABridgeHelper.sol";
import {CollarTSABridgeLib} from "./libraries/CollarTSABridgeLib.sol";
import {CollarTSAStorageLib} from "./libraries/CollarTSAStorageLib.sol";

import {IOptionRiskVerifier} from "./interfaces/IOptionRiskVerifier.sol";

interface IWithdrawalNonceTracker {
    function usedNonces(address owner, uint256 nonce) external view returns (bool);
}

/// @title CollarTSA
/// @notice TSA that allows selling covered calls and buying long puts for collar construction.
contract CollarTSA is BaseOnChainSigningTSA {
    using IntLib for int256;
    using SafeCast for int256;
    using SafeCast for uint256;
    using DecimalMath for uint256;
    using SignedDecimalMath for int256;

    address private immutable bridgeHelper;

    struct CollarTSAInitParams {
        ISpotFeed baseFeed;
        IDepositModule depositModule;
        IWithdrawalModule withdrawalModule;
        ITradeModule tradeModule;
        IRfqModule rfqModule;
        IOptionAsset optionAsset;
        IOptionRiskVerifier optionRiskVerifier;
        IRfqVerifier rfqVerifier;
        ICollarTsaRfqDelegateModule rfqDelegateModule;
        address loanStore;
        ICollarTSA.CollarTSAParams tsaParams;
        ICollarTSA.CollateralManagementParams collateralManagementParams;
    }

    uint256 internal constant LOAN_ID_NONCE_MODULUS = 1_000_000;

    function _getCollarTSAStorage() private pure returns (CollarTSAStorageLib.CollarTSAStorage storage $) {
        return CollarTSAStorageLib.get();
    }

    constructor() {
        bridgeHelper = address(new CollarTSABridgeHelper());
        _disableInitializers();
    }

    /// @notice Initialize the CollarTSA implementation.
    function initialize(
        address initialOwner,
        BaseTSA.BaseTSAInitParams memory initParams,
        CollarTSAInitParams memory collarInitParams
    ) external reinitializer(6) {
        __BaseTSA_init(initialOwner, initParams);

        CollarTSAStorageLib.CollarTSAStorage storage $ = _getCollarTSAStorage();

        $.depositModule = collarInitParams.depositModule;
        $.withdrawalModule = collarInitParams.withdrawalModule;
        $.tradeModule = collarInitParams.tradeModule;
        $.rfqModule = collarInitParams.rfqModule;
        $.optionAsset = collarInitParams.optionAsset;
        $.baseFeed = collarInitParams.baseFeed;

        if (
            collarInitParams.loanStore == address(0) || address(collarInitParams.optionRiskVerifier) == address(0)
                || address(collarInitParams.rfqVerifier) == address(0)
                || address(collarInitParams.rfqDelegateModule) == address(0)
        ) {
            revert CTSA_InvalidParams();
        }
        $.loanStore = collarInitParams.loanStore;
        $.optionRiskVerifier = collarInitParams.optionRiskVerifier;
        $.rfqVerifier = collarInitParams.rfqVerifier;
        $.rfqDelegateModule = collarInitParams.rfqDelegateModule;

        _setCollarTSAParams(collarInitParams.tsaParams);
        _setCollateralManagementParams(collarInitParams.collateralManagementParams);

        BaseTSAAddresses memory tsaAddresses = getBaseTSAAddresses();
        tsaAddresses.depositAsset.approve(address($.depositModule), type(uint256).max);
    }

    ///////////
    // Admin //
    ///////////

    /// @notice Set CollarTSA parameters.
    function setCollarTSAParams(ICollarTSA.CollarTSAParams memory newParams) external onlyOwner {
        _setCollarTSAParams(newParams);
    }

    function _setCollarTSAParams(ICollarTSA.CollarTSAParams memory newParams) internal {
        if (
            newParams.minSignatureExpiry < 1 minutes || newParams.minSignatureExpiry > newParams.maxSignatureExpiry
                || newParams.optionVolSlippageFactor > 1e18 || newParams.callMaxDelta >= 0.5e18
                || newParams.optionMaxTimeToExpiry <= newParams.optionMinTimeToExpiry || newParams.maxNegCash > 0
                || newParams.putMaxPriceFactor < 1e18 || newParams.putMaxPriceFactor > 2e18
        ) {
            revert CTSA_InvalidParams();
        }

        _getCollarTSAStorage().params = newParams;
        emit CollarTSAParamsSet(newParams);
    }

    /// @notice Set collateral management parameters.
    function setCollateralManagementParams(ICollarTSA.CollateralManagementParams memory newCollateralMgmtParams)
        external
        onlyOwner
    {
        _setCollateralManagementParams(newCollateralMgmtParams);
    }

    function _setCollateralManagementParams(ICollarTSA.CollateralManagementParams memory newCollateralMgmtParams)
        internal
    {
        if (newCollateralMgmtParams.worstSpotSellPrice > 1e18 || newCollateralMgmtParams.worstSpotSellPrice < 0.8e18) {
            revert CTSA_InvalidParams();
        }
        _getCollarTSAStorage().collateralManagementParams = newCollateralMgmtParams;

        emit CollarCollateralManagementParamsSet(newCollateralMgmtParams);
    }

    function loanStore() external view returns (address) {
        return _getCollarTSAStorage().loanStore;
    }

    function setOptionRiskVerifier(IOptionRiskVerifier newVerifier) external onlyOwner {
        if (address(newVerifier) == address(0)) {
            revert CTSA_InvalidParams();
        }
        _getCollarTSAStorage().optionRiskVerifier = newVerifier;
    }

    function setRfqVerifier(IRfqVerifier newVerifier) external onlyOwner {
        if (address(newVerifier) == address(0)) {
            revert CTSA_InvalidParams();
        }
        _getCollarTSAStorage().rfqVerifier = newVerifier;
    }

    function setRfqDelegateModule(ICollarTsaRfqDelegateModule newModule) external onlyOwner {
        if (address(newModule) == address(0)) {
            revert CTSA_InvalidParams();
        }
        _getCollarTSAStorage().rfqDelegateModule = newModule;
        emit CollarTsaRfqDelegateModuleSet(address(newModule));
    }

    function setSocketBridgeConfig(address asset, IBridgeAdapter adapter) external onlyOwner {
        CollarTSABridgeLib.setSocketBridgeConfig(_getCollarTSAStorage().bridge, asset, adapter);
    }

    function setBridgeCoordinator(address coordinator) external onlyOwner {
        CollarTSABridgeLib.setBridgeCoordinator(_getCollarTSAStorage().bridge, coordinator);
    }

    function signActionViaPermit(IMatching.Action memory action, bytes memory extraData, bytes memory signerSig)
        external
        override
        onlySubmitters
    {
        bytes32 hash = getActionTypedDataHash(action);
        (address recovered, ECDSA.RecoverError error,) = ECDSA.tryRecover(hash, signerSig);
        if (error != ECDSA.RecoverError.NoError || !this.isSigner(recovered)) {
            revert CTSA_InvalidSignature();
        }

        _signActionData(action, extraData);

        CollarTSAStorageLib.CollarTSAStorage storage $ = _getCollarTSAStorage();
        if (address(action.module) == address($.withdrawalModule)) {
            uint256 loanId = action.nonce % LOAN_ID_NONCE_MODULUS;
            $.withdrawExecutionNonce[loanId] = action.nonce;
            emit WithdrawNonceRecorded(loanId, action.nonce, hash);
        }
    }

    ///////////////////////
    // Action Validation //
    ///////////////////////

    function _verifyAction(IMatching.Action memory action, bytes32 actionHash, bytes memory extraData)
        internal
        virtual
        override
        checkBlocked
    {
        CollarTSAStorageLib.CollarTSAStorage storage $ = _getCollarTSAStorage();

        if (
            action.expiry < block.timestamp + $.params.minSignatureExpiry
                || action.expiry > block.timestamp + $.params.maxSignatureExpiry
        ) {
            revert CTSA_InvalidActionExpiry();
        }

        // Disable last seen hash when a new one comes in.
        _revokeSignature($.lastSeenHash);
        $.lastSeenHash = actionHash;

        BaseTSAAddresses memory tsaAddresses = getBaseTSAAddresses();

        if (address(action.module) == address($.depositModule)) {
            if (action.subaccountId != subAccount()) {
                revert CTSA_InvalidSubaccount();
            }
            _verifyDepositAction(action, tsaAddresses);
        } else if (address(action.module) == address($.withdrawalModule)) {
            _verifyWithdrawAction(action, tsaAddresses);
        } else if (address(action.module) == address($.tradeModule)) {
            if (action.subaccountId != subAccount()) {
                revert CTSA_InvalidSubaccount();
            }
            _verifyTradeAction(action, tsaAddresses);
        } else if (address(action.module) == address($.rfqModule)) {
            if (action.subaccountId != subAccount()) {
                revert CTSA_InvalidSubaccount();
            }
            _verifyRfqActionViaDelegate(action, extraData);
        } else {
            revert CTSA_InvalidModule();
        }
    }

    /////////////////
    // Withdrawals //
    /////////////////

    function _verifyWithdrawAction(IMatching.Action memory action, BaseTSAAddresses memory tsaAddresses) internal view {
        IWithdrawalModule.WithdrawalData memory withdrawalData =
            abi.decode(action.data, (IWithdrawalModule.WithdrawalData));

        bool isCollateral = withdrawalData.asset == address(tsaAddresses.wrappedDepositAsset);
        bool isCash = withdrawalData.asset == address(tsaAddresses.cash);
        if (!isCollateral && !isCash) {
            revert CTSA_InvalidAsset();
        }

        if (action.subaccountId != subAccount()) {
            revert CTSA_InvalidSubaccount();
        }

        (uint256 shortCalls, uint256 baseBalance, int256 cashBalance,,) = _getSubAccountStats(action.subaccountId);

        if (isCollateral) {
            uint256 amount18 =
                ConvertDecimals.to18Decimals(withdrawalData.assetAmount, tsaAddresses.depositAsset.decimals());

            if (baseBalance < amount18 + shortCalls) {
                revert CTSA_WithdrawingUtilisedCollateral();
            }

            CollarTSAStorageLib.CollarTSAStorage storage $ = _getCollarTSAStorage();
            if (cashBalance < $.params.maxNegCash) {
                revert CTSA_WithdrawalNegativeCash();
            }
            return;
        }

        uint256 cashAmount18 =
            ConvertDecimals.to18Decimals(withdrawalData.assetAmount, tsaAddresses.cash.wrappedAsset().decimals());
        int256 remainingCash = cashBalance - cashAmount18.toInt256();
        if (remainingCash < 0) {
            revert CTSA_WithdrawalNegativeCash();
        }
    }

    /////////////
    // Trading //
    /////////////

    function _verifyTradeAction(IMatching.Action memory action, BaseTSAAddresses memory tsaAddresses) internal view {
        ITradeModule.TradeData memory tradeData = abi.decode(action.data, (ITradeModule.TradeData));

        if (tradeData.desiredAmount <= 0) {
            revert CTSA_InvalidDesiredAmount();
        }

        if (tradeData.asset == address(tsaAddresses.wrappedDepositAsset)) {
            revert CTSA_SpotTradesDisabled();
        }
        revert CTSA_InvalidAsset();
    }

    /// @dev If extraData is 0, the action is a maker action; otherwise, it is a taker action.
    function _verifyRfqActionViaDelegate(IMatching.Action memory action, bytes memory extraData) internal {
        bytes memory payload = abi.encodeCall(ICollarTsaRfqDelegateModule.verifyRfqAction, (action, extraData));
        (bool success, bytes memory returnData) =
            address(_getCollarTSAStorage().rfqDelegateModule).delegatecall(payload);
        if (!success) {
            assembly {
                revert(add(returnData, 32), mload(returnData))
            }
        }
    }

    /////////////////
    // Option Math //
    /////////////////

    function _validateCallDetails(uint256 expiry, uint256 strike, uint256 limitPrice) internal view {
        CollarTSAStorageLib.CollarTSAStorage storage $ = _getCollarTSAStorage();
        IOptionRiskVerifier($.optionRiskVerifier)
            .validateCall(
                IOptionRiskVerifier.ValidateCallParams({
                    manager: address(getBaseTSAAddresses().manager),
                    optionAsset: address($.optionAsset),
                    expiry: expiry,
                    strike: strike,
                    limitPrice: limitPrice,
                    optionVolSlippageFactor: $.params.optionVolSlippageFactor,
                    callMaxDelta: $.params.callMaxDelta,
                    optionMinTimeToExpiry: $.params.optionMinTimeToExpiry,
                    optionMaxTimeToExpiry: $.params.optionMaxTimeToExpiry
                })
            );
    }

    function _validatePutDetails(uint256 expiry, uint256 strike, uint256 limitPrice) internal view {
        CollarTSAStorageLib.CollarTSAStorage storage $ = _getCollarTSAStorage();
        IOptionRiskVerifier($.optionRiskVerifier)
            .validatePut(
                IOptionRiskVerifier.ValidatePutParams({
                    manager: address(getBaseTSAAddresses().manager),
                    optionAsset: address($.optionAsset),
                    expiry: expiry,
                    strike: strike,
                    limitPrice: limitPrice,
                    optionVolSlippageFactor: $.params.optionVolSlippageFactor,
                    putMaxPriceFactor: $.params.putMaxPriceFactor,
                    optionMinTimeToExpiry: $.params.optionMinTimeToExpiry,
                    optionMaxTimeToExpiry: $.params.optionMaxTimeToExpiry
                })
            );
    }

    function _verifyDepositAction(IMatching.Action memory action, BaseTSAAddresses memory tsaAddresses) internal view {
        IDepositModule.DepositData memory depositData = abi.decode(action.data, (IDepositModule.DepositData));

        if (depositData.asset != address(tsaAddresses.wrappedDepositAsset)) {
            revert CTSA_InvalidAsset();
        }

        if (depositData.amount > tsaAddresses.depositAsset.balanceOf(address(this)) - totalPendingDeposits()) {
            revert CTSA_DepositingTooMuch();
        }
    }

    ///////////////////
    // Account Value //
    ///////////////////

    /// @notice Get short calls, base balance, cash balance, and long puts in the subaccount.
    function _getSubAccountStats(uint256 accountId)
        internal
        view
        returns (uint256 shortCalls, uint256 baseBalance, int256 cashBalance, uint256 longPuts, uint256 optionPositions)
    {
        BaseTSAAddresses memory tsaAddresses = getBaseTSAAddresses();

        ISubAccounts.AssetBalance[] memory balances = tsaAddresses.subAccounts.getAccountBalances(accountId);
        for (uint256 i = 0; i < balances.length; i++) {
            if (balances[i].asset == _getCollarTSAStorage().optionAsset) {
                int256 balance = balances[i].balance;
                if (balance == 0) {
                    continue;
                }
                (,, bool isCall) = OptionEncoding.fromSubId(balances[i].subId.toUint96());
                if (balance > 0) {
                    if (isCall) {
                        revert CTSA_InvalidOptionBalance();
                    }
                    longPuts += balance.toUint256();
                } else {
                    if (!isCall) {
                        revert CTSA_InvalidOptionBalance();
                    }
                    shortCalls += balance.abs();
                }
                optionPositions += 1;
            } else if (balances[i].asset == tsaAddresses.wrappedDepositAsset) {
                baseBalance = balances[i].balance.abs();
            } else if (balances[i].asset == tsaAddresses.cash) {
                cashBalance = balances[i].balance;
            }
        }
        return (shortCalls, baseBalance, cashBalance, longPuts, optionPositions);
    }

    function _getSubAccountStats()
        internal
        view
        returns (uint256 shortCalls, uint256 baseBalance, int256 cashBalance, uint256 longPuts, uint256 optionPositions)
    {
        return _getSubAccountStats(subAccount());
    }

    function _getAccountValue(bool includePending) internal view override returns (uint256) {
        BaseTSAAddresses memory tsaAddresses = getBaseTSAAddresses();

        uint256 depositAssetBalance = tsaAddresses.depositAsset.balanceOf(address(this));
        if (!includePending) {
            depositAssetBalance -= totalPendingDeposits();
        }

        return _getConvertedMtM(true) + depositAssetBalance;
    }

    function _getConvertedMtM(bool nativeDecimals) internal view returns (uint256) {
        BaseTSAAddresses memory tsaAddresses = getBaseTSAAddresses();

        (, int256 mtm) = tsaAddresses.manager.getMarginAndMarkToMarket(subAccount(), false, 0);
        uint256 spotPrice = _getBasePrice();
        int256 convertedMtM = mtm.divideDecimal(spotPrice.toInt256());

        if (nativeDecimals) {
            uint8 decimals = tsaAddresses.depositAsset.decimals();
            if (decimals > 18) {
                convertedMtM = convertedMtM * int256(10 ** (decimals - 18));
            } else if (decimals < 18) {
                convertedMtM = convertedMtM / int256(10 ** (18 - decimals));
            }
        }

        if (convertedMtM < 0) {
            revert CTSA_PositionInsolvent();
        }

        return uint256(convertedMtM);
    }

    function _getBasePrice() internal view returns (uint256 spotPrice) {
        (spotPrice,) = _getCollarTSAStorage().baseFeed.getSpot();
    }

    function estimateBridgeFees(address asset, address, uint256) external view returns (uint256) {
        return CollarTSABridgeLib.estimateBridgeFees(_getCollarTSAStorage().bridge, asset);
    }

    function bridgeToL1(address, uint256, address) external payable returns (bytes32 socketMessageId) {
        (bool ok, bytes memory ret) = bridgeHelper.delegatecall(msg.data);
        if (!ok) {
            assembly {
                revert(add(ret, 0x20), mload(ret))
            }
        }
        assembly {
            socketMessageId := mload(add(ret, 0x20))
        }
    }

    ///////////
    // Views //
    ///////////

    function getCollarTSAParams() external view returns (ICollarTSA.CollarTSAParams memory) {
        return _getCollarTSAStorage().params;
    }

    function getCollarTSAAddresses()
        external
        view
        returns (ISpotFeed, IDepositModule, IWithdrawalModule, ITradeModule, IRfqModule, IOptionAsset)
    {
        CollarTSAStorageLib.CollarTSAStorage storage $ = _getCollarTSAStorage();
        return ($.baseFeed, $.depositModule, $.withdrawalModule, $.tradeModule, $.rfqModule, $.optionAsset);
    }

    function withdrawExecutionNonce(uint256 loanId) external view returns (uint256) {
        return _getCollarTSAStorage().withdrawExecutionNonce[loanId];
    }

    function withdrawExecuted(uint256 loanId) external view returns (bool) {
        CollarTSAStorageLib.CollarTSAStorage storage $ = _getCollarTSAStorage();
        uint256 nonce = $.withdrawExecutionNonce[loanId];
        if (nonce == 0 || address($.withdrawalModule) == address(0)) {
            return false;
        }
        return IWithdrawalNonceTracker(address($.withdrawalModule)).usedNonces(address(this), nonce);
    }

    ///////////////////
    // Events/Errors //
    ///////////////////

    event CollarTSAParamsSet(ICollarTSA.CollarTSAParams params);
    event CollarCollateralManagementParamsSet(ICollarTSA.CollateralManagementParams collateralManagementParams);
    event CollarTsaRfqDelegateModuleSet(address module);
    event WithdrawNonceRecorded(uint256 indexed loanId, uint256 nonce, bytes32 indexed actionHash);

    error CTSA_InvalidParams();
    error CTSA_InvalidConfig();
    error CTSA_InvalidSignature();
    error CTSA_InvalidActionExpiry();
    error CTSA_InvalidModule();
    error CTSA_InvalidSubaccount();
    error CTSA_InvalidAsset();
    error CTSA_InvalidDesiredAmount();
    error CTSA_SpotTradesDisabled();
    error CTSA_WithdrawingUtilisedCollateral();
    error CTSA_WithdrawalNegativeCash();
    error CTSA_SellingTooManyCalls();
    error CTSA_CanOnlyOpenShortCalls();
    error CTSA_OnlyLongPutsAllowed();
    error CTSA_InvalidOptionBalance();
    error CTSA_OptionExpiryOutOfBounds();
    error CTSA_InsufficientCash();
    error CTSA_OptionExpired();
    error CTSA_OptionDeltaTooHigh();
    error CTSA_OptionPriceTooLow();
    error CTSA_PutPriceTooHigh();
    error CTSA_InvalidRfqTradeDetails();
    error CTSA_InvalidTradeAmount();
    error CTSA_TradeDataDoesNotMatchOrderHash();
    error CTSA_SpotRfqRequiresTaker();
    error CTSA_SpotRfqAmountInvalid();
    error CTSA_SpotRfqPriceTooLow();
    error CTSA_SpotRfqSellTooMuch();
    error CTSA_DepositingTooMuch();
    error CTSA_PositionInsolvent();
}
