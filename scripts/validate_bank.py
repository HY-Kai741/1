"""题库与知识点校验器（入库门禁）。

用法::

    python scripts/validate_bank.py            # 全量校验（知识点 + 题库）
    python scripts/validate_bank.py --kp       # 只校验知识点体系
    python scripts/validate_bank.py --bank     # 只校验题库

入库门禁（plan-02 §5.3）::

    入库 = 结构合法 ∧ 答案非空 ∧ citations 全部回库核验通过
           ∧ knowledge_points 存在 ∧ license 已确认

任一不满足即拒绝入库并计数；**通过标准是 0 个问题**。
退出码：0 = 通过，1 = 存在问题，2 = 题库不存在。

报告写入 ``reports/bank-report.json``。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from legal_agent import lawkb  # noqa: E402
from legal_agent.lawkb import database as db  # noqa: E402
from legal_agent.lawkb import retrieval as ret  # noqa: E402

DATA_DIR = ROOT / "legal_agent" / "data"
BANK_DIR = ROOT / "data" / "questions"
REPORT_PATH = ROOT / "reports" / "bank-report.json"

REQUIRED_FIELDS = (
    "id", "type", "stem", "options", "answer",
    "explanation", "citations", "knowledge_points", "source",
)
VALID_SCENES = {"rental", "parttime", "consumer", "ip", "loan",
                "network", "marriage", "injury"}
VALID_CLASSES = {"KD", "CA"}
VALID_TYPES = {"single_choice", "multi_choice"}


def _utf8_stdout() -> None:
    try:
        if (sys.stdout.encoding or "").lower() != "utf-8":
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass


def lookup_statute(law, article) -> tuple[bool, str]:
    """回库核验一条法条。返回 ``(是否通过, 库内全名或失败原因)``。

    两步走（与运行时一致）：先把简称解析为库内全名，再按全名 + 条号取现行条文。
    """
    try:
        art_no = int(article)
    except (TypeError, ValueError):
        return False, f"条号非数字：{article!r}"
    full = ret.resolve_law(str(law or ""))
    if not full:
        return False, f"法律名无法解析：{law!r}"
    rec = db.find_article(full, art_no)
    if not rec:
        return False, f"{full} 第{art_no}条 不存在"
    if not (rec.get("article_text") or "").strip():
        return False, f"{full} 第{art_no}条 文本为空"
    return True, full


def get_statute_text(law, article) -> str | None:
    """取法条原文（供出题流水线使用）。"""
    try:
        art_no = int(article)
    except (TypeError, ValueError):
        return None
    full = ret.resolve_law(str(law or ""))
    if not full:
        return None
    rec = db.find_article(full, art_no)
    return (rec.get("article_text") or "").strip() or None if rec else None


# --------------------------------------------------------------- 知识点

def load_knowledge_points() -> tuple[list[dict], list[str]]:
    path = DATA_DIR / "knowledge_points.json"
    problems: list[str] = []
    if not path.is_file():
        return [], [f"知识点文件缺失：{path}"]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return [], [f"知识点 JSON 解析失败：{exc}"]
    kps = raw.get("knowledge_points") or []
    if not kps:
        problems.append("知识点列表为空")
    return kps, problems


def check_knowledge_points(kps: list[dict]) -> tuple[dict, list[str]]:
    """校验知识点：id 唯一、场景合法、锚定法条回库可查。"""
    problems: list[str] = []
    seen: set[str] = set()
    statute_hits: set[tuple[str, int]] = set()

    for kp in kps:
        kid = kp.get("id") or "<无 id>"
        if kid in seen:
            problems.append(f"[kp] id 重复：{kid}")
        seen.add(kid)

        if kp.get("scene") not in VALID_SCENES:
            problems.append(f"[kp] {kid} 场景非法：{kp.get('scene')}")

        statutes = kp.get("statutes") or []
        if not statutes:
            problems.append(f"[kp] {kid} 未锚定任何法条")
        for st in statutes:
            law, art = st.get("law"), st.get("article")
            if not law or art is None:
                problems.append(f"[kp] {kid} 法条条目不完整：{st}")
                continue
            ok, detail = lookup_statute(law, art)
            if not ok:
                problems.append(f"[kp] {kid} 法条回库核验失败：{detail}")
            else:
                statute_hits.add((detail, int(art)))

    stats = {
        "knowledge_points": len(kps),
        "distinct_statutes": len(statute_hits),
        "by_scene": dict(Counter(kp.get("scene") for kp in kps)),
        "by_bloom": dict(Counter(kp.get("bloom") for kp in kps)),
    }
    return stats, problems


# --------------------------------------------------------------- 题库

def load_questions() -> list[tuple[str, dict]]:
    """读取 data/questions/*.jsonl，返回 (来源文件, 题目) 列表。"""
    rows: list[tuple[str, dict]] = []
    if not BANK_DIR.is_dir():
        return rows
    for f in sorted(BANK_DIR.glob("*.jsonl")):
        for lineno, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            try:
                rows.append((f"{f.name}:{lineno}", json.loads(line)))
            except ValueError as exc:
                rows.append((f"{f.name}:{lineno}", {"__parse_error__": str(exc)}))
    return rows


def check_questions(rows: list[tuple[str, dict]], kp_ids: set[str]) -> tuple[dict, list[str]]:
    problems: list[str] = []
    seen_ids: set[str] = set()
    seen_stems: dict[str, str] = {}
    ok_count = 0

    for src, q in rows:
        if "__parse_error__" in q:
            problems.append(f"[q] {src} JSON 解析失败：{q['__parse_error__']}")
            continue

        qid = q.get("id") or f"<无 id @ {src}>"

        missing = [k for k in REQUIRED_FIELDS if not q.get(k)]
        if missing:
            problems.append(f"[q] {qid} 缺字段：{', '.join(missing)}")
            continue

        if qid in seen_ids:
            problems.append(f"[q] {qid} id 重复")
        seen_ids.add(qid)

        if q.get("type") not in VALID_TYPES:
            problems.append(f"[q] {qid} 题型非法：{q.get('type')}")

        options = q.get("options") or []
        keys = {o.get("key") for o in options if isinstance(o, dict)}
        if len(options) != 4 or len(keys) != 4:
            problems.append(f"[q] {qid} 选项不是 4 个互异选项")

        answer = q.get("answer") or []
        if not answer:
            problems.append(f"[q] {qid} 答案为空")
        elif not set(answer) <= keys:
            problems.append(f"[q] {qid} 答案不在选项内：{answer}")

        if q.get("question_class") not in VALID_CLASSES:
            problems.append(f"[q] {qid} question_class 非法：{q.get('question_class')}")

        difficulty = q.get("difficulty")
        if not isinstance(difficulty, int) or not 1 <= difficulty <= 5:
            problems.append(f"[q] {qid} 难度非法：{difficulty}")

        # 门禁：citations 全部回库核验
        citations = q.get("citations") or []
        if not citations:
            problems.append(f"[q] {qid} 无 citations（法条引用）")
        for c in citations:
            ok, detail = lookup_statute(c.get("law"), c.get("article"))
            if not ok:
                problems.append(f"[q] {qid} citation 回库核验失败：{detail}")

        # 门禁：knowledge_points 必须存在
        for kid in q.get("knowledge_points") or []:
            if kid not in kp_ids:
                problems.append(f"[q] {qid} 引用未知知识点：{kid}")

        # 门禁：license 已确认
        source = q.get("source") or {}
        lic = (source.get("license") or "").strip()
        if not lic or lic in {"待核实", "unknown", "TODO"}:
            problems.append(f"[q] {qid} 许可未确认：{lic or '(空)'}")

        # 查重：题干完全相同
        stem = (q.get("stem") or "").strip()
        if stem in seen_stems:
            problems.append(f"[q] {qid} 题干与 {seen_stems[stem]} 完全相同")
        else:
            seen_stems[stem] = qid

        ok_count += 1

    stats = {
        "questions": len(rows),
        "passed": ok_count,
        "by_scene": dict(Counter(
            (q.get("knowledge_points") or ["?"])[0].split(".")[1]
            for _, q in rows if q.get("knowledge_points") and len((q.get("knowledge_points") or ["?"])[0].split(".")) > 1
        )),
        "by_type": dict(Counter(q.get("type") for _, q in rows)),
        "by_class": dict(Counter(q.get("question_class") for _, q in rows)),
        "by_difficulty": dict(sorted(Counter(
            q.get("difficulty") for _, q in rows if isinstance(q.get("difficulty"), int)
        ).items())),
    }
    return stats, problems


# --------------------------------------------------------------- 主流程

def main() -> int:
    _utf8_stdout()
    ap = argparse.ArgumentParser(description="题库与知识点校验器")
    ap.add_argument("--kp", action="store_true", help="只校验知识点体系")
    ap.add_argument("--bank", action="store_true", help="只校验题库")
    args = ap.parse_args()
    do_kp = args.kp or not args.bank
    do_bank = args.bank or not args.kp

    report: dict = {}
    problems: list[str] = []

    kps, kp_load_problems = load_knowledge_points()
    problems.extend(kp_load_problems)
    kp_ids = {kp.get("id") for kp in kps}

    if do_kp:
        kp_stats, kp_problems = check_knowledge_points(kps)
        report["knowledge_points"] = kp_stats
        problems.extend(kp_problems)

    if do_bank:
        rows = load_questions()
        if not BANK_DIR.is_dir():
            print(f"题库目录不存在：{BANK_DIR}")
            print("→ 运行 `python scripts/generate_questions.py` 生成首批题目。")
            return 2
        if not rows:
            print(f"题库为空：{BANK_DIR} 下没有 *.jsonl")
            print("→ 运行 `python scripts/generate_questions.py` 生成首批题目。")
            return 2
        q_stats, q_problems = check_questions(rows, kp_ids)
        report["questions"] = q_stats
        problems.extend(q_problems)

    report["problem_count"] = len(problems)
    report["passed"] = not problems
    report["problems"] = problems[:200]

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n报告已写入：{REPORT_PATH}")
    if problems:
        print(f"\n未通过：共 {len(problems)} 个问题（仅显示前 20 条）")
        for p in problems[:20]:
            print("  -", p)
        return 1
    print("\n通过：0 个问题")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
