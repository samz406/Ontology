# Ontology Agent

基于 [v4 完整设计文档](docs/Ontology_Agent_完整设计文档_v4.md) 实现的**可运行首期垂直切片**：将来源事件映射为有版本的业务事实，按确认的身份绑定读取事实，用确定性规则执行订单退款资格调查，并输出证据、质量状态和审计记录。前端采用 React/TypeScript，后端采用 Python/FastAPI，数据存 PostgreSQL，独立 Worker 处理入队事件。

> 当前是开发/试点版本，示例退款条件仅用于演示，**不能作为真实退款授权或生产发布依据**。v4 对完整企业环境的要求和本仓库尚未实现的能力列于下方；业务策略和真实来源接入需要企业提供。

## 五分钟运行（Docker Compose，本地开发）

需要 Docker Engine、Compose v2。仓库根目录执行：

```bash
docker compose up --build -d
docker compose ps
docker compose exec api python -m app.seed
```

打开 `http://localhost:8080`，使用本地演示管理员令牌 `local-demo-admin-token-12345` 或分析员令牌 `local-demo-analyst-token-12345` 登录。来源选择 `erp`，订单号 `ORDER-10001`，点击“开始调查”；预期示例结论为 `ELIGIBLE`，并显示两条事实证据。查看 `http://localhost:8080/api/healthz` 应得到 `{"status":"ok"}`。再次运行 seed 不会重复插入。演示令牌仅属于显式 seed 的本地租户；Compose 默认使用本地开发密码，勿对外开放。

```bash
docker compose logs api worker
docker compose down             # 保留数据库卷
docker compose down -v          # 删除本地开发数据
```

如果端口被占用，可使用 `WEB_PORT=18080 docker compose up --build -d`。本环境没有 Docker，以上容器启动步骤由 CI 之外的有 Docker 环境执行验证。

## 不使用 Docker 的开发运行

Python 3.12、Node.js 24。后端使用 SQLite 方便本地测试；并行 Worker 和正式部署请使用 PostgreSQL。

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
export DATABASE_URL=sqlite:///./ontology.db APP_ENV=development
.venv/bin/alembic upgrade head
.venv/bin/python -m app.seed
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

在另一个终端运行 `cd backend && export DATABASE_URL=sqlite:///./ontology.db && .venv/bin/python -m app.worker`；第三个终端运行 `cd frontend && npm ci && npm run dev`，打开 `http://localhost:5173`。Vite 将 `/api` 代理到 `localhost:8000`。离线检查：`cd backend && .venv/bin/python -m pytest -q && .venv/bin/alembic check`；前端 `cd frontend && npm run build`。

## 从空租户开始

不需要演示数据时，先执行 `alembic upgrade head`，再明确创建初始租户及管理员。数据库连接变量需在执行环境中指向同一数据库：

```bash
export BOOTSTRAP_TENANT_ID=mytenant
export BOOTSTRAP_TENANT_NAME='My tenant'
export BOOTSTRAP_ADMIN_ACTOR=operator
export BOOTSTRAP_ADMIN_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
python3 -m app.bootstrap
printf '请立即安全保存管理员令牌：%s\n' "$BOOTSTRAP_ADMIN_TOKEN"
```

上面的命令在 `backend` 目录、已安装依赖的 Python 环境中运行；容器则通过 `docker compose exec -e BOOTSTRAP_TENANT_ID -e BOOTSTRAP_ADMIN_TOKEN api python -m app.bootstrap` 执行。bootstrap 不回显也不明文存储令牌；请将本地终端打印的令牌存入凭据管理器，避免在共享终端操作。重复运行会拒绝覆盖现有租户。创建后的管理员可通过 UI 或 `/v1/admin/*` 创建来源、属性、Order、身份绑定、映射和规则。依次发布规则、从来源令牌调用 `/v1/ingest/events`、等待 Worker 处理、发起调查。`backend/tests/test_investigation.py::test_empty_tenant_configure_ingest_publish_investigate` 是可复现的完整调用示例。

### 核心 API

所有 `/v1` 接口使用 `Authorization: Bearer <token>`；租户和角色由服务端令牌解析，不接收调用方指定的租户 ID。

