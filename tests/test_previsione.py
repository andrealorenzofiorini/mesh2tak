"""Copertura prevista (1.17.0): la pagina la calcola dal rilievo per ogni gateway e ripetitore, con i dati veri del
suo impianto, e la corregge con le misure. Qui: i conti da soli (in node, senza browser), il rilievo tenuto dal
servizio della pagina, i dati degli impianti scritti dagli amministratori, e quello che il gateway dice della sua
radio."""
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import zlib

import pytest

from conftest import RADICE
from http_finto import ServerFinto
from test_ponte_logica import IfaceFinta, ponte, sdk  # noqa: F401  (fixture)
from test_servizio_rete import http_, sv  # noqa: F401  (fixture)

sys.path.insert(0, os.path.join(RADICE, "server"))
import rete_meshtastic as rete  # noqa: E402


def terrarium(quota=lambda colonna, riga: 300.0):
    """Un riquadro del rilievo come quelli veri: PNG 256 x 256, la quota (metri) nei tre colori."""
    righe = bytearray()
    for r in range(256):
        righe.append(0)
        for c in range(256):
            v = int(round((quota(c, r) + 32768) * 256))
            righe += bytes((v >> 16, (v >> 8) & 255, v & 255))

    def pezzo(tipo, dati):
        return struct.pack(">I", len(dati)) + tipo + dati + struct.pack(">I", zlib.crc32(tipo + dati) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + pezzo(b"IHDR", struct.pack(">IIBBBBB", 256, 256, 8, 2, 0, 0, 0)) +
            pezzo(b"IDAT", zlib.compress(bytes(righe))) + pezzo(b"IEND", b""))


PIANURA = terrarium()
# per le prove con il browser: in mezzo alla pianura un colle (il "Monte Prova", 350 m sopra, largo 1,2 km) a 4 km dal
# gateway di prova, con il suo nome e un traliccio su OpenStreetMap, e un rifugio in piano
COLLE = (45.80, 8.87, 350.0, 1200.0)
_riquadri_di_prova = {}


def rilievo_di_prova(percorso):
    m = re.match(r"^/(\d+)/(\d+)/(\d+)\.png$", percorso)
    if not m:
        return None
    z, x, y = (int(v) for v in m.groups())
    if z != 11:
        return PIANURA
    if (x, y) not in _riquadri_di_prova:
        lat, lon, alto, largo = COLLE
        n = 256 * 2 ** z
        cx = (lon + 180) / 360 * n
        cy = (0.5 - math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) / (2 * math.pi)) * n
        r = largo / (math.cos(math.radians(lat)) * 40075017 / n)
        if cx + r < x * 256 or cx - r > x * 256 + 256 or cy + r < y * 256 or cy - r > y * 256 + 256:
            _riquadri_di_prova[(x, y)] = PIANURA
        else:
            _riquadri_di_prova[(x, y)] = terrarium(lambda c, g: 300 + alto * max(
                0.0, 1 - math.hypot(x * 256 + c + 0.5 - cx, y * 256 + g + 0.5 - cy) / r))
    return _riquadri_di_prova[(x, y)]


LUOGHI_DI_PROVA = [
    {"type": "node", "id": 1, "lat": 45.8001, "lon": 8.8702, "tags": {"natural": "peak", "name": "Monte Prova", "ele": "652"}},
    {"type": "way", "id": 2, "center": {"lat": 45.8004, "lon": 8.8706}, "tags": {"man_made": "mast"}},
    {"type": "node", "id": 3, "lat": 45.83, "lon": 8.9, "tags": {"tourism": "alpine_hut", "name": "Rifugio Piano"}},
    {"type": "node", "id": 4, "lat": 45.81, "lon": 8.81, "tags": {"man_made": "tower"}},          # una torre qualsiasi: no
]


def osm_di_prova(percorso):
    """Fa la parte di Overpass: risponde con i luoghi di prova che cadono nel quadrato chiesto."""
    domanda = urllib.parse.parse_qs(urllib.parse.urlsplit(percorso).query).get("data", [""])[0]
    m = re.search(r'node\["natural"="peak"\]\(([-0-9.]+),([-0-9.]+),([-0-9.]+),([-0-9.]+)\)', domanda)
    if not m:
        return None
    s, o, n, e = (float(v) for v in m.groups())
    dentro = [x for x in LUOGHI_DI_PROVA
              if s <= (x.get("center") or x)["lat"] < n and o <= (x.get("center") or x)["lon"] < e]
    return json.dumps({"version": 0.6, "elements": dentro}).encode()


