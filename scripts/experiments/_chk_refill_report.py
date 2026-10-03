"""仲裁：tasks 表 payload.config vs 报告 JSON（引擎实际跑的 config）的 pool_refill_min。"""
import sys, json, os
sys.path.insert(0, '.')
from app import db

for tid in ['bt_76889c798212', 'bt_80a402b2cedb', 'bt_b97b590f8d4f', 'bt_ccfcd90d8acb',
            'bt_1e75caa34c5b', 'bt_af9ee940144e', 'bt_124c6d43225a']:
    t = db.get_task(tid)
    payload = t.get('payload') or {}
    cfg_db = (payload.get('config') or {}).get('pool_refill_min', '<缺>')
    rp = payload.get('report_path')
    cfg_rep = '<无报告>'
    if rp and os.path.exists(rp):
        try:
            with open(rp, encoding='utf-8') as f:
                rep = json.load(f)
            cfg_rep = (rep.get('config') or rep.get('meta', {}).get('config') or {}).get('pool_refill_min', '<报告无config>')
        except Exception as e:
            cfg_rep = f'<读取失败:{e}>'
    print(f"{tid} | {t['name'][:28]}")
    print(f"   tasks表 config.refill = {cfg_db!r} | 报告JSON config.refill = {cfg_rep!r}")
