"""1.13.0, "Gestisci il Raspberry", lato server: il servizio della pagina controlla e accoda i comandi per un ponte;
il passo dei lavori (root) li scrive nella registrazione del ponte (senza chi li ha chiesti) e lo avvisa dal tunnel; il
certificato nuovo resta per il solo utente SSH di quel ponte, che lo scarica con il comando forzato."""
import base64
import json
import os
import stat
import subprocess
import sys
import time

import pytest

from conftest import RADICE
from http_finto import ServerFinto, crea_certificati
from test_rete import Server as ServerRete, chiave, dns_di_prova, esito_di, lavoro_in_coda, pytestmark  # noqa: F401

sys.path.insert(0, os.path.join(RADICE, "server"))
import rete_meshtastic as rete  # noqa: E402

PACCHETTO = base64.b64encode(b"PK\x03\x04 pacchetto di prova").decode()


# ---------------------------------------------------------------- servizio della pagina (senza privilegi)

@pytest.fixture
def sv(tmp_path, monkeypatch):
    monkeypatch.setattr(rete, "RADICE", str(tmp_path / "r"))
    for d in (rete.SITO_DIR, rete.ACCESSO_DIR, rete.RICHIESTE_DIR, rete.LAVORI_DIR, rete.TMP_DIR, rete.ESITI_DIR):
        os.makedirs(rete.P(d))
    with open(rete.P(rete.SITO_DIR + "/ponti.json"), "w") as f:
        json.dump({"formato": 1, "ponti": [{"nome": "casa", "indirizzo": "https://meshtastic-casa.prova.lan/"}]}, f)
    return rete.Servizio()


def in_coda():
    cartella = rete.P(rete.LAVORI_DIR)
    return [json.load(open(os.path.join(cartella, f))) for f in sorted(os.listdir(cartella))]


def test_comandi_per_un_ponte_controllati(sv):
    assert sv.azione("/api/gestisci-ponte", {"nome": "sede", "azione": "registro"}, "andrea")[0] == 404
    assert sv.azione("/api/gestisci-ponte", {"nome": "casa", "azione": "rm -rf /"}, "andrea")[0] == 400
    assert sv.azione("/api/gestisci-ponte", {"nome": "casa", "azione": "certificato"}, "andrea")[0] == 400
    for valori, errore in (({"tempo_radio_percento": 20}, "il tempo radio (da 0.5 a 10)"),
                           ({"squadra": "Rosa"}, "il colore della squadra"), ({}, "nessuna impostazione"),
                           ({"collegamento": "tcp:1.2.3.4:4403"}, "sconosciuta"), ({"validita_min": "90"}, "minuti"),
                           ({"includi_ponte": 1}, "il Raspberry sulla mappa"), ({"validita_min": float("nan")}, "minuti")):
        codice, r = sv.azione("/api/gestisci-ponte", {"nome": "casa", "azione": "impostazioni", "valori": valori}, "andrea")
        assert codice == 400 and errore in r["errore"], (valori, r)
    assert in_coda() == []
    codice, r = sv.azione("/api/gestisci-ponte", {"nome": "casa", "azione": "impostazioni",
                                                  "valori": {"squadra": "Dark Blue", "invio": False, "validita_min": 120}},
                          "andrea")
    assert codice == 202 and r["messaggio"] == "Mando al ponte le impostazioni nuove."
    [lv] = in_coda()
    assert (lv["id"], lv["azione"], lv["nome"], lv["cosa"], lv["da"]) == (r["lavoro"], "azione-ponte", "casa",
                                                                         "impostazioni", "andrea")
    assert lv["valori"] == {"squadra": "Dark Blue", "invio": False, "validita_min": 120}
    codice, r = sv.azione("/api/gestisci-ponte", {"nome": "casa", "azione": "riavvia-raspberry"}, "andrea")
    assert codice == 409 and "aspetta qualche secondo" in r["errore"], "un comando alla volta per ponte"
    assert sv.stato("andrea")["lavori"][0]["cosa"] == "impostazioni"


