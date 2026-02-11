// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Black76} from "lyra-utils/math/Black76.sol";
import {SafeCast} from "openzeppelin/utils/math/SafeCast.sol";
import {DecimalMath} from "lyra-utils/decimals/DecimalMath.sol";

import {IOptionRiskVerifier} from "../interfaces/IOptionRiskVerifier.sol";
import {IOptionAsset} from "v2-core/src/interfaces/IOptionAsset.sol";
import {StandardManager, IVolFeed, IForwardFeed} from "v2-core/src/risk-managers/StandardManager.sol";

contract OptionRiskVerifier is IOptionRiskVerifier {
    using SafeCast for uint256;
    using DecimalMath for uint256;

    error CTSA_OptionExpired();
    error CTSA_OptionDeltaTooHigh();
    error CTSA_OptionPriceTooLow();
    error CTSA_PutPriceTooHigh();
    error CTSA_OptionExpiryOutOfBounds();

    function validateCall(ValidateCallParams calldata params) external view {
        _validateExpiry(params.expiry, params.optionMinTimeToExpiry, params.optionMaxTimeToExpiry);

        uint256 timeToExpiry = params.expiry - block.timestamp;
        (uint256 vol, uint256 forwardPrice) =
            _getFeedValues(params.manager, params.optionAsset, params.strike, params.expiry);

        (uint256 callPrice,, uint256 callDelta) = Black76.pricesAndDelta(
            Black76.Black76Inputs({
                timeToExpirySec: timeToExpiry.toUint64(),
                volatility: (vol.multiplyDecimal(params.optionVolSlippageFactor)).toUint128(),
                fwdPrice: forwardPrice.toUint128(),
                strikePrice: params.strike.toUint128(),
                discount: 1e18
            })
        );

        if (callDelta > params.callMaxDelta) revert CTSA_OptionDeltaTooHigh();
        if (params.limitPrice <= callPrice) revert CTSA_OptionPriceTooLow();
    }

    function validatePut(ValidatePutParams calldata params) external view {
        _validateExpiry(params.expiry, params.optionMinTimeToExpiry, params.optionMaxTimeToExpiry);

        uint256 timeToExpiry = params.expiry - block.timestamp;
        (uint256 vol, uint256 forwardPrice) =
            _getFeedValues(params.manager, params.optionAsset, params.strike, params.expiry);

        (, uint256 putPrice,) = Black76.pricesAndDelta(
            Black76.Black76Inputs({
                timeToExpirySec: timeToExpiry.toUint64(),
                volatility: (vol.multiplyDecimal(params.optionVolSlippageFactor)).toUint128(),
                fwdPrice: forwardPrice.toUint128(),
                strikePrice: params.strike.toUint128(),
                discount: 1e18
            })
        );

        uint256 maxPrice = putPrice.multiplyDecimal(params.putMaxPriceFactor);
        if (params.limitPrice > maxPrice) revert CTSA_PutPriceTooHigh();
    }

    function _validateExpiry(uint256 expiry, uint256 minTimeToExpiry, uint256 maxTimeToExpiry) internal view {
        if (block.timestamp >= expiry) revert CTSA_OptionExpired();
        uint256 timeToExpiry = expiry - block.timestamp;
        if (timeToExpiry < minTimeToExpiry || timeToExpiry > maxTimeToExpiry) {
            revert CTSA_OptionExpiryOutOfBounds();
        }
    }

    function _getFeedValues(address manager, address optionAsset, uint256 strike, uint256 expiry)
        internal
        view
        returns (uint256 vol, uint256 forwardPrice)
    {
        StandardManager srm = StandardManager(manager);
        IOptionAsset option = IOptionAsset(optionAsset);
        IVolFeed volFeed;
        IForwardFeed fwdFeed;
        {
            StandardManager.AssetDetail memory assetDetails = srm.assetDetails(option);
            (, fwdFeed, volFeed) = srm.getMarketFeeds(assetDetails.marketId);
        }
        (vol,) = volFeed.getVol(strike.toUint128(), expiry.toUint64());
        (forwardPrice,) = fwdFeed.getForwardPrice(expiry.toUint64());
    }
}
