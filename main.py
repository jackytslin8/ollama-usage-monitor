import os, re, json, time, threading, traceback, sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from http.cookiejar import MozillaCookieJar
from io import StringIO

# 防止在 Windows 終端機執行時因為 emoji 導致 UnicodeEncodeError
if sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

from tempfile import NamedTemporaryFile
from fastapi import FastAPI
from fastapi.responses import Response, HTMLResponse
import requests
from prometheus_client import generate_latest, Gauge, CONTENT_TYPE_LATEST

def get_taipei_time():
    taipei_tz = timezone(timedelta(hours=8))
    return datetime.now(taipei_tz).strftime('%Y-%m-%d %H:%M:%S')


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=background_scheduler, daemon=True).start()
    yield

app = FastAPI(title="Ollama Cloud Usage Monitor", lifespan=lifespan)

# Prometheus 指標
session_g = Gauge("ollama_session_usage_percent", "Session usage %", ["account"])
weekly_g = Gauge("ollama_weekly_usage_percent", "Weekly usage %", ["account"])
status_g = Gauge("ollama_monitor_status", "1=ok, 0=error", ["account"])

# 狀態快取
USAGE_CACHE = {}

# 讀取環境變數
ACCOUNTS = json.loads(os.getenv("OLLAMA_ACCOUNTS", "[]"))
PORT = int(os.getenv("PORT", "8080"))

# 可調式更新時間設定 (秒)
SCRAPE_INTERVAL = int(os.getenv("SCRAPE_INTERVAL", "900"))
REFRESH_INTERVAL = int(os.getenv("REFRESH_INTERVAL", "60"))

# 每個帳號各自使用獨立的 cookies
# 環境變數格式: OLLAMA_COOKIES_<帳號名稱>
# 例如: OLLAMA_COOKIES_account1, OLLAMA_COOKIES_account2
# 內容: 標準 Netscape cookies.txt（由 "Get cookies.txt LOCALLY" 擴充功能匯出）

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def load_cookies_from_txt(cookies_txt_content):
    """將 Netscape cookies.txt 格式或 JSON 格式解析為 requests 可用的 dict"""
    cookies = {}
    if not cookies_txt_content:
        return cookies
        
    content = cookies_txt_content.strip()
    print(f"🔍 DEBUG Raw Env String: {repr(content[:100])}...", flush=True)
    
    # 支援 JSON 格式的 Cookie {"aid": "...", "__Secure-session": "..."}
    if content.startswith('{') and content.endswith('}'):
        try:
            return json.loads(content)
        except Exception as e:
            print(f"⚠️ JSON parsing error for cookies: {e}", flush=True)
            return cookies

    # 用正規表達式抓取被 Zeabur 或其他雲端平台破壞斷行（擠成一團）的 Netscape 格式
    # 匹配: ollama.com (空白) FALSE (空白) / (空白) TRUE (空白) 數字 (空白) 鍵名 (空白) 數值
    matches = re.findall(r'ollama\.com\s+(?:TRUE|FALSE)\s+/\s+(?:TRUE|FALSE)\s+\d+\s+([^\s]+)\s+([^\s]+)', content, re.IGNORECASE)
    
    if matches:
        for name, value in matches:
            cookies[name] = value
    else:
        # 傳統備用解析法 (一行一行)
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 7:
                name, value = parts[5], parts[6]
                cookies[name] = value
                
    return cookies


def create_session_for_account(account_name):
    """為指定帳號建立帶有 cookies 的 requests session"""
    session = requests.Session()
    session.headers.update(HEADERS)

    # 讀取該帳號的 cookies: OLLAMA_COOKIES_<name>
    cookies_txt = os.getenv(f"OLLAMA_COOKIES_{account_name}", "")
    if not cookies_txt:
        print(f"⚠️ [{account_name}] No cookies found. Set OLLAMA_COOKIES_{account_name} env var.", flush=True)
        return None

    cookies = load_cookies_from_txt(cookies_txt)
    if cookies:
        print(f"🍪 [{account_name}] Loaded {len(cookies)} cookies", flush=True)
        for name, value in cookies.items():
            session.cookies.set(name, value)
    else:
        print(f"⚠️ [{account_name}] Cookie content could not be parsed.", flush=True)
        return None

    return session


