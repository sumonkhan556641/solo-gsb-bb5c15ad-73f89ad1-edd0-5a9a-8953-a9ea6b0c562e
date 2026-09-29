"""容量/体积累计精度回归测试(stdlib unittest;API 用例需要 fastapi,缺失时自动跳过)。

背景:容量判断、FFD 体积排序与禁配搜索的回溯/状态判重必须按输入十进制值
精确计算——恰好等于容量可装,超出(哪怕 0.00001)不可装;四位小数仅用于
输出,箱内明细、箱体占用、总货量、已装/未装体积均从未舍入的件数×单件体积
汇总后分别舍入,不得用展示值相减。

运行:python tests/test_volume_precision.py
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


def cargo(name, units, vol, lo=0, hi=8, **kw):
    return {
        "name": name,
        "units": units,
        "unit_volume": vol,
        "t_min": lo,
        "t_max": hi,
        **kw,
    }


def payload(cargoes, cap=10, max_boxes=10, forbidden=False, **kw):
    return {
        "box_capacity": cap,
        "max_boxes": max_boxes,
        "zones": ZONE,
        "forbidden_mode": forbidden,
        "cargoes": cargoes,
        **kw,
    }


class ExactCapacityNormalModeTestCase(unittest.TestCase):
    """普通 FFD 模式:容量恰等可装、逐件超出不可装。"""

    def test_three_units_033334_capacity_1_does_not_falsely_all_load(self):
        # 3 × 0.33334 = 1.00002 > 1:逐件舍入会把占用压到 0.9999 而误报全装;
        # 修复后第 3 件必须报未装载(部分装载结果保留)
        r = plan(payload([cargo("A", 3, 0.33334)], cap=1, max_boxes=1))
        s = r["summary"]
        self.assertFalse(s["all_loaded"])
        self.assertEqual(s["loaded_units"], 2)
        self.assertEqual(s["unloaded_units"], 1)
        # 占用/明细/汇总均按未舍入累计分别舍入
        self.assertEqual(r["boxes"][0]["used_volume"], 0.6667)
        self.assertEqual(r["boxes"][0]["items"][0]["units"], 2)
        self.assertEqual(r["boxes"][0]["items"][0]["volume"], 0.6667)
        self.assertEqual(s["total_cargo_volume"], 1.0)
        self.assertEqual(s["loaded_volume"], 0.6667)
        self.assertEqual(s["unloaded_volume"], 0.3333)
        self.assertEqual(r["unloaded"][0]["units"], 1)
        self.assertEqual(r["unloaded"][0]["volume"], 0.3333)

    def test_three_units_033336_capacity_100008_loads_exactly(self):
        # 3 × 0.33336 = 1.00008,恰等容量:旧逻辑逐件舍入(0.3334, 0.6668)
        # 反而在第 3 件报装不下;修复后必须全装,利用率 1.0
        r = plan(payload([cargo("A", 3, 0.33336)], cap=1.00008, max_boxes=1))
        s = r["summary"]
        self.assertTrue(s["all_loaded"])
        self.assertEqual(s["loaded_units"], 3)
        self.assertEqual(s["unloaded_units"], 0)
        self.assertEqual(r["summary"]["boxes_used"], 1)
        box = r["boxes"][0]
        self.assertEqual(box["used_volume"], 1.0001)
        self.assertEqual(box["capacity"], 1.0001)
        self.assertEqual(box["utilization"], 1.0)
        self.assertEqual(s["total_cargo_volume"], 1.0001)
        self.assertEqual(s["loaded_volume"], 1.0001)
        self.assertEqual(s["unloaded_volume"], 0.0)

    def test_exceed_by_00001_in_second_box_also_rejected(self):
        # 2 箱、4 件 0.33336:每箱容量 1.00008 恰可放 3 件,第 4 件开第二箱
        # (放宽到 2 箱时全部可装);容量改为 1.00007 则任何箱至多 2 件,
        # 2 箱只能装 4 件中的…… 4 件 0.33336:2 箱 × 容量 1.00007,
        # 每箱 2 件(0.66672),第 5 件才会触发;这里直接构造 5 件、2 箱
        r = plan(payload([cargo("A", 5, 0.33336)], cap=1.00007, max_boxes=2))
        s = r["summary"]
        # 每箱 2 件共 4 件,第 5 件未装载(3 件累计 1.00008 > 1.00007)
        self.assertEqual(s["loaded_units"], 4)
        self.assertEqual(s["unloaded_units"], 1)
        self.assertEqual(s["unloaded_volume"], 0.3334)
        self.assertEqual([b["used_volume"] for b in r["boxes"]], [0.6667, 0.6667])

    def test_display_values_not_subtracted(self):
        # 0.33333 × 3 = 0.99999,展示为 1.0000;若用展示值相减会得到未装 0.0
        # 之类的伪一致。这里容量 1、只给 1 箱却人为拆成"装 2 件"的对照:
        # 0.99999 ≤ 1,实际全装;总货量与已装均各自舍入为 1.0
        r = plan(payload([cargo("A", 3, 0.33333)], cap=1, max_boxes=1))
        s = r["summary"]
        self.assertTrue(s["all_loaded"])
        self.assertEqual(s["total_cargo_volume"], 1.0)
        self.assertEqual(s["loaded_volume"], 1.0)
        self.assertEqual(s["unloaded_volume"], 0.0)

    def test_ffd_ordering_uses_unrounded_total_volume(self):
        # B 两件总体积 0.66672 大于 A 单件 0.33336,同温限按总体积降序,B 先装;
        # 容量恰好 1.00008 时两者同箱(累计恰等容量)
        r = plan(payload([
            cargo("A", 1, 0.33336),
            cargo("B", 2, 0.33336),
        ], cap=1.00008, max_boxes=1))
        self.assertEqual(
            [(i["batch_no"], i["units"]) for i in r["boxes"][0]["items"]],
            [(2, 2), (1, 1)],
        )
        self.assertEqual(r["boxes"][0]["used_volume"], 1.0001)

    def test_box_detail_volumes_summarized_from_exact_units(self):
        # 同箱多批明细:每件体积精确累计,输出各自舍入;
        # 已装总体积由未舍入值汇总(0.33336×3 = 1.00008 → 1.0001)
        r = plan(payload([
            cargo("A", 1, 0.33336),
            cargo("B", 2, 0.33336),
        ], cap=1.00008, max_boxes=1))
        items = {i["batch_no"]: i for i in r["boxes"][0]["items"]}
        self.assertEqual(items[2]["volume"], 0.6667)
        self.assertEqual(items[1]["volume"], 0.3334)
        self.assertEqual(r["summary"]["loaded_volume"], 1.0001)


class ExactCapacityForbiddenModeTestCase(unittest.TestCase):
    """禁配精确搜索:回溯与状态判重按未舍入体积,容量恰等可装。"""

    def test_exact_capacity_single_box_finds_solution(self):
        # 3 件 0.33336 累计恰等 1.00008:旧逻辑逐件舍入使第 3 件被拒,
        # 精确搜索误判无解(400);修复后 1 箱即可全员装载
        r = plan(
            payload([cargo("A", 3, 0.33336)], cap=1.00008, max_boxes=1,
                    forbidden=True)
        )
        s = r["summary"]
        self.assertEqual(s["boxes_used"], 1)
        self.assertTrue(s["all_loaded"])
        self.assertEqual(r["boxes"][0]["used_volume"], 1.0001)
        self.assertEqual(r["forbidden_box_sequence"], [1, 1, 1])

    def test_over_capacity_single_box_returns_400_no_partial(self):
        # 3 × 0.33334 = 1.00002 > 1,1 箱无解;禁配模式返回 400 且无部分方案
        body = payload([cargo("A", 3, 0.33334)], cap=1, max_boxes=1,
                       forbidden=True)
        with self.assertRaises(ValueError) as ctx:
            plan(body)
        self.assertIn("禁配装箱模式无解", str(ctx.exception))

    def test_over_capacity_by_00001_returns_400(self):
        # 容量 1.00007 < 3×0.33336 = 1.00008,差 0.00001 也必须判无解
        body = payload([cargo("A", 3, 0.33336)], cap=1.00007, max_boxes=1,
                       forbidden=True)
        with self.assertRaises(ValueError):
            plan(body)

    def test_split_boxes_when_capacity_exceeded_keeps_minimum_boxes(self):
        # 容量 1 放 3 件 0.33334:至少 2 箱;逐件序列字典序最小 => [1,1,2]
        r = plan(
            payload([cargo("A", 3, 0.33334)], cap=1, max_boxes=3,
                    forbidden=True)
        )
        self.assertEqual(r["summary"]["boxes_used"], 2)
        self.assertEqual(r["forbidden_box_sequence"], [1, 1, 2])
        self.assertEqual(
            [b["used_volume"] for b in r["boxes"]], [0.6667, 0.3333]
        )

    def test_forbidden_pairs_with_exact_capacity_boundary(self):
        # A、B 禁配,各 2 件 0.33336;容量 0.66672(恰放 2 件):必须 2 箱,
        # 且每箱恰等容量装满
        r = plan(payload([
            {**cargo("A", 2, 0.33336), "forbidden_with": "2"},
            cargo("B", 2, 0.33336),
        ], cap=0.66672, max_boxes=3, forbidden=True))
        self.assertEqual(r["summary"]["boxes_used"], 2)
        for b in r["boxes"]:
            self.assertEqual(b["used_volume"], 0.6667)
            self.assertEqual(b["utilization"], 1.0)
        self.assertEqual(r["forbidden_box_sequence"], [1, 1, 2, 2])

    def test_memo_dedup_does_not_merge_states_differing_by_rounding(self):
        # 0.33334×2=0.66668 与 0.66668 的其他累计路径在四位展示下相同,
        # 但禁配约束随批次变化、容量判断按精确值;状态判重不得把
        # "容量边界不同"的箱态合并而漏掉可行解。
        # A、C 禁配:容量 1.00002(恰放 A×3)时必须仍能找到 2 箱方案
        r = plan(payload([
            {**cargo("A", 3, 0.33334), "forbidden_with": "3"},
            cargo("B", 1, 0.33334),
            cargo("C", 1, 0.33334),
        ], cap=1.00002, max_boxes=3, forbidden=True))
        self.assertTrue(r["summary"]["all_loaded"])
        self.assertEqual(r["summary"]["loaded_units"], 5)
        # A 的 3 件必须独占可装下 1.00002 的箱(恰等容量)
        a_box = next(
            b for b in r["boxes"]
            if {i["batch_no"] for i in b["items"]} == {1}
        )
        self.assertEqual(a_box["used_volume"], 1.0)
        self.assertEqual(a_box["utilization"], 1.0)


@unittest.skipUnless(_APP_AVAILABLE, "未安装 fastapi,跳过 HTTP 层用例")
class VolumePrecisionApiTestCase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_api_over_capacity_partial_normal_mode(self):
        resp = self.client.post("/api/plan", json=payload(
            [cargo("A", 3, 0.33334)], cap=1, max_boxes=1
        ))
        self.assertEqual(resp.status_code, 200, resp.text)
        s = resp.json()["summary"]
        self.assertFalse(s["all_loaded"])
        self.assertEqual(s["unloaded_units"], 1)
        self.assertEqual(s["unloaded_volume"], 0.3333)

    def test_api_exact_capacity_forbidden_mode(self):
        resp = self.client.post("/api/plan", json=payload(
            [cargo("A", 3, 0.33336)], cap=1.00008, max_boxes=1, forbidden=True
        ))
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["summary"]["boxes_used"], 1)
        self.assertEqual(data["boxes"][0]["used_volume"], 1.0001)

    def test_api_forbidden_over_capacity_returns_400(self):
        resp = self.client.post("/api/plan", json=payload(
            [cargo("A", 3, 0.33334)], cap=1, max_boxes=1, forbidden=True
        ))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("无解", resp.json()["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
