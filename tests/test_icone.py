"""Icone sulla mappa (1.11.0): scelte dalla pagina della rete per ogni radio e ogni ponte, tenute dal servizio della
pagina, consegnate dal tunnel a ogni ponte, usate dal ponte negli eventi per il TAK Server."""
import base64
import io
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zlib

import pytest

from conftest import RADICE
from test_gestione import SENZA_VERIFICA, gestione  # noqa: F401  (fixture)
from test_ponte_logica import MIO, B, ponte, posizione, sdk  # noqa: F401  (fixture)

sys.path.insert(0, os.path.join(RADICE, "server"))
import mesh2tak as m  # noqa: E402
import rete_meshtastic as rete  # noqa: E402

CANE = "6d781afb-89a6-4c07-b2b9-a89748b6a38f/Leisure/dog.png"


def png(colore=(200, 30, 30, 255)):
    def pezzo(tipo, dati):
        return struct.pack(">I", len(dati)) + tipo + dati + struct.pack(">I", zlib.crc32(tipo + dati) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + pezzo(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)) +
            pezzo(b"IDAT", zlib.compress(b"\x00" + bytes(colore))) + pezzo(b"IEND", b""))


# ---- sul server: elenco e figure per la pagina ----

@pytest.fixture
def radice(tmp_path, monkeypatch):
    monkeypatch.setattr(rete, "RADICE", str(tmp_path))
    for d in (rete.SITO_DIR, rete.ACCESSO_DIR, rete.RICHIESTE_DIR, rete.LAVORI_DIR, rete.TMP_DIR, rete.ESITI_DIR):
        os.makedirs(rete.P(d))
    return tmp_path


def infratak(radice, catalogo):
    impostazioni = radice / "opt/infratak/.config/settings.json"
    impostazioni.parent.mkdir(parents=True)
    impostazioni.write_text(json.dumps({"fqdn": "prova.lan"}))
    if catalogo is not None:
        (radice / "opt/infratak/nodered").mkdir()
        (radice / "opt/infratak/nodered/icon-catalog.json").write_text(json.dumps(catalogo))
    return rete.Impostazioni({"fqdn": "prova.lan"}, str(impostazioni))


def test_elenco_e_figure_prese_dal_catalogo_di_infratak(radice):
    persone, cane = png((10, 10, 10, 255)), png((120, 80, 20, 255))
    catalogo = {
        "OSM": {"uid": rete.ICONSET_OSM, "groups": {
            "General": [{"f": "people.png", "t": "a-u-G", "b": base64.b64encode(persone).decode()}],
            "Leisure": [{"f": "dog.png", "b": base64.b64encode(cane).decode()}],
            "Misc": [{"f": "Base.png", "b": base64.b64encode(b"GIF89a non e' un png").decode()}],
            "Transport": [{"f": "car.png", "b": "@@@ non e' base64"}]}},
        "Altro": {"uid": "34ae1613-9645-4222-a9d2-e5f243dea2865", "groups": {
            "Lodging": [{"f": "chalet.png", "b": base64.b64encode(png()).decode()}]}}}
    imp = infratak(radice, catalogo)
    os.makedirs(rete.P(rete.ICONE_SITO))
    vecchia = rete.P(rete.ICONE_SITO + "/vecchia.png")
    open(vecchia, "wb").write(png())
    rete.scrivi_icone_mappa(imp)
    elenco = json.load(open(rete.P(rete.ICONE_ELENCO)))["icone"]
    assert [x["chiave"] for x in elenco] == [c[0] for c in rete.ICONE_MAPPA]
    figure = {x["chiave"]: x["figura"] for x in elenco}
    assert figure["soccorritore"] == "icone/soccorritore.png" and figure["cinofilo"] == "icone/cinofilo.png"
    # figura che non e' un PNG, non leggibile o di un altro insieme di icone: niente figura, il nome resta
    assert figure["base"] == figure["auto"] == figure["rifugio"] == ""
    assert open(rete.P(rete.ICONE_SITO + "/cinofilo.png"), "rb").read() == cane
    assert oct(os.stat(rete.P(rete.ICONE_SITO + "/cinofilo.png")).st_mode)[-3:] == "644"
    assert oct(os.stat(rete.P(rete.ICONE_SITO)).st_mode)[-3:] == "755"
    assert not os.path.exists(vecchia)
    voce = next(x for x in elenco if x["chiave"] == "cinofilo")
    assert voce == {"chiave": "cinofilo", "nome": "Unità cinofila", "gruppo": "Persone", "tipo": "a-f-G-U-C-I",
                    "figura": "icone/cinofilo.png"}
    # infra-TAK senza catalogo (versione vecchia): la scelta resta, senza figure
    os.unlink(radice / "opt/infratak/nodered/icon-catalog.json")
    rete.scrivi_icone_mappa(imp)
    assert all(x["figura"] == "" for x in json.load(open(rete.P(rete.ICONE_ELENCO)))["icone"])
    assert os.listdir(rete.P(rete.ICONE_SITO)) == []