def fetch_usage(session, account_name):
    """抓取使用量資料"""
    try:
        # 嘗試 /settings 頁面
        resp = session.get("https://ollama.com/settings", timeout=15)
        print(f"📡 [{account_name}] GET /settings -> status={resp.status_code}, url={resp.url}", flush=True)

        # 如果被重導向到登入頁面
        if "signin" in resp.url.lower() or "login" in resp.url.lower():
            print(f"❌ [{account_name}] Redirected to login - cookies may be expired!", flush=True)
            return None

        txt = resp.text

        # 先清理所有 HTML 標籤
        clean = re.sub(r"<[^>]+>", " ", txt)
        clean = re.sub(r"\s+", " ", clean).strip()

        # 針對乾淨文字進行 "Session usage 0% used" 結構的搜尋
        s = re.search(r"Session usage[^\d]*?(\d+(?:\.\d+)?)\s*%", clean, re.IGNORECASE)
        w = re.search(r"Weekly usage[^\d]*?(\d+(?:\.\d+)?)\s*%", clean, re.IGNORECASE)

        s_pct = float(s.group(1)) if s else None
        w_pct = float(w.group(1)) if w else None

        s_res = re.search(r"Session usage.{0,100}?Resets in\s+([0-9.]+\s+[a-zA-Z]+)", clean, re.IGNORECASE)
        w_res = re.search(r"Weekly usage.{0,100}?Resets in\s+([0-9.]+\s+[a-zA-Z]+)", clean, re.IGNORECASE)

        s_reset = s_res.group(1) if s_res else None
        w_reset = w_res.group(1) if w_res else None

        if s_pct is not None or w_pct is not None:
            return {"session": s_pct, "weekly": w_pct, "s_reset": s_reset, "w_reset": w_reset}

        # 輸出頁面內容以供除錯（去除 HTML 標籤）
        preview = clean[:800]
        print(f"📄 [{account_name}] Page preview: {preview}", flush=True)

        return None

    except Exception as e:
        print(f"❌ [{account_name}] Fetch error: {e}", flush=True)
        traceback.print_exc()
        return None


def run_scraper():
    print("🔄 [Monitor] Starting usage check...", flush=True)

    for acc in ACCOUNTS:
        name = acc.get("name", "Unknown")
        try:
            session = create_session_for_account(name)
            if not session:
                status_g.labels(account=name).set(0)
                USAGE_CACHE[name] = {
                    "session": None, "weekly": None,
                    "s_reset": None, "w_reset": None,
                    "updated": get_taipei_time(), "status": "error",
                    "error": f"OLLAMA_COOKIES_{name} not set"
                }
                continue
            usage_data = fetch_usage(session, name)

            if usage_data:
                s_pct = usage_data.get("session") or 0.0
                w_pct = usage_data.get("weekly") or 0.0
                s_reset = usage_data.get("s_reset")
                w_reset = usage_data.get("w_reset")

                session_g.labels(account=name).set(s_pct)
                weekly_g.labels(account=name).set(w_pct)
                status_g.labels(account=name).set(1)
                USAGE_CACHE[name] = {
                    "session": s_pct, "weekly": w_pct,
                    "s_reset": s_reset, "w_reset": w_reset,
                    "updated": get_taipei_time(), "status": "ok"
                }
                print(f"✅ {name} | Session: {s_pct}% (Resets in: {s_reset}) | Weekly: {w_pct}% (Resets in: {w_reset})", flush=True)
            else:
                status_g.labels(account=name).set(0)
                USAGE_CACHE[name] = {
                    "session": None, "weekly": None,
                    "s_reset": None, "w_reset": None,
                    "updated": get_taipei_time(), "status": "error",
                    "error": "Could not parse usage data (cookies expired?)"
                }
                print(f"⚠️ {name}: Could not parse usage data", flush=True)

        except Exception as e:
            status_g.labels(account=name).set(0)
            USAGE_CACHE[name] = {
                "session": None, "weekly": None,
                "s_reset": None, "w_reset": None,
                "updated": get_taipei_time(), "status": "error",
                "error": str(e)
            }
            print(f"❌ {name} failed: {e}", flush=True)
            traceback.print_exc()

        time.sleep(3)


def background_scheduler():
    if not ACCOUNTS:
        print("⚠️ No accounts configured. Set OLLAMA_ACCOUNTS env var.", flush=True)
        return
    print(f"📋 Monitoring {len(ACCOUNTS)} account(s): {[a.get('name') for a in ACCOUNTS]}", flush=True)
    run_scraper()
    while True:
        time.sleep(SCRAPE_INTERVAL)
        run_scraper()


# ============ Routes ============

