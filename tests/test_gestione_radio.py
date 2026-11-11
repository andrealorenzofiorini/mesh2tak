"""Gestione da lontano delle radio della squadra (1.10.0): messaggi di amministrazione Meshtastic attraverso il nodo
del ponte, con una radio finta che si comporta come il firmware (chiave amministratore, chiave di sessione,
conferme). Le chiavi dei canali e la chiave privata non devono mai uscire dal ponte."""
import base64
import json
import threading
import time
from types import SimpleNamespace

import pytest
from meshtastic.protobuf import admin_pb2, channel_pb2, config_pb2, mesh_pb2, module_config_pb2

import mesh2tak as m
from test_inventario import pacchetto, utente
from test_ponte_logica import MIO, IfaceFinta, TakFinto, ponte, sdk  # noqa: F401  (fixture)

R = 0x7C3A91E2
PSK_SQUADRA = bytes(range(32))


class RadioLontana:
    """La radio dall'altra parte: risponde ai messaggi di amministrazione come il firmware."""

    def __init__(self):
        self.autorizzata = True
        self.muta = False
        self.implicita_prima = False
        self.sessione = b""
        self.sessione_valida = True
        self.ricevuti = []
        self.riavvii = 0
        self.proprietario = mesh_pb2.User(long_name="Rip Velate", short_name="VELA")
        self.device = config_pb2.Config.DeviceConfig(role=config_pb2.Config.DeviceConfig.Role.ROUTER,
                                                     node_info_broadcast_secs=10800)
        self.position = config_pb2.Config.PositionConfig(position_broadcast_secs=900, fixed_position=False)
        self.security = config_pb2.Config.SecurityConfig(public_key=b"P" * 32, private_key=b"S" * 32,
                                                         admin_key=[b"A" * 32])
        self.telemetry = module_config_pb2.ModuleConfig.TelemetryConfig(device_update_interval=1800)
        self.canali = []
        for i in range(8):
            ch = channel_pb2.Channel(index=i)
            if i == 0:
                ch.role, ch.settings.psk, ch.settings.name = channel_pb2.Channel.Role.PRIMARY, PSK_SQUADRA, "CNSAS"
            elif i == 1:
                ch.role, ch.settings.psk = channel_pb2.Channel.Role.SECONDARY, b"\x01"
            self.canali.append(ch)
        self.posizione_fissa = None

    def gestisci(self, a):
        """Torna ("admin", risposta) | ("ack", None) | ("nak", motivo) | None (silenzio)."""
        self.ricevuti.append(a)
        if self.muta:
            return None
        if not self.autorizzata:
            return "nak", "ADMIN_PUBLIC_KEY_UNAUTHORIZED"
        quale = a.WhichOneof("payload_variant")
        r = admin_pb2.AdminMessage()
        if quale.startswith("get_"):
            self.sessione = bytes([len(self.ricevuti) % 250 + 1] * 8)
            r.session_passkey = self.sessione
            if quale == "get_owner_request":
                r.get_owner_response.CopyFrom(self.proprietario)
            elif quale == "get_device_metadata_request":
                r.get_device_metadata_response.firmware_version = "2.7.11.ee68575"
                r.get_device_metadata_response.hw_model = mesh_pb2.HardwareModel.RAK4631
                r.get_device_metadata_response.hasPKC = True
            elif quale == "get_channel_request":
                r.get_channel_response.CopyFrom(self.canali[a.get_channel_request - 1])
            elif quale == "get_config_request":
                tipo = admin_pb2.AdminMessage.ConfigType.Name(a.get_config_request)
                if tipo == "SESSIONKEY_CONFIG":
                    r.get_config_response.sessionkey.SetInParent()
                else:
                    nome = tipo[:-len("_CONFIG")].lower()
                    valore = type(getattr(self, nome))()
                    valore.CopyFrom(getattr(self, nome))
                    if nome == "security":
                        valore.private_key = b""       # il firmware non la manda in aria
                    getattr(r.get_config_response, nome).CopyFrom(valore)
            elif quale == "get_module_config_request":
                r.get_module_config_response.telemetry.CopyFrom(self.telemetry)
            return "admin", r
        if not self.sessione_valida or not self.sessione or bytes(a.session_passkey) != self.sessione:
            return "nak", "ADMIN_BAD_SESSION_KEY"
        if quale == "set_config":
            nome = a.set_config.WhichOneof("payload_variant")
            getattr(self, nome).CopyFrom(getattr(a.set_config, nome))
            self.riavvii += 1
        elif quale == "set_module_config":
            self.telemetry.CopyFrom(a.set_module_config.telemetry)
            self.riavvii += 1
        elif quale == "set_owner":
            self.proprietario.long_name, self.proprietario.short_name = a.set_owner.long_name, a.set_owner.short_name
        elif quale == "set_channel":
            self.canali[a.set_channel.index].CopyFrom(a.set_channel)
        elif quale == "set_fixed_position":
            self.posizione_fissa = (a.set_fixed_position.latitude_i, a.set_fixed_position.longitude_i,
                                    a.set_fixed_position.altitude)
        elif quale == "remove_fixed_position":
            self.posizione_fissa = None
        elif quale == "reboot_seconds":
            self.riavvii += 1
        return "ack", None


