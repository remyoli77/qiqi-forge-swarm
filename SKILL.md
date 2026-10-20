---
name: qiqi-forge-swarm
version: 1.0.0
kind: swarm-skill
roles:
  - id: decomposer
    kind: ai_agent
    purpose: 只负责把大任务拆成带依赖关系的子任务列表，粒度以可被技能检索命中为准
    tools:
      - python3
  - id: retriever
    kind: ai_agent
    purpose: 只负责把子任务匹配到技能目录并打分，输出 assignments，不做编排
    tools:
      - python3
  - id: weaver
    kind: ai_agent
    purpose: 只负责把任务与技能匹配织成 DAG（检环、缺失依赖、拓扑排序），不挑技能
    tools:
      - python3
  - id: assembler
    kind: ai_agent
    purpose: 只负责把 DAG 装配成 stages 执行计划，不改拓扑
    tools:
      - python3
  - id: executor
    kind: ai_agent
    purpose: 只负责按计划真实派发执行；拿不到宿主真实派发通道时输出 needs_executor，绝不假装已执行
    tools:
      - python3
description: |
  技能组建编排团队（5 角色）：把一个复杂任务拆成子任务，检索可用技能，织成 DAG，装配成执行计划，再交给宿主派发执行。
  当用户需要「把大任务拆成步骤」「为任务找合适的技能/工具」「编排一条可执行流水线」「生成执行计划」时使用。
  触发词：任务分解、技能编排、DAG 编排、执行计划、五轮管道、forge、组队。
---

# qiqi-forge-swarm - 技能组建编排团队

## 💕 团队定位

"把一个大任务做掉"这件事，一个角色做会糊成一团：**分解粒度没人校正**（拆太粗检索不到技能，拆太细上下文丢失）、**检索和编排混在一起**（一边找工具一边连边，改一处崩一片）、以及最危险的——**假装执行了**。原框架 `core/qiqi_swarm.py` 正是这种"大脑"：它算复杂度、选协作模式（isolated/debate/team/fusion）、有熔断器，但 `_dispatch_batch()` **只往 active/queue 里写状态，并不真正派发子会话**；`core/subagent_orchestrator.py` 也只做并发登记（`register`/`complete`），从不 `spawn`。

所以本团队把"想清楚"和"做掉"严格分开：**分解官 → 检索官 → 织网官 → 装配官** 只负责产出一份可核对的执行计划（全是确定性算法），**执行官** 必须由宿主提供真实派发通道，拿不到就输出 `needs_executor` 交接包——**绝不假装已执行**。

## Roles

| id | 角色 | 何时派遣 | 输入 | 输出 | 提示词文件 |
|:--|:--|:--|:--|:--|:--|
| decomposer | 分解官 | 阶段1（硬前置） | `input.query`、`input.max_subtasks?` | `subtasks[{index,description,dependencies[]}]` | roles/decomposer.md |
| retriever | 检索官 | 阶段2 | `subtasks[]` + `input.skill_catalog[]` | `assignments[{index,skill_name,skill_score,recommended_skills[]}]` | roles/retriever.md |
| weaver | 织网官 | 阶段3 | `subtasks[]` + `assignments[]` | `dag{nodes[],edges[],execution_order[],is_valid,has_cycles,missing_deps[]}` | roles/weaver.md |
| assembler | 装配官 | 阶段4 | `dag` | `plan{team_id,units[],stages{},execution_order[]}` | roles/assembler.md |
| executor | 执行官 | 阶段5（硬前置） | `plan` | **需要宿主真实派发**；否则 `needs_executor` | roles/executor.md |

## Workflow

