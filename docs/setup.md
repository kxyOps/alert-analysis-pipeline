# 从零部署指南

## 快速开始（5 分钟）

```bash
# 1. 克隆项目
git clone <your-repo-url>
cd alert-analysis-pipeline

# 2. 启动 Docker 服务（Prometheus + Grafana + n8n + KB Server）
cd deploy
docker compose up -d

# 3. 验证各服务
curl http://localhost:3000    # Grafana（admin / 密码见 deploy/.env 中 GF_SECURITY_ADMIN_PASSWORD，默认 changeme）
curl http://localhost:9090    # Prometheus
curl http://localhost:5678    # n8n
curl http://localhost:8888    # KB Web UI

# 4. 初始化知识库
docker compose exec kb-server python3 /app/kb-server.py
```

---

## 完整部署步骤

### 前提条件

- Docker + Docker Compose
- Python 3.9+
- 一个能跑 Hermes Agent 的服务器（可以是同一台机器）

### 安全与暴露面（必读）

- **Grafana**：默认口令见 `deploy/.env.example`，上线前务必改为强密码。
- **KB Web（`kb-server`，Compose 中端口 `8888`）**：**无登录**、**CORS `*`**、监听 **`0.0.0.0`**，仅适合**内网或受控网络**。不要映射到公网；若必须对外，请用反向代理并加认证、TLS、访问控制。
- **图谱视图**：页面通过 **CDN** 加载 vis-network；无外网时需自行托管静态文件并修改 `src/kb-server.py` 中的资源 URL。

### 第一步：配置环境变量

```bash
cd deploy

# 复制环境变量模板并修改
cp .env.example .env
vim .env
# 填入: GF_SECURITY_ADMIN_PASSWORD, HERMES_WEBHOOK_URL, WEBHOOK_SECRET, MONITOR_CONTAINER
```

### 第二步：部署 Docker 服务集群

```bash
cd deploy

# 按需修改采集目标（默认仅 scrape Prometheus 自身；示例 job 在文件中以注释形式给出）
vim prometheus/prometheus.yml
# 告警规则目录 prometheus/alerts/，当前含占位 minimal.yml，可追加自有规则

# 修改 Grafana 告警规则 (可选)
vim grafana/alerts/default.yml

# 启动
docker compose up -d
```

### 第三步：导入 n8n 工作流

1. 打开 http://localhost:5678
2. 创建管理员账号
3. Settings → API → 创建 API Key
4. 运行导出/导入脚本：

```bash
# 或者直接在 n8n UI 中导入工作流：
#   工作流 → Import from File → deploy/n8n/workflows/alert-pipeline.json
```

5. 导入后打开工作流中的 **Crypto** 节点，新建 **HMAC** 凭据，密钥与 Hermes / `WEBHOOK_SECRET` 保持一致（仓库里的 JSON 刻意不包含凭据引用，便于公开分享）。

6. 在 n8n 中设置环境变量：
   - `HERMES_WEBHOOK_URL` — Hermes webhook 接收地址
   - `WEBHOOK_SECRET` — HMAC 签名密钥

### 第四步：配置 Hermes

1. 在 Hermes 服务器的 `config.yaml` 中添加 webhook 配置（参考 `config/hermes-webhook.yaml`）
2. 在 `.env` 中设置相同的 `WEBHOOK_SECRET`
3. 重启 Hermes gateway

### 第五步：配置 Grafana 告警

1. 打开 http://localhost:3000（admin / 密码与 `deploy/.env` 中 `GF_SECURITY_ADMIN_PASSWORD` 一致，模板默认为 changeme）。Dashboard 列表中应有预置的 **Alert Pipeline — Sample**（来自 `deploy/grafana/dashboards/*.json`）；可自行追加导出 JSON 到该目录并重启 Grafana。
2. 数据源 → Prometheus → URL: http://prometheus:9090
3. 告警 → 创建告警规则（或使用预置的 `grafana/alerts/default.yml`）
4. 告警联系点 → 添加 webhook → URL: `http://n8n:5678/webhook/alert-receiver`（须与工作流 `deploy/n8n/workflows/alert-pipeline.json` 里 Webhook 节点的路径 **`alert-receiver`** 一致）

### 第六步：配置飞书 Bot（可选）

1. 飞书开放平台 → 创建应用 → 获取 App ID / App Secret
2. 权限管理 → 添加 `im:message`
3. 事件订阅 → 添加 `im.message.receive_v1`
4. 配置 Hermes `.env`：
   ```
   FEISHU_APP_ID=cli_xxx
   FEISHU_APP_SECRET=xxx
   FEISHU_GROUP_POLICY=open
   ```
5. 参考 `config/hermes-feishu.yaml` 配置 channel_prompt
6. 重新发布应用版本

---

## 知识库操作

```bash
# 本地运行（不需要 Docker）
cd src

# 查看所有条目
python3 kb.py list

# 添加故障记录
python3 kb.py add "服务连接超时" 2 "网络抖动导致"

# 匹配告警消息（纯文本）
python3 kb.py match "OutOfMemoryError"

# 匹配告警 payload（Grafana webhook 格式）
python3 kb.py match --json '{"alertname":"HighMemoryUsage","message":"memory limit exceeded"}'

# 清理超量记录
python3 kb.py cleanup

# 启动 Web UI
python3 kb-server.py

# 或用环境变量指定数据目录
KB_DATA_DIR=/path/to/data python3 kb.py list
KB_DATA_DIR=/path/to/data python3 kb-server.py 9999

# 时区（可选）：IANA 名称，或相对 UTC 的小时整数
# KB_TIMEZONE=Asia/Shanghai python3 kb.py list
# KB_TZ_OFFSET=8 python3 kb-server.py
```

---

## 自定义知识库

编辑 `data/fault-kb.json`，每条记录格式：

```json
{
  "id": "001",
  "title": "故障名称",
  "match": {
    "alertname": "正则表达式（匹配告警规则名）",
    "message": "正则表达式（匹配告警消息）",
    "exclude": "正则表达式（排除恢复等误报）"
  },
  "root_cause": "根因描述",
  "root_cause_id": 1,
  "recovery_action": "恢复步骤（多行）",
  "hit_count": 0
}
```

根因可预先定义在 `root_causes` 数组中：

```json
{
  "root_causes": [
    {"id": 1, "name": "内存超限", "description": "..."},
    {"id": 2, "name": "连接失败", "description": "..."}
  ]
}
```

---

## 架构选型说明

| 为什么用 n8n 而不是直接写代码？ | n8n 提供可视化编排、重试、错误处理、Webhook 管理，非技术人员也能维护链路 |
|--|--|
| 为什么 KB 引擎用 stdlib？ | 零依赖告警场景，服务器上可能没有 pip，纯 Python 3 就能跑 |
| 为什么用 SQLite？ | 单节点场景不需要 MySQL/PostgreSQL，零运维 |
| 为什么 HMAC 签名？ | n8n → Hermes 之间需要验证来源，防止伪造告警 |
