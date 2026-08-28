# 选股池扩展方案：申万三级 + 指数成分 + 随机抽样（UNIVERSE\_PICKER）

> 状态：方案设计（待实现）
> 日期：2026-08-27
> 需求（已收敛）：回测中心选股支持 **申万三级行业、指数成分（沪深300/中证500/上证50）、板块、随机数量+随机种子**。概念板块不做。
> 参考：a-stock-momentum-trend 项目的行业/成分股管道（已验证的工程方案），适配本项目 parquet + FastAPI 架构
> 关联：`MOMENTUM_T_AUDIT.md`（点时一致性原则）、`TREN_T_COMPARISON_PLAN.md`（随机池=对照实验基建）

***

## 1. 范围与总体决策

| 维度   | 决策                                                          |
| ---- | ----------------------------------------------------------- |
| 行业分类 | **申万 2021 三级体系**（l1/l2/l3 全存，选股时可任一层级过滤）                    |
| 指数成分 | 上证50 / 沪深300 / 中证500（baostock 官方接口），另派生"中证800"（=300+500 合并） |
| 板块   | 主板/创业板/科创板/北交所，**代码前缀推导，零数据依赖**                             |
| 概念   | **不做**（按需求收敛）                                               |
| 随机抽样 | 数量 n + 种子 seed，可复现（同 seed 同池子）                              |
| 选股时机 | **回测发起时静态选定**，universe 仍是显式代码列表，引擎零改动                       |

### 1.1 功能归属决策（2026-08-27 讨论）：管道与消费分层

功能拆成三层，归属不同模块，**导航不新增菜单项**：

| 层    | 内容               | 归属                               | 理由                                                   |
| ---- | ---------------- | -------------------------------- | ---------------------------------------------------- |
| 数据管道 | 行业/成分的拉取、更新、快照管理 | **数据管理页**                        | 与日线/分钟线更新同属数据维护，复用现有任务流、进度条、失败提示                     |
| 选股消费 | 筛选 + 抽样 + 预览     | **回测中心内嵌**（StockPicker 组件，实验页复用） | 选股是"发起回测"的一步，就地完成流程最短；独立页多一跳                         |
| 池子沉淀 | 池子的保存与复用         | **暂用现有模板机制承载**                   | 模板已保存完整 config（含 universe + universe\_meta 溯源），载入即复现 |

**不建独立"股票池管理"模块的决策依据**：池子的全部诉求（保存、复现、溯源）模板已覆盖，多一个一等实体增加概念负担。**升级触发条件**：当发现自己在反复"建同样的池子只换 seed"（做池子对照组时），再把池子抽成独立实体（后端加 pools 表，回测/寻优/实验发起处引用池子 ID），迁移成本低。

### 1.2 数据使用场景分层（数据就位后怎么用）

| 层次   | 场景             | 说明                                                                                                                                                       | 状态              |
| ---- | -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------- |
| 已方案化 | **选股池预筛**      | 沪深300/中证500 = 中大盘质量池（5300 全A -> 300-800 只，匹配动量趋势风格）；申万三级 = 行业专注或行业排除                                                                                     | 本次交付            |
| 已方案化 | **随机对照池**      | 随机池 = 无选股偏差的对照组。用"同筛选条件跑 10 个 seed"检验策略池子依赖性：10 个 seed 结果仍巨离 -> 策略无稳定 edge；收敛 -> 之前的离散是挑池噪声（三案例手工挑池的幸存者偏差正是待检验对象）                                        | 本次交付            |
| 立即可做 | **行业限配**（策略层）  | momentum\_t 的 top\_n 易押单一行业（案例实证：盈利集中 603259+301217 两只票）。加 `max_per_industry` 参数：top\_n 命中时每行业最多 N 只强制分散；实现点在 `_rank_days` 选股时 join stock\_industry 分组截断 | 后续迭代（治已确诊的抱团问题） |
| 立即可做 | **行业归因**（报告层）  | 交易明细/持仓按申万一级聚合：哪个行业赚哪个亏、盈亏是否集中单一行业；喂 AI 诊断引擎（OPTIMIZE\_AND\_AI\_PLAN 方案 B）新增 `INDUSTRY_CONCENTRATION` 规则；MetricsTab 加行业盈亏分布表                             | 后续迭代            |
| 后续可选 | 行业轮动 / 行业动量横截面 | 新策略方向（select\_trend 原型的天然延伸），行业数据是其前提基建                                                                                                                  | 远期              |

