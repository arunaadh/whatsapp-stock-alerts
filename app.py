import os
import logging
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime
import pytz

from stock_analyzer import StockAnalyzer
from whatsapp_service import WhatsAppService
from subscriber_store import SubscriberStore

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s – %(message)s",
)
log = logging.getLogger(__name__)

# ─── App & Services ──────────────────────────────────────────────────────────
app         = Flask(__name__)
IST         = pytz.timezone("Asia/Kolkata")
analyzer    = StockAnalyzer()
whatsapp    = WhatsAppService()
subscribers = SubscriberStore()


# ─── Time / session helpers ──────────────────────────────────────────────────
def now_ist() -> datetime:
    return datetime.now(IST)

def market_session(dt: datetime) -> str:
    """
    Classify the current moment:
        pre_open   → weekday before 9:15 AM
        market     → 9:15 AM – 3:30 PM  (Mon-Fri)
        post_close → 3:30 PM – 6:00 PM  (Mon-Fri)
        night      → 6:00 PM – 9:15 AM
        weekend    → Saturday / Sunday
    """
    if dt.weekday() >= 5:
        return "weekend"
    mins = dt.hour * 60 + dt.minute
    if mins <  9 * 60 + 15:  return "pre_open"
    if mins <= 15 * 60 + 30: return "market"
    if mins <= 18 * 60:      return "post_close"
    return "night"

SLOT_LABELS = {
    (9,  20): "🌅 MARKET OPEN",
    (10,  0): "📊 10 AM UPDATE",
    (11,  0): "📈 11 AM UPDATE",
    (12,  0): "☀️ NOON UPDATE",
    (13,  0): "🔆 1 PM UPDATE",
    (14,  0): "🔥 2 PM UPDATE",
    (14, 30): "⚡ 2:30 PM UPDATE",
    (15,  0): "🌆 CLOSING UPDATE",
}


# ─── Broadcast ───────────────────────────────────────────────────────────────
def _broadcast(message: str):
    numbers = subscribers.get_all()
    if not numbers:
        log.warning("No subscribers – skipping broadcast")
        return
    for num in numbers:
        try:
            whatsapp.send_message(num, message)
        except Exception as e:
            log.error(f"  ✗ {num}: {e}")


# ─── Scheduled alert job (one handler for ALL slots) ─────────────────────────
def run_scheduled_alert(hour: int, minute: int = 0):
    dt    = now_ist()
    label = SLOT_LABELS.get((hour, minute), f"📣 {hour:02d}:{minute:02d} UPDATE")
    log.info(f"🔔 Scheduled → {label}")
    try:
        if hour == 9 and minute == 20:
            report = analyzer.generate_market_open_report()
            mode   = "open"
        elif hour == 15:
            report = analyzer.generate_closing_report()
            mode   = "closing"
        else:
            report = analyzer.generate_intraday_report(hour)
            mode   = "intraday"
        msg = format_scheduled_message(report, label, mode)
        _broadcast(msg)
    except Exception as e:
        log.error(f"Alert {label} failed: {e}")


# ─── Scheduler ───────────────────────────────────────────────────────────────
scheduler = BackgroundScheduler(timezone=IST)

SCHEDULE = [
    (9,  20),
    (10,  0),
    (11,  0),
    (12,  0),
    (13,  0),
    (14,  0),
    (14, 30),
    (15,  0),
]

for h, m in SCHEDULE:
    scheduler.add_job(
        run_scheduled_alert,
        CronTrigger(day_of_week="mon-fri", hour=h, minute=m),
        args=[h, m],
        id=f"slot_{h:02d}{m:02d}",
    )

scheduler.start()
log.info(f"✅ Scheduler running – {len(SCHEDULE)} slots Mon-Fri IST")


# ─── Message formatters ───────────────────────────────────────────────────────
def _emoji(sentiment: str) -> str:
    s = sentiment.lower()
    if "bullish" in s: return "🟢"
    if "bearish" in s: return "🔴"
    if "neutral" in s: return "🟡"
    return "⚪"

