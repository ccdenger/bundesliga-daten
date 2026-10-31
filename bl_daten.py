#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bundesliga-Datenbriefing aus OpenLigaDB.

Liefert alle harten Fakten fuer die Spieltags-Prognose:
Ansetzungen, Tabelle, Formkurven, Heim-/Auswaertsbilanz, direkte Duelle.

Aufrufe:
  py bl_daten.py            -> Briefing fuer den naechsten Spieltag
  py bl_daten.py --check    -> RUN / SKIP: startet der naechste Spieltag in 2 Tagen?
  py bl_daten.py --tag 5    -> Briefing fuer einen bestimmten Spieltag
"""
import json
import sys
import io
import urllib.request
from datetime import datetime, date

try:
    # Nicht sys.stdout neu umhuellen: Importiert ein zweites Modul dieselbe Zeile,
    # schliesst der erste Wrapper beim Aufraeumen den Puffer.
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

API = "https://api.openligadb.de"
LIGA = "bl1"
VORLAUF_TAGE = 2          # so viele Tage vor Anpfiff soll die Prognose entstehen
FORM_SPIELE = 5           # Laenge der Formkurve


def hole(pfad):
    req = urllib.request.Request(API + pfad, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode("utf-8"))


def saison_jahr(heute=None):
    """Bundesliga-Saison laeuft Aug-Mai: ab Juli zaehlt das laufende Jahr."""
    heute = heute or date.today()
    return heute.year if heute.month >= 7 else heute.year - 1


def endstand(match):
    for r in (match.get("matchResults") or []):
        if r.get("resultTypeID") == 2:
            return r["pointsTeam1"], r["pointsTeam2"]
    rs = sorted(match.get("matchResults") or [], key=lambda x: x.get("resultOrderID", 0))
    if rs:
        return rs[-1]["pointsTeam1"], rs[-1]["pointsTeam2"]
    return None


def anpfiff(match):
    return datetime.strptime(match["matchDateTime"][:19], "%Y-%m-%dT%H:%M:%S")


def spieltag_nr(match):
    return (match.get("group") or {}).get("groupOrderID")


def naechster_spieltag(alle, jetzt=None):
    """Kleinster Spieltag, dessen erster Anpfiff noch in der Zukunft liegt.

    Bewusst nicht "erster Spieltag mit offenem Spiel": In engen englischen Wochen
    laeuft der alte Spieltag noch, waehrend der naechste schon in zwei Tagen
    beginnt - sonst faellt genau diese Prognose aus.
    """
    jetzt = jetzt or datetime.now()
    start = {}
    for m in alle:
        st = spieltag_nr(m)
        if st is None:
            continue
        a = anpfiff(m)
        if st not in start or a < start[st]:
            start[st] = a
    kuenftig = [st for st, a in start.items() if a > jetzt]
    return min(kuenftig) if kuenftig else None


KURZ = {
    "FC Bayern München": "Bayern",
    "Borussia Dortmund": "Dortmund",
    "Borussia Mönchengladbach": "Gladbach",
    "1. FSV Mainz 05": "Mainz",
    "1. FC Union Berlin": "Union Berlin",
    "Bayer 04 Leverkusen": "Leverkusen",
    "SV 07 Elversberg": "Elversberg",
    "SC Paderborn 07": "Paderborn",
    "TSG Hoffenheim": "Hoffenheim",
    "SV Werder Bremen": "Werder Bremen",
    "Hamburger SV": "HSV",
    "1. FC Köln": "Köln",
    "Eintracht Frankfurt": "Frankfurt",
    "FC Schalke 04": "Schalke",
    "FC Augsburg": "Augsburg",
    "RB Leipzig": "Leipzig",
    "SC Freiburg": "Freiburg",
    "VfB Stuttgart": "Stuttgart",
}


def kurz(name):
    return KURZ.get(name, name)


def formkurve(team_id, alle, vor_spieltag, n=FORM_SPIELE):
    """Die letzten n beendeten Spiele eines Teams vor dem gegebenen Spieltag."""
    rel = []
    for m in alle:
        if not m.get("matchIsFinished"):
            continue
        st = spieltag_nr(m)
        if st is None or st >= vor_spieltag:
            continue
        if team_id not in (m["team1"]["teamId"], m["team2"]["teamId"]):
            continue
        rel.append(m)
    rel.sort(key=anpfiff)
    aus = []
    for m in rel[-n:]:
        e = endstand(m)
        if not e:
            continue
        heim = m["team1"]["teamId"] == team_id
        eigene, fremde = (e[0], e[1]) if heim else (e[1], e[0])
        zeichen = "S" if eigene > fremde else ("U" if eigene == fremde else "N")
        gegner = kurz(m["team2"]["teamName"] if heim else m["team1"]["teamName"])
        aus.append({
            "zeichen": zeichen,
            "ort": "H" if heim else "A",
            "gegner": gegner,
            "ergebnis": "%d:%d" % (eigene, fremde),
        })
    return aus


def duelle(id_a, id_b, historie):
    """Direkte Duelle aus der mitgelieferten Vorsaison."""
    aus = []
    for m in historie:
        if not m.get("matchIsFinished"):
            continue
        if {m["team1"]["teamId"], m["team2"]["teamId"]} != {id_a, id_b}:
            continue
        e = endstand(m)
        if not e:
            continue
        aus.append("%s %s %d:%d %s" % (
            m["matchDateTime"][:10], kurz(m["team1"]["teamName"]),
            e[0], e[1], kurz(m["team2"]["teamName"])))
    return sorted(aus)[-4:]


def tabellen_index(saison):
    try:
        tab = hole("/getbltable/%s/%d" % (LIGA, saison))
    except Exception:
        return {}
    idx = {}
    for platz, t in enumerate(tab, start=1):
        idx[t.get("teamInfoId") or t.get("teamId")] = {
            "platz": platz,
            "punkte": t.get("points"),
            "spiele": t.get("matches"),
            "tore": t.get("goals"),
            "gegentore": t.get("opponentGoals"),
            "diff": t.get("goalDiff"),
        }
    return idx


def heim_auswaerts(team_id, alle, vor_spieltag, heim):
    s = u = n = tore = gegen = 0
    for m in alle:
        if not m.get("matchIsFinished"):
            continue
        st = spieltag_nr(m)
        if st is None or st >= vor_spieltag:
            continue
        if team_id not in (m["team1"]["teamId"], m["team2"]["teamId"]):
            continue
        ist_heim = m["team1"]["teamId"] == team_id
        if ist_heim != heim:
            continue
        e = endstand(m)
        if not e:
            continue
        eigene, fremde = (e[0], e[1]) if ist_heim else (e[1], e[0])
        tore += eigene
        gegen += fremde
        if eigene > fremde:
            s += 1
        elif eigene == fremde:
            u += 1
        else:
            n += 1
    return "%dS %dU %dN, %d:%d Tore" % (s, u, n, tore, gegen)


def briefing(ziel_spieltag=None):
    saison = saison_jahr()
    alle = hole("/getmatchdata/%s/%d" % (LIGA, saison))
    st = ziel_spieltag or naechster_spieltag(alle)
    if st is None:
        print("Kein offener Spieltag gefunden - Saison beendet?")
        return
    partien = sorted([m for m in alle if spieltag_nr(m) == st], key=anpfiff)
    tab = tabellen_index(saison)
    try:
        historie = hole("/getmatchdata/%s/%d" % (LIGA, saison - 1))
    except Exception:
        historie = []

    namen = {}
    for m in alle:
        namen[m["team1"]["teamId"]] = kurz(m["team1"]["teamName"])
        namen[m["team2"]["teamId"]] = kurz(m["team2"]["teamName"])

    erster = anpfiff(partien[0])
    print("# DATENBRIEFING BUNDESLIGA - %d. SPIELTAG (Saison %d/%d)" % (st, saison, saison + 1))
    print("Erstellt: %s | Anpfiff erstes Spiel: %s" % (
        datetime.now().strftime("%d.%m.%Y %H:%M"),
        erster.strftime("%d.%m.%Y %H:%M")))
    print()

    if tab:
        print("## TABELLE VOR DEM SPIELTAG")
        for tid, d in sorted(tab.items(), key=lambda kv: kv[1]["platz"]):
            print("%2d. %-16s %2d Sp | %2d Pkt | %2d:%-2d (%+d)" % (
                d["platz"], namen.get(tid, tid), d["spiele"] or 0, d["punkte"] or 0,
                d["tore"] or 0, d["gegentore"] or 0, d["diff"] or 0))
        print()

    print("## DIE %d PARTIEN IM DETAIL" % len(partien))
    for m in partien:
        h, a = m["team1"], m["team2"]
        print()
        print("### %s vs. %s" % (kurz(h["teamName"]), kurz(a["teamName"])))
        print("Anstoss: %s" % anpfiff(m).strftime("%d.%m.%Y %H:%M"))
        for team, rolle in ((h, "HEIM"), (a, "AUSWAERTS")):
            d = tab.get(team["teamId"], {})
            pos = ("Platz %s, %s Pkt, %s:%s Tore" % (
                d.get("platz"), d.get("punkte"), d.get("tore"), d.get("gegentore"))
                if d else "keine Tabellendaten")
            print("- %s %s: %s" % (rolle, kurz(team["teamName"]), pos))
            f = formkurve(team["teamId"], alle, st)
            if f:
                kette = " ".join(x["zeichen"] for x in f)
                detail = ", ".join("%s %s %s" % (x["ort"], x["gegner"], x["ergebnis"]) for x in f)
                print("  Form (alt->neu): %s  |  %s" % (kette, detail))
            else:
                print("  Form: noch keine Spiele in dieser Saison")
            print("  %s-Bilanz Saison: %s" % (
                "Heim" if rolle == "HEIM" else "Auswaerts",
                heim_auswaerts(team["teamId"], alle, st, rolle == "HEIM")))
        d2 = duelle(h["teamId"], a["teamId"], historie)
        print("- Direkte Duelle (Vorsaison): %s" % (
            "; ".join(d2) if d2 else "keine (Auf-/Absteiger oder kein Duell)"))
    print()
    print("## RECHERCHE-AUFTRAG")
    print("Fuer jede Partie zusaetzlich im Netz pruefen: Verletzte und Gesperrte, Trainer-Aussagen")
    print("aus der Pressekonferenz, Transfer- und Wechselnews, Belastung durch Pokal oder")
    print("Europapokal, Stimmung und Personalien im Verein.")


def check():
    """Fuer den taeglichen Lauf: Prognose faellig oder nicht?"""
    saison = saison_jahr()
    try:
        alle = hole("/getmatchdata/%s/%d" % (LIGA, saison))
    except Exception as e:
        print("FEHLER %s" % e)
        return 2
    st = naechster_spieltag(alle)
    if st is None:
        print("SKIP kein offener Spieltag (Saisonpause)")
        return 1
    partien = sorted([m for m in alle if spieltag_nr(m) == st], key=anpfiff)
    erster = anpfiff(partien[0]).date()
    diff = (erster - date.today()).days
    if diff == VORLAUF_TAGE:
        print("RUN Spieltag %d startet %s (in %d Tagen)" % (
            st, erster.strftime("%d.%m.%Y"), diff))
        return 0
    print("SKIP naechster Spieltag %d startet %s (in %d Tagen, noetig: %d)" % (
        st, erster.strftime("%d.%m.%Y"), diff, VORLAUF_TAGE))
    return 1


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(check())
    tag = None
    if "--tag" in sys.argv:
        tag = int(sys.argv[sys.argv.index("--tag") + 1])
    briefing(tag)
