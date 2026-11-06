"""Test delle parti pure del ponte: precisione, CoT, decodifica TAK v2, configurazione."""
import glob
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from conftest import SDK, scrivi_config
import mesh2tak as m
GOLDEN = sorted(glob.glob(f"{SDK}/testdata/golden/*.bin"))


def sfoca(v, bit):
    maschera = (0xFFFFFFFF << (32 - bit)) & 0xFFFFFFFF
    u = ((v & 0xFFFFFFFF) & maschera) + (1 << (31 - bit))
    return u - (1 << 32) if u >= (1 << 31) else u


@pytest.mark.parametrize("lat,lon", [(45.8205234, 8.8226234), (-33.1234567, -107.2534321),
                                     (45.8, 8.8), (45.0, 9.0), (0.0000001, 0.0000001)])
def test_coordinate_precise_restano_precise(lat, lon):
    assert m.bit_precisione(int(lat * 1e7), int(lon * 1e7)) == 32


@pytest.mark.parametrize("bit", range(10, 24))
@pytest.mark.parametrize("lat,lon", [(45.8205234, 8.8226234), (-33.1234567, -107.2534321)])
def test_precisione_ricavata_dalle_coordinate(bit, lat, lon):
    lat_i, lon_i = sfoca(int(lat * 1e7), bit), sfoca(int(lon * 1e7), bit)
    assert m.bit_precisione(lat_i, lon_i) == bit


def test_precisione_dichiarata_ha_la_priorita():
    assert m.bit_precisione(458205234, 88226234, 16) == 16
    assert m.bit_precisione(458205234, 88226234, 32) == 32


def test_raggio_errore_come_tabella_meshtastic():
    attesi = {10: 23300, 13: 2900, 16: 364, 17: 182, 19: 45}
    for bit, metri in attesi.items():
        assert abs(m.errore_da_bit(bit) - metri) / metri < 0.03


def test_cot_valido_e_ripulito():
    xml = m.cot_evento(uid="!1111aaaa", tipo="a-f-G-U-C", how="m-g", lat=45.82, lon=8.82,
                       t_evento=1_790_000_000, stale=1_790_005_400, hae=1097.0,
                       callsign="07-BIANCHI <&> \x01\x07😀", squadra="Cyan", ruolo="Team Member",
                       batteria=87, rotta=90.0, velocita=1.1, geosrc="GPS", altsrc="GPS",
                       note="prova & verifica")
    testo = xml.decode("utf-8")
    assert testo.startswith('<?xml version="1.0" encoding="UTF-8"?><event ')
    ev = ET.fromstring(xml)
    assert ev.get("uid") == "!1111aaaa" and ev.get("type") == "a-f-G-U-C"
    assert ev.get("stale") == "2026-09-21T15:43:20.000Z"
    c = ev.find("detail/contact")
    assert c.get("callsign") == "07-BIANCHI <&> 😀" and c.get("endpoint") is None
    assert ev.find("detail/__group").attrib == {"name": "Cyan", "role": "Team Member"}
    assert ev.find("detail/status").get("battery") == "87"
    assert ev.find("detail/remarks").text == "prova & verifica"
    assert float(ev.find("point").get("hae")) == 1097.0


def test_ping_takserver():
    ev = ET.fromstring(m.cot_ping("mesh2tak", 1_790_000_000))
    assert ev.get("type") == "t-x-c-t"


@pytest.fixture(scope="module")
def sdk():
    import meshtastic  # noqa: F401  (come nel servizio: prima la libreria della radio)
    return m.LibreriaTak()


def test_libreria_tak_accanto_alla_libreria_meshtastic(sdk):
    """Le due librerie registrano la stessa annotazione protobuf: il caricamento separato le fa convivere."""
    from meshtastic.protobuf import atak_pb2 as mt
    assert sdk.pb.TAKPacketV2 is not mt.TAKPacketV2
    assert mt.TAKPacketV2(callsign="x").callsign == "x"  # la libreria della radio funziona ancora
    sdk.verifica()