def _stock_lines(i: int, s: dict, show_hold: bool = True) -> list:
    lines = [
        f"\n*{i}. {s.get('symbol','?')}*  [{s.get('exchange','NSE')}]",
        f"   🏷️  {s.get('sector','')}",
        f"   💡  {s.get('reason','')}",
        f"   📈  Entry  : ₹{s.get('entry_low','?')} – ₹{s.get('entry_high','?')}",
        f"   🎯  Target : ₹{s.get('target','?')}  (+{s.get('upside','?')}%)",
        f"   🛑  SL     : ₹{s.get('stop_loss','?')}",
        f"   ⚖️   R:R    : {s.get('risk_reward','1:2')}",
    ]
    if show_hold:
        lines.append(f"   ⏱  Hold   : {s.get('holding_period','Intraday')}")
    return lines

def _header(title: str, report: dict) -> list:
    dt = now_ist()
    return [
        f"*{title}*",
        f"📅  {dt.strftime('%d %b %Y, %I:%M %p IST')}",
        f"📊  Sentiment : {report.get('sentiment','N/A')} {_emoji(report.get('sentiment',''))}",
        f"📉  Nifty     : {report.get('nifty_level','N/A')}",
    ]

def _footer(report: dict, extras: list = None) -> list:
    lines = []
    if report.get("sectors_to_watch"):
        lines.append(f"\n👀  *Watch* : {', '.join(report['sectors_to_watch'])}")
    if report.get("avoid_sectors"):
        lines.append(f"🚫  *Avoid* : {', '.join(report['avoid_sectors'])}")
    if extras:
        lines += extras
    lines += ["", f"⚠️  {report.get('disclaimer','For educational purposes only. DYOR.')}"]
    return lines

def _divider(title: str) -> list:
    return ["", "━━━━━━━━━━━━━━━━━━━━━━", f"🎯  *{title}*", "━━━━━━━━━━━━━━━━━━━━━━"]

# ── Scheduled (open / intraday / closing) ────────────────────────────────────
def format_scheduled_message(report: dict, label: str, mode: str) -> str:
    lines = _header(label, report)
    if report.get("theme"):
        lines.append(f"📰  {report['theme']}")

    if mode == "open":
        lines += _divider("TOP PICKS – TODAY")
    elif mode == "closing":
        lines += _divider("SWING TRADE PICKS")
        if report.get("day_summary"):
            lines.insert(4, f"📋  {report['day_summary']}")
    else:
        lines += _divider("LIVE INTRADAY PICKS")

    for i, s in enumerate(report.get("stocks", [])[:3], 1):
        lines += _stock_lines(i, s, show_hold=(mode != "intraday"))

    extras = []
    if mode == "closing" and report.get("next_day_outlook"):
        extras.append(f"\n🔭  *Tomorrow* : {report['next_day_outlook']}")
    if report.get("global_cues"):
        extras.append(f"🌐  *Global*   : {report['global_cues']}")

    lines += _footer(report, extras)
    return "\n".join(lines)

# ── Ad-hoc (during market hours) ─────────────────────────────────────────────
def format_adhoc_message(report: dict) -> str:
    lines = _header("📲  INSTANT PICKS", report)
    lines += _divider("BEST PICKS RIGHT NOW")
    for i, s in enumerate(report.get("stocks", [])[:3], 1):
        lines += _stock_lines(i, s)
    lines += _footer(report)
    return "\n".join(lines)

# ── Pre-open ──────────────────────────────────────────────────────────────────
def format_pre_open_message(report: dict) -> str:
    lines = _header("🌄  PRE-MARKET WATCHLIST", report)
    lines.append(f"⏰  Gift Nifty : {report.get('nifty_open_estimate','N/A')}")
    lines += _divider("STOCKS TO WATCH TODAY")
    for i, s in enumerate(report.get("stocks", [])[:3], 1):
        lines += _stock_lines(i, s)
    extras = []
    if report.get("key_events"):
        extras.append(f"\n📋  Key Events : {report['key_events']}")
    lines += _footer(report, extras)
    return "\n".join(lines)

