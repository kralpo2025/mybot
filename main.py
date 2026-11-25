import asyncio
import base64
import fcntl
import logging
import os
import sys
import json
import time
import re
import html
import gc
import shutil
import subprocess
import psutil
import mimetypes
import urllib.parse
from datetime import datetime, timedelta
import pytz
import aiohttp
import aiofiles

from telethon import TelegramClient, events, functions
from telethon.tl.types import DocumentAttributeVideo, DocumentAttributeAudio
from telethon.errors import SessionPasswordNeededError, FloodWaitError

# ==========================================
# ساعت تهران — همیشه به وقت ایران (Asia/Tehran)
# ==========================================
TEHRAN_TZ = pytz.timezone('Asia/Tehran')

def get_tehran_now():
    """زمان فعلی به وقت ایران — تنها منبع ساعت ربات."""
    return datetime.now(TEHRAN_TZ)

# ==========================================
# تنظیمات اصلی و کلیدهای API
# ==========================================
API_ID = 27868969
API_HASH = "bdd2e8fccf95c9d7f3beeeff045f8df4"
BOT_TOKEN = "8347610940:AAGFiFzTFbprdjA6hRITKzcteOvnauyAGzc"

# کلیدهای API هرگز در کد نیستند — فقط در config.json روی هاست، داخل توکن هر مدل سفارشی (.add)

BOT_SESSION = 'bot_session'
USER_SESSION = 'user_session'
CONFIG_FILE = 'config.json'
TEMP_DIR = 'temp_downloads'

if not os.path.exists(TEMP_DIR):
    os.makedirs(TEMP_DIR)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ==========================================
# سیستم مدیریت تنظیمات با جزئیات کامل
# ==========================================
DEFAULT_CONFIG = {
    "pv_ai": False,
    "gp_ai": False,
    "symbol": False,
    "symbol_interval": 3600,
    "time": False,
    "font_id": 1,
    "type_mode": False,
    "base_name": "Kral", 
    "afk": False,
    "afk_reason": "",
    "ai_model": "", # شناسه مدل فعال — فقط از مدل‌های اضافه‌شده با .add انتخاب می‌شود
    "ai_tone": "playful",
    "kral_defense": 1,  # 1: خاموش, 2: محترمانه, 3: خشن و کوبنده
    "news_post_count": 5, # تعداد پست‌های پیش‌فرض برای بررسی اخبار
    "ai_length": 2, # 1: پاسخ کوتاه (مصرف توکن کم), 2: پاسخ بلند (مصرف توکن زیاد)
    "lang": "mix", # fa: فارسی, en: انگلیسی, mix: ترکیبی و شیک (پیش‌فرض)
    "custom_models": {}, # مدل‌های AI سفارشی: نام -> {id, name, provider, url, api_key} (کلید فقط در config.json روی هاست می‌ماند، هرگز در کد نیست)
    "custom_tools": {}, # ابزارهای AI سفارشی (Agent): نام -> {url, api_key, method, desc} — کلید فقط در config.json روی هاست می‌ماند، هرگز در کد نیست
    "antidel": False # ضدحذف و ضدویرایش پیام‌های پیوی (ذخیره در سیو مسیج)
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                for k, v in DEFAULT_CONFIG.items():
                    if k not in data: data[k] = v
                return data
        except: pass
    return DEFAULT_CONFIG.copy()

def save_config(config_data):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config_data, f, indent=4, ensure_ascii=False)
    except Exception as e: logger.error(f"Error saving config: {e}")

config = load_config()

# ==========================================
# حافظه‌ی چت AI — هر چت تاریخچه‌ی خودش را دارد (فایل جدا، سبک و محدود)
# ==========================================
MEMORY_FILE = 'ai_memory.json'
MEMORY_MAX_TURNS = 8        # حداکثر ۸ دور گفتگو (۱۶ پیام) در هر چت — برای کنترل مصرف توکن
MEMORY_MAX_CHARS = 600      # هر پیام حداکثر ۶۰۰ کاراکتر ذخیره می‌شود
MEMORY_MAX_CHATS = 50       # حداکثر ۵۰ چت

def load_memory():
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except: pass
    return {}

def save_memory(mem):
    try:
        with open(MEMORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(mem, f, ensure_ascii=False)
    except Exception as e: logger.error(f"Error saving memory: {e}")

def memory_history(chat_id):
    """تاریخچه‌ی یک چت به‌صورت لیست پیام‌های OpenAI."""
    if chat_id is None:
        return []
    mem = load_memory()
    hist = mem.get(str(chat_id), [])
    return [m for m in hist if isinstance(m, dict) and m.get("role") in ("user", "assistant") and m.get("content")]

def memory_add(chat_id, user_text, ai_text):
    """یک دور گفتگو را به حافظه‌ی آن چت اضافه می‌کند (با برش و سقف)."""
    if chat_id is None or not user_text or not ai_text:
        return
    if isinstance(ai_text, str) and ai_text.startswith("❌"):
        return  # پاسخ خطا ذخیره نمی‌شود
    mem = load_memory()
    key = str(chat_id)
    hist = mem.get(key, [])
    hist.append({"role": "user", "content": str(user_text)[:MEMORY_MAX_CHARS]})
    hist.append({"role": "assistant", "content": str(ai_text)[:MEMORY_MAX_CHARS]})
    hist = hist[-MEMORY_MAX_TURNS * 2:]
    mem[key] = hist
    # سقف تعداد چت‌ها — قدیمی‌ترین‌ها حذف می‌شوند
    while len(mem) > MEMORY_MAX_CHATS:
        mem.pop(next(iter(mem)))
    save_memory(mem)

def memory_clear(chat_id):
    if chat_id is None:
        return False
    mem = load_memory()
    if str(chat_id) in mem:
        del mem[str(chat_id)]
        save_memory(mem)
        return True
    return False

# ==========================================
# ابزارهای AI (Agent Tools) — ثبت API دلخواه با نام، برای فراخوانی توسط AI
# ابزارها فقط در config.json روی هاست ذخیره می‌شوند؛ کلید هرگز در کد یا لاگ نیست.
# ==========================================
TOOL_CALL_TIMEOUT = 30  # ثانیه
TOOL_RESULT_MAX_CHARS = 4000  # نتیجه‌ی ابزار برای مدل برش می‌خورد تا توکن منفجر نشود
# نام دستورهای داخلی — دیسپچر ابزارها هرگز روی این نام‌ها عمل نمی‌کند و
# ابزار هم نمی‌تواند با این نام‌ها ثبت شود، جز "image" که هندلر داخلی خودش
# (generate_image) ابزار image را صدا می‌زند؛ دیسپچر "image" را هم رد می‌کند.
BUILTIN_COMMANDS = frozenset({
    "tts", "ویس", "ais", "models", "restart", "ریستارت", "set", "ست",
    "sp", "in", "uptime", "اپتایم", "add", "afk", "ai", "ailen",
    "antidel", "arz", "back", "del", "fake", "font", "for", "ghost",
    "gp", "image", "info", "kral", "ping", "post", "pv", "sch",
    "setbio", "setname", "spotify", "st", "sum", "symbol", "tagall",
    "time", "tone", "tool", "tr", "ttl", "type", "خبر", "دانلود",
    "راهنما", "ریست", "فراموش", "نظر", "وضعیت",
})
# مارکرها با search پیدا می‌شوند، نه full-match: مدل گاهی قبل از مارکر یک
# جمله‌ی محاوره‌ای می‌نویسد (مثل «باشه داداش، الان برات می‌سازم! [[TOOL: ...]]»)
# و اگر مارکر فقط وقتی کل پاسخ باشد اجرا شود، مارکر خام برای کاربر فرستاده
# می‌شود. پس مارکر در هر جای پاسخ پیدا و اجرا می‌شود و از متن نهایی حذف می‌شود.
SEARCH_MARKER_RE = re.compile(r'\[\[SEARCH:\s*(.+?)\]\]', re.DOTALL)
TOOL_MARKER_RE = re.compile(r'\[\[TOOL:\s*([^|\]]+?)\s*\|\s*(.+?)\s*\]\]', re.DOTALL)

def _strip_markers(text):
    """هر مارکر [[TOOL/SEARCH/CHANNEL ...]] را از متن حذف می‌کند تا مارکر
    خام هرگز برای کاربر فرستاده نشود (دفاع عمقی — حتی اگر مدل در جواب
    نهایی هم مارکر برگرداند)."""
    if not text:
        return text
    return re.sub(r'\[\[(?:TOOL|SEARCH|CHANNEL):.+?\]\]', '', text, flags=re.DOTALL).strip()

def tool_list_prompt():
    """متن لیست ابزارها برای پرامپت سیستم — خالی اگر ابزاری ثبت نشده."""
    tools = config.get("custom_tools", {}) or {}
    if not tools:
        return ""
    lines = []
    for name, t in tools.items():
        if not isinstance(t, dict):
            continue
        desc = (t.get("desc") or "").strip()
        lines.append(f"- {name}: {desc}" if desc else f"- {name}")
    if not lines:
        return ""
    return ("🔴 ابزارها (Agent Tools): تو به ابزارهای زیر دسترسی داری. اگر برای جواب به یکی از آن‌ها نیاز داری، "
            "به‌جای حدس زدن، فقط و فقط همین را برگردان و هیچ چیز دیگری ننویس: [[TOOL: <نام ابزار> | <ورودی>]] — "
            "بعد از اجرای ابزار، جواب نهایی را با نتیجه‌اش می‌دهی.\n" + "\n".join(lines))

# نام‌های رایج پارامتر ورودی در APIهای GET — اگر آدرس ابزار از قبل چنین
# پارامتری داشته باشد (مثلاً ?prompt=... از مثال curl کپی شده)، مقدارش با
# ورودی جدید جایگزین می‌شود تا API همان مثال قدیمی را اجرا نکند.
_GET_INPUT_PARAMS = {"prompt", "q", "query", "text", "input", "keyword",
                     "search", "term", "msg", "message", "word"}

def _get_request_url(url, tool_input):
    """آدرس GET ابزار را می‌سازد — برمی‌گرداند (url, params).
    اگر آدرس از قبل یک پارامتر ورودی (مثل prompt) داشته باشد، مقدار همان
    پارامتر با ورودی جدید جایگزین می‌شود و بقیه‌ی پارامترها دست نمی‌خورند؛
    اگر چنین پارامتری نباشد، مثل قبل ?q=<input> اضافه می‌شود."""
    try:
        parsed = urllib.parse.urlsplit(url)
        qs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    except Exception:
        return url, {"q": tool_input}
    replaced = False
    new_qs = []
    for k, v in qs:
        if not replaced and k.lower() in _GET_INPUT_PARAMS:
            new_qs.append((k, tool_input))
            replaced = True
        else:
            new_qs.append((k, v))
    if replaced:
        new_url = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path,
             urllib.parse.urlencode(new_qs), parsed.fragment))
        return new_url, None
    return url, {"q": tool_input}

async def call_tool(name, tool_input):
    """یک ابزار ثبت‌شده را صدا می‌زند؛ برمی‌گرداند (متن نتیجه یا None, خطای کوتاه یا None).
    GET با ?q=<input>، POST با JSON {"input": ...}. کلید هرگز لاگ نمی‌شود."""
    tools = config.get("custom_tools", {}) or {}
    tool = tools.get(name)
    if not isinstance(tool, dict):
        return None, f"ابزار «{name}» ثبت نشده است."
    url = (tool.get("url") or "").strip()
    if not url:
        return None, f"ابزار «{name}» آدرس ندارد."
    method = (tool.get("method") or "POST").upper()
    if method not in ("GET", "POST"):
        method = "POST"
    api_key = tool.get("api_key") or ""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with aiohttp.ClientSession() as session:
            if method == "GET":
                req_url, req_params = _get_request_url(url, tool_input)
                async with session.get(req_url, headers=headers, params=req_params, timeout=TOOL_CALL_TIMEOUT) as resp:
                    body = await resp.text()
                    if resp.status == 200:
                        return body[:TOOL_RESULT_MAX_CHARS], None
                    return None, f"ابزار «{name}» خطا داد (HTTP {resp.status})."
            else:
                async with session.post(url, headers=headers, json={"input": tool_input}, timeout=TOOL_CALL_TIMEOUT) as resp:
                    body = await resp.text()
                    if resp.status == 200:
                        return body[:TOOL_RESULT_MAX_CHARS], None
                    return None, f"ابزار «{name}» خطا داد (HTTP {resp.status})."
    except asyncio.TimeoutError:
        return None, f"ابزار «{name}» پاسخ نداد (تایم‌اوت)."
    except Exception as e:
        return None, f"ابزار «{name}» اجرا نشد: {type(e).__name__}."

def extract_image_url(text):
    """از خروجی یک ابزار، آدرس تصویر را درمی‌آورد (URL مستقیم یا JSON با فیلد url).
    اگر چیزی پیدا نشد None برمی‌گرداند."""
    if not text:
        return None
    s = text.strip()
    if s.startswith("http://") or s.startswith("https://"):
        return s.split()[0]
    try:
        data = json.loads(s)
        if isinstance(data, dict):
            for key in ("url", "image_url", "image", "result", "data"):
                v = data.get(key)
                if isinstance(v, str) and (v.startswith("http://") or v.startswith("https://")):
                    return v
                if isinstance(v, dict):
                    u = v.get("url")
                    if isinstance(u, str) and (u.startswith("http://") or u.startswith("https://")):
                        return u
                if isinstance(v, list) and v:
                    first = v[0]
                    if isinstance(first, dict):
                        u = first.get("url")
                        if isinstance(u, str) and (u.startswith("http://") or u.startswith("https://")):
                            return u
    except Exception:
        pass
    m = re.search(r'https?://\S+\.(?:png|jpg|jpeg|webp|gif)(\?\S*)?', s)
    return m.group(0) if m else None

# ==========================================
# سیستم مترجم رابط کاربری (UI Text Manager)
# ==========================================
def _t(fa_text, en_text, mix_text):
    """انتخاب متن بر اساس زبان تنظیم شده در کانفیگ"""
    lang = config.get("lang", "mix")
    if lang == "en": return en_text
    if lang == "fa": return fa_text
    return mix_text

bot = TelegramClient(BOT_SESSION, API_ID, API_HASH)
user_client = TelegramClient(USER_SESSION, API_ID, API_HASH)
login_state = {}

START_TIME = time.time()

# ==========================================
# خواندن کانال/گروه برای AI ایجنت — فقط از مسیر دستور .ai
# گاردریل سفت: خواندن کانال فقط وقتی اتفاق می‌افتد که کاربر خودش در همان
# پیام دستور .ai را فرستاده باشد (ask_ai با allow_channel=True). منشی
# (.pv/.gp)، حلقه‌ی پس‌زمینه و هیچ مسیر دیگری هرگز کانال نمی‌خواند.
# ==========================================
CHANNEL_MARKER_RE = re.compile(r'\[\[CHANNEL:\s*([^|\]]+?)\s*(?:\|\s*(\d+)\s*)?\]\]', re.DOTALL)
CHANNEL_FETCH_MAX_MESSAGES = 20   # سقف تعداد پیام در هر بار خواندن
CHANNEL_FETCH_MAX_CHARS = 6000    # سقف کاراکتر پیام‌های برگشتی به مدل
CHANNEL_MSG_MAX_CHARS = 800       # سقف کاراکتر هر پیام

async def fetch_channel_messages(target, limit=10):
    """آخرین پیام‌های یک کانال/گروه را با Telethon می‌خواند (کانال عمومی یا
    گروهی که اکانت در آن است). فقط از داخل ask_ai و فقط وقتی
    allow_channel=True صدا زده می‌شود — یعنی فقط در جریان دستور .ai.
    برمی‌گرداند (متن پیام‌ها یا None, خطای کوتاه یا None)."""
    try:
        limit = int(limit or 10)
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, CHANNEL_FETCH_MAX_MESSAGES))
    try:
        entity = await user_client.get_entity(target)
    except Exception:
        return None, _t(f"کانال/گروه «{target}» پیدا نشد یا در دسترس نیست.",
                        f"Channel/group «{target}» was not found or is not accessible.",
                        f"کانال/گروه پیدا نشد | Not found: «{target}»")
    try:
        msgs = await user_client.get_messages(entity, limit=limit)
    except Exception as e:
        logger.error(f"channel fetch error: {e}")
        return None, _t(f"خواندن پیام‌های «{target}» ناموفق بود.",
                        f"Could not read messages from «{target}».",
                        f"خواندن پیام‌ها ناموفق بود | Read failed: «{target}»")
    lines = []
    for m in msgs or []:
        text = (getattr(m, "text", None) or "").strip()
        if not text:
            continue
        if len(text) > CHANNEL_MSG_MAX_CHARS:
            text = text[:CHANNEL_MSG_MAX_CHARS] + "…"
        lines.append(text)
    if not lines:
        return None, _t(f"در «{target}» پیام متنی قابل‌خواندنی پیدا نشد.",
                        f"No readable text messages were found in «{target}».",
                        f"پیام متنی پیدا نشد | No text messages: «{target}»")
    out = "\n\n---\n\n".join(lines)
    return out[:CHANNEL_FETCH_MAX_CHARS], None

# ==========================================
# ماژول هوش مصنوعی (طبیعی، کوتاه‌نویسی، کنترل توکن و دفاع جانانه)
# ==========================================
async def ask_ai(prompt: str, short_answer=False, is_chat=True, chat_id=None, allow_channel=False) -> str:
    """
    is_chat = True -> در حالت چت طبیعی با کاربره (استفاده از لحن و دفاع کرال).
    is_chat = False -> در حالت انجام دستوره (مثل خبر، خلاصه ساز، ترجمه) که نباید هیچ متن اضافه‌ای بده و مستقل از تنظیم ailen است.
    allow_channel = True -> فقط در جریان دستور .ai که کاربر خودش فرستاده؛
    اجازه می‌دهد AI با مارکر [[CHANNEL: ...]] کانال/گروه بخواند. در همه‌ی
    مسیرهای دیگر (منشی .pv/.gp، .نظر و...) False می‌ماند و خواندن کانال
    هرگز انجام نمی‌شود.
    """
    model_id = config.get("ai_model", "") or ""
    # فقط مدل‌های اضافه‌شده با .add — هیچ مدل داخلی وجود ندارد
    custom_models = config.get("custom_models", {}) or {}
    model_info = None
    for _name, _m in custom_models.items():
        if isinstance(_m, dict) and _m.get("id") == model_id:
            model_info = _m
            break
    if model_info is None:
        if custom_models:
            # مدل فعال پیدا نشد — اولین مدل اضافه‌شده را فعال کن
            _first = next(iter(custom_models.values()))
            model_info = _first
            config["ai_model"] = _first.get("id", "")
            save_config(config)
        else:
            return _t("❌ هنوز هیچ مدلی اضافه نشده است.\nبا `.add` یک مدل اضافه کن.", "❌ No model has been added yet.\nAdd one with `.add`.", "❌ هیچ مدلی اضافه نشده | No model added\n⊳ با `.add` یک مدل اضافه کن.")
    # کلید فقط از توکن ذخیره‌شده‌ی همان مدل در config.json خوانده می‌شود، هرگز از کد نیست
    api_key = model_info.get("api_key") or ""
    url = model_info.get("url") or ""
    # انعطاف در آدرس: فقط وقتی آدرس، «پایه‌ی خالی» یک API سازگار با OpenAI است
    # (مسیر خالی یا فقط نسخه، مثل /v1) مسیر /chat/completions اضافه می‌شود؛
    # در غیر این صورت آدرس دقیقاً همان‌طور که کاربر وارد کرده استفاده می‌شود،
    # تا API سایت‌های دیگر با مسیر متفاوت خراب نشود.
    if url:
        _path = urllib.parse.urlsplit(url).path.rstrip('/')
        if _path in ("", "/v1", "/v1beta", "/v2", "/api", "/api/v1"):
            url = url.rstrip('/') + "/chat/completions"
    if not api_key:
        return _t("❌ کلید API این مدل تنظیم نشده است. با `.add` دوباره اضافه‌اش کن.", "❌ API key for this model is not set. Re-add it with `.add`.", "❌ کلید API این مدل تنظیم نشده | API key missing — با `.add` دوباره اضافه کن.")
    if not url:
        return _t("❌ آدرس این مدل تنظیم نشده است. با `.add` دوباره اضافه‌اش کن.", "❌ URL for this model is not set. Re-add it with `.add`.", "❌ آدرس این مدل تنظیم نشده | URL missing — با `.add` دوباره اضافه کن.")
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    if is_chat:
        # مدیریت لحن ربات (پشتیبانی از ۳ حالت: شیطون، رسمی و معمولی)
        if config.get("ai_tone") == "playful":
            tone_prompt = "تو یک دوست بسیار صمیمی، شر و شیطون، حاضر جواب و باحال هستی. مثل یک آدم خودمونی و کوچه بازاری حرف بزن. زیاد از ایموجی استفاده کن. هرگز نگو تو هوش مصنوعی هستی."
        elif config.get("ai_tone") == "serious":
            tone_prompt = "تو یک دستیار بسیار رسمی، سرسنگین، محترمانه، پخته و جدی هستی. باوقار صحبت کن. از ایموجی کمتر استفاده کن. هرگز نگو تو هوش مصنوعی هستی."
        else:
            tone_prompt = "تو یک دستیار هوشمند هستی. با لحنی کاملاً معمولی، طبیعی، مودبانه و روزمره صحبت کن. نه خیلی خشک و رسمی باش، نه خیلی شوخ و شیطون. هرگز نگو تو هوش مصنوعی هستی."

        length_prompt = ""
        if short_answer:
            # منشی باید مثل یک آدم واقعی پیام بدهد، نه مثل ربات: کوتاه، خودمونی، محاوره‌ای
            length_prompt = ("🔴 قانون مهم چت: مثل یک آدم واقعی که دارد پیامک می‌دهد جواب بده — به شدت کوتاه، طبیعی و نهایتاً در ۱ الی ۲ خط. "
                "اصلاً طولانی ننویس. لحن محاوره‌ای و خودمونی داشته باش، مثل حرف زدن روزمره؛ جمله‌های رسمی و کتابی، لیست و بولت، و عبارت‌های ربات‌گونه (مثل «به عنوان یک هوش مصنوعی» یا «چطور می‌توانم کمک کنم؟») ممنوع است. "
                "ایموجی را فقط گاهی و مثل یک آدم واقعی بگذار، نه در هر پیام. لحن طرف مقابل را بگیر: اگر خودمونی حرف زد خودمونی جواب بده، اگر جدی بود جدی باش.")


        defense_prompt = ""
        kral_mode = config.get("kral_defense", 1)
        if kral_mode == 2:
            defense_prompt = "🔴 قانون خط قرمز: صاحب تو 'کرال' (Kral) است. اگر کاربر به کرال توهین کرد، بسیار محترمانه اما قاطعانه، سرسنگین و دندان‌شکن از او دفاع کن و جوابش را بده."
        elif kral_mode == 3:
            defense_prompt = "🔴 قانون خط قرمز: صاحب تو 'کرال' (Kral) است. اگر کاربر کوچکترین بی‌احترامی، تیکه یا توهینی به 'کرال' کرد، وحشی شو و با شدیدترین لحن، تیکه دار، کوبنده و فحشی از او دفاع کن! به کاربر رحم نکن!"
        
        search_prompt = "🔴 جستجوی زنده: اگر برای جواب به اطلاعات به‌روز و زنده (اخبار، قیمت، وضعیت فعلی و...) نیاز داری، به‌جای حدس زدن یا دادن اطلاعات قدیمی، فقط و فقط همین را برگردان و هیچ چیز دیگری ننویس: [[SEARCH: <عبارت جستجو>]] — بعد از جستجو، جواب نهایی را با نتایج زنده می‌دهی."
        # خواندن کانال/گروه — فقط در جریان دستور .ai به مدل گفته می‌شود؛
        # در مسیر منشی (.pv/.gp) مدل اصلاً از این قابلیت خبر ندارد.
        channel_prompt = ""
        if allow_channel:
            channel_prompt = ("🔴 خواندن کانال/گروه: اگر کاربر صریحاً از تو خواست یک کانال یا گروه تلگرامی را چک کنی و درباره‌ی پیام‌های اخیرش بگویی، "
                "به‌جای حدس زدن، فقط و فقط همین را برگردان و هیچ چیز دیگری ننویس: [[CHANNEL: <نام کانال یا گروه> | <تعداد پیام، پیش‌فرض ۱۰>]] — "
                "بعد از خواندن پیام‌ها، جواب نهایی را با خلاصه‌ی آن‌ها می‌دهی.")
        agent_rule_prompt = ("🔴 قانون ایجنت: تو فقط وقتی کاری انجام می‌دهی که کاربر در همین پیام صریحاً از تو خواسته باشد. "
            "هرگز خودت کاری را شروع نمی‌کنی، هرگز چیزی را خودسر چک نمی‌کنی، هرگز به کسی پیام نمی‌دهی و هرگز کاری را در پس‌زمینه انجام نمی‌دهی.")
        file_prompt = "🔴 ارسال فایل: اگر کاربر از تو خواست کد یا فایلی بنویسی (مثلاً صفحه HTML، اسکریپت پایتون، جاوااسکریپت و...)، کد را فقط در قالب ONE بلوک کد محصور (```زبان ... ```) با برچسب زبان بنویس، چون ربات آن را به‌صورت فایل ارسال می‌کند. توضیح بیرون از بلوک کد کوتاه باشد."
        system_prompt = f"{tone_prompt}\n\n{length_prompt}\n\n{defense_prompt}\n\n{search_prompt}\n\n{channel_prompt}\n\n{agent_rule_prompt}\n\n{file_prompt}".strip()
        
        # در حالت چت، سقف توکن‌ها وابسته به تنظیم ailen کاربر است
        ai_length_mode = config.get("ai_length", 2)
        base_max_tokens = 4096 if ai_length_mode == 2 else 800
    else:
        # ربات در حالت ابزار (بدون شخصیت و بدون دفاع) - برای خبر و ...
        system_prompt = "تو یک دستیار پردازش متن هوشمند هستی. دستور کاربر را دقیقا و مستقیما انجام بده. به هیچ وجه سلام نکن، خداحافظی نکن، ایموجی متفرقه نذار، اظهار نظر شخصی نکن و هیچ متن اضافه‌ای جز خروجی اصلی تولید نکن."
        
        # ابزارهایی مثل خبر مستقل از تنظیم ailen کاربر هستند و همیشه از سقف مجاز بالا استفاده می‌کنند
        base_max_tokens = 4096

    # حافظه‌ی چت: تاریخچه‌ی همین چت بین پیام system و پیام فعلی قرار می‌گیرد
    # (فقط در حالت چت؛ ابزارها حافظه ندارند)
    _history = memory_history(chat_id) if (is_chat and chat_id is not None) else []
    base_msgs = [{"role": "system", "content": system_prompt}] + _history

    async def _chat_call(messages):
        """یک فراخوانی به مدل — برمی‌گرداند (متن یا None, خطا یا None)"""
        payload = {
            "model": model_info["id"],
            "messages": messages,
            "temperature": 0.8 if is_chat else 0.5,
            "max_tokens": 300 if short_answer else base_max_tokens
        }
        async with aiohttp.ClientSession() as session:
            # زمان تایم‌اوت به ۱۲۰ ثانیه افزایش یافت تا قطعی رخ ندهد
            async with session.post(url, headers=headers, json=payload, timeout=120) as response:
                if response.status == 200:
                    data = await response.json()
                    res_text = data['choices'][0]['message']['content']
                    res_text = re.sub(r'<think>.*?</think>', '', res_text, flags=re.DOTALL).strip()
                    return res_text, None
                err = await response.text()
                return None, _t(f"❌ خطای سرور ({response.status}):\n{err}", f"❌ Server Error ({response.status}):\n{err}", f"❌ خطای سرور | Server Error ({response.status}):\n{err}")

    async def _continue_unclosed_code(text):
        """ادامه‌ی خودکار کد ناتمام: اگر پاسخ با بلوک کد بازِ بسته‌نشده تمام شد
        (مدل به سقف توکن رسید و کد نصف ماند)، با فراخوانی‌های «ادامه» کد را کامل
        می‌کند؛ حداکثر ۳ ادامه (روی هم تا ~۴ برابر سقف یک فراخوانی)."""
        cur = text or ""
        for _ in range(3):
            if cur.count("```") % 2 == 0:
                break
            last = cur.rfind("```")
            if len(cur) - last < 300:
                break
            cont, err = await _chat_call(base_msgs + [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": cur},
                {"role": "user", "content": "ادامه بده — دقیقاً از همان‌جایی که قطع شدی ادامه بده؛ فقط ادامه‌ی کد را بنویس، بدون تکرار کد قبلی و بدون هیچ توضیح اضافه. در پایان بلوک کد را با ``` ببند."},
            ])
            if err or not cont:
                break
            cont = re.sub(r'^\s*```\w*\n', '', cont.strip(), count=1)
            cur = cur.rstrip() + "\n" + cont
        return cur

    try:
        res_text, err = await _chat_call(base_msgs + [
            {"role": "user", "content": prompt}
        ])
        if err:
            return err
        # جستجوی زنده — فقط در حالت چت، حداکثر یک دور جستجو در هر پیام
        if is_chat:
            m = SEARCH_MARKER_RE.search(res_text or "")
            if m:
                search_results = await live_search(m.group(1).strip())
                res2, err2 = await _chat_call(base_msgs + [
                    {"role": "user", "content": f"{prompt}\n\n━━━━━━━━━━━━━━━\nنتایج جستجوی زنده در وب:\n{search_results}\n━━━━━━━━━━━━━━━\nحالا با استفاده از نتایج جستجو، جواب نهایی و به‌روز را بده."}
                ])
                if err2:
                    return err2
                res2 = _strip_markers(await _continue_unclosed_code(res2))
                if not res2:
                    return _t("❌ جستجو نتیجه‌ی قابل استفاده‌ای نداد.", "❌ The search did not yield a usable answer.", "❌ جستجو نتیجه‌ای نداد | Search yielded nothing usable")
                return res2
            # خواندن کانال/گروه — فقط در جریان دستور .ai (allow_channel)، یک دور
            cm = CHANNEL_MARKER_RE.search(res_text or "")
            if cm:
                if not allow_channel:
                    return _t("❌ خواندن کانال فقط با دستور `.ai` ممکن است.",
                               "❌ Reading a channel is only possible with the `.ai` command.",
                               "❌ خواندن کانال فقط با `.ai` | Channel reading only via `.ai`")
                chan_target = cm.group(1).strip()
                chan_limit = int(cm.group(2)) if cm.group(2) else 10
                chan_text, chan_err = await fetch_channel_messages(chan_target, chan_limit)
                if chan_err:
                    chan_block = f"خواندن کانال «{chan_target}» ناموفق بود: {chan_err}"
                else:
                    chan_block = chan_text
                res2, err2 = await _chat_call(base_msgs + [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": res_text},
                    {"role": "user", "content": f"━━━━━━━━━━━━━━━\nآخرین پیام‌های کانال/گروه «{chan_target}»:\n{chan_block}\n━━━━━━━━━━━━━━━\nحالا با استفاده از این پیام‌ها، جواب نهایی را بده — خلاصه کن و به کاربر بگو."}
                ])
                if err2:
                    return err2
                res2 = _strip_markers(await _continue_unclosed_code(res2))
                if not res2:
                    return _t("❌ خواندن کانال نتیجه‌ی قابل استفاده‌ای نداد.",
                               "❌ Reading the channel did not yield a usable answer.",
                               "❌ خواندن کانال نتیجه‌ای نداد | Channel read yielded nothing usable")
                return res2
        if is_chat:
            return _strip_markers(await _continue_unclosed_code(res_text))
        return res_text
    except Exception as e:
        return _t(f"❌ خطا در پردازش هوش مصنوعی:\n{e}", f"❌ AI Processing Error:\n{e}", f"❌ خطا در هوش مصنوعی | AI Error:\n{e}")

