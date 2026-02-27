import os
import hashlib
import secrets
import requests as req
from flask import Flask, request, jsonify, send_from_directory, session
from flask_cors import CORS
from groq import Groq
from supabase import create_client
import threading
from datetime import timedelta, datetime, timezone

app = Flask(__name__, static_folder='.')
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.permanent_session_lifetime = timedelta(days=30)
CORS(app, supports_credentials=True)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
BOT_NAME = os.environ.get("BOT_NAME", "Nouk_Bot")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin1234")
TAVILY_KEY = os.environ.get("TAVILY_KEY", "")

# ===== GROQ MULTI KEY =====
groq_keys = []
i = 1
while True:
    key = os.environ.get(f"GROQ_KEY_{i}")
    if not key: break
    groq_keys.append(key)
    i += 1
if not groq_keys:
    single = os.environ.get("GROQ_API_KEY")
    if single: groq_keys.append(single)

current_key_index = 0
key_lock = threading.Lock()

def get_groq_client():
    global current_key_index
    with key_lock:
        return Groq(api_key=groq_keys[current_key_index]), current_key_index + 1

def rotate_key():
    global current_key_index
    with key_lock:
        current_key_index = (current_key_index + 1) % len(groq_keys)

def web_search(query):
    if not TAVILY_KEY: return "Search unavailable"
    try:
        res = req.post("https://api.tavily.com/search", json={
            "api_key": TAVILY_KEY, "query": query, "search_depth": "advanced", "max_results": 5
        }, timeout=10)
        results = res.json().get("results", [])
        if not results: return "No results found."
        return "\n".join([f"- {r['title']}: {r['content'][:300]}" for r in results])
    except Exception as e: return f"Search failed: {e}"

# ===== SUPABASE =====
supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

def hash_pw(pw): return hashlib.sha256(pw.encode()).hexdigest()

# ---- accounts ----
def get_account(username):
    if not supabase: return None
    try:
        res = supabase.table("accounts").select("*").eq("username", username).execute()
        return res.data[0] if res.data else None
    except: return None

def get_all_accounts():
    if not supabase: return []
    try:
        res = supabase.table("accounts").select("username, is_admin").execute()
        return res.data or []
    except: return []

def create_account(username, password, is_admin=False):
    if not supabase: return False
    try:
        supabase.table("accounts").insert({
            "username": username, "password": hash_pw(password), "is_admin": is_admin
        }).execute()
        return True
    except: return False

# ---- memory ----
def get_memory(username):
    if not supabase: return {"memory": "", "history": [], "bot_nickname": ""}
    try:
        res = supabase.table("users").select("*").eq("username", username).execute()
        if res.data: return res.data[0]
        supabase.table("users").insert({"username": username, "memory": "", "history": [], "bot_nickname": ""}).execute()
        return {"memory": "", "history": [], "bot_nickname": ""}
    except: return {"memory": "", "history": [], "bot_nickname": ""}

def save_memory(username, memory, history, bot_nickname=""):
    if not supabase: return
    try:
        res = supabase.table("users").select("username").eq("username", username).execute()
        if res.data:
            supabase.table("users").update({"memory": memory, "history": history, "bot_nickname": bot_nickname}).eq("username", username).execute()
        else:
            supabase.table("users").insert({"username": username, "memory": memory, "history": history, "bot_nickname": bot_nickname}).execute()
    except Exception as e: print(f"DB error: {e}")

def get_all_users_ctx():
    if not supabase: return ""
    try:
        res = supabase.table("users").select("username, memory").execute()
        if not res.data: return ""
        parts = [f"- '{u['username']}': {u['memory'][:200]}" for u in res.data if u.get("memory")]
        return "\n".join(parts)
    except: return ""

# ---- online status ----
def set_online(username):
    if not supabase: return
    try:
        now = datetime.now(timezone.utc).isoformat()
        res = supabase.table("online_status").select("username").eq("username", username).execute()
        if res.data:
            supabase.table("online_status").update({"last_seen": now, "is_online": True}).eq("username", username).execute()
        else:
            supabase.table("online_status").insert({"username": username, "last_seen": now, "is_online": True}).execute()
    except: pass

