# Svitlo Bot v1.3 | DTEK Odesa outage notifier for Telegram | runs on GitHub Actions
import hashlib
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta, time as dtime
from html import escape
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Kyiv")
DTEK_URL = "https://www.dtek-oem.com.ua/ua/shutdowns"
STATE_FILE = "state.json"
WARN_MINUTES = [30, 15]
WAIT_MAX_SEC = 9 * 60
FAIL_ALERT_AFTER = 3
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

TOKEN = os.environ.get("TG_TOKEN", "").strip()
ADDRESS = os.environ.get("ADDRESS", "").strip()
GROUP_OVERRIDE = os.environ.get("GROUP", "").strip()
CHAT_ENV = os.environ.get("TG_CHAT_ID", "").strip()

SLOT_MAP = {
    "yes": ("on", "on"), "no": ("off", "off"),
    "first": ("off", "on"), "second": ("on", "off"),
    "mfirst": ("maybe", "on"), "msecond": ("on", "maybe"),
    "maybe": ("maybe", "maybe"),
}
SLOT_CHR = {"on": "1", "off": "0", "maybe": "?"}
CHR_SLOT = {v: k for k, v in SLOT_CHR.items()}
BANNER_WORDS = ("екстрен", "аварійн", "обмежен", "графік", "відключ")


def now():
    return datetime.now(TZ)


def log(msg):
    print(f"[{now():%H:%M:%S}] {msg}", flush=True)


# ---------------- state ----------------

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


# ---------------- telegram ----------------

def _post(method, data, ctype):
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/{method}",
                                 data=data, headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"ok": False, "description": str(e)}
    except Exception as e:
        return {"ok": False, "description": str(e)}


def tg(method, **params):
    return _post(method, json.dumps(params).encode(), "application/json")


def tg_files(method, fields, files):
    bnd = "----svitlo" + os.urandom(8).hex()
    body = b""
    for k, v in fields.items():
        body += f'--{bnd}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n'.encode() + str(v).encode() + b"\r\n"
    for k, (fn, data) in files.items():
        body += (f'--{bnd}\r\nContent-Disposition: form-data; name="{k}"; filename="{fn}"\r\n'
                 f'Content-Type: image/png\r\n\r\n').encode() + data + b"\r\n"
    body += f"--{bnd}--\r\n".encode()
    return _post(method, body, f"multipart/form-data; boundary={bnd}")


def send_photo(chat, png, caption, silent=False):
    r = tg_files("sendPhoto", {"chat_id": chat, "caption": caption, "parse_mode": "HTML",
                               "disable_notification": "true" if silent else "false"},
                 {"photo": ("svitlo.png", png)})
    if not r.get("ok"):
        log(f"sendPhoto failed: {r.get('description')}")
        return None
    return r["result"]["message_id"]


def send(chat, text, silent=False):
    r = tg("sendMessage", chat_id=chat, text=text, parse_mode="HTML",
           disable_web_page_preview=True, disable_notification=silent)
    if not r.get("ok"):
        log(f"sendMessage failed: {r.get('description')}")
        return None
    return r["result"]["message_id"]


def poll_updates(state):
    r = tg("getUpdates", offset=state.get("upd_offset", 0), timeout=0,
           allowed_updates=["message"])
    msgs = []
    if r.get("ok"):
        for u in r["result"]:
            state["upd_offset"] = u["update_id"] + 1
            m = u.get("message")
            if m and m.get("chat", {}).get("type") == "private":
                msgs.append(m)
    return msgs


def ensure_pinned(chat, pid):
    r = tg("getChat", chat_id=chat)
    pm = (r.get("result") or {}).get("pinned_message") or {}
    if r.get("ok") and pm.get("message_id") != pid:
        tg("pinChatMessage", chat_id=chat, message_id=pid, disable_notification=True)


