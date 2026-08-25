# 后端开发规范

必读：先完整阅读 `docs/API_CONTRACT.md`（接口契约，一字不差地实现）与本文档。设计文档为《A股个人量化回测系统-完整实现方案.docx》。

## 目标

在 `backend/` 目录下实现 FastAPI 后端：数据层（Parquet+SQLite）、自研事件驱动回测引擎（日线+5分钟线、分层持仓、T+1、涨跌停、做T、风控）、Optuna 寻优、多 LLM 分析、JWT 认证、WebSocket 进度推送、进程池异步任务。

## 技术栈与依赖（requirements.txt）

```
fastapi>=0.110
uvicorn[standard]>=0.29
pydantic>=2.6
polars>=0.20
pandas>=2.2
pyarrow>=15
optuna>=3.5
httpx>=0.27
PyYAML>=6
PyJWT>=2.8
APScheduler>=3.10
```
说明：
- 密码哈希用标准库 `hashlib.pbkdf2_hmac`，不用 bcrypt（避免Windows编译问题）。
- 数据源库（baostock/akshare/mootdx/efinance）为**可选依赖**，代码里 `try: import` 失败则该源标记不可用，系统照常运行（演示用合成数据）。不写入 requirements.txt，另建 `requirements-sources.txt` 注释说明。
- 不用 duckdb 做核心路径（polars 直读 parquet 已够），避免依赖问题；如需 SQL 查询可用 polars scan。

## 目录结构（严格遵守）

```
backend/
  requirements.txt
  requirements-sources.txt
  run.py                      # 入口: uvicorn app.main:app --host 0.0.0.0 --port 8000
  app/
    __init__.py
    main.py                   # FastAPI 实例、CORS、路由挂载、静态托管、启动初始化
    config.py                 # pydantic-settings 或 os.getenv 读取 env（见下）；路径常量
    db.py                     # SQLite (data/meta.db) 元数据：users/tasks/backtest_reports/ai_analyses/llm_usage；线程安全（check_same_thread=False + 锁 或每次新连接）
    auth.py                   # JWT 签发/校验、密码哈希、登录依赖 get_current_user
    task_manager.py           # 进程池任务执行 + 进度上报 + WS 广播
    api/
      __init__.py
      auth.py  strategies.py  stocks.py  backtests.py  optimize.py  ai.py  data.py
    engine/
      __init__.py
      datafeed.py             # Parquet 读取、后复权计算、LRU缓存
      indicators.py           # polars 指标：MA/EMA/MACD/ATR/BOLL
      broker.py               # 撮合：T+1、涨跌停、滑点、手续费
      portfolio.py            # 分层持仓 Position、账户
      risk.py                 # 风控（与策略参数分离）
      stats.py                # 绩效统计（做T/加减仓贡献分解、月度收益）
      runner.py               # 回测主流程：向量化信号→逐bar撮合
      strategies/
        __init__.py           # 策略注册表 REGISTRY
        ma_cross.py           # 双均线策略
        grid_t.py             # 网格做T策略
    data/
      __init__.py
      store.py                # Parquet 读写（daily.parquet/minute5/{code}.parquet/adj_factor.parquet/trade_calendar.parquet/stock_basic.parquet）
      sources.py              # DataSource 抽象 + baostock/akshare/mootdx 适配器（可选导入）+ 健康检查/降级
      updater.py              # 增量更新服务（含校验）
      synthetic.py            # 合成演示数据生成
    llm/
      __init__.py
      provider.py             # OpenAI 兼容多 Provider、fallback chain、用量记录
      analyzer.py             # 报告→prompt→分析结果(markdown)
    optimizer.py              # Optuna 寻优封装（SQLite study、样本内外70/30、参数重要性）
  tests/
    test_engine.py            # 引擎单元测试（见"测试要求"）
    test_api.py               # API 冒烟测试
```

## 配置（config.py）