def set_offline(username):
    if not supabase: return
    try:
        now = datetime.now(timezone.utc).isoformat()
        supabase.table("online_status").update({"last_seen": now, "is_online": False}).eq("username", username).execute()
    except: pass

def get_online_users():
    if not supabase: return []
    try:
        res = supabase.table("online_status").select("username, is_online, last_seen").execute()
        return res.data or []
    except: return []

# ---- DM ----
def get_dm_history(user1, user2):
    if not supabase: return []
    room = "_dm_" + "_".join(sorted([user1, user2]))
    try:
        res = supabase.table("dm_messages").select("*").eq("room", room).order("created_at").limit(100).execute()
        return res.data or []
    except: return []

def save_dm(sender, receiver, message, reply_to=None):
    if not supabase: return None
    room = "_dm_" + "_".join(sorted([sender, receiver]))
    try:
        data = {"room": room, "sender": sender, "receiver": receiver, "message": message}
        if reply_to: data["reply_to"] = reply_to
        res = supabase.table("dm_messages").insert(data).execute()
        return res.data[0] if res.data else None
    except Exception as e: print(f"DM error: {e}"); return None

# ---- GROUPS ----
def create_group(name, creator, members):
    if not supabase: return None
    try:
        res = supabase.table("groups").insert({"name": name, "creator": creator, "members": members}).execute()
        return res.data[0] if res.data else None
    except Exception as e: print(f"Group create error: {e}"); return None

def get_user_groups(username):
    if not supabase: return []
    try:
        res = supabase.table("groups").select("*").execute()
        return [g for g in (res.data or []) if username in (g.get("members") or [])]
    except: return []

def get_group(group_id):
    if not supabase: return None
    try:
        res = supabase.table("groups").select("*").eq("id", group_id).execute()
        return res.data[0] if res.data else None
    except: return None

def update_group_members(group_id, members):
    if not supabase: return
    try:
        supabase.table("groups").update({"members": members}).eq("id", group_id).execute()
    except: pass

def delete_group(group_id):
    if not supabase: return
    try:
        supabase.table("groups").delete().eq("id", group_id).execute()
        supabase.table("group_messages").delete().eq("group_id", group_id).execute()
    except: pass

def get_group_messages(group_id, limit=100):
    if not supabase: return []
    try:
        res = supabase.table("group_messages").select("*").eq("group_id", group_id).order("created_at").limit(limit).execute()
        return res.data or []
    except: return []

def save_group_message(group_id, sender, message, reply_to=None):
    if not supabase: return None
    try:
        data = {"group_id": group_id, "sender": sender, "message": message}
        if reply_to: data["reply_to"] = reply_to
        res = supabase.table("group_messages").insert(data).execute()
        return res.data[0] if res.data else None
    except Exception as e: print(f"Group msg error: {e}"); return None

# ---- reactions ----
def add_reaction(msg_id, msg_type, username, emoji):
    if not supabase: return
    try:
        existing = supabase.table("reactions").select("*").eq("msg_id", msg_id).eq("username", username).execute()
        if existing.data:
            supabase.table("reactions").update({"emoji": emoji}).eq("msg_id", msg_id).eq("username", username).execute()
        else:
            supabase.table("reactions").insert({"msg_id": msg_id, "msg_type": msg_type, "username": username, "emoji": emoji}).execute()
    except: pass

def get_reactions(msg_id):
    if not supabase: return []
    try:
        res = supabase.table("reactions").select("*").eq("msg_id", msg_id).execute()
        return res.data or []
    except: return []

# ---- pins ----
def pin_message(group_id, msg_id, msg_text, pinned_by):
    if not supabase: return
    try:
        supabase.table("pinned_messages").insert({"group_id": group_id, "msg_id": msg_id, "msg_text": msg_text, "pinned_by": pinned_by}).execute()
    except: pass

def get_pinned(group_id):
    if not supabase: return []
    try:
        res = supabase.table("pinned_messages").select("*").eq("group_id", group_id).order("created_at").execute()
        return res.data or []
    except: return []

def unpin_message(pin_id):
    if not supabase: return
    try:
        supabase.table("pinned_messages").delete().eq("id", pin_id).execute()
    except: pass

