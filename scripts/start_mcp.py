"""Launch the local stdio MCP bridge. Credentials come from environment, never CLI."""
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/api"))
from procureflow.mcp_server import main
if __name__ == "__main__":
    raise SystemExit(main())
