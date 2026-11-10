#!/usr/bin/env python3
"""
bahn_puffer.py - door-to-door planning Golm -> Potsdam Hbf -> S Botanischer Garten
-> Charite (and back), with transfer buffers, backup trains and UNCERTAINTY.

Uses the free VBB API (https://v6.vbb.transport.rest): no key, includes
construction timetables (e.g. the S1 20-min interval), real-time delays and
cancellations when you query today. Needs only Python 3.9+, no extra packages.

Door to door: Golm -(bike 30, then 5-10 min Potsdam Hbf transit: lights, parking, platform)-> Potsdam Hbf
-> train -> S Botanischer Garten -(bike 10)-> Charite. All times are options.

Uncertainty (similar idea to bahnvorhersage.de)
  Every run simulates the trip a few thousand times: each train gets a random
  delay or cancellation, your bike rides vary a bit, and if a connection breaks
  you take the best train you can still catch. From that you get
    Score    chance that all transfers of this connection work (you on time at start)
    50%/95%  you arrive at the door by this time in half / 95 % of all cases
  and with --ankunft: the latest time to leave home for 80 / 95 / 99 % certainty.

  Delay model: first rough built-in guesses per product (S-Bahn, RE, bus ...).
  Run "--lernen" regularly (e.g. via cron) and the script records real delays
  on your lines; once a line has >= 40 observations it uses those instead.

Examples
  python3 bahn_puffer.py                       # leave home 07-09, leave Charite 16-18
  python3 bahn_puffer.py --datum 2026-11-12    # a specific day
  python3 bahn_puffer.py --ankunft 08:30       # "I must be at the Charite by 08:30"
  python3 bahn_puffer.py --lernen              # record recent real delays (for cron)
  python3 bahn_puffer.py --statistik           # what has been learned so far
  python3 bahn_puffer.py --export-json data/delays.json   # for the web page

Columns
  Umst.     smallest transfer time in minutes (tight = risky)
  Plan B    if this connection's first train fails, door arrival with the next one;
            "= gleich" = a later feeder still catches the same S1
  *         safe: transfer >= --puffer and nothing cancelled
"""
import argparse
import bisect
import csv
import json
import math
import os
import random
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

API = "https://v6.vbb.transport.rest"
TZ = ZoneInfo("Europe/Berlin")
LOG = Path(os.environ.get("BAHN_PUFFER_LOG", Path.home() / ".bahn_puffer" / "verspaetungen.csv"))

# Rough starting guesses per product, replaced per line by learned data.
#   cancel: share of cancelled trains, tail: share of trains that are really late,
#   mean: average delay (min) of those late trains; the rest is -0.5 .. +1 min.
PRIORS = {
    "suburban": dict(cancel=0.010, tail=0.25, mean=2.5),   # S-Bahn
    "regional": dict(cancel=0.020, tail=0.40, mean=4.0),   # RE / RB
    "express":  dict(cancel=0.030, tail=0.50, mean=6.0),   # ICE / IC
    "subway":   dict(cancel=0.005, tail=0.15, mean=2.0),   # U-Bahn
    "tram":     dict(cancel=0.010, tail=0.30, mean=2.0),
    "bus":      dict(cancel=0.010, tail=0.50, mean=3.0),
}
MIN_LEARNED = 40


# ---------------------------------------------------------------- API access
def get(path, **params):
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "bahn-puffer/1.0"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.load(r)
        except Exception as e:  # API is a community mirror and sometimes hiccups
            if attempt == 3:
                sys.exit(f"API-Fehler bei {path}: {e}")
            time.sleep(2 * (attempt + 1))


def find_stop(name):
    res = get("/locations", query=name, poi="false", addresses="false", results=1)
    if not res:
        sys.exit(f"Haltestelle nicht gefunden: {name}")
    return res[0]["id"], res[0]["name"]


# ------------------------------------------------------------- journey logic
def parse(v):
    return datetime.fromisoformat(v).astimezone(TZ) if v else None


def ts(leg, kind):
    """Real-time time if known, otherwise planned time."""
    return parse(leg.get(kind) or leg.get("planned" + kind[0].upper() + kind[1:]))


def hhmm(d):
    return d.strftime("%H:%M") if d else "--:--"


def mins(m):
    return timedelta(minutes=m)


def short(name):
    for junk in (" (Berlin)", "S+U ", "S ", "U ", " Bhf"):
        name = name.replace(junk, "")
    return name.strip()


