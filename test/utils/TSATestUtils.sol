pragma solidity ^0.8.20;

import "v2-core/test/integration-tests/shared/IntegrationTestBase.t.sol";
import {MatchingHelpers} from "./MatchingBase.t.sol";
import {TokenizedSubAccount} from "v2-matching/src/tokenizedSubaccounts/TSA.sol";
import {
    TransparentUpgradeableProxy,
    ITransparentUpgradeableProxy
} from "openzeppelin/proxy/transparent/TransparentUpgradeableProxy.sol";
import {ProxyAdmin} from "openzeppelin/proxy/transparent/ProxyAdmin.sol";
import {BaseTSA, BaseOnChainSigningTSA, CollateralManagementTSA} from "v2-matching/src/tokenizedSubaccounts/CCTSA.sol";
// forge-lint: disable-next-line(unused-import)
import {LeveragedBasisTSA} from "v2-matching/src/tokenizedSubaccounts/LevBasisTSA.sol";
// forge-lint: disable-next-line(unused-import)
import {PrincipalProtectedTSA} from "v2-matching/src/tokenizedSubaccounts/PPTSA.sol";

import {OptionEncoding} from "lyra-utils/encoding/OptionEncoding.sol";

// forge-lint: disable-next-line(unused-import)
import {IDutchAuction} from "v2-core/src/interfaces/IDutchAuction.sol";
// forge-lint: disable-next-line(unused-import)
import {IManager} from "v2-core/src/interfaces/IManager.sol";
import {ITradeModule} from "v2-matching/src/interfaces/ITradeModule.sol";
import {IRfqModule} from "v2-matching/src/interfaces/IRfqModule.sol";
// forge-lint: disable-next-line(unused-import)
import {IDepositModule} from "v2-matching/src/interfaces/IDepositModule.sol";
// forge-lint: disable-next-line(unused-import)
import {IWithdrawalModule} from "v2-matching/src/interfaces/IWithdrawalModule.sol";
import {IActionVerifier} from "v2-matching/src/interfaces/IActionVerifier.sol";
// forge-lint: disable-next-line(unused-import)
import {ISubAccounts} from "v2-core/src/interfaces/ISubAccounts.sol";
// forge-lint: disable-next-line(unused-import)
import {IMatchingModule} from "v2-matching/src/interfaces/IMatchingModule.sol";
import {SignedMath} from "openzeppelin/utils/math/SignedMath.sol";
import {MockTSA} from "v2-matching/test/tokenizedSubaccounts/utils/MockTSA.sol";