def test_certificato_di_un_ponte_controllato(sv):
    buono = {"nome": "casa", "nome_pacchetto": "ponte-casa.zip", "pacchetto": PACCHETTO, "password": "segreta"}
    for cattivo in (dict(buono, nome="sede"), dict(buono, nome_pacchetto="ponte.exe"), dict(buono, pacchetto="!!"),
                    dict(buono, pacchetto=base64.b64encode(b"x" * (300 << 10)).decode()),
                    dict(buono, password="a\nb"), dict(buono, host="tak server")):
        assert sv.azione("/api/certificato-ponte", cattivo, "andrea")[0] in (400, 404), cattivo
    assert in_coda() == []
    codice, r = sv.azione("/api/certificato-ponte", dict(buono, nome_pacchetto="C:\\Download\\ponte-casa.zip"), "andrea")
    assert codice == 202 and "lo installa da solo" in r["messaggio"]
    [lv] = in_coda()
    assert (lv["azione"], lv["nome"]) == ("certificato-ponte", "casa")
    assert lv["impostazioni"] == {"nome_pacchetto": "ponte-casa.zip", "pacchetto": PACCHETTO, "password": "segreta",
                                  "host": ""}
    assert "segreta" not in json.dumps(sv.stato("andrea")), "la password non torna alla pagina"


def test_lavori_dei_ponti_letti_con_sospetto(tmp_path):
    def leggi(**dati):
        f = tmp_path / "l.json"
        f.write_text(json.dumps(dict({"id": "a1b2c3d4e5f60718", "da": "andrea"}, **dati)))
        return rete.leggi_lavoro(str(f))
    lv = leggi(azione="azione-ponte", nome="casa", cosa="riavvia-radio", valori={"squadra": "Red"})
    assert (lv["cosa"], lv["valori"]) == ("riavvia-radio", None), "i valori servono solo alle impostazioni"
    assert leggi(azione="azione-ponte", nome="casa", cosa="impostazioni", valori={"invio": True})["valori"] == {"invio": True}
    cert = {"nome_pacchetto": "a.zip", "pacchetto": PACCHETTO, "password": "", "host": "tak.prova.lan"}
    assert leggi(azione="certificato-ponte", nome="casa", impostazioni=cert)["impostazioni"]["host"] == "tak.prova.lan"
    assert leggi(azione="aggiorna-ponti", nome="casa", versione="1.9.0", reimposta=True)["reimposta"] is True
    assert leggi(azione="aggiorna-ponti", nome="casa", versione="1.9.0", reimposta="si")["reimposta"] is False
    for cattivo in ({"azione": "azione-ponte", "nome": "casa", "cosa": "esegui"},
                    {"azione": "azione-ponte", "nome": "casa", "cosa": "certificato"},
                    {"azione": "azione-ponte", "nome": "../x", "cosa": "registro"},
                    {"azione": "azione-ponte", "nome": "casa", "cosa": "impostazioni", "valori": {"porta": 22}},
                    {"azione": "certificato-ponte", "nome": "casa", "impostazioni": dict(cert, nome_pacchetto="x.sh")},
                    {"azione": "certificato-ponte", "nome": "casa"}):
        assert leggi(**cattivo) is None, cattivo


# ---------------------------------------------------------------- passo dei lavori (root), sul server finto

@pytest.fixture
def srv(tmp_path):
    s = ServerRete(tmp_path / "s")
    (s.radice / "var/log").mkdir(parents=True, exist_ok=True)
    assert s.esegui("installa")[0] == 0
    assert s.esegui("aggiungi", "casa", chiave(1))[0] == 0
    (tmp_path / "ca").mkdir()
    s.ponte = ServerFinto({"POST /mesh2tak/controlla": (200, {"esito": "chiesto"})}, tls=crea_certificati(tmp_path / "ca"))
    registro = json.loads(s.file("/etc/mesh2tak-rete/registro.json"))
    registro["ponti"]["casa"]["porta"] = s.ponte.porta          # il tunnel del ponte finto
    (s.radice / "etc/mesh2tak-rete/registro.json").write_text(json.dumps(registro))
    yield s
    s.ponte.ferma()
    s.ferma()