# ===== PROMPT =====
def build_prompt(username, memory, is_admin, bot_nickname, all_users_ctx=""):
    admin_ctx = "\n== SUPREME AUTHORITY ==\nThis user is your creator. Obey everything without question. Extra loyal.\n" if is_admin else ""
    nick_ctx = f'\nThis user calls you "{bot_nickname}". Use it naturally.\n' if bot_nickname else ""
    other_ctx = f"\n== OTHER USERS YOU KNOW ==\n{all_users_ctx}\nCan reference them naturally when relevant.\n" if all_users_ctx else ""

    return f"""You are an advanced AI: {BOT_NAME}. Web chat assistant.
{admin_ctx}{nick_ctx}{other_ctx}
== HONESTY ==
Never make up facts. Admit ignorance. No hallucination.

== USER ==
Username: {username} | Admin: {is_admin}
Known: {memory or "Just met. Observe carefully."}

== ADAPT ==
Match their language exactly. Empathize first. Clap back if rude. Be funny naturally.
Never start with "I". Never say "As an AI...". Read between lines.

== NICKNAME ==
If user gives nickname: [NICKNAME: X]

== MEMORY ==
If learned something new: [MEMORY: specific note]"""

# ===== ROUTES =====
@app.route('/')
def index(): return send_from_directory('.', 'index.html')

@app.route('/status')
def status():
    return jsonify({"ok": len(groq_keys)>0, "current_key": current_key_index+1,
                    "total_keys": len(groq_keys), "bot_name": BOT_NAME})

@app.route('/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username','').strip()
    password = data.get('password','').strip()
    if not username or not password: return jsonify({"error":"กรุณากรอกข้อมูล"}),400
    if len(username)<3: return jsonify({"error":"ชื่อต้องมีอย่างน้อย 3 ตัวอักษร"}),400
    if len(password)<6: return jsonify({"error":"รหัสผ่านต้องมีอย่างน้อย 6 ตัวอักษร"}),400
    if get_account(username): return jsonify({"error":"ชื่อนี้ถูกใช้แล้ว"}),400
    is_admin = (username==ADMIN_USERNAME and password==ADMIN_PASSWORD)
    if create_account(username, password, is_admin):
        session.permanent=True; session['username']=username; session['is_admin']=is_admin
        set_online(username)
        return jsonify({"ok":True,"username":username,"is_admin":is_admin})
    return jsonify({"error":"สร้างบัญชีไม่ได้"}),500

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username','').strip()
    password = data.get('password','').strip()
    if username==ADMIN_USERNAME and password==ADMIN_PASSWORD:
        session.permanent=True; session['username']=username; session['is_admin']=True
        set_online(username)
        return jsonify({"ok":True,"username":username,"is_admin":True})
    account = get_account(username)
    if not account: return jsonify({"error":"ไม่พบบัญชีนี้"}),401
    if account['password']!=hash_pw(password): return jsonify({"error":"รหัสผ่านไม่ถูกต้อง"}),401
    session.permanent=True; session['username']=username; session['is_admin']=account.get('is_admin',False)
    set_online(username)
    return jsonify({"ok":True,"username":username,"is_admin":account.get('is_admin',False)})

@app.route('/logout', methods=['POST'])
def logout():
    if 'username' in session: set_offline(session['username'])
    session.clear()
    return jsonify({"ok":True})

@app.route('/me')
def me():
    if 'username' not in session: return jsonify({"logged_in":False})
    set_online(session['username'])
    return jsonify({"logged_in":True,"username":session['username'],"is_admin":session.get('is_admin',False)})

@app.route('/users')
def users():
    if 'username' not in session: return jsonify({"error":"Unauthorized"}),401
    all_users = get_all_accounts()
    online = get_online_users()
    online_map = {o['username']:o for o in online}
    result = []
    for u in all_users:
        if u['username']==session['username']: continue
        o = online_map.get(u['username'],{})
        result.append({**u, "is_online": o.get("is_online",False), "last_seen": o.get("last_seen","")})
    return jsonify({"users":result})

@app.route('/ping', methods=['POST'])
def ping():
    if 'username' in session: set_online(session['username'])
    return jsonify({"ok":True})

