#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, getcontext
from typing import Any

from management.handlers.l2_derive_client import normalize_decimal_str, sign_private_api_auth, to_decimal_str
from management.l2_common import http_post_json

getcontext().prec = 80


def _post_private(
    *,
    api_url: str,
    path: str,
    body: dict[str, Any],
    x_lyra_wallet: str,
    account: str,
    private_key: str,
) -> dict[str, Any]:
    ts_ms, auth_sig = sign_private_api_auth(account=account, private_key=private_key)
    status, out = http_post_json(
        f"{api_url.rstrip('/')}{path}",
        body,
        headers={
            "X-LyraWallet": x_lyra_wallet,
            "X-LyraTimestamp": ts_ms,
            "X-LyraSignature": auth_sig,
        },
    )
    if status >= 400 or out.get("error"):
        raise RuntimeError(f"{path} failed ({status}): {json.dumps(out)}")
    return out


def _post_public(*, api_url: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    status, out = http_post_json(f"{api_url.rstrip('/')}{path}", body)
    if status >= 400 or out.get("error"):
        raise RuntimeError(f"{path} failed ({status}): {json.dumps(out)}")
    return out


def send_rfq(
    *,
    api_url: str,
    x_lyra_wallet: str,
    account: str,
    private_key: str,
    subaccount_id: int,
    direction: str,
    max_fee: str,
    label: str,
    legs: list[dict[str, str]],
) -> dict[str, Any]:
    body = {
        "subaccount_id": int(subaccount_id),
        "direction": direction,
        "max_fee": normalize_decimal_str(max_fee),
        "label": label,
        "legs": [
            {
                "instrument_name": str(leg["instrument_name"]),
                "direction": str(leg["direction"]),
                "amount": normalize_decimal_str(leg["amount"]),
            }
            for leg in legs
        ],
    }
    return _post_private(
        api_url=api_url,
        path="/private/send_rfq",
        body=body,
        x_lyra_wallet=x_lyra_wallet,
        account=account,
        private_key=private_key,
    )


def poll_quotes(
    *,
    api_url: str,
    x_lyra_wallet: str,
    account: str,
    private_key: str,
    subaccount_id: int,
) -> list[dict[str, Any]]:
    out = _post_private(
        api_url=api_url,
        path="/private/poll_quotes",
        body={
            "subaccount_id": int(subaccount_id),
            "status": "open",
        },
        x_lyra_wallet=x_lyra_wallet,
        account=account,
        private_key=private_key,
    )
    result = out.get("result")
    if not isinstance(result, dict):
        return []
    quotes = result.get("quotes")
    return quotes if isinstance(quotes, list) else []


def cancel_rfq(
    *,
    api_url: str,
    x_lyra_wallet: str,
    account: str,
    private_key: str,
    subaccount_id: int,
    rfq_id: str,
) -> dict[str, Any]:
    return _post_private(
        api_url=api_url,
        path="/private/cancel_rfq",
        body={
            "subaccount_id": int(subaccount_id),
            "rfq_id": rfq_id,
        },
        x_lyra_wallet=x_lyra_wallet,
        account=account,
        private_key=private_key,
    )


def get_option_instruments(*, api_url: str, currency: str) -> list[dict[str, Any]]:
    out = _post_public(
        api_url=api_url,
        path="/public/get_instruments",
        body={
            "currency": currency,
            "instrument_type": "option",
            "expired": False,
        },
    )
    result = out.get("result")
    return result if isinstance(result, list) else []


def format_option_strike(strike_1e18: int) -> str:
    rendered = normalize_decimal_str(Decimal(str(strike_1e18)) / (Decimal(10) ** 18))
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def format_option_expiry(expiry: int) -> str:
    return datetime.fromtimestamp(int(expiry), tz=timezone.utc).strftime("%Y%m%d")


def instrument_key(*, expiry: int, strike_1e18: int, option_type: str) -> tuple[int, str, str]:
    return int(expiry), format_option_strike(int(strike_1e18)), option_type.upper()


def build_instrument_lookup(instruments: list[dict[str, Any]]) -> dict[tuple[int, str, str], dict[str, Any]]:
    lookup: dict[tuple[int, str, str], dict[str, Any]] = {}
    for instrument in instruments:
        option_details = instrument.get("option_details")
        if not isinstance(option_details, dict):
            continue
        expiry = option_details.get("expiry")
        strike = option_details.get("strike")
        option_type = option_details.get("option_type")
        if expiry is None or strike is None or option_type is None:
            continue
        try:
            key = (int(expiry), normalize_decimal_str(str(strike)), str(option_type).upper())
        except Exception:
            continue
        lookup[key] = instrument
    return lookup


def decimal_from_1e18_int(value: int) -> Decimal:
    return Decimal(str(value)) / (Decimal(10) ** 18)


def decimal_to_1e18_int(value: Decimal) -> int:
    return int(value * (Decimal(10) ** 18))


def quote_max_fee_decimal(raw_quote: dict[str, Any]) -> str:
    for key in ("max_fee", "maxFee"):
        value = raw_quote.get(key)
        if value is not None:
            return normalize_decimal_str(value)
    return "0"


def quote_id(raw_quote: dict[str, Any]) -> str:
    for key in ("quote_id", "quoteId"):
        value = raw_quote.get(key)
        if value is not None:
            return str(value)
    return ""


def rfq_id(raw_quote: dict[str, Any]) -> str:
    for key in ("rfq_id", "rfqId"):
        value = raw_quote.get(key)
        if value is not None:
            return str(value)
    return ""


def quote_hash(raw_quote: dict[str, Any]) -> str:
    for key in ("quote_hash", "quoteHash", "hash"):
        value = raw_quote.get(key)
        if value is not None:
            return str(value)
    return ""


def subaccount_id(raw_quote: dict[str, Any], fallback: int) -> int:
    for key in ("subaccount_id", "subaccountId"):
        value = raw_quote.get(key)
        if value is not None:
            return int(value)
    return int(fallback)


def leg_amount_decimal(raw_leg: dict[str, Any]) -> Decimal:
    value = raw_leg.get("amount")
    return Decimal(normalize_decimal_str(value))


def amount_to_decimal_str(amount_1e18: int) -> str:
    return to_decimal_str(int(amount_1e18), 18)