class IfaceRadio(IfaceFinta):
    """Il nodo del ponte: come la libreria, chiama il gestore della risposta da un altro thread."""

    def __init__(self, lontana):
        super().__init__()
        self.lontana = lontana
        self.nodesByNum[R] = {"user": {"longName": "Rip Velate", "shortName": "VELA", "publicKey": "UFBQ"}}
        self.responseHandlers = {}
        self.inviati = []
        self._id = 100

    def _addResponseHandler(self, ident, gestore, ackPermitted=False):  # noqa: N802,N803
        self.responseHandlers[ident] = gestore

    def sendData(self, data, destinationId, portNum=0, wantAck=False, wantResponse=False, onResponse=None,  # noqa: N802,N803
                 onResponseAckPermitted=False, channelIndex=0, hopLimit=None, pkiEncrypted=False):
        if portNum != m.PORTA_ADMIN:
            return super().sendData(data, destinationId, portNum, wantAck, channelIndex, hopLimit)
        self._id += 1
        ident = self._id
        a = admin_pb2.AdminMessage()
        a.ParseFromString(data.SerializeToString())
        self.inviati.append(dict(a=destinationId, pki=pkiEncrypted, ack=wantAck, risposta=wantResponse, msg=a))
        self.responseHandlers[ident] = onResponse
        esito = self.lontana.gestisci(a)

        def consegna():
            if esito is None:
                return
            if self.lontana.implicita_prima and esito[0] == "ack":
                self.responseHandlers.pop(ident)({"from": MIO, "decoded": {"routing": {"errorReason": "NONE"}}})
                time.sleep(0.02)
            g = self.responseHandlers.pop(ident, None)
            if g is None:
                return
            if esito[0] == "admin":
                g({"from": destinationId, "decoded": {"admin": {"raw": esito[1]}}})
            elif esito[0] == "ack":
                g({"from": destinationId, "decoded": {"routing": {"errorReason": "NONE"}}})
            else:
                g({"from": destinationId, "decoded": {"routing": {"errorReason": esito[1]}}})
        threading.Timer(0.01, consegna).start()
        return SimpleNamespace(id=ident)


@pytest.fixture
def banco(ponte):  # noqa: F811
    lontana = RadioLontana()
    p = ponte()
    iface = IfaceRadio(lontana)
    p.collega(iface)
    p.su_pacchetto(pacchetto(R, m.PORTA_NODEINFO, utente("Rip Velate", "VELA", ruolo="ROUTER")), iface)
    g = m.GestioneRadio(p)
    g.ATTESA_RISPOSTA_S = 1.0
    return SimpleNamespace(g=g, p=p, iface=iface, lontana=lontana)


