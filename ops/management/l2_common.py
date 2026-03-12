#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from lz_harness.common import cast_call, run

MAX_LOAN_ID_FOR_NONCE_SUFFIX = 999_999


def extract_tx_hash(cast_send_output: str) -> str:
    # cast send formats vary by version:
    # - "Transaction: 0x..."
    # - key/value lines with "transactionHash      0x..."
    # - JSON with "transactionHash":"0x..."
    for pattern in (
        r'"transactionHash"\s*:\s*"(0x[a-fA-F0-9]{64})"',
        r"\btransactionHash\b\s*[:=]?\s*(0x[a-fA-F0-9]{64})",
        r"\bTransaction:\s*(0x[a-fA-F0-9]{64})",
    ):
        m = re.search(pattern, cast_send_output, flags=re.IGNORECASE)
        if m:
            return m.group(1)

    raise ValueError(f"failed to extract tx hash from cast output: {cast_send_output}")


def get_receipt(rpc_url: str, tx_hash: str) -> dict[str, Any]:
    raw = run(["cast", "rpc", "eth_getTransactionReceipt", tx_hash, "--rpc-url", rpc_url])
    payload = json.loads(raw)
    if isinstance(payload, dict) and "result" in payload:
        return payload.get("result") or {}
    if isinstance(payload, dict) and "transactionHash" in payload:
        return payload
    if isinstance(payload, dict):
        return payload
    return {}


def latest_block_timestamp(rpc_url: str) -> int:
    out = run(["cast", "block", "latest", "--rpc-url", rpc_url, "--json"])
    payload = json.loads(out)
    ts = payload.get("timestamp", 0)
    if isinstance(ts, str):
        return int(ts, 16) if ts.startswith("0x") else int(ts)
    return int(ts)


def tsa_signature_expiry_window(rpc_url: str, tsa_addr: str) -> tuple[int, int]:
    raw = cast_call(
        rpc_url,
        tsa_addr,
        "getCollarTSAParams()((uint256,uint256,uint256,uint256,int256,uint256,uint256,uint256))",
    )

    # cast may annotate large integers with scientific notation hints, e.g.
    #   86400 [8.64e4]
    # Strip those hints, then parse first two tuple fields.
    cleaned = re.sub(r"\s*\[[^\]]+\]", "", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    m = re.match(r"^\(\s*(-?\d+)\s*,\s*(-?\d+)\s*,", cleaned)
    if not m:
        raise RuntimeError(f"failed to parse signature expiry window from TSA params: {raw}")
    return int(m.group(1)), int(m.group(2))


def derive_reissue_nonce(chain_now: int, loan_id: int) -> int:
    if loan_id > MAX_LOAN_ID_FOR_NONCE_SUFFIX:
        raise RuntimeError(f"loanId too large for nonce suffix (max={MAX_LOAN_ID_FOR_NONCE_SUFFIX}, got={loan_id})")

    # Mirror receiver nonce convention:
    # nonce = timestamp_sec || loanId(6 digits)
    return (chain_now * 1_000_000) + loan_id


def wallet_sign(message: str, *, no_hash: bool, account: str, private_key: str) -> str:
    cmd = ["cast", "wallet", "sign"]
    if no_hash:
        cmd.append("--no-hash")
    if private_key:
        cmd += ["--private-key", private_key]
    elif account:
        cmd += ["--account", account]
    else:
        raise ValueError("wallet signing requires account or private key")
    cmd.append(message)
    return run(cmd)


def wallet_address(*, account: str, private_key: str) -> str:
    if private_key:
        return run(["cast", "wallet", "address", "--private-key", private_key]).strip()
    if account:
        return run(["cast", "wallet", "address", "--account", account]).strip()
    raise ValueError("wallet address resolution requires account or private key")


def assert_tsa_signer(rpc_url: str, tsa_addr: str, wallet_addr: str) -> None:
    raw = cast_call(rpc_url, tsa_addr, "isSigner(address)(bool)", wallet_addr, allow_fail=True)
    if raw.strip().lower() != "true":
        raise ValueError(
            f"wallet {wallet_addr} is not a TSA signer for {tsa_addr}; "
            "Derive API signature will be rejected"
        )


def http_post_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout_seconds: float = 30,
) -> tuple[int, dict[str, Any]]:
    # Cloudflare blocks Python's default urllib user-agent for this API.
    all_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "curl/8.5.0",
    }
    if headers:
        all_headers.update(headers)

    body = json.dumps(payload).encode("utf-8")
    req = Request(url, data=body, headers=all_headers, method="POST")
    try:
        with urlopen(req, timeout=timeout_seconds) as resp:
            status = int(resp.status)
            data = json.loads(resp.read().decode("utf-8") or "{}")
            return status, data
    except HTTPError as e:
        try:
            data = json.loads((e.read() or b"{}").decode("utf-8"))
        except Exception:
            data = {"error": {"message": str(e)}}
        return int(e.code), data
    except URLError as e:
        raise RuntimeError(f"http post failed: {url}: {e}") from e


def is_retryable_signature_sync_error(status: int, payload: dict[str, Any]) -> bool:
    err = payload.get("error")
    if not isinstance(err, dict):
        return False

    code = err.get("code")
    text = f"{err.get('message', '')} {err.get('data', '')}".lower()
    return bool(code == 14014 or str(code) == "14014" or "signature invalid" in text)


def is_retryable_signature_sync_error_text(err_text: str) -> bool:
    t = err_text.lower()
    return "14014" in t or "signature invalid" in t
