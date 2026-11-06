#!/usr/bin/env python3
"""meshtasticd finto per i test: parla la stessa API TCP (porta 4403) del nodo vero.

Alla richiesta di configurazione manda database dei nodi, canali e configurazione,
poi recita uno scenario di pacchetti ricevuti "dalla radio". Come il firmware vero
accetta un client alla volta e aggiorna il proprio database con i pacchetti ricevuti.

Rete radio finta (--rete CARTELLA): quello che il programma collegato trasmette finisce
nella cartella come file JSON, e ogni nodo finto consegna al proprio programma i file
degli altri nodi, come se li avesse sentiti in radio. Un test puo' depositare li' i
pacchetti di radio inventate. --trasmessi registra cio' che il programma ha trasmesso.
"""
import argparse
import collections
import glob
import base64
import json
import os
import random
import select
import socket
import struct
import sys
import threading
import time
import tty

from meshtastic.protobuf import (admin_pb2, atak_pb2, channel_pb2, config_pb2, mesh_pb2, portnums_pb2,
                                 telemetry_pb2)

SDK = os.environ.get("TAKSDK", "/home/claude/src/takpacket-sdk")

MIO = 0xABCD0001
A, B, C, D, E, F, G, H, I = (0x1111AAAA, 0x2222BBBB, 0x3333CCCC, 0x4444DDDD, 0x5555EEEE,
                             0x6666FFFF, 0x7777AAAA, 0x8888BBBB, 0x9999CCCC)
PSK_PRIVATA = bytes(range(1, 33))
Role = config_pb2.Config.DeviceConfig.Role


def sfoca(lat_i, lon_i, bit):
    """Come il firmware: azzera i bit bassi e somma meta' cella."""
    maschera = (0xFFFFFFFF << (32 - bit)) & 0xFFFFFFFF

    def f(v):
        u = (v & 0xFFFFFFFF) & maschera
        u += 1 << (31 - bit)
        return u - (1 << 32) if u >= (1 << 31) else u
    return f(lat_i), f(lon_i)


def nodo(num, nome, ruolo, canale, lat=None, lon=None, alt=None, sentito=0, batteria=None, salti=None,
         hw=mesh_pb2.HardwareModel.TRACKER_T1000_E, pos_time=None, bit=None):
    ni = mesh_pb2.NodeInfo(num=num, last_heard=int(sentito), channel=canale)
    ni.user.id = f"!{num:08x}"
    ni.user.long_name = nome
    ni.user.short_name = nome[:4]
    ni.user.hw_model = hw
    ni.user.role = ruolo
    if lat is not None:
        ni.position.latitude_i = int(round(lat * 1e7))
        ni.position.longitude_i = int(round(lon * 1e7))
        if bit:
            ni.position.latitude_i, ni.position.longitude_i = sfoca(ni.position.latitude_i,
                                                                    ni.position.longitude_i, bit)
        if alt is not None:
            ni.position.altitude = alt
        ni.position.time = int(pos_time or sentito)
    if batteria is not None:
        ni.device_metrics.battery_level = batteria
    if salti is not None:
        ni.hops_away = salti
    return ni


class SessionePty:
    """Fa sembrare un socket il lato 'radio' di uno pseudo-terminale."""

    def __init__(self, fd):
        self.fd = fd
        self.chiusa = False

    def recv(self, n):
        while not self.chiusa:
            try:
                if select.select([self.fd], [], [], 0.3)[0]:
                    return os.read(self.fd, n)
            except OSError:  # nessun programma ha la porta aperta
                time.sleep(0.2)
        return b""

    def sendall(self, dati):
        if self.chiusa:
            raise OSError("radio in riavvio")
        while dati:
            dati = dati[os.write(self.fd, dati):]

    def shutdown(self, _how):
        self.chiusa = True

    def close(self):
        self.chiusa = True


