"""
TradingView -> Claude Webhook Analyzer
----------------------------------------
يستقبل هذا السيرفر تنبيهات (Alerts) من TradingView عبر Webhook،
ثم يرسل بيانات الشارت لكلود (Claude API) عشان يسوي تحليل فني كامل:
- تحليل فني (مؤشرات / دعوم ومقاومات)
- إشارة تداول (Buy / Sell / Hold)
- ملخص عام للسوق

ثم (اختياري) يرسل النتيجة لك على تيليجرام.

كيف تشتغل الفكرة:
1) تسوي Alert في TradingView وتحط فيه رابط الـ Webhook تبع هذا السيرفر (بعد رفعه على استضافة
   عندها رابط عام مثل Render / Railway / VPS، أو محلياً عبر ngrok للتجربة).
2) TradingView يرسل POST request بصيغة JSON فيه بيانات الشمعة/المؤشرات حسب القالب اللي تحدده.
3) هذا الكود يستقبل البيانات، يبنى منها Prompt، ويرسله لـ Claude API.
4) يرجع لك Claude تحليل جاهز، وإذا فعّلت تيليجرام بيوصلك مباشرة.
"""

import os
import json
import logging
from datetime import datetime, timezone, timedelta

from flask import Flask, request, jsonify
from anthropic import Anthropic

# ----------------------------------------------------------------------
# الإعدادات - خذها من متغيرات البيئة (Environment Variables) لا تحطها هنا مباشرة
# ----------------------------------------------------------------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL_NAME = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

# تيليجرام (اختياري) - إذا ما تبي إشعارات تيليجرام، خلها فاضية
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# مفتاح سري بسيط للتحقق إن الطلب فعلاً جاي من TradingView (اختياري لكن يفضل تفعيله)
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("tv-claude-bot")

app = Flask(__name__)
client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


# ----------------------------------------------------------------------
# تحويل الفريم الزمني القادم من TradingView ({{interval}}) لنص عربي واضح
# مدة الصفقة المقترحة دايماً = نفس فريم الشارت اللي مربوط فيه الـ Alert،
# مش قرار عشوائي من كلود، عشان ما يصير تعارض (مثلاً شارت 5 دقايق ويقول صفقة دقيقتين)
# ----------------------------------------------------------------------
TIMEFRAME_LABELS = {
    "1": "دقيقة واحدة",
    "2": "دقيقتين",
    "3": "3 دقائق",
    "5": "5 دقائق",
    "15": "15 دقيقة",
    "30": "30 دقيقة",
    "45": "45 دقيقة",
    "60": "ساعة واحدة",
    "120": "ساعتين",
    "180": "3 ساعات",
    "240": "4 ساعات",
    "D": "يوم كامل",
    "1D": "يوم كامل",
    "W": "أسبوع",
    "1W": "أسبوع",
}


def timeframe_to_duration(timeframe: str) -> str:
    tf = str(timeframe).strip()
    return TIMEFRAME_LABELS.get(tf, f"{tf} دقيقة" if tf else "غير محدد")


# ----------------------------------------------------------------------
# بناء الـ Prompt اللي يرسل لكلود
# ----------------------------------------------------------------------
def build_prompt(payload: dict) -> str:
    """
    payload هو الـ JSON اللي أرسلته TradingView. القالب المقترح لرسالة الـ Alert
    في TradingView (تحطه في خانة Message عند إنشاء الـ Alert):

    {
      "ticker": "{{ticker}}",
      "exchange": "{{exchange}}",
      "timeframe": "{{interval}}",
      "price": "{{close}}",
      "open": "{{open}}",
      "high": "{{high}}",
      "low": "{{low}}",
      "volume": "{{volume}}",
      "rsi": "{{plot_0}}",
      "macd": "{{plot_1}}",
      "signal_line": "{{plot_2}}",
      "time": "{{time}}"
    }

    ملاحظة: plot_0 / plot_1 ... تعتمد على ترتيب المؤشرات اللي مضايفها بالشارت،
    لازم تتأكد من ترتيبها قبل ما تربطها.
    """
    ticker = payload.get("ticker", "غير معروف")
    exchange = payload.get("exchange", "")
    timeframe = payload.get("timeframe", "")
    price = payload.get("price", "")
    rsi = payload.get("rsi", "")
    macd = payload.get("macd", "")
    signal_line = payload.get("signal_line", "")
    volume = payload.get("volume", "")
    high = payload.get("high", "")
    low = payload.get("low", "")

    prompt = f"""أنت محلل فني محترف بالأسواق المالية ومتخصص في صفقات السكالبينج قصيرة المدى.
حللي البيانات التالية القادمة من TradingView وجاوب بصيغة مختصرة جداً بالعربي، بدون شرح تقني
أو أرقام كثيرة، بنفس الشكل التالي بالضبط (بدون أي نص إضافي قبله أو بعده):

القرار: [شراء / بيع / انتظار] [رمز إيموجي مناسب: 🟢 للشراء، 🔴 للبيع، ⚪ للانتظار]
نسبة الثقة: [رقم]%
نقطة الدخول: [رقم السعر المقترح للدخول]
السبب: [جملة واحدة قصيرة وبسيطة بدون مصطلحات تقنية معقدة، لغة عامة يفهمها أي شخص]

قواعد مهمة:
- إذا كان القرار "انتظار"، اكتب في نقطة الدخول: "لا يوجد"
- نقطة الدخول تكون بالقرب من السعر الحالي مع مراعاة أقرب دعم (للشراء) أو أقرب مقاومة (للبيع)
- لا تذكر أي أهداف ربح (هدف أول أو هدف ثاني)، ولا تذكر مدة الصفقة، فقط نقطة دخول واحدة
- لا تقترح مدة الصفقة إطلاقاً، هذا يُحسب خارج تحليلك

بيانات الشارت (لاستخدامك الداخلي فقط، لا تذكرها بالرد):
- الرمز: {ticker} ({exchange})
- الفريم الزمني: {timeframe}
- السعر الحالي: {price}
- أعلى سعر: {high}
- أدنى سعر: {low}
- الحجم: {volume}
- RSI: {rsi if rsi else "غير متوفر"}
- MACD: {macd if macd else "غير متوفر"}
- Signal Line: {signal_line if signal_line else "غير متوفر"}

مهم جداً: التزم فقط بالتنسيق المطلوب أعلاه بأربعة أو خمسة أسطر، بدون تحليل فني مطول، بدون ذكر
أرقام المؤشرات الخام، وبدون مقدمات أو خواتيم أو تحذيرات إضافية."""
    return prompt


