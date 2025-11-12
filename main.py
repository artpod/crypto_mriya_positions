#!/usr/bin/env python3
# simple_mexc_futures_bot_v11_orders.py
# v11.10:
# - Fixed KeyError: 'limit_orders' by correcting the .pop() logic
#   (Was popping the whole key, not just the oid)
# - v11.9: MAJOR FIX: Stop orders (SL/TP) are a single API object.
# - Bot now detects CHANGES (e.g., adding an SL to a TP) and re-sends
#   the notification instead of just looking for "new" orders.
# - format_new_stop_order() now supports showing BOTH SL and TP
#   in the same message (e.g., "Stop Loss / Take Profit").
# - Kept v11.8 logic for finding volume from the parent position.

import os, time, json, hmac, hashlib, requests, pathlib
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

# --- RENDER.COM PERSISTENT DISK CONFIG ---
# Шлях до диска беремо зі змінної оточення, яку створимо на Render
# Якщо змінної немає (локальний запуск), використовуємо поточну папку "."
DATA_DIR = os.getenv("RENDER_DISK_PATH", ".")
print(f"Using DATA_DIR: {DATA_DIR}") # Це буде видно в логах

# CONFIG
CONFIG_PATH = "config.json" # config.json НЕ буде на диску
STATE_PATH = os.path.join(DATA_DIR, "state.json")
STATE_ORDERS_PATH = os.path.join(DATA_DIR, "state_orders.json")
CONTRACT_CACHE_PATH = os.path.join(DATA_DIR, "contract_cache.json")
# --- END RENDER.COM CONFIG ---

PRICE_CACHE_TTL = 6            # seconds for market price cache
CONTRACT_CACHE_TTL = 24 * 3600  # seconds for contract detail cache
DEFAULT_POLL_INTERVAL = 6
DEFAULT_TZ = "Europe/Warsaw"

# load config
if os.path.exists(CONFIG_PATH):
    cfg = json.load(open(CONFIG_PATH, "r", encoding="utf-8"))
else:
    cfg = {
        "MEXC_API_KEY": os.getenv("MEXC_API_KEY"),
        "MEXC_API_SECRET": os.getenv("MEXC_API_SECRET"),
        "TG_BOT_TOKEN": os.getenv("TG_BOT_TOKEN"),
        "CHANNEL": int(os.getenv("CHANNEL")) if os.getenv("CHANNEL") and os.getenv("CHANNEL").lstrip("-").isdigit() else os.getenv("CHANNEL"),
        "POLL_INTERVAL": int(os.getenv("POLL_INTERVAL") or DEFAULT_POLL_INTERVAL),
        "TIMEZONE": os.getenv("TIMEZONE") or DEFAULT_TZ,
        "MEXC_BASE": os.getenv("MEXC_BASE") or "https://contract.mexc.com"
    }

for k in ("MEXC_API_KEY","MEXC_API_SECRET","TG_BOT_TOKEN","CHANNEL"):
    if not cfg.get(k):
        raise SystemExit(f"Missing config {k} in {CONFIG_PATH} or env")

POLL_INTERVAL = cfg.get("POLL_INTERVAL", DEFAULT_POLL_INTERVAL)
TZ = ZoneInfo(cfg.get("TIMEZONE", DEFAULT_TZ))
TG_API = f"https://api.telegram.org/bot{cfg['TG_BOT_TOKEN']}"
MEXC_BASE = cfg.get("MEXC_BASE").rstrip("/")

# state helpers
def load_state():
    default = {"positions": {}, "pinned_message_id": None, "last_pinned_text": None, "last_daily_pnl_date": None}
    if not os.path.exists(STATE_PATH):
        return default
    try:
        return json.load(open(STATE_PATH, "r", encoding="utf-8"))
    except:
        try:
            os.rename(STATE_PATH, STATE_PATH + ".corrupt")
        except:
            pass
        return default

def save_state(s):
    tmp = STATE_PATH + ".tmp"
    with open(tmp,"w",encoding="utf-8") as f:
        json.dump(s,f,ensure_ascii=False,indent=2)
    os.replace(tmp, STATE_PATH)

state = load_state()

# state helpers for orders
def load_state_orders():
    # 'stop_orders' (from stoporder/open_orders)
    default = {"limit_orders": {}, "stop_orders": {}}
    if not os.path.exists(STATE_ORDERS_PATH):
        return default
    try:
        return json.load(open(STATE_ORDERS_PATH, "r", encoding="utf-8"))
    except:
        try:
            os.rename(STATE_ORDERS_PATH, STATE_ORDERS_PATH + ".corrupt")
        except:
            pass
        return default

def save_state_orders(s):
    tmp = STATE_ORDERS_PATH + ".tmp"
    with open(tmp,"w",encoding="utf-8") as f:
        json.dump(s,f,ensure_ascii=False,indent=2)
    os.replace(tmp, STATE_ORDERS_PATH)

state_orders = load_state_orders()


# contract cache (file-backed)
def load_contract_cache():
    p = pathlib.Path(CONTRACT_CACHE_PATH)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except:
            return {}
    return {}

def save_contract_cache(c):
    pathlib.Path(CONTRACT_CACHE_PATH).write_text(json.dumps(c), encoding="utf-8")