def update_pinned(state, chat, c):
    pid = state.get("pin_id")
    png = safe_render(c)
    if png is None:
        text = status_text(c)
        if pid and state.get("pin_kind") != "photo":
            r = tg("editMessageText", chat_id=chat, message_id=pid, text=text,
                   parse_mode="HTML", disable_web_page_preview=True)
            if r.get("ok") or "not modified" in str(r.get("description", "")):
                return
        mid = send(chat, text, silent=True)
        kind = "text"
    else:
        cap = ""
        if pid and state.get("pin_kind") == "photo":
            media = {"type": "photo", "media": "attach://img", "caption": cap, "parse_mode": "HTML"}
            r = tg_files("editMessageMedia", {"chat_id": chat, "message_id": pid,
                                              "media": json.dumps(media)}, {"img": ("svitlo.png", png)})
            if r.get("ok") or "not modified" in str(r.get("description", "")):
                ensure_pinned(chat, pid)
                return
            log(f"editMessageMedia failed: {r.get('description')}")
        mid = send_photo(chat, png, cap, silent=True)
        kind = "photo"
    if mid:
        tg("pinChatMessage", chat_id=chat, message_id=mid, disable_notification=True)
        if pid:
            tg("unpinChatMessage", chat_id=chat, message_id=pid)
            tg("deleteMessage", chat_id=chat, message_id=pid)
        state["pin_id"], state["pin_kind"] = mid, kind


def send_status(chat, c, silent=False, title=None):
    png = safe_render(c)
    cap = title or ""
    if png is None or not send_photo(chat, png, cap, silent):
        send(chat, (title + "\n\n" if title else "") + status_text(c), silent)


# ---------------- address matching ----------------

PREFIXES = {"м", "місто", "с", "село", "смт", "селище", "вул", "вулиця", "просп",
            "проспект", "пров", "провулок", "бул", "бульв", "бульвар", "пл",
            "площа", "узв", "узвіз", "туп", "тупик", "ул", "улица", "г", "город",
            "пер", "переулок"}


def norm(s):
    s = str(s).lower().replace("’", "'").replace("ʼ", "'").replace("`", "'").replace("ё", "е")
    s = re.sub(r"[^\w'/]+", " ", s)
    return " ".join(s.split())


def core(s):
    return [t for t in norm(s).split() if t not in PREFIXES]


def pick_name(query, candidates, prefer_prefix=None):
    q = core(query)
    if not q:
        return None, []
    exact = [c for c in candidates if core(c) == q]
    pool = exact
    if not pool:
        stems = [t[:5] for t in q]
        pool = [c for c in candidates
                if all(any(w.startswith(st) for w in core(c)) for st in stems)]
    if prefer_prefix:
        pref = [c for c in pool if norm(c).startswith(prefer_prefix)]
        if pref:
            pool = pref
    if len(pool) == 1:
        return pool[0], pool
    if pool:
        pool.sort(key=lambda c: len(norm(c)))
        return pool[0], pool
    return None, []


def house_norm(s):
    return re.sub(r"\s+", "", str(s).lower()).replace("\\", "/")


def pick_house(query, keys):
    q = house_norm(query)
    keys = list(keys)
    for k in keys:
        if house_norm(k) == q:
            return k
    for k in keys:
        hk = house_norm(k)
        if hk.startswith(q) and (len(hk) == len(q) or not hk[len(q)].isdigit()):
            return k
    return None


class AddrError(Exception):
    pass


# ---------------- DTEK scraping ----------------

JS_POST = """async ([ajax, csrf, fields]) => {
  const fd = new URLSearchParams();
  for (const [k, v] of fields) fd.append(k, v);
  const r = await fetch(ajax, {method: 'POST', credentials: 'same-origin',
    headers: {'x-requested-with': 'XMLHttpRequest', 'x-csrf-token': csrf,
              'content-type': 'application/x-www-form-urlencoded; charset=UTF-8'},
    body: fd.toString()});
  return [r.status, await r.text()];
}"""

JS_DS = """() => { try { if (typeof DisconSchedule === 'undefined') return null;
  return {streets: DisconSchedule.streets || null, fact: DisconSchedule.fact || null}; }
  catch (e) { return null; } }"""

JS_BANNER = """() => { const t = [];
  document.querySelectorAll('.m-attention__text, .modal-body').forEach(e => {
    const s = (e.innerText || '').trim(); if (s) t.push(s); });
  return t.join('\\n'); }"""


def parse_json(txt, what):
    try:
        return json.loads(txt)
    except Exception:
        raise RuntimeError(f"{what}: not JSON (site protection?) {txt[:120]!r}")


