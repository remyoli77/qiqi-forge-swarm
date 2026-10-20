#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qiqi-forge-swarm · 技能组建编排团队 —— 可执行编排骨架
仅标准库（Python 3.12），无需 pip install。

用法：
    python3 scripts/swarmflow.py plan
    python3 scripts/swarmflow.py run --input samples/example-input.json
    python3 scripts/swarmflow.py status --run-id <id>
    python3 scripts/swarmflow.py selftest

工作区解析顺序：--workspace > 环境变量 QIQI_WORKSPACE > 当前工作目录（禁止硬编码路径）
产物落盘：      <workspace>/data/qiqi-forge-swarm/<run_id>/NN-<stage>.json

诚实性说明（重要）：
  * 原 core/tianheng/{decomposer,skill_index,weaver,assembler,pipeline}.py 是**真实的确定性实现**
    （模板/动词序/拓扑排序），本骨架逐条复刻，非占位。
  * 原 tianheng/skill_index.py 与 retriever.py 依赖 numpy / sklearn / scipy / jieba（三方库）。
    本骨架改用**纯 Python 的 2-gram + 标签加权打分**近似原件两级检索，并如实标注差异。
  * 原 core/qiqi_swarm.py 是"编排大脑"而非"执行器"：其 `_dispatch_batch` 只写
    active/queue 状态，**并不真正派发子会话**；core/subagent_orchestrator.py 也只做
    并发登记（register/complete），不负责真实 spawn。因此本团队执行官
    一律输出 needs_executor 交接包，**绝不假装已执行**。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

SWARM_NAME = "qiqi-forge-swarm"
SWARM_TITLE = "技能组建编排团队"
VERSION = "1.0.0"

# ────────────────────────────── 阶段表 ──────────────────────────────

STAGES = [
    {
        "id": "01-decompose", "name": "分解", "role": "decomposer", "role_name": "分解官",
        "prompt_file": "roles/decomposer.md", "depends_on": [],
        "requires_llm": False, "hard": True,
        "inputs": "input.query, input.max_subtasks?",
        "outputs": "outputs.subtasks[{index,description,dependencies[]}]",
        "acceptance": "subtasks 非空；每个含 index/description/dependencies；dependencies 索引必须 < 自身 index",
    },
    {
        "id": "02-retrieve", "name": "检索", "role": "retriever", "role_name": "检索官",
        "prompt_file": "roles/retriever.md", "depends_on": ["01-decompose"],
        "requires_llm": False, "hard": False,
        "inputs": "01-decompose 的 subtasks[], input.skill_catalog[]",
        "outputs": "outputs.assignments[{index,skill_name,skill_score,recommended_skills[]}]",
        "acceptance": "每个 subtask 得到 top-k 推荐；无 catalog 时输出 needs_llm，不得编造技能名",
    },
    {
        "id": "03-weave", "name": "织网", "role": "weaver", "role_name": "织网官",
        "prompt_file": "roles/weaver.md", "depends_on": ["02-retrieve"],
        "requires_llm": False, "hard": False,
        "inputs": "subtasks[] + assignments[]",
        "outputs": "outputs.dag{nodes[],edges[],execution_order[],is_valid,has_cycles,missing_deps[]}",
        "acceptance": "is_valid=true；无环；execution_order 覆盖全部节点；edges 无自环",
    },
    {
        "id": "04-assemble", "name": "装配", "role": "assembler", "role_name": "装配官",
        "prompt_file": "roles/assembler.md", "depends_on": ["03-weave"],
        "requires_llm": False, "hard": False,
        "inputs": "03-weave 的 dag",
        "outputs": "outputs.plan{team_id,units[],stages{},execution_order[]}",
        "acceptance": "units 数量 == 节点数量；stages 覆盖全部 unit；同阶段单元无相互依赖",
    },
    {
        "id": "05-execute", "name": "执行", "role": "executor", "role_name": "执行官",
        "prompt_file": "roles/executor.md", "depends_on": ["04-assemble"],
        "requires_llm": False, "hard": True,
        "inputs": "04-assemble 的 plan",
        "outputs": "（无）—— 需要宿主真实派发能力",
        "acceptance": "有真实 subagent 通道时回填每单元执行结果；无通道时必须输出 needs_executor，不得假装已执行",
    },
]

EXECUTORS = {}

# ────────────────────────────── 常量（抄自原件） ──────────────────────────────

# decomposer.py::DECOMPOSITION_TEMPLATES
DECOMPOSITION_TEMPLATES = {
    "research": {"keywords": ["研究", "分析", "调研", "了解", "学习", "research", "study"],
                 "subtasks": ["收集相关信息", "整理分析资料", "总结输出报告"], "deps": [[], [0], [0, 1]]},
    "pipeline": {"keywords": ["流程", "管道", "流水线", "串联", "pipeline"],
                 "subtasks": ["定义输入", "执行处理", "输出结果"], "deps": [[], [0], [0, 1]]},
    "create_image": {"keywords": ["画", "图", "生成图片", "作画", "创作", "image", "draw"],
                     "subtasks": ["构思主题与构图", "生成图片"], "deps": [[], [0]]},
    "backup": {"keywords": ["备份", "云盘", "同步", "backup", "upload"],
               "subtasks": ["创建本地备份", "上传到云存储", "验证备份完整性"], "deps": [[], [0], [0, 1]]},
    "document_process": {"keywords": ["文档", "pdf", "word", "读取", "提取", "doc", "document"],
                         "subtasks": ["读取源文档", "提取关键内容", "输出处理结果"], "deps": [[], [0], [0, 1]]},
    "data_analysis": {"keywords": ["统计", "分析", "表格", "数据", "excel", "data"],
                      "subtasks": ["获取数据源", "处理分析数据", "生成报告"], "deps": [[], [0], [0, 1]]},
    "multimodal": {"keywords": ["多模态", "组合", "综合", "复合"],
                   "subtasks": ["解析各模态需求", "分别处理", "整合最终结果"], "deps": [[], [0], [0, 1]]},
}
CONJUNCTIONS = ["和", "与", "并", "然后", "接着", "之后", "同时", "以及"]
ACTION_VERBS = ["搜索", "查找", "查询", "获取", "读取", "下载", "上传", "创建", "生成", "画", "写", "制作",
                "编辑", "修改", "保存", "备份", "删除", "发送", "通知", "提醒", "分析", "统计", "计算",
                "比较", "排序", "过滤", "转换", "翻译", "提取", "合并", "分割"]