# ---- i conti, da soli ----

@pytest.mark.skipif(not shutil.which("node"), reason="serve node")
def test_il_calcolo_da_solo():
    p = subprocess.run(["node", os.path.join(os.path.dirname(__file__), "prova_previsione.js")], capture_output=True,
                       text=True, timeout=300)
    r = json.loads(p.stdout.strip().splitlines()[-1])
    assert "errore" not in r, r
    # i conti di base, confrontati con valori calcolati a mano
    assert r["rumore"] == -114.02 and r["spazioLibero"] == 111.24
    assert r["lama"] == [0, 0, 6.03, 13.93, 20.54], "ITU-R P.526: 6 dB a filo, niente sotto -0,78"
    assert r["radio"] == {"banda": 250, "frequenza": 869.5, "limite": -17.5, "lambda": 0.3448}
    assert r["radioIgnota"] == 869.5, "regione non nota: quella europea"
    assert r["unOstacolo"][0] == r["unOstacolo"][1] and r["unOstacolo"][2] == 20.57, r["unOstacolo"]
    assert r["pianura"][0] > 3 and r["pianura"][1] is True, "antenne basse: toglie qualcosa; lontano la Terra fa da ostacolo"
    # il rilievo: letto dal PNG byte per byte, con ogni filtro, RGB e RGBA; un file rotto si dice
    assert r["png"] == [[200, 837.5, 901.25, 317.5]] * 5 and r["pngRgba"] == 317.5, r["png"]
    assert r["pngRotto"] == ["il riquadro del rilievo non è un PNG", "riquadro del rilievo troncato",
                             "riquadro del rilievo di misura non prevista"]
    assert r["riquadri"] == 4 and r["suolo"][0] == 300 and 600 < r["suolo"][1] <= 800 and r["suolo"][2] is True
    # un impianto: pianura e un crinale a nord
    assert 2400 < r["quadretti"] < 2600 and r["suoloNodo"] == 300, "6 km di raggio, quadretti da 213 m"
    assert r["sud"][3] == 1.9 and 8 < r["sud"][2] < 18 and abs(r["sud"][0] - (r["sud"][1] - r["sud"][2])) <= 0.1, r["sud"]
    assert r["nord"] == [True, True, 4.9], "dietro al crinale il rilievo toglie piu' di 25 dB"
    assert r["versi"] == [0], "stessa potenza ai due capi: i due versi sono uguali"
    assert r["antennaAlta"][0] is True and r["antennaAlta"][1] == 0 and r["antennaAlta"][2] == r["antennaAlta"][3], \
        "antenna a 60 m: in pianura il percorso e' libero e il margine e' quello dello spazio libero"
    assert r["piuPotenza"] == [5, 0], "piu' potenza al gateway: migliora solo il verso verso la radio, non il margine"
    assert r["correzione"] == 14 and r["senzaRilievo"] == "manca il rilievo nel punto dell'impianto"
    assert r["traImpianti"][0] is True and r["traImpianti"][3] is True and r["traImpianti"][2] == 2.2, r["traImpianti"]
    # la taratura: il vero era 14 dB a 1 km e b = 1,5 (29 dB a 10 km), dispersione 5
    t = r["taratura"]
    assert abs(t["a"] - 14) <= 1.5 and abs(t["b"] - 1.5) <= 0.3 and abs(t["errore"] - 5) <= 1, t
    assert t["saturi"] > 20 and t["nonSentiti"] > 20 and t["quadretti"] == t["esatti"] + t["saturi"] + t["nonSentiti"], t
    assert abs(r["correzione10km"] - 29) <= 2.5, r["correzione10km"]
    assert r["ingenua"][0] > t["a"] + 3 and r["ingenua"][1] < t["b"] - 0.5, \
        "buttando i quadretti saturi e quelli non sentiti la taratura sbaglia: per questo si tengono"
    assert r["taraturaConStrani"] == [t["a"], t["b"]], "sei quadretti strani non la spostano"
    assert r["fascia"][0] > 50 and abs(r["fascia"][2] - r["fascia"][3]) <= 1.5 and 0.5 <= r["fascia"][1] <= 1.75, r["fascia"]
    assert r["pochi"] == [None, 4]
    # dalla 1.18.0: un quadretto ogni due per confrontare tanti siti, le cime, il profilo
    assert r["rado"] == [2, True, True], "un quarto dei quadretti, con gli stessi valori"
    assert r["cime"][0] == [30, 20, 645, 336] and r["cime"][1][:2] == [-25, 28], "i due cocuzzoli, il piu' alto per primo"
    assert all(abs(x - 5) <= 6 and abs(y + 30) <= 6 and q == 400 for x, y, q, _ in r["cime"][2:]) and len(r["cime"]) <= 5, \
        "il pianoro: pochi punti, non uno per ogni punto del pianoro"
    assert r["cimeLatLon"] == [True, True] and r["unaSola"] == 1 and r["pianura2"] == 0 and r["fuori"] == 0
    p = r["profilo"]
    assert p["km"] == 5 and p["antenne"] == [305, 303] and p["primo"] == [0, 5] and p["ostacolo"] == [3, True], p
    assert p["ostacoli"] is True and 0.3 < p["curvatura"] < 0.45 and p["libero"] is True, "a meta' di 5 km la Terra sale 37 cm"
    assert r["profiloLibero"] == [None, 0, True] and r["profiloSenzaRilievo"] is None


