"""Aggiornamento del ponte dalla pagina della rete (1.8.0): mesh2tak-aggiorna (root, timer) legge la richiesta nella
registrazione del ponte, scarica l'installatore dalla release su GitHub, lo controlla, lo lancia, controlla che il ponte
riparta e altrimenti rimette la versione di prima. Qui con pagina della rete finta (HTTPS con una CA di prova), GitHub
finto, installatori finti e un ponte finto che risponde con la versione installata."""
import base64
import hashlib
import json
import os
import subprocess
import sys
import time

import pytest

from http_finto import ServerFinto as Server, crea_certificati

QUI = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(QUI), "aggiorna.py")
REPO = "andrealorenzofiorini/mesh2tak"
_BLOB = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(range(32))
CHIAVE_PUB = "ssh-ed25519 " + base64.b64encode(_BLOB).decode() + " mesh2tak-casa@raspberry"


def impronta():
    return hashlib.sha256(_BLOB).hexdigest()


@pytest.fixture(scope="module")
def certificati(tmp_path_factory):
    return crea_certificati(tmp_path_factory.mktemp("ca"))


def installatore(versione, esito=0, scrive=None):
    """Un installatore finto: scrive mesh2tak.py con la versione e lascia un segno; esito a scelta."""
    scrive = versione if scrive is None else scrive
    return (f'#!/usr/bin/env bash\n# mesh2tak {versione}\nVERSIONE="{versione}"\nset -e\n'
            f'echo "installo {versione}" >> "$MESH2TAK_PROVA_SEGNI"\n'
            f'printf \'VERSIONE = "%s"\\n\' "{scrive}" > "$MESH2TAK_AGGIORNA_DIR/mesh2tak.py"\n'
            f'cp "$0" "$MESH2TAK_AGGIORNA_DIR/installatore.sh"\n'
            f'[ {esito} = 0 ] || {{ echo "✘ Prova: qualcosa e\' andato storto"; exit {esito}; }}\n'
            'echo "Fatto."\n').encode()


