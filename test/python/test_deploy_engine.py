from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from eth_account import Account

from ops.py_lib.deploy_engine import (
    ArtifactLoader,
    DeploymentRuntime,
    SignerInput,
    VerificationConfig,
    VerificationEntry,
    _infer_mode,
    read_deployment_state,
    resolve_signer,
    write_deployment_state,
)


class _StubRuntime:
    def __init__(self, code_addrs: set[str]) -> None:
        self.code_addrs = {addr.lower() for addr in code_addrs}

    def has_code(self, addr: str) -> bool:
        return addr.lower() in self.code_addrs


class DeployEngineTests(unittest.TestCase):
    def test_artifact_loader_reads_contract_id(self) -> None:
        artifact = ArtifactLoader().load("CollarVault")
        self.assertEqual(artifact.contract_id, "src/CollarVault.sol:CollarVault")
        self.assertTrue(artifact.bytecode.startswith("0x"))

    def test_resolve_signer_private_key_and_unlocked(self) -> None:
        private_key = "0x59c6995e998f97a5a0044966f094538c5f27b0e6f0f64f7f36f5d5f7d3c5b5fd"
        signer = resolve_signer(
            "deployer",
            SignerInput(private_key=private_key, password_env_keys=("IGNORED",)),
        )
        self.assertIsNotNone(signer)
        assert signer is not None
        self.assertEqual(signer.kind, "private_key")
        self.assertEqual(signer.private_key_hex("unit-test"), private_key)

        unlocked = resolve_signer(
            "deployer",
            SignerInput(from_addr="0x70997970C51812dc3A010C7d01b50e0d17dc79C8", unlocked=True),
        )
        self.assertIsNotNone(unlocked)
        assert unlocked is not None
        self.assertEqual(unlocked.kind, "unlocked")
        self.assertEqual(unlocked.address, "0x70997970C51812dc3A010C7d01b50e0d17dc79C8")

    def test_resolve_signer_foundry_keystore_via_env_password(self) -> None:
        acct = Account.from_key("0x8b3a350cf5c34c9194ca7a9c1f90f743fe3d0f6b4cb1d0c01c4c9d4bfc4c2b52")
        payload = Account.encrypt(acct.key, "secret-password")

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            keystore_dir = home / ".foundry" / "keystores"
            keystore_dir.mkdir(parents=True)
            (keystore_dir / "TestSigner").write_text(json_dumps(payload), encoding="utf-8")

            with patch.object(Path, "home", return_value=home):
                with patch.dict(os.environ, {"ACCOUNT_PASSWORD": "secret-password"}, clear=False):
                    signer = resolve_signer(
                        "deployer",
                        SignerInput(account="TestSigner", password_env_keys=("ACCOUNT_PASSWORD",)),
                    )
                    self.assertIsNotNone(signer)
                    assert signer is not None
                    self.assertEqual(signer.kind, "keystore")
                    self.assertEqual(signer.address, acct.address)
                    self.assertEqual(signer.private_key_hex("unit-test"), acct.key.hex())

    def test_output_state_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deployments" / "l1.json"
            write_deployment_state(path, {"l1Vault": "0x1234"}, {"mode": "fresh", "txs": []})
            loaded = read_deployment_state(path)
            self.assertEqual(loaded["l1Vault"], "0x1234")
            self.assertEqual(loaded["meta"]["mode"], "fresh")

    def test_infer_mode_validates_partial_reuse_and_non_contracts(self) -> None:
        fresh_runtime = _StubRuntime(set())
        self.assertEqual(_infer_mode("auto", {"A": "", "B": ""}, fresh_runtime), "fresh")

        upgrade_runtime = _StubRuntime({"0x1111111111111111111111111111111111111111", "0x2222222222222222222222222222222222222222"})
        self.assertEqual(
            _infer_mode(
                "auto",
                {"A": "0x1111111111111111111111111111111111111111", "B": "0x2222222222222222222222222222222222222222"},
                upgrade_runtime,
            ),
            "upgrade",
        )

        with self.assertRaisesRegex(ValueError, "partial reuse"):
            _infer_mode(
                "auto",
                {"A": "0x1111111111111111111111111111111111111111", "B": ""},
                upgrade_runtime,
            )

        with self.assertRaisesRegex(ValueError, "non-contract address"):
            _infer_mode(
                "upgrade",
                {"A": "0x3333333333333333333333333333333333333333"},
                _StubRuntime(set()),
            )

    def _make_verify_runtime(self, rpc_url: str = "http://127.0.0.1:8545") -> DeploymentRuntime:
        runtime = DeploymentRuntime.__new__(DeploymentRuntime)
        runtime.verification = VerificationConfig(enabled=True, timeout_seconds=1)
        runtime.broadcast = True
        runtime.chain_id = 1
        runtime.rpc_url = rpc_url
        runtime.meta = {"verification": []}
        runtime._verify_entries = [
            VerificationEntry(
                label="deploy test contract",
                address="0x1111111111111111111111111111111111111111",
                contract_id="src/Test.sol:Test",
                constructor_args="0x",
            )
        ]
        runtime.persist = lambda: None
        return runtime

    @patch("ops.py_lib.deploy_engine.subprocess.run")
    def test_verify_pending_is_non_blocking_on_cloudflare_failure(self, mock_run: unittest.mock.Mock) -> None:
        runtime = self._make_verify_runtime("https://sepolia.etherscan.io")
        mock_run.return_value = SimpleNamespace(
            returncode=1,
            stdout="403 Forbidden\nCloudflare Ray ID: blocked",
        )

        runtime.verify_pending()

        self.assertEqual(len(runtime.meta["verification"]), 1)
        record = runtime.meta["verification"][0]
        self.assertFalse(record["ok"])
        self.assertEqual(record["status"], "cloudflare_blocked")
        self.assertTrue(record["nonBlocking"])
        self.assertEqual(runtime._verify_entries, [])

    @patch("ops.py_lib.deploy_engine.subprocess.run")
    def test_verify_pending_is_non_blocking_on_timeout(self, mock_run: unittest.mock.Mock) -> None:
        runtime = self._make_verify_runtime("https://sepolia.etherscan.io")
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=["forge", "verify-contract"],
            timeout=1,
            output="submitted but no response",
        )

        runtime.verify_pending()

        self.assertEqual(len(runtime.meta["verification"]), 1)
        record = runtime.meta["verification"][0]
        self.assertFalse(record["ok"])
        self.assertEqual(record["status"], "timeout")
        self.assertIn("timed out", record["error"])

    @patch("ops.py_lib.deploy_engine.subprocess.run")
    def test_verify_pending_skips_local_rpc(self, mock_run: unittest.mock.Mock) -> None:
        runtime = self._make_verify_runtime()

        runtime.verify_pending()

        self.assertEqual(len(runtime.meta["verification"]), 1)
        record = runtime.meta["verification"][0]
        self.assertFalse(record["ok"])
        self.assertEqual(record["status"], "skipped_local_rpc")
        self.assertTrue(record["nonBlocking"])
        mock_run.assert_not_called()


def json_dumps(payload: object) -> str:
    import json

    return json.dumps(payload)


if __name__ == "__main__":
    unittest.main()
