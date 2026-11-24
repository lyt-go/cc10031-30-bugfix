# 工业传感器时序采集与降采样平台

面向工业传感器高频时序数据的测点接入、边缘缓冲上送、单位标定、时间对齐、降采样归并与分层归档的采集平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 测点台账 | `sensor_point` | 测点档案 | 测点编号、所属产线、传感器型号 |
| 边缘缓冲 | `edge_buffer` | 缓冲分片 | 分片编号、测点编号、起始时刻 |
| 批量上送 | `upload_batch` | 上送批次 | 批次编号、分片范围、测点数量 |
| 单位标定 | `unit_calibrate` | 标定记录 | 标定编号、测点编号、原始单位 |
| 时间对齐 | `time_align` | 对齐规则 | 规则编号、对齐粒度、时区基准 |
| 断点填补 | `gap_fill` | 填补区间 | 区间编号、测点编号、缺失起点 |
| 尖峰剔除 | `spike_filter` | 异常采样 | 异常编号、测点编号、采样时刻 |
| 降采样 | `downsample` | 降采样任务 | 任务编号、源序列、源采样频率 |
| 窗口归并 | `rollup_window` | 归并窗口 | 窗口编号、测点编号、窗口类型 |
| 序列索引 | `series_index` | 索引分区 | 索引编号、测点编号、时间分区 |
| 冷热分层 | `tier_storage` | 分层策略 | 策略编号、时间边界、热区范围 |
| 采集心跳 | `device_health` | 心跳记录 | 心跳编号、采集终端、上报时刻 |
| 接入水位 | `ingest_watermark` | 水位标记 | 水位编号、测点编号、事件时间水位 |
| 区间重采 | `range_replay` | 重采任务 | 重采编号、测点编号、起始时刻 |
| 采集血缘 | `series_lineage` | 血缘链路 | 链路编号、测点编号、上送批次 |
| 序列导出 | `series_export` | 导出任务 | 导出编号、测点范围、时间范围 |
| 时序质量 | `series_quality` | 质量报告 | 报告编号、测点编号、统计窗口 |
| 序列快照 | `series_snapshot` | 版本快照 | 快照编号、口径说明、测点范围 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 窗口归并与序列交付的对账口径

窗口归并（`rollup_window`）与序列导出（`series_export`）不是简单的文件搬运，
两者按「实际发生的观测」对账：

- **窗口带实际起止与出处**。每条归并结果都写入 `actual_start` / `actual_end`
  （观测的真实首末时刻）以及 `provenance`（每条观测来自哪个文件、第几行）。
  列表里的「时窗起止」「观测出处」是只读派生字段。
- **只纳入本测点观测**。归并只读该测点钉住的源文件，并按 `point` 二次过滤，
  同一目录里的其他测点不会被夹带进窗口。
- **交付按申请对账**。导出任务只声明「测点范围 + 时间范围」，交付时逐窗判断：
  - 申请测点之外 → **无关测点**；
  - 窗口与申请区间无交集 → **完全超界**；
  - 跨界窗口且只有整窗均值 → **缺拆分依据**（拒绝把整窗平均当成区间平均）。
  有逐点原件的窗口按申请区间重新计算统计贡献，并标注 `partial=true`。
- **跨界窗口可精确拆开**。同一个滑动窗口被相邻两次申请各取一段时，两段观测
  互不重叠、合起来等于整窗；补报逐点原件（`report_originals`）只补证据、
  不改窗口身份和实际起止。
- **受理与确认分开**。「启动导出」只做对账快照（`_accepted.jsonl`，状态停在导出中），
  「确认交付」才按确认时的最新归并结果重写 `<导出编号>_export.<格式>` 与
  `<导出编号>_exclusions.jsonl`。两个文件都是覆盖写，切换相邻窗口不会残留旧数据。
- **排除明细独立计数**。交付结果与排除明细都带 `window_ref`、实际起止和出处；
  `summary` 给出 `source_windows = delivered_windows + excluded_windows` 的对平结果，
  使外部读取的值与拒收数能和源输入对上。

只读查询接口：

| 接口 | 用途 |
| --- | --- |
| `GET /api/rollup_window/{id}/windows` | 窗口的实际起止、观测出处与拆分依据 |
| `GET /api/series_export/{id}/delivery` | 一次交付的交付记录与排除明细 |
| `GET /api/series_export/{id}/exclusions` | 三类排除分别计数并与源输入对平 |

示例数据里已有测点 A 的固定窗（`ROLL-A-FIX`）、跨界滑窗（`ROLL-A-SLIDE`，起初只有整窗均值）、
完全超范围窗（`ROLL-A-OUT`）与测点 B 的窗口（`ROLL-B-FIX`），以及 10:00~11:00、
11:00~12:00 两份相邻申请（`EXPORT-A-1000` / `EXPORT-A-1100`）。

## 测试

```bash
cd backend
.venv/bin/python -m pytest -q
```

`tests/test_window_delivery.py` 覆盖上述对账口径；`tests/test_pipeline.py` 覆盖原有
10 条跨模块链路。

领域：数据工程
子方向：数据采集、爬虫与RPA