**推荐使用路径**：① 管道建好 -> 用行业/指数选池 + 随机池；② **第一件事是用随机池跑 10-seed 对照检验策略真实成色**（而非"选个更好的池子"）--手工池赢多少、随机池能不能赢，这个数字比任何单次回测更能说明策略成色；③ 行业限配 + 行业归因进迭代。

## 2. 现状与差距

| 项     | 现状                                                                     | 差距        |
| ----- | ---------------------------------------------------------------------- | --------- |
| 股票元数据 | `stock_basic.parquet` 仅 code/name/st/list\_date（且 list\_date 全 null）   | 无行业字段     |
| 指数成分  | 无                                                                      | 全新        |
| 数据源封装 | BaostockSource（`_run_query` 登录缓存+重试基建完善）/ AkshareSource / MootdxSource | 均无行业/成分接口 |
| 选股 UI | 代码/名称搜索 + 批量粘贴                                                         | 无条件选股     |
| API   | `GET /api/stocks`（模糊搜索）、`GET /api/stocks/by-codes`                     | 无 pick 接口 |

## 3. 数据源设计（适配自参考项目）

### 3.1 指数成分：baostock（主路径，秒级）

参考项目的映射直接沿用：

| 指数          | baostock 接口            | 预期行数  |
| ----------- | ---------------------- | ----- |
| sz50 上证50   | `query_sz50_stocks()`  | \~50  |
| hs300 沪深300 | `query_hs300_stocks()` | \~300 |
| zz500 中证500 | `query_zz500_stocks()` | \~500 |

* 落位：`BaostockSource.get_index_constituents(index_key)`，复用现有 `_run_query`（登录态缓存、会话失效重登）

* 返回字段：code（sh.600000 格式）-> 归一化为纯数字（项目惯例）、name、updateDate

* **全量替换**语义（参考项目教训）：每次刷新整体覆盖，避免增量合并残留已调出指数的股票

### 3.2 申万三级：乐咕乐股路径（主路径，零密钥零成本）

参考项目 `classification.py` 已验证的方案，直接移植逻辑：

1. **层级总览一次拉取**：`legulegu.com/stockdata/sw-industry-overview` 解析出 31 个一级 / \~131 个二级 / \~346 个三级行业及层级关系
2. **成分逐行业抓取**：`legulegu.com/stockdata/index-composition?industryCode=xxx`，**并发上限 2 + 0.8s 请求间隔**（防限流，参考项目实测参数）
3. 层级推导：申万 2021 六位分类代码前缀规则（前 2 位=一级、前 4 位=二级、全 6 位=三级）；只取三级条目的成分（一二级成分是其超集，避免重复计数）

* 落位：`AkshareSource.get_sw_industry()`（乐咕即 akshare sw\_index 系列接口的底层数据源；若 akshare 接口可用则优先走 akshare 封装，不可用则直接抓参考项目的 URL 解析逻辑）

* 全量时长预估：346 行业 × (0.8s 间隔 ÷ 2 并发 + 抓取) ≈ **3\~5 分钟**，走异步任务（§5）

* **失败安全**（参考项目规则）：拉取结果为空但本地已有数据 -> 保留旧数据不静默清空；单行业失败 -> 记录并跳过（缺行业不阻断整体）

### 3.3 理杏仁加速路径（可选，Phase 2 再议）

参考项目首选理杏仁（2 次请求全量，快百倍）但需 API key。本项目暂不引入（无密钥基建）；若后续申万更新太慢成为痛点，加 `LIXINGER_API_KEY` 环境变量 + `POST cn/industry/constituents/sw_2021` 两次请求的加速分支，存储格式不变。

