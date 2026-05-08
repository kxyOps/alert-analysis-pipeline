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
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# ── 配置 ──────────────────────────────────────────────────
DATA_DIR = Path(os.environ.get('KB_DATA_DIR', Path(__file__).parent.parent / 'data'))
DB_PATH = DATA_DIR / 'fault-kb.db'
JSON_PATH = DATA_DIR / 'fault-kb.json'  # 用于迁移
PORT = int(os.environ.get('KB_PORT', sys.argv[1])) if len(sys.argv) > 1 or 'KB_PORT' in os.environ else 8888
TZ = timezone(timedelta(hours=8))

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
        conn.execute("""
            INSERT OR IGNORE INTO entries
            (id, title, alertname_pattern, message_pattern, exclude_pattern,
             root_cause, root_cause_id, recovery_action, note, created_at, hit_count)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            e['id'],
            e.get('title', ''),
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

# ── API 处理 ──────────────────────────────────────────────
def api_list(conn, params):
    q = params.get('q', [''])[0].strip()
    rc = params.get('rc', [''])[0].strip()
    sort = params.get('sort', ['id'])[0].strip()
    order = 'DESC' if sort.startswith('-') else 'ASC'
    sort = sort.lstrip('-')
    allowed = {'id','hit_count','created_at','title','root_cause_id'}
    if sort not in allowed:
        sort = 'id'

    sql = "SELECT * FROM entries WHERE 1=1"
    args = []
    if q:
        sql += " AND (title LIKE ? OR root_cause LIKE ? OR recovery_action LIKE ?)"
        like = f'%{q}%'
        args.extend([like, like, like])
    if rc:
        sql += " AND root_cause_id=?"
        args.append(int(rc))

    sql += f" ORDER BY {sort} {order}"
    rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]

def api_get(conn, eid):
    r = conn.execute("SELECT * FROM entries WHERE id=?", (eid,)).fetchone()
    return dict(r) if r else None

def api_add(conn, data):
    now = datetime.now(TZ).isoformat()
    nid = data.get('id', '')
    if not nid:
        last = conn.execute("SELECT id FROM entries ORDER BY id DESC LIMIT 1").fetchone()
        nid = str(int(last['id']) + 1).zfill(3) if last else '001'
    conn.execute("""
        INSERT INTO entries
        (id, title, alertname_pattern, message_pattern, exclude_pattern,
         root_cause, root_cause_id, recovery_action, note, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        nid,
        data.get('title', ''),
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
    return nid

def api_update(conn, eid, data):
    now = datetime.now(TZ).isoformat()
    fields = []
    args = []
    for k in ('title','alertname_pattern','message_pattern','exclude_pattern',
              'root_cause','root_cause_id','recovery_action','note'):
        if k in data:
            fields.append(f"{k}=?")
            args.append(data[k])
    if not fields:
        return False
    fields.append("updated_at=?")
    args.append(now)
    args.append(eid)
    conn.execute(f"UPDATE entries SET {', '.join(fields)} WHERE id=?", args)
    conn.commit()
    return conn.execute("SELECT changes()").fetchone()[0] > 0

def api_delete(conn, eid):
    conn.execute("DELETE FROM entries WHERE id=?", (eid,))
    conn.commit()
    return conn.execute("SELECT changes()").fetchone()[0] > 0

def api_stats(conn):
    total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    top = conn.execute("SELECT id, title, hit_count FROM entries ORDER BY hit_count DESC LIMIT 5").fetchall()
    by_rc = conn.execute("SELECT root_cause_id, COUNT(*) as cnt FROM entries GROUP BY root_cause_id ORDER BY cnt DESC").fetchall()
    return {
        'total': total,
        'top_hits': [dict(r) for r in top],
        'by_root_cause': [dict(r) for r in by_rc],
    }

def api_graph(conn):
    """返回 vis.js 可用的 nodes + edges"""
    rows = conn.execute("SELECT id, title, root_cause_id, root_cause, hit_count FROM entries").fetchall()
    nodes = []
    edges = []
    seen_rc = set()
    for r in rows:
        nodes.append({
            'id': f"fault_{r['id']}",
            'label': f"#{r['id']} {r['title'][:30]}",
            'title': f"#{r['id']}: {r['title']}<br/>命中 {r['hit_count']} 次",
            'group': 'fault',
            'value': max(5, r['hit_count'] + 5),
        })
        rc_key = f"rc_{r['root_cause_id']}"
        if rc_key not in seen_rc:
            seen_rc.add(rc_key)
            nodes.append({
                'id': rc_key,
                'label': f"根因 #{r['root_cause_id']}: {r['root_cause'][:30]}",
                'title': r['root_cause'],
                'group': 'root_cause',
                'value': 10,
            })
        edges.append({'from': f"fault_{r['id']}", 'to': rc_key, 'label': '根因'})
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
th{background:#fafafa;font-weight:600;cursor:pointer;white-space:nowrap}
th:hover{background:#f0f0f0}
tr:hover{background:#f8f9ff}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600}
.badge-1{background:#ffe0e0;color:#c00}
.badge-2{background:#fff0d0;color:#a60}
.badge-3{background:#fee;color:#a00}
.badge-4{background:#e0f0ff;color:#06c}
.badge-5{background:#e8f5e9;color:#2a6}
#graph{width:100%;height:600px;border:1px solid #eee;border-radius:6px}
.detail-panel{position:fixed;top:0;right:-500px;width:480px;height:100%;background:#fff;box-shadow:-2px 0 20px rgba(0,0,0,.15);transition:right .3s;z-index:100;overflow-y:auto;padding:20px}
.detail-panel.open{right:0}
.detail-panel h2{font-size:18px;margin-bottom:8px}
.detail-panel .close{float:right;cursor:pointer;font-size:20px;color:#999}
.detail-panel .field{margin:8px 0}
.detail-panel .field label{font-size:12px;color:#888;display:block}
.detail-panel .field .val{font-size:14px;padding:4px 0}
pre{background:#f5f5f5;padding:8px;border-radius:4px;font-size:12px;overflow-x:auto;white-space:pre-wrap}
.loading{text-align:center;padding:40px;color:#999}
@media(max-width:768px){.bar .stats{display:none}.detail-panel{width:100%;right:-100%}}
</style>
</head>
<body>
<div class="container">
<h1>故障知识库</h1>
<div class="bar">
  <input type="text" id="search" placeholder="搜索标题、根因、恢复步骤..." oninput="loadData()">
  <select id="rcFilter" onchange="loadData()">
    <option value="">全部根因</option>
    <option value="1">1-内存超限</option>
    <option value="2">2-连接失败</option>
    <option value="3">3-进程宕机</option>
    <option value="4">4-磁盘空间</option>
    <option value="5">5-慢查询</option>
  </select>
  <select id="sort" onchange="loadData()">
    <option value="id">编号排序</option>
    <option value="-hit_count">命中最多</option>
    <option value="-created_at">最近添加</option>
  </select>
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
      <th onclick="sortBy('id')">#</th>
      <th onclick="sortBy('title')">标题</th>
      <th onclick="sortBy('root_cause_id')">根因</th>
      <th onclick="sortBy('hit_count')">命中</th>
      <th onclick="sortBy('created_at')">记录时间</th>
    </tr></thead>
    <tbody id="tbody"></tbody>
  </table>
</div>
<div class="panel" id="panel-graph">
  <div id="graph"></div>
</div>
</div>
<div class="detail-panel" id="detail">
  <span class="close" onclick="closeDetail()">&times;</span>
  <div id="detailContent"></div>
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

function loadData() {
  var q = document.getElementById('search').value;
  var rc = document.getElementById('rcFilter').value;
  var sort = document.getElementById('sort').value;
  var url = '/api/entries?q='+encodeURIComponent(q)+'&rc='+rc+'&sort='+sort;
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
  allData.forEach(e => {
    var tr = document.createElement('tr');
    tr.style.cursor = 'pointer';
    tr.onclick = function(){ showDetail(e.id); };
    var rcNames = {1:'内存超限',2:'连接失败',3:'进程宕机',4:'磁盘空间',5:'慢查询'};
    var rcName = rcNames[e.root_cause_id] || '其他';
    tr.innerHTML = '<td><b>#'+e.id+'</b></td>' +
      '<td>'+esc(e.title)+'</td>' +
      '<td><span class="badge badge-'+e.root_cause_id+'">'+rcName+'</span></td>' +
      '<td>'+e.hit_count+'</td>' +
      '<td>'+(e.created_at||'').slice(0,10)+'</td>';
    tbody.appendChild(tr);
  });
  document.getElementById('stats').textContent = '共 '+allData.length+' 条';
}

function showDetail(id) {
  fetch('/api/entries/'+id).then(r=>r.json()).then(e => {
    var panel = document.getElementById('detail');
    var rcNames = {1:'内存超限',2:'连接失败',3:'进程宕机',4:'磁盘空间',5:'慢查询'};
    panel.querySelector('#detailContent').innerHTML =
      '<h2>#'+e.id+' '+esc(e.title)+'</h2>' +
      '<div class="field"><label>根因</label><div class="val">'+(rcNames[e.root_cause_id]||'')+' — '+esc(e.root_cause||'')+'</div></div>' +
      '<div class="field"><label>恢复步骤</label><div class="val"><pre>'+esc(e.recovery_action||'无')+'</pre></div></div>' +
      '<div class="field"><label>匹配 alertname</label><div class="val">'+esc(e.alertname_pattern||'—')+'</div></div>' +
      '<div class="field"><label>匹配 message</label><div class="val">'+esc(e.message_pattern||'—')+'</div></div>' +
      '<div class="field"><label>排除规则</label><div class="val">'+esc(e.exclude_pattern||'—')+'</div></div>' +
      '<div class="field"><label>备注</label><div class="val">'+esc(e.note||'—')+'</div></div>' +
      '<div class="field"><label>命中 '+e.hit_count+' 次 | 创建 '+ (e.created_at||'?') +'</label></div>';
    panel.classList.add('open');
  });
}

function closeDetail() {
  document.getElementById('detail').classList.remove('open');
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

loadData();
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
            elif path.startswith('/api/entries/') and len(path) > 14:
                eid = path[14:]
                e = api_get(self.conn, eid)
                if e:
                    self.send_json(e)
                else:
                    self.send_json({'error':'not found'}, 404)
            elif path == '/api/stats':
                self.send_json(api_stats(self.conn))
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
        if self.path.startswith('/api/entries/') and len(self.path) > 14:
            eid = self.path[14:]
            length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(length)) if length else {}
            if api_update(self.conn, eid, data):
                e = api_get(self.conn, eid)
                self.send_json(e)
            else:
                self.send_json({'error':'not found'}, 404)
        else:
            self.send_json({'error':'not found'}, 404)

    def do_DELETE(self):
        if self.path.startswith('/api/entries/') and len(self.path) > 14:
            eid = self.path[14:]
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
