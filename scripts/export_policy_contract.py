"""Export the separately mounted authenticated reference API without application startup."""
import argparse
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/api'))
from procureflow.policy_library_api import create_library_api
from fastapi import Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

bearer = HTTPBearer(auto_error=False)
def schema_identity(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    # Signature mirrors the mounted API dependency, never authenticates a request.
    raise RuntimeError("Schema-only dependency must not be invoked")


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true');args=parser.parse_args()
    path=ROOT/'packages/contracts/policy-library.openapi.json'
    data=json.dumps(create_library_api(None,schema_identity).openapi(),ensure_ascii=False,indent=2)+'\n'
    if args.check:
        return 0 if path.is_file() and path.read_text()==data else 1
    path.write_text(data);return 0


if __name__=='__main__':raise SystemExit(main())
