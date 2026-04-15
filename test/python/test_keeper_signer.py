from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from hexbytes import HexBytes
from web3.exceptions import TimeExhausted

from py_lib.keeper_signer import KeeperSigner, PendingTxTimeoutError


class KeeperSignerNonceTests(unittest.TestCase):
    def _build_signer(self) -> tuple[KeeperSigner, Mock]:
        resolved = Mock()
        resolved.kind = "unlocked"
        resolved.address = "0x1111111111111111111111111111111111111111"
        resolved.account = "keeper"

        w3 = Mock()
        w3.eth.get_transaction_count.return_value = 7
        w3.eth.chain_id = 901
        w3.eth.get_block.return_value = {"baseFeePerGas": None}
        w3.eth.gas_price = 100
        w3.eth.estimate_gas.return_value = 21_000

        return KeeperSigner(w3=w3, signer=resolved), w3

    def test_send_tx_reuses_nonce_after_send_failure_without_tx_hash(self) -> None:
        signer, w3 = self._build_signer()
        w3.eth.send_transaction.side_effect = [
            RuntimeError("insufficient funds for transfer"),
            HexBytes("0x" + "12" * 32),
        ]
        w3.eth.wait_for_transaction_receipt.return_value = {"status": 1}

        with self.assertRaisesRegex(RuntimeError, "insufficient funds"):
            signer.send_tx(
                to="0x2222222222222222222222222222222222222222",
                data=b"\x12\x34",
                value_wei=1,
                label="keeper send",
            )

        tx_hash = signer.send_tx(
            to="0x2222222222222222222222222222222222222222",
            data=b"\x12\x34",
            value_wei=1,
            label="keeper send retry",
        )

        self.assertEqual(tx_hash, "12" * 32)
        self.assertEqual(w3.eth.send_transaction.call_args_list[0].args[0]["nonce"], 7)
        self.assertEqual(w3.eth.send_transaction.call_args_list[1].args[0]["nonce"], 7)
        self.assertEqual(signer._nonce_cache, 8)
        w3.eth.get_transaction_count.assert_called_once_with(
            "0x1111111111111111111111111111111111111111",
            block_identifier="pending",
        )

    def test_send_contract_tx_advances_nonce_only_after_tx_hash_is_acquired(self) -> None:
        signer, w3 = self._build_signer()
        tx_hashes = [
            HexBytes("0x" + "34" * 32),
            HexBytes("0x" + "56" * 32),
        ]
        w3.eth.send_transaction.side_effect = tx_hashes
        w3.eth.wait_for_transaction_receipt.side_effect = [
            TimeExhausted("timeout"),
            {"status": 1},
        ]

        contract = Mock()
        fn = Mock()
        fn.build_transaction.side_effect = lambda params: {
            **params,
            "to": "0x2222222222222222222222222222222222222222",
            "data": HexBytes("0x1234"),
        }
        contract.functions.handleMessage.return_value = fn
        w3.eth.contract.return_value = contract

        artifact = Mock()
        artifact.abi = []
        loader = Mock()
        loader.load.return_value = artifact

        with patch("py_lib.keeper_signer.ArtifactLoader", return_value=loader):
            with self.assertRaises(PendingTxTimeoutError):
                signer.send_contract_tx(
                    contract_name="CollarTSAReceiver",
                    address="0x2222222222222222222222222222222222222222",
                    fn_name="handleMessage",
                    args=["0x" + "ab" * 32],
                    label="handle message",
                )

            tx_hash = signer.send_contract_tx(
                contract_name="CollarTSAReceiver",
                address="0x2222222222222222222222222222222222222222",
                fn_name="handleMessage",
                args=["0x" + "cd" * 32],
                label="handle message retry",
            )

        self.assertEqual(tx_hash, "56" * 32)
        self.assertEqual(w3.eth.send_transaction.call_args_list[0].args[0]["nonce"], 7)
        self.assertEqual(w3.eth.send_transaction.call_args_list[1].args[0]["nonce"], 8)
        self.assertEqual(signer._nonce_cache, 9)
        w3.eth.get_transaction_count.assert_called_once_with(
            "0x1111111111111111111111111111111111111111",
            block_identifier="pending",
        )


if __name__ == "__main__":
    unittest.main()