环境变量（从 `.env` 加载，python-dotenv 或手写解析，注意项目根的 .env 不存在时用默认值）：
- `JWT_SECRET`（默认随机生成并打印警告）
- `ADMIN_PASSWORD`（默认 `admin123`）
- `DATA_DIR`（默认 `<项目根>/data`）
- `CONFIG_DIR`（默认 `<项目根>/config`）
- `DATA_START_DATE`（合成数据用，默认 5 年前）
- LLM 各 key：`SILICONFLOW_API_KEY/DEEPSEEK_API_KEY/ZHIPU_API_KEY/OLLAMA_API_KEY`（读配置文件的 api_key_env 指向的名字）

LLM 配置文件 `config/llm.yaml` 不存在时读 `config.example/llm.yaml`；都没有则用内置默认（见契约 profiles 示例 + cheap=ollama）。解析 yaml，结构见设计文档 6.1。

## 数据层

存储结构（DATA_DIR 下）：
- `daily.parquet`: 列 `code(str),date(str YYYY-MM-DD),open,high,low,close,volume,amount`（不复权原始价）
- `minute5/{code}.parquet`: 列 `code,date(str YYYY-MM-DD HH:mm),open,high,low,close,volume,amount`
- `adj_factor.parquet`: 列 `code,date,adj_factor(float)`（后复权因子，cumulative）
- `trade_calendar.parquet`: 列 `date, is_open(int)`
- `stock_basic.parquet`: 列 `code,name,st(bool),list_date`
- `meta.db`: SQLite

后复权：`hfq_price = raw_price * adj_factor / latest_adj_factor_of_that_backtest_window`？——不。正确做法（设计文档4.2）：存储原始价+因子，回测用后复权价 = `raw * adj_factor`（因子本身已归一化到最早日=1，即 adj_factor 为累计因子）。合成数据因子恒为 1.0。读取时按股票 merge 因子列即可。

datafeed：`load_daily(codes, start, end) -> dict[code, DataFrame]`（polars，含后复权 open/high/low/close/volume 与原始 close 供展示换算）；minute5 同理。LRU 缓存（functools.lru_cache 或 dict，key=(period, code, start, end)）。

synthetic.py：生成 N 只股票（默认 `600000 浦发银行/000001 平安银行/600036 招商银行/000858 五粮液/601318 中国平安`，可传股票列表；若传未知代码则命名为"演示股XXXX"）：
- 日线：`days` 个交易日（默认500，从今天往回推交易日），几何随机游走 + 缓慢趋势 + 偶发跳空，volume 对数正态；同步生成交易日历、复权因子（全1）、stock_basic。
- minute5：每交易日 48 根（9:35~15:00 每5分钟，含 11:30/13:00 边界处理：9:35-11:30 与 13:05-15:00 各 24 根），由日线收盘价插值+噪声生成。**注意 minute5 可选按股票生成，默认全部合成**。
- 幂等覆盖写入，返回统计信息。

updater.py：实现"框架完整、可选源可用时才真正拉数"：
- `DataSource` 抽象基类：`name, available(), health_check(timeout=10)->bool, get_daily(code, start, end), get_minute5(...), get_adj_factor(code)`。
- BaostockSource/AkshareSource/MootdxSource 适配器（可选 import，unavailable 时返回 False/None）。**所有 httpx/requests session 设 `trust_env=False`**（设计文档 4.6）。
- `update(scope)`: 健康检查→主备降级→拉数→校验（K线数 vs 日历、异常价检测）→写 parquet。无可用源时抛出带说明的错误（提示用 POST /api/data/demo 生成演示数据）。
- 记录更新水位到 meta.db。

## 回测引擎（核心，务必正确）

