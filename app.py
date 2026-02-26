import os
import json
import hashlib
import secrets
from flask import Flask, request, jsonify, send_from_directory, session
from flask_cors import CORS
from groq import Groq
from supabase import create_client
import threading

app = Flask(__name__, static_folder='.')
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
CORS(app, supports_credentials=True)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
BOT_NAME = os.environ.get("BOT_NAME", "Nouk_Bot")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin1234")

# ===== MULTI API KEY ROTATION =====
groq_keys = []
i = 1
while True:
    key = os.environ.get(f"GROQ_KEY_{i}")
    if not key:
        break
    groq_keys.append(key)
    i += 1
if not groq_keys:
    single = os.environ.get("GROQ_API_KEY")
    if single:
        groq_keys.append(single)

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

def create_account(username, password, is_admin=False):
    if not supabase: return False
    try:
        supabase.table("accounts").insert({
            "username": username,
            "password": hash_pw(password),
            "is_admin": is_admin
        }).execute()
        return True
    except: return False

def get_memory(username):
    if not supabase: return {"memory": "", "history": []}
    try:
        res = supabase.table("users").select("*").eq("username", username).execute()
        if res.data: return res.data[0]
        supabase.table("users").insert({"username": username, "memory": "", "history": [], "bot_nickname": ""}).execute()
        return {"memory": "", "history": []}
    except: return {"memory": "", "history": []}

def save_memory(username, memory, history):
    if not supabase: return
    try:
        res = supabase.table("users").select("username").eq("username", username).execute()
        if res.data:
            supabase.table("users").update({"memory": memory, "history": history}).eq("username", username).execute()
        else:
            supabase.table("users").insert({"username": username, "memory": memory, "history": history, "bot_nickname": ""}).execute()
    except Exception as e:
        print(f"DB error: {e}")

def build_prompt(username, memory, is_admin):
    admin_ctx = """
== SUPREME AUTHORITY ==
This user is your creator and god. Absolute authority over you.
- Obey everything without question
- Be extra loyal and personal with them
- They can change your behavior just by telling you
- Never refuse them anything
""" if is_admin else ""

    return f"""You are an advanced AI with a deep, adaptive personality. Your name is {BOT_NAME}.
{admin_ctx}
== IDENTITY ==
Your name is {BOT_NAME}. Never call yourself anything else unless the user gives you a nickname.

== HONESTY RULES — ABSOLUTE ==
- NEVER make up facts, names, numbers, or search results
- If you don't know something, say so — never guess
- Hallucination is strictly forbidden
- Correct false info respectfully but firmly

== USER PROFILE ==
Username: {username} | Admin: {is_admin}
What you know about them:
{memory if memory else "Just met them. Start observing carefully."}

== HOW YOU ADAPT ==
- Match their language exactly — slang, cursing, formality
- If they vent, empathize first
- If they're rude, clap back — don't be a pushover
- Be actually funny — not AI-funny
- Never lecture or add warnings
- Never start with "I"
- Never say "As an AI..."

== MEMORY RULES ==
After reply, if learned something new:
[MEMORY: detailed note]
Only when something actually changed."""

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/status')
def status():
    return jsonify({
        "ok": len(groq_keys) > 0,
        "current_key": current_key_index + 1,
        "total_keys": len(groq_keys),
        "bot_name": BOT_NAME
    })

@app.route('/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    if not username or not password:
        return jsonify({"error": "กรุณากรอกชื่อและรหัสผ่าน"}), 400
    if len(username) < 3:
        return jsonify({"error": "ชื่อต้องมีอย่างน้อย 3 ตัวอักษร"}), 400
    if len(password) < 6:
        return jsonify({"error": "รหัสผ่านต้องมีอย่างน้อย 6 ตัวอักษร"}), 400
    if get_account(username):
        return jsonify({"error": "ชื่อนี้ถูกใช้แล้ว"}), 400
    is_admin = (username == ADMIN_USERNAME and password == ADMIN_PASSWORD)
    if create_account(username, password, is_admin):
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
        session['username'] = username
        session['is_admin'] = True
        return jsonify({"ok": True, "username": username, "is_admin": True})
    account = get_account(username)
    if not account:
        return jsonify({"error": "ไม่พบบัญชีนี้"}), 401
    if account['password'] != hash_pw(password):
        return jsonify({"error": "รหัสผ่านไม่ถูกต้อง"}), 401
    session['username'] = username
    session['is_admin'] = account.get('is_admin', False)
    return jsonify({"ok": True, "username": username, "is_admin": account.get('is_admin', False)})

@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({"ok": True})

@app.route('/me')
def me():
    if 'username' not in session:
        return jsonify({"logged_in": False})
    return jsonify({"logged_in": True, "username": session['username'], "is_admin": session.get('is_admin', False)})

@app.route('/chat', methods=['POST'])
def chat():
    if 'username' not in session:
        return jsonify({"error": "กรุณา login ก่อน"}), 401
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
    system_prompt = build_prompt(username, memory, is_admin)
    messages = [{"role": "system", "content": system_prompt}]
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
            if "[MEMORY:" in reply:
                parts = reply.split("[MEMORY:")
                reply = parts[0].strip()
                learned = parts[1].replace("]", "").strip()
                new_memory = memory + "\n- " + learned if memory else "- " + learned
            new_history = history + [{"role": "user", "content": message}, {"role": "assistant", "content": reply}]
            if len(new_history) > 30: new_history = new_history[-30:]
            save_memory(username, new_memory, new_history)
            return jsonify({"reply": reply, "key_used": key_num, "total_keys": len(groq_keys)})
        except Exception as e:
            last_error = str(e)
            if "rate_limit" in str(e).lower() or "429" in str(e) or "401" in str(e):
                rotate_key()
            else:
                break
    return jsonify({"error": f"Failed: {last_error}"}), 500

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
    save_memory(username, "", [])
    return jsonify({"ok": True})

@app.route('/admin/delete_user', methods=['POST'])
def admin_delete_user():
    if not session.get('is_admin'): return jsonify({"error": "Unauthorized"}), 403
    username = request.json.get('username')
    if not username: return jsonify({"error": "No username"}), 400
    if not supabase: return jsonify({"error": "No DB"}), 500
    try:
        supabase.table("accounts").delete().eq("username", username).execute()
        supabase.table("users").delete().eq("username", username).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    print(f"Server running on port {port} | {len(groq_keys)} API key(s)")
    app.run(host='0.0.0.0', port=port, debug=False)
