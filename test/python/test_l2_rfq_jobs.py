from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ops"))
from ops.management.handlers.l2_rfq_jobs import _select_best_quote, process_rfq_jobs


class RfqQuoteSelectionTests(unittest.TestCase):
    def test_selects_highest_expected_total_quote(self) -> None:
        job = {
            "subaccountId": 144167,
            "optionAsset": "0xBcB494059969DAaB460E0B5d4f5c2366aab79aa1",
            "mandate": {
                "fixedInterest": 50_000_000_000_000_000,
                "minNetInterest": 100_000_000_000_000_000,
            },
            "rfq": {
                "rfqId": "rfq-1",
                "request": {
                    "direction": "buy",
                    "legs": [
                        {"instrument_name": "ETH-20260403-3200-C", "direction": "sell", "amount": "1"},
                        {"instrument_name": "ETH-20260403-2800-P", "direction": "buy", "amount": "1"},
                    ],
                },
            },
        }
        quote_a = {
            "rfq_id": "rfq-1",
            "quote_id": "q1",
            "subaccount_id": 144167,
            "direction": "sell",
            "max_fee": "0.01",
            "legs": [
                {
                    "instrument_name": "ETH-20260403-3200-C",
                    "direction": "sell",
                    "asset_address": job["optionAsset"],
                    "sub_id": 1,
                    "price": "0.20",
                    "amount": "1",
                },
                {
                    "instrument_name": "ETH-20260403-2800-P",
                    "direction": "buy",
                    "asset_address": job["optionAsset"],
                    "sub_id": 2,
                    "price": "0.10",
                    "amount": "1",
                },
            ],
        }
        quote_b = {
            "rfq_id": "rfq-1",
            "quote_id": "q2",
            "subaccount_id": 144167,
            "direction": "sell",
            "max_fee": "0.015",
            "legs": [
                {
                    "instrument_name": "ETH-20260403-3200-C",
                    "direction": "sell",
                    "asset_address": job["optionAsset"],
                    "sub_id": 1,
                    "price": "0.24",
                    "amount": "1",
                },
                {
                    "instrument_name": "ETH-20260403-2800-P",
                    "direction": "buy",
                    "asset_address": job["optionAsset"],
                    "sub_id": 2,
                    "price": "0.08",
                    "amount": "1",
                },
            ],
        }

        selected, records = _select_best_quote(job, [quote_a, quote_b])

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected["executeQuote"]["quoteId"], "q2")
        self.assertGreater(int(selected["expectedTotal"]), 0)
        self.assertEqual(set(records.keys()), {"q1", "q2"})
        self.assertTrue(records["q1"]["validation"]["economicsOk"])
        self.assertTrue(records["q2"]["validation"]["economicsOk"])


class RfqJobRetryTests(unittest.TestCase):
    def test_send_failure_stays_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = SimpleNamespace(
                rpc_url="http://l2",
                api_url="https://api-demo.lyra.finance",
                derive_wallet="0x1111111111111111111111111111111111111111",
                account="keeper",
                private_key="0x1234",
                broadcast=True,
                wrapped_deposit_asset="0x2222222222222222222222222222222222222222",
                option_asset="0x3333333333333333333333333333333333333333",
                loan_store_addr="0x4444444444444444444444444444444444444444",
                subaccount_id=144167,
                max_per_tick=10,
                state_file=Path(tmp) / "keeper_l2_state.json",
                rfq_module="0x5555555555555555555555555555555555555555",
                receiver_addr="0x6666666666666666666666666666666666666666",
                lz_fee_buffer_bps=500,
                sender="",
                unlocked=False,
            )
            state = {
                "rfqJobs": {},
                "rfqTrackedLoans": {"1": {"loanId": 1}},
                "rfqTradesCompleted": {},
            }
            handled: list[dict[str, object]] = []
            loan = {
                "borrower": "0x7777777777777777777777777777777777777777",
                "borrowAmount": 1,
                "minCallStrike": 3200 * 10**18,
                "maxPutStrike": 2800 * 10**18,
                "minNetInterest": 100_000_000_000_000_000,
                "fixedInterest": 50_000_000_000_000_000,
                "maturity": 1_800_000_000,
                "deadline": 1_900_000_000,
                "collateralAsset": "0x8888888888888888888888888888888888888888",
                "collateralAmount": 10**18,
                "depositExecuted": True,
                "tradeExecuted": False,
                "returnRequested": False,
                "rolloverPending": False,
            }

            with (
                patch("ops.management.handlers.l2_rfq_jobs._load_l2_loan", return_value=loan),
                patch(
                    "ops.management.handlers.l2_rfq_jobs._requested_rfq_payload",
                    return_value={
                        "direction": "buy",
                        "maxFee": "0.2",
                        "label": "label",
                        "legs": [{"instrument_name": "A", "direction": "sell", "amount": "1"}],
                    },
                ),
                patch("ops.management.handlers.l2_rfq_jobs.send_rfq", side_effect=RuntimeError("boom")),
            ):
                attempts, sent = process_rfq_jobs(runtime, state=state, handled=handled, attempts_so_far=0)

            self.assertEqual(attempts, 1)
            self.assertEqual(sent, 0)
            self.assertEqual(state["rfqJobs"]["1"]["status"], "ready_to_send")
            self.assertIn("boom", state["rfqJobs"]["1"]["error"])
            self.assertIn("error: boom", str(handled[-1]["status"]))


if __name__ == "__main__":
    unittest.main()
