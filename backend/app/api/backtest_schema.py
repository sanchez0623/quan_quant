# -*- coding: utf-8 -*-
"""回测顶层字段的唯一事实源（single source of truth）。

背景：回测配置的顶层标量字段（约 35 个）此前散落在 5 处——
  ① ``BacktestRequest`` 的 pydantic 字段默认值
  ② ``normalize_config`` 的 ``top_defaults`` 登记表
  ③ 前端 ``BacktestList.tsx`` 的 ``initialValues``
  ④ 前端 ``BacktestList.tsx`` 的 ``buildConfigFromValues``
  ⑤ 前端 ``BacktestList.tsx`` 的 ``applyConfigToForm``（含 numericKeys）
任一处漏登记就是一次静默丢值事故：

  · ``auto_rank_key`` / ``nav_take_profit_pct`` 未登记 -> 模板落库缺失、载入回落默认；
  · ``pool_refill_min`` 未进 ``BacktestRequest`` -> 被 pydantic 静默丢弃，后端强制兜底 2
    （而前端/引擎/文档都写 0=关闭，用户手动建的「采纳形态复现」任务被静默配成已证伪组合）。

本模块把「键 / 标签 / 类型 / 默认值 / 选项 / 数值范围 / 分组」收敛成一份 schema，
由它派生三处消费方：

  1. ``BacktestRequest``：backtests.py 用 ``create_model`` 生成，字段与默认值全部来自这里；
  2. ``normalize_config`` 的顶层默认值填充：``top_level_defaults()``；
  3. ``GET /api/backtests/meta``：下发给前端的表单元数据（默认值/选项/上下限不再手写）。

新增顶层字段只改本文件，契约测试见 ``tests/test_backtest_form_contract.py``。

各属性含义：
  key/label/type/default  字段三要素；``required=True`` 表示无默认值、必填
  group                   前端分组（骨架 / 动态选股 / 闸门 / 资金与基准 / 交易成本 / 账户与出金）
  ui                      block=整行字段｜inline=内联小控件｜hidden=不进表单
  fill                    是否由 normalize_config 补默认值（非模型路径：模板/实验/寻优）
  prefill                 前端表单是否用该默认值预填（False 的字段由用户显式选择，如 name/period）
  min/max/step/unit       数值控件范围（下发给前端）
  help                    字段说明（前端 extra / tooltip）
  choices / choices_from  静态选项 / 动态选项（运行时从引擎与数据层注册表解析）
"""
from __future__ import annotations

from typing import Any

# 排序键（RANK_KEYS）中文标签：与前端选择器、premarket 文档口径一致
RANK_KEY_LABELS = {
    "score": "累计强度",
    "accel": "加速度",
    "fresh": "金叉新鲜",
    "mom_gap": "短中差值",
}

