# 路跑分段计时裁决服务（Road Race Split Timing）

基于 **Python 3.12 / FastAPI / SQLAlchemy 2 / PostgreSQL 16** 的分段计时裁决 HTTP 服务。

- 赛事创建时录入起跑时间、截止时间、**有序检查点与终点**、每个分段（起跑→首点、之后各段）的
  **非负耗时闭区间** `[min_seconds, max_seconds]`、选手↔芯片绑定；**配置创建后固定**，不提供修改接口。
- 扫描记录包含唯一编号 `scan_no`、芯片、点位、发生时间，**可乱序提交**；
  存储的始终是设备上报的**原始** `occurred_at`。
  - 同编号同内容重传 → 幂等（HTTP 200，`replayed=true`）；
  - 同编号异内容 → `409` 冲突；
  - 未知芯片 / 未知点位 → `404` 拒收；封存后提交 → `409`。
- **点位时钟偏移**：工作人员可按“赛事 + 点位”设置或替换**有符号整数秒**
  偏移（设备时钟相对标准时间的修正量）；未设置按 `0`。
  - 查询返回各点当前偏移；未知赛事 / 未知点位 `404`，非整数（含小数、
    布尔）偏移 `422` 明确拒收；封存后拒绝变更（`409`）。
- 裁决规则：每条有效扫描的**校准时间 = 原始 occurred_at + 该点偏移**，
  随后要求
  - 相邻所用扫描的校准时间**严格递增**；
  - 每段（按校准时间计算的）耗时落在对应**闭区间**内；
  - 终点校准时间不晚于截止时间。
  - 在所有合法完整路径中选**完赛（到达终点）校准时间最早**的一条；
    完赛时间相同则取扫描编号序列**字典序最小**；无合法完整路径记为
    `did_not_finish`。成绩中**保留原始扫描时间并同时展示校准后时间**。
- 偏移变更后，暂定成绩以同一批**已提交扫描 + 当前偏移版本**立即重算；
  偏移变更与扫描提交 / 撤销 / 封存共用赛事行锁、彼此串行。
- 封存（seal）前撤销扫描会使**暂定成绩重算**；封存时按当时偏移冻结
  **一致快照**，此后提交、撤销、偏移变更一律拒绝，快照不受后续请求影响。
- `docker compose up --build` 一键启动（应用启动时自动建表，含数据库连接重试）。

---

## 1. 一键启动

```bash
docker compose up --build
```

启动后：

| 服务 | 地址 |
| --- | --- |
| API 基础地址 | <http://127.0.0.1:8000> |
| 交互式文档（Swagger UI） | <http://127.0.0.1:8000/docs> |
| OpenAPI 描述 | <http://127.0.0.1:8000/openapi.json> |
| 健康检查 | `GET http://127.0.0.1:8000/health` |
| PostgreSQL | `127.0.0.1:5432`（库 `race_timing`，用户/密码 `racer/racer`） |

停止并清空数据：

```bash
docker compose down -v
```

> 表结构在应用启动时通过 SQLAlchemy `create_all` 自动创建（`app/database.py::init_db`），
> Compose 中配置了 Postgres 健康检查与应用侧连接重试，因此无需手动初始化数据库。

### 本地（无 Docker）运行

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL="postgresql+psycopg2://racer:racer@127.0.0.1:5432/race_timing"
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg2://racer:racer@db:5432/race_timing` | SQLAlchemy/psycopg2 连接串 |

时间一律使用带时区的 ISO-8601（建议 UTC，如 `2026-10-01T08:00:00+00:00`）；
分段耗时单位为**秒**，数据库以 `NUMERIC(12,3)` 存储（毫秒精度）。
点位时钟偏移为**有符号整数秒**（`INTEGER`，范围 ±86400），不接受小数。

---

## 2. HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/races` | 创建赛事（配置固定）；非法时窗/区间/重复芯片等返回 `422` |
| `GET` | `/races/{race_id}` | 查询赛事配置 |
| `POST` | `/races/{race_id}/scans` | 提交单条扫描（可乱序）；幂等重传 200 / 新建 201 / 冲突 409 |
| `POST` | `/races/{race_id}/scans/batch` | 批量提交，逐条返回状态码（同一事务） |
| `DELETE` | `/races/{race_id}/scans/{scan_no}` | 封存前撤销扫描（暂定成绩随后重算） |
| `GET` | `/races/{race_id}/offsets` | 查询各点当前有符号整数秒偏移（未设置为 0） |
| `PUT` | `/races/{race_id}/points/{point_code}/offset` | 设置/替换单点位偏移；封存后 `409`，非整数 `422` |
| `GET` | `/races/{race_id}/results` | 全部选手成绩（封存前实时暂定，封存后取快照） |
| `GET` | `/races/{race_id}/runners/{runner_id}/result` | 单个选手成绩 |
| `POST` | `/races/{race_id}/seal` | 封存：冻结一致快照；重复封存幂等 |
| `GET` | `/health` | 健康检查 |

### 2.1 创建赛事

