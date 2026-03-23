from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .deployments import default_output_json
from .runtime import CmdError, load_env, load_json_object, must, require_cmd, resolve_output_json, run
from .signers import ResolvedSigner, SignerInput, resolve_signer

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
DEFAULT_PASSWORD_ENV_KEYS = ("ACCOUNT_PASSWORD", "OPS_ACCOUNT_PASSWORD")


def env_flag(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def strip_cast_units(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    return raw.split()[0]


def is_zero_address(value: str) -> bool:
    return strip_cast_units(value).lower() == ZERO_ADDRESS


def read_output_address(path_value: str, *keys: str) -> str:
    path = resolve_output_json(path_value)
    if not path.is_file():
        raise FileNotFoundError(f"deployment output not found: {path}")
    data = load_json_object(path)
    addrs = data.get("addrs", data)
    if not isinstance(addrs, dict):
        raise ValueError(f"invalid deployment output shape: {path}")
    for key in keys:
        value = addrs.get(key)
        if isinstance(value, str) and value:
            return value
    joined = ", ".join(keys)
    raise ValueError(f"missing any of [{joined}] in deployment output: {path}")


def resolve_l1_vault_address(env: dict[str, str], rpc_url: str, override: str = "") -> str:
    if override:
        return override
    if env.get("L1_VAULT"):
        return str(env["L1_VAULT"])
    output_json = env.get("OUTPUT_JSON") or default_output_json(rpc_url, "l1")
    return read_output_address(output_json, "l1VaultProxy", "l1Vault", "collarVault", "vault")


@dataclass
class OperationStep:
    name: str
    action: str
    command: str | None = None
    needs_update: bool | None = None
    current: dict[str, Any] = field(default_factory=dict)
    target: dict[str, Any] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)
    executed: bool = False
    tx: str | None = None
    skipped_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "action": self.action,
            "command": self.command,
            "needsUpdate": self.needs_update,
            "current": self.current,
            "target": self.target,
            "details": self.details,
            "executed": self.executed,
            "tx": self.tx,
            "skippedReason": self.skipped_reason,
        }


@dataclass
class OperationRuntime:
    env_file: Path
    env: dict[str, str]
    rpc_url: str
    broadcast: bool
    signer_input: SignerInput
    _resolved_signer: ResolvedSigner | None = field(default=None, init=False, repr=False)
    _resolve_attempted: bool = field(default=False, init=False, repr=False)
    _chain_id: str | None = field(default=None, init=False, repr=False)

    @classmethod
    def from_env_file(
        cls,
        env_file: Path,
        *,
        broadcast: bool,
        account: str = "",
        private_key: str = "",
        from_addr: str = "",
        unlocked: bool = False,
        password_env_keys: tuple[str, ...] = DEFAULT_PASSWORD_ENV_KEYS,
    ) -> OperationRuntime:
        env = load_env(env_file)
        signer_input = SignerInput(
            account=account or env.get("ACCOUNT", ""),
            private_key=private_key or env.get("PRIVATE_KEY", ""),
            from_addr=from_addr or env.get("FROM", ""),
            unlocked=unlocked or env_flag(env.get("UNLOCKED", "")),
            password_env_keys=password_env_keys,
        )
        return cls(
            env_file=env_file,
            env=env,
            rpc_url=must(env, "RPC_URL"),
            broadcast=broadcast,
            signer_input=signer_input,
        )

    @property
    def mode(self) -> str:
        return "broadcast" if self.broadcast else "dry-run"

    def require_cast(self) -> None:
        require_cmd("cast")

    def chain_id(self) -> str:
        if self._chain_id is None:
            self._chain_id = self.env.get("CHAIN_ID") or run(["cast", "chain-id", "--rpc-url", self.rpc_url])
        return self._chain_id

    def cast_call(self, to: str, sig: str, *args: str, allow_fail: bool = False) -> str:
        cmd = ["cast", "call", to, sig, *args, "--rpc-url", self.rpc_url]
        if allow_fail:
            try:
                return run(cmd)
            except CmdError:
                return "N/A"
        return run(cmd)

    def auth_args(self) -> list[str]:
        if self.signer_input.private_key:
            return ["--private-key", self.signer_input.private_key]
        if self.signer_input.unlocked and self.signer_input.from_addr:
            return ["--unlocked", "--from", self.signer_input.from_addr]
        if self.signer_input.account:
            return ["--account", self.signer_input.account]
        return []

    def display_auth_args(self) -> list[str]:
        if self.signer_input.private_key:
            return ["--private-key", "<redacted>"]
        if self.signer_input.unlocked and self.signer_input.from_addr:
            return ["--unlocked", "--from", self.signer_input.from_addr]
        if self.signer_input.account:
            return ["--account", self.signer_input.account]
        return ["--account", "<ACCOUNT>"]

    def require_broadcast_auth(self) -> None:
        if self.auth_args():
            return
        raise ValueError("missing auth for --broadcast: provide ACCOUNT, or --private-key, or --unlocked --from")

    def render_cast_send(self, to: str, sig: str, *args: str, value_wei: str | None = None) -> str:
        cmd = ["cast", "send", to, sig, *args]
        if value_wei is not None:
            cmd += ["--value", value_wei]
        cmd += ["--rpc-url", self.rpc_url]
        cmd += self.display_auth_args()
        return " ".join(shlex.quote(part) for part in cmd)

    def cast_send(self, to: str, sig: str, *args: str, value_wei: str | None = None) -> str:
        self.require_broadcast_auth()
        cmd = ["cast", "send", to, sig, *args]
        if value_wei is not None:
            cmd += ["--value", value_wei]
        cmd += ["--rpc-url", self.rpc_url]
        cmd += self.auth_args()
        return run(cmd)

    def resolved_signer(self) -> ResolvedSigner | None:
        if self._resolve_attempted:
            return self._resolved_signer
        self._resolve_attempted = True
        try:
            self._resolved_signer = resolve_signer("operator", self.signer_input)
        except (FileNotFoundError, ValueError):
            if self.signer_input.account and not self.signer_input.private_key and not (
                self.signer_input.unlocked and self.signer_input.from_addr
            ):
                self._resolved_signer = None
            else:
                raise
        return self._resolved_signer

    def signer_summary(self) -> dict[str, Any]:
        signer = self.resolved_signer()
        if signer is not None:
            return signer.auth_summary()
        kind = None
        address = None
        if self.signer_input.private_key:
            kind = "private_key"
        elif self.signer_input.unlocked and self.signer_input.from_addr:
            kind = "unlocked"
            address = self.signer_input.from_addr
        elif self.signer_input.account:
            kind = "account"
        return {
            "kind": kind,
            "address": address,
            "account": self.signer_input.account or None,
            "unlocked": self.signer_input.unlocked,
            "from": self.signer_input.from_addr or None,
        }


def build_operation_summary(
    runtime: OperationRuntime,
    *,
    resolved_addrs: dict[str, Any],
    steps: list[OperationStep],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary = {
        "mode": runtime.mode,
        "broadcast": runtime.broadcast,
        "signers": {"operator": runtime.signer_summary()},
        "resolvedAddrs": resolved_addrs,
        "steps": [step.to_dict() for step in steps],
        "executedSteps": [step.to_dict() for step in steps if step.executed],
    }
    if extra:
        summary.update(extra)
    return summary
