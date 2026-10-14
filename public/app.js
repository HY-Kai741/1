/* 大学生法律常识问答助手 —— 前端
   优先走 /api/ask/stream（SSE，逐行呈现），不可用时自动回退 /api/ask（整包 JSON）。
   「需要补充的信息」里的每条问题都可一键填入输入框，实现多轮补全。 */

const $ = (id) => document.getElementById(id);
const feed = $("feed");
// sceneId 只是「点过快选按钮」后的一次性领域提示，发送后即清空；
// 绝不在收到回答后回写并长期保留——否则后续问题会被强行归到同一个领域，回答完全一样。
const state = { history: [], busy: false, sceneId: null };

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
));

/* ---------------- 初始化 ---------------- */

async function boot() {
  try {
    const h = await (await fetch("/api/health")).json();
    const kb = h.knowledgeBase || {};
    $("kbBadge").textContent = kb.ok
      ? `本地法条库 · ${kb.laws} 部法律 / ${kb.articles.toLocaleString()} 条`
      : "法条库不可用";
  } catch { $("kbBadge").textContent = "法条库状态未知"; }

  try {
    const { scenes } = await (await fetch("/api/scenes")).json();
    const box = $("sceneList");
    box.innerHTML = "";
    scenes.forEach((s) => {
      const b = document.createElement("button");
      b.className = "scene-btn";
      b.type = "button";
      b.innerHTML = `<span class="n">${esc(s.name)}</span><span class="s">${esc(s.summary)}</span>`;
      b.addEventListener("click", () => {
        state.sceneId = s.id;
        $("query").value = (s.examples && s.examples[0]) || "";
        $("query").focus();
        $("statusHint").textContent = `已选定领域：${s.name}（可直接发送或改写，仅对本次生效）`;
      });
      box.appendChild(b);
    });
  } catch { /* 场景列表加载失败不影响手输 */ }
}

/* ---------------- 渲染：基础块 ---------------- */

function renderUser(text) {
  const d = document.createElement("div");
  d.className = "turn user";
  d.innerHTML = `<div class="bubble-user">${esc(text)}</div>`;
  feed.appendChild(d);
}

function scrollEnd() {
  feed.lastElementChild?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderStageCard() {
  const d = document.createElement("div");
  d.className = "turn";
  d.id = "liveTurn";
  d.innerHTML = `<div class="card">
      <div class="stages" id="liveStages"></div>
      <div class="loading" id="liveLoading"><span class="dot"></span><span class="dot"></span><span class="dot"></span><span>正在检索法条并核对…</span></div>
      <pre class="stream-text" id="liveText"></pre>
    </div>`;
  feed.appendChild(d);
  return d;
}

const STAGE_LABEL = {
  guard: "输入检查", scene: "场景识别", slots: "关键信息核对",
  statutes: "法条检索与核验", explain: "通俗解释", facts: "事实核对",
  advice: "行动建议", present: "整理输出",
};

function pushStage(name) {
  const box = $("liveStages");
  if (!box || !STAGE_LABEL[name]) return;
  if ([...box.children].some((c) => c.dataset.s === name)) return;
  const p = document.createElement("span");
  p.className = "stage-pill";
  p.dataset.s = name;
  p.textContent = STAGE_LABEL[name];
  box.appendChild(p);
}

/* ---------------- 渲染：结果卡片 ---------------- */

function renderAnswer(result) {
  const d = document.createElement("div");
  d.className = "turn";
  const sc = result.scene || {};
  const chip = result.mode === "fallback"
    ? `<span class="chip gray">未在专项范围内 · 仅供参考</span>`
    : `<span class="chip">${esc(sc.name || "")}</span>`;

  const statutes = (result.statutes || []).map((s) => `
    <div class="statute">
      <span class="src">${esc(s.source)}</span>
      <div><span class="cite">《${esc(s.law_name)}》${esc(s.article_no)}</span><span class="topic">${esc(s.topic || "")}</span></div>
      <blockquote>${esc(s.text)}</blockquote>
      <div class="plain"><b>人话：</b>${esc(s.explain || "（本条未预置通俗翻译，请以原文为准）")}</div>
    </div>`).join("") || `<p class="muted">未找到直接相关的法条。</p>`;

  const steps = (result.actions || []).map((a) => `
    <li><span class="tag ${a.stage}">${esc(a.label)}</span><span>${esc(a.text)}</span></li>`).join("");

  const missing = (result.missing || []).length
    ? (result.missing || []).map((m, i) => `
      <div class="ask-item">
        <button class="use-btn" data-fill="${i}" type="button">补充</button>
        <div><div class="q">${esc(m.question)}</div>${m.example ? `<div class="ex">${esc(m.example)}</div>` : ""}</div>
      </div>`).join("")
    : `<p class="muted">无，你提供的信息已足够给出上面的分析与路径。</p>`;

  const notes = (result.notes || []).map((n) => `<li>${esc(n)}</li>`).join("");

  const facts = (result.facts && result.facts.points) || [];
  const factsHtml = facts.length
    ? `<div class="facts"><span class="facts-title">本轮识别到的关键事实</span>${
        facts.map((f) => `<span class="fact-chip">${esc(f)}</span>`).join("")
      }</div>`
    : "";

  d.innerHTML = `<div class="card">
      <div class="conclusion">${chip}${esc(result.conclusion)}</div>
      ${factsHtml}
      <h3>适用法条（${(result.statutes || []).length} 条，原文逐字引用）</h3>
      ${statutes}
      <h3>行动建议</h3>
      ${steps ? `<ul class="steps">${steps}</ul>` : `<p class="muted">无</p>`}
      <h3>需要补充的信息</h3>
      <div id="askBox">${missing}</div>
      <div class="disclaimer">${esc(result.disclaimer)}</div>
      <h3>数据说明</h3>
      <ul class="notes">${notes}</ul>
      <div class="trace">${esc(sc.name || "")} · trace: ${esc(result.traceId || "")} · 耗时 ${esc(result.timings?.total ?? "-")} ms</div>
    </div>`;

  feed.appendChild(d);

  d.querySelectorAll("[data-fill]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const m = result.missing[Number(btn.dataset.fill)];
      const inp = $("query");
      inp.placeholder = m.question;
      inp.focus();
      $("statusHint").textContent = "请按提示补充这条信息，再点发送。";
    });
  });
}

