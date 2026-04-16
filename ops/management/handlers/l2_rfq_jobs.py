#!/usr/bin/env python3
from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any

from lz_harness.common import cast_call
from management.handlers.l2_derive_client import normalize_decimal_str, resolve_asset_name
from management.handlers.l2_rfq_api import (
    amount_to_decimal_str,
    build_instrument_lookup,
    cancel_rfq,
    decimal_from_1e18_int,
    decimal_to_1e18_int,
    get_option_instruments,
    instrument_key,
    leg_amount_decimal,
    poll_quotes,
    quote_hash,
    quote_id,
    quote_max_fee_decimal,
    rfq_id,
    send_rfq,
    subaccount_id,
)
from management.handlers.l2_rfq_common import (
    normalize_rfq_execute_quote,
    sign_and_submit_rfq_execute_quote,
    submit_rfq_trade_confirmation,
)
from py_lib.keeper_state import save_keeper_state

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def _strip_units(value: str) -> str:
    return value.strip().split()[0]


def _parse_l2_loan(raw: str) -> dict[str, Any]:
    cleaned = raw.strip()
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = cleaned[1:-1]
    cleaned = cleaned.replace("[", " [")
    parts = [segment.strip() for segment in cleaned.split(",")]
    values = [_strip_units(part) for part in parts if part.strip()]
    if len(values) != 25:
        raise RuntimeError(f"unexpected loan tuple output: {raw}")
    return {
        "borrower": values[0],
        "borrowAmount": int(values[1]),
        "minCallStrike": int(values[2]),
        "maxPutStrike": int(values[3]),
        "minNetInterest": int(values[4]),
        "fixedInterest": int(values[5]),
        "maxRollLtv": int(values[6]),
        "strikeScale": int(values[7]),
        "maturity": int(values[8]),
        "deadline": int(values[9]),
        "collateralAsset": values[10],
        "collateralAmount": int(values[11]),
        "depositExecuted": values[12].lower() == "true",
        "tradeExecuted": values[13].lower() == "true",
        "returnRequested": values[14].lower() == "true",
        "rolloverPending": values[15].lower() == "true",
        "rolloverMandateHash": values[16],
        "rolloverMinCallStrike": int(values[17]),
        "rolloverMaxPutStrike": int(values[18]),
        "rolloverMinNetInterest": int(values[19]),
        "rolloverFixedInterest": int(values[20]),
        "rolloverMaxRollLtv": int(values[21]),
        "rolloverStrikeScale": int(values[22]),
        "rolloverMaturity": int(values[23]),
        "rolloverDeadline": int(values[24]),
    }


def _load_l2_loan(runtime: Any, loan_id: int) -> dict[str, Any]:
    raw = cast_call(
        runtime.rpc_url,
        runtime.loan_store_addr,
        "getLoan(uint256)((address,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,address,uint256,bool,bool,bool,bool,bytes32,uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool))",
        str(int(loan_id)),
        allow_fail=True,
    )
    if raw == "N/A":
        raise RuntimeError(f"failed to read L2 loan {loan_id}")
    return _parse_l2_loan(raw)


def _rfq_label(loan_id: int, mandate_key: str) -> str:
    return f"collar-loan-{loan_id}-{mandate_key[:12]}"


def _active_mandate_snapshot(loan: dict[str, Any]) -> dict[str, Any] | None:
    if loan["borrower"].lower() == ZERO_ADDRESS.lower():
        return None
    if loan["rolloverPending"]:
        return {
            "borrower": loan["borrower"],
            "borrowAmount": int(loan["borrowAmount"]),
            "minCallStrike": int(loan["rolloverMinCallStrike"]),
            "maxPutStrike": int(loan["rolloverMaxPutStrike"]),
            "minNetInterest": int(loan["rolloverMinNetInterest"]),
            "fixedInterest": int(loan["rolloverFixedInterest"]),
            "maturity": int(loan["rolloverMaturity"]),
            "deadline": int(loan["rolloverDeadline"]),
            "collateralAsset": loan["collateralAsset"],
            "collateralAmount": int(loan["collateralAmount"]),
            "rolloverPending": True,
        }
    return {
        "borrower": loan["borrower"],
        "borrowAmount": int(loan["borrowAmount"]),
        "minCallStrike": int(loan["minCallStrike"]),
        "maxPutStrike": int(loan["maxPutStrike"]),
        "minNetInterest": int(loan["minNetInterest"]),
        "fixedInterest": int(loan["fixedInterest"]),
        "maturity": int(loan["maturity"]),
        "deadline": int(loan["deadline"]),
        "collateralAsset": loan["collateralAsset"],
        "collateralAmount": int(loan["collateralAmount"]),
        "rolloverPending": False,
    }