_contract_cache = load_contract_cache()

def get_contract_detail_public(symbol):
    """
    Return dict with contract detail data, uses file cache with TTL.
    """
    now = int(time.time())
    rec = _contract_cache.get(symbol)
    if rec and (now - rec.get("fetched_at", 0) < CONTRACT_CACHE_TTL):
        return rec.get("data")
    # call public endpoint
    try:
        r = requests.get(MEXC_BASE + "/api/v1/contract/detail", params={"symbol": symbol}, timeout=8)
        j = r.json()
    except Exception as e:
        if rec:
            return rec.get("data")
        raise RuntimeError(f"Failed to fetch contract detail: {e}")
    if not j.get("success"):
        if rec:
            return rec.get("data")
        raise RuntimeError(f"contract/detail API error: {j}")
    data = j.get("data") or {}
    _contract_cache[symbol] = {"data": data, "fetched_at": now}
    save_contract_cache(_contract_cache)
    return data

# market price cache (in-memory)
_price_cache = {}  # symbol -> (price, fetched_at)

def get_market_price(symbol):
    """
    Prefer index price (stable reference). Fallback to other tickers.
    Cached for PRICE_CACHE_TTL seconds.
    """
    now = int(time.time())
    rec = _price_cache.get(symbol)
    if rec and now - rec[1] < PRICE_CACHE_TTL:
        return rec[0]

    # 1) Try index price endpoint (preferred)
    try:
        r = requests.get(f"{MEXC_BASE}/api/v1/contract/index_price/{symbol}", timeout=6)
        j = r.json()
        if isinstance(j, dict) and j.get("success") and j.get("data") and j["data"].get("indexPrice") is not None:
            price = float(j["data"]["indexPrice"])
            _price_cache[symbol] = (price, now)
            return price
    except Exception:
        pass

    # 2) Fallbacks (existing logic)
    candidates = [
        (MEXC_BASE + "/api/v1/market/ticker", "data"),
        (MEXC_BASE + "/api/v1/contract/ticker", "data"),
        (MEXC_BASE + "/api/v3/market/ticker/price", None),
    ]
    price = None
    for url, data_key in candidates:
        try:
            r = requests.get(url, params={"symbol": symbol}, timeout=6)
            j = r.json()
        except Exception:
            continue
        if not isinstance(j, dict):
            continue
        data = j.get("data") if data_key else j
        if isinstance(data, dict):
            for k in ("indexPrice","lastPrice","price","close","last"):
                if data.get(k) is not None:
                    try:
                        price = float(data.get(k)); break
                    except: pass
            if price is None:
                if data.get("tick") and isinstance(data.get("tick"), dict):
                    try:
                        price = float(data["tick"].get("close"))
                    except:
                        pass
        elif isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
            for k in ("indexPrice","lastPrice","price","close","last"):
                if data[0].get(k) is not None:
                    try:
                        price = float(data[0].get(k)); break
                    except: pass
        elif isinstance(j, dict) and j.get("price") is not None:
            try:
                price = float(j.get("price"))
            except:
                pass
        if price is not None:
            break

    if price is not None:
        _price_cache[symbol] = (price, now)
    return price

# telegram helpers
def tg_send(text):
    try:
        r = requests.post(TG_API + "/sendMessage", data={"chat_id": cfg["CHANNEL"], "text": text, "parse_mode":"HTML"}, timeout=10)
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

def tg_edit(msg_id, text):
    try:
        r = requests.post(TG_API + "/editMessageText", data={"chat_id": cfg["CHANNEL"], "message_id": msg_id, "text": text, "parse_mode":"HTML"}, timeout=10)
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

def tg_pin(msg_id):
    try:
        r = requests.post(TG_API + "/pinChatMessage", data={"chat_id": cfg["CHANNEL"], "message_id": msg_id, "disable_notification": True}, timeout=10)
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

# signing / private API helpers (MEXC private endpoints require headers)
def sign_hmac_sha256(secret,msg):
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()

def api_get_private(path, params_string=""):
    url = MEXC_BASE + path
    req_time = str(int(time.time()*1000))
    access = cfg["MEXC_API_KEY"]
    secret = cfg["MEXC_API_SECRET"]
    target = f"{access}{req_time}{params_string}"
    signature = sign_hmac_sha256(secret, target)
    headers = {"ApiKey": access, "Request-Time": req_time, "Signature": signature, "Content-Type":"application/json"}
    full_url = url if not params_string else url + "?" + params_string
    try:
        r = requests.get(full_url, headers=headers, timeout=12)
    except Exception as e:
        return None, {"ok": False, "error": str(e)}
    try:
        j = r.json()
    except Exception as e:
        return None, {"ok": False, "error": f"Invalid JSON: {e}", "text": r.text[:400]}
    if not j.get("success", False):
        return None, {"ok": False, "api_error": j}
    return j.get("data", None), {"ok": True}

# endpoints wrappers
def fetch_open_positions():
    return api_get_private("/api/v1/private/position/open_positions", "")

def fetch_stoporders_for_position(symbol=None):
    # This fetches SL/TP orders ATTACHED to open positions
    # Used only for adding SL/TP info to the "New Position" message
    params = {}
    if symbol: params["symbol"]=symbol
    params_string = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return api_get_private("/api/v1/private/stoporder/open_orders", params_string)

