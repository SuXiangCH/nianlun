# 年轮企业微信渠道实现计划

> **执行方式：** 按任务顺序在当前工作区实现；每个任务先写失败测试，再实现，再运行定向验证。除用户明确要求外不创建提交。

**Goal:** 在 nianlun 落地企业微信智能机器人渠道：平台侧配置/发布/启停/状态/会话管理，独立 Python 长连接网关收发流式回答，支持 local 与 Redis 所有权。

**Architecture:** 平台仍为单进程 FastAPI + SQLite；新增 `application_wecom_channels` 与 bot 级 revision 分配器，复用不可变 release 和 Agent 运行时。网关是独立进程 `app/channel_gateway`，通过带 token 的内部 HTTP 拉快照、报状态、调用 NDJSON 运行接口；Redis 只用于网关多实例所有权。

**Tech Stack:** Python 3.11、FastAPI、SQLAlchemy/SQLite、httpx、redis.asyncio、官方 `wecom-aibot-python-sdk`、React/TypeScript/原生 CSS。

**Spec:** `docs/architecture/wecom_channel_design.md`

## Global Constraints

- 平台 API envelope、`X-Request-Id`、现有 API 渠道字段和公开运行时保持兼容。
- `secret`、API key、内部 token 不进入响应、日志、异常、指标或测试快照；管理接口只返回 `has_secret`。
- 原始企微 `userid/chatid/msgid` 不进入平台数据库；平台只保存 `user_key`、哈希幂等键和平台消息 UUID。
- 消息正文不写日志/指标；按现有会话记录策略落库。
- 平台 SQLite 单写者；所有渠道写操作在 `ApplicationReleaseService.entry_lock(app_id)` 内。
- 数据库变更必须通过 `database/migrations.py` 的幂等向前迁移，`SCHEMA_VERSION` 12 → 13。
- 网关不导入 `app.api_server` 或平台 ORM；SDK 通过适配层隔离。
- 网关 local 模式只允许一个进程连接同一 Bot；多实例必须 Redis。
- 输入 64 KiB、回答 1 MiB、企微单帧 20 KiB，均按 UTF-8 字节截断。
- 验证命令：`uv run pytest <path>`；最终 `make lint`、`make typecheck`、前端 `npm test/typecheck/build`。
- 不修改与任务无关的既有文件；不删除 `data/`、数据库或用户内容。

---

## File Structure

平台：

```text
app/api_server/database/models/publishing.py        # WeCom 渠道与 bot revision 模型
app/api_server/database/models/chat.py              # conversations channel 约束
app/api_server/database/migrations.py               # v13 迁移
app/api_server/repositories/wecom_channels.py       # 渠道 CRUD/快照/状态/revision 分配
app/api_server/repositories/application_runs.py     # 渠道感知 admit/checkpoint
app/api_server/repositories/publishing.py           # 部署事件按渠道过滤
app/api_server/services/application_wecom_service.py
app/api_server/services/application_invocation_service.py
app/api_server/services/gateway_control.py
app/api_server/apis/v1/publishing_schemas.py
app/api_server/apis/v1/apps/publishing.py
app/api_server/apis/v1/chat/api.py
app/api_server/apis/internal/wecom.py
app/api_server/apis/internal/router.py
app/api_server/config/settings.py
app/api_server/services/container.py
app/api_server/main.py
```

网关：

```text
app/channel_gateway/__init__.py
app/channel_gateway/__main__.py
app/channel_gateway/config.py
app/channel_gateway/protocol.py
app/channel_gateway/metrics.py
app/channel_gateway/message.py
app/channel_gateway/wecom_adapter.py
app/channel_gateway/ownership.py
app/channel_gateway/redis_scripts.py
app/channel_gateway/redis_ownership.py
app/channel_gateway/clients.py
app/channel_gateway/manager.py
app/channel_gateway/server.py
```

测试：

```text
tests/api_server/test_wecom_channel.py
tests/api_server/test_wecom_internal.py
tests/api_server/test_wecom_runtime.py
tests/api_server/test_orm_persistence.py        # 追加迁移测试
tests/channel_gateway/test_message.py
tests/channel_gateway/test_local_ownership.py
tests/channel_gateway/test_redis_ownership.py
tests/channel_gateway/test_clients.py
tests/channel_gateway/test_manager.py
tests/channel_gateway/test_adapter.py
tests/channel_gateway/test_server.py
```

前端：

```text
app/frontend/src/types.ts
app/frontend/src/api/client.ts
app/frontend/src/features/applications/ApplicationDetail.tsx
app/frontend/src/features/applications/ApplicationPublishPanel.tsx
app/frontend/src/features/applications/ApplicationWecomChannel.tsx
app/frontend/src/features/applications/ApplicationConversations.tsx
app/frontend/src/features/applications/ApplicationWecomChannel.test.tsx
app/frontend/src/styles.css（或现有全局样式文件）
```

---

### Task 1: Schema, Migration and WeCom Repository

