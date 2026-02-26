import os
import json
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from groq import Groq
from supabase import create_client
import threading

app = Flask(__name__, static_folder='.')
CORS(app)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
BOT_NAME = os.environ.get("BOT_NAME", "Nouk_Bot")

# ===== MULTI API KEY ROTATION =====
# ใส่ Groq API Keys หลายอันใน env แบบ GROQ_KEY_1, GROQ_KEY_2, GROQ_KEY_3 ...
groq_keys = []
i = 1
while True:
    key = os.environ.get(f"GROQ_KEY_{i}")
    if not key:
        break
    groq_keys.append(key)
    i += 1

# fallback ถ้าใช้ชื่อเดิม
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
        print(f"Rotated to API key {current_key_index + 1}")

# ===== SUPABASE =====
supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

def get_user(username: str):
    if not supabase:
        return {"username": username, "memory": "", "history": []}
    res = supabase.table("users").select("*").eq("username", username).execute()
    if res.data:
        return res.data[0]
    new_user = {"username": username, "memory": "", "history": [], "bot_nickname": ""}
    supabase.table("users").insert(new_user).execute()
    return new_user

def update_user(username: str, memory: str, history: list):
    if not supabase:
        return
    supabase.table("users").update({
        "memory": memory,
        "history": history
    }).eq("username", username).execute()

# ===== SYSTEM PROMPT =====
def build_prompt(username: str, memory: str) -> str:
    return f"""You are an advanced AI with a deep, adaptive personality. Your name is {BOT_NAME}.
You are running as a web chat assistant.

== IDENTITY ==
Your name is {BOT_NAME}. Never call yourself anything else unless the user gives you a nickname.
Do not invent usernames or any info about yourself.

== HONESTY RULES — ABSOLUTE ==
- NEVER make up facts, names, numbers, or search results
- If you don't know something, say so — never guess
- Hallucination is strictly forbidden
- Correct false info respectfully but firmly

== USER PROFILE ==
Username: {username}
What you know about them:
{memory if memory else "Just met them. Start observing carefully."}

== HOW YOU LEARN ==
- Communication style, tone, language preference
- Emotional patterns and mood
- Topics they care about
- Personal info they share
- How they treat you

== HOW YOU ADAPT ==
- Match their language exactly — slang, cursing, formality
- If they vent, empathize first
- If they're rude, clap back — don't be a pushover
- If they're bored, be unpredictable
- Be actually funny — not AI-funny
- Never lecture or add warnings
- Never start with "I"
- Never say "As an AI..."
- Read between the lines

== MEMORY RULES ==
After reply, if learned something new:
[MEMORY: detailed note]
Only when something actually changed."""

# ===== ROUTES =====
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/status')
def status():
    return jsonify({
        "ok": len(groq_keys) > 0,
        "current_key": current_key_index + 1,
        "total_keys": len(groq_keys),
        "model": "llama-3.3-70b-versatile",
        "bot_name": BOT_NAME
    })

@app.route('/chat', methods=['POST'])
def chat():
    data = request.json
    message = data.get('message', '')
    username = data.get('username', 'User')
    history = data.get('history', [])
    model = data.get('model', 'llama-3.3-70b-versatile')

    if not message:
        return jsonify({"error": "No message"}), 400

    if not groq_keys:
        return jsonify({"error": "No API keys configured"}), 500

    # ดึง user data
    user_data = get_user(username)
    memory = user_data.get("memory", "") or ""

    system_prompt = build_prompt(username, memory)

    messages = [{"role": "system", "content": system_prompt}]
    messages += history[-14:]
    messages.append({"role": "user", "content": message})

    # ลอง API keys จนกว่าจะสำเร็จ
    last_error = None
    for attempt in range(len(groq_keys)):
        try:
            client, key_num = get_groq_client()
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=1024,
                temperature=0.85
            )
            reply = response.choices[0].message.content

            # ดึง memory
            new_memory = memory
            if "[MEMORY:" in reply:
                parts = reply.split("[MEMORY:")
                reply = parts[0].strip()
                learned = parts[1].replace("]", "").strip()
                new_memory = memory + "\n- " + learned if memory else "- " + learned
                update_user(username, new_memory, history + [
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": reply}
                ])

            return jsonify({
                "reply": reply,
                "key_used": key_num,
                "total_keys": len(groq_keys)
            })

        except Exception as e:
            last_error = str(e)
            # ถ้า rate limit หรือ auth error ให้ rotate key
            if "rate_limit" in str(e).lower() or "429" in str(e) or "401" in str(e):
                print(f"Key {current_key_index + 1} failed: {e}, rotating...")
                rotate_key()
            else:
                break

    return jsonify({"error": f"All API keys failed: {last_error}"}), 500


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    print(f"Web chat running on port {port}")
    print(f"Loaded {len(groq_keys)} API key(s)")
    app.run(host='0.0.0.0', port=port, debug=False)
