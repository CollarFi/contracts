from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ops.management.handlers.l2_tsa_actions import ACTION_DEPOSIT_INTENT, ACTION_RETURN_REQUEST
from ops.management.l2_keeper_handle_messages import L2KeeperRuntime, run_keeper_tick


def _message_log(*, guid: str, loan_id: int, action: int, block_number: int, log_index: int = 0) -> dict[str, object]:
    return {
        "blockNumber": hex(block_number),
        "transactionIndex": hex(0),
        "logIndex": hex(log_index),
        "transactionHash": f"0x{block_number:064x}",
        "topics": ["0x0", guid, hex(loan_id)],
        "data": hex(action),
    }


def _pending_message_raw(*, loan_id: int) -> str:
    return (
        "(0,"
        f"{loan_id},"
        "0x1111111111111111111111111111111111111111,"
        "123,"
        "0x2222222222222222222222222222222222222222,"
        "55,"
        "0x3333333333333333333333333333333333333333333333333333333333333333,"
        "0,"
        "0x4444444444444444444444444444444444444444444444444444444444444444,"
        "88,"
        "0xdeadbeef)"
    )


class L2KeeperTickTests(unittest.TestCase):
    def _runtime(self, *, state_file: Path, signer: Mock | None) -> L2KeeperRuntime:
        return L2KeeperRuntime(
            rpc_url="http://l2",
            receiver_addr="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            tsa_addr="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            matching_addr="0xcccccccccccccccccccccccccccccccccccccccc",
            atomic_executor_addr="0xdddddddddddddddddddddddddddddddddddddddd",
            deposit_module="0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
            withdrawal_module="0xffffffffffffffffffffffffffffffffffffffff",
            rfq_module="0x9999999999999999999999999999999999999999",
            wrapped_deposit_asset="0x8888888888888888888888888888888888888888",
            state_file=state_file,
            max_per_tick=10,
            broadcast=True,
            lz_fee_buffer_bps=500,
            local_atomic_submit=False,
            submit_deposit_api=True,
            submit_withdraw_api=False,
            api_url="https://api-demo.lyra.finance",
            derive_wallet="0x7777777777777777777777777777777777777777",
            derive_asset_name="ETH",
            api_retry_attempts=1,
            api_retry_initial_delay_seconds=0.0,
            api_retry_max_delay_seconds=0.0,
            allowed_actions={ACTION_DEPOSIT_INTENT},
            signer=signer,
            account="keeper",
            private_key="0x1234",
            sender="",
            unlocked=False,
        )

    def _local_atomic_runtime(self, *, state_file: Path, signer: Mock | None) -> L2KeeperRuntime:
        runtime = self._runtime(state_file=state_file, signer=signer)
        return L2KeeperRuntime(
            rpc_url=runtime.rpc_url,
            receiver_addr=runtime.receiver_addr,
            tsa_addr=runtime.tsa_addr,
            matching_addr=runtime.matching_addr,
            atomic_executor_addr=runtime.atomic_executor_addr,
            deposit_module=runtime.deposit_module,
            withdrawal_module=runtime.withdrawal_module,
            rfq_module=runtime.rfq_module,
            wrapped_deposit_asset=runtime.wrapped_deposit_asset,
            state_file=runtime.state_file,
            max_per_tick=runtime.max_per_tick,
            broadcast=runtime.broadcast,
            lz_fee_buffer_bps=runtime.lz_fee_buffer_bps,
            local_atomic_submit=True,
            submit_deposit_api=False,
            submit_withdraw_api=False,
            api_url=runtime.api_url,
            derive_wallet=runtime.derive_wallet,
            derive_asset_name=runtime.derive_asset_name,
            api_retry_attempts=runtime.api_retry_attempts,
            api_retry_initial_delay_seconds=runtime.api_retry_initial_delay_seconds,
            api_retry_max_delay_seconds=runtime.api_retry_max_delay_seconds,
            allowed_actions={ACTION_RETURN_REQUEST},
            signer=runtime.signer,
            account=runtime.account,
            private_key=runtime.private_key,
            sender=runtime.sender,
            unlocked=runtime.unlocked,
        )

    def _withdraw_runtime(self, *, state_file: Path, signer: Mock | None) -> L2KeeperRuntime:
        runtime = self._runtime(state_file=state_file, signer=signer)
        return L2KeeperRuntime(
            rpc_url=runtime.rpc_url,
            receiver_addr=runtime.receiver_addr,
            tsa_addr=runtime.tsa_addr,
            matching_addr=runtime.matching_addr,
            atomic_executor_addr=runtime.atomic_executor_addr,
            deposit_module=runtime.deposit_module,
            withdrawal_module=runtime.withdrawal_module,
            rfq_module=runtime.rfq_module,
            wrapped_deposit_asset=runtime.wrapped_deposit_asset,
            state_file=runtime.state_file,
            max_per_tick=runtime.max_per_tick,
            broadcast=runtime.broadcast,
            lz_fee_buffer_bps=runtime.lz_fee_buffer_bps,
            local_atomic_submit=False,
            submit_deposit_api=False,
            submit_withdraw_api=True,
            api_url=runtime.api_url,
            derive_wallet=runtime.derive_wallet,
            derive_asset_name=runtime.derive_asset_name,
            api_retry_attempts=runtime.api_retry_attempts,
            api_retry_initial_delay_seconds=runtime.api_retry_initial_delay_seconds,
            api_retry_max_delay_seconds=runtime.api_retry_max_delay_seconds,
            allowed_actions={ACTION_RETURN_REQUEST},
            signer=runtime.signer,
            account=runtime.account,
            private_key=runtime.private_key,
            sender=runtime.sender,
            unlocked=runtime.unlocked,
        )

    def test_api_error_after_handle_message_keeps_cursor_on_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "keeper_l2_state.json"
            signer = Mock()
            signer.send_contract_tx.return_value = "0xtx"
            runtime = self._runtime(state_file=state_file, signer=signer)
            guid = "0x" + ("11" * 32)
            logs = [_message_log(guid=guid, loan_id=7, action=ACTION_DEPOSIT_INTENT, block_number=205)]
            state = {
                "nextBlock": 200,
                "apiSubmitted": {},
                "rfqTradeQueue": [],
                "rfqTradesCompleted": {},
            }
            handled: list[dict[str, object]] = []
            api_error = RuntimeError('private/deposit failed (403): {"error": {"message": "HTTP Error 403: Forbidden"}}')

            with (
                patch("ops.management.l2_keeper_handle_messages._block_number", return_value=205),
                patch("ops.management.l2_keeper_handle_messages.get_message_received_logs", return_value=logs),
                patch(
                    "management.handlers.l2_pending_message.cast_call",
                    side_effect=["false", _pending_message_raw(loan_id=7), "false"],
                ),
                patch("management.handlers.l2_pending_message.submit_api_for_pending_message", side_effect=api_error),
            ):
                result, next_block = run_keeper_tick(
                    runtime,
                    state=state,
                    next_block=200,
                    handled=handled,
                    rfq_trade_file=None,
                )

            self.assertEqual(result["attempted"], 1)
            self.assertEqual(result["sent"], 0)
            self.assertFalse(result["advancedCursor"])
            self.assertEqual(next_block, 200)
            self.assertEqual(state["nextBlock"], 200)
            self.assertIn("private/deposit failed (403)", str(handled[-1]["status"]))
            signer.send_contract_tx.assert_called_once()

            retry_signer = Mock()
            retry_runtime = self._runtime(state_file=state_file, signer=retry_signer)
            retry_handled: list[dict[str, object]] = []
            with (
                patch("ops.management.l2_keeper_handle_messages._block_number", return_value=205),
                patch("ops.management.l2_keeper_handle_messages.get_message_received_logs", return_value=logs),
                patch(
                    "management.handlers.l2_pending_message.cast_call",
                    side_effect=["true", _pending_message_raw(loan_id=7), "false"],
                ),
                patch("management.handlers.l2_pending_message.submit_api_for_pending_message", side_effect=api_error),
            ):
                retry_result, retry_next_block = run_keeper_tick(
                    retry_runtime,
                    state=state,
                    next_block=200,
                    handled=retry_handled,
                    rfq_trade_file=None,
                )

            self.assertEqual(retry_result["attempted"], 1)
            self.assertEqual(retry_result["sent"], 0)
            self.assertFalse(retry_result["advancedCursor"])
            self.assertEqual(retry_next_block, 200)
            self.assertEqual(state["nextBlock"], 200)
            self.assertIn("private/deposit failed (403)", str(retry_handled[-1]["status"]))
            retry_signer.send_contract_tx.assert_not_called()
            self.assertFalse(state_file.exists())

    def test_already_handled_pending_api_submit_retries_and_advances_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "keeper_l2_state.json"
            signer = Mock()
            runtime = self._runtime(state_file=state_file, signer=signer)
            guid = "0x" + ("22" * 32)
            logs = [_message_log(guid=guid, loan_id=9, action=ACTION_DEPOSIT_INTENT, block_number=305)]
            state = {
                "nextBlock": 300,
                "apiSubmitted": {},
                "rfqTradeQueue": [],
                "rfqTradesCompleted": {},
            }
            handled: list[dict[str, object]] = []
            api_meta = {"apiId": "req-1", "nonce": "77"}

            with (
                patch("ops.management.l2_keeper_handle_messages._block_number", return_value=305),
                patch("ops.management.l2_keeper_handle_messages.get_message_received_logs", return_value=logs),
                patch(
                    "management.handlers.l2_pending_message.cast_call",
                    side_effect=["true", _pending_message_raw(loan_id=9), "false"],
                ),
                patch("management.handlers.l2_pending_message.submit_api_for_pending_message", return_value=api_meta),
            ):
                result, next_block = run_keeper_tick(
                    runtime,
                    state=state,
                    next_block=300,
                    handled=handled,
                    rfq_trade_file=None,
                )

            self.assertEqual(result["attempted"], 1)
            self.assertEqual(result["sent"], 1)
            self.assertTrue(result["advancedCursor"])
            self.assertEqual(next_block, 306)
            self.assertEqual(state["nextBlock"], 306)
            self.assertEqual(handled[-1]["status"], "sent")
            self.assertEqual(handled[-1]["deriveApi"], api_meta)
            self.assertEqual(state["apiSubmitted"][guid]["deriveApi"], api_meta)
            self.assertEqual(state["messageTxs"][guid]["apiId"], "req-1")
            signer.send_contract_tx.assert_not_called()
            self.assertTrue(state_file.is_file())

    def test_already_executed_onchain_skips_api_and_sends_deposit_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "keeper_l2_state.json"
            signer = Mock()
            signer.send_tx.return_value = "0xack"
            runtime = self._runtime(state_file=state_file, signer=signer)
            guid = "0x" + ("33" * 32)
            logs = [_message_log(guid=guid, loan_id=11, action=ACTION_DEPOSIT_INTENT, block_number=405)]
            state = {
                "nextBlock": 400,
                "apiSubmitted": {},
                "rfqTradeQueue": [],
                "rfqTradesCompleted": {},
            }
            handled: list[dict[str, object]] = []

            with (
                patch("ops.management.l2_keeper_handle_messages._block_number", return_value=405),
                patch("ops.management.l2_keeper_handle_messages.get_message_received_logs", return_value=logs),
                patch(
                    "management.handlers.l2_pending_message.cast_call",
                    side_effect=["true", _pending_message_raw(loan_id=11), "true", "false"],
                ),
                patch("management.handlers.l2_pending_message.quote_ack_native_fee", return_value=100),
                patch("management.handlers.l2_pending_message.submit_api_for_pending_message") as submit_api_mock,
            ):
                result, next_block = run_keeper_tick(
                    runtime,
                    state=state,
                    next_block=400,
                    handled=handled,
                    rfq_trade_file=None,
                )

            self.assertEqual(result["attempted"], 1)
            self.assertEqual(result["sent"], 1)
            self.assertTrue(result["advancedCursor"])
            self.assertEqual(next_block, 406)
            self.assertEqual(handled[-1]["status"], "sent")
            self.assertEqual(handled[-1]["deriveApi"]["status"], "alreadyExecutedOnchain")
            self.assertEqual(handled[-1]["depositConfirmedTx"], "0xack")
            self.assertEqual(state["apiSubmitted"][guid]["deriveApi"]["status"], "alreadyExecutedOnchain")
            self.assertEqual(state["messageTxs"][guid]["depositConfirmedTx"], "0xack")
            submit_api_mock.assert_not_called()
            signer.send_tx.assert_called_once()

    def test_local_atomic_replay_skips_when_return_request_already_executed_onchain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "keeper_l2_state.json"
            signer = Mock()
            runtime = self._local_atomic_runtime(state_file=state_file, signer=signer)
            guid = "0x" + ("44" * 32)
            logs = [_message_log(guid=guid, loan_id=13, action=ACTION_RETURN_REQUEST, block_number=505)]
            state = {
                "nextBlock": 500,
                "apiSubmitted": {},
                "rfqTradeQueue": [],
                "rfqTradesCompleted": {},
            }
            handled: list[dict[str, object]] = []

            with (
                patch("ops.management.l2_keeper_handle_messages._block_number", return_value=505),
                patch("ops.management.l2_keeper_handle_messages.get_message_received_logs", return_value=logs),
                patch(
                    "management.handlers.l2_pending_message.cast_call",
                    side_effect=["true", "true"],
                ),
            ):
                result, next_block = run_keeper_tick(
                    runtime,
                    state=state,
                    next_block=500,
                    handled=handled,
                    rfq_trade_file=None,
                )

            self.assertEqual(result["attempted"], 0)
            self.assertEqual(result["sent"], 0)
            self.assertTrue(result["advancedCursor"])
            self.assertEqual(next_block, 506)
            self.assertEqual(handled, [])
            signer.send_contract_tx.assert_not_called()

    def test_return_request_api_success_bridges_and_notifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "keeper_l2_state.json"
            signer = Mock()
            signer.send_contract_tx.return_value = "0xhandle"
            signer.send_tx.return_value = "0xreturn"
            runtime = self._withdraw_runtime(state_file=state_file, signer=signer)
            guid = "0x" + ("55" * 32)
            logs = [_message_log(guid=guid, loan_id=17, action=ACTION_RETURN_REQUEST, block_number=605)]
            state = {
                "nextBlock": 600,
                "apiSubmitted": {},
                "rfqTradeQueue": [],
                "rfqTradesCompleted": {},
            }
            handled: list[dict[str, object]] = []
            api_meta = {"apiId": "req-17", "nonce": "99"}

            with (
                patch("ops.management.l2_keeper_handle_messages._block_number", return_value=605),
                patch("ops.management.l2_keeper_handle_messages.get_message_received_logs", return_value=logs),
                patch(
                    "management.handlers.l2_pending_message.cast_call",
                    side_effect=["false", _pending_message_raw(loan_id=17), "false", "false"],
                ),
                patch("management.handlers.l2_pending_message.submit_api_for_pending_message", return_value=api_meta),
                patch("management.handlers.l2_pending_message._quote_collateral_return_native_fee", return_value=123),
            ):
                result, next_block = run_keeper_tick(
                    runtime,
                    state=state,
                    next_block=600,
                    handled=handled,
                    rfq_trade_file=None,
                )

            self.assertEqual(result["attempted"], 1)
            self.assertEqual(result["sent"], 1)
            self.assertTrue(result["advancedCursor"])
            self.assertEqual(next_block, 606)
            self.assertEqual(handled[-1]["status"], "sent")
            self.assertEqual(handled[-1]["collateralReturnedTx"], "0xreturn")
            self.assertEqual(state["messageTxs"][guid]["collateralReturnedTx"], "0xreturn")
            signer.send_tx.assert_called_once()

    def test_return_request_already_executed_onchain_bridges_and_notifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "keeper_l2_state.json"
            signer = Mock()
            signer.send_tx.return_value = "0xreturn"
            runtime = self._withdraw_runtime(state_file=state_file, signer=signer)
            guid = "0x" + ("66" * 32)
            logs = [_message_log(guid=guid, loan_id=19, action=ACTION_RETURN_REQUEST, block_number=705)]
            state = {
                "nextBlock": 700,
                "apiSubmitted": {},
                "rfqTradeQueue": [],
                "rfqTradesCompleted": {},
            }
            handled: list[dict[str, object]] = []

            with (
                patch("ops.management.l2_keeper_handle_messages._block_number", return_value=705),
                patch("ops.management.l2_keeper_handle_messages.get_message_received_logs", return_value=logs),
                patch(
                    "management.handlers.l2_pending_message.cast_call",
                    side_effect=["true", _pending_message_raw(loan_id=19), "true", "false"],
                ),
                patch("management.handlers.l2_pending_message._quote_collateral_return_native_fee", return_value=123),
                patch("management.handlers.l2_pending_message.submit_api_for_pending_message") as submit_api_mock,
            ):
                result, next_block = run_keeper_tick(
                    runtime,
                    state=state,
                    next_block=700,
                    handled=handled,
                    rfq_trade_file=None,
                )

            self.assertEqual(result["attempted"], 1)
            self.assertEqual(result["sent"], 1)
            self.assertTrue(result["advancedCursor"])
            self.assertEqual(next_block, 706)
            self.assertEqual(handled[-1]["deriveApi"]["status"], "alreadyExecutedOnchain")
            self.assertEqual(handled[-1]["collateralReturnedTx"], "0xreturn")
            self.assertEqual(state["messageTxs"][guid]["collateralReturnedTx"], "0xreturn")
            submit_api_mock.assert_not_called()
            signer.send_tx.assert_called_once()


if __name__ == "__main__":
    unittest.main()
