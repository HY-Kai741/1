"""LLM 接入层（默认关闭）。

设计原则
--------
- **配置驱动、厂商无关**：只要求 OpenAI 兼容协议（``POST {BASE_URL}/chat/completions``）。
  DeepSeek / 通义千问 / 智谱 GLM / Kimi / OpenAI 均兼容 —— 换厂商只改三个环境变量，
  不动一行代码。
- **默认关闭**：未同时配置 ``LLM_API_KEY`` 与 ``LLM_BASE_URL`` 时 ``enabled()`` 恒为 False，
  法条检索与判定主流程完全离线，不受任何影响。
- **失败静默回退**：任何异常（超时 / 非 200 / JSON 解析失败）返回 ``None``，
  调用方回退到模板文案，绝不因模型故障影响可用性。
- **职责边界（红线）**：模型只承担四类「措辞与生成」任务，
  **永不参与法条的存储与判定**：

  ====================  ================================================
  ``polish``            润色通俗解释
  ``generate_question`` 生成题目（其引用的法条必须回库逐字核验后才可入库）
  ``classify_fallback`` 场景识别兜底（结果仍受映射表约束，不可越权）
  ``comment``           错题讲评与学习计划
  ====================  ================================================

- 不记录密钥；传入文本经 ``redact()`` 脱敏。

配置（写入项目根目录 ``.env``，该文件已被 .gitignore 排除）::

    LLM_API_KEY=sk-xxxxxxxx
    LLM_BASE_URL=https://api.deepseek.com/v1
    LLM_MODEL=deepseek-chat
    LLM_TIMEOUT=45          # 可选
    LLM_RETRIES=1           # 可选
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from .obs import log, redact


# ---------------------------------------------------------------- .env 载入

def load_dotenv(path: str | os.PathLike | None = None) -> bool:
    """从项目根目录 ``.env`` 载入环境变量（不覆盖已存在的）。零依赖实现。

    返回是否成功读到文件。密钥只进内存，绝不写回磁盘。
    """
    env_file = Path(path) if path else Path(__file__).resolve().parents[1] / ".env"
    if not env_file.is_file():
        return False
    try:
        content = env_file.read_text(encoding="utf-8")
    except OSError:
        return False
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val
    return True


load_dotenv()


# ---------------------------------------------------------------- 配置

def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def enabled() -> bool:
    """是否已配置可用的模型通道。"""
    return bool(
        os.environ.get("LLM_API_KEY", "").strip()
        and os.environ.get("LLM_BASE_URL", "").strip()
    )


def model_name() -> str:
    return os.environ.get("LLM_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"


def status() -> dict:
    """供 /api/health 输出，**不泄露密钥**。"""
    ok = enabled()
    return {
        "enabled": ok,
        "model": model_name() if ok else None,
        "base_url_set": bool(os.environ.get("LLM_BASE_URL", "").strip()),
        "provider_hint": _provider_hint() if ok else None,
    }


def _provider_hint() -> str | None:
    base = os.environ.get("LLM_BASE_URL", "").lower()
    for needle, name in (
        ("deepseek", "deepseek"),
        ("dashscope", "aliyun-qwen"),
        ("aliyuncs", "aliyun-qwen"),
        ("bigmodel", "zhipu-glm"),
        ("moonshot", "moonshot-kimi"),
        ("openai", "openai"),
    ):
        if needle in base:
            return name
    return "openai-compatible"


# ---------------------------------------------------------------- HTTP

def _post(payload: dict) -> dict | None:
    """POST 到 ``{BASE_URL}/chat/completions``，带重试与 400 自动降级。"""
    base = os.environ["LLM_BASE_URL"].rstrip("/")
    url = f"{base}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {os.environ['LLM_API_KEY']}",
    }
    timeout = _int_env("LLM_TIMEOUT", 45)
    retries = _int_env("LLM_RETRIES", 1)

    for attempt in range(retries + 1):
        body = json.dumps(payload).encode("utf-8")
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"http {resp.status}")
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 部分厂商不认 response_format / max_tokens —— 去掉后重试一次
            downgraded = False
            if exc.code == 400:
                for key in ("response_format", "max_tokens", "temperature"):
                    if key in payload:
                        payload.pop(key)
                        downgraded = True
            log("llm.http.error", level="warn", attempt=attempt,
                error=redact(f"http {exc.code}", 80), downgraded=downgraded)
            if not downgraded and attempt >= retries:
                break
        except (urllib.error.URLError, ValueError, TimeoutError, RuntimeError) as exc:
            log("llm.http.fail", level="warn", attempt=attempt, error=redact(str(exc), 160))
            if attempt >= retries:
                break
    return None


def chat(
    system: str,
    user: str,
    *,
    temperature: float = 0.2,
    max_tokens: int | None = None,
    json_mode: bool = False,
) -> str | None:
    """单轮对话，返回纯文本；任何失败返回 None。"""
    if not enabled():
        return None
    payload: dict = {
        "model": model_name(),
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    data = _post(payload)
    if not data:
        return None
    try:
        text = (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        log("llm.parse.fail", level="warn")
        return None
    return text or None


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _extract_json(text: str):
    """从模型输出里稳健地取出 JSON 对象/数组（容忍 ```json 包裹与前后废话）。"""
    cleaned = _FENCE.sub("", text).strip()
    try:
        return json.loads(cleaned)
    except ValueError:
        pass
    match = re.search(r"[\{\[].*[\}\]]", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------- 任务 1：润色解释

_SYS_POLISH = (
    "你是法律科普编辑。你会看到一条中国法律的条文原文和一句已有的通俗解释。"
    "你的任务是让通俗解释更自然易懂。硬性要求："
    "1) 不得改变条文的含义、条件与后果；2) 不得新增或修改任何法条名称、条款编号、金额、期限；"
    "3) 不得给出'一定能赢'之类的绝对判断；4) 只输出改写后的解释本身，不要任何前后缀。"
)


def polish(statute) -> str | None:
    """润色一条法条的通俗解释。失败返回 None（调用方回退预置文案）。"""
    text = chat(
        _SYS_POLISH,
        json.dumps({
            "法令": statute.law_name,
            "条号": statute.article_no,
            "条文原文": statute.text,
            "现有解释": getattr(statute, "explain", "") or "",
        }, ensure_ascii=False),
        temperature=0.2,
        max_tokens=400,
    )
    if not text or len(text) > 600:
        return None
    log("llm.polish.ok", model=model_name(), chars=len(text))
    return text


# ---------------------------------------------------------------- 任务 2：出题

_SYS_GEN = (
    "你是中国普法教育题库的命题编辑，服务对象是非法律专业的大学生。\n"
    "你会收到：一个【知识点】、若干条【法条原文】、若干道【已有题目】的题干。\n"
    "请依据所给法条，命制一道单选题。硬性要求：\n"
    "1) 题干必须是大学生在校期间可能遇到的生活化情境（租房、兼职、网购、借贷、作品被抄等），"
    "不得照搬法考原题的措辞、金额与人物名；\n"
    "2) 四个选项只有一个正确；干扰项要似是而非，但要能被所给法条排除；\n"
    "3) 解析必须逐句对应所给法条，**不得引入未提供的法条、司法解释或裁判观点**；\n"
    "4) 不得出现「一定能赢」「必然胜诉」「肯定可以」等绝对判断；\n"
    "5) 情境不得与【已有题目】重复。\n"
    "只输出一个 JSON 对象，字段为：\n"
    '{"stem": "题干", "options": [{"key": "A", "text": "..."}, {"key": "B", "text": "..."},'
    ' {"key": "C", "text": "..."}, {"key": "D", "text": "..."}],'
    ' "answer": ["A"], "explanation": "解析", "difficulty": 3, "question_class": "KD"}\n'
    "其中 question_class 取 KD（概念辨析）或 CA（案例分析），difficulty 取 1-5 的整数。"
)


def generate_question(kp: dict, statutes: list, existing_stems: list[str]) -> dict | None:
    """依据知识点与法条原文生成一道题（返回未核验的原始 dict）。

    **返回结果不可直接入库** —— 必须经 ``scripts/generate_questions.py`` 的
    回库核验与查重环节。
    """
    if not enabled():
        return None
    payload_in = {
        "知识点": kp.get("name"),
        "知识点说明": kp.get("desc", ""),
        "法条原文": [
            {"法令": s.law_name, "条号": s.article_no, "原文": s.text} for s in statutes
        ],
        "已有题目": [s for s in existing_stems[:20] if s],
    }
    text = chat(
        _SYS_GEN,
        json.dumps(payload_in, ensure_ascii=False),
        temperature=0.7,
        max_tokens=1200,
        json_mode=True,
    )
    if not text:
        return None
    data = _extract_json(text)
    if not isinstance(data, dict):
        log("llm.gen.parse_fail", level="warn")
        return None
    log("llm.gen.ok", model=model_name(), stem_len=len(str(data.get("stem", ""))))
    return data


# ---------------------------------------------------------------- 任务 3：场景识别兜底

_SYS_CLS = (
    "你是法律领域分类器。用户会给你一句话，和一个候选领域列表（每项含 id 与关键词）。\n"
    "请判断这句话最可能属于哪个领域。\n"
    "只输出该领域的 id（例如 rental），不要输出任何其他文字、标点或解释。\n"
    "若都不匹配，只输出 unknown。"
)


def classify_fallback(text: str, candidates: list[dict]) -> str | None:
    """关键词打分识别不出领域时的兜底判定。返回 scene_id 或 None。

    注意：结果仍须由调用方用映射表校验，不得越权直接决定法条。
    """
    if not enabled():
        return None
    out = chat(
        _SYS_CLS,
        json.dumps({"句子": text, "候选领域": candidates}, ensure_ascii=False),
        temperature=0.0,
        max_tokens=16,
    )
    if not out:
        return None
    cleaned = out.strip().strip('"').strip("'").split()[0]
    return cleaned or None


# ---------------------------------------------------------------- 任务 4：讲评与学习计划

_SYS_COMMENT = (
    "你是普法教育的助教，面向非法律专业的大学生。"
    "你会看到一道题、学生的选择和正确答案，以及参考解析。\n"
    "请用 2-3 句话讲评：先点出他的认知偏差在哪一步，再回扣相关法条的含义。\n"
    "硬性要求：1) 不得评判学生本人（不说'你应该'、'你太粗心'）；"
    "2) 不得给出绝对判断；3) 不得引入未提供的法条；4) 不要复述整道题。"
)


def comment(stem: str, answer: list[str], chosen: list[str], explanation: str) -> str | None:
    """生成错题讲评。失败返回 None。"""
    text = chat(
        _SYS_COMMENT,
        json.dumps({
            "题干": stem,
            "正确答案": answer,
            "学生选择": chosen,
            "参考解析": explanation,
        }, ensure_ascii=False),
        temperature=0.4,
        max_tokens=400,
    )
    if not text or len(text) > 600:
        return None
    return text


_SYS_PLAN = (
    "你是普法学习的规划助教。用户会给你他做错的题目所属的知识点列表。"
    "请给出 3 条以内的学习建议，说明按什么顺序复习、每步看什么。"
    "只依据所给知识点说话，不得编造法条，不得承诺效果。"
)


def study_plan(knowledge_points: list[str]) -> str | None:
    """基于错题知识点生成学习计划。失败返回 None。"""
    if not knowledge_points:
        return None
    text = chat(
        _SYS_PLAN,
        json.dumps({"薄弱知识点": knowledge_points[:10]}, ensure_ascii=False),
        temperature=0.4,
        max_tokens=400,
    )
    if not text or len(text) > 600:
        return None
    return text