# ── Night / post-close ────────────────────────────────────────────────────────
def format_night_message(report: dict) -> str:
    stocks = report.get("stocks", [])
    dt     = now_ist()
    lines  = [
        "🌙  *TOMORROW'S WATCHLIST*",
        f"📅  {dt.strftime('%d %b %Y, %I:%M %p IST')}",
        f"📊  Tomorrow's Outlook : {report.get('sentiment','N/A')} {_emoji(report.get('sentiment',''))}",
        f"📰  Theme : {report.get('theme','')}",
    ]
    lines += _divider("BUY TOMORROW – TOP PICKS")
    for i, s in enumerate(stocks[:4], 1):
        lines += [
            f"\n*{i}. {s.get('symbol','?')}*  [{s.get('exchange','NSE')}]",
            f"   🏷️  {s.get('sector','')}",
            f"   💡  {s.get('reason','')}",
            f"   📈  Entry Tmr  : ₹{s.get('entry_low','?')} – ₹{s.get('entry_high','?')}",
            f"   🎯  Target     : ₹{s.get('target','?')}  (+{s.get('upside','?')}%)",
            f"   🛑  SL         : ₹{s.get('stop_loss','?')}",
            f"   ⏱  Horizon    : {s.get('holding_period','2-3 days')}",
        ]
    lines += [
        "",
        f"🌐  *Global Cues*    : {report.get('global_cues','N/A')}",
        f"📋  *Key Events*     : {report.get('key_events','N/A')}",
        f"📉  *Nifty Open Est* : {report.get('nifty_open_estimate','N/A')}",
        "",
        "⏰  _Set price alerts at entry levels. Check pre-market at 9 AM._",
        "",
        f"⚠️  {report.get('disclaimer','For educational purposes only. DYOR.')}",
    ]
    return "\n".join(lines)

# ── Weekend ────────────────────────────────────────────────────────────────────
def format_weekend_message(report: dict) -> str:
    stocks = report.get("stocks", [])
    dt     = now_ist()
    lines  = [
        "📅  *WEEKEND WATCHLIST*",
        f"📅  {dt.strftime('%d %b %Y, %I:%M %p IST')}",
        f"📊  Next Week Outlook : {report.get('sentiment','N/A')} {_emoji(report.get('sentiment',''))}",
    ]
    lines += _divider("PICKS FOR NEXT WEEK")
    for i, s in enumerate(stocks[:4], 1):
        lines += [
            f"\n*{i}. {s.get('symbol','?')}*  [{s.get('exchange','NSE')}]",
            f"   🏷️  {s.get('sector','')}",
            f"   💡  {s.get('reason','')}",
            f"   📈  Entry  : ₹{s.get('entry_low','?')} – ₹{s.get('entry_high','?')}",
            f"   🎯  Target : ₹{s.get('target','?')}  (+{s.get('upside','?')}%)",
            f"   🛑  SL     : ₹{s.get('stop_loss','?')}",
            f"   ⏱  Hold   : {s.get('holding_period','1 week')}",
        ]
    lines += [
        "",
        f"📋  *Key Events Next Week* : {report.get('key_events','N/A')}",
        f"🌐  *Global Watch*         : {report.get('global_cues','N/A')}",
        "",
        f"⚠️  {report.get('disclaimer','For educational purposes only. DYOR.')}",
    ]
    return "\n".join(lines)


# ─── Smart ad-hoc dispatcher ─────────────────────────────────────────────────
def handle_adhoc(from_number: str):
    dt      = now_ist()
    session = market_session(dt)
    log.info(f"  Ad-hoc [{from_number}]  session={session}")

    try:
        if session == "market":
            report = analyzer.generate_adhoc_report(dt.hour)
            reply  = format_adhoc_message(report)
        elif session == "pre_open":
            report = analyzer.generate_pre_open_report()
            reply  = format_pre_open_message(report)
        elif session in ("night", "post_close"):
            report = analyzer.generate_next_day_report()
            reply  = format_night_message(report)
        else:  # weekend
            report = analyzer.generate_weekend_report()
            reply  = format_weekend_message(report)
    except Exception as e:
        log.error(f"Ad-hoc report error: {e}")
        reply = f"⚠️ Could not generate picks right now.\nPlease retry in a moment.\n\n_{e}_"

    whatsapp.send_message(from_number, reply)