def fetch_dtek(city_q, street_q, house_q):
    from playwright.sync_api import sync_playwright
    last = None
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        try:
            for attempt in range(3):
                ctx = browser.new_context(user_agent=UA, locale="uk-UA",
                                          timezone_id="Europe/Kyiv",
                                          viewport={"width": 1366, "height": 900})
                try:
                    page = ctx.new_page()
                    page.goto(DTEK_URL, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_selector('meta[name="csrf-token"]', state="attached", timeout=30000)
                    page.wait_for_timeout(2500)
                    csrf = page.get_attribute('meta[name="csrf-token"]', "content")
                    ajax = page.get_attribute('meta[name="ajaxUrl"]', "content") or "/ua/ajax"
                    ds = page.evaluate(JS_DS) or {}
                    banner = (page.evaluate(JS_BANNER) or "").strip()

                    streets = ds.get("streets")
                    if not streets:
                        st, txt = page.evaluate(JS_POST, [ajax, csrf, [["method", "getStreets"]]])
                        streets = (parse_json(txt, "getStreets") or {}).get("streets")
                    if not isinstance(streets, dict) or not streets:
                        raise RuntimeError("streets list not found on page")

                    city, cities = pick_name(city_q, list(streets.keys()), prefer_prefix="м ")
                    if not city:
                        sim = [c for c in streets if core(city_q) and core(city_q)[0][:4] in norm(c)][:15]
                        raise AddrError(f"Город «{city_q}» не найден. Похожие: {', '.join(sim) or 'нет'}")
                    street, cand = pick_name(street_q, list(streets[city]))
                    if not street:
                        st0 = core(street_q)[0][:5] if core(street_q) else ""
                        sim = [s for s in streets[city] if st0 and st0 in norm(s)][:15]
                        raise AddrError(f"Улица «{street_q}» не найдена в «{city}». Похожие: {', '.join(sim) or 'нет'}")

                    fields = [["method", "getHomeNum"],
                              ["data[0][name]", "city"], ["data[0][value]", city],
                              ["data[1][name]", "street"], ["data[1][value]", street],
                              ["data[2][name]", "updateFact"],
                              ["data[2][value]", now().strftime("%d.%m.%Y %H:%M")]]
                    st, txt = page.evaluate(JS_POST, [ajax, csrf, fields])
                    if st != 200:
                        raise RuntimeError(f"getHomeNum HTTP {st}")
                    resp = parse_json(txt, "getHomeNum")
                    houses = resp.get("data") if isinstance(resp.get("data"), dict) else {}
                    hk = pick_house(house_q, houses.keys())
                    if hk is None:
                        sim = sorted(houses.keys(), key=lambda k: (house_norm(k)[:2] != house_norm(house_q)[:2], k))[:30]
                        raise AddrError(f"Дом «{house_q}» не найден на «{street}». Есть: {', '.join(sim) or 'пусто'}")
                    fact = resp.get("fact") if isinstance(resp.get("fact"), dict) else None
                    if not fact or not fact.get("data"):
                        fact = ds.get("fact")
                    return {"city": city, "street": street, "house_key": hk,
                            "house": houses[hk] or {}, "fact": fact or {}, "banner": banner}
                except AddrError:
                    raise
                except Exception as e:
                    last = e
                    log(f"attempt {attempt + 1} failed: {type(e).__name__}: {str(e)[:200]}")
                    time.sleep(5 + attempt * 5)
                finally:
                    ctx.close()
        finally:
            browser.close()
    raise RuntimeError(f"{type(last).__name__}: {str(last)[:200]}")


# ---------------- schedule logic ----------------

def norm_group(g):
    g = str(g).upper().replace("ГПВ", "").replace("GPV", "").replace(",", ".")
    g = re.sub(r"[^0-9.]", "", g)
    return g


def day_start(d):
    return datetime.combine(d, dtime(0), tzinfo=TZ)


def build_days(fact, group):
    days = {}
    data = (fact or {}).get("data") or {}
    for ts, groups in data.items():
        try:
            d = datetime.fromtimestamp(int(ts), TZ).date()
        except Exception:
            continue
        if not isinstance(groups, dict):
            continue
        hours = groups.get("GPV" + group)
        if not isinstance(hours, dict):
            continue
        slots = []
        for h in range(1, 25):
            slots += list(SLOT_MAP.get(str(hours.get(str(h), "yes")), ("on", "on")))
        days[d] = slots
    return days


def intervals(days, kind="off"):
    res, cur = [], None
    for d in sorted(days):
        base = day_start(d)
        for i, s in enumerate(days[d]):
            t = base + timedelta(minutes=30 * i)
            if s == kind:
                if cur and cur[1] == t:
                    cur[1] = t + timedelta(minutes=30)
                else:
                    if cur:
                        res.append(tuple(cur))
                    cur = [t, t + timedelta(minutes=30)]
    if cur:
        res.append(tuple(cur))
    return res


def parse_dtek_dt(s):
    try:
        return datetime.strptime(str(s).strip(), "%H:%M %d.%m.%Y").replace(tzinfo=TZ)
    except Exception:
        return None


def house_event(h):
    s, e = parse_dtek_dt(h.get("start_date")), parse_dtek_dt(h.get("end_date"))
    typ = str(h.get("type", "")).strip()
    if not s or not e or typ not in ("1", "2") or e <= s:
        return None
    desc = str(h.get("sub_type") or "").strip()
    if not desc:
        desc = "Плановые ремонтные работы" if typ == "1" else "Аварийное отключение"
    return {"start": s, "end": e, "type": typ, "desc": desc}


MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]