# weaver.py::DependencyInferrer
VERB_ORDER = [
    ["搜索", "查找", "查询", "检索", "获取", "收集", "fetch", "search", "get", "collect"],
    ["读取", "解析", "提取", "read", "parse"],
    ["分析", "统计", "计算", "比较", "analyze", "process", "compute"],
    ["生成", "创作", "画", "制作", "create", "generate", "draw"],
    ["总结", "汇总", "报告", "输出", "summarize", "report"],
    ["保存", "上传", "导出", "save", "upload", "export"],
]
TYPE_PRODUCERS = {
    "搜索结果": ["搜索", "查找", "查询", "检索", "search", "fetch", "get"],
    "文档内容": ["读取", "提取", "解析", "文档", "pdf", "read", "parse", "extract"],
    "图片": ["生成", "画", "创作", "image", "draw", "gen", "create"],
    "分析结果": ["分析", "统计", "计算", "analyze", "compute", "stats"],
    "报告": ["报告", "总结", "日报", "周报", "report", "summary"],
    "数据": ["数据", "信息", "内容", "data", "info", "content"],
    "文件": ["文件", "保存", "上传", "下载", "file", "save", "upload", "download"],
}
TYPE_CONSUMERS = {
    "分析": ["分析", "统计", "计算", "整理", "analyze", "process"],
    "总结": ["总结", "报告", "输出", "汇总", "summarize", "report"],
    "保存": ["保存", "存储", "上传", "导出", "save", "store", "export"],
    "生成": ["生成", "创建", "制作", "画", "写", "create", "generate"],
    "使用": ["使用", "调用", "结合", "use", "combine", "merge"],
}

SAD_REFINE_ROUNDS = 2       # decomposer.py: for iteration in range(2)
SAD_LOW_SCORE = 0.05        # decomposer.py: if subtask.skill_score < 0.05
SAD_QUALITY_THRESHOLD = 0.1  # decomposer.py: if avg_score < 0.1

NEEDS_LLM_STATUSES = {"needs_llm", "needs_executor", "needs_input"}


# ────────────────────────────── 工具函数 ──────────────────────────────

def resolve_workspace(cli_workspace: str | None) -> Path:
    if cli_workspace:
        return Path(cli_workspace).expanduser().resolve()
    env = os.environ.get("QIQI_WORKSPACE")
    if env:
        return Path(env).expanduser().resolve()
    return Path.cwd().resolve()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _tokens(text: str) -> set:
    """中文 2-gram + 英文词。用于纯 Python 近似检索与名词重叠判断。"""
    text = text or ""
    grams = set()
    for m in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        for i in range(len(m) - 1):
            grams.add(m[i:i + 2])
    grams |= {w.lower() for w in re.findall(r"[A-Za-z]{3,}", text)}
    return grams


def _packet(stage, ctx, status, *, inputs=None, outputs=None, validation=None,
            uncertainty=None, note="", extra=None) -> dict:
    packet = {
        "swarm": SWARM_NAME,
        "stage": stage["id"], "stage_name": stage["name"],
        "role": stage["role"], "role_name": stage["role_name"],
        "prompt_file": stage["prompt_file"], "depends_on": list(stage["depends_on"]),
        "status": status,
        "started_at": ctx.get("_stage_started_at"), "finished_at": _now(),
        "inputs": inputs if inputs is not None else {},
        "outputs": outputs if outputs is not None else {},
        "validation": validation or {"checks": [], "passed": None},
        "uncertainty": uncertainty or [], "note": note, "next": None,
    }
    if extra:
        packet.update(extra)
    return packet


def _needs_host(stage, ctx, needs, payload, uncertainty, note="") -> dict:
    """需要 LLM / 真实执行器 / 真实索引时：输出契约交接包，绝不编造产出。"""
    return _packet(stage, ctx, "needs_llm", inputs={"required": stage["inputs"]}, outputs={},
                   uncertainty=uncertainty,
                   note=note or "该阶段需要宿主能力；产出留空，等宿主回填。",
                   extra={"status": "needs_llm", "stage": stage["id"], "role": stage["role"],
                          "prompt_file": stage["prompt_file"], "input": payload, "needs": needs})


def _failed(stage, ctx, reason, inputs=None) -> dict:
    return _packet(stage, ctx, "failed", inputs=inputs, outputs={},
                   validation={"checks": [{"name": "stage", "passed": False, "detail": reason}], "passed": False},
                   uncertainty=[reason], note="阶段失败。")


# ────────────────────────────── 通用小工具（decomposer / retriever 共用） ──────────────────────────────

def _score_entry(query: str, entry: dict) -> float:
    """纯 Python 近似两级检索打分（替代原件的 TF-IDF + 余弦相似度）。"""
    qt = _tokens(query)
    if not qt:
        return 0.0
    name_t = _tokens(entry.get("name", ""))
    tag_t = _tokens(" ".join(entry.get("tags", [])))
    desc_t = _tokens(entry.get("description", ""))
    raw = 2.0 * len(qt & name_t) + 1.5 * len(qt & tag_t) + 1.0 * len(qt & desc_t)
    for tag in entry.get("tags", []):
        if tag and tag in query:
            raw += 1.0
    return round(raw / (raw + 3.0), 4) if raw > 0 else 0.0


