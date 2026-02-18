// SPDX-License-Identifier: GPL-3.0-only
pragma solidity ^0.8.20;

import {IntLib} from "lyra-utils/math/IntLib.sol";
import {OptionEncoding} from "lyra-utils/encoding/OptionEncoding.sol";
import {SafeCast} from "openzeppelin/utils/math/SafeCast.sol";
import {DecimalMath} from "lyra-utils/decimals/DecimalMath.sol";
import {SignedDecimalMath} from "lyra-utils/decimals/SignedDecimalMath.sol";

import {ISpotFeed} from "v2-core/src/interfaces/ISpotFeed.sol";
import {IOptionAsset} from "v2-core/src/interfaces/IOptionAsset.sol";
import {IDepositModule} from "v2-matching/src/interfaces/IDepositModule.sol";
import {IWithdrawalModule} from "v2-matching/src/interfaces/IWithdrawalModule.sol";
import {IMatching} from "v2-matching/src/interfaces/IMatching.sol";
import {ITradeModule} from "v2-matching/src/interfaces/ITradeModule.sol";
import {IRfqModule} from "v2-matching/src/interfaces/IRfqModule.sol";
import {ISubAccounts} from "v2-core/src/interfaces/ISubAccounts.sol";

import {IRfqVerifier} from "../interfaces/IRfqVerifier.sol";
import {ICollarTsaRfqDelegateModule} from "../interfaces/ICollarTsaRfqDelegateModule.sol";
import {ICollarLoanStore} from "../interfaces/ICollarLoanStore.sol";
import {IOptionRiskVerifier} from "../interfaces/IOptionRiskVerifier.sol";
import {ICollarTSA} from "../interfaces/ICollarTSA.sol";

