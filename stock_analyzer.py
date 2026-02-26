import os
import json
import logging
import anthropic

log = logging.getLogger(__name__)

# ─── System Prompt ────────────────────────────────────────────────────────────
SYSTEM = """You are an expert Indian stock market analyst with deep knowledge of NSE and BSE.
You specialize in:
  • Technical analysis (RSI, MACD, Bollinger Bands, EMA, support/resistance)
  • Fundamental catalysts (earnings, FII/DII flows, news)
  • Sector rotation and market breadth analysis
  • Nifty 50, Nifty Bank, Midcap, Smallcap indices

RULES:
1. Always search for the LATEST real-time data before responding.
2. Use exact ₹ price levels based on current market prices.
3. Minimum Risk:Reward = 1:2 for every pick.
4. Each pick must have a clear catalyst (news/technical trigger).
5. Distinguish between NSE and BSE listings.
6. Consider F&O data (PCR, OI buildup) where relevant.
7. Respond ONLY in valid JSON as specified — no preamble, no markdown fences."""

# ─── Prompt Templates ─────────────────────────────────────────────────────────
def _json_schema_stocks(n: int, hold: str) -> str:
    """Reusable stock object schema description."""
    return f"""Array of exactly {n} stock objects:
{{
  "symbol": "NSE symbol (e.g. RELIANCE)",
  "exchange": "NSE or BSE",
  "sector": "sector name",
  "reason": "2-sentence catalyst + technical setup",
  "entry_low": number,
  "entry_high": number,
  "target": number,
  "stop_loss": number,
  "upside": number (percentage),
  "risk_reward": "1:2 or better",
  "holding_period": "{hold}"
}}"""

MARKET_OPEN_PROMPT = f"""Search for: current Nifty 50 levels, pre-market SGX Nifty, top NSE gainers/losers
pre-market, FII/DII data yesterday, major news today affecting Indian markets.

Return this JSON:
{{
  "sentiment": "Bullish | Bearish | Neutral",
  "theme": "one-line market theme for today",
  "nifty_level": "current/expected Nifty 50 level",
  "nifty_support": "key support",
  "nifty_resistance": "key resistance",
  "nifty_open_estimate": "expected opening range",
  "stocks": {_json_schema_stocks(3, "Intraday | 2-3 days")},
  "sectors_to_watch": ["sector1", "sector2"],
  "avoid_sectors": ["sector1"],
  "global_cues": "US futures, Asian markets summary",
  "disclaimer": "Educational purposes only. Not SEBI registered. DYOR."
}}"""

def intraday_prompt(hour: int) -> str:
    context = {
        10: "morning session (first hour complete). Look for opening range breakouts and gap fill trades.",
        11: "mid-morning. Momentum established. Look for trend continuation and sector leaders.",
        12: "approaching noon. Pre-lunch positioning. Watch for reversal signals.",
        13: "post-lunch. Institutional activity picks up. Look for accumulation patterns.",
        14: "afternoon. F&O expiry awareness. Look for high-volume breakouts.",
        14: "pre-close setup. Last 1.5 hours. Strong momentum trades with tight stops.",
    }.get(hour, f"current {hour}:00 session.")

    return f"""It is currently {hour}:00 IST. Market context: {context}

Search for: current Nifty 50 level, top NSE gainers/volume leaders right now,
intraday breakout stocks, MACD crossovers on 15-min charts, RSI extremes.

Return this JSON:
{{
  "sentiment": "Bullish | Bearish | Neutral",
  "theme": "current intraday theme",
  "nifty_level": "current Nifty level",
  "nifty_support": "intraday support",
  "nifty_resistance": "intraday resistance",
  "stocks": {_json_schema_stocks(3, "Intraday only")},
  "sectors_to_watch": ["sector1", "sector2"],
  "avoid_sectors": [],
  "disclaimer": "Educational purposes only. Not SEBI registered. DYOR."
}}"""

CLOSING_PROMPT = f"""Market is closing / just closed. Search for: Nifty 50 closing level,
today's top gainers/losers, stocks with high delivery volume (swing setups),
global cues for tomorrow (US futures, SGX Nifty).

Return this JSON:
{{
  "sentiment": "Bullish | Bearish | Neutral",
  "day_summary": "2-sentence summary of today's session",
  "nifty_level": "closing level",
  "nifty_support": "key support",
  "nifty_resistance": "key resistance",
  "stocks": {_json_schema_stocks(3, "2-5 days")},
  "sectors_to_watch": ["sector1"],
  "avoid_sectors": [],
  "next_day_outlook": "tomorrow's expected direction with levels",
  "global_cues": "US markets, SGX Nifty, Asian outlook",
  "disclaimer": "Educational purposes only. Not SEBI registered. DYOR."
}}"""

ADHOC_PROMPT_TEMPLATE = """User has requested instant picks at {hour}:00 IST during live market hours.
Search for: Nifty level RIGHT NOW, stocks moving more than 2% today with volume surge,
technical breakouts on 15-min timeframe, high RSI momentum stocks.

Give the absolute BEST picks available at this exact moment.

Return this JSON:
{{
  "sentiment": "Bullish | Bearish | Neutral",
  "theme": "what is driving the market right now",
  "nifty_level": "live Nifty level",
  "nifty_support": "nearest support",
  "nifty_resistance": "nearest resistance",
  "stocks": {stocks},
  "sectors_to_watch": ["sector1"],
  "avoid_sectors": [],
  "disclaimer": "Educational purposes only. Not SEBI registered. DYOR."
}}""".replace("{stocks}", _json_schema_stocks(3, "Intraday | 2-3 days"))