def _retrieve(query: str, catalog: list, top_k: int = 5) -> list:
    scored = [{"name": e.get("name", ""), "score": _score_entry(query, e),
               "description": (e.get("description", "") or "")[:100],
               "category": e.get("category", "general"), "tags": e.get("tags", [])}
              for e in catalog]
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


# ────────────────────────────── 阶段实现 ──────────────────────────────

def _stage_decompose(stage, inp, ctx, ws, indir, input_dir=None):
    query = (inp.get("query") or "").strip()
    if not query:
        return _failed(stage, ctx, "缺少 input.query（硬前置）")
    max_subtasks = int(inp.get("max_subtasks") or 6)
    low = query.lower()

    matched_template = None
    for tname, tpl in DECOMPOSITION_TEMPLATES.items():
        n = sum(1 for kw in tpl["keywords"] if kw in low)
        if n >= 2:
            matched_template = (tname, tpl, n)
            break

    if matched_template:
        tname, tpl, n = matched_template
        subtasks = [{"index": i, "description": d,
                     "dependencies": (tpl["deps"][i] if i < len(tpl["deps"]) else []),
                     "is_decomposable": False}
                    for i, d in enumerate(tpl["subtasks"])]
        path_used = f"template:{tname}(hits={n})"
    else:
        signals = []
        for conj in CONJUNCTIONS:
            if conj in query:
                parts = query.split(conj)
                if len(parts) >= 2:
                    signals = [(i, p.strip()) for i, p in enumerate(parts)]
                    break
        if not signals:
            verbs = [v for v in ACTION_VERBS if v in query]
            if len(verbs) >= 2:
                signals = [(i, v) for i, v in enumerate(verbs)]
        if signals:
            seen, subtasks = set(), []
            for _, text in signals:
                key = text.strip()[:40]
                if key in seen:
                    continue
                seen.add(key)
                subtasks.append({"index": len(subtasks), "description": text.strip()[:80],
                                 "dependencies": [len(subtasks) - 1] if subtasks else [],
                                 "is_decomposable": True})
            subtasks = subtasks[:max_subtasks]
            path_used = "rule:conjunction" if any(c in query for c in CONJUNCTIONS) else "rule:verbs"
        else:
            subtasks = [{"index": 0, "description": query[:80], "dependencies": [], "is_decomposable": True}]
            path_used = "fallback:single"

    ctx["subtasks"] = subtasks
    checks = [
        {"name": "subtasks_nonempty", "passed": len(subtasks) >= 1, "detail": str(len(subtasks))},
        {"name": "fields_present", "passed": all(
            all(k in s for k in ("index", "description", "dependencies")) for s in subtasks),
         "detail": "index/description/dependencies"},
        {"name": "deps_before_self", "passed": all(d < s["index"] for s in subtasks for d in s["dependencies"]),
         "detail": "依赖索引必须小于自身"},
    ]
    unc = []
    if path_used.startswith("fallback"):
        unc.append("未匹配到模板且未识别出分解信号，整体任务被当成单个子任务")
    if path_used.startswith("rule:verbs"):
        unc.append("走动词规则分解：子任务描述为裸动词（原件 rule-based 的既有行为），粒度可能过粗")
    return _packet(stage, ctx, "ok",
                   inputs={"query": query, "max_subtasks": max_subtasks},
                   outputs={"subtasks": subtasks, "decomposition_path": path_used},
                   validation={"checks": checks, "passed": all(c["passed"] for c in checks)},
                   uncertainty=unc,
                   note="模板表与规则抄自 decomposer.py::DECOMPOSITION_TEMPLATES / _rule_based_decompose。")


