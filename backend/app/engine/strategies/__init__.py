# -*- coding: utf-8 -*-
"""策略注册表"""
from .grid_t import GridTStrategy
from .ma_cross import MaCrossStrategy
from .momentum_slot import MomentumSlotStrategy
from .momentum_t import MomentumTStrategy

REGISTRY: dict[str, object] = {
    s.id: s for s in [MaCrossStrategy(), GridTStrategy(), MomentumTStrategy(),
                      MomentumSlotStrategy()]
}


def get_strategy(strategy_id: str):
    return REGISTRY.get(strategy_id)


def apply_param_defaults(strategy_id: str, params: dict) -> dict:
    """参数缺失用 schema default 填充"""
    strategy = REGISTRY.get(strategy_id)
    if strategy is None:
        return dict(params or {})
    out = dict(params or {})
    for p in strategy.param_schema:
        if p["key"] not in out:
            out[p["key"]] = p.get("default")
    return out


def validate_params(strategy_id: str, params: dict) -> tuple[bool, str]:
    """类型/范围校验（尽力而为）"""
    strategy = REGISTRY.get(strategy_id)
    if strategy is None:
        return False, f"策略不存在: {strategy_id}"
    schema = {p["key"]: p for p in strategy.param_schema}
    for k, v in (params or {}).items():
        if k not in schema:
            continue  # 允许透传（如 stop_loss_pct 同名风控参数）
        s = schema[k]
        t = s.get("type")
        try:
            if t == "int":
                v2 = int(v)
                if ("min" in s and v2 < s["min"]) or ("max" in s and v2 > s["max"]):
                    return False, f"参数 {k}={v} 超出范围 [{s.get('min')}, {s.get('max')}]"
            elif t == "float":
                v2 = float(v)
                if ("min" in s and v2 < s["min"]) or ("max" in s and v2 > s["max"]):
                    return False, f"参数 {k}={v} 超出范围 [{s.get('min')}, {s.get('max')}]"
            elif t == "categorical":
                # choices 元素支持 "value|中文标签" 展示格式，校验只认 | 前的 value
                choices = [c.split("|")[0] for c in (s.get("choices") or [])]
                if choices and v not in choices:
                    return False, f"参数 {k}={v} 不在可选值 {choices} 中"
        except (TypeError, ValueError):
            return False, f"参数 {k} 类型错误，期望 {t}"
    return True, ""
