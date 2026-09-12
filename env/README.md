# ShortPass · 短时通行票与多门点并发核销

给访客发短时通行票、多个门点同时核销的 Web 系统。

- **FastAPI + SQLite(WAL)**，单容器即可部署；数据落盘在 `/data` 卷，重启不丢状态。
- 无外部前端依赖（原生 HTML/JS），自带管理台与门点核销台。
- `tests/` 含 11 个端到端测试（真实 HTTP + 进程重启），覆盖下列全部不变量。

## 需求对应的关键保证

| 需求 | 实现方式 |
|---|---|
| 同一人可持有多张时间重叠的票，按票面编号核销，互不顶替 | 每张票是独立行（`tickets.code` 主键）；核销只按 `code` 定位，不做“同人唯一有效票”逻辑 |
| 两个门点同时扫同一张，恰有一个成功，另一个明确知道已使用 | 核销在单个 `BEGIN IMMEDIATE` 事务中完成，条件更新 `WHERE code=? AND status='ACTIVE'`；失败方返回 HTTP 409 + `reason=already_redeemed`，并告知 `redeemed_gate` / `redeemed_at` |
| 每张票只能成功核销一次 | 状态机单向迁移 `ACTIVE → REDEEMED/REVOKED/EXPIRED`，终态无任何回退路径；核销事件 append-only |
| 作废一张，其他有效期内的票继续可用 | 作废只改对应 `code` 的行，返回 410 `revoked` 给门点；同人其他票不受影响 |
| 撤销、过期、重启不能让票复活 | 所有终态落库；过期由后台清扫（默认 10s）+ 核销时惰性判定两种途径，且过期事件只记一次；无任何 API 可改回 ACTIVE；版本号用 SQLite AUTOINCREMENT，重启后继续递增不复用 |
| 门点离线重连按版本补齐撤销/核销结果 | `events` 表只追加，`id` 即全局版本；`POST /api/gate/sync {"since_version": n}` 按序拉取（每页 200 条，`has_more` 翻页）；门点页面把游标存 localStorage，每 5 秒补齐 |
| 扫码请求网络抖动重发不能重复核销 | 门点为**每次物理扫码**生成新的 `attempt_id`；`(gate_id, attempt_id)` 唯一约束，重放返回首次结果并带 `"replayed": true`。注意：同一张票的**第二次扫码**（新 attempt_id）会得到“已使用” |
| 管理员按人查看可用票、门点记录、状态变化原因 | `GET /api/admin/people/{id}` 返回：当前有效票、全部票（含有效区间/终态时间与原因）、append-only 事件流水（版本+原因+门点）、该人所有票的扫码记录（成功/拒绝） |

### 核销响应约定（门点端可直接据此亮灯/播报）

| 场景 | HTTP | `ok` | `reason` |
|---|---|---|---|
| 核销成功 | 200 | true | — |
| 已被核销（含并发落败方） | 409 | false | `already_redeemed` |
| 未到生效时间 | 403 | false | `not_yet_valid` |
| 已作废 | 410 | false | `revoked`（含 `revoked_reason`） |
| 已过期 | 410 | false | `expired` |
| 票面不存在 | 404 | false | `not_found` |

4xx 是**明确的业务终态结论**，门点端不应把这些请求重试成“待核销”；只有网络错误/5xx 才进离线队列重发（用同一个 `attempt_id`，天然幂等）。

## Docker 部署

```bash
# 1. 设置令牌（也可直接改 docker-compose.yml 的默认值）
export PASSPORT_ADMIN_TOKEN='换成强随机串'
export PASSPORT_GATE_TOKEN='换成另一个强随机串'

# 2. 启动
docker compose up -d --build

# 3. 打开
#    管理台  http://服务器:8080/admin
#    门点台  http://服务器:8080/gate  （每个门点一个浏览器/平板，各自填门点ID）
```