def test_ogni_icona_ha_un_tipo_e_una_figura_che_il_ponte_accetta():
    chiavi = [c[0] for c in rete.ICONE_MAPPA]
    assert len(chiavi) == len(set(chiavi))
    for voce in rete.ICONE_PER_CHIAVE.values():
        assert m.RE_TIPO_ICONA.match(voce["tipo"]), voce
        assert m.RE_PERCORSO_ICONA.match(voce["percorso"]), voce
    # la predefinita dei ponti e' il traliccio di sempre
    assert rete.ICONE_PER_CHIAVE["traliccio"]["percorso"] == m.ICONA_PONTE


# ---- sul server: scelte dalla pagina ----

def test_scelte_dalla_pagina(radice):
    s = rete.Servizio()
    codice, r = s.azione("/api/icona", {"radio": "!7c3a91e2", "icona": "cinofilo"}, "andrea")
    assert codice == 200 and "Icona salvata" in r["messaggio"]
    scelte = s.scelte_icone()
    assert scelte["!7c3a91e2"]["icona"] == "cinofilo" and scelte["!7c3a91e2"]["da"] == "andrea"
    assert oct(os.stat(rete.P(rete.ICONE_SCELTE)).st_mode)[-3:] == "600"
    for dati in ({"radio": "7c3a91e2", "icona": "cinofilo"}, {"radio": "!7C3A91E2", "icona": "cinofilo"},
                 {"radio": "!7c3a91e2x", "icona": "cinofilo"}, {"radio": "!7c3a91e2", "icona": "drago"},
                 {"radio": "!7c3a91e2", "icona": 5}, {"icona": "cinofilo"}):
        assert s.azione("/api/icona", dati, "andrea")[0] == 400, dati
    assert s.scelte_icone()["!7c3a91e2"]["icona"] == "cinofilo"
    versione = s.icone_per_i_ponti(s.scelte_icone())[0]
    assert s.azione("/api/icona", {"radio": "!7c3a91e2", "icona": "ambulanza"}, "maria")[0] == 200
    assert s.icone_per_i_ponti(s.scelte_icone())[0] != versione
    codice, r = s.azione("/api/icona", {"radio": "!7c3a91e2", "icona": ""}, "andrea")
    assert codice == 200 and "predefinita" in r["messaggio"] and s.scelte_icone() == {}


def test_la_versione_dipende_solo_da_cosa_ricevono_i_ponti():
    a = {"!00000001": {"icona": "sci", "da": "andrea", "quando": 1}, "!00000002": {"icona": "blu", "da": "x", "quando": 2}}
    b = {"!00000002": {"icona": "blu", "da": "maria", "quando": 9}, "!00000001": {"icona": "sci", "da": "y", "quando": 8}}
    va, mappa = rete.Servizio.icone_per_i_ponti(a)
    assert va == rete.Servizio.icone_per_i_ponti(b)[0] and len(va) == 16
    assert mappa == {"!00000001": {"percorso": rete.ICONE_PER_CHIAVE["sci"]["percorso"], "tipo": "a-f-G-U-C-I"},
                     "!00000002": {"percorso": rete.ICONE_PER_CHIAVE["blu"]["percorso"], "tipo": "a-f-G-U-C"}}
    assert rete.Servizio.icone_per_i_ponti({})[0] != va


def test_al_massimo_500_radio(radice):
    s = rete.Servizio()
    scelte = {f"!{i:08x}": {"icona": "rosso", "da": "andrea", "quando": 1} for i in range(rete.ICONE_MAX)}
    rete.Servizio._scrivi_json(rete.P(rete.ICONE_SCELTE), {"formato": 1, "scelte": scelte})
    assert s.azione("/api/icona", {"radio": "!ffffffff", "icona": "blu"}, "andrea")[0] == 409
    assert s.azione("/api/icona", {"radio": "!00000001", "icona": "blu"}, "andrea")[0] == 200, "cambiarne una si puo'"


