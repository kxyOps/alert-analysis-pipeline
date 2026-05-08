#!/usr/bin/env python3
"""
故障知识库 CLI 管理工具 v2

通用的告警知识库引擎 — 不绑定任何特定服务。
支持多字段正则匹配，适合集成到告警流水线中。

用法:
  kb.py list                              # 列出所有条目
  kb.py add <消息> <根因ID> [备注]        # 新增记录
  kb.py match <消息>                      # 纯文本匹配
  kb.py match --json '<JSON>'             # 告警 payload 匹配
  kb.py cleanup                           # 清理超量记录

环境变量:
  KB_DATA_DIR    数据目录（默认: ./data）
  KB_MAX_ENTRIES 最大条目数（默认: 200）
"""

import json, os, sys, re, time
from datetime import datetime, timezone, timedelta

DATA_DIR = os.environ.get('KB_DATA_DIR', os.path.join(os.path.dirname(__file__), '..', 'data'))
KB_PATH = os.path.join(DATA_DIR, 'fault-kb.json')
MAX_ENTRIES = int(os.environ.get('KB_MAX_ENTRIES', 200))
CLEANUP_RETAIN = int(os.environ.get('KB_CLEANUP_RETAIN', 150))
TZ = timezone(timedelta(hours=8))


def ensure_data():
    """确保数据文件和目录存在"""
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(KB_PATH):
        default = {
            'version': 2,
            'max_entries': MAX_ENTRIES,
            'created_at': datetime.now(TZ).isoformat(),
            'entries': []
        }
        save(default)
        print(f"[KB] 初始化空知识库: {KB_PATH}")
    return KB_PATH


def load():
    with open(ensure_data()) as f:
        return json.load(f)


def save(data):
    with open(KB_PATH, 'w') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def next_id(data):
    ids = [int(e['id']) for e in data['entries'] if e['id'].isdigit()]
    return f"{max(ids) + 1 if ids else 1:03d}"


def add(alert_msg, root_cause_id, note=''):
    """新增故障记录"""
    data = load()
    entry = {
        'id': next_id(data),
        'title': alert_msg[:80],
        'match': {
            'message': re.escape(alert_msg[:50]),
            'exclude': 'resolved|recover|ok|test|fake'
        },
        'root_cause_id': int(root_cause_id),
        'note': note,
        'created_at': datetime.now(TZ).isoformat(),
        'hit_count': 0
    }
    # 从已有条目中找根因描述
    for e in data['entries']:
        if str(e.get('root_cause_id')) == str(root_cause_id):
            entry['root_cause'] = e.get('root_cause', '')
            entry['recovery_action'] = e.get('recovery_action', '')
            break
    else:
        entry['root_cause'] = f"根因 #{root_cause_id}（由用户添加）"
        entry['recovery_action'] = '参考运维手册'

    data['entries'].append(entry)
    save(data)
    return entry['id']


def match_text(text):
    """旧接口：纯文本匹配"""
    return _match_all({'message': text})


def match_payload(payload):
    """从告警 payload 中提取字段匹配

    支持多种格式:
    - Grafana webhook: { alert: { labels: { alertname }, annotations: { message/summary/description } } }
    - 标准格式: { alertname, message }
    """
    if isinstance(payload, str):
        payload = json.loads(payload)

    alert = payload.get('alert', payload)
    if isinstance(alert, str):
        alert = json.loads(alert)

    labels = alert.get('labels', {})
    annotations = alert.get('annotations', {})
    alertname = labels.get('alertname', alert.get('alertname', ''))
    message = (alert.get('message', '') or
               annotations.get('message', '') or
               annotations.get('summary', '') or
               annotations.get('description', '') or
               labels.get('severity', ''))

    return _match_all({
        'alertname': str(alertname),
        'message': str(message)
    })


def _match_all(fields):
    """核心匹配逻辑：多字段交叉验证"""
    data = load()
    text_lower = {k: v.lower() for k, v in fields.items() if v}

    for e in data['entries']:
        match = e.get('match', {})
        if not match:
            old_pattern = e.get('alert_pattern', '')
            if old_pattern:
                msg = text_lower.get('message', '')
                if msg and re.search(old_pattern, msg, re.IGNORECASE):
                    e['hit_count'] = e.get('hit_count', 0) + 1
                    save(data)
                    return _build_result(e)
            continue

        # 排除规则（防止恢复/测试误判）
        exclude_pat = match.get('exclude', '')
        if exclude_pat:
            for _, txt in text_lower.items():
                if re.search(exclude_pat, txt, re.IGNORECASE):
                    return None

        checks_passed = 0
        checks_required = 0

        # alertname 匹配
        an_pat = match.get('alertname', '')
        an_text = text_lower.get('alertname', '')
        if an_pat and an_text:
            checks_required += 1
            if re.search(an_pat, an_text, re.IGNORECASE):
                checks_passed += 1

        # message 匹配
        msg_pat = match.get('message', '')
        msg_text = text_lower.get('message', '')
        if msg_pat and msg_text:
            checks_required += 1
            if re.search(msg_pat, msg_text, re.IGNORECASE):
                checks_passed += 1

        # 所有有定义的字段都必须匹配
        if checks_required > 0 and checks_passed == checks_required:
            e['hit_count'] = e.get('hit_count', 0) + 1
            save(data)
            return _build_result(e)

    return None


def _build_result(e):
    return {
        'id': e['id'],
        'title': e.get('title', ''),
        'root_cause': e.get('root_cause', ''),
        'root_cause_id': e.get('root_cause_id', ''),
        'recovery_action': e.get('recovery_action', ''),
        'note': e.get('note', '')
    }


def list_entries():
    data = load()
    for e in data['entries']:
        m = e.get('match', {})
        an = m.get('alertname', e.get('alert_pattern', '—'))[:40]
        ms = m.get('message', '—')[:40]
        c = e.get('created_at', '')
        print(f"#{e['id']}  {e.get('title','')[:60]}")
        print(f"    匹配 alertname: {an}")
        print(f"    匹配 message:   {ms}")
        print(f"    根因: {e.get('root_cause','')[:60]}")
        print(f"    命中: {e.get('hit_count',0)}次  创建: {c[:16] if c else '未记录'}")
        print()


def cleanup():
    data = load()
    entries = data['entries']
    if len(entries) <= MAX_ENTRIES:
        print(f"当前 {len(entries)} 条，未超过上限 {MAX_ENTRIES}，无需清理")
        return
    entries.sort(key=lambda e: e.get('created_at', ''), reverse=True)
    to_keep = entries[:CLEANUP_RETAIN]
    to_remove = entries[CLEANUP_RETAIN:]
    data['entries'] = to_keep
    save(data)
    print(f"清理了 {len(to_remove)} 条旧记录，剩余 {len(to_keep)} 条")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("用法: kb.py <list|add|match|cleanup> [...]")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == 'list':
        list_entries()

    elif cmd == 'add':
        if len(sys.argv) < 4:
            print("用法: kb.py add <告警消息> <根因ID> [备注]")
            sys.exit(1)
        aid = add(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else '')
        print(f"KB_WRITTEN {aid}")

    elif cmd == 'match':
        if len(sys.argv) < 3:
            print("用法: kb.py match <消息> 或 kb.py match --json '<JSON>'")
            sys.exit(1)
        if sys.argv[2] == '--json' and len(sys.argv) > 3:
            result = match_payload(sys.argv[3])
        else:
            result = match_text(sys.argv[2])
        if result:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print("NO_MATCH")

    elif cmd == 'cleanup':
        cleanup()

    else:
        print(f"未知命令: {cmd}")
