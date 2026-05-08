#!/bin/bash
# 从运行中的 n8n 导出工作流
# 用法: ./export-workflow.sh <n8n-url> <api-key>
#
# 先要在 n8n 中生成 API Key:
#   Settings → API → 创建 API Key

N8N_URL="${1:-http://localhost:5678}"
API_KEY="${2}"

if [ -z "$API_KEY" ]; then
  echo "用法: $0 <n8n-url> <api-key>"
  echo ""
  echo "在 n8n 中生成 API Key:"
  echo "  Settings → API → Create API Key"
  exit 1
fi

OUTPUT_DIR="deploy/n8n/workflows"
mkdir -p "$OUTPUT_DIR"

echo "[export] 从 $N8N_URL 导出工作流..."

curl -s -H "X-N8N-API-KEY: $API_KEY" "$N8N_URL/rest/workflows" \
  | python3 -c "
import json, sys, os
data = json.load(sys.stdin)
workflows = data.get('data', [])
os.makedirs('$OUTPUT_DIR', exist_ok=True)
for w in workflows:
    name = w['name'].replace(' ', '-').replace('/', '_')
    path = f'$OUTPUT_DIR/{name}.json'
    with open(path, 'w') as f:
        json.dump(w, f, ensure_ascii=False, indent=2)
    print(f'  ✓ {path}')
print(f'导出 {len(workflows)} 个工作流')
"
