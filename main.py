"""
Portfolio HUD backend — step 2
- serves/stores portfolio.json for the dashboard
- pulls live quotes from Finnhub
- every 5 min (US market hours): checks sell_above / buy_below rules -> WhatsApp
- after US close: daily P/L summary + watchlist opportunities (Claude) -> WhatsApp
"""
import os, re, json, time, threading, urllib.parse, datetime as dt
from zoneinfo import ZoneInfo
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler

FINNHUB = os.environ["FINNHUB_KEY"]
CMB_PHONE = os.environ.get("CALLMEBOT_PHONE", "")        # +9725xxxxxxx
CMB_KEY = os.environ.get("CALLMEBOT_KEY", "")
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")          # fallback channel until CallMeBot has a slot
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DATA = os.environ.get("DATA_PATH", "portfolio.json")
NY = ZoneInfo("America/New_York")

# first boot with a Volume: seed DATA_PATH from the portfolio.json shipped in the repo
_REPO_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portfolio.json")
if not os.path.exists(DATA):
    os.makedirs(os.path.dirname(os.path.abspath(DATA)) or ".", exist_ok=True)
    import shutil; shutil.copy(_REPO_JSON, DATA); print("seeded", DATA, "from repo")

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_lock = threading.Lock()
_quotes = {}                    # ticker -> {c, d, dp, pc, t}
_alerted = {}                   # "TICKER:rule" -> date string (one alert per rule per day)
SIGNALS = os.path.join(os.path.dirname(os.path.abspath(DATA)), "signals.json")   # alerts log + news items

def load_signals():
    try:
        with open(SIGNALS, encoding="utf-8") as f: return json.load(f)
    except Exception: return {"items": []}

def add_signal(kind, text, tickers=(), url="", impact="?"):
    s = load_signals()
    s["items"].insert(0, dict(ts=dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"), kind=kind, text=text,
                              tickers=list(tickers), url=url, impact=impact))
    s["items"] = s["items"][:300]
    with open(SIGNALS, "w", encoding="utf-8") as f: json.dump(s, f, ensure_ascii=False)

def load():
    with _lock, open(DATA, encoding="utf-8") as f:
        return json.load(f)

def store(p):
    with _lock, open(DATA, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)

def all_tickers(p):
    return sorted({h["ticker"] for h in p["holdings"]} | {w["ticker"] for w in p["watchlist"]})

def whatsapp(text):
    """Sends to WhatsApp (CallMeBot) if configured, else Telegram, else just logs."""
    sent = False
    if CMB_PHONE and CMB_KEY:
        url = f"https://api.callmebot.com/whatsapp.php?phone={CMB_PHONE}&apikey={CMB_KEY}&text={urllib.parse.quote(text)}"
        try: requests.get(url, timeout=20); sent = True
        except Exception as e: print("whatsapp error", e)
    if TG_TOKEN and TG_CHAT:
        try: requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={"chat_id": TG_CHAT, "text": text}, timeout=20); sent = True
        except Exception as e: print("telegram error", e)
    if not sent: print("[notify skipped — no channel configured]", text)

def fetch_quote(t):
    r = requests.get("https://finnhub.io/api/v1/quote", params={"symbol": t, "token": FINNHUB}, timeout=10)
    q = r.json()
    if q.get("c"): _quotes[t] = q

# market overview: (label, finnhub symbol or None for fx, TradingView symbol)
MARKETS = [("S&P 500", "SPY", "SP:SPX"), ("Nasdaq 100", "QQQ", "NASDAQ:NDX"), ("Dow", "DIA", "DJ:DJI"), ("Russell 2000", "IWM", "CBOE:RUT"),
           ("VIX", "VIXY", "CBOE:VIX"), ("USD/ILS", None, "FX_IDC:USDILS"), ("Bitcoin", "BINANCE:BTCUSDT", "BITSTAMP:BTCUSD"), ("Ethereum", "BINANCE:ETHUSDT", "BITSTAMP:ETHUSD"),
           ("Solana", "BINANCE:SOLUSDT", "BINANCE:SOLUSDT"), ("XRP", "BINANCE:XRPUSDT", "BITSTAMP:XRPUSD"), ("זהב", "GLD", "TVC:GOLD"), ("נפט", "USO", "TVC:USOIL")]