def summarize(j):
    legs = j["legs"]
    transit = [l for l in legs if not l.get("walking")]
    if not transit:
        return None
    dep, arr = ts(legs[0], "departure"), ts(legs[-1], "arrival")
    buffers = [
        (ts(b, "departure") - ts(a, "arrival")).total_seconds() / 60
        for a, b in zip(transit, transit[1:])
        if ts(a, "arrival") and ts(b, "departure")
    ]
    # legs for the simulation, with walking time before each ride
    sim, walk = [], 0.0
    for l in legs:
        if l.get("walking"):
            if ts(l, "departure") and ts(l, "arrival"):
                walk += (ts(l, "arrival") - ts(l, "departure")).total_seconds() / 60
            continue
        sim.append(dict(
            trip=l.get("tripId") or f"{l['line']['name']}{ts(l, 'departure')}",
            line=l["line"].get("name", "?"),
            product=l["line"].get("product", "suburban"),
            dep=ts(l, "departure"), arr=ts(l, "arrival"),
            walk_before=walk, cancelled=bool(l.get("cancelled")),
            realtime=l.get("departureDelay") is not None,
        ))
        walk = 0.0
    route = " > ".join(
        f"{l['line']['name']} {hhmm(ts(l, 'departure'))} {short(l['origin']['name'])}"
        for l in transit
    )
    delay = max(((l.get("departureDelay") or 0) for l in transit), default=0) / 60
    warnings = sorted({
        r.get("summary") or r.get("text", "")[:80]
        for l in transit for r in (l.get("remarks") or [])
        if r.get("type") == "warning"
    })
    return {
        "key": tuple(l.get("tripId") for l in transit),
        "dep": dep, "arr": arr,
        "minbuf": min(buffers) if buffers else None,
        "route": route,
        "cancelled": any(l.get("cancelled") for l in transit),
        "delay": delay, "warnings": warnings,
        "sim": sim, "walk_after": walk,
    }


def collect(frm, to, start, end, transfers):
    """All journeys departing between start and end (pages via laterRef).

    The API drops a journey when a later one reaches the same final train
    ("dominated"), so the safe early feeder and the tight late feeder never
    show up together. We therefore search once per transfer time and merge.
    """
    found = {}
    for transfer in sorted(set(transfers)):
        params = dict(
            **{"from": frm}, to=to, results=6, transferTime=transfer,
            stopovers="false", remarks="true", language="de",
        )
        res = get("/journeys", departure=start.isoformat(), **params)
        for _ in range(20):
            last = None
            for j in res.get("journeys", []):
                s = summarize(j)
                if s and start <= s["dep"] <= end:
                    found[s["key"]] = s
                if s:
                    last = s["dep"]
            if not res.get("laterRef") or last is None or last > end:
                break
            time.sleep(0.4)
            res = get("/journeys", laterThan=res["laterRef"], **params)
    js = sorted(found.values(), key=lambda s: (s["dep"], s["arr"]))
    for s in js:
        later = [o for o in js if o["dep"] > s["dep"] and not o["cancelled"]]
        s["planb"] = min((o["arr"] for o in later), default=None)
    return js


def is_good(s, puffer):
    return not s["cancelled"] and (s["minbuf"] is None or s["minbuf"] >= puffer)


# --------------------------------------------------------- delay data/model
LOG_FIELDS = ["key", "datum", "halt", "art", "linie", "produkt", "geplant", "verspaetung_min", "ausfall", "gleis"]


def load_log():
    by_line = defaultdict(lambda: {"delays": [], "cancel": 0, "n": 0})
    seen = set()
    if LOG.exists():
        with LOG.open(newline="") as f:
            for row in csv.DictReader(f):
                seen.add(row["key"])
                d = by_line[row["linie"]]
                d["n"] += 1
                if row["ausfall"] == "1":
                    d["cancel"] += 1
                else:
                    d["delays"].append(float(row["verspaetung_min"]))
    return by_line, seen


