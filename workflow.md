# qiqi-forge-swarm · 编排细节（workflow.md）

> 本文是 `SKILL.md` 的展开。所有表、阈值、字段名均与 `scripts/swarmflow.py` 一致；
> 与原件不同之处在"来源与差异"注明。**本团队为严格顺序流水线（非并行组）。**

---


## Overview

```mermaid
graph TD
  IN[调用方输入 query + skill_catalog] --> A[01 decomposer 分解官<br/>硬前置]
  A -->|subtasks[]| B[02 retriever 检索官]
  B -->|assignments[]| C[03 weaver 织网官]
  C -->|dag 检环/补缺| D[04 assembler 装配官]
  D -->|plan 执行计划| E[05 executor 执行官<br/>硬前置：需宿主派发]
  E -->|真实通道| OUT[执行结果]
  E -. 拿不到派发通道 .-> X[needs_executor 交接包<br/>绝不假装已执行]
```

Leader（宿主编排器）按阶段顺序派遣角色；标注「硬前置」的阶段失败则整轮终止；标注「需 LLM」的阶段在无模型能力时降级输出交接包，流程不中断、不编造。

---


## Detailed Steps

| # | 阶段 | 负责角色 | 输入 | 输出 | 验收标准 |
|:--|:--|:--|:--|:--|:--|
| 1 | `01-decompose` 分解 | decomposer | `input.query`、`input.max_subtasks?` | `subtasks[{index,description,dependencies[],is_decomposable}]` + `decomposition_path` | `len(subtasks) ≥ 1`；每项含 `index/description/dependencies`；所有 `dependencies` 元素 `<` 该项 `index` |
| 2 | `02-retrieve` 检索 | retriever | `subtasks[]`、`input.skill_catalog[]` | `assignments[{index,skill_name,skill_score,recommended_skills[]}]` + `sad_iterations` / `sad_improved` | `len(assignments) == len(subtasks)`；`skill_score ∈ [0,1]`；`skill_name` 必须来自 catalog（或空串）；无 catalog → `needs_llm` |
| 3 | `03-weave` 织网 | weaver | `subtasks[]` + `assignments[]` | `dag{node_count,edge_count,nodes[],edges[],execution_order[],is_valid,has_cycles,missing_deps[]}` | `is_valid=true`；无自环；`execution_order` 覆盖全部节点；依赖先于被依赖者出现 |
| 4 | `04-assemble` 装配 | assembler | `dag` | `plan{team_id,name,total_units,units[],execution_order[],stages{}}` | `total_units == node_count`；`stages` 覆盖全部 unit；同阶段内无相互依赖 |
| 5 | `05-execute` 执行 | executor | `plan` | **无**（需宿主）→ `needs_executor` 交接包 | 有真实通道时回填 `unit_results`；无通道时 `status=needs_llm`、`needs=executor`、`outputs` 为空 |

### 来源与差异

| 阶段 | 来源 | 差异（如实说明） |
|:--|:--|:--|
| 01 | `tianheng/decomposer.py` | 逐条复刻模板表、模板匹配（≥2 关键词）、规则分解（连接词优先，其次动词）、SAD 迭代（2 轮）。文件头的注释自称"LLM分解"，但**代码实际不调用任何 LLM**（`self.llm_available = False`），本骨架据实为确定性实现 |
| 02 | `tianheng/{skill_index,retriever}.py` | 原件依赖 `numpy/sklearn(TfidfVectorizer,cosine_similarity)/scipy/jieba` 并**扫描宿主 `skills/` 目录**自建索引。本骨架**不引入三方库、不扫目录**，改由调用方传 `skill_catalog`，用 2-gram + 标签加权近似原件两级检索。**检索精度低于原件** |
| 03 | `tianheng/weaver.py` | 逐条复刻 `VERB_ORDER`（6 级）、`TYPE_PRODUCERS`/`TYPE_CONSUMERS`、`_should_depend`、`_break_cycles`（Kahn + 打破最低动词级别边）、`_validate`（DFS 环检测 + 缺失依赖）、`_topological_sort` |
| 04 | `tianheng/assembler.py` | 复刻 `ExecutionUnit`/`AssembledTeam` 与 `assemble`/`deploy_to_stage`。**剥离 `WanxiangHub`（万象中枢）与 `DispatchCommander`**——它们属于宿主调度层 |
| 05 | `qiqi_swarm.py` + `subagent_orchestrator.py` | **原件不含真实执行**：`_dispatch_batch` 只写 `_active/_queue` 后返回；`SubagentOrchestrator` 只有 `register`/`complete`/`get_abandoned`。本骨架据此**拒绝**产出执行结果，只给出 `needs_executor` 与宿主需求清单 |
| （图级调度） | `core/workflow/`（engine.py / graph.py） | 原框架另有一套节点/边式真 DAG 引擎（`nodes/{trigger,task,skill,model,execute,post}`、`edges/{task,emotion}`）。本团队的 `stages` 只是轻量分阶段；条件边/循环/并行汇聚等请改用该引擎 |