**Files:**
- Modify: `app/api_server/database/models/publishing.py`
- Modify: `app/api_server/database/models/chat.py`
- Modify: `app/api_server/database/models/__init__.py`
- Modify: `app/api_server/database/migrations.py`
- Create: `app/api_server/repositories/wecom_channels.py`
- Modify: `app/api_server/repositories/publishing.py`
- Modify: `app/api_server/repositories/application_runs.py`
- Modify: `app/api_server/repositories/sqlite_chat.py`
- Test: `tests/api_server/test_orm_persistence.py`, `tests/api_server/test_wecom_channel.py`

**Interfaces (produced):**

```python
class ApplicationWecomChannel(Base): ...


class ApplicationWecomBotRevision(Base): ...


class WecomChannelRepository:
    def create(
        self,
        app_id: str,
        *,
        bot_id: str,
        secret: str,
        default_response: str,
        expires_at: str | None,
    ) -> dict[str, Any]: ...
    def get(self, app_id: str) -> dict[str, Any] | None: ...
    def by_channel_id(self, channel_id: str) -> dict[str, Any] | None: ...
    def update(
        self, app_id: str, values: dict[str, Any], *, expected_config_revision: int
    ) -> dict[str, Any]: ...
    def set_enabled(
        self, app_id: str, enabled: bool, expected_deployment_version: int
    ) -> dict[str, Any]: ...
    def deploy(
        self,
        app_id: str,
        release_id: str,
        *,
        expected_deployment_version: int,
        content_version: int,
    ) -> dict[str, Any]: ...
    def snapshot(self) -> list[dict[str, Any]]: ...
    def report_status(
        self, channel_id: str, revision: int, status: str, error_code: str | None
    ) -> bool: ...
    def last_deployed_at(self, app_id: str) -> datetime | None: ...
```

- `_next_revision(session, bot_id, *, after=0) -> int` executes on `ApplicationWecomBotRevision`; allocates `max(last_revision, after) + 1`, with a missing counter starting at 0. Existing channels pass their current revision as `after`, including bot rebindings, so stale requests cannot match a reused channel revision.
- `create` and `update` normalize `expires_at` via `datetime.fromisoformat(...).astimezone(timezone.utc).isoformat()`.
- `deploy` sets `enabled=True` and allocates a new revision only when `active_release_id is None`; otherwise revision unchanged; always `deployment_version += 1` and writes a `channel_type='wecom'` event.
- `set_enabled` always allocates revision and `deployment_version += 1`; writes enable/disable event.
- `report_status` is one write transaction: compare `config_revision`, reject expired channels unless `status == 'offline'`, clear/require error code, update `last_reported_at` only on acceptance.

- [ ] **Step 1: Write failing schema/migration tests**

在 `tests/api_server/test_orm_persistence.py` 追加：

```python
def test_wecom_channel_schema_and_constraints(tmp_path):
    factory = _factory(tmp_path)
    _seed_application(factory)
    now = datetime.now(timezone.utc).isoformat()
    with factory.session_scope(write=True) as session:
        session.add(
            ApplicationWecomChannel(
                id="wc-1",
                application_id="app-1",
                bot_id="bot-1",
                secret="s",
                default_response="fallback",
                enabled=False,
                active_release_id=None,
                deployment_version=0,
                config_revision=1,
                status="offline",
                error_code=None,
                expires_at=None,
                last_reported_at=None,
                created_at=now,
                updated_at=now,
            )
        )
    with factory.session_scope() as session:
        assert session.get(ApplicationWecomChannel, "wc-1").config_revision == 1
```

并断言 `SCHEMA_VERSION == 13`、表集合包含两个新表、`conversations` 表 SQL 含 `'wecom'`、`application_deployment_events` 表 SQL 的唯一键含 `channel_type`。再构造 v12 旧库（复用现有 `_seed_application` + 手工建旧表或调用旧版 SQL），执行两次 `initialize_database`，断言数据保留、约束正确。

- [ ] **Step 2: Run tests, expect failure**

Run: `uv run pytest tests/api_server/test_orm_persistence.py -k wecom -v`
Expected: FAIL（模型/版本不存在）。

- [ ] **Step 3: Implement models and migration**

`publishing.py` 增加：

```python
class ApplicationWecomChannel(Base):
    __tablename__ = "application_wecom_channels"
    __table_args__ = (
        UniqueConstraint("application_id", name="uq_application_wecom_application"),
        UniqueConstraint("bot_id", name="uq_application_wecom_bot"),
        CheckConstraint(
            "status IN ('offline','online','error')", name="ck_wecom_channel_status"
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code IN "
            "('AUTH_FAILED','NETWORK_ERROR','CONNECTION_CONFLICT')",
            name="ck_wecom_channel_error_code",
        ),
        CheckConstraint(
            "status != 'error' OR error_code IS NOT NULL",
            name="ck_wecom_channel_error_required",
        ),
        CheckConstraint(
            "status = 'error' OR error_code IS NULL",
            name="ck_wecom_channel_error_forbidden",
        ),
        CheckConstraint(
            "config_revision >= 1 AND deployment_version >= 0",
            name="ck_wecom_channel_versions",
        ),
    )
    # columns exactly as design 5.1
```

`ApplicationWecomBotRevision`：`bot_id` PK、`last_revision`、`updated_at`。

`chat.py`：channel check 改为 `('console','api','wecom')`；owner check 增加 wecom 分支，保留约束名 `ck_conversations_api_owner`；新增 `Index("idx_conversations_channel_peer", "application_id", "channel", "external_user_id", "status")`。

