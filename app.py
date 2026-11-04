"""Streamlit 股票關鍵價位監控工具 (終極修復完整版)。"""

from __future__ import annotations

import base64
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
import streamlit as st
import twstock
import yfinance as yf
from streamlit_autorefresh import st_autorefresh

# 嘗試載入 ZoneInfo，若環境缺乏 tzdata 則回退至固定時區
try:
    from zoneinfo import ZoneInfo
    TAIPEI_TZ = ZoneInfo("Asia/Taipei")
except Exception:
    TAIPEI_TZ = timezone(timedelta(hours=8))

BASE_DIR = Path(__file__).resolve().parent
WATCHLIST_FILE = BASE_DIR / "watchlist.json"
CONFIG_FILE = BASE_DIR / "config.json"
COOLDOWN_SECONDS = 30 * 60  # 30 分鐘冷卻
# 休市節流：休市、盤後或暫時無報價時，不在每次自動更新都重複寫入日誌。
MARKET_CLOSED_LOG_THROTTLE_SECONDS = 30 * 60
GITHUB_WATCHLIST_PATH = "watchlist.json"


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    """讀取 JSON；第一次使用或檔案損壞時回傳預設值。"""
    if not path.exists():
        return default.copy()
    try:
        with path.open("r", encoding="utf-8") as file:
            value = json.load(file)
            return value if isinstance(value, dict) else default.copy()
    except (json.JSONDecodeError, OSError):
        return default.copy()


def save_json(path: Path, data: dict[str, Any]) -> None:
    """寫入 JSON 檔案。"""
    try:
        with path.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
    except OSError:
        pass


def init_config() -> dict[str, Any]:
    """初始化全域 Config 並存在 Session State 中。"""
    default_config = {
        "line_channel_access_token": "",
        "line_user_id": "",
        "refresh_seconds": 60,
        "github_token": "",
        "github_repo": "",
        "github_branch": "main",
    }
    # config.json 是唯一來源；每次 rerun 都合併最新的本機設定，避免 Session State 留住舊值。
    saved_config = load_json(CONFIG_FILE, default_config)
    if "config" not in st.session_state:
        st.session_state.config = saved_config
    else:
        st.session_state.config.update(saved_config)
    return st.session_state.config


def update_config(new_settings: dict[str, Any]) -> None:
    """同步更新 Session State 與硬碟中的 config.json。"""
    if "config" not in st.session_state:
        st.session_state.config = {}
    st.session_state.config.update(new_settings)
    save_json(CONFIG_FILE, st.session_state.config)


def github_settings(config: dict[str, Any]) -> tuple[str, str, str]:
    """只從本機 config.json（Session State 快取）讀取 GitHub 設定。"""
    return (
        str(config.get("github_token", "")).strip(),
        str(config.get("github_repo", "")).strip(),
        str(config.get("github_branch", "main")).strip() or "main",
    )


