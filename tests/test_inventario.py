"""Elenco delle radio della squadra per la pagina della rete (1.6.0): solo quello che le radio trasmettono da sole
sul canale della squadra, come lo consegna la libreria meshtastic (dizionari con i nomi in camelCase, i valori a
zero omessi). Niente coordinate, niente radio del canale pubblico."""
import json
import time

from meshtastic.protobuf import mesh_pb2

import mesh2tak as m
from test_ponte_logica import MIO, IfaceFinta, TakFinto, ponte, sdk  # noqa: F401  (fixture)

R1, R2, R3 = 0x7C3A91E2, 0x9E1D0B77, 0x12345678


def pacchetto(da, porta, decodificato=None, canale=1, salti_inizio=3, salti_resto=3, snr=6.25, rssi=-87, **altro):
    mp = mesh_pb2.MeshPacket(channel=canale)
    setattr(mp, "from", da)
    mp.decoded.portnum = porta
    p = {"from": da, "to": 0xFFFFFFFF, "channel": canale, "rxTime": int(time.time()),
         "decoded": dict({"portnum": str(porta)}, **(decodificato or {})), "raw": mp}
    if salti_inizio:
        p["hopStart"] = salti_inizio
    if salti_resto:                    # come la libreria: un valore a zero non c'e'
        p["hopLimit"] = salti_resto
    if snr:
        p["rxSnr"] = snr
    if rssi:
        p["rxRssi"] = rssi
    p.update(altro)
    return p


def utente(nome, sigla, modello="TRACKER_T1000_E", ruolo=None):
    u = {"id": "!x", "longName": nome, "shortName": sigla, "hwModel": modello}
    if ruolo:
        u["role"] = ruolo              # CLIENT e' il valore predefinito: la libreria lo omette
    return {"user": u}


def metriche(**dm):
    return {"telemetry": {"time": int(time.time()), "deviceMetrics": dm}}


def voci(p):
    s = p.istantanea()
    json.dumps(s)                      # deve andare in JSON cosi' com'e'
    return {r["id"]: r for r in s["radio_rete"]}, s


def test_scheda_completa_di_una_radio(ponte):  # noqa: F811
    p = ponte()
    p.su_pacchetto(pacchetto(R1, m.PORTA_NODEINFO, utente("07-BIANCHI", "07CA", ruolo="TAK")), p.iface)
    p.su_pacchetto(pacchetto(R1, m.PORTA_TELEMETRY, metriche(batteryLevel=87, voltage=3.97, channelUtilization=7.5,
                                                             airUtilTx=0.42, uptimeSeconds=7200)), p.iface)
    r, s = voci(p)
    v = r[f"!{R1:08x}"]
    assert (v["nome"], v["nome_breve"], v["modello"], v["ruolo"]) == ("07-BIANCHI", "07CA", "TRACKER_T1000_E", "TAK")
    assert (v["batteria"], v["tensione"], v["occupato"], v["trasmesso"], v["acceso_s"]) == (87, 3.97, 7.5, 0.42, 7200)
    assert (v["salti"], v["snr"], v["rssi"]) == (0, 6.25, -87) and v["diretto"] and v["metriche"]
    assert time.time() - v["ultimo"] < 5
    testo = json.dumps(s["radio_rete"])
    assert "latitude" not in testo and "45." not in testo         # niente coordinate


def test_ruolo_predefinito_e_valori_a_zero(ponte):  # noqa: F811
    p = ponte()
    p.su_pacchetto(pacchetto(R1, m.PORTA_NODEINFO, utente("Radio", "RA")), p.iface)    # ruolo omesso = CLIENT
    # canale libero: la libreria omette 0.0, ma con uptimeSeconds e' un rapporto completo
    p.su_pacchetto(pacchetto(R1, m.PORTA_TELEMETRY, metriche(batteryLevel=101, uptimeSeconds=60)), p.iface)
    v = voci(p)[0][f"!{R1:08x}"]
    assert v["ruolo"] == "CLIENT" and v["batteria"] == 101 and (v["occupato"], v["trasmesso"]) == (0.0, 0.0)


def test_sentita_attraverso_altre_radio(ponte):  # noqa: F811
    p = ponte()
    p.su_pacchetto(pacchetto(R1, m.PORTA_NODEINFO, utente("Diretta", "DI")), p.iface)          # 0 salti
    p.su_pacchetto(pacchetto(R1, m.PORTA_TELEMETRY, metriche(batteryLevel=50, uptimeSeconds=1), salti_resto=1,
                             snr=-12.5, rssi=-120), p.iface)                                       # 2 salti
    v = voci(p)[0][f"!{R1:08x}"]
    assert v["salti"] == 2 and (v["snr"], v["rssi"]) == (6.25, -87)   # il segnale resta quello diretto
    # hopLimit omesso (0): tutti i salti consumati
    p.su_pacchetto(pacchetto(R2, m.PORTA_NODEINFO, utente("Lontana", "LO"), salti_resto=0), p.iface)
    v2 = voci(p)[0][f"!{R2:08x}"]
    assert v2["salti"] == 3 and v2["snr"] is None and v2["diretto"] is None
    # firmware vecchio senza hopStart: salti non noti
    p.su_pacchetto(pacchetto(R3, m.PORTA_NODEINFO, utente("Vecchia", "VE"), salti_inizio=0), p.iface)
    assert voci(p)[0][f"!{R3:08x}"]["salti"] is None


