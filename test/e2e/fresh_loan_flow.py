#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parents[2]
THIS_DIR = Path(__file__).resolve().parent
import sys
sys.path.insert(0, str(THIS_DIR))
from common import ensure_liquidity_vault_role as _ensure_liquidity_vault_role
from common import seed_l1_liquidity_vault as _seed_l1_liquidity_vault
from defaults import L1_ANVIL_PORT, L1_ARTIFACT_JSON, L1_COLLATERAL_ASSET, L2_ANVIL_PORT, L2_ARTIFACT_JSON
from loan_flow_helpers import resolve_mandate_ttl

ANVIL_PK0 = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ANVIL_ADDR0 = "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"
BORROWER_PK = "0x59c6995e998f97a5a0044966f0945382d77ad9e6f3c6f7f8b8d7a0f4f7f9d6f1"
L2_WETH_MOCK_CONTROLLER = "0xdcABb6d7E88396498FFF4CD987F60e354BF2a44b"
SOCKET_MOCK_CACHE: dict[str, str] = {}
BRIDGE_MOCK_CACHE: dict[str, str] = {}

app = typer.Typer(add_completion=False)


def _describe_step(step_name: str) -> str:
    descriptions = {
        "grant_l2_keeper": "Grant keeper role on L2 receiver",
        "create_deposit_with_permit": "Create L1 deposit+mandate via Permit2",
        "relay_l1_to_l2_exact": "Relay exact LayerZero packet L1 → L2",
        "simulate_socket_finalized": "Mark socket transfer as finalized on fork",
        "fund_l2_receiver_for_deposit": "Ensure L2 receiver has bridged asset balance",
        "grant_tsa_signer": "Grant receiver and keeper signer roles on TSA",
        "l2_keeper_handle_deposit": "Run L2 keeper once to process deposit and send ACK",
        "relay_l2_to_l1_exact": "Relay exact LayerZero packet L2 → L1",
        "verify_expected_state": "Verify expected post-run protocol state",
    }
    return descriptions.get(step_name, step_name)


def _print_human_summary(out: dict) -> None:
    print("\n=== collar.fi fresh loan flow e2e ===")
    for s in out.get("steps", []):
        title = _describe_step(s.get("step", ""))
        if s.get("ok"):
            print(f"✅ {title}")
        else:
            print(f"❌ {title}")
            if s.get("error"):
                print(f"   ↳ {s['error']}")

    verify = next((s.get("result") for s in out.get("steps", []) if s.get("step") == "verify_expected_state" and s.get("ok")), None)
    if isinstance(verify, dict):
        print("\nVerification snapshot")
        print(f"- Loan ID: {verify.get('loanId')}")
        print(f"- L1→L2 guid handled on L2: {verify.get('l2Handled')}")
        print(f"- L2→L1 ACK action: {verify.get('ackAction')} (loanId={verify.get('ackLoanId')})")

    print("\nResult: SUCCESS" if out.get("ok") else "\nResult: FAILED")


def run(cmd: list[str], env: dict | None = None) -> str:
    p = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    if p.returncode != 0:
        raise RuntimeError(f"command failed ({p.returncode}): {' '.join(cmd)}\n{p.stderr.strip()}")
    return p.stdout.strip()


def extract_tx_hash(raw: str) -> str:
    s = raw.strip()
    if re.fullmatch(r"0x[a-fA-F0-9]{64}", s):
        return s
    m = re.search(r"transactionHash\s+(0x[a-fA-F0-9]{64})", s)
    if m:
        return m.group(1)
    hashes = re.findall(r"0x[a-fA-F0-9]{64}", s)
    if hashes:
        return hashes[-1]
    raise RuntimeError(f"could not extract tx hash: {raw[:240]}")


def cast_call(rpc: str, to: str, sig: str, *args: str) -> str:
    return run(["cast", "call", to, sig, *args, "--rpc-url", rpc])


def cast_send(rpc: str, to: str, sig: str, *args: str, value: str | None = None, private_key: str = ANVIL_PK0) -> str:
    cmd = ["cast", "send", to, sig, *args, "--rpc-url", rpc, "--private-key", private_key]
    if value:
        cmd += ["--value", value]
    return extract_tx_hash(run(cmd))


