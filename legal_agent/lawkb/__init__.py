"""lawkb —— 知识库适配层。

本包内的 `database.py` 与 `retrieval.py` 取自开源项目 **china-law-mcp**
(https://github.com/thu-lawyer/china-law-mcp, MIT License, v0.1.1)，
仅做三处工程适配：

1. 默认数据库路径指向本项目 `data/laws.db`；
2. BM25 索引默认写入本项目 `data/`（只读时回退临时目录）；
3. `retrieval` 的导入做容错——缺 jieba / rank-bm25 时不让整个服务崩，
   由上层降级为纯 Python 关键词检索。

上游检索与引用核验逻辑未作改动，以保证「法条原文逐字一致」。
许可证副本见项目根目录 `LICENSE-china-law-mcp.txt`。
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_DB = _PROJECT_ROOT / "data" / "laws.db"
if _DB.exists():
    os.environ.setdefault("CHINA_LAW_DB", str(_DB))

_DATA_DIR = _PROJECT_ROOT / "data"
try:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    if os.access(str(_DATA_DIR), os.W_OK):
        os.environ.setdefault("CHINA_LAW_INDEX", str(_DATA_DIR / "bm25.pkl"))
except OSError:
    pass

from . import database  # noqa: E402,F401

# 检索模块依赖第三方纯 Python 包（jieba / rank-bm25）。导入失败时降级，不阻断启动。
# 注意：这里不能用 `from . import retrieval` —— 包命名空间里已存在同名占位符时，
# 该语法会直接返回占位符而跳过子模块导入，必须用 importlib 显式加载。
retrieval = None            # type: ignore[assignment]
RETRIEVAL_ERROR: str | None = None
try:
    retrieval = importlib.import_module(".retrieval", __name__)
except Exception as exc:  # pragma: no cover - 环境相关
    RETRIEVAL_ERROR = f"{type(exc).__name__}: {exc}"

__all__ = ["database", "retrieval", "RETRIEVAL_ERROR"]
