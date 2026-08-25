# -*- coding: utf-8 -*-
"""pytest fixtures：合成演示数据（3只股票300天，独立tmp目录）"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

TEST_STOCKS = ["600000", "000001", "600036"]


@pytest.fixture(scope="session")
def demo_env(tmp_path_factory):
    """生成合成数据，返回 (data_dir, start_date, end_date)"""
    from app.data import synthetic
    d = tmp_path_factory.mktemp("data")
    stats = synthetic.generate_demo_data(stocks=TEST_STOCKS, days=300,
                                         data_dir=str(d), seed=42)
    return str(d), stats["start"], stats["end"]
