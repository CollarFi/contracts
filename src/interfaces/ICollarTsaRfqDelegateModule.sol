// SPDX-License-Identifier: GPL-3.0-only
pragma solidity ^0.8.20;

import {IMatching} from "v2-matching/src/interfaces/IMatching.sol";

interface ICollarTsaRfqDelegateModule {
    function verifyRfqAction(IMatching.Action memory action, bytes memory extraData) external;
}
