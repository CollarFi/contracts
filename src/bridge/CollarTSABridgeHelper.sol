// SPDX-License-Identifier: GPL-3.0-only
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ECDSA} from "openzeppelin/utils/cryptography/ECDSA.sol";

import {IBridgeAdapter} from "../interfaces/IBridgeAdapter.sol";
import {ICollarLoanStore} from "../interfaces/ICollarLoanStore.sol";
import {CollarTSABridgeLib} from "../libraries/CollarTSABridgeLib.sol";
import {CollarTSAStorageLib} from "../libraries/CollarTSAStorageLib.sol";

interface INonceTracker {
    function usedNonces(address owner, uint256 nonce) external view returns (bool);
}

interface ISignerRegistry {
    function isSigner(address signer) external view returns (bool);
}

contract CollarTSABridgeHelper {
    uint256 internal constant LOAN_ID_NONCE_MODULUS = 1_000_000;

    error CTSA_InvalidConfig();
    error CTSA_InsufficientValue();
    error CTSA_InvalidSignature();

    event DepositNonceRecorded(uint256 indexed loanId, uint256 nonce, bytes32 indexed actionHash);
    event TradeExecutedRecorded(uint256 indexed loanId, uint256 nonce, bytes32 indexed actionHash);
    event WithdrawNonceRecorded(uint256 indexed loanId, uint256 nonce, bytes32 indexed actionHash);

    function bridgeToL1(address asset, uint256 amount, address receiver)
        external
        payable
        returns (bytes32 socketMessageId)
    {
        CollarTSAStorageLib.CollarTSAStorage storage $ = CollarTSAStorageLib.get();
        IBridgeAdapter adapter = $.bridge.socketBridgeConfigs[asset];
        if (msg.sender != $.bridge.bridgeCoordinator || receiver == address(0) || address(adapter) == address(0)) {
            revert CTSA_InvalidConfig();
        }

        uint256 fee = adapter.estimateFee();
        if (msg.value != fee) {
            revert CTSA_InsufficientValue();
        }

        socketMessageId = adapter.messageId();
        IERC20(asset).approve(address(adapter), amount);
        adapter.bridge{value: fee}(receiver, amount);
    }

    function estimateBridgeFees(address asset) external view returns (uint256) {
        return CollarTSABridgeLib.estimateBridgeFees(CollarTSAStorageLib.get().bridge, asset);
    }

    function estimateAdapterFee(address adapter) external view returns (uint256) {
        if (adapter == address(0)) {
            revert CTSA_InvalidConfig();
        }
        return IBridgeAdapter(adapter).estimateFee();
    }

    function validateSigner(bytes32 hash, bytes memory signerSig) external view {
        (address recovered, ECDSA.RecoverError error,) = ECDSA.tryRecover(hash, signerSig);
        if (error != ECDSA.RecoverError.NoError || !ISignerRegistry(msg.sender).isSigner(recovered)) {
            revert CTSA_InvalidSignature();
        }
    }

    function depositExecutionNonce(uint256 loanId) external view returns (uint256) {
        return CollarTSAStorageLib.get().depositExecutionNonce[loanId];
    }

    function withdrawExecutionNonce(uint256 loanId) external view returns (uint256) {
        return CollarTSAStorageLib.get().withdrawExecutionNonce[loanId];
    }

    function depositExecuted(uint256 loanId) external view returns (bool) {
        CollarTSAStorageLib.CollarTSAStorage storage $ = CollarTSAStorageLib.get();
        uint256 nonce = $.depositExecutionNonce[loanId];
        if (nonce == 0 || address($.depositModule) == address(0)) {
            return false;
        }
        return INonceTracker(address($.depositModule)).usedNonces(address(this), nonce);
    }

    function usedNonce(address module, uint256 nonce) external view returns (bool) {
        return INonceTracker(module).usedNonces(msg.sender, nonce);
    }

    function withdrawExecuted(uint256 loanId) external view returns (bool) {
        CollarTSAStorageLib.CollarTSAStorage storage $ = CollarTSAStorageLib.get();
        uint256 nonce = $.withdrawExecutionNonce[loanId];
        if (nonce == 0 || address($.withdrawalModule) == address(0)) {
            return false;
        }
        return INonceTracker(address($.withdrawalModule)).usedNonces(address(this), nonce);
    }

    function recordExecution(address module, uint256 nonce, bytes32 actionHash, bool hasExtraData) external {
        CollarTSAStorageLib.CollarTSAStorage storage $ = CollarTSAStorageLib.get();
        uint256 loanId = nonce % LOAN_ID_NONCE_MODULUS;

        if (module == address($.depositModule)) {
            $.depositExecutionNonce[loanId] = nonce;
            ICollarLoanStore($.loanStore).setDepositExecuted(loanId, true);
            emit DepositNonceRecorded(loanId, nonce, actionHash);
        } else if (module == address($.withdrawalModule)) {
            $.withdrawExecutionNonce[loanId] = nonce;
            emit WithdrawNonceRecorded(loanId, nonce, actionHash);
        } else if (module == address($.rfqModule) && hasExtraData) {
            ICollarLoanStore($.loanStore).setTradeExecuted(loanId, true);
            emit TradeExecutedRecorded(loanId, nonce, actionHash);
        }
    }
}
