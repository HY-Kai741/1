# -*- coding: utf-8 -*-
"""题库 / 法律语料 获取流水线。

设计原则
--------
1. **许可优先**：每个源必须显式声明 license 与是否可商用；未确认的一律不入正式库，
   只落到 `data/external/banks/raw/`（该目录已被 .gitignore 排除，不进公开仓库）。
2. **只做搬运，不做判定**：本脚本下载并规范化成统一 jsonl，**不生成法条引用、不判对错**。
   要进入对外发布的题库（`data/questions/`），必须另过 `scripts/validate_bank.py` 的七道门禁。
3. **可复跑**：源清单集中在 SOURCES，落盘时记录 sha256 / 条数 / 抓取时间，便于追溯。

用法
----
    python scripts/fetch_banks.py --list                 # 查看源清单与许可
    python scripts/fetch_banks.py --fetch all            # 抓取全部许可可用的源
    python scripts/fetch_banks.py --fetch disc_law_sft_qa --limit 5000
    python scripts/fetch_banks.py --fetch jecqa_ca --allow-restricted
    python scripts/fetch_banks.py --report               # 只统计已抓取内容
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import ssl
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BANK_DIR = ROOT / "data" / "external" / "banks"
RAW_DIR = BANK_DIR / "raw"
NORM_DIR = BANK_DIR / "normalized"
REGISTRY = BANK_DIR / "_registry.json"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) law-qa-assistant/1.0"}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

# raw.githubusercontent.com 在很多网络环境下不可达（实测 SSL UNEXPECTED_EOF）。
# 按顺序尝试镜像/代理，任一成功即用。实测可用：ghproxy.net / gh-proxy.com / ghfast.top。
GH_PROXIES = [
    "",
    "https://ghproxy.net/",
    "https://gh-proxy.com/",
    "https://ghfast.top/",
]


def _candidates(url: str) -> list[str]:
    """为 GitHub raw 地址生成「直连 + 多个代理」的候选列表；其他地址原样返回。"""
    if "raw.githubusercontent.com" not in url:
        return [url]
    return [p + url if p else url for p in GH_PROXIES]

# ---------------------------------------------------------------- 源清单
# commercial_ok: True=许可明确允许商用 / False=禁止或未确认
# restricted:    True=许可受限，默认不抓，需 --allow-restricted 显式放行
SOURCES: dict[str, dict] = {
    "disc_law_sft_qa": {
        "name": "DISC-Law-SFT（复旦 DISC 实验室）",
        "kind": "qa",
        "url": "https://hf-mirror.com/datasets/ShengbinYue/DISC-Law-SFT/resolve/main/DISC-Law-SFT-Pair-QA-released.jsonl",
        "home": "https://github.com/FudanDISC/DISC-LawLLM",
        "license": "Apache-2.0",
        "commercial_ok": True,
        "restricted": False,
        "note": "含法律问答 93K / 法律阅读理解 38K / 司法考试 12K。许可为 Apache-2.0，可商用。"
                "数据由 GPT-4 等生成后人工筛选，使用时须标注来源。",
        "size_hint": "~94 MB",
    },
    "disc_law_sft_triplet_qa": {
        "name": "DISC-Law-SFT Triplet-QA（带法条依据的问答）",
        "kind": "qa",
        "url": "https://hf-mirror.com/datasets/ShengbinYue/DISC-Law-SFT/resolve/main/DISC-Law-SFT-Triplet-QA-released.jsonl",
        "home": "https://github.com/FudanDISC/DISC-LawLLM",
        "license": "Apache-2.0",
        "commercial_ok": True,
        "restricted": False,
        "note": "三元组格式，含法条依据字段，最贴近本项目「法条回库核验」的用法。",
        "size_hint": "~86 MB",
    },
    "legalqa": {
        "name": "LegalQA（siatnlp，律师真实问答）",
        "kind": "qa",
        "url": "https://raw.githubusercontent.com/siatnlp/LegalQA/master/LegalQA-manual-train.csv",
        "home": "https://github.com/siatnlp/LegalQA",
        "license": "未声明（UNKNOWN）",
        "commercial_ok": False,
        "restricted": True,
        "note": "论坛真实问答，答案由执业律师提供。仓库未附 LICENSE 文件，商用状态未确认，"
                "仅作离线参考，不得随产品分发。",
        "size_hint": "~4.3 MB",
    },
    "jecqa_ca": {
        "name": "JEC-QA-CA（国家司法考试选择题，经 AGIEval 整理）",
        "kind": "mcq",
        "url": "https://hf-mirror.com/datasets/hails/agieval-jec-qa-ca/resolve/main/data/test-00000-of-00001.parquet",
        "home": "https://github.com/microsoft/AGIEval",
        "license": "仅限学术研究（JEC-QA 原条款禁止商用）",
        "commercial_ok": False,
        "restricted": True,
        "note": "999 道单选/多选，真实法考题目。原 JEC-QA 条款：academic research only, "
                "commercial use strictly prohibited。仅可作内部难度标定，禁止随产品分发。",
        "size_hint": "~0.6 MB",
    },
    "statutes_json": {
        "name": "中国法律法规数据库镜像（twang2218/law-datasets）",
        "kind": "statute",
        "url": "https://raw.githubusercontent.com/twang2218/law-datasets/main/law-and-regulations/README.md",
        "home": "https://github.com/twang2218/law-datasets",
        "license": "MIT",
        "commercial_ok": True,
        "restricted": False,
        "note": "从 flk.npc.gov.cn 抓取的 22,552 部法律法规 JSON（截至 2023-09）。"
                "官方站直连在本环境返回 SPA 壳，此镜像为可用替代。本项仅探测可达性。",
        "size_hint": "仓库较大，按需拉取",
        "probe_only": True,
    },
}


# ---------------------------------------------------------------- 工具
def _utf8() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024.0
    return f"{n:.1f}GB"


def _download(url: str, dest: Path, timeout: int = 180) -> tuple[int, str, str]:
    """流式下载，返回 (字节数, sha256, 实际使用的 URL)。

    对 GitHub raw 地址会依次尝试直连与镜像代理；任一成功即返回。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    last_err: Exception | None = None
    for cand in _candidates(url):
        h = hashlib.sha256()
        total = 0
        try:
            req = urllib.request.Request(cand, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r, open(tmp, "wb") as f:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    h.update(chunk)
                    f.write(chunk)
                    total += len(chunk)
        except Exception as e:  # 换下一个候选
            last_err = e
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            continue
        tmp.replace(dest)
        return total, h.hexdigest(), cand
    raise last_err if last_err else RuntimeError("no candidate url")


def _probe(url: str, timeout: int = 30) -> tuple[int, str]:
    """只取前若干字节探测可达性，返回 (状态码, 实际 URL)。"""
    last: Exception | None = None
    for cand in _candidates(url):
        try:
            req = urllib.request.Request(cand, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                b = r.read(2048)
                return (r.status, cand, len(b))  # type: ignore[return-value]
        except Exception as e:
            last = e
    raise last if last else RuntimeError("unreachable")


def _iter_jsonl(path: Path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _norm_disc(obj: dict) -> dict | None:
    """DISC-Law-SFT 是 alpaca 风格：instruction / input / output。"""
    q = (obj.get("input") or obj.get("instruction") or "").strip()
    a = (obj.get("output") or "").strip()
    if not q or not a:
        return None
    return {"question": q, "answer": a, "source": "disc_law_sft_qa"}


def _norm_legalqa(row: dict) -> dict | None:
    """实测列名：`question: subject` / `question: body` / `answer` / `label`。"""
    q = (row.get("question: body") or row.get("question: subject")
         or row.get("question") or "").strip()
    a = (row.get("answer") or "").strip()
    if not q or not a:
        return None
    return {"question": q, "answer": a,
            "subject": (row.get("question: subject") or "").strip(),
            "label": (row.get("label") or "").strip(),
            "source": "legalqa"}


def _norm_jecqa(obj: dict) -> dict | None:
    q = (obj.get("query") or "").strip()
    ch = obj.get("choices") or []
    gold = obj.get("gold") or []
    if not q or not ch:
        return None
    return {"question": q, "choices": ch, "gold": gold, "source": "jecqa_ca"}


# ---------------------------------------------------------------- 动作
def cmd_list() -> int:
    print(f"{'id':<26} {'许可':<34} {'可商用':<7} {'受限':<5} 规模")
    print("-" * 100)
    for sid, s in SOURCES.items():
        print(f"{sid:<26} {s['license'][:32]:<34} "
              f"{'是' if s['commercial_ok'] else '否':<7} "
              f"{'是' if s['restricted'] else '否':<5} {s['size_hint']}")
    print()
    print("说明：'受限=是' 的源默认不抓，需 --allow-restricted 显式放行；"
          "抓下来的原始文件全部落在 data/external/banks/（已被 .gitignore 排除）。")
    return 0


def cmd_fetch(targets: list[str], limit: int | None, allow_restricted: bool) -> int:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    NORM_DIR.mkdir(parents=True, exist_ok=True)
    reg = _load_registry()
    rc = 0
    for sid in targets:
        s = SOURCES.get(sid)
        if not s:
            print(f"[跳过] 未知源：{sid}")
            rc = 1
            continue
        if s.get("probe_only"):
            print(f"\n=== {sid} ===\n  {s['name']}\n  探针项：仅验证可达性")
            try:
                status, used, nbytes = _probe(s["url"])
                print(f"  可达 ✓  status={status} bytes={nbytes}  许可={s['license']}")
                print(f"  通道：{used}")
                reg[sid] = {**{k: s[k] for k in ("name", "license", "commercial_ok", "note")},
                            "fetched_at": _now(), "probe_bytes": nbytes,
                            "used_url": used, "status": "probed"}
            except Exception as e:
                print(f"  不可达 ✗  {type(e).__name__}: {str(e)[:120]}")
                rc = 1
            continue

        if s["restricted"] and not allow_restricted:
            print(f"\n[跳过] {sid}：许可={s['license']}（受限）。"
                  f"如需内部参考请加 --allow-restricted。")
            continue

        name = s["url"].split("/")[-1]
        dest = RAW_DIR / f"{sid}__{name}"
        print(f"\n=== {sid} ===\n  {s['name']}\n  许可：{s['license']}")
        if dest.exists():
            size = dest.stat().st_size
            print(f"  已存在，跳过下载（{_human(size)}）")
            sha = _sha256(dest)
            used = s["url"]
        else:
            print(f"  下载中 … {s['url']}")
            t0 = time.time()
            try:
                size, sha, used = _download(s["url"], dest)
            except Exception as e:
                print(f"  下载失败 ✗  {type(e).__name__}: {str(e)[:140]}")
                rc = 1
                continue
            print(f"  完成 {_human(size)} / {time.time()-t0:.1f}s  sha256={sha[:16]}…")
            if used != s["url"]:
                print(f"  通道：{used}")

        # 规范化
        out = NORM_DIR / f"{sid}.jsonl"
        n = _normalize(sid, dest, out, limit)
        if n is not None:
            print(f"  规范化 → {out.relative_to(ROOT)}  {n} 条")
        reg[sid] = {
            "name": s["name"], "license": s["license"], "commercial_ok": s["commercial_ok"],
            "restricted": s["restricted"], "home": s["home"], "note": s["note"],
            "fetched_at": _now(), "raw_file": str(dest.relative_to(ROOT)),
            "bytes": size, "sha256": sha, "normalized": str(out.relative_to(ROOT)) if n else None,
            "records": n, "status": "ok", "url": s["url"], "used_url": used,
        }
    _save_registry(reg)
    print(f"\n清单已写入 {REGISTRY.relative_to(ROOT)}")
    return rc


def _normalize(sid: str, src: Path, out: Path, limit: int | None) -> int | None:
    try:
        if sid.startswith("disc_law_sft"):
            fn = _norm_disc
        elif sid == "legalqa":
            return _normalize_csv(src, out, limit)
        elif sid == "jecqa_ca":
            return _normalize_parquet(src, out, limit)
        else:
            return None
        n = 0
        with open(out, "w", encoding="utf-8") as fo:
            for obj in _iter_jsonl(src):
                rec = fn(obj)
                if rec:
                    fo.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    n += 1
                    if limit and n >= limit:
                        break
        return n
    except Exception as e:
        print(f"  规范化失败（原始文件保留）：{type(e).__name__}: {e}")
        return None


def _normalize_csv(src: Path, out: Path, limit: int | None) -> int:
    import csv
    n = 0
    with open(src, "r", encoding="utf-8", errors="replace", newline="") as fi, \
         open(out, "w", encoding="utf-8") as fo:
        rd = csv.DictReader(fi)
        for row in rd:
            rec = _norm_legalqa(row)
            if rec:
                fo.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n += 1
                if limit and n >= limit:
                    break
    return n


def _normalize_parquet(src: Path, out: Path, limit: int | None) -> int:
    try:
        import pyarrow.parquet as pq  # type: ignore
    except ImportError:
        print("  需要 pyarrow 才能解析 parquet：pip install pyarrow（原始文件已保留）")
        return 0
    t = pq.read_table(src)
    n = 0
    with open(out, "w", encoding="utf-8") as fo:
        for obj in t.to_pylist():
            rec = _norm_jecqa(obj)
            if rec:
                fo.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n += 1
                if limit and n >= limit:
                    break
    return n


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S%z")


def _load_registry() -> dict:
    if REGISTRY.exists():
        try:
            return json.loads(REGISTRY.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_registry(reg: dict) -> None:
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")


def cmd_report() -> int:
    reg = _load_registry()
    if not reg:
        print("尚无抓取记录。先跑 --fetch。")
        return 0
    print(f"{'源':<26} {'许可':<30} {'条数':>8} {'体积':>10} {'可商用':<7}")
    print("-" * 92)
    for sid, r in reg.items():
        print(f"{sid:<26} {str(r.get('license'))[:28]:<30} "
              f"{str(r.get('records') or '-'):>8} {_human(r.get('bytes') or 0):>10} "
              f"{'是' if r.get('commercial_ok') else '否':<7}")
    ok = sum(1 for r in reg.values() if r.get("commercial_ok"))
    print(f"\n共 {len(reg)} 个源，其中 {ok} 个许可明确可商用。")
    print("对外发布的题库仍须经 scripts/validate_bank.py 门禁，抓取内容不等于可发布内容。")
    return 0


def main() -> int:
    _utf8()
    ap = argparse.ArgumentParser(description="题库/法律语料获取流水线")
    ap.add_argument("--list", action="store_true", help="列出源清单与许可")
    ap.add_argument("--fetch", nargs="*", default=None,
                    help="抓取指定源；all=抓取全部许可可用的源")
    ap.add_argument("--limit", type=int, default=None, help="每个源最多规范化多少条")
    ap.add_argument("--allow-restricted", action="store_true",
                    help="放行许可受限的源（仅限内部参考，禁止分发）")
    ap.add_argument("--report", action="store_true", help="打印已抓取统计")
    args = ap.parse_args()

    if args.list or (args.fetch is None and not args.report):
        return cmd_list()
    if args.report:
        return cmd_report()
    targets = list(SOURCES) if args.fetch == [] or "all" in args.fetch else args.fetch
    return cmd_fetch(targets, args.limit, args.allow_restricted)


if __name__ == "__main__":
    raise SystemExit(main())
