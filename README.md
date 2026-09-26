# 计量校准溯源 API（Metrological Calibration Traceability API）

基于 **Python FastAPI + SQLite** 构建。实验室提交仪器、校准证书、父子溯源关系及有效区间；
服务按指定时点从仪器向国家基准重建唯一证书链，校验：

- **证书区间连续性** — 同一仪器历史证书有效期的**闭区间并集**必须连续、不得断档
  （嵌套或重叠的区间只是并集内部冗余，不会制造假空隙）；
- **签名摘要** — 按规范化证书载荷重算摘要并与登记摘要比对（SHA-224/256/384/512）；
- **测量不确定度** — 扩展不确定度沿溯源方向（下级→上级）只能逐级收缩，即
  下级（子）不确定度不得小于上级（父），且单位必须一致；
- **溯源时点有效性** — 每一级父证书与溯源边在查询时点及子证书校准时刻都必须有效。

当存在多条成链候选（重复送检/多渠道溯源）或区间断裂（无有效证书、死路、父证书失效、环）时，
返回带 `code / severity / node / path_index / detail / 中文说明` 的**可解释冲突列表**。

---

## 一、一键启动（Docker Compose）

只需一条命令（构建镜像、安装 `requirements.txt` 依赖、容器启动时自动建库，
库为空则按 `SEED_DEMO` 播种演示数据；服务监听 **8000** 端口）：

```bash
docker compose up -d --build
```

API 统一通过宿主机 **8000** 端口访问（`docker-compose.yml` 把容器
`8000` 映射到宿主机 `8000`；`Dockerfile` 的 `CMD` 即
`uvicorn app.main:app --host 0.0.0.0 --port 8000`）：

| 用途 | 地址 |
| --- | --- |
| API 根（端点索引） | http://localhost:8000/ |
| Swagger 交互文档 | http://localhost:8000/docs |
| ReDoc 文档 | http://localhost:8000/redoc |
| OpenAPI Schema | http://localhost:8000/openapi.json |
| 健康检查 | http://localhost:8000/health |

**必要配置（环境变量，均有缺省值，docker-compose.yml 已显式给出）：**

| 变量 | 缺省值 | 说明 |
| --- | --- | --- |
| `TRACE_DB_PATH` | `/data/traceability.db`（容器内） | SQLite 数据文件路径；父目录不存在会自动创建 |
| `SEED_DEMO` | `1`（Dockerfile/Compose） | `1`：启动且库为空时自动写入演示数据（5 个场景，见第六节）；`0`：不播种 |

- 容器**启动即自动建库**（`CREATE TABLE IF NOT EXISTS …`，幂等），无需
  手工初始化；SQLite 数据文件持久化在 Docker 卷 `trace-data`
  （容器内 `/data/traceability.db`）。
- 不需要演示数据时，将 `docker-compose.yml` 中的 `SEED_DEMO` 改为 `"0"`。
- 修改代码后重建：`docker compose up -d --build`；清空数据：`docker compose down -v`。

仅用 Docker 运行（等价的一条启动命令，同样监听 8000）：

```bash
docker build -t metrological-traceability-api . && \
docker run --rm -p 8000:8000 -e SEED_DEMO=1 -v trace-data:/data \
  metrological-traceability-api
```

容器启动后可用健康检查与批量提交预检（dry-run，不写库）快速验证部署：

```bash
curl -fsS http://localhost:8000/health                 # {"status":"ok"}
curl -s -X POST http://localhost:8000/api/submissions/preflight \
  -H 'Content-Type: application/json' \
  -d '{"instruments":[],"certificates":[],"edges":[]}'
# {"strict_digest":false,"dry_run":true,"ok":true,...,
#  "would_upsert":{"instruments":0,"certificates":0,"edges":0,"total":0}}
```

## 二、本地开发运行（无 Docker）

依赖安装、启动建库与端口均与容器内一致（uvicorn 监听 8000）：

```bash
pip install -r requirements.txt
SEED_DEMO=1 TRACE_DB_PATH=./data/traceability.db \
  uvicorn app.main:app --reload --port 8000
pytest -q        # 运行测试（228 个用例）
```

本地服务监听 **http://localhost:8000**（与 Docker Compose 映射端口一致）：
接口根 `http://localhost:8000/`、Swagger `http://localhost:8000/docs`、
健康检查 `http://localhost:8000/health`。

