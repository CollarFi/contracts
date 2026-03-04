#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from pathlib import Path

from common import (
    ANVIL_PK0,
    BORROWER_PK,
    abi_encode,
    borrower_address,
    cast_call,
    cast_send_pk,
    inject_lz_message,
    run,
    run_fresh_loan_flow,
    seed_l1_liquidity_vault,
    sign_no_prefix,
)


def parse_pending_deposit(raw: str) -> dict:
    match = re.search(
        r"\((0x[a-fA-F0-9]{40}),\s*(0x[a-fA-F0-9]{40}),\s*(\d+)(?:\s*\[[^\]]+\])?,\s*(\d+)(?:\s*\[[^\]]+\])?,\s*(\d+)(?:\s*\[[^\]]+\])?,\s*(\d+)(?:\s*\[[^\]]+\])?\)",
        raw,
        flags=re.S,
    )
    if not match:
        raise RuntimeError(f"failed to parse pending deposit: {raw}")

    borrower, asset, collateral, maturity, put_strike, borrow_amount = match.groups()
    return {
        "borrower": borrower,
        "asset": asset,
        "collateral": int(collateral),
        "maturity": int(maturity),
        "putStrike": int(put_strike),
        "borrowAmount": int(borrow_amount),
    }


def latest_timestamp(rpc: str) -> int:
    block = json.loads(run(["cast", "block", "latest", "--rpc-url", rpc, "--json"]))
    ts_raw = block.get("timestamp")
    return int(ts_raw, 0) if isinstance(ts_raw, str) else int(ts_raw)


def parse_loan(vault_loan_raw: str) -> dict:
    lines = [line.strip() for line in vault_loan_raw.splitlines() if line.strip()]
    if len(lines) < 13:
        raise RuntimeError(f"unexpected loan tuple output: {vault_loan_raw}")
    return {
        "borrower": lines[0].split()[0],
        "collateralAsset": lines[1].split()[0],
        "collateralAmount": int(lines[2].split()[0]),
        "maturity": int(lines[3].split()[0]),
        "putStrike": int(lines[4].split()[0]),
        "callStrike": int(lines[5].split()[0]),
        "principal": int(lines[6].split()[0]),
        "subaccountId": int(lines[7].split()[0]),
        "state": int(lines[8].split()[0]),
        "startTime": int(lines[9].split()[0]),
        "interestApr": int(lines[10].split()[0]),
        "interestOwed": int(lines[11].split()[0]),
        "variableDebt": int(lines[12].split()[0]),
    }


def parse_mandate(vault_mandate_raw: str) -> dict:
    lines = [line.strip() for line in vault_mandate_raw.splitlines() if line.strip()]
    if len(lines) < 10:
        raise RuntimeError(f"unexpected mandate tuple output: {vault_mandate_raw}")
    return {
        "borrower": lines[0].split()[0],
        "collateralAsset": lines[1].split()[0],
        "collateralAmount": int(lines[2].split()[0]),
        "maturity": int(lines[3].split()[0]),
        "deadline": int(lines[4].split()[0]),
        "borrowAmount": int(lines[5].split()[0]),
        "minCallStrike": int(lines[6].split()[0]),
        "maxPutStrike": int(lines[7].split()[0]),
        "minNetInterest": int(lines[8].split()[0]),
        "sentToL2": lines[9].split()[0].lower() == "true",
    }


def get_pending(vault: str, l1_rpc: str, loan_id: int) -> dict:
    raw = cast_call(
        l1_rpc,
        vault,
        "pendingDeposits(uint256)((address,address,uint256,uint256,uint256,uint256))",
        str(loan_id),
    )
    return parse_pending_deposit(raw)


def get_loan(vault: str, l1_rpc: str, loan_id: int) -> dict:
    raw = cast_call(
        l1_rpc,
        vault,
        "loans(uint256)(address,address,uint256,uint256,uint256,uint256,uint256,uint256,uint8,uint256,uint256,uint256,uint256)",
        str(loan_id),
    )
    return parse_loan(raw)


def get_mandate(vault: str, l1_rpc: str, loan_id: int) -> dict:
    raw = cast_call(
        l1_rpc,
        vault,
        "mandates(uint256)(address,address,uint256,uint64,uint64,uint256,uint256,uint256,uint256,bool)",
        str(loan_id),
    )
    return parse_mandate(raw)