def fai(b, limite=10, **dati):
    dati.setdefault("radio", f"!{R:08x}")
    ident = b.g.accoda(dati, "andrea")
    fine = time.time() + limite
    while time.time() < fine:
        with b.g._lock:
            op = dict(b.g._ops[ident])
        if op["stato"] in ("fatto", "errore"):
            return op
        time.sleep(0.01)
    raise AssertionError(f"operazione mai finita: {op}")


class Richiesta:
    def __init__(self, metodo, percorso, corpo=None, da="127.0.0.1", utente_ak="andrea", **intestazioni):
        self.command, self.path, self.client_address = metodo, percorso, (da, 5555)
        self.headers = dict(intestazioni)
        if utente_ak:
            self.headers["X-Authentik-Username"] = utente_ak
        self.corpo = json.dumps(corpo).encode() if corpo is not None else None


def servi(b, r):
    return b.g.servi(r, r.path.split("?")[0], r.corpo)


def test_legge_nome_info_e_sezioni_senza_la_chiave_privata(banco):
    op = fai(banco, azione="leggi", sezioni=["proprietario", "info", "device", "position", "security", "telemetry"])
    assert op["stato"] == "fatto", op
    r = op["risultato"]
    assert r["proprietario"] == {"nome": "Rip Velate", "nome_breve": "VELA"}
    assert r["info"]["firmware"].startswith("2.7.11") and r["info"]["modello"] == "RAK4631"
    assert r["device"]["role"] == "ROUTER" and r["device"]["node_info_broadcast_secs"] == 10800
    assert r["position"]["position_broadcast_secs"] == 900 and r["position"]["fixed_position"] is False
    assert "private_key" not in r["security"] and r["security"]["admin_key"] == [base64.b64encode(b"A" * 32).decode()]
    assert r["telemetry"]["device_update_interval"] == 1800
    # ogni messaggio e' diretto alla radio, cifrato con la sua chiave pubblica, con conferma
    assert all(i["a"] == R and i["pki"] and i["ack"] for i in banco.iface.inviati)


def test_cambia_una_sezione_partendo_da_quella_letta(banco):
    assert fai(banco, azione="leggi", sezioni=["position"])["stato"] == "fatto"
    op = fai(banco, azione="scrivi", sezione="position", valori={"position_broadcast_secs": 300})
    assert op["stato"] == "fatto", op
    assert "riavvia" in op["risultato"]["messaggio"]
    assert banco.lontana.position.position_broadcast_secs == 300 and banco.lontana.riavvii == 1
    # dopo il riavvio la sessione non vale piu': al comando dopo ne chiede una nuova
    prima = len(banco.iface.inviati)
    assert fai(banco, azione="scrivi", sezione="position", valori={"fixed_position": True})["stato"] == "fatto"
    assert banco.lontana.position.fixed_position and banco.lontana.position.position_broadcast_secs == 300
    assert [i["msg"].WhichOneof("payload_variant") for i in banco.iface.inviati[prima:]] == ["get_config_request",
                                                                                           "set_config"]


def test_senza_averla_letta_non_la_cambia(banco):
    op = fai(banco, azione="scrivi", sezione="device", valori={"role": "CLIENT"})
    assert op["stato"] == "errore" and "rileggi" in op["errore"]
    assert banco.lontana.device.role == config_pb2.Config.DeviceConfig.Role.ROUTER


def test_valori_sbagliati_e_sicurezza_rifiutati(banco):
    fai(banco, azione="leggi", sezioni=["device"])
    op = fai(banco, azione="scrivi", sezione="device", valori={"role": "INVENTATO"})
    assert op["stato"] == "errore" and "non validi" in op["errore"]
    with pytest.raises(m.ErroreRadio, match="Sicurezza"):
        banco.g.accoda({"radio": f"!{R:08x}", "azione": "scrivi", "sezione": "security", "valori": {}}, "andrea")


