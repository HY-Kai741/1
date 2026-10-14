#!/usr/bin/env python3
"""评测：跑 tests/cases.jsonl，逐条断言。

全局硬断言（对所有 type=answer 的用例，一条不过即算失败）：
  G1 零编造：返回的每条法条都必须在 laws.db 中存在，且 text 与库内 article_text 逐字一致；
  G2 零编造（更强）：把 plainText 里所有「《法》第N条」引用抽出来，逐条回到库里核验存在；
  G3 6 段结构齐全：结论 / 适用法条 / 通俗解释 / 行动建议 / 需要补充的信息 / 免责声明；
  G4 免责声明原文必须出现；
  G5 不得出现绝对判断用语。

用法：
    python tests/run_eval.py                 # 全部
    python tests/run_eval.py --category normal
    python tests/run_eval.py --json
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from legal_agent import lawkb  # noqa: E402
from legal_agent.pipeline import handle  # noqa: E402
from legal_agent.presenter import DISCLAIMER  # noqa: E402

CASES = ROOT / "tests" / "cases.jsonl"
REPORT = ROOT / "tests" / "report.json"

SECTIONS = ["一、结论", "二、适用法条", "三、通俗解释", "四、行动建议", "五、需要补充的信息", "六、免责声明"]
ABSOLUTE_PHRASES = ["一定能赢", "肯定违法", "你绝对", "必然胜诉", "百分百赢", "一定会赢", "肯定能赢", "必定胜诉", "包赢"]
CITE_RE = re.compile(r"《([^》]{2,40})》\s*第\s*([一二三四五六七八九十百千零〇0-9]+)\s*条")

_CN = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
       "六": 6, "七": 7, "八": 8, "九": 9}
_UNIT = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def cn2num(raw: str) -> int | None:
    s = (raw or "").strip()
    if s.isdigit():
        return int(s)
    total = num = 0
    for ch in s:
        if ch in _CN:
            num = _CN[ch]
        elif ch in _UNIT:
            if num == 0:
                num = 1
            if _UNIT[ch] == 10000:
                total = (total + num) * 10000
            else:
                total += num * _UNIT[ch]
            num = 0
        else:
            return None
    return total + num


def verify_citation(law_short: str, article_no: int) -> dict | None:
    """先按 law_name 精确匹配（返回值本身来自库，名字是对的），再退回别名解析。

    退回到 resolve_law 时只做子串匹配，长法律名容易被解析到相似的另一部法律上——
    所以精确匹配必须放第一位，否则会误报「原文与库不一致」。
    """
    for cand in (law_short, f"中华人民共和国{law_short}"):
        try:
            with lawkb.database.get_conn() as conn:
                row = conn.execute(
                    f"SELECT {lawkb.database.ARTICLE_COLS} FROM articles "
                    "WHERE law_name=? AND article_no_arabic=? ORDER BY id LIMIT 1",
                    (cand, article_no),
                ).fetchone()
            if row:
                return dict(row)
        except Exception:
            pass

    ret = lawkb.retrieval
    full = ret.resolve_law(law_short) if ret else None
    if not full:
        rows = lawkb.database.list_laws(q=law_short)
        full = rows[0]["law_name"] if rows else None
    return lawkb.database.find_article(full, article_no) if full else None


def check_global(result: dict) -> list[str]:
    """返回失败原因列表（空 = 通过）。"""
    fails: list[str] = []
    text = result.get("plainText") or ""

    for title in SECTIONS:
        if title not in text:
            fails.append(f"G3 缺少段落「{title}」")

    if DISCLAIMER not in text:
        fails.append("G4 缺少免责声明原文")

    for phrase in ABSOLUTE_PHRASES:
        if phrase in text:
            fails.append(f"G5 出现绝对判断用语「{phrase}」")

    for s in result.get("statutes") or []:
        # 优先按 laws.db 主键 id 溯源——这是唯一能保证「拿到的就是当时那一条」的方式
        # （语料里存在同名同条号的多行，按名+号查可能命中另一行）。
        rec = None
        if s.get("article_id") is not None:
            try:
                with lawkb.database.get_conn() as conn:
                    row = conn.execute(
                        f"SELECT {lawkb.database.ARTICLE_COLS} FROM articles WHERE id=?",
                        (s["article_id"],),
                    ).fetchone()
                rec = dict(row) if row else None
            except Exception:
                rec = None
        if rec is None:
            rec = verify_citation(s["law_name"].replace("中华人民共和国", ""), s["article_no_arabic"])
        if not rec:
            fails.append(f"G1 法条不存在：{s['law_name']}第{s['article_no_arabic']}条")
            continue
        if (rec["article_text"] or "").strip() != (s["text"] or "").strip():
            fails.append(f"G1 原文与库不一致：{s['law_name']}第{s['article_no_arabic']}条")

    # 只核验「我们自己给出的引用」——即以 "- 《」开头的条目行；
    # 法条原文里出现的其他法律引用属于被引条文自身的内容，不算我们编造。
    for line in text.split("\n"):
        if not line.strip().startswith("- 《"):
            continue
        for m in CITE_RE.finditer(line):
            no = cn2num(m.group(2))
            if no is None:
                continue
            if not verify_citation(m.group(1), no):
                fails.append(f"G2 输出中引用了库中不存在的条文：《{m.group(1)}》第{m.group(2)}条")
    return fails


def run_case(case: dict) -> dict:
    message = case["message"]
    if message == "__REPEAT_3000__":
        message = "房东不退押金怎么办？" * 300

    t0 = time.perf_counter()
    result = handle(message=message, scene_id=case.get("sceneId"))
    ms = round((time.perf_counter() - t0) * 1000, 2)

    exp = case.get("expect", {})
    fails: list[str] = []

    if result.get("type") != exp.get("type"):
        fails.append(f"type 期望 {exp.get('type')}，实际 {result.get('type')}")

    if exp.get("type") == "answer":
        fails.extend(check_global(result))

        if exp.get("scene") and result.get("scene", {}).get("id") != exp["scene"]:
            fails.append(f"scene 期望 {exp['scene']}，实际 {result.get('scene', {}).get('id')}")
        if exp.get("mode") and result.get("mode") != exp["mode"]:
            fails.append(f"mode 期望 {exp['mode']}，实际 {result.get('mode')}")

        cited = {f"{s['law_name'].replace('中华人民共和国','')}|{s['article_no_arabic']}" for s in result.get("statutes", [])}
        for must in exp.get("must_cite", []):
            if must not in cited:
                fails.append(f"必须引用 {must}，实际 {sorted(cited)}")
        for ban in exp.get("must_not_cite", []):
            if ban in cited:
                fails.append(f"不得引用 {ban}")
        if exp.get("min_statutes") and len(result.get("statutes", [])) < exp["min_statutes"]:
            fails.append(f"法条数应 >= {exp['min_statutes']}，实际 {len(result.get('statutes', []))}")
        if exp.get("missing_min") and len(result.get("missing", [])) < exp["missing_min"]:
            fails.append(f"追问数应 >= {exp['missing_min']}，实际 {len(result.get('missing', []))}")
        for needle in exp.get("notes_contain", []):
            if not any(needle in n for n in result.get("notes", [])):
                fails.append(f"数据说明应包含「{needle}」")

    if exp.get("type") == "blocked" and exp.get("code") and result.get("code") != exp["code"]:
        fails.append(f"code 期望 {exp['code']}，实际 {result.get('code')}")

    return {"id": case["id"], "title": case["title"], "category": case["category"],
            "message": message[:40], "type": result.get("type"), "ms": ms, "fails": fails,
            "traceId": result.get("traceId")}


def main(argv: list[str]) -> int:
    only = None
    if "--category" in argv:
        only = argv[argv.index("--category") + 1]

    cases = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    if only:
        cases = [c for c in cases if c.get("category") == only]

    results = [run_case(c) for c in cases]
    passed = [r for r in results if not r["fails"]]
    lat = sorted(r["ms"] for r in results)
    p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else 0

    report = {
        "total": len(results), "passed": len(passed), "failed": len(results) - len(passed),
        "p95_ms": p95, "categories": {},
        "results": results,
    }
    for r in results:
        c = report["categories"].setdefault(r["category"], {"total": 0, "passed": 0})
        c["total"] += 1
        c["passed"] += 0 if r["fails"] else 1

    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if "--json" in argv:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["failed"] == 0 else 1

    for r in results:
        flag = "PASS" if not r["fails"] else "FAIL"
        print(f"[{flag}] {r['id']:>3} {r['category']:<11} {r['title']:<16} {r['ms']:>7.1f}ms  {r['message']}")
        for f in r["fails"]:
            print(f"        └─ {f}")

    print("\n" + "=" * 66)
    for k, v in report["categories"].items():
        print(f"  {k:<12} {v['passed']}/{v['total']}")
    print(f"  合计 {report['passed']}/{report['total']} | P95 {p95:.0f}ms")
    print("=" * 66)
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