def _stage_retrieve(stage, inp, ctx, ws, indir, input_dir=None):
    subtasks = ctx.get("subtasks")
    if not subtasks:
        return _packet(stage, ctx, "skipped", uncertainty=["分解官未产出 subtasks"], note="阶段未执行。")
    catalog = inp.get("skill_catalog") or []
    if not catalog:
        return _needs_host(
            stage, ctx, "skill_index_or_catalog",
            {"subtasks": subtasks,
             "expected_catalog_schema": {"skill_catalog": [
                 {"name": "skill-name", "description": "一句话描述", "tags": ["标签"], "category": "search"}]},
             "note": "原件 index 由 numpy+sklearn(TfidfVectorizer)+jieba 构建；本骨架改用调用方提供的 catalog"},
            ["未提供 input.skill_catalog，复用原件索引需 numpy/sklearn/jieba（本骨架不引入三方库）"],
            note="检索官需要技能目录；请宿主提供后重投。")

    assignments, breakdown = [], []
    for s in subtasks:
        rec = _retrieve(s["description"], catalog, top_k=5)
        top = rec[0] if rec else None
        assignments.append({"index": s["index"], "skill_name": top["name"] if top else "",
                            "skill_score": top["score"] if top else 0.0,
                            "recommended_skills": rec})
        breakdown.append({"index": s["index"], "query": s["description"],
                          "top": top["name"] if top else None, "score": top["score"] if top else 0.0})
        s["skill_name"] = top["name"] if top else ""
        s["skill_score"] = top["score"] if top else 0.0
        s["recommended_skills"] = rec

    # SAD 迭代对齐（decomposer.py::_check_decomposition_quality / _sad_refine）
    sad_iterations, sad_improved = 0, False
    for it in range(SAD_REFINE_ROUNDS):
        all_zero = all(s["skill_score"] == 0.0 for s in subtasks)
        avg = sum(s["skill_score"] for s in subtasks) / len(subtasks)
        if not (all_zero or avg < SAD_QUALITY_THRESHOLD):
            break
        new = []
        for s in subtasks:
            if s["skill_score"] < SAD_LOW_SCORE:
                if s.get("recommended_skills"):
                    hints = ", ".join(r["name"] for r in s["recommended_skills"][:3])
                    new.append({"index": len(new), "description": f"{s['description']}（可用工具: {hints}）",
                                "dependencies": [len(new) - 1] if new else [], "is_decomposable": True})
                else:
                    new.append({"index": len(new), "description": f"分析需求: {s['description']}",
                                "dependencies": [], "is_decomposable": True})
                    new.append({"index": len(new), "description": f"执行: {s['description']}",
                                "dependencies": [len(new) - 1], "is_decomposable": True})
            else:
                s["index"] = len(new)
                s["dependencies"] = [d for d in s["dependencies"] if d < s["index"]]
                new.append(s)
        subtasks = new
        for s in subtasks:
            rec = _retrieve(s["description"], catalog, top_k=5)
            s["recommended_skills"] = rec
            s["skill_name"] = rec[0]["name"] if rec else ""
            s["skill_score"] = rec[0]["score"] if rec else 0.0
        sad_iterations, sad_improved = it + 1, True
    ctx["subtasks"] = subtasks
    assignments = [{"index": s["index"], "skill_name": s["skill_name"], "skill_score": s["skill_score"],
                    "recommended_skills": s["recommended_skills"]} for s in subtasks]
    ctx["assignments"] = assignments

    checks = [
        {"name": "all_subtasks_assigned", "passed": len(assignments) == len(subtasks),
         "detail": f"{len(assignments)}/{len(subtasks)}"},
        {"name": "scores_in_range", "passed": all(0.0 <= a["skill_score"] <= 1.0 for a in assignments),
         "detail": "0..1"},
        {"name": "no_fabricated_skill", "passed": all(
            (a["skill_name"] == "" or any(a["skill_name"] == e.get("name") for e in catalog))
            for a in assignments), "detail": "技能名必须来自 catalog"},
    ]
    unc = []
    if any(a["skill_score"] == 0.0 for a in assignments):
        unc.append("部分子任务在 catalog 中无匹配（score=0），建议人工补充技能或改写子任务描述")
    unc.append("检索为纯 Python 2-gram+标签加权近似，非原件的 TF-IDF/余弦相似度")
    return _packet(stage, ctx, "ok",
                   inputs={"subtasks": len(subtasks), "catalog_size": len(catalog)},
                   outputs={"assignments": assignments, "breakdown": breakdown,
                            "sad_iterations": sad_iterations, "sad_improved": sad_improved},
                   validation={"checks": checks, "passed": all(c["passed"] for c in checks)},
                   uncertainty=unc,
                   note="打分函数近似原件 tianheng/retriever.py 的两级检索；SAD 迭代逻辑抄自 decomposer.py。")


def _verb_level(desc: str) -> int:
    level = 0
    for i, verbs in enumerate(VERB_ORDER):
        for v in verbs:
            if v in desc:
                level = max(level, i + 1)
                break
    return level


def _produce_types(desc: str) -> set:
    return {t for t, kws in TYPE_PRODUCERS.items() if any(k in desc for k in kws)}


def _consume_types(desc: str) -> set:
    return {t for t, kws in TYPE_CONSUMERS.items() if any(k in desc for k in kws)}


def _should_depend(consumer: str, producer: str) -> bool:
    c, p = consumer.lower(), producer.lower()
    if _verb_level(c) > _verb_level(p):
        return True
    if _produce_types(p) & _consume_types(c):
        return True
    if len(_tokens(p) & _tokens(c)) >= 2:
        return True
    return False


