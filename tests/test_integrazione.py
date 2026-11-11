"""Prove complete: nodo Meshtastic finto + TAK Server finto (TLS con certificato client) + ponte vero."""
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from conftest import AMBIENTE, QUI, RADICE, SDK, aspetta_porta, porta_libera, scrivi_config
import mesh2tak as m

PONTE = os.path.join(RADICE, "mesh2tak.py")


@pytest.fixture
def nodo_finto():
    porta = porta_libera()
    proc = subprocess.Popen([sys.executable, os.path.join(QUI, "fake_node.py"), "--porta", str(porta)],
                            env=AMBIENTE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    aspetta_porta(porta)
    yield porta
    proc.terminate()
    proc.wait(5)


@pytest.fixture
def nodo_finto_senza_privato():
    porta = porta_libera()
    proc = subprocess.Popen([sys.executable, os.path.join(QUI, "fake_node.py"), "--porta", str(porta), "--senza-privato",
                             "--primo-scenario", "99"], env=AMBIENTE, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    aspetta_porta(porta)
    yield porta
    proc.terminate()
    proc.wait(5)


def avvia_ponte(conf):
    return subprocess.Popen([sys.executable, PONTE, "-c", str(conf)], env=AMBIENTE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def per_uid(eventi):
    d = {}
    for e in eventi:
        d.setdefault(e["uid"], []).append(e)
    return d


def test_scenario_completo(pki, tmp_path, tak_finto, nodo_finto):
    tak = tak_finto()
    conf = scrivi_config(tmp_path / "m.conf", pki, tak.porta, nodo_finto, f"{SDK}/dictionaries")
    ponte = avvia_ponte(conf)
    time.sleep(11)
    ponte.send_signal(signal.SIGTERM)
    registro, _ = ponte.communicate(timeout=20)
    assert ponte.returncode == 0, registro
    assert "Arresto in corso" in registro
    assert "Traceback" not in registro, registro

    righe = [r for r in tak.righe() if "rifiutata" not in r]
    assert righe[0] == {"conn": 1, "aperta": True, "cn": "meshtastic-cnsas"}
    ev = per_uid(tak.eventi())

    # Inoltrati
    ponte_ev = ev["!abcd0001"][0]
    assert ponte_ev["type"] == "a-f-G-I" and ponte_ev["callsign"] == "Ponte-CNSAS" and "squadra" not in ponte_ev
    assert ponte_ev["mesh2tak"] == {"radio": "!abcd0001", "ponte": "!abcd0001"}, "segno per gli altri ponti"
    assert ponte_ev["icona"] == "6d781afb-89a6-4c07-b2b9-a89748b6a38f/Landmarks/tower.png", "traliccio OSM"
    a = ev["!1111aaaa"]
    assert a[0]["type"] == "a-f-G-U-C" and a[0]["squadra"] == "Cyan" and a[0]["batteria"] == "87"
    assert a[0]["hae"] == pytest.approx(1050 + 47)
    assert "icona" not in a[0], "le radio della squadra restano con il simbolo del tipo"
    mosso = [e for e in a if abs(e["lat"] - 45.8215) < 1e-6]
    assert mosso, a
    assert float(mosso[0]["track"]["speed"]) == pytest.approx(4 / 3.6, abs=0.01)
    assert float(mosso[0]["track"]["course"]) == pytest.approx(90.0)
    assert any(e["batteria"] == "80" and e["conn"] == 1 for e in a), "telemetria non ha aggiornato la batteria"
    assert ev["!4444dddd"][0]["type"] == "a-f-G-I"
    nomi_f = [e["callsign"] for e in ev["!6666ffff"]]
    assert nomi_f[0] == "Meshtastic ffff" and "12-ROSSI" in nomi_f
    g = ev["!7777aaaa"][0]
    assert g["callsign"] == "15-BIANCHI" and g["squadra"] == "Cyan" and g["batteria"] == "64"
    assert g.get("endpoint") is None
    atak = ev["ANDROID-0000000000000002"][0]
    assert atak["mesh2tak"] == {"radio": "!8888bbbb", "ponte": "!abcd0001"}
    assert atak["callsign"] == "TESTNODE-01" and atak["endpoint"] == "*:-1:stcp"
    assert atak["takv"]["platform"] == "ATAK-CIV" and atak["droid"] == "TESTNODE-01"
    assert atak["lat"] == pytest.approx(33.129) and atak["lon"] == pytest.approx(-107.2534)
    vecchio = ev["ANDROID-0000000000000009"][0]
    assert vecchio["callsign"] == "VECCHIO-ATAK" and vecchio["squadra"] == "Yellow" and vecchio["ruolo"] == "Team Lead"

    # Scartati
    for uid in ("!2222bbbb", "!3333cccc", "!5555eeee", "!8888bbbb"):
        assert uid not in ev, f"{uid} non doveva arrivare"
    # La chat ATAK arrivata via radio va al server come chat, con il segno del ponte
    altri = [e for e in tak.eventi() if not e["type"].startswith("a-")]
    assert [e["type"] for e in altri] == ["b-t-f"], altri
    assert altri[0]["note"] == "Roger that, moving to rally point"
    assert altri[0]["chat"]["senderCallsign"] == "TESTNODE-01"
    assert (altri[0]["mesh2tak"]["radio"], altri[0]["mesh2tak"]["ponte"]) == ("!8888bbbb", "!abcd0001")

    # Registro comprensibile
    assert "Ignoro Nodo-Pubblico (!2222bbbb): la sua posizione arriva sul canale 'LongFast'" in registro
    assert "Ignoro Sfocato (!5555eeee): posizione volutamente approssimata" in registro
    assert "pacchetto TAK non decodificabile" in registro
    assert "riprovo tra 2 s" in registro
    assert "posizioni vuote" not in registro, "una richiesta di posizione non e' una posizione vuota"
    assert registro.count("Collegato al nodo Meshtastic") == 2

    # Validita': scadenza 90 minuti dopo l'ultimo segnale ricevuto da quella radio
    from datetime import datetime

    def minuti_alla_scadenza(e):
        return (datetime.fromisoformat(e["stale"].replace("Z", "+00:00")).timestamp() - e["ricevuto"]) / 60
    assert 79 < minuti_alla_scadenza(a[0]) < 81      # dal database: sentita 10 minuti fa
    assert 89 < minuti_alla_scadenza(mosso[0]) < 91  # pacchetto appena ricevuto


def test_prova_tutto_ok(pki, tmp_path, tak_finto, nodo_finto):
    tak = tak_finto()
    conf = scrivi_config(tmp_path / "m.conf", pki, tak.porta, nodo_finto, f"{SDK}/dictionaries")
    r = subprocess.run([sys.executable, PONTE, "-c", str(conf), "--prova"], env=AMBIENTE,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ESITO nodo=ok privati=1 inoltrati=1 recenti=3" in r.stdout, r.stdout
    assert "ESITO tak=ok" in r.stdout
    assert "canale 0 'LongFast': pubblico -> ignorato" in r.stdout
    assert tak.eventi() == [], "la prova non deve mandare posizioni"


def test_prova_nodo_assente_e_tak_spento(pki, tmp_path):
    conf = scrivi_config(tmp_path / "m.conf", pki, porta_libera(), porta_libera(), f"{SDK}/dictionaries")
    r = subprocess.run([sys.executable, PONTE, "-c", str(conf), "--prova"], env=AMBIENTE,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 3
    assert "ESITO nodo=errore" in r.stdout and "meshtasticd e' avviato?" in r.stdout
    assert "ESITO tak=errore" in r.stdout and "rifiuta la connessione" in r.stdout


def test_configurazione_errata_esce_con_codice_78(tmp_path):
    p = tmp_path / "rotta.conf"
    p.write_text("[tak]\nporta = 8089\n")
    r = subprocess.run([sys.executable, PONTE, "-c", str(p)], env=AMBIENTE, capture_output=True,
                       text=True, timeout=30)
    assert r.returncode == 78 and "manca 'host'" in r.stdout


def _cfg(pki, tmp_path, porta, **extra):
    return m.carica_config(str(scrivi_config(tmp_path / "t.conf", pki, porta, 4403,
                                             f"{SDK}/dictionaries", **extra)))


def test_tak_certificato_estraneo_rifiutato(pki, tmp_path, tak_finto):
    tak = tak_finto()
    cfg = _cfg(pki, tmp_path, tak.porta, tak__certificato=pki / "rogue.pem", tak__chiave=pki / "rogue.key")
    ok, msg = m.prova_tak(cfg)
    assert not ok and "rifiutato il certificato" in msg, msg


def test_tak_server_con_ca_diversa(pki, tmp_path, tak_finto):
    tak = tak_finto()
    cfg = _cfg(pki, tmp_path, tak.porta, tak__ca_server=pki / "other-ca.pem")
    ok, msg = m.prova_tak(cfg)
    assert not ok and "non corrisponde alla CA salvata" in msg, msg


def test_tak_chiave_cifrata_senza_password(pki, tmp_path, tak_finto):
    tak = tak_finto()
    with pytest.raises(Exception):
        # la chiave del pacchetto e' cifrata: l'installatore la decifra, il ponte no
        m.contesto_tls(_cfg(pki, tmp_path, tak.porta, tak__chiave=pki / "meshtastic-cnsas.key"))


def test_tak_riconnessione_e_risincronizzazione(pki, tmp_path, tak_finto):
    tak = tak_finto(chiudi_dopo=2)
    cfg = _cfg(pki, tmp_path, tak.porta)
    cliente = m.ClienteTAK(cfg)
    cliente.ATTESA_RETE = (0.3, 1.0)
    cliente.avvia()
    ora = time.time()
    try:
        assert cliente.collegato.wait(5)
        for i in range(3):
            xml = m.cot_evento(uid=f"u{i}", tipo="a-f-G-U-C", how="m-g", lat=45 + i / 100, lon=8.8,
                               t_evento=ora, stale=ora + 600, callsign=f"N{i}")
            cliente.invia(f"u{i}", xml, ora + 600)
            time.sleep(0.4)
        scaduto = m.cot_evento(uid="vecchio", tipo="a-f-G-U-C", how="m-g", lat=45, lon=8.8,
                               t_evento=ora, stale=ora + 2, callsign="V")
        cliente.invia("vecchio", scaduto, ora + 2)
        fine = time.time() + 10
        while time.time() < fine:
            seconda = {e["uid"] for e in tak.eventi() if e["conn"] == 2}
            if {"u0", "u1", "u2"} <= seconda:
                break
            time.sleep(0.2)
    finally:
        cliente.ferma()
    righe = tak.righe()
    assert any(r.get("chiusa_dal_server") for r in righe)
    seconda = [e["uid"] for e in tak.eventi() if e["conn"] == 2]
    assert {"u0", "u1", "u2"} <= set(seconda), righe
    assert seconda.count("u0") == 1, "dopo la riconnessione ogni posizione va mandata una volta sola"


def test_tak_ping_periodico(pki, tmp_path, tak_finto):
    tak = tak_finto()
    cliente = m.ClienteTAK(_cfg(pki, tmp_path, tak.porta))
    cliente.PING_S = 0.5
    cliente.avvia()
    try:
        time.sleep(2.5)
    finally:
        cliente.ferma()
    assert sum(1 for r in tak.righe() if r.get("type") == "t-x-c-t") >= 2


def test_chiave_rovinata_esce_con_codice_78(pki, tmp_path):
    rotta = tmp_path / "rotta.key"
    rotta.write_text("-----BEGIN PRIVATE KEY-----\nnon-e-una-chiave\n-----END PRIVATE KEY-----\n")
    conf = scrivi_config(tmp_path / "m.conf", pki, porta_libera(), porta_libera(), f"{SDK}/dictionaries",
                         tak__chiave=rotta)
    r = subprocess.run([sys.executable, PONTE, "-c", str(conf)], env=AMBIENTE, capture_output=True,
                       text=True, timeout=30)
    assert r.returncode == 78 and "non leggibili" in r.stdout, r.stdout + r.stderr


@pytest.fixture
def nodo_nuovo(tmp_path):
    porta = porta_libera()
    stato = tmp_path / "nodo.json"
    proc = subprocess.Popen([sys.executable, os.path.join(QUI, "fake_node.py"), "--porta", str(porta), "--nuovo",
                             "--primo-scenario", "99", "--stato", str(stato)],
                            env=AMBIENTE, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    aspetta_porta(porta)
    yield porta, stato
    proc.terminate()
    proc.wait(5)


def _configura(porta, tmp_path, *extra):
    link = tmp_path / "link.txt"
    r = subprocess.run([sys.executable, PONTE, "--configura-nodo", f"tcp:127.0.0.1:{porta}", "--ruolo", "CLIENT_MUTE",
                        "--file-link", str(link), *extra], env=AMBIENTE, capture_output=True, text=True, timeout=240)
    return r, link


def test_configura_nodo_appena_installato(nodo_nuovo, tmp_path):
    import base64
    import json
    from meshtastic.protobuf import apponly_pb2, config_pb2
    porta, stato = nodo_nuovo
    r, link = _configura(porta, tmp_path, "--posizione", "45.8205, 8.8257, 400")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ESITO configurazione=ok" in r.stdout
    s = json.loads(stato.read_text())
    assert (s["regione"], s["ruolo"], s["nome"], s["nome_breve"]) == ("EU_868", "CLIENT_MUTE", "Gateway CNSAS Varese",
                                                                         "GTSA")
    assert {k: v for k, v in s["canali"][0].items() if k != "psk"} == {
        "indice": 0, "ruolo": "PRIMARY", "nome": "CNSAS", "psk_len": 32, "precisione": 32}
    assert [c["ruolo"] for c in s["canali"][1:]] == ["DISABLED"] * 7, "dalla 1.12.0 nessun canale secondario"
    assert "nessun canale secondario" in r.stdout
    assert (s["posizione_fissa"], s["lat"], s["lon"], s["alt"]) == (True, 458205000, 88257000, 400)
    assert os.stat(link).st_mode & 0o777 == 0o600
    url = link.read_text().strip()
    corpo = url.split("#", 1)[1].replace("-", "+").replace("_", "/")
    cs = apponly_pb2.ChannelSet.FromString(base64.b64decode(corpo + "=" * (-len(corpo) % 4)))
    assert [c.name for c in cs.settings] == ["CNSAS"], "nel QR solo il canale della squadra"
    assert cs.lora_config.region == config_pb2.Config.LoRaConfig.RegionCode.EU_868
    # rilancio: niente da cambiare, la chiave resta la stessa
    r2, link2 = _configura(porta, tmp_path, "--posizione", "45.8205,8.8257,400")
    assert r2.returncode == 0 and "era gia' tutto a posto" in r2.stdout, r2.stdout
    assert link2.read_text() == link.read_text()
    assert json.loads(stato.read_text())["riavvii"] == s["riavvii"]


def test_configura_nodo_canale_della_squadra_dato(nodo_nuovo, tmp_path):
    """Il canale della squadra viene da fuori (es. ICARO): nome e chiave dati, l'unico del nodo."""
    import base64
    import json
    from meshtastic.protobuf import apponly_pb2
    porta, stato = nodo_nuovo
    r, link = _configura(porta, tmp_path, "--posizione", "45.8205, 8.8257, 400")   # prima: CNSAS, chiave a caso
    assert r.returncode == 0, r.stdout
    chiave = bytes(range(100, 132))
    file_chiave = tmp_path / "chiave.txt"
    file_chiave.write_text("base64:" + base64.b64encode(chiave).decode() + "\n")
    r, link = _configura(porta, tmp_path, "--posizione", "45.8205, 8.8257, 400", "--canale", "CNSAS-VA",
                         "--chiave-canale-file", str(file_chiave))
    assert r.returncode == 0 and "ESITO configurazione=ok" in r.stdout, r.stdout
    assert "canale della squadra 'CNSAS-VA' con la chiave data" in r.stdout
    assert base64.b64encode(chiave).decode() not in r.stdout, "la chiave non finisce nel registro"
    assert "nessun canale secondario" in r.stdout
    s = json.loads(stato.read_text())
    assert (s["canali"][0]["ruolo"], s["canali"][0]["nome"], s["canali"][0]["psk"], s["canali"][0]["precisione"]) == \
        ("PRIMARY", "CNSAS-VA", base64.b64encode(chiave).decode(), 32)
    assert s["canali"][1]["ruolo"] == "DISABLED"
    url = link.read_text().strip()
    corpo = url.split("#", 1)[1].replace("-", "+").replace("_", "/")
    cs = apponly_pb2.ChannelSet.FromString(base64.b64decode(corpo + "=" * (-len(corpo) % 4)))
    assert [(c.name, bytes(c.psk)) for c in cs.settings] == [("CNSAS-VA", chiave)]
    # rilancio senza chiave: il canale dato resta com'e'
    r2, _ = _configura(porta, tmp_path, "--posizione", "45.8205,8.8257,400", "--canale", "CNSAS-VA")
    assert r2.returncode == 0 and "era gia' tutto a posto" in r2.stdout, r2.stdout
    assert json.loads(stato.read_text())["canali"][0]["psk"] == base64.b64encode(chiave).decode()


@pytest.fixture
def nodo_1_11(tmp_path):
    """Nodo configurato dalla 1.11.0: squadra CNSAS-VA primaria, LongFast pubblico secondario."""
    porta = porta_libera()
    stato = tmp_path / "nodo.json"
    proc = subprocess.Popen([sys.executable, os.path.join(QUI, "fake_node.py"), "--porta", str(porta), "--squadra-primaria",
                             "--primo-scenario", "99", "--stato", str(stato)],
                            env=AMBIENTE, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    aspetta_porta(porta)
    yield porta, stato
    proc.terminate()
    proc.wait(5)


def test_configura_nodo_spegne_il_canale_pubblico(nodo_1_11, tmp_path):
    """Radio preparata come all'installazione (Reimposta la radio, o un ponte della 1.11.0 che si aggiorna): il LongFast
    secondario si spegne, la squadra resta."""
    import base64
    import json
    from meshtastic.protobuf import apponly_pb2
    porta, stato = nodo_1_11
    r, link = _configura(porta, tmp_path, "--canale", "CNSAS-VA")
    assert r.returncode == 0 and "ESITO configurazione=ok" in r.stdout, r.stdout
    assert "canale secondario 'LongFast' spento" in r.stdout and "nessun canale secondario" in r.stdout
    assert "canale della squadra" not in r.stdout                  # la squadra non si tocca
    s = json.loads(stato.read_text())
    assert (s["canali"][0]["ruolo"], s["canali"][0]["nome"], s["canali"][0]["psk_len"]) == ("PRIMARY", "CNSAS-VA", 32)
    assert [c["ruolo"] for c in s["canali"][1:]] == ["DISABLED"] * 7
    corpo = link.read_text().strip().split("#", 1)[1].replace("-", "+").replace("_", "/")
    cs = apponly_pb2.ChannelSet.FromString(base64.b64decode(corpo + "=" * (-len(corpo) % 4)))
    assert [c.name for c in cs.settings] == ["CNSAS-VA"]
    r, _ = _configura(porta, tmp_path, "--canale", "CNSAS-VA")
    assert r.returncode == 0 and "era gia' tutto a posto" in r.stdout, r.stdout


def test_configura_nodo_solo_le_parti_chieste(nodo_1_11, tmp_path):
    """1.13.0: un aggiornamento non cambia la radio (--parti nessuna); una parte alla volta solo se chiesta."""
    import json
    porta, stato = nodo_1_11
    r, link = _configura(porta, tmp_path, "--canale", "CNSAS-VA", "--parti", "nessuna", "--posizione", "45.9,8.9")
    assert r.returncode == 0 and "ESITO configurazione=ok" in r.stdout, r.stdout
    assert "impostazioni della radio lasciate come sono" in r.stdout and "era gia'" not in r.stdout
    assert "canali secondari: LongFast" in r.stdout
    assert not stato.exists(), "nessun messaggio di configurazione al nodo (il nodo finto scrive lo stato a ognuno)"
    assert link.read_text().startswith("https://meshtastic.org/e/#"), "il link del canale si aggiorna sempre"
    r, _ = _configura(porta, tmp_path, "--canale", "CNSAS-VA", "--parti", "nome,secondari", "--nome", "Gateway CNSAS Prova",
                      "--ruolo", "ROUTER")
    assert r.returncode == 0 and "modifiche: nome 'Gateway CNSAS Prova' (GTSA), canale secondario 'LongFast' spento" in r.stdout, r.stdout
    dopo = json.loads(stato.read_text())
    assert (dopo["nome"], dopo["canali"][1]["ruolo"], dopo["ruolo"], dopo["posizione_fissa"]) == (
        "Gateway CNSAS Prova", "DISABLED", "CLIENT_MUTE", False), "ruolo e posizione non chiesti: restano"
    r, _ = _configura(porta, tmp_path, "--parti", "wifi")
    assert r.returncode == 2 and "parti sconosciute: wifi" in r.stdout


def test_configura_nodo_radio_nuova_sempre_tutta(nodo_nuovo, tmp_path):
    """Radio nuova (regione non impostata, es. cambiata la radio USB): si prepara tutta anche in un aggiornamento."""
    import json
    porta, stato = nodo_nuovo
    r, _ = _configura(porta, tmp_path, "--parti", "nessuna")
    assert r.returncode == 0 and "radio nuova (regione non impostata): la preparo tutta" in r.stdout, r.stdout
    s = json.loads(stato.read_text())
    assert (s["regione"], s["ruolo"], s["nome"], s["canali"][0]["psk_len"]) == ("EU_868", "CLIENT_MUTE",
                                                                              "Gateway CNSAS Varese", 32)


def test_configura_nodo_senza_canale_privato_lo_dice(nodo_finto_senza_privato, tmp_path):
    r, _ = _configura(nodo_finto_senza_privato, tmp_path, "--parti", "nessuna")
    assert r.returncode == 0 and "ATTENZIONE: il nodo non ha piu' il canale privato della squadra" in r.stdout, r.stdout


def test_riavvia_nodo(nodo_1_11):
    import json
    porta, stato = nodo_1_11
    r = subprocess.run([sys.executable, PONTE, "--riavvia-nodo", f"tcp:127.0.0.1:{porta}"], env=AMBIENTE,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "ESITO riavvio=ok" in r.stdout, r.stdout + r.stderr
    fine = time.time() + 10
    while time.time() < fine and json.loads(stato.read_text()).get("riavvii", 0) < 1:
        time.sleep(0.2)
    s = json.loads(stato.read_text())
    assert s["riavvii_chiesti"] == 1 and s["riavvii"] == 1
    r = subprocess.run([sys.executable, PONTE, "--riavvia-nodo", f"tcp:127.0.0.1:{porta_libera()}"], env=AMBIENTE,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 1 and "ESITO riavvio=errore" in r.stdout


def test_il_ponte_lascia_i_canali_scelti_dall_app(pki, tmp_path, tak_finto, nodo_1_11):
    """1.13.0 (11/11/2026: vale l'interfaccia): il LongFast acceso dall'app resta acceso, il ponte inoltra
    solo il canale della squadra."""
    porta, stato = nodo_1_11
    tak = tak_finto()
    ponte = avvia_ponte(scrivi_config(tmp_path / "m.conf", pki, tak.porta, porta, f"{SDK}/dictionaries"))
    time.sleep(6)
    ponte.send_signal(signal.SIGTERM)
    registro, _ = ponte.communicate(timeout=20)
    assert "canale 0 'CNSAS-VA' (privato): INOLTRATO" in registro, registro
    assert "canale 1 'LongFast' (pubblico, chiave nota a tutti): ignorato" in registro
    assert "spent" not in registro and "Traceback" not in registro
    assert not stato.exists(), "nessun messaggio di configurazione al nodo (il nodo finto scrive lo stato a ognuno)"


def test_configura_nodo_squadra_secondaria_non_si_perde(nodo_finto, tmp_path):
    """Nodo messo a mano con la squadra come secondario (primario pubblico): senza chiave la squadra resta."""
    r, _ = _configura(nodo_finto, tmp_path, "--canale", "CNSAS")
    assert r.returncode == 0 and "ESITO configurazione=ok" in r.stdout, r.stdout
    assert "spento" not in r.stdout and "canali secondari: CNSAS" in r.stdout


@pytest.mark.parametrize("contenuto", ["non base64!", "AQ==", "QUJD"])
def test_configura_nodo_chiave_canale_errata(tmp_path, contenuto):
    f = tmp_path / "chiave.txt"
    f.write_text(contenuto)
    r, _ = _configura(porta_libera(), tmp_path, "--chiave-canale-file", str(f))
    assert r.returncode == 2 and "chiave del canale non valida" in r.stdout


@pytest.mark.parametrize("extra,messaggio", [(["--canale", "CNSAS-VARESE-1"], "11 caratteri"),
                                             (["--posizione", "abc"], "posizione non valida")])
def test_configura_nodo_valori_errati(tmp_path, extra, messaggio):
    r, _ = _configura(porta_libera(), tmp_path, *extra)
    assert r.returncode == 2 and messaggio in r.stdout


def test_configura_nodo_spento(tmp_path):
    r = subprocess.run([sys.executable, PONTE, "--configura-nodo", f"tcp:127.0.0.1:{porta_libera()}"],
                       env=dict(AMBIENTE), capture_output=True, text=True, timeout=240)
    assert r.returncode == 1 and "ESITO configurazione=errore" in r.stdout


# ---- radio Meshtastic collegata via USB (porta seriale simulata con uno pseudo-terminale) ----

@pytest.fixture
def radio_usb(tmp_path):
    def crea(*opzioni):
        percorso = tmp_path / "radio-usb"
        stato = tmp_path / "radio.json"
        proc = subprocess.Popen([sys.executable, os.path.join(QUI, "fake_node.py"), "--usb", str(percorso),
                                 "--stato", str(stato), *opzioni],
                                env=AMBIENTE, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        fine = time.time() + 10
        while not percorso.exists() and time.time() < fine:
            time.sleep(0.1)
        creati.append(proc)
        return percorso, stato
    creati = []
    yield crea
    for p in creati:
        p.terminate()
        p.wait(5)


def test_configura_radio_usb_nuova(radio_usb, tmp_path):
    import json
    percorso, stato = radio_usb("--nuovo", "--primo-scenario", "99")
    link = tmp_path / "link.txt"
    r = subprocess.run([sys.executable, PONTE, "--configura-nodo", f"serial:{percorso}", "--ruolo", "CLIENT",
                        "--posizione", "45.8205,8.8257,400", "--file-link", str(link)],
                       env=AMBIENTE, capture_output=True, text=True, timeout=240)
    assert r.returncode == 0 and "ESITO configurazione=ok" in r.stdout, r.stdout + r.stderr
    s = json.loads(stato.read_text())
    assert (s["regione"], s["ruolo"], s["nome"]) == ("EU_868", "CLIENT", "Gateway CNSAS Varese")
    assert s["canali"][0]["nome"] == "CNSAS" and s["canali"][0]["psk_len"] == 32
    assert link.read_text().startswith("https://meshtastic.org/e/#")


def test_ponte_con_radio_usb(pki, tmp_path, tak_finto, radio_usb):
    percorso, _ = radio_usb()
    tak = tak_finto()
    conf = scrivi_config(tmp_path / "m.conf", pki, tak.porta, 4403, f"{SDK}/dictionaries",
                         meshtastic__collegamento=f"serial:{percorso}")
    ponte = avvia_ponte(conf)
    time.sleep(9)
    ponte.send_signal(signal.SIGTERM)
    registro, _ = ponte.communicate(timeout=20)
    assert ponte.returncode == 0 and "Traceback" not in registro, registro
    assert f"radio USB {percorso}" in registro
    ev = per_uid(tak.eventi())
    assert any(abs(e["lat"] - 45.8215) < 1e-6 for e in ev.get("!1111aaaa", [])), registro
    assert "ANDROID-0000000000000002" in ev, "posizione ATAK compressa non arrivata via USB"
    assert "!2222bbbb" not in ev


def test_radio_usb_assente(tmp_path):
    r = subprocess.run([sys.executable, PONTE, "--configura-nodo", f"serial:{tmp_path / 'non-ce'}"],
                       env=AMBIENTE, capture_output=True, text=True, timeout=240)
    assert r.returncode == 1 and "radio non trovata" in r.stdout and "cavo USB dati" in r.stdout, r.stdout
