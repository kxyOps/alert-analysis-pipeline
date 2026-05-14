# 告警分析管道 Alert Analysis Pipeline

**English:** A generic alert pipeline — Grafana / webhooks → knowledge-base matching → optional AI analysis → notifications (e.g. Feishu). Ships with a zero-dependency Python KB CLI, embedded Web UI, and Docker Compose for Prometheus, Grafana, n8n, and the KB server.

> 这是一个**通用的告警处理链路** —— 从告警触发 → 知识库匹配 → AI 分析 → 飞书通知，开箱即用。

## 它能做什么

```
Grafana 告警触发
    │
    ▼
知识库命中 ──→ 直接输出根因 + 恢复步骤（秒级）
    │
    ▼
知识库未命中 ──→ AI 分析告警内容 → 给出排查建议
    │
    ▼
飞书群通知
```

## 核心组件

| 组件 | 说明 |
|------|------|
| `src/kb.py` | 知识库 CLI — 排除 / `alertname` 过滤、关键词评分、`message` 正则兜底 |
| `src/kb-server.py` | Web UI + REST API — 表格/图谱视图，零依赖，单文件 |
| `deploy/` | Docker Compose — 一键启动 Prometheus + Grafana + n8n + KB Server |
| `config/` | Hermes 集成配置模板 |

**KB Web UI 预览：**
- 表格视图：搜索、排序、筛选
- 图谱视图：vis.js 故障→根因关联图，节点大小按命中次数（脚本与样式从 **cdnjs.cloudflare.com** 加载；无外网的环境请将 vis-network 放到本地或内网静态路径并改 `kb-server.py` 中的 `<script>` / `<link>`）
- 详情面板：点击条目展开根因、恢复步骤

**时区：** 条目时间戳默认按 **`KB_TIMEZONE`** 或系统 **`TZ`**（IANA，如 `Asia/Shanghai`），否则 **`KB_TZ_OFFSET`**（相对 UTC 的小时整数），再否则 **UTC+8**（兼容旧行为）。示例：`KB_TIMEZONE=UTC KB_DATA_DIR=../data python3 kb.py list`。

## 快速开始

```bash
# 启动所有服务
cd deploy
# 首次可选：cp .env.example .env 再编辑 Grafana 口令等（勿提交 .env）
docker compose up -d

# 查看知识库
cd ../src
python3 kb.py list

# 启动 Web UI
python3 kb-server.py
# 打开 http://localhost:8888
```

完整部署步骤看 [docs/setup.md](docs/setup.md)

**端到端告警链路（Grafana → n8n → Hermes → 飞书）** 需要在你环境中**另行部署 Hermes（或兼容的 Agent Gateway）**：本仓库**不包含** Hermes 服务本体，仅提供 webhook / 飞书侧配置模板。`docker compose up` 会启动 Prometheus、Grafana、n8n、KB Web；再接入 Hermes 并配置 `deploy/.env` 与 `config/hermes-webhook.yaml` 后，整条链路才能跑通。

**本地安装 CLI（可选）：** 在项目根目录执行 `pip install -e .`，可在任意路径使用命令 **`kb`** / **`kb-server`**（依赖可编辑安装，详见 `pyproject.toml`）。

**数据说明：** `kb.py` / 告警链路默认读 **`data/fault-kb.json`**。KB Web UI 在同一目录使用 **SQLite（`fault-kb.db`）**，在界面增删改后会 **同步写回 JSON**，便于 CLI 与 Git 一致。

## 工作流

### 1. 在线告警链路

```
Grafana 触发告警 → n8n（采集日志 + HMAC 签名） → Hermes（KB 匹配 → AI 分析） → 飞书通知
```

### 2. 知识库积累

```
飞书群 @bot fault <消息> <根因ID> → Hermes 执行 kb.py add → KB 自动更新
```

Web UI 上也能直接 CRUD 管理知识库条目。

### 3. 知识库匹配逻辑

