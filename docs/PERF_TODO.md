# 性能优化待办清单（PERF）

> 记录回测/寻优链路的性能问题与优化项。状态标记：✅ 已落地 / ⏳ 待办。
> 背景实测基线：200 只 · 分钟线 · 20 个月 · momentum\_slot 单次回测 ≈ 4\~7 分钟；
> 寻优单 trial ≈ 2.3 分钟（125 trial 全程 ≈ 4.8 小时）。3 年区间数据量 ×1.8（线性外推）。

## 一、内存与主循环（P0，最优先）

### ✅ P0-1 bar dict 白名单物化（已落地 2026-09-xx，commit 3d2bfca）

- 内容：`runner.py` `_simulate` 物化 bars 前按白名单选列——主循环/撮合/风控只访问 16 个协议列（`date/OHLC/volume/adj_factor/signal/tag/reason/budget_pct/t_ratio/reduce_pct/atr_pct/d_atr/atr`）+ 动态规则（`atr{N}` 数字列、`adaptive_*` 前缀）；`dif/dea/ma/slope/bias/score` 等特征列在策略信号层已消费完，不再进 dict。

- 效果：bar dict 键数约减半 → 单 trial 内存峰值与 `to_dicts()` 时间同步减半。

### ✅ P0-1b 白名单维护机制（已落地：BAR_KEEP_COLS 常量 + 静态扫描守卫测试）
- 实现：白名单上移为 `runner.py` 模块级常量 `BAR_KEEP_COLS` + 动态规则 `_BAR_KEEP_DYNAMIC`（`atr{N}` 数字列 / `adaptive_` 前缀）+ 判定函数 `_bar_col_allowed()`；
- 守卫：`tests/test_bar_whitelist.py` 静态扫描 broker/risk/runner 源码中全部 `bar.get("k")` / `bar["k"]` 字面量访问，断言被白名单覆盖——引擎新增读取字段未登记时测试显式红（消灭 .get 静默 None）；
- 反向守卫：断言白名单无"从未被读取"的死键，防长期腐化；
- 边界说明：动态 f-string 键（如 `f"atr{N}"`）静态扫描无法捕获，须命中动态规则；新增动态形态时同步扩充 `_BAR_KEEP_DYNAMIC`。

### ✅ P0-3 寻优 trial 分批子进程（已落地：BATCH_TRIALS=5 + BrokenProcessPool 减半重试）
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

- ⏳ **数据加载并行**：`datafeed.load_minute5/load_daily` 逐票串行读 parquet + 复权 join；3 年数据量 ×1.8。可并行分片或 lazy scan 下推，省 30\~50% 加载时间。

- ⏳ **报告分页/按需**：做T频繁时 trade\_log 数万条、报告 JSON 数十 MB——写盘慢、详情页打开卡。需后端按需分页接口 + 前端虚拟滚动。

- ⏳ **寻优 trial 并行**：当前 trial 内串行（探针剪枝 3 只 + 全量 1 次）。优化 P0-1/P0-3 后可评估多 worker 并行 trial。

- ⏳ **`_split_date`** **/ 首 trial 重复加载**：寻优开头同一批分钟线被加载多次；可复用特征缓存。

## 四、三年回测可行性结论（供排期参考）

| 场景                       | 现状                     | P0 全落地后预估           |
| ------------------------ | ---------------------- | ------------------- |
| 3 年 · 日线 · 中小池           | ✅ 分钟级                  | ✅ 分钟级               |
| 3 年 · 分钟线 · 200 只 · 单次   | ✅ \~9 分钟，内存 7-11GB     | ✅ \~5 分钟内，内存减半      |
| 3 年 · 分钟线 · 寻优 125 trial | ⚠️ \~9 小时挂机，单进程 OOM 风险 | ✅ 过夜可跑，trial 分批进程稳定 |
| 3 年 · 分钟线 · 寻优 + 并行其它回测  | ❌ 内存互挤                 | ✅ 分离进程池后缓解          |