数据在命名卷 `shortpass-data`（容器内 `/data/passport.db`）。备份时停容器后拷贝该卷，或直接用 `docker run --rm -v shortpass-data:/data ... tar` 打包。

不用 Docker 时：

```bash
pip install -r requirements.txt
PASSPORT_ADMIN_TOKEN=xxx PASSPORT_GATE_TOKEN=yyy \
PASSPORT_DB=./passport.db uvicorn app.main:app --host 0.0.0.0 --port 8080
```

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `PASSPORT_ADMIN_TOKEN` | `admin-change-me` | 管理 API/页面用 Bearer 令牌 |
| `PASSPORT_GATE_TOKEN` | `gate-change-me` | 门点 API/页面共用 Bearer 令牌 |
| `PASSPORT_DB` | `./passport.db`（镜像内 `/data/passport.db`） | SQLite 路径 |
| `PASSPORT_SWEEP_INTERVAL` | `10` | 过期清扫周期秒数（核销时另有惰性过期判定） |

> 门点身份是“共享门点令牌 + 预先注册的 `gate_id`”双层：管理员先在管理台注册门点；门点被停用后核销与同步都会返回 410。

## 使用流程

1. 管理台填管理员令牌 →「门点管理」注册各门点（如 `east` 东门、`west` 西门）。
2. 「签发通行票」：填人员标识 + 有效期（分钟），可指定延迟生效时间；票号形如 `T-XXXXXXXX`（Crockford base32，无易混字符），可做成二维码。
3. 门点平板打开 `/gate`，填门点 ID + 门点令牌；扫码枪输入票号回车即可核销，大字号显示放行/拒绝原因。
4. 网络断开时扫码进入本地队列，重连后自动补提交；同时按版本号补齐离线期间的作废/核销/过期事件，页面显示补齐日志。
5. 管理台「按人查看」可看到：当前可用票数、每张票的终态与原因、状态流水版本、各门点扫码成功/拒绝记录。

## API 摘要

管理端（`Authorization: Bearer <PASSPORT_ADMIN_TOKEN>`）

- `POST /api/admin/gates` / `GET /api/admin/gates` / `DELETE /api/admin/gates/{id}`
- `POST /api/admin/tickets`  `{person_id, ttl_seconds}` 或 `{person_id, valid_from, valid_until, note}`
- `POST /api/admin/tickets/revoke` `{code, reason}`
- `GET  /api/admin/tickets/{code}`
- `GET  /api/admin/people?q=` / `GET /api/admin/people/{person_id}`
- `GET  /api/admin/events?since=&code=&person_id=` （状态变化流水，版本即 `events.id`）
- `GET  /api/admin/attempts?gate_id=` （门点扫码记录）
- `GET  /api/admin/stats`

门点端（`Authorization: Bearer <PASSPORT_GATE_TOKEN>`）

- `POST /api/gate/redeem` `{gate_id, code, attempt_id}`
- `POST /api/gate/sync` `{gate_id, since_version}` → `{events:[...], next_since, has_more}`
- `GET  /api/gate/heartbeat/{gate_id}`

## 测试

```bash
pip install pytest httpx
python3 -m pytest tests/ -q
# 11 passed
```

测试启动真实 uvicorn 子进程打真实 HTTP，包含：双门点线程屏障并发核销（连跑多轮验证）、
同人三票独立核销、作废隔离、过期不复活、幂等重放、离线游标补齐、**杀进程重启后终态不复活且版本号延续**、门点停用、鉴权。

## 部署规模与边界说明

- SQLite WAL + 单写者串行事务适合单机到部门级规模（每秒数十次核销、单库几十万票据无压力）；
  多实例水平扩展需换 Postgres（`UPDATE ... WHERE status='ACTIVE' RETURNING` 同样满足并发单赢家语义）。
- 门点令牌是共享密钥模型；若每门点需独立密钥，可在 `gates` 表加 `token_hash` 列扩展。
- 时钟以服务器 UTC 为准；门点本地不裁决时间，只负责展示与重放。