# ---- il rilievo: lo prende il server, una volta, e lo tiene ----

@pytest.fixture
def sorgente(monkeypatch):
    s = ServerFinto({"*": lambda percorso: PIANURA if percorso.startswith("/11/") else None})
    monkeypatch.setattr(rete, "TERRENO_SORGENTE", s.url)
    yield s
    if not getattr(s, "fermato", False):
        s.ferma()


def test_riquadro_preso_una_volta_e_poi_tenuto(tmp_path, sorgente):
    t = rete.Terreno(str(tmp_path / "terreno"))
    assert t.riquadro(11, 1074, 730) == (200, PIANURA)
    assert t.riquadro(11, 1074, 730) == (200, PIANURA) and sorgente.richieste == ["/11/1074/730.png"]
    f = tmp_path / "terreno" / "11-1074-730.png"
    assert f.read_bytes() == PIANURA and oct(f.stat().st_mode)[-3:] == "600"
    assert oct((tmp_path / "terreno").stat().st_mode)[-3:] == "700"
    # un altro servizio (dopo un riavvio) trova il file e non chiede niente
    assert rete.Terreno(str(tmp_path / "terreno")).riquadro(11, 1074, 730)[0] == 200 and len(sorgente.richieste) == 1


def test_solo_riquadri_che_esistono(tmp_path, sorgente):
    t = rete.Terreno(str(tmp_path / "terreno"))
    for z, x, y in ((8, 1, 1), (13, 1, 1), (11, 2048, 0), (11, 0, 2048)):
        assert t.riquadro(z, x, y) == (404, b""), (z, x, y)
    assert sorgente.richieste == [], "fuori dai livelli previsti o dal mondo non si chiede niente alla sorgente"


def test_sorgente_che_non_risponde_o_risponde_altro(tmp_path, sorgente):
    t = rete.Terreno(str(tmp_path / "terreno"))
    assert t.riquadro(12, 5, 5) == (502, b""), "la sorgente dice 404"
    sorgente.risposte["/10/5/5.png"] = b"<html>pagina di errore</html>"
    assert t.riquadro(10, 5, 5) == (502, b"") and not (tmp_path / "terreno" / "10-5-5.png").exists()
    sorgente.risposte["/10/6/6.png"] = b"\x89PNG\r\n\x1a\n" + b"x" * (rete.Terreno.PESO_MAX + 1)
    assert t.riquadro(10, 6, 6) == (502, b""), "troppo grande"
    sorgente.ferma()
    sorgente.fermato = True
    assert t.riquadro(11, 7, 7) == (502, b""), "sorgente spenta"
    # un file rovinato in cartella non si da': si riprende
    (tmp_path / "terreno").mkdir(exist_ok=True)
    (tmp_path / "terreno" / "11-8-8.png").write_bytes(b"rovinato")
    assert t.riquadro(11, 8, 8) == (502, b"")


