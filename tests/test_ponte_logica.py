"""Logica del ponte in entrambe le direzioni, senza rete: radio e TAK Server finti nello stesso processo.

Le attese casuali vengono azzerate o fissate, cosi' l'ordine degli eventi e' deterministico.
"""
import re
import time
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest

from conftest import SDK
import mesh2tak as m
from meshtastic.protobuf import atak_pb2 as atak_v1, channel_pb2, config_pb2, mesh_pb2

MIO, A, B = 0xABCD0001, 0x1111AAAA, 0x2222BBBB


class IfaceFinta:
    def __init__(self):
        self.myInfo = SimpleNamespace(my_node_num=MIO)
        self.nodesByNum = {MIO: {"user": {"longName": "Ponte CNSAS Varese", "shortName": "PONT"}},
                           A: {"user": {"longName": "07-BIANCHI", "role": "TAK"}},
                           B: {"user": {"longName": "Ponte Sede", "role": "CLIENT"}}}
        pubblico = channel_pb2.Channel(index=0, role=channel_pb2.Channel.Role.PRIMARY)
        pubblico.settings.psk = b"\x01"
        squadra = channel_pb2.Channel(index=1, role=channel_pb2.Channel.Role.SECONDARY)
        squadra.settings.psk = bytes(range(32))
        squadra.settings.name = "CNSAS"
        lora = config_pb2.Config.LoRaConfig(use_preset=True, region=config_pb2.Config.LoRaConfig.RegionCode.EU_868,
                                            modem_preset=config_pb2.Config.LoRaConfig.ModemPreset.LONG_FAST)
        self.localNode = SimpleNamespace(channels=[pubblico, squadra], localConfig=SimpleNamespace(lora=lora))
        self.trasmessi = []

    def sendData(self, data, destinationId, portNum, wantAck, channelIndex, hopLimit):  # noqa: N802
        self.trasmessi.append(dict(data=bytes(data), a=destinationId, porta=portNum, ack=wantAck,
                                   canale=channelIndex, salti=hopLimit))


class TakFinto:
    def __init__(self):
        self.posizioni, self.una_volta = [], []

    def invia(self, uid, xml, stale):
        self.posizioni.append((uid, xml))

    def invia_una_volta(self, xml, scadenza):
        self.una_volta.append(ET.fromstring(xml))


@pytest.fixture
def sdk():
    import meshtastic  # noqa: F401
    return m.LibreriaTak()


@pytest.fixture
def ponte(sdk):
    def crea(**opzioni):
        cfg = m.Config(tak_host="takserver.prova", **opzioni)
        p = m.Ponte(cfg, TakFinto(), sdk)
        p.ATTESA_VERSO_SERVER = (0.0, 0.0)
        p.ATTESA_VERSO_RADIO = (0.0, 0.0)
        p.ATTESA_VERSO_RADIO_ALLARME = (0.0, 0.0)
        p.SPAZIO_TRA_INVII_S = 0.0
        p.collega(IfaceFinta())
        return p
    return crea


def pacchetto(da, porta, carico, canale=1):
    mp = mesh_pb2.MeshPacket(channel=canale)
    setattr(mp, "from", da)
    mp.decoded.portnum = porta
    mp.decoded.payload = carico
    return {"from": da, "channel": canale, "rxTime": time.time(), "decoded": {"portnum": str(porta)}, "raw": mp}


def golden(nome):
    return open(f"{SDK}/testdata/golden/{nome}.bin", "rb").read()


def cot(nome, fresco=True):
    """Esempio ufficiale; con 'fresco' l'evento e' di adesso e scade tra un'ora (quelli dell'SDK sono vecchi)."""
    xml = open(f"{SDK}/testdata/cot_xml/{nome}.xml", encoding="utf-8").read()
    if fresco:
        ora = time.time()
        tag = re.search(r"<event\b[^>]*>", xml).group(0)
        nuovo_tag = re.sub(r'\b(time|start)="[^"]*"', lambda x: f'{x.group(1)}="{m.iso_utc(ora)}"', tag)
        nuovo_tag = re.sub(r'\bstale="[^"]*"', f'stale="{m.iso_utc(ora + 3600)}"', nuovo_tag)
        xml = xml.replace(tag, nuovo_tag, 1)
    return xml.encode("utf-8")