def _mandate_key(mandate: dict[str, Any]) -> str:
    return json.dumps(mandate, sort_keys=True, separators=(",", ":"))


def _is_job_ready(loan: dict[str, Any], mandate: dict[str, Any]) -> bool:
    return (
        loan["depositExecuted"]
        and not loan["tradeExecuted"]
        and not loan["returnRequested"]
        and mandate["deadline"] > 0
        and mandate["maturity"] > 0
        and mandate["collateralAmount"] > 0
    )


def _cancel_open_rfq(runtime: Any, job: dict[str, Any]) -> None:
    rfq_state = job.get("rfq")
    if not isinstance(rfq_state, dict):
        return
    current_rfq_id = str(rfq_state.get("rfqId", "")).strip()
    if not current_rfq_id or not runtime.broadcast:
        return
    cancel_rfq(
        api_url=runtime.api_url,
        x_lyra_wallet=runtime.derive_wallet,
        account=runtime.account,
        private_key=runtime.private_key,
        subaccount_id=int(job["subaccountId"]),
        rfq_id=current_rfq_id,
    )
    rfq_state["cancelledAt"] = int(time.time())


def _ensure_rfq_job(state: dict[str, Any], loan_id: int, mandate: dict[str, Any], subaccount_id_value: int) -> dict[str, Any]:
    key = str(int(loan_id))
    job = state["rfqJobs"].get(key)
    next_key = _mandate_key(mandate)
    now_ts = int(time.time())
    if not isinstance(job, dict):
        job = {
            "loanId": int(loan_id),
            "status": "ready_to_send",
            "createdAt": now_ts,
            "updatedAt": now_ts,
            "subaccountId": int(subaccount_id_value),
            "mandate": mandate,
            "mandateKey": next_key,
            "rfq": {},
            "quotes": {},
            "selectedQuoteId": "",
            "execution": {},
            "confirmation": {},
            "attempts": {"sendRfq": 0, "poll": 0, "execute": 0, "confirm": 0},
            "error": None,
        }
        state["rfqJobs"][key] = job
        return job

    if job.get("mandateKey") != next_key:
        job.update(
            {
                "status": "ready_to_send",
                "updatedAt": now_ts,
                "mandate": mandate,
                "mandateKey": next_key,
                "rfq": {},
                "quotes": {},
                "selectedQuoteId": "",
                "execution": {},
                "confirmation": {},
                "error": None,
            }
        )
    else:
        job["mandate"] = mandate
        job["subaccountId"] = int(subaccount_id_value)
        job["updatedAt"] = now_ts
    return job


def _reset_job_for_new_mandate(job: dict[str, Any], mandate: dict[str, Any], mandate_key: str, subaccount_id_value: int) -> None:
    now_ts = int(time.time())
    job.update(
        {
            "status": "ready_to_send",
            "updatedAt": now_ts,
            "subaccountId": int(subaccount_id_value),
            "mandate": mandate,
            "mandateKey": mandate_key,
            "rfq": {},
            "quotes": {},
            "selectedQuoteId": "",
            "selectedQuote": {},
            "execution": {},
            "confirmation": {},
            "error": None,
        }
    )


