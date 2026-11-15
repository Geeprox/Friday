"""显式准备固定 CPU 分离模型；不运行推理、播放音频或连接音乐账号。"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from homeplane.ktv_cpu import MODEL, FILE, URL, SHA256, SIZE
from homeplane.ktv_accompaniment import _private_directory
from scripts.embodied.prepare_portable_models import prepare


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache-dir',type=Path,required=True)
    parser.add_argument('--download',action='store_true')
    args=parser.parse_args()
    cache=args.cache_dir.absolute()
    if args.download:
        for folder in [cache,cache/'models',cache/'models'/MODEL]:
            _private_directory(folder)
    result=prepare(cache/'models'/MODEL,download=args.download,manifest={FILE:(URL,SHA256,SIZE)})
    print(json.dumps(dict(result,model=MODEL,audio_started=False)))


if __name__=='__main__':
    main()
