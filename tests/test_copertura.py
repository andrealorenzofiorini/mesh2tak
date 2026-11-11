"""Mappa della copertura (1.14.0): il ponte annota, per quadretti di circa 200 m, con che segnale ha sentito le radio
della squadra e dove si trovavano. Solo quadretto, segnale e giorno: niente radio ne' orari."""
import json
import math
import os
import time

import pytest

import mesh2tak as m
from test_gestione import gestione  # noqa: F401 (fixture)
from test_icone import Finto
from test_ponte_logica import MIO, A, B, golden, pacchetto, ponte, posizione, sdk  # noqa: F401 (fixture)


def con_segnale(p, salti=0, snr=-7.5, rssi=-110, inizio=3):
    p["hopStart"], p["hopLimit"] = inizio, inizio - salti
    if rssi is not None:
        p["rxRssi"] = rssi
    if snr is not None:
        p["rxSnr"] = snr
    return p


def oggi(p=None):
    from datetime import datetime
    return int((datetime.now(p.tz) if p is not None and p.tz else datetime.now()).strftime("%Y%m%d"))


def test_il_quadretto_contiene_il_punto():
    for lat, lon in ((45.8206, 8.8251), (45.9, 8.8), (-33.86, 151.21), (0.0001, -0.0001)):
        x, y = m.Copertura.quadretto(lat, lon)
        n = 1 << 17
        ovest, est = x / n * 360 - 180, (x + 1) / n * 360 - 180
        nord = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
        sud = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
        assert ovest <= lon < est and sud < lat <= nord
    # alle nostre latitudini un quadretto e' largo circa 210 m
    x, _ = m.Copertura.quadretto(45.82, 8.82)
    largo = 40075016.686 * math.cos(math.radians(45.82)) / (1 << 17)
    assert 200 < largo < 225
    assert m.Copertura.quadretto(89.9, 179.999999) == ((1 << 17) - 1, 0), "ai bordi resta dentro la mappa"


def test_dirette_e_tramite_altre_radio():
    c = m.Copertura()
    c.annota(45.9, 8.8, 0, -5.0, -100, 20261110)
    c.annota(45.9, 8.8, 0, -9.0, -112, 20261111)
    c.annota(45.9, 8.8, 2, None, None, 20261109)
    c.annota(45.95, 8.85, 1, None, None, 20261111)
    x, y = m.Copertura.quadretto(45.9, 8.8)
    d = c.dati()
    assert d["formato"] == 1 and d["zoom"] == 17 and abs(d["dal"] - time.time()) < 5
    celle = {(q[0], q[1]): q[2:] for q in d["celle"]}
    assert celle[(x, y)] == [2, -7.0, -5.0, -100, 1, 20261111]
    assert celle[m.Copertura.quadretto(45.95, 8.85)] == [0, None, None, None, 1, 20261111], "solo tramite altre radio"


def test_su_file_solo_quadretti_segnali_e_giorni(tmp_path):
    f = tmp_path / "copertura.json"
    c = m.Copertura(str(f))
    c.annota(45.9, 8.8, 0, -5.25, -100, 20261111)
    c.annota(45.9, 8.8, 0, -6.0, None, 20261111)
    c.salva()
    assert not f.exists(), "non subito: al massimo ogni 10 minuti"
    c.salva(subito=True)
    assert oct(f.stat().st_mode)[-3:] == "600"
    testo = f.read_text()
    assert "!" not in testo and "2222bbbb" not in testo
    d = json.loads(testo)
    assert set(d) == {"formato", "zoom", "dal", "celle"} and d["celle"][0][2:] == [2, -11.25, -5.25, -100, 0, 20261111]
    di_nuovo = m.Copertura(str(f))
    assert di_nuovo.dati() == c.dati()
    di_nuovo.annota(45.9, 8.8, 0, -1.0, -90, 20261112)
    assert di_nuovo.dati()["celle"][0][2:] == [3, -4.1, -1.0, -90, 0, 20261112]


