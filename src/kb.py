#!/usr/bin/env python3
"""
故障知识库 CLI 管理工具 v2

通用的告警知识库引擎 — 不绑定任何特定服务。
支持多字段正则过滤 + 关键词评分排序。

用法:
  kb.py list                              # 列出所有条目
  kb.py add <消息> <根因ID> [备注]        # 新增记录（自动提取关键词）
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
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(KB_PATH):
        default = {
            'version': 3,
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


def _extract_keywords(text):
    """从文本中提取重要的中文/英文分词作为关键词"""
    text = text.strip()
    kws = []
    # 英文单词（2个字符以上）
    for w in re.findall(r'[a-zA-Z][a-zA-Z0-9_\-\.]{2,}', text):
        kws.append(w.lower())
    # 中文词组（2个字以上）
    for w in re.findall(r'[\u4e00-\u9fff]{2,}', text):
        kws.append(w)
    # 去重 + 限制数量
    seen = set()
    result = []
    for k in kws:
        if k not in seen:
            seen.add(k)
            result.append(k)
    return result[:15]  # 最多15个关键词


def add(alert_msg, root_cause_id, note='', keywords=None):
    """新增故障记录"""
    data = load()
    keywords = keywords or _extract_keywords(alert_msg)
    entry = {
        'id': next_id(data),
        'title': alert_msg[:80],
        'keywords': keywords,
        'type': 'specific',
        'match': {
            'exclude': 'resolved|recover|ok|test|fake'
        },
        'root_cause_id': int(root_cause_id),
        'note': note,
        'created_at': datetime.now(TZ).isoformat(),
        'hit_count': 0,
        # 从已有相同根因的条目继承描述
    }
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
    """
    核心匹配逻辑：正则过滤 + 关键词评分排序

    流程:
    1. 排除规则 → 被排除的跳过
    2. alertname 正则 → 不匹配的跳过
    3. 关键词评分 → 命中数/总词数 算分
    4. 无关键词的条目走旧版 message 正则保底
    5. 按得分降序 + 优先级降序，取最优

    返回的 result 增加 score 和 matched_keywords 字段
    """
    data = load()
    text_lower = {k: v.lower() for k, v in fields.items() if v}
    msg_text = text_lower.get('message', '')
    an_text = text_lower.get('alertname', '')

    scored = []  # [(score, priority, entry), ...]

    for e in data['entries']:
        match = e.get('match', {})

        # ── 旧格式兼容（无 match 字段） ──
        if not match:
            old_pattern = e.get('alert_pattern', '')
            if old_pattern and msg_text and re.search(old_pattern, msg_text, re.IGNORECASE):
                scored.append((1.0, e))
            continue

        # ── 排除规则（词边界匹配） ──
        exclude_pat = match.get('exclude', '')
        if exclude_pat:
            excluded = False
            for _, txt in text_lower.items():
                for pat in exclude_pat.split('|'):
                    pat = pat.strip()
                    if pat and re.search(r'\b' + pat + r'\b', txt, re.IGNORECASE):
                        excluded = True
                        break
                if excluded:
                    break
            if excluded:
                continue

        # ── alertname 正则过滤 ──
        an_pat = match.get('alertname', '')
        if an_pat and an_text and not re.search(an_pat, an_text, re.IGNORECASE):
            continue

        # ── 关键词评分 ──
        keywords = e.get('keywords', [])

        if keywords and msg_text:
            matched_kws = [kw for kw in keywords if kw.lower() in msg_text]
            if matched_kws:
                score = len(matched_kws) / len(keywords)
                e['_matched_keywords'] = matched_kws
                e['_score'] = round(score, 3)
                scored.append((score, e))
                continue

        # ── 无关键词 → 用 message 正则保底 ──
        msg_pat = match.get('message', '')
        if not keywords and msg_pat and msg_text:
            if re.search(msg_pat, msg_text, re.IGNORECASE):
                scored.append((0.5, e))
                continue

    if not scored:
        return None

    # 按评分降序 → specific 优先于 catchall → id 升序
    scored.sort(key=lambda x: (
        -x[0],
        0 if x[1].get('type', 'specific') == 'specific' else 1,
        x[1].get('id', '')
    ))
    best = scored[0][1]

    # 更新命中次数
    best['hit_count'] = best.get('hit_count', 0) + 1
    save(data)

    return _build_result(best)


def _build_result(e):
    result = {
        'id': e['id'],
        'title': e.get('title', ''),
        'root_cause': e.get('root_cause', ''),
        'root_cause_id': e.get('root_cause_id', ''),
        'recovery_action': e.get('recovery_action', ''),
        'note': e.get('note', ''),
    }
    # 如果有关键词匹配信息，附加到结果
    if '_score' in e:
        result['_score'] = e['_score']
    if '_matched_keywords' in e:
        result['_matched_keywords'] = e['_matched_keywords']
    return result


def list_entries():
    data = load()
    current_sym = None
    for e in data['entries']:
        sym = e.get('symptom', '')
        if sym != current_sym:
            current_sym = sym
            print(f"── {sym} ─{'─' * (40 - len(sym))}")
        kws = e.get('keywords', [])
        priority = e.get('priority', 0)
        c = e.get('created_at', '')
        print(f"  #{e['id']}  {e.get('title','')[:60]}  [{e.get('type','specific')}]")
        if kws:
            print(f"      关键词: {', '.join(kws[:8])}{'...' if len(kws) > 8 else ''}")
        print(f"      根因: {e.get('root_cause','')[:60]}")
        print(f"      命中: {e.get('hit_count',0)}次")
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
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("NO_MATCH")

    elif cmd == 'cleanup':
        cleanup()

    else:
        print(f"未知命令: {cmd}")
