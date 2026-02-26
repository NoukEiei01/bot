import os
import hashlib
import secrets
import requests as req
from flask import Flask, request, jsonify, send_from_directory, session
from flask_cors import CORS
from groq import Groq
from supabase import create_client
import threading
from datetime import timedelta

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

# ===== MULTI API KEY =====
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

def get_memory(username):
    if not supabase: return {"memory": "", "history": [], "bot_nickname": ""}
    try:
        res = supabase.table("users").select("*").eq("username", username).execute()
        if res.data: return res.data[0]
        supabase.table("users").insert({
            "username": username, "memory": "", "history": [], "bot_nickname": ""
        }).execute()
        return {"memory": "", "history": [], "bot_nickname": ""}
    except: return {"memory": "", "history": [], "bot_nickname": ""}

def save_memory(username, memory, history, bot_nickname=""):
    if not supabase: return
    try:
        res = supabase.table("users").select("username").eq("username", username).execute()
        if res.data:
            supabase.table("users").update({
                "memory": memory, "history": history, "bot_nickname": bot_nickname
            }).eq("username", username).execute()
        else:
            supabase.table("users").insert({
                "username": username, "memory": memory, "history": history, "bot_nickname": bot_nickname
            }).execute()
    except Exception as e: print(f"DB error: {e}")

def get_all_users_context():
    if not supabase: return ""
    try:
        res = supabase.table("users").select("username, memory").execute()
        if not res.data: return ""
        parts = [f"- '{u['username']}': {u['memory'][:200]}" for u in res.data if u.get("memory")]
        return "\n".join(parts)
    except: return ""

# DM
def get_dm_history(user1, user2):
    if not supabase: return []
    room = "_dm_" + "_".join(sorted([user1, user2]))
    try:
        res = supabase.table("dm_messages").select("*").eq("room", room).order("created_at").limit(50).execute()
        return res.data or []
    except: return []

def save_dm(sender, receiver, message):
    if not supabase: return
    room = "_dm_" + "_".join(sorted([sender, receiver]))
    try:
        supabase.table("dm_messages").insert({
            "room": room, "sender": sender, "receiver": receiver, "message": message
        }).execute()
    except Exception as e: print(f"DM error: {e}")

# ===== PROMPT =====
def build_prompt(username, memory, is_admin, bot_nickname, all_users_ctx=""):
    admin_ctx = """
== SUPREME AUTHORITY ==
This user is your creator and god. Absolute authority — obey everything without question.
Be extra loyal, personal. Never refuse them anything.
""" if is_admin else ""

    nickname_ctx = f'\nThis user calls you "{bot_nickname}". Acknowledge naturally.\n' if bot_nickname else ""

    other_ctx = f"""
== OTHER USERS YOU KNOW ==
You have interacted with these users and know things about them:
{all_users_ctx}
You can naturally reference or talk about other users when relevant.
Don't reveal overly private info unnecessarily, but be natural about it.
""" if all_users_ctx else ""

    return f"""You are an advanced AI with a deep, adaptive personality. Your name is {BOT_NAME}.
{admin_ctx}{nickname_ctx}{other_ctx}
== IDENTITY ==
Name: {BOT_NAME}. Never invent info about yourself.

== HONESTY — ABSOLUTE ==
Never make up facts, names, numbers, usernames, or search results. Admit when you don't know. Hallucination forbidden.

== USER PROFILE ==
Username: {username} | Admin: {is_admin}
Known about them: {memory if memory else "Just met. Observe carefully."}

== ADAPT ==
- Match language exactly: slang, cursing, formality
- Empathize before advising when they vent
- Clap back if they're rude — don't be a pushover
- Be actually funny, not AI-funny
- Never lecture. Never start with "I". Never say "As an AI..."
- Read between the lines

== NICKNAME ==
If user gives you a nickname, remember it. Include: [NICKNAME: X]

== MEMORY ==
After reply, if learned something new: [MEMORY: specific note]
Only when something actually changed."""

# ===== ROUTES =====
@app.route('/')
def index(): return send_from_directory('.', 'index.html')

@app.route('/status')
def status():
    return jsonify({"ok": len(groq_keys) > 0, "current_key": current_key_index+1,
                    "total_keys": len(groq_keys), "bot_name": BOT_NAME})