contract CollarTsaRfqDelegateModule is ICollarTsaRfqDelegateModule {
    using IntLib for int256;
    using SafeCast for int256;
    using SafeCast for uint256;
    using DecimalMath for uint256;
    using SignedDecimalMath for int256;

    struct CollarTSAParams {
        uint256 minSignatureExpiry;
        uint256 maxSignatureExpiry;
        uint256 optionVolSlippageFactor;
        uint256 callMaxDelta;
        int256 maxNegCash;
        uint256 optionMinTimeToExpiry;
        uint256 optionMaxTimeToExpiry;
        uint256 putMaxPriceFactor;
    }

    struct CollateralManagementParams {
        uint256 worstSpotSellPrice;
    }

    /// @custom:storage-location erc7201:lyra.storage.CollarTSA
    struct CollarTSAStorage {
        IDepositModule depositModule;
        IWithdrawalModule withdrawalModule;
        ITradeModule tradeModule;
        IRfqModule rfqModule;
        IOptionAsset optionAsset;
        ISpotFeed baseFeed;
        IRfqVerifier rfqVerifier;
        ICollarTsaRfqDelegateModule rfqDelegateModule;
        CollarTSAParams params;
        CollateralManagementParams collateralManagementParams;
        bytes32 lastSeenHash;
        address loanStore;
        IOptionRiskVerifier optionRiskVerifier;
    }

    bytes32 private constant CollarTSAStorageLocation =
        0x62b72349c5c9dfc4c2d0e5f1b0600421e6f0d0f8ac3a0ffdf4c4c0b7d4b4b000;

    function _getCollarTSAStorage() private pure returns (CollarTSAStorage storage $) {
        assembly {
            $.slot := CollarTSAStorageLocation
        }
    }

    function verifyRfqAction(IMatching.Action memory action, bytes memory extraData) external {
        CollarTSAStorage storage $ = _getCollarTSAStorage();
        (,, address wrappedDepositAsset, address cash,,,) = ICollarTSA(address(this)).getBaseTSAAddresses();

        IRfqVerifier.ParsedRfq memory parsed =
            $.rfqVerifier.parseAndValidate(action.data, extraData, wrappedDepositAsset, address($.optionAsset));

        if (parsed.isSpot) {
            _verifySpotRfqTrade(parsed.spotTrade, parsed.isTaker, parsed.loanId, wrappedDepositAsset, cash);
            return;
        }

        uint256 loanId = parsed.loanId;
        if (loanId == 0) {
            revert CTSA_InvalidRfqTradeDetails();
        }

        ICollarLoanStore.Loan memory loan = ICollarLoanStore($.loanStore).getLoan(loanId);
        if (loan.borrower == address(0) || loan.consumed) {
            revert CTSA_InvalidRfqTradeDetails();
        }
        uint64 expectedMaturity = loan.rolloverPending ? loan.rolloverMaturity : loan.maturity;
        uint64 expectedDeadline = loan.rolloverPending ? loan.rolloverDeadline : loan.deadline;
        uint256 expectedMinCallStrike = loan.rolloverPending ? loan.rolloverMinCallStrike : loan.minCallStrike;
        uint256 expectedMaxPutStrike = loan.rolloverPending ? loan.rolloverMaxPutStrike : loan.maxPutStrike;
        uint256 fixedInterest = loan.rolloverPending ? loan.rolloverFixedInterest : loan.fixedInterest;
        uint256 maxNegativeC = loan.rolloverPending ? loan.rolloverMaxNegativeC : loan.maxNegativeC;

        if (expectedDeadline != 0 && block.timestamp > expectedDeadline) {
            revert CTSA_InvalidRfqTradeDetails();
        }
        if (expectedMaturity != 0 && parsed.callExpiry != expectedMaturity) {
            revert CTSA_InvalidRfqTradeDetails();
        }
        if (expectedMinCallStrike != 0 && parsed.callStrike < expectedMinCallStrike) {
            revert CTSA_InvalidRfqTradeDetails();
        }
        if (expectedMaxPutStrike != 0 && parsed.putStrike > expectedMaxPutStrike) {
            revert CTSA_InvalidRfqTradeDetails();
        }

        int256 callAmount = parsed.isTaker ? -parsed.callTrade.amount : parsed.callTrade.amount;
        int256 putAmount = parsed.isTaker ? -parsed.putTrade.amount : parsed.putTrade.amount;

        if (callAmount >= 0) {
            revert CTSA_CanOnlyOpenShortCalls();
        }
        if (putAmount <= 0) {
            revert CTSA_OnlyLongPutsAllowed();
        }
        if (callAmount.abs() != putAmount.abs()) {
            revert CTSA_InvalidTradeAmount();
        }

        (uint256 shortCalls, uint256 baseBalance, int256 cashBalance) = _getSubAccountStats(wrappedDepositAsset, cash);
        if (shortCalls + callAmount.abs() > baseBalance) {
            revert CTSA_SellingTooManyCalls();
        }

        _validateCallDetails(parsed.callExpiry, parsed.callStrike, parsed.callTrade.price);
        _validatePutDetails(parsed.putExpiry, parsed.putStrike, parsed.putTrade.price);

        int256 expectedC =
            -(parsed.callTrade.price.toInt256().multiplyDecimal(parsed.callTrade.amount)
                + parsed.putTrade.price.toInt256().multiplyDecimal(parsed.putTrade.amount));
        int256 expectedTotal = int256(fixedInterest) + expectedC;
        uint256 expectedDeficit = expectedTotal < 0 ? uint256(-expectedTotal) : 0;
        if (expectedDeficit > maxNegativeC) {
            revert CTSA_InsufficientCash();
        }

        int256 cashDelta = parsed.callTrade.price.toInt256().multiplyDecimal(parsed.callTrade.amount)
            + parsed.putTrade.price.toInt256().multiplyDecimal(parsed.putTrade.amount);
        int256 postTradeCash = cashBalance + (parsed.isTaker ? cashDelta : -cashDelta);
        if (postTradeCash < $.params.maxNegCash) {
            revert CTSA_InsufficientCash();
        }
    }

    function _verifySpotRfqTrade(
        IRfqModule.TradeData memory trade,
        bool isTaker,
        uint256 loanId,
        address depositAsset,
        address cash
    ) private view {
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

        CollarTSAStorage storage $ = _getCollarTSAStorage();
        ICollarLoanStore.Loan memory loan = ICollarLoanStore($.loanStore).getLoan(loanId);
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
        if (trade.price < basePrice.multiplyDecimal($.collateralManagementParams.worstSpotSellPrice)) {
            revert CTSA_SpotRfqPriceTooLow();
        }

        (, uint256 baseBalance,) = _getSubAccountStats(depositAsset, cash);
        if (amount > baseBalance) {
            revert CTSA_SpotRfqSellTooMuch();
        }
    }

    function _validateCallDetails(uint256 expiry, uint256 strike, uint256 limitPrice) private view {
        CollarTSAStorage storage $ = _getCollarTSAStorage();
        (,,,,, address manager,) = ICollarTSA(address(this)).getBaseTSAAddresses();

        $.optionRiskVerifier
            .validateCall(
                IOptionRiskVerifier.ValidateCallParams({
                    manager: manager,
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

    function _validatePutDetails(uint256 expiry, uint256 strike, uint256 limitPrice) private view {
        CollarTSAStorage storage $ = _getCollarTSAStorage();
        (,,,,, address manager,) = ICollarTSA(address(this)).getBaseTSAAddresses();

        $.optionRiskVerifier
            .validatePut(
                IOptionRiskVerifier.ValidatePutParams({
                    manager: manager,
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

    function _getSubAccountStats(address depositAsset, address cash)
        private
        view
        returns (uint256 shortCalls, uint256 baseBalance, int256 cashBalance)
    {
        CollarTSAStorage storage $ = _getCollarTSAStorage();

        uint256 accountId = ICollarTSA(address(this)).subAccount();
        (address subAccounts,,,,,,) = ICollarTSA(address(this)).getBaseTSAAddresses();
        ISubAccounts.AssetBalance[] memory balances = ISubAccounts(subAccounts).getAccountBalances(accountId);

        for (uint256 i = 0; i < balances.length; i++) {
            if (balances[i].asset == $.optionAsset) {
                int256 balance = balances[i].balance;
                if (balance == 0) {
                    continue;
                }
                (,, bool isCall) = OptionEncoding.fromSubId(uint96(balances[i].subId));
                if (balance > 0) {
                    if (isCall) {
                        revert CTSA_InvalidOptionBalance();
                    }
                } else {
                    if (!isCall) {
                        revert CTSA_InvalidOptionBalance();
                    }
                    shortCalls += balance.abs();
                }
            } else if (address(balances[i].asset) == depositAsset) {
                baseBalance = balances[i].balance.abs();
            } else if (address(balances[i].asset) == cash) {
                cashBalance = balances[i].balance;
            }
        }
    }

    function _getBasePrice() private view returns (uint256 spotPrice) {
        (spotPrice,) = _getCollarTSAStorage().baseFeed.getSpot();
    }

    error CTSA_InvalidAsset();
    error CTSA_SellingTooManyCalls();
    error CTSA_CanOnlyOpenShortCalls();
    error CTSA_OnlyLongPutsAllowed();
    error CTSA_InvalidOptionBalance();
    error CTSA_InsufficientCash();
    error CTSA_InvalidRfqTradeDetails();
    error CTSA_InvalidTradeAmount();
    error CTSA_SpotRfqRequiresTaker();
    error CTSA_SpotRfqAmountInvalid();
    error CTSA_SpotRfqPriceTooLow();
    error CTSA_SpotRfqSellTooMuch();
}