def test_solo_il_canale_della_squadra(ponte):  # noqa: F811
    p = ponte()
    p.su_pacchetto(pacchetto(R1, m.PORTA_NODEINFO, utente("Sconosciuto", "SC"), canale=0), p.iface)   # pubblico
    p.su_pacchetto(pacchetto(R2, m.PORTA_NODEINFO, utente("Via MQTT", "MQ"), viaMqtt=True), p.iface)
    p.su_pacchetto(pacchetto(MIO, m.PORTA_NODEINFO, utente("Ponte", "PONT")), p.iface)                # il ponte
    assert voci(p)[0] == {}


def test_misure_del_canale_del_ponte(ponte):  # noqa: F811
    p = ponte()
    assert p.istantanea()["canale_radio"] is None
    p.su_pacchetto(pacchetto(MIO, m.PORTA_TELEMETRY, metriche(channelUtilization=12.25, airUtilTx=1.234,
                                                              uptimeSeconds=99), canale=0), p.iface)
    c = p.istantanea()["canale_radio"]
    assert (c["occupato"], c["trasmesso"]) == (12.2, 1.23) and c["quando"]
    p.su_pacchetto(pacchetto(MIO, m.PORTA_TELEMETRY, {"telemetry": {"localStats": {
        "uptimeSeconds": 100, "channelUtilization": 30.0, "noiseFloor": -108}}}, canale=0), p.iface)
    c = p.istantanea()["canale_radio"]
    assert (c["occupato"], c["trasmesso"], c["rumore"]) == (30.0, 0.0, -108)
    assert voci(p)[0] == {}                                      # il ponte non e' tra le radio


def test_dal_database_del_nodo_all_avvio(sdk):  # noqa: F811
    iface = IfaceFinta()
    ora = int(time.time())
    iface.nodesByNum[R1] = {"num": R1, "channel": 1, "lastHeard": ora - 600, "snr": 5.5, "hopsAway": 0,
                            "user": {"longName": "07-BIANCHI", "shortName": "07CA", "hwModel": "RAK4631",
                                     "role": "TAK"},
                            "deviceMetrics": {"batteryLevel": 64, "voltage": 3.8, "uptimeSeconds": 50}}
    iface.nodesByNum[R2] = {"num": R2, "lastHeard": ora - 60, "user": {"longName": "Pubblico"}}    # canale 0
    iface.nodesByNum[R3] = {"num": R3, "channel": 1, "user": {"longName": "Mai sentita"}}             # niente lastHeard
    # 1.15.0: il nodo la segna sul canale della squadra, ma il ponte non l'ha mai sentita li' (16 radio invece di 1)
    iface.nodesByNum[0x55555555] = {"num": 0x55555555, "channel": 1, "lastHeard": ora - 60,
                                    "user": {"longName": "Estranea di prima"}}
    p = m.Ponte(m.Config(tak_host="x"), TakFinto(), sdk)
    for n in (R1, R3):
        p.squadra.segna(n, ora - 600)          # sentite davvero sul canale della squadra (prima del riavvio)
    p.collega(iface)
    r = voci(p)[0]
    assert list(r) == [f"!{R1:08x}"], "R2 del canale pubblico, R3 mai sentita dal nodo, la quarta mai sul canale"
    v = r[f"!{R1:08x}"]
    assert (v["nome"], v["modello"], v["ruolo"], v["batteria"], v["salti"], v["snr"], v["rssi"]) == \
        ("07-BIANCHI", "RAK4631", "TAK", 64, 0, 5.5, None)
    assert v["ultimo"] == ora - 600 and v["metriche"] == ora - 600
    # un pacchetto vero dopo: vince il piu' recente, le metriche nuove sostituiscono quelle del database
    p.su_pacchetto(pacchetto(R1, m.PORTA_TELEMETRY, metriche(batteryLevel=63, uptimeSeconds=700)), p.iface)
    v = voci(p)[0][f"!{R1:08x}"]
    assert v["batteria"] == 63 and v["ultimo"] > ora - 10
    # ricollegandosi (fine di una pausa) non torna indietro
    p.collega(iface)
    assert voci(p)[0][f"!{R1:08x}"]["batteria"] == 63