@pytest.mark.parametrize("percorso", GOLDEN, ids=[os.path.basename(p) for p in GOLDEN])
def test_decodifica_identica_al_golden_ufficiale(sdk, percorso):
    atteso = open(percorso.replace("/golden/", "/protobuf/")[:-4] + ".pb", "rb").read()
    pkt = sdk.decodifica(open(percorso, "rb").read())
    assert pkt == sdk.pb.TAKPacketV2.FromString(atteso)


XML_SDK = sorted(glob.glob(f"{SDK}/testdata/cot_xml/*.xml"))


@pytest.mark.parametrize("percorso", XML_SDK, ids=[os.path.basename(p) for p in XML_SDK])
def test_andata_e_ritorno_di_ogni_esempio_ufficiale(sdk, percorso):
    """CoT -> pacchetto radio (entro 225 byte) -> CoT, con lo stesso UID e lo stesso tipo."""
    xml = open(percorso, encoding="utf-8").read()
    pkt, filo = sdk.da_xml(xml)
    assert 0 < len(filo) <= m.MAX_FILO
    indietro = ET.fromstring(sdk.in_xml(sdk.decodifica(filo)))
    originale = ET.fromstring(xml.encode())
    assert indietro.get("uid") == originale.get("uid")
    assert indietro.get("type") == originale.get("type")


def test_pacchetto_non_compresso(sdk):
    pkt = sdk.pb.TAKPacketV2(callsign="X", latitude_i=1, longitude_i=2,
                             cot_type_id=sdk.pb.CotType.CotType_a_f_G_U_C)
    assert sdk.decodifica(b"\xff" + pkt.SerializeToString()).callsign == "X"


@pytest.mark.parametrize("carico", [b"\x00garbage-garbage", b"\x05abc", b"", b"\x00"])
def test_pacchetti_illeggibili(sdk, carico):
    with pytest.raises(m.PacchettoIllegibile):
        sdk.decodifica(carico)


def test_impronta_non_dipende_dalla_scadenza(sdk):
    xml = open(f"{SDK}/testdata/cot_xml/drawing_polygon.xml", encoding="utf-8").read()
    a, _ = sdk.da_xml(xml)
    b, _ = sdk.da_xml(m.allunga_stale(xml, 1_790_000_000, 3600 * 30))
    assert a.stale_seconds != b.stale_seconds
    assert sdk.impronta(a) == sdk.impronta(b)
    c = sdk.pb.TAKPacketV2()
    c.CopyFrom(a)
    c.callsign = "Altro poligono"
    assert sdk.impronta(c) != sdk.impronta(a)


def poligono(n, uid="poligono-grande", creatore=("ANDROID-0000000000000007", "07-BIANCHI")):
    import math as mt
    punti = "".join(f'<link point="{45.8 + 0.01 * mt.sin(i / n * 6.283) * (1 + (i % 7) / 9):.7f},'
                    f'{8.8 + 0.01 * mt.cos(i / n * 6.283) * (1 + (i % 5) / 7):.7f}"/>' for i in range(n))
    return (f'<event version="2.0" uid="{uid}" type="u-d-f" how="h-e" time="2026-11-05T10:00:00Z" '
            f'start="2026-11-05T10:00:00Z" stale="2099-11-06T10:00:00Z"><point lat="45.8" lon="8.8" hae="0" '
            f'ce="9999999" le="9999999"/><detail>{punti}<contact callsign="Area di ricerca"/>'
            f'<link uid="{creatore[0]}" type="a-f-G-U-C" parent_callsign="{creatore[1]}" relation="p-p"/>'
            f'<strokeColor value="-65536"/><strokeWeight value="3.0"/></detail></event>')


def test_forma_troppo_grande(sdk):
    with pytest.raises(m.OggettoTroppoGrande):
        sdk.da_xml(poligono(150))
    _, filo = sdk.da_xml(poligono(12))
    assert len(filo) <= m.MAX_FILO


