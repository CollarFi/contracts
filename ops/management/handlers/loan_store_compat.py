from __future__ import annotations

from typing import Any


LOAN_STORE_LOAN_FIELDS: tuple[tuple[str, str], ...] = (
    ("borrower", "address"),
    ("borrowAmount", "uint256"),
    ("minCallStrike", "uint256"),
    ("maxPutStrike", "uint256"),
    ("minNetInterest", "uint256"),
    ("fixedInterest", "uint256"),
    ("maxRollLtv", "uint256"),
    ("strikeScale", "uint256"),
    ("maturity", "uint64"),
    ("deadline", "uint64"),
    ("collateralAsset", "address"),
    ("collateralAmount", "uint256"),
    ("depositExecuted", "bool"),
    ("tradeExecuted", "bool"),
    ("returnRequested", "bool"),
    ("rolloverPending", "bool"),
    ("rolloverMandateHash", "bytes32"),
    ("rolloverMinCallStrike", "uint256"),
    ("rolloverMaxPutStrike", "uint256"),
    ("rolloverMinNetInterest", "uint256"),
    ("rolloverFixedInterest", "uint256"),
    ("rolloverMaxRollLtv", "uint256"),
    ("rolloverStrikeScale", "uint256"),
    ("rolloverMaturity", "uint64"),
    ("rolloverDeadline", "uint64"),
    ("consumed", "bool"),
)

LOAN_STORE_GET_LOAN_RETURN_TUPLE = f"({','.join(field_type for _, field_type in LOAN_STORE_LOAN_FIELDS)})"
LOAN_STORE_GET_LOAN_CALL_SIGNATURE = f"getLoan(uint256)({LOAN_STORE_GET_LOAN_RETURN_TUPLE})"


def _strip_cast_units(value: str) -> str:
    return value.strip().split()[0]


def _decode_cast_value(field_type: str, raw_value: str) -> Any:
    value = _strip_cast_units(raw_value)
    if field_type.startswith("uint"):
        return int(value, 0)
    if field_type == "bool":
        normalized = value.lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
        raise RuntimeError(f"unexpected bool value: {raw_value}")
    return value


def parse_loan_store_loan(raw: str) -> dict[str, Any]:
    cleaned = raw.strip()
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = cleaned[1:-1]
    cleaned = cleaned.replace("[", " [")
    parts = [segment.strip() for segment in cleaned.split(",") if segment.strip()]
    if len(parts) != len(LOAN_STORE_LOAN_FIELDS):
        raise RuntimeError(f"unexpected loan tuple output: {raw}")

    return {
        field_name: _decode_cast_value(field_type, value)
        for (field_name, field_type), value in zip(LOAN_STORE_LOAN_FIELDS, parts, strict=True)
    }
