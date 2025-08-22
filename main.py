import os
import csv
import smtplib
import time
import datetime
import json
from flask import Flask, request, jsonify, render_template_string, send_file, Response
from email.mime.text import MIMEText
from email.header import Header
from io import StringIO, BytesIO
from threading import Thread, Lock
from queue import Queue
from typing import Dict, List

app = Flask(__name__)

# ================== 常量与文件 ==================
DAILY_LIMIT = 450  # 单账号24小时限额（你可以按需调整）
RECIPIENTS_FILE = "recipients.json"
LOG_FILE_TXT = "send_log.txt"       # 纯文本累计（便于肉眼查看）
LOG_FILE_JSON = "send_log.json"     # JSON（前端恢复历史）
USAGE_FILE = "usage.json"           # 账户发送时间戳历史（用于24小时滚动统计）
ACCOUNTS_FILE = "accounts.json"     # 前端上传/删除的账号持久化

# ================== 全局状态 ==================
ACCOUNTS: List[Dict] = []          # {email, app_password, selected}
current_index = 0
RECIPIENTS: List[Dict] = []        # 待发
SENT_RECIPIENTS: List[Dict] = []   # 已发
SEND_QUEUE: List[Dict] = []        # 待执行任务
IS_SENDING = False
PAUSED = False
SEND_LOCK = Lock()
EVENT_SUBSCRIBERS: List[Queue] = []

# 用于 24小时滚动统计：email -> [timestamp_epoch_seconds, ...]
ACCOUNT_USAGE_HISTORY: Dict[str, List[float]] = {}

# ================== 工具函数：时区/时间 ==================
TZ_UTC8 = datetime.timezone(datetime.timedelta(hours=8))

def now_utc8_iso():
    return datetime.datetime.now(datetime.timezone.utc).astimezone(TZ_UTC8).strftime("%Y-%m-%d %H:%M:%S")

def prune_usage_older_than_24h():
    """清理超过24小时的发送记录"""
    cutoff = time.time() - 24 * 3600
    changed = False
    for email, stamps in list(ACCOUNT_USAGE_HISTORY.items()):
        new_list = [ts for ts in stamps if ts >= cutoff]
        if len(new_list) != len(stamps):
            ACCOUNT_USAGE_HISTORY[email] = new_list
            changed = True
    if changed:
        save_usage_history()

def usage_count_last_24h(email: str) -> int:
    prune_usage_older_than_24h()
    return len(ACCOUNT_USAGE_HISTORY.get(email, []))

def record_usage(email: str):
    ACCOUNT_USAGE_HISTORY.setdefault(email, []).append(time.time())
    prune_usage_older_than_24h()
    save_usage_history()

# ================== 工具函数：持久化 ==================
def safe_read_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return default
    return default

def save_recipients():
    with open(RECIPIENTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"pending": RECIPIENTS, "sent": SENT_RECIPIENTS}, f, ensure_ascii=False, indent=2)

def load_recipients():
    global RECIPIENTS, SENT_RECIPIENTS
    data = safe_read_json(RECIPIENTS_FILE, {"pending": [], "sent": []})
    RECIPIENTS = data.get("pending", [])
    SENT_RECIPIENTS = data.get("sent", [])

def save_accounts():
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(ACCOUNTS, f, ensure_ascii=False, indent=2)

def load_accounts_from_file():
    global ACCOUNTS
    ACCOUNTS = safe_read_json(ACCOUNTS_FILE, [])
    # 兼容 Render 环境变量（作为默认初始账号）
    if not ACCOUNTS:
        i = 1
        while True:
            email = os.getenv(f"EMAIL{i}")
            app_password = os.getenv(f"APP_PASSWORD{i}")
            if email and app_password:
                ACCOUNTS.append({"email": email, "app_password": app_password, "selected": True})
                i += 1
            else:
                break
        save_accounts()  # 写入文件，后续通过前端管理

