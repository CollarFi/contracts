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
    VerificationConfig,
    VerificationEntry,
    _infer_mode,
)
from ops.py_lib.operation_engine import OperationRuntime, resolve_l1_vault_address
from ops.py_lib.runtime import read_deployment_state, write_deployment_state
from ops.py_lib.signers import SignerInput, resolve_signer
from ops.management.enable_collateral import run_enable_collateral
from ops.management.set_l2_message_asset import run_set_l2_message_asset


class _StubRuntime:
    def __init__(self, code_addrs: set[str]) -> None:
        self.code_addrs = {addr.lower() for addr in code_addrs}

    def has_code(self, addr: str) -> bool:
        return addr.lower() in self.code_addrs


class _FakeOperationRuntime:
    def __init__(self, *, env: dict[str, str], rpc_url: str = "http://127.0.0.1:8545", broadcast: bool = False) -> None:
        self.env = env
        self.rpc_url = rpc_url
        self.broadcast = broadcast
        self.env_file = Path(".env.test")
        self.calls: list[tuple[str, tuple[str, ...], bool]] = []
        self.sends: list[tuple[str, tuple[str, ...]]] = []
        self.call_results: dict[tuple[str, str, tuple[str, ...], bool], str] = {}
        self.next_tx = "0xdeadbeef"

    @property
    def mode(self) -> str:
        return "broadcast" if self.broadcast else "dry-run"

    def chain_id(self) -> str:
        return self.env.get("CHAIN_ID", "31337")

    def cast_call(self, to: str, sig: str, *args: str, allow_fail: bool = False) -> str:
        key = (to, sig, args, allow_fail)
        self.calls.append((sig, args, allow_fail))
        if key not in self.call_results:
            raise AssertionError(f"unexpected cast_call: {key}")
        return self.call_results[key]

    def render_cast_send(self, to: str, sig: str, *args: str, value_wei: str | None = None) -> str:
        del value_wei
        return f"cast send {to} {sig} {' '.join(args)} --rpc-url {self.rpc_url} --account test"

    def cast_send(self, to: str, sig: str, *args: str, value_wei: str | None = None) -> str:
        del value_wei
        self.sends.append((sig, args))
        return self.next_tx

    def signer_summary(self) -> dict[str, object]:
        return {"kind": "account", "address": None, "account": "test", "unlocked": False, "from": None}


class DeployEngineTests(unittest.TestCase):
    def test_artifact_loader_reads_contract_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_path = root / "out" / "CollarVault.sol" / "CollarVault.json"
            artifact_path.parent.mkdir(parents=True)
            artifact_path.write_text(
                json_dumps(
                    {
                        "abi": [],
                        "bytecode": {"object": "6000", "linkReferences": {}},
                        "metadata": {
                            "settings": {
                                "compilationTarget": {
                                    "src/CollarVault.sol": "CollarVault",
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            artifact = ArtifactLoader(root=root).load("CollarVault")
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

    def test_operation_runtime_loads_env_and_renders_redacted_private_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env.l1.test"
            env_path.write_text(
                "RPC_URL=http://127.0.0.1:8545\nACCOUNT=OpsSigner\nPRIVATE_KEY=0x59c6995e998f97a5a0044966f094538c5f27b0e6f0f64f7f36f5d5f7d3c5b5fd\n",
                encoding="utf-8",
            )
            runtime = OperationRuntime.from_env_file(env_path, broadcast=False)
            cmd = runtime.render_cast_send("0x1234", "ping()")
            self.assertIn("--private-key '<redacted>'", cmd)
            self.assertEqual(runtime.mode, "dry-run")

    def test_resolve_l1_vault_address_prefers_env_then_output(self) -> None:
        env = {"L1_VAULT": "0x1111111111111111111111111111111111111111"}
        self.assertEqual(
            resolve_l1_vault_address(env, "http://127.0.0.1:8545"),
            "0x1111111111111111111111111111111111111111",
        )

        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "l1.json"
            out_path.write_text(json_dumps({"addrs": {"l1VaultProxy": "0x2222222222222222222222222222222222222222"}}), encoding="utf-8")
            env = {"OUTPUT_JSON": str(out_path)}
            self.assertEqual(
                resolve_l1_vault_address(env, "http://127.0.0.1:8545"),
                "0x2222222222222222222222222222222222222222",
            )

    def test_enable_collateral_broadcasts_even_when_already_matching(self) -> None:
        vault = "0x1111111111111111111111111111111111111111"
        asset = "0x2222222222222222222222222222222222222222"
        l2_asset = "0x3333333333333333333333333333333333333333"
        runtime = _FakeOperationRuntime(
            env={
                "OUTPUT_JSON": "unused.json",
                "WETH_ASSET": asset,
                "L2_WRAPPED_WETH_ASSET": l2_asset,
            },
            broadcast=True,
        )
        runtime.call_results[(vault, "collateralAllowed(address)(bool)", (asset,), False)] = "true"
        runtime.call_results[(vault, "strikeScale(address)(uint256)", (asset,), False)] = str(10**30)
        runtime.call_results[(vault, "l2MessageAsset(address)(address)", (asset,), False)] = l2_asset

        with patch("ops.management.enable_collateral.resolve_l1_vault_address", return_value=vault):
            out = run_enable_collateral(runtime, env_profile="testnet", asset="", scale=10**30, l2_asset="")

        self.assertEqual(out["tx"], runtime.next_tx)
        self.assertEqual(len(runtime.sends), 1)
        self.assertFalse(out["steps"][0]["needsUpdate"])
        self.assertTrue(out["steps"][0]["executed"])

    def test_set_l2_message_asset_skips_send_when_mapping_matches(self) -> None:
        vault = "0x1111111111111111111111111111111111111111"
        l1_asset = "0x2222222222222222222222222222222222222222"
        l2_asset = "0x3333333333333333333333333333333333333333"
        runtime = _FakeOperationRuntime(
            env={
                "OUTPUT_JSON": "unused.json",
                "WETH_ASSET": l1_asset,
                "L2_WRAPPED_WETH_ASSET": l2_asset,
            },
            broadcast=True,
        )
        runtime.call_results[(vault, "l2MessageAsset(address)(address)", (l1_asset,), True)] = l2_asset
        runtime.call_results[(vault, "collateralAllowed(address)(bool)", (l1_asset,), True)] = "true"
        runtime.call_results[(vault, "strikeScale(address)(uint256)", (l1_asset,), True)] = str(10**30)

        with patch("ops.management.set_l2_message_asset.resolve_l1_vault_address", return_value=vault):
            out = run_set_l2_message_asset(
                runtime,
                env_profile="testnet",
                l2_env_file=Path(".env.l2.testnet"),
                l1_asset="",
                l2_asset="",
                vault="",
            )

        self.assertTrue(out["broadcast"])
        self.assertFalse(out["needsUpdate"])
        self.assertIsNone(out["tx"])
        self.assertEqual(runtime.sends, [])
        self.assertEqual(out["steps"][0]["skippedReason"], "already_correct")

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