```
[01-decompose 分解官]   ← 硬前置；模板匹配 / 连接词 / 动词规则
        │ subtasks[]（带 dependencies）
        ▼
[02-retrieve 检索官]    ← 需要 input.skill_catalog；纯 Python 近似两级检索 + SAD 迭代对齐
        │ assignments[]（每子任务 top-k 技能）
        ▼
[03-weave 织网官]       ← 依赖推断 → 环消除 → 校验 → 拓扑排序
        │ dag（节点/边/执行序/有效性）
        ▼
[04-assemble 装配官]    ← 装配执行单元 → 按依赖深度分阶段
        │ plan（units[]/stages{}）
        ▼
[05-execute 执行官]     ← 硬前置：需要宿主真实派发通道
        │ 无通道 → needs_executor 交接包，整体 blocked
        ▼
   宿主回填 unit_results 后收尾
```

- **阶段1 分解**：验收——`subtasks` 非空，每项含 `index/description/dependencies`，且依赖索引 `<` 自身索引。
- **阶段2 检索**：验收——每个子任务都拿到 top-k 推荐；技能名必须来自 `catalog`；无 `catalog` 时输出 `needs_llm`（`needs=skill_index_or_catalog`）。
- **阶段3 织网**：验收——`is_valid=true`、无环、无自环、`execution_order` 覆盖全部节点、依赖先于被依赖者。
- **阶段4 装配**：验收——`units` 数量 == 节点数；`stages` 覆盖全部 unit；同阶段内无相互依赖。
- **阶段5 执行**：验收——有真实通道则回填每单元结果；无通道必须 `needs_executor` 且 `outputs` 为空。

## 📁 文件结构

```
qiqi-forge-swarm/
├── SKILL.md                    ← 本文件
├── workflow.md                 ← 编排细节：阶段/契约/失败降级/终止条件
├── roles/
│   ├── decomposer.md           ← 分解官：任务 → 原子子任务
│   ├── retriever.md            ← 检索官：子任务 → 技能推荐
│   ├── weaver.md               ← 织网官：子任务+技能 → DAG
│   ├── assembler.md            ← 装配官：DAG → 执行计划/团队
│   └── executor.md             ← 执行官：计划 → 真实执行（需宿主）
├── scripts/swarmflow.py        ← 可执行编排骨架（仅标准库）
└── samples/example-input.json  ← 示例输入（⚠️ 虚构，仅用于 selftest）
```

## 🔧 使用方式

```bash
# 1) 查看 DAG 与各阶段角色/输入/输出
python3 scripts/swarmflow.py plan

# 2) 编排一个任务
QIQI_WORKSPACE=/path/to/ws python3 scripts/swarmflow.py run --input my-task.json --run-id f20260916

# 3) 查看状态（默认纯 JSON，可被 json.loads 解析；加 --format text 才附 DAG/装配人类可读摘要）
QIQI_WORKSPACE=/path/to/ws python3 scripts/swarmflow.py status --run-id f20260916
QIQI_WORKSPACE=/path/to/ws python3 scripts/swarmflow.py status --run-id f20260916 --format text

# 4) 自检
python3 scripts/swarmflow.py selftest
```

输入 JSON 形如：

```json
{
  "query": "分析这份研究报告，然后总结成日报",
  "max_subtasks": 6,
  "skill_catalog": [
    {"name": "web-search", "description": "联网搜索网页与新闻", "tags": ["搜索", "新闻"], "category": "search"}
  ]
}
```

工作区解析顺序：`--workspace` > `QIQI_WORKSPACE` > 当前目录。脚本内**无任何硬编码路径**。

## 📜 交接协议

关键阶段间载荷：