### 3.4 板块（零成本）

`code` 前缀推导：`60/00` 主板、`30` 创业板、`688/689` 科创板、`4/8/9` 北交所。纯函数，无数据源依赖，行业管道未就绪时即可用。

## 4. 存储契约（新增两个 parquet）

### 4.1 `index_constituents.parquet`（长表）

```
index_key (str)   # sz50 | hs300 | zz500 | csi800（派生）
code      (str)   # 纯数字 6 位
name      (str)
update_date (str) # baostock 返回的成分更新日
snapshot_date (str) # 本地刷新日期
```

\~1150 行（50+300+500+派生300 重复计），全量替换写入。

### 4.2 `stock_industry.parquet`

```
code (str)         # 纯数字
sw_l1 / sw_l2 / sw_l3 (str)   # 申万 2021 三级名称（代码可另存备审计）
sw_code (str)      # 六位分类代码
snapshot_date (str)
```

\~5300 行（全 A 一票一行）。**无行业数据的票不写行**，选股时 left join，行业过滤条件会自然排除它们（不设行业条件时不受影响）。

`store.py` 新增 `read/write_index_constituents`、`read/write_stock_industry`（沿用现有 read 时归一化 code 的惯例）。

**点时一致性说明**（延续 AUDIT LK3 原则）：两表均为**当前快照**，非历史时点数据。因过滤发生在选股阶段（回测发起时生成静态 universe），不构成策略层未来函数；长窗口回测的行业漂移在方案文档与报告中注明即可，与 ST 过滤同类近似。

## 5. 更新管道集成

* `updater.py` 新增 `update_industry(progress_cb)`：

  1. baostock 三指数成分（秒级）+ csi800 派生 -> 全量替换写 index\_constituents
  2. 乐咕三级管道（并发 2 / 0.8s 间隔，逐行业上报进度 `申万三级: xxx (i/346)`）-> 全量替换写 stock\_industry
  3. 失败安全：任一步拉空且本地有旧数据 -> 保留旧数据并在返回统计中标注 `kept_old: true`

* `update()` 的 scope 增加 `"industry"`；`data.py POST /api/data/update` 透传（现有任务流/进度条/错误展示全复用）

* 数据管理页加"更新行业与成分"按钮（scope=industry），显示上次快照日期

* 刷新频率建议：行业/成分月度级变动，**手动触发即可**（项目无调度器，符合现状）；参考项目的 7 天缓存与熔断机制在手动模式下无必要，暂不引入

## 6. API 设计

### 6.1 `GET /api/stocks/pick-options`

返回筛选维度选项（供前端下拉/树）：

```jsonc
{
  "indices": [
    {"key": "sz50", "name": "上证50", "count": 50},
    {"key": "hs300", "name": "沪深300", "count": 300},
    {"key": "zz500", "name": "中证500", "count": 500},
    {"key": "csi800", "name": "中证800", "count": 800}
  ],
  "industry_tree": [
    {"value": "农林牧渔", "label": "农林牧渔(一级)", "children": [
      {"value": "种植业", "label": "种植业(二级)", "children": [
        {"value": "种子", "label": "种子(三级)"}]}]}
  ],   // L1->L2->L3 树，带计数后缀
  "boards": [
    {"key": "main", "name": "主板", "count": 3100},
    {"key": "chinext", "name": "创业板", "count": 1300},
    {"key": "star", "name": "科创板", "count": 570},
    {"key": "bse", "name": "北交所", "count": 250}
  ],
  "industry_snapshot": "2026-08-27",
  "index_snapshot": "2026-08-27"
}
```

* count 基于当前本地数据实时计算；两表任一缺失 -> 对应选项为空数组 + snapshot 为 null（前端显示"请先更新行业数据"）

### 6.2 `POST /api/stocks/pick`（即时查询，非任务）