_markets = {}

def refresh_markets(fx):
    for label, sym, tv in MARKETS:
        try:
            if sym is None: q = {"c": fx, "dp": 0, "pc": fx}
            else: q = requests.get("https://finnhub.io/api/v1/quote", params={"symbol": sym, "token": FINNHUB}, timeout=10).json(); time.sleep(1.05)
            if q.get("c"): _markets[label] = {"c": q["c"], "dp": q.get("dp") or 0, "tv": tv, "proxy": sym if sym and ":" not in sym else ""}
        except Exception as e: print("market error", label, e)

def fetch_fx():
    """USD/ILS — free, no key. Broker values the portfolio at the live rate, so we must too."""
    for url, path in (("https://open.er-api.com/v6/latest/USD", ("rates", "ILS")), ("https://api.frankfurter.app/latest?from=USD&to=ILS", ("rates", "ILS"))):
        try:
            j = requests.get(url, timeout=10).json(); v = j
            for k in path: v = v[k]
            if v and 2 < float(v) < 6: return round(float(v), 4)
        except Exception as e: print("fx error", url, e)
    return None

def refresh_quotes():
    p = load()
    fx = fetch_fx()
    if fx: p["meta"]["usd_ils"] = fx
    refresh_markets(fx or p["meta"]["usd_ils"])
    for t in all_tickers(p):
        try: fetch_quote(t); time.sleep(1.05)   # free tier: 60 calls/min
        except Exception as e: print("quote error", t, e)
    for h in p["holdings"]:
        if h["ticker"] in _quotes: h["last_price"] = _quotes[h["ticker"]]["c"]
    p["meta"]["last_quote_update"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
    store(p)

def market_open():
    now = dt.datetime.now(NY)
    return now.weekday() < 5 and dt.time(9, 30) <= now.time() <= dt.time(16, 5)

def check_rules():
    if not market_open(): return
    refresh_quotes()
    p = load(); today = dt.date.today().isoformat()
    def fire(t, rule, cond, label, level, q):
        key = f"{t}:{rule}"
        if cond and _alerted.get(key) != today:
            _alerted[key] = today
            msg = f"⚡ {t} {label}: ${q['c']:.2f} (כלל: {level})  יומי {q['dp']:+.1f}%"
            add_signal("alert", msg, [t]); whatsapp(msg)
    for w in p["watchlist"]:
        q = _quotes.get(w["ticker"])
        if not q: continue
        fire(w["ticker"], "sell_above", q["c"] >= (w.get("sell_above") or 1e18), "הגיעה לשער מכירה", w.get("sell_above"), q)
        fire(w["ticker"], "buy_below", q["c"] <= (w.get("buy_below") or -1), "ירדה לשער קנייה", w.get("buy_below"), q)
    for h in p["holdings"]:        # holdings: thesis sell_target / stop act as default rules
        q = _quotes.get(h["ticker"]); th = h.get("thesis") or {}
        if not q or not isinstance(th, dict): continue
        fire(h["ticker"], "sell_target", q["c"] >= (th.get("sell_target") or 1e18), "הגיעה ליעד המכירה מהתזה", th.get("sell_target"), q)
        fire(h["ticker"], "stop", q["c"] <= (th.get("stop") or -1), "🔻 ירדה מתחת ל-stop — התזה נשברה?", th.get("stop"), q)

def portfolio_summary(p):
    fx = p["meta"]["usd_ils"]; rows = []
    for h in p["holdings"]:
        q = _quotes.get(h["ticker"], {"c": h["last_price"], "dp": 0, "pc": h["last_price"]})
        value = h["qty"] * q["c"] * fx; cost = h["qty"] * h["avg_cost_ils"]
        rows.append(dict(t=h["ticker"], value=value, pl=value - cost, day=h["qty"] * (q["c"] - q["pc"]) * fx, dp=q["dp"]))
    return rows

def watchlist_candidates(p):
    out = []
    for w in p["watchlist"]:
        q = _quotes.get(w["ticker"])
        if not q: continue
        hit = w.get("buy_below") and q["c"] <= w["buy_below"]
        if hit or q["dp"] <= -5: out.append(dict(t=w["ticker"], c=q["c"], dp=q["dp"], hit=bool(hit)))
    return sorted(out, key=lambda x: x["dp"])[:8]

def claude_take(rows, cands):
    if not ANTHROPIC_KEY or not cands: return ""
    try:
        import anthropic
        msg = anthropic.Anthropic(api_key=ANTHROPIC_KEY).messages.create(
            model="claude-sonnet-4-6", max_tokens=400,
            messages=[{"role": "user", "content":
                "אתה 'הקול השפוי' של תיק מניות. בעברית, עד 5 שורות, בלי הקדמות. "
                "תיק (טיקר, רווח/הפסד ₪, יומי %): " + json.dumps([[r['t'], round(r['pl']), r['dp']] for r in rows]) +
                ". מועמדים מרשימת המעקב שירדו היום או הגיעו לשער קנייה: " + json.dumps(cands) +
                ". אמור מי מהמועמדים שווה בדיקה ולמה, ומה הסיכון בתיק כרגע. זו לא המלצת השקעה — הצג כשאלות לבדיקה."}])
        return msg.content[0].text
    except Exception as e:
        return f"(Claude לא זמין: {e})"

# ---------------- Thesis agent (step 3) ----------------
THESIS_PROMPT = """אתה אנליסט מניות זהיר. הכן כרטיס תזה על {ticker} ({name}) למשקיע פרטי ישראלי.
מחיר נוכחי: ${price}. פוזיציה: {qty} מניות, ממוצע ${avg}, {weight:.1f}% מהתיק. שאר התיק לפי נושאים: {themes}.
התזה של המשקיע עצמו (אם יש): "{user_thesis}"
חפש באינטרנט: דוח רבעוני אחרון, תאריך הדוח הבא, יעדי אנליסטים, חדשות מ-30 הימים האחרונים, סיכונים ספציפיים.
החזר JSON בלבד (בלי טקסט לפני/אחרי, בלי ```), בעברית, במבנה הזה בדיוק:
{{"summary":"מה החברה עושה ולמה השוק מתמחר אותה כך — 2 משפטים",
 "thesis":"תזת השקעה: למה לצפות שתעלה ומה חייב לקרות — עד 3 משפטים",
 "scenarios":{{"bull":{{"price":0,"trigger":""}},"base":{{"price":0,"trigger":""}},"bear":{{"price":0,"trigger":""}}}},
 "sell_target":0,"stop":0,
 "exit_strategy":"מכירה בשלבים/בבת אחת, מה עושים אם מגיעים ליעד מוקדם — עד 2 משפטים",
 "next_earnings":"YYYY-MM-DD או 'לא ידוע'","events":["אירוע קרוב 1","אירוע קרוב 2"],
 "risk_note":"מה 'הקול השפוי' אומר על גודל הפוזיציה והקורלציה לשאר התיק — עד 2 משפטים",
 "user_thesis_check":"האם התזה של המשקיע מחזיקה מים לאור מה שמצאת — משפט אחד. אם אין תזה: ''",
 "verdict":"hold|trim|add|review"}}
המחירים בדולרים, מספרים בלבד. זה כלי עזר לחשיבה, לא ייעוץ השקעות."""

def _weights(p):
    fx = p["meta"]["usd_ils"]; vals = {h["ticker"]: h["qty"] * h["last_price"] * fx for h in p["holdings"]}
    tot = sum(vals.values()) or 1
    themes = {}
    for h in p["holdings"]: themes[h["theme"]] = themes.get(h["theme"], 0) + vals[h["ticker"]] / tot * 100
    return {t: v / tot * 100 for t, v in vals.items()}, {p["themes"].get(k, k): round(v) for k, v in themes.items()}

def generate_thesis(ticker):
    if not ANTHROPIC_KEY: raise RuntimeError("ANTHROPIC_API_KEY missing")
    import anthropic
    p = load(); ticker = ticker.upper()
    item = next((h for h in p["holdings"] if h["ticker"] == ticker), None) or next((w for w in p["watchlist"] if w["ticker"] == ticker), None)
    if item is None: raise KeyError(ticker)
    weights, themes = _weights(p)
    q = _quotes.get(ticker); price = q["c"] if q else item.get("last_price", 0)
    prompt = THESIS_PROMPT.format(ticker=ticker, name=item.get("name", ticker), price=price, qty=item.get("qty", 0),
                                  avg=item.get("avg_cost_usd", 0), weight=weights.get(ticker, 0), themes=json.dumps(themes, ensure_ascii=False),
                                  user_thesis=item.get("user_thesis", ""))
    msg = anthropic.Anthropic(api_key=ANTHROPIC_KEY).messages.create(
        model="claude-sonnet-4-6", max_tokens=2500,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}],
        messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    js = re.search(r"\{.*\}", text, re.S)
    if not js: raise ValueError("no JSON in model output: " + text[:200])
    th = json.loads(js.group(0)); th["generated"] = dt.date.today().isoformat(); th["price_at"] = price
    p = load()   # reload: user may have edited meanwhile
    for coll in ("holdings", "watchlist"):
        for it in p[coll]:
            if it["ticker"] == ticker: it["thesis"] = th
    store(p); return th

