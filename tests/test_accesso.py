"""Accesso da internet (1.4.0, richiesta automatica dalla 1.5.0): stato del ponte per la pagina della rete, nome di
chi gestisce da internet, richiesta di entrare nella rete, tunnel SSH verso il server con le sue attese (niente
tentativi a vuoto che fail2ban punirebbe)."""
import base64
import hashlib
import http.server
import json
import os
import stat
import subprocess
import sys
import threading
import time

import pytest

from conftest import RADICE, porta_libera, scrivi_config
from test_gestione import chiedi, gestione  # noqa: F401  (fixture)
import mesh2tak as m

sys.path.insert(0, os.path.join(RADICE, "server"))
import rete_meshtastic as rete  # noqa: E402

CHIAVE_PROVA = "ssh-ed25519 " + base64.b64encode(
    b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(range(32))).decode() + " mesh2tak-casa@prova"


# ---- impronta: Raspberry e server devono calcolare la stessa ----

def test_impronta_uguale_sul_raspberry_e_sul_server():
    riga, impronta = rete.leggi_chiave(CHIAVE_PROVA)
    assert impronta == m.impronta_chiave(CHIAVE_PROVA) == m.impronta_chiave(riga)
    blob = base64.b64decode(CHIAVE_PROVA.split()[1])
    assert impronta == hashlib.sha256(blob).hexdigest()


@pytest.mark.parametrize("cattiva", ["", "ssh-rsa AAAAB3Nza", "ssh-ed25519 non-base64!", "ssh-ed25519 AAAA",
                                     CHIAVE_PROVA.replace("ssh-ed25519 ", "ssh-ed25519 x", 1)])
def test_server_rifiuta_chiavi_strane(cattiva):
    with pytest.raises(rete.Errore):
        rete.leggi_chiave(cattiva)


def test_codice_di_controllo_uguale_sul_raspberry_e_sul_server():
    _, impronta = rete.leggi_chiave(CHIAVE_PROVA)
    assert m.codice_controllo(impronta) == rete.codice_controllo(impronta)
    assert m.codice_controllo("f" * 64) == "ZZZ-ZZZ" and m.codice_controllo("0" * 64) == "000-000"
    codici = {m.codice_controllo(hashlib.sha256(bytes([i, j])).hexdigest()) for i in range(40) for j in range(40)}
    assert len(codici) == 1600                        # 30 bit: due Raspberry diversi non si confondono
    assert all(len(c) == 7 and c[3] == "-" and not set(c) & set("ILOU") for c in codici)