def test_tipi_cot_come_sdk_ufficiale(sdk):
    """La conversione enum -> tipo CoT coincide con CotTypeMapper di TAKPacket-SDK."""
    atak_pb2 = sdk.pb
    codice = ("import json,sys;sys.modules['zstandard']=type(sys)('zstandard');"
              "from meshtastic_tak import atak_pb2;from meshtastic_tak.cot_type_mapper import CotTypeMapper as M;"
              "print(json.dumps({v.number: M.type_to_string(v.number) for v in atak_pb2.CotType.DESCRIPTOR.values}))")
    uscita = subprocess.run([sys.executable, "-c", codice], capture_output=True, text=True, check=True,
                            env=dict(os.environ, PYTHONPATH=f"{SDK}/python/src"))
    sdk = {int(k): v for k, v in json.loads(uscita.stdout).items()}
    for num, atteso in sdk.items():
        if num in (0, 126):  # 0 = "altro" (stringa a parte), 126 = "y-" (non e' una posizione)
            continue
        pkt = atak_pb2.TAKPacketV2(cot_type_id=num)
        assert m.tipo_cot_v2(atak_pb2, pkt) == atteso
    assert m.tipo_cot_v2(atak_pb2, atak_pb2.TAKPacketV2(cot_type_id=0, cot_type_str="a-f-G-E")) == "a-f-G-E"


def test_config_valori_predefiniti(pki, tmp_path):
    p = scrivi_config(tmp_path / "c.conf", pki, 8089, 4403, tmp_path)
    cfg = m.carica_config(str(p))
    assert cfg.tak_porta == 8089 and cfg.canali == "privati" and cfg.squadra == "Cyan"
    assert cfg.includi_ponte is True and cfg.errore_massimo_m == 200.0


@pytest.mark.parametrize("chiave,valore,messaggio", [
    ("mappa__squadra", "Viola", "squadra"),
    ("meshtastic__canali", "1,9", "da 0 a 7"),
    ("meshtastic__canali", "privatissimi", "canali"),
    ("meshtastic__collegamento", "ble:xx", "collegamento"),
    ("tak__certificato", "/non/esiste.pem", "certificato"),
    ("mappa__validita_min", "1", "validita_min"),
    ("mappa__aggiornamento_min", "0", "aggiornamento_min"),
    ("mappa__aggiornamento_min", "61", "aggiornamento_min"),
    ("tak__porta", "abc", "porta"),
])
def test_config_errori_comprensibili(pki, tmp_path, chiave, valore, messaggio):
    p = scrivi_config(tmp_path / "c.conf", pki, 8089, 4403, tmp_path, **{chiave: valore})
    with pytest.raises(m.ErroreConfig, match=messaggio):
        m.carica_config(str(p))


def test_config_assente(tmp_path):
    with pytest.raises(m.ErroreConfig, match="non trovato"):
        m.carica_config(str(tmp_path / "manca.conf"))


# ---- dal TAK Server alla radio: cosa passa e come ----

def _radice(nome):
    return ET.fromstring(open(f"{SDK}/testdata/cot_xml/{nome}.xml", "rb").read())


@pytest.mark.parametrize("nome,attesa", [
    ("geochat_simple", "chat"), ("geochat_dm", "chat"), ("chat_receipt_read", None),
    ("drawing_circle", "oggetto"), ("drawing_polygon", "oggetto"), ("marker_spot", "oggetto"),
    ("marker_2525", "oggetto"), ("route_3wp", "oggetto"), ("ranging_line", "oggetto"), ("waypoint", "oggetto"),
    ("emergency_911", "emergenza"), ("emergency_cancel", "emergenza"), ("alert_tic", "emergenza"),
    ("casevac", "medevac"), ("task_engage", "incarico"),
    ("pli_full", None), ("pli_itak", None), ("pli_takaware", None), ("pli_webtak", None), ("aircraft_adsb", None),
    ("delete_event", None), ("taktalk_text", None),
])
def test_cosa_va_alle_radio(nome, attesa):
    assert m.categoria_dal_server(_radice(nome))[0] == attesa


