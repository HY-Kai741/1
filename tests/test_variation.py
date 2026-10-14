#!/usr/bin/env python3
"""回答隔离性回归测试。

锁住两类曾经出现过的缺陷：
  V1 分支写死：前端残留的 sceneId 覆盖了用户输入 → 不同问题被强行归到同一场景。
  V2 模板写死：同一场景内不同问题拿到完全相同的结论与建议。

断言：
  V1  语义不同的消息 → plainText 两两不同（同一进程内连续调用，可排除缓存假象）；
  V2a 用过期 sceneId 提问，场景仍必须由用户输入决定；
  V2b 同一领域内、事实不同的两条问题 → 结论（conclusion）必须不同；
  V2c 同一领域内、事实不同的两条问题 → 行动建议（actions 文本）必须不同；
  V3  同一请求重复执行两次 → 结果稳定（确定性，排除随机/缓存污染）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("LAW_LOG_LEVEL", "error")   # 静音阶段日志，保持测试输出可读

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from legal_agent.pipeline import handle  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("[PASS] " if ok else "[FAIL] ") + name + (("  └─ " + detail) if detail and not ok else ""))
    if not ok:
        FAILS.append(name + (" :: " + detail if detail else ""))


def ask(msg: str, scene_id: str | None = None) -> dict:
    return handle(message=msg, scene_id=scene_id)


# --------------------------------------------------------------- V1
DISTINCT = [
    ("租房押金", "我大二，在学校附近租房，退租时房东不退我押金，说我把墙弄脏了，押金2000元，签了纸质合同。"),
    ("兼职欠薪", "我在奶茶店兼职，老板拖欠我两个月工资不给，说好20元一小时，有排班表。"),
    ("网购退货", "我在淘宝买的东西和描述不符，收到才3天，商家不给退，有订单截图和开箱视频。"),
    ("校园贷", "我在网贷平台借了5000实际到手4200，月息很高，还不上，他们打电话给我同学和父母。"),
    ("论文抄袭", "我的课程论文被同学抄了，改了个标题就提交了，我有原稿和提交记录。"),
    ("网络与个人信息", "我下载一个 App，不给它通讯录权限就不让用，还总接到推销电话。"),
    ("婚恋财产", "我和对象恋爱两年，期间转了不少钱，现在分手了想要回来，一共3万。"),
    ("交通事故", "我骑电动车被一辆车撞了，对方全责，我住院花了8000，想问他怎么赔偿。"),
    ("无关问题", "今天天气怎么样？推荐一家附近的火锅店。"),
]

texts = {}
for tag, msg in DISTINCT:
    r = ask(msg)
    texts[tag] = r.get("plainText") or ""
uniq = len({v for v in texts.values()})
check(f"V1 不同问题回答必须不同（{len(texts)} 条 → {uniq} 种）", uniq == len(texts),
      "出现重复回答：" + ", ".join(t for t in texts if list(texts.values()).count(texts[t]) > 1))


# --------------------------------------------------------------- V2a
stale = ask("我在网贷平台借了5000，到手4200，月息很高，他们还打电话骚扰我同学。", scene_id="rental")
check("V2a 残留 sceneId=rental 不得覆盖「校园贷」输入",
      (stale.get("scene") or {}).get("id") == "loan",
      f"实际 scene={(stale.get('scene') or {}).get('id')}")

hint_ok = ask("嗯……就那个事儿，对方一直拖着不给。", scene_id="rental")
check("V2a-2 识别不出领域时，sceneId 仍可作为兜底提示",
      (hint_ok.get("scene") or {}).get("id") == "rental",
      f"实际 scene={(hint_ok.get('scene') or {}).get('id')}")


# --------------------------------------------------------------- V2b / V2c
PAIRS = [
    ("rental", "退租时房东不退我押金，押金2000元，签了纸质合同，有微信转账记录。",
               "我租的房子漏水，房东一直不修，我自己找人修了，花了800元，想找他报销。"),
    ("loan", "我在网贷平台借了5000，实际到手4200，先扣了800服务费。",
             "我借的钱现在逾期了还不上，对方天天打电话爆我通讯录、威胁我父母。"),
    ("ip", "我的课程论文被同学抄了，改了个标题就提交了，我有原稿和提交记录。",
           "我的设计作品被人发到网上赚流量，完全没署我的名字。"),
    ("parttime", "我在奶茶店兼职，老板拖欠我两个月工资3200元，有排班表。",
                 "老板说要先试用两周，试用期不给工资，这合法吗？"),
    ("consumer", "我在淘宝买的东西和描述不符，收到才3天，商家不给退。",
                 "我在网上买到假货，商家不承认，想要求三倍赔偿。"),
    ("network", "App 不给通讯录权限就不让用，还总接到推销电话，怀疑信息被泄露了。",
                "有人让我把银行卡借给他过账，说给我500块好处费。"),
    ("marriage", "我和对象恋爱两年，期间转了不少钱，现在分手了想要回来，一共3万。",
                 "我和对象订婚给了10万彩礼，现在婚没结成，彩礼能退吗？"),
    ("injury", "我骑电动车被一辆车撞了，对方全责，我住院花了8000，想问他怎么赔偿。",
               "同学开车免费捎我回家，路上出事故我受伤了，我能找他赔吗？"),
]

for scene_id, a_msg, b_msg in PAIRS:
    ra, rb = ask(a_msg), ask(b_msg)
    sa, sb = (ra.get("scene") or {}).get("id"), (rb.get("scene") or {}).get("id")
    check(f"V2b/{scene_id} 同领域不同事实 → 结论不同",
          sa == scene_id and sb == scene_id and ra.get("conclusion") != rb.get("conclusion"),
          f"scene=({sa},{sb}) conclusion_same={ra.get('conclusion') == rb.get('conclusion')}")
    ta = " | ".join(x["text"] for x in ra.get("actions") or [])
    tb = " | ".join(x["text"] for x in rb.get("actions") or [])
    check(f"V2c/{scene_id} 同领域不同事实 → 建议不同", ta != tb, "行动建议完全相同")


# --------------------------------------------------------------- V3
r1 = ask(DISTINCT[0][1])
r2 = ask(DISTINCT[0][1])
check("V3 同一问题重复执行结果稳定（无随机/缓存污染）",
      r1.get("plainText") == r2.get("plainText"), "两次结果不一致")


print("\n" + "=" * 60)
if FAILS:
    print(f"失败 {len(FAILS)} 项：")
    for f in FAILS:
        print("  - " + f)
    raise SystemExit(1)
print("回答隔离性回归：全部通过")
