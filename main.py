import requests
import time
import json
import os
import threading
import concurrent.futures
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import Counter

try:
    import mplfinance as mpf
    import pandas as pd
    import matplotlib.pyplot as plt
    CHARTS_AVAILABLE = True
except ImportError:
    CHARTS_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("tsm_v32g.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

if not CHARTS_AVAILABLE:
    logger.warning("mplfinance/pandas not installed — chart images disabled, text signals unaffected. Add mplfinance,pandas,matplotlib to requirements.txt and redeploy to enable.")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8909949122:AAEINK16qv8ALdW2G3R_2Sb93LDsJG0WC6Q")
CHAT_ID        = os.getenv("CHAT_ID", "8005940008")
NEWS_API_KEY   = os.getenv("NEWS_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
AI_REVIEW_ENABLED = os.getenv("AI_REVIEW_ENABLED", "true").strip().lower() != "false"
# DATA_DIR: where every persisted JSON file (trade journal, pattern
# stats/weights, active trades, circuit breaker state, etc.) actually
# lives. Defaults to "." — the container's own working directory —
# which on Railway is EPHEMERAL and gets wiped on every redeploy unless
# a persistent Volume is mounted. Set DATA_DIR=/data in Railway
# Variables AND mount a Volume at /data (Railway dashboard -> service
# -> Volumes -> New Volume, mount path /data) to make all of this
# survive redeploys. Confirmed real before fixing: every one of the 10
# state files this bot writes (adaptive pattern weights, trade history,
# active positions, circuit breaker's daily loss counter) was a bare
# filename with no path prefix — all in the same ephemeral blast radius.
DATA_DIR = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
def data_path(filename):
    return os.path.join(DATA_DIR, filename)

BINANCE_PRICE_URL   = "https://data-api.binance.vision/api/v3/ticker/price"
BINANCE_KLINE_URL   = "https://data-api.binance.vision/api/v3/klines"
BINANCE_FUTURES_PRICE_URL = "https://fapi.binance.com/fapi/v1/ticker/price"
BINANCE_FUTURES_KLINE_URL = "https://fapi.binance.com/fapi/v1/klines"
BYBIT_KLINE_URL = "https://api.bybit.com/v5/market/kline"
OKX_KLINE_URL = "https://www.okx.com/api/v5/market/candles"
FUTURES_ONLY_SYMBOLS = {"XAUUSDT", "XAGUSDT", "COPPERUSDT"}
BINANCE_FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
BINANCE_OI_URL      = "https://fapi.binance.com/futures/data/openInterestHist"

trade_lock = threading.Lock()
IST        = ZoneInfo("Asia/Kolkata")

COINS = list(dict.fromkeys([
    "HYPE","BERA","IP","INIT","BABY","SAHARA","WAL","LAYER",
    "RED","SPK","NEWT","KERNEL","EPT","COOKIE","BIO","VVV","ARC","BANK","DEXE",

    "BTC","ETH","BNB","SOL","XRP","DOGE","ADA","TRX","AVAX","SHIB",
    "DOT","LINK","BCH","NEAR","LTC","UNI","APT","ETC","HBAR","FIL",
    "ARB","VET","INJ","OP","ATOM","TIA","SUI","SEI","ALGO","EGLD",
    "FLOW","EOS","XTZ","AAVE","MKR","GRT","SNX","COMP","CRV","SUSHI",
    "LDO","CAKE","1INCH","DYDX","GMX","ENS","PENDLE","RNDR","FET","WLD",
    "AR","THETA","LPT","AKT","TAO","XLM","AXS","GALA","CHZ","APE",
    "GMT","ENJ","PEPE","WIF","FLOKI","BONK","ORDI","BOME","NOT","DOGS",
    "JUP","PYTH","JTO","STRK","EIGEN","ETHFI","IO","ZERO","ONDO",
    "BLUR","CFX","METIS","MANTA","ZETA","TRB","ALT","PIXEL","PORTAL","STPT","KAS",
    "PIPPIN","BSB","CL","LAB","PAXG"
]))

active_trades             = {}
pending_signals           = {}
hourly_queue              = {}
sent_coins                = []
daily_losses              = 0
total_scan_cycles         = 0
radar_coins_added         = 0
failed_close_notifications = {}
radar_coins_triggered     = 0
circuit_breaker_until     = None
last_reset_day            = datetime.now(IST).date()
trade_journal             = []
JOURNAL_MAX_LIVE_ENTRIES  = 2000
learning_notes            = []
coin_cooldowns            = {}
early_watch_sent          = {}
evaluating_signals        = {}
macro_coils               = {}
retest_watchlist          = {}
htf_zones_cache           = {}
daily_levels_cache        = {}
crt_range_cache           = {}
consecutive_loss_patterns = {}
price_alerts              = {}
market_memory = {
    "bull":     {"wins":0,"losses":0,"best_pattern":None},
    "bear":     {"wins":0,"losses":0,"best_pattern":None},
    "sideways": {"wins":0,"losses":0,"best_pattern":None}
}
pattern_stats = {p: {"signals":0,"wins":0,"losses":0,"total_pnl":0.0,"weight":1.0,
                     "bull_wr":0.0,"bear_wr":0.0,"sideways_wr":0.0} for p in [
    "RSI Reversal","Double Bottom","Double Top",
    "Bullish Engulfing","Bearish Engulfing","Bull Flag Formation","Bear Flag Formation",
    "BOS Breakout","Change of Character (ChoCh)","Liquidity Sweep","Volatility Contraction (Coiling)","Pre-Breakout Compression",
    "Inside Bar Coil","BOS-Retest","BOS Retest (Sniper Entry)","Early Spark Ignition","Pressure Cooker Triangle","Vanguard Macro Squeeze","Smart Money Absorption","Funding Divergence Sniper","Trend Continuation Coil","5m Multi-TF Sniper","Order Flow Sniper","Yellow Circle Sniper","Pre-Breakout Macro","PDL Reversal Sweep","PDH Reversal Sweep","ChoCh + Fib 0.618 Golden Zone","Candle Range Theory","Inverse Fair Value Gap (IFVG)","Order Block","Quasimodo","Metals MTF"
]}

last_update_id         = None
last_river_time        = 0
last_hourly_time       = time.time()
last_pnl_update_time   = time.time() + 1800
last_8h_desk_time      = time.time()
_bot_start_time        = time.time()
last_pressure_cooker_time = time.time()
last_metals_scan_time = 0
last_weekly_report_day = None

SCAN_INTERVAL            = 90
RIVER_INTERVAL           = 900
MIN_SETUP_SCORE          = 90
ACCUMULATION_SCORE_FLOOR = 86.0
MIN_PRIMARY_SCORE        = 85
INSTANT_SIGNAL_THRESHOLD = 97
GRADE_A_THRESHOLD        = 92.2

PREMIUM_COINS             = {"BTC","ETH","BNB","SOL","PAXG","XRP","ADA","LINK","AVAX",
                              "HYPE","BERA","IP","INIT","BABY","SAHARA","WAL","LAYER",
                              "RED","SPK","NEWT","KERNEL","EPT","COOKIE","BIO","VVV","ARC","BANK"}
MIN_PROFIT_TARGET        = 15.0
SIGNAL_EXPIRY_MINUTES    = 120
INSTANT_EXPIRY_MINUTES   = 30
DELAY_BETWEEN_COINS      = 0.05
MAX_SIGNALS_PER_CYCLE    = 3
MAX_ACTIVE_TRADES        = 5
MAX_MACRO_COILS          = 15  # real gap found and fixed this round: macro_coils had no cap at all — combined with the new detection notification, an unbounded watchlist risked a burst of "NEW RADAR WATCH" messages the same way the old spam-loop bug did
ATR_SL_MULTIPLIER        = 2.5
ATR_TP_MULTIPLIER        = 5.0
MIN_RR_RATIO             = 2.0
MAX_DAILY_LOSSES         = 3
CIRCUIT_BREAKER_MIN_LOSS = -5.0
ATR_VOLATILITY_RATIO     = 3.0
CONSEC_LOSS_SUSPEND      = 5
MIN_SIGNALS_TO_SUSPEND   = 15
SUSPEND_HOURS            = 12
ADX_MIN_TREND            = 21
ST_PERIOD                = 10
ST_MULTIPLIER            = 3.0
MIN_SL_PCT               = 0.003
DEAD_HOUR_START          = 2
DEAD_HOUR_END            = 7

LONDON_OPEN_HOUR         = 12
LONDON_GOLDEN_END_HOUR   = 14
NY_OPEN_HOUR             = 17
NY_GOLDEN_END_HOUR       = 19

def is_golden_hour():
    """Point 4: "Golden Hours" vs Dead Zones. Returns True during the first"""
    hour = datetime.now(IST).hour
    in_london = LONDON_OPEN_HOUR <= hour < LONDON_GOLDEN_END_HOUR
    in_ny = NY_OPEN_HOUR <= hour < NY_GOLDEN_END_HOUR
    return in_london or in_ny

MACRO_EVENT_PAUSE_MIN_BEFORE = 30
MACRO_EVENT_PAUSE_MIN_AFTER  = 30

SQUEEZE_FUNDING_EXTREME_NEG = -0.0003
SQUEEZE_FUNDING_EXTREME_POS = 0.0003
SQUEEZE_OI_RISING_PCT       = 3.0

SCHEDULED_MACRO_EVENTS = [
]

def is_macro_event_window():
    """Point 4(b): checks the manually-maintained scheduled events list."""
    now = get_ist_datetime()
    for ev_str in SCHEDULED_MACRO_EVENTS:
        try:
            date_part = ev_str.split("#")[0].strip()
            ev_time = datetime.strptime(date_part, "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        except Exception:
            continue
        window_start = ev_time - timedelta(minutes=MACRO_EVENT_PAUSE_MIN_BEFORE)
        window_end = ev_time + timedelta(minutes=MACRO_EVENT_PAUSE_MIN_AFTER)
        if window_start <= now <= window_end:
            return True, f"scheduled macro event at {ev_time.strftime('%H:%M IST')}"
    return False, ""

def is_weekend_low_liquidity():
    """Point 4(a): Sat/Sun chop detection — doesn't need an API, just the clock."""
    now = get_ist_datetime()
    return now.weekday() in (5, 6)

BTC_CORRELATED           = ["ETH","BNB","SOL","AVAX","NEAR","APT","SUI"]

SECTOR_GROUPS = {
    "gaming":     ["SAND","MANA","AXS","GALA","ENJ","PIXEL","LAB","GMT","APE"],
    "layer1":     ["ETH","SOL","AVAX","NEAR","APT","SUI","ADA","DOT","ATOM","TIA","SEI","ALGO","EGLD","FLOW","KAS"],
    "defi":       ["UNI","AAVE","MKR","SNX","COMP","CRV","SUSHI","LDO","CAKE","1INCH","DYDX","GMX","PENDLE"],
    "meme":       ["DOGE","SHIB","PEPE","WIF","FLOKI","BONK","ORDI","BOME","NOT","DOGS"],
    "ai_compute": ["RNDR","FET","WLD","AR","AKT","IO","THETA"],
    "l2":         ["ARB","OP","STRK","METIS","ZETA","MANTA"],
    "oracle_data":["LINK","PYTH","GRT","BLUR"],
}
COIN_SECTOR = {}
for _sector, _coins in SECTOR_GROUPS.items():
    for _c in _coins:
        COIN_SECTOR[_c] = _sector
LEV_TIER_1               = ["BTC","ETH"]
LEV_TIER_2               = ["BNB","SOL","XRP","ADA","AVAX","DOT","LINK","LTC",
                             "NEAR","UNI","ATOM","APT","SUI","ARB","OP","INJ"]
LEV_TIER_3               = ["DOGE","SHIB","PEPE","WIF","FLOKI","BONK","DOGS",
                             "BOME","NOT","APE","GMT","CHZ","GALA"]
BOT_VERSION = "v32G"
BOT_NAME    = "TRADING SIGNAL MASTER"
BOT_HEADER  = f"⚙️ {BOT_NAME} {BOT_VERSION}"

PRE_BREAKOUT_ENGINE_PATTERNS = {
    "Inside Bar Coil", "Pre-Breakout Compression", "Volatility Contraction (Coiling)",
    "Early Spark Ignition", "Smart Money Absorption", "Funding Divergence Sniper",
    "Pressure Cooker Triangle", "Trend Continuation Coil", "BOS-Retest",
    "BOS Retest (Sniper Entry)", "Change of Character (ChoCh)", "Liquidity Sweep",
    "PDL Reversal Sweep", "PDH Reversal Sweep", "ChoCh + Fib 0.618 Golden Zone",
    "BOS Breakout", "Double Top", "Double Bottom", "Bull Flag Formation", "Bear Flag Formation",
    "Candle Range Theory",
    "Inverse Fair Value Gap (IFVG)",
    "Order Block",
    "Quasimodo",
    # RECLASSIFIED from the removed Blitz engine: all three already ran
    # through the standard accumulation-exempt/AI-review pipeline
    # (format_and_send with is_instant=False), never Blitz's fast-track
    # bypass — they were only ever labeled Blitz cosmetically.
    "Yellow Circle Sniper", "Order Flow Sniper", "5m Multi-TF Sniper",
}

def get_engine_label(pattern_name):
    """Maps any real pattern name to a human-readable engine label."""
    primary = pattern_name.split(" + ")[0] if pattern_name else ""
    if primary.startswith("Pre-Breakout Macro"):
        return "🛰️ RADAR ENGINE"
    if primary.startswith("Metals MTF"):
        return "🥇 METALS ENGINE"
    if primary in PRE_BREAKOUT_ENGINE_PATTERNS:
        return "🧭 SCOUT ENGINE"
    return "📊 SIGNAL ENGINE"


def S(c="━",n=30): return c*n
def fmt_pnl(v): return ("🟢 " if v>=0 else "🔴 ")+f"{v:+.2f}%"

def atomic_json_write(path, data):
    """Atomic JSON write: write to a temp file first, then os.replace() to"""
    tmp_path = f"{path}.tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(data, f)
        os.replace(tmp_path, path)
    except Exception as e:
        logger.error(f"atomic_json_write {path}: {e}")
        try:
            if os.path.exists(tmp_path): os.remove(tmp_path)
        except Exception:
            pass

def save_active_trades():
    with trade_lock:
        try:
            s={k:{**v,"timestamp":v["timestamp"].isoformat(),
                  "expires_at":v["expires_at"].isoformat() if v.get("expires_at") else None}
               for k,v in active_trades.items()}
            atomic_json_write(data_path("active_trades.json"), s)
        except Exception as e: logger.error(f"save_active_trades: {e}")

def load_active_trades():
    global active_trades
    try:
        if os.path.exists(data_path("active_trades.json")):
            with open(data_path("active_trades.json")) as f: data=json.load(f)
            active_trades={k:{**v,
                "timestamp":datetime.fromisoformat(v["timestamp"]),
                "expires_at":datetime.fromisoformat(v["expires_at"]) if v.get("expires_at") else None}
                for k,v in data.items()}
            logger.info(f"Loaded {len(active_trades)} active trades.")
    except Exception as e: logger.error(f"load_active_trades: {e}")

def save_trade_history():
    with trade_lock:
        try:
            atomic_json_write(data_path("trades.json"), pattern_stats)
        except Exception as e: logger.error(f"save_trade_history: {e}")

def load_trade_history():
    global pattern_stats
    try:
        if os.path.exists(data_path("trades.json")):
            with open(data_path("trades.json")) as f: loaded=json.load(f)
            for p in pattern_stats:
                if p in loaded: pattern_stats[p]=loaded[p]
    except Exception as e: logger.error(f"load_trade_history: {e}")

def save_journal():
    global trade_journal
    try:
        if len(trade_journal) > JOURNAL_MAX_LIVE_ENTRIES:
            overflow_count = len(trade_journal) - JOURNAL_MAX_LIVE_ENTRIES
            overflow = trade_journal[:overflow_count]
            with open(data_path("journal_archive.jsonl"), "a") as f:
                for entry in overflow:
                    f.write(json.dumps(entry) + "\n")
            trade_journal = trade_journal[overflow_count:]
            logger.info(f"Archived {overflow_count} journal entries to journal_archive.jsonl (kept most recent {JOURNAL_MAX_LIVE_ENTRIES} live)")
        atomic_json_write(data_path("journal.json"), trade_journal)
    except Exception as e: logger.error(f"save_journal: {e}")

def load_journal():
    global trade_journal
    try:
        if os.path.exists(data_path("journal.json")):
            with open(data_path("journal.json")) as f: trade_journal=json.load(f)
        logger.info(f"Loaded {len(trade_journal)} journal entries.")
    except Exception as e: logger.error(f"load_journal: {e}")

def save_learning():
    try:
        atomic_json_write(data_path("learning.json"), {"notes":learning_notes,"memory":market_memory,"clp":consecutive_loss_patterns})
    except Exception as e: logger.error(f"save_learning: {e}")

def load_learning():
    global learning_notes,market_memory,consecutive_loss_patterns
    try:
        if os.path.exists(data_path("learning.json")):
            with open(data_path("learning.json")) as f: data=json.load(f)
            learning_notes=data.get("notes",[])
            market_memory.update(data.get("memory",{}))
            consecutive_loss_patterns=data.get("clp",{})
    except Exception as e: logger.error(f"load_learning: {e}")

def save_alerts():
    try:
        atomic_json_write(data_path("alerts.json"), price_alerts)
    except Exception as e: logger.error(f"save_alerts: {e}")

def load_alerts():
    global price_alerts
    try:
        if os.path.exists(data_path("alerts.json")):
            with open(data_path("alerts.json")) as f: price_alerts=json.load(f)
    except Exception as e: logger.error(f"load_alerts: {e}")

def save_pending_signals():
    try:
        s={}
        for coin,sig in list(pending_signals.items()):
            d=dict(sig)
            if isinstance(d.get("timestamp"),datetime): d["timestamp"]=d["timestamp"].isoformat()
            if isinstance(d.get("expires_at"),datetime): d["expires_at"]=d["expires_at"].isoformat()
            s[coin]=d
        atomic_json_write(data_path("pending_signals.json"), s)
    except Exception as e: logger.error(f"save_pending: {e}")

def save_evaluating_signals():
    try:
        s = {}
        for coin, data in list(evaluating_signals.items()):
            d = dict(data)
            if isinstance(d.get("logged_at"), datetime):
                d["logged_at"] = d["logged_at"].isoformat()
            s[coin] = d
        atomic_json_write(data_path("evaluating_signals.json"), s)
    except Exception as e: logger.error(f"save_evaluating_signals: {e}")

def load_evaluating_signals():
    global evaluating_signals
    try:
        if not os.path.exists(data_path("evaluating_signals.json")): return
        with open(data_path("evaluating_signals.json")) as f: data = json.load(f)
        for coin, d in data.items():
            if d.get("logged_at"):
                try: d["logged_at"] = datetime.fromisoformat(d["logged_at"])
                except Exception: d["logged_at"] = get_ist_datetime()
            evaluating_signals[coin] = d
        logger.info(f"Loaded {len(evaluating_signals)} evaluating signals.")
    except Exception as e: logger.error(f"load_evaluating_signals: {e}")

def save_retest_watchlist():
    try:
        s={}
        for coin,w in list(retest_watchlist.items()):
            d=dict(w)
            if isinstance(d.get("logged_at"),datetime): d["logged_at"]=d["logged_at"].isoformat()
            s[coin]=d
        atomic_json_write(data_path("retest_watchlist.json"), s)
    except Exception as e: logger.error(f"save_retest_watchlist: {e}")

def load_retest_watchlist():
    global retest_watchlist
    try:
        if not os.path.exists(data_path("retest_watchlist.json")): return
        with open(data_path("retest_watchlist.json")) as f: data=json.load(f)
        for coin,w in data.items():
            if w.get("logged_at"):
                try: w["logged_at"]=datetime.fromisoformat(w["logged_at"])
                except Exception: w["logged_at"]=get_ist_datetime()
            retest_watchlist[coin]=w
        logger.info(f"Loaded {len(retest_watchlist)} retest watchlist entries.")
    except Exception as e: logger.error(f"load_retest_watchlist: {e}")

def save_macro_events():
    """Point 2: Persists SCHEDULED_MACRO_EVENTS to disk so events added via"""
    try:
        atomic_json_write(data_path("macro_events.json"), SCHEDULED_MACRO_EVENTS)
    except Exception as e: logger.error(f"save_macro_events: {e}")

def load_macro_events():
    global SCHEDULED_MACRO_EVENTS
    try:
        if not os.path.exists(data_path("macro_events.json")): return
        with open(data_path("macro_events.json")) as f: data=json.load(f)
        if isinstance(data,list):
            SCHEDULED_MACRO_EVENTS = data
            logger.info(f"Loaded {len(SCHEDULED_MACRO_EVENTS)} macro events.")
    except Exception as e: logger.error(f"load_macro_events: {e}")

def load_pending_signals():
    global pending_signals
    try:
        if not os.path.exists(data_path("pending_signals.json")): return
        with open(data_path("pending_signals.json")) as f: data=json.load(f)
        now=get_ist_datetime()
        for coin,sig in data.items():
            if sig.get("expires_at"):
                try:
                    exp=datetime.fromisoformat(sig["expires_at"])
                    if now>exp: continue
                    sig["expires_at"]=exp
                except Exception: continue
            if sig.get("timestamp"):
                try: sig["timestamp"]=datetime.fromisoformat(sig["timestamp"])
                except Exception: pass
            pending_signals[coin]=sig
        logger.info(f"Loaded {len(pending_signals)} pending signals.")
    except Exception as e: logger.error(f"load_pending: {e}")

def save_circuit_breaker():
    try:
        atomic_json_write(data_path("cb.json"), {"daily_losses":daily_losses,
                       "circuit_breaker_until":circuit_breaker_until,
                       "date":str(last_reset_day)})
    except Exception as e: logger.error(f"save_cb: {e}")

def load_circuit_breaker():
    global daily_losses,circuit_breaker_until,last_reset_day
    try:
        if os.path.exists(data_path("cb.json")):
            with open(data_path("cb.json")) as f: data=json.load(f)
            if data.get("date")==str(datetime.now(IST).date()):
                daily_losses=data.get("daily_losses",0)
                circuit_breaker_until=data.get("circuit_breaker_until")
    except Exception as e: logger.error(f"load_cb: {e}")

def cloud_save_journal():       save_journal();       save_trade_history()
def cloud_save_pattern_stats(): save_trade_history()
def cloud_save_learning():      save_learning()
def cloud_save_active_trades(): save_active_trades()
def cloud_load_all():
    """Load all data from local JSON files on startup."""
    load_active_trades(); load_trade_history()
    load_journal();       load_learning()
    logger.info("Local JSON data loaded.")

def format_price(p):
    if p>=1000:   return f"{p:.2f}"
    elif p>=1:    return f"{p:.4f}"
    elif p>=0.01: return f"{p:.6f}"
    else:         return f"{p:.8f}"

def get_ist_time():     return datetime.now(IST).strftime("%I:%M:%S %p IST")
def get_ist_datetime(): return datetime.now(IST)

def generate_signal_chart(symbol, klines, entry, sl, tp, direction, coin, interval="15m",
                          pattern_name=None, zone_ok=False, zone_low=None, zone_high=None,
                          has_bos=False, has_sweep=False, lev=1, profit_target=None,
                          st_ok=None, vwap_ok=None, vol_ratio=None, adx_val=None, rsi_val=None,
                          sup=None, res=None, opp_zone_low=None, opp_zone_high=None, opp_zone_is_tp=False):
    """Visual chart alerts — full version per explicit request to add as much"""
    if not CHARTS_AVAILABLE:
        return None
    try:
        recent = klines[-60:] if len(klines) >= 60 else klines
        df = pd.DataFrame(
            [[float(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])] for k in recent],
            columns=["time","Open","High","Low","Close","Volume"]
        )
        df["time"] = pd.to_datetime(df["time"], unit="ms")
        df.set_index("time", inplace=True)

        sl_pct = abs(entry - sl) / entry * 100 if entry > 0 else 0
        tp_pct = abs(tp - entry) / entry * 100 if entry > 0 else 0
        rr_ratio = tp_pct / sl_pct if sl_pct > 0 else 0
        dir_word = "LONG" if direction == "BUY" else "SHORT"
        title = f"{coin}/USDT   {dir_word}   {interval} Chart   |   R:R 1:{rr_ratio:.1f}"

        p1_price = p2_price = None
        m1_pnl = m2_pnl = 0
        if profit_target:
            m1_pnl = profit_target*0.30; m2_pnl = profit_target*0.60
            p1_price = price_at_pnl(entry, direction, lev, m1_pnl)
            p2_price = price_at_pnl(entry, direction, lev, m2_pnl)

        hline_prices = [entry, sl, tp]
        hline_colors = ["#0088aa","red","#00aa33"]
        hline_widths = [1.5,1.5,1.5]
        hline_styles = ["--","--","--"]
        if p1_price is not None:
            hline_prices += [p1_price, p2_price]
            hline_colors += ["#997a00","#997a00"]
            hline_widths += [1.0,1.0]
            hline_styles += [":",":"]
        if sup is not None and res is not None:
            hline_prices += [sup, res]
            hline_colors += ["#666666","#666666"]
            hline_widths += [0.9,0.9]
            hline_styles += ["-.","-."]

        hlines = dict(hlines=hline_prices, colors=hline_colors, linewidths=hline_widths, linestyle=hline_styles)

        fig, axlist = mpf.plot(
            df, type="candle", style="charles", hlines=hlines,
            volume=False, figsize=(10,8), title=title, returnfig=True
        )
        ax = axlist[0]
        xmin, xmax = ax.get_xlim()
        ymin, ymax = ax.get_ylim()

        if direction == "BUY":
            ax.axhspan(entry, tp, facecolor="#00cc44", alpha=0.08)
            ax.axhspan(sl, entry, facecolor="red", alpha=0.08)
            sl_sign, tp_sign = "-", "+"
        else:
            ax.axhspan(tp, entry, facecolor="#00cc44", alpha=0.08)
            ax.axhspan(entry, sl, facecolor="red", alpha=0.08)
            sl_sign, tp_sign = "+", "-"

        if zone_ok and zone_low is not None and zone_high is not None:
            zone_word = "DEMAND ZONE" if direction == "BUY" else "SUPPLY ZONE"
            ax.axhspan(zone_low, zone_high, xmin=0.55, xmax=1.0, facecolor="orange", alpha=0.18,
                      edgecolor="darkorange", linewidth=1)
            ax.text(xmin+(xmax-xmin)*0.57, (zone_low+zone_high)/2, zone_word, fontsize=7.5,
                   color="darkorange", fontweight="bold", ha="left", va="center",
                   bbox=dict(boxstyle="round,pad=0.2", facecolor="white", edgecolor="darkorange", alpha=0.85))

        if opp_zone_low is not None and opp_zone_high is not None:
            base_word = "SUPPLY ZONE" if direction == "BUY" else "DEMAND ZONE"
            opp_word = f"TARGET ZONE ({base_word})" if opp_zone_is_tp else base_word
            ax.axhspan(opp_zone_low, opp_zone_high, xmin=0.0, xmax=0.45, facecolor="purple", alpha=0.13,
                      edgecolor="purple", linewidth=1)
            ax.text(xmin+(xmax-xmin)*0.02, (opp_zone_low+opp_zone_high)/2, opp_word, fontsize=7.5,
                   color="purple", fontweight="bold", ha="left", va="center",
                   bbox=dict(boxstyle="round,pad=0.2", facecolor="white", edgecolor="purple", alpha=0.85))

        if has_bos:
            bx = xmin + (xmax-xmin)*0.25
            by_target = entry+(ymax-entry)*0.25 if direction=="BUY" else entry-(entry-ymin)*0.25
            ax.annotate("BOS Confirmed", xy=(bx, by_target), xytext=(bx, ymax-(ymax-ymin)*0.06),
                       fontsize=8, color="black", fontweight="bold", ha="center",
                       arrowprops=dict(arrowstyle="->", color="black", lw=1.1))

        if has_sweep:
            sx = xmin + (xmax-xmin)*0.85
            ax.annotate("Liquidity Sweep", xy=(sx, sl), xytext=(sx, sl - (ymax-ymin)*0.08 if direction=="BUY" else sl + (ymax-ymin)*0.08),
                       fontsize=8, color="black", fontweight="bold", ha="center",
                       arrowprops=dict(arrowstyle="->", color="black", lw=1.1))

        if pattern_name:
            ax.text(0.02, 0.97, f"Pattern: {pattern_name}", transform=ax.transAxes,
                   fontsize=9, color="black", fontweight="bold", ha="left", va="top",
                   bbox=dict(boxstyle="round,pad=0.35", facecolor="#fff9e6", edgecolor="black", linewidth=1))

        if sup is not None and res is not None:
            ax.text(xmax, res, f"  R {format_price(res)}", va="bottom", ha="left",
                   fontsize=7.5, color="#666666", style="italic")
            ax.text(xmax, sup, f"  S {format_price(sup)}", va="top", ha="left",
                   fontsize=7.5, color="#666666", style="italic")

        ax.text(xmax, entry, f"  ENTRY {format_price(entry)}", va="center", ha="left",
               fontsize=9, color="#0088aa", fontweight="bold")
        ax.text(xmax, sl, f"  SL {format_price(sl)} ({sl_sign}{sl_pct:.1f}%)", va="center", ha="left",
               fontsize=9, color="red", fontweight="bold")
        ax.text(xmax, tp, f"  TP {format_price(tp)} ({tp_sign}{tp_pct:.1f}%)", va="center", ha="left",
               fontsize=9, color="#00aa33", fontweight="bold")
        if p1_price is not None:
            ax.text(xmax, p1_price, f"  P1 +{m1_pnl:.0f}%", va="center", ha="left", fontsize=7.5, color="#997a00")
            ax.text(xmax, p2_price, f"  P2 +{m2_pnl:.0f}%", va="center", ha="left", fontsize=7.5, color="#997a00")

        filter_parts = []
        if st_ok is not None:   filter_parts.append(f"ST:{'OK' if st_ok else 'X'}")
        if vwap_ok is not None: filter_parts.append(f"VWAP:{'OK' if vwap_ok else 'X'}")
        filter_parts.append(f"Zone:{'OK' if zone_ok else 'X'}")
        if vol_ratio is not None: filter_parts.append(f"Vol:{vol_ratio:.1f}x")
        if adx_val is not None:   filter_parts.append(f"ADX:{adx_val:.0f}")
        if rsi_val is not None:   filter_parts.append(f"RSI:{rsi_val:.0f}")
        if filter_parts:
            fig.text(0.13, 0.94, "   ".join(filter_parts), fontsize=8.5, color="#333333",
                    ha="left", family="monospace")

        save_path = f"/tmp/chart_{coin}_{int(time.time())}.png"
        fig.savefig(save_path, dpi=100, bbox_inches="tight")
        plt.close(fig)
        return save_path
    except Exception as e:
        logger.warning(f"generate_signal_chart {coin}: {e}")
        return None


def send_telegram_photo(photo_path, caption=""):
    """Sends an image via Telegram's sendPhoto endpoint (multipart file"""
    try:
        with open(photo_path, "rb") as f:
            files = {"photo": f}
            data = {"chat_id": CHAT_ID}
            if caption: data["caption"] = caption
            res = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                data=data, files=files, timeout=20
            )
        if res.status_code != 200:
            logger.warning(f"send_telegram_photo [{res.status_code}]: {res.text[:200]}")
            return False
        return True
    except Exception as e:
        logger.warning(f"send_telegram_photo: {e}")
        return False
    finally:
        try:
            if os.path.exists(photo_path): os.remove(photo_path)
        except Exception as e:
            logger.warning(f"send_telegram_photo cleanup: {e}")


def send_telegram(text, parse_mode="HTML", reply_markup=None, disable_web_page_preview=True):
    payload={"chat_id":CHAT_ID,"text":text,"parse_mode":parse_mode,
             "disable_web_page_preview":disable_web_page_preview}
    if reply_markup: payload["reply_markup"]=reply_markup
    try:
        res=requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                          json=payload,timeout=15)
        if res.status_code!=200:
            logger.warning(f"Telegram [{res.status_code}]: {res.text[:200]}")
            if "parse" in res.text.lower() or "can't parse" in res.text.lower():
                payload2={"chat_id":CHAT_ID,"text":text,
                          "disable_web_page_preview":True}
                if reply_markup: payload2["reply_markup"]=reply_markup
                res2=requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                                   json=payload2,timeout=15)
                return res2.status_code==200
        return res.status_code==200
    except requests.RequestException as e:
        logger.error(f"Telegram error: {e}"); return False

def safe_send(fn, label="command"):
    """Call any dashboard function safely — always sends something even on error."""
    try:
        result = fn()
        if result:
            send_telegram(result)
        else:
            send_telegram(f"⚠️ <b>{label}</b> returned empty — no data yet.")
    except Exception as e:
        logger.error(f"safe_send {label}: {e}")
        send_telegram(f"⚠️ <b>{label}</b> — error: <code>{str(e)[:100]}</code>")

def answer_callback(cbid, text="OK"):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
                      json={"callback_query_id":cbid,"text":text},timeout=10)
    except Exception as e:
        logger.warning(f"answerCallback: {e}")

def get_price(symbol):
    """Originally XAU/XAG (and briefly PAXG) were routed to Binance Futures"""
    price_url = BINANCE_FUTURES_PRICE_URL if symbol in FUTURES_ONLY_SYMBOLS else BINANCE_PRICE_URL
    try:
        res=requests.get(price_url,params={"symbol":symbol},timeout=10)
        if res.status_code==200: return float(res.json()["price"])
        if symbol in FUTURES_ONLY_SYMBOLS:
            logger.warning(f"get_price {symbol}: Futures endpoint returned {res.status_code} — "
                          f"if this persists for PAXG, remove it from FUTURES_ONLY_SYMBOLS")
        return None
    except Exception as e:
        logger.warning(f"get_price {symbol}: {e}"); return None

def get_klines(symbol,interval,limit=100):
    """See get_price's docstring for the Futures-routing reasoning."""
    kline_url = BINANCE_FUTURES_KLINE_URL if symbol in FUTURES_ONLY_SYMBOLS else BINANCE_KLINE_URL
    try:
        res=requests.get(kline_url,
                         params={"symbol":symbol,"interval":interval,"limit":limit},timeout=10)
        if res.status_code==200: return res.json()
        if symbol in FUTURES_ONLY_SYMBOLS:
            logger.warning(f"get_klines {symbol}: Futures endpoint returned {res.status_code} — "
                          f"if this persists for PAXG, remove it from FUTURES_ONLY_SYMBOLS")
        return []
    except Exception as e:
        logger.warning(f"get_klines {symbol}: {e}"); return []

def calculate_ema(closes,period):
    if len(closes)<period: return None
    ema=sum(closes[:period])/period
    k=2.0/(period+1)
    for p in closes[period:]: ema=p*k+ema*(1-k)
    return ema

def calculate_rsi(closes,period=14):
    """VERIFIED AND FIXED a real, significant math bug (this round): the"""
    if len(closes)<period+1: return 50.0
    gains,losses=[],[]
    for i in range(1,len(closes)):
        d=closes[i]-closes[i-1]
        gains.append(max(0,d)); losses.append(max(0,-d))
    if len(gains)<period: return 50.0
    avg_gain=sum(gains[:period])/period
    avg_loss=sum(losses[:period])/period
    for i in range(period,len(gains)):
        avg_gain=(avg_gain*(period-1)+gains[i])/period
        avg_loss=(avg_loss*(period-1)+losses[i])/period
    if avg_loss==0: return 100.0
    rs=avg_gain/avg_loss
    return 100.0-(100.0/(1+rs))

def calculate_atr(klines,period=14):
    if len(klines)<period+1: return 0.0
    trs=[]
    for i in range(1,len(klines)):
        h=float(klines[i][2]); l=float(klines[i][3]); pc=float(klines[i-1][4])
        trs.append(max(h-l,abs(h-pc),abs(l-pc)))
    return sum(trs[-period:])/period

def calculate_adx(klines,period=14):
    """VERIFIED AND FIXED a real, serious bug (this round): the previous"""
    if len(klines)<period*2+1: return 0.0
    try:
        highs=[float(k[2]) for k in klines]; lows=[float(k[3]) for k in klines]
        closes=[float(k[4]) for k in klines]
        pdm,mdm,trl=[],[],[]
        for i in range(1,len(klines)):
            hd=highs[i]-highs[i-1]; ld=lows[i-1]-lows[i]
            pdm.append(hd if hd>ld and hd>0 else 0)
            mdm.append(ld if ld>hd and ld>0 else 0)
            trl.append(max(highs[i]-lows[i],abs(highs[i]-closes[i-1]),abs(lows[i]-closes[i-1])))
        def smooth(data,p):
            s=sum(data[:p]); r=[s]
            for v in data[p:]: s=s-s/p+v; r.append(s)
            return r
        atr_s=smooth(trl,period); pdm_s=smooth(pdm,period); mdm_s=smooth(mdm,period)
        pdi=[100*p/a if a else 0 for p,a in zip(pdm_s,atr_s)]
        mdi=[100*m/a if a else 0 for m,a in zip(mdm_s,atr_s)]
        dx=[100*abs(p-m)/(p+m) if (p+m) else 0 for p,m in zip(pdi,mdi)]
        return sum(dx[-period:])/period if len(dx)>=period else 0.0
    except Exception as e:
        logger.warning(f"ADX calculation error: {e}"); return 0.0

def calculate_vwap(klines):
    try:
        tp=sum(((float(k[2])+float(k[3])+float(k[4]))/3)*float(k[5]) for k in klines)
        tv=sum(float(k[5]) for k in klines)
        return tp/tv if tv>0 else None
    except Exception: return None

def calculate_vwap_with_bands(klines):
    """The Law of Mean Reversion: VWAP Standard Deviation Bands."""
    if len(klines) < 20: return None, None, None
    try:
        cum_vol = 0
        cum_pv = 0
        typical_prices = []
        vols = []

        for k in klines:
            h, l, c, v = float(k[2]), float(k[3]), float(k[4]), float(k[5])
            tp = (h + l + c) / 3
            cum_vol += v
            cum_pv += tp * v
            typical_prices.append(tp)
            vols.append(v)

        if cum_vol == 0: return None, None, None
        vwap = cum_pv / cum_vol

        dev_sum = 0
        for i in range(len(typical_prices)):
            dev_sum += vols[i] * ((typical_prices[i] - vwap) ** 2)

        variance = dev_sum / cum_vol
        std_dev = variance ** 0.5

        upper_band_2sd = vwap + (2 * std_dev)
        lower_band_2sd = vwap - (2 * std_dev)

        return vwap, upper_band_2sd, lower_band_2sd
    except Exception:
        return None, None, None

def get_point_of_control(klines, bins=12):
    """The Law of Liquidity Gravity (Volume Profile / Point of Control)."""
    if len(klines) < 50: return None
    try:
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        vols = [float(k[5]) for k in klines]

        max_p = max(highs)
        min_p = min(lows)
        if max_p == min_p: return None

        bin_size = (max_p - min_p) / bins
        volume_profile = {i: 0.0 for i in range(bins)}

        for i in range(len(klines)):
            h, l, v = highs[i], lows[i], vols[i]
            start_bin = int((l - min_p) / bin_size)
            end_bin = int((h - min_p) / bin_size)
            start_bin = max(0, min(start_bin, bins - 1))
            end_bin = max(0, min(end_bin, bins - 1))
            bins_touched = (end_bin - start_bin) + 1
            vol_per_bin = v / bins_touched if bins_touched > 0 else 0
            for b in range(start_bin, end_bin + 1):
                volume_profile[b] += vol_per_bin

        poc_bin = max(volume_profile, key=volume_profile.get)
        poc_price = min_p + (poc_bin * bin_size) + (bin_size / 2)
        return poc_price
    except Exception as e:
        logger.warning(f"get_point_of_control: {e}")
        return None

def detect_rsi_divergence(closes):
    if len(closes)<10: return None
    try:
        prices=closes[-6:]
        rsi_vals=[calculate_rsi(closes[:i+1]) for i in range(len(closes)-6,len(closes))]
        if prices[-1]<prices[0] and rsi_vals[-1]>rsi_vals[0]: return "BULLISH_DIV"
        if prices[-1]>prices[0] and rsi_vals[-1]<rsi_vals[0]: return "BEARISH_DIV"
        return None
    except Exception: return None

def get_recent_swing_levels(klines, lookback=20):
    """Backward-only swing level detector — fixes the audit's root-cause"""
    if len(klines) < lookback + 1:
        return 0, 0
    highs = [float(k[2]) for k in klines[-(lookback+1):-1]]
    lows  = [float(k[3]) for k in klines[-(lookback+1):-1]]
    if not highs or not lows:
        return 0, 0
    return max(highs), min(lows)


def detect_market_structure(klines):
    """Audit Fix #7: Real market structure — HH/HL/LH/LL + BOS + CHOCH detection."""
    if len(klines) < 30: return {"bias": "neutral", "bos": False, "choch": False, "swing_high": 0, "swing_low": 0}
    highs = [float(k[2]) for k in klines]
    lows  = [float(k[3]) for k in klines]
    closes= [float(k[4]) for k in klines]
    swing_highs, swing_lows = [], []
    for i in range(5, len(klines) - 5):
        if highs[i] == max(highs[i-5:i+6]): swing_highs.append((i, highs[i]))
        if lows[i]  == min(lows[i-5:i+6]):  swing_lows.append((i, lows[i]))
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return {"bias": "neutral", "bos": False, "choch": False,
                "swing_high": max(highs[-20:]), "swing_low": min(lows[-20:])}
    sh = swing_highs[-3:]; sl = swing_lows[-3:]
    hh = len(sh) >= 2 and sh[-1][1] > sh[-2][1]
    hl = len(sl) >= 2 and sl[-1][1] > sl[-2][1]
    lh = len(sh) >= 2 and sh[-1][1] < sh[-2][1]
    ll = len(sl) >= 2 and sl[-1][1] < sl[-2][1]
    if hh and hl:   bias = "bullish"
    elif lh and ll: bias = "bearish"
    else:           bias = "neutral"
    last_sh = swing_highs[-1][1] if swing_highs else max(highs[-20:])
    last_sl = swing_lows[-1][1]  if swing_lows  else min(lows[-20:])
    bos_bull  = closes[-1] > last_sh and bias == "bullish"
    bos_bear  = closes[-1] < last_sl and bias == "bearish"
    bos = bos_bull or bos_bear
    choch = (closes[-1] < last_sl and bias == "bullish") or \
            (closes[-1] > last_sh and bias == "bearish")
    return {"bias": bias, "bos": bos, "choch": choch,
            "swing_high": last_sh, "swing_low": last_sl,
            "hh": hh, "hl": hl, "lh": lh, "ll": ll}


def detect_supply_demand_zones(klines):
    """Audit Fix #5: Professional S&D zones — unmitigated, multi-retest, volume-confirmed."""
    zones = {"demand": [], "supply": []}
    if len(klines) < 30: return zones
    try:
        closes = [float(k[4]) for k in klines]
        opens  = [float(k[1]) for k in klines]
        highs  = [float(k[2]) for k in klines]
        lows   = [float(k[3]) for k in klines]
        vols   = [float(k[5]) for k in klines]
        avg_vol = sum(vols[-30:]) / 30
        for i in range(5, len(klines) - 3):
            body = abs(closes[i] - opens[i])
            avg_body = sum(abs(closes[j] - opens[j]) for j in range(i-4, i)) / 4
            if avg_body == 0: continue
            is_strong_move = body > avg_body * 1.8
            high_vol = vols[i] > avg_vol * 1.3
            if not (is_strong_move and high_vol): continue
            zone_high = max(highs[i-2:i+1])
            zone_low  = min(lows[i-2:i+1])
            future_closes = closes[i+1:]
            if closes[i] > opens[i]:
                mitigated = any(c < zone_low for c in future_closes)
                if not mitigated:
                    retests = sum(1 for c in future_closes if zone_low * 0.995 <= c <= zone_high * 1.005)
                    zones["demand"].append({
                        "high": zone_high, "low": zone_low,
                        "retests": retests, "vol_strength": vols[i] / avg_vol,
                        "unmitigated": True
                    })
            else:
                mitigated = any(c > zone_high for c in future_closes)
                if not mitigated:
                    retests = sum(1 for c in future_closes if zone_low * 0.995 <= c <= zone_high * 1.005)
                    zones["supply"].append({
                        "high": zone_high, "low": zone_low,
                        "retests": retests, "vol_strength": vols[i] / avg_vol,
                        "unmitigated": True
                    })
        for key in zones:
            zones[key].sort(key=lambda z: (z["retests"] in [1,2], z["vol_strength"]), reverse=True)
    except Exception as e:
        logger.warning(f"S&D zones: {e}")
    return zones




def calculate_supertrend(klines, period=10, multiplier=3.0):
    """Audit Fix #6: Real SuperTrend with proper band tracking over time."""
    if len(klines) < period + 5: return None
    try:
        highs  = [float(k[2]) for k in klines]
        lows   = [float(k[3]) for k in klines]
        closes = [float(k[4]) for k in klines]
        trs = []
        for i in range(1, len(klines)):
            h = highs[i]; l = lows[i]; pc = closes[i-1]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        atr_vals = []
        atr = sum(trs[:period]) / period
        atr_vals.append(atr)
        for tr in trs[period:]:
            atr = (atr * (period - 1) + tr) / period
            atr_vals.append(atr)
        direction = 1
        prev_upper = prev_lower = 0
        for i in range(len(atr_vals)):
            idx = i + period
            if idx >= len(closes): break
            hl2 = (highs[idx] + lows[idx]) / 2
            upper = hl2 + multiplier * atr_vals[i]
            lower = hl2 - multiplier * atr_vals[i]
            if i > 0:
                lower = max(lower, prev_lower) if closes[idx-1] > prev_lower else lower
                upper = min(upper, prev_upper) if closes[idx-1] < prev_upper else upper
            prev_upper = upper; prev_lower = lower
            if closes[idx] > upper:   direction = 1
            elif closes[idx] < lower: direction = -1
        return "BUY" if direction == 1 else "SELL"
    except Exception: return None


def detect_bull_flag(closes, highs, lows, vols, avg_vol):
    """Predictive Bull Flag — detects the channel FORMATION, not the"""
    if len(closes) < 30: return False
    impulse_bars = closes[-25:-10]
    impulse_gain = (impulse_bars[-1] - impulse_bars[0]) / impulse_bars[0] * 100 if impulse_bars[0] > 0 else 0
    if impulse_gain < 3.0: return False

    consol_highs = highs[-10:]
    consol_lows = lows[-10:]
    consol_range = (max(consol_highs) - min(consol_lows)) / min(consol_lows) * 100 if min(consol_lows) > 0 else 999
    if consol_range > 5.0: return False

    if min(consol_lows) < impulse_bars[0]: return False

    impulse_avg_vol = sum(vols[-25:-10]) / 15
    consol_avg_vol  = sum(vols[-10:])  / 10
    if consol_avg_vol >= impulse_avg_vol * 0.85: return False

    return True


def detect_bear_flag(closes, highs, lows, vols, avg_vol):
    """Predictive Bear Flag — mirror of the predictive bull flag above."""
    if len(closes) < 30: return False
    impulse_bars = closes[-25:-10]
    impulse_drop = (impulse_bars[0] - impulse_bars[-1]) / impulse_bars[0] * 100 if impulse_bars[0] > 0 else 0
    if impulse_drop < 3.0: return False

    consol_highs = highs[-10:]
    consol_lows = lows[-10:]
    consol_range = (max(consol_highs) - min(consol_lows)) / min(consol_lows) * 100 if min(consol_lows) > 0 else 999
    if consol_range > 5.0: return False

    if max(consol_highs) > impulse_bars[0]: return False

    impulse_avg_vol = sum(vols[-25:-10]) / 15
    consol_avg_vol  = sum(vols[-10:])  / 10
    if consol_avg_vol >= impulse_avg_vol * 0.85: return False

    return True


def detect_double_bottom_pro(highs, lows, closes, vols, price, avg_vol):
    """Institutional Double Bottom — Audit Fix #1, extended with a Liquidity"""
    if len(lows) < 50: return False, 0
    region = lows[-50:]
    low1_idx = region.index(min(region[:-15])) if len(region) > 15 else region.index(min(region))

    region2_start = low1_idx + 8
    if region2_start >= len(region) - 2: return False, 0
    region2 = region[region2_start:]
    if not region2: return False, 0
    low2_val = min(region2)
    low2_idx = region2_start + region2.index(low2_val)

    low1_val = region[low1_idx]
    similarity = abs(low1_val - low2_val) / low1_val if low1_val > 0 else 1
    if similarity > 0.015: return False, 0

    neckline = max(highs[-50 + low1_idx : -50 + low2_idx + 1] or [0])
    if neckline == 0: return False, 0

    neckline_broken = price > neckline * 1.002 and vols[-1] > avg_vol * 1.1

    is_sweep = lows[-2] <= low1_val and closes[-2] > low1_val

    fired = neckline_broken or is_sweep
    return fired, (low2_val if fired else 0)


def detect_double_top_pro(highs, lows, closes, vols, price, avg_vol):
    """Institutional Double Top — mirror of detect_double_bottom_pro, same"""
    if len(highs) < 50: return False, 0
    region = highs[-50:]
    high1_idx = region.index(max(region[:-15])) if len(region) > 15 else region.index(max(region))

    region2_start = high1_idx + 8
    if region2_start >= len(region) - 2: return False, 0
    region2 = region[region2_start:]
    if not region2: return False, 0
    high2_val = max(region2)
    high2_idx = region2_start + region2.index(high2_val)

    high1_val = region[high1_idx]
    similarity = abs(high1_val - high2_val) / high1_val if high1_val > 0 else 1
    if similarity > 0.015: return False, 0

    neckline = min(lows[-50 + high1_idx : -50 + high2_idx + 1] or [999999])
    if neckline == 999999: return False, 0

    neckline_broken = price < neckline * 0.998 and vols[-1] > avg_vol * 1.1
    is_sweep = highs[-2] >= high1_val and closes[-2] < high1_val

    fired = neckline_broken or is_sweep
    return fired, (high2_val if fired else 0)




def detect_volatility_contraction(closes, highs, lows, vols, price):
    """Point 2: Volatility Contraction Pattern (VCP) — catches the setup BEFORE"""
    if len(closes) < 40: return None, 0
    lookback = closes[-40:]
    look_highs = highs[-40:]
    look_lows = lows[-40:]
    look_vols = vols[-40:]

    impulse = lookback[:-12]
    contraction = lookback[-12:]
    contraction_highs = look_highs[-12:]
    contraction_lows = look_lows[-12:]
    contraction_vols = look_vols[-12:]
    impulse_vols = look_vols[:-12]

    if len(impulse) < 10 or not impulse_vols: return None, 0

    impulse_move_pct = (impulse[-1] - impulse[0]) / impulse[0] * 100 if impulse[0] > 0 else 0

    range_high = max(contraction_highs)
    range_low = min(contraction_lows)
    range_pct = (range_high - range_low) / price * 100 if price > 0 else 99

    avg_impulse_vol = sum(impulse_vols) / len(impulse_vols)
    avg_contraction_vol = sum(contraction_vols) / len(contraction_vols)
    vol_dying = avg_contraction_vol < avg_impulse_vol * 0.75

    pos_in_range = (price - range_low) / (range_high - range_low) if range_high > range_low else 0.5

    tight_enough = range_pct < 3.5
    if not tight_enough or not vol_dying:
        return None, 0

    tightness_score = max(0, 100 - range_pct * 15)

    if impulse_move_pct > 4.0 and pos_in_range > 0.55:
        return "BUY", tightness_score
    if impulse_move_pct < -4.0 and pos_in_range < 0.45:
        return "SELL", tightness_score

    return None, 0


def detect_pre_breakout_compression(closes, highs, lows, vols, price, sup, res, direction_bias):
    """Pre-Breakout Compression — catches the coil BEFORE a BOS Breakout"""
    if len(closes) < 20 or res <= 0 or sup <= 0:
        return None, 0

    recent_highs = highs[-5:]
    recent_lows = lows[-5:]
    recent_vols = vols[-5:]
    avg_vol_20 = sum(vols[-20:]) / 20 if len(vols) >= 20 else (vols[-1] if vols else 1)

    avg_recent_vol = sum(recent_vols) / len(recent_vols)
    volume_quiet = avg_recent_vol < avg_vol_20 * 0.85

    typical_range = (max(highs[-20:]) - min(lows[-20:])) / 20 if len(highs) >= 20 else 1
    tight_candles = all((h - l) < typical_range * 0.8 for h, l in zip(recent_highs, recent_lows)) if typical_range > 0 else False

    if not volume_quiet or not tight_candles:
        return None, 0

    dist_to_res_pct = (res - price) / res * 100 if res > 0 else 99
    dist_to_sup_pct = (price - sup) / sup * 100 if sup > 0 else 99

    tightness_score = max(0, 100 - (max(recent_highs) - min(recent_lows)) / price * 100 * 20)

    if 0 <= dist_to_res_pct <= 1.0 and direction_bias == "bullish":
        return "BUY", tightness_score
    if 0 <= dist_to_sup_pct <= 1.0 and direction_bias == "bearish":
        return "SELL", tightness_score

    return None, 0


def detect_macro_triangle_1h(klines_1h):
    """1H Ascending/Descending Triangle detector — Category 5 (comprehensive"""
    if len(klines_1h) < 30:
        return None, 0, None, 0

    highs = [float(k[2]) for k in klines_1h[-30:]]
    lows = [float(k[3]) for k in klines_1h[-30:]]
    vols = [float(k[5]) for k in klines_1h[-30:]]

    recent_highs = highs[-15:]
    recent_lows = lows[-15:]
    earlier_highs = highs[:15]
    earlier_lows = lows[:15]
    recent_vols = vols[-15:]
    earlier_vols = vols[:15]

    if not earlier_vols or not recent_vols:
        return None, 0, None, 0
    avg_earlier_vol = sum(earlier_vols) / len(earlier_vols)
    avg_recent_vol = sum(recent_vols) / len(recent_vols)
    vol_contracting = avg_recent_vol < avg_earlier_vol * 0.85

    earlier_range = max(earlier_highs) - min(earlier_lows)
    recent_range = max(recent_highs) - min(recent_lows)
    range_narrowing = recent_range < earlier_range * 0.75 if earlier_range > 0 else False

    if not vol_contracting or not range_narrowing:
        return None, 0, None, 0

    flat_res = max(recent_highs)
    res_touches = sum(1 for h in recent_highs if flat_res > 0 and abs(h - flat_res) / flat_res <= 0.003)
    third = len(recent_lows) // 3
    if third >= 2:
        low_1 = min(recent_lows[:third])
        low_2 = min(recent_lows[third:third*2])
        low_3 = min(recent_lows[third*2:])
        rising_lows = low_1 < low_2 < low_3
        if res_touches >= 2 and rising_lows:
            tightness = max(0, 100 - (recent_range / flat_res * 100) * 15) if flat_res > 0 else 0
            return "BUY", tightness, "Ascending Triangle (1H)", flat_res

    flat_sup = min(recent_lows)
    sup_touches = sum(1 for l in recent_lows if flat_sup > 0 and abs(l - flat_sup) / flat_sup <= 0.003)
    if third >= 2:
        high_1 = max(recent_highs[:third])
        high_2 = max(recent_highs[third:third*2])
        high_3 = max(recent_highs[third*2:])
        falling_highs = high_1 > high_2 > high_3
        if sup_touches >= 2 and falling_highs:
            tightness = max(0, 100 - (recent_range / flat_sup * 100) * 15) if flat_sup > 0 else 0
            return "SELL", tightness, "Descending Triangle (1H)", flat_sup

    return None, 0, None, 0


def detect_macro_4h_sweep(klines_4h):
    """4H Liquidity Sweep (Spring/Upthrust) — Category 5, point 16. Reuses"""
    if len(klines_4h) < 20:
        return None, 0, None, 0
    highs = [float(k[2]) for k in klines_4h]
    lows = [float(k[3]) for k in klines_4h]
    closes = [float(k[4]) for k in klines_4h]
    opens = [float(k[1]) for k in klines_4h]
    ms = detect_market_structure(klines_4h)
    sup = ms["swing_low"] if ms["swing_low"] > 0 else min(lows[-20:-1])
    res = ms["swing_high"] if ms["swing_high"] > 0 else max(highs[-20:-1])
    sweep_dir, sweep_strength = detect_liquidity_sweep(klines_4h, highs, lows, closes, opens, sup, res, ms)
    if sweep_dir == "BUY":
        return "BUY", sweep_strength, "4H Liquidity Sweep", max(highs[-3:])
    elif sweep_dir == "SELL":
        return "SELL", sweep_strength, "4H Liquidity Sweep", min(lows[-3:])
    return None, 0, None, 0


def detect_macro_divergence_bottom(symbol, klines_4h):
    """4H Double Bottom with Bullish RSI Divergence — high-conviction macro"""
    if len(klines_4h) < 40: return None, 0, None, 0

    closes = [float(k[4]) for k in klines_4h]
    highs = [float(k[2]) for k in klines_4h]
    lows = [float(k[3]) for k in klines_4h]

    recent_low = min(lows[-10:])
    older_low = min(lows[-40:-15])

    if older_low <= 0:
        return None, 0, None, 0

    if abs(recent_low - older_low) / older_low < 0.015:
        rsi_recent = calculate_rsi(closes)
        rsi_older = calculate_rsi(closes[:-15])

        if rsi_recent > rsi_older + 5.0:
            neckline = max(highs[-40:-10])
            return "BUY", 90.0, "4H Double Bottom + Bullish Div", neckline

    return None, 0, None, 0


def detect_macro_choch_4h(klines_4h, ms_4h):
    """4H Change of Character — detects macro structure flipping from"""
    if not ms_4h["choch"]: return None, 0, None, 0

    closes = [float(k[4]) for k in klines_4h]
    if ms_4h["bias"] == "bearish" and closes[-1] > ms_4h["swing_high"]:
        return "BUY", 88.0, "4H Change of Character (Bullish)", ms_4h["swing_high"]
    elif ms_4h["bias"] == "bullish" and closes[-1] < ms_4h["swing_low"]:
        return "SELL", 88.0, "4H Change of Character (Bearish)", ms_4h["swing_low"]

    return None, 0, None, 0


def detect_macro_cup_and_handle(klines_1h):
    """1H Cup and Handle (and Inverse) — completes the Category 5 macro"""
    if len(klines_1h) < 60:
        return None, 0, None, 0

    closes = [float(k[4]) for k in klines_1h]
    highs = [float(k[2]) for k in klines_1h]
    lows = [float(k[3]) for k in klines_1h]
    vols = [float(k[5]) for k in klines_1h]

    handle_highs = highs[-12:]
    handle_lows = lows[-12:]
    handle_vols = vols[-12:]

    cup_highs = highs[-50:-12]
    cup_lows = lows[-50:-12]

    if not cup_highs or not cup_lows:
        return None, 0, None, 0

    left_lip = max(cup_highs[:15])
    right_lip = max(cup_highs[-10:])
    cup_bottom = min(cup_lows)

    if right_lip > 0 and abs(left_lip - right_lip) / right_lip < 0.025:
        cup_depth = (right_lip - cup_bottom) / cup_bottom if cup_bottom > 0 else 0
        if cup_depth > 0.10:
            handle_low = min(handle_lows)

            if handle_low > cup_bottom + ((right_lip - cup_bottom) * 0.5):
                avg_cup_vol = sum(vols[-50:-12]) / 38
                avg_handle_vol = sum(handle_vols) / 12

                if avg_handle_vol < avg_cup_vol * 0.85:
                    tightness = 90.0
                    level = max(left_lip, right_lip)
                    return "BUY", tightness, "Cup & Handle (1H)", level

    inv_left_lip = min(cup_lows[:15])
    inv_right_lip = min(cup_lows[-10:])
    cup_top = max(cup_highs)

    if inv_right_lip > 0 and abs(inv_left_lip - inv_right_lip) / inv_right_lip < 0.025:
        cup_height = (cup_top - inv_right_lip) / inv_right_lip if inv_right_lip > 0 else 0
        if cup_height > 0.10:
            handle_high = max(handle_highs)

            if handle_high < cup_top - ((cup_top - inv_right_lip) * 0.5):
                avg_cup_vol = sum(vols[-50:-12]) / 38
                avg_handle_vol = sum(handle_vols) / 12

                if avg_handle_vol < avg_cup_vol * 0.85:
                    tightness = 90.0
                    level = min(inv_left_lip, inv_right_lip)
                    return "SELL", tightness, "Inv Cup & Handle (1H)", level

    return None, 0, None, 0


def detect_macro_wedge(klines_1h):
    """1H Macro Wedge (Falling = Bullish, Rising = Bearish). A contracting"""
    if len(klines_1h) < 30: return None, 0, None, 0

    highs = [float(k[2]) for k in klines_1h[-30:]]
    lows = [float(k[3]) for k in klines_1h[-30:]]
    closes = [float(k[4]) for k in klines_1h[-30:]]

    first_half_highs = highs[:15]
    first_half_lows = lows[:15]
    second_half_highs = highs[15:]
    second_half_lows = lows[15:]

    h_start = max(first_half_highs)
    h_end = max(second_half_highs)
    l_start = min(first_half_lows)
    l_end = min(second_half_lows)

    range_start = h_start - l_start
    range_end = h_end - l_end
    if range_start <= 0: return None, 0, None, 0

    is_contracting = range_end < range_start * 0.75
    if not is_contracting: return None, 0, None, 0

    if h_end < h_start * 0.99 and l_end < l_start:
        drop_highs = h_start - h_end
        drop_lows = l_start - l_end
        if drop_lows >= l_start * 0.003 and drop_highs > drop_lows * 1.2:
            tightness = max(0, 100 - (range_end / closes[-1] * 100) * 15) if closes[-1] > 0 else 0
            return "BUY", tightness, "Falling Wedge (1H)", h_end

    if l_end > l_start * 1.01 and h_end > h_start:
        rise_lows = l_end - l_start
        rise_highs = h_end - h_start
        if rise_highs >= h_start * 0.003 and rise_lows > rise_highs * 1.2:
            tightness = max(0, 100 - (range_end / closes[-1] * 100) * 15) if closes[-1] > 0 else 0
            return "SELL", tightness, "Rising Wedge (1H)", l_end

    return None, 0, None, 0


def detect_macro_head_and_shoulders(klines_1h):
    """1H Macro Head & Shoulders and Inverse Head & Shoulders."""
    if len(klines_1h) < 45: return None, 0, None, 0

    highs = [float(k[2]) for k in klines_1h[-45:]]
    lows = [float(k[3]) for k in klines_1h[-45:]]

    head_high = max(highs)
    head_idx = highs.index(head_high)

    if 15 < head_idx < 30:
        left_shoulder = max(highs[:head_idx-5])
        right_shoulder = max(highs[head_idx+5:])

        if left_shoulder < head_high * 0.95 and right_shoulder < head_high * 0.95:
            if abs(left_shoulder - right_shoulder) / left_shoulder < 0.10:
                neckline = min(lows[head_idx-8:head_idx+8])
                return "SELL", 88.0, "Head & Shoulders (1H)", neckline

    head_low = min(lows)
    head_idx = lows.index(head_low)

    if 15 < head_idx < 30:
        left_shoulder = min(lows[:head_idx-5])
        right_shoulder = min(lows[head_idx+5:])

        if left_shoulder > head_low * 1.05 and right_shoulder > head_low * 1.05:
            if abs(left_shoulder - right_shoulder) / left_shoulder < 0.10:
                neckline = max(highs[head_idx-8:head_idx+8])
                return "BUY", 88.0, "Inverse H&S (1H)", neckline

    return None, 0, None, 0


def detect_macro_setups_4h_1h(symbol):
    """Dedicated macro-timeframe detector — the real fix for the "orphan"""
    try:
        klines_4h = get_klines(symbol, "4h", 45)
        if klines_4h:
            p_dir, p_qual, p_name, p_lvl = detect_macro_pennant_4h(klines_4h)
            if p_dir:
                return p_name, p_dir, p_qual, p_lvl

            s_dir, s_qual, s_name, s_lvl = detect_macro_4h_sweep(klines_4h)
            if s_dir:
                return s_name, s_dir, s_qual, s_lvl

            e_dir, e_qual, e_name, e_lvl = detect_macro_ema_reclaim(symbol, klines_4h)
            if e_dir:
                return e_name, e_dir, e_qual, e_lvl

            div_dir, div_qual, div_name, div_lvl = detect_macro_divergence_bottom(symbol, klines_4h)
            if div_dir:
                return div_name, div_dir, div_qual, div_lvl

            ms_4h = detect_market_structure(klines_4h)
            choch_dir, choch_qual, choch_name, choch_lvl = detect_macro_choch_4h(klines_4h, ms_4h)
            if choch_dir:
                return choch_name, choch_dir, choch_qual, choch_lvl

        klines_1h = get_klines(symbol, "1h", 60)
        if klines_1h:
            tri_dir, tri_qual, tri_name, tri_lvl = detect_macro_triangle_1h(klines_1h)
            if tri_dir:
                return tri_name, tri_dir, tri_qual, tri_lvl

            b_dir, b_qual, b_name, b_lvl = detect_macro_rectangle_box(klines_1h)
            if b_dir:
                return b_name, b_dir, b_qual, b_lvl

            c_dir, c_qual, c_name, c_lvl = detect_macro_cup_and_handle(klines_1h)
            if c_dir:
                return c_name, c_dir, c_qual, c_lvl

            w_dir, w_qual, w_name, w_lvl = detect_macro_wedge(klines_1h)
            if w_dir:
                return w_name, w_dir, w_qual, w_lvl

            hs_dir, hs_qual, hs_name, hs_lvl = detect_macro_head_and_shoulders(klines_1h)
            if hs_dir:
                return hs_name, hs_dir, hs_qual, hs_lvl

        return None, None, 0, 0
    except Exception as e:
        logger.warning(f"detect_macro_setups_4h_1h {symbol}: {e}")
        return None, None, 0, 0


def detect_fair_value_gaps(klines, lookback=40):
    """Fair Value Gap (FVG) — a 3-candle imbalance where candle 1 and candle"""
    if len(klines) < 3:
        return []
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    closes = [float(k[4]) for k in klines]

    start = max(2, len(klines) - lookback)
    gaps = []
    for i in range(start, len(klines)):
        c1_high, c1_low = highs[i-2], lows[i-2]
        c3_high, c3_low = highs[i], lows[i]

        if c1_high < c3_low:
            top, bottom, direction = c3_low, c1_high, "BUY"
        elif c1_low > c3_high:
            top, bottom, direction = c1_low, c3_high, "SELL"
        else:
            continue

        disrespected = False
        for j in range(i + 1, len(closes)):
            if direction == "BUY" and closes[j] < bottom:
                disrespected = True; break
            if direction == "SELL" and closes[j] > top:
                disrespected = True; break

        gaps.append({"direction": direction, "top": top, "bottom": bottom,
                      "formed_idx": i, "disrespected": disrespected})
    return gaps


def detect_inverse_fvg(klines, price, lookback=40):
    """Inverse Fair Value Gap (IFVG) — an FVG that got disrespected (price"""
    gaps = detect_fair_value_gaps(klines, lookback)
    for g in gaps:
        if not g["disrespected"]:
            continue
        ifvg_direction = "SELL" if g["direction"] == "BUY" else "BUY"
        top, bottom = g["top"], g["bottom"]
        if bottom <= price <= top:
            return ifvg_direction, top, bottom
    return None, 0, 0


def check_retest_rejection_candle(o, h, l, c, direction, level):
    """Real candle-level confirmation for a retest — the piece"""
    candle_range = h - l
    if candle_range <= 0:
        return False
    body = abs(c - o)
    body_pct = body / candle_range * 100

    if direction == "BUY":
        if c <= level:
            return False
        lower_wick_pct = (min(o, c) - l) / candle_range * 100
        strong_bull_close = c > o and body_pct >= 55.0
        return lower_wick_pct >= 35.0 or strong_bull_close
    else:
        if c >= level:
            return False
        upper_wick_pct = (h - max(o, c)) / candle_range * 100
        strong_bear_close = c < o and body_pct >= 55.0
        return upper_wick_pct >= 35.0 or strong_bear_close


def detect_candle_range_theory(klines_htf, klines_ltf):
    """Candle Range Theory (CRT) — matches the reference diagram exactly:"""
    if len(klines_htf) < 2 or len(klines_ltf) < 3:
        return None, 0, 0

    ref = klines_htf[-2]
    crh, crl = float(ref[2]), float(ref[3])
    if crh <= crl:
        return None, 0, 0

    highs = [float(k[2]) for k in klines_ltf]
    lows = [float(k[3]) for k in klines_ltf]
    closes = [float(k[4]) for k in klines_ltf]
    opens = [float(k[1]) for k in klines_ltf]

    for i in range(len(klines_ltf) - 2, max(len(klines_ltf) - 8, 0), -1):
        if lows[i] < crl and closes[i] > crl:
            if check_retest_rejection_candle(opens[i], highs[i], lows[i], closes[i], "BUY", crl):
                return "BUY", crh, crl
        if highs[i] > crh and closes[i] < crh:
            if check_retest_rejection_candle(opens[i], highs[i], lows[i], closes[i], "SELL", crh):
                return "SELL", crh, crl

    return None, 0, 0


def find_order_block_zone(klines):
    """Order Block — the last opposite-colored candle before a strong"""
    if len(klines) < 20:
        return None, 0, 0
    opens = [float(k[1]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    closes = [float(k[4]) for k in klines]

    for i in range(len(klines) - 6, max(len(klines) - 26, 0), -1):
        window_end = min(i + 5, len(klines))
        if closes[i] < opens[i]:
            impulse_high = max(highs[i+1:window_end])
            swing_high_before = max(highs[max(0, i-15):i]) if i > 0 else 0
            impulse_pct = (impulse_high - closes[i]) / closes[i] * 100 if closes[i] > 0 else 0
            if impulse_pct >= 2.0 and swing_high_before > 0 and impulse_high > swing_high_before:
                ob_top, ob_bottom = highs[i], lows[i]
                mitigated = any(closes[j] < ob_bottom for j in range(i+1, len(closes)-1))
                if not mitigated:
                    return "BUY", ob_top, ob_bottom
        if closes[i] > opens[i]:
            impulse_low = min(lows[i+1:window_end])
            swing_low_before = min(lows[max(0, i-15):i]) if i > 0 else 0
            impulse_pct = (closes[i] - impulse_low) / closes[i] * 100 if closes[i] > 0 else 0
            if impulse_pct >= 2.0 and swing_low_before > 0 and impulse_low < swing_low_before:
                ob_top, ob_bottom = highs[i], lows[i]
                mitigated = any(closes[j] > ob_top for j in range(i+1, len(closes)-1))
                if not mitigated:
                    return "SELL", ob_top, ob_bottom
    return None, 0, 0


def find_order_block_retest(klines):
    """Turns a tracked Order Block zone into an actual trade trigger: the"""
    if len(klines) < 21:
        return None, 0, 0
    direction, ob_top, ob_bottom = find_order_block_zone(klines)
    if direction is None:
        return None, 0, 0

    o, h, l, c = float(klines[-2][1]), float(klines[-2][2]), float(klines[-2][3]), float(klines[-2][4])
    if direction == "BUY":
        if not (ob_bottom <= l <= ob_top or ob_bottom <= c <= ob_top):
            return None, 0, 0
        level = ob_top
    else:
        if not (ob_bottom <= h <= ob_top or ob_bottom <= c <= ob_top):
            return None, 0, 0
        level = ob_bottom

    if check_retest_rejection_candle(o, h, l, c, direction, level):
        return direction, ob_top, ob_bottom
    return None, 0, 0


def find_swing_points(highs, lows, left=2, right=2):
    """Simple pivot-based swing detection: index i is a swing high if its"""
    swing_highs, swing_lows = [], []
    n = len(highs)
    for i in range(left, n - right):
        window_h = highs[i-left:i+right+1]
        window_l = lows[i-left:i+right+1]
        if highs[i] == max(window_h):
            swing_highs.append((i, highs[i]))
        if lows[i] == min(window_l):
            swing_lows.append((i, lows[i]))
    return swing_highs, swing_lows


def detect_quasimodo(klines):
    """Quasimodo (QM) — matches the reference cheat sheet exactly (all 4"""
    if len(klines) < 30:
        return None, 0
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    opens = [float(k[1]) for k in klines]
    closes = [float(k[4]) for k in klines]

    swing_highs, swing_lows = find_swing_points(highs, lows, left=2, right=2)
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return None, 0

    for ll_idx, ll_price in reversed(swing_lows[-6:]):
        hh_candidates = [(i, p) for i, p in swing_highs if i < ll_idx]
        if not hh_candidates:
            continue
        hh_idx, hh_price = hh_candidates[-1]
        l_candidates = [(i, p) for i, p in swing_lows if i < hh_idx]
        if not l_candidates:
            continue
        l_idx, l_price = l_candidates[-1]
        h_candidates = [(i, p) for i, p in swing_highs if i < l_idx]
        if not h_candidates:
            continue
        h_idx, h_price = h_candidates[-1]

        if hh_price > h_price and ll_price < l_price:
            qm_level = l_price
            for j in range(ll_idx + 1, len(closes) - 1):
                if check_retest_rejection_candle(opens[j], highs[j], lows[j], closes[j], "BUY", qm_level):
                    return "BUY", qm_level

    return None, 0


def detect_bos_retest(klines, ms, price, avg_vol):
    """Synchronous complement to the existing async retest_watchlist"""
    if len(klines) < 30: return None

    closes = [float(k[4]) for k in klines]
    highs  = [float(k[2]) for k in klines]
    lows   = [float(k[3]) for k in klines]
    vols   = [float(k[5]) for k in klines]

    opens  = [float(k[1]) for k in klines]

    if ms["bias"] == "neutral": return None

    swing_high = ms["swing_high"]
    swing_low = ms["swing_low"]

    recent_vols = vols[-4:]
    avg_recent_vol = sum(recent_vols) / len(recent_vols)
    if avg_recent_vol > avg_vol * 0.9: return None

    if ms["bias"] == "bullish" and swing_high > 0:
        max_recent_high = max(highs[-15:])
        if max_recent_high > swing_high * 1.015:
            dist = abs(price - swing_high) / swing_high * 100
            candle_range = highs[-2] - lows[-2]
            lower_wick_pct = (min(opens[-2], closes[-2]) - lows[-2]) / candle_range * 100 if candle_range > 0 else 0
            is_rejecting = closes[-2] > opens[-2] or lower_wick_pct > 40
            if dist <= 0.8 and closes[-1] > swing_high and is_rejecting:
                return "BUY"

    if ms["bias"] == "bearish" and swing_low > 0:
        min_recent_low = min(lows[-15:])
        if min_recent_low < swing_low * 0.985:
            dist = abs(price - swing_low) / swing_low * 100
            candle_range = highs[-2] - lows[-2]
            upper_wick_pct = (highs[-2] - max(opens[-2], closes[-2])) / candle_range * 100 if candle_range > 0 else 0
            is_rejecting = closes[-2] < opens[-2] or upper_wick_pct > 40
            if dist <= 0.8 and closes[-1] < swing_low and is_rejecting:
                return "SELL"

    return None


def detect_early_spark(closes, highs, lows, opens, vols, price):
    """Early Spark Ignition: catches the first sign of life at a potential"""
    if len(closes) < 30: return None

    avg_vol_20 = sum(vols[-20:]) / 20 if len(vols) >= 20 else 1
    current_vol = vols[-1]

    lookback = min(len(lows), 96)
    recent_low = min(lows[-lookback:])
    recent_high = max(highs[-lookback:])

    dist_from_low_pct = (price - recent_low) / recent_low * 100 if recent_low > 0 else 99
    dist_from_high_pct = (recent_high - price) / price * 100 if price > 0 else 99

    volume_igniting = current_vol >= avg_vol_20 * 1.1

    if dist_from_low_pct <= 1.5 and volume_igniting and closes[-1] > opens[-1]:
        return "BUY"

    if dist_from_high_pct <= 1.5 and volume_igniting and closes[-1] < opens[-1]:
        return "SELL"

    return None


def detect_funding_divergence(funding_rate, price, sup, res):
    """Funding Rate Divergence Sniper: a genuinely STANDALONE predictive"""
    if funding_rate is None: return None

    near_support = abs(price - sup) / sup * 100 < 1.0 if sup > 0 else False
    near_resistance = abs(res - price) / res * 100 < 1.0 if res > 0 else False

    if funding_rate <= SQUEEZE_FUNDING_EXTREME_NEG and near_support:
        return "BUY"

    if funding_rate >= SQUEEZE_FUNDING_EXTREME_POS and near_resistance:
        return "SELL"

    return None


def detect_smart_money_absorption(closes, highs, lows, vols, price):
    """Predictive Bottom Fishing (the "BANK setup"): price has dropped"""
    if len(closes) < 40: return None

    recent_low = min(lows[-40:])
    recent_high = max(highs[-40:])
    macro_drop = (recent_high - recent_low) / recent_high * 100 if recent_high > 0 else 0

    if macro_drop < 12.0: return None

    dist_from_low = (price - recent_low) / recent_low * 100 if recent_low > 0 else 99
    if dist_from_low > 5.0: return None

    recent_ranges = [h - l for h, l in zip(highs[-5:], lows[-5:])]
    avg_range_20 = sum([highs[i] - lows[i] for i in range(-20, 0)]) / 20 if len(highs) >= 20 else 1
    is_compressed = sum(1 for r in recent_ranges if r < avg_range_20 * 0.6) >= 3

    avg_vol_20 = sum(vols[-20:]) / 20 if len(vols) >= 20 else 1
    volume_absorbing = vols[-1] > avg_vol_20 * 1.6 or vols[-2] > avg_vol_20 * 1.6

    if is_compressed and volume_absorbing:
        return "BUY"

    return None


def detect_pressure_triangle(highs, lows, closes, vols, price):
    """The "Pressure Cooker": Ascending/Descending Triangle detection."""
    if len(closes) < 30: return None

    recent_highs = highs[-30:]
    recent_lows = lows[-30:]

    max_high = max(recent_highs)
    min_low = min(recent_lows)
    if max_high <= 0 or min_low <= 0: return None

    avg_vol = sum(vols[-20:]) / 20 if len(vols) >= 20 else (vols[-1] if vols else 1)
    vol_ratio = vols[-1] / avg_vol if avg_vol > 0 else 1.0
    if vol_ratio < 1.35:
        return None

    flat_resistance = sum(1 for h in recent_highs if abs(max_high - h) / max_high < 0.005) >= 3
    rising_support = min(recent_lows[-10:]) > min(recent_lows[:10]) * 1.01
    pressed_to_ceiling = abs(max_high - price) / max_high < 0.008

    if flat_resistance and rising_support and pressed_to_ceiling:
        return "BUY"

    flat_support = sum(1 for l in recent_lows if abs(l - min_low) / min_low < 0.005) >= 3
    falling_resistance = max(recent_highs[-10:]) < max(recent_highs[:10]) * 0.99
    pressed_to_floor = abs(price - min_low) / min_low < 0.008

    if flat_support and falling_resistance and pressed_to_floor:
        return "SELL"

    return None


def detect_inside_bar_coil(closes, highs, lows, opens, vols, price, zone_low, zone_high, direction_bias):
    '''Point 2 (Inside Bar Coil): "The True Early Entry."'''
    if len(closes) < 3 or zone_low is None or zone_high is None or zone_low <= 0:
        return None, 0, 0

    mother_high, mother_low = highs[-3], lows[-3]
    inside_high, inside_low = highs[-2], lows[-2]

    is_inside_bar = inside_high < mother_high and inside_low > mother_low
    if not is_inside_bar:
        return None, 0, 0

    avg_vol_20 = sum(vols[-20:]) / 20 if len(vols) >= 20 else (vols[-1] if vols else 1)
    inside_bar_vol = vols[-2]
    low_volume = avg_vol_20 > 0 and inside_bar_vol < avg_vol_20 * 0.85

    resting_on_zone = zone_low*0.995 <= price <= zone_high*1.005

    if not low_volume or not resting_on_zone:
        return None, 0, 0

    if direction_bias == "bullish":
        return "BUY", inside_high, inside_low
    if direction_bias == "bearish":
        return "SELL", inside_high, inside_low
    return None, 0, 0


def detect_liquidity_sweep(klines, highs, lows, closes, opens, sup, res, ms):
    """Liquidity Sweep (failed breakout / stop hunt), per instruction."""
    if len(closes) < 10 or not ms["choch"]:
        return None, 0

    for i in range(2, 5):
        if i > len(closes): break
        idx = -i
        c_open, c_high, c_low, c_close = opens[idx], highs[idx], lows[idx], closes[idx]
        candle_range = c_high - c_low
        if candle_range <= 0: continue

        pierced_support = c_low < sup and sup > 0
        closed_back_above = c_close > sup
        lower_wick_pct = (min(c_open, c_close) - c_low) / candle_range * 100
        if pierced_support and closed_back_above and lower_wick_pct > 40:
            if ms["bias"] == "bullish" or ms["choch"]:
                sweep_depth_pct = abs(sup - c_low) / sup * 100 if sup > 0 else 0
                strength = min(100, 60 + sweep_depth_pct * 20 + lower_wick_pct * 0.3)
                return "BUY", strength

        pierced_resistance = c_high > res and res > 0
        closed_back_below = c_close < res
        upper_wick_pct = (c_high - max(c_open, c_close)) / candle_range * 100
        if pierced_resistance and closed_back_below and upper_wick_pct > 40:
            if ms["bias"] == "bearish" or ms["choch"]:
                sweep_depth_pct = abs(c_high - res) / res * 100 if res > 0 else 0
                strength = min(100, 60 + sweep_depth_pct * 20 + upper_wick_pct * 0.3)
                return "SELL", strength

    return None, 0


def detect_trend_continuation_coil(symbol, klines, price):
    """User's Multi-Timeframe Workflow:"""
    if len(klines) < 30: return None, 0

    t_4h = get_htf_trend(symbol, "4h")
    t_1h = get_htf_trend(symbol, "1h")

    if t_4h == 1 and t_1h == 1:
        direction = "BUY"
    elif t_4h == -1 and t_1h == -1:
        direction = "SELL"
    else:
        return None, 0

    closes = [float(k[4]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    vols = [float(k[5]) for k in klines]
    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)

    if not ema20 or not ema50: return None, 0

    if direction == "BUY" and ema20 <= ema50: return None, 0
    if direction == "SELL" and ema20 >= ema50: return None, 0

    recent_range_pct = (max(highs[-6:]) - min(lows[-6:])) / price * 100
    if recent_range_pct > 2.5: return None, 0

    dist_to_ema20 = abs(price - ema20) / ema20 * 100
    if dist_to_ema20 > 1.2: return None, 0

    avg_vol = sum(vols[-20:]) / 20 if len(vols) >= 20 else 1.0
    if vols[-1] > avg_vol * 1.5: return None, 0

    tightness_score = max(0, 100 - (recent_range_pct * 15))
    return direction, tightness_score


def detect_5m_sniper_entry(symbol, klines_15m, price):
    """The User's Exact Multi-Timeframe Workflow:"""
    if len(klines_15m) < 30: return None

    t_4h = get_htf_trend(symbol, "4h")
    t_1h = get_htf_trend(symbol, "1h")

    if t_4h == 1 and t_1h == 1: direction = "BUY"
    elif t_4h == -1 and t_1h == -1: direction = "SELL"
    else: return None

    closes_15 = [float(k[4]) for k in klines_15m]
    ema20_15 = calculate_ema(closes_15, 20)
    ema50_15 = calculate_ema(closes_15, 50)
    if not ema20_15 or not ema50_15: return None

    if direction == "BUY" and ema20_15 <= ema50_15: return None
    if direction == "SELL" and ema20_15 >= ema50_15: return None

    dist_to_ema20 = abs(price - ema20_15) / ema20_15 * 100
    if dist_to_ema20 > 2.0: return None

    try:
        k5 = get_klines(symbol, "5m", 30)
        if not k5 or len(k5) < 25: return None

        highs5 = [float(k[2]) for k in k5]
        lows5 = [float(k[3]) for k in k5]
        closes5 = [float(k[4]) for k in k5]
        vols5 = [float(k[5]) for k in k5]

        coil_high = max(highs5[-13:-1])
        coil_low = min(lows5[-13:-1])
        coil_range_pct = (coil_high - coil_low) / coil_low * 100 if coil_low > 0 else 999

        if coil_range_pct > 3.0: return None

        avg_vol_5 = sum(vols5[-25:-5]) / 20 if len(vols5) >= 25 else 1.0
        current_vol = vols5[-1]
        open_time_ms = float(k5[-1][0])
        seconds_open = (time.time() * 1000 - open_time_ms) / 1000

        if seconds_open < 30:
            projected_vol = current_vol
        else:
            projected_vol = current_vol * (300 / min(seconds_open, 300))

        if projected_vol < avg_vol_5 * 1.5 or current_vol < avg_vol_5 * 0.20: return None

        if direction == "BUY" and closes5[-1] > coil_high:
            return "BUY"
        if direction == "SELL" and closes5[-1] < coil_low:
            return "SELL"

        return None
    except Exception:
        return None


def detect_yellow_circle_sniper(symbol, live_price):
    """OUT OF THE BOX: Time-Weighted Volume Velocity + Dead Zone Pinch."""
    try:
        k5 = get_klines(symbol, "5m", 20)
        if not k5 or len(k5) < 15: return None

        live_candle = k5[-1]
        history = k5[-13:-1]

        highs = [float(k[2]) for k in history]
        lows = [float(k[3]) for k in history]
        vols = [float(k[5]) for k in history]

        dead_zone_high = max(highs)
        dead_zone_low = min(lows)

        range_pct = (dead_zone_high - dead_zone_low) / dead_zone_low * 100 if dead_zone_low > 0 else 999
        if range_pct > 2.0: return None

        avg_dead_vol = sum(vols) / len(vols) if vols else 1.0

        live_open_time = float(live_candle[0])
        live_vol = float(live_candle[5])
        live_close = float(live_candle[4])

        seconds_open = (time.time() * 1000 - live_open_time) / 1000

        if seconds_open < 30: return None

        projected_vol = live_vol * (300 / min(seconds_open, 300))

        if projected_vol < avg_dead_vol * 2.5 or live_vol < avg_dead_vol * 0.20: return None

        zone_width = dead_zone_high - dead_zone_low
        breakout_tolerance = zone_width * 0.05
        if live_close > dead_zone_high - breakout_tolerance:
            return "BUY"
        if live_close < dead_zone_low + breakout_tolerance:
            return "SELL"

        return None
    except Exception as e:
        logger.warning(f"Yellow Circle Sniper error {symbol}: {e}")
        return None


def detect_market_regime(klines):
    """Per-coin market regime classifier — the structural fix for "a Double"""
    if len(klines) < 30:
        return "RANGE_BOUND"
    closes = [float(k[4]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    adx_val = calculate_adx(klines)

    if adx_val >= 25:
        return "TRENDING"

    recent_high = max(highs[-20:])
    recent_low = min(lows[-20:])
    range_pct = (recent_high - recent_low) / recent_low * 100 if recent_low > 0 else 999

    if range_pct < 3.0 and adx_val < 18:
        return "SQUEEZE"

    candle_ranges = [(float(k[2]) - float(k[3])) / float(k[3]) * 100 if float(k[3]) > 0 else 0 for k in klines[-20:]]
    avg_candle_range = sum(candle_ranges) / len(candle_ranges) if candle_ranges else 0
    net_move_pct = abs(closes[-1] - closes[-20]) / closes[-20] * 100 if len(closes) >= 20 and closes[-20] > 0 else 0
    if avg_candle_range > 0 and (net_move_pct / (avg_candle_range * 20)) < 0.15:
        return "CHOPPY"

    return "RANGE_BOUND"


def get_cached_daily_levels(symbol):
    """Fetches the Previous Daily High (PDH), Low (PDL), and Midpoint."""
    now = get_ist_datetime()
    cached = daily_levels_cache.get(symbol)
    if cached and (now - cached["cached_at"]).total_seconds() < 3600:
        return cached["levels"]

    klines_1d = get_klines(symbol, "1d", 5)
    if not klines_1d or len(klines_1d) < 2: return None

    prev_day = klines_1d[-2]
    pdh = float(prev_day[2])
    pdl = float(prev_day[3])
    midpoint = (pdh + pdl) / 2

    levels = {"pdh": pdh, "pdl": pdl, "mid": midpoint}
    daily_levels_cache[symbol] = {"levels": levels, "cached_at": now}
    return levels


def get_cached_crt_range(symbol):
    """Fetches the 4H candles CRT reads its reference range from. Cached"""
    now = get_ist_datetime()
    cached = crt_range_cache.get(symbol)
    if cached and (now - cached["cached_at"]).total_seconds() < 3600:
        return cached["klines_4h"]

    klines_4h = get_klines(symbol, "4h", 5)
    if not klines_4h or len(klines_4h) < 4:
        return None

    crt_range_cache[symbol] = {"klines_4h": klines_4h, "cached_at": now}
    return klines_4h


def detect_daily_level_reversal(symbol, klines_15m, price):
    """Video 1 Strategy: 15m rejection exactly at the Previous Daily High"""
    if len(klines_15m) < 5: return None, 0

    levels = get_cached_daily_levels(symbol)
    if not levels: return None, 0

    pdh, pdl, mid = levels["pdh"], levels["pdl"], levels["mid"]

    if mid > 0 and abs(price - mid) / mid < 0.01:
        return None, 0

    c_open = float(klines_15m[-2][1])
    c_high = float(klines_15m[-2][2])
    c_low = float(klines_15m[-2][3])
    c_close = float(klines_15m[-2][4])
    candle_range = c_high - c_low
    if candle_range <= 0: return None, 0

    if pdl > 0 and abs(c_low - pdl) / pdl < 0.005:
        lower_wick_pct = (min(c_open, c_close) - c_low) / candle_range * 100
        if c_close > c_open and lower_wick_pct > 35.0 and abs(price - pdl) / pdl < 0.008:
            return "BUY", pdl

    if pdh > 0 and abs(c_high - pdh) / pdh < 0.005:
        upper_wick_pct = (c_high - max(c_open, c_close)) / candle_range * 100
        if c_close < c_open and upper_wick_pct > 35.0 and abs(pdh - price) / pdh < 0.008:
            return "SELL", pdh

    return None, 0


def detect_fibonacci_golden_zone(klines):
    """ChoCh + 0.618-0.786 Fibonacci Golden Zone (ICT "Optimal Trade Entry")"""
    if len(klines) < 50: return None, 0

    highs = [float(k[2]) for k in klines[-50:]]
    lows = [float(k[3]) for k in klines[-50:]]
    closes = [float(k[4]) for k in klines[-50:]]
    opens = [float(k[1]) for k in klines[-50:]]

    lowest_low = min(lows[-30:-5])
    ll_idx = lows[-30:].index(lowest_low) + (len(lows) - 30)

    if ll_idx < len(highs) - 5:
        impulse_high = max(highs[ll_idx:-2])

        if lowest_low > 0 and (impulse_high - lowest_low) / lowest_low > 0.02:
            previous_highs = highs[:ll_idx]
            if previous_highs:
                prev_swing_high = max(previous_highs[-15:])

                if impulse_high > prev_swing_high:
                    fib_618 = impulse_high - ((impulse_high - lowest_low) * 0.618)
                    fib_786 = impulse_high - ((impulse_high - lowest_low) * 0.786)

                    if fib_786 <= lows[-1] <= fib_618 * 1.005:
                        candle_range = highs[-1] - lows[-1]
                        lower_wick_pct = (min(opens[-1], closes[-1]) - lows[-1]) / candle_range * 100 if candle_range > 0 else 0

                        if closes[-1] > opens[-1] or lower_wick_pct > 40:
                            return "BUY", fib_618

    highest_high = max(highs[-30:-5])
    hh_idx = highs[-30:].index(highest_high) + (len(highs) - 30)

    if hh_idx < len(lows) - 5:
        impulse_low = min(lows[hh_idx:-2])

        if impulse_low > 0 and (highest_high - impulse_low) / impulse_low > 0.02:
            previous_lows = lows[:hh_idx]
            if previous_lows:
                prev_swing_low = min(previous_lows[-15:])

                if impulse_low < prev_swing_low:
                    fib_618 = impulse_low + ((highest_high - impulse_low) * 0.618)
                    fib_786 = impulse_low + ((highest_high - impulse_low) * 0.786)

                    if fib_618 * 0.995 <= highs[-1] <= fib_786:
                        candle_range = highs[-1] - lows[-1]
                        upper_wick_pct = (highs[-1] - max(opens[-1], closes[-1])) / candle_range * 100 if candle_range > 0 else 0

                        if closes[-1] < opens[-1] or upper_wick_pct > 40:
                            return "SELL", fib_618

    return None, 0


def detect_patterns(symbol, klines, price, btc_trend):
    """Upgraded pattern detection with:"""
    if len(klines) < 50: return []
    closes = [float(k[4]) for k in klines]
    opens  = [float(k[1]) for k in klines]
    highs  = [float(k[2]) for k in klines]
    lows   = [float(k[3]) for k in klines]
    vols   = [float(k[5]) for k in klines]
    avg_vol = sum(vols[-20:]) / 20
    rsi    = calculate_rsi(closes)
    ema20  = calculate_ema(closes, 20)
    ema50  = calculate_ema(closes, 50)
    adx    = calculate_adx(klines)
    if ((max(highs[-20:]) - min(lows[-20:])) / price) * 100 < 1.5: return []
    ms = detect_market_structure(klines)
    ms_bias = ms["bias"]
    alt_bull_ok  = btc_trend == 1 or ms_bias == "bullish"
    alt_bear_ok  = btc_trend == -1 or ms_bias == "bearish"
    p = []
    sup = ms["swing_low"] if ms["swing_low"] > 0 else min(lows[-30:-1])
    res = ms["swing_high"] if ms["swing_high"] > 0 else max(highs[-30:-1])

    TIER1_BASE = 88.0
    TIER2_BASE = 75.0

    vcp_dir, vcp_tightness = detect_volatility_contraction(closes, highs, lows, vols, price)
    if vcp_dir == "BUY" and alt_bull_ok:
        p.append(("Volatility Contraction (Coiling)", TIER1_BASE, "BUY"))
    elif vcp_dir == "SELL" and alt_bear_ok:
        p.append(("Volatility Contraction (Coiling)", TIER1_BASE, "SELL"))

    pbc_sup, pbc_res = get_recent_swing_levels(klines, lookback=20)
    if pbc_sup <= 0: pbc_sup = sup
    if pbc_res <= 0: pbc_res = res
    pbc_dir, pbc_tightness = detect_pre_breakout_compression(closes, highs, lows, vols, price, pbc_sup, pbc_res, ms_bias)
    if pbc_dir == "BUY" and alt_bull_ok:
        p.append(("Pre-Breakout Compression", TIER1_BASE, "BUY"))
    elif pbc_dir == "SELL" and alt_bear_ok:
        p.append(("Pre-Breakout Compression", TIER1_BASE, "SELL"))

    ib_dir, ib_high, ib_low = detect_inside_bar_coil(closes, highs, lows, opens, vols, price, sup, res, ms_bias)
    if ib_dir == "BUY" and alt_bull_ok and price > ib_high:
        p.append(("Inside Bar Coil", TIER1_BASE, "BUY"))
    elif ib_dir == "SELL" and alt_bear_ok and price < ib_low:
        p.append(("Inside Bar Coil", TIER1_BASE, "SELL"))

    spark_dir = detect_early_spark(closes, highs, lows, opens, vols, price)
    if spark_dir == "BUY" and alt_bull_ok:
        p.append(("Early Spark Ignition", TIER1_BASE, "BUY"))
    elif spark_dir == "SELL" and alt_bear_ok:
        p.append(("Early Spark Ignition", TIER1_BASE, "SELL"))

    sma_dir = detect_smart_money_absorption(closes, highs, lows, vols, price)
    if sma_dir == "BUY" and alt_bull_ok:
        p.append(("Smart Money Absorption", TIER1_BASE, "BUY"))

    triangle_dir = detect_pressure_triangle(highs, lows, closes, vols, price)
    if triangle_dir == "BUY" and alt_bull_ok:
        p.append(("Pressure Cooker Triangle", TIER1_BASE, "BUY"))
    elif triangle_dir == "SELL" and alt_bear_ok:
        p.append(("Pressure Cooker Triangle", TIER1_BASE, "SELL"))

    tcc_dir, tcc_score = detect_trend_continuation_coil(symbol, klines, price)
    if tcc_dir == "BUY" and alt_bull_ok:
        p.append(("Trend Continuation Coil", TIER1_BASE + 2.0, "BUY"))
    elif tcc_dir == "SELL" and alt_bear_ok:
        p.append(("Trend Continuation Coil", TIER1_BASE + 2.0, "SELL"))

    if detect_bull_flag(closes, highs, lows, vols, avg_vol) and alt_bull_ok:
        p.append(("Bull Flag Formation", TIER1_BASE, "BUY"))

    if detect_bear_flag(closes, highs, lows, vols, avg_vol) and alt_bear_ok:
        p.append(("Bear Flag Formation", TIER1_BASE, "SELL"))

    sniper_dir = detect_5m_sniper_entry(symbol, klines, price)
    if sniper_dir == "BUY" and alt_bull_ok:
        p.append(("5m Multi-TF Sniper", TIER1_BASE + 2.0, "BUY"))
    elif sniper_dir == "SELL" and alt_bear_ok:
        p.append(("5m Multi-TF Sniper", TIER1_BASE + 2.0, "SELL"))

    yc_dir = detect_yellow_circle_sniper(symbol, price)
    if yc_dir in ("BUY", "SELL"):
        _yc_cvd = detect_cvd_delta_3m(symbol)
        _yc_score = 92.0 + (2.0 if _yc_cvd == yc_dir else 0.0)
        if yc_dir == "BUY" and alt_bull_ok:
            p.append(("Yellow Circle Sniper", _yc_score, "BUY"))
        elif yc_dir == "SELL" and alt_bear_ok:
            p.append(("Yellow Circle Sniper", _yc_score, "SELL"))

    if adx < ADX_MIN_TREND:
        return p


    if opens[-2] > closes[-2] and opens[-1] < closes[-2] and closes[-1] > opens[-2]:
        body_ratio = (closes[-1] - opens[-1]) / (opens[-2] - closes[-2]) if (opens[-2] - closes[-2]) > 0 else 0
        if body_ratio > 1.2 and alt_bull_ok:
            p.append(("Bullish Engulfing", TIER2_BASE, "BUY"))

    elif opens[-2] < closes[-2] and opens[-1] > closes[-2] and closes[-1] < opens[-2]:
        body_ratio = (opens[-1] - closes[-1]) / (closes[-2] - opens[-2]) if (closes[-2] - opens[-2]) > 0 else 0
        if body_ratio > 1.2 and alt_bear_ok:
            p.append(("Bearish Engulfing", TIER2_BASE, "SELL"))


    if rsi < 28 and alt_bull_ok:   p.append(("RSI Reversal", TIER2_BASE, "BUY"))
    elif rsi > 72 and alt_bear_ok: p.append(("RSI Reversal", TIER2_BASE, "SELL"))



    daily_dir, daily_level = detect_daily_level_reversal(symbol, klines, price)
    if daily_dir == "BUY" and alt_bull_ok:
        p.append(("PDL Reversal Sweep", TIER1_BASE + 2.0, "BUY"))
    elif daily_dir == "SELL" and alt_bear_ok:
        p.append(("PDH Reversal Sweep", TIER1_BASE + 2.0, "SELL"))

    _db_fired, _db_level = detect_double_bottom_pro(highs, lows, closes, vols, price, avg_vol)
    if _db_fired and alt_bull_ok:
        p.append(("Double Bottom", TIER1_BASE, "BUY"))

    _dt_fired, _dt_level = detect_double_top_pro(highs, lows, closes, vols, price, avg_vol)
    if _dt_fired and alt_bear_ok:
        p.append(("Double Top", TIER1_BASE, "SELL"))


    if ms["bos"] and not ms["choch"]:
        if ms_bias == "bullish" and alt_bull_ok:
            p.append(("BOS Breakout", TIER1_BASE, "BUY"))
        elif ms_bias == "bearish" and alt_bear_ok:
            p.append(("BOS Breakout", TIER1_BASE, "SELL"))

    bos_retest_dir = detect_bos_retest(klines, ms, price, avg_vol)
    if bos_retest_dir == "BUY" and alt_bull_ok:
        p.append(("BOS Retest (Sniper Entry)", min(TIER1_BASE + 2.0, 99), "BUY"))
    elif bos_retest_dir == "SELL" and alt_bear_ok:
        p.append(("BOS Retest (Sniper Entry)", min(TIER1_BASE + 2.0, 99), "SELL"))

    if ms["choch"]:
        if ms_bias == "bearish" and closes[-1] > ms["swing_high"] and alt_bull_ok:
            p.append(("Change of Character (ChoCh)", TIER1_BASE, "BUY"))
        elif ms_bias == "bullish" and closes[-1] < ms["swing_low"] and alt_bear_ok:
            p.append(("Change of Character (ChoCh)", TIER1_BASE, "SELL"))

    fib_dir, fib_level = detect_fibonacci_golden_zone(klines)
    if fib_dir == "BUY" and alt_bull_ok:
        p.append(("ChoCh + Fib 0.618 Golden Zone", TIER1_BASE + 2.0, "BUY"))
    elif fib_dir == "SELL" and alt_bear_ok:
        p.append(("ChoCh + Fib 0.618 Golden Zone", TIER1_BASE + 2.0, "SELL"))

    sweep_dir, sweep_strength = detect_liquidity_sweep(klines, highs, lows, closes, opens, sup, res, ms)
    if sweep_dir == "BUY" and alt_bull_ok:
        p.append(("Liquidity Sweep", min(TIER1_BASE + 1.0, 99), "BUY"))
    elif sweep_dir == "SELL" and alt_bear_ok:
        p.append(("Liquidity Sweep", min(TIER1_BASE + 1.0, 99), "SELL"))

    klines_4h_crt = get_cached_crt_range(symbol)
    if klines_4h_crt:
        crt_dir, crt_crh, crt_crl = detect_candle_range_theory(klines_4h_crt, klines)
        if crt_dir == "BUY" and alt_bull_ok:
            p.append(("Candle Range Theory", TIER1_BASE, "BUY"))
        elif crt_dir == "SELL" and alt_bear_ok:
            p.append(("Candle Range Theory", TIER1_BASE, "SELL"))

    ifvg_dir, ifvg_top, ifvg_bottom = detect_inverse_fvg(klines, price)
    if ifvg_dir == "BUY" and alt_bull_ok:
        p.append(("Inverse Fair Value Gap (IFVG)", TIER1_BASE, "BUY"))
    elif ifvg_dir == "SELL" and alt_bear_ok:
        p.append(("Inverse Fair Value Gap (IFVG)", TIER1_BASE, "SELL"))

    ob_dir, ob_top, ob_bottom = find_order_block_retest(klines)
    if ob_dir == "BUY" and alt_bull_ok:
        p.append(("Order Block", TIER1_BASE, "BUY"))
    elif ob_dir == "SELL" and alt_bear_ok:
        p.append(("Order Block", TIER1_BASE, "SELL"))

    qm_dir, qm_level = detect_quasimodo(klines)
    if qm_dir == "BUY" and alt_bull_ok:
        p.append(("Quasimodo", TIER1_BASE, "BUY"))

    return p

def is_in_zone(price,direction,zones):
    key="demand" if direction=="BUY" else "supply"
    for zone in zones.get(key,[])[-5:]:
        if zone["low"]*0.995<=price<=zone["high"]*1.005:
            return True,f"{format_price(zone['low'])}-{format_price(zone['high'])}"
    return False,""

def get_htf_zones(symbol):
    """Point 2 (HTF Zones): A professional top-down approach establishes true"""
    now = get_ist_datetime()
    cached = htf_zones_cache.get(symbol)
    if cached and (now - cached["cached_at"]).total_seconds() < 900:
        return cached["zones"]

    zones_4h = {"demand": [], "supply": []}
    zones_1h = {"demand": [], "supply": []}
    try:
        klines_4h = get_klines(symbol, "4h", 100)
        if klines_4h and len(klines_4h) >= 30:
            zones_4h = detect_supply_demand_zones(klines_4h)
    except Exception as e:
        logger.warning(f"get_htf_zones 4h {symbol}: {e}")
    try:
        klines_1h = get_klines(symbol, "1h", 100)
        if klines_1h and len(klines_1h) >= 30:
            zones_1h = detect_supply_demand_zones(klines_1h)
    except Exception as e:
        logger.warning(f"get_htf_zones 1h {symbol}: {e}")

    merged = {
        "demand": zones_4h["demand"] + zones_1h["demand"],
        "supply": zones_4h["supply"] + zones_1h["supply"],
    }
    htf_zones_cache[symbol] = {"zones": merged, "cached_at": now}
    return merged

def get_structural_tp(entry, direction, zones, min_tp_dist):
    """Point 2: Structural Take Profit — targets the nearest mapped"""
    key = "supply" if direction == "BUY" else "demand"
    candidates = zones.get(key, [])
    if not candidates:
        return None

    qualifying = []
    for z in candidates:
        if direction == "BUY":
            zone_price = z["low"]
            if zone_price <= entry: continue
            dist = zone_price - entry
        else:
            zone_price = z["high"]
            if zone_price >= entry: continue
            dist = entry - zone_price
        if dist >= min_tp_dist:
            qualifying.append((dist, zone_price))

    if not qualifying:
        return None
    qualifying.sort(key=lambda x: x[0])
    return qualifying[0][1]

def detect_market_condition(btc_price,btc_klines):
    try:
        closes=[float(k[4]) for k in btc_klines]
        e20=calculate_ema(closes,20); e50=calculate_ema(closes,50)
        h20=max(closes[-20:]); l20=min(closes[-20:])
        rng=((h20-l20)/l20)*100 if l20>0 else 0
        if e20 and e50:
            if e20>e50*1.02 and btc_price>e20:   return "bull"
            elif e20<e50*0.98 and btc_price<e20: return "bear"
        return "sideways" if rng<5.0 else ("bull" if btc_price>(e50 or btc_price) else "bear")
    except Exception: return "sideways"

def is_good_trading_session(coin=None):
    """Point 3: PREMIUM_COINS (BTC, ETH, BNB, SOL, PAXG, XAU, XAG) get VIP"""
    if coin in PREMIUM_COINS:
        return True
    hour=datetime.now(IST).hour
    if DEAD_HOUR_START<=hour<DEAD_HOUR_END:
        logger.info(f"Dead session {hour}:xx IST"); return False
    is_macro, macro_note = is_macro_event_window()
    if is_macro:
        logger.info(f"Paused - {macro_note}"); return False
    return True

def get_smart_leverage(symbol, atr_pct, score, grade="Grade B"):
    """Leverage tiers based on BOTH coin tier AND signal grade:"""
    g = grade[0] if isinstance(grade, tuple) else str(grade)
    is_aplus = "A+" in g
    is_a     = "A 🍀" in g or (not is_aplus and "A" in g)

    base = symbol.replace("USDT","")
    if base in LEV_TIER_3:
        lev = 5 if is_aplus else 4 if is_a else 3
    elif base in LEV_TIER_1:
        lev = 15 if is_aplus else 10 if is_a else 7
    elif base in LEV_TIER_2:
        lev = 12 if is_aplus else 8 if is_a else 5
    else:
        lev = 10 if is_aplus else 7 if is_a else 5

    if atr_pct >= 6.0:   lev = min(lev, 3)
    elif atr_pct >= 4.0: lev = min(lev, 5)
    elif atr_pct >= 2.5: lev = min(lev, 8)

    return max(lev, 1)

def get_signal_grade(score,vol_ratio,oi_rising,tf_score,vol_ok,rsi_ok,funding_ok,st_ok,vwap_ok,zone_ok,adx_val,btc_aligned=False,ms_bias=None,bos=False,is_sweep=False,closes=None,atr_pct=None,symbol=None,regime=None,primary_pattern=None):
    """Unified grading fix: the letter grade is now decided PURELY by the"""
    breakdown=[]
    pts=0
    if score>=98:    pts+=3; breakdown.append(("🎯 Score ≥98",      3))
    elif score>=96:  pts+=2; breakdown.append(("🎯 Score ≥96",      2))
    elif score>=92:  pts+=2; breakdown.append(("🎯 Score ≥92",      2))
    elif score>=85:  pts+=1; breakdown.append(("🎯 Score ≥85",      1))
    else:                    breakdown.append(("🎯 Score",           0))
    _is_coiling_for_vol = primary_pattern in ("Inside Bar Coil","Pre-Breakout Compression","Volatility Contraction (Coiling)","Early Spark Ignition","Smart Money Absorption","Funding Divergence Sniper","Liquidity Sweep","Trend Continuation Coil","Bull Flag Formation","Bear Flag Formation") if primary_pattern else False
    if _is_coiling_for_vol:
        if vol_ratio<=0.6:   pts+=2; breakdown.append((f"📊 Volume {vol_ratio:.1f}x (dying — coil intact)", 2))
        elif vol_ratio<=0.9: pts+=1; breakdown.append((f"📊 Volume {vol_ratio:.1f}x (quiet)", 1))
        else:                        breakdown.append((f"📊 Volume {vol_ratio:.1f}x (not yet dying)", 0))
    else:
        if vol_ratio>=1.5:   pts+=2; breakdown.append((f"📊 Volume {vol_ratio:.1f}x (strong)",   2))
        elif vol_ratio>=1.2: pts+=1; breakdown.append((f"📊 Volume {vol_ratio:.1f}x (moderate)",  1))
        else:                        breakdown.append((f"📊 Volume {vol_ratio:.1f}x",              0))
    if tf_score==3:  pts+=2; breakdown.append(("📡 4h+1h Aligned",  2))
    elif tf_score==2:pts+=1; breakdown.append(("📡 4h Aligned",     1))
    else:                    breakdown.append(("📡 TF Alignment",    0))
    if vol_ok:       pts+=1; breakdown.append(("📊 Volume Confirm",  1))
    else:                    breakdown.append(("📊 Volume",          0))
    if rsi_ok:       pts+=1; breakdown.append(("📈 RSI Valid",       1))
    else:                    breakdown.append(("📈 RSI",             0))
    if funding_ok:   pts+=1; breakdown.append(("💸 Funding OK",      1))
    else:                    breakdown.append(("💸 Funding",         0))
    if vwap_ok:      pts+=1; breakdown.append(("💧 VWAP Confirm",    1))
    else:                    breakdown.append(("💧 VWAP",            0))
    if zone_ok:      pts+=2; breakdown.append(("📍 S/D Zone Hit",    2))
    else:                    breakdown.append(("📍 S/D Zone",        0))
    if btc_aligned:  pts+=2; breakdown.append(("👑 BTC Aligned",     2))
    else:            breakdown.append(("👑 BTC Aligned",     0))
    if is_golden_hour(): pts+=1; breakdown.append(("⏰ Golden Hour",  1))
    else:                        breakdown.append(("⏰ Golden Hour",  0))
    if ms_bias in ("bullish","bearish"):
        pts+=1; breakdown.append(("🏗️ Market Structure", 1))
    else:            breakdown.append(("🏗️ Structure",        0))
    if is_sweep:     pts+=2; breakdown.append(("🌊 Liquidity Sweep",  2))
    else:            breakdown.append(("🌊 Liquidity Sweep",  0))

    oi_change_pct = None
    if symbol:
        oi_change_pct = get_oi_change_pct(symbol)
    if oi_change_pct is not None and oi_change_pct >= 2.5:
        pts+=2; breakdown.append((f"📈 OI Accelerating (+{oi_change_pct:.1f}%)", 2))
    elif oi_rising:
        pts+=1; breakdown.append(("📈 OI Rising", 1))
    else:
        breakdown.append(("📈 OI", 0))

    if atr_pct is not None and atr_pct > 0:
        if atr_pct < 0.8:    pts+=2; breakdown.append((f"🎯 Tight Risk ({atr_pct:.2f}% ATR)", 2))
        elif atr_pct < 1.5:  pts+=1; breakdown.append((f"🎯 Moderate Risk ({atr_pct:.2f}% ATR)", 1))
        else:                        breakdown.append((f"🎯 Risk ({atr_pct:.2f}% ATR)", 0))

    regime_label = None
    if regime and primary_pattern:
        _is_coiling_pattern = primary_pattern in ("Inside Bar Coil","Pre-Breakout Compression","Volatility Contraction (Coiling)","Early Spark Ignition","Smart Money Absorption","Funding Divergence Sniper","Liquidity Sweep","Trend Continuation Coil","Bull Flag Formation","Bear Flag Formation")
        if regime == "TRENDING":
            if not _is_coiling_pattern:
                pts += 1; regime_label = ("🌊 Regime: Trending (breakout favored)", 1)
            else:
                regime_label = ("🌊 Regime: Trending", 0)
        elif regime == "SQUEEZE":
            if _is_coiling_pattern:
                pts += 2; regime_label = ("🌊 Regime: Squeeze (coil favored, +2)", 2)
            else:
                regime_label = ("🌊 Regime: Squeeze", 0)
        elif regime in ("RANGE_BOUND", "CHOPPY"):
            if not _is_coiling_pattern:
                pts = -99
                regime_label = (f"🌊 Regime: {regime.title()} (breakout VETOED — no real trend to confirm)", -99)
            else:
                regime_label = (f"🌊 Regime: {regime.title()}", 0)
        if regime_label:
            breakdown.append(regime_label)

    thresholds_max = 23
    if pts >= 20:   grade = "Grade A+ 🍀"
    elif pts >= 15: grade = "Grade A 🍀"
    elif pts >= 9:  grade = "Grade B"
    else:           grade = "Grade C"
    return grade, pts, breakdown

def get_fixed_fractional_size(risk_per_trade_pct, entry_price, sl_price, leverage):
    """The Law of Fixed Fractional Risk. Replaces the old flat grade-based"""
    sl_distance_pct = abs(entry_price - sl_price) / entry_price

    if sl_distance_pct == 0: return 0.0

    position_size_pct = (risk_per_trade_pct / 100) / sl_distance_pct

    margin_pct = (position_size_pct / leverage) * 100

    if margin_pct > 25.0:
        achieved_risk_pct = 25.0 * leverage * sl_distance_pct
        logger.info(f"Position size capped at 25% margin — actual risk "
                     f"{achieved_risk_pct:.2f}% is below the {risk_per_trade_pct:.1f}% target "
                     f"(SL only {sl_distance_pct*100:.2f}% away)")
    return min(margin_pct, 25.0)


RISK_PCT_BY_GRADE = {"A+": 2.0, "A": 1.5, "B": 1.0, "default": 0.5}

def is_volume_confirmed(klines):
    vols=[float(k[5]) for k in klines]
    return len(vols)>=20 and vols[-1]>sum(vols[-20:])/20*0.85

def is_rsi_valid(closes,direction):
    rsi=calculate_rsi(closes)
    return not (direction=="BUY" and rsi>72) and not (direction=="SELL" and rsi<28)

def is_volatility_normal(klines):
    an=calculate_atr(klines,14); as_=calculate_atr(klines,50)
    return as_==0 or (an/as_)<=ATR_VOLATILITY_RATIO

def is_pattern_blacklisted(name):
    s=pattern_stats.get(name)
    if not s or s["signals"]<10: return False
    return (s["wins"]/s["signals"])*100<40

def is_pattern_suspended(name):
    d=consecutive_loss_patterns.get(name,{})
    if d.get("consecutive_losses",0)>=CONSEC_LOSS_SUSPEND:
        su=d.get("suspended_until")
        if su:
            try:
                if datetime.now(IST)<datetime.fromisoformat(su): return True
                consecutive_loss_patterns[name]["consecutive_losses"]=0
                consecutive_loss_patterns[name]["suspended_until"]=None
            except Exception: pass
    return False

def too_many_correlated_active():
    return sum(1 for c in active_trades if c in BTC_CORRELATED)>=2

def too_many_sector_active(coin):
    """Point 1: Law of Portfolio Heat — sector position limit."""
    sector = COIN_SECTOR.get(coin)
    if not sector:
        return False
    return sum(1 for c in active_trades if COIN_SECTOR.get(c) == sector) >= 1

funding_cache = {}

def get_funding_rate(symbol):
    """Bypass added for PAXG/XAU/XAG: these trade as Binance "TradFi Perpetual"""
    now = get_ist_datetime()
    cached = funding_cache.get(symbol)
    if cached and (now - cached["cached_at"]).total_seconds() < 900:
        return cached["rate"]

    if "PAXG" in symbol or symbol in FUTURES_ONLY_SYMBOLS: return None
    try:
        res=requests.get(BINANCE_FUNDING_URL,params={"symbol":symbol,"limit":1},timeout=10)
        rate=float(res.json()[0]["fundingRate"]) if res.status_code==200 and res.json() else None
        funding_cache[symbol]={"rate":rate,"cached_at":now}
        return rate
    except Exception as e:
        logger.warning(f"funding {symbol}: {e}"); return None

def is_funding_favorable(symbol,direction):
    rate=get_funding_rate(symbol)
    if rate is None: return True
    if direction=="BUY"  and rate>0.002:  return False
    if direction=="SELL" and rate<-0.002: return False
    return True

def get_oi_trend(symbol):
    """Bypass for PAXG/XAU/XAG — see get_funding_rate's docstring for the"""
    if "PAXG" in symbol or symbol in FUTURES_ONLY_SYMBOLS: return None
    try:
        res=requests.get(BINANCE_OI_URL,params={"symbol":symbol,"period":"15m","limit":5},timeout=10)
        if res.status_code==200 and len(res.json())>=2:
            d=res.json()
            return float(d[-1]["sumOpenInterest"])>float(d[-2]["sumOpenInterest"])
        return None
    except Exception as e:
        logger.warning(f"OI {symbol}: {e}"); return None

def get_oi_change_pct(symbol):
    """Point 3: Squeeze detection needs OI MAGNITUDE ("is it skyrocketing"),"""
    if "PAXG" in symbol or symbol in FUTURES_ONLY_SYMBOLS: return None
    try:
        res=requests.get(BINANCE_OI_URL,params={"symbol":symbol,"period":"15m","limit":5},timeout=10)
        if res.status_code==200 and len(res.json())>=2:
            d=res.json()
            prev=float(d[-2]["sumOpenInterest"]); curr=float(d[-1]["sumOpenInterest"])
            if prev<=0: return None
            return (curr-prev)/prev*100
        return None
    except Exception as e:
        logger.warning(f"OI change {symbol}: {e}"); return None

def detect_aggressive_order_flow(klines):
    """Real directional order flow via Taker Buy/Sell Delta — REPLACES the"""
    if len(klines) < 5: return None
    try:
        recent = klines[-5:]
        taker_buy_vol = sum(float(k[9]) for k in recent)
        total_vol = sum(float(k[5]) for k in recent)
        taker_sell_vol = total_vol - taker_buy_vol

        if total_vol <= 0: return None

        if taker_buy_vol / total_vol >= 0.65:
            return "BUY"
        if taker_sell_vol / total_vol >= 0.65:
            return "SELL"
        return None
    except Exception as e:
        logger.warning(f"order flow: {e}")
        return None

def detect_cvd_delta_3m(symbol):
    """Near-real-time 3m Cumulative Volume Delta — genuinely distinct from"""
    try:
        k3 = get_klines(symbol, "3m", 10)
        if not k3 or len(k3) < 3:
            return None
        recent = k3[-3:]
        taker_buy_vol = sum(float(k[9]) for k in recent)
        total_vol = sum(float(k[5]) for k in recent)
        if total_vol <= 0:
            return None
        taker_sell_vol = total_vol - taker_buy_vol
        if taker_buy_vol / total_vol >= 0.70:
            return "BUY"
        if taker_sell_vol / total_vol >= 0.70:
            return "SELL"
        return None
    except Exception as e:
        logger.warning(f"3m CVD delta {symbol}: {e}")
        return None


def log_macro_coil(coin, symbol, pattern, direction, quality, level):
    """Logs a detected macro (1H/4H) coil into the lifecycle tracker"""
    global macro_coils
    if coin in macro_coils:
        return
    if len(macro_coils) >= MAX_MACRO_COILS:
        logger.info(f"{coin} macro coil detected but watchlist is full ({MAX_MACRO_COILS}) — skipping to avoid unbounded tracking/notification volume.")
        return

    klines_4h = get_klines(symbol, "4h", 25)
    klines_1h = get_klines(symbol, "1h", 30)
    ai_approved, ai_reasoning = ai_analyze_macro_coil(coin, direction, klines_4h, klines_1h, pattern, level)
    if not ai_approved:
        logger.info(f"{coin} macro coil REJECTED by AI: {ai_reasoning}")
        return

    macro_coils[coin] = {
        "symbol": symbol,
        "pattern": pattern,
        "direction": direction,
        "quality": quality,
        "level": level,
        "ai_reasoning": ai_reasoning,
        "detected_at": get_ist_datetime(),
        "last_update_sent": get_ist_datetime(),
    }
    logger.info(f"{coin} MACRO COIL DETECTED: {pattern} ({direction}), quality={quality:.1f} — added to macro_coils for ongoing monitoring.")
    # REAL FIX (this round, confirmed missing — not assumed): a coin
    # being added to the radar previously sent NO notification at all,
    # only a server-side log line the user has no way to see. The user
    # was only ever hearing about a coin via the 4-hourly "still
    # coiling" ping or an eventual invalidation — meaning the actual
    # real, leveraged breakout signal (if it fires) could be missed
    # entirely if it happens between those. This message closes that
    # gap: sent once, the moment a coin actually enters the watchlist.
    send_telegram(
        f"🛰️ <b>NEW RADAR WATCH</b>\n\n"
        f"🪙 <b>{coin}</b>  {'🟢' if direction=='BUY' else '🔴'} {direction}\n"
        f"📌 {pattern}\n"
        f"📍 Level: {format_price(level)}  |  Now: {format_price(get_price(symbol) or level)}\n\n"
        f"<i>Now tracking for a volume breakout. You'll get an update within 4 hours,\n"
        f"an invalidation if it breaks the level, or a full signal if it triggers.</i>\n"
        f"🕐 {get_ist_time()}"
    )


def ai_analyze_macro_coil(coin, direction, klines_4h, klines_1h, pattern, level):
    """Upstream AI Evaluator for the Pre-Breakout Macro Engine — called"""
    if not AI_REVIEW_ENABLED: return True, "AI Review Disabled - Auto-pass"
    if not ANTHROPIC_API_KEY: return True, "AI Disabled - Auto-pass"

    try:
        recent_4h = klines_4h[-6:]
        desc_4h = []
        for i, k in enumerate(recent_4h):
            o, h, l, c, v = float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])
            rng = h - l if h > l else 0.0001
            ctype = "BULL" if c > o else "BEAR"
            desc_4h.append(f"4H_C{i+1}: {ctype} | Range: {rng:.4f} | Vol: {v:.1f}")

        prompt = (
            f"You are a Senior Macro Swing Trader evaluating a developing setup on {coin} ({direction}).\n"
            f"The scanner detected a {pattern} forming around {format_price(level)}.\n\n"
            f"Recent 4H Price Action (Oldest to Newest):\n"
            + "\n".join(desc_4h) + "\n\n"
            f"Your job is to evaluate POTENTIAL ENERGY. Do not look for a breakout that already happened. "
            f"Look for extreme compression, tight ranges, and volume drying up near the key level. "
            f"If the setup is already heavily expanded and loud, it is LATE. If it is quietly resting "
            f"and squeezing, it is EARLY.\n\n"
            f"Respond EXACTLY in this format:\n"
            f"VERDICT: [CLEAN/MESSY]\n"
            f"STAGE: [EARLY/MID/LATE]\n"
            f"REASONING: [1 sentence blunt desk-trader analysis.]"
        )

        res = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key":ANTHROPIC_API_KEY, "anthropic-version":"2023-06-01", "content-type":"application/json"},
            json={"model":"claude-haiku-4-5-20251001", "max_tokens": 150, "messages":[{"role":"user", "content":prompt}]},
            timeout=15)

        if res.status_code != 200: return True, "API Error - Auto-pass"
        text = res.json()["content"][0]["text"].strip()

        is_early = "STAGE: EARLY" in text or "STAGE: MID" in text
        is_clean = "VERDICT: CLEAN" in text
        reasoning = text.split("REASONING:")[-1].strip() if "REASONING:" in text else "Looks solid."

        return (is_early and is_clean), reasoning

    except Exception as e:
        logger.warning(f"Macro AI Error {coin}: {e}")
        return True, "Error - Auto-pass"


def detect_macro_pennant_4h(klines_4h):
    """4H Bull/Bear Pennant Detector. Requires a strong prior 4H impulse"""
    if len(klines_4h) < 20:
        return None, 0, None, 0

    closes = [float(k[4]) for k in klines_4h]
    highs = [float(k[2]) for k in klines_4h]
    lows = [float(k[3]) for k in klines_4h]
    vols = [float(k[5]) for k in klines_4h]

    impulse = closes[-20:-8]
    if not impulse or impulse[0] <= 0: return None, 0, None, 0
    impulse_chg = (impulse[-1] - impulse[0]) / impulse[0] * 100

    consol_highs = highs[-8:]
    consol_lows = lows[-8:]
    range_start = max(highs[-12:-8]) - min(lows[-12:-8])
    range_end = max(consol_highs) - min(consol_lows)

    vol_impulse = sum(vols[-20:-8]) / 12
    vol_consol = sum(vols[-8:]) / 8

    is_contracting = range_end < range_start * 0.70 and vol_consol < vol_impulse * 0.75

    if is_contracting:
        tightness = max(0, 100 - (range_end / closes[-1] * 100) * 12) if closes[-1] > 0 else 0
        if impulse_chg > 4.0:
            return "BUY", tightness, "Bull Pennant (4H)", max(consol_highs)
        elif impulse_chg < -4.0:
            return "SELL", tightness, "Bear Pennant (4H)", min(consol_lows)

    return None, 0, None, 0


def detect_macro_rectangle_box(klines_1h):
    """1H/4H Rectangle Box Compression (Range Channel). Detects price"""
    if len(klines_1h) < 24:
        return None, 0, None, 0

    recent = klines_1h[-20:]
    highs = [float(k[2]) for k in recent]
    lows = [float(k[3]) for k in recent]
    closes = [float(k[4]) for k in recent]
    vols = [float(k[5]) for k in recent]

    box_high = max(highs)
    box_low = min(lows)
    if box_low <= 0: return None, 0, None, 0

    box_width_pct = (box_high - box_low) / box_low * 100
    if box_width_pct > 2.2:
        return None, 0, None, 0

    avg_vol_first_half = sum(vols[:10]) / 10
    avg_vol_second_half = sum(vols[10:]) / 10
    if avg_vol_second_half >= avg_vol_first_half * 0.85:
        return None, 0, None, 0

    pos_in_box = (closes[-1] - box_low) / (box_high - box_low) if box_high > box_low else 0.5
    tightness = max(0, 100 - box_width_pct * 25)

    if pos_in_box >= 0.5:
        return "BUY", tightness, "Rectangle Box Compression (1H)", box_high
    else:
        return "SELL", tightness, "Rectangle Box Compression (1H)", box_low


def detect_macro_ema_reclaim(symbol, klines_4h):
    """4H Dynamic EMA20/EMA50 Reclaim. Fires when price pulls back into a"""
    if len(klines_4h) < 30:
        return None, 0, None, 0

    closes = [float(k[4]) for k in klines_4h]
    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)
    if not ema20 or not ema50: return None, 0, None, 0

    price = closes[-1]
    prev_price = closes[-2]
    zones = get_htf_zones(symbol)

    in_demand, _ = is_in_zone(price, "BUY", zones)
    if prev_price <= ema20 and price > ema20 and ema20 > ema50 and in_demand:
        return "BUY", 85.0, "4H EMA20 Dynamic Reclaim", ema20

    in_supply, _ = is_in_zone(price, "SELL", zones)
    if prev_price >= ema20 and price < ema20 and ema20 < ema50 and in_supply:
        return "SELL", 85.0, "4H EMA20 Dynamic Reclaim", ema20

    return None, 0, None, 0


def get_macro_coil_grade(symbol, direction, klines_4h, live_price, sl_price, tp_price):
    """Dedicated Macro Scorecard for 1H/4H/1D Swing Setups — evaluates"""
    pts = 0
    breakdown = []

    if klines_4h and len(klines_4h) >= 20:
        vols = [float(k[5]) for k in klines_4h]
        avg_vol = sum(vols[-20:-1]) / 19 if len(vols) >= 20 else 1.0
        vol_ratio = vols[-1] / avg_vol if avg_vol > 0 else 1.0
        if vol_ratio <= 0.6:
            pts += 3; breakdown.append((f"📊 Volume {vol_ratio:.2f}x (extreme decay)", 3))
        elif vol_ratio <= 0.85:
            pts += 2; breakdown.append((f"📊 Volume {vol_ratio:.2f}x (quiet)", 2))
        else:
            breakdown.append((f"📊 Volume {vol_ratio:.2f}x", 0))

    zones = get_htf_zones(symbol)
    in_zone, zone_lbl = is_in_zone(live_price, direction, zones)
    if in_zone:
        pts += 3; breakdown.append((f"📍 Inside HTF Zone ({zone_lbl})", 3))

    t_4h = get_htf_trend(symbol, "4h")
    t_1d = get_htf_trend(symbol, "1d")
    target_dir = 1 if direction == "BUY" else -1
    if t_4h == target_dir and t_1d == target_dir:
        pts += 3; breakdown.append(("📡 1D + 4H Trend Aligned", 3))
    elif t_4h == target_dir:
        pts += 1.5; breakdown.append(("📡 4H Trend Aligned", 1.5))

    sl_dist = abs(live_price - sl_price)
    tp_dist = abs(tp_price - live_price)
    rr_ratio = tp_dist / sl_dist if sl_dist > 0 else 0
    if rr_ratio >= 3.5:
        pts += 4; breakdown.append((f"⚖️ Asymmetric R:R (1:{rr_ratio:.1f})", 4))
    elif rr_ratio >= 2.0:
        pts += 2; breakdown.append((f"⚖️ R:R Ratio (1:{rr_ratio:.1f})", 2))

    grade = "Grade A+ 🍀" if pts >= 10 else "Grade A 🍀" if pts >= 7 else "Grade B"
    return grade, pts, breakdown


def get_macro_structure_sl_tp(symbol, direction, entry_price):
    """Anchors Macro SL to real 4H Swing Structure. VERIFIED before"""
    klines_4h = get_klines(symbol, "4h", 40)
    if not klines_4h or len(klines_4h) < 20:
        sl = entry_price * 0.98 if direction == "BUY" else entry_price * 1.02
        return sl

    atr_4h = calculate_atr(klines_4h, 14)
    cushion = atr_4h * 0.5
    ms_4h = detect_market_structure(klines_4h)

    if direction == "BUY":
        pivot = ms_4h["swing_low"] if ms_4h["swing_low"] > 0 else min(float(k[3]) for k in klines_4h[-15:])
        sl = pivot - cushion
        sl = min(sl, entry_price * (1 - MIN_SL_PCT))
    else:
        pivot = ms_4h["swing_high"] if ms_4h["swing_high"] > 0 else max(float(k[2]) for k in klines_4h[-15:])
        sl = pivot + cushion
        sl = max(sl, entry_price * (1 + MIN_SL_PCT))

    return sl


def check_active_macro_coils():
    """The Heartbeat of the Pre-Breakout Engine. Runs every scan cycle to"""
    global macro_coils
    now = get_ist_datetime()
    keys_to_delete = []

    for coin, data in list(macro_coils.items()):
        symbol = data["symbol"]
        live_price = get_price(symbol)
        if not live_price: continue

        hours_active = (now - data["detected_at"]).total_seconds() / 3600

        if hours_active > 72:
            logger.info(f"{coin} Macro Coil expired (72h limit).")
            keys_to_delete.append(coin)
            continue

        level = data["level"]
        if data["direction"] == "BUY" and live_price < level * 0.98:
            send_telegram(f"❌ <b>MACRO SETUP INVALIDATED</b>\n🏗️ Engine: 🛰️ RADAR ENGINE\n🪙 {coin} broke 2% below {data['pattern']} support. Removed from radar.")
            keys_to_delete.append(coin)
            continue
        elif data["direction"] == "SELL" and live_price > level * 1.02:
            send_telegram(f"❌ <b>MACRO SETUP INVALIDATED</b>\n🏗️ Engine: 🛰️ RADAR ENGINE\n🪙 {coin} broke 2% above {data['pattern']} resistance. Removed from radar.")
            keys_to_delete.append(coin)
            continue

        klines_15m = get_klines(symbol, "15m", 25)
        if klines_15m:
            vols = [float(k[5]) for k in klines_15m]
            avg_vol = sum(vols[-20:-1]) / 19 if len(vols) >= 20 else 1.0

            live_vol = vols[-1]
            open_time_ms = float(klines_15m[-1][0])
            seconds_open = (time.time() * 1000 - open_time_ms) / 1000

            if seconds_open < 30:
                projected_vol = live_vol
            else:
                projected_vol = live_vol * (900 / min(seconds_open, 900))

            live_vol_ratio = projected_vol / avg_vol if avg_vol > 0 else 0
            _macro_vol_floor_ok = live_vol >= avg_vol * 0.20

            if live_vol_ratio >= 1.8 and _macro_vol_floor_ok:
                if data["direction"] == "BUY":
                    is_valid_breakout = level < live_price <= (level * 1.025)
                else:
                    is_valid_breakout = level > live_price >= (level * 0.975)

                if is_valid_breakout:
                    macro_sl = get_macro_structure_sl_tp(symbol, data["direction"], live_price)

                    sl_dist = abs(live_price - macro_sl)
                    min_tp_dist = sl_dist * MIN_RR_RATIO
                    macro_zones = get_htf_zones(symbol)
                    macro_tp = get_structural_tp(live_price, data["direction"], macro_zones, min_tp_dist)
                    if macro_tp is None:
                        atr_4h = calculate_atr(get_klines(symbol, "4h", 20), 14)
                        atr_tp_dist = atr_4h * ATR_TP_MULTIPLIER
                        tp_dist = max(atr_tp_dist, min_tp_dist)
                        macro_tp = live_price + tp_dist if data["direction"] == "BUY" else live_price - tp_dist

                    klines_4h_grade = get_klines(symbol, "4h", 30)
                    macro_grade, macro_pts, macro_breakdown = get_macro_coil_grade(symbol, data["direction"], klines_4h_grade, live_price, macro_sl, macro_tp)
                    macro_setup = {
                        "coin": coin, "symbol": symbol, "direction": data["direction"],
                        "pattern": f"Pre-Breakout Macro ({data['pattern']})", "setup_score": 99.0,
                        "leverage": get_smart_leverage(symbol, 1.0, 99.0), "scan_price": live_price,
                        "market_condition": "unknown", "tf_score": get_timeframe_score(symbol, data["direction"]),
                        "macro_sl": macro_sl,
                        "macro_tp": macro_tp,
                        "is_macro": True,
                        "macro_grade": macro_grade,
                        "macro_pts": macro_pts,
                        "macro_breakdown": macro_breakdown,
                        "macro_ai_reasoning": data.get("ai_reasoning", "Upstream Macro AI approved."),
                    }
                    logger.info(f"{coin} MACRO BREAKOUT TRIGGERED: {data['pattern']} {data['direction']} ({macro_grade}, {macro_pts}pts) on {live_vol_ratio:.1f}x volume — executing.")
                    format_and_send(macro_setup, coin, is_instant=True, market_condition="unknown")
                    keys_to_delete.append(coin)
                    continue

        hours_since_ping = (now - data["last_update_sent"]).total_seconds() / 3600
        if hours_since_ping >= 4.0:
            send_telegram(
                f"⏳ <b>MACRO RADAR UPDATE</b>\n\n"
                f"🪙 <b>{coin}</b>  {'🟢' if data['direction']=='BUY' else '🔴'} {data['direction']}\n"
                f"📌 {data['pattern']}\n"
                f"📍 Level: {format_price(level)}  |  Now: {format_price(live_price)}\n\n"
                f"<i>Setup remains valid and coiling. Monitoring for volume breakout.</i>\n"
                f"🕐 {get_ist_time()}"
            )
            macro_coils[coin]["last_update_sent"] = now

    for k in keys_to_delete:
        if k in macro_coils:
            del macro_coils[k]


def detect_order_flow_sniper(symbol, klines, price):
    """Order Flow Sniper — a genuinely STANDALONE predictive trigger, built"""
    if len(klines) < 5: return None
    flow_direction = detect_aggressive_order_flow(klines)
    if not flow_direction: return None
    t_4h = get_htf_trend(symbol, "4h")
    t_1h = get_htf_trend(symbol, "1h")

    closes = [float(k[4]) for k in klines[-5:]]
    price_moving_up = closes[-1] > closes[0]
    price_moving_down = closes[-1] < closes[0]

    if flow_direction == "BUY" and t_4h == 1 and t_1h == 1 and price_moving_up:
        return "BUY"
    if flow_direction == "SELL" and t_4h == -1 and t_1h == -1 and price_moving_down:
        return "SELL"
    return None

def get_fear_greed_index():
    try:
        res=requests.get("https://api.alternative.me/fng/?limit=1",timeout=10)
        return int(res.json()["data"][0]["value"]) if res.status_code==200 else 50
    except Exception as e:
        logger.warning(f"F&G: {e}"); return 50

def is_sentiment_valid(direction,fng,pattern_name=""):
    """Sentiment Guard: prevents buying into pure panic or shorting pure"""
    predictive_patterns = (
        "Inside Bar Coil","Pre-Breakout Compression",
        "Volatility Contraction (Coiling)","Early Spark Ignition",
        "Smart Money Absorption","Funding Divergence Sniper",
        "PDL Reversal Sweep","PDH Reversal Sweep","ChoCh + Fib 0.618 Golden Zone"
    )
    if pattern_name in predictive_patterns:
        return True
    return not (direction=="BUY" and fng<20) and not (direction=="SELL" and fng>80)

def check_relative_strength(symbol, btc_klines_1h):
    """The Law of Idiosyncratic Alpha. Alts trade against a backdrop of BTC"""
    alt_klines = get_klines(symbol, "1h", 5)
    if not alt_klines or len(alt_klines) < 4 or not btc_klines_1h or len(btc_klines_1h) < 4:
        return 0.0, 0.0

    alt_start, alt_curr = float(alt_klines[-4][4]), float(alt_klines[-1][4])
    btc_start, btc_curr = float(btc_klines_1h[-4][4]), float(btc_klines_1h[-1][4])

    alt_perf = (alt_curr - alt_start) / alt_start if alt_start > 0 else 0
    btc_perf = (btc_curr - btc_start) / btc_start if btc_start > 0 else 0

    return alt_perf, btc_perf

htf_trend_cache = {}

def get_htf_trend(symbol,interval="1h"):
    """TTL CACHING ADDED (this round): VERIFIED THIS WAS GENUINELY NEEDED"""
    cache_key = f"{symbol}_{interval}"
    now = get_ist_datetime()
    cached = htf_trend_cache.get(cache_key)
    if cached and (now - cached["cached_at"]).total_seconds() < 900:
        return cached["trend"]
    try:
        klines=get_klines(symbol,interval,50)
        if not klines or len(klines)<50:
            trend = 0
        else:
            closes=[float(k[4]) for k in klines]
            e20=calculate_ema(closes,20); e50=calculate_ema(closes,50)
            trend = (1 if e20>e50 else -1) if (e20 and e50) else 0
        htf_trend_cache[cache_key] = {"trend": trend, "cached_at": now}
        return trend
    except Exception as e:
        logger.warning(f"HTF {symbol} {interval}: {e}"); return 0

def is_btc_aligned(direction):
    """Shared BTC 1h alignment check — replaces the deleted Order Book check"""
    btc_1h_trend = get_htf_trend("BTCUSDT","1h")
    aligned = (btc_1h_trend==1 and direction=="BUY") or (btc_1h_trend==-1 and direction=="SELL")
    return aligned, btc_1h_trend

def get_volume_ratio(klines):
    """Shared volume-vs-20-candle-average ratio. Consolidated here: this same"""
    if not klines: return 1.0
    vols = [float(k[5]) for k in klines]
    avg_vol = sum(vols[-20:])/20 if len(vols)>=20 else (vols[-1] if vols else 1)
    return vols[-1]/avg_vol if avg_vol>0 else 1.0

def get_global_volume(symbol, binance_klines):
    """GLOBAL VOLUME RADAR: cross-references 15m volume across Binance,"""
    if not binance_klines or len(binance_klines) < 20:
        return 1.0, "Binance"

    b_vols = [float(k[5]) for k in binance_klines]
    b_avg = sum(b_vols[-20:-1]) / 19 if len(b_vols) >= 20 else 1.0
    highest_ratio = b_vols[-1] / b_avg if b_avg > 0 else 1.0
    lead_exchange = "Binance"

    try:
        res = requests.get(BYBIT_KLINE_URL, params={"category": "linear", "symbol": symbol, "interval": "15", "limit": 20}, timeout=3)
        if res.status_code == 200:
            data = res.json().get("result", {}).get("list", [])
            if len(data) >= 20:
                by_vols = [float(k[5]) for k in data]
                by_avg = sum(by_vols[1:20]) / 19
                by_ratio = by_vols[0] / by_avg if by_avg > 0 else 1.0
                if by_ratio > highest_ratio:
                    highest_ratio = by_ratio
                    lead_exchange = "Bybit"
    except Exception as e:
        logger.warning(f"get_global_volume Bybit {symbol}: {e}")

    try:
        okx_sym = symbol.replace("USDT", "-USDT-SWAP")
        res = requests.get(OKX_KLINE_URL, params={"instId": okx_sym, "bar": "15m", "limit": 20}, timeout=3)
        if res.status_code == 200:
            data = res.json().get("data", [])
            if len(data) >= 20:
                ok_vols = [float(k[5]) for k in data]
                ok_avg = sum(ok_vols[1:20]) / 19
                ok_ratio = ok_vols[0] / ok_avg if ok_avg > 0 else 1.0
                if ok_ratio > highest_ratio:
                    highest_ratio = ok_ratio
                    lead_exchange = "OKX"
    except Exception as e:
        logger.warning(f"get_global_volume OKX {symbol}: {e}")

    return round(highest_ratio, 2), lead_exchange

def price_at_pnl(entry, direction, lev, target_pnl):
    """Shared "what price corresponds to X% PnL" calculation. Consolidated"""
    move = entry * (target_pnl/100) / lev
    return entry+move if direction=="BUY" else entry-move

def get_timeframe_score(symbol,direction):
    """Point 4 (Daily Macro Filter): a Daily-trend disagreement is now a HARD"""
    di=1 if direction=="BUY" else -1
    d1=get_htf_trend(symbol,"1d")
    if d1!=0 and d1!=di: return -1
    h4=get_htf_trend(symbol,"4h"); h1=get_htf_trend(symbol,"1h")
    if h4!=0 and h4!=di: return -1
    score=0
    if h4==di: score+=2
    if h1==di: score+=1
    return score

def get_structure_sl(klines,direction,entry,atr):
    """Structural Stop Loss with Institutional Volatility Cushion."""
    min_dist = entry * MIN_SL_PCT

    ms = detect_market_structure(klines)
    has_valid_swing = ms["swing_low"] > 0 and ms["swing_high"] > 0

    cushion = atr * 0.5

    if has_valid_swing:
        if direction == "BUY":
            sl = ms["swing_low"] - cushion
        else:
            sl = ms["swing_high"] + cushion
    else:
        logger.info("get_structure_sl: no valid swing data, falling back to ATR")
        if direction == "BUY":
            sl = entry - atr * ATR_SL_MULTIPLIER
        else:
            sl = entry + atr * ATR_SL_MULTIPLIER

    if direction == "BUY":
        return min(sl, entry - min_dist)
    return max(sl, entry + min_dist)

def check_circuit_breaker():
    global daily_losses,circuit_breaker_until,last_reset_day
    today=datetime.now(IST).date()
    if today!=last_reset_day:
        daily_losses=0; circuit_breaker_until=None; last_reset_day=today
        save_circuit_breaker(); return False
    if circuit_breaker_until:
        try:
            until_dt=datetime.fromisoformat(circuit_breaker_until)
            if datetime.now(IST)>=until_dt:
                daily_losses=0; circuit_breaker_until=None
                save_circuit_breaker()
                send_telegram(f"✅ <b>{BOT_HEADER}</b>\nCircuit Breaker RESET - scanning resumed!")
                return False
            return True
        except Exception:
            circuit_breaker_until=None; return False
    return daily_losses>=MAX_DAILY_LOSSES

def increment_daily_losses(pnl):
    global daily_losses,circuit_breaker_until
    if pnl>CIRCUIT_BREAKER_MIN_LOSS:
        logger.info(f"Small loss {pnl:.2f}% - not counted"); return
    daily_losses+=1
    if daily_losses==MAX_DAILY_LOSSES:
        midnight=(datetime.now(IST)+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
        circuit_breaker_until=midnight.isoformat()
        save_circuit_breaker()
        send_telegram(f"🚨 <b>{BOT_HEADER}</b>\nCIRCUIT BREAKER ACTIVE\n3 big losses today.\nResumes at midnight IST.")

def is_btc_crashing():
    try:
        klines=get_klines("BTCUSDT","1h",5)
        if not klines or len(klines)<4: return False
        now=float(klines[-1][4]); h4=float(klines[-4][4])
        drop=((now-h4)/h4)*100
        if drop<-5.0: logger.info(f"BTC crashed {drop:.1f}% in 4h"); return True
        return False
    except Exception: return False

def get_adjusted_score(pattern_name,base_score,market_condition):
    """FIX: previously this blended base_score with historical win rate (mc_wr)"""
    stats=pattern_stats.get(pattern_name,{})
    weight=stats.get("weight",1.0)
    adjusted=base_score*weight
    return min(round(adjusted,1),99.0)

def check_sector_correlation(coin, direction):
    """Point 3: Trade like a human — check the "neighborhood" before confirming."""
    sector = COIN_SECTOR.get(coin)
    if not sector:
        return True, "no sector defined"
    peers = [c for c in SECTOR_GROUPS[sector] if c != coin][:4]
    if len(peers) < 2:
        return True, "insufficient sector peers"

    agree = 0
    checked = 0
    for peer in peers:
        try:
            k = get_klines(peer+"USDT", "15m", 5)
            if not k or len(k) < 3: continue
            closes_p = [float(x[4]) for x in k]
            change_pct = (closes_p[-1] - closes_p[-3]) / closes_p[-3] * 100 if closes_p[-3] > 0 else 0
            checked += 1
            if direction == "BUY" and change_pct > -0.3: agree += 1
            elif direction == "SELL" and change_pct < 0.3: agree += 1
        except Exception:
            continue

    if checked < 2:
        return True, "insufficient sector data"

    agree_ratio = agree / checked
    passes = agree_ratio >= 0.5
    note = f"sector {sector}: {agree}/{checked} peers agree"
    return passes, note


htf_1h_cache = {}

def get_cached_1h_klines(symbol):
    """Caches 1h klines specifically for the RSI divergence check inside"""
    now = get_ist_datetime()
    cached = htf_1h_cache.get(symbol)
    if cached and (now - cached["cached_at"]).total_seconds() < 900:
        return cached["klines"]

    klines = get_klines(symbol, "1h", 20)
    if klines:
        htf_1h_cache[symbol] = {"klines": klines, "cached_at": now}
    return klines

def detect_fvg_momentum_extension(klines):
    """Smart Money Concept: Fair Value Gap (FVG) detection, with an"""
    if len(klines) < 5: return None

    c1, c2, c3 = klines[-4], klines[-3], klines[-2]

    high1, low1 = float(c1[2]), float(c1[3])
    high2, low2 = float(c2[2]), float(c2[3])
    high3, low3, close3 = float(c3[2]), float(c3[3]), float(c3[4])

    if low3 > high1:
        if close3 < high2:
            return "BULLISH_GAP_RETRACING"
        elif close3 > high2:
            return "BULLISH_GAP_EXTENDING"

    if high3 < low1:
        if close3 > low2:
            return "BEARISH_GAP_RETRACING"
        elif close3 < low2:
            return "BEARISH_GAP_EXTENDING"

    return None


def compute_confirmation_bonus(symbol, direction, klines, vols, tf_score, btc_aligned=False, zone_ok=False, ms_bos=False, ms_bias=None, ms_choch=False, is_tier1=True, is_compression=False, is_sweep=False, entry=None, sl=None):
    """The Location Multiplier + hard Tier 1/Tier 2 AI cap."""
    bonus = 0.0
    notes = []

    if entry is not None and sl is not None and entry > 0:
        sl_dist_pct = abs(entry - sl) / entry * 100
        if sl_dist_pct < 0.5:
            bonus += 6.0; notes.append(f"Risk-Proximity: SL {sl_dist_pct:.2f}% away - tight stop (+6.0)")
        elif sl_dist_pct < 1.0:
            bonus += 3.0; notes.append(f"Risk-Proximity: SL {sl_dist_pct:.2f}% away (+3.0)")
        elif sl_dist_pct < 1.5:
            bonus += 1.5; notes.append(f"Risk-Proximity: SL {sl_dist_pct:.2f}% away (+1.5)")

    if is_sweep:
        bonus += 4.0; notes.append("Liquidity Sweep - Spring/Upthrust priority (+4.0)")

    flow_direction = detect_aggressive_order_flow(klines)
    if flow_direction == direction:
        bonus += 2.0; notes.append(f"Aggressive order flow confirms {direction} (+2.0)")

    gap_type = detect_fvg_momentum_extension(klines)
    if gap_type == "BULLISH_GAP_EXTENDING" and direction == "BUY":
        bonus += 3.0; notes.append("Bullish FVG extending — immediate momentum follow-through (+3.0)")
    elif gap_type == "BEARISH_GAP_EXTENDING" and direction == "SELL":
        bonus += 3.0; notes.append("Bearish FVG extending — immediate momentum follow-through (+3.0)")
    elif gap_type == "BULLISH_GAP_RETRACING" and direction == "BUY":
        notes.append("Note: Bullish FVG retracing into C2 (expect a fill/retest)")
    elif gap_type == "BEARISH_GAP_RETRACING" and direction == "SELL":
        notes.append("Note: Bearish FVG retracing into C2 (expect a fill/retest)")

    try:
        klines_1h_div = get_cached_1h_klines(symbol)
        if klines_1h_div and len(klines_1h_div) >= 10:
            closes_1h = [float(k[4]) for k in klines_1h_div]
            div_1h = detect_rsi_divergence(closes_1h)
            if (div_1h == "BULLISH_DIV" and direction == "BUY") or (div_1h == "BEARISH_DIV" and direction == "SELL"):
                bonus += 3.0; notes.append(f"1h RSI divergence confirms {direction} (+3.0)")
    except Exception as e:
        logger.warning(f"1h RSI divergence {symbol}: {e}")

    if is_tier1:
        choch_in_zone = ms_choch and zone_ok
        if choch_in_zone:
            bonus += 7.5; notes.append("ChoCh inside zone - ultimate signal (+7.5)")
        else:
            if zone_ok:
                bonus += 6.0; notes.append("in S/D zone - Location Multiplier (+6.0)")

            structure_agrees = ms_bias == ("bullish" if direction == "BUY" else "bearish")
            if is_compression:
                bonus += 3.5; notes.append("Pre-breakout coiling consolidation (+3.5)")
            elif ms_bos and structure_agrees:
                bonus += 3.0; notes.append("BOS confirms direction - Shift (+3.0)")
            elif structure_agrees:
                bonus += 1.2; notes.append("structure bias agrees (+1.2)")
    else:
        notes.append("Tier 2: zone/BOS/ChoCh bonuses excluded by design (auto-execute only)")

    oi_change_pct = get_oi_change_pct(symbol)
    funding_rate = get_funding_rate(symbol)
    if oi_change_pct is not None and funding_rate is not None and oi_change_pct >= SQUEEZE_OI_RISING_PCT:
        if direction == "BUY" and funding_rate <= SQUEEZE_FUNDING_EXTREME_NEG:
            bonus += 3.0; notes.append(f"Squeeze: OI +{oi_change_pct:.1f}% + funding {funding_rate*100:.3f}% (short squeeze setup) (+3.0)")
        elif direction == "SELL" and funding_rate >= SQUEEZE_FUNDING_EXTREME_POS:
            bonus += 3.0; notes.append(f"Squeeze: OI +{oi_change_pct:.1f}% + funding {funding_rate*100:.3f}% (long squeeze setup) (+3.0)")

    if tf_score == 3:
        bonus += 3.0; notes.append("HTF fully aligned (+3.0)")
    elif tf_score == 2:
        bonus += 1.5; notes.append("HTF partially aligned (+1.5)")

    if btc_aligned:
        bonus += 2.0; notes.append("BTC 1h trend aligned (+2.0)")

    avg_vol = sum(vols[-20:]) / 20 if len(vols) >= 20 else (vols[-1] if vols else 1)
    vol_ratio = vols[-1] / avg_vol if avg_vol > 0 else 1.0
    if vol_ratio >= 1.5:
        bonus += 2.0; notes.append("volume strong (+2.0)")
    elif vol_ratio >= 1.2:
        bonus += 1.0; notes.append("volume moderate (+1.0)")

    adx_val = calculate_adx(klines)
    if adx_val >= 30:
        bonus += 1.5; notes.append("ADX strong (+1.5)")

    return round(bonus, 1), notes


def get_all_pattern_scores(patterns,market_condition):
    scored=[]
    for pat_tuple in patterns:
        name, base_score, direction = pat_tuple[0], pat_tuple[1], pat_tuple[2]
        geo_notes = pat_tuple[3] if len(pat_tuple) > 3 else None
        adj=get_adjusted_score(name,base_score,market_condition)
        scored.append((name,adj,direction,base_score,geo_notes))
    scored.sort(key=lambda x:x[1],reverse=True)
    return scored

def learn_from_trade(coin,pattern,result,pnl,mc,tf_score):
    global learning_notes,market_memory,consecutive_loss_patterns
    if result=="WIN": market_memory[mc]["wins"]+=1
    else:             market_memory[mc]["losses"]+=1
    wins_by_pat={}
    for e in trade_journal:
        if e.get("market_condition")==mc and e.get("result")=="WIN":
            p=e.get("pattern","?"); wins_by_pat[p]=wins_by_pat.get(p,0)+1
    if wins_by_pat:
        market_memory[mc]["best_pattern"]=max(wins_by_pat,key=wins_by_pat.get)
    if pattern not in consecutive_loss_patterns:
        consecutive_loss_patterns[pattern]={"consecutive_losses":0,"suspended_until":None}
    if result=="LOSS":
        consecutive_loss_patterns[pattern]["consecutive_losses"]+=1
        cl=consecutive_loss_patterns[pattern]["consecutive_losses"]
        sigs=pattern_stats.get(pattern,{}).get("signals",0)
        if cl>=CONSEC_LOSS_SUSPEND and sigs>=MIN_SIGNALS_TO_SUSPEND:
            su=(datetime.now(IST)+timedelta(hours=SUSPEND_HOURS)).isoformat()
            consecutive_loss_patterns[pattern]["suspended_until"]=su
            send_telegram(f"🧠 <b>{BOT_HEADER}</b>\nPattern suspended: {pattern}\n{cl} consecutive losses.")
    else:
        consecutive_loss_patterns[pattern]["consecutive_losses"]=0
        consecutive_loss_patterns[pattern]["suspended_until"]=None
    if pattern in pattern_stats:
        s=pattern_stats[pattern]; sigs=s.get("signals",0)
        if sigs>=3:
            wr=(s["wins"]/sigs)*100
            if wr>=70:   s["weight"]=min(s["weight"]+0.1,1.5)
            elif wr<40:  s["weight"]=max(s["weight"]-0.15,0.5)
            mc_trades=[t for t in trade_journal if t.get("pattern")==pattern and t.get("market_condition")==mc]
            mc_wins=sum(1 for t in mc_trades if t["result"]=="WIN")
            mc_wr=(mc_wins/len(mc_trades)*100) if mc_trades else 50.0
            s[f"{mc}_wr"]=round(mc_wr,1)
    stats=pattern_stats.get(pattern,{}); sigs2=stats.get("signals",0); note=None
    if sigs2>=5:
        wr=(stats["wins"]/sigs2)*100
        if result=="LOSS" and wr<45:
            note=f"Pattern '{pattern}' only {wr:.1f}% WR - consider avoiding in {mc} market."
        elif result=="WIN" and wr>70:
            note=f"Pattern '{pattern}' strong - {wr:.1f}% WR in {mc} market."
    if note and note not in learning_notes:
        learning_notes.append(note)
        if len(learning_notes)>100: learning_notes=learning_notes[-100:]
    save_learning()
    cloud_save_learning()

def get_crypto_news():
    """Fetch news from CryptoPanic (primary) + CryptoCompare (fallback) with beautiful output."""
    headlines = []
    if NEWS_API_KEY:
        try:
            res = requests.get(
                "https://cryptopanic.com/api/v1/posts/",
                params={"auth_token": NEWS_API_KEY, "kind": "news",
                        "filter": "hot", "public": "true"},
                timeout=10
            )
            if res.status_code == 200:
                for item in res.json().get("results", [])[:8]:
                    title  = item.get("title", "")[:90]
                    source = item.get("domain", "CryptoPanic")
                    votes  = item.get("votes", {})
                    pos = votes.get("positive", 0); neg = votes.get("negative", 0)
                    sent = "🟢" if pos > neg else "🔴" if neg > pos else "⚪"
                    currencies = [c["code"] for c in item.get("currencies", [])[:3]]
                    tags = "  <i>" + " ".join(f"#{c}" for c in currencies) + "</i>" if currencies else ""
                    if title:
                        headlines.append(f"{sent} <b>{title}</b>\n     <i>— {source}</i>{tags}")
        except Exception as e:
            logger.warning(f"CryptoPanic: {e}")
    if not headlines:
        try:
            res = requests.get(
                "https://min-api.cryptocompare.com/data/v2/news/?lang=EN&sortOrder=latest",
                timeout=10
            )
            if res.status_code == 200:
                for a in res.json().get("Data", [])[:6]:
                    title  = a.get("title", "")[:90]
                    source = a.get("source_info", {}).get("name", "Unknown")
                    if title:
                        headlines.append(f"⚪ <b>{title}</b>\n     <i>— {source}</i>")
        except Exception as e:
            logger.warning(f"CryptoCompare: {e}")
    fng = get_fear_greed_index()
    fng_lbl = ("Extreme Fear 😨" if fng<=25 else "Fear 😟" if fng<=45 else
               "Neutral 😐" if fng<=55 else "Greed 😊" if fng<=75 else "Extreme Greed 🤑")
    fng_bar = "█"*min(int(fng/10),10) + "░"*(10-min(int(fng/10),10))
    fng_em = "🔴" if fng<=25 else "🟠" if fng<=45 else "🟡" if fng<=55 else "🟢"
    prices = []
    for sym, lbl in [("BTCUSDT","₿  BTC"),("ETHUSDT","Ξ  ETH"),
                     ("SOLUSDT","◎  SOL"),("BNBUSDT","◈  BNB"),("XRPUSDT","✦  XRP")]:
        p = get_price(sym)
        if p: prices.append(f"  │  {lbl}  <code>${format_price(p)}</code>")
    news_src = "CryptoPanic 🔥" if (NEWS_API_KEY and headlines) else "CryptoCompare"
    msg  = (f"╔══════════════════════════════════╗\n"
            f"║   📰  CRYPTO NEWS & MARKET       ║\n"
            f"╚══════════════════════════════════╝\n\n")
    msg += f"  {fng_em} <b>Fear & Greed: {fng} — {fng_lbl}</b>\n"
    msg += f"  [{fng_bar}]\n\n"
    msg += f"  ┌── LIVE PRICES ──────────────┐\n"
    for p in prices: msg += p + "\n"
    msg += f"  └─────────────────────────────┘\n\n"
    msg += f"  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"  🗞️ <b>Latest News</b>  <i>(via {news_src})</i>\n\n"
    if headlines:
        msg += "\n\n".join(f"  {h}" for h in headlines[:6])
    else:
        msg += "  No news available right now."
    msg += f"\n\n  🕐 {get_ist_time()}"
    return msg

def run_backtest(symbol):
    """Audit Fix #3: Realistic backtest with fees (0.05% per side) and slippage (0.1%)."""
    FEE_PCT      = 0.05
    SLIPPAGE_PCT = 0.10
    LEVERAGE     = 5
    try:
        klines=get_klines(symbol,"15m",1000)
        if not klines or len(klines)<100: return f"Not enough data for {symbol}"
        results={"WIN":0,"LOSS":0,"SKIP":0}
        cond_res={"bull":{"W":0,"L":0},"bear":{"W":0,"L":0},"sideways":{"W":0,"L":0}}
        total_pnl=0.0; window=60
        for i in range(window,len(klines)-10):
            wk=klines[i-window:i]; price=float(klines[i][4])
            closes=[float(k[4]) for k in wk]; e20=calculate_ema(closes,20); e50=calculate_ema(closes,50)
            rng=((max(closes[-20:])-min(closes[-20:]))/min(closes[-20:]))*100 if min(closes[-20:])>0 else 0
            if e20 and e50:
                if e20>e50*1.02:   cond="bull"
                elif e20<e50*0.98: cond="bear"
                else:              cond="sideways" if rng<5 else ("bull" if price>e50 else "bear")
            else: cond="sideways"
            bt=1 if (e20 and e50 and e20>e50) else -1
            found=detect_patterns(symbol,wk,price,bt)
            if not found: continue
            best=max(found,key=lambda x:x[1])
            if best[1]<MIN_PRIMARY_SCORE: continue
            atr=calculate_atr(wk)
            if atr==0: continue
            direction=best[2]
            slip = price * SLIPPAGE_PCT / 100
            entry = price + slip if direction=="BUY" else price - slip
            sl=entry-atr*ATR_SL_MULTIPLIER if direction=="BUY" else entry+atr*ATR_SL_MULTIPLIER
            tp=entry+atr*ATR_TP_MULTIPLIER if direction=="BUY" else entry-atr*ATR_TP_MULTIPLIER
            hit="SKIP"
            for j in range(i+1,min(i+96,len(klines))):
                fh=float(klines[j][2]); fl=float(klines[j][3])
                if direction=="BUY":
                    if fh>=tp: hit="WIN";  break
                    if fl<=sl: hit="LOSS"; break
                else:
                    if fl<=tp: hit="WIN";  break
                    if fh>=sl: hit="LOSS"; break
            if hit=="SKIP": results["SKIP"]+=1; continue
            results[hit]+=1; cond_res[cond]["W" if hit=="WIN" else "L"]+=1
            gross = (abs(tp-entry)/entry)*100*LEVERAGE if hit=="WIN" else -(abs(sl-entry)/entry)*100*LEVERAGE
            total_cost = (FEE_PCT * 2 + SLIPPAGE_PCT) * LEVERAGE
            pnl = gross - total_cost
            total_pnl+=pnl
        total=results["WIN"]+results["LOSS"]; wr=(results["WIN"]/total*100) if total>0 else 0
        r =(f"┌──────────────────────────────────┐\n"
            f"│  🔬  BACKTEST: {symbol:<18}│\n"
            f"└──────────────────────────────────┘\n\n"
            f"  ⚠️ Realistic: fees {FEE_PCT*2:.2f}% + slippage {SLIPPAGE_PCT:.2f}%\n\n"
            f"  📊 Total Trades : {total}\n"
            f"  ✅ Wins         : {results['WIN']}\n"
            f"  ❌ Losses       : {results['LOSS']}\n"
            f"  🎯 Win Rate     : <b>{wr:.1f}%</b>\n"
            f"  💰 Net PnL      : {fmt_pnl(total_pnl)}\n\n"
            f"  ── By Market Condition ──\n")
        for cond,res in cond_res.items():
            ct=res["W"]+res["L"]; wr2=(res["W"]/ct*100) if ct>0 else 0
            em="📈" if cond=="bull" else "📉" if cond=="bear" else "➡️"
            r+=f"  {em} {cond:<9}: {res['W']}W/{res['L']}L ({wr2:.1f}%)\n"
        r+=f"\n  🕐 {get_ist_time()}"
        return r
    except Exception as e: return f"Backtest failed: {e}"

def _H(title, emoji=""):
    """Safe Telegram header — no box drawing chars that can cause parse failures."""
    icon = f"{emoji} " if emoji else ""
    return f"{'━'*32}\n{icon}<b>{title}</b>\n{'━'*32}"

def get_active_trades_text():
    if not active_trades:
        return (f"{_H('ACTIVE TRADES','📊')}\n\n"
                f"  ⚪  No active trades right now.\n\n"
                f"  🛡️ CB      : {'🔴 ACTIVE' if check_circuit_breaker() else '🟢 OK'}\n"
                f"  ⏳ Pending : {len(pending_signals)}\n"
                f"  🕐 {get_ist_time()}")
    now=get_ist_datetime(); lines=[]; total_pnl=0.0
    for coin,t in active_trades.items():
        price=get_price(t.get("symbol",coin+"USDT"))
        sl_pct=abs(t["entry"]-t["sl"])/t["entry"]*100
        tp_pct=abs(t["tp"]-t["entry"])/t["entry"]*100
        rr=round(tp_pct/sl_pct,1) if sl_pct>0 else 0
        dirn=t.get("direction","?"); lev=t.get("leverage",1)
        pat=t.get("pattern","?").split(" + ")[0]
        dir_em="🟢 LONG  ▲" if dirn=="BUY" else "🔴 SHORT ▼"
        dur=""
        if t.get("timestamp"):
            try:
                m=int((now-t["timestamp"]).total_seconds()/60)
                dur=f"{m}m" if m<60 else f"{m//60}h {m%60}m"
            except Exception: pass
        if price:
            pnl=((price-t["entry"])/t["entry"])*100*lev if dirn=="BUY" else ((t["entry"]-price)/t["entry"])*100*lev
            total_pnl+=pnl; pnl_txt=fmt_pnl(pnl)
        else: pnl_txt="⏳"
        ms=t.get("milestones_sent",[])
        badge=("  🚀 M3 LOCKED" if "p3" in ms else "  🔥 M2 LOCKED" if "p2" in ms else "  ✅ M1 BREAKEVEN" if "p1" in ms else "")
        target=t.get("profit_target", abs(t['tp']-t['entry'])/t['entry']*100*lev)
        partial="  💰 Partial TP" if t.get("partial_tp_taken") else ""
        lines.append(
            f"  ┌─────────────────────────────┐\n"
            f"  │  🪙 <b>{coin}</b>  {dir_em}  ✦ {lev}x\n"
            f"  │  💰 Entry  : <code>{format_price(t['entry'])}</code>\n"
            f"  │  🎯 Target : <code>{format_price(t['tp'])}</code>  ↑{tp_pct:.2f}%\n"
            f"  │  🛑 Stop   : <code>{format_price(t['sl'])}</code>  ↓{sl_pct:.2f}%\n"
            f"  │  ⚖️  RR 1:{rr}   ⏱️ {dur or 'just now'}\n"
            f"  │  📈 PnL    : {pnl_txt}  🎯Target:+{target:.1f}%{partial}\n"
            f"  │  📌 {pat}{badge}\n"
            f"  └─────────────────────────────┘"
        )
    return (f"{_H(f'ACTIVE TRADES  {len(active_trades)}/{MAX_ACTIVE_TRADES}','📊')}\n\n"
            + "\n\n".join(lines) +
            f"\n\n  ══════════════════════════════\n"
            f"  💼 Portfolio PnL : {fmt_pnl(total_pnl)}\n"
            f"  🛡️ CB      : {'🔴 ACTIVE' if check_circuit_breaker() else '🟢 OK'}\n"
            f"  ⏳ Pending : {len(pending_signals)}\n"
            f"  🕐 {get_ist_time()}")

def get_expectancy_report_text():
    """Point 5 of the four-part redesign (this round): win rate alone is a"""
    if not trade_journal:
        return f"{_H('EXPECTANCY & PROFIT FACTOR','📊')}\n\n  ⚪ No closed trades yet.\n\n  🕐 {get_ist_time()}"

    all_trades = trade_journal
    wins = [t for t in all_trades if t.get("result") == "WIN"]
    losses = [t for t in all_trades if t.get("result") == "LOSS"]
    total = len(all_trades)
    win_rate = (len(wins) / total * 100) if total > 0 else 0

    avg_win_pct = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
    avg_loss_pct = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
    gross_profit = sum(t["pnl"] for t in wins if t["pnl"] > 0)
    gross_loss = abs(sum(t["pnl"] for t in losses if t["pnl"] < 0))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0)
    expectancy_pct = (win_rate/100 * avg_win_pct) + ((1 - win_rate/100) * avg_loss_pct)

    r_trades = [t for t in all_trades if t.get("r_multiple") is not None]
    r_wins = [t for t in r_trades if t.get("result") == "WIN"]
    r_losses = [t for t in r_trades if t.get("result") == "LOSS"]
    avg_win_r = sum(t["r_multiple"] for t in r_wins) / len(r_wins) if r_wins else None
    avg_loss_r = sum(t["r_multiple"] for t in r_losses) / len(r_losses) if r_losses else None

    running = 0.0; peak = 0.0; max_dd = 0.0
    for t in all_trades:
        running += t.get("pnl", 0)
        peak = max(peak, running)
        max_dd = min(max_dd, running - peak)
    net_port_pnl = sum(t.get("pnl", 0) for t in all_trades)

    text = f"{_H('EXPECTANCY & PROFIT FACTOR','📊')}\n\n"
    text += f"  🎯 Win Rate    : <b>{win_rate:.1f}%</b>  ({len(wins)}W / {len(losses)}L, {total} trades)\n"
    text += f"  📈 Avg Win     : {avg_win_pct:+.2f}%\n"
    text += f"  📉 Avg Loss    : {avg_loss_pct:+.2f}%\n"
    text += f"  💰 Expectancy  : <b>{expectancy_pct:+.3f}%</b> per trade\n"
    _pf_display = f"{profit_factor:.2f}" if profit_factor != float('inf') else "∞"
    text += f"  ⚖️ Profit Factor: <b>{_pf_display}</b>" + ("  (gross profit ÷ gross loss)\n" if profit_factor != float('inf') else "  (no losses yet)\n")
    text += f"  📉 Max Drawdown: {max_dd:.2f}% (running, port-weighted)\n"
    text += f"  💰 Net PnL     : {fmt_pnl(net_port_pnl)}\n"
    text += f"\n  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    if r_trades:
        text += f"  R-Multiple stats (from {len(r_trades)}/{total} trades with risk data):\n"
        text += f"  📈 Avg Win  : {avg_win_r:+.2f}R\n" if avg_win_r is not None else "  📈 Avg Win  : n/a\n"
        text += f"  📉 Avg Loss : {avg_loss_r:+.2f}R\n" if avg_loss_r is not None else "  📉 Avg Loss : n/a\n"
    else:
        text += f"  R-Multiple stats: no trades with risk data yet — this\n"
        text += f"  tracking started this round, so it builds up going forward.\n"
    text += f"\n  🕐 {get_ist_time()}"
    return text

def get_pattern_stats_text():
    tw=sum(s["wins"] for s in pattern_stats.values())
    tl=sum(s["losses"] for s in pattern_stats.values())
    ts=sum(s["signals"] for s in pattern_stats.values())
    owr=(tw/ts*100) if ts>0 else 0
    tp_=sum(s["total_pnl"] for s in pattern_stats.values())
    text=(f"{_H('PATTERN PERFORMANCE','📈')}\n\n"
          f"  🔢 Signals  : {ts}   ✅ {tw}W  ❌ {tl}L\n"
          f"  🎯 Win Rate : <b>{owr:.1f}%</b>\n"
          f"  💰 Total PnL: {fmt_pnl(tp_)}\n\n"
          f"  ══════════════════════════════\n\n")
    for pat,s in sorted(pattern_stats.items(),key=lambda x:x[1]["signals"],reverse=True):
        if s["signals"]>0:
            wr=(s["wins"]/s["signals"])*100
            filled=int(wr/10); bar="█"*filled+"░"*(10-filled)
            flag="🔴" if wr<40 else "🟡" if wr<60 else "🟢"
            susp="  🔒 SUSP" if is_pattern_suspended(pat) else ""
            w=s.get("weight",1.0); wt="📈" if w>1.05 else "📉" if w<0.95 else "━"
            text+=(f"  {flag} <b>{pat}</b>{susp}\n"
                   f"  [{bar}] {wr:.1f}%  •  {s['signals']} signals  •  {wt}{w:.1f}x\n"
                   f"  {s['wins']}W / {s['losses']}L  •  {fmt_pnl(s['total_pnl'])}\n\n")
    text+=f"  🕐 {get_ist_time()}"
    return text

def get_detailed_summary_text():
    """System Telemetry & Summary — the /summary command's new content."""
    today=datetime.now(IST).date()
    conversion_rate = (radar_coins_triggered / radar_coins_added * 100) if radar_coins_added > 0 else 0
    uptime_secs = time.time() - _bot_start_time
    uptime_h = int(uptime_secs // 3600); uptime_m = int((uptime_secs % 3600) // 60)

    text = f"{_H('SYSTEM TELEMETRY & SUMMARY','⚙️')}\n\n"
    text += f"  🔄 Scan Cycles: <b>{total_scan_cycles}</b>  •  ⏱️ Uptime: <b>{uptime_h}h {uptime_m}m</b>\n"
    text += f"  📡 Radar Conversions: {radar_coins_triggered}/{radar_coins_added} (<b>{conversion_rate:.1f}%</b>)\n"
    text += f"  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

    ow=ol=0; op=0.0; best_pnl=worst_pnl=None; best_ds=worst_ds=""; active_days=0
    for days_ago in range(9,-1,-1):
        day=today-timedelta(days=days_ago)
        dt=[j for j in trade_journal if j.get("date")==str(day)]
        w=sum(1 for t in dt if t["result"]=="WIN"); l=sum(1 for t in dt if t["result"]=="LOSS")
        total=w+l; pnl=sum(t.get("pnl", 0) for t in dt)
        ow+=w; ol+=l; op+=pnl; ds=day.strftime("%d %b")
        if total==0:
            text+=f"  ⚪ <b>{ds}</b>  ──────────  No trades\n"
        else:
            active_days+=1
            em="✅" if w>l else "❌" if l>w else "➖"
            bar="█"*w+"░"*l
            text+=f"  {em} <b>{ds}</b>  [{bar[:8]}]  {w}W/{l}L  {fmt_pnl(pnl)}\n"
            if best_pnl is None or pnl>best_pnl: best_pnl=pnl; best_ds=ds
            if worst_pnl is None or pnl<worst_pnl: worst_pnl=pnl; worst_ds=ds
    ot=ow+ol; owr=(ow/ot*100) if ot>0 else 0
    avg_per_day = op/active_days if active_days>0 else 0.0
    text+=(f"\n  ══════════════════════════════\n"
           f"  ✅ Wins     : {ow}   ❌ Losses  : {ol}\n"
           f"  🎯 Win Rate : <b>{owr:.1f}%</b>\n"
           f"  💰 PnL      : {fmt_pnl(op)}   📊 Avg/Day: {fmt_pnl(avg_per_day)} ({active_days} active day{'s' if active_days!=1 else ''})\n")
    if best_ds:  text+=f"  🏆 Best Day : {best_ds}  ({fmt_pnl(best_pnl)})\n"
    if worst_ds: text+=f"  📉 Worst    : {worst_ds}  ({fmt_pnl(worst_pnl)})\n"
    text+=f"  🕐 {get_ist_time()}"
    return text

def get_streak_text():
    if not trade_journal:
        return f"{_H('STREAK TRACKER','🔥')}\n\n  ⚪ No trades recorded yet."
    st=trade_journal[-1]["result"]; sc=0
    for t in reversed(trade_journal):
        if t["result"]==st: sc+=1
        else: break
    total=len(trade_journal); wins=sum(1 for t in trade_journal if t["result"]=="WIN")
    owr=(wins/total*100) if total>0 else 0
    em="🔥" if st=="WIN" else "❄️"
    bar=(em*min(sc,8)).ljust(8)
    label="WINNING 🏆" if st=="WIN" else "LOSING ⚠️"
    return (f"{_H('STREAK TRACKER','🔥')}\n\n"
            f"  {bar}\n\n"
            f"  Current  : <b>{sc} {label}</b>\n"
            f"  Trades   : {total}\n"
            f"  Win Rate : <b>{owr:.1f}%</b>\n\n"
            f"  🕐 {get_ist_time()}")

def get_best_text():
    if not trade_journal:
        return f"{_H('BEST PERFORMERS','🏆')}\n\n  ⚪ No trade data yet."
    cs={}; ps2={}
    for t in trade_journal:
        c=t["coin"]
        if c not in cs: cs[c]={"W":0,"L":0,"pnl":0.0}
        cs[c]["W" if t["result"]=="WIN" else "L"]+=1; cs[c]["pnl"]+=t["pnl"]
        p=t["pattern"]
        if p not in ps2: ps2[p]={"W":0,"L":0}
        ps2[p]["W" if t["result"]=="WIN" else "L"]+=1
    medals=["🥇","🥈","🥉","🏅","🏅"]
    sc=sorted(cs.items(),key=lambda x:(x[1]["W"]/(x[1]["W"]+x[1]["L"])) if (x[1]["W"]+x[1]["L"])>0 else 0,reverse=True)[:5]
    sp=sorted(ps2.items(),key=lambda x:(x[1]["W"]/(x[1]["W"]+x[1]["L"])) if (x[1]["W"]+x[1]["L"])>0 else 0,reverse=True)[:5]
    text=(f"{_H('BEST PERFORMERS','🏆')}\n\n"
          f"  💰 <b>Top Coins by Win Rate</b>\n\n")
    for i,(c,s) in enumerate(sc):
        tot=s["W"]+s["L"]; wr=(s["W"]/tot*100) if tot>0 else 0
        text+=f"  {medals[i]} <b>{c}</b>  {wr:.1f}% WR  ({tot} trades)  {fmt_pnl(s['pnl'])}\n"
    text+=f"\n  ══════════════════════════════\n\n  🌀 <b>Top Patterns by Win Rate</b>\n\n"
    for i,(p,s) in enumerate(sp):
        tot=s["W"]+s["L"]; wr=(s["W"]/tot*100) if tot>0 else 0
        text+=f"  {medals[i]} <b>{p}</b>  {wr:.1f}%  ({tot} trades)\n"
    text+=f"\n  🕐 {get_ist_time()}"
    return text

def get_risk_text():
    if not active_trades:
        return (f"{_H('RISK MONITOR','🛡️')}\n\n"
                f"  ⚪  No active trades — zero exposure.\n\n"
                f"  🛡️ CB     : {'🔴 ACTIVE' if check_circuit_breaker() else '🟢 OK'}\n"
                f"  📉 Losses : {daily_losses}/{MAX_DAILY_LOSSES}\n"
                f"  🕐 {get_ist_time()}")
    text=f"{_H('RISK MONITOR','🛡️')}\n\n"; total_risk=0.0
    for coin,t in active_trades.items():
        rp=abs(t["entry"]-t["sl"])/t["entry"]*100*t["leverage"]
        tp_pct=abs(t["tp"]-t["entry"])/t["entry"]*100
        sl_pct=abs(t["entry"]-t["sl"])/t["entry"]*100
        total_risk+=rp
        filled=min(int(rp/5),10); bar="█"*filled+"░"*(10-filled)
        em="🔴" if rp>20 else "🟡" if rp>10 else "🟢"
        text+=(f"  {em} <b>{coin}</b>  {t['direction']}  {t['leverage']}x\n"
               f"  [{bar}]  Max loss: <b>{rp:.1f}%</b>\n"
               f"  SL dist: {sl_pct:.2f}%  TP dist: {tp_pct:.2f}%\n\n")
    total_em="🔴" if total_risk>40 else "🟡" if total_risk>20 else "🟢"
    text+=(f"  ══════════════════════════════\n"
           f"  {total_em} Portfolio Risk : <b>{total_risk:.1f}%</b>\n"
           f"  📌 Slots   : {len(active_trades)}/{MAX_ACTIVE_TRADES}\n"
           f"  🛡️ CB      : {'🔴 ACTIVE' if check_circuit_breaker() else '🟢 OK'}\n"
           f"  📉 Losses  : {daily_losses}/{MAX_DAILY_LOSSES}\n"
           f"  ⏳ Pending : {len(pending_signals)}\n"
           f"  🕐 {get_ist_time()}")
    return text

def get_learning_text():
    text=(f"{_H('BOT LEARNING','🧠')}\n\n"
          f"  📊 <b>Market Memory</b>\n\n")
    icons={"bull":"📈","bear":"📉","sideways":"➡️"}
    for cond in ["bull","bear","sideways"]:
        mem=market_memory[cond]; tot=mem["wins"]+mem["losses"]
        wr=(mem["wins"]/tot*100) if tot>0 else 0
        text+=(f"  {icons.get(cond,'')} <b>{cond.capitalize()}</b>   {mem['wins']}W / {mem['losses']}L   {wr:.1f}%\n"
               f"     Best: {mem['best_pattern'] or 'N/A'}\n\n")
    text+=f"  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    if learning_notes:
        text+=f"  💡 <b>Latest Insights</b>\n\n"
        for note in learning_notes[-8:]: text+=f"  ◆ {note}\n"
    else:
        text+=f"  💡 <b>Insights</b>\n\n  ⚪ No insights yet — keeps building as trades close.\n"
    text+=f"\n  🕐 {get_ist_time()}"
    return text

def get_journal_text():
    if not trade_journal:
        return f"{_H('TRADE JOURNAL','📓')}\n\n  ⚪ No trades recorded yet."
    recent=trade_journal[-10:][::-1]
    text=f"{_H('TRADE JOURNAL  (Last 10)','📓')}\n\n"
    for t in recent:
        em="✅" if t.get("result")=="WIN" else "🔴"
        dirn_em="🟢" if t.get("direction")=="BUY" else "🔴"
        text+=(f"  {em} <b>{t.get('coin','?')}</b>  {dirn_em} {t.get('direction','?')}\n"
               f"  ◆ {t.get('pattern','?')}\n"
               f"  💰 {fmt_pnl(t.get('pnl',0))}  ⏱️ {t.get('duration','?')}  📅 {t.get('date','?')}\n\n")
    total=len(trade_journal); wins=sum(1 for t in trade_journal if t.get("result")=="WIN")
    wr=(wins/total*100) if total>0 else 0
    text+=(f"  ══════════════════════════════\n"
           f"  Total: {total}   Win Rate: <b>{wr:.1f}%</b>\n"
           f"  🕐 {get_ist_time()}")
    return text

def get_patterns_ranked_text():
    text=f"{_H('ALL PATTERNS RANKED','🌀')}\n\n"
    all_pats=[]
    for pat,s in pattern_stats.items():
        sigs=s.get("signals",0); wr=(s["wins"]/sigs*100) if sigs>0 else 0
        w=s.get("weight",1.0); adj=get_adjusted_score(pat,80,"bull")
        all_pats.append((pat,sigs,wr,w,adj))
    all_pats.sort(key=lambda x:x[4],reverse=True)
    medal_list=["🥇","🥈","🥉"]
    for i,(pat,sigs,wr,w,adj) in enumerate(all_pats):
        medal=medal_list[i] if i<len(medal_list) else f"{i+1}."
        flag="🔴" if wr<40 and sigs>=5 else "🟢" if wr>=60 else "🟡"
        susp="  🔒" if is_pattern_suspended(pat) else ""
        wt="📈" if w>1.05 else "📉" if w<0.95 else "━"
        filled=int(wr/10); bar="█"*filled+"░"*(10-filled)
        if sigs==0:
            text+=f"  {medal} <b>{pat}</b>{susp}  <i>(no trades yet)</i>\n\n"
        else:
            text+=(f"  {medal} <b>{pat}</b>{susp}\n"
                   f"  {flag} [{bar}] {wr:.1f}%\n"
                   f"  {sigs} trades · {wt}{w:.2f}x · Adj:{adj:.1f}\n\n")
    if not all_pats:
        text+="  ⚪ No pattern data yet.\n"
    text+=f"  🕐 {get_ist_time()}"
    return text

def get_trend_label(ema20,ema50,price,label):
    if not ema20 or not ema50: return "Neutral"
    diff_pct=((ema20-ema50)/ema50)*100
    if price>ema20>ema50:
        if diff_pct>3:   return "Strong Uptrend"
        elif diff_pct>1: return "Uptrend"
        else:            return "Weak Uptrend"
    elif price<ema20<ema50:
        if diff_pct<-3:  return "Strong Downtrend"
        elif diff_pct<-1:return "Downtrend"
        else:            return "Weak Downtrend"
    elif price>ema50: return "Ranging Above EMA50"
    else:             return "Ranging Below EMA50"

def cmd_trend(coin_input):
    coin=coin_input.upper().replace("USDT","").strip()
    symbol=coin+"USDT"; price=get_price(symbol)
    if not price:
        return f"{_H(f'TREND  {coin}','📉')}\n\n  ❌ Could not fetch price for <b>{coin}</b>."
    tfs=[("1d","Daily"),("4h","4 Hour"),("1h","1 Hour"),("15m","15 Min")]
    results=[]; bull_c=bear_c=0
    for tf,label in tfs:
        klines=get_klines(symbol,tf,60)
        if not klines or len(klines)<50: results.append((label,"No data",50,0)); continue
        closes=[float(k[4]) for k in klines]
        e20=calculate_ema(closes,20); e50=calculate_ema(closes,50)
        rsi=calculate_rsi(closes); adx=calculate_adx(klines)
        trend=get_trend_label(e20,e50,price,label)
        if "Uptrend" in trend:   bull_c+=1
        if "Downtrend" in trend: bear_c+=1
        results.append((label,trend,rsi,adx))
    if bull_c>=3:   bias="STRONGLY BULLISH 🚀"; bias_em="🟢"
    elif bull_c>=2: bias="BULLISH 📈";           bias_em="🟢"
    elif bear_c>=3: bias="STRONGLY BEARISH 🔻"; bias_em="🔴"
    elif bear_c>=2: bias="BEARISH 📉";           bias_em="🔴"
    else:           bias="MIXED / SIDEWAYS ➡️"; bias_em="🟡"
    klines_4h=get_klines(symbol,"4h",30); s1=r1=0
    if klines_4h and len(klines_4h)>=5:
        highs=[float(k[2]) for k in klines_4h]; lows=[float(k[3]) for k in klines_4h]
        c4=[float(k[4]) for k in klines_4h]
        pivot=(highs[-2]+lows[-2]+c4[-2])/3
        r1=2*pivot-lows[-2]; s1=2*pivot-highs[-2]
    rsi_1h=results[2][2] if len(results)>2 else 50
    adx_1h=results[2][3] if len(results)>2 else 0
    text=(f"{_H(f'TREND ANALYSIS  {coin}','📉')}\n\n"
          f"  💰 Price  : <code>{format_price(price)}</code>\n"
          f"  {bias_em} Bias   : <b>{bias}</b>\n\n"
          f"  ┌── TIMEFRAMES ───────────────┐\n")
    for label,trend,rsi,adx in results:
        em="🟢" if "Up" in trend else "🔴" if "Down" in trend else "🟡"
        text+=f"  │  {em} <b>{label:<8}</b> {trend}\n"
    text+=(f"  └─────────────────────────────┘\n\n"
           f"  ┌── KEY LEVELS ───────────────┐\n"
           f"  │  🎯 Resistance : <code>{format_price(r1)}</code>\n"
           f"  │  🛡️ Support    : <code>{format_price(s1)}</code>\n"
           f"  │  📊 RSI(1h)   : {rsi_1h:.1f}   ADX: {adx_1h:.1f}\n"
           f"  └─────────────────────────────┘\n\n"
           f"  🕐 {get_ist_time()}")
    return text

DESK_REPORT_COINS = ["BTC","ETH","SOL","HYPE","BERA","IP"]

def send_pressure_cooker_report():
    """The "Pressure Cooker" Report — the scheduled watchlist alert. Chosen"""
    pending = {c: w for c, w in retest_watchlist.items() if w.get("status", "PENDING") == "PENDING"}
    if not pending:
        return
    lines = [f"👀 <b>PRESSURE COOKER REPORT</b>", f"⚙️ <b>{len(pending)} coin(s) on the radar</b>", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", ""]
    for coin, w in sorted(pending.items(), key=lambda kv: kv[1]["logged_at"], reverse=True):
        age_hrs = (get_ist_datetime() - w["logged_at"]).total_seconds() / 3600
        dir_icon = "🟢" if w["direction"] == "BUY" else "🔴"
        reason = "waiting for pullback to breakout line" if w.get("pattern_type") == "bos_retest" else "move already extended, watching for retest"
        lines.append(f"{dir_icon} <b>{coin}</b>  {w['direction']}")
        lines.append(f"   📌 {w['pattern']}")
        lines.append(f"   📍 Level: <code>{format_price(w['level'])}</code>  •  {reason}")
        lines.append(f"   ⏱️ On radar {age_hrs:.1f}h (expires at 12h)")
        lines.append("")
    lines.append(f"🕐 {get_ist_time()}")
    send_telegram("\n".join(lines))

def send_8h_ai_desk_report():
    """Point 4: The 8-Hour VIP "Prop-Desk" AI Report (retimed from 4h to 8h per user request)."""
    if not AI_REVIEW_ENABLED or not ANTHROPIC_API_KEY:
        logger.info("send_8h_ai_desk_report: AI review disabled or key not set, skipping")
        return

    coin_summaries = []
    ready_candidates = []
    for coin in DESK_REPORT_COINS:
        symbol = coin + "USDT"
        price = get_price(symbol)
        if not price:
            coin_summaries.append(f"{coin}: price unavailable, skipping")
            continue
        klines_4h = get_klines(symbol, "4h", 50)
        klines_15m = get_klines(symbol, "15m", 50)
        if not klines_4h or len(klines_4h) < 30 or not klines_15m or len(klines_15m) < 30:
            coin_summaries.append(f"{coin}: insufficient chart data, skipping")
            continue

        closes_4h = [float(k[4]) for k in klines_4h]
        e20_4h = calculate_ema(closes_4h, 20); e50_4h = calculate_ema(closes_4h, 50)
        trend_4h = "BULLISH" if (e20_4h and e50_4h and e20_4h > e50_4h) else "BEARISH" if (e20_4h and e50_4h) else "UNCLEAR"
        adx_4h = calculate_adx(klines_4h)

        closes_15m = [float(k[4]) for k in klines_15m]
        rsi_15m = calculate_rsi(closes_15m)
        ms_15m = detect_market_structure(klines_15m)
        vcp_dir, vcp_tightness = detect_volatility_contraction(closes_15m,
            [float(k[2]) for k in klines_15m], [float(k[3]) for k in klines_15m],
            [float(k[5]) for k in klines_15m], price)
        zones = get_htf_zones(symbol)
        zone_ok_buy, zone_label_buy = is_in_zone(price, "BUY", zones)
        zone_ok_sell, zone_label_sell = is_in_zone(price, "SELL", zones)
        zone_note = (f"in demand zone {zone_label_buy}" if zone_ok_buy else
                     f"in supply zone {zone_label_sell}" if zone_ok_sell else "no zone tap")

        coin_summaries.append(
            f"{coin}: price {format_price(price)} | 4H trend:{trend_4h} ADX:{adx_4h:.0f} | "
            f"15m RSI:{rsi_15m:.0f} structure:{ms_15m['bias']}{' +ChoCh' if ms_15m['choch'] else ''}"
            f"{' +BOS' if ms_15m['bos'] else ''} | {zone_note}"
            f"{' | coiling (VCP)' if vcp_dir else ''}"
        )

    if not coin_summaries:
        logger.warning("send_8h_ai_desk_report: no coin data available, skipping")
        return

    btc_price_desk = get_price("BTCUSDT")
    btc_klines_desk = get_klines("BTCUSDT", "1h", 60)
    market_regime = detect_market_condition(btc_price_desk, btc_klines_desk) if btc_price_desk and btc_klines_desk else "sideways"
    regime_label = {"bull":"BULLISH 📈","bear":"BEARISH 📉","sideways":"SIDEWAYS ➡️"}.get(market_regime, "UNKNOWN")

    prompt = (
        "You are running the 8-hour desk check for a proprietary trading desk, reviewing "
        "a fixed watchlist top-down: 4-Hour macro structure first, then 15-minute entry timing.\n\n"
        "WATCHLIST:\n" + "\n".join(coin_summaries) + "\n\n"
        "For EACH coin with data, give a one-line read: what's the macro bias, and is anything "
        "actionable forming on the 15m (zone tap, ChoCh, coiling, clean structure)? Be direct, "
        "like a real trader's desk note, not a generic summary.\n"
        "Format EXACTLY like this per coin:\n"
        "COIN: [read] — [1 short sentence]\n\n"
        "Then, if and ONLY if a coin genuinely looks ready to execute RIGHT NOW (not just "
        "'watching', an actual clean entry), add this exact line for each one:\n"
        "READY: COIN — [why, 1 sentence]\n"
        "If nothing is ready, omit the READY lines entirely — do not force one."
    )

    try:
        res = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key":ANTHROPIC_API_KEY,"anthropic-version":"2023-06-01",
                     "content-type":"application/json"},
            json={"model":"claude-haiku-4-5-20251001","max_tokens":600,
                  "messages":[{"role":"user","content":prompt}]},
            timeout=25
        )
        if res.status_code != 200:
            logger.warning(f"send_8h_ai_desk_report: API returned {res.status_code}")
            return
        text = res.json()["content"][0]["text"].strip()
    except Exception as e:
        logger.warning(f"send_8h_ai_desk_report: {e}")
        return

    ready_lines = []
    report_lines = []
    for line in text.split("\n"):
        line = line.strip()
        if not line: continue
        if line.upper().startswith("READY:"):
            ready_lines.append(line)
        else:
            report_lines.append(line)

    msg = f"{_H('8H PROP-DESK REPORT','🏦')}\n\n"
    msg += f"  ₿ BTC Trend: <b>{regime_label}</b>\n\n"
    for line in report_lines:
        if ":" in line:
            coin_part, rest = line.split(":", 1)
            msg += f"  🔹 <b>{coin_part.strip()}</b>:{rest}\n"
    msg += f"\n  🕐 {get_ist_time()}"
    send_telegram(msg)
    logger.info(f"8h desk report sent, {len(ready_lines)} ready candidate(s)")

    if ready_lines:
        ping_msg = f"{_H('⚡ DESK ALERT — TRADE READY','🚨')}\n\n"
        for line in ready_lines:
            _, rest = line.split(":", 1) if ":" in line else ("", line)
            ping_msg += f"  🎯 {rest.strip()}\n"
        ping_msg += f"\n  Check the chart now — this may be your entry.\n  🕐 {get_ist_time()}"
        send_telegram(ping_msg)


def ai_analyst_review():
    """AI Analyst — reviews ALL active trades using Claude, like a portfolio manager."""
    if not active_trades:
        return f"{_H('AI ANALYST','🧠')}\n\n  🌙 No active trades to review.\n\n  🕐 {get_ist_time()}"
    if not AI_REVIEW_ENABLED:
        return f"{_H('AI ANALYST','🧠')}\n\n  ⚠️ AI review is currently disabled (AI_REVIEW_ENABLED=false) — turn it back on in Railway Variables to use this.\n\n  🕐 {get_ist_time()}"
    if not ANTHROPIC_API_KEY:
        return f"{_H('AI ANALYST','🧠')}\n\n  ⚠️ ANTHROPIC_API_KEY not set — AI Analyst unavailable.\n\n  🕐 {get_ist_time()}"

    trades_summary=[]
    for coin,t in active_trades.items():
        symbol=t.get("symbol",coin+"USDT")
        price=get_price(symbol)
        if not price: continue
        direction=t.get("direction","BUY"); entry=t["entry"]
        tp=t["tp"]; sl=t["sl"]; lev=t.get("leverage",1)
        if direction=="BUY": pnl=((price-entry)/entry)*100*lev
        else:                pnl=((entry-price)/entry)*100*lev
        klines=get_klines(symbol,"15m",30)
        rsi=calculate_rsi([float(k[4]) for k in klines]) if klines else 50
        adx=calculate_adx(klines) if klines else 20
        dist_tp=abs(tp-price)/price*100
        dist_sl=abs(price-sl)/price*100
        trades_summary.append(
            f"{coin}: {direction} | Entry:{format_price(entry)} Now:{format_price(price)} "
            f"PnL:{pnl:+.1f}% | TP:{dist_tp:.1f}% away SL:{dist_sl:.1f}% away | "
            f"RSI:{rsi:.0f} ADX:{adx:.0f} | Pattern:{t.get('pattern','?')}"
        )

    if not trades_summary:
        return f"{_H('AI ANALYST','🧠')}\n\n  ⚠️ Could not fetch live prices.\n\n  🕐 {get_ist_time()}"

    prompt = (
        "You are a professional portfolio manager reviewing open crypto futures positions.\n\n"
        "OPEN TRADES:\n" + "\n".join(trades_summary) + "\n\n"
        "For EACH trade, give a one-line action: HOLD, TAKE PROFIT NOW, EXIT NOW (cut loss), "
        "or WATCH CLOSELY (risk building). Base it on PnL, distance to TP/SL, RSI, and ADX.\n"
        "Format EXACTLY like this per trade:\n"
        "COIN: ACTION — short reason (max 12 words)\n\n"
        "Then add one line: OVERALL: [1 sentence portfolio-level insight]"
    )

    try:
        res = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key":ANTHROPIC_API_KEY,"anthropic-version":"2023-06-01",
                     "content-type":"application/json"},
            json={"model":"claude-haiku-4-5-20251001","max_tokens":400,
                  "messages":[{"role":"user","content":prompt}]},
            timeout=20
        )
        if res.status_code!=200:
            return f"{_H('AI ANALYST','🧠')}\n\n  ⚠️ AI request failed.\n\n  🕐 {get_ist_time()}"
        text = res.json()["content"][0]["text"].strip()
    except Exception as e:
        return f"{_H('AI ANALYST','🧠')}\n\n  ⚠️ Error: {e}\n\n  🕐 {get_ist_time()}"

    msg = f"{_H('AI ANALYST — PORTFOLIO REVIEW','🧠')}\n\n"
    for line in text.split("\n"):
        line=line.strip()
        if not line: continue
        if line.upper().startswith("OVERALL:"):
            msg += f"\n  📌 <b>{line}</b>\n"
        elif ":" in line:
            coin_part, rest = line.split(":",1)
            em = "🟢" if "HOLD" in rest.upper() else "✅" if "TAKE PROFIT" in rest.upper() else "🔴" if "EXIT" in rest.upper() else "⚠️"
            msg += f"  {em} <b>{coin_part.strip()}</b>:{rest}\n"
    msg += f"\n  🕐 {get_ist_time()}"
    return msg


def cmd_market():
    btc=get_price("BTCUSDT"); eth=get_price("ETHUSDT"); sol=get_price("SOLUSDT")
    bnb=get_price("BNBUSDT"); xrp=get_price("XRPUSDT")
    btc_klines=get_klines("BTCUSDT","1h",50); btc_trend="N/A"
    if btc_klines and len(btc_klines)>=50:
        closes=[float(k[4]) for k in btc_klines]
        e20=calculate_ema(closes,20); e50=calculate_ema(closes,50)
        btc_trend=get_trend_label(e20,e50,btc,"1h") if btc else "N/A"
    scan_list=["BTC","ETH","BNB","SOL","XRP","ADA","AVAX","DOT","LINK","NEAR",
               "INJ","SUI","APT","ARB","OP","ATOM","PEPE","WIF","BONK","DOGE"]
    gainers=[]; losers=[]
    for coin in scan_list:
        try:
            klines=get_klines(coin+"USDT","1d",3)
            if klines and len(klines)>=2:
                prev=float(klines[-2][4]); curr=float(klines[-1][4])
                chg=((curr-prev)/prev)*100 if prev>0 else 0
                if chg>0: gainers.append((coin,chg))
                else:     losers.append((coin,chg))
        except Exception: continue
    gainers.sort(key=lambda x:x[1],reverse=True)
    losers.sort(key=lambda x:x[1])
    fng=get_fear_greed_index()
    fng_lbl=("Extreme Fear 😨" if fng<=25 else "Fear 😟" if fng<=45 else
             "Neutral 😐" if fng<=55 else "Greed 😊" if fng<=75 else "Extreme Greed 🤑")
    fng_bar="█"*min(int(fng/10),10)+"░"*(10-min(int(fng/10),10))
    fng_em="🔴" if fng<=25 else "🟠" if fng<=45 else "🟡" if fng<=55 else "🟢"
    bt_em="🟢" if "Up" in btc_trend else "🔴" if "Down" in btc_trend else "🟡"
    text=(f"{_H('MARKET OVERVIEW','🌍')}\n\n"
          f"  {fng_em} <b>Fear & Greed: {fng} — {fng_lbl}</b>\n"
          f"  [{fng_bar}]\n\n"
          f"  ┌── LIVE PRICES ──────────────┐\n")
    for sym,lbl,p in [("BTC","₿  BTC",btc),("ETH","Ξ  ETH",eth),
                       ("SOL","◎  SOL",sol),("BNB","◈  BNB",bnb),("XRP","✦  XRP",xrp)]:
        if p: text+=f"  │  {lbl}  <code>${format_price(p)}</code>\n"
    text+=(f"  │\n"
           f"  │  {bt_em} BTC Trend: {btc_trend}\n"
           f"  └─────────────────────────────┘\n\n")
    text+=f"  🚀 <b>Top Gainers 24h</b>\n"
    for coin,chg in gainers[:5]:
        bar="▓"*min(int(abs(chg)/2),8)
        text+=f"  🟢 <b>{coin:<6}</b> +{chg:.2f}%  {bar}\n"
    text+=f"\n  📉 <b>Top Losers 24h</b>\n"
    for coin,chg in losers[:5]:
        bar="░"*min(int(abs(chg)/2),8)
        text+=f"  🔴 <b>{coin:<6}</b> {chg:.2f}%  {bar}\n"
    text+=f"\n  🕐 {get_ist_time()}"
    return text

def cmd_compare(coins_str):
    coins=[c.upper().replace("USDT","") for c in coins_str.split()[:4]]
    if not coins: return f"{_H('COIN COMPARE','🆚')}\n\n  Usage: /compare BTC ETH SOL"
    text=f"{_H('COIN COMPARE','🆚')}\n\n"
    for coin in coins:
        symbol=coin+"USDT"; price=get_price(symbol)
        if not price: text+=f"  ❌ <b>{coin}</b> — Not found\n\n"; continue
        klines=get_klines(symbol,"4h",60); trend="N/A"; rsi=50.0; adx=0.0
        if klines and len(klines)>=50:
            closes=[float(k[4]) for k in klines]
            e20=calculate_ema(closes,20); e50=calculate_ema(closes,50)
            rsi=calculate_rsi(closes); adx=calculate_adx(klines)
            trend=get_trend_label(e20,e50,price,"4h")
        em="🟢" if "Up" in trend else "🔴" if "Down" in trend else "🟡"
        rsi_em="🔴" if rsi>70 else "🟢" if rsi<30 else "🟡"
        text+=(f"  {em} <b>{coin}</b>  <code>{format_price(price)}</code>\n"
               f"  Trend: {trend}\n"
               f"  RSI: {rsi_em} {rsi:.1f}   ADX: {adx:.1f}\n\n")
    text+=f"  🕐 {get_ist_time()}"
    return text

def cmd_scan_manual(btc_trend,fng,market_condition):
    send_telegram(
        f"{_H('SCANNING NOW','🔍')}\n\n"
        f"  ⚙️ Scanning {len(COINS)} coins...\n"
        f"  📊 Market: {market_condition.upper()}  F&G: {fng}\n"
        f"  🕐 {get_ist_time()}"
    )
    results=[]
    for coin in COINS:
        try:
            symbol=coin+"USDT"; price=get_price(symbol); klines=get_klines(symbol,"15m",100)
            if not price or not klines: continue
            found=detect_patterns(symbol,klines,price,btc_trend)
            if not found: continue
            scored=get_all_pattern_scores(found,market_condition)
            if not scored: continue
            best=scored[0]; adj_score=min(best[1]+min(len(scored)*0.5,3),99)
            tf_score=get_timeframe_score(symbol,best[2])
            if tf_score==-1: continue
            results.append({"coin":coin,"direction":best[2],"score":adj_score,
                            "pattern":best[0],"tf_score":tf_score})
        except Exception: continue
        time.sleep(0.1)
    if not results:
        return (f"{_H('SCAN RESULTS','🔍')}\n\n"
                f"  ⚪ No qualifying setups found right now.\n\n"
                f"  📊 Market: {market_condition.upper()}   F&G: {fng}\n"
                f"  🕐 {get_ist_time()}")
    results.sort(key=lambda x:x["score"],reverse=True)
    text=f"{_H(f'SCAN RESULTS  ({len(results)} found)','🔍')}\n\n"
    for r in results[:5]:
        em="🟢" if r["direction"]=="BUY" else "🔴"
        dir_arrow="▲ LONG" if r["direction"]=="BUY" else "▼ SHORT"
        tf="⭐⭐" if r["tf_score"]==3 else "⭐" if r["tf_score"]==2 else "◆"
        filled=min(int(r["score"]/10),10); bar="█"*filled+"░"*(10-filled)
        text+=(f"  {em} <b>{r['coin']}</b>  {dir_arrow}  {tf}\n"
               f"  [{bar}] {r['score']:.1f}\n"
               f"  ◆ {r['pattern']}\n\n")
    text+=(f"  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
           f"  📊 {market_condition.upper()}   F&G: {fng}\n"
           f"  🕐 {get_ist_time()}")
    return text

def cmd_hidden_gems():
    """💎 Hidden Gems Scanner"""
    send_telegram(
        f"{_H('SCANNING FOR HIDDEN GEMS','💎')}\n\n"
        f"  ⚙️ Analysing {len(COINS)} coins...\n"
        f"  🔍 Looking for volume spikes + early momentum\n"
        f"  🕐 {get_ist_time()}"
    )
    gems = []; vol_spikes = []; unpumped = []; early_mom = []
    for coin in COINS:
        try:
            symbol = coin + "USDT"
            price  = get_price(symbol)
            if not price: continue
            klines = get_klines(symbol, "1h", 50)
            if not klines or len(klines) < 30: continue
            closes = [float(k[4]) for k in klines]
            highs  = [float(k[2]) for k in klines]
            lows   = [float(k[3]) for k in klines]
            vols   = [float(k[5]) for k in klines]
            vol_ratio  = get_volume_ratio(klines)
            rsi        = calculate_rsi(closes)
            ema20      = calculate_ema(closes, 20)
            ema50      = calculate_ema(closes, 50)
            chg_24h = ((closes[-1] - closes[-24]) / closes[-24] * 100) if len(closes) >= 24 else 0
            recent_low  = min(lows[-48:])
            dist_low_pct = ((price - recent_low) / recent_low * 100) if recent_low > 0 else 999
            if vol_ratio >= 2.0 and closes[-1] > closes[-2] and chg_24h < 15:
                vol_spikes.append({
                    "coin": coin, "vol_ratio": vol_ratio,
                    "price": price, "chg_24h": chg_24h, "rsi": rsi
                })
            if dist_low_pct < 8 and vol_ratio >= 1.3 and 35 <= rsi <= 58:
                unpumped.append({
                    "coin": coin, "dist_low": dist_low_pct,
                    "price": price, "vol_ratio": vol_ratio, "rsi": rsi
                })
            if ema20 and ema50 and ema20 > ema50 and 45 <= rsi <= 65 and chg_24h > 1 and vol_ratio >= 1.2:
                early_mom.append({
                    "coin": coin, "rsi": rsi,
                    "price": price, "chg_24h": chg_24h, "vol_ratio": vol_ratio
                })
            time.sleep(0.1)
        except Exception: continue

    vol_spikes.sort(key=lambda x: x["vol_ratio"], reverse=True)
    unpumped.sort(key=lambda x: x["dist_low"])
    early_mom.sort(key=lambda x: x["rsi"])

    msg = f"{_H('HIDDEN GEMS REPORT','💎')}\n\n"

    msg += f"  🚀 <b>Volume Spikes</b>  <i>(sudden activity)</i>\n"
    if vol_spikes:
        for g in vol_spikes[:5]:
            bar = "█" * min(int(g["vol_ratio"]), 8)
            msg += (f"  🔹 <b>{g['coin']}</b>  <code>{format_price(g['price'])}</code>\n"
                    f"      Vol: {bar} {g['vol_ratio']:.1f}x avg  •  24h: {g['chg_24h']:+.1f}%  •  RSI:{g['rsi']:.0f}\n\n")
    else:
        msg += "  ⚪ No volume spikes right now.\n\n"

    msg += f"  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

    msg += f"  💤 <b>Not Yet Pumped</b>  <i>(near lows, vol building)</i>\n"
    if unpumped:
        for g in unpumped[:5]:
            msg += (f"  🔹 <b>{g['coin']}</b>  <code>{format_price(g['price'])}</code>\n"
                    f"      {g['dist_low']:.1f}% above low  •  Vol:{g['vol_ratio']:.1f}x  •  RSI:{g['rsi']:.0f}\n\n")
    else:
        msg += "  ⚪ No unpumped coins found.\n\n"

    msg += f"  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

    msg += f"  📈 <b>Early Momentum</b>  <i>(EMA cross + rising RSI)</i>\n"
    if early_mom:
        for g in early_mom[:5]:
            msg += (f"  🔹 <b>{g['coin']}</b>  <code>{format_price(g['price'])}</code>\n"
                    f"      24h: {g['chg_24h']:+.1f}%  •  Vol:{g['vol_ratio']:.1f}x  •  RSI:{g['rsi']:.0f}\n\n")
    else:
        msg += "  ⚪ No early momentum coins found.\n\n"

    total = len(set([g["coin"] for g in vol_spikes+unpumped+early_mom]))
    msg += (f"  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"  💎 {total} potential gems found\n")

    candidate_coins = list(dict.fromkeys(
        [g["coin"] for g in vol_spikes] + [g["coin"] for g in unpumped] + [g["coin"] for g in early_mom]
    ))
    best=None
    btc_p=get_price("BTCUSDT"); btc_k=get_klines("BTCUSDT","1h",50)
    bt_e=calculate_ema([float(x[4]) for x in btc_k],50) if btc_k else None
    btc_trend=1 if (btc_p and bt_e and btc_p>bt_e) else -1
    mc = detect_market_condition(btc_p,btc_k) if btc_p and btc_k else "sideways"
    for coin in candidate_coins[:25]:
        try:
            symbol=coin+"USDT"; price=get_price(symbol)
            klines=get_klines(symbol,"15m",100)
            if not price or not klines or len(klines)<50: continue
            found=detect_patterns(symbol,klines,price,btc_trend)
            if not found: continue
            scored=get_all_pattern_scores(found,mc)
            if not scored: continue
            top=scored[0]; adj_score=min(top[1]+min(len(scored)*0.5,3),99)
            if adj_score<MIN_SETUP_SCORE: continue
            tf_score=get_timeframe_score(symbol,top[2])
            if tf_score==-1: continue
            if best is None or adj_score>best["score"]:
                best={"coin":coin,"symbol":symbol,"price":price,"klines":klines,
                      "direction":top[2],"pattern":top[0],"score":adj_score,"tf_score":tf_score}
        except Exception: continue
        time.sleep(0.05)

    if best:
        klines_15m=best["klines"]; entry=best["price"]
        atr_1h_klines=get_klines(best["symbol"],"1h",30)
        atr_1h=calculate_atr(atr_1h_klines) if atr_1h_klines else calculate_atr(klines_15m)
        atr_pct=(atr_1h/entry)*100 if entry>0 else 0
        sl=get_structure_sl(klines_15m,best["direction"],entry,atr_1h)
        sl_dist=abs(entry-sl)
        atr_tp_dist=atr_1h*ATR_TP_MULTIPLIER
        min_rr_tp_dist=sl_dist*MIN_RR_RATIO
        gem_zones=get_htf_zones(best["symbol"])
        structural_tp_gem=get_structural_tp(entry,best["direction"],gem_zones,min_rr_tp_dist)
        if structural_tp_gem is not None:
            tp=structural_tp_gem
        else:
            tp_dist=max(atr_tp_dist,min_rr_tp_dist)
            tp=entry+tp_dist if best["direction"]=="BUY" else entry-tp_dist
        ms_b=detect_market_structure(klines_15m)
        vol_ratio_gem=get_volume_ratio(klines_15m)
        oi_rising=get_oi_trend(best["symbol"])
        adx_val=calculate_adx(klines_15m)
        closes=[float(k[4]) for k in klines_15m]
        rsi_val=calculate_rsi(closes)
        vol_ok=is_volume_confirmed(klines_15m)
        rsi_ok=35<=rsi_val<=65 if best["direction"]=="BUY" else 35<=rsi_val<=65
        funding_ok=True
        vwap=calculate_vwap(klines_15m); vwap_ok=(entry>vwap if best["direction"]=="BUY" else entry<vwap) if vwap else False
        st_15m=calculate_supertrend(klines_15m,ST_PERIOD,ST_MULTIPLIER)
        st_ok=(st_15m==best["direction"])
        zone_ok=False
        btc_aligned_gem,_=is_btc_aligned(best["direction"])
        grade,pts,_=get_signal_grade(best["score"],vol_ratio_gem,oi_rising,best["tf_score"],vol_ok,rsi_ok,funding_ok,st_ok,vwap_ok,zone_ok,adx_val,btc_aligned_gem,ms_b["bias"],ms_b["bos"])
        lev=get_smart_leverage(best["symbol"],atr_pct,best["score"],grade)
        profit_target=(abs(tp-entry)/entry)*100*lev
        sl_pct=abs(entry-sl)/entry*100; tp_pct=abs(tp-entry)/entry*100
        rr=tp_pct/sl_pct if sl_pct>0 else 0
        dir_arrow="🟢 LONG ▲" if best["direction"]=="BUY" else "🔴 SHORT ▼"
        grade_em="🏆" if "A+" in grade else "🍀" if " A" in grade else "🥈" if "B" in grade else "🥉"
        msg += (f"\n  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"  ⭐ <b>BEST PICK RIGHT NOW</b>  {grade_em}\n"
                f"  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"  🪙 <b>{best['coin']}</b>  {dir_arrow}  ✦ {lev}x\n"
                f"  {grade_em} {grade}  •  Score {best['score']:.0f}/100\n"
                f"  📌 {best['pattern']}\n\n"
                f"  💰 Entry  : <code>{format_price(entry)}</code>\n"
                f"  🎯 Target : <code>{format_price(tp)}</code>  +{tp_pct:.2f}%\n"
                f"  🛑 Stop   : <code>{format_price(sl)}</code>  -{sl_pct:.2f}%\n"
                f"  ⚖️ RR 1:{rr:.1f}  •  📈 Max Profit +{profit_target:.1f}%\n\n"
                f"  💡 Type <code>/trend {best['coin']}</code> to confirm before entering.\n")
    else:
        msg += f"\n  ⭐ <b>BEST PICK</b>: No setup ≥{MIN_SETUP_SCORE} found among gems right now.\n"

    msg += (f"  ⚠️ <i>Always confirm before trading</i>\n"
            f"  🕐 {get_ist_time()}")
    return msg

def ai_analyze_setup(coin, direction, klines, price, pattern, rsi_val, adx_val, vol_strength, is_volatile=False, penalty_notes=None, htf_4h_trend=None, zone_ok=False, zone_label="", ms_bos=False, ms_choch=False, ms_bias=None, is_sweep=False, sl_pct=None, rr_ratio=None, hist_wr=None, hist_signals=0):
    """The Human Narrative upgrade: Claude previously only saw 20 raw 15m"""
    if not AI_REVIEW_ENABLED or not ANTHROPIC_API_KEY: return None
    try:
        recent=klines[-20:]
        candle_desc=[]
        for i,k in enumerate(recent):
            o,h,l,c=float(k[1]),float(k[2]),float(k[3]),float(k[4])
            body=abs(c-o); rng=h-l if h>l else 0.0001
            lower_wick=(min(o,c)-l)/rng*100
            upper_wick=(h-max(o,c))/rng*100
            ctype="BULL" if c>o else "BEAR"
            strength="strong" if body/rng>0.6 else "weak" if body/rng<0.3 else "normal"
            candle_desc.append(f"C{i+1}:{ctype} {strength} low_wick={lower_wick:.0f}% up_wick={upper_wick:.0f}%")
        dir_word="LONG (BUY)" if direction=="BUY" else "SHORT (SELL)"
        vol_note = "Volatility is currently ELEVATED vs normal — could mean a real breakout OR just chop. Judge from candle quality." if is_volatile else "Volatility is normal."
        penalty_line = f"Note: scanner flagged secondary weakness — {', '.join(penalty_notes)}. Weigh this against price action quality.\n" if penalty_notes else ""

        htf_desc = {1:"BULLISH",-1:"BEARISH",0:"NEUTRAL/UNCLEAR",None:"UNKNOWN"}.get(htf_4h_trend,"UNKNOWN")
        zone_line = f"We are currently sitting INSIDE a {'Demand' if direction=='BUY' else 'Supply'} zone ({zone_label})." if zone_ok else "Price is NOT inside a known Supply/Demand zone right now — no man's land."
        if ms_choch and zone_ok:
            shift_line = "A Change of Character (ChoCh) just fired INSIDE this zone — the market just reversed structure exactly at a key level. This is the strongest possible setup type."
        elif ms_choch:
            shift_line = "A Change of Character (ChoCh) just fired, but NOT inside a known zone — a real structure shift, though without the location confirmation."
        elif ms_bos:
            shift_line = f"A Break of Structure (BOS) just confirmed, structure bias is {ms_bias or 'unclear'}."
        else:
            shift_line = f"No fresh structure break yet — current bias reads {ms_bias or 'neutral'}."

        narrative = (
            f"THE NARRATIVE (read this first, the way a trader scans top-down):\n"
            + (f"- 🚨 A LIQUIDITY SWEEP just occurred! Price pierced a key structural "
               f"level to trap retail stop-losses and reversed.\n" if is_sweep else "")
            + f"- 4-Hour trend: {htf_desc}.\n"
            f"- {zone_line}\n"
            f"- {shift_line}\n"
            f"- On the 15-minute chart, the scanner flagged: {pattern}.\n"
            + (f"- DATA-DRIVEN PROBABILITY: this pattern has historically won "
               f"{hist_wr:.0f}% of the time over {hist_signals} tracked signals. "
               f"Weigh this real track record against what you see in the candles — "
               f"a clean-looking setup on a historically weak pattern deserves more "
               f"skepticism, and vice versa.\n" if hist_wr is not None
               else "- DATA-DRIVEN PROBABILITY: not enough tracked history for this "
                    "pattern yet to have a reliable win rate — judge on price action alone.\n")
            + (f"- The planned Stop Loss is {sl_pct:.2f}% away with a 1:{rr_ratio:.1f} "
               f"Risk/Reward. Reject this trade if the required stop is too wide for "
               f"the current local volatility.\n" if sl_pct is not None and rr_ratio is not None else "")
        )

        prompt=(f"You are a veteran prop-firm trader with years on a funded desk — blunt, "
                f"experienced, and speaking with the raw conviction of someone who has seen "
                f"this exact setup a hundred times before. You are NOT writing a textbook "
                f"summary or a balanced research note. You call it like you see it: "
                f"'Clear retail trap,' 'Heavy accumulation,' 'Chop zone, avoiding,' 'This is "
                f"a gift,' 'Textbook, but late.' Deciding whether to actually "
                f"take this trade with real money, the way you would after scanning a chart top-down "
                f"across multiple timeframes — starting with the big picture, then zooming in.\n\n"
                f"Setup: {coin}/USDT {dir_word}\n\n"
                f"{narrative}\n"
                f"Price: {format_price(price)}\n"
                f"RSI: {rsi_val:.0f} | ADX (trend strength): {adx_val:.0f} | Volume: {vol_strength:.1f}x average\n"
                f"{vol_note}\n{penalty_line}\n"
                f"Last 20 candles on the 15m chart, oldest to newest (C20 = right now):\n"+"\n".join(candle_desc)+
                f"\n\nUsing the narrative above FIRST — is this accumulation/distribution happening at a "
                f"real level, with the higher timeframe on your side? Then look at the local candles: "
                f"do NOT just grade whether momentum already confirmed — a confirmed breakout candle "
                f"often means the easy money is already made. Judge the STAGE of this move by looking "
                f"for signs of build-up: volatility contraction, absorption (heavy volume with small net "
                f"price change), dying volume before a squeeze, or wicks showing rejection at a level "
                f"repeatedly tested. A calm, tightening range sitting just under resistance (or above "
                f"support), inside a real zone, with the 4h trend aligned, is often the BEST entry — "
                f"before the crowd's breakout signal fires.\n\n"
                f"Classify the STAGE: EARLY (still coiling/building, low risk entry), MID (breaking out now, "
                f"some room left), or LATE (already extended, chasing).\n\n"
                f"Respond EXACTLY in this format:\n"
                f"VERDICT: [CLEAN/MESSY]\nCONFIDENCE: [HIGH/MEDIUM/LOW]\n"
                f"STAGE: [EARLY/MID/LATE]\nTRADE: [YES/NO]\n"
                f"ETA_READ: [short phrase, e.g. 'could take 2-4h to develop' or 'move may already be exhausted']\n"
                f"REASONING: [2 sentences max — speak like a trader calling it on the desk, not a "
                f"textbook. Be specific and blunt about what you saw. Real desk language, not "
                f"hedge-everything corporate-speak.]")
        res=requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key":ANTHROPIC_API_KEY,"anthropic-version":"2023-06-01",
                     "content-type":"application/json"},
            json={"model":"claude-haiku-4-5-20251001","max_tokens":220,
                  "messages":[{"role":"user","content":prompt}]},timeout=15)
        if res.status_code!=200: return None
        text=res.json()["content"][0]["text"].strip()
        verdict="CLEAN" if "VERDICT: CLEAN" in text else "MESSY"
        confidence="HIGH" if "CONFIDENCE: HIGH" in text else "MEDIUM" if "CONFIDENCE: MEDIUM" in text else "LOW"
        stage="EARLY" if "STAGE: EARLY" in text else "MID" if "STAGE: MID" in text else "LATE" if "STAGE: LATE" in text else "UNKNOWN"
        trade="YES" in (text.split("TRADE:")[-1].split("\n")[0] if "TRADE:" in text else "")
        eta_read=text.split("ETA_READ:")[-1].split("REASONING:")[0].strip() if "ETA_READ:" in text else ""
        reasoning=text.split("REASONING:")[-1].strip() if "REASONING:" in text else ""
        logger.info(f"AI {coin}: {verdict}/{confidence}/STAGE:{stage}/TRADE:{'YES' if trade else 'NO'}")
        return {"verdict":verdict,"confidence":confidence,"stage":stage,"trade":trade,
                "eta_read":eta_read,"reasoning":reasoning}
    except Exception as e:
        logger.warning(f"AI error {coin}: {e}"); return None

def expire_pending_signals():
    now=get_ist_datetime()
    expired=[c for c,s in list(pending_signals.items()) if s.get("expires_at") and now>s["expires_at"]]
    for coin in expired:
        with trade_lock:
            s = pending_signals.get(coin)
            _eng_label = get_engine_label(s["pattern"].split(" + ")[0]) if s and s.get("pattern") else "📊 SIGNAL ENGINE"
            if coin in pending_signals: del pending_signals[coin]
        send_telegram(f"⏰ <b>{BOT_HEADER}</b>\n🏗️ Engine: {_eng_label}\nSignal expired: <b>{coin}</b>")
    if expired: save_pending_signals()

def check_price_alerts():
    triggered=[]
    for sym,alert in list(price_alerts.items()):
        price=get_price(sym+"USDT")
        if not price: continue
        if alert["direction"]=="above" and price>=alert["price"]:
            send_telegram(
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🔔 <b>PRICE ALERT TRIGGERED</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"  🪙 <b>{sym}</b> broke ABOVE target\n"
                f"  🎯 Target : <code>{format_price(alert['price'])}</code>\n"
                f"  💰 Now    : <code>{format_price(price)}</code>\n"
                f"  🕐 {get_ist_time()}"
            )
            triggered.append(sym)
        elif alert["direction"]=="below" and price<=alert["price"]:
            send_telegram(
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🔔 <b>PRICE ALERT TRIGGERED</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"  🪙 <b>{sym}</b> broke BELOW target\n"
                f"  🎯 Target : <code>{format_price(alert['price'])}</code>\n"
                f"  💰 Now    : <code>{format_price(price)}</code>\n"
                f"  🕐 {get_ist_time()}"
            )
            triggered.append(sym)
    for sym in triggered: del price_alerts[sym]
    if triggered: save_alerts()

def update_trailing_sl(coin,trade,price,klines=None):
    """The Law of Dynamic Noise: Chandelier Exit trailing stop, based on"""
    if trade.get("is_macro"):
        klines = get_klines(trade.get("symbol", coin+"USDT"), "4h", 20)
    elif trade.get("pattern","").split(" + ")[0] in ("Yellow Circle Sniper","5m Multi-TF Sniper"):
        klines = get_klines(trade.get("symbol", coin+"USDT"), "5m", 20)

    if klines and len(klines) >= 15 and trade.get("timestamp"):
        atr = calculate_atr(klines, 14)
        if atr <= 0: return
        atr_trail_dist = atr * 2.5
        activation_buffer = atr * 1.5
        if trade["direction"] == "BUY":
            if price > trade["entry"] + activation_buffer:
                highest_recent_high = max(float(k[2]) for k in klines[-5:])
                new_sl = highest_recent_high - atr_trail_dist
                if new_sl > trade["sl"] and new_sl < price:
                    with trade_lock:
                        if coin in active_trades: active_trades[coin]["sl"] = new_sl
                    save_active_trades()
        else:
            if price < trade["entry"] - activation_buffer:
                lowest_recent_low = min(float(k[3]) for k in klines[-5:])
                new_sl = lowest_recent_low + atr_trail_dist
                if new_sl < trade["sl"] and new_sl > price:
                    with trade_lock:
                        if coin in active_trades: active_trades[coin]["sl"] = new_sl
                    save_active_trades()
        return
    trail=abs(trade["tp"]-trade["entry"])*0.3
    if trade["direction"]=="BUY":
        new_sl=price-trail
        if new_sl>trade["sl"]:
            with trade_lock:
                if coin in active_trades: active_trades[coin]["sl"]=new_sl
            save_active_trades()
    else:
        new_sl=price+trail
        if new_sl<trade["sl"]:
            with trade_lock:
                if coin in active_trades: active_trades[coin]["sl"]=new_sl
            save_active_trades()

def check_profit_milestones(coin,trade,price,pnl):
    """Proportional milestone system — scales with the trade's ACTUAL profit target,"""
    milestones=trade.get("milestones_sent",[])
    ep=trade["entry"]; direction=trade["direction"]; lev=trade.get("leverage",1)

    if trade.get("pattern","").startswith("Metals MTF"):
        tp1_roi = trade.get("metals_tp1_roi_pct", 10.0)
        if pnl >= tp1_roi and "p1" not in milestones:
            sl_price = ep
            active_trades[coin].setdefault("milestones_sent",[]).append("p1")
            active_trades[coin]["sl"] = sl_price
            if trade.get("timestamp"):
                m1_mins = (get_ist_datetime() - trade["timestamp"]).total_seconds() / 60
                active_trades[coin]["time_to_m1_mins"] = round(m1_mins, 1)
            save_active_trades()
            send_telegram(
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"✅ <b>METALS TP1  •  +{tp1_roi:.1f}% ROI reached</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"  🪙 Coin    : <b>{coin}</b>\n"
                f"  📈 PnL     : {fmt_pnl(pnl)}\n"
                f"  🎯 Riding to TP2 (capped ≤20% ROI)\n"
                f"  🛑 Move SL : <code>{format_price(sl_price)}</code>  (breakeven)\n"
                f"  🕐 {get_ist_time()}"
            )
        return

    target=trade.get("profit_target", abs(trade["tp"]-ep)/ep*100*lev)
    if target<=0: target=10

    m1=target*0.50; m2=target*0.75; m3=target*0.90

    def _sl_lock_price(target_pnl, lock_ratio):
        gain_price = abs(price_at_pnl(ep, direction, lev, target_pnl) - ep)
        locked = gain_price * lock_ratio
        return ep+locked if direction=="BUY" else ep-locked

    def _ms(icon,title,detail,sl_price):
        return (f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{icon} <b>{title}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"  🪙 Coin    : <b>{coin}</b>\n"
                f"  🏗️ Engine  : {get_engine_label(trade.get('pattern',''))}\n"
                f"  📈 PnL     : {fmt_pnl(pnl)}\n"
                f"  🎯 Target  : +{target:.1f}%\n"
                f"  🛑 Move SL : <code>{format_price(sl_price)}</code>\n"
                f"  💡 {detail}\n"
                f"  🕐 {get_ist_time()}")

    if pnl>=m1 and "p1" not in milestones:
        sl_price=_sl_lock_price(m1,0.0)
        active_trades[coin].setdefault("milestones_sent",[]).append("p1")
        active_trades[coin]["sl"]=sl_price
        if trade.get("timestamp"):
            m1_mins = (get_ist_datetime() - trade["timestamp"]).total_seconds() / 60
            active_trades[coin]["time_to_m1_mins"] = round(m1_mins, 1)
        save_active_trades()
        send_telegram(_ms("✅",f"MILESTONE 1  •  +{m1:.1f}% reached",
                          "SL moved to breakeven — trade is now risk-free!",sl_price))
    elif pnl>=m2 and "p2" not in milestones:
        sl_price=_sl_lock_price(m2,0.5)
        active_trades[coin].setdefault("milestones_sent",[]).append("p2")
        active_trades[coin]["sl"]=sl_price
        save_active_trades()
        send_telegram(_ms("🔥",f"MILESTONE 2  •  +{m2:.1f}% reached",
                          f"SL moved to lock in ~50% of current gain ({fmt_pnl(m2*0.5)} minimum).",sl_price))
    elif pnl>=m3 and "p3" not in milestones:
        sl_price=_sl_lock_price(m3,0.8)
        active_trades[coin].setdefault("milestones_sent",[]).append("p3")
        active_trades[coin]["sl"]=sl_price
        save_active_trades()
        send_telegram(_ms("🚀",f"MILESTONE 3  •  +{m3:.1f}% reached",
                          f"SL moved to lock in ~80% of current gain ({fmt_pnl(m3*0.8)} minimum). Final target +{target:.1f}%!",sl_price))

def check_5m_sniper_trigger(symbol, direction):
    """The Law of Two-Stage Execution (The Sniper Trigger)."""
    try:
        k5 = get_klines(symbol, "5m", 15)
        if not k5 or len(k5) < 13:
            return False, "5m data unavailable"

        for i in range(-3, 0):
            c_open, c_high, c_low, c_close = float(k5[i][1]), float(k5[i][2]), float(k5[i][3]), float(k5[i][4])
            candle_range = c_high - c_low

            avg_v = sum(float(x[5]) for x in k5[-13:-3]) / 10 if len(k5) >= 13 else 1.0
            vol_ratio = float(k5[i][5]) / avg_v if avg_v > 0 else 0
            vol_spiking = vol_ratio >= 1.5
            vol_not_dead = vol_ratio >= 0.6

            if direction == "BUY":
                bullish_close = c_close > c_open
                breaking_high = c_close > max(float(x[2]) for x in k5[i-4:i]) if len(k5[:i]) >= 4 else False
                if bullish_close and breaking_high and vol_spiking:
                    return True, f"5m Sniper: Breakout confirmed ({vol_ratio:.1f}x volume)"
                lower_wick_pct = (min(c_open, c_close) - c_low) / candle_range * 100 if candle_range > 0 else 0
                if (lower_wick_pct >= 35.0 or breaking_high) and bullish_close and vol_not_dead:
                    return True, f"5m Sniper: Early micro-reversal/level defense ({vol_ratio:.1f}x vol)"

            elif direction == "SELL":
                bearish_close = c_close < c_open
                breaking_low = c_close < min(float(x[3]) for x in k5[i-4:i]) if len(k5[:i]) >= 4 else False
                if bearish_close and breaking_low and vol_spiking:
                    return True, f"5m Sniper: Breakdown confirmed ({vol_ratio:.1f}x volume)"
                upper_wick_pct = (c_high - max(c_open, c_close)) / candle_range * 100 if candle_range > 0 else 0
                if (upper_wick_pct >= 35.0 or breaking_low) and bearish_close and vol_not_dead:
                    return True, f"5m Sniper: Early micro-reversal/level defense ({vol_ratio:.1f}x vol)"

        return False, "Waiting for 5m volume/breakout or early micro-reversal trigger"
    except Exception as e:
        return False, f"5m trigger error: {e}"


def check_evaluating_signals():
    """The Poll & Resume half of the two-stage EVALUATING pipeline. Runs"""
    global evaluating_signals
    triggered_any = False
    now = get_ist_datetime()

    for coin, data in list(evaluating_signals.items()):
        minutes_active = (now - data["logged_at"]).total_seconds() / 60
        if minutes_active > 90:
            logger.info(f"{coin} evaluation expired — no 5m trigger within 90 mins.")
            del evaluating_signals[coin]
            triggered_any = True
            continue

        setup = data["setup"]
        market_condition = data["market_condition"]

        sniper_triggered, sniper_note = check_5m_sniper_trigger(setup["symbol"], setup["direction"])

        if sniper_triggered:
            logger.info(f"{coin} EVALUATING signal triggered: {sniper_note} — Resuming pipeline.")
            del evaluating_signals[coin]
            triggered_any = True

            fresh_scan_price = get_price(setup["symbol"])
            if fresh_scan_price:
                setup["scan_price"] = fresh_scan_price

            format_and_send(setup, coin, market_condition=market_condition, from_evaluation=True)

    if triggered_any:
        save_evaluating_signals()

def format_and_send(setup,coin,is_river=False,is_instant=False,market_condition="bull",from_evaluation=False):
    global sent_coins,coin_cooldowns
    if check_circuit_breaker(): return False
    if not is_good_trading_session(coin): return False
    live_price=get_price(setup["symbol"])
    if not live_price: return False
    entry=live_price
    drift_pct=abs(entry-setup["scan_price"])/setup["scan_price"]*100
    if drift_pct>5.0:
        logger.info(f"{coin} rejected - drifted {drift_pct:.1f}%"); return False
    klines_1d=get_klines(setup["symbol"],"1d",20)
    if klines_1d and len(klines_1d)>=15:
        daily_atr=calculate_atr(klines_1d,14)
        todays_range=float(klines_1d[-1][2])-float(klines_1d[-1][3])
        if daily_atr>0 and todays_range>(daily_atr*2.5):
            logger.info(f"{coin} rejected - Daily ATR exhausted (today's range {todays_range:.4g} vs "
                       f"14d ATR {daily_atr:.4g}, {todays_range/daily_atr:.1f}x)")
            return False
    klines_15m=get_klines(setup["symbol"],"15m",100)
    klines_1h=get_klines(setup["symbol"],"1h",50)
    if not klines_15m: return False
    closes=[float(x[4]) for x in klines_15m]
    _pattern_native_interval = {
        "Yellow Circle Sniper": "5m",
        "5m Multi-TF Sniper": "5m",
        "Order Flow Sniper": "15m",
    }
    _sl_klines = klines_15m
    _chart_interval = "15m"
    _primary_for_native = setup["pattern"].split(" + ")[0]
    if _primary_for_native in _pattern_native_interval:
        _native_interval = _pattern_native_interval[_primary_for_native]
        if _native_interval != "15m":
            _native_klines = get_klines(setup["symbol"], _native_interval, 60)
            if _native_klines and len(_native_klines) >= 20:
                _sl_klines = _native_klines
                _chart_interval = _native_interval
    atr_1h=calculate_atr(klines_1h) if len(klines_1h)>=15 else calculate_atr(klines_15m)
    atr_pct=(atr_1h/entry)*100 if entry>0 else 0
    vol_ok=is_volume_confirmed(klines_15m)
    rsi_ok=is_rsi_valid(closes,setup["direction"])
    funding_ok=is_funding_favorable(setup["symbol"],setup["direction"])
    is_volatile=not is_volatility_normal(klines_15m)

    score_penalty = 0
    penalty_notes = []
    _primary_pat = setup["pattern"].split(" + ")[0]
    _floor_primary = _primary_pat
    if setup.get("is_macro"):
        st_ok = True
        is_instant = True
    if not setup.get("is_macro"):
        is_quiet_accumulation_pattern = any(p in setup["pattern"] for p in ("Inside Bar Coil","Pre-Breakout Compression","Volatility Contraction (Coiling)","Early Spark Ignition","Smart Money Absorption","Funding Divergence Sniper"))
        if not vol_ok and not is_quiet_accumulation_pattern:
            score_penalty += 6; penalty_notes.append("volume soft (-6)")
        if not rsi_ok:
            score_penalty += 5; penalty_notes.append("RSI stretched (-5)")
        if not funding_ok:
            score_penalty += 4; penalty_notes.append("funding against (-4)")
        if is_volatile:
            logger.info(f"{coin} high volatility — noted, letting AI judge")


        if _primary_pat in ("Double Top","Double Bottom","BOS Breakout","Volume Breakout"):
            _veto_closes = [float(k[4]) for k in klines_15m]
            _veto_highs = [float(k[2]) for k in klines_15m]
            _veto_lows = [float(k[3]) for k in klines_15m]
            recent_4_lows = _veto_lows[-4:]
            recent_4_highs = _veto_highs[-4:]
            _veto_direction = setup["direction"]
            if _veto_direction == "BUY":
                local_base = min(recent_4_lows)
                vertical_stretch_pct = (entry - local_base) / local_base * 100 if local_base > 0 else 0
            else:
                local_base = max(recent_4_highs)
                vertical_stretch_pct = (local_base - entry) / entry * 100 if entry > 0 else 0
            if vertical_stretch_pct > 1.8:
                logger.info(f"{coin} Anti-Chase Veto: {_veto_direction} extended {vertical_stretch_pct:+.2f}% from the local 1h base ({_primary_pat}). Move is exhausted, rerouting to retest watchlist.")
                _veto_precise_level = None
                if _primary_pat in ("Double Top","Double Bottom"):
                    _veto_vols = [float(k[5]) for k in klines_15m]
                    _veto_avg_vol = sum(_veto_vols[-20:]) / 20 if len(_veto_vols) >= 20 else 1.0
                    if _primary_pat == "Double Bottom":
                        _veto_fired, _veto_lvl = detect_double_bottom_pro(_veto_highs, _veto_lows, _veto_closes, _veto_vols, entry, _veto_avg_vol)
                    else:
                        _veto_fired, _veto_lvl = detect_double_top_pro(_veto_highs, _veto_lows, _veto_closes, _veto_vols, entry, _veto_avg_vol)
                    if _veto_fired and _veto_lvl > 0:
                        _veto_precise_level = _veto_lvl
                log_retest_candidate(coin, setup["symbol"], _veto_direction, _veto_closes, _veto_highs, _veto_lows, setup["pattern"], pattern_type="bos_retest", precise_level=_veto_precise_level)
                coin_cooldowns[coin] = get_ist_datetime() + timedelta(minutes=30)
                return False

        is_quiet_accumulation = _primary_pat in ("Inside Bar Coil","Pre-Breakout Compression","Volatility Contraction (Coiling)","Early Spark Ignition","Smart Money Absorption","Funding Divergence Sniper","Liquidity Sweep","Trend Continuation Coil","Bull Flag Formation","Bear Flag Formation")

        sniper_triggered, sniper_note = check_5m_sniper_trigger(setup["symbol"], setup["direction"])

        if is_quiet_accumulation and not sniper_triggered and not from_evaluation:
            _deep_coil_zones = get_htf_zones(setup["symbol"])
            _deep_coil_zone_ok, _deep_coil_zone_label = is_in_zone(entry, setup["direction"], _deep_coil_zones)
            if _deep_coil_zone_ok and setup["setup_score"] >= 92.0:
                logger.info(f"{coin} Deep Coil Bypass — A+ score ({setup['setup_score']:.1f}) already sitting in {_deep_coil_zone_label}, skipping 5m wait")
                penalty_notes.append("Deep Coil Bypass (Instant Entry)")
            else:
                if coin not in evaluating_signals:
                    logger.info(f"{coin} Quiet Coil detected. Asking Claude before tracking...")
                    _pre_susp_highs = [float(k[2]) for k in klines_15m]
                    _pre_susp_lows = [float(k[3]) for k in klines_15m]
                    _pre_susp_vols = [float(k[5]) for k in klines_15m]
                    _pre_susp_avg_vol = sum(_pre_susp_vols[-20:-1]) / 19 if len(_pre_susp_vols) >= 20 else 1.0
                    _pre_susp_vol_ratio = _pre_susp_vols[-1] / _pre_susp_avg_vol if _pre_susp_avg_vol > 0 else 1.0
                    _pre_susp_rsi = calculate_rsi(closes)
                    _pre_susp_adx = calculate_adx(klines_15m)
                    _pre_susp_ms = detect_market_structure(klines_15m)
                    _pre_susp_zones = get_htf_zones(setup["symbol"])
                    _pre_susp_zone_ok, _pre_susp_zone_label = is_in_zone(entry, setup["direction"], _pre_susp_zones)
                    _pre_susp_hist = pattern_stats.get(_primary_pat, {})
                    _pre_susp_signals = _pre_susp_hist.get("signals", 0)
                    _pre_susp_hist_wr = (_pre_susp_hist.get("wins", 0) / _pre_susp_signals * 100) if _pre_susp_signals >= 3 else None

                    _pre_susp_ai = ai_analyze_setup(
                        coin, setup["direction"], klines_15m, entry, setup["pattern"],
                        _pre_susp_rsi, _pre_susp_adx, _pre_susp_vol_ratio, False, penalty_notes,
                        get_htf_trend(setup["symbol"], "4h"), _pre_susp_zone_ok, _pre_susp_zone_label,
                        _pre_susp_ms["bos"], _pre_susp_ms["choch"], _pre_susp_ms["bias"], False,
                        None, None, _pre_susp_hist_wr, _pre_susp_signals
                    )

                    if _pre_susp_ai:
                        if not _pre_susp_ai["trade"] or _pre_susp_ai["stage"] == "LATE" or _pre_susp_ai["verdict"] == "MESSY":
                            logger.info(f"{coin} EVALUATION REJECTED BY AI before tracking: {_pre_susp_ai['reasoning']}")
                            if _pre_susp_ai["stage"] == "LATE":
                                log_retest_candidate(coin, setup["symbol"], setup["direction"], closes, _pre_susp_highs, _pre_susp_lows, setup["pattern"])
                            coin_cooldowns[coin] = get_ist_datetime() + timedelta(minutes=20)
                            return False
                        setup["ai_reasoning"] = _pre_susp_ai["reasoning"]

                    evaluating_signals[coin] = {
                        "setup": setup,
                        "market_condition": market_condition,
                        "logged_at": get_ist_datetime()
                    }
                    save_evaluating_signals()

                    if coin not in early_watch_sent or (get_ist_datetime()-early_watch_sent[coin]).total_seconds()>3600:
                        early_watch_sent[coin]=get_ist_datetime()
                        send_telegram(
                            f"🟡 <b>EARLY ALERT: 15m Setup Detected — {coin}</b>\n"
                            f"⚙️ <b>TRADING SIGNAL MASTER v32G</b>\n"
                            f"🏗️ Engine: {get_engine_label(_primary_pat)}\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                            f"🪙 <b>{coin}</b>  {'🟢' if setup['direction']=='BUY' else '🔴'} {setup['direction']}\n"
                            f"📌 Pattern: {_primary_pat}\n"
                            f"💰 Price coiling at: <code>{format_price(setup['scan_price'])}</code>\n\n"
                            f"⏳ <b>STATUS: MONITORING 5M CHART (90m Window)</b>\n"
                            f"   The 15m/1h trend is compressing early. We are now\n"
                            f"   waiting for the exact 5-minute wick rejection to\n"
                            f"   fire the executable trade signal.\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"🕐 {get_ist_time()}"
                        )
                logger.info(f"{coin} {setup['direction']} coiled on 15m, moved to EVALUATING state.")
                return False

        if from_evaluation:
            logger.info(f"{coin} 5m Sniper confirmed from EVALUATING state.")
            penalty_notes.append("5m Sniper Executed (Post-Evaluation)")
        elif sniper_triggered:
            logger.info(f"{coin} {sniper_note} — EXECUTING EARLY ENTRY")
            penalty_notes.append("5m Sniper Executed")
        else:
            score_penalty += 4; penalty_notes.append(f"5m timing weak (-4)")

        sector_ok, sector_note = check_sector_correlation(coin, setup["direction"])
        if not sector_ok:
            score_penalty += 5; penalty_notes.append(f"sector diverging (-5)")
        logger.info(f"{coin} sector check: {sector_note}")

        if is_weekend_low_liquidity():
            score_penalty += 3; penalty_notes.append("weekend low-liquidity (-3)")

        st_15m=calculate_supertrend(klines_15m,ST_PERIOD,ST_MULTIPLIER)
        st_1h=calculate_supertrend(klines_1h,ST_PERIOD,ST_MULTIPLIER) if klines_1h else st_15m
        st_ok=(st_15m==setup["direction"]) and (st_1h==setup["direction"])
        st_strongly_against = (st_15m!=setup["direction"]) and (st_1h!=setup["direction"])
        _st_primary = setup["pattern"].split(" + ")[0]
        _st_is_accum = _st_primary in ("Inside Bar Coil","Pre-Breakout Compression","Volatility Contraction (Coiling)","Early Spark Ignition","Vanguard Macro Squeeze","Smart Money Absorption","Funding Divergence Sniper","Order Flow Sniper","Yellow Circle Sniper")
        if st_strongly_against and not _st_is_accum:
            logger.info(f"{coin} rejected - SuperTrend opposed on both 15m+1h"); return False
        elif st_strongly_against and _st_is_accum:
            logger.info(f"{coin} SuperTrend opposed on both 15m+1h, but {_st_primary} is exempt (early accumulation pattern)")
        elif st_15m!=setup["direction"] or st_1h!=setup["direction"]:
            score_penalty += 5; penalty_notes.append("SuperTrend partial lag (-5)")

        setup["setup_score"] = max(setup["setup_score"] - score_penalty, 0)
        if penalty_notes:
            logger.info(f"{coin} score adjusted: -{score_penalty} ({', '.join(penalty_notes)}) -> {setup['setup_score']:.1f}")
        is_instant = setup["setup_score"] >= INSTANT_SIGNAL_THRESHOLD
        _is_accum = _floor_primary in ("Inside Bar Coil","Pre-Breakout Compression","Volatility Contraction (Coiling)","Early Spark Ignition","Vanguard Macro Squeeze","Smart Money Absorption","Funding Divergence Sniper","Order Flow Sniper","Yellow Circle Sniper")
        _effective_floor = ACCUMULATION_SCORE_FLOOR if _is_accum else 92.0
        if setup["setup_score"] < _effective_floor:
            logger.info(f"{coin} rejected - score {setup['setup_score']:.1f} below strict floor {_effective_floor}"); return False
    vwap,vwap_upper,vwap_lower=calculate_vwap_with_bands(klines_15m); vwap_ok=False; vwap_label="N/A"
    if vwap:
        if setup["direction"]=="BUY" and entry>vwap:    vwap_ok=True; vwap_label=f"Above {format_price(vwap)}"
        elif setup["direction"]=="SELL" and entry<vwap: vwap_ok=True; vwap_label=f"Below {format_price(vwap)}"
        else: vwap_label=f"{'Below' if setup['direction']=='BUY' else 'Above'} {format_price(vwap)}"
    if not setup.get("is_macro"):
        if vwap_upper and setup["direction"]=="BUY" and entry>vwap_upper:
            logger.info(f"{coin} rejected - price {format_price(entry)} is +2 SD above VWAP {format_price(vwap)} (Mean Reversion Risk)")
            return False
        if vwap_lower and setup["direction"]=="SELL" and entry<vwap_lower:
            logger.info(f"{coin} rejected - price {format_price(entry)} is -2 SD below VWAP {format_price(vwap)} (Mean Reversion Risk)")
            return False
    poc_price = get_point_of_control(klines_1h)
    if poc_price and not setup.get("is_macro"):
        dist_to_poc = (poc_price - entry) / entry * 100
        if setup["direction"] == "BUY" and 0 < dist_to_poc < 1.0:
            logger.info(f"{coin} rejected - buying directly into heavy POC resistance at {format_price(poc_price)}")
            return False
        if setup["direction"] == "SELL" and -1.0 < dist_to_poc < 0:
            logger.info(f"{coin} rejected - shorting directly into heavy POC support at {format_price(poc_price)}")
            return False
    zones=get_htf_zones(setup["symbol"])
    zone_ok,zone_label=is_in_zone(entry,setup["direction"],zones)
    div=detect_rsi_divergence(closes)
    oi_rising=get_oi_trend(setup["symbol"])
    vol_ratio,lead_exchange=get_global_volume(setup["symbol"],klines_15m)
    adx_val=calculate_adx(klines_15m)
    tf_score=setup.get("tf_score",get_timeframe_score(setup["symbol"],setup["direction"]))
    btc_aligned,btc_1h_trend=is_btc_aligned(setup["direction"])
    ms = detect_market_structure(klines_15m)
    highs_15m=[float(k[2]) for k in klines_15m]; lows_15m=[float(k[3]) for k in klines_15m]

    if len(klines_15m) >= 19:
        _local_base_klines = klines_15m[-4:]
        _pre_breakout_klines = klines_15m[:-4]
        _pre_breakout_atr = calculate_atr(_pre_breakout_klines)
        _pre_breakout_atr_pct = (_pre_breakout_atr / entry * 100) if entry > 0 and _pre_breakout_atr > 0 else 0
        if _pre_breakout_atr_pct > 0:
            if setup["direction"] == "BUY":
                _local_base = min(float(k[3]) for k in _local_base_klines)
                _stretch_pct = (entry - _local_base) / _local_base * 100 if _local_base > 0 else 0
            else:
                _local_base = max(float(k[2]) for k in _local_base_klines)
                _stretch_pct = (_local_base - entry) / entry * 100 if entry > 0 else 0
            _stretch_risk_multiples = _stretch_pct / _pre_breakout_atr_pct
            if _stretch_risk_multiples >= 4.0:
                logger.info(f"{coin} Anti-Chase Veto: +{_stretch_pct:.2f}% from local base = {_stretch_risk_multiples:.1f}x pre-breakout risk. Rerouting to retest watchlist.")
                log_retest_candidate(coin, setup["symbol"], setup["direction"], closes, highs_15m, lows_15m, setup["pattern"])
                coin_cooldowns[coin] = get_ist_datetime() + timedelta(minutes=30)
                return False

    res = ms["swing_high"] if ms["swing_high"] > 0 else max(highs_15m[-30:-1])
    sup = ms["swing_low"]  if ms["swing_low"]  > 0 else min(lows_15m[-30:-1])
    opens_15m=[float(k[1]) for k in klines_15m]
    sweep_dir_chk, sweep_strength_chk = detect_liquidity_sweep(klines_15m, highs_15m, lows_15m, closes, opens_15m, sup, res, ms)
    is_sweep = sweep_dir_chk is not None and sweep_dir_chk == setup["direction"]
    if setup.get("is_macro") and setup.get("macro_grade") is not None:
        grade = setup["macro_grade"]
        pts = setup["macro_pts"]
        breakdown = setup["macro_breakdown"]
        _coin_regime = None
    else:
        _coin_regime = detect_market_regime(klines_15m)
        grade_result=get_signal_grade(setup["setup_score"],vol_ratio,oi_rising,tf_score,vol_ok,rsi_ok,funding_ok,st_ok,vwap_ok,zone_ok,adx_val,btc_aligned,ms["bias"],ms["bos"],is_sweep,closes,atr_pct,setup["symbol"],_coin_regime,_primary_pat)
        grade,pts,breakdown=grade_result

    _accum_exempt_patterns = ("Inside Bar Coil","Pre-Breakout Compression","Volatility Contraction (Coiling)","Early Spark Ignition","Vanguard Macro Squeeze","Smart Money Absorption","Funding Divergence Sniper","Order Flow Sniper","Yellow Circle Sniper")
    _is_accum_exempt = _floor_primary in _accum_exempt_patterns or _floor_primary.startswith("Pre-Breakout Macro")
    if grade in ("Grade C","Grade B") and (not _is_accum_exempt or pts < 7):
        logger.info(f"{coin} rejected - {grade} on scorecard ({pts} pts) despite score {setup['setup_score']:.1f}"); return False

    fresh_price=get_price(setup["symbol"])
    if not fresh_price:
        logger.info(f"{coin} rejected - price unavailable at final pre-risk check")
        return False
    final_drift_pct=abs(fresh_price-entry)/entry*100 if entry>0 else 99
    if final_drift_pct>5.0:
        logger.info(f"{coin} rejected - drifted {final_drift_pct:.1f}% during processing (was {format_price(entry)}, now {format_price(fresh_price)})")
        return False
    entry=fresh_price

    lev=get_smart_leverage(setup["symbol"],atr_pct,setup["setup_score"],grade)
    if setup["pattern"].split(" + ")[0].startswith("Pre-Breakout Macro") and setup.get("macro_sl") is not None:
        sl = setup["macro_sl"]
    else:
        sl=get_structure_sl(_sl_klines,setup["direction"],entry,atr_1h)
    if setup.get("is_macro") and setup.get("macro_tp") is not None:
        tp = setup["macro_tp"]
        sl_dist = abs(entry - sl)
        logger.info(f"{coin} using pre-computed 4H Macro TP at {format_price(tp)} (R:R {abs(tp-entry)/sl_dist:.1f}:1)" if sl_dist > 0 else f"{coin} using pre-computed 4H Macro TP at {format_price(tp)}")
    else:
        sl_dist=abs(entry-sl)
        atr_tp_dist=atr_1h*ATR_TP_MULTIPLIER
        min_rr_tp_dist=sl_dist*MIN_RR_RATIO
        structural_tp=get_structural_tp(entry,setup["direction"],zones,min_rr_tp_dist)
        if structural_tp is not None:
            tp=structural_tp
            logger.info(f"{coin} TP anchored to structural zone at {format_price(tp)} "
                        f"(R:R {abs(tp-entry)/sl_dist:.1f}:1)")
        else:
            tp_dist=max(atr_tp_dist,min_rr_tp_dist)
            tp=entry+tp_dist if setup["direction"]=="BUY" else entry-tp_dist
    profit_target=(abs(tp-entry)/entry)*100*lev
    if profit_target<MIN_PROFIT_TARGET:
        risk=abs(tp-entry)/entry
        if risk>0:
            needed=int(MIN_PROFIT_TARGET/(risk*100))+1
            if needed<=20: lev=needed; profit_target=(abs(tp-entry)/entry)*100*lev
            else: return False
    setup["leverage"]=lev

    ai_result=None
    if setup.get("is_macro") or from_evaluation:
        if setup.get("is_macro"):
            ai_reason = setup.get("macro_ai_reasoning", "Approved by Upstream Macro AI.")
        elif from_evaluation:
            ai_reason = setup.get("ai_reasoning", "Pre-approved by AI during coiling phase.")
        else:
            ai_reason = "Mathematical Sniper Execution (AI Bypassed to avoid late-chase rejection)."
        ai_result = {
            "verdict": "CLEAN",
            "confidence": "HIGH",
            "stage": "EARLY",
            "trade": True,
            "eta_read": "Executing pure pattern geometry.",
            "reasoning": ai_reason,
        }
        logger.info(f"{coin} Pattern Fast-Track — bypassing 15m AI check for pure execution.")
    else:
        primary_pattern = setup["pattern"].split(" + ")[0]
        is_grade_a = grade in ("Grade A 🍀","Grade A+ 🍀")
        is_early_pat = primary_pattern in ("Inside Bar Coil","Pre-Breakout Compression","Volatility Contraction (Coiling)","Early Spark Ignition","Vanguard Macro Squeeze","Smart Money Absorption","Funding Divergence Sniper","5m Multi-TF Sniper","Order Flow Sniper","Yellow Circle Sniper")
        if is_grade_a or is_early_pat:
            if is_early_pat and not is_grade_a:
                logger.info(f"{coin} AI Fast-Track ({primary_pattern}, {grade}/{pts}pts) — sending to Claude despite not being Grade A")
            rsi_ai=calculate_rsi(closes)
            adx_ai=calculate_adx(klines_15m)
            htf_4h=get_htf_trend(setup["symbol"],"4h")
            sl_pct_ai = abs(entry-sl)/entry*100 if entry>0 else 0
            tp_pct_ai = abs(tp-entry)/entry*100 if entry>0 else 0
            rr_ratio_ai = tp_pct_ai/sl_pct_ai if sl_pct_ai>0 else 0
            pstat = pattern_stats.get(primary_pattern, {})
            p_signals = pstat.get("signals", 0)
            hist_wr = (pstat.get("wins", 0) / p_signals * 100) if p_signals >= 3 else None
            logger.info(f"{coin} AI-eligible + {grade} ({pts}pts) — calling Claude for final verification")
            ai_result=ai_analyze_setup(coin,setup["direction"],klines_15m,entry,
                                       setup["pattern"],rsi_ai,adx_ai,vol_ratio,is_volatile,penalty_notes,
                                       htf_4h_trend=htf_4h,zone_ok=zone_ok,zone_label=zone_label,
                                       ms_bos=ms["bos"],ms_choch=ms["choch"],ms_bias=ms["bias"],
                                       is_sweep=is_sweep,sl_pct=sl_pct_ai,rr_ratio=rr_ratio_ai,
                                       hist_wr=hist_wr,hist_signals=p_signals)
            if ai_result and ai_result["trade"]==False:
                stage = ai_result.get("stage","")
                if stage == "EARLY" and primary_pattern == "Early Spark Ignition":
                    logger.info(f"{coin} AI said TRADE:NO but STAGE:EARLY on Early Spark Ignition — executing per explicit bottom-catching override, AI notes will be shown")
                else:
                    logger.info(f"{coin} rejected by AI — {ai_result['verdict']}/{ai_result['confidence']}/STAGE:{stage}")
                    coin_cooldowns[coin]=get_ist_datetime()+timedelta(minutes=20)
                    return False
            if ai_result and ai_result.get("stage") in ("LATE","MID"):
                _stage_now = ai_result.get("stage")
                if from_evaluation or sniper_triggered or primary_pattern in ("5m Multi-TF Sniper", "Yellow Circle Sniper"):
                    logger.info(f"{coin} AI flagged {_stage_now}, but 5m Sniper confirms live entry. Firing Signal.")
                    penalty_notes.append(f"AI Override (5m Sniper Confirmed Live Entry, was {_stage_now})")
                else:
                    logger.info(f"{coin} AI flagged stage {_stage_now} — Breakout only sends EARLY, logging as retest candidate instead")
                    highs_r=[float(k[2]) for k in klines_15m]; lows_r=[float(k[3]) for k in klines_15m]
                    _primary_late = setup["pattern"].split(" + ")[0]
                    _precise_level_late = None
                    if _primary_late in ("Double Top","Double Bottom"):
                        vols_r=[float(k[5]) for k in klines_15m]
                        _avg_vol_late = sum(vols_r[-20:]) / 20 if len(vols_r) >= 20 else 1.0
                        if _primary_late == "Double Bottom":
                            _fired_late, _lvl_late = detect_double_bottom_pro(highs_r, lows_r, closes, vols_r, entry, _avg_vol_late)
                        else:
                            _fired_late, _lvl_late = detect_double_top_pro(highs_r, lows_r, closes, vols_r, entry, _avg_vol_late)
                        if _fired_late and _lvl_late > 0:
                            _precise_level_late = _lvl_late
                    log_retest_candidate(coin,setup["symbol"],setup["direction"],closes,highs_r,lows_r,setup["pattern"],precise_level=_precise_level_late)
                    coin_cooldowns[coin]=get_ist_datetime()+timedelta(minutes=20)
                    return False
        else:
            logger.info(f"{coin} grade is {grade} ({pts}pts, not A/A+) — executing on pure code, no AI call")

    price_range=(max(closes[-10:])-min(closes[-10:]))/10
    eta=int(abs(tp-entry)/(price_range if price_range>0 else 0.001)*15)
    eta=max(30,min(eta,1440)); setup["eta_minutes"]=eta
    expiry_minutes=INSTANT_EXPIRY_MINUTES if is_instant else SIGNAL_EXPIRY_MINUTES
    expiry_time=get_ist_datetime()+timedelta(minutes=expiry_minutes)
    expiry_str=expiry_time.strftime("%I:%M %p IST")
    mom=(closes[-1]-closes[-3])/closes[-3]*100
    rsi_val=calculate_rsi(closes)
    risk_pct = RISK_PCT_BY_GRADE["A+"] if "A+" in grade else RISK_PCT_BY_GRADE["A"] if "A" in grade else RISK_PCT_BY_GRADE["B"] if "B" in grade else RISK_PCT_BY_GRADE["default"]
    pos_size=get_fixed_fractional_size(risk_pct, entry, sl, lev)
    sl_pct=abs(entry-sl)/entry*100; tp_pct=abs(tp-entry)/entry*100
    rr_ratio=tp_pct/sl_pct if sl_pct>0 else 0
    tf_map={3:"4h + 1h  ✅✅",2:"4h Only  ✅",1:"1h Only  ⚡",0:"Counter  ⚠️"}
    tf_label=tf_map.get(tf_score,"N/A")
    cond_em={"bull":"Bullish 📈","bear":"Bearish 📉","sideways":"Sideways ➡️"}.get(market_condition,"")
    _regime_label_map = {"TRENDING": "Trending 🌊", "SQUEEZE": "Squeeze 🌀", "CHOPPY": "Choppy ⚠️", "RANGE_BOUND": "Range-bound ↔️"}
    _coin_regime_text = _regime_label_map.get(_coin_regime, "N/A (macro 4H setup)")
    if is_instant: sig_type="⚡ INSTANT SIGNAL"
    elif is_river: sig_type="🌊 LAB SIGNAL"
    else:          sig_type="🔥 VERIFIED SETUP"
    dir_arrow="🟢 LONG  ▲" if setup["direction"]=="BUY" else "🔴 SHORT ▼"
    grade_em="🏆" if "A+" in grade else "🍀" if " A" in grade else "🥈" if "B" in grade else "🥉"
    cond_icon="📈" if market_condition=="bull" else "📉" if market_condition=="bear" else "➡️"

    filled=min(int(setup["setup_score"]/10),10)
    score_bar="█"*filled+"░"*(10-filled)

    max_pts=22
    grade_filled=min(int(pts/max_pts*10),10)
    grade_bar="█"*grade_filled+"░"*(10-grade_filled)

    msg  = f"{'⚡' if is_instant else '🔥'} <b>{sig_type}</b>\n"
    msg += f"┌─────────────────────────────────┐\n"
    msg += f"│  ⚙️  TRADING SIGNAL MASTER v32G  │\n"
    msg += f"└─────────────────────────────────┘\n\n"
    msg += f"  🏗️ Engine: {get_engine_label(setup['pattern'].split(' + ')[0])}\n"
    msg += f"  🪙 <b>{coin}</b>  {dir_arrow}  🔧 <b>{lev}x Leverage</b>\n"
    msg += f"  {cond_icon} Market: <b>{cond_em}</b>\n"
    msg += f"  🌊 Regime ({coin}): <b>{_coin_regime_text}</b>\n\n"

    _is_genuine_scout = (not setup.get("is_macro")) and (not setup["pattern"].startswith("Metals MTF"))

    if _is_genuine_scout:
        # LEAN FORMAT (this round, explicit instruction): every genuine
        # Scout signal now uses the exact same template
        # check_retest_triggers() already used for its own path —
        # confirmed those two paths had drifted into visibly different
        # message styles, which was the actual source of "does this
        # look like a different engine" confusion. Radar (is_macro)
        # and the SIGNAL ENGINE fallback are untouched below.
        msg += f"  ┌── TRADE LEVELS ─────────────┐\n"
        msg += f"  │  💰 Entry      <code>{format_price(entry)}</code>\n"
        msg += f"  │  🎯 Target     <code>{format_price(tp)}</code>  <i>+{tp_pct:.2f}%</i>\n"
        msg += f"  │  🛑 Stop       <code>{format_price(sl)}</code>  <i>-{sl_pct:.2f}%</i>\n"
        msg += f"  └─────────────────────────────┘\n\n"
        msg += f"  📈 Max Profit : <b>+{profit_target:.1f}%</b>\n"
        msg += f"  ⚖️  Risk/Reward: <b>1 : {rr_ratio:.1f}</b>\n"
        msg += f"  💼 Position   : <b>{pos_size:.1f}% of margin</b>\n"
        msg += f"  📊 Volume     : <b>{vol_ratio:.1f}x avg</b>\n"
    else:
        msg += f"  ┌── TRADE LEVELS ─────────────┐\n"
        msg += f"  │  💰 Entry      <code>{format_price(entry)}</code>\n"
        msg += f"  │  🎯 Target     <code>{format_price(tp)}</code>  <i>+{tp_pct:.2f}%</i>\n"
        msg += f"  │  🛑 Stop       <code>{format_price(sl)}</code>  <i>-{sl_pct:.2f}%</i>\n"
        res_dist=abs(res-entry)/entry*100; sup_dist=abs(entry-sup)/entry*100

        def _break_prob(dist_pct, favourable_dir):
            """Heuristic probability that price breaks through this level."""
            dist_score = max(0, 50 - dist_pct*8)
            mom_score = mom * 3 if favourable_dir else -mom * 3
            adx_score = (adx_val - 20) * 0.6
            vol_score = 8 if vol_ok else -4
            if favourable_dir:
                rsi_score = (rsi_val - 50) * 0.4
            else:
                rsi_score = (50 - rsi_val) * 0.4
            prob = 35 + dist_score*0.4 + mom_score + adx_score + vol_score + rsi_score
            return max(5, min(95, prob))

        res_break_pct = _break_prob(res_dist, favourable_dir=True)
        sup_break_pct = _break_prob(sup_dist, favourable_dir=False)
        msg += f"  │  🚧 Resistance <code>{format_price(res)}</code>  <i>{res_dist:.2f}% away</i>  •  Break: <b>{res_break_pct:.0f}%</b>\n"
        msg += f"  │  🛡️ Support    <code>{format_price(sup)}</code>  <i>{sup_dist:.2f}% away</i>  •  Break: <b>{sup_break_pct:.0f}%</b>\n"
        msg += f"  └─────────────────────────────┘\n\n"

        msg += f"  📈 Max Profit : <b>+{profit_target:.1f}%</b>\n"
        msg += f"  ⚖️  Risk/Reward: <b>1 : {rr_ratio:.1f}</b>\n"
        msg += f"  💼 Position   : <b>{pos_size:.1f}% of margin</b>  (risking {risk_pct:.1f}% of equity if SL hits)\n\n"


        msg += f"  ┌── CONFIRMATIONS ────────────┐\n"
        msg += f"  │  📡 TF   : {tf_label}\n"
        st_icon="✅✅" if st_ok else "⚠️"
        msg += f"  │  🌀 ST   : {st_icon}  VWAP: {'✅' if vwap_ok else '⚠️'}\n"
        vol_icon="✅" if vol_ratio>=1.5 else "⚠️" if vol_ratio>=1.2 else "➖"
        exchange_tag=f" (Led by {lead_exchange} 🌍)" if lead_exchange!="Binance" else ""
        msg += f"  │  📊 Vol  : {vol_icon} {vol_ratio:.2f}x avg{exchange_tag}\n"
        msg += f"  │  📌 Pat  : {setup['pattern']}\n"
        if setup.get("geometry_notes"):
            msg += f"  │  📐 Geo  : {setup['geometry_notes']}\n"
        msg += f"  │  📊 RSI  : {rsi_val:.1f}   ADX: {adx_val:.1f}   Mom: {mom:+.2f}%\n"
        if zone_ok: msg += f"  │  📍 Zone : ✅ {'Demand' if setup['direction']=='BUY' else 'Supply'}\n"
        if div=="BULLISH_DIV":   msg += f"  │  🔀 Div  : 🟢 Bullish RSI Divergence\n"
        elif div=="BEARISH_DIV": msg += f"  │  🔀 Div  : 🔴 Bearish RSI Divergence\n"
        btc_em = "👑" if btc_aligned else "➖"
        btc_trend_label = "Bullish" if btc_1h_trend==1 else "Bearish" if btc_1h_trend==-1 else "Neutral"
        msg += f"  │  {btc_em} BTC   : {'Aligned' if btc_aligned else 'Not aligned'} ({btc_trend_label} 1h)\n"
        ms_bias_em = "📈" if ms["bias"]=="bullish" else "📉" if ms["bias"]=="bearish" else "➡️"
        hh_str = "HH✅" if ms.get("hh") else "HH❌"
        hl_str = "HL✅" if ms.get("hl") else "HL❌"
        lh_str = "LH✅" if ms.get("lh") else "LH❌"
        ll_str = "LL✅" if ms.get("ll") else "LL❌"
        if setup["direction"] == "BUY":
            struct_str = f"{hh_str} {hl_str}"
        else:
            struct_str = f"{lh_str} {ll_str}"
        bos_str = "  🔥BOS" if ms["bos"] else ""
        msg += f"  │  🏗️ MS   : {ms_bias_em} {struct_str}{bos_str}\n"
        msg += f"  └─────────────────────────────┘\n\n"

        m1_pnl = profit_target*0.30; m2_pnl = profit_target*0.60; m3_pnl = profit_target*0.85
        def _sl_lock_price(target_pnl, lock_ratio):
            gain_price = abs(price_at_pnl(entry, setup["direction"], lev, target_pnl) - entry)
            locked = gain_price * lock_ratio
            return entry+locked if setup["direction"]=="BUY" else entry-locked
        ms1=format_price(_sl_lock_price(m1_pnl, 0.0))
        ms2=format_price(_sl_lock_price(m2_pnl, 0.5))
        ms3=format_price(_sl_lock_price(m3_pnl, 0.8))
        msg += f"  ┌── MILESTONE PLAN ───────────┐\n"
        msg += f"  │  🎯 +{m1_pnl:.1f}%  → SL to <code>{ms1}</code>  <i>(breakeven)</i>\n"
        msg += f"  │  🔥 +{m2_pnl:.1f}%  → SL to <code>{ms2}</code>  <i>(lock 50%)</i>\n"
        msg += f"  │  🚀 +{m3_pnl:.1f}%  → SL to <code>{ms3}</code>  <i>(lock 80%)</i>\n"
        msg += f"  │  🏁 Final Target: +{profit_target:.1f}%\n"
        msg += f"  └─────────────────────────────┘\n\n"

        if ai_result:
            v_em="✅" if ai_result["verdict"]=="CLEAN" else "⚠️"
            c_em="🟢" if ai_result["confidence"]=="HIGH" else "🟡" if ai_result["confidence"]=="MEDIUM" else "🔴"
            stage_em={"EARLY":"🌱","MID":"🔥","LATE":"⏰"}.get(ai_result.get("stage","UNKNOWN"),"❔")
            msg+=f"\n  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            msg+=f"  🧠 <b>AI ANALYSIS</b>\n"
            if ai_result.get("trade")==False:
                msg+=f"  ⚠️ <b>AI said TRADE:NO but STAGE:MID — sent for your review, not an AI approval</b>\n"
            msg+=f"  {v_em} Pattern: <b>{ai_result['verdict']}</b>  {c_em} Confidence: <b>{ai_result['confidence']}</b>\n"
            if ai_result.get("stage") and ai_result["stage"]!="UNKNOWN":
                msg+=f"  {stage_em} Stage: <b>{ai_result['stage']}</b>\n"
            if ai_result.get("eta_read"):
                msg+=f"  ⏱️ {ai_result['eta_read']}\n"
            if ai_result['reasoning']:
                msg+=f"  💡 {ai_result['reasoning']}\n"
            if penalty_notes:
                msg+=f"  📉 Score adj: {', '.join(penalty_notes)}\n"
    msg += f"  ⏳ ETA: ~{eta} min  •  ⏰ Exp: {expiry_str}\n"
    msg += f"  🕐 {get_ist_time()}"
    setup.update({"entry":entry,"sl":sl,"tp":tp,"original_tp":tp,"timestamp":get_ist_datetime(),
                  "expires_at":expiry_time,"reversal_alerted":False,"breakeven_sent":False,
                  "partial_tp_taken":False,"milestones_sent":[],"tf_score":tf_score,
                  "market_condition":market_condition,"eta_minutes":eta,
                  "profit_target":profit_target,"pos_size":pos_size})
    pending_signals[coin]=setup
    reply_markup={"inline_keyboard":[[
        {"text":"✅ Activate Trade","callback_data":f"ACTIVATE_{coin}"},
        {"text":"❌ Ignore","callback_data":f"IGNORE_{coin}"}
    ]]}
    if CHARTS_AVAILABLE:
        chart_zone_low = chart_zone_high = None
        if zone_ok:
            zone_key = "demand" if setup["direction"] == "BUY" else "supply"
            for z in zones.get(zone_key, [])[-5:]:
                if z["low"]*0.995 <= entry <= z["high"]*1.005:
                    chart_zone_low, chart_zone_high = z["low"], z["high"]
                    break
        opp_zone_low = opp_zone_high = None
        opp_zone_is_tp = False
        opp_key = "supply" if setup["direction"] == "BUY" else "demand"
        opp_candidates = zones.get(opp_key, [])
        for z in opp_candidates:
            if z["low"]*0.995 <= tp <= z["high"]*1.005:
                opp_zone_low, opp_zone_high = z["low"], z["high"]
                opp_zone_is_tp = True
                break
        if opp_zone_low is None and opp_candidates:
            if setup["direction"] == "BUY":
                above = [z for z in opp_candidates if z["low"] > entry]
                if above:
                    nearest = min(above, key=lambda z: z["low"])
                    opp_zone_low, opp_zone_high = nearest["low"], nearest["high"]
            else:
                below = [z for z in opp_candidates if z["high"] < entry]
                if below:
                    nearest = max(below, key=lambda z: z["high"])
                    opp_zone_low, opp_zone_high = nearest["low"], nearest["high"]
        chart_path = generate_signal_chart(
            setup["symbol"], _sl_klines, entry, sl, tp, setup["direction"], coin,
            interval=_chart_interval,
            pattern_name=setup["pattern"], zone_ok=zone_ok,
            zone_low=chart_zone_low, zone_high=chart_zone_high,
            has_bos=ms["bos"], has_sweep=is_sweep, lev=lev, profit_target=profit_target,
            st_ok=st_ok, vwap_ok=vwap_ok, vol_ratio=vol_ratio, adx_val=adx_val, rsi_val=rsi_val,
            sup=sup, res=res, opp_zone_low=opp_zone_low, opp_zone_high=opp_zone_high,
            opp_zone_is_tp=opp_zone_is_tp
        )
        if chart_path:
            send_telegram_photo(chart_path)
    result=send_telegram(msg,reply_markup=reply_markup)
    if result:
        sent_coins.append(coin)
        coin_cooldowns[coin]=get_ist_datetime()+timedelta(minutes=eta)
        save_pending_signals()
        logger.info(f"Signal sent: {coin}|{setup['direction']}|Score:{setup['setup_score']}|ETA:{eta}m")
        return True
    else:
        with trade_lock:
            if coin in pending_signals: del pending_signals[coin]
        return False

def check_active_trades():
    with trade_lock:
        trades_to_check = list(active_trades.items())
    for coin,trade in trades_to_check:
        price=get_price(trade["symbol"])
        if not price: continue
        hit=None
        if trade["direction"]=="BUY":
            pnl=((price-trade["entry"])/trade["entry"])*100*trade["leverage"]
        else:
            pnl=((trade["entry"]-price)/trade["entry"])*100*trade["leverage"]
        klines_check=get_klines(trade["symbol"],"15m",60)
        update_trailing_sl(coin,trade,price,klines_check)
        check_profit_milestones(coin,trade,price,pnl)
        if not trade.get("reversal_alerted",False):
            _reversal_pattern = trade.get("pattern", "").split(" + ")[0]
            _reversal_native_interval = {
                "Yellow Circle Sniper": "5m",
                "5m Multi-TF Sniper": "5m",
                "Order Flow Sniper": "15m",
            }.get(_reversal_pattern)
            if _reversal_native_interval == "1d":
                klines = get_klines(trade["symbol"], "1d", 60)
                _ema_period = 20
                _reversal_tolerance = 1.5
            elif _reversal_native_interval and _reversal_native_interval != "15m":
                klines = get_klines(trade["symbol"], _reversal_native_interval, 60)
                _ema_period = 20
                _reversal_tolerance = 1.5
            else:
                klines = klines_check
                _ema_period = 50
                _reversal_tolerance = 1.5
            if klines and len(klines)>=(_ema_period+1):
                closes=[float(x[4]) for x in klines]
                ema50_prev=calculate_ema(closes[:-1],_ema_period)
                if ema50_prev:
                    atr_prev = calculate_atr(klines[:-1], 14)
                    if atr_prev and atr_prev > 0:
                        if trade["direction"]=="BUY":
                            rev_threshold = ema50_prev - atr_prev
                            rev_threshold = max(rev_threshold, trade["sl"])
                            rev = closes[-2] < rev_threshold
                        else:
                            rev_threshold = ema50_prev + atr_prev
                            rev_threshold = min(rev_threshold, trade["sl"])
                            rev = closes[-2] > rev_threshold
                    else:
                        rev=((trade["direction"]=="BUY" and closes[-2]<ema50_prev*0.985) or
                             (trade["direction"]=="SELL" and closes[-2]>ema50_prev*1.015))
                    if rev:
                        hit="REVERSAL"
                        active_trades[coin]["reversal_alerted"]=True
                        active_trades[coin]["_reversal_interval_used"]=_reversal_native_interval or "15m"
                        active_trades[coin]["_reversal_ema_used"]=_ema_period
                        save_active_trades()
        if trade.get("timestamp"):
            hours_open=(get_ist_datetime()-trade["timestamp"]).total_seconds()/3600
            _eta_hours = trade.get("eta_minutes", 60) / 60
            squeeze_start_hours = max(6, _eta_hours * 0.4)
            timeout_hours = max(12, _eta_hours * 0.9)
            if hours_open>squeeze_start_hours and "p1" not in trade.get("milestones_sent",[]):
                time_decay_factor=min((hours_open-squeeze_start_hours)/(timeout_hours-squeeze_start_hours) if timeout_hours>squeeze_start_hours else 1.0,1.0)
                original_tp_ref=trade.get("original_tp",trade["tp"])
                original_target_dist=abs(original_tp_ref-trade["entry"])
                squeezed_dist=original_target_dist*(1.0-(time_decay_factor*0.40))
                if trade["direction"]=="BUY":
                    active_trades[coin]["tp"]=trade["entry"]+squeezed_dist
                else:
                    active_trades[coin]["tp"]=trade["entry"]-squeezed_dist
                save_active_trades()
            if hours_open>timeout_hours and "p1" not in trade.get("milestones_sent",[]):
                hit="TIMEOUT"
        recent_highs=[float(k[2]) for k in klines_check[-2:]] if klines_check else [price]
        recent_lows=[float(k[3]) for k in klines_check[-2:]] if klines_check else [price]
        highest_wick=max(recent_highs); lowest_wick=min(recent_lows)
        exit_price=price
        if trade["direction"]=="BUY":
            if highest_wick>=trade["tp"]:
                hit="WIN"; exit_price=trade["tp"]
            elif lowest_wick<=trade["sl"]:
                hit="LOSS"; exit_price=trade["sl"]
        else:
            if lowest_wick<=trade["tp"]:
                hit="WIN"; exit_price=trade["tp"]
            elif highest_wick>=trade["sl"]:
                hit="LOSS"; exit_price=trade["sl"]
        if trade["direction"]=="BUY":
            pnl=((exit_price-trade["entry"])/trade["entry"])*100*trade["leverage"]
        else:
            pnl=((trade["entry"]-exit_price)/trade["entry"])*100*trade["leverage"]
        if hit:
            _should_delete_trade = True
            # Safe defaults computed before the risky section below, so
            # the notification always has what it needs even if the
            # journal/stats write fails partway through.
            primary = trade.get("pattern", "Unknown").split(" + ")[0]
            duration = ""
            if trade.get("timestamp"):
                mins = int((get_ist_datetime()-trade["timestamp"]).total_seconds()/60)
                duration = f"{mins} mins"
            pnl_result = "WIN" if pnl >= 0 else "LOSS"

            # JOURNAL/STATS RECORDING — deliberately isolated in its own
            # try/except (this round, real fix — confirmed the bug by
            # tracing the control flow, not guessed): this used to be
            # the first thing inside the SAME try block as the actual
            # notification send, with _should_delete_trade already set
            # True before entering. Any exception here — file I/O in
            # save_journal(), anything in learn_from_trade() — jumped
            # straight to the outer except, which never reset
            # _should_delete_trade, so the trade got silently deleted
            # with the close notification NEVER EVEN ATTEMPTED. Very
            # likely exactly the missing-notification issue reported.
            # A journal-write failure can now never block the actual
            # Telegram message, which is what the user is relying on.
            try:
                pos_size = trade.get("pos_size", 5.0)
                port_pnl = (pos_size / 100) * pnl
                with trade_lock:
                    _stats_key = "Pre-Breakout Macro" if primary.startswith("Pre-Breakout Macro") else "Metals MTF" if primary.startswith("Metals MTF") else primary
                    if _stats_key in pattern_stats:
                        pattern_stats[_stats_key]["signals"]+=1
                        pattern_stats[_stats_key]["total_pnl"]+=port_pnl
                        pattern_stats[_stats_key]["wins" if pnl_result=="WIN" else "losses"]+=1
                    increment_daily_losses(pnl)
                    if hit=="LOSS" and pnl_result=="LOSS":
                        coin_cooldowns[coin]=get_ist_datetime()+timedelta(hours=4)
                    elif hit=="TIMEOUT":
                        coin_cooldowns[coin]=get_ist_datetime()+timedelta(hours=2)
                    elif hit=="REVERSAL":
                        coin_cooldowns[coin]=get_ist_datetime()+timedelta(hours=3)
                    mc=trade.get("market_condition","bull")
                    _entry_r = trade.get("entry", 0)
                    _sl_r = trade.get("sl", 0)
                    _risk_pct_r = abs(_entry_r - _sl_r) / _entry_r * 100 if _entry_r > 0 and _sl_r > 0 else 0
                    r_multiple = (pnl / _risk_pct_r) if _risk_pct_r > 0 else None
                    trade_journal.append({"date":str(datetime.now(IST).date()),"coin":coin,
                        "direction":trade["direction"],"pattern":primary,
                        "entry":trade["entry"],"exit":exit_price,"pnl":pnl,"port_pnl":port_pnl,"result":pnl_result,
                        "exit_reason":hit,"time_to_m1_mins":trade.get("time_to_m1_mins"),
                        "duration":duration,"tf_score":trade.get("tf_score",0),"market_condition":mc,
                        "r_multiple":r_multiple})
                    save_journal(); learn_from_trade(coin,_stats_key,pnl_result,pnl,mc,trade.get("tf_score",0))
            except Exception as e:
                logger.error(f"Journal/stats recording failed for {coin} close (notification will still be attempted): {e}")

            # NOTIFICATION — always reached now regardless of whether
            # journal/stats recording above succeeded.
            try:
                em="✅" if pnl_result=="WIN" else "⏰" if hit=="TIMEOUT" else "🔄" if hit=="REVERSAL" else "🛑"
                title_word="WON" if pnl_result=="WIN" else "TIME STOP" if hit=="TIMEOUT" else "THESIS CUT" if hit=="REVERSAL" else "CLOSED"
                if hit=="LOSS" and pnl>=0:
                    title_word="SCRATCHED (BREAKEVEN)"
                    em="🛡️"
                _rev_interval_label = trade.get("_reversal_interval_used", "15m")
                _rev_ema_label = trade.get("_reversal_ema_used", 50)
                _close_msg = (
                    f"{em} <b>TRADE {title_word} — {coin}</b>\n"
                    f"⚙️ <b>TRADING SIGNAL MASTER v32G</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    + (f"⏰ Momentum thesis didn't play out — sat flat {duration} without\n"
                       f"   reaching the first milestone. Closed to free up capital.\n\n" if hit=="TIMEOUT" else "")
                    + (f"🔄 Dynamic Thesis Cut — price broke the {_rev_interval_label} EMA{_rev_ema_label} against\n"
                       f"   the trade's direction. The original entry thesis is\n"
                       f"   invalidated, closed here instead of riding it to the\n"
                       f"   structural stop.\n\n" if hit=="REVERSAL" else "")
                    + f"🪙 <b>{coin}</b>  {'🟢' if trade['direction']=='BUY' else '🔴'} {trade['direction']}\n"
                    f"📌 Pattern: {primary}\n"
                    f"🏗️ Engine: {get_engine_label(primary)}\n\n"
                    f"💰 Entry: <code>{format_price(trade['entry'])}</code>\n"
                    f"📍 Exit:  <code>{format_price(exit_price)}</code>\n"
                    f"⏱️ Duration: {duration}\n\n"
                    f"📈 <b>PnL: {fmt_pnl(pnl)}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🕐 {get_ist_time()}"
                )
                global failed_close_notifications
                if not send_telegram(_close_msg):
                    _now_retry = get_ist_datetime()
                    _first_failed = failed_close_notifications.get(coin, {}).get("first_failed_at", _now_retry)
                    _minutes_failing = (_now_retry - _first_failed).total_seconds() / 60
                    if _minutes_failing < 30:
                        failed_close_notifications[coin] = {"msg": _close_msg, "first_failed_at": _first_failed}
                        logger.error(f"Close notification failed for {coin} (failing {_minutes_failing:.1f} min) — queued for retry next cycle, NOT yet deleted from active_trades.")
                        _should_delete_trade = False
                    else:
                        logger.error(f"Close notification for {coin} failed for 30+ minutes — deleting anyway per the bounded guarantee (never permanently consume a MAX_ACTIVE_TRADES slot).")
                        failed_close_notifications.pop(coin, None)
                elif coin in failed_close_notifications:
                    failed_close_notifications.pop(coin, None)
            except Exception as e:
                logger.error(f"Error processing trade close for {coin}: {e}")
            finally:
                if _should_delete_trade:
                    with trade_lock:
                        if coin in active_trades:
                            del active_trades[coin]
                save_active_trades(); save_trade_history()
                cloud_save_journal(); cloud_save_pattern_stats(); cloud_save_active_trades()

def poll_telegram():
    global last_update_id

    try:
        res = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates", timeout=10)
        if res.status_code == 200:
            updates = res.json().get("result", [])
            if updates:
                last_id = updates[-1]["update_id"]
                requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates", params={"offset": last_id + 1}, timeout=10)
                last_update_id = last_id
                logger.info(f"Flushed {len(updates)} stale Telegram updates on startup. Starting clean.")
    except Exception as e:
        logger.error(f"Failed to flush Telegram queue on startup: {e}")

    while True:
        try:
            params={}
            if last_update_id is not None: params["offset"]=last_update_id+1
            res=requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                             params=params,timeout=15)
            if res.status_code!=200: time.sleep(2); continue
            for update in res.json().get("result",[]):
                last_update_id=update["update_id"]
                if "callback_query" in update:
                    cb=update["callback_query"]
                    data=cb.get("data","")
                    cbid=cb.get("id","")
                    answer_callback(cbid,"Processing...")
                    logger.info(f"Callback received: data={data} pending={list(pending_signals.keys())}")
                    if data and "_" in data:
                        action=data.split("_",1)[0]
                        coin=data.split("_",1)[1]
                        if action=="ACTIVATE":
                            with trade_lock:
                                if coin not in pending_signals:
                                    setup = None
                                else:
                                    setup = pending_signals.pop(coin)
                            if setup is None:
                                send_telegram(f"⏰ <b>{BOT_HEADER}</b>\nSignal for {coin} expired.\nWait for next signal.")
                                logger.warning(f"ACTIVATE failed: {coin} not in pending={list(pending_signals.keys())}")
                            else:
                                lp=get_price(setup.get("symbol",coin+"USDT"))
                                if lp and lp>0: setup["entry"]=lp
                                setup["breakeven_sent"]=False
                                setup["partial_tp_taken"]=False
                                setup["reversal_alerted"]=False
                                setup["milestones_sent"]=[]
                                setup["timestamp"]=get_ist_datetime()
                                setup["expires_at"]=None
                                with trade_lock:
                                    active_trades[coin]=setup
                                save_active_trades(); save_pending_signals()
                                t=active_trades[coin]
                                ep=t.get("entry",0); sl_p=t.get("sl",0); tp_p=t.get("tp",0)
                                lev=t.get("leverage",5); dirn=t.get("direction","?"); pat=t.get("pattern","?")
                                sl_pct=abs(ep-sl_p)/ep*100 if ep>0 else 0
                                tp_pct=abs(tp_p-ep)/ep*100 if ep>0 else 0
                                rr=round(tp_pct/sl_pct,1) if sl_pct>0 else 0
                                if dirn=="BUY":
                                    sl_10=format_price(ep); sl_20=format_price(ep+(tp_p-ep)*0.5); sl_35=format_price(ep+(tp_p-ep)*0.75)
                                else:
                                    sl_10=format_price(ep); sl_20=format_price(ep-(ep-tp_p)*0.5); sl_35=format_price(ep-(ep-tp_p)*0.75)
                                dir_em2 = "🟢 LONG" if dirn=="BUY" else "🔴 SHORT"
                                send_telegram(
                                    f"🚀 <b>TRADE ACTIVATED</b>\n"
                                    f"⚙️ <b>TRADING SIGNAL MASTER v32G</b>\n"
                                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                                    f"🪙 <b>{coin}</b>  {dir_em2}  🔧 <b>{lev}x</b>\n"
                                    f"⚖️ Risk/Reward: <b>1:{rr}</b>\n\n"
                                    f"💰 <b>Entry</b>    <code>{format_price(ep)}</code>\n"
                                    f"🎯 <b>Target</b>   <code>{format_price(tp_p)}</code>  (+{tp_pct:.1f}%)\n"
                                    f"🛑 <b>Stop</b>     <code>{format_price(sl_p)}</code>  (-{sl_pct:.1f}%)\n\n"
                                    f"📌 Pattern: {pat}\n"
                                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                                    f"📋 <b>Milestone Plan:</b>\n"
                                    f"  🎯 +10% → Move SL to <code>{sl_10}</code>\n"
                                    f"  🎯 +20% → Move SL to <code>{sl_20}</code>\n"
                                    f"  🚀 +35% → Move SL to <code>{sl_35}</code>\n"
                                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                                    f"✏️ Set your trade on CoinDCX now!\n"
                                    f"🕐 {get_ist_time()}"
                                )
                                logger.info(f"ACTIVATED: {coin}|{dirn}|Entry:{ep}|{lev}x")
                        elif action=="IGNORE":
                            with trade_lock:
                                if coin in pending_signals: del pending_signals[coin]
                            save_pending_signals()
                            send_telegram(f"❌ <b>{BOT_HEADER}</b>\n{coin} signal ignored.")
                elif "message" in update:
                    txt=update["message"].get("text","").strip().lower()
                    txt_slash=txt
                    txt_clean = txt.replace('\ufe0f','').replace('\ufe0e','').strip()
                    if   txt_slash=="/trades":   safe_send(get_active_trades_text,"📊 Trades")
                    elif txt_slash=="/pending":
                        if pending_signals:
                            msg=f"{_H('PENDING SIGNALS','⏳')}\n\n"
                            for c,s in pending_signals.items():
                                exp=s.get("expires_at"); exp_str=exp.strftime("%I:%M %p IST") if isinstance(exp,datetime) else "N/A"
                                dirn_em="🟢 LONG" if s.get("direction")=="BUY" else "🔴 SHORT"
                                msg+=(f"  🪙 <b>{c}</b>  {dirn_em}\n"
                                      f"  ◆ {s.get('pattern','?')}\n"
                                      f"  Score: {s.get('setup_score',0):.0f}  ⏰ {exp_str}\n\n")
                            msg+=f"  🕐 {get_ist_time()}"
                            send_telegram(msg)
                        else: send_telegram(f"{_H('PENDING SIGNALS','⏳')}\n\n  ⚪ No pending signals.\n\n  🕐 {get_ist_time()}")
                    elif txt_slash=="/retests":   safe_send(get_retest_watchlist_text,"👀 Retests")
                    elif txt_slash=="/stats":    safe_send(get_pattern_stats_text,"📈 Stats")
                    elif txt_slash=="/summary":  safe_send(get_detailed_summary_text,"📅 Summary")
                    elif txt_slash=="/expectancy":  safe_send(get_expectancy_report_text,"📊 Expectancy")
                    elif txt_slash=="/streak":   safe_send(get_streak_text,"🔥 Streak")
                    elif txt_slash=="/best":     safe_send(get_best_text,"🏆 Best")
                    elif txt_slash=="/risk":     safe_send(get_risk_text,"🛡️ Risk")
                    elif txt_slash=="/learn":    safe_send(get_learning_text,"🧠 Learn")
                    elif txt_slash=="/journal":  safe_send(get_journal_text,"📓 Journal")
                    elif txt_slash=="/patterns": safe_send(get_patterns_ranked_text,"🌀 Patterns")
                    elif txt_slash=="/news":
                        send_telegram(f"⚙️ Fetching latest news...")
                        safe_send(get_crypto_news,"📰 News")
                    elif txt_slash=="/gems":    safe_send(cmd_hidden_gems,"💎 Hidden Gems")
                    elif txt_slash=="/metalsstatus":  safe_send(get_metals_status_text,"🥇 Metals Status")
                    elif txt_slash=="/analyst":
                        send_telegram("🧠 AI Analyst reviewing your trades...", parse_mode="")
                        safe_send(ai_analyst_review,"🧠 AI Analyst")
                    elif txt_slash in ("/counsel","/regime"):
                        pass
                    elif txt_slash=="/market":   safe_send(cmd_market,"🌍 Market")
                    elif txt_slash=="/cb":
                        cb_on=check_circuit_breaker()
                        send_telegram(
                            f"{_H('CIRCUIT BREAKER','⚡')}\n\n"
                            f"  Status   : {'🔴 ACTIVE — paused' if cb_on else '🟢 OK — scanning'}\n"
                            f"  Losses   : {daily_losses}/{MAX_DAILY_LOSSES}\n"
                            f"  Resets   : Midnight IST\n\n"
                            f"  🕐 {get_ist_time()}"
                        )
                    elif txt_slash.startswith("/trend"):
                        parts=txt.split(); coin2=parts[1].upper() if len(parts)>1 else "BTC"
                        safe_send(lambda: cmd_trend(coin2),"📉 Trend")
                    elif txt_slash.startswith("/compare"):
                        parts=txt.split(maxsplit=1); coins_str=parts[1].upper() if len(parts)>1 else "BTC ETH SOL"
                        safe_send(lambda: cmd_compare(coins_str),"🆚 Compare")
                    elif txt_slash=="/scan":
                        btc_p=get_price("BTCUSDT"); btc_k=get_klines("BTCUSDT","1h",50)
                        bt_e50=calculate_ema([float(x[4]) for x in btc_k],50) if btc_k else None
                        bt=1 if (btc_p and bt_e50 and btc_p>bt_e50) else -1
                        fng2=get_fear_greed_index(); mc2=detect_market_condition(btc_p,btc_k) if btc_p and btc_k else "sideways"
                        send_telegram(cmd_scan_manual(bt,fng2,mc2))
                    elif txt_slash.startswith("/alert "):
                        parts=txt.split()
                        if len(parts)>=4:
                            try:
                                sym=parts[1].upper(); target=float(parts[2]); direction=parts[3].lower()
                                price_alerts[sym]={"price":target,"direction":direction}; save_alerts()
                                send_telegram(f"🔔 Alert set: {sym} {direction} {format_price(target)}")
                            except Exception: send_telegram("Usage: /alert BTC 95000 above")
                        else: send_telegram("Usage: /alert BTC 95000 above")
                    elif txt_slash=="/alerts":
                        if price_alerts:
                            msg=f"<b>{BOT_HEADER} Alerts</b>\n{S()}\n\n"
                            for sym,a in price_alerts.items(): msg+=f"{sym}: {a['direction']} {format_price(a['price'])}\n"
                            send_telegram(msg)
                        else: send_telegram(f"<b>{BOT_HEADER}</b>\nNo alerts set.")
                    elif txt_slash.startswith("/addmacroevent"):
                        raw = update["message"].get("text","").strip()
                        body = raw[len("/addmacroevent"):].strip()
                        parts = body.split(maxsplit=2)
                        if len(parts) < 2:
                            send_telegram("Usage: /addmacroevent 2026-08-01 18:00 FOMC rate decision")
                        else:
                            date_part, time_part = parts[0], parts[1]
                            label = parts[2] if len(parts) > 2 else ""
                            ev_str = f"{date_part} {time_part}"
                            try:
                                datetime.strptime(ev_str, "%Y-%m-%d %H:%M").replace(tzinfo=IST)
                                SCHEDULED_MACRO_EVENTS.append(ev_str + (f"  # {label}" if label else ""))
                                save_macro_events()
                                send_telegram(f"📅 Macro event added: {ev_str} IST" + (f" — {label}" if label else "") +
                                             f"\nBot will pause new signals ±{MACRO_EVENT_PAUSE_MIN_BEFORE}min around this time.")
                            except Exception:
                                send_telegram("⚠️ Invalid format. Use: /addmacroevent 2026-08-01 18:00 FOMC rate decision\n(date as YYYY-MM-DD, time as 24h HH:MM, IST)")
                    elif txt_slash=="/macroevents":
                        if SCHEDULED_MACRO_EVENTS:
                            msg=f"<b>{BOT_HEADER} Scheduled Macro Events</b>\n{S()}\n\n"
                            for i,ev in enumerate(SCHEDULED_MACRO_EVENTS,1): msg+=f"{i}. {ev}\n"
                            msg+=f"\nUse /clearmacroevents to remove all."
                            send_telegram(msg)
                        else:
                            send_telegram(f"<b>{BOT_HEADER}</b>\nNo scheduled macro events. Add one with:\n/addmacroevent 2026-08-01 18:00 FOMC rate decision")
                    elif txt_slash=="/clearmacroevents":
                        SCHEDULED_MACRO_EVENTS.clear()
                        save_macro_events()
                        send_telegram("🗑️ All scheduled macro events cleared.")
                    elif txt_slash.startswith("/backtest"):
                        parts=txt.split(); bc=(parts[1].upper() if len(parts)>1 else "BTC")+"USDT"
                        send_telegram(f"Running backtest for {bc}...")
                        send_telegram(run_backtest(bc))
                    elif txt_slash in ("/start","/help","/menu"):
                        menu_kb={
                            "keyboard":[
                                [{"text":"📊 Active Trades"}, {"text":"📓 Trade Journal"}, {"text":"🧠 AI Analyst"}],
                                [{"text":"🌍 Market Overview"}, {"text":"🌐 Regime & BTC"}, {"text":"🔍 Check Coin"}],
                                [{"text":"💎 Hidden Gems"}, {"text":"🔥 Squeeze Radar"}, {"text":"👀 Watchlist"}],
                                [{"text":"📅 10-Day Summary"}, {"text":"📈 Pattern Stats"}, {"text":"⚡ CB Status"}]
                            ],
                            "resize_keyboard":True,
                            "persistent":True
                        }
                        send_telegram(
                            f"{_H('TRADING SIGNAL MASTER v32G','⚙️')}\n\n"
                            f"  Tap a command to execute:\n\n"
                            f"  📊 /trades    — Active trades\n"
                            f"  🌍 /market    — Market overview\n"
                            f"  📉 /trend BTC — Trend analysis\n"
                            f"  🆚 /compare BTC ETH — Compare\n"
                            f"  🔬 /backtest BTC — Backtest\n\n"
                            f"  🕐 {get_ist_time()}",
                            reply_markup=menu_kb
                        )
                    elif txt_slash in ("/status","📡 status"):
                        pass
                    elif txt_clean in ("📊 trades","📊 active trades"):   safe_send(get_active_trades_text,"📊 Trades")
                    elif txt_clean=="⏳ pending":
                        if pending_signals:
                            msg=f"{_H('PENDING SIGNALS','⏳')}\n\n"
                            for c,s in pending_signals.items():
                                exp=s.get("expires_at")
                                exp_str=exp.strftime("%I:%M %p IST") if isinstance(exp,datetime) else "N/A"
                                dirn_em="🟢 LONG" if s.get("direction")=="BUY" else "🔴 SHORT"
                                msg+=(f"  🪙 <b>{c}</b>  {dirn_em}\n"
                                      f"  ◆ {s.get('pattern','?')}\n"
                                      f"  Score: {s.get('setup_score',0):.0f}  ⏰ {exp_str}\n\n")
                            msg+=f"  🕐 {get_ist_time()}"
                            send_telegram(msg)
                        else:
                            send_telegram(f"{_H('PENDING SIGNALS','⏳')}\n\n  ⚪ No pending signals right now.\n\n  🕐 {get_ist_time()}")
                    elif txt_clean in ("📈 stats","📈 pattern stats"):    safe_send(get_pattern_stats_text,"📈 Stats")
                    elif txt_clean in ("📅 summary","📅 10-day summary"):  safe_send(get_detailed_summary_text,"📅 Summary")
                    elif txt_clean=="🔥 streak":   safe_send(get_streak_text,"🔥 Streak")
                    elif txt_clean=="🏆 best":     safe_send(get_best_text,"🏆 Best")
                    elif txt_clean in ("🛡️ risk","🛡 risk"):  safe_send(get_risk_text,"🛡 Risk")
                    elif txt_clean=="🧠 learn":    safe_send(get_learning_text,"🧠 Learn")
                    elif txt_clean in ("📓 journal","📓 trade journal"):  safe_send(get_journal_text,"📓 Journal")
                    elif txt_clean=="🌀 patterns": safe_send(get_patterns_ranked_text,"🌀 Patterns")
                    elif txt_clean=="📰 news":
                        send_telegram("⚙️ Fetching latest news...")
                        safe_send(get_crypto_news,"📰 News")
                    elif txt_clean in ("🌍 market","🌍 market overview"):   safe_send(cmd_market,"🌍 Market")
                    elif txt_clean=="🔍 scan":
                        btc_p2=get_price("BTCUSDT"); btc_k2=get_klines("BTCUSDT","1h",50)
                        bt_e2=calculate_ema([float(x[4]) for x in btc_k2],50) if btc_k2 else None
                        bt2=1 if (btc_p2 and bt_e2 and btc_p2>bt_e2) else -1
                        fg2=get_fear_greed_index()
                        mc2=detect_market_condition(btc_p2,btc_k2) if btc_p2 and btc_k2 else "sideways"
                        safe_send(lambda: cmd_scan_manual(bt2,fg2,mc2),"🔍 Scan")
                    elif txt_clean in ("⚡ cb status","⚡ cb"):
                        cb_on=check_circuit_breaker()
                        send_telegram(
                            f"{_H('CIRCUIT BREAKER','⚡')}\n\n"
                            f"  Status   : {'🔴 ACTIVE — scanning paused' if cb_on else '🟢 OK — scanning active'}\n"
                            f"  Losses   : {daily_losses}/{MAX_DAILY_LOSSES}\n"
                            f"  Resets   : Midnight IST\n\n"
                            f"  🕐 {get_ist_time()}"
                        )
                    elif txt_clean in ("📡 status","📡status"):
                        btc_p=get_price("BTCUSDT"); fng=get_fear_greed_index()
                        btc_k=get_klines("BTCUSDT","1h",50)
                        bt_e=calculate_ema([float(x[4]) for x in btc_k],50) if btc_k else None
                        bt=1 if (btc_p and bt_e and btc_p>bt_e) else -1
                        mc=detect_market_condition(btc_p,btc_k) if btc_p and btc_k else "unknown"
                        sess=is_good_trading_session(); sess_premium=is_good_trading_session("BTC"); cb=check_circuit_breaker()
                        btc_crash=is_btc_crashing()
                        send_telegram(
                            f"{_H('LIVE BOT STATUS','📡')}\n\n"
                            f"  {'✅' if sess else '🔴'} Session (regular): {'ACTIVE' if sess else 'DEAD (2-7AM IST)'}\n"
                            f"  {'✅' if sess_premium else '🔴'} Session (premium): {'ACTIVE 24/7' if sess_premium else 'DEAD'}\n"
                            f"  {'✅' if not cb else '🔴'} CB         : {'OK' if not cb else 'ACTIVE — paused'}\n"
                            f"  {'✅' if not btc_crash else '🔴'} BTC Crash  : {'OK' if not btc_crash else 'CRASHING'}\n"
                            f"  {'🟢' if bt==1 else '🔴'} BTC Trend  : {'BULLISH ▲' if bt==1 else 'BEARISH ▼'}\n"
                            f"  📊 Market   : {mc.upper()}\n"
                            f"  😰 F&G      : {fng}\n"
                            f"  📌 Trades   : {len(active_trades)}/{MAX_ACTIVE_TRADES}\n"
                            f"  ⏳ Pending  : {len(pending_signals)}\n"
                            f"  🔒 Cooldowns: {len(coin_cooldowns)} coins\n"
                            f"  📉 Losses   : {daily_losses}/{MAX_DAILY_LOSSES}\n"
                            f"  🎯 Min Score: {MIN_SETUP_SCORE}\n\n"
                            f"  {'🟢 Bot CAN send signals' if sess and not cb else '🔴 Bot BLOCKED'}\n\n"
                            f"  🕐 {get_ist_time()}"
                        )
                    elif txt_clean=="🔔 alerts":
                        if price_alerts:
                            msg=f"{_H('PRICE ALERTS','🔔')}\n\n"
                            for sym,a in price_alerts.items():
                                msg+=f"  🔔 <b>{sym}</b>  {a['direction'].upper()}  <code>{format_price(a['price'])}</code>\n"
                            msg+=f"\n  ➕ /alert BTC 95000 above\n  🕐 {get_ist_time()}"
                            send_telegram(msg)
                        else:
                            send_telegram(f"{_H('PRICE ALERTS','🔔')}\n\n  ⚪ No alerts set.\n\n  ➕ /alert BTC 95000 above\n  🕐 {get_ist_time()}")
                    elif txt_clean.startswith("📉 trend"):
                        parts=txt_clean.split(); coin_t=(parts[-1].upper() if len(parts)>1 and parts[-1].upper()!="TREND" else "BTC")+"USDT"
                        safe_send(lambda: cmd_trend(coin_t),"📉 Trend")
                    elif txt_clean.startswith("🔬 backtest"):
                        parts=txt_clean.split(); bc2=(parts[-1].upper() if len(parts)>1 and parts[-1].upper()!="BACKTEST" else "BTC")+"USDT"
                        send_telegram(f"🔬 Running backtest for <b>{bc2}</b>...")
                        safe_send(lambda: run_backtest(bc2),"🔬 Backtest")
                    elif txt_clean in ("💎 hidden gems","/gems"):
                        safe_send(cmd_hidden_gems,"💎 Hidden Gems")
                    elif txt_clean in ("👀 watchlist","/watchlist"):
                        safe_send(get_retest_watchlist_text,"👀 Watchlist")
                    elif txt_clean in ("🔥 squeeze radar","/squeeze"):
                        safe_send(get_squeeze_radar_text,"🔥 Squeeze Radar")
                    elif txt_clean=="🔍 check coin":
                        send_telegram("🔍 Type <code>/check COIN</code> — e.g. <code>/check FIL</code>", parse_mode="HTML")
                    elif txt_slash.startswith("/check"):
                        parts=txt_clean.split()
                        if len(parts)>1:
                            safe_send(lambda: get_check_coin_text(parts[1]),"🔍 Check Coin")
                        else:
                            send_telegram("🔍 Usage: <code>/check COIN</code> — e.g. <code>/check FIL</code>", parse_mode="HTML")
                    elif txt_clean in ("🧠 ai analyst","/analyst"):
                        send_telegram("🧠 AI Analyst reviewing your trades...", parse_mode="")
                        safe_send(ai_analyst_review,"🧠 AI Analyst")
                    elif txt_clean in ("🔮 counsel","/counsel"):
                        if not active_trades:
                            send_telegram(_H("COUNSEL","🔮")+"\n\n  🌙 No open trades.\n\n  🕐 "+get_ist_time())
                        else:
                            lines=[_H("TRADE COUNSEL","🔮")+"\n"]
                            for coin,t in active_trades.items():
                                symbol=t.get("symbol",coin+"USDT"); price=get_price(symbol)
                                if not price: continue
                                direction=t.get("direction","BUY"); entry=t["entry"]; lev=t.get("leverage",1)
                                pnl=((price-entry)/entry)*100*lev if direction=="BUY" else ((entry-price)/entry)*100*lev
                                dist_tp=abs(t["tp"]-price)/price*100
                                em="🟢" if pnl>=0 else "🔴"
                                lines.append(f"  {em} <b>{coin}</b> {direction} PnL:{pnl:+.1f}% TP:{dist_tp:.1f}% away")
                            lines.append(f"\n  🕐 {get_ist_time()}")
                            send_telegram("\n".join(lines))
                    elif txt_clean in ("🌐 regime","🌐 regime & btc","/regime"):
                        btc_p=get_price("BTCUSDT"); btc_k=get_klines("BTCUSDT","1h",50)
                        adx=calculate_adx(btc_k) if btc_k else 0
                        fng=get_fear_greed_index()
                        mc=detect_market_condition(btc_p,btc_k) if btc_p and btc_k else "sideways"
                        em="📈" if mc=="bull" else "📉" if mc=="bear" else "➡️"
                        send_telegram(
                            _H("MARKET REGIME","🌐")+"\n\n"
                            f"  {em} Regime: <b>{mc.upper()}</b>\n"
                            f"  💪 ADX: {adx:.1f}\n"
                            f"  😰 F&G: {fng}\n"
                            f"  ₿ BTC: <code>${format_price(btc_p) if btc_p else 'N/A'}</code>\n\n"
                            f"  🕐 {get_ist_time()}"
                        )
        except requests.RequestException as e: logger.error(f"Poll network: {e}")
        except Exception as e:                 logger.error(f"Poll error: {e}",exc_info=True)
        time.sleep(2)

def send_hourly_report():
    r=f"<b>{BOT_HEADER} Hourly Report</b>\n{get_ist_time()}\n{S()}\n\n"
    r+=f"Active: {len(active_trades)} | Pending: {len(pending_signals)}\n"
    r+=f"Circuit Breaker: {'ACTIVE' if check_circuit_breaker() else 'OK'}\n\n"
    r+=get_pattern_stats_text()
    send_telegram(r)

def send_live_pnl_update():
    if not active_trades: return
    total_pnl=0.0; wins=losses=0
    msg=f"<b>{BOT_HEADER} Live PnL</b>\n{get_ist_time()}\n{S()}\n\n"
    for coin,t in active_trades.items():
        price=get_price(t["symbol"])
        if not price: continue
        pnl=(((price-t["entry"])/t["entry"])*100*t["leverage"] if t["direction"]=="BUY"
             else ((t["entry"]-price)/t["entry"])*100*t["leverage"])
        total_pnl+=pnl
        if pnl>=3: wins+=1
        elif pnl<=-3: losses+=1
        msg+=f"{coin} {t['direction']} | {fmt_pnl(pnl)}\n"
    total=wins+losses; wr=(wins/total*100) if total>0 else 0
    msg+=f"\n{S()}\nTotal: {fmt_pnl(total_pnl)} | WR: {wr:.1f}%"
    send_telegram(msg)


def generate_weekly_insight():
    today = datetime.now(IST).date()
    wt = [j for j in trade_journal
          if (today - datetime.strptime(j["date"], "%Y-%m-%d").date()).days < 7]
    if not wt: return "Not enough data for weekly insight yet."
    wins   = [t for t in wt if t["result"] == "WIN"]
    losses = [t for t in wt if t["result"] == "LOSS"]
    total  = len(wt)
    wr     = (len(wins) / total * 100) if total > 0 else 0
    day_wins = {}
    for t in wins:
        d = t["date"]; day_wins[d] = day_wins.get(d, 0) + 1
    best_day  = max(day_wins, key=day_wins.get) if day_wins else None
    wp        = [t["pattern"] for t in wins]
    lp        = [t["pattern"] for t in losses]
    best_pat  = Counter(wp).most_common(1)[0][0]  if wp  else None
    worst_pat = Counter(lp).most_common(1)[0][0]  if lp  else None
    sw_losses = sum(1 for t in losses if t.get("market_condition") == "sideways")
    msg  = f"AI Weekly Insight:\n"
    msg += f"{len(wins)}W / {len(losses)}L | WR: {wr:.1f}%\n"
    if best_day:  msg += f"Best day: {best_day}\n"
    if best_pat:  msg += f"Best pattern: {best_pat}\n"
    if worst_pat: msg += f"Most losses from: {worst_pat}\n"
    if sw_losses >= 2:
        msg += f"{sw_losses} losses in sideways — reduce size when BTC ranges\n"
    if wr >= 70:   msg += "Excellent week!"
    elif wr >= 50: msg += "Decent week. Stay disciplined."
    else:          msg += "Tough week. Review learning notes."
    return msg

def send_weekly_report():
    today=datetime.now(IST).date(); week=[today-timedelta(days=i) for i in range(6,-1,-1)]
    wins=losses=0; total_pnl=0.0
    msg=f"<b>{BOT_HEADER} Weekly Report</b>\n{today.strftime('%d %b %Y')}\n{S()}\n\n"
    for day in week:
        dt=[j for j in trade_journal if j.get("date")==str(day)]
        w=sum(1 for t in dt if t["result"]=="WIN"); l=sum(1 for t in dt if t["result"]=="LOSS")
        pnl=sum(t.get("pnl", 0) for t in dt); wins+=w; losses+=l; total_pnl+=pnl
        em="✅" if w>l else "❌" if l>w else "⚪"
        msg+=f"{em} {day.strftime('%a %d')}: {w}W/{l}L {fmt_pnl(pnl)}\n"
    total=wins+losses; wr=(wins/total*100) if total>0 else 0
    msg+=f"\n{S()}\nTotal: {wins}W/{losses}L | WR:{wr:.1f}% | {fmt_pnl(total_pnl)}"
    msg+=f"\n\n{generate_weekly_insight()}"
    send_telegram(msg)

METALS_SYMBOLS = ["XAUUSDT", "XAGUSDT", "COPPERUSDT"]
METALS_SCAN_INTERVAL_SECONDS = 60
METALS_MAX_LEVERAGE = 10
metals_cooldowns = {}
metals_last_status = {}
METALS_COOLDOWN_MINUTES = 30

def get_metals_macro_context(symbol):
    """
    Step 1 (Dynamic Filter, 1D & 4H): boundary context only (major
    daily pivots + 4H ATR), not a hard trend-direction blocker.
    """
    klines_1d = get_klines(symbol, "1d", 30)
    klines_4h = get_klines(symbol, "4h", 50)
    if not klines_1d or len(klines_1d) < 20 or not klines_4h or len(klines_4h) < 20:
        return None
    atr_4h = calculate_atr(klines_4h, 14)
    return {
        "atr_4h": atr_4h,
        "pivot_high": max(float(k[2]) for k in klines_1d[-20:]),
        "pivot_low": min(float(k[3]) for k in klines_1d[-20:]),
    }


def check_near_major_level(price, macro_ctx, direction):
    """The one thing Step 1 still blocks: a trade slamming directly into a major 1D level against itself."""
    if not macro_ctx or not macro_ctx.get("atr_4h"):
        return True
    tol = 0.003
    if direction == "BUY" and abs(price - macro_ctx["pivot_high"]) / price < tol:
        return False
    if direction == "SELL" and abs(price - macro_ctx["pivot_low"]) / price < tol:
        return False
    return True


def get_metals_session_bias(symbol):
    """
    Step 2 (Session Trend, 15m & 1H): direction from 15m VWAP + 20/50
    EMA. Ranging 15m RSI (40-60) allows a mean-reversion trade toward
    the 15m EMA mean, rather than blocking everything.
    """
    klines_15m = get_klines(symbol, "15m", 100)
    if not klines_15m or len(klines_15m) < 60:
        return None, "insufficient 15m history"
    closes = [float(k[4]) for k in klines_15m]
    price = closes[-1]
    ema20, ema50 = calculate_ema(closes, 20), calculate_ema(closes, 50)
    vwap = calculate_vwap(klines_15m[-96:])
    rsi = calculate_rsi(closes)
    if not all([ema20, ema50, vwap, rsi]):
        return None, "indicator calculation failed"
    if 40 <= rsi <= 60:
        if price > ema20:
            return "SELL", f"15m ranging (RSI {rsi:.1f}), price above 20-EMA — mean-reversion short"
        return "BUY", f"15m ranging (RSI {rsi:.1f}), price below 20-EMA — mean-reversion long"
    if price > vwap and ema20 > ema50:
        return "BUY", f"15m price>VWAP, 20-EMA>50-EMA, RSI {rsi:.1f}"
    if price < vwap and ema20 < ema50:
        return "SELL", f"15m price<VWAP, 20-EMA<50-EMA, RSI {rsi:.1f}"
    return None, "15m VWAP/EMA not aligned"


def detect_fair_value_gaps_metals(klines, lookback=40):
    """Fair Value Gap — 3-candle imbalance, candle 2 never touches the gap. Metals-specific rebuild."""
    if len(klines) < 3:
        return []
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    closes = [float(k[4]) for k in klines]
    start = max(2, len(klines) - lookback)
    gaps = []
    for i in range(start, len(klines)):
        c1_high, c1_low = highs[i-2], lows[i-2]
        c3_high, c3_low = highs[i], lows[i]
        if c1_high < c3_low:
            top, bottom, direction = c3_low, c1_high, "BUY"
        elif c1_low > c3_high:
            top, bottom, direction = c1_low, c3_high, "SELL"
        else:
            continue
        disrespected = False
        for j in range(i + 1, len(closes)):
            if direction == "BUY" and closes[j] < bottom:
                disrespected = True; break
            if direction == "SELL" and closes[j] > top:
                disrespected = True; break
        gaps.append({"direction": direction, "top": top, "bottom": bottom,
                      "formed_idx": i, "disrespected": disrespected})
    return gaps


def detect_inverse_fvg_metals(klines, price, lookback=40):
    """IFVG — a disrespected FVG that flips role, live once price retests the flipped zone."""
    gaps = detect_fair_value_gaps_metals(klines, lookback)
    for g in gaps:
        if not g["disrespected"]:
            continue
        ifvg_direction = "SELL" if g["direction"] == "BUY" else "BUY"
        top, bottom = g["top"], g["bottom"]
        if bottom <= price <= top:
            return ifvg_direction, top, bottom
    return None, 0, 0


def find_metals_order_block(klines):
    """Order Block — last opposite candle before a >=2% displacement breaking a 15-candle swing, plus retest confirmation."""
    if len(klines) < 20:
        return None, 0, 0
    opens = [float(k[1]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    closes = [float(k[4]) for k in klines]
    zone_dir, zone_top, zone_bottom = None, 0, 0
    for i in range(len(klines) - 6, max(len(klines) - 26, 0), -1):
        window_end = min(i + 5, len(klines))
        if closes[i] < opens[i]:
            impulse_high = max(highs[i+1:window_end])
            swing_high_before = max(highs[max(0, i-15):i]) if i > 0 else 0
            impulse_pct = (impulse_high - closes[i]) / closes[i] * 100 if closes[i] > 0 else 0
            if impulse_pct >= 2.0 and swing_high_before > 0 and impulse_high > swing_high_before:
                top, bottom = highs[i], lows[i]
                mitigated = any(closes[j] < bottom for j in range(i+1, len(closes)-1))
                if not mitigated:
                    zone_dir, zone_top, zone_bottom = "BUY", top, bottom
                    break
        if closes[i] > opens[i]:
            impulse_low = min(lows[i+1:window_end])
            swing_low_before = min(lows[max(0, i-15):i]) if i > 0 else 0
            impulse_pct = (closes[i] - impulse_low) / closes[i] * 100 if closes[i] > 0 else 0
            if impulse_pct >= 2.0 and swing_low_before > 0 and impulse_low < swing_low_before:
                top, bottom = highs[i], lows[i]
                mitigated = any(closes[j] > top for j in range(i+1, len(closes)-1))
                if not mitigated:
                    zone_dir, zone_top, zone_bottom = "SELL", top, bottom
                    break
    if zone_dir is None or len(klines) < 2:
        return None, 0, 0
    o, h, l, c = opens[-2], highs[-2], lows[-2], closes[-2]
    if zone_dir == "BUY":
        if not (zone_bottom <= l <= zone_top or zone_bottom <= c <= zone_top):
            return None, 0, 0
        level = zone_top
    else:
        if not (zone_bottom <= h <= zone_top or zone_bottom <= c <= zone_top):
            return None, 0, 0
        level = zone_bottom
    if check_retest_rejection_candle(o, h, l, c, zone_dir, level):
        return zone_dir, zone_top, zone_bottom
    return None, 0, 0


def find_metals_5m_pullback_setup(symbol, direction):
    """
    Step 3 (Entry Setup, 5m): pullback to 5m 20-EMA, liquidity sweep,
    IFVG retest, or Order Block retest. RSI extreme guard (>75/<25),
    volume just needs to be non-dormant (>=0.8x SMA20).
    """
    klines_5m = get_klines(symbol, "5m", 60)
    if not klines_5m or len(klines_5m) < 40:
        return None, "insufficient 5m history"
    closes = [float(k[4]) for k in klines_5m]
    highs = [float(k[2]) for k in klines_5m]
    lows = [float(k[3]) for k in klines_5m]
    opens = [float(k[1]) for k in klines_5m]
    vols = [float(k[5]) for k in klines_5m]
    avg_vol = sum(vols[-20:]) / 20
    price = closes[-1]
    rsi = calculate_rsi(closes)
    ema20 = calculate_ema(closes, 20)
    if not all([rsi, ema20]):
        return None, "indicator calculation failed"
    if direction == "BUY" and rsi > 75:
        return None, f"5m RSI {rsi:.1f} overbought — long blocked"
    if direction == "SELL" and rsi < 25:
        return None, f"5m RSI {rsi:.1f} oversold — short blocked"
    if avg_vol <= 0 or vols[-1] < avg_vol * 0.8:
        return None, "5m volume dormant (<0.8x SMA20)"
    if ema20 and abs(price - ema20) / ema20 < 0.0015:
        return "5m 20-EMA Pullback", None
    sup, res = min(lows[-20:]), max(highs[-20:])
    ms = detect_market_structure(klines_5m)
    sweep_dir, _ = detect_liquidity_sweep(klines_5m, highs, lows, closes, opens, sup, res, ms)
    if sweep_dir == direction:
        return "5m Liquidity Sweep", None
    ifvg_dir, _, _ = detect_inverse_fvg_metals(klines_5m, price)
    if ifvg_dir == direction:
        return "5m IFVG Retest", None
    ob_dir, _, _ = find_metals_order_block(klines_5m)
    if ob_dir == direction:
        return "5m Order Block Retest", None
    return None, "no qualifying 5m pullback/sweep/IFVG/OB setup"


def confirm_metals_1m_micro_entry(symbol, direction):
    """Step 4 (Micro Execution, 1m): engulfing/pin-bar confirmation on close, 1m volume >=1.2x SMA20."""
    klines_1m = get_klines(symbol, "1m", 25)
    if not klines_1m or len(klines_1m) < 22:
        return False
    o, h, l, c, v = (float(klines_1m[-2][1]), float(klines_1m[-2][2]),
                      float(klines_1m[-2][3]), float(klines_1m[-2][4]), float(klines_1m[-2][5]))
    vols = [float(k[5]) for k in klines_1m[-22:-2]]
    avg_vol = sum(vols) / len(vols) if vols else 0
    if avg_vol <= 0 or v < avg_vol * 1.2:
        return False
    prev_l, prev_h = float(klines_1m[-3][3]), float(klines_1m[-3][2])
    level = prev_l if direction == "BUY" else prev_h
    return check_retest_rejection_candle(o, h, l, c, direction, level)


def check_metals_session_active():
    """Session filter removed per explicit instruction (5-7 signals/day target) — kept as a no-op for a one-line restore later."""
    return True


def build_metals_trade_plan(symbol, direction, entry, leverage):
    """
    Risk management: SL beyond the further of structural swing / 1.5x
    ATR. If that would violate 1:1.5 R:R even at the 20% TP cap, the
    SL is TIGHTENED to exactly clear it, rather than rejecting the
    signal. TP1 fixed +10% ROI (breakeven trigger). TP2 target 18-20%.
    """
    klines_5m = get_klines(symbol, "5m", 30)
    klines_1m = get_klines(symbol, "1m", 30)
    if not klines_5m:
        return None
    atr_5m = calculate_atr(klines_5m, 14)
    if direction == "BUY":
        swings = [min(float(k[3]) for k in klines_5m[-15:])]
        if klines_1m: swings.append(min(float(k[3]) for k in klines_1m[-15:]))
        structural_sl = min(swings)
        atr_sl = entry - (atr_5m * 1.5) if atr_5m else structural_sl
        sl = min(structural_sl, atr_sl)
    else:
        swings = [max(float(k[2]) for k in klines_5m[-15:])]
        if klines_1m: swings.append(max(float(k[2]) for k in klines_1m[-15:]))
        structural_sl = max(swings)
        atr_sl = entry + (atr_5m * 1.5) if atr_5m else structural_sl
        sl = max(structural_sl, atr_sl)
    sl_pct = abs(entry - sl) / entry * 100
    if sl_pct <= 0:
        return None
    sl_roi_pct = sl_pct * leverage
    max_sl_roi_for_min_rr = 20.0 / 1.5
    if sl_roi_pct > max_sl_roi_for_min_rr:
        sl_roi_pct = max_sl_roi_for_min_rr
        sl_pct = sl_roi_pct / leverage
        sl = entry * (1 - sl_pct/100) if direction == "BUY" else entry * (1 + sl_pct/100)
    tp1_roi_pct = 10.0
    tp2_roi_pct = min(20.0, max(18.0, sl_roi_pct * 1.5))
    rr_ratio = tp2_roi_pct / sl_roi_pct if sl_roi_pct > 0 else 0
    if rr_ratio < 1.5:
        return None
    tp1_pct, tp2_pct = tp1_roi_pct / leverage, tp2_roi_pct / leverage
    if direction == "BUY":
        tp1, tp2 = entry * (1 + tp1_pct/100), entry * (1 + tp2_pct/100)
    else:
        tp1, tp2 = entry * (1 - tp1_pct/100), entry * (1 - tp2_pct/100)
    return {"sl": sl, "tp1": tp1, "tp2": tp2, "sl_roi_pct": sl_roi_pct,
            "tp1_roi_pct": tp1_roi_pct, "tp2_roi_pct": tp2_roi_pct, "rr_ratio": rr_ratio}


def send_metals_signal(symbol, direction, macro_reason, intermediate_reason, setup_name, entry, plan, confluence_score, leverage):
    """Sends the Telegram alert, registers the trade in pending_signals (Activate/Ignore/expiry), returns the JSON payload."""
    coin = symbol.replace("USDT", "")
    dir_em = "🟢 LONG  ▲" if direction == "BUY" else "🔴 SHORT ▼"
    pos_size = get_fixed_fractional_size(1.0, entry, plan["sl"], leverage)
    msg = (
        f"<b>🥇 METALS MTF SIGNAL — {coin}</b>\n"
        f"┌─────────────────────────────────┐\n"
        f"│  ⚙️  TRADING SIGNAL MASTER v32G  │\n"
        f"└─────────────────────────────────┘\n\n"
        f"  🏗️ Engine: 🥇 METALS ENGINE\n"
        f"  🪙 <b>{coin}</b>  {dir_em}  🔧 <b>{leverage}x Leverage</b>\n\n"
        f"  ┌── WATERFALL ALIGNMENT ──────┐\n"
        f"  │  📅 D1/4H: {macro_reason}\n"
        f"  │  ⏱️ 1H/15m: {intermediate_reason}\n"
        f"  │  📌 5m Setup: {setup_name}\n"
        f"  │  🎯 1m Execution: confirmed close\n"
        f"  └─────────────────────────────┘\n\n"
        f"  ┌── TRADE LEVELS ─────────────┐\n"
        f"  │  💰 Entry  <code>{entry:.4f}</code>\n"
        f"  │  🎯 TP1    <code>{plan['tp1']:.4f}</code>  +{plan['tp1_roi_pct']:.1f}% ROI (SL→breakeven)\n"
        f"  │  🎯 TP2    <code>{plan['tp2']:.4f}</code>  +{plan['tp2_roi_pct']:.1f}% ROI (capped ≤20%)\n"
        f"  │  🛑 SL     <code>{plan['sl']:.4f}</code>  -{plan['sl_roi_pct']:.1f}% ROI\n"
        f"  └─────────────────────────────┘\n\n"
        f"  ⚖️ R:R  1 : {plan['rr_ratio']:.2f}\n"
        f"  💼 Position   : <b>{pos_size:.1f}% of margin</b>\n"
        f"  🎯 Confluence Score: {confluence_score}/100\n\n"
        f"  🕐 {get_ist_time()}"
    )
    reply_markup = {"inline_keyboard": [[
        {"text": "✅ Activate Trade", "callback_data": f"ACTIVATE_{coin}"},
        {"text": "❌ Ignore", "callback_data": f"IGNORE_{coin}"}
    ]]}
    send_telegram(msg, reply_markup=reply_markup)
    now = get_ist_datetime()
    setup = {
        "coin": coin, "symbol": symbol, "direction": direction,
        "pattern": f"Metals MTF ({setup_name})", "setup_score": float(confluence_score),
        "leverage": leverage, "scan_price": entry, "entry": entry,
        "sl": plan["sl"], "tp": plan["tp2"], "original_tp": plan["tp2"],
        "metals_tp1": plan["tp1"], "metals_tp1_roi_pct": plan["tp1_roi_pct"],
        "timestamp": now, "expires_at": now + timedelta(hours=2),
        "pos_size": pos_size, "profit_target": plan["tp2_roi_pct"],
        "eta_minutes": 120, "reversal_alerted": False, "breakeven_sent": False,
        "partial_tp_taken": False, "milestones_sent": [], "market_condition": "unknown",
    }
    pending_signals[coin] = setup
    return {"symbol": symbol, "direction": direction, "entry_price": entry, "stop_loss": plan["sl"],
            "take_profit_1": plan["tp1"], "take_profit_2_max_20pct": plan["tp2"], "confluence_score": confluence_score}


def scan_metals_engine():
    """Entry point — runs on METALS_SCAN_INTERVAL_SECONDS, checks the full waterfall per symbol."""
    if not check_metals_session_active():
        return
    now = get_ist_datetime()
    for symbol in METALS_SYMBOLS:
        coin = symbol.replace("USDT", "")
        metals_last_status[symbol] = {"stage": "starting", "reason": "", "checked_at": now}
        cooldown_until = metals_cooldowns.get(symbol)
        if cooldown_until and now < cooldown_until:
            metals_last_status[symbol] = {"stage": "cooldown", "reason": f"on cooldown until {cooldown_until.strftime('%H:%M IST')}", "checked_at": now}
            continue
        with trade_lock:
            if coin in active_trades or coin in pending_signals:
                metals_last_status[symbol] = {"stage": "already active", "reason": "trade already open or pending", "checked_at": now}
                continue
        macro_ctx = get_metals_macro_context(symbol)
        session_dir, session_reason = get_metals_session_bias(symbol)
        if not session_dir:
            metals_last_status[symbol] = {"stage": "1H/15m session bias", "reason": session_reason, "checked_at": now}
            continue
        entry = get_price(symbol)
        if not entry:
            metals_last_status[symbol] = {"stage": "price fetch", "reason": "could not fetch live price", "checked_at": now}
            continue
        if not check_near_major_level(entry, macro_ctx, session_dir):
            metals_last_status[symbol] = {"stage": "1D/4H boundary", "reason": "price too close to a major daily level against the trade direction", "checked_at": now}
            continue
        setup_name, setup_level = find_metals_5m_pullback_setup(symbol, session_dir)
        if not setup_name:
            metals_last_status[symbol] = {"stage": "5m setup", "reason": setup_level, "checked_at": now}
            continue
        if not confirm_metals_1m_micro_entry(symbol, session_dir):
            metals_last_status[symbol] = {"stage": "1m execution", "reason": f"5m setup found ({setup_name}) but 1m candle didn't confirm yet", "checked_at": now}
            continue
        plan = build_metals_trade_plan(symbol, session_dir, entry, METALS_MAX_LEVERAGE)
        if not plan:
            metals_last_status[symbol] = {"stage": "risk plan", "reason": "structural SL too wide to clear minimum 1:1.5 R:R even after tightening", "checked_at": now}
            continue
        confluence_score = 85
        metals_cooldowns[symbol] = now + timedelta(minutes=METALS_COOLDOWN_MINUTES)
        metals_last_status[symbol] = {"stage": "signal sent", "reason": f"{session_dir} — {setup_name}", "checked_at": now}
        payload = send_metals_signal(symbol, session_dir, "1D/4H boundary OK" if macro_ctx else "1D/4H context unavailable (soft)", session_reason, setup_name, entry, plan, confluence_score, METALS_MAX_LEVERAGE)
        logger.info(f"METALS ENGINE signal: {json.dumps(payload)}")


def get_metals_status_text():
    """
    Manual visibility check for Metals — confirmed missing before this
    round: every waterfall stage already computed a real rejection
    reason internally, it was just discarded with a bare `continue`,
    so the user had zero way to see why nothing was firing. This shows
    exactly which stage each of the 3 symbols is stuck at, right now.
    """
    text = f"{_H('METALS ENGINE STATUS','🥇')}\n\n"
    if not check_metals_session_active():
        text += "  ⚠️ Session filter currently returns always-active (removed per your 5-7/day spec) — this line should never show inactive.\n\n"
    for symbol in METALS_SYMBOLS:
        coin = symbol.replace("USDT", "")
        st = metals_last_status.get(symbol)
        if not st:
            text += f"  ⚪ <b>{coin}</b> — not checked yet since last restart\n\n"
            continue
        age_mins = (get_ist_datetime() - st["checked_at"]).total_seconds() / 60
        text += (f"  🪙 <b>{coin}</b>  (checked {age_mins:.0f} min ago)\n"
                  f"  ↳ Stopped at: <b>{st['stage']}</b>\n"
                  f"  ↳ Reason: {st['reason']}\n\n")
    text += f"  🕐 {get_ist_time()}"
    return text


def scan_river(now,market_condition):
    """NOTE: function/variable names (scan_river, last_river_time, RIVER_INTERVAL)"""
    global last_river_time
    try:
        if "LAB" not in active_trades and "LAB" not in pending_signals:
            price=get_price("LABUSDT"); klines=get_klines("LABUSDT","15m",100)
            if not price or not klines or len(klines)<50: return
            found=detect_patterns("LABUSDT",klines,price,1)+detect_patterns("LABUSDT",klines,price,-1)
            seen=set(); unique=[]
            for pat in found:
                if (pat[0],pat[2]) not in seen: seen.add((pat[0],pat[2])); unique.append(pat)
            if unique:
                best=max(unique,key=lambda x:x[1])
                if best[1]<MIN_PRIMARY_SCORE: return
                confirmed=list(dict.fromkeys([x[0] for x in unique]))
                primary=best[0]; extras=[p for p in confirmed if p!=primary]
                pt=primary+(" + "+" + ".join(extras[:2]) if extras else "")
                score=min(best[1]+min(len(unique)*0.5,2),99)
                if score>=82:
                    atr=calculate_atr(klines); atr_pct=(atr/price)*100 if price>0 else 0
                    setup={"coin":"LAB","symbol":"LABUSDT","direction":best[2],"pattern":pt,
                           "setup_score":score,"leverage":get_smart_leverage("LABUSDT",atr_pct,score),
                           "scan_price":price}
                    format_and_send(setup,"LAB",is_river=True,is_instant=score>=INSTANT_SIGNAL_THRESHOLD,market_condition=market_condition)
        last_river_time=now
    except Exception as e: logger.error(f"River: {e}",exc_info=True)


def is_move_already_extended(closes, direction):
    """Point 5: Detects if a move has already run too far to chase."""
    if len(closes) < 12: return False
    recent = closes[-12:]
    move_pct = (recent[-1] - recent[0]) / recent[0] * 100 if recent[0] > 0 else 0
    if direction == "BUY" and move_pct > 3.5: return True
    if direction == "SELL" and move_pct < -3.5: return True
    return False


def log_retest_candidate(coin, symbol, direction, closes, highs, lows, pattern, pattern_type="extended_move", precise_level=None):
    """Point 5 (extended_move) / BOS+Retest Point 1 (bos_retest): Silent"""
    global retest_watchlist, radar_coins_added

    existing = retest_watchlist.get(coin)
    if existing and existing.get("status") == "PENDING" and precise_level is None:
        return

    if precise_level is not None and precise_level > 0:
        level = precise_level
    else:
        level = min(lows[-12:]) if direction == "BUY" else max(highs[-12:])
    retest_watchlist[coin] = {
        "symbol": symbol,
        "direction": direction,
        "level": level,
        "pattern": pattern,
        "pattern_type": pattern_type,
        "logged_at": get_ist_datetime(),
        "current_price": closes[-1],
        "status": "PENDING"
    }
    radar_coins_added += 1
    save_retest_watchlist()
    reason = "BOS — waiting for pullback to the breakout line" if pattern_type=="bos_retest" else "move already extended"
    logger.info(f"{coin} {reason} — logged retest watch at {format_price(level)} (silent, no push)")


def check_retest_triggers():
    """Point 5 (extended_move) / Point 1 (bos_retest) / Direct-Breakout"""
    global retest_watchlist, radar_coins_triggered, pending_signals, sent_coins, coin_cooldowns
    triggered = []
    now = get_ist_datetime()
    for coin, w in list(retest_watchlist.items()):
        minutes_active = (now - w["logged_at"]).total_seconds() / 60
        if minutes_active > 720:
            if w.get("status") == "PENDING":
                logger.info(f"{coin} retest watch expired after 12h without trigger.")
            del retest_watchlist[coin]; continue
        if w.get("status") == "TRIGGERED":
            continue
        price = get_price(w["symbol"])
        if not price: continue

        klines = get_klines(w["symbol"], "15m", 25)
        vol_ratio = get_volume_ratio(klines) if (klines and len(klines) >= 21) else 1.0

        fast_track = False
        if vol_ratio >= 1.4:
            if w["direction"] == "BUY" and price > (w["level"] * 1.004):
                fast_track = True
            elif w["direction"] == "SELL" and price < (w["level"] * 0.996):
                fast_track = True
        if not fast_track and vol_ratio >= 2.0:
            if w["direction"] == "BUY" and price > (w["level"] * 1.008):
                fast_track = True
            elif w["direction"] == "SELL" and price < (w["level"] * 0.992):
                fast_track = True

        near_level = abs(price - w["level"]) / w["level"] * 100 < 1.5 if w["level"] > 0 else False
        if not fast_track and not near_level:
            continue
        if not fast_track and w.get("pattern_type") == "bos_retest":
            if not klines or len(klines) < 21 or vol_ratio >= 0.85:
                continue
        if not fast_track:
            if not klines or len(klines) < 2:
                continue
            _rc_o, _rc_h, _rc_l, _rc_c = float(klines[-2][1]), float(klines[-2][2]), float(klines[-2][3]), float(klines[-2][4])
            if not check_retest_rejection_candle(_rc_o, _rc_h, _rc_l, _rc_c, w["direction"], w["level"]):
                continue

        w["status"] = "TRIGGERED"
        radar_coins_triggered += 1

        atr_klines = klines or get_klines(w["symbol"], "15m", 30)
        atr_val = calculate_atr(atr_klines) if atr_klines else (price * 0.01)
        sl_price = get_structure_sl(atr_klines, w["direction"], price, atr_val)
        sl_dist = abs(price - sl_price)

        zones = get_htf_zones(w["symbol"])
        min_rr_tp_dist = sl_dist * MIN_RR_RATIO
        structural_tp = get_structural_tp(price, w["direction"], zones, min_rr_tp_dist)
        if structural_tp is not None:
            tp_price = structural_tp
        else:
            tp_dist = max(atr_val * ATR_TP_MULTIPLIER, min_rr_tp_dist)
            tp_price = price + tp_dist if w["direction"] == "BUY" else price - tp_dist

        sl_pct = abs(price - sl_price) / price * 100 if price > 0 else 0
        tp_pct = abs(tp_price - price) / price * 100 if price > 0 else 0
        rr_ratio = tp_pct / sl_pct if sl_pct > 0 else 0.0

        atr_pct = (atr_val / price) * 100 if price > 0 else 0
        lev = get_smart_leverage(w["symbol"], atr_pct, 95.0, "Grade A")
        pos_size = get_fixed_fractional_size(RISK_PCT_BY_GRADE["A"], price, sl_price, lev)
        profit_target = tp_pct * lev

        eta = 60
        expiry_time = now + timedelta(minutes=SIGNAL_EXPIRY_MINUTES)
        pat_name = w["pattern"] + (" (Fast-Track)" if fast_track else " (Retest)")

        setup = {
            "coin": coin, "symbol": w["symbol"], "direction": w["direction"],
            "pattern": pat_name, "setup_score": 95.0, "leverage": lev,
            "scan_price": price, "entry": price, "sl": sl_price, "tp": tp_price,
            "original_tp": tp_price, "timestamp": now, "expires_at": expiry_time,
            "pos_size": pos_size, "profit_target": profit_target, "eta_minutes": eta,
            "reversal_alerted": False, "breakeven_sent": False, "partial_tp_taken": False,
            "milestones_sent": [], "market_condition": "unknown"
        }
        pending_signals[coin] = setup

        dir_em = "🟢 LONG  ▲" if w["direction"] == "BUY" else "🔴 SHORT ▼"
        header_title = ("⚡ EARLY ENTRY — FAST-TRACK CONFIRMED" if fast_track
                         else "🎯 EARLY ENTRY — RETEST CONFIRMED")
        msg = (
            f"<b>{header_title}</b>\n"
            f"┌─────────────────────────────────┐\n"
            f"│  ⚙️  TRADING SIGNAL MASTER v32G  │\n"
            f"└─────────────────────────────────┘\n\n"
            f"  🏗️ Engine: 🧭 SCOUT ENGINE\n"
            f"  🪙 <b>{coin}</b>  {dir_em}  🔧 <b>{lev}x Leverage</b>\n"
            f"  📌 Setup : <b>{pat_name}</b>\n\n"
            f"  ┌── TRADE LEVELS ─────────────┐\n"
            f"  │  💰 Entry      <code>{format_price(price)}</code>\n"
            f"  │  🎯 Target     <code>{format_price(tp_price)}</code>  <i>+{tp_pct:.2f}%</i>\n"
            f"  │  🛑 Stop       <code>{format_price(sl_price)}</code>  <i>-{sl_pct:.2f}%</i>\n"
            f"  └─────────────────────────────┘\n\n"
            f"  📈 Max Profit : <b>+{profit_target:.1f}%</b>\n"
            f"  ⚖️  Risk/Reward: <b>1 : {rr_ratio:.1f}</b>\n"
            f"  💼 Position   : <b>{pos_size:.1f}% of margin</b>\n"
            f"  📊 Volume     : <b>{vol_ratio:.1f}x avg</b>\n"
            f"  ⏰ Exp        : {expiry_time.strftime('%I:%M %p IST')}\n"
            f"  🕐 {get_ist_time()}"
        )
        reply_markup = {"inline_keyboard": [[
            {"text": "✅ Activate Trade", "callback_data": f"ACTIVATE_{coin}"},
            {"text": "❌ Ignore", "callback_data": f"IGNORE_{coin}"}
        ]]}

        if CHARTS_AVAILABLE:
            chart_path = generate_signal_chart(
                w["symbol"], atr_klines, price, sl_price, tp_price, w["direction"], coin,
                pattern_name=pat_name, lev=lev, profit_target=profit_target, vol_ratio=vol_ratio
            )
            if chart_path: send_telegram_photo(chart_path)

        send_telegram(msg, reply_markup=reply_markup)
        sent_coins.append(coin)
        coin_cooldowns[coin] = now + timedelta(minutes=eta)
        save_pending_signals()
        triggered.append((coin, w, price))

    if triggered: save_retest_watchlist()
    return triggered


def get_squeeze_radar_text():
    """Squeeze Radar: scans the coin list for extreme funding rates,"""
    results = []
    for coin in COINS:
        symbol = coin + "USDT"
        rate = get_funding_rate(symbol)
        if rate is None:
            continue
        if rate <= SQUEEZE_FUNDING_EXTREME_NEG:
            results.append((coin, rate, "SHORT squeeze setup (shorts paying heavily)"))
        elif rate >= SQUEEZE_FUNDING_EXTREME_POS:
            results.append((coin, rate, "LONG squeeze setup (longs paying heavily)"))
    if not results:
        return f"{_H('SQUEEZE RADAR','🔥')}\n\nNo coins currently showing extreme funding rates."
    results.sort(key=lambda r: abs(r[1]), reverse=True)
    lines = [_H("SQUEEZE RADAR","🔥"), ""]
    for coin, rate, label in results[:15]:
        lines.append(f"🪙 <b>{coin}</b>  {rate*100:+.3f}%  •  {label}")
    lines.append("")
    lines.append(f"🕐 {get_ist_time()}")
    return "\n".join(lines)

def get_check_coin_text(coin_input):
    """Check Coin: on-demand pull of 15m structure, ADX, volume ratio, and"""
    coin = coin_input.strip().upper().replace("USDT", "")
    symbol = coin + "USDT"
    price = get_price(symbol)
    klines = get_klines(symbol, "15m", 100)
    if not price or not klines or len(klines) < 50:
        return f"{_H('CHECK COIN','🔍')}\n\n❌ Could not fetch enough data for <b>{coin}</b> — check the symbol and try again."
    closes = [float(k[4]) for k in klines]
    adx_val = calculate_adx(klines)
    vol_ratio = get_volume_ratio(klines)
    ms = detect_market_structure(klines)
    zones = get_htf_zones(symbol)
    zone_ok_buy, zone_label_buy = is_in_zone(price, "BUY", zones)
    zone_ok_sell, zone_label_sell = is_in_zone(price, "SELL", zones)
    rsi_val = calculate_rsi(closes)
    lines = [
        _H(f"CHECK COIN — {coin}", "🔍"), "",
        f"💰 Price: <code>{format_price(price)}</code>",
        f"🏗️ Structure: {ms['bias'].upper()}  •  BOS: {'✅' if ms['bos'] else '➖'}  •  ChoCh: {'✅' if ms['choch'] else '➖'}",
        f"💪 ADX: {adx_val:.1f}  •  📊 Vol: {vol_ratio:.2f}x avg  •  📈 RSI: {rsi_val:.1f}",
    ]
    if zone_ok_buy:
        lines.append(f"📍 Sitting in a real Demand zone: {zone_label_buy}")
    elif zone_ok_sell:
        lines.append(f"📍 Sitting in a real Supply zone: {zone_label_sell}")
    else:
        lines.append("📍 Not currently inside a real HTF zone")
    lines.append("")
    lines.append(f"🕐 {get_ist_time()}")
    return "\n".join(lines)

def get_retest_watchlist_text():
    pending = {c: w for c, w in retest_watchlist.items() if w.get("status", "PENDING") == "PENDING"}
    if not pending:
        return f"{_H('RETEST WATCHLIST','👀')}\n\n  🌙 No coins currently being watched for retest.\n\n  🕐 {get_ist_time()}"
    lines = [f"{_H('RETEST WATCHLIST','👀')}\n"]
    for coin, w in pending.items():
        price = get_price(w["symbol"]) or w["current_price"]
        dist = abs(price - w["level"]) / w["level"] * 100 if w["level"] > 0 else 0
        dir_em = "🟢" if w["direction"] == "BUY" else "🔴"
        age_min = int((get_ist_datetime() - w["logged_at"]).total_seconds() / 60)
        lines.append(
            f"  {dir_em} <b>{coin}</b> {w['direction']} — watching <code>{format_price(w['level'])}</code>\n"
            f"     now {format_price(price)} ({dist:.1f}% away) · {w['pattern']} · {age_min}m ago\n"
        )
    lines.append(f"\n  🕐 {get_ist_time()}")
    return "\n".join(lines)


def scan_coins(btc_trend,fng,market_condition,btc_klines=None):
    btc_crashing=is_btc_crashing(); signals_this_cycle=0
    now_check=get_ist_datetime()
    coins_to_fetch=[c for c in COINS if c not in coin_cooldowns or now_check>=coin_cooldowns[c]]
    fetch_results={}
    def _fetch_one(coin):
        symbol=coin+"USDT"
        try:
            return coin,symbol,get_price(symbol),get_klines(symbol,"15m")
        except Exception as e:
            logger.warning(f"prefetch {coin}: {e}")
            return coin,symbol,None,None
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        for coin,symbol,price,klines in executor.map(_fetch_one,coins_to_fetch):
            fetch_results[coin]=(symbol,price,klines)
    for coin in COINS:
        if signals_this_cycle>=MAX_SIGNALS_PER_CYCLE: break
        try:
            if coin in coin_cooldowns:
                if get_ist_datetime()<coin_cooldowns[coin]:
                    logger.info(f"Skip {coin} - cooldown until {coin_cooldowns[coin].strftime('%H:%M')}"); continue
                else: del coin_cooldowns[coin]
            if coin not in fetch_results: continue
            symbol,price,klines=fetch_results[coin]
            if not price or not klines: continue
            vanguard_coins = (
                "BTC","ETH","SOL","HYPE","BERA","IP","INIT","BABY",
                "SAHARA","WAL","LAYER","RED","SPK","NEWT","KERNEL",
                "EPT","COOKIE","BIO","VVV","ARC","BANK"
            )
            if coin in vanguard_coins and len(klines)>=20:
                v_highs=[float(k[2]) for k in klines[-20:]]
                v_lows=[float(k[3]) for k in klines[-20:]]
                v_range_high=max(v_highs); v_range_low=min(v_lows)
                price_range_pct=(v_range_high-v_range_low)/price*100 if price>0 else 99
                v_thresh = 1.0 if coin in ("BTC","ETH","SOL") else 3.8
                if price_range_pct < v_thresh:
                    htf_4h=get_htf_trend(symbol,"4h")
                    if htf_4h==1: v_direction="BUY"
                    elif htf_4h==-1: v_direction="SELL"
                    else:
                        v_mid=(v_range_high+v_range_low)/2
                        v_direction="BUY" if price>=v_mid else "SELL"
                    v_tf_score=get_timeframe_score(symbol,v_direction)
                    if v_tf_score==-1:
                        logger.info(f"Skip {coin} {v_direction} - VANGUARD counter-trend (Daily Macro Veto enforced)")
                        continue
                    logger.info(f"{coin} VANGUARD: extreme compression ({price_range_pct:.2f}% range, threshold {v_thresh}%) — bypassing Python math, sending directly to Claude to forecast {v_direction} breakout")
                    v_atr=calculate_atr(klines); v_atr_pct=(v_atr/price)*100 if price>0 else 0
                    v_score=92.0
                    v_lev=get_smart_leverage(symbol,v_atr_pct,v_score)
                    v_setup={"coin":coin,"symbol":symbol,"direction":v_direction,
                             "pattern":"Vanguard Macro Squeeze","setup_score":v_score,
                             "leverage":v_lev,"scan_price":price,
                             "market_condition":market_condition,"tf_score":v_tf_score}
                    if format_and_send(v_setup,coin,is_instant=False,market_condition=market_condition):
                        signals_this_cycle+=1
                    continue
            fd_funding=get_funding_rate(symbol)
            if fd_funding is not None:
                fd_ms=detect_market_structure(klines)
                fd_highs=[float(k[2]) for k in klines]; fd_lows=[float(k[3]) for k in klines]
                fd_sup=fd_ms["swing_low"] if fd_ms["swing_low"]>0 else min(fd_lows[-30:-1])
                fd_res=fd_ms["swing_high"] if fd_ms["swing_high"]>0 else max(fd_highs[-30:-1])
                fd_direction=detect_funding_divergence(fd_funding,price,fd_sup,fd_res)
                with trade_lock:
                    fd_ok_to_send = (fd_direction and coin not in active_trades and coin not in pending_signals and len(active_trades)<MAX_ACTIVE_TRADES)
                if fd_ok_to_send:
                    logger.info(f"{coin} FUNDING DIVERGENCE: {fd_funding*100:.3f}% funding at a real level — {fd_direction} squeeze setup")
                    fd_atr=calculate_atr(klines); fd_atr_pct=(fd_atr/price)*100 if price>0 else 0
                    fd_score=92.0
                    fd_lev=get_smart_leverage(symbol,fd_atr_pct,fd_score)
                    fd_setup={"coin":coin,"symbol":symbol,"direction":fd_direction,
                             "pattern":"Funding Divergence Sniper","setup_score":fd_score,
                             "leverage":fd_lev,"scan_price":price,
                             "market_condition":market_condition,"tf_score":get_timeframe_score(symbol,fd_direction)}
                    if format_and_send(fd_setup,coin,is_instant=False,market_condition=market_condition):
                        signals_this_cycle+=1
                    continue

            of_direction = detect_order_flow_sniper(symbol, klines, price)
            with trade_lock:
                of_ok_to_send = (of_direction and coin not in active_trades and coin not in pending_signals and len(active_trades)<MAX_ACTIVE_TRADES)
            if of_ok_to_send:
                logger.info(f"{coin} ORDER FLOW SNIPER: sustained taker {of_direction} imbalance with 4H/1H trend — {of_direction} setup")
                of_atr=calculate_atr(klines); of_atr_pct=(of_atr/price)*100 if price>0 else 0
                of_score=90.0
                of_lev=get_smart_leverage(symbol,of_atr_pct,of_score)
                of_setup={"coin":coin,"symbol":symbol,"direction":of_direction,
                         "pattern":"Order Flow Sniper","setup_score":of_score,
                         "leverage":of_lev,"scan_price":price,
                         "market_condition":market_condition,"tf_score":get_timeframe_score(symbol,of_direction)}
                if format_and_send(of_setup,coin,is_instant=False,market_condition=market_condition):
                    signals_this_cycle+=1
                continue

            if coin not in macro_coils:
                macro_pat, macro_dir, macro_quality, macro_level = detect_macro_setups_4h_1h(symbol)
                if macro_pat:
                    _macro_live_price = get_price(symbol)
                    _macro_level_still_valid = (
                        _macro_live_price is not None and macro_level and (
                            (macro_dir == "BUY" and _macro_live_price >= macro_level * 0.98) or
                            (macro_dir == "SELL" and _macro_live_price <= macro_level * 1.02)
                        )
                    )
                    if _macro_level_still_valid:
                        log_macro_coil(coin, symbol, macro_pat, macro_dir, macro_quality, macro_level)

            if coin in macro_coils:
                continue

            found=detect_patterns(symbol,klines,price,btc_trend)
            if not found: continue
            scored=get_all_pattern_scores(found,market_condition)
            signal_sent=False
            for direction in ["BUY","SELL"]:
                if signal_sent: break
                dir_pats=[p for p in scored if p[2]==direction]
                if not dir_pats: continue
                best_pat=dir_pats[0]; primary=best_pat[0]; adj_score=best_pat[1]; base_s=best_pat[3]
                best_geo_notes = best_pat[4] if len(best_pat) > 4 else None
                if base_s<MIN_PRIMARY_SCORE:                                   continue
                if is_pattern_blacklisted(primary):                             continue
                if is_pattern_suspended(primary):                               continue
                if not is_sentiment_valid(direction,fng,primary):
                    logger.info(f"Skip {coin} {direction} - blocked by Fear & Greed sentiment ({fng})")
                    continue
                if btc_crashing and direction=="BUY":                           continue
                if coin in BTC_CORRELATED and too_many_correlated_active():     continue
                if too_many_sector_active(coin):
                    logger.info(f"Skip {coin} {direction} - sector already has an open trade")
                    continue
                is_early_setup = primary in ("Inside Bar Coil","Pre-Breakout Compression","Volatility Contraction (Coiling)","Early Spark Ignition","Smart Money Absorption","Funding Divergence Sniper","Order Flow Sniper","Yellow Circle Sniper")
                alt_perf, btc_perf = check_relative_strength(symbol, btc_klines)
                if not is_early_setup:
                    if direction == "BUY" and alt_perf < btc_perf:
                        logger.info(f"Skip {coin} LONG - underperforming BTC (Beta Trap Risk)")
                        continue
                    if direction == "SELL" and alt_perf > btc_perf:
                        logger.info(f"Skip {coin} SHORT - outperforming BTC (Short Squeeze Risk)")
                        continue
                    if direction == "SELL" and alt_perf > 0:
                        logger.info(f"Skip {coin} SHORT - coin is still green ({alt_perf*100:+.2f}%), absolute directional lock")
                        continue
                    if direction == "BUY" and alt_perf < 0:
                        logger.info(f"Skip {coin} LONG - coin is still red ({alt_perf*100:+.2f}%), absolute directional lock")
                        continue
                tf_score=get_timeframe_score(symbol,direction)
                is_early_setup = primary in ("Early Spark Ignition","Inside Bar Coil","Smart Money Absorption")
                if tf_score==-1 and not is_early_setup:
                    logger.info(f"Skip {coin} {direction} - counter-trend (Daily Macro Veto enforced)"); continue
                extras=[p[0] for p in dir_pats[1:3]]
                pt=primary+(" + "+" + ".join(extras) if extras else "")
                vols_chk=[float(k[5]) for k in klines]
                btc_aligned_chk,_=is_btc_aligned(direction)
                zones_chk=get_htf_zones(symbol)
                zone_ok,_zone_label=is_in_zone(price,direction,zones_chk)
                ms_chk=detect_market_structure(klines)
                is_tier1_pattern = base_s >= 88.0
                is_comp_pattern = "Pre-Breakout Compression" in primary
                is_sweep_pattern = "Liquidity Sweep" in primary
                atr_chk=calculate_atr(klines)
                sl_chk=get_structure_sl(klines,direction,price,atr_chk)
                confirm_bonus,bonus_notes=compute_confirmation_bonus(
                    symbol,direction,klines,vols_chk,tf_score,btc_aligned_chk,
                    zone_ok=zone_ok,ms_bos=ms_chk["bos"],ms_bias=ms_chk["bias"],
                    ms_choch=ms_chk["choch"],is_tier1=is_tier1_pattern,is_compression=is_comp_pattern,
                    is_sweep=is_sweep_pattern,entry=price,sl=sl_chk
                )
                confluence_bonus=min(len(dir_pats)*0.3,1.0)
                score=min(adj_score+confirm_bonus+confluence_bonus,99)
                if bonus_notes:
                    logger.info(f"{coin} {direction} confirmation: base={adj_score:.1f} +{confirm_bonus} ({', '.join(bonus_notes)}) -> {score:.1f}")
                is_accumulation_pattern = primary in ("Inside Bar Coil","Pre-Breakout Compression","Volatility Contraction (Coiling)","Early Spark Ignition","Vanguard Macro Squeeze","Smart Money Absorption","Funding Divergence Sniper","Order Flow Sniper","Yellow Circle Sniper")
                effective_floor = ACCUMULATION_SCORE_FLOOR if is_accumulation_pattern else MIN_SETUP_SCORE
                if score<effective_floor: continue
                closes_chk=[float(k[4]) for k in klines]
                highs_chk=[float(k[2]) for k in klines]
                lows_chk=[float(k[3]) for k in klines]
                if "Volatility Contraction" not in primary and is_move_already_extended(closes_chk,direction):
                    if coin not in retest_watchlist:
                        log_retest_candidate(coin,symbol,direction,closes_chk,highs_chk,lows_chk,pt)
                    continue
                if primary == "Inside Bar Coil":
                    ib_zone_ok,_ib_zone_label=is_in_zone(price,direction,zones_chk)
                    if not ib_zone_ok:
                        logger.info(f"Skip {coin} {direction} - Inside Bar Coil not in a real HTF zone (local swing level only)")
                        continue
                    ib_vol_ratio=get_volume_ratio(klines)
                    if ib_vol_ratio < 1.1:
                        logger.info(f"Skip {coin} {direction} - Inside Bar Coil break lacks volume uncoiling ({ib_vol_ratio:.2f}x)")
                        continue
                if primary in ("Double Top","Double Bottom","Support Bounce","Resistance Rejection"):
                    if not zone_ok:
                        logger.info(f"Skip {coin} {direction} - {primary} rejected: formed outside real HTF zone (no man's land)")
                        continue
                if primary in ("BOS Breakout","Double Top","Double Bottom"):
                    if coin in retest_watchlist:
                        continue
                    _precise_level = None
                    if primary in ("Double Top","Double Bottom"):
                        _avg_vol_chk = sum(vols_chk[-20:]) / 20 if len(vols_chk) >= 20 else 1.0
                        if primary == "Double Bottom":
                            _fired, _lvl = detect_double_bottom_pro(highs_chk, lows_chk, closes_chk, vols_chk, price, _avg_vol_chk)
                        else:
                            _fired, _lvl = detect_double_top_pro(highs_chk, lows_chk, closes_chk, vols_chk, price, _avg_vol_chk)
                        if _fired and _lvl > 0:
                            _precise_level = _lvl
                    log_retest_candidate(coin,symbol,direction,closes_chk,highs_chk,lows_chk,pt,pattern_type="bos_retest",precise_level=_precise_level)
                    continue
                atr=atr_chk; atr_pct=(atr/price)*100 if price>0 else 0
                lev=get_smart_leverage(symbol,atr_pct,score)
                setup={"coin":coin,"symbol":symbol,"direction":direction,"pattern":pt,
                       "setup_score":score,"leverage":lev,"scan_price":price,
                       "market_condition":market_condition,"tf_score":tf_score,
                       "geometry_notes":best_geo_notes}
                with trade_lock:
                    ok_to_send = (coin not in active_trades and coin not in pending_signals and len(active_trades)<MAX_ACTIVE_TRADES)
                if ok_to_send:
                    is_inst=score>=INSTANT_SIGNAL_THRESHOLD
                    logger.info(f"{'INSTANT' if is_inst else 'SIGNAL'}: {coin}|{direction}|Score:{score:.1f}|{primary}")
                    if format_and_send(setup,coin,is_instant=is_inst,market_condition=market_condition):
                        signal_sent=True; signals_this_cycle+=1
        except Exception as e: logger.error(f"Scan {coin}: {e}",exc_info=True)
        time.sleep(DELAY_BETWEEN_COINS)

def main():
    if TELEGRAM_TOKEN=="YOUR_TOKEN_HERE" or CHAT_ID=="YOUR_CHAT_ID_HERE" or not TELEGRAM_TOKEN or not CHAT_ID:
        logger.error("FATAL: TELEGRAM_TOKEN and/or CHAT_ID are missing or still set to placeholder "
                     "values. Set the real environment variables on your deployment platform before "
                     "starting the bot — it will not run with placeholder credentials, since every "
                     "Telegram send would otherwise fail silently in the background.")
        raise SystemExit(1)
    global last_river_time,last_hourly_time,last_pnl_update_time,last_8h_desk_time,last_weekly_report_day,last_pressure_cooker_time,total_scan_cycles,last_metals_scan_time
    load_alerts(); load_circuit_breaker(); load_pending_signals(); load_retest_watchlist(); load_macro_events(); load_evaluating_signals()
    cloud_load_all()
    threading.Thread(target=poll_telegram,daemon=True).start()
    logger.info(f"{BOT_NAME} {BOT_VERSION} starting...")
    send_telegram(
        f"🚀 <b>TRADING SIGNAL MASTER v32G</b> 🍀\n"
        f"<i>Smart • Fast • Accurate • AI</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>✅ Online</b>  •  Scanning <b>{len(COINS)} coins</b>\n\n"
        f"📌 Type /help for all commands\n"
        f"🕐 {get_ist_time()}"
    )
    while True:
        try:
            btc_price=get_price("BTCUSDT"); btc_klines=get_klines("BTCUSDT","1h",100)
            btc_ema50=calculate_ema([float(x[4]) for x in btc_klines],50) if btc_klines else None
            if not btc_price or btc_ema50 is None:
                logger.warning("BTC data unavailable"); time.sleep(60); continue
            btc_trend=1 if btc_price>btc_ema50 else -1
            fng=get_fear_greed_index()
            market_condition=detect_market_condition(btc_price,btc_klines)
            logger.info(f"BTC:{'BULL' if btc_trend==1 else 'BEAR'}|Market:{market_condition}|F&G:{fng}|Losses:{daily_losses}/{MAX_DAILY_LOSSES}|CB:{'ACTIVE' if check_circuit_breaker() else 'OK'}")
            scan_coins(btc_trend,fng,market_condition,btc_klines)
            total_scan_cycles += 1
            check_active_trades()
            expire_pending_signals()
            check_price_alerts()
            for coin,w,price in check_retest_triggers():
                logger.info(f"RETEST/FAST-TRACK signal dispatched: {coin}|{w['direction']}|{w['pattern']}")
            check_evaluating_signals()
            check_active_macro_coils()
            now=time.time()
            if (now-last_hourly_time)>=3600:          send_hourly_report();   last_hourly_time=now
            if (now-last_pnl_update_time)>=3600:      send_live_pnl_update(); last_pnl_update_time=now
            if (now-last_river_time)>=RIVER_INTERVAL:  scan_river(now,market_condition); last_river_time=now
            if (now-last_8h_desk_time)>=28800:         send_8h_ai_desk_report(); last_8h_desk_time=now
            if (now-last_pressure_cooker_time)>=7200:  send_pressure_cooker_report(); last_pressure_cooker_time=now
            if (now-last_metals_scan_time)>=METALS_SCAN_INTERVAL_SECONDS: scan_metals_engine(); last_metals_scan_time=now
            today=datetime.now(IST).date()
            if today.weekday()==6 and last_weekly_report_day!=today:
                send_weekly_report(); last_weekly_report_day=today
            time.sleep(SCAN_INTERVAL)
        except Exception as e:
            logger.error(f"Main loop: {e}",exc_info=True); time.sleep(60)

if __name__=="__main__":
    main()
