"""Explicit public model provisioning; never called by the application."""
import argparse
import json
from pathlib import Path

MODEL = 'BAAI/bge-small-zh-v1.5'
REVISION = '7999e1d3359715c523056ef9478215996d62a620'


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--allow-public-download',action='store_true')
    args=parser.parse_args()
    if not args.allow_public_download:
        print(json.dumps({'status':'blocked','reason':'EXPLICIT_DOWNLOAD_REQUIRED','network_requests':0}));return 2
    root=Path(__file__).resolve().parents[1]
    target=args.output.resolve()
    if target.is_relative_to(root) or target.exists() and any(target.iterdir()):
        raise ValueError('Use an empty directory outside the source checkout')
    from huggingface_hub import snapshot_download
    snapshot_download(MODEL,revision=REVISION,local_dir=str(target),token=False,
        allow_patterns=['*.json','*.txt','model.safetensors','1_Pooling/config.json'],max_workers=2)
    print(json.dumps({'status':'downloaded','model':MODEL,'revision':REVISION,'paid_api_calls':0}))
    return 0


if __name__=='__main__':raise SystemExit(main())