def giri(p, n=5):
    for _ in range(n):
        p.passo_uscite()


# ---- dalla radio al server ----

def test_chat_dalla_radio_arriva_al_server(ponte):
    p = ponte()
    p._gestisci(pacchetto(A, 78, golden("geochat_simple")))
    giri(p)
    assert len(p.tak.una_volta) == 1
    ev = p.tak.una_volta[0]
    assert ev.get("type") == "b-t-f" and ev.get("uid").startswith("GeoChat.ANDROID-0000000000000002.All Chat Rooms")
    assert ev.find("detail/__chat").get("senderCallsign") == "TESTNODE-01"
    assert ev.find("detail/remarks").text == "Roger that, moving to rally point"
    segno = ev.find("detail/__mesh2tak").attrib
    assert segno["radio"] == "!1111aaaa" and segno["ponte"] == "!abcd0001" and len(segno["h"]) == 24


@pytest.mark.parametrize("nome,tipo", [("drawing_circle", "u-d-c-c"), ("drawing_polygon", "u-d-p"),
                                       ("marker_spot", "b-m-p-s-m"), ("route_3wp", "b-m-r"),
                                       ("emergency_911", "b-a-o-tbl"), ("casevac", "b-r-f-h-c"),
                                       ("task_engage", "t-s"), ("ranging_line", "u-rb-a")])
def test_oggetti_dalla_radio_arrivano_al_server(ponte, nome, tipo):
    p = ponte()
    p._gestisci(pacchetto(A, 78, golden(nome)))
    giri(p)
    assert [e.get("type") for e in p.tak.una_volta] == [tipo]
    assert p.tak.posizioni == [], "un oggetto non e' una posizione da risincronizzare"


def test_stessa_chat_sentita_due_volte_va_al_server_una_volta(ponte):
    p = ponte()
    p._gestisci(pacchetto(A, 78, golden("geochat_simple")))
    p._gestisci(pacchetto(B, 78, golden("geochat_simple")))  # ripetuta da un'altra radio
    giri(p)
    assert len(p.tak.una_volta) == 1


def test_canale_pubblico_ignorato(ponte):
    p = ponte()
    p._gestisci(pacchetto(A, 78, golden("geochat_simple"), canale=0))
    giri(p)
    assert p.tak.una_volta == []


def test_un_altro_ponte_l_ha_gia_portato_sul_server(ponte, sdk):
    p = ponte()
    p.ATTESA_VERSO_SERVER = (0.3, 0.3)
    p._gestisci(pacchetto(A, 78, golden("drawing_circle")))
    impronta = sdk.impronta(sdk.decodifica(golden("drawing_circle")))
    gia = m.marca_dalla_radio(sdk.in_xml(sdk.decodifica(golden("drawing_circle"))), impronta, "!1111aaaa",
                              "!2222bbbb")
    p.su_evento_tak(gia.encode())
    giri(p, 2)
    time.sleep(0.35)
    giri(p)
    assert p.tak.una_volta == []
    assert p.iface.trasmessi == [], "cio' che arriva dalla radio non torna in radio"


def test_chat_vecchio_formato(ponte):
    p = ponte()
    v1 = atak_v1.TAKPacket(is_compressed=False,
                           contact=atak_v1.Contact(callsign="VECCHIO-ATAK", device_callsign="ANDROID-9|msg1"),
                           group=atak_v1.Group(team=atak_v1.Team.Yellow, role=atak_v1.MemberRole.TeamLead),
                           chat=atak_v1.GeoChat(message="Arrivati al rifugio"))
    p._gestisci(pacchetto(A, 72, v1.SerializeToString()))
    compressa = atak_v1.TAKPacket(is_compressed=True, contact=atak_v1.Contact(callsign="xx", device_callsign="yy"),
                                  chat=atak_v1.GeoChat(message="illeggibile"))
    p._gestisci(pacchetto(A, 72, compressa.SerializeToString()))
    giri(p)
    assert len(p.tak.una_volta) == 1
    ev = p.tak.una_volta[0]
    assert ev.get("uid") == "GeoChat.ANDROID-9.All Chat Rooms.msg1"
    assert ev.find("detail/__chat").get("senderCallsign") == "VECCHIO-ATAK"
    assert ev.find("detail/remarks").text == "Arrivati al rifugio"


