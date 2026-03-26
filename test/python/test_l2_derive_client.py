from __future__ import annotations

import unittest
from unittest.mock import patch

from ops.management.handlers.l2_derive_client import submit_api_for_pending_message
from ops.management.handlers.l2_tsa_actions import ACTION_DEPOSIT_INTENT, ACTION_RETURN_REQUEST


class DeriveClientTests(unittest.TestCase):
    def _pending_message(self) -> dict[str, object]:
        return {
            "loanId": 1,
            "amount": 10**18,
            "asset": "0x1111111111111111111111111111111111111111",
            "subaccountId": 123,
        }

    def test_private_deposit_includes_is_atomic_signing(self) -> None:
        calls: list[tuple[str, dict[str, object], dict[str, object] | None]] = []

        def fake_http_post_json(url: str, body: dict[str, object], headers: dict[str, object] | None = None):
            calls.append((url, body, headers))
            if url.endswith("/public/deposit_debug"):
                return 200, {"result": {"typed_data_hash": "0xtypedhash"}}
            if url.endswith("/private/deposit"):
                return 200, {"id": "req-1", "result": {"status": "requested"}}
            raise AssertionError(f"unexpected url: {url}")

        with (
            patch("ops.management.handlers.l2_derive_client.fresh_action_nonce_and_expiry", return_value=(111, 222)),
            patch("ops.management.handlers.l2_derive_client._erc20_decimals", return_value=18),
            patch("ops.management.handlers.l2_derive_client.resolve_asset_name", return_value="ETH"),
            patch("ops.management.handlers.l2_derive_client.wallet_sign", side_effect=["0xactionsig", "0xauthsig"]),
            patch("ops.management.handlers.l2_derive_client.http_post_json", side_effect=fake_http_post_json),
        ):
            result = submit_api_for_pending_message(
                action_type=ACTION_DEPOSIT_INTENT,
                pending_message=self._pending_message(),
                tsa_addr="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                account="keeper",
                private_key="0x1234",
                api_url="https://api-demo.lyra.finance",
                x_lyra_wallet="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                fallback_asset_name="ETH",
                rpc_url="http://rpc",
            )

        self.assertEqual(result["apiId"], "req-1")
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0][0].endswith("/public/deposit_debug"))
        self.assertTrue(calls[1][0].endswith("/private/deposit"))
        self.assertEqual(calls[0][1]["is_atomic_signing"], True)
        self.assertEqual(calls[1][1]["is_atomic_signing"], True)

    def test_private_withdraw_includes_is_atomic_signing(self) -> None:
        calls: list[tuple[str, dict[str, object], dict[str, object] | None]] = []

        def fake_http_post_json(url: str, body: dict[str, object], headers: dict[str, object] | None = None):
            calls.append((url, body, headers))
            if url.endswith("/public/withdraw_debug"):
                return 200, {"result": {"typed_data_hash": "0xtypedhash"}}
            if url.endswith("/private/withdraw"):
                return 200, {"id": "req-2", "result": {"status": "requested"}}
            raise AssertionError(f"unexpected url: {url}")

        with (
            patch("ops.management.handlers.l2_derive_client.fresh_action_nonce_and_expiry", return_value=(333, 444)),
            patch("ops.management.handlers.l2_derive_client._erc20_decimals", return_value=18),
            patch("ops.management.handlers.l2_derive_client.resolve_asset_name", return_value="ETH"),
            patch("ops.management.handlers.l2_derive_client.wallet_sign", side_effect=["0xactionsig", "0xauthsig"]),
            patch("ops.management.handlers.l2_derive_client.http_post_json", side_effect=fake_http_post_json),
        ):
            result = submit_api_for_pending_message(
                action_type=ACTION_RETURN_REQUEST,
                pending_message=self._pending_message(),
                tsa_addr="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                account="keeper",
                private_key="0x1234",
                api_url="https://api-demo.lyra.finance",
                x_lyra_wallet="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                fallback_asset_name="ETH",
                rpc_url="http://rpc",
            )

        self.assertEqual(result["apiId"], "req-2")
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0][0].endswith("/public/withdraw_debug"))
        self.assertTrue(calls[1][0].endswith("/private/withdraw"))
        self.assertEqual(calls[0][1]["is_atomic_signing"], True)
        self.assertEqual(calls[1][1]["is_atomic_signing"], True)


if __name__ == "__main__":
    unittest.main()
