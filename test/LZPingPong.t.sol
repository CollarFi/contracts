// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {
    MessagingParams,
    MessagingFee,
    MessagingReceipt,
    Origin
} from "@layerzerolabs/lz-evm-protocol-v2/contracts/interfaces/ILayerZeroEndpointV2.sol";

import {LZPingPong} from "../src/bridge/harness/LZPingPong.sol";

contract MockEndpointV2Harness {
    uint64 public nonce;
    uint256 public quoteFee = 1;
    address public delegate;

    uint32 public lastDstEid;
    bytes32 public lastReceiver;
    bytes public lastMessage;
    bytes public lastOptions;
    bool public lastPayInLzToken;
    uint256 public lastNativeFee;
    bytes32 public lastGuid;

    function setQuoteFee(uint256 fee) external {
        quoteFee = fee;
    }

    function setDelegate(address delegate_) external {
        delegate = delegate_;
    }

    function lzToken() external pure returns (address) {
        return address(0);
    }

    function quote(MessagingParams calldata, address) external view returns (MessagingFee memory) {
        return MessagingFee({nativeFee: quoteFee, lzTokenFee: 0});
    }

    function send(MessagingParams calldata params, address) external payable returns (MessagingReceipt memory) {
        nonce++;
        lastDstEid = params.dstEid;
        lastReceiver = params.receiver;
        lastMessage = params.message;
        lastOptions = params.options;
        lastPayInLzToken = params.payInLzToken;
        lastNativeFee = msg.value;
        lastGuid = keccak256(abi.encodePacked(nonce, params.dstEid, params.receiver, params.message));

        return MessagingReceipt({guid: lastGuid, nonce: nonce, fee: MessagingFee(msg.value, 0)});
    }
}

contract LZPingPongTest is Test {
    LZPingPong internal l1;
    LZPingPong internal l2;

    MockEndpointV2Harness internal endpointL1;
    MockEndpointV2Harness internal endpointL2;

    uint32 internal constant L1_EID = 1;
    uint32 internal constant L2_EID = 2;

    function setUp() public {
        endpointL1 = new MockEndpointV2Harness();
        endpointL2 = new MockEndpointV2Harness();

        l1 = new LZPingPong(address(this), address(endpointL1), L2_EID);
        l2 = new LZPingPong(address(this), address(endpointL2), L1_EID);

        l1.setPeer(L2_EID, _addressToBytes32(address(l2)));
        l2.setPeer(L1_EID, _addressToBytes32(address(l1)));
    }

    function testL1ToL2Ping() public {
        MessagingReceipt memory receipt = l1.sendPing{value: 1}(1, keccak256("smoke"));

        assertEq(endpointL1.lastDstEid(), L2_EID);
        assertEq(endpointL1.lastReceiver(), _addressToBytes32(address(l2)));

        LZPingPong.Message memory sent = abi.decode(endpointL1.lastMessage(), (LZPingPong.Message));
        _deliverToL2(receipt.guid, sent);

        LZPingPong.Message memory l2Last = l2.getLastReceived();
        assertEq(uint256(l2Last.kind), uint256(LZPingPong.MessageKind.Ping));
        assertEq(l2Last.nonce, 1);
        assertEq(l2Last.tag, keccak256("smoke"));
        assertEq(l2.lastReceivedGuid(), receipt.guid);
        assertEq(l2.messageHashByGuid(receipt.guid), keccak256(abi.encode(sent)));
    }

    function testPingAckRoundTrip() public {
        MessagingReceipt memory pingReceipt = l1.sendPing{value: 1}(42, keccak256("ping-42"));
        LZPingPong.Message memory pingMessage = abi.decode(endpointL1.lastMessage(), (LZPingPong.Message));

        _deliverToL2(pingReceipt.guid, pingMessage);

        l2.ackLastReceived{value: 1}();

        LZPingPong.Message memory ackMessage = abi.decode(endpointL2.lastMessage(), (LZPingPong.Message));
        assertEq(uint256(ackMessage.kind), uint256(LZPingPong.MessageKind.Ack));
        assertEq(ackMessage.nonce, 42);
        assertEq(ackMessage.requestGuid, pingReceipt.guid);

        _deliverToL1(endpointL2.lastGuid(), ackMessage);

        LZPingPong.Message memory l1Last = l1.getLastReceived();
        assertEq(uint256(l1Last.kind), uint256(LZPingPong.MessageKind.Ack));
        assertEq(l1Last.requestGuid, pingReceipt.guid);
        assertEq(l1Last.nonce, 42);
    }

    function testQuote() public {
        endpointL1.setQuoteFee(7);

        LZPingPong.Message memory m = LZPingPong.Message({
            kind: LZPingPong.MessageKind.Ping,
            nonce: 1,
            sender: address(this),
            sentAt: uint64(block.timestamp),
            tag: bytes32(0),
            requestGuid: bytes32(0)
        });

        MessagingFee memory fee = l1.quoteMessage(m, "");
        assertEq(fee.nativeFee, 7);
        assertEq(fee.lzTokenFee, 0);
    }

    function _deliverToL2(bytes32 guid, LZPingPong.Message memory message) internal {
        Origin memory origin = Origin({srcEid: L1_EID, sender: _addressToBytes32(address(l1)), nonce: 1});
        vm.prank(address(endpointL2));
        l2.lzReceive(origin, guid, abi.encode(message), address(0), bytes(""));
    }

    function _deliverToL1(bytes32 guid, LZPingPong.Message memory message) internal {
        Origin memory origin = Origin({srcEid: L2_EID, sender: _addressToBytes32(address(l2)), nonce: 1});
        vm.prank(address(endpointL1));
        l1.lzReceive(origin, guid, abi.encode(message), address(0), bytes(""));
    }

    function _addressToBytes32(address value) internal pure returns (bytes32) {
        return bytes32(uint256(uint160(value)));
    }
}
