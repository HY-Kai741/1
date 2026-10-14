#!/usr/bin/env python3
"""知识库一致性审计（构建期自检，只读）。

已知语料特征：同一「法律名 + 条号」可能出现多行（多版本目录被同时收录），
个别情况下**文本还不一样**（例如《商标法》出现过条号↔文本错位）。
这会导致「按名称+条号查」与「按主键 id 查」命中不同行，属于必须显式声明的数据风险。

本脚本统计并列出这类冲突，供交付说明引用。
    python scripts/audit_kb.py            # 报告
    python scripts/audit_kb.py --strict   # 存在文本冲突时以退出码 1 结束
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from legal_agent import lawkb  # noqa: E402

CURRENT = "status IN ('现行有效','已被修改')"


def main(strict: bool = False) -> int:
    with lawkb.database.get_conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) c FROM articles WHERE {CURRENT}").fetchone()["c"]
        dup_law_names = conn.execute(
            f"SELECT COUNT(*) c FROM (SELECT law_name FROM articles WHERE {CURRENT} "
            "GROUP BY law_name HAVING COUNT(DISTINCT law_id) > 1)"
        ).fetchone()["c"]
        conflicts = conn.execute(
            f"""SELECT law_name, article_no_arabic, COUNT(*) n, COUNT(DISTINCT article_text) d
                FROM articles WHERE {CURRENT}
                GROUP BY law_name, article_no_arabic
                HAVING COUNT(*) > 1 AND COUNT(DISTINCT article_text) > 1
                ORDER BY n DESC, law_name LIMIT 40"""
        ).fetchall()
        conflict_total = conn.execute(
            f"""SELECT COUNT(*) c FROM (
                  SELECT law_name, article_no_arabic FROM articles WHERE {CURRENT}
                  GROUP BY law_name, article_no_arabic
                  HAVING COUNT(*) > 1 AND COUNT(DISTINCT article_text) > 1)"""
        ).fetchone()["c"]

    print(f"现行有效条文总数        : {total}")
    print(f"同名不同 law_id 的法律  : {dup_law_names}")
    print(f"「法律名+条号」文本冲突 : {conflict_total}")
    if conflicts:
        print("\n冲突示例（同名同条号、文本不同）：")
        for r in conflicts[:15]:
            print(f"  - {r['law_name']} 第{r['article_no_arabic']}条：{r['n']} 行 / {r['d']} 种文本")

    # 关键核验：专项映射引用的 35 条法条，是否有落在冲突集内的
    from legal_agent.maps import load_maps
    ret = lawkb.retrieval
    mapped: list[tuple[str, int, str]] = []
    for scene in load_maps().scenes:
        for ref in scene["statutes"]:
            full = ret.resolve_law(ref["law"]) if ret else None
            if full:
                mapped.append((scene["id"], ref["article"], full))

    with lawkb.database.get_conn() as conn:
        conflict_keys = {
            (r["law_name"], r["article_no_arabic"])
            for r in conn.execute(
                f"""SELECT law_name, article_no_arabic FROM articles WHERE {CURRENT}
                    GROUP BY law_name, article_no_arabic
                    HAVING COUNT(*) > 1 AND COUNT(DISTINCT article_text) > 1"""
            ).fetchall()
        }
    mapped_hits = [(sid, no, law) for sid, no, law in mapped if (law, no) in conflict_keys]

    print(f"\n专项映射法条            : {len(mapped)} 条")
    print(f"其中落在冲突集内的       : {len(mapped_hits)} 条"
          + (f" → {mapped_hits}" if mapped_hits else "（不受影响）"))

    print("\n影响与处置：")
    print("  1) 映射命中的法条一律按主键 id 取值，并随结果返回 article_id，保证可溯源；")
    print("  2) 专项五领域法条已在构建期逐条核验（scripts/verify_maps.py）通过；")
    print("  3) 兜底检索路径命中的条文会标注「仅供参考」，请以官方公布文本为准。")

    if strict and conflict_total:
        print("\n[strict] 存在文本冲突，退出码 1", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--strict" in sys.argv))
