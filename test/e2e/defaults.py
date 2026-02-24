from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

L1_CHAIN_ID = 11155111
L2_CHAIN_ID = 901

L1_ANVIL_PORT = 10018
L2_ANVIL_PORT = 10019

L1_ARTIFACT_JSON = Path(f"deployments/{L1_CHAIN_ID}/l1-e2e.json")
L2_ARTIFACT_JSON = Path(f"deployments/{L2_CHAIN_ID}/l2-e2e.json")
