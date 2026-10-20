# bind.md · qiqi-forge-swarm

## Resource Constraints

| 约束 | 值 |
|:--|:--|
| max_parallel_teammates | 1（分解→检索→织网→装配严格串行；执行阶段并行度由宿主决定） |
| total_wall_clock_budget | 计划产出 ≤ 15 分钟；执行阶段预算由宿主 bind 决定 |
| total_token_budget | 确定性阶段（01–04）不消耗 LLM token；05 由宿主按子任务预算 |

## Behavioral Constraints

- 前四官只产出计划，不执行任何子任务；
- 织网官发现环或缺失依赖时 DAG 判 is_valid=false，直接退回分解官修正，不静默修复；
- 执行官只能派发计划内子任务，不得私加任务；
- 没有真实派发通道时，交付物是计划 + needs_executor 交接包，绝不声称已完成。

## Failure Handling

- 角色超时/输出不合法：重试 1 次，仍失败则最终报告如实标记该阶段失败；
- DAG has_cycles/missing_deps 非空：返回 decomposer 重新分解（最多 2 轮）；
- 执行官不可用：输出 needs_executor，plan 原样保留供宿主接手；
- 输入超规模（子任务数 > 50）：降级为「只分解 + 给出人工编排建议」模式并如实标注。