def clean_stocks_for_github(stocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """清理動態快取欄位，避免 GitHub commit 充斥無關歷史變更。"""
    cleaned = []
    for s in stocks:
        item = {
            "symbol": s.get("symbol"),
            "name": s.get("name"),
            "buy_price": s.get("buy_price"),
            "buy_price_2": s.get("buy_price_2"),
            "sell_price": s.get("sell_price"),
            "sell_price_2": s.get("sell_price_2"),
            "notes": s.get("notes", ""),
            "status": s.get("status", "active"),
            "buy_notified": s.get("buy_notified", False),
            "sell_notified": s.get("sell_notified", False),
            "last_alerts": s.get("last_alerts", {}),
        }
        cleaned.append(item)
    return cleaned


def clear_widget_state():
    """徹底清除表格所有 Widget 的 Session State，防止舊 Key 污染新清單。"""
    prefixes = ("buy_price_", "sell_price_", "notes_", "status_", "delete_", "move_")
    for key in list(st.session_state.keys()):
        if any(key.startswith(p) for p in prefixes):
            del st.session_state[key]


def get_remote_watchlist(config: dict[str, Any]) -> list[dict[str, Any]] | None:
    """從 GitHub 下載已同步的監控清單。"""
    token, repo, branch = github_settings(config)
    if not token or not repo:
        return None
    try:
        response = requests.get(
            f"https://api.github.com/repos/{repo}/contents/{GITHUB_WATCHLIST_PATH}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            params={"ref": branch},
            timeout=15,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        encoded_content = response.json().get("content", "")
        payload = json.loads(base64.b64decode(encoded_content).decode("utf-8"))
        stocks = payload.get("stocks", [])
        return stocks if isinstance(stocks, list) else None
    except Exception:
        return None


def sync_watchlist_to_github(stocks: list[dict[str, Any]], config: dict[str, Any]) -> str | None:
    """將清單過濾後上傳至 GitHub。"""
    token, repo, branch = github_settings(config)
    if not token or not repo:
        return None
    try:
        url = f"https://api.github.com/repos/{repo}/contents/{GITHUB_WATCHLIST_PATH}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        current = requests.get(url, headers=headers, params={"ref": branch}, timeout=15)
        sha = current.json().get("sha") if current.ok else None
        if not current.ok and current.status_code != 404:
            current.raise_for_status()

        cleaned_stocks = clean_stocks_for_github(stocks)
        content = json.dumps({"stocks": cleaned_stocks}, ensure_ascii=False, indent=2).encode("utf-8")
        payload: dict[str, Any] = {
            "message": "chore: sync stock watchlist",
            "content": base64.b64encode(content).decode("ascii"),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha
        response = requests.put(url, headers=headers, json=payload, timeout=15)
        response.raise_for_status()
        return None
    except (requests.RequestException, ValueError) as error:
        return f"GitHub 同步失敗：{error}"


def load_watchlist(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """讀取本地 Watchlist，首次進入頁面時嘗試由 GitHub 還原。"""
    data = load_json(WATCHLIST_FILE, {"stocks": []})
    stocks = data.get("stocks", []) if isinstance(data.get("stocks"), list) else []
    # 相容舊版 buy_prices=[第一批, 第二批] 與 note 欄位，避免再次遺失歷史設定。
    for stock in stocks:
        legacy_buy_prices = stock.get("buy_prices")
        if isinstance(legacy_buy_prices, list):
            stock.setdefault("buy_price", legacy_buy_prices[0] if legacy_buy_prices else None)
            stock.setdefault("buy_price_2", legacy_buy_prices[1] if len(legacy_buy_prices) > 1 else None)
        if "notes" not in stock and "note" in stock:
            stock["notes"] = str(stock.get("note") or "")

    # 本機已有清單時絕不以遠端資料覆寫，避免手動備份或剛還原的資料被蓋掉。
    if config and not stocks and not st.session_state.get("remote_watchlist_loaded", False):
        try:
            remote_stocks = get_remote_watchlist(config)
            if remote_stocks is not None:
                stocks = remote_stocks
                save_json(WATCHLIST_FILE, {"stocks": stocks})
                clear_widget_state()
        except Exception:
            pass
        st.session_state.remote_watchlist_loaded = True
    return stocks


def save_watchlist(stocks: list[dict[str, Any]], config: dict[str, Any] | None = None) -> str | None:
    """儲存清單到本地與 GitHub。"""
    save_json(WATCHLIST_FILE, {"stocks": stocks})
    return sync_watchlist_to_github(stocks, config) if config else None


def process_table_action(stocks: list[dict[str, Any]], config: dict[str, Any]) -> None:
    """處理 HTML Flexbox 操作列的網址參數，避免巢狀 Streamlit 欄位。"""
    action = str(st.query_params.get("stock_action", ""))
    symbol = str(st.query_params.get("stock_symbol", "")).upper()
    if action not in {"toggle", "delete", "up", "down"} or not symbol:
        return

    index = next((i for i, stock in enumerate(stocks) if str(stock.get("symbol", "")).upper() == symbol), None)
    if index is None:
        st.query_params.clear()
        return

    changed = False
    if action == "toggle":
        stocks[index]["status"] = "paused" if stocks[index].get("status", "active") == "active" else "active"
        changed = True
    elif action == "delete":
        stocks.pop(index)
        changed = True
    else:
        destination = index - 1 if action == "up" else index + 1
        if 0 <= destination < len(stocks):
            stocks[index], stocks[destination] = stocks[destination], stocks[index]
            changed = True

    st.query_params.clear()
    if changed:
        sync_error = save_watchlist(stocks, config)
        if sync_error:
            st.error(sync_error)
        else:
            st.rerun()


def taiwan_yfinance_symbol(stock_code: str) -> str:
    """依 twstock 判斷上市(.TW)或上櫃(.TWO)。"""
    stock_info = twstock.codes.get(stock_code)
    if stock_info and stock_info.market == "上櫃":
        return f"{stock_code}.TWO"
    return f"{stock_code}.TW"


def normalize_stock_symbol(raw_symbol: str) -> str:
    """支援 4~6 位數台股代碼、ETF 與中文模糊搜尋。"""
    symbol = raw_symbol.strip().upper()

    if re.fullmatch(r"\d{4,6}[A-Z]?", symbol):
        return taiwan_yfinance_symbol(symbol)

    if re.search(r"[\u4e00-\u9fff]", raw_symbol):
        search_kw = raw_symbol.strip()
        matched_stocks = [
            info for info in twstock.codes.values() if search_kw in info.name
        ]
        if not matched_stocks:
            raise ValueError(f"找不到包含「{raw_symbol}」的台股名稱，請嘗試輸入完整代碼。")
        return taiwan_yfinance_symbol(matched_stocks[0].code)

    return symbol


@st.cache_data(ttl=86400, show_spinner=False)
def get_stock_name(symbol: str) -> str:
    """取得股票名稱 (修正未知名稱避免無效重試)。"""
    taiwan_match = re.fullmatch(r"(\d{4,6}[A-Z]?)\.(?:TW|TWO)", symbol.upper())
    if taiwan_match:
        stock_info = twstock.codes.get(taiwan_match.group(1))
        if stock_info:
            return stock_info.name

    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info
        name = str(info.get("longName") or info.get("shortName") or "")
        return name if name else symbol
    except Exception:
        return symbol


def fetch_latest_prices(symbols: list[str]) -> dict[str, float]:
    """批次拉取多檔股票最新價格 (修復 FastInfo 物件屬性讀取)。"""
    if not symbols:
        return {}

    prices: dict[str, float] = {}
    try:
        data = yf.download(tickers=symbols, period="1d", interval="1m", progress=False)

        if not data.empty and "Close" in data:
            close_data = data["Close"]
            for sym in symbols:
                try:
                    if len(symbols) == 1:
                        series = close_data.dropna()
                        if not series.empty:
                            val = series.iloc[-1]
                            prices[sym] = float(val.iloc[0]) if hasattr(val, "iloc") else float(val)
                    else:
                        if sym in close_data:
                            series = close_data[sym].dropna()
                            if not series.empty:
                                prices[sym] = float(series.iloc[-1])
                except Exception:
                    pass

        # 針對沒抓到 1m 價格的股票，使用 FastInfo 屬性或 5d 歷史退回
        missing_symbols = [s for s in symbols if s not in prices]
        for sym in missing_symbols:
            try:
                ticker = yf.Ticker(sym)
                fast_info = ticker.fast_info
                fast_price = getattr(fast_info, "last_price", None) or getattr(
                    fast_info, "regular_market_previous_close", None
                )
                if fast_price is not None:
                    prices[sym] = float(fast_price)
                else:
                    hist = ticker.history(period="5d", interval="1d")
                    if not hist.empty and not hist["Close"].dropna().empty:
                        prices[sym] = float(hist["Close"].dropna().iloc[-1])
            except Exception:
                pass
    except Exception:
        pass

    return prices


def send_line_message(channel_access_token: str, user_id: str, message: str) -> None:
    """透過 LINE Messaging API 發送 Push Message。"""
    response = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={
            "Authorization": f"Bearer {channel_access_token}",
            "Content-Type": "application/json",
        },
        json={
            "to": user_id,
            "messages": [{"type": "text", "text": message}],
            "notificationDisabled": False,
        },
        timeout=15,
    )
    response.raise_for_status()


def line_user_ids(config: dict[str, Any]) -> list[str]:
    """將逗號（或換行）分隔的 LINE User ID 正規化並去除重複值。"""
    raw_ids = str(config.get("line_user_id", "")).replace("\n", ",").split(",")
    return list(dict.fromkeys(user_id.strip() for user_id in raw_ids if user_id.strip()))


def can_notify(stock: dict[str, Any], alert_type: str, now: float) -> bool:
    """判斷是否超過冷卻時間。"""
    last_alerts = stock.get("last_alerts", {})
    last_sent = float(last_alerts.get(alert_type, 0))
    return (now - last_sent) >= COOLDOWN_SECONDS


def should_report_market_closed(symbol: str, now: float) -> bool:
    """休市節流：同一標的沒有即時價格時，每 30 分鐘才提示一次。"""
    key = f"market_closed_report_{symbol}"
    last_reported = float(st.session_state.get(key, 0))
    if now - last_reported < MARKET_CLOSED_LOG_THROTTLE_SECONDS:
        return False
    st.session_state[key] = now
    return True


def reset_notification_state(stock: dict[str, Any], alert_type: str) -> None:
    """目標價變更後，清除該方向的已通知狀態。"""
    stock[f"{alert_type}_notified"] = False
    stock.setdefault("last_alerts", {}).pop(alert_type, None)


def run_monitor(stocks: list[dict[str, Any]], config: dict[str, Any]) -> list[str]:
    """檢查所有股票，優化到價提醒與冷卻機制。"""
    messages: list[str] = []
    channel_access_token = str(config.get("line_channel_access_token", "")).strip()
    user_ids = line_user_ids(config)
    now = time.time()
    notification_state_changed = False

    active_stocks = [s for s in stocks if s.get("status", "active") == "active"]
    active_symbols = [str(s.get("symbol", "")).upper() for s in active_stocks if s.get("symbol")]

    fetched_prices = fetch_latest_prices(active_symbols)

    for stock in stocks:
        symbol = str(stock.get("symbol", "")).upper()
        if stock.get("status", "active") != "active":
            messages.append(f"⚪ {symbol}: 監控已暫停")
            continue

        price = fetched_prices.get(symbol)
        if price is None:
            # 休市節流：保留休市／無報價提示，但避免自動刷新時洗版。
            if should_report_market_closed(symbol, now):
                messages.append(f"⚪ {symbol}: 休市中或暫時無法取得即時價格")
            continue

        stock["last_price"] = price
        stock["last_checked"] = datetime.now(TAIPEI_TZ).strftime("%H:%M")
        messages.append(f"🔹 {symbol}: {price:,.2f}")

        # 提醒完全由 30 分鐘冷卻控制；不需要價格回彈才可再次提醒。
        buy_price = stock.get("buy_price")
        buy_price_2 = stock.get("buy_price_2")
        sell_price = stock.get("sell_price")
        sell_price_2 = stock.get("sell_price_2")

        alerts: list[tuple[str, float]] = []

        if buy_price is not None and float(buy_price) > 0 and price <= float(buy_price):
            if can_notify(stock, "buy", now):
                alerts.append(("buy", float(buy_price)))

        if buy_price_2 is not None and float(buy_price_2) > 0 and price <= float(buy_price_2):
            if can_notify(stock, "buy_2", now):
                alerts.append(("buy_2", float(buy_price_2)))

        if sell_price is not None and float(sell_price) > 0 and price >= float(sell_price):
            if can_notify(stock, "sell", now):
                alerts.append(("sell", float(sell_price)))

        if sell_price_2 is not None and float(sell_price_2) > 0 and price >= float(sell_price_2):
            if can_notify(stock, "sell_2", now):
                alerts.append(("sell_2", float(sell_price_2)))

        for alert_type, target in alerts:
            verb = "第 2 批買入" if alert_type == "buy_2" else (
                "第 1 批買入" if alert_type == "buy" else ("第 2 批賣出" if alert_type == "sell_2" else "第 1 批賣出")
            )
            text = f"📈 {symbol} 已觸發{verb}價提醒\n目前價格：{price:,.2f}\n目標價格：{target:,.2f}"

            if channel_access_token and user_ids:
                try:
                    for user_id in user_ids:
                        send_line_message(channel_access_token, user_id, text)
                    stock[f"{alert_type}_notified"] = True
                    stock.setdefault("last_alerts", {})[alert_type] = now
                    notification_state_changed = True
                    messages.append(f"✅ 已發送 LINE：{symbol} {verb}價提醒")
                except Exception as e:
                    messages.append(f"❌ {symbol} LINE 發送失敗：{e}")
            else:
                stock.setdefault("last_alerts", {})[alert_type] = now
                messages.append(f"🔔 {symbol} 已觸發{verb}價（未設定 LINE 憑證）")

    if notification_state_changed:
        save_watchlist(stocks, config)

    return messages


# --- 介面渲染 ---

st.set_page_config(page_title="股票到價提醒", page_icon="📈", layout="wide")
st.markdown(
    """<style>
    /* 以直接輸入為主，收起 number_input 預設的加減控制鈕 */
    div[data-testid="stNumberInput"] button { display: none; }
    div[data-testid="stNumberInput"] input { text-align: right; }
    /* 清單專用的緊湊排版：縮小欄距、欄位內距與操作鈕高度。 */
    /* Streamlit 預設 gap 會與欄位未使用寬度疊加；直接歸零，由比例控制空間。 */
    [data-testid="stHorizontalBlock"], [data-testid="stHorizontalBlock"] > div {
        gap: 0 !important; column-gap: 0 !important; row-gap: 0 !important;
    }
    [data-testid="stHorizontalBlock"] > [data-testid="stColumn"],
    [data-testid="stHorizontalBlock"] > [data-testid="column"],
    [data-testid="stColumn"], [data-testid="column"] {
        min-width: 0 !important; margin: 0 !important; padding: 0 !important;
    }
    div[data-testid="stButton"] { margin: 0 !important; padding: 0 !important; }
    div[data-testid="stButton"] > button { width: 100% !important; min-width: 0 !important; min-height: 1.55rem; height: 1.55rem; padding: 0; font-size: .75rem; line-height: 1; }
    div[data-testid="stNumberInput"] input, div[data-testid="stTextInput"] input { min-height: 1.7rem; padding: .1rem .3rem; }
    .stock-actions { display: flex !important; flex-flow: row nowrap !important; align-items: center !important; justify-content: space-between !important; gap: .12rem !important; min-width: 0 !important; white-space: nowrap !important; }
    .stock-action { display: inline-flex !important; flex: 1 1 0 !important; min-width: 0 !important; height: 1.55rem !important; align-items: center !important; justify-content: center !important; padding: 0 !important; border: 1px solid rgba(151, 166, 195, .35) !important; border-radius: .35rem !important; color: inherit !important; text-decoration: none !important; font-size: .78rem !important; line-height: 1 !important; }
    .stock-action:hover { background: rgba(255, 255, 255, .10) !important; border-color: rgba(255, 255, 255, .65) !important; }
    .price-buy { color: #5c4400; font-weight: 800; background: #ffd43b; padding: .15rem .4rem; border-radius: .3rem; }
    .price-sell { color: #ffffff; font-weight: 800; background: #e03131; padding: .15rem .4rem; border-radius: .3rem; }
    </style>""",
    unsafe_allow_html=True,
)
st.title("📈 股票關鍵價位監控")
st.caption("到價 LINE 提醒與即時價格對照")

config = init_config()
stocks = load_watchlist(config)
process_table_action(stocks, config)

with st.sidebar:
    st.header("新增股票")
    st.caption("支援 4~6 位代碼（如 2330、00878）、中文名稱（如 台積電）或美股（如 NVDA）")
    with st.form("add_stock_form", clear_on_submit=True):
        stock_input = st.text_input("股票代碼／名稱", placeholder="例如：2330、00878、台積電、NVDA")
        buy_price = st.number_input("第 1 批買入價（0 代表不設定）", min_value=0.0, value=0.0, step=0.01)
        buy_price_2 = st.number_input("第 2 批買入價（0 代表不設定）", min_value=0.0, value=0.0, step=0.01)
        sell_price = st.number_input("第 1 批賣出價（0 代表不設定）", min_value=0.0, value=0.0, step=0.01)
        sell_price_2 = st.number_input("第 2 批賣出價（0 代表不設定）", min_value=0.0, value=0.0, step=0.01)
        notes = st.text_input("決策思維／進出場備註（選填）")
        add_clicked = st.form_submit_button("新增股票", use_container_width=True)

    if add_clicked:
        if not stock_input.strip():
            st.error("請輸入股票代碼。")
        elif buy_price == 0 and buy_price_2 == 0 and sell_price == 0 and sell_price_2 == 0:
            st.error("請至少設定一個價格。")
        else:
            try:
                symbol = normalize_stock_symbol(stock_input)
                if any(item.get("symbol", "").upper() == symbol for item in stocks):
                    st.error("此股票代碼已在監控清單中。")
                else:
                    stocks.append({
                        "symbol": symbol,
                        "name": get_stock_name(symbol),
                        "buy_price": round(buy_price, 4) if buy_price > 0 else None,
                        "buy_price_2": round(buy_price_2, 4) if buy_price_2 > 0 else None,
                        "sell_price": round(sell_price, 4) if sell_price > 0 else None,
                        "sell_price_2": round(sell_price_2, 4) if sell_price_2 > 0 else None,
                        "notes": notes.strip(),
                        "status": "active",
                        "buy_notified": False,
                        "sell_notified": False,
                        "last_alerts": {},
                    })
                    sync_error = save_watchlist(stocks, config)
                    if sync_error:
                        st.error(sync_error)
                    else:
                        st.success(f"已加入 {symbol}")
                        st.rerun()
            except ValueError as error:
                st.error(str(error))

    st.divider()
    with st.expander("💾 備份與 GitHub 同步", expanded=False):
        st.download_button(
            "匯出監控清單 JSON",
            data=json.dumps({"stocks": clean_stocks_for_github(stocks)}, ensure_ascii=False, indent=2),
            file_name="watchlist-backup.json",
            mime="application/json",
            use_container_width=True,
        )
        backup_file = st.file_uploader("匯入監控清單 JSON", type=["json"])
        if backup_file and st.button("匯入並覆蓋目前清單", use_container_width=True):
            try:
                backup_data = json.loads(backup_file.getvalue().decode("utf-8"))
                imported_stocks = backup_data.get("stocks")
                if not isinstance(imported_stocks, list):
                    raise ValueError("備份檔案缺少 stocks 清單。")
                sync_error = save_watchlist(imported_stocks, config)
                clear_widget_state()
                if sync_error:
                    st.error(sync_error)
                else:
                    st.success("監控清單已匯入。")
                    st.rerun()
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                st.error(f"無法匯入備份：{error}")

        st.caption("填寫 Token 與 Repo 後，清單變動時會自動同步至 GitHub。")
        with st.form("github_form"):
            github_token = st.text_input("GitHub Token", value=config.get("github_token", ""), type="password")
            github_repo = st.text_input("GitHub Repo", value=config.get("github_repo", ""), placeholder="owner/repository")
            github_branch = st.text_input("GitHub Branch", value=config.get("github_branch", "main"))
            save_github_config = st.form_submit_button("儲存 GitHub 同步設定", use_container_width=True)

            # 💡 已修正：放進 form 裡面，按下按鈕時能準確抓到剛填寫的 Token 並存入 config.json
            if save_github_config:
                update_config({
                    "github_token": github_token.strip(),
                    "github_repo": github_repo.strip(),
                    "github_branch": github_branch.strip() or "main",
                })
                st.session_state.remote_watchlist_loaded = False
                sync_error = save_watchlist(stocks, st.session_state.config)
                clear_widget_state()
                if sync_error:
                    st.error(sync_error)
                else:
                    st.success("GitHub 同步設定已儲存。")
                    st.rerun()

    with st.popover("⚙️ 系統設定", use_container_width=True):
        with st.form("system_settings_form"):
            refresh_seconds = st.selectbox(
                "重新整理頻率",
                options=[30, 60, 180, 300, 600],
                index=[30, 60, 180, 300, 600].index(int(config.get("refresh_seconds", 60)))
                if int(config.get("refresh_seconds", 60)) in [30, 60, 180, 300, 600] else 1,
                format_func=lambda seconds: f"{seconds // 60} 分鐘" if seconds >= 60 else f"{seconds} 秒",
            )
            if st.form_submit_button("儲存系統設定", use_container_width=True):
                update_config({"refresh_seconds": refresh_seconds})
                st.success("系統設定已儲存。")
                st.rerun()

    with st.popover("⚙️ LINE 設定", use_container_width=True):
        st.caption("儲存憑證或發送測試通知。")
        line_channel_access_token = st.text_input(
            "LINE Channel Access Token",
            value=config.get("line_channel_access_token", ""),
            type="password",
            key="line_channel_access_token_input",
        )
        line_user_id = st.text_input(
            "LINE User ID（可用逗號分隔多位）",
            value=config.get("line_user_id", ""),
            type="password",
            key="line_user_id_input",
        )
        save_config = st.button("儲存 LINE 設定", use_container_width=True)
        test_line_message = st.button("🧪 發送測試通知", use_container_width=True)

        if save_config:
            update_config({
                "line_channel_access_token": line_channel_access_token.strip(),
                "line_user_id": line_user_id.strip(),
            })
            st.success("LINE 設定已儲存。")
            st.rerun()

        if test_line_message:
            try:
                test_ids = [item.strip() for item in line_user_id.replace("\n", ",").split(",") if item.strip()]
                if not line_channel_access_token.strip() or not test_ids:
                    raise ValueError("請先輸入 LINE Channel Access Token 與 LINE User ID。")
                for test_id in test_ids:
                    send_line_message(line_channel_access_token.strip(), test_id, "LINE 機器人連線成功測試！")
                st.success("測試通知已發送。")
            except (requests.RequestException, ValueError) as error:
                st.error(f"測試通知發送失敗：{error}")

# 主監控區域
refresh_seconds = int(config.get("refresh_seconds", 60))
monitoring = st.toggle(
    f"啟動監控（每 {refresh_seconds // 60} 分鐘檢查一次）" if refresh_seconds >= 60 else f"啟動監控（每 {refresh_seconds} 秒檢查一次）",
    value=True,
    key="monitoring",
)

st.subheader("監控清單")
if not stocks:
    st.info("尚未加入任何股票。")
else:
    save_refresh = st.button("💾 儲存/重新整理", type="primary")

    if monitoring or save_refresh:
        if monitoring:
            st_autorefresh(interval=refresh_seconds * 1_000, key="stock_monitor_refresh")
        with st.spinner("正在更新價格與檢查提醒…"):
            monitor_messages = run_monitor(stocks, config)
        
        log_column, sort_column = st.columns([2.2, 1.3], gap="small")
        with log_column:
            with st.expander("🔍 即時檢查日誌", expanded=False):
                for msg in monitor_messages:
                    st.write(msg)
        with sort_column:
            sort_mode = st.radio(
                "排序",
                ["手動排序", "依代碼排序"],
                horizontal=True,
                label_visibility="collapsed",
            )
    else:
        st.caption("監控目前已停止；可按「💾 儲存/重新整理」手動刷新報價。")
        sort_mode = st.radio("排序", ["手動排序", "依代碼排序"], horizontal=True, label_visibility="collapsed")

    watchlist_changed = False

    # 代碼／名稱／時間只保留足夠比例，避免空白欄位吞掉操作區的寬度。
    column_widths = [0.46, 0.50, 1.03, 1.03, 1.03, 1.03, 0.56, 0.50, 1.60, 2.05]
    header = st.columns(column_widths, gap="small")
    header[0].markdown("**股票代碼**")
    header[1].markdown("**股票名稱**")
    header[2].markdown("<span style='color:#00c853; font-weight:bold;'>第 1 批買入</span>", unsafe_allow_html=True)
    header[3].markdown("<span style='color:#00c853; font-weight:bold;'>第 2 批買入</span>", unsafe_allow_html=True)
    header[4].markdown("<span style='color:#ff5252; font-weight:bold;'>第 1 批賣出</span>", unsafe_allow_html=True)
    header[5].markdown("<span style='color:#ff5252; font-weight:bold;'>第 2 批賣出</span>", unsafe_allow_html=True)
    header[6].markdown("**最新價格**")
    header[7].markdown("**最後檢查**")
    header[8].markdown("**決策思維／進出場備註**")
    header[9].markdown("**操作**")

    active_items = [(index, stock) for index, stock in enumerate(stocks) if stock.get("status", "active") == "active"]
    if sort_mode == "依代碼排序":
        active_items.sort(key=lambda item: str(item[1].get("symbol", "")))

    def render_stock_row(index: int, stock: dict[str, Any], archived: bool = False) -> bool:
        """顯示一列股票設定；暫停項目維持資料但不參與報價與通知。"""
        row_changed = False
        columns = st.columns(column_widths, gap="small")
        symbol = str(stock.get("symbol", "-"))
        columns[0].write(symbol)

        name = stock.get("name")
        if not name:
            name = get_stock_name(symbol)
            stock["name"] = name
        columns[1].write(name)

        current_buy = float(stock.get("buy_price") or 0.0)
        current_buy_2 = float(stock.get("buy_price_2") or 0.0)
        current_sell = float(stock.get("sell_price") or 0.0)
        current_sell_2 = float(stock.get("sell_price_2") or 0.0)
        current_notes = str(stock.get("notes", ""))

        new_buy = columns[2].number_input(
            "第 1 批買入價",
            min_value=0.0,
            value=current_buy,
            step=0.01,
            label_visibility="collapsed",
            key=f"buy_price_{symbol}",
        )
        new_buy_2 = columns[3].number_input(
            "第 2 批買入價",
            min_value=0.0,
            value=current_buy_2,
            step=0.01,
            label_visibility="collapsed",
            key=f"buy_price_2_{symbol}",
        )
        new_sell = columns[4].number_input(
            "第 1 批賣出價",
            min_value=0.0,
            value=current_sell,
            step=0.01,
            label_visibility="collapsed",
            key=f"sell_price_{symbol}",
        )
        new_sell_2 = columns[5].number_input("第 2 批賣出價", min_value=0.0, value=current_sell_2, step=0.01,
                                              label_visibility="collapsed", key=f"sell_price_2_{symbol}")
        price = stock.get("last_price")
        price_markup = "-"
        if price is not None:
            numeric_price = float(price)
            buy_nearby = any(target > 0 and numeric_price <= target * 1.01 for target in (current_buy, current_buy_2))
            sell_nearby = any(target > 0 and numeric_price >= target * 0.99 for target in (current_sell, current_sell_2))
            # 賣出訊號優先紅底、買入訊號為黃底；批次不影響視覺判讀。
            css_class = "price-sell" if sell_nearby else ("price-buy" if buy_nearby else "")
            price_markup = f"<span class='{css_class}'>{numeric_price:,.2f}</span>" if css_class else f"{numeric_price:,.2f}"
        columns[6].markdown(price_markup, unsafe_allow_html=True)
        checked_at = str(stock.get("last_checked", "尚未檢查"))
        # 相容舊資料的「月-日 時:分:秒」格式；畫面一律只顯示時:分。
        time_match = re.search(r"(\d{1,2}:\d{2})(?::\d{2})?", checked_at)
        columns[7].write(time_match.group(1) if time_match else checked_at)
        new_notes = columns[8].text_input(
            "決策思維／進出場備註",
            value=current_notes,
            label_visibility="collapsed",
            key=f"notes_{symbol}",
            placeholder="例：突破後分批進場",
        )

        for field, alert_type, new_price in (
            ("buy_price", "buy", new_buy), ("buy_price_2", "buy_2", new_buy_2),
            ("sell_price", "sell", new_sell),
            ("sell_price_2", "sell_2", new_sell_2),
        ):
            normalized_price = None if new_price <= 0 else round(float(new_price), 4)
            if stock.get(field) != normalized_price:
                stock[field] = normalized_price
                if alert_type:
                    reset_notification_state(stock, alert_type)
                row_changed = True

        if stock.get("notes", "") != new_notes.strip():
            stock["notes"] = new_notes.strip()
            row_changed = True

        is_paused = stock.get("status", "active") != "active"
        encoded_symbol = quote(symbol, safe="")
        status_icon = "▶" if is_paused else "⏸"
        status_title = "恢復監控" if is_paused else "暫停監控"
        columns[9].markdown(
            f'''<div class="stock-actions">
                <a class="stock-action" href="?stock_action=toggle&amp;stock_symbol={encoded_symbol}" title="{status_title}" aria-label="{status_title}">{status_icon}</a>
                <a class="stock-action" href="?stock_action=delete&amp;stock_symbol={encoded_symbol}" title="刪除這檔股票" aria-label="刪除這檔股票">🗑</a>
                <a class="stock-action" href="?stock_action=up&amp;stock_symbol={encoded_symbol}" title="在手動排序中上移" aria-label="在手動排序中上移">↑</a>
                <a class="stock-action" href="?stock_action=down&amp;stock_symbol={encoded_symbol}" title="在手動排序中下移" aria-label="在手動排序中下移">↓</a>
            </div>''',
            unsafe_allow_html=True,
        )
        return row_changed

    for index, stock in active_items:
        watchlist_changed = render_stock_row(index, stock) or watchlist_changed

    archived_items = [(index, stock) for index, stock in enumerate(stocks) if stock.get("status", "active") != "active"]
    if archived_items:
        with st.expander(f"📦 已出清／暫停庫存（{len(archived_items)}）", expanded=False):
            for index, stock in archived_items:
                watchlist_changed = render_stock_row(index, stock, archived=True) or watchlist_changed

    if watchlist_changed:
        sync_error = save_watchlist(stocks, config)
        if sync_error:
            st.error(sync_error)
        else:
            st.success("監控清單已儲存。")
            st.rerun()