class DelayModel:
    def __init__(self):
        self.learned, _ = load_log()

    def source(self, line, product):
        d = self.learned.get(line)
        if d and d["n"] >= MIN_LEARNED and d["delays"]:
            return f"{line}: gelernt aus {d['n']} Beobachtungen"
        return f"{line}: Schaetzwert ({product})"

    def draw(self, rng, leg, now):
        """Returns (cancelled, delay_min) relative to the time we already know."""
        soon = leg["realtime"] and leg["dep"] and -5 <= (leg["dep"] - now).total_seconds() / 60 <= 20
        if soon:  # live data for a train leaving soon is already quite reliable
            return rng.random() < 0.003, max(-0.5, rng.gauss(0, 1.0))
        d = self.learned.get(leg["line"])
        if d and d["n"] >= MIN_LEARNED and d["delays"]:
            p_cancel = (d["cancel"] + 1) / (d["n"] + 2)
            return rng.random() < p_cancel, rng.choice(d["delays"])
        p = PRIORS.get(leg["product"], PRIORS["suburban"])
        u = rng.random()
        if u < p["cancel"]:
            return True, 0.0
        if rng.random() < p["tail"]:
            return False, rng.expovariate(1 / p["mean"])
        return False, rng.uniform(-0.5, 1.0)


# ---------------------------------------------------------------- simulation
class Simulation:
    """Monte Carlo over train delays/cancellations and bike times.

    Per sample, each trip gets one delay (shared by all connections using it).
    For each connection we get L = latest time you must be at the start platform
    and A = arrival at the end platform (or invalid if a transfer breaks).
    Arrival for a given start time r is then min(A) over connections with L >= r.
    """

    def __init__(self, js, model, umstieg, pre, post, n=3000, seed=42):
        self.ref = js[0]["dep"].replace(hour=0, minute=0, second=0, microsecond=0) if js else None
        self.samples, self.pre, self.post = [], [], []
        self.ok = [0] * len(js)
        self.n = n
        rng, now = random.Random(seed), datetime.now(TZ)
        m = lambda dt: (dt - self.ref).total_seconds() / 60
        for _ in range(n):
            draws, pairs = {}, []
            for idx, s in enumerate(js):
                ready, valid, first_L = None, True, None
                for leg in s["sim"]:
                    if leg["trip"] not in draws:
                        draws[leg["trip"]] = model.draw(rng, leg, now)
                    canc, d = draws[leg["trip"]]
                    if canc or leg["cancelled"] or not leg["dep"] or not leg["arr"]:
                        valid = False
                        break
                    dep, arr = m(leg["dep"]) + d, m(leg["arr"]) + d
                    if ready is None:
                        first_L = dep - leg["walk_before"]
                    elif ready + leg["walk_before"] + umstieg > dep:
                        valid = False
                        break
                    ready = arr
                if valid and ready is not None:
                    self.ok[idx] += 1
                    pairs.append((first_L, ready + s["walk_after"]))
            pairs.sort()
            Ls = [p[0] for p in pairs]
            suff, best = [0.0] * len(pairs), math.inf
            for i in range(len(pairs) - 1, -1, -1):
                best = min(best, pairs[i][1])
                suff[i] = best
            self.samples.append((Ls, suff))
            self.pre.append(pre(rng))
            self.post.append(post(rng))

    def score(self, idx):
        return self.ok[idx] / self.n

    def arrivals(self, leave):
        """Door arrival times (minutes since midnight) when leaving the door at `leave`."""
        r0 = (leave - self.ref).total_seconds() / 60
        out = []
        for (Ls, suff), pre, post in zip(self.samples, self.pre, self.post):
            i = bisect.bisect_left(Ls, r0 + pre)
            out.append(suff[i] + post if i < len(Ls) else math.inf)
        return out

    def p_by(self, leave, target):
        t = (target - self.ref).total_seconds() / 60
        return sum(a <= t for a in self.arrivals(leave)) / self.n

    def quantile(self, leave, q):
        a = sorted(self.arrivals(leave))
        v = a[min(len(a) - 1, int(q * len(a)))]
        return None if math.isinf(v) else self.ref + mins(v)


