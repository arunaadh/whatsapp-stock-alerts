import os
import logging
import threading
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

# ─── TwiML helper ─────────────────────────────────────────────────────────────
TWIML_OK = ('<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
            200, {"Content-Type": "text/xml"})

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


# ─── Scheduled alert job ─────────────────────────────────────────────────────
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


# ─── Message formatters (compact – stays under Twilio 1600 char limit) ─────────

def _fmt_stock(i: int, s: dict, show_hold: bool = True) -> str:
    """Single stock block ~65 chars."""
    lines = [
        f"{i}. {s.get('symbol','?')} [{s.get('exchange','NSE')}]",
        f"Entry  : ₹{s.get('entry_low','?')} – ₹{s.get('entry_high','?')}",
        f"Target : ₹{s.get('target','?')}",
        f"SL     : ₹{s.get('stop_loss','?')}",
    ]
    if show_hold:
        lines.append(f"Hold   : {s.get('holding_period','Intraday')}")
    return "\n".join(lines)

def _build(header: str, stocks: list, show_hold: bool = True, note: str = "") -> str:
    """Assemble header + stock blocks and hard-truncate to 1590 chars."""
    parts = [header]
    for i, s in enumerate(stocks[:3], 1):
        parts.append("\n" + _fmt_stock(i, s, show_hold))
    if note:
        parts.append("\n" + note)
    msg = "\n".join(parts)
    return msg[:1590]          # safety truncation – never exceed Twilio limit

def format_scheduled_message(report: dict, label: str, mode: str) -> str:
    dt  = now_ist().strftime("%d %b %I:%M %p")
    hdr = f"{label} | {dt} IST"
    return _build(hdr, report.get("stocks", []), show_hold=(mode != "intraday"))

def format_adhoc_message(report: dict) -> str:
    dt  = now_ist().strftime("%d %b %I:%M %p")
    hdr = f"Picks | {dt} IST"
    return _build(hdr, report.get("stocks", []))

def format_pre_open_message(report: dict) -> str:
    dt   = now_ist().strftime("%d %b %I:%M %p")
    hdr  = f"Pre-Market | {dt} IST"
    note = f"Gift Nifty: {report.get('nifty_open_estimate','N/A')}"
    return _build(hdr, report.get("stocks", []), note=note)

def format_night_message(report: dict) -> str:
    dt  = now_ist().strftime("%d %b %I:%M %p")
    hdr = f"Tomorrow Picks | {dt} IST"
    note = f"Nifty est: {report.get('nifty_open_estimate','N/A')}"
    return _build(hdr, report.get("stocks", []), note=note)

def format_weekend_message(report: dict) -> str:
    dt  = now_ist().strftime("%d %b %I:%M %p")
    hdr = f"Next Week Picks | {dt} IST"
    return _build(hdr, report.get("stocks", []))


# ─── Smart ad-hoc dispatcher (runs in background thread) ─────────────────────
def handle_adhoc(from_number: str):
    """
    Runs in a background thread so the webhook can return 200 to Twilio
    immediately. Claude + web search takes 20-60s — far beyond Twilio's
    15-second timeout if run synchronously.
    """
    dt      = now_ist()
    session = market_session(dt)
    log.info(f"  [thread] Ad-hoc [{from_number}] session={session}")

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
        reply = (
            "⚠️  Sorry, couldn't generate picks right now.\n"
            "Please try again in a minute.\n\n"
            f"_{str(e)[:120]}_"
        )

    try:
        whatsapp.send_message(from_number, reply)
    except Exception as e:
        log.error(f"Failed to deliver ad-hoc reply to {from_number}: {e}")


def _spawn_adhoc(from_number: str):
    """Fire-and-forget: run handle_adhoc in a daemon thread."""
    t = threading.Thread(target=handle_adhoc, args=(from_number,), daemon=True)
    t.start()


