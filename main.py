import html
import json
import math
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta
from html.parser import HTMLParser
from typing import Dict, List, Optional, Set, Tuple

import pytz
import requests
import yfinance as yf
from googletrans import Translator
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

NY_TZ = pytz.timezone("America/New_York")
SA_TZ = pytz.timezone("Asia/Riyadh")

BOT_TOKEN = (os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
CHAT_ID = (os.getenv("CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID") or "").strip()
STATE_FILE = os.getenv("RADAR_STATE_FILE", "radar_state.json")
INTERVAL_SECONDS = int(os.getenv("RADAR_INTERVAL_SECONDS", "180"))  # كل 3 دقائق
MAX_RUN_SECONDS = int(os.getenv("RADAR_MAX_RUN_SECONDS", "3300"))    # 55 دقيقة كحد أقصى لكل تشغيل

LOOKBACK_BARS = int(os.getenv("STRATEGY_LOOKBACK_BARS", "400"))      # آخر 400 شمعة حسب الفاصل
MA_TYPE = (os.getenv("STRATEGY_MA_TYPE", "EMA") or "EMA").strip().upper()  # EMA أو SMA
MIN_PRICE_STEP_PCT = float(os.getenv("RADAR_MIN_PRICE_STEP_PCT", "0.5"))  # الحد الأدنى لنسبة ارتفاع السعر لإعادة التنبيه

SUPPORTED_INTERVALS = ["1m", "2m", "5m", "15m", "30m", "1h", "4h", "1d", "1wk", "1mo"]
INTRADAY_INTERVALS = {"1m", "2m", "5m", "15m", "30m", "1h", "4h"}
RAW_INTERVALS = (os.getenv("RADAR_INTERVALS") or os.getenv("RADAR_INTERVAL") or "5m,15m,1h,1d").strip()

INTERVAL_PERIOD_MAP: Dict[str, str] = {
    "1m": "5d",
    "2m": "10d",
    "5m": "15d",
    "15m": "30d",
    "30m": "60d",
    "1h": "6mo",
    "4h": "1y",
    "1d": "2y",
    "1wk": "5y",
    "1mo": "10y",
}

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


@dataclass
class Candle:
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    ny_dt: Optional[datetime] = None


@dataclass
class TimeframeVwapSetup:
    interval: str
    entry_price: float
    vwap: float
    ma_100: float
    ma_200: float
    stop_loss: float
    stop_reasons: List[str]
    targets_4: List[float]      # TP1 .. TP4
    far_targets_2: List[float]  # TP5 .. TP6


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
translator = Translator()
translation_cache: Dict[str, str] = {}


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


def is_us_stock_ticker(ticker: str) -> bool:
    """التحقق من أن الرمز سهم أمريكي فقط (بدون أوبشن، عقود، عملات، أو أسواق أخرى)."""
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


def get_active_intervals() -> List[str]:
    selected: List[str] = []
    for part in RAW_INTERVALS.split(","):
        iv = part.strip()
        if iv in SUPPORTED_INTERVALS and iv not in selected:
            selected.append(iv)
    return selected or ["5m", "15m", "1h", "1d"]


def load_state() -> dict:
    today_ny = datetime.now(NY_TZ).strftime("%Y-%m-%d")
    default_state = {
        "date": today_ny,
        "stocks": {},
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
            "stocks": data.get("stocks", {}) if isinstance(data.get("stocks"), dict) else {},
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


def translate_to_arabic(text: str) -> str:
    if not text or text == "لايوجد":
        return "لايوجد"
    if text in translation_cache:
        return translation_cache[text]
    try:
        translated = translator.translate(text, dest="ar")
        result = translated.text or text
        translation_cache[text] = result
        return result
    except Exception as e:
        print(f"⚠️ خطأ في الترجمة: {e}")
        return text


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


class FinvizNewsParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.headlines: List[str] = []
        self.in_news_table = False
        self.in_a = False
        self.current_headline: List[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table" and a.get("id") == "news-table":
            self.in_news_table = True
        elif self.in_news_table and tag == "a":
            self.in_a = True
            self.current_headline = []

    def handle_endtag(self, tag):
        if tag == "table" and self.in_news_table:
            self.in_news_table = False
        elif self.in_a and tag == "a":
            self.in_a = False
            headline = "".join(self.current_headline).strip()
            if headline and headline not in self.headlines:
                self.headlines.append(headline)

    def handle_data(self, data):
        if self.in_a:
            self.current_headline.append(data)


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


def fetch_stock_news(ticker: str) -> str:
    try:
        url = f"https://finviz.com/quote.ashx?t={ticker}"
        time.sleep(random.uniform(1.0, 2.0))
        res = finviz_session.get(url, timeout=12)
        if res.status_code == 200:
            parser = FinvizNewsParser()
            parser.feed(res.text)
            if parser.headlines:
                latest_news = parser.headlines[0]
                if len(latest_news) > 90:
                    latest_news = latest_news[:87] + "..."
                return translate_to_arabic(latest_news)
    except Exception as e:
        print(f"⚠️ خطأ في جلب أخبار السهم لـ {ticker}: {e}")
    return "لايوجد"


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


def get_rel_vol_status(rvol_value) -> str:
    val = to_float(rvol_value)
    if val is None:
        return ""
    if val > 3.0:
        return "استثنائي"
    if val >= 1.5:
        return "اعلى"
    if val >= 0.8:
        return "عادي"
    return "ضعيف"


SEC_8K_ITEMS = {
    "1.01": "اتفاقية جوهرية", "1.02": "إنهاء اتفاقية جوهرية", "1.03": "إفلاس / حراسة قضائية",
    "2.01": "استحواذ / دمج أو بيع أصول", "2.02": "إعلان نتائج مالية", "2.03": "التزام مالي / دين جديد",
    "2.04": "تسريع التزام مالي", "2.05": "تكاليف إعادة هيكلة", "2.06": "انخفاض جوهري في قيمة أصول",
    "3.01": "إشعار شطب / مخالفة شروط الإدراج", "3.02": "إصدار أسهم غير مسجل", "3.03": "تعديل حقوق المساهمين",
    "4.01": "تغيير المدقق", "4.02": "إعادة صياغة قوائم مالية", "5.01": "تغيّر السيطرة",
    "5.02": "تغييرات الإدارة", "5.03": "حوكمة الشركة", "5.05": "حوكمة الشركة", "5.07": "تصويت المساهمين",
    "5.08": "ترشيحات مجلس الإدارة", "7.01": "إفصاح عادل (Reg FD)", "8.01": "أحداث أخرى", "9.01": "مرفقات مالية",
}

SEC_FORM_NAMES = {
    "10-K": "التقرير السنوي", "10-Q": "التقرير الفصلي", "20-F": "التقرير السنوي (مصدر أجنبي)",
    "6-K": "تقرير مصدر أجنبي", "3": "بيان ملكية مبدئي لمطلع", "4": "تغيّر ملكية لمطلع",
    "5": "بيان ملكية سنوي لمطلع", "144": "إشعار بيع أسهم لمطلع", "S-1": "طرح / تسجيل أوراق مالية",
    "S-3": "طرح / تسجيل أوراق مالية", "F-1": "طرح / تسجيل أوراق مالية", "F-3": "طرح / تسجيل أوراق مالية",
    "S-8": "تسجيل أسهم برامج الموظفين", "DEF 14A": "بيان الجمعية العمومية (حوكمة)", "PRE 14A": "بيان الجمعية العمومية (حوكمة)",
}


def get_sec_description(f_form: str, f_items: str = "") -> str:
    f_upper = str(f_form).upper().strip()
    is_amend = f_upper.endswith("/A")
    base = f_upper[:-2] if is_amend else f_upper
    prefix = "تعديل: " if is_amend else ""

    if base == "8-K":
        names = []
        for code in str(f_items or "").split(","):
            name = SEC_8K_ITEMS.get(code.strip())
            if name and name not in names:
                names.append(name)
        if len(names) > 1 and "مرفقات مالية" in names:
            names.remove("مرفقات مالية")
        if names:
            return prefix + " / ".join(names[:2])
        return prefix + "إفصاح جوهري"

    if "13D" in base:
        return prefix + "ملكية جوهرية 5%+ (نية نشطة)"
    if "13G" in base:
        return prefix + "ملكية جوهرية 5%+ (استثمار سلبي)"
    if base.startswith("424B"):
        return prefix + "طرح / تسجيل أوراق مالية"
    if base.startswith("NT "):
        return "إشعار تأخر تقديم التقرير"
    if base in SEC_FORM_NAMES:
        return prefix + SEC_FORM_NAMES[base]
    return prefix + "إفصاح رسمي"


def fetch_sec_filings_only(symbol: str) -> Tuple[str, Optional[str], str]:
    symbol = symbol.upper().strip()
    sec_headers = {"User-Agent": "StockRadarBot/1.0 (contact@stockradar.com)"}
    filings = []
    cik_str_val = None
    try:
        tickers_res = requests.get("https://www.sec.gov/files/company_tickers.json", headers=sec_headers, timeout=8)
        if tickers_res.status_code == 200:
            tickers_data = tickers_res.json()
            cik = None
            cik_raw = None
            for _, val in tickers_data.items():
                if val.get("ticker", "").upper() == symbol:
                    cik_raw = str(val.get("cik_str"))
                    cik = cik_raw.zfill(10)
                    cik_str_val = cik_raw
                    break
            if cik:
                sub_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
                sub_res = requests.get(sub_url, headers=sec_headers, timeout=8)
                if sub_res.status_code == 200:
                    recent = sub_res.json().get("filings", {}).get("recent", {})
                    forms = recent.get("form", [])
                    accession_numbers = recent.get("accessionNumber", [])
                    primary_docs = recent.get("primaryDocument", [])
                    filing_dates = recent.get("filingDate", [])
                    filing_items = recent.get("items", [])
                    for i in range(min(5, len(forms))):
                        f_form = forms[i]
                        f_items = filing_items[i] if i < len(filing_items) else ""
                        f_date = filing_dates[i]
                        acc_num = accession_numbers[i]
                        acc_clean = acc_num.replace("-", "")
                        p_doc = primary_docs[i]
                        if p_doc:
                            filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik_raw}/{acc_clean}/{p_doc}"
                        else:
                            filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik_raw}/{acc_clean}/{acc_num}-index.htm"
                        filings.append({
                            "form": f_form,
                            "date": f_date,
                            "url": filing_url,
                            "accession": acc_num,
                            "brief": get_sec_description(f_form, f_items),
                        })
    except Exception as e:
        print(f"⚠️ خطأ في جلب إفصاحات SEC لـ {symbol}: {e}")

    sec_lines = []
    seen_accessions = set()
    latest_accession = ""
    if filings:
        latest_accession = filings[0]["accession"]
        for f in filings:
            if f["accession"] in seen_accessions:
                continue
            seen_accessions.add(f["accession"])
            sec_lines.append(
                f"• <a href='{f['url']}'>{html.escape(str(f['form']))}</a> · <code>{f['date']}</code> — {html.escape(f['brief'])}"
            )
            if len(sec_lines) >= 4:
                break
    sec_summary = "\n".join(sec_lines) if sec_lines else "• لا توجد إفصاحات حديثة"
    return sec_summary, cik_str_val, latest_accession


def parse_candle_ny_dt(ts) -> Optional[datetime]:
    try:
        if hasattr(ts, "to_pydatetime"):
            dt_val = ts.to_pydatetime()
        elif isinstance(ts, datetime):
            dt_val = ts
        else:
            dt_val = datetime.fromisoformat(str(ts))
        if dt_val.tzinfo is None:
            return NY_TZ.localize(dt_val)
        return dt_val.astimezone(NY_TZ)
    except Exception:
        return None


def _df_to_candles(df) -> List[Candle]:
    candles: List[Candle] = []
    for ts, row in df.iterrows():
        o = to_float(row.get("Open"))
        h = to_float(row.get("High"))
        l = to_float(row.get("Low"))
        c = to_float(row.get("Close"))
        v = to_float(row.get("Volume")) or 0.0
        if None in (o, h, l, c) or min(o, h, l, c) <= 0:
            continue
        candles.append(
            Candle(time=str(ts), open=o, high=h, low=l, close=c, volume=max(0.0, v), ny_dt=parse_candle_ny_dt(ts))
        )
    return candles


def merge_4h_candles(candles_1h: List[Candle]) -> List[Candle]:
    buckets: Dict[Tuple[str, int], List[Candle]] = {}
    ordered_keys: List[Tuple[str, int]] = []
    for c in candles_1h:
        if c.ny_dt is None:
            continue
        block_hour = (c.ny_dt.hour // 4) * 4
        key = (c.ny_dt.strftime("%Y-%m-%d"), block_hour)
        if key not in buckets:
            buckets[key] = []
            ordered_keys.append(key)
        buckets[key].append(c)

    merged: List[Candle] = []
    for key in ordered_keys:
        chunk = buckets[key]
        if not chunk:
            continue
        merged.append(
            Candle(
                time=chunk[0].time,
                open=chunk[0].open,
                high=max(x.high for x in chunk),
                low=min(x.low for x in chunk),
                close=chunk[-1].close,
                volume=sum(x.volume for x in chunk),
                ny_dt=chunk[-1].ny_dt,
            )
        )
    return merged


def fetch_candles_for_interval(symbol: str, interval: str) -> List[Candle]:
    period = INTERVAL_PERIOD_MAP.get(interval, "30d")
    ticker = yf.Ticker(symbol)

    if interval == "4h":
        df_1h = ticker.history(period=period, interval="1h", prepost=True, auto_adjust=True)
        if df_1h is not None and not df_1h.empty:
            merged_4h = merge_4h_candles(_df_to_candles(df_1h))
            if len(merged_4h) >= 205:
                return merged_4h

    df = ticker.history(period=period, interval=interval, prepost=True, auto_adjust=True)
    if df is None or df.empty:
        raise ValueError(f"لا توجد بيانات شموع لـ {symbol} على إطار {interval}")

    candles = _df_to_candles(df)
    if len(candles) < 205:
        raise ValueError(f"عدد الشموع قليل ({len(candles)}) لحساب المتوسطات والهيكل لـ {symbol} على {interval}")
    return candles


def get_session_window_for_dt(dt_ny: datetime) -> Optional[Tuple[str, datetime, datetime]]:
    if dt_ny.weekday() >= 5:
        return None
    day_date = dt_ny.date()
    t_now = dt_ny.time()
    for s_key, _, s_start, s_end in SESSION_WINDOWS:
        if s_start <= t_now < s_end:
            return (
                s_key,
                NY_TZ.localize(datetime.combine(day_date, s_start)),
                NY_TZ.localize(datetime.combine(day_date, s_end)),
            )
    return None


def calculate_vwap_series(candles: List[Candle], interval: str = "5m") -> List[float]:
    vwap_vals: List[float] = []
    cum_pv = 0.0
    cum_vol = 0.0
    active_session_tag: Optional[Tuple[str, str]] = None

    for c in candles:
        if interval in INTRADAY_INTERVALS and c.ny_dt is not None:
            win = get_session_window_for_dt(c.ny_dt)
            session_tag = (c.ny_dt.strftime("%Y-%m-%d"), win[0]) if win else None
            if session_tag is not None and session_tag != active_session_tag:
                cum_pv = 0.0
                cum_vol = 0.0
                active_session_tag = session_tag

        tp = (c.high + c.low + c.close) / 3.0
        cum_pv += tp * c.volume
        cum_vol += c.volume
        vwap_vals.append(cum_pv / cum_vol if cum_vol > 0 else c.close)
    return vwap_vals


def calculate_ma_series(closes: List[float], period: int, ma_type: str = "EMA") -> List[Optional[float]]:
    n = len(closes)
    result: List[Optional[float]] = [None] * n
    if n < period:
        return result

    if ma_type == "SMA":
        running_sum = sum(closes[:period])
        result[period - 1] = running_sum / period
        for i in range(period, n):
            running_sum += closes[i] - closes[i - period]
            result[i] = running_sum / period
        return result

    sma_init = sum(closes[:period]) / period
    result[period - 1] = sma_init
    multiplier = 2.0 / (period + 1.0)
    prev_ema = sma_init
    for i in range(period, n):
        ema_val = (closes[i] - prev_ema) * multiplier + prev_ema
        result[i] = ema_val
        prev_ema = ema_val
    return result


def find_swing_highs(candles: List[Candle], left: int = 3, right: int = 3) -> List[float]:
    highs = [c.high for c in candles]
    swings: List[float] = []
    n = len(highs)
    for i in range(left, n - right):
        window = highs[i - left : i + right + 1]
        if highs[i] == max(window):
            swings.append(float(highs[i]))
    return swings


def find_last_swing_low(candles: List[Candle], left: int = 3, right: int = 3) -> Optional[float]:
    lows = [c.low for c in candles]
    n = len(lows)
    for i in range(n - right - 1, left - 1, -1):
        window = lows[i - left : i + right + 1]
        if lows[i] == min(window):
            return float(lows[i])
    return None


def analyze_single_timeframe(symbol: str, interval: str, live_price: float) -> Optional[TimeframeVwapSetup]:
    """
    استراتيجية VWAP + المتوسطات + الهيكل (بدون ChanLun):
    - الدخول من VWAP (ارتداد أو ثبات فوق VWAP)
    - الوقف: كسر VWAP / كسر متوسط 200 / كسر الهيكل (آخر قاع Swing Low)
    - 4 أهداف + هدفان بعيدان من (متوسط 100، متوسط 200، القمم السابقة ضمن آخر 400 شمعة)
    """
    try:
        all_candles = fetch_candles_for_interval(symbol, interval)
    except Exception as e:
        print(f"⚠️ تعذر تحليل {symbol} على إطار {interval}: {e}")
        return None

    window_candles = all_candles[-LOOKBACK_BARS:] if len(all_candles) >= LOOKBACK_BARS else all_candles
    closes = [c.close for c in window_candles]

    ma100_series = calculate_ma_series(closes, 100, MA_TYPE)
    ma200_series = calculate_ma_series(closes, 200, MA_TYPE)
    vwap_series = calculate_vwap_series(window_candles, interval=interval)

    curr_ma100 = ma100_series[-1]
    curr_ma200 = ma200_series[-1]
    curr_vwap = vwap_series[-1]
    prev_vwap = vwap_series[-2] if len(vwap_series) >= 2 else curr_vwap

    if None in (curr_ma100, curr_ma200, curr_vwap) or any(math.isnan(x) for x in [curr_ma100, curr_ma200, curr_vwap]):
        return None

    prev_c = window_candles[-2]
    curr_c = window_candles[-1]
    entry_price = live_price if live_price > 0 else curr_c.close

    vwap_rebound = (prev_c.low <= prev_vwap * 1.003) and (entry_price >= curr_vwap * 0.998)
    vwap_above_hold = (prev_c.close >= prev_vwap * 0.998) and (entry_price >= curr_vwap * 0.998)
    if not (vwap_rebound or vwap_above_hold):
        return None

    last_swing_low = find_last_swing_low(window_candles, left=3, right=3)
    stop_candidates: List[Tuple[float, str]] = [
        (curr_vwap * 0.998, "كسر VWAP"),
    ]
    if curr_ma200 < entry_price:
        stop_candidates.append((curr_ma200 * 0.998, "كسر متوسط 200"))
    if last_swing_low is not None and last_swing_low < entry_price:
        stop_candidates.append((last_swing_low * 0.999, "كسر الهيكل"))

    valid_stops = [s for s in stop_candidates if s[0] < entry_price]
    if not valid_stops:
        return None

    stop_loss = max(s[0] for s in valid_stops)
    stop_reasons = [s[1] for s in valid_stops]

    swing_highs = find_swing_highs(window_candles, left=3, right=3)
    candidate_levels = sorted(
        set(
            [
                lvl
                for lvl in [curr_ma100, curr_ma200] + swing_highs
                if lvl is not None and lvl > entry_price * 1.002
            ]
        )
    )

    max_high = max(c.high for c in window_candles)
    min_low = min(c.low for c in window_candles)
    wave_span = max(max_high - min_low, entry_price * 0.02)

    step = 1
    while len(candidate_levels) < 6:
        projected = max_high + wave_span * (0.25 * step)
        candidate_levels.append(float(projected))
        step += 1

    targets_4 = [round(x, 2) for x in candidate_levels[:4]]

    far_1 = max(candidate_levels[4], max_high) if max_high > targets_4[-1] else candidate_levels[4]
    far_2 = max(candidate_levels[5], far_1 + wave_span * 0.272)
    if far_2 <= far_1:
        far_2 = far_1 + wave_span * 0.272

    far_targets_2 = [round(float(far_1), 2), round(float(far_2), 2)]

    return TimeframeVwapSetup(
        interval=interval,
        entry_price=round(entry_price, 2),
        vwap=round(curr_vwap, 2),
        ma_100=round(curr_ma100, 2),
        ma_200=round(curr_ma200, 2),
        stop_loss=round(stop_loss, 2),
        stop_reasons=stop_reasons,
        targets_4=targets_4,
        far_targets_2=far_targets_2,
    )


def analyze_all_timeframes(symbol: str, live_price: float) -> List[TimeframeVwapSetup]:
    results: List[TimeframeVwapSetup] = []
    for iv in get_active_intervals():
        res = analyze_single_timeframe(symbol, iv, live_price)
        if res:
            results.append(res)
    return results


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
        "change": change_val if change_val is not None else 0.0,
        "change_str": change_str,
        "raw": stock,
    }


def send_stock_cards(
    session_name: str,
    stocks: List[dict],
    signal_label: str,
    stock_state: dict,
    positive_only: bool = False,
    skip_tickers: Optional[Set[str]] = None,
) -> int:
    sent = 0
    skip_tickers = skip_tickers or set()

    for stock in stocks:
        item = is_stock_eligible(stock, session_name, positive_only=positive_only, skip_tickers=skip_tickers)
        if not item:
            continue

        ticker = item["ticker"]
        price = item["price"]
        raw = item["raw"]

        key = f"{session_name}:{ticker}"
        last = stock_state.get(key)

        if last:
            prev_price = float(last.get("price", 0.0))
            required_price = prev_price * (1.0 + (MIN_PRICE_STEP_PCT / 100.0))
            if price < required_price:
                continue

        tf_setups = analyze_all_timeframes(ticker, price)
        if not tf_setups:
            continue

        alert_no = (last.get("alert_no", 0) + 1) if last else 1
        stock_news = fetch_stock_news(ticker)
        sec_summary, cik_val, _ = fetch_sec_filings_only(ticker)

        rel_vol = str(raw.get("revol", "")).strip() or str(raw.get("rel_volume", "")).strip()
        float_val = str(raw.get("float", "")).strip()

        tv_url = f"https://www.tradingview.com/chart/?symbol={ticker}"
        sec_page_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik_val or ticker}&type=&dateb=&owner=include&count=40"

        message_lines = [
            f"🚨 <b>تنبيه {alert_no}</b> · {html.escape(session_name)} (أسهم فقط)",
            f"📡 Signal: <b>{html.escape(signal_label)}</b>\n",
            f"🔹 <b>{html.escape(ticker)}</b>",
            f"💵 <code>${price:.2f}</code> · 📈 <code>{html.escape(item['change_str'])}</code>",
            f"📊 Vol: <code>{html.escape(str(raw.get('volume', int(item['volume']))))}</code>",
            f"🏭 القطاع: <code>{html.escape(str(raw.get('sector', '-')))}</code>",
            f"🏷️ الصناعة: <code>{html.escape(str(raw.get('industry', '-')))}</code>",
            f"📰 أخبار اليوم: {html.escape(stock_news)}",
        ]

        if rel_vol and rel_vol not in ["-", "nan", "None", ""]:
            status_text = get_rel_vol_status(rel_vol)
            status_suffix = f" ({status_text})" if status_text else ""
            message_lines.append(f"🔥 الحجم النسبي: <code>{html.escape(rel_vol)}</code>{status_suffix}")

        if float_val and float_val not in ["-", "nan", "None", ""]:
            message_lines.append(f"🌊 الفلوت: <code>{html.escape(float_val)}</code>")

        message_lines.append(f"\n🎯 <b>استراتيجية VWAP (آخر {LOOKBACK_BARS} شمعة · {html.escape(MA_TYPE)})</b>")
        for tf in tf_setups:
            t4_str = " · ".join([f"هـ{i+1}:<code>{v:.2f}</code>" for i, v in enumerate(tf.targets_4)])
            f2_str = " · ".join([f"بعيد{i+1}:<code>{v:.2f}</code>" for i, v in enumerate(tf.far_targets_2)])
            stops_str = " / ".join(tf.stop_reasons)
            message_lines.extend([
                f"▪️ <b>إطار {html.escape(tf.interval)}</b> | دخول VWAP: <code>{tf.entry_price:.2f}</code> (VWAP: <code>{tf.vwap:.2f}</code>)",
                f"   متوسطات: 100=<code>{tf.ma_100:.2f}</code> · 200=<code>{tf.ma_200:.2f}</code>",
                f"   🛑 وقف: <code>{tf.stop_loss:.2f}</code> ({html.escape(stops_str)})",
                f"   🎯 أهداف (4): {t4_str}",
                f"   🚀 بعيدة (2): {f2_str}",
            ])

        message_lines.extend([
            f"\n📄 <b>آخر إفصاحات SEC</b>",
            f"{sec_summary}\n",
            f"📈 <a href='{tv_url}'>TradingView</a>",
            f"🏛️ <a href='{html.escape(sec_page_url)}'>SEC</a>",
        ])

        message = "\n".join(message_lines)

        if send_telegram_message(message, parse_mode="HTML"):
            stock_state[key] = {"price": price, "alert_no": alert_no}
            sent += 1
            time.sleep(1.5)

    return sent


