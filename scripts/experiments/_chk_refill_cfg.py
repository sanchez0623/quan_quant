import sys
sys.path.insert(0, '.')
from app import db

for tid in ['bt_ccfcd90d8acb', 'bt_af9ee940144e']:
    t = db.get_task(tid)
    cfg = (t.get('payload') or {}).get('config') or {}
    p = cfg.get('params') or {}
    print(tid, '|', t['name'], '| created:', t['created_at'])
    print('  refill:', repr(cfg.get('pool_refill_min')),
          '| shadow_confirm:', repr(p.get('shadow_confirm')),
          '| shadow_z_confirm:', repr(p.get('shadow_z_confirm')),
          '| top_x:', repr(cfg.get('auto_top_x')),
          '| universe_auto:', repr(cfg.get('universe_auto')))
