# 参数寻优化与 AI 分析升级方案

> 状态：方案 A 已实现；方案 B Phase 1 已实现（2026-09-04：诊断引擎+数据加厚+验证闭环+实盘AI简报点评），Phase 2/3 待实现；下钻工具已实现（2026-09-04：query_trades / get_code_profile / get_market_context 只读取证 + 预算护栏 + 不支持 tools 的端点自动降级，见 llm/drilldown.py）
> 日期：2026-08-26（2026-09-04 更新）
> 背景：基于真实回测/寻优数据暴露的问题，提出两个方向的系统性升级方案

---

## 0. 现状盘点（问题证据）

### 0.1 参数寻优现状

以寻优任务 `opt_548fe930e083`（测试三只-寻优）为案例：

| 项 | 值 | 问题 |
|---|---|---|
| 搜索空间 | 仅 1 个参数（atr_period） | momentum_t 共 39 个参数，平铺搜索等于盲搜 |
| 目标函数 | 样本内（前70%）单窗口裸年化收益 | 必然过拟合 |
| 样本内年化 | +1758%（17.58） | "记住答案" |
| 样本外年化 | -80.7% | 崩塌 |
| overfit_risk | high | 50 次计算全部浪费 |

以最新回测任务 `bt_26190e0b01c7`（测试十个3）为案例：

- universe 10 只票、36 个默认参数、minute5 周期
- 总收益 **-23%**、夏普 -1.88、最大回撤 -31.8%
- 结论：默认参数在多票池上亏损——正是寻优该介入的场景

### 0.2 AI 分析现状

当前实现（`backend/app/llm/analyzer.py`）：单轮 LLM 读取报告摘要 JSON → 输出 markdown + 结构化参数建议。

三个根本缺陷：

1. **建议从未被验证**——LLM 在"猜"
2. **没有记忆**——上次分析学到的教训这次全忘
3. **喂的数据太薄**——只有指标和 10 笔采样交易

---

## 1. 方案 A：分层寻优 + 多窗口稳健目标

### A1. 核心设计

#### 1) param_space 分组协议（向后兼容）

```jsonc
// 新格式：分组
{
  "objective": {                       // 缺省时退化为旧行为
    "metric": "calmar",                // calmar | sharpe | annual_return
    "n_windows": 3,                    // 样本内切 3 个子窗口
    "variance_penalty": 0.5,           // λ：跨窗口方差惩罚
    "dd_floor": -0.40                  // 任一窗口回撤击穿 -> 该 trial 重罚
  },
  "rounds": 2,                         // 坐标轮换轮数上限
  "groups": [
    { "name": "趋势层", "n_trials": 40, "params": {
        "trend_ma":  {"type":"int","low":30,"high":90,"step":5},
        "slope_n":   {"type":"int","low":3,"high":8} } },
    { "name": "仓位与选股", "n_trials": 40, "params": {
        "top_n": {"type":"int","low":2,"high":5},
        "base_pct_max": {"type":"float","low":40,"high":90,"step":5} } },
    { "name": "加仓与过热", "n_trials": 30, "params": {
        "max_adds": {"type":"int","low":0,"high":3},
        "add_breakout_n": {"type":"int","low":10,"high":40},
        "overheat_k": {"type":"float","low":2,"high":4,"step":0.5} } },
    { "name": "做T网格", "n_trials": 40, "params": {
        "grid_atr_mult": {"type":"float","low":0.3,"high":1.0,"step":0.1},
        "t_ratio_base": {"type":"float","low":15,"high":40,"step":5},
        "max_t_times": {"type":"int","low":2,"high":6} } },
    { "name": "风控", "n_trials": 30, "params": {
        "atr_multiplier": {"type":"float","low":1.0,"high":2.5,"step":0.25},
        "trailing_stop_pct": {"type":"float","low":3,"high":8,"step":1} } }
  ]
}
// 旧格式（平铺 dict）-> 自动包装为单组 + 单窗口，行为不变
```

#### 2) 分层坐标轮换主流程