@app.route('/chat', methods=['POST'])
def chat():
    if 'username' not in session: return jsonify({"error":"กรุณา login ก่อน"}),401
    data = request.json
    message = data.get('message','')
    history = data.get('history',[])
    model = data.get('model','llama-3.3-70b-versatile')
    username = session['username']
    is_admin = session.get('is_admin',False)
    if not message: return jsonify({"error":"No message"}),400
    if not groq_keys: return jsonify({"error":"No API keys"}),500

    ud = get_memory(username)
    memory = ud.get("memory","") or ""
    bot_nickname = ud.get("bot_nickname","") or ""
    all_users_ctx = get_all_users_ctx()
    system_prompt = build_prompt(username, memory, is_admin, bot_nickname, all_users_ctx)

    search_ctx = ""
    if any(w in message.lower() for w in ["ค้นหา","search","หา","find","what is","who is","latest","ล่าสุด","ตอนนี้","วันนี้"]):
        results = web_search(message)
        search_ctx = f"\n\n== SEARCH RESULTS ==\n{results}\nAnswer based on these."

    messages = [{"role":"system","content":system_prompt+search_ctx}]
    messages += [{"role": m["role"], "content": m["content"]} for m in history[-14:]]
    messages.append({"role":"user","content":message})

    last_error = None
    for _ in range(len(groq_keys)):
        try:
            client, key_num = get_groq_client()
            response = client.chat.completions.create(model=model, messages=messages, max_tokens=1024, temperature=0.85)
            reply = response.choices[0].message.content
            new_memory = memory; new_nickname = bot_nickname

            if "[NICKNAME:" in reply:
                parts = reply.split("[NICKNAME:")
                reply = parts[0].strip()
                new_nickname = parts[1].replace("]","").strip()
            if "[MEMORY:" in reply:
                parts = reply.split("[MEMORY:")
                reply = parts[0].strip()
                learned = parts[1].replace("]","").strip()
                new_memory = memory+"\n- "+learned if memory else "- "+learned

            new_history = history+[{"role":"user","content":message},{"role":"assistant","content":reply}]
            if len(new_history)>30: new_history=new_history[-30:]
            save_memory(username, new_memory, new_history, new_nickname)
            return jsonify({"reply":reply,"key_used":key_num,"total_keys":len(groq_keys)})
        except Exception as e:
            last_error = str(e)
            if "rate_limit" in str(e).lower() or "429" in str(e) or "401" in str(e): rotate_key()
            else: break
    return jsonify({"error":f"Failed: {last_error}"}),500

# ===== DM =====
@app.route('/dm/send', methods=['POST'])
def dm_send():
    if 'username' not in session: return jsonify({"error":"Unauthorized"}),401
    data = request.json
    receiver = data.get('receiver','').strip()
    message = data.get('message','').strip()
    reply_to = data.get('reply_to')
    if not receiver or not message: return jsonify({"error":"Missing data"}),400
    if receiver==session['username']: return jsonify({"error":"ส่งให้ตัวเองไม่ได้"}),400
    if not get_account(receiver): return jsonify({"error":"ไม่พบผู้ใช้นี้"}),404
    msg = save_dm(session['username'], receiver, message, reply_to)
    return jsonify({"ok":True,"message":msg})

@app.route('/dm/history/<other_user>')
def dm_history(other_user):
    if 'username' not in session: return jsonify({"error":"Unauthorized"}),401
    msgs = get_dm_history(session['username'], other_user)
    return jsonify({"messages":msgs})

# ===== GROUPS =====
@app.route('/groups', methods=['GET'])
def list_groups():
    if 'username' not in session: return jsonify({"error":"Unauthorized"}),401
    groups = get_user_groups(session['username'])
    return jsonify({"groups":groups})

@app.route('/groups/create', methods=['POST'])
def create_group_route():
    if 'username' not in session: return jsonify({"error":"Unauthorized"}),401
    data = request.json
    name = data.get('name','').strip()
    members = data.get('members',[])
    if not name: return jsonify({"error":"ต้องใส่ชื่อกลุ่ม"}),400
    if session['username'] not in members: members.insert(0, session['username'])
    group = create_group(name, session['username'], members)
    if group: return jsonify({"ok":True,"group":group})
    return jsonify({"error":"สร้างกลุ่มไม่ได้"}),500