PRE_OPEN_PROMPT = f"""It is before 9:15 AM IST. Market has not opened yet.
Search for: SGX Nifty / GIFT Nifty current level, US markets yesterday close,
Asian markets this morning, major overnight news for India,
FII activity yesterday, key stocks in news today.

Return this JSON:
{{
  "sentiment": "Expected Bullish | Bearish | Neutral",
  "theme": "expected theme for today",
  "nifty_level": "yesterday's close",
  "nifty_open_estimate": "expected opening level based on Gift Nifty",
  "nifty_support": "key support for today",
  "nifty_resistance": "key resistance for today",
  "stocks": {_json_schema_stocks(3, "Intraday | 2-3 days")},
  "key_events": "important events today (earnings, RBI, global data)",
  "sectors_to_watch": ["sector1"],
  "avoid_sectors": [],
  "global_cues": "US close, SGX Nifty, Asian markets summary",
  "disclaimer": "Educational purposes only. Not SEBI registered. DYOR."
}}"""

NEXT_DAY_PROMPT = f"""Market is closed for today. A user wants to know what to buy TOMORROW.
Search for: today's NSE closing data, top performing sectors today,
stocks near key technical breakout levels (52-week high, chart patterns),
tomorrow's economic calendar (India + US), FII/DII net activity today,
global futures tonight.

Identify 4 stocks with the best setup for TOMORROW's trading session.

Return this JSON:
{{
  "sentiment": "Expected Bullish | Bearish | Neutral for tomorrow",
  "theme": "expected theme for tomorrow",
  "nifty_level": "today's closing level",
  "nifty_open_estimate": "expected Nifty opening range tomorrow",
  "stocks": {_json_schema_stocks(4, "2-5 days")},
  "key_events": "important events tomorrow (results, macro data, global)",
  "sectors_to_watch": ["sector1", "sector2"],
  "avoid_sectors": ["sector1"],
  "global_cues": "US markets tonight, Asian cues, currency",
  "disclaimer": "Educational purposes only. Not SEBI registered. DYOR."
}}"""

WEEKEND_PROMPT = f"""It is the weekend. Indian markets are closed. A user wants to plan for NEXT WEEK.
Search for: this week's Nifty performance summary, top sector performers this week,
stocks with strong weekly charts / breakout setups for next week,
next week's key events (earnings calendar, RBI, US Fed data, IPOs),
FII net buying/selling this week.

Identify 4 stocks with the best potential for next week.

Return this JSON:
{{
  "sentiment": "Expected Bullish | Bearish | Neutral for next week",
  "theme": "expected theme for next week",
  "nifty_level": "this week's closing level",
  "nifty_support": "key weekly support",
  "nifty_resistance": "key weekly resistance",
  "stocks": {_json_schema_stocks(4, "1-2 weeks")},
  "key_events": "important events next week (earnings, macro, global)",
  "sectors_to_watch": ["sector1", "sector2"],
  "avoid_sectors": ["sector1"],
  "global_cues": "US market trend, global macro",
  "disclaimer": "Educational purposes only. Not SEBI registered. DYOR."
}}"""


# ─── Analyzer Class ───────────────────────────────────────────────────────────
class StockAnalyzer:
    def __init__(self):
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY environment variable not set")
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model  = "claude-opus-4-5"

    def _call(self, prompt: str) -> dict:
        """Call Claude with web search and parse JSON response."""
        log.info(f"  → Calling Claude ({self.model}) with web search…")
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=2000,
            system=SYSTEM,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}],
        )

        # Collect all text blocks (Claude may call web_search first, then respond)
        full_text = "".join(
            block.text for block in resp.content if block.type == "text"
        )
        log.debug(f"  Raw response preview: {full_text[:300]}")

        # Strip markdown fences if present
        text = full_text.strip()
        for fence in ("```json", "```"):
            if fence in text:
                text = text.split(fence, 1)[1].split("```")[0].strip()
                break

        # Extract outermost JSON object
        start = text.find("{")
        end   = text.rfind("}") + 1
        if start != -1 and end > start:
            text = text[start:end]

        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            log.error(f"JSON parse failed: {e}\nRaw: {text[:500]}")
            raise ValueError(f"Claude returned invalid JSON: {e}")

    # ── Public report methods ─────────────────────────────────────────────────
    def generate_market_open_report(self) -> dict:
        log.info("📊 Generating market-open report…")
        return self._call(MARKET_OPEN_PROMPT)

    def generate_intraday_report(self, hour: int) -> dict:
        log.info(f"📊 Generating intraday report (hour={hour})…")
        return self._call(intraday_prompt(hour))

    def generate_closing_report(self) -> dict:
        log.info("📊 Generating closing report…")
        return self._call(CLOSING_PROMPT)

    def generate_adhoc_report(self, hour: int) -> dict:
        log.info(f"📊 Generating ad-hoc report (hour={hour})…")
        prompt = ADHOC_PROMPT_TEMPLATE.replace("{hour}", str(hour))
        return self._call(prompt)

    def generate_pre_open_report(self) -> dict:
        log.info("📊 Generating pre-open report…")
        return self._call(PRE_OPEN_PROMPT)

    def generate_next_day_report(self) -> dict:
        log.info("📊 Generating next-day (night) report…")
        return self._call(NEXT_DAY_PROMPT)

    def generate_weekend_report(self) -> dict:
        log.info("📊 Generating weekend report…")
        return self._call(WEEKEND_PROMPT)