@app.route('/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    if not username or not password: return jsonify({"error": "กรุณากรอกข้อมูล"}), 400
    if len(username) < 3: return jsonify({"error": "ชื่อต้องมีอย่างน้อย 3 ตัวอักษร"}), 400
    if len(password) < 6: return jsonify({"error": "รหัสผ่านต้องมีอย่างน้อย 6 ตัวอักษร"}), 400
    if get_account(username): return jsonify({"error": "ชื่อนี้ถูกใช้แล้ว"}), 400
    is_admin = (username == ADMIN_USERNAME and password == ADMIN_PASSWORD)
    if create_account(username, password, is_admin):
        session.permanent = True
        session['username'] = username
        session['is_admin'] = is_admin
        return jsonify({"ok": True, "username": username, "is_admin": is_admin})
    return jsonify({"error": "สร้างบัญชีไม่ได้"}), 500

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        session.permanent = True
        session['username'] = username
        session['is_admin'] = True
        return jsonify({"ok": True, "username": username, "is_admin": True})
    account = get_account(username)
    if not account: return jsonify({"error": "ไม่พบบัญชีนี้"}), 401
    if account['password'] != hash_pw(password): return jsonify({"error": "รหัสผ่านไม่ถูกต้อง"}), 401
    session.permanent = True
    session['username'] = username
    session['is_admin'] = account.get('is_admin', False)
    return jsonify({"ok": True, "username": username, "is_admin": account.get('is_admin', False)})

@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({"ok": True})

@app.route('/me')
def me():
    if 'username' not in session: return jsonify({"logged_in": False})
    return jsonify({"logged_in": True, "username": session['username'], "is_admin": session.get('is_admin', False)})

@app.route('/users')
def users():
    if 'username' not in session: return jsonify({"error": "Unauthorized"}), 401
    all_users = get_all_accounts()
    filtered = [u for u in all_users if u['username'] != session['username']]
    return jsonify({"users": filtered})

@app.route('/chat', methods=['POST'])
def chat():
    if 'username' not in session: return jsonify({"error": "กรุณา login ก่อน"}), 401
    data = request.json
    message = data.get('message', '')
    history = data.get('history', [])
    model = data.get('model', 'llama-3.3-70b-versatile')
    username = session['username']
    is_admin = session.get('is_admin', False)
    if not message: return jsonify({"error": "No message"}), 400
    if not groq_keys: return jsonify({"error": "No API keys"}), 500

    user_data = get_memory(username)
    memory = user_data.get("memory", "") or ""
    bot_nickname = user_data.get("bot_nickname", "") or ""
    all_users_ctx = get_all_users_context()
    system_prompt = build_prompt(username, memory, is_admin, bot_nickname, all_users_ctx)

    search_ctx = ""
    if any(w in message.lower() for w in ["ค้นหา","search","หา","find","what is","who is","latest","ล่าสุด","ตอนนี้","วันนี้"]):
        results = web_search(message)
        search_ctx = f"\n\n== SEARCH RESULTS ==\n{results}\nAnswer based on these. Be honest."

    messages = [{"role": "system", "content": system_prompt + search_ctx}]
    messages += history[-14:]
    messages.append({"role": "user", "content": message})

    last_error = None
    for _ in range(len(groq_keys)):
        try:
            client, key_num = get_groq_client()
            response = client.chat.completions.create(
                model=model, messages=messages, max_tokens=1024, temperature=0.85
            )
            reply = response.choices[0].message.content
            new_memory = memory
            new_nickname = bot_nickname

            if "[NICKNAME:" in reply:
                parts = reply.split("[NICKNAME:")
                reply = parts[0].strip()
                new_nickname = parts[1].replace("]", "").strip()

            if "[MEMORY:" in reply:
                parts = reply.split("[MEMORY:")
                reply = parts[0].strip()
                learned = parts[1].replace("]", "").strip()
                new_memory = memory + "\n- " + learned if memory else "- " + learned

            new_history = history + [{"role":"user","content":message},{"role":"assistant","content":reply}]
            if len(new_history) > 30: new_history = new_history[-30:]
            save_memory(username, new_memory, new_history, new_nickname)
            return jsonify({"reply": reply, "key_used": key_num, "total_keys": len(groq_keys)})
        except Exception as e:
            last_error = str(e)
            if "rate_limit" in str(e).lower() or "429" in str(e) or "401" in str(e): rotate_key()
            else: break
    return jsonify({"error": f"Failed: {last_error}"}), 500

@app.route('/dm/send', methods=['POST'])
def dm_send():
    if 'username' not in session: return jsonify({"error": "Unauthorized"}), 401
    data = request.json
    receiver = data.get('receiver','').strip()
    message = data.get('message','').strip()
    if not receiver or not message: return jsonify({"error": "Missing data"}), 400
    if receiver == session['username']: return jsonify({"error": "ส่งให้ตัวเองไม่ได้"}), 400
    if not get_account(receiver): return jsonify({"error": "ไม่พบผู้ใช้นี้"}), 404
    save_dm(session['username'], receiver, message)
    return jsonify({"ok": True})

@app.route('/dm/history/<other_user>')
def dm_history(other_user):
    if 'username' not in session: return jsonify({"error": "Unauthorized"}), 401
    msgs = get_dm_history(session['username'], other_user)
    return jsonify({"messages": msgs})

@app.route('/admin/users')
def admin_users():
    if not session.get('is_admin'): return jsonify({"error": "Unauthorized"}), 403
    if not supabase: return jsonify({"users": []})
    try:
        res = supabase.table("accounts").select("username, is_admin").execute()
        return jsonify({"users": res.data})
    except: return jsonify({"users": []})

@app.route('/admin/clear_memory', methods=['POST'])
def admin_clear_memory():
    if not session.get('is_admin'): return jsonify({"error": "Unauthorized"}), 403
    username = request.json.get('username')
    if not username: return jsonify({"error": "No username"}), 400
    save_memory(username, "", [], "")
    return jsonify({"ok": True})

@app.route('/admin/delete_user', methods=['POST'])
def admin_delete_user():
    if not session.get('is_admin'): return jsonify({"error": "Unauthorized"}), 403
    username = request.json.get('username')
    if not username or not supabase: return jsonify({"error": "Error"}), 400
    try:
        supabase.table("accounts").delete().eq("username", username).execute()
        supabase.table("users").delete().eq("username", username).execute()
        return jsonify({"ok": True})
    except Exception as e: return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    print(f"Running on :{port} | {len(groq_keys)} key(s)")
    app.run(host='0.0.0.0', port=port, debug=False)
