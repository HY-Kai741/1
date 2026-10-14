"""输出组装：严格按需求要求的 6 段式结构产出。

结论 / 适用法条 / 通俗解释 / 行动建议 / 需要补充的信息 / 免责声明
（对应需求「输出格式要求」的 1–6 条，顺序与措辞保持一致。）
"""
from __future__ import annotations

from .facts import Facts
from .statutes import Statute

DISCLAIMER = "以上内容仅供参考，不构成法律意见，具体问题建议咨询专业律师。"

# 场景级「基准结论」。**不是最终结论**——最终结论由 compose_conclusion() 拼接本轮事实后产出，
# 否则同一领域内的不同问题会拿到一模一样的回答。
CONCLUSION: dict[str, str] = {
    "rental": "押金能不能扣、能扣多少，取决于你们的合同约定，以及房屋是否因你的不当使用而受损——正常使用造成的磨损不该由你承担。",
    "parttime": "兼职被欠薪，先分清你是「非全日制劳动关系」还是「个人劳务关系」，二者维权路径不同；无论哪种，被拖欠的报酬都可以依法追讨。",
    "consumer": "能不能退、怎么赔，取决于购买渠道、是否落在七天无理由退货范围内，以及商家有没有欺诈；先固定证据再走平台与投诉渠道，通常最省力。",
    "ip": "对方未经许可使用你的作品，可能构成著作权侵权；能主张什么，取决于作品是否受保护、对方如何使用，以及你能证明的时间先后与相似程度。",
    "loan": "高利放贷和「砍头息」在法律上受到限制；你需要先按实际到手金额核算真实成本，再判断哪些部分可以不认。",
    "unknown": "这个问题不在本系统专项覆盖的 5 个领域（租房 / 兼职 / 消费 / 知产 / 借贷）内，下面是按你的描述从法条库检索到的可能相关的条文，仅供参考。",
}

BOUNDARY_NOTES: dict[str, str] = {
    "rental": "《民法典》没有对「押金」单设条文，押金的性质与返还方式主要依据你们的合同约定来判断。",
    "parttime": "劳动关系与劳务关系的最终认定，由仲裁机构或法院结合排班、考勤、工资发放等事实综合判断，本系统只能提示区分方向。",
    "consumer": "网购七天无理由退货存在法定例外（定作、鲜活易腐、已拆封的音像制品或软件、报纸期刊等），是否适用要看具体商品。",
    "ip": "本系统收录的是法律条文，未收录司法解释；赔偿数额的最终认定还会受个案证据影响。",
    "loan": "《民法典》禁止高利放贷，但「利率受司法保护的具体上限」（如一年期 LPR 的倍数）由司法解释规定，不在本系统收录范围内，请另行核实。",
}

_SECTION_TITLES = ["结论", "适用法条", "通俗解释", "行动建议", "需要补充的信息", "免责声明"]

# 事实如何回灌进结论：每个领域一句「落点」，把基准判断落到用户这次说的事实上。
_FACT_LEAD = {
    "rental": "结合你这次描述的具体情况（{points}），下面按这些事实来判断。",
    "parttime": "结合你这次描述的具体情况（{points}），下面按这些事实区分路径。",
    "consumer": "结合你这次描述的具体情况（{points}），下面按这些事实来判断。",
    "ip": "结合你这次描述的具体情况（{points}），下面按这些事实来分析。",
    "loan": "结合你这次描述的具体情况（{points}），下面按这些事实来核算。",
}


def compose_conclusion(scene_id: str, facts: Facts | None) -> str:
    """基准结论 + 本轮事实。事实不同 → 结论不同。"""
    base = CONCLUSION.get(scene_id, CONCLUSION["unknown"])
    if not facts or not facts.points:
        return base
    lead = _FACT_LEAD.get(scene_id)
    if not lead:
        return base
    return base + " " + lead.format(points=facts.summary())


def _statute_view(s: Statute) -> dict:
    return {
        "law_name": s.law_name,
        "article_no": s.article_no,
        "article_no_arabic": s.article_no_arabic,
        "article_id": s.article_id,
        "citation": s.citation,
        "text": s.text,
        "topic": s.topic,
        "source": s.source,
        "out_of_scope": s.out_of_scope,
        "chapter": s.chapter,
        "score": s.score,
    }