def _instrument_legs(runtime: Any, mandate: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    currency = resolve_asset_name("", runtime.wrapped_deposit_asset, runtime.rpc_url)
    instruments = get_option_instruments(api_url=runtime.api_url, currency=currency)
    lookup = build_instrument_lookup(instruments)
    call = lookup.get(instrument_key(expiry=mandate["maturity"], strike_1e18=mandate["minCallStrike"], option_type="C"))
    put = lookup.get(instrument_key(expiry=mandate["maturity"], strike_1e18=mandate["maxPutStrike"], option_type="P"))
    if call is None or put is None:
        raise RuntimeError(
            f"missing instruments for loan mandate: call={mandate['minCallStrike']} put={mandate['maxPutStrike']} expiry={mandate['maturity']}"
        )
    return call, put


def _requested_rfq_payload(runtime: Any, mandate: dict[str, Any], *, loan_id: int, mandate_key: str) -> dict[str, Any]:
    call, put = _instrument_legs(runtime, mandate)
    amount = amount_to_decimal_str(int(mandate["collateralAmount"]))
    max_fee = amount_to_decimal_str(int(mandate["fixedInterest"]) + int(mandate["minNetInterest"]))
    return {
        "direction": "buy",
        "maxFee": max_fee,
        "label": _rfq_label(int(loan_id), mandate_key),
        "legs": [
            {
                "instrument_name": str(call["instrument_name"]),
                "direction": "sell",
                "amount": amount,
            },
            {
                "instrument_name": str(put["instrument_name"]),
                "direction": "buy",
                "amount": amount,
            },
        ],
        "callInstrument": call,
        "putInstrument": put,
    }


def _validate_quote_structure(job: dict[str, Any], raw_quote: dict[str, Any]) -> tuple[bool, str | None, dict[str, Any] | None]:
    try:
        execute_quote = normalize_rfq_execute_quote(raw_quote)
    except Exception as exc:
        return False, f"invalid quote payload: {exc}", None

    rfq_state = job.get("rfq", {})
    request = rfq_state.get("request", {})
    expected_direction = str(request.get("direction", "buy"))
    if str(execute_quote["direction"]).lower() == expected_direction.lower():
        return False, "quote direction matches RFQ direction", None

    if len(execute_quote["legs"]) != 2:
        return False, "quote does not contain exactly two legs", None

    expected_legs = request.get("legs", [])
    expected_by_name = {str(leg["instrument_name"]): leg for leg in expected_legs if isinstance(leg, dict)}
    for leg in execute_quote["legs"]:
        expected = expected_by_name.get(str(leg["instrumentName"]))
        if expected is None:
            return False, f"unexpected instrument {leg['instrumentName']}", None
        if str(leg["direction"]).lower() != str(expected["direction"]).lower():
            return False, f"direction mismatch for {leg['instrumentName']}", None
        if normalize_decimal_str(leg["amount"]) != normalize_decimal_str(expected["amount"]):
            return False, f"amount mismatch for {leg['instrumentName']}", None
        if str(leg["assetAddress"]).lower() != str(job.get("optionAsset", "")).lower():
            return False, f"asset mismatch for {leg['instrumentName']}", None

    if int(execute_quote["subaccountId"]) != int(job["subaccountId"]):
        return False, "quote subaccount mismatch", None

    return True, None, execute_quote


def _quote_expected_total(job: dict[str, Any], execute_quote: dict[str, Any]) -> tuple[int, int, int]:
    maker_global_direction = "sell" if str(execute_quote["direction"]).lower() == "buy" else "buy"
    expected_c = Decimal("0")
    for leg in execute_quote["legs"]:
        amount = leg_amount_decimal(leg)
        price = Decimal(normalize_decimal_str(leg["price"]))
        leg_sign = Decimal("1") if str(leg["direction"]).lower() == "buy" else Decimal("-1")
        quote_sign = Decimal("1") if maker_global_direction == "buy" else Decimal("-1")
        maker_amount = amount * leg_sign * quote_sign
        expected_c += -(price * maker_amount)

    derive_fee = Decimal(normalize_decimal_str(execute_quote["maxFee"]))
    fixed_interest = decimal_from_1e18_int(int(job["mandate"]["fixedInterest"]))
    min_net_interest = decimal_from_1e18_int(int(job["mandate"]["minNetInterest"]))
    expected_total = fixed_interest + expected_c - derive_fee

    return (
        decimal_to_1e18_int(expected_c),
        decimal_to_1e18_int(expected_total),
        decimal_to_1e18_int(min_net_interest),
    )


def _select_best_quote(job: dict[str, Any], polled_quotes: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    quote_records: dict[str, Any] = {}
    best_quote: dict[str, Any] | None = None
    best_total: int | None = None
    current_rfq_id = str(job.get("rfq", {}).get("rfqId", ""))

    for raw_quote in polled_quotes:
        if current_rfq_id and rfq_id(raw_quote) != current_rfq_id:
            continue
        qid = quote_id(raw_quote)
        valid, reject_reason, execute_quote = _validate_quote_structure(job, raw_quote)
        record: dict[str, Any] = {
            "seenAt": int(time.time()),
            "raw": raw_quote,
            "validation": {
                "structureOk": valid,
                "economicsOk": False,
                "expectedC": "0",
                "expectedTotal": "0",
                "deriveFee": "0",
                "rejectReason": reject_reason,
            },
        }
        if valid and execute_quote is not None:
            expected_c, expected_total, min_net_interest = _quote_expected_total(job, execute_quote)
            derive_fee = decimal_to_1e18_int(Decimal(normalize_decimal_str(execute_quote["maxFee"])))
            economics_ok = expected_c >= 0 and expected_total >= min_net_interest
            record["validation"].update(
                {
                    "economicsOk": economics_ok,
                    "expectedC": str(expected_c),
                    "expectedTotal": str(expected_total),
                    "deriveFee": str(derive_fee),
                    "rejectReason": None if economics_ok else "economics below minNetInterest",
                }
            )
            if economics_ok and (best_total is None or expected_total > best_total):
                best_total = expected_total
                best_quote = {
                    "rawQuote": raw_quote,
                    "executeQuote": execute_quote,
                    "expectedC": expected_c,
                    "expectedTotal": expected_total,
                    "deriveFee": derive_fee,
                }
        quote_records[qid or f"quote-{len(quote_records)}"] = record

    return best_quote, quote_records


def _append_reconcile_error(handled: list[dict[str, Any]], *, loan_id: int, loan_key: str, exc: Exception) -> None:
    handled.append(
        {
            "action": "RfqReconcile",
            "loanId": str(loan_id),
            "guid": loan_key,
            "status": f"error: {exc}",
        }
    )


def _close_terminal_job(runtime: Any, state: dict[str, Any], *, loan: dict[str, Any], job: dict[str, Any], now_ts: int) -> bool:
    if not (loan["tradeExecuted"] or loan["returnRequested"] or loan["borrower"].lower() == ZERO_ADDRESS.lower()):
        return False

    try:
        _cancel_open_rfq(runtime, job)
    except Exception as exc:
        job["error"] = str(exc)
    job["status"] = "completed" if loan["tradeExecuted"] else "cancelled"
    job["updatedAt"] = now_ts
    save_keeper_state(runtime.state_file, state)
    return True


def _prepare_rfq_job(
    runtime: Any,
    state: dict[str, Any],
    *,
    loan_id: int,
    loan: dict[str, Any],
    mandate: dict[str, Any] | None,
    job: dict[str, Any] | None,
    now_ts: int,
) -> dict[str, Any] | None:
    if isinstance(job, dict) and _close_terminal_job(runtime, state, loan=loan, job=job, now_ts=now_ts):
        return None

    if mandate is None:
        return None

    next_key = _mandate_key(mandate)
    if isinstance(job, dict) and job.get("mandateKey") != next_key:
        try:
            _cancel_open_rfq(runtime, job)
        except Exception as exc:
            job["error"] = str(exc)
        _reset_job_for_new_mandate(job, mandate, next_key, runtime.subaccount_id)
        save_keeper_state(runtime.state_file, state)

    job = _ensure_rfq_job(state, loan_id, mandate, runtime.subaccount_id)
    job["optionAsset"] = runtime.option_asset

    if mandate["deadline"] <= now_ts:
        try:
            _cancel_open_rfq(runtime, job)
        except Exception as exc:
            job["error"] = str(exc)
        job["status"] = "expired"
        job["updatedAt"] = now_ts
        save_keeper_state(runtime.state_file, state)
        return None

    if not _is_job_ready(loan, mandate):
        job["status"] = "waiting_state"
        job["updatedAt"] = now_ts
        return None

    return job


def _process_rfq_send(
    runtime: Any,
    state: dict[str, Any],
    handled: list[dict[str, Any]],
    *,
    loan_id: int,
    loan_key: str,
    mandate: dict[str, Any],
    job: dict[str, Any],
    now_ts: int,
) -> tuple[int, int]:
    item = {"action": "RfqSend", "loanId": str(loan_id), "guid": loan_key, "status": "dry-run"}
    sent = 0
    try:
        request = _requested_rfq_payload(runtime, mandate, loan_id=loan_id, mandate_key=str(job["mandateKey"]))
        job["rfq"] = {
            "request": {
                "direction": request["direction"],
                "maxFee": request["maxFee"],
                "label": request["label"],
                "legs": request["legs"],
            }
        }
        if runtime.broadcast:
            resp = send_rfq(
                api_url=runtime.api_url,
                x_lyra_wallet=runtime.derive_wallet,
                account=runtime.account,
                private_key=runtime.private_key,
                subaccount_id=int(job["subaccountId"]),
                direction=request["direction"],
                max_fee=request["maxFee"],
                label=request["label"],
                legs=request["legs"],
            )
            result = resp.get("result") if isinstance(resp.get("result"), dict) else {}
            job["rfq"].update(
                {
                    "rfqId": str(result.get("rfq_id") or result.get("rfqId") or resp.get("id") or ""),
                    "submittedAt": now_ts,
                    "apiId": resp.get("id"),
                }
            )
            item["status"] = "sent"
            item["rfqId"] = job["rfq"].get("rfqId")
            sent = 1
        job["status"] = "polling"
        job["attempts"]["sendRfq"] = int(job["attempts"].get("sendRfq", 0)) + 1
        job["updatedAt"] = now_ts
        job["error"] = None
        save_keeper_state(runtime.state_file, state)
    except Exception as exc:
        job["status"] = "ready_to_send"
        job["updatedAt"] = now_ts
        job["error"] = str(exc)
        save_keeper_state(runtime.state_file, state)
        item["status"] = f"error: {exc}"
    handled.append(item)
    return 1, sent


def _process_rfq_poll(
    runtime: Any,
    state: dict[str, Any],
    handled: list[dict[str, Any]],
    *,
    loan_id: int,
    loan_key: str,
    job: dict[str, Any],
    now_ts: int,
) -> tuple[int, int]:
    item = {"action": "RfqPoll", "loanId": str(loan_id), "guid": loan_key, "status": "no-quotes"}
    sent = 0
    try:
        polled = [] if not runtime.broadcast else poll_quotes(
            api_url=runtime.api_url,
            x_lyra_wallet=runtime.derive_wallet,
            account=runtime.account,
            private_key=runtime.private_key,
            subaccount_id=int(job["subaccountId"]),
        )
        best_quote, quote_records = _select_best_quote(job, polled)
        if quote_records:
            existing = job.get("quotes")
            if not isinstance(existing, dict):
                existing = {}
            existing.update(quote_records)
            job["quotes"] = existing
        job["attempts"]["poll"] = int(job["attempts"].get("poll", 0)) + 1
        if best_quote is not None:
            qid = quote_id(best_quote["rawQuote"])
            job["selectedQuoteId"] = qid
            job["selectedQuote"] = best_quote
            job["status"] = "quote_selected"
            item["status"] = "selected"
            item["quoteId"] = qid
            item["expectedTotal"] = str(best_quote["expectedTotal"])
            sent = 1
        job["updatedAt"] = now_ts
        job["error"] = None
        save_keeper_state(runtime.state_file, state)
    except Exception as exc:
        job["status"] = "polling"
        job["updatedAt"] = now_ts
        job["error"] = str(exc)
        save_keeper_state(runtime.state_file, state)
        item["status"] = f"error: {exc}"
    handled.append(item)
    return 1, sent


def _build_execute_trade(job: dict[str, Any], selected: dict[str, Any], mandate: dict[str, Any], *, loan_id: int, now_ts: int) -> dict[str, Any]:
    execute_quote = dict(selected["executeQuote"])
    execute_quote["rfqId"] = rfq_id(selected["rawQuote"]) or str(job.get("rfq", {}).get("rfqId", ""))
    execute_quote["quoteId"] = quote_id(selected["rawQuote"])
    execute_quote["maxFee"] = normalize_decimal_str(execute_quote["maxFee"])
    execute_quote["label"] = str(job.get("rfq", {}).get("request", {}).get("label", ""))
    return {
        "loanId": loan_id,
        "takerNonce": int(job["rfq"].get("submittedAt", now_ts) * 1_000_000 + loan_id % 1_000_000),
        "callStrike": int(mandate["minCallStrike"]),
        "putStrike": int(mandate["maxPutStrike"]),
        "expiry": int(mandate["maturity"]),
        "asset": ZERO_ADDRESS,
        "amount": 0,
        "socketMessageId": "0x" + ("00" * 32),
        "quoteHash": quote_hash(selected["rawQuote"]) or "0x" + ("00" * 32),
        "realizedC": int(selected["expectedC"]),
        "executeQuote": execute_quote,
    }


def _process_rfq_execute(
    runtime: Any,
    state: dict[str, Any],
    handled: list[dict[str, Any]],
    *,
    loan_id: int,
    loan_key: str,
    mandate: dict[str, Any],
    job: dict[str, Any],
    now_ts: int,
) -> tuple[int, int]:
    item = {"action": "RfqExecute", "loanId": str(loan_id), "guid": loan_key, "status": "dry-run"}
    sent = 0
    try:
        selected = job.get("selectedQuote")
        if not isinstance(selected, dict):
            raise RuntimeError("selected quote missing from RFQ job")
        trade = _build_execute_trade(job, selected, mandate, loan_id=loan_id, now_ts=now_ts)
        execute_meta = sign_and_submit_rfq_execute_quote(
            rpc_url=runtime.rpc_url,
            tsa_addr=runtime.tsa_addr,
            rfq_module=runtime.rfq_module,
            trade=trade,
            account=runtime.account,
            private_key=runtime.private_key,
            api_url=runtime.api_url,
            x_lyra_wallet=runtime.derive_wallet,
            broadcast=runtime.broadcast,
        )
        job["execution"] = {
            "completedAt": now_ts,
            "trade": trade,
            "deriveApi": execute_meta,
        }
        job["status"] = "executed"
        job["attempts"]["execute"] = int(job["attempts"].get("execute", 0)) + 1
        job["updatedAt"] = now_ts
        job["error"] = None
        save_keeper_state(runtime.state_file, state)
        item["status"] = "sent"
        item["deriveApi"] = execute_meta
        sent = 1
    except Exception as exc:
        job["status"] = "quote_selected"
        job["updatedAt"] = now_ts
        job["error"] = str(exc)
        save_keeper_state(runtime.state_file, state)
        item["status"] = f"error: {exc}"
    handled.append(item)
    return 1, sent


def _process_rfq_confirm(
    runtime: Any,
    state: dict[str, Any],
    handled: list[dict[str, Any]],
    *,
    loan_id: int,
    loan_key: str,
    job: dict[str, Any],
    now_ts: int,
) -> tuple[int, int]:
    item = {"action": "RfqConfirm", "loanId": str(loan_id), "guid": loan_key, "status": "dry-run"}
    sent = 0
    try:
        execution = job.get("execution")
        if not isinstance(execution, dict) or not isinstance(execution.get("trade"), dict):
            raise RuntimeError("RFQ execution state missing trade payload")
        trade_result = submit_rfq_trade_confirmation(
            rpc_url=runtime.rpc_url,
            receiver_addr=runtime.receiver_addr,
            trade=execution["trade"],
            lz_fee_buffer_bps=runtime.lz_fee_buffer_bps,
            broadcast=runtime.broadcast,
            account=runtime.account,
            private_key=runtime.private_key,
            from_addr=runtime.sender,
            unlocked=runtime.unlocked,
        )
        job["confirmation"] = trade_result
        job["status"] = "completed"
        job["attempts"]["confirm"] = int(job["attempts"].get("confirm", 0)) + 1
        job["updatedAt"] = now_ts
        job["error"] = None
        state["rfqTradesCompleted"][f"{loan_id}:{execution['trade']['takerNonce']}"] = {
            "completedAt": now_ts,
            "loanId": int(loan_id),
            "takerNonce": int(execution["trade"]["takerNonce"]),
            "tradeConfirmedTx": trade_result.get("tradeConfirmedTx"),
            "rfqJob": True,
        }
        save_keeper_state(runtime.state_file, state)
        item.update(trade_result)
        item["status"] = "sent"
        sent = 1
    except Exception as exc:
        job["status"] = "executed"
        job["updatedAt"] = now_ts
        job["error"] = str(exc)
        save_keeper_state(runtime.state_file, state)
        item["status"] = f"error: {exc}"
    handled.append(item)
    return 1, sent


def _process_single_rfq_job(
    runtime: Any,
    state: dict[str, Any],
    handled: list[dict[str, Any]],
    *,
    loan_id: int,
    loan_key: str,
    loan: dict[str, Any],
    now_ts: int,
) -> tuple[int, int]:
    mandate = _active_mandate_snapshot(loan)
    job = _prepare_rfq_job(
        runtime,
        state,
        loan_id=loan_id,
        loan=loan,
        mandate=mandate,
        job=state["rfqJobs"].get(loan_key),
        now_ts=now_ts,
    )
    if mandate is None or job is None:
        return 0, 0

    if job["status"] in {"ready_to_send", "waiting_state"}:
        return _process_rfq_send(runtime, state, handled, loan_id=loan_id, loan_key=loan_key, mandate=mandate, job=job, now_ts=now_ts)
    if job["status"] == "polling":
        return _process_rfq_poll(runtime, state, handled, loan_id=loan_id, loan_key=loan_key, job=job, now_ts=now_ts)
    if job["status"] == "quote_selected":
        return _process_rfq_execute(runtime, state, handled, loan_id=loan_id, loan_key=loan_key, mandate=mandate, job=job, now_ts=now_ts)
    if job["status"] == "executed":
        return _process_rfq_confirm(runtime, state, handled, loan_id=loan_id, loan_key=loan_key, job=job, now_ts=now_ts)
    return 0, 0


def process_rfq_jobs(runtime: Any, *, state: dict[str, Any], handled: list[dict[str, Any]], attempts_so_far: int) -> tuple[int, int]:
    attempts = 0
    sent = 0
    now_ts = int(time.time())

    tracked = state.get("rfqTrackedLoans", {})
    for loan_key in sorted(tracked.keys(), key=lambda value: int(value)):
        if attempts_so_far + attempts >= runtime.max_per_tick:
            break

        loan_id = int(loan_key)
        try:
            loan = _load_l2_loan(runtime, loan_id)
        except Exception as exc:
            _append_reconcile_error(handled, loan_id=loan_id, loan_key=loan_key, exc=exc)
            attempts += 1
            continue

        job_attempts, job_sent = _process_single_rfq_job(
            runtime,
            state,
            handled,
            loan_id=loan_id,
            loan_key=loan_key,
            loan=loan,
            now_ts=now_ts,
        )
        attempts += job_attempts
        sent += job_sent

    return attempts, sent
