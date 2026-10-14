"""可观测：trace_id 贯穿、阶段埋点、脱敏、指标。

设计要点：
- 全链路一个 trace_id；每个阶段产出一条结构化日志（JSON 单行），便于 grep/采集。
- 日志前一律先过 redact()：手机号、身份证、银行卡、邮箱等一律打码（C6/PII）。
- 指标为进程内计数（无外部依赖，可被 /api/health 读取），不引入监控组件。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager

# ---------- trace ----------

def new_trace_id() -> str:
    return "law_" + secrets.token_hex(6)


# ---------- 脱敏 ----------

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[手机号]"),
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "[身份证]"),
    (re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "[银行卡]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[邮箱]"),
]


def redact(text: str, limit: int = 400) -> str:
    """PII 打码 + 截断。用于日志与埋点，绝不落库原文。"""
    if not text:
        return ""
    out = str(text)
    for pat, rep in _PATTERNS:
        out = pat.sub(rep, out)
    out = out.replace("\n", " ")
    return out[:limit] + ("…" if len(out) > limit else "")


# ---------- 日志 ----------

_LOGGER = print
_LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}
# 测试/本地可设 LAW_LOG_LEVEL=error 静音阶段日志（默认 info，线上保留可观测性）。
_MIN_LEVEL = _LEVELS.get(str(os.environ.get("LAW_LOG_LEVEL", "info")).lower(), 20)


def log(event: str, level: str = "info", **fields) -> None:
    if _LEVELS.get(level, 20) < _MIN_LEVEL:
        return
    payload = {"ts": round(time.time(), 3), "level": level, "event": event, **fields}
    try:
        _LOGGER(json.dumps(payload, ensure_ascii=False, default=str))
    except Exception:  # pragma: no cover - 日志失败绝不能影响主流程
        pass


# ---------- span ----------

class Span:
    def __init__(self, trace_id: str, name: str):
        self.trace_id = trace_id
        self.name = name
        self.t0 = time.perf_counter()

    def ok(self, **extra) -> float:
        ms = (time.perf_counter() - self.t0) * 1000
        log("stage", trace_id=self.trace_id, stage=self.name, ok=True, ms=round(ms, 2), **extra)
        METRICS.bump(f"stage.{self.name}.ok")
        return ms

    def fail(self, err: str, **extra) -> float:
        ms = (time.perf_counter() - self.t0) * 1000
        log("stage", level="error", trace_id=self.trace_id, stage=self.name, ok=False,
            ms=round(ms, 2), error=redact(err, 200), **extra)
        METRICS.bump(f"stage.{self.name}.fail")
        return ms


def span(trace_id: str, name: str) -> Span:
    return Span(trace_id, name)


# ---------- 指标 ----------

class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}
        self._latencies: list[float] = []

    def bump(self, key: str, n: int = 1) -> None:
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + n

    def observe_ms(self, ms: float) -> None:
        with self._lock:
            self._latencies.append(ms)
            if len(self._latencies) > 2000:
                del self._latencies[:1000]

    def snapshot(self) -> dict:
        with self._lock:
            lat = sorted(self._latencies)
            counters = dict(self._counters)
        def pct(p: float) -> float | None:
            if not lat:
                return None
            idx = min(len(lat) - 1, int(len(lat) * p))
            return round(lat[idx], 2)
        return {
            "counters": counters,
            "requests": len(lat),
            "p50_ms": pct(0.50),
            "p95_ms": pct(0.95),
            "p99_ms": pct(0.99),
        }


METRICS = Metrics()


@contextmanager
def timed_stage(trace_id: str, name: str, **extra):
    """with timed_stage(...) as st: ... ; st['ms'] 可读。异常时自动记 fail 并向上抛。"""
    box: dict = {"trace_id": trace_id, "name": name}
    sp = span(trace_id, name)
    try:
        yield box
    except Exception as exc:
        box["ms"] = sp.fail(str(exc), **extra)
        raise
    else:
        box["ms"] = sp.ok(**extra)
