"""窗口归并领域服务：维护固定、滑动与会话窗口的时序归并聚合指标与窗口条数。
对降采样后的序列数据按窗口类型进行窗口归并计算，生成归并结果数据集。

每条归并结果都带上窗口的**实际起止时刻**以及**纳入的观测出处**：
交付服务只有拿到逐点原件才能把跨界窗口按申请区间精确拆开；只有整窗均值时
必须显式标记为不可拆分，不能把整窗平均冒充区间平均。

观测源有两种形态，服务都支持：
1. 逐点观测（带 timestamp + value）：按窗口长度/步长切窗，窗口可精确拆分；
2. 预聚合窗口（带 actual_start/actual_end + 整窗值）：原样登记为整窗窗口。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.services.base import BaseModuleService
from app.services.window_utils import parse_timestamp
from app.store import store

_VALUE_FIELDS = ("value", "aggregated_value", "calibrated_value_V", "raw_value_mV",
                 "raw_value", "rollup_value", "观测值")
_START_FIELDS = ("actual_start", "window_actual_start", "window_start_time",
                 "start_time", "start", "begin", "窗口起始")
_END_FIELDS = ("actual_end", "window_actual_end", "window_end_time",
               "end_time", "end", "stop", "窗口结束")
_PROVENANCE_FIELDS = ("provenance", "originals", "observations", "sources", "观测出处")


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _pick_float(record: dict[str, Any], fields: tuple[str, ...]) -> float | None:
    for field in fields:
        if field in record and record[field] not in (None, ""):
            try:
                return float(record[field])
            except (TypeError, ValueError):
                continue
    return None


def _pick_timestamp(record: dict[str, Any], fields: tuple[str, ...]) -> str | None:
    for field in fields:
        parsed = parse_timestamp(record.get(field))
        if parsed is not None:
            return parsed.strftime("%Y-%m-%dT%H:%M:%S")
    return None


def _observation_timestamp(record: dict[str, Any]) -> str | None:
    return _pick_timestamp(record, ("timestamp", "aligned_timestamp", "original_timestamp",
                                    "采样时刻", "time", "ts"))


def _observation_value(record: dict[str, Any]) -> float | None:
    return _pick_float(record, _VALUE_FIELDS)


def _observation_point(record: dict[str, Any]) -> str | None:
    for field in ("point", "测点编号", "点编号", "source_point"):
        if record.get(field):
            return str(record.get(field))
    return None


def _aggregate(values: list[float], metric: str) -> float:
    if not values:
        return 0.0
    if metric in ("求和", "sum"):
        return round(sum(values), 4)
    if metric in ("最大值", "max"):
        return round(max(values), 4)
    if metric in ("最小值", "min"):
        return round(min(values), 4)
    if metric in ("计数", "count"):
        return round(float(len(values)), 4)
    return round(sum(values) / len(values), 4)


class RollupWindowService(BaseModuleService):
    MODULE = "rollup_window"
    ENTITY = "归并窗口"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = ["窗口编号", "测点编号", "窗口类型", "窗口长度", "滑动步长", "聚合指标",
                   "窗口条数", "时窗起止", "观测出处", "归并水位"]
    # 时窗起止/观测出处由归并计算产生，只读展示，不在登记时手填。
    READONLY_FIELDS = ["时窗起止", "观测出处", "可拆分窗口数", "整窗窗口数"]
    REQUIRED_FIELDS = ["窗口编号", "测点编号", "窗口类型"]
    STATUS_ORDER = ["草稿", "待归并", "已归并", "已作废"]
    ACTION_RULES = {"启动归并": "待归并", "确认归并": "已归并", "标记作废": "已作废"}
    # 数据归并操作：按窗口归并规则执行聚合计算生成归并结果数据集
    NEGATIVE_ACTIONS = []

    # -- 观测源定位 --

    def _observation_files(self, point_key: str) -> list[tuple[int, Path]]:
        """按保真度列出该测点可用的观测源文件，数字越小越接近逐点原件。"""
        sources: list[tuple[int, Path]] = []
        filtered = self._data_dir("filtered_series") / f"{point_key}_filtered.jsonl"
        if filtered.exists():
            sources.append((0, filtered))
        calibrated = self._data_dir("calibrated") / f"{point_key}_calibrated.jsonl"
        if calibrated.exists():
            sources.append((1, calibrated))
        edge = self._data_dir("edge_samples") / f"{point_key}.jsonl"
        if edge.exists():
            sources.append((2, edge))
        aligned_dir = self._data_dir("aligned")
        for path in sorted(aligned_dir.glob("*_aligned.jsonl")):
            if point_key in path.name:
                sources.append((3, path))
        ds_dir = self._data_dir("downsampled")
        for path in sorted(ds_dir.glob("*_downsampled.jsonl")):
            if point_key in path.name:
                sources.append((4, path))
        return sources

    def _pinned_source(self, entry: dict[str, Any]) -> tuple[Path, int] | None:
        """窗口登记了「源文件」时钉住真正的观测出处，避免同类文件互相污染。"""
        pinned = str(entry.get("源文件") or "").strip()
        if not pinned:
            return None
        candidate = Path(pinned)
        candidates = [candidate] if candidate.is_absolute() else [
            self._data_dir("downsampled") / pinned,
            self._data_dir("calibrated") / pinned,
            self._data_dir("filtered_series") / pinned,
            self._data_dir("edge_samples") / pinned,
        ]
        for path in candidates:
            if path.exists():
                tier = 4 if "downsampled" in path.name else 1
                return path, tier
        return None

    @staticmethod
    def _window_bounds_of(record: dict[str, Any]) -> tuple[str | None, str | None]:
        return (_pick_timestamp(record, _START_FIELDS),
                _pick_timestamp(record, _END_FIELDS))

    @staticmethod
    def _provenance_of(record: dict[str, Any], source_file: str,
                       start: str | None, end: str | None) -> list[dict[str, Any]]:
        """规范化观测出处：优先用源记录自带的逐条出处。"""
        for field in _PROVENANCE_FIELDS:
            items = record.get(field)
            if isinstance(items, list) and items:
                provenance = []
                for index, item in enumerate(items):
                    if isinstance(item, dict):
                        stamp = _observation_timestamp(item) or item.get("timestamp")
                        provenance.append({
                            "timestamp": stamp,
                            "source_file": item.get("source_file") or source_file,
                            "row_index": item.get("row_index", index),
                        })
                    else:
                        provenance.append({"timestamp": None,
                                           "source_file": source_file,
                                           "row_index": index})
                return provenance
        return [{"timestamp": start or end, "source_file": source_file, "row_index": None}]

    def _source_records(self, point_key: str, entry: dict[str, Any]) -> list[tuple[dict[str, Any], str, int]]:
        """读取一个保真层级里的所有记录，按测点过滤并返回 (记录, 文件名, 层级)。

        归并窗口自身登记了逐点原件或预聚合窗口时，优先用登记值：这既让窗口
        能钉住真正的观测出处，也让没有上游文件的场景可以离线演示。
        """
        inline = entry.get("逐点原件") or entry.get("observations")
        if isinstance(inline, list) and inline:
            source = str(entry.get("源文件") or f"{entry['窗口编号']}_inline")
            inline_rows: list[tuple[dict[str, Any], str, int]] = []
            for item in inline:
                if not isinstance(item, dict):
                    continue
                stamp = _observation_timestamp(item)
                value = _observation_value(item)
                if stamp is None or value is None:
                    continue
                inline_rows.append(({"point": point_key, "timestamp": stamp, "value": value},
                                    str(item.get("source_file") or source), 0))
            if inline_rows:
                start, end = self._window_bounds_of(entry)
                if start and end and _observation_value(entry) is not None:
                    # 登记了整窗值 + 起止 + 逐点原件：两者必须自洽才算可拆分。
                    observations = self._observations_of(entry, point_key, source)
                    return [({"point": point_key, "actual_start": start, "actual_end": end,
                              "rollup_value": _observation_value(entry),
                              "metric": entry.get("聚合指标"),
                              "observations": observations}, source, 0)]
                return inline_rows
        pinned = self._pinned_source(entry)
        if pinned is None and all(self._window_bounds_of(entry)) and \
                _observation_value(entry) is not None:
            source = str(entry.get("源文件") or f"{entry['窗口编号']}_inline")
            return [({"point": point_key,
                      "actual_start": entry.get("actual_start"),
                      "actual_end": entry.get("actual_end"),
                      "rollup_value": _observation_value(entry),
                      "metric": entry.get("聚合指标"),
                      "observations": entry.get("observations") or []}, source, 4)]
        # 统一成 (层级, 路径)，便于与自动发现的观测源走同一套逻辑。
        sources = [(pinned[1], pinned[0])] if pinned else self._observation_files(point_key)
        for tier in sorted({item[0] for item in sources}):
            rows: list[tuple[dict[str, Any], str, int]] = []
            for path in [p for t, p in sources if t == tier]:
                for row in self._read_jsonl(path):
                    if not isinstance(row, dict):
                        continue
                    row_point = _observation_point(row)
                    if row_point is not None and \
                            row_point.strip().casefold() != point_key.strip().casefold():
                        continue
                    rows.append((row, path.name, tier))
            if rows:
                return rows
        return []

    # -- 归并计算 --

    def _build_windows(self, entry: dict[str, Any],
                       rows: list[tuple[dict[str, Any], str, int]]) -> list[dict[str, Any]]:
        point_key = entry["测点编号"]
        window_type = entry.get("窗口类型", "固定窗口")
        metric = entry.get("聚合指标", "均值")
        window_len = _positive_int(entry.get("窗口长度"), 5)
        step = _positive_int(entry.get("滑动步长"), 1) if window_type == "滑动窗口" else window_len

        # 预聚合记录：自身带实际起止的整窗值，原样登记，不强行拼装。
        pre_aggregated = [
            item for item in rows
            if all(self._window_bounds_of(item[0])) and _observation_value(item[0]) is not None
        ]
        if pre_aggregated and len(pre_aggregated) == len(rows):
            windows = []
            for number, (row, source_file, _tier) in enumerate(rows, start=1):
                start, end = self._window_bounds_of(row)
                observations = self._observations_of(row, point_key, source_file)
                windows.append({
                    "window_id": entry["窗口编号"],
                    "window_ref": f"{entry['窗口编号']}#{number}",
                    "point": point_key,
                    "window_type": window_type,
                    "window_index": number,
                    "window_start": start,
                    "window_end": end,
                    "record_count": len(observations) or int(row.get("record_count") or 0),
                    "actual_start": start,
                    "actual_end": end,
                    "rollup_value": _observation_value(row),
                    "metric": row.get("metric") or metric,
                    "split_basis": "逐点原件" if observations else "整窗聚合",
                    "source_file": source_file,
                    "provenance": self._provenance_of(row, source_file, start, end),
                    "observations": observations,
                })
            return windows

        observations = []
        for row_index, (row, source_file, tier) in enumerate(rows):
            value = _observation_value(row)
            stamp = _observation_timestamp(row)
            if value is None:
                continue
            observations.append({
                "timestamp": stamp,
                "value": value,
                "point": point_key,
                "source_file": source_file,
                "row_index": row_index,
            })
        if not observations:
            return []
        # 降采样目录里的记录是聚合值，即便带时间戳也不具备逐点拆分能力。
        per_point = all(obs["timestamp"] for obs in observations) and \
            all(tier < 4 for _row, _name, tier in rows)
        if per_point:
            observations.sort(key=lambda item: item["timestamp"])

        merged: list[dict[str, Any]] = []
        index = 0
        number = 0
        while index < len(observations):
            window = observations[index:index + window_len]
            if not window:
                break
            # 尾部不足一窗的残片不单独成窗，避免同一滑动窗口被拆成多份身份。
            if window_type == "滑动窗口" and len(window) < window_len \
                    and len(observations) > window_len:
                break
            number += 1
            values = [float(item["value"]) for item in window]
            stamps = [item["timestamp"] for item in window if item["timestamp"]]
            merged.append({
                "window_id": entry["窗口编号"],
                "window_ref": f"{entry['窗口编号']}#{number}",
                "point": point_key,
                "window_type": window_type,
                "window_index": number,
                "window_start": index + 1,
                "window_end": index + len(window),
                "record_count": len(window),
                "actual_start": min(stamps) if per_point else None,
                "actual_end": max(stamps) if per_point else None,
                "rollup_value": _aggregate(values, metric),
                "metric": metric,
                "split_basis": "逐点原件" if per_point else "整窗聚合",
                "source_file": window[0]["source_file"],
                "provenance": [
                    {"timestamp": item["timestamp"], "source_file": item["source_file"],
                     "row_index": item["row_index"]}
                    for item in window
                ],
                "observations": window if per_point else [],
            })
            if step >= len(observations):
                break
            index += step
        return merged

    @staticmethod
    def _sorted_observations(rows: list[tuple[dict[str, Any], str, int]],
                             point_key: str) -> list[dict[str, Any]]:
        observations = []
        for row_index, (row, source_file, _tier) in enumerate(rows):
            value = _observation_value(row)
            stamp = _observation_timestamp(row)
            if value is None or stamp is None:
                continue
            observations.append({"timestamp": stamp, "value": value,
                                 "source_file": source_file, "row_index": row_index})
        observations.sort(key=lambda item: item["timestamp"])
        return observations

    def _attach_originals(self, entry: dict[str, Any], windows: list[dict[str, Any]],
                          observations: list[dict[str, Any]], source_name: str
                          ) -> tuple[list[dict[str, Any]], int]:
        """把逐点原件挂回既有窗口，保持窗口身份与实际起止不变。

        跨界窗口的关键就在这里：窗口还是那一个窗口，只是现在每一段观测都有了
        出处，交付服务能按申请区间把它切成相邻两段，而不是拿整窗均值顶替。
        """
        metric = entry.get("聚合指标", "均值")
        updated = 0
        for item in windows:
            start = _pick_timestamp(item, _START_FIELDS)
            end = _pick_timestamp(item, _END_FIELDS)
            if start is None or end is None:
                continue
            window_obs = [obs for obs in observations if start <= obs["timestamp"] <= end]
            if not window_obs:
                continue
            item["observations"] = window_obs
            item["record_count"] = len(window_obs)
            item["rollup_value"] = _aggregate([obs["value"] for obs in window_obs], metric)
            item["split_basis"] = "逐点原件"
            item["provenance"] = [
                {"timestamp": obs["timestamp"], "source_file": source_name,
                 "row_index": index}
                for index, obs in enumerate(window_obs)
            ]
            updated += 1
        return windows, updated

    @staticmethod
    def _observations_of(row: dict[str, Any], point_key: str,
                         source_file: str) -> list[dict[str, Any]]:
        items = None
        for field in ("observations", "originals", "逐点原件"):
            if isinstance(row.get(field), list):
                items = row[field]
                break
        if not items:
            return []
        observations = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            value = _observation_value(item)
            stamp = _observation_timestamp(item)
            if value is None or stamp is None:
                continue
            observations.append({
                "timestamp": stamp,
                "value": value,
                "point": point_key,
                "source_file": item.get("source_file") or source_file,
                "row_index": item.get("row_index", index),
            })
        observations.sort(key=lambda item: item["timestamp"])
        return observations

    # -- 归并执行 --

    def _supplement_file(self, entry: dict[str, Any]) -> Path:
        return self._data_dir("rollup_originals") / f"{entry['窗口编号']}_originals.jsonl"

    def rollup_windows(self, rollup_id: int) -> dict[str, Any]:
        """按窗口归并规则对降采样后的时序数据执行聚合计算，生成归并结果数据集。

        输出每条窗口的实际起止和观测出处；补报的逐点原件优先于整窗聚合值。
        """
        entry = store.find(self.MODULE, rollup_id)
        if entry is None:
            return {"error": f"归并窗口 {rollup_id} 不存在"}
        self._set_status(rollup_id, "启动归并")
        point_key = entry["测点编号"]

        supplement = self._supplement_file(entry)
        output_file = self._data_dir("rollup_windows") / f"{entry['窗口编号']}_rollup.jsonl"
        if supplement.exists():
            rows = [(row, supplement.name, 0) for row in self._read_jsonl(supplement)
                    if isinstance(row, dict)]
            # 已登记窗口保留身份与起止：补报只补证据，不改窗口边界。
            existing = [item for item in self._read_jsonl(output_file) if isinstance(item, dict)]
            if existing:
                merged, _updated = self._attach_originals(
                    entry, existing,
                    self._sorted_observations(rows, point_key), supplement.name)
            else:
                merged = self._build_windows(entry, rows)
        else:
            rows = self._source_records(point_key, entry)
            merged = self._build_windows(entry, rows)

        output_file = self._data_dir("rollup_windows") / f"{entry['窗口编号']}_rollup.jsonl"
        # 归并产物重写：同一窗口重复归并只保留最新口径，避免旧窗口残留。
        self._write_jsonl(output_file, merged, append=False)
        entry["窗口条数"] = str(len(merged))
        entry["归并水位"] = str(len(merged))
        self._update_window_summary(entry, merged)
        self._set_status(rollup_id, "确认归并")
        sources = sorted({item.get("source_file") for item in merged if item.get("source_file")})
        return {
            "rollup_id": entry["窗口编号"],
            "point": point_key,
            "window_type": entry.get("窗口类型", "固定窗口"),
            "window_period_count": len(merged),
            "metric": entry.get("聚合指标", "均值"),
            "source_file": ", ".join(sources) if sources else None,
            "observations": sum(1 for _row, _name, _tier in rows),
            "split_basis": "逐点原件" if merged and all(
                item.get("split_basis") == "逐点原件" for item in merged) else "整窗聚合",
            "windows": merged,
        }

    def _update_window_summary(self, entry: dict[str, Any],
                               merged: list[dict[str, Any]]) -> None:
        bounds = [
            f"{item['actual_start']}~{item['actual_end']}"
            for item in merged if item.get("actual_start") and item.get("actual_end")
        ]
        entry["时窗起止"] = "; ".join(bounds[:20])
        sources = sorted({item.get("source_file") for item in merged if item.get("source_file")})
        entry["观测出处"] = ", ".join(sources)
        entry["可拆分窗口数"] = str(sum(1 for item in merged if item.get("split_basis") == "逐点原件"))
        entry["整窗窗口数"] = str(sum(1 for item in merged if item.get("split_basis") != "逐点原件"))

    # -- 逐点原件补报 --

    def report_originals(self, rollup_id: int,
                         observations: list[dict[str, Any]] | None = None,
                         source_file: str | None = None) -> dict[str, Any]:
        """补报逐点原件：把真实观测挂回窗口，使跨界窗口可以按区间精确拆分。

        原件来源优先级：显式传入 > 指定文件 > ``rollup_originals/<窗口编号>_originals.jsonl``
        > 该测点现有逐点原件文件。补报后落盘并按原件重算窗口值。
        """
        entry = store.find(self.MODULE, rollup_id)
        if entry is None:
            return {"error": f"归并窗口 {rollup_id} 不存在"}
        point_key = entry["测点编号"]
        supplement = self._supplement_file(entry)

        ingested: list[dict[str, Any]] = []
        origin = "inline"
        if observations:
            for index, row in enumerate(observations):
                if not isinstance(row, dict):
                    continue
                value = _observation_value(row)
                stamp = _observation_timestamp(row)
                if value is None or stamp is None:
                    continue
                ingested.append({"timestamp": stamp, "value": value})
        else:
            explicit = None
            if source_file:
                candidate = Path(str(source_file))
                if not candidate.is_absolute():
                    candidate = self._data_dir("rollup_originals") / str(source_file)
                if candidate.exists():
                    explicit = candidate
            if explicit is not None:
                origin = explicit.name
                for row in self._read_jsonl(explicit):
                    if not isinstance(row, dict):
                        continue
                    value = _observation_value(row)
                    stamp = _observation_timestamp(row)
                    if value is None or stamp is None:
                        continue
                    row_point = _observation_point(row)
                    if row_point and row_point.strip().casefold() != point_key.strip().casefold():
                        continue
                    ingested.append({"timestamp": stamp, "value": value})
            elif supplement.exists():
                origin = supplement.name
                for row in self._read_jsonl(supplement):
                    if not isinstance(row, dict):
                        continue
                    value = _observation_value(row)
                    stamp = _observation_timestamp(row)
                    if value is None or stamp is None:
                        continue
                    ingested.append({"timestamp": stamp, "value": value})
            else:
                rows = self._source_records(point_key, entry)
                for row, name, _tier in rows:
                    value = _observation_value(row)
                    stamp = _observation_timestamp(row)
                    if value is None or stamp is None:
                        continue
                    ingested.append({"timestamp": stamp, "value": value})
                    origin = name

        ingested.sort(key=lambda item: item["timestamp"])
        # 同一时刻只保留一条，避免补报重复放大贡献。
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in ingested:
            if item["timestamp"] in seen:
                continue
            seen.add(item["timestamp"])
            deduped.append(item)
        if not deduped:
            return {"error": f"归并窗口 {entry['窗口编号']} 没有可用的逐点原件"}

        self._write_jsonl(
            supplement,
            [{"timestamp": item["timestamp"], "value": item["value"], "point": point_key}
             for item in deduped],
            append=False,
        )

        rollup_file = self._data_dir("rollup_windows") / f"{entry['窗口编号']}_rollup.jsonl"
        existing = self._read_jsonl(rollup_file)
        existing = [item for item in existing if isinstance(item, dict)]
        updated = 0
        if existing:
            # 窗口身份、实际起止都保持不变，只把原件与按原件重算的值挂回去。
            existing, updated = self._attach_originals(entry, existing, deduped, supplement.name)
        if updated == 0:
            rows = [(row, supplement.name, 0) for row in self._read_jsonl(supplement)]
            existing = self._build_windows(entry, rows)
            updated = len(existing)
        if not existing:
            return {"error": f"归并窗口 {entry['窗口编号']} 补报原件无法构建窗口"}

        self._write_jsonl(rollup_file, existing, append=False)
        entry["窗口条数"] = str(len(existing))
        entry["归并水位"] = str(len(existing))
        entry["逐点原件补报"] = origin
        self._update_window_summary(entry, existing)
        return {
            "rollup_id": entry["窗口编号"],
            "point": point_key,
            "origin": origin,
            "originals_ingested": len(deduped),
            "windows_updated": updated,
            "windows": existing,
        }

    # -- 只读视图 --

    def window_detail(self, rollup_id: int) -> dict[str, Any]:
        """返回窗口的实际起止与纳入观测出处，供工作台核对是否夹带外测点。"""
        entry = store.find(self.MODULE, rollup_id)
        if entry is None:
            return {"error": f"归并窗口 {rollup_id} 不存在"}
        rollup_file = self._data_dir("rollup_windows") / f"{entry['窗口编号']}_rollup.jsonl"
        windows = [item for item in self._read_jsonl(rollup_file) if isinstance(item, dict)]
        return {
            "rollup_id": entry["窗口编号"],
            "point": entry["测点编号"],
            "window_type": entry.get("窗口类型", ""),
            "metric": entry.get("聚合指标", ""),
            "window_count": len(windows),
            "split_basis": "逐点原件" if windows and all(
                item.get("split_basis") == "逐点原件" for item in windows) else "整窗聚合",
            "source_files": sorted({item.get("source_file") for item in windows if item.get("source_file")}),
            "windows": [
                {
                    "window_ref": item.get("window_ref", ""),
                    "actual_start": item.get("actual_start"),
                    "actual_end": item.get("actual_end"),
                    "record_count": item.get("record_count", 0),
                    "rollup_value": item.get("rollup_value"),
                    "split_basis": item.get("split_basis", ""),
                    "source_file": item.get("source_file", ""),
                    "provenance": item.get("provenance", []),
                    # 只回传出处摘要，逐点原件留在文件里，避免列表接口膨胀。
                    "observation_count": len(item.get("observations") or []),
                }
                for item in windows
            ],
        }

    def compute_cumulative_rollup(self, rollup_id: int) -> dict[str, Any]:
        """对归并窗口数据计算累计聚合指标写入累计快照数据文件。"""
        entry = store.find(self.MODULE, rollup_id)
        if entry is None:
            return {"error": f"归并窗口 {rollup_id} 不存在"}
        rollup_result = self.rollup_windows(rollup_id)
        if "error" in rollup_result:
            return rollup_result
        rollup_file = self._data_dir("rollup_windows") / f"{entry['窗口编号']}_rollup.jsonl"
        records = self._read_jsonl(rollup_file)
        cumulative = []
        running_sum = 0.0
        for i, r in enumerate(records):
            val = float(r.get("rollup_value", 0))
            running_sum += val
            cumulative.append({
                "window_index": i + 1,
                "point": r["point"],
                "window_value": val,
                "cumulative_value": round(running_sum, 4),
                "window_id": r["window_id"],
            })
        cum_file = self._data_dir("rollup_windows") / f"{entry['窗口编号']}_cumulative.jsonl"
        self._write_jsonl(cum_file, cumulative)
        return {"rollup_id": entry["窗口编号"], "cumulative_periods": len(cumulative),
                "final_cumulative_value": round(running_sum, 4)}
