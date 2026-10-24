import os
import time
import html
import json
from datetime import datetime
import pytz
import requests
import yfinance as yf

# قراءة الإعدادات
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

STATE_FILE = "alerted_stocks.json"

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


def load_state():
    ny_tz = pytz.timezone('America/New_York')
    today = datetime.now(ny_tz).strftime("%Y-%m-%d")
    
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("date") == today:
                    return data.get("alerted", [])
        except Exception:
            pass
    return []


def save_state(alerted_list):
    ny_tz = pytz.timezone('America/New_York')
    today = datetime.now(ny_tz).strftime("%Y-%m-%d")
    data = {
        "date": today,
        "alerted": alerted_list
    }
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"⚠️ خطأ في حفظ الحالة: {e}")


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


def analyze_and_scan_stocks(tickers, session_name):
    sent = 0
    alerted_stocks = load_state()

    print(f"⏳ جلب وتحليل بيانات {len(tickers)} سهم...")

    for ticker in tickers:
        try:
            stock_data = yf.Ticker(ticker)
            df = stock_data.history(period="1mo")

            if df.empty or len(df) < 10:
                continue

            current_price = float(df['Close'].iloc[-1])
            prev_price = float(df['Close'].iloc[-2])
            volume = float(df['Volume'].iloc[-1])
            
            change_pct = ((current_price - prev_price) / prev_price) * 100

            df['SMA10'] = df['Close'].rolling(window=10).mean()
            sma_10 = float(df['SMA10'].iloc[-1])

            # شروط التصفية الذكية
            if change_pct < 1.5 or change_pct > 8.0:
                continue

            if volume < 500000:
                continue

            if current_price < sma_10:
                continue

            if ticker in alerted_stocks:
                continue

            stop_loss = current_price * 0.975
            tv_url = f"https://www.tradingview.com/chart/?symbol={ticker}"
            finviz_url = f"https://finviz.com/quote.ashx?t={ticker}"

            message = (
                f"🎯 <b>رادار كسر القناة (تصفية ذكية)</b> · {html.escape(session_name)}\n\n"
                f"🔹 السهم: <b>{html.escape(ticker)}</b>\n"
                f"💵 السعر الحالي: <code>${current_price:.2f}</code>\n"
                f"📈 التغير: <code>+{change_pct:.2f}%</code>\n"
                f"📊 الحجم: <code>{int(volume):,}</code>\n"
                f"🛑 <b>وقف الخسارة:</b> <code>${stop_loss:.2f}</code>\n\n"
                f"✅ <i>الشروط: صعود إيجابي + سيولة عالية + السعر فوق متوسط 10 أيام.</i>\n\n"
                f"🔗 <a href='{tv_url}'>TradingView</a> | <a href='{finviz_url}'>Finviz</a>"
            )

            if send_telegram_message(message, parse_mode="HTML"):
                alerted_stocks.append(ticker)
                sent += 1
                time.sleep(1)

        except Exception as e:
            print(f"⚠️ خطأ في معالجة السهم {ticker}: {e}")

    save_state(alerted_stocks)
    return sent


def main():
    print("🚀 بدء تنفيذ رادار غيت هاب الذكي...")
    session_name = "GitHub Smart Radar 🔔"

    sent = analyze_and_scan_stocks(US_STOCKS, session_name)
    print(f"✅ تم الانتهاء وإرسال {sent} تنبيه جديد بنجاح.")


if __name__ == "__main__":
    main()