### Position 模型
```python
@dataclass
class Position:
    code: str
    volume: int
    cost_price: float          # 后复权成本
    open_time: str             # 开仓bar时间
    sellable_date: str | None  # T+1: 下一交易日(日级)或当日收盘后(分钟级按日判断)
    group_id: int              # 建仓组：同组开仓+后续加仓；平仓时归属分解用
    tag: str                   # 开仓类型标签: 开仓/加仓/做T
    highest_price: float       # 移动止损用
    open_fee: float
```

### Broker 撮合规则（A股真实约束）
- 买入：信号 bar 的**下一根 bar 开盘价**成交（避免未来函数），加滑点（买价=开*(1+slippage_pct)）；若下一bar开盘即涨停（open >= 前close*1.095 近似，简化：本bar high==low 且 close>open 跳过）——简化实现：**用下一bar开盘成交，若该bar 一字涨停（high==low 且 close>prev_close）则不成交**；资金不足按可用资金缩量（100股整数倍）。
- 卖出：同样下一bar开盘价*(1-slippage_pct)；**T+1**：只能卖 `sellable_date <= 当前日期` 的仓位（日线：sellable_date=开仓日的下一交易日；分钟线：开仓当日之后的下一交易日——即分钟级当日买入不可卖）；**跌停不成交**（该bar high==low 且 close<prev_close）；持仓按 FIFO 平。
- 手续费：佣金 `max(amount*commission_rate, commission_min)`（双向）+ 印花税 `amount*stamp_tax`（仅卖出）+ 过户费 `amount*transfer_fee`（双向）。
- 做T（"卖旧买新"）：先卖底仓（可用仓位）再当日买回，新 Position 标记 tag=做T、单独 group_id；分钟线策略可日内多次（受 risk_config.max_intraday_trades 限制），日线不支持日内做T（tag 做T仅在分钟级产生）。

### 策略接口（向量化信号 + 事件回调混合）
```python
class Strategy(ABC):
    id: str; name: str; description: str; periods: list[str]
    param_schema: list[dict]
    def prepare(self, data: dict[str, pl.DataFrame], params: dict) -> dict[str, pl.DataFrame]:
        """向量化计算指标与信号列，返回带 signal 列(1买/-1卖/0持有)及附加列(如 reason)的每个code的df"""
    def on_bar(...)  # 可选：网格做T需要更细粒度，允许策略直接实现 decide(bar_ctx)->list[Order]
```
简化决定：**每个策略实现 `prepare()` 返回逐bar的目标信号与理由列**；runner 逐bar执行：信号=1 且无仓位→开仓；信号=1 且有仓位且策略允许加仓（param `max_adds` 默认2）→加仓（tag=加仓，同group）；信号=-1→全部清仓（tag=清仓 或 止损）；网格做T策略在 prepare 中对分钟数据计算网格买卖点（side/buy_sell 列+tag 列：开仓/加仓/做T/清仓）。

**必须提供的两个内置策略**：
1. `ma_cross`（日线+分钟）：fast/slow 均线交叉；reason 如 "MA5上穿MA20"。
2. `grid_t`（分钟，也允许日线但提示适合分钟）：底仓 + ATR自适应网格（盘中振幅阈值=近N日ATR/close 的倍数，**动态阈值**，参数 atr_period/grid_atr_mult），价格跌破下网格线买回、升破上网格线卖出部分底仓；体现做T交易记录。参数：base_pct(底仓资金占比)、grid_atr_mult、max_t_times(日内T次数上限)。

### Risk 模块（runner 在每bar撮合前检查）
- 买入前：个股仓位上限（市值占比）、总仓位上限、最大回撤熔断（净值从峰值回撤超 max_drawdown_breaker% 后停止开新仓）、日内交易次数限制。
- 持仓中：止损（fixed: cost*(1-stop_loss_pct%)；atr: cost - atr*mult；trailing: highest*(1-trailing_stop%)），触发即生成强平卖单（type=止损，reason 如 "固定止损8%"）。

