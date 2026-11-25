"""Inoltro verso altri server (dalla 1.23.0): il gateway lavora come sempre e in piu' da' una copia dei pacchetti
ricevuti a un altro server, dove lo stesso programma li converte per il suo TAK Server (solo ascolto)."""
import json
import os
import ssl
import time
import urllib.request

import pytest

import subprocess
import sys

from conftest import AMBIENTE, porta_libera, scrivi_config
from test_integrazione import PONTE, avvia_ponte, nodo_finto, per_uid   # noqa: F401
import mesh2tak as m


def aspetta(cosa, limite=30.0):
    fine = time.time() + limite
    while time.time() < fine:
        v = cosa()
        if v:
            return v
        time.sleep(0.2)
    return cosa()


def chiudi(*processi):
    for p in processi:
        p.terminate()
    return [p.communicate(timeout=15)[0] for p in processi]


def leggi(url):
    apri = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with apri.open(url, timeout=10) as r:
        return json.loads(r.read().decode())


def chiave_ssh(cartella):
    """Una chiave del gateway (solo il file: nelle prove nessun tunnel parte davvero)."""
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(cartella / "chiave")], check=True)
    return cartella / "chiave"


def test_il_server_converte_quello_che_il_gateway_inoltra(pki, tmp_path, tak_finto, nodo_finto):
    tak_di_sempre, tak_altro = tak_finto(), tak_finto()
    porta_inoltro, porta_pagina = porta_libera(), porta_libera()
    (tmp_path / "gw").mkdir()
    (tmp_path / "srv").mkdir()
    conf_gw = scrivi_config(tmp_path / "gw" / "c.ini", pki, tak_di_sempre.porta, nodo_finto, tmp_path / "diz",
                            accesso_esterno__nome="casa", accesso_esterno__rete="https://127.0.0.1:9",
                            accesso_esterno__chiave=chiave_ssh(tmp_path / "gw"),
                            inoltro__reti="https://altro.invalid", inoltro__porta=porta_inoltro)
    conf_srv = scrivi_config(tmp_path / "srv" / "c.ini", pki, tak_altro.porta, 0, tmp_path / "diz",
                             meshtastic__collegamento=f"inoltro:http://127.0.0.1:{porta_inoltro}",
                             gestione__attiva="si", gestione__porta_pagina=porta_pagina,
                             gestione__indirizzo="127.0.0.1", gestione__porta_app=4403,
                             gestione__certificato=pki / "takserver-chain.pem", gestione__chiave=pki / "takserver.key")
    # il server parte prima che il gateway sia pronto: lo aspetta da solo (nelle prove riprova piu' spesso)
    srv = subprocess.Popen([sys.executable, PONTE, "-c", str(conf_srv)], env=dict(AMBIENTE, MESH2TAK_ATTESE="0.1"),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(1.5)
    gw = avvia_ponte(conf_gw)
    try:
        def radio(tak):
            return {u for u, ev in per_uid(tak.eventi()).items() if u.startswith("!") and
                    any(e["type"].startswith("a-f-G") and e["type"] != "a-f-G-I" for e in ev)}
        assert aspetta(lambda: len(radio(tak_di_sempre)) >= 2, 60), tak_di_sempre.eventi()
        assert aspetta(lambda: radio(tak_altro) >= radio(tak_di_sempre), 60), (radio(tak_altro), radio(tak_di_sempre))
        # la stessa conversione: stesso tipo e stessa posizione per ogni radio
        a, b = per_uid(tak_di_sempre.eventi()), per_uid(tak_altro.eventi())
        for uid in radio(tak_di_sempre):
            ea, eb = a[uid][-1], b[uid][-1]
            assert (ea["type"], ea["lat"], ea["lon"]) == (eb["type"], eb["lat"], eb["lon"]), uid
        # sul server: solo ascolto, niente gestione delle radio, niente dati del Raspberry
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        apri = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx))
        with apri.open(f"https://127.0.0.1:{porta_pagina}/mesh2tak/stato.json", timeout=10) as r:
            stato = json.loads(r.read().decode())
        assert stato["ascolto"] is True and stato["sistema"] is None and stato["gestione"]["radio"] is False
        assert stato["nodo"]["collegato"] is True and stato["impostazioni"]["invio"] is False
        assert stato["inoltro"] is None
        # dalla porta dell'inoltro non esce altro che i pacchetti: niente pagina, niente comandi
        d = leggi(f"http://127.0.0.1:{porta_inoltro}/inoltro/descrizione.json")
        assert d["nodo"]["canali"] and "psk" not in json.dumps(d).lower()
        with pytest.raises(urllib.error.HTTPError):
            leggi(f"http://127.0.0.1:{porta_inoltro}/mesh2tak/stato.json")
    except AssertionError:
        print("\n".join(x[-6000:] for x in chiudi(gw, srv)))
        raise
    finally:
        uscite = chiudi(gw, srv)
    assert "In attesa del gateway" in uscite[1]
    assert "Inoltro: una copia dei pacchetti" in uscite[0], uscite[0][-3000:]
    assert "Traceback" not in uscite[0] and "Traceback" not in uscite[1], uscite[1][-3000:]


