// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {MockERC20} from "./mocks/MockERC20.sol";
import {SocketBridgeAdapterL2Compat} from "../src/bridge/SocketBridgeAdapterL2Compat.sol";
import {SocketBridgeAdapterNew} from "../src/bridge/SocketBridgeAdapterNew.sol";
import {SocketBridgeAdapterOld} from "../src/bridge/SocketBridgeAdapterOld.sol";

contract MockSocketCore {
    uint32 public chainSlug;
    uint64 public globalMessageCount;
    mapping(address => mapping(uint32 => address)) public siblingPlug;

    constructor(uint32 chainSlug_) {
        chainSlug = chainSlug_;
    }

    function setGlobalMessageCount(uint64 n) external {
        globalMessageCount = n;
    }

    function setPlugConfig(address plug, uint32 siblingChainSlug, address siblingPlug_) external {
        siblingPlug[plug][siblingChainSlug] = siblingPlug_;
    }

    function getPlugConfig(address plugAddress_, uint32 siblingChainSlug_)
        external
        view
        returns (address, address, address, address, address)
    {
        return (siblingPlug[plugAddress_][siblingChainSlug_], address(0), address(0), address(0), address(0));
    }
}

contract MockConnector {
    address public socket__;
    uint32 public siblingChainSlug;

    constructor(address socket_, uint32 siblingChainSlug_) {
        socket__ = socket_;
        siblingChainSlug = siblingChainSlug_;
    }
}

contract MockSocketBridgeWithFees {
    IERC20 public immutable token;
    uint256 public totalBridged;

    constructor(IERC20 token_) {
        token = token_;
    }

    function getMinFees(address, uint256, uint256) external pure returns (uint256) {
        return 1;
    }

    function bridge(address, uint256 amount_, uint256, address, bytes calldata, bytes calldata) external payable {
        token.transferFrom(msg.sender, address(this), amount_);
        totalBridged += amount_;
    }
}

contract MockSocketVault {
    IERC20 public immutable token;
    uint256 public totalBridged;

    constructor(IERC20 token_) {
        token = token_;
    }

    function getMinFees(address, uint256) external pure returns (uint256) {
        return 1;
    }

    function __token() external view returns (address) {
        return address(token);
    }

    function depositToAppChain(address, uint256 amount_, uint256, address) external payable {
        token.transferFrom(msg.sender, address(this), amount_);
        totalBridged += amount_;
    }
}

contract MockSocketWithdrawController {
    IERC20 public immutable token;
    uint256 public totalBridged;

    constructor(IERC20 token_) {
        token = token_;
    }

    function getMinFees(address, uint256) external pure returns (uint256) {
        return 2;
    }

    function withdrawFromAppChain(address, uint256 amount_, uint256, address) external payable {
        token.transferFrom(msg.sender, address(this), amount_);
        totalBridged += amount_;
    }
}

contract SocketBridgeAdaptersTest is Test {
    function test_newAdapter_bridgePullsAndApprovesUnderlying() external {
        MockERC20 token = new MockERC20("T", "T", 18);
        MockSocketCore socket = new MockSocketCore(901);
        MockConnector connector = new MockConnector(address(socket), 40232);
        socket.setPlugConfig(address(connector), 40232, address(0xBEEF));
        socket.setGlobalMessageCount(42);

        MockSocketBridgeWithFees bridge = new MockSocketBridgeWithFees(IERC20(address(token)));
        SocketBridgeAdapterNew adapter =
            new SocketBridgeAdapterNew(address(token), address(bridge), address(connector), 100_000, 161, "", "");

        token.mint(address(this), 10 ether);
        token.approve(address(adapter), type(uint256).max);

        adapter.bridge(address(0xCAFE), 3 ether);

        assertEq(token.balanceOf(address(bridge)), 3 ether);
        assertEq(token.balanceOf(address(adapter)), 0);

        bytes32 expectedId = bytes32((uint256(901) << 224) | (uint256(uint160(address(0xBEEF))) << 64) | uint256(42));
        assertEq(adapter.messageId(), expectedId);
    }

    function test_oldAdapter_bridgePullsAndApprovesUnderlying() external {
        MockERC20 token = new MockERC20("T", "T", 18);
        MockSocketCore socket = new MockSocketCore(901);
        MockConnector connector = new MockConnector(address(socket), 40232);
        socket.setPlugConfig(address(connector), 40232, address(0xD00D));
        socket.setGlobalMessageCount(77);

        MockSocketVault vault = new MockSocketVault(IERC20(address(token)));
        SocketBridgeAdapterOld adapter =
            new SocketBridgeAdapterOld(address(token), address(vault), address(connector), 100_000);

        token.mint(address(this), 10 ether);
        token.approve(address(adapter), type(uint256).max);

        adapter.bridge(address(0xCAFE), 4 ether);

        assertEq(token.balanceOf(address(vault)), 4 ether);
        assertEq(token.balanceOf(address(adapter)), 0);

        bytes32 expectedId = bytes32((uint256(901) << 224) | (uint256(uint160(address(0xD00D))) << 64) | uint256(77));
        assertEq(adapter.messageId(), expectedId);
    }

    function test_l2CompatAdapter_bridgePullsAndApprovesUnderlying() external {
        MockERC20 token = new MockERC20("T", "T", 18);
        MockSocketCore socket = new MockSocketCore(901);
        MockConnector connector = new MockConnector(address(socket), 40232);
        socket.setPlugConfig(address(connector), 40232, address(0xC0DE));
        socket.setGlobalMessageCount(99);

        MockSocketWithdrawController controller = new MockSocketWithdrawController(IERC20(address(token)));
        SocketBridgeAdapterL2Compat adapter =
            new SocketBridgeAdapterL2Compat(address(token), address(controller), address(connector), 100_000);

        token.mint(address(this), 10 ether);
        token.approve(address(adapter), type(uint256).max);

        assertEq(adapter.estimateFee(), 2);

        adapter.bridge(address(0xCAFE), 5 ether);

        assertEq(token.balanceOf(address(controller)), 5 ether);
        assertEq(token.balanceOf(address(adapter)), 0);

        bytes32 expectedId = bytes32((uint256(901) << 224) | (uint256(uint160(address(0xC0DE))) << 64) | uint256(99));
        assertEq(adapter.messageId(), expectedId);
    }
}
