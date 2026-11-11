"""Ponti nuovi che si installano da soli (1.10.0), lato server: impostazioni (certificato e canale) salvate da root,
inviti, approvazione con installazione, configurazione letta solo dal suo ponte, comando SSH dei ponti."""
import base64
import hashlib
import json
import os
import stat
import sys
import time
from types import SimpleNamespace

import pytest

from conftest import RADICE

sys.path.insert(0, os.path.join(RADICE, "server"))
import rete_meshtastic as rete  # noqa: E402
from test_servizio_rete import chiave, domanda, esito, impronta, lavori, richiesta_salvata, sv  # noqa: E402,F401

PSK = base64.b64encode(b"k" * 32).decode()
IMP = SimpleNamespace(url=lambda n=None: f"https://meshtastic{'-' + n if n else ''}.prova.lan/",
                      host=lambda n=None: f"meshtastic{'-' + n if n else ''}.prova.lan", dominio="prova.lan")


def pronto(s):
    """Come dopo che root ha salvato certificato e canale, e la pagina ha il suo indirizzo."""
    os.makedirs(rete.P(rete.WEB_DIR), exist_ok=True)
    with open(rete.P(rete.RIASSUNTO_NUOVI), "w") as f:
        json.dump({"formato": 1, "pronto": True, "certificato": "meshtastic-cnsas", "canale": "CNSAS-VA",
                   "secondario": "", "server": "tak.prova.lan:8089", "aggiornato": 1, "da": "andrea"}, f)
    with open(rete.P(rete.SORVEGLIANZA), "w") as f:
        json.dump({"formato": 1, "pagina": "https://meshtastic.prova.lan/", "ponti": []}, f)


def invita(s, nome="velate", scheda="waveshare", posizione="45.8205, 8.8257"):
    codice, r = s.azione("/api/invita", {"nome": nome, "scheda": scheda, "posizione": posizione}, "andrea")
    assert codice == 200, r
    return r["invito"]


def nuovo(n, riga=None, nome="velate", macchina="ponte-velate", **altro):
    inviti = [riga.split()[1]] if riga else []
    return dict(domanda(n, nome, macchina=macchina, versione="1.10.0"),
                nuovo=dict({"modello": "Raspberry Pi 3 Model B Rev 1.2", "radio_usb": False, "inviti": inviti}, **altro))


# ---- inviti ----

def test_invito_solo_con_le_impostazioni_e_una_chiave_che_porta_l_indirizzo(sv):  # noqa: F811
    codice, r = sv.azione("/api/invita", {"nome": "velate", "scheda": "waveshare"}, "andrea")
    assert codice == 409 and "certificato e canale" in r["errore"]
    pronto(sv)
    inv = invita(sv)
    parti = inv["riga"].split()
    blob = base64.b64decode(parti[1])
    assert parti[0] == "ssh-ed25519" and parti[2:] == ["mesh2tak-invito", "https://meshtastic.prova.lan"]
    assert len(blob) == 51 and inv["id"] == hashlib.sha256(blob).hexdigest()
    assert (inv["nome"], inv["scheda"], inv["posizione"], inv["stato"], inv["da"]) == ("velate", "waveshare",
                                                                                      "45.8205,8.8257", "attesa", "andrea")
    assert inv["scade"] - inv["creato"] == 14 * 24 * 3600
    f = rete.P(f"{rete.INVITI_DIR}/{inv['id']}.json")
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    assert sv.stato("andrea")["inviti"][0]["riga"] == inv["riga"]
    assert invita(sv, "sede")["riga"] != inv["riga"]                    # ogni invito ha la sua chiave
    for dati, atteso in (({"nome": "velate", "scheda": "usb"}, 409), ({"nome": "Velate!", "scheda": "usb"}, 400),
                         ({"nome": "x", "scheda": "inventata"}, 400),
                         ({"nome": "x", "scheda": "usb", "posizione": "qui"}, 400)):
        assert sv.azione("/api/invita", dati, "andrea")[0] == atteso, dati
    sv.metti_ponti("casa")
    assert sv.azione("/api/invita", {"nome": "casa", "scheda": "usb"}, "andrea")[0] == 409