### Runner 主流程
```
for bar_time in 时间轴(所有股票日期并集):
    for code in universe:
        bar = 该code该bar数据(可能停牌缺失→跳过)
        1) 若有持仓: 检查止损/止盈/移动止损 → 卖出(type=止损/止盈)
        2) 消费策略信号 → 买/卖/加仓
    收盘(日线或每bar): 更新持仓市值、资金曲线(point: date/equity/drawdown/position_ratio)
```
- equity = cash + Σ持仓市值（用bar收盘价）。
- 进度上报：按时间轴推进百分比，经 task_manager 写 SQLite。
- 5分钟线：日线级约束（T+1按交易日、涨跌停按日线prev_close计算——用当日第一根bar前的日收盘）… 简化：分钟级涨跌停判定用「当日已合成日线的 prev_daily_close」（datafeed 需同时提供分钟bar与对应日线收盘序列）。
- 输出结构：契约 report 的所有字段（metrics/equity_curve/monthly_returns/trade_log/position_snapshots）。position_snapshots 按日采样（收盘快照），trade_log 每笔带 reason/type/pnl。

### Stats 指标（必须全实现）
总收益、年化（252日）、最大回撤、夏普（rf=0，日频）、索提诺、卡玛、胜率（按平仓笔）、盈亏比、平均持仓天数、**做T贡献分解**（平仓时持有时长<1交易日的归为T交易，单独统计 t_trade_count/t_win_rate/t_pnl；开仓/加仓/减仓/止损各自 pnl 累计）、月度收益表（equity_curve 按月末采样）、总手续费。

## 任务系统（task_manager.py）

- 全局 `ProcessPoolExecutor(max_workers=3)`（Windows spawn 兼容：任务函数必须是模块级可 pickle 的；进度通过子进程直接写 SQLite tasks 表：`update_progress(task_id, progress, message)`）。
- 主进程 `TaskManager`：submit(task_id, fn, kwargs)、内存注册表 {task_id: TaskInfo}、WS 广播：asyncio task 每0.5s查 SQLite 变化并推给订阅者。
- task 表字段：id/name/type(backtest/optimize/ai/data_update)/status/progress/message/error/created_at/finished_at/payload(json结果引用)。
- 回测结果存 `data/reports/{task_id}.json`；寻优结果（study）存 Optuna SQLite `data/optuna/{task_id}.db` + 汇总存 reports。
- 任务函数捕获所有异常写 error 字段，进程池 worker 崩溃也要能标记 failed（用 future callback）。

## Optuna 寻优（optimizer.py）

- `run_optimize(task_id, config, param_space, n_trials, metric)`。
- **样本内外划分**：时间轴前70%为样本内。寻优 objective 只跑样本内（end_date 覆盖为分割日）；完成后用 best_params 分别跑样本内/样本外完整回测，产出 oos_validation（含 overfit_risk 判定：样本外 metric < 样本内 * 0.5 → high；<0.8 → medium；否则 low，按 metric 类型注意方向，收益类指标）。
- `MedianPruner(n_warmup=5)`：objective 中每跑完一只股票 report 一次中间值（或按时间轴25%/50%/75%报告equity年化中间估计——简化：完成度>50%时用当前年化做 prune 参考）。
- param_space 支持 int/float/categorical(用 choices)。
- 完成后计算 `optuna.importance.get_param_importances`。
- trials 序列化：number/params/value/state/持续时间。

## LLM（llm/provider.py, analyzer.py）

