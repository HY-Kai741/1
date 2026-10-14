#!/usr/bin/env python3
"""构建期核验：把 scene_map 里引用的每一条法条拿去 laws.db 核对。

核验三件事，任一不过即判定该法条不可用（构建失败，退出码 1）：
  1. 法律简称能解析成库内全名（resolve_law）；
  2. 该「法律 + 条号」在库中真实存在且现行有效；
  3. 取出的原文命中 verify_any 中至少一个关键词 —— 用于发现语料
     「条号 ↔ 文本」错位（例如同名重复条目导致 57 条返回 49 条内容）。

同时检查 explain_map 是否为该法条提供了人话翻译（缺失只告警，不失败）。

用法：
    python scripts/verify_maps.py            # 人类可读报告
    python scripts/verify_maps.py --json     # 机器可读
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from legal_agent import lawkb  # noqa: E402
from legal_agent.maps import load_maps  # noqa: E402


def main(as_json: bool = False) -> int:
    maps = load_maps()
    db = lawkb.database
    ret = lawkb.retrieval

    problems: list[dict] = []
    checked = 0
    missing_explain: list[str] = []

    for scene in maps.scene_map["scenes"]:
        for ref in scene["statutes"]:
            checked += 1
            key = f"{ref['law']}|{ref['article']}"
            full = ret.resolve_law(ref["law"]) if ret else None
            if not full:
                problems.append({"scene": scene["id"], "key": key, "reason": "law_not_found"})
                continue
            rec = db.find_article(full, ref["article"])
            if not rec:
                problems.append({"scene": scene["id"], "key": key, "reason": "article_not_found"})
                continue
            text = rec["article_text"] or ""
            hits = [k for k in ref.get("verify_any", []) if k and k in text]
            if ref.get("verify_any") and not hits:
                problems.append({
                    "scene": scene["id"], "key": key, "reason": "text_mismatch",
                    "expect_any": ref["verify_any"], "got": text[:50],
                })
                continue
            if key not in maps.explain_map["map"]:
                missing_explain.append(key)

    report = {
        "checked": checked,
        "ok": checked - len(problems),
        "problems": problems,
        "missing_explain": missing_explain,
        "scenes": [
            {"id": s["id"], "name": s["name"], "statutes": len(s["statutes"])}
            for s in maps.scene_map["scenes"]
        ],
        "advice_scenes": sorted(maps.advice_map["map"].keys()),
    }

    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"核验法条：{checked} 条，通过 {report['ok']} 条")
        for s in report["scenes"]:
            print(f"  - {s['name']}({s['id']}): {s['statutes']} 条")
        if problems:
            print("\n✗ 不通过：")
            for p in problems:
                print(f"  {p['key']} [{p['reason']}] {p.get('expect_any', '')} got={p.get('got', '')}")
        else:
            print("\n✓ 全部法条真实存在且原文命中关键词")
        if missing_explain:
            print(f"\n! 缺少人话翻译（将退化为结构化要点）：{', '.join(missing_explain)}")
        print(f"\n行动建议覆盖场景：{', '.join(report['advice_scenes'])}")

    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main("--json" in sys.argv))