def test_limite_ai_riquadri_nuovi(tmp_path, sorgente):
    ora = [1000.0]
    t = rete.Terreno(str(tmp_path / "terreno"), ora=lambda: ora[0])
    t.NUOVI_OGNI_10_MIN = 3
    assert [t.riquadro(11, x, 1)[0] for x in range(5)] == [200, 200, 200, 429, 429]
    assert t.riquadro(11, 0, 1)[0] == 200, "quelli gia' presi non contano"
    assert len(sorgente.richieste) == 3
    ora[0] += 601
    assert t.riquadro(11, 4, 1)[0] == 200, "dieci minuti dopo si puo' di nuovo"


def test_due_richieste_insieme_un_solo_scarico(tmp_path, monkeypatch):
    via = threading.Event()

    def lenta(percorso):
        via.wait(5)
        return PIANURA
    s = ServerFinto({"*": lenta})
    monkeypatch.setattr(rete, "TERRENO_SORGENTE", s.url)
    t = rete.Terreno(str(tmp_path / "terreno"))
    esiti = []
    fili = [threading.Thread(target=lambda: esiti.append(t.riquadro(11, 3, 3)[0])) for _ in range(4)]
    for f in fili:
        f.start()
    via.set()
    for f in fili:
        f.join(15)
    s.ferma()
    assert esiti == [200] * 4 and s.richieste == ["/11/3/3.png"]


def test_la_cartella_non_cresce_senza_fine(tmp_path, sorgente):
    t = rete.Terreno(str(tmp_path / "terreno"))
    t.RIQUADRI_MAX = 120
    for x in range(125):
        f = tmp_path / "terreno" / f"11-{x}-9.png"
        f.parent.mkdir(exist_ok=True)
        f.write_bytes(PIANURA)
        os.utime(f, (1000 + x, 1000 + x))
    assert t.riquadro(11, 500, 9)[0] == 200
    rimasti = sorted(os.listdir(tmp_path / "terreno"))
    assert len(rimasti) == 20 and "11-500-9.png" in rimasti and "11-0-9.png" not in rimasti, "via i piu' vecchi"


def test_il_rilievo_dalla_pagina_solo_per_gli_amministratori(http_, sv, sorgente):
    def chiedi(percorso, **intestazioni):
        rq = urllib.request.Request(f"http://127.0.0.1:{http_.porta}{percorso}", headers=intestazioni)
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(rq, timeout=10) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)
    assert chiedi("/api/terreno/11/1074/730.png")[0] == 403 and sorgente.richieste == []
    codice, corpo, intestazioni = chiedi("/api/terreno/11/1074/730.png", **{"X-Authentik-Username": "maria"})
    assert codice == 200 and corpo == PIANURA and intestazioni["Content-Type"] == "image/png"
    assert intestazioni["Cache-Control"] == "private, max-age=2592000, immutable", "il rilievo non cambia: il browser lo tiene"
    codice, corpo, _ = chiedi("/api/terreno/7/1/1.png", **{"X-Authentik-Username": "maria"})
    assert codice == 404 and json.loads(corpo) == {"errore": "riquadro fuori dalla mappa"}
    for strano in ("/api/terreno/11/1074/730.jpg", "/api/terreno/11/../730.png", "/api/terreno/11/1074/730.png/x",
                   "/api/terreno/11/-1/730.png", "/api/terreno/11/1074.5/730.png"):
        assert chiedi(strano, **{"X-Authentik-Username": "maria"})[0] == 404, strano
    assert os.listdir(rete.P(rete.TERRENO_DIR)) == ["11-1074-730.png"]


# ---- cime, tralicci e rifugi da OpenStreetMap (1.18.0) ----

@pytest.fixture
def osm(monkeypatch):
    s = ServerFinto({"*": osm_di_prova})
    monkeypatch.setattr(rete, "LUOGHI_SORGENTI", [s.url])
    yield s
    if not getattr(s, "fermato", False):
        s.ferma()