```jsonc
// 请求
{
  "filters": {
    "index": "hs300",              // 可选，单选：sz50|hs300|zz500|csi800
    "industry_l1": [],             // 可选，多选 OR（与 l2/l3 为 OR 合集，行业内任一命中即算）
    "industry_l2": ["种植业"],
    "industry_l3": [],
    "boards": ["chinext"],         // 可选，多选 OR
    "exclude_st": true
  },
  "random": {"n": 20, "seed": 42}  // n/seed 均可选
}
// 响应
{
  "codes": ["300139", "..."],      // 最终池子（=写进 universe 的内容）
  "name_map": {"300139": "晓程科技"},
  "total_matched": 450,
  "total_picked": 20,
  "seed_used": 42,
  "meta": {"source": "industry_pick", "filters": {...}, "seed_used": 42,
           "total_matched": 450, "picked_at": "2026-08-27"}   // 直接可存进 universe_meta
}
```

**过滤语义**：`index ∧ (l1∨l2∨l3 任一命中) ∧ board ∧ 非ST`（维度间 AND、维度内 OR）；行业条件全空 = 不限行业。

**随机抽样语义**（与 `synthetic.generate_demo_data` 的 RNG 惯例一致）：

* `sorted(codes)` 排序后 `np.random.default_rng(seed).choice(codes, n, replace=False)`--池子排序固定，**同 seed 必然同结果**

* seed 缺省 -> 后端生成随机种子并在 `seed_used` 返回，前端固化进配置（复现实验）

* n 缺省 = 全量；n > 命中数 -> 全取并在响应提示

* ST 过滤复用 stock\_basic 现有字段

## 7. universe 溯源（后端契约小改）

`validate_backtest_config` 放行可选 `universe_meta` 字段（§6.2 的 meta 原样存储），随 config 进入 reports/templates。作用：**模板载入/实验复现时，池子的来历与 seed 可审计**（codes 本身已显式，meta 是溯源不是执行依赖）。前端 types.ts 加对应可选类型，引擎与执行链路零改动。

## 8. 前端方案：StockPicker 组件

### 8.1 结构（BacktestList 股票池 Form.Item 替换）

```
Radio.Group: [手动选择 | 条件选股]
├─ 手动模式：现有搜索 + 批量粘贴（原样保留）
└─ 条件选股模式（新）：
   ┌─ 筛选条件 ──────────────────────────────┐
   │ 指数成分: Select 单选（不限/上证50/沪深300/中证500/中证800）│
   │ 申万行业: TreeSelect 多选（L1→L2→L3 树，勾父级=全选子级） │
   │ 板　　块: Checkbox.Group（主板/创业板/科创板/北交所）      │
   │ 剔除ST:  Switch（默认开）                                │
   ├─ 随机抽样 ──────────────────────────────┤
   │ 数量 n: InputNumber   种子: InputNumber（可手改）          │
   │ [重新抽样]（换随机 seed）  ✓锁定种子（模板载入后防误抽样） │
   ├─ 预览 ─────────────────────────────────┤
   │ 命中 450 只 → 抽取 20 只（seed=42）                       │
   │ [Tag 流：前 20 只 代码+名称，点击全部展开]                 │
   │ [应用为股票池]  ← 唯一写入口                              │
   └────────────────────────────────────────┘
```

### 8.2 交互要点

* **预览与应用解耦**：筛选条件变化只刷新预览（调 pick），点"应用为股票池"才写入 form 的 `universe` 字段并折叠回 Tag 展示--避免误覆盖已选池

* **seed 锁定开关**：载入模板/复现实验时锁住 seed（显示锁图标），防手滑点"重新抽样"

* **重新抽样**：仅随机化 seed 后重调 pick（过滤条件不变）

* 行业树选项带计数（如"种子(18)"）；两表未更新时条件选股模式顶部显示 Alert 引导去数据管理页更新

* 提交校验：条件选股模式下 universe 为空 -> "请先点击「应用为股票池」"

### 8.3 文件清单