# ─── Twilio webhook ──────────────────────────────────────────────────────────
@app.route("/webhook/whatsapp", methods=["GET", "POST"])
def whatsapp_webhook():
    from_number = request.form.get("From", "").replace("whatsapp:", "")
    body        = request.form.get("Body", "").strip()
    cmd         = body.lower()
    dt          = now_ist()
    session     = market_session(dt)

    log.info(f"📩 [{from_number}] '{body}'  session={session}")

    # ── Subscription ─────────────────────────────────────────────────────────
    if cmd in ("start", "subscribe", "hi", "hello", "hey"):
        subscribers.add(from_number)
        reply = (
            "✅  *Subscribed to India Stock Alerts!*\n\n"
            "📅  *Automated Alerts (Mon–Fri IST)*\n"
            "   9:20 AM  – Market Open Picks\n"
            "  10:00 AM  – Hourly Update\n"
            "  11:00 AM  – Hourly Update\n"
            "  12:00 PM  – Noon Update\n"
            "   1:00 PM  – Hourly Update\n"
            "   2:00 PM  – Hourly Update\n"
            "   2:30 PM  – Pre-Close Picks\n"
            "   3:00 PM  – Closing Picks\n\n"
            "📲  *Message anytime* for instant picks!\n"
            "   • Market hours  → Live intraday picks\n"
            "   • Night time    → Tomorrow's watchlist\n"
            "   • Weekend       → Next week's picks\n\n"
            "Commands: *stop* | *picks* | *help*\n\n"
            "⚠️  For educational purposes only."
        )
        whatsapp.send_message(from_number, reply)
        return jsonify({"status": "ok"})

    elif cmd in ("stop", "unsubscribe"):
        subscribers.remove(from_number)
        reply = "❌  Unsubscribed. Send *start* to re-subscribe anytime."
        whatsapp.send_message(from_number, reply)
        return jsonify({"status": "ok"})

    elif cmd == "help":
        reply = (
            "📋  *Commands*\n\n"
            "  *start*  – Subscribe to auto alerts\n"
            "  *stop*   – Unsubscribe\n"
            "  *picks*  – Instant stock picks\n"
            "  *help*   – This menu\n\n"
            "💡  Or send *any message* for smart picks!\n\n"
            "Smart picks adapt to the time:\n"
            "  📈 Market hours → Intraday picks\n"
            "  🌙 Night time   → Tomorrow's watchlist\n"
            "  📅 Weekend      → Next week picks"
        )
        whatsapp.send_message(from_number, reply)
        return jsonify({"status": "ok"})

    # ── Any other message → smart ad-hoc picks ───────────────────────────────
    else:
        subscribers.add(from_number)   # auto-subscribe on first message
        # Send instant "please wait" acknowledgment
        wait_msg = {
            "market"    : "🔍  Analyzing live market data... ~30 sec ⏳",
            "pre_open"  : "🌄  Checking pre-market signals... ~30 sec ⏳",
            "post_close": "📊  Building tomorrow's watchlist... ~30 sec ⏳",
            "night"     : "🌙  Preparing tomorrow's picks... ~30 sec ⏳",
            "weekend"   : "📅  Scanning next week's opportunities... ~30 sec ⏳",
        }.get(session, "⏳  Analyzing... please wait ~30 sec")

        whatsapp.send_message(from_number, wait_msg)
        handle_adhoc(from_number)
        return jsonify({"status": "ok"})


# ─── REST endpoints ──────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    dt = now_ist()
    return jsonify({
        "status"   : "healthy",
        "time_ist" : dt.strftime("%d %b %Y %I:%M %p IST"),
        "session"  : market_session(dt),
        "schedule" : [f"{h:02d}:{m:02d}" for h, m in SCHEDULE],
    })

@app.route("/subscribers", methods=["GET"])
def list_subscribers():
    subs = subscribers.get_all()
    return jsonify({"subscribers": subs, "count": len(subs)})

@app.route("/trigger/<slot>", methods=["GET", "POST"])
def trigger_alert(slot):
    """Manual trigger: open | 10am | 11am | noon | 1pm | 2pm | 230pm | closing"""
    mapping = {
        "open"    : (9,  20),
        "10am"    : (10,  0),
        "11am"    : (11,  0),
        "noon"    : (12,  0),
        "1pm"     : (13,  0),
        "2pm"     : (14,  0),
        "230pm"   : (14, 30),
        "closing" : (15,  0),
        "night"   : None,
        "weekend" : None,
    }
    if slot not in mapping:
        return jsonify({"error": f"Unknown slot. Use: {list(mapping.keys())}"}), 400

    try:
        if slot == "night":
            report = analyzer.generate_next_day_report()
            _broadcast(format_night_message(report))
        elif slot == "weekend":
            report = analyzer.generate_weekend_report()
            _broadcast(format_weekend_message(report))
        else:
            h, m = mapping[slot]
            run_scheduled_alert(h, m)
        return jsonify({"status": "triggered", "slot": slot})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Entry point ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
