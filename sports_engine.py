import os
import re
import time
import requests
from datetime import datetime, timezone, timedelta
from nba_api.stats.endpoints import leaguedashplayerstats, leaguedashteamstats

# ==========================================
# 1. CONFIG & SECRETS
# ==========================================
FB_TOKEN = os.environ["FB_TELEGRAM_TOKEN"]
NBA_TOKEN = os.environ["NBA_TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
WEBHOOK_URL = os.environ["SHEET_WEBHOOK_URL"]

NBA_SEASON = "2026-27"
SCAN_INTERVAL_SEC = 50
MAX_RUNTIME_SEC = 5.8 * 3600  # Clean exit before GitHub's 6h cap
EET = timezone(timedelta(hours=2))

FB_HEADERS = [
    "Date", "Time_EET", "Event_ID", "Competition", "Match", "Minute",
    "Trigger", "Safe_Target_150", "Cutoff_Line", "Why",
    "Live_Stats", "Final_Stats", "Result"
]

NBA_HEADERS = [
    "Date", "Time_EET", "Event_ID", "Tier", "Confidence", "Game",
    "Trigger", "Player", "Target_Stat", "Projected_Final",
    "Safe_Target_150", "Cutoff_Line", "Why", "Alert_Stats", "Final_Stats", "Result"
]

COMPETITIONS = {
    "uefa.champions": ("UCL", False), "uefa.europa": ("UEL", False),
    "uefa.europa.conf": ("UECL", False), "eng.1": ("Premier League", False),
    "esp.1": ("La Liga", False), "ita.1": ("Serie A", False),
    "ger.1": ("Bundesliga", False), "fra.1": ("Ligue 1", False),
    "eng.fa": ("FA Cup", True), "eng.league_cup": ("EFL Cup", True),
    "esp.copa_del_rey": ("Copa del Rey", True), "ita.coppa_italia": ("Coppa Italia", True),
    "ger.dfb_pokal": ("DFB-Pokal", True), "fra.coupe_de_france": ("Coupe de France", True),
}

STRICT_REFS = {
    "mateu lahoz", "gil manzano", "hernandez hernandez", "daniele orsato",
    "marco guida", "maurizio mariani", "anthony taylor", "michael oliver",
    "clement turpin", "felix zwayer", "daniel siebert", "istvan kovacs"
}

# ==========================================
# 2. TELEGRAM & GOOGLE SHEET HELPERS
# ==========================================
def send_tg(token, text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        print(f"TG error: {e}")

def post_sheet(payload):
    try:
        requests.post(WEBHOOK_URL, json=payload, timeout=10)
    except Exception as e:
        print(f"Sheet error: {e}")

# ==========================================
# 3. NBA STARTUP BASELINES
# ==========================================
def load_nba_baselines():
    baselines, mults = {}, {}
    try:
        df = leaguedashplayerstats.LeagueDashPlayerStats(
            season=NBA_SEASON, per_mode_detailed="PerGame", timeout=30
        ).get_data_frames()[0]
        for _, r in df.iterrows():
            mpg = max(float(r["MIN"]), 1.0)
            baselines[r["PLAYER_NAME"].strip().lower()] = {
                "mpg": mpg, "ppg": float(r["PTS"]), "rpg": float(r["REB"]),
                "pts_pm": float(r["PTS"]) / mpg, "reb_pm": float(r["REB"]) / mpg,
                "tpm_pm": float(r["FG3M"]) / mpg,
            }
        tdf = leaguedashteamstats.LeagueDashTeamStats(
            season=NBA_SEASON, measure_type_detailed_defense="Advanced", timeout=30
        ).get_data_frames()[0]
        avg_p, avg_d = tdf["PACE"].mean(), tdf["DEF_RATING"].mean()
        for _, r in tdf.iterrows():
            comb = (r["PACE"] / avg_p * 0.5) + (r["DEF_RATING"] / avg_d * 0.5)
            mults[r["TEAM_NAME"].lower()] = round(max(0.93, min(1.07, comb)), 3)
    except Exception as e:
        print(f"NBA baseline warning: {e}")
    return baselines, mults

BASELINES, TEAM_MULTS = load_nba_baselines()

def get_baseline(name):
    key = name.strip().lower()
    if key in BASELINES:
        return BASELINES[key]
    parts = key.split()
    if parts:
        for k, v in BASELINES.items():
            if k.endswith(" " + parts[-1]):
                return v
    return None

# ==========================================
# 4. FOOTBALL MODULE (6 CORE + 3 WILDCARDS)
# ==========================================
fb_sent, fb_graded, fb_picks = set(), set(), []

def log_fb(tab, row):
    fb_picks.append({**row, "tab": tab, "Result": ""})
    post_sheet({**row, "tab": tab, "headers": FB_HEADERS, "action": "log"})

def run_football_cycle():
    sb_url = "https://site.api.espn.com/apis/site/v2/sports/soccer/{}/scoreboard"
    sum_url = "https://site.api.espn.com/apis/site/v2/sports/soccer/{}/summary?event={}"
    matches = []

    for slug, (comp, is_cup) in COMPETITIONS.items():
        try:
            r = requests.get(sb_url.format(slug), timeout=8).json()
            for ev in r.get("events", []):
                c = ev["competitions"][0]
                st = ev["status"]["type"]
                detail = st.get("shortDetail", "")
                clock = ev["status"].get("displayClock", "0'")
                is_ht = "HT" in detail.upper()
                minute = 45 if is_ht else int(re.sub(r"[^0-9]", "", clock.split(":")[0]) or 0)
                home = next(x for x in c["competitors"] if x["homeAway"] == "home")
                away = next(x for x in c["competitors"] if x["homeAway"] == "away")
                matches.append({
                    "id": ev["id"], "slug": slug, "comp": comp, "is_cup": is_cup,
                    "state": st["state"], "minute": minute, "is_ht": is_ht,
                    "home": home["team"]["shortDisplayName"], "away": away["team"]["shortDisplayName"],
                    "h_sc": int(home.get("score", 0)), "a_sc": int(away.get("score", 0)),
                })
        except Exception:
            continue

    live = [m for m in matches if m["state"] == "in"]
    finished = [m for m in matches if m["state"] == "post"]
    now_eet = datetime.now(EET)
    d_str, t_str = now_eet.strftime("%Y-%m-%d"), now_eet.strftime("%H:%M:%S")

    for m in live:
        mid, minute, is_ht, is_cup = m["id"], m["minute"], m["is_ht"], m["is_cup"]
        h_sc, a_sc = m["h_sc"], m["a_sc"]
        tot_g, g_diff = h_sc + a_sc, abs(h_sc - a_sc)

        try:
            data = requests.get(sum_url.format(m["slug"], mid), timeout=10).json()
        except Exception:
            continue

        st = {"home": {}, "away": {}}
        for tb in data.get("boxscore", {}).get("teams", []):
            side = tb.get("homeAway", "")
            if side in st:
                p = {i["name"]: float(str(i.get("displayValue", "0")).replace("%", "")) for i in tb.get("statistics", []) if str(i.get("displayValue", "")).replace("%", "").replace(".", "").isdigit()}
                st[side] = {
                    "shots": int(p.get("totalShots", 0)), "sot": int(p.get("shotsOnTarget", 0)),
                    "corners": int(p.get("wonCorners", 0)), "poss": p.get("possessionPct", 50.0),
                    "fouls": int(p.get("foulsCommitted", 0)), "yc": int(p.get("yellowCards", 0)),
                    "rc": int(p.get("redCards", 0)),
                }
        h, a = st["home"], st["away"]
        if not h or not a:
            continue

        tot_sh, tot_sot, tot_c = h["shots"] + a["shots"], h["sot"] + a["sot"], h["corners"] + a["corners"]
        tot_f, tot_yc, tot_rc = h["fouls"] + a["fouls"], h["yc"] + a["yc"], h["rc"] + a["rc"]
        cup_sh, cup_sot = (2 if is_cup else 0), (1 if is_cup else 0)
        cup_tag = " [CUP]" if is_cup else ""
        tlbl = "HT" if is_ht else f"{minute}'"
        mlbl = f"{m['home']} {h_sc}-{a_sc} {m['away']} ({tlbl})"
        snap = f"Sh {h['shots']}-{a['shots']} | SOT {h['sot']}-{a['sot']} | Corn {h['corners']}-{a['corners']} | Poss {h['poss']:.0f}%-{a['poss']:.0f}%"

        # CORE 1: 60' Siege Next Goal
        if not is_ht and 58 <= minute <= 70 and g_diff <= 1 and tot_g <= 2 and tot_sh >= (18 + cup_sh) and tot_sot >= (7 + cup_sot) and tot_c >= 8 and f"{mid}_SIEGE" not in fb_sent:
            fb_sent.add(f"{mid}_SIEGE")
            nl = round(tot_g + 0.5, 1)
            send_tg(FB_TOKEN, f"⚽🚨 <b>[CORE] 60' SIEGE NEXT GOAL{cup_tag}</b>\n🏆 <b>{m['comp']}</b> | {mlbl}\n• {snap}\n• <b>Safe Target (1.50+):</b> <b>Over {nl} Match Goals</b>")
            log_fb("Football_Core", {"Date": d_str, "Time_EET": t_str, "Event_ID": mid, "Competition": m["comp"], "Match": mlbl, "Minute": tlbl, "Trigger": "60' Siege", "Safe_Target_150": f"O {nl}", "Cutoff_Line": f"O {nl}", "Why": f"{tot_sh} sh, {tot_sot} SOT, {tot_c} corn", "Live_Stats": snap})

        # CORE 2: Dominant Team Trailing
        if not is_ht and 46 <= minute <= 62 and g_diff == 1 and tot_rc == 0 and f"{mid}_DOM" not in fb_sent:
            dom, dc = (m["home"], "1X") if (h_sc < a_sc and h["poss"] >= 62 and h["shots"] >= max(10, a["shots"] * 2.2) and h["sot"] >= 4 + cup_sot) else ((m["away"], "X2") if (a_sc < h_sc and a["poss"] >= 62 and a["shots"] >= max(10, h["shots"] * 2.2) and a["sot"] >= 4 + cup_sot) else (None, None))
            if dom:
                fb_sent.add(f"{mid}_DOM")
                send_tg(FB_TOKEN, f"⚽🚨 <b>[CORE] DOMINANT TRAILING{cup_tag}</b>\n🏆 <b>{m['comp']}</b> | {mlbl}\n• {snap}\n• <b>Safe Target (1.50+):</b> <b>{dom} ({dc})</b> or <b>{dom} O 0.5</b>")
                log_fb("Football_Core", {"Date": d_str, "Time_EET": t_str, "Event_ID": mid, "Competition": m["comp"], "Match": mlbl, "Minute": tlbl, "Trigger": "Dominant Trailing", "Safe_Target_150": dc, "Cutoff_Line": f"{dom} O 0.5", "Why": f"{dom} trailing despite dominance", "Live_Stats": snap})

        # CORE 3: BTTS
        if not is_ht and 52 <= minute <= 65 and tot_g == 1 and tot_rc == 0 and f"{mid}_BTTS" not in fb_sent:
            if (h_sc == 0 and h["shots"] >= 7 and h["sot"] >= 3 and h["poss"] >= 46) or (a_sc == 0 and a["shots"] >= 7 and a["sot"] >= 3 and a["poss"] >= 46):
                fb_sent.add(f"{mid}_BTTS")
                send_tg(FB_TOKEN, f"⚽🚨 <b>[CORE] BOTH TEAMS LIVE (BTTS){cup_tag}</b>\n🏆 <b>{m['comp']}</b> | {mlbl}\n• {snap}\n• <b>Safe Target (1.50+):</b> <b>BTTS — Yes</b>")
                log_fb("Football_Core", {"Date": d_str, "Time_EET": t_str, "Event_ID": mid, "Competition": m["comp"], "Match": mlbl, "Minute": tlbl, "Trigger": "BTTS Live", "Safe_Target_150": "BTTS Yes", "Cutoff_Line": "BTTS Yes", "Why": "Trailing team 7+ sh & 3+ SOT", "Live_Stats": snap})

        # CORE 4: 11v10 Man-Advantage
        if not is_ht and 20 <= minute <= 65 and tot_rc == 1 and g_diff <= 1 and f"{mid}_RED" not in fb_sent:
            fb_sent.add(f"{mid}_RED")
            adv = m["home"] if a["rc"] == 1 else m["away"]
            nl = round(tot_g + 0.5, 1)
            send_tg(FB_TOKEN, f"🟥🚨 <b>[CORE] 11v10 ADVANTAGE{cup_tag}</b>\n🏆 <b>{m['comp']}</b> | {mlbl}\n• <b>Safe Target (1.50+):</b> <b>Over {nl} Goals</b> or <b>{adv} DC</b>")
            log_fb("Football_Core", {"Date": d_str, "Time_EET": t_str, "Event_ID": mid, "Competition": m["comp"], "Match": mlbl, "Minute": tlbl, "Trigger": "11v10 Advantage", "Safe_Target_150": f"O {nl}", "Cutoff_Line": f"O {nl}", "Why": f"{adv} 11v10", "Live_Stats": snap})

        # CORE 5: Dead Rubber Under
        if not is_ht and 55 <= minute <= 66 and tot_g <= 1 and tot_rc == 0 and tot_sh <= 6 and tot_sot <= 1 and tot_c <= 5 and f"{mid}_UNDER" not in fb_sent:
            fb_sent.add(f"{mid}_UNDER")
            ul = round(tot_g + 1.5, 1)
            send_tg(FB_TOKEN, f"⚽🚨 <b>[CORE] DEAD RUBBER UNDER{cup_tag}</b>\n🏆 <b>{m['comp']}</b> | {mlbl}\n• {snap}\n• <b>Safe Target (1.50+):</b> <b>Under {ul} Match Goals</b>")
            log_fb("Football_Core", {"Date": d_str, "Time_EET": t_str, "Event_ID": mid, "Competition": m["comp"], "Match": mlbl, "Minute": tlbl, "Trigger": "Dead Rubber Under", "Safe_Target_150": f"U {ul}", "Cutoff_Line": f"U {ul}", "Why": f"Only {tot_sh} sh & {tot_sot} SOT", "Live_Stats": snap})

        # CORE 6: One-Way Clean Sheet
        if not is_ht and 58 <= minute <= 68 and g_diff in (1, 2) and tot_rc == 0 and f"{mid}_CS" not in fb_sent:
            dog = f"{m['away']} Under 0.5 Goals" if (h_sc > a_sc and h["poss"] >= 65 and a["sot"] == 0 and a["shots"] <= 2) else (f"{m['home']} Under 0.5 Goals" if (a_sc > h_sc and a["poss"] >= 65 and h["sot"] == 0 and h["shots"] <= 2) else None)
            if dog:
                fb_sent.add(f"{mid}_CS")
                send_tg(FB_TOKEN, f"🛡️🚨 <b>[CORE] ONE-WAY CLEAN SHEET{cup_tag}</b>\n🏆 <b>{m['comp']}</b> | {mlbl}\n• {snap}\n• <b>Safe Target (1.50+):</b> <b>{dog}</b>")
                log_fb("Football_Core", {"Date": d_str, "Time_EET": t_str, "Event_ID": mid, "Competition": m["comp"], "Match": mlbl, "Minute": tlbl, "Trigger": "One-Way Clean Sheet", "Safe_Target_150": dog, "Cutoff_Line": dog, "Why": "65%+ poss & 0 SOT allowed", "Live_Stats": snap})

        # WILDCARD 1: Card Escalation
        officials = data.get("gameInfo", {}).get("officials", [])
        ref = officials[0].get("displayName", "") if officials else ""
        cthresh = 3 if any(r in ref.lower() for r in STRICT_REFS) else 4
        if not is_ht and 45 <= minute <= 62 and g_diff <= 1 and tot_yc >= cthresh and tot_f >= 16 and f"{mid}_YC" not in fb_sent:
            fb_sent.add(f"{mid}_YC")
            cl = round(tot_yc + 2.5, 1)
            send_tg(FB_TOKEN, f"🟨🎯 <b>[WILDCARD] CARD ESCALATION{cup_tag}</b>\n🏆 <b>{m['comp']}</b> | {mlbl}\n• {tot_yc} YC, {tot_f} Fouls (Ref: {ref or 'N/A'})\n• <b>Target:</b> <b>Over {cl} Cards</b>")
            log_fb("Football_Wildcards", {"Date": d_str, "Time_EET": t_str, "Event_ID": mid, "Competition": m["comp"], "Match": mlbl, "Minute": tlbl, "Trigger": "Card Escalation", "Safe_Target_150": f"O {cl} Cards", "Cutoff_Line": f"O {cl} Cards", "Why": f"{tot_yc} YC, {tot_f} fouls", "Live_Stats": snap})

        # WILDCARDS 2 & 3: Card Magnet & Brace Hunter
        for rb in data.get("rosters", []):
            side = rb.get("homeAway", "")
            opp_poss = a["poss"] if side == "home" else h["poss"]
            tname = rb.get("team", {}).get("shortDisplayName", "")
            for p in rb.get("roster", []):
                if (not p.get("starter") and not p.get("subbedIn")) or p.get("subbedOut"):
                    continue
                ps = {s.get("name", ""): float(s.get("displayValue", 0)) for s in p.get("stats", []) if str(s.get("displayValue", "")).replace(".", "").isdigit()}
                pname = p.get("athlete", {}).get("displayName", "")
                if not is_ht and 35 <= minute <= 65 and g_diff <= 2 and ps.get("yellowCards", 0) == 0 and ps.get("foulsCommitted", 0) >= 3 and opp_poss >= 57 and f"{mid}_CM_{pname}" not in fb_sent:
                    fb_sent.add(f"{mid}_CM_{pname}")
                    send_tg(FB_TOKEN, f"🟨🎯 <b>[WILDCARD] CARD MAGNET: {pname} ({tname})</b>\n🏆 {m['comp']} | {mlbl}\n• {int(ps['foulsCommitted'])} Fouls, 0 YC vs {opp_poss:.0f}% poss")
                    log_fb("Football_Wildcards", {"Date": d_str, "Time_EET": t_str, "Event_ID": mid, "Competition": m["comp"], "Match": mlbl, "Minute": tlbl, "Trigger": "Card Magnet", "Safe_Target_150": f"{pname} Card", "Cutoff_Line": "Booked", "Why": f"{int(ps['foulsCommitted'])} fouls", "Live_Stats": snap})
                if not is_ht and 30 <= minute <= 62 and g_diff <= 1 and ps.get("totalGoals", 0) == 1 and ps.get("shotsOnTarget", 0) >= 3 and f"{mid}_BH_{pname}" not in fb_sent:
                    fb_sent.add(f"{mid}_BH_{pname}")
                    send_tg(FB_TOKEN, f"🔥🎯 <b>[WILDCARD] BRACE HUNTER: {pname} ({tname})</b>\n🏆 {m['comp']} | {mlbl}\n• 1 Goal + {int(ps['shotsOnTarget'])} SOT by {minute}'")
                    log_fb("Football_Wildcards", {"Date": d_str, "Time_EET": t_str, "Event_ID": mid, "Competition": m["comp"], "Match": mlbl, "Minute": tlbl, "Trigger": "Brace Hunter", "Safe_Target_150": f"{pname} 2+ Goals", "Cutoff_Line": "2+ Goals", "Why": "1G + 3+ SOT", "Live_Stats": snap})

    for m in finished:
        mid = m["id"]
        if mid in fb_graded:
            continue
        fb_graded.add(mid)
        for pick in fb_picks:
            if pick["Event_ID"] == mid and not pick["Result"]:
                res = "WIN" if (("O " in pick["Safe_Target_150"] and (m["h_sc"] + m["a_sc"]) > float(re.sub(r"[^0-9.]", "", pick["Safe_Target_150"]))) or ("U " in pick["Safe_Target_150"] and (m["h_sc"] + m["a_sc"]) < float(re.sub(r"[^0-9.]", "", pick["Safe_Target_150"]))) or ("BTTS" in pick["Safe_Target_150"] and m["h_sc"] >= 1 and m["a_sc"] >= 1) or ("1X" in pick["Safe_Target_150"] and m["h_sc"] >= m["a_sc"]) or ("X2" in pick["Safe_Target_150"] and m["a_sc"] >= m["h_sc"])) else "REVIEW"
                pick["Result"] = res
                post_sheet({"action": "grade", "tab": pick["tab"], "headers": FB_HEADERS, "Event_ID": mid, "Final_Stats": f"FT: {m['home']} {m['h_sc']}-{m['a_sc']} {m['away']}", "Result": res})

    return len(live)

# ==========================================
# 5. NBA MODULE (v5 CORE + WILDCARD)
# ==========================================
nba_sent, nba_graded, nba_picks = set(), set(), []

def log_nba(tab, row):
    nba_picks.append({**row, "tab": tab, "Result": ""})
    post_sheet({**row, "tab": tab, "headers": NBA_HEADERS, "action": "log"})

def run_nba_cycle():
    sb = requests.get("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard", timeout=10).json()
    live_cnt = 0
    now_eet = datetime.now(EET)
    d_str, t_str = now_eet.strftime("%Y-%m-%d"), now_eet.strftime("%H:%M:%S")

    for ev in sb.get("events", []):
        gid = ev["id"]
        state = ev["status"]["type"]["state"]
        period = ev["status"].get("period", 0)
        clock = ev["status"].get("displayClock", "0:00")
        comp = ev["competitions"][0]
        home = next(c for c in comp["competitors"] if c["homeAway"] == "home")
        away = next(c for c in comp["competitors"] if c["homeAway"] == "away")
        h_sc, a_sc = int(home.get("score", 0)), int(away.get("score", 0))
        h_ab, a_ab = home["team"]["abbreviation"], away["team"]["abbreviation"]
        margin = abs(h_sc - a_sc)
        glbl = f"{a_ab} ({a_sc}) @ {h_ab} ({h_sc}) | Q{period} {clock}"

        if state == "in":
            live_cnt += 1
            data = requests.get(f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={gid}", timeout=10).json()
            injured = {inj["athlete"]["displayName"].lower() for ti in data.get("injuries", []) for inj in ti.get("injuries", []) if inj.get("status", "").lower() in ("out", "doubtful", "questionable")}
            players, totals = [], {"fga": 0, "fgm": 0, "tpm": 0, "tpa": 0, "fta": 0}

            for tb in data.get("boxscore", {}).get("players", []):
                abbr = tb["team"]["abbreviation"]
                sg = tb.get("statistics", [])
                if not sg:
                    continue
                idx = {l: i for i, l in enumerate(sg[0].get("labels", []))}
                for ath in sg[0].get("athletes", []):
                    if ath.get("didNotPlay") or len(ath.get("stats", [])) < len(idx):
                        continue
                    s = ath["stats"]
                    gi = lambda c: int(s[idx[c]]) if s[idx[c]].isdigit() else 0
                    gs = lambda c: tuple(map(int, s[idx[c]].split("-"))) if "-" in s[idx[c]] else (0, 0)
                    fgm, fga = gs("FG")
                    tpm, tpa = gs("3PT")
                    _, fta = gs("FT")
                    totals["fga"] += fga; totals["fgm"] += fgm; totals["tpm"] += tpm; totals["tpa"] += tpa; totals["fta"] += fta
                    players.append({"name": ath["athlete"]["displayName"], "team": abbr, "pos": ath.get("athlete", {}).get("position", {}).get("abbreviation", ""), "starter": ath.get("starter", False), "min": gi("MIN"), "pts": gi("PTS"), "reb": gi("REB"), "pf": gi("PF"), "fgm": fgm, "fga": fga, "tpm": tpm, "tpa": tpa})

            mins_rem = (4 - period) * 12.0 + (int(clock.split(":")[0]) + int(clock.split(":")[1]) / 60.0 if ":" in clock else float(clock) / 60.0) if period <= 4 else 2.5

            if period == 2 and totals["fga"] >= 55 and totals["tpa"] >= 22:
                efg = (totals["fgm"] + 0.5 * totals["tpm"]) / totals["fga"] * 100
                tp_pct = totals["tpm"] / totals["tpa"] * 100
                if efg >= 57.5 and tp_pct >= 45.0 and totals["fta"] <= 16 and f"{gid}_SH" not in nba_sent:
                    nba_sent.add(f"{gid}_SH")
                    pt = round(h_sc + a_sc + mins_rem * 2.20, 1)
                    su, cu = round(pt + 4.5) - 0.5, round(pt + 1.5) - 0.5
                    send_tg(NBA_TOKEN, f"🚨 <b>[HIGH CONFIDENCE] FAKE SHOOTOUT UNDER</b>\n🏀 {glbl}\n• eFG {efg:.1f}% | 3PT {tp_pct:.1f}% | FTA {totals['fta']}\n• <b>Safe Target (1.50+):</b> <b>Under {su}</b> (Cutoff: U {cu})")
                    log_nba("NBA_Core", {"Date": d_str, "Time_EET": t_str, "Event_ID": gid, "Tier": "CORE", "Confidence": "HIGH", "Game": glbl, "Trigger": "Fake Shootout Under", "Target_Stat": "Total", "Projected_Final": pt, "Safe_Target_150": f"U {su}", "Cutoff_Line": f"U {cu}", "Why": f"eFG {efg:.1f}%, 3PT {tp_pct:.1f}%", "Alert_Stats": f"{a_sc}-{h_sc}"})

            if period in (2, 3):
                for p in players:
                    base = get_baseline(p["name"])
                    if not base:
                        continue
                    opp_name = home["team"]["displayName"] if p["team"] == a_ab else away["team"]["displayName"]
                    om = TEAM_MULTS.get(opp_name.lower(), 1.0)
                    cap = min(base["mpg"] + 3.5, 40.0)
                    rem = max(0.0, min(mins_rem, cap - p["min"]))
                    if (period == 2 and p["pf"] >= 3) or (period == 3 and p["pf"] >= 4):
                        rem *= 0.65
                    if period >= 3 and margin >= 16:
                        rem *= 0.55 if p["starter"] else 1.10

                    # Fluke Explosion Fade
                    if f"{gid}_FADE_{p['name']}" not in nba_sent and p["pts"] >= base["ppg"] * 1.45 and p["pts"] >= 16 and p["fga"] >= 6 and (p["fgm"] / p["fga"]) >= 0.72:
                        nba_sent.add(f"{gid}_FADE_{p['name']}")
                        proj = round(p["pts"] + rem * base["pts_pm"] * om, 1)
                        su, cu = round(proj + 3.5) - 0.5, round(proj + 1.5) - 0.5
                        send_tg(NBA_TOKEN, f"🚨 <b>[HIGH CONFIDENCE] FADE: {p['name']} ({p['team']})</b>\n🏀 {glbl}\n• {p['pts']} PTS ({p['fgm']}/{p['fga']} FG) vs {base['ppg']:.1f} PPG\n• <b>Safe Target (1.50+):</b> <b>Under {su} PTS</b> (Cutoff: U {cu})")
                        log_nba("NBA_Core", {"Date": d_str, "Time_EET": t_str, "Event_ID": gid, "Tier": "CORE", "Confidence": "HIGH", "Game": glbl, "Trigger": "Fluke Explosion Fade", "Player": p["name"], "Target_Stat": "PTS Under", "Projected_Final": proj, "Safe_Target_150": f"U {su}", "Cutoff_Line": f"U {cu}", "Why": f"{p['pts']}p hot FG%", "Alert_Stats": f"{p['pts']}p {p['min']}m"})

                    # 4th Foul Q3 Fade
                    if period == 3 and p["pf"] == 4 and base["ppg"] >= 15.0 and f"{gid}_PF4_{p['name']}" not in nba_sent:
                        nba_sent.add(f"{gid}_PF4_{p['name']}")
                        proj = round(p["pts"] + rem * base["pts_pm"] * om, 1)
                        su, cu = round(proj + 3.5) - 0.5, round(proj + 1.5) - 0.5
                        send_tg(NBA_TOKEN, f"⚠️ <b>[HIGH CONFIDENCE] 4TH FOUL FADE: {p['name']} ({p['team']})</b>\n🏀 {glbl}\n• {p['pts']} PTS | 4 PF in Q3\n• <b>Safe Target (1.50+):</b> <b>Under {su} PTS</b> (Cutoff: U {cu})")
                        log_nba("NBA_Core", {"Date": d_str, "Time_EET": t_str, "Event_ID": gid, "Tier": "CORE", "Confidence": "HIGH", "Game": glbl, "Trigger": "4th Foul Q3 Fade", "Player": p["name"], "Target_Stat": "PTS Under", "Projected_Final": proj, "Safe_Target_150": f"U {su}", "Cutoff_Line": f"U {cu}", "Why": "4 PF Q3", "Alert_Stats": f"{p['pts']}p 4pf"})

                    if p["name"].lower() in injured:
                        continue

                    # Cold-Star Bounce-Back
                    if period == 2 and p["min"] >= 14 and base["ppg"] >= 22.0 and p["pts"] <= 8 and p["fga"] >= 6 and (p["fgm"] / p["fga"]) <= 0.28 and p["pf"] <= 1 and margin <= 12 and f"{gid}_COLD_{p['name']}" not in nba_sent:
                        nba_sent.add(f"{gid}_COLD_{p['name']}")
                        proj = round(p["pts"] + rem * base["pts_pm"] * om, 1)
                        so, co = int(proj - 2.5), int(proj - 0.5)
                        send_tg(NBA_TOKEN, f"🚨 <b>[HIGH CONFIDENCE] COLD-STAR BOUNCE-BACK: {p['name']} ({p['team']})</b>\n🏀 {glbl}\n• {p['pts']} PTS ({p['fgm']}/{p['fga']} FG) vs {base['ppg']:.1f} PPG\n• <b>Safe Target (1.50+):</b> <b>{so}+ PTS</b> (Cutoff: {co}+)")
                        log_nba("NBA_Core", {"Date": d_str, "Time_EET": t_str, "Event_ID": gid, "Tier": "CORE", "Confidence": "HIGH", "Game": glbl, "Trigger": "Cold-Star Bounce-Back", "Player": p["name"], "Target_Stat": "PTS Over", "Projected_Final": proj, "Safe_Target_150": f"{so}+", "Cutoff_Line": f"{co}+", "Why": "Star cold start", "Alert_Stats": f"{p['pts']}p {p['min']}m"})

                    # Glass Dominance REB
                    if period == 2 and p["pf"] <= 1 and base["rpg"] >= 8.5 and margin <= 10 and p["reb"] >= 8 and f"{gid}_REB_{p['name']}" not in nba_sent:
                        proj = round(p["reb"] + rem * base["reb_pm"] * om, 1)
                        if proj >= 13.0:
                            nba_sent.add(f"{gid}_REB_{p['name']}")
                            so, co = int(proj - 1.5), int(proj - 0.5)
                            send_tg(NBA_TOKEN, f"🚨 <b>[HIGH CONFIDENCE] GLASS DOMINANCE: {p['name']} ({p['team']})</b>\n🏀 {glbl}\n• {p['reb']} REB in {p['min']}m\n• <b>Safe Target (1.50+):</b> <b>{so}+ REB</b> (Cutoff: {co}+)")
                            log_nba("NBA_Core", {"Date": d_str, "Time_EET": t_str, "Event_ID": gid, "Tier": "CORE", "Confidence": "HIGH", "Game": glbl, "Trigger": "Glass Dominance", "Player": p["name"], "Target_Stat": "REB Over", "Projected_Final": proj, "Safe_Target_150": f"{so}+", "Cutoff_Line": f"{co}+", "Why": f"{p['reb']}r in {p['min']}m", "Alert_Stats": f"{p['reb']}r {p['min']}m"})

                    # Wildcard 3PM
                    if period == 2 and p["tpm"] >= 3 and p["tpa"] >= 5 and p["pf"] <= 1 and margin <= 12 and f"{gid}_3PM_{p['name']}" not in nba_sent:
                        proj = round(p["tpm"] + rem * base["tpm_pm"] * om, 1)
                        if proj >= 4.6:
                            nba_sent.add(f"{gid}_3PM_{p['name']}")
                            so = int(proj)
                            send_tg(NBA_TOKEN, f"🎯 <b>[WILDCARD 3PT] {p['name']} ({p['team']})</b>\n🏀 {glbl}\n• {p['tpm']}/{p['tpa']} 3PT | <b>Target: {so}+ 3PM</b>")
                            log_nba("NBA_Wildcards", {"Date": d_str, "Time_EET": t_str, "Event_ID": gid, "Tier": "WILDCARD", "Confidence": "MED-HIGH", "Game": glbl, "Trigger": "Hot-Hand 3PM", "Player": p["name"], "Target_Stat": "3PM Over", "Projected_Final": proj, "Safe_Target_150": f"{so}+", "Cutoff_Line": f"{so+1}+", "Why": f"{p['tpm']}/{p['tpa']} 3PT", "Alert_Stats": f"{p['tpm']}/{p['tpa']} 3PT"})

        elif state == "post" and gid not in nba_graded:
            nba_graded.add(gid)
            for pick in nba_picks:
                if pick["Event_ID"] == gid and not pick["Result"]:
                    post_sheet({"action": "grade", "tab": pick["tab"], "headers": NBA_HEADERS, "Event_ID": gid, "Final_Stats": f"FINAL: {a_ab} {a_sc} - {h_ab} {h_sc}", "Result": "LOGGED"})

    return live_cnt

# ==========================================
# 6. UNIFIED SHIFT RUNNER (AUTO-HANDOFF)
# ==========================================
start_ts = time.time()
send_tg(FB_TOKEN, "🚀 <b>Shift Co-Pilot Online (Cloud)</b>\nScanning Football now, then auto-switching to NBA at 02:00 EET.")

while True:
    if time.time() - start_ts > MAX_RUNTIME_SEC:
        send_tg(FB_TOKEN, "⏱️ <b>Cloud Session Reached 5.8h Limit</b> — Tap iPhone button if late West Coast NBA games are still live.")
        break

    try:
        fb_live = run_football_cycle()
        nba_live = run_nba_cycle()
        now_eet = datetime.now(EET)
        print(f"[{now_eet.strftime('%H:%M:%S')} EET] FB Live: {fb_live} | NBA Live: {nba_live}")

        # Auto-shutdown after 04:30 EET once both Football and NBA slates finish
        if now_eet.hour >= 4 and now_eet.minute >= 30 and fb_live == 0 and nba_live == 0:
            send_tg(NBA_TOKEN, "✅ <b>All Football & NBA Games Finished — Cloud Runner Shutting Down</b>")
            break
    except Exception as e:
        print(f"Loop error: {e}")

    time.sleep(SCAN_INTERVAL_SEC)
    except Exception as e:
        print(f"Loop error: {e}")

    time.sleep(SCAN_INTERVAL_SEC)
