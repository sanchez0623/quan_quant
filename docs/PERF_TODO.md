# 性能优化待办清单（PERF）

> 记录回测/寻优链路的性能问题与优化项。状态标记：✅ 已落地 / ⏳ 待办。
> 背景实测基线：200 只 · 分钟线 · 20 个月 · momentum\_slot 单次回测 ≈ 4\~7 分钟；
> 寻优单 trial ≈ 2.3 分钟（125 trial 全程 ≈ 4.8 小时）。3 年区间数据量 ×1.8（线性外推）。

## 一、内存与主循环（P0，最优先）

### ✅ P0-1 bar dict 白名单物化（已落地 2026-09-xx，commit 3d2bfca）

- 内容：`runner.py` `_simulate` 物化 bars 前按白名单选列——主循环/撮合/风控只访问 16 个协议列（`date/OHLC/volume/adj_factor/signal/tag/reason/budget_pct/t_ratio/reduce_pct/atr_pct/d_atr/atr`）+ 动态规则（`atr{N}` 数字列、`adaptive_*` 前缀）；`dif/dea/ma/slope/bias/score` 等特征列在策略信号层已消费完，不再进 dict。

- 效果：bar dict 键数约减半 → 单 trial 内存峰值与 `to_dicts()` 时间同步减半。

### ✅ P0-1b 白名单维护机制（已落地：BAR\_KEEP\_COLS 常量 + 静态扫描守卫测试）

- 实现：白名单上移为 `runner.py` 模块级常量 `BAR_KEEP_COLS` + 动态规则 `_BAR_KEEP_DYNAMIC`（`atr{N}` 数字列 / `adaptive_` 前缀）+ 判定函数 `_bar_col_allowed()`；

- 守卫：`tests/test_bar_whitelist.py` 静态扫描 broker/risk/runner 源码中全部 `bar.get("k")` / `bar["k"]` 字面量访问，断言被白名单覆盖——引擎新增读取字段未登记时测试显式红（消灭 .get 静默 None）；

- 反向守卫：断言白名单无"从未被读取"的死键，防长期腐化；

- 边界说明：动态 f-string 键（如 `f"atr{N}"`）静态扫描无法捕获，须命中动态规则；新增动态形态时同步扩充 `_BAR_KEEP_DYNAMIC`。

### ✅ P0-3 寻优 trial 分批子进程（已落地：BATCH\_TRIALS=5 + BrokenProcessPool 减半重试）

- 实现：`optimizer._optuna_batch_worker` 子进程入口——载入既有 study（SQLite storage，study 名 `task_id__g{gi}__r{rnd}`），连续执行 ≤5 个 trial 后进程退出；主流程每批用 `ProcessPoolExecutor(max_workers=1, max_tasks_per_child=1)` 独占全新进程，批结束 OS 彻底回收内存；

- 行为同源：默认 TPE sampler（历史驱动）/ MedianPruner / 探针剪枝 / 窗口评分全部在 worker 内复刻；TPE 建议从 storage 读取历史，跨进程行为一致；

- 崩溃自愈：批内子进程被系统杀死（BrokenProcessPool）→ 批大小减半重试，已完成 trial 已持久化、只补剩余；减到 1 仍失败 → 明确报错"疑似内存不足"；

- 进度：worker 按同一公式直写 SQLite（子进程与任务库解耦）；

- 语义改进：单 trial 回测异常按 FAIL 记账并继续（`catch=(Exception,)`），不再放大为整个寻优任务失败；组内无 COMPLETE trial 时按 -9e9 跳过（原 `study.best_trial` 会直接崩）。

## 二、策略层循环（P0）

### ✅ P0-2 momentum\_slot `_enforce_slots` 索引化（先前迭代已落地）

- 现状：预建 `day → [(code_idx, bar_idx)]` 单次归组（O(总bar数)），旧 O(天数×股票数×全量bar) ≈ 30 亿次比较已消除。

- 后续新增策略若出现「按日×按票×全量bar」型循环，参照此模式索引化。

## 三、数据层与展示（P1/P2）

- ✅ **数据加载并行（P1-1，已落地）**：`datafeed` 逐票读取/复权 join 改线程池并行（`_pmap`，worker 数 = 4\~8 按 CPU 自适应；polars 读取/变换释放 GIL）；LRU 缓存加 `_CACHE_LOCK` 保证多线程安全（未命中在锁外加载，并发重复加载幂等可接受）。

- ✅ **报告传输（P1-2，已落地）**：`main.py` 挂 `GZipMiddleware(minimum_size=1024)`——报告 JSON（含数万条 trade\_log）传输体积降约 90%，浏览器自动解压前端零改动；前端大表本就有客户端分页（TradeLogTab 50/页、PositionTab 30/页），无 DOM 爆炸问题。全量虚拟滚动/服务端分页留待 3 年规模实测出现真实痛点再做。

- ✅ **寻优 trial 并行（P1-3，已落地）**：env `OPTIMIZE_PARALLEL_TRIALS`（默认 1=串行批处理）控制波次并行度——每组 trial 由 k 个子进程波次并发执行（组内 trial 相互独立、同一 best\_params 基线，TPE 从 storage 读历史，跨进程语义安全）；SQLite 写超时加固至 60s（`_make_storage`，防并发写 database is locked 把 trial 误记 FAIL）；波次内 worker 被杀 → 先降并行度再减批，全部 trial 持久化只补剩余。**注意：并行度 × 单trial内存峰值（200只分钟线约 2GB）需留足物理内存。**

- ✅ **`_split_date`** **重复加载（P1-4，已落地）**：交易日历改由日线派生（单 parquet 秒级），不再为取日期集合全量加载 200 只分钟线（旧实现寻优开头空耗 1\~2 分钟）。日线与分钟线同源自更新管道，交易日集合一致；极端停牌数据差异可能导致分割日偏移一天，仅影响单次寻优的样本内边界，可接受。

## 四、三年回测可行性结论（供排期参考）

| 场景                       | 现状                  | P0/P1 全落地后预估         |
| ------------------------ | ------------------- | -------------------- |
| 3 年 · 日线 · 中小池           | ✅ 分钟级               | ✅ 分钟级                |
| 3 年 · 分钟线 · 200 只 · 单次   | ✅ \~9 分钟，内存 7-11GB  | ✅ \~5 分钟内，内存减半       |
| 3 年 · 分钟线 · 寻优 125 trial | ✅ 过夜可跑（P0-3 分批进程稳定） | ✅ 并行度 2-3 时约 3\~5 小时 |
| 3 年 · 分钟线 · 寻优 + 并行其它回测  | ✅ 分离进程池后缓解          | ✅                    |