class NodoFinto:
    def __init__(self, porta, senza_privato=False, primo_scenario=1, nuovo=False, file_stato=None,
                 rete=None, file_trasmessi=None):
        self.porta = porta
        self.rete = rete
        self.file_trasmessi = file_trasmessi
        self.sessione = None
        self.lock_sessione = threading.Lock()
        self.lock_invio = threading.Lock()
        # Come il firmware vero: i messaggi di configurazione aspettano in una coda di 4 posti
        # (Router::fromRadioQueue, MAX_RX_FROMRADIO = 4) e, se ne arrivano di piu' tutti insieme,
        # i piu' vecchi vengono scartati senza avvisare.
        self.coda_admin = collections.deque()
        self.lock_admin = threading.Lock()
        self.scartati_admin = 0
        threading.Thread(target=self.elabora_admin, daemon=True).start()
        self.primo_scenario = primo_scenario
        self.senza_privato = senza_privato
        self.file_stato = file_stato
        self.transazione = False
        self.riavvii = 0
        self.in_riavvio_fino = 0.0
        self.lora = config_pb2.Config.LoRaConfig(
            use_preset=True, modem_preset=config_pb2.Config.LoRaConfig.ModemPreset.LONG_FAST,
            region=config_pb2.Config.LoRaConfig.RegionCode.EU_868)
        self.device = config_pb2.Config.DeviceConfig(role=Role.CLIENT_MUTE)
        self.posizione_cfg = config_pb2.Config.PositionConfig()
        self.canali = []
        for i in range(8):
            ch = channel_pb2.Channel(index=i)
            if i == 0:
                ch.role = channel_pb2.Channel.Role.PRIMARY
                ch.settings.psk = b"\x01"
            elif i == 1 and not senza_privato:
                ch.role = channel_pb2.Channel.Role.SECONDARY
                ch.settings.psk = PSK_PRIVATA
                ch.settings.name = "CNSAS"
            else:
                ch.role = channel_pb2.Channel.Role.DISABLED
            self.canali.append(ch)
        ora = time.time()
        if nuovo:
            # meshtasticd appena installato: regione non impostata, solo il canale pubblico
            self.lora.region = config_pb2.Config.LoRaConfig.RegionCode.UNSET
            self.device.role = Role.CLIENT
            self.canali[1].role = channel_pb2.Channel.Role.DISABLED
            self.canali[1].settings.Clear()
            self.db = {MIO: nodo(MIO, "Meshtastic 0001", Role.CLIENT, 0, sentito=ora,
                                 hw=mesh_pb2.HardwareModel.PORTDUINO)}
            self.db[MIO].user.short_name = "0001"
            self.connessioni = 0
            self.salva_stato()
            return
        self.db = {n.num: n for n in [
            nodo(MIO, "Ponte-CNSAS", Role.CLIENT_MUTE, 0, 45.8205, 8.8257, 400, ora,
                 hw=mesh_pb2.HardwareModel.PORTDUINO),
            nodo(A, "07-BIANCHI", Role.TRACKER, 1, 45.8205, 8.8226, 1050, ora - 600, 87, 1),
            nodo(B, "Nodo-Pubblico", Role.CLIENT, 0, 45.80, 8.85, 300, ora - 300),
            nodo(C, "Vecchio-Nodo", Role.CLIENT, 1, 45.81, 8.81, 300, ora - 5 * 3600),
            nodo(D, "Rip-CampoDeiFiori", Role.ROUTER, 1, 45.8700, 8.7700, 1220, ora - 900,
                 hw=mesh_pb2.HardwareModel.RAK4631),
            nodo(E, "Sfocato", Role.CLIENT, 1, 45.8300, 8.8300, 500, ora - 60, bit=13),
            nodo(G, "15-BIANCHI", Role.TAK_TRACKER, 1, sentito=ora - 7200),
            nodo(H, "radio-atak", Role.TAK, 1, sentito=ora - 7200),
            nodo(I, "radio-vecchia", Role.TAK, 1, sentito=ora - 7200),
        ]}
        self.connessioni = 0

    # ---- protocollo ----
    @staticmethod
    def cornice(fr):
        b = fr.SerializeToString()
        return bytes([0x94, 0xC3, len(b) >> 8, len(b) & 0xFF]) + b

    def manda(self, s, fr):
        with self.lock_invio:
            s.sendall(self.cornice(fr))

    def configurazione(self, s, id_conf):
        fr = mesh_pb2.FromRadio(id=1)
        fr.my_info.my_node_num = MIO
        self.manda(s, fr)
        fr = mesh_pb2.FromRadio()
        fr.metadata.firmware_version = "2.7.15.test"
        self.manda(s, fr)
        for n in self.db.values():
            fr = mesh_pb2.FromRadio()
            fr.node_info.CopyFrom(n)
            self.manda(s, fr)
        fr = mesh_pb2.FromRadio()
        fr.config.lora.CopyFrom(self.lora)
        self.manda(s, fr)
        fr = mesh_pb2.FromRadio()
        fr.config.device.CopyFrom(self.device)
        self.manda(s, fr)
        fr = mesh_pb2.FromRadio()
        fr.config.position.CopyFrom(self.posizione_cfg)
        self.manda(s, fr)
        for ch in self.canali:
            fr = mesh_pb2.FromRadio()
            fr.channel.CopyFrom(ch)
            self.manda(s, fr)
        fr = mesh_pb2.FromRadio()
        fr.config_complete_id = id_conf
        self.manda(s, fr)

    def pacchetto(self, s, da, canale, porta, carico):
        mp = mesh_pb2.MeshPacket()
        setattr(mp, "from", da)
        mp.to = 0xFFFFFFFF
        mp.channel = canale
        mp.id = random.randint(1, 0x7FFFFFFF)
        mp.rx_time = int(time.time())
        mp.rx_snr = 6.25
        mp.hop_start = 3
        mp.hop_limit = 2
        mp.decoded.portnum = porta
        mp.decoded.payload = carico
        fr = mesh_pb2.FromRadio()
        fr.packet.CopyFrom(mp)
        self.manda(s, fr)
        n = self.db.get(da)
        if n is not None:
            n.last_heard = int(time.time())

    def posizione(self, s, da, canale, lat, lon, alt=None, bit=32, velocita=None, rotta=None,
                  dichiara=True):
        p = mesh_pb2.Position(latitude_i=int(round(lat * 1e7)), longitude_i=int(round(lon * 1e7)),
                              time=int(time.time()))
        if bit < 32:
            p.latitude_i, p.longitude_i = sfoca(p.latitude_i, p.longitude_i, bit)
        if dichiara:
            p.precision_bits = bit
        if alt is not None:
            p.altitude = alt
        if velocita is not None:
            p.ground_speed = velocita
        if rotta is not None:
            p.ground_track = rotta
        self.pacchetto(s, da, canale, portnums_pb2.PortNum.POSITION_APP, p.SerializeToString())
        n = self.db.get(da)
        if n is not None:
            n.position.latitude_i, n.position.longitude_i = p.latitude_i, p.longitude_i
            n.position.time = p.time
            if alt is not None:
                n.position.altitude = alt

    def nodeinfo(self, s, da, canale, nome, ruolo=Role.CLIENT):
        u = mesh_pb2.User(id=f"!{da:08x}", long_name=nome, short_name=nome[:4], role=ruolo,
                          hw_model=mesh_pb2.HardwareModel.HELTEC_V3)
        self.pacchetto(s, da, canale, portnums_pb2.PortNum.NODEINFO_APP, u.SerializeToString())
        n = self.db.setdefault(da, mesh_pb2.NodeInfo(num=da))
        n.user.CopyFrom(u)
        n.channel = canale

    def richiesta_posizione(self, s, da, canale):
        """Un'app che chiede la posizione manda un Position vuoto con want_response."""
        mp_payload = mesh_pb2.Position().SerializeToString()
        mp = mesh_pb2.MeshPacket()
        setattr(mp, "from", da)
        mp.to = 0xFFFFFFFF
        mp.channel = canale
        mp.id = random.randint(1, 0x7FFFFFFF)
        mp.rx_time = int(time.time())
        mp.decoded.portnum = portnums_pb2.PortNum.POSITION_APP
        mp.decoded.payload = mp_payload
        mp.decoded.want_response = True
        fr = mesh_pb2.FromRadio()
        fr.packet.CopyFrom(mp)
        self.manda(s, fr)

    def telemetria(self, s, da, canale, batteria):
        t = telemetry_pb2.Telemetry(time=int(time.time()))
        t.device_metrics.battery_level = batteria
        t.device_metrics.voltage = 3.9
        self.pacchetto(s, da, canale, portnums_pb2.PortNum.TELEMETRY_APP, t.SerializeToString())

    # ---- amministrazione (come il firmware, nei limiti di quanto serve ai test) ----
    def salva_stato(self):
        if not self.file_stato:
            return
        stato = {
            "regione": config_pb2.Config.LoRaConfig.RegionCode.Name(self.lora.region),
            "preset": config_pb2.Config.LoRaConfig.ModemPreset.Name(self.lora.modem_preset),
            "ruolo": Role.Name(self.device.role),
            "nome": self.db[MIO].user.long_name, "nome_breve": self.db[MIO].user.short_name,
            "posizione_fissa": self.posizione_cfg.fixed_position,
            "lat": self.db[MIO].position.latitude_i, "lon": self.db[MIO].position.longitude_i,
            "alt": self.db[MIO].position.altitude,
            "canali": [{"indice": c.index, "ruolo": channel_pb2.Channel.Role.Name(c.role), "nome": c.settings.name,
                        "psk_len": len(c.settings.psk), "precisione": c.settings.module_settings.position_precision,
                        "psk": base64.b64encode(bytes(c.settings.psk)).decode()}
                       for c in self.canali],
            "riavvii": self.riavvii,
            "scartati_admin": self.scartati_admin,
        }
        with open(self.file_stato, "w") as f:
            json.dump(stato, f)

    def admin(self, s, mp):
        a = admin_pb2.AdminMessage()
        a.ParseFromString(mp.decoded.payload)
        quale = a.WhichOneof("payload_variant")
        riavvio = False
        if quale == "begin_edit_settings":
            self.transazione = True
        elif quale == "commit_edit_settings":
            self.transazione = False
            riavvio = True
        elif quale == "set_config":
            c = a.set_config.WhichOneof("payload_variant")
            if c == "lora":
                self.lora.CopyFrom(a.set_config.lora)
            elif c == "device":
                self.device.CopyFrom(a.set_config.device)
            elif c == "position":
                self.posizione_cfg.CopyFrom(a.set_config.position)
            riavvio = not self.transazione
        elif quale == "set_owner":
            if a.set_owner.long_name:
                self.db[MIO].user.long_name = a.set_owner.long_name
            if a.set_owner.short_name:
                self.db[MIO].user.short_name = a.set_owner.short_name
        elif quale == "set_channel":
            self.canali[a.set_channel.index].CopyFrom(a.set_channel)
        elif quale == "set_fixed_position":
            self.db[MIO].position.CopyFrom(a.set_fixed_position)
            self.posizione_cfg.fixed_position = True
        self.salva_stato()
        if riavvio:
            # come meshtasticd: dopo il salvataggio si riavvia e per qualche secondo non risponde
            def _riavvia():
                time.sleep(0.5)
                self.riavvii += 1
                self.in_riavvio_fino = time.time() + 3
                self.salva_stato()
                print("[nodo] riavvio dopo le modifiche", flush=True)
                try:
                    s.shutdown(socket.SHUT_RDWR)
                    s.close()
                except OSError:
                    pass
            threading.Thread(target=_riavvia, daemon=True).start()

    # ---- scenari ----
    def scenario(self, s, n):
        n = n - self.primo_scenario + 1  # le connessioni precedenti (es. la prova dell'installatore) non recitano
        ATAK_V2 = portnums_pb2.PortNum.ATAK_PLUGIN_V2
        if n == 1:
            passi = [
                (0.8, lambda: self.posizione(s, A, 1, 45.8215, 8.8236, 1100, velocita=4, rotta=9000000)),
                (1.1, lambda: self.posizione(s, B, 0, 45.8010, 8.8510, 310)),
                (1.4, lambda: self.posizione(s, F, 1, 45.8300, 8.8000, 700)),
                (1.7, lambda: self.nodeinfo(s, F, 1, "12-ROSSI")),
                (2.0, lambda: self.posizione(s, E, 1, 45.8301, 8.8301, 500, bit=13)),
                (2.3, lambda: self.pacchetto(s, G, 1, ATAK_V2, b"\xff" + atak_pb2.TAKPacketV2(
                    cot_type_id=atak_pb2.CotType.CotType_a_f_G_U_C, how=atak_pb2.CotHow.CotHow_m_g,
                    callsign="15-BIANCHI", device_callsign="15-BIANCHI", uid=f"!{G:08x}",
                    team=atak_pb2.Team.Cyan, role=atak_pb2.MemberRole.TeamMember,
                    latitude_i=458888000, longitude_i=87777000, altitude=1500, battery=64,
                    geo_src=atak_pb2.GeoPointSource.GeoPointSource_GPS,
                    alt_src=atak_pb2.GeoPointSource.GeoPointSource_GPS).SerializeToString())),
                (2.6, lambda: self.pacchetto(s, H, 1, ATAK_V2,
                                             open(SDK + "/testdata/golden/pli_full.bin", "rb").read())),
                (2.9, lambda: self.pacchetto(s, I, 1, portnums_pb2.PortNum.ATAK_PLUGIN, atak_pb2.TAKPacket(
                    is_compressed=False,
                    contact=atak_pb2.Contact(callsign="VECCHIO-ATAK", device_callsign="ANDROID-0000000000000009"),
                    group=atak_pb2.Group(role=atak_pb2.MemberRole.TeamLead, team=atak_pb2.Team.Yellow),
                    status=atak_pb2.Status(battery=55),
                    pli=atak_pb2.PLI(latitude_i=457000000, longitude_i=88000000, altitude=300, speed=1,
                                     course=90)).SerializeToString())),
                (3.2, lambda: self.pacchetto(s, H, 1, ATAK_V2,
                                             open(SDK + "/testdata/golden/geochat_simple.bin", "rb").read())),
                (3.5, lambda: self.posizione(s, H, 1, 45.0, 9.0, 200)),
                (3.8, lambda: self.pacchetto(s, H, 1, ATAK_V2, b"\x00\x01\x02\x03garbage")),
                (4.0, lambda: self.richiesta_posizione(s, A, 1)),
            ]
            fine = 4.6
        elif n == 2:
            passi = [(0.8, lambda: self.telemetria(s, A, 1, 80))]
            fine = None
        else:
            passi, fine = [], None
        t0 = time.time()
        for quando, azione in passi:
            time.sleep(max(0, t0 + quando - time.time()))
            azione()
        if fine is not None:
            time.sleep(max(0, t0 + fine - time.time()))
            print(f"[nodo] chiudo la connessione {n} (come quando si collega un'altra app)", flush=True)
            s.shutdown(socket.SHUT_RDWR)
            s.close()
            return True
        return False

    def servi(self, s):
        try:
            self._servi(s)
        finally:
            with self.lock_sessione:
                if self.sessione is s:
                    self.sessione = None

    def _servi(self, s):
        n = 0  # numerata solo quando chiede la configurazione (le prove di porta aperta non contano)
        buf = b""
        scenario_avviato = False
        try:
            while True:
                dati = s.recv(4096)
                if not dati:
                    return
                buf += dati
                while True:
                    i = buf.find(b"\x94\xc3")
                    if i < 0 or len(buf) < i + 4:
                        break
                    lunghezza = struct.unpack(">H", buf[i + 2:i + 4])[0]
                    if len(buf) < i + 4 + lunghezza:
                        break
                    corpo, buf = buf[i + 4:i + 4 + lunghezza], buf[i + 4 + lunghezza:]
                    tr = mesh_pb2.ToRadio()
                    tr.ParseFromString(corpo)
                    if tr.HasField("want_config_id"):
                        if not n:
                            self.connessioni += 1
                            n = self.connessioni
                            print(f"[nodo] connessione {n}", flush=True)
                        self.configurazione(s, tr.want_config_id)
                        with self.lock_sessione:
                            self.sessione = s
                        if not scenario_avviato:
                            scenario_avviato = True
                            threading.Thread(target=self.scenario, args=(s, n), daemon=True).start()
                    elif tr.HasField("disconnect"):
                        return
                    elif tr.HasField("packet") and tr.packet.decoded.portnum == portnums_pb2.PortNum.ADMIN_APP:
                        self.accoda_admin(s, tr.packet)
                    elif tr.HasField("packet"):
                        self.trasmesso(tr.packet)
        except OSError:
            return

    def accoda_admin(self, s, mp):
        with self.lock_admin:
            if len(self.coda_admin) >= 4:
                self.coda_admin.popleft()
                self.scartati_admin += 1
                print("[nodo] coda piena: messaggio di configurazione scartato (fromRadioQ full, drop oldest)",
                      flush=True)
            self.coda_admin.append((s, mp))

    def elabora_admin(self):
        while True:
            with self.lock_admin:
                voce = self.coda_admin.popleft() if self.coda_admin else None
            if voce is None:
                time.sleep(0.01)
                continue
            try:
                self.admin(*voce)
            except OSError:
                pass
            time.sleep(0.05)  # il thread del Router lavora un messaggio alla volta

    # ---- rete radio finta ----
    def trasmesso(self, mp):
        rec = {"da": MIO, "a": mp.to, "canale": mp.channel, "porta": mp.decoded.portnum,
               "carico": mp.decoded.payload.hex(), "hop_limit": mp.hop_limit, "want_ack": mp.want_ack,
               "id": mp.id or random.randint(1, 0x7FFFFFFF), "quando": time.time()}
        print(f"[nodo] trasmesso porta {rec['porta']} canale {rec['canale']} ({len(mp.decoded.payload)} byte)",
              flush=True)
        if self.file_trasmessi:
            with open(self.file_trasmessi, "a") as f:
                f.write(json.dumps(rec) + "\n")
        if self.rete:
            nome = os.path.join(self.rete, f"{time.time():.6f}-{MIO:08x}-{rec['id']}.json")
            with open(nome + ".tmp", "w") as f:
                json.dump(rec, f)
            os.rename(nome + ".tmp", nome)

    def ascolta_rete(self):
        visti = set()
        while True:
            for f in sorted(glob.glob(os.path.join(self.rete, "*.json"))):
                if f in visti:
                    continue
                try:
                    rec = json.load(open(f))
                except (OSError, ValueError):
                    continue
                visti.add(f)
                if rec["da"] == MIO:
                    continue  # il firmware non riconsegna i propri pacchetti
                with self.lock_sessione:
                    s = self.sessione
                if s is None:
                    continue
                try:
                    self.pacchetto(s, rec["da"], rec.get("canale", 1), rec["porta"], bytes.fromhex(rec["carico"]))
                except OSError:
                    pass
            time.sleep(0.1)

    def avvia_pty(self, collegamento):
        """Come una radio Meshtastic collegata via USB: lo stesso protocollo su una porta seriale
        (uno pseudo-terminale, raggiungibile dal percorso 'collegamento')."""
        radio, porta = os.openpty()
        tty.setraw(porta)
        nome = os.ttyname(porta)
        os.chmod(nome, 0o666)  # come una porta del gruppo dialout
        if os.path.lexists(collegamento):
            os.remove(collegamento)
        os.symlink(nome, collegamento)
        print(f"[nodo] radio USB finta su {collegamento} -> {nome}", flush=True)
        while True:
            if time.time() < self.in_riavvio_fino:
                # la radio si sta riavviando: quello che arriva si perde
                if select.select([radio], [], [], 0.2)[0]:
                    os.read(radio, 4096)
                continue
            self.servi(SessionePty(radio))

    def avvia(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", self.porta))
        srv.listen(2)
        print(f"[nodo] in ascolto su {self.porta}", flush=True)
        while True:
            s, _ = srv.accept()
            if time.time() < self.in_riavvio_fino:
                s.close()  # meshtasticd sta ripartendo
                continue
            threading.Thread(target=self.servi, args=(s,), daemon=True).start()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--porta", type=int, default=14403)
    ap.add_argument("--num", type=lambda x: int(x, 0), default=MIO, help="numero del nodo (es. 0xabcd0002)")
    ap.add_argument("--rete", default=None, help="cartella della rete radio finta condivisa tra nodi")
    ap.add_argument("--trasmessi", default=None, help="file JSON-lines con i pacchetti trasmessi")
    ap.add_argument("--senza-privato", action="store_true")
    ap.add_argument("--primo-scenario", type=int, default=1)
    ap.add_argument("--nuovo", action="store_true", help="come meshtasticd appena installato")
    ap.add_argument("--stato", default=None, help="file JSON con lo stato del nodo")
    ap.add_argument("--usb", default=None, metavar="PERCORSO", help="radio USB finta invece della porta TCP")
    a = ap.parse_args()
    MIO = a.num
    nodo_finto = NodoFinto(a.porta, a.senza_privato, a.primo_scenario, a.nuovo, a.stato, a.rete, a.trasmessi)
    if a.rete:
        os.makedirs(a.rete, exist_ok=True)
        threading.Thread(target=nodo_finto.ascolta_rete, daemon=True).start()
    if a.usb:
        nodo_finto.avvia_pty(a.usb)
    else:
        nodo_finto.avvia()
