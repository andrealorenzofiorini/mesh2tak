"""Canali del nodo del ponte. 1.12.0: l'installatore lascia sul nodo solo il canale della squadra. 1.13.0 (scelta
dell'11/11/2026): vale quello che si sceglie dall'app o dalla pagina; il ponte non tocca piu' il suo nodo e gli
aggiornamenti cambiano solo le parti chieste. Tutto come all'installazione solo con "Reimposta la radio"."""
from types import SimpleNamespace

import pytest
from meshtastic.protobuf import channel_pb2, config_pb2, localonly_pb2

import mesh2tak as m
from test_ponte_logica import MIO, TakFinto

P, S, D = (channel_pb2.Channel.Role.PRIMARY, channel_pb2.Channel.Role.SECONDARY, channel_pb2.Channel.Role.DISABLED)


def canale(i, ruolo, psk=b"", nome=""):
    c = channel_pb2.Channel(index=i, role=ruolo)
    c.settings.psk, c.settings.name = psk, nome
    return c


def squadra():
    return canale(0, P, bytes(range(32)), "CNSAS-VA")


class NodoLocale:
    def __init__(self, canali, regione="EU_868", ruolo="CLIENT_MUTE"):
        self.channels = canali + [canale(i, D) for i in range(len(canali), 8)]
        self.scritti = []
        self.localConfig = localonly_pb2.LocalConfig()
        self.localConfig.lora.region = config_pb2.Config.LoRaConfig.RegionCode.Value(regione)
        self.localConfig.device.role = config_pb2.Config.DeviceConfig.Role.Value(ruolo)

    def writeChannel(self, i):  # noqa: N802  (come la libreria meshtastic)
        self.scritti.append((i, channel_pb2.Channel.Role.Name(self.channels[i].role)))


def iface(nodo, nome="Gateway CNSAS Varese", breve="GTSA"):
    return SimpleNamespace(localNode=nodo, myInfo=SimpleNamespace(my_node_num=MIO),
                           nodesByNum={MIO: {"user": {"longName": nome, "shortName": breve}}})


def test_quali_secondari_si_spengono():
    # come dalla 1.11.0: squadra primaria e LongFast secondario
    assert m.secondari_da_spegnere(NodoLocale([squadra(), canale(1, S, b"\x01")])) == [1]
    # con la squadra primaria si spengono anche altri canali privati
    assert m.secondari_da_spegnere(NodoLocale([squadra(), canale(1, S, bytes(16), "ALTRO"),
                                               canale(2, S, b"\x01", "Public")])) == [1, 2]
    # squadra messa a mano come secondario (primario pubblico): solo i pubblici, la squadra resta
    assert m.secondari_da_spegnere(NodoLocale([canale(0, P, b"\x01"), canale(1, S, bytes(32), "CNSAS"),
                                               canale(2, S, b"", "Aperto")])) == [2]
    assert m.secondari_da_spegnere(NodoLocale([squadra()])) == []
    assert m.secondari_da_spegnere(SimpleNamespace(channels=[])) == []
    assert m.nome_canale(canale(1, S, b"\x01")) == "LongFast" and m.nome_canale(canale(3, S)) == "n. 3"


def test_il_ponte_non_tocca_i_canali_del_suo_nodo():
    """1.13.0: un canale secondario acceso dall'app resta acceso; il ponte inoltra solo la squadra (canali = privati)."""
    p = m.Ponte(m.Config(tak_host="takserver.prova"), TakFinto(), None)
    nodo = NodoLocale([squadra(), canale(1, S, b"\x01"), canale(2, S, b"\x01", "Public")])
    p.collega(iface(nodo))
    assert nodo.scritti == [] and nodo.channels[1].role == S and nodo.channels[2].role == S
    assert sorted(p.canali) == [0, 1, 2] and p.canali_ok == {0}, "pubblici visti ma non inoltrati"


def test_leggi_parti():
    assert m.leggi_parti("tutto") == set(m.PARTI_NODO) == m.leggi_parti("")
    assert m.leggi_parti("nessuna") == set()
    assert m.leggi_parti(" Nome, secondari ") == {"nome", "secondari"}
    with pytest.raises(ValueError, match="parti sconosciute: wifi"):
        m.leggi_parti("nome,wifi")


def cosa_cambia(nodo, parti, chiave=None, sul_nodo=("Gateway CNSAS Varese", "GTSA")):
    i = iface(nodo, *sul_nodo)
    return [d for d, _ in m._modifiche_da_fare(i, "Gateway CNSAS Varese", "GTSA", "CNSAS", "CLIENT_MUTE",
                                                (45.8205, 8.8257, 400), lambda: None, chiave, parti)]


def test_solo_le_parti_chieste():
    nodo = NodoLocale([squadra(), canale(1, S, b"\x01")], ruolo="ROUTER")
    dall_app = ("Scelto dall'app", "APP")
    assert cosa_cambia(nodo, set(), sul_nodo=dall_app) == [], "aggiornamento: la radio resta com'e'"
    assert cosa_cambia(nodo, {"secondari"}, sul_nodo=dall_app) == ["canale secondario 'LongFast' spento"]
    assert cosa_cambia(nodo, {"nome"}, sul_nodo=dall_app) == ["nome 'Gateway CNSAS Varese' (GTSA)"]
    tutto = cosa_cambia(nodo, None, sul_nodo=dall_app)
    assert tutto[:3] == ["ruolo CLIENT_MUTE", "nome 'Gateway CNSAS Varese' (GTSA)", "canale secondario 'LongFast' spento"]
    assert tutto[3].startswith("posizione fissa 45.82050, 8.82570") and len(tutto) == 4
    assert nodo.scritti == [], "solo decise, non ancora fatte"


def test_canale_della_squadra_dato_e_nodo_senza_canale_privato():
    nodo = NodoLocale([canale(0, P, b"\x01"), canale(1, S, b"\x01", "Altro")])
    # --canale-squadra in un aggiornamento: solo il primario, i secondari restano
    assert cosa_cambia(nodo, {"canale"}, chiave=bytes(32)) == ["canale della squadra 'CNSAS' con la chiave data"]
    # nessun canale privato e parte "canale": se ne crea uno, e con "secondari" la squadra diventa l'unica
    assert cosa_cambia(nodo, {"canale", "secondari"}) == ["canale privato 'CNSAS' con chiave nuova",
                                                          "canale secondario 'Altro' spento"]
    assert cosa_cambia(nodo, {"secondari"}) == ["canale secondario 'Altro' spento"], "pubblici: si spengono comunque"
