#!/bin/bash
# 初始化故障知识库（从样本 JSON 迁移到 SQLite）
set -e

DATA_DIR="${KB_DATA_DIR:-./data}"

echo "[init-kb] 数据目录: $DATA_DIR"
echo "[init-kb] 启动 kb-server 初始化数据库..."
python3 src/kb-server.py &
PID=$!

# 等服务器启动
sleep 2

# 测试 API
echo "[init-kb] 验证 API..."
curl -s http://localhost:8888/api/stats | python3 -m json.tool 2>/dev/null || echo "API 暂不可用，请手动验证"

# 停止服务器
kill $PID 2>/dev/null
wait $PID 2>/dev/null

echo "[init-kb] 完成。数据已写入 $DATA_DIR"
echo "[init-kb] 运行 docker compose up -d 可启动完整服务"
