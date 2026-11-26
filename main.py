import html
import json
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple

import pytz
import requests
import yfinance as yf
from googletrans import Translator
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BOT_TOKEN = (os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
CHAT_ID = (os.getenv("CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID") or "").strip()
STATE_FILE = os.getenv("RADAR_STATE_FILE", "radar_state.json")
INTERVAL_SECONDS = int(os.getenv("RADAR_INTERVAL_SECONDS", "180"))  # كل 3 دقائق
MAX_RUN_SECONDS = int(os.getenv("RADAR_MAX_RUN_SECONDS", "3300"))    # 55 دقيقة كحد أقصى لكل تشغيل

# دعم جميع الأطر الزمنية المتاحة للأسهم (مفصولة بفاصلة في متغير CHAN_INTERVALS)
SUPPORTED_INTERVALS = ["1m", "2m", "5m", "15m", "30m", "1h", "4h", "1d", "1wk", "1mo"]
RAW_CHAN_INTERVALS = (os.getenv("CHAN_INTERVALS") or os.getenv("CHAN_INTERVAL") or "5m,15m,1h,1d").strip()

INTERVAL_PERIOD_MAP: Dict[str, str] = {
    "1m": "5d",
    "2m": "5d",
    "5m": "10d",
    "15m": "15d",
    "30m": "1mo",
    "1h": "3mo",
    "4h": "6mo",
    "1d": "1y",
    "1wk": "2y",
    "1mo": "5y",
}

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


@dataclass
class Pivot:
    index: int
    time: str
    price: float
    kind: str  # "top" or "bottom"


@dataclass
class TimeframeChan:
    interval: str
    bias: str
    zhongshu_low: float
    zhongshu_high: float
    support: float
    resistance: float
    entry_low: float
    entry_high: float
    stop_loss: float
    target_1: float
    target_2: float
    rr_ratio: float
    divergence: str


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


def to_float(value) -> Optional[float]:
    try:
        cleaned = str(value).replace(",", "").replace("%", "").replace("x", "").replace("$", "").strip()
        return float(cleaned)
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


def get_active_intervals() -> List[str]:
    selected: List[str] = []
    for part in RAW_CHAN_INTERVALS.split(","):
        iv = part.strip()
        if iv in SUPPORTED_INTERVALS and iv not in selected:
            selected.append(iv)
    return selected or ["5m", "15m", "1h", "1d"]


def load_state() -> dict:
    today_ny = datetime.now(pytz.timezone("America/New_York")).strftime("%Y-%m-%d")
    default_state = {
        "date": today_ny,
        "stocks": {},
        "last_summary_tickers": [],
        "was_open": False,
    }
    if not os.path.exists(STATE_FILE):
        return default_state
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("date") != today_ny:
            default_state["was_open"] = bool(data.get("was_open", False))
            return default_state
        return {
            "date": today_ny,
            "stocks": data.get("stocks", {}),
            "last_summary_tickers": data.get("last_summary_tickers", []),
            "was_open": bool(data.get("was_open", False)),
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
    try:
        translated = translator.translate(text, dest="ar")
        return translated.text
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
    return "ظعيف"


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


def fetch_candles_for_interval(symbol: str, interval: str) -> List[Candle]:
    period = INTERVAL_PERIOD_MAP.get(interval, "15d")
    ticker = yf.Ticker(symbol)

    if interval == "4h":
        df = ticker.history(period=period, interval="1h", prepost=True, auto_adjust=True)
        if df is None or df.empty:
            raise ValueError(f"لا توجد بيانات كافية لبناء 4h لـ {symbol}")
        candles_1h: List[Candle] = []
        for ts, row in df.iterrows():
            o = to_float(row.get("Open"))
            h = to_float(row.get("High"))
            l = to_float(row.get("Low"))
            c = to_float(row.get("Close"))
            v = to_float(row.get("Volume")) or 0.0
            if None in (o, h, l, c) or min(o, h, l, c) <= 0:
                continue
            candles_1h.append(Candle(time=str(ts), open=o, high=h, low=l, close=c, volume=max(0.0, v)))

        merged_4h: List[Candle] = []
        for idx in range(0, len(candles_1h) - 3, 4):
            chunk = candles_1h[idx : idx + 4]
            merged_4h.append(
                Candle(
                    time=chunk[0].time,
                    open=chunk[0].open,
                    high=max(x.high for x in chunk),
                    low=min(x.low for x in chunk),
                    close=chunk[-1].close,
                    volume=sum(x.volume for x in chunk),
                )
            )
        if len(merged_4h) >= 12:
            return merged_4h

    df = ticker.history(period=period, interval=interval, prepost=True, auto_adjust=True)
    if df is None or df.empty:
        raise ValueError(f"لا توجد بيانات شموع لـ {symbol} على إطار {interval}")

    candles: List[Candle] = []
    for ts, row in df.iterrows():
        o = to_float(row.get("Open"))
        h = to_float(row.get("High"))
        l = to_float(row.get("Low"))
        c = to_float(row.get("Close"))
        v = to_float(row.get("Volume")) or 0.0
        if None in (o, h, l, c) or min(o, h, l, c) <= 0:
            continue
        candles.append(Candle(time=str(ts), open=o, high=h, low=l, close=c, volume=max(0.0, v)))

    if len(candles) < 12:
        raise ValueError(f"عدد الشموع قليل ({len(candles)}) لـ {symbol} على {interval}")
    return candles


def detect_pivots(candles: List[Candle]) -> List[Pivot]:
    raw_pivots: List[Pivot] = []
    for i in range(1, len(candles) - 1):
        prev_c, curr_c, next_c = candles[i - 1], candles[i], candles[i + 1]
        if curr_c.high > prev_c.high and curr_c.high >= next_c.high:
            raw_pivots.append(Pivot(i, curr_c.time, curr_c.high, "top"))
        elif curr_c.low < prev_c.low and curr_c.low <= next_c.low:
            raw_pivots.append(Pivot(i, curr_c.time, curr_c.low, "bottom"))

    alternating: List[Pivot] = []
    for p in raw_pivots:
        if not alternating:
            alternating.append(p)
            continue
        last = alternating[-1]
        if p.kind != last.kind:
            if p.index - last.index >= 2:
                alternating.append(p)
        else:
            if p.kind == "top" and p.price >= last.price:
                alternating[-1] = p
            elif p.kind == "bottom" and p.price <= last.price:
                alternating[-1] = p
    return alternating


def extract_zhongshu(pivots: List[Pivot], candles: List[Candle]) -> Tuple[float, float]:
    recent = candles[-16:]
    fallback_low = min(c.low for c in recent)
    fallback_high = max(c.high for c in recent)

    if len(pivots) < 4:
        span = max(fallback_high - fallback_low, 0.01)
        return fallback_low + span * 0.25, fallback_high - span * 0.25

    ordered = sorted(p.price for p in pivots[-4:])
    zs_low = ordered[1]
    zs_high = ordered[2]
    if zs_low >= zs_high:
        zs_low = min(c.low for c in candles[-8:])
        zs_high = max(c.high for c in candles[-8:])
    return zs_low, zs_high


def detect_divergence(pivots: List[Pivot], candles: List[Candle]) -> str:
    tops = [p for p in pivots if p.kind == "top"]
    bottoms = [p for p in pivots if p.kind == "bottom"]
    if len(tops) >= 2 and tops[-1].price > tops[-2].price:
        prev_vol = candles[tops[-2].index].volume
        curr_vol = candles[tops[-1].index].volume
        if prev_vol > 0 and curr_vol < prev_vol * 0.85:
            return "تباعد قمة محتمل (ضعف زخم)"
    if len(bottoms) >= 2 and bottoms[-1].price < bottoms[-2].price:
        prev_vol = candles[bottoms[-2].index].volume
        curr_vol = candles[bottoms[-1].index].volume
        if prev_vol > 0 and curr_vol < prev_vol * 0.85:
            return "تباعد قاع محتمل (ضعف هبوط)"
    return "بدون تباعد لافت"


def analyze_single_timeframe(symbol: str, interval: str, live_price: float) -> Optional[TimeframeChan]:
    try:
        candles = fetch_candles_for_interval(symbol, interval)
    except Exception as e:
        print(f"⚠️ تعذر تحليل {symbol} على إطار {interval}: {e}")
        return None

    pivots = detect_pivots(candles)
    zs_low, zs_high = extract_zhongshu(pivots, candles)
    divergence = detect_divergence(pivots, candles)
    ref_price = live_price if live_price > 0 else candles[-1].close

    recent_bottoms = [p.price for p in pivots if p.kind == "bottom"][-2:]
    recent_tops = [p.price for p in pivots if p.kind == "top"][-2:]
    swing_low = min(recent_bottoms) if recent_bottoms else min(c.low for c in candles[-12:])
    swing_high = max(recent_tops) if recent_tops else max(c.high for c in candles[-12:])

    support = min(zs_low, swing_low)
    resistance = max(zs_high, swing_high)

    if ref_price > zs_high:
        bias = "صاعد (فوق 中枢)"
        entry_low = max(zs_high * 0.997, ref_price * 0.990)
        entry_high = ref_price
        stop_loss = min(zs_low, entry_low * 0.985)
        risk = max(entry_high - stop_loss, 0.01)
        target_1 = entry_high + risk * 1.5
        target_2 = max(resistance, entry_high + risk * 2.5)
    elif ref_price < zs_low:
        bias = "هابط (تحت 中枢)"
        entry_low = ref_price
        entry_high = min(zs_low * 1.003, ref_price * 1.010)
        stop_loss = max(zs_high, entry_high * 1.015)
        risk = max(stop_loss - entry_low, 0.01)
        target_1 = entry_low - risk * 1.5
        target_2 = min(support, entry_low - risk * 2.5)
    else:
        bias = "تذبذب (داخل 中枢)"
        entry_low = zs_low
        entry_high = zs_low + (zs_high - zs_low) * 0.25
        stop_loss = support * 0.992
        risk = max(entry_high - stop_loss, 0.01)
        target_1 = zs_high
        target_2 = max(resistance, zs_high + risk)

    mid_entry = (entry_low + entry_high) / 2
    rr_ratio = round(abs(target_1 - mid_entry) / max(risk, 0.01), 2)

    return TimeframeChan(
        interval=interval,
        bias=bias,
        zhongshu_low=round(zs_low, 2),
        zhongshu_high=round(zs_high, 2),
        support=round(support, 2),
        resistance=round(resistance, 2),
        entry_low=round(entry_low, 2),
        entry_high=round(entry_high, 2),
        stop_loss=round(stop_loss, 2),
        target_1=round(target_1, 2),
        target_2=round(target_2, 2),
        rr_ratio=rr_ratio,
        divergence=divergence,
    )


def analyze_all_timeframes(symbol: str, live_price: float) -> List[TimeframeChan]:
    results: List[TimeframeChan] = []
    for iv in get_active_intervals():
        res = analyze_single_timeframe(symbol, iv, live_price)
        if res:
            results.append(res)
    return results


def send_stock_cards(
    session_name: str,
    stocks: List[dict],
    signal_label: str,
    stock_state: dict,
    positive_only: bool = False,
    skip_tickers: Optional[set] = None,
) -> int:
    sent = 0
    skip_tickers = skip_tickers or set()
    min_volume = 1000 if session_name == "Pre-Market" else 10000

    for stock in stocks:
        ticker = str(stock.get("row_key", "")).strip().upper()
        price = to_float(stock.get("price"))
        if not ticker or not is_us_stock_ticker(ticker) or price is None or price <= 0:
            continue

        if ticker in skip_tickers:
            continue

        volume = to_float(stock.get("volume"))
        if volume is None or volume < min_volume:
            continue

        if is_excluded_sector(stock):
            continue

        if positive_only:
            change_val = to_float(stock.get("change"))
            if change_val is None or change_val <= 0:
                continue

        key = f"{session_name}:{ticker}"
        last = stock_state.get(key)

        if last and price <= last["price"]:
            continue

        alert_no = (last["alert_no"] + 1) if last else 1

        stock_news = fetch_stock_news(ticker)
        sec_summary, cik_val, _ = fetch_sec_filings_only(ticker)
        tf_analyses = analyze_all_timeframes(ticker, price)

        rel_vol = str(stock.get("revol", "")).strip() or str(stock.get("rel_volume", "")).strip()
        float_val = str(stock.get("float", "")).strip()

        tv_url = f"https://www.tradingview.com/chart/?symbol={ticker}"
        sec_page_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik_val or ticker}&type=&dateb=&owner=include&count=40"

        message_lines = [
            f"🚨 <b>تنبيه {alert_no}</b> · {html.escape(session_name)} (أسهم فقط)",
            f"📡 Signal: <b>{html.escape(signal_label)}</b>\n",
            f"🔹 <b>{html.escape(ticker)}</b>",
            f"💵 <code>${price:.2f}</code> · 📈 <code>{html.escape(str(stock.get('change', '-')))}</code>",
            f"📊 Vol: <code>{html.escape(str(stock.get('volume', '-')))}</code>",
            f"🏭 القطاع: <code>{html.escape(str(stock.get('sector', '-')))}</code>",
            f"🏷️ الصناعة: <code>{html.escape(str(stock.get('industry', '-')))}</code>",
            f"📰 أخبار اليوم: {html.escape(stock_news)}",
        ]

        if rel_vol and rel_vol not in ["-", "nan", "None", ""]:
            status_text = get_rel_vol_status(rel_vol)
            status_suffix = f" ({status_text})" if status_text else ""
            message_lines.append(f"🔥 الحجم النسبي: <code>{html.escape(rel_vol)}</code>{status_suffix}")

        if float_val and float_val not in ["-", "nan", "None", ""]:
            message_lines.append(f"🌊 الفلوت: <code>{html.escape(float_val)}</code>")

        if tf_analyses:
            message_lines.append("\n📐 <b>تحليل تشان لون على الأطر الزمنية</b>")
            for tf in tf_analyses:
                message_lines.extend([
                    f"▪️ <b>إطار {html.escape(tf.interval)}</b>: <b>{html.escape(tf.bias)}</b> | {html.escape(tf.divergence)}",
                    f"   中枢: <code>{tf.zhongshu_low:.2f} - {tf.zhongshu_high:.2f}</code> | دعم/مقاومة: <code>{tf.support:.2f}</code> / <code>{tf.resistance:.2f}</code>",
                    f"   دخول: <code>{tf.entry_low:.2f} - {tf.entry_high:.2f}</code> | خروج: <code>{tf.stop_loss:.2f}</code>",
                    f"   أهداف: هدف1 <code>{tf.target_1:.2f}</code> · هدف2 <code>{tf.target_2:.2f}</code> (R:R <code>{tf.rr_ratio}</code>)",
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


def get_market_session_and_name() -> Tuple[Optional[str], Optional[str]]:
    ny_tz = pytz.timezone("America/New_York")
    now_ny = datetime.now(ny_tz)

    if now_ny.weekday() >= 5:
        return None, None

    current_time = now_ny.time()

    pre_start = datetime.strptime("04:00", "%H:%M").time()
    main_start = datetime.strptime("09:30", "%H:%M").time()
    main_end = datetime.strptime("16:00", "%H:%M").time()
    post_end = datetime.strptime("20:00", "%H:%M").time()

    if pre_start <= current_time < main_start:
        return "Pre-Market", "الأسهم الفعالة ماقبل السوق"
    if main_start <= current_time < main_end:
        return "Main Session", "الأسهم الفعالة للجلسة الرئيسة"
    if main_end <= current_time <= post_end:
        return "Post-Market", "الأسهم الفعالة لجلسة post"

    return None, None


def filter_signal_stocks(
    raw_stocks: List[dict],
    session_key: str,
    positive_only: bool = False,
    skip_tickers: Optional[set] = None,
) -> List[dict]:
    skip_tickers = skip_tickers or set()
    min_volume = 1000 if session_key == "Pre-Market" else 10000
    valid = []

    for stock in raw_stocks:
        ticker = str(stock.get("row_key", "")).strip().upper()
        if not ticker or not is_us_stock_ticker(ticker) or ticker in skip_tickers:
            continue

        price = to_float(stock.get("price"))
        if price is None or price <= 0:
            continue

        volume = to_float(stock.get("volume"))
        if volume is None or volume < min_volume:
            continue

        if is_excluded_sector(stock):
            continue

        change_val = to_float(stock.get("change"))
        if positive_only and (change_val is None or change_val <= 0):
            continue

        change_str = str(stock.get("change", "0%"))
        if not change_str.startswith("+") and not change_str.startswith("-") and change_val and change_val > 0:
            change_str = "+" + change_str

        valid.append({
            "ticker": ticker,
            "price": price,
            "change": change_val if change_val is not None else 0.0,
            "change_str": change_str,
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
    last_summary_set = set(state.get("last_summary_tickers", []))

    gainers = filter_signal_stocks(signals_data.get("ta_topgainers", []), session_key, positive_only=True)
    if not gainers:
        return None

    gainers.sort(key=lambda x: x["change"], reverse=True)
    top_40 = gainers[:40]

    sa_tz = pytz.timezone("Asia/Riyadh")
    time_sa = datetime.now(sa_tz).strftime("%I:%M %p")
    current_tickers = [item["ticker"] for item in top_40]

    lines = [
        f"⏱️ <b>{session_title} · آخر 3 دقائق · {time_sa} بتوقيت السعودية</b>\n"
    ]

    for idx, item in enumerate(top_40, 1):
        t = item["ticker"]
        tv_url = f"https://www.tradingview.com/chart/?symbol={t}"
        ch_str = item["change_str"]
        new_mark = " 🆕" if t not in last_summary_set else ""
        lines.append(f"{idx}. <a href='{tv_url}'>{t}</a> {ch_str} · ${item['price']:.2f}{new_mark}")

    state["last_summary_tickers"] = current_tickers
    return "\n".join(lines)


def run_individual_alerts(session_name: str, signals_data: Dict[str, List[dict]], stock_state: dict) -> None:
    batch = signals_data.get("ta_topgainers", [])[:25]
    send_stock_cards(session_name, batch, "Top Gainers", stock_state, positive_only=False)


def seconds_to_next_session_start() -> Optional[float]:
    ny_tz = pytz.timezone("America/New_York")
    now_ny = datetime.now(ny_tz)
    starts = [(4, 0), (9, 30), (16, 0)]
    for day_offset in range(0, 8):
        day = now_ny + timedelta(days=day_offset)
        if day.weekday() >= 5:
            continue
        for h, m in starts:
            cand = ny_tz.localize(datetime(day.year, day.month, day.day, h, m))
            if cand > now_ny:
                return (cand - now_ny).total_seconds()
    return None


def run_session_batch() -> None:
    print("🚀 تشغيل رادار الأسهم الأمريكية (Finviz + تشان لون متعدد الأطر)...")
    start_time = time.time()
    state = load_state()
    last_session_key = None
    next_tick = start_time

    while True:
        if time.time() - start_time >= MAX_RUN_SECONDS:
            print("⏱️ تم الوصول للحد الزمني الآمن للدفعة الحالية.")
            break

        session_key, session_title = get_market_session_and_name()

        if session_key:
            if not state.get("was_open"):
                send_telegram_message(f"🟢 <b>بدء رادار التداول الحركي لـ {session_title}</b>")
                state["was_open"] = True
            elif last_session_key and session_key != last_session_key:
                send_telegram_message(f"🟢 <b>بدء رادار التداول الحركي لـ {session_title}</b>")

            if session_key != last_session_key:
                next_tick = time.time()
            last_session_key = session_key

            try:
                signals_data = fetch_all_signals()
                report_message = build_summary_report(session_key, session_title, signals_data, state)
                if report_message and send_telegram_message(report_message, parse_mode="HTML"):
                    print("✅ تم إرسال التقرير المجمع بنجاح.")

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
                wake_at = min(next_tick, now + to_boundary + 0.2)

            if wake_at - start_time >= MAX_RUN_SECONDS:
                break

            sleep_sec = max(0.0, wake_at - time.time())
            print(f"💤 التحديث القادم بعد {int(sleep_sec)} ثانية...")
            time.sleep(sleep_sec)
        else:
            if state.get("was_open"):
                send_telegram_message("🔴 <b>انتهت فترات التداول بالكامل (إغلاق السوق). يتوقف الرادار الآن مؤقتاً لحين الجلسة القادمة.</b>")
                state["was_open"] = False
                state["last_summary_tickers"] = []
                save_state(state)

            wait = seconds_to_next_session_start()
            elapsed = time.time() - start_time
            if wait is not None and elapsed + wait < MAX_RUN_SECONDS:
                print(f"😴 السوق مغلق. الانتظار {int(wait)} ثانية حتى بداية الجلسة القادمة.")
                time.sleep(wait + 0.2)
                last_session_key = None
                continue

            print("😴 السوق مغلق حالياً. إنهاء التشغيل بانتظار الجلسة القادمة.")
            break

    save_state(state)
    print("✅ انتهى التشغيل بنجاح.")


if __name__ == "__main__":
    run_session_batch()