```
best_params = 默认参数（来自 apply_param_defaults）
for round in 1..rounds:
    improved = False
    for group in groups:                      # 每组一个独立 Optuna study
        其它组参数固定在 best_params，只搜本组
        study = f"{task_id}__g{组号}__r{轮次}"   # 同一 .db 多 study，可断点续跑
        if 组内最优 > 当前最优 + ε: 更新 best_params，improved = True
    if not improved: break                    # 收敛提前退出
```

- 采样器固定 `TPESampler(seed=42)`，同配置可复现
- 组间结果通过 `_merged_config` 注入（现有函数直接复用，risk keys 自动落位 risk_config）
- ε 阈值（建议 0.5% 相对提升）防止组间震荡

#### 3) 多窗口目标：一次回测 + 权益曲线切窗（关键降本设计）

每个 trial 仍然**只跑 1 次完整样本内回测**（与现在成本相同），然后从 `equity_curve` 的 `adjusted_equity` 切成 n 段计算每窗指标：

```
每窗: w_ret / w_sharpe(日收益年化) / w_maxdd / w_calmar
score = mean(w_metric) − λ × std(w_metric) − 大惩罚(任一窗 w_maxdd < dd_floor)
```

- momentum_t 是路径依赖状态机，切窗必须整段连续回测（不能各窗独立跑，否则持仓状态丢失）——单次回测 + 曲线切窗天然满足
- λ 惩罚"只在某一段行情有效"的参数，直接针对 17.6 → -0.81 型崩塌
- 窗口自适应：`n_windows = max(1, min(请求值, 样本内交易日 // 30))`，样本太短自动降级并在报告中注明

#### 4) 样本外验证（保持无泄漏）

维持现有 70/30 结构不动：前 70% 做寻优（内含多窗口），后 30% 只验证不参与选择。`oos_validation` 增加**逐窗 OOS 指标**，overfit 分级逻辑保留。

### A2. 契约与文件改动清单

| 位置 | 改动 |
|---|---|
| `backend/app/optimizer.py` | 主体重写：新增 `_window_metrics(equity_curve, windows)`；`run_optimize` 增加分组轮换外层循环；报告新增 `groups_schedule / per_group_best / rounds_history / window_scores / objective` |
| `backend/app/api/optimize.py` | `OptimizeRequest` 增加 `objective`/`groups`/`rounds` 字段与校验；总预算护栏（`Σ(组trials×轮次) ≤ 2000`，超限报 400）；`VALID_METRICS` 不变 |
| `backend/app/task_manager.py` | `optimize_task` 透传新参数（签名过滤机制已有，改动极小） |
| 前端 OptimizeList 建表单 | 分组编辑器 + momentum_t 预设分层模板（上表 5 组一键填充）+ objective 高级配置折叠面板 |
| 前端 OptimizeDetail | 运行中进度显示"轮次 x/y · 组 i/5 · trial k/n"；结果页展示 rounds_history 折线、每窗分数、per_group_best |
| 新增测试 | 平铺兼容、窗口指标计算正确性、单窗暴涨+其余暴跌的参数得分应低于平稳参数、预算护栏 |

### A3. 运行预算与风险

| 项 | 估算 |
|---|---|
| 全量（5组×40×2轮） | ~400 次回测；minute5+10票 ≈ 30-60s/次 → **3~7 小时** |
| 快速模式（3组×15×1轮） | ~45 次 → 30~45 分钟，建议先用它验证管线 |
| 断点续跑 | Optuna `load_if_exists` + study 命名规则已覆盖，进程中断可重提同 task_id |

风险点：

1. 前 3 只票探针剪枝在分组模式下继续生效（保留）
2. 若某组参数在轮次间震荡，ε 阈值防抖
3. 分层找到的是局部最优——这是坐标轮换的固有代价，换来的是可行预算，值得

---

## 2. 方案 B：AI 诊断引擎 + 自动验证闭环

### B1. 架构

```
回测报告 ──> 诊断引擎（纯规则，零幻觉）──> findings JSON ──┐
                                                        ├──> LLM（只做解读+开方）──> 建议
参数寻优重要性 ─────────────────────────────────────────┘        │
                                                                  ▼
                                              自动验证回测（同 worker 进程内跑）
                                                                  │
                                                                  ▼
                                            A/B 对比表 + 改善/持平/恶化 结论
                                                                  │（可选 Phase 2）
                                                                  ▼
                                              二轮 LLM：读实测结果修正建议
```

