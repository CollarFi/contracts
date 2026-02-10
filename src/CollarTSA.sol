// SPDX-License-Identifier: GPL-3.0-only
pragma solidity ^0.8.20;

import {IntLib} from "lyra-utils/math/IntLib.sol";
import {OptionEncoding} from "lyra-utils/encoding/OptionEncoding.sol";
import {Black76} from "lyra-utils/math/Black76.sol";
import {SafeCast} from "openzeppelin/utils/math/SafeCast.sol";
import {DecimalMath} from "lyra-utils/decimals/DecimalMath.sol";
import {SignedDecimalMath} from "lyra-utils/decimals/SignedDecimalMath.sol";
import {ConvertDecimals} from "lyra-utils/decimals/ConvertDecimals.sol";

import {BaseTSA} from "v2-matching/src/tokenizedSubaccounts/BaseOnChainSigningTSA.sol";
import {ISubAccounts} from "v2-core/src/interfaces/ISubAccounts.sol";
import {IOptionAsset} from "v2-core/src/interfaces/IOptionAsset.sol";
import {ISpotFeed} from "v2-core/src/interfaces/ISpotFeed.sol";
import {IDepositModule} from "v2-matching/src/interfaces/IDepositModule.sol";
import {IWithdrawalModule} from "v2-matching/src/interfaces/IWithdrawalModule.sol";
import {IMatching} from "v2-matching/src/interfaces/IMatching.sol";
import {ITradeModule} from "v2-matching/src/interfaces/ITradeModule.sol";
import {IRfqModule} from "v2-matching/src/interfaces/IRfqModule.sol";

import {StandardManager, IStandardManager, IVolFeed, IForwardFeed} from "v2-core/src/risk-managers/StandardManager.sol";
import {CollateralManagementTSA} from "v2-matching/src/tokenizedSubaccounts/CollateralManagementTSA.sol";

import {ICollarLoanStore} from "./interfaces/ICollarLoanStore.sol";

