"""Gateway in ascolto, dal vero (dalla 1.23.0): un Raspberry che lavora per un altro server chiede a questo di
ascoltarlo; approvato dalla pagina apre il tunnel SSH (sshd vero) verso la porta dei pacchetti, e il convertitore di
questo server (lo stesso programma del gateway) li manda al suo TAK Server."""
import json
import os
import subprocess
import sys
import time
import zipfile

import pytest

from conftest import AMBIENTE, TakFinto, porta_libera
from test_rete_e2e import RETE, Rete, aspetta, pulisci_utenti, nuova_chiave, pytestmark  # noqa: F401
import mesh2tak as m

NOMI = ["meshtastic.prova.lan", "meshtastic-altro.prova.lan"]


@pytest.fixture(scope="module")
def rete(tmp_path_factory, pki):
    righe = open("/etc/hosts").read()
    with open("/etc/hosts", "a") as f:
        for n in NOMI:
            if n not in righe:
                f.write(f"127.0.0.1 {n}\n")
    pulisci_utenti()
    subprocess.run(["userdel", "mesh2tak-converte"], capture_output=True)
    r = Rete(tmp_path_factory.mktemp("rete"), pki)
    r.pki = pki
    di_prima = r.ambiente
    r.ambiente = lambda: dict(di_prima(), RETE_PROVA_PYTHON=sys.executable)   # niente download delle librerie
    r.tak = TakFinto(pki, r.c)
    r.convertitore = None
    yield r
    if r.convertitore is not None:
        r.convertitore.terminate()
        r.convertitore.wait(10)
    r.tak.ferma()
    r.ferma()
    subprocess.run(["userdel", "mesh2tak-converte"], capture_output=True)


def pacchetto_posizione(da, lat, lon, canale=1):
    from meshtastic.protobuf import mesh_pb2, portnums_pb2
    pos = mesh_pb2.Position(latitude_i=int(lat * 1e7), longitude_i=int(lon * 1e7), altitude=400, time=int(time.time()))
    mp = mesh_pb2.MeshPacket(id=int(time.time() * 1000) & 0x7fffffff, channel=canale, rx_time=int(time.time()),
                             rx_snr=5.0, hop_start=3, hop_limit=3)
    setattr(mp, "from", da)
    mp.to = 0xffffffff
    mp.decoded.portnum = portnums_pb2.PortNum.POSITION_APP
    mp.decoded.payload = pos.SerializeToString()
    return mp


def test_1_senza_certificato_non_si_approva(rete, tmp_path):
    esito, uscita = rete.esegui("installa")
    assert esito == 0, uscita
    # nessun gateway in ascolto: niente convertitore, niente utente, niente librerie
    assert not (rete.r / "opt/mesh2tak-rete/convertitore").exists()
    assert subprocess.run(["id", "-u", "mesh2tak-converte"], capture_output=True).returncode != 0
    rete.avvia_servizio()
    # il Raspberry "altro": lavora per un altro server, qui chiede solo di essere ascoltato
    rete.inoltro = m.Inoltro(porta_libera())
    rete.inoltro.avvia()
    chiave = nuova_chiave(tmp_path, "altro")
    cfg = m.Config(**{**rete.cfg.__dict__, "accesso_chiave": str(chiave), "accesso_nome": "altro",
                      "accesso_rete": "https://principale.invalid"})
    a = m.AccessoEsterno(cfg, rete.inoltro.porta, rete=RETE, inoltro=True)
    a.ATTESA_REGISTRAZIONE = (0.5, 0.5)
    a.RICHIESTA_OGNI_S = a.RICHIESTA_RIPROVA_S = a.ATTESA_PORTA_OCCUPATA_S = a.DURATA_STABILE_S = 1.0
    a.ATTESA_RETE = (0.5, 1.0)
    a.ATTESA_RIFIUTO_S = 3.0
    a.CONFERMA_S = 0.5
    a.contesto_https = rete.contesto()
    a.avvia()
    rete.altri.append(a)
    rete.a = a
    assert aspetta(lambda: a.stato()["richiesta"] == "in_attesa", 30), a.stato()
    [r] = rete.stato_api()["richieste"]
    assert r["nome"] == "altro" and r["ascolto"] is True
    codice, d = rete.api("POST", "/api/approva", {"id": r["id"]})
    assert codice == 202, d
    assert aspetta(lambda: any(x["esito"] == "errore" for x in rete.stato_api()["lavori"]), 60), rete.esiti_lavora
    [lavoro] = rete.stato_api()["lavori"]
    assert "manca il certificato" in lavoro["messaggio"] and "certificato-convertitore" in lavoro["messaggio"]
    assert "ponte-altro" not in rete.sshd_log() and not a.stato()["collegato"]
    assert json.load(open(rete.r / "etc/mesh2tak-rete/registro.json"))["ponti"] == {}


