from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from getpass import getpass
from pathlib import Path
from typing import Any, Callable, Literal, Sequence

from eth_account import Account
from eth_utils import to_hex
from hexbytes import HexBytes
from web3 import HTTPProvider, Web3
from web3.contract import Contract

from .lz import encode_lz_receive_option, is_empty_hex, is_zero_address

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
EIP1967_ADMIN_SLOT = int.from_bytes(Web3.keccak(text="eip1967.proxy.admin"), "big") - 1
EIP1967_IMPLEMENTATION_SLOT = int.from_bytes(Web3.keccak(text="eip1967.proxy.implementation"), "big") - 1
ROOT_DIR = Path(__file__).resolve().parents[2]

SignerRole = Literal["deployer", "proxy_admin"]
DeployMode = Literal["auto", "fresh", "upgrade"]


def _normalize_addr(value: str) -> str:
    return Web3.to_checksum_address(value)


def _nonzero_addr(value: str) -> bool:
    return bool(value) and not is_zero_address(value)


def _int_env(env: dict[str, str], key: str, default: int = 0) -> int:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    return int(raw, 0)


def _bool_env(env: dict[str, str], key: str) -> bool:
    return (env.get(key) or "").strip().lower() in {"1", "true", "yes", "on"}


def _bytes_env(env: dict[str, str], key: str) -> bytes:
    raw = (env.get(key) or "").strip()
    if not raw:
        return b""
    if raw.startswith("0x"):
        return bytes.fromhex(raw[2:])
    return raw.encode("utf-8")


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object in {path}")
    return data


def read_deployment_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = _load_json(path)
    addrs = data.get("addrs")
    if isinstance(addrs, dict):
        merged = dict(addrs)
        if isinstance(data.get("meta"), dict):
            merged["meta"] = data["meta"]
        return merged
    return data


def write_deployment_state(path: Path, addrs: dict[str, Any], meta: dict[str, Any]) -> None:
    payload = dict(addrs)
    if meta:
        payload["meta"] = meta
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class Artifact:
    name: str
    path: Path
    contract_id: str
    abi: list[dict[str, Any]]
    bytecode: str


class ArtifactLoader:
    def __init__(self, root: Path = ROOT_DIR):
        self.root = root
        self._cache: dict[str, Artifact] = {}

    def ensure_build(self) -> None:
        proc = subprocess.run(["forge", "build"], cwd=self.root, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if proc.returncode != 0:
            raise CmdError(f"forge build failed\n{proc.stdout}")

    def load(self, contract_name: str) -> Artifact:
        cached = self._cache.get(contract_name)
        if cached:
            return cached

        artifact_path = self.root / "out" / f"{contract_name}.sol" / f"{contract_name}.json"
        if not artifact_path.is_file():
            raise FileNotFoundError(f"artifact not found for {contract_name}: {artifact_path}")

        raw = _load_json(artifact_path)
        abi = raw.get("abi")
        bytecode = raw.get("bytecode", {}).get("object")
        target = raw.get("metadata", {}).get("settings", {}).get("compilationTarget", {})
        if not isinstance(abi, list) or not isinstance(bytecode, str) or not bytecode:
            raise ValueError(f"invalid artifact shape: {artifact_path}")
        if len(target) != 1:
            raise ValueError(f"unexpected compilation target in {artifact_path}")
        source_path, compiled_name = next(iter(target.items()))
        artifact = Artifact(
            name=contract_name,
            path=artifact_path,
            contract_id=f"{source_path}:{compiled_name}",
            abi=abi,
            bytecode=bytecode if bytecode.startswith("0x") else f"0x{bytecode}",
        )
        self._cache[contract_name] = artifact
        return artifact

    def encode_constructor_args(self, w3: Web3, artifact: Artifact, args: Sequence[Any]) -> str:
        constructors = [entry for entry in artifact.abi if entry.get("type") == "constructor"]
        if not constructors:
            return "0x"
        ctor = constructors[0]
        inputs = ctor.get("inputs", [])
        if not inputs:
            return "0x"
        encoded = w3.codec.encode([str(inp["type"]) for inp in inputs], list(args))
        return to_hex(encoded)


class CmdError(RuntimeError):
    pass


def run(cmd: list[str], *, capture: bool = True, check: bool = True) -> str:
    proc = subprocess.run(
        cmd,
        cwd=ROOT_DIR,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if check and proc.returncode != 0:
        raise CmdError(f"command failed ({proc.returncode}): {' '.join(shlex.quote(part) for part in cmd)}\n{(proc.stderr or '').strip()}")
    return (proc.stdout or "").strip()


def require_cmd(name: str) -> None:
    run(["bash", "-lc", f"command -v {shlex.quote(name)} >/dev/null"])


@dataclass
class SignerInput:
    account: str = ""
    private_key: str = ""
    from_addr: str = ""
    unlocked: bool = False
    password_env_keys: tuple[str, ...] = ()


@dataclass
class ResolvedSigner:
    kind: Literal["private_key", "keystore", "unlocked"]
    address: str
    account: str = ""
    private_key: str = ""
    from_addr: str = ""
    unlocked: bool = False
    keystore_path: Path | None = None
    password_env_keys: tuple[str, ...] = ()

    def auth_summary(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "address": self.address,
            "account": self.account or None,
            "unlocked": self.unlocked,
            "from": self.from_addr or None,
        }

    def _resolve_password(self, prompt_label: str) -> str:
        for key in self.password_env_keys:
            value = os.environ.get(key, "")
            if value:
                return value
        if not os.isatty(0):
            joined = ", ".join(self.password_env_keys) or "<no password env configured>"
            raise ValueError(
                f"missing password for Foundry account {self.account}; "
                f"set one of [{joined}] or run in an interactive terminal"
            )
        return getpass(f"Password for {prompt_label}: ")

    def private_key_hex(self, prompt_label: str) -> str:
        if self.kind == "private_key":
            return self.private_key
        if self.kind == "keystore":
            if self.keystore_path is None:
                raise ValueError(f"missing keystore path for {self.account}")
            payload = json.loads(self.keystore_path.read_text(encoding="utf-8"))
            password = self._resolve_password(prompt_label)
            secret = Account.decrypt(payload, password)
            return HexBytes(secret).hex()
        raise ValueError("unlocked signers do not expose a private key")


def resolve_signer(role: str, cfg: SignerInput) -> ResolvedSigner | None:
    if cfg.private_key:
        addr = Account.from_key(cfg.private_key).address
        return ResolvedSigner(kind="private_key", address=_normalize_addr(addr), private_key=cfg.private_key)
    if cfg.account:
        keystore = Path.home() / ".foundry" / "keystores" / cfg.account
        if not keystore.is_file():
            raise FileNotFoundError(f"Foundry keystore not found for {cfg.account}: {keystore}")
        payload = _load_json(keystore)
        raw_addr = payload.get("address")
        if not isinstance(raw_addr, str) or not raw_addr:
            raise ValueError(f"keystore {keystore} missing address")
        if not raw_addr.startswith("0x"):
            raw_addr = f"0x{raw_addr}"
        return ResolvedSigner(
            kind="keystore",
            address=_normalize_addr(raw_addr),
            account=cfg.account,
            keystore_path=keystore,
            password_env_keys=cfg.password_env_keys,
        )
    if cfg.unlocked and cfg.from_addr:
        return ResolvedSigner(
            kind="unlocked",
            address=_normalize_addr(cfg.from_addr),
            from_addr=_normalize_addr(cfg.from_addr),
            unlocked=True,
        )
    return None


@dataclass
class VerificationConfig:
    enabled: bool = False
    verifier: str = ""
    verifier_url: str = ""
    etherscan_api_key: str = ""
    timeout_seconds: int = 120


@dataclass
class VerificationEntry:
    label: str
    address: str
    contract_id: str
    constructor_args: str


@dataclass
class StepResult:
    changed: bool
    details: str = ""
    outputs: dict[str, Any] = field(default_factory=dict)
    tx_hash: str | None = None


@dataclass
class DeployStep:
    chain: str
    target: str
    action: str
    signer_role: SignerRole
    precondition: str
    postcondition: str
    output_fields: list[str]
    when: Callable[["DeploymentRuntime"], bool]
    execute: Callable[["DeploymentRuntime"], StepResult]

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain": self.chain,
            "target": self.target,
            "action": self.action,
            "signerRole": self.signer_role,
            "precondition": self.precondition,
            "postcondition": self.postcondition,
            "outputFields": self.output_fields,
        }


@dataclass
class DeploymentSummary:
    mode: DeployMode
    broadcast: bool
    output_json: Path
    addrs: dict[str, Any]
    meta: dict[str, Any]
    steps: list[dict[str, Any]]
    executed_steps: list[dict[str, Any]]


class DeploymentRuntime:
    def __init__(
        self,
        *,
        chain: str,
        rpc_url: str,
        broadcast: bool,
        output_json: Path,
        artifact_loader: ArtifactLoader,
        signers: dict[SignerRole, ResolvedSigner | None],
        verification: VerificationConfig,
        existing_state: dict[str, Any] | None = None,
    ) -> None:
        self.chain = chain
        self.rpc_url = rpc_url
        self.broadcast = broadcast
        self.output_json = output_json
        self.artifacts = artifact_loader
        self.signers = signers
        self.verification = verification
        self.w3 = Web3(HTTPProvider(rpc_url, request_kwargs={"timeout": 45}))
        self.chain_id = int(self.w3.eth.chain_id)
        self.addrs: dict[str, Any] = {
            key: value
            for key, value in (existing_state or {}).items()
            if key != "meta"
        }
        self.meta: dict[str, Any] = dict((existing_state or {}).get("meta", {}))
        self.meta.setdefault("mode", None)
        self.meta.setdefault("chainId", self.chain_id)
        self.meta.setdefault("signers", {})
        self.meta.setdefault("txs", [])
        self.meta.setdefault("verification", [])
        self._nonce_cache: dict[str, int] = {}
        self._verify_entries: list[VerificationEntry] = []

    def has_code(self, addr: str) -> bool:
        return _nonzero_addr(addr) and self.w3.eth.get_code(_normalize_addr(addr)) not in {b"", HexBytes("0x")}

    def read_storage_address(self, addr: str, slot: int) -> str:
        raw = self.w3.eth.get_storage_at(_normalize_addr(addr), slot)
        return _normalize_addr("0x" + raw[-20:].hex())

    def proxy_admin_of(self, proxy: str) -> str:
        return self.read_storage_address(proxy, EIP1967_ADMIN_SLOT)

    def proxy_implementation_of(self, proxy: str) -> str:
        return self.read_storage_address(proxy, EIP1967_IMPLEMENTATION_SLOT)

    def contract(self, artifact_name: str, address: str) -> Contract:
        artifact = self.artifacts.load(artifact_name)
        return self.w3.eth.contract(address=_normalize_addr(address), abi=artifact.abi)

    def contract_from_artifact(self, artifact: Artifact, address: str) -> Contract:
        return self.w3.eth.contract(address=_normalize_addr(address), abi=artifact.abi)

    def contract_factory(self, artifact_name: str) -> Contract:
        artifact = self.artifacts.load(artifact_name)
        return self.w3.eth.contract(abi=artifact.abi, bytecode=artifact.bytecode)

    def encode_call(self, artifact_name: str, function_name: str, *args: Any) -> bytes:
        artifact = self.artifacts.load(artifact_name)
        contract = self.w3.eth.contract(abi=artifact.abi)
        data = getattr(contract.functions, function_name)(*args)._encode_transaction_data()
        return HexBytes(data)

    def call(self, artifact_name: str, address: str, function_name: str, *args: Any, default: Any = None) -> Any:
        try:
            contract = self.contract(artifact_name, address)
            return getattr(contract.functions, function_name)(*args).call()
        except Exception:
            return default

    def require_signer(self, role: SignerRole) -> ResolvedSigner:
        signer = self.signers.get(role)
        if signer is None:
            raise ValueError(f"missing signer for role {role}")
        return signer

    def _tx_template(self, signer: ResolvedSigner) -> dict[str, Any]:
        tx: dict[str, Any] = {
            "from": signer.address,
            "chainId": self.chain_id,
            "nonce": self._nonce_cache.get(signer.address, self.w3.eth.get_transaction_count(signer.address)),
        }
        tx["gasPrice"] = int(self.w3.eth.gas_price)
        return tx

    def _sign_and_send(self, signer: ResolvedSigner, tx: dict[str, Any], label: str) -> str:
        if "gas" not in tx:
            estimated = self.w3.eth.estimate_gas(tx)
            tx["gas"] = max(int(estimated * 1.2), estimated + 25_000)

        if signer.kind == "unlocked":
            tx_hash = self.w3.eth.send_transaction(tx)
        else:
            signed = self.w3.eth.account.sign_transaction(
                tx,
                signer.private_key_hex(f"{label} ({signer.account or signer.address})"),
            )
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)

        self._nonce_cache[signer.address] = int(tx["nonce"]) + 1
        return HexBytes(tx_hash).hex()

    def send_contract_tx(
        self,
        *,
        role: SignerRole,
        contract_call: Any,
        label: str,
        value: int = 0,
    ) -> str:
        signer = self.require_signer(role)
        tx = contract_call.build_transaction(self._tx_template(signer))
        if value:
            tx["value"] = value
        tx_hash = self._sign_and_send(signer, tx, label)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
        if int(receipt["status"]) != 1:
            raise RuntimeError(f"{label} reverted: {tx_hash}")
        self.meta["txs"].append({"label": label, "hash": tx_hash})
        return tx_hash

    def deploy_contract(
        self,
        *,
        role: SignerRole,
        contract_name: str,
        constructor_args: Sequence[Any],
        label: str,
    ) -> tuple[str, str]:
        artifact = self.artifacts.load(contract_name)
        factory = self.contract_factory(contract_name)
        signer = self.require_signer(role)
        tx = factory.constructor(*constructor_args).build_transaction(self._tx_template(signer))
        tx_hash = self._sign_and_send(signer, tx, label)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
        if int(receipt["status"]) != 1 or receipt.contractAddress is None:
            raise RuntimeError(f"{label} failed: {tx_hash}")
        address = _normalize_addr(receipt.contractAddress)
        self.meta["txs"].append({"label": label, "hash": tx_hash})
        if self.verification.enabled:
            self._verify_entries.append(
                VerificationEntry(
                    label=label,
                    address=address,
                    contract_id=artifact.contract_id,
                    constructor_args=self.artifacts.encode_constructor_args(self.w3, artifact, constructor_args),
                )
            )
        return address, tx_hash

    def persist(self) -> None:
        write_deployment_state(self.output_json, self.addrs, self.meta)

    @staticmethod
    def _is_cloudflare_verification_failure(output: str) -> bool:
        lowered = output.lower()
        return any(
            marker in lowered
            for marker in (
                "cloudflare",
                "attention required",
                "error code: 1020",
                "access denied",
                "forbidden",
            )
        )

    @staticmethod
    def _verification_error_summary(output: str) -> str:
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        if not lines:
            return "verification failed"
        return lines[-1]

    def verify_pending(self) -> None:
        if not self.verification.enabled or not self.broadcast:
            return
        pending = list(self._verify_entries)
        self._verify_entries.clear()
        for entry in pending:
            cmd = [
                "forge",
                "verify-contract",
                entry.address,
                entry.contract_id,
                "--chain",
                str(self.chain_id),
                "--rpc-url",
                self.rpc_url,
                "--watch",
                "--skip-is-verified-check",
            ]
            if not is_empty_hex(entry.constructor_args):
                cmd += ["--constructor-args", entry.constructor_args]
            if self.verification.verifier:
                cmd += ["--verifier", self.verification.verifier]
            if self.verification.verifier_url:
                cmd += ["--verifier-url", self.verification.verifier_url]
            if self.verification.etherscan_api_key:
                cmd += ["--etherscan-api-key", self.verification.etherscan_api_key]
            record = {
                "label": entry.label,
                "address": entry.address,
                "contract": entry.contract_id,
                "command": " ".join(shlex.quote(part) for part in cmd),
                "nonBlocking": True,
            }
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=ROOT_DIR,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=self.verification.timeout_seconds,
                )
                output = proc.stdout or ""
                record["ok"] = proc.returncode == 0
                record["status"] = "ok" if proc.returncode == 0 else "failed"
                if proc.returncode != 0:
                    if self._is_cloudflare_verification_failure(output):
                        record["status"] = "cloudflare_blocked"
                    record["error"] = self._verification_error_summary(output)
                    if output.strip():
                        record["outputTail"] = "\n".join(output.strip().splitlines()[-10:])
            except subprocess.TimeoutExpired as exc:
                output = exc.stdout if isinstance(exc.stdout, str) else ""
                record["ok"] = False
                record["status"] = "timeout"
                record["error"] = f"verification timed out after {self.verification.timeout_seconds}s"
                if output.strip():
                    record["outputTail"] = "\n".join(output.strip().splitlines()[-10:])
            except Exception as exc:
                record["ok"] = False
                record["status"] = "error"
                record["error"] = str(exc)
            self.meta["verification"].append(record)
            self.persist()


