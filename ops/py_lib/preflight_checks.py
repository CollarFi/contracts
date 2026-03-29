from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from lz_harness.common import ROOT_DIR, address_to_peer_bytes32, cast_call, load_env, must, run
from py_lib.deployments import resolve_addr
from py_lib.envs import resolve_l1_l2_env_paths, resolve_l2_env_path
from py_lib.lz import decode_send_executor_config, encode_lz_receive_option, is_empty_hex, is_zero_address, parse_uint


def strip_units(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    return raw.split()[0]


def addr_to_bytes32(addr: str) -> str:
    normalized = addr.lower().replace("0x", "")
    return "0x" + ("0" * 24) + normalized


def recipient_check(
    l1_env_file: Path = ROOT_DIR / ".env.l1.testnet",
    l2_env_file: Path = ROOT_DIR / ".env.l2.testnet",
    *,
    env_profile: str = "",
) -> dict[str, Any]:
    l1_env_file, l2_env_file = resolve_l1_l2_env_paths(env_profile, l1_env_file, l2_env_file)
    l1 = load_env(l1_env_file)
    l2 = load_env(l2_env_file)

    must(l1, "RPC_URL")
    must(l2, "RPC_URL")

    vault = resolve_addr(l1, "L1_VAULT", "l1Vault", "l1")
    receiver = resolve_addr(l2, "L2_RECEIVER", "l2Receiver", "l2")
    l1_recipient = strip_units(cast_call(l1["RPC_URL"], vault, "l2Recipient()(address)", allow_fail=True))

    return {
        "ok": l1_recipient.lower() == receiver.lower(),
        "vault": vault,
        "l1Recipient": l1_recipient,
        "l2Receiver": receiver,
    }


def vault_recipient_check(
    l1_env_file: Path = ROOT_DIR / ".env.l1.testnet",
    l2_env_file: Path = ROOT_DIR / ".env.l2.testnet",
    *,
    env_profile: str = "",
) -> dict[str, Any]:
    l1_env_file, l2_env_file = resolve_l1_l2_env_paths(env_profile, l1_env_file, l2_env_file)
    l1 = load_env(l1_env_file)
    l2 = load_env(l2_env_file)

    must(l1, "RPC_URL")
    must(l2, "RPC_URL")

    vault = resolve_addr(l1, "L1_VAULT", "l1Vault", "l1")
    receiver = resolve_addr(l2, "L2_RECEIVER", "l2Receiver", "l2")
    actual = strip_units(cast_call(l2["RPC_URL"], receiver, "vaultRecipient()(address)", allow_fail=True))

    return {
        "ok": actual.lower() == vault.lower(),
        "vault": vault,
        "receiver": receiver,
        "actualVaultRecipient": actual,
        "expectedVaultRecipient": vault,
    }


def peer_check(
    l1_env_file: Path = ROOT_DIR / ".env.l1.testnet",
    l2_env_file: Path = ROOT_DIR / ".env.l2.testnet",
    *,
    env_profile: str = "",
) -> dict[str, Any]:
    l1_env_file, l2_env_file = resolve_l1_l2_env_paths(env_profile, l1_env_file, l2_env_file)
    l1 = load_env(l1_env_file)
    l2 = load_env(l2_env_file)

    must(l1, "RPC_URL")
    must(l2, "RPC_URL")

    l1_messenger = resolve_addr(l1, "L1_MESSENGER", "l1Messenger", "l1")
    l2_receiver = resolve_addr(l2, "L2_RECEIVER", "l2Receiver", "l2")

    # Route EIDs should come from the live LayerZero endpoints. Env EIDs are
    # only a fallback for deployments where the endpoint is unavailable.
    l1_chain_eid = 0
    l2_chain_eid = 0
    if l1.get("LZ_ENDPOINT"):
        l1_eid_raw = cast_call(l1["RPC_URL"], l1["LZ_ENDPOINT"], "eid()(uint32)", allow_fail=True)
        if l1_eid_raw != "N/A":
            l1_chain_eid = int(strip_units(l1_eid_raw))
    if l2.get("LZ_ENDPOINT"):
        l2_eid_raw = cast_call(l2["RPC_URL"], l2["LZ_ENDPOINT"], "eid()(uint32)", allow_fail=True)
        if l2_eid_raw != "N/A":
            l2_chain_eid = int(strip_units(l2_eid_raw))

    l2_eid = l2_chain_eid or int((l1.get("L2_EID") or l1.get("REMOTE_EID") or "0").strip())
    l1_eid = l1_chain_eid or int((l2.get("L1_EID") or "0").strip())

    l1_peer_actual = cast_call(l1["RPC_URL"], l1_messenger, "peers(uint32)(bytes32)", str(l2_eid), allow_fail=True)
    l2_peer_actual = cast_call(l2["RPC_URL"], l2_receiver, "peers(uint32)(bytes32)", str(l1_eid), allow_fail=True)

    l1_peer_expected = addr_to_bytes32(l2_receiver)
    l2_peer_expected = addr_to_bytes32(l1_messenger)

    l1_ok = l1_peer_actual != "N/A" and l1_peer_actual.lower() == l1_peer_expected.lower()
    l2_ok = l2_peer_actual != "N/A" and l2_peer_actual.lower() == l2_peer_expected.lower()

    issues: list[str] = []
    if not l1_ok:
        issues.append("L1 messenger peer mismatch")
    if not l2_ok:
        issues.append("L2 receiver peer mismatch")

    return {
        "ok": l1_ok and l2_ok,
        "l1Messenger": l1_messenger,
        "l2Receiver": l2_receiver,
        "l1ToL2Eid": l2_eid,
        "l2ToL1Eid": l1_eid,
        "l1PeerActual": l1_peer_actual,
        "l1PeerExpected": l1_peer_expected,
        "l2PeerActual": l2_peer_actual,
        "l2PeerExpected": l2_peer_expected,
        "issues": issues,
    }


def asset_mapping_check(
    l1_env_file: Path = ROOT_DIR / ".env.l1.testnet",
    l2_env_file: Path = ROOT_DIR / ".env.l2.testnet",
    *,
    env_profile: str = "",
    l1_asset: str = "",
) -> dict[str, Any]:
    l1_env_file, l2_env_file = resolve_l1_l2_env_paths(env_profile, l1_env_file, l2_env_file)
    l1 = load_env(l1_env_file)
    l2 = load_env(l2_env_file)

    for env in (l1, l2):
        must(env, "RPC_URL")

    vault = resolve_addr(l1, "L1_VAULT", "l1Vault", "l1")
    receiver = resolve_addr(l2, "L2_RECEIVER", "l2Receiver", "l2")

    asset = l1_asset or l1.get("WETH_ASSET", "")
    if not asset:
        raise ValueError("missing L1 asset: pass --l1-asset or set WETH_ASSET in L1 env")

    mapped_l2_asset_raw = cast_call(l1["RPC_URL"], vault, "l2MessageAsset(address)(address)", asset, allow_fail=True)
    mapped_l2_asset = strip_units(mapped_l2_asset_raw) if mapped_l2_asset_raw != "N/A" else "N/A"
    tsa = cast_call(l2["RPC_URL"], receiver, "tsa()(address)", allow_fail=True)

    wrapped = "N/A"
    wrapped_underlying = "N/A"
    if tsa != "N/A":
        base = cast_call(
            l2["RPC_URL"],
            tsa,
            "getBaseTSAAddresses()(address,address,address,address,address,address,address)",
            allow_fail=True,
        )
        if base != "N/A":
            lines = [line.strip() for line in base.splitlines() if line.strip()]
            if len(lines) >= 3:
                wrapped = lines[2]
                wrapped_underlying_raw = cast_call(
                    l2["RPC_URL"], wrapped, "wrappedAsset()(address)", allow_fail=True
                )
                if wrapped_underlying_raw != "N/A":
                    wrapped_underlying = strip_units(wrapped_underlying_raw)

    ok = (
        mapped_l2_asset != "N/A"
        and wrapped_underlying != "N/A"
        and mapped_l2_asset.lower() == wrapped_underlying.lower()
    )
    return {
        "vault": vault,
        "receiver": receiver,
        "tsa": tsa,
        "l1Asset": asset,
        "mappedL2MessageAsset": mapped_l2_asset,
        "tsaWrappedDepositAsset": wrapped,
        "tsaWrappedUnderlyingAsset": wrapped_underlying,
        "ok": ok,
    }


def parse_pending_message(raw: str) -> dict[str, Any]:
    stripped = re.sub(r"\s*\[[^\]]+\]", "", raw.strip())
    tuple_match = re.match(
        r"^\((\d+),\s*(\d+),\s*(0x[a-fA-F0-9]{40}),\s*(\d+),\s*(0x[a-fA-F0-9]{40}),\s*(\d+),\s*(0x[a-fA-F0-9]{64}),\s*(\d+),\s*(0x[a-fA-F0-9]{64}),\s*(\d+),\s*(0x[a-fA-F0-9]*)\)$",
        stripped,
    )
    if tuple_match:
        return {
            "action": int(tuple_match.group(1)),
            "loanId": int(tuple_match.group(2)),
            "asset": tuple_match.group(3),
            "amount": int(tuple_match.group(4)),
            "recipient": tuple_match.group(5),
            "subaccountId": int(tuple_match.group(6)),
            "socketMessageId": tuple_match.group(7),
            "secondaryAmount": int(tuple_match.group(8)),
            "quoteHash": tuple_match.group(9),
            "takerNonce": int(tuple_match.group(10)),
            "data": tuple_match.group(11),
        }

    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    if len(lines) == 11:
        return {
            "action": int(lines[0]),
            "loanId": int(lines[1]),
            "asset": lines[2],
            "amount": int(lines[3]),
            "recipient": lines[4],
            "subaccountId": int(lines[5]),
            "socketMessageId": lines[6],
            "secondaryAmount": int(lines[7]),
            "quoteHash": lines[8],
            "takerNonce": int(lines[9]),
            "data": lines[10],
        }

    raise ValueError(f"failed to parse pendingMessages tuple: {raw}")


def recent_message_guids(rpc_url: str, receiver: str, from_block: int, to_block: int) -> list[str]:
    out = run(
        [
            "cast",
            "logs",
            "MessageReceived(bytes32,uint8,uint256)",
            "--address",
            receiver,
            "--from-block",
            str(from_block),
            "--to-block",
            str(to_block),
            "--rpc-url",
            rpc_url,
            "--json",
        ]
    )
    logs = json.loads(out)
    guids: list[str] = []
    for log in logs:
        topics = log.get("topics", [])
        if len(topics) > 1:
            guids.append(topics[1])
    return guids


def l2_message_preflight(
    l2_env_file: Path = ROOT_DIR / ".env.l2.testnet",
    *,
    env_profile: str = "",
    receiver: str = "",
    guid: list[str] | None = None,
    lookback_blocks: int = 50000,
) -> dict[str, Any]:
    l2_env_file = resolve_l2_env_path(env_profile, l2_env_file)
    env = load_env(l2_env_file)

    rpc_url = must(env, "RPC_URL")
    receiver_addr = receiver or resolve_addr(env, "L2_RECEIVER", "l2Receiver", "l2")

    latest = int(run(["cast", "block-number", "--rpc-url", rpc_url]))
    from_block = max(0, latest - lookback_blocks)

    guids = guid if guid else recent_message_guids(rpc_url, receiver_addr, from_block, latest)
    guids = list(dict.fromkeys([item.lower() for item in guids]))

    socket_addr = cast_call(rpc_url, receiver_addr, "socket()(address)")
    tsa_addr = cast_call(rpc_url, receiver_addr, "tsa()(address)")
    tsa_subaccount = int(strip_units(cast_call(rpc_url, tsa_addr, "subAccount()(uint256)")))

    base_addrs_raw = cast_call(
        rpc_url,
        tsa_addr,
        "getBaseTSAAddresses()(address,address,address,address,address,address,address)",
        allow_fail=True,
    )
    wrapped_deposit_asset = "N/A"
    wrapped_underlying_asset = "N/A"
    if base_addrs_raw != "N/A":
        lines = [line.strip() for line in base_addrs_raw.splitlines() if line.strip()]
        if len(lines) >= 3:
            wrapped_deposit_asset = lines[2]
            underlying_raw = cast_call(rpc_url, wrapped_deposit_asset, "wrappedAsset()(address)", allow_fail=True)
            if underlying_raw != "N/A":
                wrapped_underlying_asset = strip_units(underlying_raw).strip()

    results: list[dict[str, Any]] = []

    for item_guid in guids:
        item: dict[str, Any] = {"guid": item_guid, "ok": True, "issues": []}

        handled_raw = cast_call(rpc_url, receiver_addr, "handledMessages(bytes32)(bool)", item_guid, allow_fail=True)
        item["handled"] = handled_raw

        pending_raw = cast_call(
            rpc_url,
            receiver_addr,
            "pendingMessages(bytes32)(uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes)",
            item_guid,
            allow_fail=True,
        )
        item["pendingRaw"] = pending_raw

        if pending_raw == "N/A":
            item["ok"] = False
            item["issues"].append("cannot read pendingMessages")
            results.append(item)
            continue

        try:
            msg = parse_pending_message(pending_raw)
        except Exception as exc:
            item["ok"] = False
            item["issues"].append(str(exc))
            results.append(item)
            continue

        item["message"] = msg

        if msg["loanId"] == 0:
            item["ok"] = False
            item["issues"].append("pending message missing (loanId==0)")

        if msg["subaccountId"] != tsa_subaccount:
            item["ok"] = False
            item["issues"].append(
                f"subaccount mismatch: message={msg['subaccountId']} tsa={tsa_subaccount}"
            )

        if msg["socketMessageId"] != "0x" + "0" * 64 and socket_addr != "0x0000000000000000000000000000000000000000":
            socket_executed = cast_call(
                rpc_url,
                socket_addr,
                "messageExecuted(bytes32)(bool)",
                msg["socketMessageId"],
                allow_fail=True,
            )
            item["socketExecuted"] = socket_executed
            if socket_executed.strip().lower() != "true":
                item["ok"] = False
                item["issues"].append("socket message not finalized")

        asset = msg["asset"]
        asset_code = run(["cast", "code", asset, "--rpc-url", rpc_url])
        item["assetCodeEmpty"] = asset_code in {"0x", "0x0"}
        if item["assetCodeEmpty"]:
            item["ok"] = False
            item["issues"].append("asset has no bytecode on L2")

        bal_raw = cast_call(rpc_url, asset, "balanceOf(address)(uint256)", receiver_addr, allow_fail=True)
        item["receiverAssetBalance"] = bal_raw
        if bal_raw == "N/A":
            item["ok"] = False
            item["issues"].append("asset.balanceOf(receiver) reverted")
        else:
            balance = int(strip_units(bal_raw))
            if balance < msg["amount"]:
                item["ok"] = False
                item["issues"].append(
                    f"insufficient receiver balance: have={balance} need={msg['amount']}"
                )

        item["wrappedDepositAsset"] = wrapped_deposit_asset
        item["wrappedUnderlyingAsset"] = wrapped_underlying_asset
        if wrapped_underlying_asset != "N/A" and asset.lower() != wrapped_underlying_asset.lower():
            item["issues"].append(
                f"asset differs from wrappedDepositAsset.wrappedAsset ({asset} != {wrapped_underlying_asset})"
            )

        if item["issues"]:
            item["ok"] = False

        results.append(item)

    return {
        "receiver": receiver_addr,
        "socket": socket_addr,
        "tsa": tsa_addr,
        "tsaSubaccount": tsa_subaccount,
        "wrappedDepositAsset": wrapped_deposit_asset,
        "wrappedUnderlyingAsset": wrapped_underlying_asset,
        "latestBlock": latest,
        "inspected": len(results),
        "results": results,
    }


def snapshot_uln_side(
    *,
    label: str,
    rpc_url: str,
    endpoint: str,
    oapp: str,
    remote_eid: str,
    source_eid: str,
    expected_peer_b32: str,
    expected_remote_eid: str,
    expected_default_options: str | None,
) -> dict[str, Any]:
    peer = cast_call(rpc_url, oapp, "peers(uint32)(bytes32)", remote_eid, allow_fail=True)
    delegate = cast_call(rpc_url, endpoint, "delegates(address)(address)", oapp, allow_fail=True)
    configured_remote_eid = cast_call(rpc_url, oapp, "remoteEid()(uint32)", allow_fail=True)
    default_options = cast_call(rpc_url, oapp, "defaultOptions()(bytes)", allow_fail=True)

    send_lib = cast_call(rpc_url, endpoint, "getSendLibrary(address,uint32)(address)", oapp, remote_eid, allow_fail=True)
    recv_lib_raw = cast_call(
        rpc_url,
        endpoint,
        "getReceiveLibrary(address,uint32)(address,bool)",
        oapp,
        source_eid,
        allow_fail=True,
    )
    recv_lib = recv_lib_raw.splitlines()[0] if recv_lib_raw != "N/A" else "N/A"

    send_cfg_1 = (
        cast_call(
            rpc_url,
            endpoint,
            "getConfig(address,address,uint32,uint32)(bytes)",
            oapp,
            send_lib,
            remote_eid,
            "1",
            allow_fail=True,
        )
        if send_lib != "N/A"
        else "N/A"
    )
    send_cfg_2 = (
        cast_call(
            rpc_url,
            endpoint,
            "getConfig(address,address,uint32,uint32)(bytes)",
            oapp,
            send_lib,
            remote_eid,
            "2",
            allow_fail=True,
        )
        if send_lib != "N/A"
        else "N/A"
    )
    recv_cfg_2 = (
        cast_call(
            rpc_url,
            endpoint,
            "getConfig(address,address,uint32,uint32)(bytes)",
            oapp,
            recv_lib,
            source_eid,
            "2",
            allow_fail=True,
        )
        if recv_lib != "N/A"
        else "N/A"
    )

    send_max_message_size, send_executor = decode_send_executor_config(send_cfg_1 if send_cfg_1 != "N/A" else "")
    receive_timeout = cast_call(
        rpc_url,
        endpoint,
        "receiveLibraryTimeout(address,uint32)(address,uint256)",
        oapp,
        source_eid,
        allow_fail=True,
    )

    checks: list[dict[str, Any]] = [
        {
            "name": "peer wired",
            "ok": peer.lower() == expected_peer_b32.lower(),
            "actual": peer,
            "expected": expected_peer_b32,
            "hint": "Run ops/preflight/wire_lz_peers.py --broadcast if mismatch.",
        },
        {
            "name": "remoteEid set",
            "ok": parse_uint(configured_remote_eid) == int(expected_remote_eid),
            "actual": configured_remote_eid,
            "expected": str(expected_remote_eid),
            "hint": "Set setRemoteEid(...) on the OApp contract.",
        },
        {
            "name": "defaultOptions set",
            "ok": default_options not in {"N/A"} and not is_empty_hex(default_options),
            "actual": default_options,
            "hint": "Set setDefaultOptions(...) on the OApp contract.",
        },
    ]
    if expected_default_options:
        checks.append(
            {
                "name": "defaultOptions matches env",
                "ok": default_options.lower().strip() == expected_default_options.lower().strip(),
                "actual": default_options,
                "expected": expected_default_options,
                "hint": "Re-apply setDefaultOptions from env LZ_RECEIVE_GAS/LZ_RECEIVE_VALUE.",
            }
        )

    checks.extend(
        [
            {
                "name": "delegate set",
                "ok": delegate != "N/A" and not is_zero_address(delegate),
                "actual": delegate,
                "hint": "Set OApp delegate via endpoint.setDelegate(...) if zero.",
            },
            {
                "name": "send library set",
                "ok": send_lib != "N/A" and not is_zero_address(send_lib),
                "actual": send_lib,
                "hint": "Missing send lib route config on endpoint.",
            },
            {
                "name": "receive library set",
                "ok": recv_lib != "N/A" and not is_zero_address(recv_lib),
                "actual": recv_lib,
                "hint": "Missing receive lib route config on endpoint.",
            },
            {
                "name": "send Executor config (type 1) present",
                "ok": send_cfg_1 not in {"N/A"} and not is_empty_hex(send_cfg_1),
                "actual": send_cfg_1,
                "hint": "Likely missing executor config for send path.",
            },
            {
                "name": "send executor address set (non-zero)",
                "ok": bool(send_executor) and not is_zero_address(send_executor),
                "actual": send_executor or "N/A",
                "hint": "Send executor is empty; set endpoint send executor config for this OApp/eid route.",
            },
            {
                "name": "send maxMessageSize > 0",
                "ok": (send_max_message_size or 0) > 0,
                "actual": str(send_max_message_size) if send_max_message_size is not None else "N/A",
                "hint": "Set a non-zero maxMessageSize in send executor config.",
            },
            {
                "name": "send ULN config (type 2) present",
                "ok": send_cfg_2 not in {"N/A"} and not is_empty_hex(send_cfg_2),
                "actual": send_cfg_2,
                "hint": "Likely missing ULN config (DVN/confirmations) for send path.",
            },
            {
                "name": "receive ULN config (type 2) present",
                "ok": recv_cfg_2 not in {"N/A"} and not is_empty_hex(recv_cfg_2),
                "actual": recv_cfg_2,
                "hint": "Likely missing ULN config on receive path.",
            },
        ]
    )

    ok = all(check["ok"] for check in checks)
    return {
        "label": label,
        "oapp": oapp,
        "endpoint": endpoint,
        "remoteEid": remote_eid,
        "sourceEid": source_eid,
        "peer": peer,
        "expectedPeer": expected_peer_b32,
        "delegate": delegate,
        "configuredRemoteEid": configured_remote_eid,
        "defaultOptions": default_options,
        "sendLibrary": send_lib,
        "receiveLibrary": recv_lib,
        "sendConfigType1": send_cfg_1,
        "sendConfigType2": send_cfg_2,
        "sendExecutor": send_executor,
        "sendMaxMessageSize": send_max_message_size,
        "receiveConfigType2": recv_cfg_2,
        "receiveLibraryTimeout": receive_timeout,
        "checks": checks,
        "ok": ok,
    }


def uln_route_check(
    l1_env_file: Path = ROOT_DIR / ".env.l1.testnet",
    l2_env_file: Path = ROOT_DIR / ".env.l2.testnet",
    *,
    env_profile: str = "",
) -> dict[str, Any]:
    l1_env_file, l2_env_file = resolve_l1_l2_env_paths(env_profile, l1_env_file, l2_env_file)

    l1 = load_env(l1_env_file)
    l2 = load_env(l2_env_file)

    for env in (l1, l2):
        for key in ("RPC_URL", "LZ_ENDPOINT"):
            must(env, key)

    l1_chain_eid = parse_uint(cast_call(l1["RPC_URL"], l1["LZ_ENDPOINT"], "eid()(uint32)"))
    l2_chain_eid = parse_uint(cast_call(l2["RPC_URL"], l2["LZ_ENDPOINT"], "eid()(uint32)"))
    if l1_chain_eid is None:
        raise ValueError("failed to resolve L1 endpoint eid()")
    if l2_chain_eid is None:
        raise ValueError("failed to resolve L2 endpoint eid()")

    l1_to_l2_eid = str(l2_chain_eid)
    l2_to_l1_eid = str(l1_chain_eid)

    l1_messenger = resolve_addr(l1, "L1_MESSENGER", "l1Messenger", "l1")
    l2_receiver = resolve_addr(l2, "L2_RECEIVER", "l2Receiver", "l2")

    l1_expected_peer = address_to_peer_bytes32(l2_receiver)
    l2_expected_peer = address_to_peer_bytes32(l1_messenger)

    l1_expected_options = None
    if l1.get("LZ_RECEIVE_GAS"):
        l1_expected_options = encode_lz_receive_option(
            int(l1["LZ_RECEIVE_GAS"]),
            int(l1.get("LZ_RECEIVE_VALUE", "0") or 0),
        )

    l2_expected_options = None
    if l2.get("LZ_RECEIVE_GAS"):
        l2_expected_options = encode_lz_receive_option(
            int(l2["LZ_RECEIVE_GAS"]),
            int(l2.get("LZ_RECEIVE_VALUE", "0") or 0),
        )

    l1_side = snapshot_uln_side(
        label="L1 messenger (send->L2, recv<-L2)",
        rpc_url=l1["RPC_URL"],
        endpoint=l1["LZ_ENDPOINT"],
        oapp=l1_messenger,
        remote_eid=l1_to_l2_eid,
        source_eid=l2_to_l1_eid,
        expected_peer_b32=l1_expected_peer,
        expected_remote_eid=l1_to_l2_eid,
        expected_default_options=l1_expected_options,
    )
    l2_side = snapshot_uln_side(
        label="L2 receiver (send->L1, recv<-L1)",
        rpc_url=l2["RPC_URL"],
        endpoint=l2["LZ_ENDPOINT"],
        oapp=l2_receiver,
        remote_eid=l2_to_l1_eid,
        source_eid=l1_to_l2_eid,
        expected_peer_b32=l2_expected_peer,
        expected_remote_eid=l2_to_l1_eid,
        expected_default_options=l2_expected_options,
    )

    return {
        "env": env_profile.strip().lower() or "custom",
        "l1Env": str(l1_env_file),
        "l2Env": str(l2_env_file),
        "l1Messenger": l1_messenger,
        "l2Receiver": l2_receiver,
        "l1EndpointEid": str(l1_chain_eid),
        "l2EndpointEid": str(l2_chain_eid),
        "l1ToL2Eid": l1_to_l2_eid,
        "l2ToL1Eid": l2_to_l1_eid,
        "sides": [l1_side, l2_side],
        "ok": l1_side["ok"] and l2_side["ok"],
    }
