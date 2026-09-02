"""Streamlit 股票關鍵價位監控工具 (終極完美修復版)。"""

from __future__ import annotations

import base64
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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
    if "config" not in st.session_state:
        default_config = {
            "line_channel_access_token": "",
            "line_user_id": "",
            "github_token": "",
            "github_repo": "",
            "github_branch": "main",
        }
        st.session_state.config = load_json(CONFIG_FILE, default_config)
    return st.session_state.config


def update_config(new_settings: dict[str, Any]) -> None:
    """同步更新 Session State 與硬碟中的 config.json。"""
    if "config" not in st.session_state:
        st.session_state.config = {}
    st.session_state.config.update(new_settings)
    save_json(CONFIG_FILE, st.session_state.config)


def github_settings(config: dict[str, Any]) -> tuple[str, str, str]:
    """優先讀取 Streamlit secrets，其次讀取介面儲存的 GitHub 設定。"""
    token, repo, branch = "", "", "main"
    try:
        if "github_token" in st.secrets and str(st.secrets["github_token"]).strip():
            token = str(st.secrets["github_token"]).strip()
        if "github_repo" in st.secrets and str(st.secrets["github_repo"]).strip():
            repo = str(st.secrets["github_repo"]).strip()
        if "github_branch" in st.secrets and str(st.secrets["github_branch"]).strip():
            branch = str(st.secrets["github_branch"]).strip()
    except Exception:
        pass

    if not token:
        token = str(config.get("github_token", "")).strip()
    if not repo:
        repo = str(config.get("github_repo", "")).strip()
    if not branch or branch == "main":
        branch = str(config.get("github_branch", "main")).strip() or "main"

    return token, repo, branch