```jsonc
// 01 → 02/03
"subtasks": [ {"index": 0, "description": "收集相关信息", "dependencies": []} ]

// 02 → 03/04
"assignments": [ {"index": 0, "skill_name": "web-search", "skill_score": 0.33,
                  "recommended_skills": [{"name": "", "score": 0.0, "category": "", "tags": []}]} ]

// 03 → 04
"dag": {"node_count": 3, "edge_count": 3,
        "nodes": [ {"index": 0, "description": "", "skill_name": "", "skill_score": 0.0,
                    "depends_on": [], "depended_by": [1]} ],
        "edges": [[0, 1]], "execution_order": [0, 1, 2],
        "is_valid": true, "has_cycles": false, "missing_deps": []}

// 04 → 05
"plan": {"team_id": "team_xxx", "name": "五轮驱动·…", "total_units": 3,
         "units": [ {"unit_id": "unit_xxx", "index": 0, "description": "", "skill_name": "",
                     "skill_score": 0.0, "depends_on": [], "depended_by": [1],
                     "execution_order": 0, "status": "pending"} ],
         "execution_order": [0, 1, 2], "stages": {"0": ["unit_xxx"], "1": ["…"], "2": ["…"]}}
```

**needs_executor 契约**（阶段 5 强制）：

```jsonc
{
  "status": "needs_llm",            // 引擎统一状态名；用 "needs" 字段区分具体缺口
  "stage": "05-execute", "role": "executor", "prompt_file": "roles/executor.md",
  "input": {
    "team_id": "…", "execution_order": [0, 1, 2], "stages": {…}, "units": [ … ],
    "expected_result_schema": {"unit_results": [{"unit_id": "", "status": "done|failed", "output": "", "duration_ms": 0}]},
    "host_requirements": {"spawn": "…", "concurrency": "…", "circuit_breaker": "…"}
  },
  "outputs": {},                     // ★ 必须为空 —— 不得编造执行结果
  "needs": "executor"
}
```

通用交接包 schema 与 `_valid_packet()` 校验规则见 `workflow.md` 第二节。

## 🛡️ 失败与降级

| 情况 | 行为 |
|:--|:--|
| 缺 `input.query` | 01-decompose `failed`（硬前置）→ 整体 `failed`，退出码 2 |
| 未匹配模板且无分解信号 | 01 把整任务当单子任务，并在 `uncertainty` 标注"粒度可能过粗" |
| 缺 `input.skill_catalog` | 02-retrieve 输出 `needs_llm`（`needs=skill_index_or_catalog`，**非硬前置**）→ 流程继续；后续织网/装配在 `skill_name=""` 下仍可产出计划 |
| 子任务在 catalog 中无匹配 | `skill_score=0.0` 并列入 `uncertainty`（不得编造技能名） |
| 子任务依赖互指成环 | 03-weave **断环降级**（打破动词级别最低的边），记录 `dag.broken_edges` 并在 `uncertainty` 标注；**绝不崩溃** |
| 无真实派发通道 | 05-execute 输出 `needs_executor`（**硬前置**）→ 整体 `blocked`，退出码 2 |
| DAG 校验失败（有环/缺依赖） | 03 `ok` 但 `is_valid=false`，`uncertainty` 非空；04 仍可装配但标注风险 |
| 输入非法 JSON / 字段类型错误 / run 目录不可写 | 一律 **stdout 输出 `{"error":...}` 且退出码 2**（与"文件不存在"一致），不抛栈 |

**降级后如何标注不确定性**：`uncertainty[]` 非空；`status=ok` 但 `validation.passed=false` 时不得当成功处理；执行结果**绝不由本团队填充**。

## 🔥 五行归属

| 角色 | 五行 | 依据 |
|:--|:--|:--|
| 分解官 | 🌳 木行 | 原件 `tianheng/decomposer.py` 标识"🌳 天衡·解意"（生长、拆分、生发） |
| 检索官 | ⚔️ 金行 | 原件 `tianheng/retriever.py` 标识"⚔️ 天衡·觅真"（甄别、约束、筛选） |
| 织网官 | 🏔️ 土行 | 原件 `weaver.py` 标识"🏔️ 天衡·连脉"（连接、承托、结构） |
| 装配官 | 🔥 火行 | 原件 `assembler.py` 标识"🔥 天衡·成阵"（聚合、显形、成阵） |
| 执行官 | 🌊 水行 | 执行、流动、落地 |
| 团队整体 | 🏔️ 土行 | 原件 `qiqi_swarm.py` 自注"🏔️ 土行（协调中枢）" |