def thesis_all_bg():
    p = load(); done = []
    for h in p["holdings"]:
        try: generate_thesis(h["ticker"]); done.append(h["ticker"])
        except Exception as e: print("thesis error", h["ticker"], e)
    whatsapp("🧾 כרטיסי תזה נוצרו: " + ", ".join(done))

# ---------------- News agent: Pelosi + Trump (step 4) ----------------
NEWS_PROMPT = """היום {today}. אתה סוכן חדשות למשקיע פרטי. חפש באינטרנט שני דברים:
1. עסקאות מניות חדשות של ננסי פלוסי שדווחו (Periodic Transaction Report) ב-10 הימים האחרונים — טיקר, קנייה/מכירה, טווח סכום, תאריך העסקה ותאריך הדיווח. זכור: הדיווח מגיע בפיגור של עד 45 יום.
2. הצהרות/פוסטים/החלטות מדיניות של דונלד טראמפ מ-48 השעות האחרונות עם השפעה על מניות: מכסים, שבבים, קריפטו, אנרגיה גרעינית, AI, סין, ריבית.
3. לאופולד אשנברנר (Leopold Aschenbrenner) וקרן Situational Awareness LP — דיווחי 13F חדשים ב-SEC, שינויי פוזיציות שדווחו, ראיונות/מאמרים חדשים עם עמדות על מניות. זכור: 13F מתפרסם עד 45 יום אחרי סוף רבעון.
סנן: השאר רק פריטים שנוגעים למניות או לנושאים האלה — החזקות: {holdings}; רשימת מעקב: {watch}; נושאים: {themes}.
החזר JSON בלבד (בלי טקסט לפני/אחרי, בלי ```): רשימה של עד 10 פריטים:
[{{"source":"pelosi"|"trump"|"aschenbrenner","date":"YYYY-MM-DD","headline":"משפט אחד בעברית","tickers":["..."],"impact":"+"|"-"|"?","note":"למה זה רלוונטי לתיק — משפט","url":"..."}}]
אם אין כלום — החזר []. אל תמציא עסקאות: רק מה שמצאת במקור."""

