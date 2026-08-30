"""Streamlit 股票關鍵價位監控工具。"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
import streamlit as st
import twstock
import yfinance as yf
from streamlit_autorefresh import st_autorefresh


BASE_DIR = Path(__file__).resolve().parent
WATCHLIST_FILE = BASE_DIR / "watchlist.json"
CONFIG_FILE = BASE_DIR / "config.json"
COOLDOWN_SECONDS = 30 * 60


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
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def load_watchlist() -> list[dict[str, Any]]:
    data = load_json(WATCHLIST_FILE, {"stocks": []})
    return data.get("stocks", []) if isinstance(data.get("stocks"), list) else []


def save_watchlist(stocks: list[dict[str, Any]]) -> None:
    save_json(WATCHLIST_FILE, {"stocks": stocks})


def normalize_stock_symbol(raw_symbol: str) -> str:
    """將台股四位代碼或中文名稱轉為 yfinance 可用的代碼。"""
    symbol = raw_symbol.strip()
    if re.fullmatch(r"\d{4}", symbol):
        return f"{symbol}.TW"

    # 中文名稱以 twstock 的官方台股代碼資料為準，例如「台積電」→ 2330.TW。
    if re.search(r"[\u4e00-\u9fff]", symbol):
        matched_codes = [
            info.code
            for info in twstock.codes.values()
            if info.name == symbol
        ]
        if not matched_codes:
            raise ValueError(f"找不到台股名稱「{symbol}」，請改輸入代碼。")
        return f"{matched_codes[0]}.TW"

    return symbol.upper()


@st.cache_data(ttl=3600, show_spinner=False)
def get_stock_name(symbol: str) -> str:
    """取得股票名稱：台股優先使用 twstock，其餘代碼使用 yfinance。"""
    taiwan_match = re.fullmatch(r"(\d{4,6})\.TW", symbol.upper())
    if taiwan_match:
        stock_info = twstock.codes.get(taiwan_match.group(1))
        if stock_info:
            return stock_info.name

    try:
        info = yf.Ticker(symbol).get_info()
        return str(info.get("longName") or info.get("shortName") or "-")
    except Exception:
        return "-"


def get_current_price(symbol: str) -> float:
    """從 yfinance 取得最近一筆可用的收盤價。"""
    history = yf.Ticker(symbol).history(period="1d", interval="1m")
    if history.empty:
        # 非交易時段或分鐘資料不可用時，退回最近 5 天日線。
        history = yf.Ticker(symbol).history(period="5d", interval="1d")
    if history.empty:
        raise ValueError("查無價格資料，請確認股票代碼。")
    return float(history["Close"].dropna().iloc[-1])


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
        },
        timeout=15,
    )
    response.raise_for_status()


def can_notify(stock: dict[str, Any], alert_type: str, now: float) -> bool:
    last_alerts = stock.get("last_alerts", {})
    last_sent = float(last_alerts.get(alert_type, 0))
    return now - last_sent >= COOLDOWN_SECONDS


def run_monitor(stocks: list[dict[str, Any]], config: dict[str, Any]) -> list[str]:
    """檢查一次所有股票；回傳畫面可顯示的結果訊息。"""
    messages: list[str] = []
    channel_access_token = str(config.get("line_channel_access_token", "")).strip()
    user_id = str(config.get("line_user_id", "")).strip()
    now = time.time()
    changed = False

    for stock in stocks:
        symbol = str(stock.get("symbol", "")).upper()
        if stock.get("status", "active") == "paused":
            messages.append(f"{symbol}: 監控已暫停")
            continue
        try:
            price = get_current_price(symbol)
            stock["last_price"] = price
            stock["last_checked"] = datetime.now().strftime("%m-%d %H:%M:%S")
            changed = True
            messages.append(f"{symbol}: {price:,.2f}")

            alerts: list[tuple[str, float]] = []
            buy_price = stock.get("buy_price")
            sell_price = stock.get("sell_price")
            if buy_price is not None and price <= float(buy_price):
                alerts.append(("buy", float(buy_price)))
            if sell_price is not None and price >= float(sell_price):
                alerts.append(("sell", float(sell_price)))

            for alert_type, target in alerts:
                if not can_notify(stock, alert_type, now):
                    continue
                verb = "買入" if alert_type == "buy" else "賣出"
                text = f"📈 {symbol} 已觸發{verb}價提醒\n目前價格：{price:,.2f}\n目標價格：{target:,.2f}"
                if channel_access_token and user_id:
                    send_line_message(channel_access_token, user_id, text)
                    messages.append(f"已發送 LINE：{symbol} {verb}價提醒")
                else:
                    messages.append(f"{symbol} 已觸發{verb}價；尚未設定 LINE，未發送通知。")
                stock.setdefault("last_alerts", {})[alert_type] = now
                changed = True
        except Exception as error:  # 讓單一股票失敗時不影響其他監控項目。
            messages.append(f"{symbol}: 取得價格或發送通知失敗（{error}）")

    if changed:
        save_watchlist(stocks)
    return messages


st.set_page_config(page_title="股票到價提醒", page_icon="📈", layout="wide")
st.title("📈 股票關鍵價位監控")
st.caption("使用 yfinance 取得報價，觸及買入／賣出目標價時通知 LINE。")

if "monitoring" not in st.session_state:
    st.session_state.monitoring = False

config = load_json(CONFIG_FILE, {"line_channel_access_token": "", "line_user_id": ""})

with st.sidebar:
    st.header("新增股票")
    st.caption("支援輸入 4 位代碼（如 2330）、中文名稱（如 台積電）或美股代碼（如 NVDA）")
    with st.form("add_stock_form", clear_on_submit=True):
        stock_input = st.text_input("股票代碼／名稱", placeholder="例如：2330、台積電、NVDA")
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
                stocks = load_watchlist()
                if any(item.get("symbol", "").upper() == symbol for item in stocks):
                    st.error("此股票代碼已在監控清單中。")
                else:
                    stocks.append({
                        "symbol": symbol,
                        "name": get_stock_name(symbol),
                        "buy_price": buy_price if buy_price > 0 else None,
                        "sell_price": sell_price if sell_price > 0 else None,
                        "status": "active",
                        "last_alerts": {},
                    })
                    save_watchlist(stocks)
                    st.success(f"已加入 {symbol}")
            except ValueError as error:
                st.error(str(error))

    st.divider()
    with st.expander("⚙️ LINE 設定", expanded=False):
        with st.form("line_form"):
            line_channel_access_token = st.text_input(
                "LINE Channel Access Token",
                value=config.get("line_channel_access_token", ""),
                type="password",
            )
            line_user_id = st.text_input("LINE User ID", value=config.get("line_user_id", ""), type="password")
            save_config = st.form_submit_button("儲存 LINE 設定", use_container_width=True)
        if save_config:
            config = {
                "line_channel_access_token": line_channel_access_token.strip(),
                "line_user_id": line_user_id.strip(),
            }
            save_json(CONFIG_FILE, config)
            st.success("LINE 設定已儲存。")

monitoring = st.toggle("啟動監控（每 60 秒檢查一次）", key="monitoring")
stocks = load_watchlist()

if monitoring:
    st_autorefresh(interval=60_000, key="stock_monitor_refresh")
    if stocks:
        with st.spinner("正在更新價格與檢查提醒…"):
            monitor_messages = run_monitor(stocks, config)
        st.info("｜".join(monitor_messages))
    else:
        st.warning("目前沒有監控股票，請先從左側新增。")
else:
    st.caption("監控目前已停止。開啟上方開關後會立即檢查，並每 60 秒自動更新。")

st.subheader("監控清單")
if not stocks:
    st.info("尚未加入任何股票。")
else:
    header = st.columns([1.5, 1.5, 1.5, 1.5, 1.5, 1.8, 1.8])
    header[0].markdown("**股票代碼**")
    header[1].markdown("**股票名稱**")
    header[2].markdown("<span style='color:#00c853; font-weight:bold;'>目標買入價</span>", unsafe_allow_html=True)
    header[3].markdown("<span style='color:#ff5252; font-weight:bold;'>目標賣出價</span>", unsafe_allow_html=True)
    header[4].markdown("**最新價格**")
    header[5].markdown("**最後檢查**")
    header[6].markdown("**操作**")

    for index, stock in enumerate(stocks):
        columns = st.columns([1.5, 1.5, 1.5, 1.5, 1.5, 1.8, 1.8])
        columns[0].write(stock.get("symbol", "-"))
        name = stock.get("name") or get_stock_name(str(stock.get("symbol", "")))
        columns[1].write(name)
        columns[2].write("-" if stock.get("buy_price") is None else f"{float(stock['buy_price']):,.2f}")
        columns[3].write("-" if stock.get("sell_price") is None else f"{float(stock['sell_price']):,.2f}")
        columns[4].write("-" if stock.get("last_price") is None else f"{float(stock['last_price']):,.2f}")
        columns[5].write(stock.get("last_checked", "尚未檢查"))
        action_columns = columns[6].columns(2)
        is_paused = stock.get("status", "active") == "paused"
        toggle_label = "恢復" if is_paused else "暫停"
        if action_columns[0].button(toggle_label, key=f"status_{index}_{stock.get('symbol')}"):
            stock["status"] = "active" if is_paused else "paused"
            save_watchlist(stocks)
            st.rerun()
        if action_columns[1].button("刪除", key=f"delete_{index}_{stock.get('symbol')}"):
            stocks.pop(index)
            save_watchlist(stocks)
            st.rerun()