---

## 二、交接协议

### 2.1 交接包通用 schema

```jsonc
{
  "swarm": "qiqi-forge-swarm", "stage": "01-decompose", "stage_name": "分解",
  "role": "decomposer", "role_name": "分解官",
  "prompt_file": "roles/decomposer.md", "depends_on": [],
  "status": "ok",              // ok | needs_llm | needs_executor | needs_input | failed | skipped
  "started_at": "ISO8601", "finished_at": "ISO8601",
  "inputs": {}, "outputs": {},  // status=needs_llm 时 outputs 必须为 {}
  "validation": {"checks": [{"name":"","passed":true,"detail":""}], "passed": true},
  "uncertainty": [], "note": "", "next": null
}
```

### 2.2 各阶段载荷必填字段

| 载荷 | 来源阶段 | 必填字段 | 校验 |
|:--|:--|:--|:--|
| `subtasks[]` | 01 | `index`(int)、`description`(str)、`dependencies`(int[]) | `0 ≤ d < index`（严格小于，禁止自依赖/前向依赖） |
| `assignments[]` | 02 | `index`、`skill_name`、`skill_score`、`recommended_skills[]` | `skill_name ∈ catalog.names ∪ {""}`；`0 ≤ skill_score ≤ 1` |
| `dag` | 03 | `nodes[]`、`edges[]`、`execution_order[]`、`is_valid`、`has_cycles`、`missing_deps[]` | 无自环 `f != t`；`sorted(execution_order) == range(node_count)` |
| `plan` | 04 | `team_id`、`units[]`、`stages{}`、`execution_order[]` | `len(units) == node_count`；`stages` 并集 == 全部 `unit_id`；同阶段内无依赖 |
| **needs_executor** | 05 | `stage`/`role`/`prompt_file`/`input`/`needs="executor"` | `outputs == {}`（不得含任何执行结果） |

### 2.3 `needs_llm` / `needs_executor` 契约

引擎统一用 `status="needs_llm"` 表示"本阶段无法自行完成"，用 `needs` 字段区分具体缺口：

| `needs` 取值 | 触发阶段 | 含义 |
|:--|:--|:--|
| `skill_index_or_catalog` | 02-retrieve | 缺少技能目录（需宿主提供 `skill_catalog`） |
| `executor` | 05-execute | 缺少真实子会话派发能力 |

校验：四字段齐备 + `outputs` 为空。若该阶段 `hard=true`（本团队 01、05），整体置 `blocked` 并停止推进。

---

## 三、失败与降级

### 3.1 重试 / 超时 / 可跳过性

| 阶段 | 重试 | 超时 | 能否跳过 | 说明 |
|:--|:--|:--|:--|:--|
| 01-decompose | 0 | 5s | 否（硬前置） | 缺 `query` 则 failed |
| 02-retrieve | 0 | 10s | 是（降级继续） | 缺 catalog → `needs_llm`，后续在无技能名下继续 |
| 03-weave | 0 | 10s | 是 | 缺 subtasks → `skipped` |
| 04-assemble | 0 | 10s | 是 | 缺 dag → `skipped` |
| 05-execute | 0 | — | 否（硬前置） | 缺 plan 则 failed；无通道 → `needs_executor` |

### 3.2 降级矩阵

| 场景 | 阶段状态 | 整体状态 | 后续行为 | `uncertainty` 标注 |
|:--|:--|:--|:--|:--|
| 缺 `query` | `01 = failed` | `failed` | 停止，退出码 2 | 失败原因 |
| 无模板无信号 | `01 = ok` | 继续 | 单子任务 | "未匹配模板且未识别出分解信号，整体任务被当成单个子任务" |
| 走动词规则分解 | `01 = ok` | 继续 | 继续 | "子任务描述为裸动词（原件既有行为），粒度可能过粗" |
| 缺 `skill_catalog` | `02 = needs_llm` | 继续（非硬前置） | 03/04 在 `skill_name=""` 下继续 | "复用原件索引需 numpy/sklearn/jieba" |
| 子任务无匹配技能 | `02 = ok(score=0)` | 继续 | 继续 | "部分子任务在 catalog 中无匹配（score=0），建议人工补充" |
| DAG 有环/缺依赖 | `03 = ok(is_valid=false)` | 继续 | 04 仍装配 | "DAG 校验未通过，装配阶段可能不可用" |
| 无真实派发通道 | `05 = needs_llm` | **blocked** | 停止，等宿主 | 三条：`_dispatch_batch` 只写状态 / orchestrator 只登记 / 不编造结果 |
| SAD 触发 | `02 = ok(sad_improved=true)` | 继续 | 粒度被修正 | 记录 `sad_iterations` |