# ─── Twilio webhook ──────────────────────────────────────────────────────────
@app.route("/webhook/whatsapp", methods=["GET", "POST"])
def whatsapp_webhook():
    # Twilio sends GET to verify the URL — respond with empty TwiML
    if request.method == "GET":
        return TWIML_OK

    try:
        from_number = request.form.get("From", "").replace("whatsapp:", "").strip()
        body        = request.form.get("Body", "").strip()
        cmd         = body.lower()
        dt          = now_ist()
        session     = market_session(dt)

        log.info(f"📩 [{from_number}] '{body}'  session={session}")

        if not from_number:
            log.warning("Webhook called with no From number")
            return TWIML_OK

        # ── Subscription commands ─────────────────────────────────────────────
        if cmd in ("start", "subscribe", "hi", "hello", "hey"):
            subscribers.add(from_number)
            reply = (
                "✅  *Subscribed to India Stock Alerts!*\n\n"
                "📅  *Automated Alerts (Mon–Fri IST)*\n"
                "    9:20 AM  – Market Open Picks\n"
                "   10:00 AM  – Hourly Update\n"
                "   11:00 AM  – Hourly Update\n"
                "   12:00 PM  – Noon Update\n"
                "    1:00 PM  – Hourly Update\n"
                "    2:00 PM  – Hourly Update\n"
                "    2:30 PM  – Pre-Close Picks\n"
                "    3:00 PM  – Closing Picks\n\n"
                "📲  *Message anytime* for instant picks!\n"
                "    Market hours  → Live intraday picks\n"
                "    Night time    → Tomorrow's watchlist\n"
                "    Weekend       → Next week's picks\n\n"
                "Commands: *stop* | *picks* | *help*\n\n"
                "⚠️  For educational purposes only."
            )
            try:
                whatsapp.send_message(from_number, reply)
            except Exception as e:
                log.error(f"Failed to send subscribe reply: {e}")

        elif cmd in ("stop", "unsubscribe"):
            subscribers.remove(from_number)
            try:
                whatsapp.send_message(from_number,
                    "❌  Unsubscribed. Send *start* to re-subscribe anytime.")
            except Exception as e:
                log.error(f"Failed to send unsubscribe reply: {e}")

        elif cmd == "help":
            reply = (
                "📋  *Commands*\n\n"
                "  *start*  – Subscribe to auto alerts\n"
                "  *stop*   – Unsubscribe\n"
                "  *picks*  – Instant stock picks\n"
                "  *help*   – This menu\n\n"
                "💡  Or send *any message* for smart picks!\n\n"
                "  📈 Market hours → Intraday picks\n"
                "  🌙 Night time   → Tomorrow's watchlist\n"
                "  📅 Weekend      → Next week picks"
            )
            try:
                whatsapp.send_message(from_number, reply)
            except Exception as e:
                log.error(f"Failed to send help reply: {e}")

        else:
            # ── Any other message → smart ad-hoc picks ───────────────────────
            subscribers.add(from_number)   # auto-subscribe on first message

            # Acknowledge immediately — Claude takes 20-60s
            wait_msg = {
                "market"    : "🔍  Analyzing live market data...\nPicks arriving in ~30 sec ⏳",
                "pre_open"  : "🌄  Checking pre-market signals...\nPicks arriving in ~30 sec ⏳",
                "post_close": "📊  Building tomorrow's watchlist...\nPicks arriving in ~30 sec ⏳",
                "night"     : "🌙  Preparing tomorrow's picks...\nPicks arriving in ~30 sec ⏳",
                "weekend"   : "📅  Scanning next week's opportunities...\nPicks arriving in ~30 sec ⏳",
            }.get(session, "⏳  Analyzing... picks arriving in ~30 sec")

            try:
                whatsapp.send_message(from_number, wait_msg)
            except Exception as e:
                log.error(f"Failed to send wait message: {e}")

            # ✅ KEY FIX: spawn background thread → return 200 to Twilio immediately
            _spawn_adhoc(from_number)

    except Exception as e:
        # Catch-all: log but always return 200 so Twilio doesn't keep retrying
        log.error(f"Unhandled webhook error: {e}", exc_info=True)

    return TWIML_OK   # always return TwiML 200


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