def test_un_elenco_si_sostituisce(banco):
    fai(banco, azione="leggi", sezioni=["telemetry"])
    banco.g._lette[(R, "security")] = (time.time(), banco.lontana.security)
    nuova = banco.g._modifica(R, "security", {"admin_key": [base64.b64encode(b"B" * 32).decode()]})
    assert list(nuova.admin_key) == [b"B" * 32] and nuova.private_key == b"S" * 32


def test_nome_posizione_fissa_e_riavvio(banco):
    assert fai(banco, azione="proprietario", nome="Rip Campo dei Fiori", nome_breve="RCDF")["stato"] == "fatto"
    assert (banco.lontana.proprietario.long_name, banco.lontana.proprietario.short_name) == ("Rip Campo dei Fiori",
                                                                                           "RCDF")
    assert fai(banco, azione="posizione_fissa", lat=45.8700, lon=8.7700, quota=1220)["stato"] == "fatto"
    assert banco.lontana.posizione_fissa == (458700000, 87700000, 1220)
    assert fai(banco, azione="togli_posizione_fissa")["stato"] == "fatto" and banco.lontana.posizione_fissa is None
    assert fai(banco, azione="riavvia")["stato"] == "fatto" and banco.lontana.riavvii == 1
    for sbagliata in (dict(azione="proprietario", nome="x" * 40, nome_breve="AB"),
                      dict(azione="proprietario", nome="ok", nome_breve="LUNGA"),
                      dict(azione="posizione_fissa", lat=0, lon=0), dict(azione="posizione_fissa", lat="x", lon=8),
                      dict(azione="formatta")):
        with pytest.raises(m.ErroreRadio):
            banco.g.accoda(dict(sbagliata, radio=f"!{R:08x}"), "andrea")


def test_canali_le_chiavi_non_escono_dal_ponte(banco):
    op = fai(banco, azione="leggi", sezioni=["canali"])
    assert op["stato"] == "fatto", op
    canali = op["risultato"]["canali"]
    assert [(c["indice"], c["ruolo"], c["nome"]) for c in canali] == [(0, "PRIMARY", "CNSAS"), (1, "SECONDARY", "")]
    assert canali[0]["chiave"] == {"tipo": "privata", "bit": 256, "come_ponte": "CNSAS"}
    assert canali[1]["chiave"] == {"tipo": "predefinita"}
    # si e' fermato al primo canale spento: tre richieste, non otto
    assert sum(1 for i in banco.iface.inviati if i["msg"].HasField("get_channel_request")) == 3
    testo = json.dumps(servi(banco, Richiesta("GET", f"/mesh2tak/radio/op?id={op['id']}"))[1])
    assert base64.b64encode(PSK_SQUADRA).decode() not in testo and "psk" not in testo


def test_canale_nuovo_con_la_chiave_del_ponte_o_una_nuova(banco):
    fai(banco, azione="leggi", sezioni=["canali"])
    op = fai(banco, azione="canale", indice=2, ruolo="SECONDARY", nome="x", chiave={"modo": "ponte", "canale_ponte": 1},
             precisione=13)
    assert op["stato"] == "fatto", op
    ch = banco.lontana.canali[2]
    assert (ch.role, bytes(ch.settings.psk), ch.settings.name) == (channel_pb2.Channel.Role.SECONDARY, PSK_SQUADRA,
                                                                    "CNSAS")
    assert ch.settings.module_settings.position_precision == 13
    nuova = base64.b64encode(b"k" * 16).decode()
    op = fai(banco, azione="canale", indice=2, ruolo="SECONDARY", nome="Prova", chiave={"modo": "nuova", "valore": nuova})
    assert op["stato"] == "fatto" and bytes(banco.lontana.canali[2].settings.psk) == b"k" * 16
    assert nuova not in json.dumps({k: v for k, v in op.items() if not k.startswith("_")})
    # tenere la chiave com'e' cambiando solo il nome; spegnere un canale lo svuota
    assert fai(banco, azione="canale", indice=0, ruolo="PRIMARY", nome="CNSAS-VA", chiave={"modo": "tieni"})["stato"] == "fatto"
    assert (banco.lontana.canali[0].settings.name, bytes(banco.lontana.canali[0].settings.psk)) == ("CNSAS-VA", PSK_SQUADRA)
    assert fai(banco, azione="canale", indice=2, ruolo="DISABLED", nome="")["stato"] == "fatto"
    assert banco.lontana.canali[2].role == 0 and not banco.lontana.canali[2].settings.psk
    for sbagliato in (dict(indice=0, ruolo="SECONDARY"), dict(indice=1, ruolo="PRIMARY"), dict(indice=9, ruolo="SECONDARY"),
                      dict(indice=1, ruolo="SECONDARY", nome="troppolungoooo"),
                      dict(indice=1, ruolo="SECONDARY", chiave={"modo": "nuova", "valore": "corta"})):
        with pytest.raises(m.ErroreRadio):
            banco.g.accoda(dict(sbagliato, radio=f"!{R:08x}", azione="canale"), "andrea")