def save_usage_history():
    with open(USAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(ACCOUNT_USAGE_HISTORY, f, ensure_ascii=False, indent=2)

def load_usage_history():
    global ACCOUNT_USAGE_HISTORY
    ACCOUNT_USAGE_HISTORY = safe_read_json(USAGE_FILE, {})

def append_log_line_txt(msg: str):
    with open(LOG_FILE_TXT, "a", encoding="utf-8") as f:
        f.write(msg + "\n")

def append_log_json(entry: Dict):
    # 限制日志长度，避免无穷增长（比如保留最近 5000 条）
    history = safe_read_json(LOG_FILE_JSON, [])
    history.append(entry)
    if len(history) > 5000:
        history = history[-5000:]
    with open(LOG_FILE_JSON, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

def read_log_json(limit: int = 500):
    history = safe_read_json(LOG_FILE_JSON, [])
    if limit and limit > 0:
        return history[-limit:]
    return history

# ================== SMTP 服务器选择（支持 Gmail/Hotmail/Outlook） ==================
def get_smtp_for_email(email: str):
    domain = (email or "").split("@")[-1].lower()
    # 常见 Hotmail/Outlook/Live/Office365
    hotmail_domains = {
        "hotmail.com", "outlook.com", "live.com", "msn.com",
        "hotmail.co.uk", "outlook.co.uk", "live.co.uk", "office365.com", "outlook.office365.com"
    }
    if domain in hotmail_domains or domain.endswith(".outlook.com") or domain.endswith(".office365.com"):
        return ("smtp.office365.com", 587)
    # 默认 Gmail
    return ("smtp.gmail.com", 587)

# ================== 事件与日志推送 ==================
def send_event(data):
    """向所有 SSE 订阅者推送事件"""
    for subscriber in list(EVENT_SUBSCRIBERS):
        try:
            subscriber.put(json.dumps(data))
        except:
            pass  # 某个断了就忽略

def log_progress(kind: str, email: str = "", account: str = "", message: str = ""):
    """
    kind: "info" | "success" | "error"
    """
    entry = {
        "time": now_utc8_iso(),
        "kind": kind,
        "email": email,
        "account": account,
        "message": message
    }
    append_log_json(entry)
    append_log_line_txt(f"[{entry['time']}] [{kind}] {email} {account} {message}".strip())
    send_event({"log": entry, "usage": get_account_usage_snapshot()})

def get_account_usage_snapshot():
    prune_usage_older_than_24h()
    snapshot = {}
    for acc in ACCOUNTS:
        snapshot[acc["email"]] = usage_count_last_24h(acc["email"])
    return snapshot

# ================== 账号轮询选择 ==================
def get_next_account():
    """只在被勾选(selected=True)且24小时计数未达 DAILY_LIMIT 的账号中轮询"""
    global current_index
    selected = [acc for acc in ACCOUNTS if acc.get("selected", True)]
    if not selected:
        return None
    # 从 current_index 开始寻找可用账号
    n = len(selected)
    for _ in range(n):
        acc = selected[current_index % n]
        current_index = (current_index + 1) % n
        if usage_count_last_24h(acc["email"]) < DAILY_LIMIT:
            return acc
    return None

# ================== 发信 ==================
def send_email(account, to_email, subject, body):
    smtp_host, smtp_port = get_smtp_for_email(account["email"])
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = account["email"]
        msg["To"] = to_email
        msg["Subject"] = Header(subject, "utf-8")

        server = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
        server.starttls()
        server.login(account["email"], account["app_password"])
        server.sendmail(account["email"], [to_email], msg.as_string())
        server.quit()

        # 记录 24 小时窗口内的发送
        record_usage(account["email"])
        return True, ""
    except Exception as e:
        return False, str(e)

# ================== 页面（完整 HTML 内嵌） ==================
@app.route("/", methods=["GET"])
def home():
    template = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<title>MailBot 后台</title>
<style>
    body { font-family: Arial,Helvetica,sans-serif; margin:0; padding:0; display:flex; height:100vh; background:#f5f5f5;}
    .sidebar { width:220px; background:#2f4050; color:#fff; display:flex; flex-direction:column; }
    .sidebar .nav { padding:15px; background:none; border:none; color:#fff; cursor:pointer; text-align:left; font-size:16px; border-bottom:1px solid #3c4b5a;}
    .sidebar .nav:hover { background:#1ab394;}
    .main { flex:1; padding:20px; overflow:auto;}
    .card { background:#fff; padding:15px; margin-bottom:15px; box-shadow:0 2px 5px rgba(0,0,0,0.1); border-radius:10px;}
    table { width:100%; border-collapse: collapse;}
    th, td { border:1px solid #ddd; padding:8px; text-align:left;}
    th { background:#f2f2f2;}
    .btn { padding:8px 14px; background:#1ab394; color:#fff; border:none; cursor:pointer; border-radius:8px;}
    .btn:hover { background:#18a689;}
    .btn-danger { background:#e74c3c;}
    .btn-danger:hover { background:#c0392b;}
    .btn-light { background:#ecf0f1; color:#2f4050;}
    .row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
    .input { padding:8px; border:1px solid #ccc; border-radius:8px; }
    .tag { display:inline-block; padding:2px 8px; border-radius:999px; background:#ecf0f1; margin-right:6px; font-size:12px;}
    .usage-list li { margin-bottom:4px; }
    .account-item { display:flex; align-items:center; justify-content:space-between; padding:8px 10px; border:1px solid #eee; border-radius:8px; margin-bottom:8px; }
    .account-left { display:flex; align-items:center; gap:10px;}
    .pill-danger { background:#fdecea; color:#c0392b; padding:4px 8px; border-radius:999px; font-size:12px; }
    .pill-ok { background:#eafaf1; color:#1e8449; padding:4px 8px; border-radius:999px; font-size:12px; }
    .muted { color:#888; font-size:12px;}
    #sendLog { max-height: 320px; overflow:auto; padding-left:18px;}
    #sendLog li { margin-bottom:6px; }
</style>
</head>
<body>
    <div class="sidebar">
        <button class="nav" onclick="showPage('recipients')">收件箱管理</button>
        <button class="nav" onclick="showPage('send')">邮件发送</button>
        <button class="nav" onclick="showPage('accounts')">账号管理</button>
    </div>
    <div class="main">
        <!-- 收件人管理 -->
        <div id="recipientsPage">
            <h2>收件箱管理</h2>
            <div class="card">
                <div class="row">
                    <input type="file" id="csvFile" />
                    <button class="btn" onclick="uploadCSV()">上传 CSV</button>
                    <button class="btn btn-light" onclick="downloadTemplate()">下载 CSV 模板</button>
                    <button class="btn btn-danger" onclick="clearRecipients()">一键清空列表</button>
                    <button class="btn" onclick="exportPending()">导出未发送</button>
                    <button class="btn" onclick="exportSent()">导出已发送</button>
                    <button class="btn" onclick="continueTask()">继续上次任务</button>
                    <span class="tag">分页显示</span>
                    <select id="pageSize" class="input" onchange="loadRecipients(1)">
                        <option value="10">10条/页</option>
                        <option value="50" selected>50条/页</option>
                        <option value="100">100条/页</option>
                    </select>
                </div>
            </div>
            <div class="card">
                <h3>收件箱列表</h3>
                <table id="recipientsTable">
                    <thead><tr><th>Email</th><th>Name</th><th>Real Name</th><th>操作</th></tr></thead>
                    <tbody></tbody>
                </table>
                <div class="row" style="margin-top:10px;">
                    <button class="btn" onclick="prevPage()">上一页</button>
                    <span id="pageInfo" class="muted"></span>
                    <button class="btn" onclick="nextPage()">下一页</button>
                </div>
            </div>
        </div>

        <!-- 邮件发送 -->
        <div id="sendPage" style="display:none;">
            <h2>邮件发送</h2>
            <div class="card">
                <div class="row">
                    <span class="tag">选择发送账号（轮询）</span>
                </div>
                <div id="accountCheckboxes" class="row" style="margin:10px 0;"></div>
                <div class="row">
                    <label>主题:</label>
                    <input type="text" id="subject" class="input" style="flex:1;" placeholder="可用变量 {name} {real_name}">
                </div>
                <div class="row" style="margin-top:10px;">
                    <label>正文:</label>
                </div>
                <textarea id="body" class="input" style="width:100%;height:150px;" placeholder="可用变量 {name} {real_name}"></textarea>
                <div class="row" style="margin-top:10px;">
                    <label>发送间隔(秒):</label>
                    <input type="number" id="interval" class="input" value="5" style="width:80px;">
                    <button class="btn" onclick="startSend()">开始发送</button>
                    <button class="btn btn-light" onclick="pauseSend()">暂停</button>
                    <button class="btn" onclick="resumeSend()">继续</button>
                    <span class="muted">时间戳为 UTC+8</span>
                </div>
            </div>
            <div class="card">
                <h3>实时发送进度（自动续接 + 刷新保留）</h3>
                <ul id="sendLog"></ul>
                <div class="row"><span class="muted">自动滚动</span>
                    <input type="checkbox" id="autoscroll" checked />
                </div>
            </div>
            <div class="card">
                <h3>账号发送统计（24小时滚动）</h3>
                <ul id="accountUsage" class="usage-list"></ul>
            </div>
        </div>

        <!-- 账号管理 -->
        <div id="accountsPage" style="display:none;">
            <h2>账号管理</h2>
            <div class="card">
                <div class="row">
                    <input type="file" id="accountFile" />
                    <button class="btn" onclick="uploadAccounts()">导入账号 CSV</button>
                    <span class="muted">CSV 列：email,app_password</span>
                </div>
            </div>
            <div class="card">
                <h3>账号列表</h3>
                <div id="accountList"></div>
            </div>
        </div>
    </div>

<script>
let evtSource = null;
let currentPage = 1;
let totalPages = 1;

// ---------- 页面切换 ----------
function showPage(page){
    document.getElementById('recipientsPage').style.display = page==='recipients'?'block':'none';
    document.getElementById('sendPage').style.display = page==='send'?'block':'none';
    document.getElementById('accountsPage').style.display = page==='accounts'?'block':'none';
    if(page==='recipients'){ loadRecipients(1); }
    if(page==='send'){ loadAccounts(); bootstrapLogAndUsage(); startEventSource(); }
    if(page==='accounts'){ loadAccountsList(); }
}

// ---------- 收件人 ----------
function pageSize(){ return parseInt(document.getElementById('pageSize').value); }

function loadRecipients(page){
    currentPage = page || 1;
    fetch(`/recipients?limit=${pageSize()}&page=${currentPage}`)
    .then(res=>res.json()).then(data=>{
        const tbody = document.querySelector('#recipientsTable tbody');
        tbody.innerHTML = '';
        data.items.forEach(r=>{
            const tr = document.createElement('tr');
            tr.innerHTML = \`<td>\${r.email||''}</td><td>\${r.name||''}</td><td>\${r.real_name||''}</td>
            <td><button class="btn btn-danger" onclick="deleteRecipient('\${r.email}')">删除</button></td>\`;
            tbody.appendChild(tr);
        });
        totalPages = data.total_pages;
        document.getElementById('pageInfo').innerText = \`第 \${data.page} / \${data.total_pages} 页（共 \${data.total} 条）\`;
    });
}
function prevPage(){ if(currentPage>1){ loadRecipients(currentPage-1);} }
function nextPage(){ if(currentPage<totalPages){ loadRecipients(currentPage+1);} }

function uploadCSV(){
    const file = document.getElementById('csvFile').files[0];
    if(!file){ alert("请选择文件"); return; }
    const formData = new FormData();
    formData.append('file', file);
    fetch('/upload-csv', {method:'POST', body:formData})
    .then(res=>res.json()).then(data=>{
        alert(data.message);
        loadRecipients(1);
    });
}
function deleteRecipient(email){
    fetch('/delete-recipient', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({email})})
    .then(res=>res.json()).then(data=>{ alert(data.message); loadRecipients(currentPage); });
}
function clearRecipients(){
    fetch('/clear-recipients', {method:'POST'}).then(res=>res.json()).then(data=>{ alert(data.message); loadRecipients(1); });
}
function downloadTemplate(){ window.location.href="/download-template"; }
function exportPending(){ window.location.href="/download-recipients?status=pending"; }
function exportSent(){ window.location.href="/download-recipients?status=sent"; }
function continueTask(){ window.location.href="/continue-task"; }

// ---------- 发送 ----------
function loadAccounts(){
    fetch('/accounts').then(res=>res.json()).then(data=>{
        const div = document.getElementById('accountCheckboxes');
        div.innerHTML = '';
        data.forEach(acc=>{
            const id = acc.email.replace(/[@.]/g,'_');
            div.innerHTML += \`<label class="tag"><input type="checkbox" id="\${id}" \${acc.selected?'checked':''} onchange="toggleAccount('\${acc.email}', this.checked)"> \${acc.email}</label>\`;
        });
    });
}
function toggleAccount(email, checked){
    fetch('/toggle-account', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({email,checked})});
}

function startSend(){
    const subject = document.getElementById('subject').value.trim();
    const body = document.getElementById('body').value.trim();
    const interval = parseInt(document.getElementById('interval').value);
    if(!subject || !body){ alert("请填写主题和正文"); return; }
    fetch('/send', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({subject,body,interval})})
    .then(res=>res.json()).then(data=>{ alert(data.message); });
    startEventSource(); // 确保连接
}
function pauseSend(){
    fetch('/pause-send', {method:'POST'})
    .then(res=>res.json()).then(data=>alert(data.message));
}
function resumeSend(){
    fetch('/resume-send', {method:'POST'})
    .then(res=>res.json()).then(data=>alert(data.message));
}

// ---------- 日志 + 用量 ----------
function bootstrapLogAndUsage(){
    // 拉历史日志
    fetch('/send-log?limit=500')
    .then(res=>res.json()).then(history=>{
        const log = document.getElementById('sendLog');
        log.innerHTML = '';
        history.forEach(appendLogLine);
        // 拉取当前 usage 快照
        refreshUsage();
    });
}
function appendLogLine(entry){
    const log = document.getElementById('sendLog');
    const li = document.createElement('li');
    const color = entry.kind==='success' ? '#1e8449' : (entry.kind==='error' ? '#c0392b' : '#2f4050');
    li.innerHTML = \`<span class="muted">\${entry.time}</span> <b style="color:\${color}">[\${entry.kind}]</b> \${entry.email||''} \${entry.account?('('+entry.account+')'):''} \${entry.message||''}\`;
    log.appendChild(li);
    if(document.getElementById('autoscroll').checked){
        log.scrollTop = log.scrollHeight;
    }
}
function refreshUsage(){
    fetch('/account-usage').then(res=>res.json()).then(data=>{
        const usage = document.getElementById('accountUsage');
        usage.innerHTML='';
        Object.keys(data).forEach(acc=>{
            const n = data[acc];
            const badge = n >= {{daily_limit}} ? '<span class="pill-danger">已达上限</span>' : '<span class="pill-ok">可发送</span>';
            const li = document.createElement('li');
            li.innerHTML = \`\${acc}: \${n} / {{daily_limit}} \${badge}\`;
            usage.appendChild(li);
        });
    });
}
function startEventSource(){
    if(evtSource){ evtSource.close(); }
    evtSource = new EventSource('/send-stream');
    evtSource.onmessage = function(e){
        const d = JSON.parse(e.data);
        if(d.log){ appendLogLine(d.log); }
        if(d.usage){
            const usage = document.getElementById('accountUsage');
            usage.innerHTML='';
            Object.keys(d.usage).forEach(acc=>{
                const n = d.usage[acc];
                const badge = n >= {{daily_limit}} ? '<span class="pill-danger">已达上限</span>' : '<span class="pill-ok">可发送</span>';
                const li = document.createElement('li');
                li.innerHTML = \`\${acc}: \${n} / {{daily_limit}} \${badge}\`;
                usage.appendChild(li);
            });
        }
    };
    evtSource.onerror = function(){
        // 断线自动重连（浏览器会自动重连，这里只做提示）
        console.log('SSE 连接中断，浏览器将自动重连...');
    };
}

// ---------- 账号管理 ----------
function uploadAccounts(){
    const file = document.getElementById('accountFile').files[0];
    if(!file){ alert("请选择账号 CSV"); return; }
    const formData = new FormData();
    formData.append('file', file);
    fetch('/upload-accounts', {method:'POST', body:formData})
    .then(res=>res.json()).then(data=>{
        alert(data.message);
        loadAccountsList();
        loadAccounts();
        refreshUsage();
    });
}
function loadAccountsList(){
    fetch('/accounts').then(res=>res.json()).then(data=>{
        const div = document.getElementById('accountList');
        div.innerHTML = '';
        data.forEach(acc=>{
            const row = document.createElement('div');
            row.className = 'account-item';
            row.innerHTML = \`
                <div class="account-left">
                    <span>\${acc.email}</span>
                    <span class="muted">\${acc.selected ? '已启用' : '未启用'}</span>
                </div>
                <div>
                    <button class="btn btn-danger" onclick="deleteAccount('\${acc.email}')">删除</button>
                </div>\`;
            div.appendChild(row);
        });
    });
}
function deleteAccount(email){
    fetch('/delete-account', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({email})})
    .then(res=>res.json()).then(data=>{
        alert(data.message);
        loadAccountsList();
        loadAccounts();
        refreshUsage();
    });
}

// 默认打开发送页并初始化
document.addEventListener('DOMContentLoaded', ()=>{ showPage('send'); });
</script>
</body>
</html>
    """
    return render_template_string(template, daily_limit=DAILY_LIMIT)

# ================== 发送控制与任务循环 ==================
@app.route("/send", methods=["POST"])
def start_send():
    global IS_SENDING, PAUSED
    data = request.json
    subject = data.get("subject")
    body = data.get("body")
    interval = int(data.get("interval",5))
    if not subject or not body:
        return jsonify({"message":"主题和正文不能为空"}), 400
    SEND_QUEUE.append({"subject":subject,"body":body,"interval":interval})
    if not IS_SENDING:
        IS_SENDING = True
        PAUSED = False
        t = Thread(target=send_worker_loop, daemon=True)
        t.start()
    return jsonify({"message":"邮件发送任务已启动"})

def send_worker_loop():
    global IS_SENDING, PAUSED
    while SEND_QUEUE:
        task = SEND_QUEUE[0]
        subject = task["subject"]
        body = task["body"]
        interval = task["interval"]

        if PAUSED:
            time.sleep(0.5)
            continue

        recipient = None
        with SEND_LOCK:
            if RECIPIENTS:
                recipient = RECIPIENTS.pop(0)

        if not recipient:
            break

        acc = get_next_account()
        if not acc:
            # 没有可用账号或都达上限
            log_progress("info", "", "", "没有可用账号或账号今日已达上限，等待1分钟重试…")
            time.sleep(60)
            # 把收件人放回队列前端，避免丢失
            with SEND_LOCK:
                RECIPIENTS.insert(0, recipient)
            save_recipients()
            continue

        # 个性化内容
        recipient_safe = {
            "name": recipient.get("name",""),
            "real_name": recipient.get("real_name","")
        }
        try:
            personalized_subject = subject.format(**recipient_safe)
            personalized_body = body.format(**recipient_safe)
        except Exception as e:
            log_progress("error", recipient.get("email",""), acc["email"], f"内容格式错误: {e}")
            with SEND_LOCK:
                RECIPIENTS.append(recipient)
            save_recipients()
            time.sleep(interval)
            continue

        success, err = send_email(acc, recipient["email"], personalized_subject, personalized_body)
        if success:
            SENT_RECIPIENTS.append(recipient)
            save_recipients()
            log_progress("success", recipient["email"], acc["email"], "发送成功")
        else:
            # 失败：记录并把该收件人放回队列末尾（避免阻塞）
            with SEND_LOCK:
                RECIPIENTS.append(recipient)
            save_recipients()
            log_progress("error", recipient["email"], acc["email"], f"发送失败: {err}")

        time.sleep(interval)

    IS_SENDING = False
    with SEND_LOCK:
        if SEND_QUEUE:
            SEND_QUEUE.pop(0)

@app.route("/pause-send", methods=["POST"])
def pause_send():
    global PAUSED
    PAUSED = True
    return jsonify({"message":"发送已暂停"})

@app.route("/resume-send", methods=["POST"])
def resume_send():
    global PAUSED
    PAUSED = False
    return jsonify({"message":"发送已继续"})

# ================== SSE：实时日志流 ==================
@app.route("/send-stream")
def send_stream():
    def event_stream():
        q = Queue()
        EVENT_SUBSCRIBERS.append(q)
        try:
            while True:
                data = q.get()
                yield f"data: {data}\n\n"
        except GeneratorExit:
            if q in EVENT_SUBSCRIBERS:
                EVENT_SUBSCRIBERS.remove(q)
    return Response(event_stream(), mimetype="text/event-stream")

# ================== 历史日志 & 账号用量接口 ==================
@app.route("/send-log")
def get_send_log():
    limit = int(request.args.get("limit", 500))
    return jsonify(read_log_json(limit))

@app.route("/account-usage")
def account_usage_api():
    return jsonify(get_account_usage_snapshot())

# ================== 收件人管理（分页） ==================
@app.route("/continue-task")
def continue_task():
    # 这里仅作为占位提示，让你可以从 UI 点“继续”
    return jsonify({"message":"若任务未完成，发送线程仍会继续运行；可直接在发送页查看实时进度"})

@app.route("/recipients", methods=["GET"])
def get_recipients():
    # 分页参数
    limit = max(1, int(request.args.get("limit", 50)))
    page = max(1, int(request.args.get("page", 1)))
    total = len(RECIPIENTS)
    total_pages = max(1, (total + limit - 1)//limit)
    if page > total_pages: page = total_pages
    start = (page - 1) * limit
    end = start + limit
    items = RECIPIENTS[start:end]
    return jsonify({"items": items, "page": page, "total_pages": total_pages, "total": total})

@app.route("/upload-csv", methods=["POST"])
def upload_csv():
    file = request.files.get('file')
    if not file:
        return jsonify({"message":"未选择文件"}), 400
    csv_data = file.read().decode('utf-8').splitlines()
    reader = csv.DictReader(csv_data)
    added = 0
    for row in reader:
        email = (row.get("email") or "").strip()
        if not email:
            continue
        RECIPIENTS.append({
            "email": email,
            "name": row.get("name",""),
            "real_name": row.get("real_name","")
        })
        added += 1
    save_recipients()
    log_progress("info", "", "", f"导入收件人 {added} 条")
    return jsonify({"message":f"CSV 上传成功，导入 {added} 条"})

@app.route("/delete-recipient", methods=["POST"])
def delete_recipient():
    data = request.json
    email = data.get("email")
    global RECIPIENTS
    before = len(RECIPIENTS)
    RECIPIENTS = [r for r in RECIPIENTS if r.get("email") != email]
    after = len(RECIPIENTS)
    save_recipients()
    return jsonify({"message": f"{email} 已删除（剩余 {after} / 原 {before}）"})

@app.route("/clear-recipients", methods=["POST"])
def clear_recipients():
    global RECIPIENTS
    n = len(RECIPIENTS)
    RECIPIENTS = []
    save_recipients()
    return jsonify({"message":f"收件人列表已清空（清理 {n} 条）"})

@app.route("/download-template")
def download_template():
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=["email","name","real_name"])
    writer.writeheader()
    output.seek(0)
    return send_file(
        BytesIO(output.getvalue().encode("utf-8")),
        mimetype="text/csv",
        download_name="template.csv",
        as_attachment=True
    )

@app.route("/download-recipients")
def download_recipients():
    status = request.args.get("status","pending")
    if status=="pending":
        data = RECIPIENTS
        filename="pending.csv"
    else:
        data = SENT_RECIPIENTS
        filename="sent.csv"
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=["email","name","real_name"])
    writer.writeheader()
    for r in data:
        writer.writerow(r)
    output.seek(0)
    return send_file(
        BytesIO(output.getvalue().encode("utf-8")),
        mimetype="text/csv",
        download_name=filename,
        as_attachment=True
    )

# ================== 账号管理（持久化） ==================
@app.route("/accounts")
def get_accounts():
    return jsonify(ACCOUNTS)

@app.route("/toggle-account", methods=["POST"])
def toggle_account():
    data = request.json
    email = data.get("email")
    checked = bool(data.get("checked"))
    for acc in ACCOUNTS:
        if acc["email"] == email:
            acc["selected"] = checked
            save_accounts()
            return jsonify({"message":"账号状态已更新"})
    return jsonify({"message":"未找到该账号"}), 404

@app.route("/upload-accounts", methods=["POST"])
def upload_accounts():
    file = request.files.get('file')
    if not file:
        return jsonify({"message":"未选择文件"}), 400
    csv_data = file.read().decode('utf-8').splitlines()
    reader = csv.DictReader(csv_data)
    added = 0
    for row in reader:
        email = (row.get("email") or "").strip()
        app_password = (row.get("app_password") or "").strip()
        if not email or not app_password:
            continue
        # 若已存在则更新密码，否则新增
        exists = False
        for acc in ACCOUNTS:
            if acc["email"].lower() == email.lower():
                acc["app_password"] = app_password
                acc["selected"] = True
                exists = True
                break
        if not exists:
            ACCOUNTS.append({"email":email, "app_password":app_password, "selected":True})
        ACCOUNT_USAGE_HISTORY.setdefault(email, [])
        added += 1
    save_accounts()
    save_usage_history()
    log_progress("info", "", "", f"导入/更新账号 {added} 个")
    return jsonify({"message":"账号上传成功"})

@app.route("/delete-account", methods=["POST"])
def delete_account():
    data = request.json
    email = data.get("email")
    global ACCOUNTS
    before = len(ACCOUNTS)
    ACCOUNTS = [acc for acc in ACCOUNTS if acc["email"] != email]
    ACCOUNT_USAGE_HISTORY.pop(email, None)
    save_accounts()
    save_usage_history()
    after = len(ACCOUNTS)
    return jsonify({"message": f"{email} 已删除（剩余 {after} / 原 {before}）"})

# ================== 启动 ==================
if __name__ == "__main__":
    load_recipients()
    load_accounts_from_file()
    load_usage_history()
    # 启动时广播一次当前 usage（方便新打开的前端立即看到）
    send_event({"usage": get_account_usage_snapshot()})
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, threaded=True)
