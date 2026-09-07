"""
Portfolio HUD backend — step 2
- serves/stores portfolio.json for the dashboard
- pulls live quotes from Finnhub
- every 5 min (US market hours): checks sell_above / buy_below rules -> WhatsApp
- after US close: daily P/L summary + watchlist opportunities (Claude) -> WhatsApp
"""
import os, json, time, threading, urllib.parse, datetime as dt
from zoneinfo import ZoneInfo
import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler

FINNHUB = os.environ["FINNHUB_KEY"]
CMB_PHONE = os.environ.get("CALLMEBOT_PHONE", "")        # +9725xxxxxxx
CMB_KEY = os.environ.get("CALLMEBOT_KEY", "")
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
    if not (CMB_PHONE and CMB_KEY):
        print("[whatsapp skipped]", text); return
    url = f"https://api.callmebot.com/whatsapp.php?phone={CMB_PHONE}&apikey={CMB_KEY}&text={urllib.parse.quote(text)}"
    try: requests.get(url, timeout=20)
    except Exception as e: print("whatsapp error", e)

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
    for w in p["watchlist"]:
        q = _quotes.get(w["ticker"]);
        if not q: continue
        for rule, cond, label in (("sell_above", q["c"] >= (w.get("sell_above") or 1e18), "הגיעה לשער מכירה"),
                                  ("buy_below", q["c"] <= (w.get("buy_below") or -1), "ירדה לשער קנייה")):
            key = f"{w['ticker']}:{rule}"
            if cond and _alerted.get(key) != today:
                _alerted[key] = today
                whatsapp(f"⚡ {w['ticker']} {label}: ${q['c']:.2f} (כלל: {w[rule]})  יומי {q['dp']:+.1f}%")

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
def test_wa(): whatsapp("✅ Portfolio HUD מחובר"); return {"ok": True}

@app.post("/run-daily")
def run_daily(): daily_summary(); return {"ok": True}

@app.get("/")
def root(): return {"status": "up", "quotes": len(_quotes), "market_open": market_open()}