`ApplicationDeploymentEvent`：唯一约束改为 `("application_id","channel_type","deployment_version")`，增加 `channel_type IN ('api','wecom')` check。

`migrations.py`：
- `SCHEMA_VERSION = 13`。
- 新增 `_rebuild_conversations_wecom_constraints`：读取 `sqlite_master` 表 SQL，若已含 `wecom` 直接返回；否则用 `source.to_metadata(MetaData(), name="conversations_v13")` 编译建表、复制列、删旧表、改名、重建依赖索引。
- 新增 `_rebuild_deployment_event_channel_unique`：若表 SQL 已含 `channel_type, deployment_version` 唯一键则返回；否则同样用 ORM clone 重建，复制全部列。
- 在 `initialize_database` 中 `_migrate_application_api` 之后依次调用两个新 helper。
- 注意 `Base.metadata.create_all` 会创建新表；旧表重建必须在 `PRAGMA foreign_keys=OFF` 事务内。

- [ ] **Step 4: Write failing repository tests**

`tests/api_server/test_wecom_channel.py` 使用临时 SQLite，断言：
- 创建后 `config_revision == 1`；更新 secret/default_response 后为 2；删除渠道行后用同一 bot_id 重建得到 3（同事务保留 bot_revisions）。
- `set_enabled` 分配新 revision 且写 `channel_type='wecom'` 事件。
- 首次 `deploy` 自动启用并分配 revision；第二次 deploy 不改变 revision、`deployment_version` 递增。
- 重复 bot_id 返回 409。
- `snapshot()` 只含 enabled 且有 active_release 的渠道，包含 secret 和过期时间。
- `report_status` 旧 revision/过期渠道 online 被忽略；online 接受后 `last_reported_at` 更新。

- [ ] **Step 5: Implement `WecomChannelRepository` and adjust other repositories**

- `publishing.py`：`live_release_ids` 会话查询改 `channel.in_(("api","wecom"))`，并加入启用 WeCom 渠道的 `active_release_id`；`assert_application_deletable` 检查 WeCom enabled / 未过期 WeCom 会话；`deployment_events(..., channel_type="api")`；`last_deployed_at(app_id, channel_type="api")`；`_deployment_event` 接受 `channel_type` 和显式 `deployment_version`。
- `application_runs.py`：新增 `admit_channel(...)`（见 Task 3），`conversation(channel=...)` 支持 wecom，`interrupt_context` 不再限定 api，`checkpoint_cleanup_candidates` / `conversation_ids_for_application` 返回 `(conversation_id, channel)` 并覆盖两渠道，`recover(checkpoint_missing=True)` 覆盖两渠道。
- `sqlite_chat.py`：`list_conversations(app_id, channel="console")` 过滤并返回 `channel`、`external_user_id`；`get_messages`、`get_source` 去掉 console 限定但仍校验 app 归属；`delete_conversation` 保持 console-only。

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/api_server/test_orm_persistence.py tests/api_server/test_wecom_channel.py -v`
Expected: PASS。

---

### Task 2: WeCom Management API, Publication and Gateway Control

**Files:**
- Modify: `app/api_server/apis/v1/publishing_schemas.py`
- Create: `app/api_server/services/application_wecom_service.py`
- Create: `app/api_server/services/gateway_control.py`
- Modify: `app/api_server/apis/v1/apps/publishing.py`
- Modify: `app/api_server/config/settings.py`
- Modify: `app/api_server/services/container.py`
- Test: `tests/api_server/test_wecom_channel.py`

**Interfaces (produced):**

```python
class WecomChannelState(ApiSchema): ...
class WecomChannelCreateRequest(ApiSchema): ...
class WecomChannelUpdateRequest(ApiSchema): ...
PublicationChannelCreateRequest = Annotated[
    ApiChannelCreateRequest | WecomChannelCreateRequest,
    Field(discriminator="type"),
]

class GatewayControlClient:
    def refresh(self) -> None: ...
    def connect(self, channel_id: str, revision: int) -> dict[str, Any]: ...

class ApplicationWecomService:
    def create(self, app_id: str, request: WecomChannelCreateRequest) -> PublicationResponse
    def update(self, app_id: str, request: WecomChannelUpdateRequest) -> PublicationResponse
    def set_enabled(self, app_id: str, request: PublicationUpdateRequest) -> PublicationResponse
    def deploy(self, app_id: str, request: ApplicationDeployRequest) -> PublicationResponse
    def connect(self, app_id: str) -> dict[str, Any]
    def attach(self, publication: PublicationResponse) -> PublicationResponse
    def snapshot(self) -> dict[str, Any]
    def report_status(self, channel_id: str, request: WecomStatusRequest) -> bool