def _resolve_existing_proxy(env: dict[str, str], existing: dict[str, Any], env_key: str, output_key: str) -> str:
    value = (env.get(env_key) or existing.get(output_key) or "").strip()
    return value


def _infer_mode(mode: DeployMode, existing_pairs: dict[str, str], runtime: DeploymentRuntime) -> Literal["fresh", "upgrade"]:
    if mode == "fresh":
        for key, value in existing_pairs.items():
            if _nonzero_addr(value):
                raise ValueError(f"{key} must be unset in --mode fresh")
        return "fresh"
    if mode == "upgrade":
        missing = [key for key, value in existing_pairs.items() if not _nonzero_addr(value)]
        if missing:
            raise ValueError(f"--mode upgrade requires existing proxy addresses: {', '.join(missing)}")
        for key, value in existing_pairs.items():
            if not runtime.has_code(value):
                raise ValueError(f"{key} points to a non-contract address: {value}")
        return "upgrade"

    present = {key: value for key, value in existing_pairs.items() if _nonzero_addr(value)}
    if not present:
        return "fresh"
    if len(present) != len(existing_pairs):
        keys = ", ".join(sorted(present))
        raise ValueError(f"--mode auto does not support partial reuse; found only: {keys}")
    for key, value in present.items():
        if not runtime.has_code(value):
            raise ValueError(f"{key} points to a non-contract address: {value}")
    return "upgrade"


def _peer_bytes32(addr: str) -> bytes:
    return bytes.fromhex("00" * 12 + _normalize_addr(addr)[2:].lower())


def _role_bytes(contract: Contract, role_name: str) -> bytes:
    return getattr(contract.functions, role_name)().call()


def _has_role(contract: Contract, role_name: str, account: str) -> bool:
    role = _role_bytes(contract, role_name)
    return bool(contract.functions.hasRole(role, _normalize_addr(account)).call())


def _preflight_proxy_admin(runtime: DeploymentRuntime, proxy: str, label: str) -> None:
    signer = runtime.require_signer("proxy_admin")
    proxy_admin = runtime.proxy_admin_of(proxy)
    if not runtime.has_code(proxy_admin):
        raise ValueError(f"{label} proxy admin is not a contract: {proxy_admin}")
    owner = runtime.call("ProxyAdmin", proxy_admin, "owner")
    if owner is None or _normalize_addr(owner) != signer.address:
        raise ValueError(
            f"{label} proxy admin owner mismatch: expected {signer.address}, found {owner or 'unknown'}"
        )


