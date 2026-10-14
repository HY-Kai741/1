"""法条求解器：映射优先 → 数据库核验 → 逐字取原文；未命中再走 BM25 兜底。

硬约束（对应「不要编造法条」）：
- 任何返回给用户的法条，`text` 都必须来自 data/laws.db 的 `article_text`，本模块不生成文本；
- 映射候选在构建期已核验（scripts/verify_maps.py），运行期再做一次「存在性 + 关键词」防御性校验，
  校验不过的法条直接丢弃并记 warn，绝不带病输出；
- 兜底检索路径的结果标记为 out_of_scope，由展示层附加「仅供参考」提示。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import lawkb
from .maps import load_maps
from .obs import log

DEFAULT_TOP_N = 3

# 场景内的「聚焦规则」：按用户实际说了什么，把最贴题的法条顶上来。
# 权重 = 0.5 + 0.15 × 命中的提示词个数（上限 1.0），命中越多越贴题。
FOCUS: dict[str, list[tuple[tuple[str, ...], list[int]]]] = {
    "rental": [
        (("押金", "退押", "退还", "扣押", "不退"), [733, 714, 586, 587]),
        (("维修", "漏水", "坏了", "损坏", "修"), [713, 714]),
        (("欠租", "没交租", "没交房租", "赶", "换锁", "断水", "断电", "解除"), [722]),
        (("提前退", "不租了", "搬走", "退租", "到期"), [733, 577]),
    ],
    "parttime": [
        (("拖欠", "欠薪", "欠", "不给", "没给", "两个月", "三个月", "一直不", "赖"), [85, 30, 50]),
        (("试用期",), [70]),
        (("口头", "没签", "没合同", "没有合同"), [69]),
        (("小时", "时薪", "日结", "月结", "结算", "一小时"), [72, 68]),
        (("仲裁", "时效", "拖了"), [27]),
        (("个人", "私人", "学长", "介绍", "朋友"), [579]),
    ],
    "consumer": [
        (("七天", "无理由", "想退", "退货", "退款"), [25, 24]),
        (("质量", "不符", "坏了", "次品", "假"), [24, 55]),
        (("欺诈", "骗", "虚假", "假货"), [55]),
        (("平台", "商家不给", "客服", "投诉", "介入"), [39, 49]),
        (("外卖", "吃出", "食品", "过期", "变质", "异物"), [148]),
    ],
    "ip": [
        (("抄", "剽窃", "洗稿", "搬运", "盗用", "复制"), [52, 11]),
        (("论文", "文章", "作品", "代码", "画", "设计", "照片"), [3, 11]),
        (("署名", "名字", "作者", "没提"), [11, 52]),
        (("赔", "赔偿", "损失", "赚钱", "营利", "流量"), [54, 1185]),
        (("算不算", "是否侵权", "属不属于"), [52, 1165]),
    ],
    "loan": [
        (("高利", "高息", "月息", "利率", "利息", "很高", "太高", "网贷", "校园贷", "利滚利", "翻倍"), [680, 674]),
        (("砍头", "到手", "只给", "扣了", "服务费", "手续费"), [670, 561]),
        (("还不上", "还不起", "逾期", "逾期费", "还不完"), [676, 675]),
        (("催收", "威胁", "恐吓", "通讯录", "骚扰", "打电话"), [680, 676]),
        (("合同", "协议", "书面", "借条"), [668, 667]),
    ],
}


@dataclass
class Statute:
    law_name: str
    article_no: str            # 中文条号，如「第七百三十三条」
    article_no_arabic: int
    text: str                  # 逐字来自 laws.db
    topic: str = ""
    source: str = "映射核验"     # 映射核验 / 知识库语义检索
    score: float = 0.0
    explain: str | None = None
    out_of_scope: bool = False
    chapter: str = ""
    status: str = ""
    article_id: int | None = None   # laws.db 主键，用于溯源与逐字校验

    @property
    def citation(self) -> str:
        return f"《{self.law_name}》{self.article_no}"


def _fetch(law_short: str, article_no: int) -> dict | None:
    ret = lawkb.retrieval
    full = ret.resolve_law(law_short) if ret else None
    if not full:
        return None
    return lawkb.database.find_article(full, article_no)


def _passes_keyword_guard(text: str, verify_any: list[str]) -> bool:
    if not verify_any:
        return True
    return any(k and k in text for k in verify_any)


def _relevance(scene_id: str, message: str, idx: int, ref: dict, text: str) -> float:
    score = 1.0 - idx * 0.015
    for keys, arts in FOCUS.get(scene_id, []):
        if ref["article"] in arts:
            hits = sum(1 for k in keys if k in message)
            if hits:
                score += min(1.0, 0.5 + 0.15 * hits)
    for kw in load_maps().scene_by_id(scene_id).get("keywords", []):
        if kw in message and kw in text:
            score += 0.04
    return round(score, 4)


def collect(scene_id: str, message: str, top_n: int = DEFAULT_TOP_N) -> list[Statute]:
    """返回按相关度排序的前 top_n 条法条（映射路径）。"""
    scene = load_maps().scene_by_id(scene_id)
    if not scene:
        return []

    cands: list[Statute] = []
    for idx, ref in enumerate(scene["statutes"]):
        rec = _fetch(ref["law"], ref["article"])
        if not rec:
            log("statute.missing", level="warn", scene=scene_id,
                key=f"{ref['law']}|{ref['article']}", reason="not_in_db")
            continue
        text = rec["article_text"] or ""
        if not _passes_keyword_guard(text, ref.get("verify_any", [])):
            log("statute.mismatch", level="warn", scene=scene_id,
                key=f"{ref['law']}|{ref['article']}", reason="text_mismatch")
            continue
        cands.append(Statute(
            law_name=rec["law_name"],
            article_no=rec["article_no"],
            article_no_arabic=rec["article_no_arabic"],
            text=text,
            topic=ref.get("topic", ""),
            source="映射核验",
            score=_relevance(scene_id, message, idx, ref, text),
            explain=load_maps().explain_for(ref["law"], ref["article"]),
            chapter=rec.get("chapter") or "",
            status=rec.get("status") or "",
            article_id=rec["id"],
        ))

    cands.sort(key=lambda s: -s.score)
    return cands[:top_n]


def search_raw(query: str, top_k: int = DEFAULT_TOP_N) -> list[Statute]:
    """BM25 兜底检索（超出 5 个专项领域时使用），结果标记 out_of_scope。"""
    ret = lawkb.retrieval
    if not ret:
        log("fallback.unavailable", level="warn", reason=lawkb.RETRIEVAL_ERROR or "no retrieval module")
        return []
    try:
        rows = ret.search(query, top_k=top_k)
    except Exception as exc:  # 兜底路径失败绝不影响主流程
        log("fallback.error", level="error", error=str(exc)[:200])
        return []

    out: list[Statute] = []
    for r in rows:
        text = r.get("article_text") or ""
        if not text or float(r.get("score") or 0) <= 0:
            continue
        out.append(Statute(
            law_name=r["law_name"],
            article_no=r["article_no"],
            article_no_arabic=r["article_no_arabic"],
            text=text,
            topic="（按你的描述检索到的相关条文）",
            source="知识库语义检索",
            score=float(r.get("score") or 0),
            explain=None,
            out_of_scope=True,
            chapter=r.get("chapter") or "",
            status=r.get("status") or "",
            article_id=r.get("id"),
        ))
    return out