def fetch_history_positions(symbol=None, page_num=1, page_size=50, start_time=None, end_time=None):
    params = {}
    if symbol: params["symbol"]=symbol
    params["page_num"]=str(page_num); params["page_size"]=str(page_size)
    if start_time: params["start_time"]=str(start_time)
    if end_time: params["end_time"]=str(end_time)
    params_string = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return api_get_private("/api/v1/private/position/list/history_positions", params_string)

# Endpoint for open LIMIT orders
def fetch_open_limit_orders(symbol=None):
    params = {}
    if symbol: params["symbol"]=symbol
    params_string = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return api_get_private("/api/v1/private/order/list/open_orders", params_string)

# Endpoint for open STOP orders (SL/TP)
def fetch_open_stop_orders(symbol=None):
    params = {}
    if symbol: params["symbol"]=symbol
    params_string = "&".join(f"{k}={params[k]}" for k in sorted(params))
    # This is the correct endpoint for ACTIVE (untriggered) SL/TP
    return api_get_private("/api/v1/private/stoporder/open_orders", params_string)


def find_history_position(symbol, positionId, pages=5):
    for p in range(1, pages+1):
        data, meta = fetch_history_positions(symbol=symbol, page_num=p, page_size=100)
        if not meta.get("ok"):
            return None, meta
        items = []
        if isinstance(data, dict) and data.get("resultList"):
            items = data.get("resultList")
        elif isinstance(data, list):
            items = data
        elif isinstance(data, dict) and data.get("data"):
            items = data.get("data")
        for it in items:
            try:
                if str(it.get("positionId")) == str(positionId):
                    return it, {"ok": True}
            except:
                pass
    return None, {"ok": False, "error": "not_found"}

# contract size retrieval (preferred via public detail)
def get_contract_multiplier_from_raw(raw, symbol=None):
    # direct fields
    if not isinstance(raw, dict):
        return None
    for k in ("contractSize","contract_size","multiplier","contract_value","contractVal"):
        if raw.get(k) is not None:
            try:
                return float(raw.get(k))
            except:
                pass
    # try public contract/detail
    if symbol:
        try:
            detail = get_contract_detail_public(symbol)
            if detail and detail.get("contractSize") is not None:
                return float(detail.get("contractSize"))
        except Exception:
            pass
    # fallback inference (less reliable)
    try:
        order_margin = raw.get("orderMargin") or raw.get("usedMargin")
        lev = raw.get("leverage")
        deal_price = raw.get("dealAvgPrice")
        deal_vol = raw.get("dealVol")
        if order_margin and lev and deal_price and deal_vol:
            order_margin = float(order_margin); lev = float(lev)
            deal_price = float(deal_price); deal_vol = float(deal_vol)
            notional = order_margin * lev
            if deal_price * deal_vol > 0:
                mult = notional / (deal_price * deal_vol)
                if mult > 0:
                    return float(mult)
    except Exception:
        pass
    try:
        vol = float(raw.get("holdVol") or raw.get("closeVol") or raw.get("vol") or 0)
        price = float(raw.get("closeAvgPrice") or raw.get("holdAvgPrice") or raw.get("openAvgPrice") or 0)
        im = raw.get("im") or raw.get("oim") or raw.get("orderMargin") or raw.get("usedMargin")
        lev = raw.get("leverage") or None
        if im is not None and lev is not None and price > 0 and vol > 0:
            notional = float(im) * float(lev)
            mult = notional / (price * vol)
            if mult > 0:
                return float(mult)
    except Exception:
        pass
    return None

# size formatting using contract/detail when possible (3 decimals for display)
def format_size_display(raw_holdVol, raw, symbol=None):
    # (v11.7) - Added 'realityVol'
    try:
        vol_from_pos = raw_holdVol
        # For orders, 'vol' is primary. 'realityVol' / 'takeProfitVol' / 'stopLossVol' are fallbacks
        vol_from_order = raw.get('vol') or raw.get('realityVol') or raw.get('takeProfitVol') or raw.get('stopLossVol')
        vol = float(vol_from_pos or vol_from_order or 0)
    except:
        vol = 0.0

    sym = (raw.get("symbol") or symbol or "")
    base_coin = sym.split("_")[0] if "_" in sym else sym or ""
    cm = get_contract_multiplier_from_raw(raw, symbol=sym)
    
    # prefer index price for notional display
    price = None
    try:
        price = get_market_price(sym) or float(raw.get("holdAvgPrice") or raw.get("openAvgPrice") or raw.get("closeAvgPrice") or 0)
    except:
        price = float(raw.get("holdAvgPrice") or raw.get("openAvgPrice") or raw.get("closeAvgPrice") or 0)

    if cm is not None:
        base_amount = vol * cm
        
        # Determine if this is an order to get order-specific price
        is_order_heuristic = 'holdVol' not in raw
        
        order_price = None
        if is_order_heuristic:
            try:
                # 'price' for limit, 'triggerPrice' for stop
                order_price = float(raw.get('price') or raw.get('triggerPrice') or raw.get('stopLossPrice') or raw.get('takeProfitPrice') or 0)
            except: pass
        
        # Use order_price if available, else fallback to market/entry price
        price_to_use = order_price or price

        notional = base_amount * price_to_use if price_to_use and price_to_use > 0 else None
        notional_str = f" (~{notional:,.3f} {(raw.get('quoteCoin') or 'USDT')})" if notional and notional > 0 else ""
        return f"{base_amount:.3f} {base_coin}{notional_str}"
    else:
        # Fallback for unknown contract size (rare)
        try:
            im = raw.get("im") or raw.get("oim") or raw.get("orderMargin") or raw.get("usedMargin")
            lev = raw.get("leverage") or None
            if im is not None and lev is not None and price and price > 0:
                notional = float(im) * float(lev)
                base_amount = notional / price
                return f"est. {base_amount:.3f} {base_coin} — inferred{'' if notional is None else f' (~{notional:,.3f} USDT)'}"
        except:
            pass
        # Fallback if everything fails
        return f"{vol:.3f} (contractSize unknown)"