def _has_code(rpc: str, addr: str) -> bool:
    return run(["cast", "code", addr, "--rpc-url", rpc]).strip().lower() != "0x"


def sign_hash_no_prefix(digest_hex: str, private_key: str) -> str:
    return run(["cast", "wallet", "sign", "--no-hash", "--private-key", private_key, digest_hex])


def _keccak_text(text: str) -> str:
    return run(["cast", "keccak", text]).splitlines()[0].strip()


def _keccak_hex(hex_data: str) -> str:
    return run(["cast", "keccak", hex_data]).splitlines()[0].strip()


def _abi_encode(sig: str, *args: str) -> str:
    return run(["cast", "abi-encode", sig, *args]).splitlines()[0].strip()


def sign_permit2_single(chain_id: int, permit2: str, token: str, amount: int, expiration: int, nonce: int, spender: str, sig_deadline: int, private_key: str) -> str:
    domain_typehash = _keccak_text("EIP712Domain(string name,uint256 chainId,address verifyingContract)")
    name_hash = _keccak_text("Permit2")
    domain_sep = _keccak_hex(_abi_encode("f(bytes32,bytes32,uint256,address)", domain_typehash, name_hash, str(chain_id), permit2))

    details_typehash = _keccak_text("PermitDetails(address token,uint160 amount,uint48 expiration,uint48 nonce)")
    details_hash = _keccak_hex(_abi_encode("f(bytes32,address,uint160,uint48,uint48)", details_typehash, token, str(amount), str(expiration), str(nonce)))

    single_typehash = _keccak_text("PermitSingle(PermitDetails details,address spender,uint256 sigDeadline)PermitDetails(address token,uint160 amount,uint48 expiration,uint48 nonce)")
    struct_hash = _keccak_hex(_abi_encode("f(bytes32,bytes32,address,uint256)", single_typehash, details_hash, spender, str(sig_deadline)))
    digest = _keccak_hex("0x1901" + domain_sep[2:] + struct_hash[2:])
    return sign_hash_no_prefix(digest, private_key)


def wallet_address(private_key: str) -> str:
    return run(["cast", "wallet", "address", "--private-key", private_key]).strip()


def _force_set_erc20_balance_on_anvil(rpc: str, token: str, who: str, target_amount: int) -> bool:
    # last-resort helper for local fork tests when upstream faucet is unavailable
    for slot in range(0, 16):
        key = _keccak_hex(_abi_encode("f(address,uint256)", who, str(slot)))
        run([
            "cast", "rpc", "anvil_setStorageAt", token, key, run(["cast", "to-bytes32", str(target_amount)]), "--rpc-url", rpc,
        ])
        bal = int(cast_call(rpc, token, "balanceOf(address)(uint256)", who).split()[0])
        if bal >= target_amount:
            return True
    return False


def ensure_token_balance_via_faucet(rpc: str, faucet: str, token: str, to: str, amount: int) -> str:
    bal_before = int(cast_call(rpc, token, "balanceOf(address)(uint256)", to).split()[0])
    if bal_before >= amount:
        return "already-funded"

    faucet_code = run(["cast", "code", faucet, "--rpc-url", rpc]).strip().lower()
    if faucet_code != "0x":
        try:
            cast_send(rpc, faucet, "getTokens(address,address[])", to, f"[{token}]")
            bal_after = int(cast_call(rpc, token, "balanceOf(address)(uint256)", to).split()[0])
            if bal_after >= amount:
                return "funded-via-faucet"
        except Exception:
            pass

    if _force_set_erc20_balance_on_anvil(rpc, token, to, amount):
        return "funded-via-anvil-storage"
    raise RuntimeError("faucet funding failed")


def _iter_calls(node: dict):
    yield node
    for c in node.get("calls", []) or []:
        yield from _iter_calls(c)


