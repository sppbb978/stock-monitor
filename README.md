# 股票到價提醒

這是一個使用 Streamlit 製作的網頁介面股票關鍵價位監控工具。可輸入台股（例如 `2330.TW`）或美股（例如 `AAPL`）代碼，並在價格觸發買入或賣出目標時發送 LINE 通知。

## 啟動

在本資料夾開啟終端機並執行：

```powershell
pip install -r requirements.txt
streamlit run app.py
```

瀏覽器會自動開啟；若沒有，請造訪終端機顯示的本機網址（通常是 `http://localhost:8501`）。

## 使用方式

1. 在左側欄輸入股票代碼／名稱與至少一個目標價，按「新增股票」。支援台股 4 位代碼（`2330`）、中文名稱（`台積電`）及美股代碼（`NVDA`）；台股會自動轉為 yfinance 使用的 `.TW` 格式。
2. 輸入 LINE Channel Access Token 與 LINE User ID，按「儲存 LINE 設定」。
3. 開啟「啟動監控」；工具會立即檢查，之後每 60 秒更新一次。

同一支股票、同一類型的提醒（買入或賣出）在發送後 30 分鐘內不會重複發送。設定存放於 `config.json`，監控清單存放於 `watchlist.json`。請勿將包含真實 Token 的 `config.json` 提交到公開版本庫。

> 注意：Streamlit 的自動更新需讓該瀏覽器頁面保持開啟。yfinance 報價可能有延遲，僅適合提醒用途，不應作為交易依據。
