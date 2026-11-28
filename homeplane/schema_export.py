"""将 Pydantic 模型导出为 Friday 公开 JSON Schema 的固定文本格式。"""

import json
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel


def write_schemas(destination: str | Path, models: Mapping[str, type[BaseModel]]) -> None:
    directory = Path(destination)
    directory.mkdir(parents=True, exist_ok=True)
    for name, model in models.items():
        data = json.dumps(model.model_json_schema(), indent=2) + "\n"
        (directory / f"{name}.json").write_text(data, encoding="utf-8")