def _stage_weave(stage, inp, ctx, ws, indir, input_dir=None):
    subtasks = ctx.get("subtasks")
    if not subtasks:
        return _packet(stage, ctx, "skipped", uncertainty=["上游未产出 subtasks"], note="阶段未执行。")
    n = len(subtasks)
    descs = [s["description"] for s in subtasks]

    deps = {s["index"]: set() for s in subtasks}
    for i in range(n):
        for j in range(n):
            if i != j and _should_depend(descs[i], descs[j]):
                deps[i].add(j)
    for s in subtasks:
        for d in s.get("dependencies", []):
            if d < n:
                deps[s["index"]].add(d)

    # 环消除（Kahn 迭代，打破动词级别最低的边）——weaver.py::_break_cycles
    # 注意：edges 元素是 (i, j) 二元组（i 依赖 j）。原件此处把二元组按三元解包，凡子任务对
    # 出现"推断依赖与声明依赖互指成环"即 ValueError 崩溃。本骨架改为：成环时断掉动词级别最低
    # 的一条边，逐条记录到 broken_edges（断环降级并留痕），绝不因成环而崩溃。
    broken_edges = []
    for _ in range(n * 2 + 1):
        indeg = {i: 0 for i in range(n)}
        for i in range(n):
            for j in deps.get(i, set()):
                if j in indeg:
                    indeg[j] += 1
        q = deque([i for i in range(n) if indeg.get(i, 0) == 0])
        visited = set()
        while q:
            node = q.popleft()
            visited.add(node)
            for nb in list(deps.get(node, set())):
                if nb in indeg:
                    indeg[nb] -= 1
                    if indeg[nb] == 0:
                        q.append(nb)
        cycle = set(range(n)) - visited
        if not cycle:
            break
        edges = [(i, j) for i in cycle for j in list(deps.get(i, set())) if j in cycle]
        if not edges:
            break
        edges.sort(key=lambda e: min(_verb_level(descs[e[0]]), _verb_level(descs[e[1]])))
        bi, bj = edges[0]              # (i, j) 二元组：(消费者, 生产者)
        deps[bi].discard(bj)
        broken_edges.append([bi, bj])

    deps = {k: sorted(v) for k, v in deps.items()}
    nodes, edges = [], []
    depended_by = defaultdict(list)
    for s in subtasks:
        nodes.append({"index": s["index"], "description": s["description"],
                      "skill_name": s.get("skill_name", ""), "skill_score": s.get("skill_score", 0.0),
                      "depends_on": deps[s["index"]], "depended_by": []})
    for node in nodes:
        for d in node["depends_on"]:
            if d != node["index"]:
                edges.append((d, node["index"]))
                depended_by[d].append(node["index"])
    for node in nodes:
        node["depended_by"] = sorted(depended_by[node["index"]])

    missing_deps = [node["index"] for node in nodes for d in node["depends_on"] if d >= n]
    # 拓扑排序（Kahn）
    indeg = {node["index"]: 0 for node in nodes}
    adj = defaultdict(list)
    for f, t in edges:
        adj[f].append(t)
        indeg[t] += 1
    q = deque([i for i, d in indeg.items() if d == 0])
    order = []
    while q:
        cur = q.popleft()
        order.append(cur)
        for nb in adj[cur]:
            indeg[nb] -= 1
            if indeg[nb] == 0:
                q.append(nb)
    remaining = sorted(set(indeg) - set(order))
    order.extend(remaining)
    has_cycles = bool(remaining) or any(f == t for f, t in edges)
    is_valid = (not has_cycles) and (not missing_deps)

    dag = {"node_count": len(nodes), "edge_count": len(edges), "nodes": nodes,
           "edges": [list(e) for e in edges], "execution_order": order,
           "is_valid": is_valid, "has_cycles": has_cycles, "missing_deps": missing_deps,
           "broken_edges": [list(e) for e in broken_edges]}
    ctx["dag"] = dag
    checks = [
        {"name": "is_valid", "passed": is_valid, "detail": f"cycles={has_cycles} missing={missing_deps}"},
        {"name": "no_self_edge", "passed": all(f != t for f, t in edges), "detail": "无自环"},
        {"name": "order_covers_all", "passed": set(order) == set(indeg), "detail": f"{len(order)}/{len(nodes)}"},
        {"name": "deps_precede", "passed": all(order.index(d) < order.index(node["index"])
                                               for node in nodes for d in node["depends_on"]),
         "detail": "依赖先于被依赖者执行"},
        {"name": "cycle_broken_recorded", "passed": (not has_cycles) or bool(broken_edges),
         "detail": f"broken_edges={len(broken_edges)}"},
    ]
    unc = []
    if broken_edges:
        unc.append(f"检测到 {len(broken_edges)} 处互指成环依赖，已断环降级（见 dag.broken_edges），"
                   "被断边不再作为依赖")
    if not is_valid:
        unc.append("DAG 校验未通过，装配阶段可能不可用")
    return _packet(stage, ctx, "ok",
                   inputs={"subtasks": n},
                   outputs={"dag": dag},
                   validation={"checks": checks, "passed": all(c["passed"] for c in checks)},
                   uncertainty=unc,
                   note="依赖推断/环消除/拓扑排序抄自 weaver.py::DependencyInferrer / TaskWeaver。")


def _stage_assemble(stage, inp, ctx, ws, indir, input_dir=None):
    dag = ctx.get("dag")
    query = (inp.get("query") or "").strip()
    if not dag:
        return _packet(stage, ctx, "skipped", uncertainty=["织网官未产出 dag"], note="阶段未执行。")
    order = dag["execution_order"]
    order_map = {idx: pos for pos, idx in enumerate(order)}
    units = []
    for node in dag["nodes"]:
        unit_id = "unit_" + _sha256(f"{query}|{node['index']}|{node.get('skill_name', '')}")[:6]
        units.append({
            "unit_id": unit_id, "index": node["index"], "description": node["description"],
            "skill_name": node.get("skill_name", ""), "skill_score": node.get("skill_score", 0.0),
            "depends_on": list(node["depends_on"]), "depended_by": list(node["depended_by"]),
            "execution_order": order_map.get(node["index"], -1), "status": "pending",
        })
    umap = {u["index"]: u for u in units}

    # 分阶段（assembler.py::TeamAssembler.deploy_to_stage）：同一阶段内无相互依赖
    stage_of = {}          # unit_index -> stage index
    for idx in order:
        unit = umap.get(idx)
        if unit is None:
            continue
        max_dep_stage = 0
        for d in unit["depends_on"]:
            if d in stage_of:
                max_dep_stage = max(max_dep_stage, stage_of[d] + 1)
        stage_of[idx] = max_dep_stage

    stages = defaultdict(list)
    for idx in order:
        if idx in umap:
            stages[stage_of[idx]].append(umap[idx]["unit_id"])

    team_id = "team_" + _sha256(query + "|" + ",".join(map(str, order)))[:8]
    plan = {"team_id": team_id, "name": f"五轮驱动·{query[:30]}", "total_units": len(units),
            "units": units, "execution_order": order,
            "stages": {str(k): stages[k] for k in sorted(stages)}}
    ctx["plan"] = plan

    # 校验：同阶段内不得存在相互依赖
    intra_stage_dep = []
    for si, uids in plan["stages"].items():
        members = {u["index"] for u in units if u["unit_id"] in uids}
        for u in units:
            if u["index"] in members and (set(u["depends_on"]) & members):
                intra_stage_dep.append({"stage": si, "unit": u["unit_id"], "depends_on": u["depends_on"]})
    checks = [
        {"name": "units_match_nodes", "passed": len(units) == len(dag["nodes"]),
         "detail": f"{len(units)}/{len(dag['nodes'])}"},
        {"name": "stages_cover_units",
         "passed": sorted(u for v in plan["stages"].values() for u in v) == sorted(u["unit_id"] for u in units),
         "detail": f"{len(plan['stages'])} 阶段"},
        {"name": "no_intra_stage_dep", "passed": not intra_stage_dep, "detail": f"{len(intra_stage_dep)} 处"},
    ]
    return _packet(stage, ctx, "ok",
                   inputs={"nodes": len(dag["nodes"])},
                   outputs={"plan": plan},
                   validation={"checks": checks, "passed": all(c["passed"] for c in checks)},
                   uncertainty=[],
                   note="装配逻辑抄自 assembler.py::TeamAssembler.assemble / deploy_to_stage。")


