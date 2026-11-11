"""Script del server rete-meshtastic.sh: su un "server infra-TAK" finto (file sotto una cartella di prova),
con sshd e Caddy veri per i controlli di configurazione e Authentik finto per le API."""
import base64
import glob
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

from authentik_finto import TOKEN, AuthentikFinto
from conftest import RADICE, porta_libera
import mesh2tak as m

SCRIPT = os.path.join(RADICE, "dist", "rete-meshtastic.sh")
SSHD = "/opt/prova-openssh/sbin/sshd"
CADDY = {"2.10": "/opt/prova-caddy/caddy-2.10.2", "2.6": "/opt/prova-caddy/caddy-2.6.4"}
DOMINIO = "prova.lan"
NOMI_DNS = [f"meshtastic.{DOMINIO}", f"meshtastic-casa.{DOMINIO}", f"meshtastic-sede.{DOMINIO}"]
MARCATORE = "# --- User-added blocks (do not remove) ---"

pytestmark = pytest.mark.skipif(not (os.path.exists(SSHD) and os.path.exists(CADDY["2.10"]) and shutil.which("ssh-keygen")),
                                reason="servono sshd, ssh-keygen e caddy di prova")


def chiave(n):
    blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes([n]) * 32
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} mesh2tak-prova{n}@raspberry"


CADDYFILE_INFRATAK = f"""{{
	http_port 18080
	https_port 8443
	local_certs
	skip_install_trust
	admin off
}}

# infra-TAK Console
infratak.{DOMINIO} {{
	respond "console"
}}

# Directives INSIDE a generated site: /etc/caddy/sites.d/<host>/*.caddy (see README there).
{MARCATORE}
# Anything below this line survives every infra-TAK regeneration.
# Add custom site blocks here (extra domains, redirects, monitors).

health.{DOMINIO} {{
	respond "ok"
}}
"""


@pytest.fixture(scope="session", autouse=True)
def dns_di_prova():
    righe = open("/etc/hosts").read()
    aggiunte = "".join(f"127.0.0.1 {n}\n" for n in NOMI_DNS if n not in righe)
    if aggiunte:
        with open("/etc/hosts", "a") as f:
            f.write("# prove rete-meshtastic\n" + aggiunte)
    yield


