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
def strength_model(table, games):
    """Poisson attack/defense rates per team, shrunk toward league average (prior weight 2 games)."""
    import math
    finals = [g for g in games if g["round"] == "pool" and g["status"] == "final"]
    avg = (sum(g["hs"] + g["as"] for g in finals) / (2 * len(finals))) if finals else 3.0
    rates = {}
    for r in table:
        gp, k = r["gp"], 2.0
        rates[r["team"]] = ((r["gf"] + avg * k) / (gp + k) / avg, (r["ga"] + avg * k) / (gp + k) / avg)

    def play(rnd, h, a):
        eh = avg * rates[h][0] * rates[a][1]
        ea = avg * rates[a][0] * rates[h][1]

        def pois(lam):
            L, kk, p = math.exp(-lam), 0, rnd.random()
            while p > L:
                p *= rnd.random(); kk += 1
            return kk
        x, y, so = pois(eh), pois(ea), False
        if x == y:
            so = True
            if rnd.random() < 0.5:
                x += 1
            else:
                y += 1
        if x - y > 7: x = y + 7
        if y - x > 7: y = x + 7
        return x, y, so
    return play


def pack(p, gf, ga):
    return p * 10_000_000 + round(gq(gf, ga) * 1000) * 10_000 + (9999 - min(ga, 9999))


def project(table, games, sims=500, seed=7):
    """Simulate the remaining prelim games not involving the Sabres. Returns, per sim, the other
    teams' packed (pts, goal quotient, fewest GA) values sorted best first."""
    rnd = random.Random(seed)
    play = strength_model(table, games)
    base = {r["team"]: [r["pts"], r["gf"], r["ga"]] for r in table}
    pending = [g for g in games if g["round"] == "pool" and g["status"] != "final" and US not in (g["home"], g["away"])]
    out = []
    for _ in range(sims):
        st = {k: v[:] for k, v in base.items()}
        for g in pending:
            h, a = g["home"], g["away"]
            if h not in st or a not in st:
                continue
            x, y, so = play(rnd, h, a)
            st[h][1] += x; st[h][2] += y; st[a][1] += y; st[a][2] += x
            if x > y:
                st[h][0] += 2 if so else 3; st[a][0] += 1 if so else 0
            else:
                st[a][0] += 2 if so else 3; st[h][0] += 1 if so else 0
        out.append(sorted((pack(*v) for k, v in st.items() if k != US), reverse=True))
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


def rank_dist(sims, pts, gf, ga):
    """Rank distribution of the Sabres across simulated futures for a given final ledger."""
    packed = pack(pts, gf, ga)
    ranks = []
    for sim in sims:
        better = 0
        for v in sim:
            if v > packed:
                better += 1
            else:
                break
        ranks.append(better + 1)
    return ranks