def dm(d):
    return f"{d.day} {MONTHS[d.month - 1]}"


def hm(t):
    return t.strftime("%H:%M")


def fmt_end(s, e):
    if e.time() == dtime(0) and e.date() == s.date() + timedelta(days=1):
        return "24:00"
    if e.date() != s.date():
        return f"{hm(e)} ({dm(e)})"
    return hm(e)


def fmt_iv(s, e):
    return f"{hm(s)}–{fmt_end(s, e)}"


def day_word(d, ref):
    if d == ref:
        return "сегодня"
    if d == ref + timedelta(days=1):
        return "завтра"
    return dm(d)


def clip_day(ivs, d):
    a, b = day_start(d), day_start(d + timedelta(days=1))
    out = []
    for s, e in ivs:
        s2, e2 = max(s, a), min(e, b)
        if s2 < e2:
            out.append((s2, e2))
    return out


def is_schedule_event(ev, offs):
    if "стабіліз" in ev["desc"].lower() or "графік" in ev["desc"].lower():
        return True
    tol = timedelta(minutes=31)
    return any(s - tol <= ev["start"] and ev["end"] <= e + tol for s, e in offs)


class Ctx:
    pass


def make_ctx(data, state=None):
    state = {} if state is None else state
    c = Ctx()
    c.now = now()
    c.today = c.now.date()
    h = data["house"]
    c.site_groups = [norm_group(g) for g in (h.get("sub_type_reason") or []) if norm_group(g)]
    c.group = norm_group(GROUP_OVERRIDE) if GROUP_OVERRIDE else (c.site_groups[0] if c.site_groups else "")
    c.fact = data["fact"] or {}
    c.days = build_days(c.fact, c.group) if c.group else {}
    c.days = {d: v for d, v in c.days.items() if d >= c.today - timedelta(days=1)}
    c.offs = intervals(c.days, "off")
    c.maybes = intervals(c.days, "maybe")
    raw = house_event(h)
    if raw and raw["end"] <= c.now:
        raw = None
    house_active = bool(raw and raw["start"] <= c.now)
    c.event = raw if raw and not is_schedule_event(raw, c.offs) else None
    c.early = None
    cur = next(((s, e) for s, e in c.offs if s <= c.now < e), None)
    if cur:
        key = cur[0].isoformat()
        if house_active:
            state["seen_off"] = key
        elif state.get("seen_off") == key and (c.now - cur[0]) > timedelta(minutes=5):
            c.early = cur
            c.offs = [(s, c.now) if (s, e) == cur else (s, e) for s, e in c.offs]
    c.banner = data["banner"]
    c.addr = f"{data['street']}, {data['house_key']}"
    return c


def check_early(c, state, chat):
    if not c.early:
        return
    key = c.early[0].isoformat()
    if state.get("early_sent") != key:
        send(chat, f"💡 <b>Свет дали раньше графика</b>\nПо графику было до {fmt_end(*c.early)}")
        state["early_sent"] = key


DOT = {"on": "🟢", "off": "🔴", "maybe": "🟡"}


def day_block(c, d):
    L = [f"<b>{day_word(d, c.today).capitalize()}, {dm(d)}</b>"]
    if d not in c.days:
        L.append("нет графика")
        return L
    slots = c.days[d]
    if all(x == "on" for x in slots):
        L.append("🟢 весь день без отключений")
        return L
    base = day_start(d)
    i = 0
    while i < 48:
        j = i
        while j < 48 and slots[j] == slots[i]:
            j += 1
        s, e = base + timedelta(minutes=30 * i), base + timedelta(minutes=30 * j)
        line = f"{DOT[slots[i]]} {hm(s)} – {fmt_end(s, e)}"
        if s <= c.now < e:
            line += "  ◀ сейчас"
        L.append(line)
        i = j
    return L