关键认知：**LLM 从"分析师"降级为"医生"**——体检数值（诊断）由机器出，医生只负责解读和开处方。幻觉问题从根上消除。

### B2. 诊断引擎（新模块 `backend/app/llm/diagnostics.py`）

纯函数 `diagnose(report, param_importance=None) -> list[Finding]`，无 LLM 依赖，可单测。规则清单（数据全部来自现有报告字段）：

| 规则 code | 触发条件（示例阈值） | 建议方向 hint |
|---|---|---|
| `T_NEG_PNL` | `t_pnl < 0` 且 `t_trade_count ≥ 5` | 网格过窄/费用侵蚀：grid_atr_mult ↑ |
| `T_SELL_FLY` | 做T 卖出后买回价高于卖价的比例 > 50%（trade_log 配对可算） | 卖出阈值放宽：asym_bias ↑ |
| `STOP_TOO_TIGHT` | 止损后 N 日价格反弹超止损幅的比例 > 60%（需 K 线数据，或用 trade_log pnl 代理） | atr_multiplier ↑ |
| `LOW_PROFIT_RATIO` | 胜率 > 50% 但盈亏比 < 1.2 | 止盈过早：trailing_stop 放宽 |
| `ADD_DRAG` | `add_pnl < 0` 且加仓笔数 > 开仓笔数一半 | add_scale ↓（递减更快） |
| `CONCENTRATION` | 单票盈亏占比 > 60%（trade_log 按 code 聚合） | top_n ↑ 或分散 |
| `DEEP_DD` | `max_drawdown < -25%` | 风控收紧 |
| `IDLE_CAPITAL` | equity_curve 的 position_ratio 均值 < 20% | base_pct_max ↑（选股信号不足或门槛过高） |
| `LONG_FLAT` | position_snapshots 连续空仓 > 60 交易日 | 动量门槛/趋势参数 |
| `WD_SHORTFALL` | withdrawal.shortfall 月占比 > 40%（仅出金开启时） | 收益不足以支撑出目标 |
| `OVERFIT_WARN` | param_importance 最高项 > 0.6（若传入了寻优结果） | 该参数大概率记住行情 |

每条 Finding 结构：

```jsonc
{
  "code": "T_NEG_PNL", "severity": "high",
  "title": "做T总贡献为负",
  "evidence": "T交易 42 笔，T盈亏 -3,821 元，占手续费 12%",
  "hint": "网格阈值未能覆盖往返成本（约0.07%+滑点）",
  "suggest_params": {"grid_atr_mult": 0.8}   // 供 LLM 参考的先验方向
}
```

### B3. LLM 角色重定义（改 `backend/app/llm/analyzer.py`）

1. **Prompt 重构**：输入 = findings + 策略 param_schema（含 min/max/label）+ 原报告摘要。SYSTEM_PROMPT 明确："诊断已由系统给出，你只能解读这些发现并开方，**禁止发明 findings 之外的问题**；每条建议必须引用 finding code"
2. **幻觉护栏（代码层，不靠 LLM 自觉）**：`_extract_suggestions` 增强——
   - 建议值必须落在 param_schema 的 min/max 内，越界直接 clamp 或丢弃
   - 建议 item 携带 `finding_code` 引用；引用了不存在 code 的建议直接丢弃
3. findings 作为结构化字段随 analysis 一起入库，前端渲染成诊断卡片（不是埋在 markdown 里）

### B4. 自动验证闭环（改 `backend/app/task_manager.py` 的 `ai_analyze_task`）

**最简实现：不引入任务链，验证回测在同一个 ai worker 子进程内顺序执行。**