@dataclass
class L1Config:
    mode: DeployMode
    admin: str
    proxy_admin_owner: str
    treasury: str
    vault_owner: str
    permit2: str
    l2_recipient: str
    liquidity_vault: str
    usdc_asset: str
    euler_adapter: str
    lz_endpoint: str
    l2_eid: int
    lz_receive_gas: int
    lz_receive_value: int
    weth_asset: str
    weth_socket_vault: str
    weth_socket_bridge: str
    weth_socket_connector: str
    weth_msg_gas_limit: int
    weth_payload_size: int
    weth_strike_scale: int
    l2_wrapped_weth_asset: str
    derive_subaccount_id: int
    rfq_signer: str
    existing_vault: str
    existing_messenger: str


@dataclass
class L2Config:
    mode: DeployMode
    admin: str
    proxy_admin_owner: str
    l1_messenger: str
    l1_vault: str
    lz_endpoint: str
    socket_tracker: str
    loan_store: str
    tsa_proxy: str
    receiver_proxy: str
    tsa_implementation: str
    tsa_init_data: bytes
    option_risk_verifier: str
    rfq_verifier: str
    rfq_delegate_module: str
    atomic_executor: str
    l1_eid: int
    matching: str
    subaccounts: str
    auction: str
    cash: str
    wrapped_deposit_asset: str
    manager: str
    base_feed: str
    deposit_module: str
    withdrawal_module: str
    trade_module: str
    rfq_module: str
    option_asset: str
    tsa_initial_owner: str
    tsa_symbol: str
    tsa_name: str
    tsa_min_signature_expiry: int
    tsa_max_signature_expiry: int
    tsa_option_vol_slippage_factor: int
    tsa_call_max_delta: int
    tsa_max_neg_cash: int
    tsa_option_min_time_to_expiry: int
    tsa_option_max_time_to_expiry: int
    tsa_put_max_price_factor: int
    tsa_worst_spot_sell_price: int
    weth_asset: str
    weth_socket_bridge: str
    weth_socket_connector: str
    weth_msg_gas_limit: int
    weth_payload_size: int
    l2_socket_adapter_mode: str
    usdc_asset: str
    usdc_socket_bridge: str
    usdc_socket_connector: str
    usdc_msg_gas_limit: int
    usdc_payload_size: int


def _build_l2_tsa_init_data(runtime: DeploymentRuntime, cfg: L2Config, loan_store: str, option_risk: str, rfq_verifier: str, delegate_module: str) -> bytes:
    base_init = (
        _normalize_addr(cfg.subaccounts),
        _normalize_addr(cfg.auction),
        _normalize_addr(cfg.cash),
        _normalize_addr(cfg.wrapped_deposit_asset),
        _normalize_addr(cfg.manager),
        _normalize_addr(cfg.matching),
        cfg.tsa_symbol,
        cfg.tsa_name,
    )
    tsa_params = (
        cfg.tsa_min_signature_expiry,
        cfg.tsa_max_signature_expiry,
        cfg.tsa_option_vol_slippage_factor,
        cfg.tsa_call_max_delta,
        cfg.tsa_max_neg_cash,
        cfg.tsa_option_min_time_to_expiry,
        cfg.tsa_option_max_time_to_expiry,
        cfg.tsa_put_max_price_factor,
    )
    collateral_params = (cfg.tsa_worst_spot_sell_price,)
    collar_init = (
        _normalize_addr(cfg.base_feed),
        _normalize_addr(cfg.deposit_module),
        _normalize_addr(cfg.withdrawal_module),
        _normalize_addr(cfg.trade_module),
        _normalize_addr(cfg.rfq_module),
        _normalize_addr(cfg.option_asset),
        _normalize_addr(option_risk),
        _normalize_addr(rfq_verifier),
        _normalize_addr(delegate_module),
        _normalize_addr(loan_store),
        tsa_params,
        collateral_params,
    )
    return runtime.encode_call("CollarTSA", "initialize", _normalize_addr(cfg.tsa_initial_owner), base_init, collar_init)


def _ensure_parameter_role(runtime: DeploymentRuntime, artifact_name: str, address: str, signer: str, label: str) -> None:
    contract = runtime.contract(artifact_name, address)
    if not _has_role(contract, "PARAMETER_ROLE", signer):
        raise ValueError(f"{label} requires PARAMETER_ROLE for signer {signer}")


def _ensure_default_admin_role(runtime: DeploymentRuntime, artifact_name: str, address: str, signer: str, label: str) -> None:
    contract = runtime.contract(artifact_name, address)
    if not _has_role(contract, "DEFAULT_ADMIN_ROLE", signer):
        raise ValueError(f"{label} requires DEFAULT_ADMIN_ROLE for signer {signer}")


def _ensure_owner(runtime: DeploymentRuntime, artifact_name: str, address: str, signer: str, label: str) -> None:
    owner = runtime.call(artifact_name, address, "owner")
    if owner is None or _normalize_addr(owner) != _normalize_addr(signer):
        raise ValueError(f"{label} requires owner {signer}, found {owner or 'unknown'}")


def _deploy_socket_adapter_l2(runtime: DeploymentRuntime, cfg: L2Config, asset: str, bridge: str, connector: str, gas_limit: int, payload_size: int, label: str) -> str:
    if cfg.l2_socket_adapter_mode == "compat":
        address, _ = runtime.deploy_contract(
            role="deployer",
            contract_name="SocketBridgeAdapterL2Compat",
            constructor_args=[_normalize_addr(asset), _normalize_addr(bridge), _normalize_addr(connector), gas_limit],
            label=label,
        )
        return address
    if cfg.l2_socket_adapter_mode == "new":
        address, _ = runtime.deploy_contract(
            role="deployer",
            contract_name="SocketBridgeAdapterNew",
            constructor_args=[
                _normalize_addr(asset),
                _normalize_addr(bridge),
                _normalize_addr(connector),
                gas_limit,
                payload_size,
                b"",
                b"",
            ],
            label=label,
        )
        return address
    raise ValueError(f"invalid L2_SOCKET_ADAPTER_MODE: {cfg.l2_socket_adapter_mode}")


def build_l1_config(env: dict[str, str], *, mode: DeployMode, proxy_admin_owner: str) -> L1Config:
    return L1Config(
        mode=mode,
        admin=_normalize_addr(env["ADMIN"]),
        proxy_admin_owner=_normalize_addr(proxy_admin_owner),
        treasury=_normalize_addr(env["TREASURY"]),
        vault_owner=_normalize_addr(env.get("VAULT_OWNER") or env["ADMIN"]),
        permit2=_normalize_addr(env.get("PERMIT2") or "0x000000000022D473030F116dDEE9F6B43aC78BA3"),
        l2_recipient=(env.get("L2_RECIPIENT") or "").strip(),
        liquidity_vault=(env.get("LIQUIDITY_VAULT") or "").strip(),
        usdc_asset=(env.get("USDC_ASSET") or "").strip(),
        euler_adapter=(env.get("EULER_ADAPTER") or "").strip(),
        lz_endpoint=(env.get("LZ_ENDPOINT") or "").strip(),
        l2_eid=_int_env(env, "L2_EID", _int_env(env, "REMOTE_EID", 0)),
        lz_receive_gas=_int_env(env, "LZ_RECEIVE_GAS", 0),
        lz_receive_value=_int_env(env, "LZ_RECEIVE_VALUE", 0),
        weth_asset=(env.get("WETH_ASSET") or "").strip(),
        weth_socket_vault=(env.get("WETH_SOCKET_VAULT") or "").strip(),
        weth_socket_bridge=(env.get("WETH_SOCKET_BRIDGE") or "").strip(),
        weth_socket_connector=(env.get("WETH_SOCKET_CONNECTOR") or "").strip(),
        weth_msg_gas_limit=_int_env(env, "LZ_RECEIVE_GAS", _int_env(env, "WETH_MSG_GAS_LIMIT", 100_000)),
        weth_payload_size=_int_env(env, "WETH_PAYLOAD_SIZE", 161),
        weth_strike_scale=_int_env(env, "WETH_STRIKE_SCALE", 10**30),
        l2_wrapped_weth_asset=(env.get("L2_WRAPPED_WETH_ASSET") or "").strip(),
        derive_subaccount_id=_int_env(env, "DERIVE_SUBACCOUNT_ID", 0),
        rfq_signer=(env.get("RFQ_SIGNER") or "").strip(),
        existing_vault=(env.get("L1_VAULT") or "").strip(),
        existing_messenger=(env.get("L1_MESSENGER") or "").strip(),
    )


