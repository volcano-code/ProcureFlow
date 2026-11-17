"""Create a local-only Compose secret file, without overwriting an existing file."""
import os
from pathlib import Path
import secrets

ROOT = Path(__file__).resolve().parents[1]
path = ROOT / ".env.postgres"
try:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    raise SystemExit(".env.postgres already exists; kept unchanged")
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    handle.write("# Local demo database only; ignored by Git. Do not publish this file.\n")
    handle.write("PF_PG_PASSWORD=" + secrets.token_urlsafe(36) + "\n")
    handle.write("PF_API_PORT=8000\n")
print("Created .env.postgres (password not printed); only use on a trusted local machine")
