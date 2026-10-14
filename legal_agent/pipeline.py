"""编排：护栏 → 场景识别 → 槽位校验 → 法条求解 → 通俗解释 → 行动建议 → 组装输出。

固定流水线，无自主循环（C3/C5）：每一步都是纯函数或只读查询，可重放、可断言。
返回结构化结果；任何阶段异常都被收敛为 error 结果并记 trace，不向用户抛栈。
"""
from __future__ import annotations

import re

from . import advice as advice_mod
from . import explain as explain_mod
from . import facts as facts_mod
from . import guard as guard_mod
from . import obs
from . import presenter
from . import scene as scene_mod
from . import slots as slots_mod
from . import statutes as statutes_mod

_GENERIC_MISSING = [{
    "field": "facts",
    "question": "能补充一下关键事实吗？",
    "example": "例如：什么时候发生的、对方是谁、涉及多少钱、你手里有哪些证据（合同 / 聊天记录 / 转账凭证 / 截图）。",
}]

# 需求：不输出法律文书模板，除非用户明确要求。命中后不做硬拦截，只在数据说明里给出柔性提示。
_TEMPLATE_RE = re.compile(r"(起诉状|答辩状|律师函|法律文书|申请书|仲裁申请书).{0,6}(模板|范文|格式|怎么写|写一份|起草)")


def handle(
    *,
    message: str,
    history: list[str] | None = None,
    scene_id: str | None = None,
    session: dict | None = None,
    on_stage=None,
) -> dict:
    trace_id = obs.new_trace_id()
    t0 = obs.time.perf_counter()
    timings: dict[str, float] = {}
    history = history or []

    def stage(name: str, **extra) -> None:
        if callable(on_stage):
            try:
                on_stage(name, extra)
            except Exception:  # 回调异常绝不影响判定与结果
                pass

    try:
        # ---- 1. 护栏 ----
        sp = obs.span(trace_id, "guard")
        g = guard_mod.guard(message)
        timings["guard"] = sp.ok(passed=g.ok, code=g.code)
        stage("guard", ok=g.ok, code=g.code)
        if not g.ok:
            obs.METRICS.bump("response.blocked")
            return presenter.render_blocked(g.code, g.message, trace_id)

        text = g.text
        cumulative = " ".join([*history, text]).strip()

        # ---- 2. 场景识别 ----
        # 分支必须由「用户输入」驱动：先对用户原话做识别；前端传入的 scene_id 只作为
        # 识别不出领域时的兜底提示——绝不能反过来覆盖用户这次实际说的话，
        # 否则前端残留的 sceneId 会让后续所有问题都被强行归到同一个场景（回答一模一样）。
        sp = obs.span(trace_id, "scene")
        sc = scene_mod.classify(text)
        scene_source = "input"
        if not sc.known and scene_id:
            hint = _hint_scene(scene_id, text)
            if hint.known:
                sc = hint
                scene_source = "hint"
        timings["scene"] = sp.ok(scene=sc.scene_id, score=sc.score,
                                 confidence=sc.confidence, source=scene_source)
        stage("scene", scene=sc.scene_id, confidence=sc.confidence, source=scene_source)

        if sc.is_greeting:
            obs.METRICS.bump("response.prompt")
            return presenter.render_prompt(trace_id)

        # ---- 3. 槽位校验 ----
        sp = obs.span(trace_id, "slots")
        missing = slots_mod.missing_slots(sc.scene_id, cumulative) if sc.known else list(_GENERIC_MISSING)
        timings["slots"] = sp.ok(missing=len(missing))
        stage("slots", missing=len(missing))

        # ---- 4. 法条求解 ----
        sp = obs.span(trace_id, "statutes")
        if sc.known:
            found = statutes_mod.collect(sc.scene_id, text)
            mode = "mapped"
        else:
            found = statutes_mod.search_raw(text)
            mode = "fallback"
        timings["statutes"] = sp.ok(count=len(found), mode=mode)
        stage("statutes", count=len(found), mode=mode)

        # ---- 5. 通俗解释 ----
        sp = obs.span(trace_id, "explain")
        explains = explain_mod.explain_all(found)
        timings["explain"] = sp.ok(count=len(explains))
        stage("explain", count=len(explains))

        # ---- 6. 事实抽取（让结论与建议跟随用户这次说的具体情况） ----
        sp = obs.span(trace_id, "facts")
        fx = facts_mod.extract(sc.scene_id, text)
        timings["facts"] = sp.ok(points=len(fx.points))
        stage("facts", points=len(fx.points))

        # ---- 7. 行动建议 ----
        sp = obs.span(trace_id, "advice")
        actions = advice_mod.advise(sc.scene_id, fx)
        timings["advice"] = sp.ok(steps=len(actions))
        stage("advice", steps=len(actions))

        # ---- 8. 组装输出 ----
        sp = obs.span(trace_id, "present")
        t_host = obs.time.perf_counter()
        extra_notes: list[str] = []
        if _TEMPLATE_RE.search(text):
            extra_notes.append(
                "你提到了法律文书模板：本系统不直接产出正式文书（起诉状、答辩状等）。"
                "我可以帮你把案情要点、请求事项和证据清单整理成提纲，正式文书建议由你本人或律师起草。"
            )
        result = presenter.build(
            scene={"id": sc.scene_id, "name": sc.name, "confidence": sc.confidence, "score": sc.score},
            statutes=found,
            explains=explains,
            actions=actions,
            missing=missing,
            trace_id=trace_id,
            timings={},
            mode=mode,
            extra_notes=extra_notes,
            facts=fx,
        )
        timings["present"] = sp.ok(chars=len(result["plainText"]))
        stage("present", statutes=len(found))

        timings["total"] = round((obs.time.perf_counter() - t0) * 1000, 2)
        result["timings"] = timings
        result["plainText"] = presenter.render_text(result)

        obs.METRICS.bump("response.answer")
        obs.METRICS.bump(f"scene.{sc.scene_id}")
        obs.METRICS.observe_ms(timings["total"])
        obs.log("request.done", trace_id=trace_id, scene=sc.scene_id, mode=mode,
                statutes=len(found), total_ms=timings["total"])
        return result

    except Exception as exc:  # 收敛所有异常，绝不让栈冒到用户面前
        obs.METRICS.bump("response.error")
        obs.log("request.error", level="error", trace_id=trace_id, error=obs.redact(str(exc), 300))
        return {
            "type": "error",
            "message": "内部处理出错，请稍后重试，或换个说法再描述一次。",
            "detail": f"{type(exc).__name__}: {exc}",
            "traceId": trace_id,
        }


def _hint_scene(scene_id: str, text: str) -> scene_mod.SceneResult:
    """前端「场景快捷入口」传来、且用户原话识别不出领域时的兜底提示。

    仅在 classify(text) 判定为 unknown 时生效——用户输入永远优先于界面残留的选择。
    """
    from .maps import load_maps
    sid = scene_id if scene_id in {s["id"] for s in load_maps().scenes} else "unknown"
    scene = load_maps().scene_by_id(sid)
    return scene_mod.SceneResult(sid, scene["name"] if scene else "其他", 0, "hint")