function renderBlocked(result) {
  const d = document.createElement("div");
  d.className = "turn";
  d.innerHTML = `<div class="card"><span class="badge no">${esc(result.label || "已拦截")}</span>
    <div style="margin-top:10px;">${esc(result.message)}</div>
    ${result.traceId ? `<div class="trace">trace: ${esc(result.traceId)}</div>` : ""}</div>`;
  feed.appendChild(d);
}

function renderPrompt(result) {
  const chips = (result.domains || []).map((x) => `<button class="scene-btn" type="button" data-ex="${esc(x.example)}">
      <span class="n">${esc(x.name)}</span><span class="s">${esc(x.example)}</span></button>`).join("");
  const d = document.createElement("div");
  d.className = "turn";
  d.innerHTML = `<div class="card"><div class="conclusion">${esc(result.message)}</div>
    <h3>我可以帮你的五类问题</h3><div class="scenes" style="flex-direction:column;">${chips}</div>
    <p class="muted" style="margin-top:10px;">${esc(result.hint || "")}</p></div>`;
  feed.appendChild(d);
  d.querySelectorAll("[data-ex]").forEach((b) => b.addEventListener("click", () => {
    $("query").value = b.dataset.ex; $("query").focus();
  }));
}

function renderError(result) {
  const d = document.createElement("div");
  d.className = "turn";
  d.innerHTML = `<div class="card"><span class="badge no">处理失败</span>
    <div style="margin-top:10px;">${esc(result.message || "未知错误")}</div>
    ${result.detail ? `<div class="trace">${esc(result.detail)}</div>` : ""}
    ${result.traceId ? `<div class="trace">trace: ${esc(result.traceId)}</div>` : ""}</div>`;
  feed.appendChild(d);
}

/* ---------------- 发送（流式 + 回退） ---------------- */

async function send(text) {
  if (state.busy) return;
  const message = (text ?? $("query").value ?? "").trim();
  if (!message) { $("statusHint").textContent = "请输入你的情况"; return; }

  state.busy = true;
  $("sendBtn").disabled = true;
  $("statusHint").textContent = "";
  renderUser(message);
  $("query").value = "";
  renderStageCard();

  const body = JSON.stringify({ message, history: state.history.slice(-6), sceneId: state.sceneId });
  // 领域提示是一次性的：随本次请求发出后立即清空，避免粘住后续所有问题。
  state.sceneId = null;

  let result = null;
  try {
    result = await streamAsk(body);
  } catch {
    result = null;
  }
  if (!result) {
    try {
      const resp = await fetch("/api/ask", {
        method: "POST", headers: { "Content-Type": "application/json" }, body,
      });
      result = await resp.json();
    } catch (err) {
      result = { type: "error", message: "请求失败", detail: err.message };
    }
  }

  $("liveTurn")?.remove();
  state.history.push(message);

  if (result.type === "answer") renderAnswer(result);
  else if (result.type === "blocked") renderBlocked(result);
  else if (result.type === "prompt") renderPrompt(result);
  else renderError(result);

  state.busy = false;
  $("sendBtn").disabled = false;
  scrollEnd();
}

async function streamAsk(body) {
  const resp = await fetch("/api/ask/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body,
  });
  if (!resp.ok || !resp.body) return null;

  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  let result = null;
  let textLines = [];

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const block = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const em = /^event: (.+)$/m.exec(block);
      const dm = /^data: (.*)$/m.exec(block);
      if (!em || !dm) continue;
      const ev = em[1].trim();
      let data = dm[1];
      try { data = JSON.parse(data); } catch { /* 保持字符串 */ }

      if (ev === "stage") {
        pushStage(data.stage);
      } else if (ev === "text") {
        $("liveLoading")?.remove();
        textLines.push(data);
        const pre = $("liveText");
        if (pre) pre.textContent = textLines.join("\n");
      } else if (ev === "result") {
        result = data;
      }
    }
  }
  return result;
}

/* ---------------- 事件绑定 ---------------- */

$("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  send();
});

$("query").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    send();
  }
});

$("query").addEventListener("input", (e) => {
  e.target.style.height = "auto";
  e.target.style.height = Math.min(e.target.scrollHeight, 160) + "px";
});

boot();
