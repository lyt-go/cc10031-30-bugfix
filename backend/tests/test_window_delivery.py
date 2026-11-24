"""窗口归并 -> 序列交付的对账测试。

覆盖题目要求的错误形态：
- 按测点 A 申请 10:00~11:00，绝不能夹带测点 B 或 11:00 以后的观测；
- 每个归并窗口必须给出实际起止与纳入观测出处；
- 跨界滑窗只有整窗均值时不能拆，补报逐点原件后才能按区间重算；
- 交付文件与排除明细逐条可回查，三类排除分别计数并与源输入对平；
- 相邻两次申请切换时，上一份申请的外测点数据不得残留。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.window_utils import parse_time_range


@pytest.fixture(autouse=True)
def data_root():
    with tempfile.TemporaryDirectory() as tmp:
        import app.services.rollup_window as rw
        import app.services.series_export as se
        with patch("app.services.base.DATA_ROOT", Path(tmp)):
            rw.DATA_ROOT = Path(tmp)
            se.DATA_ROOT = Path(tmp)
            yield Path(tmp)


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _per_point(path: Path, point: str, start_hour: int, minutes: int,
               base_value: float) -> None:
    _write(path, [
        {"point": point,
         "timestamp": f"2026-09-01T{start_hour:02d}:{minute:02d}:00",
         "calibrated_value_V": round(base_value + offset, 4)}
        for offset, minute in enumerate(minutes)
    ])


def _upsert(module: str, row: dict) -> None:
    """种子数据也带同号样例行，这里按 id 覆盖，保证用例不受种子影响。"""
    from app.store import store
    rows = store.rows(module)
    for index, existing in enumerate(rows):
        if int(existing.get("id", 0)) == int(row["id"]):
            rows[index] = row
            return
    rows.append(row)


def _add_rollup(row_id: int, key: str, point: str, window_type: str,
                extra: dict | None = None) -> None:
    row = {
        "id": row_id, "窗口编号": key, "测点编号": point,
        "窗口类型": window_type, "窗口长度": "5", "滑动步长": "1",
        "聚合指标": "均值", "窗口条数": "0", "归并水位": "0",
        "status": "待归并", "pending": True, "abnormal": False,
    }
    row.update(extra or {})
    _upsert("rollup_window", row)


def _add_export(row_id: int, key: str, points: str, window: str) -> None:
    _upsert("series_export", {
        "id": row_id, "导出编号": key, "测点范围": points, "时间范围": window,
        "导出格式": "jsonl", "采样粒度": "1s", "记录条数": "0", "分片大小": "0",
        "导出水位": "0", "status": "待导出", "pending": True, "abnormal": False,
    })


def _deliveries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _exclusions(path: Path) -> list[dict]:
    return [row for row in _deliveries(path)]


class TestWindowProvenance:
    """归并工作台必须给出实际起止和观测出处，且只纳入本测点观测。"""

    def test_fixed_window_records_bounds_and_provenance(self, data_root):
        from app.services.rollup_window import RollupWindowService

        _per_point(data_root / "calibrated" / "SENS-A_calibrated.jsonl",
                   "SENS-A", 10, range(5), 10.0)
        # 同一目录里混入测点 B 的观测，归并 A 时不得夹带。
        _per_point(data_root / "calibrated" / "SENS-A_calibrated.jsonl",
                   "SENS-A", 10, range(5), 10.0)
        _write(data_root / "calibrated" / "SENS-A_calibrated.jsonl", [
            {"point": "SENS-A", "timestamp": f"2026-09-01T10:0{i}:00",
             "calibrated_value_V": 10.0 + i} for i in range(5)
        ] + [
            {"point": "SENS-B", "timestamp": f"2026-09-01T10:0{i}:00",
             "calibrated_value_V": 90.0 + i} for i in range(5)
        ])
        _add_rollup(901, "ROLL-A-FIX", "SENS-A", "固定窗口",
                    {"源文件": "SENS-A_calibrated.jsonl"})

        result = RollupWindowService().rollup_windows(901)
        assert result["window_period_count"] == 1
        window = result["windows"][0]
        assert window["actual_start"] == "2026-09-01T10:00:00"
        assert window["actual_end"] == "2026-09-01T10:04:00"
        assert window["split_basis"] == "逐点原件"
        assert window["rollup_value"] == 12.0
        assert {item["point"] for item in window["observations"]} == {"SENS-A"}
        assert {item["source_file"] for item in window["provenance"]} == {
            "SENS-A_calibrated.jsonl"}

    def test_sliding_window_keeps_single_identity(self, data_root):
        """补报原件后滑窗身份不变：起止沿用登记值，只补逐点证据。"""
        from app.services.rollup_window import RollupWindowService

        _write(data_root / "downsampled" / "DOWN-A-SLIDE_downsampled.jsonl", [{
            "point": "SENS-A",
            "actual_start": "2026-09-01T10:58:00",
            "actual_end": "2026-09-01T11:06:00",
            "rollup_value": 23.9, "metric": "均值",
        }])
        _add_rollup(902, "ROLL-A-SLIDE", "SENS-A", "滑动窗口",
                    {"源文件": "DOWN-A-SLIDE_downsampled.jsonl"})
        svc = RollupWindowService()
        before = svc.rollup_windows(902)["windows"][0]
        assert before["split_basis"] == "整窗聚合"
        assert before["observations"] == []

        originals = [
            {"timestamp": f"2026-09-01T10:{minute:02d}:00", "value": 20.0}
            for minute in (58, 59)
        ] + [
            {"timestamp": f"2026-09-01T11:{minute:02d}:00", "value": float(minute)}
            for minute in range(7)
        ]
        svc.report_originals(902, observations=originals)
        after = svc.rollup_windows(902)["windows"][0]
        assert after["window_ref"] == before["window_ref"] == "ROLL-A-SLIDE#1"
        assert (after["actual_start"], after["actual_end"]) == \
            ("2026-09-01T10:58:00", "2026-09-01T11:06:00")
        assert after["split_basis"] == "逐点原件"
        assert len(after["observations"]) == 9


class TestDeliveryReconciliation:
    """交付入口按申请测点与区间对账。"""

    def _prepare(self, data_root: Path) -> None:
        # 测点 A：合法固定窗 + 跨界滑窗（起初只有整窗均值）+ 完全超范围窗
        _write(data_root / "calibrated" / "SENS-A_calibrated.jsonl", [
            {"point": "SENS-A", "timestamp": f"2026-09-01T10:0{i}:00",
             "calibrated_value_V": 10.0 + i} for i in range(5)
        ])
        _write(data_root / "downsampled" / "DOWN-A-SLIDE_downsampled.jsonl", [{
            "point": "SENS-A",
            "actual_start": "2026-09-01T10:58:00",
            "actual_end": "2026-09-01T11:06:00",
            "rollup_value": 23.9, "metric": "均值",
        }])
        _write(data_root / "downsampled" / "DOWN-A-OUT_downsampled.jsonl", [{
            "point": "SENS-A",
            "actual_start": "2026-09-01T12:00:00",
            "actual_end": "2026-09-01T12:10:00",
            "rollup_value": 99.0, "metric": "均值",
        }])
        # 测点 B：窗口时间落在申请区间里，但测点不属于申请范围
        _per_point(data_root / "calibrated" / "SENS-B_calibrated.jsonl",
                   "SENS-B", 10, range(10, 13), 50.0)

        _add_rollup(901, "ROLL-A-FIX", "SENS-A", "固定窗口",
                    {"源文件": "SENS-A_calibrated.jsonl"})
        _add_rollup(902, "ROLL-A-SLIDE", "SENS-A", "滑动窗口",
                    {"源文件": "DOWN-A-SLIDE_downsampled.jsonl"})
        _add_rollup(903, "ROLL-A-OUT", "SENS-A", "固定窗口",
                    {"源文件": "DOWN-A-OUT_downsampled.jsonl"})
        _add_rollup(904, "ROLL-B", "SENS-B", "固定窗口",
                    {"源文件": "SENS-B_calibrated.jsonl"})

        from app.services.rollup_window import RollupWindowService
        service = RollupWindowService()
        for rollup_id in (901, 902, 903, 904):
            assert "error" not in service.rollup_windows(rollup_id)

        _add_export(801, "EXP-1", "SENS-A", "2026-09-01T10:00:00~2026-09-01T11:00:00")
        _add_export(802, "EXP-2", "SENS-A", "2026-09-01T11:00:00~2026-09-01T12:00:00")

    def test_accepted_before_supplement_keeps_unsplittable_exclusion(self, data_root):
        self._prepare(data_root)
        from app.services.series_export import SeriesExportService
        from app.store import store

        svc = SeriesExportService()
        accepted = svc.start_export(801)
        assert accepted["status"] == "导出中"
        assert accepted["summary"]["excluded_by_reason"] == {
            "无关测点": 1, "完全超界": 1, "缺拆分依据": 1,
        }
        delivery_refs = {item["window_ref"] for item in accepted["deliveries"]}
        assert "ROLL-A-SLIDE#1" not in delivery_refs
        assert "ROLL-B#1" not in delivery_refs
        # 受理阶段不生成交付结果文件，状态停在导出中。
        assert not (data_root / "exports" / "EXP-1_export.jsonl").exists()
        assert store.find("series_export", 801)["status"] == "导出中"

        # 整窗均值 23.9 绝不能被当成 10:00~11:00 的区间平均交付。
        assert all(item["value"] != 23.9 for item in accepted["deliveries"])

    def test_supplement_then_confirm_splits_crossing_window(self, data_root):
        self._prepare(data_root)
        from app.services.rollup_window import RollupWindowService
        from app.services.series_export import SeriesExportService

        export_svc = SeriesExportService()
        export_svc.start_export(801)

        # 受理之后、确认之前补报真正的逐点原件。
        originals = [
            {"timestamp": f"2026-09-01T10:{minute:02d}:00", "value": 20.0}
            for minute in (58, 59)
        ] + [
            {"timestamp": f"2026-09-01T11:{minute:02d}:00", "value": float(minute)}
            for minute in range(7)
        ]
        supplement = RollupWindowService().report_originals(902, observations=originals)
        assert supplement["originals_ingested"] == 9

        confirmed = export_svc.confirm_delivery(801)
        assert confirmed["status"] == "已交付"
        by_ref = {item["window_ref"]: item for item in confirmed["deliveries"]}

        # 跨界滑窗在 10:00~11:00 内只交 10:58、10:59 两条观测。
        partial = by_ref["ROLL-A-SLIDE#1"]
        assert partial["delivered_count"] == 2
        assert partial["delivered_start"] == "2026-09-01T10:58:00"
        assert partial["delivered_end"] == "2026-09-01T10:59:00"
        assert partial["partial"] is True
        assert partial["value"] == 20.0
        assert partial["split_basis"] == "逐点原件"
        assert {item["timestamp"] for item in partial["provenance"]} == {
            "2026-09-01T10:58:00", "2026-09-01T10:59:00"}
        # 11 点以后的观测不得出现在这一份交付里。
        assert all("T11:" not in item["timestamp"] for item in partial["provenance"])

        # 补报通过后不再有「缺拆分依据」，排除只剩无关测点与完全超界。
        assert confirmed["summary"]["excluded_by_reason"] == {
            "无关测点": 1, "完全超界": 1, "缺拆分依据": 0,
        }
        # 与源输入对平：源窗口 = 交付 + 拒收。
        assert confirmed["summary"]["source_windows"] == 4
        assert confirmed["summary"]["reconciled"] is True

    def test_adjacent_request_gets_the_other_half(self, data_root):
        self._prepare(data_root)
        from app.services.rollup_window import RollupWindowService
        from app.services.series_export import SeriesExportService

        export_svc = SeriesExportService()
        export_svc.start_export(801)
        originals = [
            {"timestamp": f"2026-09-01T10:{minute:02d}:00", "value": 20.0}
            for minute in (58, 59)
        ] + [
            {"timestamp": f"2026-09-01T11:{minute:02d}:00", "value": float(minute)}
            for minute in range(7)
        ]
        RollupWindowService().report_originals(902, observations=originals)
        first = export_svc.confirm_delivery(801)

        second = export_svc.export_series(802)
        second_ref = {item["window_ref"]: item for item in second["deliveries"]}
        assert "ROLL-A-SLIDE#1" in second_ref
        remainder = second_ref["ROLL-A-SLIDE#1"]
        assert remainder["delivered_count"] == 7
        assert remainder["delivered_start"] == "2026-09-01T11:00:00"
        assert remainder["delivered_end"] == "2026-09-01T11:06:00"

        # 两次申请对同一跨界窗口的观测互不重叠、合起来正好是整窗 9 条。
        first_ref = {item["window_ref"]: item for item in first["deliveries"]}
        first_stamps = {item["timestamp"] for item in first_ref["ROLL-A-SLIDE#1"]["provenance"]}
        second_stamps = {item["timestamp"] for item in remainder["provenance"]}
        assert first_stamps.isdisjoint(second_stamps)
        assert len(first_stamps | second_stamps) == 9
        # 第二份申请不得包含第一份的固定窗，也不得包含测点 B。
        assert "ROLL-A-FIX#1" not in second_ref
        assert "ROLL-B#1" not in second_ref
        assert second["summary"]["excluded_by_reason"]["无关测点"] == 1

    def test_delivery_files_do_not_keep_foreign_residue(self, data_root):
        self._prepare(data_root)
        from app.services.series_export import SeriesExportService

        svc = SeriesExportService()
        svc.export_series(801)
        first_rows = _deliveries(data_root / "exports" / "EXP-1_export.jsonl")
        assert first_rows
        assert {row["point"] for row in first_rows} == {"SENS-A"}

        svc.export_series(802)
        # 相邻窗口的交付文件独立，第一份文件不被追加，第二份也不含第一份内容。
        assert _deliveries(data_root / "exports" / "EXP-1_export.jsonl") == first_rows
        second_rows = _deliveries(data_root / "exports" / "EXP-2_export.jsonl")
        # 此时跨界窗口还没有逐点原件，不能在 11:00 之后凭空交出一条记录。
        assert second_rows == []
        assert all(row["window_ref"] != "ROLL-A-FIX#1" for row in second_rows)

        # 同一任务重复交付时重写而不是追加，不会残留上一轮的外测点行。
        svc.confirm_delivery(801)
        assert _deliveries(data_root / "exports" / "EXP-1_export.jsonl") == first_rows

    def test_exclusion_file_is_independent_and_counted(self, data_root):
        self._prepare(data_root)
        from app.services.series_export import SeriesExportService

        svc = SeriesExportService()
        result = svc.export_series(801)
        exclusions = _exclusions(data_root / "exports" / "EXP-1_exclusions.jsonl")
        assert len(exclusions) == result["summary"]["excluded_windows"]
        reasons = {row["window_ref"]: row["reason"] for row in exclusions}
        assert reasons["ROLL-B#1"] == "无关测点"
        assert reasons["ROLL-A-OUT#1"] == "完全超界"
        assert reasons["ROLL-A-SLIDE#1"] == "缺拆分依据"
        # 排除记录也带实际起止，便于回查来源。
        for row in exclusions:
            assert row["window_actual_start"] or row["reason"] == "缺拆分依据"
            assert row["detail"]


class TestTimeRangeParsing:
    def test_supported_forms(self):
        start, end, basis = parse_time_range("2026-09-01")
        assert (start.strftime("%H:%M"), end.strftime("%H:%M"), basis) == ("00:00", "00:00", "date")
        assert end.day == 2

        start, end, _ = parse_time_range("2026-09-01T10:00:00~2026-09-01T11:00:00")
        assert start.strftime("%H") == "10" and end.strftime("%H") == "11"

        start, end, basis = parse_time_range("10:00-11:00")
        assert (start.strftime("%H:%M"), end.strftime("%H:%M"), basis) == \
            ("10:00", "11:00", "time")

        start, end, _ = parse_time_range("2026-09-01 10:00:00 至 2026-09-01 11:00:00")
        assert start.hour == 10 and end.hour == 11

        assert parse_time_range("") == (None, None, "empty")
        assert parse_time_range("不是时间")[2] == "unparsed"

    def test_time_only_request_matches_same_clock_window(self, data_root):
        """申请写成「10:00-11:00」时，对账按每天的时刻比较。"""
        from app.services.rollup_window import RollupWindowService
        from app.services.series_export import SeriesExportService
        from tests.test_window_delivery import TestDeliveryReconciliation

        TestDeliveryReconciliation._prepare(None, data_root)
        originals = [{"timestamp": "2026-09-01T10:59:00", "value": 20.0}] + [
            {"timestamp": f"2026-09-01T11:{minute:02d}:00", "value": float(minute)}
            for minute in range(7)
        ]
        RollupWindowService().report_originals(902, observations=originals)
        _add_export(803, "EXP-3", "SENS-A", "10:00-11:00")

        result = SeriesExportService().export_series(803)
        by_ref = {item["window_ref"]: item for item in result["deliveries"]}
        slide = by_ref["ROLL-A-SLIDE#1"]
        assert slide["delivered_count"] == 1
        assert slide["delivered_start"] == "2026-09-01T10:59:00"
        assert slide["partial"] is True
        assert result["counts"]["无关测点"] == 1
        assert result["counts"]["缺拆分依据"] == 0