def accept_mandate_for_pending(
    l1_rpc: str,
    vault: str,
    collateral_asset: str,
    loan_id: int,
    pending: dict,
    mandate_ttl: int = 1800,
    rfq_ttl: int = 3600,
    signer_pk: str = ANVIL_PK0,
    borrower_pk: str = BORROWER_PK,
) -> dict:
    borrower = borrower_address()
    now_ts = latest_timestamp(l1_rpc)
    rfq_expiry = now_ts + rfq_ttl
    mandate_deadline = now_ts + mandate_ttl
    call_strike = int(pending["putStrike"]) + 1

    rfq_tuple = (
        f"({loan_id},{collateral_asset},{pending['collateral']},{pending['maturity']},{pending['putStrike']},{call_strike},"
        f"{pending['borrowAmount']},0,{rfq_expiry},{borrower},0)"
    )
    rfq_hash = cast_call(
        l1_rpc,
        vault,
        "hashBaselineRfq((uint256,address,uint256,uint64,uint256,uint256,uint256,uint256,uint64,address,uint256))(bytes32)",
        rfq_tuple,
    ).splitlines()[0].strip()
    rfq_sig = sign_no_prefix(rfq_hash, signer_pk)

    lz_messenger = cast_call(l1_rpc, vault, "lzMessenger()(address)").splitlines()[0].strip()
    subaccount_id = int(cast_call(l1_rpc, vault, "deriveSubaccountId()(uint256)").split()[0])
    default_opts = cast_call(l1_rpc, lz_messenger, "defaultOptions()(bytes)").splitlines()[0].strip()
    apr = int(cast_call(l1_rpc, vault, "originationFeeApr()(uint256)").split()[0])
    year = 365 * 24 * 3600
    fixed_interest = ((int(pending["borrowAmount"]) * apr) // 10**18) * (int(pending["maturity"]) - now_ts) // year
    max_roll_ltv = int(cast_call(l1_rpc, vault, "maxRollLtv()(uint256)").split()[0])
    strike_scale = int(cast_call(l1_rpc, vault, "strikeScale(address)(uint256)", collateral_asset).split()[0])

    mandate_data = abi_encode(
        "f(address,uint256,uint256,uint256,uint256,uint256,uint256,uint64,uint64)",
        borrower,
        str(call_strike),
        str(pending["putStrike"]),
        "0",
        str(fixed_interest),
        str(max_roll_ltv),
        str(strike_scale),
        str(pending["maturity"]),
        str(mandate_deadline),
    )
    quote_msg = (
        f"(6,{loan_id},{collateral_asset},{pending['borrowAmount']},{vault},{subaccount_id},"
        f"0x{'00'*32},0,0x{'00'*32},0,{mandate_data})"
    )
    lz_fee = int(
        re.search(
            r"\d+",
            cast_call(
                l1_rpc,
                lz_messenger,
                "quoteMessage((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes),bytes)((uint256,uint256))",
                quote_msg,
                default_opts,
            ),
        ).group(0)
    )

    liquidity_vault = cast_call(l1_rpc, vault, "liquidityVault()(address)").splitlines()[0].strip()
    usdc_asset = cast_call(l1_rpc, vault, "usdc()(address)").splitlines()[0].strip()
    seed_l1_liquidity_vault(l1_rpc, usdc_asset, liquidity_vault, int(pending["borrowAmount"]))

    cast_send_pk(
        l1_rpc,
        vault,
        "acceptMandate(uint256,(uint256,address,uint256,uint64,uint256,uint256,uint256,uint256,uint64,address,uint256),bytes,uint64)",
        str(loan_id),
        rfq_tuple,
        rfq_sig,
        str(mandate_deadline),
        private_key=borrower_pk,
        value=str(lz_fee),
    )

    return {
        "borrower": borrower,
        "rfqTuple": rfq_tuple,
        "rfqHash": rfq_hash,
        "mandateDeadline": mandate_deadline,
        "fixedInterest": fixed_interest,
        "subaccountId": subaccount_id,
        "callStrike": call_strike,
    }


def inject_trade_confirmed(
    l1_rpc: str,
    messenger: str,
    loan_id: int,
    vault: str,
    subaccount_id: int,
    maturity: int,
    put_strike: int,
    call_strike: int,
    guid_nonce_base: int = 10_000_000,
) -> str:
    trade_data = abi_encode("f(uint256,uint256,uint64,int256)", str(call_strike), str(put_strike), str(maturity), "0")
    trade_msg = abi_encode(
        "f((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes))",
        f"(5,{loan_id},0x0000000000000000000000000000000000000000,0,{vault},{subaccount_id},0x{'00'*32},0,0x{'00'*32},0,{trade_data})",
    )
    trade_guid = "0x" + format(guid_nonce_base + loan_id, "064x")
    inject_lz_message(l1_rpc, messenger, trade_guid, trade_msg)
    return trade_guid


def inject_deposit_confirmed(
    l1_rpc: str,
    messenger: str,
    loan_id: int,
    vault: str,
    subaccount_id: int,
    collateral_asset: str,
    collateral_amount: int,
    guid: str,
) -> None:
    deposit_confirm_msg = abi_encode(
        "f((uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes))",
        f"(3,{loan_id},{collateral_asset},{collateral_amount},{vault},{subaccount_id},0x{'00'*32},0,0x{'00'*32},0,0x)",
    )
    inject_lz_message(l1_rpc, messenger, guid, deposit_confirm_msg)


def extract_tx_hash(raw: str) -> str:
    s = raw.strip()
    if re.fullmatch(r"0x[a-fA-F0-9]{64}", s):
        return s
    match = re.search(r"transactionHash\s+(0x[a-fA-F0-9]{64})", s)
    if match:
        return match.group(1)
    hashes = re.findall(r"0x[a-fA-F0-9]{64}", s)
    if hashes:
        return hashes[-1]
    raise RuntimeError(f"could not extract tx hash: {raw[:240]}")


def parse_json_or_fallback(raw: str) -> dict:
    value = raw.strip()
    try:
        return json.loads(value, strict=False)
    except Exception:
        txs = re.findall(r"0x[a-fA-F0-9]{64}", value)
        return {"raw": value, "txHashes": txs}


def run_fresh_pending_loan(
    l1_json: Path,
    l2_json: Path,
    l1_rpc: str,
    l2_rpc: str,
    collateral_asset: str,
) -> dict:
    flow = run_fresh_loan_flow(l1_json, l2_json, l1_rpc, l2_rpc, collateral_asset)
    if not flow.get("ok"):
        raise RuntimeError("fresh_loan_flow failed")
    verify = next((s.get("result") for s in flow.get("steps", []) if s.get("step") == "verify_expected_state"), None)
    if not isinstance(verify, dict):
        raise RuntimeError("fresh_loan_flow verify result missing")

    loan_id = int(verify["loanId"])
    deposit_guid = verify["l2ToL1Guid"]
    return {
        "flow": flow,
        "loanId": loan_id,
        "depositGuid": deposit_guid,
    }


def finalize_fresh_loan_to_active_zero_cost(
    l1_json: Path,
    l2_json: Path,
    l1_rpc: str,
    l2_rpc: str,
    vault: str,
    messenger: str,
    collateral_asset: str,
) -> dict:
    fresh = run_fresh_pending_loan(l1_json, l2_json, l1_rpc, l2_rpc, collateral_asset)
    loan_id = int(fresh["loanId"])
    deposit_guid = fresh["depositGuid"]
    pending = get_pending(vault, l1_rpc, loan_id)
    mandate_ctx = accept_mandate_for_pending(l1_rpc, vault, collateral_asset, loan_id, pending)
    subaccount_id = int(mandate_ctx["subaccountId"])

    trade_guid = inject_trade_confirmed(
        l1_rpc,
        messenger,
        loan_id,
        vault,
        subaccount_id,
        int(pending["maturity"]),
        int(pending["putStrike"]),
        int(mandate_ctx["callStrike"]),
    )
    inject_deposit_confirmed(
        l1_rpc,
        messenger,
        loan_id,
        vault,
        subaccount_id,
        collateral_asset,
        int(pending["collateral"]),
        deposit_guid,
    )
    cast_send_pk(l1_rpc, vault, "finalizeLoan(uint256,bytes32,bytes32)", str(loan_id), deposit_guid, trade_guid)
    loan = get_loan(vault, l1_rpc, loan_id)
    if int(loan["state"]) != 1:
        raise RuntimeError(f"loan not ACTIVE_ZERO_COST after finalize (state={loan['state']})")

    return {
        "loanId": loan_id,
        "depositGuid": deposit_guid,
        "tradeGuid": trade_guid,
        "pending": pending,
        "mandate": mandate_ctx,
        "loan": loan,
        "fresh": fresh,
    }
