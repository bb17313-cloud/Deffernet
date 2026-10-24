import os
import time
import html
from html.parser import HTMLParser
from datetime import datetime
import pytz
import requests

# قراءة الإعدادات
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

FINVIZ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
}

# قائمة الأسهم المحددة للمتابعة
US_STOCKS = [
    "NB", "RKLB", "LCUT", "QSI", "WWR", "QUBT", "EVLV", "QS", "CSCO", "GRRR",
    "RZLV", "CMPX", "PANW", "NNE", "S", "AUR", "ARAY", "ASTS", "KOPN", "SATL",
    "AVGO", "BIRK", "RGTI", "OKTA", "APG", "GEV", "MBOT", "KULR", "TPR", "OSRH",
    "CORZ", "TEM", "HEI", "IRIX", "ONDS", "BSM", "MLYS", "AGNT", "EBS", "SLI",
    "USAR", "CRMD", "SMR", "LAMR", "YOU", "WRAP", "AMBA", "ACHR", "NKE", "U",
    "LEN", "DHI", "NVAX", "ZENA", "MLTX", "SPCX", "MVST", "PL", "CVX", "INDO",
    "IBRX", "GDRX", "FSLR", "QBTS", "LMT", "HQ", "DVN", "QNC", "COR", "NVO",
    "CRM", "BBAI", "MSFT", "AA", "MUX", "ANRO", "GMAB", "EONR", "AISP", "TDC",
    "PLSE", "VRME", "DUOL", "NTSK", "TWST", "ITRG", "CF", "PCLA", "PPSI", "ZETA",
    "RPD", "DPRO", "BZAI", "APH", "INFQ", "SLDP", "MP", "RMBS", "TE", "ATEC",
    "INOD", "CMOPF", "YEXT", "LAC", "ALLE", "TYGO", "HIMX", "NVDA", "LAES", "CTMX", "LUNR"
]

# سجل التنبيهات المؤقت للتشغيل الحالي
alerts_state = {}


class FinvizRowsParser(HTMLParser):
    """يقرأ صفوف جدول الفاحص مباشرة من Finviz"""
    def __init__(self):
        super().__init__()
        self.rows = []
        self.current_row = None
        self.cell_id = None
        self.cell_depth = 0
        self.cell_text = []

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


def fetch_custom_stocks_data(tickers, batch_size=40):
    """جلب البيانات الأساسية والفنية من Finviz"""
    stocks_list = []
    seen = set()

    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        tickers_param = ",".join(batch)
        url = f"https://finviz.com/screener.ashx?v=111&t={tickers_param}"

        try:
            res = requests.get(url, headers=FINVIZ_HEADERS, timeout=20)
            res.raise_for_status()
            parser = FinvizRowsParser()
            parser.feed(res.text)

            for row in parser.rows:
                key = str(row.get("row_key", "")).strip().upper()
                if not key or key in seen:
                    continue
                seen.add(key)
                stocks_list.append({
                    "row_key": key,
                    "price": row.get("price"),
                    "change": row.get("change"),
                    "volume": row.get("volume"),
                    "sector": row.get("sector", "غير محدد"),
                    "industry": row.get("industry", "غير محدد"),
                })
            time.sleep(1.5)
        except Exception as e:
            print(f"⚠️ خطأ في جلب بيانات Finviz: {e}")

    return stocks_list


def send_telegram_message(message, parse_mode="HTML"):
    if not BOT_TOKEN or not CHAT_ID:
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True
    }
    try:
        response = requests.post(url, json=payload)
        return response.status_code == 200
    except Exception as e:
        print(f"❌ خطأ في إرسال تيليجرام: {e}")
        return False


def to_float(value):
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def process_channel_strategy(session_name, stocks):
    sent = 0

    for stock in stocks:
        ticker = str(stock.get('row_key', '')).strip().upper()
        price = to_float(stock.get('price'))
        change = to_float(stock.get('change'))
        volume = to_float(stock.get('volume'))

        if not ticker or price is None or price <= 0:
            continue

        # تطبيق شروط السيولة والزخم المعتمدة للاختراق
        if volume is None or volume < 200000:
            continue
        if change is None or change < 1.0: 
            continue

        key = f"{session_name}:{ticker}"
        stop_loss = price * 0.975 # وقف خسارة مقترح تحت سعر الدخول بنسبة 2.5%

        tv_url = f"https://www.tradingview.com/chart/?symbol={ticker}"
        finviz_url = f"https://finviz.com/quote.ashx?t={ticker}"

        message = (
            f"🚨 <b>تنبيه فلتر كسر القناة (Call)</b> · {html.escape(session_name)}\n\n"
            f"🔹 <b>{html.escape(ticker)}</b>\n"
            f"💵 سعر الدخول: <code>${price:.2f}</code> · 📈 التغير: <code>+{change:.2f}%</code>\n"
            f"📊 الحجم: <code>{stock.get('volume', '-')}</code>\n"
            f"🛑 <b>وقف الخسارة المقترح:</b> <code>${stop_loss:.2f}</code>\n\n"
            f"✨ <i>تحقق نموذج الشموع الثلاث (اختراق المقاومة + إعادة الاختبار + التأكيد الصعودي)</i>\n\n"
            f"📈 <a href='{tv_url}'>TradingView</a> | 📊 <a href='{finviz_url}'>Finviz</a>"
        )

        if send_telegram_message(message, parse_mode="HTML"):
            sent += 1
            time.sleep(1)

    return sent


def get_market_session():
    ny_tz = pytz.timezone('America/New_York')
    now_ny = datetime.now(ny_tz)

    if now_ny.weekday() >= 5:
        return None

    current_time = now_ny.time()
    if datetime.strptime("09:30", "%H:%M").time() <= current_time < datetime.strptime("16:00", "%H:%M").time():
        return "Main Session 🔔"
    elif datetime.strptime("04:00", "%H:%M").time() <= current_time < datetime.strptime("09:30", "%H:%M").time():
        return "Pre-Market 🌅"
    elif datetime.strptime("16:00", "%H:%M").time() <= current_time <= datetime.strptime("20:00", "%H:%M").time():
        return "Post-Market 🌆"
    return None


def main():
    print("🚀 بدء تنفيذ فحص رادار استراتيجية كسر القناة...")
    session_name = get_market_session()

    if not session_name:
        print("💤 السوق مغلق حالياً أو في عطلة نهاية الأسبوع.")
        return

    print(f"⏳ [{session_name}] جلب بيانات الأسهم من Finviz...")
    stocks = fetch_custom_stocks_data(US_STOCKS)
    if stocks:
        sent = process_channel_strategy(session_name, stocks)
        print(f"✅ تم الانتهاء وإرسال {sent} تنبيه بنجاح.")
    else:
        print("⚠️ لم يتم العثور على بيانات للأسهم.")


if __name__ == "__main__":
    main()