def get_market_session_and_name(now_ny: Optional[datetime] = None) -> Tuple[Optional[str], Optional[str]]:
    current_ny = now_ny.astimezone(NY_TZ) if now_ny else datetime.now(NY_TZ)

    if current_ny.weekday() >= 5:
        return None, None

    current_time = current_ny.time()
    for s_key, s_title, s_start, s_end in SESSION_WINDOWS:
        if s_start <= current_time < s_end:
            return s_key, s_title

    return None, None


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
            valid.append({
                "ticker": item["ticker"],
                "price": item["price"],
                "change": item["change"],
                "change_str": item["change_str"],
            })
    return valid


def fetch_all_signals() -> Dict[str, List[dict]]:
    signals = ["ta_topgainers"]
    data: Dict[str, List[dict]] = {}
    for sig in signals:
        try:
            data[sig] = fetch_finviz_signal(sig, limit=40)
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
    top_40 = gainers[:40]

    time_sa = datetime.now(SA_TZ).strftime("%I:%M %p")
    current_tickers = [item["ticker"] for item in top_40]

    lines = [
        f"⏱️ <b>{html.escape(session_title)} · آخر 3 دقائق · {time_sa} بتوقيت السعودية</b>\n"
    ]

    for idx, item in enumerate(top_40, 1):
        t = item["ticker"]
        tv_url = f"https://www.tradingview.com/chart/?symbol={t}"
        ch_str = html.escape(item["change_str"])
        new_mark = " 🆕" if t not in last_summary_set else ""
        lines.append(f"{idx}. <a href='{tv_url}'>{t}</a> {ch_str} · ${item['price']:.2f}{new_mark}")

    summary_by_session[session_key] = current_tickers
    return "\n".join(lines)


def run_individual_alerts(session_name: str, signals_data: Dict[str, List[dict]], stock_state: dict) -> None:
    batch = signals_data.get("ta_topgainers", [])[:25]
    sent = send_stock_cards(session_name, batch, "Top Gainers", stock_state, positive_only=False)
    print(f"📩 تم إرسال {sent} تنبيه فردي لجلسة {session_name}.")


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
    print(f"🚀 تشغيل رادار الأسهم الأمريكية (Finviz + VWAP / MA100-200 / {LOOKBACK_BARS} شمعة)...")
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
                state["stocks"] = {}
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
                        print(f"✅ تم إرسال التقرير المجمع لجلسة {session_key}.")

                    run_individual_alerts(session_key, signals_data, state.setdefault("stocks", {}))
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
