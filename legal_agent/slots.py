"""槽位校验：判断关键信息是否缺失，缺失则主动追问（不假设）。

规则：把「本轮消息 + 历史消息」拼成一份累计文本，对每个槽位查提示词；
任一点中即视为已提供。numeric 槽位只要出现数字即视为已提供。

最多返回 MAX_ASK 个缺失项，避免一次抛出过多问题。
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from .maps import load_maps

MAX_ASK = 3

_HINTS_PATH = Path(__file__).resolve().parent / "data" / "slot_hints.json"
_NUM = re.compile(r"\d")


@lru_cache(maxsize=1)
def _hints() -> dict:
    return json.loads(_HINTS_PATH.read_text(encoding="utf-8"))["hints"]


def _satisfied(scene_id: str, field: str, text: str) -> bool:
    spec = _hints().get(f"{scene_id}.{field}")
    if spec is None:
        return True                      # 未配置提示词的槽位不追问
    if isinstance(spec, dict):
        if spec.get("numeric"):
            return bool(_NUM.search(text))
        return True
    return any(h and h in text for h in spec)


def missing_slots(scene_id: str, cumulative_text: str) -> list[dict]:
    """返回缺失槽位（含问题与示例），最多 MAX_ASK 个。"""
    scene = load_maps().scene_by_id(scene_id)
    if not scene:
        return []
    missing: list[dict] = []
    for slot in scene.get("slots", []):
        if not _satisfied(scene_id, slot["field"], cumulative_text):
            missing.append({
                "field": slot["field"],
                "question": slot["question"],
                "example": slot.get("example", ""),
            })
        if len(missing) >= MAX_ASK:
            break
    return missing