| 文件                           | 动作                                                |
| ---------------------------- | ------------------------------------------------- |
| `components/StockPicker.tsx` | 新建（双模式 + 筛选 + 抽样 + 预览/应用；手动模式逻辑从 BacktestList 抽出） |
| `pages/BacktestList.tsx`     | 股票池 Form.Item 换 StockPicker；提交体带 universe\_meta   |
| `pages/ExperimentList.tsx`   | 复用 StockPicker（同一组件）                              |
| `pages/DataManagement.tsx`   | 加"更新行业与成分"按钮（scope=industry，显示快照日期）               |
| `api/client.ts` / `types.ts` | pickStocks / getPickOptions + 类型定义                |

## 9. 实施顺序

| 阶段          | 内容                                                             | 量级                   |
| ----------- | -------------------------------------------------------------- | -------------------- |
| Phase 1     | baostock 指数成分接口 + 两个 parquet 存储 + 板块推导函数                       | \~2 小时               |
| Phase 2     | 乐咕申万三级管道（移植参考项目并发/限流参数）+ updater scope=industry + 失败安全         | \~半天（含真机调通 346 行业抓取） |
| Phase 3     | pick-options / pick API（过滤+可复现抽样）+ universe\_meta 契约 + 测试      | \~半天                 |
| Phase 4     | 前端 StockPicker + BacktestList/ExperimentList/DataManagement 接入 | \~1 天                |
| Phase 5（可选） | 理杏仁加速分支（LIXINGER\_API\_KEY 存在时启用）                              | \~2 小时               |

**验收标准**：

1. 数据管理页一键更新行业/成分，乐咕全量 \~5 分钟完成，空数据保护生效（拔网线重跑不清空旧数据）
2. 条件选股：沪深300 ∧ 创业板 ∧ 随机 20 只（seed=42）-> 应用 -> 提交回测 -> 载入模板复现得到完全相同的 20 只
3. 行业树三层可选，任一层级过滤计数与 parquet 实际行数一致
4. 手动模式回归无损（搜索/批量粘贴照常）

## 10. 风险与注意事项

1. **乐咕接口反爬**：参考项目的并发 2 / 0.8s 间隔是实测安全参数，**不得放宽**；单行业失败跳过不阻断；整站不可用时保留旧数据
2. **申万口径**：乐咕为申万 2021 版三级（与理杏仁 sw\_2021 同口径），快照日期入表可查；行业月度调整导致的成分漂移，长窗口回测时属已知近似
3. **代码归一化**：baostock 返回 sh. 前缀、乐咕返回 6 位纯数字，入库前统一纯数字（沿用 `_norm_code` + store 读时兜底归一化的双保险）
4. **csi800 派生行与 hs300/zz500 的重复**：长表存储天然允许一票多行（不同 index\_key），pick 按所选 index\_key 过滤，无冲突
5. **中证500 与沪深300 互斥**（编制规则如此），多选无意义故设计为单选；csi800 覆盖两者合集
6. **后续可选扩展**（不在本范围）：策略层行业限配（参考项目 `apply_per_industry_cap`，避免 top\_n 押单一行业）、按 point-in-time 行业历史做长窗口精确回测

## 11. 参考项目映射表（实现时对照移植）

| 参考项目实现                               | 本项目落位                                                        |
| ------------------------------------ | ------------------------------------------------------------ |
| `baostock_src.py` 指数接口映射             | `sources.py` BaostockSource.get\_index\_constituents         |
| `universe.py` refresh\_universe 全量替换 | `updater.update_industry()` 步骤 1                             |
| `classification.py` 乐咕解析（并发2/0.8s）   | `sources.py` AkshareSource.get\_sw\_industry（或独立 crawler 函数） |
| `classification.py` 失败安全/失败保留旧数据     | `updater.update_industry()` kept\_old 逻辑                     |
| `StockClassification` 表 sw\_l1/l2/l3 | `stock_industry.parquet`                                     |
| `IndexConstituent` 表                 | `index_constituents.parquet`                                 |
| `industry_tree()` 树形接口               | `GET /api/stocks/pick-options`                               |
| 证监会行业兜底 fallback\_csrc               | 暂不引入（申万缺失票直接排除，简单明确）                                         |
| 理杏仁主路径                               | 可选 Phase 5（密钥基建缺省不启用）                                        |