## 🔗 与框架的对应关系

| 本团队角色 | 来源 core 模块 | 说明 |
|:--|:--|:--|
| decomposer | `core/tianheng/decomposer.py`（363 行） | 复刻 `DECOMPOSITION_TEMPLATES`（7 套）、`_match_template`（≥2 命中）、`_rule_based_decompose`（连接词/动词）、SAD 迭代（2 轮、`<0.05` 修正、`avg<0.1` 触发） |
| retriever | `core/tianheng/skill_index.py`（457 行）+ `retriever.py` | 原件用 `numpy/sklearn(TfidfVectorizer)/scipy/jieba`；本骨架**不引入三方库**，改为纯 Python 2-gram + 标签加权打分（如实标注差异） |
| weaver | `core/tianheng/weaver.py`（446 行） | 复刻 `VERB_ORDER`（6 级）、`TYPE_PRODUCERS/TYPE_CONSUMERS`、`_should_depend`（动词序/类型匹配/名词重叠≥2）、Kahn 环消除、拓扑排序、环与缺失依赖校验 |
| assembler | `core/tianheng/assembler.py`（321 行） | 复刻 `ExecutionUnit` / `AssembledTeam` / `assemble` / `deploy_to_stage`（按依赖深度分阶段）；**剥离 `WanxiangHub`/`DispatchCommander`**（属宿主调度，迁移时外置） |
| （管道形态） | `core/tianheng/pipeline.py`（160 行） | 复刻 `FiveWheelsPipeline` 串联形态：解意→觅真→连脉→成阵 |
| （模式/路由参考） | `core/qiqi_swarm.py`（410 行） | 参考其 4 种协作模式（isolated/debate/team/fusion）、复杂度路由（`TASK_PATTERNS`+超时档位）与断路器（阈值 3、冷却 300s）。**明确：它是编排大脑而非执行器，`_dispatch_batch` 只写状态不派发** |
| （并发控制参考） | `core/subagent_orchestrator.py`（222 行） | 参考其 `max_concurrent`（默认 2）、`register/complete/get_abandoned`（300s 判丢失）。**明确：它只做并发登记与健康检查，不负责真实 spawn** |
| （真 DAG 引擎参考） | `core/workflow/`（engine.py 363、graph.py 208） | 原件另有一套节点/边式工作流引擎（nodes: trigger/task/skill/model/execute/post；edges: task/emotion）。本团队的 `stages` 分阶段是**轻量等价物**；若要图级调度（条件边、循环、并行汇聚），应改用 `core/workflow/` 那套引擎 |

## ⚠️ 迁移注意

1. **执行官绝不假装执行**（最高优先级）。原 `qiqi_swarm.py::_dispatch_batch` 只把 worker 写进 `self._active`/`self._queue` 就返回 `success=any(spawned)`，**从未真正创建子会话**；`subagent_orchestrator.py` 仅 `register`/`complete` 登记。迁移时**不得**把"已入队"当成"已执行"。
2. **三方依赖已剥离**：原件 `skill_index.py` 顶层 `import numpy/sklearn/scipy/jieba`。本骨架改纯 Python 近似检索，检索精度**低于**原件，已在 `uncertainty` 与 `workflow.md` 标注。
3. **路径参数化**：原件 `skill_index.py` 用 `WORKSPACE = dirname(dirname(dirname(abspath(__file__))))` 定位 `skills/`，`qiqi_swarm.py` 状态写 `WORKSPACE/data/`。本骨架统一走 `--workspace` / `QIQI_WORKSPACE`，数据只落 `<workspace>/data/qiqi-forge-swarm/`。
4. **状态目录隔离**：不写 `data/qiqi_swarm_state.json`、不写 `data/subagent_orchestrator.json`、不写 `core/tianheng/skill_index_data/`。
5. **宿主要补的能力**：真实子会话派发（spawn）、并发上限、熔断、执行结果回收。
6. **引入前先过 `skill-scope` 内容安全校验**。

