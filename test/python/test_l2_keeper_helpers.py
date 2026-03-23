from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ops.management.handlers.l2_derive_client import submit_with_retries
from ops.management.handlers.l2_rfq_trade import enqueue_rfq_trades_from_file, ensure_rfq_trade_state
from ops.management.handlers.l2_tsa_actions import ACTION_DEPOSIT_INTENT, parse_pending_message


class PendingMessageParsingTests(unittest.TestCase):
    def test_parse_pending_message_accepts_tuple_line(self) -> None:
        raw = (
            "(0,7,0x1111111111111111111111111111111111111111,123,"
            "0x2222222222222222222222222222222222222222,55,"
            "0x3333333333333333333333333333333333333333333333333333333333333333,0,"
            "0x4444444444444444444444444444444444444444444444444444444444444444,88,0xdeadbeef)"
        )

        parsed = parse_pending_message(raw)

        self.assertEqual(parsed["action"], ACTION_DEPOSIT_INTENT)
        self.assertEqual(parsed["loanId"], 7)
        self.assertEqual(parsed["amount"], 123)
        self.assertEqual(parsed["subaccountId"], 55)
        self.assertEqual(parsed["takerNonce"], 88)
        self.assertEqual(parsed["data"], "0xdeadbeef")

    def test_parse_pending_message_accepts_multiline_cast_output(self) -> None:
        raw = "\n".join(
            [
                "0",
                "9",
                "0x1111111111111111111111111111111111111111",
                "456",
                "0x2222222222222222222222222222222222222222",
                "99",
                "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "1",
                "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "11",
                "0x01",
            ]
        )

        parsed = parse_pending_message(raw)

        self.assertEqual(parsed["loanId"], 9)
        self.assertEqual(parsed["socketMessageId"], "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        self.assertEqual(parsed["secondaryAmount"], 1)


class RfqTradeQueueTests(unittest.TestCase):
    def test_enqueue_rfq_trades_normalizes_and_dedupes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = {"rfqTradeQueue": [{"loanId": 10, "takerNonce": 2}], "rfqTradesCompleted": {"11:3": {}}}
            ensure_rfq_trade_state(state)
            path = Path(tmp) / "rfq.json"
            path.write_text(
                json.dumps(
                    {
                        "rfqTrades": [
                            {
                                "loan_id": 10,
                                "taker_nonce": 2,
                                "call_strike": 1,
                                "put_strike": 2,
                                "expiry": 3,
                            },
                            {
                                "loan_id": 11,
                                "taker_nonce": 3,
                                "call_strike": 4,
                                "put_strike": 5,
                                "expiry": 6,
                            },
                            {
                                "loan_id": 12,
                                "taker_nonce": 4,
                                "call_strike": 7,
                                "put_strike": 8,
                                "expiry": 9,
                                "execute_quote": {
                                    "rfq_id": "rfq-1",
                                    "quote_id": "quote-1",
                                    "subaccount_id": 77,
                                    "direction": "buy",
                                    "max_fee": "1.5000",
                                    "legs": [
                                        {
                                            "instrument_name": "ETH-TEST",
                                            "direction": "sell",
                                            "asset_address": "0x1234567890123456789012345678901234567890",
                                            "sub_id": 88,
                                            "price": "12.3400",
                                            "amount": "0.5000",
                                        }
                                    ],
                                },
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            summary = enqueue_rfq_trades_from_file(state, path)

            self.assertEqual(summary, {"added": 1, "skipped": 2})
            self.assertEqual(len(state["rfqTradeQueue"]), 2)
            trade = state["rfqTradeQueue"][1]
            self.assertEqual(trade["loanId"], 12)
            self.assertEqual(trade["executeQuote"]["maxFee"], "1.5")
            self.assertEqual(trade["executeQuote"]["legs"][0]["price"], "12.34")
            self.assertEqual(trade["executeQuote"]["legs"][0]["amount"], "0.5")


class RetryHelperTests(unittest.TestCase):
    def test_submit_with_retries_retries_signature_sync_errors(self) -> None:
        attempts = {"count": 0}

        def submitter() -> str:
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise RuntimeError("14014 signature invalid")
            return "ok"

        with patch("ops.management.handlers.l2_derive_client.time.sleep") as sleep_mock:
            result, used_attempts = submit_with_retries(
                submitter,
                attempts=4,
                initial_delay_seconds=1.0,
                max_delay_seconds=5.0,
            )

        self.assertEqual(result, "ok")
        self.assertEqual(used_attempts, 3)
        self.assertEqual(attempts["count"], 3)
        self.assertEqual(sleep_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
