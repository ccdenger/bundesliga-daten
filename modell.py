#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Poisson-Modell fuer die Bundesliga-Spieltagsprognose.

Schaetzt Angriffs- und Abwehrstaerke jedes Teams plus Heimvorteil aus den Ergebnissen
der laufenden Saison und der Vorsaison, und waehlt je Partie das Ergebnis, das den
Erwartungswert der Tippspiel-Punkte maximiert.

Zwei Dinge tragen das Ergebnis:

1. Die Schaetzung laeuft ueber multiplikatives iteratives Skalieren. Fuer dieses
   Modell gibt es geschlossene Koordinatenupdates - jede Runde erhoeht die
   Likelihood garantiert, und es gibt keine Lernrate, die danebenliegen kann.
   Ein Gradientenaufstieg mit fester Schrittweite driftete hier je nach Saison in
   voellig verschiedene Regionen ab.

2. Die Tippauswahl maximiert den Erwartungswert der Punkte, nicht die
   Wahrscheinlichkeit. Bei 4 Punkten fuers exakte Ergebnis, 3 fuer die richtige
   Tordifferenz und 2 fuer die richtige Tendenz lohnt sich ein Tipp, der auch
   knapp daneben noch punktet.

Aufrufe:
  py modell.py                 -> Prognosebasis fuer den naechsten Spieltag als JSON
  py modell.py --tag 9         -> Prognosebasis fuer einen bestimmten Spieltag
  py modell.py --out datei     -> Ausgabe in eine Datei statt auf die Konsole