def test_codice_di_controllo_da_riga_di_comando(tmp_path):
    (tmp_path / "chiave.pub").write_text(CHIAVE_PROVA + "\n")
    p = subprocess.run([sys.executable, os.path.join(RADICE, "mesh2tak.py"), "--codice-controllo",
                        str(tmp_path / "chiave.pub")], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0 and p.stdout.strip() == m.codice_controllo(m.impronta_chiave(CHIAVE_PROVA))
    p = subprocess.run([sys.executable, os.path.join(RADICE, "mesh2tak.py"), "--codice-controllo",
                        str(tmp_path / "manca.pub")], capture_output=True, text=True, timeout=60)
    assert p.returncode == 1 and "ERRORE" in p.stderr


def test_commento_della_chiave_ripulito():
    riga, _ = rete.leggi_chiave(CHIAVE_PROVA.replace("mesh2tak-casa@prova", 'x" ,command="/bin/sh'))
    assert '"' not in riga and "," not in riga and riga.count(" ") <= 2


# ---- registrazione pubblicata dal server ----

BUONA = {"formato": 1, "nome": "casa", "utente": "ponte-casa", "porta": 19443, "ssh_host": "meshtastic.esempio.it",
         "ssh_porta": 22, "chiavi_host": ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIB"], "indirizzo": "https://meshtastic-casa.esempio.it/"}


def test_registrazione_valida():
    r = m.AccessoEsterno.valida(BUONA)
    assert (r["utente"], r["porta"], r["host"], r["porta_ssh"], r["indirizzo"]) == \
        ("ponte-casa", 19443, "meshtastic.esempio.it", 22, "https://meshtastic-casa.esempio.it/")


@pytest.mark.parametrize("campo,valore", [
    ("utente", "root"), ("utente", "ponte-casa -oProxyCommand=x"), ("porta", "19443"), ("porta", 80),
    ("ssh_host", "-oProxyCommand=touch"), ("ssh_host", "a b"), ("ssh_porta", 0),
    ("chiavi_host", []), ("chiavi_host", ["ssh-ed25519 AAAA\nmesh2tak-rete ssh-rsa BBBB"]), ("chiavi_host", "x")])
def test_registrazione_strana_rifiutata(campo, valore):
    with pytest.raises(ValueError):
        m.AccessoEsterno.valida(dict(BUONA, **{campo: valore}))


def test_indirizzo_strano_ignorato():
    assert m.AccessoEsterno.valida(dict(BUONA, indirizzo="javascript:alert(1)"))["indirizzo"] is None


@pytest.mark.parametrize("righe,esito", [
    (["Authenticated to x ([1.2.3.4]:22) using \"publickey\".", "Error: remote port forwarding failed for listen port 19443"], "porta"),
    (["ponte-casa@x: Permission denied (publickey)."], "rifiuto"),
    (["Host key verification failed."], "chiave_server"),
    (["No ED25519 host key is known for mesh2tak-rete and you have requested strict checking."], "chiave_server"),
    (["ssh: connect to host x port 22: Connection timed out"], "rete"),
    (["Timeout, server x not responding."], "rete"),
])
def test_classifica_messaggi_di_ssh(righe, esito):
    assert m.AccessoEsterno.classifica(righe) == esito


# ---- configurazione: un errore spegne solo l'accesso ----

def _conf(tmp_path, pki, **sezioni):
    return scrivi_config(tmp_path / "c.conf", pki, 8089, 4404, tmp_path, gestione__attiva="si",
                         gestione__certificato=pki / "meshtastic-cnsas.pem",
                         gestione__chiave=pki / "meshtastic-cnsas-plain.key", **sezioni)


def test_config_accesso(pki, tmp_path):
    chiave = tmp_path / "chiave"
    chiave.write_text("privata")
    (tmp_path / "chiave.pub").write_text(CHIAVE_PROVA)
    cfg = m.carica_config(_conf(tmp_path, pki, accesso_esterno__attivo="si", accesso_esterno__nome="casa",
                                accesso_esterno__rete="https://meshtastic.esempio.it", accesso_esterno__chiave=chiave))
    assert cfg.accesso_attivo and cfg.accesso_nome == "casa" and not cfg.accesso_errore


@pytest.mark.parametrize("nome,rete_,messaggio", [("Casa!", "https://meshtastic.esempio.it", "nome"),
                                                  ("casa", "meshtastic.esempio.it", "rete"),
                                                  ("casa", "https://meshtastic.esempio.it/x", "rete")])
def test_config_accesso_sbagliata_non_ferma_il_ponte(pki, tmp_path, nome, rete_, messaggio):
    cfg = m.carica_config(_conf(tmp_path, pki, accesso_esterno__attivo="si", accesso_esterno__nome=nome,
                                accesso_esterno__rete=rete_))
    assert not cfg.accesso_attivo and messaggio in cfg.accesso_errore


def test_config_nodo_pagina_vuoto_resta_vuoto(pki, tmp_path):
    # radio USB con accesso da internet: la pagina del ponte da' solo lo stato, niente meshtasticd dietro
    cfg = m.carica_config(_conf(tmp_path, pki, gestione__nodo_pagina="", gestione__porta_app=0))
    assert cfg.gestione_nodo_pagina == ""
    assert m.carica_config(_conf(tmp_path, pki)).gestione_nodo_pagina == "https://127.0.0.1:9444"


def test_config_accesso_senza_chiave(pki, tmp_path):
    cfg = m.carica_config(_conf(tmp_path, pki, accesso_esterno__attivo="si", accesso_esterno__nome="casa",
                                accesso_esterno__rete="https://meshtastic.esempio.it",
                                accesso_esterno__chiave=tmp_path / "manca"))
    assert not cfg.accesso_attivo and "--accesso-esterno" in cfg.accesso_errore


# ---- pagina del ponte: stato per la rete, chi gestisce da internet ----

def test_stato_json_non_mette_in_pausa(gestione):  # noqa: F811
    g, pagina, _ = gestione()
    g.fornisci_stato = lambda: {"versione": m.VERSIONE, "nome": "Ponte <b>"}
    stato, corpo = chiedi(g.cfg.gestione_porta_pagina, "/mesh2tak/stato.json")
    assert stato == 200 and json.loads(corpo) == {"versione": m.VERSIONE, "nome": "Ponte <b>"}
    assert not g.pausa.attiva and not pagina.chiamate
    assert chiedi(g.cfg.gestione_porta_pagina, "/mesh2tak/stato.json", "PUT", b"x")[0] == 405


def test_da_internet_con_il_nome_dell_amministratore(gestione):  # noqa: F811
    import ssl
    import urllib.request
    g, pagina, _ = gestione()
    rq = urllib.request.Request(f"https://127.0.0.1:{g.cfg.gestione_porta_pagina}/api/v1/fromradio",
                                headers={"X-Authentik-Username": "andrea<script>", "X-Authentik-Groups": "admin"})
    with urllib.request.urlopen(rq, context=ssl._create_unverified_context(), timeout=30) as r:
        assert r.status == 200
    assert g.pausa.attiva and "gestito da internet (andreascript)" in g.pausa.nota
    st = g.stato_pausa()
    assert st["attiva"] and st["chi"] == "da internet (andreascript)" and st["fino_al_massimo"] - st["dal"] == 1800
    assert pagina.chiamate and pagina.chiamate[-1][3] is True   # il ponte aveva lasciato il nodo


def test_intestazioni_di_authentik_non_arrivano_al_nodo(gestione, pki):  # noqa: F811
    import ssl
    import urllib.request
    g, pagina, _ = gestione()
    ricevute = {}
    vecchia = pagina.server.RequestHandlerClass.do_GET

    def do_get(self):
        ricevute.update({k.lower(): v for k, v in self.headers.items()})
        vecchia(self)
    pagina.server.RequestHandlerClass.do_GET = do_get
    rq = urllib.request.Request(f"https://127.0.0.1:{g.cfg.gestione_porta_pagina}/",
                                headers={"X-Authentik-Username": "andrea", "X-Authentik-Jwt": "segreto"})
    urllib.request.urlopen(rq, context=ssl._create_unverified_context(), timeout=30).read()
    assert ricevute and not [k for k in ricevute if k.startswith("x-authentik")]


def test_dalla_rete_di_casa_resta_dal_browser(gestione):  # noqa: F811
    g, _, _ = gestione()
    chiedi(g.cfg.gestione_porta_pagina, "/api/v1/fromradio")   # senza nome di Authentik
    assert "dal browser" in g.pausa.nota


def test_radio_usb_solo_stato(gestione):  # noqa: F811
    g, pagina, _ = gestione(gestione_nodo_pagina="", gestione_porta_app=0)
    g.fornisci_stato = lambda: {"gestione": {"pagina": bool(g.nodo_pagina)}}
    assert json.loads(chiedi(g.cfg.gestione_porta_pagina, "/mesh2tak/stato.json")[1]) == {"gestione": {"pagina": False}}
    stato, testo = chiedi(g.cfg.gestione_porta_pagina, "/")
    assert stato == 200 and "radio collegata via USB" in testo.decode()
    assert chiedi(g.cfg.gestione_porta_pagina, "/api/v1/fromradio")[0] == 404
    assert not g.pausa.attiva and not pagina.chiamate


# ---- istantanea del ponte ----

def test_istantanea_senza_coordinate_e_con_nomi_puliti():
    cfg = m.Config(tak_host="x", collegamento="tcp:127.0.0.1:4404")
    p = m.Ponte(cfg, tak=None, sdk=None)
    p.mio_num = 0x11111111
    p.canali = {0: m.Canale(0, "CNSAS-VA", True), 1: m.Canale(1, "Public", False)}
    p.canali_ok = {0}
    ora = time.time()
    f = m.Fonte(tipo_fonte="mesh", uid="mesh-22222222", lat=45.8, lon=8.8, hae=0, ce=10, t_fix=ora - 60, canale=0)
    p.stato[0x22222222] = m.StatoNodo(0x22222222, f, ora - 30, nome_inviato="Mario\x00 <b>", batteria=80, salti=1)
    p.stato[0x33333333] = m.StatoNodo(0x33333333, f, ora - 7200, nome_inviato="Vecchia")
    p.stato[0x11111111] = m.StatoNodo(0x11111111, f, ora, nome_inviato="Ponte")
    s = p.istantanea()
    testo = json.dumps(s)
    assert "45.8" not in testo and "8.8" not in testo          # niente coordinate
    assert [r["nome"] for r in s["radio"]] == ["Mario <b>", "Vecchia"]   # il ponte non c'e', i piu' recenti prima
    assert s["radio"][0]["batteria"] == 80 and s["radio"][0]["salti"] == 1 and s["radio"][0]["canale"] == "CNSAS-VA"
    assert [r["sulla_mappa"] for r in s["radio"]] == [True, False] and s["radio_sulla_mappa"] == 1
    assert s["nodo"]["canali"][0] == {"indice": 0, "nome": "CNSAS-VA", "privato": True, "inoltrato": True}
    assert s["nodo"]["collegato"] is False and s["id_nodo"] == "!11111111"


# ---- il tunnel: ssh finto e registrazione finta ----

SSH_FINTO = r"""#!/usr/bin/env python3
import os, sys, time, json
scenario = open(os.environ["SSH_SCENARIO"]).read().strip()
with open(os.environ["SSH_CHIAMATE"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
if scenario == "su":
    print('Authenticated to meshtastic.esempio.it ([127.0.0.1]:22) using "publickey".', file=sys.stderr, flush=True)
    time.sleep(3600)
elif scenario == "rifiuto":
    print("ponte-casa@meshtastic.esempio.it: Permission denied (publickey).", file=sys.stderr)
    sys.exit(255)
elif scenario == "porta":
    print('Authenticated to meshtastic.esempio.it ([127.0.0.1]:22) using "publickey".', file=sys.stderr)
    print("Error: remote port forwarding failed for listen port 19443", file=sys.stderr)
    sys.exit(255)
else:
    print("ssh: connect to host meshtastic.esempio.it port 22: Connection refused", file=sys.stderr)
    sys.exit(255)
"""


class RegistrazioneFinta:
    """La pagina della rete vista dal Raspberry: registrazione (GET) e richiesta di entrare (POST)."""

    def __init__(self):
        self.porta = porta_libera()
        self.dati = None          # None = 404
        self.richieste = []
        self.domande = []         # corpi delle richieste di entrare
        self.risposta = (202, {"esito": "in_attesa", "controllo": "?"})
        finta = self

        class Gestore(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):  # noqa: N802
                finta.richieste.append(self.path)
                if finta.dati is None:
                    self.send_error(404)
                    return
                corpo = json.dumps(finta.dati).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(corpo)))
                self.end_headers()
                self.wfile.write(corpo)

            def do_POST(self):  # noqa: N802
                corpo = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                finta.domande.append((self.path, self.headers.get("Content-Type"), json.loads(corpo)))
                codice, dati = finta.risposta
                risposta = json.dumps(dati).encode()
                self.send_response(codice)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(risposta)))
                self.end_headers()
                self.wfile.write(risposta)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", self.porta), Gestore)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def ferma(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def tunnel(tmp_path, monkeypatch):
    ssh = tmp_path / "ssh"
    ssh.write_text(SSH_FINTO)
    ssh.chmod(0o755)
    scenario, chiamate = tmp_path / "scenario", tmp_path / "chiamate.jsonl"
    scenario.write_text("su")
    monkeypatch.setenv("SSH_SCENARIO", str(scenario))
    monkeypatch.setenv("SSH_CHIAMATE", str(chiamate))
    (tmp_path / "chiave").write_text("privata")
    (tmp_path / "chiave.pub").write_text(CHIAVE_PROVA + "\n")
    reg = RegistrazioneFinta()
    cfg = m.Config(tak_host="x", accesso_attivo=True, accesso_nome="casa", accesso_rete=f"http://127.0.0.1:{reg.porta}",
                   accesso_chiave=str(tmp_path / "chiave"), accesso_cartella_stato=str(tmp_path / "stato"))
    a = m.AccessoEsterno(cfg, 9443)
    a.ssh = str(ssh)
    a.ATTESA_REGISTRAZIONE = (0.3, 0.3)
    a.ATTESA_RETE = (0.3, 0.6)
    a.ATTESA_RIFIUTO_S = 0.8
    a.ATTESA_PORTA_OCCUPATA_S = 0.3
    a.CONFERMA_S = 0.3
    a.DURATA_STABILE_S = 1.0

    def chiamate_ssh():
        return [json.loads(x) for x in chiamate.read_text().splitlines()] if chiamate.exists() else []
    yield a, reg, scenario, chiamate_ssh
    a.ferma()
    reg.ferma()