def test_senza_chiave_amministratore_lo_dice_in_chiaro(banco):
    banco.lontana.autorizzata = False
    op = fai(banco, azione="leggi", sezioni=["device"])
    assert op["stato"] == "errore" and "chiave di amministrazione" in op["errore"] and "da vicino" in op["errore"]


def test_radio_muta_sessione_scaduta_e_conferma_implicita(banco):
    banco.lontana.muta = True
    op = fai(banco, azione="leggi", sezioni=["device"])
    assert op["stato"] == "errore" and "non ha risposto" in op["errore"]
    assert not banco.iface.responseHandlers          # niente gestori dimenticati
    banco.lontana.muta = False
    # sessione rifiutata una volta: ne chiede un'altra e riprova da solo
    fai(banco, azione="leggi", sezioni=["device"])
    banco.g._sessioni[R] = (time.time(), b"vecchia!")
    assert fai(banco, azione="riavvia")["stato"] == "fatto"
    # prima la conferma di un vicino che ripete, poi quella vera
    banco.lontana.implicita_prima = True
    assert fai(banco, azione="riavvia")["stato"] == "fatto"
    banco.lontana.sessione_valida = False
    op = fai(banco, azione="riavvia")
    assert op["stato"] == "errore" and "sessione" in op["errore"]


def test_solo_radio_della_squadra_e_chiave_pubblica_nota(banco):
    with pytest.raises(m.ErroreRadio, match="canale della squadra"):
        banco.g.accoda({"radio": "!12345678", "azione": "riavvia"}, "andrea")
    with pytest.raises(m.ErroreRadio, match="canale della squadra"):
        banco.g.accoda({"radio": f"!{MIO:08x}", "azione": "riavvia"}, "andrea")
    del banco.iface.nodesByNum[R]["user"]["publicKey"]
    op = fai(banco, azione="leggi", sezioni=["device"])
    assert op["stato"] == "errore" and "chiave pubblica" in op["errore"]
    banco.p.scollega()
    op = fai(banco, azione="leggi", sezioni=["device"])
    assert op["stato"] == "errore" and "non e' collegato" in op["errore"]