```

- `ApplicationSettings` 增加 `internal_token`、`gateway_control_urls`、`gateway_control_token`、`gateway_control_timeout_seconds`、`wecom_max_concurrent`、`wecom_run_timeout_seconds`、`wecom_conversation_retention_days`，并在 `__post_init__` 校验。
- `create` 要求 `settings.internal_token` 已配置，否则 503。
- 所有写方法在 `publishing.entry_lock(app_id)` 下；成功写后调用 `gateway_control.refresh()`（best-effort，异常只日志）。
- `attach` 读取渠道记录、active release、last deploy event，计算 `has_unpublished_changes`、`dependencies_ready`、`dependency_error`，返回 `publication.model_copy(update={"wecom_channel": state})`；无渠道返回原对象。
- `connect` 未配置 control URL → 503；渠道停用/未部署/过期 → 409；调用 `GatewayControlClient.connect`，全部失败 502。

- [ ] **Step 1: Write failing API tests**

覆盖：
- 创建 wecom 渠道（未配置 internal token → 503；配置后 200，响应无 secret）。
- `PUT` 修改 default_response 递增 revision；secret 省略保持原值；显式 null 清除 expires_at。
- `PATCH` 启停需要 active release；首次 deploy 自动启用；`POST /deployments` `channel_type='wecom'` 只改 WeCom active release，API 发布字段不变。
- `GET /publication` 同时含 `wecom_channel`，API 字段兼容。
- 部署事件 `?channel_type=wecom` 只返回 WeCom；默认仍 API。
- 过期渠道 deploy/enable/connect 返回 409。
- `GET /snapshot`（内部）只返回 enabled+active release，含 secret；管理响应不含 secret。

- [ ] **Step 2: Implement schemas, settings and service**

Discriminated union 使用 `type: Literal["api"]` / `Literal["wecom"]`。更新路由：

```python
@router.post("/{application_id}/channels")
def create_channel(application_id, body, request):
    if body.type == "api":
        publication = _service(request).create_api_channel(application_id)
    else:
        publication = _wecom(request).create(application_id, body)
    return success(_attach(request, publication).model_dump(mode="json"))
```

`_attach` 在 `services.wecom is None` 时原样返回。`GET /publication`、`PATCH /publication`、`POST /publish`、`POST /deployments` 都经 `_attach`。`deploy` 按 `channel_type` 分派。

`GatewayControlClient` 使用 `httpx.Client`，请求头 `Authorization: Bearer <token>`，refresh 超时 `gateway_control_timeout_seconds`，connect 固定 12 秒，`redirect` 不允许；返回首个成功实例的 JSON。

- [ ] **Step 3: Wire container and router**

`ApiServices.wecom`；`build_services` 构造 `WecomChannelRepository`、`GatewayControlClient`、`ApplicationWecomService`；`main.py` 无需改动（Task 3 加内部路由）。

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/api_server/test_wecom_channel.py -v`
Expected: PASS。

---

### Task 3: Internal API, Channel Runtime and Checkpoint Wiring

**Files:**
- Create: `app/api_server/apis/internal/__init__.py`
- Create: `app/api_server/apis/internal/wecom.py`
- Create: `app/api_server/apis/internal/router.py`
- Modify: `app/api_server/main.py`
- Modify: `app/api_server/services/application_invocation_service.py`
- Modify: `app/api_server/repositories/application_runs.py`
- Modify: `app/api_server/repositories/sqlite_chat.py`（完成态续期）
- Modify: `app/api_server/services/container.py`
- Test: `tests/api_server/test_wecom_internal.py`, `tests/api_server/test_wecom_runtime.py`

**Interfaces (produced):**

```python
def checkpoint_thread_id(channel: str, conversation_id: str) -> str:
    if channel not in {"api", "wecom"}:
        raise ValueError("unsupported conversation channel")
    return f"{channel}:{conversation_id}"


@dataclass
class PreparedInvocation:
    row: dict[str, Any]
    future: Future[PublicChatResponse] | None = None
    cancellation: threading.Event | None = None
    started: float = 0.0
    replay: PublicChatResponse | None = None
    deltas: queue.Queue[str] | None = None
    channel: str = "api"
    timeout_seconds: float = 300.0
    retention_days: int = 30
    duplicate: bool = False


class ApplicationInvocationService:
    def prepare_channel(
        self,
        channel_id: str,
        revision: int,
        user_key: str,
        text: str,
        key: str,
        request_id: str | None,
        started: float,
    ) -> PreparedInvocation: ...
    async def stream_channel(
        self, prepared: PreparedInvocation
    ) -> AsyncIterator[dict[str, Any]]: ...
```

- 路由 `/runs` 在开始流之前完成 `prepare_channel`；`duplicate=True` 返回 204；其余返回 `StreamingResponse(media_type="application/x-ndjson")`。
- 首个事件 `{"type":"start","run_id","conversation_id","release_id"}`；随后增量 `{"type":"chunk","content"}`；结束 `{"type":"done","content","run_id"}` 或 `{"type":"error","code","message"}`。
- `_execute` 的线程 ID 使用 `checkpoint_thread_id(prepared.channel, ...)`；`complete(..., retention_days=prepared.retention_days)`；`wait` 使用 `prepared.timeout_seconds`。
- `_active` 记录 `(app_id, client_id, channel, prepared)`；API 限额只统计 `channel == "api"`，WeCom 只检查全局 `wecom_max_concurrent`；executor `max_workers = api_max_concurrent + wecom_max_concurrent`。
- checkpointer 初始化条件改为 `application_api_enabled or internal_token is not None`。

- [ ] **Step 1: Write failing internal auth and snapshot/status tests**

