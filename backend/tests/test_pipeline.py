"""跨模块集成测试：验证10组数据传递链路，断言真实文件IO产物与下游消费可见性。"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.base import DATA_ROOT


@pytest.fixture(autouse=True)
def setup_data_dir():
    """将数据目录指向临时位置，避免污染 real data。"""
    with tempfile.TemporaryDirectory() as tmp:
        with patch("app.services.base.DATA_ROOT", Path(tmp)):
            # 也 patch 各模块引入的 DATA_ROOT
            import app.services.sensor_point as sp
            import app.services.edge_buffer as eb
            import app.services.unit_calibrate as uc
            import app.services.time_align as ta
            import app.services.gap_fill as gf
            import app.services.downsample as ds
            import app.services.rollup_window as rw
            import app.services.series_index as si
            import app.services.tier_storage as ts
            import app.services.series_lineage as sl
            import app.services.series_export as se
            import app.services.series_quality as sq
            import app.services.series_snapshot as ss
            for mod in [sp, eb, uc, ta, gf, ds, rw, si, ts, sl, se, sq, ss]:
                mod.DATA_ROOT = Path(tmp)
            yield Path(tmp)


class TestSensorToEdgeChain:
    """链路1: sensor_point 投运后自动生成 edge_buffer + device_health 种子数据"""

    def test_onboard_then_activate_creates_edge_and_health_seeds(self, setup_data_dir):
        from app.services.sensor_point import SensorPointService
        from app.store import store

        svc = SensorPointService()
        entry = svc.onboard_sensor({
            "测点编号": "SENS-P1",
            "所属产线": "P1",
            "传感器型号": "PT100",
            "量程范围": "0-200",
            "采样频率": "10Hz",
            "计量单位": "C",
        })
        result = svc.activate_point(entry["id"])
        assert "error" not in result
        assert "auto_triggers" in result
        assert "edge_buffer" in result["auto_triggers"]
        assert "device_health" in result["auto_triggers"]

        # 验证 edge_buffer store 确实有种子
        edge_rows = [r for r in store.rows("edge_buffer") if r.get("测点编号") == "SENS-P1"]
        assert len(edge_rows) >= 1
        # 验证 device_health store 确实有种子
        health_rows = [r for r in store.rows("device_health") if r.get("采集终端") == "SENS-P1"]
        assert len(health_rows) >= 1


class TestEdgeToUploadChain:
    """链路2: edge_buffer 采集+落盘后触发 upload_batch 种子"""

    def test_collect_then_flush_triggers_upload_batch(self, setup_data_dir):
        from app.services.edge_buffer import EdgeBufferService
        from app.services.sensor_point import SensorPointService
        from app.store import store

        # 先创建测点和边缘缓冲种子
        point_svc = SensorPointService()
        point_entry = point_svc.onboard_sensor({"测点编号": "SENS-P2", "所属产线": "P2", "传感器型号": "TC"})
        point_svc.activate_point(point_entry["id"])

        edge_rows = store.rows("edge_buffer")
        assert len(edge_rows) >= 1
        # 使用最新创建的边缘缓冲（测点编号为最近激活的）
        auto_edge = max(edge_rows, key=lambda r: int(r.get("id", 0)))
        buffer_id = auto_edge["id"]
        assert "SENS-P2" in str(auto_edge.get("测点编号", ""))

        # 采集采样数据
        buffer_svc = EdgeBufferService()
        collect_result = buffer_svc.collect_samples(buffer_id, sample_count=30)
        assert collect_result["written"] == 30
        # 验证采样文件真实存在
        shard = collect_result["shard_key"]
        samples_file = setup_data_dir / "edge_samples" / f"{shard}.jsonl"
        assert samples_file.exists()
        lines = samples_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 30

        # 落盘触发上送
        flush_result = buffer_svc.flush_to_disk(buffer_id)
        assert "records_flushed" in flush_result
        assert flush_result["records_flushed"] == 30
        assert "upload_batch" in flush_result.get("auto_triggers", {})

        # 验证 upload_batch store 确实有种子（自动创建的行会比种子行多）
        upload_rows = store.rows("upload_batch")
        assert len(upload_rows) >= 1
        # 确认自动触发结果包含 upload_batch 且不是 error
        trigger_result = flush_result.get("auto_triggers", {}).get("upload_batch", {})
        assert "error" not in trigger_result, f"upload trigger failed: {trigger_result}"


class TestUploadToCalibrateChain:
    """链路3: upload_batch 完成后触发 unit_calibrate + ingest_watermark"""

    def test_upload_complete_triggers_calibrate_and_watermark(self, setup_data_dir):
        from app.services.edge_buffer import EdgeBufferService
        from app.services.upload_batch import UploadBatchService
        from app.store import store

        # 准备：先确保有 edge_buffer 种子
        from app.services.sensor_point import SensorPointService
        point_svc = SensorPointService()
        point_svc.onboard_sensor({"测点编号": "SENS-P3", "所属产线": "P3", "传感器型号": "PT200"})
        point_svc.activate_point(1)
        # 找到自动创建的edge_buffer（最新插入的）
        edge_rows = store.rows("edge_buffer")
        auto_edge = max(edge_rows, key=lambda r: int(r.get("id", 0)))
        buffer_svc = EdgeBufferService()
        buffer_svc.collect_samples(auto_edge["id"], sample_count=20)
        buffer_svc.flush_to_disk(auto_edge["id"])

        # 找到上送批次（最新创建的）
        upload_rows = store.rows("upload_batch")
        assert len(upload_rows) >= 1
        auto_batch = max(upload_rows, key=lambda r: int(r.get("id", 0)))
        batch_id = auto_batch["id"]
        # 确保批次编号不为空且是有效批次
        assert auto_batch.get("批次编号", "").startswith("UPLO-")

        batch_svc = UploadBatchService()
        submit_result = batch_svc.submit_batch(batch_id)
        assert "records" in submit_result
        result = batch_svc.complete_batch(batch_id)
        assert "auto_triggers" in result
        assert "unit_calibrate" in result["auto_triggers"]
        assert "ingest_watermark" in result["auto_triggers"]

        # 验证 unit_calibrate store 有种子
        cal_rows = store.rows("unit_calibrate")
        assert len(cal_rows) >= 1
        # 验证 ingest_watermark store 有种子
        wm_rows = store.rows("ingest_watermark")
        assert len(wm_rows) >= 1


class TestCalibrateToAlignChain:
    """链路4: unit_calibrate 标定后时间对齐"""

    def test_calibrate_then_align(self, setup_data_dir):
        from app.services.unit_calibrate import UnitCalibrateService
        from app.services.time_align import TimeAlignService
        from app.store import store

        # 先写入模拟采样数据文件
        samples = []
        for i in range(10):
            ts = f"2026-09-01T10:{i:02d}:00.000000"
            samples.append({"point": "SENS-P4", "timestamp": ts, "raw_value_mV": 23.5 + i * 0.3})
        (setup_data_dir / "edge_samples").mkdir(parents=True, exist_ok=True)
        with open(setup_data_dir / "edge_samples" / "SENS-P4.jsonl", "w", encoding="utf-8") as f:
            for s in samples:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")

        # 创建标定种子
        from app.store import store
        store.rows("unit_calibrate").append({
            "id": 201, "标定编号": "UNIT-P4", "测点编号": "SENS-P4",
            "原始单位": "mV", "目标单位": "V", "换算系数": "0.001",
            "零点偏移": "0", "标定方式": "线性换算",
            "标定时间": "2026-09-01", "status": "待标定", "pending": True, "abnormal": False
        })

        cal_svc = UnitCalibrateService()
        cal_result = cal_svc.calibrate_values(201)
        assert cal_result["records_calibrated"] == 10
        # 验证标定文件存在
        cal_file = setup_data_dir / "calibrated" / "SENS-P4_calibrated.jsonl"
        assert cal_file.exists()

        # 创建时间对齐规则并执行对齐
        store.rows("time_align").append({
            "id": 202, "规则编号": "ALIGN-P4", "对齐粒度": "1s",
            "时区基准": "UTC+8", "插值方式": "linear",
            "容许偏移": "100ms", "对齐条数": "0", "未对齐条数": "0",
            "规则版本": "v1.0", "status": "待生效", "pending": True, "abnormal": False
        })
        align_svc = TimeAlignService()
        align_result = align_svc.align_series(202, ["SENS-P4"])
        assert align_result["aligned_count"] == 10
        # 验证对齐数据文件存在
        align_file = setup_data_dir / "aligned" / "ALIGN-P4_aligned.jsonl"
        assert align_file.exists()


class TestDownsampleChain:
    """链路5: spike_filter 清洗后的数据进入降采样"""

    def test_spike_filter_then_downsample(self, setup_data_dir):
        from app.services.spike_filter import SpikeFilterService
        from app.services.downsample import DownsampleService
        from app.store import store

        # 写入标定数据
        samples = []
        for i in range(20):
            ts = f"2026-09-01T10:{i//60:02d}:{i%60:02d}.000000"
            val = 23.5 + i * 0.3
            if i == 7:
                val = 999.0  # 尖峰
            samples.append({"point": "SENS-P5", "timestamp": ts, "calibrated_value_V": val})
        (setup_data_dir / "calibrated").mkdir(parents=True, exist_ok=True)
        with open(setup_data_dir / "calibrated" / "SENS-P5_calibrated.jsonl", "w", encoding="utf-8") as f:
            for s in samples:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")

        sf_svc = SpikeFilterService()
        spike_result = sf_svc.detect_spikes("SENS-P5", threshold_multiplier=3.0, range_limit=100.0)
        assert spike_result["spike_count"] >= 1

        # 标记过滤
        store.rows("spike_filter").append({
            "id": 203, "异常编号": "SPIKE-P5", "测点编号": "SENS-P5",
            "采样时刻": samples[7]["timestamp"], "观测值": str(samples[7]["calibrated_value_V"]),
            "量程上限": "100.0", "偏离倍数": "10.0", "判定窗口": "3x 均值",
            "处置结论": "待判定", "status": "待判定", "pending": True, "abnormal": True
        })
        filter_result = sf_svc.filter_anomalous(203)
        assert filter_result["filtered_count"] == 19
        # 验证过滤后文件存在
        filtered_file = setup_data_dir / "filtered_series" / "SENS-P5_filtered.jsonl"
        assert filtered_file.exists()

        # 降采样
        store.rows("downsample").append({
            "id": 204, "任务编号": "DOWN-P5", "源序列": "SENS-P5",
            "源采样频率": "10Hz", "目标频率": "1Hz", "聚合方式": "均值",
            "输出点数": "0", "压缩比": "0", "任务水位": "0",
            "status": "待执行", "pending": True, "abnormal": False
        })
        ds_svc = DownsampleService()
        ds_result = ds_svc.downsample_series(204)
        assert ds_result["output_points"] > 0
        # 验证降采样文件存在
        ds_file = setup_data_dir / "downsampled" / "DOWN-P5_downsampled.jsonl"
        assert ds_file.exists()
        # 验证压缩比
        assert ds_result["compression_ratio"] > 0


class TestRollupToIndexChain:
    """链路6: downsample -> rollup_window -> series_index -> tier_storage"""

    def test_downsample_rollup_index_tier_chain(self, setup_data_dir):
        from app.services.rollup_window import RollupWindowService
        from app.services.series_index import SeriesIndexService
        from app.services.tier_storage import TierStorageService
        from app.store import store

        # 准备降采样数据文件
        ds_data = []
        for i in range(15):
            ds_data.append({"task_id": "DOWN-P6", "point": "SENS-P6",
                           "window_index": i+1, "aggregated_value": round(23.5 + i * 0.2, 4),
                           "method": "均值", "target_freq": "1Hz"})
        (setup_data_dir / "downsampled").mkdir(parents=True, exist_ok=True)
        with open(setup_data_dir / "downsampled" / "DOWN-SENS-P6_downsampled.jsonl", "w", encoding="utf-8") as f:
            for d in ds_data:
                f.write(json.dumps(d, ensure_ascii=False) + "\n")

        # Window rollup
        store.rows("rollup_window").append({
            "id": 205, "窗口编号": "ROLL-P6", "测点编号": "SENS-P6",
            "窗口类型": "固定窗口", "窗口长度": "5", "滑动步长": "1",
            "聚合指标": "均值", "窗口条数": "0", "归并水位": "0",
            "status": "待归并", "pending": True, "abnormal": False
        })
        rw_svc = RollupWindowService()
        rollup_result = rw_svc.rollup_windows(205)
        assert rollup_result["window_period_count"] >= 1
        rollup_file = setup_data_dir / "rollup_windows" / "ROLL-P6_rollup.jsonl"
        assert rollup_file.exists()

        # Series index
        store.rows("series_index").append({
            "id": 206, "索引编号": "IDX-P6", "测点编号": "SENS-P6",
            "时间分区": "2026-09-01", "点区间": "0-0", "索引粒度": "1h",
            "索引条数": "0", "存储位置": "", "索引版本": "v1.0",
            "status": "待构建", "pending": True, "abnormal": False
        })
        si_svc = SeriesIndexService()
        idx_result = si_svc.build_index(206)
        assert idx_result["indexed_count"] > 0
        idx_file = setup_data_dir / "series_index" / "IDX-P6_index.jsonl"
        assert idx_file.exists()

        # Tier storage
        store.rows("tier_storage").append({
            "id": 207, "策略编号": "TIER-P6", "时间边界": "30d",
            "热区范围": "0-7d", "温区范围": "7d-30d", "冷区路径": "/cold/archive",
            "迁移方式": "自动迁移", "驻留天数": "30", "策略版本": "v1.0",
            "status": "待生效", "pending": True, "abnormal": False
        })
        ts_svc = TierStorageService()
        tier_result = ts_svc.apply_tier_policy(207)
        assert tier_result["hot_records"] + tier_result["warm_records"] + tier_result["cold_records"] > 0


class TestLineageChain:
    """链路7: 全链路血缘追溯"""

    def test_build_lineage_from_full_chain(self, setup_data_dir):
        from app.services.series_lineage import SeriesLineageService
        from app.store import store

        # 写入各阶段种子数据
        store.rows("upload_batch").append({
            "id": 221, "批次编号": "UPLO-P7", "分片范围": "SENS-P7", "测点数量": "1",
            "记录条数": "50", "压缩方式": "gzip", "批次校验值": "abc123",
            "上送时长": "0.5", "批次水位": "50", "status": "已完成", "pending": False, "abnormal": False
        })
        store.rows("unit_calibrate").append({
            "id": 222, "标定编号": "UNIT-P7", "测点编号": "SENS-P7",
            "原始单位": "mV", "目标单位": "V", "换算系数": "0.001",
            "零点偏移": "0", "标定方式": "线性换算", "标定时间": "2026-09-01",
            "status": "已标定", "pending": False, "abnormal": False
        })
        store.rows("downsample").append({
            "id": 223, "任务编号": "DOWN-P7", "源序列": "SENS-P7",
            "源采样频率": "10Hz", "目标频率": "1Hz", "聚合方式": "均值",
            "输出点数": "10", "压缩比": "5.0", "任务水位": "10",
            "status": "已完成", "pending": False, "abnormal": False
        })
        store.rows("rollup_window").append({
            "id": 224, "窗口编号": "ROLL-P7", "测点编号": "SENS-P7",
            "窗口类型": "固定窗口", "窗口长度": "5", "滑动步长": "1",
            "聚合指标": "均值", "窗口条数": "2", "归并水位": "2",
            "status": "已归并", "pending": False, "abnormal": False
        })

        # 创建各阶段数据文件（供验证链使用）
        for path, data in [
            ("edge_samples/SENS-P7.jsonl", [{"point": "SENS-P7", "timestamp": "2026-09-01T10:00:00", "raw_value_mV": 23.5}]),
            ("calibrated/SENS-P7_calibrated.jsonl", [{"point": "SENS-P7", "timestamp": "2026-09-01T10:00:00", "calibrated_value_V": 0.0235}]),
            ("downsampled/DOWN-P7_downsampled.jsonl", [{"task_id": "DOWN-P7", "point": "SENS-P7", "aggregated_value": 0.024}]),
            ("rollup_windows/ROLL-P7_rollup.jsonl", [{"window_id": "ROLL-P7", "point": "SENS-P7", "rollup_value": 0.024}]),
        ]:
            full_path = setup_data_dir / path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                for d in data:
                    f.write(json.dumps(d, ensure_ascii=False) + "\n")

        svc = SeriesLineageService()
        lineage = svc.build_lineage("SENS-P7")
        assert lineage["stages_traced"] == 4  # batch + calibrate + downsample + rollup all present

        # 验证血缘链路文件存在
        lineage_file = setup_data_dir / "lineage_traces" / "SENS-P7_lineage.jsonl"
        assert lineage_file.exists()


class TestQualityChain:
    """链路8: 数据质量校验"""

    def test_quality_check_on_calibrated_data(self, setup_data_dir):
        from app.services.series_quality import SeriesQualityService
        from app.store import store

        # 写入标定数据（含重复和脏数据）
        records = [
            {"point": "SENS-P8", "timestamp": "2026-09-01T10:00:00", "calibrated_value_V": 23.5},
            {"point": "SENS-P8", "timestamp": "2026-09-01T10:01:00", "calibrated_value_V": 24.0},
            {"point": "SENS-P8", "timestamp": "2026-09-01T10:01:00", "calibrated_value_V": 24.0},  # 重复
            {"point": "SENS-P8", "timestamp": "2026-09-01T10:02:00", "calibrated_value_V": 25.0},
            {"point": "SENS-P8", "timestamp": "2026-09-01T10:03:00", "calibrated_value_V": 2000.0},  # 越界
        ]
        (setup_data_dir / "calibrated").mkdir(parents=True, exist_ok=True)
        with open(setup_data_dir / "calibrated" / "SENS-P8_calibrated.jsonl", "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

        store.rows("series_quality").append({
            "id": 208, "报告编号": "QUAL-P8", "测点编号": "SENS-P8",
            "统计窗口": "2026-09-01", "完整率": "0", "重复点数": "0",
            "越界点数": "0", "校验规则": "完整性+重复+越界", "质检结论": "",
            "status": "待质检", "pending": True, "abnormal": False
        })
        sq_svc = SeriesQualityService()
        result = sq_svc.run_quality_check(208)
        assert result["duplicate_count"] >= 1
        assert result["out_of_range"] >= 1
        assert result["completeness"] > 0
        # 验证质检报告文件存在
        report_file = setup_data_dir / "quality_reports" / "QUAL-P8_quality.jsonl"
        assert report_file.exists()


class TestExportChain:
    """链路9: 序列导出——按申请测点与区间对账，不再整目录搬运。"""

    def test_export_from_downsample_and_rollup(self, setup_data_dir):
        from app.services.series_export import SeriesExportService
        from app.store import store

        # 归并产物带实际起止；隔了很远的降采样任务不属于本次申请。
        rollup_data = [
            {"point": "SENS-P9", "window_ref": f"ROLL-P9#{i+1}",
             "actual_start": f"2026-09-01T10:0{i}:00",
             "actual_end": f"2026-09-01T10:0{i+4}:00",
             "rollup_value": 24.5, "metric": "均值", "record_count": 5}
            for i in range(3)
        ]
        ds_data = [{"point": "SENS-P9", "window_index": i, "aggregated_value": 24.0, "method": "均值"}
                   for i in range(5)]

        (setup_data_dir / "downsampled").mkdir(parents=True, exist_ok=True)
        (setup_data_dir / "rollup_windows").mkdir(parents=True, exist_ok=True)
        with open(setup_data_dir / "downsampled" / "DOWN-P9_downsampled.jsonl", "w", encoding="utf-8") as f:
            for d in ds_data:
                f.write(json.dumps(d) + "\n")
        with open(setup_data_dir / "rollup_windows" / "ROLL-P9_rollup.jsonl", "w", encoding="utf-8") as f:
            for d in rollup_data:
                f.write(json.dumps(d) + "\n")

        store.rows("series_export").append({
            "id": 209, "导出编号": "EXPORT-P9", "测点范围": "SENS-P9",
            "时间范围": "2026-09-01", "导出格式": "jsonl", "采样粒度": "1h",
            "记录条数": "0", "分片大小": "0", "导出水位": "0",
            "status": "待导出", "pending": True, "abnormal": False
        })
        se_svc = SeriesExportService()
        result = se_svc.export_series(209)
        # 只有该测点的 3 个归并窗口进入交付，降采样整窗记录不再被整目录夹带。
        assert result["records_exported"] == 3
        assert {item["point"] for item in result["deliveries"]} == {"SENS-P9"}
        assert all(item["split_basis"] == "整窗聚合" for item in result["deliveries"])
        assert result["summary"]["reconciled"] is True
        # 验证导出文件存在
        export_file = setup_data_dir / "exports" / "EXPORT-P9_export.jsonl"
        assert export_file.exists()
        assert len(export_file.read_text(encoding="utf-8").strip().splitlines()) == 3


class TestSnapshotChain:
    """链路10: 版本快照"""

    def test_snapshot_from_rollup_and_quality(self, setup_data_dir):
        from app.services.series_snapshot import SeriesSnapshotService
        from app.store import store

        # 准备归并+质量数据文件
        rollup_data = [{"point": "SENS-P10", "window_start": i, "rollup_value": 24.0, "metric": "均值"}
                       for i in range(3)]
        quality_data = [{"point": "SENS-P10", "conclusion": "合格", "completeness": 0.98}]

        (setup_data_dir / "rollup_windows").mkdir(parents=True, exist_ok=True)
        (setup_data_dir / "quality_reports").mkdir(parents=True, exist_ok=True)
        with open(setup_data_dir / "rollup_windows" / "ROLL-P10_rollup.jsonl", "w", encoding="utf-8") as f:
            for d in rollup_data:
                f.write(json.dumps(d) + "\n")
        with open(setup_data_dir / "quality_reports" / "QUAL-P10_quality.jsonl", "w", encoding="utf-8") as f:
            for d in quality_data:
                f.write(json.dumps(d) + "\n")

        store.rows("series_snapshot").append({
            "id": 210, "快照编号": "SNAP-P10", "口径说明": "归并+质量",
            "测点范围": "SENS-P10", "时间范围": "2026-09-01",
            "序列条数": "0", "生成方式": "", "快照水位": "0",
            "生成时间": "", "status": "草稿", "pending": True, "abnormal": False
        })
        ss_svc = SeriesSnapshotService()
        result = ss_svc.create_snapshot(210)
        assert result["records_in_snapshot"] > 0
        # 验证快照文件存在
        snap_file = setup_data_dir / "snapshots" / "SNAP-P10_snapshot.jsonl"
        assert snap_file.exists()


class TestErrorHandling:
    """异常场景：脏数据、重复批次、中断恢复"""

    def test_calibrate_handles_empty_source_file(self, setup_data_dir):
        from app.services.unit_calibrate import UnitCalibrateService
        from app.store import store

        store.rows("unit_calibrate").append({
            "id": 99, "标定编号": "UNIT-EMPTY", "测点编号": "NONEXIST",
            "原始单位": "mV", "目标单位": "V", "换算系数": "0.001",
            "零点偏移": "0", "标定方式": "线性换算",
            "标定时间": "2026-09-01", "status": "待标定", "pending": True, "abnormal": False
        })
        svc = UnitCalibrateService()
        result = svc.calibrate_values(99)
        assert result["records_calibrated"] == 0  # 空文件不出错但也没有记录

    def test_quality_handles_empty_data(self, setup_data_dir):
        from app.services.series_quality import SeriesQualityService
        from app.store import store

        store.rows("series_quality").append({
            "id": 212, "报告编号": "QUAL-EMPTY", "测点编号": "NO_DATA",
            "统计窗口": "2026-09-01", "完整率": "0", "重复点数": "0",
            "越界点数": "0", "校验规则": "", "质检结论": "",
            "status": "待质检", "pending": True, "abnormal": False
        })
        svc = SeriesQualityService()
        result = svc.run_quality_check(212)
        assert result["conclusion"] == "无数据"
        assert result["total_records"] == 0

    def test_duplicate_batch_detection(self, setup_data_dir):
        """重复批次应被识别"""
        from app.services.series_quality import SeriesQualityService
        from app.store import store

        # 写入含重复的数据
        records = []
        for i in range(10):
            records.append({"point": "SENS-DUP", "timestamp": f"2026-09-01T10:{i:02d}:00", "calibrated_value_V": 23.5 + i})
            if i == 5:
                records.append({"point": "SENS-DUP", "timestamp": "2026-09-01T10:05:00", "calibrated_value_V": 28.5})  # 重复时间戳
        (setup_data_dir / "calibrated").mkdir(parents=True, exist_ok=True)
        with open(setup_data_dir / "calibrated" / "SENS-DUP_calibrated.jsonl", "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

        store.rows("series_quality").append({
            "id": 213, "报告编号": "QUAL-DUP", "测点编号": "SENS-DUP",
            "统计窗口": "2026-09-01", "完整率": "0", "重复点数": "0",
            "越界点数": "0", "校验规则": "完整性+重复", "质检结论": "",
            "status": "待质检", "pending": True, "abnormal": False
        })
        sq_svc = SeriesQualityService()
        result = sq_svc.run_quality_check(213)
        assert result["duplicate_count"] >= 1