def test_senza_inoltro_la_porta_non_esiste(pki, tmp_path, tak_finto, nodo_finto):
    """Spento finche' non lo si accende: senza [inoltro] nessuna porta in piu' e lo stato lo dice."""
    tak = tak_finto()
    conf = scrivi_config(tmp_path / "c.ini", pki, tak.porta, nodo_finto, tmp_path / "diz")
    cfg = m.carica_config(str(conf))
    assert cfg.inoltro_reti == () and cfg.inoltro_errore == "" and cfg.gestione_indirizzo == "0.0.0.0"
    p = avvia_ponte(conf)
    try:
        assert aspetta(lambda: tak.eventi(), 30)
        import socket
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", cfg.inoltro_porta), timeout=1).close()
    finally:
        uscita = chiudi(p)[0]
    assert "Inoltro" not in uscita


def test_configurazione_sbagliata_spegne_solo_l_inoltro(pki, tmp_path):
    conf = scrivi_config(tmp_path / "c.ini", pki, 1, 1, tmp_path / "diz", inoltro__reti="http://non-cifrato.it")
    cfg = m.carica_config(str(conf))
    assert cfg.inoltro_reti == () and "https://" in cfg.inoltro_errore
    conf = scrivi_config(tmp_path / "c.ini", pki, 1, 1, tmp_path / "diz", inoltro__reti="https://altro.it",
                         accesso_esterno__nome="casa")
    cfg = m.carica_config(str(conf))
    assert cfg.inoltro_reti == () and "chiave" in cfg.inoltro_errore


def test_memoria_dell_inoltro():
    class Grezzo:
        class decoded:
            portnum = m.PORTA_POSITION

        def __init__(self, n):
            self.n = n

        def SerializeToString(self):  # noqa: N802
            return b"p%d" % self.n
    ino = m.Inoltro(0)
    ino.MEMORIA = 5
    primo = ino.pacchetti("", -1, 0, -1)
    assert primo["pacchetti"] == [] and primo["avvio"] == ino.avvio
    for n in range(3):
        ino.aggiungi({"raw": Grezzo(n), "decoded": {}})
    amministrazione = Grezzo(9)
    amministrazione.decoded = type("d", (), {"portnum": 6})     # ADMIN_APP: puo' portare la chiave del canale
    ino.aggiungi({"raw": amministrazione, "decoded": {}})
    r = ino.pacchetti(ino.avvio, 0, 0, 0)
    assert [n for n, _ in r["pacchetti"]] == [1, 2, 3] and r["ultimo"] == 3 and r["persi"] == 0
    for n in range(3, 10):
        ino.aggiungi({"raw": Grezzo(n), "decoded": {}})
    r = ino.pacchetti(ino.avvio, 3, 0, 0)
    assert [n for n, _ in r["pacchetti"]] == [6, 7, 8, 9, 10] and r["persi"] == 2
    # gateway riavviato (altro 'avvio'): chi legge riparte da adesso, senza pacchetti vecchi
    assert ino.pacchetti("vecchio", 3, 0, 0)["pacchetti"] == []
    # niente di nuovo: aspetta al massimo quanto chiesto
    t = time.time()
    assert ino.pacchetti(ino.avvio, 10, 0.3, 0)["pacchetti"] == [] and 0.25 < time.time() - t < 2
    ino.descrivi({"num": 1})
    assert ino.pacchetti(ino.avvio, 10, 5, 0)["descrizione"] == 1    # torna subito: il nodo e' cambiato