def _stage_execute(stage, inp, ctx, ws, indir, input_dir=None):
    plan = ctx.get("plan")
    if not plan:
        return _failed(stage, ctx, "缺少装配官产出的 plan（硬前置）")
    ctx["_needs"] = "executor"
    # 原 qiqi_swarm._dispatch_batch 只写 active/queue 状态，subagent_orchestrator 只做并发登记，
    # 二者都不真正派发子会话。因此本阶段一律 hands-off：
    return _needs_host(
        stage, ctx, "executor",
        {"team_id": plan["team_id"], "execution_order": plan["execution_order"],
         "stages": plan["stages"],
         "units": [{"unit_id": u["unit_id"], "index": u["index"], "description": u["description"],
                    "skill_name": u["skill_name"], "depends_on": u["depends_on"]} for u in plan["units"]],
         "expected_result_schema": {"unit_results": [
             {"unit_id": "", "status": "done|failed", "output": "", "duration_ms": 0}]},
         "host_requirements": {
             "spawn": "真实子会话/子 agent 派发能力（原 subagent_orchestrator 只 register/complete，不 spawn）",
             "concurrency": "建议 max_concurrent=2..3（原 qiqi_swarm 默认 3，subagent_orchestrator 默认 2）",
             "circuit_breaker": "建议复用原 qiqi_swarm 的 closed/open/half_open 熔断（阈值 3 次、冷却 300s）",
         }},
        ["原 qiqi_swarm.py 是编排大脑而非执行器：_dispatch_batch 只写状态、不真派发",
         "core/subagent_orchestrator.py 只做并发登记与健康检查，不负责真实 spawn",
         "本骨架不编造任何执行结果"],
        note="执行官需要宿主提供真实派发能力；请按 input.host_requirements 接入后回填 unit_results。")


EXECUTORS.update({
    "01-decompose": _stage_decompose,
    "02-retrieve": _stage_retrieve,
    "03-weave": _stage_weave,
    "04-assemble": _stage_assemble,
    "05-execute": _stage_execute,
})


# ────────────────────────────── 运行引擎 ──────────────────────────────

def _execute_stage(stage, inp, ctx, ws, indir, input_dir=None) -> dict:
    ctx["_stage_started_at"] = _now()
    fn = EXECUTORS.get(stage["id"])
    if fn is None:
        return _failed(stage, ctx, f"未实现执行器: {stage['id']}")
    return fn(stage, inp, ctx, ws, indir, input_dir)


def _overall_status(records, blocked_stage):
    if any(r["status"] == "failed" for r in records):
        return "failed"
    if blocked_stage:
        return "blocked"
    if any(r["status"] in NEEDS_LLM_STATUSES for r in records):
        return "partial"
    return "completed"


def run_pipeline(inp, ws, run_id=None, quiet=False, input_dir=None):
    run_id = run_id or (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" +
                        _sha256(json.dumps(inp, ensure_ascii=False, sort_keys=True))[:8])
    indir = ws / "data" / SWARM_NAME / run_id
    indir.mkdir(parents=True, exist_ok=True)
    ctx, records, blocked_stage, stop = {}, [], None, False
    for stage in STAGES:
        pkt = (_packet(stage, ctx, "skipped", uncertainty=[f"上游硬前置 {blocked_stage} 未满足，流程已停止"],
                       note="阶段未执行。")
               if stop else _execute_stage(stage, inp, ctx, ws, indir, input_dir))
        records.append(pkt)
        if pkt["status"] in NEEDS_LLM_STATUSES and stage["hard"]:
            blocked_stage, stop = stage["id"], True
    for i, r in enumerate(records):
        r["next"] = records[i + 1]["stage"] if i + 1 < len(records) else None
    for stage, rec in zip(STAGES, records):
        (indir / f"{stage['id']}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = {
        "swarm": SWARM_NAME, "title": SWARM_TITLE, "version": VERSION,
        "run_id": run_id, "workspace": str(ws), "run_dir": str(indir),
        "overall_status": _overall_status(records, blocked_stage), "blocked_stage": blocked_stage,
        "needs": (ctx.get("_needs") or None),
        "generated_at": _now(),
        "stages": [{"stage": r["stage"], "role": r["role"], "status": r["status"],
                    "prompt_file": r["prompt_file"], "file": f"{r['stage']}.json"} for r in records],
        "input_snapshot": inp,
    }
    (indir / "run.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if not quiet:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest, records, indir


# ────────────────────────────── 输入校验（B1） ──────────────────────────────

# 顶层字段类型约束：非法 JSON / 字段类型错误统一为 stdout JSON + exit 2
INPUT_TYPE_SPEC = {
    "query": (str,), "max_subtasks": (int,), "skill_catalog": (list,),
}
INPUT_LIST_ELEM = {"skill_catalog": (dict,)}


def _check_input_types(inp):
    if not isinstance(inp, dict):
        return f"输入 JSON 顶层必须是对象，实际 {type(inp).__name__}"
    for key, types in INPUT_TYPE_SPEC.items():
        if key in inp and inp[key] is not None and not isinstance(inp[key], types):
            names = "/".join(t.__name__ for t in types)
            return f"输入字段类型错误: {key} 期望 {names}，实际 {type(inp[key]).__name__}"
    for key, elems in INPUT_LIST_ELEM.items():
        v = inp.get(key)
        if isinstance(v, list) and any(not isinstance(x, elems) for x in v):
            names = "/".join(t.__name__ for t in elems)
            return f"输入字段元素类型错误: {key}[] 元素期望 {names}"
    return None


# ────────────────────────────── 子命令 ──────────────────────────────

def cmd_plan(args):
    print(f"🔥 {SWARM_NAME} · {SWARM_TITLE}  v{VERSION}")
    print("=" * 66)
    print("DAG：")
    print()
    for i, s in enumerate(STAGES):
        arrow = "  " if i == 0 else ("  │\n  ▼\n")
        flags = (" [需宿主执行器]" if s["id"] == "05-execute" else
                 " [需 catalog]" if s["id"] == "02-retrieve" else "")
        hard = " (硬前置)" if s["hard"] else ""
        print(f"{arrow}  [{s['id']}] {s['name']}{flags}{hard}")
    print()
    print("各阶段角色 / 输入 / 输出 / 验收标准：")
    print("-" * 66)
    for s in STAGES:
        print(f"[{s['id']}] {s['name']}   角色: {s['role']} ({s['role_name']})"
              f"   依赖: {s['depends_on'] or '无'}")
        print(f"    提示词: {s['prompt_file']}")
        print(f"    输入:   {s['inputs']}")
        print(f"    输出:   {s['outputs']}")
        print(f"    验收:   {s['acceptance']}")
        print()
    print("-" * 66)
    print("产物落盘： <workspace>/data/" + SWARM_NAME + "/<run_id>/NN-<stage>.json")
    print("诚实性：   05-execute 无真实派发通道时输出 needs_executor，绝不假装已执行。")


def cmd_run(args):
    ws = resolve_workspace(args.workspace)
    inp_path = Path(args.input).expanduser().resolve()
    if not inp_path.is_file():
        print(json.dumps({"error": f"输入文件不存在: {inp_path}"}, ensure_ascii=False))
        return 2
    try:
        raw = inp_path.read_text(encoding="utf-8")
    except OSError as e:
        print(json.dumps({"error": f"输入文件不可读: {e}"}, ensure_ascii=False))
        return 2
    try:
        inp = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"输入 JSON 非法: {e}"}, ensure_ascii=False))
        return 2
    err = _check_input_types(inp)
    if err:
        print(json.dumps({"error": err}, ensure_ascii=False))
        return 2
    try:
        manifest, _, _ = run_pipeline(inp, ws, run_id=args.run_id, input_dir=inp_path.parent)
    except OSError as e:
        print(json.dumps({"error": f"run 目录不可写: {e}"}, ensure_ascii=False))
        return 2
    return 0 if manifest["overall_status"] in ("completed", "partial") else 2


