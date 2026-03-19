// SPDX-License-Identifier: MIT

pragma solidity ^0.8.20;

import {OAppSenderUpgradeable, MessagingFee, MessagingReceipt} from "./OAppSenderUpgradeable.sol";
import {OAppReceiverUpgradeable, Origin} from "./OAppReceiverUpgradeable.sol";
import {OAppCoreUpgradeable} from "./OAppCoreUpgradeable.sol";

abstract contract OAppUpgradeable is OAppSenderUpgradeable, OAppReceiverUpgradeable {
    constructor(address _endpoint) OAppCoreUpgradeable(_endpoint) {}

    function __OApp_init(address _delegate) internal onlyInitializing {
        __OAppCore_init(_delegate);
        __OAppReceiver_init_unchained();
        __OAppSender_init_unchained();
    }

    function __OApp_init_unchained() internal onlyInitializing {}

    function oAppVersion()
        public
        pure
        virtual
        override(OAppSenderUpgradeable, OAppReceiverUpgradeable)
        returns (uint64 senderVersion, uint64 receiverVersion)
    {
        return (SENDER_VERSION, RECEIVER_VERSION);
    }
}
