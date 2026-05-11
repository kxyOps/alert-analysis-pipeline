#!/usr/bin/env python3
"""
故障知识库 CLI 管理工具 v2

通用的告警知识库引擎 — 不绑定任何特定服务。
支持多字段正则过滤 + 关键词评分排序。

用法:
  kb.py list                              # 列出所有条目
  kb.py add <消息> <根因ID> [备注]        # 新增记录（自动提取关键词）
  kb.py delete <id>                       # 删除指定记录
  kb.py edit <id> [--title ...] [--symptom ...] [--symptom-id ...] [--root-cause ...] [--keywords ...] [--type ...]
  kb.py match <消息>                      # 纯文本匹配
  kb.py match --json '<JSON>'             # 告警 payload 匹配
  kb.py cleanup                           # 清理超量记录
  kb.py validate                          # 检查条目结构与重复 id

环境变量:
  KB_DATA_DIR    数据目录（默认: ./data）
  KB_MAX_ENTRIES 最大条目数（默认: 200）
"""

import json, os, sys, re, time
from datetime import datetime

from kb_tz import get_kb_timezone

DATA_DIR = os.environ.get('KB_DATA_DIR', os.path.join(os.path.dirname(__file__), '..', 'data'))
KB_PATH = os.path.join(DATA_DIR, 'fault-kb.json')
MAX_ENTRIES = int(os.environ.get('KB_MAX_ENTRIES', 200))
CLEANUP_RETAIN = int(os.environ.get('KB_CLEANUP_RETAIN', 150))
TZ = get_kb_timezone()


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


def _strip_entry_runtime_fields(entries):
    """匹配过程产生的临时字段不应写入磁盘，避免污染 JSON / Git diff。"""
    for e in entries:
        for k in list(e.keys()):
            if k.startswith('_'):
                del e[k]


def save(data):
    _strip_entry_runtime_fields(data.get('entries', []))
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