def status_text(c):
    L = []
    ev = c.event
    cur = next(((s, e) for s, e in c.offs if s <= c.now < e), None)
    nxt = next(((s, e) for s, e in c.offs if s > c.now), None)
    if ev and ev["start"] <= c.now:
        L.append(f"🚨 <b>Нет света до ~{fmt_end(c.now, ev['end'])}</b> (авария)")
    elif cur:
        L.append(f"⛔ <b>Нет света до {fmt_end(cur[0], cur[1])}</b>")
    elif nxt:
        when = "" if nxt[0].date() == c.today else day_word(nxt[0].date(), c.today) + " "
        L.append(f"✅ <b>Свет есть до {when}{hm(nxt[0])}</b>")
    else:
        L.append("✅ <b>Свет есть</b>")
    if ev and ev["start"] > c.now:
        L.append(f"🛠 Работы: {dm(ev['start'])} {fmt_iv(ev['start'], ev['end'])}")
    for d in (c.today, c.today + timedelta(days=1)):
        L.append("")
        L += day_block(c, d)
    L.append("")
    L.append(f"<i>🕒 {c.now:%H:%M} · гр. {escape(c.group) or '?'}</i>")
    return "\n".join(L)


FONT_FILES = [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/freefont/FreeSans.ttf", "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"),
]
_fonts = {}


def font(size, bold=False):
    from PIL import ImageFont
    k = (size, bold)
    if k not in _fonts:
        for reg, bld in FONT_FILES:
            p = bld if bold else reg
            if os.path.exists(p):
                _fonts[k] = ImageFont.truetype(p, size)
                break
        else:
            _fonts[k] = ImageFont.load_default(size)
    return _fonts[k]


C_BG, C_GREEN, C_RED, C_YEL, C_ORANGE = (24, 26, 31), (46, 160, 90), (214, 64, 64), (215, 180, 60), (235, 125, 35)
C_GRAY, C_TXT, C_DIM = (70, 74, 82), (235, 235, 235), (150, 155, 165)


def headline(c):
    ev = c.event
    cur = next(((s, e) for s, e in c.offs if s <= c.now < e), None)
    nxt = next(((s, e) for s, e in c.offs if s > c.now), None)
    nxt_txt = ""
    if nxt:
        nxt_txt = ("" if nxt[0].date() == c.today else day_word(nxt[0].date(), c.today) + " ") + fmt_iv(*nxt)
    if ev and ev["start"] <= c.now:
        return f"Нет света до ~{fmt_end(c.now, ev['end'])}", "Аварийное отключение", C_ORANGE, "🚨"
    if cur:
        return f"Нет света до {fmt_end(cur[0], cur[1])}", (f"Дальше: {nxt_txt}" if nxt else ""), C_RED, "⛔"
    pre = "Дали раньше графика · " if c.early else ""
    if nxt:
        when = "" if nxt[0].date() == c.today else day_word(nxt[0].date(), c.today) + " "
        return f"Свет есть до {when}{hm(nxt[0])}", f"{pre}Отключение: {nxt_txt}", C_GREEN, "✅"
    return "Свет есть", (pre + "Отключений по графику нет" if c.days else pre.strip(" ·")), C_GREEN, "✅"


def caption_text(c):
    h, sub, _, icon = headline(c)
    L = [f"{icon} <b>{escape(h)}</b>"]
    if sub:
        L.append(escape(sub))
    if c.event and c.event["start"] > c.now:
        L.append(f"🛠 Работы: {dm(c.event['start'])} {fmt_iv(c.event['start'], c.event['end'])}")
    L.append(f"<i>🕒 {c.now:%H:%M} · гр. {escape(c.group) or '?'}</i>")
    return "\n".join(L)


