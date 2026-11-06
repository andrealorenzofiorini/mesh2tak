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

import pytest

from authentik_finto import TOKEN, AuthentikFinto
from conftest import RADICE, aspetta_porta, porta_libera
from http_finto import ServerFinto, somme
from test_gestione import PaginaNodoFinta
import mesh2tak as m

SCRIPT = os.path.join(RADICE, "dist", "rete-meshtastic.sh")
SSHD = "/opt/prova-openssh/sbin/sshd"
CADDY = "/opt/prova-caddy/caddy-2.10.2"
DOMINIO = "prova.lan"
RETE = f"https://meshtastic.{DOMINIO}:8443"
PONTE = f"https://meshtastic-casa.{DOMINIO}:8443"
NOMI = [f"meshtastic.{DOMINIO}", f"meshtastic-casa.{DOMINIO}", f"meshtastic-altro.{DOMINIO}"]
REPO = "andrealorenzofiorini/mesh2tak"
FILE_RELEASE = ("installa-ponte-meshtastic.sh", "rete-meshtastic.sh", "SHA256SUMS")

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
        self.ponte.mio_num, self.ponte.nodo_nome, self.ponte.nodo_nome_breve = 0x11111111, "Ponte di prova", "PROV"
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
    assert stato == 200 and json.loads(corpo)["nome"] == "Ponte di prova"
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
    assert r["visibili"] == [True, False]
    assert r["ordine"] == ["<img src=x onerror=alert(1)>", "07-BIANCHI"]          # prima chi ha un problema
    assert "batteria quasi scarica" in r["voci"][0] and "casa: passando da 2 radio" in r["voci"][0]
    assert "SenseCAP T1000-E, con ATAK" in r["voci"][1] and "casa: diretta, segnale buono" in r["voci"][1]
    assert r["riassunto"].startswith("2 radio della squadra: 2 sentite nell'ultima ora.")
    assert r["cerca"] == ["07-BIANCHI"] and r["ponti"] == [True, False]


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
    assert aspetta(lambda: "[Rete Meshtastic] Ponte casa: non risponde" in oggetti_email(rete), 40), oggetti_email(rete)
    vista = rete.stato_api()["avvisi"]
    assert vista["problemi"]["casa"][0]["codice"] == "irraggiungibile" and vista["ultimo"]["esito"] == "mandato"
    rete.accesso = rete.avvia_accesso()
    assert aspetta(lambda: rete.accesso.stato()["collegato"], 60), rete.accesso.stato()
    assert aspetta(lambda: "[Rete Meshtastic] Ponte casa: di nuovo in linea" in oggetti_email(rete), 40), oggetti_email(rete)
    assert oggetti_email(rete).count("[Rete Meshtastic] Ponte casa: non risponde") == 1     # una volta sola
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
    pubblica_release(rete, nuova, "- una novita' di prova\n- un'altra")
    base = f"/{REPO}/releases/download/v{nuova}"
    rete.github.risposte[f"{base}/rete-meshtastic.sh"] = b"#!/bin/bash\necho manomesso\n"
    rete.github.risposte[f"{base}/SHA256SUMS"] = somme({"rete-meshtastic.sh": b"quello pubblicato"})
    senza_proxy = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    p = subprocess.run(["node", os.path.join(os.path.dirname(__file__), "pagina_rete_aggiorna.js"), f"{RETE}/"],
                       capture_output=True, text=True, timeout=180, env=senza_proxy)
    r = json.loads(p.stdout.strip().splitlines()[-1])
    assert "errore" not in r, r
    assert r["errori"] == [] and r["finestre"] == []
    assert f"È uscita la versione {nuova}" in r["sezione"] and "una novita' di prova" in r["sezione"], r["sezione"]
    assert f"Aggiorna il server alla {nuova}" in r["sezione"] and "Prima il server, poi i ponti" in r["sezione"]
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
    assert r["codice"] == a.controllo and r["titolo"] == "Un ponte chiede di entrare"
    assert f"Raspberry {a.macchina}, dall'indirizzo 127.0.0.1" in r["testoRichiesta"]
    assert r["errori"] == [] and r["finestre"] == [] and r["ponti"] == ["altro", "casa"]
    assert r["modulo"] == {"nome": "Prova!", "chiave": "ssh-ed25519 AAAA", "cursore": "nuova-chiave", "nomeValido": False}
    assert any(u.endswith("/api/approva") and h == "1" for u, h, _ in r["chiamate"])
    assert "Aggiunta del ponte altro: fatta alle" in r["testo"]
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
    g.fornisci_stato = lambda: {"formato": 1, "nome": "Ponte reinstallato", "nodo": {"collegato": True}}
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
        assert json.loads(rete.chiedi(f"{RETE}/stato/casa.json")[1])["nome"] == "Ponte reinstallato"
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