def compute_next_time_at(hour, minute=0, after=None):
    if after is None:
        after = datetime.now(TZ)
    target = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if after >= target:
        target = target + timedelta(days=1)
    return target

def fetch_all_history_positions_day(start_ms, end_ms):
    page = 1
    page_size = 100
    all_items = []
    while True:
        data, meta = fetch_history_positions(page_num=page, page_size=page_size, start_time=start_ms, end_time=end_ms)
        if not meta.get("ok"):
            return None, meta
        items = []
        if isinstance(data, dict):
            if data.get("resultList"):
                items = data.get("resultList")
            elif data.get("resultList") is None and data.get("data"):
                items = data.get("data")
            else:
                if isinstance(data, list):
                    items = data
        elif isinstance(data, list):
            items = data
        if not items:
            break
        all_items.extend(items)
        if len(items) < page_size:
            break
        page += 1
    return all_items, {"ok": True}

def compute_and_send_daily_pnl_for_day(day_date):
    start_dt = datetime.combine(day_date, dtime(0,0), tzinfo=TZ)
    end_dt = start_dt + timedelta(days=1)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    items, meta = fetch_all_history_positions_day(start_ms, end_ms)
    if not meta.get("ok"):
        print("Daily PnL: failed to fetch history:", meta)
        return False

    total_realised = 0.0
    per_symbol = {}
    for it in items:
        r = None
        for k in ("realised","closeProfitLoss","profit"):
            if it.get(k) is not None:
                try:
                    r = float(it.get(k)); break
                except:
                    try:
                        r = float(str(it.get(k)).replace(",",".")); break
                    except:
                        r = None
        if r is None:
            continue
        total_realised += r
        sym = it.get("symbol") or "UNKNOWN"
        per_symbol[sym] = per_symbol.get(sym, 0.0) + r

    day_str = day_date.strftime("%Y-%m-%d")
    lines = [f"📊 <b>Денний PnL — {day_str} (завершено)</b>",
             f"Сумарний зафіксований PnL: <b>{total_realised:.3f} USDT</b>",
             ""]
    if per_symbol:
        lines.append("Угоди:")
        for i,(s,v) in enumerate(sorted(per_symbol.items(), key=lambda x: -abs(x[1]))[:8],1):
            lines.append(f"{i}) {s}: <b>{v:.3f} USDT</b>")
    msg = "\n".join(lines)
    tg_send(msg)
    return True

# PnL extraction
def extract_realised_and_exit_from_history(hist):
    if not hist or not isinstance(hist, dict):
        return None, None
    realised = hist.get("realised")
    if realised is None:
        realised = hist.get("closeProfitLoss")
    exit_price = hist.get("closeAvgPrice") or hist.get("newCloseAvgPrice") or hist.get("closeAvgPriceFullyScale")
    return realised, exit_price

# compute unrealized PnL for an active position
def compute_upnl_for_position(norm):
    raw = norm.get("raw", {})
    symbol = norm.get("symbol")
    try:
        holdVol = float(norm.get("size") or raw.get("holdVol") or 0)
    except:
        holdVol = 0.0
    try:
        entry = float(norm.get("entryPrice") or raw.get("holdAvgPrice") or 0)
    except:
        entry = 0.0
    side = 1 if raw.get("positionType")==1 or norm.get("side") == "LONG" else 2
    cs = get_contract_multiplier_from_raw(raw, symbol=symbol) or None
    cur = get_market_price(symbol)
    if cs is None or cur is None or entry == 0 or holdVol == 0:
        return None
    if side == 1:
        upnl_quote = (cur - entry) * holdVol * cs
    else:
        upnl_quote = (entry - cur) * holdVol * cs
    fee = 0.0
    try:
        fee = float(raw.get("holdFee") or raw.get("totalFee") or 0)
    except:
        fee = 0.0
    upnl_after_fee = upnl_quote - fee
    upnl_base = upnl_after_fee / cur if cur else None
    return {"upnl_quote": upnl_after_fee, "upnl_base": upnl_base, "cur_price": cur, "contractSize": cs}

