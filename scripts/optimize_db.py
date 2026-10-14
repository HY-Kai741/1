#!/usr/bin/env python3
"""给 data/laws.db 补一条「法律名 + 条号」复合索引（幂等）。

背景：上游库只建了 law_id 索引，而本系统按「法律全名 + 条号」直查
（`lawkb/database.find_article`），每次都是 26,196 行全表扫描（约 34 ms/次）。
一次回答要查 6 条法条，累计约 200 ms——占了整个请求耗时的大头。

本脚本只新增索引，**不改动任何条文内容**（原文逐字一致性不受影响）。
重复执行安全（IF NOT EXISTS）。

用法：
    python scripts/optimize_db.py [--db data/laws.db]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_articles_name_no "
    "ON articles(law_name, article_no_arabic)"
)
PROBE = (
    "SELECT id FROM articles WHERE law_name=? AND article_no_arabic=? "
    "AND status IN ('现行有效','已被修改') ORDER BY id LIMIT 1"
)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "laws.db"))
    args = ap.parse_args(argv)

    db = Path(args.db)
    if not db.exists():
        print(f"找不到数据库：{db}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db))
    try:
        total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]

        def probe_ms(n: int = 20) -> float:
            t0 = time.perf_counter()
            for _ in range(n):
                conn.execute(PROBE, ("中华人民共和国民法典", 733)).fetchone()
            return (time.perf_counter() - t0) * 1000 / n

        before = probe_ms()
        existing = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        already = "idx_articles_name_no" in existing

        conn.execute(INDEX_SQL)
        conn.commit()
        after = probe_ms()

        print(f"条文总数            : {total}")
        print(f"索引状态            : {'已存在（跳过创建）' if already else '已创建'}")
        print(f"单次直查耗时        : {before:.2f} ms → {after:.2f} ms"
              + (f"  （提速 {before / after:.0f}×）" if after > 0 else ""))
        print(f"数据库体积          : {db.stat().st_size / 1048576:.1f} MB")
        print("说明：仅新增索引，未改动任何条文内容。")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