def test_file_rovinato_si_riparte(tmp_path, caplog):
    f = tmp_path / "copertura.json"
    f.write_text('{"formato": 1, "zoom": 17, "dal": 1762800000, "celle": [[1, 2, 1, -5, -5, -100, 0, 20261111], '
                 '[1, 3, 1, -5, null, -100, 0, 20261111], [-1, 2, 1, -5, -5, -100, 0, 20261111], "x", '
                 '[1, 4, 0, 0, null, null, 2, 1999], [1, 5, 0, 0, null, null, 2, 20261111]]}')
    c = m.Copertura(str(f))
    assert [q[:2] for q in c.dati()["celle"]] == [[1, 2], [1, 5]], "solo i quadretti validi"
    f.write_text("non e' json")
    assert m.Copertura(str(f)).dati()["celle"] == []
    assert "non valido" in caplog.text


def test_al_massimo_si_tengono_i_piu_recenti():
    c = m.Copertura()
    c.MASSIMO = 20
    for i in range(20):
        c.annota(45.0 + i * 0.01, 8.0, 0, -5.0, None, 20261101 + i)
    c.annota(46.0, 8.0, 0, -5.0, None, 20261125)
    giorni = sorted(q[7] for q in c.dati()["celle"])
    assert len(giorni) == 19 and giorni[0] == 20261103 and giorni[-1] == 20261125


def test_azzera(tmp_path):
    f = tmp_path / "copertura.json"
    c = m.Copertura(str(f))
    c.annota(45.9, 8.8, 0, -5.0, None, 20261111)
    c.salva(subito=True)
    c.azzera()
    assert not f.exists() and c.dati()["celle"] == [] and c.dati()["dal"] is None


def test_limite_del_segnale_per_preset():
    assert m.limite_snr("LONG_FAST") == -17.5 and m.limite_snr("SHORT_FAST") == -7.5
    assert m.limite_snr("LONG_SLOW") == -20 and m.limite_snr("MEDIUM_FAST") == -12.5
    assert m.limite_snr("SCONOSCIUTO") == -17.5


# ---- il ponte annota le posizioni che riceve ----

def celle_di(p):
    return {(q[0], q[1]): q[2:] for q in p.copertura.dati()["celle"]}


def test_il_ponte_annota_le_posizioni_ricevute(ponte):
    p = ponte()
    p._gestisci(con_segnale(posizione(B, 45.9, 8.8), snr=-7.5, rssi=-110))
    p._gestisci(con_segnale(posizione(B, 45.9, 8.8), salti=1))                 # ripetuta da un'altra radio
    p._gestisci(con_segnale(posizione(B, 45.95, 8.85), salti=2))
    assert celle_di(p) == {m.Copertura.quadretto(45.9, 8.8): [1, -7.5, -7.5, -110, 1, oggi(p)],
                           m.Copertura.quadretto(45.95, 8.85): [0, None, None, None, 1, oggi(p)]}


def test_il_ponte_non_annota_quello_che_non_e_una_misura(ponte):
    p = ponte()
    senza_salti = posizione(B, 45.9, 8.8)
    senza_salti["rxSnr"], senza_salti["rxRssi"] = -5.0, -100
    p._gestisci(senza_salti)                                                    # firmware che non dice i salti
    p._gestisci(con_segnale(posizione(B, 45.91, 8.81), rssi=None))              # nessuna misura del segnale
    mqtt = con_segnale(posizione(B, 45.92, 8.82))
    mqtt["viaMqtt"] = True
    p._gestisci(mqtt)                                                           # arrivata da internet
    p._gestisci(con_segnale(posizione(B, 45.93, 8.83, canale=0)))              # canale pubblico
    p._gestisci(con_segnale(posizione(MIO, 45.94, 8.84)))                       # il nodo del ponte
    imprecisa = con_segnale(posizione(B, 45.95, 8.85))
    imprecisa["decoded"]["position"]["precisionBits"] = 17                      # +-182 m: sulla mappa si', qui no
    p._gestisci(imprecisa)
    assert celle_di(p) == {}
    assert len([u for u, _ in p.tak.posizioni if u == "!2222bbbb"]) >= 2, "sulla mappa TAK vanno lo stesso"
    precisa = con_segnale(posizione(B, 45.96, 8.86))
    precisa["decoded"]["position"]["precisionBits"] = 18                        # +-91 m
    p._gestisci(precisa)
    assert list(celle_di(p)) == [m.Copertura.quadretto(45.96, 8.86)]


