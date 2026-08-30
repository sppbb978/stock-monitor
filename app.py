"""Streamlit 股票關鍵價位監控工具。"""

from __future__ import annotations

import base64
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
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def github_settings(config: dict[str, Any]) -> tuple[str, str, str]:
    """優先讀取 Streamlit secrets，其次讀取介面儲存的 GitHub 設定。"""
    try:
        secrets: dict[str, Any] = st.secrets
    except FileNotFoundError:
        secrets = {}
    token = str(secrets.get("github_token", config.get("github_token", ""))).strip()
    repo = str(secrets.get("github_repo", config.get("github_repo", ""))).strip()
    branch = str(secrets.get("github_branch", config.get("github_branch", "main"))).strip() or "main"
    return token, repo, branch


def get_remote_watchlist(config: dict[str, Any]) -> list[dict[str, Any]] | None:
    """從 GitHub 下載已同步的監控清單；未設定時不執行。"""
    token, repo, branch = github_settings(config)
    if not token or not repo:
        return None
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


def sync_watchlist_to_github(stocks: list[dict[str, Any]], config: dict[str, Any]) -> str | None:
    """將清單上傳至 GitHub；失敗時回傳錯誤訊息但不影響本機資料。"""
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

        content = json.dumps({"stocks": stocks}, ensure_ascii=False, indent=2).encode("utf-8")
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
    data = load_json(WATCHLIST_FILE, {"stocks": []})
    stocks = data.get("stocks", []) if isinstance(data.get("stocks"), list) else []
    # 每個瀏覽器工作階段只在首次載入時從遠端還原，避免每次互動都呼叫 GitHub。
    if config and not st.session_state.get("remote_watchlist_loaded", False):
        try:
            remote_stocks = get_remote_watchlist(config)
            if remote_stocks is not None:
                stocks = remote_stocks
                save_json(WATCHLIST_FILE, {"stocks": stocks})
        except (requests.RequestException, ValueError, json.JSONDecodeError):
            pass
        st.session_state.remote_watchlist_loaded = True
    return stocks


def save_watchlist(stocks: list[dict[str, Any]], config: dict[str, Any] | None = None) -> str | None:
    save_json(WATCHLIST_FILE, {"stocks": stocks})
    return sync_watchlist_to_github(stocks, config) if config else None


def taiwan_yfinance_symbol(stock_code: str) -> str:
    """依 twstock 的市場資料轉換為 yfinance 使用的 TW／TWO 後綴。"""
    stock_info = twstock.codes.get(stock_code)
    if stock_info and stock_info.market == "上櫃":
        return f"{stock_code}.TWO"
    return f"{stock_code}.TW"


def normalize_stock_symbol(raw_symbol: str) -> str:
    """將台股四位代碼或中文名稱轉為 yfinance 可用的代碼。"""
    symbol = raw_symbol.strip()
    if re.fullmatch(r"\d{4}", symbol):
        return taiwan_yfinance_symbol(symbol)

    # 中文名稱以 twstock 的台股代碼資料與市場別為準。
    if re.search(r"[\u4e00-\u9fff]", symbol):
        matched_stocks = [
            info
            for info in twstock.codes.values()
            if info.name == symbol
        ]
        if not matched_stocks:
            raise ValueError(f"找不到台股名稱「{symbol}」，請改輸入代碼。")
        return taiwan_yfinance_symbol(matched_stocks[0].code)

    return symbol.upper()


@st.cache_data(ttl=3600, show_spinner=False)
def get_stock_name(symbol: str) -> str:
    """取得股票名稱：台股優先使用 twstock，其餘代碼使用 yfinance。"""
    taiwan_match = re.fullmatch(r"(\d{4,6})\.(?:TW|TWO)", symbol.upper())
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
            "notificationDisabled": False,
        },
        timeout=15,
    )
    response.raise_for_status()


def can_notify(stock: dict[str, Any], alert_type: str, now: float) -> bool:
    """保留 30 分鐘保護；改價時會清除對應時間戳，允許立刻重新通知。"""
    last_alerts = stock.get("last_alerts", {})
    last_sent = float(last_alerts.get(alert_type, 0))
    return now - last_sent >= COOLDOWN_SECONDS


def reset_notification_state(stock: dict[str, Any], alert_type: str) -> None:
    """目標價變更後，清除該方向的已通知旗標與冷卻時間。"""
    stock[f"{alert_type}_notified"] = False
    stock.setdefault("last_alerts", {}).pop(alert_type, None)


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
            if (
                buy_price is not None
                and float(buy_price) > 0
                and price <= float(buy_price)
                and not stock.get("buy_notified", False)
            ):
                alerts.append(("buy", float(buy_price)))
            if (
                sell_price is not None
                and float(sell_price) > 0
                and price >= float(sell_price)
                and not stock.get("sell_notified", False)
            ):
                alerts.append(("sell", float(sell_price)))

            for alert_type, target in alerts:
                if not can_notify(stock, alert_type, now):
                    continue
                verb = "買入" if alert_type == "buy" else "賣出"
                text = f"📈 {symbol} 已觸發{verb}價提醒\n目前價格：{price:,.2f}\n目標價格：{target:,.2f}"
                if channel_access_token and user_id:
                    send_line_message(channel_access_token, user_id, text)
                    stock[f"{alert_type}_notified"] = True
                    stock.setdefault("last_alerts", {})[alert_type] = now
                    changed = True
                    messages.append(f"已發送 LINE：{symbol} {verb}價提醒")
                else:
                    messages.append(f"{symbol} 已觸發{verb}價；尚未設定 LINE，未發送通知。")
        except Exception as error:  # 讓單一股票失敗時不影響其他監控項目。
            messages.append(f"{symbol}: 取得價格或發送通知失敗（{error}）")

    if changed:
        sync_error = save_watchlist(stocks, config)
        if sync_error:
            messages.append(sync_error)
    return messages


