"""场景识别：把口语描述路由到 8 个专项领域，或判定为「其他」。

规则打分（无模型）：命中关键词按 `2 + 词长` 累加，长词权重更高。
最高分低于阈值 → unknown（走 BM25 兜底并标注「仅供参考」）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .maps import load_maps

# 命中阈值：单个两字关键词（如「押金」「房东」）得分 4，达到阈值即算命中。
MATCH_THRESHOLD = 4
HIGH_CONFIDENCE = 12
MEDIUM_CONFIDENCE = 7

_GREETING = re.compile(r"^(你好|您好|hi|hello|在吗|在么|有人吗|嗨)[!！。~\s]*$", re.I)


@dataclass
class SceneResult:
    scene_id: str
    name: str
    score: int
    confidence: str
    scores: dict = field(default_factory=dict)
    matched: list = field(default_factory=list)
    is_greeting: bool = False

    @property
    def known(self) -> bool:
        return self.scene_id != "unknown"


def classify(text: str) -> SceneResult:
    maps = load_maps()
    scores: dict[str, int] = {}
    matched: dict[str, list[str]] = {}

    for scene in maps.scenes:
        s = 0
        hits: list[str] = []
        for kw in scene["keywords"]:
            if kw and kw in text:
                s += 2 + len(kw)
                hits.append(kw)
        if s:
            scores[scene["id"]] = s
            matched[scene["id"]] = hits

    if not scores:
        return SceneResult(
            scene_id="unknown", name="其他", score=0, confidence="none",
            scores={}, matched=[],
            is_greeting=bool(_GREETING.match(text)),
        )

    best_id = max(scores, key=lambda k: (scores[k], -len(k)))
    best = scores[best_id]

    if best < MATCH_THRESHOLD:
        return SceneResult("unknown", "其他", best, "low", scores, matched.get(best_id, []))

    scene = maps.scene_by_id(best_id)
    confidence = "high" if best >= HIGH_CONFIDENCE else ("medium" if best >= MEDIUM_CONFIDENCE else "low")
    return SceneResult(best_id, scene["name"] if scene else best_id, best, confidence,
                       scores, matched.get(best_id, []))