# ----------------------------------------------------------------------
# استدعاء Claude API
# ----------------------------------------------------------------------
def analyze_with_claude(payload: dict) -> str:
    if client is None:
        return "خطأ: ما تم ضبط ANTHROPIC_API_KEY في متغيرات البيئة."

    prompt = build_prompt(payload)
    try:
        response = client.messages.create(
            model=MODEL_NAME,
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        text_parts = [block.text for block in response.content if block.type == "text"]
        return "\n".join(text_parts).strip()
    except Exception as e:
        log.exception("فشل استدعاء Claude API")
        return f"خطأ أثناء التحليل: {e}"


# ----------------------------------------------------------------------
# إرسال إشعار تيليجرام (اختياري)
# ----------------------------------------------------------------------
def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    import urllib.request
    import urllib.parse

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
    }).encode()
    try:
        urllib.request.urlopen(url, data=data, timeout=10)
    except Exception:
        log.exception("فشل إرسال رسالة تيليجرام")


# ----------------------------------------------------------------------
# نقطة الاستقبال (Webhook Endpoint)
# ----------------------------------------------------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    # تحقق بسيط من السر (إذا فعلته) - أضف ?secret=xxxx في رابط الـ webhook بـ TradingView
    if WEBHOOK_SECRET:
        provided = request.args.get("secret", "")
        if provided != WEBHOOK_SECRET:
            log.warning("طلب مرفوض: سر غير صحيح")
            return jsonify({"status": "unauthorized"}), 401

    raw = request.get_data(as_text=True)
    log.info("تنبيه جديد من TradingView: %s", raw[:500])

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        # بعض تنبيهات TradingView تكون نص عادي مو JSON، نتعامل معها كنص خام
        payload = {"ticker": "غير محدد", "price": raw}

    analysis = analyze_with_claude(payload)

    # مدة الصفقة المقترحة = نفس فريم الشارت الفعلي اللي مربوط فيه الـ Alert
    # (يرجع من TradingView في حقل timeframe / {{interval}})، مش تخمين من كلود
    is_waiting = "انتظار" in analysis
    duration_label = "-" if is_waiting else timeframe_to_duration(payload.get("timeframe", ""))

    # نحط سطر "مدة الصفقة المقترحة" بعد سطر "نقطة الدخول" مباشرة داخل نص التحليل
    lines = analysis.split("\n")
    entry_line_idx = next(
        (i for i, line in enumerate(lines) if line.strip().startswith("نقطة الدخول")),
        None,
    )
    if entry_line_idx is not None:
        lines.insert(entry_line_idx + 1, f"مدة الصفقة المقترحة: {duration_label}")
        analysis = "\n".join(lines)
    else:
        # احتياطي في حال كلود ما التزم بالتنسيق المتوقع
        analysis = f"{analysis}\nمدة الصفقة المقترحة: {duration_label}"

    # وقت مصر/السعودية (GMT+3) عشان يكون واضح للمستخدم متى صار التحليل
    now_local = datetime.now(timezone.utc).astimezone(
        timezone(timedelta(hours=3))
    ).strftime("%I:%M %p")

    log.info("نتيجة التحليل:\n%s", analysis)
    ticker_name = payload.get("ticker", "")
    price = payload.get("price", "")
    telegram_msg = (
        f"📊 {ticker_name} | {price}$\n"
        f"🕒 وقت التنبيه: {now_local}\n\n"
        f"{analysis}"
    )
    send_telegram(telegram_msg)

    return jsonify({
        "status": "ok",
        "received_at": datetime.now(timezone.utc).isoformat(),
        "analysis": analysis,
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "running"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