st.set_page_config(page_title="股票到價提醒", page_icon="📈", layout="wide")
st.title("📈 股票關鍵價位監控")
st.caption("使用 yfinance 取得報價，觸及買入／賣出目標價時通知 LINE。")

config = load_json(CONFIG_FILE, {"line_channel_access_token": "", "line_user_id": ""})
stocks = load_watchlist(config)

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
                if any(item.get("symbol", "").upper() == symbol for item in stocks):
                    st.error("此股票代碼已在監控清單中。")
                else:
                    stocks.append({
                        "symbol": symbol,
                        "name": get_stock_name(symbol),
                        "buy_price": buy_price if buy_price > 0 else None,
                        "sell_price": sell_price if sell_price > 0 else None,
                        "status": "active",
                        "buy_notified": False,
                        "sell_notified": False,
                        "last_alerts": {},
                    })
                    sync_error = save_watchlist(stocks, config)
                    st.success(f"已加入 {symbol}")
                    if sync_error:
                        st.warning(sync_error)
            except ValueError as error:
                st.error(str(error))

    st.divider()
    with st.expander("💾 備份與 GitHub 同步", expanded=False):
        st.download_button(
            "匯出監控清單 JSON",
            data=json.dumps({"stocks": stocks}, ensure_ascii=False, indent=2),
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
                if sync_error:
                    st.warning(sync_error)
                else:
                    st.success("監控清單已匯入。")
                st.rerun()
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                st.error(f"無法匯入備份：{error}")

        st.caption("可將 Token／Repo 放在 Streamlit secrets；若填在此處，清單每次變動會自動同步到 GitHub。")
        with st.form("github_form"):
            github_token = st.text_input("GitHub Token", value=config.get("github_token", ""), type="password")
            github_repo = st.text_input("GitHub Repo", value=config.get("github_repo", ""), placeholder="owner/repository")
            github_branch = st.text_input("GitHub Branch", value=config.get("github_branch", "main"))
            save_github_config = st.form_submit_button("儲存 GitHub 同步設定", use_container_width=True)
        if save_github_config:
            config.update({
                "github_token": github_token.strip(),
                "github_repo": github_repo.strip(),
                "github_branch": github_branch.strip() or "main",
            })
            save_json(CONFIG_FILE, config)
            st.session_state.remote_watchlist_loaded = False
            sync_error = save_watchlist(stocks, config)
            if sync_error:
                st.warning(sync_error)
            else:
                st.success("GitHub 同步設定已儲存，現有清單已同步。")

    with st.popover("⚙️ LINE 設定", use_container_width=True):
        st.caption("儲存憑證或先發送測試訊息確認 LINE 連線。")
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
            config.update({
                "line_channel_access_token": line_channel_access_token.strip(),
                "line_user_id": line_user_id.strip(),
            })
            save_json(CONFIG_FILE, config)
            st.success("LINE 設定已儲存。")
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
        st.info("｜".join(monitor_messages))
    else:
        st.caption("監控目前已停止；可按「💾 儲存/重新整理」手動刷新報價。")

    price_changed = False
    watchlist_changed = False
    action_changed = False
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
        columns[1].write(stock.get("name") or get_stock_name(symbol))
        current_buy = float(stock.get("buy_price") or 0.0)
        current_sell = float(stock.get("sell_price") or 0.0)
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

        for field, alert_type, new_price in (("buy_price", "buy", new_buy), ("sell_price", "sell", new_sell)):
            normalized_price = None if new_price <= 0 else float(new_price)
            if stock.get(field) != normalized_price:
                stock[field] = normalized_price
                reset_notification_state(stock, alert_type)
                price_changed = True
                watchlist_changed = True

        action_columns = columns[6].columns(2)
        is_paused = stock.get("status", "active") == "paused"
        status_label = "🟢 啟用" if is_paused else "🟡 暫停"
        if action_columns[0].button(status_label, key=f"status_{index}_{symbol}", use_container_width=True):
            stock["status"] = "active" if is_paused else "paused"
            watchlist_changed = True
            action_changed = True
        if action_columns[1].button("🔴 刪除", key=f"delete_{index}_{symbol}", use_container_width=True):
            delete_indices.append(index)
            watchlist_changed = True
            action_changed = True

    for index in reversed(delete_indices):
        stocks.pop(index)

    if watchlist_changed:
        sync_error = save_watchlist(stocks, config)
        if sync_error:
            st.warning(sync_error)
        else:
            st.success("監控清單已儲存。")

        if stocks:
            with st.spinner("正在以目前報價立即比對…"):
                immediate_messages = run_monitor(stocks, config)
            st.info("｜".join(immediate_messages))
        if action_changed or price_changed:
            st.rerun()