def aspetta(condizione, limite=10.0):
    fine = time.time() + limite
    while time.time() < fine:
        if condizione():
            return True
        time.sleep(0.05)
    return False


def test_senza_registrazione_chiede_di_entrare_e_non_bussa(tunnel):
    a, reg, _, chiamate_ssh = tunnel
    a.avvia()
    assert aspetta(lambda: len(reg.richieste) >= 3)
    assert chiamate_ssh() == []                       # nessun tentativo SSH: fail2ban non vede nulla
    assert reg.richieste[0] == f"/accesso/{m.impronta_chiave(CHIAVE_PROVA)}.json"
    # una sola richiesta di entrare (si ripete ogni ora), con nome e chiave pubblica: nient'altro
    assert len(reg.domande) == 1
    percorso, tipo, corpo = reg.domande[0]
    assert percorso == "/accesso/richiesta" and tipo == "application/json"
    assert corpo == {"formato": 1, "nome": "casa", "chiave": CHIAVE_PROVA, "macchina": a.macchina, "versione": m.VERSIONE}
    assert rete.RE_MACCHINA.match(a.macchina)          # il server la accetta
    s = a.stato()
    codice = m.codice_controllo(m.impronta_chiave(CHIAVE_PROVA))
    assert s["richiesta"] == "in_attesa" and s["controllo"] == codice and not s["collegato"]
    assert f"in attesa di approvazione sulla pagina della rete http://127.0.0.1:{reg.porta} (codice di controllo {codice})" \
        in s["messaggio"]