# ---- dal server alla radio ----

def test_chat_dal_server_trasmessa_sul_canale_della_squadra(ponte, sdk):
    p = ponte()
    p.su_evento_tak(cot("geochat_simple"))
    giri(p)
    assert len(p.iface.trasmessi) == 1
    t = p.iface.trasmessi[0]
    assert (t["porta"], t["canale"], t["a"], t["ack"], t["salti"]) == (78, 1, m.BROADCAST, False, 3)
    pkt = sdk.decodifica(t["data"])
    assert pkt.chat.message == "Roger that, moving to rally point"
    assert p.tak.una_volta == [], "cio' che arriva dal server non torna al server"


@pytest.mark.parametrize("nome", ["pli_full", "pli_itak", "pli_takaware", "aircraft_adsb", "chat_receipt_read",
                                  "delete_event", "taktalk_text"])
def test_non_vanno_in_radio(ponte, nome):
    p = ponte()
    p.su_evento_tak(cot(nome))
    giri(p)
    assert p.iface.trasmessi == [] and len(p.coda_radio) == 0


def test_invio_disattivato(ponte):
    p = ponte(invio_radio=False)
    p.su_evento_tak(cot("geochat_simple"))
    giri(p)
    assert p.iface.trasmessi == []


def test_allarme_passa_per_primo(ponte, sdk):
    p = ponte()
    p.SPAZIO_TRA_INVII_S = 3600  # una trasmissione sola, poi si guarda la coda
    p._ultima_trasmissione = time.time()
    for nome in ("drawing_circle", "geochat_simple", "emergency_911"):
        p.su_evento_tak(cot(nome))
    giri(p, 4)
    assert p.iface.trasmessi == [] and len(p.coda_radio) == 3
    p._ultima_trasmissione = 0
    giri(p, 1)
    assert sdk.decodifica(p.iface.trasmessi[0]["data"]).WhichOneof("payload_variant") == "emergency"


def test_ordine_completo(ponte, sdk):
    p = ponte()
    p.ATTESA_VERSO_RADIO = (0.2, 0.2)
    p.ATTESA_VERSO_RADIO_ALLARME = (0.2, 0.2)
    for nome in ("drawing_circle", "geochat_simple", "casevac", "emergency_911"):
        p.su_evento_tak(cot(nome))
    giri(p, 4)
    time.sleep(0.25)
    giri(p, 6)
    tipi = [sdk.decodifica(t["data"]).WhichOneof("payload_variant") for t in p.iface.trasmessi]
    assert tipi == ["emergency", "chat", "casevac", "shape"]


def test_un_altro_ponte_ha_gia_trasmesso(ponte, sdk):
    p = ponte()
    p.ATTESA_VERSO_RADIO = (5.0, 5.0)
    p.su_evento_tak(cot("geochat_simple"))
    giri(p)
    assert len(p.coda_radio) == 1
    _, filo = sdk.da_xml(cot("geochat_simple").decode())
    p._gestisci(pacchetto(B, 78, filo))  # il ponte di Sede l'ha appena trasmesso
    giri(p)
    assert len(p.coda_radio) == 0 and p.iface.trasmessi == []
    assert p.tak.una_volta == [], "sentito in radio ma arrivato dal server: non si rimanda al server"


def test_gia_sentito_in_radio_non_si_ritrasmette(ponte, sdk):
    p = ponte()
    _, filo = sdk.da_xml(cot("drawing_circle").decode())
    p._gestisci(pacchetto(A, 78, filo))           # l'utente l'ha mandato in radio e via internet
    p.su_evento_tak(cot("drawing_circle"))        # ... e arriva anche dal server
    giri(p)
    assert p.iface.trasmessi == []
    assert len(p.tak.una_volta) <= 1


