#!/usr/bin/env python3
"""Rebuild index.html for the Southern Maryland Sabres 18U tracker.

Pulls the live 18U schedule and standings from the 200x85 tournament app,
recomputes the Sabres' situation, runs a small projection of the remaining
preliminary games, and renders template.html -> index.html.
"""
import html
import json
import random
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TOURNEY = "b242def3-6e03-4dba-8a7f-6b5960f35df0"
DIVISION = "889"
BASE = f"https://my.200x85.com/tournaments/{TOURNEY}"
SCHED_URL = f"{BASE}/schedules?division={DIVISION}"
STAND_URL = f"{BASE}/standings?division={DIVISION}"
US = "Southern Maryland Sabres"
TOP_N = 8
YEAR = 2026
ET = ZoneInfo("America/New_York")
HERE = Path(__file__).resolve().parent

# Likely seed pairings for the placeholder slots, by game number, following the
# pattern 200x85 used in its other 2026 events (1v8, 4v5, 2v7, 3v6 in game order).
QF_GUESS = {31: "Seed 1 vs 8", 32: "Seed 4 vs 5", 33: "Seed 2 vs 7", 34: "Seed 3 vs 6"}
SEMI_GUESS = {37: "winners of games 31 and 32", 38: "winners of games 33 and 34"}
VENUE_SHORT = {"Wings Event Center": "Wings", "BCIC - Kzoo": "BCIC Kalamazoo"}


def fetch(url, xhr=False):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (sabres-tracker)"})
    if xhr:
        req.add_header("X-Requested-With", "XMLHttpRequest")
        req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def text(fragment):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def esc(s):
    return html.escape(str(s), quote=True)


# ---------------------------------------------------------------- schedule
def parse_schedule(raw):
    data = json.loads(raw)["html"]
    games, day = [], None
    for tr in re.findall(r"<tr[^>]*>.*?</tr>", data, flags=re.S):
        if "fa-calendar-alt" in tr:
            day = text(tr)  # e.g. "Friday, September 11th"
            continue
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.S)
        if len(tds) < 12 or not text(tds[0]).isdigit():
            continue
        home_lab = re.search(r"<i[^>]*>\((.*?)\)</i>", tds[2])
        away_lab = re.search(r"<i[^>]*>\((.*?)\)</i>", tds[8])
        home = text(re.sub(r"<i[^>]*>.*?</i>", "", tds[2], flags=re.S))
        away = text(re.sub(r"<i[^>]*>.*?</i>", "", tds[8], flags=re.S))
        loc = text(tds[9]).replace(" / ", " · ").replace("/", "·")
        m = re.match(r"\w+, (\w+) (\d+)", day or "")
        when = None
        if m:
            try:
                when = datetime.strptime(f"{m.group(1)} {m.group(2)} {YEAR} {text(tds[1])}", "%B %d %Y %I:%M %p").replace(tzinfo=ET)
            except ValueError:
                when = None
        games.append({
            "num": int(text(tds[0])), "time": text(tds[1]), "day": day, "when": when,
            "home": home, "away": away,
            "home_label": home_lab.group(1) if home_lab else "", "away_label": away_lab.group(1) if away_lab else "",
            "hs": int(text(tds[4]) or 0), "as": int(text(tds[6]) or 0),
            "status": text(tds[5]).lower(), "loc": loc, "round": text(tds[11]).lower(),
            "so": "so" in text(tds[5]).lower() or "shootout" in tr.lower(),
        })
    games.sort(key=lambda g: (g["when"] or datetime.max.replace(tzinfo=ET), g["num"]))
    return games


def is_placeholder(name):
    return bool(re.search(r"placeholder|seed #|winner of|game #|\*$", name, flags=re.I))


# ---------------------------------------------------------------- standings
def parse_standings(raw):
    tb = re.search(r"<table[^>]*id=\"standings\".*?</table>", raw, flags=re.S)
    rows = []
    if not tb:
        return rows
    for tr in re.findall(r"<tr[^>]*>.*?(?=<tr|</table>)", tb.group(0), flags=re.S):
        tds = re.findall(r"<td[^>]*>(.*?)(?=<td|</tr>|$)", tr, flags=re.S)
        if len(tds) < 12:
            continue
        name = text(tds[0])
        nums = [text(t) for t in tds[1:]]
        try:
            gp, w, t, l, otw, otl, pts, gf, ga = [int(x) for x in nums[:9]]
        except ValueError:
            continue
        rows.append({"team": name, "gp": gp, "w": w, "l": l, "otw": otw, "otl": otl, "pts": pts, "gf": gf, "ga": ga})
    return rows


