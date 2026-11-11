#!/usr/bin/env python3
"""Genera i file da consegnare:
  dist/installa-ponte-meshtastic.sh   (Raspberry: incorpora mesh2tak.py, scorta.py e aggiorna.py)
  dist/rete-meshtastic.sh             (server infra-TAK: incorpora server/rete_meshtastic.py e la pagina)
  dist/mesh2tak-primo-avvio.py        (va nell'immagine per Raspberry Pi Imager: immagine/costruisci.py)"""
import base64, hashlib, os, re
QUI = os.path.dirname(os.path.abspath(__file__))


def leggi(*p):
    return open(os.path.join(QUI, *p), encoding="utf-8").read()


def salva(nome, testo):
    dest = os.path.join(QUI, "dist", nome)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    open(dest, "w", encoding="utf-8", newline="\n").write(testo)
    os.chmod(dest, 0o755)
    print(dest, len(testo), "byte, sha256", hashlib.sha256(testo.encode()).hexdigest()[:16])


py = leggi("mesh2tak.py")
versione = re.search(r'^VERSIONE = "([^"]+)"', py, re.M).group(1)
scorta = leggi("scorta.py").replace("@@VERSIONE@@", versione)
aggiorna = leggi("aggiorna.py").replace("@@VERSIONE@@", versione)
assert "\nFINE_MESH2TAK_PY\n" not in py and "\nFINE_SCORTA_PY\n" not in scorta and "@@" not in scorta
assert "\nFINE_AGGIORNA_PY\n" not in aggiorna and "@@" not in aggiorna
salva("installa-ponte-meshtastic.sh",
      leggi("installa.sh.in").replace("@@VERSIONE@@", versione).replace("@@MESH2TAK_PY@@", py.rstrip("\n"))
      .replace("@@SCORTA_PY@@", scorta.rstrip("\n")).replace("@@AGGIORNA_PY@@", aggiorna.rstrip("\n")))

rete = leggi("server", "rete_meshtastic.py")
b64 = lambda nome: base64.b64encode(open(os.path.join(QUI, "server", "pagina", nome), "rb").read()).decode()
rete = (rete.replace('VERSIONE = "@@VERSIONE@@"', f'VERSIONE = "{versione}"')
            .replace('PAGINA_B64 = "@@PAGINA_B64@@"', f'PAGINA_B64 = "{b64("index.html")}"')
            .replace('ICONA_B64 = "@@ICONA_B64@@"', f'ICONA_B64 = "{b64("icona.svg")}"'))
assert "@@" not in rete and "\nFINE_RETE_PY\n" not in rete
salva("rete-meshtastic.sh",
      leggi("server", "rete.sh.in").replace("@@VERSIONE@@", versione).replace("@@RETE_PY@@", rete.rstrip("\n")))

primo = leggi("primo_avvio.py").replace("@@VERSIONE@@", versione)
assert "@@" not in primo
salva("mesh2tak-primo-avvio.py", primo)