断言：未配置 token → 503；错误 token → 401；正确 token → snapshot/status；status 旧 revision → 204 且不更新；过期渠道 online → 204 且不更新；响应 `Cache-Control: no-store`。

- [ ] **Step 2: Implement internal router and auth dependency**

```python
def _authorize(request: Request, authorization: str | None) -> None:
    expected = request.app.state.settings.internal_token
    if not expected:
        raise ApiError("内部接口未启用", 503)
    provided = authorization or ""
    if not hmac.compare_digest(
        hashlib.sha256(expected.encode()).hexdigest(),
        hashlib.sha256(provided.encode()).hexdigest(),
    ):
        raise ApiError("内部凭据无效", 401)
```

- [ ] **Step 3: Write failing runtime tests**

使用 fake runtime（同 `test_public_runtime.py` 模式），覆盖：
- 首次消息返回 start/chunk/done，会话 `channel='wecom'`、`external_user_id=user_key`、run `client_id='wecom:<id>'`。
- 同一 user_key 复用会话；群聊同 user_key 共享。
- 重复幂等键 completed → 204；running → 409；failed → 409；指纹不同 → 409。
- 停用/过期/revision 不匹配 → 403/409/404。
- checkpoint 使用 `wecom:`；完成后续期使用 WeCom retention。
- API 运行仍使用 `api:` 且不消耗 WeCom 限额。
- 客户端断开取消 run。

- [ ] **Step 4: Implement `admit_channel` and runtime methods**

`admit_channel` 在单事务内：
- 校验渠道存在、enabled、未过期、`config_revision == revision`；新会话要求 `active_release_id == release_id`。
- 已有会话校验 `channel='wecom'`、`application_id`、`external_user_id=user_key`、`release_id`、context ready、未过期、无活跃 run。
- 新会话插入 `Conversation(channel='wecom', api_client_id=None, external_user_id=user_key, release_id=..., expires_at=now+retention)`。
- `begin_turn(channel='wecom')`，插入 run，`client_id=f"wecom:{channel_id}"`，`idempotency_key=key`，返回 dict。
- 唯一约束冲突转换为 409。

`prepare_channel`：
- 幂等 replay 在 entry lock 与 guard 内先查；completed 返回 `duplicate=True`；其他终态抛 409 安全文案。
- 会话解析：最新 active 会话；若 context 非 ready/过期/checkpoint 缺失/release 不可用 → `interrupt_context` 并归档，再新建。
- release 校验与 runtime 构建复用 API 路径；构建后再次检查剩余时间，超时 504 且不 admit。
- `text` 去空白后非空、UTF-8 ≤ 64 KiB；key 长度 ≤ 128。
- `started` 由路由传入，超时预算 `settings.wecom_run_timeout_seconds`。

`stream_channel`：
- `_execute` 在 `finally` 向队列放入哨兵；生成器先发 start，循环读取增量；收到哨兵后 `await future`，成功发 done，失败发 error（安全文案）。
- 生成器 `finally`：future 未完成则 `cancel(prepared)`。

- [ ] **Step 5: Wire route and main**

`main.py` `app.include_router(internal_router)`；内部路由不进入 OpenAPI（`include_in_schema=False`）；所有响应设置 `Cache-Control: no-store`。

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/api_server/test_wecom_internal.py tests/api_server/test_wecom_runtime.py -v`
Expected: PASS。

---

### Task 4: Gateway Protocol, Message Normalization and Local Ownership

**Files:**
- Create: `app/channel_gateway/__init__.py`
- Create: `app/channel_gateway/protocol.py`
- Create: `app/channel_gateway/message.py`
- Create: `app/channel_gateway/ownership.py`
- Create: `app/channel_gateway/metrics.py`
- Test: `tests/channel_gateway/test_message.py`, `tests/channel_gateway/test_local_ownership.py`

**Interfaces (produced):**

```python
@dataclass(frozen=True)
class NormalizedMessage:
    channel_id: str
    event_id: str
    idempotency_key: str
    req_id: str
    conversation_key: str
    user_key: str
    text: str


class MessageNotice(Exception):
    def __init__(self, code: str) -> None: ...


def normalize_message(
    channel_id: str, bot_id: str, frame: dict[str, Any], max_bytes: int
) -> NormalizedMessage: ...
def truncate_reply(content: str, max_bytes: int) -> str: ...
def sha256_text(value: str) -> str: ...


@dataclass
class Lease:
    bot_id: str
    channel_id: str
    revision: int
    recovery_version: int
    token: str
    generation: int
    safe_until: float


class LocalOwnershipManager:
    def acquire(self, input: AcquireInput) -> Lease | None: ...
    def renew(self, lease: Lease) -> bool: ...
    def release(self, lease: Lease) -> None: ...
    def block(self, lease: Lease) -> bool: ...
    def recover(self, input: AcquireInput) -> bool: ...
    def has_recovered(self, lease: Lease) -> bool: ...
    def publish_status(
        self, lease: Lease, status: str, error_code: str | None
    ) -> StatusState | None: ...
    def read_status(self, input: AcquireInput) -> StatusState | None: ...
    def is_valid(self, lease: Lease) -> bool: ...
    def close(self) -> None: ...
