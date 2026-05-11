#!/usr/bin/env python3
"""
告警知识库 Web UI + REST API — SQLite 后端

零外部依赖（stdlib only），单文件可运行。
提供表格视图、vis.js 故障图谱、CRUD API。

用法:
  python3 kb-server.py [port]

默认端口 8888
环境变量:
  KB_DATA_DIR    数据目录（默认: ./data）
  KB_PORT        端口（默认: 8888）
"""

import json, os, re, sqlite3, sys, time, urllib.parse
from datetime import datetime

from kb_tz import get_kb_timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# ── 配置 ──────────────────────────────────────────────────
DATA_DIR = Path(os.environ.get('KB_DATA_DIR', Path(__file__).parent.parent / 'data'))
DB_PATH = DATA_DIR / 'fault-kb.db'
JSON_PATH = DATA_DIR / 'fault-kb.json'  # 用于迁移
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get('KB_PORT', 8888))
TZ = get_kb_timezone()
# len('/api/entries/') == 13，勿用 path[14:] 否则会截断 id（如 006→06 → 404）
API_ENTRIES_PREFIX = '/api/entries/'

# ── 数据库初始化 ─────────────────────────────────────────
def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id TEXT PRIMARY KEY,
            title TEXT DEFAULT '',
            keywords TEXT DEFAULT '',
            entry_type TEXT DEFAULT 'specific',
            symptom TEXT DEFAULT '',
            symptom_id TEXT DEFAULT '',
            alertname_pattern TEXT DEFAULT '',
            message_pattern TEXT DEFAULT '',
            exclude_pattern TEXT DEFAULT 'resolved|recover|ok|test|fake',
            root_cause TEXT DEFAULT '',
            root_cause_id INTEGER DEFAULT 0,
            recovery_action TEXT DEFAULT '',
            note TEXT DEFAULT '',
            created_at TEXT DEFAULT '',
            updated_at TEXT DEFAULT '',
            hit_count INTEGER DEFAULT 0
        )
    """)
    # 兼容旧数据库：新增列不存在时添加
    for col, typ in [('keywords', "TEXT DEFAULT ''"), ('priority', 'INTEGER DEFAULT 10'),
                     ('symptom', "TEXT DEFAULT ''"), ('symptom_id', "TEXT DEFAULT ''"), ('entry_type', "TEXT DEFAULT 'specific'")]:
        try:
            conn.execute(f"ALTER TABLE entries ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass  # 已存在
    conn.commit()
    return conn

# ── 从 JSON 迁移 ──────────────────────────────────────────
def migrate_json(conn):
    if not JSON_PATH.exists():
        return False
    try:
        data = json.loads(open(str(JSON_PATH)).read())
    except (json.JSONDecodeError, OSError):
        return False
    count = 0
    for e in data.get('entries', []):
        mid = e.get('match', {})
        existing = conn.execute("SELECT id FROM entries WHERE id=?", (e['id'],)).fetchone()
        if existing:
            continue
        keywords = e.get('keywords', [])
        conn.execute("""
            INSERT OR IGNORE INTO entries
            (id, title, keywords, entry_type, symptom, symptom_id,
             alertname_pattern, message_pattern, exclude_pattern,
             root_cause, root_cause_id, recovery_action, note, created_at, hit_count)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            e['id'],
            e.get('title', ''),
            json.dumps(keywords, ensure_ascii=False) if keywords else '',
            e.get('type', 'specific'),
            e.get('symptom', ''),
            e.get('symptom_id', ''),
            mid.get('alertname', e.get('alert_pattern', '')),
            mid.get('message', ''),
            mid.get('exclude', 'resolved|recover|ok|test|fake'),
            e.get('root_cause', ''),
            e.get('root_cause_id', 0),
            e.get('recovery_action', ''),
            e.get('note', ''),
            e.get('created_at', ''),
            e.get('hit_count', 0),
        ))
        count += 1
    conn.commit()
    return count > 0


