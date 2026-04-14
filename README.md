# Ollama Usage Monitor

監控 Ollama 雲端帳號的使用量，透過輕量級的 HTTP 請求搭配使用者匯出的 Cookie 定期獲取用量資訊，並以 Prometheus metrics 格式暴露數據，提供即時的網頁儀表板。

## 功能

- 🔄 自動抓取 Ollama 帳號用量 (不需複雜瀏覽器自動化，完全繞過 CAPTCHA 阻擋)
- 📊 提供 Prometheus metrics 端點 (`/metrics`) 以及視覺化網頁儀表板
- 🔐 支援多帳號監控，且可透過 JSON 格式輕鬆設定 Cookie
- ⏱️ 支援環境變數自訂背景爬取頻率與前端重新整理秒數
- 🐳 極致輕量的 Docker 映像檔 (`python:3.11-slim`)

## 環境變數

| 變數 | 說明 | 預設值 |
|------|------|--------|
| `OLLAMA_ACCOUNTS` | JSON 格式的帳號清單 (只需填寫 `name`) | `[]` |
| `OLLAMA_COOKIES_<name>` | 該帳號的 Cookie (支援短巧的單行 JSON 格式) | 無 |
| `SCRAPE_INTERVAL` | 背景自動去 Ollama 抓取一次最新資料的間隔 (秒) | `900` (15分鐘) |
| `REFRESH_INTERVAL` | 瀏覽器停留在監控首頁時，幾秒鐘自動重新整理一次 (秒) | `60` |
| `PORT` | 服務埠號 | `8080` |

### `OLLAMA_ACCOUNTS` 設定範例

現在不需要填寫帳號密碼，只需為帳號取名：

```json
[
  {
    "name": "account1"
  },
  {
    "name": "account2"
  }
]
```

### `OLLAMA_COOKIES_<name>` 設定範例

請利用瀏覽器擴充功能（如 "Get cookies.txt LOCALLY"）匯出 `ollama.com` 的 Cookie。為了避免部署平台（如 Zeabur）把斷行與空白搞亂，強烈建議組合為**單行 JSON 格式**貼上：

> **環境變數名稱**: `OLLAMA_COOKIES_account1` (名稱需和 ACCOUNTS 裡的 name 對應)

```json
{"aid": "填入你的aid數值", "__Secure-session": "填入你的__Secure-session非常長的那串數值"}
```

## API 端點

| 路徑 | 方法 | 說明 |
|------|------|------|
| `/` | GET | 監控首頁視覺化儀表板 |
| `/health` | GET | 健康檢查 |
| `/metrics` | GET | Prometheus metrics |
| `/usage` | GET | JSON 格式用量資料 |
| `/trigger` | POST | 手動觸發爬取 |

## 部署到 Zeabur

1. Fork 此 repo 或連結 GitHub
2. 在 Zeabur 建立新服務，選擇此 repo
3. 設定環境變數 `OLLAMA_ACCOUNTS` (如: `[{"name":"account1"}]`)
4. 設定對應的 Cookie 環境變數 `OLLAMA_COOKIES_account1`
5. (可選) 設定 `SCRAPE_INTERVAL` 與 `REFRESH_INTERVAL`
6. 部署完成後即可使用

## 本地開發

```bash
# 安裝依賴 (已經移除龐大的 playwright)
pip install -r requirements.txt

# 設定環境變數
export OLLAMA_ACCOUNTS='[{"name":"test"}]'
export OLLAMA_COOKIES_test='{"aid": "xxx", "__Secure-session": "yyy"}'

# 啟動服務
uvicorn main:app --host 0.0.0.0 --port 8080
```
