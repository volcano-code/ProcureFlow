"""Regenerate or check API contracts using a disposable, offline application."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_NAMES = ("openapi.json", "quotation.schema.json", "request.schema.json")

# Importing procureflow.app constructs its default application. Keep that import in
# a fresh interpreter so an already-imported app or ambient settings cannot select
# the user's database or ERP. No endpoint or application lifespan is invoked.
_SCHEMA_EXPORT = """
import json
from procureflow.app import app
from procureflow.contracts import QuoteValues, RequestCreate
try:
    print(json.dumps({
        "openapi.json": app.openapi(),
        "quotation.schema.json": QuoteValues.model_json_schema(),
        "request.schema.json": RequestCreate.model_json_schema(),
    }, ensure_ascii=False))
finally:
    app.state.service.db.engine.dispose()
"""


def generate_contracts() -> dict[str, bytes]:
    """Build all contracts without reading application credentials or user data."""
    with tempfile.TemporaryDirectory(prefix="pf-contracts-") as directory:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("PF_", "ERP_", "LLM_"))}
        env.update(
            PYTHONPATH=str(ROOT / "services/api"),
            PYTHONHASHSEED="0",
            PF_DATA_DIR=directory,
            PF_DATABASE_URL=f"sqlite:///{Path(directory) / 'contracts.sqlite3'}",
            PF_MODE="demo",
            PF_ERP_MODE="mock",
            ERP_ALLOW_DRAFT_WRITES="false",
        )
        result = subprocess.run(
            [sys.executable, "-c", _SCHEMA_EXPORT], cwd=directory, env=env,
            capture_output=True, text=True, encoding="utf-8", check=True, timeout=60,
        )
        schemas = json.loads(result.stdout)
    if not isinstance(schemas, dict) or set(schemas) != set(CONTRACT_NAMES) or any(
            not isinstance(schemas[name], dict) for name in CONTRACT_NAMES):
        raise ValueError("Incomplete contract export")
    # Preserve the generator's field order for reviewable diffs. Python's ordered
    # dicts, fixed hash seed, pinned dependencies and byte-level checks make the
    # output reproducible, including its UTF-8 encoding and final newline.
    return {name: (json.dumps(schemas[name], ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            for name in CONTRACT_NAMES}


def run(output: Path, *, check: bool = False) -> int:
    contracts = generate_contracts()
    if check:
        drift = []
        for name, expected in contracts.items():
            path = output / name
            if not path.is_file():
                drift.append(f"{name} (missing)")
            elif path.read_bytes() != expected:
                drift.append(f"{name} (changed)")
        if drift:
            for item in drift:
                print(f"Contract drift: {item}", file=sys.stderr)
            print("Regenerate with python scripts/export_contracts.py and review the changes.", file=sys.stderr)
            return 1
        print("Contracts match the current API and models.")
        return 0
    output.mkdir(parents=True, exist_ok=True)
    for name, content in contracts.items():
        (output / name).write_bytes(content)
    print(f"Contracts exported to {output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail on drift without changing any contract files")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "packages/contracts",
                        help="contract directory (default: packages/contracts)")
    args = parser.parse_args(argv)
    try:
        return run(args.output_dir, check=args.check)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Contract generation failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