def test_luoghi_chiesti_una_volta_e_tenuti_tre_mesi(tmp_path, osm):
    ora = [1_000_000.0]
    luoghi = rete.Luoghi(str(tmp_path / "luoghi"), ora=lambda: ora[0])
    codice, d = luoghi.quadrato(183, 35)          # da 45,75 a 46 nord, da 8,75 a 9 est
    assert codice == 200 and d["cime"] == [[45.8001, 8.8702, "Monte Prova", 652]]
    assert d["sostegni"] == [[45.8004, 8.8706, "traliccio", ""], [45.83, 8.9, "rifugio", "Rifugio Piano"]], "la torre qualsiasi no"
    assert d["quando"] == 1_000_000 and len(osm.richieste) == 1
    domanda = urllib.parse.parse_qs(urllib.parse.urlsplit(osm.richieste[0]).query)["data"][0]
    assert domanda.startswith('[out:json][timeout:60];(node["natural"="peak"](45.75,8.75,46,9);') and \
        'nwr["tourism"="alpine_hut"](45.75,8.75,46,9);' in domanda and domanda.endswith(");out center tags;")
    f = tmp_path / "luoghi" / "183_35.json"
    assert oct(f.stat().st_mode)[-3:] == "600" and oct((tmp_path / "luoghi").stat().st_mode)[-3:] == "700"
    ora[0] += 89 * 86400
    assert luoghi.quadrato(183, 35)[0] == 200 and len(osm.richieste) == 1, "per tre mesi non si richiede"
    assert rete.Luoghi(str(tmp_path / "luoghi"), ora=lambda: ora[0]).quadrato(183, 35)[1] == d and len(osm.richieste) == 1
    ora[0] += 2 * 86400
    assert luoghi.quadrato(183, 35)[1]["quando"] == int(ora[0]) and len(osm.richieste) == 2, "dopo si rinfresca"
    # un quadrato vuoto e' una risposta buona (e si tiene); fuori dal mondo non si chiede niente
    assert luoghi.quadrato(100, 100) == (200, {"formato": 1, "quando": int(ora[0]), "cime": [], "sostegni": []})
    for la, lo in ((360, 0), (-361, 0), (0, 720), (0, -721)):
        assert luoghi.quadrato(la, lo) == (404, {})
    assert len(osm.richieste) == 3


def test_luoghi_quando_openstreetmap_non_risponde(tmp_path, osm, monkeypatch):
    ora = [1_000_000.0]
    luoghi = rete.Luoghi(str(tmp_path / "luoghi"), ora=lambda: ora[0])
    assert luoghi.quadrato(183, 35)[0] == 200
    osm.risposte["*"] = lambda percorso: b"<html>servizio occupato</html>"
    assert luoghi.quadrato(184, 35) == (502, {}), "risposta che non e' quella attesa, e niente di tenuto"
    ora[0] += 100 * 86400
    codice, d = luoghi.quadrato(183, 35)
    assert codice == 200 and d["quando"] == 1_000_000, "scaduti, ma OpenStreetMap non risponde: meglio vecchi che niente"
    osm.risposte["*"] = lambda percorso: json.dumps({"elements": "tanti"}).encode()
    assert luoghi.quadrato(185, 35) == (502, {})
    # la seconda sorgente, se la prima non va
    spenta = ServerFinto({})
    spenta.ferma()
    osm.risposte["*"] = osm_di_prova
    monkeypatch.setattr(rete, "LUOGHI_SORGENTI", [spenta.url, osm.url])
    assert luoghi.quadrato(183, 36)[0] == 200


def test_luoghi_limite_e_pulizia(tmp_path, osm):
    ora = [1000.0]
    luoghi = rete.Luoghi(str(tmp_path / "luoghi"), ora=lambda: ora[0])
    luoghi.NUOVI_OGNI_10_MIN = 3
    assert [luoghi.quadrato(la, 35)[0] for la in range(180, 185)] == [200, 200, 200, 429, 429]
    assert luoghi.quadrato(180, 35)[0] == 200 and len(osm.richieste) == 3, "quelli tenuti non contano"
    ora[0] += 601
    assert luoghi.quadrato(184, 35)[0] == 200
    luoghi.QUADRATI_MAX = 60
    for i in range(70):
        f = tmp_path / "luoghi" / f"{i}_0.json"
        f.write_text('{"quando": 1, "cime": [], "sostegni": []}')
        os.utime(f, (500 + i, 500 + i))
    ora[0] += 601
    assert luoghi.quadrato(185, 35)[0] == 200
    rimasti = os.listdir(tmp_path / "luoghi")
    assert len(rimasti) < 60 and "185_35.json" in rimasti and "0_0.json" not in rimasti