def test_quello_che_viene_dalla_radio_non_ci_torna():
    xml = open(f"{SDK}/testdata/cot_xml/geochat_simple.xml", encoding="utf-8").read()
    segnato = m.marca_dalla_radio(xml, "abc123", "!1111aaaa", "!abcd0001")
    radice = ET.fromstring(segnato.encode())
    assert radice.find("detail/__mesh2tak").attrib == {"h": "abc123", "radio": "!1111aaaa", "ponte": "!abcd0001"}
    categoria, motivo = m.categoria_dal_server(radice)
    assert categoria is None and "rete radio" in motivo
    vuoto = m.marca_dalla_radio('<event uid="x" type="b-t-f"><detail/></event>', "h", "!1", "!2")
    assert ET.fromstring(vuoto).find("detail/__mesh2tak") is not None


def test_scadenza_allungata_solo_se_corta():
    ora = 1_790_000_000
    xml = ('<event version="2.0" uid="u" type="u-d-c-c" time="x" start="x" stale="2026-09-21T14:15:00Z">'
           '<detail><link stale="2020-01-01T00:00:00Z"/></detail></event>')
    nuovo = m.allunga_stale(xml, ora)
    radice = ET.fromstring(nuovo)
    assert m.leggi_ora_cot(radice.get("stale")) == pytest.approx(ora + 900, abs=1)
    assert radice.find("detail/link").get("stale") == "2020-01-01T00:00:00Z"  # solo l'evento, non i figli
    lunga = xml.replace("2026-09-21T14:15:00Z", "2026-09-22T14:15:00Z")
    assert m.allunga_stale(lunga, ora) == lunga


def test_eventi_dal_flusso_del_server():
    buf = bytearray(b'<?xml version="1.0"?><event uid="a"><detail/></event>\n<event uid="b"/><eve')
    assert m.estrai_eventi(buf) == [b'<event uid="a"><detail/></event>', b'<event uid="b"/>']
    buf += b'nt uid="c"><point/></event>'
    assert m.estrai_eventi(buf) == [b'<event uid="c"><point/></event>']
    assert m.estrai_eventi(bytearray(b"rumore senza eventi")) == []


def test_tempo_in_aria_long_fast():
    breve, lungo = m.tempo_in_aria(60), m.tempo_in_aria(225)
    assert 0.6 < breve < lungo < 2.5
    assert m.tempo_in_aria(100, "SHORT_FAST") < m.tempo_in_aria(100, "LONG_FAST") < m.tempo_in_aria(100, "LONG_SLOW")
    assert m.tempo_in_aria(100, "PRESET_NUOVO") == m.tempo_in_aria(100, "LONG_FAST")


def test_memoria_dimentica():
    mem = m.Memoria(0.3)
    mem.aggiungi("a")
    assert "a" in mem and "b" not in mem
    import time as t
    t.sleep(0.4)
    assert "a" not in mem
    piccola = m.Memoria(60, massimo=2)
    for k in "xyz":
        piccola.aggiungi(k)
    assert "x" not in piccola and "z" in piccola


def _voce(uid, categoria="oggetto", priorita=3, impronta=None, creata=0.0, pronta=0.0, scadenza=1e12):
    return m.VoceRadio(priorita=priorita, categoria=categoria, uid=uid, descrizione=uid, filo=b"x",
                       impronta=impronta or uid, creata=creata, pronta=pronta, scadenza=scadenza)


