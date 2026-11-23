"""Bot API long-poller for the selfbot management bot (@Ajdhhdudhdhbot).
Why this exists: the Telethon MTProto bot client in main.py intermittently
stops receiving updates (messages sit unprocessed in the Bot API queue),
so /start and the login commands go unanswered. The Bot API (getUpdates)
is the official, reliable delivery path for a bot.

This poller ONLY receives and replies via the Bot API. It never touches the
Telethon sessions. The bot token is read from main.py (same token the
Telethon bot client uses) and is never printed or stored elsewhere.

Commands:
  /start            -> replies with the userbot status (read from status.json,
                       written by main.py's heartbeat loop).
  /phone, /code, /password -> login flow; forwarded to main.py via a JSON
                       request file (botapi_request.json); main.py's IPC task
                       executes it with the user client and writes the reply
                       to botapi_reply.json, which this poller sends back.

Run: python3 botapi_poller.py  (kept alive by the panel scheduled task /
     watchdog-botapi.sh). Single instance only (flock on botapi.lock).
"""
import fcntl
import json
import os
import sys
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN_PY = os.path.join(HERE, "main.py")
STATUS_FILE = os.path.join(HERE, "botapi_status.json")
REQ_FILE = os.path.join(HERE, "botapi_request.json")
REPLY_FILE = os.path.join(HERE, "botapi_reply.json")
LOCK_FILE = os.path.join(HERE, "botapi.lock")
OFFSET_FILE = os.path.join(HERE, "botapi_offset.txt")

# --- read the bot token from main.py (same source the Telethon client uses) ---
import re
_src = open(MAIN_PY, encoding="utf-8").read()
_m = re.search(r"BOT_TOKEN\s*=\s*[\"']([^\"']+)[\"']", _src)
if not _m:
    print("NO_BOT_TOKEN_IN_MAIN", flush=True)
    sys.exit(1)
TOKEN = _m.group(1)
API = f"https://api.telegram.org/bot{TOKEN}"


def api_call(method, payload=None, timeout=35):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        API + "/" + method, data=data,
        headers={"Content-Type": "application/json"}, method="POST" if data else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def send_message(chat_id, text):
    try:
        api_call("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})
    except Exception as e:
        print(f"sendMessage failed: {e}", flush=True)


def read_status():
    try:
        with open(STATUS_FILE, encoding="utf-8") as f:
            st = json.load(f)
        # stale if older than 10 minutes
        if time.time() - st.get("ts", 0) > 600:
            return None
        return st
    except Exception:
        return None


def handle_start(chat_id):
    st = read_status()
    if st and st.get("user_connected"):
        status = "🟢 متصل"
    elif st:
        status = "🔴 قطع"
    else:
        status = "⚠️ نامشخص (وضعیت به‌روز نیست)"
    send_message(
        chat_id,
        f"✅ **ربات آنلاین است!**\nوضعیت یوزربات: {status}\n\n1️⃣ `/phone +98...`\n2️⃣ `/code 12345`",
    )


def forward_login(chat_id, text):
    """Write a login request for main.py's IPC task; wait for its reply file."""
    req_id = str(int(time.time() * 1000))
    req = {"id": req_id, "chat_id": chat_id, "text": text}
    with open(REQ_FILE, "w", encoding="utf-8") as f:
        json.dump(req, f)
    # wait up to 60s for main.py to write the reply
    for _ in range(120):
        time.sleep(0.5)
        try:
            with open(REPLY_FILE, encoding="utf-8") as f:
                rep = json.load(f)
            if rep.get("id") == req_id:
                os.remove(REPLY_FILE)
                send_message(chat_id, rep.get("text", "✅ انجام شد."))
                return
        except FileNotFoundError:
            continue
        except Exception:
            continue
    send_message(chat_id, "❌ پاسخی از یوزربات نرسید؛ دوباره تلاش کن.")


def main():
    # single instance
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another botapi_poller is already running", flush=True)
        return

    offset = 0
    try:
        with open(OFFSET_FILE) as f:
            offset = int(f.read().strip() or 0)
    except Exception:
        offset = 0

    print("botapi_poller started", flush=True)
    while True:
        try:
            res = api_call("getUpdates", {"offset": offset, "timeout": 30}, timeout=35)
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                with open(OFFSET_FILE, "w") as f:
                    f.write(str(offset))
                msg = upd.get("message") or {}
                chat_id = (msg.get("chat") or {}).get("id")
                text = (msg.get("text") or "").strip()
                if not chat_id or not text:
                    continue
                # skip stale updates (older than 5 min) so a restart does not
                # spam the user with a reply for every old /start they sent
                if time.time() - int(msg.get("date") or 0) > 300:
                    continue
                if text.startswith("/start"):
                    handle_start(chat_id)
                elif text.startswith("/phone ") or text.startswith("/code ") or text.startswith("/password "):
                    forward_login(chat_id, text)
                # other messages are ignored
        except urllib.error.HTTPError as e:
            print(f"getUpdates HTTP {e.code}", flush=True)
            time.sleep(5)
        except Exception as e:
            print(f"poller error: {e}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