# formatting messages (marginRatio removed)
def make_pinned_text(positions):
    lines=["📌 <b>Активні позиції</b>"]
    if not positions:
        lines.append("— Немає активних позицій —")
    else:
        # Додаємо порожній рядок для відступу після заголовка
        lines.append("") 
        
        for i,p in enumerate(positions,1):
            sym = p.get("symbol")
            side = p.get("side")
            entry = p.get("entryPrice") or "-"
            lev = p.get("leverage") or "-"
            size_str = format_size_display(p.get("size"), p.get("raw",{}))
            
            # --- Нова логіка форматування ---
            
            # 1. Заголовок з емодзі для LONG/SHORT
            side_emoji = "🟢" if side == "LONG" else "🔴"
            lines.append(f"<b>{i}) {sym} — {side} {side_emoji}</b>")
            
            # 2. Інформація з відступами
            # (Використовуємо 3 пробіли для імітації "tab")
            
            entry_s = f"{entry:.3f}" if isinstance(entry, (int,float)) else entry
            lines.append(f"   📈 <b>Вхід:</b> {entry_s}")
            
            lines.append(f"   📦 <b>Розмір:</b> {size_str}")
            
            leverage_str = f"{lev}x" if lev != "-" else "-"
            lines.append(f"   💥 <b>Плече:</b> {leverage_str}")

            # 3. UPNL зі знаком (+/-) та емодзі
            upnl = compute_upnl_for_position(p)
            if upnl:
                q = upnl["upnl_quote"]
                # Додаємо знак "+" для позитивного PnL
                sign = "+" if q > 0 else ""
                # Різні емодзі для прибутку/збитку
                upnl_emoji = "💰" if q >= 0 else "📉"
                lines.append(f"   {upnl_emoji} <b>UPNL: <b>{sign}{q:.3f} USDT</b></b>")
            else:
                lines.append(f"   💰 <b>UPNL:</b> <i>розрахунок...</i>")
            
            # Додаємо порожній рядок-роздільник між позиціями
            lines.append("") 

    # Рядок оновлення залишаємо в кінці (без відступу)
    lines.append(f"Оновлено: {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')} ({cfg.get('TIMEZONE')})")
    return "\n".join(lines)

def format_new_position(p, sl=None, tp=None):
    sym=p.get("symbol"); side=p.get("side")
    size_str = format_size_display(p.get("size"), p.get("raw",{}))
    entry = p.get("entryPrice") or "-"
    lev = p.get("leverage") or "-"
    liq = p.get("liquidatePrice") or "-"
    sl_line = f"\nSL: {sl}" if sl else ""
    tp_line = f"\nTP: {tp}" if tp else ""
    entry_s = f"{entry:.3f}" if isinstance(entry, (int,float)) else entry
    return (f"⚡️ <b>Нова позиція (FUTURES)</b>\n<b>{sym} — {side}</b>\n{size_str}\n<b>Вхід:</b> {entry_s}\nПлече: {lev}\nЛіквідація: {liq}{sl_line}{tp_line}\n⏱ {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')} ({cfg.get('TIMEZONE')})")

def format_closed_using_history(old_norm, hist):
    entry = hist.get("holdAvgPrice") or hist.get("openAvgPrice") or old_norm.get("entryPrice") or "-"
    exit_price = hist.get("closeAvgPrice") or hist.get("newCloseAvgPrice") or hist.get("closeAvgPriceFullyScale") or "-"
    realised = hist.get("realised")
    if realised is None: realised = hist.get("closeProfitLoss")
    size_str = format_size_display(old_norm.get("size"), old_norm.get("raw",{}))
    realised_str = f"{float(realised):.3f}" if realised is not None else "—"
    entry_s = f"{float(entry):.3f}" if isinstance(entry, (int,float,str)) and str(entry).replace(".","",1).isdigit() else entry
    exit_s = f"{float(exit_price):.3f}" if isinstance(exit_price, (int,float,str)) and str(exit_price).replace(".","",1).isdigit() else exit_s
    return (f"🟢 <b>Позиція закрита</b>\n"
            f"<b>{old_norm.get('symbol')} — {old_norm.get('side')}</b> — {size_str}\n"
            f"<b>Вхід:</b> {entry_s}\n"
            f"<b>Вихід:</b> {exit_s}\n"
            f"<b>PnL:</b> {realised_str}\n"
            f"⏱ {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')} ({cfg.get('TIMEZONE')})")


def format_closed_fallback(old_norm):
    raw = old_norm.get("raw",{})
    entry = raw.get("holdAvgPrice") or old_norm.get("entryPrice") or "-"
    exit_price = raw.get("closeAvgPrice") or raw.get("newCloseAvgPrice") or "-"
    pnl = None
    for k in ("closeProfitLoss","profit","realised"):
        if raw.get(k) is not None:
            try:
                pnl = float(raw.get(k)); break
            except:
                pnl = raw.get(k)
    if pnl is None:
        try:
            e = float(entry); c = float(exit_price); vol = float(raw.get("holdVol") or raw.get("closeVol") or old_norm.get("size") or 0)
            cm = get_contract_multiplier_from_raw(raw, symbol=raw.get("symbol")) or 1.0
            fee = float(raw.get("totalFee") or raw.get("fee") or 0)
            pnl = (c - e) * vol * cm - fee
        except:
            pnl = None
    pnl_str = f"{float(pnl):.3f}" if pnl is not None else "—"
    size_str = format_size_display(old_norm.get("size"), raw)
    entry_s = f"{float(entry):.3f}" if isinstance(entry, (int,float,str)) and str(entry).replace(".","",1).isdigit() else entry
    exit_s = f"{float(exit_price):.3f}" if isinstance(exit_price, (int,float,str)) and str(exit_price).replace(".","",1).isdigit() else exit_s
    return (f"🟢 <b>Позиція закрита</b>\n"
            f"<b>{old_norm.get('symbol')} — {old_norm.get('side')}</b> — {size_str}\n"
            f"<b>Вхід:</b> {entry_s}\n"
            f"<b>Вихід:</b> {exit_s}\n"
            f"<b>PnL:</b> {pnl_str}\n"
            f"⏱ {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')} ({cfg.get('TIMEZONE')})")