def gq(gf, ga):
    return gf / (gf + ga) if gf + ga else 0.0


def fmt_gq(q):
    return "1.000" if q >= 1 else f".{round(q * 1000):03d}"


def sort_table(rows, games):
    """Points, then goal quotient, then fewest GA. Two-team ties on points use head to head first."""
    rows = sorted(rows, key=lambda r: (-r["pts"], -gq(r["gf"], r["ga"]), r["ga"], -r["w"], r["team"]))
    finals = [g for g in games if g["round"] == "pool" and g["status"] == "final"]
    i = 0
    while i < len(rows) - 1:
        j = i
        while j + 1 < len(rows) and rows[j + 1]["pts"] == rows[i]["pts"]:
            j += 1
        if j == i + 1:  # exactly two tied
            a, b = rows[i]["team"], rows[j]["team"]
            for g in finals:
                if {g["home"], g["away"]} == {a, b}:
                    winner = g["home"] if g["hs"] > g["as"] else g["away"]
                    if winner == b:
                        rows[i], rows[j] = rows[j], rows[i]
        i = j + 1
    return rows


# ---------------------------------------------------------------- projection
def project(table, games, sims=500, seed=7):
    """Simulate the remaining non-Sabres prelim games. Returns a compact list of the other
    17 teams' final (pts, gq*1000) per sim, sorted best first, plus our fixed contribution.
    Our own remaining games are assumed to be 3-0 Sabres wins for the opponents' ledgers."""
    rnd = random.Random(seed)
    base = {r["team"]: [r["pts"], r["gf"], r["ga"]] for r in table}
    pending = [g for g in games if g["round"] == "pool" and g["status"] != "final"]
    finals = [g for g in games if g["round"] == "pool" and g["status"] == "final"]
    if len(finals) >= 8:
        pool = [s for g in finals for s in (g["hs"], g["as"])]
    else:
        pool = [0, 1, 1, 2, 2, 2, 3, 3, 4, 4, 5, 6]
    out = []
    for _ in range(sims):
        st = {k: v[:] for k, v in base.items()}
        for g in pending:
            h, a = g["home"], g["away"]
            if h not in st or a not in st:
                continue
            if US in (h, a):
                opp = a if h == US else h
                st[opp][2] += 3
                continue
            x, y = rnd.choice(pool), rnd.choice(pool)
            so = False
            if x == y:
                so = True
                if rnd.random() < 0.5:
                    x += 1
                else:
                    y += 1
            st[h][1] += x; st[h][2] += y; st[a][1] += y; st[a][2] += x
            if x > y:
                st[h][0] += 2 if so else 3; st[a][0] += 1 if so else 0
            else:
                st[a][0] += 2 if so else 3; st[h][0] += 1 if so else 0
        others = sorted(((v[0], round(gq(v[1], v[2]) * 1000)) for k, v in st.items() if k != US), reverse=True)
        out.append([p * 10000 + q for p, q in others])  # pack as pts*10000+gq
    return out


# ---------------------------------------------------------------- rendering
def short_loc(loc):
    for k, v in VENUE_SHORT.items():
        loc = loc.replace(k, v)
    return loc


def day_short(g):
    return g["when"].strftime("%a %b %-d") if g["when"] else (g["day"] or "")


def render_our_games(games, our):
    out = []
    nxt = next((g for g in our if g["status"] != "final"), None)
    for g in our:
        home = g["home"] == US
        opp = g["away"] if home else g["home"]
        cls, pill, note = "later", "", ""
        if g["status"] == "final":
            us_s, op_s = (g["hs"], g["as"]) if home else (g["as"], g["hs"])
            won = us_s > op_s
            cls = "won" if won else "done"
            pill = f'<span class="pill {"good" if won else "bad"}">{"Win" if won else "Loss"} {us_s}–{op_s}</span>'
        elif g["status"] == "in-progress":
            us_s, op_s = (g["hs"], g["as"]) if home else (g["as"], g["hs"])
            cls, pill = "next", f'<span class="pill warn">Live {us_s}–{op_s}</span>'
        elif g is nxt:
            cls, pill = "next", '<span class="pill gold">Next</span>'
        if g["round"] != "pool":
            pill += f' <span class="pill muted">{esc(g["round"].replace("-", " "))}</span>'
        jersey = "Sabres are the home team, wear white" if home else "Sabres are the visitor, wear dark"
        out.append(
            f'<div class="game {cls}"><div class="when"><div class="day">{esc(day_short(g))}</div><div class="disp">{esc(g["time"].lstrip("0"))}</div></div>'
            f'<div><div class="opp">{"vs" if home else "at"} {esc(opp)} {pill}</div><div class="meta">{esc(short_loc(g["loc"]))} · {jersey}</div>{note}</div></div>'
        )
    return "\n".join(out)


