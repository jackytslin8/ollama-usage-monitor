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
    rows = ""
    for name, data in USAGE_CACHE.items():
        status_icon = "✅" if data.get("status") == "ok" else "❌"
        session_val = f"{data.get('session', 'N/A')}%" if data.get("session") is not None else "N/A"
        s_reset = data.get("s_reset") or "N/A"
        weekly_val = f"{data.get('weekly', 'N/A')}%" if data.get("weekly") is not None else "N/A"
        w_reset = data.get("w_reset") or "N/A"
        updated = data.get("updated", "N/A")
        error = data.get("error", "")
        rows += f"<tr><td>{status_icon}</td><td>{name}</td><td>{session_val}</td><td>{s_reset}</td><td>{weekly_val}</td><td>{w_reset}</td><td>{updated}</td><td style='color:#f87171;'>{error}</td></tr>"

    if not rows:
        rows = "<tr><td colspan='8' style='text-align:center;color:#888;'>等待首次資料抓取中...</td></tr>"

    cookie_counts = sum(1 for a in ACCOUNTS if os.getenv(f"OLLAMA_COOKIES_{a.get('name', '')}"))
    cookie_status = f"🍪 {cookie_counts}/{len(ACCOUNTS)} 已設定"

    html = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8"><title>Ollama Usage Monitor</title>
<meta http-equiv="refresh" content="{REFRESH_INTERVAL}">
<style>
  body {{ font-family: -apple-system, 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0; padding: 2rem; margin: 0; }}
  h1 {{ color: #38bdf8; margin-bottom: 0.5rem; }}
  .subtitle {{ color: #94a3b8; margin-bottom: 1.5rem; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
  th, td {{ padding: 0.75rem 1rem; text-align: left; border-bottom: 1px solid #334155; }}
  th {{ background: #1e293b; color: #94a3b8; font-weight: 600; }}
  tr:hover {{ background: #1e293b; }}
  .info {{ color: #64748b; margin-top: 1.5rem; font-size: 0.875rem; }}
  .badge {{ display: inline-block; padding: 0.25rem 0.5rem; border-radius: 4px; font-size: 0.8rem; }}
  .badge-ok {{ background: #065f46; color: #6ee7b7; }}
  .badge-err {{ background: #7f1d1d; color: #fca5a5; }}
  a {{ color: #38bdf8; text-decoration: none; }} a:hover {{ text-decoration: underline; }}
</style></head><body>
<h1>📊 Ollama Usage Monitor</h1>
<p class="subtitle">Monitoring {len(ACCOUNTS)} account(s) | Cookie: {cookie_status} | Auto-refresh {REFRESH_INTERVAL}s | Scrape {SCRAPE_INTERVAL}s</p>
<table>
  <tr><th>Status</th><th>Account</th><th>Session usage</th><th>Session Reset</th><th>Weekly usage</th><th>Weekly Reset</th><th>Updated</th><th>Error</th></tr>
  {rows}
</table>
<p class="info">
  API: <a href="/health">/health</a> | <a href="/metrics">/metrics</a> | <a href="/usage">/usage</a>
</p>
</body></html>"""
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