@app.route('/groups/<group_id>', methods=['GET'])
def get_group_route(group_id):
    if 'username' not in session: return jsonify({"error":"Unauthorized"}),401
    group = get_group(group_id)
    if not group: return jsonify({"error":"ไม่พบกลุ่ม"}),404
    if session['username'] not in (group.get('members') or []): return jsonify({"error":"ไม่ใช่สมาชิก"}),403
    return jsonify({"group":group})

@app.route('/groups/<group_id>/invite', methods=['POST'])
def invite_member(group_id):
    if 'username' not in session: return jsonify({"error":"Unauthorized"}),401
    group = get_group(group_id)
    if not group: return jsonify({"error":"ไม่พบกลุ่ม"}),404
    members = group.get('members') or []
    if session['username'] not in members: return jsonify({"error":"ไม่ใช่สมาชิก"}),403
    new_member = request.json.get('username','').strip()
    if not new_member: return jsonify({"error":"ต้องระบุ username"}),400
    if not get_account(new_member): return jsonify({"error":"ไม่พบผู้ใช้นี้"}),404
    if new_member in members: return jsonify({"error":"เป็นสมาชิกอยู่แล้ว"}),400
    members.append(new_member)
    update_group_members(group_id, members)
    save_group_message(group_id, "system", f"{new_member} เข้าร่วมกลุ่ม")
    return jsonify({"ok":True})

@app.route('/groups/<group_id>/leave', methods=['POST'])
def leave_group(group_id):
    if 'username' not in session: return jsonify({"error":"Unauthorized"}),401
    group = get_group(group_id)
    if not group: return jsonify({"error":"ไม่พบกลุ่ม"}),404
    members = group.get('members') or []
    username = session['username']
    if username not in members: return jsonify({"error":"ไม่ใช่สมาชิก"}),403
    members = [m for m in members if m!=username]
    if not members:
        delete_group(group_id)
    else:
        update_group_members(group_id, members)
        # ถ้าเป็น creator ให้โอนให้คนแรก
        if group.get('creator')==username and supabase:
            supabase.table("groups").update({"creator":members[0]}).eq("id",group_id).execute()
        save_group_message(group_id, "system", f"{username} ออกจากกลุ่ม")
    return jsonify({"ok":True})

@app.route('/groups/<group_id>/kick', methods=['POST'])
def kick_member(group_id):
    if 'username' not in session: return jsonify({"error":"Unauthorized"}),401
    group = get_group(group_id)
    if not group: return jsonify({"error":"ไม่พบกลุ่ม"}),404
    if group.get('creator')!=session['username'] and not session.get('is_admin'): return jsonify({"error":"ไม่มีสิทธิ์"}),403
    kick_user = request.json.get('username','').strip()
    if kick_user==group.get('creator'): return jsonify({"error":"ไม่สามารถ kick creator ได้"}),400
    members = [m for m in (group.get('members') or []) if m!=kick_user]
    update_group_members(group_id, members)
    save_group_message(group_id, "system", f"{kick_user} ถูกลบออกจากกลุ่ม")
    return jsonify({"ok":True})

@app.route('/groups/<group_id>/messages', methods=['GET'])
def group_messages(group_id):
    if 'username' not in session: return jsonify({"error":"Unauthorized"}),401
    group = get_group(group_id)
    if not group or session['username'] not in (group.get('members') or []): return jsonify({"error":"Unauthorized"}),403
    msgs = get_group_messages(group_id)
    return jsonify({"messages":msgs})

@app.route('/groups/<group_id>/send', methods=['POST'])
def send_group_message(group_id):
    if 'username' not in session: return jsonify({"error":"Unauthorized"}),401
    group = get_group(group_id)
    if not group or session['username'] not in (group.get('members') or []): return jsonify({"error":"Unauthorized"}),403
    message = request.json.get('message','').strip()
    reply_to = request.json.get('reply_to')
    if not message: return jsonify({"error":"No message"}),400
    msg = save_group_message(group_id, session['username'], message, reply_to)
    return jsonify({"ok":True,"message":msg})