# ------------------------------------------------------------------- output
def print_table(title, js, puffer, pre, post, door_from, door_to, sim, show):
    print(f"\n{title}")
    print("-" * len(title))
    rows = [(i, s) for i, s in enumerate(js) if show(s)]
    if not rows:
        print("  keine Verbindungen gefunden")
        return
    print(f"  {'':1} {door_from:>8} {'Zug ab':>6} {'Zug an':>6} {door_to:>8} {'Umst.':>5} "
          f"{'Score':>5}  {'da 50%':>6} {'da 95%':>6}  {'Plan B':<13} Route")
    for i, s in rows:
        mark = "*" if is_good(s, puffer) else " "
        buf = "-" if s["minbuf"] is None else f"{s['minbuf']:.0f}"
        if s["planb"] is None:
            pb = "?"
        elif s["planb"] <= s["arr"] + mins(1):
            pb = "= gleich"
        else:
            pb = f"{hhmm(s['planb'] + mins(post))} (+{(s['planb'] - s['arr']).total_seconds() / 60:.0f})"
        flags = ""
        if s["cancelled"]:
            flags += "  !! FAELLT AUS"
        if s["delay"] >= 2:
            flags += f"  (+{s['delay']:.0f} min Verspaetung)"
        leave, reach = s["dep"] - mins(pre), s["arr"] + mins(post)
        q50, q95 = sim.quantile(leave, 0.5), sim.quantile(leave, 0.95)
        print(f"  {mark} {hhmm(leave):>8} {hhmm(s['dep']):>6} {hhmm(s['arr']):>6} {hhmm(reach):>8} "
              f"{buf:>5} {sim.score(i) * 100:>4.0f}%  {hhmm(q50) if q50 else '  ?  ':>6} "
              f"{hhmm(q95) if q95 else ' spaet':>6}  {pb:<13} {s['route']}{flags}")
    warns = sorted({w for _, s in rows for w in s["warnings"] if w})
    if warns:
        print("\n  Meldungen:")
        for w in warns[:6]:
            print(f"   - {w}")


def arrival_advice(js, target, reserve, puffer, pre, post, door_to, sim, levels):
    latest = target - mins(reserve + post)
    ok = [s for s in js if s["arr"] <= latest and is_good(s, puffer)]
    print(f"\nEmpfehlung: {door_to} bis {hhmm(target)}")
    line = lambda s: (f"zu Hause los {hhmm(s['dep'] - mins(pre))}  ->  Zug {hhmm(s['dep'])}  ->  "
                      f"{door_to} {hhmm(s['arr'] + mins(post))}   [{s['route']}]")
    p = lambda s: f"{sim.p_by(s['dep'] - mins(pre), target) * 100:.0f} % puenktlich"
    if ok:
        pick = max(ok, key=lambda s: s["dep"])
        print(f"  NIMM:     {line(pick)}   {p(pick)}")
        safer = [s for s in ok if s["dep"] < pick["dep"]]
        if not (pick["planb"] and pick["planb"] + mins(post) <= target) and safer:
            s = max(safer, key=lambda s: s["dep"])
            print(f"  SICHERER: {line(s)}   {p(s)}")
        fast = [s for s in js if not s["cancelled"] and s["arr"] <= latest and s["dep"] > pick["dep"]]
        if fast:
            f = max(fast, key=lambda s: s["dep"])
            print(f"  KNAPP:    {line(f)}   {p(f)}")
    else:
        print("  Keine Verbindung mit genug Umsteigepuffer gefunden.")

    # probabilistic: latest leave-home time per certainty level
    print(f"\n  Spaetestens zu Hause losfahren, um mit ... Wahrscheinlichkeit bis {hhmm(target)} da zu sein:")
    leave_times = [target - mins(k) for k in range(0, 181)]
    probs = [(t, sim.p_by(t, target)) for t in leave_times]
    for lvl in levels:
        cands = [t for t, pr in probs if pr >= lvl]
        if cands:
            t = max(cands)
            print(f"   {lvl * 100:>4.0f} %   {hhmm(t)}   (dann {hhmm(sim.quantile(t, 0.5))} da im Mittel)")
        else:
            print(f"   {lvl * 100:>4.0f} %   in den letzten 3 h nicht erreichbar")


