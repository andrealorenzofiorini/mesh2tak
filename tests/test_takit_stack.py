"""Rete dentro lo stack takit (dalla 1.21.0): niente Caddy, Authentik e posta di infra-TAK. L'ingresso e il login sono
quelli dello stack (scheda del modulo rete-radio), il servizio della pagina fa da pagina intera (file, registrazioni,
domande ai gateway attraverso i tunnel) e gli avvisi partono con la posta dello stack, mandati da root.

Lo script vero (dist/rete-meshtastic.sh) gira su un "server dello stack" finto: file sotto una cartella di prova,
programmi di sistema finti che scrivono che cosa e' stato chiesto. Il servizio della pagina gira davvero, con un
gateway finto in HTTPS al posto del tunnel."""
import base64
import http.server
import json
import os
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib

import pytest

from conftest import RADICE, porta_libera
from http_finto import crea_certificati

sys.path.insert(0, os.path.join(RADICE, "server"))
import rete_meshtastic as rete  # noqa: E402

SCRIPT = os.path.join(RADICE, "dist", "rete-meshtastic.sh")
DOMINIO = "stack.lan"
OSPITE = f"meshtastic.{DOMINIO}"
PAGINE = "/opt/takit-dati/rete-radio/pagine.json"     # dalla 1.22.0: l'elenco dei gateway per lo stack
STACK = "/opt/takit-stack"
IMPIANTO = "/etc/takit/impianto.json"

# la posta dello stack, finta: scrive che cosa le chiedono e risponde come dice esito-posta.json (se c'e')
POSTA_FINTA = r'''
import json, os, sys
qui = os.path.dirname(os.path.abspath(__file__))
dati = json.loads(sys.stdin.read())
with open(os.path.join(qui, "chiamate-posta.jsonl"), "a") as f:
    f.write(json.dumps({"argomenti": sys.argv[1:], "oggetto": dati["oggetto"], "testo": dati["testo"]}) + "\n")
try:
    risposta = open(os.path.join(qui, "esito-posta.txt")).read()
except FileNotFoundError:
    risposta = json.dumps({"esito": "mandato", "dettaglio": "consegnata"})
print("== qualche riga prima")
print(risposta)
'''

# la console dello stack, finta: scrive che cosa le chiedono; con il file "console-ferma" accanto risponde con un errore
CONSOLE_FINTA = r'''
import json, os, sys
qui = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(qui, "chiamate-console.jsonl"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
if os.path.exists(os.path.join(qui, "console-ferma")):
    print("ERRORE: la console e' ferma", file=sys.stderr)
    sys.exit(2)
print("0123456789abcdef")
'''


def png_di_prova():
    """Un PNG di un punto (come in test_icone.py, che qui non si importa: vuole la libreria meshtastic)."""
    def pezzo(tipo, dati):
        return struct.pack(">I", len(dati)) + tipo + dati + struct.pack(">I", zlib.crc32(tipo + dati) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + pezzo(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)) +
            pezzo(b"IDAT", zlib.compress(b"\x00" + bytes((10, 10, 10, 255)))) + pezzo(b"IEND", b""))


def chiave(n):
    blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes([n]) * 32
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} mesh2tak-prova{n}@raspberry"


@pytest.fixture(scope="module", autouse=True)
def dns_di_prova():
    try:
        righe = open("/etc/hosts").read()
        aggiunte = "".join(f"127.0.0.1 {n}\n" for n in (OSPITE, f"meshtastic-casa.{DOMINIO}") if n not in righe)
        if aggiunte:
            with open("/etc/hosts", "a") as f:
                f.write("# prove rete-meshtastic nello stack\n" + aggiunte)
    except OSError:
        pytest.skip("serve poter scrivere /etc/hosts (le prove girano da root)")
    yield


