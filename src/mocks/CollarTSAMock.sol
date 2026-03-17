// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @dev Minimal mock implementing the subset used by CollarTSAReceiver.
contract CollarTSAMock {
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

    uint256 public subAccount;
    CollarTSAParams private params;

    event SignedAction(bytes32 actionHash);

    constructor(uint256 subAccount_) {
        subAccount = subAccount_;
        params = CollarTSAParams({
            minSignatureExpiry: 60,
            maxSignatureExpiry: 3600,
            optionVolSlippageFactor: 0,
            callMaxDelta: 0,
            maxNegCash: 0,
            optionMinTimeToExpiry: 0,
            optionMaxTimeToExpiry: 0,
            putMaxPriceFactor: 0
        });
    }

    // Signature hook used by the receiver.
    function signActionData(bytes memory action, bytes memory extraData) external {
        emit SignedAction(keccak256(abi.encode(action, extraData)));
    }

    function signActionViaPermit(bytes memory action, bytes memory extraData, bytes memory signerSig) external {
        emit SignedAction(keccak256(abi.encode(action, extraData, signerSig)));
    }

    function getCollarTSAParams() external view returns (CollarTSAParams memory) {
        return params;
    }

    // Return placeholder addresses; receiver only uses them to build an action struct.
    function getCollarTSAAddresses() external pure returns (address, address, address, address, address, address) {
        return (address(0), address(0x1111), address(0x2222), address(0x3333), address(0x4444), address(0x5555));
    }

    function getBaseTSAAddresses()
        external
        pure
        returns (address, address, address, address, address, address, address)
    {
        // wrappedDepositAsset must be non-zero; the rest is unused by the receiver.
        return (address(0), address(0), address(0x6666), address(0), address(0), address(0), address(0));
    }

    function depositExecutionNonce(uint256) external pure returns (uint256) {
        return 0;
    }

    function depositExecuted(uint256) external pure returns (bool) {
        return false;
    }

    function withdrawExecutionNonce(uint256) external pure returns (uint256) {
        return 0;
    }

    function withdrawExecuted(uint256) external pure returns (bool) {
        return false;
    }
}