def build_l2_config(env: dict[str, str], *, mode: DeployMode, proxy_admin_owner: str) -> L2Config:
    return L2Config(
        mode=mode,
        admin=_normalize_addr(env["ADMIN"]),
        proxy_admin_owner=_normalize_addr(proxy_admin_owner),
        l1_messenger=(env.get("L1_MESSENGER") or "").strip(),
        l1_vault=(env.get("L1_VAULT") or "").strip(),
        lz_endpoint=(env.get("LZ_ENDPOINT") or "").strip(),
        socket_tracker=_normalize_addr(env["SOCKET_TRACKER"]),
        loan_store=(env.get("LOAN_STORE") or "").strip(),
        tsa_proxy=(env.get("TSA_PROXY") or "").strip(),
        receiver_proxy=(env.get("L2_RECEIVER") or "").strip(),
        tsa_implementation=(env.get("TSA_IMPLEMENTATION") or "").strip(),
        tsa_init_data=_bytes_env(env, "TSA_INIT_DATA"),
        option_risk_verifier=(env.get("OPTION_RISK_VERIFIER") or "").strip(),
        rfq_verifier=(env.get("RFQ_VERIFIER") or "").strip(),
        rfq_delegate_module=(env.get("RFQ_DELEGATE_MODULE") or "").strip(),
        atomic_executor=(env.get("ATOMIC_EXECUTOR") or "").strip(),
        l1_eid=_int_env(env, "L1_EID", 0),
        matching=(env.get("MATCHING") or "").strip(),
        subaccounts=(env.get("SUBACCOUNTS") or "").strip(),
        auction=(env.get("AUCTION") or "").strip(),
        cash=(env.get("CASH") or "").strip(),
        wrapped_deposit_asset=(env.get("WRAPPED_DEPOSIT_ASSET") or "").strip(),
        manager=(env.get("MANAGER") or "").strip(),
        base_feed=(env.get("BASE_FEED") or "").strip(),
        deposit_module=(env.get("DEPOSIT_MODULE") or "").strip(),
        withdrawal_module=(env.get("WITHDRAWAL_MODULE") or "").strip(),
        trade_module=(env.get("TRADE_MODULE") or "").strip(),
        rfq_module=(env.get("RFQ_MODULE") or "").strip(),
        option_asset=(env.get("OPTION_ASSET") or "").strip(),
        tsa_initial_owner=_normalize_addr(env.get("TSA_INITIAL_OWNER") or env["ADMIN"]),
        tsa_symbol=(env.get("TSA_SYMBOL") or "cTSA"),
        tsa_name=(env.get("TSA_NAME") or "Collar TSA"),
        tsa_min_signature_expiry=_int_env(env, "TSA_MIN_SIGNATURE_EXPIRY", 1800),
        tsa_max_signature_expiry=_int_env(env, "TSA_MAX_SIGNATURE_EXPIRY", 21600),
        tsa_option_vol_slippage_factor=_int_env(env, "TSA_OPTION_VOL_SLIPPAGE_FACTOR", int(0.9e18)),
        tsa_call_max_delta=_int_env(env, "TSA_CALL_MAX_DELTA", int(0.4e18)),
        tsa_max_neg_cash=_int_env(env, "TSA_MAX_NEG_CASH", int(-100e18)),
        tsa_option_min_time_to_expiry=_int_env(env, "TSA_OPTION_MIN_TIME_TO_EXPIRY", 86400),
        tsa_option_max_time_to_expiry=_int_env(env, "TSA_OPTION_MAX_TIME_TO_EXPIRY", 31536000),
        tsa_put_max_price_factor=_int_env(env, "TSA_PUT_MAX_PRICE_FACTOR", int(1.1e18)),
        tsa_worst_spot_sell_price=_int_env(env, "TSA_WORST_SPOT_SELL_PRICE", int(0.99e18)),
        weth_asset=(env.get("WETH_ASSET") or "").strip(),
        weth_socket_bridge=(env.get("WETH_SOCKET_BRIDGE") or "").strip(),
        weth_socket_connector=(env.get("WETH_SOCKET_CONNECTOR") or "").strip(),
        weth_msg_gas_limit=_int_env(env, "WETH_MSG_GAS_LIMIT", 100_000),
        weth_payload_size=_int_env(env, "WETH_PAYLOAD_SIZE", 161),
        l2_socket_adapter_mode=(env.get("L2_SOCKET_ADAPTER_MODE") or "new"),
        usdc_asset=(env.get("USDC_ASSET") or "").strip(),
        usdc_socket_bridge=(env.get("USDC_SOCKET_BRIDGE") or "").strip(),
        usdc_socket_connector=(env.get("USDC_SOCKET_CONNECTOR") or "").strip(),
        usdc_msg_gas_limit=_int_env(env, "USDC_MSG_GAS_LIMIT", 100_000),
        usdc_payload_size=_int_env(env, "USDC_PAYLOAD_SIZE", 161),
    )


def _validate_l1_permissions(runtime: DeploymentRuntime, cfg: L1Config, mode: Literal["fresh", "upgrade"]) -> None:
    deployer = runtime.require_signer("deployer").address
    if mode == "fresh":
        if cfg.vault_owner != deployer:
            raise ValueError(f"fresh L1 deploy requires deployer signer {deployer} to equal VAULT_OWNER {cfg.vault_owner}")
        if cfg.lz_receive_gas > 0 and cfg.admin != deployer:
            raise ValueError(f"fresh L1 deploy requires deployer signer {deployer} to equal ADMIN {cfg.admin}")
        return

    _ensure_parameter_role(runtime, "CollarVault", cfg.existing_vault, deployer, "L1 vault config")
    if cfg.rfq_signer:
        vault = runtime.contract("CollarVault", cfg.existing_vault)
        rfq_role = vault.functions.RFQ_SIGNER_ROLE().call()
        if not vault.functions.hasRole(rfq_role, _normalize_addr(cfg.rfq_signer)).call():
            _ensure_default_admin_role(runtime, "CollarVault", cfg.existing_vault, deployer, "L1 RFQ signer grant")
    if cfg.lz_receive_gas > 0:
        _ensure_parameter_role(runtime, "CollarVaultMessenger", cfg.existing_messenger, deployer, "L1 messenger options")


def _validate_l2_permissions(runtime: DeploymentRuntime, cfg: L2Config, mode: Literal["fresh", "upgrade"]) -> None:
    deployer = runtime.require_signer("deployer").address
    if mode == "fresh":
        if cfg.admin != deployer:
            raise ValueError(f"fresh L2 deploy requires deployer signer {deployer} to equal ADMIN {cfg.admin}")
        if cfg.tsa_initial_owner != deployer and (
            cfg.atomic_executor
            or cfg.weth_socket_connector
            or cfg.usdc_socket_connector
        ):
            raise ValueError(
                f"fresh L2 deploy requires deployer signer {deployer} to equal TSA_INITIAL_OWNER {cfg.tsa_initial_owner}"
            )
        return

    _ensure_default_admin_role(runtime, "CollarLoanStore", cfg.loan_store, deployer, "L2 loan store writer grants")
    if cfg.atomic_executor or cfg.weth_socket_connector or cfg.usdc_socket_connector:
        _ensure_owner(runtime, "CollarTSA", cfg.tsa_proxy, deployer, "L2 TSA owner config")
    if cfg.l1_messenger or cfg.l1_vault:
        _ensure_parameter_role(runtime, "CollarTSAReceiver", cfg.receiver_proxy, deployer, "L2 receiver config")


