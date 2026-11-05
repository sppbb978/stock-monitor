"""Streamlit 股票關鍵價位監控工具。"""
from __future__ import annotations

import base64
import json
import re
import time
from datetime import datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
import streamlit as st
import twstock
import yfinance as yf
from streamlit_autorefresh import st_autorefresh

try:
    from zoneinfo import ZoneInfo
    TAIPEI_TZ = ZoneInfo("Asia/Taipei")
except Exception:
    TAIPEI_TZ = timezone(timedelta(hours=8))

BASE_DIR = Path(__file__).resolve().parent
WATCHLIST_FILE = BASE_DIR / "watchlist.json"
CONFIG_FILE = BASE_DIR / "config.json"
GITHUB_WATCHLIST_PATH = "watchlist.json"
COOLDOWN_SECONDS = 30 * 60
REFRESH_OPTIONS = {"30 秒": 30, "60 秒": 60, "3 分鐘": 180, "5 分鐘": 300}


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as file:
            value = json.load(file)
        return value if isinstance(value, dict) else default.copy()
    except (OSError, json.JSONDecodeError):
        return default.copy()


def save_json(path: Path, data: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def init_config() -> dict[str, Any]:
    """Session 與檔案共用同一份設定；所有更新必須走 update_config。"""
    if "config" not in st.session_state:
        defaults = {"line_channel_access_token": "", "line_user_id": "", "github_token": "", "github_repo": "", "github_branch": "main", "refresh_seconds": 60}
        defaults.update(load_json(CONFIG_FILE, {}))
        st.session_state.config = defaults
    return st.session_state.config


def update_config(settings: dict[str, Any]) -> dict[str, Any]:
    config = init_config()
    config.update(settings)
    save_json(CONFIG_FILE, config)
    return config


def clear_widget_state() -> None:
    """只清除資料列 widgets，絕不影響 cfg_ 或 LINE 憑證 widgets。"""
    for key in list(st.session_state):
        if key.startswith(("buy1_", "buy2_", "sell_", "note_", "status_", "delete_")):
            del st.session_state[key]


def github_settings(config: dict[str, Any]) -> tuple[str, str, str]:
    try:
        secrets = st.secrets
    except Exception:
        secrets = {}
    token = str(secrets.get("github_token", config.get("github_token", ""))).strip()
    repo = str(secrets.get("github_repo", config.get("github_repo", ""))).strip()
    branch = str(secrets.get("github_branch", config.get("github_branch", "main"))).strip() or "main"
    return token, repo, branch


def normalize_stock(stock: dict[str, Any]) -> dict[str, Any]:
    """舊 buy_price 自動遷移成兩批 buy_prices，確保舊備份可讀。"""
    item = dict(stock)
    prices = item.get("buy_prices")
    if not isinstance(prices, list):
        prices = [item.get("buy_price"), None]
    prices = (list(prices) + [None, None])[:2]
    item["buy_prices"] = [float(x) if x not in (None, "", 0) else None for x in prices]
    item["buy_price"] = item["buy_prices"][0]  # 兼容舊版欄位
    item["sell_price"] = float(item["sell_price"]) if item.get("sell_price") not in (None, "", 0) else None
    item.setdefault("name", "")
    item.setdefault("note", "")
    item.setdefault("status", "active")
    item.setdefault("last_alerts", {})
    flags = item.get("buy_notified_flags")
    if not isinstance(flags, list):
        flags = [bool(item.get("buy_notified", False)), False]
    item["buy_notified_flags"] = [bool(x) for x in (list(flags) + [False, False])[:2]]
    item["buy_notified"] = item["buy_notified_flags"][0]
    item.setdefault("sell_notified", False)
    return item


def normalize_watchlist(stocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [normalize_stock(stock) for stock in stocks if isinstance(stock, dict)]


def clean_stocks(stocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """GitHub 只儲存設定與通知狀態，不提交每分鐘報價。"""
    fields = ("symbol", "name", "buy_prices", "buy_price", "sell_price", "note", "status", "buy_notified_flags", "buy_notified", "sell_notified", "last_alerts")
    return [{field: stock.get(field) for field in fields} for stock in normalize_watchlist(stocks)]


def get_remote_watchlist(config: dict[str, Any]) -> list[dict[str, Any]] | None:
    token, repo, branch = github_settings(config)
    if not token or not repo:
        return None
    response = requests.get(f"https://api.github.com/repos/{repo}/contents/{GITHUB_WATCHLIST_PATH}", headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}, params={"ref": branch}, timeout=15)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    payload = json.loads(base64.b64decode(response.json()["content"]).decode("utf-8"))
    return normalize_watchlist(payload["stocks"]) if isinstance(payload.get("stocks"), list) else None


def sync_watchlist_to_github(stocks: list[dict[str, Any]], config: dict[str, Any]) -> str | None:
    token, repo, branch = github_settings(config)
    if not token or not repo:
        return None
    try:
        url = f"https://api.github.com/repos/{repo}/contents/{GITHUB_WATCHLIST_PATH}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        current = requests.get(url, headers=headers, params={"ref": branch}, timeout=15)
        if not current.ok and current.status_code != 404:
            current.raise_for_status()
        content = json.dumps({"stocks": clean_stocks(stocks)}, ensure_ascii=False, indent=2).encode("utf-8")
        payload: dict[str, Any] = {"message": "chore: sync stock watchlist", "content": base64.b64encode(content).decode("ascii"), "branch": branch}
        if current.ok:
            payload["sha"] = current.json().get("sha")
        requests.put(url, headers=headers, json=payload, timeout=15).raise_for_status()
        return None
    except (requests.RequestException, ValueError, KeyError) as error:
        return f"GitHub 同步失敗：{error}"


def load_watchlist(config: dict[str, Any]) -> list[dict[str, Any]]:
    data = load_json(WATCHLIST_FILE, {"stocks": []})
    stocks = normalize_watchlist(data.get("stocks", [])) if isinstance(data.get("stocks"), list) else []
    if not st.session_state.get("remote_watchlist_loaded", False):
        try:
            remote = get_remote_watchlist(config)
            if remote is not None:
                stocks = remote
                save_json(WATCHLIST_FILE, {"stocks": stocks})
                clear_widget_state()
        except (requests.RequestException, ValueError, KeyError, json.JSONDecodeError):
            pass
        st.session_state.remote_watchlist_loaded = True
    return stocks


def save_watchlist(stocks: list[dict[str, Any]], config: dict[str, Any], sync: bool = True) -> str | None:
    stocks[:] = normalize_watchlist(stocks)
    save_json(WATCHLIST_FILE, {"stocks": stocks})
    return sync_watchlist_to_github(stocks, config) if sync else None


def taiwan_yfinance_symbol(code: str) -> str:
    info = twstock.codes.get(code)
    return f"{code}.TWO" if info and info.market == "上櫃" else f"{code}.TW"


def normalize_stock_symbol(raw: str) -> str:
    value = raw.strip().upper()
    if re.fullmatch(r"\d{4,6}[A-Z]?", value):
        return taiwan_yfinance_symbol(value)
    if re.search(r"[\u4e00-\u9fff]", raw):
        matches = [info for info in twstock.codes.values() if raw.strip() in info.name]
        if not matches:
            raise ValueError(f"找不到包含「{raw.strip()}」的台股名稱。")
        return taiwan_yfinance_symbol(matches[0].code)
    return value


@st.cache_data(ttl=86400, show_spinner=False)
def get_stock_name(symbol: str) -> str:
    match = re.fullmatch(r"(\d{4,6}[A-Z]?)\.(?:TW|TWO)", symbol.upper())
    if match and (info := twstock.codes.get(match.group(1))):
        return info.name
    try:
        info = yf.Ticker(symbol).get_info()
        return str(info.get("longName") or info.get("shortName") or symbol)
    except Exception:
        return symbol


def fetch_prices(symbols: list[str]) -> dict[str, float]:
    if not symbols:
        return {}
    result: dict[str, float] = {}
    try:
        data = yf.download(symbols, period="1d", interval="1m", progress=False, group_by="column")
        close = data["Close"] if not data.empty and "Close" in data else None
        for symbol in symbols:
            try:
                series = close[symbol] if len(symbols) > 1 else close
                if not series.dropna().empty:
                    result[symbol] = float(series.dropna().iloc[-1])
            except Exception:
                pass
    except Exception:
        pass
    for symbol in (symbol for symbol in symbols if symbol not in result):
        try:
            history = yf.Ticker(symbol).history(period="5d", interval="1d")
            if not history.empty:
                result[symbol] = float(history["Close"].dropna().iloc[-1])
        except Exception:
            pass
    return result


def send_line_message(token: str, user_id: str, message: str) -> None:
    response = requests.post("https://api.line.me/v2/bot/message/push", headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, json={"to": user_id, "messages": [{"type": "text", "text": message}], "notificationDisabled": False}, timeout=15)
    response.raise_for_status()


def reset_buy_notification(stock: dict[str, Any], index: int) -> None:
    flags = stock.setdefault("buy_notified_flags", [False, False])
    while len(flags) < 2:
        flags.append(False)
    flags[index] = False
    stock["buy_notified"] = flags[0]
    stock.setdefault("last_alerts", {}).pop(f"buy_{index}", None)


def reset_sell_notification(stock: dict[str, Any]) -> None:
    stock["sell_notified"] = False
    stock.setdefault("last_alerts", {}).pop("sell", None)


def can_notify(stock: dict[str, Any], alert_key: str, now: float) -> bool:
    return now - float(stock.get("last_alerts", {}).get(alert_key, 0)) >= COOLDOWN_SECONDS


def alert_text(stock: dict[str, Any], label: str, price: float, target: float) -> str:
    name, symbol = stock.get("name") or stock.get("symbol", ""), stock.get("symbol", "")
    text = f"📈 {name} ({symbol}) 已觸發{label}提醒\n目前價格：{price:,.2f}\n目標價格：{target:,.2f}"
    if note := str(stock.get("note", "")).strip():
        text += f"\n備註：{note}"
    return text


def run_monitor(stocks: list[dict[str, Any]], config: dict[str, Any]) -> list[str]:
    """支援兩批買點；每批有獨立通知旗標與冷卻時間。"""
    active = [stock for stock in stocks if stock.get("status", "active") != "paused"]
    prices = fetch_prices([str(stock.get("symbol", "")).upper() for stock in active])
    token, user_id = str(config.get("line_channel_access_token", "")).strip(), str(config.get("line_user_id", "")).strip()
    messages: list[str] = []
    now = time.time()
    notified = dynamic = False
    for stock in stocks:
        symbol = str(stock.get("symbol", "")).upper()
        if stock.get("status", "active") == "paused":
            messages.append(f"⚪ {symbol}：監控已暫停")
            continue
        price = prices.get(symbol)
        if price is None:
            messages.append(f"⚠️ {symbol}：暫時無法取得價格")
            continue
        stock["last_price"] = price
        stock["last_checked"] = datetime.now(TAIPEI_TZ).strftime("%m-%d %H:%M:%S")
        # 舊版清單可能尚未保存名稱；通知前補齊，確保 LINE 一律有中文股名。
        stock["name"] = stock.get("name") or get_stock_name(symbol)
        dynamic = True
        flags = stock.setdefault("buy_notified_flags", [False, False])
        for index, target in enumerate(stock.get("buy_prices", [None, None])):
            if target is None:
                continue
            key = f"buy_{index}"
            if price > target * 1.01:
                flags[index] = False
            if price <= target and not flags[index] and can_notify(stock, key, now):
                if token and user_id:
                    try:
                        send_line_message(token, user_id, alert_text(stock, f"第 {index + 1} 批買入價", price, target))
                        flags[index] = True
                        stock["buy_notified"] = flags[0]
                        stock.setdefault("last_alerts", {})[key] = now
                        notified = True
                        messages.append(f"✅ {symbol}：第 {index + 1} 批買入提醒已發送")
                    except requests.RequestException as error:
                        messages.append(f"❌ {symbol}：LINE 發送失敗（{error}）")
        target = stock.get("sell_price")
        if target is not None:
            if price < target * 0.99:
                stock["sell_notified"] = False
            if price >= target and not stock.get("sell_notified", False) and can_notify(stock, "sell", now):
                if token and user_id:
                    try:
                        send_line_message(token, user_id, alert_text(stock, "賣出價", price, target))
                        stock["sell_notified"] = True
                        stock.setdefault("last_alerts", {})["sell"] = now
                        notified = True
                        messages.append(f"✅ {symbol}：賣出提醒已發送")
                    except requests.RequestException as error:
                        messages.append(f"❌ {symbol}：LINE 發送失敗（{error}）")
        messages.append(f"🔹 {symbol}：{price:,.2f}")
    if dynamic:
        save_json(WATCHLIST_FILE, {"stocks": stocks})
    if notified:
        error = sync_watchlist_to_github(stocks, config)
        if error:
            messages.append(error)
    return messages


def taiwan_market_open() -> bool:
    now = datetime.now(TAIPEI_TZ)
    return now.weekday() < 5 and clock_time(8, 45) <= now.time() <= clock_time(13, 45)


def refresh_label(seconds: int) -> str:
    return next((label for label, value in REFRESH_OPTIONS.items() if value == seconds), "60 秒")


st.set_page_config(page_title="股票到價提醒", page_icon="📈", layout="wide")
st.title("📈 股票關鍵價位監控")
st.caption("使用 yfinance 取得報價，觸及目標價格時發送 LINE 通知。")
config = init_config()
stocks = load_watchlist(config)

with st.sidebar:
    st.header("新增股票")
    st.caption("支援 4~6 位代碼、中文名稱或美股代碼，例如 2330、台積電、NVDA。")
    with st.form("add_stock_form", clear_on_submit=True):
        stock_input = st.text_input("股票代碼／名稱")
        buy_1 = st.number_input("第 1 批買價（0 代表不設定）", min_value=0.0, step=0.01)
        buy_2 = st.number_input("第 2 批買價（0 代表不設定）", min_value=0.0, step=0.01)
        sell_price = st.number_input("目標賣出價（0 代表不設定）", min_value=0.0, step=0.01)
        add_clicked = st.form_submit_button("新增股票", use_container_width=True)
    if add_clicked:
        try:
            if not stock_input.strip() or (buy_1 <= 0 and buy_2 <= 0 and sell_price <= 0):
                raise ValueError("請輸入股票，並至少設定一個買入價或賣出價。")
            symbol = normalize_stock_symbol(stock_input)
            if any(str(stock.get("symbol", "")).upper() == symbol for stock in stocks):
                raise ValueError("此股票已在監控清單中。")
            stocks.append(normalize_stock({"symbol": symbol, "name": get_stock_name(symbol), "buy_prices": [buy_1 or None, buy_2 or None], "sell_price": sell_price or None}))
            error = save_watchlist(stocks, config)
            if error:
                st.error(error)
            else:
                clear_widget_state(); st.rerun()
        except ValueError as error:
            st.error(str(error))

    st.divider()
    configured = bool(github_settings(config)[0] and github_settings(config)[1])
    title = "💾 備份與 GitHub 同步 [🟢 已設定]" if configured else "💾 備份與 GitHub 同步 [⚪ 未設定]"
    with st.expander(title, expanded=False):
        st.download_button("匯出監控清單 JSON", json.dumps({"stocks": clean_stocks(stocks)}, ensure_ascii=False, indent=2), "watchlist-backup.json", "application/json", use_container_width=True)
        backup = st.file_uploader("匯入監控清單 JSON", type=["json"], key="watchlist_import")
        if backup and st.button("匯入並覆蓋目前清單", use_container_width=True):
            try:
                imported = json.loads(backup.getvalue().decode("utf-8")).get("stocks")
                if not isinstance(imported, list): raise ValueError("備份檔案缺少 stocks 清單。")
                stocks[:] = normalize_watchlist(imported)
                error = save_watchlist(stocks, config)
                if error: st.error(error)
                else: clear_widget_state(); st.rerun()
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                st.error(f"無法匯入備份：{error}")
        # 不使用 form：自動刷新時，獨立 key 會保留尚未提交的輸入值。
        github_token = st.text_input("GitHub Token", value=config.get("github_token", ""), type="password", key="cfg_github_token_input")
        github_repo = st.text_input("GitHub Repo", value=config.get("github_repo", ""), placeholder="owner/repository", key="cfg_github_repo_input")
        github_branch = st.text_input("GitHub Branch", value=config.get("github_branch", "main"), key="cfg_github_branch_input")
        if st.button("儲存 GitHub 設定", use_container_width=True, key="save_github_config"):
            config = update_config({"github_token": github_token.strip(), "github_repo": github_repo.strip(), "github_branch": github_branch.strip() or "main"})
            error = sync_watchlist_to_github(stocks, config)
            if error: st.error(error)
            else: st.success("GitHub 設定已儲存並同步目前清單。")

    with st.expander("⚙️ 系統設定", expanded=False):
        label = st.selectbox("自動重新整理頻率", list(REFRESH_OPTIONS), index=list(REFRESH_OPTIONS).index(refresh_label(int(config.get("refresh_seconds", 60)))), key="cfg_refresh_seconds_input")
        if st.button("儲存系統設定", use_container_width=True, key="save_system_config"):
            update_config({"refresh_seconds": REFRESH_OPTIONS[label]})
            st.rerun()

    # Popover 將密碼欄位隔離於主表單外，降低 Chrome 誤提示儲存密碼的機率。
    with st.popover("⚙️ LINE 設定", use_container_width=True):
        line_token = st.text_input("LINE Channel Access Token", value=config.get("line_channel_access_token", ""), type="password", key="cfg_line_token_input")
        line_user = st.text_input("LINE User ID", value=config.get("line_user_id", ""), type="password", key="cfg_line_user_input")
        if st.button("儲存 LINE 設定", use_container_width=True, key="save_line_config"):
            update_config({"line_channel_access_token": line_token.strip(), "line_user_id": line_user.strip()})
            st.success("LINE 設定已儲存。")
        if st.button("🧪 發送測試通知", use_container_width=True, key="test_line_notification"):
            try:
                if not line_token.strip() or not line_user.strip(): raise ValueError("請先輸入 LINE Token 與 User ID。")
                send_line_message(line_token.strip(), line_user.strip(), "LINE 機器人連線成功測試！")
                st.success("測試通知已發送。")
            except (requests.RequestException, ValueError) as error:
                st.error(f"測試通知發送失敗：{error}")

market_open = taiwan_market_open()
base_seconds = int(config.get("refresh_seconds", 60))
effective_seconds = base_seconds if market_open else max(base_seconds, 300)
st.caption(("🟢 台股盤中" if market_open else "🌙 休市中") + f"　自動更新：每 {effective_seconds} 秒" + ("（休市節流）" if not market_open else ""))
monitoring = st.toggle("啟動監控", value=True, key="monitoring")
st.subheader("監控清單")

if not stocks:
    st.info("尚未加入任何股票。")
else:
    manual_refresh = st.button("💾 儲存/重新整理", type="primary")
    messages: list[str] = []
    if monitoring or manual_refresh:
        if monitoring: st_autorefresh(interval=effective_seconds * 1000, key="stock_monitor_refresh")
        with st.spinner("正在更新價格與檢查提醒…"):
            messages = run_monitor(stocks, config)
        with st.expander("🔍 即時檢查日誌", expanded=False):
            for message in messages: st.write(message)
    else:
        st.caption("監控目前已停止；可按「💾 儲存/重新整理」手動檢查。")

    changed = action_changed = False
    delete_indices: list[int] = []
    widths = [1.15, 1.3, 1.15, 1.15, 1.15, 1.05, 1.45, 2.0, 2.1]
    header = st.columns(widths)
    for column, label in zip(header, ["**股票代碼**", "**股票名稱**", "<span style='color:#00c853; font-weight:bold;'>第 1 批買價</span>", "<span style='color:#00c853; font-weight:bold;'>第 2 批買價</span>", "<span style='color:#ff5252; font-weight:bold;'>目標賣出價</span>", "**最新價格**", "**最後檢查時間**", "**決策思維／進出場備註**", "**操作**"]):
        column.markdown(label, unsafe_allow_html=True)
    for index, stock in enumerate(stocks):
        row = st.columns(widths)
        symbol = str(stock.get("symbol", "-")); stock["name"] = stock.get("name") or get_stock_name(symbol)
        row[0].write(symbol); row[1].write(stock["name"])
        prices = stock["buy_prices"]
        values = [float(prices[0] or 0), float(prices[1] or 0)]
        buy_values = [row[2].number_input("第 1 批買價", min_value=0.0, value=values[0], step=0.01, label_visibility="collapsed", key=f"buy1_{symbol}"), row[3].number_input("第 2 批買價", min_value=0.0, value=values[1], step=0.01, label_visibility="collapsed", key=f"buy2_{symbol}")]
        sell = row[4].number_input("目標賣出價", min_value=0.0, value=float(stock.get("sell_price") or 0), step=0.01, label_visibility="collapsed", key=f"sell_{symbol}")
        row[5].write("-" if stock.get("last_price") is None else f"{float(stock['last_price']):,.2f}")
        row[6].write(stock.get("last_checked", "尚未檢查"))
        note = row[7].text_input("備註", value=str(stock.get("note", "")), label_visibility="collapsed", key=f"note_{symbol}")
        for batch, value in enumerate(buy_values):
            value = round(float(value), 4) if value > 0 else None
            if stock["buy_prices"][batch] != value:
                stock["buy_prices"][batch] = value; stock["buy_price"] = stock["buy_prices"][0]
                reset_buy_notification(stock, batch); changed = True
        sell = round(float(sell), 4) if sell > 0 else None
        if stock.get("sell_price") != sell:
            stock["sell_price"] = sell; reset_sell_notification(stock); changed = True
        if stock.get("note", "") != note:
            stock["note"] = note; changed = True
        actions = row[8].columns(2); paused = stock.get("status", "active") == "paused"
        if actions[0].button("🟢 啟用" if paused else "🟡 暫停", key=f"status_{symbol}", use_container_width=True):
            stock["status"] = "active" if paused else "paused"; changed = action_changed = True
        if actions[1].button("🔴 刪除", key=f"delete_{symbol}", use_container_width=True):
            delete_indices.append(index); changed = action_changed = True
    for index in reversed(delete_indices): stocks.pop(index)
    if changed:
        error = save_watchlist(stocks, config)
        if error: st.error(error)
        else:
            if stocks:
                with st.spinner("正在以最新設定立即比對…"): run_monitor(stocks, config)
            if action_changed: clear_widget_state()
            st.rerun()