TOP_FIELDS: list[dict[str, Any]] = [
    # ---------------- 骨架（必填或由前端回填；不参与 normalize 默认值填充） ----------------
    {"key": "name", "label": "任务名称", "type": "str", "default": "回测任务", "group": "骨架", "ui": "block"},
    {"key": "strategy_id", "label": "策略", "type": "str", "required": True, "group": "骨架", "ui": "block"},
    {"key": "period", "label": "回测周期", "type": "str", "default": "daily", "group": "骨架", "ui": "block",
     "choices_from": "strategy_periods"},
    {"key": "universe", "label": "股票池", "type": "str_list", "default": [], "group": "骨架", "ui": "block",
     "fill": False},
    {"key": "universe_meta", "label": "条件选股溯源", "type": "dict_or_none", "default": None,
     "group": "骨架", "ui": "hidden", "fill": False},
    {"key": "start_date", "label": "回测起始日", "type": "str", "required": True, "group": "骨架", "ui": "hidden"},
    {"key": "end_date", "label": "回测结束日", "type": "str", "required": True, "group": "骨架", "ui": "hidden"},
    {"key": "end_date_today", "label": "结束日跟随今天", "type": "bool", "default": False,
     "group": "骨架", "ui": "hidden", "fill": False},
    {"key": "params", "label": "策略参数", "type": "dict", "default": {}, "group": "骨架", "ui": "hidden",
     "fill": False},
    {"key": "risk_config", "label": "风控配置", "type": "risk_config", "group": "骨架", "ui": "hidden",
     "fill": False},

    # ---------------- 动态选股（universe_auto 分段滚动重选） ----------------
    {"key": "universe_auto", "prefill": True, "label": "动态选股（滚动重选）", "type": "bool", "default": False,
     "group": "动态选股", "ui": "block", "fill": False,
     "help": "全空仓 N 个交易日后自动重跑动量趋势预筛换池；仅支持 momentum_t / momentum_slot"},
    {"key": "auto_idle_days", "prefill": True, "label": "空仓触发（交易日）", "type": "int", "default": 5,
     "group": "动态选股", "ui": "inline", "fill": True, "min": 1, "max": 60, "step": 1},
    {"key": "pool_refill_min", "prefill": True, "label": "枯竭换血线（持仓少于）", "type": "int", "default": 0,
     "group": "动态选股", "ui": "inline", "fill": True, "min": 0, "max": 50, "step": 1,
     "help": "日终持仓低于该值（闸门未拦截时）当天收盘后换池重选；已持仓不动。0=关闭"},
    {"key": "auto_top_x", "prefill": True, "label": "池子大小", "type": "int", "default": 30,
     "group": "动态选股", "ui": "inline", "fill": True, "min": 1, "max": 500, "step": 1},
    {"key": "auto_above_ma", "prefill": True, "label": "均线锚", "type": "int", "default": 20,
     "group": "动态选股", "ui": "inline", "fill": True, "min": 5, "max": 120, "step": 1,
     "choices": [
         {"value": 20, "label": "MA20（slot）"},
         {"value": 60, "label": "MA60（t）"},
         {"value": 120, "label": "MA120"},
     ]},
    {"key": "auto_with_accel", "prefill": True, "label": "加速项", "type": "bool_or_none", "default": False,
     "group": "动态选股", "ui": "inline", "fill": True,
     "help": "动量分叠加加速度项（短周期跑赢中周期）；None=跟随策略默认"},
    {"key": "auto_min_rps", "label": "RPS≥", "type": "float_or_none", "default": None,
     "group": "动态选股", "ui": "inline", "fill": True, "min": 0, "max": 100,
     "help": "全市场 RPS 分位下限；留空=不限"},
    {"key": "auto_index", "prefill": True, "label": "指数域", "type": "str_list", "default": [],
     "group": "动态选股", "ui": "inline", "fill": True, "choices_from": "auto_index"},
    {"key": "auto_boards", "prefill": True, "label": "板块域", "type": "str_list", "default": [],
     "group": "动态选股", "ui": "inline", "fill": True, "choices_from": "auto_boards"},
    {"key": "auto_rank_key", "prefill": True, "label": "排序键", "type": "str", "default": "score",
     "group": "动态选股", "ui": "inline", "fill": True, "choices_from": "rank_key"},

    # ---------------- 闸门（池级趋势 / 大盘趋势） ----------------
    {"key": "pool_gate", "prefill": True, "label": "池级趋势开关", "type": "bool", "default": False,
     "group": "闸门", "ui": "block", "fill": True,
     "help": "池内动量为正的票占比连续 2 日低于触发阈值时抑制开新仓/加仓；回升至 2×阈值连续 2 日恢复"},
    {"key": "pool_gate_enter_th", "prefill": True, "label": "触发阈值（健康度）", "type": "float", "default": 0.15,
     "group": "闸门", "ui": "inline", "fill": True, "min": 0.02, "max": 0.49, "step": 0.05,
     "help": "恢复线 = 2×触发值（内置）"},
    {"key": "index_gate", "prefill": True, "label": "大盘趋势闸门", "type": "bool", "default": False,
     "group": "闸门", "ui": "block", "fill": True,
     "help": "中证500 收盘跌破所选均线连续 2 日抑制开新仓/加仓；回升至均线×1.01 上方连续 2 日恢复"},
    {"key": "index_gate_ma", "prefill": True, "label": "闸门均线周期", "type": "int", "default": 20,
     "group": "闸门", "ui": "inline", "fill": True, "min": 5, "max": 250, "step": 5,
     "help": "日线，默认 20；恢复线 = 均线×1.01（内置）"},

    # ---------------- 资金与基准 ----------------
    {"key": "initial_capital", "prefill": True, "label": "初始资金（元）", "type": "float", "default": 400000,
     "group": "资金与基准", "ui": "block", "fill": False, "min": 1000, "step": 100000},
    {"key": "benchmark", "prefill": True, "label": "基准指数", "type": "str", "default": "000905",
     "group": "资金与基准", "ui": "block", "fill": True,
     "choices": [
         {"value": "000905", "label": "中证500"},
         {"value": "000300", "label": "沪深300"},
     ],
     "help": "报告净值图叠加基准对比 + 超额收益指标"},

    # ---------------- 交易成本 ----------------
    {"key": "slippage_pct", "prefill": True, "label": "滑点比例", "type": "float", "default": 0.001,
     "group": "交易成本", "ui": "block", "fill": False, "min": 0, "step": 0.0005,
     "help": "0.001 表示 0.1%"},
    {"key": "commission_rate", "prefill": True, "label": "佣金率", "type": "float", "default": 0.00005,
     "group": "交易成本", "ui": "block", "fill": False, "min": 0, "step": 0.00001,
     "help": "0.00005 = 万0.5"},
    {"key": "commission_min", "prefill": True, "label": "最低佣金（元）", "type": "float", "default": 5,
     "group": "交易成本", "ui": "block", "fill": False, "min": 0, "step": 1},
    {"key": "stamp_tax", "prefill": True, "label": "印花税", "type": "float", "default": 0.0005,
     "group": "交易成本", "ui": "block", "fill": False, "min": 0, "step": 0.0001,
     "help": "0.0005 = 万5，仅卖出"},
    {"key": "handling_fee", "prefill": True, "label": "经手费", "type": "float", "default": 0.0000341,
     "group": "交易成本", "ui": "block", "fill": False, "min": 0, "step": 0.0000034,
     "help": "万0.341 双边"},
    {"key": "regulatory_fee", "prefill": True, "label": "证管费", "type": "float", "default": 0.00002,
     "group": "交易成本", "ui": "block", "fill": False, "min": 0, "step": 0.000002,
     "help": "万0.2 双边"},
    {"key": "transfer_fee", "prefill": True, "label": "过户费", "type": "float", "default": 0.00001,
     "group": "交易成本", "ui": "block", "fill": False, "min": 0, "step": 0.000005,
     "help": "万0.1 双边"},

    # ---------------- 账户与出金 ----------------
    {"key": "monthly_withdraw_base", "prefill": True, "label": "每月提取目标额（元）", "type": "float", "default": 5000,
     "group": "账户与出金", "ui": "block", "fill": True, "min": 0, "step": 500,
     "help": "月中已达标则月末不再提取；0=关闭"},
    {"key": "t_profit_withdraw_pct", "prefill": True, "label": "T盈利提成（%）", "type": "float", "default": 10,
     "group": "账户与出金", "ui": "block", "fill": True, "min": 0, "max": 100, "step": 1,
     "help": "每笔做T盈利即时提取比例"},
    {"key": "nav_take_profit_pct", "prefill": True, "label": "总资金止盈（%）", "type": "float", "default": 0,
     "group": "账户与出金", "ui": "block", "fill": True, "min": 0, "max": 1000, "step": 5,
     "help": "净值相对上次提取后基准涨幅达阈值即触发；0=关闭"},
    {"key": "nav_take_profit_withdraw_pct", "prefill": True, "label": "止盈提取收益（%）", "type": "float", "default": 0,
     "group": "账户与出金", "ui": "block", "fill": True, "min": 0, "max": 100, "step": 5,
     "help": "触发时按比例提取收益部分（本金不动）；0=关闭"},
    {"key": "min_t_amount", "prefill": True, "label": "最小T金额（元）", "type": "float", "default": 20000,
     "group": "账户与出金", "ui": "block", "fill": True, "min": 0, "step": 5000,
     "help": "低于该金额的做T自动跳过（防碎单费用磨损）"},
    {"key": "warmup_days", "prefill": True, "label": "指标预热（交易日）", "type": "int", "default": 0,
     "group": "账户与出金", "ui": "block", "fill": False, "min": 0, "step": 10,
     "help": "0=自动按策略建议前推；数据不足时按实际"},
    {"key": "exclude_st", "prefill": True, "label": "剔除ST", "type": "bool", "default": True,
     "group": "账户与出金", "ui": "block", "fill": True},
]