- provider.py：读 llm.yaml profiles；`chat(profile_name, messages, temperature=0.3) -> {content, model, tokens, elapsed}`；httpx 调 `{base_url}/chat/completions`，`trust_env=False`，timeout 120s；主 profile 失败（超时/HTTP错误/空回复）→ 沿 fallback_chain 降级；每次调用记 SQLite llm_usage(profile, model, prompt_tokens, completion_tokens, elapsed, created_at)。
- analyzer.py：`analyze_backtest(report, profile)`：
  - 构造 prompt：系统提示词（你是量化策略分析师，输出markdown：弱点诊断/参数敏感性/优化建议列表，建议需具体到参数与方向）+ 用户消息（指标JSON+资金曲线特征点(最大回撤区间/最长回撤期/月度收益)+交易明细采样：盈利最多5笔/亏损最多5笔/随机10笔+各type数量统计）。
  - 若寻优报告存在（同config的optimize任务），附加参数重要性。
  - 返回 markdown 文本存 ai_analyses 表。
  - 未配置任何可用 profile（环境变量全空）→ 任务 failed，error 提示"未配置 LLM API Key，请设置环境变量或在 .env 中配置"。

## API 实现要点

- 严格按契约实现所有端点（含响应字段名与枚举值）。
- `POST /api/backtests` 校验：strategy_id 存在、universe 非空、日期合法、period 在策略 periods 内（不在则400）；参数缺失用 schema default 填充后回显。
- report 端点：任务非 success 返回 400 `{"detail": "回测未完成或失败: <status/error>"}`。
- kline 端点：从 report.trade_log 取该 code 的 marks；bars 来自 datafeed（原始价展示，marks 的 price 同样换算回原始价：`hfq_price / adj_factor * raw_close_ratio`——注意 marks price 直接用报告中的后复权价除以当日因子乘以原始比例。**实现建议**：trade_log 存两套价：`price`(后复权，用于计算)与 `raw_price`(展示)；kline 返回 raw_price）。trade_log JSON 中 price 用 raw_price（展示友好），另加 `hfq_price` 字段。
- WS 端点在 main.py 挂载；循环查 DB 推送，任务终态推最后一条后 `break` 关闭。
- CORS 允许 `http://localhost:5173`。
- 启动时（lifespan）：初始化 DB、确保 admin 用户存在（密码=env ADMIN_PASSWORD，pbkdf2 存储）、启动 TaskManager、注册 APScheduler 每日 16:10 数据更新任务（默认 disabled，env `ENABLE_SCHEDULER=0` 时不启用）。
- `/api/data/status`：sources 健康为 null 表示未安装/未检查；daily/minute5 统计来自 parquet 文件扫描（文件不存在时字段为 null，不报错）。

## 测试要求（必须写并跑通）

`backend/tests/`，用 pytest（加到 requirements：`pytest>=8`、`httpx` 已有）。测试用合成数据（fixture 里调 synthetic 生成 3 只股票 300 天）：
1. test_engine.py：
   - 合成数据回测 ma_cross daily 跑通，产出 report 结构完整（契约字段全存在）。
   - T+1 验证：当日买入仓位当日不可卖（构造场景断言）。
   - 手续费计算断言（买单 fee=佣金+过户费；卖单含印花税）。
   - 涨跌停一字板不成交（构造合成一字板数据）。
   - 做T分解：分钟级 grid_t 回测产生 tag=做T 的平仓记录且 t_trade_count>0。
   - 风控：个股仓位上限生效（市值占比不超上限+单bar成交误差）。
2. test_api.py：TestClient 冒烟——login→创建回测任务→轮询 status 到 success→report/kline 可取。AI 分析在无 key 环境断言 failed 且 error 提示友好。

运行：`cd backend && python -m pytest tests/ -x -q`。**所有测试必须通过后才能交付**。引擎测试直接函数调用 runner（不走进程池，便于测试）。

## 其他

- 所有代码 UTF-8，中文注释。
- Windows 兼容：multiprocessing 用 spawn（`if __name__ == "__main__"` 保护出现在 run.py 和测试入口）；路径用 pathlib。
- SQLite 并发：WAL 模式；所有写操作短事务。
- 不要创建 .env（由联调阶段统一做）；不要启动服务器。
- 完成后运行 `python -m compileall app` 确保无语法错误，运行 pytest 全绿。
