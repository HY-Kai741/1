"""领域数据装载：场景映射、人话翻译、行动建议。

三个 JSON 都随代码发布，只读、无副作用；用 lru_cache 保证进程内只解析一次。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"


@dataclass(frozen=True)
class Maps:
    scene_map: dict
    explain_map: dict
    advice_map: dict

    @property
    def scenes(self) -> list[dict]:
        return self.scene_map["scenes"]

    def scene_by_id(self, scene_id: str) -> dict | None:
        for s in self.scenes:
            if s["id"] == scene_id:
                return s
        return None

    def explain_for(self, law_short: str, article_no: int) -> str | None:
        return self.explain_map["map"].get(f"{law_short}|{article_no}")

    def advice_for(self, scene_id: str) -> list[dict]:
        return self.advice_map["map"].get(scene_id, [])


def _read(name: str) -> dict:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def load_maps() -> Maps:
    return Maps(_read("scene_map.json"), _read("explain_map.json"), _read("advice_map.json"))
