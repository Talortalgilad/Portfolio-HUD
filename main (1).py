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

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_lock = threading.Lock()
_quotes = {}                    # ticker -> {c, d, dp, pc, t}
_alerted = {}                   # "TICKER:rule" -> date string (one alert per rule per day)

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

def refresh_quotes():
    p = load()
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
            whatsapp(f"⚡ {t} {label}: ${q['c']:.2f} (כלל: {level})  יומי {q['dp']:+.1f}%")
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

sched = BackgroundScheduler(timezone=NY)
sched.add_job(check_rules, "cron", day_of_week="mon-fri", hour="9-16", minute="*/5")
sched.add_job(daily_summary, "cron", day_of_week="mon-fri", hour=16, minute=15)
sched.start()

@app.get("/portfolio")
def get_portfolio(): return load()

@app.put("/portfolio")
def put_portfolio(p: dict):
    if "holdings" not in p or "watchlist" not in p: raise HTTPException(400, "bad shape")
    store(p); return {"ok": True}

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

@app.get("/health")
def health(): return {"status": "up", "quotes": len(_quotes), "market_open": market_open()}

INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")

@app.get("/")
def root():
    if os.path.exists(INDEX): return FileResponse(INDEX)
    return {"status": "up", "hint": "index.html missing next to main.py"}
