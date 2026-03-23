from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import dotenv_values

ROOT_DIR = Path(__file__).resolve().parents[2]


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
        raise CmdError(
            f"command failed ({proc.returncode}): {' '.join(shlex.quote(part) for part in cmd)}\n{(proc.stderr or '').strip()}"
        )
    return (proc.stdout or "").strip()


def require_cmd(name: str) -> None:
    run(["bash", "-lc", f"command -v {shlex.quote(name)} >/dev/null"])


def load_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise FileNotFoundError(f"env file not found: {path}")
    values = dotenv_values(path)
    return {key: str(value) for key, value in values.items() if key and value is not None}


def must(env: dict[str, str], key: str) -> str:
    value = env.get(key, "")
    if not value:
        raise ValueError(f"missing required variable: {key}")
    return value


def resolve_output_json(path_value: str) -> Path:
    path = Path(path_value)
    if not path.is_absolute():
        path = ROOT_DIR / path
    return path


def is_local_rpc_url(rpc_url: str) -> bool:
    parsed = urlparse(rpc_url)
    return parsed.hostname in {"127.0.0.1", "localhost", "0.0.0.0"}


def load_json_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object in {path}")
    return data


def read_deployment_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = load_json_object(path)
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
