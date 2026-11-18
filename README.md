# 防汛雨量数据平台

把雨量站主、备翻斗计的**累计脉冲计数**转换为可信的 **UTC 小时降雨量**，供预警模型取数。
Flask + SQLite，网页上传/查询 + 等效 JSON API，Docker Compose 一键启动。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:8000
```

数据库文件保存在命名卷 `rainfall-data`（容器内 `/data/rainfall.db`），重启后数据仍可查询。

本地开发（无需 Docker）：

```bash
pip install -r requirements.txt
python -m app.app                 # 开发服务器，监听 8000
# 或生产形态（与 Docker CMD 相同）：
gunicorn --bind 0.0.0.0:8000 --workers 1 --threads 8 "app.app:create_app()"
```

运行自测（共 46 个用例）：

```bash
python -m unittest discover -s tests -v
```

## CSV 格式

列顺序：`站点, 主备标记, UTC整分钟, 计数(0~9999), 修订号`；表头可选（支持中英文列名），无表头时按此顺序解析。

```csv
station,device,minute,counter,revision
ST01,primary,2026-11-17T10:00:00Z,9980,1
ST01,backup,2026-11-17T10:00:00Z,120,1
```

- **站点**：非空字符串（≤64 字符）。
- **主备标记**：`primary`/`backup`，也接受 `主`/`备` 等别名。
- **UTC 整分钟**：如 `2026-11-17T10:00:00Z`、`2026-11-17 10:00`；未带时区按 UTC 处理，带偏移会换算成 UTC；秒必须为 00。
- **计数**：整数 0~9999。
- **修订号**：非负整数。同一（站点, 设备, 分钟）**只接受更高修订号**，否则该行被拒绝并记入批次报告。

示例文件：`examples/sample.csv`（含完整小时、备计、回绕、缺口四种情形）。

## 计算规则

- 每小时 = 60 个相邻分钟间隔（整点 H:00 至下一整点，共 61 个端点）。
- 每个间隔：**主计两端齐全取主计差值，否则备计两端齐全取备计差值，绝不跨设备相减**；两端都无可用数据则该间隔缺失。
- 计数下降按一次 10000 回绕处理：差值 = (末 − 初) mod 10000。
- 每脉冲 0.2 mm；**任一间隔缺失则整小时不可供数**（`status=incomplete`，`rainfall_mm=null`），网页会标出缺口与每段来源。

## 上下游时差分析（预警模型候选提前量）

预警模型的候选提前量参考上、下游雨量站的降雨时差。在网页 `/lag`（首页有入口）或 API `/api/lag`
选择**上游站、下游站、UTC 起始/结束整点（含）和 1～6 小时最大时差**，平台按现有**可供数小时雨量**
比较 0～最大时差各候选的皮尔逊相关系数。

配对与取数规则：

- 配对方向：**上游小时 h 与下游延后小时 h+时差** 配对（时差 0 表示两站同一小时）。
- **所有候选只用共同可配对的上游小时**：一个上游小时 h 必须对 0～最大时差的每个时差都能配上
  （窗口内且上游 h、下游各延后小时均为可供数小时），才会进入序列；因此各候选的配对集合与配对数完全相同，系数可横向比较。
- 不可供数的小时（任一间隔缺失）自动剔除，不参与任何候选。

无法判断的三种情形（结论 `无法判断`，`reason` 分别为）：

| reason | 触发条件 |
|---|---|
| `insufficient_pairs` | 共同可配对上游小时不足 8 对（此时不计算系数） |
| `no_variation` | 配对后任一序列无波动（常数序列），皮尔逊系数无定义 |
| `non_positive` | 所有可计算候选的最高系数不大于 0，不支持正相关提前量 |

存在正相关最高系数时结论为 `可判断`；**最高系数并列时 `best_lags` 列出全部并列时差，不擅选一个**
（`is_tie=true`）。页面列出每个候选的系数、配对小时数与逐对配对明细，API 返回同一结果。

示例：

```bash
curl "http://localhost:8000/api/lag?upstream=UP&downstream=DOWN\
&start=2026-11-18T00:00Z&end=2026-11-18T13:00Z&max_lag=3"
```

返回要点：`conclusion` / `conclusion_text`、`best_lags`、`best_coefficient`、`is_tie`、
`common_pair_count`、`common_upstream_hours`，以及 `candidates[]`
（每个候选含 `lag_hours`、`coefficient`、`pair_count`、`pairs[]` 逐对小时与毫米数）。

## 接口

| 接口 | 说明 |
|---|---|
| `GET /` | 首页：上传、查询、时差分析入口、站点与批次列表 |
| `POST /upload` | 网页上传（文件或粘贴文本），返回批次报告页 |
| `POST /api/upload` | 同上，JSON 返回 |
| `GET /report?station=..&hour=..` | 网页小时报告（雨量、缺口、每段来源） |
| `GET /api/hourly?station=..&hour=..` | 等效 JSON（与网页同一计算入口，结果一致） |
| `GET /lag?upstream=..&downstream=..&start=..&end=..&max_lag=..` | 网页上下游时差分析（各候选系数、配对小时与结论） |
| `GET /api/lag?upstream=..&downstream=..&start=..&end=..&max_lag=..` | 时差分析等效 JSON（与网页同一计算入口） |
| `GET /api/stations` | 站点数据范围 |
| `GET /api/batches` | 上传批次列表 |
| `GET /batches/<id>` | 批次行级明细（接受/拒绝及原因） |
| `GET /api/health` | 健康检查 |

`hour`、`start`、`end` 均需整点对齐，如 `2026-11-17T10:00Z`；`max_lag` 为 1～6 的整数。
预警模型取数时请检查 `status == "ok"` 再使用 `rainfall_mm`。

## 一致性与可靠性设计

- **两小时一致**：小时雨量由 `readings` 表现算现算（无缓存），补传或修订整点读数提交后，前后两小时立即同时反映新值；网页与 API 共用同一计算函数。
- **时差分析同快照**：一次 `/lag`（或 `/api/lag`）查询的两站读数在**同一个只读快照事务**中取出，补传与查询交错时，两站及全部候选时差都取自同一已提交状态的有效读数；修订提交后重查即更新结论，失败上传或被拒的低修订行不改变任何结果。
- **原子性**：每批上传的读数、批次记录、行级明细在**单个事务**中写入，任何失败整体回滚，不留半批新结果。
- **持久化**：SQLite（WAL 模式）落盘于挂载卷，重启后仍可查询。
- **并发**：单 gunicorn worker 多线程 + SQLite `BEGIN IMMEDIATE` 与 `busy_timeout`，写入串行化；分析查询用只读 `BEGIN` 快照，不阻塞写入提交。

## 目录结构

```
app/
  core.py        # 纯逻辑：CSV 解析校验、小时雨量计算、皮尔逊系数与时差分析（不依赖 Flask/SQLite）
  db.py          # SQLite：模式、事务性批量入库、查询、只读快照
  app.py         # Flask 路由（网页 + API）
  templates/     # 页面模板（含 lag.html 时差分析页）
tests/test_app.py  # 原有 27 个自测用例
tests/test_lag.py  # 时差分析 19 个自测用例
examples/sample.csv
Dockerfile / docker-compose.yml / requirements.txt
```