# 前端表单需要渲染的字段（hidden 的由其他字段派生或后端专用）
UI_FIELDS: list[dict[str, Any]] = [f for f in TOP_FIELDS if f.get("ui") != "hidden"]


def field_map() -> dict[str, dict[str, Any]]:
    """key -> schema 条目。"""
    return {f["key"]: f for f in TOP_FIELDS}


def top_level_defaults() -> dict[str, Any]:
    """normalize_config 需要补默认值的字段（fill=True）。

    刻意不含骨架字段（name/strategy_id/universe/params/risk_config 等）与
    非 fill 的成本/资金字段：它们的默认值由调用方或引擎自己的默认值负责，
    在这里改写会静默改动非模型路径（模板/实验/寻优）的落库配置。
    """
    return {f["key"]: f["default"] for f in TOP_FIELDS if f.get("fill")}


def pydantic_field_spec(field: dict[str, Any]) -> tuple[Any, Any]:
    """把 schema 条目转成 ``create_model`` 的 (注解, FieldInfo)。

    只处理无外部依赖的类型；``risk_config`` 由 backtests.py 注入具体模型。
    """
    from pydantic import Field

    type_name = field["type"]
    annotations: dict[str, Any] = {
        "str": str,
        "int": int,
        "float": float,
        "bool": bool,
        "str_list": list[str],
        "dict": dict,
        "dict_or_none": dict | None,
        "bool_or_none": bool | None,
        "float_or_none": float | None,
        "int_or_none": int | None,
    }
    if type_name == "risk_config":
        raise ValueError("risk_config 需由调用方注入具体模型类型")
    annotation = annotations[type_name]
    label = field.get("label", field["key"])
    if field.get("required"):
        return annotation, Field(..., description=label)
    if type_name in ("str_list", "dict"):
        return annotation, Field(default_factory=annotation, description=label)
    return annotation, Field(default=field.get("default"), description=label)
