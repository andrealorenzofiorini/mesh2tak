"""Prova completa della rete: sshd 8.9 vero (quello di Ubuntu 22.04) con le regole dei ponti, Caddy vero con i
siti generati, Authentik finto (API e avamposto), utenti veri, il servizio della pagina come utente senza privilegi
e il passo dei lavori come lo fa partire systemd; dal lato Raspberry pagina del ponte, richiesta di entrare e
tunnel veri. Dal browser dell'amministratore fino al nodo, passando da internet."""
import glob
import http.server
import json
import os
import pathlib
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import base64
import pwd

import pytest

from authentik_finto import TOKEN, AuthentikFinto
from conftest import RADICE, aspetta_porta, porta_libera
from http_finto import ServerFinto, somme
from test_gestione import PaginaNodoFinta
import mesh2tak as m

SCRIPT = os.path.join(RADICE, "dist", "rete-meshtastic.sh")
SSHD = os.environ.get("PROVA_SSHD", "/opt/prova-openssh/sbin/sshd")
CADDY = os.environ.get("PROVA_CADDY", "/opt/prova-caddy") + "/caddy-2.10.2"
DOMINIO = "prova.lan"
RETE = f"https://meshtastic.{DOMINIO}:8443"
PONTE = f"https://meshtastic-casa.{DOMINIO}:8443"
NOMI = [f"meshtastic.{DOMINIO}", f"meshtastic-casa.{DOMINIO}", f"meshtastic-altro.{DOMINIO}",
        f"meshtastic-velate.{DOMINIO}"]
REPO = "andrealorenzofiorini/mesh2tak"
FILE_RELEASE = ("installa-ponte-meshtastic.sh", "rete-meshtastic.sh", "SHA256SUMS")
# figura finta per il catalogo di icone di infra-TAK (un quadratino PNG valido)
FIGURA_CANE = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg==")
CANE = "6d781afb-89a6-4c07-b2b9-a89748b6a38f/Leisure/dog.png"

pytestmark = pytest.mark.skipif(not (os.path.exists(SSHD) and os.path.exists(CADDY) and shutil.which("ssh")),
                                reason="servono sshd, ssh e caddy di prova")


def aspetta(condizione, limite=20.0):
    fine = time.time() + limite
    while time.time() < fine:
        if condizione():
            return True
        time.sleep(0.1)
    return False


class Rete:
    """Il server della rete (sshd, Caddy, Authentik finto) e un Raspberry (pagina del ponte, tunnel)."""

    def __init__(self, cartella, pki):
        self.c = cartella
        self.github = ServerFinto()       # API e download delle release (1.8.0): nessuna release all'inizio
        # sshd legge le chiavi autorizzate con i permessi dell'utente del ponte, e (StrictModes) vuole cartelle
        # non scrivibili da altri fino a /: la radice di prova sta in /srv, non sotto /tmp come sul server sta in /etc
        r = self.r = pathlib.Path("/srv/mesh2tak-prova-e2e")
        shutil.rmtree(r, ignore_errors=True)
        r.mkdir(parents=True)
        os.chmod("/srv", 0o755)
        self.stub = cartella / "stub"
        self.log = cartella / "chiamate.log"
        for d in ("etc/systemd/system", "opt/infratak/.config", "root/authentik", "etc/caddy", "etc/ssh/sshd_config.d",
                  "run", "caddy"):
            (r / d).mkdir(parents=True, exist_ok=True)
        (r / "etc/systemd/system/takwerx-console.service").write_text("[Service]\nWorkingDirectory=/opt/infratak\n")
        # console di infra-TAK finta: riceve gli avvisi per email come quelli di Guard Dog (1.7.0)
        self.email = []
        posta = self.email

        class Console(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):  # noqa: N802
                if self.path == "/api/guarddog/send-alert-email":
                    posta.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0))))
                corpo = b'{"success": true, "suppressed": false}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(corpo)))
                self.end_headers()
                self.wfile.write(corpo)
        self.console = http.server.ThreadingHTTPServer(("127.0.0.1", porta_libera()), Console)
        threading.Thread(target=self.console.serve_forever, daemon=True).start()
        (r / "opt/infratak/.config/settings.json").write_text(json.dumps(
            {"fqdn": DOMINIO, "server_ip": "127.0.0.1", "ssl_mode": "letsencrypt", "caddy_https_port": 8443,
             "guarddog_alert_email": "info@prova.lan", "console_port": self.console.server_address[1]}))
        # catalogo delle icone di infra-TAK (1.11.0): figure finte, o quello vero con RETE_PROVA_CATALOGO_ICONE
        (r / "opt/infratak/nodered").mkdir()
        if os.environ.get("RETE_PROVA_CATALOGO_ICONE"):
            shutil.copyfile(os.environ["RETE_PROVA_CATALOGO_ICONE"], r / "opt/infratak/nodered/icon-catalog.json")
        else:
            (r / "opt/infratak/nodered/icon-catalog.json").write_text(json.dumps({"OSM": {
                "uid": "6d781afb-89a6-4c07-b2b9-a89748b6a38f", "groups": {
                    "Leisure": [{"f": "dog.png", "b": base64.b64encode(FIGURA_CANE).decode()}],
                    "Landmarks": [{"f": "tower.png", "b": base64.b64encode(FIGURA_CANE).decode()}]}}}))
        (r / "root/authentik/.env").write_text(f"AUTHENTIK_BOOTSTRAP_TOKEN={TOKEN}\n")
        (r / "etc/caddy/Caddyfile").write_text(
            f"{{\n\thttp_port 18080\n\thttps_port 8443\n\tlocal_certs\n\tskip_install_trust\n\tadmin localhost:2999\n"
            f"\tstorage file_system {r}/caddy\n}}\n\ninfratak.{DOMINIO} {{\n\trespond \"console\"\n}}\n\n"
            "# --- User-added blocks (do not remove) ---\n# Anything below this line survives every infra-TAK regeneration.\n")
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(r / "etc/ssh/ssh_host_ed25519_key")], check=True)
        (r / "etc/ssh/sshd_config").write_text(
            f"Include {r}/etc/ssh/sshd_config.d/*.conf\nPort 2222\nListenAddress 127.0.0.1\n"
            f"HostKey {r}/etc/ssh/ssh_host_ed25519_key\nPidFile {r}/run/sshd.pid\nLogLevel VERBOSE\nStrictModes yes\n")
        self.stub.mkdir()
        stub = {
            "systemctl": f'echo "systemctl $*" >> {self.log}\n'
                         f'case "$*" in\n'
                         f'  "reload caddy") exec {CADDY} reload --config {r}/etc/caddy/Caddyfile --adapter caddyfile '
                         f'--address localhost:2999 ;;\n'
                         f'  "reload ssh") kill -HUP "$(cat {r}/run/sshd.pid)"; sleep 0.5 ;;\n'
                         f'  "show caddy"*) echo "" ;;\nesac\nexit 0',
            "docker": f'echo "docker $*" >> {self.log}\ncase "$1" in ps) echo authentik-server-1;; inspect) echo healthy;; esac',
            "sshd": f'exec {SSHD} -f {r}/etc/ssh/sshd_config "$@"',
            "caddy": f'exec {CADDY} "$@"',
            "resolvectl": "exit 0",
        }
        for nome in ("useradd", "usermod", "userdel", "groupadd", "groupdel", "pkill"):
            stub[nome] = f'echo "{nome} $*" >> {self.log}\nexec {shutil.which(nome)} "$@"'
        for nome, corpo in stub.items():
            (self.stub / nome).write_text("#!/bin/bash\n" + corpo + "\n")
            (self.stub / nome).chmod(0o755)
        # avvio: sshd, Authentik finto, Caddy
        subprocess.run([SSHD, "-f", str(r / "etc/ssh/sshd_config"), "-E", str(cartella / "sshd.log")], check=True)
        aspetta_porta(2222)
        self.porta_ak = porta_libera()
        self.ak = AuthentikFinto(self.porta_ak)
        self.caddy = subprocess.Popen([CADDY, "run", "--config", str(r / "etc/caddy/Caddyfile"), "--adapter", "caddyfile"],
                                      stdout=open(cartella / "caddy.log", "w"), stderr=subprocess.STDOUT,
                                      env=dict(os.environ, HOME=str(cartella)))
        aspetta_porta(8443)
        assert aspetta(lambda: bool(self.ca_caddy()), 20), "CA locale di Caddy non creata"
        # Raspberry: nodo finto, pagina del ponte, chiave
        self.pausa = m.Pausa()
        self.nodo = PaginaNodoFinta(pki, self.pausa)
        self.intestazioni_nodo = []
        vecchia = self.nodo.server.RequestHandlerClass.do_GET

        def do_get(gestore):
            self.intestazioni_nodo.append({k.lower(): v for k, v in gestore.headers.items()})
            vecchia(gestore)
        self.nodo.server.RequestHandlerClass.do_GET = do_get
        self.chiave = cartella / "pi" / "chiave"
        self.chiave.parent.mkdir()
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "mesh2tak-casa@raspberry", "-f", str(self.chiave)],
                       check=True)
        self.porta_web = porta_libera()
        self.servizio = None
        self.porta_pagina = porta_libera()
        self.cfg = m.Config(tak_host="x", collegamento="tcp:127.0.0.1:1", gestione_attiva=True,
                            certificato=str(pki / "meshtastic-cnsas.pem"),
                            gestione_porta_pagina=self.porta_pagina, gestione_porta_app=0,
                            gestione_nodo_pagina=f"https://127.0.0.1:{self.nodo.porta}",
                            gestione_certificato=str(pki / "meshtastic-cnsas.pem"),
                            gestione_chiave=str(pki / "meshtastic-cnsas-plain.key"), gestione_chiusura_s=1000.0,
                            accesso_attivo=True, accesso_nome="casa", accesso_rete=RETE,
                            accesso_chiave=str(self.chiave), accesso_cartella_stato=str(cartella / "pi" / "stato"))
        self.g = m.Gestione(self.cfg, self.pausa)
        # lo stato vero del ponte (stessa funzione del servizio), con un paio di radio sentite
        self.ponte = m.Ponte(self.cfg, tak=None, sdk=None)
        self.ponte.icone = m.IconeMappa(str(cartella / "pi" / "icone.json"))
        self.g.icone = self.ponte.imposta_icone
        # copertura (1.14.0), vuota: dalla 1.15.0 la pagina la legge appena si apre, come dal Raspberry vero
        self.ponte.copertura = m.Copertura(str(cartella / "pi" / "copertura-vuota.json"))
        self.g.copertura = self.ponte.copertura_per_la_pagina
        self.ponte.storico = m.Storico(str(cartella / "pi" / "storico.json"))      # dalla 1.16.0
        self.g.storico = self.ponte.storico.dati
        self.ponte.mio_num, self.ponte.nodo_nome, self.ponte.nodo_nome_breve = 0x11111111, "Gateway di prova", "PROV"
        self.ponte.canali = {0: m.Canale(0, "CNSAS-VA", True), 1: m.Canale(1, "Public", False)}
        self.ponte.canali_ok = {0}
        ora = time.time()
        for num, nome, eta, batt in ((0x7c3a91e2, "07-BIANCHI", 40, 87), (0x9e1d0b77, "<img src=x onerror=alert(1)>", 900, 40)):
            f = m.Fonte(tipo_fonte="mesh", uid=f"mesh-{num:08x}", lat=45.8, lon=8.8, hae=0, ce=10, t_fix=ora - eta, canale=0)
            self.ponte.stato[num] = m.StatoNodo(num, f, ora - eta, nome_inviato=nome, batteria=batt, salti=1)
        # elenco delle radio della squadra (1.6.0): una con la batteria scarica, una con un nome malevolo
        self.ponte.viste[0x7c3a91e2] = m.RadioVista(0x7c3a91e2, ultimo=ora - 40, nome="07-BIANCHI", nome_breve="07CA",
                                                    modello="TRACKER_T1000_E", ruolo="TAK", batteria=87, tensione=3.97,
                                                    occupato=6.5, trasmesso=0.4, acceso_s=7200, metriche=ora - 300,
                                                    salti=0, snr=6.25, rssi=-87, diretto=ora - 40)
        self.ponte.viste[0x9e1d0b77] = m.RadioVista(0x9e1d0b77, ultimo=ora - 900, nome="<img src=x onerror=alert(1)>",
                                                    modello="HELTEC_V3", ruolo="CLIENT", batteria=15, metriche=ora - 900,
                                                    salti=2)
        self.ponte.canale_locale = {"occupato": 8.2, "trasmesso": 1.23, "quando": ora - 30}
        tak = type("TakFinto", (), {"collegato": threading.Event(), "collegato_dal": ora - 3600, "ultimo_errore": None})()
        tak.collegato.set()
        self.sistema = m.Sistema()
        self.g.fornisci_stato = lambda: m.stato_per_la_rete(self.cfg, self.ponte, tak, self.g, self.accesso, ora - 7200,
                                                            self.sistema)
        self.g.avvia()
        aspetta_porta(self.porta_pagina)
        self._fermo = threading.Event()

        def ciclo():   # come il ciclo principale del ponte: lascia il nodo poco dopo la pausa
            while not self._fermo.wait(0.05):
                if self.pausa.attiva and not self.pausa.rilasciato.is_set():
                    time.sleep(0.2)
                    self.pausa.rilasciato.set()
        threading.Thread(target=ciclo, daemon=True).start()
        self.accesso = None
        self.altri = []

    def ca_caddy(self):
        return glob.glob(str(self.r / "caddy/pki/authorities/local/root.crt"))

    def contesto(self):
        return ssl.create_default_context(cafile=self.ca_caddy()[0])

    def avvia_accesso(self, chiave=None, nome="casa", porta_pagina=None):
        cfg = self.cfg if chiave is None else m.Config(**{**self.cfg.__dict__, "accesso_chiave": str(chiave),
                                                          "accesso_nome": nome})
        a = m.AccessoEsterno(cfg, porta_pagina or self.porta_pagina)
        a.ATTESA_REGISTRAZIONE = (0.5, 0.5)
        a.RICHIESTA_OGNI_S = 1.0
        a.RICHIESTA_RIPROVA_S = 1.0
        a.ATTESA_RETE = (0.5, 1.0)
        a.ATTESA_PORTA_OCCUPATA_S = 1.0
        a.ATTESA_RIFIUTO_S = 60.0
        a.DURATA_STABILE_S = 1.0
        a.CONFERMA_S = 0.5
        a.contesto_https = self.contesto()
        a.avvia()
        return a

    def ambiente(self):
        return dict(os.environ, RETE_PROVA_RADICE=str(self.r), RETE_PROVA_AUTHENTIK=f"127.0.0.1:{self.porta_ak}",
                    RETE_PROVA_PORTA_WEB=str(self.porta_web), PATH=f"{self.stub}:{os.environ['PATH']}",
                    RETE_PROVA_GITHUB=self.github.url, RETE_PROVA_ATTESA_SERVIZIO="5")

    def esegui(self, *argomenti):
        p = subprocess.run(["bash", SCRIPT, *argomenti], env=self.ambiente(), capture_output=True, text=True, timeout=300,
                           stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout + p.stderr

    def avvia_servizio(self):
        """Come systemd: il servizio della pagina come utente mesh2tak-rete, e il passo dei lavori (root) ogni volta
        che la cartella dei lavori non e' vuota (l'unita' .path)."""
        script = str(self.r / "opt/mesh2tak-rete/rete_meshtastic.py")
        amb = {k: v for k, v in self.ambiente().items() if "proxy" not in k.lower()}
        self.servizio = subprocess.Popen(
            ["runuser", "-u", "mesh2tak-rete", "--", "env", f"RETE_PROVA_RADICE={self.r}",
             f"RETE_PROVA_PORTA_WEB={self.porta_web}", "RETE_PROVA_AVVISI_OGNI=1", "RETE_PROVA_AVVISI_ATTESA=4",
             f"RETE_PROVA_GITHUB_API={self.github.url}", "RETE_PROVA_RILASCI_CONTROLLO=1", "RETE_PROVA_RILASCI_PRIMO=86400",
             sys.executable, "-I", script, "servi"],
            stdout=open(self.c / "servizio.log", "a"), stderr=subprocess.STDOUT, env=amb)
        aspetta_porta(self.porta_web)
        self.esiti_lavora = []

        def lavori():
            cartella = self.r / "var/lib/mesh2tak-rete/servizio/lavori"
            while not self._fermo.wait(0.2):
                if cartella.exists() and any(cartella.iterdir()):
                    p = subprocess.run([sys.executable, "-I", script, "lavora"], env=self.ambiente(), capture_output=True,
                                       text=True, timeout=600)
                    self.esiti_lavora.append((p.returncode, p.stdout + p.stderr))
        threading.Thread(target=lavori, daemon=True).start()

    def api(self, metodo, percorso, corpo=None, cookie="andrea", intestazioni=None):
        """Come la pagina della rete nel browser dell'amministratore."""
        h = {"Content-Type": "application/json", "X-Mesh2tak": "1", "Origin": RETE}
        h.update(intestazioni or {})
        if cookie:
            h["Cookie"] = f"ak_finto={cookie}"
        rq = urllib.request.Request(f"{RETE}{percorso}", method=metodo, headers={k: v for k, v in h.items() if v},
                                    data=json.dumps(corpo).encode() if corpo is not None else None)
        apri = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=self.contesto()),
                                           SenzaRedirect())
        try:
            with apri.open(rq, timeout=20) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            corpo = e.read()
            try:
                return e.code, json.loads(corpo or b"null")
            except ValueError:
                return e.code, {"corpo": corpo[:200].decode("utf-8", "replace"), "location": e.headers.get("Location")}

    def stato_api(self):
        codice, d = self.api("GET", "/api/stato")
        assert codice == 200, d
        return d

    def chiedi(self, url, cookie="andrea", intestazioni=None):
        rq = urllib.request.Request(url, headers=dict(intestazioni or {}, **({"Cookie": f"ak_finto={cookie}"} if cookie else {})))
        apri = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=self.contesto()),
                                           SenzaRedirect())
        try:
            with apri.open(rq, timeout=20) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def sshd_log(self):
        return (self.c / "sshd.log").read_text() if (self.c / "sshd.log").exists() else ""

    def ferma(self):
        self._fermo.set()
        for a in [self.accesso] + self.altri:
            if a is not None:
                a.ferma()
        if self.servizio is not None:
            self.servizio.terminate()
            self.servizio.wait(10)
        self.g.ferma()
        self.nodo.ferma()
        self.console.shutdown()
        self.console.server_close()
        self.caddy.terminate()
        self.caddy.wait(10)
        try:
            os.kill(int((self.r / "run/sshd.pid").read_text()), 15)
        except (OSError, ValueError):
            pass
        self.ak.ferma()
        self.github.ferma()
        pulisci_utenti()


class SenzaRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def pulisci_utenti():
    for u in ("ponte-casa", "ponte-altro"):
        subprocess.run(["pkill", "-KILL", "-u", u], capture_output=True)
        subprocess.run(["userdel", u], capture_output=True)
    subprocess.run(["pkill", "-u", "mesh2tak-rete"], capture_output=True)
    subprocess.run(["userdel", "mesh2tak-rete"], capture_output=True)
    for g in ("ponti-meshtastic", "mesh2tak-rete"):
        subprocess.run(["groupdel", g], capture_output=True)


@pytest.fixture(scope="module")
def rete(tmp_path_factory, pki):
    righe = open("/etc/hosts").read()
    with open("/etc/hosts", "a") as f:
        for n in NOMI:
            if n not in righe:
                f.write(f"127.0.0.1 {n}\n")
    pulisci_utenti()
    r = Rete(tmp_path_factory.mktemp("rete"), pki)
    yield r
    r.ferma()


def nuova_chiave(cartella, nome):
    k = cartella / nome
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"mesh2tak-{nome}@raspberry", "-f", str(k)],
                   check=True)
    return k


def nessun_tentativo(rete, dal):
    return rete.sshd_log().count("Connection from") == dal


def test_1_prima_della_pagina_il_raspberry_non_bussa(rete):
    rete.accesso = rete.avvia_accesso()
    assert aspetta(lambda: "non risponde" in rete.accesso.stato()["messaggio"])   # Caddy non ha ancora il sito
    time.sleep(2)
    assert "ponte-casa" not in rete.sshd_log() and rete.accesso.stato()["richiesta"] is None


def test_2_installa_una_volta_e_la_richiesta_arriva_da_sola(rete):
    esito, uscita = rete.esegui("installa")
    assert esito == 0, uscita
    assert subprocess.run(["id", "-u", "mesh2tak-rete"], capture_output=True).returncode == 0
    rete.avvia_servizio()
    controllo = m.codice_controllo(rete.accesso.impronta)
    assert aspetta(lambda: rete.accesso.stato()["richiesta"] == "in_attesa", 30), rete.accesso.stato()
    assert f"codice di controllo {controllo}" in rete.accesso.stato()["messaggio"]
    [r] = rete.stato_api()["richieste"]
    assert (r["nome"], r["controllo"], r["ip"], r["stato"], r["versione"]) == ("casa", controllo, "127.0.0.1", "attesa",
                                                                             m.VERSIONE)
    assert r["macchina"] == rete.accesso.macchina and r["sostituisce"] is False
    assert "ponte-casa" not in rete.sshd_log()              # nessun tentativo SSH prima dell'approvazione
    # le azioni solo da un amministratore entrato in Authentik, e solo dalla pagina della rete
    assert rete.api("GET", "/api/stato", cookie=None)[0] == 302
    assert rete.api("POST", "/api/approva", {"id": r["id"]}, cookie=None)[0] == 302
    assert rete.api("POST", "/api/approva", {"id": r["id"]}, intestazioni={"X-Mesh2tak": ""})[0] == 403
    assert rete.api("POST", "/api/approva", {"id": r["id"]}, intestazioni={"Origin": "https://cattivo.example"})[0] == 403
    assert rete.api("POST", "/api/aggiungi", {"nome": "x", "chiave": "y"},
                    intestazioni={"X-Authentik-Username": "mallory"}, cookie=None)[0] == 302
    assert rete.stato_api()["lavori"] == []