/// @title CollarTSA
/// @notice TSA that allows selling covered calls and buying long puts for collar construction.
contract CollarTSA is CollateralManagementTSA {
    using IntLib for int256;
    using SafeCast for int256;
    using SafeCast for uint256;
    using DecimalMath for uint256;
    using SignedDecimalMath for int256;

    struct CollarTSAInitParams {
        ISpotFeed baseFeed;
        IDepositModule depositModule;
        IWithdrawalModule withdrawalModule;
        ITradeModule tradeModule;
        IRfqModule rfqModule;
        IOptionAsset optionAsset;
        address loanStore;
    }

    struct CollarTSAParams {
        /// @dev Minimum time before an action is expired
        uint256 minSignatureExpiry;
        /// @dev Maximum time before an action is expired
        uint256 maxSignatureExpiry;
        /// @dev The worst difference to vol that is accepted for pricing options (e.g. 0.9e18)
        uint256 optionVolSlippageFactor;
        /// @dev The highest delta for calls accepted by the TSA after vol/fwd slippage is applied (e.g. 0.15e18).
        uint256 callMaxDelta;
        /// @dev Maximum amount of negative cash allowed when opening option positions. (e.g. -100e18)
        int256 maxNegCash;
        /// @dev Lower bound for option expiry
        uint256 optionMinTimeToExpiry;
        /// @dev Upper bound for option expiry
        uint256 optionMaxTimeToExpiry;
        /// @dev Maximum price factor for long puts relative to mark (e.g. 1.05e18).
        uint256 putMaxPriceFactor;
    }

    struct CollarLeg {
        IRfqModule.TradeData trade;
        uint256 expiry;
        uint256 strike;
    }

    /// @custom:storage-location erc7201:lyra.storage.CollarTSA
    struct CollarTSAStorage {
        IDepositModule depositModule;
        IWithdrawalModule withdrawalModule;
        ITradeModule tradeModule;
        IRfqModule rfqModule;
        IOptionAsset optionAsset;
        ISpotFeed baseFeed;
        CollarTSAParams params;
        CollateralManagementParams collateralManagementParams;
        /// @dev Only one hash is considered valid at a time, and it is revoked when a new one comes in.
        bytes32 lastSeenHash;

        /// @dev L2 loan store to enforce borrower mandates and track per-loan collateral accounting.
        address loanStore;
    }

    // keccak256(abi.encode(uint256(keccak256("lyra.storage.CollarTSA")) - 1)) & ~bytes32(uint256(0xff))
    bytes32 private constant CollarTSAStorageLocation =
        0x62b72349c5c9dfc4c2d0e5f1b0600421e6f0d0f8ac3a0ffdf4c4c0b7d4b4b000;

    function _getCollarTSAStorage() private pure returns (CollarTSAStorage storage $) {
        assembly {
            $.slot := CollarTSAStorageLocation
        }
    }

    constructor() {
        _disableInitializers();
    }

    /// @notice Initialize the CollarTSA implementation.
    function initialize(
        address initialOwner,
        BaseTSA.BaseTSAInitParams memory initParams,
        CollarTSAInitParams memory collarInitParams
    ) external reinitializer(6) {
        __BaseTSA_init(initialOwner, initParams);

        CollarTSAStorage storage $ = _getCollarTSAStorage();

        $.depositModule = collarInitParams.depositModule;
        $.withdrawalModule = collarInitParams.withdrawalModule;
        $.tradeModule = collarInitParams.tradeModule;
        $.rfqModule = collarInitParams.rfqModule;
        $.optionAsset = collarInitParams.optionAsset;
        $.baseFeed = collarInitParams.baseFeed;

        if (collarInitParams.loanStore == address(0)) {
            revert CTSA_InvalidParams();
        }
        $.loanStore = collarInitParams.loanStore;

        BaseTSAAddresses memory tsaAddresses = getBaseTSAAddresses();
        tsaAddresses.depositAsset.approve(address($.depositModule), type(uint256).max);
    }

    ///////////
    // Admin //
    ///////////

    /// @notice Set CollarTSA parameters.
    function setCollarTSAParams(CollarTSAParams memory newParams) external onlyOwner {
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
    function setCollateralManagementParams(CollateralManagementParams memory newCollateralMgmtParams)
        external
        override
        onlyOwner
    {
        if (
            newCollateralMgmtParams.worstSpotBuyPrice < 1e18 || newCollateralMgmtParams.worstSpotBuyPrice > 1.2e18
                || newCollateralMgmtParams.worstSpotSellPrice > 1e18
                || newCollateralMgmtParams.worstSpotSellPrice < 0.8e18
                || newCollateralMgmtParams.spotTransactionLeniency < 1e18
                || newCollateralMgmtParams.spotTransactionLeniency > 1.2e18
                || newCollateralMgmtParams.feeFactor > 0.05e18
        ) {
            revert CTSA_InvalidParams();
        }
        _getCollarTSAStorage().collateralManagementParams = newCollateralMgmtParams;

        emit CMTSAParamsSet(newCollateralMgmtParams);
    }

    function loanStore() public view returns (address) {
        return _getCollarTSAStorage().loanStore;
    }

    function _getCollateralManagementParams() internal view override returns (CollateralManagementParams storage $) {
        return _getCollarTSAStorage().collateralManagementParams;
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
        CollarTSAStorage storage $ = _getCollarTSAStorage();

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
            _verifyRfqAction(action, extraData);
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

            CollarTSAStorage storage $ = _getCollarTSAStorage();
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
    function _verifyRfqAction(IMatching.Action memory action, bytes memory extraData) internal view {
        // TODO: Confirm whether RFQ maxFee should be bounded by on-chain parameters for collar trades.
        // AtomicSigningExecutor callback `extraData` is used to carry mandate context as well.
        // Format (preferred): abi.encode(uint256 loanId, bytes rfqExtraData)
        // - rfqExtraData is the same data that would have previously been passed as extraData,
        //   i.e. the ABI-encoded `IRfqModule.TradeData[]` for taker orders.
        uint256 loanId = 0;
        bytes memory rfqExtraData = extraData;
        if (extraData.length != 0) {
            // AtomicSigningExecutor callback format (required for all taker RFQs, including spot RFQs):
            // abi.encode(uint256 loanId, bytes rfqExtraData)
            (loanId, rfqExtraData) = abi.decode(extraData, (uint256, bytes));
            if (loanId == 0) {
                revert CTSA_InvalidRfqTradeDetails();
            }
        }

        IRfqModule.TradeData[] memory makerTrades;
        bool isTaker = rfqExtraData.length != 0;
        if (!isTaker) {
            IRfqModule.RfqOrder memory makerOrder = abi.decode(action.data, (IRfqModule.RfqOrder));
            makerTrades = makerOrder.trades;
        } else {
            IRfqModule.TakerOrder memory takerOrder = abi.decode(action.data, (IRfqModule.TakerOrder));
            if (keccak256(rfqExtraData) != takerOrder.orderHash) {
                revert CTSA_TradeDataDoesNotMatchOrderHash();
            }
            makerTrades = abi.decode(rfqExtraData, (IRfqModule.TradeData[]));
        }

        if (_isSpotRfqTrade(makerTrades)) {
            _verifySpotRfqTrade(makerTrades[0], isTaker, loanId);
            return;
        }

        _verifyCollarRfqTrades(makerTrades, isTaker, loanId);
    }

    function _verifyCollarRfqTrades(IRfqModule.TradeData[] memory makerTrades, bool isTaker, uint256 loanId)
        internal
        view
    {
        CollarTSAStorage storage $ = _getCollarTSAStorage();
        (CollarLeg memory callLeg, CollarLeg memory putLeg) = _splitRfqLegs(makerTrades, address($.optionAsset));

        if (callLeg.expiry != putLeg.expiry) {
            revert CTSA_InvalidRfqTradeDetails();
        }

        // Enforce borrower mandate constraints.
        if (loanId == 0) {
            revert CTSA_InvalidRfqTradeDetails();
        }

        ICollarLoanStore.Loan memory loan = ICollarLoanStore($.loanStore).getLoan(loanId);

        if (loan.borrower == address(0) || loan.consumed) {
            revert CTSA_InvalidRfqTradeDetails();
        }
        if (loan.deadline != 0 && block.timestamp > loan.deadline) {
            revert CTSA_InvalidRfqTradeDetails();
        }

        // Mandate requires exact maturity == option expiry.
        if (loan.maturity != 0 && callLeg.expiry != loan.maturity) {
            revert CTSA_InvalidRfqTradeDetails();
        }

        // Strike bounds.
        if (loan.minCallStrike != 0 && callLeg.strike < loan.minCallStrike) {
            revert CTSA_InvalidRfqTradeDetails();
        }
        if (loan.maxPutStrike != 0 && putLeg.strike > loan.maxPutStrike) {
            revert CTSA_InvalidRfqTradeDetails();
        }

        // borrowAmount is not directly observable in RFQ leg data; it is enforced on L1 when finalizing the loan.

        int256 callAmount = isTaker ? -callLeg.trade.amount : callLeg.trade.amount;
        int256 putAmount = isTaker ? -putLeg.trade.amount : putLeg.trade.amount;

        if (callAmount >= 0) {
            revert CTSA_CanOnlyOpenShortCalls();
        }
        if (putAmount <= 0) {
            revert CTSA_OnlyLongPutsAllowed();
        }
        if (callAmount.abs() != putAmount.abs()) {
            revert CTSA_InvalidTradeAmount();
        }

        (uint256 shortCalls, uint256 baseBalance, int256 cashBalance,,) = _getSubAccountStats();
        if (shortCalls + callAmount.abs() > baseBalance) {
            revert CTSA_SellingTooManyCalls();
        }

        _validateCallDetails(callLeg.expiry, callLeg.strike, callLeg.trade.price);
        _validatePutDetails(putLeg.expiry, putLeg.strike, putLeg.trade.price);

        int256 cashDelta = callLeg.trade.price.toInt256().multiplyDecimal(callLeg.trade.amount)
            + putLeg.trade.price.toInt256().multiplyDecimal(putLeg.trade.amount);
        int256 postTradeCash = cashBalance + (isTaker ? cashDelta : -cashDelta);
        if (postTradeCash < $.params.maxNegCash) {
            revert CTSA_InsufficientCash();
        }
    }

    function _isSpotRfqTrade(IRfqModule.TradeData[] memory makerTrades) internal view returns (bool) {
        if (makerTrades.length != 1) {
            return false;
        }
        BaseTSAAddresses memory tsaAddresses = getBaseTSAAddresses();
        return makerTrades[0].asset == address(tsaAddresses.wrappedDepositAsset) && makerTrades[0].subId == 0;
    }

    function _verifySpotRfqTrade(IRfqModule.TradeData memory trade, bool isTaker, uint256 loanId) internal view {
        if (!isTaker) {
            revert CTSA_SpotRfqRequiresTaker();
        }
        if (trade.subId != 0) {
            revert CTSA_InvalidAsset();
        }
        if (loanId == 0) {
            revert CTSA_InvalidRfqTradeDetails();
        }

        uint256 amount = trade.amount.toUint256();
        if (amount == 0) {
            revert CTSA_SpotRfqAmountInvalid();
        }

        address store = _getCollarTSAStorage().loanStore;
        if (store == address(0)) {
            revert CTSA_InvalidRfqTradeDetails();
        }

        ICollarLoanStore.Loan memory loan = ICollarLoanStore(store).getLoan(loanId);
        if (loan.borrower == address(0) || loan.consumed) {
            revert CTSA_InvalidRfqTradeDetails();
        }
        if (loan.collateralAsset != address(0) && trade.asset != loan.collateralAsset) {
            revert CTSA_InvalidAsset();
        }
        if (loan.collateralAmount != 0 && amount > loan.collateralAmount) {
            revert CTSA_SpotRfqSellTooMuch();
        }

        uint256 basePrice = _getBasePrice();
        if (trade.price < basePrice.multiplyDecimal(_getCollateralManagementParams().worstSpotSellPrice)) {
            revert CTSA_SpotRfqPriceTooLow();
        }

        (, uint256 baseBalance,,,) = _getSubAccountStats();
        if (amount > baseBalance) {
            revert CTSA_SpotRfqSellTooMuch();
        }
    }

    function _splitRfqLegs(IRfqModule.TradeData[] memory makerTrades, address optionAsset)
        internal
        pure
        returns (CollarLeg memory callLeg, CollarLeg memory putLeg)
    {
        if (makerTrades.length != 2) {
            revert CTSA_InvalidRfqTradeLength();
        }

        bool hasCall;
        bool hasPut;

        for (uint256 i = 0; i < makerTrades.length; i++) {
            if (makerTrades[i].asset != optionAsset) {
                revert CTSA_InvalidAsset();
            }

            (uint256 expiry, uint256 strike, bool isCall) = OptionEncoding.fromSubId(makerTrades[i].subId.toUint96());
            if (isCall) {
                if (hasCall) {
                    revert CTSA_InvalidRfqTradeDetails();
                }
                callLeg = CollarLeg({trade: makerTrades[i], expiry: expiry, strike: strike});
                hasCall = true;
            } else {
                if (hasPut) {
                    revert CTSA_InvalidRfqTradeDetails();
                }
                putLeg = CollarLeg({trade: makerTrades[i], expiry: expiry, strike: strike});
                hasPut = true;
            }
        }

        if (!hasCall || !hasPut) {
            revert CTSA_InvalidRfqTradeDetails();
        }
    }

    function _verifyCallSell(ITradeModule.TradeData memory tradeData, uint256 expiry, uint256 strike) internal view {
        (uint256 shortCalls, uint256 baseBalance, int256 cashBalance,,) = _getSubAccountStats();

        if (tradeData.desiredAmount.abs() + shortCalls > baseBalance) {
            revert CTSA_SellingTooManyCalls();
        }

        if (cashBalance < _getCollarTSAStorage().params.maxNegCash) {
            revert CTSA_CannotSellOptionsWithNegativeCash();
        }

        _verifyCollateralTradeFee(tradeData.worstFee, _getBasePrice());
        _validateCallDetails(expiry, strike, tradeData.limitPrice.toUint256());
    }

    function _verifyPutBuy(ITradeModule.TradeData memory tradeData, uint256 expiry, uint256 strike) internal view {
        (,, int256 cashBalance,,) = _getSubAccountStats();

        _verifyCollateralTradeFee(tradeData.worstFee, _getBasePrice());
        _validatePutDetails(expiry, strike, tradeData.limitPrice.toUint256());

        int256 cost = tradeData.limitPrice.multiplyDecimal(tradeData.desiredAmount);
        int256 remainingCash = cashBalance - cost;
        if (remainingCash < _getCollarTSAStorage().params.maxNegCash) {
            revert CTSA_InsufficientCash();
        }
    }

    /////////////////
    // Option Math //
    /////////////////

    function _validateCallDetails(uint256 expiry, uint256 strike, uint256 limitPrice) internal view {
        CollarTSAStorage storage $ = _getCollarTSAStorage();

        _validateExpiry(expiry);

        uint256 timeToExpiry = expiry - block.timestamp;
        (uint256 vol, uint256 forwardPrice) = _getFeedValues(strike.toUint128(), expiry.toUint64());

        (uint256 callPrice,, uint256 callDelta) = Black76.pricesAndDelta(
            Black76.Black76Inputs({
                timeToExpirySec: timeToExpiry.toUint64(),
                volatility: (vol.multiplyDecimal($.params.optionVolSlippageFactor)).toUint128(),
                fwdPrice: forwardPrice.toUint128(),
                strikePrice: strike.toUint128(),
                discount: 1e18
            })
        );

        if (callDelta > $.params.callMaxDelta) {
            revert CTSA_OptionDeltaTooHigh();
        }

        if (limitPrice <= callPrice) {
            revert CTSA_OptionPriceTooLow();
        }
    }

    function _validatePutDetails(uint256 expiry, uint256 strike, uint256 limitPrice) internal view {
        CollarTSAStorage storage $ = _getCollarTSAStorage();

        _validateExpiry(expiry);

        uint256 timeToExpiry = expiry - block.timestamp;
        (uint256 vol, uint256 forwardPrice) = _getFeedValues(strike.toUint128(), expiry.toUint64());

        (, uint256 putPrice,) = Black76.pricesAndDelta(
            Black76.Black76Inputs({
                timeToExpirySec: timeToExpiry.toUint64(),
                volatility: (vol.multiplyDecimal($.params.optionVolSlippageFactor)).toUint128(),
                fwdPrice: forwardPrice.toUint128(),
                strikePrice: strike.toUint128(),
                discount: 1e18
            })
        );

        uint256 maxPrice = putPrice.multiplyDecimal($.params.putMaxPriceFactor);
        if (limitPrice > maxPrice) {
            revert CTSA_PutPriceTooHigh();
        }
    }

    function _validateExpiry(uint256 expiry) internal view {
        CollarTSAStorage storage $ = _getCollarTSAStorage();

        if (block.timestamp >= expiry) {
            revert CTSA_OptionExpired();
        }
        uint256 timeToExpiry = expiry - block.timestamp;
        if (timeToExpiry < $.params.optionMinTimeToExpiry || timeToExpiry > $.params.optionMaxTimeToExpiry) {
            revert CTSA_OptionExpiryOutOfBounds();
        }
    }

    function _getFeedValues(uint128 strike, uint64 expiry) internal view returns (uint256 vol, uint256 forwardPrice) {
        CollarTSAStorage storage $ = _getCollarTSAStorage();

        StandardManager srm = StandardManager(address(getBaseTSAAddresses().manager));
        IStandardManager.AssetDetail memory assetDetails = srm.assetDetails($.optionAsset);
        (, IForwardFeed fwdFeed, IVolFeed volFeed) = srm.getMarketFeeds(assetDetails.marketId);
        (vol,) = volFeed.getVol(strike, expiry);
        (forwardPrice,) = fwdFeed.getForwardPrice(expiry);
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

    function _getBasePrice() internal view override returns (uint256 spotPrice) {
        (spotPrice,) = _getCollarTSAStorage().baseFeed.getSpot();
    }

    ///////////
    // Views //
    ///////////

    function getAccountValue(bool includePending) public view returns (uint256) {
        return _getAccountValue(includePending);
    }

    function getSubAccountStats()
        public
        view
        returns (uint256 shortCalls, uint256 baseBalance, int256 cashBalance, uint256 longPuts, uint256 optionPositions)
    {
        return _getSubAccountStats();
    }

    function getBasePrice() public view returns (uint256) {
        return _getBasePrice();
    }

    function getCollarTSAParams() public view returns (CollarTSAParams memory) {
        return _getCollarTSAStorage().params;
    }

    function getCollateralManagementParams() public view returns (CollateralManagementParams memory) {
        return _getCollateralManagementParams();
    }

    function lastSeenHash() public view returns (bytes32) {
        return _getCollarTSAStorage().lastSeenHash;
    }

    function getCollarTSAAddresses()
        public
        view
        returns (ISpotFeed, IDepositModule, IWithdrawalModule, ITradeModule, IRfqModule, IOptionAsset)
    {
        CollarTSAStorage storage $ = _getCollarTSAStorage();
        return ($.baseFeed, $.depositModule, $.withdrawalModule, $.tradeModule, $.rfqModule, $.optionAsset);
    }

    ///////////////////
    // Events/Errors //
    ///////////////////

    event CollarTSAParamsSet(CollarTSAParams params);

    error CTSA_InvalidParams();
    error CTSA_InvalidActionExpiry();
    error CTSA_InvalidModule();
    error CTSA_InvalidSubaccount();
    error CTSA_InvalidAsset();
    error CTSA_InvalidDesiredAmount();
    error CTSA_SpotTradesDisabled();
    error CTSA_WithdrawingUtilisedCollateral();
    error CTSA_WithdrawalNegativeCash();
    error CTSA_SellingTooManyCalls();
    error CTSA_CannotSellOptionsWithNegativeCash();
    error CTSA_CanOnlyOpenShortCalls();
    error CTSA_OnlyLongPutsAllowed();
    error CTSA_OptionExpired();
    error CTSA_OptionDeltaTooHigh();
    error CTSA_OptionPriceTooLow();
    error CTSA_PutPriceTooHigh();
    error CTSA_InvalidOptionBalance();
    error CTSA_OptionExpiryOutOfBounds();
    error CTSA_InsufficientCash();
    error CTSA_InvalidRfqTradeLength();
    error CTSA_InvalidRfqTradeDetails();
    error CTSA_InvalidTradeAmount();
    error CTSA_TradeDataDoesNotMatchOrderHash();
    error CTSA_SpotRfqRequiresTaker();
    error CTSA_SpotRfqAmountInvalid();
    error CTSA_SpotRfqPriceTooLow();
    error CTSA_SpotRfqSellTooMuch();
}
