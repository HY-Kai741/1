"""可选 LLM 润色（默认关闭）。

约束：
- 只在环境变量存在时启用；无 key 时 enabled() 恒为 False，主流程完全离线；
- 提示词强制「只复述条文含义，不得新增法条编号、不得改变法条内容」，并且只在
  检索范围内改写措辞；
- 任何异常（超时、非 200、解析失败）一律返回 None，调用方回退到模板文案；
- 有超时与一次重试；不记录密钥、不把 PII 送上链路（问题文本经脱敏后传入）。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .obs import log, redact

_TIMEOUT = 12
_RETRIES = 1


def enabled() -> bool:
    return bool(os.environ.get("LLM_API_KEY", "").strip() and os.environ.get("LLM_BASE_URL", "").strip())


_SYSTEM = (
    "你是法律科普编辑。你会看到一条中国法律的条文原文和一句已有的通俗解释。"
    "你的任务是让通俗解释更自然易懂。硬性要求："
    "1) 不得改变条文的含义、条件与后果；2) 不得新增或修改任何法条名称、条款编号、金额、期限；"
    "3) 不得给出‘一定能赢’之类的绝对判断；4) 只输出改写后的解释本身，不要任何前后缀。"
)


def polish(statute) -> str | None:
    if not enabled():
        return None
    base = os.environ["LLM_BASE_URL"].rstrip("/")
    model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
    url = f"{base}/chat/completions"
    payload = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": json.dumps({
                "法令": statute.law_name,
                "条号": statute.article_no,
                "条文原文": statute.text,
                "现有解释": statute.explain or "",
            }, ensure_ascii=False)},
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {os.environ['LLM_API_KEY']}",
    }

    for attempt in range(_RETRIES + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"http {resp.status}")
                data = json.loads(resp.read().decode("utf-8"))
            text = (data["choices"][0]["message"]["content"] or "").strip()
            if not text or len(text) > 600:
                return None
            log("llm.polish.ok", model=model, chars=len(text))
            return text
        except (urllib.error.URLError, KeyError, ValueError, TimeoutError, RuntimeError) as exc:
            log("llm.polish.fail", level="warn", attempt=attempt, error=redact(str(exc), 160))
    return None