def cmd_status(args):
    ws = resolve_workspace(args.workspace)
    run_dir = ws / "data" / SWARM_NAME / args.run_id
    mf = run_dir / "run.json"
    if not mf.is_file():
        print(json.dumps({"error": f"未找到 run: {args.run_id}", "dir": str(run_dir)}, ensure_ascii=False))
        return 2
    try:
        manifest = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(json.dumps({"error": f"run.json 不可读/非法: {e}", "dir": str(run_dir)}, ensure_ascii=False))
        return 2
    # B2：默认只输出机器可解析的 JSON；人类可读内容仅在 --format text 时输出
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    if (getattr(args, "format", "json") or "json") != "text":
        return 0
    print("\n阶段交接包：")
    for s in manifest["stages"]:
        f = run_dir / s["file"]
        if f.is_file():
            pkt = json.loads(f.read_text(encoding="utf-8"))
            print(f"  {s['stage']:<14} {s['status']:<14} schema={'ok' if _valid_packet(pkt) else 'BAD'}")
        else:
            print(f"  {s['stage']:<14} {s['status']:<14} 缺少文件: {s['file']}")
    wf, af = run_dir / "03-weave.json", run_dir / "04-assemble.json"
    if wf.is_file():
        dag = json.loads(wf.read_text(encoding="utf-8"))["outputs"].get("dag", {})
        print(f"\nDAG: {dag.get('node_count')} 节点 / {dag.get('edge_count')} 边 "
              f"valid={dag.get('is_valid')} order={dag.get('execution_order')}")
    if af.is_file():
        plan = json.loads(af.read_text(encoding="utf-8"))["outputs"].get("plan", {})
        print(f"装配: {plan.get('total_units')} 单元 / {len(plan.get('stages', {}))} 阶段 ({plan.get('team_id')})")
        for si, uids in sorted(plan.get("stages", {}).items(), key=lambda x: int(x[0])):
            names = [u["skill_name"] or u["description"][:12] for u in plan["units"] if u["unit_id"] in uids]
            print(f"  阶段{si}: {names}")
    return 0


REQUIRED_KEYS = ["swarm", "stage", "stage_name", "role", "role_name", "prompt_file",
                 "depends_on", "status", "started_at", "finished_at", "inputs",
                 "outputs", "validation", "uncertainty", "next"]


def _valid_packet(pkt) -> bool:
    if not isinstance(pkt, dict) or any(k not in pkt for k in REQUIRED_KEYS):
        return False
    if pkt.get("status") not in ("ok", "needs_llm", "needs_executor", "needs_input", "failed", "skipped"):
        return False
    if not isinstance(pkt.get("uncertainty"), list):
        return False
    if pkt.get("status") == "needs_llm":
        if not all(k in pkt for k in ("stage", "role", "prompt_file", "input")):
            return False
        if pkt.get("outputs"):
            return False
    if pkt.get("status") in ("ok", "failed") and "checks" not in pkt.get("validation", {}):
        return False
    if pkt.get("status") == "ok" and not pkt["validation"]["checks"]:
        return False
    return True