# ------------------------------------------------------------------ learning
def learn(stop_names, products=("suburban", "regional", "express"), keep_days=60):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    _, seen = load_log()
    now = datetime.now(TZ)
    new = []
    for name in stop_names:
        sid, sname = find_stop(name)
        for kind in ("departures", "arrivals"):
            res = get(f"/stops/{sid}/{kind}", when=(now - mins(90)).isoformat(),
                      duration=90, results=400, remarks="false")
            items = res.get(kind, []) if isinstance(res, dict) else res
            for it in items:
                planned = parse(it.get("plannedWhen"))
                if not planned or planned > now - mins(5):
                    continue  # only trains that have (almost certainly) left already
                canc = bool(it.get("cancelled"))
                if it.get("delay") is None and not canc:
                    continue  # no real-time data -> can't tell
                key = f"{it.get('tripId')}|{sid}|{kind}"
                if key in seen:
                    continue
                seen.add(key)
                line = it.get("line") or {}
                if products and line.get("product") not in products:
                    continue  # skip buses/trams at the big stations to keep the file small
                new.append(dict(
                    key=key, datum=planned.date().isoformat(), halt=sname, art=kind[:3],
                    linie=line.get("name", "?"), produkt=line.get("product", "?"),
                    geplant=planned.isoformat(),
                    verspaetung_min=f"{(it.get('delay') or 0) / 60:.1f}", ausfall="1" if canc else "0",
                    gleis=it.get("platform") or it.get("plannedPlatform") or "",
                ))
            time.sleep(0.4)
    rows = []
    if LOG.exists():
        with LOG.open(newline="") as f:
            rows = list(csv.DictReader(f))
    cutoff = (now - timedelta(days=keep_days)).date().isoformat()
    rows = [r for r in rows if r["datum"] >= cutoff] + new
    with LOG.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"{len(new)} neue Beobachtungen, {len(rows)} insgesamt (letzte {keep_days} Tage) in {LOG}")


def _read_log():
    if not LOG.exists():
        return []
    with LOG.open(newline="") as f:
        return list(csv.DictReader(f))


def punctuality(rows):
    """Per line: share of ALL scheduled trains that ran with <= 1 / 3 / 5 min delay.
    Cancelled trains count as not punctual (passenger view)."""
    by = defaultdict(list)
    for r in rows:
        by[r["linie"]].append(r)
    out = {}
    for line, rs in by.items():
        n = len(rs)
        canc = sum(r["ausfall"] == "1" for r in rs)
        ds = sorted(float(r["verspaetung_min"]) for r in rs if r["ausfall"] != "1")
        q = lambda p: ds[min(len(ds) - 1, int(p * len(ds)))] if ds else None
        out[line] = {
            "n": n, "ausfall": canc,
            "le1": sum(x <= 1 for x in ds), "le3": sum(x <= 3 for x in ds), "le5": sum(x <= 5 for x in ds),
            "median": q(.5), "p90": q(.9),
        }
    return out


def by_hour(rows):
    """Per line and hour of day (Berlin): n, <= 1 min late, cancelled. Lets the page warn
    'S1 is often late around 8' for the hour of the trip you picked."""
    out = defaultdict(lambda: defaultdict(lambda: {"n": 0, "le1": 0, "ausfall": 0}))
    for r in rows:
        h = str(datetime.fromisoformat(r["geplant"]).astimezone(TZ).hour)
        d = out[r["linie"]][h]
        d["n"] += 1
        if r["ausfall"] == "1":
            d["ausfall"] += 1
        elif float(r["verspaetung_min"]) <= 1:
            d["le1"] += 1
    return out


def heatmap(rows):
    """Per line: weekday (0 = Monday) x hour -> n, <= 1/3/5 min late, cancelled."""
    out = defaultdict(lambda: defaultdict(lambda: {"n": 0, "le1": 0, "le3": 0, "le5": 0, "ausfall": 0}))
    for r in rows:
        t = datetime.fromisoformat(r["geplant"]).astimezone(TZ)
        d = out[r["linie"]][f"{t.weekday()}-{t.hour}"]
        d["n"] += 1
        if r["ausfall"] == "1":
            d["ausfall"] += 1
            continue
        x = float(r["verspaetung_min"])
        d["le1"] += x <= 1
        d["le3"] += x <= 3
        d["le5"] += x <= 5
    return out


def _station(halt):
    h = halt.lower()
    return "pdm" if "potsdam" in h else "wan" if "wannsee" in h else "bg" if "botanischer" in h else None


WANNSEE_WALK = {"same": 2, "tunnel": 4, "regio": 5}   # minutes, see the web page settings


def _island(p):
    digits = "".join(ch for ch in str(p or "") if ch.isdigit())
    if not digits:
        return None
    n = int(digits)
    return "S12" if n <= 2 else "S34" if n <= 4 else "R"


def wannsee_walk(arr_platform, dep_platform):
    a, b = _island(arr_platform), _island(dep_platform)
    if not a or not b:
        return WANNSEE_WALK["tunnel"]
    if a == b:
        return WANNSEE_WALK["same"]
    return WANNSEE_WALK["regio"] if "R" in (a, b) else WANNSEE_WALK["tunnel"]