def render_bracket(games):
    days, order = {}, []
    for g in games:
        if g["round"] == "pool":
            continue
        d = g["when"].strftime("%A, %b %-d") if g["when"] else g["day"]
        if d not in days:
            days[d] = []; order.append(d)
        days[d].append(g)
    out = []
    for d in order:
        out.append(f'<div class="bday"><h3>{esc(d)}</h3>')
        for g in days[d]:
            r = g["round"]
            cls = {"quarter-final": "qf", "semi-final": "semi", "final": "final"}.get(r, "cons")
            title = {"quarter-final": "Quarterfinal", "semi-final": "Semifinal", "final": "Championship", "consolation": "Consolation"}.get(r, r.title())
            filled = not (is_placeholder(g["home"]) or is_placeholder(g["away"]))
            if filled:
                hl = f' <small>({esc(g["home_label"])})</small>' if g["home_label"] else ""
                al = f' <small>({esc(g["away_label"])})</small>' if g["away_label"] else ""
                score = f' <span class="pill {"good" if g["status"] == "final" else "warn"}">{g["hs"]}–{g["as"]}{" final" if g["status"] == "final" else ""}</span>' if g["status"] != "scheduled" else ""
                us_cls = " ours" if US in (g["home"], g["away"]) else ""
                who = f'<div class="who{us_cls}">{esc(g["home"])}{hl} vs {esc(g["away"])}{al}{score}<span>{esc(title)} · {esc(short_loc(g["loc"]))}</span></div>'
                pill = f'<span class="pill {"gold" if cls in ("semi", "final") else ("good" if cls == "qf" else "muted")}">{esc(title)}</span>'
            else:
                if r == "quarter-final":
                    hint, pill = short_loc(g["loc"]), f'<span class="pill good">{QF_GUESS.get(g["num"], "Top 8 seeds")}</span>'
                elif r == "semi-final":
                    hint, pill = f'{short_loc(g["loc"])} · {SEMI_GUESS.get(g["num"], "quarterfinal winners")}', '<span class="pill gold">Semi</span>'
                elif r == "final":
                    hint, pill = f'{short_loc(g["loc"])} · winners of the two semifinals', '<span class="pill gold">Final</span>'
                else:
                    seeds = re.findall(r"Seed #(\d+)", g["home"] + " " + g["away"])
                    lab = f"Seed {seeds[0]} vs {seeds[1]}" if len(seeds) == 2 else "Seeds 9–16"
                    hint, pill = f'{short_loc(g["loc"])} · {"" if len(seeds) == 2 else "two of seeds 9–16"}'.rstrip(" ·"), f'<span class="pill muted">{lab}</span>'
                who = f'<div class="who">{esc(title)}<span>{esc(hint)}</span></div>'
            out.append(f'<div class="slot {cls}"><div class="t">{esc(g["time"].lstrip("0"))}<small>Game {g["num"]}</small></div>{who}{pill}</div>')
        out.append("</div>")
    return "\n".join(out)


def render_standings(table, live_games):
    live = {}
    for g in live_games:
        if g["status"] == "in-progress":
            live[g["home"]] = f'{"up" if g["hs"] > g["as"] else ("down" if g["hs"] < g["as"] else "tied")} {g["hs"]}–{g["as"]}, in progress'
            live[g["away"]] = f'{"up" if g["as"] > g["hs"] else ("down" if g["as"] < g["hs"] else "tied")} {g["as"]}–{g["hs"]}, in progress'
    out = []
    for i, r in enumerate(table, 1):
        tag = f' <span class="tag">{esc(live[r["team"]])}</span>' if r["team"] in live else ""
        cls = ' class="us"' if r["team"] == US else (' class="cut"' if i == TOP_N else "")
        q = fmt_gq(gq(r["gf"], r["ga"])) if r["gp"] else "—"
        out.append(
            f'<tr{cls}><td class="num">{i}</td><td>{esc(r["team"])}{tag}</td><td class="r">{r["gp"]}</td><td class="r">{r["w"] + r["otw"]}</td>'
            f'<td class="r">{r["l"] + r["otl"]}</td><td class="r">{r["pts"]}</td><td class="r">{r["gf"]}</td><td class="r">{r["ga"]}</td><td class="r">{q}</td></tr>'
        )
    return "\n".join(out)