def render_png(c):
    from PIL import Image, ImageDraw
    import io
    W, H = 1200, 600
    L, R = 110, W - 40
    im = Image.new("RGB", (W, H), C_BG)
    d = ImageDraw.Draw(im)
    h, sub, col, _ = headline(c)
    d.text((40, 26), h, font=font(52, True), fill=col)
    if sub:
        d.text((40, 96), sub, font=font(34), fill=C_TXT)

    def x(dt, day):
        return L + (R - L) * (dt - day_start(day)).total_seconds() / 86400

    for n, day in enumerate((c.today, c.today + timedelta(days=1))):
        y = 165 + n * 200
        d.text((40, y), f"{day_word(day, c.today).capitalize()}, {dm(day)}", font=font(30, True), fill=C_TXT)
        y0, y1 = y + 62, y + 122
        if day not in c.days:
            d.rounded_rectangle((L, y0, R, y1), 12, fill=C_GRAY)
            d.text(((L + R) / 2, (y0 + y1) / 2), "график ещё не опубликован", font=font(30), fill=C_TXT, anchor="mm")
            continue
        bar = Image.new("RGB", (R - L, y1 - y0), C_GREEN)
        bd = ImageDraw.Draw(bar)
        segs = [(s, e, C_RED) for s, e in clip_day(c.offs, day)]
        segs += [(s, e, C_YEL) for s, e in clip_day(c.maybes, day)]
        if c.event:
            segs += [(s, e, C_ORANGE) for s, e in clip_day([(c.event["start"], c.event["end"])], day)]
        for s, e, cl in segs:
            bd.rectangle((x(s, day) - L, 0, x(e, day) - L, y1 - y0), fill=cl)
        mask = Image.new("L", bar.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, bar.size[0] - 1, bar.size[1] - 1), 12, fill=255)
        im.paste(bar, (L, y0), mask)
        for hh in range(0, 25, 3):
            xx = L + (R - L) * hh / 24
            d.line((xx, y1, xx, y1 + 8), fill=C_DIM, width=2)
            d.text((xx, y1 + 12), str(hh), font=font(24), fill=C_DIM, anchor="ma")
        labs = [(s, e, cl) for s, e, cl in segs if cl != C_YEL]
        labs.sort(key=lambda t: t[0])
        f = font(24, True)
        boxes, ok = [], True
        for s, e, cl in labs:
            t = f"{hm(s)}–{fmt_end(s, e)}"
            w = d.textlength(t, font=f)
            cx = (x(s, day) + x(e, day)) / 2
            bx = (cx - w / 2, cx + w / 2)
            if bx[0] < 10 or bx[1] > W - 10 or (boxes and bx[0] < boxes[-1][1][1] + 12):
                ok = False
            boxes.append((t, bx, cx, cl))
        if ok:
            for t, bx, cx, cl in boxes:
                d.text((cx, y0 - 6), t, font=f, fill=cl, anchor="md")
        elif boxes:
            d.text((L, y0 - 6), "  ·  ".join(b[0] for b in boxes), font=font(22, True), fill=C_RED, anchor="ld")
        if day == c.today:
            xn = x(c.now, day)
            d.line((xn, y0 - 4, xn, y1 + 4), fill=(255, 255, 255), width=5)
            d.polygon([(xn - 10, y1 + 6), (xn + 10, y1 + 6), (xn, y1 - 6)], fill=(255, 255, 255))
    d.text((W - 40, H - 22), f"обновлено {c.now:%H:%M} · группа {c.group or '?'}", font=font(24), fill=C_DIM, anchor="rd")
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def safe_render(c):
    try:
        return render_png(c)
    except Exception as e:
        log(f"render failed: {type(e).__name__}: {e}")
        return None


def banner_relevant(b):
    t = (b or "").lower()
    return bool(t) and any(w in t for w in BANNER_WORDS)


# ---------------- notifications ----------------

def warn_targets(c):
    t = []
    for s, e in c.offs:
        if s > c.now:
            t.append(("off", s, s, e, None))
        if e > c.now:
            t.append(("on", e, s, e, None))
    ev = c.event
    if ev:
        if ev["start"] > c.now:
            t.append(("off", ev["start"], ev["start"], ev["end"], ev["desc"]))
        if ev["end"] > c.now:
            t.append(("on", ev["end"], ev["start"], ev["end"], ev["desc"]))
    return t


def allowed(kind, at, s, e, w):
    return kind == "off" or at - timedelta(minutes=w) > s