def test_raspberry_con_l_invito_si_aggiunge_e_si_installa_da_solo(sv):  # noqa: F811
    pronto(sv)
    inv = invita(sv)
    codice, r = sv.nuova_richiesta(nuovo(1, inv["riga"], nome="raspberrypi"), "203.0.113.5")
    assert codice == 202 and r["in_corso"] is True
    [lavoro] = lavori()
    assert (lavoro["azione"], lavoro["nome"], lavoro["da"], lavoro["richiesta"]) == ("aggiungi", "velate", "andrea",
                                                                                    impronta(1))
    assert lavoro["installa"] == {"scheda": "waveshare", "posizione": "45.8205,8.8257"} and lavoro["chiave"] == chiave(1)
    assert rete.leggi_lavoro(rete.P(f"{rete.LAVORI_DIR}/{lavoro['id']}.json"))["installa"] == lavoro["installa"]
    r = richiesta_salvata(1)
    assert (r["nome"], r["stato"], r["invito"], r["nuovo"]["modello"]) == ("velate", "in_corso", inv["id"],
                                                                         "Raspberry Pi 3 Model B Rev 1.2")
    st = sv.stato("andrea")
    assert st["inviti"][0]["stato"] == "usato" and "riga" not in st["inviti"][0]
    assert st["richieste"][0]["invito"] is True and st["richieste"][0]["installa"]["scheda"] == "waveshare"
    # la chiave d'invito vale una volta: un secondo Raspberry che la presenta resta da approvare a mano
    codice, r = sv.nuova_richiesta(nuovo(2, inv["riga"], nome="copione"), "203.0.113.6")
    assert codice == 202 and "in_corso" not in r and len(lavori()) == 1
    assert richiesta_salvata(2)["stato"] == "attesa" and "installa" not in richiesta_salvata(2)
    # il primo ripete la richiesta: resta in corso, nessun altro lavoro
    assert sv.nuova_richiesta(nuovo(1, inv["riga"], nome="raspberrypi"), "203.0.113.5")[1]["in_corso"] is True
    assert len(lavori()) == 1


def test_invito_scaduto_sbagliato_o_annullato_non_vale(sv, monkeypatch):  # noqa: F811
    pronto(sv)
    inv = invita(sv)
    falsa = "ssh-ed25519 " + base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"z" * 32).decode()
    for n, riga in ((1, falsa), (2, "ssh-ed25519 AAAA"), (3, None)):
        codice, r = sv.nuova_richiesta(nuovo(n, riga, nome=f"p{n}"), f"203.0.113.{n}")
        assert codice == 202 and "in_corso" not in r
    assert lavori() == []
    f = rete.P(f"{rete.INVITI_DIR}/{inv['id']}.json")
    d = json.load(open(f))
    d["creato"] -= 15 * 24 * 3600
    json.dump(d, open(f, "w"))
    assert "in_corso" not in sv.nuova_richiesta(nuovo(4, inv["riga"]), "203.0.113.4")[1]
    assert sv.stato("andrea")["inviti"] == [] and not os.path.exists(f)       # scaduto: tolto
    inv = invita(sv, "sede")
    assert sv.azione("/api/annulla-invito", {"id": inv["id"]}, "andrea")[0] == 200
    assert sv.azione("/api/annulla-invito", {"id": inv["id"]}, "andrea")[0] == 404
    assert "in_corso" not in sv.nuova_richiesta(nuovo(5, inv["riga"]), "203.0.113.9")[1] and lavori() == []


