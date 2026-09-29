"""容量逐件舍入修复回归测试(stdlib unittest;API 用例需要 fastapi,缺失时跳过)。

覆盖两类曾被逐件四位小数舍入改变的输入:
- 容量 1、3 件各 0.33334(合计 1.00002,超出容量):普通模式不得误报全装,
  禁配模式 1 箱须判无解(400)、2 箱全员装载;
- 容量 1.00008、3 件各 0.33336(合计恰等):两种模式均须全员装入 1 箱。

同时验证:箱内明细、箱体占用、总货量、已装/未装体积均从「未舍入件数 ×
单件体积」汇总后分别四位舍入,不用展示值相减;四位小数仅用于输出。

运行:python tests/test_volume_rounding.py
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:  # 未安装 Web 依赖时仍可运行核心规则用例,API 用例自动跳过
    from fastapi.testclient import TestClient  # noqa: E402
    from app.main import app  # noqa: E402

    _APP_AVAILABLE = True
except ImportError:  # pragma: no cover - 仅极简环境触发
    TestClient = None  # type: ignore[assignment,misc]
    app = None  # type: ignore[assignment]
    _APP_AVAILABLE = False

from app.planner import plan  # noqa: E402

ZONE = [{"name": "通用", "t_min": -30, "t_max": 30}]


def body(cap, uv, *, forbidden=False, max_boxes=100, units=3):
    return {
        "box_capacity": cap,
        "max_boxes": max_boxes,
        "zones": ZONE,
        "forbidden_mode": forbidden,
        "cargoes": [
            {"name": "A", "units": units, "unit_volume": uv, "t_min": 0, "t_max": 8}
        ],
    }


class OverflowNotHiddenTestCase(unittest.TestCase):
    """容量 1、3×0.33334(合计 1.00002):逐件舍入会虚降到 0.9999 误判可装。"""

    def test_ffd_one_box_loads_two_only(self):
        r = plan(body(1, 0.33334, max_boxes=1))
        s = r["summary"]
        self.assertEqual(s["loaded_units"], 2)
        self.assertEqual(s["unloaded_units"], 1)
        self.assertFalse(s["all_loaded"])
        box = r["boxes"][0]
        # 箱内明细与箱体占用均从精确值 2×0.33334=0.66668 汇总后舍入
        self.assertEqual(box["used_volume"], 0.6667)
        self.assertEqual(box["items"][0]["units"], 2)
        self.assertEqual(box["items"][0]["volume"], 0.6667)
        # 总货量/已装/未装分别独立舍入(不用展示值相减)
        self.assertEqual(s["total_cargo_volume"], 1.0)
        self.assertEqual(s["loaded_volume"], 0.6667)
        self.assertEqual(s["unloaded_volume"], 0.3333)
        self.assertEqual(r["unloaded"][0]["volume"], 0.3333)

    def test_ffd_two_boxes_loads_all_with_partial_first_box(self):
        # 保留 FFD 顺序与部分装载结果:首箱 2 件、第二箱 1 件
        r = plan(body(1, 0.33334, max_boxes=2))
        s = r["summary"]
        self.assertEqual(s["boxes_used"], 2)
        self.assertEqual(s["loaded_units"], 3)
        self.assertTrue(s["all_loaded"])
        self.assertEqual(r["boxes"][0]["used_volume"], 0.6667)
        self.assertEqual(r["boxes"][1]["used_volume"], 0.3333)

    def test_forbidden_one_box_is_infeasible(self):
        with self.assertRaises(ValueError) as ctx:
            plan(body(1, 0.33334, forbidden=True, max_boxes=1))
        self.assertIn("禁配装箱模式无解", str(ctx.exception))

    def test_forbidden_two_boxes_sequence(self):
        r = plan(body(1, 0.33334, forbidden=True, max_boxes=2))
        self.assertEqual(r["summary"]["boxes_used"], 2)
        self.assertEqual(r["forbidden_box_sequence"], [1, 1, 2])


class ExactFitTestCase(unittest.TestCase):
    """容量 1.00008、3×0.33336(合计恰等):逐件舍入会虚增到 1.0001 误拒。"""

    def test_ffd_all_three_fit_exactly(self):
        r = plan(body(1.00008, 0.33336, max_boxes=1))
        s = r["summary"]
        self.assertEqual(s["loaded_units"], 3)
        self.assertEqual(s["unloaded_units"], 0)
        self.assertTrue(s["all_loaded"])
        box = r["boxes"][0]
        self.assertEqual(s["boxes_used"], 1)
        self.assertEqual(box["used_volume"], 1.0001)
        self.assertEqual(box["capacity"], 1.0001)
        self.assertEqual(box["utilization"], 1.0)
        self.assertEqual(box["items"][0]["volume"], 1.0001)
        self.assertEqual(s["total_cargo_volume"], 1.0001)
        self.assertEqual(s["loaded_volume"], 1.0001)
        self.assertEqual(s["unloaded_volume"], 0.0)

    def test_forbidden_all_three_fit_one_box(self):
        r = plan(body(1.00008, 0.33336, forbidden=True, max_boxes=1))
        self.assertEqual(r["summary"]["boxes_used"], 1)
        self.assertEqual(r["forbidden_box_sequence"], [1, 1, 1])

    def test_exact_equality_is_loadable(self):
        # 容量恰等可装(Decimal 精确比较,不依赖浮点容差):2×0.1=0.2
        r = plan(body(0.2, 0.1, max_boxes=1, units=2))
        self.assertTrue(r["summary"]["all_loaded"])
        self.assertEqual(r["boxes"][0]["utilization"], 1.0)

    def test_tiny_overflow_rejected(self):
        # 超出不可装:0.33334×3=1.00002,容量 1.00001 仍不够
        r = plan(body(1.00001, 0.33334, max_boxes=1))
        self.assertEqual(r["summary"]["loaded_units"], 2)
        self.assertEqual(r["summary"]["unloaded_units"], 1)


class IndependentRoundingTestCase(unittest.TestCase):
    def test_each_aggregate_rounded_separately(self):
        # 单件 0.00005(展示 0.0001),容量只能装 1 件:总/已装/未装各 0.0001,
        # 三者都是先精确汇总再分别舍入,不存在展示值相减得到 0.0
        r = plan(body(0.00005, 0.00005, max_boxes=1, units=2))
        s = r["summary"]
        self.assertEqual(s["total_cargo_volume"], 0.0001)
        self.assertEqual(s["loaded_volume"], 0.0001)
        self.assertEqual(s["unloaded_volume"], 0.0001)

    def test_multi_batch_used_volume_from_exact_sum(self):
        # 0.66666 + 0.33334 = 1.00000 恰等容量,精确十进制下可装
        r = plan({
            "box_capacity": 1,
            "max_boxes": 1,
            "zones": ZONE,
            "cargoes": [
                {"name": "A", "units": 1, "unit_volume": 0.33334,
                 "t_min": 0, "t_max": 8},
                {"name": "B", "units": 2, "unit_volume": 0.33333,
                 "t_min": 0, "t_max": 8},
            ],
        })
        self.assertTrue(r["summary"]["all_loaded"])
        self.assertEqual(r["boxes"][0]["used_volume"], 1.0)


@unittest.skipUnless(_APP_AVAILABLE, "未安装 fastapi,跳过 API 用例")
class VolumeRoundingApiTestCase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_api_overflow_not_all_loaded(self):
        resp = self.client.post("/api/plan", json=body(1, 0.33334, max_boxes=1))
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["summary"]["loaded_units"], 2)
        self.assertFalse(data["summary"]["all_loaded"])

    def test_api_exact_fit_all_loaded(self):
        resp = self.client.post("/api/plan", json=body(1.00008, 0.33336, max_boxes=1))
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["summary"]["loaded_units"], 3)
        self.assertTrue(data["summary"]["all_loaded"])

    def test_api_forbidden_overflow_one_box_400(self):
        resp = self.client.post(
            "/api/plan", json=body(1, 0.33334, forbidden=True, max_boxes=1)
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("禁配装箱模式无解", resp.json()["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
