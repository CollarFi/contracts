from __future__ import annotations


def is_zero_address(addr: str) -> bool:
    return addr.lower() == "0x0000000000000000000000000000000000000000"


def is_empty_hex(blob: str) -> bool:
    b = blob.strip().lower()
    return b in {"", "0x"}


def encode_lz_receive_option(gas: int, value: int) -> str:
    # Matches OptionsBuilder.addExecutorLzReceiveOption encoding.
    if value == 0:
        return "0x000301001101" + f"{gas:032x}"
    return "0x000301001102" + f"{gas:032x}" + f"{value:032x}"


def parse_uint(value: str) -> int | None:
    s = value.strip()
    if not s or s == "N/A":
        return None
    token = s.split()[0]
    try:
        return int(token)
    except ValueError:
        return None


def decode_send_executor_config(cfg_hex: str) -> tuple[int | None, str | None]:
    """Decode abi.encode(ExecutorConfig{uint32 maxMessageSize,address executor})."""
    s = cfg_hex.strip().lower()
    if not s.startswith("0x"):
        return None, None
    raw = s[2:]
    if len(raw) < 128:
        return None, None
    try:
        max_message_size = int(raw[:64], 16)
    except ValueError:
        return None, None
    addr_word = raw[64:128]
    executor = "0x" + addr_word[-40:]
    return max_message_size, executor


def first_line(s: str) -> str:
    return s.splitlines()[0].strip()


def must_non_empty_hex(name: str, value: str) -> str:
    v = value.strip().lower()
    if v in {"", "0x", "n/a"}:
        raise ValueError(f"{name} is empty or unavailable: {value}")
    return value.strip()


def norm_hex(value: str) -> str:
    return value.strip().lower()
