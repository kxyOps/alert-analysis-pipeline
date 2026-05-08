# 告警分析管道 Alert Analysis Pipeline

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
| `src/kb.py` | 知识库 CLI 工具 — 多字段正则匹配告警，查出已知故障 |
| `src/kb-server.py` | Web UI + REST API — 表格/图谱视图，零依赖，单文件 |
| `deploy/` | Docker Compose — 一键启动 Prometheus + Grafana + n8n + KB Server |
| `config/` | Hermes/Flysrs 集成配置模板 |

**KB Web UI 预览：**
- 表格视图：搜索、排序、筛选
- 图谱视图：vis.js 故障→根因关联图，节点大小按命中次数
- 详情面板：点击条目展开根因、恢复步骤

## 快速开始

```bash
# 启动所有服务
cd deploy
docker compose up -d

# 查看知识库
cd ../src
python3 kb.py list

# 启动 Web UI
python3 kb-server.py
# 打开 http://localhost:8888
```

完整部署步骤看 [docs/setup.md](docs/setup.md)

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

每条 KB 记录定义：
- `alertname` 正则 — 匹配 Grafana 规则名
- `message` 正则 — 匹配告警消息
- `exclude` 正则 — 排除 resolved/recover 等恢复消息

**两者都匹配才算命中**，有效防止误报。

## 项目架构

```
alert-analysis-pipeline/
├── src/                  # 核心 Python 代码（零外部依赖）
│   ├── kb.py             # 知识库 CLI 工具
│   └── kb-server.py      # Web UI + REST API
├── data/
│   └── fault-kb.json     # 样本知识库（5 类预置故障）
├── deploy/               # 一键部署
│   ├── docker-compose.yml
│   ├── Dockerfile.kb
│   ├── prometheus/       # Prometheus 配置
│   ├── grafana/          # Grafana 告警规则 + 数据源
│   └── n8n/workflows/    # n8n 工作流 JSON
├── config/               # 集成配置
│   ├── kb.env.example
│   ├── hermes-webhook.yaml
│   └── hermes-feishu.yaml
├── docs/
│   ├── architecture.md   # 整体架构图
│   └── setup.md          # 从零部署指南
└── scripts/              # 辅助脚本
```

## 适用场景

- Doris / ClickHouse / MySQL 等数据库监控告警
- 微服务健康告警体系
- 任何需要「快速识别已知故障」的运维场景
- 团队内部知识库积累 + 故障复盘

## 设计原则

- **零外部依赖** — Python stdlib only，部署即用
- **开箱即用** — docker compose up 就具备完整链路
- **通用不绑定** — 不限定某一种数据库或服务
- **低成本** — SQLite 存储，单文件 Web UI，15MB 内存

## 许可

MIT