def _extract_send_packet_from_tx(src_rpc: str, tx_hash: str) -> dict:
    trace = json.loads(
        run([
            "cast", "rpc", "debug_traceTransaction", tx_hash,
            json.dumps({"tracer": "callTracer", "tracerConfig": {"withLog": False}}),
            "--rpc-url", src_rpc,
        ])
    )
    for call in _iter_calls(trace):
        data = (call.get("input") or "").lower()
        if not data.startswith("0x4389e58f"):
            continue
        decoded = run(["cast", "calldata-decode", "send((uint64,uint32,address,uint32,bytes32,bytes32,bytes),bytes,bool)", data])
        first = decoded.splitlines()[0].strip()
        m = re.match(r"\((\d+),\s*(\d+)(?:\s*\[[^\]]+\])?,\s*(0x[a-fA-F0-9]{40}),\s*(\d+)(?:\s*\[[^\]]+\])?,\s*(0x[a-fA-F0-9]{64}),\s*(0x[a-fA-F0-9]{64}),\s*(0x[a-fA-F0-9]*)\)", first)
        if not m:
            raise RuntimeError(f"parse failed for decoded send packet: {first}")
        nonce, src_eid, sender, dst_eid, receiver_b32, guid, message = m.groups()
        return {
            "nonce": int(nonce),
            "srcEid": int(src_eid),
            "dstEid": int(dst_eid),
            "sender": sender.lower(),
            "senderB32": "0x" + ("00" * 12) + sender.lower().removeprefix("0x"),
            "receiver": "0x" + receiver_b32[-40:],
            "guid": guid,
            "message": message,
        }
    raise RuntimeError(f"send((...),bytes,bool) not found in trace: {tx_hash}")


def relay_exact_lz_packet(src_rpc: str, dst_rpc: str, tx_hash: str) -> dict:
    pkt = _extract_send_packet_from_tx(src_rpc, tx_hash)
    endpoint = cast_call(dst_rpc, pkt["receiver"], "endpoint()(address)").splitlines()[0].strip()
    run(["cast", "rpc", "anvil_setBalance", endpoint, "0x3635C9ADC5DEA00000", "--rpc-url", dst_rpc])
    relay_tx = extract_tx_hash(run([
        "cast", "send", pkt["receiver"],
        "lzReceive((uint32,bytes32,uint64),bytes32,bytes,address,bytes)",
        f"({pkt['srcEid']},{pkt['senderB32']},{pkt['nonce']})", pkt["guid"], pkt["message"],
        "0x0000000000000000000000000000000000000000", "0x",
        "--rpc-url", dst_rpc, "--unlocked", "--from", endpoint, "--gas-limit", "1500000",
    ]))
    pkt["relayTx"] = relay_tx
    pkt["endpoint"] = endpoint
    return pkt


def _deploy_socket_mock(l2_rpc: str) -> str:
    if l2_rpc in SOCKET_MOCK_CACHE:
        return SOCKET_MOCK_CACHE[l2_rpc]
    deployed = json.loads(run([
        "forge", "create", "src/mocks/SocketMessageTrackerMock.sol:SocketMessageTrackerMock",
        "--rpc-url", l2_rpc, "--private-key", ANVIL_PK0, "--broadcast", "--json",
    ]))["deployedTo"]
    SOCKET_MOCK_CACHE[l2_rpc] = deployed
    return deployed


def _deploy_bridge_mock(l1_rpc: str) -> str:
    if l1_rpc in BRIDGE_MOCK_CACHE:
        return BRIDGE_MOCK_CACHE[l1_rpc]
    deployed = json.loads(run([
        "forge", "create", "test/mocks/MockBridgeAdapter.sol:MockBridgeAdapter",
        "--rpc-url", l1_rpc, "--private-key", ANVIL_PK0, "--broadcast", "--json",
    ]))["deployedTo"]
    BRIDGE_MOCK_CACHE[l1_rpc] = deployed
    return deployed


def _pending_message_raw(l2_rpc: str, receiver: str, guid: str) -> str:
    return cast_call(
        l2_rpc,
        receiver,
        "pendingMessages(bytes32)(uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes)",
        guid,
    )


def _parse_pending_message(raw: str) -> dict[str, str | int]:
    cleaned = re.sub(r"\s*\[[^\]]+\]", "", raw.strip())
    tuple_match = re.match(
        r"^\((\d+),\s*(\d+),\s*(0x[a-fA-F0-9]{40}),\s*(\d+),\s*(0x[a-fA-F0-9]{40}),\s*(\d+),\s*(0x[a-fA-F0-9]{64}),\s*(\d+),\s*(0x[a-fA-F0-9]{64}),\s*(\d+),\s*(0x[a-fA-F0-9]*)\)$",
        cleaned,
    )
    if tuple_match:
        parts = [tuple_match.group(i) for i in range(1, 12)]
    else:
        parts = [line.strip() for line in cleaned.splitlines() if line.strip()]
        if len(parts) != 11:
            raise RuntimeError(f"failed parse pending message: {raw}")

    return {
        "action": int(parts[0]),
        "loanId": int(parts[1]),
        "asset": parts[2],
        "amount": int(parts[3]),
        "recipient": parts[4],
        "subaccountId": int(parts[5]),
        "socketMessageId": parts[6],
        "secondaryAmount": int(parts[7]),
        "quoteHash": parts[8],
        "takerNonce": int(parts[9]),
        "data": parts[10],
    }


