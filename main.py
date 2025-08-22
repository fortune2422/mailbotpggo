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

app = Flask(__name__)

# ================== 配置 ==================
DAILY_LIMIT = 450
RECIPIENTS_FILE = "recipients.json"
LOG_FILE_JSON = "send_log.json"         # 持久化日志（数组）
USAGE_FILE_JSON = "account_usage.json"  # 账号用量持久化

# ================== 账号加载与持久化 ==================
def load_accounts_from_env():
    accounts = []
    i = 1
    while True:
        email = os.getenv(f"EMAIL{i}")
        app_password = os.getenv(f"APP_PASSWORD{i}")
        smtp_server = os.getenv(f"SMTP_SERVER{i}")
        smtp_port = os.getenv(f"SMTP_PORT{i}")
        if email and app_password:
            rec = {"email": email, "app_password": app_password, "selected": True}
            if smtp_server:
                rec["smtp_server"] = smtp_server
            if smtp_port:
                try: rec["smtp_port"] = int(smtp_port)
                except: pass
            accounts.append(rec)
            i += 1
        else:
            break
    return accounts

ACCOUNTS = load_accounts_from_env()
current_index = 0

def load_usage():
    if os.path.exists(USAGE_FILE_JSON):
        try:
            with open(USAGE_FILE_JSON, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_usage():
    with open(USAGE_FILE_JSON, "w", encoding="utf-8") as f:
        json.dump(account_usage, f, ensure_ascii=False, indent=2)

# 账号使用统计，每条记录带时间戳
account_usage = load_usage()
for acc in ACCOUNTS:
    account_usage.setdefault(acc["email"], [])

last_reset_date = datetime.date.today()

# ================== 收件人列表 ==================
RECIPIENTS = []
SENT_RECIPIENTS = []

# ================== 发送控制 ==================
SEND_QUEUE = []
IS_SENDING = False
PAUSED = False
SEND_LOCK = Lock()
EVENT_SUBSCRIBERS = []

# ================== 日志持久化 ==================
def load_logs():
    if os.path.exists(LOG_FILE_JSON):
        try:
            with open(LOG_FILE_JSON, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return []
    return []

def save_logs(logs):
    with open(LOG_FILE_JSON, "w", encoding="utf-8") as f:
        json.dump(logs, f, ensure_ascii=False, indent=2)

SEND_LOGS = load_logs()  # 列表元素形如：{"ts": "...", "msg": "..."}

def append_log(msg):
    now = datetime.datetime.utcnow()
    entry = {"ts": now.isoformat() + "Z", "msg": msg}
    SEND_LOGS.append(entry)

    # 清理 24 小时以前的日志
    cutoff = now - datetime.timedelta(hours=24)
    SEND_LOGS[:] = [log for log in SEND_LOGS if datetime.datetime.fromisoformat(log["ts"][:-1]) >= cutoff]

    save_logs(SEND_LOGS)

    # 清理 account_usage 超过 24 小时记录
    for k in account_usage:
        account_usage[k] = [r for r in account_usage[k] if datetime.datetime.fromisoformat(r["ts"]) >= cutoff]
    save_usage()

    # SSE 推送
    data = {
        "log": msg,
        "usage": {k: len(v) for k,v in account_usage.items()}
    }
    send_event(data)

# ================== 辅助函数 ==================
def reset_daily_usage_if_needed():
    global account_usage, last_reset_date
    today = datetime.date.today()
    if today != last_reset_date:
        for k in account_usage.keys():
            account_usage[k] = []
        last_reset_date = today
        save_usage()
        append_log("已进入新的一天，账号发送计数已重置。")

def infer_smtp(email):
    domain = email.split("@")[-1].lower().strip()
    outlook_domains = {"outlook.com", "hotmail.com", "live.com", "msn.com", "outlook.cn"}
    if domain in outlook_domains:
        return ("smtp.office365.com", 587)
    if domain == "gmail.com":
        return ("smtp.gmail.com", 587)
    return ("smtp." + domain, 587)

def get_next_account():
    global current_index
    selected_accounts = [acc for acc in ACCOUNTS if acc.get("selected", True)]
    if not selected_accounts:
        return None
    for _ in range(len(selected_accounts)):
        acc = selected_accounts[current_index % len(selected_accounts)]
        current_index = (current_index + 1) % len(selected_accounts)
        count = len(account_usage.get(acc["email"], []))
        if count < DAILY_LIMIT:
            return acc
    return None

def send_email(account, to_email, subject, body):
    try:
        smtp_server, smtp_port = infer_smtp(account["email"])
        if "smtp_server" in account:
            smtp_server = account["smtp_server"]
        if "smtp_port" in account:
            smtp_port = int(account["smtp_port"])

        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = account["email"]
        msg["To"] = to_email
        msg["Subject"] = Header(subject, "utf-8")

        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(account["email"], account["app_password"])
        server.sendmail(account["email"], [to_email], msg.as_string())
        server.quit()

        return True, ""
    except Exception as e:
        return False, str(e)

def add_usage(email):
    now = datetime.datetime.utcnow()
    account_usage.setdefault(email, [])
    account_usage[email].append({"ts": now.isoformat()})
    cutoff = now - datetime.timedelta(hours=24)
    account_usage[email] = [r for r in account_usage[email] if datetime.datetime.fromisoformat(r["ts"]) >= cutoff]
    save_usage()

def save_recipients():
    with open(RECIPIENTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"pending": RECIPIENTS, "sent": SENT_RECIPIENTS}, f, ensure_ascii=False, indent=2)

def load_recipients():
    global RECIPIENTS, SENT_RECIPIENTS
    if os.path.exists(RECIPIENTS_FILE):
        with open(RECIPIENTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            RECIPIENTS = data.get("pending", [])
            SENT_RECIPIENTS = data.get("sent", [])
    else:
        RECIPIENTS, SENT_RECIPIENTS = [], []

def send_event(data):
    for subscriber in EVENT_SUBSCRIBERS:
        try:
            subscriber.put(json.dumps(data))
        except:
            pass

# ================== 前端页面（完整 HTML 内嵌） ==================
@app.route("/", methods=["GET"])
def home():
    template = """<html lang="zh-CN">
    <head>
    <meta charset="UTF-8">
    <title>MailBot 后台</title>
    <style>
    /* --- 保留原有 CSS --- */
    /* ... 此处省略，保持原样 ... */
    </style>
    </head>
    <body>
    <div class="sidebar">
        <h1>MailBot 控制台</h1>
        <button id="tabRecipients" onclick="showPage('recipients')">收件箱管理</button>
        <button id="tabSend" onclick="showPage('send')">邮件发送</button>
        <button id="tabAccounts" onclick="showPage('accounts')">账号管理</button>
    </div>
    <div class="main">
        <!-- 收件人管理 -->
        <div id="recipientsPage">
            <div class="card">
                <h2>收件箱管理</h2>
                <div class="row">
                    <input type="file" id="csvFile">
                    <button class="btn" onclick="uploadCSV()">上传 CSV</button>
                    <button class="btn" onclick="clearRecipients()">一键清空列表</button>
                    <button class="btn" onclick="downloadTemplate()">下载 CSV 模板</button>
                    <button class="btn" onclick="exportPending()">导出未发送收件人</button>
                    <button class="btn" onclick="exportSent()">导出已发送收件人</button>
                    <button class="btn" onclick="continueTask()">继续上次任务</button>
                    <label>每页显示:</label>
                    <select id="pageSize" onchange="loadRecipients()">
                        <option value="10">10</option>
                        <option value="50" selected>50</option>
                        <option value="100">100</option>
                    </select>
                </div>
            </div>
            <div class="card" style="margin-top:10px;">
                <h3>收件箱列表</h3>
                <table id="recipientsTable">
                    <thead><tr><th>Email</th><th>Name</th><th>Real Name</th><th>操作</th></tr></thead>
                    <tbody></tbody>
                </table>
            </div>
        </div>

        <!-- 邮件发送 -->
        <div id="sendPage" style="display:none;">
            <div class="card">
                <h2>邮件发送</h2>
                <div class="muted">主题/正文可用变量：<code>{name}</code>、<code>{real_name}</code></div>
                <div class="row" style="margin-top:6px;">
                    <label>主题:</label>
                    <input type="text" id="subject" style="flex:1; min-width:280px;" placeholder="请输入主题, 可用 {name} {real_name}">
                </div>
                <div class="row">
                    <label>正文:</label>
                </div>
                <textarea id="body" style="width:100%;height:160px;" placeholder="请输入正文, 可用 {name} {real_name}"></textarea>
                <div class="row">
                    <label>选择发送账号:</label>
                    <div id="accountCheckboxes"></div>
                </div>
                <div class="row">
                    <label>发送间隔(秒):</label>
                    <input type="number" id="interval" value="5" style="width:80px;">
                    <button class="btn" onclick="startSend()">开始发送</button>
                    <button class="btn" onclick="pauseSend()">暂停</button>
                    <button class="btn" onclick="resumeSend()">继续</button>
                </div>
            </div>
            <div class="card" style="margin-top:10px;">
                <h3>实时发送进度</h3>
                <ul id="sendLog"></ul>
                <h3>账号发送统计</h3>
                <ul id="accountUsage"></ul>
            </div>
        </div>

        <!-- 账号管理 -->
        <div id="accountsPage" style="display:none;">
            <div class="card">
                <h2>账号管理</h2>
                <div class="muted">支持从 CSV 导入：列名 <code>email</code>、<code>app_password</code>、（可选）<code>smtp_server</code>、<code>smtp_port</code>。</div>
                <div class="row" style="margin-top:6px;">
                    <input type="file" id="accountFile">
                    <button class="btn" onclick="uploadAccounts()">导入账号 CSV</button>
                </div>
            </div>
            <div class="card" style="margin-top:10px;">
                <h3>账号列表</h3>
                <div id="accountList"></div>
            </div>
        </div>
    </div>

<script>
// 这里省略 JS 内容，只保留关键修改点
// SSE 自动滚动 + UTC+8 显示
function startEventSource(){
    if(evtSource){ evtSource.close(); }
    evtSource = new EventSource('/send-stream');
    const log = document.getElementById('sendLog');
    const usage = document.getElementById('accountUsage');
    evtSource.onmessage = function(e){
        const d = JSON.parse(e.data);
        if(d.log){
            const li = document.createElement('li');
            const localTime = new Date(new Date().getTime() + 8*60*60*1000);
            const ts = localTime.toISOString().replace('T',' ').split('.')[0];
            li.textContent = `[${ts}] ${d.log}`;
            log.appendChild(li);
            log.scrollTop = log.scrollHeight;
        }
        if(d.usage){
            usage.innerHTML='';
            for(const acc in d.usage){
                const li = document.createElement('li');
                li.textContent = acc + ': ' + d.usage[acc];
                usage.appendChild(li);
            }
        }
    }
}

// 收件人分页
function loadRecipients(){
    fetch('/recipients').then(res=>res.json()).then(data=>{
        const tbody = document.querySelector('#recipientsTable tbody');
        tbody.innerHTML = '';
        const pageSize = parseInt(document.getElementById('pageSize').value);
        const displayList = data.pending.slice(0,pageSize);
        displayList.forEach((r)=>{
            const tr = document.createElement('tr');
            tr.innerHTML = `<td>${r.email}</td><td>${r.name||''}</td><td>${r.real_name||''}</td>
            <td><button class="danger-link" onclick="deleteRecipient('${r.email}')">删除</button></td>`;
            tbody.appendChild(tr);
        });
    });
}
</script>
</body>
</html>
"""
    return render_template_string(template)

# ================== 邮件发送逻辑 ==================
@app.route("/send", methods=["POST"])
def start_send():
    global IS_SENDING, PAUSED
    data = request.json
    subject = data.get("subject")
    body = data.get("body")
    interval = int(data.get("interval", 5))
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
        reset_daily_usage_if_needed()
        task = SEND_QUEUE[0]
        subject = task["subject"]
        body = task["body"]
        interval = task["interval"]

        if PAUSED:
            time.sleep(1)
            continue

        recipient = None
        with SEND_LOCK:
            if RECIPIENTS:
                recipient = RECIPIENTS.pop(0)

        if not recipient:
            break

        acc = get_next_account()
        if not acc:
            append_log("没有可用账号或账号今日已达上限，等待 60 秒后重试。")
            time.sleep(60)
            with SEND_LOCK:
                RECIPIENTS.insert(0, recipient)
            continue

        recipient_safe = {"name": recipient.get("name",""), "real_name": recipient.get("real_name","")}
        try:
            personalized_subject = subject.format(**recipient_safe)
            personalized_body = body.format(**recipient_safe)
        except Exception as e:
            append_log(f"内容格式错误 {recipient['email']}: {e}")
            with SEND_LOCK:
                RECIPIENTS.append(recipient)
            continue

        success, err = send_email(acc, recipient["email"], personalized_subject, personalized_body)
        if success:
            add_usage(acc["email"])
            SENT_RECIPIENTS.append(recipient)
            save_recipients()
            append_log(f"已发送给 {recipient['email']} (使用账号 {acc['email']})")
        else:
            append_log(f"发送失败 {recipient['email']} : {err}")
            with SEND_LOCK:
                RECIPIENTS.append(recipient)

        time.sleep(interval)

    IS_SENDING = False
    with SEND_LOCK:
        if SEND_QUEUE:
            SEND_QUEUE.pop(0)

# ================== 其他接口（SSE、暂停、恢复、获取日志等） ==================
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

@app.route("/get-logs")
def get_logs():
    return jsonify({"logs": SEND_LOGS})

@app.route("/get-usage")
def get_usage():
    usage_count = {k: len(v) for k,v in account_usage.items()}
    return jsonify({"usage": usage_count})

# ================== 收件人接口 ==================
@app.route("/recipients", methods=["GET"])
def get_recipients():
    return jsonify({"pending": RECIPIENTS, "sent": SENT_RECIPIENTS})

# ================== 启动 ==================
if __name__ == "__main__":
    load_recipients()
    reset_daily_usage_if_needed()
    startEventSource = False
    app.run(host="0.0.0.0", port=5000, debug=True)
