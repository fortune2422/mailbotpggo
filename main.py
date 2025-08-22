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
SMTP_SETTINGS = {
    "gmail.com": {"server": "smtp.gmail.com", "port": 587},
    "hotmail.com": {"server": "smtp.office365.com", "port": 587},
    "outlook.com": {"server": "smtp.office365.com", "port": 587},
}

DAILY_LIMIT = 450
RECIPIENTS_FILE = "recipients.json"
LOG_FILE = "send_log.txt"
ACCOUNT_STATS_FILE = "account_stats.json"

# ================== 账号加载 ==================
def load_accounts():
    accounts = []
    i = 1
    while True:
        email = os.getenv(f"EMAIL{i}")
        app_password = os.getenv(f"APP_PASSWORD{i}")
        if email and app_password:
            accounts.append({"email": email, "app_password": app_password, "selected": True})
            i += 1
        else:
            break
    return accounts

ACCOUNTS = load_accounts()
current_index = 0
account_usage = {acc["email"]: 0 for acc in ACCOUNTS}
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

# ================== 账号发送统计 ==================
ACCOUNT_STATS = {}  # {"email": [{"timestamp": xxx, "count": n}, ...]}

def load_account_stats():
    global ACCOUNT_STATS
    if os.path.exists(ACCOUNT_STATS_FILE):
        with open(ACCOUNT_STATS_FILE, "r", encoding="utf-8") as f:
            ACCOUNT_STATS = json.load(f)
    else:
        ACCOUNT_STATS = {}

