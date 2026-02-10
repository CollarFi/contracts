#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

# common.py lives in script/lz_harness/, so repo root is 2 levels up.
ROOT_DIR = Path(__file__).resolve().parents[2]


class CmdError(RuntimeError):
    pass


def run(cmd: list[str], *, capture: bool = True, check: bool = True) -> str:
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
        )
    except FileNotFoundError as e:
        raise CmdError(f"command not found: {cmd[0]}") from e

    if check and proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise CmdError(f"command failed ({proc.returncode}): {' '.join(shlex.quote(c) for c in cmd)}\n{stderr}")
    return (proc.stdout or "").strip()


def require_cmd(name: str) -> None:
    run(["bash", "-lc", f"command -v {shlex.quote(name)} >/dev/null"])


def load_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise FileNotFoundError(f"env file not found: {path}")
    vals = dotenv_values(path)
    return {k: str(v) for k, v in vals.items() if k and v is not None}


def must(env: dict[str, str], key: str) -> str:
    v = env.get(key, "")
    if not v:
        raise ValueError(f"missing required variable: {key}")
    return v


def resolve_output_json(path_value: str) -> Path:
    p = Path(path_value)
    if not p.is_absolute():
        p = ROOT_DIR / p
    return p


def load_harness_address(path_value: str) -> str:
    out_path = resolve_output_json(path_value)
    if not out_path.is_file():
        raise FileNotFoundError(f"missing deployment output: {out_path}")
    data = json.loads(out_path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        if "lzHarness" in data:
            return str(data["lzHarness"])
        addrs = data.get("addrs")
        if isinstance(addrs, dict) and "lzHarness" in addrs:
            return str(addrs["lzHarness"])
    raise KeyError("Could not find lzHarness in deployment json")


def address_to_peer_bytes32(addr: str) -> str:
    return run(["cast", "abi-encode", "f(address)", addr])


def cast_call(rpc_url: str, to: str, sig: str, *args: str, allow_fail: bool = False) -> str:
    cmd = ["cast", "call", to, sig, *args, "--rpc-url", rpc_url]
    if allow_fail:
        try:
            return run(cmd)
        except CmdError:
            return "N/A"
    return run(cmd)


def cast_send(
    rpc_url: str,
    account: str,
    to: str,
    sig: str,
    *args: str,
    value_wei: str | None = None,
) -> str:
    cmd = ["cast", "send", to, sig, *args]
    if value_wei is not None:
        cmd += ["--value", value_wei]
    cmd += ["--rpc-url", rpc_url, "--account", account]
    return run(cmd)


def forge_script(
    script_target: str,
    rpc_url: str,
    account: str,
    broadcast: bool,
    env_overrides: dict[str, str],
    extra_args: list[str] | None = None,
) -> str:
    env = os.environ.copy()
    env.update(env_overrides)
    cmd = ["forge", "script", script_target, "--rpc-url", rpc_url, "--account", account]
    if broadcast:
        cmd.append("--broadcast")
    if extra_args:
        cmd.extend(extra_args)
    proc = subprocess.run(
        cmd,
        cwd=ROOT_DIR,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )
    if proc.returncode != 0:
        raise CmdError(f"forge script failed: {' '.join(cmd)}\n{proc.stdout}")
    return proc.stdout
