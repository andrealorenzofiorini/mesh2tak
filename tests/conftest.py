import json
import os
import socket
import subprocess
import sys
import time

import pytest

QUI = os.path.dirname(os.path.abspath(__file__))
RADICE = os.path.dirname(QUI)
SRC = "/home/claude/src"
PERCORSI = [RADICE, os.environ.get("MT_SRC", f"{SRC}/meshtastic-python"), f"{SRC}/pypubsub/src", f"{SRC}/pyserial",
            f"{SRC}/zstd-target", f"{SRC}/takpacket-sdk/python/src"]
for p in PERCORSI:
    if p not in sys.path:
        sys.path.insert(0, p)
AMBIENTE = dict(os.environ, PYTHONPATH=os.pathsep.join(PERCORSI), TAKSDK=f"{SRC}/takpacket-sdk")
SDK = f"{SRC}/takpacket-sdk"


def porta_libera():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def aspetta_porta(porta, limite=10):
    fine = time.time() + limite
    while time.time() < fine:
        try:
            socket.create_connection(("127.0.0.1", porta), timeout=0.5).close()
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"porta {porta} non pronta")


@pytest.fixture(scope="session")
def pki(tmp_path_factory):
    d = tmp_path_factory.mktemp("pki")
    subprocess.run(["bash", os.path.join(QUI, "pki.sh"), str(d)], check=True, capture_output=True)
    return d


class TakFinto:
    def __init__(self, pki, cartella, chiudi_dopo=0):
        self.porta = porta_libera()
        self.uscita = cartella / f"eventi-{self.porta}.jsonl"
        args = [sys.executable, os.path.join(QUI, "fake_tak.py"), "--porta", str(self.porta),
                "--cert", str(pki / "takserver-chain.pem"), "--chiave", str(pki / "takserver.key"),
                "--ca", str(pki / "ca-chain.pem"), "--uscita", str(self.uscita)]
        if chiudi_dopo:
            args += ["--chiudi-dopo", str(chiudi_dopo)]
        self.proc = subprocess.Popen(args, env=AMBIENTE, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        aspetta_porta(self.porta)

    def righe(self):
        if not self.uscita.exists():
            return []
        return [json.loads(r) for r in self.uscita.read_text().splitlines() if r.strip()]

    def eventi(self):
        return [r for r in self.righe() if "type" in r and r["type"] != "t-x-c-t"]

    def ferma(self):
        self.proc.terminate()
        self.proc.wait(5)


@pytest.fixture
def tak_finto(pki, tmp_path):
    creati = []

    def crea(**kw):
        t = TakFinto(pki, tmp_path, **kw)
        creati.append(t)
        return t
    yield crea
    for t in creati:
        t.ferma()


def scrivi_config(percorso, pki, porta_tak, porta_nodo, dizionari, **extra):
    valori = {
        "tak": {"host": "127.0.0.1", "porta": porta_tak,
                "certificato": pki / "meshtastic-cnsas.pem",
                "chiave": pki / "meshtastic-cnsas-plain.key",
                "ca_server": pki / "ca-chain.pem"},
        "meshtastic": {"collegamento": f"tcp:127.0.0.1:{porta_nodo}", "canali": "privati"},
        "mappa": {"validita_min": 90, "aggiornamento_min": 0.01, "squadra": "Cyan"},
        "avanzate": {"dizionari": dizionari, "riconnessione_nodo_s": 2},
    }
    for chiave, v in extra.items():
        sez, nome = chiave.split("__")
        valori.setdefault(sez, {})[nome] = v
    righe = []
    for sez, kv in valori.items():
        righe.append(f"[{sez}]")
        righe += [f"{k} = {v}" for k, v in kv.items()]
        righe.append("")
    percorso.write_text("\n".join(righe))
    return percorso
