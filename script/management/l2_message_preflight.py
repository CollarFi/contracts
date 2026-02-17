#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import typer
from rich import print

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lz_harness.common import ROOT_DIR, cast_call, load_env, must, run  # noqa: E402

app = typer.Typer(add_completion=False)


def _resolve_env_path(env_profile: str, l2_env_file: Path) -> Path:
    profile = env_profile.strip().lower()
    if profile and l2_env_file == (ROOT_DIR / ".env.l2.testnet"):
        return ROOT_DIR / f".env.l2.{profile}"
    return l2_env_file


def _read_addr_from_output(path_value: str, key: str) -> str:
    path = Path(path_value)
    if not path.is_absolute():
        path = ROOT_DIR / path
    if not path.is_file():
        raise FileNotFoundError(f"deployment output not found: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    addrs = data.get("addrs", data)
    val = addrs.get(key)
    if not val:
        raise ValueError(f"missing {key} in deployment output: {path}")
    return str(val)


def _default_output_json(rpc_url: str, side: str) -> str:
    chain_id = run(["cast", "chain-id", "--rpc-url", rpc_url])
    return str(ROOT_DIR / "deployments" / chain_id / f"{side}.json")


def _resolve_receiver_addr(env: dict[str, str]) -> str:
    if env.get("L2_RECEIVER"):
        return str(env["L2_RECEIVER"])
    output_json = env.get("OUTPUT_JSON") or _default_output_json(must(env, "RPC_URL"), "l2")
    return _read_addr_from_output(output_json, "l2Receiver")


def _strip_units(s: str) -> str:
    return re.sub(r"\s*\[[^\]]+\]", "", s)


def _parse_pending_message(raw: str) -> dict[str, Any]:
    s = _strip_units(raw.strip())
    m = re.match(
        r"^\((\d+),\s*(\d+),\s*(0x[a-fA-F0-9]{40}),\s*(\d+),\s*(0x[a-fA-F0-9]{40}),\s*(\d+),\s*(0x[a-fA-F0-9]{64}),\s*(\d+),\s*(0x[a-fA-F0-9]{64}),\s*(\d+),\s*(0x[a-fA-F0-9]*)\)$",
        s,
    )
    if not m:
        raise ValueError(f"failed to parse pendingMessages tuple: {raw}")

    return {
        "action": int(m.group(1)),
        "loanId": int(m.group(2)),
        "asset": m.group(3),
        "amount": int(m.group(4)),
        "recipient": m.group(5),
        "subaccountId": int(m.group(6)),
        "socketMessageId": m.group(7),
        "secondaryAmount": int(m.group(8)),
        "quoteHash": m.group(9),
        "takerNonce": int(m.group(10)),
        "data": m.group(11),
    }


def _recent_message_guids(rpc_url: str, receiver: str, from_block: int, to_block: int) -> list[str]:
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


@app.command()
def main(
    l2_env_file: Path = typer.Argument(ROOT_DIR / ".env.l2.testnet"),
    env_profile: str = typer.Option("", "--env", help="Environment profile: testnet|mainnet"),
    receiver: str = typer.Option("", "--receiver", help="Override L2 receiver address"),
    guid: list[str] = typer.Option(None, "--guid", help="Specific message guid(s) to inspect"),
    lookback_blocks: int = typer.Option(50000, "--lookback-blocks", min=1),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    l2_env_file = _resolve_env_path(env_profile, l2_env_file)
    env = load_env(l2_env_file)

    rpc_url = must(env, "RPC_URL")
    receiver_addr = receiver or _resolve_receiver_addr(env)

    latest = int(run(["cast", "block-number", "--rpc-url", rpc_url]))
    from_block = max(0, latest - lookback_blocks)

    guids = guid if guid else _recent_message_guids(rpc_url, receiver_addr, from_block, latest)
    guids = list(dict.fromkeys([g.lower() for g in guids]))

    socket_addr = cast_call(rpc_url, receiver_addr, "socket()(address)")
    tsa_addr = cast_call(rpc_url, receiver_addr, "tsa()(address)")
    tsa_subaccount = int(_strip_units(cast_call(rpc_url, tsa_addr, "subAccount()(uint256)")))

    base_addrs_raw = cast_call(
        rpc_url,
        tsa_addr,
        "getBaseTSAAddresses()(address,address,address,address,address,address,address)",
        allow_fail=True,
    )
    wrapped_deposit_asset = "N/A"
    if base_addrs_raw != "N/A":
        lines = [ln.strip() for ln in base_addrs_raw.splitlines() if ln.strip()]
        if len(lines) >= 3:
            wrapped_deposit_asset = lines[2]

    results: list[dict[str, Any]] = []

    for g in guids:
        item: dict[str, Any] = {"guid": g, "ok": True, "issues": []}

        handled_raw = cast_call(rpc_url, receiver_addr, "handledMessages(bytes32)(bool)", g, allow_fail=True)
        item["handled"] = handled_raw

        pending_raw = cast_call(
            rpc_url,
            receiver_addr,
            "pendingMessages(bytes32)((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes))",
            g,
            allow_fail=True,
        )
        item["pendingRaw"] = pending_raw

        if pending_raw == "N/A":
            item["ok"] = False
            item["issues"].append("cannot read pendingMessages")
            results.append(item)
            continue

        try:
            msg = _parse_pending_message(pending_raw)
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
            bal = int(_strip_units(bal_raw))
            if bal < msg["amount"]:
                item["ok"] = False
                item["issues"].append(
                    f"insufficient receiver balance: have={bal} need={msg['amount']}"
                )

        item["wrappedDepositAsset"] = wrapped_deposit_asset
        if wrapped_deposit_asset != "N/A" and asset.lower() != wrapped_deposit_asset.lower():
            item["issues"].append(
                f"asset differs from TSA wrappedDepositAsset ({asset} != {wrapped_deposit_asset})"
            )

        if item["issues"]:
            item["ok"] = False

        results.append(item)

    out = {
        "receiver": receiver_addr,
        "socket": socket_addr,
        "tsa": tsa_addr,
        "tsaSubaccount": tsa_subaccount,
        "wrappedDepositAsset": wrapped_deposit_asset,
        "latestBlock": latest,
        "inspected": len(results),
        "results": results,
    }

    if json_out:
        print(json.dumps(out, indent=2))
        return

    print(f"[bold]L2 message preflight[/bold] receiver={receiver_addr} inspected={len(results)}")
    for r in results:
        icon = "✅" if r["ok"] else "❌"
        print(f"{icon} {r['guid']}")
        msg = r.get("message")
        if msg:
            print(
                f"   action={msg['action']} loanId={msg['loanId']} asset={msg['asset']} amount={msg['amount']} "
                f"subaccount={msg['subaccountId']}"
            )
        for issue in r.get("issues", []):
            print(f"   - {issue}")


if __name__ == "__main__":
    app()
