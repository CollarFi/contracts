from __future__ import annotations

import json
import os
from dataclasses import dataclass
from getpass import getpass
from pathlib import Path
from typing import Any, Literal

from eth_account import Account
from hexbytes import HexBytes
from web3 import Web3

from .runtime import load_json_object


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
        """Return the signer private key as a hex string.

        For keystore-backed signers, this method now caches the decrypted
        private key in-memory on first use. This means:

        - the Foundry account password is prompted for only once per
          process (unless provided via env vars), and
        - subsequent transactions reuse the already-decrypted key without
          re-opening the keystore or re-prompting for the password.

        The cached key is kept only in-process and is never printed or
        included in auth summaries.
        """

        if self.kind == "private_key":
            # Raw private key was provided directly; nothing to decrypt.
            return self.private_key

        if self.kind == "keystore":
            # If we've already decrypted this keystore once in this
            # process, reuse the cached key instead of prompting again.
            if self.private_key:
                return self.private_key

            if self.keystore_path is None:
                raise ValueError(f"missing keystore path for {self.account}")

            payload = json.loads(self.keystore_path.read_text(encoding="utf-8"))
            password = self._resolve_password(prompt_label)
            secret = Account.decrypt(payload, password)

            # Cache the decrypted key for subsequent calls. This value is
            # kept in-memory only and is never logged.
            self.private_key = HexBytes(secret).hex()
            return self.private_key
        raise ValueError("unlocked signers do not expose a private key")


def resolve_signer(role: str, cfg: SignerInput) -> ResolvedSigner | None:
    del role
    if cfg.private_key:
        addr = Account.from_key(cfg.private_key).address
        return ResolvedSigner(kind="private_key", address=Web3.to_checksum_address(addr), private_key=cfg.private_key)
    if cfg.account:
        keystore = Path.home() / ".foundry" / "keystores" / cfg.account
        if not keystore.is_file():
            raise FileNotFoundError(f"Foundry keystore not found for {cfg.account}: {keystore}")
        payload = load_json_object(keystore)
        raw_addr = payload.get("address")
        if not isinstance(raw_addr, str) or not raw_addr:
            raise ValueError(f"keystore {keystore} missing address")
        if not raw_addr.startswith("0x"):
            raw_addr = f"0x{raw_addr}"
        return ResolvedSigner(
            kind="keystore",
            address=Web3.to_checksum_address(raw_addr),
            account=cfg.account,
            keystore_path=keystore,
            password_env_keys=cfg.password_env_keys,
        )
    if cfg.unlocked and cfg.from_addr:
        from_addr = Web3.to_checksum_address(cfg.from_addr)
        return ResolvedSigner(
            kind="unlocked",
            address=from_addr,
            from_addr=from_addr,
            unlocked=True,
        )
    return None