def test_3_approvata_dalla_pagina_il_raspberry_apre_il_tunnel(rete):
    [r] = rete.stato_api()["richieste"]
    codice, d = rete.api("POST", "/api/approva", {"id": r["id"]})
    assert codice == 202 and d["esito"] == "in_corso", d
    assert aspetta(lambda: rete.accesso.stato()["collegato"], 90), (rete.accesso.stato(), rete.esiti_lavora)
    assert rete.accesso.stato()["indirizzo"] == PONTE + "/"
    with socket.create_connection(("127.0.0.1", 19443), timeout=5):
        pass
    kh = (rete.c / "pi/stato/server-rete.known_hosts").read_text()
    assert kh.split()[1:3] == (rete.r / "etc/ssh/ssh_host_ed25519_key.pub").read_text().split()[:2]
    assert "Accepted publickey for ponte-casa" in rete.sshd_log()
    assert aspetta(lambda: rete.stato_api()["richieste"] == [], 30)
    st = rete.stato_api()
    assert [(x["azione"], x["nome"], x["esito"], x["da"]) for x in st["lavori"]] == \
        [("aggiungi", "casa", "ok", "andrea")]
    assert all(e == 0 for e, _ in rete.esiti_lavora), rete.esiti_lavora
    assert "docker restart authentik-server-1" in rete.log.read_text()


def test_4_pagina_della_rete_e_stato_del_ponte_dietro_authentik(rete):
    stato, corpo, _ = rete.chiedi(f"{RETE}/stato/casa.json")
    assert stato == 200 and json.loads(corpo)["nome"] == "Gateway di prova"
    stato, corpo, _ = rete.chiedi(f"{RETE}/")
    assert stato == 200 and b"Rete Meshtastic" in corpo
    stato, corpo, h = rete.chiedi(f"{RETE}/ponti.json")
    assert stato == 200 and json.loads(corpo)["ponti"][0]["nome"] == "casa" and h.get("Cache-Control") == "no-store"
    # senza sessione: ad Authentik
    for url in (f"{RETE}/", f"{RETE}/stato/casa.json", f"{RETE}/api/stato", f"{PONTE}/", f"{PONTE}/api/v1/fromradio"):
        stato, _, h = rete.chiedi(url, cookie=None)
        assert stato == 302 and "tak.prova.lan" in h["Location"], url
    # pubbliche: registrazione (per i Raspberry) e icona (per la pagina iniziale di Authentik)
    stato, corpo, _ = rete.chiedi(f"{RETE}/accesso/{rete.accesso.impronta}.json", cookie=None)
    assert stato == 200 and json.loads(corpo)["utente"] == "ponte-casa"
    assert rete.chiedi(f"{RETE}/icona.svg", cookie=None)[0] == 200
    assert rete.chiedi(f"{RETE}/accesso/", cookie=None)[0] == 404             # niente elenco
    assert rete.chiedi(f"{RETE}/stato/altro.json")[0] == 404


def test_5_gestione_da_internet_con_il_nome_vero(rete):
    stato, corpo, _ = rete.chiedi(f"{PONTE}/")
    assert stato == 200 and corpo == b"<html>Meshtastic</html>" and not rete.pausa.attiva
    # chi prova a spacciarsi per un altro: Caddy toglie l'intestazione, Authentik mette quella vera
    stato, corpo, _ = rete.chiedi(f"{PONTE}/api/v1/fromradio?all=false", intestazioni={"X-Authentik-Username": "mallory"})
    assert stato == 200 and corpo == b"\x08\x01"
    assert rete.pausa.attiva and rete.g.chi == "da internet (andrea)"
    stato, corpo, _ = rete.chiedi(f"{RETE}/stato/casa.json")
    assert json.loads(corpo)["pausa"]["chi"] == "da internet (andrea)"
    # la sessione di Authentik del browser (valida per tutto il dominio) non arriva al Raspberry
    assert rete.intestazioni_nodo and not [h for h in rete.intestazioni_nodo if "cookie" in h or "authorization" in h]


@pytest.mark.skipif(not shutil.which("node"), reason="serve node con playwright")
def test_5b_la_pagina_della_rete_nel_browser(rete):
    foto = os.environ.get("RETE_FOTO", "")
    senza_proxy = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}   # Chromium userebbe il proxy
    p = subprocess.run(["node", os.path.join(os.path.dirname(__file__), "pagina_rete.js"), f"{RETE}/", foto],
                       capture_output=True, text=True, timeout=120, env=senza_proxy)
    r = json.loads(p.stdout.strip().splitlines()[-1])
    assert "errore" not in r, r
    assert r["errori"] == [] and r["finestre"] == [] and r["immagini"] == 0     # niente CSP violata, niente codice dalle radio
    assert r["stato"] == "pausa" and "da internet (andrea)" in r["testo"]
    assert "07-BIANCHI" in r["testo"] and "<img src=x onerror=alert(1)>" in r["testo"]   # mostrato come testo
    assert "CNSAS-VA (squadra, inoltrato)" in r["testo"]


@pytest.mark.skipif(not shutil.which("node"), reason="serve node con playwright")
def test_5c_la_scheda_delle_radio_nel_browser(rete):
    stato, corpo, _ = rete.chiedi(f"{RETE}/stato/casa.json")
    d = json.loads(corpo)
    assert [r["nome"] for r in d["radio_rete"]] == ["07-BIANCHI", "<img src=x onerror=alert(1)>"]
    assert d["canale_radio"]["occupato"] == 8.2
    foto = os.environ.get("RETE_FOTO", "")
    senza_proxy = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    p = subprocess.run(["node", os.path.join(os.path.dirname(__file__), "pagina_rete_radio.js"), f"{RETE}/",
                        foto.replace(".png", "-radio.png") if foto else ""], capture_output=True, text=True, timeout=120,
                       env=senza_proxy)
    r = json.loads(p.stdout.strip().splitlines()[-1])
    assert "errore" not in r, r
    assert r["errori"] == [] and r["finestre"] == [] and r["immagini"] == 0      # CSP rispettata, niente codice dai nomi
    assert r["visibili"] == [True, False]           # il vecchio indirizzo #radio apre la scheda delle radio
    assert r["mappa"] == [True, False, True] and r["schede"][:3] == ["Rete", "Radio2", "Storico"], r
    assert r["ordine"] == ["<img src=x onerror=alert(1)>", "07-BIANCHI"]          # prima chi ha un problema
    assert "batteria quasi scarica" in r["voci"][0] and "casa: passando da 2 radio" in r["voci"][0]
    assert "SenseCAP T1000-E, con ATAK" in r["voci"][1] and "casa: diretta, segnale buono" in r["voci"][1]
    assert r["riassunto"].startswith("2 radio: 2 sentite nell'ultima ora.")
    assert r["cerca"] == ["07-BIANCHI"] and r["ponti"] == [True, True]
    assert r["ripetitori"].startswith("Ripetitori") and "Nessun ripetitore sentito finora." in r["ripetitori"], r["ripetitori"]


def oggetti_email(rete):
    return [e["subject"] for e in rete.email]


def test_5d_avvisi_per_email(rete):
    """Il servizio (utente senza privilegi) legge lo stato del ponte dal tunnel vero e avvisa con la console di
    infra-TAK: certificato in scadenza (quello di prova scade tra 30 giorni), ponte che non risponde, di nuovo in linea."""
    assert oggetti_email(rete)[:1] == ["[Rete Meshtastic] Email di prova"]          # quella di installa
    assert rete.email[0]["to"] == "" and "https://meshtastic.prova.lan:8443/" in rete.email[0]["body"]
    stato, corpo, _ = rete.chiedi(f"{RETE}/stato/casa.json")
    d = json.loads(corpo)
    assert d["certificato"]["nome"] == "meshtastic-cnsas" and 0 < d["certificato"]["scade"] - time.time() < 31 * 86400
    assert d["sistema"]["avvio"] and d["sistema"]["acceso_dal"] and d["sistema"]["disco"]["totale_mb"] > 0
    assert aspetta(lambda: any("il certificato TAK scade il" in o for o in oggetti_email(rete)), 30), oggetti_email(rete)
    rete.accesso.ferma()                                    # il Raspberry sparisce da internet
    assert aspetta(lambda: "[Rete Meshtastic] Gateway casa: non risponde" in oggetti_email(rete), 40), oggetti_email(rete)
    vista = rete.stato_api()["avvisi"]
    assert vista["problemi"]["casa"][0]["codice"] == "irraggiungibile" and vista["ultimo"]["esito"] == "mandato"
    rete.accesso = rete.avvia_accesso()
    assert aspetta(lambda: rete.accesso.stato()["collegato"], 60), rete.accesso.stato()
    assert aspetta(lambda: "[Rete Meshtastic] Gateway casa: di nuovo in linea" in oggetti_email(rete), 40), oggetti_email(rete)
    assert oggetti_email(rete).count("[Rete Meshtastic] Gateway casa: non risponde") == 1     # una volta sola
    assert rete.stato_api()["avvisi"]["problemi"] == {}


@pytest.mark.skipif(not shutil.which("node"), reason="serve node con playwright")
def test_5e_avvisi_e_salute_del_raspberry_nel_browser(rete):
    prima = len(rete.email)
    senza_proxy = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    p = subprocess.run(["node", os.path.join(os.path.dirname(__file__), "pagina_rete_avvisi.js"), f"{RETE}/"],
                       capture_output=True, text=True, timeout=120, env=senza_proxy)
    r = json.loads(p.stdout.strip().splitlines()[-1])
    assert "errore" not in r, r
    assert r["errori"] == [] and r["finestre"] == []
    assert "Certificato TAK" in r["ponte"] and "scade il" in r["ponte"] and "rinnovalo" in r["ponte"]
    assert "MicroSD di scorta" in r["ponte"] and "sudo mesh2tak-scorta" in r["ponte"]
    assert r["avvisi"].startswith("Avvisi per email") and "Ultimo avviso mandato" in r["avvisi"]
    assert "Email di prova consegnata a infra-TAK" in r["prova"]
    assert len(rete.email) == prima + 1 and "chiesta da andrea" in rete.email[-1]["body"]


def pubblica_release(rete, versione, note="- prova di aggiornamento"):
    rete.github.risposte[f"/repos/{REPO}/releases/latest"] = {
        "tag_name": f"v{versione}", "body": f"## {versione}\n{note}", "published_at": "2026-11-06T10:00:00Z",
        "assets": [{"name": n} for n in FILE_RELEASE]}


def esito_lavoro(rete, lavoro):
    return next((lv for lv in rete.stato_api()["lavori"] if lv["id"] == lavoro), {}).get("esito")


