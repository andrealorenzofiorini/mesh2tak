#!/usr/bin/env python3
"""Genera i file da consegnare:
  dist/installa-ponte-meshtastic.sh   (Raspberry: incorpora mesh2tak.py, scorta.py e aggiorna.py)
  dist/rete-meshtastic.sh             (server infra-TAK: incorpora server/rete_meshtastic.py e la pagina)
  dist/mesh2tak-primo-avvio.py        (va nell'immagine per Raspberry Pi Imager: immagine/costruisci.py)
e prima monta dai loro pezzi mesh2tak.py (sorgenti/gateway), server/rete_meshtastic.py (server/sorgenti) e
server/pagina/index.html (server/pagina/sorgenti)."""
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


def monta_pagina():
    """La pagina della rete e' scritta a pezzi (server/pagina/sorgenti: pagina.html, stile.css e js/NN-*.js in ordine)
    e qui diventa il solo file che il server pubblica, server/pagina/index.html (non sta nel repository)."""
    d = os.path.join(QUI, "server", "pagina", "sorgenti")
    pezzo = lambda *p: open(os.path.join(d, *p), encoding="utf-8").read()
    script = "".join(pezzo("js", n) for n in sorted(os.listdir(os.path.join(d, "js"))) if n.endswith(".js"))
    pagina = pezzo("pagina.html")
    assert pagina.count("/*@@STILE@@*/\n") == 1 and pagina.count("//@@SCRIPT@@\n") == 1
    pagina = pagina.replace("/*@@STILE@@*/\n", pezzo("stile.css")).replace("//@@SCRIPT@@\n", script)
    dest = os.path.join(QUI, "server", "pagina", "index.html")
    open(dest, "w", encoding="utf-8", newline="\n").write(pagina)
    print(dest, len(pagina), "byte, sha256", hashlib.sha256(pagina.encode()).hexdigest()[:16])


def monta_python(cartella, *dest):
    """I due programmi grandi sono scritti a pezzi (NN-nome.py, in ordine, uno per argomento): incollati uno dopo
    l'altro danno il file solo che si installa. Non sono moduli: un pezzo da solo non si importa."""
    d = os.path.join(QUI, *cartella)
    testo = "".join(open(os.path.join(d, n), encoding="utf-8").read() for n in sorted(os.listdir(d)) if n.endswith(".py"))
    open(os.path.join(QUI, *dest), "w", encoding="utf-8", newline="\n").write(testo)
    print(os.path.join(QUI, *dest), len(testo), "byte, sha256", hashlib.sha256(testo.encode()).hexdigest()[:16])


monta_pagina()
monta_python(("sorgenti", "gateway"), "mesh2tak.py")
monta_python(("server", "sorgenti"), "server", "rete_meshtastic.py")
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