class Raspberry:
    def __init__(self, tmp_path, certificati, installata="1.8.0", accesso=True):
        self.t = tmp_path
        self.dir = tmp_path / "opt"
        self.stato = tmp_path / "stato"
        self.dir.mkdir()
        (self.dir / "mesh2tak.py").write_text(f'VERSIONE = "{installata}"\n')
        (self.dir / "installatore.sh").write_bytes(installatore(installata))      # quello buono di adesso
        (tmp_path / "chiave.pub").write_text(CHIAVE_PUB + "\n")
        self.richiesta = None
        self.hub = Server({f"/accesso/{impronta()}.json": self.registrazione}, tls=certificati)
        self.github = Server({})
        self.rotte = set()                  # versioni con cui il ponte finto non riparte
        self.ponte = Server({"/mesh2tak/stato.json": self.stato_ponte}, tls=certificati)
        conf = ("[tak]\nhost = takserver.esempio.it\n\n[gestione]\nattiva = si   # come la scrive l'installatore\n"
                f"porta_pagina = {self.ponte.porta}\n")
        if accesso:
            conf += (f"\n[accesso_esterno]\nattivo = si\nnome = casa\nrete = https://127.0.0.1:{self.hub.porta}\n"
                     f"chiave = {tmp_path / 'chiave'}\n")
        (tmp_path / "mesh2tak.conf").write_text(conf)
        stub = tmp_path / "stub"
        stub.mkdir()
        (stub / "systemctl").write_text("#!/bin/bash\nexit 0\n")
        os.chmod(stub / "systemctl", 0o755)
        self.amb = dict(os.environ, PATH=f"{stub}:{os.environ['PATH']}", MESH2TAK_AGGIORNA_CONF=str(tmp_path / "mesh2tak.conf"),
                        MESH2TAK_AGGIORNA_DIR=str(self.dir), MESH2TAK_AGGIORNA_STATO=str(self.stato),
                        MESH2TAK_AGGIORNA_LOG=str(tmp_path / "aggiornamento.log"),
                        MESH2TAK_PROVA_GITHUB=f"http://127.0.0.1:{self.github.porta}",
                        SSL_CERT_FILE=str(certificati / "ca.pem"), MESH2TAK_AGGIORNA_ATTESA="3",
                        MESH2TAK_PROVA_SEGNI=str(tmp_path / "segni"))

    def registrazione(self):
        d = {"formato": 1, "nome": "casa", "utente": "ponte-casa", "porta": 19443, "ssh_host": "meshtastic.esempio.it",
             "ssh_porta": 22, "chiavi_host": ["ssh-ed25519 AAAA"], "chiave": CHIAVE_PUB}
        if self.richiesta:
            d["aggiorna"] = self.richiesta
        return d

    def stato_ponte(self):
        v = self.installata()
        return None if v in self.rotte else {"formato": 1, "versione": v}

    def pubblica(self, versione, script, somme=None):
        base = f"/{REPO}/releases/download/v{versione}"
        self.github.risposte[f"{base}/installa-ponte-meshtastic.sh"] = script
        self.github.risposte[f"{base}/SHA256SUMS"] = somme if somme is not None else (
            f"{hashlib.sha256(script).hexdigest()}  installa-ponte-meshtastic.sh\n"
            "0000000000000000000000000000000000000000000000000000000000000000  rete-meshtastic.sh\n").encode()

    def chiedi(self, versione, chiesto=1000):
        self.richiesta = {"versione": versione, "chiesto": chiesto}

    def lancia(self):
        return subprocess.run([sys.executable, "-I", SCRIPT], env=self.amb, capture_output=True, text=True, timeout=120)

    def installata(self):
        return (self.dir / "mesh2tak.py").read_text().split('"')[1]

    def esito(self):
        f = self.stato / "stato.json"
        return json.loads(f.read_text()) if f.exists() else None

    def segni(self):
        f = self.t / "segni"
        return f.read_text().splitlines() if f.exists() else []

    def ferma(self):
        for s in (self.hub, self.github, self.ponte):
            s.ferma()


@pytest.fixture
def pi(tmp_path, certificati):
    r = Raspberry(tmp_path, certificati)
    yield r
    r.ferma()


def test_niente_da_fare_senza_richiesta(pi):
    assert pi.lancia().returncode == 0
    assert pi.esito() is None and pi.segni() == [] and pi.installata() == "1.8.0"
    assert any(p.startswith("/accesso/") for p in pi.hub.richieste) and pi.github.richieste == []


def test_aggiornato_alla_versione_chiesta(pi):
    pi.pubblica("1.9.0", installatore("1.9.0"))
    pi.chiedi("1.9.0")
    r = pi.lancia()
    assert r.returncode == 0, r.stdout + r.stderr
    e = pi.esito()
    assert e["stato"] == "fatto" and e["versione"] == "1.9.0" and e["da"] == "1.8.0" and "dalla 1.8.0 alla 1.9.0" in e["messaggio"]
    assert pi.installata() == "1.9.0" and pi.segni() == ["installo 1.9.0"]
    assert (pi.stato / "installatore-precedente.sh").read_bytes() == installatore("1.8.0")   # per tornare indietro
    assert oct((pi.stato / "stato.json").stat().st_mode)[-3:] == "644" and oct(pi.stato.stat().st_mode)[-3:] == "755"
    assert pi.lancia().returncode == 0 and pi.segni() == ["installo 1.9.0"]                  # gia' fatto: niente


def test_file_diverso_da_quello_pubblicato_non_installato(pi):
    pi.pubblica("1.9.0", installatore("1.9.0"), somme=b"ffff  installa-ponte-meshtastic.sh\n")
    pi.chiedi("1.9.0")
    assert pi.lancia().returncode == 1
    e = pi.esito()
    assert e["stato"] == "errore" and "non e' quello pubblicato" in e["messaggio"]
    assert pi.segni() == [] and pi.installata() == "1.8.0"