def test_coda_radio_priorita_e_versioni():
    c = m.CodaRadio()
    c.metti(_voce("forma", creata=1))
    c.metti(_voce("chat1", "chat", 1, creata=2))
    c.metti(_voce("allarme", "emergenza", 0, creata=3))
    assert c.metti(_voce("forma", impronta="forma-v2", creata=4)) is True   # nuova versione dello stesso oggetto
    c.metti(_voce("chat1", "chat", 1, impronta="altra-chat", creata=5))      # le chat non si sostituiscono
    assert len(c) == 4
    assert [c.prossima(10).uid for _ in range(4)] == ["allarme", "chat1", "chat1", "forma"]
    c.metti(_voce("dopo", pronta=50))
    assert c.prossima(10) is None and c.guarda(60).uid == "dopo"
    c.metti(_voce("vecchia", creata=0, scadenza=5))
    assert [v.uid for v in c.scadute(6)] == ["vecchia"]
    c.metti(_voce("doppia", impronta="hh"))
    assert [v.uid for v in c.togli_impronta("hh")] == ["doppia"]


def test_avviso_al_mittente_e_mittenti():
    xml = m.cot_chat_diretta(da_uid="!abcd0001", da_callsign="Ponte CNSAS Varese", a_uid="ANDROID-7",
                             a_callsign="07-BIANCHI", testo="troppo grande <&>", ora=1_790_000_000)
    ev = ET.fromstring(xml)
    assert ev.get("type") == "b-t-f" and ev.get("uid").startswith("GeoChat.!abcd0001.ANDROID-7.")
    assert [d.get("callsign") for d in ev.findall("detail/marti/dest")] == ["07-BIANCHI"]
    assert ev.find("detail/remarks").text == "troppo grande <&>"
    assert ev.find("detail/__mesh2tak") is not None, "l'avviso non deve finire in radio"
    assert m.mittente_evento(ev) == ("!abcd0001", "Ponte CNSAS Varese")
    assert m.mittente_evento(_radice("marker_spot")) == ("ANDROID-0000000000000001", "SIM-01")
    assert m.mittente_evento(_radice("geochat_simple")) == ("ANDROID-0000000000000002", "TESTNODE-01")


@pytest.mark.parametrize("valore", ["0.1", "12"])
def test_config_tempo_radio_entro_i_limiti(pki, tmp_path, valore):
    p = scrivi_config(tmp_path / "c.conf", pki, 8089, 4403, tmp_path, radio__tempo_radio_percento=valore)
    with pytest.raises(m.ErroreConfig, match="tempo_radio_percento"):
        m.carica_config(str(p))


def test_config_radio_predefinita(pki, tmp_path):
    cfg = m.carica_config(str(scrivi_config(tmp_path / "c.conf", pki, 8089, 4403, tmp_path)))
    assert cfg.invio_radio is True and cfg.tempo_radio_percento == 3.0
    cfg = m.carica_config(str(scrivi_config(tmp_path / "d.conf", pki, 8089, 4403, tmp_path, radio__invio="no")))
    assert cfg.invio_radio is False


def test_icona_del_ponte(pki, tmp_path):
    xml = m.cot_evento(uid="!abcd0001", tipo="a-f-G-I", how="h-e", lat=45.8, lon=8.6, t_evento=1_790_000_000,
                       stale=1_790_003_600, callsign="Ponte", icona=m.ICONA_PONTE)
    import xml.etree.ElementTree as ET
    ev = ET.fromstring(xml)
    assert ev.find("detail/usericon").get("iconsetpath") == "6d781afb-89a6-4c07-b2b9-a89748b6a38f/Landmarks/tower.png"
    assert ev.get("type") == "a-f-G-I", "chi non ha l'insieme OSM mostra il simbolo militare"
    senza = ET.fromstring(m.cot_evento(uid="x", tipo="a-f-G-U-C", how="m-g", lat=45.8, lon=8.6,
                                       t_evento=1_790_000_000, stale=1_790_003_600))
    assert senza.find("detail/usericon") is None
    assert m.carica_config(str(scrivi_config(tmp_path / "a.conf", pki, 8089, 4403, tmp_path))).icona_ponte == m.ICONA_PONTE
    cfg = m.carica_config(str(scrivi_config(tmp_path / "b.conf", pki, 8089, 4403, tmp_path, mappa__icona_ponte="")))
    assert cfg.icona_ponte == "", "vuoto = simbolo militare"