def do_warnings(c, state, chat):
    sent = set(state.get("sent", []))
    c.now = now()
    for kind, at, s, e, desc in warn_targets(c):
        lead = (at - c.now).total_seconds() / 60
        due = [w for w in WARN_MINUTES if lead <= w + 0.5 and allowed(kind, at, s, e, w)]
        if not due:
            continue
        w = min(due)
        key = f"{kind}|{at.isoformat()}"
        if f"{key}|{w}" in sent:
            continue
        for w2 in WARN_MINUTES:
            if w2 >= w:
                sent.add(f"{key}|{w2}")
        mins = w if abs(lead - w) <= 2 else max(1, round(lead))
        if kind == "off":
            txt = [f"⏰ <b>Через {mins} мин нет света</b>",
                   f"🕒 {fmt_iv(s, e)}" + (f" — {escape(desc)}" if desc else "")]
        else:
            approx = "~" if desc else ""
            txt = [f"🟢 <b>Через {approx}{mins} мин включат свет</b>",
                   f"🕒 в {approx}{fmt_end(s, e)}"]
        send(chat, "\n".join(txt))
    cutoff = (c.now - timedelta(days=2)).isoformat()
    state["sent"] = sorted(k for k in sent if k.split("|")[1] >= cutoff)


def next_due(c, state):
    sent = set(state.get("sent", []))
    best = None
    for kind, at, s, e, desc in warn_targets(c):
        for w in WARN_MINUTES:
            if f"{kind}|{at.isoformat()}|{w}" in sent or not allowed(kind, at, s, e, w):
                continue
            due = at - timedelta(minutes=w)
            if due > now() and (best is None or due < best):
                best = due
    return best


def check_schedule_change(c, state, chat):
    new = {d.isoformat(): "".join(SLOT_CHR[x] for x in v) for d, v in c.days.items() if d >= c.today}
    old = state.get("days")
    old_group = state.get("days_group")
    state["days"], state["days_group"] = new, c.group
    if old is None or old_group != c.group:
        return
    changed = [d for d in sorted(new) if old.get(d) != new[d]]
    if not changed:
        return
    only_new = all(d not in old for d in changed)
    title = "📅 <b>Новый график</b>" if only_new else "🔄 <b>График изменился</b>"
    names = ", ".join(day_word(datetime.fromisoformat(iso).date(), c.today) for iso in changed)
    send_status(chat, c, title=f"{title} ({names})")


def check_event(c, state, chat):
    ev = c.event
    old = state.get("event")
    if ev:
        key = f"{ev['start'].isoformat()}|{ev['type']}"
        end_iso = ev["end"].isoformat()
        if not old or old.get("key") != key:
            if ev["start"] <= c.now:
                head = "🚨 <b>Отключение света</b>" if ev["type"] == "2" else "🛠 <b>Отключение (ремонтные работы)</b>"
                send(chat, f"{head}\n💡 Включат ~<b>{fmt_end(c.now, ev['end'])}</b>\n<i>{escape(ev['desc'])}</i>")
            else:
                send(chat, f"🛠 <b>Плановые работы</b>\n🕒 {dm(ev['start'])} {fmt_iv(ev['start'], ev['end'])}")
        elif old.get("end") != end_iso:
            send(chat, f"🕒 <b>Включат теперь ~{fmt_end(c.now, ev['end'])}</b>")
        state["event"] = {"key": key, "end": end_iso, "started": ev["start"] <= c.now}
    else:
        if old and old.get("started"):
            end = datetime.fromisoformat(old["end"])
            if end > c.now + timedelta(minutes=10):
                send(chat, "✅ <b>ДТЕК снял отключение по адресу</b> — свет должен быть")
        state.pop("event", None)


def check_banner(c, state, chat):
    rel = banner_relevant(c.banner)
    h = hashlib.md5(c.banner.encode()).hexdigest() if rel else ""
    if rel and state.get("banner") != h:
        send(chat, "📢 <b>ДТЕК:</b>\n" + escape(c.banner[:400]))
    state["banner"] = h


def check_group(c, state, chat):
    if GROUP_OVERRIDE:
        key = f"{c.group}|{','.join(c.site_groups)}"
        if c.site_groups and c.group not in c.site_groups and state.get("grp_note") != key:
            send(chat, f"⚠️ Сайт ДТЕК показывает группу <b>{escape(', '.join(c.site_groups))}</b>, "
                       f"а у тебя задана <b>{escape(c.group)}</b>. Использую {escape(c.group)}.")
        state["grp_note"] = key
        return
    key = ",".join(c.site_groups) or "none"
    if state.get("grp_note") != key:
        if not c.site_groups:
            send(chat, "⚠️ Сайт не вернул группу для адреса. Задай её вручную (переменная GROUP).")
        elif len(c.site_groups) > 1:
            send(chat, f"ℹ️ Для дома на сайте несколько групп: {escape(key)}. Беру {escape(c.group)}. "
                       f"Сверь с ботом ДТЕК и задай GROUP.")
        elif state.get("grp_note"):
            send(chat, f"ℹ️ Сайт ДТЕК сменил группу дома: теперь <b>{escape(c.group)}</b>.")
    state["grp_note"] = key