def test_senza_invito_si_approva_a_mano_scegliendo_nome_e_radio(sv):  # noqa: F811
    pronto(sv)
    sv.nuova_richiesta(nuovo(1, nome="nuovo"), "203.0.113.5")
    v = sv.stato("andrea")["richieste"][0]
    assert v["nuovo"] == {"modello": "Raspberry Pi 3 Model B Rev 1.2", "radio_usb": False} and v["invito"] is False
    for installa in ({"nome": "sede", "scheda": "boh"}, {"nome": "Sede!", "scheda": "usb"},
                     {"nome": "sede", "scheda": "usb", "posizione": "0,0"}):
        assert sv.azione("/api/approva", {"id": impronta(1), "installa": installa}, "andrea")[0] == 400
    codice, r = sv.azione("/api/approva", {"id": impronta(1), "installa": {"nome": "sede", "scheda": "usb",
                                                                         "posizione": ""}}, "andrea")
    assert codice == 202 and "si installa da solo" in r["messaggio"]
    [lavoro] = lavori()
    assert (lavoro["nome"], lavoro["installa"]) == ("sede", {"scheda": "usb", "posizione": ""})
    assert richiesta_salvata(1)["nome"] == "sede"
    # un Raspberry installato a mano (senza "nuovo") non riceve certificato e canale
    sv.nuova_richiesta(domanda(2, "campo"), "203.0.113.7")
    codice, r = sv.azione("/api/approva", {"id": impronta(2), "installa": {"nome": "campo", "scheda": "usb"}}, "andrea")
    assert codice == 400 and "a mano" in r["errore"] and len(lavori()) == 1


def test_aggiunta_non_riuscita_si_riprova_con_le_scelte_dell_invito(sv):  # noqa: F811
    pronto(sv)
    inv = invita(sv)
    sv.nuova_richiesta(nuovo(1, inv["riga"]), "203.0.113.5")
    [lavoro] = lavori()
    os.unlink(rete.P(f"{rete.LAVORI_DIR}/{lavoro['id']}.json"))
    esito(lavoro["id"], "errore", "il nome meshtastic-velate non punta a questo server")
    v = sv.stato("andrea")["richieste"][0]
    assert v["stato"] == "attesa" and "non punta" in v["errore"] and v["installa"]["scheda"] == "waveshare"
    assert sv.azione("/api/approva", {"id": impronta(1)}, "andrea")[0] == 202
    assert lavori()[0]["installa"] == {"scheda": "waveshare", "posizione": "45.8205,8.8257"}


def test_avanzamento_e_riprova_dalla_pagina(sv):  # noqa: F811
    pronto(sv)
    sv.metti_ponti("velate")
    assert sv.azione("/api/riprova-installazione", {"nome": "velate"}, "andrea")[0] == 409      # non sta installando
    os.makedirs(rete.P(rete.AVANZAMENTO_DIR))
    sv._salva_installazione("velate", {"scheda": "waveshare", "posizione": "45.8,8.8"}, "andrea")
    f = rete.P(f"{rete.AVANZAMENTO_DIR}/velate.json")
    json.dump({"formato": 1, "fase": "errore", "testo": "Password del certificato sbagliata.", "quando": 1700000000},
              open(f, "w"))
    assert sv.stato("andrea")["installazioni"] == {"velate": {
        "fase": "errore", "testo": "Password del certificato sbagliata.", "quando": 1700000000, "scheda": "waveshare",
        "posizione": "45.8,8.8"}}
    assert sv.azione("/api/riprova-installazione", {"nome": "velate", "scheda": "boh"}, "andrea")[0] == 400
    assert sv.azione("/api/riprova-installazione", {"nome": "altro"}, "andrea")[0] == 404
    codice, r = sv.azione("/api/riprova-installazione", {"nome": "velate", "scheda": "usb", "posizione": ""}, "andrea")
    assert codice == 202 and "riprova" in r["messaggio"]
    [lavoro] = lavori()
    assert (lavoro["azione"], lavoro["nome"], lavoro["installa"]) == ("installa", "velate", {"scheda": "usb",
                                                                                         "posizione": ""})
    letto = rete.leggi_lavoro(rete.P(f"{rete.LAVORI_DIR}/{lavoro['id']}.json"))
    assert letto["azione"] == "installa" and letto["installa"] == {"scheda": "usb", "posizione": ""}
    assert sv.azione("/api/riprova-installazione", {"nome": "velate"}, "andrea")[0] == 409      # gia' chiesto
    # un file di avanzamento strano non rompe la pagina
    open(f, "w").write('{"fase": "<script>", "quando": "ieri"}')
    assert sv.stato("andrea")["installazioni"] == {}
    json.dump({"fase": "fatto", "testo": "x" * 900, "quando": 5}, open(f, "w"))
    assert len(sv.stato("andrea")["installazioni"]["velate"]["testo"]) == 300


