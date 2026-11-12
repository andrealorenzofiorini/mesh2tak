"""Gestione del nodo dal browser o dall'app Meshtastic via Wi-Fi: il ponte va in pausa da solo (1.3.0)."""
import http.server
import os
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from conftest import AMBIENTE, QUI, RADICE, aspetta_porta, porta_libera, scrivi_config
import mesh2tak as m

PONTE = os.path.join(RADICE, "mesh2tak.py")
SENZA_VERIFICA = ssl._create_unverified_context()  # il certificato della pagina e' fatto in casa


def nodo_libero(pausa):
    return None if pausa is None else (pausa.attiva and pausa.rilasciato.is_set())


class PaginaNodoFinta:
    """Il server web di meshtasticd: pagine e API /api/v1/fromradio, /api/v1/toradio."""

    def __init__(self, pki, pausa=None):
        self.porta = porta_libera()
        self.chiamate = []   # (metodo, percorso, corpo, nodo libero dal ponte?)
        finta = self

        class Gestore(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _rispondi(self, corpo, tipo="application/x-protobuf"):
                self.send_response(200)
                self.send_header("Content-Type", tipo)
                self.send_header("Content-Length", str(len(corpo)))
                self.end_headers()
                self.wfile.write(corpo)

            def do_GET(self):  # noqa: N802
                finta.chiamate.append(("GET", self.path, b"", nodo_libero(pausa)))
                if self.path.startswith("/api/v1/fromradio"):
                    self._rispondi(b"\x08\x01")
                else:
                    self._rispondi(b"<html>Meshtastic</html>", "text/html")

            def do_PUT(self):  # noqa: N802
                corpo = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                finta.chiamate.append(("PUT", self.path, corpo, nodo_libero(pausa)))
                self._rispondi(b"")

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(pki / "takserver-chain.pem"), str(pki / "takserver.key"))
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", self.porta), Gestore)
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def ferma(self):
        self.server.shutdown()
        self.server.server_close()


class ApiNodoFinta:
    """La porta API di meshtasticd: rimanda indietro quello che riceve, e annota se il ponte l'aveva lasciata."""

    def __init__(self, pausa):
        self.porta = porta_libera()
        self.collegamenti = []   # nodo libero dal ponte al momento del collegamento?
        self.ascolto = socket.create_server(("127.0.0.1", self.porta))
        threading.Thread(target=self._accetta, args=(pausa,), daemon=True).start()

    def _accetta(self, pausa):
        while True:
            try:
                c, _ = self.ascolto.accept()
            except OSError:
                return
            self.collegamenti.append(nodo_libero(pausa))
            threading.Thread(target=self._eco, args=(c,), daemon=True).start()

    @staticmethod
    def _eco(c):
        try:
            while True:
                dati = c.recv(4096)
                if not dati:
                    return
                c.sendall(dati)
        except OSError:
            return
        finally:
            c.close()

    def ferma(self):
        self.ascolto.close()


def chiedi(porta, percorso, metodo="GET", corpo=None):
    r = urllib.request.Request(f"https://127.0.0.1:{porta}{percorso}", data=corpo, method=metodo)
    try:
        with urllib.request.urlopen(r, context=SENZA_VERIFICA, timeout=30) as risposta:
            return risposta.status, risposta.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def chiuso_dal_ponte(s, limite=5.0):
    s.settimeout(limite)
    try:
        return s.recv(10) == b""
    except (ConnectionResetError, BrokenPipeError):
        return True
    except socket.timeout:
        return False


@pytest.fixture
def gestione(pki):
    creati = []

    def crea(**opzioni):
        pausa = m.Pausa()
        pagina = PaginaNodoFinta(pki, pausa)
        api = ApiNodoFinta(pausa)
        valori = dict(tak_host="x", collegamento=f"tcp:127.0.0.1:{api.porta}", gestione_attiva=True,
                      gestione_porta_pagina=porta_libera(), gestione_porta_app=porta_libera(),
                      gestione_nodo_pagina=f"https://127.0.0.1:{pagina.porta}",
                      gestione_certificato=str(pki / "meshtastic-cnsas.pem"),
                      gestione_chiave=str(pki / "meshtastic-cnsas-plain.key"), gestione_chiusura_s=1000.0)
        valori.update(opzioni)
        g = m.Gestione(m.Config(**valori), pausa)
        g.avvia()
        for porta in (g.cfg.gestione_porta_pagina, g.cfg.gestione_porta_app):
            if porta:
                aspetta_porta(porta)
        # il ciclo principale del ponte: stacca il nodo poco dopo la richiesta di pausa
        fermo = threading.Event()

        def ciclo():
            while not fermo.wait(0.05):
                if pausa.attiva and not pausa.rilasciato.is_set():
                    time.sleep(0.3)
                    pausa.rilasciato.set()
        threading.Thread(target=ciclo, daemon=True).start()
        creati.append((g, pagina, api, fermo))
        return g, pagina, api
    yield crea
    for g, pagina, api, fermo in creati:
        fermo.set()
        g.ferma()
        pagina.ferma()
        api.ferma()