def run_news():
    if not ANTHROPIC_KEY: return []
    import anthropic
    p = load(); _, themes = _weights(p)
    prompt = NEWS_PROMPT.format(today=dt.date.today().isoformat(), holdings=",".join(h["ticker"] for h in p["holdings"]),
                                watch=",".join(w["ticker"] for w in p["watchlist"]), themes=",".join(themes))
    msg = anthropic.Anthropic(api_key=ANTHROPIC_KEY).messages.create(
        model="claude-sonnet-4-6", max_tokens=2500,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}],
        messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    js = re.search(r"\[.*\]", text, re.S)
    items = json.loads(js.group(0)) if js else []
    seen = {i["text"] for i in load_signals()["items"]}
    new = []
    for it in items:
        line = f"[{it.get('source','?').upper()}] {it.get('headline','')} — {it.get('note','')}"
        if line in seen: continue
        add_signal(it.get("source", "news"), line, it.get("tickers", []), it.get("url", ""), it.get("impact", "?")); new.append(it)
    if new:
        whatsapp("📰 פלוסי/טראמפ — חדש:\n" + "\n".join(
            f"{ {'pelosi':'🏛','trump':'🇺🇸','aschenbrenner':'🧠'}.get(i.get('source'),'📰') } {i.get('headline','')} [{', '.join(i.get('tickers',[]))}] {i.get('impact','')}" for i in new))
    return new

