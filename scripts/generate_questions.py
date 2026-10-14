"""出题流水线：知识点 + 法条原文 → 模型生成 → 硬校验 → 入库。

这是解决「没有题库」的核心工具。**模型只负责起草，法条事实一律回库核验。**

流水线::

    知识点 ──► 取锚定法条原文（laws.db）
          ──► 组 prompt 调模型（llm.generate_question）
          ──► 硬校验（7 道关口，见下）
          ──► 组装记录 ──► 追加写入 data/questions/{scene}.jsonl

硬校验（任一不过即拒绝，记录原因）::

    G1 结构完整      stem 非空、options 恰好 4 个且 key 互异、answer ⊆ key
    G2 答案解析      explanation ≥ 20 字
    G3 法条不越界    正文中「第X条」引用的条号必须 ⊆ 所提供法条
    G4 不引外部来源  不得出现「司法解释/最高人民法院/会议纪要」等库外来源
    G5 无绝对判断    不得出现「一定能赢/必然胜诉/肯定可以」等
    G6 回库核验      citations 逐条回 laws.db 核验（不信模型输出）
    G7 查重          题干不得与已有题目完全相同

用法::

    python scripts/generate_questions.py --dry-run          # 不调模型，验证流程
    python scripts/generate_questions.py --scene rental --limit 5
    python scripts/generate_questions.py --per-kp 2         # 每个知识点 2 道

依赖 ``.env`` 中的 ``LLM_API_KEY`` / ``LLM_BASE_URL``（见 legal_agent/llm.py）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from legal_agent import llm  # noqa: E402
from legal_agent.lawkb import database as db  # noqa: E402
from legal_agent.lawkb import retrieval as ret  # noqa: E402

DATA_DIR = ROOT / "legal_agent" / "data"
OUT_DIR = ROOT / "data" / "questions"

VALID_TYPES = {"single_choice", "multi_choice"}
KNOWN_KEYS = ("A", "B", "C", "D")

_ARTICLE_RE = re.compile(r"第\s*([零一二三四五六七八九十百千两\d]+)\s*条")
_SOURCES_BANNED = (
    "司法解释", "最高人民法院", "最高法", "指导性案例", "指导案例",
    "会议纪要", "批复", "答复", "检察院",
)
_ABSOLUTE_BANNED = (
    "一定能赢", "必然胜诉", "肯定可以", "绝对能", "百分百", "一定能要回", "肯定能赢",
)

_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
           "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_UNITS = {"十": 10, "百": 100, "千": 1000}


def _utf8_stdout() -> None:
    try:
        if (sys.stdout.encoding or "").lower() != "utf-8":
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass


def cn_to_int(text: str) -> int | None:
    """中文数字转阿拉伯数字：「五百七十七」→ 577，「六十八」→ 68。"""
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    total = section = number = 0
    for ch in text:
        if ch in _DIGITS:
            number = _DIGITS[ch]
        elif ch in _UNITS:
            unit = _UNITS[ch]
            section += (number or 1) * unit
            number = 0
        else:
            return None
    return total + section + number


# --------------------------------------------------------------- 数据载入

def load_kps(scene: str | None = None) -> list[dict]:
    path = DATA_DIR / "knowledge_points.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    kps = raw.get("knowledge_points") or []
    return [kp for kp in kps if not scene or kp.get("scene") == scene]


def load_statutes(kp: dict) -> list[dict]:
    """取知识点锚定的法条原文。**取不到原文的知识点不得出题。**"""
    out: list[dict] = []
    for st in kp.get("statutes") or []:
        law, art = st.get("law"), st.get("article")
        try:
            art_no = int(art)
        except (TypeError, ValueError):
            continue
        full = ret.resolve_law(str(law or ""))
        if not full:
            continue
        rec = db.find_article(full, art_no)
        text = (rec.get("article_text") or "").strip() if rec else ""
        if not text:
            continue
        out.append({"law": law, "law_full": full, "article": art_no, "text": text})
    return out


def load_existing_stems() -> dict[str, list[str]]:
    """已有题干，按场景分组，用于查重。"""
    by_scene: dict[str, list[str]] = {}
    if not OUT_DIR.is_dir():
        return by_scene
    for f in sorted(OUT_DIR.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            try:
                q = json.loads(line)
            except ValueError:
                continue
            by_scene.setdefault(q.get("scene") or f.stem, []).append(
                (q.get("stem") or "").strip()
            )
    return by_scene


def next_seq(scene: str) -> int:
    f = OUT_DIR / f"{scene}.jsonl"
    if not f.is_file():
        return 1
    n = 0
    for line in f.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.strip().startswith("//"):
            n += 1
    return n + 1


# --------------------------------------------------------------- 硬校验

def validate(raw: dict, statutes: list[dict], existing_stems: list[str]) -> tuple[dict | None, list[str]]:
    """七道关口。返回 ``(清洗后的记录或 None, 问题列表)``。"""
    problems: list[str] = []

    if not isinstance(raw, dict):
        return None, ["G1 模型输出不是 JSON 对象"]

    stem = str(raw.get("stem") or "").strip()
    if len(stem) < 10:
        problems.append(f"G1 题干过短或为空（{len(stem)} 字）")

    options = raw.get("options")
    if not isinstance(options, list) or len(options) != 4:
        problems.append("G1 选项不是 4 个")
        options = []
    keys = [str(o.get("key", "")).strip().upper() for o in options if isinstance(o, dict)]
    if len(set(keys)) != 4 or set(keys) != set(KNOWN_KEYS):
        problems.append(f"G1 选项 key 非法：{keys}")
    for o in options:
        if isinstance(o, dict) and len(str(o.get("text") or "").strip()) < 2:
            problems.append("G1 存在空选项文本")

    answer = [str(a).strip().upper() for a in (raw.get("answer") or [])]
    if not answer:
        problems.append("G1 答案为空")
    elif not set(answer) <= set(keys):
        problems.append(f"G1 答案不在选项内：{answer}")
    if len(answer) > 1:
        problems.append("G1 本批仅收单选题，答案却多于 1 项")

    explanation = str(raw.get("explanation") or "").strip()
    if len(explanation) < 20:
        problems.append(f"G2 解析过短（{len(explanation)} 字）")

    body = stem + explanation + " ".join(str(o.get("text") or "") for o in options if isinstance(o, dict))

    allowed_articles = {s["article"] for s in statutes}
    mentioned: set[int] = set()
    for token in _ARTICLE_RE.findall(body):
        num = cn_to_int(token)
        if num is not None:
            mentioned.add(num)
    outside = sorted(n for n in mentioned if n not in allowed_articles)
    if outside:
        problems.append(f"G3 引用了未提供的条号：{outside}（允许 {sorted(allowed_articles)}）")

    for term in _SOURCES_BANNED:
        if term in body:
            problems.append(f"G4 出现库外来源：「{term}」")

    for term in _ABSOLUTE_BANNED:
        if term in body:
            problems.append(f"G5 出现绝对判断：「{term}」")

    difficulty = raw.get("difficulty")
    if not isinstance(difficulty, int) or not 1 <= difficulty <= 5:
        difficulty = 3

    question_class = str(raw.get("question_class") or "KD").strip().upper()
    if question_class not in {"KD", "CA"}:
        question_class = "KD"

    if stem and stem in existing_stems:
        problems.append("G7 题干与已有题目重复")

    if problems:
        return None, problems

    return {
        "stem": stem,
        "options": [{"key": str(o["key"]).strip().upper(),
                     "text": str(o["text"]).strip()} for o in options],
        "answer": answer,
        "explanation": explanation,
        "difficulty": difficulty,
        "question_class": question_class,
    }, []


def to_record(clean: dict, kp: dict, scene: str, seq: int, model: str) -> dict:
    """组装为 plan-02 §5.2 的题目结构；``citations`` 由流水线填入，不采信模型输出。"""
    return {
        "id": f"q.{scene}.{seq:04d}",
        "type": "single_choice",
        "scene": scene,
        "stem": clean["stem"],
        "options": clean["options"],
        "answer": clean["answer"],
        "explanation": clean["explanation"],
        "citations": [{"law": s["law"], "article": s["article"]} for s in kp["_statutes"]],
        "knowledge_points": [kp["id"]],
        "difficulty": clean["difficulty"],
        "question_class": clean["question_class"],
        "bloom": kp.get("bloom"),
        "source": {
            "name": "自建",
            "ref": f"由知识点 {kp['id']} 生成",
            "license": "自有",
            "generated_by": "scripts/generate_questions.py",
            "model": model,
        },
        "reviewed": False,
    }


# --------------------------------------------------------------- 主流程

def dry_run_sample() -> dict:
    """--dry-run 用的合成样本：一道合法的 + 一道故意违规的。"""
    return {
        "stem": "小张租房到期搬走后，房东以「墙皮有点旧」为由拒不退还押金。下列说法正确的是？",
        "options": [
            {"key": "A", "text": "房东可以任意扣除押金"},
            {"key": "B", "text": "租期届满承租人返还房屋后，无正当理由房东应退还押金"},
            {"key": "C", "text": "押金一律不予退还"},
            {"key": "D", "text": "必须报警才能退押金"},
        ],
        "answer": ["B"],
        "explanation": "依据民法典第七百三十三条，租赁期限届满，承租人应当返还租赁物。"
                       "房屋返还后房东若无正当理由，不应扣留押金；所谓「墙皮有点旧」需区分自然老化与人为损坏。",
        "difficulty": 2,
        "question_class": "CA",
    }


def main() -> int:
    _utf8_stdout()
    ap = argparse.ArgumentParser(description="出题流水线")
    ap.add_argument("--dry-run", action="store_true", help="不调模型，用合成样本验证流水线")
    ap.add_argument("--scene", help="只处理某个场景（rental/parttime/consumer/ip/loan）")
    ap.add_argument("--limit", type=int, default=0, help="最多生成多少道（0 = 不限）")
    ap.add_argument("--per-kp", type=int, default=1, help="每个知识点生成几道")
    ap.add_argument("--out", default=None,
                    help="输出目录（默认 data/questions；--dry-run 时默认 _dryrun_out）")
    ap.add_argument("--model", help="覆盖 LLM_MODEL")
    args = ap.parse_args()

    global OUT_DIR
    if args.out:
        OUT_DIR = Path(args.out)
    elif args.dry_run:
        OUT_DIR = ROOT / "_dryrun_out"
    if args.model:
        import os
        os.environ["LLM_MODEL"] = args.model

    kps = load_kps(args.scene)
    if not kps:
        print(f"没有匹配的知识点（scene={args.scene}）")
        return 2

    if not args.dry_run and not llm.enabled():
        print("模型通道未启用：请在项目根目录 .env 中配置 LLM_API_KEY 与 LLM_BASE_URL。")
        print("配置说明见 legal_agent/llm.py 顶部注释。")
        print("（可先用 --dry-run 验证流水线本身。）")
        return 3

    existing = load_existing_stems()
    model = llm.model_name() if llm.enabled() else "(dry-run)"

    stats = {"attempted": 0, "accepted": 0, "rejected": 0}
    rejects: list[str] = []
    records: list[dict] = []

    for kp in kps:
        statutes = load_statutes(kp)
        if not statutes:
            rejects.append(f"{kp['id']}：锚定法条取不到原文，跳过")
            continue
        kp["_statutes"] = statutes
        scene = kp["scene"]
        scene_stems = existing.setdefault(scene, [])

        for _ in range(max(1, args.per_kp)):
            if args.limit and stats["accepted"] >= args.limit:
                break
            stats["attempted"] += 1

            if args.dry_run:
                raw = dry_run_sample()
            else:
                raw = llm.generate_question(kp, _StatuteViews(statutes), scene_stems)

            if not raw:
                stats["rejected"] += 1
                rejects.append(f"{kp['id']}：模型未返回可用结果")
                continue

            clean, problems = validate(raw, statutes, scene_stems)
            if not clean:
                stats["rejected"] += 1
                rejects.append(f"{kp['id']}：{' / '.join(problems)}")
                continue

            rec = to_record(clean, kp, scene, next_seq(scene) + len(
                [r for r in records if r["scene"] == scene]
            ), model)
            records.append(rec)
            scene_stems.append(rec["stem"])
            stats["accepted"] += 1

        if args.limit and stats["accepted"] >= args.limit:
            break

    if records:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        by_scene: dict[str, list[dict]] = {}
        for r in records:
            by_scene.setdefault(r["scene"], []).append(r)
        for scene, rows in by_scene.items():
            f = OUT_DIR / f"{scene}.jsonl"
            with f.open("a", encoding="utf-8") as fh:
                for r in rows:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            print(f"写入 {f.name}：{len(rows)} 道")

    print(f"\n尝试 {stats['attempted']}，通过 {stats['accepted']}，拒绝 {stats['rejected']}")
    if rejects:
        print("\n拒绝原因（前 20 条）：")
        for r in rejects[:20]:
            print("  -", r)
    if args.dry_run:
        print("\n[dry-run] 未调用任何模型，未写入任何文件以外的事实来源。")
    if records:
        print(f"\n下一步：python scripts/validate_bank.py --bank")
    return 0 if stats["accepted"] else 1


class _StatuteViews:
    """把 dict 法条包装成带属性的对象，供 llm.generate_question 序列化。"""

    def __init__(self, rows: list[dict]):
        self._rows = rows

    def __iter__(self):
        for r in self._rows:
            yield type("S", (), {
                "law_name": r["law"],
                "article_no": str(r["article"]),
                "text": r["text"],
            })()

    def __len__(self):
        return len(self._rows)


if __name__ == "__main__":
    raise SystemExit(main())
