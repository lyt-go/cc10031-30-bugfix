"""序列导出领域服务：维护时序结果按测点与时间范围的分片导出任务与导出水位。
按测点范围和时间窗口选择性导出归并数据，写入分片导出数据文件。

交付按申请对账，而不是按目录整包复制：
- 只交付申请测点、且观测时刻落在申请区间内的窗口；
- 可拿到逐点原件的窗口按区间重新计算统计贡献，跨界窗口可被相邻请求精确切开；
- 只存整窗均值又跨界的窗口按「缺拆分依据」拒收，绝不把整窗平均当成区间平均；
- 无关测点、完全超界、缺拆分依据三类分别计数，并与源窗口条数对平；
- 交付文件每次重写，切换到相邻窗口时不残留上一份申请的外测点数据。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.services.base import BaseModuleService
from app.services.window_utils import (
    parse_points,
    parse_time_range,
    parse_timestamp,
    point_matches,
    request_covers,
    request_contains,
    request_overlaps,
)
from app.store import store

_WINDOW_REF_FIELDS = ("window_ref", "window_id", "window_index", "window_start")

EXCLUSION_POINTS = "无关测点"
EXCLUSION_OUT_OF_RANGE = "完全超界"
EXCLUSION_NO_SPLIT_BASIS = "缺拆分依据"


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso(value: Any) -> str | None:
    parsed = parse_timestamp(value)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S") if parsed else None


def _contribution(values: list[float], metric: str) -> float:
    if not values:
        return 0.0
    if metric in ("求和", "sum"):
        return round(sum(values), 4)
    if metric in ("最大值", "max"):
        return round(max(values), 4)
    if metric in ("最小值", "min"):
        return round(min(values), 4)
    if metric in ("计数", "count"):
        return float(len(values))
    return round(sum(values) / len(values), 4)


class SeriesExportService(BaseModuleService):
    MODULE = "series_export"
    ENTITY = "导出任务"
    OPERATION_TERM = "导出"
    LIST_FIELDS = ["导出编号", "测点范围", "时间范围", "导出格式", "采样粒度", "记录条数",
                   "交付窗口数", "排除条数", "无关测点", "完全超界", "缺拆分依据", "导出水位"]
    # 对账计数由交付计算产生，只读展示，不在登记时手填。
    READONLY_FIELDS = ["交付窗口数", "排除条数", "无关测点", "完全超界", "缺拆分依据"]
    REQUIRED_FIELDS = ["导出编号", "测点范围", "时间范围"]
    STATUS_ORDER = ["草稿", "待导出", "导出中", "已交付"]
    # 数据导出操作：按测点范围时间窗口导出降采样与归并结果到分片导出文件
    ACTION_RULES = {"启动导出": "导出中", "确认交付": "已交付", "标记失败": "已交付"}
    NEGATIVE_ACTIONS = []
    # 动作入口：受理只做对账快照，确认交付才按最新归并口径落成交付文件。
    ACTION_METHODS = {"启动导出": "start_export", "确认交付": "confirm_delivery"}

    # -- 请求解析 --

    @staticmethod
    def _request_of(task: dict[str, Any]) -> dict[str, Any]:
        point_patterns = parse_points(task.get("测点范围"))
        start, end, basis = parse_time_range(task.get("时间范围"))
        return {
            "points": point_patterns,
            "start": start,
            "end": end,
            "basis": basis,
            "start_iso": start.strftime("%Y-%m-%dT%H:%M:%S") if start else None,
            "end_iso": end.strftime("%Y-%m-%dT%H:%M:%S") if end else None,
            "has_window": start is not None or end is not None,
        }

    # -- 归并窗口读取 --

    def _load_rollup_records(self) -> list[tuple[dict[str, Any], str]]:
        """读取归并产物作为交付源；没有任何归并结果时才回退到降采样整窗记录。"""
        records: list[tuple[dict[str, Any], str]] = []
        rollup_dir = self._data_dir("rollup_windows")
        for path in sorted(rollup_dir.glob("*_rollup.jsonl")):
            for row in self._read_jsonl(path):
                if isinstance(row, dict):
                    records.append((row, path.name))
        if records:
            return records
        ds_dir = self._data_dir("downsampled")
        for path in sorted(ds_dir.glob("*_downsampled.jsonl")):
            for row in self._read_jsonl(path):
                # 降采样记录只是整窗聚合值，交付时同样必须自带实际起止。
                if isinstance(row, dict):
                    record = dict(row)
                    record.setdefault("source", "downsample")
                    records.append((record, path.name))
        return records

    @staticmethod
    def _window_bounds(record: dict[str, Any]) -> tuple[Any, Any]:
        """取窗口的实际起止。

        只认真正的时刻字段：``window_start/window_end`` 在旧产物里是观测序号
        （1、2、3…），不能当成时间戳，否则会被误判成 1970 年。
        """
        start = parse_timestamp(record.get("actual_start")) \
            or parse_timestamp(record.get("window_actual_start"))
        end = parse_timestamp(record.get("actual_end")) \
            or parse_timestamp(record.get("window_actual_end"))
        if start is not None and end is not None:
            return start, end
        return None, None

    @staticmethod
    def _window_observations(record: dict[str, Any]) -> list[dict[str, Any]]:
        raw = record.get("observations")
        if not isinstance(raw, list):
            return []
        observations = []
        for row_index, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            stamp = parse_timestamp(item.get("timestamp"))
            value = _float_or_none(item.get("value"))
            if stamp is None or value is None:
                continue
            observations.append({
                "timestamp": stamp,
                "timestamp_iso": stamp.strftime("%Y-%m-%dT%H:%M:%S"),
                "value": value,
                "source_file": item.get("source_file") or record.get("source_file") or "",
                "row_index": item.get("row_index", row_index),
            })
        observations.sort(key=lambda item: item["timestamp"])
        return observations

    def _late_originals(self, record: dict[str, Any], point: str,
                        window_start, window_end) -> list[dict[str, Any]]:
        """确认交付时补读该窗口的逐点原件。

        受理时窗口可能只有整窗均值，真正的逐点原件在确认前才补报落盘。
        这里按「补报文件 > 该测点逐点原件」的顺序读取，并裁剪到窗口实际起止，
        保证确认时能按区间重算，而不是退回整窗平均。
        """
        window_id = str(record.get("window_id") or "")
        window_ref = str(record.get("window_ref") or "")
        candidates = []
        originals_dir = self._data_dir("rollup_originals")
        for key in (window_ref, window_id):
            if key:
                path = originals_dir / f"{key}_originals.jsonl"
                if path.exists():
                    candidates.append(path)
        if not candidates:
            for sub, suffixes in (
                ("filtered_series", (f"{point}_filtered.jsonl",)),
                ("calibrated", (f"{point}_calibrated.jsonl",)),
                ("edge_samples", (f"{point}.jsonl",)),
            ):
                for suffix in suffixes:
                    path = self._data_dir(sub) / suffix
                    if path.exists():
                        candidates.append(path)
                if candidates:
                    break
        observations: list[dict[str, Any]] = []
        for path in candidates:
            for row_index, row in enumerate(self._read_jsonl(path)):
                stamp = parse_timestamp(row.get("timestamp") or row.get("aligned_timestamp")
                                        or row.get("original_timestamp"))
                value = _float_or_none(
                    row.get("aggregated_value", row.get("calibrated_value_V",
                            row.get("raw_value_mV", row.get("raw_value")))))
                if stamp is None or value is None:
                    continue
                row_point = row.get("point") or row.get("测点编号")
                if row_point and str(row_point).strip().casefold() != point.strip().casefold():
                    continue
                if window_start is not None and stamp < window_start:
                    continue
                if window_end is not None and stamp > window_end:
                    continue
                observations.append({
                    "timestamp": stamp,
                    "timestamp_iso": stamp.strftime("%Y-%m-%dT%H:%M:%S"),
                    "value": value,
                    "source_file": path.name,
                    "row_index": row_index,
                })
            if observations:
                break
        observations.sort(key=lambda item: item["timestamp"])
        # 同一时刻重复补报只保留一条，避免贡献被放大。
        deduped: list[dict[str, Any]] = []
        seen: set[Any] = set()
        for item in observations:
            if item["timestamp"] in seen:
                continue
            seen.add(item["timestamp"])
            deduped.append(item)
        return deduped

    @staticmethod
    def _window_ref(record: dict[str, Any], source_file: str, ordinal: int) -> str:
        for field in _WINDOW_REF_FIELDS:
            if record.get(field) not in (None, ""):
                return str(record.get(field))
        return f"{source_file}#{ordinal}"

    # -- 对账 --

    def reconcile(self, task: dict[str, Any], *, use_originals: bool = True) -> dict[str, Any]:
        """按申请测点与区间对账归并窗口，返回交付记录、排除明细和三类计数。

        ``use_originals=True`` 时会补读窗口落盘的逐点原件（确认交付时使用），
        这样补报通过后能按区间精确拆分；受理阶段只认归并产物现成口径。
        """
        request = self._request_of(task)
        deliveries: list[dict[str, Any]] = []
        exclusions: list[dict[str, Any]] = []
        base = {
            "export_id": task.get("导出编号", ""),
            "request_point": task.get("测点范围", ""),
            "request_time": task.get("时间范围", ""),
            "request_start": request["start_iso"],
            "request_end": request["end_iso"],
        }

        for ordinal, (record, source_file) in enumerate(self._load_rollup_records(), start=1):
            point = str(record.get("point", "")).strip()
            window_ref = self._window_ref(record, source_file, ordinal)
            window_start, window_end = self._window_bounds(record)
            common = {
                **base,
                "source": record.get("source", "rollup_window"),
                "source_file": source_file,
                "window_id": record.get("window_id", ""),
                "window_ref": window_ref,
                "point": point,
                "window_type": record.get("window_type", ""),
                "window_actual_start": _iso(record.get("actual_start"))
                or _iso(record.get("window_actual_start")),
                "window_actual_end": _iso(record.get("actual_end"))
                or _iso(record.get("window_actual_end")),
                "window_rollup_value": _float_or_none(record.get("rollup_value")),
                "window_metric": record.get("metric", ""),
            }

            def exclude(reason: str, detail: str) -> None:
                exclusions.append({**common, "reason": reason, "detail": detail})

            # 1) 测点归属
            if not point_matches(point, request["points"]):
                exclude(EXCLUSION_POINTS,
                        f"窗口测点 {point or '空'} 不在申请测量范围「{task.get('测点范围', '')}」内")
                continue

            # 2) 时间归属：没有实际起止就无法判断是否落在申请区间
            if request["has_window"] and (window_start is None or window_end is None):
                exclude(EXCLUSION_NO_SPLIT_BASIS,
                        "窗口缺少实际起止时刻，无法核对申请区间")
                continue

            observations = self._window_observations(record)
            if not observations and use_originals and request["has_window"]:
                observations = self._late_originals(record, point, window_start, window_end)
            if request["has_window"]:
                if not request_overlaps(window_start, window_end, request):
                    exclude(EXCLUSION_OUT_OF_RANGE,
                            f"窗口实际起止 {common['window_actual_start']}~{common['window_actual_end']} "
                            f"完全落在申请区间 {request['start_iso']}~{request['end_iso']} 之外")
                    continue
                in_range = [obs for obs in observations
                            if request_contains(obs["timestamp"], request)]
                fully_inside = request_covers(window_start, window_end, request)
            else:
                in_range = observations
                fully_inside = True

            # 3) 有逐点原件：按申请区间精确计算统计贡献
            if in_range:
                values = [obs["value"] for obs in in_range]
                metric = record.get("metric", "") or "均值"
                delivered_start = in_range[0]["timestamp_iso"]
                delivered_end = in_range[-1]["timestamp_iso"]
                deliveries.append({
                    **common,
                    "delivered_start": delivered_start,
                    "delivered_end": delivered_end,
                    "delivered_count": len(in_range),
                    "value": _contribution(values, metric),
                    "metric": metric,
                    "granularity": "区间精确",
                    "split_basis": "逐点原件",
                    "partial": not fully_inside,
                    "provenance": [
                        {"timestamp": obs["timestamp_iso"],
                         "source_file": obs["source_file"],
                         "row_index": obs["row_index"]}
                        for obs in in_range
                    ],
                })
                continue

            # 4) 没有可用的逐点原件
            if observations:
                # 原件尚未覆盖申请区间，窗口自身落在申请区间外。
                exclude(EXCLUSION_OUT_OF_RANGE,
                        f"窗口实际观测 {common['window_actual_start']}~{common['window_actual_end']} "
                        f"全部落在申请区间 {request['start_iso']}~{request['end_iso']} 之外")
                continue
            rollup_value = _float_or_none(record.get("rollup_value"))
            if not fully_inside:
                # 跨界窗口只有整窗均值：不能拆，也不能拿整窗平均冒充区间平均。
                exclude(EXCLUSION_NO_SPLIT_BASIS,
                        f"窗口 {common['window_actual_start']}~{common['window_actual_end']} "
                        f"跨界申请区间 {request['start_iso']}~{request['end_iso']}，"
                        f"仅有整窗值 {rollup_value}，缺少逐点原件无法拆分")
                continue
            if rollup_value is None:
                exclude(EXCLUSION_NO_SPLIT_BASIS,
                        "窗口缺少可交付的整窗值与逐点原件")
                continue
            # 整窗完整落在申请区间内：可以交付整窗口径结果，并显式标注粒度。
            deliveries.append({
                **common,
                "delivered_start": common["window_actual_start"],
                "delivered_end": common["window_actual_end"],
                "delivered_count": int(record.get("record_count") or 0),
                "value": rollup_value,
                "metric": record.get("metric", ""),
                "granularity": "整窗口径",
                "split_basis": record.get("split_basis", "整窗聚合"),
                "partial": False,
                "provenance": [
                    {"timestamp": item.get("timestamp"),
                     "source_file": item.get("source_file", source_file),
                     "row_index": item.get("row_index")}
                    for item in (record.get("provenance") or [])
                    if isinstance(item, dict)
                ],
            })

        counts = {
            EXCLUSION_POINTS: sum(1 for item in exclusions if item["reason"] == EXCLUSION_POINTS),
            EXCLUSION_OUT_OF_RANGE: sum(1 for item in exclusions if item["reason"] == EXCLUSION_OUT_OF_RANGE),
            EXCLUSION_NO_SPLIT_BASIS: sum(1 for item in exclusions if item["reason"] == EXCLUSION_NO_SPLIT_BASIS),
        }
        source_records = len(deliveries) + len(exclusions)
        return {
            "request": request,
            "deliveries": deliveries,
            "exclusions": exclusions,
            "counts": counts,
            "summary": {
                "source_windows": source_records,
                "delivered_windows": len(deliveries),
                "excluded_windows": len(exclusions),
                "delivered_observations": sum(item["delivered_count"] for item in deliveries
                                              if item["split_basis"] == "逐点原件"),
                "delivered_points": sorted({item["point"] for item in deliveries}),
                "excluded_by_reason": counts,
                # 源输入条数 = 交付窗口 + 拒收窗口，逐条可回查。
                "reconciled": source_records == len(deliveries) + len(exclusions),
            },
        }

    # -- 只读视图 --

    def _write_csv(self, filepath: Path, rows: list[dict[str, Any]]) -> int:
        """交付格式为 csv 时把交付记录摊平成表格；嵌套字段按 JSON 串保留。"""
        import csv
        import json

        filepath.parent.mkdir(parents=True, exist_ok=True)
        columns = ["window_ref", "point", "delivered_start", "delivered_end",
                   "delivered_count", "value", "metric", "granularity", "split_basis",
                   "partial", "window_actual_start", "window_actual_end", "source_file"]
        with open(filepath, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(columns + ["provenance"])
            for row in rows:
                values = []
                for column in columns:
                    value = row.get(column, "")
                    # 以公式字符开头的值加前缀，避免在表格软件里被执行。
                    text = str(value) if value is not None else ""
                    if text.startswith(("=", "+", "-", "@")):
                        text = "'" + text
                    values.append(text)
                writer.writerow(values + [json.dumps(row.get("provenance", []),
                                                     ensure_ascii=False)])
        return len(rows)

    def delivery_detail(self, export_id: int) -> dict[str, Any]:
        """只读对账视图：按当前归并产物重算，不推进状态、不写交付文件。"""
        task = store.find(self.MODULE, export_id)
        if task is None:
            return {"error": f"导出任务 {export_id} 不存在"}
        result = self.reconcile(task)
        return {
            "export_id": task["导出编号"],
            "status": task.get("status", ""),
            "request_point": task.get("测点范围", ""),
            "request_time": task.get("时间范围", ""),
            "request": result["request"],
            "summary": result["summary"],
            "deliveries": result["deliveries"],
            "exclusions": result["exclusions"],
            "counts": result["counts"],
        }

    def exclusion_detail(self, export_id: int) -> dict[str, Any]:
        """只读的排除对账：三类原因分别计数，便于和源输入对上。"""
        task = store.find(self.MODULE, export_id)
        if task is None:
            return {"error": f"导出任务 {export_id} 不存在"}
        result = self.reconcile(task)
        return {
            "export_id": task["导出编号"],
            "counts": result["counts"],
            "excluded_windows": len(result["exclusions"]),
            "delivered_windows": len(result["deliveries"]),
            "source_windows": result["summary"]["source_windows"],
            "reconciled": result["summary"]["reconciled"],
            "exclusions": result["exclusions"],
        }

    # -- 动作 --

    def start_export(self, export_id: int) -> dict[str, Any]:
        """受理导出申请：按当前归并口径对账并留下受理快照，暂不落成交付结果。"""
        task = store.find(self.MODULE, export_id)
        if task is None:
            return {"error": f"导出任务 {export_id} 不存在"}
        self._set_status(export_id, "启动导出")
        # 受理阶段只认已有归并产物：此时跨界窗口可能只有整窗均值。
        result = self.reconcile(task, use_originals=False)
        accepted = [
            {"stage": "受理", "window_ref": item["window_ref"], "point": item["point"],
             "window_actual_start": item["window_actual_start"],
             "window_actual_end": item["window_actual_end"],
             "split_basis": item["split_basis"]}
            for item in result["deliveries"]
        ] + [
            {"stage": "受理", "window_ref": item["window_ref"], "point": item["point"],
             "window_actual_start": item["window_actual_start"],
             "window_actual_end": item["window_actual_end"],
             "reason": item["reason"], "detail": item["detail"]}
            for item in result["exclusions"]
        ]
        self._write_jsonl(
            self._data_dir("exports") / f"{task['导出编号']}_accepted.jsonl",
            accepted, append=False,
        )
        return {
            "export_id": task["导出编号"],
            "status": "导出中",
            "accepted": True,
            "request": result["request"],
            "summary": result["summary"],
            "deliveries": result["deliveries"],
            "exclusions": result["exclusions"],
        }

    def confirm_delivery(self, export_id: int) -> dict[str, Any]:
        """确认交付：按确认时的最新归并窗口重新对账并重写交付与排除文件。"""
        task = store.find(self.MODULE, export_id)
        if task is None:
            return {"error": f"导出任务 {export_id} 不存在"}
        result = self.reconcile(task)
        export_fmt = task.get("导出格式", "jsonl") or "jsonl"
        export_file = self._data_dir("exports") / f"{task['导出编号']}_export.{export_fmt}"
        exclusion_file = self._data_dir("exports") / f"{task['导出编号']}_exclusions.jsonl"

        if export_fmt.lower() == "csv":
            written = self._write_csv(export_file, result["deliveries"])
        else:
            written = self._write_jsonl(export_file, result["deliveries"], append=False)
        # 排除明细始终是独立 JSONL：三类原因的明细必须逐条可回查。
        excluded = self._write_jsonl(exclusion_file, result["exclusions"], append=False)

        task["记录条数"] = str(written)
        task["交付窗口数"] = str(written)
        task["排除条数"] = str(excluded)
        task["无关测点"] = str(result["counts"][EXCLUSION_POINTS])
        task["完全超界"] = str(result["counts"][EXCLUSION_OUT_OF_RANGE])
        task["缺拆分依据"] = str(result["counts"][EXCLUSION_NO_SPLIT_BASIS])
        task["分片大小"] = str(written * 80)
        task["导出水位"] = str(written)
        self._set_status(export_id, "确认交付")
        return {
            "export_id": task["导出编号"],
            "format": export_fmt,
            "status": "已交付",
            "records_exported": written,
            "total_size_estimate": written * 80,
            "request": result["request"],
            "summary": result["summary"],
            "deliveries": result["deliveries"],
            "exclusions": result["exclusions"],
            "counts": result["counts"],
            "export_file": export_file.name,
            "exclusion_file": exclusion_file.name,
        }

    def export_series(self, export_id: int) -> dict[str, Any]:
        """受理并直接确认交付（供批量调用）；等价于启动导出后确认交付。"""
        accepted = self.start_export(export_id)
        if "error" in accepted:
            return accepted
        return self.confirm_delivery(export_id)

    def export_by_time_window(self, point_range: str, time_window: str,
                               fmt: str = "jsonl") -> dict[str, Any]:
        """按指定时间窗口和测点范围创建导出任务并输出交付数据。"""
        tid = max((int(r.get("id", 0)) for r in store.rows(self.MODULE)), default=0) + 1
        self._auto_create_seed(self.MODULE, {
            "导出编号": f"EXPORT-{point_range}-{tid}",
            "测点范围": point_range,
            "时间范围": time_window,
            "导出格式": fmt,
            "采样粒度": "1h",
            "记录条数": "0",
            "分片大小": "0",
            "导出水位": "0",
        })
        return self.export_series(tid)