def run_l1_deploy(
    *,
    rpc_url: str,
    output_json: Path,
    cfg: L1Config,
    signers: dict[SignerRole, ResolvedSigner | None],
    broadcast: bool,
    verification: VerificationConfig,
) -> DeploymentSummary:
    require_cmd("forge")
    artifacts = ArtifactLoader()
    artifacts.ensure_build()
    existing = read_deployment_state(output_json)
    runtime = DeploymentRuntime(
        chain="l1",
        rpc_url=rpc_url,
        broadcast=broadcast,
        output_json=output_json,
        artifact_loader=artifacts,
        signers=signers,
        verification=verification,
        existing_state=existing,
    )

    cfg.existing_vault = _resolve_existing_proxy({"L1_VAULT": cfg.existing_vault}, existing, "L1_VAULT", "l1Vault")
    cfg.existing_messenger = _resolve_existing_proxy({"L1_MESSENGER": cfg.existing_messenger}, existing, "L1_MESSENGER", "l1Messenger")
    mode = _infer_mode(cfg.mode, {"L1_VAULT": cfg.existing_vault, "L1_MESSENGER": cfg.existing_messenger}, runtime)
    runtime.meta["mode"] = mode
    runtime.meta["signers"] = {
        role: signer.auth_summary() for role, signer in runtime.signers.items() if signer is not None
    }

    if cfg.proxy_admin_owner and runtime.signers.get("proxy_admin") and _normalize_addr(cfg.proxy_admin_owner) != runtime.require_signer("proxy_admin").address:
        raise ValueError(
            f"PROXY_ADMIN owner {cfg.proxy_admin_owner} does not match proxy-admin signer {runtime.require_signer('proxy_admin').address}"
        )

    if mode == "upgrade":
        _preflight_proxy_admin(runtime, cfg.existing_vault, "L1_VAULT")
        _preflight_proxy_admin(runtime, cfg.existing_messenger, "L1_MESSENGER")
    _validate_l1_permissions(runtime, cfg, mode)

    runtime.addrs.setdefault("l1Permit2", cfg.permit2)
    runtime.addrs.setdefault("l1DeriveSubaccountId", cfg.derive_subaccount_id)

    steps: list[DeployStep] = []

    def deploy_or_reuse_liquidity(rt: DeploymentRuntime) -> StepResult:
        if mode == "upgrade":
            value = rt.call("CollarVault", cfg.existing_vault, "liquidityVault")
            if not value:
                raise ValueError("failed to resolve liquidityVault() from existing L1 vault")
            return StepResult(changed=False, outputs={"l1LiquidityVault": _normalize_addr(value)})
        if _nonzero_addr(cfg.liquidity_vault):
            return StepResult(changed=False, outputs={"l1LiquidityVault": _normalize_addr(cfg.liquidity_vault)})
        if not _nonzero_addr(cfg.usdc_asset):
            raise ValueError("USDC_ASSET required when LIQUIDITY_VAULT is unset")
        address, tx_hash = rt.deploy_contract(
            role="deployer",
            contract_name="CollarLiquidityVault",
            constructor_args=[_normalize_addr(cfg.usdc_asset), "Collar Liquidity Vault", "cLV", cfg.admin],
            label="deploy L1 liquidity vault",
        )
        return StepResult(changed=True, tx_hash=tx_hash, outputs={"l1LiquidityVault": address})

    steps.append(
        DeployStep(
            chain="l1",
            target="liquidity_vault",
            action="ensure",
            signer_role="deployer",
            precondition="fresh deploy needs a liquidity vault address or USDC asset",
            postcondition="liquidity vault address is known",
            output_fields=["l1LiquidityVault"],
            when=lambda _: True,
            execute=deploy_or_reuse_liquidity,
        )
    )

    def deploy_or_reuse_euler(rt: DeploymentRuntime) -> StepResult:
        if mode == "upgrade":
            value = rt.call("CollarVault", cfg.existing_vault, "lendingAdapter")
            if not value:
                raise ValueError("failed to resolve lendingAdapter() from existing L1 vault")
            return StepResult(changed=False, outputs={"l1EulerAdapter": _normalize_addr(value)})
        if _nonzero_addr(cfg.euler_adapter):
            return StepResult(changed=False, outputs={"l1EulerAdapter": _normalize_addr(cfg.euler_adapter)})
        address, tx_hash = rt.deploy_contract(role="deployer", contract_name="EulerAdapterMock", constructor_args=[], label="deploy L1 euler adapter")
        return StepResult(changed=True, tx_hash=tx_hash, outputs={"l1EulerAdapter": address})

    steps.append(
        DeployStep(
            chain="l1",
            target="euler_adapter",
            action="ensure",
            signer_role="deployer",
            precondition="upgrade resolves from existing vault, fresh deploys mock if unset",
            postcondition="lending adapter address is known",
            output_fields=["l1EulerAdapter"],
            when=lambda _: True,
            execute=deploy_or_reuse_euler,
        )
    )

    def ensure_endpoint(rt: DeploymentRuntime) -> StepResult:
        if mode == "upgrade":
            value = rt.call("CollarVaultMessenger", cfg.existing_messenger, "endpoint")
            if not value:
                raise ValueError("failed to resolve endpoint() from existing L1 messenger")
            return StepResult(changed=False, outputs={"l1LzEndpoint": _normalize_addr(value)})
        if _nonzero_addr(cfg.lz_endpoint):
            return StepResult(changed=False, outputs={"l1LzEndpoint": _normalize_addr(cfg.lz_endpoint)})
        address, tx_hash = rt.deploy_contract(role="deployer", contract_name="LZEndpointV2Mock", constructor_args=[], label="deploy L1 LZ endpoint mock")
        return StepResult(changed=True, tx_hash=tx_hash, outputs={"l1LzEndpoint": address})

    steps.append(
        DeployStep(
            chain="l1",
            target="lz_endpoint",
            action="ensure",
            signer_role="deployer",
            precondition="upgrade resolves endpoint from existing messenger",
            postcondition="L1 endpoint address is known",
            output_fields=["l1LzEndpoint"],
            when=lambda _: True,
            execute=ensure_endpoint,
        )
    )

    def deploy_vault_impl(rt: DeploymentRuntime) -> StepResult:
        address, tx_hash = rt.deploy_contract(role="deployer", contract_name="CollarVault", constructor_args=[], label="deploy L1 vault implementation")
        return StepResult(changed=True, tx_hash=tx_hash, outputs={"l1VaultImplementation": address})

    steps.append(
        DeployStep(
            chain="l1",
            target="vault_impl",
            action="deploy",
            signer_role="deployer",
            precondition="artifacts built",
            postcondition="new L1 vault implementation exists",
            output_fields=["l1VaultImplementation"],
            when=lambda _: True,
            execute=deploy_vault_impl,
        )
    )

    def ensure_vault_proxy(rt: DeploymentRuntime) -> StepResult:
        impl = rt.addrs["l1VaultImplementation"]
        liquidity = rt.addrs["l1LiquidityVault"]
        euler = rt.addrs["l1EulerAdapter"]
        if mode == "fresh":
            if not _nonzero_addr(cfg.l2_recipient):
                raise ValueError("L2_RECIPIENT required for fresh L1 deployment")
            init_data = rt.encode_call(
                "CollarVault",
                "initialize",
                cfg.vault_owner,
                _normalize_addr(liquidity),
                _normalize_addr(euler),
                cfg.permit2,
                _normalize_addr(cfg.l2_recipient),
                cfg.treasury,
            )
            proxy_addr, tx_hash = rt.deploy_contract(
                role="deployer",
                contract_name="TransparentUpgradeableProxy",
                constructor_args=[_normalize_addr(impl), cfg.proxy_admin_owner, init_data],
                label="deploy L1 vault proxy",
            )
            return StepResult(
                changed=True,
                tx_hash=tx_hash,
                outputs={
                    "l1Vault": proxy_addr,
                    "l1VaultProxy": proxy_addr,
                    "l1VaultProxyAdmin": rt.proxy_admin_of(proxy_addr),
                },
            )

        proxy_admin = rt.proxy_admin_of(cfg.existing_vault)
        proxy_admin_contract = rt.contract("ProxyAdmin", proxy_admin)
        tx_hash = rt.send_contract_tx(
            role="proxy_admin",
            contract_call=proxy_admin_contract.functions.upgradeAndCall(
                _normalize_addr(cfg.existing_vault),
                _normalize_addr(impl),
                b"",
            ),
            label="upgrade L1 vault proxy",
        )
        return StepResult(
            changed=True,
            tx_hash=tx_hash,
            outputs={
                "l1Vault": _normalize_addr(cfg.existing_vault),
                "l1VaultProxy": _normalize_addr(cfg.existing_vault),
                "l1VaultProxyAdmin": proxy_admin,
            },
        )

    steps.append(
        DeployStep(
            chain="l1",
            target="vault_proxy",
            action="deploy_or_upgrade",
            signer_role="proxy_admin" if mode == "upgrade" else "deployer",
            precondition="vault implementation exists",
            postcondition="vault proxy address is current",
            output_fields=["l1Vault", "l1VaultProxy", "l1VaultProxyAdmin"],
            when=lambda _: True,
            execute=ensure_vault_proxy,
        )
    )

    def deploy_messenger_impl(rt: DeploymentRuntime) -> StepResult:
        endpoint = rt.addrs["l1LzEndpoint"]
        address, tx_hash = rt.deploy_contract(
            role="deployer",
            contract_name="CollarVaultMessenger",
            constructor_args=[_normalize_addr(endpoint)],
            label="deploy L1 messenger implementation",
        )
        return StepResult(changed=True, tx_hash=tx_hash, outputs={"l1MessengerImplementation": address})

    steps.append(
        DeployStep(
            chain="l1",
            target="messenger_impl",
            action="deploy",
            signer_role="deployer",
            precondition="endpoint exists",
            postcondition="new L1 messenger implementation exists",
            output_fields=["l1MessengerImplementation"],
            when=lambda _: True,
            execute=deploy_messenger_impl,
        )
    )

    def ensure_messenger_proxy(rt: DeploymentRuntime) -> StepResult:
        impl = rt.addrs["l1MessengerImplementation"]
        endpoint = rt.addrs["l1LzEndpoint"]
        if mode == "fresh":
            init_data = rt.encode_call(
                "CollarVaultMessenger",
                "initialize",
                cfg.admin,
                _normalize_addr(rt.addrs["l1Vault"]),
                _normalize_addr(endpoint),
                cfg.l2_eid,
            )
            proxy_addr, tx_hash = rt.deploy_contract(
                role="deployer",
                contract_name="TransparentUpgradeableProxy",
                constructor_args=[_normalize_addr(impl), cfg.proxy_admin_owner, init_data],
                label="deploy L1 messenger proxy",
            )
            return StepResult(
                changed=True,
                tx_hash=tx_hash,
                outputs={
                    "l1Messenger": proxy_addr,
                    "l1MessengerProxyAdmin": rt.proxy_admin_of(proxy_addr),
                },
            )

        proxy_admin = rt.proxy_admin_of(cfg.existing_messenger)
        proxy_admin_contract = rt.contract("ProxyAdmin", proxy_admin)
        tx_hash = rt.send_contract_tx(
            role="proxy_admin",
            contract_call=proxy_admin_contract.functions.upgradeAndCall(
                _normalize_addr(cfg.existing_messenger),
                _normalize_addr(impl),
                b"",
            ),
            label="upgrade L1 messenger proxy",
        )
        return StepResult(
            changed=True,
            tx_hash=tx_hash,
            outputs={
                "l1Messenger": _normalize_addr(cfg.existing_messenger),
                "l1MessengerProxyAdmin": proxy_admin,
            },
        )

    steps.append(
        DeployStep(
            chain="l1",
            target="messenger_proxy",
            action="deploy_or_upgrade",
            signer_role="proxy_admin" if mode == "upgrade" else "deployer",
            precondition="messenger implementation exists",
            postcondition="messenger proxy address is current",
            output_fields=["l1Messenger", "l1MessengerProxyAdmin"],
            when=lambda _: True,
            execute=ensure_messenger_proxy,
        )
    )

    for contract_name, output_key, label in (
        ("CollarVaultFinalizeModule", "l1FinalizeModule", "deploy L1 finalize module"),
        ("CollarVaultSettleModule", "l1SettleModule", "deploy L1 settle module"),
        ("CollarVaultRolloverModule", "l1RolloverModule", "deploy L1 rollover module"),
    ):
        steps.append(
            DeployStep(
                chain="l1",
                target=output_key,
                action="deploy",
                signer_role="deployer",
                precondition="artifacts built",
                postcondition=f"{output_key} address exists",
                output_fields=[output_key],
                when=lambda _, key=output_key: True,
                execute=lambda rt, name=contract_name, key=output_key, step_label=label: StepResult(
                    changed=True,
                    tx_hash=(lambda deployed: deployed[1])(rt.deploy_contract(role="deployer", contract_name=name, constructor_args=[], label=step_label)),
                    outputs={key: (lambda deployed: deployed[0])(rt.deploy_contract(role="deployer", contract_name=name, constructor_args=[], label=step_label))},
                ),
            )
        )

    # Replace module steps with explicit closures to avoid double deploys.
    steps = steps[:-3]

    def _module_step(contract_name: str, output_key: str, label: str) -> DeployStep:
        def _exec(rt: DeploymentRuntime) -> StepResult:
            address, tx_hash = rt.deploy_contract(role="deployer", contract_name=contract_name, constructor_args=[], label=label)
            return StepResult(changed=True, tx_hash=tx_hash, outputs={output_key: address})

        return DeployStep(
            chain="l1",
            target=output_key,
            action="deploy",
            signer_role="deployer",
            precondition="artifacts built",
            postcondition=f"{output_key} address exists",
            output_fields=[output_key],
            when=lambda _: True,
            execute=_exec,
        )

    steps.extend(
        [
            _module_step("CollarVaultFinalizeModule", "l1FinalizeModule", "deploy L1 finalize module"),
            _module_step("CollarVaultSettleModule", "l1SettleModule", "deploy L1 settle module"),
            _module_step("CollarVaultRolloverModule", "l1RolloverModule", "deploy L1 rollover module"),
        ]
    )

    def execute_steps() -> list[dict[str, Any]]:
        executed: list[dict[str, Any]] = []
        for step in steps:
            if not step.when(runtime):
                continue
            plan_entry = step.to_dict()
            if not broadcast:
                executed.append({**plan_entry, "changed": True, "dryRun": True})
                continue
            result = step.execute(runtime)
            runtime.addrs.update(result.outputs)
            runtime.persist()
            executed.append({**plan_entry, "changed": result.changed, "txHash": result.tx_hash})
        return executed

    executed = execute_steps()

    if broadcast:
        vault_addr = runtime.addrs["l1Vault"]
        messenger_addr = runtime.addrs["l1Messenger"]
        liquidity = runtime.addrs["l1LiquidityVault"]
        vault = runtime.contract("CollarVault", vault_addr)
        messenger = runtime.contract("CollarVaultMessenger", messenger_addr)
        liquidity_vault = runtime.contract("CollarLiquidityVault", liquidity)

        if not liquidity_vault.functions.hasRole(liquidity_vault.functions.VAULT_ROLE().call(), _normalize_addr(vault_addr)).call():
            runtime.send_contract_tx(
                role="deployer",
                contract_call=liquidity_vault.functions.grantRole(liquidity_vault.functions.VAULT_ROLE().call(), _normalize_addr(vault_addr)),
                label="grant L1 liquidity vault role",
            )

        if _normalize_addr(vault.functions.lzMessenger().call()) != _normalize_addr(messenger_addr):
            runtime.send_contract_tx(
                role="deployer",
                contract_call=vault.functions.setLZMessenger(_normalize_addr(messenger_addr)),
                label="set L1 vault messenger",
            )

        if cfg.derive_subaccount_id and int(vault.functions.deriveSubaccountId().call()) != cfg.derive_subaccount_id:
            runtime.send_contract_tx(
                role="deployer",
                contract_call=vault.functions.setDeriveSubaccountId(cfg.derive_subaccount_id),
                label="set L1 derive subaccount id",
            )

        if _normalize_addr(vault.functions.finalizeModule().call()) != _normalize_addr(runtime.addrs["l1FinalizeModule"]):
            runtime.send_contract_tx(
                role="deployer",
                contract_call=vault.functions.setFinalizeModule(_normalize_addr(runtime.addrs["l1FinalizeModule"])),
                label="set L1 finalize module",
            )
        if _normalize_addr(vault.functions.settleModule().call()) != _normalize_addr(runtime.addrs["l1SettleModule"]):
            runtime.send_contract_tx(
                role="deployer",
                contract_call=vault.functions.setSettleModule(_normalize_addr(runtime.addrs["l1SettleModule"])),
                label="set L1 settle module",
            )
        runtime.send_contract_tx(
            role="deployer",
            contract_call=vault.functions.setRolloverModule(_normalize_addr(runtime.addrs["l1RolloverModule"])),
            label="set L1 rollover module",
        )

        if cfg.rfq_signer:
            rfq_role = vault.functions.RFQ_SIGNER_ROLE().call()
            if not vault.functions.hasRole(rfq_role, _normalize_addr(cfg.rfq_signer)).call():
                runtime.send_contract_tx(
                    role="deployer",
                    contract_call=vault.functions.grantRole(rfq_role, _normalize_addr(cfg.rfq_signer)),
                    label="grant L1 RFQ signer",
                )

        runtime.addrs["l1WethAdapter"] = runtime.addrs.get("l1WethAdapter", ZERO_ADDRESS)
        if _nonzero_addr(cfg.weth_asset):
            if not _nonzero_addr(cfg.l2_wrapped_weth_asset):
                raise ValueError("L2_WRAPPED_WETH_ASSET required when WETH_ASSET is set")
            allowed = bool(vault.functions.collateralAllowed(_normalize_addr(cfg.weth_asset)).call())
            scale_now = int(vault.functions.strikeScale(_normalize_addr(cfg.weth_asset)).call())
            l2_asset_now = _normalize_addr(vault.functions.l2MessageAsset(_normalize_addr(cfg.weth_asset)).call())
            if (not allowed) or scale_now != cfg.weth_strike_scale or l2_asset_now != _normalize_addr(cfg.l2_wrapped_weth_asset):
                runtime.send_contract_tx(
                    role="deployer",
                    contract_call=vault.functions.setCollateralConfig(
                        _normalize_addr(cfg.weth_asset),
                        True,
                        cfg.weth_strike_scale,
                        _normalize_addr(cfg.l2_wrapped_weth_asset),
                    ),
                    label="set L1 WETH collateral config",
                )

            existing_adapter = runtime.addrs.get("l1WethAdapter", "")
            adapter_addr = existing_adapter if _nonzero_addr(str(existing_adapter)) and runtime.has_code(str(existing_adapter)) else ""
            if not adapter_addr and _nonzero_addr(cfg.weth_socket_connector):
                if _nonzero_addr(cfg.weth_socket_vault):
                    adapter_addr, _ = runtime.deploy_contract(
                        role="deployer",
                        contract_name="SocketBridgeAdapterOld",
                        constructor_args=[
                            _normalize_addr(cfg.weth_asset),
                            _normalize_addr(cfg.weth_socket_vault),
                            _normalize_addr(cfg.weth_socket_connector),
                            cfg.weth_msg_gas_limit,
                        ],
                        label="deploy L1 WETH socket adapter old",
                    )
                elif _nonzero_addr(cfg.weth_socket_bridge):
                    adapter_addr, _ = runtime.deploy_contract(
                        role="deployer",
                        contract_name="SocketBridgeAdapterNew",
                        constructor_args=[
                            _normalize_addr(cfg.weth_asset),
                            _normalize_addr(cfg.weth_socket_bridge),
                            _normalize_addr(cfg.weth_socket_connector),
                            cfg.weth_msg_gas_limit,
                            cfg.weth_payload_size,
                            b"",
                            b"",
                        ],
                        label="deploy L1 WETH socket adapter new",
                    )
                if adapter_addr:
                    runtime.addrs["l1WethAdapter"] = adapter_addr
                    runtime.send_contract_tx(
                        role="deployer",
                        contract_call=vault.functions.setSocketVaultConfig(_normalize_addr(cfg.weth_asset), _normalize_addr(adapter_addr)),
                        label="set L1 WETH socket config",
                    )

        if cfg.lz_receive_gas > 0:
            desired_options = HexBytes(encode_lz_receive_option(cfg.lz_receive_gas, cfg.lz_receive_value))
            current_options = HexBytes(messenger.functions.defaultOptions().call())
            if current_options != desired_options:
                runtime.send_contract_tx(
                    role="deployer",
                    contract_call=messenger.functions.setDefaultOptions(desired_options),
                    label="set L1 messenger default options",
                )

        runtime.persist()
        runtime.verify_pending()

    return DeploymentSummary(
        mode=mode,
        broadcast=broadcast,
        output_json=output_json,
        addrs=dict(runtime.addrs),
        meta=dict(runtime.meta),
        steps=[step.to_dict() for step in steps],
        executed_steps=executed,
    )