def test_file_di_un_altra_versione_non_installato(pi):
    pi.pubblica("1.9.0", installatore("1.8.5"))
    pi.chiedi("1.9.0")
    assert pi.lancia().returncode == 1
    assert "non e' la versione 1.9.0" in pi.esito()["messaggio"] and pi.segni() == []


def test_mai_indietro(pi):
    pi.pubblica("1.7.0", installatore("1.7.0"))
    pi.chiedi("1.7.0")
    assert pi.lancia().returncode == 0 and pi.esito() is None and pi.github.richieste == []
    pi.chiedi("1.8.0")
    assert pi.lancia().returncode == 0 and pi.esito() is None


def test_installazione_non_riuscita_resta_la_versione_di_prima(pi):
    pi.pubblica("1.9.0", installatore("1.9.0", esito=3, scrive="1.8.0"))
    pi.chiedi("1.9.0")
    assert pi.lancia().returncode == 1
    e = pi.esito()
    assert e["stato"] == "errore" and "Prova: qualcosa e' andato storto" in e["messaggio"] and "resta la 1.8.0" in e["messaggio"]
    assert pi.installata() == "1.8.0"


def test_se_non_riparte_torna_alla_versione_di_prima(pi):
    pi.pubblica("1.9.0", installatore("1.9.0"))
    pi.chiedi("1.9.0")
    pi.rotte = {"1.9.0"}                              # con la 1.9.0 la pagina del ponte non risponde
    t0 = time.time()
    r = pi.lancia()
    assert r.returncode == 1, r.stdout + r.stderr
    e = pi.esito()
    assert e["stato"] == "ripristinato" and e["messaggio"] == "con la 1.9.0 il ponte non riparte; rimessa la 1.8.0"
    assert pi.segni() == ["installo 1.9.0", "installo 1.8.0"] and pi.installata() == "1.8.0"
    assert time.time() - t0 < 60
    assert not (pi.stato / "installatore-1.9.0.sh").exists()        # niente file lasciati in giro


def test_se_non_riparte_neanche_la_vecchia_lo_dice(pi):
    pi.pubblica("1.9.0", installatore("1.9.0"))
    pi.chiedi("1.9.0")
    pi.rotte = {"1.9.0", "1.8.0"}
    assert pi.lancia().returncode == 1
    e = pi.esito()
    assert e["stato"] == "errore" and "anche il ritorno alla 1.8.0 non e' riuscito: il ponte non riparte" in e["messaggio"]


def test_senza_pagina_del_ponte_controlla_la_versione_installata(pi):
    conf = (pi.t / "mesh2tak.conf").read_text().replace("attiva = si", "attiva = no")
    (pi.t / "mesh2tak.conf").write_text(conf)
    pi.rotte = {"1.9.0"}                              # la pagina non c'e': non va guardata
    pi.pubblica("1.9.0", installatore("1.9.0"))
    pi.chiedi("1.9.0")
    assert pi.lancia().returncode == 0 and pi.esito()["stato"] == "fatto"
    assert not any(p.startswith("/mesh2tak/") for p in pi.ponte.richieste)


def test_tentativi_limitati(pi):
    pi.pubblica("1.9.0", installatore("1.9.0", esito=3, scrive="1.8.0"))
    pi.chiedi("1.9.0")
    pi.lancia()
    assert pi.lancia().returncode == 0 and pi.segni() == ["installo 1.9.0"]      # riprova solo dopo 10 minuti
    for n in (2, 3):
        e = pi.esito()
        e["quando"] -= 700
        (pi.stato / "stato.json").write_text(json.dumps(e))
        pi.lancia()
        assert pi.esito()["tentativi"] == n
    e = pi.esito()
    e["quando"] -= 700
    (pi.stato / "stato.json").write_text(json.dumps(e))
    pi.lancia()
    assert len(pi.segni()) == 3                                                    # al massimo 3 per richiesta
    pi.chiedi("1.9.0", chiesto=2000)                                               # chiesto di nuovo dalla pagina
    pi.lancia()
    assert len(pi.segni()) == 4 and pi.esito()["tentativi"] == 1