# --- Order Formatting ---

def format_new_limit_order(o):
    sym = o.get("symbol")
    # side 1:OpenLong 2:CloseShort 3:OpenShort 4:CloseLong
    side_map = {1: "Відкриття LONG", 2: "Закриття SHORT", 3: "Відкриття SHORT", 4: "Закриття LONG"}
    side = side_map.get(o.get("side"), "UNKNOWN")
    price = o.get("price") or "-"
    size_str = format_size_display(None, o) # Use order 'vol'
    price_s = f"{float(price):.3f}" if isinstance(price, (int,float,str)) and str(price).replace(".","",1).isdigit() else price
    
    return (f"🔔 <b>Новий лімітний ордер</b>\n"
            f"<b>{sym} — {side}</b>\n"
            f"{size_str}\n"
            f"<b>Ціна:</b> {price_s}\n"
            f"⏱ {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')} ({cfg.get('TIMEZONE')})")

# (v11.9) - Logic to handle SL/TP as one object
def format_new_stop_order(o, positions_map={}):
    sym = o.get("symbol")
    
    # 1. Determine Order Type (SL or TP or both)
    order_types = []
    price_lines = []
    sl_price = o.get("stopLossPrice")
    tp_price = o.get("takeProfitPrice")
    
    try:
        if sl_price and float(sl_price) > 0:
            order_types.append("Stop Loss")
            sl_price_s = f"{float(sl_price):.3f}" if isinstance(sl_price, (int,float,str)) and str(sl_price).replace(".","",1).isdigit() else sl_price
            price_lines.append(f"<b>Stop Loss:</b> {sl_price_s}")
    except: pass
    
    try:
        if tp_price and float(tp_price) > 0:
            order_types.append("Take Profit")
            tp_price_s = f"{float(tp_price):.3f}" if isinstance(tp_price, (int,float,str)) and str(tp_price).replace(".","",1).isdigit() else tp_price
            price_lines.append(f"<b>Take Profit:</b> {tp_price_s}")
    except: pass

    order_type_str = " / ".join(order_types) or "Тригер"

    # 2. Determine Side
    # positionType 1: long, 2: short (This is the position it's attached to)
    side_raw = o.get("positionType")
    side = "UNKNOWN"
    if order_types: # If we identified SL or TP
        if side_raw == 1:
            side = "Закриття LONG"
        elif side_raw == 2:
            side = "Закриття SHORT"

    # 3. Determine Price (Market or Limit)
    # This is less critical, but good to have
    price_type = "Market"
    try:
        if o.get("takeProfitType") == 1: # 1 = limit TP
            price_type = f"Limit {o.get('takeProfitOrderPrice')}"
        elif o.get("stopLossType") == 1: # 1 = limit SL
            price_type = f"Limit {o.get('stopLossOrderPrice')}"
    except:
        pass # Keep default price

    # 4. Get Volume
    size_str = None
    try:
        pid = str(o.get("positionId"))
        if pid and positions_map.get(pid):
            pos_norm = positions_map.get(pid)
            pos_raw = pos_norm.get("raw", {})
            # Use the position's 'holdVol' and 'raw' object
            size_str = format_size_display(pos_raw.get('holdVol'), pos_raw)
    except Exception as e:
        print(f"Error getting SL/TP size from position: {e}")
    
    # Fallback to using the order object itself if position-link fails
    if size_str is None:
        size_str = format_size_display(None, o)

    # 5. Format message
    return (f"🔔 <b>Ордер оновлено: {order_type_str}</b>\n"
            f"<b>{sym} — {side}</b>\n"
            f"{size_str}\n"
            f"{'Ціна ордера: ' + price_type}\n"
            f"{'' if not price_lines else (chr(10).join(price_lines))}\n"
            f"⏱ {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')} ({cfg.get('TIMEZONE')})")