> 所有时间均按 **UTC** 解释，建议使用 `YYYY-MM-DDTHH:MM:SSZ` 格式；
> 时间窗覆盖查询（`/api/coverage`）的 `start`/`end` 也接受小数秒
> （如 `2026-06-01T00:00:00.500Z`）与时区偏移（如 `...+02:00`），
> 时区换算后的微秒精度会原样保留在响应中，详见 [4.1 节](#41仪器时间窗覆盖查询get-apicoverageinstrument)。

---

## 三、数据模型

```
instruments（仪器）
    code (PK)、name、manufacturer、model、serial_no、is_national_standard
        │ 1
        │ n
certificates（校准证书）
    id (PK)、instrument_code(FK)、cert_number
    issued_at（校准/签发时刻）、valid_from、valid_until（有效区间，闭区间）
    uncertainty、unit（扩展测量不确定度及单位）
    digest_alg、signature_digest、signed_payload（签名摘要与被签载荷）
        │ 1                    │ 1
        │ n                    │ n
trace_edges（父子溯源边）
    child_cert_id(FK) → parent_cert_id(FK)
    valid_from、valid_until（边本身的有效区间，缺省继承子证书区间）、note

certificate_revisions（证书修订历史；与证书更新同一事务维护）
    certificate_id(FK→certificates)、version（连续版本号，从 1 起）
    instrument_code、cert_number、issued_at、valid_from、valid_until、
    uncertainty、unit、digest_alg、signature_digest、signed_payload（完整快照）
    recorded_at（版本记录时刻）
    PRIMARY KEY (certificate_id, version)
```

- 国家基准：`instruments.is_national_standard = true` 的仪器所持证书是溯源根节点，
  根节点不再向上溯源。
- 同一子证书可挂多条父边，因此同一时点可能枚举出多条候选链。
- 证书修订历史：首次按 ID 保存证书记录 v1；功能上线前已存在的存量证书首次更新前
  先补录旧值为 v1；仅业务字段实际变化才追加连续版本，同内容重交不增版；
  批量预检（dry-run）绝不留下版本。

```
calibration_bookings（校准工位预约；全实验室单个工位，半开区间占用）
    id (PK)、instrument_code(FK→instruments)
    start_at、end_at（半开占用 [start_at, end_at)，UTC）
    duration_seconds
    certificate_id、certificate_valid_until（确认时起点证书及其到期时刻快照）
    note、created_at

calibration_waitlist（校准工位候补；挂在某条已确认预约上，按登记先后排队）
    seq (PK, AUTOINCREMENT；登记先后即队列顺序)、id（候补单号 WL-...，UNIQUE）
    booking_id（目标预约 id；不设外键——目标预约取消删除后候补记录仍保留追溯）
    instrument_code（候补仪器）
    start_at、end_at、duration_seconds（登记时目标预约的时段快照）
    note、status（waiting 候补中 | promoted 已补位 | failed 重验不合格 | expired 已失效）
    status_reason（进入 failed/expired 的中文原因）
    promoted_booking_id（补位成功生成的新预约 id）
    created_at（登记时刻）、processed_at（进入终态时刻）

station_maintenance_windows（校准工位维护封锁；UTC 半开区间，与预约共同占用单工位）
    id (PK，维护单号 MW-...)
    start_at、end_at（半开封锁 [start_at, end_at)，UTC；首尾相接允许）
    reason（维护/封锁原因）
    status（active 生效中 | revoked 已撤销——软删除，行保留可追溯）
    created_at（登记时刻）、revoked_at（撤销时刻）
```

- 工位占用**不分仪器**：所有预约共用同一张表；冲突按半开区间
  `b.start < end AND b.end > start` 判定，相接但不重叠不冲突。
- 预约确认时在同一写事务内重查溯源链与占用；查询/确认要求该仪器在
  起点存在唯一且校验通过的溯源链，且 `end_at <= 起点证书 valid_until`。
- 候补沿用目标预约登记时的起止时段排队；**登记时不校验**溯源链与占用，
  而在目标预约取消的补位事务内按队列顺序逐条重验；目标预约改期时
  其未补位（waiting）候补标记失效，避免旧时段队列悬空。
- **维护封锁**：维护窗口与预约一样按半开区间占用单工位。登记在同一
  写事务内同时检查既有预约与其他活跃维护窗口，重叠返回 409 并附冲突 ID，
  失败不写入；空档查询把维护窗口与预约一并视作占用；确认预约、批量改期
  在各自写事务内重查维护封锁（改期整体回滚）；取消补位同样避开维护窗口，
  被跳过的候补记录 `STATION_UNDER_MAINTENANCE` 原因。撤销为软删除，
  撤销后窗口不再封锁工位但记录保留。

---

## 四、API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/submissions` | 批量提交（upsert）：仪器 + 证书 + 溯源边 |
| POST | `/api/submissions/preflight` | **批量提交预检（dry-run，独立事务回滚，绝不写库）** |
| POST | `/api/instruments` | 单个仪器 upsert |
| GET | `/api/instruments` · `/api/instruments/{code}` | 仪器列表/详情 |
| POST | `/api/certificates` | 单个证书 upsert（未给摘要时服务端重算） |
| GET | `/api/certificates?instrument_code=` | 证书列表 |
| **GET** | **`/api/certificates/{id}/revisions`** | **证书修订历史：连续版本快照列表（首存 v1，业务字段变化才增版）** |
| **GET** | **`/api/certificates/{id}/revisions/diff?from_version=&to_version=`** | **指定两版的字段差异（缺失版本 404）** |
| POST | `/api/edges` | 溯源边 upsert（禁止自环、校验区间） |
| GET | `/api/edges` | 溯源边列表 |
| **GET** | **`/api/trace/{instrument}?at=`** | **链路查询：重建唯一证书链** |
| **GET** | **`/api/reports/{instrument}?at=`** | **校验报告（冲突明细）** |
| **GET** | **`/api/coverage/{instrument}?start=&end=`** | **仪器时间窗覆盖查询：按有效期边界输出状态区段** |
| **GET** | **`/api/calibration-slots?instrument=&start=&end=&duration_seconds=`** | **校准工位空档查询：单工位半开占用，候选起点重验唯一链与证书截止、沿有效期边界寻找最早可确认时段** |
| **POST** | **`/api/calibration-bookings`** | **确认校准工位预约（同一写事务内重查占用与证书截止，冲突 409）** |
| **POST** | **`/api/calibration-bookings/reschedule`** | **批量改期 2～10 条已有预约（同一写事务核验，冲突 409 整体回滚）** |
| **GET** | **`/api/calibration-bookings?instrument=`** | **预约列表（可按仪器筛选）** |
| **GET** | **`/api/calibration-bookings/inspection?start=&end=`** | **已确认预约有效性巡检：UTC 半开窗筛选，逐条在开始时刻只读复核唯一有效链与证书快照（valid/changed/invalid）** |
| **POST** | **`/api/calibration-bookings/{booking_id}/waitlist`** | **登记候补：沿用该预约起止时段，按登记先后排队（预约不存在 404）** |
| **GET** | **`/api/calibration-waitlist?booking_id=&instrument=&status=`** | **候补状态查询（队列名次、终态原因）** |
| **DELETE** | **`/api/calibration-bookings/{booking_id}`** | **取消预约：同一写事务内释放占用并按候补队列补位（避开维护封锁；重复取消 404，不重复补位）** |
| **POST** | **`/api/calibration-maintenance-windows`** | **登记工位维护封锁窗口（UTC 半开区间+原因；同一写事务内对预约与维护窗口查重，重叠 409 附冲突 ID，首尾相接允许）** |
| **GET** | **`/api/calibration-maintenance-windows?status=&start=&end=`** | **维护窗口列表（可按状态/时间窗筛选；active 与 revoked 均可查）** |
| **DELETE** | **`/api/calibration-maintenance-windows/{window_id}`** | **撤销维护封锁（软删除；撤销后不再封锁工位，重复撤销 404）** |
| **GET** | **`/api/export/{instrument}?at=`** | **JSON 导出（attachment 下载）** |
| **POST** | **`/api/assessments/certificate-deactivation`** | **证书停用影响评估（dry-run，不写库）** |
| **POST** | **`/api/assessments/common-traceability`** | **批次共同溯源核查：多仪器唯一有效链求最近共同证书（只读，不写库）** |
| **POST** | **`/api/assessments/measurement-compatibility`** | **测量相容性评估：多仪器读数两两 En 数判定与极大相容分组（只读，不写库）** |
| POST | `/api/admin/reset` | 清空全部数据（演示/测试用） |

### 1）批量提交

```bash
curl -X POST http://localhost:8000/api/submissions \
  -H 'Content-Type: application/json' -d '{
  "instruments": [
    {"code": "G",  "name": "国家长度基准", "is_national_standard": true},
    {"code": "R",  "name": "一等标准量块"},
    {"code": "W",  "name": "数显卡尺"}
  ],
  "certificates": [
    {"id": "CG", "instrument_code": "G", "cert_number": "NIM-001",
     "issued_at": "2024-01-02T00:00:00Z",
     "valid_from": "2024-01-01T00:00:00Z", "valid_until": "2029-01-01T00:00:00Z",
     "uncertainty": 0.000001, "unit": "mm"},
    {"id": "CR", "instrument_code": "R", "cert_number": "STD-016",
     "issued_at": "2025-06-05T00:00:00Z",
     "valid_from": "2025-06-01T00:00:00Z", "valid_until": "2026-12-31T00:00:00Z",
     "uncertainty": 0.00005, "unit": "mm"},
    {"id": "CW", "instrument_code": "W", "cert_number": "CAL-101",
     "issued_at": "2026-01-10T00:00:00Z",
     "valid_from": "2026-01-01T00:00:00Z", "valid_until": "2027-01-01T00:00:00Z",
     "uncertainty": 0.002, "unit": "mm"}
  ],
  "edges": [
    {"child_cert_id": "CW", "parent_cert_id": "CR", "note": "量块传递"},
    {"child_cert_id": "CR", "parent_cert_id": "CG", "note": "基准送检"}
  ]
}'
```

`signature_digest` 与 `signed_payload` 均可留空：服务端按固定字段集生成
规范化 JSON（键排序、无空白、UTF-8）并计算 SHA-256 摘要，日后逐字节可复核。

### 1.1）批量提交预检（POST /api/submissions/preflight）

正式提交前先做整批校验：请求体与 `/api/submissions` **完全相同**，另加
`strict_digest` 开关。服务在**独立事务**（独立连接，进入即 `BEGIN`、退出一律
`ROLLBACK`）中按提交顺序（仪器 → 证书 → 边）高保真模拟整批 upsert，依次执行：

1. **引用校验** — 证书的 `instrument_code`、边的 `child_cert_id`/`parent_cert_id`
   必须已存在于库中或同批先提交（同批引用可正常解析）；
2. **区间校验** — 证书 `valid_from ≤ valid_until`；溯源边自身区间不倒置，
   且必须与子证书有效区间相交（边区间缺省继承子证书区间）；
3. **自环校验** — 溯源边 `child_cert_id` 不得等于 `parent_cert_id`；
4. **摘要校验** — 仅当**显式**提交 `signature_digest` 时才与服务端按规范化
   载荷的重算值比对（留空则由服务端重算，不产生问题）：
   `strict_digest=true` 记为**错误**（`errors`，`ok=false`），
   `strict_digest=false`（默认）记为**警告**（`warnings`，不阻断）。

响应恒为 **200**（载荷本身的 JSON/字段格式错误仍返回 422），包含稳定排序的
错误/警告列表：`path` 为 JSON Pointer 风格字段路径（如
`certificates/0/instrument_code`、`edges/2/parent_cert_id`，排序时下标按
整数比较，故 `.../2` 排在 `.../10` 之前），并带机器可读 `code`、中文 `message`
与结构化 `detail`；`would_upsert` 给出若正式提交将执行 upsert 的条目数量
（存在阻断性错误的条目不写入、也不计数；摘要不匹配从不阻断）。
响应恒带 `"dry_run": true`——**预检绝不写入数据库**，可安全重复调用。

```bash
curl -X POST http://localhost:8000/api/submissions/preflight \
  -H 'Content-Type: application/json' -d '{
  "strict_digest": true,
  "instruments": [
    {"code": "G", "name": "国家长度基准", "is_national_standard": true},
    {"code": "W", "name": "数显卡尺"}
  ],
  "certificates": [
    {"id": "CG", "instrument_code": "G", "cert_number": "NIM-001",
     "issued_at": "2024-01-02T00:00:00Z",
     "valid_from": "2024-01-01T00:00:00Z", "valid_until": "2029-01-01T00:00:00Z",
     "uncertainty": 0.000001, "unit": "mm"},
    {"id": "CW", "instrument_code": "W", "cert_number": "CAL-101",
     "issued_at": "2026-01-10T00:00:00Z",
     "valid_from": "2026-01-01T00:00:00Z", "valid_until": "2027-01-01T00:00:00Z",
     "uncertainty": 0.002, "unit": "mm",
     "signature_digest": "0000000000000000000000000000000000000000000000000000000000000000"}
  ],
  "edges": [
    {"child_cert_id": "CW", "parent_cert_id": "CW"}
  ]
}'
```

```jsonc
{
  "strict_digest": true,
  "dry_run": true,                    // 恒为 true：预检不写库
  "ok": false,                        // errors 非空即为 false（警告不影响）
  "error_count": 2,
  "warning_count": 0,
  "errors": [
    {
      "path": "certificates/1/signature_digest",
      "code": "SIGNATURE_DIGEST_MISMATCH",
      "message": "证书 CW（CAL-101）签名摘要校验失败：提交 0000…，重算 9f86…",
      "detail": {
        "certificate_id": "CW", "digest_alg": "sha256",
        "submitted": "0000…", "recomputed": "9f86…"
      }
    },
    {
      "path": "edges/0/parent_cert_id",
      "code": "EDGE_SELF_LOOP",
      "message": "溯源边不能指向证书自身（自环）",
      "detail": {"child_cert_id": "CW", "parent_cert_id": "CW"}
    }
  ],
  "warnings": [],                     // strict_digest=false 时摘要不匹配出现在这里
  "would_upsert": {
    "instruments": 2, "certificates": 2, "edges": 0, "total": 4
  }
}
```

预检错误码：

| code | path 指向 | 含义 |
| --- | --- | --- |
| `INSTRUMENT_NOT_FOUND` | `certificates/{i}/instrument_code` | 证书引用的仪器在库中/同批均不存在 |
| `CERTIFICATE_INTERVAL_INVALID` | `certificates/{i}/valid_from` | 证书 `valid_from` 晚于 `valid_until` |
| `SIGNATURE_DIGEST_MISMATCH` | `certificates/{i}/signature_digest` | 显式摘要与重算值不符（strict 时 ERROR，否则 WARN） |
| `CHILD_CERT_NOT_FOUND` | `edges/{i}/child_cert_id` | 子证书不存在 |
| `PARENT_CERT_NOT_FOUND` | `edges/{i}/parent_cert_id` | 父证书不存在 |
| `EDGE_SELF_LOOP` | `edges/{i}/parent_cert_id` | 溯源边指向证书自身 |
| `EDGE_INTERVAL_INVALID` | `edges/{i}/valid_from` | 边自身 `valid_from` 晚于 `valid_until` |
| `EDGE_INTERVAL_DISJOINT` | `edges/{i}/valid_from` | 边有效区间与子证书区间完全不相交 |

> 预检通过（`ok=true`）后再以相同载荷调用 `POST /api/submissions` 即可正式写入；
> 预检与正式提交之间若有其他写入，正式提交仍可能因数据变化而返回 422。

### 2）链路查询 / 3）校验报告 / 4）JSON 导出

```bash
curl "http://localhost:8000/api/trace/W?at=2026-06-15T00:00:00Z"
curl "http://localhost:8000/api/reports/W?at=2026-06-15T00:00:00Z"
curl -OJ "http://localhost:8000/api/export/W?at=2026-06-15T00:00:00Z"
```

#### 4.1）仪器时间窗覆盖查询（GET /api/coverage/{instrument}）

需要回答“这台仪器在**一段时间内**分别处于什么溯源状态、冲突在何时出现/消失”时，
传入仪器编码与 UTC 闭时间窗 `start`、`end`：

```bash
curl -G "http://localhost:8000/api/coverage/INST-T1" \
  --data-urlencode "start=2025-01-01T00:00:00Z" \
  --data-urlencode "end=2025-12-31T23:59:59Z"
```

服务端按以下步骤计算：

1. 收集与该仪器相关的证书与父子溯源边（从该仪器证书出发沿“子→父”闭包可达，
   含国家基准根证书）落在窗内的全部有效期边界 `valid_from / valid_until`；
2. 窗口端点与这些边界合并去重、按**真实时间顺序**排列为评估网格 `grid_points`；
3. 在**每个网格点**（证书/边的整秒边界按闭区间端点评估）与每对相邻网格点之间的
   **开区间内部**各运行一次现有时点校验报告（`/api/reports` 的同一引擎）；
4. 按时间顺序输出状态区段 `segments`：**仅当** `resolution`（状态）、`valid`
   （有效性）与冲突码多重集完全相同，且两端**严格连续**（端点时间相等且一含一不含）
   时才把相邻段合并。

每个区段显式标明 `start_inclusive` / `end_inclusive`（端点是否包含）。
因此当相邻证书有效期首尾相接（闭区间，如前证 `valid_until == 后证 valid_from`）时，
**交接点上的瞬时冲突**（典型：该瞬时两张证书同时有效 → `MULTIPLE_ACTIVE_CERTIFICATES`
+ `MULTIPLE_VALID_CHAINS`）会保留为一个零长度、两端都含的独立点区段，
不会被两侧状态相同的开区间吞掉：

```jsonc
{
  "instrument": "W",
  "window_start": "2026-01-01T00:00:00Z",
  "window_end": "2027-01-01T00:00:00Z",
  "boundary_count": 1,
  "grid_points": ["2026-01-01T00:00:00Z",
                  "2026-06-01T00:00:00Z",   // 两张证书闭区间交接点
                  "2027-01-01T00:00:00Z"],
  "segment_count": 3,
  "segments": [
    {
      "start": "2026-01-01T00:00:00Z", "end": "2026-06-01T00:00:00Z",
      "start_inclusive": true,  "end_inclusive": false,   // 左闭右开
      "resolution": "unique", "valid": true,
      "conflict_codes": [], "error_count": 0, "warning_count": 0,
      "sample_at": "2026-01-01T00:00:00Z", "starts": ["CW-1"],
      "conflicts": [ /* 与 /api/reports 同构的完整冲突明细 */ ]
    },
    {
      "start": "2026-06-01T00:00:00Z", "end": "2026-06-01T00:00:00Z",
      "start_inclusive": true,  "end_inclusive": true,    // 瞬时点区段：两端都含
      "resolution": "ambiguous", "valid": false,
      "conflict_codes": ["MULTIPLE_ACTIVE_CERTIFICATES",
                         "MULTIPLE_VALID_CHAINS"],
      "sample_at": "2026-06-01T00:00:00Z", "starts": ["CW-1", "CW-2"]
    },
    {
      "start": "2026-06-01T00:00:00Z", "end": "2027-01-01T00:00:00Z",
      "start_inclusive": false, "end_inclusive": true,    // 左开右闭
      "resolution": "unique", "valid": true,
      "conflict_codes": [],
      "sample_at": "2026-09-16T00:00:00Z", "starts": ["CW-2"]
    }
  ],
  "statistics": {"boundaries_in_window": 1, "grid_points": 3,
                 "point_samples": 3, "interval_samples": 2,
                 "reports_evaluated": 5}
}
```

**小数秒查询。** 证书/边的有效期边界恒为整秒（写入时规范化为
`YYYY-MM-DDTHH:MM:SSZ`），但查询窗端点允许带小数秒
（如 `.500Z`，也接受带时区偏移的 `...+02:00`）。服务端先做时区换算并
**保留微秒精度**，再以真实时间顺序建立网格——整秒边界本身仍属于闭区间，
而该秒之后的任意小数秒位于边界之后。因此：

- 查询交接点之后的小数秒（如上例 `2026-06-01T00:00:00.500Z`）**不会**
  被当成两证闭区间交接点，只有后证 `CW-2` 有效，不再误报双证书冲突；
- 查询截止点之后的同秒小数秒（如证书 `valid_until=2027-01-01T00:00:00Z`
  而查询 `2027-01-01T00:00:00.500Z`）**不**落在闭区间内，正确报失效；
- **单点窗口只覆盖该时刻**：`start == end` 时网格仅此一点、只评估一次，
  区段端点与 `sample_at` 均为该精确时刻；小数秒窗口的 `window_start` /
  `window_end`、`grid_points`、区段端点与 `sample_at` 一律保留 6 位微秒。

```bash
# 交接整秒之后的半秒单点：只有 CW-2 有效（不产生 MULTIPLE_ACTIVE_CERTIFICATES）
curl -G "http://localhost:8000/api/coverage/W" \
  --data-urlencode "start=2026-06-01T00:00:00.500Z" \
  --data-urlencode "end=2026-06-01T00:00:00.500Z"
```

```jsonc
{
  "instrument": "W",
  "window_start": "2026-06-01T00:00:00.500000Z",
  "window_end": "2026-06-01T00:00:00.500000Z",
  "boundary_count": 0,
  "grid_points": ["2026-06-01T00:00:00.500000Z"],
  "segment_count": 1,
  "segments": [
    {
      "start": "2026-06-01T00:00:00.500000Z",
      "end": "2026-06-01T00:00:00.500000Z",
      "start_inclusive": true, "end_inclusive": true,   // 单点只覆盖该时刻
      "resolution": "unique", "valid": true,
      "conflict_codes": [],
      "sample_at": "2026-06-01T00:00:00.500000Z",      // 精确微秒原样保留
      "starts": ["CW-2"]
    }
  ],
  "statistics": {"boundaries_in_window": 0, "grid_points": 1,
                 "point_samples": 1, "interval_samples": 0,
                 "reports_evaluated": 1}
}
```

作为对照，**整秒**交接点 `2026-06-01T00:00:00Z` 仍是闭区间交接，瞬时双证
冲突照常保留为独立点区段；窗口横跨整秒截止点时（如
`start=2026-12-31T23:59:59.500Z&end=2027-01-01T00:00:00.500Z`），截止点
`...00:00:00Z` 本身仍有效（并入左侧、段尾含），其后的 `.500000` 区段断裂
（左开、段尾含窗口终点）。

区段字段说明：

| 字段 | 含义 |
| --- | --- |
| `start` / `end` | 区段端点（UTC）；点区段二者相等 |
| `start_inclusive` / `end_inclusive` | 是否包含对应端点；相邻区段在公共端点上恰好一含一不含 |
| `resolution` | `unique` / `ambiguous` / `broken` / `undetermined`，含义与时点报告一致（`undetermined`=枚举截断、结果未定） |
| `valid` | 代表采样点上唯一成链且无 ERROR |
| `conflict_codes` | 该区段全部冲突码（去重排序）；合并判定使用冲突码**多重集**（同码出现次数也需一致） |
| `error_count` / `warning_count` | 代表采样点的 ERROR/WARN 数量 |
| `sample_at` | 代表评估点：点区段为网格点本身；开区间区段为两端点之间的内部采样时刻（跨度 ≥2 秒取整秒中点，相隔 1 秒取后半秒，更短取真实中点）；同态相邻原子合并后保留最左原子的代表点 |
| `starts` / `conflicts` | 代表采样点的有效起点证书与完整可解释冲突明细（结构同 `/api/reports`） |

特性与约束：

- 区段按时间升序、互不重叠且完整覆盖闭窗口 `[start, end]`；
  窗口端点恒被包含（首段 `start_inclusive=true`、末段 `end_inclusive=true`）。
- 状态在边界**两侧开区间相同、但边界点本身不同**（瞬时冲突）时保留点区段；
  状态在边界点与一侧也相同时，该点并入对应区段（端点包含性合并后仍正确）。
- `start` 晚于 `end` 返回 **422**（中文原因，含精确微秒时刻）；缺少 `start`/`end` 返回 **422**；
  仪器不存在返回 **404**。时间入参按 UTC 解释，建议统一用 `...Z` 格式，
  可带小数秒（如 `.500Z`）或时区偏移（换算为 UTC 后保留微秒）；
  有效期边界恒为整秒、按闭区间比较，开区间内部采样不会落回端点，小数秒点保持真实时间顺序。
- 内置演示数据中，热电偶 `INST-T1` 在 2025 年夏季存在证书断档，
  可直接用本节开头的 curl 观察“有效 → 断档（`INTERVAL_GAP` /
  `NO_ACTIVE_CERTIFICATE`）→ 恢复”的分段效果。

#### 4.2）校准工位预约（GET /api/calibration-slots + POST /api/calibration-bookings）

实验室只有**单个校准工位**（所有仪器共用一份占用），占用一律按
**半开区间 `[start_at, end_at)`** 计算：相接但不重叠的两段（如
`[09:00,10:00)` 与 `[10:00,11:00)`）互不冲突。

**① 查询最早可用时段（只读）。** 输入仪器、UTC 起止时间与所需时长（秒）：

```bash
curl -G "http://localhost:8000/api/calibration-slots" \
  --data-urlencode "instrument=INST-W1" \
  --data-urlencode "start=2026-06-20T00:00:00Z" \
  --data-urlencode "end=2026-06-21T00:00:00Z" \
  --data-urlencode "duration_seconds=3600"
```

服务端：

1. 在**查询起点**判定该仪器是否存在**唯一且校验通过**的溯源链——这是
   入口门禁（复用 `/api/reports` 同一引擎：`resolution=unique` 且零 ERROR
   才放行，多候选链绝不任取一条；枚举截断导致**结果未定**（`undetermined`）
   时同样拒绝）。查询起点链不可用时 `slot=null`，
   `reason_code=TRACEABILITY_CHAIN_UNAVAILABLE`，
   `chain_reason_code` 给出细分原因，完整证据见响应内的 `trace_report`；
2. 用与**整个请求查询窗**半开相交的既有预约（`busy`，单工位、不分仪器）
   与**生效中维护封锁窗口**（`maintenance_windows`，两者一并视作占用）
   贪心求物理最早空档；**搜索窗不再按查询起点证书的截止裁剪**；
3. **对每个候选起点重放与预约确认相同的门禁**：工位占用把建议起点推后后，
   **绝不沿用查询起点的旧溯源结论**——在候选起点重新要求唯一有效链，且
   候选结束（半开，`end == valid_until` 允许）不晚于**该候选起点**的起点
   证书 `valid_until`。因此占用推后到换新证书生效后的候选，只要新证书在
   新起点构成唯一有效链、截止充足，仍可给出建议时段；
4. 候选不可确认时，沿**证书与溯源边的有效期边界**继续寻找最早可确认时段：
   边界是闭区间端点，即使落在占用段起点、物理空档在忙段结束处，边界点本身
   也会单独重验；**闭区间交接点**（前证 `valid_until == 后证 valid_from`
   的整秒点，两证同时有效、常为起点不唯一）不合格时，从其后一个整秒
   （边界 +1s）继续——闭区间交接点必须重验，不得由两侧开区间状态推断；
5. 响应中的溯源说明（`traceability_at` / `certificate_id` /
   `certificate_valid_until` / `trace_report`）均对应**实际建议起点**
   （无建议时为最后一次重验的候选起点；查询起点链即不可用时为查询起点）。
   全窗无合格时段时 `slot=null` 并给出具体原因码：

   - `NO_FREE_SLOT_IN_WINDOW`：入口链成立，但窗内连可容纳时长的物理空档都没有；
   - `CERTIFICATE_DEADLINE_TOO_EARLY`：全部物理候选均无法在其**当时**起点
     证书到期前结束，且越过截止后无换新链（仅无证书空档）；
   - `NO_CONFIRMABLE_SLOT_IN_WINDOW`：沿证书/溯源边有效期边界（含闭区间
     交接点）逐点重验后，仍有结构性不可确认候选（起点不唯一/多候选/断裂/
     校验失败/结果未定），`chain_reason_code` 给出最后候选的细分原因。

```jsonc
{
  "instrument": "INST-W1",
  "window_start": "2026-06-20T00:00:00Z",
  "window_end": "2026-06-21T00:00:00Z",
  "duration_seconds": 3600,
  "traceability_at": "2026-06-20T00:00:00Z",   // 溯源判定时点：实际建议起点
  "traceability_available": true,
  "certificate_id": "CW1-1",                   // 实际建议起点的唯一有效链起点证书
  "certificate_valid_until": "2027-01-01T00:00:00Z",
  "search_window_start": "2026-06-20T00:00:00Z",
  "search_window_end": "2026-06-21T00:00:00Z", // 恒为请求窗终点（不再按证书截止裁剪）
  "slot": {                                     // 最早可确认时段（半开）
    "start": "2026-06-20T00:00:00Z",
    "end": "2026-06-20T01:00:00Z",
    "ends_by_certificate_valid_until": true
  },
  "busy": [ /* 与整个请求窗（半开）相交的既有预约：booking_id/instrument_code/start_at/end_at */ ],
  "maintenance_windows": [ /* 与请求窗半开相交的生效维护封锁，同样占用工位 */ ],
  "trace_report": { /* 实际建议起点的完整溯源报告，结构同 /api/reports */ }
}
```

> 若 09:00~10:00 的占用把建议起点推后到 10:00，而该仪器旧证恰在 10:00
> 到期、新证 10:00 起换发（两证闭区间首尾相接），则：10:00 交接点两证
> 同时有效（起点不唯一）会被单独重验并拒绝，从 10:00:01 起以**新证**
> 重验唯一链与截止，最早可确认时段从 10:00:01 开始，响应的
> `certificate_id` / `certificate_valid_until` / `trace_report` 全部对应
> 10:00:01 的新证结论——不会沿用 09:00 查询起点的旧证结果返回一个确认时
> 必被 409 拒绝的时段。

> 演示库已预置一条 `INST-W1` 在 `2026-06-20T09:00:00Z ~ 10:00:00Z` 的占用，
> 用 `start=...T09:00:00Z` 查询可看到空档被它推后到 `10:00:00Z`（半开相接）。

**② 确认预约（写事务，冲突 409）。** 传入仪器、预约起点与时长（结束由
服务端按 `start + duration_seconds` 计算）。服务端在**同一写事务**
（`BEGIN IMMEDIATE`）内**重新检查**（不沿用 GET 的旧结果）：仪器存在 →
预约起点唯一有效链仍成立 → 结束不晚于起点证书 `valid_until` → 单工位
在该半开区间无占用；任一条件不成立即返回 **409** 并整体回滚（绝不写入），
全部成立才在同一事务内原子写入 SQLite：

```bash
curl -X POST http://localhost:8000/api/calibration-bookings \
  -H 'Content-Type: application/json' -d '{
    "instrument_code": "INST-W1",
    "start": "2026-06-20T11:00:00Z",
    "duration_seconds": 1800,
    "note": "例行校准"
  }'
# 201
# {"id":"BK-20260924-xxxxxxxxxxxx", "instrument_code":"INST-W1",
#  "start_at":"2026-06-20T11:00:00Z", "end_at":"2026-06-20T11:30:00Z",
#  "duration_seconds":1800, "certificate_id":"CW1-1",
#  "certificate_valid_until":"2027-01-01T00:00:00Z", "note":"例行校准", ...}
```

409 响应体带机器可读 `code`、中文 `message` 与结构化 `detail`：

| `detail.code` | 触发条件 |
| --- | --- |
| `INSTRUMENT_NOT_FOUND` | 仪器在确认时已不存在 |
| `TRACEABILITY_CHAIN_UNAVAILABLE` | 预约起点唯一有效链不成立（`detail.reason_code` 细分，附 `trace_report`；枚举截断结果未定时细分码为 `RESULT_UNDETERMINED`） |
| `BOOKING_ENDS_AFTER_CERT_EXPIRY` | 结束晚于起点证书有效期截止（附两者时刻） |
| `STATION_OCCUPIED` | 单工位该半开区间已有预约（`detail.conflicting_bookings` 给出全部冲突预约） |
| `STATION_UNDER_MAINTENANCE` | 该半开区间与生效中维护封锁窗口重叠（`detail.conflicting_maintenance_windows` 给出全部冲突窗口） |

> 半开语义下，新预约起点恰等于既有预约结束点（如 `10:00:00Z` 紧接
> `[09:00,10:00)`）不冲突、可以确认；结束时刻**恰等于**证书
> `valid_until` 同样允许（到期前结束）。写事务以 `BEGIN IMMEDIATE`
> 立即取写锁，并发确认同一时段时恰有一个成功，其余在事务内重查到
> 已提交占用后返回 409，不会产生重叠预约。

**③ 查询预约。** 按仪器查询（留空 `instrument` 则返回全部，按起点升序）：

```bash
curl "http://localhost:8000/api/calibration-bookings?instrument=INST-W1"
curl "http://localhost:8000/api/calibration-bookings"
```

入参/状态码：时间均按 UTC（建议 `...Z`）；`duration_seconds` 须为正整数
（否则 422）；`start` 不早于 `end`、时长不超过窗长，否则 422；
仪器不存在 GET 返回 404，POST 在写事务内确认时返回 409。

**④ 批量改期（POST /api/calibration-bookings/reschedule，写事务，冲突 409）。**
一次提交 **2～10 条**已有预约的改期项（`booking_id` + 新起点 `start` +
**可选**新时长 `duration_seconds`，缺省沿用原时长）。**保留原仪器与备注**
（创建时间同样不变）。服务端在**同一写事务**（`BEGIN IMMEDIATE`）内：

1. 核对每条预约均存在且本批预约 **ID 不重复**；
2. 逐条按**新起点重新验证**该仪器唯一有效溯源链（重查，不沿用旧证书
   快照），新结束（`start + duration_seconds`，半开）不晚于新起点
   证书 `valid_until`；
3. 先把本批预约的**旧占用整体排除**，再按半开区间检查新时段**彼此**
   及**与其他预约**的冲突——因此允许本批预约**交换时段**（如 A、B
   互换原占用），也允许**首尾相接**（新起点恰等于另一段结束点）。

任一条件不成立即返回 **409**（`detail.booking_id` 指明失败预约）并
**整体回滚**（整批保持改期前状态，绝不部分更新）；全部通过才在同一
事务内更新各预约的时段、时长与证书快照，响应按请求顺序返回。

```bash
curl -X POST http://localhost:8000/api/calibration-bookings/reschedule \
  -H 'Content-Type: application/json' -d '{
    "items": [
      {"booking_id": "BK-DEMO-20260620-W1", "start": "2026-06-21T09:00:00Z"},
      {"booking_id": "BK-20260620-aaaaaaaaaaaa",
       "start": "2026-06-21T10:00:00Z", "duration_seconds": 5400}
    ]
  }'
# 200
# [ {"id":"BK-DEMO-20260620-W1", "instrument_code":"INST-W1",
#    "start_at":"2026-06-21T09:00:00Z", "end_at":"2026-06-21T10:00:00Z",
#    "duration_seconds":3600, "certificate_id":"CW1-1",
#    "certificate_valid_until":"2027-01-01T00:00:00Z", "note":"演示数据：数显卡尺例行校准", ...},
#   {"id":"BK-20260620-aaaaaaaaaaaa", ..., "duration_seconds":5400,
#    "end_at":"2026-06-21T11:30:00Z", ...} ]
```

改期特有的 409 原因码（其余复用确认预约的四条）：

| `detail.code` | 触发条件 |
| --- | --- |
| `BOOKING_NOT_FOUND` | 某条预约 ID 在库中不存在（`detail.booking_id`） |
| `DUPLICATE_BOOKING_ID` | 请求中预约 ID 重复（`detail.booking_id`） |
| `INSTRUMENT_NOT_FOUND` | 预约所属仪器已不存在（附 `booking_id`） |
| `TRACEABILITY_CHAIN_UNAVAILABLE` | 新起点唯一有效链不成立（附 `booking_id` 与 `trace_report`） |
| `BOOKING_ENDS_AFTER_CERT_EXPIRY` | 新结束晚于新起点证书截止（附 `booking_id` 与两者时刻） |
| `STATION_OCCUPIED` | 新时段与其他预约冲突（`conflicting_bookings`）或本批新时段彼此冲突（`conflicting_batch_bookings`），均附 `booking_id` |
| `STATION_UNDER_MAINTENANCE` | 新时段与生效中维护封锁窗口重叠（附 `booking_id` 与 `conflicting_maintenance_windows`），整批回滚 |

> 改期只更新 `start_at / end_at / duration_seconds / certificate_id /
> certificate_valid_until`；**仪器、备注与创建时间不变**。改期项少于
> 2 条或多于 10 条、`duration_seconds` 非正数、ID 为空白字符串时
> 返回 **422**（ID 重复与不存在在写事务内核验，返回 **409**）。
> 时段实际变化的预约，其**未补位候补（waiting）在同一事务内标记失效**
> （`expired`），避免旧时段队列悬空；时段未变的预约队列不受影响，
> 改期整批回滚时候补队列同样不受影响。

**⑤ 登记候补（POST /api/calibration-bookings/{booking_id}/waitlist，201）。**
对指定**已确认预约**登记候补仪器与备注；候补**沿用该预约当前的起止
时段**（快照保存），按登记先后（`seq` 自增）排队，响应
`queue_position` 为当前队列名次。溯源链、证书截止与占用冲突**不在
登记时校验**，而在目标预约取消时的补位事务内逐条重验：

```bash
curl -X POST http://localhost:8000/api/calibration-bookings/BK-DEMO-20260620-W1/waitlist \
  -H 'Content-Type: application/json' -d '{
    "instrument_code": "INST-W2",
    "note": "高度尺候补同一时段"
  }'
# 201
# {"id":"WL-20260925-xxxxxxxxxxxx", "booking_id":"BK-DEMO-20260620-W1",
#  "instrument_code":"INST-W2",
#  "start_at":"2026-06-20T09:00:00Z", "end_at":"2026-06-20T10:00:00Z",
#  "duration_seconds":3600, "status":"waiting", "queue_position":2, ...}
```

目标预约不存在（含已取消）返回 **404**（`BOOKING_NOT_FOUND`）；
候补仪器不存在返回 **409**（`INSTRUMENT_NOT_FOUND`）。

**⑥ 候补状态查询（GET /api/calibration-waitlist）。** 按目标预约 /
仪器 / 状态筛选，按登记先后升序返回；`queue_position` 仅对候补中
（`waiting`）条目给出其在目标预约队列中的 1 基名次，终态条目为
`null`，其 `status_reason` 给出进入终态的中文原因：

```bash
curl "http://localhost:8000/api/calibration-waitlist?booking_id=BK-DEMO-20260620-W1"
curl "http://localhost:8000/api/calibration-waitlist?instrument=INST-W2&status=waiting"
```

状态机：`waiting` 候补中 → `promoted` 已补位 / `failed` 补位重验不
合格被跳过 / `expired` 已失效（目标预约改期，或队列中更早候补已
补位）。`status` 取值非法返回 **422**；按仪器筛选而仪器不存在返回
**404**；目标预约已取消删除后其候补记录仍可查（`booking_id` 筛选
不做存在性校验）。

**⑦ 取消预约与补位（DELETE /api/calibration-bookings/{booking_id}）。**
在**同一写事务**（`BEGIN IMMEDIATE`）内：删除预约行、释放占用，
然后**依登记先后逐条重验**候补中申请——仪器在时段起点的唯一有效
溯源链（重查，不沿用任何旧结果）、时段终点不晚于起点证书
`valid_until`、与**剩余预约**无半开冲突；**跳过不合格申请**（标记
`failed` 并记录原因），只将**首个合格申请**转为已确认预约（沿用
原时段与时长，保存起点证书快照与候补备注），排在中签者之后、未及
评估的候补中条目标记 `expired`：

```bash
curl -X DELETE http://localhost:8000/api/calibration-bookings/BK-DEMO-20260620-W1
# 200
# {
#   "cancelled_booking": {"id":"BK-DEMO-20260620-W1", ...},
#   "waitlist_processed": 1,
#   "promoted": true,
#   "promotion": {
#     "waitlist_id": "WL-DEMO-20260620-W2",
#     "instrument_code": "INST-W2",
#     "queue_position": 1,
#     "booking": {"id":"BK-20260925-...", "instrument_code":"INST-W2",
#                 "start_at":"2026-06-20T09:00:00Z",
#                 "end_at":"2026-06-20T10:00:00Z",
#                 "certificate_id":"CW2-1",
#                 "certificate_valid_until":"2027-02-01T00:00:00Z", ...}
#   },
#   "skipped": [],              // 被跳过的不合格申请及原因码
#   "expired_waitlist_ids": []  // 排在中签者之后而失效的候补
# }
```

无合格候补时 `promoted=false`、`promotion=null`，时段真正释放，
`skipped` 逐条给出不合格原因码（`INSTRUMENT_NOT_FOUND` / 溯源链
细分原因码 / `BOOKING_ENDS_AFTER_CERT_EXPIRY` / `STATION_OCCUPIED` /
`STATION_UNDER_MAINTENANCE`（补位时段撞维护封锁））。
预约不存在或已取消返回 **404**（`BOOKING_NOT_FOUND`）——**重复
取消不会重复补位**；补位在同一 `BEGIN IMMEDIATE` 写事务内串行化，
并发取消同一预约恰有一个成功。

**⑧ 已确认预约有效性巡检（GET /api/calibration-bookings/inspection，只读）。**
证书与溯源边日后可能发生修订（证书改期/续期、溯源关系调整、摘要更正等），
需要批量核查“历史上确认过的预约，按当前数据复核是否仍然成立”。传入 UTC
半开查询窗 `[start, end)`，服务端筛选**开始时刻**落在窗内的已确认预约
（开始恰等于窗起点计入、恰等于窗终点不计入），按**开始时刻升序、预约 ID
次序**稳定排序后逐条只读复核（全程不写库）：

1. 对每条预约在其**开始时刻**重建该仪器的**唯一有效溯源链**（复用与确认/
   改期相同的链路门禁 `unique_chain_gate`，绝不沿用预约保存的旧快照）；
2. 要求链校验通过（唯一成链且零 ERROR）且预约**结束不晚于当前起点证书
   `valid_until`**（半开：结束恰等于到期时刻允许）；
3. 再与预约保存的证书 ID 与到期快照比较：

   - 链失效（仪器不存在 / 无有效证书 / 起点不唯一 / 多候选 / 断裂 /
     校验失败）或**截止不足**（结束晚于当前证书到期）→ `invalid`；
   - 链仍有效且截止充足，但当前**证书 ID 或到期时刻**与快照不同 →
     `changed`（`reason_code=CERTIFICATE_SNAPSHOT_CHANGED`，中文原因
     逐项说明“起点证书由 X 变为 Y”“到期时刻由 X 变为 Y”）；
   - 其余 → `valid`。

每条预约返回状态、机器可读原因码、**当前**起点证书（id + 到期；链不可用时
为 `null`）及确认时的**原快照**（id + 到期）；顶层 `summary` 汇总
`total / valid / changed / invalid` 数量。查询窗起点不早于终点
（`start >= end`）或缺参数返回 **422**（中文原因）。

```bash
curl -G "http://localhost:8000/api/calibration-bookings/inspection" \
  --data-urlencode "start=2026-06-20T00:00:00Z" \
  --data-urlencode "end=2026-06-21T00:00:00Z"
```

```jsonc
{
  "window_start": "2026-06-20T00:00:00Z",
  "window_end": "2026-06-21T00:00:00Z",
  "summary": {"total": 1, "valid": 1, "changed": 0, "invalid": 0},
  "items": [
    {
      "booking_id": "BK-DEMO-20260620-W1",
      "instrument_code": "INST-W1",
      "start_at": "2026-06-20T09:00:00Z",
      "end_at": "2026-06-20T10:00:00Z",
      "status": "valid",
      "reason_code": null,
      "reason": null,
      "current_certificate_id": "CW1-1",              // 复核时重建出的当前起点证书
      "current_certificate_valid_until": "2027-01-01T00:00:00Z",
      "snapshot_certificate_id": "CW1-1",            // 预约确认时保存的原快照
      "snapshot_certificate_valid_until": "2027-01-01T00:00:00Z"
    }
  ]
}
```

`invalid` 的 `reason_code` 与确认/改期接口保持一致并更细：
`INSTRUMENT_NOT_FOUND` / `NO_ACTIVE_CERTIFICATE` /
`MULTIPLE_ACTIVE_CERTIFICATES` / `MULTIPLE_VALID_CHAINS` /
`CHAIN_BROKEN` / `ALL_CHAINS_FAILED_CHECKS` / `CHAIN_VALIDATION_FAILED` /
`RESULT_UNDETERMINED`（候选链枚举截断、结果未定——既不按样本链放行也
不误报断裂）
（以上为链失效）或 `BOOKING_ENDS_AFTER_CERT_EXPIRY`（链有效但截止不足）。
典型场景：将某条预约的起点证书有效期提前但仍覆盖预约结束，巡检结果为
`changed`；若提前到预约结束之前则为 `invalid`
（`BOOKING_ENDS_AFTER_CERT_EXPIRY`）；以新证书无缝替换旧起点证书
（两证首尾相接、不断档）则为 `changed`（当前证书 ID 已切换）。

#### 4.3）校准工位维护封锁（maintenance windows）

工位需要停机维护时，可登记**维护封锁窗口**。维护窗口与预约一样按
**半开区间 `[start_at, end_at)`** 占用全实验室唯一的校准工位，时间
一律 UTC，并必须给出维护原因。

**① 登记封锁（POST，同一写事务内查重，冲突 409）。**

```bash
curl -X POST http://localhost:8000/api/calibration-maintenance-windows \
  -H 'Content-Type: application/json' -d '{
    "start": "2026-06-22T08:00:00Z",
    "end":   "2026-06-22T12:00:00Z",
    "reason": "工位年度保养与导轨精度复测"
  }'
# 201
# {"id":"MW-20260925-xxxxxxxxxxxx",
#  "start_at":"2026-06-22T08:00:00Z", "end_at":"2026-06-22T12:00:00Z",
#  "reason":"工位年度保养与导轨精度复测",
#  "status":"active", "created_at":"2026-09-25T...Z", "revoked_at":null}
```

服务端在**同一写事务**（`BEGIN IMMEDIATE`）内同时检查：

- **既有预约**：与半开区间重叠的全部预约；
- **生效中维护窗口**（`status=active`）：与半开区间重叠的全部窗口。

重叠判据仍是 `b.start < end AND b.end > start`，因此**首尾相接允许**
（如维护 `[10:00,11:00)` 紧接预约 `[09:00,10:00)` 可登记）。任一重叠
返回 **409**（`STATION_UNDER_MAINTENANCE`），`detail` 同时给出全部
`conflicting_bookings`（冲突预约 ID、仪器、时段）与
`conflicting_maintenance_windows`（冲突窗口 ID、时段、原因），事务
回滚、**失败绝不写入**。`start >= end` 或 `reason` 为空白返回 **422**。

**② 封锁生效后的行为。**

- **空档查询**（GET `/api/calibration-slots`）：维护窗口与预约**一并
  视作占用**；预约仍在响应的 `busy` 中列出，维护窗口单独在
  `maintenance_windows` 中列出（含 `maintenance_window_id / start_at /
  end_at / reason`），最早空档贪心同时避开两者。
- **确认预约**（POST `/api/calibration-bookings`）：在同一写事务内
  重查活跃维护窗口，半开重叠返回 **409**
  （`STATION_UNDER_MAINTENANCE`，附全部冲突窗口），不写入；首尾相接
  （预约起点恰为维护结束点，或预约结束恰为维护起点）允许。
- **批量改期**（POST `/api/calibration-bookings/reschedule`）：在同一
  写事务内对每条新时段重查维护封锁（本批旧占用可整体排除，维护封锁
  **不排除**），任一新时段撞窗口返回 **409** 并**整体回滚**。
- **取消补位**（DELETE `/api/calibration-bookings/{id}`）：候补逐条
  重验时同样避开活跃维护窗口；时段撞窗口的候补标记 `failed` 跳过并在
  `skipped[].reason_code` 记录 `STATION_UNDER_MAINTENANCE`（中文原因
  见 `reason`，并同步写入候补 `status_reason`），继续检查队列后续条目，
  只补位首个对预约与维护窗口都不冲突的合格候补。

**③ 列表与撤销。**

```bash
# 全部维护窗口（active/revoked），按起点升序
curl "http://localhost:8000/api/calibration-maintenance-windows"

# 仅生效中窗口；status=revoked 查看已撤销；非法状态 422
curl "http://localhost:8000/api/calibration-maintenance-windows?status=active"

# 与半开查询窗 [start,end) 相交的窗口（也可只给 start 或 end）
curl -G "http://localhost:8000/api/calibration-maintenance-windows" \
  --data-urlencode "start=2026-06-22T00:00:00Z" \
  --data-urlencode "end=2026-06-23T00:00:00Z"

# 撤销封锁（软删除）：撤销后该窗口不再参与任何占用重查，行保留可追溯
curl -X DELETE http://localhost:8000/api/calibration-maintenance-windows/MW-20260925-xxxxxxxxxxxx
# 200 -> {"status":"revoked", "revoked_at":"2026-09-25T...Z", ...}

# 窗口不存在或已撤销：404 MAINTENANCE_WINDOW_NOT_FOUND（重复撤销不重复处理）
```

### 5）证书停用影响评估（dry-run 模拟）

报告关键字段：

```jsonc
{
  "instrument": "W",
  "at": "2026-06-15T00:00:00Z",
  "resolution": "unique",          // unique=唯一成链 | ambiguous=多候选 | broken=断裂 | undetermined=结果未定（枚举截断）
  "valid": true,                   // 唯一成链且无 ERROR 级问题
  "starts": ["CW"],                // 该时点有效的起点证书
  "active_chain": {                // 叶子→根的有序节点与边（undetermined 时恒为 null）
    "nodes": [ {"certificate_id": "CW", ...}, {"...":"CR"}, {"...":"CG（国家基准）"} ],
    "edges": [ {"child_cert_id": "CW", "parent_cert_id": "CR", ...} ],
    "reaches_national_standard": true
  },
  "candidates": [],                // ambiguous 时：>=2 条有效候选链；undetermined 时：样本中到基准的候选链（仅供查看，非主链）
  "broken_chains": [],             // 未到基准的部分链（死路/断档/环）；undetermined 时为样本中的断裂分支
  "conflicts": [ /* 可解释冲突，见下表 */ ],
  "error_count": 0, "warning_count": 0,
  "statistics": {"paths_enumerated": 1, "paths_truncated": false, "max_paths": 500,
                 "root_reaching_chains": 1, ...}
}
```

#### 4.0）枚举上限与“结果未定”（undetermined）

候选链按 DFS 枚举，最多记录 **500 条**（含到基准、死路、断档与成环的各类
终止）。枚举以**显式栈**遍历（非递归），因此单条溯源链即使经超过 Python
默认递归深度（1000）的逐级父证书连到国家基准，也不会触发
`RecursionError`；起点与父边的确定性顺序、各类终止记录与下述截断语义保持
不变。达到上限后**仍有未展开分支**时，已枚举的路径只是样本，服务按下列
规则定论，避免把样本误判为唯一或断裂：

- 已见 **≥2 条**到基准且校验通过的合格链 —— 多候选结论不受截断影响，
  仍判 `ambiguous`（实际候选数可能更多，`TOO_MANY_CANDIDATES` 警告附
  已枚举样本数）；
- 已见合格链**不足 2 条**（0 或 1 条）—— 漏检分支中可能存在第 1/2 条
  合格链，一律判 **`undetermined`（结果未定）**：
  - `active_chain` 恒为 `null`（**不提供有效主链**）；
  - 已枚举样本仍在 `candidates`（样本中到基准的链）与 `broken_chains`
    （样本中未到基准的分支）中给出，`TOO_MANY_CANDIDATES` 给出截断说明；
  - 冲突中追加 ERROR 级 **`RESULT_UNDETERMINED`**，`detail` 含
    `max_paths / paths_enumerated / valid_root_chains_in_sample /
    root_reaching_chains_in_sample / branches_remaining_beyond_limit`，
    且**不再**给出 `CHAIN_BROKEN` / `ALL_CHAINS_FAILED_CHECKS` 等定论性冲突；
  - `statistics.paths_truncated=true`。
- 枚举恰好在 500 条处**穷尽全部分支**（上限外无未展开分支）时
  `paths_truncated=false`，维持 unique / ambiguous / broken 的正常定论。

`undetermined` 报告**不是断裂也不是唯一**，共同溯源核查、测量相容性评估
以及全部预约链路门禁（空档查询、确认、批量改期、取消补位、有效性巡检）
都以专用原因码 **`RESULT_UNDETERMINED`** 拒绝未定结果——不会把漏检的
合格链误报为断裂，也不会任取一条样本链作为预约/比对依据。

### 冲突（conflicts）编码

| code | 级别 | 含义 |
| --- | --- | --- |
| `NO_ACTIVE_CERTIFICATE` | ERROR | 查询时点仪器无有效证书，链无法开始 |
| `MULTIPLE_ACTIVE_CERTIFICATES` | ERROR | 同一时点多张证书有效，起点不唯一 |
| `INTERVAL_GAP` | ERROR | 仪器历史证书有效期的闭区间并集存在断档（附缺口前后证书与边界 `gap_from/gap_to`；嵌套/重叠区间不误报） |
| `DEAD_END` | ERROR | 非基准证书无有效向上溯源边（附节点） |
| `PARENT_CERT_NOT_ACTIVE` | ERROR | 父证书在查询时点失效/未生效，链断裂 |
| `CYCLE_DETECTED` | ERROR | 溯源边构成环（附重复节点、边 id） |
| `SIGNATURE_DIGEST_MISMATCH` | ERROR | 摘要重算与登记值不符（附两者值） |
| `PARENT_INTERVAL_NOT_COVER_CALIBRATION` | ERROR | 父证书区间未覆盖子证书校准时刻 |
| `UNCERTAINTY_UNIT_MISMATCH` | ERROR | 父子不确定度单位不一致，无法比较 |
| `UNCERTAINTY_REGRESSION` | ERROR | 下级不确定度小于上级（沿链收缩） |
| `MULTIPLE_VALID_CHAINS` | ERROR | 多条候选链均到基准且通过校验，链不唯一 |
| `ALL_CHAINS_FAILED_CHECKS` | ERROR | 有到基准的链但全部未通过校验 |
| `CHAIN_BROKEN` | ERROR | 全部分支在到基准前终止 |
| `RESULT_UNDETERMINED` | ERROR | 候选链枚举达到 500 条上限且上限外仍有未展开分支、已见合格链不足 2 条，**结果未定**（保留样本链与截断说明，但不提供有效主链，不判唯一也不判断裂）；共同溯源、测量相容性与预约链路门禁以此码拒绝 |
| `TOO_MANY_CANDIDATES` | WARN | 候选链枚举达到 500 条上限的截断说明（附已枚举样本数；已见 ≥2 条合格链时多候选仍可定论） |
| `UNCERTAINTY_NOT_DECAY` | WARN | 父子不确定度相同，未体现逐级放大 |
| `EDGE_NOT_COVER_CALIBRATION` | WARN | 溯源边区间未覆盖子证书校准时刻 |
| `ROOT_HAS_PARENT` | WARN | 国家基准证书仍存在向上溯源边 |
| `CANDIDATE_CHAIN_FAILED_CHECKS` / `ALTERNATIVE_BRANCH_PRESENT` | WARN | 唯一链之外的其他分支提示 |

每个冲突都带 `node`（证书 id）、`path_index`（所属候选链下标，节点级为 null）
与 `detail`（结构化证据），便于定位到具体证书和证据数值。

### 5）证书停用影响评估（dry-run 模拟）

证书因吊销/过期/发现问题需要停用时，先评估影响范围再操作。提交**查询时点**与
**待停用证书 id 列表**，服务端在内存中模拟移除这些证书及其全部关联溯源边
（`child` 或 `parent` 命中即移除），**不写入数据库**（响应恒带 `dry_run: true`），
重新评估每台受影响仪器的唯一链 / 多候选 / 断裂状态，并给出：

- `before` / `after`：停用前后的完整评估快照（与 `/api/reports` 同构）；
- `transition`：状态迁移（如 `unique_to_broken`、`ambiguous_to_unique`、`unchanged`）；
- `severed_chains`：停用前存在、因途经被停用证书而被切断的链（含被切断的候选链）；
- `alternative_chain` / `alternative_chains`：停用后仍成立的唯一替代链 / 全部候选链摘要；
- `simulation_notes`：可解释说明（`severity=INFO`，非真实数据冲突）。

默认自动覆盖所有受影响仪器（被停用证书所属仪器 + 停用前任一溯源链经过这些证书的
仪器）；也可用 `instrument_codes` 仅筛选指定仪器评估。

```bash
curl -X POST http://localhost:8000/api/assessments/certificate-deactivation \
  -H 'Content-Type: application/json' -d '{
  "at": "2026-06-15T00:00:00Z",
  "certificate_ids": ["CR"],
  "instrument_codes": ["W"]
}'
```

```jsonc
{
  "at": "2026-06-15T00:00:00Z",
  "dry_run": true,                       // 恒为 true：模拟不写库
  "disabled_certificate_ids": ["CR"],
  "instruments_filter": ["W"],           // null 表示自动覆盖全部受影响仪器
  "affected_owner_instruments": ["R"],   // 被停用证书直接所属仪器
  "removed_edge_count": 2,               // 一并模拟移除的溯源边数
  "affected_instrument_count": 1,
  "evaluated_instrument_count": 1,
  "transition_counts": {"unique_to_broken": 1},
  "results": [
    {
      "instrument": "W",
      "impacted": true,
      "transition": "unique_to_broken",
      "before": { "resolution": "unique", "active_chain": { /* ... */ } },
      "after":  { "resolution": "broken", "conflicts": [ /* ... */ ] },
      "alternative_chain": null,         // 停用后无唯一有效替代链
      "alternative_chains": [],
      "severed_chains": [
        { "role": "active",              // active | candidate | broken
          "certificate_ids": ["CW", "CR", "CG"],
          "reaches_national_standard": true,
          "disabled_certificates_on_path": ["CR"],
          "chain": { "nodes": [ /* ... */ ], "edges": [ /* ... */ ] } }
      ],
      "simulation_notes": [
        {"code": "SIMULATED_CHAIN_SEVERED", "severity": "INFO", "message": "..."},
        {"code": "SIMULATED_NO_ALTERNATIVE_CHAIN", "severity": "INFO", "message": "..."}
      ]
    }
  ],
  "statistics": {"certificates_before": 3, "certificates_after": 2,
                 "edges_before": 2, "edges_after": 0}
}
```

> 多候选场景停用一条参考通道后，若另一通道仍成立，`transition` 为
> `ambiguous_to_unique`，替代链出现在 `alternative_chain`。
> 待停用证书 id 不存在或筛选仪器不存在时返回 **422**（中文原因）；
> 重复/带空白的证书 id 会自动去重。

模拟说明（`simulation_notes`，均为 INFO 级）：

| code | 含义 |
| --- | --- |
| `SIMULATED_START_CERTIFICATE_REMOVED` | 仪器在查询时点的有效起点证书被停用 |
| `SIMULATED_CHAIN_SEVERED` | 停用前的链/候选链途经被停用证书，已被切断（附链与命中证书） |
| `SIMULATED_ALTERNATIVE_CHAIN_AVAILABLE` | 停用后仍存在唯一有效替代链（附替代链节点） |
| `SIMULATED_NO_ALTERNATIVE_CHAIN` | 停用后断裂，不存在唯一有效替代链 |
| `SIMULATED_AMBIGUOUS_AFTER_DEACTIVATION` | 停用后仍为多候选，无唯一替代链 |
| `SIMULATED_RESULT_UNDETERMINED` | 停用后候选链枚举截断、上限外仍有分支，结果未定，不存在唯一有效替代链（附已枚举样本数） |
| `SIMULATED_NO_EFFECT` | 停用证书不在该仪器该时点的溯源路径上，状态不变 |

### 6）批次共同溯源核查（POST /api/assessments/common-traceability）

需要确认**多台仪器在同一 UTC 时点是否溯源到同一张证书**（如两份校准结果
能否回溯到同一台参考标准/基准）时，提交**至少两台不同仪器编码**与查询时点。
服务端逐台**复用现有单仪器链路报告**（`/api/reports` 的同一引擎）：

- **仅当**某台仪器在该时点的报告为“唯一成链”（`resolution=unique`）**且零
  ERROR** 时，它才以该唯一证书链参与比较；
- 其余仪器（仪器不存在 / 无有效证书 / 起点证书不唯一 / **存在多条候选链** /
  链断裂 / 唯一链未通过校验）在 `nonparticipants` 中**逐台返回不可参与原因**，
  并附该时点完整冲突明细；**多候选链绝不任意选取一条参与**。

对参与仪器的链证书 id 集合求交集，得到全部共同证书候选；按下列规则选出
**最近共同证书**（规则随响应 `selection_rule` 一并返回）：

1. 各参与仪器的链到候选共同证书的**距离最大值最小**；
2. 最大值相同，取**距离总和最小**；
3. 仍相同，取**证书 ID 字典序末位**（确定性兜底）。

距离 = 该仪器起点证书沿链到共同证书经过的边数（起点证书自身为 0）。
响应给出共同证书节点详情、全部候选及距离指标，以及每台参与仪器从其起点
证书**到共同证书（含）为止的证书子链**与距离。交集为空时
`common_certificate_found=false`、共同证书字段为 `null`，并在 `reason_code`
中明确给出 `NO_COMMON_CERTIFICATE`（参与仪器不足 2 台时为
`INSUFFICIENT_PARTICIPANTS`）。响应恒带 `"dry_run": true`——**只读计算，
不写入任何业务数据**；现有单仪器查询（`/api/trace`、`/api/reports`）语义不变。

```bash
curl -X POST http://localhost:8000/api/assessments/common-traceability \
  -H 'Content-Type: application/json' -d '{
  "at": "2026-06-15T00:00:00Z",
  "instrument_codes": ["INST-W1", "INST-W2",
                       "INST-P1", "INST-T1", "INST-B1"]
}'
```

```jsonc
{
  "at": "2026-06-15T00:00:00Z",
  "dry_run": true,                          // 恒为 true：只读核查，不写库
  "instrument_codes": ["INST-W1", "INST-W2", "INST-P1", "INST-T1", "INST-B1"],
  "participant_count": 2,
  "nonparticipant_count": 3,
  "participants": ["INST-W1", "INST-W2"],  // 仅唯一且校验通过的链参与
  "nonparticipants": [
    {
      "instrument": "INST-P1",             // 多候选：不任取一条
      "reason_code": "MULTIPLE_VALID_CHAINS",
      "reason": "仪器 INST-P1 在查询时点 ... 存在 2 条均到达国家基准且通过校验的候选链，链不唯一...",
      "resolution": "ambiguous", "valid": false,
      "starts": ["CPP1-1"],
      "conflicts": [ /* 与 /api/reports 同构的完整冲突明细 */ ],
      "error_count": 1, "warning_count": 0
    },
    {
      "instrument": "INST-T1",             // 链断裂
      "reason_code": "CHAIN_BROKEN", "resolution": "broken", "valid": false,
      "starts": ["CT1-2"], "conflicts": [ /* INTERVAL_GAP / DEAD_END ... */ ],
      "error_count": 3, "warning_count": 0
    },
    {
      "instrument": "INST-B1",             // 到基准但校验失败（摘要被篡改）
      "reason_code": "ALL_CHAINS_FAILED_CHECKS", "resolution": "broken",
      "valid": false, "starts": ["CBB1-1"],
      "conflicts": [ /* SIGNATURE_DIGEST_MISMATCH ... */ ],
      "error_count": 2, "warning_count": 0
    }
  ],
  "common_certificate_found": true,
  "common_certificate_id": "CG-1",         // 最近共同证书（国家长度基准）
  "common_certificate": { "certificate_id": "CG-1", "is_national_standard": true,
                          "cert_number": "NIM-LEN-2024-001", ... },
  "reason_code": null,
  "reason": null,
  "selection_rule": [
    "各参与仪器唯一有效链到候选共同证书的距离最大值最小",
    "距离最大值相同时，距离总和最小",
    "仍相同时，取证书 ID 字典序末位"
  ],
  "candidates": [                           // 全部共同证书候选（按当选顺序）
    {
      "certificate_id": "CG-1", "max_distance": 2, "total_distance": 4,
      "distances": [ {"instrument": "INST-W1", "distance": 2},
                     {"instrument": "INST-W2", "distance": 2} ],
      "selected": true
    }
  ],
  "instrument_chains": [
    {
      "instrument": "INST-W1", "participated": true,
      "start_certificate_id": "CW1-1",
      "chain_certificate_ids": ["CW1-1", "CR1-1", "CG-1"],  // 完整唯一链
      "sub_chain": {                        // 到共同证书（含）的证书子链
        "nodes": [ {"certificate_id": "CW1-1", ...},
                   {"certificate_id": "CR1-1", ...},
                   {"certificate_id": "CG-1", ...} ],
        "edges": [ {"child_cert_id": "CW1-1", "parent_cert_id": "CR1-1", ...},
                   {"child_cert_id": "CR1-1", "parent_cert_id": "CG-1", ...} ],
        "reaches_national_standard": true, "cycle": false,
        "errors": 0, "warnings": 0
      },
      "distance": 2,
      "nonparticipation_reason_code": null,
      "nonparticipation_reason": null
    },
    {
      "instrument": "INST-W2", "participated": true,
      "start_certificate_id": "CW2-1",
      "chain_certificate_ids": ["CW2-1", "CR2-1", "CG-1"],
      "sub_chain": { "nodes": [ /* CW2-1 -> CR2-1 -> CG-1 */ ],
                     "edges": [ /* ... */ ] },
      "distance": 2,
      "nonparticipation_reason_code": null,
      "nonparticipation_reason": null
    },
    {
      "instrument": "INST-P1", "participated": false,
      "start_certificate_id": null, "chain_certificate_ids": [],
      "sub_chain": null, "distance": null,
      "nonparticipation_reason_code": "MULTIPLE_VALID_CHAINS",
      "nonparticipation_reason": "..."
    }
    // INST-T1 / INST-B1 同形（CHAIN_BROKEN / ALL_CHAINS_FAILED_CHECKS）
  ],
  "statistics": {"requested_instrument_count": 5, "participant_count": 2,
                 "nonparticipant_count": 3, "reports_evaluated": 4,
                 "common_certificate_candidate_count": 1, ...}
}
```

不可参与原因码（`nonparticipants[].reason_code`）：

| code | 含义 |
| --- | --- |
| `INSTRUMENT_NOT_FOUND` | 仪器编码不存在于库中 |
| `NO_ACTIVE_CERTIFICATE` | 查询时点无处于有效区间的证书，链无法开始 |
| `MULTIPLE_ACTIVE_CERTIFICATES` | 查询时点多张证书有效，起点不唯一 |
| `MULTIPLE_VALID_CHAINS` | 存在 ≥2 条到基准且校验通过的候选链，**不任取一条** |
| `CHAIN_BROKEN` | 全部候选分支到基准前终止（死路/断档/环） |
| `ALL_CHAINS_FAILED_CHECKS` | 有到基准的候选链，但均未通过校验（签名/区间/不确定度） |
| `CHAIN_VALIDATION_FAILED` | 唯一成链但链上存在 ERROR（如历史证书区间断档） |
| `RESULT_UNDETERMINED` | 候选链枚举达到 500 条上限且上限外仍有分支、已见合格链不足 2 条，结果未定——不按样本链参与，也不误报为断裂 |

顶层 `reason_code`：参与仪器不足 2 台时为 `INSUFFICIENT_PARTICIPANTS`；
参与仪器链证书 id 交集为空时为 `NO_COMMON_CERTIFICATE`（此时各参与仪器的
`sub_chain/distance` 为 `null`，但仍返回其完整 `chain_certificate_ids`）。
请求中仪器少于 2 台、编码重复或含空白字符串时返回 **422**（中文原因）；
时间入参按 UTC 解释，建议统一用 `...Z` 格式，留空取服务端当前时间。

### 7）测量相容性评估（POST /api/assessments/measurement-compatibility）

需要判断**多台仪器在同一时点的测量读数是否两两相容**（实验室间比对、
期间核查等场景）时，提交 **UTC 时点**、**目标单位**与 **2～12 条互异仪器
编码及有限测量值**。服务端逐条**复用现有单仪器链路报告**
（`/api/reports` 的同一引擎）：

- **仅当**仪器在该时点的报告为“唯一成链且零 ERROR”时，其读数才参与计算，
  并取其**唯一有效链起点证书**的扩展不确定度作为该读数的 `U`；
- 链不可用（不存在 / 无有效证书 / 起点不唯一 / **多候选链** / 断裂 /
  校验失败）或**起点证书单位与目标单位不符**的读数，在 `nonparticipants`
  与 `readings` 中**逐条返回原因**、不参与计算；多候选链绝不任意选取一条。

对全部可参与读数**两两**计算 En 数（`pairs`，按仪器编码稳定排序）：

```
En = |x_i - x_j| / sqrt(U_i^2 + U_j^2)     En ≤ 1  判为相容
```

En 计算以**两个不确定度中的较大者为尺度做稳定缩放**（缩放后用
`hypot` 求范数），避免直接平方求和在极端量级下出错：

- 不确定度约 1e308 时 `U²` 溢出为无穷、分母变为无穷，差异巨大的一对
  会被误算成 En=0 而**误判相容**；
- 不确定度为非零**次正规**值（如 5e-324）时 `U²` 下溢为 0，分母误判
  为“双零”而错走读数相等规则，进而**污染极大相容分组**。

`pairs` 的相容判定是唯一真相来源，`groups` 直接复用同一判定派生
（两者绝不分叉）。两个不确定度**确实均为 0** 时分母为 0、En 无定义
（响应中 `en=null`，`rule=zero_uncertainty_equality`），此时**仅当
两读数相等才相容**；仅一方为 0 时仍按常式计算。

若**读数差绝对值**（如 ±1.7e308 相减）或 **En 本身**（差值有限但相对
不确定度过大）超出有限浮点可表示范围，接口返回 **422** 并在
`detail` 中给出原因码（`READING_DIFFERENCE_NOT_FINITE` /
`EN_SCORE_NOT_FINITE`）、中文说明与仪器对证据，**不让 ±Infinity/NaN
进入响应**；起点证书登记了非有限不确定度时同理以
`CERTIFICATE_UNCERTAINTY_NOT_FINITE` 返回 422。

分组取**全部人数 ≥ 2 的极大两两相容分组**（相容关系图的**极大团**）：
分组可重叠（同一仪器可属于多个分组），组内成员与分组列表均按仪器编码
稳定排序。**不以连通分量代替**——连通分量会把两两并不相容的仪器并入
同一组（如 A-B、B-C 相容而 A-C 不相容时，{A,B,C} 不是两两相容分组，
应输出 {A,B} 与 {B,C} 两个极大团）。可参与读数不足 2 条时 `pairs`、
`groups` 为空，顶层 `reason_code=INSUFFICIENT_PARTICIPANTS`。
响应恒带 `"dry_run": true`——**只读计算，不写入任何业务数据**。

```bash
curl -X POST http://localhost:8000/api/assessments/measurement-compatibility \
  -H 'Content-Type: application/json' -d '{
  "at": "2026-06-15T00:00:00Z",
  "target_unit": "mm",
  "readings": [
    {"instrument_code": "INST-W1", "value": 10.0010},
    {"instrument_code": "INST-W2", "value": 10.0020},
    {"instrument_code": "INST-P1", "value": 10.0015},
    {"instrument_code": "INST-T1", "value": 10.0},
    {"instrument_code": "INST-B1", "value": 10.0}
  ]
}'
```

```jsonc
{
  "at": "2026-06-15T00:00:00Z",
  "dry_run": true,                           // 恒为 true：只读评估，不写库
  "target_unit": "mm",
  "reading_count": 5,
  "participant_count": 2,
  "nonparticipant_count": 3,
  "participants": ["INST-W1", "INST-W2"],    // 唯一且校验通过的链才参与
  "nonparticipants": [
    {
      "instrument": "INST-P1",               // 多候选：不任取一条
      "value": 10.0015,
      "reason_code": "MULTIPLE_VALID_CHAINS",
      "reason": "仪器 INST-P1 在查询时点 ... 存在 2 条均到达国家基准且通过校验的候选链...",
      "resolution": "ambiguous", "valid": false,
      "starts": ["CPP1-1"],
      "conflicts": [ /* 与 /api/reports 同构的完整冲突明细 */ ],
      "start_certificate_id": null, "certificate_unit": null,
      "error_count": 1, "warning_count": 0
    },
    { "instrument": "INST-T1", "reason_code": "CHAIN_BROKEN", /* ... */ },
    { "instrument": "INST-B1", "reason_code": "ALL_CHAINS_FAILED_CHECKS", /* ... */ }
  ],
  "readings": [                               // 逐条读数结果（按请求顺序）
    {
      "instrument": "INST-W1", "value": 10.001, "participated": true,
      "start_certificate_id": "CW1-1",        // U 取自该起点证书
      "certificate_unit": "mm", "uncertainty": 0.002,
      "nonparticipation_reason_code": null, "nonparticipation_reason": null
    },
    { "instrument": "INST-W2", "value": 10.002, "participated": true,
      "start_certificate_id": "CW2-1", "certificate_unit": "mm",
      "uncertainty": 0.003, /* ... */ },
    { "instrument": "INST-P1", "value": 10.0015, "participated": false,
      "nonparticipation_reason_code": "MULTIPLE_VALID_CHAINS", /* ... */ }
    // INST-T1 / INST-B1 同形
  ],
  "pairs": [                                  // 可参与读数的两两 En（按编码排序）
    {
      "instrument_a": "INST-W1", "instrument_b": "INST-W2",
      "value_a": 10.001, "value_b": 10.002,
      "uncertainty_a": 0.002, "uncertainty_b": 0.003,
      "difference": 0.0010000000000012221,
      "en": 0.2773500981129535,               // En≤1 -> 相容
      "compatible": true,
      "rule": "en_le_1"
    }
  ],
  "groups": [                                 // 极大两两相容分组（极大团，可重叠）
    {"members": ["INST-W1", "INST-W2"], "size": 2}
  ],
  "reason_code": null,                        // 可参与不足 2 条时为 INSUFFICIENT_PARTICIPANTS
  "reason": null,
  "statistics": {"requested_reading_count": 5, "participant_count": 2,
                 "pair_count": 1, "compatible_pair_count": 1,
                 "group_count": 1, "reports_evaluated": 5, ...}
}
```

不可参与原因码（`nonparticipants[].reason_code`）在共同溯源核查原因码
（`INSTRUMENT_NOT_FOUND` / `NO_ACTIVE_CERTIFICATE` /
`MULTIPLE_ACTIVE_CERTIFICATES` / `MULTIPLE_VALID_CHAINS` / `CHAIN_BROKEN` /
`ALL_CHAINS_FAILED_CHECKS` / `CHAIN_VALIDATION_FAILED` /
`RESULT_UNDETERMINED`）之外，新增：

| code | 含义 |
| --- | --- |
| `CERTIFICATE_UNIT_MISMATCH` | 链唯一且校验通过，但起点证书不确定度单位与目标单位不符（附 `certificate_unit` 与 `start_certificate_id`） |

请求校验：读数少于 2 条或多于 12 条、仪器编码重复/为空白、测量值为
NaN/±Inf（非有限浮点）、缺 `target_unit`/`readings` 字段时返回 **422**
（中文原因）；读数量级合法但两两**读数差绝对值或 En 超出有限浮点可
表示范围**时同样返回 **422**（附原因码与仪器对，不把无穷值放入响应）；
时间入参按 UTC 解释，建议统一用 `...Z` 格式，`at` 留空取
服务端当前时间。

### 8）证书修订历史（GET /api/certificates/{id}/revisions · /revisions/diff）

单条证书接口（`POST /api/certificates`）与批量提交（`POST /api/submissions`）
每次按 ID 写入证书时，在**同一事务**内维护可追查的连续版本历史：

- **首次保存**：完整业务字段快照记为 **v1**；
- **存量证书**（功能上线前已入库、尚无版本记录的证书）首次更新前，先把
  **旧值补录为 v1**，再按新值判定是否追加；
- **仅当业务字段实际变化**（`instrument_code / cert_number / issued_at /
  valid_from / valid_until / uncertainty / unit / digest_alg /
  signature_digest / signed_payload`）才追加连续版本 vN+1；
  **同内容重交不增版**；
- 版本写入与证书更新处于**同一事务**，任一步失败整体回滚，不会出现
  “证书已改但版本缺失”的中间态；
- 批量预检（`/api/submissions/preflight`）在独立事务中模拟并整体回滚，
  **绝不留下版本**。

```bash
# ① 首次保存 -> v1
curl -X POST http://localhost:8000/api/certificates \
  -H 'Content-Type: application/json' -d '{
    "id": "CW", "instrument_code": "W", "cert_number": "CAL-101",
    "issued_at": "2026-01-10T00:00:00Z",
    "valid_from": "2026-01-01T00:00:00Z", "valid_until": "2027-01-01T00:00:00Z",
    "uncertainty": 0.002, "unit": "mm"
  }'

# ② 修改不确定度 -> 追加 v2（同内容重交不会增版）
curl -X POST http://localhost:8000/api/certificates \
  -H 'Content-Type: application/json' -d '{
    "id": "CW", "instrument_code": "W", "cert_number": "CAL-101",
    "issued_at": "2026-01-10T00:00:00Z",
    "valid_from": "2026-01-01T00:00:00Z", "valid_until": "2027-01-01T00:00:00Z",
    "uncertainty": 0.0015, "unit": "mm"
  }'

# ③ 按证书 ID 查询版本列表
curl http://localhost:8000/api/certificates/CW/revisions

# ④ 指定两版的字段差异
curl "http://localhost:8000/api/certificates/CW/revisions/diff?from_version=1&to_version=2"
```

```jsonc
// GET /api/certificates/CW/revisions
{
  "certificate_id": "CW",
  "revision_count": 2,
  "revisions": [
    {"certificate_id": "CW", "version": 1, "cert_number": "CAL-101",
     "uncertainty": 0.002, "unit": "mm", /* ...完整业务字段快照... */
     "recorded_at": "2026-09-24T08:00:00Z"},
    {"certificate_id": "CW", "version": 2, "cert_number": "CAL-101",
     "uncertainty": 0.0015, "unit": "mm",
     "recorded_at": "2026-09-24T09:00:00Z"}
  ]
}

// GET /api/certificates/CW/revisions/diff?from_version=1&to_version=2
// 载荷/摘要由业务字段重算，随 uncertainty 一并变化
{
  "certificate_id": "CW",
  "from_version": 1,
  "to_version": 2,
  "changed_fields": ["uncertainty", "signature_digest", "signed_payload"],
  "changes": {
    "uncertainty": {"from": 0.002, "to": 0.0015},
    "signature_digest": {"from": "9f86…", "to": "2c62…"},
    "signed_payload": {"from": "{...}", "to": "{...}"}
  }
}
```

证书不存在返回 **404**；引用的版本号不存在同样返回 **404**
（如 `证书 CW 不存在版本 3`，中文原因）。`from_version == to_version`
时 `changed_fields` 为空、`changes` 为空对象。

---

## 六、内置演示场景

演示查询时点统一为 **2026-06-15T00:00:00Z**：

| 仪器 | 结果 | 说明 |
| --- | --- | --- |
| `INST-W1` 数显卡尺 | `unique` / valid | CW1-1 → CR1-1 → CG-1（国家长度基准），全部检查通过 |
| `INST-W2` 高度尺 | `unique` / valid | CW2-1 → CR2-1 → CG-1，与 INST-W1 共享国家长度基准 CG-1（共同溯源核查用） |
| `INST-P1` 精密压力表 | `ambiguous` | 经活塞压力计 A 或数字校准器 B 均能到国家压力基准，2 条有效候选链 |
| `INST-T1` 热电偶 | `broken` | 历史证书 2025 年夏季断档（`INTERVAL_GAP`），且参考标准未再溯源（`DEAD_END`） |
| `INST-B1` 三坐标 | `broken` | 链结构完整，但证书登记摘要被篡改（`SIGNATURE_DIGEST_MISMATCH`） |

```bash
curl "http://localhost:8000/api/reports/INST-W1?at=2026-06-15T00:00:00Z"
curl "http://localhost:8000/api/reports/INST-P1?at=2026-06-15T00:00:00Z"
curl "http://localhost:8000/api/reports/INST-T1?at=2026-06-15T00:00:00Z"
curl "http://localhost:8000/api/reports/INST-B1?at=2026-06-15T00:00:00Z"
```

时间窗覆盖查询示例：热电偶 `INST-T1` 的旧证 2025-06-30 到期、新证 2025-09-01
生效，窗内自动划出“有效 → 夏季断档 → 恢复”三段：

```bash
curl -G "http://localhost:8000/api/coverage/INST-T1" \
  --data-urlencode "start=2025-01-01T00:00:00Z" \
  --data-urlencode "end=2025-12-31T23:59:59Z"
# segments:
#  [2025-01-01 含, 2025-06-30 含]        broken（DEAD_END：参考标准未再溯源；INTERVAL_GAP）
#  (2025-06-30, 2025-09-01) 两端均不含   broken（NO_ACTIVE_CERTIFICATE + INTERVAL_GAP 断档）
#  [2025-09-01 含, 2025-12-31 含]        broken（DEAD_END）
```

> 三段冲突码不同因此不会合并；若把窗口缩小到不含任何边界的区间，
> 结果为单个两端都含的区段。

停用评估示例（dry-run）：停用压力多候选中的活塞压力计通道证书，观察
`INST-P1` 由 `ambiguous` 变为经数字校准器 B 的 `unique` 替代链；停用一等量块
证书 `CR1-1` 则会使 `INST-W1` 由唯一链变为断裂：

```bash
curl -X POST http://localhost:8000/api/assessments/certificate-deactivation \
  -H 'Content-Type: application/json' -d '{
  "at": "2026-06-15T00:00:00Z",
  "certificate_ids": ["CPRA-1"]
}'

curl -X POST http://localhost:8000/api/assessments/certificate-deactivation \
  -H 'Content-Type: application/json' -d '{
  "at": "2026-06-15T00:00:00Z",
  "certificate_ids": ["CR1-1"],
  "instrument_codes": ["INST-W1"]
}'
```

批次共同溯源核查示例：`INST-W1` 与 `INST-W2` 各自唯一有效链共同溯源到国家
长度基准 `CG-1`（共同证书 `CG-1`，距离均为 2）；同次提交中 `INST-P1`
（多候选）、`INST-T1`（断档/死路）、`INST-B1`（摘要篡改）逐台返回不可参与
原因，其链不会被任意选取：

```bash
curl -X POST http://localhost:8000/api/assessments/common-traceability \
  -H 'Content-Type: application/json' -d '{
  "at": "2026-06-15T00:00:00Z",
  "instrument_codes": ["INST-W1", "INST-W2",
                       "INST-P1", "INST-T1", "INST-B1"]
}'
# common_certificate_found=true, common_certificate_id="CG-1"
# participants=["INST-W1","INST-W2"]（距离均为 2）
# nonparticipants:
#   INST-P1 -> MULTIPLE_VALID_CHAINS
#   INST-T1 -> CHAIN_BROKEN
#   INST-B1 -> ALL_CHAINS_FAILED_CHECKS
```

测量相容性评估示例：`INST-W1`（U=0.002mm）与 `INST-W2`（U=0.003mm）的
读数差 0.001mm，En≈0.277≤1 判为相容并组成一个二人分组；`INST-P1`/
`INST-T1`/`INST-B1` 因链不可用逐条返回原因、不参与计算：

```bash
curl -X POST http://localhost:8000/api/assessments/measurement-compatibility \
  -H 'Content-Type: application/json' -d '{
  "at": "2026-06-15T00:00:00Z",
  "target_unit": "mm",
  "readings": [
    {"instrument_code": "INST-W1", "value": 10.0010},
    {"instrument_code": "INST-W2", "value": 10.0020},
    {"instrument_code": "INST-P1", "value": 10.0015},
    {"instrument_code": "INST-T1", "value": 10.0},
    {"instrument_code": "INST-B1", "value": 10.0}
  ]
}'
# participants=["INST-W1","INST-W2"]
# pairs: INST-W1~INST-W2 en≈0.277, compatible=true
# groups: [{"members":["INST-W1","INST-W2"],"size":2}]
# nonparticipants:
#   INST-P1 -> MULTIPLE_VALID_CHAINS
#   INST-T1 -> CHAIN_BROKEN
#   INST-B1 -> ALL_CHAINS_FAILED_CHECKS
```

校准工位预约示例（单工位、半开占用、起点证书截止）：

```bash
# 空档查询：演示库已占用 INST-W1 的 2026-06-20 09:00~10:00（半开）
curl -G "http://localhost:8000/api/calibration-slots" \
  --data-urlencode "instrument=INST-W1" \
  --data-urlencode "start=2026-06-20T09:00:00Z" \
  --data-urlencode "end=2026-06-20T12:00:00Z" \
  --data-urlencode "duration_seconds=3600"
# slot: 2026-06-20T10:00:00Z ~ 11:00:00Z（09~10 被占用，半开相接后最早空档）
# certificate_id=CW1-1, certificate_valid_until=2027-01-01T00:00:00Z

# 确认预约（结束 11:00，恰在既有占用结束点，半开不冲突）
curl -X POST http://localhost:8000/api/calibration-bookings \
  -H 'Content-Type: application/json' -d '{
    "instrument_code": "INST-W1",
    "start": "2026-06-20T10:00:00Z",
    "duration_seconds": 3600
  }'
# 201

# 链不可用的仪器（INST-T1 断裂 / INST-P1 多候选 / INST-B1 摘要篡改）
# 空档查询 slot=null 并说明原因：
curl -G "http://localhost:8000/api/calibration-slots" \
  --data-urlencode "instrument=INST-P1" \
  --data-urlencode "start=2026-06-20T09:00:00Z" \
  --data-urlencode "end=2026-06-20T12:00:00Z" \
  --data-urlencode "duration_seconds=3600"
# reason_code=TRACEABILITY_CHAIN_UNAVAILABLE,
# chain_reason_code=MULTIPLE_VALID_CHAINS（多候选，绝不任取一条），
# trace_report 给出完整可解释冲突

# 按仪器查询已确认预约
curl "http://localhost:8000/api/calibration-bookings?instrument=INST-W1"

# 批量改期：演示预约与刚确认的预约一起后移一天（时长缺省沿用原值，
# 仪器与备注保留；本批旧占用整体排除，允许交换时段与首尾相接）
curl -X POST http://localhost:8000/api/calibration-bookings/reschedule \
  -H 'Content-Type: application/json' -d '{
    "items": [
      {"booking_id": "BK-DEMO-20260620-W1", "start": "2026-06-21T09:00:00Z"},
      {"booking_id": "<上一步返回的预约 id>", "start": "2026-06-21T10:00:00Z"}
    ]
  }'
# 200：两条预约时段更新；任一核验失败（预约不存在 / 链不可用 /
# 越过证书截止 / 时段冲突）则 409 且整批回滚；
# 时段变化的预约其候补中（waiting）队列随旧时段一并失效（expired）
```

候补与取消补位示例（演示库已预置 `INST-W2` 候补 `BK-DEMO-20260620-W1`
的 09:00~10:00 时段，队列名次 1）：

```bash
# 候补状态查询：队列名次与状态
curl "http://localhost:8000/api/calibration-waitlist?booking_id=BK-DEMO-20260620-W1"
# -> WL-DEMO-20260620-W2  status=waiting  queue_position=1

# 再登记一条候补（INST-T1 在 2026-06-20 溯源链断裂，补位时会被跳过）
curl -X POST http://localhost:8000/api/calibration-bookings/BK-DEMO-20260620-W1/waitlist \
  -H 'Content-Type: application/json' -d '{"instrument_code": "INST-T1"}'
# 201, queue_position=2

# 取消目标预约：同一写事务内释放占用并按队列补位
curl -X DELETE http://localhost:8000/api/calibration-bookings/BK-DEMO-20260620-W1
# 200：promoted=true，首个合格候补 INST-W2 转为已确认预约
#     （沿用 09:00~10:00，证书快照 CW2-1 / 2027-02-01）；
#     INST-T1 排在中签者之后、未及评估而失效（expired）

# 重复取消：404 BOOKING_NOT_FOUND，不会重复补位
curl -X DELETE http://localhost:8000/api/calibration-bookings/BK-DEMO-20260620-W1

# 候补终态可追溯
curl "http://localhost:8000/api/calibration-waitlist?booking_id=BK-DEMO-20260620-W1"
# WL-DEMO-20260620-W2 -> promoted（promoted_booking_id 指向新预约）
# INST-T1 的候补     -> expired
```

已确认预约有效性巡检示例（只读；演示预约在 2026-06-20 当天窗内为 valid）：

```bash
# 巡检 2026-06-20 当天开始的全部已确认预约（UTC 半开窗 [start, end)）
curl -G "http://localhost:8000/api/calibration-bookings/inspection" \
  --data-urlencode "start=2026-06-20T00:00:00Z" \
  --data-urlencode "end=2026-06-21T00:00:00Z"
# summary: {"total":1,"valid":1,"changed":0,"invalid":0}
# items[0]: BK-DEMO-20260620-W1 / INST-W1 / status=valid
#   current CW1-1 @ 2027-01-01 == 快照 CW1-1 @ 2027-01-01

# 窗起点不早于终点 -> 422
curl -G "http://localhost:8000/api/calibration-bookings/inspection" \
  --data-urlencode "start=2026-06-21T00:00:00Z" \
  --data-urlencode "end=2026-06-21T00:00:00Z"
# 422 {"detail":"巡检查询窗起点 start 必须严格早于终点 end"}
```

## 七、溯源算法说明

**快照与索引。** 每次评估先把仪器/证书/溯源边装载为内存快照
（`engine.Catalog`；证书停用评估则在内存中派生“移除证书与关联边”的
新快照，不写库）。装载（或派生）时一次性建立三类索引：

- **按仪器分组的证书索引**（`certs_by_instrument`，按
  `(valid_from, valid_until)` 预排序）——起点选取与历史区间断档扫描
  只访问相关仪器的分组，不再每台仪器重复扫描全库证书；
- **按边 ID 定位的索引**（`edges_by_id`）——链级校验与链序列化按边 id
  直接取边，不再每条候选链重建全边映射；
- **父子边索引**（`out_edges`，子证书 → 全部向上溯源边）——链路枚举的
  邻接表；另预取国家基准证书列表供基准巡检直接遍历。

索引只属于当前快照：派生视图在自己的构造中重建索引，不污染原快照；
每个请求各自装载新 Catalog，后续请求总是读取最新数据。下列步骤 1~5
均在同一快照上复用这些索引，扫描量随快照规模增长而非候选链数。

1. 取目标仪器在 `at` 生效（`valid_from <= at <= valid_until`）的证书作为起点；
2. 自起点沿在 `at` 有效的边做 DFS 枚举（同父子证书的并行边合并；检测环；500 条上限保护——达到上限且仍有未展开分支时置 `paths_truncated=true`）；
3. 终止条件：到达国家基准仪器证书（成链）、无有效边（死路）、父证书失效（断档）、成环；
4. 对每条链执行节点级（时点有效性、签名摘要）与链级（父区间覆盖校准时刻、
   不确定度单调不收缩、单位一致、边覆盖校准时刻）检查；对链上全部仪器检查
   历史区间连续性——按各仪器证书有效期的**闭区间并集**扫描：仅当某时刻不被
   任何证书覆盖才报 `INTERVAL_GAP`（前证书取把并集推进到缺口边界的证书，
   缺口边界为并集真实覆盖终点），嵌套/重叠区间不产生假空隙；
5. 决策：成链且零 ERROR 的路径恰有 1 条 → `unique`；≥2 条 → `ambiguous`；
   0 条 → `broken`。所有 ERROR/WARN 汇总为可解释冲突列表。
   **枚举截断保护**：达到 500 条上限且上限外仍有分支时，已枚举路径仅为样本——
   已见 ≥2 条合格链仍判 `ambiguous`；已见合格链不足 2 条判 `undetermined`
   （结果未定，保留样本链与截断说明、`active_chain=null`，不下唯一/断裂定论，
   追加 ERROR `RESULT_UNDETERMINED`）；恰好 500 条穷尽分支则正常定论。
6. 证书停用影响评估：在内存 Catalog 中移除证书及其关联边（不写库），对每台受影响
   仪器分别在停用前/后的 Catalog 上重跑步骤 1~5，比较状态与链指纹，输出迁移、
   被切断链、替代链与 INFO 级模拟说明。
7. 仪器时间窗覆盖查询：收集该仪器沿溯源边闭包可达的证书/边在窗内的
   `valid_from / valid_until` 边界，与窗口端点合成评估网格；在每个边界点（闭区间）
   及相邻边界间的开区间内部各跑一次步骤 1~5；仅当状态、有效性与冲突码多重集相同
   且端点严格连续（一含一不含）时合并，输出带端点包含标记的时间区段，
   使闭区间交接点上的瞬时冲突独立成段而不被吞并。
8. 批次共同溯源核查：对每台请求仪器在同一时点各跑一次步骤 1~5（只读）；
   仅收集“唯一成链且零 ERROR”的仪器链参与比较，其余仪器逐台记录不可参与
   原因（多候选绝不任取一条）。对参与链的证书 id 集合求交集，候选共同证书
   按“各链距离最大值最小 → 距离总和最小 → 证书 ID 字典序末位”排序选出
   最近共同证书，并从各链截取到该证书（含）为止的子链与距离；交集为空时
   明确返回无共同证书。全程不写入业务数据。
9. 校准工位预约：全实验室单个工位，占用按半开区间 `[start, end)` 计算。
   GET 先在查询起点复用步骤 1~5 要求“唯一成链且零 ERROR”作为入口门禁，
   再把与整个请求窗半开相交的既有预约与生效中维护封锁窗口合并为工位
   占用，贪心求物理最早空档；**对每个候选起点重放与确认相同的门禁**
   （候选起点重新要求唯一成链、候选结束不晚于该起点证书 `valid_until`），
   占用把建议起点推后后绝不沿用查询起点的旧溯源结论；候选不可确认时沿
   证书与溯源边的有效期边界继续寻找——边界为闭区间端点，即使落在占用段
   起点也单独重验，闭区间交接点（两证同时有效）不合格则从其后一个整秒
   继续；全窗无合格时段按
   `NO_FREE_SLOT_IN_WINDOW / CERTIFICATE_DEADLINE_TOO_EARLY /
   NO_CONFIRMABLE_SLOT_IN_WINDOW` 给出空建议与具体原因，响应的证书、
   截止与溯源报告对应实际建议起点（无建议时为最后重验候选）。
   POST 在 `BEGIN IMMEDIATE` 同一写事务内重查仪器、溯源链、证书截止与
   半开占用，任一不满足即 409 回滚，全部满足才原子写入；因此并发确认
   同一时段恰有一个成功，不产生重叠预约。批量改期（reschedule）同样在
   同一写事务内：核对 2～10 条预约存在且 ID 不重复，逐条按新起点重验
   唯一有效链与证书截止，将本批旧占用整体排除后按半开区间检查新时段
   彼此及与其他预约的冲突（允许交换时段与首尾相接），任一失败整体
   回滚，全部通过才更新时段与证书快照（仪器与备注不变）；时段实际
   变化的预约，其候补中（waiting）候补在同一事务内标记失效（expired），
   避免旧时段队列悬空。
10. 候补与取消补位：候补登记在同一写事务内核验目标预约与候补仪器存在
    后写入，沿用该预约当前起止时段快照，按登记先后（`seq` 自增）排队；
    登记时不校验溯源链与占用。取消预约在同一 `BEGIN IMMEDIATE` 写事务
    内删除预约行释放占用，依队列顺序逐条重验候补中申请（时段起点唯一
    有效溯源链、终点不晚于起点证书 `valid_until`、与剩余预约及**生效中
    维护封锁窗口**无半开冲突），不合格申请标记 `failed` 跳过（撞维护
    窗口记 `STATION_UNDER_MAINTENANCE` 原因），只将首个合格申请转为
    已确认预约（沿用原时段与时长，保存起点证书快照与候补备注）并标记
    `promoted`；排在中签者之后、未及评估的候补中条目标记 `expired`。
    预约不存在或已取消返回 404，重复取消不会重复补位；写事务串行化
    保证并发取消同一预约恰有一个完成补位。
13. 维护封锁：维护窗口按 UTC 半开区间 `[start, end)` 保存原因。登记在
    `BEGIN IMMEDIATE` 同一写事务内同时检查既有预约与活跃维护窗口
    （半开重叠判据 `x.start < end AND x.end > start`，首尾相接允许），
    任一重叠返回 409 并在 detail 中同时给出冲突预约与冲突窗口 ID，
    事务回滚绝不写入。空档查询把活跃维护窗口与预约合并为占用集合做
    最早空档贪心；确认预约与批量改期在各自写事务内重查活跃维护窗口
    （改期仅排除本批旧预约占用、不排除维护窗口，任一冲突整体回滚）。
    撤销为软删除（`status=revoked`、记 `revoked_at`），撤销后窗口不再
    参与任何占用重查，行保留用于状态追溯，重复撤销返回 404。
11. 测量相容性评估：对每条读数复用步骤 1~5（只读），仅以“唯一成链且
    零 ERROR”的链参与，取起点证书扩展不确定度为 U；链不可用或起点证书
    单位与目标单位不符的读数逐条记录原因、不参与。对可参与读数两两计算
    `En=|x_i-x_j|/sqrt(U_i^2+U_j^2)`，En≤1 判为相容；En 以最大 U 为尺度
    稳定缩放（避免平方求和在 1e308 量级溢出为无穷而误判相容、非零次正规
    U 平方下溢为 0 而误入双零规则污染分组），pairs 与 groups 共用同一份
    判定；两 U 确实均为 0 时 En 无定义，仅读数相等才相容。读数差绝对值
    或 En 超出有限浮点范围时返回 422，不让无穷值进入响应。再以
    Bron–Kerbosch（带枢轴）枚举相容关系图的全部极大团，输出人数 ≥2 的
    极大两两相容分组（可重叠，按仪器编码稳定排序，不以连通分量代替）。
    全程不写入业务数据。
12. 证书修订历史：单条证书接口与批量提交按 ID upsert 证书时，在同一事务
    内维护 `certificate_revisions` 连续版本快照——首次写入记 v1；功能
    上线前已存在的存量证书首次更新前先补录旧值为 v1；仅业务字段实际变化
    才追加 vN+1，同内容重交不增版；任一步失败整体回滚。批量预检在独立
    事务中模拟并回滚，绝不留下版本。版本列表与指定两版字段差异可按证书
    ID 查询，引用不存在的证书或版本号返回 404。
14. 已确认预约有效性巡检：只读。按 UTC 半开窗 `[start, end)` 筛选预约
    **开始时刻**（`start <= start_at < end`），按（开始时刻, 预约 ID）
    稳定排序；对每条预约在其开始时刻复用步骤 1~5 重建唯一有效溯源链
    （复用 `unique_chain_gate`，不沿用预约保存的快照），链校验通过且
    预约结束不晚于**当前**起点证书到期后，再与预约保存的证书 id 与到期
    快照比较——链失效（仪器不存在/无有效证书/起点不唯一/多候选/断裂/
    校验失败）或截止不足记 `invalid`，链仍有效但证书 id 或到期变化记
    `changed`，其余记 `valid`；返回逐条状态、原因码、当前证书与原快照
    并汇总各状态数量；`start >= end` 返回 422。全程只读，不写库。

## 八、项目结构

```
app/
  main.py            FastAPI 入口、路由注册、启动建库/播种
  database.py        SQLite 连接、表结构
  schemas.py         Pydantic 请求/响应模型
  crud.py            upsert 与写入期校验、证书修订历史（连续版本快照）
  engine.py          溯源链重建（DFS）、校验规则、内存快照 Catalog
                     （装载/派生时一次性建立按仪器分组的证书索引、
                     按边 ID 定位的索引与父子边索引，链路枚举、历史
                     断档与链级校验复用，索引不跨快照共享）、
                     停用影响模拟（派生 Catalog，不写库）、
                     仪器时间窗覆盖分段（边界点 + 开区间内部采样）、
                     批次共同溯源核查（共同证书选举 + 子链截取）、
                     测量相容性评估（En 数 + 极大团分组）、
                     unique_chain_gate（唯一有效链门禁，工位预约复用）
  booking.py         校准工位预约：单工位半开占用查询、最早空档贪心、
                     写事务内重查确认（409 冲突）、批量改期（旧占用整体
                     排除后核验新时段，409 整体回滚）、候补登记/排队/状态
                     查询与取消补位（同一写事务内释放占用、按队列顺序逐条
                     重验并只补位首个合格申请，改期使未补位候补失效）、
                     已确认预约有效性巡检（半开窗筛选，逐条只读复核唯一
                     有效链与证书快照，valid/changed/invalid）与
                     预约持久化/列表；确认/改期/补位均在写事务内重查
                     maintenance.py 中的活跃维护封锁窗口
  maintenance.py     校准工位维护封锁：UTC 半开区间维护窗口登记（同一写
                     事务内对既有预约与活跃维护窗口查重，409 附冲突 ID，
                     首尾相接允许、失败不写入）、列表（状态/时间窗筛选）、
                     软删除撤销（重复撤销 404）与占用重查
  seed.py            五场景演示数据 + 1 条校准工位演示预约 + 1 条演示候补
  routers/           instruments / certificates（含修订历史 revisions
                     与两版差异 diff）/ edges /
                     submissions（批量提交 + preflight 预检）/
                     trace(report + 时间窗覆盖 + 停用影响评估 + 批次共同溯源核查
                     + 测量相容性评估) /
                     calibration（工位空档 GET + 预约确认 POST + 批量改期
                     POST + 预约列表 GET + 已确认预约有效性巡检 GET +
                     候补登记 POST + 候补状态查询 GET
                     + 取消补位 DELETE）/
                     maintenance_windows（维护封锁登记 POST + 列表 GET +
                     撤销 DELETE）/
                     export / admin
tests/test_trace.py          12 个接口级测试
tests/test_catalog_indexes.py 10 个 Catalog 派生索引测试（索引结构、
                             停用派生视图索引隔离、后续装载读新数据、
                             多分支长链结果等价、扫描量随快照而非候选
                             链数增长）
tests/test_undetermined.py   10 个枚举截断“结果未定”测试（undetermined 语义、
                             样本链保留、已见两条合格链仍判多候选、
                             空档/确认/巡检/补位/共同溯源/相容性门禁以
                             RESULT_UNDETERMINED 拒绝）
tests/test_preflight.py      11 个批量提交预检测试
tests/test_deactivation.py   8 个停用影响评估测试
tests/test_coverage.py       16 个仪器时间窗覆盖查询测试（含 6 个小数秒：
                             交接后半秒单点不双报、截止后同秒报失效、
                             跨截止点窗口分段、交接秒内窗口、时区偏移
                             换算保留微秒、反向小数窗口 422）
tests/test_common_trace.py   12 个批次共同溯源核查测试
tests/test_measurement_compatibility.py  21 个测量相容性评估测试
tests/test_calibration.py    28 个校准工位预约测试
tests/test_inspection.py     15 个已确认预约有效性巡检测试（半开窗筛选、
                             422 入参、valid/changed/invalid 三态原因码、
                             稳定排序与汇总、只读不写库）
tests/test_reschedule.py     16 个批量改期测试
tests/test_revisions.py      15 个证书修订历史测试
tests/test_waitlist.py       20 个候补登记/查询/取消补位测试
tests/test_maintenance.py    20 个工位维护封锁登记/列表/撤销及对空档/确认/
                             改期/取消补位的半开封锁联动测试
Dockerfile · docker-compose.yml · requirements.txt
```
