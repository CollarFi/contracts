from __future__ import annotations

from pathlib import Path

from lz_harness.common import ROOT_DIR


def resolve_l1_env_path(env_profile: str, l1_env_file: Path) -> Path:
    profile = env_profile.strip().lower()
    if profile and l1_env_file == (ROOT_DIR / ".env.l1.testnet"):
        l1_env_file = ROOT_DIR / f".env.l1.{profile}"
    return l1_env_file


def resolve_l1_l2_env_paths(env_profile: str, l1_env_file: Path, l2_env_file: Path) -> tuple[Path, Path]:
    l1_env_file = resolve_l1_env_path(env_profile, l1_env_file)
    profile = env_profile.strip().lower()
    if profile and l2_env_file == (ROOT_DIR / ".env.l2.testnet"):
        l2_env_file = ROOT_DIR / f".env.l2.{profile}"
    return l1_env_file, l2_env_file


def resolve_l2_env_path(env_profile: str, l2_env_file: Path) -> Path:
    profile = env_profile.strip().lower()
    if profile and l2_env_file == (ROOT_DIR / ".env.l2.testnet"):
        l2_env_file = ROOT_DIR / f".env.l2.{profile}"
    return l2_env_file