def test_scelte_strane_sul_file_ignorate(radice):
    rete.Servizio._scrivi_json(rete.P(rete.ICONE_SCELTE), {"formato": 1, "scelte": {
        "!00000001": {"icona": "sci"}, "../x": {"icona": "sci"}, "!00000002": {"icona": "drago"}, "!00000003": "sci"}})
    assert list(rete.Servizio().scelte_icone()) == ["!00000001"]


# ---- sul server: consegna ai ponti dal tunnel ----

def test_le_icone_arrivano_ai_ponti(radice):
    s = rete.Servizio()
    s.metti = None
    mandate = []

    def manda(porta, corpo):
        d = json.loads(corpo)
        mandate.append((porta, d))
        return risposta(d)
    risposta = lambda d: {"versione": d["versione"], "numero": len(d["icone"])}  # noqa: E731
    s.manda_icone = manda
    s.allinea_icone("casa", 40001, None)                       # ponte che non risponde: niente
    assert mandate == [] and s.icone_ponti == {}
    s.allinea_icone("casa", 40001, {"formato": 1})            # ponte di prima della 1.11.0: non le capirebbe
    assert mandate == [] and s.vista_icone(["casa"])["ponti"] == {"casa": "vecchio"}
    s.azione("/api/icona", {"radio": "!7c3a91e2", "icona": "cinofilo"}, "andrea")
    versione = s.vista_icone(["casa"])["versione"]
    s.allinea_icone("casa", 40001, {"formato": 1, "icone": {"versione": "", "numero": 0}})
    assert len(mandate) == 1 and mandate[0][0] == 40001
    corpo = mandate[0][1]
    assert corpo == {"formato": 1, "versione": versione,
                     "icone": {"!7c3a91e2": {"percorso": CANE, "tipo": "a-f-G-U-C-I"}}}
    assert "andrea" not in json.dumps(corpo), "ai ponti solo figure e tipi, non chi le ha scelte"
    assert s.vista_icone(["casa", "velate"])["ponti"] == {"casa": "aggiornato", "velate": "attesa"}
    # gia' uguali: non si rimandano
    s.allinea_icone("casa", 40001, {"formato": 1, "icone": {"versione": versione, "numero": 1}})
    assert len(mandate) == 1
    # il ponte non le prende (o risponde altro): si riprova al giro dopo
    risposta = lambda d: None  # noqa: E731
    s.allinea_icone("velate", 40002, {"formato": 1, "icone": {"versione": "0123456789abcdef", "numero": 3}})
    assert len(mandate) == 2 and s.vista_icone(["velate"])["ponti"] == {"velate": "attesa"}
    risposta = lambda d: {"versione": "fedcba9876543210"}  # noqa: E731
    s.allinea_icone("velate", 40002, {"formato": 1, "icone": {"versione": "0123456789abcdef", "numero": 3}})
    assert len(mandate) == 3 and s.vista_icone(["velate"])["ponti"] == {"velate": "attesa"}


def test_la_sorveglianza_passa_ogni_stato_alle_icone():
    stati = {40001: {"formato": 1, "icone": {"versione": ""}}, 40002: None}
    sv = rete.Sorveglianza(ora=lambda: 1_800_000_000.0, prendi=lambda porta: stati[porta], manda=lambda *a: ("mandato", ""),
                           percorso="/dev/null/x", config=lambda: {"ponti": [{"nome": "casa", "porta": 40001},
                                                                              {"nome": "velate", "porta": 40002}]})
    sv._salva = lambda: None
    visti = []

    def dopo(nome, porta, st):
        visti.append((nome, porta, st))
        raise RuntimeError("guasto nelle icone")              # non ferma gli avvisi
    sv.dopo = dopo
    sv.giro()
    assert visti == [("casa", 40001, stati[40001]), ("velate", 40002, None)]
    assert "irraggiungibile" in sv.s["ponti"]["velate"]["problemi"]