def save_account_stats():
    with open(ACCOUNT_STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(ACCOUNT_STATS, f, ensure_ascii=False, indent=2)

def add_account_stat(email):
    ts = int(time.time())
    if email not in ACCOUNT_STATS:
        ACCOUNT_STATS[email] = []
    ACCOUNT_STATS[email].append({"timestamp": ts, "count": 1})
    clean_old_stats()
    save_account_stats()

def clean_old_stats():
    """删除超过24小时的记录"""
    cutoff = int(time.time()) - 24*3600
    for email in ACCOUNT_STATS:
        ACCOUNT_STATS[email] = [s for s in ACCOUNT_STATS[email] if s["timestamp"] >= cutoff]

# ================== 辅助函数 ==================
def reset_daily_usage():
    global account_usage, last_reset_date
    today = datetime.date.today()
    if today != last_reset_date:
        account_usage = {acc["email"]: 0 for acc in ACCOUNTS}
        last_reset_date = today

def get_next_account():
    global current_index
    selected_accounts = [acc for acc in ACCOUNTS if acc.get("selected", True)]
    if not selected_accounts:
        return None
    for _ in range(len(selected_accounts)):
        acc = selected_accounts[current_index % len(selected_accounts)]
        current_index = (current_index + 1) % len(selected_accounts)
        if account_usage.get(acc["email"], 0) < DAILY_LIMIT:
            return acc
    return None

def send_email(account, to_email, subject, body):
    try:
        domain = account["email"].split("@")[-1]
        smtp_info = SMTP_SETTINGS.get(domain)
        if not smtp_info:
            return False, f"未知邮箱域名 {domain}"

        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = account["email"]
        msg["To"] = to_email
        msg["Subject"] = Header(subject, "utf-8")

        server = smtplib.SMTP(smtp_info["server"], smtp_info["port"])
        server.starttls()
        server.login(account["email"], account["app_password"])
        server.sendmail(account["email"], [to_email], msg.as_string())
        server.quit()
        account_usage[account["email"]] = account_usage.get(account["email"], 0) + 1
        add_account_stat(account["email"])
        return True, ""
    except Exception as e:
        return False, str(e)

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

def log_message(msg):
    timestamp = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    line = f"[{timestamp.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    send_event({"log": line, "usage": account_usage.copy(), "stats": ACCOUNT_STATS})

def send_event(data):
    for subscriber in EVENT_SUBSCRIBERS:
        try:
            subscriber.put(json.dumps(data))
        except:
            pass

# ================== Flask 前端 HTML ==================
@app.route("/")
def home():
    template = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>MailBot 后台</title>
<style>
body { font-family: Arial; margin:0; padding:0; display:flex; height:100vh; background:#f5f5f5;}
.sidebar { width:200px; background:#2f4050; color:#fff; display:flex; flex-direction:column; }
.sidebar button { padding:15px; background:none; border:none; color:#fff; cursor:pointer; text-align:left; font-size:16px; border-bottom:1px solid #3c4b5a;}
.sidebar button:hover { background:#1ab394;}
.main { flex:1; padding:20px; overflow:auto;}
.card { background:#fff; padding:15px; margin-bottom:15px; box-shadow:0 2px 5px rgba(0,0,0,0.1);}
table { width:100%; border-collapse: collapse;}
th, td { border:1px solid #ddd; padding:8px; text-align:left;}
th { background:#f2f2f2;}
.btn { padding:6px 12px; background:#1ab394; color:#fff; border:none; cursor:pointer;}
.btn:hover { background:#18a689;}
.account-btn { padding:4px 8px; margin-left:10px; background:#e74c3c; color:#fff; border:none; cursor:pointer; border-radius:3px;}
.account-btn:hover { background:#c0392b;}
.pagination { margin-top:10px; }
.pagination button { margin-right:5px; padding:3px 6px; }
</style>
</head>
<body>
<div class="sidebar">
<button onclick="showPage('recipients')">收件箱管理</button>
<button onclick="showPage('send')">邮件发送</button>
<button onclick="showPage('accounts')">账号管理</button>
</div>
<div class="main">
<!-- 收件箱管理 -->
<div id="recipientsPage">
<h2>收件箱管理</h2>
<input type="file" id="csvFile">
<button class="btn" onclick="uploadCSV()">上传 CSV</button>
<button class="btn" onclick="clearRecipients()">一键清空列表</button>
<button class="btn" onclick="downloadTemplate()">下载 CSV 模板</button>
<button class="btn" onclick="exportPending()">导出未发送收件人</button>
<button class="btn" onclick="exportSent()">导出已发送收件人</button>
<button class="btn" onclick="continueTask()">继续上次任务</button>
<div>
<label>每页显示:</label>
<select id="perPage" onchange="loadRecipients()">
<option value="10">10</option>
<option value="50" selected>50</option>
<option value="100">100</option>
</select>
</div>
<div class="card" style="margin-top:10px;">
<h3>收件箱列表</h3>
<table id="recipientsTable">
<thead><tr><th>Email</th><th>Name</th><th>Real Name</th><th>操作</th></tr></thead>
<tbody></tbody>
</table>
<div class="pagination" id="pagination"></div>
</div>
</div>

<!-- 邮件发送 -->
<div id="sendPage" style="display:none;">
<h2>邮件发送</h2>
<div class="card">
<label>选择发送账号:</label><br>
<div id="accountCheckboxes"></div>
<br>
<label>主题:</label><br>
<input type="text" id="subject" style="width:100%" placeholder="请输入主题, 可用 {name} {real_name}">
<br><br>
<label>正文:</label><br>
<textarea id="body" style="width:100%;height:150px;" placeholder="请输入正文, 可用 {name} {real_name}"></textarea>
<br><br>
<label>发送间隔(秒):</label>
<input type="number" id="interval" value="5" style="width:60px;">
<button class="btn" onclick="startSend()">开始发送</button>
<button class="btn" onclick="pauseSend()">暂停</button>
<button class="btn" onclick="resumeSend()">继续</button>
</div>
<div class="card" style="margin-top:10px;">
<h3>实时发送进度</h3>
<ul id="sendLog" style="max-height:200px; overflow:auto;"></ul>
<h3>账号发送统计 (24小时)</h3>
<ul id="accountUsage"></ul>
</div>
</div>

<!-- 账号管理 -->
<div id="accountsPage" style="display:none;">
<h2>账号管理</h2>
<input type="file" id="accountFile">
<button class="btn" onclick="uploadAccounts()">导入账号 CSV</button>
<div class="card" style="margin-top:10px;">
<h3>账号列表</h3>
<div id="accountList"></div>
</div>
</div>
</div>

<script>
function showPage(page){
document.getElementById('recipientsPage').style.display = page==='recipients'?'block':'none';
document.getElementById('sendPage').style.display = page==='send'?'block':'none';
document.getElementById('accountsPage').style.display = page==='accounts'?'block':'none';
if(page==='recipients'){ loadRecipients(); }
if(page==='send'){ loadAccounts(); }
if(page==='accounts'){ loadAccountsList(); }
}

// ---------------- 收件人 ----------------
let currentPage = 1;
function uploadCSV(){
const file = document.getElementById('csvFile').files[0];
if(!file){ alert("请选择文件"); return; }
const formData = new FormData();
formData.append('file', file);
fetch('/upload-csv', {method:'POST', body:formData})
.then(res=>res.json()).then(data=>{
alert(data.message);
loadRecipients();
});
}

function loadRecipients(){
fetch('/recipients').then(res=>res.json()).then(data=>{
const perPage = parseInt(document.getElementById('perPage').value);
const tbody = document.querySelector('#recipientsTable tbody');
tbody.innerHTML = '';
const start = (currentPage-1)*perPage;
const end = start+perPage;
const list = data.pending.slice(start,end);
list.forEach((r)=>{
const tr = document.createElement('tr');
tr.innerHTML = "<td>"+(r.email||'')+"</td><td>"+(r.name||'')+"</td><td>"+(r.real_name||'')+"</td><td><button onclick=\"deleteRecipient('"+r.email+"')\">删除</button></td>";
tbody.appendChild(tr);
});
const pages = Math.ceil(data.pending.length/perPage);
const pagination = document.getElementById('pagination');
pagination.innerHTML='';
for(let i=1;i<=pages;i++){
const btn = document.createElement('button');
btn.innerText=i;
btn.onclick=()=>{currentPage=i; loadRecipients();};
pagination.appendChild(btn);
}
});
}

function deleteRecipient(email){
fetch('/delete-recipient', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({email})})
.then(res=>res.json()).then(data=>{ alert(data.message); loadRecipients(); });
}

function clearRecipients(){
fetch('/clear-recipients', {method:'POST'}).then(res=>res.json()).then(data=>{ alert(data.message); loadRecipients(); });
}

function downloadTemplate(){ window.location.href="/download-template"; }
function exportPending(){ window.location.href="/download-recipients?status=pending"; }
function exportSent(){ window.location.href="/download-recipients?status=sent"; }
function continueTask(){ window.location.href="/continue-task"; }

// ---------------- 邮件发送 ----------------
function loadAccounts(){
fetch('/accounts').then(res=>res.json()).then(data=>{
const div = document.getElementById('accountCheckboxes');
div.innerHTML = '';
data.forEach(acc=>{
const id = acc.email.replace(/[@.]/g,'_');
div.innerHTML += `<label><input type="checkbox" id="${id}" ${acc.selected?'checked':''} onchange="toggleAccount('${acc.email}', this.checked)"> ${acc.email}</label><br>`;
});
}

)}

function toggleAccount(email, checked){
fetch('/toggle-account', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({email,checked})});
}

let evtSource;
function startSend(){
const subject = document.getElementById('subject').value;
const body = document.getElementById('body').value;
const interval = parseInt(document.getElementById('interval').value);
if(!subject || !body){ alert("请填写主题和正文"); return; }
fetch('/send', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({subject,body,interval})})
.then(res=>res.json()).then(data=>{ alert(data.message); });
startEventSource();
}

function startEventSource(){
if(evtSource) evtSource.close();
evtSource = new EventSource('/send-stream');
const log = document.getElementById('sendLog');
const usage = document.getElementById('accountUsage');
evtSource.onmessage = function(e){
const d = JSON.parse(e.data);
if(d.log){ 
const li = document.createElement('li'); 
li.textContent=d.log; 
log.appendChild(li);
log.scrollTop = log.scrollHeight;
}
if(d.stats){
usage.innerHTML='';
for(const acc in d.stats){
const total = d.stats[acc].reduce((a,b)=>a+b.count,0);
usage.innerHTML += `<li>${acc}: ${total}</li>`;
}
}
}
}

function pauseSend(){ fetch('/pause-send',{method:'POST'}).then(r=>r.json()).then(d=>alert(d.message)); }
function resumeSend(){ fetch('/resume-send',{method:'POST'}).then(r=>r.json()).then(d=>alert(d.message)); }

// ---------------- 账号管理 ----------------
function uploadAccounts(){
const file = document.getElementById('accountFile').files[0];
if(!file){ alert("请选择账号 CSV"); return; }
const formData = new FormData();
formData.append('file', file);
fetch('/upload-accounts', {method:'POST', body:formData})
.then(res=>res.json()).then(data=>{
alert(data.message);
loadAccountsList();
});
}

function loadAccountsList(){
fetch('/accounts').then(res=>res.json()).then(data=>{
const div = document.getElementById('accountList');
div.innerHTML='';
data.forEach(acc=>{
const id='list_'+acc.email.replace(/[@.]/g,'_');
div.innerHTML += `<div>${acc.email}<button class="account-btn" onclick="deleteAccount('${acc.email}')">删除</button></div>`;
});
});

}

function deleteAccount(email){
fetch('/delete-account',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email})})
.then(res=>res.json()).then(data=>{
alert(data.message); loadAccountsList(); loadAccounts();
});
}

</script>
</body>
</html>"""
    return render_template_string(template)

# ================== 邮件发送逻辑 ==================
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
        reset_daily_usage()
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
            log_message("没有可用账号或账号今日已达上限")
            time.sleep(60)
            with SEND_LOCK:
                RECIPIENTS.insert(0, recipient)
            continue

        recipient_safe = {
            "name": recipient.get("name",""),
            "real_name": recipient.get("real_name","")
        }
        try:
            personalized_subject = subject.format(**recipient_safe)
            personalized_body = body.format(**recipient_safe)
        except Exception as e:
            log_message(f"内容格式错误 {recipient['email']}: {e}")
            with SEND_LOCK:
                RECIPIENTS.append(recipient)
            continue

        success, err = send_email(acc, recipient["email"], personalized_subject, personalized_body)
        if success:
            SENT_RECIPIENTS.append(recipient)
            log_message(f"已发送给 {recipient['email']} (使用账号 {acc['email']})")
        else:
            log_message(f"发送失败 {recipient['email']} : {err}")
            with SEND_LOCK:
                RECIPIENTS.append(recipient)
        save_recipients()
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
            EVENT_SUBSCRIBERS.remove(q)
    return Response(event_stream(), mimetype="text/event-stream")

# ================== 收件人管理 ==================
@app.route("/continue-task")
def continue_task():
    global SEND_QUEUE, IS_SENDING, PAUSED
    if RECIPIENTS:
        SEND_QUEUE.append({"subject":"继续上次任务","body":"继续上次任务邮件","interval":5})
        if not IS_SENDING:
            IS_SENDING = True
            PAUSED = False
            t = Thread(target=send_worker_loop, daemon=True)
            t.start()
    return jsonify({"message":"已加载上次未完成任务，可开始发送"})

@app.route("/recipients", methods=["GET"])
def get_recipients():
    return jsonify({"pending": RECIPIENTS, "sent": SENT_RECIPIENTS})

@app.route("/upload-csv", methods=["POST"])
def upload_csv():
    file = request.files.get('file')
    if not file:
        return jsonify({"message":"未选择文件"}), 400
    csv_data = file.read().decode('utf-8').splitlines()
    reader = csv.DictReader(csv_data)
    for row in reader:
        RECIPIENTS.append({
            "email": row.get("email"),
            "name": row.get("name",""),
            "real_name": row.get("real_name","")
        })
    save_recipients()
    return jsonify({"message":"CSV 上传成功"})

@app.route("/delete-recipient", methods=["POST"])
def delete_recipient():
    data = request.json
    email = data.get("email")
    global RECIPIENTS
    RECIPIENTS = [r for r in RECIPIENTS if r["email"] != email]
    save_recipients()
    return jsonify({"message": f"{email} 已删除"})

@app.route("/clear-recipients", methods=["POST"])
def clear_recipients():
    global RECIPIENTS
    RECIPIENTS = []
    save_recipients()
    return jsonify({"message":"收件人列表已清空"})

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
    data = RECIPIENTS if status=="pending" else SENT_RECIPIENTS
    filename = "pending.csv" if status=="pending" else "sent.csv"
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=["email","name","real_name"])
    writer.writeheader()
    for r in data:
        writer.writerow(r)
    output.seek(0)
    return send_file(BytesIO(output.getvalue().encode("utf-8")), mimetype="text/csv", download_name=filename, as_attachment=True)

# ================== 账号管理 ==================
@app.route("/accounts")
def get_accounts(): return jsonify(ACCOUNTS)

@app.route("/toggle-account", methods=["POST"])
def toggle_account():
    data = request.json
    email = data.get("email")
    checked = data.get("checked")
    for acc in ACCOUNTS:
        if acc["email"] == email: acc["selected"] = checked
    return jsonify({"message":"账号状态已更新"})

@app.route("/upload-accounts", methods=["POST"])
def upload_accounts():
    file = request.files.get('file')
    if not file:
        return jsonify({"message":"未选择文件"}), 400
    csv_data = file.read().decode('utf-8').splitlines()
    reader = csv.DictReader(csv_data)
    for row in reader:
        ACCOUNTS.append({
            "email": row.get("email"),
            "app_password": row.get("app_password"),
            "selected": True
        })
        account_usage[row.get("email")] = 0
    return jsonify({"message":"账号上传成功"})

@app.route("/delete-account", methods=["POST"])
def delete_account():
    data = request.json
    email = data.get("email")
    global ACCOUNTS
    ACCOUNTS = [acc for acc in ACCOUNTS if acc["email"] != email]
    account_usage.pop(email, None)
    ACCOUNT_STATS.pop(email, None)
    save_account_stats()
    return jsonify({"message": f"{email} 已删除"})

# ================== 启动 ==================
if __name__ == "__main__":
    load_recipients()
    load_account_stats()
    port = int(os.environ.get("PORT",10000))
    app.run(host="0.0.0.0", port=port, threaded=True)
