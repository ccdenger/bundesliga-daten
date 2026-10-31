#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Spiegelt die OpenLigaDB-Daten ins Repository und rechnet die Prognosebasis aus.

Laeuft als GitHub Action. Der Grund fuer diesen Umweg: Die Cloud-Umgebung, in der die
Prognose-Routine arbeitet, blockiert per Egress-Allowlist jede Sportdatenquelle --
erreichbar ist nur GitHub. GitHub Actions laufen dagegen auf GitHubs eigener
Infrastruktur und kommen problemlos an OpenLigaDB heran.

Die Rechenarbeit passiert deshalb hier, wo die Daten sind. Die Routine liest am Ende
nur noch eine fertige Datei.

Schreibt nach daten/:
  bl1_aktuell.json     alle Partien der laufenden Saison
  bl1_vorsaison.json   die Vorsaison, fuer Modellprior und direkte Duelle
  tabelle.json         aktuelle Tabelle
  prognose_basis.json  Modellausgabe fuer den naechsten Spieltag
  stand.json           Zeitstempel und Kurzstatus
"""
import io
import json
import os
import sys
from datetime import datetime

import bl_daten as bd
import modell as mo

ZIEL = "daten"


def schreibe(name, obj):
    pfad = os.path.join(ZIEL, name)
    text = json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=False)
    with io.open(pfad, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print("  %-22s %8d Bytes" % (name, len(text)))
    return pfad


def main():
    if not os.path.isdir(ZIEL):
        os.makedirs(ZIEL)

    saison = bd.saison_jahr()
    print("Saison %d/%d" % (saison, saison + 1))

    aktuell = bd.hole("/getmatchdata/%s/%d" % (bd.LIGA, saison))
    vorsaison = bd.hole("/getmatchdata/%s/%d" % (bd.LIGA, saison - 1))
    try:
        tabelle = bd.hole("/getbltable/%s/%d" % (bd.LIGA, saison))
    except Exception as e:
        print("  Tabelle nicht abrufbar: %s" % e)
        tabelle = []

    gespielt = sum(1 for m in aktuell if m.get("matchIsFinished"))
    print("  %d Partien, davon %d gespielt" % (len(aktuell), gespielt))

    schreibe("bl1_aktuell.json", aktuell)
    schreibe("bl1_vorsaison.json", vorsaison)
    schreibe("tabelle.json", tabelle)

    basis = mo.prognose_basis(aktuell, vorsaison, tabelle)
    if basis:
        schreibe("prognose_basis.json", basis)
        print("  Naechster Spieltag: %d, Anpfiff %s, %d Partien" % (
            basis["spieltag"], basis["erster_anpfiff"], len(basis["partien"])))
    else:
        print("  Kein offener Spieltag - Prognosebasis uebersprungen (Saisonpause?)")

    schreibe("stand.json", {
        "aktualisiert": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "saison": saison,
        "partien_gesamt": len(aktuell),
        "partien_gespielt": gespielt,
        "naechster_spieltag": basis["spieltag"] if basis else None,
        "erster_anpfiff": basis["erster_anpfiff"] if basis else None,
    })
    print("Fertig.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("FEHLER: %s" % e, file=sys.stderr)
        sys.exit(1)