```jsonc
POST /races
{
  "name": "City 10K 2026",
  "start_time": "2026-10-01T08:00:00+00:00",
  "cutoff_time": "2026-10-01T09:00:00+00:00",
  "points": [
    {"code": "C1", "name": "Checkpoint 1"},
    {"code": "C2", "name": "Checkpoint 2"},
    {"code": "FIN", "name": "Finish", "is_finish": true}
  ],
  "segments": [
    {"min_seconds": 300, "max_seconds": 1800},   // START -> C1
    {"min_seconds": 300, "max_seconds": 1800},   // C1 -> C2
    {"min_seconds": 300, "max_seconds": 1800}    // C2 -> FIN
  ],
  "runners": [
    {"bib": "A01", "name": "Alice", "chip": "CHIP-A"},
    {"bib": "D04", "name": "Dan",   "chip": "CHIP-D"}
  ]
}
```

校验（不通过返回 `422`）：

- `cutoff_time > start_time`（无效时窗拒收）；
- 点代码非空、不重复（最后一个有序点即终点；`START` 为保留字）；
- `segments` 数量必须等于点数（起跑→首点一段，其后相邻点各一段）；
- 每个区间 `0 <= min_seconds <= max_seconds`（非法区间拒收）；
- `bib` 与 `chip` 在赛事内唯一（重复芯片/号码拒收）。

### 2.2 提交扫描

```jsonc
POST /races/{race_id}/scans
{"scan_no": "A-010", "chip": "CHIP-A", "point": "C1",
 "occurred_at": "2026-10-01T08:10:00+00:00"}
```

- 扫描可乱序提交（先发终点、后发检查点也可以）。
- `scan_no` 在赛事内唯一。同 `scan_no` 且 `chip/point/occurred_at` 完全相同 →
  HTTP `200` 且 `replayed=true`；内容不同 → `409 scan_conflict`。
  该编号若此前已被撤销，同内容重传仍返回该记录（`revoked=true`）；异内容仍冲突，
  新观测请使用新编号。
- 芯片未绑定或点位不存在 → `404`；赛事已封存 → `409 race_sealed`。

批量接口 `POST /races/{race_id}/scans/batch` 的请求体为 `{"scans":[ ... ]}`，
响应为与输入等长、同序的数组，每项带独立 `status_code` / `error` / `detail` / `scan`。

### 2.3 点位时钟偏移

点位设备时钟可能与标准时间偏离。每个赛事点位保存一个**有符号整数秒**
偏移，含义为“设备时间相对标准时间慢/快多少秒”：裁决使用的校准时间为

```
calibrated_at = 原始 occurred_at + offset_seconds
```

时钟慢 30 秒就设置 `+30`（设备显示 08:09:30 时标准时间已是 08:10:00）；
时钟快则设置负数。未设置偏移的点位一律按 `0` 处理。

```jsonc
GET /races/{race_id}/offsets
{
  "race_id": "...", "sealed": false,
  "offsets": [
    {"position": 1, "code": "C1", "name": "Checkpoint 1",
     "is_finish": false, "offset_seconds": 0},
    {"position": 3, "code": "FIN", "name": "Finish",
     "is_finish": true, "offset_seconds": 30}
  ]
}

PUT /races/{race_id}/points/FIN/offset
{"offset_seconds": 30}        // 首次设置或整体替换；200 返回全部点位最新偏移
PUT /races/{race_id}/points/FIN/offset
{"offset_seconds": 0}         // 置回零等价于“取消修正”
```

- 只接受**整数**：`60.5` / `1.5` 等小数以及布尔值返回 `422`（有范围限制
  ±86400 秒，防止误填）；未知赛事返回 `404 race_not_found`，未知点位返回
  `404 unknown_point`。
- 扫描提交始终记录原始 `occurred_at`；偏移变更**不改动任何扫描记录**，
  现有 `scan_no` 同内容重传 / 异内容冲突 / 撤销语义保持不变。
- 变更与扫描提交、批量提交、撤销、封存共用赛事行 `FOR UPDATE` 锁，
  彼此串行；提交成功后下一次查询即按“同一批已提交扫描 + 新偏移版本”
  重算暂定成绩，无需重新提交扫描。
- 封存后偏移仍可查询，但变更返回 `409 race_sealed`；封存快照使用封存
  当时的偏移版本，此后任何请求都不会改变它。

### 2.4 成绩

```jsonc
GET /races/{race_id}/results
{
  "race_id": "...", "name": "City 10K 2026",
  "sealed": false, "sealed_at": null,
  "results": [
    {
      "runner_id": "...", "bib": "A01", "name": "Alice", "chip": "CHIP-A",
      "status": "finished",
      "finish_time": "2026-10-01T08:30:30+00:00",
      "total_seconds": 1830.0,
      "rank": 1,
      "used_scans": [
        {"segment": 1, "point": "C1", "scan_no": "A-010",
         "occurred_at":   "2026-10-01T08:10:00+00:00",
         "calibrated_at": "2026-10-01T08:10:00+00:00",
         "offset_seconds": 0, "segment_seconds": 600.0},
        {"segment": 3, "point": "FIN", "scan_no": "A-030",
         "occurred_at":   "2026-10-01T08:30:00+00:00",
         "calibrated_at": "2026-10-01T08:30:30+00:00",
         "offset_seconds": 30, "segment_seconds": 630.0}
      ]
    },
    {"status": "did_not_finish", "finish_time": null, "total_seconds": null,
     "rank": null, "used_scans": []}
  ]
}
```