def connections(rows, min_transfer=None, max_gap=16):
    """How often did a transfer in Wannsee actually work?

    Trips are matched by tripId across the logged stops, so we know an S7 arriving
    in Wannsee came from Potsdam, an S1 leaving Wannsee goes to Botanischer Garten, etc.
    Every feeder/connector pair whose planned gap is at least the walking time between
    their platforms (max max_gap min) counts as one possible transfer; it worked if neither
    train was cancelled and the real gap was still >= that walking time.
    Key: "out|S7>S1|2" = way there, S7 to S1, 2 min planned.
    """
    ev = defaultdict(dict)
    for r in rows:
        st = _station(r["halt"])
        if st:
            ev[r["key"].split("|")[0]][(st, r["art"])] = r
    plan = lambda r: datetime.fromisoformat(r["geplant"])
    real = lambda r: plan(r) + timedelta(minutes=float(r["verspaetung_min"]))
    groups = {k: [] for k in ("out_feed", "out_conn", "back_feed", "back_conn")}

    def leg(e, a, b, into, take):
        if a in e and b in e and plan(e[b]) > plan(e[a]):
            groups[into].append(e[take])

    for e in ev.values():
        leg(e, ("pdm", "dep"), ("wan", "arr"), "out_feed", ("wan", "arr"))
        leg(e, ("wan", "dep"), ("bg", "arr"), "out_conn", ("wan", "dep"))
        leg(e, ("bg", "dep"), ("wan", "arr"), "back_feed", ("wan", "arr"))
        leg(e, ("wan", "dep"), ("pdm", "arr"), "back_conn", ("wan", "dep"))

    res = defaultdict(lambda: {"n": 0, "ok": 0})
    for d in ("out", "back"):
        feeds = sorted(groups[d + "_feed"], key=plan)
        ftimes = [plan(f) for f in feeds]
        for c in groups[d + "_conn"]:
            tc = plan(c)
            i = bisect.bisect_left(ftimes, tc - timedelta(minutes=max_gap))
            while i < len(feeds) and ftimes[i] <= tc - timedelta(minutes=1):
                f = feeds[i]
                gap = round((tc - ftimes[i]).total_seconds() / 60)
                need = min_transfer if min_transfer is not None else wannsee_walk(f.get("gleis"), c.get("gleis"))
                if gap < need:
                    i += 1
                    continue
                k = res[f"{d}|{f['linie']}>{c['linie']}|{gap}"]
                k["n"] += 1
                k["ok"] += int(f["ausfall"] != "1" and c["ausfall"] != "1"
                               and (real(c) - real(f)).total_seconds() / 60 >= need)
                i += 1
    return dict(res)


def export_json(path, max_per_line=600):
    """Data for the web page (data/delays.json): delay samples for the simulation,
    punctuality per line and measured transfer success in Wannsee."""
    rows = _read_log()
    rows.sort(key=lambda r: r["geplant"])
    lines = defaultdict(lambda: {"n": 0, "ausfall": 0, "delays": []})
    for r in rows[::-1]:  # newest first, so the cap keeps recent data
        d = lines[r["linie"]]
        if d["n"] >= max_per_line:
            continue
        d["n"] += 1
        if r["ausfall"] == "1":
            d["ausfall"] += 1
        else:
            d["delays"].append(round(float(r["verspaetung_min"]), 1))
    out = {
        "erstellt": datetime.now(TZ).isoformat(),
        "zeitraum": {"von": rows[0]["datum"], "bis": rows[-1]["datum"]} if rows else None,
        "linien": lines,
        "puenktlichkeit": punctuality(rows),
        "stunden": by_hour(rows),
        "heatmap": {k: v for k, v in heatmap(rows).items() if k in ("S1", "S7", "RE1")},
        "anschluesse": connections(rows),
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(out, separators=(",", ":")))
    print(f"{path}: {len(lines)} Linien, {len(rows)} Beobachtungen, {len(out['anschluesse'])} Umstiegsarten")