# ---- impostazioni: certificato e canale ----

def impostazioni(pki, **cambi):
    d = {"nome_pacchetto": "meshtastic-cnsas-integration.zip",
         "pacchetto": base64.b64encode((pki / "meshtastic-cnsas-integration.zip").read_bytes()).decode(),
         "password": "atakatak", "host": "", "canale": "CNSAS-VA", "chiave": PSK, "secondario": ""}
    d.update(cambi)
    return d


def test_impostazioni_dalla_pagina_al_lavoro_di_root(sv, pki):  # noqa: F811
    for cambi, parola in (({"nome_pacchetto": "virus.exe"}, "pacchetto"), ({"pacchetto": "non base64!"}, "certificato"),
                          ({"chiave": "corta"}, "chiave"), ({"canale": "nome troppo lungo"}, "canale"),
                          ({"host": "tak server"}, "TAK Server"), ({"secondario": "con spazi"}, "pubblico"),
                          ({"pacchetto": base64.b64encode(b"x" * (rete.PACCHETTO_MAX + 1)).decode()}, "grande")):
        codice, r = sv.azione("/api/impostazioni-nuovi", impostazioni(pki, **cambi), "andrea")
        assert codice == 400 and parola in r["errore"], (cambi.keys(), r)
    assert lavori() == []
    codice, r = sv.azione("/api/impostazioni-nuovi", impostazioni(pki, nome_pacchetto="C:\\Scaricati\\meshtastic-cnsas-integration.zip"),
                          "andrea")
    assert codice == 202
    [lavoro] = lavori()
    f = rete.P(f"{rete.LAVORI_DIR}/{lavoro['id']}.json")
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    assert lavoro["impostazioni"]["nome_pacchetto"] == "meshtastic-cnsas-integration.zip"
    # nella pagina, del lavoro si vede solo che c'e'
    testo = json.dumps(sv.stato("andrea"))
    assert PSK not in testo and "atakatak" not in testo and "impostazioni-nuovi" in testo
    assert sv.azione("/api/impostazioni-nuovi", impostazioni(pki), "andrea")[0] == 409          # uno alla volta
    letto = rete.leggi_lavoro(f)
    assert letto["azione"] == "impostazioni-nuovi" and letto["impostazioni"]["chiave"] == PSK
    # un lavoro normale grande come un certificato non passa
    grande = dict(lavoro, azione="rimuovi", nome="casa", riempitivo="x" * 20000)
    json.dump(grande, open(f, "w"))
    assert os.path.getsize(f) > rete.LAVORO_MAX and rete.leggi_lavoro(f) is None