# ---------------- main ----------------

def resolve_chat(state, msgs):
    if CHAT_ENV:
        return int(CHAT_ENV)
    if state.get("chat_id"):
        return state["chat_id"]
    if msgs:
        state["chat_id"] = msgs[0]["chat"]["id"]
        return state["chat_id"]
    return None


def main():
    if not TOKEN or not ADDRESS:
        log("TG_TOKEN or ADDRESS secret is missing")
        return
    parts = [x.strip() for x in re.split(r"[|;]", ADDRESS) if x.strip()]
    if len(parts) != 3:
        log("ADDRESS must look like: city|street|house")
        return
    state = load_state()
    msgs = poll_updates(state)
    chat = resolve_chat(state, msgs)
    if not chat:
        log("No chat yet: send /start to the bot")
        save_state(state)
        return
    msgs = [m for m in msgs if m["chat"]["id"] == chat]
    if not state.get("welcomed"):
        send(chat, "👋 Бот подключён. Буду предупреждать за 30 и 15 минут до отключения и до включения, "
                   "писать об изменениях графика и аварийных отключениях. "
                   "Актуальный статус — в закреплённом сообщении. Напиши мне что угодно — пришлю свежий статус.")
        state["welcomed"] = True

    month = now().strftime("%Y-%m")
    if state.get("keepalive") != month:
        state["keepalive"] = month

    try:
        data = fetch_dtek(*parts)
    except AddrError as e:
        msg = str(e)
        if state.get("addr_err") != msg or msgs:
            send(chat, "❗ <b>Проблема с адресом</b>\n" + escape(msg) +
                 "\n\nИсправь секрет ADDRESS (формат: город|улица|дом).")
        state["addr_err"] = msg
        save_state(state)
        return
    except Exception as e:
        state["fails"] = state.get("fails", 0) + 1
        err = f"{type(e).__name__}: {str(e)[:200]}"
        log(f"DTEK fetch failed ({state['fails']}): {err}")
        if state["fails"] == FAIL_ALERT_AFTER:
            send(chat, f"⚠️ Не могу получить данные с сайта ДТЕК уже ~{FAIL_ALERT_AFTER * 10} мин.\n<code>{escape(err)}</code>")
        for _ in msgs:
            send(chat, "⚠️ Сейчас сайт ДТЕК не отвечает, попробую на следующей проверке.")
            break
        save_state(state)
        return

    state.pop("addr_err", None)
    if state.get("fails", 0) >= FAIL_ALERT_AFTER:
        send(chat, "✅ Связь с сайтом ДТЕК восстановлена", silent=True)
    state["fails"] = 0

    c = make_ctx(data, state)
    log(f"ok: group={c.group or '?'} days={len(c.days)} offs={len(c.offs)} event={'yes' if c.event else 'no'}")
    check_group(c, state, chat)
    check_event(c, state, chat)
    check_early(c, state, chat)
    check_banner(c, state, chat)
    check_schedule_change(c, state, chat)
    do_warnings(c, state, chat)
    update_pinned(state, chat, c)
    if msgs:
        send_status(chat, c)
    save_state(state)

    deadline = time.time() + WAIT_MAX_SEC
    while True:
        due = next_due(c, state)
        if not due:
            break
        wait = (due - now()).total_seconds()
        if wait <= 0 or time.time() + wait > deadline:
            break
        log(f"waiting {int(wait)}s for exact warning")
        time.sleep(wait + 3)
        do_warnings(c, state, chat)
        c.now = now()
        update_pinned(state, chat, c)
        save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        tb = traceback.format_exc()
        print(tb, flush=True)
        try:
            st = load_state()
            sig = hashlib.md5(tb.splitlines()[-1].encode()).hexdigest()
            chat = int(CHAT_ENV) if CHAT_ENV else st.get("chat_id")
            if chat and st.get("crash") != sig:
                send(chat, "🐞 Ошибка в боте:\n<code>" + escape(tb[-700:]) + "</code>")
                st["crash"] = sig
                save_state(st)
        except Exception:
            pass
    sys.exit(0)