```

- `normalize_message` 只接受 `cmd == 'aibot_msg_callback'`、`aibotid == bot_id`、非空 `req_id/msgid/userid`、`chattype ∈ {single,group}`、group 有 `chatid`、`msgtype='text'`、`text.content` 去空白非空且 ≤ max_bytes。
- `user_key = "wc_" + sha256(channel_id + "|" + conversation_key)[:32]`。
- `event_id = sha256(channel_id + "|" + msgid)`（hex）；`idempotency_key = "wcm_" + event_id`。
- `truncate_reply` 按 Unicode 码点累加 UTF-8 字节，不切断字符。

- [ ] **Step 1: Write failing message tests**

覆盖单聊/群聊、bot 不匹配、缺 chatid/msgid/req_id、非文本 notice、空文本、64 KiB 边界、非 ASCII event_id 稳定性、20 KiB 截断不产生半个字符。

- [ ] **Step 2: Implement `protocol.py` / `message.py` / `metrics.py`**

`protocol.py` 定义快照/状态/NDJSON Pydantic 模型、`GatewayError(code, message, status_code=502, retryable=False, reply_rejected=False)`、常量。`metrics.py` 用线程安全 `Counter` 暴露 `snapshot()`。

- [ ] **Step 3: Write failing local ownership tests**

覆盖：generation 单调；revision 更高的 acquire 清除 conflict；同 revision 冲突未推进时 acquire 返回 None；过期 revision 拒绝；renew 校验 token/binding/generation/recovery；release 后其他实例可 acquire；block/recover；expiredRevision 持久且只允许更高 revision；status 在租约有效且 TTL > safety 时可读写；终态 `CHANNEL_EXPIRED/CONNECTION_CONFLICT/GATEWAY_STOPPED` 即使无 owner 也可读。

- [ ] **Step 4: Implement `LocalOwnershipManager`**

内部状态按 bot_id：owner token/generation/safe_until、binding channel/revision、conflict、recovery_version、expired_revision、status。所有 mutating 操作在 `threading.Lock` 下；时间使用 `time.monotonic()`。语义与 Redis 脚本一致：acquire 先处理 binding/revision/expired，再检查 owner/conflict；renew 失败返回 False；publish_status 只在有效租约内写，`CHANNEL_EXPIRED` 同时推进 expired_revision；read_status 对终态无 owner 也返回。

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/channel_gateway/test_message.py tests/channel_gateway/test_local_ownership.py -v`
Expected: PASS。

---

### Task 5: Redis Ownership and Platform Clients

**Files:**
- Create: `app/channel_gateway/redis_scripts.py`
- Create: `app/channel_gateway/redis_ownership.py`
- Create: `app/channel_gateway/clients.py`
- Test: `tests/channel_gateway/test_redis_ownership.py`, `tests/channel_gateway/test_clients.py`

**Interfaces (produced):**

```python
class RedisOwnershipManager:
    def __init__(
        self,
        redis: Any,
        *,
        key_prefix: str,
        ttl_ms: int,
        safety_ms: int,
        instance_id: str,
    ) -> None: ...

    # same protocol as LocalOwnershipManager


class ConfigClient:
    async def load(
        self, signal: asyncio.Event | None = None
    ) -> list[ChannelConfig]: ...


class StatusClient:
    async def publish(
        self, channel_id: str, revision: int, status: str, error_code: str | None
    ) -> None: ...


class WorkflowClient:
    async def run(
        self,
        *,
        channel_id: str,
        revision: int,
        user_key: str,
        text: str,
        idempotency_key: str,
        signal: asyncio.Event,
    ) -> AsyncIterator[dict[str, Any]]: ...
```

- Redis key：`{prefix}:{{{sha256(bot_id)}}}:owner|generation|conflict|binding|recovery-version|recovery-requests|status`。
- 七个 Lua 脚本从 TS `src/ownership/redis.ts` 逐字移植；`acquire` 返回 `-1..-4` 映射为异常/None，成功返回 `[generation, recoveryVersion]`。
- Python 侧在 `PUBLISH_STATUS`/`READ_STATUS` 后按 `time.monotonic()` 再校验 `ttl - safety`；`RENEW` 失败后清零本地 `safe_until`。
- `WorkflowClient` 使用 `httpx.AsyncClient.stream`，手动按 `\n` 切行、trim、跳过空行、EOF 处理尾行；单行上限 `max_answer_bytes * 6 + 16384`；204 → 单个 `{"type":"duplicate"}`；非 2xx 读 ≤64 KiB 并映射 `GatewayError`；`STREAM_INTERRUPTED` retryable。

- [ ] **Step 1: Write failing client tests**

使用 `httpx.MockTransport` 覆盖：204、start/chunk/done、error 事件、分块 UTF-8、超大行、空行、尾行、非 NDJSON content-type、非 2xx envelope、请求体上限和 `Idempotency-Key` 头。

- [ ] **Step 2: Write failing Redis tests**

优先使用 `fakeredis`（若项目未引入则用本地 fake 实现脚本语义，测试跳过真实 Redis）；`REDIS_TEST_URL` 存在时跑真实 Redis 集成测试。覆盖 acquire/renew/block/recover/expiredRevision/status 读写。

- [ ] **Step 3: Implement scripts, managers and clients**