def _ensure_socket_finalized_for_guid(l2_rpc: str, receiver: str, guid: str) -> dict:
    msg = _parse_pending_message(_pending_message_raw(l2_rpc, receiver, guid))
    socket_message_id = str(msg["socketMessageId"])
    if socket_message_id == "0x" + "00" * 32:
        return {"socketMessageId": socket_message_id, "updated": False}
    mock = _deploy_socket_mock(l2_rpc)
    set_socket_tx = cast_send(l2_rpc, receiver, "setSocket(address)", mock)
    set_exe_tx = cast_send(l2_rpc, mock, "setExecuted(bytes32,bool)", socket_message_id, "true")
    return {"socketMessageId": socket_message_id, "socketTracker": mock, "setSocketTx": set_socket_tx, "setExecutedTx": set_exe_tx, "updated": True}


def _ensure_receiver_asset_balance_for_guid(l2_rpc: str, receiver: str, guid: str) -> dict:
    msg = _parse_pending_message(_pending_message_raw(l2_rpc, receiver, guid))
    asset = str(msg["asset"])
    amount = int(msg["amount"])
    amount_s = str(amount)
    bal_before = int(cast_call(l2_rpc, asset, "balanceOf(address)(uint256)", receiver).split()[0])
    if bal_before >= amount:
        return {"asset": asset, "amount": amount_s, "balanceBefore": str(bal_before), "funded": False}
    run(["cast", "rpc", "anvil_setBalance", L2_WETH_MOCK_CONTROLLER, "0x3635C9ADC5DEA00000", "--rpc-url", l2_rpc])
    mint_tx = extract_tx_hash(run([
        "cast", "send", asset, "mint(address,uint256)", receiver, str(amount - bal_before),
        "--rpc-url", l2_rpc, "--unlocked", "--from", L2_WETH_MOCK_CONTROLLER,
    ]))
    bal_after = int(cast_call(l2_rpc, asset, "balanceOf(address)(uint256)", receiver).split()[0])
    if bal_after < amount:
        raise RuntimeError(f"receiver balance still short: {bal_after} < {amount}")
    return {"asset": asset, "amount": amount_s, "balanceBefore": str(bal_before), "balanceAfter": str(bal_after), "mintTx": mint_tx, "mintFrom": L2_WETH_MOCK_CONTROLLER, "funded": True}


def _ensure_l2_keeper_role(l2_rpc: str, receiver: str, keeper: str) -> dict:
    role = run(["cast", "keccak", "KEEPER_ROLE"]).strip()
    try:
        grant_tx = cast_send(l2_rpc, receiver, "grantRole(bytes32,address)", role, keeper)
        return {"granted": True, "grantTx": grant_tx}
    except Exception as e:
        # Some forks use receivers without AccessControl-compatible introspection/admin wiring.
        # Keep the flow running and let keeper execution be the source of truth.
        return {"granted": False, "warning": str(e)}


def _ensure_tsa_signer(l2_rpc: str, tsa: str, receiver: str) -> dict:
    updates: dict[str, object] = {}

    receiver_is_signer = cast_call(l2_rpc, tsa, "isSigner(address)(bool)", receiver).strip().lower() == "true"
    if receiver_is_signer:
        updates["receiverAlreadySigner"] = True
    else:
        updates["receiverAlreadySigner"] = False
        updates["receiverSetSignerTx"] = cast_send(l2_rpc, tsa, "setSigner(address,bool)", receiver, "true")

    keeper_is_signer = cast_call(l2_rpc, tsa, "isSigner(address)(bool)", ANVIL_ADDR0).strip().lower() == "true"
    if keeper_is_signer:
        updates["keeperAlreadySigner"] = True
    else:
        updates["keeperAlreadySigner"] = False
        updates["keeperSetSignerTx"] = cast_send(l2_rpc, tsa, "setSigner(address,bool)", ANVIL_ADDR0, "true")

    return updates


