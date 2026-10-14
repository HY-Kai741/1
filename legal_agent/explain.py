"""通俗解释：优先用人工预置的「人话翻译」，缺失时给中性结构化说明。

绝不臆测法条含义：没有预置翻译时，宁可告诉用户「以原文为准」，也不生成可能误解的解读。

启用 LLM 时的三条工程约束
------------------------
1. **只润色有人工预置翻译的法条**。对没有预置翻译的条文让模型「改写」，等于让模型替我们
   解读法条 —— 与「绝不臆测法条含义」直接冲突，因此一律不润色，直接回退中性文案。
2. **进程内缓存，保持确定性**。同一法条的润色结果只算一次；同一进程内重复提问会得到
   逐字相同的回答，从而不破坏 `tests/test_variation.py` 的「同输入稳定」约束。
3. **并发 + 失败熔断**。一条回答常引 3～6 条法条，串行调用会把响应从 ~0.35s 拖到 15s；
   改为并发后首次约 3～4s，命中缓存后回到毫秒级。若润色连续失败（如密钥失效、端点不可达），
   熔断一段时间内直接跳过润色，避免把响应拖成长等待。

可调环境变量：
    LLM_POLISH_WORKERS   并发润色线程数（默认 4，上限 8）
    LLM_FAIL_COOLDOWN    润色失败后的熔断秒数（默认 60）
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import llm
from .statutes import Statute

# ---------------------------------------------------------------- 缓存

_CACHE: dict[str, str] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 1024          # 映射法条仅 60 条 + 兜底检索，容量足够且不会无界增长

# ---------------------------------------------------------------- 熔断

_FAIL_LOCK = threading.Lock()
_FAIL_UNTIL = 0.0


def _cooldown() -> float:
    try:
        return max(0.0, float(os.environ.get("LLM_FAIL_COOLDOWN", "60") or 60))
    except ValueError:
        return 60.0


def _breaker_open() -> bool:
    with _FAIL_LOCK:
        return time.time() < _FAIL_UNTIL


def _trip_breaker() -> None:
    global _FAIL_UNTIL
    cd = _cooldown()
    if cd <= 0:
        return
    with _FAIL_LOCK:
        _FAIL_UNTIL = time.time() + cd


def _reset_breaker() -> None:
    global _FAIL_UNTIL
    with _FAIL_LOCK:
        _FAIL_UNTIL = 0.0


def _workers() -> int:
    try:
        return max(1, min(8, int(os.environ.get("LLM_POLISH_WORKERS", "4") or 4)))
    except ValueError:
        return 4


# ---------------------------------------------------------------- 文案

def _base_text(statute: Statute) -> str:
    """没有润色结果时的兜底文案（与关闭 LLM 时的输出完全一致）。"""
    if statute.explain:
        return statute.explain
    return (
        "这条规定出自"
        f"《{statute.law_name}》{statute.chapter or ''}。"
        "系统没有为这一条预置通俗翻译，为避免解读偏差，建议直接阅读上方原文；"
        "如需要，我可以帮你把案情要素和条文要点逐句对照。"
    )


def _cache_key(statute: Statute) -> str:
    raw = "\x1f".join([
        llm.model_name(),
        os.environ.get("LLM_BASE_URL", ""),
        statute.law_name,
        statute.article_no,
        statute.text,
        statute.explain or "",
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _polishable(statute: Statute) -> bool:
    """是否值得交给模型润色。"""
    return bool(
        llm.enabled()
        and statute.explain          # 只润色有人工翻译的条文（见模块 docstring 第 1 条）
        and not statute.out_of_scope
        and not _breaker_open()
    )


# ---------------------------------------------------------------- 对外接口

def explain(statute: Statute) -> str:
    """单条解释。保留单条接口，内部走批量路径。"""
    return explain_all([statute])[0]


def explain_all(statutes: list[Statute]) -> list[str]:
    """批量产出解释，顺序与输入一致；任何异常都回退到预置文案。"""
    results: list[str | None] = [None] * len(statutes)
    pending: list[tuple[int, Statute, str]] = []

    for i, s in enumerate(statutes):
        if not _polishable(s):
            results[i] = _base_text(s)
            continue
        key = _cache_key(s)
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
        if hit is not None:
            results[i] = hit
            continue
        pending.append((i, s, key))

    if pending:
        try:
            with ThreadPoolExecutor(max_workers=_workers()) as pool:
                futures = {pool.submit(llm.polish, s): (i, s, key) for i, s, key in pending}
                for fut, (i, _s, key) in futures.items():
                    try:
                        polished = fut.result()
                    except Exception:  # noqa: BLE001 —— 润色失败绝不影响主流程
                        polished = None
                    if polished:
                        results[i] = polished
                        with _CACHE_LOCK:
                            if len(_CACHE) >= _CACHE_MAX:
                                _CACHE.clear()
                            _CACHE[key] = polished
            if all(results[i] is not None for i, _s, _k in pending):
                _reset_breaker()
            else:
                _trip_breaker()
        except Exception:  # noqa: BLE001 —— 线程池不可用时静默降级
            _trip_breaker()

    return [r if r is not None else _base_text(s) for r, s in zip(results, statutes)]