- 脚本常量 `ACQUIRE_SCRIPT` 等；`register_script` 或 `eval`。
- 所有 key 通过 `_keys(bot_id)` 生成，确保 hash tag。
- `ConfigClient` 限制响应 1 MiB，校验 `channels` 列表、channel_id/bot_id 唯一；网络错误抛 `GatewayError("CONFIG_UNAVAILABLE")`。
- `StatusClient` 204 视为成功，其他 2xx 也成功；4xx/5xx 抛错，由上报器决定重试。
- `WorkflowClient` 请求体 `{channel_id, revision, message:{user_key,text}}`，头 `Authorization`、`Content-Type`、`Accept: application/x-ndjson`、`Idempotency-Key`。

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/channel_gateway/test_clients.py tests/channel_gateway/test_redis_ownership.py -v`
Expected: PASS（Redis 集成在无 `REDIS_TEST_URL` 时 skip）。

---

### Task 6: WeCom Adapter, Channel Manager and Control Server

**Files:**
- Create: `app/channel_gateway/wecom_adapter.py`
- Create: `app/channel_gateway/manager.py`
- Create: `app/channel_gateway/server.py`
- Create: `app/channel_gateway/config.py`
- Create: `app/channel_gateway/__main__.py`
- Test: `tests/channel_gateway/test_adapter.py`, `tests/channel_gateway/test_manager.py`, `tests/channel_gateway/test_server.py`

**Interfaces (produced):**

```python
class AdapterCallbacks(Protocol):
    def on_ready(self) -> None: ...
    def on_close(self) -> None: ...
    def on_error(self, kind: str) -> None: ...
    def on_message(self, frame: dict[str, Any]) -> None: ...


class WecomAdapter:
    def __init__(
        self,
        *,
        bot_id: str,
        secret: str,
        callbacks: AdapterCallbacks,
        sdk_factory: Any | None = None,
    ) -> None: ...
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def reply(
        self, *, req_id: str, stream_id: str, content: str, finish: bool
    ) -> None: ...


class ChannelManager:
    def __init__(
        self,
        settings,
        config: ConfigClient,
        workflow: WorkflowClient,
        status: StatusClient,
        ownership: OwnershipManager,
        adapter_factory: Callable[..., WecomAdapter],
    ) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def request_refresh(self) -> bool: ...
    async def ensure_connected(
        self, channel_id: str, revision: int, request_id: str
    ) -> dict[str, Any]: ...
    def status(self) -> dict[str, Any]: ...
```

**Manager behavior:** 按设计 6.3–6.7 实现：
- 快照定时拉取 + 单飞；diff 新增/revision 变化/删除；非法快照保留旧状态；快照失败保留连接。
- 到期定时器：本地标记 expired、取消在途、`publish_status(CHANNEL_EXPIRED)`、release、不重连。
- 连接：acquire → adapter.connect → `authenticated` online；15 秒连接定时检查（网关侧，SDK 异步不抛异常）。
- 认证失败保持租约 60 秒重试；网络错误 1s→30s 指数退避；冲突 block + error + release + 停自动重试；续租失败 release/remove。
- 去重：in-flight/pending/completed 10k/5min；busy 回复；全局并发；`activeTasks` 计数。
- 回复：`start` 占位、chunk 300ms 节流、15s 保活、done 权威、20 KiB 截断继续读流、1 MiB 上限取消、首事件 30s 超时、失败按规则兜底、最多 3 次重试（同一幂等键、新 requestId）。
- 状态上报：在线每 30 秒心跳；只在 `ownership.is_valid` 时上报；内部状态映射按设计 7.2。
- 关闭顺序：停止接收 → 取消在途 → await conflict persistence → 逐个 `remove_entry(GATEWAY_STOPPED)` → await refresh → flush status → ownership.close。

- [ ] **Step 1: Write failing adapter tests**

用 fake SDK client 覆盖：认证成功 → on_ready；认证失败 → on_error('auth')；disconnected → on_close；`event.disconnected_event` → on_error('replaced')；ACK errcode 846604–846609 映射；ACK 超时/格式异常 → `WECOM_ACK_UNKNOWN`；disconnect 后晚到回调忽略；每次 connect 创建新实例。

- [ ] **Step 2: Implement adapter**

SDK 延迟导入；`WSClientOptions(bot_id, secret, max_reconnect_attempts=0, logger=SilentLogger())`。事件绑定 `authenticated/connected/disconnected/error/message/event.disconnected_event`。`reply_stream({"headers":{"req_id": req_id}}, stream_id, content, finish)`；解析 `RuntimeError("Reply ack error: errcode=...")`，仅整数非零视为 provider rejection。

- [ ] **Step 3: Write failing manager tests**

用 fake config/workflow/status/ownership/adapter，覆盖：
- 快照新增/变更/删除/非法；过期渠道跳过不建连。
- 认证/网络重试与租约保持；冲突 block 后停止；续租失败移除。
- 去重、pending、completed 204、busy、全局并发。
- start/chunk/done、截断、保活、首事件超时、STREAM_INTERRUPTED 重试、204 兜底、409 终态、404 移除。
- 到期定时器先发 CHANNEL_EXPIRED 再 release。
- stop 顺序与 offline/GATEWAY_STOPPED 上报。

- [ ] **Step 4: Implement manager and server**

`server.py` 使用 FastAPI：
- `GET /health` 200；`GET /ready` 依赖 `status().ready`。
- `POST /api/channels/connect`、`POST /api/channels/refresh`、`GET /internal/metrics` 使用 Bearer token，SHA-256 + `hmac.compare_digest`。
- connect 严格校验 `channel_id/revision/request_id`；调用 `manager.ensure_connected`；`GatewayError.status_code` 透传 400/404/409/503。
- refresh 202。
- metrics 文本 `channel_gateway_<key>`。

`config.py` 环境变量与校验按设计 10.2；`__main__.py` 加载配置、启动 uvicorn、SIGINT/SIGTERM 优雅关闭。

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/channel_gateway -v`
Expected: PASS。