def test_5f_aggiornamento_dei_ponti_dalla_pagina(rete, tmp_path):
    """1.8.0: la pagina legge le release su GitHub; "Aggiorna il ponte" scrive la richiesta nella registrazione del
    ponte (pubblica, servita da Caddy) e mesh2tak-aggiorna sul Raspberry si aggiorna scaricando da GitHub."""
    assert rete.stato_api()["rilasci"]["ultima"] is None
    pubblica_release(rete, m.VERSIONE)                       # il server ce l'ha gia', il Raspberry di prova no
    codice, r = rete.api("POST", "/api/controlla-aggiornamenti", {})
    assert codice == 200 and r["rilasci"]["ultima"]["versione"] == m.VERSIONE and r["rilasci"]["nuova"] is False, r
    assert rete.api("POST", "/api/aggiorna-server", {"versione": m.VERSIONE})[0] == 409
    assert rete.api("POST", "/api/aggiorna-ponti", {"versione": m.VERSIONE, "nome": "casa"}, cookie=None)[0] == 302
    codice, r = rete.api("POST", "/api/aggiorna-ponti", {"versione": m.VERSIONE, "nome": "casa"})
    assert codice == 202, r
    assert aspetta(lambda: esito_lavoro(rete, r["lavoro"]) == "ok", 60), rete.stato_api()["lavori"]
    stato, corpo, _ = rete.chiedi(f"{RETE}/accesso/{rete.accesso.impronta}.json", cookie=None)
    assert stato == 200 and json.loads(corpo)["aggiorna"]["versione"] == m.VERSIONE
    # il Raspberry, con una versione piu' vecchia: mesh2tak-aggiorna come lo lancia il timer
    pi = tmp_path / "pi"
    (pi / "opt").mkdir(parents=True)
    (pi / "opt/mesh2tak.py").write_text('VERSIONE = "1.7.0"\n')
    installatore = (f'#!/bin/bash\n# mesh2tak {m.VERSIONE}\nVERSIONE="{m.VERSIONE}"\n'
                    f'printf \'VERSIONE = "%s"\\n\' "{m.VERSIONE}" > "$MESH2TAK_AGGIORNA_DIR/mesh2tak.py"\necho Fatto.\n').encode()
    base = f"/{REPO}/releases/download/v{m.VERSIONE}"
    rete.github.risposte[f"{base}/installa-ponte-meshtastic.sh"] = installatore
    rete.github.risposte[f"{base}/SHA256SUMS"] = somme({"installa-ponte-meshtastic.sh": installatore})
    (pi / "mesh2tak.conf").write_text(f"[gestione]\nattiva = si\nporta_pagina = {rete.porta_pagina}\n\n[accesso_esterno]\n"
                                      f"attivo = si\nnome = casa\nrete = {RETE}\nchiave = {rete.chiave}\n")
    (pi / "stub").mkdir()
    (pi / "stub/systemctl").write_text("#!/bin/bash\nexit 0\n")
    (pi / "stub/systemctl").chmod(0o755)
    amb = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    amb.update(PATH=f"{pi / 'stub'}:{amb['PATH']}", MESH2TAK_AGGIORNA_CONF=str(pi / "mesh2tak.conf"),
               MESH2TAK_AGGIORNA_DIR=str(pi / "opt"), MESH2TAK_AGGIORNA_STATO=str(pi / "stato"),
               MESH2TAK_AGGIORNA_LOG=str(pi / "aggiornamento.log"), MESH2TAK_PROVA_GITHUB=rete.github.url,
               SSL_CERT_FILE=rete.ca_caddy()[0], MESH2TAK_AGGIORNA_ATTESA="10")
    p = subprocess.run([sys.executable, "-I", os.path.join(RADICE, "aggiorna.py")], env=amb, capture_output=True,
                       text=True, timeout=120)
    registro = (pi / "aggiornamento.log").read_text() if (pi / "aggiornamento.log").exists() else ""
    assert p.returncode == 0, p.stdout + p.stderr + registro
    st = json.loads((pi / "stato/stato.json").read_text())
    assert (st["stato"], st["versione"], st["da"]) == ("fatto", m.VERSIONE, "1.7.0"), st
    # lo stato del ponte, letto dalla pagina della rete attraverso il tunnel, lo dice
    rete.sistema.AGGIORNAMENTO = str(pi / "stato/stato.json")
    rete.sistema.TIMER_AGGIORNA = str(pi / "stub/systemctl")          # c'e': il ponte si aggiorna dalla pagina
    stato, corpo, _ = rete.chiedi(f"{RETE}/stato/casa.json")
    sis = json.loads(corpo)["sistema"]
    assert sis["aggiornabile"] is True and sis["aggiornamento"]["stato"] == "fatto", sis


@pytest.mark.skipif(not shutil.which("node"), reason="serve node con playwright")
def test_5g_versione_nuova_nella_pagina_e_aggiornamento_del_server(rete):
    """La pagina dice che e' uscita una versione nuova, con le novita'; "Aggiorna il server" chiede conferma e accoda
    il lavoro; il server scarica da GitHub e, se il file non e' quello pubblicato, non lo installa e lo dice."""
    n = [int(x) for x in m.VERSIONE.split(".")]
    nuova = f"{n[0]}.{n[1] + 1}.0"
    pubblica_release(rete, nuova, "- una novita' di prova\n  che va a capo\n- un'altra")
    base = f"/{REPO}/releases/download/v{nuova}"
    rete.github.risposte[f"{base}/rete-meshtastic.sh"] = b"#!/bin/bash\necho manomesso\n"
    rete.github.risposte[f"{base}/SHA256SUMS"] = somme({"rete-meshtastic.sh": b"quello pubblicato"})
    senza_proxy = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    p = subprocess.run(["node", os.path.join(os.path.dirname(__file__), "pagina_rete_aggiorna.js"), f"{RETE}/"],
                       capture_output=True, text=True, timeout=180, env=senza_proxy)
    r = json.loads(p.stdout.strip().splitlines()[-1])
    assert "errore" not in r, r
    assert r["errori"] == [] and r["finestre"] == []
    assert f"È uscita la versione {nuova}" in r["sezione"] and "una novita' di prova che va a capo" in r["sezione"], r["sezione"]
    assert f"Aggiorna il server alla {nuova}" in r["sezione"] and "Prima il server, poi i gateway" in r["sezione"]
    assert f"È uscita la versione {nuova}: aggiorna il server." in r["in_cima"]
    assert r["domanda"] == f"Aggiornare il server alla {nuova}?"
    assert f"Aggiornamento del server alla {nuova}: non riuscita" in r["lavoro"], r["lavoro"]
    assert "non e' quello pubblicato" in r["lavoro"] or "non è quello pubblicato" in r["lavoro"], r["lavoro"]
    assert "Aggiornamento" in r["ponte"] and f"alla {m.VERSIONE}" in r["ponte"], r["ponte"]
    assert not (rete.r / f"etc/mesh2tak-rete/rete-meshtastic-{nuova}.sh").exists()
    assert rete.github.richieste.count(f"{base}/SHA256SUMS") == 1


