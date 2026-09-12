# ShortPass · 短时通行票与多门点并发核销

给访客发短时通行票、多个门点同时核销的 Web 系统，支持**分区通行策略**与**紧急封锁**。

- **FastAPI + SQLite(WAL)**，单容器即可部署；数据落盘在 `/data` 卷，重启不丢状态。
- 无外部前端依赖（原生 HTML/JS），自带管理台与门点核销台。
- `tests/` 含 20 个端到端测试（真实 HTTP + 进程重启），覆盖下列全部不变量。

## 需求对应的关键保证

| 需求 | 实现方式 |
|---|---|
| 同一人可持有多张时间重叠的票，按票面编号核销，互不顶替 | 每张票是独立行（`tickets.code` 主键）；核销只按 `code` 定位，不做“同人唯一有效票”逻辑 |
| 两个门点同时扫同一张，恰有一个成功，另一个明确知道已使用 | 核销在单个 `BEGIN IMMEDIATE` 事务中完成，条件更新 `WHERE code=? AND status='ACTIVE'`；失败方返回 HTTP 409 + `reason=already_redeemed`，并告知 `redeemed_gate` / `redeemed_at` |
| 每张票只能成功核销一次 | 状态机单向迁移 `ACTIVE → REDEEMED/REVOKED/EXPIRED`，终态无任何回退路径；核销事件 append-only |
| 作废一张，其他有效期内的票继续可用 | 作废只改对应 `code` 的行，返回 410 `revoked` 给门点；同人其他票不受影响 |
| 撤销、过期、重启不能让票复活 | 所有终态落库；过期由后台清扫（默认 10s）+ 核销时惰性判定两种途径，且过期事件只记一次；无任何 API 可改回 ACTIVE；版本号用 SQLite AUTOINCREMENT，重启后继续递增不复用 |
| 门点离线重连按版本补齐撤销/核销/策略事件 | `events` 表只追加，`id` 即全局版本；`POST /api/gate/sync {"since_version": n}` 按序拉取（每页 200 条，`has_more` 翻页）；封锁规则与票据事件共用同一版本流，门点页面把游标与策略版本存 localStorage，每 5 秒补齐 |
| 扫码请求网络抖动重发不能重复核销 | 门点为**每次物理扫码**生成新的 `attempt_id`；`(gate_id, attempt_id)` 唯一约束，重放返回首次结果并带 `"replayed": true`。注意：同一张票的**第二次扫码**（新 attempt_id）会得到“已使用” |
| 管理员按人查看可用票、门点记录、状态变化原因 | `GET /api/admin/people/{id}` 返回：当前有效票、全部票（含有效区间/终态时间与原因）、append-only 事件流水（版本+原因+门点）、该人所有票的扫码记录（成功/拒绝） |
| 门点按分区通行，票指定允许分区 | `zones` 注册表 + `gates.zone_id` + `tickets.zones`（JSON 数组）；门点分区不在票据允许列表中 → 403 `zone_mismatch`（不消耗票据） |
| 未知分区、缺少策略的门点默认拒绝 | 门点未配置分区 → 403 `gate_no_policy`；分区被注销后引用它的门点 → 403 `unknown_zone`；发票时指定不存在的分区 → 400 |
| 管理台发布带版本号的封锁/解除规则 | `POST /api/admin/policy/rules`（`action=LOCK/UNLOCK`，`zone_id` 为空=全局）；发布即写 `POLICY_LOCK/POLICY_UNLOCK` 事件，版本号即 `events.id`；分区当前状态 = 版本最新的适用规则（本区或全局） |
| 重复发布同一规则不重复生效 | `rule_id` 是 `policy_rules` 主键：同内容重发返回原规则 `duplicated=true`（HTTP 200，不产生新版本/事件）；同 ID 不同内容 → 409 |
| 策略变更与并发核销有明确先后 | 规则发布与核销都走 `BEGIN IMMEDIATE` 写事务串行化：核销事件版本 < 规则版本 ⇒ 按旧策略放行，反之按新策略判定；核销响应与事件都记录判定时的 `policy_version` |
| 门点规则版本过旧要明示 | 核销请求携带 `policy_version`（门点已同步的最新策略版本）；落后于服务器 → 409 `stale_policy` 并附 `current_policy_version`；该结果**不写入**扫码记录，门点按版本补齐后可用同一 `attempt_id` 安全重试 |
| 管理员按分区查看当前规则、受影响票据、门点执行记录 | `GET /api/admin/zones/{id}` 返回：当前规则与全部历史、允许该分区的票据、本分区门点的扫码执行记录；`GET /api/admin/zones` 为总览（封锁状态/门点数/受影响票数） |

### 核销响应约定（门点端可直接据此亮灯/播报）

| 场景 | HTTP | `ok` | `reason` |
|---|---|---|---|
| 核销成功 | 200 | true | — |
| 已被核销（含并发落败方） | 409 | false | `already_redeemed` |
| 门点规则版本过旧 | 409 | false | `stale_policy`（附 `current_policy_version`，**未落库**，同步后同 `attempt_id` 重试） |
| 未到生效时间 | 403 | false | `not_yet_valid` |
| 分区不匹配 | 403 | false | `zone_mismatch`（附门点 `zone_id` 与票据 `ticket_zones`，不消耗票据） |
| 门点未配置分区策略 | 403 | false | `gate_no_policy` |
| 门点分区未知（已注销） | 403 | false | `unknown_zone` |
| 分区紧急封锁中 | 423 | false | `locked`（附 `lock_rule` 规则详情，不消耗票据） |
| 已作废 | 410 | false | `revoked`（含 `revoked_reason`） |
| 已过期 | 410 | false | `expired` |
| 票面不存在 | 404 | false | `not_found` |

