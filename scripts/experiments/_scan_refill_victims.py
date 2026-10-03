"""扫描中招任务：config 里 pool_refill_min=2 且非换血线实验链（疑似前端默认值 bug 注入）。"""
import sys
sys.path.insert(0, '.')
from app import db

EXP_KEYWORDS = ('换血线', '组合', '动态选股', 'refill', '枯竭')
rows = db.list_tasks('backtest')
sus, exp_ok = [], 0
for t in rows:
    cfg = (t.get('payload') or {}).get('config') or {}
    refill = cfg.get('pool_refill_min')
    if refill != 2:
        continue
    name = t['name']
    if any(k in name for k in EXP_KEYWORDS):
        exp_ok += 1  # 实验链：故意配 2，正常
        continue
    ua = cfg.get('universe_auto')
    p = cfg.get('params') or {}
    sus.append((t['task_id'], name, t['created_at'], ua, p.get('shadow_confirm'), p.get('shadow_z_confirm')))

print(f"实验链 refill=2 任务（正常）: {exp_ok} 个")
print(f"疑似中招（refill=2 且非实验名）: {len(sus)} 个")
for tid, name, created, ua, sc, szc in sus:
    print(f"  {tid} | {created} | {name} | universe_auto={ua} | shadow_confirm={sc}/{szc}")