@app.get("/")
def root():
    cards = ""
    for name, data in USAGE_CACHE.items():
        status_ok = data.get("status") == "ok"
        status_text = "Active" if status_ok else "Error"
        status_color = "text-emerald-400" if status_ok else "text-rose-400"
        
        session_pct = data.get("session")
        session_val_str = f"{session_pct}%" if session_pct is not None else "N/A"
        s_reset = data.get("s_reset") or "N/A"
        
        weekly_pct = data.get("weekly")
        weekly_val_str = f"{weekly_pct}%" if weekly_pct is not None else "N/A"
        w_reset = data.get("w_reset") or "N/A"
        
        updated = data.get("updated", "N/A")
        error_msg = data.get("error", "")
        
        session_width = session_pct if session_pct is not None else 0
        weekly_width = weekly_pct if weekly_pct is not None else 0

        # 色彩邏輯：使用量越高越危險
        def get_progress_color(pct):
            if pct is None: return "bg-slate-600"
            if pct < 50: return "bg-emerald-500"
            if pct < 80: return "bg-amber-500"
            return "bg-rose-500"
        
        s_color = get_progress_color(session_pct)
        w_color = get_progress_color(weekly_pct)
        
        error_html = f'<div class="mt-4 text-xs text-rose-400 bg-rose-950/30 p-2 rounded border border-rose-900/50 break-words">{error_msg}</div>' if error_msg and not status_ok else ''

        cards += f"""
        <div class="bg-slate-800/50 backdrop-blur-md rounded-xl p-6 border border-slate-700/50 shadow-xl hover:shadow-2xl hover:border-slate-600/50 transition-all duration-300 group">
            <div class="flex justify-between items-center mb-6">
                <h3 class="text-xl font-bold font-semibold text-slate-100 flex items-center gap-2">
                    <span class="text-2xl group-hover:scale-110 transition-transform">🤖</span>
                    {name}
                </h3>
                <div class="flex items-center gap-1.5 px-3 py-1 rounded-full bg-slate-900/50 border border-slate-700 border-opacity-50 {status_color} text-sm font-medium shadow-inner">
                    <span class="relative flex h-2.5 w-2.5">
                      {'<span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>' if status_ok else ''}
                      <span class="relative inline-flex rounded-full h-2.5 w-2.5 {'bg-emerald-500' if status_ok else 'bg-rose-500'}"></span>
                    </span>
                    {status_text}
                </div>
            </div>
            
            <div class="space-y-5">
                <!-- Session Usage -->
                <div class="space-y-2">
                    <div class="flex justify-between text-sm">
                        <span class="text-slate-400 font-medium tracking-wide">Session Usage</span>
                        <span class="text-slate-200 font-bold">{session_val_str}</span>
                    </div>
                    <div class="h-2.5 w-full bg-slate-900/80 rounded-full overflow-hidden shadow-inner">
                        <div class="h-full {s_color} rounded-full transition-all duration-1000 ease-out relative" style="width: {session_width}%">
                            <div class="absolute top-0 left-0 right-0 bottom-0 bg-white/20" style="animation: shimmer 2s infinite linear;"></div>
                        </div>
                    </div>
                    <div class="text-xs text-slate-500 text-right">Resets in: <span class="text-slate-400">{s_reset}</span></div>
                </div>

                <!-- Weekly Usage -->
                <div class="space-y-2">
                    <div class="flex justify-between text-sm">
                        <span class="text-slate-400 font-medium tracking-wide">Weekly Usage</span>
                        <span class="text-slate-200 font-bold">{weekly_val_str}</span>
                    </div>
                    <div class="h-2.5 w-full bg-slate-900/80 rounded-full overflow-hidden shadow-inner">
                        <div class="h-full {w_color} rounded-full transition-all duration-1000 ease-out relative" style="width: {weekly_width}%">
                            <div class="absolute top-0 left-0 right-0 bottom-0 bg-white/20" style="animation: shimmer 2s infinite linear;"></div>
                        </div>
                    </div>
                    <div class="text-xs text-slate-500 text-right">Resets in: <span class="text-slate-400">{w_reset}</span></div>
                </div>
            </div>
            
            <div class="mt-6 pt-4 border-t border-slate-700/50 flex justify-between items-center text-xs text-slate-500">
                <span>Updated: {updated}</span>
            </div>
            {error_html}
        </div>
        """

    if not cards:
        cards = """
        <div class="col-span-full flex flex-col items-center justify-center py-20 px-4 text-center bg-slate-800/30 rounded-xl border border-slate-700/50 border-dashed">
            <svg class="w-16 h-16 text-slate-600 mb-4 animate-pulse" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M13 10V3L4 14h7v7l9-11h-7z"></path></svg>
            <h3 class="text-lg font-medium text-slate-400 mb-1">Waiting for initial data...</h3>
            <p class="text-slate-500">The scraper is gathering usage metrics</p>
        </div>
        """

    cookie_counts = sum(1 for a in ACCOUNTS if os.getenv(f"OLLAMA_COOKIES_{a.get('name', '')}"))
    cookie_status = f"{cookie_counts}/{len(ACCOUNTS)} Configured"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Ollama Cloud Usage Monitor</title>
    <meta http-equiv="refresh" content="{REFRESH_INTERVAL}">
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        body {{
            font-family: 'Inter', sans-serif;
            background-color: #0f172a;
            background-image: 
                radial-gradient(at 0% 0%, hsla(253,16%,7%,1) 0, transparent 50%), 
                radial-gradient(at 50% 0%, hsla(225,39%,30%,0.2) 0, transparent 50%), 
                radial-gradient(at 100% 0%, hsla(339,49%,30%,0.2) 0, transparent 50%);
            background-attachment: fixed;
            min-height: 100vh;
        }}
        @keyframes shimmer {{
            0% {{ transform: translateX(-100%); }}
            100% {{ transform: translateX(100%); }}
        }}
        .glass-panel {{
            background: rgba(30, 41, 59, 0.7);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            border: 1px solid rgba(255, 255, 255, 0.05);
        }}
    </style>