def _parse_keeper_output(raw: str) -> dict:
    raw = raw.strip()
    try:
        return json.loads(raw, strict=False)
    except Exception:
        txs = re.findall(r"0x[a-fA-F0-9]{64}", raw)
        return {"raw": raw, "txHashes": txs}


def _assert_true(name: str, cond: bool) -> None:
    if not cond:
        raise RuntimeError(f"assertion failed: {name}")


@app.command()
def main(
    l1_json: Path = typer.Option(L1_ARTIFACT_JSON),
    l2_json: Path = typer.Option(L2_ARTIFACT_JSON),
    l1_rpc: str = typer.Option(f"http://127.0.0.1:{L1_ANVIL_PORT}"),
    l2_rpc: str = typer.Option(f"http://127.0.0.1:{L2_ANVIL_PORT}"),
    collateral_asset: str = typer.Option(L1_COLLATERAL_ASSET, help="Override L1 collateral asset used for fresh deposit"),
    faucet: str = typer.Option("", help="Override faucet contract address"),
    relay_l2_ack_to_l1: bool = typer.Option(True, "--relay-l2-ack-to-l1/--no-relay-l2-ack-to-l1"),
    json_output: bool = typer.Option(False, "--json", help="Output machine-readable JSON report"),
):
    l1 = json.loads((ROOT / l1_json).read_text())
    l2 = json.loads((ROOT / l2_json).read_text())
    l1a = l1.get("addrs", l1)
    l2a = l2.get("addrs", l2)

    vault = l1a["l1Vault"]
    receiver = l2a["l2Receiver"]

    out: dict = {"ok": False, "steps": []}

    def step(name: str, fn):
        rec = {"step": name, "ok": False}
        try:
            rec["result"] = fn()
            rec["ok"] = True
        except Exception as e:
            rec["error"] = str(e)
            out["steps"].append(rec)
            if json_output:
                print(json.dumps(out, indent=2))
            else:
                _print_human_summary(out)
            raise SystemExit(1)
        out["steps"].append(rec)

    step("grant_l2_keeper", lambda: _ensure_l2_keeper_role(l2_rpc, receiver, ANVIL_ADDR0))

    def do_deposit():
        borrower = wallet_address(BORROWER_PK)
        run(["cast", "rpc", "anvil_setBalance", borrower, "0x3635C9ADC5DEA00000", "--rpc-url", l1_rpc])
        if run(["cast", "code", borrower, "--rpc-url", l1_rpc]).strip().lower() != "0x":
            raise RuntimeError(f"borrower has code (not EOA): {borrower}")

        l1_env = {}
        for line in (ROOT / ".env.l1.testnet").read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                l1_env[k.strip()] = v.strip()

        configured_weth = l1_env["WETH_ASSET"]
        weth = collateral_asset or configured_weth
        if not _has_code(l1_rpc, weth):
            raise RuntimeError(
                f"collateral asset has no code on L1 fork: {weth}"
                + (f" (configured WETH_ASSET={configured_weth})" if collateral_asset else "")
            )

        faucet_addr = faucet or l1_env.get("FAUCET", "0x0000000000000000000000000000000000000000")
        permit2 = cast_call(l1_rpc, vault, "permit2()(address)").splitlines()[0].strip()
        next_loan = int(cast_call(l1_rpc, vault, "nextLoanId()(uint256)").split()[0])

        # force l1->l2 asset mapping to TSA underlying on this fork
        tsa = l2a["l2Tsa"]
        base_raw = cast_call(l2_rpc, tsa, "getBaseTSAAddresses()(address,address,address,address,address,address,address)")
        addrs = re.findall(r"0x[a-fA-F0-9]{40}", base_raw)
        wrapped_deposit_asset = addrs[2]
        underlying = cast_call(l2_rpc, wrapped_deposit_asset, "wrappedAsset()(address)").splitlines()[0].strip()
        scale = int(cast_call(l1_rpc, vault, "strikeScale(address)(uint256)", weth).split()[0])
        if scale == 0:
            scale = 10**30
        cast_send(l1_rpc, vault, "setCollateralConfig(address,bool,uint256,address)", weth, "true", str(scale), underlying)
        _ensure_liquidity_vault_role(l1_rpc, vault)

        fund_mode = ensure_token_balance_via_faucet(l1_rpc, faucet_addr, weth, borrower, 10**18)
        cast_send(l1_rpc, weth, "approve(address,uint256)", permit2, str(2**256 - 1), private_key=BORROWER_PK)

        chain_id = int(run(["cast", "chain-id", "--rpc-url", l1_rpc]))
        latest = json.loads(run(["cast", "block", "latest", "--rpc-url", l1_rpc, "--json"]))
        ts_raw = latest.get("timestamp")
        now = int(ts_raw, 0) if isinstance(ts_raw, str) else int(ts_raw)
        expiration = now + 3600
        sig_deadline = now + 3600
        allowance = cast_call(l1_rpc, permit2, "allowance(address,address,address)(uint160,uint48,uint48)", borrower, weth, vault)
        nonce = int(allowance.splitlines()[2].split()[0])
        sig = sign_permit2_single(chain_id, permit2, weth, 10**18, expiration, nonce, vault, sig_deadline, BORROWER_PK)

        maturity = now + 7 * 24 * 3600
        put_strike = 1500000000000000000000
        call_strike = 1700000000000000000000
        borrow_amount = 1500000000
        usdc_asset = cast_call(l1_rpc, vault, "usdc()(address)").splitlines()[0].strip()
        liquidity_vault = cast_call(l1_rpc, vault, "liquidityVault()(address)").splitlines()[0].strip()
        _seed_l1_liquidity_vault(l1_rpc, usdc_asset, liquidity_vault, borrow_amount)
        params = f"({weth},1000000000000000000,{maturity},{put_strike},{borrow_amount})"
        permit_arg = f"(({weth},1000000000000000000,{expiration},{nonce}),{vault},{sig_deadline})"

        rfq_expiry = now + 3600
        mandate_deadline = now + resolve_mandate_ttl(l1_rpc, vault)
        rfq_nonce = now
        rfq_arg = f"(0,{weth},1000000000000000000,{maturity},{put_strike},{call_strike},{borrow_amount},0,{rfq_expiry},{borrower},{rfq_nonce})"
        rfq_hash = cast_call(
            l1_rpc,
            vault,
            "hashBaselineRfq((uint256,address,uint256,uint64,uint256,uint256,uint256,uint256,uint64,address,uint256))(bytes32)",
            rfq_arg,
        ).splitlines()[0].strip()
        # RFQ is keeper-signed in production; e2e forks sign with local keeper key.
        rfq_sig = sign_hash_no_prefix(rfq_hash, ANVIL_PK0)

        l2_recipient = cast_call(l1_rpc, vault, "l2Recipient()(address)").splitlines()[0].strip()
        try:
            bridge_fee = int(cast_call(l1_rpc, vault, "estimateBridgeFees(address,address,uint256)(uint256)", weth, l2_recipient, str(10**18)).split()[0])
        except Exception:
            bridge_fee = 0
        lz_messenger = cast_call(l1_rpc, vault, "lzMessenger()(address)").splitlines()[0].strip()
        l2_asset = cast_call(l1_rpc, vault, "l2MessageAsset(address)(address)", weth).splitlines()[0].strip()
        subaccount_id = int(cast_call(l1_rpc, vault, "deriveSubaccountId()(uint256)").split()[0])
        default_opts = cast_call(l1_rpc, lz_messenger, "defaultOptions()(bytes)").splitlines()[0].strip()
        deposit_quote_msg = f"(0,{next_loan},{l2_asset},1000000000000000000,{vault},{subaccount_id},0x{'00'*32},0,0x{'00'*32},0,0x)"
        deposit_lz_fee = int(re.search(r"\d+", cast_call(l1_rpc, lz_messenger, "quoteMessage((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes),bytes)((uint256,uint256))", deposit_quote_msg, default_opts)).group(0))
        apr = int(cast_call(l1_rpc, vault, "originationFeeApr()(uint256)").split()[0])
        year = 365 * 24 * 3600
        fixed_interest = ((borrow_amount * apr) // 10**18) * (maturity - now) // year
        max_roll_ltv = int(cast_call(l1_rpc, vault, "maxRollLtv()(uint256)").split()[0])
        strike_scale = int(cast_call(l1_rpc, vault, "strikeScale(address)(uint256)", weth).split()[0])
        mandate_data = _abi_encode(
            "f(address,uint256,uint256,uint256,uint256,uint256,uint256,uint64,uint64)",
            borrower,
            str(call_strike),
            str(put_strike),
            "0",
            str(fixed_interest),
            str(max_roll_ltv),
            str(strike_scale),
            str(maturity),
            str(mandate_deadline),
        )
        mandate_quote_msg = f"(6,{next_loan},{weth},{borrow_amount},{vault},{subaccount_id},0x{'00'*32},0,0x{'00'*32},0,{mandate_data})"
        mandate_lz_fee = int(re.search(r"\d+", cast_call(l1_rpc, lz_messenger, "quoteMessage((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes),bytes)((uint256,uint256))", mandate_quote_msg, default_opts)).group(0))

        create_sig = "createDepositWithMandatePermit((address,uint256,uint256,uint256,uint256),(uint256,address,uint256,uint64,uint256,uint256,uint256,uint256,uint64,address,uint256),bytes,uint64,((address,uint160,uint48,uint48),address,uint256),bytes)"
        fallback_bridge = None
        try:
            tx = cast_send(
                l1_rpc,
                vault,
                create_sig,
                params,
                rfq_arg,
                rfq_sig,
                str(mandate_deadline),
                permit_arg,
                sig,
                value=str(bridge_fee + deposit_lz_fee + mandate_lz_fee),
                private_key=BORROWER_PK,
            )
        except Exception as e:
            err = str(e)
            if "TRANSFER_FROM_FAILED" not in err and "NotEnoughNative" not in err:
                raise
            # Sepolia fork can carry stale Socket route config or drifted bridge fees.
            fallback_bridge = _deploy_bridge_mock(l1_rpc)
            cast_send(l1_rpc, fallback_bridge, "setFee(uint256)", "0")
            mock_msg_id = "0x" + "11" * 32
            cast_send(l1_rpc, fallback_bridge, "setMessageId(bytes32)", mock_msg_id)
            cast_send(l1_rpc, vault, "setSocketVaultConfig(address,address)", weth, fallback_bridge)
            tx = cast_send(
                l1_rpc,
                vault,
                create_sig,
                params,
                rfq_arg,
                rfq_sig,
                str(mandate_deadline),
                permit_arg,
                sig,
                value=str(deposit_lz_fee + mandate_lz_fee),
                private_key=BORROWER_PK,
            )
        return {
            "tx": tx,
            "loanId": next_loan,
            "fundMode": fund_mode,
            "borrower": borrower,
            "bridgeFee": bridge_fee,
            "depositLzFee": deposit_lz_fee,
            "mandateLzFee": mandate_lz_fee,
            "bridgeAdapterFallback": fallback_bridge,
        }

    step("create_deposit_with_permit", do_deposit)
    step("relay_l1_to_l2_exact", lambda: relay_exact_lz_packet(l1_rpc, l2_rpc, out["steps"][-1]["result"]["tx"]))
    step("simulate_socket_finalized", lambda: _ensure_socket_finalized_for_guid(l2_rpc, receiver, out["steps"][-1]["result"]["guid"]))
    step("fund_l2_receiver_for_deposit", lambda: _ensure_receiver_asset_balance_for_guid(l2_rpc, receiver, out["steps"][-2]["result"]["guid"]))
    step("grant_tsa_signer", lambda: _ensure_tsa_signer(l2_rpc, l2a["l2Tsa"], receiver))

    def run_l2_keeper_once():
        base = (ROOT / ".env.l2.testnet").read_text()
        tmpdir = Path(tempfile.mkdtemp(prefix="e2e-l2-"))
        tmp = tmpdir / ".env.l2.fork"
        state = tmpdir / "keeper_l2_state.json"
        tmp.write_text(base + f"\nRPC_URL={l2_rpc}\nL2_RECEIVER={receiver}\n")

        env = dict(os.environ)
        env.update({"NO_COLOR": "1", "CLICOLOR": "0", "TERM": "dumb"})
        p = subprocess.run(
            ["uv", "run", "python", str(ROOT / "ops/management/l2_keeper_handle_messages.py"), str(tmp), "--state-file", str(state), "--once", "--broadcast", "--private-key", ANVIL_PK0, "--json"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        if p.returncode != 0:
            raise RuntimeError(f"l2 keeper failed ({p.returncode}): {p.stderr.strip()}\n{p.stdout}")
        return _parse_keeper_output(p.stdout)

    step("l2_keeper_handle_deposit", run_l2_keeper_once)

    if not relay_l2_ack_to_l1:
        out["ok"] = True
        if json_output:
            print(json.dumps(out, indent=2))
        else:
            _print_human_summary(out)
        return

    def relay_l2_ack_to_l1():
        k = out["steps"][-1]["result"]
        handled = k.get("handled", []) if isinstance(k, dict) else []
        deposit_guid = out["steps"][2]["result"]["guid"]
        sent = [
            h
            for h in handled
            if isinstance(h, dict)
            and h.get("status") == "sent"
            and h.get("guid") == deposit_guid
        ]
        if sent:
            ack_tx = (
                sent[0].get("depositConfirmedTx")
                or sent[0].get("collateralReturnedTx")
                or sent[0].get("tradeConfirmedTx")
                or sent[0].get("tx")
            )
        else:
            hashes = k.get("txHashes", []) if isinstance(k, dict) else []
            if not hashes:
                raise RuntimeError(f"no ack tx found in keeper output: {json.dumps(k)}")
            ack_tx = hashes[-1]
        if not ack_tx:
            raise RuntimeError(f"keeper handled message but produced no relayable tx: {json.dumps(sent[0])}")
        return relay_exact_lz_packet(l2_rpc, l1_rpc, ack_tx)

    step("relay_l2_to_l1_exact", relay_l2_ack_to_l1)

    def verify_expected_state():
        loan_id = int(out["steps"][1]["result"]["loanId"])
        l1_to_l2_guid = out["steps"][2]["result"]["guid"]
        l2_to_l1_guid = out["steps"][7]["result"]["guid"]

        handled = cast_call(l2_rpc, receiver, "handledMessages(bytes32)(bool)", l1_to_l2_guid).strip().lower()
        _assert_true("l2 handledMessages guid", handled == "true")

        messenger = l1a["l1Messenger"]
        msg_raw = cast_call(
            l1_rpc,
            messenger,
            "receivedMessage(bytes32)((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes))",
            l2_to_l1_guid,
        )
        m = re.match(r"\((\d+),\s*(\d+)(?:\s*\[[^\]]+\])?,", msg_raw)
        if not m:
            raise RuntimeError(f"failed parsing receivedMessage: {msg_raw}")
        action, msg_loan = int(m.group(1)), int(m.group(2))
        _assert_true("ack action is DepositConfirmed(3)", action == 3)
        _assert_true("ack loanId matches", msg_loan == loan_id)

        tmp = Path(tempfile.mkdtemp(prefix="e2e-l1-")) / ".env.l1.fork"
        tmp.write_text((ROOT / ".env.l1.testnet").read_text() + f"\nRPC_URL={l1_rpc}\nL1_VAULT={vault}\nL1_MESSENGER={messenger}\n")
        pre = json.loads(
            run([
                "uv",
                "run",
                "python",
                str(ROOT / "ops/management/l1_message_preflight.py"),
                str(tmp),
                "--json",
                "--logs-rpc-url",
                l1_rpc,
            ]),
            strict=False,
        )

        results = pre.get("results", [])
        mine = [r for r in results if str(r.get("loanId")) == str(loan_id)]
        _assert_true("preflight contains loan", len(mine) == 1)
        _assert_true("preflight has pending deposit", bool(mine[0].get("hasPendingDeposit")))
        _assert_true("preflight not ready to finalize yet", not bool(mine[0].get("readyToFinalize")))

        return {
            "loanId": loan_id,
            "l1ToL2Guid": l1_to_l2_guid,
            "l2ToL1Guid": l2_to_l1_guid,
            "l2Handled": True,
            "ackAction": action,
            "ackLoanId": msg_loan,
            "preflight": mine[0],
        }

    step("verify_expected_state", verify_expected_state)

    out["ok"] = True
    if json_output:
        print(json.dumps(out, indent=2))
    else:
        _print_human_summary(out)


if __name__ == "__main__":
    app()