# ---- pagina nel browser ----

def test_pagine_passano_senza_pausa(gestione):
    g, pagina, _ = gestione()
    assert chiedi(g.cfg.gestione_porta_pagina, "/") == (200, b"<html>Meshtastic</html>")
    assert chiedi(g.cfg.gestione_porta_pagina, "/assets/app.js")[0] == 200
    assert not g.pausa.attiva, "aprire la pagina non basta: il gateway si ferma solo quando la pagina usa il nodo"


def test_la_pagina_usa_il_nodo_solo_con_il_ponte_in_pausa(gestione):
    g, pagina, _ = gestione()
    assert chiedi(g.cfg.gestione_porta_pagina, "/api/v1/fromradio?all=false") == (200, b"\x08\x01")
    assert g.pausa.attiva and "dal browser" in g.pausa.nota
    assert chiedi(g.cfg.gestione_porta_pagina, "/api/v1/toradio", "PUT", b"\x18\x01")[0] == 200
    api = [c for c in pagina.chiamate if c[1].startswith("/api/")]
    assert len(api) == 2 and all(c[3] for c in api), "mai una chiamata al nodo con il gateway ancora collegato"
    assert api[1][2] == b"\x18\x01"


def test_pagina_chiusa_il_ponte_riprende(gestione):
    g, _, _ = gestione(gestione_chiusura_s=0.5)
    chiedi(g.cfg.gestione_porta_pagina, "/api/v1/fromradio")
    assert g.pausa.attiva
    time.sleep(2.0)   # nessuna richiesta: la sorveglianza (ogni secondo) chiude la pausa
    assert not g.pausa.attiva
    assert chiedi(g.cfg.gestione_porta_pagina, "/api/v1/fromradio")[0] == 200, "riaprendo la pagina si ricomincia"
    assert g.pausa.attiva


def test_pagina_pausa_massima_poi_serve_ricaricare(gestione):
    g, pagina, _ = gestione(gestione_pausa_massima_min=1)
    chiedi(g.cfg.gestione_porta_pagina, "/api/v1/fromradio")
    g.controlla(time.time() + 61)
    assert not g.pausa.attiva and g.scaduta
    chiamate = len(pagina.chiamate)
    stato, testo = chiedi(g.cfg.gestione_porta_pagina, "/api/v1/fromradio")
    assert stato == 503 and "Ricarica la pagina" in testo.decode()
    assert len(pagina.chiamate) == chiamate and not g.pausa.attiva, "una scheda dimenticata non ferma il gateway"
    chiedi(g.cfg.gestione_porta_pagina, "/")   # ricaricata
    assert chiedi(g.cfg.gestione_porta_pagina, "/api/v1/fromradio")[0] == 200 and g.pausa.attiva


def test_pagina_nodo_spento_risposta_comprensibile(gestione):
    g, _, _ = gestione(gestione_nodo_pagina=f"https://127.0.0.1:{porta_libera()}")
    stato, testo = chiedi(g.cfg.gestione_porta_pagina, "/")
    assert stato == 502 and "meshtasticd" in testo.decode()


# ---- app Meshtastic via Wi-Fi ----

def test_app_usa_il_nodo_solo_con_il_ponte_in_pausa(gestione):
    g, _, api = gestione(gestione_chiusura_s=0.5)
    app = socket.create_connection(("127.0.0.1", g.cfg.gestione_porta_app), timeout=10)
    app.sendall(b"\x94\xc3\x00\x02ab")
    assert app.recv(10) == b"\x94\xc3\x00\x02ab", "i dati passano nei due sensi"
    assert g.pausa.attiva and "dall'app Meshtastic" in g.pausa.nota
    assert api.collegamenti == [True], "il gateway aveva gia' lasciato il nodo"
    time.sleep(2.0)
    assert g.pausa.attiva, "app collegata: la pausa continua anche se l'app tace"
    app.close()
    time.sleep(2.5)   # app chiusa: dopo chiusura_s il ponte riprende
    assert not g.pausa.attiva