def run_l2_deploy(
    *,
    rpc_url: str,
    output_json: Path,
    cfg: L2Config,
    signers: dict[SignerRole, ResolvedSigner | None],
    broadcast: bool,
    verification: VerificationConfig,
) -> DeploymentSummary:
    require_cmd("forge")
    artifacts = ArtifactLoader()
    artifacts.ensure_build()
    existing = read_deployment_state(output_json)
    runtime = DeploymentRuntime(
        chain="l2",
        rpc_url=rpc_url,
        broadcast=broadcast,
        output_json=output_json,
        artifact_loader=artifacts,
        signers=signers,
        verification=verification,
        existing_state=existing,
    )

    cfg.tsa_proxy = _resolve_existing_proxy({"TSA_PROXY": cfg.tsa_proxy}, existing, "TSA_PROXY", "l2Tsa")
    cfg.receiver_proxy = _resolve_existing_proxy({"L2_RECEIVER": cfg.receiver_proxy}, existing, "L2_RECEIVER", "l2Receiver")
    mode = _infer_mode(cfg.mode, {"TSA_PROXY": cfg.tsa_proxy, "L2_RECEIVER": cfg.receiver_proxy}, runtime)
    runtime.meta["mode"] = mode
    runtime.meta["signers"] = {
        role: signer.auth_summary() for role, signer in runtime.signers.items() if signer is not None
    }

    if cfg.proxy_admin_owner and runtime.signers.get("proxy_admin") and _normalize_addr(cfg.proxy_admin_owner) != runtime.require_signer("proxy_admin").address:
        raise ValueError(
            f"PROXY_ADMIN owner {cfg.proxy_admin_owner} does not match proxy-admin signer {runtime.require_signer('proxy_admin').address}"
        )

    if mode == "upgrade":
        _preflight_proxy_admin(runtime, cfg.tsa_proxy, "TSA_PROXY")
        _preflight_proxy_admin(runtime, cfg.receiver_proxy, "L2_RECEIVER")
        if not _nonzero_addr(cfg.loan_store):
            resolved_loan_store = runtime.call("CollarTSA", cfg.tsa_proxy, "loanStore")
            if not resolved_loan_store:
                raise ValueError("LOAN_STORE missing and could not resolve loanStore() from TSA proxy")
            cfg.loan_store = _normalize_addr(resolved_loan_store)
        if not _nonzero_addr(cfg.lz_endpoint):
            resolved_endpoint = runtime.call("CollarTSAReceiver", cfg.receiver_proxy, "endpoint")
            if not resolved_endpoint:
                raise ValueError("LZ_ENDPOINT missing and could not resolve endpoint() from receiver proxy")
            cfg.lz_endpoint = _normalize_addr(resolved_endpoint)

    _validate_l2_permissions(runtime, cfg, mode)

    steps: list[DeployStep] = []

    def ensure_lz_endpoint(rt: DeploymentRuntime) -> StepResult:
        if mode == "upgrade":
            return StepResult(changed=False, outputs={"l2LzEndpoint": _normalize_addr(cfg.lz_endpoint)})
        if _nonzero_addr(cfg.lz_endpoint):
            return StepResult(changed=False, outputs={"l2LzEndpoint": _normalize_addr(cfg.lz_endpoint)})
        address, tx_hash = rt.deploy_contract(role="deployer", contract_name="LZEndpointV2Mock", constructor_args=[], label="deploy L2 LZ endpoint mock")
        return StepResult(changed=True, tx_hash=tx_hash, outputs={"l2LzEndpoint": address})

    steps.append(
        DeployStep(
            chain="l2",
            target="lz_endpoint",
            action="ensure",
            signer_role="deployer",
            precondition="upgrade resolves endpoint from existing receiver",
            postcondition="L2 endpoint address is known",
            output_fields=["l2LzEndpoint"],
            when=lambda _: True,
            execute=ensure_lz_endpoint,
        )
    )

    def ensure_loan_store(rt: DeploymentRuntime) -> StepResult:
        if _nonzero_addr(cfg.loan_store):
            return StepResult(changed=False, outputs={"l2LoanStore": _normalize_addr(cfg.loan_store)})
        address, tx_hash = rt.deploy_contract(
            role="deployer",
            contract_name="CollarLoanStore",
            constructor_args=[cfg.admin],
            label="deploy L2 loan store",
        )
        return StepResult(changed=True, tx_hash=tx_hash, outputs={"l2LoanStore": address})

    steps.append(
        DeployStep(
            chain="l2",
            target="loan_store",
            action="ensure",
            signer_role="deployer",
            precondition="upgrade resolves from existing TSA when unset",
            postcondition="loan store address is known",
            output_fields=["l2LoanStore"],
            when=lambda _: True,
            execute=ensure_loan_store,
        )
    )

    def ensure_option_risk(rt: DeploymentRuntime) -> StepResult:
        requires_dependency = mode == "fresh" or len(cfg.tsa_init_data) > 0
        if _nonzero_addr(cfg.option_risk_verifier):
            return StepResult(changed=False, outputs={"l2OptionRiskVerifier": _normalize_addr(cfg.option_risk_verifier)})
        if not requires_dependency:
            value = existing.get("l2OptionRiskVerifier") or ZERO_ADDRESS
            return StepResult(changed=False, outputs={"l2OptionRiskVerifier": value})
        address, tx_hash = rt.deploy_contract(role="deployer", contract_name="OptionRiskVerifier", constructor_args=[], label="deploy L2 option risk verifier")
        return StepResult(changed=True, tx_hash=tx_hash, outputs={"l2OptionRiskVerifier": address})

    steps.append(
        DeployStep(
            chain="l2",
            target="option_risk_verifier",
            action="ensure",
            signer_role="deployer",
            precondition="deploy only when fresh or upgrade uses init data",
            postcondition="option risk verifier address is known",
            output_fields=["l2OptionRiskVerifier"],
            when=lambda _: True,
            execute=ensure_option_risk,
        )
    )

    def ensure_rfq_verifier(rt: DeploymentRuntime) -> StepResult:
        requires_dependency = mode == "fresh" or len(cfg.tsa_init_data) > 0
        if _nonzero_addr(cfg.rfq_verifier):
            return StepResult(changed=False, outputs={"l2RfqVerifier": _normalize_addr(cfg.rfq_verifier)})
        if not requires_dependency:
            value = existing.get("l2RfqVerifier") or ZERO_ADDRESS
            return StepResult(changed=False, outputs={"l2RfqVerifier": value})
        address, tx_hash = rt.deploy_contract(role="deployer", contract_name="RfqVerifier", constructor_args=[], label="deploy L2 RFQ verifier")
        return StepResult(changed=True, tx_hash=tx_hash, outputs={"l2RfqVerifier": address})

    steps.append(
        DeployStep(
            chain="l2",
            target="rfq_verifier",
            action="ensure",
            signer_role="deployer",
            precondition="deploy only when fresh or upgrade uses init data",
            postcondition="RFQ verifier address is known",
            output_fields=["l2RfqVerifier"],
            when=lambda _: True,
            execute=ensure_rfq_verifier,
        )
    )

    def ensure_delegate(rt: DeploymentRuntime) -> StepResult:
        requires_dependency = mode == "fresh" or len(cfg.tsa_init_data) > 0
        if _nonzero_addr(cfg.rfq_delegate_module):
            return StepResult(changed=False, outputs={"l2RfqDelegateModule": _normalize_addr(cfg.rfq_delegate_module)})
        if not requires_dependency:
            value = existing.get("l2RfqDelegateModule") or ZERO_ADDRESS
            return StepResult(changed=False, outputs={"l2RfqDelegateModule": value})
        address, tx_hash = rt.deploy_contract(
            role="deployer",
            contract_name="CollarTsaRfqDelegateModule",
            constructor_args=[],
            label="deploy L2 RFQ delegate module",
        )
        return StepResult(changed=True, tx_hash=tx_hash, outputs={"l2RfqDelegateModule": address})

    steps.append(
        DeployStep(
            chain="l2",
            target="rfq_delegate_module",
            action="ensure",
            signer_role="deployer",
            precondition="deploy only when fresh or upgrade uses init data",
            postcondition="RFQ delegate module address is known",
            output_fields=["l2RfqDelegateModule"],
            when=lambda _: True,
            execute=ensure_delegate,
        )
    )

    def ensure_tsa_impl(rt: DeploymentRuntime) -> StepResult:
        if _nonzero_addr(cfg.tsa_implementation):
            return StepResult(changed=False, outputs={"l2TsaImplementation": _normalize_addr(cfg.tsa_implementation)})
        address, tx_hash = rt.deploy_contract(role="deployer", contract_name="CollarTSA", constructor_args=[], label="deploy L2 TSA implementation")
        return StepResult(changed=True, tx_hash=tx_hash, outputs={"l2TsaImplementation": address})

    steps.append(
        DeployStep(
            chain="l2",
            target="tsa_impl",
            action="ensure",
            signer_role="deployer",
            precondition="implementation is explicit or freshly deployed",
            postcondition="TSA implementation address is known",
            output_fields=["l2TsaImplementation"],
            when=lambda _: True,
            execute=ensure_tsa_impl,
        )
    )

    def ensure_tsa_proxy(rt: DeploymentRuntime) -> StepResult:
        impl = rt.addrs["l2TsaImplementation"]
        loan_store = rt.addrs["l2LoanStore"]
        option_risk = rt.addrs["l2OptionRiskVerifier"]
        rfq_verifier = rt.addrs["l2RfqVerifier"]
        delegate = rt.addrs["l2RfqDelegateModule"]
        init_data = cfg.tsa_init_data or _build_l2_tsa_init_data(rt, cfg, loan_store, option_risk, rfq_verifier, delegate)

        if mode == "fresh":
            proxy_addr, tx_hash = rt.deploy_contract(
                role="deployer",
                contract_name="TransparentUpgradeableProxy",
                constructor_args=[_normalize_addr(impl), cfg.proxy_admin_owner, init_data],
                label="deploy L2 TSA proxy",
            )
            return StepResult(
                changed=True,
                tx_hash=tx_hash,
                outputs={
                    "l2Tsa": proxy_addr,
                    "l2TsaProxyAdmin": rt.proxy_admin_of(proxy_addr),
                },
            )

        proxy_admin = rt.proxy_admin_of(cfg.tsa_proxy)
        proxy_admin_contract = rt.contract("ProxyAdmin", proxy_admin)
        tx_hash = rt.send_contract_tx(
            role="proxy_admin",
            contract_call=proxy_admin_contract.functions.upgradeAndCall(
                _normalize_addr(cfg.tsa_proxy),
                _normalize_addr(impl),
                init_data,
            ),
            label="upgrade L2 TSA proxy",
        )
        return StepResult(
            changed=True,
            tx_hash=tx_hash,
            outputs={
                "l2Tsa": _normalize_addr(cfg.tsa_proxy),
                "l2TsaProxyAdmin": proxy_admin,
            },
        )

    steps.append(
        DeployStep(
            chain="l2",
            target="tsa_proxy",
            action="deploy_or_upgrade",
            signer_role="proxy_admin" if mode == "upgrade" else "deployer",
            precondition="TSA implementation exists",
            postcondition="TSA proxy address is current",
            output_fields=["l2Tsa", "l2TsaProxyAdmin"],
            when=lambda _: True,
            execute=ensure_tsa_proxy,
        )
    )

    def deploy_receiver_impl(rt: DeploymentRuntime) -> StepResult:
        endpoint = rt.addrs["l2LzEndpoint"]
        address, tx_hash = rt.deploy_contract(
            role="deployer",
            contract_name="CollarTSAReceiver",
            constructor_args=[_normalize_addr(endpoint)],
            label="deploy L2 receiver implementation",
        )
        return StepResult(changed=True, tx_hash=tx_hash, outputs={"l2ReceiverImplementation": address})

    steps.append(
        DeployStep(
            chain="l2",
            target="receiver_impl",
            action="deploy",
            signer_role="deployer",
            precondition="endpoint exists",
            postcondition="receiver implementation exists",
            output_fields=["l2ReceiverImplementation"],
            when=lambda _: True,
            execute=deploy_receiver_impl,
        )
    )

    def ensure_receiver_proxy(rt: DeploymentRuntime) -> StepResult:
        impl = rt.addrs["l2ReceiverImplementation"]
        endpoint = rt.addrs["l2LzEndpoint"]
        if mode == "fresh":
            init_data = rt.encode_call(
                "CollarTSAReceiver",
                "initialize",
                cfg.admin,
                _normalize_addr(endpoint),
                cfg.socket_tracker,
                _normalize_addr(rt.addrs["l2Tsa"]),
                _normalize_addr(rt.addrs["l2LoanStore"]),
                cfg.l1_eid,
            )
            proxy_addr, tx_hash = rt.deploy_contract(
                role="deployer",
                contract_name="TransparentUpgradeableProxy",
                constructor_args=[_normalize_addr(impl), cfg.proxy_admin_owner, init_data],
                label="deploy L2 receiver proxy",
            )
            return StepResult(
                changed=True,
                tx_hash=tx_hash,
                outputs={
                    "l2Receiver": proxy_addr,
                    "l2ReceiverProxyAdmin": rt.proxy_admin_of(proxy_addr),
                },
            )

        proxy_admin = rt.proxy_admin_of(cfg.receiver_proxy)
        proxy_admin_contract = rt.contract("ProxyAdmin", proxy_admin)
        tx_hash = rt.send_contract_tx(
            role="proxy_admin",
            contract_call=proxy_admin_contract.functions.upgradeAndCall(
                _normalize_addr(cfg.receiver_proxy),
                _normalize_addr(impl),
                b"",
            ),
            label="upgrade L2 receiver proxy",
        )
        return StepResult(
            changed=True,
            tx_hash=tx_hash,
            outputs={
                "l2Receiver": _normalize_addr(cfg.receiver_proxy),
                "l2ReceiverProxyAdmin": proxy_admin,
            },
        )

    steps.append(
        DeployStep(
            chain="l2",
            target="receiver_proxy",
            action="deploy_or_upgrade",
            signer_role="proxy_admin" if mode == "upgrade" else "deployer",
            precondition="receiver implementation exists",
            postcondition="receiver proxy address is current",
            output_fields=["l2Receiver", "l2ReceiverProxyAdmin"],
            when=lambda _: True,
            execute=ensure_receiver_proxy,
        )
    )

    def execute_steps() -> list[dict[str, Any]]:
        executed: list[dict[str, Any]] = []
        for step in steps:
            if not step.when(runtime):
                continue
            plan_entry = step.to_dict()
            if not broadcast:
                executed.append({**plan_entry, "changed": True, "dryRun": True})
                continue
            result = step.execute(runtime)
            runtime.addrs.update(result.outputs)
            runtime.persist()
            executed.append({**plan_entry, "changed": result.changed, "txHash": result.tx_hash})
        return executed

    executed = execute_steps()

    runtime.addrs["l2SocketTracker"] = cfg.socket_tracker
    runtime.addrs["l2AtomicExecutor"] = cfg.atomic_executor or runtime.addrs.get("l2AtomicExecutor", ZERO_ADDRESS)

    if broadcast:
        loan_store = runtime.contract("CollarLoanStore", runtime.addrs["l2LoanStore"])
        tsa = runtime.contract("CollarTSA", runtime.addrs["l2Tsa"])
        receiver = runtime.contract("CollarTSAReceiver", runtime.addrs["l2Receiver"])

        writer_role = loan_store.functions.WRITER_ROLE().call()
        for target, label in (
            (runtime.addrs["l2Receiver"], "grant L2 receiver writer role"),
            (runtime.addrs["l2Tsa"], "grant L2 TSA writer role"),
        ):
            if not loan_store.functions.hasRole(writer_role, _normalize_addr(target)).call():
                runtime.send_contract_tx(
                    role="deployer",
                    contract_call=loan_store.functions.grantRole(writer_role, _normalize_addr(target)),
                    label=label,
                )

        if _nonzero_addr(cfg.atomic_executor):
            if not bool(tsa.functions.isSubmitter(_normalize_addr(cfg.atomic_executor)).call()):
                runtime.send_contract_tx(
                    role="deployer",
                    contract_call=tsa.functions.setSubmitter(_normalize_addr(cfg.atomic_executor), True),
                    label="set L2 TSA submitter",
                )
            if _nonzero_addr(cfg.matching):
                matching = runtime.contract("Matching", cfg.matching)
                owner = matching.functions.owner().call()
                if _normalize_addr(owner) == runtime.require_signer("deployer").address:
                    runtime.send_contract_tx(
                        role="deployer",
                        contract_call=matching.functions.setTradeExecutor(_normalize_addr(cfg.atomic_executor), True),
                        label="set L2 matching trade executor",
                    )

        if _nonzero_addr(cfg.l1_vault):
            current_recipient = _normalize_addr(receiver.functions.vaultRecipient().call())
            if current_recipient != _normalize_addr(cfg.l1_vault):
                runtime.send_contract_tx(
                    role="deployer",
                    contract_call=receiver.functions.setVaultRecipient(_normalize_addr(cfg.l1_vault)),
                    label="set L2 vault recipient",
                )

        if _nonzero_addr(cfg.l1_messenger):
            desired_peer = _peer_bytes32(cfg.l1_messenger)
            current_peer = receiver.functions.peers(cfg.l1_eid).call() if hasattr(receiver.functions, "peers") else None
            if current_peer != desired_peer:
                runtime.send_contract_tx(
                    role="deployer",
                    contract_call=receiver.functions.setPeer(cfg.l1_eid, desired_peer),
                    label="set L2 receiver peer",
                )

        bridge_configured = False
        runtime.addrs.setdefault("l2WethAdapter", ZERO_ADDRESS)
        runtime.addrs.setdefault("l2UsdcAdapter", ZERO_ADDRESS)

        if _nonzero_addr(cfg.weth_socket_bridge) or _nonzero_addr(cfg.weth_socket_connector):
            if not (_nonzero_addr(cfg.weth_socket_bridge) and _nonzero_addr(cfg.weth_socket_connector)):
                raise ValueError("WETH_SOCKET_BRIDGE and WETH_SOCKET_CONNECTOR required together")
            weth_asset = cfg.weth_asset or runtime.call("IWrappedERC20Asset", cfg.wrapped_deposit_asset, "wrappedAsset")
            if not weth_asset:
                raise ValueError("WETH_ASSET required or derivable from WRAPPED_DEPOSIT_ASSET")
            adapter = runtime.addrs.get("l2WethAdapter", "")
            if not (_nonzero_addr(str(adapter)) and runtime.has_code(str(adapter))):
                adapter = _deploy_socket_adapter_l2(
                    runtime,
                    cfg,
                    str(weth_asset),
                    cfg.weth_socket_bridge,
                    cfg.weth_socket_connector,
                    cfg.weth_msg_gas_limit,
                    cfg.weth_payload_size,
                    "deploy L2 WETH adapter",
                )
                runtime.addrs["l2WethAdapter"] = adapter
            runtime.send_contract_tx(
                role="deployer",
                contract_call=tsa.functions.setSocketBridgeConfig(_normalize_addr(str(weth_asset)), _normalize_addr(str(adapter))),
                label="set L2 WETH bridge config",
            )
            bridge_configured = True

        if _nonzero_addr(cfg.usdc_socket_bridge) or _nonzero_addr(cfg.usdc_socket_connector):
            if not (_nonzero_addr(cfg.usdc_socket_bridge) and _nonzero_addr(cfg.usdc_socket_connector)):
                raise ValueError("USDC_SOCKET_BRIDGE and USDC_SOCKET_CONNECTOR required together")
            usdc_asset = cfg.usdc_asset or runtime.call("IERC20BasedAsset", cfg.cash, "wrappedAsset")
            if not usdc_asset:
                raise ValueError("USDC_ASSET required or derivable from CASH")
            adapter = runtime.addrs.get("l2UsdcAdapter", "")
            if not (_nonzero_addr(str(adapter)) and runtime.has_code(str(adapter))):
                adapter = _deploy_socket_adapter_l2(
                    runtime,
                    cfg,
                    str(usdc_asset),
                    cfg.usdc_socket_bridge,
                    cfg.usdc_socket_connector,
                    cfg.usdc_msg_gas_limit,
                    cfg.usdc_payload_size,
                    "deploy L2 USDC adapter",
                )
                runtime.addrs["l2UsdcAdapter"] = adapter
            runtime.send_contract_tx(
                role="deployer",
                contract_call=tsa.functions.setSocketBridgeConfig(_normalize_addr(str(usdc_asset)), _normalize_addr(str(adapter))),
                label="set L2 USDC bridge config",
            )
            bridge_configured = True

        if bridge_configured:
            runtime.send_contract_tx(
                role="deployer",
                contract_call=tsa.functions.setBridgeCoordinator(_normalize_addr(runtime.addrs["l2Receiver"])),
                label="set L2 bridge coordinator",
            )

        runtime.persist()
        runtime.verify_pending()

    return DeploymentSummary(
        mode=mode,
        broadcast=broadcast,
        output_json=output_json,
        addrs=dict(runtime.addrs),
        meta=dict(runtime.meta),
        steps=[step.to_dict() for step in steps],
        executed_steps=executed,
    )