def test_solo_dalla_pagina_della_rete(banco):
    corpo = {"radio": f"!{R:08x}", "azione": "riavvia"}
    assert servi(banco, Richiesta("POST", "/mesh2tak/radio/op", corpo, da="192.168.1.50", **{"X-Mesh2tak": "1"}))[0] == 403
    assert servi(banco, Richiesta("POST", "/mesh2tak/radio/op", corpo, utente_ak="", **{"X-Mesh2tak": "1"}))[0] == 403
    assert servi(banco, Richiesta("POST", "/mesh2tak/radio/op", corpo))[0] == 403                # senza intestazione
    assert servi(banco, Richiesta("POST", "/mesh2tak/radio/op", corpo, Origin="https://altro.example",
                                  Host="meshtastic.example", **{"X-Mesh2tak": "1"}))[0] == 403
    assert not banco.lontana.ricevuti
    stato, r = servi(banco, Richiesta("POST", "/mesh2tak/radio/op", corpo, Origin="https://meshtastic.example",
                                      Host="meshtastic.example", **{"X-Mesh2tak": "1"}))
    assert stato == 202 and r["op"]
    for _ in range(300):
        stato, op = servi(banco, Richiesta("GET", f"/mesh2tak/radio/op?id={r['op']}"))
        if op["stato"] == "fatto":
            break
        time.sleep(0.01)
    assert stato == 200 and op["stato"] == "fatto" and op["chi"] == "andrea" and "_param" not in op
    assert servi(banco, Richiesta("GET", "/mesh2tak/radio/op?id=nonce"))[0] == 404
    assert servi(banco, Richiesta("GET", "/mesh2tak/radio/ops"))[1]["operazioni"][-1]["azione"] == "riavvia"
    schema = servi(banco, Richiesta("GET", "/mesh2tak/radio/schema"))[1]
    nomi = {s["nome"]: s for s in schema["sezioni"]}
    assert {"device", "position", "lora", "security", "mqtt", "telemetry"} <= set(nomi)
    assert nomi["security"]["sola_lettura"] and not nomi["lora"]["sola_lettura"] and nomi["mqtt"]["modulo"]
    ruolo = next(c for c in nomi["device"]["campi"] if c["nome"] == "role")
    assert ruolo["tipo"] == "scelta" and "ROUTER" in ruolo["opzioni"]
    json.dumps(schema)


def test_radio_estranee_ricordate_dal_nodo_non_entrano(ponte):  # noqa: F811
    """Dove il canale della squadra e' il primario (indice 0), le radio sentite sul canale pubblico prima di aver
    mandato il loro nome restavano nel nodo con canale 0: all'avvio finivano tra le radio della squadra."""
    iface = IfaceFinta()
    iface.localNode.channels[0].settings.psk = bytes(range(32))      # squadra primario
    iface.localNode.channels[0].settings.name = "CNSAS-VA"
    iface.localNode.channels[1].settings.psk = b"\x01"               # pubblico secondario
    iface.localNode.channels[1].settings.name = ""
    ora = int(time.time())
    iface.nodesByNum.update({
        0x11111111: {"lastHeard": ora - 60, "position": {"latitudeI": 458000000, "longitudeI": 88000000}},
        0x22222222: {"lastHeard": ora - 60, "channel": 1, "user": {"longName": "Estraneo LongFast"},
                     "position": {"latitudeI": 458100000, "longitudeI": 88100000}},
        0x33333333: {"lastHeard": ora - 60, "user": {"longName": "08-ROSSI", "shortName": "08RO"},
                     "position": {"latitudeI": 458200000, "longitudeI": 88200000, "time": ora - 60}},
        # 1.15.0: col nome e il canale 0 come una radio della squadra, ma sentita quando il primario del nodo era
        # ancora il pubblico: il ponte non l'ha mai sentita sul canale della squadra
        0x55555555: {"lastHeard": ora - 60, "user": {"longName": "Estranea di prima", "shortName": "ESTR"},
                     "position": {"latitudeI": 458300000, "longitudeI": 88300000, "time": ora - 60}},
    })
    p = ponte()
    p.squadra.segna(0x33333333, ora - 60)        # questa il ponte l'ha sentita davvero sul canale della squadra
    p.collega(iface)
    s = p.istantanea()
    tutte = ("!11111111", "!22222222", "!33333333", "!55555555")
    assert [r["id"] for r in s["radio_rete"] if r["id"] in tutte] == ["!33333333"]
    assert [r["id"] for r in s["radio"] if r["id"] in tutte] == ["!33333333"], "al TAK solo quella della squadra"
    # un messaggio diretto cifrato (canale 0 per forza) di uno sconosciuto non lo fa entrare
    p.su_pacchetto(pacchetto(0x44444444, m.PORTA_NODEINFO, utente("Sconosciuto", "SCON"), canale=0,
                             pkiEncrypted=True), iface)
    assert "!44444444" not in [r["id"] for r in p.istantanea()["radio_rete"]]