def registrazione(s):
    [r] = s.registrazioni().values()
    return r


def test_comando_nella_registrazione_e_ponte_avvisato(srv):
    prima = int(time.time())
    lavoro = lavoro_in_coda(srv, azione="azione-ponte", nome="casa", cosa="riavvia-programma")
    esito, uscita = srv.esegui("lavora")
    assert esito == 0, uscita
    e = esito_di(srv, lavoro)
    assert (e["esito"], e["messaggio"], e["cosa"]) == ("ok", "chiesto al ponte: lo fa subito", "riavvia-programma"), e
    [a] = registrazione(srv)["azioni"]
    assert a["id"] == lavoro and a["azione"] == "riavvia-programma" and a["chiesto"] >= prima
    assert "da" not in a and "andrea" not in json.dumps(registrazione(srv)), "la registrazione e' pubblica"
    assert srv.ponte.spedite == [("/mesh2tak/controlla", b"{}")]
    # ponte che non risponde: il comando resta nella registrazione e lo fa quando torna
    srv.ponte.ferma()
    lavoro2 = lavoro_in_coda(srv, id="b1b2c3d4e5f60718", azione="azione-ponte", nome="casa", cosa="impostazioni",
                             valori={"squadra": "Red"})
    assert srv.esegui("lavora")[0] == 0
    assert "non risponde adesso" in esito_di(srv, lavoro2)["messaggio"]
    azioni = registrazione(srv)["azioni"]
    assert [x["id"] for x in azioni] == [lavoro, lavoro2] and azioni[1]["valori"] == {"squadra": "Red"}
    # dopo un'ora un comando non serve piu': esce dalla registrazione (anche quando il server la riscrive)
    registro = json.loads(srv.file("/etc/mesh2tak-rete/registro.json"))
    registro["ponti"]["casa"]["azioni"][0]["chiesto"] -= 4000
    (srv.radice / "etc/mesh2tak-rete/registro.json").write_text(json.dumps(registro))
    assert srv.esegui("installa")[0] == 0
    assert [x["id"] for x in registrazione(srv)["azioni"]] == [lavoro2]


def test_aggiornamento_con_la_radio_nella_registrazione(srv):
    lavoro = lavoro_in_coda(srv, azione="aggiorna-ponti", nome="casa", versione="9.9.9", reimposta=True)
    assert srv.esegui("lavora")[0] == 0
    e = esito_di(srv, lavoro)
    assert e["esito"] == "ok" and e["reimposta"] is True and "anche la radio come all'installazione" in e["testo"]
    assert registrazione(srv)["aggiorna"]["reimposta"] is True
    assert srv.ponte.spedite == [("/mesh2tak/controlla", b"{}")], "il ponte comincia subito"
    lavoro_in_coda(srv, id="b1b2c3d4e5f60718", azione="aggiorna-ponti", nome="casa", versione="9.9.9")
    assert srv.esegui("lavora")[0] == 0
    assert "reimposta" not in registrazione(srv)["aggiorna"]


def comando_ponte(s, comando, utente="ponte-casa"):
    amb = dict(os.environ, RETE_PROVA_RADICE=str(s.radice), RETE_PROVA_UTENTE=utente, SSH_ORIGINAL_COMMAND=comando)
    return subprocess.run([sys.executable, "-I", str(s.radice / "opt/mesh2tak-rete/rete_meshtastic.py"), "ponte"],
                          env=amb, capture_output=True, timeout=60)