"""
import json
import math
import sys
import io
from datetime import datetime

import bl_daten as bd

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Abgestimmt ueber zwei Saisons, 339 Partien. Wichtig zur Einordnung: Alle getesteten
# Kombinationen landeten zwischen 1,34 und 1,41 Punkten pro Spiel - die Unterschiede
# sind groesstenteils Rauschen. Ein belastbares Signal gab es nur beim Vorsaison-
# Gewicht: 0,3 schlug 0,8 in nahezu jeder Zeile, die laufende Saison zaehlt also mehr.
# Bei der Schrumpfung wurde bewusst nicht das Maximum genommen, sondern der Wert, der
# auf der laufenden Saison mit ihren drei Aufsteigern am besten lag - dort schuetzt
# Schrumpfung davor, drei Spiele zur Wahrheit zu erklaeren.
VORSAISON_GEWICHT = 0.3   # Vorsaison zaehlt knapp ein Drittel der laufenden
SHRINK = 2.0              # Schrumpfung Richtung Ligadurchschnitt, in Toren
ITERATIONEN = 120         # iteratives Skalieren konvergiert lange vorher
MAX_TORE = 9              # Torematrix 0..8 je Seite
MAX_TIPP = 6              # als Tipp kommen 0..5 Tore je Seite in Frage

PUNKTE_EXAKT = 4
PUNKTE_DIFFERENZ = 3
PUNKTE_TENDENZ = 2


# ---------------------------------------------------------------- Punktesystem

def vorzeichen(x):
    return (x > 0) - (x < 0)


def punkte(tipp_h, tipp_a, real_h, real_a):
    """Kicker-Schema: 4 exakt, 3 richtige Tordifferenz, 2 richtige Tendenz."""
    if tipp_h == real_h and tipp_a == real_a:
        return PUNKTE_EXAKT
    if tipp_h - tipp_a == real_h - real_a:
        return PUNKTE_DIFFERENZ
    if vorzeichen(tipp_h - tipp_a) == vorzeichen(real_h - real_a):
        return PUNKTE_TENDENZ
    return 0


# ---------------------------------------------------------------- Datenaufbereitung

def ergebnisliste(matches):
    """(Spieltag, Heimteam, Auswaertsteam, Tore heim, Tore auswaerts) je beendeter Partie."""
    aus = []
    for m in matches:
        if not m.get("matchIsFinished"):
            continue
        e = bd.endstand(m)
        if not e:
            continue
        aus.append((bd.spieltag_nr(m), m["team1"]["shortName"],
                    m["team2"]["shortName"], e[0], e[1]))
    return aus


# ---------------------------------------------------------------- Schaetzung

class Modell(object):
    """Multiplikatives Poisson-Modell.

        erwartete Tore heim     = basis * angriff[h] * abwehr[a] * heimfaktor
        erwartete Tore auswaerts = basis * angriff[a] * abwehr[h]

    angriff und abwehr sind Faktoren um 1.0 herum: angriff 1.3 heisst dreissig
    Prozent mehr Tore als der Ligadurchschnitt, abwehr 1.3 heisst dreissig Prozent
    mehr Gegentore, also eine schwaechere Abwehr.
    """

    def __init__(self, teams, angriff, abwehr, basis, heimfaktor, n_training):
        self.teams = teams
        self.angriff = angriff
        self.abwehr = abwehr
        self.basis = basis
        self.heimfaktor = heimfaktor
        self.n_training = n_training

    def erwartungswerte(self, heim, auswaerts):
        h = self.teams.get(heim)
        a = self.teams.get(auswaerts)
        ang_h = self.angriff[h] if h is not None else 1.0
        abw_h = self.abwehr[h] if h is not None else 1.0
        ang_a = self.angriff[a] if a is not None else 1.0
        abw_a = self.abwehr[a] if a is not None else 1.0
        return (self.basis * ang_h * abw_a * self.heimfaktor,
                self.basis * ang_a * abw_h)

    def staerken(self):
        """Teams nach Angriffsstaerke, fuer Diagnose und Protokoll."""
        inv = sorted(self.teams.items(), key=lambda kv: kv[1])
        return [(name, round(self.angriff[i], 3), round(self.abwehr[i], 3))
                for name, i in inv]


def schaetze(spiele_aktuell, spiele_vorsaison, vorsaison_gewicht=VORSAISON_GEWICHT,
             bis_spieltag=None, shrink=SHRINK, iterationen=ITERATIONEN):
    """Gewichtete Maximum-Likelihood-Schaetzung per iterativem Skalieren.

    bis_spieltag schliesst die laufende Saison ab diesem Spieltag aus - unverzichtbar
    fuer den Backtest, sonst misst sich das Modell an Daten, die es schon kannte.

    shrink zieht Teams mit wenigen Spielen Richtung Ligadurchschnitt. Das ist genau
    die Situation von Aufsteigern im August: ohne Schrumpfung werden drei Spiele zur
    Wahrheit erklaert.
    """
    namen = sorted({t for r in list(spiele_aktuell) + list(spiele_vorsaison)
                    for t in (r[1], r[2])})
    ix = {t: i for i, t in enumerate(namen)}
    n = len(namen)

    training = [(ix[h], ix[a], yh, ya, vorsaison_gewicht)
                for (_, h, a, yh, ya) in spiele_vorsaison]
    for (st, h, a, yh, ya) in spiele_aktuell:
        if bis_spieltag is not None and st is not None and st >= bis_spieltag:
            continue
        training.append((ix[h], ix[a], yh, ya, 1.0))

    if not training:
        return Modell(ix, [1.0] * n, [1.0] * n, 1.45, 1.25, 0)

    # Feste Groessen: erzielte und kassierte Tore je Team, gewichtet.
    erzielt = [0.0] * n
    kassiert = [0.0] * n
    tore_heim = 0.0
    tore_gesamt = 0.0
    gewicht_gesamt = 0.0
    for (h, a, yh, ya, w) in training:
        erzielt[h] += w * yh
        erzielt[a] += w * ya
        kassiert[a] += w * yh
        kassiert[h] += w * ya
        tore_heim += w * yh
        tore_gesamt += w * (yh + ya)
        gewicht_gesamt += w

    angriff = [1.0] * n
    abwehr = [1.0] * n
    basis = tore_gesamt / (2.0 * gewicht_gesamt)
    heimfaktor = 1.0
    if tore_gesamt > tore_heim > 0:
        heimfaktor = tore_heim / (tore_gesamt - tore_heim)

    for _ in range(iterationen):
        # --- Angriff: erzielte Tore geteilt durch das, was ohne eigene Staerke
        # --- zu erwarten waere. shrink zieht Richtung 1.0.
        nenner = [0.0] * n
        for (h, a, yh, ya, w) in training:
            nenner[h] += w * basis * abwehr[a] * heimfaktor
            nenner[a] += w * basis * abwehr[h]
        for i in range(n):
            if nenner[i] > 0:
                angriff[i] = (erzielt[i] + shrink) / (nenner[i] + shrink)

        # --- Abwehr: dieselbe Rechnung aus Sicht der kassierten Tore.
        nenner = [0.0] * n
        for (h, a, yh, ya, w) in training:
            nenner[a] += w * basis * angriff[h] * heimfaktor
            nenner[h] += w * basis * angriff[a]
        for i in range(n):
            if nenner[i] > 0:
                abwehr[i] = (kassiert[i] + shrink) / (nenner[i] + shrink)

        # --- Heimfaktor aus dem Verhaeltnis von Heim- zu Auswaertstoren.
        erw_heim = sum(w * basis * angriff[h] * abwehr[a] for (h, a, _, _, w) in training)
        if erw_heim > 0:
            heimfaktor = tore_heim / erw_heim

        # --- Gesamtniveau nachziehen, damit die Torsumme stimmt.
        erw_gesamt = sum(w * (basis * angriff[h] * abwehr[a] * heimfaktor
                              + basis * angriff[a] * abwehr[h])
                         for (h, a, _, _, w) in training)
        if erw_gesamt > 0:
            basis *= tore_gesamt / erw_gesamt

        # --- Angriff und Abwehr sind nur bis auf einen gemeinsamen Faktor
        # --- bestimmt. Auf geometrisches Mittel 1 normieren und die Verschiebung
        # --- in basis schieben, sonst driften die Zahlen unlesbar auseinander.
        for werte in (angriff, abwehr):
            log_mittel = sum(math.log(max(v, 1e-9)) for v in werte) / n
            faktor = math.exp(log_mittel)
            if faktor > 0:
                for i in range(n):
                    werte[i] /= faktor
                basis *= faktor

    return Modell(ix, angriff, abwehr, basis, heimfaktor, len(training))


# ---------------------------------------------------------------- Torematrix und Tipp

_FAKULTAET = [math.factorial(k) for k in range(MAX_TORE)]

# Punktetabelle vorberechnen: _PT[tipp_h][tipp_a][real_h][real_a]
_PT = [[[[punkte(th, ta, rh, ra) for ra in range(MAX_TORE)]
         for rh in range(MAX_TORE)]
        for ta in range(MAX_TIPP)]
       for th in range(MAX_TIPP)]


def torematrix(lam_h, lam_a):
    """Wahrscheinlichkeit jedes Ergebnisses unter unabhaengigen Poisson-Verteilungen."""
    p_h = [math.exp(-lam_h) * lam_h ** k / _FAKULTAET[k] for k in range(MAX_TORE)]
    p_a = [math.exp(-lam_a) * lam_a ** k / _FAKULTAET[k] for k in range(MAX_TORE)]
    return [[p_h[i] * p_a[j] for j in range(MAX_TORE)] for i in range(MAX_TORE)]


def tendenzwahrscheinlichkeiten(matrix):
    heim = remis = auswaerts = 0.0
    for rh in range(MAX_TORE):
        for ra in range(MAX_TORE):
            p = matrix[rh][ra]
            if rh > ra:
                heim += p
            elif rh == ra:
                remis += p
            else:
                auswaerts += p
    gesamt = heim + remis + auswaerts
    if gesamt <= 0:
        return 0.0, 0.0, 0.0
    return heim / gesamt, remis / gesamt, auswaerts / gesamt


def bester_tipp(matrix):
    """Das Ergebnis mit dem hoechsten Erwartungswert an Tippspiel-Punkten."""
    bestes = None
    for th in range(MAX_TIPP):
        for ta in range(MAX_TIPP):
            tabelle = _PT[th][ta]
            ev = 0.0
            for rh in range(MAX_TORE):
                zeile_m = matrix[rh]
                zeile_p = tabelle[rh]
                for ra in range(MAX_TORE):
                    ev += zeile_m[ra] * zeile_p[ra]
            if bestes is None or ev > bestes[0]:
                bestes = (ev, th, ta)
    return bestes[1], bestes[2], bestes[0]


def top_ergebnisse(matrix, anzahl=5):
    alle = []
    for rh in range(MAX_TORE):
        for ra in range(MAX_TORE):
            alle.append((matrix[rh][ra], rh, ra))
    alle.sort(reverse=True)
    return [{"ergebnis": "%d:%d" % (rh, ra), "p": round(p, 4)}
            for p, rh, ra in alle[:anzahl]]


def zuversicht(p_heim, p_remis, p_auswaerts):
    """Aus der Wahrscheinlichkeit abgeleitet, nicht aus dem Bauch."""
    fuehrend = max(p_heim, p_remis, p_auswaerts)
    if fuehrend >= 0.55:
        return "hoch"
    if fuehrend >= 0.42:
        return "mittel"
    return "niedrig"


def tendenz_knapp(p_heim, p_remis, p_auswaerts, schwelle=0.08):
    """True, wenn die beiden fuehrenden Tendenzen dicht beieinanderliegen.

    Nur dann darf die redaktionelle Korrektur die Tendenz kippen.
    """
    sortiert = sorted([p_heim, p_remis, p_auswaerts], reverse=True)
    return (sortiert[0] - sortiert[1]) < schwelle


# ---------------------------------------------------------------- Prognosebasis

def prognose_basis(matches_aktuell, matches_vorsaison, tabelle_roh, spieltag=None):
    """Alles, was die Routine fuer einen Spieltag braucht - als serialisierbares dict."""
    spiele_akt = ergebnisliste(matches_aktuell)
    spiele_vor = ergebnisliste(matches_vorsaison)

    st = spieltag or bd.naechster_spieltag(matches_aktuell)
    if st is None:
        return None

    modell = schaetze(spiele_akt, spiele_vor, bis_spieltag=st)

    partien_roh = sorted([m for m in matches_aktuell if bd.spieltag_nr(m) == st],
                         key=bd.anpfiff)
    if not partien_roh:
        return None

    tab = {}
    for platz, t in enumerate(tabelle_roh or [], start=1):
        tid = t.get("teamInfoId") or t.get("teamId")
        tab[tid] = {"platz": platz, "punkte": t.get("points"),
                    "spiele": t.get("matches"), "tore": t.get("goals"),
                    "gegentore": t.get("opponentGoals"), "diff": t.get("goalDiff")}

    partien = []
    for m in partien_roh:
        heim, ausw = m["team1"], m["team2"]
        lam_h, lam_a = modell.erwartungswerte(heim["shortName"], ausw["shortName"])
        matrix = torematrix(lam_h, lam_a)
        p_h, p_x, p_a = tendenzwahrscheinlichkeiten(matrix)
        th, ta, ev = bester_tipp(matrix)

        partien.append({
            "heim": bd.kurz(heim["teamName"]),
            "auswaerts": bd.kurz(ausw["teamName"]),
            "heim_voll": heim["teamName"],
            "auswaerts_voll": ausw["teamName"],
            "heim_kurz_api": heim["shortName"],
            "auswaerts_kurz_api": ausw["shortName"],
            "anpfiff": m["matchDateTime"][:16],
            "lambda_heim": round(lam_h, 3),
            "lambda_auswaerts": round(lam_a, 3),
            "p_heimsieg": round(p_h, 4),
            "p_remis": round(p_x, 4),
            "p_auswaertssieg": round(p_a, 4),
            "modelltipp": {"heim": th, "auswaerts": ta,
                           "text": "%d:%d" % (th, ta),
                           "erwartete_punkte": round(ev, 3)},
            "zuversicht": zuversicht(p_h, p_x, p_a),
            "tendenz_offen": tendenz_knapp(p_h, p_x, p_a),
            "top_ergebnisse": top_ergebnisse(matrix),
            "tabelle": {"heim": tab.get(heim["teamId"], {}),
                        "auswaerts": tab.get(ausw["teamId"], {})},
            "form": {
                "heim": bd.formkurve(heim["teamId"], matches_aktuell, st),
                "auswaerts": bd.formkurve(ausw["teamId"], matches_aktuell, st),
            },
            "heimbilanz": bd.heim_auswaerts(heim["teamId"], matches_aktuell, st, True),
            "auswaertsbilanz": bd.heim_auswaerts(ausw["teamId"], matches_aktuell, st, False),
            "duelle_vorsaison": bd.duelle(heim["teamId"], ausw["teamId"], matches_vorsaison),
        })

    return {
        "erstellt": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "saison": bd.saison_jahr(),
        "spieltag": st,
        "erster_anpfiff": partien[0]["anpfiff"],
        "modell": {
            "heimfaktor": round(modell.heimfaktor, 4),
            "torbasis": round(modell.basis, 4),
            "trainingsspiele": modell.n_training,
            "vorsaison_gewicht": VORSAISON_GEWICHT,
            "schrumpfung": SHRINK,
            "punkteschema": "%d exakt / %d Tordifferenz / %d Tendenz" % (
                PUNKTE_EXAKT, PUNKTE_DIFFERENZ, PUNKTE_TENDENZ),
        },
        "partien": partien,
    }


def basis_von_api(spieltag=None):
    """Prognosebasis direkt von OpenLigaDB - fuer lokale Laeufe."""
    saison = bd.saison_jahr()
    aktuell = bd.hole("/getmatchdata/%s/%d" % (bd.LIGA, saison))
    vorsaison = bd.hole("/getmatchdata/%s/%d" % (bd.LIGA, saison - 1))
    try:
        tabelle = bd.hole("/getbltable/%s/%d" % (bd.LIGA, saison))
    except Exception:
        tabelle = []
    return prognose_basis(aktuell, vorsaison, tabelle, spieltag)


if __name__ == "__main__":
    tag = None
    if "--tag" in sys.argv:
        tag = int(sys.argv[sys.argv.index("--tag") + 1])
    basis = basis_von_api(tag)
    if basis is None:
        print("Kein offener Spieltag gefunden.")
        sys.exit(1)
    text = json.dumps(basis, ensure_ascii=False, indent=2)
    if "--out" in sys.argv:
        ziel = sys.argv[sys.argv.index("--out") + 1]
        with io.open(ziel, "w", encoding="utf-8") as f:
            f.write(text)
        print("Geschrieben: %s (Spieltag %d, %d Partien)" % (
            ziel, basis["spieltag"], len(basis["partien"])))
    else:
        print(text)