## 📝 更新日志

- 2026-09-16：由 core 模块改造为 Swarm Skill（5 角色 + 可执行编排骨架）。
- 2026-09-16（修复轮）：① A1 修复织网官破环逻辑——`edges` 为二元组 `(i,j)`，原 `_,bi,bj=edges[0]` 会 `ValueError`
  崩溃；改为断环 + 记录 `dag.broken_edges`，真实输入不再崩、可产出完整 run 目录；② B1 输入层错误统一
  stdout JSON + rc=2；③ B2 `status` 默认纯 JSON，人类可读内容走 `--format text`；④ selftest 增补 B1/B2 回归。

## 前置条件（Precondition）

- Python ≥ 3.12（仅标准库，**无 numpy/sklearn/scipy/jieba 依赖**）。
- 调用方必须提供 `input.query`。
- 检索官需要 `input.skill_catalog[]`（否则该阶段 `needs_llm`，后续仍可在无技能名的情况下产出计划）。
- 执行官需要宿主提供**真实子会话/子 agent 派发通道**；否则流程始终 `blocked` 于阶段 5。
- 若仅供框架内部调用，工作区应通过 `--workspace` 或 `QIQI_WORKSPACE` 显式指定。

## 后置效果（Effect）

- `eff_add`：
  - `<workspace>/data/qiqi-forge-swarm/<run_id>/01-decompose.json`
  - `<workspace>/data/qiqi-forge-swarm/<run_id>/02-retrieve.json`
  - `<workspace>/data/qiqi-forge-swarm/<run_id>/03-weave.json`
  - `<workspace>/data/qiqi-forge-swarm/<run_id>/04-assemble.json`
  - `<workspace>/data/qiqi-forge-swarm/<run_id>/05-execute.json`（`needs_executor`，`outputs` 为空）
  - `<workspace>/data/qiqi-forge-swarm/<run_id>/run.json`（含 `overall_status` / `blocked_stage` / `needs`）
  - 逐步向 stdout 打印 JSON 运行清单
- `eff_event`：`swarm.plan.ready`（阶段 1–4 完成后发出）。
  说明：本骨架**不主动发事件**（原件的 `linkage_bus`、`WanxiangHub.dispatch` 属宿主调度层，迁移时剥离）；宿主应在读到 `run.json.needs == "executor"` 后自行派发。
- **不产生**任何执行结果文件——那属于宿主。

## 副作用说明

- **写文件**：仅在 `<workspace>/data/qiqi-forge-swarm/<run_id>/` 下创建文件。
- **读文件**：只读 `--input` 指定的输入 JSON。**不扫描宿主的 `skills/` 目录**（原件 `skill_index.py` 会扫；本骨架改由调用方传 catalog）。
- **网络 / LLM**：**不发起网络请求，不调用 LLM**（纯确定性算法）。
- **不执行子任务**：本团队只产出计划；任何真实执行都必须由宿主完成。
- **幂等性**：相同 `--run-id` 覆盖写；不同 `run_id` 完全隔离。
- **退出码**：`0`=completed/partial；`2`=blocked/failed 或输入层错误（非法 JSON/字段类型错误/run 目录不可写/文件缺失）。输入层错误一律 **stdout 输出 `{"error":...}`**，不抛栈。
- **`status` 输出**：默认只输出 `run.json` 原样 JSON（可直接 `json.loads`）；人类可读的 DAG/装配摘要仅在 `--format text` 时追加。


## Files

| 文件 | 内容 |
|:--|:--|
| SKILL.md | 本文件（入口） |
| workflow.md | 编排细节（Overview/Detailed Steps/Acceptance Criteria + mermaid） |
| bind.md | 资源/行为/失败约束 |
| dependencies.yaml | 角色技能/工具依赖 |
| roles/*.md | 各角色完整提示词 |
| scripts/swarmflow.py | 可执行编排（plan/run/status/selftest） |
