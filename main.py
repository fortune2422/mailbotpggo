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
import atexit


app = Flask(__name__)

# ================== 配置 ==================
DAILY_LIMIT = 450
RECIPIENTS_FILE = "recipients.json"
LOG_FILE_JSON = "send_log.json"
USAGE_FILE_JSON = "account_usage.json"

# ================== 账号加载 ==================
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
            if smtp_server: rec["smtp_server"] = smtp_server
            if smtp_port: rec["smtp_port"] = int(smtp_port)
            accounts.append(rec)
            i += 1
        else:
            break
    return accounts

ACCOUNTS = load_accounts_from_env()
current_index = 0

# ================== 用量持久化 ==================
account_usage = {}
last_reset_date = datetime.date.today()

if os.path.exists(USAGE_FILE_JSON):
    try:
        with open(USAGE_FILE_JSON,'r',encoding='utf-8') as f:
            account_usage = json.load(f)
    except:
        account_usage = {}

for acc in ACCOUNTS:
    account_usage.setdefault(acc['email'], 0)

def save_usage():
    with open(USAGE_FILE_JSON,'w',encoding='utf-8') as f:
        json.dump(account_usage,f,ensure_ascii=False,indent=2)

# ================== 收件人 ==================
RECIPIENTS = []
SENT_RECIPIENTS = []

# ================== 发送控制 ==================
SEND_QUEUE = []
IS_SENDING = False
PAUSED = False
SEND_LOCK = Lock()
EVENT_SUBSCRIBERS = []

# ================== 日志 ==================
SEND_LOGS = []

def load_logs():
    global SEND_LOGS
    if os.path.exists(LOG_FILE_JSON):
        try:
            with open(LOG_FILE_JSON,'r',encoding='utf-8') as f:
                SEND_LOGS = json.load(f)
        except:
            SEND_LOGS = []
    else:
        SEND_LOGS = []

def save_logs():
    with open(LOG_FILE_JSON,'w',encoding='utf-8') as f:
        json.dump(SEND_LOGS,f,ensure_ascii=False,indent=2)
        
def cleanup():
    save_recipients()
    save_logs()
    save_usage()

atexit.register(cleanup)

# ================== 辅助 ==================
def reset_daily_usage_if_needed():
    global account_usage,last_reset_date
    today = datetime.date.today()
    if today != last_reset_date:
        for k in account_usage.keys():
            account_usage[k] = 0
        last_reset_date = today
        save_usage()
        append_log("已进入新的一天，账号发送计数已重置。")

def infer_smtp(email):
    domain = email.split('@')[-1].lower().strip()
    if domain in {"outlook.com","hotmail.com","live.com","msn.com","outlook.cn"}: return ("smtp.office365.com",587)
    if domain=="gmail.com": return ("smtp.gmail.com",587)
    return ("smtp."+domain,587)

def get_next_account():
    global current_index
    selected_accounts = [acc for acc in ACCOUNTS if acc.get("selected",True)]
    if not selected_accounts: return None
    for _ in range(len(selected_accounts)):
        acc = selected_accounts[current_index % len(selected_accounts)]
        current_index = (current_index+1) % len(selected_accounts)
        count = account_usage.get(acc['email'],0)
        if count < DAILY_LIMIT: return acc
    return None

def send_email(account,to_email,subject,body):
    try:
        smtp_server,smtp_port = infer_smtp(account['email'])
        if 'smtp_server' in account: smtp_server = account['smtp_server']
        if 'smtp_port' in account: smtp_port = int(account['smtp_port'])
        msg = MIMEText(body,'plain','utf-8')
        msg['From'] = account['email']
        msg['To'] = to_email
        msg['Subject'] = Header(subject,'utf-8')
        server = smtplib.SMTP(smtp_server,smtp_port)
        server.starttls()
        server.login(account['email'],account['app_password'])
        server.sendmail(account['email'],[to_email],msg.as_string())
        server.quit()
        account_usage[account['email']] = account_usage.get(account['email'],0)+1
        save_usage()
        return True,''
    except Exception as e:
        return False,str(e)