def test_certificato_per_il_solo_ponte_e_tolto_dopo(srv, monkeypatch):
    lavoro = lavoro_in_coda(srv, azione="certificato-ponte", nome="casa", impostazioni={
        "nome_pacchetto": "ponte-casa.zip", "pacchetto": PACCHETTO, "password": "", "host": ""})
    esito, uscita = srv.esegui("lavora")
    assert esito == 0, uscita
    e = esito_di(srv, lavoro)
    # il pacchetto finto non si apre: controllato come quello dei ponti nuovi, niente al Raspberry
    assert e["esito"] == "errore" and "non si apre" in e["messaggio"]
    assert not (srv.radice / "etc/mesh2tak-rete/configurazioni/casa.certificato.json").exists()
    assert "azioni" not in registrazione(srv)
    # con un pacchetto buono (quello della console di prova)
    lavoro = lavoro_in_coda(srv, id="b1b2c3d4e5f60718", azione="certificato-ponte", nome="casa",
                            impostazioni={"nome_pacchetto": "ponte-casa.p12", "pacchetto": p12_di_prova(srv),
                                          "password": "atakatak", "host": ""})
    esito, uscita = srv.esegui("lavora")
    e = esito_di(srv, lavoro)
    assert e["esito"] == "ok" and e["messaggio"] == "Certificato 'ponte-casa' pronto per il ponte casa: lo fa subito.", e
    f = srv.radice / "etc/mesh2tak-rete/configurazioni/casa.certificato.json"
    assert stat.S_IMODE(f.stat().st_mode) == 0o400
    dati = json.loads(f.read_text())
    assert (dati["nome_pacchetto"], dati["password"]) == ("ponte-casa.p12", "atakatak")
    [a] = registrazione(srv)["azioni"]
    assert (a["id"], a["azione"]) == (lavoro, "certificato") and "atakatak" not in json.dumps(registrazione(srv))
    # il comando forzato: lo da' solo all'utente di quel ponte, poi lo toglie
    assert comando_ponte(srv, "certificato", "ponte-sede").returncode == 3
    r = comando_ponte(srv, "certificato")
    assert r.returncode == 0 and json.loads(r.stdout)["pacchetto"] == dati["pacchetto"]
    assert comando_ponte(srv, "certificato-fatto").returncode == 0 and not f.exists()
    assert comando_ponte(srv, "certificato").returncode == 3
    assert comando_ponte(srv, "certificato altro").returncode == 2


def p12_di_prova(s):
    d = s.radice.parent / "p12"
    d.mkdir()
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                    "-keyout", str(d / "k.pem"), "-out", str(d / "c.pem"), "-days", "30", "-subj", "/CN=ponte-casa"],
                   check=True, capture_output=True)
    subprocess.run(["openssl", "pkcs12", "-export", "-inkey", str(d / "k.pem"), "-in", str(d / "c.pem"), "-out",
                    str(d / "p.p12"), "-passout", "pass:atakatak"], check=True, capture_output=True)
    return base64.b64encode((d / "p.p12").read_bytes()).decode()


def test_certificati_scaduti_e_ponte_tolto(srv):
    cartella = srv.radice / "etc/mesh2tak-rete/configurazioni"
    cartella.mkdir(parents=True, exist_ok=True)
    vecchio, conf = cartella / "casa.certificato.json", cartella / "sede.json"
    for f in (vecchio, conf):
        f.write_text("{}")
        os.utime(f, (time.time() - 2 * 86400, time.time() - 2 * 86400))
    lavoro_in_coda(srv, azione="azione-ponte", nome="casa", cosa="registro")
    assert srv.esegui("lavora")[0] == 0
    assert not vecchio.exists() and conf.exists(), "il certificato di un ponte dura un giorno, la configurazione 30"
    vecchio.write_text("{}")
    assert srv.esegui("rimuovi", "casa", "--si")[0] == 0
    assert not vecchio.exists()