def test_app_collegamento_muto_non_ferma_il_ponte(gestione):
    g, _, api = gestione()
    prova = socket.create_connection(("127.0.0.1", g.cfg.gestione_porta_app), timeout=10)   # es. prova della porta
    time.sleep(0.5)
    prova.close()
    time.sleep(0.3)
    assert not g.pausa.attiva and api.collegamenti == []


def test_app_l_ultima_collegata_vince(gestione):
    g, _, _ = gestione()
    prima = socket.create_connection(("127.0.0.1", g.cfg.gestione_porta_app), timeout=10)
    prima.sendall(b"x")
    assert prima.recv(1) == b"x"
    seconda = socket.create_connection(("127.0.0.1", g.cfg.gestione_porta_app), timeout=10)
    seconda.sendall(b"y")
    assert seconda.recv(1) == b"y"
    assert chiuso_dal_ponte(prima), "come il nodo: due app insieme si butterebbero fuori a vicenda"
    seconda.close()


def test_app_pausa_massima_poi_deve_tacere(gestione):
    g, _, _ = gestione(gestione_pausa_massima_min=1, gestione_silenzio_app_s=1.0)
    app = socket.create_connection(("127.0.0.1", g.cfg.gestione_porta_app), timeout=10)
    app.sendall(b"x")
    assert app.recv(1) == b"x"
    g.controlla(time.time() + 61)
    assert not g.pausa.attiva and g.app_bloccata
    assert chiuso_dal_ponte(app), "dopo la pausa massima l'app viene scollegata"
    for _ in range(3):   # l'app riprova subito: rifiutata, e ogni tentativo allunga il blocco
        time.sleep(0.4)
        di_nuovo = socket.create_connection(("127.0.0.1", g.cfg.gestione_porta_app), timeout=10)
        di_nuovo.sendall(b"x")
        assert chiuso_dal_ponte(di_nuovo)
        assert not g.pausa.attiva, "un'app dimenticata non ferma di nuovo il gateway"
    time.sleep(1.3)      # zitta per silenzio_app_s: si puo' ricollegare
    di_nuovo = socket.create_connection(("127.0.0.1", g.cfg.gestione_porta_app), timeout=10)
    di_nuovo.sendall(b"z")
    assert di_nuovo.recv(1) == b"z" and g.pausa.attiva
    di_nuovo.close()


def test_config_gestione(pki, tmp_path):
    conf = scrivi_config(tmp_path / "c.conf", pki, 8089, 4404, tmp_path, gestione__attiva="si",
                         gestione__certificato=pki / "meshtastic-cnsas.pem",
                         gestione__chiave=pki / "meshtastic-cnsas-plain.key")
    cfg = m.carica_config(str(conf))
    assert cfg.gestione_attiva and cfg.gestione_porta_pagina == 9443 and cfg.gestione_porta_app == 4403
    assert cfg.gestione_nodo_pagina == "https://127.0.0.1:9444"
    assert cfg.gestione_pausa_massima_min == 30 and cfg.gestione_chiusura_s == 120
    assert m.Gestione(cfg, m.Pausa()).app_nodo == ("127.0.0.1", 4404), "l'app va al nodo come il gateway"
    conf = scrivi_config(tmp_path / "d.conf", pki, 8089, 4404, tmp_path, gestione__attiva="si",
                         gestione__certificato=tmp_path / "manca.pem", gestione__chiave=pki / "meshtastic-cnsas-plain.key")
    with pytest.raises(m.ErroreConfig, match="certificato della pagina"):
        m.carica_config(str(conf))
    conf = scrivi_config(tmp_path / "e.conf", pki, 8089, 4404, tmp_path, gestione__attiva="si",
                         meshtastic__collegamento="serial:/dev/ttyACM0",
                         gestione__certificato=pki / "meshtastic-cnsas.pem",
                         gestione__chiave=pki / "meshtastic-cnsas-plain.key")
    with pytest.raises(m.ErroreConfig, match="porta_app"):
        m.carica_config(str(conf))
    assert m.carica_config(str(scrivi_config(tmp_path / "f.conf", pki, 8089, 4403, tmp_path))).gestione_attiva is False