@pytest.fixture
def radice(tmp_path, monkeypatch):
    monkeypatch.setattr(rete, "RADICE", str(tmp_path))
    monkeypatch.setattr(rete, "VERSIONE", "1.10.0")
    monkeypatch.setattr(rete, "REGISTRO_LOG", "/registro.log", raising=False)
    monkeypatch.setattr(rete, "porta_ssh", lambda: 22)
    monkeypatch.setattr(rete, "chiavi_host", lambda: ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIserver"])
    os.makedirs(rete.P(rete.CONF_DIR))
    os.makedirs(rete.P(rete.ACCESSO_DIR))
    return tmp_path


def test_root_controlla_e_salva_certificato_e_canale(radice, pki, capsys):
    lavoro = {"da": "andrea", "impostazioni": impostazioni(pki)}
    messaggio = rete.salva_impostazioni_nuovi(lavoro)
    assert "meshtastic-cnsas" in messaggio and "CNSAS-VA" in messaggio and PSK not in messaggio
    assert PSK not in capsys.readouterr().out
    f = rete.P(rete.IMPOSTAZIONI_NUOVI)
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600 and stat.S_IMODE(os.stat(os.path.dirname(f)).st_mode) == 0o700
    salvate = json.load(open(f))
    assert salvate["canale"] == {"nome": "CNSAS-VA", "chiave": PSK, "secondario": ""} and salvate["password"] == "atakatak"
    riassunto = open(rete.P(rete.RIASSUNTO_NUOVI)).read()
    r = json.loads(riassunto)
    assert r["pronto"] is True and r["certificato"] == "meshtastic-cnsas" and r["canale"] == "CNSAS-VA"
    assert r["scade"] > time.time() and r["server"].endswith(":8089") and r["da"] == "andrea"
    assert PSK not in riassunto and "atakatak" not in riassunto and salvate["pacchetto"][:40] not in riassunto
    assert not os.path.exists(rete.P(rete.PONTI_NUOVI_DIR + "/controllo"))                    # niente avanzi
    # password sbagliata, file che non e' un pacchetto: non si salva nulla di nuovo
    with pytest.raises(rete.Errore, match="password"):
        rete.salva_impostazioni_nuovi({"da": "andrea", "impostazioni": impostazioni(pki, password="sbagliata")})
    with pytest.raises(rete.Errore, match="non si apre"):
        rete.salva_impostazioni_nuovi({"da": "andrea", "impostazioni": impostazioni(
            pki, pacchetto=base64.b64encode(b"non sono uno zip").decode())})
    assert json.load(open(f))["password"] == "atakatak"
    # anche il solo .p12
    p12 = base64.b64encode((pki / "meshtastic-cnsas.p12").read_bytes()).decode()
    with pytest.raises(rete.Errore, match="indirizzo del TAK Server"):
        rete.salva_impostazioni_nuovi({"da": "a", "impostazioni": impostazioni(pki, nome_pacchetto="c.p12", pacchetto=p12)})
    rete.salva_impostazioni_nuovi({"da": "a", "impostazioni": impostazioni(pki, nome_pacchetto="c.p12", pacchetto=p12,
                                                                          host="tak.prova.lan")})
    assert json.load(open(rete.P(rete.RIASSUNTO_NUOVI)))["server"] == "tak.prova.lan:8089"


def test_configurazione_del_ponte_nuovo_e_registrazione(radice, pki):
    reg = {"formato": 1, "ponti": {"velate": {"porta": 19402, "chiave": chiave(1), "impronta": impronta(1)}}}
    with pytest.raises(rete.Errore, match="certificato e canale"):
        rete.prepara_configurazione(IMP, reg, "velate", {"scheda": "waveshare", "posizione": ""}, "andrea")
    rete.salva_impostazioni_nuovi({"da": "andrea", "impostazioni": impostazioni(pki, secondario="Varese")})
    assert rete.prepara_configurazione(IMP, reg, "velate", {"scheda": "waveshare", "posizione": "45.8,8.8"}, "andrea") == 1
    f = rete.file_configurazione("velate")
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o400
    assert stat.S_IMODE(os.stat(os.path.dirname(f)).st_mode) == 0o1730
    assert stat.S_IMODE(os.stat(rete.P(rete.AVANZAMENTO_DIR)).st_mode) == 0o1731
    c = json.load(open(f))
    assert (c["nome"], c["scheda"], c["posizione"], c["rete"], c["versione"], c["tentativo"]) == (
        "velate", "waveshare", "45.8,8.8", "https://meshtastic.prova.lan", "1.10.0", 1)
    assert c["canale"] == {"nome": "CNSAS-VA", "chiave": PSK, "secondario": "Varese"}
    assert c["tak"]["password"] == "atakatak" and c["tak"]["nome_pacchetto"] == "meshtastic-cnsas-integration.zip"
    assert base64.b64decode(c["tak"]["pacchetto"]) == (pki / "meshtastic-cnsas-integration.zip").read_bytes()
    assert reg["ponti"]["velate"]["installa"]["tentativo"] == 1
    # la registrazione pubblica dice solo che c'e' qualcosa da installare
    rete.pubblica_registrazioni(IMP, reg, ["velate"])
    pubblica = open(rete.P(f"{rete.ACCESSO_DIR}/{impronta(1)}.json")).read()
    assert json.loads(pubblica)["installa"] == {"tentativo": 1}
    assert PSK not in pubblica and "atakatak" not in pubblica and "45.8" not in pubblica
    # "Riprova": tentativo 2, le scelte di prima se non ne arrivano di nuove
    rete.riprova_installazione(IMP, reg, {"nome": "velate", "da": "andrea", "installa": {"scheda": "usb"}})
    c = json.load(open(f))
    assert (c["tentativo"], c["scheda"], c["posizione"]) == (2, "usb", "45.8,8.8")
    assert json.load(open(rete.P(f"{rete.ACCESSO_DIR}/{impronta(1)}.json")))["installa"] == {"tentativo": 2}
    with pytest.raises(rete.Errore):
        rete.riprova_installazione(IMP, reg, {"nome": "altro", "da": "a", "installa": {}})
    # installato (il Raspberry ha tolto la configurazione): la registrazione non lo chiede piu'
    os.unlink(f)
    rete.pubblica_registrazioni(IMP, reg, ["velate"])
    assert "installa" not in json.load(open(rete.P(f"{rete.ACCESSO_DIR}/{impronta(1)}.json")))
    # configurazioni dimenticate: dopo 30 giorni via
    rete.prepara_configurazione(IMP, reg, "velate", {"scheda": "usb", "posizione": ""}, "andrea")
    vecchio = time.time() - 31 * 24 * 3600
    os.utime(f, (vecchio, vecchio))
    rete.pulisci_configurazioni()
    assert not os.path.exists(f)


# ---- il comando dei ponti via SSH ----

def comando(monkeypatch, capsys, utente, richiesta):
    monkeypatch.setenv("RETE_PROVA_UTENTE", utente)
    if richiesta is None:
        monkeypatch.delenv("SSH_ORIGINAL_COMMAND", raising=False)
    else:
        monkeypatch.setenv("SSH_ORIGINAL_COMMAND", richiesta)
    codice = rete.cmd_ponte(None)
    letto = capsys.readouterr()
    return codice, letto.out, letto.err


def test_comando_dei_ponti_solo_configurazione_e_avanzamento(radice, pki, monkeypatch, capsys):
    reg = {"formato": 1, "ponti": {"velate": {"porta": 19402, "chiave": chiave(1), "impronta": impronta(1)},
                                   "casa": {"porta": 19401, "chiave": chiave(2), "impronta": impronta(2)}}}
    rete.salva_impostazioni_nuovi({"da": "andrea", "impostazioni": impostazioni(pki)})
    rete.prepara_configurazione(IMP, reg, "velate", {"scheda": "waveshare", "posizione": ""}, "andrea")
    capsys.readouterr()
    codice, uscita, _ = comando(monkeypatch, capsys, "ponte-velate", "configurazione")
    assert codice == 0 and json.loads(uscita)["canale"]["chiave"] == PSK
    av = rete.P(f"{rete.AVANZAMENTO_DIR}/velate.json")
    assert json.load(open(av))["fase"] == "configurazione" and stat.S_IMODE(os.stat(av).st_mode) == 0o644
    # un altro ponte non ha niente da leggere, e non puo' chiedere quella degli altri
    codice, uscita, errore = comando(monkeypatch, capsys, "ponte-casa", "configurazione")
    assert codice == 3 and uscita == "" and "nessuna configurazione" in errore
    for strano in ("configurazione velate", "configurazione ../velate", "cat /etc/passwd", "avanzamento", "avanzamento boh x",
                   "avanzamento\tfatto", "sh -c id"):
        if strano == "avanzamento\tfatto":
            continue
        codice, uscita, _ = comando(monkeypatch, capsys, "ponte-casa", strano)
        assert codice == 2 and uscita == "", strano
    assert comando(monkeypatch, capsys, "ponte-casa", None)[0] == 1                # niente terminale
    assert comando(monkeypatch, capsys, "root", "configurazione")[0] == 1
    assert comando(monkeypatch, capsys, "ponte-../../x", "configurazione")[0] == 1
    assert os.path.exists(rete.file_configurazione("velate"))
    # avanzamento: testo ripulito e corto, solo nel file del proprio ponte
    assert comando(monkeypatch, capsys, "ponte-velate", "avanzamento installo 3/9  Meshtastic\x1b[0m\n" + "x" * 500)[0] == 0
    d = json.load(open(av))
    assert d["fase"] == "installo" and d["testo"].startswith("3/9  Meshtastic [0m x") and len(d["testo"]) == 300
    assert abs(d["quando"] - time.time()) < 5
    assert sorted(os.listdir(rete.P(rete.AVANZAMENTO_DIR))) == ["velate.json"]
    # a installazione finita la configurazione (certificato e canale) sparisce dal server
    assert comando(monkeypatch, capsys, "ponte-velate", "avanzamento fatto installato: mesh2tak 1.10.0")[0] == 0
    assert not os.path.exists(rete.file_configurazione("velate")) and json.load(open(av))["fase"] == "fatto"
    assert comando(monkeypatch, capsys, "ponte-velate", "avanzamento fatto di nuovo")[0] == 0


def test_sshd_e_caddy_per_i_ponti(radice, monkeypatch):
    monkeypatch.setattr(rete, "RADICE", "")
    comando_forzato = "/usr/bin/python3 -I /opt/mesh2tak-rete/rete_meshtastic.py ponte"
    assert rete.comando_ponte() == comando_forzato
    assert f"\tForceCommand {comando_forzato}\n" in rete.testo_dropin() and "PermitTTY no" in rete.testo_dropin()
    riga = rete.riga_autorizzata(chiave(1), 19401)
    assert riga == f'restrict,port-forwarding,permitlisten="127.0.0.1:19401",command="{comando_forzato}" {chiave(1)}\n'
    vecchia = f'restrict,port-forwarding,permitlisten="127.0.0.1:19401",command="/usr/sbin/nologin" {chiave(1)}\n'
    assert rete.stessa_chiave_e_porta(vecchia, riga)                    # cambia solo il comando: il tunnel resta
    assert not rete.stessa_chiave_e_porta(vecchia, rete.riga_autorizzata(chiave(2), 19401))
    assert not rete.stessa_chiave_e_porta(vecchia, rete.riga_autorizzata(chiave(1), 19402))
    assert not rete.stessa_chiave_e_porta(None, riga)
    assert rete.SHELL_PONTI == "/bin/sh"


def test_ponte_che_si_installa_non_fa_partire_avvisi(radice):
    f = rete.P(f"{rete.AVANZAMENTO_DIR}/velate.json")
    assert rete.Sorveglianza._in_installazione("velate") is False
    os.makedirs(os.path.dirname(f))
    json.dump({"fase": "installo", "testo": "2/9", "quando": 1}, open(f, "w"))
    assert rete.Sorveglianza._in_installazione("velate") is True
    json.dump({"fase": "fatto", "testo": "", "quando": 1}, open(f, "w"))
    assert rete.Sorveglianza._in_installazione("velate") is False