def test_nuova_versione_sostituisce_quella_in_coda(ponte, sdk):
    p = ponte()
    p.ATTESA_VERSO_RADIO = (5.0, 5.0)
    xml = cot("drawing_polygon").decode()
    p.su_evento_tak(xml.encode())
    p.su_evento_tak(xml.replace('callsign="Polygon 1"', 'callsign="Settore 3"').encode())
    giri(p)
    assert len(p.coda_radio) == 1
    assert "Settore 3" in p.coda_radio.guarda(time.time() + 10).descrizione


def test_troppo_grande_avvisa_chi_l_ha_mandato(ponte):
    from test_unita import poligono
    p = ponte()
    p.su_evento_tak(poligono(150).encode())
    p.su_evento_tak(poligono(150).encode())  # rimandato: un avviso solo
    giri(p)
    assert p.iface.trasmessi == []
    assert len(p.tak.una_volta) == 1
    avviso = p.tak.una_volta[0]
    assert avviso.get("type") == "b-t-f"
    assert [d.get("callsign") for d in avviso.findall("detail/marti/dest")] == ["07-BIANCHI"]
    assert "Area di ricerca" in avviso.find("detail/remarks").text
    assert avviso.find("detail/__chat").get("senderCallsign") == "Ponte CNSAS Varese"


def test_limite_del_tempo_radio(ponte, sdk):
    p = ponte(tempo_radio_percento=0.5)   # 3 secondi di credito: due o tre forme
    for i in range(8):
        xml = cot("drawing_circle").decode().replace("6d09b6f6-720a-4eef-a197-183012512316", f"cerchio-{i}")
        p.su_evento_tak(xml.encode())
    giri(p, 20)
    trasmessi = len(p.iface.trasmessi)
    assert 1 <= trasmessi <= 4 and len(p.coda_radio) == 8 - trasmessi
    p.su_evento_tak(cot("emergency_911"))   # gli allarmi passano anche senza credito
    giri(p, 3)
    assert sdk.decodifica(p.iface.trasmessi[-1]["data"]).WhichOneof("payload_variant") == "emergency"


def test_senza_radio_collegata_resta_in_coda(ponte):
    p = ponte()
    p.scollega()
    p.su_evento_tak(cot("geochat_simple"))
    giri(p)
    assert len(p.coda_radio) == 1
    iface = IfaceFinta()
    p.collega(iface)
    p._ultima_trasmissione = 0
    giri(p)
    assert len(iface.trasmessi) == 1


def test_eventi_scaduti_non_vanno_in_radio(ponte):
    p = ponte()
    p.su_evento_tak(cot("geochat_simple", fresco=False))  # scaduto a marzo
    giri(p)
    assert p.iface.trasmessi == []


def test_posizioni_portate_da_un_altro_ponte_non_tornano_in_radio(ponte):
    p = ponte()
    ora = time.time()
    # posizione fissa di un altro ponte (inserita a mano, quindi how h-e): ha il segno, non va in radio
    xml = m.cot_evento(uid="!abcd0002", tipo="a-f-G-I", how="h-e", lat=45.9, lon=8.9, t_evento=ora,
                       stale=ora + 600, callsign="Ponte Sede", segno={"radio": "!abcd0002", "ponte": "!abcd0002"})
    p.su_evento_tak(xml)
    giri(p)
    assert p.iface.trasmessi == [] and len(p.coda_radio) == 0


def test_segnaposto_2525_messo_a_mano_va_in_radio(ponte, sdk):
    p = ponte()
    p.su_evento_tak(cot("marker_2525"))
    giri(p)
    assert len(p.iface.trasmessi) == 1
    assert sdk.decodifica(p.iface.trasmessi[0]["data"]).callsign == "U.16.135057"


def test_stesso_messaggio_due_volte_dal_server_in_coda_una_volta(ponte):
    p = ponte()
    p.ATTESA_VERSO_RADIO = (5.0, 5.0)
    p.su_evento_tak(cot("geochat_simple"))
    p.su_evento_tak(cot("geochat_simple"))
    giri(p)
    assert len(p.coda_radio) == 1


# ---- posizioni ripetute per chi apre l'app dopo (1.2.2) ----

def posizione(da, lat, lon, canale=1):
    """Posizione Meshtastic come la consegna la libreria (porta 3, gia' decodificata)."""
    p = pacchetto(da, 3, mesh_pb2.Position(latitude_i=int(lat * 1e7), longitude_i=int(lon * 1e7)).SerializeToString(),
                  canale)
    p["decoded"]["position"] = {"latitudeI": int(lat * 1e7), "longitudeI": int(lon * 1e7), "time": int(time.time())}
    return p


