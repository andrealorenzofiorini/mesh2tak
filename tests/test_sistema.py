"""Come sta il Raspberry (1.7.0): accensione, corrente insufficiente, spazio, protezioni, microSD di scorta e
scadenza del certificato, per la pagina della rete e gli avvisi. Il Raspberry e' una cartella finta."""
import json
import os
import ssl
import time

import mesh2tak as m


def raspberry(tmp_path, allarme="0", watchdog_state=None, protezioni=("watchdog", "aggiornamenti", "registri"),
              scorta=None):
    r = tmp_path / "pi"
    (r / "proc/sys/kernel/random").mkdir(parents=True)
    (r / "proc/sys/kernel/random/boot_id").write_text("1b4e28ba-2fa1-11d2-883f-0016d3cca427\n")
    (r / "proc/uptime").write_text("3600.25 7000.10\n")
    if allarme is not None:
        h = r / "sys/class/hwmon"
        (h / "hwmon0").mkdir(parents=True)
        (h / "hwmon0/name").write_text("cpu_thermal\n")
        (h / "hwmon1").mkdir()
        (h / "hwmon1/name").write_text("rpi_volt\n")
        (h / "hwmon1/in0_lcrit_alarm").write_text(allarme + "\n")
    if watchdog_state is not None:
        (r / "sys/class/watchdog/watchdog0").mkdir(parents=True)
        (r / "sys/class/watchdog/watchdog0/state").write_text(watchdog_state + "\n")
    for k in protezioni:
        f = r / m.Sistema.PROTEZIONI[k].lstrip("/")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("# mesh2tak\n")
    if scorta is not None:
        f = r / m.Sistema.SCORTA.lstrip("/")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(scorta)
    return r


def test_stato_completo(tmp_path):
    r = raspberry(tmp_path, scorta=json.dumps({"quando": 1762437000, "disco": "Generic SD/MMC 31.9 GB"}))
    s = m.Sistema(str(r))
    st = s.stato()
    assert st["avvio"] == "1b4e28ba-2fa1-11d2-883f-0016d3cca427"
    assert abs(st["acceso_dal"] - (time.time() - 3600)) < 5
    assert st["alimentazione"] == {"bassa_ora": False, "ultima": None, "volte": 0, "dal": round(s.inizio)}
    assert st["disco"]["totale_mb"] > 0 and 0 <= st["disco"]["libero_mb"] <= st["disco"]["totale_mb"]
    assert st["protezioni"] == {"watchdog": True, "aggiornamenti": True, "registri": True}
    assert st["scorta"] == {"quando": 1762437000, "disco": "Generic SD/MMC 31.9 GB"}
    json.dumps(st)


def test_raspberry_senza_niente(tmp_path):
    """Un computer qualsiasi (o un Raspberry vecchio): niente corrente misurabile, niente protezioni, niente scorta."""
    r = tmp_path / "vuoto"
    r.mkdir()
    st = m.Sistema(str(r)).stato()
    assert st["avvio"] is None and st["acceso_dal"] is None
    assert st["alimentazione"] is None
    assert st["protezioni"] == {"watchdog": False, "aggiornamenti": False, "registri": False}
    assert st["scorta"] is None


def test_watchdog_vero_vince_sul_file(tmp_path):
    """Il file di configurazione c'e', ma conta quello che dice il chip."""
    assert m.Sistema(str(raspberry(tmp_path, watchdog_state="inactive"))).stato()["protezioni"]["watchdog"] is False
    r2 = tmp_path / "b"
    r2.mkdir()
    assert m.Sistema(str(raspberry(r2, watchdog_state="active", protezioni=()))).stato()["protezioni"]["watchdog"]


def test_cali_di_corrente_contati_anche_brevi(tmp_path, caplog):
    r = raspberry(tmp_path)
    s = m.Sistema(str(r))
    allarme = r / "sys/class/hwmon/hwmon1/in0_lcrit_alarm"
    t0 = 1_000_000.0
    for i, v in enumerate("0110001"):        # due cali: secondi 1-2 e 6
        allarme.write_text(v + "\n")
        s.controlla_alimentazione(t0 + i)
    st = s.stato()["alimentazione"]
    assert st["volte"] == 2 and st["ultima"] == round(t0 + 6) and st["bassa_ora"] is True
    allarme.write_text("0\n")
    s.controlla_alimentazione(t0 + 7)
    assert s.stato()["alimentazione"]["bassa_ora"] is False
    # nel registro una riga sola: il secondo calo arriva meno di 10 minuti dopo il primo
    assert sum("Alimentazione insufficiente" in x.getMessage() for x in caplog.records) == 1


def test_scorta_rovinata_ignorata(tmp_path):
    for i, testo in enumerate(("non json", '{"quando": "ieri"}', '{"quando": true}', "[1]")):
        d = tmp_path / str(i)
        d.mkdir()
        assert m.Sistema(str(raspberry(d, scorta=testo))).stato()["scorta"] is None


def test_scadenza_del_certificato(pki):
    c = m.certificato_tak(str(pki / "meshtastic-cnsas.pem"))
    assert c is not None and c["nome"] == "meshtastic-cnsas"
    vero = ssl._ssl._test_decode_cert(str(pki / "meshtastic-cnsas.pem"))["notAfter"]
    assert c["scade"] == int(ssl.cert_time_to_seconds(vero)) and c["scade"] > time.time()


def test_scadenza_con_openssl_se_python_non_sa_leggerlo(pki, tmp_path, monkeypatch):
    copia = tmp_path / "client.pem"
    copia.write_bytes((pki / "meshtastic-cnsas.pem").read_bytes())

    def rotto(_):
        raise AttributeError("niente lettore interno")
    monkeypatch.setattr(ssl._ssl, "_test_decode_cert", rotto)
    c = m.certificato_tak(str(copia))
    assert c and c["scade"] > time.time() and c["nome"] == ""


def test_certificato_mancante_o_rovinato(tmp_path):
    assert m.certificato_tak(str(tmp_path / "manca.pem")) is None
    rotto = tmp_path / "rotto.pem"
    rotto.write_text("-----BEGIN CERTIFICATE-----\nnon-e-un-certificato\n-----END CERTIFICATE-----\n")
    assert m.certificato_tak(str(rotto)) is None


def test_certificato_riletto_quando_cambia(pki, tmp_path):
    f = tmp_path / "client.pem"
    f.write_bytes((pki / "meshtastic-cnsas.pem").read_bytes())
    prima = m.certificato_tak(str(f))
    f.write_bytes((pki / "takserver.pem").read_bytes())
    os.utime(f, (time.time() + 5, time.time() + 5))
    dopo = m.certificato_tak(str(f))
    assert prima["nome"] == "meshtastic-cnsas" and dopo["nome"] != "meshtastic-cnsas"