def sync_json_from_db(conn):
    """把 SQLite 全量写回 fault-kb.json，与 kb.py / Hermes 读取路径一致。"""
    meta = {}
    if JSON_PATH.exists():
        try:
            meta = json.loads(JSON_PATH.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            meta = {}
    rows = conn.execute("SELECT * FROM entries ORDER BY id").fetchall()
    entries = []
    for r in rows:
        d = dict(r)
        kws = _parse_keywords(d.get('keywords', ''))
        entries.append({
            'id': d['id'],
            'title': d.get('title', ''),
            'keywords': kws,
            'type': d.get('entry_type', 'specific'),
            'symptom': d.get('symptom', ''),
            'symptom_id': d.get('symptom_id', ''),
            'match': {
                'alertname': d.get('alertname_pattern', ''),
                'message': d.get('message_pattern', ''),
                'exclude': d.get('exclude_pattern', 'resolved|recover|ok|test|fake'),
            },
            'root_cause': d.get('root_cause', ''),
            'root_cause_id': int(d['root_cause_id']) if d.get('root_cause_id') is not None else 0,
            'recovery_action': d.get('recovery_action', ''),
            'note': d.get('note', ''),
            'created_at': d.get('created_at', ''),
            'hit_count': int(d['hit_count']) if d.get('hit_count') is not None else 0,
        })
    out = {
        'version': meta.get('version', 4),
        'max_entries': meta.get('max_entries', 200),
        'created_at': meta.get('created_at') or datetime.now(TZ).isoformat(),
        'entries': entries,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(str(JSON_PATH), 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

# ── 辅助: 解析 keywords ──────────────────────────────────
def _parse_keywords(val):
    if not val:
        return []
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return []
    return val if isinstance(val, list) else []

# ── API 处理 ──────────────────────────────────────────────
def _row_to_dict(r):
    d = dict(r)
    d['keywords'] = _parse_keywords(d.get('keywords', ''))
    # 前端表格使用 e.type，与 entry_type 对齐
    if d.get('entry_type') is not None:
        d['type'] = d['entry_type']
    return d

def api_list(conn, params):
    q = params.get('q', [''])[0].strip()
    rc = params.get('rc', [''])[0].strip()
    sym = params.get('sym', [''])[0].strip()
    sort = params.get('sort', ['id'])[0].strip()
    order = 'DESC' if sort.startswith('-') else 'ASC'
    sort = sort.lstrip('-')
    allowed = {'id','hit_count','created_at','title','root_cause_id','entry_type','symptom_id'}
    if sort not in allowed:
        sort = 'id'

    sql = "SELECT * FROM entries WHERE 1=1"
    args = []
    if q:
        sql += " AND (title LIKE ? OR root_cause LIKE ? OR recovery_action LIKE ? OR keywords LIKE ?)"
        like = f'%{q}%'
        args.extend([like, like, like, like])
    if rc:
        sql += " AND root_cause_id=?"
        args.append(int(rc))
    if sym:
        sql += " AND symptom_id=?"
        args.append(sym)

    sql += f" ORDER BY {sort} {order}"
    rows = conn.execute(sql, args).fetchall()
    return [_row_to_dict(r) for r in rows]

def api_get(conn, eid):
    r = conn.execute("SELECT * FROM entries WHERE id=?", (eid,)).fetchone()
    return _row_to_dict(r) if r else None

def api_add(conn, data):
    now = datetime.now(TZ).isoformat()
    nid = data.get('id', '')
    if not nid:
        last = conn.execute("SELECT id FROM entries ORDER BY id DESC LIMIT 1").fetchone()
        nid = str(int(last['id']) + 1).zfill(3) if last else '001'
    keywords = data.get('keywords', [])
    conn.execute("""
        INSERT INTO entries
        (id, title, keywords, entry_type, symptom, symptom_id,
         alertname_pattern, message_pattern, exclude_pattern,
         root_cause, root_cause_id, recovery_action, note, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        nid,
        data.get('title', ''),
        json.dumps(keywords, ensure_ascii=False) if keywords else '',
        data.get('type') or data.get('entry_type') or 'specific',
        data.get('symptom', ''),
        data.get('symptom_id', ''),
        data.get('alertname_pattern', ''),
        data.get('message_pattern', ''),
        data.get('exclude_pattern', 'resolved|recover|ok|test|fake'),
        data.get('root_cause', ''),
        int(data.get('root_cause_id', 0)),
        data.get('recovery_action', ''),
        data.get('note', ''),
        data.get('created_at', now),
        now,
    ))
    conn.commit()
    sync_json_from_db(conn)
    return nid

def api_update(conn, eid, data):
    now = datetime.now(TZ).isoformat()
    if not conn.execute("SELECT 1 FROM entries WHERE id=?", (eid,)).fetchone():
        return False
    fields = []
    args = []
    for k in ('title','keywords','entry_type','symptom','symptom_id',
              'alertname_pattern','message_pattern','exclude_pattern',
              'root_cause','root_cause_id','recovery_action','note'):
        if k in data:
            v = data[k]
            if k == 'keywords' and isinstance(v, list):
                v = json.dumps(v, ensure_ascii=False)
            if k == 'root_cause_id':
                v = int(v) if v not in (None, '') else 0
            fields.append(f"{k}=?")
            args.append(v)
    if not fields:
        return False
    fields.append("updated_at=?")
    args.append(now)
    args.append(eid)
    conn.execute(f"UPDATE entries SET {', '.join(fields)} WHERE id=?", args)
    conn.commit()
    sync_json_from_db(conn)
    return True

def api_delete(conn, eid):
    conn.execute("DELETE FROM entries WHERE id=?", (eid,))
    conn.commit()
    deleted = conn.execute("SELECT changes()").fetchone()[0] > 0
    if deleted:
        sync_json_from_db(conn)
    return deleted

def api_stats(conn):
    total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    top = conn.execute("SELECT id, title, hit_count FROM entries ORDER BY hit_count DESC LIMIT 5").fetchall()
    by_rc = conn.execute("SELECT root_cause_id, COUNT(*) as cnt FROM entries GROUP BY root_cause_id ORDER BY cnt DESC").fetchall()
    return {
        'total': total,
        'top_hits': [dict(r) for r in top],
        'by_root_cause': [dict(r) for r in by_rc],
    }
def api_symptoms(conn, params):
    """返回按症状分组的数据"""
    sym = params.get('sym', [''])[0].strip()
    q = params.get('q', [''])[0].strip()
    sql = "SELECT * FROM entries WHERE 1=1"
    args = []
    if sym:
        sql += " AND symptom_id=?"
        args.append(sym)
    if q:
        sql += " AND (title LIKE ? OR root_cause LIKE ? OR keywords LIKE ?)"
        like = f'%{q}%'
        args.extend([like, like, like])
    sql += " ORDER BY symptom_id, id"
    rows = conn.execute(sql, args).fetchall()
    groups = {}
    for r in rows:
        r = _row_to_dict(r)
        sid = r.get('symptom_id') or 'other'
        if sid not in groups:
            groups[sid] = {
                'symptom_id': sid,
                'symptom': r.get('symptom') or '其他',
                'entries': []
            }
        groups[sid]['entries'].append(r)
    return list(groups.values())


def api_symptom_options(conn):
    """筛选下拉 / datalist：按 symptom_id 去重"""
    rows = conn.execute(
        """SELECT symptom_id, MAX(symptom) AS symptom FROM entries
           WHERE COALESCE(TRIM(symptom_id), '') != ''
           GROUP BY symptom_id ORDER BY symptom_id"""
    ).fetchall()
    out = []
    for r in rows:
        sid = r['symptom_id']
        name = (r['symptom'] or sid or '').strip()
        out.append({'symptom_id': sid, 'symptom': name})
    return out


def api_graph(conn):
    """三层图谱：症状 → 条目"""
    rows = conn.execute("SELECT * FROM entries ORDER BY symptom_id, id").fetchall()
    nodes = []
    edges = []
    seen_symptom = set()
    for r in rows:
        sym_id = r['symptom_id'] or 'other'
        sym_name = r['symptom'] or '其他'
        if sym_id not in seen_symptom:
            seen_symptom.add(sym_id)
            nodes.append({
                'id': f"sym_{sym_id}",
                'label': sym_name[:20],
                'title': sym_name,
                'group': 'symptom',
                'value': 15,
                'shape': 'hexagon',
            })
        entry_node_id = f"fault_{r['id']}"
        nodes.append({
            'id': entry_node_id,
            'label': f"#{r['id']} {r['title'][:20]}",
            'title': f"#{r['id']}: {r['title']}<br/>命中 {r['hit_count']} 次<br/>{r['root_cause'][:40]}",
            'group': 'fault',
            'value': max(5, r['hit_count'] + 5),
        })
        edges.append({
            'from': f"sym_{sym_id}",
            'to': entry_node_id,
            'label': r['root_cause'][:15],
            'arrows': 'to',
            'font': {'size': 10},
        })
    return {'nodes': nodes, 'edges': edges}

# ── HTML 前端 ─────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>故障知识库</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.6/standalone/umd/vis-network.min.js"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.6/dist/vis-network.min.css">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f5f5f5;color:#333;padding:20px}
.container{max-width:1200px;margin:0 auto}
h1{font-size:22px;margin-bottom:12px;color:#1a1a1a}
h1 small{font-size:13px;color:#999;font-weight:400;margin-left:8px}
.bar{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap}
.bar input{padding:8px 12px;border:1px solid #ddd;border-radius:6px;font-size:14px;flex:1;min-width:200px}
.bar select{padding:8px;border:1px solid #ddd;border-radius:6px;font-size:14px}
.bar .stats{font-size:13px;color:#666;padding:8px 0;margin-left:auto;white-space:nowrap}
.tabs{display:flex;gap:4px;margin-bottom:12px}
.tab{padding:8px 20px;border-radius:6px 6px 0 0;cursor:pointer;background:#e8e8e8;font-size:14px}
.tab.active{background:#fff;font-weight:600}
.panel{display:none;background:#fff;border-radius:0 6px 6px 6px;padding:16px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.panel.active{display:block}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid #eee}
th{background:#fafafa;font-weight:600;cursor:pointer;white-space:nowrap;user-select:none}
th:hover{background:#f0f0f0}
tr:hover{background:#f8f9ff}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600}
.badge-1{background:#ffe0e0;color:#c00}
.badge-2{background:#fff0d0;color:#a60}
.badge-3{background:#fee;color:#a00}
.badge-4{background:#e0f0ff;color:#06c}
.badge-5{background:#e8f5e9;color:#2a6}
.tag{display:inline-block;padding:2px 7px;margin:1px 2px;border-radius:4px;font-size:11px;background:#f0f0f0;color:#555;border:1px solid #e0e0e0}
#graph{width:100%;height:600px;border:1px solid #eee;border-radius:6px}
.detail-panel{position:fixed;top:0;right:-500px;width:480px;height:100%;background:#fff;box-shadow:-2px 0 20px rgba(0,0,0,.15);transition:right .3s;z-index:100;overflow-y:auto;padding:20px;box-sizing:border-box}
.detail-panel.open{right:0}
.detail-panel .close{position:absolute;top:14px;right:18px;cursor:pointer;font-size:22px;line-height:1;color:#999;z-index:2;padding:4px 6px}
.detail-panel .close:hover{color:#333}
#detailContent{padding-right:4px}
.detail-panel h2.detail-head{display:flex;flex-wrap:wrap;align-items:flex-start;gap:12px;font-size:18px;margin:0 0 12px 0;padding-right:52px;line-height:1.4}
.detail-panel h2.detail-head .detail-title{flex:1;min-width:0;word-break:break-word;padding-right:8px}
.detail-panel h2.detail-head .detail-actions{display:flex;gap:8px;flex-shrink:0;align-items:center;margin-top:2px;margin-right:44px}
.detail-actions .btn-edit,.detail-actions .btn-delete{margin-left:0}
.detail-panel .field{margin:8px 0}
.detail-panel .field label{font-size:12px;color:#888;display:block}
.detail-panel .field .val{font-size:14px;padding:4px 0}
pre{background:#f5f5f5;padding:8px;border-radius:4px;font-size:12px;overflow-x:auto;white-space:pre-wrap}
.loading{text-align:center;padding:40px;color:#999}
@media(max-width:768px){.bar .stats{display:none}.detail-panel{width:100%;right:-100%}}
.modal-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.45);z-index:200;justify-content:center;align-items:center}
.modal-overlay.show{display:flex}
.modal{background:#fff;border-radius:8px;width:520px;max-height:85vh;overflow-y:auto;padding:24px;box-shadow:0 8px 30px rgba(0,0,0,.2)}
.modal h3{font-size:17px;margin-bottom:16px;color:#1a1a1a}
.modal label{display:block;font-size:12px;color:#666;margin-bottom:3px;margin-top:12px}
.modal label:first-of-type{margin-top:0}
.modal input,.modal textarea,.modal select{width:100%;padding:8px 10px;border:1px solid #e0e0e0;border-radius:5px;font-size:13px;font-family:inherit;box-sizing:border-box}
.modal textarea{resize:vertical;min-height:60px}
.modal .btn-row{display:flex;gap:8px;justify-content:flex-end;margin-top:18px}
.modal .btn{padding:8px 20px;border:none;border-radius:5px;font-size:13px;cursor:pointer;font-family:inherit}
.modal .btn-primary{background:#4a90d9;color:#fff}
.modal .btn-primary:hover{background:#3a7bc8}
.modal .btn-cancel{background:#e0e0e0;color:#555}
.modal .btn-cancel:hover{background:#d0d0d0}
.btn-add{background:#4a90d9;color:#fff;border:none;padding:8px 14px;border-radius:6px;font-size:13px;cursor:pointer;white-space:nowrap}
.btn-add:hover{background:#3a7bc8}
.btn-edit{background:#4a90d9;color:#fff;border:none;padding:4px 12px;border-radius:4px;font-size:12px;cursor:pointer;margin-left:10px;vertical-align:middle}
.btn-edit:hover{background:#3a7bc8}
.btn-delete{background:#c0392b;color:#fff;border:none;padding:4px 12px;border-radius:4px;font-size:12px;cursor:pointer;margin-left:8px;vertical-align:middle}
.btn-delete:hover{background:#a93226}
</style>
</head>
<body>
<div class="container">
<h1>故障知识库 <small>specific / catchall</small></h1>
<div class="bar">
  <input type="text" id="search" placeholder="搜索标题、根因、关键词..." oninput="loadData()">
  <select id="symFilter" onchange="loadData()">
    <option value="">全部症状</option>
  </select>
  <select id="sort" onchange="loadData()">
    <option value="id">编号排序</option>
    <option value="-hit_count">命中最多</option>
    <option value="">类型</option>
    <option value="-created_at">最近添加</option>
  </select>
  <button class="btn-add" onclick="openAddModal()">＋ 新增</button>
  <div class="stats" id="stats"></div>
</div>
<div class="tabs">
  <div class="tab active" onclick="switchTab('table')">表格</div>
  <div class="tab" onclick="switchTab('graph')">图谱</div>
</div>
<div class="panel active" id="panel-table">
  <div id="loading" class="loading">加载中...</div>
  <table id="table" style="display:none">
    <thead><tr>
      <th>症状</th>
      <th onclick="sortBy('id')">#</th>
      <th>告警 / 根因</th>
      <th>类型</th>
      <th onclick="sortBy('hit_count')">命中</th>
    </tr></thead>
    <tbody id="tbody"></tbody>
  </table>
</div>
<div class="panel" id="panel-graph">
  <p style="font-size:12px;color:#888;margin-bottom:8px;line-height:1.5">图谱依赖 CDN（vis-network）；无外网时请改用本地或内网镜像静态文件，详见 README。</p>
  <div id="graph"></div>
</div>
</div>
<div class="detail-panel" id="detail">
  <span class="close" onclick="closeDetail()">&times;</span>
  <div id="detailContent"></div>
</div>
<div class="modal-overlay" id="modalOverlay" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <h3 id="modalTitle">新增条目</h3>
    <label>症状分类</label>
    <input list="symptom_list" id="f_symptom_id" placeholder="选择已有或输入新分类">
    <datalist id="symptom_list"></datalist>
    <label>类型</label>
    <select id="f_entry_type">
      <option value="specific">specific</option>
      <option value="catchall">catchall</option>
    </select>
    <label>标题</label>
    <input type="text" id="f_title" placeholder="简要描述">
    <label>根因</label>
    <textarea id="f_root_cause" placeholder="故障根因"></textarea>
    <label>关键词（逗号分隔）</label>
    <input type="text" id="f_keywords" placeholder="keyword1, keyword2">
    <label>恢复步骤</label>
    <textarea id="f_recovery_action" placeholder="恢复操作步骤"></textarea>
    <label>匹配 alertname</label>
    <input type="text" id="f_alertname_pattern" placeholder="正则表达式">
    <label>匹配 message</label>
    <input type="text" id="f_message_pattern" placeholder="正则表达式">
    <label>排除规则</label>
    <input type="text" id="f_exclude_pattern" value="resolved|recover|ok|test|fake">
    <label>备注</label>
    <textarea id="f_note" placeholder="补充说明"></textarea>
    <input type="hidden" id="f_id">
    <div class="btn-row">
      <button class="btn btn-cancel" onclick="closeModal()">取消</button>
      <button class="btn btn-primary" onclick="submitForm()">保存</button>
    </div>
  </div>
</div>
<script>
let allData = [];
let currentSort = 'id';
let currentOrder = 'ASC';

function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelector('.tab[onclick*="'+name+'"]').classList.add('active');
  document.getElementById('panel-'+name).classList.add('active');
  if (name === 'graph') loadGraph();
}

function refreshSymptomOptions(done) {
  fetch('/api/symptom-options').then(function(r){ return r.json(); }).then(function(opts){
    var sel = document.getElementById('symFilter');
    var prev = sel.value;
    sel.innerHTML = '<option value="">全部症状</option>';
    opts.forEach(function(o){
      var op = document.createElement('option');
      op.value = o.symptom_id;
      op.textContent = o.symptom || o.symptom_id;
      sel.appendChild(op);
    });
    var ok = false;
    for (var i = 0; i < sel.options.length; i++) {
      if (sel.options[i].value === prev) { ok = true; break; }
    }
    if (ok) sel.value = prev;
    var dl = document.getElementById('symptom_list');
    dl.innerHTML = '';
    opts.forEach(function(o){
      var opt = document.createElement('option');
      opt.value = o.symptom_id;
      opt.textContent = o.symptom || o.symptom_id;
      dl.appendChild(opt);
    });
    if (done) done();
  });
}

function loadData() {
  var q = document.getElementById('search').value;
  var sym = document.getElementById('symFilter').value;
  var sort = document.getElementById('sort').value;
  var url = '/api/symptoms';
  if (sym) url += '?sym=' + sym;
  if (q) url += (sym ? '&' : '?') + 'q=' + encodeURIComponent(q);
  fetch(url).then(r=>r.json()).then(data => {
    allData = data;
    document.getElementById('loading').style.display = 'none';
    document.getElementById('table').style.display = '';
    renderTable();
  });
}

function renderTable() {
  var tbody = document.getElementById('tbody');
  tbody.innerHTML = '';
  var total = 0;
  allData.forEach(function(g) {
    var entries = g.entries;
    var rowspan = entries.length;
    total += entries.length;
    entries.forEach(function(e, i) {
      var tr = document.createElement('tr');
      tr.style.cursor = 'pointer';
      tr.onclick = function(){ showDetail(e.id); };
      var rcRaw = e.root_cause != null ? String(e.root_cause) : '';
      var rcLabel = rcRaw ? rcRaw.slice(0, 35) : '—';
      var typeHtml = (e.type === 'catchall' ? '<span style="color:#999">catchall</span>' : '<span style="color:#2a6">specific</span>');
      var rowHtml = '';
      if (i === 0) {
        rowHtml += '<td rowspan="'+rowspan+'" style="background:#f8f9ff;font-weight:600;width:120px">'+esc(g.symptom)+'</td>';
      }
      rowHtml += '<td><b>#'+esc(e.id)+'</b></td>' +
        '<td><b>'+esc(e.title)+'</b><br/><span style="color:#888;font-size:11px">'+esc(rcLabel)+'</span></td>' +
        '<td>'+typeHtml+'</td>' +
        '<td>'+esc(String(e.hit_count != null ? e.hit_count : 0))+'</td>';
      tr.innerHTML = rowHtml;
      tbody.appendChild(tr);
    });
  });
  document.getElementById('stats').textContent = '共 '+total+' 条';
}

function showDetail(id) {
  var panel = document.getElementById('detail');
  var box = document.getElementById('detailContent');
  if (!panel || !box) return;
  var sid = encodeURIComponent(id);
  fetch('/api/entries/'+sid).then(function(r){
    return r.json().then(function(e){ return { ok: r.ok, status: r.status, e: e }; });
  }).then(function(res){
    if (!res.ok || res.e.error) {
      box.innerHTML = '<p style="color:#c00;padding:12px">加载失败（HTTP '+res.status+'）: '+esc(res.e.error || 'not found')+'</p>';
      panel.classList.add('open');
      return;
    }
    var e = res.e;
    var kwArr = Array.isArray(e.keywords) ? e.keywords : [];
    var kws = kwArr.map(function(k){ return '<span class="tag">'+esc(k)+'</span>'; }).join('');
    var safeId = String(e.id != null ? e.id : '').replace(/\\/g,'\\\\').replace(/'/g,"\\'");
    box.innerHTML =
      '<h2 class="detail-head"><span class="detail-title">#'+esc(e.id)+' '+esc(e.title)+'</span>' +
      '<span class="detail-actions">' +
      '<button type="button" class="btn-edit" onclick="openEditModal(\''+safeId+'\')">编辑</button>' +
      '<button type="button" class="btn-delete" onclick="deleteEntry(\''+safeId+'\')">删除</button></span></h2>' +
      '<div class="field"><label>症状分类</label><div class="val"><b>'+esc(e.symptom||'—')+'</b></div></div>' +
      '<div class="field"><label>类型</label><div class="val"><b>'+esc(e.type||e.entry_type||'specific')+'</b></div></div>' +
      '<div class="field"><label>根因</label><div class="val">'+esc(e.root_cause!=null?e.root_cause:'')+'</div></div>' +
      '<div class="field"><label>关键词</label><div class="val">'+(kws||'<span style="color:#999">未设置</span>')+'</div></div>' +
      '<div class="field"><label>恢复步骤</label><div class="val"><pre>'+esc(e.recovery_action||'无')+'</pre></div></div>' +
      '<div class="field"><label>匹配 alertname</label><div class="val">'+esc(e.alertname_pattern||'—')+'</div></div>' +
      '<div class="field"><label>匹配 message</label><div class="val">'+esc(e.message_pattern||'—')+'</div></div>' +
      '<div class="field"><label>排除规则</label><div class="val">'+esc(e.exclude_pattern||'—')+'</div></div>' +
      '<div class="field"><label>备注</label><div class="val">'+esc(e.note||'—')+'</div></div>' +
      '<div class="field"><label>命中 '+esc(String(e.hit_count!=null?e.hit_count:0))+' 次 | 创建 '+esc(e.created_at||'?')+'</label></div>';
    panel.classList.add('open');
  }).catch(function(err){
    box.innerHTML = '<p style="color:#c00;padding:12px">加载异常: '+esc(err && err.message ? err.message : String(err))+'</p>';
    panel.classList.add('open');
  });
}

function closeDetail() {
  document.getElementById('detail').classList.remove('open');
}

function deleteEntry(id) {
  if (!id) return;
  if (!confirm('确定删除条目 #' + id + '？删除后将同步更新 fault-kb.json，且不可撤销。')) return;
  var sid = encodeURIComponent(id);
  fetch('/api/entries/' + sid, { method: 'DELETE' })
    .then(function(r){ return r.json().then(function(j){ return { ok: r.ok, status: r.status, j: j }; }); })
    .then(function(res){
      if (!res.ok || res.j.error) {
        alert('删除失败（HTTP ' + res.status + '）: ' + (res.j.error || 'unknown'));
        return;
      }
      closeDetail();
      refreshSymptomOptions(function(){
        loadData();
        var gp = document.getElementById('panel-graph');
        if (gp && gp.classList.contains('active')) loadGraph();
      });
    })
    .catch(function(err){ alert('删除异常: ' + (err && err.message ? err.message : String(err))); });
}

function loadGraph() {
  var container = document.getElementById('graph');
  container.innerHTML = '<div class="loading">加载图谱...</div>';
  fetch('/api/graph').then(r=>r.json()).then(data => {
    var nodes = new vis.DataSet(data.nodes);
    var edges = new vis.DataSet(data.edges);
    var options = {
      nodes: {shape:'dot',size:12,font:{size:14,face:'Arial'},borderWidth:2},
      edges: {arrows:'to',smooth:true,font:{size:11}},
      groups: {
        symptom: {color:{background:'#6c5ce7',border:'#4a3ba8'},shape:'hexagon',font:{size:15,weight:'bold'}},
        fault: {color:{background:'#4a90d9',border:'#2a5fa8'}},
        root_cause: {color:{background:'#e8a838',border:'#b87a18'}},
      },
      physics:{barnesHut:{gravitationalConstant:-3000,springLength:200}},
      interaction:{hover:true,tooltipDelay:200},
      layout:{improvedLayout:true}
    };
    new vis.Network(container, {nodes,edges}, options);
  });
}

function sortBy(field) {
  if (currentSort === field) currentOrder = currentOrder === 'ASC' ? 'DESC' : 'ASC';
  else { currentSort = field; currentOrder = 'ASC'; }
  allData.sort((a,b) => {
    var va = a[field]||'', vb = b[field]||'';
    if (typeof va === 'number') return currentOrder==='ASC' ? va-vb : vb-va;
    return currentOrder==='ASC' ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
  });
  renderTable();
}

function esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function openAddModal() {
  document.getElementById('modalTitle').textContent = '新增条目';
  document.getElementById('f_id').value = '';
  document.getElementById('f_symptom_id').value = '';
  document.getElementById('f_entry_type').value = 'specific';
  document.getElementById('f_title').value = '';
  document.getElementById('f_root_cause').value = '';
  document.getElementById('f_keywords').value = '';
  document.getElementById('f_recovery_action').value = '';
  document.getElementById('f_alertname_pattern').value = '';
  document.getElementById('f_message_pattern').value = '';
  document.getElementById('f_exclude_pattern').value = 'resolved|recover|ok|test|fake';
  document.getElementById('f_note').value = '';
  document.getElementById('modalOverlay').classList.add('show');
}

function openEditModal(id) {
  fetch('/api/entries/' + id).then(function(r){ return r.json(); }).then(function(e) {
    document.getElementById('modalTitle').textContent = '编辑条目 #' + e.id;
    document.getElementById('f_id').value = e.id;
    document.getElementById('f_symptom_id').value = e.symptom_id || '';
    document.getElementById('f_entry_type').value = e.entry_type || 'specific';
    document.getElementById('f_title').value = e.title || '';
    document.getElementById('f_root_cause').value = e.root_cause || '';
    document.getElementById('f_keywords').value = (e.keywords || []).join(', ');
    document.getElementById('f_recovery_action').value = e.recovery_action || '';
    document.getElementById('f_alertname_pattern').value = e.alertname_pattern || '';
    document.getElementById('f_message_pattern').value = e.message_pattern || '';
    document.getElementById('f_exclude_pattern').value = e.exclude_pattern || '';
    document.getElementById('f_note').value = e.note || '';
    document.getElementById('modalOverlay').classList.add('show');
  });
}

function closeModal() {
  document.getElementById('modalOverlay').classList.remove('show');
}

var _PINYIN_MAP = {'内存':'neicun','使用率':'shiyonglv','高':'gao','服务':'fuwu','不可用':'bukeyong','存储':'cunchu','空间':'kongjian','不足':'buzu','查询':'chaxun','延迟':'yanchi','CPU':'cpu','满载':'manzai','过高':'guogao','过低':'guodi','磁盘':'cipan','占用':'zhanyong','网络':'wangluo','丢包':'diubao','超时':'chaoshi','连接':'lianjie','失败':'shibai','异常':'yichang','错误':'cuowu','警告':'jinggao','阈值':'yuzhi','触发':'chufa','恢复':'huifu','重启':'zhongqi','宕机':'dangji','中断':'zhongduan','卡顿':'kadun','响应':'xiangying','数据库':'shujuku','主从':'zhucong','同步':'tongbu','复制':'fuzhi','堆积':'duiji','队列':'duilie','消息':'xiaoxi','缓存':'huancun','命中':'mingzhong','过期':'guoqi','刷新':'shuaxin','配置':'peizhi','变更':'biangeng','部署':'bushi','升级':'shengji','版本':'banben','兼容':'jianrong','权限':'quanxian','认证':'renzheng','授权':'shouquan','证书':'zhengshu','加密':'jiami','解密':'jiemi','负载':'fuzai','均衡':'junheng','限流':'xianliu','熔断':'rongduan','降级':'jiangji','兜底':'doudi','告警':'gaojing','监控':'jiankong','日志':'rizhi','指标':'zhibiao','链路':'lianlu','追踪':'zhuizong','跨度':'kuadu','采样':'caiyang','聚合':'juhe','统计':'tongji','分析':'fenxi','报表':'baobiao','仪表盘':'yibiaopan','面板':'mianban','视图':'shitu','图表':'tubiao','趋势':'qushi','对比':'duibi','基线':'jixian','偏差':'piancha','波动':'bodong','峰值':'fengzhi','谷值':'guzhi','均值':'junzhi','百分位':'baifenwei','方差':'fangcha','标准差':'biaozhuncha','概率':'gailv','置信':'zhixin','区间':'qujian','样本':'yangben','总体':'zongti','抽样':'chouyang','误差':'wucha','精度':'jingdu','准确':'zhunque','精确':'jingque','分类':'fenlei','聚类':'julei','特征':'tezheng','工程':'gongcheng','选择':'xuanze','提取':'tiqu','构造':'gouzao','变换':'bianhuan','标准化':'biaozhunhua','编码':'bianma','向量':'xiangliang','矩阵':'juzhen','优化':'youhua','损失':'sunshi','函数':'hanshu','激活':'jihuo','传播':'chuanbo','批量':'piliang','大小':'daxiao','衰减':'shuaijian','动量':'dongliang','正则':'zhengze','验证':'yanzheng','测试':'ceshi','评估':'pinggu','曲线':'quxian','决策':'juece','树':'shu','深度':'shendu','学习':'xuexi','模型':'moxing','训练':'xunlian','预测':'yuce','回归':'huigui','降维':'jiangwei','嵌入':'qianru','排序':'paixu','分词':'fenci','词频':'cipin','检索':'jiansuo','索引':'suoyin','相似度':'xiangsidu','距离':'juli','密度':'midu','划分':'huafen','轮廓':'lunkuo','系数':'xishu','集中':'jizhong','CPU使用率':'cpu_shiyonglv','IO':'io','读写':'duxie','带宽':'daikuan','流量':'liuliang','入侵':'ruqin','攻击':'gongji','漏洞':'loudong','补丁':'buding','进程':'jincheng','线程':'xiancheng','死锁':'sisuo','内存泄漏':'neicunxielou','泄漏':'xielou','溢出':'yichu','OOM':'oom','GC':'gc','回收':'huishou','堆':'dui','栈':'zhan','句柄':'jubing','文件描述符':'wenjianshumiaofu','连接池':'lianjiechi','线程池':'xianchengchi','队列积压':'duiliejiya','积压':'jiya'}
function _pinyinId(s) {
  var result = s;
  // 先尝试匹配较长的词
  var keys = Object.keys(_PINYIN_MAP).sort(function(a,b){return b.length-a.length;});
  for (var i = 0; i < keys.length; i++) {
    result = result.split(keys[i]).join(_PINYIN_MAP[keys[i]]);
  }
  // 保留字母、数字、下划线，其余转下划线，合并连续下划线，去首尾
  return result.toLowerCase().replace(/[^a-z0-9_]+/g,'_').replace(/^_+|_+$/g,'').replace(/_+/g,'_') || 'custom';
}
function submitForm() {
  var id = document.getElementById('f_id').value;
  var kwRaw = document.getElementById('f_keywords').value.trim();
  var keywords = kwRaw ? kwRaw.split(',').map(function(s){ return s.trim(); }).filter(Boolean) : [];
  var symInput = document.getElementById('f_symptom_id');
  var symVal = symInput.value.trim();
  // 在 datalist 中查找已知选项
  var dl = document.getElementById('symptom_list');
  var knownOpts = {};
  Array.prototype.forEach.call(dl.options, function(opt) {
    knownOpts[opt.value] = opt.textContent;
  });
  var symptom_id, symptom;
  if (knownOpts[symVal]) {
    // 选择了已知分类
    symptom_id = symVal;
    symptom = knownOpts[symVal];
  } else if (symVal) {
    // 全新分类：自动生成 ID
    symptom_id = _pinyinId(symVal);
    symptom = symVal;
  } else {
    symptom_id = '';
    symptom = '';
  }
  var body = {
    symptom_id: symptom_id,
    symptom: symptom,
    entry_type: document.getElementById('f_entry_type').value,
    title: document.getElementById('f_title').value.trim(),
    root_cause: document.getElementById('f_root_cause').value.trim(),
    keywords: keywords,
    recovery_action: document.getElementById('f_recovery_action').value.trim(),
    alertname_pattern: document.getElementById('f_alertname_pattern').value.trim(),
    message_pattern: document.getElementById('f_message_pattern').value.trim(),
    exclude_pattern: document.getElementById('f_exclude_pattern').value.trim(),
    note: document.getElementById('f_note').value.trim()
  };
  var url, method;
  if (id) {
    url = '/api/entries/' + id;
    method = 'PUT';
  } else {
    url = '/api/entries';
    method = 'POST';
  }
  fetch(url, {method: method, headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)})
    .then(function(r){
      return r.json().then(function(j){
        if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
        return j;
      });
    })
    .then(function() {
      closeModal();
      closeDetail();
      refreshSymptomOptions(loadData);
    })
    .catch(function(err) {
      alert('保存失败: ' + (err && err.message ? err.message : err));
    });
}

refreshSymptomOptions(loadData);
</script>
</body>
</html>"""

# ── HTTP 服务 ─────────────────────────────────────────────
class KBHandler(BaseHTTPRequestHandler):
    conn = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        try:
            if path == '/':
                self.send_html(200, HTML)
            elif path == '/api/entries':
                self.send_json(api_list(self.conn, params))
            elif path.startswith(API_ENTRIES_PREFIX) and len(path) > len(API_ENTRIES_PREFIX):
                eid = urllib.parse.unquote(path[len(API_ENTRIES_PREFIX):])
                e = api_get(self.conn, eid)
                if e:
                    self.send_json(e)
                else:
                    self.send_json({'error':'not found'}, 404)
            elif path == '/api/stats':
                self.send_json(api_stats(self.conn))
            elif path == '/api/symptoms':
                self.send_json(api_symptoms(self.conn, params))
            elif path == '/api/symptom-options':
                self.send_json(api_symptom_options(self.conn))
            elif path == '/api/graph':
                self.send_json(api_graph(self.conn))
            else:
                self.send_json({'error':'not found'}, 404)
        except Exception as e:
            self.send_json({'error':str(e)}, 500)

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length else b'{}'
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.send_json({'error':'invalid json'}, 400)
            return
        try:
            if self.path == '/api/entries':
                nid = api_add(self.conn, data)
                e = api_get(self.conn, nid)
                self.send_json(e, 201)
            else:
                self.send_json({'error':'not found'}, 404)
        except Exception as e:
            self.send_json({'error':str(e)}, 500)

    def do_PUT(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith(API_ENTRIES_PREFIX) and len(path) > len(API_ENTRIES_PREFIX):
            eid = urllib.parse.unquote(path[len(API_ENTRIES_PREFIX):])
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length) if length else b'{}'
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                self.send_json({'error': 'invalid json'}, 400)
                return
            if api_update(self.conn, eid, data):
                e = api_get(self.conn, eid)
                self.send_json(e)
            else:
                self.send_json({'error':'not found'}, 404)
        else:
            self.send_json({'error':'not found'}, 404)

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith(API_ENTRIES_PREFIX) and len(path) > len(API_ENTRIES_PREFIX):
            eid = urllib.parse.unquote(path[len(API_ENTRIES_PREFIX):])
            if api_delete(self.conn, eid):
                self.send_json({'deleted': eid})
            else:
                self.send_json({'error':'not found'}, 404)
        else:
            self.send_json({'error':'not found'}, 404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,PUT,DELETE,OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def send_html(self, status, html):
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))

    def log_message(self, fmt, *args):
        pass  # 安静运行

# ── 启动 ──────────────────────────────────────────────────
if __name__ == '__main__':
    print(f"[KB] 初始化数据库: {DB_PATH}")
    conn = init_db()
    KBHandler.conn = conn
    if migrate_json(conn):
        print(f"[KB] 已从 JSON 迁移数据到 SQLite")
    total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    print(f"[KB] 数据库共 {total} 条记录")
    server = HTTPServer(('0.0.0.0', PORT), KBHandler)
    print(f"[KB] Web UI: http://0.0.0.0:{PORT}")
    print(f"[KB] API:   http://0.0.0.0:{PORT}/api/entries")
    print(f"[KB] 图谱:  http://0.0.0.0:{PORT}/api/graph")
    print(f"[KB] 按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
        conn.close()
        print("\n[KB] 已停止")
