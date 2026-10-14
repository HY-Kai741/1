"""事实抽取：从用户原话里摘出可核验的具体事实（金额 / 凭证 / 时间 / 对方行为）。

为什么要这一层：
    场景级的固定结论会让「同一个领域里的不同问题」拿到一模一样的回答。
    本模块把用户这次实际说了什么摘出来，交给展示层拼进结论、交给建议层做条件化补充，
    使回答随事实变化——而不是无论问什么都回同一段模板。

原则：
    - 只做「摘录」，不做「推断」：抽不到就留空，绝不替用户补事实；
    - 纯规则（正则 + 关键词），无模型、可重放、可断言；
    - points 给人看（事实清单），flags 给建议层用（布尔标记）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 「N 元 / N 元」这类带单位的金额
_AMOUNT_UNIT = re.compile(r"(\d{2,7}(?:\.\d+)?)\s*(万元|万|千元|元|块钱|块)")
# 「借了 5000」「到手 4200」这类无单位的金额，靠上下文词锚定
_AMOUNT_CTX = re.compile(
    r"(押金|租金|房租|月租|借了|借|到手|欠|拖欠|被扣|扣了|服务费|手续费|花费|花了|付了|交了|收了|赔|工资|报酬)"
    r"[^\d，。；,;]{0,4}(\d{2,7})"
)
_MONTHS = re.compile(r"([一二三四五六七八九十两\d]{1,3})\s*个?\s*月")
_DAYS = re.compile(r"(\d{1,3})\s*天")
_RATE = re.compile(r"月息\s*([\d.]+)\s*%|([\d.]+)\s*%\s*(?:的)?月|年(?:利)?息\s*([\d.]+)\s*%")

_CN_DIGIT = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6,
             "七": 7, "八": 8, "九": 9, "十": 10}


def _months_to_int(raw: str) -> str:
    """把「两 / 二 / 十二」这类中文月数转成阿拉伯数字，便于阅读。超出能力范围就原样返回。"""
    s = (raw or "").strip()
    if s.isdigit():
        return s
    if s in _CN_DIGIT:
        return str(_CN_DIGIT[s])
    if len(s) == 2:
        a, b = s[0], s[1]
        if a == "十" and b in _CN_DIGIT:          # 十一 ~ 十九
            return str(10 + _CN_DIGIT[b])
        if b == "十" and a in _CN_DIGIT:          # 二十 ~ 九十
            return str(_CN_DIGIT[a] * 10)
    if len(s) == 3 and s[1] == "十" and s[0] in _CN_DIGIT and s[2] in _CN_DIGIT:
        return str(_CN_DIGIT[s[0]] * 10 + _CN_DIGIT[s[2]])   # 二十一 ~ 九十九
    return s


@dataclass
class Facts:
    scene_id: str
    points: list[str] = field(default_factory=list)
    flags: dict[str, bool] = field(default_factory=dict)

    def has(self, name: str) -> bool:
        return bool(self.flags.get(name))

    def summary(self) -> str:
        return "；".join(self.points)

    def as_dict(self) -> dict:
        return {"points": self.points, "flags": {k: True for k, v in self.flags.items() if v}}


def _hit(text: str, *words: str) -> bool:
    return any(w in text for w in words)


def _amounts(text: str) -> list[int]:
    vals: list[int] = []
    for m in _AMOUNT_UNIT.finditer(text):
        v = float(m.group(1))
        vals.append(int(v * 10000) if m.group(2) in ("万元", "万") else int(v))
    for m in _AMOUNT_CTX.finditer(text):
        vals.append(int(m.group(2)))
    return vals


def _fmt(n: int) -> str:
    return f"{n} 元"


# ---------------------------------------------------------------- 各领域

def _rental(text: str, f: Facts) -> None:
    amts = [a for a in _amounts(text) if a >= 100]
    if amts:
        f.points.append(f"涉及金额约 {_fmt(max(amts))}")
    if _hit(text, "没签", "没有合同", "没合同", "未签", "口头", "微信上谈", "只在微信", "没约定"):
        f.flags["no_contract"] = True
        f.points.append("没有书面租赁合同（仅有口头或微信约定）")
    elif _hit(text, "签了", "签过", "纸质合同", "书面合同", "合同上"):
        f.flags["has_contract"] = True
        f.points.append("已签（纸质/书面）租赁合同")
    if _hit(text, "转账记录", "微信转账", "押金条", "收据", "凭证", "截图", "支付记录"):
        f.flags["has_proof"] = True
        f.points.append("有押金支付凭证（转账记录 / 押金条 / 截图）")
    elif _hit(text, "现金"):
        f.flags["no_proof"] = True
        f.points.append("押金为现金支付、没有收据")
    elif "押金" in text:
        f.flags["no_proof"] = True
    if _hit(text, "弄脏", "损坏", "弄坏", "划痕", "墙皮", "墙", "地板", "家具", "赔"):
        f.flags["damage_claim"] = True
        f.points.append("房东以「房屋受损」为由主张扣押金")
    if _hit(text, "漏水", "维修", "坏了", "不修", "自己修"):
        f.flags["repair"] = True
        f.points.append("房屋存在需维修问题、房东未处理")
    if _hit(text, "二房东", "转租"):
        f.flags["sublet"] = True
        f.points.append("涉及二房东 / 转租关系")
    if _hit(text, "中介"):
        f.flags["agent"] = True


def _parttime(text: str, f: Facts) -> None:
    amts = [a for a in _amounts(text) if a >= 50]
    owed = [a for a in amts if a >= 200]
    if owed:
        f.points.append(f"被拖欠金额约 {_fmt(max(owed))}")
    m = _MONTHS.search(text)
    if m:
        f.points.append(f"拖欠时长约 {_months_to_int(m.group(1))} 个月")
    if _hit(text, "元一小时", "元/小时", "时薪", "一小时", "小时工"):
        rate = re.search(r"(\d{2,4})\s*元\s*[/每]?\s*小时|时薪\s*(\d{2,4})", text)
        if rate:
            f.points.append(f"约定报酬约 {rate.group(1) or rate.group(2)} 元/小时")
        f.flags["hourly"] = True
    if _hit(text, "微信", "聊天记录", "排班表", "考勤", "打卡", "书面", "合同"):
        f.flags["has_agreement"] = True
        f.points.append("有约定或管理痕迹（聊天记录 / 排班表 / 考勤）")
    else:
        f.flags["no_agreement"] = True
    if _hit(text, "试用期", "试用"):
        f.flags["probation"] = True
        f.points.append("对方设定了试用期")
    if _hit(text, "学长", "朋友", "个人", "私人", "介绍"):
        f.flags["personal"] = True
        f.points.append("雇主偏个人（同学 / 朋友 / 私人介绍）")


def _consumer(text: str, f: Facts) -> None:
    ch = next((c for c in ("淘宝", "天猫", "京东", "拼多多", "抖音", "闲鱼", "微商", "小红书", "直播间") if c in text), None)
    if ch:
        f.points.append(f"购买渠道：{ch}")
    elif _hit(text, "线下", "实体店", "门店"):
        f.points.append("购买渠道：线下实体店")
    d = _DAYS.search(text)
    if d:
        days = int(d.group(1))
        f.points.append(f"收到货约 {days} 天（{'在' if days <= 7 else '已超过'}七天无理由范围内）")
        f.flags["within_7d"] = days <= 7
    if _hit(text, "描述不符", "和描述不一样", "不符", "不一样"):
        f.flags["mismatch"] = True
        f.points.append("商品与描述不符")
    if _hit(text, "假货", "假冒", "骗", "欺诈", "虚假宣传"):
        f.flags["fraud"] = True
        f.points.append("怀疑存在欺诈（假货 / 虚假宣传）")
    if _hit(text, "质量", "坏了", "次品", "破损", "瑕疵"):
        f.flags["quality"] = True
    if _hit(text, "过期", "变质", "异物", "吃出", "食品安全"):
        f.flags["food"] = True
        f.points.append("涉及食品安全问题（过期 / 变质 / 异物）")
    if _hit(text, "截图", "开箱视频", "聊天记录", "订单", "凭证"):
        f.flags["has_evidence"] = True
        f.points.append("留有订单 / 截图 / 开箱视频等证据")
    else:
        f.flags["no_evidence"] = True


def _ip(text: str, f: Facts) -> None:
    work = next((w for w in ("论文", "课程论文", "文章", "设计", "代码", "照片", "插画", "视频", "PPT", "作业") if w in text), None)
    if work:
        f.points.append(f"涉事作品类型：{work}")
    if _hit(text, "同学", "室友", "同班", "别人"):
        f.flags["peer"] = True
        f.points.append("涉嫌侵权方为同学 / 同校人员")
    if _hit(text, "提交", "发表", "期刊", "投稿", "评奖", "参赛"):
        f.flags["published"] = True
        f.points.append("对方将作品用于提交 / 发表 / 评奖")
    if _hit(text, "发到网上", "赚流量", "带货", "商用", "营利", "赚钱"):
        f.flags["commercial"] = True
        f.points.append("对方存在网络传播 / 营利使用")
    if _hit(text, "原稿", "提交记录", "查重", "相似度", "时间戳", "修改记录"):
        f.flags["has_proof"] = True
        f.points.append("有原稿 / 提交记录 / 查重结果等时间先后证据")
    else:
        f.flags["no_proof"] = True
    if _hit(text, "没署名", "没提我名字", "没写我", "改了标题", "改头换面", "改个标题"):
        f.flags["no_attribution"] = True
        f.points.append("对方未署名，或改动后据为己有")


def _loan(text: str, f: Facts) -> None:
    borrowed = None
    received = None
    mb = re.search(r"借(?:了|款)?\D{0,4}(\d{2,7})", text)
    if mb:
        borrowed = int(mb.group(1))
    mr = re.search(r"(?:到手|拿到|实际收到|实际到手)\D{0,4}(\d{2,7})", text)
    if mr:
        received = int(mr.group(1))
    if borrowed:
        f.points.append(f"合同金额约 {_fmt(borrowed)}")
    if received:
        f.points.append(f"实际到手约 {_fmt(received)}")
    if _hit(text, "服务费", "手续费", "先扣", "扣了", "砍头息") or (
        borrowed and received and received < borrowed
    ):
        f.flags["cut_interest"] = True
        f.points.append("存在「先扣费用 / 砍头息」情形")
    r = _RATE.search(text)
    if r:
        f.points.append(f"约定利率约 {r.group(1) or r.group(2) or r.group(3)}%/月")
        f.flags["high_rate"] = True
    elif _hit(text, "高利", "高息", "月息很高", "利息很高", "很高"):
        f.flags["high_rate"] = True
        f.points.append("当事人认为利息明显偏高")
    if _hit(text, "打电话", "电话", "骚扰", "通讯录", "爆", "打给我同学", "打给我父母", "父母", "同学和父母", "威胁", "恐吓", "上门"):
        f.flags["violent_collection"] = True
        f.points.append("对方存在向亲友施压 / 威胁式催收")
    if _hit(text, "还不上", "还不起", "逾期", "还不完", "逾期费"):
        f.flags["overdue"] = True
        f.points.append("目前已逾期或无力按期偿还")
    ch = next((c for c in ("网贷", "校园贷", "花呗", "借呗", "信用卡", "分期") if c in text), None)
    if ch:
        f.points.append(f"借款渠道：{ch}")


def _network(text: str, f: Facts) -> None:
    info = [w for w in ("人脸", "刷脸", "身份证", "通讯录", "位置", "定位", "行踪",
                        "手机号", "相册", "照片", "姓名") if w in text]
    if info:
        f.points.append("涉及的信息类型：" + "、".join(dict.fromkeys(info)))
    if _hit(text, "强制", "不给权限", "不同意就", "不授权就", "必须授权", "不同意就不"):
        f.flags["force_authorize"] = True
        f.points.append("对方以「不授权就不能使用」相要挟（强制授权）")
    if _hit(text, "泄露", "被泄露", "倒卖", "被曝光", "被卖了"):
        f.flags["leak"] = True
        f.points.append("个人信息已被泄露或被他人获知")
    if _hit(text, "银行卡", "电话卡", "借给别人", "出借", "卖卡", "跑分", "过账",
             "代收", "代付", "刷流水"):
        f.flags["two_cards"] = True
        f.points.append("涉及出租、出借银行卡 / 电话卡或代人过账")
    if _hit(text, "诈骗", "刷单", "返利", "冒充客服", "验证码", "钓鱼", "垫付", "先交钱"):
        f.flags["scam"] = True
        f.points.append("存在疑似电信网络诈骗的情形（刷单返利 / 冒充客服 / 索要验证码等）")
    src = next((c for c in ("App", "app", "小程序", "平台", "网站") if c in text), None)
    if src:
        f.points.append(f"对方为网络平台或应用（{src}）")
    if _hit(text, "陌生", "来电", "短信"):
        f.flags["stranger"] = True
        f.points.append("对方通过来电 / 短信等远程方式联系")
    if _hit(text, "未成年", "未满 18", "17 岁", "16 岁"):
        f.flags["minor"] = True
        f.points.append("当事人可能为未成年人")
    amts = [a for a in _amounts(text) if a >= 50]
    if amts:
        f.points.append(f"涉及金额约 {_fmt(max(amts))}")
    if _hit(text, "截图", "聊天记录", "通话", "录音", "转账记录", "订单", "凭证"):
        f.flags["has_evidence"] = True
        f.points.append("留有截图 / 聊天或通话记录等证据")
    else:
        f.flags["no_evidence"] = True


def _marriage(text: str, f: Facts) -> None:
    if _hit(text, "恋爱", "男朋友", "女朋友", "前男友", "前女友", "对象"):
        f.points.append("双方为恋爱关系")
    if _hit(text, "分手"):
        f.points.append("双方已分手")
    if _hit(text, "订婚", "彩礼", "聘礼", "嫁妆"):
        f.flags["betrothal"] = True
        f.points.append("涉及婚前给付（彩礼 / 聘礼 / 嫁妆）")
    if _hit(text, "已婚", "已经结婚", "已结婚", "领证了", "登记结婚", "结了婚"):
        f.points.append("已办理结婚登记或处于婚姻关系")
    elif _hit(text, "准备结婚", "想结婚", "打算结婚", "不结婚", "没结婚", "没领证", "婚没结成", "还没结"):
        f.points.append("尚未完成结婚登记")
    if _hit(text, "离婚", "净身出户"):
        f.flags["divorce"] = True
        f.points.append("涉及离婚与财产分割")
    if _hit(text, "借", "还给我", "要我还", "欠"):
        f.flags["loan_nature"] = True
        f.points.append("存在主张为借款的表述")
    if _hit(text, "礼物", "送", "赠与", "生日", "节日", "520", "1314", "情人节", "日常"):
        f.flags["gift"] = True
        f.points.append("该笔支出可能是赠与（礼物 / 节日 / 日常消费）")
        if _hit(text, "520", "1314", "礼物", "生日", "节日", "情人节", "日常"):
            f.flags["gift_small"] = True
    amts = [a for a in _amounts(text) if a >= 100]
    if amts:
        f.points.append(f"涉及金额约 {_fmt(max(amts))}")
    if _hit(text, "家暴", "家庭暴力", "殴打", "打我"):
        f.flags["violence"] = True
        f.points.append("涉及家庭暴力")
    if _hit(text, "共同债务", "债务", "欠款", "贷款"):
        f.flags["debt"] = True
        f.points.append("涉及债务归属问题")
    if _hit(text, "转账记录", "流水", "聊天记录", "截图", "凭证", "转账"):
        f.flags["has_proof"] = True
        f.points.append("有转账记录 / 聊天记录等凭证")
    else:
        f.flags["no_proof"] = True


def _injury(text: str, f: Facts) -> None:
    role = None
    if _hit(text, "行人", "走路", "步行"):
        role = "行人"
    elif _hit(text, "坐车", "乘车", "乘客", "坐在", "捎我", "捎带", "带我", "顺路"):
        role = "乘车人"
    elif _hit(text, "电动车", "自行车", "骑车", "摩托"):
        role = "非机动车（骑车人）"
    elif _hit(text, "我开车", "我开的", "我驾驶", "我是司机", "驾车"):
        role = "机动车驾驶人"
    if role:
        f.points.append(f"当事人在事故中的身份：{role}")
    if _hit(text, "认定书", "交警", "全责", "主责", "次责", "同等责任"):
        f.flags["has_report"] = True
        f.points.append("已有交警介入或事故责任认定")
    else:
        f.flags["no_report"] = True
    loss = [w for w in ("医疗费", "医药费", "住院", "手术", "误工", "护理", "伤残",
                        "骨折", "受伤", "擦伤", "缝针", "修车", "财产损失") if w in text]
    if loss:
        f.points.append("已发生的损害：" + "、".join(dict.fromkeys(loss)))
    if _hit(text, "逃逸", "跑了", "撞了就", "肇事逃逸"):
        f.flags["hit_and_run"] = True
        f.points.append("对方存在逃逸情形")
    if _hit(text, "顺路", "捎", "带我", "免费", "好意"):
        f.flags["hitchhike"] = True
        f.points.append("涉及好意同乘（无偿搭乘）")
    if _hit(text, "没保险", "没买保险", "未投保"):
        f.flags["no_insurance"] = True
        f.points.append("对方可能未投保或保险不足")
    if _hit(text, "保险", "交强险", "商业险"):
        f.points.append("已涉及保险理赔")
    if _hit(text, "我也有责任", "闯红灯", "逆行", "我全责", "我的责任", "次要责任"):
        f.flags["own_fault"] = True
        f.points.append("当事人自身可能也存在过错")
    if _hit(text, "未成年", "小孩", "未满 18"):
        f.flags["minors"] = True
        f.points.append("涉及未成年人")
    amts = [a for a in _amounts(text) if a >= 100]
    if amts:
        f.points.append(f"涉及金额约 {_fmt(max(amts))}")


_EXTRACTORS = {
    "rental": _rental,
    "parttime": _parttime,
    "consumer": _consumer,
    "ip": _ip,
    "loan": _loan,
    "network": _network,
    "marriage": _marriage,
    "injury": _injury,
}


def extract(scene_id: str, text: str) -> Facts:
    """抽取本轮事实。未知场景返回空的 Facts（不臆测）。"""
    f = Facts(scene_id=scene_id)
    fn = _EXTRACTORS.get(scene_id)
    if fn:
        try:
            fn(text or "", f)
        except Exception:  # 抽取失败绝不影响主流程
            pass
    # 去重保序
    seen: set[str] = set()
    f.points = [p for p in f.points if not (p in seen or seen.add(p))]
    return f