def add(alert_msg, root_cause_id, note='', keywords=None, symptom='', symptom_id='', entry_type='specific'):
    """新增故障记录"""
    data = load()
    keywords = keywords or _extract_keywords(alert_msg)
    entry = {
        'id': next_id(data),
        'title': alert_msg[:80],
        'keywords': keywords,
        'type': entry_type,
        'symptom': symptom,
        'symptom_id': symptom_id,
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

    scored = []  # [(score, entry), ...]

    for e in data['entries']:
        match = e.get('match', {})

        # ── 旧格式兼容（无 match 字段） ──
        if not match:
            old_pattern = e.get('alert_pattern', '')
            if old_pattern and msg_text and re.search(old_pattern, msg_text, re.IGNORECASE):
                scored.append((1.0, e, None))
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
                scored.append((score, e, matched_kws))
                continue

        # ── 无关键词 → 用 message 正则保底 ──
        msg_pat = match.get('message', '')
        if not keywords and msg_pat and msg_text:
            if re.search(msg_pat, msg_text, re.IGNORECASE):
                scored.append((0.5, e, None))
                continue

    if not scored:
        return None

    # 按评分降序 → specific 优先于 catchall → id 升序
    scored.sort(key=lambda x: (
        -x[0],
        0 if x[1].get('type', 'specific') == 'specific' else 1,
        x[1].get('id', '')
    ))
    win_score, best, matched_kws = scored[0]

    # 更新命中次数（不向条目写入 _score / _matched_keywords，避免 save 污染数据文件）
    best['hit_count'] = best.get('hit_count', 0) + 1
    save(data)

    return _build_result(best, score=round(win_score, 3), matched_keywords=matched_kws)


def _build_result(e, score=None, matched_keywords=None):
    result = {
        'id': e['id'],
        'title': e.get('title', ''),
        'symptom': e.get('symptom', ''),
        'symptom_id': e.get('symptom_id', ''),
        'type': e.get('type', 'specific'),
        'root_cause': e.get('root_cause', ''),
        'root_cause_id': e.get('root_cause_id', ''),
        'recovery_action': e.get('recovery_action', ''),
        'note': e.get('note', ''),
    }
    if score is not None:
        result['_score'] = score
    if matched_keywords:
        result['_matched_keywords'] = matched_keywords
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
        print(f"  #{e['id']}  {e.get('title','')[:60]}  [{e.get('type','specific')}]")
        if kws:
            print(f"      关键词: {', '.join(kws[:8])}{'...' if len(kws) > 8 else ''}")
        print(f"      根因: {e.get('root_cause','')[:60]}")
        print(f"      命中: {e.get('hit_count',0)}次")
        print()


def find_entry(data, entry_id):
    for e in data['entries']:
        if e['id'] == entry_id:
            return e
    return None


def delete(entry_id):
    data = load()
    entry = find_entry(data, entry_id)
    if not entry:
        print(f"#{entry_id} 不存在")
        sys.exit(1)
    data['entries'].remove(entry)
    save(data)
    print(f"已删除 #{entry_id}")


def edit(entry_id, match_updates=None, **fields):
    data = load()
    entry = find_entry(data, entry_id)
    if not entry:
        print(f"#{entry_id} 不存在")
        sys.exit(1)
    for k, v in fields.items():
        if v is not None:
            entry[k] = v
    if match_updates:
        entry.setdefault('match', {}).update(match_updates)
    save(data)
    print(f"已更新 #{entry_id}")


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


def validate():
    """结构自检：便于 CI / 发布前检查 JSON 是否可被加载。"""
    data = load()
    errs = []
    ids_seen = set()
    required_root = ('id', 'root_cause_id')
    for i, e in enumerate(data.get('entries', [])):
        prefix = f"entries[{i}]"
        for fld in required_root:
            if fld not in e:
                errs.append(f"{prefix}: 缺少字段 {fld}")
        eid = e.get('id')
        if eid is not None:
            if eid in ids_seen:
                errs.append(f"重复 id: {eid}")
            ids_seen.add(eid)
        m = e.get('match') or {}
        an = m.get('alertname')
        if an:
            try:
                re.compile(an, re.IGNORECASE)
            except re.error as ex:
                errs.append(f"{prefix} match.alertname: {ex}")
        msg_pat = m.get('message')
        if msg_pat:
            try:
                re.compile(msg_pat, re.IGNORECASE)
            except re.error as ex:
                errs.append(f"{prefix} match.message: {ex}")
        excl = m.get('exclude')
        if excl:
            for part in str(excl).split('|'):
                p = part.strip()
                if not p:
                    continue
                try:
                    re.compile(r'\b' + p + r'\b', re.IGNORECASE)
                except re.error as ex:
                    errs.append(f"{prefix} match.exclude ({p!r}): {ex}")
    if errs:
        print('验证失败:')
        for x in errs:
            print(' ', x)
        sys.exit(1)
    print(f'[KB] 验证通过: {len(data.get("entries", []))} 条记录')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("用法: kb.py <list|add|delete|edit|match|cleanup> [...]")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd in ('list', '--help', '-h'):
        if cmd != 'list':
            print("用法: kb.py <list|add|delete|edit|match|cleanup> [...]")
            print()
            print("  list                         列出所有条目（按症状分组）")
            print("  add <消息> <根因ID> [备注]   新增记录（自动提取关键词）")
            print("       [--symptom S] [--symptom-id SID] [--type specific|catchall]")
            print("  delete <id>                  删除条目")
            print("  edit <id> [字段...]           修改条目字段（含 --match-alertname / --match-message / --match-exclude）")
            print("  match <消息>                 纯文本匹配")
            print("  match --json '<JSON>'         告警 payload 匹配")
            print("  cleanup                      清理超量记录")
            print("  validate                     校验 JSON 结构与正则字段")
            sys.exit(0)
        list_entries()

    elif cmd == 'add':
        if len(sys.argv) < 4:
            print("用法: kb.py add <消息> <根因ID> [备注] [--symptom S] [--symptom-id SID] [--type specific|catchall]")
            sys.exit(1)
        msg = sys.argv[2]
        rc_id = sys.argv[3]
        note = ''
        symptom = ''
        symptom_id = ''
        entry_type = 'specific'
        i = 4
        while i < len(sys.argv):
            if sys.argv[i] == '--symptom' and i + 1 < len(sys.argv):
                symptom = sys.argv[i + 1]; i += 2
            elif sys.argv[i] == '--symptom-id' and i + 1 < len(sys.argv):
                symptom_id = sys.argv[i + 1]; i += 2
            elif sys.argv[i] == '--type' and i + 1 < len(sys.argv):
                entry_type = sys.argv[i + 1]; i += 2
            else:
                note = sys.argv[i]; i += 1
        aid = add(msg, rc_id, note=note, symptom=symptom, symptom_id=symptom_id, entry_type=entry_type)
        print(f"KB_WRITTEN {aid}")

    elif cmd == 'delete':
        if len(sys.argv) < 3:
            print("用法: kb.py delete <id>")
            sys.exit(1)
        delete(sys.argv[2])

    elif cmd == 'edit':
        if len(sys.argv) < 3:
            print("用法: kb.py edit <id> [--title T] [--symptom S] [--symptom-id SID] [--root-cause RC] [--keywords K1,K2] [--type specific|catchall]")
            print("          [--match-alertname R] [--match-message R] [--match-exclude R]")
            sys.exit(1)
        entry_id = sys.argv[2]
        args = sys.argv[3:]
        fields = {}
        match_updates = {}
        i = 0
        while i < len(args):
            if args[i] == '--title' and i + 1 < len(args):
                fields['title'] = args[i + 1]; i += 2
            elif args[i] == '--symptom' and i + 1 < len(args):
                fields['symptom'] = args[i + 1]; i += 2
            elif args[i] == '--symptom-id' and i + 1 < len(args):
                fields['symptom_id'] = args[i + 1]; i += 2
            elif args[i] == '--root-cause' and i + 1 < len(args):
                fields['root_cause'] = args[i + 1]; i += 2
            elif args[i] == '--keywords' and i + 1 < len(args):
                fields['keywords'] = [k.strip() for k in args[i + 1].split(',') if k.strip()]; i += 2
            elif args[i] == '--type' and i + 1 < len(args):
                fields['type'] = args[i + 1]; i += 2
            elif args[i] == '--match-alertname' and i + 1 < len(args):
                match_updates['alertname'] = args[i + 1]; i += 2
            elif args[i] == '--match-message' and i + 1 < len(args):
                match_updates['message'] = args[i + 1]; i += 2
            elif args[i] == '--match-exclude' and i + 1 < len(args):
                match_updates['exclude'] = args[i + 1]; i += 2
            else:
                print(f"未知参数: {args[i]}"); sys.exit(1)
        if not fields and not match_updates:
            print("至少指定一个要修改的字段"); sys.exit(1)
        edit(entry_id, match_updates=match_updates or None, **fields)

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

    elif cmd == 'validate':
        validate()

    else:
        print(f"未知命令: {cmd}")
