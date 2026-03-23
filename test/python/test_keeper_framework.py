from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ops.py_lib.keeper_logs import data_int, get_message_received_logs, order_logs, topic_hex, topic_int
from ops.py_lib.keeper_loop import resolve_scan_range, resolve_start_block, should_advance_cursor
from ops.py_lib.keeper_state import load_keeper_state, read_keeper_cursor, save_keeper_state, write_keeper_cursor


class KeeperStateTests(unittest.TestCase):
    def test_state_round_trip_and_cursor_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "keeper.json"

            state = load_keeper_state(state_path, 123)
            self.assertEqual(state, {"nextBlock": 123})
            self.assertEqual(read_keeper_cursor(state, 0), 123)

            write_keeper_cursor(state, 456)
            save_keeper_state(state_path, state)

            reloaded = load_keeper_state(state_path, 1)
            self.assertEqual(reloaded, {"nextBlock": 456})
            self.assertEqual(read_keeper_cursor(reloaded, 0), 456)


class KeeperLoopTests(unittest.TestCase):
    def test_resolve_start_block_backfills_only_without_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "keeper.json"

            start = resolve_start_block(
                state_file=state_path,
                start_block=0,
                backfill_blocks=50,
                latest_block=lambda: 300,
            )
            self.assertEqual(start, 250)

            state_path.write_text("{}", encoding="utf-8")
            existing = resolve_start_block(
                state_file=state_path,
                start_block=0,
                backfill_blocks=50,
                latest_block=lambda: 999,
            )
            self.assertEqual(existing, 0)

    def test_scan_range_and_advance_rules(self) -> None:
        self.assertIsNone(resolve_scan_range(11, 10))
        self.assertEqual(resolve_scan_range(11, 11), (11, 11))
        self.assertTrue(
            should_advance_cursor(
                broadcast=False,
                attempts=0,
                sent=0,
                advance_on_dry_run=True,
            )
        )
        self.assertFalse(
            should_advance_cursor(
                broadcast=False,
                attempts=0,
                sent=0,
                advance_on_dry_run=False,
            )
        )
        self.assertTrue(
            should_advance_cursor(
                broadcast=True,
                attempts=2,
                sent=2,
                advance_on_dry_run=False,
            )
        )
        self.assertFalse(
            should_advance_cursor(
                broadcast=True,
                attempts=2,
                sent=1,
                advance_on_dry_run=False,
            )
        )


class KeeperLogsTests(unittest.TestCase):
    def test_order_and_parse_helpers(self) -> None:
        logs = [
            {
                "blockNumber": "0x11",
                "transactionIndex": "0x2",
                "logIndex": "0x1",
                "transactionHash": "0xbb",
                "topics": ["0x0", "0xabc", "0x2"],
                "data": "0x5",
            },
            {
                "blockNumber": "0x10",
                "transactionIndex": "0x1",
                "logIndex": "0x3",
                "transactionHash": "0xaa",
                "topics": ["0x0", "0xdef", "0x1"],
                "data": "0x3",
            },
        ]

        ordered = order_logs(logs)
        self.assertEqual(topic_hex(ordered[0], 1), "0xdef")
        self.assertEqual(topic_int(ordered[0], 2), 1)
        self.assertEqual(data_int(ordered[0], default=-1), 3)

    def test_get_message_received_logs_sorts_cast_output(self) -> None:
        payload = json.dumps(
            [
                {
                    "blockNumber": "0x12",
                    "transactionIndex": "0x0",
                    "logIndex": "0x1",
                    "transactionHash": "0xbb",
                    "topics": ["0x0", "0xbbb", "0x2"],
                    "data": "0x5",
                },
                {
                    "blockNumber": "0x11",
                    "transactionIndex": "0x0",
                    "logIndex": "0x0",
                    "transactionHash": "0xaa",
                    "topics": ["0x0", "0xaaa", "0x1"],
                    "data": "0x3",
                },
            ]
        )

        with patch("ops.py_lib.keeper_logs.run", return_value=payload):
            logs = get_message_received_logs("http://rpc", "0x1234", 1, 2)

        self.assertEqual(topic_hex(logs[0], 1), "0xaaa")
        self.assertEqual(topic_hex(logs[1], 1), "0xbbb")