def format_cancelled_order(o, reason="Скасовано"):
    sym = o.get("symbol")
    
    # (v11.9) - Handle combined SL/TP
    order_types = []
    price_lines = []
    sl_price = o.get("stopLossPrice")
    tp_price = o.get("takeProfitPrice")

    try:
        if sl_price and float(sl_price) > 0:
            order_types.append("Stop Loss")
            sl_price_s = f"{float(sl_price):.3f}" if isinstance(sl_price, (int,float,str)) and str(sl_price).replace(".","",1).isdigit() else sl_price
            price_lines.append(f"SL: {sl_price_s}")
    except: pass
    
    try:
        if tp_price and float(tp_price) > 0:
            order_types.append("Take Profit")
            tp_price_s = f"{float(tp_price):.3f}" if isinstance(tp_price, (int,float,str)) and str(tp_price).replace(".","",1).isdigit() else tp_price
            price_lines.append(f"TP: {tp_price_s}")
    except: pass

    if not order_types and o.get("price"):
        order_types.append("Лімітний")
        price_lines.append(f"Ціна: {o.get('price')}")

    order_type_str = " / ".join(order_types) or "Ордер"

    side_map = {1: "LONG", 2: "SHORT"}
    side_raw = o.get("side") or o.get("positionType")
    side = side_map.get(side_raw, "")
    
    price_str = " ".join(price_lines)

    return (f"❌ <b>{order_type_str} {reason}</b>\n"
            f"<b>{sym} {side}</b>\n"
            f"{price_str}\n"
            f"⏱ {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')} ({cfg.get('TIMEZONE')})")

# --- End Order Formatting ---


# daily helper
def compute_next_9am(after=None):
    if after is None:
        after = datetime.now(TZ)
    # create today's 09:00 in TZ
    target = after.replace(hour=9, minute=0, second=0, microsecond=0)
    if after >= target:
        target = target + timedelta(days=1)
    return target