# ==========================================
# بینایی — دیدن عکس ریپلای‌شده با مدل بینایی (Vision)
# مدل متنی کاربر (mercury-3) بینایی ندارد، پس عکس از این مسیر جدا می‌رود
# و هرگز از ask_ai رد نمی‌شود. بک‌اند: Google Gemini API
# (generativelanguage.googleapis.com) با مدل gemini-flash-latest — رایگان
# با کلید Google AI Studio. بک‌اند قبلی (HuggingFace Inference) کنار گذاشته
# شد: Gemma به PRO یا pre-paid credits نیاز داشت و Qwen2.5-VL-7B را هیچ
# پرووایدرى سرو نمی‌کرد (تأییدشده 2026-11-19). کلید هرگز در کد نیست؛
# فقط config["gemini_api_key"] یا env GEMINI_API_KEY.
# (Pollinations رایگان بینایی ندارد: مدل Classic آن فقط متن است.)
# ==========================================
# دیدن عکس از طریق پراکسی Cloudflare (gemini-vision-proxy) می‌رود، چون گوگل
# آی‌پی دیتاسنتر هاست (Alwaysdata) را بلاک می‌کند؛ کلید همان gemini_api_key است.
VISION_API_URL = "https://gemini.kral.cc.cd/"
VISION_MODEL = "gemini-flash-latest"
VISION_MAX_TOKENS = 1000
VISION_TIMEOUT = 120

async def ask_vision(question: str, image_path: str) -> str:
    """عکس را با مدل بینایی تحلیل می‌کند؛ سؤال کاربر + توصیف عکس.
    اگر سؤالی نباشد، عکس توصیف می‌شود و متن داخلش خوانده/ترجمه می‌شود."""
    api_key = (config.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return _t("❌ برای دیدن عکس به کلید Google AI Studio نیاز است.\nیک کلید رایگان بساز: aistudio.google.com — بعد آن را در .env هاست به‌صورت GEMINI_API_KEY=... بگذار و ربات را ری‌استارت کن.",
                  "❌ A Google AI Studio key is needed to see photos.\nCreate a free one at aistudio.google.com — then set GEMINI_API_KEY=... in the host .env and restart the bot.",
                  "❌ کلید Google لازم است | Google key needed\n⊳ رایگان بساز: aistudio.google.com — در .env بگذار GEMINI_API_KEY=...")
    try:
        with open(image_path, "rb") as f:
            raw = f.read()
        b64 = base64.b64encode(raw).decode()
    except Exception:
        return _t("❌ خواندن فایل عکس ناموفق بود.", "❌ Could not read the photo file.", "❌ خواندن عکس ناموفق بود | Could not read photo")
    # mime را از بایت‌های واقعی فایل تشخیص بده (نه پسوند؛ تلگرام گاهی پسوند نمی‌دهد)
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    elif raw[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        ext = os.path.splitext(image_path)[1].lower()
        mime = "image/png" if ext == ".png" else ("image/webp" if ext == ".webp" else "image/jpeg")
    q = (question or "").strip()
    if not q:
        q = _t("این عکس را کامل توصیف کن: چه چیزی در آن است؟ اگر متنی در عکس هست، آن را بخوان و به فارسی ترجمه‌اش کن.",
               "Describe this photo in full: what is in it? If there is text in the photo, read it and translate it.",
               "این عکس را توصیف کن | Describe this photo — متن داخلش را هم بخوان و ترجمه کن")
    payload = {
        "contents": [{"parts": [
            {"text": q},
            {"inline_data": {"mime_type": mime, "data": b64}},
        ]}],
        "generationConfig": {"maxOutputTokens": VISION_MAX_TOKENS},
    }
    headers = {"X-goog-api-key": api_key, "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(VISION_API_URL, headers=headers, json=payload, timeout=VISION_TIMEOUT) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    try:
                        parts = data["candidates"][0]["content"].get("parts", [])
                        content = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
                    except Exception:
                        content = ""
                    if content:
                        return content
                    return _t("❌ مدل چیزی برنگرداند.", "❌ The model returned nothing.", "❌ مدل چیزی برنگرداند | Model returned nothing")
                if resp.status == 503:
                    return _t("❌ مدل بینایی الان شلوغ است (overload)؛ چند دقیقه‌ی دیگر دوباره امتحان کن.", "❌ The vision model is overloaded right now; try again in a few minutes.", "❌ مدل شلوغ است | Model overloaded — چند دقیقه‌ی دیگر دوباره امتحان کن")
                err_body = ""
                try:
                    err_body = (await resp.text())[:400]
                except Exception:
                    pass
                logger.warning(f"vision HTTP {resp.status}: {err_body}")
                # فقط 401/403 یا پیام صریح «کلید نامعتبر» یعنی کلید خراب است؛
                # 400 معمولاً یعنی درخواست مشکل دارد، نه کلید — پیام واقعی گوگل را نشان بده
                key_bad = resp.status in (401, 403) or "API_KEY_INVALID" in err_body or "API key not valid" in err_body or "API_KEY_EXPIRED" in err_body
                if key_bad:
                    return _t("❌ کلید Google معتبر نیست. GEMINI_API_KEY را در .env چک کن.", "❌ Google key is invalid. Check GEMINI_API_KEY in .env.", "❌ کلید معتبر نیست | Invalid key — GEMINI_API_KEY را چک کن")
                if resp.status == 400:
                    try:
                        gmsg = json.loads(err_body).get("error", {}).get("message", "")
                    except Exception:
                        gmsg = err_body
                    gmsg = (gmsg or "HTTP 400")[:200]
                    return _t(f"❌ گوگل درخواست را نپذیرفت: {gmsg}", f"❌ Google rejected the request: {gmsg}", f"❌ گوگل درخواست را نپذیرفت | Google rejected: {gmsg}")
                return _t(f"❌ خطای سرویس بینایی (HTTP {resp.status}).", f"❌ Vision service error (HTTP {resp.status}).", f"❌ خطای سرویس بینایی | Vision error (HTTP {resp.status})")
    except asyncio.TimeoutError:
        return _t("❌ سرویس بینایی پاسخ نداد (تایم‌اوت).", "❌ Vision service timed out.", "❌ تایم‌اوت | Vision timed out")
    except Exception as e:
        return _t(f"❌ خطا در سرویس بینایی: {type(e).__name__}", f"❌ Vision service error: {type(e).__name__}", f"❌ خطا در بینایی | Vision error: {type(e).__name__}")

async def send_type_mode(event, text, is_reply=False):
    """
    تابع ارسال پیام هوشمند با قابلیت حل مشکل محدودیت طول پیام تلگرام (Message too long)
    اگر متن بیشتر از 4000 کاراکتر باشد، به صورت خودکار چند تکه شده و روی هم ریپلای می‌شوند.
    """
    MAX_LEN = 4000
    # تقسیم متن به تکه‌های 4000 تایی
    parts = [text[i:i+MAX_LEN] for i in range(0, len(text), MAX_LEN)]
    
    if not config.get("type_mode"):
        current_msg = None
        for idx, part in enumerate(parts):
            if idx == 0:
                current_msg = await event.reply(part) if is_reply else await event.edit(part)
            else:
                # پیام‌های طولانی و اضافی روی پیام قبلی ریپلای می‌شوند تا گم نشوند
                current_msg = await current_msg.reply(part)
        return

    # حالت افکت تایپ روشن
    current_msg = None
    chunk_size = 35
    for idx, part in enumerate(parts):
        if idx == 0:
            current_msg = await event.reply("...") if is_reply else await event.edit("...")
        else:
            current_msg = await current_msg.reply("...")
            
        for i in range(chunk_size, len(part) + chunk_size, chunk_size):
            try:
                await current_msg.edit(part[:i] + " █")
                await asyncio.sleep(0.8)
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds)
        await current_msg.edit(part)

# ==========================================
# ارسال پاسخ AI — بلوک کد بزرگ به‌صورت فایل فرستاده می‌شود
# ==========================================
_CODE_EXT = {
    "html": "index.html", "htm": "index.html",
    "python": "script.py", "py": "script.py",
    "js": "script.js", "javascript": "script.js",
    "css": "style.css", "json": "data.json",
    "sh": "script.sh", "bash": "script.sh",
    "sql": "query.sql", "xml": "data.xml",
    "md": "readme.md", "markdown": "readme.md",
    "c": "main.c", "cpp": "main.cpp", "java": "Main.java",
    "php": "index.php", "go": "main.go", "rs": "main.rs",
}

async def send_ai_response(event, msg, res):
    """پاسخ AI را می‌فرستد؛ اگر بلوک کد بزرگی (بیش از ~۳۰۰ کاراکتر) داشته باشد،
    بزرگ‌ترین بلوک را به‌صورت فایل با پسوند درست می‌فرستد و بقیه‌ی متن را جداگانه.
    بلوک‌های کوتاه داخل متن باقی می‌مانند.
    بلوک بازِ بسته‌نشده در انتهای متن (پاسخ ناتمام مدل) هم به‌عنوان کد گرفته می‌شود.
    اگر پاسخ شامل آدرس تصویر باشد (مثلاً خروجی ابزار ساخت عکس)، عکس به‌صورت
    عکس فرستاده می‌شود و لینک خام هرگز به کاربر نمی‌رسد."""
    text = res or ""
    cands = []  # (start, end, lang, code)
    for m in re.finditer(r'```(\w*)\n(.*?)```', text, flags=re.DOTALL):
        cands.append((m.start(), m.end(), (m.group(1) or "").strip().lower(), m.group(2).strip('\n')))
    # بلوک بازِ بسته‌نشده: تعداد fenceها فرد است — آخرین fence، شروع کد ناتمام است
    fences = [m.start() for m in re.finditer(r'```', text)]
    if len(fences) % 2 == 1:
        last = fences[-1]
        m2 = re.match(r'(\w*)\n(.*)$', text[last + 3:], flags=re.DOTALL)
        if m2:
            cands.append((last, len(text), (m2.group(1) or "").strip().lower(), m2.group(2).strip('\n')))
    big = None  # (start, end, lang, code)
    for c in cands:
        if len(c[3]) > 300 and (big is None or len(c[3]) > len(big[3])):
            big = c
    img_url = extract_image_url(text)
    if big is None:
        if img_url:
            # عکس به‌جای لینک: متن بدون آدرس تصویر، به‌عنوان کپشن/پیام
            body = re.sub(r'\n{3,}', '\n\n', text.replace(img_url, "").strip())
            try:
                reply_to = getattr(msg, 'id', None)
                if body and len(body) <= 1000:
                    await user_client.send_file(event.chat_id, img_url, caption=body, reply_to=reply_to)
                    try:
                        await msg.delete()
                    except Exception:
                        pass
                else:
                    if body:
                        await send_type_mode(msg, body)
                    else:
                        try:
                            await msg.delete()
                        except Exception:
                            pass
                    await user_client.send_file(event.chat_id, img_url, reply_to=reply_to)
                return
            except Exception as e:
                logger.error(f"send_ai_response image error: {e}")
                # برگشت به رفتار قبلی: متن (با لینک) فرستاده می‌شود
        await send_type_mode(msg, text)
        return
    start, end, lang, code = big
    filename = _CODE_EXT.get(lang, "code.txt")
    remaining = (text[:start] + text[end:]).strip()
    if img_url:
        remaining = re.sub(r'\n{3,}', '\n\n', remaining.replace(img_url, "").strip())
    note = _t("📎 فایل ساخته و ارسال شد.", "📎 File created and sent.", "📎 فایل ساخته شد | File created & sent")
    body = (remaining + f"\n\n{note}") if remaining else note
    path = os.path.join(TEMP_DIR, filename)
    try:
        async with aiofiles.open(path, 'w', encoding='utf-8') as f:
            await f.write(code)
        await send_type_mode(msg, body)
        reply_to = getattr(msg, 'id', None)
        await user_client.send_file(event.chat_id, path, caption=f"📄 `{filename}`", reply_to=reply_to)
        if img_url:
            # پاسخ علاوه بر کد، آدرس تصویر هم دارد — عکس هم فرستاده شود
            try:
                await user_client.send_file(event.chat_id, img_url, reply_to=reply_to)
            except Exception as e:
                logger.error(f"send_ai_response image error: {e}")
    except Exception as e:
        logger.error(f"send_ai_response file error: {e}")
        await send_type_mode(msg, text)
    finally:
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

# ==========================================
# جستجوی زنده وب — مستقیم از هاست، بدون هیچ API واسط
# ==========================================
async def live_search(query: str) -> str:
    """جستجوی زنده در وب با DuckDuckGo HTML — بدون هیچ کلید API.
    در صورت خطا، رشته‌ی خطای کوتاه برمی‌گرداند و هرگز raise نمی‌کند."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fa,en-US;q=0.9,en;q=0.8",
    }
    urls = [
        f"https://html.duckduckgo.com/html/?q={urllib.parse.quote_plus(query)}",
        f"https://lite.duckduckgo.com/lite/?q={urllib.parse.quote_plus(query)}",
    ]
    page_html = None
    for u in urls:
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(u, timeout=30) as resp:
                    if resp.status == 200:
                        html_text = await resp.text()
                        # صفحه‌ی «anomaly» داک‌داک‌گو ۲۰۰ برمی‌گرداند اما نتیجه‌ای ندارد؛
                        # در این صورت سراغ اندپوینت بعدی می‌رویم
                        if 'result__a' in html_text or 'result-link' in html_text:
                            page_html = html_text
                            break
                        page_html = html_text
        except Exception:
            continue
    if not page_html:
        return _t("❌ جستجوی زنده در دسترس نیست.", "❌ Live search is unavailable.", "❌ جستجوی زنده در دسترس نیست | Live search unavailable")

    def _clean(s):
        return html.unescape(re.sub(r'<[^>]+>', '', s or "")).strip()

    def _real_link(link):
        # لینک‌های duckduckgo به صورت //duckduckgo.com/l/?uddg=... هستند — آدرس واقعی را درمی‌آوریم
        if 'uddg=' in link:
            try:
                real = urllib.parse.unquote(urllib.parse.parse_qs(urllib.parse.urlsplit(link).query).get('uddg', [''])[0])
                if real:
                    link = real
            except Exception:
                pass
        if link.startswith('//'):
            link = 'https:' + link
        return link

    results = []  # (title, link, snippet)
    for m in re.finditer(r"""<a\b[^>]*class=["'][^"']*(?:result__a|result-link)[^"']*["'][^>]*>""", page_html, flags=re.IGNORECASE):
        tag = m.group(0)
        hm = re.search(r"""href=["']([^"']+)["']""", tag)
        if not hm:
            continue
        link = _real_link(hm.group(1))
        tail = page_html[m.end():m.end() + 4000]
        tm = re.match(r'(.*?)</a>', tail, flags=re.DOTALL)
        title = _clean(tm.group(1)) if tm else ""
        if not title or not link or link.startswith('https://duckduckgo.com'):
            continue
        snippet = ""
        sm = re.search(r"""class=["'][^"']*result__snippet[^"']*["'][^>]*>(.*?)</a>""", tail, flags=re.DOTALL | re.IGNORECASE)
        if sm:
            snippet = _clean(sm.group(1))
        else:
            sm = re.search(r'result-snippet[^>]*>(.*?)</td>', tail, flags=re.DOTALL | re.IGNORECASE)
            if sm:
                snippet = _clean(sm.group(1))
        results.append((title, link, snippet))
        if len(results) >= 5:
            break

    if not results:
        return _t("❌ برای این عبارت نتیجه‌ای پیدا نشد.", "❌ No results found for this query.", "❌ نتیجه‌ای پیدا نشد | No results found")

    out = _t("🌐 **نتایج جستجوی زنده:**\n━━━━━━━━━━━━━━━\n", "🌐 **Live Search Results:**\n━━━━━━━━━━━━━━━\n", "🌐 ❲ نتایج جستجوی زنده | Live Results ❳\n━━━━━━━━━━━━━━━\n")
    for i, (title, link, snippet) in enumerate(results, 1):
        out += f"{i}. [{title}]({link})\n"
        if snippet:
            out += f"   {snippet}\n"
    return out

def get_size(bytes_val, suffix="B"):
    factor = 1024
    for unit in ["", "KB", "MB", "GB"]:
        if bytes_val < factor: return f"{bytes_val:.2f} {unit}"
        bytes_val /= factor
    return f"{bytes_val:.2f} {suffix}"

# ==========================================
# 30 فونت درخواستی (سیستم فوق پیشرفته و ضد باگ برای اموجی‌ها)
# ==========================================
# لیست نمادهای درخواستی کرال
SYMBOLS = ["♔", "♕", "♚", "♛", "⚚", "⚝", "☥", "⌬", "⌘", "⎔", "⛧", "✠", "✞", "✟", "✥", "✦", "⟡", "✶", "✷", "𖤐", "𓂀", "𓆣", "𓃠", "𓅓", "𓏲", "𓍢", "𖣔", "𖠋", "𖥧", "𖧷", "𖦏", "𖦹"]

# آرایه‌ها دقیقاً شامل ۱۰ ایندکس (از ۰ تا ۹) هستند.
FONTS = {
    1: list("0123456789"),
    2: list("𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗"),
    3: list("𝟘𝟙𝟚𝟛𝟜𝟝𝟞𝟟𝟠𝟡"),
    4: list("𝟢𝟣𝟤𝟥𝟦𝟧𝟨𝟩𝟪𝟫"),
    5: list("𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵"),
    6: list("𝟶𝟷𝟸𝟹𝟺𝟻𝟼𝟽𝟾𝟿"),
    7: list("０１２３４５６７８９"),
    8: list("۰۱۲۳۴۵۶۷۸۹"),
    9: list("٠١٢٣٤٥٦٧٨٩"),
    10: list("⓪①②③④⑤⑥⑦⑧⑨"),
    11: list("⓿❶❷❸❹❺❻❼❽❾"),
    12: list("₀₁₂₃₄₅₆₇₈₉"),
    13: list("⁰¹²³⁴⁵⁶⁷⁸⁹"),
    14: ["0️⃣", "1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"],
    15: ["0⃣", "1⃣", "2⃣", "3⃣", "4⃣", "5⃣", "6⃣", "7⃣", "8⃣", "9⃣"],
    16: ["➉", "➀", "➁", "➂", "➃", "➄", "➅", "➆", "➇", "➈"], 
    17: ["➓", "➊", "➋", "➌", "➍", "➎", "➏", "➐", "➑", "➒"], 
    18: ["⑽", "⑴", "⑵", "⑶", "⑷", "⑸", "⑹", "⑺", "⑻", "⑼"],
    19: ["⒑", "⒈", "⒉", "⒊", "⒋", "⒌", "⒍", "⒎", "⒏", "⒐"],
    20: ["㈩", "㈠", "㈡", "㈢", "㈣", "㈤", "㈥", "㈦", "㈧", "㈨"],
    21: ["⓾", "⓵", "⓶", "⓷", "⓸", "⓹", "⓺", "⓻", "⓼", "⓽"],
    22: list("🯰🯱🯲🯳🯴🯵🯶🯷🯸🯹"),
    23: list("🄀🄁🄂🄃🄄🄅🄆🄇🄈🄉"),
    24: list("🄌🄍🄎🄏🄐🄑🄒🄓🄔🄕"),
    25: ["⓿", "①", "❷", "③", "❹", "⑤", "❻", "⑦", "❽", "⑨"],
    26: ["⓪", "①", "❷", "③", "❹", "⑤", "❻", "⑦", "❽", "⑨"],
    27: ["𝟬", "①", "𝟮", "③", "𝟰", "⑤", "❻", "⑦", "❽", "⑨"],
    28: ["𝟘", "❶", "𝟚", "❸", "𝟜", "❺", "𝟞", "❼", "❽", "❾"],
    29: ["𝟢", "⓵", "𝟤", "⓷", "𝟦", "⓹", "𝟨", "⓻", "𝟪", "⓽"],
    30: ["０", "①", "２", "③", "４", "⑤", "６", "⑦", "８", "⑨"]
}

def convert_font(text, font_id):
    if font_id not in FONTS: font_id = 1
    font_list = FONTS[font_id]
    res = ""
    for char in text:
        if char.isdigit():
            res += font_list[int(char)]
        else:
            res += char
    return res

class Progress:
    def __init__(self, event, action):
        self.event = event; self.action = action; self.last_percent = -1; self.last_time = time.time()
    async def __call__(self, current, total):
        if total == 0: return
        percent = int(current * 100 / total)
        now = time.time()
        if percent >= self.last_percent + 10 or (now - self.last_time > 2 and percent != self.last_percent):
            try:
                # نوار پیشرفت شیک و لاکچری
                filled = percent // 10
                bar = f"[{'▰' * filled}{'▱' * (10 - filled)}]"
                text = _t(f"⏳ **{self.action}...**\n{bar} {percent}%", f"⏳ **{self.action}...**\n{bar} {percent}%", f"⏳ ❲ {self.action} ❳\n{bar} ⟨ {percent}% ⟩")
                await self.event.edit(text)
                self.last_percent = percent; self.last_time = now
            except: pass

# ==========================================
# سیستم لاگین (Bot API)
# NOTE: /start, /phone, /code, /password are now handled by the Bot API
# long-poller (botapi_poller.py), NOT by this Telethon bot client — the
# Telethon MTProto bot client intermittently stops receiving updates, so
# those commands go unanswered. The poller forwards login steps to this
# process via botapi_request.json (see botapi_ipc_loop). The handlers below
# are kept OFF on purpose to avoid double replies.
# ==========================================

async def set_initial_base_name():
    if not config.get("base_name"):
        me = await user_client.get_me()
        name = me.first_name if me.first_name else "Kral"
        config["base_name"] = name
        save_config(config)

# ==========================================
# تسک پروفایل (مجهز به موتور زمان‌سنج اختصاصی و بدون باگ)
# ==========================================
async def heartbeat_loop():
    """Log a heartbeat every 5 minutes so the watchdog can tell a stuck-but-alive bot apart from a dead one."""
    while True:
        try:
            logger.info("HEARTBEAT: selfbot alive")
            # status file for the Bot API poller (/start) — userbot connection state
            try:
                _connected = user_client.is_connected()
                _authorized = False
                if _connected:
                    _authorized = await user_client.is_user_authorized()
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'botapi_status.json'), 'w', encoding='utf-8') as _f:
                    json.dump({"ts": time.time(), "user_connected": bool(_connected and _authorized)}, _f)
            except Exception:
                pass
        except Exception:
            pass
        await asyncio.sleep(300)