def render_scenarios(table, games, us, remaining, avoid_mode, opp_names):
    if not remaining or len(remaining) > 2:
        return "", ""
    sims = project(table, games, sims=4000, seed=21)
    n_final = sum(1 for g in games if g["round"] == "pool" and g["status"] == "final")
    n_pend = sum(1 for g in games if g["round"] == "pool" and g["status"] != "final")
    P, GF, GA = us["pts"], us["gf"], us["ga"]

    def outcome(us_g, op_g, so=False):
        # (pts, gf, ga) after one game; shootout winner is credited one extra goal
        if us_g > op_g:
            return (3, us_g, op_g)
        if us_g == op_g:
            return (2, us_g + 1, op_g) if so else (1, us_g, op_g + 1)
        return (0, us_g, op_g)
    if len(remaining) == 1:
        rows = [("Win, regulation or shootout", [(3, 0)]), ("Shootout loss (1 point)", [(1, 1, "sol")]),
                ("Lose 3–6", [(3, 6)]), ("Lose 2–4", [(2, 4)]), ("Lose 1–3", [(1, 3)]), ("Lose 0–1", [(0, 1)]), ("Lose 0–3 or worse", [(0, 3)])] if avoid_mode else \
               [("Win 7–0", [(7, 0)]), ("Win 4–0", [(4, 0)]), ("Win 2–0", [(2, 0)]), ("Win 2–1", [(2, 1)]), ("Shootout win", [(2, 2, "sow")]), ("Shootout loss", [(2, 2, "sol")]), ("Regulation loss", [(1, 3)])]
    else:
        rows = [("Win 5–0 and 5–0", [(5, 0), (5, 0)]), ("Win 3–0 and 3–0", [(3, 0), (3, 0)]), ("Win 4–1 and 3–1", [(4, 1), (3, 1)]), ("Win 2–1 and 2–1", [(2, 1), (2, 1)]),
                ("Win 1–0 and 1–0", [(1, 0), (1, 0)]), ("One regulation win, one shootout win", [(3, 0), (2, 2, "sow")]), ("Win one, lose one", [(3, 0), (1, 3)])]
    out = []
    for label, results in rows:
        p, gf, ga = P, GF, GA
        for r in results:
            dp, dgf, dga = outcome(r[0], r[1], so=(len(r) > 2 and r[2] == "sow")) if not (len(r) > 2 and r[2] == "sol") else (1, r[0], r[1] + 1)
            p += dp; gf += dgf; ga += dga
        ranks = rank_dist(sims, p, gf, ga)
        N = len(ranks)
        if avoid_mode:
            cells = [sum(1 for x in ranks if x <= 16) / N, sum(1 for x in ranks if x == 17) / N, sum(1 for x in ranks if x == 18) / N]
        else:
            s = sorted(ranks)
            cells = [sum(1 for x in ranks if x <= TOP_N) / N, s[N // 2], f"{s[N // 10]}–{s[9 * N // 10]}"]
        cls = "good" if cells[0] >= 0.85 else ("warn" if cells[0] >= 0.15 else "bad")
        fmtc = lambda c: f"{round(c * 100)}%" if isinstance(c, float) else str(c)
        out.append(f'<tr><td>{esc(label)}</td><td class="r"><span class="pill {cls}">{fmtc(cells[0])}</span></td>' + "".join(f'<td class="r">{fmtc(c)}</td>' for c in cells[1:]) + "</tr>")
    head = ('<tr><th>Sabres\' result today</th><th class="r">Avoid Sunday game</th><th class="r">Seed 17</th><th class="r">Seed 18</th></tr>' if avoid_mode
            else '<tr><th>Sabres\' results</th><th class="r">Top-8 odds</th><th class="r">Median seed</th><th class="r">Likely range</th></tr>')
    opp = " and ".join(opp_names)
    note = (f'A team-strength model fitted to the {n_final} results so far, replaying the {n_pend - len(remaining)} other remaining preliminary games {len(sims):,} times for each Sabres outcome against {esc(opp)}. '
            f'Shootout results credit the winner one extra goal, as the tournament does. Estimates, not guarantees.')
    return head + "\n" + "\n".join(out), note


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

    # can the Sabres still reach the quarterfinals? (teams already at or above our max points)
    max_pts = us["pts"] + 3 * len(remaining)
    locked_above = sum(1 for r in table if r["team"] != US and r["pts"] >= max_pts)
    avoid_mode = bool(remaining) and locked_above >= TOP_N + 2
    target = 16 if avoid_mode else TOP_N

    # remaining-game inputs for the calculator
    calc_rows, opp_json = [], []
    for i, g in enumerate(remaining, 1):
        home = g["home"] == US
        opp = g["away"] if home else g["home"]
        abbr = "".join(w[0] for w in re.sub(r"\d+U|18U", "", opp).split()[:3]).upper() or "OPP"
        calc_rows.append(
            f'<div class="row"><label for="g{i}s">{"vs" if home else "at"} {esc(opp)}<span>{esc(day_short(g))} {esc(g["time"].lstrip("0"))} · {esc(short_loc(g["loc"]))}</span></label>'
            f'<div class="score"><span class="side">SMD</span><input id="g{i}s" type="number" min="0" max="15" value="{1 if avoid_mode else 3}" inputmode="numeric"><span class="dash">–</span>'
            f'<input id="g{i}o" type="number" min="0" max="15" value="{1 if avoid_mode else 0}" inputmode="numeric"><span class="side">{esc(abbr)}</span></div></div>'
        )
        opp_json.append(opp)
    proj = project(table, games) if remaining else []

    # callout copy by phase
    if remaining and avoid_mode:
        n = len(remaining)
        zero = [r for r in table if r["team"] != US and r["pts"] == us["pts"]]
        callout = (f'<p><strong>The quarterfinals are out of reach. The goal now is to stay out of the Sunday 17-vs-18 game.</strong> '
                   f'Seeds 9 through 16 play a consolation game Saturday night and are done. Seeds 17 and 18 play Sunday at 11:10 AM. '
                   f'The Sabres are {seed_txt.lower()} with {us["pts"]} point{"s" if us["pts"] != 1 else ""}, {len(zero)} other team{"s" if len(zero) != 1 else ""} on the same points, and {n} game{"s" if n > 1 else ""} left.</p><ul>'
                   f'<li><strong>One point is the whole game.</strong> Prelim games have no overtime: a tie after three periods goes straight to a shootout, and the shootout loser still gets 1 point. One point puts the Sabres above every team still on {us["pts"]}.</li>'
                   f'<li><strong>A shutout loss is fatal.</strong> With {us["gf"]} goals for, the Sabres\' goal quotient is {esc(fmt_gq(gq(us["gf"], us["ga"])))}. The next tiebreaker is fewest goals against, and at {us["ga"]} they have the most in the group.</li>'
                   f'<li><strong>If it\'s a loss, score anyway.</strong> Every Sabres goal lifts the quotient above teams that get shut out today. Two or more goals in a close loss keeps a slim chance alive.</li>'
                   f'<li><strong>Keep it tight.</strong> A 0–0 or 1–1 game into the shootout is worth far more than chasing goals and losing by three.</li></ul>')
    elif remaining:
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

    scen_rows, scen_note = render_scenarios(table, games, us, remaining, avoid_mode, opp_json)
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
        "SCEN_ROWS": scen_rows, "SCEN_NOTE": scen_note, "SCEN_HIDDEN": "" if scen_rows else " hidden",
        "GOAL_H2": "What it takes to avoid the Sunday game" if avoid_mode else "What it takes to make the quarterfinals",
        "ODDS_LABEL": "Saturday-night odds" if avoid_mode else "Top-8 odds",
        "SIM_JSON": json.dumps({"base_pts": us["pts"], "base_gf": us["gf"], "base_ga": us["ga"], "n": len(remaining), "opps": opp_json, "target": target, "sims": proj}, separators=(",", ":")),
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