- 每条 `used_scans` 同时给出原始 `occurred_at`、校准后 `calibrated_at`
  与所用 `offset_seconds`；`finish_time`、`total_seconds`、
  `segment_seconds`、排名与选路全部基于校准时间。
- 完赛者按校准 `finish_time` 升序排名；相同完赛时间按所用扫描编号序列
  字典序排序。未完赛者排在后面，`rank=null`。
- 裁决的选路是按点位顺序的动态规划：每个点保留“校准到达最早、同到达
  时间取扫描编号序列字典序最小”的前驱路径；最后一层再按（校准完赛时间，
  编号序列）取最优。

### 2.5 撤销与封存

- `DELETE /races/{race_id}/scans/{scan_no}`：封存前可用；撤销后 `GET /results`
  立即反映重算的暂定成绩。重复撤销返回 `409`，未知编号返回 `404`。
- `POST /races/{race_id}/seal`：按当时已提交扫描与当前偏移版本对全部选手
  计算一次并写入快照（`result_snapshots`，含每条扫描的原始/校准时间），
  同时置 `sealed=true / sealed_at=...`；该操作对赛事行加 `FOR UPDATE` 行锁，
  与扫描提交 / 撤销 / 偏移变更互斥，保证快照与已提交数据一致。重复调用幂等
  并返回同一份快照。
- 封存后任何 `POST .../scans`、`.../scans/batch`、`DELETE .../scans/...`、
  `PUT .../points/.../offset` 均返回 `409`；偏移与成绩仍可 `GET`，成绩取
  封存快照，不受后续请求影响。

---

## 3. 调用示例

服务启动后另开终端：

```bash
# 端到端 Python 示例（仅用标准库）：覆盖建赛、拒收、乱序提交、幂等/冲突、
# 最早完赛 + 字典序决胜、撤销重算、点位偏移（整数校验/原始与校准时间/
# 变更后立即重算）、封存后拒绝等全部场景
BASE_URL=http://127.0.0.1:8000 python3 examples/demo.py

# 或最小 curl 走查（含偏移的查询/设置/拒收/封存后拒绝）
bash examples/curl.sh
```

`examples/demo.py` 中内置了字典序决胜场景：选手 Dan 在三个点各有两条
发生时间相同的扫描，编号路径 `"DA01","DB01","DC01"` 与 `"DA02","DB02","DC02"`
完赛时间一致，服务选择扫描编号元组字典序更小的 `"DA01","DB01","DC01"`。

---

## 4. 运行测试

裁决算法（`app/adjudication.py`）不依赖任何第三方库，可直接用系统 Python 测试：

```bash
python3 tests/test_adjudication.py
# 或（容器/虚拟环境内）
pytest
```

覆盖：乱序提交、最早完赛路径、同成绩字典序决胜、相邻扫描等时刻拒收、
区间越界、闭区间边界（含起点 `min=0` 时恰在起跑瞬间）、缺终点、晚于截止、
恰好等于截止可完赛，以及点位偏移：正/负偏移校准分段与终点、越界与截止
的修复或失效、严格递增按校准时间判定、按校准时间排名、原始时间保留。

---

## 5. 项目结构

```
app/
  main.py          # FastAPI 路由与异常映射
  schemas.py       # Pydantic v2 请求/响应模型与入参校验
  database.py      # 引擎、会话、ORM 模型、建表（含连库重试）
  service.py       # 事务/行锁、扫描幂等与冲突、撤销、点位偏移、成绩、封存快照
  adjudication.py  # 纯函数裁决（原始时间+偏移校准，动态规划选路，零第三方依赖）
examples/
  demo.py          # 端到端调用示例（标准库 urllib）
  curl.sh          # curl 调用示例
tests/
  test_adjudication.py
Dockerfile
docker-compose.yml
requirements.txt
```

### 主要数据表

- `races`：起跑/截止时间、封存标志与封存时间（配置仅在建赛时写入）；
- `race_points`：赛事内有序点位（`(race_id,position)`、`(race_id,code)` 唯一），
  含每点有符号整数秒 `offset_seconds`（默认 `0`，封存前可改）；
- `segments`：按序闭区间（秒，`NUMERIC(12,3)`）；
- `runners`：号码与芯片（赛事内各自唯一）；
- `scans`：`(race_id,scan_no)` 唯一，绑定选手与点位，仅存原始时间，含撤销标志；
- `result_snapshots`：封存时每名选手一份 JSONB 成绩快照（原始与校准时间、
  偏移版本均冻结在内，赛事唯一批次）。
