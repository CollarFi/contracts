from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ops.management.l1_keeper_handle_messages import (
    ACTION_COLLATERAL_RETURNED,
    ACTION_DEPOSIT_CONFIRMED,
    ACTION_TRADE_CONFIRMED,
    L1KeeperRuntime,
    _latest_unconsumed_pairs,
    run_keeper_tick,
)
from py_lib.keeper_logs import LogRangeNotReadyError
from py_lib.keeper_signer import PendingTxTimeoutError


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
            _message_log(guid="0xddd", loan_id=7, action=ACTION_COLLATERAL_RETURNED, block_number=14, log_index=0),
        ]

        with patch("ops.management.l1_keeper_handle_messages._guid_consumed", side_effect=lambda _rpc, _vault, guid: guid == "0xaaa"):
            pairs = _latest_unconsumed_pairs(logs, rpc_url="http://rpc", vault_addr="0xvault")

        self.assertEqual(
            pairs,
            {
                7: {
                    ACTION_DEPOSIT_CONFIRMED: "0xccc",
                    ACTION_TRADE_CONFIRMED: "0xbbb",
                    ACTION_COLLATERAL_RETURNED: "0xddd",
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
                patch("ops.management.l1_keeper_handle_messages._return_requested", return_value=False),
            ):
                result, next_block = run_keeper_tick(runtime, state=state, next_block=100, handled=handled)

            self.assertEqual(result["attempted"], 0)
            self.assertEqual(result["logs"], 1)
            self.assertTrue(result["advancedCursor"])
            self.assertEqual(next_block, 106)
            self.assertEqual(state["nextBlock"], 106)
            self.assertEqual(handled[0]["status"], "waiting-pair")
            self.assertTrue(state_file.is_file())

    def test_broadcast_success_sends_finalize_and_advances_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "keeper_l1_state.json"
            signer = Mock()
            signer.send_contract_tx.return_value = "0xtx"
            runtime = L1KeeperRuntime(
                rpc_url="http://l1",
                logs_url="http://logs",
                messenger_addr="0x1111111111111111111111111111111111111111",
                vault_addr="0x2222222222222222222222222222222222222222",
                state_file=state_file,
                max_per_tick=10,
                broadcast=True,
                signer=signer,
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
                patch("ops.management.l1_keeper_handle_messages._return_requested", return_value=False),
            ):
                result, next_block = run_keeper_tick(runtime, state=state, next_block=300, handled=handled)

            self.assertEqual(result["attempted"], 1)
            self.assertEqual(result["sent"], 1)
            self.assertTrue(result["advancedCursor"])
            self.assertEqual(next_block, 302)
            self.assertEqual(state["nextBlock"], 302)
            self.assertEqual(handled[0]["status"], "sent")
            self.assertEqual(handled[0]["tx"], "0xtx")
            self.assertEqual(state["finalizedLoans"]["88"]["tx"], "0xtx")
            signer.send_contract_tx.assert_called_once()

    def test_broadcast_success_sends_finalize_deposit_return_and_advances_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "keeper_l1_state.json"
            signer = Mock()
            signer.send_contract_tx.return_value = "0xreturntx"
            runtime = L1KeeperRuntime(
                rpc_url="http://l1",
                logs_url="http://logs",
                messenger_addr="0x1111111111111111111111111111111111111111",
                vault_addr="0x2222222222222222222222222222222222222222",
                state_file=state_file,
                max_per_tick=10,
                broadcast=True,
                signer=signer,
            )
            state = {"nextBlock": 400}
            handled: list[dict[str, object]] = []
            guid = "0x" + "55" * 32
            logs = [
                _message_log(
                    guid=guid,
                    loan_id=1,
                    action=ACTION_COLLATERAL_RETURNED,
                    block_number=401,
                    log_index=0,
                )
            ]

            with (
                patch("ops.management.l1_keeper_handle_messages._block_number", return_value=401),
                patch("ops.management.l1_keeper_handle_messages.get_message_received_logs", return_value=logs),
                patch("ops.management.l1_keeper_handle_messages._guid_consumed", return_value=False),
                patch("ops.management.l1_keeper_handle_messages._has_pending_deposit", return_value=True),
                patch("ops.management.l1_keeper_handle_messages._has_mandate", return_value=True),
                patch("ops.management.l1_keeper_handle_messages._return_requested", return_value=True),
            ):
                result, next_block = run_keeper_tick(runtime, state=state, next_block=400, handled=handled)

            self.assertEqual(result["attempted"], 1)
            self.assertEqual(result["sent"], 1)
            self.assertTrue(result["advancedCursor"])
            self.assertEqual(next_block, 402)
            self.assertEqual(handled[0]["action"], "finalizeDepositReturn")
            self.assertEqual(handled[0]["collateralReturnedGuid"], guid)
            self.assertEqual(handled[0]["tx"], "0xreturntx")
            self.assertEqual(state["returnedDeposits"]["1"]["tx"], "0xreturntx")
            signer.send_contract_tx.assert_called_once_with(
                contract_name="CollarVault",
                address="0x2222222222222222222222222222222222222222",
                fn_name="finalizeDepositReturn",
                args=[1, guid],
                label="L1 keeper finalizeDepositReturn",
            )

    def test_broadcast_receipt_timeout_tracks_pending_return_tx_and_advances_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "keeper_l1_state.json"
            signer = Mock()
            signer.send_contract_tx.side_effect = PendingTxTimeoutError(
                label="L1 keeper finalizeDepositReturn",
                tx_hash="0xpending",
                nonce=77,
                timeout_seconds=120,
            )
            runtime = L1KeeperRuntime(
                rpc_url="http://l1",
                logs_url="http://logs",
                messenger_addr="0x1111111111111111111111111111111111111111",
                vault_addr="0x2222222222222222222222222222222222222222",
                state_file=state_file,
                max_per_tick=10,
                broadcast=True,
                signer=signer,
            )
            state = {"nextBlock": 500}
            handled: list[dict[str, object]] = []
            guid = "0x" + "66" * 32
            logs = [
                _message_log(
                    guid=guid,
                    loan_id=2,
                    action=ACTION_COLLATERAL_RETURNED,
                    block_number=501,
                    log_index=0,
                )
            ]

            with (
                patch("ops.management.l1_keeper_handle_messages._block_number", return_value=501),
                patch("ops.management.l1_keeper_handle_messages.get_message_received_logs", return_value=logs),
                patch("ops.management.l1_keeper_handle_messages._guid_consumed", return_value=False),
                patch("ops.management.l1_keeper_handle_messages._has_pending_deposit", return_value=True),
                patch("ops.management.l1_keeper_handle_messages._has_mandate", return_value=True),
                patch("ops.management.l1_keeper_handle_messages._return_requested", return_value=True),
            ):
                result, next_block = run_keeper_tick(runtime, state=state, next_block=500, handled=handled)

            self.assertEqual(result["attempted"], 1)
            self.assertEqual(result["sent"], 1)
            self.assertTrue(result["advancedCursor"])
            self.assertEqual(next_block, 502)
            self.assertEqual(handled[0]["status"], "pending-tx")
            self.assertEqual(handled[0]["tx"], "0xpending")
            self.assertIn("return:2:", next(iter(state["pendingTxs"].keys())))
            self.assertEqual(state["pendingTxs"][next(iter(state["pendingTxs"].keys()))]["tx"], "0xpending")

    def test_completed_pending_tx_is_promoted_to_returned_deposit_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "keeper_l1_state.json"
            signer = Mock()
            signer.try_get_receipt.return_value = {"status": 1}
            runtime = L1KeeperRuntime(
                rpc_url="http://l1",
                logs_url="http://logs",
                messenger_addr="0x1111111111111111111111111111111111111111",
                vault_addr="0x2222222222222222222222222222222222222222",
                state_file=state_file,
                max_per_tick=10,
                broadcast=True,
                signer=signer,
            )
            guid = "0x" + "77" * 32
            state = {
                "nextBlock": 900,
                "pendingTxs": {
                    f"return:2:{guid.lower()}": {
                        "action": "finalizeDepositReturn",
                        "loanId": "2",
                        "collateralReturnedGuid": guid,
                        "tx": "0xsettled",
                        "submittedAt": 1,
                    }
                },
            }
            handled: list[dict[str, object]] = []

            with patch("ops.management.l1_keeper_handle_messages._block_number", return_value=899):
                result, next_block = run_keeper_tick(runtime, state=state, next_block=900, handled=handled)

            self.assertEqual(result["pending"], 0)
            self.assertEqual(next_block, 900)
            self.assertEqual(state["returnedDeposits"]["2"]["tx"], "0xsettled")
            self.assertEqual(state["returnedDeposits"]["2"]["collateralReturnedGuid"], guid)
            self.assertEqual(state["pendingTxs"], {})

    def test_head_lag_does_not_advance_cursor_or_crash(self) -> None:
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
                signer=Mock(),
            )
            state = {"nextBlock": 600}
            handled: list[dict[str, object]] = []

            with (
                patch("ops.management.l1_keeper_handle_messages._block_number", return_value=600),
                patch(
                    "ops.management.l1_keeper_handle_messages.get_message_received_logs",
                    side_effect=LogRangeNotReadyError(from_block=600, to_block=600),
                ),
            ):
                result, next_block = run_keeper_tick(runtime, state=state, next_block=600, handled=handled)

            self.assertTrue(result["headLag"])
            self.assertFalse(result["advancedCursor"])
            self.assertEqual(result["attempted"], 0)
            self.assertEqual(result["logs"], 0)
            self.assertEqual(next_block, 600)
            self.assertEqual(state["nextBlock"], 600)

    def test_stale_partial_pair_without_pending_deposit_is_ignored(self) -> None:
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
            state = {"nextBlock": 700}
            handled: list[dict[str, object]] = []
            logs = [
                _message_log(
                    guid="0x" + "88" * 32,
                    loan_id=2,
                    action=ACTION_DEPOSIT_CONFIRMED,
                    block_number=701,
                    log_index=0,
                )
            ]

            with (
                patch("ops.management.l1_keeper_handle_messages._block_number", return_value=701),
                patch("ops.management.l1_keeper_handle_messages.get_message_received_logs", return_value=logs),
                patch("ops.management.l1_keeper_handle_messages._guid_consumed", return_value=False),
                patch("ops.management.l1_keeper_handle_messages._has_pending_deposit", return_value=False),
                patch("ops.management.l1_keeper_handle_messages._has_mandate", return_value=False),
                patch("ops.management.l1_keeper_handle_messages._return_requested", return_value=False),
            ):
                result, next_block = run_keeper_tick(runtime, state=state, next_block=700, handled=handled)

            self.assertEqual(result["attempted"], 0)
            self.assertEqual(result["logs"], 1)
            self.assertEqual(next_block, 702)
            self.assertEqual(handled, [])