@pytest.mark.parametrize("codice,dati,esito,parole", [
    (202, {"esito": "in_attesa", "sostituisce": True}, "in_attesa", "va scelto 'Sostituisci'"),
    (403, {"errore": "questo Raspberry e' stato tolto dalla rete sulla pagina della rete"}, "rifiutata",
     "tolto dalla rete sulla pagina della rete: per farlo rientrare, sulla pagina 'Lascia rientrare'"),
    (429, {"errore": "troppe richieste da questo indirizzo: riprovo tra un'ora"}, "troppe", "riprovo tra un'ora"),
    (400, {"errore": "nome del ponte: solo minuscole"}, "non_valida", "rilancia l'installatore"),
    (405, {}, "server_da_preparare", "sudo bash rete-meshtastic.sh installa"),     # server ancora alla 1.4.0
    (502, {}, "server_da_preparare", "risponde 502"),
    (500, {"errore": "boh\n\n<script>"}, "errore", "risponde 500 alla richiesta (boh <script>)"),
])
def test_esiti_della_richiesta(tunnel, codice, dati, esito, parole):
    a, reg, _, chiamate_ssh = tunnel
    reg.risposta = (codice, dati)
    a.avvia()
    assert aspetta(lambda: a.stato()["richiesta"] == esito), a.stato()
    assert parole in a.stato()["messaggio"] and "\n" not in a.stato()["messaggio"]
    assert chiamate_ssh() == []


