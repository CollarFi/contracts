// SPDX-License-Identifier: GPL-3.0-only
pragma solidity ^0.8.20;

import {TSATestUtils} from "./TSATestUtils.sol";
import {CollarTSA} from "../../src/CollarTSA.sol";
import {CollarLoanStore} from "../../src/CollarLoanStore.sol";
import {CollateralManagementTSA} from "v2-matching/src/tokenizedSubaccounts/CollateralManagementTSA.sol";
import {IWrappedERC20Asset} from "v2-core/src/interfaces/IWrappedERC20Asset.sol";
import {ISpotFeed} from "v2-core/src/interfaces/ISpotFeed.sol";
import {IOptionAsset} from "v2-core/src/interfaces/IOptionAsset.sol";
import {
    TransparentUpgradeableProxy,
    ITransparentUpgradeableProxy
} from "openzeppelin/proxy/transparent/TransparentUpgradeableProxy.sol";
import {BaseTSA, BaseOnChainSigningTSA} from "v2-matching/src/tokenizedSubaccounts/CCTSA.sol";

contract CollarTSATestUtils is TSATestUtils {
    CollarTSA public tsaImplementation;
    CollarTSA internal collarTsa;
    CollarLoanStore internal loanStore;

    CollarTSA.CollarTSAParams public defaultCollarParams = CollarTSA.CollarTSAParams({
        minSignatureExpiry: 5 minutes,
        maxSignatureExpiry: 30 minutes,
        optionVolSlippageFactor: 0.9e18,
        callMaxDelta: 0.4e18,
        maxNegCash: -100e18,
        optionMinTimeToExpiry: 1 days,
        optionMaxTimeToExpiry: 30 days,
        putMaxPriceFactor: 1.1e18
    });

    CollateralManagementTSA.CollateralManagementParams public defaultCollateralManagementParams =
        CollateralManagementTSA.CollateralManagementParams({
            feeFactor: 0.01e18,
            spotTransactionLeniency: 1.01e18,
            worstSpotBuyPrice: 1.01e18,
            worstSpotSellPrice: 0.99e18
        });

    function upgradeToCollarTSA(string memory market) internal {
        IWrappedERC20Asset wrappedDepositAsset;
        ISpotFeed baseFeed;
        IOptionAsset optionAsset;

        if (keccak256(abi.encodePacked(market)) == keccak256(abi.encodePacked("usdc"))) {
            wrappedDepositAsset = IWrappedERC20Asset(address(cash));
            baseFeed = stableFeed;
            optionAsset = IOptionAsset(address(0));
        } else {
            wrappedDepositAsset = markets[market].base;
            baseFeed = markets[market].spotFeed;
            optionAsset = markets[market].option;
        }

        tsaImplementation = new CollarTSA();

        loanStore = new CollarLoanStore(address(this));

        proxyAdmin.upgradeAndCall(
            ITransparentUpgradeableProxy(address(proxy)),
            address(tsaImplementation),
            abi.encodeWithSelector(
                tsaImplementation.initialize.selector,
                address(this),
                BaseTSA.BaseTSAInitParams({
                    subAccounts: subAccounts,
                    auction: auction,
                    cash: cash,
                    wrappedDepositAsset: wrappedDepositAsset,
                    manager: srm,
                    matching: matching,
                    symbol: "Tokenised SubAccount",
                    name: "TSA"
                }),
                CollarTSA.CollarTSAInitParams({
                    baseFeed: baseFeed,
                    depositModule: depositModule,
                    withdrawalModule: withdrawalModule,
                    tradeModule: tradeModule,
                    rfqModule: rfqModule,
                    optionAsset: optionAsset,
                    loanStore: address(loanStore)
                })
            )
        );

        tsa = BaseOnChainSigningTSA(address(proxy));
        tsaSubacc = tsa.subAccount();
        collarTsa = CollarTSA(address(proxy));
    }

    function setupCollarTSA() internal {
        tsa.setTSAParams(
            BaseTSA.TSAParams({
                depositCap: 10000e18,
                minDepositValue: 1e18,
                depositScale: 1e18,
                withdrawScale: 1e18,
                managementFee: 0,
                feeRecipient: address(0)
            })
        );

        CollarTSA(address(tsa)).setCollarTSAParams(defaultCollarParams);
        CollarTSA(address(tsa)).setCollateralManagementParams(defaultCollateralManagementParams);

        // loanStore is now required in initialize()

        tsa.setShareKeeper(address(this), true);

        signerPk = 0xBEEF;
        signer = vm.addr(signerPk);

        tsa.setSigner(signer, true);
    }

    function _seedLoan(uint256 loanId, uint64 maturity) internal {
        // Generous collateral amount for tests; TSA enforces that spot sells don't exceed this.
        loanStore.recordCollateral(loanId, address(markets[MARKET].base), 1_000_000e18);
        loanStore.recordMandate(
            loanId, address(0xB0B0), address(markets[MARKET].base), 0, 0, 0, maturity, uint64(block.timestamp + 1 days)
        );
    }
}