```
ai_analyze_task:
  1. 读报告 -> diagnose() -> findings
  2. LLM 分析（现有 chat）
  3. 若 suggestions 非空:
     a. 建议合并进原 config（复用 runner 的 params/risk 合并逻辑，
        后端补一个共享 merge 函数，前端"应用到下一轮回测"以后也走它）
     b. 同区间同 universe 跑 runner.run_backtest（进程内，datafeed 缓存还在）
     c. 对比原报告: total_return / sharpe / max_drawdown / total_trades / t_pnl
     d. verdict: 改善(≥2项指标变好且无一恶化超阈值) / 持平 / 恶化
  4. save_analysis(..., validation=<对比JSON>)
```

- 进度消息分四段：读报告 → 诊断 → LLM → 验证回测 → 完成
- 建议为空则跳过验证（不浪费算力）；验证回测抛异常时 analysis 仍标记 success，validation 字段记录 error（AI 不应为回测失败背锅）
- DB 迁移：`ai_analyses` 加一列 `validation TEXT`（沿用现有 `_migrate` 幂等模式）

### B5. 契约与前端

| 位置 | 改动 |
|---|---|
| `backend/app/db.py` | ai_analyses 加 `validation`、`diagnostics` 列 + list 时解析返回 |
| `backend/app/api/ai.py` | 接口签名不变，返回体自然多两个字段（前端非破坏性升级） |
| 前端 AiAnalysis.tsx | ① 诊断卡片列表（severity 颜色 Tag + evidence + 建议方向）；② 建议卡片下挂"实测验证"A/B 表 + verdict 标签（绿/灰/红） |
| 后端共享工具 | `merge_suggestions(config, suggestions)`（前后端共用一份逻辑） |
| 测试 | 规则引擎单测（合成报告逐条触发）；LLM monkeypatch 后的闭环集成测试；越界建议被 clamp/丢弃 |

### B6. 分期

| 期 | 内容 | 价值 |
|---|---|---|
| Phase 1 ✅（2026-09-04） | 诊断引擎 diagnostics.py + LLM 解读角色重构 + 建议 clamp + 进程内验证回测 + A/B verdict + 二轮点评 + 前端诊断/验证卡片 + 胜率统计（/api/ai/suggestion-stats）+ 数据加厚（市场环境/交易统计深化/参数表） | 建议可证伪、幻觉清零 |
| Phase 2 | 二轮修正：验证结果喂回 LLM 出修正建议（`POST /ai/analyses/{id}/refine`）——目前只做了一轮点评（validation.commentary），修正建议待实现 | 单步 agentic loop |
| Phase 3 | 敏感度扫描（±20% 网格实测表喂 prompt）+ embedding 实验记忆库 | 中期 |

---

## 3. 两个方案的交汇点

- 寻优的 `param_importance` 现在只喂给 AI 一次（`_latest_param_importance`）；方案 B 的 `OVERFIT_WARN` 规则把它变成诊断信号，方案 A 的产出直接被方案 B 消费
- 方案 B 验证回测用的就是方案 A 优化的引擎管线，两者共享 `runner.run_backtest`，无重复建设

## 4. 实施顺序建议

| 优先级 | 事项 | 理由 |
|---|---|---|
| P0 | 方案 B Phase 1（半天量级） | 立刻看到"AI 建议实测是亏是赚"，建议可证伪 |
| P0 | 方案 A 快速模式（3组×15×1轮） | 先验证管线，再放开全量跑过夜 |
| P1 | 方案 A 全量 + 前端分组编辑器 | 完整能力 |
| P2 | 方案 B Phase 2（二轮修正） | agentic loop |
| P3 | 敏感度扫描 / embedding 记忆库 | 中期投入 |

## 5. 案例验收标准

以 `bt_26190e0b01c7`（测试十个3，当前 -23%）为验收对象：

1. **寻优**：用 5 组分层空间 + calmar + 3 窗口发起寻优，验收标准：
   - `oos_validation.overfit_risk ∈ {low, medium}`（不允许 high）
   - best_params 在完整区间回测，总收益显著优于 -23% 基线
   - `param_importance` 无单参数 > 0.6 的"记住行情"信号
2. **AI 分析**：对该报告发起 AI 分析，验收标准：
   - 诊断卡片 ≥ 3 条且 evidence 均可从报告数据复核
   - 建议自动跑验证回测，前端可见 A/B 对比表与 verdict
   - 建议参数全部落在 param_schema 合法区间内