def test_dati_strani_ripuliti(ponte):  # noqa: F811
    p = ponte()
    p.su_pacchetto(pacchetto(R1, m.PORTA_NODEINFO, utente("Mario\x00\x07 <b>", "M\x01", modello="<script>",
                                                         ruolo="x; rm")), p.iface)
    p.su_pacchetto(pacchetto(R1, m.PORTA_TELEMETRY, metriche(batteryLevel=250, voltage="tanto", channelUtilization=900,
                                                             airUtilTx=-3, uptimeSeconds=-5)), p.iface)
    p.su_pacchetto(pacchetto(R1, m.PORTA_NODEINFO, {}, snr=99.0, rssi=50), p.iface)
    v = voci(p)[0][f"!{R1:08x}"]
    assert v["nome"] == "Mario <b>" and v["nome_breve"] == "M" and v["modello"] == "" and v["ruolo"] == ""
    assert (v["batteria"], v["tensione"], v["occupato"], v["trasmesso"], v["acceso_s"]) == (None, None, None, None, None)
    assert v["snr"] is None and v["rssi"] is None


def test_al_massimo_le_ultime_radio(ponte):  # noqa: F811
    p = ponte()
    p.MAX_VISTE = 5
    for i in range(8):
        pk = pacchetto(0x1000 + i, m.PORTA_NODEINFO, utente(f"R{i}", f"R{i}"))
        pk["rxTime"] = int(time.time()) - 100 + i
        p.su_pacchetto(pk, p.iface)
    assert sorted(voci(p)[0]) == [f"!{0x1000 + i:08x}" for i in range(3, 8)]


def test_resta_durante_la_pausa(ponte):  # noqa: F811
    p = ponte()
    p.su_pacchetto(pacchetto(R1, m.PORTA_NODEINFO, utente("07-BIANCHI", "07CA")), p.iface)
    p.scollega()                                                  # gestione del nodo: il ponte lascia il nodo
    v = voci(p)[0][f"!{R1:08x}"]
    assert v["nome"] == "07-BIANCHI"


def test_formato_vero_della_libreria_meshtastic(ponte):  # noqa: F811
    """Gli stessi pacchetti passati dalla libreria meshtastic vera (protobuf -> dizionario), non imitati a mano."""
    import threading

    from meshtastic.mesh_interface import MeshInterface
    from meshtastic.protobuf import config_pb2, portnums_pb2, telemetry_pb2
    from pubsub import pub

    p = ponte()
    arrivati, pronti = [], threading.Event()

    def prendi(packet, interface):
        arrivati.append(packet)
        if len(arrivati) == 3:
            pronti.set()

    def pacchetto_vero(porta, carico, resto, snr=-3.25, rssi=-101):
        mp = mesh_pb2.MeshPacket(channel=1, rx_time=int(time.time()), rx_snr=snr, rx_rssi=rssi, hop_start=3,
                                 hop_limit=resto, id=42)
        setattr(mp, "from", R1)
        mp.to = 0xFFFFFFFF
        mp.decoded.portnum = porta
        mp.decoded.payload = carico
        return mp

    pub.subscribe(prendi, "meshtastic.receive")
    lib = MeshInterface(noProto=True)
    lib.nodes, lib.nodesByNum = {}, {}
    try:
        u = mesh_pb2.User(id=f"!{R1:08x}", long_name="07-BIANCHI", short_name="07CA",
                          hw_model=mesh_pb2.HardwareModel.RAK4631, role=config_pb2.Config.DeviceConfig.Role.TAK)
        lib._handlePacketFromRadio(pacchetto_vero(portnums_pb2.PortNum.NODEINFO_APP, u.SerializeToString(), 3))
        t = telemetry_pb2.Telemetry(time=int(time.time()))
        t.device_metrics.battery_level = 55
        t.device_metrics.voltage = 3.71
        t.device_metrics.channel_utilization = 4.5      # air_util_tx resta 0: la libreria lo omette
        t.device_metrics.uptime_seconds = 99
        lib._handlePacketFromRadio(pacchetto_vero(portnums_pb2.PortNum.TELEMETRY_APP, t.SerializeToString(), 3))
        # poi un pacchetto arrivato passando da un'altra radio (hop_limit 2): il segnale diretto resta quello di prima
        lib._handlePacketFromRadio(pacchetto_vero(portnums_pb2.PortNum.NODEINFO_APP, u.SerializeToString(), 2,
                                                  snr=-15.0, rssi=-125))
        assert pronti.wait(5), arrivati
    finally:
        pub.unsubscribe(prendi, "meshtastic.receive")
    for pk in arrivati:
        p.su_pacchetto(pk, p.iface)
    v = voci(p)[0][f"!{R1:08x}"]
    assert (v["nome"], v["nome_breve"], v["modello"], v["ruolo"]) == ("07-BIANCHI", "07CA", "RAK4631", "TAK")
    assert (v["batteria"], v["tensione"], v["occupato"], v["trasmesso"], v["acceso_s"]) == (55, 3.71, 4.5, 0.0, 99)
    assert (v["salti"], v["snr"], v["rssi"]) == (1, -3.25, -101)
