"""Download immutable model snapshots; no model code is executed."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from huggingface_hub import HfApi, snapshot_download

manifest = Path("models.local.json")
MODELS = json.loads((manifest if manifest.exists() else Path("models.example.json")).read_text())


def download(item):
    name, spec = item
    repo = spec["repository"]
    info = HfApi().model_info(repo, revision=spec["revision"])
    folder = Path("data/models") / name
    folder.mkdir(parents=True, exist_ok=True)
    print(f"{name}: downloading {repo}@{info.sha}", flush=True)
    snapshot_download(
        repo,
        revision=info.sha,
        local_dir=folder,
        max_workers=3,
        allow_patterns=[
            "*.json",
            "*.safetensors",
            "*.npz",
            "*.model",
            "*.tiktoken",
            "*.jinja",
            "*.txt",
            "encoder/*.json",
            "tokenizer/*",
        ],
    )
    (folder / "friday-source.json").write_text(
        json.dumps({"repository": repo, "revision": info.sha}, indent=2)
    )
    print(f"{name}: complete", flush=True)


if __name__ == "__main__":
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(download, MODELS.items()))