def test_quello_che_arriva_da_openstreetmap_si_controlla():
    cime, sostegni = rete.luoghi_da_osm({"elements": [
        {"type": "node", "lat": 45.8, "lon": 8.87, "tags": {"natural": "peak", "name": " Monte\x07  <b>Prova</b> " + "x" * 80, "ele": "1 226"}},
        {"type": "node", "lat": 45.8, "lon": 8.87, "tags": {"natural": "peak", "ele": "1226.4 m"}},
        {"type": "node", "lat": "45.8", "lon": 8.87, "tags": {"natural": "peak"}},
        {"type": "node", "lat": 95, "lon": 8.87, "tags": {"natural": "peak"}},
        {"type": "node", "lat": True, "lon": 8.87, "tags": {"natural": "peak"}},
        {"type": "node", "lat": float("nan"), "lon": 8.87, "tags": {"natural": "peak"}},
        {"type": "way", "tags": {"man_made": "mast"}}, {"type": "way", "center": [45, 8], "tags": {"man_made": "mast"}},
        {"type": "relation", "center": {"lat": 45.9, "lon": 8.9}, "tags": {"man_made": "water_tower", "name": 7}},
        {"type": "node", "lat": 45.9, "lon": 8.9, "tags": {"man_made": "tower", "tower:type": "communication"}},
        {"type": "node", "lat": 45.9, "lon": 8.9, "tags": {"man_made": "communications_tower"}},
        {"type": "node", "lat": 45.9, "lon": 8.9, "tags": "x"}, "x", None]})
    assert cime == [[45.8, 8.87, ("Monte <b>Prova</b> " + "x" * 80)[:60], None], [45.8, 8.87, "", 1226]], cime
    assert sostegni == [[45.9, 8.9, "acqua", ""], [45.9, 8.9, "torre", ""], [45.9, 8.9, "torre", ""]]
    assert rete.luoghi_da_osm({}) == ([], []) and rete.luoghi_da_osm({"elements": 3}) == ([], [])


def test_i_luoghi_dalla_pagina_solo_per_gli_amministratori(http_, sv, osm):
    def chiedi(percorso, **intestazioni):
        rq = urllib.request.Request(f"http://127.0.0.1:{http_.porta}{percorso}", headers=intestazioni)
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(rq, timeout=10) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)
    assert chiedi("/api/luoghi/183/35.json")[0] == 403 and osm.richieste == []
    codice, corpo, intestazioni = chiedi("/api/luoghi/183/35.json", **{"X-Authentik-Username": "maria"})
    d = json.loads(corpo)
    assert codice == 200 and d["cime"][0][2] == "Monte Prova" and len(d["sostegni"]) == 2
    assert intestazioni["Content-Type"] == "application/json; charset=utf-8" and intestazioni["Cache-Control"] == "private, max-age=86400"
    assert chiedi("/api/luoghi/999/35.json", **{"X-Authentik-Username": "maria"})[0] == 404
    for strano in ("/api/luoghi/183/35.txt", "/api/luoghi/183.5/35.json", "/api/luoghi/183/35.json/x", "/api/luoghi/../35.json"):
        assert chiedi(strano, **{"X-Authentik-Username": "maria"})[0] == 404, strano


# ---- i dati degli impianti ----

