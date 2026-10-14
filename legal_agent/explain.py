"""通俗解释：优先用人工预置的「人话翻译」，缺失时给中性结构化说明。

绝不臆测法条含义：没有预置翻译时，宁可告诉用户「以原文为准」，也不生成可能误解的解读。
可选 LLM 润色由 llm.polish() 提供，失败即回退，不影响主流程。
"""
from __future__ import annotations

from . import llm
from .statutes import Statute


def explain(statute: Statute) -> str:
    if statute.explain:
        base = statute.explain
    else:
        base = (
            "这条规定出自"
            f"《{statute.law_name}》{statute.chapter or ''}。"
            "系统没有为这一条预置通俗翻译，为避免解读偏差，建议直接阅读上方原文；"
            "如需要，我可以帮你把案情要素和条文要点逐句对照。"
        )
    if llm.enabled() and not statute.out_of_scope:
        polished = llm.polish(statute)
        if polished:
            return polished
    return base