# ---------------- Risk agent: "the sane voice" (step 5) ----------------
RISK_PROMPT = """אתה 'הקול השפוי' — מנהל סיכונים של תיק מניות פרטי. היום {today}. אתה לא יועץ השקעות; אתה מציג שאלות, לא הוראות.
התיק (טיקר | נושא | משקל % | רווח/הפסד ₪ | שינוי יומי % | תזה של המשקיע | verdict מכרטיס התזה | יעד/stop):
{rows}
חשיפה לפי נושא: {themes}
סיגנלים מ-7 הימים האחרונים (פלוסי/טראמפ/התראות שער): {signals}
כתוב בעברית סקירה שבועית קצרה וישירה, עד 250 מילים, במבנה:
1. ריכוז וקורלציה — איפה התיק חשוף לגורם אחד, ומה קורה לו בתרחיש רע.
2. סטייה מהתזה — אילו פוזיציות התרחקו ממה שהמשקיע ציפה או חצו stop/יעד.
3. הצעות שהייתי פוסל — מהסיגנלים של השבוע, מה נראה FOMO או "כבר במחיר".
4. שלוש שאלות שהמשקיע צריך לענות עליהן השבוע.
בלי הקדמות, בלי סיכום, בלי דיסקליימר."""

def risk_review():
    if not ANTHROPIC_KEY: raise RuntimeError("ANTHROPIC_API_KEY missing")
    import anthropic
    p = load(); weights, themes = _weights(p); fx = p["meta"]["usd_ils"]
    rows = []
    for h in sorted(p["holdings"], key=lambda h: -weights.get(h["ticker"], 0)):
        q = _quotes.get(h["ticker"], {"c": h["last_price"], "dp": 0}); th = h.get("thesis") or {}
        pl = h["qty"] * q["c"] * fx - h["qty"] * h["avg_cost_ils"]
        rows.append(f"{h['ticker']} | {p['themes'].get(h['theme'], h['theme'])} | {weights.get(h['ticker'],0):.1f}% | {pl:+,.0f} | {q.get('dp',0):+.1f}% | "
                    f"{h.get('user_thesis','') or '-'} | {th.get('verdict','-')} | {th.get('sell_target','-')}/{th.get('stop','-')}")
    week_ago = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).isoformat()
    sigs = [s["text"] for s in load_signals()["items"] if s["ts"] >= week_ago and s["kind"] != "risk"][:25]
    prompt = RISK_PROMPT.format(today=dt.date.today().isoformat(), rows="\n".join(rows), themes=json.dumps(themes, ensure_ascii=False),
                                signals="\n".join(sigs) or "אין")
    msg = anthropic.Anthropic(api_key=ANTHROPIC_KEY).messages.create(model="claude-sonnet-4-6", max_tokens=1500,
                                                                     messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    p = load(); p["meta"]["risk_review"] = {"date": dt.date.today().isoformat(), "text": text}; store(p)
    add_signal("risk", "🧭 סקירת סיכונים שבועית נוצרה — ראה פאנל הקול השפוי")
    whatsapp("🧭 הקול השפוי — סקירה שבועית\n\n" + text[:3500])
    return p["meta"]["risk_review"]

# ---------------- Chat agent: consult on the portfolio (dashboard + Telegram) ----------------
def context_blob():
    p = load(); weights, themes = _weights(p); fx = p["meta"]["usd_ils"]
    L = [f"תאריך: {dt.date.today()} | USD/ILS {fx} | מזומן נטו ${p['cash']['usd'] + p['cash']['usd_liability']:,.0f}", "## החזקות (טיקר | נושא | כמות | ממוצע $ | אחרון $ | יומי % | רווח/הפסד ₪ | משקל | התזה שלי | verdict | יעד/stop)"]
    for h in sorted(p["holdings"], key=lambda h: -weights.get(h["ticker"], 0)):
        q = _quotes.get(h["ticker"], {"c": h["last_price"], "dp": 0}); th = h.get("thesis") or {}
        pl = h["qty"] * q["c"] * fx - h["qty"] * h["avg_cost_ils"]
        L.append(f"{h['ticker']} | {p['themes'].get(h['theme'], h['theme'])} | {h['qty']} | {h['avg_cost_usd']} | {q['c']:.2f} | {q.get('dp',0):+.1f}% | {pl:+,.0f} | {weights.get(h['ticker'],0):.1f}% | "
                 f"{h.get('user_thesis','') or '-'} | {th.get('verdict','-')} | {th.get('sell_target','-')}/{th.get('stop','-')} | תזה: {th.get('thesis','-')[:200]}")
    L.append("## חשיפה: " + json.dumps(themes, ensure_ascii=False))
    L.append("## רשימת מעקב (טיקר אחרון יומי% | מכירה מעל / קנייה מתחת)")
    L.append("; ".join(f"{w['ticker']} {_quotes.get(w['ticker'],{}).get('c','?')} {_quotes.get(w['ticker'],{}).get('dp',0):+.1f}% | {w.get('sell_above') or '-'}/{w.get('buy_below') or '-'}" for w in p["watchlist"]))
    sigs = load_signals()["items"][:15]
    L.append("## סיגנלים אחרונים:\n" + "\n".join(f"{s['ts'][:16]} [{s['kind']}] {s['text']}" for s in sigs))
    rr = p["meta"].get("risk_review")
    if rr: L.append(f"## סקירת הקול השפוי ({rr['date']}):\n{rr['text']}")
    return "\n".join(L)

CHAT_SYSTEM = """אתה היועץ הפנימי של תיק המניות של טל. ענה בעברית, קצר וישיר, כמו שותף להתייעצות — לא כמו דיסקליימר. אתה יודע את כל מצב התיק (למטה). אם צריך נתון חיצוני (חדשות, דוח, מחיר יעד של אנליסטים) — חפש. כשטל שואל "מה לעשות" — תן דעה מנומקת, מספרים, ומה הסיכון, ותזכיר לו שההחלטה שלו. אל תמציא נתונים שאין לך.

""" + "{ctx}"

def chat_reply(messages):
    if not ANTHROPIC_KEY: return "ANTHROPIC_API_KEY חסר בשרת"
    import anthropic
    msg = anthropic.Anthropic(api_key=ANTHROPIC_KEY).messages.create(
        model="claude-sonnet-4-6", max_tokens=1200, system=CHAT_SYSTEM.replace("{ctx}", context_blob()),
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
        messages=[{"role": x["role"], "content": x["content"]} for x in messages[-12:]])
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip() or "(אין תשובה)"

def telegram_loop():
    """Long-polls Telegram so Tal can chat with the portfolio bot from his phone. Commands: /summary /news /risk /open /thesis TICKER"""
    offset = None; hist = []
    while True:
        try:
            r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates", params={"timeout": 30, "offset": offset}, timeout=40).json()
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message") or {}; text = (msg.get("text") or "").strip()
                if str(msg.get("chat", {}).get("id")) != str(TG_CHAT) or not text: continue
                cmd = text.split()[0].lower()
                if cmd == "/summary": daily_summary(); continue
                if cmd == "/news": whatsapp("סורק…"); run_news() or whatsapp("אין פריטים חדשים"); continue
                if cmd == "/risk": risk_review(); continue
                if cmd == "/open": opening_brief(); continue
                if cmd == "/thesis" and len(text.split()) > 1:
                    th = generate_thesis(text.split()[1]); whatsapp(f"🧾 {text.split()[1].upper()}: {th.get('thesis','')}\nיעד ${th.get('sell_target')} | stop ${th.get('stop')} | {th.get('verdict')}\n{th.get('risk_note','')}"); continue
                hist.append({"role": "user", "content": text})
                reply = chat_reply(hist); hist.append({"role": "assistant", "content": reply}); hist = hist[-12:]
                whatsapp(reply)
        except Exception as e:
            print("telegram loop error", e); time.sleep(5)

if TG_TOKEN and TG_CHAT:
    threading.Thread(target=telegram_loop, daemon=True).start()

def daily_summary():
    if dt.datetime.now(NY).weekday() >= 5: return
    refresh_quotes(); p = load(); rows = portfolio_summary(p)
    total = sum(r["value"] for r in rows); day = sum(r["day"] for r in rows); pl = sum(r["pl"] for r in rows)
    movers = sorted(rows, key=lambda r: r["dp"])
    txt = (f"📊 סיכום יומי {dt.date.today():%d.%m}\n"
           f"שווי: ₪{total:,.0f} | היום: ₪{day:+,.0f} | מצטבר: ₪{pl:+,.0f}\n"
           f"⬆ {movers[-1]['t']} {movers[-1]['dp']:+.1f}%  ⬇ {movers[0]['t']} {movers[0]['dp']:+.1f}%")
    cands = watchlist_candidates(p)
    if cands:
        txt += "\n\n👀 מרשימת המעקב:\n" + "\n".join(f"{c['t']} ${c['c']:.2f} ({c['dp']:+.1f}%){' ✅ שער קנייה' if c['hit'] else ''}" for c in cands)
        take = claude_take(rows, cands)
        if take: txt += "\n\n🧠 " + take
    whatsapp(txt)

_jobs = {}   # job name -> {"last": iso, "ok": bool, "err": str}
def logged(fn):
    def run():
        try: fn(); _jobs[fn.__name__] = {"last": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "ok": True, "err": ""}
        except Exception as e: _jobs[fn.__name__] = {"last": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "ok": False, "err": str(e)}; print("job error", fn.__name__, e)
    run.__name__ = fn.__name__; return run

def opening_brief():
    """09:35 NY: first prices of the day + what the news agent found this morning."""
    refresh_quotes(); p = load(); rows = portfolio_summary(p)
    movers = sorted(rows, key=lambda r: r["dp"]); day = sum(r["day"] for r in rows)
    today = dt.date.today().isoformat()
    news = [s for s in load_signals()["items"] if s["ts"][:10] == today and s["kind"] in ("pelosi", "trump", "aschenbrenner")]
    txt = (f"🔔 פתיחה {dt.date.today():%d.%m} — התיק בפתיחה: ₪{day:+,.0f}\n"
           f"⬆ {movers[-1]['t']} {movers[-1]['dp']:+.1f}% | {movers[-2]['t']} {movers[-2]['dp']:+.1f}%\n"
           f"⬇ {movers[0]['t']} {movers[0]['dp']:+.1f}% | {movers[1]['t']} {movers[1]['dp']:+.1f}%\n"
           f"📰 סיגנלים הבוקר: {len(news)}" + ("\n" + "\n".join("• " + n["text"][:120] for n in news[:4]) if news else ""))
    whatsapp(txt)

sched = BackgroundScheduler(timezone=NY)
sched.add_job(logged(opening_brief), "cron", id="opening_brief", name="opening_brief", day_of_week="mon-fri", hour=9, minute=36)
sched.add_job(logged(check_rules), "cron", id="check_rules", name="check_rules", day_of_week="mon-fri", hour="9-16", minute="*/5")
sched.add_job(logged(daily_summary), "cron", id="daily_summary", name="daily_summary", day_of_week="mon-fri", hour=16, minute=15)
sched.add_job(logged(run_news), "cron", id="run_news", name="run_news", hour="1,9", minute=0)          # 08:00 + 16:00 Israel time
sched.add_job(logged(risk_review), "cron", id="risk_review", name="risk_review", day_of_week="fri", hour=16, minute=40)   # weekly, after Friday close
sched.start()

@app.get("/portfolio")
def get_portfolio(): return load()

@app.put("/portfolio")
def put_portfolio(p: dict):
    if "holdings" not in p or "watchlist" not in p: raise HTTPException(400, "bad shape")
    store(p); return {"ok": True}

@app.get("/markets")
def get_markets(): return _markets

@app.get("/quotes")
def get_quotes(): return _quotes

@app.post("/refresh")
def refresh(): refresh_quotes(); return {"ok": True, "n": len(_quotes)}

@app.post("/test-whatsapp")
def test_wa(): whatsapp("✅ Portfolio HUD מחובר"); return {"ok": True, "channels": {"whatsapp": bool(CMB_KEY), "telegram": bool(TG_TOKEN)}}

@app.post("/run-daily")
def run_daily(): daily_summary(); return {"ok": True}

@app.post("/thesis/{ticker}")
def thesis(ticker: str):
    try: return generate_thesis(ticker)
    except KeyError: raise HTTPException(404, "ticker not in portfolio/watchlist")
    except Exception as e: raise HTTPException(500, str(e))

@app.post("/thesis-all")
def thesis_all():
    threading.Thread(target=thesis_all_bg, daemon=True).start()
    return {"ok": True, "note": "running in background, ~1-2 min per holding; notification when done"}

@app.get("/signals")
def get_signals(): return load_signals()

@app.post("/run-news")
def run_news_now():
    try: return {"ok": True, "new": run_news()}
    except Exception as e: raise HTTPException(500, str(e))

@app.post("/risk-review")
def risk_review_now():
    try: return risk_review()
    except Exception as e: raise HTTPException(500, str(e))

@app.post("/chat")
def chat(body: dict):
    try: return {"reply": chat_reply(body.get("messages", []))}
    except Exception as e: raise HTTPException(500, str(e))

@app.get("/health")
def health():
    return {"status": "up", "quotes": len(_quotes), "market_open": market_open(), "now_ny": dt.datetime.now(NY).isoformat(timespec="minutes"),
            "channels": {"whatsapp": bool(CMB_KEY), "telegram": bool(TG_TOKEN and TG_CHAT)},
            "jobs": _jobs, "next_runs": {j.name: str(j.next_run_time) for j in sched.get_jobs()}}

@app.post("/run-opening")
def run_opening(): opening_brief(); return {"ok": True}

INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")

@app.get("/")
def root():
    if os.path.exists(INDEX): return FileResponse(INDEX)
    return {"status": "up", "hint": "index.html missing next to main.py"}