def build(
    *,
    scene: dict,
    statutes: list[Statute],
    explains: list[str],
    actions: list[dict],
    missing: list[dict],
    trace_id: str,
    timings: dict,
    mode: str,
    extra_notes: list[str] | None = None,
    facts: Facts | None = None,
) -> dict:
    scene_id = scene["id"]
    conclusion = compose_conclusion(scene_id, facts)

    notes: list[str] = [
        "法条原文取自本地法律知识库（378 部法律 / 23,995 条现行条文），逐字引用，未作改写。",
    ]
    if not statutes:
        notes.append("本次未在知识库中命中原条文，未找到直接相关的法条。")
    if any(s.out_of_scope for s in statutes):
        notes.append("以下条文是按你的描述从法条库检索得到的，不在专项覆盖范围内，仅供参考，请务必核对原文。")
    if any(s.explain is None for s in statutes):
        notes.append("部分条文未预置通俗翻译，为避免解读偏差，请以法条原文为准。")
    if scene_id in BOUNDARY_NOTES:
        notes.append(BOUNDARY_NOTES[scene_id])
    notes.extend(extra_notes or [])

    statutes_view = []
    for s, ex in zip(statutes, explains):
        v = _statute_view(s)
        v["explain"] = ex
        statutes_view.append(v)

    result = {
        "type": "answer",
        "mode": mode,
        "scene": scene,
        "conclusion": conclusion,
        "facts": facts.as_dict() if facts else {"points": [], "flags": {}},
        "statutes": statutes_view,
        "actions": actions,
        "missing": missing,
        "disclaimer": DISCLAIMER,
        "notes": notes,
        "traceId": trace_id,
        "timings": timings,
    }
    result["plainText"] = render_text(result)
    return result


def render_text(r: dict) -> str:
    """6 段式纯文本（供接口/无障碍/评测断言使用）。"""
    lines: list[str] = []
    lines.append(f"一、{_SECTION_TITLES[0]}")
    lines.append(r["conclusion"])
    lines.append("")

    lines.append(f"二、{_SECTION_TITLES[1]}")
    if r["statutes"]:
        for s in r["statutes"]:
            lines.append(f"- {s['citation']}（{s['topic']}）")
            lines.append(f"  原文：{s['text']}")
    else:
        lines.append("未找到直接相关的法条。")
    lines.append("")

    lines.append(f"三、{_SECTION_TITLES[2]}")
    if r["statutes"]:
        for s in r["statutes"]:
            lines.append(f"- {s['citation']}：{s.get('explain') or '（以原文为准）'}")
    else:
        lines.append("无（未命中相关法条，请补充更多事实）。")
    lines.append("")

    lines.append(f"四、{_SECTION_TITLES[3]}")
    for a in r["actions"]:
        lines.append(f"- [{a['label']}] {a['text']}")
    lines.append("")

    lines.append(f"五、{_SECTION_TITLES[4]}")
    if r["missing"]:
        for m in r["missing"]:
            lines.append(f"- {m['question']}")
            if m.get("example"):
                lines.append(f"  （{m['example']}）")
    else:
        lines.append("无，你提供的信息已足够给出上面的分析与路径。")
    lines.append("")

    lines.append(f"六、{_SECTION_TITLES[5]}")
    lines.append(r["disclaimer"])
    lines.append("")
    lines.append("数据说明：")
    for n in r["notes"]:
        lines.append(f"- {n}")
    return "\n".join(lines)


def render_prompt(trace_id: str) -> dict:
    return {
        "type": "prompt",
        "message": "你好，我是面向在校大学生的法律常识问答助手。",
        "domains": [
            {"id": "rental", "name": "租房纠纷", "example": "房东不退我押金，说我弄脏了墙"},
            {"id": "parttime", "name": "兼职与劳动报酬", "example": "兼职老板拖欠我两个月工资"},
            {"id": "consumer", "name": "消费维权", "example": "网购的东西和描述不符，商家不给退"},
            {"id": "ip", "name": "知识产权", "example": "我写的论文被别人抄袭发表了"},
            {"id": "loan", "name": "借贷与校园贷风险", "example": "网贷利息很高，还被暴力催收"},
        ],
        "hint": "用一两句话描述你遇到的具体情况即可（发生了什么、和谁、有什么证据）。",
        "traceId": trace_id,
    }


def render_blocked(code: str, message: str, trace_id: str) -> dict:
    labels = {
        "empty": "请输入具体情形",
        "too_long": "输入过长",
        "injection": "已拦截",
        "refuse": "无法协助",
    }
    return {
        "type": "blocked",
        "code": code,
        "label": labels.get(code, "已拦截"),
        "message": message,
        "traceId": trace_id,
    }