def eventi_di(p, uid):
    return [ET.fromstring(x) for u, x in p.tak.posizioni if u == uid]


def test_aggiornamento_predefinito_due_minuti():
    assert m.Config(tak_host="x").aggiornamento_min == 2.0


def test_radio_silenziosa_ripetuta_con_l_ora_dell_ultimo_segnale(ponte, caplog):
    p = ponte()
    caplog.set_level("INFO", logger="mesh2tak")
    p._gestisci(posizione(B, 45.9, 8.8))
    assert len(eventi_di(p, "!2222bbbb")) == 1
    p.manutenzione()
    assert len(eventi_di(p, "!2222bbbb")) == 1, "prima dei 2 minuti non si ripete"
    st = p.stato[B]
    st.ultimo_segnale -= 180   # la radio tace da 3 minuti (es. uscita dalla copertura)
    st.ultimo_invio -= 180
    p.manutenzione()
    ev = eventi_di(p, "!2222bbbb")
    assert len(ev) == 2
    assert ev[1].get("time") == m.iso_utc(st.ultimo_segnale), "l'evento dice quando la radio si e' sentita"
    assert ev[1].get("stale") == m.iso_utc(st.ultimo_segnale + 90 * 60)
    assert "ultimo segnale alle" in ev[1].find("detail/remarks").text
    assert p.contatori["ripetute"] == 1
    righe = [r.getMessage() for r in caplog.records if r.levelname == "INFO" and "-> TAK" in r.getMessage()]
    assert len(righe) == 1, "le ripetizioni non riempiono il registro"
    # oltre la validita' (90 minuti dall'ultimo segnale) non si ripete piu'
    st.ultimo_segnale -= 90 * 60
    st.ultimo_invio -= 180
    p.manutenzione()
    assert len(eventi_di(p, "!2222bbbb")) == 2


def test_ponte_ripetuto_con_l_ora_attuale(ponte):
    p = ponte()
    p.stato[MIO] = m.StatoNodo(MIO, m.Fonte(tipo_fonte="mesh", uid="!abcd0001", lat=45.82, lon=8.67, hae=300.0,
                                            ce=m.NON_NOTO, t_fix=time.time() - 3600), time.time() - 3600)
    p.manutenzione()
    ev = eventi_di(p, "!abcd0001")
    assert len(ev) == 1 and ev[0].get("type") == "a-f-G-I"
    assert abs(m.leggi_ora_cot(ev[0].get("time")) - time.time()) < 5


def test_telefono_collegato_via_internet_non_ripetuto_dalla_radio(ponte):
    p = ponte()
    p._gestisci(pacchetto(A, 78, golden("pli_full")))
    assert len(eventi_di(p, "ANDROID-0000000000000002")) == 1, "senza internet passa dalla radio"
    p._collegati_internet = m.Memoria(0.3)
    p.su_evento_tak(cot("pli_full"))   # lo stesso telefono manda la posizione anche via internet
    giri(p)
    p._gestisci(pacchetto(A, 78, golden("pli_full")))
    st = p.stato[A]
    st.ultimo_invio -= 180
    p.manutenzione()
    assert len(eventi_di(p, "ANDROID-0000000000000002")) == 1, "la copia via radio farebbe saltare l'icona indietro"
    assert p.contatori["scartate_collegate_via_internet"] == 1
    time.sleep(0.4)                    # niente piu' via internet: torna a passare dalla radio
    p._gestisci(pacchetto(A, 78, golden("pli_full")))
    assert len(eventi_di(p, "ANDROID-0000000000000002")) == 2


def test_posizione_dal_server_riconosciuta_come_dispositivo():
    for nome in ("pli_full", "pli_itak", "pli_takaware", "pli_webtak"):
        assert m.e_posizione_dispositivo(ET.fromstring(cot(nome))), nome
    assert not m.e_posizione_dispositivo(ET.fromstring(cot("marker_spot")))
    assert not m.e_posizione_dispositivo(ET.fromstring(cot("geochat_simple")))