def test_2_con_il_certificato_il_server_ascolta_e_converte(rete, tmp_path):
    pki = rete.pki
    z = tmp_path / "ascolto-integration.zip"
    with zipfile.ZipFile(z, "w") as f:
        for nome in ("meshtastic-cnsas.pem", "meshtastic-cnsas-plain.key", "ca-chain.pem"):
            f.write(pki / nome, nome)
        f.writestr("README.txt", f"Server: 127.0.0.1\nPort: {rete.tak.porta}\n")
    esito, uscita = rete.esegui("certificato-convertitore", "--pacchetto", str(z))
    assert esito == 0 and "per i gateway in ascolto" in uscita, uscita
    conf = rete.r / "etc/mesh2tak-rete/convertitore"
    assert oct(os.stat(conf / "client.key").st_mode & 0o777) == "0o640"
    assert json.load(open(conf / "tak.json"))["porta"] == rete.tak.porta
    # "Riprova" sulla pagina (qui: la richiesta torna in attesa e si approva di nuovo)
    assert aspetta(lambda: [x for x in rete.stato_api()["richieste"] if x["stato"] in ("attesa", "errore")], 30)
    [r] = rete.stato_api()["richieste"]
    codice, d = rete.api("POST", "/api/approva", {"id": r["id"]})
    assert codice == 202, d
    assert aspetta(lambda: rete.a.stato()["collegato"], 90), (rete.a.stato(), rete.esiti_lavora)
    p = json.load(open(rete.r / "etc/mesh2tak-rete/registro.json"))["ponti"]["altro"]
    assert p["ascolto"] is True and p["porta_inoltro"] != p["porta"]
    [reg] = [json.load(open(f)) for f in (rete.r / "var/lib/mesh2tak-rete/accesso").glob("*.json")]
    assert reg["inoltro"] is True and reg["porta"] == p["porta_inoltro"]
    assert f'permitlisten="127.0.0.1:{p["porta_inoltro"]}"' in (rete.r / "etc/mesh2tak-rete/chiavi/ponte-altro").read_text()
    assert "systemctl enable --now mesh2tak-rete-converte@altro.service" in rete.log.read_text()
    unita = (rete.r / "etc/systemd/system/mesh2tak-rete-converte@.service").read_text()
    assert "User=mesh2tak-converte" in unita and "NoNewPrivileges=true" in unita
    # il convertitore, come lo farebbe partire systemd: stesso programma del gateway, la sua configurazione
    testo = (conf / "altro.conf").read_text()
    assert f"collegamento = inoltro:http://127.0.0.1:{p['porta_inoltro']}" in testo
    rete.convertitore = subprocess.Popen(
        [sys.executable, str(rete.r / "opt/mesh2tak-rete/convertitore/mesh2tak.py"), "-c", str(conf / "altro.conf")],
        env=dict(AMBIENTE, MESH2TAK_ATTESE="0.1"), stdout=open(rete.c / "convertitore.log", "w"),
        stderr=subprocess.STDOUT)
    # il Raspberry si collega alla sua radio e sente una radio della squadra
    rete.inoltro.descrivi({"num": 0x11111111, "preset": "MEDIUM_FAST", "salti": 3, "potenza_dbm": 27, "regione": "EU_868",
                           "canali": [[0, "Pubblico", False], [1, "Squadra", True]], "canali_ok": [1],
                           "nodi": {str(0x22222222): {"num": 0x22222222, "user": {"id": "!22222222",
                                                                                 "longName": "Radio di prova",
                                                                                 "shortName": "RP"}}}})

    def manda():
        mp = pacchetto_posizione(0x22222222, 45.82, 8.83)
        rete.inoltro.aggiungi({"raw": mp, "decoded": {}})
        return [e for e in rete.tak.eventi() if e.get("uid") == "!22222222"]
    assert aspetta(manda, 40), (rete.c / "convertitore.log").read_text()[-3000:]
    eventi = manda()
    assert abs(eventi[-1]["lat"] - 45.82) < 1e-4 and abs(eventi[-1]["lon"] - 8.83) < 1e-4
    # per la pagina della rete e' un gateway come gli altri: lo stato arriva dal convertitore
    def stato():
        codice, corpo, _ = rete.chiedi(f"{RETE}/stato/altro.json")
        return json.loads(corpo) if codice == 200 else None
    assert aspetta(stato, 20)
    st = stato()
    assert st["ascolto"] is True and st["nodo"]["collegato"] is True and st["tak"]["collegato"] is True
    codice, corpo, _ = rete.chiedi(f"{RETE}/ponti.json")
    [voce] = json.loads(corpo)["ponti"]
    assert voce["ascolto"] is True and voce["indirizzo"] == ""


def test_3_tolto_dalla_pagina_si_ferma_tutto(rete):
    codice, d = rete.api("POST", "/api/rimuovi", {"nome": "altro"})
    assert codice == 202, d
    assert aspetta(lambda: "systemctl disable --now mesh2tak-rete-converte@altro.service" in rete.log.read_text(), 60)
    assert aspetta(lambda: not rete.a.stato()["collegato"], 60)
    assert not (rete.r / "etc/mesh2tak-rete/convertitore/altro.conf").exists()
    assert json.load(open(rete.r / "etc/mesh2tak-rete/registro.json"))["ponti"] == {}
