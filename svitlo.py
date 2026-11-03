# Svitlo Bot v1.0 | DTEK Odesa outage notifier for Telegram | runs on GitHub Actions
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
WARN_MINUTES = [60, 30]
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

def tg(method, **params):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"ok": False, "description": str(e)}
    except Exception as e:
        return {"ok": False, "description": str(e)}


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


def update_pinned(state, chat, text):
    pid = state.get("pin_id")
    if pid:
        r = tg("editMessageText", chat_id=chat, message_id=pid, text=text,
               parse_mode="HTML", disable_web_page_preview=True)
        if r.get("ok") or "not modified" in str(r.get("description", "")):
            return
    mid = send(chat, text, silent=True)
    if mid:
        tg("pinChatMessage", chat_id=chat, message_id=mid, disable_notification=True)
        state["pin_id"] = mid


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


def hm(t):
    return t.strftime("%H:%M")


def fmt_end(s, e):
    if e.time() == dtime(0) and e.date() == s.date() + timedelta(days=1):
        return "24:00"
    if e.date() != s.date():
        return f"{hm(e)} ({e:%d.%m})"
    return hm(e)


def fmt_iv(s, e):
    return f"{hm(s)}–{fmt_end(s, e)}"


def day_word(d, ref):
    if d == ref:
        return "сегодня"
    if d == ref + timedelta(days=1):
        return "завтра"
    return d.strftime("%d.%m")


def clip_day(ivs, d):
    a, b = day_start(d), day_start(d + timedelta(days=1))
    out = []
    for s, e in ivs:
        s2, e2 = max(s, a), min(e, b)
        if s2 < e2:
            out.append((s2, e2))
    return out


class Ctx:
    pass


def make_ctx(data):
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
    c.event = house_event(h)
    if c.event and c.event["end"] <= c.now:
        c.event = None
    c.banner = data["banner"]
    c.addr = f"{data['street']}, {data['house_key']}"
    return c


def day_line(c, d):
    label = day_word(d, c.today).capitalize()
    if d not in c.days:
        return f"{label}: нет графика"
    parts = [fmt_iv(s, e) for s, e in clip_day(c.offs, d)]
    parts += [f"возм. {fmt_iv(s, e)}" for s, e in clip_day(c.maybes, d)]
    return f"{label}: " + (" · ".join(parts) if parts else "без отключений ✅")


def status_text(c):
    L = []
    ev = c.event
    cur = next(((s, e) for s, e in c.offs if s <= c.now < e), None)
    if ev and ev["start"] <= c.now:
        L.append(f"🚨 <b>Нет света до ~{fmt_end(c.now, ev['end'])}</b> (авария)")
    elif cur:
        L.append(f"⛔ <b>Нет света до {fmt_end(cur[0], cur[1])}</b>")
    else:
        L.append("✅ <b>Свет есть</b>")
    nxt = next(((s, e) for s, e in c.offs if s > c.now), None)
    if nxt:
        L.append(f"⏭ Дальше: {'' if nxt[0].date() == c.today else day_word(nxt[0].date(), c.today) + ' '}{fmt_iv(*nxt)}")
    if ev and ev["start"] > c.now:
        L.append(f"🛠 Работы: {ev['start']:%d.%m} {fmt_iv(ev['start'], ev['end'])}")
    L.append("")
    L.append(day_line(c, c.today))
    L.append(day_line(c, c.today + timedelta(days=1)))
    L.append(f"<i>🕒 {c.now:%H:%M} · гр. {escape(c.group) or '?'}</i>")
    return "\n".join(L)


def banner_relevant(b):
    t = (b or "").lower()
    return bool(t) and any(w in t for w in BANNER_WORDS)


# ---------------- notifications ----------------

def warn_targets(c):
    t = [(s, e, None) for s, e in c.offs if s > c.now]
    if c.event and c.event["start"] > c.now:
        t.append((c.event["start"], c.event["end"], c.event["desc"]))
    return t


def do_warnings(c, state, chat):
    sent = set(state.get("sent", []))
    c.now = now()
    for s, e, desc in warn_targets(c):
        lead = (s - c.now).total_seconds() / 60
        due = [w for w in WARN_MINUTES if lead <= w + 0.5]
        if not due:
            continue
        w = min(due)
        key = s.isoformat()
        if f"{key}|{w}" in sent:
            continue
        for w2 in WARN_MINUTES:
            if w2 >= w:
                sent.add(f"{key}|{w2}")
        mins = max(1, round(lead))
        head = "⚠️" if w == max(WARN_MINUTES) else "⏰"
        txt = [f"{head} <b>Через {mins} мин нет света</b>",
               f"🕒 {fmt_iv(s, e)}" + (f" — {escape(desc)}" if desc else "")]
        send(chat, "\n".join(txt))
    cutoff = (c.now - timedelta(days=2)).isoformat()
    state["sent"] = sorted(k for k in sent if k.split("|")[0] >= cutoff)


def next_due(c, state):
    sent = set(state.get("sent", []))
    best = None
    for s, e, desc in warn_targets(c):
        for w in WARN_MINUTES:
            if f"{s.isoformat()}|{w}" in sent:
                continue
            due = s - timedelta(minutes=w)
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
    L = [title] + [day_line(c, datetime.fromisoformat(iso).date()) for iso in changed]
    send(chat, "\n".join(L))


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
                send(chat, f"🛠 <b>Плановые работы</b>\n🕒 {ev['start']:%d.%m} {fmt_iv(ev['start'], ev['end'])}")
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
        send(chat, "👋 Бот подключён. Буду предупреждать за 60 и 30 минут до отключения, "
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

    c = make_ctx(data)
    log(f"ok: group={c.group or '?'} days={len(c.days)} offs={len(c.offs)} event={'yes' if c.event else 'no'}")
    check_group(c, state, chat)
    check_event(c, state, chat)
    check_banner(c, state, chat)
    check_schedule_change(c, state, chat)
    do_warnings(c, state, chat)
    update_pinned(state, chat, status_text(c))
    if msgs:
        send(chat, status_text(c))
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
        update_pinned(state, chat, status_text(c))
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
