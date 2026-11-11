"""1.12.0: il nodo del ponte ha solo il canale della squadra (scelta di Andrea dell'11/11/2026). I canali secondari si
spengono quando l'installatore configura il nodo e ogni volta che il ponte si collega al nodo."""
from types import SimpleNamespace

from meshtastic.protobuf import channel_pb2

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
    def __init__(self, canali, guasto=False):
        self.channels = canali + [canale(i, D) for i in range(len(canali), 8)]
        self.scritti, self.guasto = [], guasto

    def writeChannel(self, i):  # noqa: N802  (come la libreria meshtastic)
        if self.guasto:
            raise RuntimeError("il nodo non risponde")
        self.scritti.append((i, channel_pb2.Channel.Role.Name(self.channels[i].role)))


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


def ponte():
    p = m.Ponte(m.Config(tak_host="takserver.prova"), TakFinto(), None)
    p.PAUSA_CANALI_S = 0.0
    return p


def iface(nodo):
    return SimpleNamespace(localNode=nodo, myInfo=SimpleNamespace(my_node_num=MIO), nodesByNum={})


def test_il_ponte_spegne_i_secondari_quando_si_collega(caplog):
    caplog.set_level("INFO", logger="mesh2tak")
    p = ponte()
    nodo = NodoLocale([squadra(), canale(1, S, b"\x01"), canale(2, S, b"\x01", "Public")])
    p.collega(iface(nodo))
    assert nodo.scritti == [(1, "DISABLED"), (2, "DISABLED")]
    assert nodo.channels[1].settings.name == "" and bytes(nodo.channels[1].settings.psk) == b""
    assert list(p.canali) == [0] and p.canali_ok == {0}, "il ponte vede solo la squadra"
    assert "Canali secondari del nodo spenti: LongFast, Public" in caplog.text
    nodo.scritti.clear()
    p.collega(iface(nodo))                    # ricollegato (es. dopo una pausa): niente da fare
    assert nodo.scritti == []


def test_squadra_secondaria_e_nodo_che_non_risponde(caplog):
    p = ponte()
    nodo = NodoLocale([canale(0, P, b"\x01"), canale(1, S, bytes(32), "CNSAS")])
    p.collega(iface(nodo))
    assert nodo.scritti == [] and p.canali_ok == {1}, "la squadra messa come secondario non si tocca"
    nodo = NodoLocale([squadra(), canale(1, S, b"\x01")], guasto=True)
    p.collega(iface(nodo))
    assert "Non riesco a spegnere il canale secondario 'LongFast'" in caplog.text
    assert nodo.channels[1].role == S and bytes(nodo.channels[1].settings.psk) == b"\x01", "come e' sul nodo"
    assert p.canali_ok == {0}, "il ponte lavora lo stesso"