def test_stato_della_pagina_con_le_icone(radice):
    s = rete.Servizio()
    open(rete.P(rete.SITO_DIR + "/ponti.json"), "w").write(json.dumps({"formato": 1, "ponti": [{"nome": "casa"}]}))
    s.azione("/api/icona", {"radio": "!7c3a91e2", "icona": "sci"}, "andrea")
    ic = s.stato("andrea")["icone"]
    assert ic["scelte"]["!7c3a91e2"]["icona"] == "sci" and ic["ponti"] == {"casa": "attesa"} and len(ic["versione"]) == 16


# ---- sul ponte ----

def corpo_icone(icone, versione="0123456789abcdef", formato=1):
    return json.dumps({"formato": formato, "versione": versione, "icone": icone}).encode()


def test_icone_controllate_e_tenute_su_file(tmp_path, caplog):
    f = tmp_path / "icone.json"
    ic = m.IconeMappa(str(f))
    assert ic.versione == "" and ic.di("!2222bbbb") is None and ic.stato() == {"versione": "", "numero": 0}
    stato, risposta, cambiate = ic.imposta(corpo_icone({"!2222bbbb": {"percorso": CANE, "tipo": "a-f-G-U-C-I"}}))
    assert (stato, risposta, cambiate) == (200, {"versione": "0123456789abcdef", "numero": 1}, {"!2222bbbb"})
    assert m.IconeMappa(str(f)).di("!2222bbbb") == ("a-f-G-U-C-I", CANE), "dopo un riavvio del ponte"
    for sbagliato in (b"non json", corpo_icone({}, formato=2), corpo_icone({}, versione="corta"),
                      corpo_icone({"2222bbbb": {"percorso": CANE, "tipo": "a-f-G"}}),
                      corpo_icone({"!2222bbbb": {"percorso": CANE, "tipo": "a-f-G-U-C<script>"}}),
                      corpo_icone({"!2222bbbb": {"percorso": CANE, "tipo": "b-m-p-s-m"}}),
                      corpo_icone({"!2222bbbb": {"percorso": "../../../etc/passwd.png", "tipo": "a-f-G"}}),
                      corpo_icone({"!2222bbbb": {"percorso": "javascript:alert(1)", "tipo": "a-f-G"}}),
                      corpo_icone({"!2222bbbb": {"percorso": CANE.replace("Leisure", ".."), "tipo": "a-f-G"}}),
                      corpo_icone({"!2222bbbb": {"percorso": CANE.replace("dog", ".dog"), "tipo": "a-f-G"}}),
                      corpo_icone({f"!{i:08x}": {"percorso": CANE, "tipo": "a-f-G"} for i in range(501)}), None):
        assert ic.imposta(sbagliato)[0] == 400, sbagliato
    assert ic.di("!2222bbbb") == ("a-f-G-U-C-I", CANE), "una consegna sbagliata non cambia niente"
    # tolta l'icona: cambia solo quella radio
    _, _, cambiate = ic.imposta(corpo_icone({}, versione="fedcba9876543210"))
    assert cambiate == {"!2222bbbb"} and m.IconeMappa(str(f)).stato() == {"versione": "fedcba9876543210", "numero": 0}
    f.write_text("{guasto")
    assert m.IconeMappa(str(f)).stato()["numero"] == 0 and "non valido" in caplog.text


def eventi(p, uid):
    return [ET.fromstring(x) for u, x in p.tak.posizioni if u == uid]


def test_la_radio_con_l_icona_scelta(ponte):
    p = ponte()
    p._gestisci(posizione(B, 45.9, 8.8))
    prima = eventi(p, "!2222bbbb")[-1]
    assert prima.get("type") == "a-f-G-U-C" and prima.find("detail/usericon") is None
    assert prima.find("detail/__group") is not None
    stato, _ = p.imposta_icone(corpo_icone({"!2222bbbb": {"percorso": CANE, "tipo": "a-f-G-U-C-I"}}))
    assert stato == 200
    p.manutenzione()                                    # rimandata subito, senza aspettare la ripetizione
    dopo = eventi(p, "!2222bbbb")
    assert len(dopo) == 2
    ev = dopo[-1]
    assert ev.get("type") == "a-f-G-U-C-I" and ev.find("detail/usericon").get("iconsetpath") == CANE
    assert ev.find("detail/__group") is None, "l'icona scelta al posto del colore della squadra"
    assert ev.find("detail/contact").get("callsign") == "Ponte Sede"
    p.manutenzione()
    assert len(eventi(p, "!2222bbbb")) == 2, "poi di nuovo ogni 2 minuti"
    # tornata la predefinita
    p.imposta_icone(corpo_icone({}, versione="fedcba9876543210"))
    p.manutenzione()
    ev = eventi(p, "!2222bbbb")[-1]
    assert ev.get("type") == "a-f-G-U-C" and ev.find("detail/usericon") is None and ev.find("detail/__group") is not None