def main():
    sched_raw = fetch(SCHED_URL, xhr=True)
    stand_raw = fetch(STAND_URL)
    games = parse_schedule(sched_raw)
    table = parse_standings(stand_raw)
    if not games or len(table) < 10:
        print("parse failure: games", len(games), "table", len(table), file=sys.stderr)
        sys.exit(1)
    table = sort_table(table, games)
    us = next((r for r in table if r["team"] == US), None)
    if us is None:
        print("Sabres not found in standings", file=sys.stderr)
        sys.exit(1)
    rank = [r["team"] for r in table].index(US) + 1

    our = [g for g in games if US in (g["home"], g["away"])]
    our_pool = [g for g in our if g["round"] == "pool"]
    remaining = [g for g in our_pool if g["status"] != "final"]
    pool_total = sum(1 for g in games if g["round"] == "pool")
    pool_done = sum(1 for g in games if g["round"] == "pool" and g["status"] == "final")
    prelims_over = pool_done == pool_total
    now = datetime.now(ET)

    # situation board
    nxt = next((g for g in our if g["status"] != "final"), None)
    if nxt:
        rel = "Today" if nxt["when"] and nxt["when"].date() == now.date() else day_short(nxt)
        next_big, next_small = nxt["time"].lstrip("0"), f'{rel}, {short_loc(nxt["loc"])}'
        if nxt["round"] != "pool":
            next_small = f'{nxt["round"].replace("-", " ").title()} · {next_small}'
    else:
        next_big, next_small = "—", "No games left"
    last = next((g for g in reversed(our) if g["status"] == "final"), None)
    if last:
        home = last["home"] == US
        us_s, op_s = (last["hs"], last["as"]) if home else (last["as"], last["hs"])
        opp = last["away"] if home else last["home"]
        last_txt = f'{"Beat" if us_s > op_s else "Lost to"} {opp} {us_s}–{op_s}'
    else:
        last_txt = "No games played yet"
    seed_txt = f"Seeded {rank}" if prelims_over else f"{rank}th of {len(table)} right now".replace("1th", "1st").replace("2th", "2nd").replace("3th", "3rd").replace("11st", "11th").replace("12nd", "12th").replace("13rd", "13th")

    # remaining-game inputs for the calculator
    calc_rows, opp_json = [], []
    for i, g in enumerate(remaining, 1):
        home = g["home"] == US
        opp = g["away"] if home else g["home"]
        abbr = "".join(w[0] for w in re.sub(r"\d+U|18U", "", opp).split()[:3]).upper() or "OPP"
        calc_rows.append(
            f'<div class="row"><label for="g{i}s">{"vs" if home else "at"} {esc(opp)}<span>{esc(day_short(g))} {esc(g["time"].lstrip("0"))} · {esc(short_loc(g["loc"]))}</span></label>'
            f'<div class="score"><span class="side">SMD</span><input id="g{i}s" type="number" min="0" max="15" value="3" inputmode="numeric"><span class="dash">–</span>'
            f'<input id="g{i}o" type="number" min="0" max="15" value="0" inputmode="numeric"><span class="side">{esc(abbr)}</span></div></div>'
        )
        opp_json.append(opp)
    proj = project(table, games) if remaining else []

    # callout copy by phase
    if remaining:
        n = len(remaining)
        need = TOP_N
        lead = (f"<strong>Win {'both remaining games' if n == 2 else 'tomorrow'} in regulation, and win by a margin.</strong> "
                if n == 2 else "<strong>Win the last game in regulation, and win by a margin.</strong> ")
        callout = (f'<p>{lead}The 18U division is one {len(table)}-team table. Everyone plays three games, then the top {need} seeds go to the '
                   f'quarterfinals Saturday night. The Sabres have {us["pts"]} point{"s" if us["pts"] != 1 else ""} with {n} game{"s" if n > 1 else ""} left, '
                   f'so the most they can finish with is {us["pts"] + 3 * n}.</p><ul>'
                   f'<li><strong>Regulation wins only.</strong> A shootout win is worth 2 points instead of 3, and in a table this tight that usually costs a seed.</li>'
                   f'<li><strong>Goal quotient breaks ties</strong>: goals for ÷ (goals for + goals against). The Sabres are at {esc(fmt_gq(gq(us["gf"], us["ga"])))} '
                   f'({us["gf"]} for, {us["ga"]} against). Getting above .500 means outscoring the remaining opponent{"s" if n > 1 else ""} by a combined {max(0, us["ga"] - us["gf"]) + 1} or more.</li>'
                   f'<li><strong>Shutouts count double.</strong> Goals against is in the quotient and is also the next tiebreaker after it.</li>'
                   f'<li><strong>Run it up, to a point.</strong> A 7-goal cap applies to prelim differentials, so anything past 7–0 doesn\'t help the seed.</li>'
                   f'<li><strong>Head to head helps.</strong> If two teams finish tied on points, the team that beat the other is seeded ahead.</li></ul>')
    elif not prelims_over:
        callout = (f'<p><strong>Prelims are done for the Sabres.</strong> They finished with {us["pts"]} points and a goal quotient of {esc(fmt_gq(gq(us["gf"], us["ga"])))}, '
                   f'currently {seed_txt.lower()}. {pool_total - pool_done} division games are still to be played; the seed is final once the 2:40 PM Saturday game ends and the tournament posts the bracket.</p>')
    else:
        slot = next((g for g in our if g["round"] != "pool"), None)
        where = f' Next up: {slot["round"].replace("-", " ")} at {slot["time"].lstrip("0")} {day_short(slot)}, {short_loc(slot["loc"])}.' if slot else ""
        callout = f'<p><strong>Seeding is final.</strong> The Sabres finished {us["pts"]} points, goal quotient {esc(fmt_gq(gq(us["gf"], us["ga"])))}, seeded {rank} of {len(table)}.{where}</p>'

    tpl = (HERE / "template.html").read_text()
    subs = {
        "RECORD": f'{us["w"] + us["otw"]}–{us["l"] + us["otl"]}',
        "RECORD_SMALL": esc(last_txt),
        "PTS": str(us["pts"]),
        "PTS_SMALL": f'{len(remaining)} game{"s" if len(remaining) != 1 else ""} left, {us["pts"] + 3 * len(remaining)} possible' if remaining else esc(seed_txt),
        "GQ": esc(fmt_gq(gq(us["gf"], us["ga"]))),
        "GQ_SMALL": f'{us["gf"]} for, {us["ga"]} against',
        "NEXT_BIG": esc(next_big), "NEXT_SMALL": esc(next_small),
        "UPDATED": now.strftime("%A, %B %-d, %Y, %-I:%M %p ET"),
        "POOL_DONE": str(pool_done), "POOL_TOTAL": str(pool_total),
        "CALLOUT": callout,
        "CALC_ROWS": "\n".join(calc_rows),
        "CALC_HIDDEN": "" if remaining else " hidden",
        "OUR_GAMES": render_our_games(games, our),
        "BRACKET": render_bracket(games),
        "STANDINGS": render_standings(table, games),
        "N_TEAMS": str(len(table)),
        "SIM_JSON": json.dumps({"base_pts": us["pts"], "base_gf": us["gf"], "base_ga": us["ga"], "n": len(remaining), "opps": opp_json, "sims": proj}, separators=(",", ":")),
    }
    out = tpl
    for k, v in subs.items():
        out = out.replace("{{" + k + "}}", v)
    leftover = re.findall(r"\{\{[A-Z_]+\}\}", out)
    if leftover:
        print("unfilled template keys:", leftover, file=sys.stderr)
        sys.exit(1)
    (HERE / "index.html").write_text(out)
    Path(HERE / "data").mkdir(exist_ok=True)
    (HERE / "data" / "snapshot.json").write_text(json.dumps({"updated": now.isoformat(), "table": table, "games": [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in g.items()} for g in games]}, indent=1))
    print(f"ok: {pool_done}/{pool_total} pool games final, Sabres {us['pts']} pts, rank {rank}, {len(remaining)} left, {len(proj)} sims")


if __name__ == "__main__":
    main()