---

### Task 7: Frontend Channel and Conversation Management

**Files:**
- Modify: `app/frontend/src/types.ts`
- Modify: `app/frontend/src/api/client.ts`
- Modify: `app/frontend/src/features/applications/ApplicationDetail.tsx`
- Modify: `app/frontend/src/features/applications/ApplicationPublishPanel.tsx`
- Create: `app/frontend/src/features/applications/ApplicationWecomChannel.tsx`
- Create: `app/frontend/src/features/applications/ApplicationConversations.tsx`
- Modify: 现有全局样式文件
- Test: `app/frontend/src/features/applications/ApplicationWecomChannel.test.tsx`、`ApplicationDetail` 相关测试

**Interfaces (produced):**

```ts
export interface WecomChannelState {
  id: string; bot_id: string; has_secret: boolean; default_response: string;
  enabled: boolean; active_release: ApplicationRelease | null;
  deployment_version: number; config_revision: number;
  status: "offline" | "online" | "error";
  error_code: "AUTH_FAILED" | "NETWORK_ERROR" | "CONNECTION_CONFLICT" | null;
  expires_at: string | null; has_unpublished_changes: boolean;
  dependencies_ready: boolean; dependency_error: string | null;
  last_deployed_at: string | null; last_reported_at: string | null;
}
export interface Publication { /* existing */ wecom_channel: WecomChannelState | null }
export interface Conversation {
  /* existing */ channel: "console" | "api" | "wecom";
  external_user_id: string | null;
}
```

- `client.ts` 增加：`createWecomChannel`、`updateWecomChannel`、`setWecomEnabled`、`connectWecomChannel`；`listDeployments(channelType)`；`listConversations(appId, channel)`。
- `ApplicationDetail` 渠道表增加企微行和“创建发布渠道”类型选择；企业微信配置弹窗；状态徽标（在线/离线/异常/已过期/状态未知，90 秒无上报）。
- `ApplicationPublishPanel` 目标渠道下拉；按所选渠道的 active release 比较；提交 `channel_type`。
- 会话视图增加渠道筛选（默认 console）；WeCom 只读、显示 `user_key` 摘要、无删除。
- secret 输入框永不回填；保存后清空。

- [ ] **Step 1: Write failing Vitest tests**

覆盖创建企微渠道、secret 不回填、状态徽标、过期禁用发布/启用/重连、按渠道部署、配置冲突提示、会话筛选、WeCom 无删除按钮。

- [ ] **Step 2: Implement types and API client**

- [ ] **Step 3: Implement UI components and integrate**

- [ ] **Step 4: Run frontend checks**

Run: `cd app/frontend && npm test && npm run typecheck && npm run build`
Expected: PASS。

---

### Task 8: Dependencies, Environment, Docs and End-to-End Verification

**Files:**
- Modify: `pyproject.toml`（`wecom-aibot-python-sdk>=1.0.2,<2.0.0`、`redis>=5.2,<6.0`）
- Modify: `uv.lock`（通过 `uv lock`）
- Modify: `.env.example`
- Modify: `docs/application_api.md` 或新增 `docs/wecom_channel.md`
- Test: 全量定向测试

- [ ] **Step 1: Add dependencies and lock**

Run: `uv add "wecom-aibot-python-sdk>=1.0.2,<2.0.0" "redis>=5.2,<6.0"`（若 uv 不在 PATH，先用项目约定方式安装/定位 uv；不得跳过 lock 更新）。

- [ ] **Step 2: Update `.env.example` and docs**

写入平台和网关变量、安全默认值、启动命令、local/Redis 单/多实例约束、机器人后台配置和 `/internal` 不对外暴露说明。

- [ ] **Step 3: Cross-layer contract tests**

新增 `tests/api_server/test_wecom_integration.py`：启动平台 TestClient + fake gateway control/workflow，覆盖创建渠道 → 创建 release → 首次 deploy → snapshot → status online → NDJSON run → 会话查询 → 停用 → snapshot 消失。

- [ ] **Step 4: Full verification**

```bash
uv run pytest tests/api_server tests/channel_gateway tests/agent -q
make lint
make typecheck
cd app/frontend && npm test && npm run typecheck && npm run build
```

Expected: 全部通过；Redis 集成测试在未设置 `REDIS_TEST_URL` 时 skip；记录任何无法运行的外部集成项。

- [ ] **Step 5: Review diff**

检查 `git diff`：无 secret、无 `.env`、无运行时数据、无无关格式化；确认 `.gitignore` 只新增设计/计划白名单。