def test_6_l_utente_del_ponte_non_puo_fare_altro(rete):
    k = str(rete.chiave)
    base = ["ssh", "-F", "/dev/null", "-i", k, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-p", "2222"]
    # nessun comando
    p = subprocess.run(base + ["ponte-casa@127.0.0.1", "id; cat /etc/shadow"], capture_output=True, text=True, timeout=20)
    assert "uid=" not in p.stdout and "root:" not in p.stdout
    # nessuna altra porta in ascolto sul server
    p = subprocess.run(base + ["-N", "-o", "ExitOnForwardFailure=yes", "-R", "127.0.0.1:19999:127.0.0.1:22",
                               "ponte-casa@127.0.0.1"], capture_output=True, text=True, timeout=20)
    assert p.returncode != 0 and "remote port forwarding failed" in p.stderr
    # nessun accesso ai servizi interni del server (es. Authentik su 127.0.0.1)
    locale = porta_libera()
    tunnel = subprocess.Popen(base + ["-N", "-L", f"127.0.0.1:{locale}:127.0.0.1:{rete.porta_ak}", "ponte-casa@127.0.0.1"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        aspetta_porta(locale)
        with socket.create_connection(("127.0.0.1", locale), timeout=5) as s:
            s.sendall(b"GET /api/v3/core/applications/ HTTP/1.0\r\nAuthorization: Bearer " + TOKEN.encode() + b"\r\n\r\n")
            s.settimeout(5)
            try:
                risposta = s.recv(100)
            except (ConnectionResetError, socket.timeout):
                risposta = b""
        assert not risposta.startswith(b"HTTP")
    finally:
        tunnel.terminate()
        tunnel.wait(5)
    assert "refused local port forward" in rete.sshd_log() or "open failed" in tunnel.stderr.read()


def ssh_ponte(chiave, utente, *comando, attesa=30):
    return subprocess.run(["ssh", "-F", "/dev/null", "-i", str(chiave), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                           "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-p", "2222",
                           f"{utente}@127.0.0.1", *comando], capture_output=True, text=True, timeout=attesa,
                          stdin=subprocess.DEVNULL)


def test_6b_il_comando_dei_ponti_dal_vero(rete):
    """1.10.0: con sshd vero un ponte puo' solo leggere la sua configurazione e scrivere a che punto e'."""
    assert pwd.getpwnam("ponte-casa").pw_shell == "/bin/sh"          # per sshd, che lancia il comando forzato
    p = ssh_ponte(rete.chiave, "ponte-casa", "configurazione")
    assert p.returncode == 3 and "nessuna configurazione" in p.stderr and p.stdout == ""
    p = ssh_ponte(rete.chiave, "ponte-casa", "avanzamento installo 3/9 Meshtastic (prova)")
    assert p.returncode == 0, p.stderr
    f = rete.r / "var/lib/mesh2tak-rete/avanzamento/casa.json"
    assert pwd.getpwuid(f.stat().st_uid).pw_name == "ponte-casa" and oct(f.stat().st_mode)[-3:] == "644"
    assert rete.stato_api()["installazioni"]["casa"]["testo"] == "3/9 Meshtastic (prova)"
    # niente terminale, nessun altro comando (neanche nascosto nel testo), niente sftp
    for comando in ("sh", "/bin/sh -c id", "configurazione; id", "id"):
        p = ssh_ponte(rete.chiave, "ponte-casa", comando)
        assert p.returncode != 0 and "uid=" not in p.stdout + p.stderr, comando
    p = ssh_ponte(rete.chiave, "ponte-casa", "avanzamento installo $(id) `id`")
    assert p.returncode == 0 and rete.stato_api()["installazioni"]["casa"]["testo"] == "$(id) `id`"
    # sftp come lo chiede il programma sftp (sottosistema): il primo messaggio SFTP (INIT, versione 3) non ha risposta
    p = subprocess.run(["ssh", "-F", "/dev/null", "-i", str(rete.chiave), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                        "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-p", "2222", "-s",
                        "ponte-casa@127.0.0.1", "sftp"], input=b"\x00\x00\x00\x05\x01\x00\x00\x00\x03",
                       capture_output=True, timeout=30)
    assert p.returncode != 0 and b"\x02\x00\x00\x00\x03" not in p.stdout, (p.returncode, p.stdout[:40])
    p = ssh_ponte(rete.chiave, "ponte-casa", "-tt", "configurazione")
    assert "PTY allocation request failed" in p.stderr or p.returncode == 3
    # il tunnel del ponte intanto non si e' mai chiuso
    assert rete.accesso.stato()["collegato"]
    f.unlink()
    assert "casa" not in rete.stato_api()["installazioni"]


def aspetta_operazione(rete, ponte, op, limite=30):
    fine = time.time() + limite
    while time.time() < fine:
        codice, d = rete.api("GET", f"/radio/{ponte}/op?id={op}")
        assert codice == 200, d
        if d["stato"] in ("fatto", "errore"):
            return d
        time.sleep(0.2)
    raise AssertionError("operazione mai finita")


def test_6c_radio_gestite_dalla_pagina_attraverso_il_ponte(rete):
    """1.10.0: dalla pagina della rete (Caddy, Authentik) al tunnel, al ponte, al suo nodo e via radio alla radio."""
    from test_gestione_radio import IfaceRadio, RadioLontana
    rete.lontana = RadioLontana()
    rete.ponte.iface = IfaceRadio(rete.lontana)
    rete.ponte.nodo_chiave_pubblica = base64.b64encode(b"N" * 32).decode()
    rete.g.radio = m.GestioneRadio(rete.ponte)
    rete.g.radio.ATTESA_RISPOSTA_S = 3.0
    stato, corpo, _ = rete.chiedi(f"{RETE}/stato/casa.json")
    d = json.loads(corpo)
    assert d["gestione"]["radio"] is True and d["chiave_pubblica"] == rete.ponte.nodo_chiave_pubblica
    leggi = {"radio": "!7c3a91e2", "azione": "leggi", "sezioni": ["proprietario", "info"]}
    codice, r = rete.api("POST", "/radio/casa/op", leggi)
    assert codice == 202, r
    op = aspetta_operazione(rete, "casa", r["op"])
    assert op["stato"] == "fatto" and op["risultato"]["proprietario"] == {"nome": "Rip Velate", "nome_breve": "VELA"}
    assert op["chi"] == "andrea" and op["nome"] == "07-BIANCHI"          # il nome vero, messo da Authentik
    # senza sessione ad Authentik; da un altro sito o senza l'intestazione della pagina: rifiutata
    assert rete.api("POST", "/radio/casa/op", leggi, cookie=None)[0] == 302
    assert rete.api("POST", "/radio/casa/op", leggi, intestazioni={"X-Mesh2tak": ""})[0] == 403
    assert rete.api("POST", "/radio/casa/op", leggi, intestazioni={"Origin": "https://cattivo.example"})[0] == 403
    assert rete.api("POST", "/radio/casa/op", leggi, intestazioni={"X-Authentik-Username": "mallory"})[0] == 202
    assert rete.api("GET", "/radio/casa/ops")[1]["operazioni"][-1]["chi"] == "andrea"
    assert rete.api("GET", "/radio/altro/schema")[0] == 404           # ponte che non c'e'
    assert rete.api("POST", "/radio/casa/op", {"radio": "!12345678", "azione": "riavvia"})[0] == 400
    codice, schema = rete.api("GET", "/radio/casa/schema")
    assert codice == 200 and any(x["nome"] == "lora" for x in schema["sezioni"])
    # troppo grande per Caddy
    codice, _ = rete.api("POST", "/radio/casa/op", dict(leggi, riempitivo="x" * 20000))
    assert codice == 413


@pytest.mark.skipif(not shutil.which("node"), reason="serve node con playwright")
def test_6d_radio_gestite_dal_browser(rete):
    foto = os.environ.get("RETE_FOTO", "")
    senza_proxy = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    p = subprocess.run(["node", os.path.join(os.path.dirname(__file__), "pagina_rete_gestione_radio.js"), f"{RETE}/",
                        foto.replace(".png", "-gestione-radio.png") if foto else ""], capture_output=True, text=True,
                       timeout=180, env=senza_proxy)
    r = json.loads(p.stdout.strip().splitlines()[-1])
    assert "errore" not in r, r
    assert r["errori"] == [], r["errori"]
    assert dict(r["bottoni"]) == {"07-BIANCHI": True, "<img src=x onerror=alert(1)>": True}
    assert r["titolo"] == "07-BIANCHI" and r["chiaveAdmin"] == base64.b64encode(b"N" * 32).decode()
    assert r["lettura"][0] == "fatto" and r["nome"] == ["Rip Velate", "VELA"]
    assert r["letturaSezione"][0] == "fatto" and r["salvataggio"][0] == "fatto", r["salvataggio"]
    assert rete.lontana.position.position_broadcast_secs == 300 and rete.lontana.position.fixed_position is False
    assert len(r["finestre"]) == 1 and "Salvo" in r["finestre"][0]            # ha chiesto conferma
    assert r["canali"][0] == "fatto" and r["moduliCanali"][:2] == ["Canale 0: CNSAS", "Canale 1"]
    assert r["sceltaChiave0"].startswith("Lascia la chiave com'è (privata, uguale a quella del canale")
    assert base64.b64encode(bytes(range(32))).decode() not in r["testoFinestra"]
    assert r["chiusa"]


def test_6e_icona_scelta_sulla_pagina_arriva_al_ponte(rete):
    """1.11.0: l'icona scelta sulla pagina della rete arriva al ponte dal suo tunnel (il servizio, al giro della
    sorveglianza) e il ponte la usa per quella radio; figure ed elenco dalla pagina, dietro Authentik."""
    codice, r = rete.api("POST", "/api/icona", {"radio": "!7c3a91e2", "icona": "cinofilo"})
    assert codice == 200, r
    assert aspetta(lambda: rete.ponte.icone.di("!7c3a91e2") == ("a-f-G-U-C-I", CANE), 30)
    assert aspetta(lambda: rete.stato_api()["icone"]["ponti"].get("casa") == "aggiornato", 30)
    st = rete.stato_api()["icone"]
    assert st["scelte"]["!7c3a91e2"]["icona"] == "cinofilo" and st["scelte"]["!7c3a91e2"]["da"] == "andrea"
    stato, corpo, _ = rete.chiedi(f"{RETE}/stato/casa.json")
    assert json.loads(corpo)["icone"] == {"versione": st["versione"], "numero": 1}
    assert json.loads((rete.c / "pi" / "icone.json").read_text())["versione"] == st["versione"], "tenute su file"
    # elenco e figure per la pagina: solo con Authentik
    stato, corpo, _ = rete.chiedi(f"{RETE}/icone-mappa.json")
    elenco = {x["chiave"]: x for x in json.loads(corpo)["icone"]}
    assert stato == 200 and elenco["cinofilo"]["figura"] == "icone/cinofilo.png" and elenco["sci"]["figura"] == ""
    stato, figura, _ = rete.chiedi(f"{RETE}/icone/cinofilo.png")
    assert stato == 200 and figura == FIGURA_CANE
    assert rete.chiedi(f"{RETE}/icone/cinofilo.png", cookie=None)[0] == 302
    # le icone si scelgono solo dalla pagina (Authentik e intestazione), non da un altro sito
    assert rete.api("POST", "/api/icona", {"radio": "!7c3a91e2", "icona": "sci"}, cookie=None)[0] == 302
    assert rete.api("POST", "/api/icona", {"radio": "!7c3a91e2", "icona": "sci"}, intestazioni={"X-Mesh2tak": ""})[0] == 403
    # tornata la predefinita: anche il ponte torna a quella
    assert rete.api("POST", "/api/icona", {"radio": "!7c3a91e2", "icona": ""})[0] == 200
    assert aspetta(lambda: rete.ponte.icone.di("!7c3a91e2") is None, 30)


@pytest.mark.skipif(not shutil.which("node"), reason="serve node con playwright")
def test_6f_icona_scelta_dal_browser(rete):
    foto = os.environ.get("RETE_FOTO", "")
    senza_proxy = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    p = subprocess.run(["node", os.path.join(os.path.dirname(__file__), "pagina_rete_icone.js"), f"{RETE}/",
                        foto.replace(".png", "-icone.png") if foto else ""], capture_output=True, text=True,
                       timeout=180, env=senza_proxy)
    r = json.loads(p.stdout.strip().splitlines()[-1])
    assert "errore" not in r, r
    assert r["errori"] == [] and r["finestre"] == [], r["errori"]
    assert r["titolo"] == "Icona di 07-BIANCHI sulla mappa"
    assert r["gruppi"][:2] == ["Persone", "Mezzi"] and "Unità cinofila" in r["tessere"]
    assert r["sceltaPrima"] == "Predefinita"
    assert r["esito"].startswith("Icona salvata") and r["sceltaDopo"] == "Unità cinofila"
    assert r["bottoneDopo"] == "Icona: Unità cinofila" and r["figuraNelBottone"].endswith("icone/cinofilo.png")
    assert r["titoloPonte"] == "Icona di gateway casa sulla mappa" and r["predefinitaPonte"] == "Traliccio (predefinita)"
    assert rete.stato_api()["icone"]["scelte"]["!7c3a91e2"]["icona"] == "cinofilo"
    assert aspetta(lambda: rete.ponte.icone.di("!7c3a91e2") == ("a-f-G-U-C-I", CANE), 30)
    rete.api("POST", "/api/icona", {"radio": "!7c3a91e2", "icona": ""})


def prepara_comandi(rete):
    """Il lato root del Raspberry per "Gestisci il Raspberry" (1.13.0): mesh2tak-aggiorna con le sue cartelle, un
    installatore e programmi di sistema finti; il ponte di prova legge da li' esiti e registro. Lo stesso per tutte le
    prove: i comandi gia' fatti restano fatti."""
    pi = rete.c / "pi-root"
    (pi / "opt").mkdir(parents=True, exist_ok=True)
    (pi / "opt/mesh2tak.py").write_text(f'VERSIONE = "{m.VERSIONE}"\n')
    (pi / "opt/installatore.sh").write_text(
        '#!/bin/bash\necho "installatore $* password=$MESH2TAK_PASSWORD host=$MESH2TAK_HOST" >> "$PROVA_LANCI"\n'
        'cp "$1" "$PROVA_LANCI.pacchetto"\necho "  ✔ certificato \'meshtastic-cnsas\' (scade: Dec 11 12:00:00 2026 GMT)"\n')
    (pi / "mesh2tak.conf").write_text(f"[tak]\nhost = takserver\n\n[mappa]\nsquadra = Cyan\n\n[gestione]\nattiva = si\n"
                                      f"porta_pagina = {rete.porta_pagina}\n\n[accesso_esterno]\nattivo = si\nnome = casa\n"
                                      f"rete = {RETE}\nchiave = {rete.chiave}\n")
    (pi / "stub").mkdir(exist_ok=True)
    for nome, corpo in (("systemctl", 'echo "systemctl $*" >> "$PROVA_LANCI"\nexit 0\n'),
                        ("journalctl", 'echo "INFO mesh2tak: canale con psk=AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="\n')):
        (pi / "stub" / nome).write_text("#!/bin/bash\n" + corpo)
        (pi / "stub" / nome).chmod(0o755)
    amb = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    amb.update(PATH=f"{pi / 'stub'}:{amb['PATH']}", MESH2TAK_AGGIORNA_CONF=str(pi / "mesh2tak.conf"),
               MESH2TAK_AGGIORNA_DIR=str(pi / "opt"), MESH2TAK_AGGIORNA_STATO=str(pi / "stato"),
               MESH2TAK_AGGIORNA_LOG=str(pi / "aggiornamento.log"), MESH2TAK_PROVA_GITHUB=rete.github.url,
               SSL_CERT_FILE=rete.ca_caddy()[0], MESH2TAK_AGGIORNA_ATTESA="10", PROVA_LANCI=str(pi / "lanci"),
               MESH2TAK_AGGIORNA_LOG_INST=str(pi / "installazione.log"))
    # il ponte di prova: comandi possibili (c'e' l'unita' .path), esiti e registro di questo Raspberry
    rete.sistema.SVEGLIA_AGGIORNA = str(pi / "opt/mesh2tak.py")
    rete.sistema.AZIONI = str(pi / "stato/azioni.json")
    rete.sistema.REGISTRO = rete.g.file_registro = str(pi / "stato/registro.txt")
    return pi, amb


def lancia_aggiorna(amb):
    return subprocess.run([sys.executable, "-I", os.path.join(RADICE, "aggiorna.py")], env=amb, capture_output=True,
                          text=True, timeout=180)


def lavoro_della_pagina(rete, lavoro):
    return next((lv for lv in rete.stato_api()["lavori"] if lv["id"] == lavoro), {})


def test_6g_gestisci_il_raspberry_dalla_pagina(rete, pki):
    """1.13.0: il comando dalla pagina (Authentik) va nella registrazione del ponte, il server avvisa il ponte dal
    tunnel, mesh2tak-aggiorna lo fa; il registro torna alla pagina dal tunnel; il certificato nuovo il Raspberry lo
    scarica con ssh vero dal comando forzato del suo utente, e sul server non resta."""
    pi, amb = prepara_comandi(rete)
    if os.path.exists(rete.g.file_controlla):
        os.unlink(rete.g.file_controlla)
    codice, r = rete.api("POST", "/api/gestisci-ponte", {"nome": "casa", "azione": "registro"})
    assert codice == 202, r
    assert aspetta(lambda: lavoro_della_pagina(rete, r["lavoro"]).get("esito") == "ok", 60), rete.esiti_lavora[-2:]
    assert lavoro_della_pagina(rete, r["lavoro"])["messaggio"] == "chiesto al gateway: lo fa subito"
    assert os.path.exists(rete.g.file_controlla), "avvisato dal tunnel"
    stato, corpo, _ = rete.chiedi(f"{RETE}/accesso/{rete.accesso.impronta}.json", cookie=None)
    [a] = json.loads(corpo)["azioni"]
    assert a["id"] == r["lavoro"] and a["azione"] == "registro" and "andrea" not in corpo.decode()
    p = lancia_aggiorna(amb)
    assert p.returncode == 0, p.stdout + p.stderr
    stato, corpo, _ = rete.chiedi(f"{RETE}/stato/casa.json")
    sis = json.loads(corpo)["sistema"]
    assert sis["comandi"] is True and sis["registro"]["byte"] > 0, sis
    assert [(x["id"], x["azione"], x["stato"]) for x in sis["azioni"]] == [(r["lavoro"], "registro", "fatto")]
    stato, testo, intestazioni = rete.chiedi(f"{RETE}/registro/casa.txt")
    assert stato == 200 and b"Registro del gateway" in testo and b"psk=[tolto]" in testo, testo[:300]
    assert intestazioni.get("Content-Type", "").startswith("text/plain") and "Access-Control-Allow-Origin" not in intestazioni
    assert rete.chiedi(f"{RETE}/registro/casa.txt", cookie=None)[0] == 302
    assert rete.chiedi(f"{RETE}/registro/altro.txt")[0] == 404
    # comandi solo dalla pagina e solo quelli previsti
    assert rete.api("POST", "/api/gestisci-ponte", {"nome": "casa", "azione": "registro"}, cookie=None)[0] == 302
    assert rete.api("POST", "/api/gestisci-ponte", {"nome": "casa", "azione": "registro"},
                    intestazioni={"Origin": "https://cattivo.example"})[0] == 403
    assert rete.api("POST", "/api/gestisci-ponte", {"nome": "casa", "azione": "shell"})[0] == 400
    # certificato nuovo: password sbagliata, il server non lo passa al Raspberry
    p12 = base64.b64encode((pki / "meshtastic-cnsas.p12").read_bytes()).decode()
    codice, r = rete.api("POST", "/api/certificato-ponte", {"nome": "casa", "nome_pacchetto": "meshtastic-cnsas.p12",
                                                            "pacchetto": p12, "password": "sbagliata"})
    assert codice == 202, r
    assert aspetta(lambda: lavoro_della_pagina(rete, r["lavoro"]).get("esito") == "errore", 60)
    assert "password del certificato sbagliata" in lavoro_della_pagina(rete, r["lavoro"])["messaggio"]
    f = rete.r / "etc/mesh2tak-rete/configurazioni/casa.certificato.json"
    assert not f.exists()
    codice, r = rete.api("POST", "/api/certificato-ponte", {"nome": "casa", "nome_pacchetto": "meshtastic-cnsas.p12",
                                                            "pacchetto": p12, "password": "atakatak"})
    assert codice == 202, r
    assert aspetta(lambda: lavoro_della_pagina(rete, r["lavoro"]).get("esito") == "ok", 60), rete.esiti_lavora[-2:]
    assert lavoro_della_pagina(rete, r["lavoro"])["messaggio"] == ("Certificato 'meshtastic-cnsas' pronto per il gateway "
                                                                  "casa: lo fa subito.")
    assert pwd.getpwuid(f.stat().st_uid).pw_name == "ponte-casa" and oct(f.stat().st_mode)[-3:] == "400"
    assert "atakatak" not in json.dumps(rete.stato_api())
    p = lancia_aggiorna(amb)                                 # ssh vero verso sshd vero, con la chiave del ponte
    assert p.returncode == 0, p.stdout + p.stderr
    esiti = {x["id"]: x for x in json.loads((pi / "stato/azioni.json").read_text())["azioni"]}
    assert esiti[r["lavoro"]]["stato"] == "fatto", esiti
    assert esiti[r["lavoro"]]["messaggio"] == "certificato sostituito: 'meshtastic-cnsas', scade Dec 11 12:00:00 2026 GMT"
    assert "password=atakatak host=takserver" in (pi / "lanci").read_text()
    assert (pi / "lanci.pacchetto").read_bytes() == (pki / "meshtastic-cnsas.p12").read_bytes()
    assert not f.exists(), "consegnato: tolto dal server"
    assert not (pi / "stato/certificato").exists(), "e dal Raspberry"
    assert "atakatak" not in (pi / "aggiornamento.log").read_text()
    assert rete.accesso.stato()["collegato"], "il tunnel intanto resta aperto"


@pytest.mark.skipif(not shutil.which("node"), reason="serve node con playwright")
def test_6h_gestisci_il_raspberry_dal_browser(rete):
    """1.13.0 nel browser, con l'unita' .path del Raspberry simulata: quando il ponte segna il file, mesh2tak-aggiorna
    parte e fa il comando."""
    pi, amb = prepara_comandi(rete)
    fermo = threading.Event()

    def sveglia():
        ultimo = None
        while not fermo.wait(0.3):
            try:
                t = os.stat(rete.g.file_controlla).st_mtime_ns
            except OSError:
                continue
            if t != ultimo:
                ultimo = t
                lancia_aggiorna(amb)
    threading.Thread(target=sveglia, daemon=True).start()
    try:
        foto = os.environ.get("RETE_FOTO", "")
        senza_proxy = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
        p = subprocess.run(["node", os.path.join(os.path.dirname(__file__), "pagina_rete_raspberry.js"), f"{RETE}/",
                            foto.replace(".png", "-raspberry.png") if foto else ""], capture_output=True, text=True,
                           timeout=300, env=senza_proxy)
    finally:
        fermo.set()
    r = json.loads(p.stdout.strip().splitlines()[-1])
    assert "errore" not in r, (r, rete.esiti_lavora[-2:])
    assert r["errori"] == [], r["errori"]
    assert r["titolo"] == "Raspberry del gateway casa"
    assert r["sezioni"] == ["Riavvia", "Registro", "Impostazioni del gateway", "Certificato del TAK Server",
                            "Radio del gateway", "Ultimi comandi"]
    assert (r["squadra"], r["validita"], r["invio"]) == ("Cyan", "90", True)
    assert "valido fino al" in r["certificato"]
    assert "Registro del gateway" in r["registro"] and r["scarica"].startswith("registro-casa-")
    assert r["statoRegistro"].startswith("Registro pronto (")
    assert r["esitoImpostazioni"] == "fatto", r["statoImpostazioni"]
    assert r["statoImpostazioni"] == "Impostazioni salvate: squadra = Red.", r["statoImpostazioni"]
    assert len(r["finestre"]) == 1 and "Se non riparte, il Raspberry rimette da solo quelle di prima" in r["finestre"][0]
    assert r["comandi"][0].startswith("Impostazioni: fatto") and r["comandi"][1].startswith("Registro: fatto")
    assert r["chiusa"] and "Ultimo comando" in r["ultimoComando"] and "Impostazioni: fatto" in r["ultimoComando"]
    assert "squadra = Red" in (pi / "mesh2tak.conf").read_text()


RIPETITORE = 0x3333CCCC


def prepara_copertura(rete):
    """1.14.0: la mappa della copertura del ponte casa, con cinque quadretti: vicino al ponte buono, poi discreto,
    debole e al limite, e uno arrivato solo tramite un'altra radio. Dalla 1.15.0 anche un ripetitore fisso della
    squadra (ruolo Router, posizione impostata) con un sesto quadretto raggiunto tramite lui."""
    p = rete.ponte
    ora = time.time()
    p.squadra.segna(RIPETITORE, ora)
    with p._lock:
        v = p._vista_di(RIPETITORE)
        v.nome, v.nome_breve, v.modello, v.ruolo = "Ripetitore Velate", "RVEL", "THINKNODE_M6", "ROUTER"
        v.ultimo, v.lat, v.lon, v.posizione_fissa = ora - 600, 45.875, 8.805, True
        v.ripetuti, v.ultimo_ripetuto = 2, ora - 60
    p.copertura = m.Copertura(str(rete.c / "pi" / "copertura.json"))
    rete.g.copertura = p.copertura_per_la_pagina
    if p.iface is None:
        p.iface = type("IfaceFinta", (), {"nodesByNum": {}})()
    p.iface.nodesByNum[p.mio_num] = {"position": {"latitudeI": 458206000, "longitudeI": 88251000}}
    oggi = int(time.strftime("%Y%m%d"))
    for dlat, snr in ((0.0, 6.0), (0.004, -9.0), (0.008, -14.0), (0.012, -16.5)):
        for _ in range(3):
            p.copertura.annota(45.8206 + dlat, 8.8281, 0, snr, -100, oggi)
    p.copertura.annota(45.8406, 8.8251, 2, None, None, oggi)
    for _ in range(2):
        p.copertura.annota(45.8606, 8.8051, 1, None, None, oggi, via=RIPETITORE)
    # storico (1.16.0): tre mezz'ore del gateway e del ripetitore
    with p.storico._lock:
        p.storico.ponte = [[round(ora - 1800 * i), 4.0 + i, 0.5, 2, 1] for i in (3, 2, 1)]
        p.storico.ripetitori = {"!3333cccc": {"nome": "Ripetitore Velate", "campioni": [
            [round(ora - 1800 * i), 70 + i, 3.9, -7.5 - i, 1 if i != 2 else 0, i] for i in (3, 2, 1)]}}
    return oggi


def test_6i_la_copertura_dalla_pagina(rete):
    """1.14.0: la copertura del ponte dal tunnel, solo per gli amministratori; azzerarla solo dalla pagina stessa."""
    prepara_copertura(rete)
    stato, corpo, intestazioni = rete.chiedi(f"{RETE}/copertura/casa.json")
    assert stato == 200, corpo
    d = json.loads(corpo)
    assert d["ponte"] == {"lat": 45.8206, "lon": 8.8251} and d["limite_snr"] == -17.5 and d["preset"] == "LONG_FAST"
    assert sorted(q[3] for q in d["celle"] if q[3] is not None) == [-16.5, -14.0, -9.0, 6.0] and len(d["celle"]) == 6
    assert [q[8] for q in d["celle"] if q[8]] == [{"!3333cccc": 2}] and d["formato"] == 2
    assert d["ripetitori"] == [{"id": "!3333cccc", "nome": "Ripetitore Velate", "nome_breve": "RVEL", "lat": 45.875, "lon": 8.805}]
    assert intestazioni.get("Content-Type", "").startswith("application/json")
    assert "Access-Control-Allow-Origin" not in intestazioni and intestazioni.get("Cache-Control") == "no-store"
    assert rete.chiedi(f"{RETE}/copertura/casa.json", cookie=None)[0] == 302, "senza Authentik: al login"
    assert rete.chiedi(f"{RETE}/copertura/altro.json")[0] == 404
    assert rete.api("POST", "/copertura/casa.json", {"azzera": True}, intestazioni={"X-Mesh2tak": ""})[0] == 403
    assert rete.api("POST", "/copertura/casa.json", {"azzera": True},
                    intestazioni={"Origin": "https://cattivo.example"})[0] == 403
    assert rete.api("POST", "/copertura/casa.json", {"azzera": False})[0] == 400
    assert len(rete.ponte.copertura.dati()["celle"]) == 6, "niente azzerato"


@pytest.mark.skipif(not shutil.which("node"), reason="serve node con playwright")
def test_6j_la_copertura_nel_browser(rete):
    oggi = prepara_copertura(rete)
    foto = os.environ.get("RETE_FOTO", "")
    senza_proxy = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    p = subprocess.run(["node", os.path.join(os.path.dirname(__file__), "pagina_rete_copertura.js"), f"{RETE}/",
                        foto.replace(".png", "-copertura.png") if foto else ""], capture_output=True, text=True,
                       timeout=300, env=senza_proxy)
    r = json.loads(p.stdout.strip().splitlines()[-1])
    assert "errore" not in r, r
    assert r["errori"] == [], r["errori"]
    assert r["prima"] == [True, True], "si apre sulla scheda Rete: mappa, gateway e ripetitori insieme"
    assert r["schede"][:3] == ["Rete", "Radio2", "Storico"], r["schede"]
    assert r["schede"][3] in ("Gestione", "Gestione!"), "il punto esclamativo: c'e' da aggiornare"
    assert r["prevista"] == ("https://site.meshtastic.org/?lat=45.8750&lon=8.8050&name=Ripetitore+Velate&tx_freq=869.525"
                             "&tx_power=0.158&tx_height=3&max_range=30&run=1"), r["prevista"]
    assert [x[0] for x in r["storico"]] == ["Gateway casa", "Ripetitore Velate"] and r["linee"] == 4, r["storico"]
    assert r["storico"][0][1][0].startswith("Canale radio occupato ultimo 5%, minimo 5%, massimo 7%"), r["storico"]
    assert r["storico"][1][1][0].startswith("Batteria ultimo 71%, minimo 71%, massimo 73%"), r["storico"]
    assert r["storico"][1][1][2] == "Sentito dal gateway in 2 mezz'ore su 3 (67%)", r["storico"]
    assert r["riassunto"] == ("6 quadretti: buono 1, discreto 1, debole 1, al limite 1, tramite ripetitore 1, "
                              "tramite altre radio 1."), r["riassunto"]
    assert r["legenda"] == ["Buono", "Discreto", "Debole", "Al limite", "Tramite un ripetitore", "Tramite altre radio",
                            "Gateway", "Ripetitore"]
    assert r["rete"][0].startswith("Gateway casa ") and re.match(r"Ripetitore Velate attivo \d+ min fa", r["rete"][1]), r["rete"]
    assert "batteria non inviata" in r["rete"][1] and r["rete"][2].startswith("Radio della squadra"), r["rete"]
    assert r["coperturaGateway"] == ("6 quadretti: buono 1, discreto 1, debole 1, al limite 1, tramite ripetitore 1, "
                                     "tramite altre radio 1"), r["coperturaGateway"]
    assert "ThinkNode M6, ruolo ripetitore, !3333cccc" in r["ripetitore"] and "Acceso" in r["ripetitore"], r["ripetitore"]
    assert "non la manda" in r["ripetitore"] and "Device Metrics" in r["ripetitore"], r["ripetitore"]
    assert "1 quadretto raggiunto tramite questo ripetitore" in r["ripetitore"], r["ripetitore"]
    assert r["mostra"] == [["", "Tutta la rete"], ["g:casa", "Gateway casa"], ["r:!3333cccc", "Ripetitore Velate"]], r["mostra"]
    # la mappa di sfondo: la pagina la puo' caricare (CSP) e dice solo da che sito arriva, non quale pagina
    assert r["sfondo"] and all("tile.opentopomap.org/" in x["url"] for x in r["sfondo"]), r["sfondo"]
    assert all(x["referer"] == RETE + "/" for x in r["sfondo"]), r["sfondo"]
    assert r["sfondoCaricato"] > 0 and "OpenTopoMap" in r["crediti"] and "OpenStreetMap" in r["crediti"]
    assert r["buono"].startswith("Buono: i messaggi arrivano sicuri"), r["buono"]
    assert "Gateway casa: buono, segnale medio 6 dB (migliore 6 dB), sentita diretta 3 volte." in r["buono"]
    assert r["ripetuta"].startswith("Tramite altre radio: il gateway non la sentiva diretta"), r["ripetuta"]
    assert "Gateway casa: tramite altre radio 1 volta." in r["ripetuta"]
    assert r["viaRipetitore"].startswith("Tramite un ripetitore: un ripetitore della rete l'ha portata al gateway")
    assert "Gateway casa: tramite Ripetitore Velate 2 volte." in r["viaRipetitore"], r["viaRipetitore"]
    assert re.match(r"Ripetitore Velate: attivo \d+ min fa", r["segnoRipetitore"]), r["segnoRipetitore"]
    assert r["centrato"]["zoom"] >= 13 and abs(r["centrato"]["dx"]) <= 2 and abs(r["centrato"]["dy"]) <= 2, r["centrato"]
    assert r["zoomLontano"] == r["zoomAdattata"] - 3 and 1 <= r["disegnatiLontano"] <= 6
    assert r["zoomTastiera"] == r["zoomAdattata"] - 2
    assert r["kmlNome"] == f"copertura-rete-{oggi}.kml" and r["statoKml"].startswith("Scaricato copertura-rete-")
    assert r["kml"].count("<Placemark>") == 6 and "<name>Copertura Meshtastic, tutta la rete</name>" in r["kml"]
    assert '<Style id="buono">' in r["kml"] and "<color>990ca30c</color>" in r["kml"]
    assert '<Style id="ripetitore">' in r["kml"] and "<styleUrl>#ripetitore</styleUrl>" in r["kml"]
    assert re.search(r"<coordinates>8\.82\d+,45\.82\d+ ", r["kml"]), "longitudine, latitudine"
    assert r["oggi"] == "6" and "OpenStreetMap" in r["creditiStrade"] and "OpenTopoMap" not in r["creditiStrade"]
    assert r["sfondoScelto"] == ["false", "true"]
    assert r["soloRipetitore"] == ["1", "1 quadretto: tramite ripetitore 1.", False], r["soloRipetitore"]
    assert r["azzeraPrima"] is False and r["azzeraBottone"] == "Azzera la copertura di casa"
    assert len(r["finestre"]) == 1 and r["finestre"][0].startswith("Cancello la copertura raccolta dal gateway casa?")
    assert r["statoAzzera"] == "Copertura del gateway casa azzerata: ricomincia da adesso."
    assert r["riassuntoDopo"].startswith("Nessun quadretto") and rete.ponte.copertura.dati()["celle"] == []


@pytest.mark.skipif(not shutil.which("node"), reason="serve node con playwright")
def test_7_un_altro_raspberry_approvato_dal_browser(rete, tmp_path):
    altra = nuova_chiave(tmp_path, "altra")
    tentativi = rete.sshd_log().count("Connection from")
    a = rete.avvia_accesso(altra, nome="altro")
    rete.altri.append(a)
    assert aspetta(lambda: a.stato()["richiesta"] == "in_attesa", 30), a.stato()
    assert nessun_tentativo(rete, tentativi)
    foto = os.environ.get("RETE_FOTO", "")
    senza_proxy = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    p = subprocess.run(["node", os.path.join(os.path.dirname(__file__), "pagina_rete_approva.js"), f"{RETE}/", "altro",
                        foto.replace(".png", "") if foto else ""], capture_output=True, text=True, timeout=240, env=senza_proxy)
    r = json.loads(p.stdout.strip().splitlines()[-1])
    assert "errore" not in r, (r, rete.esiti_lavora)
    assert r["codice"] == a.controllo and r["titolo"] == "Un gateway chiede di entrare"
    assert f"Raspberry {a.macchina}, dall'indirizzo 127.0.0.1" in r["testoRichiesta"]
    assert r["errori"] == [] and r["finestre"] == [] and r["ponti"] == ["altro", "casa"]
    assert r["modulo"] == {"nome": "Prova!", "chiave": "ssh-ed25519 AAAA", "cursore": "nuova-chiave", "nomeValido": False}
    assert any(u.endswith("/api/approva") and h == "1" for u, h, _ in r["chiamate"])
    assert "Aggiunta del gateway altro: fatta alle" in r["testo"]
    assert aspetta(lambda: a.stato()["collegato"], 60), a.stato()
    with socket.create_connection(("127.0.0.1", 19444), timeout=5):
        pass
    a.ferma()
    rete.altri.remove(a)


def test_8_tunnel_chiuso_dal_server_si_riapre(rete):
    prima = rete.sshd_log().count("Accepted publickey for ponte-casa")
    subprocess.run(["pkill", "-KILL", "-u", "ponte-casa"], check=True)
    assert aspetta(lambda: not rete.accesso.stato()["collegato"], 20)
    assert aspetta(lambda: rete.accesso.stato()["collegato"], 60), rete.accesso.stato()
    assert rete.sshd_log().count("Accepted publickey for ponte-casa") == prima + 1
    assert rete.chiedi(f"{RETE}/stato/casa.json")[0] == 200


def test_9_rilancio_sul_server_con_utenti_veri(rete):
    rete.log.write_text("")
    for argomenti in (("installa",), ("aggiungi", "casa", (rete.chiave.parent / "chiave.pub").read_text().strip())):
        esito, uscita = rete.esegui(*argomenti)
        assert esito == 0, uscita
    chiamate = rete.log.read_text()
    assert "useradd" not in chiamate and "pkill" not in chiamate and "docker restart" not in chiamate
    assert rete.accesso.stato()["collegato"]          # il tunnel non e' stato toccato


def test_10_tolto_dalla_pagina_il_raspberry_smette(rete):
    codice, d = rete.api("POST", "/api/rimuovi", {"nome": "casa"})
    assert codice == 202, d
    assert aspetta(lambda: rete.accesso.stato()["richiesta"] == "rifiutata", 60), (rete.accesso.stato(), rete.esiti_lavora)
    assert "tolto dalla rete" in rete.accesso.stato()["messaggio"] and not rete.accesso.stato()["collegato"]
    assert subprocess.run(["id", "ponte-casa"], capture_output=True).returncode != 0
    tentativi = rete.sshd_log().count("Connection from")
    time.sleep(3)
    assert nessun_tentativo(rete, tentativi)          # niente tentativi a vuoto: fail2ban non vede nulla
    try:
        stato = rete.chiedi(f"{PONTE}/")[0]
    except urllib.error.URLError:
        stato = None                                   # Caddy non ha piu' il sito del ponte
    assert stato != 200
    st = rete.stato_api()
    assert [(x["nome"], x["motivo"], x["chiave"], x["da"]) for x in st["rifiutate"]] == [("casa", "tolto", True, "andrea")]
    assert [p["nome"] for p in json.loads(rete.chiedi(f"{RETE}/ponti.json")[1])["ponti"]] == ["altro"]


def test_11_lascia_rientrare(rete):
    [x] = rete.stato_api()["rifiutate"]
    codice, d = rete.api("POST", "/api/riammetti", {"id": x["id"]})
    assert codice == 202, d
    assert aspetta(lambda: rete.accesso.stato()["collegato"], 90), (rete.accesso.stato(), rete.esiti_lavora)
    assert rete.stato_api()["rifiutate"] == [] and rete.chiedi(f"{RETE}/stato/casa.json")[0] == 200


def test_12_sostituito_da_un_raspberry_reinstallato(rete, tmp_path):
    nuova = nuova_chiave(tmp_path, "nuova")
    porta = porta_libera()
    g = m.Gestione(m.Config(**{**rete.cfg.__dict__, "gestione_porta_pagina": porta}), m.Pausa())
    g.fornisci_stato = lambda: {"formato": 1, "nome": "Gateway reinstallato", "nodo": {"collegato": True}}
    g.avvia()
    aspetta_porta(porta)
    try:
        a = rete.avvia_accesso(nuova, nome="casa", porta_pagina=porta)
        rete.altri.append(a)
        assert aspetta(lambda: a.stato()["richiesta"] == "in_attesa", 30), a.stato()
        assert "Sostituisci" in a.stato()["messaggio"]
        [r] = rete.stato_api()["richieste"]
        assert r["sostituisce"] is True and r["controllo"] == a.controllo
        assert rete.api("POST", "/api/approva", {"id": r["id"]})[0] == 409           # non per sbaglio
        assert rete.api("POST", "/api/approva", {"id": r["id"], "sostituisci": True})[0] == 202
        assert aspetta(lambda: a.stato()["collegato"], 90), (a.stato(), rete.esiti_lavora)
        assert json.loads(rete.chiedi(f"{RETE}/stato/casa.json")[1])["nome"] == "Gateway reinstallato"
        # il Raspberry vecchio e' fuori: la sua chiave non vale piu', richiede ma non bussa
        assert aspetta(lambda: not rete.accesso.stato()["collegato"], 30)
        assert aspetta(lambda: rete.accesso.stato()["richiesta"] == "in_attesa", 30), rete.accesso.stato()
        tentativi = rete.sshd_log().count("Connection from")
        time.sleep(2)
        assert nessun_tentativo(rete, tentativi)
        [vecchia] = rete.stato_api()["richieste"]
        assert vecchia["id"] == rete.accesso.impronta and vecchia["sostituisce"] is True
        assert rete.api("POST", "/api/rifiuta", {"id": vecchia["id"]})[0] == 200
        assert aspetta(lambda: rete.accesso.stato()["richiesta"] == "rifiutata", 30)
    finally:
        g.ferma()


def test_13_ponte_nuovo_dalla_pagina_all_installazione(rete, pki, tmp_path):
    """1.10.0, dall'inizio alla fine: certificato e canale caricati dal browser, invito, Raspberry che si accende con
    la chiave d'invito (primo_avvio.py vero, ssh vero, HTTPS vero), configurazione via SSH, installatore da GitHub,
    avanzamento sulla pagina, certificato tolto dal server a installazione finita."""
    from test_primo_avvio import INSTALLATORE
    psk = base64.b64encode(b"k" * 32).decode()
    pacchetto = pki / "meshtastic-cnsas-integration.zip"
    senza_proxy = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    if shutil.which("node"):
        foto = os.environ.get("RETE_FOTO", "")
        p = subprocess.run(["node", os.path.join(os.path.dirname(__file__), "pagina_rete_nuovi.js"), f"{RETE}/",
                            str(pacchetto), psk, foto.replace(".png", "-nuovi.png") if foto else ""],
                           capture_output=True, text=True, timeout=180, env=senza_proxy)
        r = json.loads(p.stdout.strip().splitlines()[-1])
        assert "errore" not in r, (r, rete.esiti_lavora[-3:])
        # il ponte "altro" a questo punto e' spento: il browser segnala solo il suo stato che non arriva (502)
        assert [e for e in r["errori"] if "status of 502" not in e] == [] and r["finestre"] == [], r["errori"]
        # "casa" manda uno stato ridotto (Raspberry reinstallato del test 12): la pagina si disegna lo stesso
        assert r["ponti"] == ["altro", "casa"], r["ponti"]
        assert r["prima"].startswith("Prima, una volta sola") and r["impostazioniAperte"] and not r["moduloInvitoVisibile"]
        assert "certificato \"meshtastic-cnsas\"" in r["dopo"] and "canale CNSAS-VA" in r["dopo"], r["dopo"]
        assert psk not in r["dopo"] + r["invito"] + r["copiate"]             # la chiave del canale non torna indietro
        assert r["campoChiaveVuoto"] == ""
        assert "velate" in r["invito"] and "Radio Meshtastic collegata via USB" in r["invito"]
        assert r["istruzioniAperte"] and len(r["chiaviInvito"]) == 2
        imager, riga = r["chiaviInvito"]
        assert imager == f"{rete.github.url}/{REPO}/releases/latest/download/imager.json"     # GitHub finto delle prove
        assert riga.startswith("ssh-ed25519 ") and riga.endswith(f" mesh2tak-invito {RETE}")
        assert riga in r["copiate"] and '"Opzioni app" (in basso a sinistra)' in r["copiate"] and "ponte-velate" in r["copiate"]
        assert r["dopoRicarica"] == r["chiaviInvito"]
    else:
        codice, d = rete.api("POST", "/api/impostazioni-nuovi", {
            "nome_pacchetto": pacchetto.name, "pacchetto": base64.b64encode(pacchetto.read_bytes()).decode(),
            "password": "atakatak", "canale": "CNSAS-VA", "chiave": psk})
        assert codice == 202, d
        assert aspetta(lambda: rete.stato_api()["ponti_nuovi"]["pronto"], 60)
        codice, d = rete.api("POST", "/api/invita", {"nome": "velate", "scheda": "usb", "posizione": "45.8205, 8.8257"})
        assert codice == 200, d
    # sul server: certificato e canale leggibili solo da root, niente nella pagina
    impostazioni = rete.r / "etc/mesh2tak-rete/ponti-nuovi/impostazioni.json"
    assert oct(impostazioni.stat().st_mode)[-3:] == "600" and impostazioni.stat().st_uid == 0
    st = rete.stato_api()
    assert psk not in json.dumps(st) and "atakatak" not in json.dumps(st)
    [inv] = [i for i in st["inviti"] if i["nome"] == "velate"]
    riga = inv["riga"]
    # il Raspberry: microSD scritta da Imager con la chiave d'invito tra le chiavi SSH
    versione = st["versione"]
    installatore = INSTALLATORE.replace(b"1.10.0", versione.encode())
    base = f"/{REPO}/releases/download/v{versione}"
    rete.github.risposte[f"{base}/installa-ponte-meshtastic.sh"] = installatore
    rete.github.risposte[f"{base}/SHA256SUMS"] = somme({"installa-ponte-meshtastic.sh": installatore})
    pi = tmp_path / "raspberry"
    (pi / "home/andrea/.ssh").mkdir(parents=True)
    (pi / "home/andrea/.ssh/authorized_keys").write_text(f"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIandrea andrea@mac\n{riga}\n")
    amb = dict(senza_proxy, MESH2TAK_PROVA_RADICE=str(pi), MESH2TAK_PROVA_GITHUB=rete.github.url,
               SSL_CERT_FILE=rete.ca_caddy()[0], MESH2TAK_PROVA_UN_GIRO="1")
    agente = os.path.join(RADICE, "dist", "mesh2tak-primo-avvio.py")
    fine = time.time() + 240
    while time.time() < fine:
        p = subprocess.run([sys.executable, "-I", agente], env=amb, capture_output=True, text=True, timeout=120)
        if p.returncode == 0:
            break
        assert p.returncode == 75, p.stdout + p.stderr
        time.sleep(1)
    else:
        raise AssertionError((p.stdout[-3000:], rete.esiti_lavora[-2:]))
    registro = (pi / "var/log/mesh2tak-primo-avvio.log").read_text()
    assert "invito riconosciuto" in registro and "primo avvio finito" in registro, registro
    assert "Accepted publickey for ponte-velate" in rete.sshd_log()
    # l'installatore ha avuto tutto senza domande
    lancio = (pi / "lanci").read_text(errors="replace")
    assert "ARGOMENTI --scheda usb --canale-squadra --accesso-esterno=velate " in lancio
    for atteso in (f"MESH2TAK_CHIAVE_CANALE={psk}", "MESH2TAK_PASSWORD=atakatak", "MESH2TAK_CANALE_SQUADRA=CNSAS-VA",
                   "MESH2TAK_POSIZIONE=45.8205,8.8257", f"MESH2TAK_ACCESSO_RETE={RETE}"):
        assert atteso in lancio.splitlines(), atteso
    assert f"PACCHETTO {pacchetto.read_bytes()[:2].decode()}" in lancio
    # la pagina ha seguito l'installazione; il server non ha piu' certificato e canale per questo ponte
    st = rete.stato_api()
    assert st["installazioni"]["velate"]["fase"] == "fatto" and "installato" in st["installazioni"]["velate"]["testo"]
    assert "[installo] 1/9 Certificato del gateway" in registro and "[fatto] installato" in registro
    assert not (rete.r / "etc/mesh2tak-rete/configurazioni/velate.json").exists()
    assert [i["stato"] for i in st["inviti"] if i["nome"] == "velate"] == ["usato"]
    assert ("aggiungi", "velate", "ok", "andrea") in [(x["azione"], x["nome"], x["esito"], x["da"]) for x in st["lavori"]]
    assert "velate" in [x["nome"] for x in json.loads(rete.chiedi(f"{RETE}/ponti.json")[1])["ponti"]]
    # sul Raspberry: la chiave d'invito tolta, nessun segreto rimasto, il primo avvio non c'e' piu'
    assert "mesh2tak-invito" not in (pi / "home/andrea/.ssh/authorized_keys").read_text()
    for f in (pi / "var/lib/mesh2tak-primo-avvio").iterdir():
        assert f.name in ("stato.json", "installazione.log"), f
    assert psk not in (pi / "var/lib/mesh2tak-primo-avvio/stato.json").read_text()
