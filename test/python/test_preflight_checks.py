from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ops import preflight
from ops.py_lib import preflight_checks
from ops.py_lib.lz import encode_lz_receive_option


class PreflightChecksTests(unittest.TestCase):
    def _write_env(self, path: Path, values: dict[str, str]) -> None:
        lines = [f"{key}={value}" for key, value in values.items()]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_recipient_check_matches_receiver(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            l1_env = root / ".env.l1.testnet"
            l2_env = root / ".env.l2.testnet"
            vault = "0x1111111111111111111111111111111111111111"
            receiver = "0x2222222222222222222222222222222222222222"
            self._write_env(l1_env, {"RPC_URL": "http://l1", "L1_VAULT": vault})
            self._write_env(l2_env, {"RPC_URL": "http://l2", "L2_RECEIVER": receiver})

            with patch("ops.py_lib.preflight_checks.cast_call", return_value=f"{receiver} [address]"):
                out = preflight_checks.recipient_check(l1_env, l2_env)

        self.assertTrue(out["ok"])
        self.assertEqual(out["vault"], vault)
        self.assertEqual(out["l1Recipient"], receiver)
        self.assertEqual(out["l2Receiver"], receiver)

    def test_peer_check_uses_endpoint_fallback_eids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            l1_env = root / ".env.l1.testnet"
            l2_env = root / ".env.l2.testnet"
            l1_messenger = "0x1111111111111111111111111111111111111111"
            l2_receiver = "0x2222222222222222222222222222222222222222"
            l1_endpoint = "0x3333333333333333333333333333333333333333"
            l2_endpoint = "0x4444444444444444444444444444444444444444"
            self._write_env(
                l1_env,
                {"RPC_URL": "http://l1", "L1_MESSENGER": l1_messenger, "LZ_ENDPOINT": l1_endpoint},
            )
            self._write_env(
                l2_env,
                {"RPC_URL": "http://l2", "L2_RECEIVER": l2_receiver, "LZ_ENDPOINT": l2_endpoint},
            )

            expected_l1_peer = preflight_checks.addr_to_bytes32(l2_receiver)
            expected_l2_peer = preflight_checks.addr_to_bytes32(l1_messenger)
            responses = {
                ("http://l2", l2_endpoint, "eid()(uint32)", (), True): "40231",
                ("http://l1", l1_endpoint, "eid()(uint32)", (), True): "40161",
                ("http://l1", l1_messenger, "peers(uint32)(bytes32)", ("40231",), True): expected_l1_peer,
                ("http://l2", l2_receiver, "peers(uint32)(bytes32)", ("40161",), True): expected_l2_peer,
            }

            def fake_cast_call(rpc_url: str, to: str, sig: str, *args: str, allow_fail: bool = False) -> str:
                return responses[(rpc_url, to, sig, args, allow_fail)]

            with patch("ops.py_lib.preflight_checks.cast_call", side_effect=fake_cast_call):
                out = preflight_checks.peer_check(l1_env, l2_env)

        self.assertTrue(out["ok"])
        self.assertEqual(out["l1ToL2Eid"], 40231)
        self.assertEqual(out["l2ToL1Eid"], 40161)
        self.assertEqual(out["issues"], [])

    def test_vault_recipient_check_matches_l1_vault(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            l1_env = root / ".env.l1.testnet"
            l2_env = root / ".env.l2.testnet"
            vault = "0x1111111111111111111111111111111111111111"
            receiver = "0x2222222222222222222222222222222222222222"
            self._write_env(l1_env, {"RPC_URL": "http://l1", "L1_VAULT": vault})
            self._write_env(l2_env, {"RPC_URL": "http://l2", "L2_RECEIVER": receiver})

            with patch("ops.py_lib.preflight_checks.cast_call", return_value=f"{vault} [address]"):
                out = preflight_checks.vault_recipient_check(l1_env, l2_env)

        self.assertTrue(out["ok"])
        self.assertEqual(out["receiver"], receiver)
        self.assertEqual(out["actualVaultRecipient"], vault)
        self.assertEqual(out["expectedVaultRecipient"], vault)

    def test_asset_mapping_check_reports_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            l1_env = root / ".env.l1.testnet"
            l2_env = root / ".env.l2.testnet"
            vault = "0x1111111111111111111111111111111111111111"
            receiver = "0x2222222222222222222222222222222222222222"
            tsa = "0x3333333333333333333333333333333333333333"
            wrapped = "0x4444444444444444444444444444444444444444"
            asset = "0x5555555555555555555555555555555555555555"
            self._write_env(l1_env, {"RPC_URL": "http://l1", "L1_VAULT": vault, "WETH_ASSET": asset})
            self._write_env(l2_env, {"RPC_URL": "http://l2", "L2_RECEIVER": receiver})

            responses = {
                ("http://l1", vault, "l2MessageAsset(address)(address)", (asset,), True): asset,
                ("http://l2", receiver, "tsa()(address)", (), True): tsa,
                (
                    "http://l2",
                    tsa,
                    "getBaseTSAAddresses()(address,address,address,address,address,address,address)",
                    (),
                    True,
                ): "\n".join(
                    [
                        "0x0000000000000000000000000000000000000001",
                        "0x0000000000000000000000000000000000000002",
                        wrapped,
                        "0x0000000000000000000000000000000000000004",
                        "0x0000000000000000000000000000000000000005",
                        "0x0000000000000000000000000000000000000006",
                        "0x0000000000000000000000000000000000000007",
                    ]
                ),
                ("http://l2", wrapped, "wrappedAsset()(address)", (), True): asset,
            }

            def fake_cast_call(rpc_url: str, to: str, sig: str, *args: str, allow_fail: bool = False) -> str:
                return responses[(rpc_url, to, sig, args, allow_fail)]

            with patch("ops.py_lib.preflight_checks.cast_call", side_effect=fake_cast_call):
                out = preflight_checks.asset_mapping_check(l1_env, l2_env)

        self.assertTrue(out["ok"])
        self.assertEqual(out["vault"], vault)
        self.assertEqual(out["receiver"], receiver)
        self.assertEqual(out["tsaWrappedUnderlyingAsset"], asset)

    def test_parse_pending_message_supports_tuple_and_multiline_forms(self) -> None:
        guid = "0x" + "ab" * 32
        quote_hash = "0x" + "cd" * 32
        raw_tuple = (
            "(1, 2, 0x1111111111111111111111111111111111111111, 3, "
            "0x2222222222222222222222222222222222222222, 4, "
            f"{guid}, 5, {quote_hash}, 6, 0x1234)"
        )
        tuple_msg = preflight_checks.parse_pending_message(raw_tuple)
        self.assertEqual(tuple_msg["loanId"], 2)
        self.assertEqual(tuple_msg["socketMessageId"], guid)

        raw_multiline = "\n".join(
            [
                "1",
                "2",
                "0x1111111111111111111111111111111111111111",
                "3",
                "0x2222222222222222222222222222222222222222",
                "4",
                guid,
                "5",
                quote_hash,
                "6",
                "0x1234",
            ]
        )
        multiline_msg = preflight_checks.parse_pending_message(raw_multiline)
        self.assertEqual(multiline_msg, tuple_msg)

    def test_l2_message_preflight_returns_expected_json_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            l2_env = root / ".env.l2.testnet"
            receiver = "0x1111111111111111111111111111111111111111"
            socket = "0x2222222222222222222222222222222222222222"
            tsa = "0x3333333333333333333333333333333333333333"
            wrapped = "0x4444444444444444444444444444444444444444"
            asset = "0x5555555555555555555555555555555555555555"
            guid = "0x" + "aa" * 32
            self._write_env(l2_env, {"RPC_URL": "http://l2", "L2_RECEIVER": receiver})

            def fake_run(cmd: list[str]) -> str:
                if cmd[:2] == ["cast", "block-number"]:
                    return "100"
                if cmd[:2] == ["cast", "code"]:
                    return "0x6000"
                raise AssertionError(f"unexpected run call: {cmd}")

            pending_raw = (
                "(1, 7, 0x5555555555555555555555555555555555555555, 10, "
                "0x6666666666666666666666666666666666666666, 77, "
                f"0x{'0' * 64}, 0, 0x{'1' * 64}, 9, 0x)"
            )
            responses = {
                ("http://l2", receiver, "socket()(address)", (), False): socket,
                ("http://l2", receiver, "tsa()(address)", (), False): tsa,
                ("http://l2", tsa, "subAccount()(uint256)", (), False): "77",
                (
                    "http://l2",
                    tsa,
                    "getBaseTSAAddresses()(address,address,address,address,address,address,address)",
                    (),
                    True,
                ): "\n".join(
                    [
                        "0x0000000000000000000000000000000000000001",
                        "0x0000000000000000000000000000000000000002",
                        wrapped,
                        "0x0000000000000000000000000000000000000004",
                        "0x0000000000000000000000000000000000000005",
                        "0x0000000000000000000000000000000000000006",
                        "0x0000000000000000000000000000000000000007",
                    ]
                ),
                ("http://l2", wrapped, "wrappedAsset()(address)", (), True): asset,
                ("http://l2", receiver, "handledMessages(bytes32)(bool)", (guid,), True): "false",
                (
                    "http://l2",
                    receiver,
                    "pendingMessages(bytes32)(uint8,uint256,address,uint256,address,uint256,bytes32,uint256,bytes32,uint256,bytes)",
                    (guid,),
                    True,
                ): pending_raw,
                ("http://l2", asset, "balanceOf(address)(uint256)", (receiver,), True): "10",
            }

            def fake_cast_call(rpc_url: str, to: str, sig: str, *args: str, allow_fail: bool = False) -> str:
                return responses[(rpc_url, to, sig, args, allow_fail)]

            with patch("ops.py_lib.preflight_checks.run", side_effect=fake_run), patch(
                "ops.py_lib.preflight_checks.cast_call", side_effect=fake_cast_call
            ):
                out = preflight_checks.l2_message_preflight(l2_env, guid=[guid], lookback_blocks=10)

        self.assertEqual(out["receiver"], receiver)
        self.assertEqual(out["inspected"], 1)
        self.assertEqual(out["results"][0]["guid"], guid)
        self.assertTrue(out["results"][0]["ok"])

    def test_snapshot_uln_side_builds_successful_checks(self) -> None:
        oapp = "0x1111111111111111111111111111111111111111"
        endpoint = "0x2222222222222222222222222222222222222222"
        send_lib = "0x3333333333333333333333333333333333333333"
        recv_lib = "0x4444444444444444444444444444444444444444"
        executor = "0x5555555555555555555555555555555555555555"
        remote_eid = "40231"
        source_eid = "40161"
        expected_peer = preflight_checks.addr_to_bytes32("0x6666666666666666666666666666666666666666")
        expected_options = encode_lz_receive_option(250000, 0)
        send_cfg_1 = "0x" + f"{1024:064x}" + ("0" * 24) + executor[2:]
        responses = {
            ("http://rpc", oapp, "peers(uint32)(bytes32)", (remote_eid,), True): expected_peer,
            ("http://rpc", endpoint, "delegates(address)(address)", (oapp,), True): executor,
            ("http://rpc", oapp, "remoteEid()(uint32)", (), True): remote_eid,
            ("http://rpc", oapp, "defaultOptions()(bytes)", (), True): expected_options,
            ("http://rpc", endpoint, "getSendLibrary(address,uint32)(address)", (oapp, remote_eid), True): send_lib,
            (
                "http://rpc",
                endpoint,
                "getReceiveLibrary(address,uint32)(address,bool)",
                (oapp, source_eid),
                True,
            ): f"{recv_lib}\nfalse",
            (
                "http://rpc",
                endpoint,
                "getConfig(address,address,uint32,uint32)(bytes)",
                (oapp, send_lib, remote_eid, "1"),
                True,
            ): send_cfg_1,
            (
                "http://rpc",
                endpoint,
                "getConfig(address,address,uint32,uint32)(bytes)",
                (oapp, send_lib, remote_eid, "2"),
                True,
            ): "0x1234",
            (
                "http://rpc",
                endpoint,
                "getConfig(address,address,uint32,uint32)(bytes)",
                (oapp, recv_lib, source_eid, "2"),
                True,
            ): "0x5678",
            (
                "http://rpc",
                endpoint,
                "receiveLibraryTimeout(address,uint32)(address,uint256)",
                (oapp, source_eid),
                True,
            ): f"{recv_lib}\n0",
        }

        def fake_cast_call(rpc_url: str, to: str, sig: str, *args: str, allow_fail: bool = False) -> str:
            return responses[(rpc_url, to, sig, args, allow_fail)]

        with patch("ops.py_lib.preflight_checks.cast_call", side_effect=fake_cast_call):
            out = preflight_checks.snapshot_uln_side(
                label="L1 messenger",
                rpc_url="http://rpc",
                endpoint=endpoint,
                oapp=oapp,
                remote_eid=remote_eid,
                source_eid=source_eid,
                expected_peer_b32=expected_peer,
                expected_remote_eid=remote_eid,
                expected_default_options=expected_options,
            )

        self.assertTrue(out["ok"])
        self.assertEqual(out["sendExecutor"], executor)
        self.assertEqual(out["sendMaxMessageSize"], 1024)
        self.assertTrue(all(check["ok"] for check in out["checks"]))

    def test_preflight_all_uses_direct_library_checks(self) -> None:
        with patch("ops.preflight.load_env", return_value={"RPC_URL": "http://l1", "ACCOUNT": "ops", "WETH_ASSET": "0xaaa"}), patch(
            "ops.preflight.recipient_check",
            return_value={
                "ok": False,
                "vault": "0x1111111111111111111111111111111111111111",
                "l1Recipient": "0x0",
                "l2Receiver": "0x2222222222222222222222222222222222222222",
            },
        ), patch(
            "ops.preflight.vault_recipient_check",
            return_value={
                "ok": False,
                "receiver": "0x2222222222222222222222222222222222222222",
                "actualVaultRecipient": "0x0000000000000000000000000000000000000000",
                "expectedVaultRecipient": "0x1111111111111111111111111111111111111111",
            },
        ) as recipient_mock, patch(
            "ops.preflight.peer_check",
            return_value={"ok": True},
        ) as peer_mock, patch(
            "ops.preflight.asset_mapping_check",
            return_value={"ok": True, "l1Asset": "0xaaa", "tsaWrappedUnderlyingAsset": "0xbbb"},
        ) as asset_mock, patch(
            "ops.preflight.uln_route_check",
            return_value={"ok": True, "env": "testnet"},
        ) as uln_mock, patch(
            "ops.preflight.l2_message_preflight",
            return_value={"results": [{"ok": True}]},
        ) as messages_mock:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                preflight.all_checks(
                    l1_env_file=Path(".env.l1.testnet"),
                    l2_env_file=Path(".env.l2.testnet"),
                    env_profile="",
                    include_messages=True,
                    include_uln=True,
                    lookback_blocks=123,
                    json_out=True,
                )

        out = json.loads(buffer.getvalue())
        self.assertFalse(out["ok"])
        recipient_mock.assert_called_once_with(Path(".env.l1.testnet"), Path(".env.l2.testnet"), env_profile="testnet")
        # load_env is called for both L1 and L2 envs in all_checks
        peer_mock.assert_called_once_with(Path(".env.l1.testnet"), Path(".env.l2.testnet"), env_profile="testnet")
        asset_mock.assert_called_once_with(Path(".env.l1.testnet"), Path(".env.l2.testnet"), env_profile="testnet")
        uln_mock.assert_called_once_with(Path(".env.l1.testnet"), Path(".env.l2.testnet"), env_profile="testnet")
        messages_mock.assert_called_once_with(Path(".env.l2.testnet"), env_profile="testnet", lookback_blocks=123)
        self.assertIn("setL2Recipient(address)", out["recommendations"][0])
        self.assertTrue(any("setVaultRecipient(address)" in rec for rec in out["recommendations"]))