# ================== 收件人持久化 ==================
def save_recipients():
    with open(RECIPIENTS_FILE,'w',encoding='utf-8') as f:
        json.dump({"pending":RECIPIENTS,"sent":SENT_RECIPIENTS},f,ensure_ascii=False,indent=2)

def load_recipients():
    global RECIPIENTS,SENT_RECIPIENTS
    if os.path.exists(RECIPIENTS_FILE):
        with open(RECIPIENTS_FILE,'r',encoding='utf-8') as f:
            data=json.load(f)
            RECIPIENTS=data.get('pending',[])
            SENT_RECIPIENTS=data.get('sent',[])
    else:
        RECIPIENTS=[]
        SENT_RECIPIENTS=[]
        
# ---- 保证启动时总是加载历史数据 ----
load_recipients()
load_logs()


# ================== 后端：24小时内账号统计 ==================
def append_log(msg):
    now_utc = datetime.datetime.utcnow()
    cutoff = now_utc - datetime.timedelta(hours=24)
    global SEND_LOGS
    # 清理24小时以上日志
    valid_logs = []
    for entry in SEND_LOGS:
        try:
            ts = datetime.datetime.fromisoformat(entry['ts'])
        except Exception:
            continue
        if ts > cutoff:
            valid_logs.append(entry)
    SEND_LOGS = valid_logs


    entry = {"ts":(now_utc+datetime.timedelta(hours=8)).isoformat()+"+08:00", "msg":msg}
    SEND_LOGS.append(entry)
    save_logs()
    save_usage()

    # 统计24小时内账号发送次数
    recent_usage = {}
    for log in SEND_LOGS:
        text = log['msg']
        if text.startswith('已发送给'):
            parts = text.split('使用账号')
            if len(parts) == 2:
                acc_email = parts[1].strip(' )')
                recent_usage[acc_email] = recent_usage.get(acc_email, 0) + 1

    send_event({"log": msg, "usage": recent_usage})

# ================== SSE ==================
def send_event(data):
    for subscriber in EVENT_SUBSCRIBERS:
        try: subscriber.put(json.dumps(data))
        except: pass

@app.route('/send-stream')
def send_stream():
    def event_stream():
        q = Queue()
        EVENT_SUBSCRIBERS.append(q)
        try:
            while True:
                data = q.get()
                yield f"data: {data}\n\n"
        except GeneratorExit:
            if q in EVENT_SUBSCRIBERS: EVENT_SUBSCRIBERS.remove(q)
    return Response(event_stream(),mimetype='text/event-stream')

        recipient_safe={'name':recipient.get('name',''),'real_name':recipient.get('real_name','')}
        try:
            personalized_subject = subject.format(**recipient_safe)
            personalized_body = body.format(**recipient_safe)
        except Exception as e:
            append_log(f"内容格式错误 {recipient['email']}: {e}")
            with SEND_LOCK: RECIPIENTS.append(recipient)
            continue

        success,err = send_email(acc,recipient['email'],personalized_subject,personalized_body)
        if success:
            SENT_RECIPIENTS.append(recipient)
            save_recipients()
            append_log(f"已发送给 {recipient['email']} (使用账号 {acc['email']})")
        else:
            append_log(f"发送失败 {recipient['email']} : {err}")
            with SEND_LOCK: RECIPIENTS.append(recipient)
        time.sleep(interval)
    IS_SENDING=False
    with SEND_LOCK:
        if SEND_QUEUE: SEND_QUEUE.pop(0)