def test_richiesta_non_mandata_se_la_pagina_non_risponde(tunnel):
    a, reg, _, chiamate_ssh = tunnel
    reg.ferma()                                        # niente pagina della rete
    a.avvia()
    assert aspetta(lambda: "non risponde" in a.stato()["messaggio"])
    assert reg.domande == [] and chiamate_ssh() == []
    assert "sudo bash rete-meshtastic.sh installa" in a.stato()["messaggio"]


def test_dopo_l_approvazione_si_collega_da_solo(tunnel):
    a, reg, _, chiamate_ssh = tunnel
    a.avvia()
    assert aspetta(lambda: a.stato()["richiesta"] == "in_attesa")
    assert chiamate_ssh() == []
    reg.dati = BUONA                                   # approvata sulla pagina: la registrazione compare
    assert aspetta(lambda: a.stato()["collegato"])
    assert a.stato()["richiesta"] is None and len(reg.domande) == 1 and len(chiamate_ssh()) == 1


def test_approvata_proprio_adesso_rilegge_subito(tunnel):
    a, reg, _, chiamate_ssh = tunnel
    a.ATTESA_REGISTRAZIONE = (30.0, 30.0)              # senza la rilettura subito, aspetterebbe 30 s
    reg.risposta = (200, {"esito": "registrato"})

    class Domande(list):                               # la registrazione compare mentre risponde 200
        def append(self, x):
            super().append(x)
            reg.dati = BUONA
    reg.domande = Domande()
    a.avvia()
    assert aspetta(lambda: a.stato()["collegato"], 8)


