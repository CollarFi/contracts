from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ops.management.l1_keeper_handle_messages import (
    ACTION_DEPOSIT_CONFIRMED,
    ACTION_TRADE_CONFIRMED,
    L1KeeperRuntime,
    _latest_unconsumed_pairs,
    run_keeper_tick,
)


def _message_log(
    *,
    guid: str,
    loan_id: int,
    action: int,
    block_number: int,
    log_index: int,
    tx_index: int = 0,
) -> dict[str, object]:
    return {
        "blockNumber": hex(block_number),
        "transactionIndex": hex(tx_index),
        "logIndex": hex(log_index),
        "transactionHash": f"0x{block_number:064x}",
        "topics": ["0x0", guid, hex(loan_id)],
        "data": hex(action),
    }


class L1KeeperHelpersTests(unittest.TestCase):
    def test_latest_unconsumed_pairs_keeps_latest_per_action(self) -> None:
        logs = [
            _message_log(guid="0xaaa", loan_id=7, action=ACTION_DEPOSIT_CONFIRMED, block_number=12, log_index=1),
            _message_log(guid="0xbbb", loan_id=7, action=ACTION_TRADE_CONFIRMED, block_number=12, log_index=2),
            _message_log(guid="0xccc", loan_id=7, action=ACTION_DEPOSIT_CONFIRMED, block_number=13, log_index=0),
        ]

        with patch("ops.management.l1_keeper_handle_messages._guid_consumed", side_effect=lambda _rpc, _vault, guid: guid == "0xaaa"):
            pairs = _latest_unconsumed_pairs(logs, rpc_url="http://rpc", vault_addr="0xvault")

        self.assertEqual(
            pairs,
            {
                7: {
                    ACTION_DEPOSIT_CONFIRMED: "0xccc",
                    ACTION_TRADE_CONFIRMED: "0xbbb",
                }
            },
        )


class L1KeeperTickTests(unittest.TestCase):
    def test_dry_run_waiting_pair_advances_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "keeper_l1_state.json"
            runtime = L1KeeperRuntime(
                rpc_url="http://l1",
                logs_url="http://logs",
                messenger_addr="0x1111111111111111111111111111111111111111",
                vault_addr="0x2222222222222222222222222222222222222222",
                state_file=state_file,
                max_per_tick=10,
                broadcast=False,
            )
            state = {"nextBlock": 100}
            handled: list[dict[str, object]] = []
            logs = [
                _message_log(
                    guid="0x" + "ab" * 32,
                    loan_id=42,
                    action=ACTION_TRADE_CONFIRMED,
                    block_number=105,
                    log_index=0,
                )
            ]

            with (
                patch("ops.management.l1_keeper_handle_messages._block_number", return_value=105),
                patch("ops.management.l1_keeper_handle_messages.get_message_received_logs", return_value=logs),
                patch("ops.management.l1_keeper_handle_messages._guid_consumed", return_value=False),
                patch("ops.management.l1_keeper_handle_messages._has_pending_deposit", return_value=True),
                patch("ops.management.l1_keeper_handle_messages._has_mandate", return_value=True),
            ):
                result, next_block = run_keeper_tick(runtime, state=state, next_block=100, handled=handled)

            self.assertEqual(result["attempted"], 0)
            self.assertEqual(result["logs"], 1)
            self.assertTrue(result["advancedCursor"])
            self.assertEqual(next_block, 106)
            self.assertEqual(state["nextBlock"], 106)
            self.assertEqual(handled[0]["status"], "waiting-pair")
            self.assertTrue(state_file.is_file())

    def test_broadcast_error_keeps_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "keeper_l1_state.json"
            runtime = L1KeeperRuntime(
                rpc_url="http://l1",
                logs_url="http://logs",
                messenger_addr="0x1111111111111111111111111111111111111111",
                vault_addr="0x2222222222222222222222222222222222222222",
                state_file=state_file,
                max_per_tick=10,
                broadcast=True,
                private_key="0x1234",
            )
            state = {"nextBlock": 200}
            handled: list[dict[str, object]] = []
            logs = [
                _message_log(
                    guid="0x" + "11" * 32,
                    loan_id=9,
                    action=ACTION_DEPOSIT_CONFIRMED,
                    block_number=205,
                    log_index=0,
                ),
                _message_log(
                    guid="0x" + "22" * 32,
                    loan_id=9,
                    action=ACTION_TRADE_CONFIRMED,
                    block_number=205,
                    log_index=1,
                ),
            ]

            with (
                patch("ops.management.l1_keeper_handle_messages._block_number", return_value=205),
                patch("ops.management.l1_keeper_handle_messages.get_message_received_logs", return_value=logs),
                patch("ops.management.l1_keeper_handle_messages._guid_consumed", return_value=False),
                patch("ops.management.l1_keeper_handle_messages._has_pending_deposit", return_value=True),
                patch("ops.management.l1_keeper_handle_messages._has_mandate", return_value=True),
                patch("ops.management.l1_keeper_handle_messages.cast_send", side_effect=RuntimeError("boom")),
            ):
                result, next_block = run_keeper_tick(runtime, state=state, next_block=200, handled=handled)

            self.assertEqual(result["attempted"], 1)
            self.assertEqual(result["sent"], 0)
            self.assertFalse(result["advancedCursor"])
            self.assertEqual(next_block, 200)
            self.assertEqual(state["nextBlock"], 200)
            self.assertFalse(state_file.exists())
            self.assertIn("error: boom", str(handled[0]["status"]))

    def test_broadcast_success_sends_finalize_and_advances_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "keeper_l1_state.json"
            runtime = L1KeeperRuntime(
                rpc_url="http://l1",
                logs_url="http://logs",
                messenger_addr="0x1111111111111111111111111111111111111111",
                vault_addr="0x2222222222222222222222222222222222222222",
                state_file=state_file,
                max_per_tick=10,
                broadcast=True,
                account="keeper",
            )
            state = {"nextBlock": 300}
            handled: list[dict[str, object]] = []
            logs = [
                _message_log(
                    guid="0x" + "33" * 32,
                    loan_id=88,
                    action=ACTION_DEPOSIT_CONFIRMED,
                    block_number=301,
                    log_index=0,
                ),
                _message_log(
                    guid="0x" + "44" * 32,
                    loan_id=88,
                    action=ACTION_TRADE_CONFIRMED,
                    block_number=301,
                    log_index=1,
                ),
            ]

            with (
                patch("ops.management.l1_keeper_handle_messages._block_number", return_value=301),
                patch("ops.management.l1_keeper_handle_messages.get_message_received_logs", return_value=logs),
                patch("ops.management.l1_keeper_handle_messages._guid_consumed", return_value=False),
                patch("ops.management.l1_keeper_handle_messages._has_pending_deposit", return_value=True),
                patch("ops.management.l1_keeper_handle_messages._has_mandate", return_value=True),
                patch("ops.management.l1_keeper_handle_messages.cast_send", return_value="0xtx"),
            ):
                result, next_block = run_keeper_tick(runtime, state=state, next_block=300, handled=handled)

            self.assertEqual(result["attempted"], 1)
            self.assertEqual(result["sent"], 1)
            self.assertTrue(result["advancedCursor"])
            self.assertEqual(next_block, 302)
            self.assertEqual(state["nextBlock"], 302)
            self.assertTrue(state_file.is_file())
            self.assertEqual(handled[0]["status"], "sent")
            self.assertEqual(handled[0]["tx"], "0xtx")
            self.assertEqual(state["finalizedLoans"]["88"]["tx"], "0xtx")