async def botapi_ipc_loop():
    """Serve login requests (/phone, /code, /password) coming from the Bot API poller.
    The poller writes botapi_request.json; this task executes the step with the
    user client and writes the reply text to botapi_reply.json for the poller to send."""
    _base = os.path.dirname(os.path.abspath(__file__))
    _req = os.path.join(_base, 'botapi_request.json')
    _rep = os.path.join(_base, 'botapi_reply.json')
    while True:
        try:
            if os.path.exists(_req):
                try:
                    with open(_req, encoding='utf-8') as _f:
                        _r = json.load(_f)
                except Exception:
                    _r = {}
                try:
                    os.remove(_req)
                except Exception:
                    pass
                _text = (_r.get("text") or "").strip()
                _rid = _r.get("id", "")
                _out = "❌ دستور نامشخص."
                try:
                    if _text.startswith("/phone "):
                        _phone = _text[len("/phone "):].strip()
                        if not user_client.is_connected():
                            await user_client.connect()
                        if await user_client.is_user_authorized():
                            _out = "✅ قبلاً لاگین شده است!"
                        else:
                            _sent = await user_client.send_code_request(_phone)
                            login_state['phone'] = _phone
                            login_state['hash'] = _sent.phone_code_hash
                            _out = "✅ **کد ارسال شد!**\n`/code 12345`"
                    elif _text.startswith("/code "):
                        _code = _text[len("/code "):].strip()
                        try:
                            await user_client.sign_in(phone=login_state.get('phone'), code=_code, phone_code_hash=login_state.get('hash'))
                            _out = "✅ **ورود موفقیت‌آمیز بود!**"
                            await set_initial_base_name()
                            if not login_state.get('loops_started'):
                                login_state['loops_started'] = True
                                asyncio.get_running_loop().create_task(background_profile_updater())
                                asyncio.get_running_loop().create_task(heartbeat_loop())
                        except SessionPasswordNeededError:
                            _out = "⚠️ **رمز دوم را بفرست:**\n`/password رمز`"
                        except Exception as _e:
                            _out = f"❌ خطا: {_e}"
                    elif _text.startswith("/password "):
                        _pw = _text[len("/password "):].strip()
                        try:
                            await user_client.sign_in(password=_pw)
                            _out = "✅ ورود کامل شد!"
                            await set_initial_base_name()
                            if not login_state.get('loops_started'):
                                login_state['loops_started'] = True
                                asyncio.get_running_loop().create_task(background_profile_updater())
                                asyncio.get_running_loop().create_task(heartbeat_loop())
                        except Exception as _e:
                            _out = f"❌ خطا: {_e}"
                except Exception as _e:
                    _out = f"❌ خطا:\n{str(_e)}"
                try:
                    with open(_rep, 'w', encoding='utf-8') as _f:
                        json.dump({"id": _rid, "text": _out}, _f)
                except Exception:
                    pass
        except Exception:
            pass
        await asyncio.sleep(2)

async def background_profile_updater():
    import random
    last_symbol_time = 0
    current_symbol = ""
    
    while True:
        try:
            if not user_client.is_connected() or not await user_client.is_user_authorized():
                await asyncio.sleep(10)
                continue

            me = await user_client.get_me()

            current_real_time = get_tehran_now()
            current_time_str = current_real_time.strftime("%H:%M")
            
            base_name = config.get("base_name", "Kral")
            final_name = base_name
            needs_update = False

            if config.get("symbol"):
                now = time.time()
                if now - last_symbol_time >= config.get("symbol_interval"):
                    current_symbol = random.choice(SYMBOLS)
                    last_symbol_time = now
                if current_symbol: final_name += f" {current_symbol}"
                needs_update = True

            if config.get("time"):
                styled_time = convert_font(current_time_str, config.get("font_id"))
                final_name += f" {styled_time}"
                needs_update = True

            if not config.get("symbol") and not config.get("time"):
                final_name = base_name
                needs_update = True

            if needs_update and me and me.first_name != final_name:
                try:
                    await user_client(functions.account.UpdateProfileRequest(first_name=final_name))
                except FloodWaitError as fw:
                    logger.warning(f"FloodWait in Profile Updater: {fw.seconds}s")
                    await asyncio.sleep(fw.seconds)

            sleep_seconds = 60 - get_tehran_now().second
            if sleep_seconds < 1: sleep_seconds = 60
            await asyncio.sleep(sleep_seconds)

        except Exception as e: 
            logger.error(f"Profile Updater Loop Error: {e}")
            await asyncio.sleep(15)

# ==========================================
# دستورات یوزربات
# ==========================================

# منوی تنظیم زبان (UI Language Settings)
@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.st(?:\s+(\d))?$'))
async def ui_lang_menu(event):
    choice = event.pattern_match.group(1)
    if choice == '1':
        config["lang"] = "fa"; save_config(config)
        await event.edit("✅ **زبان رابط کاربری به «فارسی» تغییر یافت.**")
    elif choice == '2':
        config["lang"] = "en"; save_config(config)
        await event.edit("✅ **UI Language changed to «English».**")
    elif choice == '3':
        config["lang"] = "mix"; save_config(config)
        await event.edit("✅ **زبان رابط کاربری به «ترکیبی | Mixed» تغییر یافت.**")
    else:
        current_lang = config.get("lang", "mix")
        status = "فارسی 🇮🇷" if current_lang == "fa" else "English 🇺🇸" if current_lang == "en" else "ترکیبی | Mix 🌍"
        
        fa_text = f"⚙️ **تنظیمات زبان رابط کاربری:**\n━━━━━━━━━━━━━━━\nوضعیت فعلی: {status}\n\n1️⃣ فارسی (`.st 1`)\n2️⃣ انگلیسی (`.st 2`)\n3️⃣ ترکیبی شیک (`.st 3`)\n\n*(نکته: تلگرام به یوزربات‌ها اجازه ساخت دکمه شیشه‌ای نمی‌دهد، لذا این منو به صورت متنی و با دستورات عددی کار می‌کند.)*"
        en_text = f"⚙️ **UI Language Settings:**\n━━━━━━━━━━━━━━━\nCurrent: {status}\n\n1️⃣ Persian (`.st 1`)\n2️⃣ English (`.st 2`)\n3️⃣ Elegant Mix (`.st 3`)\n\n*(Note: Userbots cannot send inline buttons, so this menu uses command numbers.)*"
        mix_text = f"⚙️ ❲ تنظیمات زبان | Language ❳\n━━━━━━━━━━━━━━━\n⊳ وضعیت | Current: {status}\n\n1️⃣ فارسی | Persian (`.st 1`)\n2️⃣ انگلیسی | English (`.st 2`)\n3️⃣ ترکیبی | Mix (`.st 3`)\n\n*(تلگرام قابلیت دکمه شیشه‌ای برای یوزربات ندارد | No inline buttons for Userbots)*"
        
        await event.edit(_t(fa_text, en_text, mix_text))


def _tools_help_sections():
    """بخش پویا ابزارهای ثبت‌شده برای .راهنما — در هر فراخوانی از config
    ساخته می‌شود، پس .tool del ابزار را از راهنما هم حذف می‌کند."""
    tools = config.get("custom_tools", {}) or {}
    lines = []
    for name, t in sorted(tools.items()):
        if not isinstance(t, dict):
            continue
        desc = (t.get("desc") or "").strip()
        method = (t.get("method") or "POST").upper()
        lines.append(f"⊳ `.{name} <ورودی>` [{method}]" + (f" — {desc}" if desc else ""))
    if not lines:
        return "", "", ""
    fa = "\n\n🔧 **ابزارهای ثبت‌شده (دستور مستقیم):**\n" + "\n".join(lines)
    en = "\n\n🔧 **Registered tools (direct commands):**\n" + "\n".join(lines)
    mix = "\n\n🔧 ❲ ابزارهای ثبت‌شده | Registered Tools ❳\n" + "\n".join(lines)
    return fa, en, mix