def test_richiesta_ripetuta_dopo_un_po(tunnel):
    a, reg, _, _ = tunnel
    a.RICHIESTA_OGNI_S = 0.8
    a.avvia()
    assert aspetta(lambda: len(reg.domande) >= 3, 6)
    assert len(reg.richieste) >= len(reg.domande)


def test_registrato_apre_il_tunnel_con_le_chiavi_del_server(tunnel, tmp_path):
    a, reg, _, chiamate_ssh = tunnel
    reg.dati = dict(BUONA, ssh_porta=2222)
    a.avvia()
    assert aspetta(lambda: a.stato()["collegato"])
    s = a.stato()
    assert s["indirizzo"] == "https://meshtastic-casa.esempio.it/" and "attivo" in s["messaggio"]
    argomenti = chiamate_ssh()[0]
    assert argomenti[-1] == "ponte-casa@meshtastic.esempio.it"
    assert "127.0.0.1:19443:127.0.0.1:9443" in argomenti and "2222" in argomenti
    for opzione in ("BatchMode=yes", "StrictHostKeyChecking=yes", "ExitOnForwardFailure=yes", "HostKeyAlias=mesh2tak-rete",
                    "PasswordAuthentication=no", "IdentitiesOnly=yes"):
        assert opzione in argomenti
    kh = (tmp_path / "stato" / "server-rete.known_hosts").read_text()
    assert kh == "mesh2tak-rete ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIB\n"


def test_chiave_rifiutata_aspetta_a_lungo(tunnel):
    a, reg, scenario, chiamate_ssh = tunnel
    reg.dati = BUONA
    scenario.write_text("rifiuto")
    a.ATTESA_RIFIUTO_S = 30.0
    a.avvia()
    assert aspetta(lambda: "rifiutato la chiave" in a.stato()["messaggio"])
    time.sleep(1.5)
    assert len(chiamate_ssh()) == 1                    # un solo tentativo, poi una lunga attesa


def test_porta_occupata_riprova_e_poi_si_collega(tunnel):
    a, reg, scenario, chiamate_ssh = tunnel
    reg.dati = BUONA
    scenario.write_text("porta")
    a.avvia()
    assert aspetta(lambda: "ancora occupata" in a.stato()["messaggio"])
    scenario.write_text("su")
    assert aspetta(lambda: a.stato()["collegato"])
    assert len(chiamate_ssh()) >= 2


def test_rete_giu_attese_crescenti_poi_si_collega(tunnel):
    a, reg, scenario, chiamate_ssh = tunnel
    reg.dati = BUONA
    scenario.write_text("giu")
    a.avvia()
    assert aspetta(lambda: "non raggiungibile" in a.stato()["messaggio"])
    scenario.write_text("su")
    assert aspetta(lambda: a.stato()["collegato"], 15)


def test_ferma_chiude_ssh(tunnel):
    a, reg, _, _ = tunnel
    reg.dati = BUONA
    a.avvia()
    assert aspetta(lambda: a.stato()["collegato"])
    proc = a._proc
    a.ferma()
    assert proc.poll() is not None


def test_ponte_tolto_dalla_rete_smette(tunnel):
    a, reg, scenario, chiamate_ssh = tunnel
    reg.dati = BUONA
    a.avvia()
    assert aspetta(lambda: a.stato()["collegato"])
    reg.dati = None                                      # tolto dalla pagina della rete: registrazione sparita...
    reg.risposta = (403, {"errore": "questo Raspberry e' stato tolto dalla rete sulla pagina della rete"})
    scenario.write_text("rifiuto")
    a._proc.terminate()                                  # ...e tunnel chiuso dal server
    assert aspetta(lambda: a.stato()["richiesta"] == "rifiutata", 15), a.stato()
    assert "Lascia rientrare" in a.stato()["messaggio"] and len(reg.domande) == 1   # richiede subito, una volta
    n = len(chiamate_ssh())
    time.sleep(1.0)
    assert len(chiamate_ssh()) == n                      # non bussa piu'