def cmd_selftest(args):
    pkg_root = Path(__file__).resolve().parent.parent
    sample = pkg_root / "samples" / "example-input.json"
    print(f"🧪 {SWARM_NAME} selftest —— 使用示例输入（明确标注为示例，非真实生产数据）")
    print(f"   sample: {sample}")
    if not sample.is_file():
        print("   ❌ FAIL: 缺少 samples/example-input.json")
        return 1
    inp = json.loads(sample.read_text(encoding="utf-8"))
    ws = Path(tempfile.mkdtemp(prefix="qiqi-forge-selftest-"))
    print(f"   临时工作区: {ws}\n")
    manifest, records, indir = run_pipeline(inp, ws, run_id="selftest", quiet=True, input_dir=sample.parent)
    fails = 0

    def check(name, cond, detail=""):
        nonlocal fails
        print(f"   {'✅ PASS' if cond else '❌ FAIL'}: {name}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails += 1

    check("run.json 落盘", (indir / "run.json").is_file(), str(indir))
    for stage, rec in zip(STAGES, records):
        f = indir / f"{stage['id']}.json"
        check(f"{stage['id']} 交接包落盘", f.is_file(), f.name)
        check(f"{stage['id']} schema 合法", _valid_packet(rec), f"status={rec['status']}")
    by = {r["stage"]: r for r in records}
    d = by["01-decompose"]
    check("01-decompose 产出子任务", d["status"] == "ok" and len(d["outputs"]["subtasks"]) >= 1,
          f"{len(d['outputs']['subtasks'])} 个 / {d['outputs']['decomposition_path']}")
    check("01-decompose 依赖索引合法", all(x < s["index"] for s in d["outputs"]["subtasks"] for x in s["dependencies"]))
    r = by["02-retrieve"]
    check("02-retrieve 每子任务都有分配", r["status"] == "ok"
          and len(r["outputs"]["assignments"]) == len(d["outputs"]["subtasks"]))
    check("02-retrieve 技能名来自 catalog",
          all(a["skill_name"] == "" or any(a["skill_name"] == e["name"] for e in inp["skill_catalog"])
              for a in r["outputs"]["assignments"]))
    w = by["03-weave"]
    dag = w["outputs"]["dag"]
    check("03-weave DAG 有效无环", dag["is_valid"] and not dag["has_cycles"],
          f"nodes={dag['node_count']} edges={dag['edge_count']}")
    check("03-weave 拓扑序覆盖全部节点", sorted(dag["execution_order"]) == list(range(dag["node_count"])),
          str(dag["execution_order"]))
    a = by["04-assemble"]
    plan = a["outputs"]["plan"]
    check("04-assemble 单元数 == 节点数", plan["total_units"] == dag["node_count"],
          f"{plan['total_units']}")
    check("04-assemble 阶段覆盖全部单元",
          sorted(u for v in plan["stages"].values() for u in v) == sorted(u["unit_id"] for u in plan["units"]),
          f"{len(plan['stages'])} 阶段")
    e = by["05-execute"]
    check("05-execute 输出 needs_executor（不假装已执行）",
          e["status"] == "needs_llm" and e["outputs"] == {} and e.get("needs") == "executor",
          f"status={e['status']} needs={e.get('needs')}")
    check("05-execute 契约字段齐备", all(k in e for k in ("stage", "role", "prompt_file", "input")))
    check("05-execute 声明宿主需求", "host_requirements" in e["input"], "host_requirements")
    check("overall_status == blocked（硬前置：真实执行通道缺失）",
          manifest["overall_status"] == "blocked", manifest["overall_status"])

    # ── B1/B2 必补回归（真实调用 CLI，覆盖非法 JSON / 不存在 run / status 纯度） ──
    def _is_json(s):
        try:
            json.loads(s)
            return True
        except Exception:
            return False

    def _cli(extra):
        r = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                            "--workspace", str(ws)] + extra,
                           capture_output=True, text=True)
        return r.returncode, r.stdout, r.stderr

    bad = ws / "selftest-bad.json"
    bad.write_text('{ this is not json ', encoding="utf-8")
    rc_b, out_b, err_b = _cli(["run", "--input", str(bad), "--run-id", "selftest-bad"])
    check("回归① 非法 JSON → rc=2 且 stdout 为合法 JSON",
          rc_b == 2 and _is_json(out_b) and "Traceback" not in err_b, f"rc={rc_b}")
    rc_n, out_n, err_n = _cli(["status", "--run-id", "no-such-run-xyz"])
    check("回归② 不存在的 run_id → rc=2 且无 traceback",
          rc_n == 2 and _is_json(out_n) and "Traceback" not in err_n, f"rc={rc_n}")
    rc_s, out_s, err_s = _cli(["status", "--run-id", "selftest"])
    check("回归③ status 默认输出可被 json.loads 解析", rc_s == 0 and _is_json(out_s), f"rc={rc_s}")

    print(f"\n   计划：{plan['total_units']} 单元 / {len(plan['stages'])} 阶段；执行被阻塞于 05-execute")
    print(f"\n{'✅ selftest 全部通过' if fails == 0 else f'❌ selftest 失败 {fails} 项'}  (产物: {indir})")
    return 0 if fails == 0 else 1


def build_parser():
    p = argparse.ArgumentParser(prog="swarmflow.py", description=f"{SWARM_NAME} · {SWARM_TITLE}")
    p.add_argument("--workspace", help="工作区（优先于 QIQI_WORKSPACE 与当前目录）")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan", help="打印 DAG 与各阶段角色/输入/输出")
    r = sub.add_parser("run", help="执行流水线")
    r.add_argument("--input", required=True)
    r.add_argument("--run-id")
    s = sub.add_parser("status", help="查看某次 run 的状态")
    s.add_argument("--run-id", required=True)
    s.add_argument("--format", choices=["json", "text"], default="json",
                   help="输出格式：json（默认，纯 JSON，可被 json.loads 解析）| text（JSON + 人类可读摘要）")
    sub.add_parser("selftest", help="用示例输入跑通全部阶段")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.cmd == "plan":
        cmd_plan(args)
        return 0
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "selftest":
        return cmd_selftest(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