@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.راهنما$'))
async def help_cmd(event):
    fa_help = """
💎 ❲ **راهنمای جامع یوزربات کرال** ❳ 💎
━━━━━━━━━━━━━━━━━━━
🤖 **هوش مصنوعی و چت:**
⊳ `.ai [متن]` : چت با دستیار هوشمند (روی عکس ریپلای کن تا ببیندش؛ می‌تواند کانال را چک کند و گزارش بدهد)
⊳ `.فراموش` : پاک کردن حافظه‌ی چت فعلی
⊳ `.tool add نام | آدرس | کلید | توضیح` یا توضیح آزاد : ثبت API به‌عنوان ابزار — بعد با `.{نام} <ورودی>` صدا می‌زنیش
⊳ `.tool list` / `.tool del [نام]` : لیست و حذف ابزارها
⊳ `.نظر` : تحلیل پیام ریپلای شده
⊳ `.ais` / `.models` : لیست مدل‌های اضافه‌شده (فقط مدل‌های خودت)
⊳ `.add` : ویزارد مرحله‌ای افزودن مدل (یا `.add نام | مدل | کلید | آدرس`)
⊳ `.del [نام]` : حذف مدل اضافه‌شده
⊳ `.ailen [1/2]` : تغییر طول پیام AI
⊳ `.tone` : تنظیم لحن پاسخگویی ربات
⊳ `.kral` : تنظیم سطح دفاع از نام شما
⊳ `.pv [1/2]` : پاسخگوی خودکار پیوی
⊳ `.gp [1/2]` : پاسخگوی خودکار گروه‌ها
⊳ `.type [1/2]` : افکت تایپ تدریجی

🌐 **ابزارها و جستجو:**
⊳ `.image [متن]` : 🎨 نقاشی با هوش مصنوعی
⊳ `.tagall [متن]` : 📣 منشن نامرئی اعضا
⊳ `.sch [متن]` : جستجوگر در اینترنت
⊳ `.خبر` : گزارش مفصل و جذاب اخبار
⊳ `.خبر کوتاه` : خلاصه سریع اخبار
⊳ `.post [عدد]` : تنظیم تعداد پست خبری
⊳ `.tr [زبان] [متن]` : مترجم هوشمند
⊳ `.sum` : خلاصه‌ساز متون طولانی
⊳ `.arz [ارز]` : نرخ دلار و ارزها به تومان
⊳ `.fake [ثانیه]` : نمایش تایپ جعلی در چت
⊳ `.ttl` : ذخیره بی‌صدای مدیای تایمردار در سیو مسیج
⊳ `.antidel [1/0]` : ضدحذف و ضدویرایش پیام‌های پیوی
⊳ `.دانلود [لینک]` : دانلود مستقیم فایل

⚙️ **پروفایل و سیستم:**
⊳ `.st` : 🌍 تنظیم زبان رابط کاربری
⊳ `.setname [اسم]` : تنظیم نام اکانت
⊳ `.setbio [متن]` : تنظیم بیو اکانت
⊳ `.ست` : همگام‌سازی دستی زمان
⊳ `.time [1/2]` : نمایش ساعت آنلاین
⊳ `.font` : انتخاب از بین ۳۰ فونت عددی
⊳ `.symbol [1/2/زمان]` : نماد چرخشی نام
⊳ `.in` یا `.sp` : دانلودر سریع مدیا
⊳ `.spotify [لینک]` : دانلود آهنگ اسپاتیفای (mp3)
⊳ `.tts [متن]` / `.ویس [متن]` : تبدیل متن به ویس
⊳ `.for [لینک]` : دور زدن قفل کانال‌ها (پشتیبانی از کامنت)
⊳ `.وضعیت` : مانیتورینگ منابع سرور
⊳ `.اپتایم` : نمایش مدت زمان فعالیت
⊳ `.ریست` : پاکسازی کش و رم سیستم
⊳ `.ریستارت` : راه‌اندازی مجدد ربات
⊳ `.ping` : تست سرعت اتصال
⊳ `.info` : استخراج اطلاعات کاربر
⊳ `.ghost [متن]` : پیام محو شونده (۳ثانیه)
⊳ `.del` : حذف سریع پیام خود
⊳ `.afk [دلیل]` / `.back` : حالت عدم حضور
"""

    en_help = """
💎 ❲ **Kral Userbot Guide** ❳ 💎
━━━━━━━━━━━━━━━━━━━
🤖 **AI & Chat Automation:**
⊳ `.ai [text]` : Smart AI Chat (reply to a photo so it can see it; it can check a channel and report back)
⊳ `.فراموش` : Clear current chat memory
⊳ `.tool add name | url | key | desc` or free-text explanation : Register an API as a tool — then call it with `.{name} <input>`
⊳ `.tool list` / `.tool del [name]` : List & delete tools
⊳ `.نظر` : Analyze replied message
⊳ `.ais` / `.models` : List your added models (only your own)
⊳ `.add` : Step-by-step wizard to add a model (or `.add name | model | key | url`)
⊳ `.del [name]` : Delete an added model
⊳ `.ailen [1/2]` : Change AI response length
⊳ `.tone` : Set bot tone/personality
⊳ `.kral` : Name defense sensitivity
⊳ `.pv [1/2]` : Auto-reply Private
⊳ `.gp [1/2]` : Auto-reply Groups
⊳ `.type [1/2]` : Typing effect

🌐 **Tools & Search:**
⊳ `.image [text]` : 🎨 AI Image Gen
⊳ `.tagall [text]` : 📣 Invisible Mention
⊳ `.sch [text]` : Web Searcher
⊳ `.خبر` : Detailed News Report
⊳ `.خبر کوتاه` : Short News Titles
⊳ `.post [num]` : Set news post count
⊳ `.tr [lang] [text]` : Smart Translator
⊳ `.sum` : Text Summarizer
⊳ `.arz [code]` : Currency rates in Toman
⊳ `.fake [seconds]` : Fake typing in chat
⊳ `.ttl` : Silently save TTL media to Saved Messages
⊳ `.antidel [1/0]` : Anti-delete & anti-edit for PV
⊳ `.دانلود [link]` : Direct file download

⚙️ **Profile & System:**
⊳ `.st` : 🌍 UI Language Settings
⊳ `.setname [name]` : Set Base Name
⊳ `.setbio [text]` : Set Account Bio
⊳ `.ست` : Force Time Sync
⊳ `.time [1/2]` : Online Clock in Name
⊳ `.font` : Choose Clock Font (30 styles)
⊳ `.symbol [1/2/time]` : Rotating Symbols
⊳ `.in` or `.sp` : Fast Media Downloader
⊳ `.spotify [link]` : Download Spotify track (mp3)
⊳ `.tts [text]` : Text to voice
⊳ `.for [link]` : Bypass Channel Lock (supports comments)
⊳ `.وضعیت` : Server Resource Monitor
⊳ `.اپتایم` : Bot Uptime
⊳ `.ریست` : Clear Cache & RAM
⊳ `.ریستارت` : Restart Bot
⊳ `.ping` : Connection Speed Test
⊳ `.info` : Extract User Info
⊳ `.ghost [text]` : Auto-delete msg (3s)
⊳ `.del` : Fast Message Delete
⊳ `.afk [reason]` / `.back` : AFK Mode
"""

    mix_help = """
💎 ❲ **Kral Userbot | راهنمای جامع** ❳ 💎
━━━━━━━━━━━━━━━━━━━━
🤖 ❲ هوش مصنوعی | AI & Chat ❳
⊳ `.ai [text]` : چت هوشمند | Smart Chat (روی عکس ریپلای کن تا ببیندش؛ کانال را چک می‌کند و گزارش می‌دهد)
⊳ `.فراموش` : پاک کردن حافظه | Clear Memory
⊳ `.tool add نام | آدرس | کلید | توضیح` یا توضیح آزاد : ثبت ابزار | Register Tool — بعد با `.{نام} <ورودی>` صدا می‌زنیش
⊳ `.tool list` / `.tool del [نام]` : لیست ابزارها | List & Delete
⊳ `.نظر` : تحلیل پیام | Analyze msg
⊳ `.ais` / `.models` : لیست مدل‌ها | Your Models
⊳ `.add` : ویزارد افزودن مدل | Add Model Wizard (یا `.add نام | مدل | کلید | آدرس`)
⊳ `.del [نام]` : حذف مدل | Delete Model
⊳ `.ailen [1/2]` : طول پیام | Response Length
⊳ `.tone` : لحن ربات | Bot Tone
⊳ `.kral` : دفاع از نام | Defense Mode
⊳ `.pv [1/2]` : منشی پیوی | PV Auto-reply
⊳ `.gp [1/2]` : منشی گروه | GP Auto-reply
⊳ `.type [1/2]` : افکت تایپ | Typing Effect

🌐 ❲ ابزارها | Tools & Search ❳
⊳ `.image [text]` : 🎨 نقاشی | AI Image
⊳ `.tagall [text]` : 📣 تگ گروهی | Tag All
⊳ `.sch [text]` : جستجوگر وب | Web Search
⊳ `.خبر` : گزارش اخبار | News Report
⊳ `.خبر کوتاه` : اخبار کوتاه | Short News
⊳ `.post [num]` : تعداد خبر | News Count
⊳ `.tr [lang] [text]` : مترجم | Translator
⊳ `.sum` : خلاصه‌ساز | Summarizer
⊳ `.arz [code]` : نرخ ارز | Currency Rates
⊳ `.fake [sec]` : تایپ جعلی | Fake Typing
⊳ `.ttl` : مدیای تایمردار | TTL Media Saver
⊳ `.antidel [1/0]` : ضدحذف پیوی | Anti-delete
⊳ `.دانلود [link]` : دانلود مستقیم | Direct Download

⚙️ ❲ سیستم و پروفایل | System & Profile ❳
⊳ `.st` : 🌍 زبان رابط | UI Language
⊳ `.setname [name]` : اسم پایه | Base Name
⊳ `.setbio [text]` : بیو اکانت | Account Bio
⊳ `.ست` : همگام‌سازی زمان | Sync Time
⊳ `.time [1/2]` : ساعت پروفایل | Time Profile
⊳ `.font` : فونت ساعت | Clock Font
⊳ `.symbol [1/2/time]` : نمادها | Symbols
⊳ `.in` / `.sp` : دانلودر | Downloader
⊳ `.spotify [لینک]` : آهنگ اسپاتیفای | Spotify track
⊳ `.tts [متن]` : متن به ویس | Text to voice
⊳ `.for [link]` : دور زدن قفل | Bypass Lock (پشتیبانی از کامنت | Comments)
⊳ `.وضعیت` : مانیتورینگ | Sys Monitor
⊳ `.اپتایم` : زمان روشنی | Uptime
⊳ `.ریست` : پاکسازی رم | Reset Cache
⊳ `.ریستارت` : راه‌اندازی مجدد | Restart
⊳ `.ping` : تست سرعت | Ping
⊳ `.info` : اطلاعات کاربر | User Info
⊳ `.ghost [text]` : پیام روح | Ghost Msg
⊳ `.del` : حذف سریع | Quick Delete
⊳ `.afk` / `.back` : حالت آفلاین | AFK
"""
    fa_tools, en_tools, mix_tools = _tools_help_sections()
    await event.edit(_t(fa_help + fa_tools, en_help + en_tools, mix_help + mix_tools))

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.(restart|ریستارت)$'))
async def restart_bot(event):
    await event.edit(_t("🔄 در حال راه‌اندازی مجدد یوزربات...", "🔄 Restarting Userbot...", "🔄 ❲ ریستارت | Restarting ❳..."))
    os.execl(sys.executable, sys.executable, *sys.argv)

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.(set|ست)$'))
async def force_time_sync_cmd(event):
    # ساعت همیشه مستقیم از Asia/Tehran خوانده می‌شود — دیگر نیازی به همگام‌سازی با سرور نیست
    current_time_str = get_tehran_now().strftime("%H:%M:%S")
    text = _t(f"✅ **زمان تهران:**\n⏰ ساعت: `{current_time_str}`", f"✅ **Tehran time:**\n⏰ Clock: `{current_time_str}`", f"✅ ❲ زمان تهران | Tehran Time ❳\n⏰ ساعت | Clock: `{current_time_str}`")
    await event.edit(text)

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.نظر$'))
async def opinion_cmd(event):
    reply = await event.get_reply_message()
    if not reply or not reply.text: 
        return await event.edit(_t("❌ پیام متنی ریپلای نشده است.", "❌ Please reply to a text message.", "❌ لطفاً ریپلای کنید | Please reply to a text message."))
    msg = await event.edit(_t("⏳ در حال تحلیل...", "⏳ Analyzing...", "⏳ ❲ در حال تحلیل | Analyzing ❳..."))
    prompt = f"کاربر این پیام را فرستاده:\n\n{reply.text}\n\nلطفاً نظر خودت را درباره این پیام بگو، اگر سوال است جواب بده و اگر نیاز به تحلیل دارد آن را تحلیل کن. کامل و با لحن طبیعی خودت جواب بده."
    res = await ask_ai(prompt, short_answer=False, is_chat=True)
    
    header = _t("🗣 **نظر من:**\n\n", "🗣 **My Opinion:**\n\n", "🗣 ❲ نظر هوش مصنوعی | AI Opinion ❳\n━━━━━━━━━━━━━━━\n")
    await send_type_mode(msg, f"{header}{res}")

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.kral(?:\s+(\d))?'))
async def kral_menu(event):
    choice = event.pattern_match.group(1)
    if choice == '1':
        config["kral_defense"] = 1; save_config(config)
        await event.edit(_t("❌ دفاع خاموش شد.", "❌ Defense Disabled.", "❌ ❲ دفاع خاموش شد | Defense Disabled ❳"))
    elif choice == '2':
        config["kral_defense"] = 2; save_config(config)
        await event.edit(_t("✅ دفاع محترمانه فعال شد.", "✅ Polite Defense Enabled.", "✅ ❲ دفاع محترمانه | Polite Defense Enabled ❳"))
    elif choice == '3':
        config["kral_defense"] = 3; save_config(config)
        await event.edit(_t("🔥 دفاع خشن فعال شد!", "🔥 Aggressive Defense Enabled!", "🔥 ❲ دفاع خشن | Aggressive Defense Enabled ❳"))
    else:
        status_map = {1: "خاموش | Off 🔴", 2: "محترمانه | Polite 🔵", 3: "خشن | Aggressive 🔴🔥"}
        status = status_map.get(config.get("kral_defense"), "خشن | Aggressive")
        
        fa = f"🛡 **دفاع از نام کرال:**\nوضعیت: {status}\n\n۱. خاموش\n۲. محترمانه\n۳. خشن"
        mix = f"🛡 ❲ دفاع از نام | Defense Mode ❳\n━━━━━━━━━━━━━━━\n⊳ وضعیت | Status: {status}\n\n1️⃣ خاموش | Off (`.kral 1`)\n2️⃣ محترمانه | Polite (`.kral 2`)\n3️⃣ خشن | Aggressive (`.kral 3`)"
        await event.edit(_t(fa, fa, mix))

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.(uptime|اپتایم)$'))
async def uptime_cmd(event):
    uptime_seconds = int(time.time() - START_TIME)
    days, rem = divmod(uptime_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    
    fa = f"⏳ **آپتایم سیستم:**\n📆 روز: `{days}`\n⏱ ساعت: `{hours}`\n⏳ دقیقه: `{minutes}`\n⏲ ثانیه: `{seconds}`"
    mix = f"⏳ ❲ آپتایم | System Uptime ❳\n━━━━━━━━━━━━━━━\n📆 روز | Days: `{days}`\n⏱ ساعت | Hours: `{hours}`\n⏳ دقیقه | Mins: `{minutes}`\n⏲ ثانیه | Secs: `{seconds}`"
    
    await event.edit(_t(fa, fa, mix))

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.tone(?:\s+(\d))?'))
async def tone_menu(event):
    choice = event.pattern_match.group(1)
    if choice == '1': 
        config["ai_tone"] = "playful"; save_config(config)
        await event.edit(_t("✅ لحن شر و شیطون تنظیم شد.", "✅ Playful tone set.", "✅ ❲ لحن شیطون | Playful Tone ❳"))
    elif choice == '2': 
        config["ai_tone"] = "serious"; save_config(config)
        await event.edit(_t("✅ لحن رسمی تنظیم شد.", "✅ Serious tone set.", "✅ ❲ لحن رسمی | Serious Tone ❳"))
    elif choice == '3':
        config["ai_tone"] = "normal"; save_config(config)
        await event.edit(_t("✅ لحن معمولی تنظیم شد.", "✅ Normal tone set.", "✅ ❲ لحن معمولی | Normal Tone ❳"))
    else:
        current = config.get("ai_tone")
        status = "شیطون 😜" if current == "playful" else "رسمی 🎩" if current == "serious" else "معمولی 🗣"
        
        mix = f"🤖 ❲ لحن ربات | Bot Tone ❳\n━━━━━━━━━━━━━━━\n⊳ وضعیت | Current: {status}\n\n1️⃣ شیطون | Playful (`.tone 1`)\n2️⃣ رسمی | Serious (`.tone 2`)\n3️⃣ معمولی | Normal (`.tone 3`)"
        await event.edit(_t(mix, mix, mix))

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.ailen(?:\s+(\d))?'))
async def ailen_cmd(event):
    choice = event.pattern_match.group(1)
    if choice == '1':
        config["ai_length"] = 1; save_config(config)
        await event.edit(_t("✅ پاسخ کوتاه تنظیم شد.", "✅ Short response set.", "✅ ❲ پاسخ کوتاه | Short Response ❳"))
    elif choice == '2':
        config["ai_length"] = 2; save_config(config)
        await event.edit(_t("✅ پاسخ بلند تنظیم شد.", "✅ Long response set.", "✅ ❲ پاسخ بلند | Long Response ❳"))
    else:
        status = "بلند 📝" if config.get("ai_length", 2) == 2 else "کوتاه ✂️"
        mix = f"📏 ❲ طول پاسخ چت | AI Length ❳\n━━━━━━━━━━━━━━━\n⊳ وضعیت | Current: {status}\n\n1️⃣ کوتاه | Short (`.ailen 1`)\n2️⃣ بلند | Long (`.ailen 2`)"
        await event.edit(_t(mix, mix, mix))

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.(ais|models)(?:\s+(\d+))?$'))
async def ais_menu(event):
    # فقط مدل‌های اضافه‌شده با .add — شماره‌گذاری از ۱
    custom_models = config.get("custom_models", {}) or {}
    names = sorted(custom_models.keys())

    choice = event.pattern_match.group(2)
    if choice:
        try:
            idx = int(choice) - 1
        except:
            idx = -1
        if 0 <= idx < len(names):
            picked = custom_models[names[idx]]
            config["ai_model"] = picked.get("id", ""); save_config(config)
            await event.edit(_t(f"✅ مدل به {picked.get('name', names[idx])} تغییر یافت.", f"✅ Model changed to {picked.get('name', names[idx])}.", f"✅ ❲ مدل تغییر یافت | Model Changed ❳\n⊳ {picked.get('name', names[idx])}"))
            return
        return await event.edit(_t("❌ شماره مدل اشتباه است.", "❌ Wrong model number.", "❌ شماره اشتباه | Wrong number"))
    if not names:
        return await event.edit(_t("🤖 هنوز هیچ مدلی اضافه نشده است.\nبا `.add` یک مدل اضافه کن.", "🤖 No model has been added yet.\nAdd one with `.add`.", "🤖 هیچ مدلی اضافه نشده | No models yet\n⊳ با `.add` یک مدل اضافه کن."))
    mix = "🤖 ❲ مدل‌های هوش مصنوعی | AI Models ❳\n━━━━━━━━━━━━━━━\n"
    for i, _name in enumerate(names, 1):
        _m = custom_models[_name]
        active = "✅" if config.get("ai_model") == _m.get("id") else "🔸"
        mix += f"{active} {i}. {_m.get('name', _name)} (`{_m.get('id', '')}`)\n"

    len_status = "بلند 📝" if config.get("ai_length", 2) == 2 else "کوتاه ✂️"
    mix += f"━━━━━━━━━━━━━━━\n📏 طول پاسخ چت | Chat Length: {len_status}\n*(تغییر با `.ailen 1` یا `.ailen 2`)*\n*(مدل جدید با `.add` اضافه کن)*"
    await event.edit(_t(mix, mix, mix))

# ==========================================
# ویزارد مرحله‌ای .add — نام ← آدرس ← توکن ← نام مدل
# ==========================================
ADD_WIZARD = {}  # chat_id -> {"step": str, "data": dict, "prompt_msg_id": int, "ts": float}
ADD_WIZARD_TIMEOUT = 300  # ۵ دقیقه عدم فعالیت

def _wizard_expired(state):
    return time.time() - state.get("ts", 0) > ADD_WIZARD_TIMEOUT

async def _wizard_ask(chat_id, state, text):
    try:
        await user_client.edit_message(chat_id, state.get("prompt_msg_id"), text)
    except Exception:
        pass

@user_client.on(events.NewMessage(outgoing=True))
async def add_wizard_catcher(event):
    """گیرنده‌ی پاسخ‌های ویزارد .add — پیام‌های ساده‌ی مالک را می‌گیرد."""
    chat_id = event.chat_id
    state = ADD_WIZARD.get(chat_id)
    if not state:
        return
    text = (event.raw_text or "").strip()
    if text == ".cancel":
        ADD_WIZARD.pop(chat_id, None)
        await _wizard_ask(chat_id, state, _t("❌ افزودن مدل لغو شد.", "❌ Model setup cancelled.", "❌ لغو شد | Cancelled"))
        raise events.StopPropagation
    if _wizard_expired(state):
        ADD_WIZARD.pop(chat_id, None)
        await _wizard_ask(chat_id, state, _t("⏰ زمان ویزارد تمام شد. دوباره `.add` بزن.", "⏰ Wizard expired. Send `.add` again.", "⏰ ویزارد منقضی شد | Wizard expired"))
        return
    if not text:
        return
    if text.startswith("."):
        # دستور دیگر → لغو ویزارد و عبور دستور
        ADD_WIZARD.pop(chat_id, None)
        await _wizard_ask(chat_id, state, _t("❌ ویزارد لغو شد — دستور جدید اجرا می‌شود.", "❌ Wizard cancelled — running your command.", "❌ ویزارد لغو شد | Wizard cancelled"))
        return
    state["ts"] = time.time()
    step = state["step"]
    data = state["data"]
    if step == "name":
        data["name"] = text
        state["step"] = "url"
        await _wizard_ask(chat_id, state, _t("🌐 **مرحله ۲ از ۴ — آدرس (base URL)**\nآدرس API را بنویس:\n`https://api.groq.com/openai/v1/chat/completions`\n\n(لغو با `.cancel`)", "🌐 **Step 2 of 4 — base URL**\nSend the API base URL.\n\n(Cancel with `.cancel`)", "🌐 ❲ مرحله ۲ از ۴ | آدرس ❳\n⊳ آدرس API را بنویس:\n`https://api.groq.com/openai/v1/chat/completions`\n\n(لغو با `.cancel`)"))
    elif step == "url":
        if not (text.startswith("http://") or text.startswith("https://")):
            await _wizard_ask(chat_id, state, _t("❌ آدرس باید با http:// یا https:// شروع شود.\nدوباره آدرس را بنویس:", "❌ URL must start with http:// or https://.\nSend the URL again:", "❌ آدرس اشتباه | Invalid URL\n⊳ دوباره آدرس را بنویس:"))
            raise events.StopPropagation
        data["url"] = text.rstrip('/')
        state["step"] = "token"
        await _wizard_ask(chat_id, state, _t("🔑 **مرحله ۳ از ۴ — توکن (API key)**\nتوکن را بنویس:\n⚠️ پیام توکن بعد از ذخیره، خودکار حذف می‌شود.\n\n(لغو با `.cancel`)", "🔑 **Step 3 of 4 — token (API key)**\nSend the token.\n⚠️ Your token message will be auto-deleted after saving.\n\n(Cancel with `.cancel`)", "🔑 ❲ مرحله ۳ از ۴ | توکن ❳\n⊳ توکن را بنویس:\n⚠️ پیام توکن بعد از ذخیره حذف می‌شود.\n\n(لغو با `.cancel`)"))
    elif step == "token":
        data["api_key"] = text
        state["step"] = "model"
        # حذف پیام توکن برای امنیت
        try:
            await event.delete()
        except Exception:
            pass
        await _wizard_ask(chat_id, state, _t("🤖 **مرحله ۴ از ۴ — نام مدل**\nشناسه مدل را بنویس:\n`llama-3.1-8b-instant`\n\n(لغو با `.cancel`)", "🤖 **Step 4 of 4 — model name**\nSend the model id:\n`llama-3.1-8b-instant`\n\n(Cancel with `.cancel`)", "🤖 ❲ مرحله ۴ از ۴ | نام مدل ❳\n⊳ شناسه مدل را بنویس:\n`llama-3.1-8b-instant`\n\n(لغو با `.cancel`)"))
    elif step == "model":
        data["id"] = text
        name = data["name"]
        custom_models = config.get("custom_models", {}) or {}
        existed = name in custom_models
        if "groq" in data["url"]:
            provider = "groq"
        elif "bynara" in data["url"]:
            provider = "bynara"
        else:
            provider = "custom"
        custom_models[name] = {"id": data["id"], "name": name, "provider": provider, "url": data["url"], "api_key": data["api_key"]}
        config["custom_models"] = custom_models
        save_config(config)
        ADD_WIZARD.pop(chat_id, None)
        note = " (جایگزین مدل قبلی با همین نام شد)" if existed else ""
        await _wizard_ask(chat_id, state, _t(f"✅ مدل «{name}» اضافه شد{note}.\n⊳ مدل: `{data['id']}`\n⊳ ارائه‌دهنده: `{provider}`\n⊳ آدرس: `{data['url']}`\nبا `.models` می‌توانی انتخابش کنی.", f"✅ Model «{name}» added{' (replaced the previous model with this name)' if existed else ''}.\n⊳ Model: `{data['id']}`\n⊳ Provider: `{provider}`\n⊳ URL: `{data['url']}`\nPick it with `.models`.", f"✅ ❲ مدل اضافه شد | Model Added ❳\n⊳ نام | Name: `{name}`{note}\n⊳ مدل | Model: `{data['id']}`\n⊳ ارائه‌دهنده | Provider: `{provider}`\n⊳ آدرس | URL: `{data['url']}`"))
    raise events.StopPropagation

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.add(?:\s+(.*))?$'))
async def add_custom_model(event):
    raw = (event.pattern_match.group(1) or "").strip()
    if not raw:
        # ویزارد مرحله‌ای: نام ← آدرس ← توکن ← نام مدل
        ADD_WIZARD[event.chat_id] = {"step": "name", "data": {}, "prompt_msg_id": event.id, "ts": time.time()}
        await event.edit(_t("📝 **مرحله ۱ از ۴ — نام مدل**\nیک نام برای این مدل بنویس:\n`mymodel`\n\n(لغو با `.cancel`)", "📝 **Step 1 of 4 — model name**\nSend a name for this model:\n`mymodel`\n\n(Cancel with `.cancel`)", "📝 ❲ مرحله ۱ از ۴ | نام مدل ❳\n⊳ یک نام بنویس:\n`mymodel`\n\n(لغو با `.cancel`)"))
        return
    parts = [p.strip() for p in raw.split('|')]
    if len(parts) != 4 or not all(parts):
        return await event.edit(_t(
            "❌ فرمت اشتباه است.\nاستفاده:\n`.add نام | model_id | api_key | base_url`\nمثال:\n`.add mymodel | llama-3.1-8b | sk-... | https://api.groq.com/openai/v1/chat/completions`\n\nیا فقط `.add` بزن و مرحله‌به‌مرحله جواب بده.",
            "❌ Wrong format.\nUsage:\n`.add name | model_id | api_key | base_url`\n\nOr just send `.add` and answer step by step.",
            "❌ فرمت اشتباه | Wrong format\n⊳ استفاده | Usage:\n`.add نام | model_id | api_key | base_url`\n\nیا فقط `.add` بزن."))
    name, model_id, api_key, base_url = parts
    if base_url.startswith("http://") or base_url.startswith("https://"):
        base_url = base_url.rstrip('/')
    else:
        return await event.edit(_t("❌ آدرس base_url باید با http:// یا https:// شروع شود.", "❌ base_url must start with http:// or https://.", "❌ آدرس اشتباه | Invalid base_url"))
    if "groq" in base_url:
        provider = "groq"
    elif "bynara" in base_url:
        provider = "bynara"
    else:
        provider = "custom"
    custom_models = config.get("custom_models", {}) or {}
    existed = name in custom_models
    custom_models[name] = {"id": model_id, "name": name, "provider": provider, "url": base_url, "api_key": api_key}
    config["custom_models"] = custom_models
    save_config(config)
    note = " (جایگزین مدل قبلی با همین نام شد)" if existed else ""
    await event.edit(_t(f"✅ مدل سفارشی «{name}» اضافه شد{note}.\n⊳ مدل: `{model_id}`\n⊳ ارائه‌دهنده: `{provider}`\nبا `.models` می‌توانی انتخابش کنی.", f"✅ Custom model «{name}» added{' (replaced the previous model with this name)' if existed else ''}.\n⊳ Model: `{model_id}`\n⊳ Provider: `{provider}`\nPick it with `.models`.", f"✅ ❲ مدل سفارشی اضافه شد | Custom Model Added ❳\n⊳ نام | Name: `{name}`{note}\n⊳ مدل | Model: `{model_id}`\n⊳ ارائه‌دهنده | Provider: `{provider}`"))

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.del (.+)$'))
async def del_custom_model(event):
    name = event.pattern_match.group(1).strip()
    custom_models = config.get("custom_models", {}) or {}
    if name not in custom_models:
        return await event.edit(_t(f"❌ مدل سفارشی «{name}» پیدا نشد.", f"❌ Custom model «{name}» not found.", f"❌ مدل پیدا نشد | Model not found: `{name}`"))
    if config.get("ai_model") == custom_models[name].get("id"):
        remaining = [m for n, m in custom_models.items() if n != name]
        config["ai_model"] = remaining[0].get("id", "") if remaining else ""
    del custom_models[name]
    config["custom_models"] = custom_models
    save_config(config)
    await event.edit(_t(f"✅ مدل سفارشی «{name}» حذف شد.", f"✅ Custom model «{name}» deleted.", f"✅ ❲ مدل حذف شد | Model Deleted ❳\n⊳ `{name}`"))

# ==========================================
# ثبت ابزار AI — قالب pipe و توضیح آزاد (هر دو به یک مسیر ثبت می‌رسند)
# ==========================================
def _derive_tool_name(desc):
    """از توضیح، یک نام معتبر برای ابزار می‌سازد (حروف/عدد/_/-، حداکثر ۴۰ کاراکتر)."""
    words = re.findall(r'[\w]+', desc or "", re.UNICODE)
    name = "_".join(words[:2]).strip("_")
    return (name[:40].strip("_") or "tool")

async def extract_tool_from_text(text):
    """مشخصات یک ابزار را از توضیح آزاد کاربر با AI استخراج می‌کند.
    برمی‌گرداند (data یا None, err, err_text) که err یکی از None / "ai_failed" / "no_url" است.
    آدرس هرگز ساخته نمی‌شود — اگر در توضیح آدرسی نباشد، err برابر "no_url" است."""
    prompt = (
        "تو یک استخراج‌کننده‌ی مشخصات API هستی. از توضیح زیر فقط و فقط یک شیء JSON برگردان، "
        "بدون هیچ متن اضافه، بدون بلوک کد، بدون توضیح:\n"
        '{"name": "نام کوتاه ابزار (فقط حروف، عدد، _ و -، حداکثر ۴۰ کاراکتر) یا رشته‌ی خالی اگر نامی گفته نشده", '
        '"url": "آدرس کامل API با http:// یا https:// یا رشته‌ی خالی اگر آدرسی گفته نشده", '
        '"key": "کلید API اگر گفته شده، وگرنه رشته‌ی خالی", '
        '"description": "توضیح کوتاه یک‌خطی به زبان کاربر", '
        '"method": "GET یا POST — پیش‌فرض POST"}\n'
        "قانون مهم: هرگز آدرس نساز؛ اگر آدرسی در توضیح نیست، url را رشته‌ی خالی بگذار. "
        "اگر نامی گفته نشده، name را رشته‌ی خالی بگذار.\n\nتوضیح کاربر:\n" + text
    )
    res = await ask_ai(prompt, is_chat=False)
    if not res or not isinstance(res, str) or res.strip().startswith("❌"):
        return None, "ai_failed", (res.strip() if isinstance(res, str) and res.strip() else None)
    s = res.strip()
    m = re.search(r'```(?:json)?\s*(.*?)\s*```', s, re.DOTALL)
    if m:
        s = m.group(1)
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end <= start:
        return None, "ai_failed", None
    try:
        data = json.loads(s[start:end + 1])
    except Exception:
        return None, "ai_failed", None
    if not isinstance(data, dict):
        return None, "ai_failed", None
    name = str(data.get("name") or "").strip()
    url = str(data.get("url") or "").strip().rstrip(".,)\"'’")
    api_key = str(data.get("key") or "").strip()
    desc = str(data.get("description") or "").strip()
    method = str(data.get("method") or "POST").strip().upper() or "POST"
    if method not in ("GET", "POST"):
        method = "POST"
    if not url:
        # تور ایمنی: اگر کاربر آدرس را مستقیم در متن نوشته ولی AI جا انداخته، همان را بگیر
        m2 = re.search(r'https?://\S+', text)
        if m2:
            url = m2.group(0).rstrip(".,)\"'’")
    if not url:
        return None, "no_url", None
    if not name:
        name = _derive_tool_name(desc or text)
    else:
        name = re.sub(r'_+', '_', re.sub(r'[^\w-]', '_', name, flags=re.UNICODE)).strip('_-')[:40].strip('_-')
        if not name:
            name = _derive_tool_name(desc or text)
    return {"name": name, "url": url, "api_key": api_key, "method": method, "desc": desc}, None, None

async def _save_custom_tool(event, name, url, api_key, method, desc):
    """اعتبارسنجی + ثبت ابزار در config.json + پیام موفقیت — مسیر مشترک قالب pipe و توضیح آزاد."""
    if not re.match(r'^[\w-]+$', name, re.UNICODE) or len(name) > 40:
        return await event.edit(_t("❌ نام ابزار نامعتبر است. فقط حروف، عدد، _ و - (حداکثر ۴۰ کاراکتر).", "❌ Invalid tool name. Letters, digits, _ and - only (max 40 chars).", "❌ نام ابزار نامعتبر | Invalid tool name"))
    if name in BUILTIN_COMMANDS and name != "image":
        return await event.edit(_t(f"❌ نام «{name}» یک دستور داخلی است؛ نام دیگری انتخاب کن.", f"❌ «{name}» is a built-in command; pick another name.", f"❌ نام داخلی است | Built-in name — نام دیگری انتخاب کن: `{name}`"))
    if not (url.startswith("http://") or url.startswith("https://")) or len(url) > 500:
        return await event.edit(_t("❌ آدرس ابزار باید با http:// یا https:// شروع شود.", "❌ Tool URL must start with http:// or https://.", "❌ آدرس نامعتبر | Invalid URL"))
    if method not in ("GET", "POST"):
        return await event.edit(_t("❌ متد باید GET یا POST باشد.", "❌ Method must be GET or POST.", "❌ متد نامعتبر | Invalid method"))
    custom_tools = config.get("custom_tools", {}) or {}
    existed = name in custom_tools
    custom_tools[name] = {"url": url, "api_key": api_key, "method": method, "desc": desc[:200]}
    config["custom_tools"] = custom_tools
    save_config(config)
    note = " (جایگزین ابزار قبلی با همین نام شد)" if existed else ""
    await event.edit(_t(f"✅ ابزار «{name}» ثبت شد{note}.\n⊳ آدرس: `{url}`\n⊳ متد: `{method}`\n⊳ توضیح: {desc or '—'}\nحالا با `.{name} <ورودی>` صدایش بزن — در `.راهنما` هم اضافه شد.", f"✅ Tool «{name}» registered{' (replaced the previous tool with this name)' if existed else ''}.\n⊳ URL: `{url}`\n⊳ Method: `{method}`\n⊳ Description: {desc or '—'}\nCall it with `.{name} <input>` — it was added to `.راهنما` too.", f"✅ ❲ ابزار ثبت شد | Tool Registered ❳\n⊳ نام | Name: `{name}`{note}\n⊳ متد | Method: `{method}`\n⊳ توضیح | Description: {desc or '—'}\n⊳ با `.{name} <ورودی>` صدایش بزن | در `.راهنما` اضافه شد"))

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.tool add (.*)$'))
async def add_custom_tool(event):
    raw = event.pattern_match.group(1).strip()
    if "|" in raw:
        # قالب قدیمی pipe — بدون تغییر در تجزیه
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            return await event.edit(_t("❌ قالب درست:\n`.tool add نام | آدرس | کلید | توضیح`\n(کلید و توضیح اختیاری‌اند؛ برای متد GET یک بخش پنجم `| GET` اضافه کن)", "❌ Correct format:\n`.tool add name | url | key | description`\n(key and description are optional; add a fifth `| GET` segment for the GET method)", "❌ قالب درست | Format: `.tool add نام | آدرس | کلید | توضیح`"))
        name, url = parts[0], parts[1]
        api_key = parts[2] if len(parts) > 2 else ""
        desc = parts[3] if len(parts) > 3 else ""
        method = parts[4].upper() if len(parts) > 4 else "POST"
        if api_key == "-":
            api_key = ""
        return await _save_custom_tool(event, name, url, api_key, method, desc)
    # قالب جدید: توضیح آزاد — AI خودش نام، آدرس، کلید، توضیح و متد را درمی‌آورد
    if not raw:
        return await event.edit(_t("❌ قالب درست:\n`.tool add نام | آدرس | کلید | توضیح`\nیا فقط توضیح بده، مثلاً:\n`.tool add یه API برای ساخت عکس، آدرسش https://api.example.com و کلیدش ...`\n(AI خودش نام، آدرس، کلید و متد را درمی‌آورد.)", "❌ Correct format:\n`.tool add name | url | key | description`\nor just describe it, e.g.:\n`.tool add an API for image generation, its URL is https://api.example.com and its key is ...`\n(The AI extracts the name, URL, key and method itself.)", "❌ قالب درست | Format: `.tool add نام | آدرس | کلید | توضیح` یا توضیح آزاد"))
    await event.edit(_t("⏳ دارم مشخصات API را از توضیحت درمی‌آورم...", "⏳ Extracting the API details from your description...", "⏳ ❲ در حال بررسی | Extracting ❳..."))
    data, err, err_text = await extract_tool_from_text(raw)
    if err == "ai_failed":
        extra = f"\n{err_text}" if err_text else ""
        return await event.edit(_t(f"❌ نتوانستم مشخصات API را از توضیحت دربیاورم.{extra}\nدوباره با جزئیات بیشتر توضیح بده یا از قالب pipe استفاده کن:\n`.tool add نام | آدرس | کلید | توضیح`", f"❌ I could not extract the API details from your description.{extra}\nDescribe it with more detail or use the pipe format:\n`.tool add name | url | key | description`", f"❌ نتوانستم مشخصات را دربیاورم | Could not extract{extra}\n⊳ قالب pipe: `.tool add نام | آدرس | کلید | توضیح`"))
    if err == "no_url":
        return await event.edit(_t("❌ در توضیحت آدرسی پیدا نکردم و آدرس را نمی‌سازم.\nلطفاً آدرس API را هم بفرست، مثلاً:\n`.tool add ... آدرسش https://api.example.com ...`", "❌ I could not find a URL in your description, and I will not invent one.\nPlease send the API URL as well, e.g.:\n`.tool add ... its URL is https://api.example.com ...`", "❌ آدرسی پیدا نکردم | No URL found — آدرس را نمی‌سازم.\n⊳ آدرس API را هم بفرست، مثلاً: `https://api.example.com`"))
    return await _save_custom_tool(event, data["name"], data["url"], data["api_key"], data["method"], data["desc"])

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.tool list$'))
async def list_custom_tools(event):
    custom_tools = config.get("custom_tools", {}) or {}
    if not custom_tools:
        return await event.edit(_t("🔧 هیچ ابزاری ثبت نشده است.\nبا `.tool add نام | آدرس | کلید | توضیح` یکی اضافه کن.", "🔧 No tools registered.\nAdd one with `.tool add name | url | key | description`.", "🔧 ابزاری ثبت نشده | No tools\n⊳ با `.tool add نام | آدرس | کلید | توضیح` اضافه کن."))
    lines = []
    for name, t in sorted(custom_tools.items()):
        if not isinstance(t, dict):
            continue
        desc = (t.get("desc") or "").strip()
        method = (t.get("method") or "POST").upper()
        lines.append(f"• `.{name} <ورودی>` [{method}]" + (f" — {desc}" if desc else ""))
    await event.edit(_t("🔧 ابزارهای ثبت‌شده:\n" + "\n".join(lines), "🔧 Registered tools:\n" + "\n".join(lines), "🔧 ❲ ابزارهای ثبت‌شده | Registered Tools ❳\n" + "\n".join(lines)))

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.tool del (.+)$'))
async def del_custom_tool(event):
    name = event.pattern_match.group(1).strip()
    custom_tools = config.get("custom_tools", {}) or {}
    if name not in custom_tools:
        return await event.edit(_t(f"❌ ابزار «{name}» پیدا نشد.", f"❌ Tool «{name}» not found.", f"❌ ابزار پیدا نشد | Tool not found: `{name}`"))
    del custom_tools[name]
    config["custom_tools"] = custom_tools
    save_config(config)
    await event.edit(_t(f"✅ ابزار «{name}» حذف شد.", f"✅ Tool «{name}» deleted.", f"✅ ❲ ابزار حذف شد | Tool Deleted ❳\n⊳ `{name}`"))

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.ai(?:\s+(.*))?$'))
async def ai_chat(event):
    prompt = (event.pattern_match.group(1) or "").strip()
    # اگر به عکسی ریپلای شده — عکس را دانلود کن و با مدل بینایی ببین
    # (مدل متنی ask_ai بینایی ندارد؛ عکس از مسیر ask_vision می‌رود)
    reply = await event.get_reply_message() if event.is_reply else None
    if reply is not None and getattr(reply, "photo", None):
        msg = await event.edit(_t("⏳ دانلود عکس و پردازش...", "⏳ Downloading photo and processing...", "⏳ ❲ دانلود عکس | Downloading photo ❳..."))
        photo_path = None
        try:
            try:
                photo_path = await reply.download_media(file=TEMP_DIR + '/')
            except Exception as e:
                logger.error(f"vision download error: {e}")
            if not photo_path:
                return await msg.edit(_t("❌ دانلود عکس ناموفق بود.", "❌ Photo download failed.", "❌ دانلود عکس ناموفق بود | Download failed"))
            try:
                too_big = os.path.getsize(photo_path) > 50 * 1024 * 1024
            except Exception:
                too_big = False
            if too_big:
                return await msg.edit(_t("❌ عکس خیلی بزرگ است (سقف ۵۰ مگابایت).", "❌ Photo too large (50 MB cap).", "❌ عکس بزرگ است | Photo too large (50 MB)"))
            res = await ask_vision(prompt, photo_path)
            if prompt:
                memory_add(event.chat_id, prompt, res)
            return await send_ai_response(event, msg, res)
        finally:
            try:
                if photo_path and os.path.exists(photo_path):
                    os.remove(photo_path)
            except Exception:
                pass
    if not prompt:
        return await event.edit(_t("✍️ بنویس `.ai [سؤال]` — یا روی یک عکس ریپلای کن و بپرس توی عکس چی می‌بینی.", "✍️ Write `.ai [question]` — or reply to a photo and ask what is in it.", "✍️ `.ai [سؤال]` بنویس | یا روی عکس ریپلای کن و بپرس چی می‌بینی"))
    msg = await event.edit(_t("⏳ پردازش...", "⏳ Processing...", "⏳ ❲ در حال پردازش | Processing ❳..."))
    # فقط این مسیر (دستور .ai که کاربر خودش فرستاده) اجازه‌ی خواندن کانال دارد
    res = await ask_ai(prompt, short_answer=False, is_chat=True, chat_id=event.chat_id, allow_channel=True)
    memory_add(event.chat_id, prompt, res)
    await send_ai_response(event, msg, res)

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.فراموش$'))
async def forget_memory_cmd(event):
    ok = memory_clear(event.chat_id)
    if ok:
        await event.edit(_t("🧠 حافظه‌ی این چت پاک شد. از این به بعد AI این گفتگو را از اول می‌شناسد.", "🧠 Memory of this chat was cleared. The AI will treat this conversation as new.", "🧠 حافظه‌ی این چت پاک شد | Memory cleared"))
    else:
        await event.edit(_t("🧠 این چت حافظه‌ای نداشت که پاک شود.", "🧠 This chat had no memory to clear.", "🧠 این چت حافظه‌ای نداشت | No memory to clear"))

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.sch (.*)'))
async def web_search_cmd(event):
    query = event.pattern_match.group(1).strip()
    msg = await event.edit(_t("🔍 جستجو در وب...", "🔍 Searching...", "🔍 ❲ در حال جستجو | Searching ❳..."))
    res = await live_search(query)
    await msg.edit(res)

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.setname (.*)'))
async def setname_cmd(event):
    name = event.pattern_match.group(1).strip()
    config["base_name"] = name; save_config(config)
    await event.edit(_t(f"✅ نام پایه: {name}", f"✅ Base name: {name}", f"✅ ❲ نام پایه تغییر یافت | Base Name Set ❳\n⊳ `{name}`"))

# ==========================================
# بهترین قابلیت‌های دارک‌سلف (تمیز و امن)
# ==========================================
@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.setbio(?:\s+(.+))?$'))
async def setbio_cmd(event):
    text = (event.pattern_match.group(1) or "").strip()
    if len(text) > 70:
        return await event.edit(_t("❌ بیو تلگرام حداکثر ۷۰ کاراکتر است.", "❌ Telegram bio is at most 70 characters.", "❌ بیو طولانی است | Bio too long (max 70)"))
    try:
        await user_client(functions.account.UpdateProfileRequest(about=text))
        if text:
            await event.edit(_t(f"✅ بیو تنظیم شد:\n`{text}`", f"✅ Bio set:\n`{text}`", f"✅ ❲ بیو تنظیم شد | Bio Set ❳\n⊳ `{text}`"))
        else:
            await event.edit(_t("✅ بیو پاک شد.", "✅ Bio cleared.", "✅ ❲ بیو پاک شد | Bio Cleared ❳"))
    except Exception as e:
        await event.edit(f"❌ Error:\n{e}")

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.fake(?:\s+(\d+))?$'))
async def fake_typing_cmd(event):
    seconds = event.pattern_match.group(1)
    try:
        seconds = int(seconds) if seconds else 10
    except: seconds = 10
    seconds = max(1, min(seconds, 60))
    try:
        await event.delete()
        async with user_client.action(event.chat_id, 'typing'):
            await asyncio.sleep(seconds)
    except Exception as e:
        logger.error(f"Fake typing error: {e}")

# ==========================================
# نرخ زنده‌ی ارز — جستجوی زنده (بازار آزاد) + بکاپ API رسمی
# ==========================================
_CURRENCY_FA = {
    "USD": "دلار", "EUR": "یورو", "GBP": "پوند", "AED": "درهم",
    "TRY": "لیر ترکیه", "CHF": "فرانک سوئیس", "CAD": "دلار کانادا",
    "AUD": "دلار استرالیا", "CNY": "یوان چین", "JPY": "ین ژاپن",
    "KRW": "وون کره", "INR": "روپیه هند", "RUB": "روبل روسیه",
    "IQD": "دینار عراق", "AFN": "افغانی",
}
_FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

def _extract_toman_rate(text, keyword):
    """استخراج نرخ تومان از متن جستجو: اولین عدد معتبرِ همراه با «ریال/تومان»
    در جمله‌ای که نام ارز در آن آمده. ریال را به تومان تبدیل می‌کند (÷۱۰).
    عددهای غیرمنطقی (تاریخ، درصد، رقم‌های کوچک) را رد می‌کند."""
    if not text:
        return None
    t = text.translate(_FA_DIGITS)
    pat = re.compile(r"(\d{1,3}(?:[,\u066c]\d{3})+|\d{5,})\s*(ریال|تومان|تومن)")
    # نام ارز باید کمی قبل از عدد آمده باشد (همان جمله) — تقسیم متن به جمله
    # با نقطه اشتباه است چون «۱.۵۸ درصد» هم نقطه دارد و جمله را می‌شکند
    for m in pat.finditer(t):
        window = t[max(0, m.start() - 150):m.start()]
        if keyword not in window:
            continue
        num = int(m.group(1).replace(",", "").replace("٬", ""))
        val = num // 10 if m.group(2) == "ریال" else num
        if 10_000 <= val <= 10_000_000:
            return val
    return None

async def _live_toman_rate(code):
    """نرخ زنده‌ی بازار آزاد یک ارز به تومان، از جستجوی زنده؛ None یعنی پیدا نشد."""
    name = _CURRENCY_FA.get(code)
    if not name:
        return None
    try:
        res = await live_search(f"قیمت {name} امروز")
    except Exception as e:
        logger.error(f"live currency search error ({code}): {e}")
        return None
    if not res or res.startswith("❌"):
        return None
    return _extract_toman_rate(res, name)

async def _fetch_api_rates():
    """بکاپ: نرخ رسمی از exchangerate-api — فقط وقتی جستجوی زنده جواب نداد."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=30) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        rates = data.get("rates") or {}
        if not rates.get("IRR"):
            return None
        return rates
    except Exception as e:
        logger.error(f"currency API fallback error: {e}")
        return None

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.arz(?:\s+([A-Za-z]{3}))?$'))
async def currency_cmd(event):
    code = (event.pattern_match.group(1) or "USD").upper()
    msg = await event.edit(_t("⏳ دریافت نرخ ارز...", "⏳ Fetching rates...", "⏳ ❲ دریافت نرخ | Fetching Rates ❳..."))
    def fmt(n):
        return f"{n:,.0f}"
    try:
        # ۱) اول نرخ زنده‌ی بازار آزاد از جستجوی زنده
        codes = ["USD", "EUR"]
        if code not in codes and code in _CURRENCY_FA:
            codes.append(code)
        live = dict(zip(codes, await asyncio.gather(*[_live_toman_rate(c) for c in codes])))
        usd = live.get("USD")
        if usd:
            lines = []
            lines.append(_t("💱 ❲ نرخ ارز | Currency Rates ❳", "💱 ❲ Currency Rates ❳", "💱 ❲ نرخ ارز | Currency Rates ❳"))
            lines.append("━━━━━━━━━━━━━━━")
            lines.append(_t(f"💵 ۱ دلار: `{fmt(usd)}` تومان", f"💵 1 USD: `{fmt(usd)}` Toman", f"💵 ۱ دلار | 1 USD: `{fmt(usd)}` تومان"))
            if code not in ("USD", "EUR"):
                if live.get(code):
                    lines.append(_t(f"💶 ۱ {code}: `{fmt(live[code])}` تومان", f"💶 1 {code}: `{fmt(live[code])}` Toman", f"💶 ۱ {code} | 1 {code}: `{fmt(live[code])}` تومان"))
                else:
                    lines.append(_t(f"❌ ارز `{code}` پیدا نشد.", f"❌ Currency `{code}` not found.", f"❌ ارز پیدا نشد | Currency not found: `{code}`"))
            eur = live.get("EUR")
            if eur:
                lines.append(_t(f"💶 ۱ یورو: `{fmt(eur)}` تومان", f"💶 1 EUR: `{fmt(eur)}` Toman", f"💶 ۱ یورو | 1 EUR: `{fmt(eur)}` تومان"))
            lines.append(_t("📌 منبع: جستجوی زنده", "📌 Source: live search", "📌 منبع: جستجوی زنده | Live search"))
            return await msg.edit("\n".join(lines))
        # ۲) بکاپ: API رسمی (ممکن است به‌روز نباشد)
        rates = await _fetch_api_rates()
        if not rates:
            return await msg.edit(_t("❌ نرخ ارز در دسترس نیست.", "❌ Currency rate service unavailable.", "❌ نرخ ارز در دسترس نیست | Rate service unavailable"))
        irr = rates["IRR"]
        lines = []
        lines.append(_t("💱 ❲ نرخ ارز | Currency Rates ❳", "💱 ❲ Currency Rates ❳", "💱 ❲ نرخ ارز | Currency Rates ❳"))
        lines.append("━━━━━━━━━━━━━━━")
        lines.append(_t(f"💵 ۱ دلار: `{fmt(irr / 10)}` تومان", f"💵 1 USD: `{fmt(irr / 10)}` Toman", f"💵 ۱ دلار | 1 USD: `{fmt(irr / 10)}` تومان"))
        if code != "USD" and code in rates:
            lines.append(_t(f"💶 ۱ {code}: `{fmt(irr / rates[code] / 10)}` تومان", f"💶 1 {code}: `{fmt(irr / rates[code] / 10)}` Toman", f"💶 ۱ {code} | 1 {code}: `{fmt(irr / rates[code] / 10)}` تومان"))
        elif code != "USD":
            lines.append(_t(f"❌ ارز `{code}` پیدا نشد.", f"❌ Currency `{code}` not found.", f"❌ ارز پیدا نشد | Currency not found: `{code}`"))
        eur = rates.get("EUR")
        if eur:
            lines.append(_t(f"💶 ۱ یورو: `{fmt(irr / eur / 10)}` تومان", f"💶 1 EUR: `{fmt(irr / eur / 10)}` Toman", f"💶 ۱ یورو | 1 EUR: `{fmt(irr / eur / 10)}` تومان"))
        lines.append(_t("⚠️ نرخ ممکن است به‌روز نباشد", "⚠️ Rate may be stale", "⚠️ نرخ ممکن است به‌روز نباشد | May be stale"))
        await msg.edit("\n".join(lines))
    except Exception as e:
        logger.error(f"currency_cmd error: {e}")
        await msg.edit(_t("❌ خطا در دریافت نرخ ارز.", "❌ Rate fetch error.", "❌ خطا در نرخ | Rate error"))

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.symbol(?:\s+(.+))?'))
async def symbol_menu(event):
    choice = event.pattern_match.group(1)
    if choice == '1': 
        config["symbol"] = False; save_config(config)
        await event.edit(_t("❌ نماد خاموش شد.", "❌ Symbol Off.", "❌ ❲ نماد خاموش شد | Symbol Off ❳"))
    elif choice == '2': 
        config["symbol"] = True; save_config(config)
        await event.edit(_t("✅ نماد روشن شد.", "✅ Symbol On.", "✅ ❲ نماد روشن شد | Symbol On ❳"))
    elif choice and re.match(r'^(\d+)[mh]$', choice):
        val, unit = re.findall(r'^(\d+)([mh])$', choice)[0]
        config["symbol_interval"] = int(val) * 60 if unit == 'm' else int(val) * 3600
        save_config(config)
        await event.edit(_t(f"✅ زمان چرخش: {choice}", f"✅ Rotation: {choice}", f"✅ ❲ زمان چرخش نماد | Rotation Interval ❳: {choice}"))
    else: 
        status = "روشن 🟢" if config["symbol"] else "خاموش 🔴"
        mix = f"⚜️ ❲ تنظیمات نماد | Symbol Settings ❳\n━━━━━━━━━━━━━━━\n⊳ وضعیت | Current: {status}\n\n1️⃣ خاموش | Off (`.symbol 1`)\n2️⃣ روشن | On (`.symbol 2`)\n3️⃣ زمان چرخش | Interval (`.symbol 30m`)"
        await event.edit(_t(mix, mix, mix))

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.time(?:\s+(.+))?'))
async def time_menu(event):
    choice = event.pattern_match.group(1)
    if choice == '1': 
        config["time"] = False; save_config(config)
        await event.edit(_t("❌ ساعت خاموش شد.", "❌ Clock Off.", "❌ ❲ ساعت خاموش شد | Clock Off ❳"))
    elif choice == '2': 
        config["time"] = True; save_config(config)
        await event.edit(_t("✅ ساعت روشن شد.", "✅ Clock On.", "✅ ❲ ساعت روشن شد | Clock On ❳"))
    else: 
        status = "روشن 🟢" if config["time"] else "خاموش 🔴"
        mix = f"⌚️ ❲ ساعت پروفایل | Profile Time ❳\n━━━━━━━━━━━━━━━\n⊳ وضعیت | Current: {status}\n\n1️⃣ خاموش | Off (`.time 1`)\n2️⃣ روشن | On (`.time 2`)"
        await event.edit(_t(mix, mix, mix))

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.font(?:\s+(\d+))?'))
async def font_menu(event):
    choice = event.pattern_match.group(1)
    if choice and int(choice) in FONTS:
        config["font_id"] = int(choice); save_config(config)
        preview = "".join(FONTS[int(choice)])
        await event.edit(_t(f"✅ فونت اعمال شد:\n{preview}", f"✅ Font set:\n{preview}", f"✅ ❲ فونت اعمال شد | Font Set ❳\n⊳ `{preview}`"))
    else:
        text = _t("🎨 **لیست فونت‌ها:**\n\n", "🎨 **Fonts List:**\n\n", "🎨 ❲ لیست فونت‌ها | Fonts List ❳\n━━━━━━━━━━━━━━━\n")
        for k, v in FONTS.items(): 
            text += f"**{k}.** `{''.join(v)}`\n"
        await event.edit(text)

# ==========================================
# دانلودر Clipto (اینستاگرام، یوتیوب، X، فیسبوک، تردز، پینترست، لینکدین، بیلیبیلی، بلوسکای، تلگرام، تامبلر، OK.ru، VK)
# ==========================================
CLIPTO_URL = "https://www.clipto.com/api/youtube"
CLIPTO_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://www.clipto.com",
    "Referer": "https://www.clipto.com/media-downloader/all-in-one-video-downloader",
    "User-Agent": "Mozilla/5.0 (Linux; Android 13; SM-A346E) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
}

def _clipto_quality(media):
    """عدد کیفیت از رشته quality (مثلا 1080p -> 1080)؛ برای صوت 0."""
    q = str(media.get("quality", "") or "")
    m = re.search(r'(\d+)', q)
    if m:
        return int(m.group(1))
    if "best" in q.lower() or "high" in q.lower():
        return 9999
    return 0

def _clipto_pick_best(medias):
    """بهترین مدیا: اول ویدیوی mp4 با بالاترین کیفیت، بعد هر ویدیو، بعد صوت."""
    videos = [m for m in medias if (m.get("type") or "").lower() == "video"]
    mp4_videos = [m for m in videos if (m.get("extension") or "").lower() == "mp4"]
    if mp4_videos:
        return max(mp4_videos, key=_clipto_quality)
    if videos:
        return max(videos, key=_clipto_quality)
    audios = [m for m in medias if (m.get("type") or "").lower() == "audio"]
    if audios:
        return audios[0]
    return medias[0] if medias else None

# پسوندهای ویدیویی — فقط این‌ها اتریبیوت ویدیو + faststart می‌گیرند
_VIDEO_EXTS = ('mp4', 'mov', 'm4v', 'mkv', 'webm')
_AUDIO_EXTS = ('mp3', 'm4a', 'ogg', 'oga', 'flac', 'wav', 'opus', 'aac')

def _parse_ffmpeg_probe(text):
    """مدت (ثانیه) و ابعاد را از خروجی `ffmpeg -i` می‌خواند. در خطا 0 برمی‌گرداند."""
    duration = 0
    width = height = 0
    try:
        m = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', text or '')
        if m:
            duration = int(float(m.group(1)) * 3600 + float(m.group(2)) * 60 + float(m.group(3)))
    except Exception:
        duration = 0
    try:
        m2 = re.search(r'Stream #\d+:\d+[^:]*: Video:[^\n]*?(\d{2,5})x(\d{2,5})', text or '')
        if m2:
            width, height = int(m2.group(1)), int(m2.group(2))
    except Exception:
        width = height = 0
    return duration, width, height

def _probe_audio_duration(path):
    """مدت صوت (ثانیه) را از `ffmpeg -i` می‌خواند (همان روش prepare_video_for_streaming).
    در هر خطایی 0 برمی‌گرداند — ارسال هرگز به‌خاطر این تابع نمی‌میرد."""
    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return 0
    if not ffmpeg or not path or not os.path.exists(path):
        return 0
    try:
        proc = subprocess.run([ffmpeg, "-i", path], capture_output=True, text=True, timeout=60)
        dur, _, _ = _parse_ffmpeg_probe((proc.stderr or '') + (proc.stdout or ''))
        return dur or 0
    except Exception:
        return 0

def _sanitize_filename(name):
    """کاراکترهای غیرمجاز در نام فایل را حذف و طول را به ~۱۲۰ کاراکتر محدود می‌کند (با حفظ پسوند)."""
    name = (name or '').strip()
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '', name)
    name = re.sub(r'\s+', ' ', name).strip().strip('. ')
    if not name:
        name = 'file'
    if len(name) > 120:
        base, dot, fext = name.rpartition('.')
        if dot and 0 < len(fext) <= 10:
            name = base[:120 - len(fext) - 1].rstrip('. ') + '.' + fext
        else:
            name = name[:120]
    return name

def _direct_display_name(content_disposition, url, ext):
    """نام نمایشی فایل برای `.دانلود` — به‌ترتیب: (الف) هدر Content-Disposition،
    (ب) نام فایل از خود لینک، (ج) `direct_<زمان>.<پسوند>`."""
    name = None
    cd = content_disposition or ''
    # filename*=UTF-8''... (RFC 5987)
    m = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", cd, re.I)
    if m:
        try:
            name = urllib.parse.unquote(m.group(1).strip().strip('"'))
        except Exception:
            name = None
    if not name:
        m = re.search(r'filename\s*=\s*"([^"]+)"', cd, re.I) or re.search(r'filename\s*=\s*([^;\s]+)', cd, re.I)
        if m:
            name = m.group(1).strip()
    if not name:
        try:
            base = os.path.basename(urllib.parse.urlparse(url or '').path)
            base = urllib.parse.unquote(base)
            if base and '.' in base:
                name = base
        except Exception:
            name = None
    if not name:
        name = f"direct_{int(time.time())}.{ext or 'bin'}"
    name = _sanitize_filename(name)
    if '.' not in os.path.basename(name):
        name = name + '.' + (ext or 'bin')
    return name

def _audio_metadata(display_name):
    """خواننده/عنوان را از نام فایل می‌گیرد: «خواننده - عنوان (320).mp3» → ("خواننده", "عنوان").
    جداسازی روی اولین " - " است؛ پسوند بیت‌ریت انتهایی مثل " (320)" هم حذف می‌شود."""
    stem = os.path.splitext(os.path.basename(display_name or ''))[0]
    stem = re.sub(r'\s*\(\s*\d{2,4}\s*(?:kbps)?\s*\)\s*$', '', stem, flags=re.I)
    performer = ''
    title = stem
    if ' - ' in stem:
        performer, title = stem.split(' - ', 1)
        performer = performer.strip()
        title = title.strip()
    return performer, title

def prepare_video_for_streaming(path):
    """همگام (در thread اجرا می‌شود): ویدیو را برای پخش آنلاین (استریم) آماده می‌کند.
    - مدت/ابعاد را از `ffmpeg -i` می‌خواند (imageio-ffmpeg فقط باینری ffmpeg دارد، ffprobe ندارد)
    - mp4/mov/m4v را با `-movflags +faststart` ریماکس می‌کند (بدون ری‌انکد — فقط اتم moov به ابتدای فایل می‌رود)
    برمی‌گرداند: (مسیر فایل برای ارسال, مدت به ثانیه, عرض, ارتفاع).
    در هر خطایی فایل اصلی با 0 برمی‌گردد — ارسال هرگز به‌خاطر این تابع نمی‌میرد."""
    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return path, 0, 0, 0
    if not ffmpeg or not path or not os.path.exists(path):
        return path, 0, 0, 0
    # ۱) مدت و ابعاد از خروجی ffmpeg -i (کد خروج 1 است چون خروجی ندارد — مهم نیست، stderr خوانده می‌شود)
    duration = width = height = 0
    try:
        proc = subprocess.run([ffmpeg, "-i", path], capture_output=True, text=True, timeout=60)
        duration, width, height = _parse_ffmpeg_probe((proc.stderr or '') + (proc.stdout or ''))
    except Exception:
        duration = width = height = 0
    # ۲) faststart فقط برای کانتینر MP4 (mkv/webm اتم moov ندارند)
    ext = (os.path.splitext(path)[1] or '').lstrip('.').lower()
    if ext not in ('mp4', 'mov', 'm4v'):
        return path, duration, width, height
    base, _ = os.path.splitext(path)
    out = base + ".faststart." + ext
    try:
        proc = subprocess.run([ffmpeg, "-y", "-i", path, "-c", "copy", "-movflags", "+faststart", out],
                              capture_output=True, text=True, timeout=300)
        if proc.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            try:
                os.replace(out, path)
                return path, duration, width, height
            except Exception:
                # جایگزینی نشد — همان فایل ریماکس‌شده را برای ارسال برگردان
                return out, duration, width, height
        try:
            if os.path.exists(out):
                os.remove(out)
        except Exception:
            pass
    except Exception:
        pass
    return path, duration, width, height

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.(sp|in)$'))
async def downloader(event):
    reply = await event.get_reply_message()
    reply_text = reply.text if reply else None
    if not reply or not reply_text or not re.search(r'https?://[^\s]+', reply_text):
        return await event.edit(_t("❌ روی پیامی که لینک دارد ریپلای کن.", "❌ Reply to a message containing a link.", "❌ ریپلای روی لینک | Reply to a message with a link"))
    link = re.search(r'https?://[^\s]+', reply_text).group(0)
    msg = await event.edit(_t("⏳ ارتباط با سرور دانلود...", "⏳ Connecting to download server...", "⏳ ❲ ارتباط با سرور | Connecting ❳..."))
    # اسپاتیفای هرگز به Clipto نمی‌رود (CDN آن 502 می‌دهد) — مسیر مخصوص خودش را دارد
    if extract_spotify_id(link):
        await _handle_spotify(event, msg, link, reply)
        return
    temp_file = None
    send_path = None
    try:
        async with aiohttp.ClientSession() as session:
            # یک درخواست به Clipto — اگر success نباشد، همان خطا را نشان بده و تمام
            async with session.post(CLIPTO_URL, json={"url": link}, headers=CLIPTO_HEADERS, timeout=60) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    return await msg.edit(_t("❌ پاسخ سرور دانلود خوانا نبود.", "❌ Download server returned an unreadable response.", "❌ پاسخ سرور خوانا نبود | Bad server response"))
            if not data.get("success"):
                err_text = data.get("error") or data.get("message") or "نامشخص"
                return await msg.edit(_t(f"❌ دانلود ناموفق:\n{err_text}", f"❌ Download failed:\n{err_text}", f"❌ دانلود ناموفق | Download failed:\n{err_text}"))
            medias = data.get("medias") or []
            if not medias:
                return await msg.edit(_t("❌ برای این لینک فایل قابل دانلودی پیدا نشد.", "❌ No downloadable media found for this link.", "❌ فایل قابل دانلود نیست | No downloadable media"))
            best = _clipto_pick_best(medias)
            if not best or not best.get("url"):
                return await msg.edit(_t("❌ برای این لینک فایل قابل دانلودی پیدا نشد.", "❌ No downloadable media found for this link.", "❌ فایل قابل دانلود نیست | No downloadable media"))
            dl_url = best["url"]
            ext = (best.get("extension") or "").strip().lstrip('.')
            title = data.get("title") or ""

            await msg.edit(_t(f"⏳ دانلود: {title[:60]}...", f"⏳ Downloading: {title[:60]}...", f"⏳ ❲ در حال دانلود | Downloading ❳\n⊳ {title[:60]}"))
            async with session.get(dl_url, headers={"Referer": "https://www.clipto.com/"}, timeout=600) as resp:
                if resp.status != 200:
                    return await msg.edit(_t(f"❌ خطا در دانلود فایل (کد {resp.status}).", f"❌ File download error (code {resp.status}).", f"❌ خطا در دانلود | Download error ({resp.status})"))
                total = int(resp.headers.get('Content-Length', 0))
                ct = resp.headers.get('Content-Type', '')
                if not ext:
                    ext = (mimetypes.guess_extension(ct.split(';')[0]) or '.mp4').lstrip('.')
                temp_file = os.path.join(TEMP_DIR, f"dl_{int(time.time())}.{ext}")
                downloaded = 0
                prog = Progress(msg, _t("دانلود فایل", "Downloading", "دانلود فایل | Downloading"))
                async with aiofiles.open(temp_file, 'wb') as f:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        await f.write(chunk); downloaded += len(chunk); await prog(downloaded, total)

            up_prog = Progress(msg, _t("آپلود در تلگرام", "Uploading", "ارسال به تلگرام | Uploading"))
            caption = title[:200] if title else None
            send_path = temp_file
            video_attrs = None
            if ext.lower() in _VIDEO_EXTS:
                send_path, dur, w, h = await asyncio.to_thread(prepare_video_for_streaming, temp_file)
                video_attrs = [DocumentAttributeVideo(duration=int(dur) if dur else 0, w=w or 0, h=h or 0, supports_streaming=True)]
            if video_attrs:
                await user_client.send_file(event.chat_id, send_path, caption=caption, reply_to=reply, progress_callback=up_prog, attributes=video_attrs)
            else:
                await user_client.send_file(event.chat_id, temp_file, caption=caption, reply_to=reply, progress_callback=up_prog)
            await msg.delete()
    except Exception as e:
        await msg.edit(f"❌ Error:\n{e}")
    finally:
        for p in {temp_file, send_path}:
            if p and os.path.exists(p):
                try: os.remove(p)
                except: pass

# ==========================================
# دانلود اسپاتیفای (متادیتا از oEmbed + صوت از SoundCloud اول، یوتیوب دوم)
# Clipto برای اسپاتیفای 502 می‌دهد، پس لینک اسپاتیفای هرگز به Clipto نمی‌رود.
# yt-dlp و imageio-ffmpeg فقط همین‌جا و به‌صورت تنبل (lazy) ایمپورت می‌شوند،
# تا نبودنشان در زمان راه‌اندازی باعث کرش ربات نشود.
# ==========================================
SPOTIFY_MAX_MB = 50  # سقف حجم فایل — مثل دانلودر مستقیم
_SPOTIFY_RE = re.compile(r'(?:open\.spotify\.com/(?:intl-[a-z]{2}/)?track/([A-Za-z0-9]{22})|spotify:track:([A-Za-z0-9]{22}))')
_SPOTIFY_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

def extract_spotify_id(url):
    """شناسه ترک اسپاتیفای از لینک یا URI — اگر اسپاتیفای نبود None."""
    if not url:
        return None
    m = _SPOTIFY_RE.search(url)
    if not m:
        return None
    return m.group(1) or m.group(2)

def _spotify_clean_title(title, artist):
    """فقط عنوان را تمیز می‌کند — هرگز هنرمند را از عنوان نمی‌سازد.
    اگر انتهای عنوان « — <هنرمند>» باشد (همان هنرمند)، آن پسوند تکراری حذف می‌شود؛
    در غیر این صورت عنوان کامل می‌ماند (مثلاً «Piano Version» بخشی از عنوان است، نه هنرمند)."""
    t = (title or "").strip()
    a = (artist or "").strip()
    if t and a:
        for sep in (" — ", " – ", " - ", " | "):
            if sep in t:
                head, tail = t.rsplit(sep, 1)
                if tail.strip().lower() == a.lower():
                    return head.strip()
    return t

def _spotify_safe_filename(name):
    """نام فایل امن: کاراکترهای غیرمجاز حذف، فاصله‌ها یکی، حداکثر ~۱۰۰ کاراکتر."""
    s = re.sub(r'[/\\:*?"<>|]', '', name or "")
    s = re.sub(r'\s+', ' ', s).strip().strip('. ')
    return s[:100]

def _spotify_parse_embed(html_text):
    """(title, artist, duration_ms) را از HTML صفحه embed استخراج می‌کند.
    اول JSON داخل __NEXT_DATA__ (هنرمند و مدت واقعی)، بعد regex. اگر چیزی پیدا نشد (None, None, None)."""
    title = None
    artist = None
    dur = None
    # ۱) __NEXT_DATA__ JSON
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html_text, re.S)
    if m:
        try:
            data = json.loads(m.group(1))
            entity = data["props"]["pageProps"]["state"]["data"]["entity"]
            title = (entity.get("title") or entity.get("name") or "").strip() or None
            artists = entity.get("artists") or []
            if artists and isinstance(artists[0], dict):
                artist = (artists[0].get("name") or "").strip() or None
            d = entity.get("duration")
            if isinstance(d, (int, float)) and d > 0:
                dur = int(d)
        except Exception:
            pass
    # ۲) regex fallback
    if not artist:
        m2 = re.search(r'"artists":\s*\[\s*\{\s*"name"\s*:\s*"([^"]+)"', html_text)
        if m2:
            artist = m2.group(1)
    if not dur:
        m3 = re.search(r'"duration"\s*:\s*(\d+)', html_text)
        if m3:
            dur = int(m3.group(1))
    if not title:
        m4 = re.search(r'<title[^>]*>([^<]+)</title>', html_text)
        if m4:
            t = re.sub(r'\s*\|\s*Spotify\s*$', '', m4.group(1).strip())
            title = t or None
    return title, artist, dur

async def _spotify_metadata(track_id):
    """(title, artist, duration_ms) — یا (None, None, None) اگر پیدا نشد.
    منبع اصلی: صفحه embed (هنرمند و مدت واقعی را دارد).
    oEmbed فقط fallback برای عنوان است — نه author_name دارد نه مدت."""
    async with aiohttp.ClientSession() as session:
        # ۱) صفحه embed — منبع اصلی
        try:
            async with session.get(f"https://open.spotify.com/embed/track/{track_id}", headers=_SPOTIFY_UA, timeout=20) as resp:
                if resp.status == 200:
                    html_text = await resp.text()
                    title, artist, dur = _spotify_parse_embed(html_text)
                    if title:
                        return _spotify_clean_title(title, artist or ""), artist, dur
        except Exception:
            pass
        # ۲) oEmbed — فقط عنوان (author_name ندارد)
        try:
            async with session.get(f"https://open.spotify.com/oembed?url=https://open.spotify.com/track/{track_id}", headers=_SPOTIFY_UA, timeout=20) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    raw_title = (data.get("title") or "").strip()
                    if raw_title:
                        return _spotify_clean_title(raw_title, ""), "", None
        except Exception:
            pass
    return None, None, None

def _spotify_pick(entries, duration_s):
    """بهترین گزینه از نتایج جستجو: کوتاه‌تر از ۶۰ ثانیه پیش‌نمایش است و حذف می‌شود.
    اگر مدت اسپاتیفای معلوم باشد، اولین نتیجه با اختلاف حداکثر ۱۵ ثانیه؛
    در غیر این صورت اولین نتیجه. اگر هیچ‌کدام در بازه نبود، None."""
    cands = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        d = e.get('duration')
        if isinstance(d, (int, float)) and d < 60:
            continue
        cands.append(e)
    if not cands:
        return None, None
    if duration_s:
        for e in cands:
            d = e.get('duration')
            if isinstance(d, (int, float)) and abs(d - duration_s) <= 15:
                return e, cands[0]
        return None, cands[0]
    return cands[0], cands[0]

def _spotify_ytdlp_run(query, duration_s, temp_base):
    """همگام (در thread اجرا می‌شود): جستجو در SoundCloud بعد یوتیوب، دانلود بهترین تطابق و تبدیل به mp3 با 128k.
    برمی‌گرداند: (مسیر mp3 یا None, کلید خطا یا None)."""
    try:
        from yt_dlp import YoutubeDL
        import imageio_ffmpeg
    except ImportError:
        return None, "no_packages"
    try:
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None, "no_ffmpeg"
    common = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'ffmpeg_location': ffmpeg,
        'format': 'bestaudio/best',
        'outtmpl': temp_base + '.%(ext)s',
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '128'}],
    }
    fallback = None
    picked = None
    for engine in ('scsearch5', 'ytsearch5'):
        try:
            with YoutubeDL(common) as ydl:
                info = ydl.extract_info(f"{engine}:{query}", download=False)
            entries = (info or {}).get('entries') or []
            pick, first = _spotify_pick(entries, duration_s)
            if first and fallback is None:
                fallback = first
            if pick:
                picked = pick
                break
        except Exception:
            continue
    if not picked:
        picked = fallback
    if not picked:
        return None, "no_result"
    url = picked.get('webpage_url') or picked.get('url')
    if not url:
        return None, "no_result"
    try:
        with YoutubeDL(common) as ydl:
            ydl.download([url])
    except Exception:
        return None, "download_failed"
    mp3 = temp_base + '.mp3'
    if os.path.exists(mp3):
        return mp3, None
    # اگر yt-dlp نام فایل را کمی متفاوت گذاشت، فایل صوتی هم‌پیشوند را پیدا کن
    try:
        base = os.path.basename(temp_base)
        for fn in os.listdir(TEMP_DIR):
            if fn.startswith(base + '.') and fn.rsplit('.', 1)[-1].lower() in ('mp3', 'm4a', 'ogg', 'opus', 'webm', 'wav', 'flac', 'aac'):
                return os.path.join(TEMP_DIR, fn), None
    except Exception:
        pass
    return None, "no_file"

async def _spotify_tag_and_rename(mp3_path, artist, title):
    """فایل mp3 را به «<artist> - <title>.mp3» تغییر نام می‌دهد و تگ ID3 (artist/title) می‌زند.
    مسیر فایل نهایی را برمی‌گرداند؛ اگر تغییر نام/تگ ناموفق بود همان فایل اصلی."""
    base = _spotify_safe_filename(f"{artist} - {title}" if artist else title) or "track"
    final_path = os.path.join(TEMP_DIR, base + ".mp3")
    if os.path.abspath(final_path) == os.path.abspath(mp3_path):
        return mp3_path
    # برخورد نام: یک شمارنده اضافه کن
    n = 1
    while os.path.exists(final_path):
        final_path = os.path.join(TEMP_DIR, f"{base} ({n}).mp3")
        n += 1
    tagged = None
    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        ffmpeg = None
    if ffmpeg:
        tmp_out = final_path + ".tmp.mp3"
        cmd = [ffmpeg, "-y", "-i", mp3_path]
        if artist:
            cmd += ["-metadata", f"artist={artist}"]
        cmd += ["-metadata", f"title={title}", "-c", "copy", tmp_out]
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            rc = await proc.wait()
            if rc == 0 and os.path.exists(tmp_out) and os.path.getsize(tmp_out) > 0:
                tagged = tmp_out
        except Exception:
            tagged = None
    if tagged:
        try:
            os.replace(tagged, final_path)
        except Exception:
            tagged = None
            try: os.remove(tmp_out)
            except: pass
    if tagged:
        try: os.remove(mp3_path)
        except: pass
        return final_path
    # تگ نشد — فقط تغییر نام
    try:
        os.rename(mp3_path, final_path)
        return final_path
    except Exception:
        return mp3_path

async def _handle_spotify(event, msg, link, reply):
    """دانلود یک ترک اسپاتیفای و ارسال آن به‌صورت فایل mp3. همیشه True برمی‌گرداند
    (لینک اسپاتیفای بود و رسیدگی شد — موفق یا با پیام خطای فارسی)."""
    track_id = extract_spotify_id(link)
    if not track_id:
        return False
    title, artist, duration_ms = await _spotify_metadata(track_id)
    if not title:
        await msg.edit(_t("❌ اطلاعات این آهنگ از اسپاتیفای پیدا نشد.", "❌ Could not get this track's info from Spotify.", "❌ اطلاعات آهنگ پیدا نشد | No Spotify metadata"))
        return True
    disp = f"{artist} — {title}" if artist else title
    await msg.edit(_t(f"⏳ جستجوی آهنگ: {disp[:80]}...", f"⏳ Searching for: {disp[:80]}...", f"⏳ ❲ جستجوی آهنگ | Searching ❳\n⊳ {disp[:80]}"))
    temp_base = os.path.join(TEMP_DIR, f"sp_{int(time.time())}")
    duration_s = (duration_ms / 1000.0) if duration_ms else None
    query = f"{artist} {title}".strip() if artist else title
    mp3_path, err = await asyncio.to_thread(_spotify_ytdlp_run, query, duration_s, temp_base)
    if err == "no_packages":
        await msg.edit(_t("❌ پکیج‌های دانلود (yt-dlp و imageio-ffmpeg) روی هاست نصب نیستند.", "❌ Download packages (yt-dlp and imageio-ffmpeg) are not installed on the host.", "❌ پکیج دانلود نصب نیست | Missing download packages"))
        return True
    if err == "no_ffmpeg":
        await msg.edit(_t("❌ ffmpeg پیدا نشد.", "❌ ffmpeg not found.", "❌ ffmpeg پیدا نشد | No ffmpeg"))
        return True
    if err == "no_result" or not mp3_path or not os.path.exists(mp3_path):
        # فایل‌های نصفه‌ی احتمالی را پاک کن
        try:
            base = os.path.basename(temp_base)
            for fn in os.listdir(TEMP_DIR):
                if fn.startswith(base + '.'):
                    try: os.remove(os.path.join(TEMP_DIR, fn))
                    except: pass
        except Exception:
            pass
        await msg.edit(_t("❌ آهنگی برای این ترک پیدا نشد.", "❌ No matching audio found for this track.", "❌ آهنگ پیدا نشد | No audio found"))
        return True
    # تغییر نام به «هنرمند — آهنگ» + تگ ID3، قبل از ارسال
    send_path = await _spotify_tag_and_rename(mp3_path, artist, title)
    if os.path.getsize(send_path) > SPOTIFY_MAX_MB * 1024 * 1024:
        for p in {mp3_path, send_path}:
            try: os.remove(p)
            except: pass
        await msg.edit(_t(f"❌ فایل بزرگ‌تر از {SPOTIFY_MAX_MB} مگابایت است.", f"❌ File is larger than {SPOTIFY_MAX_MB} MB.", f"❌ فایل خیلی بزرگ است | Too large (>{SPOTIFY_MAX_MB} MB)"))
        return True
    try:
        up_prog = Progress(msg, _t("آپلود در تلگرام", "Uploading", "ارسال به تلگرام | Uploading"))
        caption = f"🎵 {disp}"
        await user_client.send_file(event.chat_id, send_path, caption=caption, reply_to=reply, progress_callback=up_prog)
        await msg.delete()
    except Exception as e:
        await msg.edit(f"❌ Error:\n{e}")
    finally:
        for p in {mp3_path, send_path}:
            try: os.remove(p)
            except: pass
    return True

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.spotify(?:\s+(\S+))?$'))
async def spotify_cmd(event):
    arg = event.pattern_match.group(1)
    url = arg
    reply = await event.get_reply_message()
    if not url and reply and reply.text:
        m = re.search(r'https?://[^\s]+|spotify:track:[A-Za-z0-9]+', reply.text)
        if m:
            url = m.group(0)
    if not url or not extract_spotify_id(url):
        return await event.edit(_t("❌ لینک آهنگ اسپاتیفای را بده.\n`.spotify https://open.spotify.com/track/...`", "❌ Give a Spotify track link.\n`.spotify https://open.spotify.com/track/...`", "❌ لینک اسپاتیفای بده | Give a Spotify link\n`.spotify https://open.spotify.com/track/...`"))
    msg = await event.edit(_t("⏳ ارتباط با اسپاتیفای...", "⏳ Connecting to Spotify...", "⏳ ❲ ارتباط با اسپاتیفای | Connecting ❳..."))
    await _handle_spotify(event, msg, url, reply)

# ==========================================
# تبدیل متن به ویس — بدون مدل لوکال؛ cilla-mini روی هاست جا نمی‌شود.
# فارسی → edge-tts (صدای fa-IR-DilaraNeural)؛ انگلیسی/بقیه → gTTS.
# (gTTS فارسی ندارد — حتی با lang_check=False گوگل «Unsupported language» می‌دهد.)
# edge-tts و gTTS و imageio-ffmpeg فقط همین‌جا و به‌صورت تنبل (lazy) ایمپورت می‌شوند،
# تا نبودنشان در زمان راه‌اندازی باعث کرش ربات نشود.
# ==========================================
TTS_MAX_CHARS = 500  # سقف طول متن
_TTS_FA_RE = re.compile(r'[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]')

def _tts_lang(text):
    """زبان ویس: اگر متن هیچ حرف فارسی/عربی نداشت انگلیسی، وگرنه فارسی."""
    return 'fa' if _TTS_FA_RE.search(text) else 'en'

def _tts_make_voice(text, lang, temp_base):
    """همگام (در thread اجرا می‌شود): متن را به mp3 تبدیل می‌کند،
    بعد با ffmpeg به OGG Opus (48kHz، مناسب ویس تلگرام) تبدیل می‌کند.
    فارسی → edge-tts (fa-IR-DilaraNeural)؛ بقیه → gTTS.
    برمی‌گرداند: (مسیر ogg یا None, کلید خطا یا None)."""
    mp3_path = temp_base + '.mp3'
    ogg_path = temp_base + '.ogg'
    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None, "no_ffmpeg"
    if not ffmpeg:
        return None, "no_ffmpeg"
    # ۱) متن → mp3
    if lang == 'fa':
        # فارسی: gTTS فارسی ندارد — با edge-tts و صدای fa-IR-DilaraNeural ساخته می‌شود.
        # edge-tts ناهمگام است؛ این تابع در thread اجرا می‌شود پس asyncio.run() امن است.
        try:
            import edge_tts
        except ImportError:
            return None, "edge_failed"
        try:
            asyncio.run(edge_tts.Communicate(text, "fa-IR-DilaraNeural").save(mp3_path))
        except Exception:
            return None, "edge_failed"
        if not os.path.exists(mp3_path) or os.path.getsize(mp3_path) == 0:
            return None, "edge_failed"
    else:
        try:
            from gtts import gTTS
        except ImportError:
            return None, "no_gtts"
        try:
            gTTS(text=text, lang=lang).save(mp3_path)
        except Exception:
            return None, "gtts_failed"
        if not os.path.exists(mp3_path) or os.path.getsize(mp3_path) == 0:
            return None, "gtts_failed"
    # ۲) mp3 → ogg Opus 48kHz (ویس تلگرام)
    try:
        proc = subprocess.run([ffmpeg, "-y", "-i", mp3_path,
                               "-c:a", "libopus", "-b:a", "64k",
                               "-ar", "48000", "-ac", "1", ogg_path],
                              capture_output=True, text=True, timeout=120)
    except Exception:
        return None, "ffmpeg_failed"
    try:
        if os.path.exists(mp3_path):
            os.remove(mp3_path)
    except Exception:
        pass
    if proc.returncode == 0 and os.path.exists(ogg_path) and os.path.getsize(ogg_path) > 0:
        return ogg_path, None
    return None, "ffmpeg_failed"

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.(?:tts|ویس)(?:\s+([\s\S]+))?$'))
async def tts_cmd(event):
    text = (event.pattern_match.group(1) or "").strip()
    reply = await event.get_reply_message()
    if not text and reply and reply.text:
        text = reply.text.strip()
    if not text:
        return await event.edit(_t("❌ متنی برای تبدیل به ویس بده.\n`.tts سلام چطوری`", "❌ Give text to convert to voice.\n`.tts hello how are you`", "❌ متن بده | Give text\n`.tts سلام چطوری`"))
    if len(text) > TTS_MAX_CHARS:
        return await event.edit(_t(f"❌ متن خیلی طولانیه — حداکثر {TTS_MAX_CHARS} کاراکتر.", f"❌ Text too long — max {TTS_MAX_CHARS} characters.", f"❌ متن طولانیه | Too long — max {TTS_MAX_CHARS} chars"))
    msg = await event.edit(_t("⏳ ساخت ویس...", "⏳ Generating voice...", "⏳ ❲ ساخت ویس | Generating voice ❳..."))
    lang = _tts_lang(text)
    temp_base = os.path.join(TEMP_DIR, f"tts_{int(time.time())}")
    ogg_path, err = await asyncio.to_thread(_tts_make_voice, text, lang, temp_base)
    try:
        if err == "no_gtts":
            await msg.edit(_t("❌ پکیج تبدیل متن به ویس (gTTS) روی هاست نصب نیست.", "❌ Text-to-speech package (gTTS) is not installed on the host.", "❌ پکیج ویس نصب نیست | Missing gTTS package"))
            return
        if err == "no_ffmpeg":
            await msg.edit(_t("❌ ffmpeg پیدا نشد.", "❌ ffmpeg not found.", "❌ ffmpeg پیدا نشد | No ffmpeg"))
            return
        if err == "edge_failed":
            await msg.edit(_t("❌ ساخت ویس ناموفق بود.", "❌ Voice generation failed.", "❌ ساخت ویس نشد | Voice failed"))
            return
        if err or not ogg_path or not os.path.exists(ogg_path):
            await msg.edit(_t("❌ ساخت ویس ناموفق بود.", "❌ Voice generation failed.", "❌ ساخت ویس نشد | Voice failed"))
            return
        await user_client.send_file(event.chat_id, ogg_path, voice_note=True, reply_to=reply)
        await msg.delete()
    except Exception as e:
        await msg.edit(f"❌ Error:\n{e}")
    finally:
        for p in (temp_base + '.mp3', temp_base + '.ogg'):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass

# سقف حجم هر فایل در .for — مثل .دانلود
FOR_MAX_MB = 50

def _for_albumable(m):
    """True اگر مدیای پیام بتواند جزو یک آلبوم تلگرام باشد (عکس / ویدیوی معمولی)."""
    try:
        if getattr(m, 'photo', None) is not None:
            return True
        doc = getattr(m, 'document', None)
        if doc is None:
            return False
        for a in (getattr(doc, 'attributes', None) or []):
            aname = type(a).__name__
            if aname in ('DocumentAttributeSticker', 'DocumentAttributeAudio'):
                return False
            if aname == 'DocumentAttributeVideo' and getattr(a, 'round_message', False):
                return False
        mt = (getattr(doc, 'mime_type', '') or '').lower()
        if mt.startswith('video/') or mt.startswith('image/'):
            return True
        for a in (getattr(doc, 'attributes', None) or []):
            if type(a).__name__ == 'DocumentAttributeVideo':
                return True
        return False
    except Exception:
        return False


async def _for_group_messages(chat, target_id, target_msg):
    """همه‌ی پیام‌های گروه رسانه‌ای (آلبوم) پیام هدف را برگردان (مرتب بر اساس id)؛ اگر گروه نبود فقط خود پیام."""
    grouped_id = getattr(target_msg, 'grouped_id', None)
    if not grouped_id:
        return [target_msg]
    ids = list(range(max(1, target_id - 10), target_id + 11))
    try:
        msgs = await user_client.get_messages(chat, ids=ids)
    except Exception:
        return [target_msg]
    if not msgs:
        return [target_msg]
    if not isinstance(msgs, list):
        msgs = [msgs]
    members = [m for m in msgs if m is not None and getattr(m, 'grouped_id', None) == grouped_id]
    if not members:
        return [target_msg]
    if all(m.id != target_id for m in members):
        members.append(target_msg)
    members.sort(key=lambda m: m.id)
    return members


@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.for (https://t\.me/(c/)?([a-zA-Z0-9_]+)/(\d+))(?:[?&]comment=(\d+))?'))
async def forward_restricted(event):
    link, msg_id = event.pattern_match.group(1), int(event.pattern_match.group(4))
    comment_id = event.pattern_match.group(5)
    comment_id = int(comment_id) if comment_id else None
    chat = int('-100' + event.pattern_match.group(3)) if '/c/' in link else event.pattern_match.group(3)
    msg = await event.edit(_t("⏳ در حال دریافت...", "⏳ Fetching...", "⏳ ❲ در حال استخراج | Fetching ❳..."))
    paths = []
    try:
        if comment_id is not None:
            # لینک کامنت است: کامنت در گروه بحث (discussion group) کانال زندگی می‌کند، نه در خود کانال
            full = await user_client(functions.channels.GetFullChannelRequest(chat))
            linked_id = full.full_chat.linked_chat_id
            if not linked_id:
                return await msg.edit(_t("❌ این کانال گروه بحث (کامنت) ندارد؛ کامنت پیدا نشد.", "❌ This channel has no linked discussion group; comment not found.", "❌ گروه بحث ندارد | No discussion group — کامنت پیدا نشد"))
            target_msg = await user_client.get_messages(linked_id, ids=comment_id)
            if not target_msg:
                return await msg.edit(_t("❌ کامنت موردنظر پیدا نشد.", "❌ Comment not found.", "❌ کامنت پیدا نشد | Comment not found"))
            target_chat, target_id = linked_id, comment_id
        else:
            target_msg = await user_client.get_messages(chat, ids=msg_id)
            target_chat, target_id = chat, msg_id
        if not target_msg:
            return await msg.edit(_t("❌ پیام پیدا نشد.", "❌ Message not found.", "❌ پیام پیدا نشد | Message not found"))
        # کل پست: اگر عضو یک گروه رسانه‌ای (آلبوم) است، همه‌ی اعضایش را بگیر
        msgs = await _for_group_messages(target_chat, target_id, target_msg)
        media_msgs = [m for m in msgs if getattr(m, 'media', None)]
        if not media_msgs:
            # فقط متن — مثل قبل
            return await msg.edit(f"📄 ❲ کپی شد | Copied ❳\n━━━━━━━━━━━━━━━\n{target_msg.text}")
        caption = next((m.text for m in msgs if getattr(m, 'text', None)), None)
        album_paths, single_paths = [], []
        skipped = 0
        for m in media_msgs:
            try:
                size = getattr(getattr(m, 'file', None), 'size', None)
            except Exception:
                size = None
            if size and size > FOR_MAX_MB * 1024 * 1024:
                skipped += 1
                continue
            p = await m.download_media(file=TEMP_DIR + '/')
            if not p:
                continue
            if os.path.getsize(p) > FOR_MAX_MB * 1024 * 1024:
                try: os.remove(p)
                except: pass
                skipped += 1
                continue
            paths.append(p)
            (album_paths if _for_albumable(m) else single_paths).append(p)
        if not paths:
            return await msg.edit(_t(f"❌ فایل‌ها بزرگ‌تر از {FOR_MAX_MB} مگابایت‌اند و دانلود نمی‌شوند.", f"❌ Files are larger than {FOR_MAX_MB} MB and will not be downloaded.", f"❌ فایل خیلی بزرگ است | Too large (>{FOR_MAX_MB} MB)"))
        if len(paths) == 1:
            # فقط یک فایل — دقیقاً رفتار قبلی
            await user_client.send_file(event.chat_id, paths[0], caption=caption)
        else:
            if album_paths:
                if len(album_paths) == 1:
                    await user_client.send_file(event.chat_id, album_paths[0], caption=caption)
                else:
                    await user_client.send_file(event.chat_id, album_paths, caption=caption)
            for p in single_paths:
                await user_client.send_file(event.chat_id, p)
        if skipped:
            await msg.edit(_t(f"⚠️ {skipped} فایل به‌خاطر حجم بالای {FOR_MAX_MB} مگابایت رد شد؛ بقیه فرستاده شد.", f"⚠️ {skipped} file(s) skipped (over {FOR_MAX_MB} MB); the rest were sent.", f"⚠️ {skipped} فایل رد شد | Skipped (over {FOR_MAX_MB} MB) — بقیه فرستاده شد"))
        else:
            await msg.delete()
    except Exception as e: await msg.edit(f"❌ Error:\n{e}")
    finally:
        for p in paths:
            if p and os.path.exists(p):
                try: os.remove(p)
                except: pass

# ==========================================
# دانلود مستقیم فایل (لینک مستقیم — نه لینک صفحه شبکه اجتماعی)
# ==========================================
DIRECT_MAX_MB = 50

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.دانلود(?:\s+(https?://\S+))?$'))
async def direct_download(event):
    arg_url = event.pattern_match.group(1)
    url = arg_url
    reply = await event.get_reply_message()
    if not url and reply and reply.text:
        m = re.search(r'https?://[^\s]+', reply.text)
        if m:
            url = m.group(0)
    if not url:
        return await event.edit(_t("❌ لینک مستقیم را بده یا روی پیامی که لینک دارد ریپلای کن.\n`.دانلود https://example.com/video.mp4`", "❌ Give a direct link or reply to a message containing one.\n`.دانلود https://example.com/video.mp4`", "❌ لینک مستقیم بده | Give a direct link\n`.دانلود https://example.com/video.mp4`"))
    msg = await event.edit(_t("⏳ بررسی لینک...", "⏳ Checking link...", "⏳ ❲ بررسی لینک | Checking Link ❳..."))
    temp_file = None
    send_path = None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=60, allow_redirects=True) as resp:
                if resp.status != 200:
                    return await msg.edit(_t(f"❌ خطا در دریافت فایل (کد {resp.status}).", f"❌ File fetch error (code {resp.status}).", f"❌ خطا در دریافت | Fetch error ({resp.status})"))
                total = int(resp.headers.get('Content-Length', 0))
                if total and total > DIRECT_MAX_MB * 1024 * 1024:
                    return await msg.edit(_t(f"❌ فایل بزرگ‌تر از {DIRECT_MAX_MB} مگابایت است و دانلود نمی‌شود.", f"❌ File is larger than {DIRECT_MAX_MB} MB and will not be downloaded.", f"❌ فایل خیلی بزرگ است | Too large (>{DIRECT_MAX_MB} MB)"))
                ct = (resp.headers.get('Content-Type', '') or '').split(';')[0].strip()
                # پسوند از Content-Type، در غیر این صورت از خود لینک
                ext = (mimetypes.guess_extension(ct) or '').lstrip('.')
                if not ext:
                    path_part = urllib.parse.urlparse(str(resp.url)).path
                    base = os.path.basename(path_part)
                    if '.' in base:
                        ext = base.rsplit('.', 1)[-1].split('?')[0][:10] or 'bin'
                    else:
                        ext = 'bin'
                display_name = _direct_display_name(resp.headers.get('Content-Disposition'), str(resp.url), ext)
                temp_file = os.path.join(TEMP_DIR, display_name)
                # اگر فایلی با همین نام هست (دانلود هم‌زمان همان لینک)، نام یکتا بساز
                if os.path.exists(temp_file):
                    base, dot, fext = display_name.rpartition('.')
                    n = 1
                    while os.path.exists(temp_file):
                        n += 1
                        display_name = f"{base} ({n}).{fext}" if dot else f"{display_name} ({n})"
                        temp_file = os.path.join(TEMP_DIR, display_name)
                ext = (os.path.splitext(display_name)[1] or '').lstrip('.').lower() or ext
                downloaded = 0
                prog = Progress(msg, _t("دانلود فایل", "Downloading", "دانلود فایل | Downloading"))
                async with aiofiles.open(temp_file, 'wb') as f:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        await f.write(chunk)
                        downloaded += len(chunk)
                        if downloaded > DIRECT_MAX_MB * 1024 * 1024:
                            return await msg.edit(_t(f"❌ فایل بزرگ‌تر از {DIRECT_MAX_MB} مگابایت است و دانلود نمی‌شود.", f"❌ File is larger than {DIRECT_MAX_MB} MB and will not be downloaded.", f"❌ فایل خیلی بزرگ است | Too large (>{DIRECT_MAX_MB} MB)"))
                        await prog(downloaded, total)
            up_prog = Progress(msg, _t("آپلود در تلگرام", "Uploading", "ارسال به تلگرام | Uploading"))
            # نوع فایل از روی پسوند/مایم تشخیص داده می‌شود (ویدیو، عکس، صوت یا سند)
            send_path = temp_file
            video_attrs = None
            audio_attrs = None
            if ext.lower() in _VIDEO_EXTS:
                send_path, dur, w, h = await asyncio.to_thread(prepare_video_for_streaming, temp_file)
                if os.path.getsize(send_path) > DIRECT_MAX_MB * 1024 * 1024:
                    return await msg.edit(_t(f"❌ فایل بزرگ‌تر از {DIRECT_MAX_MB} مگابایت است و دانلود نمی‌شود.", f"❌ File is larger than {DIRECT_MAX_MB} MB and will not be downloaded.", f"❌ فایل خیلی بزرگ است | Too large (>{DIRECT_MAX_MB} MB)"))
                video_attrs = [DocumentAttributeVideo(duration=int(dur) if dur else 0, w=w or 0, h=h or 0, supports_streaming=True)]
            elif ext.lower() in _AUDIO_EXTS:
                # صوت را مثل موزیک می‌فرستیم (با خواننده/عنوان و مدت) تا تلگرام آن را موزیک نشان بدهد
                dur = await asyncio.to_thread(_probe_audio_duration, temp_file)
                performer, title = _audio_metadata(display_name)
                audio_attrs = [DocumentAttributeAudio(duration=int(dur) if dur else 0, title=title, performer=performer)]
            if video_attrs:
                await user_client.send_file(event.chat_id, send_path, reply_to=reply, progress_callback=up_prog, attributes=video_attrs)
            elif audio_attrs:
                await user_client.send_file(event.chat_id, send_path, reply_to=reply, progress_callback=up_prog, attributes=audio_attrs)
            else:
                await user_client.send_file(event.chat_id, temp_file, reply_to=reply, progress_callback=up_prog)
            await msg.delete()
    except Exception as e:
        await msg.edit(f"❌ Error:\n{e}")
    finally:
        for p in {temp_file, send_path}:
            if p and os.path.exists(p):
                try: os.remove(p)
                except: pass

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.sum$'))
async def summarize_cmd(event):
    reply = await event.get_reply_message()
    if not reply or not reply.text: return await event.edit("❌")
    msg = await event.edit(_t("⏳ خلاصه‌سازی...", "⏳ Summarizing...", "⏳ ❲ در حال خلاصه سازی | Summarizing ❳..."))
    res = await ask_ai(f"یک خلاصه کوتاه و بسیار مفید از این متن ارائه بده (بدون سلام، خداحافظی یا اضافه گویی):\n\n{reply.text}", short_answer=False, is_chat=False)
    header = _t("📌 **خلاصه:**\n\n", "📌 **Summary:**\n\n", "📌 ❲ خلاصه متن | Summary ❳\n━━━━━━━━━━━━━━━\n")
    await send_type_mode(msg, f"{header}{res}")

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.tr(?:\s+(\w+)\s+(.+))?$'))
async def translate_cmd(event):
    reply = await event.get_reply_message()
    if event.pattern_match.group(1):
        lang, text = event.pattern_match.group(1), event.pattern_match.group(2)
        res = await ask_ai(f"فقط متن زیر را به زبان {lang} ترجمه کن (بدون هیچ توضیح و اضافه گویی):\n\n{text}", short_answer=False, is_chat=False)
        await event.delete()
        if reply: await reply.reply(res)
        else: await user_client.send_message(event.chat_id, res)
    elif reply:
        await event.edit(_t("⏳ ترجمه...", "⏳ Translating...", "⏳ ❲ در حال ترجمه | Translating ❳..."))
        res = await ask_ai(f"فقط متن زیر را به فارسی (یا انگلیسی در صورت فارسی بودن) روان ترجمه کن (بدون توضیحات اضافه):\n\n{reply.text}", short_answer=False, is_chat=False)
        await event.delete(); await reply.reply(res)

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.post(?:\s+(\d+))?$'))
async def post_count_cmd(event):
    count = event.pattern_match.group(1)
    if count:
        config["news_post_count"] = int(count)
        save_config(config)
        await event.edit(_t(f"✅ تعداد پست خبر: {count}", f"✅ News limit: {count}", f"✅ ❲ محدودیت اخبار | News Limit ❳: {count}"))
    else:
        curr = config.get("news_post_count", 5)
        await event.edit(_t(f"📰 تعداد فعلی: {curr}", f"📰 Current limit: {curr}", f"📰 ❲ تعداد پست‌های خبر | News Post Limit ❳\n⊳ Current: `{curr}`"))

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.خبر(?:\s+(کوتاه))?$'))
async def news_cmd(event):
    is_short = bool(event.pattern_match.group(1))
    msg = await event.edit(_t("⏳ دریافت اخبار...", "⏳ Fetching news...", "⏳ ❲ جمع‌آوری اخبار | Fetching News ❳..."))
    try:
        limit_count = 5 if is_short else config.get("news_post_count", 5)
        messages = await user_client.get_messages('iranintltv', limit=limit_count)
        news_text = "".join([m.text[:250] + "\n" for m in messages if m.text])
        
        if is_short:
            prompt_text = (
                "فقط اخبار زیر را به صورت تیتروار، خلاصه و بسیار کوتاه (هر خبر در یک خط) بنویس.\n"
                "قانون مهم: سلام، خداحافظی، ایموجی متفرقه و نظر شخصی ممنوع! فقط تیتر اخبار.\n\n"
                f"{news_text}"
            )
            header = _t("📰 **اخبار کوتاه:**\n\n", "📰 **Short News:**\n\n", "📰 ❲ اخبار کوتاه | Short News ❳\n━━━━━━━━━━━━━━━\n")
        else:
            prompt_text = (
                "تو یک خبرنگار حرفه‌ای و جذاب هستی. اخبار زیر را به سبک یک «گزارش خبری ژورنالیستی و جذاب» بنویس.\n"
                "قوانین ساختار دقیقاً باید اینگونه باشد:\n"
                "۱. در ابتدا یک تیتر اصلی و جنجالی برای کل گزارش بنویس همراه با ایموجی مرتبط.\n"
                "۲. برای هر خبر، یک تیتر جذاب با ایموجی مناسب بنویس و سپس در یک پاراگراف کامل و خودمونی (اما دقیق) آن را توضیح بده.\n"
                "۳. در انتهای گزارش، حتماً یک بخش با عنوان «خلاصه ماجرا:» قرار بده و در ۲ الی ۳ خط کل اتفاقات را جمع‌بندی کن.\n"
                "۴. از ایموجی‌ها به صورت هوشمندانه در انتهای تیترها استفاده کن.\n"
                "۵. سلام و احوال‌پرسی ممنوع! فقط مستقیم برو سراغ گزارش خبری.\n\n"
                f"{news_text}"
            )
            header = ""

        res = await ask_ai(prompt_text, short_answer=False, is_chat=False)
        final_text = f"{header}{res}"
        
        await send_type_mode(msg, final_text)
    except Exception as e: 
        await msg.edit(f"❌ Error:\n{e}")

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.image (.*)'))
async def generate_image(event):
    prompt = event.pattern_match.group(1).strip()
    msg = await event.edit(_t("🎨 در حال نقاشی...", "🎨 Generating...", "🎨 ❲ تولید تصویر | Generating Image ❳..."))
    try:
        en_prompt = await ask_ai(f"فقط این متن را به انگلیسی ترجمه کن بدون هیچ کلمه اضافه‌ای: {prompt}", short_answer=True, is_chat=False)
        cap = _t(f"🎨 **نقاشی برای:**\n`{prompt}`", f"🎨 **Prompt:**\n`{prompt}`", f"🎨 ❲ خروجی تصویر | Image Result ❳\n━━━━━━━━━━━━━━━\n⊳ `{prompt}`")
        # اگر ابزار image ثبت شده باشد، اول از API خود کاربر استفاده می‌شود
        custom_tools = config.get("custom_tools", {}) or {}
        if "image" in custom_tools:
            tool_result, tool_err = await call_tool("image", en_prompt)
            img_url = extract_image_url(tool_result) if not tool_err else None
            if img_url:
                await user_client.send_file(event.chat_id, img_url, caption=cap)
                await msg.delete()
                return
            # ابزار image جواب قابل‌استفاده نداد — برگشت به پیش‌فرض رایگان
            await msg.edit(_t("⚠️ ابزار image تصویر قابل‌استفاده‌ای برنگرداند؛ با سرویس رایگان ادامه می‌دهم...", "⚠️ The image tool returned no usable image; falling back to the free service...", "⚠️ ابزار image جواب نداد | Image tool failed — ادامه با سرویس رایگان..."))
        safe_prompt = urllib.parse.quote(en_prompt)
        url = f"https://image.pollinations.ai/prompt/{safe_prompt}?width=1024&height=1024&nologo=true"
        await user_client.send_file(event.chat_id, url, caption=cap)
        await msg.delete()
    except Exception as e:
        await msg.edit(f"❌ Error:\n{e}")

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.tagall(?:\s+(.+))?$'))
async def tagall_cmd(event):
    if not event.is_group: return await event.edit("❌")
    msg_text = event.pattern_match.group(1) or "📣"
    await event.edit(_t("⏳ جمع‌آوری اعضا...", "⏳ Gathering members...", "⏳ ❲ لیست‌گیری اعضا | Gathering Members ❳..."))
    mentions = []
    try:
        async for user in user_client.iter_participants(event.chat_id, limit=50):
            if not user.bot and not user.deleted:
                mentions.append(f"<a href='tg://user?id={user.id}'>\u200b</a>")
                
        if not mentions: return await event.edit("❌ Empty.")
            
        chunk_size = 5
        for i in range(0, len(mentions), chunk_size):
            chunk = mentions[i:i + chunk_size]
            await user_client.send_message(event.chat_id, f"{msg_text}\n{''.join(chunk)}", parse_mode='html')
            await asyncio.sleep(2)
        await event.delete()
    except Exception as e: await event.edit(f"❌ Error:\n{e}")

# ==========================================
# دیسک خانه‌ی کاربر (نه دیسک ریشه‌ی سیستم)
# ==========================================
HOME_DISK_TOTAL = 1024 ** 3  # سهمیه دیسک پلن رایگان Alwaysdata: ۱ گیگابایت

async def get_home_disk():
    """(used, total, free) بر حسب بایت برای پوشه‌ی خانه‌ی کاربر.
    اول du -sb، اگر نشد os.walk (بدون دنبال کردن لینک‌های نمادین)."""
    home = os.path.expanduser("~")
    used = None
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                "du", "-sb", home,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL),
            timeout=60)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        used = int(out.split()[0])
    except Exception:
        used = None
    if used is None:
        try:
            total_size = 0
            for root, _dirs, files in os.walk(home):
                for fn in files:
                    try:
                        total_size += os.path.getsize(os.path.join(root, fn))
                    except Exception:
                        pass
            used = total_size
        except Exception:
            used = 0
    total = HOME_DISK_TOTAL
    free = max(0, total - used)
    return used, total, free

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.وضعیت$'))
async def system_status(event):
    msg = await event.edit(_t("⏳ بررسی منابع...", "⏳ Scanning...", "⏳ ❲ اسکن سیستم | System Scan ❳..."))
    try:
        cpu = psutil.Process(os.getpid()).cpu_percent(interval=0.5)
        total_ram = 256 * 1024 * 1024
        used_ram = psutil.Process(os.getpid()).memory_info().rss
        free_ram = total_ram - used_ram if total_ram > used_ram else 0
        mem_percent = round((used_ram / total_ram) * 100, 1)
        disk_used, disk_total, disk_free = await get_home_disk()
        
        mix = f"""💻 ❲ وضعیت سرور | Server Status ❳
━━━━━━━━━━━━━━━━━━━━
🧠 **CPU Usage:** `[▰▰▰▱▱]` `{cpu}%`

📟 **Memory (RAM):**
⊳ Used: `{get_size(used_ram)}` / `{get_size(total_ram)}`
⊳ Load: `{mem_percent}%`

💾 **Storage (Disk):**
⊳ Used: `{get_size(disk_used)}` / `{get_size(disk_total)}`
⊳ Free: `{get_size(disk_free)}`
━━━━━━━━━━━━━━━━━━━━"""
        await msg.edit(mix) 
    except Exception as e: await msg.edit(f"❌ Error:\n{e}")

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.ریست$'))
async def system_reset(event):
    msg = await event.edit(_t("🧹 پاکسازی...", "🧹 Cleaning...", "🧹 ❲ پاکسازی حافظه | Cleaning Memory ❳..."))
    try:
        deleted = 0; freed = 0
        if os.path.exists(TEMP_DIR):
            for fn in os.listdir(TEMP_DIR):
                fp = os.path.join(TEMP_DIR, fn)
                try:
                    freed += os.path.getsize(fp)
                    if os.path.isfile(fp): os.unlink(fp)
                    elif os.path.isdir(fp): shutil.rmtree(fp)
                    deleted += 1
                except: pass
        for root, dirs, files in os.walk('.'):
            for d in dirs:
                if d == '__pycache__':
                    try: shutil.rmtree(os.path.join(root, d))
                    except: pass
        collected = gc.collect()
        mix = f"""✅ ❲ بهینه‌سازی کامل شد | System Optimized ❳
━━━━━━━━━━━━━━━━━━━━
🗑 Files Deleted: `{deleted}`
💾 Space Freed: `{get_size(freed)}`
🧠 RAM Objects Cleared: `{collected}`"""
        await msg.edit(mix)
    except Exception as e: await msg.edit(f"❌ Error:\n{e}")

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.type(?:\s+(\d))?'))
async def type_menu(event):
    choice = event.pattern_match.group(1)
    if choice == '1': 
        config["type_mode"] = False; save_config(config)
        await event.edit("❌ Type mode off.")
    elif choice == '2': 
        config["type_mode"] = True; save_config(config)
        await event.edit("✅ Type mode on.")
    else: 
        status = "روشن 🟢" if config["type_mode"] else "خاموش 🔴"
        mix = f"⌨️ ❲ افکت تایپ | Typing Effect ❳\n━━━━━━━━━━━━━━━\n⊳ وضعیت | Current: {status}\n\n1️⃣ خاموش | Off (`.type 1`)\n2️⃣ روشن | On (`.type 2`)"
        await event.edit(mix)

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.ping$'))
async def ping_cmd(event):
    start = time.time()
    msg = await event.edit(_t("تست پینگ...", "Pinging...", "پینگ | Pinging..."))
    end = time.time()
    ping_val = round((end - start) * 1000, 2)
    mix = f"🏓 ❲ پونگ | Pong! ❳\n━━━━━━━━━━━━━━━\n⚡ سرعت | Latency: `{ping_val} ms`"
    await msg.edit(_t(mix, mix, mix))

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.info$'))
async def info_cmd(event):
    reply = await event.get_reply_message()
    if not reply: return await event.edit("❌")
    user = await reply.get_sender()
    mix = f"""👤 ❲ اطلاعات کاربر | User Info ❳
━━━━━━━━━━━━━━━━━━━━
⊳ Name: `{user.first_name}`
⊳ Last Name: `{user.last_name or '---'}`
⊳ Username: `@{user.username or '---'}`
⊳ User ID: `{user.id}`
⊳ Is Bot: `{'Yes' if user.bot else 'No'}`"""
    await event.edit(mix)

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.ghost (.*)'))
async def ghost_cmd(event):
    await event.edit(f"👻 {event.pattern_match.group(1)}")
    await asyncio.sleep(3); await event.delete()

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.del$'))
async def del_cmd(event):
    reply = await event.get_reply_message()
    if reply and reply.sender_id == (await user_client.get_me()).id: await reply.delete()
    await event.delete()

# ==========================================
# ذخیره بی‌صدای مدیای تایمردار (پاک‌شونده) در سیو مسیج — بدون هیچ متنی
# ==========================================
@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.ttl$'))
async def ttl_save_cmd(event):
    reply = await event.get_reply_message()
    ttl = None
    if reply and reply.media:
        ttl = getattr(reply.media, 'ttl_seconds', None)
        if ttl is None:
            ttl = getattr(reply, 'ttl', None)
    if not reply or not ttl:
        return await event.edit(_t("❌ روی یک عکس یا ویدیوی تایمردار (پاک‌شونده) ریپلای کن.", "❌ Reply to a self-destructing (TTL) photo or video.", "❌ ریپلای روی مدیای تایمردار | Reply to a TTL photo/video"))
    temp_path = None
    try:
        temp_path = await reply.download_media(file=TEMP_DIR + '/')
        if not temp_path:
            return await event.edit(_t("❌ دانلود مدیا ناموفق بود.", "❌ Media download failed.", "❌ دانلود ناموفق | Download failed"))
        # فقط خود مدیا — بدون هیچ کپشن یا متنی
        await user_client.send_file('me', temp_path)
        await event.delete()
    except Exception as e:
        await event.edit(f"❌ Error:\n{e}")
    finally:
        if temp_path and os.path.exists(temp_path):
            try: os.remove(temp_path)
            except: pass

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.afk(?:\s+(.+))?$'))
async def set_afk(event):
    reason = event.pattern_match.group(1) or "فعلا نیستم و آفلاینم."
    config['afk'] = True; config['afk_reason'] = reason; save_config(config)
    mix = f"💤 ❲ حالت آفلاین | AFK Mode Enabled ❳\n━━━━━━━━━━━━━━━\n⊳ دلیل | Reason: {reason}"
    await event.edit(mix)

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.back$'))
async def unset_afk(event):
    if config.get('afk'): 
        config['afk'] = False; save_config(config)
        await event.edit("🌅 ❲ حالت آفلاین خاموش شد | AFK Disabled ❳")

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.pv(?:\s+(\d))?'))
async def pv_menu(event):
    choice = event.pattern_match.group(1)
    if choice == '1': 
        config["pv_ai"] = True; save_config(config)
        await event.edit("✅ PV AI On.")
    elif choice == '2': 
        config["pv_ai"] = False; save_config(config)
        await event.edit("❌ PV AI Off.")
    else: 
        status = "روشن 🟢" if config["pv_ai"] else "خاموش 🔴"
        mix = f"💬 ❲ منشی پیوی | PV Auto-reply ❳\n━━━━━━━━━━━━━━━\n⊳ وضعیت | Current: {status}\n\n1️⃣ روشن | On (`.pv 1`)\n2️⃣ خاموش | Off (`.pv 2`)"
        await event.edit(mix)

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.gp(?:\s+(\d))?'))
async def gp_menu(event):
    choice = event.pattern_match.group(1)
    if choice == '1': 
        config["gp_ai"] = True; save_config(config)
        await event.edit("✅ GP AI On.")
    elif choice == '2': 
        config["gp_ai"] = False; save_config(config)
        await event.edit("❌ GP AI Off.")
    else: 
        status = "روشن 🟢" if config["gp_ai"] else "خاموش 🔴"
        mix = f"💬 ❲ پاسخگوی گروه | GP Auto-reply ❳\n━━━━━━━━━━━━━━━\n⊳ وضعیت | Current: {status}\n\n1️⃣ روشن | On (`.gp 1`)\n2️⃣ خاموش | Off (`.gp 2`)"
        await event.edit(mix)

# ==========================================
# ضدحذف و ضدویرایش پیام‌های پیوی — ذخیره در سیو مسیج
# ==========================================
ANTIDEL_CACHE_MAX = 200
ANTIDEL_MEDIA_MAX_MB = 20
_pv_cache = {}  # msg_id -> {"sender": str, "text": str, "date": str, "media_path": str|None}
_me_id = None

def _antidel_cache_put(msg_id, sender, text, date, media_path=None):
    if len(_pv_cache) >= ANTIDEL_CACHE_MAX:
        try:
            oldest = next(iter(_pv_cache))
            old = _pv_cache.pop(oldest)
            if old.get("media_path") and os.path.exists(old["media_path"]):
                try: os.remove(old["media_path"])
                except: pass
        except: pass
    _pv_cache[msg_id] = {"sender": sender, "text": text, "date": date, "media_path": media_path}

@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.antidel(?:\s+(\d))?'))
async def antidel_menu(event):
    choice = event.pattern_match.group(1)
    if choice == '1':
        config["antidel"] = True; save_config(config)
        await event.edit(_t("✅ ضدحذف پیوی روشن شد.", "✅ Anti-delete enabled.", "✅ ❲ ضدحذف روشن شد | Anti-delete On ❳"))
    elif choice == '0':
        config["antidel"] = False; save_config(config)
        await event.edit(_t("❌ ضدحذف پیوی خاموش شد.", "❌ Anti-delete disabled.", "❌ ❲ ضدحذف خاموش شد | Anti-delete Off ❳"))
    else:
        status = "روشن 🟢" if config.get("antidel") else "خاموش 🔴"
        mix = f"🛡 ❲ ضدحذف پیوی | Anti-delete ❳\n━━━━━━━━━━━━━━━\n⊳ وضعیت | Current: {status}\n\n1️⃣ روشن | On (`.antidel 1`)\n0️⃣ خاموش | Off (`.antidel 0`)\n\nپیام‌های حذف‌شده و ویرایش‌شده پیوی در سیو مسیج ذخیره می‌شوند."
        await event.edit(_t(mix, mix, mix))

# ==========================================
# دستور مستقیم ابزارهای ثبت‌شده — هر ابزار دستور خودش را دارد
# (دیگر از مسیر .ai صدا زده نمی‌شوند؛ مدل قابل‌اعتماد نیست)
# ==========================================
@user_client.on(events.NewMessage(outgoing=True, pattern=r'^\.([\w-]+)(?:\s+([\s\S]+))?$'))
async def tool_command_dispatcher(event):
    """`.{نام ابزار} <ورودی>` — اجرای مستقیم ابزار ثبت‌شده.
    فقط وقتی نام، ابزار ثبت‌شده باشد و دستور داخلی نباشد عمل می‌کند؛
    بقیه‌ی پیام‌ها (دستورهای داخلی، نام‌های ناشناخته) دست نمی‌خورند."""
    name = (event.pattern_match.group(1) or "").strip()
    tool_input = (event.pattern_match.group(2) or "").strip()
    if name in BUILTIN_COMMANDS:
        return
    tools = config.get("custom_tools", {}) or {}
    tool = tools.get(name)
    if not isinstance(tool, dict):
        return
    desc = (tool.get("desc") or "").strip()
    if not tool_input:
        return await event.edit(_t(
            f"🔧 ابزار «{name}»" + (f" — {desc}" if desc else "") + f"\nاستفاده: `.{name} <ورودی>`",
            f"🔧 Tool «{name}»" + (f" — {desc}" if desc else "") + f"\nUsage: `.{name} <input>`",
            f"🔧 ❲ ابزار | Tool: `{name}` ❳" + (f"\n⊳ {desc}" if desc else "") + f"\n⊳ استفاده | Usage: `.{name} <ورودی>`"))
    msg = await event.edit(_t("⏳ در حال اجرا...", "⏳ Running...", "⏳ ❲ در حال اجرا | Running ❳..."))
    result, err = await call_tool(name, tool_input)
    if err:
        return await msg.edit(_t(f"❌ {err}", f"❌ {err}", f"❌ {err}"))
    img_url = extract_image_url(result)
    if img_url:
        try:
            await user_client.send_file(event.chat_id, img_url, caption=tool_input[:1000])
            await msg.delete()
            return
        except Exception as e:
            logger.error(f"tool photo send error: {e}")
            # افتادن عکس → نتیجه را به‌صورت متن بفرست
    await msg.edit(result or _t("✅ ابزار چیزی برنگرداند.", "✅ The tool returned nothing.", "✅ ابزار چیزی برنگرداند | Tool returned nothing"))

# ==========================================
# هوش مصنوعی چت‌ها (طبیعی، کوتاه و پاسخگو به مدیا)
# ==========================================
def get_media_context(event):
    if event.media:
        if hasattr(event.media, 'document') and event.media.document.mime_type == 'application/x-tgsticker': return "[کاربر یک استیکر فرستاده]"
        elif event.photo: return "[کاربر یک عکس فرستاده]"
        elif event.video: return "[کاربر یک ویدیو فرستاده]"
        elif event.gif: return "[کاربر یک گیف فرستاده]"
        elif event.voice: return "[کاربر یک ویس فرستاده]"
    return ""

@user_client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
async def ai_pv_reply(event):
    me = await user_client.get_me()
    if event.sender_id == me.id:
        if config.get("afk") and not event.raw_text.startswith('.'):
            config["afk"] = False; save_config(config)
            await event.respond("🌅 ❲ حالت آفلاین به دلیل فعالیت شما در اکانت به صورت خودکار خاموش شد ❳")
        return

    if config.get("afk"): 
        return await event.reply(f"💤 ❲ من الان آفلاین هستم | AFK ❳\n⊳ دلیل: {config.get('afk_reason')}")
        
    if not config.get("pv_ai"): return
    user = await event.get_sender()
    
    media_note = get_media_context(event)
    text = event.raw_text.strip()
    if not text and not media_note: return

    async def process():
        prompt = f"کاربری به نام '{user.first_name}' در چت خصوصی به من پیام داده: '{media_note} {text}'. لطفاً یک جواب مناسب به او بده."
        res = await ask_ai(prompt, short_answer=True, is_chat=True, chat_id=event.chat_id)
        memory_add(event.chat_id, text, res)
        await send_type_mode(event, res, is_reply=True)
    asyncio.create_task(process())

@user_client.on(events.NewMessage(incoming=True, func=lambda e: e.is_group))
async def ai_gp_reply(event):
    if not config.get("gp_ai"): return
    me = await user_client.get_me()
    if event.sender_id == me.id: return
    
    is_mentioned = False
    
    if event.is_reply:
        reply_msg = await event.get_reply_message()
        # وقتی ادمین مخفی هستید، پیام‌های شما sender_id گروه را دارند اما ویژگی out آنها True است
        if reply_msg and (reply_msg.out or reply_msg.sender_id == me.id or reply_msg.sender_id == event.chat_id):
            is_mentioned = True
            
    if not is_mentioned and me.username and f"@{me.username}" in event.raw_text:
        is_mentioned = True
        
    if not is_mentioned: return
    
    media_note = get_media_context(event)
    text = event.raw_text.strip()
    if not text and not media_note: return

    async def process():
        prompt = f"در یک گروه تلگرامی هستیم. کسی خطاب به من گفته: '{media_note} {text}'. لطفاً یک جواب مناسب بده."
        res = await ask_ai(prompt, short_answer=True, is_chat=True, chat_id=event.chat_id)
        memory_add(event.chat_id, text, res)
        await send_type_mode(event, res, is_reply=True)
    asyncio.create_task(process())

# --- کش پیام‌های پیوی (فقط ذخیره — هیچ پاسخی نمی‌دهد) ---
@user_client.on(events.NewMessage(func=lambda e: e.is_private))
async def antidel_cache_handler(event):
    global _me_id
    try:
        # پیام‌های سیو مسیج را کش نکن (چت با خودم)
        if event.out and event.chat_id == event.sender_id:
            return
        if _me_id is None:
            try:
                _me_id = (await user_client.get_me()).id
            except: pass
        sender = None
        try:
            s = await event.get_sender()
            if s:
                sender = (s.first_name or "") + (f" {s.last_name}" if getattr(s, 'last_name', None) else "")
                sender = sender.strip() or str(s.id)
        except: pass
        if not sender:
            sender = str(event.sender_id or "?")
        text = event.raw_text or ""
        date_str = ""
        try:
            date_str = event.date.astimezone(TEHRAN_TZ).strftime("%Y-%m-%d %H:%M") if event.date else ""
        except: pass
        media_path = None
        try:
            if event.media and event.file and event.file.size and event.file.size <= ANTIDEL_MEDIA_MAX_MB * 1024 * 1024:
                # پاس دادن پوشه تا تلتون خودش نام + پسوند درست را انتخاب کند
                media_path = await event.download_media(file=TEMP_DIR + '/')
        except: media_path = None
        _antidel_cache_put(event.id, sender, text, date_str, media_path)
    except Exception as e:
        logger.error(f"Antidel cache error: {e}")

@user_client.on(events.MessageDeleted)
async def antidel_deleted_handler(event):
    try:
        if not config.get("antidel"):
            return
        chat_id = getattr(event, 'chat_id', None)
        is_private = getattr(event, 'is_private', None)
        if is_private is None:
            is_private = bool(chat_id) and chat_id > 0
        if not is_private:
            return
        for deleted_id in (event.deleted_ids or []):
            entry = _pv_cache.get(deleted_id)
            if not entry:
                continue
            header = f"🗑 ❲ پیام حذف‌شده | Deleted Message ❳\n━━━━━━━━━━━━━━━\n👤 {entry['sender']}\n🕒 {entry['date']}\n━━━━━━━━━━━━━━━\n"
            body = entry["text"] or "(بدون متن)"
            media_path = entry.get("media_path")
            try:
                if media_path and os.path.exists(media_path):
                    await user_client.send_file('me', media_path, caption=f"{header}{body}")
                else:
                    await user_client.send_message('me', f"{header}{body}")
            except Exception as e:
                logger.error(f"Antidel restore error: {e}")
    except Exception as e:
        logger.error(f"Antidel deleted handler error: {e}")

@user_client.on(events.MessageEdited(func=lambda e: e.is_private))
async def antidel_edited_handler(event):
    try:
        if not config.get("antidel"):
            return
        msg = event.message
        if not msg:
            return
        entry = _pv_cache.get(msg.id)
        if not entry:
            return
        new_text = msg.text or ""
        old_text = entry.get("text") or ""
        if new_text == old_text:
            return
        text = (f"✏️ ❲ پیام ویرایش‌شده | Edited Message ❳\n━━━━━━━━━━━━━━━\n"
                f"👤 {entry['sender']}\n🕒 {entry['date']}\n━━━━━━━━━━━━━━━\n"
                f"📌 قبل | Before:\n{old_text or '(بدون متن)'}\n━━━━━━━━━━━━━━━\n"
                f"📌 بعد | After:\n{new_text or '(بدون متن)'}")
        try:
            await user_client.send_message('me', text)
        except Exception as e:
            logger.error(f"Antidel edit restore error: {e}")
        entry["text"] = new_text
    except Exception as e:
        logger.error(f"Antidel edited handler error: {e}")

# === BOTAPI POLLER (MERGED) ===
# Bot API long-poller for the management bot (@Ajdhhdudhdhbot), merged into
# this single process (was botapi_poller.py). Runs in a daemon thread: the
# Telethon selfbot runs on the asyncio loop, the poller does blocking
# getUpdates long-polls in its own thread. They talk through the same JSON
# files as before (botapi_request.json / botapi_reply.json / botapi_status.json
# / botapi_offset.txt), and botapi_ipc_loop serves the login steps.
import threading as _threading
import urllib.request as _urllib_request
import urllib.error as _urllib_error

_BOTAPI_HERE = os.path.dirname(os.path.abspath(__file__))
_BOTAPI_STATUS_FILE = os.path.join(_BOTAPI_HERE, "botapi_status.json")
_BOTAPI_REQ_FILE = os.path.join(_BOTAPI_HERE, "botapi_request.json")
_BOTAPI_REPLY_FILE = os.path.join(_BOTAPI_HERE, "botapi_reply.json")
_BOTAPI_LOCK_FILE = os.path.join(_BOTAPI_HERE, "botapi.lock")
_BOTAPI_OFFSET_FILE = os.path.join(_BOTAPI_HERE, "botapi_offset.txt")
_BOTAPI_API = "https://api.telegram.org/bot" + BOT_TOKEN
_botapi_stop_event = _threading.Event()


def _botapi_api_call(method, payload=None, timeout=35):
    data = json.dumps(payload).encode() if payload is not None else None
    req = _urllib_request.Request(
        _BOTAPI_API + "/" + method, data=data,
        headers={"Content-Type": "application/json"}, method="POST" if data else "GET",
    )
    with _urllib_request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _botapi_send_message(chat_id, text):
    try:
        _botapi_api_call("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})
    except Exception as e:
        print(f"botapi sendMessage failed: {e}", flush=True)


def _botapi_read_status():
    try:
        with open(_BOTAPI_STATUS_FILE, encoding="utf-8") as f:
            st = json.load(f)
        if time.time() - st.get("ts", 0) > 600:
            return None
        return st
    except Exception:
        return None


def _botapi_handle_start(chat_id):
    st = _botapi_read_status()
    if st and st.get("user_connected"):
        status = "🟢 متصل"
    elif st:
        status = "🔴 قطع"
    else:
        status = "⚠️ نامشخص (وضعیت به‌روز نیست)"
    _botapi_send_message(
        chat_id,
        f"✅ **ربات آنلاین است!**\nوضعیت یوزربات: {status}\n\n1️⃣ `/phone +98...`\n2️⃣ `/code 12345`",
    )


def _botapi_forward_login(chat_id, text):
    req_id = str(int(time.time() * 1000))
    req = {"id": req_id, "chat_id": chat_id, "text": text}
    with open(_BOTAPI_REQ_FILE, "w", encoding="utf-8") as f:
        json.dump(req, f)
    for _ in range(120):
        if _botapi_stop_event.is_set():
            return
        time.sleep(0.5)
        try:
            with open(_BOTAPI_REPLY_FILE, encoding="utf-8") as f:
                rep = json.load(f)
            if rep.get("id") == req_id:
                os.remove(_BOTAPI_REPLY_FILE)
                _botapi_send_message(chat_id, rep.get("text", "✅ انجام شد."))
                return
        except FileNotFoundError:
            continue
        except Exception:
            continue
    _botapi_send_message(chat_id, "❌ پاسخی از یوزربات نرسید؛ دوباره تلاش کن.")


def _botapi_poller_loop():
    try:
        _lock = open(_BOTAPI_LOCK_FILE, "w")
        try:
            fcntl.flock(_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("botapi_poller: another poller is already running", flush=True)
            return
    except Exception as e:
        print(f"botapi_poller: lock failed: {e}", flush=True)
        return
    offset = 0
    try:
        with open(_BOTAPI_OFFSET_FILE) as f:
            offset = int(f.read().strip() or 0)
    except Exception:
        offset = 0
    print("botapi_poller started", flush=True)
    while not _botapi_stop_event.is_set():
        try:
            res = _botapi_api_call("getUpdates", {"offset": offset, "timeout": 30}, timeout=35)
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                with open(_BOTAPI_OFFSET_FILE, "w") as f:
                    f.write(str(offset))
                msg = upd.get("message") or {}
                chat_id = (msg.get("chat") or {}).get("id")
                text = (msg.get("text") or "").strip()
                if not chat_id or not text:
                    continue
                if time.time() - int(msg.get("date") or 0) > 300:
                    continue
                if text.startswith("/start"):
                    _botapi_handle_start(chat_id)
                elif text.startswith("/phone ") or text.startswith("/code ") or text.startswith("/password "):
                    _botapi_forward_login(chat_id, text)
        except _urllib_error.HTTPError as e:
            print(f"botapi_poller getUpdates HTTP {e.code}", flush=True)
            _botapi_stop_event.wait(5)
        except Exception as e:
            print(f"botapi_poller error: {e}", flush=True)
            _botapi_stop_event.wait(5)


def start_botapi_poller_thread():
    t = _threading.Thread(target=_botapi_poller_loop, daemon=True, name="botapi-poller")
    t.start()
    return t
# === END BOTAPI POLLER (MERGED) ===

# ==========================================
# استارت ربات
# ==========================================
if __name__ == '__main__':
    # Single-instance lock: if another mybot is already running, exit immediately.
    # This stops the watchdog (or anything else) from spawning a duplicate that
    # would fight over the same Telegram sessions and break update delivery.
    _lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mybot.lock')
    _lock_file = open(_lock_path, 'w')
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("⚠️ Another mybot instance is already running. Exiting.")
        raise SystemExit(0)
    print("🚀 استارت سرویس‌ها...")
    start_botapi_poller_thread()
    loop = asyncio.get_event_loop()
    
    bot.start(bot_token=BOT_TOKEN)
    print("✅ ربات کنترلر آنلاین شد!")
    
    async def check_user_session():
        try:
            if os.path.exists(USER_SESSION + '.session'):
                print("🔄 سشن یافت شد، در حال اتصال...")
            await user_client.connect()
            if await user_client.is_user_authorized():
                await set_initial_base_name()
                print("✅ یوزربات متصل شد!")
                loop.create_task(background_profile_updater())
                loop.create_task(heartbeat_loop())
            else:
                print("⚠️ یوزربات لاگین نشده — با /phone وارد شوید.")
        except Exception as e: print(f"⚠️ مشکل اتصال: {e}")
        loop.create_task(botapi_ipc_loop())

    loop.create_task(check_user_session())
    try: bot.run_until_disconnected()
    except KeyboardInterrupt: pass