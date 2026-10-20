# qiqi-forge-swarm

> **标签**：`task-decomposition` `dag` `multi-agent` `orchestration` `swarm-skill` `python`

> 多角色团队技能（Swarm Skill） · v1.0.0 · Python 3.10+ · 仅标准库，零第三方依赖

## 功能简介

**技能组建编排：把复杂任务拆成子任务，检索可用技能，织成 DAG，装配成执行计划**

技能组建编排团队（5 角色）：把一个复杂任务拆成子任务，检索可用技能，织成 DAG，装配成执行计划，再交给宿主派发执行。
当用户需要「把大任务拆成步骤」「为任务找合适的技能/工具」「编排一条可执行流水线」「生成执行计划」时使用。
触发词：任务分解、技能编排、DAG 编排、执行计划、五轮管道、forge、组队。


**触发词**：任务分解、技能编排、DAG 编排、执行计划、五轮管道、forge、组队


## 特点

- ✅ 自带 `selftest` 自检命令，开箱即验
- ✅ 仅用 Python 标准库，`pip install` 零依赖
- ✅ 工作区可参数化（`--workspace` / 环境变量 `QIQI_WORKSPACE`），无硬编码路径
- ✅ 默认输出 JSON，便于被其他智能体/流程编排调用
- ✅ 需要 LLM 推理的阶段输出 `needs_llm` 交接包，绝不编造内容

## 快速开始

```bash
python3 scripts/<入口脚本>.py selftest
```

## 来源与改动

- **来源模块**：`tianheng/{decomposer,skill_index,weaver,assembler,pipeline}.py` + `core/workflow/` + `core/qiqi_swarm.py`

### 诚实标注（未移植/边界）

## ⚠️ 迁移注意
1. **执行官绝不假装执行**（最高优先级）。原 `qiqi_swarm.py::_dispatch_batch` 只把 worker 写进 `self._active`/`self._queue` 就返回 `success=any(spawned)`，**从未真正创建子会话**；`subagent_orchestrator.py` 仅 `register`/`complete` 登记。迁移时**不得**把"已入队"当成"已执行"。
2. **三方依赖已剥离**：原件 `skill_index.py` 顶层 `import numpy/sklearn/scipy/jieba`。本骨架改纯 Python 近似检索，检索精度**低于**原件，已在 `uncertainty` 与 `workflow.md` 标注。
3. **路径参数化**：原件 `skill_index.py` 用 `WORKSPACE = dirname(dirname(dirname(abspath(__file__))))` 定位 `skills/`，`qiqi_swarm.py` 状态写 `WORKSPACE/data/`。本骨架统一走 `--workspace` / `QIQI_WORKSPACE`，数据只落 `<workspace>/data/qiqi-forge-swarm/`。
4. **状态目录隔离**：不写 `data/qiqi_swarm_state.json`、不写 `data/subagent_orchestrator.json`、不写 `core/tianheng/skill_index_data/`。
5. **宿主要补的能力**：真实子会话派发（spawn）、并发上限、熔断、执行结果回收。
6. **引入前先过 `skill-scope` 内容安全校验**。


## 目录结构

```
SKILL.md
LICENSE
roles/assembler.md
roles/decomposer.md
roles/executor.md
roles/retriever.md
roles/weaver.md
samples/example-input.json
scripts/swarmflow.py
workflow.md
```

## 许可证

BSD-3-Clause（见 [LICENSE](LICENSE)）