# main loop
def run():
    global state, state_orders
    if "last_daily_pnl_date" not in state:
        state["last_daily_pnl_date"] = None

    # (v11.4) - Ensure state keys exist after load
    if "limit_orders" not in state_orders:
        state_orders["limit_orders"] = {}
    if "stop_orders" not in state_orders:
        state_orders["stop_orders"] = {}
        # Simple migration for old state files
        if "trigger_orders" in state_orders:
            del state_orders["trigger_orders"]


    if not state.get("pinned_message_id"):
        r = tg_send(make_pinned_text([]))
        if r.get("ok"):
            mid = r["result"]["message_id"]
            state["pinned_message_id"] = mid
            pinr = tg_pin(mid)
            if not pinr.get("ok"): print("Pin warn:", pinr)
            save_state(state)
            print("Pinned created", mid)
        else:
            print("Failed create pinned:", r)

    next_daily = compute_next_9am()
    next_daily_pnl = compute_next_time_at(22, 0)
    try:
        if state.get("last_daily_pnl_date"):
            last_date = datetime.fromisoformat(state["last_daily_pnl_date"]).date()
            if last_date >= datetime.now(TZ).date():
                next_daily_pnl = compute_next_time_at(22, 0, after=datetime.now(TZ) + timedelta(days=1))
    except Exception:
        pass

    backoff = 1
    while True:
        try:
            # --- POSITIONS (Existing logic) ---
            pos_data, meta = fetch_open_positions()
            if not meta.get("ok"):
                print("Open positions fetch error:", meta)
                time.sleep(min(backoff,30)); backoff=min(backoff*2,60); continue
            backoff = 1
            
            # 'new_map' (positions map) is now used by order tracking
            new_map = {}
            for p in (pos_data or []):
                pid = str(p.get("positionId"))
                side = "LONG" if p.get("positionType")==1 else "SHORT" if p.get("positionType")==2 else "UNKNOWN"
                norm = {
                    "positionId": pid,
                    "symbol": p.get("symbol"),
                    "side": side,
                    "size": float(p.get("holdVol") or 0),
                    "entryPrice": float(p.get("holdAvgPrice")) if p.get("holdAvgPrice") is not None else None,
                    "leverage": int(p.get("leverage") or 0),
                    "liquidatePrice": p.get("liquidatePrice"),
                    "raw": p
                }
                new_map[pid] = norm
            
            # stoporders for SL/TP (Only used for NEW position message)
            stop_data, stop_meta = fetch_stoporders_for_position()
            stop_map = {}
            if stop_meta.get("ok") and isinstance(stop_data, list):
                for so in stop_data:
                    posid = str(so.get("positionId")) if so.get("positionId") is not None else None
                    if posid:
                        # This just gets latest prices, doesn't track new/cancel
                        stop_map[posid] = {"sl": so.get("stopLossPrice"), "tp": so.get("takeProfitPrice")}
            
            # detect new positions
            for pid, norm in new_map.items():
                if pid not in state["positions"]:
                    sl=None; tp=None
                    if stop_map.get(pid):
                        sl=stop_map[pid].get("sl"); tp=stop_map[pid].get("tp")
                    r = tg_send(format_new_position(norm, sl=sl, tp=tp))
                    if not r.get("ok"): print("Send new pos fail:", r)
                    else: print("Sent new-position", pid)
                    state["positions"][pid]=norm
            
            # detect closed positions
            removed = [pid for pid in list(state["positions"].keys()) if pid not in new_map]
            for pid in removed:
                old = state["positions"].get(pid)
                hist, hm = find_history_position(old.get("symbol"), pid)
                if hm.get("ok") and hist:
                    msg = format_closed_using_history(old, hist)
                else:
                    msg = format_closed_fallback(old)
                tg_send(msg)
                print("Sent closed for", pid)
                del state["positions"][pid]
            
            # update pinned if changed
            pinned_text = make_pinned_text(list(new_map.values()))
            if pinned_text != state.get("last_pinned_text"):
                if state.get("pinned_message_id"):
                    editr = tg_edit(state["pinned_message_id"], pinned_text)
                    if not editr.get("ok"):
                        ec = editr.get("error_code"); desc = editr.get("description","")
                        if not (ec==400 and "message is not modified" in desc):
                            print("Edit pinned fail:", editr)
                state["last_pinned_text"] = pinned_text
            save_state(state)


            # --- Order Tracking ---
            
            # 1. Fetch Open LIMIT Orders
            limit_orders_data, lo_meta = fetch_open_limit_orders()
            new_lo_map = {}
            if lo_meta.get("ok") and isinstance(limit_orders_data, list):
                for o in limit_orders_data:
                    oid = str(o.get("orderId"))
                    new_lo_map[oid] = o
            elif not lo_meta.get("ok"):
                print("Limit orders fetch error:", lo_meta)

            # 2. Fetch Open STOP Orders (SL/TP)
            stop_orders_data, so_meta = fetch_open_stop_orders()
            new_so_map = {}
            # (v11.7) - More robust filter
            finished_states = (2, 3, 4, 5) # 2:canceled, 3:executed, 4:invalidated, 5:failed
            
            if so_meta.get("ok") and isinstance(stop_orders_data, list):
                for o in stop_orders_data:
                    # Filter out "ghost" orders (executed, cancelled, etc.)
                    if o.get("state") in finished_states:
                        continue # Ignore
                    
                    oid = str(o.get("id")) # Key is 'id'
                    new_so_map[oid] = o
            elif not so_meta.get("ok"):
                print("Stop orders fetch error:", so_meta)
            
            # 3. Process new/changed LIMIT orders
            # (Using similar change-detection logic as stop orders now)
            for oid, new_o in new_lo_map.items():
                old_o = state_orders["limit_orders"].get(oid)
                if new_o != old_o:
                    print(f"New or changed limit order: {oid}")
                    tg_send(format_new_limit_order(new_o))
                    state_orders["limit_orders"][oid] = new_o

            # 4. Process new/changed STOP orders (SL/TP)
            # (v11.9) - This logic now detects changes, not just new
            for oid, new_o in new_so_map.items():
                old_o = state_orders["stop_orders"].get(oid)
                if new_o != old_o: # This handles both NEW and CHANGED
                    print(f"New or changed stop order: {oid}")
                    # Pass 'new_map' (positions) to find the volume
                    tg_send(format_new_stop_order(new_o, new_map))
                    state_orders["stop_orders"][oid] = new_o # Add or update

            # 5. Process cancelled/filled LIMIT orders
            lo_removed = [oid for oid in list(state_orders["limit_orders"].keys()) if oid not in new_lo_map]
            for oid in lo_removed:
                print(f"Limit order removed: {oid}")
                # ВИПРАВЛЕНО (v11.10) - Correct .pop() syntax
                old_o = state_orders["limit_orders"].pop(oid) # Use .pop() on the sub-dict
                tg_send(format_cancelled_order(old_o, reason="скасовано/виконано"))

            # 6. Process cancelled/filled STOP orders (SL/TP)
            so_removed = [oid for oid in list(state_orders["stop_orders"].keys()) if oid not in new_so_map]
            for oid in so_removed:
                print(f"Stop order removed: {oid}")
                old_o = state_orders["stop_orders"].pop(oid) # Use .pop()
                tg_send(format_cancelled_order(old_o, reason="скасовано/спрацював"))

            # 7. Save order state
            save_state_orders(state_orders)

            # --- End Order Tracking ---


            # --- time checks (Existing logic) ---
            now = datetime.now(TZ)

            # daily pinned send at 09:00 TZ
            if now >= next_daily:
                r = tg_send(pinned_text)
                if r.get("ok"):
                    print("Daily send done")
                    try:
                        new_mid = r["result"]["message_id"]
                        pinr = tg_pin(new_mid)
                        if not pinr.get("ok"):
                            print("Daily pin failed:", pinr)
                        else:
                            state["pinned_message_id"] = new_mid
                            state["last_pinned_text"] = pinned_text
                            save_state(state)
                            print("Daily message pinned:", new_mid)
                    except Exception as e:
                        print("Daily pin exception:", e)
                else:
                    print("Daily send fail", r)
                next_daily = compute_next_9am(after=now + timedelta(seconds=60))

            # daily PnL summary at 22:00 TZ
            if now >= next_daily_pnl:
                target_day = now.date()
                success = compute_and_send_daily_pnl_for_day(target_day)
                if success:
                    state["last_daily_pnl_date"] = target_day.isoformat()
                    save_state(state)
                    print("Daily PnL sent for", target_day.isoformat())
                else:
                    print("Daily PnL failed for", target_day.isoformat())
                next_daily_pnl = compute_next_time_at(22, 0, after=now + timedelta(seconds=60))

        except Exception as e:
            print("Loop error:", e)
            import traceback
            traceback.print_exc() # Print full error trace
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    print("Start v11.10-orders: MEXC->TG (index price, daily pin 09:00, order tracking)")
    run()