| 动作 | 方法与路径 | 角色 |
| --- | --- | --- |
| 当前身份、来源列表 | `GET /v1/me`, `GET /v1/sources` | 管理员/分析员 |
| 来源、属性、实体、绑定、Mapping、规则配置 | `POST /v1/admin/{sources,properties,entities,bindings,mappings,rules}` | 管理员 |
| 撤销绑定、发布规则 | `PATCH /v1/admin/bindings/{id}`, `POST /v1/admin/rules/{id}/publish` | 管理员 |
| 入队来源事实事件 | `POST /v1/ingest/events` | 对应来源令牌/管理员 |
| 任务状态、审计日志、资产 | `GET /v1/admin/sync/tasks/{id}`, `/v1/admin/audit-events`, `/v1/admin/state` | 管理员 |
| 调查、历史与证据 | `POST/GET /v1/investigations`, `GET /v1/investigations/{id}`, `GET /v1/evidence/{id}` | 有权访问的管理员/分析员 |

API 的 OpenAPI 描述在 `http://localhost:8080/api/docs`（直连后端为 `/docs`）。调查请求样例：

```json
{"anchor":{"source_system_id":"<erp 的来源 ID>","source_record_key":"ORDER-10001"},"investigation_type":"refund_eligibility","question":"现在符合退款资格吗？"}
```

结果拆分 `decision.kind`（`ELIGIBLE`/`INELIGIBLE`/`CONFLICT`/`INDETERMINATE`）、`execution.state` 和 `quality`；`consistency.unified_snapshot=false`，不声称跨系统事务快照。映射只允许每来源首次发布；事件字段为局部更新，删除事件将该来源记录的既有属性标记撤销。同一来源记录的版本单调递增，重放旧版本不覆盖新版本，同版本不同载荷进入失败任务。`business_as_of` 仅支持当前附近 60 秒，不能把它用于历史时点重放。

## v4 落地范围与生产门槛

| v4 能力 | 当前代码状态 |
| --- | --- |
| 租户隔离、角色/属性访问检查、确认和撤销身份绑定 | 已实现基本 API、数据库约束和集成测试；生产身份源需对接企业 SSO |
| 来源记录→Mapping→追加式 Assertion→规则→Evidence/Audit | 已实现可运行链路；按来源记录版本去重，删除标记撤销，Worker 有持久队列和重试 |
| 业务结论/执行状态/数据质量分离、规则 AST、语义包版本 | 已实现示例 `Order` 退款调查；规则为业务管理员提交的确定性表达式 |
| 前端调查、证据、资产配置及同步监控 | 已实现基本工作台；未实现完整身份治理与高级运营控制台 |
| 真实 ERP/支付/物流连接器、增量游标、全量对账、完备性证明 | 尚未实现；接入方调用 Ingest API，来源完整性固定为 `UNKNOWN` |
| OIDC/企业权限、密钥轮换、双人审批、字段级细粒度策略、保留和删除策略 | 尚未实现；当前开发令牌登录不可直接用于企业生产 |
| 任意历史时点回放、跨来源统一快照、Mapping 升级影子验证 | 尚未实现；历史查询明确拒绝，Mapping 升级明确拒绝 |
| 高可用、性能压测、灾难恢复演练、监控告警、漏洞扫描 | 尚未完成；CI 验证单元/集成用例、迁移和前端构建 |

正式环境必须先确认真实退款政策与权限矩阵、来源权威性和 SLA；接入真实系统并验证删除/重放/故障场景；部署 PostgreSQL 与持久备份、TLS 网关、企业身份系统、审计留存和密钥管理；按 v4 第 16～18 节完成发布证据与回滚演练。此仓库不发起退款或修改业务系统，仅提供调查结果。

## 代码结构

`backend/app/main.py` 是 API，`worker.py` 处理来源事件，`investigation.py` 组织事实/证据，`rules.py` 执行规则；`models.py` 和 `alembic/` 定义并迁移数据库。`frontend/src/` 是前端，`docs/` 保留完整 v4 设计原文。CI 位于 `.github/workflows/ci.yml`。