@app.route("/trigger/<slot>", methods=["POST"])
def trigger_alert(slot):
    """Manual trigger: open | 10am | 11am | noon | 1pm | 2pm | 230pm | closing | night | weekend"""
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
        return jsonify({"error": f"Unknown slot. Options: {list(mapping.keys())}"}), 400

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
        log.error(f"Trigger error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# ─── Entry point ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

# ─── Debug & Test endpoints ───────────────────────────────────────────────────
@app.route("/debug", methods=["GET"])
def debug():
    """
    Visit https://your-app.onrender.com/debug to check all config at a glance.
    Shows masked env vars, subscriber list, and Twilio connectivity test.
    """
    import traceback

    # Check env vars (masked)
    sid   = os.environ.get("TWILIO_ACCOUNT_SID", "")
    token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    key   = os.environ.get("ANTHROPIC_API_KEY", "")
    wa    = os.environ.get("TWILIO_WHATSAPP_NUMBER", "")

    env_status = {
        "TWILIO_ACCOUNT_SID"    : f"{sid[:6]}…{sid[-4:]}" if len(sid) > 10 else ("❌ MISSING" if not sid else sid),
        "TWILIO_AUTH_TOKEN"     : f"{token[:4]}…{token[-4:]}" if len(token) > 8 else ("❌ MISSING" if not token else "set"),
        "ANTHROPIC_API_KEY"     : f"{key[:7]}…{key[-4:]}" if len(key) > 11 else ("❌ MISSING" if not key else "set"),
        "TWILIO_WHATSAPP_NUMBER": wa if wa else "❌ MISSING",
    }

    # Test Twilio credentials by fetching account info
    twilio_ok  = False
    twilio_err = None
    try:
        from twilio.rest import Client
        c = Client(sid, token)
        acct = c.api.accounts(sid).fetch()
        twilio_ok = True
        twilio_info = f"Account '{acct.friendly_name}' status={acct.status}"
    except Exception as e:
        twilio_err = str(e)
        twilio_info = f"❌ {e}"

    dt = now_ist()
    return jsonify({
        "server_time"  : dt.strftime("%d %b %Y %I:%M %p IST"),
        "session"      : market_session(dt),
        "env_vars"     : env_status,
        "twilio"       : {"ok": twilio_ok, "info": twilio_info},
        "subscribers"  : subscribers.get_all(),
        "whatsapp_from": os.environ.get("TWILIO_WHATSAPP_NUMBER", "NOT SET"),
        "hint": (
            "If twilio.ok is false → check your SID/token in Render env vars. "
            "If env vars look right but still no reply → make sure you joined "
            "the sandbox by sending 'join <code>' to +14155238886 on WhatsApp first."
        )
    })


@app.route("/test/send", methods=["POST"])
def test_send():
    """
    POST /test/send  with JSON body: {"to": "+919XXXXXXXXX", "msg": "hello"}
    Useful to verify Twilio can actually deliver a message.
    """
    data = request.get_json(force=True, silent=True) or {}
    to   = data.get("to", "").strip()
    msg  = data.get("msg", "✅ Test message from WhatsApp Stock Bot").strip()

    if not to:
        return jsonify({"error": "Provide 'to' field with E.164 number e.g. +919XXXXXXXXX"}), 400

    try:
        sid = whatsapp.send_message(to, msg)
        return jsonify({"status": "sent", "sid": sid, "to": to})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/test/webhook", methods=["POST"])
def test_webhook():
    """
    Simulate an incoming WhatsApp message without needing Twilio.
    POST JSON: {"from": "+919XXXXXXXXX", "body": "hi"}
    """
    data        = request.get_json(force=True, silent=True) or {}
    from_number = data.get("from", "+910000000000").replace("whatsapp:", "").strip()
    body        = data.get("body", "hi").strip()
    cmd         = body.lower()
    dt          = now_ist()
    session     = market_session(dt)

    log.info(f"🧪 test/webhook [{from_number}] '{body}' session={session}")

    if cmd in ("start", "subscribe", "hi", "hello", "hey"):
        subscribers.add(from_number)
        return jsonify({
            "status"    : "would_send_subscribe_message",
            "to"        : from_number,
            "session"   : session,
            "subscriber_added": True,
            "all_subscribers" : subscribers.get_all(),
        })
    elif cmd in ("stop", "unsubscribe"):
        subscribers.remove(from_number)
        return jsonify({"status": "would_send_unsubscribe", "to": from_number})
    else:
        return jsonify({
            "status"   : "would_spawn_adhoc_thread",
            "to"       : from_number,
            "session"  : session,
            "note"     : "In real webhook this fires a background AI analysis thread",
        })