# ================== 前端页面（完整 HTML 内嵌） ==================
@app.route("/", methods=["GET"])
def home():
    template = """
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <title>MailBot 后台</title>
        <style>
            :root{
              --brand:#1ab394;
              --brand-dark:#18a689;
              --danger:#e74c3c;
              --danger-dark:#c0392b;
              --bg:#f5f5f5;
              --sidebar:#2f4050;
              --line:#e6e6e6;
            }
            *{box-sizing:border-box}
            body { font-family: Arial, Helvetica, sans-serif; margin:0; padding:0; display:flex; height:100vh; background:var(--bg);}
            .sidebar { width:220px; background:var(--sidebar); color:#fff; display:flex; flex-direction:column; }
            .sidebar h1{font-size:18px; font-weight:600; padding:16px; border-bottom:1px solid #3c4b5a; margin:0;}
            .sidebar button { padding:14px 16px; background:none; border:none; color:#fff; cursor:pointer; text-align:left; font-size:15px; border-bottom:1px solid #3c4b5a;}
            .sidebar button:hover, .sidebar button.active { background:var(--brand);}
            .main { flex:1; padding:20px; overflow:auto;}
            .card { background:#fff; padding:16px; margin-bottom:16px; box-shadow:0 2px 5px rgba(0,0,0,0.08); border-radius:12px; border:1px solid var(--line);}
            table { width:100%; border-collapse: collapse;}
            th, td { border:1px solid #ddd; padding:8px; text-align:left; font-size:14px;}
            th { background:#f9f9f9;}
            .btn { padding:8px 14px; background:var(--brand); color:#fff; border:none; cursor:pointer; border-radius:10px;}
            .btn:hover { background:var(--brand-dark);}
            .btn-danger { background:var(--danger);}
            .btn-danger:hover { background:var(--danger-dark);}
            .muted{ color:#888; font-size:12px;}
            input[type="text"], input[type="number"], textarea{ border:1px solid #ddd; border-radius:10px; padding:8px; }
            #accountList .item{ display:flex; align-items:center; justify-content:space-between; padding:10px 12px; border:1px solid var(--line); border-radius:10px; margin-bottom:8px; }
            #accountList .left{ display:flex; gap:10px; align-items:center;}
            .pill{ padding:3px 8px; border-radius:999px; background:#f2f2f2; font-size:12px;}
            .danger-link{ background:transparent; border:1px solid var(--danger); color:var(--danger); padding:6px 10px; border-radius:999px; cursor:pointer; }
            .danger-link:hover{ background:var(--danger); color:#fff; }
            .row{ display:flex; gap:10px; flex-wrap:wrap; align-items:center;}
            .row > *{ margin-top:6px;}
            ul{ margin:8px 0 0 18px; padding:0;}
            li{ margin:4px 0;}
            .checkbox-line{ margin-bottom:4px;}
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
                    </div>
                </div>
                <div class="card" style="margin-top:10px;">
    <h3>收件箱列表</h3>

    <!-- 分页控件 -->
    <div style="margin-bottom:6px;">
        每页显示：
        <select id="perPage" onchange="loadRecipients()">
            <option value="10">10</option>
            <option value="50">50</option>
            <option value="100">100</option>
        </select>
        当前页：
        <button onclick="changePage(-1)">上一页</button>
    <input type="number" id="currentPage" value="1" style="width:50px;" onchange="loadRecipients()">
    <button onclick="changePage(1)">下一页</button>
    <span id="pagination"></span>
</div>

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
            // 选项卡激活态
            function activeTab(tab){
                document.getElementById('tabRecipients').classList.remove('active');
                document.getElementById('tabSend').classList.remove('active');
                document.getElementById('tabAccounts').classList.remove('active');
                if(tab==='recipients') document.getElementById('tabRecipients').classList.add('active');
                if(tab==='send') document.getElementById('tabSend').classList.add('active');
                if(tab==='accounts') document.getElementById('tabAccounts').classList.add('active');
            }

            function showPage(page){
                document.getElementById('recipientsPage').style.display = page==='recipients'?'block':'none';
                document.getElementById('sendPage').style.display = page==='send'?'block':'none';
                document.getElementById('accountsPage').style.display = page==='accounts'?'block':'none';
                activeTab(page);
                if(page==='recipients'){ loadRecipients(); }
                if(page==='send'){ loadAccounts(); loadLogsAndUsage(); startEventSource(); }
                if(page==='accounts'){ loadAccountsList(); }
            }

            // ---------------- 收件人 ----------------
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
        const tbody = document.querySelector('#recipientsTable tbody');
        tbody.innerHTML = '';

        const perPage = parseInt(document.getElementById('perPage')?.value || 10);
        let page = parseInt(document.getElementById('currentPage')?.value || 1);

        const totalPages = Math.ceil(data.pending.length / perPage);
        if(page > totalPages) page = totalPages;
        if(page < 1) page = 1;
        document.getElementById('currentPage').value = page;

        const startIdx = (page-1) * perPage;
        const endIdx = startIdx + perPage;
        const pageData = data.pending.slice(startIdx, endIdx);

        pageData.forEach((r)=>{
            const tr = document.createElement('tr');
            tr.innerHTML = `<td>${r.email}</td><td>${r.name||''}</td><td>${r.real_name||''}</td>`+
                           `<td><button class="danger-link" onclick="deleteRecipient('${r.email}')">删除</button></td>`;
            tbody.appendChild(tr);
        });

        document.getElementById('pagination').textContent = `页数: ${page}/${totalPages}`;
    });
}

// 上一页 / 下一页按钮函数
function changePage(offset){
    const pageInput = document.getElementById('currentPage');
    let page = parseInt(pageInput.value || 1);
    page += offset;
    if(page < 1) page = 1;
    pageInput.value = page;
    loadRecipients();
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

            // ---------------- 邮件发送 ----------------
            function loadAccounts(){
                fetch('/accounts').then(res=>res.json()).then(data=>{
                    const div = document.getElementById('accountCheckboxes');
                    div.innerHTML = '';
                    data.forEach(acc=>{
                        const id = acc.email.replace(/[@.]/g,'_');
                        div.innerHTML += `<div class="checkbox-line">
                            <label><input type="checkbox" id="${id}" ${acc.selected?'checked':''} onchange="toggleAccount('${acc.email}', this.checked)"> ${acc.email}
                            ${acc.smtp_server?'<span class="pill">'+acc.smtp_server+(acc.smtp_port?(':'+acc.smtp_port):'')+'</span>':''}
                            </label></div>`;
                    });
                });
            }

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
                // SSE 在 showPage('send') 时已经启动，这里无需重复
            }

            function startEventSource(){
                if(evtSource){ evtSource.close(); }
                evtSource = new EventSource('/send-stream');
                const log = document.getElementById('sendLog');
                const usage = document.getElementById('accountUsage');
                evtSource.onmessage = function(e){
                    const d = JSON.parse(e.data);
                    if(d.log){
                        const li = document.createElement('li');
                        li.textContent = d.log;
                        log.appendChild(li);
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

            function loadLogsAndUsage(){
                // 回放历史日志
                fetch('/get-logs').then(res=>res.json()).then(data=>{
                    const log = document.getElementById('sendLog');
                    log.innerHTML = '';
                    data.logs.forEach(item=>{
                        const li = document.createElement('li');
                        li.textContent = '[' + item.ts + '] ' + item.msg;
                        log.appendChild(li);
                    });
                });
                // 读取历史用量
                fetch('/get-usage').then(res=>res.json()).then(data=>{
                    const usage = document.getElementById('accountUsage');
                    usage.innerHTML = '';
                    const map = data.usage || {};
                    for(const acc in map){
                        const li = document.createElement('li');
                        li.textContent = acc + ': ' + map[acc];
                        usage.appendChild(li);
                    }
                });
            }

            function pauseSend(){
                fetch('/pause-send', {method:'POST'}).then(res=>res.json()).then(data=>alert(data.message));
            }
            function resumeSend(){
                fetch('/resume-send', {method:'POST'}).then(res=>res.json()).then(data=>alert(data.message));
            }

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
                    loadAccounts();
                });
            }

            function loadAccountsList(){
                fetch('/accounts').then(res=>res.json()).then(data=>{
                    const div = document.getElementById('accountList');
                    div.innerHTML = '';
                    data.forEach(acc=>{
                        const id = 'list_'+acc.email.replace(/[@.]/g,'_');
                        div.innerHTML += `
                          <div class="item" id="${id}">
                            <div class="left">
                              <strong>${acc.email}</strong>
                              ${acc.smtp_server?'<span class="pill">'+acc.smtp_server+(acc.smtp_port?(':'+acc.smtp_port):'')+'</span>':''}
                              <span class="pill">${acc.selected?'已启用':'未启用'}</span>
                            </div>
                            <div class="right">
                              <button class="btn-danger" onclick="deleteAccount('${acc.email}')">删除</button>
                            </div>
                          </div>`;
                    });
                });
            }

            function deleteAccount(email){
                fetch('/delete-account', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({email})})
                .then(res=>res.json()).then(data=>{
                    alert(data.message);
                    loadAccountsList();
                    loadAccounts();
                });
            }

            // 初始默认
            showPage('recipients');
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

        recipient_safe = {
            "name": recipient.get("name",""),
            "real_name": recipient.get("real_name","")
        }
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


# ======= 历史日志 / 用量：用于刷新后回放 =======
@app.route("/get-logs")
def get_logs():
    return jsonify({"logs": SEND_LOGS})

@app.route("/get-usage")
def get_usage():
    return jsonify({"usage": account_usage})

# ================== 收件人管理 ==================
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
        if not row.get("email"):
            continue
        RECIPIENTS.append({
            "email": row.get("email").strip(),
            "name": row.get("name","").strip(),
            "real_name": row.get("real_name","").strip()
        })
    save_recipients()
    append_log(f"已导入收件人 {len(RECIPIENTS)} 条（包含历史未发送）。")
    return jsonify({"message":"CSV 上传成功"})

@app.route("/delete-recipient", methods=["POST"])
def delete_recipient():
    data = request.json
    email = data.get("email")
    global RECIPIENTS
    RECIPIENTS = [r for r in RECIPIENTS if r["email"] != email]
    save_recipients()
    append_log(f"已删除收件人 {email}")
    return jsonify({"message": f"{email} 已删除"})

@app.route("/clear-recipients", methods=["POST"])
def clear_recipients():
    global RECIPIENTS
    count = len(RECIPIENTS)
    RECIPIENTS = []
    save_recipients()
    append_log(f"已清空未发送收件人 {count} 条")
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

# ================== 账号管理 ==================
@app.route("/accounts")
def get_accounts():
    return jsonify(ACCOUNTS)

@app.route("/toggle-account", methods=["POST"])
def toggle_account():
    data = request.json
    email = data.get("email")
    checked = data.get("checked")
    for acc in ACCOUNTS:
        if acc["email"] == email:
            acc["selected"] = bool(checked)
            break
    append_log(f"账号 {email} 已{ '启用' if checked else '禁用' }")
    return jsonify({"message":"账号状态已更新"})

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
        rec = {
            "email": email,
            "app_password": app_password,
            "selected": True
        }
        if row.get("smtp_server"):
            rec["smtp_server"] = row.get("smtp_server").strip()
        if row.get("smtp_port"):
            try:
                rec["smtp_port"] = int(row.get("smtp_port"))
            except:
                pass
        # 若已存在同邮箱，则覆盖更新
        existing_idx = next((i for i,a in enumerate(ACCOUNTS) if a["email"]==email), None)
        if existing_idx is not None:
            ACCOUNTS[existing_idx] = rec
        else:
            ACCOUNTS.append(rec)
        account_usage.setdefault(email, 0)
        added += 1
    save_usage()
    append_log(f"已导入/更新账号 {added} 个")
    return jsonify({"message":"账号上传成功"})

@app.route("/delete-account", methods=["POST"])
def delete_account():
    data = request.json
    email = data.get("email")
    global ACCOUNTS
    ACCOUNTS = [acc for acc in ACCOUNTS if acc["email"] != email]
    account_usage.pop(email, None)
    save_usage()
    append_log(f"已删除账号 {email}")
    return jsonify({"message": f"{email} 已删除"})

# ================== 启动 ==================
if __name__ == "__main__":
    # 再次校验 usage 中包含所有 ENV 账号
    for acc in ACCOUNTS:
        account_usage.setdefault(acc["email"], 0)
    save_usage()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, threaded=True)