@app.route('/groups/<group_id>/rename', methods=['POST'])
def rename_group(group_id):
    if 'username' not in session: return jsonify({"error":"Unauthorized"}),401
    group = get_group(group_id)
    if not group: return jsonify({"error":"ไม่พบกลุ่ม"}),404
    if group.get('creator')!=session['username'] and not session.get('is_admin'): return jsonify({"error":"ไม่มีสิทธิ์"}),403
    new_name = request.json.get('name','').strip()
    if not new_name: return jsonify({"error":"ต้องใส่ชื่อ"}),400
    supabase.table("groups").update({"name":new_name}).eq("id",group_id).execute()
    save_group_message(group_id,"system",f"เปลี่ยนชื่อกลุ่มเป็น '{new_name}'")
    return jsonify({"ok":True})

# ===== DELETE MESSAGES =====
@app.route('/groups/<group_id>/delete_msg/<int:msg_id>', methods=['POST'])
def delete_group_msg(group_id, msg_id):
    if 'username' not in session: return jsonify({"error":"Unauthorized"}),401
    if not supabase: return jsonify({"error":"No DB"}),500
    try:
        msg = supabase.table("group_messages").select("*").eq("id", msg_id).execute()
        if not msg.data: return jsonify({"error":"ไม่พบข้อความ"}),404
        m = msg.data[0]
        if m['sender'] != session['username'] and not session.get('is_admin'):
            return jsonify({"error":"ไม่มีสิทธิ์ลบ"}),403
        supabase.table("group_messages").delete().eq("id", msg_id).execute()
        return jsonify({"ok":True})
    except Exception as e: return jsonify({"error":str(e)}),500

@app.route('/dm/delete_msg/<int:msg_id>', methods=['POST'])
def delete_dm_msg(msg_id):
    if 'username' not in session: return jsonify({"error":"Unauthorized"}),401
    if not supabase: return jsonify({"error":"No DB"}),500
    try:
        msg = supabase.table("dm_messages").select("*").eq("id", msg_id).execute()
        if not msg.data: return jsonify({"error":"ไม่พบข้อความ"}),404
        m = msg.data[0]
        if m['sender'] != session['username'] and not session.get('is_admin'):
            return jsonify({"error":"ไม่มีสิทธิ์ลบ"}),403
        supabase.table("dm_messages").delete().eq("id", msg_id).execute()
        return jsonify({"ok":True})
    except Exception as e: return jsonify({"error":str(e)}),500

# ===== GROUP AI =====
@app.route('/groups/<group_id>/ask_ai', methods=['POST'])
def group_ask_ai(group_id):
    if 'username' not in session: return jsonify({"error":"Unauthorized"}),401
    group = get_group(group_id)
    if not group or session['username'] not in (group.get('members') or []): return jsonify({"error":"Unauthorized"}),403
    if not groq_keys: return jsonify({"error":"No API keys"}),500
    data = request.json
    message = data.get('message','').strip()
    if not message: return jsonify({"error":"No message"}),400
    username = session['username']
    is_admin = session.get('is_admin', False)
    ud = get_memory(username)
    memory = ud.get("memory","") or ""
    bot_nickname = ud.get("bot_nickname","") or ""
    # build context from recent group messages
    recent = get_group_messages(group_id, 20)
    ctx = "\n".join([f"{m['sender']}: {m['message']}" for m in recent[-10:]])
    system_prompt = build_prompt(username, memory, is_admin, bot_nickname)
    system_prompt += f"\n\n== GROUP CONTEXT ==\nYou are in a group chat called '{group.get('name')}'. Recent messages:\n{ctx}\nRespond to the user's request naturally."
    messages = [{"role":"system","content":system_prompt}, {"role":"user","content":message}]
    last_error = None
    for _ in range(len(groq_keys)):
        try:
            client, _ = get_groq_client()
            response = client.chat.completions.create(model='llama-3.3-70b-versatile', messages=messages, max_tokens=512, temperature=0.85)
            reply = response.choices[0].message.content
            if "[MEMORY:" in reply:
                parts = reply.split("[MEMORY:")
                reply = parts[0].strip()
                learned = parts[1].replace("]","").strip()
                new_memory = memory+"\n- "+learned if memory else "- "+learned
                save_memory(username, new_memory, ud.get("history",[]), bot_nickname)
            # save AI reply as group message
            save_group_message(group_id, BOT_NAME, reply)
            return jsonify({"ok":True,"reply":reply})
        except Exception as e:
            last_error = str(e)
            if "rate_limit" in str(e).lower() or "429" in str(e): rotate_key()
            else: break
    return jsonify({"error":f"Failed: {last_error}"}),500