contract TSATestUtils is IntegrationTestBase, MatchingHelpers {
    using SignedMath for int256;

    string public MARKET = "wbtc";
    uint256 public MARKET_UNIT;
    uint256 public MARKET_REF_SPOT;

    uint256 internal signerPk;
    address internal signer;
    uint256 internal tsaNonce = 0;

    uint256 internal nonVaultPk;
    address internal nonVaultAddr;
    uint256 internal nonVaultNonce = 0;
    uint256 internal nonVaultSubacc;

    TransparentUpgradeableProxy proxy;
    ProxyAdmin proxyAdmin;

    MockTSA public mockTsa;

    BaseOnChainSigningTSA public tsa;
    uint256 public tsaSubacc;

    function setUp() public virtual {
        _setupIntegrationTestComplete();
        _setupMatching();
        _setupTaker();
        _setupMarketFeeds();
        _setupTradeModule();
        _setupRfqModule();

        MARKET_UNIT = 10 ** markets[MARKET].erc20.decimals();

        _setSpotPrice("weth", 2000e18, 1e18);
        _setSpotPrice("wbtc", 25000e18, 1e18);
        (uint256 spot, uint256 conf) = markets[MARKET].spotFeed.getSpot();
        MARKET_REF_SPOT = spot;
    }

    function _setupMatching() internal {
        MatchingHelpers._deployMatching(subAccounts, address(cash), auction, 0);
        cash.setWhitelistManager(address(markets[MARKET].pmrm), true);
    }

    function _setupTaker() internal {
        nonVaultPk = 0xDEAD;
        nonVaultAddr = vm.addr(nonVaultPk);
        nonVaultSubacc = subAccounts.createAccount(address(this), markets[MARKET].pmrm);
        usdc.mint(address(this), 1_000_000e6);
        usdc.approve(address(cash), 1_000_000e6);
        cash.deposit(nonVaultSubacc, 1_000_000e6);

        markets[MARKET].base.setTotalPositionCap(markets[MARKET].pmrm, 1_000_000e18);
        markets[MARKET].base.setTotalPositionCap(srm, 1_000_000e18);

        markets[MARKET].erc20.mint(address(this), 100_000e18);
        markets[MARKET].erc20.approve(address(markets[MARKET].base), 100_000e18);
        markets[MARKET].base.deposit(nonVaultSubacc, 100_000e18);

        subAccounts.setApprovalForAll(address(matching), true);
        matching.depositSubAccountFor(nonVaultSubacc, nonVaultAddr);
    }

    function _setupMarketFeeds() internal {
        markets[MARKET].spotFeed.setHeartbeat(100 weeks);
        markets[MARKET].forwardFeed.setHeartbeat(100 weeks);
        markets[MARKET].volFeed.setHeartbeat(100 weeks);
        markets[MARKET].perpFeed.setHeartbeat(100 weeks);
        markets[MARKET].ibpFeed.setHeartbeat(100 weeks);
        markets[MARKET].iapFeed.setHeartbeat(100 weeks);
        markets[MARKET].rateFeed.setHeartbeat(100 weeks);
    }

    function _setupTradeModule() internal {
        tradeModule.setFeeRecipient(bobAcc);
        srm.setBorrowingEnabled(true);
        srm.setBaseAssetMarginFactor(markets["weth"].id, 0.8e18, 0.8e18);
        srm.setBaseAssetMarginFactor(markets["wbtc"].id, 0.8e18, 0.8e18);
        tradeModule.setPerpAsset(markets[MARKET].perp, true);
    }

    function _setupRfqModule() internal {
        rfqModule.setFeeRecipient(bobAcc);
        rfqModule.setPerpAsset(markets[MARKET].perp, true);
    }

    function deployPredeposit(address erc20) internal {
        TokenizedSubAccount tsaImplementation = new TokenizedSubAccount();

        vm.recordLogs();
        proxy = new TransparentUpgradeableProxy(
            address(tsaImplementation),
            address(this),
            abi.encodeWithSelector(tsaImplementation.initialize.selector, "TSA", "TSA", erc20)
        );

        Vm.Log[] memory logs = vm.getRecordedLogs();
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].topics[0] == keccak256("AdminChanged(address,address)")) {
                (address oldAdmin, address newAdmin) = abi.decode(logs[i].data, (address, address));
                proxyAdmin = ProxyAdmin(newAdmin);
                break;
            }
        }
    }

    function upgradeToMockTSA(address wrappedDepositAsset) internal {
        MockTSA mockTsaImp = new MockTSA();
        proxyAdmin.upgradeAndCall(
            ITransparentUpgradeableProxy(address(proxy)),
            address(mockTsaImp),
            abi.encodeWithSelector(
                mockTsa.initialize.selector,
                address(this),
                BaseTSA.BaseTSAInitParams({
                    subAccounts: subAccounts,
                    auction: auction,
                    cash: cash,
                    wrappedDepositAsset: IWrappedERC20Asset(wrappedDepositAsset),
                    manager: srm,
                    matching: matching,
                    symbol: "Tokenised SubAccount",
                    name: "TSA"
                })
            )
        );

        mockTsa = MockTSA(address(proxy));

        mockTsa.setTSAParams(
            BaseTSA.TSAParams({
                depositCap: type(uint256).max,
                minDepositValue: 0,
                depositScale: 1e18,
                withdrawScale: 1e18,
                managementFee: 0,
                feeRecipient: address(0)
            })
        );

        mockTsa.setShareKeeper(address(this), true);

        signerPk = 0xBEEF;
        signer = vm.addr(signerPk);

        mockTsa.setSigner(signer, true);
    }

    function _setFixedSVIDataForExpiry(string memory key, uint64 expiry) internal {
        vm.warp(block.timestamp + 1);

        LyraForwardFeed forwardFeed = markets[key].forwardFeed;
        (uint256 fwdPrice,) = forwardFeed.getForwardPrice(uint64(expiry));

        LyraVolFeed volFeed = markets[key].volFeed;

        IBaseLyraFeed.FeedData memory feedData = _getFixedVolData(expiry, fwdPrice);

        // sign data
        bytes memory data = _signFeedData(volFeed, keeperPk, feedData);

        volFeed.acceptData(data);
    }

    function _getFixedVolData(uint64 expiry, uint256 fwdPrice) internal view returns (IBaseLyraFeed.FeedData memory) {
        uint64 SVI_refTau = uint64(Black76.annualise(uint64(expiry - block.timestamp)));
        // vol will be sqrt(1.8)
        int256 SVI_a = int256((1.8e18) * uint256(SVI_refTau) / 1e18);
        uint256 SVI_b = 0;
        int256 SVI_rho = 0;
        int256 SVI_m = 0;
        uint256 SVI_sigma = 0;
        uint256 SVI_fwd = fwdPrice;
        uint64 confidence = 1e18;

        // example data: a = 1, b = 1.5, sig = 0.05, rho = -0.1, m = -0.05
        bytes memory volData =
            abi.encode(expiry, SVI_a, SVI_b, SVI_rho, SVI_m, SVI_sigma, SVI_fwd, SVI_refTau, confidence);
        return IBaseLyraFeed.FeedData({
            data: volData,
            timestamp: uint64(block.timestamp),
            deadline: block.timestamp + 5,
            signers: new address[](1),
            signatures: new bytes[](1)
        });
    }

    function _createMatchedTrade(
        uint256 takerAccount,
        uint256 makerAcc,
        uint256 amountFilled,
        int256 price,
        uint256 takerFee,
        uint256 makerFee
    ) internal pure returns (bytes memory) {
        ITradeModule.FillDetails memory fillDetails = ITradeModule.FillDetails({
            filledAccount: makerAcc, amountFilled: amountFilled, price: price, fee: makerFee
        });

        ITradeModule.FillDetails[] memory fills = new ITradeModule.FillDetails[](1);
        fills[0] = fillDetails;

        ITradeModule.OrderData memory orderData = ITradeModule.OrderData({
            takerAccount: takerAccount, takerFee: takerFee, fillDetails: fills, managerData: bytes("")
        });

        bytes memory encodedAction = abi.encode(orderData);
        return encodedAction;
    }

    function _tradeOption(int256 amount, uint256 price, uint256 expiry, uint256 strike) internal {
        // forge-lint: disable-next-line(unsafe-typecast)
        _setForwardPrice(MARKET, uint64(expiry), 2000e18, 1e18);
        // forge-lint: disable-next-line(unsafe-typecast)
        _setFixedSVIDataForExpiry(MARKET, uint64(expiry));

        bytes memory tradeData = abi.encode(
            ITradeModule.TradeData({
                asset: address(markets[MARKET].option),
                subId: OptionEncoding.toSubId(expiry, strike, true),
                // forge-lint: disable-next-line(unsafe-typecast)
                limitPrice: int256(price),
                desiredAmount: int256(amount.abs()),
                worstFee: 1e18,
                recipientId: tsaSubacc,
                isBid: amount > 0
            })
        );

        bytes memory tradeMaker = abi.encode(
            ITradeModule.TradeData({
                asset: address(markets[MARKET].option),
                subId: OptionEncoding.toSubId(expiry, strike, true),
                // forge-lint: disable-next-line(unsafe-typecast)
                limitPrice: int256(price),
                desiredAmount: int256(amount.abs()),
                worstFee: 1e18,
                recipientId: nonVaultSubacc,
                isBid: amount < 0
            })
        );

        IActionVerifier.Action[] memory actions = new IActionVerifier.Action[](2);
        bytes[] memory signatures = new bytes[](2);

        actions[0] = IActionVerifier.Action({
            subaccountId: tsaSubacc,
            nonce: ++tsaNonce,
            module: tradeModule,
            data: tradeData,
            expiry: block.timestamp + 8 minutes,
            owner: address(tsa),
            signer: address(tsa)
        });

        (actions[1], signatures[1]) = _createActionAndSign(
            nonVaultSubacc,
            ++nonVaultNonce,
            address(tradeModule),
            tradeMaker,
            block.timestamp + 1 days,
            nonVaultAddr,
            nonVaultAddr,
            nonVaultPk
        );

        vm.prank(signer);
        tsa.signActionData(actions[0], "");

        _verifyAndMatch(
            actions,
            signatures,
            _createMatchedTrade(
                tsaSubacc,
                nonVaultSubacc,
                amount.abs(),
                // forge-lint: disable-next-line(unsafe-typecast)
                int256(price),
                // trade fees
                0,
                0
            )
        );
    }

    function _tradeSpot(int256 amount, uint256 price) internal {
        bytes memory tradeData = abi.encode(
            ITradeModule.TradeData({
                asset: address(markets[MARKET].base),
                subId: 0,
                // forge-lint: disable-next-line(unsafe-typecast)
                limitPrice: int256(price),
                desiredAmount: int256(amount.abs()),
                worstFee: 1e18,
                recipientId: tsaSubacc,
                isBid: amount > 0
            })
        );

        bytes memory tradeMaker = abi.encode(
            ITradeModule.TradeData({
                asset: address(markets[MARKET].base),
                subId: 0,
                // forge-lint: disable-next-line(unsafe-typecast)
                limitPrice: int256(price),
                desiredAmount: int256(amount.abs()),
                worstFee: 1e18,
                recipientId: nonVaultSubacc,
                isBid: amount < 0
            })
        );

        IActionVerifier.Action[] memory actions = new IActionVerifier.Action[](2);
        bytes[] memory signatures = new bytes[](2);

        actions[0] = IActionVerifier.Action({
            subaccountId: tsaSubacc,
            nonce: ++tsaNonce,
            module: tradeModule,
            data: tradeData,
            expiry: block.timestamp + 8 minutes,
            owner: address(tsa),
            signer: address(tsa)
        });

        (actions[1], signatures[1]) = _createActionAndSign(
            nonVaultSubacc,
            ++nonVaultNonce,
            address(tradeModule),
            tradeMaker,
            block.timestamp + 1 days,
            nonVaultAddr,
            nonVaultAddr,
            nonVaultPk
        );

        vm.prank(signer);
        tsa.signActionData(actions[0], "");

        _verifyAndMatch(
            actions,
            signatures,
            _createMatchedTrade(
                tsaSubacc,
                nonVaultSubacc,
                amount.abs(),
                // forge-lint: disable-next-line(unsafe-typecast)
                int256(price),
                // trade fees
                0,
                0
            )
        );
    }

    function _getSpotTradeAction(int256 amount, uint256 price) internal returns (IActionVerifier.Action memory) {
        IActionVerifier.Action memory action = IActionVerifier.Action({
            subaccountId: tsaSubacc,
            nonce: ++tsaNonce,
            module: tradeModule,
            data: abi.encode(
                ITradeModule.TradeData({
                    asset: address(markets[MARKET].base),
                    subId: 0,
                    // forge-lint: disable-next-line(unsafe-typecast)
                    limitPrice: int256(price),
                    desiredAmount: int256(amount.abs()),
                    worstFee: 1e18,
                    recipientId: tsaSubacc,
                    isBid: amount > 0
                })
            ),
            expiry: block.timestamp + 8 minutes,
            owner: address(tsa),
            signer: address(tsa)
        });

        return action;
    }

    function _tradePerp(int256 amount, uint256 price) internal {
        bytes memory tradeMaker = abi.encode(
            ITradeModule.TradeData({
                asset: address(markets[MARKET].perp),
                subId: 0,
                // forge-lint: disable-next-line(unsafe-typecast)
                limitPrice: int256(price),
                desiredAmount: int256(amount.abs()),
                worstFee: 1e18,
                recipientId: nonVaultSubacc,
                isBid: amount < 0
            })
        );

        IActionVerifier.Action[] memory actions = new IActionVerifier.Action[](2);
        bytes[] memory signatures = new bytes[](2);

        actions[0] = IActionVerifier.Action({
            subaccountId: tsaSubacc,
            nonce: ++tsaNonce,
            module: tradeModule,
            data: abi.encode(
                ITradeModule.TradeData({
                    asset: address(markets[MARKET].perp),
                    subId: 0,
                    // forge-lint: disable-next-line(unsafe-typecast)
                    limitPrice: int256(price),
                    desiredAmount: int256(amount.abs()),
                    worstFee: 1e18,
                    recipientId: tsaSubacc,
                    isBid: amount > 0
                })
            ),
            expiry: block.timestamp + 8 minutes,
            owner: address(tsa),
            signer: address(tsa)
        });

        (actions[1], signatures[1]) = _createActionAndSign(
            nonVaultSubacc,
            ++nonVaultNonce,
            address(tradeModule),
            tradeMaker,
            block.timestamp + 1 days,
            nonVaultAddr,
            nonVaultAddr,
            nonVaultPk
        );

        vm.prank(signer);
        tsa.signActionData(actions[0], "");

        _verifyAndMatch(
            // forge-lint: disable-next-line(unsafe-typecast)
            actions,
            signatures,
            // forge-lint: disable-next-line(unsafe-typecast)
            _createMatchedTrade(tsaSubacc, nonVaultSubacc, amount.abs(), int256(price), 0, 0)
        );
    }

    function _createDepositAction(uint256 amount) internal returns (IActionVerifier.Action memory) {
        bytes memory depositData = _encodeDepositData(amount, address(markets[MARKET].base), address(0));

        IActionVerifier.Action memory action = IActionVerifier.Action({
            subaccountId: tsaSubacc,
            nonce: ++tsaNonce,
            module: depositModule,
            data: depositData,
            expiry: block.timestamp + 8 minutes,
            owner: address(tsa),
            signer: address(tsa)
        });

        return action;
    }

    function _executeDeposit(uint256 amount) internal {
        IActionVerifier.Action memory action = _createDepositAction(amount);
        vm.prank(signer);
        tsa.signActionData(action, "");

        _submitToMatching(action);
    }

    function _createWithdrawalAction(uint256 amount) internal returns (IActionVerifier.Action memory) {
        bytes memory withdrawalData = _encodeWithdrawData(amount, address(markets[MARKET].base));

        IActionVerifier.Action memory action = IActionVerifier.Action({
            subaccountId: tsaSubacc,
            nonce: ++tsaNonce,
            module: withdrawalModule,
            data: withdrawalData,
            expiry: block.timestamp + 8 minutes,
            owner: address(tsa),
            signer: address(tsa)
        });

        return action;
    }

    function _executeWithdrawal(uint256 amount) internal {
        IActionVerifier.Action memory action = _createWithdrawalAction(amount);
        vm.prank(signer);
        tsa.signActionData(action, "");

        _submitToMatching(action);
    }

    function _submitToMatching(IActionVerifier.Action memory action) internal {
        bytes memory encodedAction = abi.encode(action);
        IActionVerifier.Action[] memory actions = new IActionVerifier.Action[](1);
        bytes[] memory signatures = new bytes[](1);
        actions[0] = action;
        _verifyAndMatch(actions, signatures, encodedAction);
    }

    function _depositToTSA(uint256 amount) internal {
        markets[MARKET].erc20.mint(address(this), amount);
        markets[MARKET].erc20.approve(address(tsa), amount);
        uint256 depositId = tsa.initiateDeposit(amount, address(this));
        tsa.processDeposit(depositId);
    }

    function _createInsolventAuction() internal returns (uint256 auctionId) {
        auction.setSMAccount(securityModule.accountId());

        uint256 newSubacc = subAccounts.createAccount(address(this), srm);
        vm.warp(block.timestamp + 1);
        _setSpotPrice("wbtc", 1_000_000e18, 1e18);

        markets["wbtc"].erc20.mint(address(this), 1e18);
        markets["wbtc"].erc20.approve(address(markets["wbtc"].base), 1e18);
        markets["wbtc"].base.deposit(newSubacc, 1e18);

        cash.withdraw(newSubacc, 200_000e6, address(1234));

        vm.warp(block.timestamp + 1);
        _setSpotPrice("wbtc", 25000e18, 1e18);

        auction.startAuction(newSubacc, 0);

        return newSubacc;
    }

    function _clearAuction(uint256 auctionId) internal {
        uint256 newSubacc = subAccounts.createAccount(address(this), srm);
        usdc.mint(address(this), 1e18);
        usdc.approve(address(cash), 1e18);
        cash.deposit(newSubacc, 1e18);

        auction.bid(auctionId, newSubacc, 1e18, 0, 0);

        // mint a crazy amount of cash to clear insolvency...
        usdc.mint(address(cash), 1e18);
        cash.disableWithdrawFee();
    }

    function _setupRfq(
        int256 amount,
        uint256 price,
        uint256 expiry,
        uint256 strike,
        uint256 price2,
        uint256 strike2,
        bool isCallSpread
    ) internal returns (IRfqModule.RfqOrder memory order, IRfqModule.TakerOrder memory takerOrder) {
        // forge-lint: disable-next-line(unsafe-typecast)
        _setForwardPrice(MARKET, uint64(expiry), 2000e18, 1e18);
        // forge-lint: disable-next-line(unsafe-typecast)
        _setFixedSVIDataForExpiry(MARKET, uint64(expiry));

        IRfqModule.TradeData[] memory trades = new IRfqModule.TradeData[](2);
        trades[0] = IRfqModule.TradeData({
            asset: address(markets[MARKET].option),
            subId: OptionEncoding.toSubId(expiry, strike, isCallSpread),
            price: price,
            amount: amount
        });

        trades[1] = IRfqModule.TradeData({
            asset: address(markets[MARKET].option),
            subId: OptionEncoding.toSubId(expiry, strike2, isCallSpread),
            price: price2,
            amount: -amount
        });

        order = IRfqModule.RfqOrder({maxFee: 0, trades: trades});

        takerOrder = IRfqModule.TakerOrder({orderHash: keccak256(abi.encode(trades)), maxFee: 0});
    }

    function _tradeRfqAsTaker(
        int256 amount,
        uint256 price,
        uint256 expiry,
        uint256 strike,
        uint256 price2,
        uint256 strike2,
        bool isCallSpread
    ) internal {
        (IRfqModule.RfqOrder memory order, IRfqModule.TakerOrder memory takerOrder) =
            _setupRfq(amount, price, expiry, strike, price2, strike2, isCallSpread);
        IActionVerifier.Action[] memory actions = new IActionVerifier.Action[](2);
        bytes[] memory signatures = new bytes[](2);
        // maker order
        (actions[0], signatures[0]) = _createActionAndSign(
            nonVaultSubacc,
            ++nonVaultNonce,
            address(rfqModule),
            abi.encode(order),
            block.timestamp + 1 days,
            nonVaultAddr,
            nonVaultAddr,
            nonVaultPk
        );

        // taker order
        actions[1] = IActionVerifier.Action({
            subaccountId: tsaSubacc,
            nonce: ++tsaNonce,
            module: rfqModule,
            data: abi.encode(takerOrder),
            expiry: block.timestamp + 8 minutes,
            owner: address(tsa),
            signer: address(tsa)
        });
        vm.prank(signer);
        tsa.signActionData(actions[1], abi.encode(uint256(1), abi.encode(order.trades)));

        IRfqModule.FillData memory fill = IRfqModule.FillData({
            makerAccount: nonVaultSubacc, takerAccount: tsaSubacc, makerFee: 0, takerFee: 0, managerData: bytes("")
        });

        _verifyAndMatch(actions, signatures, abi.encode(fill));
    }

    function _tradeRfqAsMaker(
        int256 amount,
        uint256 price,
        uint256 expiry,
        uint256 strike,
        uint256 price2,
        uint256 strike2,
        bool isCallSpread
    ) internal {
        (IRfqModule.RfqOrder memory order, IRfqModule.TakerOrder memory takerOrder) =
            _setupRfq(amount, price, expiry, strike, price2, strike2, isCallSpread);
        IActionVerifier.Action[] memory actions = new IActionVerifier.Action[](2);
        bytes[] memory signatures = new bytes[](2);

        actions[0] = IActionVerifier.Action({
            subaccountId: tsaSubacc,
            nonce: ++tsaNonce,
            module: rfqModule,
            data: abi.encode(order),
            expiry: block.timestamp + 8 minutes,
            owner: address(tsa),
            signer: address(tsa)
        });

        (actions[1], signatures[1]) = _createActionAndSign(
            nonVaultSubacc,
            ++nonVaultNonce,
            address(rfqModule),
            abi.encode(takerOrder),
            block.timestamp + 1 days,
            nonVaultAddr,
            nonVaultAddr,
            nonVaultPk
        );
        vm.prank(signer);
        tsa.signActionData(actions[0], "");

        IRfqModule.FillData memory fill = IRfqModule.FillData({
            makerAccount: tsaSubacc, takerAccount: nonVaultSubacc, makerFee: 0, takerFee: 0, managerData: bytes("")
        });

        _verifyAndMatch(actions, signatures, abi.encode(fill));
    }
}