### 3.3 关于"整体 blocked"的说明

阶段 5 标为**硬前置**：它没有后继阶段，但它代表**本次编排的唯一交付物（执行结果）**。缺少真实派发通道时流程并非"部分完成"，而是"无法交付"，因此记 `blocked`（退出码 2）而非 `partial`。此时 01–04 的计划产物**仍然完整保留**在 run 目录，可直接交给宿主使用。

### 3.4 不确定性标注约定

- 机器可读：`uncertainty[]` 非空 + `validation.passed` 严格按 checks 计算。
- **禁止**在无 catalog 时编造技能名；**禁止**在无执行通道时填充 `unit_results`。
- 检索精度差异必须在 `uncertainty` 中声明。

---

## 四、终止条件

| 终止类型 | 判定 |
|:--|:--|
| **正常完成** | 5 阶段全部 `ok`（需宿主已回填执行结果）→ `completed`，退出码 0。**本骨架单独运行时不会出现** |
| **部分完成** | 非硬前置阶段 `needs_llm`（如缺 catalog）→ `partial`，退出码 0 |
| **阻塞** | 硬前置阶段出现缺口：01 缺 `query`（实际先 failed），或 05 无派发通道 → `blocked`，退出码 2 |
| **失败** | 任何阶段 `failed` → `failed`，退出码 2 |
| **需人工介入** | ① `blocked` 且宿主尚未接入执行通道；② `dag.is_valid=false`（计划不可用，需人工改分解/技能）；③ 分解走 `fallback:single`（等于没拆，需人工补充粒度） |

---

## 五、产物落盘

```
<workspace>/data/qiqi-forge-swarm/<run_id>/
├── 01-decompose.json   ← 子任务列表
├── 02-retrieve.json    ← 技能分配（含 SAD 迭代信息）
├── 03-weave.json       ← DAG（节点/边/执行序/有效性）
├── 04-assemble.json    ← 执行计划（units/stages）
├── 05-execute.json     ← needs_executor 交接包（outputs 为空）
└── run.json            ← 运行清单（overall_status / blocked_stage / needs / stages[]）
```

- `<workspace>`：`--workspace` > `QIQI_WORKSPACE` > 当前目录。
- `<run_id>`：缺省 `<UTC时间戳>-<输入哈希前8位>`，可用 `--run-id` 覆盖。
- 落盘时机：每阶段执行完立即落盘。
- 幂等性：同一 `run_id` 覆盖写；不同 `run_id` 完全隔离。
- **不写**宿主状态文件：`data/qiqi_swarm_state.json`、`data/subagent_orchestrator.json`、`core/tianheng/skill_index_data/`（原件位置，已改道）。**也不产出**执行结果文件（属宿主）。


## Acceptance Criteria

- 每个确定性阶段产物齐备且自检通过（run.json 中该阶段 status=done）；
- 标注「硬前置」的阶段无 skipped；
- 所有 needs_llm 阶段均输出规范交接包（outputs 为空 + needs 字段），全文无编造内容；
- 最终报告含每个阶段的输入/输出/验收结果与失败原因（如有）。


## Overview

```mermaid
graph TD
  IN[调用方输入 query + skill_catalog] --> A[01 decomposer 分解官<br/>硬前置]
  A -->|subtasks[]| B[02 retriever 检索官]
  B -->|assignments[]| C[03 weaver 织网官]
  C -->|dag 检环/补缺| D[04 assembler 装配官]
  D -->|plan 执行计划| E[05 executor 执行官<br/>硬前置：需宿主派发]
  E -->|真实通道| OUT[执行结果]
  E -. 拿不到派发通道 .-> X[needs_executor 交接包<br/>绝不假装已执行]
```

Leader（宿主编排器）按阶段顺序派遣角色；标注「硬前置」的阶段失败则整轮终止；标注「需 LLM」的阶段在无模型能力时降级输出交接包，流程不中断、不编造。

---

