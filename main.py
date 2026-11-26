import html
import json
import os
import random
import time
from datetime import datetime, time as dt_time, timedelta
from html.parser import HTMLParser
from typing import Dict, List, Optional, Set, Tuple

import pytz
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

NY_TZ = pytz.timezone("America/New_York")
SA_TZ = pytz.timezone("Asia/Riyadh")

BOT_TOKEN = (os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
CHAT_ID = (os.getenv("CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID") or "").strip()
STATE_FILE = os.getenv("RADAR_STATE_FILE", "radar_state.json")
INTERVAL_SECONDS = int(os.getenv("RADAR_INTERVAL_SECONDS", "180"))  # كل 3 دقائق
MAX_RUN_SECONDS = int(os.getenv("RADAR_MAX_RUN_SECONDS", "3300"))    # 55 دقيقة كحد أقصى لكل تشغيل
TOP_LIST_LIMIT = int(os.getenv("RADAR_TOP_LIMIT", "40"))

SESSION_WINDOWS: List[Tuple[str, str, dt_time, dt_time]] = [
    ("Pre-Market", "الأسهم الفعالة ماقبل السوق", dt_time(4, 0), dt_time(9, 30)),
    ("Main Session", "الأسهم الفعالة للجلسة الرئيسة", dt_time(9, 30), dt_time(16, 0)),
    ("Post-Market", "الأسهم الفعالة لجلسة post", dt_time(16, 0), dt_time(20, 0)),
]

FINVIZ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://finviz.com/",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

EXCLUDED_SECTOR_KEYWORDS = ["bank", "insurance", "restaurant", "beverage"]


def create_finviz_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=[403, 429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(FINVIZ_HEADERS)
    return session


finviz_session = create_finviz_session()


def to_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        raw = str(value).strip()
        if not raw or raw in {"-", "N/A", "nan", "NaN", "None"}:
            return None
        multiplier = 1.0
        upper = raw.upper()
        if upper.endswith("B"):
            multiplier = 1_000_000_000.0
            upper = upper[:-1]
        elif upper.endswith("M"):
            multiplier = 1_000_000.0
            upper = upper[:-1]
        elif upper.endswith("K"):
            multiplier = 1_000.0
            upper = upper[:-1]
        cleaned = upper.replace(",", "").replace("%", "").replace("x", "").replace("$", "").replace("+", "").strip()
        return float(cleaned) * multiplier
    except (TypeError, ValueError):
        return None


def format_volume(raw_vol, numeric_vol: float) -> str:
    raw_str = str(raw_vol or "").strip()
    if raw_str and raw_str not in {"-", "N/A", "nan", "NaN", "None", ""}:
        return raw_str.replace(" ", "")
    if numeric_vol >= 1_000_000_000:
        return f"{numeric_vol / 1_000_000_000:.2f}B"
    if numeric_vol >= 1_000_000:
        return f"{numeric_vol / 1_000_000:.2f}M"
    if numeric_vol >= 1_000:
        return f"{numeric_vol / 1_000:.0f}K"
    return str(int(numeric_vol))


def is_us_stock_ticker(ticker: str) -> bool:
    """التحقق من أن الرمز سهم أمريكي فقط."""
    t = str(ticker or "").strip().upper()
    if not t or len(t) > 6:
        return False
    if any(ch in t for ch in ["=", "/", "^", "-", ".", ":", " "]):
        return False
    if t.endswith("USDT") or t.endswith("USDC") or t.endswith("BUSD"):
        return False
    return t.isalpha()


def is_excluded_sector(stock: dict) -> bool:
    sector = str(stock.get("sector", "")).lower()
    industry = str(stock.get("industry", "")).lower()
    return any(kw in sector or kw in industry for kw in EXCLUDED_SECTOR_KEYWORDS)


def get_session_min_volume(session_key: str) -> float:
    env_map = {
        "Pre-Market": ("RADAR_MIN_VOL_PRE", 1000.0),
        "Main Session": ("RADAR_MIN_VOL_MAIN", 10000.0),
        "Post-Market": ("RADAR_MIN_VOL_POST", 5000.0),
    }
    env_name, default_val = env_map.get(session_key, ("RADAR_MIN_VOL_MAIN", 10000.0))
    parsed = to_float(os.getenv(env_name))
    return parsed if parsed is not None and parsed >= 0 else default_val


def load_state() -> dict:
    today_ny = datetime.now(NY_TZ).strftime("%Y-%m-%d")
    default_state = {
        "date": today_ny,
        "last_summary_tickers": {},
        "was_open": False,
        "last_open_session": None,
    }
    if not os.path.exists(STATE_FILE):
        return default_state
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("date") != today_ny:
            default_state["was_open"] = bool(data.get("was_open", False))
            return default_state
        raw_summary = data.get("last_summary_tickers", {})
        summary_map = raw_summary if isinstance(raw_summary, dict) else {}
        return {
            "date": today_ny,
            "last_summary_tickers": summary_map,
            "was_open": bool(data.get("was_open", False)),
            "last_open_session": data.get("last_open_session"),
        }
    except Exception as e:
        print(f"⚠️ تعذر قراءة ملف الحالة {STATE_FILE}: {e}")
        return default_state


def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ تعذر حفظ ملف الحالة {STATE_FILE}: {e}")


class FinvizRowsParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: List[dict] = []
        self.current_row: Optional[dict] = None
        self.cell_id: Optional[str] = None
        self.cell_depth = 0
        self.cell_text: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "div":
            return
        a = dict(attrs)
        if self.cell_id is not None:
            self.cell_depth += 1
            return
        if "data-row-key" in a:
            self.current_row = {"row_key": a["data-row-key"]}
            self.rows.append(self.current_row)
        elif self.current_row is not None and "data-column-id" in a:
            self.cell_id = a["data-column-id"]
            self.cell_depth = 1
            self.cell_text = []

    def handle_endtag(self, tag):
        if tag != "div" or self.cell_id is None:
            return
        self.cell_depth -= 1
        if self.cell_depth == 0:
            self.current_row[self.cell_id] = "".join(self.cell_text).strip()
            self.cell_id = None

    def handle_data(self, data):
        if self.cell_id is not None:
            self.cell_text.append(data)


def fetch_finviz_signal(signal: str, limit: int = 40) -> List[dict]:
    rows: List[dict] = []
    seen_keys = set()
    offsets = [1, 21] if limit > 20 else [1]

    for start_row in offsets:
        if len(rows) >= limit:
            break
        url = f"https://finviz.com/screener.ashx?v=111&s={signal}&r={start_row}"
        time.sleep(random.uniform(1.5, 3.0))
        res = finviz_session.get(url, timeout=20)
        res.raise_for_status()
        parser = FinvizRowsParser()
        parser.feed(res.text)

        if not parser.rows:
            break

        for row in parser.rows:
            key = str(row.get("row_key", "")).strip().upper()
            if not key or key in seen_keys or not is_us_stock_ticker(key):
                continue
            seen_keys.add(key)
            row["row_key"] = key
            rows.append(row)
            if len(rows) >= limit:
                break

    return rows[:limit]


def send_telegram_message(message: str, parse_mode: str = "HTML") -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ خطأ: لم يتم العثور على إعدادات BOT_TOKEN أو CHAT_ID.")
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    try:
        response = requests.post(url, json=payload, timeout=15)
        if response.status_code != 200:
            print(f"❌ فشل إرسال رسالة التلغرام: {response.text}")
            return False
        return True
    except Exception as e:
        print(f"❌ خطأ أثناء الاتصال بالتلغرام: {e}")
        return False


def get_market_session_and_name(now_ny: Optional[datetime] = None) -> Tuple[Optional[str], Optional[str]]:
    current_ny = now_ny.astimezone(NY_TZ) if now_ny else datetime.now(NY_TZ)

    if current_ny.weekday() >= 5:
        return None, None

    current_time = current_ny.time()
    for s_key, s_title, s_start, s_end in SESSION_WINDOWS:
        if s_start <= current_time < s_end:
            return s_key, s_title

    return None, None


def is_stock_eligible(
    stock: dict,
    session_key: str,
    positive_only: bool = False,
    skip_tickers: Optional[Set[str]] = None,
) -> Optional[dict]:
    skip_tickers = skip_tickers or set()
    ticker = str(stock.get("row_key") or stock.get("ticker") or "").strip().upper()
    if not ticker or not is_us_stock_ticker(ticker) or ticker in skip_tickers:
        return None

    price = to_float(stock.get("price"))
    if price is None or price <= 0:
        return None

    min_volume = get_session_min_volume(session_key)
    volume = to_float(stock.get("volume"))
    if volume is None or volume < min_volume:
        return None

    if is_excluded_sector(stock):
        return None

    change_val = to_float(stock.get("change"))
    if positive_only and (change_val is None or change_val <= 0):
        return None

    change_str = str(stock.get("change", "0%")).strip()
    if change_val is not None and change_val > 0 and not change_str.startswith("+"):
        change_str = "+" + change_str

    return {
        "ticker": ticker,
        "price": price,
        "volume": volume,
        "volume_str": format_volume(stock.get("volume"), volume),
        "change": change_val if change_val is not None else 0.0,
        "change_str": change_str,
    }


def filter_signal_stocks(
    raw_stocks: List[dict],
    session_key: str,
    positive_only: bool = False,
    skip_tickers: Optional[Set[str]] = None,
) -> List[dict]:
    valid: List[dict] = []
    for stock in raw_stocks:
        item = is_stock_eligible(stock, session_key, positive_only=positive_only, skip_tickers=skip_tickers)
        if item:
            valid.append(item)
    return valid


def fetch_all_signals() -> Dict[str, List[dict]]:
    signals = ["ta_topgainers"]
    data: Dict[str, List[dict]] = {}
    for sig in signals:
        try:
            data[sig] = fetch_finviz_signal(sig, limit=TOP_LIST_LIMIT)
        except Exception as e:
            print(f"❌ خطأ في جلب إشارة {sig}: {e}")
            data[sig] = []
    return data


def build_summary_report(
    session_key: str,
    session_title: str,
    signals_data: Dict[str, List[dict]],
    state: dict,
) -> Optional[str]:
    summary_by_session = state.setdefault("last_summary_tickers", {})
    last_summary_set = set(summary_by_session.get(session_key, []))

    gainers = filter_signal_stocks(signals_data.get("ta_topgainers", []), session_key, positive_only=True)
    if not gainers:
        return None

    gainers.sort(key=lambda x: x["change"], reverse=True)
    top_stocks = gainers[:TOP_LIST_LIMIT]

    time_sa = datetime.now(SA_TZ).strftime("%I:%M %p")
    current_tickers = [item["ticker"] for item in top_stocks]

    lines = [
        f"⏱️ <b>{html.escape(session_title)} · {time_sa}</b>"
    ]

    for idx, item in enumerate(top_stocks, 1):
        t = item["ticker"]
        tv_url = f"https://www.tradingview.com/chart/?symbol={t}"
        ch_str = html.escape(item["change_str"])
        vol_str = html.escape(item["volume_str"])
        new_mark = " 🆕" if t not in last_summary_set else ""
        lines.append(f"{idx}. <a href='{tv_url}'>{t}</a> {ch_str} ${item['price']:.2f} V:{vol_str}{new_mark}")

    summary_by_session[session_key] = current_tickers
    return "\n".join(lines)


def seconds_to_next_session_start() -> Optional[float]:
    now_ny = datetime.now(NY_TZ)
    starts = [(s_start.hour, s_start.minute) for _, _, s_start, _ in SESSION_WINDOWS]
    for day_offset in range(0, 8):
        day = now_ny + timedelta(days=day_offset)
        if day.weekday() >= 5:
            continue
        for h, m in starts:
            cand = NY_TZ.localize(datetime(day.year, day.month, day.day, h, m))
            if cand > now_ny:
                return (cand - now_ny).total_seconds()
    return None


def run_session_batch() -> None:
    print(f"🚀 تشغيل رادار قائمة الـ {TOP_LIST_LIMIT} سهم (Finviz + Volume)...")
    start_time = time.time()
    state = load_state()
    last_session_key: Optional[str] = None
    next_tick = start_time

    try:
        while True:
            elapsed = time.time() - start_time
            if elapsed >= MAX_RUN_SECONDS:
                print("⏱️ تم الوصول للحد الزمني الآمن للدفعة الحالية.")
                break

            now_ny = datetime.now(NY_TZ)
            today_str = now_ny.strftime("%Y-%m-%d")
            if state.get("date") != today_str:
                state["date"] = today_str
                state["last_summary_tickers"] = {}

            session_key, session_title = get_market_session_and_name(now_ny)

            if session_key and session_title:
                session_changed = last_session_key is not None and session_key != last_session_key
                if not state.get("was_open") or session_changed:
                    send_telegram_message(f"🟢 <b>بدء رادار التداول الحركي لـ {html.escape(session_title)}</b>")
                    state["was_open"] = True
                    state["last_open_session"] = session_key

                if session_key != last_session_key:
                    next_tick = time.time()
                last_session_key = session_key

                try:
                    signals_data = fetch_all_signals()
                    report_message = build_summary_report(session_key, session_title, signals_data, state)
                    if report_message and send_telegram_message(report_message, parse_mode="HTML"):
                        print(f"✅ تم إرسال قائمة الـ {TOP_LIST_LIMIT} سهم لجلسة {session_key}.")

                    save_state(state)
                except Exception as e:
                    print(f"❌ خطأ في الدورة الحالية (سيستمر الرادار): {e}")

                now = time.time()
                next_tick += INTERVAL_SECONDS
                while next_tick <= now:
                    next_tick += INTERVAL_SECONDS

                wake_at = next_tick
                to_boundary = seconds_to_next_session_start()
                if to_boundary is not None:
                    wake_at = min(wake_at, now + to_boundary + 0.2)

                remaining_budget = MAX_RUN_SECONDS - (time.time() - start_time)
                if remaining_budget <= 0 or (wake_at - start_time) >= MAX_RUN_SECONDS:
                    print("⏱️ إيقاف الانتظار لتجاوز الحد الزمني للدفعة الحالية.")
                    break

                sleep_sec = max(0.0, min(wake_at - time.time(), remaining_budget))
                print(f"💤 التحديث القادم بعد {int(sleep_sec)} ثانية...")
                time.sleep(sleep_sec)
            else:
                if state.get("was_open"):
                    send_telegram_message("🔴 انتهت فترات التداول بالكامل (إغلاق <b>السوق</b>). يتوقف الرادار الآن مؤقتاً...")
                    state["was_open"] = False
                    save_state(state)
                last_session_key = None

                to_next = seconds_to_next_session_start()
                if to_next is None:
                    print("🛑 لا توجد جلسة تداول قادمة قريبة.")
                    break

                remaining_budget = MAX_RUN_SECONDS - (time.time() - start_time)
                if remaining_budget <= 1.0:
                    print("🌙 السوق مغلق وانتهى وقت الدفعة الحالية.")
                    break

                sleep_sec = max(0.0, min(to_next + 0.5, remaining_budget))
                print(f"🌙 السوق مغلق حالياً؛ الانتظار لمدة {int(sleep_sec)} ثانية...")
                time.sleep(sleep_sec)
    finally:
        save_state(state)


if __name__ == "__main__":
    run_session_batch()