def clean_stocks_for_github(stocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """清理動態快取欄位，避免 GitHub commit 充斥無關歷史變更。"""
    cleaned = []
    for s in stocks:
        item = {
            "symbol": s.get("symbol"),
            "name": s.get("name"),
            "buy_price": s.get("buy_price"),
            "sell_price": s.get("sell_price"),
            "status": s.get("status", "active"),
            "buy_notified": s.get("buy_notified", False),
            "sell_notified": s.get("sell_notified", False),
            "last_alerts": s.get("last_alerts", {}),
        }
        cleaned.append(item)
    return cleaned


def clear_widget_state():
    """徹底清除表格所有 Widget 的 Session State，防止舊 Key 污染新清單。"""
    prefixes = ("buy_price_", "sell_price_", "status_", "delete_")
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

    if config and not st.session_state.get("remote_watchlist_loaded", False):
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
                # 修復：FastInfo 是物件而非 dict，必須使用 getattr 讀取屬性而非 .get()
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


def can_notify(stock: dict[str, Any], alert_type: str, now: float) -> bool:
    """判斷是否超過冷卻時間。"""
    last_alerts = stock.get("last_alerts", {})
    last_sent = float(last_alerts.get(alert_type, 0))
    return (now - last_sent) >= COOLDOWN_SECONDS


def reset_notification_state(stock: dict[str, Any], alert_type: str) -> None:
    """目標價變更後，清除該方向的已通知狀態。"""
    stock[f"{alert_type}_notified"] = False
    stock.setdefault("last_alerts", {}).pop(alert_type, None)


def run_monitor(stocks: list[dict[str, Any]], config: dict[str, Any]) -> list[str]:
    """檢查所有股票，優化到價提醒與冷卻機制。"""
    messages: list[str] = []
    channel_access_token = str(config.get("line_channel_access_token", "")).strip()
    user_id = str(config.get("line_user_id", "")).strip()
    now = time.time()
    notification_state_changed = False

    active_stocks = [s for s in stocks if s.get("status", "active") != "paused"]
    active_symbols = [str(s.get("symbol", "")).upper() for s in active_stocks if s.get("symbol")]

    fetched_prices = fetch_latest_prices(active_symbols)

    for stock in stocks:
        symbol = str(stock.get("symbol", "")).upper()
        if stock.get("status", "active") == "paused":
            messages.append(f"⚪ {symbol}: 監控已暫停")
            continue

        price = fetched_prices.get(symbol)
        if price is None:
            messages.append(f"⚠️ {symbol}: 暫時無法取得價格")
            continue

        stock["last_price"] = price
        stock["last_checked"] = datetime.now(TAIPEI_TZ).strftime("%m-%d %H:%M:%S")
        messages.append(f"🔹 {symbol}: {price:,.2f}")

        # 價格回到安全區間時自動重置提醒 flag
        buy_price = stock.get("buy_price")
        sell_price = stock.get("sell_price")
        if buy_price and price > float(buy_price) * 1.01:
            stock["buy_notified"] = False
        if sell_price and price < float(sell_price) * 0.99:
            stock["sell_notified"] = False

        alerts: list[tuple[str, float]] = []

        if buy_price is not None and float(buy_price) > 0 and price <= float(buy_price):
            if can_notify(stock, "buy", now):
                alerts.append(("buy", float(buy_price)))

        if sell_price is not None and float(sell_price) > 0 and price >= float(sell_price):
            if can_notify(stock, "sell", now):
                alerts.append(("sell", float(sell_price)))

        for alert_type, target in alerts:
            verb = "買入" if alert_type == "buy" else "賣出"
            text = f"📈 {symbol} 已觸發{verb}價提醒\n目前價格：{price:,.2f}\n目標價格：{target:,.2f}"

            if channel_access_token and user_id:
                try:
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
st.title("📈 股票關鍵價位監控")
st.caption("使用 yfinance 取得報價，觸及買入／賣出目標價時發送 LINE 通知。")

config = init_config()
stocks = load_watchlist(config)

with st.sidebar:
    st.header("新增股票")
    st.caption("支援 4~6 位代碼（如 2330、00878）、中文名稱（如 台積電）或美股（如 NVDA）")
    with st.form("add_stock_form", clear_on_submit=True):
        stock_input = st.text_input("股票代碼／名稱", placeholder="例如：2330、00878、台積電、NVDA")
        buy_price = st.number_input("目標買入價（0 代表不設定）", min_value=0.0, value=0.0, step=0.01)
        sell_price = st.number_input("目標賣出價（0 代表不設定）", min_value=0.0, value=0.0, step=0.01)
        add_clicked = st.form_submit_button("新增股票", use_container_width=True)

    if add_clicked:
        if not stock_input.strip():
            st.error("請輸入股票代碼。")
        elif buy_price == 0 and sell_price == 0:
            st.error("請至少設定一個買入價或賣出價。")
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
                        "sell_price": round(sell_price, 4) if sell_price > 0 else None,
                        "status": "active",
                        "buy_notified": False,
                        "sell_notified": False,
                        "last_alerts": {},
                    })
                    sync_error = save_watchlist(stocks, config)
                    if sync_error:
                        st.error(sync_error)  # 修復：有錯誤時不呼叫 rerun 以確保警告維持在畫面上
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

    with st.popover("⚙️ LINE 設定", use_container_width=True):
        st.caption("儲存憑證或發送測試通知。")
        line_channel_access_token = st.text_input(
            "LINE Channel Access Token",
            value=config.get("line_channel_access_token", ""),
            type="password",
            key="line_channel_access_token_input",
        )
        line_user_id = st.text_input(
            "LINE User ID",
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
                if not line_channel_access_token.strip() or not line_user_id.strip():
                    raise ValueError("請先輸入 LINE Channel Access Token 與 LINE User ID。")
                send_line_message(
                    line_channel_access_token.strip(),
                    line_user_id.strip(),
                    "LINE 機器人連線成功測試！",
                )
                st.success("測試通知已發送。")
            except (requests.RequestException, ValueError) as error:
                st.error(f"測試通知發送失敗：{error}")

# 主監控區域
monitoring = st.toggle("啟動監控（每 60 秒檢查一次）", value=True, key="monitoring")

st.subheader("監控清單")
if not stocks:
    st.info("尚未加入任何股票。")
else:
    save_refresh = st.button("💾 儲存/重新整理", type="primary")

    if monitoring or save_refresh:
        if monitoring:
            st_autorefresh(interval=60_000, key="stock_monitor_refresh")
        with st.spinner("正在更新價格與檢查提醒…"):
            monitor_messages = run_monitor(stocks, config)
        
        # 修復：Log 逐行展示，避免拼成一行混亂長字串
        with st.expander("🔍 即時檢查日誌", expanded=True):
            for msg in monitor_messages:
                st.write(msg)
    else:
        st.caption("監控目前已停止；可按「💾 儲存/重新整理」手動刷新報價。")

    watchlist_changed = False
    delete_indices: list[int] = []

    header = st.columns([1.35, 1.45, 1.45, 1.45, 1.3, 1.55, 2.1])
    header[0].markdown("**股票代碼**")
    header[1].markdown("**股票名稱**")
    header[2].markdown("<span style='color:#00c853; font-weight:bold;'>目標買入價</span>", unsafe_allow_html=True)
    header[3].markdown("<span style='color:#ff5252; font-weight:bold;'>目標賣出價</span>", unsafe_allow_html=True)
    header[4].markdown("**最新價格**")
    header[5].markdown("**最後檢查時間**")
    header[6].markdown("**操作**")

    for index, stock in enumerate(stocks):
        columns = st.columns([1.35, 1.45, 1.45, 1.45, 1.3, 1.55, 2.1])
        symbol = str(stock.get("symbol", "-"))
        columns[0].write(symbol)

        # 讀取股票名稱；若無則自動補充
        name = stock.get("name")
        if not name:
            name = get_stock_name(symbol)
            stock["name"] = name
        columns[1].write(name)

        current_buy = float(stock.get("buy_price") or 0.0)
        current_sell = float(stock.get("sell_price") or 0.0)

        # 修復：移除 key 中的 index，避免列表項目刪除遞移時產生 Session State 錯位
        new_buy = columns[2].number_input(
            "買入價",
            min_value=0.0,
            value=current_buy,
            step=0.01,
            label_visibility="collapsed",
            key=f"buy_price_{symbol}",
        )
        new_sell = columns[3].number_input(
            "賣出價",
            min_value=0.0,
            value=current_sell,
            step=0.01,
            label_visibility="collapsed",
            key=f"sell_price_{symbol}",
        )

        columns[4].write("-" if stock.get("last_price") is None else f"{float(stock['last_price']):,.2f}")
        columns[5].write(stock.get("last_checked", "尚未檢查"))

        # 更新目標價判斷
        for field, alert_type, new_price in (("buy_price", "buy", new_buy), ("sell_price", "sell", new_sell)):
            normalized_price = None if new_price <= 0 else round(float(new_price), 4)
            if stock.get(field) != normalized_price:
                stock[field] = normalized_price
                reset_notification_state(stock, alert_type)
                watchlist_changed = True

        action_columns = columns[6].columns(2)
        is_paused = stock.get("status", "active") == "paused"
        status_label = "🟢 啟用" if is_paused else "🟡 暫停"

        if action_columns[0].button(status_label, key=f"status_{symbol}", use_container_width=True):
            stock["status"] = "active" if is_paused else "paused"
            watchlist_changed = True

        if action_columns[1].button("🔴 刪除", key=f"delete_{symbol}", use_container_width=True):
            delete_indices.append(index)
            watchlist_changed = True

    for index in reversed(delete_indices):
        stocks.pop(index)

    if watchlist_changed:
        sync_error = save_watchlist(stocks, config)
        if sync_error:
            st.error(sync_error)
        else:
            st.success("監控清單已儲存。")
            st.rerun()