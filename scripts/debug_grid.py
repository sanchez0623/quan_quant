# -*- coding: utf-8 -*-
"""调试 grid_t 信号分布"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, "backend")
from app.engine import datafeed
from app.engine.strategies.grid_t import GridTStrategy

data = datafeed.load_minute5(["600000", "000001"], None, None, None)
print(f"股票数: {len(data)}, 600000 bars: {len(data['600000'])}")

st = GridTStrategy()
params = {"base_pct": 30, "grid_atr_mult": 1.5, "atr_period": 14, "max_t_times": 4}
out = st.prepare(data, params)

for code, df in out.items():
    sig = df["signal"].to_list()
    tags = df["tag"].to_list()
    from collections import Counter
    tag_cnt = Counter(t for t, s in zip(tags, sig) if s != 0)
    atr_pct = df["day_atr_pct"].drop_nulls().to_list()
    print(f"{code}: bars={len(sig)}, 信号数={sum(1 for s in sig if s != 0)}, tag分布={dict(tag_cnt)}")
    print(f"  day_atr_pct: min={min(atr_pct):.4f}, max={max(atr_pct):.4f}, mean={sum(atr_pct)/len(atr_pct):.4f}")
    print(f"  网格阈值 g = atr_pct*1.5 ≈ {sum(atr_pct)/len(atr_pct)*1.5*100:.2f}%")