实现见 `src/kb.py` 中 `_match_all`。告警文本来自 webhook payload（如 Grafana：`labels.alertname` + `annotations` / 顶层中的 message/summary/description）。

每条记录在 `match` 下可配置（字段均可选）：

| 字段 | 作用 |
|------|------|
| `exclude` | 按 `\|` 拆成多段，对 **alertname 与 message 全文** 做词边界匹配；任一段命中则 **本条目跳过**（常用于过滤含 resolved / recover 的恢复态告警） |
| `alertname` | 若配置了非空正则，则 Grafana **规则名** 必须匹配，否则跳过 |
| `keywords` | 在 **message**（小写）中做子串匹配；至少命中一个词则参与打分：**得分 = 命中词数 / 关键词总数** |
| `message` | **仅当该条目没有 `keywords`（或为空）时** 启用：对 message 做正则匹配，命中则参与候选（固定较低基准分） |

无 `match` 的旧数据可走顶层 `alert_pattern`，仅对 message 正则匹配（兼容旧版）。

**择优**：在所有通过筛选的候选中，按 **得分从高到低**；得分相同则 **`type: specific` 优先于 `catchall`**；再按 **id** 取最优一条。`kb.py validate` 可校验 JSON 与正则写法。

## 项目架构

```
alert-analysis-pipeline/
├── pyproject.toml        # pip install -e . 注册 kb / kb-server 命令
├── src/                  # 核心 Python 代码（零外部依赖）
│   ├── kb.py             # 知识库 CLI 工具
│   ├── kb-server.py      # Web UI + REST API
│   └── kb_tz.py          # 默认时区（KB_TIMEZONE / KB_TZ_OFFSET）
├── data/
│   └── fault-kb.json     # 样本知识库（含多条示例条目）
├── deploy/               # 一键部署
│   ├── docker-compose.yml
│   ├── Dockerfile.kb
│   ├── prometheus/       # Prometheus（含 alerts/*.yml）
│   ├── grafana/          # Grafana 告警规则 + 数据源
│   └── n8n/workflows/    # n8n 工作流 JSON
├── .github/workflows/    # CI（validate + 语法检查）
├── config/               # 集成配置
│   ├── kb.env.example
│   ├── hermes-webhook.yaml
│   └── hermes-feishu.yaml
├── docs/
│   ├── architecture.md   # 整体架构图
│   └── setup.md          # 从零部署指南
└── scripts/              # 辅助脚本（含 kb-web-dev.py 开发热重启）
```

## 适用场景

- Doris / ClickHouse / MySQL 等数据库监控告警
- 微服务健康告警体系
- 任何需要「快速识别已知故障」的运维场景
- 团队内部知识库积累 + 故障复盘

## CI 与安全提示

- **GitHub Actions**：`.github/workflows/ci.yml` 在推送到 **`main` / `master` / `develop`** 或指向这些分支的 PR 时运行；也可在仓库 **Actions** 页 **手动运行**（`workflow_dispatch`）。
- **Compose 默认口令**：Grafana 默认用户 `admin`，密码来自环境变量 **`GF_SECURITY_ADMIN_PASSWORD`**（未设置时为 `changeme`，见 `deploy/.env.example`）；对外部署前务必修改，并与 **`WEBHOOK_SECRET`** 等一并替换占位符。
- **KB Web（`kb-server`）暴露面**：默认监听 **`0.0.0.0`**，REST API **无认证**，响应头 **CORS 为 `*`**，便于内网调试。**请勿将服务端口直接暴露到公网**；生产应置于反向代理之后，按需加认证、TLS 与 IP 限制。图谱页从 **CDN** 加载 vis-network；离线环境需自备静态资源（见上文「KB Web UI 预览」或 `docs/setup.md`）。

## 设计原则

- **零外部依赖** — Python stdlib only，部署即用
- **开箱即用** — docker compose up 就具备完整链路
- **通用不绑定** — 不限定某一种数据库或服务
- **低成本** — SQLite 存储，单文件 Web UI，15MB 内存

## 许可

MIT