def statistics():
    rows = _read_log()
    if not rows:
        print(f"Noch keine Daten in {LOG}. Erst ein paar Mal --lernen laufen lassen.")
        return
    print(f"Zeitraum {rows[0]['datum']} bis {max(r['datum'] for r in rows)}, {len(rows)} Beobachtungen\n")
    print("Puenktlichkeit (Anteil aller geplanten Zuege; Ausfaelle zaehlen als unpuenktlich)")
    print(f"{'Linie':<7} {'n':>5} {'<=1 min':>8} {'<=3 min':>8} {'<=5 min':>8} {'Ausfall':>8} {'Median':>7} {'90 %':>6}")
    for line, d in sorted(punctuality(rows).items(), key=lambda kv: -kv[1]["n"]):
        n = d["n"]
        pc = lambda k: f"{d[k] / n * 100:.0f} %"
        print(f"{line:<7} {n:>5} {pc('le1'):>8} {pc('le3'):>8} {pc('le5'):>8} {d['ausfall'] / n * 100:>6.1f} % "
              f"{d['median'] if d['median'] is not None else 0:>6.1f}' {d['p90'] if d['p90'] is not None else 0:>5.1f}'")
    con = connections(rows)
    if con:
        print("\nUmsteigen in Wannsee - wie oft hat es geklappt?")
        for k, d in sorted(con.items(), key=lambda kv: (kv[0].split("|")[0], -kv[1]["n"])):
            dr, lines_, gap = k.split("|")
            if d["n"] >= 3:
                print(f"  {'hin ' if dr == 'out' else 'rueck'}  {lines_.replace('>', ' -> '):<12} {gap:>2} min geplant: "
                      f"{d['ok']:>4} von {d['n']:>4}  = {d['ok'] / d['n'] * 100:.0f} %")


# --------------------------------------------------------------------- main
def at(d, hm):
    return datetime.combine(d, datetime.strptime(hm, "%H:%M").time(), TZ)


def window(d, spec):
    a, b = spec.split("-")
    return at(d, a), at(d, b)


def clip_gauss(rng, mu, sd, lo=0.0):
    return max(lo, rng.gauss(mu, sd))