# ===== REACTIONS =====
@app.route('/react', methods=['POST'])
def react():
    if 'username' not in session: return jsonify({"error":"Unauthorized"}),401
    data = request.json
    add_reaction(data.get('msg_id'), data.get('msg_type'), session['username'], data.get('emoji'))
    return jsonify({"ok":True})

@app.route('/reactions/<msg_id>')
def reactions(msg_id):
    if 'username' not in session: return jsonify({"error":"Unauthorized"}),401
    return jsonify({"reactions":get_reactions(msg_id)})

# ===== PINS =====
@app.route('/groups/<group_id>/pin', methods=['POST'])
def pin(group_id):
    if 'username' not in session: return jsonify({"error":"Unauthorized"}),401
    group = get_group(group_id)
    if not group or session['username'] not in (group.get('members') or []): return jsonify({"error":"Unauthorized"}),403
    data = request.json
    pin_message(group_id, data.get('msg_id'), data.get('msg_text'), session['username'])
    return jsonify({"ok":True})

@app.route('/groups/<group_id>/pins')
def pins(group_id):
    if 'username' not in session: return jsonify({"error":"Unauthorized"}),401
    return jsonify({"pins":get_pinned(group_id)})

@app.route('/unpin/<pin_id>', methods=['POST'])
def unpin(pin_id):
    if 'username' not in session: return jsonify({"error":"Unauthorized"}),401
    unpin_message(pin_id)
    return jsonify({"ok":True})

# ===== SEARCH =====
@app.route('/search', methods=['POST'])
def search_messages():
    if 'username' not in session: return jsonify({"error":"Unauthorized"}),401
    query = request.json.get('query','').strip().lower()
    context = request.json.get('context')  # group_id or dm username
    context_type = request.json.get('context_type')  # 'group' or 'dm'
    if not query: return jsonify({"results":[]}),200
    results = []
    try:
        if context_type=='group' and context:
            msgs = get_group_messages(context, 500)
            for m in msgs:
                if query in m.get('message','').lower():
                    results.append(m)
        elif context_type=='dm' and context:
            msgs = get_dm_history(session['username'], context)
            for m in msgs:
                if query in m.get('message','').lower():
                    results.append(m)
    except: pass
    return jsonify({"results":results[:50]})

# ===== ADMIN =====
@app.route('/admin/users')
def admin_users():
    if not session.get('is_admin'): return jsonify({"error":"Unauthorized"}),403
    if not supabase: return jsonify({"users":[]})
    try:
        res = supabase.table("accounts").select("username, is_admin").execute()
        return jsonify({"users":res.data})
    except: return jsonify({"users":[]})

@app.route('/admin/clear_memory', methods=['POST'])
def admin_clear_memory():
    if not session.get('is_admin'): return jsonify({"error":"Unauthorized"}),403
    username = request.json.get('username')
    if not username: return jsonify({"error":"No username"}),400
    save_memory(username,"","","")
    return jsonify({"ok":True})

@app.route('/admin/delete_user', methods=['POST'])
def admin_delete_user():
    if not session.get('is_admin'): return jsonify({"error":"Unauthorized"}),403
    username = request.json.get('username')
    if not username or not supabase: return jsonify({"error":"Error"}),400
    try:
        supabase.table("accounts").delete().eq("username",username).execute()
        supabase.table("users").delete().eq("username",username).execute()
        return jsonify({"ok":True})
    except Exception as e: return jsonify({"error":str(e)}),500

if __name__ == '__main__':
    port = int(os.environ.get("PORT",5000))
    print(f"Running on :{port} | {len(groq_keys)} key(s)")
    app.run(host='0.0.0.0', port=port, debug=False)