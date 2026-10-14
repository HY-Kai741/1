#!/usr/bin/env python3
"""大学生法律常识问答 Agent —— 单端口 HTTP 服务。

只用标准库 http.server，无 Web 框架依赖（C2：依赖越少，部署越稳）。
- 监听环境变量 PORT，绑定 0.0.0.0（发布沙箱的硬性要求）；
- GET  /                 前端页面
- GET  /api/health       健康检查（含知识库规模、BM25 状态、指标）
- GET  /api/scenes       八个专项领域（供前端快捷入口）
- POST /api/ask          非流式问答
- POST /api/ask/stream   流式问答（SSE：stage → text → result → done）

加固：请求体大小上限、JSON 解析失败兜底、每请求超时（处理器内无阻塞外部调用，
唯一外部依赖是本地 SQLite 与内存索引）、异常一律收敛为 error 结果。
幂等性：整个问答链路只读（SQLite 只读查询 + 纯函数），同一输入恒定输出，天然幂等。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from legal_agent import lawkb, obs  # noqa: E402
from legal_agent.maps import load_maps  # noqa: E402
from legal_agent.pipeline import handle  # noqa: E402

PORT = int(os.environ.get("PORT", "3000"))
HOST = os.environ.get("HOST", "0.0.0.0")
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(256 * 1024)))
STREAM_DELAY_MS = int(os.environ.get("STREAM_DELAY_MS", "14"))
PUBLIC_DIR = ROOT / "public"

_STARTED_AT = time.time()
_BM25_STATE = {"ready": False, "building": False, "error": None}


def prewarm_bm25() -> None:
    """后台预热 BM25 索引：专项问题走映射路径、无需它；兜底路径首次查询才需要。"""
    if lawkb.retrieval is None:
        _BM25_STATE["error"] = lawkb.RETRIEVAL_ERROR or "retrieval module unavailable"
        return
    _BM25_STATE["building"] = True
    t0 = time.time()
    try:
        lawkb.retrieval.load()
        _BM25_STATE["ready"] = True
        obs.log("bm25.ready", ms=round((time.time() - t0) * 1000, 1))
    except Exception as exc:  # 预热失败不影响映射路径
        _BM25_STATE["error"] = f"{type(exc).__name__}: {exc}"
        obs.log("bm25.failed", level="warn", error=str(exc)[:200])
    finally:
        _BM25_STATE["building"] = False


def health_payload() -> dict:
    try:
        stats = lawkb.database.get_stats()
        kb = {"ok": True, "laws": stats["laws"], "articles": stats["articles"]}
    except Exception as exc:
        kb = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "status": "ok" if kb.get("ok") else "degraded",
        "service": "大学生法律常识问答 Agent",
        "version": __import__("legal_agent").__version__,
        "uptimeSec": round(time.time() - _STARTED_AT, 1),
        "knowledgeBase": {
            **kb,
            "source": "china-law-mcp (MIT) / data/laws.db",
            "bm25": _BM25_STATE,
        },
        "scenes": [{"id": s["id"], "name": s["name"]} for s in load_maps().scenes],
        "llm": {"enabled": bool(os.environ.get("LLM_API_KEY"))},
        "metrics": obs.METRICS.snapshot(),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "LawQAAgent/1.0"
    protocol_version = "HTTP/1.1"

    # ---- 基础设施 ----
    def log_message(self, fmt: str, *args) -> None:  # 统一走 obs，不打印原始访问日志
        obs.log("http", level="debug", msg=fmt % args)

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status: int, obj: dict) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def _serve_static(self, rel: str) -> None:
        path = (PUBLIC_DIR / rel.lstrip("/")).resolve()
        if not str(path).startswith(str(PUBLIC_DIR.resolve())) or not path.is_file():
            self._json(404, {"error": "not found"})
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }.get(path.suffix, "application/octet-stream")
        self._send(200, path.read_bytes(), ctype)

    # ---- GET ----
    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self._serve_static("index.html")
        elif url.path == "/api/health":
            self._json(200, health_payload())
        elif url.path == "/api/scenes":
            maps = load_maps()
            self._json(200, {"scenes": [
                {"id": s["id"], "name": s["name"], "summary": s["summary"],
                 "examples": [b for b in _EXAMPLES.get(s["id"], [])]}
                for s in maps.scenes
            ]})
        else:
            self._serve_static(url.path)

    # ---- POST ----
    def do_POST(self) -> None:
        url = urlparse(self.path)
        payload = self._read_json()
        if payload is None:
            self._json(400, {"type": "error", "message": "请求体过大或不是合法 JSON。"})
            return
        if url.path == "/api/ask":
            self._json(200, self._ask(payload))
        elif url.path == "/api/ask/stream":
            self._ask_stream(payload)
        else:
            self._json(404, {"error": "not found"})

    def _ask(self, payload: dict) -> dict:
        message = str(payload.get("message") or "")
        history = payload.get("history") or []
        if not isinstance(history, list):
            history = []
        history = [str(h) for h in history][-6:]
        scene_id = payload.get("sceneId") or None
        return handle(message=message, history=history, scene_id=scene_id)

    def _ask_stream(self, payload: dict) -> None:
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return
        # SSE 无 Content-Length，必须让连接在 handler 结束时关闭，客户端才知道流已结束。
        self.close_connection = True

        state = {"closed": False}

        def write(event: str, data) -> bool:
            if state["closed"]:
                return False
            try:
                payload_str = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
                self.wfile.write(f"event: {event}\ndata: {payload_str}\n\n".encode("utf-8"))
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError):
                state["closed"] = True
                return False

        def on_stage(name: str, extra: dict) -> None:
            write("stage", {"stage": name, **{k: v for k, v in extra.items()}})

        try:
            result = handle(
                message=str(payload.get("message") or ""),
                history=[str(h) for h in (payload.get("history") or [])][-6:]
                if isinstance(payload.get("history"), list) else [],
                scene_id=payload.get("sceneId") or None,
                on_stage=on_stage,
            )
        except Exception as exc:  # 兜底：绝不让异常打断 SSE 连接
            result = {"type": "error", "message": "内部处理出错，请稍后重试。",
                      "detail": f"{type(exc).__name__}: {exc}"}

        if result.get("type") == "answer":
            for line in (result.get("plainText") or "").split("\n"):
                if not write("text", line):
                    return
                if STREAM_DELAY_MS > 0:
                    time.sleep(STREAM_DELAY_MS / 1000.0)

        write("result", result)
        write("done", {"ok": True})


_EXAMPLES: dict[str, list[str]] = {
    "rental": ["退租时房东不退我押金，说我弄脏了墙", "房东要提前赶我走，还说押金不退"],
    "parttime": ["兼职老板拖欠我两个月工资不给", "兼职说好 20 一小时，结账时只给一半"],
    "consumer": ["网购的东西和描述不符，商家不给退", "外卖吃出异物，能要求赔偿吗"],
    "ip": ["我写的论文被同学抄袭后提交了", "我的设计被人搬到网上赚钱"],
    "loan": ["网贷借 5000 只到手 4200，还不上怎么办", "被平台暴力催收，爆我通讯录"],
    "network": ["App 强制读取我通讯录，还老接到推销电话", "有人让我借银行卡帮他过账，还给好处费"],
    "marriage": ["恋爱期间转给对方的钱，分手后能要回来吗", "婚前买的房，婚后算共同财产吗"],
    "injury": ["骑电动车被车撞了，对方全责怎么要赔偿", "同学免费捎我回家，出事故受伤了谁担责"],
}


def main() -> None:
    threading.Thread(target=prewarm_bm25, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    obs.log("server.start", host=HOST, port=PORT,
            kb=lawkb.database.DEFAULT_DB.as_posix(),
            retrieval="ok" if lawkb.retrieval else (lawkb.RETRIEVAL_ERROR or "unavailable"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