def main():
    p = argparse.ArgumentParser(description="Robuste Tuer-zu-Tuer-Verbindungen mit Puffer und Unsicherheit.")
    g = p.add_argument_group("Strecke")
    g.add_argument("--start", default="Potsdam Hauptbahnhof", help="Startbahnhof")
    g.add_argument("--ziel", default="S Botanischer Garten", help="Zielbahnhof")
    g.add_argument("--zielort", default="Charite", help="Name fuers Ziel (nur Anzeige)")
    g = p.add_argument_group("Rad- und Fusswege (Minuten)")
    g.add_argument("--rad-start", type=int, default=30, help="Rad zu Hause -> Startbahnhof")
    g.add_argument("--rad-streuung", type=float, default=1.5, help="Schwankung der langen Radfahrt (Std.-Abw.)")
    g.add_argument("--hbf", default="5-10",
                   help="Uebergang Potsdam Hbf (Ampeln vor dem Bahnhof, Rad abstellen, zum Gleis), "
                        "von-bis in Minuten; gilt auch auf dem Rueckweg")
    g.add_argument("--rad-ziel", type=int, default=10, help="Rad Zielbahnhof <-> Zielort")
    g.add_argument("--gleis-ziel", type=int, default=2, help="Zielort-Rad -> Gleis am Zielbahnhof (Rueckweg)")
    g = p.add_argument_group("Zeiten")
    g.add_argument("--datum", help="YYYY-MM-DD (Standard: heute)")
    g.add_argument("--hin", default="07:00-09:00", help="Fenster: wann du zu Hause losfaehrst")
    g.add_argument("--rueck", default="16:00-18:00", help="Fenster: wann du am Zielort losfaehrst")
    g.add_argument("--ankunft", help="HH:MM - spaetestens am Zielort sein (ersetzt --hin)")
    g.add_argument("--reserve", type=int, default=5, help="Reserve vor --ankunft (fuer NIMM)")
    g.add_argument("--sicherheit", default="80,95,99", help="Wahrscheinlichkeiten fuer --ankunft, in %%")
    g = p.add_argument_group("Umstieg")
    g.add_argument("--puffer", type=int, default=6, help="Umsteigezeit, ab der eine Verbindung als sicher (*) gilt")
    g.add_argument("--umstieg", type=int, default=2, help="kuerzeste Umsteigezeit in Minuten (Fussweg zwischen den Bahnsteigen)")
    g = p.add_argument_group("Lernen")
    g.add_argument("--lernen", action="store_true", help="echte Verspaetungen der letzten 90 min speichern")
    g.add_argument("--lern-halte", default="Potsdam Hauptbahnhof;S Wannsee;S Botanischer Garten",
                   help="Haltestellen fuers Lernen, mit ; getrennt")
    g.add_argument("--statistik", action="store_true", help="gelernte Verspaetungen anzeigen")
    g.add_argument("--export-json", metavar="DATEI", help="gelernte Daten fuer die Webseite exportieren")
    g.add_argument("--simulationen", type=int, default=3000)
    a = p.parse_args()

    if a.lernen:
        return learn([s.strip() for s in a.lern_halte.split(";") if s.strip()])
    if a.statistik:
        return statistics()
    if a.export_json:
        return export_json(a.export_json)

    # worst-case times for the table, random times for the simulation
    hbf_lo, hbf_hi = sorted(int(x) for x in a.hbf.split("-"))
    hin_pre = a.rad_start + hbf_hi
    hin_post = a.rad_ziel
    rueck_pre = a.rad_ziel + a.gleis_ziel
    rueck_post = hbf_hi + a.rad_start
    # bike ride plus Potsdam Hbf transit, anywhere between hbf_lo and hbf_hi minutes
    bike_long = lambda r: clip_gauss(r, a.rad_start, a.rad_streuung) + r.uniform(hbf_lo, hbf_hi)
    hin_sim = (bike_long, lambda r: clip_gauss(r, a.rad_ziel, 1.0))
    rueck_sim = (lambda r: clip_gauss(r, a.rad_ziel, 1.0) + clip_gauss(r, a.gleis_ziel, 0.5), bike_long)
    tt = (a.umstieg, a.puffer)
    model = DelayModel()

    d = date.fromisoformat(a.datum) if a.datum else datetime.now(TZ).date()
    home_id, home = find_stop(a.start)
    dest_id, dest = find_stop(a.ziel)
    print(f"{d:%a %d.%m.%Y}   zu Hause -> {home} <-> {dest} -> {a.zielort}")
    print(f"Potsdam Hbf: {hbf_lo:g}-{hbf_hi:g} min.  Tuerzeiten im Worst Case: hin {hin_pre:g} min bis zum Zug, {hin_post} danach; "
          f"rueck {rueck_pre} bis zum Zug, {rueck_post} danach.")

    def run(frm, to, s, e, pre, post, simfn, title, labels):
        js = collect(frm, to, s, e + mins(60), tt)  # +60 min: backups for late rows
        if not js:
            print(f"\n{title}\n  keine Verbindungen gefunden")
            return js, None
        sim = Simulation(js, model, a.umstieg, simfn[0], simfn[1], n=a.simulationen)
        print_table(title, js, a.puffer, pre, post, *labels, sim, show=lambda x: x["dep"] <= e)
        return js, sim

    if a.ankunft:
        target = at(d, a.ankunft)
        last_train = target - mins(hin_post)
        js, sim = run(home_id, dest_id, last_train - mins(150), last_train, hin_pre, hin_post, hin_sim,
                      f"Hinweg - am {a.zielort} bis {a.ankunft}", ("Haus los", "Ziel an"))
        if sim:
            levels = [float(x) / 100 for x in a.sicherheit.split(",")]
            arrival_advice([s for s in js if s["dep"] <= last_train], target, a.reserve, a.puffer,
                           hin_pre, hin_post, a.zielort, sim, levels)
    else:
        s, e = window(d, a.hin)
        js, _ = run(home_id, dest_id, s + mins(hin_pre), e + mins(hin_pre), hin_pre, hin_post, hin_sim,
                    f"Hinweg - zu Hause los {a.hin}", ("Haus los", "Ziel an"))
    s, e = window(d, a.rueck)
    js2, _ = run(dest_id, home_id, s + mins(rueck_pre), e + mins(rueck_pre), rueck_pre, rueck_post, rueck_sim,
                 f"Rueckweg - am {a.zielort} los {a.rueck}", ("Ziel los", "Haus an"))

    lines = {(l["line"], l["product"]) for s in (js or []) + (js2 or []) for l in s["sim"]}
    print("\n* = sicher (Umstieg >= Puffer).  Score = alle Umstiege klappen.  da 50/95% = Ankunft an der Tuer"
          "\nin 50 / 95 % der simulierten Faelle (inkl. Ausweichen auf spaetere Zuege).")
    print("Verspaetungsmodell: " + "; ".join(sorted(model.source(l, p) for l, p in lines)))


if __name__ == "__main__":
    main()