class ServerStack:
    """Un server con lo stack takit, finto: nessun infra-TAK, nessun Caddy, nessun Authentik da chiamare."""

    def __init__(self, cartella):
        self.radice = cartella / "radice"
        self.stub = cartella / "stub"
        self.log = cartella / "chiamate.log"
        r = self.radice
        for d in ("etc/systemd/system", "etc/ssh/sshd_config.d", "run", "var/log", STACK.lstrip("/") + "/strumenti",
                  "etc/takit"):
            (r / d).mkdir(parents=True, exist_ok=True)
        (r / "etc/ssh/ssh_host_ed25519_key.pub").write_text(chiave(200) + "\n")
        (r / IMPIANTO.lstrip("/")).write_text('{"formato": 1, "profilo": "centrale", "dominio": "stack.lan"}\n')
        self.posta = r / STACK.lstrip("/") / "strumenti"
        (self.posta / "posta.py").write_text(POSTA_FINTA)
        self.console = r / STACK.lstrip("/") / "console"
        self.console.mkdir(parents=True)
        (self.console / "takit_console.py").write_text(CONSOLE_FINTA)
        self.stub.mkdir()
        stub = {"systemctl": f'echo "systemctl $*" >> {self.log}\nexit 0',
                "sshd": f'echo "sshd $*" >> {self.log}\n[ "$1" = "-T" ] && echo "port 22"\nexit 0',
                "ss": "exit 0"}
        for nome in ("useradd", "usermod", "userdel", "groupadd", "groupdel", "pkill", "resolvectl", "caddy", "docker"):
            stub[nome] = f'echo "{nome} $*" >> {self.log}; exit 0'
        for nome, corpo in stub.items():
            (self.stub / nome).write_text("#!/bin/bash\n" + corpo + "\n")
            (self.stub / nome).chmod(0o755)

    def esegui(self, *argomenti):
        # Authentik a un indirizzo dove non risponde nessuno: se lo script provasse a chiamarlo, la prova lo vedrebbe
        amb = dict(os.environ, RETE_PROVA_RADICE=str(self.radice), RETE_PROVA_AUTHENTIK=f"127.0.0.1:{porta_libera()}",
                   PATH=f"{self.stub}:{os.environ['PATH']}")
        r = subprocess.run(["bash", SCRIPT, *argomenti], env=amb, capture_output=True, text=True, timeout=300,
                           stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout + r.stderr

    def installa(self, *in_piu):
        return self.esegui("installa", "--takit-stack", "--dominio", DOMINIO, "--takit-radice", STACK,
                           "--takit-impianto", IMPIANTO, *in_piu)

    def richieste_alla_console(self):
        f = self.console / "chiamate-console.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def json(self, percorso):
        p = self.radice / percorso.lstrip("/")
        return json.loads(p.read_text()) if p.exists() else None

    def esiste(self, percorso):
        return (self.radice / percorso.lstrip("/")).exists()

    def chiamate(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def email(self):
        f = self.posta / "chiamate-posta.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


@pytest.fixture
def stack(tmp_path):
    return ServerStack(tmp_path)


# ---- lo script del server ----

def test_installa_dentro_lo_stack(stack):
    codice, uscita = stack.installa()
    assert codice == 0, uscita
    p = stack.json(rete.PIATTAFORMA)
    assert p == {"formato": 1, "tipo": "takit-stack", "dominio": DOMINIO, "radice": STACK, "impianto": IMPIANTO}
    # niente Caddy e niente Authentik: li fa lo stack
    assert not [c for c in stack.chiamate() if c.startswith(("caddy", "docker"))]
    assert "Caddy: lo gestisce lo stack takit" in uscita and "Authentik: lo gestisce lo stack takit" in uscita
    assert not stack.esiste("/etc/caddy")
    # la pagina e il servizio sanno dove sono
    sito = stack.json(rete.SITO_DIR + "/ponti.json")
    assert sito["piattaforma"] == "takit-stack" and sito["tak"] == f"takserver.{DOMINIO}" and sito["ponti"] == []
    sorveglianza = stack.json(rete.SORVEGLIANZA)
    assert sorveglianza["piattaforma"] == "takit-stack" and sorveglianza["console"] == 0
    assert sorveglianza["pagina"] == f"https://{OSPITE}/"
    # la coda della posta e' guardata dallo stesso servizio di root dei lavori
    unita = (stack.radice / "etc/systemd/system/mesh2tak-rete-lavori.path").read_text()
    assert f"DirectoryNotEmpty={rete.POSTA_DIR}" in unita and f"DirectoryNotEmpty={rete.LAVORI_DIR}" in unita
    assert stack.esiste(rete.POSTA_DIR) and stack.esiste(rete.ESITI_POSTA_DIR)
    # email di prova con la posta dello stack, una volta sola
    email = stack.email()
    assert len(email) == 1 and email[0]["oggetto"] == "[Rete Meshtastic] Email di prova"
    assert email[0]["argomenti"] == ["manda", "--impianto", str(stack.radice) + IMPIANTO]
    assert f"https://{OSPITE}/" in email[0]["testo"]
    assert stack.esiste(rete.AVVISI_PROVATI)
    assert "dietro al login dello stack takit" in uscita


def test_rilancio_senza_opzioni_resta_nello_stack(stack):
    assert stack.installa()[0] == 0
    codice, uscita = stack.esegui("installa")
    assert codice == 0, uscita
    assert stack.json(rete.PIATTAFORMA)["tipo"] == "takit-stack"
    assert len(stack.email()) == 1                     # l'email di prova non si ripete
    assert not [c for c in stack.chiamate() if c.startswith(("caddy", "docker"))]


def test_posta_dello_stack_non_impostata_lo_dice(stack):
    (stack.posta / "esito-posta.txt").write_text(json.dumps({"esito": "nessuno", "dettaglio": "posta non impostata"}))
    codice, uscita = stack.installa()
    assert codice == 0, uscita
    assert "la posta di servizio dello stack takit non e' impostata" in uscita
    assert not stack.esiste(rete.AVVISI_PROVATI)       # alla prossima installazione ci riprova


def test_da_infratak_allo_stack_solo_dopo_aver_disinstallato(stack):
    (stack.radice / "etc/mesh2tak-rete").mkdir(parents=True)
    (stack.radice / rete.REGISTRO.lstrip("/")).write_text('{"formato": 1, "ponti": {}, "authentik": {"ponti": {}}}')
    codice, uscita = stack.installa()
    assert codice == 1 and "installata accanto a infra-TAK" in uscita
    assert not stack.esiste(rete.PIATTAFORMA)


@pytest.mark.parametrize("cambio", [("--dominio", "non valido!"), ("--takit-radice", "opt/takit-stack"),
                                    ("--takit-impianto", "/etc/takit/../shadow"), ("--takit-radice", "/opt/altro")])
def test_dati_dello_stack_sbagliati(stack, cambio):
    argomenti = {"--dominio": DOMINIO, "--takit-radice": STACK, "--takit-impianto": IMPIANTO}
    argomenti[cambio[0]] = cambio[1]
    codice, uscita = stack.esegui("installa", "--takit-stack", *[x for k, v in argomenti.items() for x in (k, v)])
    assert codice == 1, uscita
    assert not stack.esiste(rete.PIATTAFORMA)


def test_cambiare_dominio_richiede_di_disinstallare(stack):
    assert stack.installa()[0] == 0
    codice, uscita = stack.esegui("installa", "--takit-stack", "--dominio", "altro.lan", "--takit-radice", STACK,
                                  "--takit-impianto", IMPIANTO)
    assert codice == 1 and "per cambiare dominio" in uscita
    assert stack.json(rete.PIATTAFORMA)["dominio"] == DOMINIO


def test_gateway_nello_stack_una_pagina_sola(stack):
    assert stack.installa()[0] == 0
    codice, uscita = stack.esegui("aggiungi", "casa", chiave(1))
    assert codice == 0, uscita
    registrazioni = [json.loads(p.read_text()) for p in (stack.radice / rete.ACCESSO_DIR.lstrip("/")).glob("*.json")]
    assert len(registrazioni) == 1
    r = registrazioni[0]
    assert r["ssh_host"] == OSPITE and r["ssh_porta"] == 22 and r["indirizzo"] == f"https://{OSPITE}/"
    sito = stack.json(rete.SITO_DIR + "/ponti.json")
    assert [p["nome"] for p in sito["ponti"]] == ["casa"] and sito["ponti"][0]["indirizzo"] == ""
    assert stack.json(rete.SORVEGLIANZA)["ponti"] == [{"nome": "casa", "porta": rete.PORTA_BASE}]
    assert not [c for c in stack.chiamate() if c.startswith(("caddy", "docker"))]
    assert f"meshtastic-casa.{DOMINIO}" not in uscita
    # tolto e poi tutto disinstallato: niente Authentik da chiamare, e il server torna senza rete
    codice, uscita = stack.esegui("rimuovi", "casa", "--si")
    assert codice == 0, uscita
    codice, uscita = stack.esegui("disinstalla", "--si")
    assert codice == 0, uscita
    assert "la pagina esce dal login quando lo stack takit toglie il modulo" in uscita
    assert not stack.esiste(rete.PIATTAFORMA) and not stack.esiste(rete.REGISTRO)


def test_un_indirizzo_per_gateway_con_lo_stack(stack):
    """Dalla 1.22.0: con --takit-pagine mesh2tak scrive l'elenco dei gateway e chiede alla console di rileggerlo."""
    codice, uscita = stack.installa("--takit-pagine", PAGINE)
    assert codice == 0, uscita
    assert stack.json(rete.PIATTAFORMA)["pagine"] == PAGINE
    assert stack.json(PAGINE) == {"formato": 1, "pagine": []}
    assert stack.richieste_alla_console() == []          # niente da pubblicare: niente da chiedere
    codice, uscita = stack.esegui("aggiungi", "casa", chiave(1))
    assert codice == 0, uscita
    assert stack.json(PAGINE) == {"formato": 1, "pagine": [{"nome": "casa", "porta": rete.PORTA_BASE}]}
    assert stack.richieste_alla_console() == [["chiedi", "--impianto", str(stack.radice) + IMPIANTO, "--modulo", "sistema",
                                               "--azione", "rileggi-indirizzi", "--utente", "rete-radio"]]
    assert "la console rilegge gli indirizzi tra pochi secondi" in uscita
    (r,) = [json.loads(p.read_text()) for p in (stack.radice / rete.ACCESSO_DIR.lstrip("/")).glob("*.json")]
    assert r["indirizzo"] == f"https://meshtastic-casa.{DOMINIO}/" and r["ssh_host"] == OSPITE
    sito = stack.json(rete.SITO_DIR + "/ponti.json")
    assert sito["ponti"][0]["indirizzo"] == f"https://meshtastic-casa.{DOMINIO}/"    # "Gestisci la radio" c'e'
    assert not [c for c in stack.chiamate() if c.startswith(("caddy", "docker"))]
    # rilanciare non chiede niente; togliere il gateway si'
    assert stack.esegui("installa")[0] == 0
    assert len(stack.richieste_alla_console()) == 1
    assert stack.esegui("rimuovi", "casa", "--si")[0] == 0
    assert stack.json(PAGINE) == {"formato": 1, "pagine": []} and len(stack.richieste_alla_console()) == 2


def test_se_la_console_non_prende_la_richiesta_lo_dice(stack):
    assert stack.installa("--takit-pagine", PAGINE)[0] == 0
    (stack.console / "console-ferma").write_text("")
    codice, uscita = stack.esegui("aggiungi", "casa", chiave(1))
    assert codice == 0, uscita                           # il gateway c'e' lo stesso: l'indirizzo arriva a mano
    assert "premi 'Rileggi gli indirizzi dei moduli'" in uscita and "la console e' ferma" in uscita


def test_dalla_pagina_il_server_si_aggiorna_con_la_console(stack):
    assert stack.installa("--takit-pagine", PAGINE)[0] == 0
    cartella = stack.radice / rete.LAVORI_DIR.lstrip("/")
    (cartella / "a1b2c3d4e5f60718.json").write_text(json.dumps(
        {"id": "a1b2c3d4e5f60718", "azione": "aggiorna-server", "nome": "server", "versione": "9.9.9", "da": "andrea",
         "richiesta": None, "sostituisci": False}))
    codice, uscita = stack.esegui("lavora")
    assert codice == 0, uscita
    assert [x[3:7] for x in stack.richieste_alla_console()] == [["--modulo", "sistema", "--azione", "aggiornamenti"],
                                                                ["--modulo", "rete-radio", "--azione", "sistema-aggiorna"]]
    assert all(x[-1] == "andrea" for x in stack.richieste_alla_console())
    esito = stack.json(rete.ESITI_DIR + "/a1b2c3d4e5f60718.json")
    assert esito["esito"] == "ok" and "Chiesto alla console takit" in esito["messaggio"]


def test_le_figure_delle_icone_dal_catalogo_che_da_lo_stack(stack):
    """Dalla 1.22.0: lo stack scarica e controlla lo stesso catalogo di infra-TAK e lo passa con --takit-icone."""
    figura = png_di_prova()
    catalogo = {"OSM": {"uid": rete.ICONSET_OSM, "groups": {
        "General": [{"f": "people.png", "b": base64.b64encode(figura).decode()}]}}}
    file = stack.radice / "opt/takit-dati/rete-radio/icon-catalog.json"
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(catalogo))
    codice, uscita = stack.installa("--takit-icone", "/opt/takit-dati/rete-radio/icon-catalog.json")
    assert codice == 0, uscita
    assert stack.json(rete.PIATTAFORMA)["icone"] == "/opt/takit-dati/rete-radio/icon-catalog.json"
    figure = {x["chiave"]: x["figura"] for x in stack.json(rete.ICONE_ELENCO)["icone"]}
    assert figure["soccorritore"] == "icone/soccorritore.png" and figure["cinofilo"] == ""
    assert (stack.radice / rete.ICONE_SITO.lstrip("/") / "soccorritore.png").read_bytes() == figura
    # uno stack che non lo passa (prima della alfa.21): la scelta delle icone resta, con i nomi
    assert stack.installa()[0] == 0
    assert "icone" not in stack.json(rete.PIATTAFORMA)
    assert all(x["figura"] == "" for x in stack.json(rete.ICONE_ELENCO)["icone"])


def test_percorsi_dello_stack_scritti_male():
    for voce in ("pagine", "icone"):
        for valore in ("relativo/x.json", "/a/../b.json", "/con spazio.json"):
            with pytest.raises(rete.Errore):
                rete.controlla_piattaforma({"dominio": DOMINIO, "radice": STACK, "impianto": IMPIANTO, voce: valore})
    d = rete.controlla_piattaforma({"dominio": DOMINIO, "radice": STACK, "impianto": IMPIANTO, "pagine": "", "icone": ""})
    assert "pagine" not in d and "icone" not in d
    assert rete.ImpostazioniStack(d).pagine_dei_gateway is False and rete.ImpostazioniStack(d).catalogo_icone() == ""


def metti_in_coda(stack, ident, oggetto="[Rete Meshtastic] Gateway casa: non risponde", testo="Da 10 minuti."):
    cartella = stack.radice / rete.POSTA_DIR.lstrip("/")
    (cartella / f"{ident}.json").write_text(json.dumps({"formato": 1, "id": ident, "oggetto": oggetto, "testo": testo,
                                                        "creato": int(time.time())}))


def test_root_manda_la_posta_in_coda_e_scrive_com_e_andata(stack):
    assert stack.installa()[0] == 0
    metti_in_coda(stack, "0123456789abcdef")
    cartella = stack.radice / rete.POSTA_DIR.lstrip("/")
    (cartella / "fedcba9876543210.json").symlink_to("/etc/passwd")    # un collegamento: scartato
    (cartella / "non-va.json").write_text("{}")                         # nome sbagliato: scartato
    codice, uscita = stack.esegui("lavora")
    assert codice == 0, uscita
    assert os.listdir(cartella) == []
    email = stack.email()
    assert [e["oggetto"] for e in email][-1] == "[Rete Meshtastic] Gateway casa: non risponde"
    assert len(email) == 2                              # quella di prova dell'installazione e questa
    esito = stack.json(rete.ESITI_POSTA_DIR + "/0123456789abcdef.json")
    assert esito["esito"] == "mandato" and esito["dettaglio"] == "consegnata"
    assert not stack.esiste(rete.ESITI_POSTA_DIR + "/fedcba9876543210.json")


@pytest.mark.parametrize("risposta,esito,dettaglio", [
    (json.dumps({"esito": "pausa", "dettaglio": "avvisi in pausa fino alle 18:00"}), "pausa", "avvisi in pausa fino alle 18:00"),
    (json.dumps({"esito": "nessuno", "dettaglio": "posta non impostata"}), "nessuno", "posta non impostata"),
    ("Traceback (most recent call last): ...", "errore", "la posta dello stack ha risposto senza dire"),
    (json.dumps({"esito": "inventato"}), "errore", "la posta dello stack ha risposto senza dire")])
def test_esiti_della_posta_dello_stack(stack, risposta, esito, dettaglio):
    assert stack.installa()[0] == 0
    (stack.posta / "esito-posta.txt").write_text(risposta)
    metti_in_coda(stack, "00000000000000aa")
    assert stack.esegui("lavora")[0] == 0
    d = stack.json(rete.ESITI_POSTA_DIR + "/00000000000000aa.json")
    assert d["esito"] == esito and d["dettaglio"].startswith(dettaglio)


# ---- il servizio della pagina, dentro lo stack ----

class GatewayFinto:
    """La pagina del gateway dall'altra parte del tunnel: in HTTPS con un certificato suo, scrive che cosa riceve."""

    def __init__(self, cartella):
        self.richieste = []
        self.risposte = {}
        g = self

        class G(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _rispondi(self):
                n = int(self.headers.get("Content-Length") or 0)
                g.richieste.append({"metodo": self.command, "percorso": self.path, "intestazioni": dict(self.headers),
                                    "corpo": self.rfile.read(n) if n else b""})
                codice, tipo, corpo = g.risposte.get(self.path.split("?")[0],
                                                     (200, "application/json", json.dumps({"dal_gateway": self.path}).encode()))
                self.send_response(codice)
                self.send_header("Content-Type", tipo)
                self.send_header("Set-Cookie", "dal-gateway=1")
                self.send_header("Content-Length", str(len(corpo)))
                self.end_headers()
                self.wfile.write(corpo)

            do_GET = do_POST = _rispondi

        self.porta = porta_libera()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", self.porta), G)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        crea_certificati(cartella)
        ctx.load_cert_chain(str(cartella / "srv.pem"), str(cartella / "srv.key"))
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def ferma(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def pagina(tmp_path, monkeypatch):
    monkeypatch.setattr(rete, "RADICE", str(tmp_path / "radice"))
    for d in (rete.SITO_DIR, rete.ACCESSO_DIR, rete.RICHIESTE_DIR, rete.LAVORI_DIR, rete.TMP_DIR, rete.ESITI_DIR,
              rete.ICONE_SITO, rete.POSTA_DIR, rete.ESITI_POSTA_DIR):
        os.makedirs(rete.P(d), exist_ok=True)
    (tmp_path / "gw").mkdir()
    gw = GatewayFinto(tmp_path / "gw")
    for nome, contenuto in (("index.html", "<!doctype html><title>Rete</title>"), ("icona.svg", "<svg/>"),
                            ("ponti.json", json.dumps({"formato": 1, "piattaforma": "takit-stack", "ponti": []})),
                            ("icone-mappa.json", json.dumps({"formato": 1, "icone": []}))):
        with open(rete.P(f"{rete.SITO_DIR}/{nome}"), "w") as f:
            f.write(contenuto)
    with open(rete.P(f"{rete.ICONE_SITO}/soccorritore.png"), "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\nfinta")
    with open(rete.P(f"{rete.ACCESSO_DIR}/{'a' * 64}.json"), "w") as f:
        f.write(json.dumps({"formato": 1, "nome": "casa"}))

    def piattaforma(tipo):
        with open(rete.P(rete.SORVEGLIANZA), "w") as f:
            json.dump({"formato": 1, "pagina": f"https://{OSPITE}/", "piattaforma": tipo, "console": 0,
                       "ponti": [{"nome": "casa", "porta": gw.porta}, {"nome": "spento", "porta": porta_libera()}]}, f)
    piattaforma("takit-stack")
    servizio = rete.Servizio()
    porta = porta_libera()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", porta), rete.gestore_per(servizio))
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def chiedi(percorso, metodo="GET", corpo=None, utente="andrea", **intestazioni):
        h = {"Host": OSPITE, **intestazioni}
        if utente:
            h["X-Authentik-Username"] = utente
        richiesta = urllib.request.Request(f"http://127.0.0.1:{porta}{percorso}", data=corpo, method=metodo, headers=h)
        apri = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with apri.open(richiesta, timeout=20) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    chiedi.gateway, chiedi.servizio, chiedi.piattaforma = gw, servizio, piattaforma
    yield chiedi
    server.shutdown()
    server.server_close()
    gw.ferma()


def test_la_pagina_e_i_suoi_file_solo_dopo_il_login(pagina):
    codice, intestazioni, corpo = pagina("/")
    assert codice == 200 and corpo.startswith(b"<!doctype html>")
    assert intestazioni["Content-Type"] == "text/html; charset=utf-8"
    csp = intestazioni["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in csp and "connect-src 'self'" in csp and "https://tile.openstreetmap.org" in csp
    assert intestazioni["X-Content-Type-Options"] == "nosniff" and intestazioni["Cache-Control"] == "no-store"
    assert pagina("/ponti.json")[2].startswith(b'{"formato": 1, "piattaforma": "takit-stack"')
    assert pagina("/icone/soccorritore.png")[1]["Content-Type"] == "image/png"
    for percorso in ("/", "/ponti.json", "/icone-mappa.json", "/icone/soccorritore.png", "/stato/casa.json"):
        assert pagina(percorso, utente="")[0] == 403, percorso
    for percorso in ("/icone/../ponti.json", "/icone/Soccorritore.png", "/sorveglianza.json", "/../etc/passwd", "/x"):
        assert pagina(percorso)[0] == 404, percorso
    assert pagina.gateway.richieste == []


def test_registrazioni_e_icona_aperte_a_chi_non_e_entrato(pagina):
    codice, intestazioni, corpo = pagina(f"/accesso/{'a' * 64}.json", utente="")
    assert codice == 200 and json.loads(corpo)["nome"] == "casa"
    assert pagina(f"/accesso/{'b' * 64}.json", utente="")[0] == 404
    codice, intestazioni, _ = pagina("/icona.svg", utente="")
    assert codice == 200 and intestazioni["Content-Type"] == "image/svg+xml"


def test_le_domande_ai_gateway_passano_dal_suo_tunnel(pagina):
    codice, intestazioni, corpo = pagina("/stato/casa.json", Cookie="authentik_session=segreta",
                                         Authorization="Bearer x")
    assert codice == 200 and json.loads(corpo) == {"dal_gateway": "/mesh2tak/stato.json"}
    assert "Set-Cookie" not in intestazioni                # nemmeno al ritorno passano i cookie
    r = pagina.gateway.richieste[-1]
    assert r["metodo"] == "GET" and r["percorso"] == "/mesh2tak/stato.json"
    assert r["intestazioni"]["X-Authentik-Username"] == "andrea"
    assert r["intestazioni"]["X-Forwarded-Host"] == OSPITE
    assert "Cookie" not in r["intestazioni"] and "Authorization" not in r["intestazioni"]
    # come in Caddy: storico, registro, copertura, radio (con il resto dell'indirizzo e la domanda)
    for sulla_pagina, sul_gateway in (("/storico/casa.json", "/mesh2tak/storico.json"),
                                      ("/registro/casa.txt", "/mesh2tak/registro"),
                                      ("/copertura/casa.json", "/mesh2tak/copertura.json"),
                                      ("/radio/casa/op?id=abc123", "/mesh2tak/radio/op?id=abc123"),
                                      ("/radio/casa/schema", "/mesh2tak/radio/schema")):
        assert pagina(sulla_pagina)[0] == 200, sulla_pagina
        assert pagina.gateway.richieste[-1]["percorso"] == sul_gateway
    corpo = json.dumps({"azione": "nome", "radio": "!1234abcd"}).encode()
    codice, _, _ = pagina("/radio/casa/op", metodo="POST", corpo=corpo, **{"X-Mesh2tak": "1", "Origin": f"https://{OSPITE}",
                                                                          "Content-Type": "application/json"})
    assert codice == 200
    r = pagina.gateway.richieste[-1]
    assert (r["metodo"], r["percorso"], r["corpo"]) == ("POST", "/mesh2tak/radio/op", corpo)
    assert r["intestazioni"]["X-Mesh2tak"] == "1" and r["intestazioni"]["Origin"] == f"https://{OSPITE}"
    assert r["intestazioni"]["X-Forwarded-Host"] == OSPITE
    pagina.gateway.risposte["/mesh2tak/copertura.json"] = (403, "application/json", b'{"errore": "no"}')
    assert pagina("/copertura/casa.json", metodo="POST", corpo=b'{"azzera": true}')[0] == 403    # risposta del gateway


def test_le_domande_ai_gateway_hanno_i_limiti_di_caddy(pagina):
    prima = len(pagina.gateway.richieste)
    assert pagina("/copertura/casa.json", metodo="POST", corpo=b"x" * 1025)[0] == 413
    assert pagina("/radio/casa/op", metodo="POST", corpo=b"x" * 16385)[0] == 413
    assert pagina("/stato/casa.json", metodo="POST", corpo=b"{}")[0] == 405
    assert pagina("/registro/casa.txt", metodo="POST", corpo=b"{}")[0] == 405
    assert pagina("/stato/altro.json")[0] == 404
    assert pagina("/stato/Casa.json")[0] == 404
    assert pagina("/radio/casa/op?" + "x" * 300)[0] == 414
    assert pagina("/radio/casa/op", metodo="POST", corpo=b"{}", utente="")[0] == 403
    assert len(pagina.gateway.richieste) == prima
    codice, _, corpo = pagina("/stato/spento.json")
    assert codice == 502 and "il gateway spento non risponde" in json.loads(corpo)["errore"]


def test_accanto_a_infratak_il_servizio_non_fa_da_pagina(pagina):
    pagina.piattaforma("infra-tak")
    for percorso in ("/", "/ponti.json", "/stato/casa.json", f"/accesso/{'a' * 64}.json"):
        assert pagina(percorso)[0] == 404, percorso          # li serve Caddy, come prima
    assert pagina("/radio/casa/op", metodo="POST", corpo=b"{}")[0] == 404
    assert pagina.gateway.richieste == []


def test_il_server_dello_stack_si_aggiorna_dalla_pagina_attraverso_la_console(pagina):
    class Rilasci:
        pronta = staticmethod(lambda v: True)
    pagina.servizio.rilasci = Rilasci()
    codice, d = pagina.servizio.azione("/api/aggiorna-server", {"versione": "99.0.0"}, "andrea")
    assert codice == 202 and "Chiedo alla console takit" in d["messaggio"]
    (lavoro,) = os.listdir(rete.P(rete.LAVORI_DIR))
    assert json.load(open(rete.P(f"{rete.LAVORI_DIR}/{lavoro}")))["azione"] == "aggiorna-server"


def test_avvisi_con_la_posta_dello_stack_mandati_da_root(pagina, tmp_path):
    radice_stack = tmp_path / "radice" / STACK.lstrip("/") / "strumenti"
    radice_stack.mkdir(parents=True)
    (radice_stack / "posta.py").write_text(POSTA_FINTA)
    imp = rete.ImpostazioniStack({"formato": 1, "tipo": "takit-stack", "dominio": DOMINIO, "radice": STACK,
                                  "impianto": IMPIANTO})
    fermo = threading.Event()

    def root():                         # al posto del servizio di root che guarda la coda
        while not fermo.is_set():
            rete.consegna_la_posta(imp)
            time.sleep(0.1)
    t = threading.Thread(target=root, daemon=True)
    t.start()
    try:
        sorveglianza = rete.Sorveglianza(config=lambda: {"piattaforma": "takit-stack", "pagina": f"https://{OSPITE}/"})
        codice, d = sorveglianza.prova("andrea")
        assert codice == 200 and d["esito"] == "mandato"
        assert "posta dello stack takit" in d["messaggio"]
        email = [json.loads(x) for x in (radice_stack / "chiamate-posta.jsonl").read_text().splitlines()]
        assert email[-1]["oggetto"] == "[Rete Meshtastic] Email di prova" and "chiesta da andrea" in email[-1]["testo"]
        oggetto, testo = sorveglianza.componi([{"ponte": "casa", "codice": "giu", "tipo": "problema",
                                                "quando": time.time() - 700}], {"piattaforma": "takit-stack"}, time.time())
        assert "avvisi dello stack takit (Posta di servizio)" in testo and "infra-TAK" not in testo
    finally:
        fermo.set()
        t.join(5)
    assert os.listdir(rete.P(rete.POSTA_DIR)) == []


def test_avviso_non_preso_in_tempo_non_resta_in_coda(pagina, monkeypatch):
    monkeypatch.setattr(rete.Sorveglianza, "ATTESA_POSTA_S", 0.6)
    esito, dettaglio = rete.Sorveglianza.invia_stack("[Rete Meshtastic] prova", "testo")
    assert esito == "errore" and "non ha mandato l'email in tempo" in dettaglio
    assert os.listdir(rete.P(rete.POSTA_DIR)) == []       # non parte piu' da sola: niente email doppia