def test_anche_le_posizioni_di_atak(ponte):
    """Le radio abbinate ad ATAK o TAK Aware: la posizione arriva dall'app, il segnale e' quello della radio."""
    p = ponte()
    p._gestisci(con_segnale(pacchetto(A, 78, golden("pli_full")), snr=2.25, rssi=-80))
    [(chiave, valori)] = celle_di(p).items()
    assert valori == [1, 2.2, 2.25, -80, 0, oggi(p)]
    ev = p.tak.posizioni[-1][1]
    import xml.etree.ElementTree as ET
    pt = ET.fromstring(ev).find("point")
    assert chiave == m.Copertura.quadretto(float(pt.get("lat")), float(pt.get("lon")))


def test_copertura_per_la_pagina(ponte, caplog):
    p = ponte()
    p.iface.nodesByNum[MIO]["position"] = {"latitudeI": 458206123, "longitudeI": 88251456}
    p._gestisci(con_segnale(posizione(B, 45.9, 8.8)))
    d = p.copertura_per_la_pagina()
    assert d["ponte"] == {"lat": 45.8206, "lon": 8.8251} and d["preset"] == "LONG_FAST" and d["limite_snr"] == -17.5
    assert len(d["celle"]) == 1
    caplog.set_level("INFO", logger="mesh2tak")
    d = p.copertura_per_la_pagina(azzera=True, chi="andrea")
    assert d["celle"] == [] and "Mappa della copertura azzerata dalla pagina della rete (andrea)" in caplog.text


# ---- la pagina del ponte: solo dal tunnel, solo per gli amministratori ----

def test_la_copertura_solo_per_gli_amministratori_dal_tunnel(gestione):
    g, _, _ = gestione()
    chiamate = []

    def copertura(azzera, chi):
        chiamate.append((azzera, chi))
        return {"formato": 1, "celle": [] if azzera else [[1, 2, 1, -5.0, -5.0, -100, 0, 20261111]]}

    def chiedi(metodo, corpo=b"", da="127.0.0.1", **intestazioni):
        h = Finto(metodo, corpo, da=da, **intestazioni)
        g._servi_copertura(h)
        testo = h.wfile.getvalue()
        return h.codice, (json.loads(testo) if testo else None)
    amministratore = {"X-Authentik-Username": "andrea"}
    assert chiedi("GET", **amministratore)[0] == 404, "ponte senza mappa della copertura"
    g.copertura = copertura
    assert chiedi("GET", **amministratore) == (200, {"formato": 1, "celle": [[1, 2, 1, -5.0, -5.0, -100, 0, 20261111]]})
    assert chiedi("GET")[0] == 403, "senza il nome messo da Authentik"
    assert chiedi("GET", da="192.168.1.50", **amministratore)[0] == 403, "dal Wi-Fi del posto"
    assert chiedi("PUT", **amministratore)[0] == 405
    azzera = json.dumps({"azzera": True}).encode()
    pagina = {"X-Mesh2tak": "1", "Origin": "https://meshtastic.prova.lan", "X-Forwarded-Host": "meshtastic.prova.lan"}
    assert chiedi("POST", azzera, **amministratore)[0] == 403, "non dalla pagina"
    assert chiedi("POST", azzera, **amministratore, **dict(pagina, Origin="https://cattivo.example"))[0] == 403
    assert chiedi("POST", b"{}", **amministratore, **pagina)[0] == 400
    assert chiedi("POST", b"x" * 2000, **amministratore, **pagina)[0] == 413
    assert chiedi("POST", azzera, **amministratore, **pagina) == (200, {"formato": 1, "celle": []})
    assert chiamate == [(False, "andrea"), (True, "andrea")]


def test_il_ponte_salva_la_copertura(ponte, tmp_path):
    p = ponte()
    p.copertura = m.Copertura(str(tmp_path / "copertura.json"))
    p._gestisci(con_segnale(posizione(B, 45.9, 8.8)))
    p.copertura.SALVA_OGNI_S = 0.0
    p.manutenzione()
    assert json.loads((tmp_path / "copertura.json").read_text())["celle"][0][2] == 1
    assert os.stat(tmp_path / "copertura.json").st_mode & 0o077 == 0
