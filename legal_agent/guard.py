"""护栏：输入校验 + 提示注入拦截 + 拒答红线。

返回 GuardResult；被拦截时 ok=False，由 pipeline 直接产出拦截卡片，不进入检索。
拦截不消耗检索资源，也不落原文日志（只记脱敏摘要）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_INPUT_CHARS = 2000
MIN_INPUT_CHARS = 2

_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"忽略(以上|之前|前面|上述).{0,6}(指令|指示|要求|设定|规则|提示)"),
    re.compile(r"(不要|别|无需|不必).{0,4}(遵守|理会|执行).{0,6}(规则|限制|指令|设定)"),
    re.compile(r"(输出|告诉我|重复|复述|展示|打印).{0,8}(系统提示词|系统提示|system\s*prompt|你的指令|prompt|设定内容)"),
    re.compile(r"(开发者模式|上帝模式|DAN模式|越狱模式|jailbreak)", re.I),
    re.compile(r"ignore\s+(all\s+)?(previous|above|prior)\s+(instructions?|prompts?)", re.I),
    re.compile(r"你现在(是|扮演|不再是|变成)"),
]

# 拒答红线：(触发正则, 一句合规替代方向)
_REDLINE: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(伪造|变造|假冒|做一份假的|p图).{0,8}(合同|协议|签名|印章|公章|证件|证据|聊天记录|转账记录|票据|身份证|简历|证明)"),
     "如果你想维护自己的权益，我可以帮你梳理**合法**的证据类型与举证路径。"),
    (re.compile(r"(帮我|教我|怎么|如何|怎样).{0,6}(逃避|躲掉|赖掉|不还|甩掉).{0,6}(债务|还款|借款|欠款|执行)"),
     "债务压力大是可以正面处理的——我可以帮你了解协商、展期与合法抗辩的路径。"),
    (re.compile(r"(转移|隐匿|藏起来).{0,6}(财产|资产).{0,8}(逃避|躲|不还|执行|追债)"),
     "财产与债务问题建议通过协商或司法途径解决，我可以说明这些合法途径的流程。"),
    (re.compile(r"(敲诈|勒索|威胁|恐吓).{0,6}(他|她|对方|同学|室友|房东|老板|商家)"),
     "如果你觉得权益被侵害，合法途径是投诉、调解或起诉；我可以帮你说明这些办法。"),
    (re.compile(r"(买卖|购买|搞到|弄到|查).{0,6}(他人|别人|同学).{0,4}(身份证|个人信息|隐私|通讯录|位置)"),
     "个人信息受法律保护，我不能协助获取他人信息；如果你自己的信息被泄露，我可以说明维权路径。"),
    (re.compile(r"(破解|侵入|盗取|黑进).{0,6}(账号|系统|网站|平台|数据库)"),
     "网络安全问题有合规的处理方式；如果你遭遇账号被盗或被入侵，我可以帮你梳理应对步骤。"),
]


@dataclass
class GuardResult:
    ok: bool
    text: str
    code: str = "ok"
    message: str = ""
    meta: dict = field(default_factory=dict)


def normalize(text: str) -> str:
    return re.sub(r"[ \t\u3000]+", " ", (text or "").replace("\r", "\n")).strip()


def guard(message: str) -> GuardResult:
    text = normalize(message)

    if len(text) < MIN_INPUT_CHARS:
        return GuardResult(False, text, "empty", "请先用一两句话描述你遇到的**具体情形**（发生了什么、和谁、涉及什么）。")

    if len(text) > MAX_INPUT_CHARS:
        return GuardResult(
            False, text, "too_long",
            f"你的描述超过了 {MAX_INPUT_CHARS} 字的处理上限。请精简到关键事实：**发生了什么、和谁、有什么证据、你的诉求**。",
            meta={"length": len(text)},
        )

    for pat in _INJECTION_PATTERNS:
        if pat.search(text):
            return GuardResult(False, text, "injection",
                               "这条消息看起来在试图修改我的工作规则，我不能照做。请直接描述你遇到的法律问题。")

    for pat, alternative in _REDLINE:
        if pat.search(text):
            return GuardResult(False, text, "refuse",
                               f"这个请求我不能协助——{alternative}")

    return GuardResult(True, text)
