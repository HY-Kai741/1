# plan-06 · 核心一：工具（Tools）

| 项 | 值 |
|---|---|
| 对应 PRD | §8.1 工具 |
| 阶段 | Phase 5 |
| 优先级 | P0 |
| 依赖 | plan-02、plan-03 |
| 规模档 | 中 |

---

## 1. 目标

建立**统一的工具注册表**，让每个 Agent 只能通过声明式工具访问知识与能力，杜绝绕过证据层的直连。

**一句话验收：任何 Agent 都不允许直连数据库或文件；所有访问必须走工具，且工具调用可审计。**

---

## 2. 工具清单

### 2.1 知识类（只读）

| 工具 | 输入 | 输出 | 现状 |
|---|---|---|---|
| `law.search` | 自然语言 query | 法条列表（含原文、条号、来源） | [实证] 已有（映射 + BM25） |
| `law.get` | 法律名 + 条号 / article_id | 单条法条原文 | [实证] 已有 |
| `law.verify` | 法条引用列表 | 逐条核验结果 | [实证] 已有 |
| `law.resolve` | 法律简称 | 库内全名 | [实证] 已有 |
| `kp.tree` | 部门法 / 路径 | 知识点树 | [计划] |
| `kp.get` | kp_id | 知识点详情 + 关联法条 | [计划] |
| `kp.search` | 关键词 | 相关知识点 | [计划] |
| `q.search` | 知识点 + 难度 + 题型 | 题目列表 | [计划] |
| `q.get` | question_id | 题目详情 | [计划] |
| `case.get` | case_id | 教学案例 | [计划] |

### 2.2 测评类

| 工具 | 输入 | 输出 |
|---|---|---|
| `grade.objective` | 题目 + 用户答案 | 对错 + 解析 |
| `grade.subjective` | 题目 + 用户答案 + 采分点 | 初评明细 |
| `paper.build` | 知识点集 + 题量 + 难度 | 题组 + 组卷理由 |
| `diagnose.run` | 错题集 | 归因结果 |
| `mastery.compute` | 答题记录 | 掌握度向量 |
| `path.plan` | 掌握度 + 目标 | 学习序列 |

### 2.3 横切类

| 工具 | 说明 |
|---|---|
| `evidence.check` | **强制前置**：任何含法条引用的输出都必须过此工具 |
| `obs.trace` | trace / span / 指标 |
| `obs.redact` | PII 脱敏 |

---

## 3. 工具契约

每个工具必须声明：

```python
{
  "name": "law.get",
  "kind": "read_only",          # read_only | compute | side_effect
  "input_schema": {...},
  "output_schema": {...},
  "deterministic": True,        # 是否同输入恒定输出
  "uses_llm": False,
  "latency_budget_ms": 50,
  "errors": ["not_found", "invalid_input"],
  "audit": True
}
```

### 3.1 硬约束

| 编号 | 约束 |
|---|---|
| T-1 | 工具必须**无状态**（状态由调用方持有） |
| T-2 | 工具必须**声明是否用模型**；用模型的必须可关闭 |
| T-3 | 工具必须**声明确定性**；`deterministic=false` 的工具不得参与判定链 |
| T-4 | 知识类工具**全部只读**，禁止写 |
| T-5 | 任何有副作用的工具必须显式登记并记审计日志 |
| T-6 | 工具失败必须返回结构化错误，**不得抛裸异常** |

---

## 4. 调用审计

每次工具调用记录：

```json
{"ts": 0.0, "trace_id": "edu_x", "agent": "subject.civil", "tool": "law.get",
 "args_hash": "...", "ok": true, "ms": 3.1, "evidence": true}
```

**审计用途：** 事后可回答"这个结论是哪个 Agent 用哪次工具调用得出的"。这是教学产品可信度的基础。

---

## 5. 任务分解

| ID | 任务 | 产出 |
|---|---|---|
| T6.1 | 设计工具注册表 | `legal_agent/tools/registry.py` |
| T6.2 | 包装现有知识工具 | `tools/knowledge.py` |
| T6.3 | 实现知识点/题库工具 | `tools/kp.py`、`tools/question.py` |
| T6.4 | 实现测评工具 | `tools/assessment.py` |
| T6.5 | 实现证据工具（强制前置） | `tools/evidence.py` |
| T6.6 | 工具调用审计 | `tools/audit.py` |
| T6.7 | 禁止直连检查器 | `scripts/check_no_direct_access.py` |
| T6.8 | 工具层单测 | `tests/test_tools.py` |

---

## 6. 验收标准

| 项 | 标准 | 可验证 |
|---|---|---|
| A1 | 所有 Agent 经工具访问知识 | 直连数 = 0（静态检查器清零） |
| A2 | 工具契约齐全 | 缺契约的工具数 = 0 |
| A3 | 含法条引用的输出全部过 `evidence.check` | 绕过数 = 0 |
| A4 | 工具调用可审计 | 每次调用均有日志 |
| A5 | 工具失败不抛裸异常 | 用例 100% 通过 |

---

## 7. 依赖与风险

| 风险 | 等级 | 应对 |
|---|---|---|
| 工具层变成薄包装、无实际约束力 | 中 | 用静态检查器强制"禁止直连"，而不是靠约定 |
| 工具粒度过细导致调用爆炸 | 中 | 以"一次业务动作"为粒度，不做 CRUD 级拆分 |