def test_errore_con_la_riga_dell_installatore():
    sys.path.insert(0, os.path.dirname(SCRIPT))
    import aggiorna
    testo = "  ✔ uno\n\n✘ Non trovo il certificato.\n    Ultime righe di /var/log/x:\n      riga del log\n"
    assert aggiorna.ultima_riga(testo) == "Non trovo il certificato."
    assert aggiorna.ultima_riga("a\nb\n") == "b" and aggiorna.ultima_riga("") == "nessun messaggio"


def test_interrotto_a_meta_riprova_dopo(pi):
    pi.pubblica("1.9.0", installatore("1.9.0"))
    pi.chiedi("1.9.0")
    pi.stato.mkdir()
    (pi.stato / "stato.json").write_text(json.dumps({"stato": "in_corso", "versione": "1.9.0", "da": "1.8.0",
                                                     "richiesta": ["1.9.0", 1000], "tentativi": 1, "quando": 1}))
    assert pi.lancia().returncode == 0
    e = pi.esito()
    assert e["stato"] == "errore" and "si e' interrotto" in e["messaggio"] and pi.segni() == []
    e["quando"] -= 700
    (pi.stato / "stato.json").write_text(json.dumps(e))
    assert pi.lancia().returncode == 0
    assert pi.esito()["stato"] == "fatto" and pi.esito()["tentativi"] == 2 and pi.segni() == ["installo 1.9.0"]


def test_dopo_un_riavvio_a_meta_segna_fatto(pi):
    pi.pubblica("1.9.0", installatore("1.9.0"))
    pi.chiedi("1.9.0")
    pi.stato.mkdir()
    (pi.stato / "stato.json").write_text(json.dumps({"stato": "in_corso", "versione": "1.9.0", "da": "1.8.0",
                                                     "richiesta": ["1.9.0", 1000], "tentativi": 1, "quando": 1}))
    (pi.dir / "mesh2tak.py").write_text('VERSIONE = "1.9.0"\n')                     # installata prima del riavvio
    assert pi.lancia().returncode == 0
    assert pi.esito()["stato"] == "fatto" and pi.esito()["messaggio"] == "aggiornato dalla 1.8.0 alla 1.9.0"
    assert pi.segni() == []


def test_senza_accesso_da_internet_niente(tmp_path, certificati):
    r = Raspberry(tmp_path, certificati, accesso=False)
    try:
        assert r.lancia().returncode == 0 and r.hub.richieste == [] and r.esito() is None
    finally:
        r.ferma()


def test_un_aggiornamento_alla_volta(pi):
    import fcntl
    pi.pubblica("1.9.0", installatore("1.9.0"))
    pi.chiedi("1.9.0")
    pi.stato.mkdir()
    with open(pi.stato / ".blocco", "w") as b:
        fcntl.flock(b, fcntl.LOCK_EX)
        assert pi.lancia().returncode == 0 and pi.segni() == [] and pi.hub.richieste == []
    assert pi.lancia().returncode == 0 and pi.segni() == ["installo 1.9.0"]


def test_pagina_della_rete_con_certificato_sconosciuto(pi):
    pi.amb.pop("SSL_CERT_FILE")                       # senza la CA di prova: certificato non valido, niente
    pi.chiedi("1.9.0")
    pi.pubblica("1.9.0", installatore("1.9.0"))
    assert pi.lancia().returncode == 0 and pi.esito() is None and pi.segni() == []
    assert "registrazione non letta" in (pi.t / "aggiornamento.log").read_text()
    pi.lancia()
    assert (pi.t / "aggiornamento.log").read_text().count("registrazione non letta") == 1    # una volta sola


def test_registro_limitato(pi):
    (pi.t / "aggiornamento.log").write_bytes(b"vecchia riga\n" * 500_000)           # 6,5 MB
    pi.amb.pop("SSL_CERT_FILE")
    pi.lancia()
    testo = (pi.t / "aggiornamento.log").read_bytes()
    assert len(testo) < 1_100_000 and testo.startswith(b"vecchia riga\n") and b"registrazione non letta" in testo