def test_dati_di_un_impianto(sv):
    codice, r = sv.azione("/api/impianto", {"chi": "g:casa", "altezza_m": 12, "guadagno_dbi": 5.8, "perdite_db": 1.5,
                                            "potenza_dbm": None}, "maria")
    assert codice == 200 and r["messaggio"] == "Dati dell'impianto salvati."
    voce = r["impianti"]["g:casa"]
    assert {k: voce[k] for k in ("altezza_m", "guadagno_dbi", "perdite_db")} == {"altezza_m": 12.0, "guadagno_dbi": 5.8,
                                                                                 "perdite_db": 1.5}
    assert "potenza_dbm" not in voce and voce["da"] == "maria" and voce["quando"] > 0
    assert oct(os.stat(rete.P(rete.IMPIANTI)).st_mode)[-3:] == "600"
    assert sv.azione("/api/impianto", {"chi": "r:!3333cccc", "altezza_m": 3.04, "potenza_dbm": 27}, "maria")[0] == 200
    assert sv.azione("/api/impianto", {"chi": "portatile", "guadagno_dbi": -2}, "maria")[0] == 200
    stato = sv.stato("maria")["impianti"]
    assert sorted(stato) == ["g:casa", "portatile", "r:!3333cccc"] and stato["r:!3333cccc"]["altezza_m"] == 3.0
    # tutto vuoto: tornano i valori di fabbrica
    codice, r = sv.azione("/api/impianto", {"chi": "g:casa", "altezza_m": "", "guadagno_dbi": None}, "maria")
    assert codice == 200 and "g:casa" not in r["impianti"] and r["messaggio"] == "Tornati i valori di fabbrica."


@pytest.mark.parametrize("dati", [
    {"altezza_m": 5}, {"chi": "casa", "altezza_m": 5}, {"chi": "g:Casa", "altezza_m": 5}, {"chi": "r:3333cccc", "altezza_m": 5},
    {"chi": "r:!3333CCCC", "altezza_m": 5}, {"chi": "g:casa/../x", "altezza_m": 5}, {"chi": ["g:casa"], "altezza_m": 5},
    {"chi": "g:casa", "altezza_m": 0.2}, {"chi": "g:casa", "altezza_m": 500}, {"chi": "g:casa", "altezza_m": "alta"},
    {"chi": "g:casa", "altezza_m": True}, {"chi": "g:casa", "guadagno_dbi": 25}, {"chi": "g:casa", "perdite_db": -1},
    {"chi": "g:casa", "potenza_dbm": 31}, {"chi": "g:casa", "potenza_dbm": float("nan")}, {"chi": "g:casa", "altezza_m": [5]}])
def test_dati_di_un_impianto_non_validi(sv, dati):
    assert sv.azione("/api/impianto", dati, "maria")[0] == 400, dati
    assert sv.stato("maria")["impianti"] == {}


def test_impianti_file_rovinato_e_troppi(sv, monkeypatch):
    os.makedirs(os.path.dirname(rete.P(rete.IMPIANTI)), exist_ok=True)
    for brutto in ("non json", "[]", '{"impianti": []}', '{"impianti": {"altro": {"altezza_m": 5}, "g:casa": 7}}'):
        open(rete.P(rete.IMPIANTI), "w").write(brutto)
        assert rete.leggi_impianti() == {}, brutto
    monkeypatch.setattr(rete, "IMPIANTI_MAX", 2)
    assert sv.azione("/api/impianto", {"chi": "g:uno", "altezza_m": 5}, "maria")[0] == 200
    assert sv.azione("/api/impianto", {"chi": "g:due", "altezza_m": 5}, "maria")[0] == 200
    assert sv.azione("/api/impianto", {"chi": "g:tre", "altezza_m": 5}, "maria")[0] == 409
    assert sv.azione("/api/impianto", {"chi": "g:due", "altezza_m": 8}, "maria")[0] == 200, "cambiarne uno si puo'"


# ---- il gateway dice la potenza e la regione vere della sua radio ----

def test_il_gateway_dice_potenza_e_regione(ponte):
    p = ponte()
    assert p.copertura_per_la_pagina()["radio"] == {"potenza_dbm": None, "regione": "EU_868"}, \
        "potenza 0 sulla radio vuol dire il massimo della regione: non si inventa un numero"
    iface = IfaceFinta()
    iface.localNode.localConfig.lora.tx_power = 17
    p.collega(iface)
    assert p.copertura_per_la_pagina()["radio"] == {"potenza_dbm": 17, "regione": "EU_868"}
    iface.localNode.localConfig.lora.tx_power = 99
    p.collega(iface)
    assert p.copertura_per_la_pagina()["radio"]["potenza_dbm"] is None, "un valore impossibile non si passa"