</head>
<body class="text-slate-300 antialiased selection:bg-cyan-500/30">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-10">
        
        <!-- Header -->
        <header class="mb-10 animate-[fadeIn_0.5s_ease-out]">
            <div class="flex flex-col md:flex-row md:items-end justify-between gap-6">
                <div>
                    <h1 class="text-4xl md:text-5xl font-extrabold text-transparent bg-clip-text bg-gradient-to-r from-cyan-400 to-blue-500 tracking-tight mb-3">
                        Ollama Monitor
                    </h1>
                    <p class="text-slate-400 text-lg max-w-2xl">
                        Real-time cloud usage monitoring and analytics
                    </p>
                </div>
                
                <!-- System Status -->
                <div class="glass-panel rounded-lg p-4 flex gap-6 items-center shadow-lg w-full md:w-auto overflow-x-auto">
                    <div class="flex flex-col min-w-[70px]">
                        <span class="text-xs text-slate-500 uppercase tracking-wider font-semibold mb-1">Accounts</span>
                        <span class="text-xl font-bold text-slate-200">{len(ACCOUNTS)}</span>
                    </div>
                    <div class="w-px h-10 bg-slate-700/50"></div>
                    <div class="flex flex-col min-w-[90px]">
                        <span class="text-xs text-slate-500 uppercase tracking-wider font-semibold mb-1">Configured</span>
                        <span class="text-xl font-bold text-slate-200">{cookie_status}</span>
                    </div>
                    <div class="w-px h-10 bg-slate-700/50"></div>
                    <div class="flex flex-col min-w-[100px]">
                        <span class="text-xs text-slate-500 uppercase tracking-wider font-semibold mb-1">Refresh</span>
                        <span class="text-sm font-medium text-cyan-400">{REFRESH_INTERVAL}s / {SCRAPE_INTERVAL}s</span>
                    </div>
                </div>
            </div>
        </header>

        <!-- Main Grid -->
        <main>
            <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-6 auto-rows-max">
                {cards}
            </div>
        </main>

        <!-- Footer -->
        <footer class="mt-16 pt-8 border-t border-slate-800/80 flex flex-col md:flex-row items-center justify-between gap-4 text-sm text-slate-500">
            <div class="flex flex-wrap gap-4">
                <a href="/health" class="hover:text-cyan-400 transition-colors flex items-center gap-1">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
                    Health API
                </a>
                <a href="/metrics" class="hover:text-cyan-400 transition-colors flex items-center gap-1">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 13v-1m4 1v-3m4 3V8M8 21l4-4 4 4M3 4h18M4 4h16v12a1 1 0 01-1 1H5a1 1 0 01-1-1V4z"></path></svg>
                    Prometheus
                </a>
                <a href="/usage" class="hover:text-cyan-400 transition-colors flex items-center gap-1">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zM14 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zM14 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z"></path></svg>
                    Raw JSON
                </a>
            </div>
            <div>
                Built with FastAPI & Tailwind CSS
            </div>
        </footer>
    </div>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.get("/health")
def health():
    cookie_counts = sum(1 for a in ACCOUNTS if os.getenv(f"OLLAMA_COOKIES_{a.get('name', '')}"))
    return {
        "status": "ok",
        "accounts_monitored": len(ACCOUNTS),
        "cookies_configured_count": cookie_counts,
    }


@app.get("/metrics")
def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/usage")
def usage():
    return USAGE_CACHE


@app.post("/trigger")
def trigger():
    run_scraper()
    return {"message": "Scraper triggered manually"}
