"""上下游时差（候选提前量）分析的自测。

覆盖：
- 皮尔逊相关系数与无波动处理；
- 候选时差 0~max_lag 的配对规则、共同可配对上游小时、并列时差；
- 不足 8 对、无波动、最高系数不大于 0 的无法判断；
- 网页/API 同结果、补传与查询交错的同快照读取、修订更新结论、失败上传不变；
- 参数校验。

运行：python3 -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import core, db  # noqa: E402
from app.app import create_app  # noqa: E402

H = core.HOUR_SECONDS


def epoch(y, mo, d, h):
    return int(datetime(y, mo, d, h, tzinfo=timezone.utc).timestamp())


T00 = epoch(2026, 11, 18, 0)


def rain_map(base_epoch, values):
    """[雨量...] -> {hour_epoch: rainfall_mm}。"""
    return {base_epoch + i * H: float(v) for i, v in enumerate(values)}


class PearsonTests(unittest.TestCase):
    def test_perfect_and_negative(self):
        x = [1.0, 2, 3, 4, 5, 6, 7, 8]
        self.assertAlmostEqual(core.pearson(x, x), 1.0)
        self.assertAlmostEqual(core.pearson(x, [9 - v for v in x]), -1.0)
        r = core.pearson(x, [2.0 * v + 3 for v in x])
        self.assertAlmostEqual(r, 1.0)

    def test_known_value(self):
        # 经典样例：x=1..5, y=[2,4,5,4,5]，r ≈ 0.7746
        r = core.pearson([1, 2, 3, 4, 5], [2, 4, 5, 4, 5])
        self.assertAlmostEqual(r, 0.7745966692, places=6)

    def test_zero_variance_undefined(self):
        x = [1.0, 2, 3, 4, 5, 6, 7, 8]
        self.assertIsNone(core.pearson(x, [5.0] * 8))       # 下游无波动
        self.assertIsNone(core.pearson([0.0] * 8, x))       # 上游无波动
        self.assertIsNone(core.pearson([3.0] * 8, [3.0] * 8))


class AnalyzeLagCoreTests(unittest.TestCase):
    def setUp(self):
        # 12 个窗口小时；max_lag=3 -> 共同上游小时为前 9 个（>=8）
        self.start, self.end = T00, T00 + 11 * H
        self.up = rain_map(self.start, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12])

    def test_best_lag_detected(self):
        # 非线性上游序列，避免线性序列导致多时差并列
        up_vals = [3, 1, 4, 1, 5, 9, 2, 6, 5, 3, 5, 8]
        self.up = rain_map(self.start, up_vals)
        # 下游序列 = 上游延后 2 小时：down[t] = up[t-2]
        down_vals = [99, 98] + up_vals[:-2]
        down = rain_map(self.start, down_vals)
        r = core.analyze_lag(self.up, down, self.start, self.end, 3)
        self.assertEqual(r["conclusion"], "可判断")
        self.assertEqual(r["best_lags"], [2])
        self.assertFalse(r["is_tie"])
        self.assertAlmostEqual(r["best_coefficient"], 1.0, places=4)
        # 各候选配对集合相同：均为 9 对
        self.assertEqual({c["pair_count"] for c in r["candidates"]}, {9})
        self.assertEqual(r["common_pair_count"], 9)
        # 配对方向正确：lag=2 时上游 h 对下游 h+2h
        c2 = next(c for c in r["candidates"] if c["lag_hours"] == 2)
        p0 = c2["pairs"][0]
        self.assertEqual(p0["upstream_hour"], core.iso_utc(self.start))
        self.assertEqual(p0["downstream_hour"], core.iso_utc(self.start + 2 * H))

    def test_tie_reports_all_lags(self):
        # 上、下游都是等差数列 -> 每个时差的相关系数都为 1.0，全部并列
        down = rain_map(self.start, [5 + i for i in range(12)])
        r = core.analyze_lag(self.up, down, self.start, self.end, 3)
        self.assertEqual(r["conclusion"], "可判断")
        self.assertEqual(r["best_lags"], [0, 1, 2, 3])
        self.assertTrue(r["is_tie"])

    def test_insufficient_pairs(self):
        # 窗口太短：max_lag=3 时共同上游小时最多 4 个 < 8
        short_end = self.start + 6 * H
        up = rain_map(self.start, [1, 2, 3, 4, 5, 6, 7])
        down = rain_map(self.start, [2, 3, 4, 5, 6, 7, 8])
        r = core.analyze_lag(up, down, self.start, short_end, 3)
        self.assertEqual(r["conclusion"], "无法判断")
        self.assertEqual(r["reason"], "insufficient_pairs")
        self.assertTrue(all(c["coefficient"] is None for c in r["candidates"]))
        self.assertEqual(r["candidates"][0]["pair_count"], 4)

    def test_no_variation(self):
        down = rain_map(self.start, [7.0] * 12)  # 下游恒定
        r = core.analyze_lag(self.up, down, self.start, self.end, 2)
        self.assertEqual(r["conclusion"], "无法判断")
        self.assertEqual(r["reason"], "no_variation")
        self.assertTrue(all(not c["defined"] for c in r["candidates"]))

    def test_non_positive_coefficient(self):
        # 下游与上游完全反向 -> 所有系数为负
        down = rain_map(self.start, [13 - i for i in range(12)])
        r = core.analyze_lag(self.up, down, self.start, self.end, 2)
        self.assertEqual(r["conclusion"], "无法判断")
        self.assertEqual(r["reason"], "non_positive")
        self.assertIsNone(r["best_coefficient"])

    def test_common_hours_require_all_lags_pairable(self):
        # 删掉下游第 5 个窗口小时：会使 lag 0..3 中部分候选缺对，
        # 该小时及其前 3 个上游小时都不应进入共同集合
        down = rain_map(self.start, [5 + i for i in range(12)])
        del down[self.start + 4 * H]
        r = core.analyze_lag(self.up, down, self.start, self.end, 3)
        common = set(r["common_upstream_hours"])
        for bad in range(1, 5):  # 上游 h1..h4 都受影响
            self.assertNotIn(core.iso_utc(self.start + bad * H), common)
        self.assertIn(core.iso_utc(self.start), common)
        # 所有候选使用完全相同的共同小时集合（pair_count 一致）
        self.assertTrue(all(
            c["pair_count"] == r["common_pair_count"] for c in r["candidates"]
        ))

    def test_max_lag_validation(self):
        with self.assertRaises(ValueError):
            core.analyze_lag(self.up, self.up, self.start, self.end, 0)
        with self.assertRaises(ValueError):
            core.analyze_lag(self.up, self.up, self.start, self.end, 7)

    def test_lag_zero_included(self):
        r = core.analyze_lag(self.up, self.up, self.start, self.end, 1)
        self.assertEqual([c["lag_hours"] for c in r["candidates"]], [0, 1])


class LagAppTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "lag.db")
        self.app = create_app(self.path)
        self.client = self.app.test_client()
        self.start = epoch(2026, 11, 18, 0)
        self.end = epoch(2026, 11, 18, 13)   # 14 个窗口小时

    def _upload_station(self, station, hourly_pulses, revision=1):
        """按每小时脉冲序列生成连续分钟计数器并上传（主计）。"""
        lines = []
        counter = 0
        minute = self.start
        for pulses in hourly_pulses:
            # 小时起点计数器（首行含起始整点），随后每分钟 +pulses（恒定速率）
            lines.append(
                f"{station},primary,{core.iso_utc(minute)},{counter % 10000},{revision}"
            )
            for _ in range(60):
                counter += pulses
                minute += core.MINUTE_SECONDS
                lines.append(
                    f"{station},primary,{core.iso_utc(minute)},{counter % 10000},{revision}"
                )
        resp = self.client.post("/api/upload", data={"csv_text": "\n".join(lines)})
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def _lag(self, up="UP", down="DOWN", max_lag=3, start=None, end=None):
        start = start or self.start
        end = end or self.end
        url = (
            f"/api/lag?upstream={up}&downstream={down}"
            f"&start={core.iso_utc(start)}&end={core.iso_utc(end)}&max_lag={max_lag}"
        )
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        return resp.get_json()

    def test_end_to_end_best_lag_and_web_api_match(self):
        up_pulses = [5, 0, 8, 2, 9, 1, 7, 3, 6, 4, 8, 2, 5, 1]
        # 下游 = 上游延后 2 小时（前两小时补常数值）
        down_pulses = [1, 1] + up_pulses[:-2]
        self._upload_station("UP", up_pulses)
        self._upload_station("DOWN", down_pulses)

        r = self._lag()
        self.assertEqual(r["conclusion"], "可判断")
        self.assertEqual(r["best_lags"], [2])
        self.assertEqual(r["common_pair_count"], 11)  # 14 - max_lag
        for c in r["candidates"]:
            self.assertEqual(c["pair_count"], 11)
            self.assertTrue(c["defined"])
            self.assertEqual(len(c["pairs"]), 11)

        # 网页与 API 同入口同结果
        html = self.client.get(
            f"/lag?upstream=UP&downstream=DOWN"
            f"&start={core.iso_utc(self.start)}&end={core.iso_utc(self.end)}&max_lag=3"
        ).get_data(as_text=True)
        self.assertIn("可判断", html)
        self.assertIn("2 小时", html)
        self.assertIn("%.4f" % r["best_coefficient"], html)

    def test_revision_changes_conclusion_on_requery(self):
        up_pulses = [5, 0, 8, 2, 9, 1, 7, 3, 6, 4, 8, 2, 5, 1]
        down_pulses = [1, 1] + up_pulses[:-2]
        self._upload_station("UP", up_pulses)
        self._upload_station("DOWN", down_pulses)
        before = self._lag()
        self.assertEqual(before["best_lags"], [2])

        # 修订上游首个窗口小时的整点读数（修订号 2），改变该小时雨量
        rev_line = f"UP,primary,{core.iso_utc(self.start)},50,2"
        rep = self.client.post("/api/upload", data={"csv_text": rev_line}).get_json()
        self.assertEqual(rep["accepted_count"], 1)
        self.assertEqual(rep["accepted"][0]["action"], "updated")

        after = self._lag()
        self.assertEqual(after["conclusion"], "可判断")
        self.assertNotEqual(before["candidates"], after["candidates"])  # 结论数据已更新
        # 配对集合本身不变（两站各小时仍可供数）
        self.assertEqual(after["common_pair_count"], before["common_pair_count"])

    def test_stale_revision_and_failed_upload_leave_result_unchanged(self):
        up_pulses = [5, 0, 8, 2, 9, 1, 7, 3, 6, 4, 8, 2, 5, 1]
        down_pulses = [1, 1] + up_pulses[:-2]
        self._upload_station("UP", up_pulses)
        self._upload_station("DOWN", down_pulses)
        before = self._lag()

        # 低修订号被拒绝
        stale = f"UP,primary,{core.iso_utc(self.start)},99,1"
        rep = self.client.post("/api/upload", data={"csv_text": stale}).get_json()
        self.assertEqual(rep["accepted_count"], 0)
        # 文件级失败上传（缺列）整体拒收
        bad = self.client.post(
            "/api/upload", data={"csv_text": "station,device,minute,counter\n"}
        )
        self.assertEqual(bad.status_code, 400)
        # 完全空上传
        empty = self.client.post("/api/upload", data={})
        self.assertEqual(empty.status_code, 400)

        self.assertEqual(self._lag(), before)

    def test_incomplete_hour_excluded_from_pairs(self):
        up_pulses = [5, 0, 8, 2, 9, 1, 7, 3, 6, 4, 8, 2, 5, 1]
        down_pulses = [1, 1] + up_pulses[:-2]
        self._upload_station("UP", up_pulses)
        self._upload_station("DOWN", down_pulses)

        # 删掉下游第 6 小时的一个分钟端点 -> 该小时不可供数
        miss = self.start + 5 * H + 30 * core.MINUTE_SECONDS
        # 直接在库中删除该读数（模拟补传缺口）
        conn = db.connect(self.path)
        try:
            conn.execute("DELETE FROM readings WHERE station=? AND minute_epoch=?",
                         ("DOWN", miss))
        finally:
            conn.close()

        r = self._lag()
        # 共同上游小时减少（h2..h5 受影响），但仍 >=8 时仍可判断
        self.assertLess(r["common_pair_count"], 11)
        self.assertNotIn(
            core.iso_utc(self.start + 5 * H), r["common_upstream_hours"]
        )
        for c in r["candidates"]:
            self.assertEqual(c["pair_count"], r["common_pair_count"])

    def test_insufficient_pairs_through_app(self):
        # 只上传 5 个小时，max_lag=3 -> 共同配对最多 2 对
        self._upload_station("UP", [1, 2, 3, 4, 5])
        self._upload_station("DOWN", [2, 3, 4, 5, 6])
        r = self._lag(end=self.start + 4 * H)
        self.assertEqual(r["conclusion"], "无法判断")
        self.assertEqual(r["reason"], "insufficient_pairs")
        html = self.client.get(
            f"/lag?upstream=UP&downstream=DOWN"
            f"&start={core.iso_utc(self.start)}&end={core.iso_utc(self.start + 4 * H)}&max_lag=3"
        ).get_data(as_text=True)
        self.assertIn("无法判断", html)
        self.assertIn("不足 8 对", html)

    def test_snapshot_consistency_under_interleaving(self):
        up_pulses = [5, 0, 8, 2, 9, 1, 7, 3, 6, 4, 8, 2, 5, 1]
        down_pulses = [1, 1] + up_pulses[:-2]
        self._upload_station("UP", up_pulses)
        self._upload_station("DOWN", down_pulses)

        # 快照事务期间另一个连接提交修订，事务内查询不应看到新状态
        conn = db.connect(self.path)
        try:
            with db.snapshot_read(conn):
                before_counters = db.fetch_range_counters(
                    conn, "UP", self.start, self.end + H
                )
                # 用另一连接并发提交
                other = db.connect(self.path)
                try:
                    reading = core.Reading(
                        "UP", "primary", self.start, 77, 2
                    )
                    db.apply_batch(other, [(1, "raw", reading)], [], "rev.csv")
                finally:
                    other.close()
                after_counters = db.fetch_range_counters(
                    conn, "UP", self.start, self.end + H
                )
                self.assertEqual(before_counters, after_counters)
        finally:
            conn.close()
        # 事务结束后新查询能看到修订
        fresh = db.connect(self.path)
        try:
            self.assertEqual(
                db.fetch_hour_counters(fresh, "UP", self.start)[
                    ("primary", self.start)
                ],
                77,
            )
        finally:
            fresh.close()

    def test_param_validation(self):
        base = (
            f"start={core.iso_utc(self.start)}&end={core.iso_utc(self.end)}"
        )
        # 同一站点
        resp = self.client.get(f"/api/lag?upstream=A&downstream=A&{base}&max_lag=3")
        self.assertEqual(resp.status_code, 400)
        # max_lag 越界
        resp = self.client.get(
            f"/api/lag?upstream=A&downstream=B&{base}&max_lag=7"
        )
        self.assertEqual(resp.status_code, 400)
        # max_lag 非整数
        resp = self.client.get(
            f"/api/lag?upstream=A&downstream=B&{base}&max_lag=x"
        )
        self.assertEqual(resp.status_code, 400)
        # 缺少站点
        resp = self.client.get(f"/api/lag?{base}&max_lag=3")
        self.assertEqual(resp.status_code, 400)
        # 非整点
        resp = self.client.get(
            f"/api/lag?upstream=A&downstream=B&start=2026-11-18T00:30Z"
            f"&end={core.iso_utc(self.end)}&max_lag=3"
        )
        self.assertEqual(resp.status_code, 400)
        # 网页表单错误返回 400 且带 flash
        resp = self.client.get(f"/lag?upstream=A&downstream=A&{base}&max_lag=3")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("查询参数无效", resp.get_data(as_text=True))

    def test_lag_form_renders_without_params(self):
        resp = self.client.get("/lag")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("上下游降雨时差分析", resp.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