class Server:
    def __init__(self, cartella, caddy="2.10", **ak):
        self.radice = cartella / "radice"
        self.stub = cartella / "stub"
        self.log = cartella / "chiamate.log"
        r = self.radice
        for d in ("etc/systemd/system", "opt/infratak/.config", "root/authentik", "etc/caddy", "etc/ssh/sshd_config.d",
                  "run"):
            (r / d).mkdir(parents=True, exist_ok=True)
        (r / "etc/systemd/system/takwerx-console.service").write_text("[Service]\nWorkingDirectory=/opt/infratak\n")
        self.impostazioni = {"fqdn": DOMINIO, "server_ip": "127.0.0.1", "ssl_mode": "letsencrypt", "caddy_https_port": 8443}
        self.scrivi_impostazioni()
        (r / "root/authentik/.env").write_text(f"PG_PASS=x\nAUTHENTIK_BOOTSTRAP_TOKEN={TOKEN}\n")
        (r / "etc/caddy/Caddyfile").write_text(CADDYFILE_INFRATAK)
        for tipo in ("ed25519", "ecdsa"):
            subprocess.run(["ssh-keygen", "-q", "-t", tipo, "-N", "", "-f", str(r / f"etc/ssh/ssh_host_{tipo}_key")],
                           check=True)
        (r / "etc/ssh/sshd_config").write_text(
            f"Include {r}/etc/ssh/sshd_config.d/*.conf\nPort 2222\nListenAddress 127.0.0.1\n"
            f"HostKey {r}/etc/ssh/ssh_host_ed25519_key\nHostKey {r}/etc/ssh/ssh_host_ecdsa_key\n"
            f"PidFile {r}/run/sshd.pid\nPasswordAuthentication yes\n")
        self.porte_occupate = cartella / "porte"
        self.porte_occupate.write_text("")
        self.caddy_fallisce = cartella / "caddy-fallisce"
        self.stub.mkdir()
        stub = {
            "systemctl": f'echo "systemctl $*" >> {self.log}\n'
                         'case "$*" in "show caddy"*) echo "";; esac\nexit 0',
            "docker": f'echo "docker $*" >> {self.log}\n'
                      'case "$1" in ps) echo authentik-server-1;; inspect) echo healthy;; esac\nexit 0',
            "sshd": f'echo "sshd $*" >> {self.log}\nexec {SSHD} -f {r}/etc/ssh/sshd_config "$@"',
            "caddy": f'echo "caddy $*" >> {self.log}\n[ -e {self.caddy_fallisce} ] && {{ echo "Error: adapting config: '
                     f'finto" >&2; exit 1; }}\nexec {CADDY[caddy]} "$@"',
            "ss": f'for p in $(cat {self.porte_occupate}); do echo "LISTEN 0 128 127.0.0.1:$p 0.0.0.0:*"; done',
        }
        for nome in ("useradd", "usermod", "userdel", "groupadd", "groupdel", "pkill", "resolvectl"):
            stub[nome] = f'echo "{nome} $*" >> {self.log}; exit 0'
        for nome, corpo in stub.items():
            (self.stub / nome).write_text("#!/bin/bash\n" + corpo + "\n")
            (self.stub / nome).chmod(0o755)
        self.porta_ak = porta_libera()
        self.ak = AuthentikFinto(self.porta_ak, **ak)

    def scrivi_impostazioni(self):
        (self.radice / "opt/infratak/.config/settings.json").write_text(json.dumps(self.impostazioni))

    def esegui(self, *argomenti):
        amb = dict(os.environ, RETE_PROVA_RADICE=str(self.radice), RETE_PROVA_AUTHENTIK=f"127.0.0.1:{self.porta_ak}",
                   PATH=f"{self.stub}:{os.environ['PATH']}")
        r = subprocess.run(["bash", SCRIPT, *argomenti], env=amb, capture_output=True, text=True, timeout=300,
                           stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout + r.stderr

    def file(self, percorso):
        p = self.radice / percorso.lstrip("/")
        return p.read_text() if p.exists() else None

    def chiamate(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def registrazioni(self):
        return {os.path.basename(f): json.load(open(f)) for f in glob.glob(str(self.radice / "var/lib/mesh2tak-rete/accesso/*.json"))}

    def ferma(self):
        self.ak.ferma()


@pytest.fixture
def server(tmp_path):
    creati = []

    def crea(**kw):
        s = Server(tmp_path / f"s{len(creati)}", **kw)
        creati.append(s)
        return s
    yield crea
    for s in creati:
        s.ferma()


def test_aggiungi_il_primo_ponte(server):
    s = server()
    esito, uscita = s.esegui("aggiungi", "casa", chiave(1))
    assert esito == 0, uscita
    assert "Fatto." in uscita and "https://meshtastic.prova.lan:8443/" in uscita
    # sshd: regole per il gruppo dei ponti, controllate dal vero sshd 8.9 (Ubuntu 22.04)
    dropin = s.file("/etc/ssh/sshd_config.d/mesh2tak-ponti.conf")
    for riga in ("Match Group ponti-meshtastic", f"AuthorizedKeysFile {s.radice}/etc/mesh2tak-rete/chiavi/%u",
                 "AllowTcpForwarding remote", "PermitOpen none", "PermitTTY no",
                 "ClientAliveInterval 30", "AuthenticationMethods publickey"):
        assert f"\t{riga}" in dropin or riga in dropin
    # dalla 1.10.0 il solo comando possibile e' quello dei ponti (configurazione e avanzamento dell'installazione)
    forzato = (f"/usr/bin/env RETE_PROVA_RADICE={s.radice} /usr/bin/python3 -I "
               f"{s.radice}/opt/mesh2tak-rete/rete_meshtastic.py ponte")
    assert f"\tForceCommand {forzato}\n" in dropin
    assert "sshd -t" in s.chiamate() and "systemctl reload ssh" in s.chiamate()
    autorizzata = s.file("/etc/mesh2tak-rete/chiavi/ponte-casa")
    assert autorizzata == f'restrict,port-forwarding,permitlisten="127.0.0.1:19443",command="{forzato}" {chiave(1)}\n'
    assert oct((s.radice / "etc/mesh2tak-rete/chiavi/ponte-casa").stat().st_mode)[-3:] == "644"
    assert any(c.startswith("useradd --system --no-create-home --home-dir /nonexistent --shell /bin/sh "
                            "--gid ponti-meshtastic") and c.endswith(" ponte-casa") for c in s.chiamate())
    assert "usermod -p * ponte-casa" in s.chiamate()
    # registrazione per il Raspberry, letta come la legge il Raspberry
    impronta = m.impronta_chiave(chiave(1))
    reg = s.registrazioni()[f"{impronta}.json"]
    r = m.AccessoEsterno.valida(reg)
    assert (r["utente"], r["porta"], r["host"], r["porta_ssh"], r["indirizzo"]) == \
        ("ponte-casa", 19443, "meshtastic.prova.lan", 2222, "https://meshtastic-casa.prova.lan:8443/")
    chiavi_server = sorted(" ".join(open(f).read().split()[:2]) for f in glob.glob(str(s.radice / "etc/ssh/ssh_host_*_key.pub")))
    assert sorted(r["chiavi"]) == chiavi_server
    # pagina della rete
    assert s.file("/var/lib/mesh2tak-rete/sito/index.html") == open(os.path.join(RADICE, "server/pagina/index.html")).read()
    assert "<svg" in s.file("/var/lib/mesh2tak-rete/sito/icona.svg")
    ponti = json.loads(s.file("/var/lib/mesh2tak-rete/sito/ponti.json"))
    assert ponti["ponti"] == [{"nome": "casa", "indirizzo": "https://meshtastic-casa.prova.lan:8443/",
                               "stato": "stato/casa.json", "aggiunto": ponti["ponti"][0]["aggiunto"]}]
    # Caddy: il file di infra-TAK intatto, piu' tre righe sotto il suo marcatore
    caddyfile = s.file("/etc/caddy/Caddyfile")
    assert caddyfile.startswith(CADDYFILE_INFRATAK.rstrip("\n"))
    sotto = caddyfile.split(MARCATORE, 1)[1]
    assert f"import {s.radice}/etc/caddy/mesh2tak-rete/*.caddy" in sotto
    rete = s.file("/etc/caddy/mesh2tak-rete/rete.caddy")
    assert "meshtastic.prova.lan {" in rete and "meshtastic-casa.prova.lan {" in rete
    assert "handle /stato/casa.json {" in rete and "reverse_proxy https://127.0.0.1:19443 {" in rete
    assert rete.count("copy_headers X-Authentik-Username") == 2 and rete.count("request_header -X-Authentik-Username") == 2
    assert any(c.startswith("caddy validate --config") for c in s.chiamate()) and "systemctl reload caddy" in s.chiamate()
    # Authentik: due applicazioni solo per gli amministratori, provider nell'avamposto, riavvio una volta
    ak = s.ak
    pv = {p["name"]: p for p in ak.provider.values()}
    assert pv["Rete Meshtastic"]["external_host"] == "https://meshtastic.prova.lan:8443"
    assert pv["Meshtastic casa"]["external_host"] == "https://meshtastic-casa.prova.lan:8443"
    for p in (pv["Rete Meshtastic"], pv["Meshtastic casa"]):
        assert (p["mode"], p["cookie_domain"], p["authorization_flow"], p["invalidation_flow"]) == \
            ("forward_single", ".prova.lan", "f-implicito", "f-inv")
    rete_app, ponte_app = ak.app["rete-meshtastic"], ak.app["meshtastic-casa"]
    assert rete_app["meta_launch_url"] == "https://meshtastic.prova.lan:8443/" and rete_app["open_in_new_tab"]
    assert rete_app["meta_icon"] == "https://meshtastic.prova.lan:8443/icona.svg"
    assert ponte_app["meta_launch_url"] == "blank://blank"
    assert ak.solo_admin("rete-meshtastic") and ak.solo_admin("meshtastic-casa") and not ak.solo_admin("tak-portal")
    assert sorted(ak.avamposto["providers"]) == sorted([7, pv["Rete Meshtastic"]["pk"], pv["Meshtastic casa"]["pk"]])
    assert [c for c in s.chiamate() if c.startswith("docker restart")] == ["docker restart authentik-server-1"]
    # registro (solo root)
    registro = json.loads(s.file("/etc/mesh2tak-rete/registro.json"))
    assert registro["ponti"]["casa"]["porta"] == 19443 and registro["ponti"]["casa"]["impronta"] == impronta
    assert oct((s.radice / "etc/mesh2tak-rete/registro.json").stat().st_mode)[-3:] == "600"


def test_rilancio_non_cambia_nulla(server):
    s = server()
    assert s.esegui("aggiungi", "casa", chiave(1))[0] == 0
    prima = (dict(s.ak.provider), dict(s.ak.app), list(s.ak.legami), list(s.ak.avamposto["providers"]))
    s.log.write_text("")
    esito, uscita = s.esegui("aggiungi", "casa", chiave(1))
    assert esito == 0, uscita
    assert "Caddy gia' a posto" in uscita and "avamposto di Authentik gia' a posto" in uscita
    # (useradd qui e' finto e non crea l'utente: lo si richiama; con l'utente vero no, vedi la prova completa)
    assert not [c for c in s.chiamate() if c.startswith(("docker restart", "systemctl reload", "pkill"))]
    assert (dict(s.ak.provider), dict(s.ak.app), list(s.ak.legami), list(s.ak.avamposto["providers"])) == prima


def test_secondo_ponte_porta_successiva_e_porte_occupate_saltate(server):
    s = server()
    s.porte_occupate.write_text("19443")            # un altro servizio sulla 19443
    assert s.esegui("aggiungi", "casa", chiave(1))[0] == 0
    esito, uscita = s.esegui("aggiungi", "sede", chiave(2))
    assert esito == 0, uscita
    registro = json.loads(s.file("/etc/mesh2tak-rete/registro.json"))
    assert (registro["ponti"]["casa"]["porta"], registro["ponti"]["sede"]["porta"]) == (19444, 19445)
    rete = s.file("/etc/caddy/mesh2tak-rete/rete.caddy")
    assert "handle /stato/sede.json" in rete and "meshtastic-sede.prova.lan {" in rete
    assert len([c for c in s.chiamate() if c.startswith("docker restart")]) == 2
    assert [p["nome"] for p in json.loads(s.file("/var/lib/mesh2tak-rete/sito/ponti.json"))["ponti"]] == ["casa", "sede"]


def test_stessa_chiave_su_due_ponti_rifiutata(server):
    s = server()
    assert s.esegui("aggiungi", "casa", chiave(1))[0] == 0
    esito, uscita = s.esegui("aggiungi", "sede", chiave(1))
    assert esito == 1 and "gia' del ponte 'casa'" in uscita
    assert "sede" not in json.loads(s.file("/etc/mesh2tak-rete/registro.json"))["ponti"]


def test_nuova_chiave_dello_stesso_ponte(server):
    s = server()
    assert s.esegui("aggiungi", "casa", chiave(1))[0] == 0
    s.log.write_text("")
    esito, uscita = s.esegui("aggiungi", "casa", chiave(3))
    assert esito == 0 and "cambia chiave" in uscita
    assert list(s.registrazioni()) == [f"{m.impronta_chiave(chiave(3))}.json"]   # la vecchia non vale piu'
    assert chiave(3) in s.file("/etc/mesh2tak-rete/chiavi/ponte-casa")
    assert "pkill -KILL -u ponte-casa" in s.chiamate()     # il tunnel con la chiave vecchia si chiude


@pytest.mark.parametrize("nome,chiave_", [("ca sa", chiave(1)), ("-casa", chiave(1)), ("casa;rm", chiave(1)), ("casa", "ssh-rsa AAAA x"),
                                          ("x" * 21, chiave(1))])
def test_argomenti_sbagliati(server, nome, chiave_):
    s = server()
    esito, uscita = s.esegui("aggiungi", nome, chiave_)
    assert esito != 0 and not s.file("/etc/mesh2tak-rete/registro.json")   # (-casa lo ferma gia' argparse)


def test_dns_mancante_spiega_cosa_aggiungere(server):
    s = server()
    esito, uscita = s.esegui("aggiungi", "nonce", chiave(1))
    assert esito == 1
    assert "meshtastic-nonce.prova.lan non esiste" in uscita
    assert "Type A   Name meshtastic-nonce   IPv4 address 127.0.0.1   Proxy status: DNS only" in uscita
    assert not s.file("/etc/mesh2tak-rete/registro.json") and not s.file("/etc/caddy/mesh2tak-rete/rete.caddy")
    assert s.file("/etc/caddy/Caddyfile") == CADDYFILE_INFRATAK


def test_dns_che_punta_altrove(server):
    s = server()
    s.impostazioni["server_ip"] = "203.0.113.7"
    s.scrivi_impostazioni()
    esito, uscita = s.esegui("aggiungi", "casa", chiave(1))
    assert esito == 1 and "punta a 127.0.0.1 invece che a 203.0.113.7" in uscita
    esito, uscita = s.esegui("aggiungi", "casa", chiave(1), "--ignora-dns")
    assert esito == 0, uscita


def test_caddy_rifiuta_e_tutto_torna_com_era(server):
    s = server()
    s.caddy_fallisce.write_text("")
    esito, uscita = s.esegui("aggiungi", "casa", chiave(1))
    assert esito == 1 and "Caddy rifiuta la configurazione" in uscita
    assert s.file("/etc/caddy/Caddyfile") == CADDYFILE_INFRATAK
    assert s.file("/etc/caddy/mesh2tak-rete/rete.caddy") is None
    assert "systemctl reload caddy" not in s.chiamate()
    assert not s.ak.app.get("rete-meshtastic")


@pytest.mark.parametrize("opzioni", [dict(politica_esistente=True), dict(group_membership_405=True),
                                     dict(icona_vecchio_stile=True)])
def test_versioni_di_authentik(server, opzioni):
    s = server(**opzioni)
    esito, uscita = s.esegui("aggiungi", "casa", chiave(1))
    assert esito == 0, uscita
    assert s.ak.solo_admin("rete-meshtastic") and s.ak.solo_admin("meshtastic-casa")
    if opzioni.get("politica_esistente"):
        assert list(s.ak.politiche) == ["pol-esistente"]
    if opzioni.get("group_membership_405"):
        assert any("ak_is_group_member" in p.get("expression", "") for p in s.ak.politiche.values())
    assert s.ak.app["rete-meshtastic"]["meta_icon"] == "https://meshtastic.prova.lan:8443/icona.svg"
    if opzioni.get("icona_vecchio_stile"):
        assert any(c[1].endswith("/set_icon_url/") for c in s.ak.chiamate)


def test_rimuovi(server):
    s = server()
    assert s.esegui("aggiungi", "casa", chiave(1))[0] == 0
    assert s.esegui("aggiungi", "sede", chiave(2))[0] == 0
    pk_casa = next(p["pk"] for p in s.ak.provider.values() if p["name"] == "Meshtastic casa")
    s.log.write_text("")
    esito, uscita = s.esegui("rimuovi", "casa", "--si")
    assert esito == 0, uscita
    assert list(s.registrazioni()) == [f"{m.impronta_chiave(chiave(2))}.json"]
    assert s.file("/etc/mesh2tak-rete/chiavi/ponte-casa") is None
    assert {"pkill -KILL -u ponte-casa"} <= set(s.chiamate())
    rete = s.file("/etc/caddy/mesh2tak-rete/rete.caddy")
    assert "meshtastic-casa" not in rete and "meshtastic-sede.prova.lan {" in rete
    assert "meshtastic-casa" not in s.ak.app and pk_casa not in s.ak.provider and pk_casa not in s.ak.avamposto["providers"]
    assert "meshtastic-sede" in s.ak.app
    assert [p["nome"] for p in json.loads(s.file("/var/lib/mesh2tak-rete/sito/ponti.json"))["ponti"]] == ["sede"]
    assert "rimuovi" in uscita or "accesso-esterno=no" in uscita
    esito, uscita = s.esegui("rimuovi", "casa", "--si")
    assert esito == 1 and "non e' registrato" in uscita


def test_rimuovi_senza_conferma_non_tocca_nulla(server):
    s = server()
    assert s.esegui("aggiungi", "casa", chiave(1))[0] == 0
    esito, uscita = s.esegui("rimuovi", "casa")
    assert esito == 1 and "--si" in uscita and s.registrazioni()


def test_disinstalla_rimette_il_server_com_era(server):
    s = server()
    assert s.esegui("aggiungi", "casa", chiave(1))[0] == 0
    assert s.esegui("aggiungi", "sede", chiave(2))[0] == 0
    esito, uscita = s.esegui("disinstalla", "--si")
    assert esito == 0, uscita
    assert s.file("/etc/caddy/Caddyfile").rstrip("\n") == CADDYFILE_INFRATAK.rstrip("\n")
    for p in ("/etc/caddy/mesh2tak-rete", "/var/lib/mesh2tak-rete", "/etc/ssh/sshd_config.d/mesh2tak-ponti.conf",
              "/etc/mesh2tak-rete/registro.json", "/etc/mesh2tak-rete/chiavi/ponte-casa"):
        assert not (s.radice / p.lstrip("/")).exists(), p
    assert set(s.ak.app) == {"tak-portal"} and set(s.ak.provider) == {7} and s.ak.avamposto["providers"] == [7]
    assert {"pkill -KILL -u ponte-casa", "pkill -KILL -u ponte-sede"} <= set(s.chiamate())   # tunnel chiusi


def test_elenco(server):
    s = server()
    esito, uscita = s.esegui("elenco")
    assert esito == 0 and "nessun ponte registrato" in uscita
    assert s.esegui("aggiungi", "casa", chiave(1))[0] == 0
    assert "tunnel non collegato" in s.esegui("elenco")[1]
    s.porte_occupate.write_text("19443")            # tunnel del Raspberry aperto
    assert "tunnel collegato" in s.esegui("elenco")[1]


def rigenera_come_infratak(testo):
    """La parte di generate_caddyfile() di infra-TAK che conserva i blocchi sotto il marcatore (v10.1.0+)."""
    generato = (testo.split(MARCATORE, 1)[0].rstrip() + "\n\n# infra-TAK ha rigenerato la sua parte\n")
    after = testo.split(MARCATORE, 1)[1]
    aiuti = ('# Anything below this line survives', '# Add custom site blocks here')
    utente = "\n".join(x for x in after.splitlines() if not x.strip().startswith(aiuti)).strip("\n")
    coda = ("\n\n# Directives INSIDE a generated site: /etc/caddy/sites.d/<host>/*.caddy (see README there).\n"
            + MARCATORE + "\n# Anything below this line survives every infra-TAK regeneration.\n"
            + "# Add custom site blocks here (extra domains, redirects, monitors).\n")
    if utente:
        coda += "\n" + utente + "\n"
    return generato.rstrip() + coda


@pytest.mark.parametrize("versione", ["2.10", "2.6"])
def test_caddy_accetta_e_infratak_conserva(server, versione):
    s = server(caddy=versione)
    assert s.esegui("aggiungi", "casa", chiave(1))[0] == 0
    caddyfile = s.radice / "etc/caddy/Caddyfile"
    rigenerato = rigenera_come_infratak(caddyfile.read_text())
    assert "import " + str(s.radice) + "/etc/caddy/mesh2tak-rete/*.caddy" in rigenerato.split(MARCATORE, 1)[1]
    caddyfile.write_text(rigenerato)
    r = subprocess.run([CADDY[versione], "validate", "--config", str(caddyfile), "--adapter", "caddyfile"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]
    # e uno 'aggiorna' dopo la rigenerazione non duplica le tre righe
    assert s.esegui("aggiorna")[0] == 0
    assert s.file("/etc/caddy/Caddyfile").count("import " + str(s.radice) + "/etc/caddy/mesh2tak-rete/*.caddy") == 1


def test_certificato_proprio_di_infratak(server, tmp_path):
    s = server()
    cert = s.radice / "etc/caddy/custom"
    cert.mkdir()
    for nome, cn, san in (("jolly", "*.prova.lan", "DNS:*.prova.lan,DNS:prova.lan"), ("solo", "prova.lan", "DNS:prova.lan")):
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                        "-days", "30", "-subj", f"/CN={cn}", "-addext", f"subjectAltName={san}",
                        "-keyout", str(cert / f"{nome}.key"), "-out", str(cert / f"{nome}.pem")],
                       check=True, capture_output=True)
    s.impostazioni["ssl_mode"] = "custom"
    s.scrivi_impostazioni()
    testo = CADDYFILE_INFRATAK.replace('\trespond "console"', f'\ttls {cert}/solo.pem {cert}/solo.key\n\trespond "console"')
    # nel Caddyfile il percorso vero (lo legge Caddy); lo script lo cerca sotto la cartella di prova
    (s.radice / "etc/caddy/Caddyfile").write_text(testo.replace(f"{cert}/", "/etc/caddy/custom/"))
    os.makedirs("/etc/caddy/custom", exist_ok=True)
    for f in ("solo.pem", "solo.key", "jolly.pem", "jolly.key"):
        shutil.copy(cert / f, f"/etc/caddy/custom/{f}")
    try:
        esito, uscita = s.esegui("aggiungi", "casa", chiave(1))
        assert esito == 1 and "non copre meshtastic.prova.lan" in uscita
        (s.radice / "etc/caddy/Caddyfile").write_text(testo.replace(f"{cert}/solo", "/etc/caddy/custom/jolly"))
        esito, uscita = s.esegui("aggiungi", "casa", chiave(1))
        assert esito == 0, uscita
        assert s.file("/etc/caddy/mesh2tak-rete/rete.caddy").count("\ttls /etc/caddy/custom/jolly.pem /etc/caddy/custom/jolly.key") == 2
    finally:
        shutil.rmtree("/etc/caddy/custom", ignore_errors=True)


# ---- 1.5.0: installa una volta, poi i ponti dalla pagina ----

def test_installa_senza_ponti(server):
    s = server()
    esito, uscita = s.esegui("installa")
    assert esito == 0, uscita
    assert "Da ora i ponti si aggiungono dalla pagina" in uscita and "installa-ponte-meshtastic.sh --accesso-esterno" in uscita
    assert json.loads(s.file("/var/lib/mesh2tak-rete/sito/ponti.json"))["ponti"] == []
    # servizio della pagina: utente senza privilegi, una sola cartella scrivibile, script copiato in /opt
    web = s.file("/etc/systemd/system/mesh2tak-rete-web.service")
    for riga in ("User=mesh2tak-rete", "NoNewPrivileges=true", "ProtectSystem=strict", "CapabilityBoundingSet=",
                 "ExecStart=/usr/bin/python3 -I /opt/mesh2tak-rete/rete_meshtastic.py servi",
                 "ReadWritePaths=/var/lib/mesh2tak-rete/servizio"):
        assert riga + "\n" in web, riga
    assert "DirectoryNotEmpty=/var/lib/mesh2tak-rete/servizio/lavori" in s.file("/etc/systemd/system/mesh2tak-rete-lavori.path")
    assert "ExecStart=/usr/bin/python3 -I /opt/mesh2tak-rete/rete_meshtastic.py lavora" in \
        s.file("/etc/systemd/system/mesh2tak-rete-lavori.service")
    copia = s.file("/opt/mesh2tak-rete/rete_meshtastic.py")
    assert copia and "PAGINA_B64 = \"@@" not in copia
    compile(copia, "rete_meshtastic.py", "exec")
    for d, modo in (("servizio", "700"), ("servizio/richieste", "700"), ("servizio/lavori", "700"), ("servizio/tmp", "700"),
                    ("esiti", "755"), ("sito", "755"), ("accesso", "755")):
        assert oct((s.radice / "var/lib/mesh2tak-rete" / d).stat().st_mode)[-3:] == modo, d
    assert any(c.startswith("useradd --system") and c.endswith(" mesh2tak-rete") for c in s.chiamate())
    assert "systemctl enable --now mesh2tak-rete-web.service mesh2tak-rete-lavori.path" in s.chiamate()
    # Caddy e Authentik: la pagina della rete, senza ponti
    rete = s.file("/etc/caddy/mesh2tak-rete/rete.caddy")
    assert "meshtastic.prova.lan {" in rete and "meshtastic-" not in rete.split("meshtastic.prova.lan {", 1)[1]
    assert set(a for a in s.ak.app if a != "tak-portal") == {"rete-meshtastic"}
    # rilancio: niente da rifare, niente riavvii
    s.log.write_text("")
    esito, uscita = s.esegui("installa")
    assert esito == 0, uscita
    assert not [c for c in s.chiamate() if c.startswith(("docker restart", "systemctl reload", "systemctl restart"))]


def test_caddy_richieste_pubbliche_e_azioni_dietro_authentik(server):
    s = server()
    assert s.esegui("installa")[0] == 0
    assert s.esegui("aggiungi", "casa", chiave(1))[0] == 0
    rete = s.file("/etc/caddy/mesh2tak-rete/rete.caddy")
    pagina = rete.split("meshtastic.prova.lan {", 1)[1].split("\n}\n", 1)[0]
    i_richiesta, i_auth, i_api = (pagina.index("handle /accesso/richiesta {"), pagina.index("forward_auth"),
                                  pagina.index("handle /api/* {"))
    assert i_richiesta < i_auth < i_api        # la richiesta dei Raspberry e' pubblica, le azioni no
    # dalla 1.10.0: certificato dei ponti nuovi (piu' grande) e radio gestite attraverso i ponti, solo dopo Authentik;
    # dalla 1.13.0 anche il certificato nuovo di un ponte e il registro raccolto dal Raspberry
    i_impostazioni, i_radio = pagina.index("handle @certificati {"), pagina.index("handle_path /radio/casa/* {")
    assert "@certificati path /api/impostazioni-nuovi /api/certificato-ponte" in pagina
    assert i_auth < i_impostazioni < i_api and i_auth < i_radio
    assert pagina.index("handle /radio/* {") > i_radio            # le radio di un ponte che non c'e': 404
    assert pagina.count("reverse_proxy 127.0.0.1:19400 {") == 3 and pagina.count("max_size 16KB") == 3
    assert pagina.count("max_size 400KB") == 1
    radio = pagina[i_radio:].split("\n\t\t}\n", 1)[0]
    assert "rewrite * /mesh2tak/radio{uri}" in radio and "reverse_proxy https://127.0.0.1:19443 {" in radio
    i_registro = pagina.index("handle /registro/casa.txt {")
    registro = pagina[i_registro:].split("\n\t\t}\n", 1)[0]
    assert i_auth < i_registro < pagina.index("handle /registro/* {")
    assert "rewrite * /mesh2tak/registro" in registro and "reverse_proxy https://127.0.0.1:19443 {" in registro
    # dalla 1.14.0 la mappa della copertura del ponte, anche lei solo dopo Authentik
    i_copertura = pagina.index("handle /copertura/casa.json {")
    copertura = pagina[i_copertura:].split("\n\t\t}\n", 1)[0]
    assert i_auth < i_copertura < pagina.index("handle /copertura/* {")
    assert "rewrite * /mesh2tak/copertura.json" in copertura and "max_size 1KB" in copertura
    assert "header_up X-Forwarded-Host {hostport}" in copertura and "reverse_proxy https://127.0.0.1:19443 {" in copertura
    # le mappe di sfondo vengono da OpenTopoMap e OpenStreetMap; script e richieste solo dalla pagina stessa
    csp = re.search(r'header Content-Security-Policy "([^"]*)"', pagina).group(1)
    assert "img-src 'self' data: https://*.tile.opentopomap.org https://tile.openstreetmap.org;" in csp
    assert "script-src 'self' 'unsafe-inline';" in csp and "connect-src 'self';" in csp
    # nessun cookie del browser (sessione di Authentik) arriva al servizio o ai Raspberry
    for blocco in re.findall(r"reverse_proxy (?:https://)?127\.0\.0\.1:\d+ \{(.*?)\n\t\t\t?\}", rete, re.S):
        assert "header_up -Cookie" in blocco and "header_up -Authorization" in blocco, blocco
    assert rete.count("header_up -Cookie") == 8


def lavoro_in_coda(s, **dati):
    cartella = s.radice / "var/lib/mesh2tak-rete/servizio/lavori"
    lavoro = dict({"id": "a1b2c3d4e5f60718", "da": "andrea", "richiesta": None, "sostituisci": False}, **dati)
    (cartella / f"{lavoro['id']}.json").write_text(json.dumps(lavoro))
    return lavoro["id"]


def esito_di(s, lavoro):
    return json.loads(s.file(f"/var/lib/mesh2tak-rete/esiti/{lavoro}.json"))


def test_passo_dei_lavori_dal_vero(server):
    s = server()
    (s.radice / "var/log").mkdir(parents=True, exist_ok=True)
    assert s.esegui("installa")[0] == 0
    lavoro = lavoro_in_coda(s, azione="aggiungi", nome="casa", chiave=chiave(1))
    esito, uscita = s.esegui("lavora")
    assert esito == 0, uscita
    e = esito_di(s, lavoro)
    assert e["esito"] == "ok" and "4/5  Authentik" in e["testo"] and "\x1b[" not in e["testo"], e
    assert "5/5  Pagina della rete e registrazione per il Raspberry" in e["testo"]
    assert list(s.registrazioni()) == [f"{m.impronta_chiave(chiave(1))}.json"]
    assert json.loads(s.file("/etc/mesh2tak-rete/registro.json"))["ponti"]["casa"]["porta"] == 19443
    assert not list((s.radice / "var/lib/mesh2tak-rete/servizio/lavori").iterdir())
    assert "lavoro a1b2c3d4e5f60718: aggiungi casa chiesto da andrea" in s.file("/var/log/mesh2tak-rete.log")
    # la registrazione porta anche la chiave del Raspberry (per "Lascia rientrare" dopo averlo tolto)
    assert next(iter(s.registrazioni().values()))["chiave"] == chiave(1)
    lavoro = lavoro_in_coda(s, id="b1b2c3d4e5f60718", azione="rimuovi", nome="casa")
    assert s.esegui("lavora")[0] == 0
    assert esito_di(s, lavoro)["esito"] == "ok" and s.registrazioni() == {}
    assert "meshtastic-casa" not in s.ak.app


def test_passo_dei_lavori_cambia_chiave_solo_sostituendo(server):
    s = server()
    assert s.esegui("aggiungi", "casa", chiave(1))[0] == 0
    lavoro = lavoro_in_coda(s, azione="aggiungi", nome="casa", chiave=chiave(2))
    assert s.esegui("lavora")[0] == 0
    e = esito_di(s, lavoro)
    assert e["esito"] == "errore" and "Sostituisci" in e["messaggio"]
    assert list(s.registrazioni()) == [f"{m.impronta_chiave(chiave(1))}.json"]
    lavoro = lavoro_in_coda(s, id="c1b2c3d4e5f60718", azione="aggiungi", nome="casa", chiave=chiave(2), sostituisci=True)
    assert s.esegui("lavora")[0] == 0
    assert esito_di(s, lavoro)["esito"] == "ok"
    assert list(s.registrazioni()) == [f"{m.impronta_chiave(chiave(2))}.json"]
    assert "pkill -KILL -u ponte-casa" in s.chiamate()     # il Raspberry vecchio esce subito


def test_disinstalla_toglie_anche_il_servizio_della_pagina(server):
    s = server()
    assert s.esegui("installa")[0] == 0
    assert s.esegui("disinstalla", "--si")[0] == 0
    for p in ("/etc/systemd/system/mesh2tak-rete-web.service", "/etc/systemd/system/mesh2tak-rete-lavori.service",
              "/etc/systemd/system/mesh2tak-rete-lavori.path", "/opt/mesh2tak-rete", "/var/lib/mesh2tak-rete"):
        assert not (s.radice / p.lstrip("/")).exists(), p
    assert "systemctl disable --now mesh2tak-rete-web.service mesh2tak-rete-lavori.path" in s.chiamate()


def test_lavoro_non_riuscito_non_lascia_il_ponte_a_meta(server):
    s = server()
    assert s.esegui("installa")[0] == 0
    s.caddy_fallisce.write_text("")
    lavoro = lavoro_in_coda(s, azione="aggiungi", nome="casa", chiave=chiave(1))
    assert s.esegui("lavora")[0] == 0
    e = esito_di(s, lavoro)
    assert e["esito"] == "errore" and "Caddy rifiuta" in e["messaggio"]
    # niente registrazione (il Raspberry non bussa), niente ponte nel registro o sulla pagina: si puo' riprovare
    assert s.registrazioni() == {} and json.loads(s.file("/var/lib/mesh2tak-rete/sito/ponti.json"))["ponti"] == []
    assert "casa" not in json.loads(s.file("/etc/mesh2tak-rete/registro.json") or '{"ponti": {}}')["ponti"]
    s.caddy_fallisce.unlink()
    lavoro = lavoro_in_coda(s, id="d1b2c3d4e5f60718", azione="aggiungi", nome="casa", chiave=chiave(1))
    assert s.esegui("lavora")[0] == 0
    assert esito_di(s, lavoro)["esito"] == "ok" and len(s.registrazioni()) == 1
    assert json.loads(s.file("/etc/mesh2tak-rete/registro.json"))["ponti"]["casa"]["porta"] == 19443


def test_avvisi_per_email_preparati_da_installa(server):
    """1.7.0: installa dice dove arrivano gli avvisi (gli indirizzi di Guard Dog in infra-TAK), la prima volta manda
    un'email di prova e scrive per il servizio le porte dei tunnel e quella della console."""
    import http.server
    import threading
    ricevute = []

    class Console(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):  # noqa: N802
            ricevute.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0))))
            corpo = b'{"success": true, "suppressed": false}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

    console = http.server.ThreadingHTTPServer(("127.0.0.1", porta_libera()), Console)
    threading.Thread(target=console.serve_forever, daemon=True).start()
    try:
        s = server()
        esito, uscita = s.esegui("installa")          # senza indirizzo per gli avvisi in infra-TAK
        assert esito == 0, uscita
        assert "non c'e' un indirizzo per gli avvisi" in uscita and ricevute == []
        sorv = json.loads(s.file("/var/lib/mesh2tak-rete/sorveglianza.json"))
        assert sorv == {"formato": 1, "pagina": "https://meshtastic.prova.lan:8443/", "console": 5001, "ponti": []}
        s.impostazioni.update(guarddog_alert_email="info@prova.lan", console_port=console.server_address[1])
        s.scrivi_impostazioni()
        assert s.esegui("aggiungi", "casa", chiave(1))[0] == 0
        esito, uscita = s.esegui("installa")
        assert esito == 0, uscita
        assert "avvisi per email a: info@prova.lan" in uscita and "email di prova mandata" in uscita
        assert len(ricevute) == 1 and ricevute[0]["subject"] == "[Rete Meshtastic] Email di prova" and ricevute[0]["to"] == ""
        assert "https://meshtastic.prova.lan:8443/" in ricevute[0]["body"]
        sorv = json.loads(s.file("/var/lib/mesh2tak-rete/sorveglianza.json"))
        assert sorv["console"] == console.server_address[1] and sorv["ponti"] == [{"nome": "casa", "porta": 19443}]
        esito, uscita = s.esegui("installa")          # la seconda volta niente email di prova
        assert esito == 0 and len(ricevute) == 1 and "avvisi per email a: info@prova.lan" in uscita
        assert s.esegui("disinstalla", "--si")[0] == 0
        assert s.file("/etc/mesh2tak-rete/avvisi-provati") is None
    finally:
        console.shutdown()
        console.server_close()