4xx 是**明确的业务终态结论**，门点端不应把这些请求重试成“待核销”；只有网络错误/5xx 才进离线队列重发（用同一个 `attempt_id`，天然幂等）。`stale_policy` 是唯一的例外：它不是业务结论，门点补齐策略事件后用同一 `attempt_id` 重试即可。

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

1. 管理台填管理员令牌 →「分区与紧急封锁」创建分区（如 `Z-A` A 区）→「门点管理」注册各门点并在表格里为每个门点选择分区（未配置分区的门点核销默认拒绝）。
2. 「签发通行票」：填人员标识 + 有效期（分钟），勾选允许通行的分区（不勾选 = 所有门点拒绝）；票号形如 `T-XXXXXXXX`（Crockford base32，无易混字符），可做成二维码。
3. 门点平板打开 `/gate`，填门点 ID + 门点令牌；扫码枪输入票号回车即可核销，大字号显示放行/拒绝原因（含分区不符、封锁中、版本过旧等）。
4. 紧急情况下在「分区与紧急封锁」发布规则：选封锁/解除、目标分区（或全局）、填规则 ID（留空自动生成）与原因；发布即产生新版本，重复发布同一规则 ID 不会重复生效。
5. 网络断开时扫码进入本地队列，重连后自动补提交；同时按版本号补齐离线期间的作废/核销/过期/**封锁规则**事件，页面显示补齐日志与当前策略版本。若核销被拒为“规则版本过旧”，门点会自动补齐后用同一次扫码重试。
6. 管理台「按分区查看」可看到：分区当前规则与历史版本、受影响票据、本分区门点的执行记录；「按人查看」可看到：当前可用票数、每张票的终态与原因、状态流水版本、各门点扫码成功/拒绝记录。

## API 摘要

管理端（`Authorization: Bearer <PASSPORT_ADMIN_TOKEN>`）

- `POST /api/admin/gates` / `GET /api/admin/gates` / `DELETE /api/admin/gates/{id}`
- `PUT  /api/admin/gates/{id}/zone` `{zone_id}`（null = 清除分区，门点回到默认拒绝）
- `POST /api/admin/zones` / `GET /api/admin/zones` / `GET /api/admin/zones/{id}` / `DELETE /api/admin/zones/{id}`
- `POST /api/admin/policy/rules` `{action: "LOCK"|"UNLOCK", zone_id?, rule_id?, reason?}` → 201 新规则 / 200 `duplicated`（重复发布不生效）/ 409（同 ID 不同内容）
- `GET  /api/admin/policy/rules?zone_id=`（规则列表 + 当前策略版本）
- `POST /api/admin/tickets`  `{person_id, ttl_seconds, zones}` 或 `{person_id, valid_from, valid_until, note, zones}`
- `POST /api/admin/tickets/revoke` `{code, reason}`
- `GET  /api/admin/tickets/{code}`
- `GET  /api/admin/people?q=` / `GET /api/admin/people/{person_id}`
- `GET  /api/admin/events?since=&code=&person_id=` （状态变化流水，版本即 `events.id`，含 `POLICY_LOCK`/`POLICY_UNLOCK`）
- `GET  /api/admin/attempts?gate_id=` （门点扫码记录）
- `GET  /api/admin/stats`

门点端（`Authorization: Bearer <PASSPORT_GATE_TOKEN>`）

- `POST /api/gate/redeem` `{gate_id, code, attempt_id, policy_version}`
- `POST /api/gate/sync` `{gate_id, since_version}` → `{events:[...], next_since, has_more}`（含封锁策略事件）
- `GET  /api/gate/heartbeat/{gate_id}`

## 测试

```bash
pip install pytest httpx
python3 -m pytest tests/ -q
# 20 passed
```

测试启动真实 uvicorn 子进程打真实 HTTP，包含：双门点线程屏障并发核销（连跑多轮验证）、
同人三票独立核销、作废隔离、过期不复活、幂等重放、离线游标补齐、**杀进程重启后终态不复活且版本号延续**、门点停用、鉴权，
以及分区与封锁：分区不匹配不消耗票据、缺少策略/未知分区默认拒绝、封锁-版本过旧-解锁全流程、
全局封锁、规则幂等重发与冲突、策略事件离线补齐、**封锁发布与并发核销的先后序验证**、按分区的管理视图。

> 注意：`tests/test_zones.py` 依赖在 `test_system.py` 之后运行（pytest 默认按文件名字序），
> 因为 test_system 的用例假定“尚未发布任何封锁规则”（门点策略版本为 0）。

## 升级说明（老库迁移）

启动时自动就地迁移：`gates` 加 `zone_id` 列、`tickets` 加 `zones` 列（默认 `[]`）、
重建 `events` 表以扩展事件类型 CHECK（事件与版本号完整保留、继续递增）。
**注意**：升级前已存在的票 `zones` 为空数组，按“未授权任何分区”处理，在所有门点都会被
`zone_mismatch` 拒绝；需要的话请作废旧票并按分区重新签发。

## 部署规模与边界说明

- SQLite WAL + 单写者串行事务适合单机到部门级规模（每秒数十次核销、单库几十万票据无压力）；
  多实例水平扩展需换 Postgres（`UPDATE ... WHERE status='ACTIVE' RETURNING` 同样满足并发单赢家语义）。
- 门点令牌是共享密钥模型；若每门点需独立密钥，可在 `gates` 表加 `token_hash` 列扩展。
- 时钟以服务器 UTC 为准；门点本地不裁决时间，只负责展示与重放。