# ---- con il ponte vero: nodo finto, TAK Server finto, pagina finta di meshtasticd ----

@pytest.fixture
def ponte_con_gestione(pki, tmp_path, tak_finto):
    nodo_porta = porta_libera()
    nodo = subprocess.Popen([sys.executable, os.path.join(QUI, "fake_node.py"), "--porta", str(nodo_porta),
                             "--primo-scenario", "99"], env=AMBIENTE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.STDOUT)
    aspetta_porta(nodo_porta)
    pagina_nodo = PaginaNodoFinta(pki)
    tak = tak_finto()
    porte = {"pagina": porta_libera(), "app": porta_libera()}
    conf = scrivi_config(tmp_path / "m.conf", pki, tak.porta, nodo_porta, tmp_path, gestione__attiva="si",
                         gestione__porta_pagina=porte["pagina"], gestione__porta_app=porte["app"],
                         gestione__nodo_pagina=f"https://127.0.0.1:{pagina_nodo.porta}",
                         gestione__certificato=pki / "meshtastic-cnsas.pem",
                         gestione__chiave=pki / "meshtastic-cnsas-plain.key", gestione__chiusura_s=3)
    ponte = subprocess.Popen([sys.executable, PONTE, "-c", str(conf)], env=AMBIENTE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
    stato = {}

    def eventi_ponte():
        return [e for e in tak.eventi() if e.get("type") == "a-f-G-I"]

    def aspetta(condizione, limite=15.0):
        fine = time.time() + limite
        while not condizione() and time.time() < fine:
            time.sleep(0.2)
        return condizione()

    def ferma():
        ponte.send_signal(signal.SIGTERM)
        stato["registro"], _ = ponte.communicate(timeout=20)
        return stato["registro"]

    assert aspetta(lambda: bool(eventi_ponte()), 20), "gateway non partito"
    yield porte, eventi_ponte, aspetta, ferma
    if "registro" not in stato:
        ferma()
    nodo.terminate()
    nodo.wait(5)
    pagina_nodo.ferma()


def in_pausa(eventi_ponte):
    return any("IN PAUSA" in (e.get("note") or "") for e in eventi_ponte())


def test_ponte_in_pausa_per_la_pagina_e_ripresa(ponte_con_gestione):
    porte, eventi_ponte, aspetta, ferma = ponte_con_gestione
    aspetta_porta(porte["pagina"])
    assert chiedi(porte["pagina"], "/api/v1/fromradio")[0] == 200
    assert aspetta(lambda: in_pausa(eventi_ponte), 10), "chi guarda la mappa lo sa"
    time.sleep(7)   # 3 s senza richieste: pagina chiusa, il ponte si ricollega
    assert "IN PAUSA" not in (eventi_ponte()[-1].get("note") or "")
    registro = ferma()
    assert "Traceback" not in registro, registro
    assert "Gestione del nodo: pagina nel browser sulla porta" in registro
    assert "Nodo gestito dal browser da 127.0.0.1: gateway in pausa" in registro
    assert "Gestione del nodo finita" in registro
    assert registro.count("Collegato al nodo Meshtastic") == 2, registro
    assert "riprovo tra" not in registro, "la pausa non e' un guasto del nodo"


def test_app_meshtastic_vera_attraverso_il_ponte(ponte_con_gestione):
    """La libreria Meshtastic (come fa l'app) si collega al nodo passando dal ponte e legge chi e'."""
    porte, eventi_ponte, aspetta, ferma = ponte_con_gestione
    aspetta_porta(porte["app"])
    import meshtastic.tcp_interface
    app = meshtastic.tcp_interface.TCPInterface(hostname="127.0.0.1", portNumber=porte["app"], noNodes=False)
    try:
        assert app.myInfo.my_node_num == 0xABCD0001, "configurazione completa del nodo attraverso il gateway"
        assert app.localNode.localConfig.lora.region != 0 or app.nodesByNum
        assert aspetta(lambda: in_pausa(eventi_ponte), 10)
    finally:
        app.close()
    time.sleep(7)   # app chiusa: dopo 3 s il ponte si ricollega
    registro = ferma()
    assert "Traceback" not in registro, registro
    assert "Nodo gestito dall'app Meshtastic da 127.0.0.1: gateway in pausa" in registro
    assert "App Meshtastic da 127.0.0.1 scollegata dal nodo" in registro
    assert registro.count("Collegato al nodo Meshtastic") == 2, registro
