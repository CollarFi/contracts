from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eth_account import Account
from eth_account.messages import encode_defunct
from hexbytes import HexBytes
from web3 import HTTPProvider, Web3
from web3.exceptions import TimeExhausted, TransactionNotFound

from .deploy_engine import ArtifactLoader
from .signers import ResolvedSigner, SignerInput, resolve_signer


class PendingTxTimeoutError(RuntimeError):
    def __init__(self, *, label: str, tx_hash: str, nonce: int, timeout_seconds: int) -> None:
        self.label = label
        self.tx_hash = tx_hash
        self.nonce = int(nonce)
        self.timeout_seconds = int(timeout_seconds)
        super().__init__(
            f"{label} pending after {self.timeout_seconds}s: tx={self.tx_hash} nonce={self.nonce}"
        )


@dataclass
class KeeperSigner:
    """Thin Web3-based signer for keeper scripts.

    This mirrors the deploy engine design:
    - uses `resolve_signer` to support Foundry keystores, raw private keys, or
      unlocked senders.
    - decrypts a keystore once per process via `private_key_hex` and caches
      the result in-memory only.
    - signs and sends transactions via Web3 / eth_account, so the private key
      is never passed to `cast`/`forge` or printed in command output.
    """

    w3: Web3
    signer: ResolvedSigner
    receipt_timeout_seconds: int = 120
    receipt_poll_seconds: float = 1.0
    _private_key_hex: str | None = None
    _nonce_cache: int | None = None

    @classmethod
    def from_env(
        cls,
        rpc_url: str,
        *,
        account: str,
        private_key: str,
        from_addr: str,
        unlocked: bool,
        password_env_keys: tuple[str, ...] = ("ACCOUNT_PASSWORD", "DEPLOYER_PASSWORD"),
    ) -> KeeperSigner | None:
        """Resolve a signer from env/CLI-style inputs.

        Returns None when no auth is configured.
        """

        rs = resolve_signer(
            "keeper",
            SignerInput(
                account=account,
                private_key=private_key,
                from_addr=from_addr,
                unlocked=unlocked,
                password_env_keys=password_env_keys,
            ),
        )
        if rs is None:
            return None
        w3 = Web3(HTTPProvider(rpc_url, request_kwargs={"timeout": 45}))
        return cls(w3=w3, signer=rs)

    @property
    def address(self) -> str:
        return self.signer.address

    def _pk(self, label: str) -> str:
        """Return the signer private key hex, caching keystore decryption.

        For unlocked signers this is not available and will raise.
        """

        if self.signer.kind == "unlocked":
            raise ValueError("unlocked signer does not expose a private key")
        if self._private_key_hex is None:
            self._private_key_hex = self.signer.private_key_hex(label)
        return self._private_key_hex

    def _next_nonce(self) -> int:
        if self._nonce_cache is None:
            self._nonce_cache = int(self.w3.eth.get_transaction_count(self.address, block_identifier="pending"))
        nonce = self._nonce_cache
        self._nonce_cache += 1
        return nonce

    def _fee_params(self) -> dict[str, int]:
        latest_block = self.w3.eth.get_block("latest")
        base_fee = latest_block.get("baseFeePerGas")
        suggested_gas_price = int(self.w3.eth.gas_price)
        if base_fee is None:
            return {"gasPrice": suggested_gas_price}

        try:
            priority_fee = int(self.w3.eth.max_priority_fee)
        except Exception:
            priority_fee = max(1, suggested_gas_price - int(base_fee))
        max_fee = max((int(base_fee) * 2) + priority_fee, suggested_gas_price)
        return {
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority_fee,
        }

    @staticmethod
    def _signed_raw_tx(signed: Any) -> bytes:
        """Return the raw signed tx bytes across eth-account versions.

        eth-account exposes `raw_transaction` on newer versions and
        `rawTransaction` on older ones.
        """

        raw = getattr(signed, "raw_transaction", None)
        if raw is None:
            raw = getattr(signed, "rawTransaction", None)
        if raw is None:
            raise AttributeError("signed transaction has neither raw_transaction nor rawTransaction")
        return bytes(raw)

    def _wait_for_receipt(self, tx_hash: HexBytes | bytes | str, *, label: str, nonce: int) -> dict[str, Any]:
        tx_hash_hex = HexBytes(tx_hash).hex()
        try:
            receipt = self.w3.eth.wait_for_transaction_receipt(
                tx_hash,
                timeout=self.receipt_timeout_seconds,
                poll_latency=self.receipt_poll_seconds,
            )
        except TimeExhausted as exc:
            raise PendingTxTimeoutError(
                label=label,
                tx_hash=tx_hash_hex,
                nonce=nonce,
                timeout_seconds=self.receipt_timeout_seconds,
            ) from exc
        if int(receipt["status"]) != 1:
            raise RuntimeError(f"{label} reverted: {tx_hash_hex}")
        return receipt

    def try_get_receipt(self, tx_hash: str) -> dict[str, Any] | None:
        try:
            return self.w3.eth.get_transaction_receipt(tx_hash)
        except TransactionNotFound:
            return None

    def send_tx(self, *, to: str, data: bytes, value_wei: int = 0, label: str) -> str:
        """Sign and send a raw transaction.

        - For unlocked signers: forwards to the node via `send_transaction`.
        - For keystore/raw-key signers: signs locally and uses
          `send_raw_transaction`.
        """

        to_addr = Web3.to_checksum_address(to)
        tx: dict[str, Any] = {
            "from": self.address,
            "to": to_addr,
            "data": HexBytes(data),
            "value": int(value_wei or 0),
            "chainId": int(self.w3.eth.chain_id),
            "nonce": self._next_nonce(),
        }
        tx.update(self._fee_params())

        # Gas estimation
        estimated = int(self.w3.eth.estimate_gas(tx))
        tx["gas"] = max(int(estimated * 1.2), estimated + 25_000)

        if self.signer.kind == "unlocked":
            tx_hash = self.w3.eth.send_transaction(tx)
        else:
            pk = self._pk(f"{label} ({self.signer.account or self.address})")
            signed = self.w3.eth.account.sign_transaction(tx, pk)
            tx_hash = self.w3.eth.send_raw_transaction(self._signed_raw_tx(signed))

        self._wait_for_receipt(tx_hash, label=label, nonce=int(tx["nonce"]))
        return HexBytes(tx_hash).hex()

    def send_contract_tx(
        self,
        *,
        contract_name: str,
        address: str,
        fn_name: str,
        args: list[Any],
        value_wei: int = 0,
        label: str,
    ) -> str:
        """Load a contract from Foundry artifacts and send a function call.

        This mirrors the deploy engine pattern but is scoped for keeper use
        (no deployment state or output-json tracking).
        """

        loader = ArtifactLoader()
        artifact = loader.load(contract_name)
        contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(address),
            abi=artifact.abi,
        )
        fn = getattr(contract.functions, fn_name)(*args)
        tx: dict[str, Any] = fn.build_transaction(
            {
                "from": self.address,
                "chainId": int(self.w3.eth.chain_id),
                "nonce": self._next_nonce(),
                "value": int(value_wei or 0),
                **self._fee_params(),
            }
        )

        estimated = int(self.w3.eth.estimate_gas(tx))
        tx["gas"] = max(int(estimated * 1.2), estimated + 25_000)

        if self.signer.kind == "unlocked":
            tx_hash = self.w3.eth.send_transaction(tx)
        else:
            pk = self._pk(f"{label} ({self.signer.account or self.address})")
            signed = self.w3.eth.account.sign_transaction(tx, pk)
            tx_hash = self.w3.eth.send_raw_transaction(self._signed_raw_tx(signed))

        self._wait_for_receipt(tx_hash, label=label, nonce=int(tx["nonce"]))
        return HexBytes(tx_hash).hex()

    def sign_hash(self, digest_hex: str, *, label: str) -> str:
        """Sign a 32-byte digest (hex string) as-is.

        This matches `cast wallet sign --no-hash`. The digest is expected to
        already be a typed-data hash or similar.
        """

        pk = self._pk(label)
        digest = HexBytes(digest_hex)
        signed = Account.signHash(digest, pk)
        return signed.signature.hex()

    def sign_message(self, message: str, *, label: str) -> str:
        """Sign a human-readable message using the Ethereum signed message prefix.

        This matches the default `cast wallet sign` behaviour (no --no-hash).
        """

        pk = self._pk(label)
        msg = encode_defunct(text=message)
        signed = Account.sign_message(msg, pk)
        return signed.signature.hex()
