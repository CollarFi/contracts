#!/usr/bin/env python3
import json
import re
import sys
from pathlib import Path

EIP170_RUNTIME_LIMIT = 24_576
CONTRACT_DECL_RE = re.compile(r"^\s*(?:abstract\s+)?contract\s+([A-Za-z_][A-Za-z0-9_]*)\b", re.MULTILINE)
SKIP_PARTS = {"interfaces", "libraries", "mocks", "harness"}


def deployed_runtime_size(artifact_path: Path) -> int:
    artifact = json.loads(artifact_path.read_text())
    deployed = artifact.get("deployedBytecode", {}).get("object", "")
    if deployed.startswith("0x"):
        deployed = deployed[2:]
    if not deployed:
        return 0
    return len(deployed) // 2


def iter_project_contracts(src_root: Path):
    for source_path in sorted(src_root.rglob("*.sol")):
        if any(part in SKIP_PARTS for part in source_path.parts):
            continue
        source = source_path.read_text()
        for contract_name in CONTRACT_DECL_RE.findall(source):
            yield source_path, contract_name


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    src_root = repo_root / "src"
    out_root = repo_root / "out"

    checked = []
    failures = []

    for source_path, contract_name in iter_project_contracts(src_root):
        artifact_path = out_root / source_path.relative_to(src_root).parent / source_path.name / f"{contract_name}.json"
        if not artifact_path.exists():
            artifact_path = out_root / source_path.name / f"{contract_name}.json"
        if not artifact_path.exists():
            print(f"missing artifact for {contract_name} from {source_path}", file=sys.stderr)
            return 1

        size = deployed_runtime_size(artifact_path)
        checked.append((contract_name, source_path, size))
        if size > EIP170_RUNTIME_LIMIT:
            failures.append((contract_name, source_path, size))

    print("Checked contract runtime sizes:")
    for contract_name, source_path, size in checked:
        margin = EIP170_RUNTIME_LIMIT - size
        print(f"- {contract_name}: {size} bytes ({margin:+} margin) [{source_path}]")

    if failures:
        print("\nContracts exceeding EIP-170 runtime size limit:", file=sys.stderr)
        for contract_name, source_path, size in failures:
            print(
                f"- {contract_name}: {size} bytes ({size - EIP170_RUNTIME_LIMIT} bytes over) [{source_path}]",
                file=sys.stderr,
            )
        return 1

    print(f"\nAll project contracts are within the EIP-170 runtime size limit of {EIP170_RUNTIME_LIMIT} bytes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
