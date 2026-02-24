from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

L1_CHAIN_ID = 11155111
L2_CHAIN_ID = 901

L1_ANVIL_PORT = 10018
L2_ANVIL_PORT = 10019

L1_ARTIFACT_JSON = Path(f"deployments/{L1_CHAIN_ID}/l1-e2e.json")
L2_ARTIFACT_JSON = Path(f"deployments/{L2_CHAIN_ID}/l2-e2e.json")

# Sepolia collateral + debt + Socket route defaults used by fork e2e scripts.
L1_COLLATERAL_ASSET = "0xE67ABDA0D43f7AC8f37876bBF00D1DFadbB93aaa"
L1_DEBT_ASSET = "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238"
L1_WETH_SOCKET_VAULT = "0xd9cb39b5ad36c6d2ec4e8d5337b62a1c1b71bacc"
L1_WETH_SOCKET_CONNECTOR = "0x2d7F2B4CEe097F08ed8d30D928A40eB1379071Fe"