def test_il_ponte_con_l_icona_scelta(ponte):
    p = ponte()
    p._gestisci(posizione(MIO, 45.8, 8.6, canale=0))
    ev = eventi(p, f"!{MIO:08x}")[-1]
    assert ev.find("detail/usericon").get("iconsetpath") == m.ICONA_PONTE and ev.get("type") == "a-f-G-I"
    base = "6d781afb-89a6-4c07-b2b9-a89748b6a38f/Misc/Base.png"
    p.imposta_icone(corpo_icone({f"!{MIO:08x}": {"percorso": base, "tipo": "a-f-G-I"}}))
    p.manutenzione()
    ev = eventi(p, f"!{MIO:08x}")[-1]
    assert ev.find("detail/usericon").get("iconsetpath") == base


class Finto:
    """Richiesta HTTP finta per Gestione._servi_icone."""

    def __init__(self, metodo, corpo=b"", da="127.0.0.1", **intestazioni):
        self.command, self.client_address = metodo, (da, 5555)
        self.headers = dict({"Content-Length": str(len(corpo))}, **intestazioni)
        self.rfile, self.wfile = io.BytesIO(corpo), io.BytesIO()
        self.codice, self.close_connection = None, False

    def send_response(self, codice, *_):
        self.codice = codice

    def send_header(self, *_):
        pass

    def end_headers(self):
        pass


def test_le_icone_si_accettano_solo_dal_tunnel(gestione):
    g, _, _ = gestione()
    arrivate = []

    def prendi(corpo):
        arrivate.append(corpo)
        return 200, {"versione": "0123456789abcdef", "numero": 1}
    g.icone = prendi
    corpo = corpo_icone({"!2222bbbb": {"percorso": CANE, "tipo": "a-f-G-U-C-I"}})

    def chiedi(metodo, dati=b"", **intestazioni):
        r = urllib.request.Request(f"https://127.0.0.1:{g.cfg.gestione_porta_pagina}/mesh2tak/icone", data=dati or None,
                                   method=metodo, headers=intestazioni)
        try:
            with urllib.request.urlopen(r, context=SENZA_VERIFICA, timeout=30) as risposta:
                return risposta.status, json.loads(risposta.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    assert chiedi("POST", corpo, **{"X-Mesh2tak": "1"}) == (200, {"versione": "0123456789abcdef", "numero": 1})
    assert arrivate == [corpo]
    assert chiedi("POST", corpo)[0] == 403, "senza l'intestazione della rete (es. un altro sito nel browser)"
    assert chiedi("GET", **{"X-Mesh2tak": "1"})[0] == 405
    assert chiedi("POST", b"x" * 140000, **{"X-Mesh2tak": "1"})[0] == 413
    assert len(arrivate) == 1 and not g.pausa.attiva, "le icone non mettono in pausa il ponte"
    # dal Wi-Fi del posto (non dal tunnel): rifiutate, anche con l'intestazione
    h = Finto("POST", corpo, da="192.168.1.50", **{"X-Mesh2tak": "1"})
    g._servi_icone(h)
    assert h.codice == 403 and len(arrivate) == 1
    g.icone = None
    h = Finto("POST", corpo, **{"X-Mesh2tak": "1"})
    g._servi_icone(h)
    assert h.codice == 404


def test_lo_stato_del_ponte_dice_che_icone_usa(ponte):
    p = ponte()
    p.imposta_icone(corpo_icone({"!2222bbbb": {"percorso": CANE, "tipo": "a-f-G-U-C-I"}}))
    tak = type("T", (), {"collegato": __import__("threading").Event(), "collegato_dal": None, "ultimo_errore": ""})()
    g = type("G", (), {"stato_pausa": lambda self: {"attiva": False}, "nodo_pagina": "", "radio": None})()
    s = m.stato_per_la_rete(p.cfg, p, tak, g, None, time.time())
    assert s["icone"] == {"versione": "0123456789abcdef", "numero": 1}
