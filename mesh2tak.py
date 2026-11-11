#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mesh2tak - ponte Meshtastic <-> TAK Server.

Collega il nodo Meshtastic locale (meshtasticd sul Raspberry, oppure una radio
collegata via USB) al TAK Server, su TLS con certificato client.

Dalla radio al TAK Server:
  - POSITION_APP (porta 3): posizioni di qualsiasi radio Meshtastic
  - ATAK_PLUGIN_V2 (porta 78): tutto quello che ATAK, iTAK e TAK Aware mandano via
    Meshtastic (posizioni, chat, segnaposti, forme, percorsi, MEDEVAC, allarmi,
    incarichi), convertito con la libreria ufficiale TAKPacket-SDK (meshtastic-tak)
  - ATAK_PLUGIN (porta 72): formato precedente (firmware 2.7), posizioni e chat

Dal TAK Server alla radio (se il certificato ha anche il permesso di lettura):
  chat, segnaposti, forme, percorsi, MEDEVAC, allarmi e incarichi, sulla porta 78
  del canale privato della squadra. Le posizioni di chi e' collegato via internet
  non vanno in radio: la rete LoRa non le reggerebbe. Gli invii passano da una coda
  con priorita' (allarmi per primi) e un limite al tempo di trasmissione.
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import http.client
import http.server
import json
import logging
import math
import os
import queue
import random
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Set, Tuple

VERSIONE = "1.15.0"
CONFIG_PREDEFINITA = "/etc/mesh2tak/mesh2tak.conf"

log = logging.getLogger("mesh2tak")

PORTA_POSITION = 3
PORTA_NODEINFO = 4
PORTA_TELEMETRY = 67
PORTA_ATAK_V1 = 72
PORTA_ATAK_V2 = 78

NON_NOTO = 9999999.0
BROADCAST = 0xFFFFFFFF

# Byte disponibili per un pacchetto TAK dentro un pacchetto radio: lo stesso limite
# delle app Meshtastic (233 byte del messaggio Data, meno l'intestazione).
MAX_FILO = 225
# Forme, segnaposti e percorsi mandati in radio restano validi almeno 15 minuti,
# come fanno le app Meshtastic: in radio arrivano in ritardo e ATAK scarta quelli scaduti.
STALE_MINIMO_RADIO_S = 15 * 60
TIPI_STATICI = ("b-m-r", "u-d-", "b-m-p-")

# Icona del ponte sulla mappa: traliccio con onde radio dell'insieme OSM, che ATAK ha di serie e
# TAK Aware porta con se'. Chi non ce l'ha mostra il simbolo militare del tipo (installazione).
ICONA_PONTE = "6d781afb-89a6-4c07-b2b9-a89748b6a38f/Landmarks/tower.png"

# Ruoli Meshtastic di infrastruttura: compaiono come installazione fissa,
# non come persona della squadra.
RUOLI_FISSI = {"ROUTER", "ROUTER_LATE", "REPEATER", "ROUTER_CLIENT", "CLIENT_BASE"}

# Colori squadra e ruoli TAK, con i nomi usati da ATAK (stessa tabella di TAKPacket-SDK)
SQUADRE = {1: "White", 2: "Yellow", 3: "Orange", 4: "Magenta", 5: "Red", 6: "Maroon",
           7: "Purple", 8: "Dark Blue", 9: "Blue", 10: "Cyan", 11: "Teal", 12: "Green",
           13: "Dark Green", 14: "Brown"}
RUOLI_TAK = {1: "Team Member", 2: "Team Lead", 3: "HQ", 4: "Sniper", 5: "Medic",
             6: "ForwardObserver", 7: "RTO", 8: "K9"}
SORGENTI = {1: "GPS", 2: "USER", 3: "NETWORK"}

# Parametri radio dei preset Meshtastic (spreading factor, banda in Hz, coding rate 4/x),
# per stimare quanto tempo occupa in aria ogni pacchetto.
PRESET_LORA = {
    "SHORT_TURBO": (7, 500000, 5), "SHORT_FAST": (7, 250000, 5), "SHORT_SLOW": (8, 250000, 5),
    "MEDIUM_FAST": (9, 250000, 5), "MEDIUM_SLOW": (10, 250000, 5), "LONG_TURBO": (11, 500000, 8),
    "LONG_FAST": (11, 250000, 5), "LONG_MODERATE": (11, 125000, 8), "LONG_SLOW": (12, 125000, 8),
    "VERY_LONG_SLOW": (12, 62500, 8),
}


# --------------------------------------------------------------------------
# Configurazione
# --------------------------------------------------------------------------

class ErroreConfig(Exception):
    pass


@dataclass
class Config:
    tak_host: str
    tak_porta: int = 8089
    certificato: str = "/etc/mesh2tak/client.pem"
    chiave: str = "/etc/mesh2tak/client.key"
    ca_server: str = "/etc/mesh2tak/server-ca.pem"
    collegamento: str = "tcp:127.0.0.1:4403"
    canali: str = "privati"
    errore_massimo_m: float = 200.0
    validita_min: float = 90.0
    aggiornamento_min: float = 2.0     # ogni quanto si ripetono al server le posizioni ancora valide
    squadra: str = "Cyan"
    geoide_m: float = 47.0
    includi_ponte: bool = True
    icona_ponte: str = ICONA_PONTE   # vuoto = simbolo militare
    fuso_orario: str = "Europe/Rome"
    invio_radio: bool = True
    tempo_radio_percento: float = 3.0
    riconnessione_nodo_s: int = 120
    # gestione del nodo (meshtasticd) dal browser o dall'app Meshtastic via Wi-Fi, con il ponte in pausa
    gestione_attiva: bool = False
    gestione_porta_pagina: int = 9443          # 0 = niente pagina
    gestione_porta_app: int = 4403             # 0 = niente app via Wi-Fi
    gestione_nodo_pagina: str = "https://127.0.0.1:9444"
    gestione_certificato: str = "/etc/mesh2tak/pagina.pem"
    gestione_chiave: str = "/etc/mesh2tak/pagina.key"
    gestione_pausa_massima_min: float = 30.0
    gestione_chiusura_s: float = 120.0         # pagina zitta o app chiusa da tanto: il ponte riprende
    gestione_silenzio_app_s: float = 360.0     # dopo la pausa massima l'app deve tacere tanto per rientrare
    # accesso da internet: tunnel SSH verso il server della rete Meshtastic (pagina https://meshtastic.<dominio>)
    accesso_attivo: bool = False
    accesso_nome: str = ""                     # es. casa -> gestione su https://meshtastic-casa.<dominio>
    accesso_rete: str = ""                     # es. https://meshtastic.tuodominio.it
    accesso_chiave: str = "/etc/mesh2tak/accesso/chiave"
    accesso_cartella_stato: str = "/var/lib/mesh2tak"   # scrivibile dal servizio: chiave del server
    accesso_errore: str = ""                   # configurazione non valida: accesso spento, il ponte lavora

def _booleano(testo: str) -> bool:
    t = testo.strip().lower()
    if t in ("si", "sì", "s", "yes", "y", "true", "1", "on"):
        return True
    if t in ("no", "n", "false", "0", "off"):
        return False
    raise ValueError(testo)


def carica_config(percorso: str) -> Config:
    cp = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    if not cp.read(percorso, encoding="utf-8"):
        raise ErroreConfig(f"file di configurazione non trovato: {percorso}")

    def leggi(sezione, chiave, tipo=str, obbligatoria=False, predefinito=None):
        if not cp.has_option(sezione, chiave) or cp.get(sezione, chiave).strip() == "":
            if obbligatoria:
                raise ErroreConfig(f"manca '{chiave}' nella sezione [{sezione}]")
            return predefinito
        grezzo = cp.get(sezione, chiave).strip()
        try:
            if tipo is bool:
                return _booleano(grezzo)
            return tipo(grezzo)
        except ValueError:
            raise ErroreConfig(f"valore non valido per '{chiave}' in [{sezione}]: {grezzo}")

    base = Config(tak_host="")
    cfg = Config(
        tak_host=leggi("tak", "host", obbligatoria=True),
        tak_porta=leggi("tak", "porta", int, predefinito=base.tak_porta),
        certificato=leggi("tak", "certificato", predefinito=base.certificato),
        chiave=leggi("tak", "chiave", predefinito=base.chiave),
        ca_server=leggi("tak", "ca_server", predefinito=""),
        collegamento=leggi("meshtastic", "collegamento", predefinito=base.collegamento),
        canali=leggi("meshtastic", "canali", predefinito=base.canali),
        errore_massimo_m=leggi("mappa", "errore_massimo_m", float, predefinito=base.errore_massimo_m),
        validita_min=leggi("mappa", "validita_min", float, predefinito=base.validita_min),
        aggiornamento_min=leggi("mappa", "aggiornamento_min", float, predefinito=base.aggiornamento_min),
        squadra=leggi("mappa", "squadra", predefinito=base.squadra),
        geoide_m=leggi("mappa", "geoide_m", float, predefinito=base.geoide_m),
        includi_ponte=leggi("mappa", "includi_ponte", bool, predefinito=base.includi_ponte),
        icona_ponte=(cp.get("mappa", "icona_ponte").strip() if cp.has_option("mappa", "icona_ponte")
                     else base.icona_ponte),   # vuoto = simbolo militare
        fuso_orario=leggi("mappa", "fuso_orario", predefinito=base.fuso_orario),
        invio_radio=leggi("radio", "invio", bool, predefinito=base.invio_radio),
        tempo_radio_percento=leggi("radio", "tempo_radio_percento", float,
                                   predefinito=base.tempo_radio_percento),
        riconnessione_nodo_s=leggi("avanzate", "riconnessione_nodo_s", int,
                                   predefinito=base.riconnessione_nodo_s),
        gestione_attiva=leggi("gestione", "attiva", bool, predefinito=base.gestione_attiva),
        gestione_porta_pagina=leggi("gestione", "porta_pagina", int, predefinito=base.gestione_porta_pagina),
        gestione_porta_app=leggi("gestione", "porta_app", int, predefinito=base.gestione_porta_app),
        gestione_nodo_pagina=(cp.get("gestione", "nodo_pagina").strip() if cp.has_option("gestione", "nodo_pagina")
                              else base.gestione_nodo_pagina),   # vuoto = radio USB, solo lo stato del ponte
        gestione_certificato=leggi("gestione", "certificato", predefinito=base.gestione_certificato),
        gestione_chiave=leggi("gestione", "chiave", predefinito=base.gestione_chiave),
        gestione_pausa_massima_min=leggi("gestione", "pausa_massima_min", float,
                                         predefinito=base.gestione_pausa_massima_min),
        gestione_chiusura_s=leggi("gestione", "chiusura_s", float, predefinito=base.gestione_chiusura_s),
        gestione_silenzio_app_s=leggi("gestione", "silenzio_app_s", float,
                                      predefinito=base.gestione_silenzio_app_s),
        accesso_attivo=leggi("accesso_esterno", "attivo", bool, predefinito=base.accesso_attivo),
        accesso_nome=leggi("accesso_esterno", "nome", predefinito=""),
        accesso_rete=leggi("accesso_esterno", "rete", predefinito=""),
        accesso_chiave=leggi("accesso_esterno", "chiave", predefinito=base.accesso_chiave),
        accesso_cartella_stato=leggi("accesso_esterno", "cartella_stato", predefinito=base.accesso_cartella_stato),
    )
    if not 0.5 <= cfg.tempo_radio_percento <= 10:
        raise ErroreConfig("tempo_radio_percento va da 0.5 a 10 (la legge permette al massimo il 10%)")
    if cfg.squadra not in SQUADRE.values():
        raise ErroreConfig(f"squadra '{cfg.squadra}' non valida: usa uno di {', '.join(SQUADRE.values())}")
    modo = cfg.canali.strip().lower()
    if modo not in ("privati", "tutti"):
        try:
            indici = [int(x) for x in modo.replace(";", ",").split(",") if x.strip()]
        except ValueError:
            raise ErroreConfig("canali deve essere 'privati', 'tutti' o un elenco di numeri (es. 1,2)")
        if not indici or any(i < 0 or i > 7 for i in indici):
            raise ErroreConfig("i numeri dei canali vanno da 0 a 7")
    tipo_colleg, _, _ = cfg.collegamento.partition(":")
    if tipo_colleg not in ("tcp", "serial"):
        raise ErroreConfig("collegamento deve iniziare con 'tcp:' o 'serial:'")
    for nome in ("certificato", "chiave"):
        if not os.path.isfile(getattr(cfg, nome)):
            raise ErroreConfig(f"file {nome} non trovato: {getattr(cfg, nome)}")
    if cfg.ca_server and not os.path.isfile(cfg.ca_server):
        raise ErroreConfig(f"file ca_server non trovato: {cfg.ca_server}")
    if cfg.validita_min < 5:
        raise ErroreConfig("validita_min deve essere almeno 5")
    if not 0 < cfg.aggiornamento_min <= 60:
        raise ErroreConfig("aggiornamento_min deve essere tra 0 e 60 (minuti)")
    if cfg.gestione_attiva:
        for nome in ("porta_pagina", "porta_app"):
            if not 0 <= getattr(cfg, "gestione_" + nome) <= 65535:
                raise ErroreConfig(f"{nome} in [gestione] non valida")
        if cfg.gestione_porta_app and not cfg.collegamento.startswith("tcp:"):
            raise ErroreConfig("porta_app in [gestione] serve solo con meshtasticd (collegamento tcp:...); "
                               "con la radio USB metti porta_app = 0")
        if cfg.gestione_nodo_pagina and not re.match(r"^https?://[^/]+$", cfg.gestione_nodo_pagina):
            raise ErroreConfig("nodo_pagina in [gestione] deve essere tipo https://127.0.0.1:9444 "
                               "(vuoto con la radio USB: solo lo stato del ponte)")
        if not 1 <= cfg.gestione_pausa_massima_min <= 240:
            raise ErroreConfig("pausa_massima_min va da 1 a 240")
        if cfg.gestione_chiusura_s <= 0 or cfg.gestione_silenzio_app_s <= 0:
            raise ErroreConfig("chiusura_s e silenzio_app_s devono essere maggiori di 0")
        if cfg.gestione_porta_pagina:
            for nome in ("certificato", "chiave"):
                if not os.path.isfile(getattr(cfg, "gestione_" + nome)):
                    raise ErroreConfig(f"file {nome} della pagina di gestione non trovato: "
                                       f"{getattr(cfg, 'gestione_' + nome)}")
    if cfg.accesso_attivo:
        # Facoltativo: un errore qui spegne solo l'accesso da internet, il ponte lavora lo stesso.
        errore = ""
        if not re.match(r"^[a-z0-9](?:[a-z0-9-]{0,18}[a-z0-9])?$", cfg.accesso_nome):
            errore = "nome: solo minuscole, numeri e trattini, al massimo 20 caratteri (es. casa)"
        elif not re.match(r"^https?://[A-Za-z0-9.-]+(:\d+)?/?$", cfg.accesso_rete):
            errore = "rete deve essere tipo https://meshtastic.tuodominio.it"
        elif not (os.path.isfile(cfg.accesso_chiave) and os.path.isfile(cfg.accesso_chiave + ".pub")):
            errore = f"chiave del ponte non trovata ({cfg.accesso_chiave}): rilancia l'installatore con --accesso-esterno"
        elif not (cfg.gestione_attiva and cfg.gestione_porta_pagina):
            errore = "serve la pagina del ponte: in [gestione] attiva = si e porta_pagina = 9443"
        if errore:
            cfg.accesso_attivo, cfg.accesso_errore = False, errore
    return cfg


# --------------------------------------------------------------------------
# Utilita'
# --------------------------------------------------------------------------

def iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def errore_da_bit(bit: int) -> float:
    """Raggio di incertezza (metri) di una posizione ridotta a 'bit' bit di precisione."""
    return 23300.0 / (2.0 ** (bit - 10))


def bit_precisione(lat_i: int, lon_i: int, dichiarati: int = 0) -> int:
    """Bit di precisione di una posizione Meshtastic (32 = precisa).

    Se il pacchetto non lo dichiara (firmware vecchi, posizioni nel database del
    nodo) lo ricava dalle coordinate: il firmware azzera i bit bassi e ci somma
    la meta' della cella, quindi resta una firma '1 seguito da soli zeri'.
    """
    if dichiarati and 0 < dichiarati < 32:
        return int(dichiarati)
    if dichiarati and dichiarati >= 32:
        return 32
    lu, ou = lat_i & 0xFFFFFFFF, lon_i & 0xFFFFFFFF
    for bit in range(1, 24):
        resto = 32 - bit
        maschera = (1 << resto) - 1
        meta = 1 << (resto - 1)
        if (lu & maschera) == meta and (ou & maschera) == meta:
            return bit
    return 32


def pulisci(testo: Optional[str], massimo: int = 200) -> str:
    """Toglie i caratteri non ammessi in XML."""
    if not testo:
        return ""
    buoni = []
    for c in str(testo):
        o = ord(c)
        if o in (9, 10, 13) or 0x20 <= o <= 0xD7FF or 0xE000 <= o <= 0xFFFD or 0x10000 <= o <= 0x10FFFF:
            buoni.append(c)
    return "".join(buoni)[:massimo].strip()


def nome_preset(nome_enum: str) -> str:
    return "".join(p.capitalize() for p in nome_enum.split("_")) if nome_enum else "predefinito"


DICHIARAZIONE = '<?xml version="1.0" encoding="UTF-8"?>'


def _numero(v: float) -> str:
    return f"{v:.1f}"


def cot_evento(*, uid: str, tipo: str, how: str, lat: float, lon: float, t_evento: float,
               stale: float, hae: float = NON_NOTO, ce: float = NON_NOTO, le: float = NON_NOTO,
               callsign: Optional[str] = None, endpoint: bool = False,
               squadra: Optional[str] = None, ruolo: Optional[str] = None,
               batteria: Optional[int] = None, rotta: Optional[float] = None,
               velocita: Optional[float] = None, geosrc: Optional[str] = None,
               altsrc: Optional[str] = None, takv: Optional[Dict[str, str]] = None,
               droid: Optional[str] = None, note: Optional[str] = None,
               segno: Optional[Dict[str, str]] = None, icona: Optional[str] = None) -> bytes:
    ev = ET.Element("event", {
        "version": "2.0", "uid": pulisci(uid, 120), "type": tipo, "how": how,
        "time": iso_utc(t_evento), "start": iso_utc(t_evento), "stale": iso_utc(stale),
    })
    ET.SubElement(ev, "point", {"lat": f"{lat:.7f}", "lon": f"{lon:.7f}", "hae": _numero(hae),
                                "ce": _numero(ce), "le": _numero(le)})
    det = ET.SubElement(ev, "detail")
    if takv:
        ET.SubElement(det, "takv", {k: pulisci(v, 80) for k, v in takv.items() if v})
    if callsign:
        attr = {"callsign": pulisci(callsign, 80)}
        if endpoint:
            attr["endpoint"] = "*:-1:stcp"
        ET.SubElement(det, "contact", attr)
    if droid:
        ET.SubElement(det, "uid", {"Droid": pulisci(droid, 80)})
    if squadra or ruolo:
        g = {}
        if squadra:
            g["name"] = squadra
        if ruolo:
            g["role"] = ruolo
        ET.SubElement(det, "__group", g)
    if batteria:
        ET.SubElement(det, "status", {"battery": str(int(batteria))})
    if rotta is not None or velocita is not None:
        ET.SubElement(det, "track", {"course": f"{(rotta or 0.0):.1f}", "speed": f"{(velocita or 0.0):.2f}"})
    if geosrc or altsrc:
        ET.SubElement(det, "precisionlocation", {"geopointsrc": geosrc or "???", "altsrc": altsrc or "???"})
    if note:
        ET.SubElement(det, "remarks").text = pulisci(note, 400)
    if icona:
        ET.SubElement(det, "usericon", {"iconsetpath": pulisci(icona, 200)})
    if segno:
        ET.SubElement(det, "__mesh2tak", segno)
    return (DICHIARAZIONE + ET.tostring(ev, encoding="unicode")).encode("utf-8")


def cot_ping(uid: str, ora: float) -> bytes:
    ev = ET.Element("event", {"version": "2.0", "uid": uid + "-ping", "type": "t-x-c-t", "how": "h-g-i-g-o",
                              "time": iso_utc(ora), "start": iso_utc(ora), "stale": iso_utc(ora + 20)})
    ET.SubElement(ev, "point", {"lat": "0.0", "lon": "0.0", "hae": _numero(NON_NOTO),
                                "ce": _numero(NON_NOTO), "le": _numero(NON_NOTO)})
    ET.SubElement(ev, "detail")
    return (DICHIARAZIONE + ET.tostring(ev, encoding="unicode")).encode("utf-8")


def cot_chat_diretta(*, da_uid: str, da_callsign: str, a_uid: str, a_callsign: str, testo: str,
                     ora: float) -> bytes:
    """Messaggio privato GeoChat (come lo manda ATAK), per gli avvisi del ponte a chi ha scritto."""
    id_msg = str(uuid.uuid4())
    ev = ET.Element("event", {"version": "2.0", "uid": f"GeoChat.{da_uid}.{a_uid}.{id_msg}", "type": "b-t-f",
                              "how": "h-g-i-g-o", "time": iso_utc(ora), "start": iso_utc(ora),
                              "stale": iso_utc(ora + 86400)})
    ET.SubElement(ev, "point", {"lat": "0.0", "lon": "0.0", "hae": _numero(NON_NOTO),
                                "ce": _numero(NON_NOTO), "le": _numero(NON_NOTO)})
    det = ET.SubElement(ev, "detail")
    chat = ET.SubElement(det, "__chat", {"parent": "RootContactGroup", "groupOwner": "false",
                                         "messageId": id_msg, "chatroom": pulisci(a_callsign, 80),
                                         "id": a_uid, "senderCallsign": pulisci(da_callsign, 80)})
    ET.SubElement(chat, "chatgrp", {"uid0": da_uid, "uid1": a_uid, "id": a_uid})
    ET.SubElement(det, "link", {"uid": da_uid, "type": "a-f-G-I", "relation": "p-p"})
    ET.SubElement(det, "remarks", {"source": f"BAO.F.ATAK.{da_uid}", "to": a_uid,
                                   "time": iso_utc(ora)}).text = pulisci(testo, 400)
    ET.SubElement(ET.SubElement(det, "marti"), "dest", {"callsign": pulisci(a_callsign, 80)})
    ET.SubElement(det, "__mesh2tak", {"h": "avviso", "ponte": da_uid})
    return (DICHIARAZIONE + ET.tostring(ev, encoding="unicode")).encode("utf-8")


def leggi_ora_cot(testo: Optional[str]) -> Optional[float]:
    if not testo:
        return None
    try:
        return datetime.fromisoformat(testo.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


_TAG_EVENTO = re.compile(r"<event\b[^>]*>")
_ATTR_STALE = re.compile(r'\bstale="([^"]*)"')


def allunga_stale(xml: str, ora: float, minimo_s: float = STALE_MINIMO_RADIO_S) -> str:
    """Porta la scadenza di forme, segnaposti e percorsi ad almeno 'minimo_s' secondi da ora."""
    tag = _TAG_EVENTO.search(xml)
    if not tag:
        return xml
    stale = _ATTR_STALE.search(tag.group(0))
    attuale = leggi_ora_cot(stale.group(1)) if stale else None
    if stale is None or (attuale is not None and attuale - ora >= minimo_s):
        return xml
    nuovo_tag = tag.group(0)[:stale.start(1)] + iso_utc(ora + minimo_s) + tag.group(0)[stale.end(1):]
    return xml[:tag.start()] + nuovo_tag + xml[tag.end():]


def marca_dalla_radio(xml: str, impronta: str, radio: str, ponte: str) -> str:
    """Segno nel <detail> degli eventi portati dalla radio al server: gli altri ponti lo
    riconoscono e non lo rimandano in radio (niente giri a vuoto tra radio e server)."""
    segno = f'<__mesh2tak h="{impronta}" radio="{radio}" ponte="{ponte}"/>'
    if "</detail>" in xml:
        return xml.replace("</detail>", segno + "</detail>", 1)
    if "<detail/>" in xml:
        return xml.replace("<detail/>", "<detail>" + segno + "</detail>", 1)
    return xml.replace("</event>", "<detail>" + segno + "</detail></event>", 1)


def estrai_eventi(buf: bytearray) -> List[bytes]:
    """Toglie da 'buf' gli eventi CoT completi arrivati dal TAK Server, nell'ordine."""
    eventi = []
    while True:
        inizio = buf.find(b"<event")
        if inizio < 0:
            if len(buf) > 16:
                del buf[:-16]  # tiene un eventuale "<eve" spezzato a meta'
            return eventi
        if inizio:
            del buf[:inizio]
        fine_tag = buf.find(b">")
        if fine_tag < 0:
            return eventi
        if buf[fine_tag - 1:fine_tag] == b"/":
            eventi.append(bytes(buf[:fine_tag + 1]))
            del buf[:fine_tag + 1]
            continue
        chiusura = buf.find(b"</event>")
        if chiusura < 0:
            return eventi
        eventi.append(bytes(buf[:chiusura + 8]))
        del buf[:chiusura + 8]


def tempo_in_aria(byte_utili: int, preset: str = "LONG_FAST") -> float:
    """Secondi di trasmissione LoRa di un pacchetto Meshtastic con 'byte_utili' di carico."""
    sf, banda, cr = PRESET_LORA.get(preset, PRESET_LORA["LONG_FAST"])
    simbolo = (2 ** sf) / banda
    ottimizza = 1 if simbolo > 0.016 else 0
    byte_radio = byte_utili + 16 + 6  # intestazione Meshtastic + involucro del messaggio
    simboli = 8 + max(math.ceil((8 * byte_radio - 4 * sf + 28 + 16) / (4 * (sf - 2 * ottimizza))) * cr, 0)
    return (16 + 4.25) * simbolo + simboli * simbolo


class Memoria:
    """Chiavi ricordate per 'durata' secondi (doppioni, giri a vuoto tra radio e server)."""

    def __init__(self, durata: float, massimo: int = 5000):
        self.durata = durata
        self.massimo = massimo
        self._voci: "OrderedDict[str, float]" = OrderedDict()
        self._lock = threading.Lock()

    def _pulisci(self, ora: float) -> None:
        while self._voci:
            chiave, quando = next(iter(self._voci.items()))
            if ora - quando <= self.durata and len(self._voci) <= self.massimo:
                break
            self._voci.popitem(last=False)

    def aggiungi(self, chiave: str) -> None:
        ora = time.time()
        with self._lock:
            self._voci.pop(chiave, None)
            self._voci[chiave] = ora
            self._pulisci(ora)

    def __contains__(self, chiave) -> bool:
        with self._lock:
            self._pulisci(time.time())
            return chiave in self._voci


# Un telefono che ha mandato la sua posizione al TAK Server via internet da meno di cosi' e' collegato:
# la copia arrivata via radio e' piu' vecchia e farebbe saltare indietro la sua icona.
COLLEGATO_INTERNET_S = 120

# Cosa arriva dal TAK Server e se va alle radio: categoria -> priorita' (0 = prima)
PRIORITA = {"emergenza": 0, "chat": 1, "medevac": 1, "incarico": 2, "oggetto": 3}


def categoria_dal_server(radice: ET.Element) -> Tuple[Optional[str], str]:
    """(categoria, motivo): categoria None se l'evento non va mandato alle radio."""
    tipo = radice.get("type") or ""
    det = radice.find("detail")
    if det is not None and det.find("__mesh2tak") is not None:
        return None, "arriva gia' dalla rete radio"
    if tipo == "b-t-f":
        return "chat", ""
    if tipo.startswith("b-t-f-"):
        return None, "conferma di consegna o lettura di una chat"
    if tipo.startswith("b-a-"):
        return "emergenza", ""
    if tipo == "b-r-f-h-c":
        return "medevac", ""
    if tipo == "t-s" or tipo.startswith("t-s-"):
        return "incarico", ""
    if tipo == "t-x-d-d":
        return None, "cancellazione (la libreria Meshtastic non trasporta l'oggetto da cancellare)"
    if tipo.startswith("t-"):
        return None, "messaggio di servizio"
    if tipo.startswith(("u-d-", "u-r-b-", "u-rb-", "b-m-p-", "b-m-r", "b-i-x-i")):
        return "oggetto", ""
    if tipo.startswith("a-"):
        if e_posizione_dispositivo(radice):
            return None, "posizione di un dispositivo collegato via internet"
        return "oggetto", ""
    return None, f"tipo {tipo or '?'} non previsto per la radio"


def e_posizione_dispositivo(radice: ET.Element) -> bool:
    """Posizione mandata da un telefono o computer stesso (ATAK, TAK Aware...), non un segnaposto."""
    if not (radice.get("type") or "").startswith("a-"):
        return False
    det = radice.find("detail")
    if det is not None and det.find("__mesh2tak") is not None:
        return False
    return (radice.get("how") or "").startswith("m-") or (det is not None and (
        det.find("takv") is not None or any(c.get("endpoint") for c in det.findall("contact"))))


def mittente_evento(radice: ET.Element) -> Tuple[Optional[str], Optional[str]]:
    """UID e nominativo di chi ha creato o scritto un evento, se ricavabili."""
    det = radice.find("detail")
    if det is None:
        return None, None
    chat = det.find("__chat")
    if chat is not None:
        grp = chat.find("chatgrp")
        return (grp.get("uid0") if grp is not None else None), chat.get("senderCallsign")
    creatore = det.find("creator")
    if creatore is not None and creatore.get("uid"):
        return creatore.get("uid"), creatore.get("callsign")
    for link in det.findall("link"):
        if link.get("relation") == "p-p" and link.get("uid"):
            return link.get("uid"), link.get("parent_callsign")
    return None, None


def nome_evento(radice: ET.Element) -> str:
    det = radice.find("detail")
    contatto = det.find("contact") if det is not None else None
    return pulisci(contatto.get("callsign") if contatto is not None else "", 60)


# --------------------------------------------------------------------------
# Pacchetti TAK della porta 78: libreria ufficiale di Meshtastic (TAKPacket-SDK)
# --------------------------------------------------------------------------

class PacchettoIllegibile(Exception):
    pass


class OggettoTroppoGrande(Exception):
    pass


def _senza_annotazioni(fdp) -> None:
    """Toglie dallo schema le annotazioni field_metadata (servono solo alla documentazione)."""
    def pulisci_opzioni(elemento):
        if not elemento.HasField("options"):
            return
        opzioni = elemento.options
        for campo, _ in opzioni.ListFields():
            if campo.is_extension:
                opzioni.ClearExtension(campo)
        opzioni.DiscardUnknownFields()
        if not opzioni.ListFields():
            elemento.ClearField("options")

    def messaggio(m):
        for campo in m.field:
            pulisci_opzioni(campo)
        for annidato in m.nested_type:
            messaggio(annidato)
        for e in m.enum_type:
            for v in e.value:
                pulisci_opzioni(v)
    del fdp.dependency[:]
    for m in fdp.message_type:
        messaggio(m)
    for e in fdp.enum_type:
        for v in e.value:
            pulisci_opzioni(v)


def carica_sdk_tak():
    """Carica la libreria meshtastic-tak accanto alla libreria meshtastic.

    Tutte e due registrano in protobuf un'annotazione (field_metadata) con lo stesso numero:
    nello stesso programma la seconda non si caricherebbe ("duplicate extension entry").
    Lo schema atak.proto della libreria TAK viene quindi caricato in un archivio protobuf a
    parte e senza quelle annotazioni: il formato dei pacchetti radio resta identico.
    """
    if "meshtastic_tak" in sys.modules:
        return sys.modules["meshtastic_tak"]
    import ast
    import importlib
    import importlib.util
    import types
    from google.protobuf import descriptor_pb2, descriptor_pool
    from google.protobuf.internal import builder
    spec = importlib.util.find_spec("meshtastic_tak")
    if spec is None or not spec.submodule_search_locations:
        raise ImportError("libreria meshtastic-tak non installata")
    cartella = list(spec.submodule_search_locations)[0]
    with open(os.path.join(cartella, "atak_pb2.py"), encoding="utf-8") as f:
        albero = ast.parse(f.read())
    dati = None
    for nodo in ast.walk(albero):
        if (isinstance(nodo, ast.Call) and getattr(nodo.func, "attr", "") == "AddSerializedFile" and nodo.args
                and isinstance(nodo.args[0], ast.Constant) and isinstance(nodo.args[0].value, bytes)):
            dati = nodo.args[0].value
    if dati is None:
        raise ImportError("schema atak.proto non trovato in meshtastic_tak")
    fdp = descriptor_pb2.FileDescriptorProto.FromString(dati)
    _senza_annotazioni(fdp)
    archivio = descriptor_pool.DescriptorPool()
    archivio.AddSerializedFile(fdp.SerializeToString())
    fd = archivio.FindFileByName(fdp.name)
    modulo = types.ModuleType("meshtastic_tak.atak_pb2")
    builder.BuildTopDescriptorsAndMessages(fd, modulo.__name__, modulo.__dict__)
    modulo.DESCRIPTOR = fd
    sys.modules[modulo.__name__] = modulo
    sys.modules.setdefault("meshtastic_tak.field_metadata_pb2", types.ModuleType("meshtastic_tak.field_metadata_pb2"))
    try:
        return importlib.import_module("meshtastic_tak")
    except BaseException:
        sys.modules.pop(modulo.__name__, None)
        raise


class LibreriaTak:
    """CoT XML <-> pacchetti TAK compressi della porta 78, con la stessa libreria delle app Meshtastic."""

    def __init__(self):
        sdk = carica_sdk_tak()
        self.pb = sys.modules["meshtastic_tak.atak_pb2"]
        try:
            from importlib.metadata import version
            self.versione = version("meshtastic-tak")
        except Exception:  # noqa: libreria presa dai sorgenti
            self.versione = getattr(sdk, "__version__", "?")
        self._compressore = sdk.TakCompressor()
        self._costruttore = sdk.CotXmlBuilder()
        self._lettore = sdk.CotXmlParser()
        self._igiene = sdk.CotMeshSanitizer
        self._lock = threading.Lock()  # i compressori zstd non si usano da due thread insieme

    def decodifica(self, carico: bytes):
        if not carico or len(carico) < 2:
            raise PacchettoIllegibile("pacchetto vuoto")
        try:
            with self._lock:
                return self._compressore.decompress(bytes(carico))
        except Exception as e:  # noqa: zstd, protobuf, dizionario sconosciuto...
            raise PacchettoIllegibile(str(e) or type(e).__name__)

    def in_xml(self, pkt) -> str:
        """Evento CoT su una riga, senza dichiarazione <?xml?>, come lo vogliono i server TAK."""
        return self._igiene.normalize_cot_xml(self._costruttore.build(pkt)).strip()

    def da_xml(self, xml: str):
        """(pacchetto, byte da trasmettere). Se non sta in un pacchetto radio nemmeno senza
        le note, OggettoTroppoGrande. Il pacchetto restituito e' quello che va davvero in aria."""
        pulito = self._igiene.strip_non_essential_for_mesh(self._igiene.normalize_cot_xml(xml))
        pkt = self._lettore.parse(pulito)
        with self._lock:
            filo = self._compressore.compress_with_remarks_fallback(pkt, MAX_FILO)
        if filo is None:
            raise OggettoTroppoGrande(pkt.callsign or pkt.uid)
        return self.decodifica(filo), filo

    def impronta(self, pkt) -> str:
        """Impronta del contenuto, uguale per tutti i ponti e per le app: senza la scadenza e
        l'ora di partenza, che ognuno ricalcola dal proprio orologio."""
        copia = self.pb.TAKPacketV2()
        copia.CopyFrom(pkt)
        copia.ClearField("stale_seconds")
        if "start_epoch_lo14" in {f.name for f in copia.DESCRIPTOR.fields}:
            copia.ClearField("start_epoch_lo14")
        return hashlib.sha256(copia.SerializeToString(deterministic=True)).hexdigest()[:24]

    def verifica(self) -> None:
        """Prova completa: chat CoT -> pacchetto radio -> CoT."""
        ora = time.time()
        xml = cot_chat_diretta(da_uid="ANDROID-PROVA-1", da_callsign="PROVA-1", a_uid="ANDROID-PROVA-2",
                               a_callsign="PROVA-2", testo="prova libreria", ora=ora).decode()
        pkt, filo = self.da_xml(xml)
        indietro = self.in_xml(self.decodifica(filo))
        if "prova libreria" not in indietro or not 0 < len(filo) <= MAX_FILO:
            raise RuntimeError("conversione di prova non riuscita")


def descrivi_pkt(pb, pkt) -> str:
    """Descrizione breve per il registro."""
    tipo = tipo_cot_v2(pb, pkt)
    nome = pulisci(pkt.callsign, 60)
    quale = pkt.WhichOneof("payload_variant")
    if quale == "chat":
        if pkt.chat.receipt_type:
            return f"conferma di lettura di {nome or '?'}"
        mittente = pulisci(pkt.chat.to_callsign, 60) or nome or "?"
        testo = pulisci(pkt.chat.message, 60)
        return f"chat di {mittente}: «{testo}»"
    nomi = {"shape": "forma", "marker": "segnaposto", "route": "percorso", "rab": "misura distanza e direzione",
            "casevac": "richiesta MEDEVAC", "task": "incarico", "aircraft": "aereo",
            "taktalk": "messaggio TAK Talk", "taktalk_room": "stanza TAK Talk"}
    if quale == "emergency":
        return f"ALLARME di {nome or '?'} ({tipo})"
    if tipo == "t-x-d-d":
        return "cancellazione di un oggetto"
    if quale in nomi:
        return f"{nomi[quale]} «{nome}»" if nome else nomi[quale]
    if tipo.startswith("a-"):
        return f"punto «{nome}» ({tipo})" if nome else f"punto {tipo}"
    return f"evento {tipo or '?'}"


def tipo_cot_v2(pb, pkt) -> str:
    if pkt.cot_type_id == 0:
        return pkt.cot_type_str or ""
    nome = pb.CotType.Name(pkt.cot_type_id)
    return nome[len("CotType_"):].replace("_", "-") if nome.startswith("CotType_") else ""


def how_v2(pb, pkt) -> str:
    try:
        nome = pb.CotHow.Name(pkt.how)
    except ValueError:
        return "m-g"
    if not nome.startswith("CotHow_") or pkt.how == 0:
        return "m-g"
    return nome[len("CotHow_"):].replace("_", "-")


# --------------------------------------------------------------------------
# Connessione al TAK Server
# --------------------------------------------------------------------------

def contesto_tls(cfg: Config) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cfg.certificato, cfg.chiave)
    ctx.check_hostname = False  # il certificato di TAK Server spesso non riporta il nome pubblico
    if cfg.ca_server:
        ctx.load_verify_locations(cfg.ca_server)
        ctx.verify_mode = ssl.CERT_REQUIRED
        if hasattr(ssl, "VERIFY_X509_PARTIAL_CHAIN"):
            ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    else:
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _keepalive(s: socket.socket) -> None:
    s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for nome, valore in (("TCP_KEEPIDLE", 60), ("TCP_KEEPINTVL", 20), ("TCP_KEEPCNT", 3)):
        if hasattr(socket, nome):
            try:
                s.setsockopt(socket.IPPROTO_TCP, getattr(socket, nome), valore)
            except OSError:
                pass


AVVISI_CERTIFICATO = ("certificate required", "bad certificate", "unknown ca", "certificate unknown",
                      "certificate revoked", "certificate expired", "access denied", "handshake failure",
                      "unsupported certificate")


def spiega_errore_tak(e: BaseException, host: str, porta: int) -> Tuple[str, bool]:
    """Messaggio comprensibile + True se riguarda i certificati (attesa piu' lunga)."""
    if isinstance(e, socket.gaierror):
        return f"non trovo l'indirizzo {host}: il Raspberry e' collegato a internet?", False
    if isinstance(e, ConnectionRefusedError):
        return f"{host}:{porta} rifiuta la connessione (porta chiusa o TAK Server spento)", False
    if isinstance(e, (socket.timeout, TimeoutError)):
        return f"{host}:{porta} non risponde (rete assente o firewall)", False
    if isinstance(e, ssl.SSLCertVerificationError):
        return ("il certificato del TAK Server non corrisponde alla CA salvata. Se il server e' stato "
                "reinstallato o la CA ruotata, rifai l'installazione con un pacchetto nuovo"), True
    if isinstance(e, ssl.SSLError):
        testo = (getattr(e, "reason", None) or str(e)).replace("_", " ").lower()
        if any(a in testo for a in AVVISI_CERTIFICATO):
            return ("il TAK Server ha rifiutato il certificato del ponte (" + testo + "). Controlla nella "
                    "console infra-TAK che il certificato esista e non sia revocato"), True
        return "errore TLS: " + testo, False
    if isinstance(e, ConnectionError) and "chiusa dal server" in str(e):
        return str(e), False
    return f"{type(e).__name__}: {e}", False


class ClienteTAK:
    """Una connessione TLS verso il TAK Server, con riconnessione automatica.

    Tiene l'ultima versione di ogni posizione (per UID): se la connessione cade,
    alla riconnessione rimanda quelle ancora valide invece di accumulare code.
    Chat e oggetti invece partono una volta sola (se la connessione e' giu', aspettano).
    Gli eventi che arrivano dal server vanno a 'su_evento'.
    """

    PING_S = 300
    ATTESA_RETE = (15.0, 300.0)      # attesa minima e massima dopo errori di rete
    ATTESA_CERT = (120.0, 600.0)     # dopo un rifiuto del certificato: piano, per non farsi bannare
    ATTESA_DOPO_CADUTA = 5.0         # se la connessione era stabile
    DURATA_STABILE = 120.0
    MAX_UNA_VOLTA = 500
    MAX_IN_ARRIVO = 2 * 1024 * 1024

    def __init__(self, cfg: Config, uid_ponte: str = "mesh2tak"):
        self.host = cfg.tak_host
        self.porta = cfg.tak_porta
        self.ctx = contesto_tls(cfg)
        self.uid_ponte = uid_ponte
        self._lock = threading.Lock()
        self._sveglia = threading.Event()
        self._fermo = threading.Event()
        self._ultimi: Dict[str, Tuple[bytes, float]] = {}
        self._da_inviare: "OrderedDict[str, None]" = OrderedDict()
        self._una_volta: "deque[Tuple[bytes, float]]" = deque()
        self._in_arrivo = bytearray()
        self._sock: Optional[ssl.SSLSocket] = None
        self._ultimo_ping = 0.0
        self.collegato = threading.Event()
        self.collegato_dal: Optional[float] = None
        self.ultimo_errore: Optional[str] = None
        self.inviati = 0
        self.ricevuti = 0
        self.su_evento: Optional[Callable[[bytes], None]] = None
        self._thread = threading.Thread(target=self._ciclo, name="tak", daemon=True)
        if cfg.ca_server == "":
            log.warning("Nessuna CA del TAK Server configurata: connessione cifrata ma senza verifica del server")

    def avvia(self) -> None:
        self._thread.start()

    def ferma(self) -> None:
        self._fermo.set()
        self._sveglia.set()
        self._thread.join(timeout=10)
        self._chiudi()

    def invia(self, uid: str, xml: bytes, stale: float) -> None:
        with self._lock:
            self._ultimi[uid] = (xml, stale)
            self._da_inviare[uid] = None
            self._da_inviare.move_to_end(uid)
        self._sveglia.set()

    def invia_una_volta(self, xml: bytes, scadenza: float) -> None:
        """Per chat e oggetti: partono una volta, anche dopo una caduta della connessione."""
        with self._lock:
            self._una_volta.append((xml, scadenza))
            while len(self._una_volta) > self.MAX_UNA_VOLTA:
                self._una_volta.popleft()
        self._sveglia.set()

    def _connetti(self) -> ssl.SSLSocket:
        grezzo = socket.create_connection((self.host, self.porta), timeout=20)
        try:
            _keepalive(grezzo)
            s = self.ctx.wrap_socket(grezzo, server_hostname=self.host)
        except BaseException:
            grezzo.close()
            raise
        s.settimeout(20)
        return s

    def _chiudi(self) -> None:
        s, self._sock = self._sock, None
        self.collegato.clear()
        self.collegato_dal = None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass

    def _prossima_attesa(self, attesa: float, cert: bool) -> float:
        minima, massima = self.ATTESA_CERT if cert else self.ATTESA_RETE
        return min(max(attesa * 2, minima), massima)

    def _ciclo(self) -> None:
        attesa = 0.0
        while not self._fermo.is_set():
            if attesa and self._fermo.wait(attesa):
                break
            try:
                self._sock = self._connetti()
            except Exception as e:  # noqa: qualunque errore di rete/TLS
                motivo, cert = spiega_errore_tak(e, self.host, self.porta)
                attesa = self._prossima_attesa(attesa, cert)
                self.ultimo_errore = motivo
                log.error("TAK Server: %s. Riprovo tra %d s", motivo, attesa)
                continue
            inizio = time.time()
            self._in_arrivo.clear()
            self.collegato_dal, self.ultimo_errore = inizio, None
            self.collegato.set()
            log.info("Collegato al TAK Server %s:%d (%s)", self.host, self.porta, self._sock.version())
            with self._lock:
                ora = time.time()
                for uid, (_, stale) in list(self._ultimi.items()):
                    if stale > ora:
                        self._da_inviare[uid] = None
            self._ultimo_ping = time.time()
            try:
                while not self._fermo.is_set():
                    self._servi()
            except Exception as e:  # noqa
                motivo, cert = spiega_errore_tak(e, self.host, self.porta)
                durata = time.time() - inizio
                self._chiudi()
                self.ultimo_errore = motivo
                if durata > self.DURATA_STABILE:
                    attesa = self.ATTESA_DOPO_CADUTA
                else:
                    attesa = self._prossima_attesa(attesa, cert)
                log.error("TAK Server: connessione interrotta (%s). Riprovo tra %d s", motivo, attesa)
        self._chiudi()

    def _leggi(self, s: ssl.SSLSocket) -> None:
        """Legge quello che manda il server (eventi della squadra, pong, offerta di protocollo).
        Lettura non bloccante: in TLS 1.3 arrivano anche messaggi senza dati (ticket di
        sessione) e una recv bloccante resterebbe ferma fino al timeout."""
        s.settimeout(0.0)
        try:
            while True:
                try:
                    dati = s.recv(65536)
                except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError):
                    break
                if not dati:
                    raise ConnectionError("chiusa dal server")
                self._in_arrivo += dati
                if len(self._in_arrivo) > self.MAX_IN_ARRIVO:
                    log.warning("TAK Server: dati non riconosciuti come eventi CoT, li scarto")
                    self._in_arrivo.clear()
                for evento in estrai_eventi(self._in_arrivo):
                    self.ricevuti += 1
                    if self.su_evento is not None:
                        try:
                            self.su_evento(evento)
                        except Exception:  # noqa
                            log.exception("Errore inatteso con un evento dal TAK Server")
        finally:
            s.settimeout(20)

    def _servi(self) -> None:
        self._sveglia.wait(timeout=0.5)
        self._sveglia.clear()
        s = self._sock
        if s is None:
            raise ConnectionError("socket assente")
        self._leggi(s)
        ora = time.time()
        with self._lock:
            uids = list(self._da_inviare.keys())
            self._da_inviare.clear()
            for uid in [u for u, (_, st) in self._ultimi.items() if st <= ora]:
                del self._ultimi[uid]
        for i, uid in enumerate(uids):
            voce = self._ultimi.get(uid)
            if voce is None or voce[1] <= ora:
                continue
            try:
                s.sendall(voce[0])
            except Exception:
                with self._lock:
                    for altro in uids[i:]:
                        self._da_inviare[altro] = None
                raise
            self.inviati += 1
        while True:
            with self._lock:
                if not self._una_volta:
                    break
                xml, scadenza = self._una_volta.popleft()
            if scadenza <= ora:
                continue
            try:
                s.sendall(xml)
            except Exception:
                with self._lock:
                    self._una_volta.appendleft((xml, scadenza))
                raise
            self.inviati += 1
        if ora - self._ultimo_ping >= self.PING_S:
            s.sendall(cot_ping(self.uid_ponte, ora))
            self._ultimo_ping = ora


def prova_tak(cfg: Config) -> Tuple[bool, str]:
    """Verifica che il TAK Server accetti il certificato del ponte."""
    try:
        ctx = contesto_tls(cfg)
    except (ssl.SSLError, OSError) as e:
        return False, f"certificato o chiave del ponte non leggibili: {e}"
    try:
        grezzo = socket.create_connection((cfg.tak_host, cfg.tak_porta), timeout=15)
    except Exception as e:  # noqa
        return False, spiega_errore_tak(e, cfg.tak_host, cfg.tak_porta)[0]
    try:
        s = ctx.wrap_socket(grezzo, server_hostname=cfg.tak_host)
        versione = s.version()
        s.settimeout(4)
        try:
            dati = s.recv(4096)
            if not dati:
                return False, ("il server ha chiuso subito la connessione: il certificato forse non ha "
                               "permessi su nessun canale")
        except (socket.timeout, TimeoutError):
            pass
        s.close()
        return True, f"collegamento riuscito ({versione}), certificato accettato"
    except Exception as e:  # noqa
        return False, spiega_errore_tak(e, cfg.tak_host, cfg.tak_porta)[0]
    finally:
        try:
            grezzo.close()
        except OSError:
            pass


# --------------------------------------------------------------------------
# Collegamento al nodo Meshtastic
# --------------------------------------------------------------------------

def _classe_tcp():
    import meshtastic.tcp_interface as ti

    class TCPSenzaAutoRiconnessione(ti.TCPInterface):
        """Il nodo accetta un solo client TCP alla volta: quando si collega un'altra
        app, chiude la connessione precedente. La libreria si ricollegherebbe dopo
        1 secondo, buttando fuori l'altra app in un ping-pong continuo. Qui la
        connessione resta chiusa e decide il ciclo principale quando riprovare."""

        def _reconnect(self):  # noqa: N802 (nome della libreria)
            self._wantExit = True

        def sendHeartbeat(self):  # noqa: N802
            try:
                super().sendHeartbeat()
            except Exception as e:  # noqa
                log.debug("heartbeat non inviato: %s", e)

    return TCPSenzaAutoRiconnessione


def descrivi_collegamento(collegamento: str) -> str:
    tipo, _, resto = collegamento.partition(":")
    if tipo == "tcp":
        return f"meshtasticd su {resto or '127.0.0.1:4403'}"
    return f"radio USB {resto or '(ricerca automatica)'}"


def apri_interfaccia(cfg):
    """cfg puo' essere la Config del ponte o direttamente la stringa di collegamento."""
    collegamento = cfg if isinstance(cfg, str) else cfg.collegamento
    tipo, _, resto = collegamento.partition(":")
    if tipo == "tcp":
        host, porta = resto or "127.0.0.1", 4403
        if ":" in host:
            host, _, testo_porta = host.rpartition(":")
            porta = int(testo_porta)
        return _classe_tcp()(hostname=host or "127.0.0.1", portNumber=porta, timeout=90)
    import meshtastic.serial_interface as si
    dev = None if resto in ("", "auto") else resto
    if dev is None:
        # Senza percorso la libreria, se non trova la radio, ripiega in silenzio su TCP
        # e con piu' porte chiude il programma: meglio scegliere qui, con un errore chiaro.
        import glob
        trovati = sorted(glob.glob("/dev/serial/by-id/*"))
        if not trovati:
            raise FileNotFoundError("No such file: nessuna radio USB collegata")
        if len(trovati) > 1:
            raise OSError("piu' dispositivi USB collegati, indica quale in 'collegamento': " + ", ".join(trovati))
        dev = trovati[0]
    if not os.path.exists(dev):
        raise FileNotFoundError(f"No such file: {dev}")
    return si.SerialInterface(devPath=dev, timeout=90)


def spiega_errore_nodo(e: BaseException, cfg) -> str:
    dove = descrivi_collegamento(cfg if isinstance(cfg, str) else cfg.collegamento)
    if isinstance(e, ConnectionRefusedError):
        return f"{dove}: connessione rifiutata (meshtasticd e' avviato?)"
    if isinstance(e, (socket.timeout, TimeoutError)):
        return f"{dove}: nessuna risposta"
    testo = str(e)
    if "Timed out waiting for interface config" in testo:
        return f"{dove}: il nodo non ha inviato la sua configurazione in tempo"
    if "could not open port" in testo or "No such file" in testo:
        return (f"{dove}: radio non trovata. Controlla che sia collegata con un cavo USB dati "
                "(i cavi solo di ricarica non bastano) e che nessun altro programma la stia usando")
    if "Resource temporarily unavailable" in testo or "Could not exclusively lock" in testo:
        return f"{dove}: la radio e' gia' usata da un altro programma"
    if "piu' dispositivi USB" in testo:
        return f"{dove}: {testo}"
    return f"{dove}: {type(e).__name__}: {testo}"


# --------------------------------------------------------------------------
# Il ponte
# --------------------------------------------------------------------------

@dataclass
class Canale:
    indice: int
    nome: str
    privato: bool


@dataclass
class Fonte:
    """Quanto serve per ricostruire l'evento CoT di una radio."""
    tipo_fonte: str            # "mesh" (radio Meshtastic) o "tak" (pacchetto TAK)
    uid: str
    lat: float
    lon: float
    hae: float
    ce: float
    t_fix: float
    how: str = "m-g"
    geosrc: Optional[str] = None
    altsrc: Optional[str] = None
    rotta: Optional[float] = None
    velocita: Optional[float] = None
    quota_msl: Optional[float] = None
    precisione_bit: int = 32
    canale: int = 0
    # solo per i pacchetti TAK
    tipo_cot: str = "a-f-G-U-C"
    callsign: Optional[str] = None
    squadra: Optional[str] = None
    ruolo: Optional[str] = None
    batteria: Optional[int] = None
    takv: Optional[Dict[str, str]] = None
    droid: Optional[str] = None
    endpoint: bool = False
    tracker: bool = False


@dataclass
class StatoNodo:
    num: int
    fonte: Fonte
    ultimo_segnale: float
    ultimo_invio: float = 0.0
    nome_inviato: str = ""
    batteria: Optional[int] = None      # ultimi valori mandati al server, per la pagina della rete
    salti: Optional[int] = None


@dataclass
class RadioVista:
    """Una radio della squadra come la sente questo ponte, per l'elenco delle radio della pagina della rete.
    Solo quello che le radio trasmettono da sole sul canale della squadra: il ponte non chiede nulla."""
    num: int
    ultimo: float = 0.0                 # ultimo pacchetto sentito (qualsiasi)
    nome: str = ""
    nome_breve: str = ""
    modello: str = ""                   # es. TRACKER_T1000_E
    ruolo: str = ""                     # es. TAK, CLIENT
    batteria: Optional[int] = None      # 0-100, 101 = alimentata
    tensione: Optional[float] = None
    occupato: Optional[float] = None    # canale occupato dove si trova la radio (%)
    trasmesso: Optional[float] = None   # tempo in cui la radio ha trasmesso nell'ultima ora (%)
    acceso_s: Optional[int] = None      # da quanto e' accesa, al momento delle metriche
    metriche: Optional[float] = None    # quando sono arrivate batteria e canale
    salti: Optional[int] = None         # ultimo pacchetto: 0 = sentita direttamente
    snr: Optional[float] = None         # ultimo pacchetto sentito direttamente
    rssi: Optional[int] = None
    diretto: Optional[float] = None     # quando e' stata sentita direttamente l'ultima volta
    posizione_fissa: bool = False       # dalla 1.15.0: manda una posizione impostata a mano (LOC_MANUAL)
    lat: Optional[float] = None         # ultima posizione (in memoria; esce solo per i ripetitori fissi)
    lon: Optional[float] = None
    ripetuti: int = 0                   # pacchetti di altri che il ponte ha ricevuto ripetuti da questa radio
    ultimo_ripetuto: Optional[float] = None

    @property
    def fisso(self) -> bool:
        """Un ripetitore fisso: ruolo da infrastruttura o posizione impostata a mano."""
        return self.ruolo in RUOLI_FISSI or self.posizione_fissa


# Ruoli Meshtastic dei nodi fissi che fanno da ripetitore (ROUTER_CLIENT e' il vecchio nome)
RUOLI_FISSI = frozenset({"ROUTER", "ROUTER_LATE", "ROUTER_CLIENT", "REPEATER", "CLIENT_BASE"})


RE_ENUM = re.compile(r"^[A-Z][A-Z0-9_]{0,39}$")


def numero(v, minimo: float, massimo: float, cifre: int = 1) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return round(x, cifre) if minimo <= x <= massimo and x == x else None


@dataclass
class VoceRadio:
    """Un evento del TAK Server in attesa di essere trasmesso alle radio."""
    priorita: int
    categoria: str
    uid: str
    descrizione: str
    filo: bytes
    impronta: str
    creata: float
    pronta: float       # non prima: intanto si ascolta se un altro ponte lo trasmette gia'
    scadenza: float     # dopo questa ora l'evento non serve piu'


class CodaRadio:
    """Eventi da trasmettere, i piu' urgenti per primi. Di uno stesso oggetto (UID) resta
    solo l'ultima versione; le chat restano tutte."""

    ETA_MASSIMA_S = 30 * 60

    def __init__(self):
        self._voci: List[VoceRadio] = []
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._voci)

    def metti(self, v: VoceRadio) -> bool:
        """True se ha sostituito una versione precedente dello stesso oggetto."""
        with self._lock:
            if v.categoria != "chat":
                for i, vecchia in enumerate(self._voci):
                    if vecchia.uid == v.uid and vecchia.categoria == v.categoria:
                        v.creata = vecchia.creata
                        self._voci[i] = v
                        return True
            self._voci.append(v)
            return False

    def contiene(self, impronta: str) -> bool:
        with self._lock:
            return any(v.impronta == impronta for v in self._voci)

    def togli_impronta(self, impronta: str) -> List[VoceRadio]:
        with self._lock:
            tolte = [v for v in self._voci if v.impronta == impronta]
            self._voci = [v for v in self._voci if v.impronta != impronta]
            return tolte

    def scadute(self, ora: float) -> List[VoceRadio]:
        with self._lock:
            vecchie = [v for v in self._voci if v.scadenza <= ora or ora - v.creata > self.ETA_MASSIMA_S]
            if vecchie:
                self._voci = [v for v in self._voci if v not in vecchie]
            return vecchie

    def prossima(self, ora: float) -> Optional[VoceRadio]:
        """La prossima da trasmettere (tolta dalla coda), o None."""
        with self._lock:
            pronte = [v for v in self._voci if v.pronta <= ora]
            if not pronte:
                return None
            v = min(pronte, key=lambda x: (x.priorita, x.creata))
            self._voci.remove(v)
            return v

    def guarda(self, ora: float) -> Optional[VoceRadio]:
        with self._lock:
            pronte = [v for v in self._voci if v.pronta <= ora]
            return min(pronte, key=lambda x: (x.priorita, x.creata)) if pronte else None

    def rimetti(self, v: VoceRadio) -> None:
        with self._lock:
            self._voci.append(v)


RE_ID_RADIO = re.compile(r"^![0-9a-f]{8}$")
RE_VERSIONE_ICONE = re.compile(r"^[0-9a-f]{16}$")
RE_PERCORSO_ICONA = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/[A-Za-z][A-Za-z0-9_-]{0,39}/"
                               r"[A-Za-z0-9][A-Za-z0-9_-]{0,59}\.png$")
RE_TIPO_ICONA = re.compile(r"^a-f-[A-Z](-[A-Za-z]{1,3}){0,5}$")


class IconeMappa:
    """Le icone sulla mappa scelte dalla pagina della rete per le radio e per i ponti (dalla 1.11.0). Le manda
    il server dal tunnel quando cambiano; restano su file per i riavvii del ponte."""

    MASSIMO = 500

    def __init__(self, percorso: Optional[str] = None):
        self.percorso = percorso
        self._lock = threading.Lock()
        self.mappa: Dict[str, Tuple[str, str]] = {}      # "!7c3a91e2" -> (tipo CoT, figura dell'insieme di icone)
        self.versione = ""
        if percorso:
            try:
                with open(percorso, encoding="utf-8") as f:
                    self.versione, self.mappa = self.valida(json.load(f))
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as e:
                log.warning("Icone sulla mappa: %s non valido (%s), uso quelle predefinite", percorso, e)

    @classmethod
    def valida(cls, d) -> Tuple[str, Dict[str, Tuple[str, str]]]:
        if not isinstance(d, dict) or d.get("formato") != 1:
            raise ValueError("formato sconosciuto")
        versione, icone = d.get("versione"), d.get("icone")
        if not isinstance(versione, str) or not RE_VERSIONE_ICONE.match(versione) or not isinstance(icone, dict):
            raise ValueError("versione o elenco non validi")
        if len(icone) > cls.MASSIMO:
            raise ValueError("troppe radio")
        mappa = {}
        for radio, v in icone.items():
            if not isinstance(radio, str) or not RE_ID_RADIO.match(radio) or not isinstance(v, dict):
                raise ValueError("radio non valida")
            tipo, percorso = v.get("tipo"), v.get("percorso")
            if not isinstance(tipo, str) or not RE_TIPO_ICONA.match(tipo):
                raise ValueError(f"tipo non valido per {radio}")
            if not isinstance(percorso, str) or not RE_PERCORSO_ICONA.match(percorso):
                raise ValueError(f"icona non valida per {radio}")
            mappa[radio] = (tipo, percorso)
        return versione, mappa

    def imposta(self, corpo: Optional[bytes]) -> Tuple[int, dict, Set[str]]:
        """(stato HTTP, risposta, radio che cambiano icona)."""
        try:
            d = json.loads((corpo or b"").decode("utf-8"))
            versione, mappa = self.valida(d)
        except (ValueError, UnicodeDecodeError) as e:
            return 400, {"errore": f"icone non valide: {e}"}, set()
        with self._lock:
            cambiate = {r for r in set(mappa) | set(self.mappa) if mappa.get(r) != self.mappa.get(r)}
            self.versione, self.mappa = versione, mappa
            if self.percorso:
                try:
                    nuovo = self.percorso + ".nuovo"
                    with open(os.open(nuovo, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644), "w",
                              encoding="utf-8") as f:
                        json.dump(d, f)
                    os.replace(nuovo, self.percorso)
                except OSError as e:
                    log.warning("Icone sulla mappa: non riesco a salvarle (%s): valgono fino al riavvio", e)
        log.info("Icone sulla mappa dalla pagina della rete: %d radio con un'icona scelta (%d cambiate)",
                 len(mappa), len(cambiate))
        return 200, {"versione": versione, "numero": len(mappa)}, cambiate

    def di(self, radio: str) -> Optional[Tuple[str, str]]:
        with self._lock:
            return self.mappa.get(radio)

    def stato(self) -> dict:
        with self._lock:
            return {"versione": self.versione, "numero": len(self.mappa)}


# Sotto questo rapporto segnale/rumore (dB) un pacchetto LoRa non si decifra piu': dipende dallo "spreading factor"
# del preset (SF7 -7,5 dB ... SF12 -20 dB). Quanto il segnale sta sopra questo limite dice quanto e' solido il
# collegamento: e' il margine che la pagina della rete colora nella mappa della copertura.
SF_DEI_PRESET = {"SHORT_TURBO": 7, "SHORT_FAST": 7, "SHORT_SLOW": 8, "MEDIUM_FAST": 9, "MEDIUM_SLOW": 10,
                 "LONG_TURBO": 11, "LONG_FAST": 11, "LONG_MODERATE": 11, "LONG_SLOW": 12, "VERY_LONG_SLOW": 12}


def limite_snr(preset: str) -> float:
    return -2.5 * (SF_DEI_PRESET.get(preset, 11) - 4)


class Copertura:
    """Mappa della copertura (dalla 1.14.0): per ogni quadretto in cui si trovava una radio della squadra quando il
    ponte l'ha sentita, con che segnale l'ha sentita. Il quadretto e' il riquadro della mappa a zoom 17 (circa 210 m
    alle nostre latitudini). Si tengono solo quadretto, segnale e giorno: non quale radio ne' a che ora, cosi' la mappa
    non diventa lo storico degli spostamenti dei volontari.

    Su file ogni quadretto e' [x, y, dirette, somma dei segnali, segnale migliore, potenza migliore, tramite altre
    radio, giorno AAAAMMGG, {ripetitore: volte}]; alla pagina va la media al posto della somma. Dalla 1.15.0 i
    pacchetti arrivati con un salto da un ripetitore fisso contano per quel ripetitore (formato 2; il formato 1,
    senza i ripetitori, si legge ancora)."""

    ZOOM = 17
    MASSIMO = 20000                 # quadretti tenuti (qualche centinaio di kB): oltre, escono i piu' vecchi
    SALVA_OGNI_S = 600.0            # su file al massimo ogni 10 minuti: la microSD non va consumata
    ERRORE_MASSIMO_M = 100.0        # una posizione piu' approssimata finirebbe nel quadretto sbagliato

    def __init__(self, percorso: Optional[str] = None):
        self.percorso = percorso
        self._lock = threading.Lock()
        self.celle: Dict[Tuple[int, int], list] = {}
        self.dal: Optional[int] = None          # da quando raccoglie (dall'ultimo azzeramento)
        self._da_salvare = False
        self._salvata = time.monotonic()
        if percorso:
            try:
                with open(percorso, encoding="utf-8") as f:
                    self._carica(json.load(f))
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as e:
                log.warning("Mappa della copertura: %s non valido (%s), riparto da zero", percorso, e)

    @classmethod
    def quadretto(cls, lat: float, lon: float) -> Tuple[int, int]:
        n = 1 << cls.ZOOM
        lat = max(-85.05112878, min(85.05112878, lat))
        x = int((lon + 180.0) / 360.0 * n)
        y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
        return min(n - 1, max(0, x)), min(n - 1, max(0, y))

    def _carica(self, d) -> None:
        if not isinstance(d, dict) or d.get("formato") not in (1, 2) or d.get("zoom") != self.ZOOM:
            raise ValueError("formato sconosciuto")
        n = 1 << self.ZOOM

        def intero(v, minimo, massimo):
            return isinstance(v, int) and not isinstance(v, bool) and minimo <= v <= massimo

        def segnale(v, minimo, massimo):
            return v is None or (isinstance(v, (int, float)) and not isinstance(v, bool) and minimo <= v <= massimo)
        for c in d.get("celle") or []:
            if len(self.celle) >= self.MASSIMO:
                break
            if (not isinstance(c, list) or len(c) not in (8, 9) or not intero(c[0], 0, n - 1)
                    or not intero(c[1], 0, n - 1) or not intero(c[2], 0, 10 ** 9)
                    or not segnale(c[3], -40.0 * 10 ** 9, 40.0 * 10 ** 9) or not segnale(c[4], -40, 40)
                    or not segnale(c[5], -200, 0) or not intero(c[6], 0, 10 ** 9)
                    or not intero(c[7], 19700101, 29991231) or (c[2] > 0) != (c[4] is not None)):
                continue
            ripetitori = {}
            if len(c) == 9 and isinstance(c[8], dict):
                for chi, volte in list(c[8].items())[:20]:
                    if isinstance(chi, str) and re.fullmatch(r"![0-9a-f]{8}", chi) and intero(volte, 1, 10 ** 9):
                        ripetitori[int(chi[1:], 16)] = volte
            self.celle[(c[0], c[1])] = [c[2], float(c[3] or 0.0), c[4], c[5], c[6], c[7], ripetitori]
        dal = d.get("dal")
        self.dal = dal if intero(dal, 0, 4102444800) else None

    def annota(self, lat: float, lon: float, salti: int, snr: Optional[float], rssi: Optional[int],
               giorno: int, via: Optional[int] = None) -> None:
        """Una posizione ricevuta: diretta (salti 0, con il suo segnale), con un salto da un ripetitore fisso (via)
        o arrivata tramite altre radio."""
        chiave = self.quadretto(lat, lon)
        with self._lock:
            c = self.celle.get(chiave)
            if c is None:
                if len(self.celle) >= self.MASSIMO:
                    self._fai_spazio()
                c = self.celle[chiave] = [0, 0.0, None, None, 0, giorno, {}]
            if salti == 0 and snr is not None:
                c[0] += 1
                c[1] += snr
                c[2] = snr if c[2] is None else max(c[2], snr)
                if rssi is not None:
                    c[3] = rssi if c[3] is None else max(c[3], rssi)
            elif via is not None and (via in c[6] or len(c[6]) < 20):
                c[6][via] = c[6].get(via, 0) + 1
            else:
                c[4] += 1
            c[5] = max(c[5], giorno)
            if self.dal is None:
                self.dal = int(time.time())
            self._da_salvare = True

    def _fai_spazio(self) -> None:
        """Pieno: escono i quadretti visti meno di recente (e, a parita', con meno ricezioni)."""
        via = sorted(self.celle, key=lambda k: (self.celle[k][5],
                                                self.celle[k][0] + self.celle[k][4] + sum(self.celle[k][6].values())))
        for k in via[:max(1, len(via) // 10)]:
            del self.celle[k]

    def dati(self) -> dict:
        """Per la pagina della rete: [x, y, dirette, segnale medio, migliore, potenza migliore, tramite altre radio,
        giorno, {ripetitore: volte}]."""
        with self._lock:
            celle = [[x, y, c[0], round(c[1] / c[0], 1) if c[0] else None, c[2], c[3], c[4], c[5],
                      {f"!{r:08x}": volte for r, volte in sorted(c[6].items())}]
                     for (x, y), c in sorted(self.celle.items(), key=lambda kv: (kv[0][1], kv[0][0]))]
            return {"formato": 2, "zoom": self.ZOOM, "dal": self.dal, "celle": celle}

    def salva(self, subito: bool = False) -> None:
        if not self.percorso:
            return
        with self._lock:
            if not self._da_salvare or (not subito and time.monotonic() - self._salvata < self.SALVA_OGNI_S):
                return
            dati = {"formato": 2, "zoom": self.ZOOM, "dal": self.dal,
                    "celle": [[x, y, c[0], round(c[1], 2), c[2], c[3], c[4], c[5],
                               {f"!{r:08x}": volte for r, volte in c[6].items()}] for (x, y), c in self.celle.items()]}
            self._da_salvare = False
            self._salvata = time.monotonic()
        try:
            nuovo = self.percorso + ".nuovo"
            with open(os.open(nuovo, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
                json.dump(dati, f, separators=(",", ":"))
            os.replace(nuovo, self.percorso)
        except OSError as e:
            log.warning("Mappa della copertura: non riesco a salvarla (%s); riprovo piu' tardi", e)
            with self._lock:
                self._da_salvare = True

    def azzera(self) -> None:
        with self._lock:
            self.celle.clear()
            self.dal = None
            self._da_salvare = False
            self._salvata = time.monotonic()
        if self.percorso:
            try:
                os.unlink(self.percorso)
            except FileNotFoundError:
                pass
            except OSError as e:
                log.warning("Mappa della copertura: non riesco a cancellare %s (%s)", self.percorso, e)


class RadioSquadra:
    """Le radio sentite davvero sul canale della squadra (dalla 1.15.0), con l'ultima volta. Il database del nodo
    Meshtastic segna come "della squadra" anche radio sentite quando il canale principale del nodo era un altro (per
    esempio il pubblico LongFast prima dell'installazione): all'avvio il ponte prende dal database solo queste.
    Su file: {"formato": 1, "radio": {"!1234abcd": ultima volta}}."""

    TIENI_S = 90 * 86400            # una radio non sentita da tre mesi esce
    MASSIMO = 2000
    SALVA_OGNI_S = 600.0            # su file al massimo ogni 10 minuti

    def __init__(self, percorso: Optional[str] = None):
        self.percorso = percorso
        self._lock = threading.Lock()
        self.radio: Dict[int, float] = {}
        self._da_salvare = False
        self._salvata = time.monotonic()
        if percorso:
            try:
                with open(percorso, encoding="utf-8") as f:
                    d = json.load(f)
                if not isinstance(d, dict) or d.get("formato") != 1 or not isinstance(d.get("radio"), dict):
                    raise ValueError("formato sconosciuto")
                limite = time.time() - self.TIENI_S
                for chiave, quando in d["radio"].items():
                    if (isinstance(chiave, str) and re.fullmatch(r"![0-9a-f]{8}", chiave)
                            and isinstance(quando, (int, float)) and not isinstance(quando, bool) and quando > limite):
                        self.radio[int(chiave[1:], 16)] = float(quando)
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as e:
                log.warning("Radio della squadra: %s non valido (%s), riparto da zero", percorso, e)

    def __contains__(self, num) -> bool:
        with self._lock:
            return num in self.radio

    def segna(self, num: int, quando: float) -> None:
        with self._lock:
            if self.radio.get(num, 0.0) >= quando:
                return
            if num not in self.radio and len(self.radio) >= self.MASSIMO:
                del self.radio[min(self.radio, key=self.radio.get)]
            nuova = num not in self.radio
            self.radio[num] = quando
            self._da_salvare = True
        if nuova:
            log.info("Radio della squadra: !%08x sentita sul canale della squadra", num)

    def salva(self, subito: bool = False) -> None:
        if not self.percorso:
            return
        with self._lock:
            if not self._da_salvare or (not subito and time.monotonic() - self._salvata < self.SALVA_OGNI_S):
                return
            dati = {"formato": 1, "radio": {f"!{n:08x}": round(q) for n, q in self.radio.items()}}
            self._da_salvare = False
            self._salvata = time.monotonic()
        try:
            nuovo = self.percorso + ".nuovo"
            with open(os.open(nuovo, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
                json.dump(dati, f, separators=(",", ":"))
            os.replace(nuovo, self.percorso)
        except OSError as e:
            log.warning("Radio della squadra: non riesco a salvarle (%s); riprovo piu' tardi", e)
            with self._lock:
                self._da_salvare = True


class Ponte:
    # Attese casuali prima di un inoltro, per lasciar fare a un altro ponte che l'ha gia' in mano
    ATTESA_VERSO_SERVER = (0.5, 2.5)
    ATTESA_VERSO_RADIO = (2.0, 8.0)
    ATTESA_VERSO_RADIO_ALLARME = (0.2, 1.5)
    SPAZIO_TRA_INVII_S = 3.0          # tra due trasmissioni del ponte, per lasciar ripetere la rete
    RISERVA_RADIO_S = 600.0           # il credito di tempo radio si accumula al massimo per 10 minuti
    MAX_VISTE = 300                   # radio ricordate per l'elenco della pagina della rete

    def __init__(self, cfg: Config, tak: ClienteTAK, sdk: Optional[LibreriaTak]):
        self.cfg = cfg
        self.tak = tak
        self.sdk = sdk
        self.iface = None
        self.mio_num: Optional[int] = None
        self.canali: Dict[int, Canale] = {}
        self.canali_ok: Set[int] = set()
        self.stato: Dict[int, StatoNodo] = {}
        self.viste: Dict[int, RadioVista] = {}       # radio della squadra sentite (per la pagina della rete)
        self.canale_locale: dict = {}               # canale radio misurato dal nodo del ponte
        self.persa = threading.Event()
        self.contatori: Counter = Counter()
        self._segnalati: Set[Tuple[int, str]] = set()
        self._lock = threading.RLock()
        self._ultimo_riepilogo = time.time()
        self.nota_ponte: Optional[str] = None   # es. "IN PAUSA: ..." in testa alle note del ponte
        self.icone = IconeMappa()               # dalla 1.11.0: icone scelte dalla pagina della rete (main: su file)
        self.copertura = Copertura()            # dalla 1.14.0: mappa della copertura (main: su file)
        self.squadra = RadioSquadra()           # dalla 1.15.0: radio sentite sul canale della squadra (main: su file)
        # ultimo nodo visto (resta anche da scollegati, per la pagina della rete)
        self.nodo_nome = ""
        self.nodo_nome_breve = ""
        self.nodo_chiave_pubblica = ""
        self.nodo_collegato_dal: Optional[float] = None
        try:
            from zoneinfo import ZoneInfo
            self.tz = ZoneInfo(cfg.fuso_orario)
        except Exception:  # noqa
            self.tz = None
        # radio <-> server
        self.preset = "LONG_FAST"
        self.hop_limit = 3
        self._inoltrati_al_server = Memoria(600)    # gia' portati dalla radio al server
        self._visti_dal_server = Memoria(900)       # arrivati dal server (o portati la' da un altro ponte)
        self._sentiti_in_radio = Memoria(600)       # sentiti in radio (da app, radio o altri ponti)
        self._trasmessi = Memoria(120)              # trasmessi da noi da poco
        self._avvisati = Memoria(3600)              # avvisi "troppo grande" gia' mandati
        self._collegati_internet = Memoria(COLLEGATO_INTERNET_S)  # UID di telefoni visti via internet
        self.coda_radio = CodaRadio()
        self._dal_server: "queue.Queue[bytes]" = queue.Queue(maxsize=1000)
        self._verso_server: List[Tuple[float, str, bytes, str]] = []
        self._lock_uscite = threading.Lock()
        self._lock_invio = threading.Lock()     # un pacchetto alla volta verso il nodo (anche dalla gestione delle radio)
        self._ultima_trasmissione = 0.0
        self._credito_radio = self.RISERVA_RADIO_S * cfg.tempo_radio_percento / 100.0
        self._ultima_ricarica = time.time()
        self._fermo = threading.Event()
        self._thread_uscite: Optional[threading.Thread] = None

    @property
    def mio_uid(self) -> str:
        return f"!{self.mio_num:08x}" if self.mio_num else "mesh2tak"

    # ---- eventi dalla libreria meshtastic (thread di pubblicazione) ----

    def su_pacchetto(self, packet, interface=None):
        if interface is not self.iface or self.iface is None:
            return
        try:
            self._gestisci(packet)
        except Exception:  # noqa
            log.exception("Errore inatteso nel gestire un pacchetto")

    def su_perdita(self, interface=None):
        if interface is self.iface:
            self.persa.set()

    # ---- collegamento ----

    def collega(self, iface) -> None:
        # dalla 1.13.0 il ponte non tocca le impostazioni del suo nodo: vale quello che si sceglie dall'app o dalla
        # pagina (anche un canale secondario acceso). Le rimette come all'installazione solo "Reimposta la radio".
        canali, ok = leggi_canali(iface, self.cfg)
        preset, salti = "LONG_FAST", 3
        try:
            from meshtastic.protobuf import config_pb2
            lora = iface.localNode.localConfig.lora
            if lora.use_preset:
                preset = config_pb2.Config.LoRaConfig.ModemPreset.Name(lora.modem_preset)
            salti = int(lora.hop_limit) or 3
        except Exception:  # noqa
            pass
        with self._lock:
            self.canali, self.canali_ok = canali, ok
            self.mio_num = iface.myInfo.my_node_num if getattr(iface, "myInfo", None) else None
            self.preset, self.hop_limit = preset, salti
            self.persa.clear()
            self.iface = iface
            utente = self._info(self.mio_num).get("user", {}) if self.mio_num else {}
            self.nodo_nome = pulisci(utente.get("longName"), 60)
            self.nodo_nome_breve = pulisci(utente.get("shortName"), 8)
            self.nodo_collegato_dal = time.time()
            # la chiave pubblica del nodo: e' la "chiave amministratore" da mettere sulle radio da gestire
            self.nodo_chiave_pubblica = pulisci(utente.get("publicKey"), 64) or self.nodo_chiave_pubblica
        self._descrivi_nodo()
        self._carica_database()
        self._carica_viste()

    def scollega(self) -> None:
        with self._lock:
            self.iface = None
            self.nodo_collegato_dal = None

    def _descrivi_nodo(self) -> None:
        info = self._info(self.mio_num) if self.mio_num else {}
        nome = info.get("user", {}).get("longName", "?")
        log.info("Collegato al nodo Meshtastic '%s' (!%08x) - %s", nome, self.mio_num or 0,
                 descrivi_collegamento(self.cfg.collegamento))
        for c in self.canali.values():
            stato = "INOLTRATO" if c.indice in self.canali_ok else "ignorato"
            log.info("  canale %d '%s' (%s): %s", c.indice, c.nome,
                     "privato" if c.privato else "pubblico, chiave nota a tutti", stato)
        if not self.canali_ok:
            log.warning("Nessun canale da inoltrare con 'canali = %s': non arrivera' nessuna posizione. "
                        "Crea un canale privato della squadra anche sul Raspberry, o cambia 'canali' "
                        "in %s", self.cfg.canali, CONFIG_PREDEFINITA)
        if self.cfg.invio_radio and self.sdk is not None:
            canale = self._canale_invio()
            if canale is None:
                log.warning("Nessun canale privato su cui trasmettere: chat e oggetti dal TAK Server non "
                            "andranno alle radio")
            else:
                log.info("  dal TAK Server alle radio: canale %d '%s', preset %s, al massimo %.1f%% del "
                         "tempo radio", canale, self.canali[canale].nome, self.preset,
                         self.cfg.tempo_radio_percento)

    def _canale_invio(self) -> Optional[int]:
        """Il canale privato della squadra su cui trasmettere (il primo inoltrato)."""
        privati = sorted(i for i, c in self.canali.items() if c.privato and i in self.canali_ok)
        return privati[0] if privati else None

    def _info(self, num: Optional[int]) -> dict:
        iface = self.iface
        if iface is None or num is None:
            return {}
        try:
            return iface.nodesByNum.get(num) or {}
        except Exception:  # noqa
            return {}

    def _carica_database(self) -> None:
        """All'avvio inoltra le radio gia' note al nodo e sentite di recente."""
        iface = self.iface
        if iface is None:
            return
        ora = time.time()
        validita = self.cfg.validita_min * 60
        inoltrate = 0
        for num, info in list(iface.nodesByNum.items()):
            pos = info.get("position") or {}
            lat_i, lon_i = pos.get("latitudeI"), pos.get("longitudeI")
            if not lat_i and not lon_i:
                continue
            ponte = num == self.mio_num
            if ponte and not self.cfg.includi_ponte:
                continue
            sentito = ora if ponte else float(info.get("lastHeard") or 0)
            if not ponte:
                if ora - sentito > validita:
                    continue
                if num not in self.squadra:     # dalla 1.15.0: solo radio sentite sul canale della squadra
                    continue
                if info.get("user", {}).get("role") == "TAK":
                    continue
            fonte = self._fonte_mesh(num, pos, info.get("channel", 0), sentito)
            if fonte is None:
                continue
            with self._lock:
                self.stato[num] = StatoNodo(num, fonte, sentito)
                self._invia(self.stato[num], ora)
            inoltrate += 1
        log.info("Dal database del nodo: %d radio con posizione recente inoltrate", inoltrate)

    def _canale_noto(self, info: dict) -> bool:
        """La radio ricordata dal nodo e' davvero del canale della squadra. Il nodo segna il canale di una radio
        solo quando ne riceve il nome (NodeInfo): prima vale 0, che dove il canale della squadra e' il primario
        faceva passare per radio della squadra anche gli estranei sentiti sul canale pubblico (fino alla 1.9.0)."""
        return bool(info.get("user")) and int(info.get("channel", 0) or 0) in self.canali_ok

    def _carica_viste(self) -> None:
        """All'avvio, le radio della squadra che il nodo conosce gia' (con l'ultimo stato che ricorda)."""
        iface = self.iface
        if iface is None:
            return
        for num, info in list(iface.nodesByNum.items()):
            # dalla 1.15.0 solo le radio che il ponte ha sentito davvero sul canale della squadra: il canale che il
            # nodo ricorda puo' essere quello di quando il suo canale principale era un altro
            if not isinstance(num, int) or num == self.mio_num or info.get("viaMqtt") or num not in self.squadra:
                continue
            sentito = numero(info.get("lastHeard"), 1, 4102444800, 0)
            if not sentito:
                continue
            with self._lock:
                v = self._vista_di(num)
                if v.ultimo >= sentito:
                    continue
                v.ultimo = sentito
                self._utente(v, info.get("user") or {})
                if v.lat is None:
                    self._posizione_vista(v, info.get("position") or {})
                if v.metriche is None:
                    self._metriche(v, info.get("deviceMetrics") or {}, sentito)
                salti = info.get("hopsAway")
                if isinstance(salti, int) and 0 <= salti <= 7:
                    v.salti = salti
                    if salti == 0 and info.get("snr") is not None:
                        v.snr, v.diretto = numero(info.get("snr"), -40, 40, 2), sentito

    def _vista_di(self, num: int) -> RadioVista:
        v = self.viste.get(num)
        if v is None:
            if len(self.viste) >= self.MAX_VISTE:
                del self.viste[min(self.viste.values(), key=lambda x: x.ultimo).num]
            v = self.viste[num] = RadioVista(num)
        return v

    @staticmethod
    def _utente(v: RadioVista, u: dict) -> None:
        if not u:
            return
        v.nome = pulisci(u.get("longName"), 60) or v.nome
        v.nome_breve = pulisci(u.get("shortName"), 8) or v.nome_breve
        modello, ruolo = str(u.get("hwModel") or ""), str(u.get("role") or "CLIENT")
        v.modello = modello if RE_ENUM.match(modello) else v.modello
        v.ruolo = ruolo if RE_ENUM.match(ruolo) else v.ruolo

    @staticmethod
    def _metriche(v: RadioVista, dm: dict, quando: float) -> None:
        if not dm:
            return
        completo = "uptimeSeconds" in dm      # un rapporto vero: i valori a zero il formato li omette
        b = dm.get("batteryLevel")
        if isinstance(b, int) and 0 < b <= 101:
            v.batteria = b
        v.tensione = numero(dm.get("voltage"), 0.5, 60, 2) if dm.get("voltage") else v.tensione
        for campo, chiave in (("occupato", "channelUtilization"), ("trasmesso", "airUtilTx")):
            if chiave in dm or completo:
                setattr(v, campo, numero(dm.get(chiave, 0.0), 0, 100, 1 if campo == "occupato" else 2))
        acceso = dm.get("uptimeSeconds")
        if isinstance(acceso, int) and acceso >= 0:
            v.acceso_s = acceso
        v.metriche = quando

    def _vista(self, p: dict, num: int, canale: int, rx: float, porta: int) -> None:
        """Ogni pacchetto di una radio della squadra aggiorna la sua scheda per la pagina della rete."""
        if num == self.mio_num or canale not in self.canali_ok or p.get("viaMqtt"):
            return
        if p.get("pkiEncrypted") and num not in self.viste:
            return      # messaggio diretto cifrato per il nodo: non dice su che canale sta chi lo manda
        d = p.get("decoded") or {}
        if not p.get("pkiEncrypted"):
            self.squadra.segna(num, rx)     # decifrato con la chiave del canale: e' davvero della squadra
        with self._lock:
            v = self._vista_di(num)
            v.ultimo = max(v.ultimo, rx)
            inizio, resto = p.get("hopStart"), p.get("hopLimit", 0)
            if isinstance(inizio, int) and 0 < inizio <= 7 and isinstance(resto, int) and 0 <= resto <= inizio:
                v.salti = inizio - resto
                if v.salti == 0 and p.get("rxRssi"):        # sentita direttamente: segnale del suo trasmettitore
                    v.snr = numero(p.get("rxSnr", 0.0), -40, 40, 2)
                    v.rssi = int(numero(p["rxRssi"], -200, 0, 0) or 0) or None
                    v.diretto = rx
            if porta == PORTA_NODEINFO:
                self._utente(v, d.get("user") or {})
            elif porta == PORTA_TELEMETRY:
                self._metriche(v, (d.get("telemetry") or {}).get("deviceMetrics") or {}, rx)
            elif porta == PORTA_POSITION:
                self._posizione_vista(v, d.get("position") or {})

    @staticmethod
    def _posizione_vista(v: RadioVista, pos: dict) -> None:
        """Dalla 1.15.0: posizione impostata a mano (nodo fisso) e ultima posizione, per i ripetitori sulla mappa."""
        lat_i, lon_i = pos.get("latitudeI"), pos.get("longitudeI")
        if not isinstance(lat_i, int) or not isinstance(lon_i, int) or (lat_i == 0 and lon_i == 0):
            return
        lat, lon = lat_i * 1e-7, lon_i * 1e-7
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            v.lat, v.lon = lat, lon
            v.posizione_fissa = pos.get("locationSource") == "LOC_MANUAL"

    def _canale_del_ponte(self, p: dict, rx: float) -> None:
        """Il nodo del ponte manda ogni minuto le sue misure: quanto e' occupato il canale, quanto trasmette."""
        t = (p.get("decoded") or {}).get("telemetry") or {}
        for chiave in ("deviceMetrics", "localStats"):
            m = t.get(chiave)
            if not m or "uptimeSeconds" not in m:
                continue
            with self._lock:
                c = self.canale_locale
                c["occupato"] = numero(m.get("channelUtilization", 0.0), 0, 100, 1)
                c["trasmesso"] = numero(m.get("airUtilTx", 0.0), 0, 100, 2)
                if chiave == "localStats" and m.get("noiseFloor"):
                    c["rumore"] = numero(m.get("noiseFloor"), -200, 0, 0)
                c["quando"] = rx

    # ---- pacchetti ----

    def _gestisci(self, p: dict) -> None:
        num = p.get("from")
        grezzo = p.get("raw")
        if not num or num == BROADCAST or grezzo is None or "decoded" not in p:
            return
        porta = grezzo.decoded.portnum
        canale = int(p.get("channel", 0) or 0)
        ora = time.time()
        rx = float(p.get("rxTime") or ora)
        if rx > ora + 300 or rx < ora - 86400:
            rx = ora
        ripetitore = None
        if num == self.mio_num:
            if porta == PORTA_TELEMETRY:
                self._canale_del_ponte(p, rx)
        else:
            self._vista(p, num, canale, rx, porta)
            ripetitore = self._ripetuto_da(p, num, rx)
        fonte = None
        if porta == PORTA_POSITION:
            pos = p["decoded"].get("position") or {}
            if pos.get("latitudeI") or pos.get("longitudeI") or not p["decoded"].get("wantResponse"):
                fonte = self._posizione(num, pos, canale, rx)  # una richiesta di posizione vuota non e' una posizione
        elif porta == PORTA_ATAK_V2:
            fonte = self._tak_v2(num, bytes(grezzo.decoded.payload), canale, rx)
        elif porta == PORTA_ATAK_V1:
            fonte = self._tak_v1(num, bytes(grezzo.decoded.payload), canale, rx)
        if fonte is not None and num != self.mio_num:
            self._annota_copertura(p, fonte, rx, ripetitore)
        self._attivita(num, rx, porta)

    def _ripetuto_da(self, p: dict, num: int, rx: float) -> Optional[int]:
        """Dalla 1.15.0: quale radio della squadra ha ripetuto il pacchetto arrivato al ponte. Il pacchetto porta
        solo l'ultimo byte del numero di chi l'ha ritrasmesso per ultimo (firmware 2.6 e successivi): vale se una
        sola radio della squadra finisce con quel byte. E' anche il segno che un ripetitore e' acceso."""
        inizio, resto, byte = p.get("hopStart"), p.get("hopLimit", 0), p.get("relayNode")
        if (p.get("viaMqtt") or not isinstance(byte, int) or not 0 < byte <= 0xFF or not isinstance(inizio, int)
                or not isinstance(resto, int) or not 0 <= resto < inizio <= 7):
            return None
        with self._lock:
            chi = [v for v in self.viste.values() if v.num & 0xFF == byte and v.num not in (num, self.mio_num)]
            if len(chi) != 1:
                return None
            v = chi[0]
            v.ripetuti += 1
            v.ultimo_ripetuto = max(v.ultimo_ripetuto or 0.0, rx)
            return v.num

    def _annota_copertura(self, p: dict, fonte: "Fonte", rx: float, ripetitore: Optional[int] = None) -> None:
        """Mappa della copertura (dalla 1.14.0): dove si trovava la radio e con che segnale il ponte l'ha sentita.
        Il segnale misurato e' quello dell'ultimo trasmettitore: vale per il punto della radio solo se e' arrivata
        diretta. Arrivata con un salto da un ripetitore fisso (dalla 1.15.0), il quadretto conta per quel
        ripetitore, senza segnale; altrimenti dice soltanto che da li' si arriva alla rete."""
        if p.get("viaMqtt") or (fonte.ce != NON_NOTO and fonte.ce > Copertura.ERRORE_MASSIMO_M):
            return
        inizio, resto = p.get("hopStart"), p.get("hopLimit", 0)
        if not (isinstance(inizio, int) and 0 < inizio <= 7 and isinstance(resto, int) and 0 <= resto <= inizio):
            return      # firmware che non dice i salti: non si sa se e' arrivata diretta
        salti, snr, rssi, via = inizio - resto, None, None, None
        if salti == 0:
            if not p.get("rxRssi"):
                return  # nessuna misura del segnale
            snr = numero(p.get("rxSnr", 0.0), -40, 40, 2)
            rssi = int(numero(p["rxRssi"], -200, 0, 0) or 0) or None
        elif salti == 1 and ripetitore is not None:
            with self._lock:
                v = self.viste.get(ripetitore)
                via = ripetitore if v is not None and v.fisso else None
        quando = datetime.fromtimestamp(rx, self.tz) if self.tz else datetime.fromtimestamp(rx)
        self.copertura.annota(fonte.lat, fonte.lon, salti, snr, rssi, int(quando.strftime("%Y%m%d")), via)

    def copertura_per_la_pagina(self, azzera: bool = False, chi: str = "") -> dict:
        """/mesh2tak/copertura.json: i quadretti, dove sta il ponte e il limite del segnale del suo preset."""
        if azzera:
            self.copertura.azzera()
            log.info("Mappa della copertura azzerata dalla pagina della rete (%s)", chi or "?")
        d = self.copertura.dati()
        pos = self._info(self.mio_num).get("position") or {}
        lat_i, lon_i = pos.get("latitudeI"), pos.get("longitudeI")
        d["ponte"] = ({"lat": round(lat_i * 1e-7, 4), "lon": round(lon_i * 1e-7, 4)}
                      if isinstance(lat_i, int) and isinstance(lon_i, int) and (lat_i or lon_i) else None)
        d["preset"], d["limite_snr"] = self.preset, limite_snr(self.preset)
        with self._lock:      # dalla 1.15.0 i ripetitori fissi, con la loro posizione (solo qui, dietro Authentik)
            d["ripetitori"] = [{"id": f"!{v.num:08x}", "nome": v.nome or self._nome(v.num), "nome_breve": v.nome_breve,
                                "lat": round(v.lat, 4), "lon": round(v.lon, 4)}
                               for v in sorted(self.viste.values(), key=lambda v: v.num)
                               if v.fisso and v.lat is not None and v.lon is not None]
        return d

    def _scarta(self, num: int, motivo: str, spiegazione: str) -> None:
        self.contatori["scartate_" + motivo] += 1
        chiave = (num, motivo)
        if chiave not in self._segnalati:
            self._segnalati.add(chiave)
            log.info("Ignoro %s (!%08x): %s", self._nome(num), num, spiegazione)
        else:
            log.debug("Ignoro !%08x: %s", num, spiegazione)

    def _nome(self, num: int) -> str:
        u = self._info(num).get("user", {})
        return pulisci(u.get("longName") or u.get("shortName") or f"Meshtastic {num & 0xFFFF:04x}", 80)

    def _canale_ok(self, num: int, canale: int) -> bool:
        if num == self.mio_num or canale in self.canali_ok:
            return True
        c = self.canali.get(canale)
        nome = c.nome if c else f"n. {canale}"
        self._scarta(num, "canale", f"la sua posizione arriva sul canale '{nome}', non inoltrato")
        return False

    def _fonte_mesh(self, num: int, pos: dict, canale: int, rx: float) -> Optional[Fonte]:
        lat_i, lon_i = int(pos.get("latitudeI") or 0), int(pos.get("longitudeI") or 0)
        if lat_i == 0 and lon_i == 0:
            self._scarta(num, "senza_gps", "manda posizioni vuote (GPS senza fix?)")
            return None
        lat, lon = lat_i * 1e-7, lon_i * 1e-7
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        bit = bit_precisione(lat_i, lon_i, int(pos.get("precisionBits") or 0))
        ce = NON_NOTO
        if bit < 32:
            ce = errore_da_bit(bit)
            if ce > self.cfg.errore_massimo_m:
                self._scarta(num, "imprecisa", f"posizione volutamente approssimata (±{ce:.0f} m): sulla radio "
                             "attiva la posizione precisa per il canale della squadra")
                return None
        quota_msl = None
        if "altitudeHae" in pos:
            hae = float(pos["altitudeHae"])
        elif "altitude" in pos:
            quota_msl = float(pos["altitude"])
            sep = pos.get("altitudeGeoidalSeparation")
            hae = quota_msl + (float(sep) if sep is not None else self.cfg.geoide_m)
        else:
            hae = NON_NOTO
        t_pos = pos.get("time") or pos.get("timestamp")
        ora = time.time()
        t_fix = float(t_pos) if t_pos and ora - 7 * 86400 < float(t_pos) < ora + 300 else rx
        manuale = pos.get("locationSource") == "LOC_MANUAL"
        velocita = rotta = None
        if "groundSpeed" in pos:
            velocita = float(pos["groundSpeed"]) / 3.6  # km/h -> m/s
        if "groundTrack" in pos:
            rotta = (float(pos["groundTrack"]) / 1e5) % 360
        return Fonte(tipo_fonte="mesh", uid=f"!{num:08x}", lat=lat, lon=lon, hae=hae, ce=ce, t_fix=t_fix,
                     how="h-e" if manuale else "m-g", geosrc="USER" if manuale else "GPS",
                     altsrc="USER" if manuale else "GPS", rotta=rotta, velocita=velocita,
                     quota_msl=quota_msl, precisione_bit=bit, canale=canale)

    def _posizione(self, num: int, pos: dict, canale: int, rx: float) -> Optional["Fonte"]:
        if num == self.mio_num and not self.cfg.includi_ponte:
            return None
        if not self._canale_ok(num, canale):
            return None
        if self._info(num).get("user", {}).get("role") == "TAK":
            # Radio abbinata ad ATAK: la posizione buona e' quella che manda ATAK (porta 78)
            self.contatori["scartate_doppione_tak"] += 1
            return None
        fonte = self._fonte_mesh(num, pos, canale, rx)
        if fonte is None:
            return None
        with self._lock:
            st = self.stato.get(num)
            if st is None:
                st = self.stato[num] = StatoNodo(num, fonte, rx)
            else:
                st.fonte, st.ultimo_segnale = fonte, max(st.ultimo_segnale, rx)
            self._invia(st, time.time())
        return fonte

    def _tak_v2(self, num: int, carico: bytes, canale: int, rx: float) -> Optional["Fonte"]:
        """La posizione (Fonte) se il pacchetto la porta, per la mappa della copertura."""
        if not self._canale_ok(num, canale):
            return None
        if self.sdk is None:
            self._scarta(num, "tak_senza_libreria", "pacchetto TAK non leggibile: manca la libreria meshtastic-tak "
                         "(rifai l'installazione)")
            return None
        try:
            pkt = self.sdk.decodifica(carico)
        except PacchettoIllegibile as e:
            self._scarta(num, "tak_illeggibile", f"pacchetto TAK non decodificabile ({e})")
            return None
        impronta = self.sdk.impronta(pkt)
        self._sentiti_in_radio.aggiungi(impronta)
        for v in self.coda_radio.togli_impronta(impronta):
            self.contatori["radio_gia_trasmessi_da_altri"] += 1
            log.info("<- radio: %s gia' trasmesso da %s (!%08x): non lo ripeto", v.descrizione, self._nome(num), num)
        tipo = tipo_cot_v2(self.sdk.pb, pkt)
        if pkt.WhichOneof("payload_variant") is None and tipo.startswith("a-"):
            return self._pli_v2(num, pkt, tipo, canale, rx)
        self._oggetto_dalla_radio(num, pkt, impronta)
        return None

    def _pli_v2(self, num: int, pkt, tipo: str, canale: int, rx: float) -> Optional["Fonte"]:
        lat, lon = pkt.latitude_i * 1e-7, pkt.longitude_i * 1e-7
        if (pkt.latitude_i == 0 and pkt.longitude_i == 0) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        id_radio = f"!{num:08x}"
        tracker = (not pkt.uid) or pkt.uid == id_radio
        takv = None
        if pkt.tak_platform or pkt.tak_version:
            takv = {"device": pkt.tak_device, "platform": pkt.tak_platform, "os": pkt.tak_os,
                    "version": pkt.tak_version}
        hae = float(pkt.altitude) if abs(pkt.altitude) < 999999 else NON_NOTO
        fonte = Fonte(tipo_fonte="tak", uid=pkt.uid or id_radio, lat=lat, lon=lon, hae=hae, ce=NON_NOTO,
                      t_fix=rx, how=how_v2(self.sdk.pb, pkt), geosrc=SORGENTI.get(pkt.geo_src),
                      altsrc=SORGENTI.get(pkt.alt_src),
                      rotta=pkt.course / 100.0 if (pkt.course or pkt.speed) else None,
                      velocita=pkt.speed / 100.0 if (pkt.course or pkt.speed) else None,
                      canale=canale, tipo_cot=tipo,
                      callsign=pulisci(pkt.callsign, 80) or self._nome(num),
                      squadra=SQUADRE.get(pkt.team) or (self.cfg.squadra if tracker else None),
                      ruolo=RUOLI_TAK.get(pkt.role) or ("Team Member" if tracker else None),
                      batteria=pkt.battery or None, takv=takv,
                      droid=None if tracker else (pulisci(pkt.device_callsign, 80) or None),
                      endpoint=not tracker, tracker=tracker)
        self._salva_tak(num, fonte, rx)
        return fonte

    def _tak_v1(self, num: int, carico: bytes, canale: int, rx: float) -> Optional["Fonte"]:
        if not self._canale_ok(num, canale):
            return None
        try:
            from meshtastic.protobuf import atak_pb2
            pkt = atak_pb2.TAKPacket.FromString(carico)
        except Exception as e:  # noqa
            self._scarta(num, "tak_illeggibile", f"pacchetto TAK (vecchio formato) non decodificabile ({e})")
            return None
        if pkt.HasField("chat"):
            self._chat_v1(num, pkt)
            return None
        if not pkt.HasField("pli"):
            self.contatori["tak_v1_altro"] += 1
            return None
        lat, lon = pkt.pli.latitude_i * 1e-7, pkt.pli.longitude_i * 1e-7
        if (pkt.pli.latitude_i == 0 and pkt.pli.longitude_i == 0) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        id_radio = f"!{num:08x}"
        in_chiaro = not pkt.is_compressed
        utente_atak = (self._info(num).get("user", {}).get("role") == "TAK" and in_chiaro
                       and bool(pkt.contact.device_callsign))
        uid = pkt.contact.device_callsign.split("|", 1)[0] if utente_atak else id_radio
        callsign = (pulisci(pkt.contact.callsign, 80) if in_chiaro else "") or self._nome(num)
        tracker = not utente_atak
        fonte = Fonte(tipo_fonte="tak", uid=uid, lat=lat, lon=lon,
                      hae=float(pkt.pli.altitude) if abs(pkt.pli.altitude) < 999999 else NON_NOTO,
                      ce=NON_NOTO, t_fix=rx, how="m-g", geosrc="GPS", altsrc="GPS",
                      rotta=float(pkt.pli.course) if (pkt.pli.course or pkt.pli.speed) else None,
                      velocita=float(pkt.pli.speed) if (pkt.pli.course or pkt.pli.speed) else None,
                      canale=canale, callsign=callsign,
                      squadra=SQUADRE.get(pkt.group.team) or (self.cfg.squadra if tracker else None),
                      ruolo=RUOLI_TAK.get(pkt.group.role) or ("Team Member" if tracker else None),
                      batteria=pkt.status.battery or None, endpoint=utente_atak, tracker=tracker)
        self._salva_tak(num, fonte, rx)
        return fonte

    def _chat_v1(self, num: int, pkt) -> None:
        """Chat ATAK nel formato precedente (radio con firmware 2.7): convertita nel formato
        nuovo e ricostruita con la libreria TAK, come le altre chat."""
        if self.sdk is None:
            return
        if pkt.is_compressed:
            # testo compresso dal firmware 2.7 (unishox2): illeggibile qui. Una radio 2.7 consegna
            # anche la copia decompressa, che e' quella buona.
            self.contatori["chat_v1_compresse"] += 1
            return
        chat = pkt.chat
        if chat.receipt_type or not chat.message:
            return
        uid_mitt, _, id_msg = pkt.contact.device_callsign.partition("|")
        uid_mitt = uid_mitt or f"!{num:08x}"
        id_msg = id_msg or hashlib.sha256(f"{num}{chat.message}{pkt.contact.callsign}".encode()).hexdigest()[:8]
        privata = chat.HasField("to") and bool(chat.to)
        stanza = chat.to if privata else "All Chat Rooms"
        pb = self.sdk.pb
        nuovo = pb.TAKPacketV2(cot_type_id=pb.CotType.Value("CotType_b_t_f"),
                               how=pb.CotHow.Value("CotHow_h_g_i_g_o"),
                               callsign=pulisci(pkt.contact.callsign, 80) or self._nome(num),
                               uid=f"GeoChat.{uid_mitt}.{stanza}.{id_msg}",
                               team=pkt.group.team, role=pkt.group.role, stale_seconds=86400)
        nuovo.chat.message = chat.message
        nuovo.chat.to_callsign = nuovo.callsign  # nella libreria TAK qui va il mittente
        if privata:
            nuovo.chat.to = chat.to
            if chat.HasField("to_callsign") and chat.to_callsign:
                nuovo.marti.dest_callsign.append(chat.to_callsign)
        self._oggetto_dalla_radio(num, nuovo, self.sdk.impronta(nuovo))

    # ---- dalla radio al TAK Server: chat, segnaposti, forme... ----

    def _oggetto_dalla_radio(self, num: int, pkt, impronta: str) -> None:
        if impronta in self._visti_dal_server:
            # e' arrivato anche dal TAK Server: lo ha gia' portato la' un altro ponte, o viene dalla
            # sala operativa e un ponte l'ha appena trasmesso in radio
            self.contatori["radio_gia_sul_server"] += 1
            return
        if impronta in self._inoltrati_al_server:
            self.contatori["radio_doppioni"] += 1
            return
        self._inoltrati_al_server.aggiungi(impronta)
        try:
            xml = self.sdk.in_xml(pkt)
        except Exception as e:  # noqa
            self._scarta(num, "tak_illeggibile", f"oggetto TAK non ricostruibile ({e})")
            return
        if tipo_cot_v2(self.sdk.pb, pkt).startswith(TIPI_STATICI):
            xml = allunga_stale(xml, time.time())
        xml = marca_dalla_radio(xml, impronta, f"!{num:08x}", self.mio_uid)
        descrizione = f"{descrivi_pkt(self.sdk.pb, pkt)} da {self._nome(num)} (!{num:08x})"
        quando = time.time() + random.uniform(*self.ATTESA_VERSO_SERVER)
        with self._lock_uscite:
            self._verso_server.append((quando, impronta, xml.encode("utf-8"), descrizione))

    def _manda_al_server(self, impronta: str, xml: bytes, descrizione: str) -> None:
        if impronta in self._visti_dal_server:
            self.contatori["radio_gia_sul_server"] += 1
            log.info("-> TAK: %s gia' portato sul server da un altro ponte", descrizione)
            return
        self.tak.invia_una_volta(xml, time.time() + 3600)
        self.contatori["radio_al_server"] += 1
        log.info("-> TAK: %s", descrizione)

    # ---- dal TAK Server alla radio ----

    def su_evento_tak(self, evento: bytes) -> None:
        """Chiamato dal thread del TAK Server: il lavoro vero lo fa il thread delle uscite."""
        try:
            self._dal_server.put_nowait(evento)
        except queue.Full:
            self.contatori["server_coda_piena"] += 1

    def _dal_tak(self, grezzo: bytes) -> None:
        ora = time.time()
        try:
            radice = ET.fromstring(grezzo)
        except ET.ParseError:
            self.contatori["server_illeggibili"] += 1
            return
        if radice.tag != "event":
            return
        tipo = radice.get("type") or ""
        if tipo.startswith(("t-x-c-t", "t-x-takp")):
            return  # risposte ai ping e offerte di protocollo
        self.contatori["server_eventi"] += 1
        det = radice.find("detail")
        segno = det.find("__mesh2tak") if det is not None else None
        if segno is not None:
            if segno.get("h"):
                self._visti_dal_server.aggiungi(segno.get("h"))
            return
        if e_posizione_dispositivo(radice) and radice.get("uid"):
            self._collegati_internet.aggiungi(radice.get("uid"))
        if self.sdk is None or not self.cfg.invio_radio:
            return
        categoria, motivo = categoria_dal_server(radice)
        if categoria is None:
            self.contatori["server_non_per_radio"] += 1
            log.debug("Dal TAK Server, non per la radio (%s): %s %s", motivo, tipo, radice.get("uid"))
            return
        stale = leggi_ora_cot(radice.get("stale"))
        if stale is not None and stale <= ora:
            self.contatori["server_scaduti"] += 1
            return
        xml = grezzo.decode("utf-8", "replace")
        if tipo.startswith(TIPI_STATICI):
            xml = allunga_stale(xml, ora)
        try:
            pkt, filo = self.sdk.da_xml(xml)
        except OggettoTroppoGrande:
            self._troppo_grande(radice, categoria)
            return
        except Exception as e:  # noqa
            self.contatori["server_non_convertibili"] += 1
            log.info("Dal TAK Server: %s %s non convertibile per la radio (%s)", tipo, radice.get("uid"), e)
            return
        impronta = self.sdk.impronta(pkt)
        self._visti_dal_server.aggiungi(impronta)
        descrizione = descrivi_pkt(self.sdk.pb, pkt)
        if impronta in self._sentiti_in_radio:
            self.contatori["server_gia_in_radio"] += 1
            log.debug("Dal TAK Server: %s gia' sentito in radio", descrizione)
            return
        if impronta in self._trasmessi or self.coda_radio.contiene(impronta):
            return  # appena trasmesso, o gia' in coda identico (es. allarme ripetuto dal server)
        attesa = self.ATTESA_VERSO_RADIO_ALLARME if categoria == "emergenza" else self.ATTESA_VERSO_RADIO
        v = VoceRadio(priorita=PRIORITA[categoria], categoria=categoria, uid=radice.get("uid") or "",
                      descrizione=descrizione, filo=filo, impronta=impronta, creata=ora,
                      pronta=ora + random.uniform(*attesa), scadenza=stale or ora + 3600)
        sostituita = self.coda_radio.metti(v)
        log.info("<- TAK: %s%s (in coda per la radio, %d byte)", descrizione,
                 " - versione nuova" if sostituita else "", len(filo))

    def _troppo_grande(self, radice: ET.Element, categoria: str) -> None:
        self.contatori["server_troppo_grandi"] += 1
        uid_mitt, nominativo = mittente_evento(radice)
        nome = nome_evento(radice) or {"chat": "il messaggio", "oggetto": "l'oggetto"}.get(categoria, categoria)
        log.warning("Non trasmesso alle radio: %s «%s» di %s e' troppo grande per un pacchetto radio",
                    categoria, nome, nominativo or "?")
        chiave = radice.get("uid") or ""
        if not uid_mitt or not nominativo or chiave in self._avvisati:
            return
        self._avvisati.aggiungi(chiave)
        cosa = "Il messaggio" if categoria == "chat" else f"«{nome}»"
        testo = (f"{cosa} non e' stato inviato alle radio in campo: e' troppo grande per un pacchetto radio. "
                 + ("Accorcialo e rimandalo." if categoria == "chat"
                    else "Semplificalo (meno punti) o accorcia le note, poi rimandalo."))
        self.tak.invia_una_volta(cot_chat_diretta(da_uid=self.mio_uid, da_callsign=self._nome_ponte(),
                                                  a_uid=uid_mitt, a_callsign=nominativo, testo=testo,
                                                  ora=time.time()), time.time() + 3600)

    def _nome_ponte(self) -> str:
        return self._nome(self.mio_num) if self.mio_num else "Ponte Meshtastic"

    # ---- thread delle uscite: verso il server (con attesa) e verso la radio ----

    def avvia_uscite(self) -> None:
        self._thread_uscite = threading.Thread(target=self._ciclo_uscite, name="uscite", daemon=True)
        self._thread_uscite.start()

    def ferma_uscite(self) -> None:
        self._fermo.set()
        if self._thread_uscite is not None:
            self._thread_uscite.join(timeout=5)

    def _ciclo_uscite(self) -> None:
        while not self._fermo.is_set():
            try:
                self.passo_uscite(attesa=0.25)
            except Exception:  # noqa
                log.exception("Errore inatteso nelle uscite verso radio e server")
                self._fermo.wait(1)

    def passo_uscite(self, attesa: float = 0.0) -> None:
        """Un giro: un evento dal server, gli inoltri al server maturati, una trasmissione radio."""
        try:
            grezzo = self._dal_server.get(timeout=attesa) if attesa else self._dal_server.get_nowait()
        except queue.Empty:
            grezzo = None
        if grezzo is not None:
            self._dal_tak(grezzo)
        ora = time.time()
        with self._lock_uscite:
            pronti = [x for x in self._verso_server if x[0] <= ora]
            self._verso_server = [x for x in self._verso_server if x[0] > ora]
        for _, impronta, xml, descrizione in pronti:
            self._manda_al_server(impronta, xml, descrizione)
        self._forse_trasmetti(ora)

    def _ricarica(self, ora: float) -> None:
        quota = self.cfg.tempo_radio_percento / 100.0
        self._credito_radio = min(self._credito_radio + (ora - self._ultima_ricarica) * quota,
                                  self.RISERVA_RADIO_S * quota)
        self._ultima_ricarica = ora

    def _forse_trasmetti(self, ora: float) -> None:
        for v in self.coda_radio.scadute(ora):
            self.contatori["radio_scaduti_in_coda"] += 1
            log.warning("Non trasmesso alle radio: %s (rimasto in coda troppo a lungo o scaduto)", v.descrizione)
        if ora - self._ultima_trasmissione < self.SPAZIO_TRA_INVII_S:
            return
        v = self.coda_radio.guarda(ora)
        if v is None:
            return
        self._ricarica(ora)
        durata = tempo_in_aria(len(v.filo), self.preset)
        if v.priorita > 0 and self._credito_radio < durata:
            return  # aspetta che si liberi tempo radio; gli allarmi passano comunque
        v = self.coda_radio.prossima(ora)
        if v is None:
            return
        durata = tempo_in_aria(len(v.filo), self.preset)
        if not self._trasmetti(v):
            self.coda_radio.rimetti(v)
            self._ultima_trasmissione = ora  # riprova piu' tardi, senza insistere
            return
        self._credito_radio -= durata
        self._ultima_trasmissione = ora
        self._trasmessi.aggiungi(v.impronta)
        self.contatori["radio_trasmessi"] += 1
        log.info("-> radio: %s (%d byte, %.1f s in aria)", v.descrizione, len(v.filo), durata)

    def _trasmetti(self, v: VoceRadio) -> bool:
        iface = self.iface
        canale = self._canale_invio()
        if iface is None or canale is None:
            return False
        try:
            with self._lock_invio:
                iface.sendData(v.filo, destinationId=BROADCAST, portNum=PORTA_ATAK_V2, wantAck=False,
                               channelIndex=canale, hopLimit=self.hop_limit)
        except Exception as e:  # noqa
            log.warning("Trasmissione alla radio non riuscita (%s): riprovo", e)
            return False
        return True

    def _salva_tak(self, num: int, fonte: Fonte, rx: float) -> None:
        with self._lock:
            st = self.stato.get(num)
            if st is None:
                st = self.stato[num] = StatoNodo(num, fonte, rx)
            else:
                st.fonte, st.ultimo_segnale = fonte, max(st.ultimo_segnale, rx)
            self._invia(st, time.time())

    def _attivita(self, num: int, rx: float, porta: int) -> None:
        """Qualsiasi pacchetto di una radio nota la tiene viva sulla mappa."""
        with self._lock:
            st = self.stato.get(num)
            if st is None:
                return
            st.ultimo_segnale = max(st.ultimo_segnale, rx)
            ora = time.time()
            nome_cambiato = (porta == PORTA_NODEINFO and st.fonte.tipo_fonte == "mesh"
                             and self._nome(num) != st.nome_inviato)
            if nome_cambiato or ora - st.ultimo_invio >= self.cfg.aggiornamento_min * 60:
                self._invia(st, ora, ripetizione=not nome_cambiato)

    # ---- costruzione e invio CoT ----

    def _ora_locale(self, ts: float) -> str:
        dt = datetime.fromtimestamp(ts, self.tz) if self.tz else datetime.fromtimestamp(ts)
        oggi = (datetime.now(self.tz) if self.tz else datetime.now()).date()
        return dt.strftime("%H:%M") if dt.date() == oggi else dt.strftime("%d/%m %H:%M")

    def _invia(self, st: StatoNodo, ora: float, ripetizione: bool = False) -> None:
        """Manda (o ripete) la posizione di una radio al TAK Server.

        L'evento porta l'ora dell'ultimo segnale ricevuto, non quella dell'invio: chi apre l'app
        dopo vede da quanto la radio non si fa sentire. Il ponte stesso e' sempre "adesso".
        """
        f = st.fonte
        ponte = st.num == self.mio_num
        if f.tipo_fonte == "tak" and not ponte and f.uid in self._collegati_internet:
            # il telefono manda gia' la sua posizione via internet, piu' fresca di questa
            if not ripetizione:
                self.contatori["scartate_collegate_via_internet"] += 1
                log.debug("Non inoltro %s: e' collegato al TAK Server via internet", f.callsign or f.uid)
            return
        stale = ora + self.cfg.validita_min * 60 if ponte else st.ultimo_segnale + self.cfg.validita_min * 60
        if stale <= ora + 30:
            return
        nome, xml = self._cot(st, ora if ponte else min(st.ultimo_segnale, ora), stale)
        self.tak.invia(f.uid, xml, stale)
        st.ultimo_invio = ora
        st.nome_inviato = nome
        self.contatori["inoltrate"] += 1
        if ripetizione:
            self.contatori["ripetute"] += 1
        canale = self.canali.get(f.canale)
        (log.debug if ripetizione else log.info)(
            "-> TAK: %s (!%08x) %.5f, %.5f%s%s%s", nome, st.num, f.lat, f.lon,
            f" ±{f.ce:.0f} m" if f.ce < NON_NOTO else "",
            f" [canale {canale.nome}]" if canale and not ponte else "",
            " (ripetuta)" if ripetizione else "")

    def _cot(self, st: StatoNodo, t_evento: float, stale: float) -> Tuple[str, bytes]:
        f = st.fonte
        info = self._info(st.num)
        utente = info.get("user", {})
        metriche = info.get("deviceMetrics", {})
        livello = metriche.get("batteryLevel")
        batteria = int(livello) if livello and 0 < livello <= 100 else None
        id_radio = f"!{st.num:08x}"

        segno = {"radio": id_radio, "ponte": self.mio_uid}
        st.batteria = f.batteria or batteria or st.batteria
        st.salti = info.get("hopsAway") if "hopsAway" in info else st.salti
        if f.tipo_fonte == "tak" and not f.tracker:
            note = f"Ricevuta via Meshtastic dalla radio {id_radio} alle {self._ora_locale(st.ultimo_segnale)}"
            xml = cot_evento(uid=f.uid, tipo=f.tipo_cot, how=f.how, lat=f.lat, lon=f.lon, hae=f.hae,
                             ce=f.ce, t_evento=t_evento, stale=stale, callsign=f.callsign, endpoint=f.endpoint,
                             squadra=f.squadra, ruolo=f.ruolo, batteria=f.batteria, rotta=f.rotta,
                             velocita=f.velocita, geosrc=f.geosrc, altsrc=f.altsrc, takv=f.takv,
                             droid=f.droid, note=note, segno=segno)
            return f.callsign or id_radio, xml

        ponte = st.num == self.mio_num
        ruolo_radio = utente.get("role", "CLIENT")
        fisso = ponte or ruolo_radio in RUOLI_FISSI
        nome = f.callsign if f.tipo_fonte == "tak" else self._nome(st.num)
        hw = utente.get("hwModel")
        dettagli = [x for x in ((hw if hw and hw != "UNSET" else None), ruolo_radio) if x]
        intest = ("Gateway Meshtastic verso TAK" if ponte else "Radio Meshtastic") + f" {id_radio}"
        if dettagli:
            intest += " (" + ", ".join(dettagli) + ")"
        parti = [intest, f"posizione delle {self._ora_locale(f.t_fix)}"]
        if ponte and self.nota_ponte:
            parti.insert(0, self.nota_ponte)
        if not ponte:
            parti.append(f"ultimo segnale alle {self._ora_locale(st.ultimo_segnale)}")
        if f.precisione_bit < 32 and f.ce < NON_NOTO:
            parti.append(f"posizione approssimata ±{f.ce:.0f} m")
        if f.quota_msl is not None:
            parti.append(f"quota {f.quota_msl:.0f} m s.l.m.")
        b = f.batteria or batteria
        if b:
            parti.append(f"batteria {b}%")
        elif livello and livello > 100:
            parti.append("alimentata da rete")
        salti = info.get("hopsAway")
        if not ponte and salti is not None:
            parti.append("ricevuta direttamente" if salti == 0 else
                         ("1 salto radio" if salti == 1 else f"{salti} salti radio"))
        tipo = "a-f-G-I" if fisso else (f.tipo_cot or "a-f-G-U-C")
        icona = self.cfg.icona_ponte if ponte else None
        squadra = None if fisso else (f.squadra or self.cfg.squadra)
        ruolo = None if fisso else (f.ruolo or "Team Member")
        scelta = self.icone.di(id_radio)
        if scelta:
            # icona scelta dalla pagina della rete: al posto del colore della squadra (TAK Aware), e un tipo che
            # da' un simbolo sensato a chi non ha l'insieme di icone
            tipo, icona = scelta
            squadra = ruolo = None
        xml = cot_evento(
            uid=f.uid, tipo=tipo, how=f.how,
            lat=f.lat, lon=f.lon, hae=f.hae, ce=f.ce, t_evento=t_evento, stale=stale, callsign=nome,
            squadra=squadra, ruolo=ruolo,
            batteria=None if fisso else b, rotta=f.rotta, velocita=f.velocita,
            geosrc=f.geosrc, altsrc=f.altsrc, note=" · ".join(parti), segno=segno, icona=icona)
        return nome, xml

    def imposta_icone(self, corpo: Optional[bytes]) -> Tuple[int, dict]:
        """Icone arrivate dalla pagina della rete: le radio che cambiano icona si rimandano al prossimo giro di
        manutenzione (pochi secondi), senza aspettare i 2 minuti della ripetizione."""
        stato, risposta, cambiate = self.icone.imposta(corpo)
        if cambiate:
            with self._lock:
                for st in self.stato.values():
                    if f"!{st.num:08x}" in cambiate:
                        st.ultimo_invio = 0.0
        return stato, risposta

    def annuncia_pausa(self, nota: str) -> None:
        """Prima di lasciare il nodo alla pagina di gestione: chi guarda la mappa lo legge nel ponte."""
        with self._lock:
            self.nota_ponte = nota
            st = self.stato.get(self.mio_num)
            if st is not None and self.cfg.includi_ponte:
                ora = time.time()
                st.ultimo_segnale = ora
                self._invia(st, ora)

    def istantanea(self) -> dict:
        """Stato del ponte per la pagina della rete: nomi, orari e numeri. Niente chiavi ne' coordinate."""
        ora = time.time()
        validita = self.cfg.validita_min * 60
        with self._lock:
            radio = []
            for st in list(self.stato.values()):
                if st.num == self.mio_num:
                    continue
                f = st.fonte
                canale = self.canali.get(f.canale)
                radio.append({
                    "id": f"!{st.num:08x}",
                    "nome": pulisci(st.nome_inviato or self._nome(st.num), 60),
                    "app_tak": f.tipo_fonte == "tak",
                    "ultimo_segnale": round(st.ultimo_segnale),
                    "posizione": round(f.t_fix) if f.t_fix else None,
                    "batteria": st.batteria,
                    "salti": st.salti,
                    "canale": canale.nome if canale else None,
                    "sulla_mappa": st.ultimo_segnale + validita > ora,
                })
            radio.sort(key=lambda r: -r["ultimo_segnale"])
            canali = [{"indice": c.indice, "nome": c.nome, "privato": c.privato,
                       "inoltrato": c.indice in self.canali_ok}
                      for c in sorted(self.canali.values(), key=lambda c: c.indice)]
            contatori = dict(self.contatori)
            viste = sorted(self.viste.values(), key=lambda v: -v.ultimo)[:200]
            radio_rete = [{"id": f"!{v.num:08x}", "nome": v.nome or self._nome(v.num), "nome_breve": v.nome_breve,
                           "modello": v.modello, "ruolo": v.ruolo, "ultimo": round(v.ultimo),
                           "batteria": v.batteria, "tensione": v.tensione, "occupato": v.occupato,
                           "trasmesso": v.trasmesso, "acceso_s": v.acceso_s,
                           "metriche": round(v.metriche) if v.metriche else None, "salti": v.salti,
                           "snr": v.snr, "rssi": v.rssi, "diretto": round(v.diretto) if v.diretto else None,
                           "fisso": v.fisso, "posizione_fissa": v.posizione_fissa, "ripetuti": v.ripetuti,
                           "ultimo_ripetuto": round(v.ultimo_ripetuto) if v.ultimo_ripetuto else None}
                          for v in viste]
            canale_radio = dict(self.canale_locale)
            if "quando" not in canale_radio:       # prima della misura del minuto: quella del database del nodo
                dm = self._info(self.mio_num).get("deviceMetrics") or {}
                if "uptimeSeconds" in dm:
                    canale_radio = {"occupato": numero(dm.get("channelUtilization", 0.0), 0, 100, 1),
                                    "trasmesso": numero(dm.get("airUtilTx", 0.0), 0, 100, 2), "quando": None}
            if canale_radio.get("quando"):
                canale_radio["quando"] = round(canale_radio["quando"])
            return {
                "nome": self.nodo_nome,
                "nome_breve": self.nodo_nome_breve,
                "id_nodo": f"!{self.mio_num:08x}" if self.mio_num else None,
                "chiave_pubblica": self.nodo_chiave_pubblica or None,
                "nodo": {"collegato": self.iface is not None,
                         "dal": round(self.nodo_collegato_dal) if self.nodo_collegato_dal else None,
                         "collegamento": descrivi_collegamento(self.cfg.collegamento),
                         "preset": nome_preset(self.preset), "canali": canali},
                "radio": radio[:150],
                "radio_rete": radio_rete,
                "canale_radio": canale_radio or None,
                "radio_sulla_mappa": sum(1 for r in radio if r["sulla_mappa"]),
                "validita_min": self.cfg.validita_min,
                "conteggi": {k: contatori.get(k, 0) for k in
                             ("inoltrate", "ripetute", "radio_al_server", "server_eventi", "radio_trasmessi")},
                "conteggi_dal": round(self._ultimo_riepilogo),
                "coda_radio": len(self.coda_radio),
            }

    # ---- manutenzione periodica (thread principale) ----

    def manutenzione(self) -> None:
        """Ogni pochi secondi: ripete al TAK Server le posizioni ancora valide e fa il riepilogo orario.

        Il TAK Server non rimanda a chi si collega dopo le posizioni portate dal ponte: senza
        ripeterle, chi apre l'app vedrebbe una radio solo al suo invio successivo, e una radio
        uscita dalla copertura (l'ultima posizione nota) non la vedrebbe affatto.
        """
        ora = time.time()
        intervallo = self.cfg.aggiornamento_min * 60
        with self._lock:
            for st in list(self.stato.values()):
                if ora - st.ultimo_invio < intervallo:
                    continue
                if st.num == self.mio_num:
                    if not self.cfg.includi_ponte:
                        continue
                    st.ultimo_segnale = ora
                self._invia(st, ora, ripetizione=True)
            for num in [n for n, s in self.stato.items() if ora - s.ultimo_segnale > 86400]:
                del self.stato[num]
        if ora - self._ultimo_riepilogo >= 3600:
            c, self.contatori = self.contatori, Counter()
            self._ultimo_riepilogo = ora
            scartate = {k[len("scartate_"):]: v for k, v in c.items() if k.startswith("scartate_")}
            sulla_mappa = sum(1 for s in self.stato.values()
                              if s.ultimo_segnale + self.cfg.validita_min * 60 > ora or s.num == self.mio_num)
            log.info("Ultima ora: %d invii al TAK Server (%d ripetizioni per chi apre l'app dopo), "
                     "%d radio sulla mappa%s", c.get("inoltrate", 0), c.get("ripetute", 0), sulla_mappa,
                     ("; scartate: " + ", ".join(f"{k} {v}" for k, v in sorted(scartate.items())))
                     if scartate else "")
            log.info("Ultima ora: dalla radio al server %d chat/oggetti; dal server %d eventi, %d trasmessi alle "
                     "radio, %d in coda, %d troppo grandi", c.get("radio_al_server", 0), c.get("server_eventi", 0),
                     c.get("radio_trasmessi", 0), len(self.coda_radio), c.get("server_troppo_grandi", 0))
        self.copertura.salva()          # al massimo ogni 10 minuti, e solo se e' cambiata
        self.squadra.salva()


# --------------------------------------------------------------------------
# Pagina di gestione del nodo nel browser, con il ponte in pausa
# --------------------------------------------------------------------------

class Pausa:
    """Il ponte lascia il nodo alla pagina di gestione e lo riprende quando la pagina ha finito.

    Il nodo serve un programma collegato alla volta: con il ponte e la pagina collegati insieme
    i messaggi in arrivo si dividono fra i due (una sola coda, MeshService::toPhoneQueue) e il
    ponte ne perde una parte.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.attiva = False
        self.nota = ""
        self.rilasciato = threading.Event()   # il ciclo principale ha staccato il nodo

    def chiedi(self, nota: str) -> bool:
        with self._lock:
            if self.attiva:
                return False
            self.attiva, self.nota = True, nota
            self.rilasciato.clear()
            return True

    def termina(self) -> bool:
        with self._lock:
            if not self.attiva:
                return False
            self.attiva = False
            return True


class _ServerPagina(http.server.ThreadingHTTPServer):
    """HTTPS con la stretta di mano TLS nel thread della richiesta, non in quello che accetta."""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, indirizzo, gestore, contesto: ssl.SSLContext):
        self.contesto = contesto
        super().__init__(indirizzo, gestore)

    def finish_request(self, request, client_address):
        request.settimeout(30)
        try:
            tls = self.contesto.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            return
        try:
            self.RequestHandlerClass(tls, client_address, self)
        finally:
            try:
                tls.close()
            except OSError:
                pass


class Gestione:
    """Gestione del nodo dal browser o dall'app Meshtastic via Wi-Fi, passando dal ponte.

    meshtasticd risponde solo al Raspberry (porte interne): la pagina (porta 9443) e l'app
    (porta 4403) passano dal ponte. Pagine, script e immagini passano sempre; quando la pagina
    usa il nodo (/api/...) o l'app si collega, il ponte va in pausa: stacca il nodo e lo riprende
    'chiusura_s' secondi dopo che la pagina tace o l'app si e' scollegata, al massimo dopo
    'pausa_massima_min'. Dopo la pausa massima la pagina riceve un rifiuto finche' non viene
    ricaricata e l'app finche' non resta zitta per 'silenzio_app_s' (l'app Meshtastic riprova da
    sola con attese fino a 5 minuti): una scheda o un'app dimenticate non tengono fermo il ponte.
    """

    SALTA = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "te", "trailer",
             "upgrade", "host", "content-length"}

    def __init__(self, cfg: Config, pausa: Pausa, tz=None):
        self.cfg = cfg
        self.pausa = pausa
        self.tz = tz
        # vuoto con la radio USB: la pagina da' solo lo stato del ponte (per la pagina della rete)
        self.nodo_pagina = cfg.gestione_nodo_pagina.strip()
        u = urllib.parse.urlsplit(self.nodo_pagina or "https://127.0.0.1:9444")
        self.nodo_https = u.scheme == "https"
        self.nodo_host = u.hostname or "127.0.0.1"
        self.nodo_porta = u.port or (443 if self.nodo_https else 80)
        self.fornisci_stato: Optional[Callable[[], dict]] = None   # /mesh2tak/stato.json
        self.radio: Optional["GestioneRadio"] = None                # /mesh2tak/radio/... (dalla 1.10.0)
        self.icone: Optional[Callable[[Optional[bytes]], Tuple[int, dict]]] = None   # /mesh2tak/icone (1.11.0)
        self.copertura: Optional[Callable[[bool, str], dict]] = None   # /mesh2tak/copertura.json (1.14.0)
        # dalla 1.13.0: comandi dalla pagina della rete (li fa mesh2tak-aggiorna, da root) e registro per la pagina
        self.file_controlla = os.path.join(cfg.accesso_cartella_stato, "controlla-adesso")
        self.file_registro = Sistema.REGISTRO
        self._ultimo_controllo = 0.0
        self._controllo_rimandato = False   # un avviso arrivato entro 5 s dal precedente: si da' dopo, non si perde
        self.chi = ""                    # chi tiene il nodo nella pausa in corso
        self._ctx_nodo = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._ctx_nodo.check_hostname = False
        self._ctx_nodo.verify_mode = ssl.CERT_NONE   # solo verso 127.0.0.1, certificato fatto da meshtasticd
        app_host, app_porta = "127.0.0.1", 4403
        if cfg.collegamento.startswith("tcp:"):
            app_host, _, porta = cfg.collegamento[4:].rpartition(":")
            app_host, app_porta = app_host or "127.0.0.1", int(porta or 4403)
        self.app_nodo = (app_host, app_porta)
        self._lock = threading.Lock()
        self.inizio = 0.0
        self.ultima_attivita = 0.0
        self.scaduta = False             # pagina: rifiuto fino al ricaricamento
        self.app_bloccata = False        # app: rifiuto finche' non tace per silenzio_app_s
        self.ultimo_tentativo_app = 0.0
        self._app: List[Tuple[socket.socket, socket.socket]] = []   # (app, nodo) collegate adesso
        self._server: Optional[_ServerPagina] = None
        self._ascolto_app: Optional[socket.socket] = None
        self._fermo = threading.Event()

    def avvia(self) -> None:
        if self.cfg.gestione_porta_pagina:
            self._avvia_pagina()
        if self.cfg.gestione_porta_app:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", self.cfg.gestione_porta_app))
            s.listen(4)
            self._ascolto_app = s
            threading.Thread(target=self._accetta_app, name="gestione-app", daemon=True).start()
        threading.Thread(target=self._sorveglia, name="gestione-pausa", daemon=True).start()

    def _avvia_pagina(self) -> None:
        gestione = self

        class Gestore(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, formato, *argomenti):  # niente righe per ogni richiesta
                pass

            def do_GET(self):  # noqa: N802
                gestione.servi(self)

            do_PUT = do_POST = do_OPTIONS = do_HEAD = do_DELETE = do_GET  # noqa: N815

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cfg.gestione_certificato, self.cfg.gestione_chiave)
        self._server = _ServerPagina(("0.0.0.0", self.cfg.gestione_porta_pagina), Gestore, ctx)
        threading.Thread(target=self._server.serve_forever, name="gestione-pagina", daemon=True).start()

    def ferma(self) -> None:
        self._fermo.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._ascolto_app is not None:
            try:
                self._ascolto_app.close()
            except OSError:
                pass
        self._chiudi_app()
        self.pausa.termina()

    def _ora(self, ts: float) -> str:
        return (datetime.fromtimestamp(ts, self.tz) if self.tz else datetime.fromtimestamp(ts)).strftime("%H:%M")

    def _pausa(self, da: str, chi: str) -> Optional[str]:
        """Chiede la pausa (gia' sotto lock) e restituisce None, oppure il perche' del rifiuto."""
        ora = time.time()
        self.ultima_attivita = ora
        if not self.pausa.attiva:
            fine = ora + self.cfg.gestione_pausa_massima_min * 60
            nota = (f"IN PAUSA: nodo gestito {chi} dalle {self._ora(ora)} (al massimo fino alle "
                    f"{self._ora(fine)}), niente radio verso il TAK Server")
            if self.pausa.chiedi(nota):
                self.inizio, self.chi = ora, chi
                log.info("Nodo gestito %s%s: ponte in pausa, il nodo passa a lei (il ponte lo riprende "
                         "%g s dopo la chiusura, al massimo tra %g minuti)", chi,
                         "" if chi.startswith("da internet") else f" da {da}", self.cfg.gestione_chiusura_s,
                         self.cfg.gestione_pausa_massima_min)
        return None

    def stato_pausa(self) -> dict:
        with self._lock:
            if not self.pausa.attiva:
                return {"attiva": False}
            return {"attiva": True, "chi": self.chi, "dal": round(self.inizio),
                    "fino_al_massimo": round(self.inizio + self.cfg.gestione_pausa_massima_min * 60)}

    @staticmethod
    def _chi(h) -> str:
        """Chi usa la pagina: dal tunnel (indirizzo del Raspberry stesso) arriva da internet, con il nome
        dell'amministratore che Authentik ha fatto entrare (lo aggiunge Caddy sul server)."""
        utente = re.sub(r"[^\w.@+-]", "", pulisci(h.headers.get("X-Authentik-Username"), 40))
        if h.client_address[0] in ("127.0.0.1", "::1") and utente:
            return f"da internet ({utente})"
        return "dal browser"

    def _aspetta_nodo(self) -> Optional[str]:
        if not self.pausa.rilasciato.wait(20):
            return "Il ponte non ha ancora lasciato il nodo: riprova tra qualche secondo."
        return None

    # ---- pagina nel browser ----

    def servi(self, h: http.server.BaseHTTPRequestHandler) -> None:
        percorso = urllib.parse.urlsplit(h.path).path
        if percorso == "/mesh2tak/stato.json":   # per la pagina della rete: non mette in pausa
            self._leggi_corpo(h)
            if h.command in ("GET", "HEAD"):
                self._stato(h)
            else:
                self._rispondi(h, 405, "Solo lettura")
            return
        if percorso == "/mesh2tak/icone":   # icone sulla mappa scelte dalla pagina della rete (dalla 1.11.0)
            self._servi_icone(h)
            return
        if percorso == "/mesh2tak/controlla":   # dalla 1.13.0: c'e' un comando dalla pagina della rete
            self._servi_controlla(h)
            return
        if percorso == "/mesh2tak/registro":    # dalla 1.13.0: il registro per la pagina della rete
            self._servi_registro(h)
            return
        if percorso == "/mesh2tak/copertura.json":   # dalla 1.14.0: la mappa della copertura
            self._servi_copertura(h)
            return
        if percorso.startswith("/mesh2tak/radio/"):   # radio della squadra gestite dalla pagina della rete
            try:
                troppo = int(h.headers.get("Content-Length") or 0) > 16384
            except ValueError:
                troppo = True
            if troppo or self.radio is None:
                stato, dati = (413, {"errore": "richiesta troppo grande"}) if troppo else \
                    (404, {"errore": "gestione delle radio non attiva su questo ponte"})
            else:
                stato, dati = self.radio.servi(h, percorso, self._leggi_corpo(h))
            self._rispondi(h, stato, json.dumps(dati, ensure_ascii=False, separators=(",", ":")),
                           "application/json; charset=utf-8")
            return
        if not self.nodo_pagina:
            self._leggi_corpo(h)
            self._rispondi(h, 200 if percorso in ("/", "/index.html") else 404,
                           f"Ponte Meshtastic -> TAK (mesh2tak {VERSIONE}). Questo ponte usa una radio collegata via "
                           "USB: la radio si gestisce dall'app Meshtastic, via Bluetooth o collegandola a un computer.")
            return
        if h.command == "GET" and percorso in ("/", "/index.html"):
            with self._lock:
                self.scaduta = False   # pagina ricaricata: si puo' ricominciare
        if percorso.startswith("/api/") and h.command != "OPTIONS":
            with self._lock:
                if self.scaduta:
                    motivo = (f"Pausa del ponte finita ({self.cfg.gestione_pausa_massima_min:g} minuti al massimo): "
                              "il ponte ha ripreso il nodo. Ricarica la pagina per altri "
                              f"{self.cfg.gestione_pausa_massima_min:g} minuti.")
                else:
                    motivo = self._pausa(h.client_address[0], self._chi(h))
            motivo = motivo or self._aspetta_nodo()
            if motivo:
                self._leggi_corpo(h)
                self._rispondi(h, 503, motivo)
                return
        self._inoltra(h)

    @staticmethod
    def _leggi_corpo(h) -> Optional[bytes]:
        try:
            lunghezza = int(h.headers.get("Content-Length") or 0)
        except ValueError:
            lunghezza = 0
        return h.rfile.read(lunghezza) if lunghezza > 0 else None

    def _servi_icone(self, h) -> None:
        """Le manda il servizio della pagina della rete dal tunnel: si accettano solo da li' (indirizzo del
        Raspberry stesso) e con l'intestazione della rete, mai dal Wi-Fi."""
        try:
            lunghezza = int(h.headers.get("Content-Length") or 0)
        except ValueError:
            lunghezza = -1
        if lunghezza < 0 or lunghezza > 131072:
            if 0 < lunghezza <= 1 << 20:
                resta = lunghezza                # letta e buttata: la risposta arriva senza chiudere a meta'
                while resta > 0:
                    pezzo = h.rfile.read(min(resta, 65536))
                    if not pezzo:
                        break
                    resta -= len(pezzo)
            h.close_connection = True
            stato, dati = 413, {"errore": "richiesta troppo grande"}
        else:
            corpo = h.rfile.read(lunghezza) if lunghezza else None
            if h.command != "POST":
                stato, dati = 405, {"errore": "solo POST"}
            elif h.client_address[0] not in ("127.0.0.1", "::1") or h.headers.get("X-Mesh2tak") != "1":
                stato, dati = 403, {"errore": "le icone arrivano solo dalla pagina della rete"}
            elif self.icone is None:
                stato, dati = 404, {"errore": "icone non attive su questo ponte"}
            else:
                stato, dati = self.icone(corpo)
        self._rispondi(h, stato, json.dumps(dati, ensure_ascii=False, separators=(",", ":")),
                       "application/json; charset=utf-8")

    @staticmethod
    def _corpo_al_massimo(h, massimo: int) -> Tuple[Optional[bytes], bool]:
        """(corpo, troppo grande). Uno troppo grande si legge e si butta (fino a 1 MB): la risposta arriva intera."""
        try:
            lunghezza = int(h.headers.get("Content-Length") or 0)
        except ValueError:
            lunghezza = -1
        if 0 <= lunghezza <= massimo:
            return (h.rfile.read(lunghezza) if lunghezza else None), False
        resta = lunghezza if 0 < lunghezza <= 1 << 20 else 0
        while resta > 0:
            pezzo = h.rfile.read(min(resta, 65536))
            if not pezzo:
                break
            resta -= len(pezzo)
        h.close_connection = True
        return None, True

    def _servi_controlla(self, h) -> None:
        """Dalla 1.13.0 il server della rete, dal tunnel, avvisa che nella registrazione c'e' un comando per il
        Raspberry: si tocca il file che sveglia mesh2tak-aggiorna (root), che lo legge e lo fa. Non porta comandi: al
        massimo fa guardare la registrazione un po' prima dei soliti 2 minuti."""
        _, troppo = self._corpo_al_massimo(h, 4096)
        if troppo:
            stato, dati = 413, {"errore": "richiesta troppo grande"}
        elif h.command != "POST":
            stato, dati = 405, {"errore": "solo POST"}
        elif h.client_address[0] not in ("127.0.0.1", "::1") or h.headers.get("X-Mesh2tak") != "1":
            stato, dati = 403, {"errore": "solo dalla pagina della rete"}
        else:
            # il file si tocca al massimo ogni 5 secondi (l'unita' .path di systemd non va tempestata); un avviso
            # arrivato prima si da' allo scadere dei 5 secondi: il comando nuovo potrebbe non essere stato ancora letto
            ora = time.time()
            with self._lock:
                attesa = self._ultimo_controllo + 5.0 - ora
                if attesa <= 0:
                    self._ultimo_controllo = ora
                elif not self._controllo_rimandato:
                    self._controllo_rimandato = True
                    t = threading.Timer(attesa, self._controlla_rimandato)
                    t.daemon = True
                    t.start()
            stato, dati = 200, {"esito": "chiesto"}
            if attesa <= 0:
                errore = self._tocca_controlla()
                if errore:
                    stato, dati = 500, {"errore": f"non riesco ad avvisare il Raspberry ({errore})"}
        self._rispondi(h, stato, json.dumps(dati, ensure_ascii=False, separators=(",", ":")),
                       "application/json; charset=utf-8")

    def _tocca_controlla(self) -> Optional[str]:
        """Sveglia mesh2tak-aggiorna (unita' .path). None se fatto, altrimenti il motivo."""
        try:
            with open(self.file_controlla, "w", encoding="ascii") as f:
                f.write(f"{time.time():.0f}\n")
            return None
        except OSError as e:
            return str(e.strerror or e)

    def _controlla_rimandato(self) -> None:
        with self._lock:
            self._controllo_rimandato = False
            self._ultimo_controllo = time.time()
        errore = self._tocca_controlla()
        if errore:
            log.warning("avviso dei comandi della pagina non passato a mesh2tak-aggiorna: %s", errore)

    def _servi_copertura(self, h) -> None:
        """La mappa della copertura (dalla 1.14.0), solo per gli amministratori della pagina della rete: dal tunnel e
        con il nome che Authentik ha messo, perche' dice dove erano le radio. POST {"azzera": true} la ricomincia
        (dopo aver spostato l'antenna), solo dalla pagina stessa (intestazione X-Mesh2tak e stessa origine)."""
        corpo, troppo = self._corpo_al_massimo(h, 1024)
        chi = re.sub(r"[^\w.@+-]", "", pulisci(h.headers.get("X-Authentik-Username"), 40))
        if troppo:
            stato, dati = 413, {"errore": "richiesta troppo grande"}
        elif h.command not in ("GET", "HEAD", "POST"):
            stato, dati = 405, {"errore": "metodo non previsto"}
        elif h.client_address[0] not in ("127.0.0.1", "::1") or not chi:
            stato, dati = 403, {"errore": "La copertura si vede solo dalla pagina della rete."}
        elif self.copertura is None:
            stato, dati = 404, {"errore": "mappa della copertura non disponibile"}
        elif h.command == "POST":
            origine = urllib.parse.urlsplit(h.headers.get("Origin") or "").netloc
            ospite = h.headers.get("X-Forwarded-Host") or h.headers.get("Host") or ""
            try:
                richiesta = json.loads((corpo or b"").decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                richiesta = None
            if h.headers.get("X-Mesh2tak") != "1" or (origine and origine != ospite):
                stato, dati = 403, {"errore": "richiesta non valida"}
            elif not isinstance(richiesta, dict) or richiesta.get("azzera") is not True:
                stato, dati = 400, {"errore": "richiesta non valida"}
            else:
                stato, dati = 200, self.copertura(True, chi)
        else:
            stato, dati = 200, self.copertura(False, chi)
        self._rispondi(h, stato, json.dumps(dati, ensure_ascii=False, separators=(",", ":")),
                       "application/json; charset=utf-8")

    def _servi_registro(self, h) -> None:
        """Il registro raccolto da mesh2tak-aggiorna (senza chiavi ne' password), solo per gli amministratori della
        pagina della rete: dal tunnel e con il nome che Authentik ha messo."""
        _, troppo = self._corpo_al_massimo(h, 0)
        chi = re.sub(r"[^\w.@+-]", "", pulisci(h.headers.get("X-Authentik-Username"), 40))
        if troppo or h.command not in ("GET", "HEAD"):
            self._rispondi(h, 405, "Solo lettura")
            return
        if h.client_address[0] not in ("127.0.0.1", "::1") or not chi:
            self._rispondi(h, 403, "Il registro si legge solo dalla pagina della rete.")
            return
        try:
            with open(self.file_registro, "rb") as f:
                corpo = f.read(4 << 20)
        except FileNotFoundError:
            self._rispondi(h, 404, "Registro non ancora raccolto: chiedilo dalla pagina della rete.")
            return
        except OSError as e:
            self._rispondi(h, 500, f"Registro non leggibile ({e.strerror or e}).")
            return
        h.send_response(200)
        h.send_header("Content-Type", "text/plain; charset=utf-8")
        h.send_header("Cache-Control", "no-store")
        h.send_header("X-Content-Type-Options", "nosniff")
        h.send_header("Content-Length", str(len(corpo)))
        h.end_headers()
        if h.command != "HEAD":
            h.wfile.write(corpo)

    def _stato(self, h) -> None:
        try:
            dati = self.fornisci_stato() if self.fornisci_stato is not None else {}
        except Exception:  # noqa
            log.exception("Stato del ponte per la pagina della rete non disponibile")
            dati = {"errore": "stato non disponibile"}
        corpo = json.dumps(dati, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        h.send_response(200)
        h.send_header("Content-Type", "application/json; charset=utf-8")
        h.send_header("Cache-Control", "no-store")
        h.send_header("Content-Length", str(len(corpo)))
        h.end_headers()
        if h.command != "HEAD":
            h.wfile.write(corpo)

    def _inoltra(self, h) -> None:
        corpo = self._leggi_corpo(h)
        intestazioni = {k: v for k, v in h.headers.items()
                        if k.lower() not in self.SALTA and not k.lower().startswith("x-authentik-")}
        if self.nodo_https:
            c = http.client.HTTPSConnection(self.nodo_host, self.nodo_porta, timeout=15, context=self._ctx_nodo)
        else:
            c = http.client.HTTPConnection(self.nodo_host, self.nodo_porta, timeout=15)
        try:
            c.request(h.command, h.path, body=corpo, headers=intestazioni)
            r = c.getresponse()
            dati = r.read()
            stato, ragione, risposta = r.status, r.reason, r.getheaders()
        except Exception as e:  # noqa
            log.debug("Pagina di gestione: il nodo non risponde (%s)", e)
            self._rispondi(h, 502, "La pagina del nodo non risponde: meshtasticd e' acceso?")
            return
        finally:
            c.close()
        h.send_response(stato, ragione)
        for k, v in risposta:
            if k.lower() not in self.SALTA:
                h.send_header(k, v)
        h.send_header("Content-Length", str(len(dati)))
        h.end_headers()
        if h.command != "HEAD":
            h.wfile.write(dati)

    @staticmethod
    def _rispondi(h, stato: int, testo: str, tipo: str = "text/plain; charset=utf-8") -> None:
        corpo = testo.encode("utf-8")
        h.send_response(stato)
        h.send_header("Content-Type", tipo)
        if tipo.startswith("text/plain"):
            h.send_header("Access-Control-Allow-Origin", "*")
        else:
            h.send_header("Cache-Control", "no-store")
        h.send_header("Content-Length", str(len(corpo)))
        h.end_headers()
        if h.command != "HEAD":
            h.wfile.write(corpo)

    # ---- app Meshtastic via Wi-Fi ----

    def _accetta_app(self) -> None:
        while not self._fermo.is_set():
            try:
                app, indirizzo = self._ascolto_app.accept()
            except OSError:
                return
            threading.Thread(target=self._servi_app, args=(app, indirizzo[0]), name="gestione-app-1",
                             daemon=True).start()

    def _servi_app(self, app: socket.socket, da: str) -> None:
        # L'app parla per prima (chiede la configurazione del nodo): un collegamento muto, come una
        # prova della porta, non ferma il ponte.
        try:
            app.settimeout(30)
            primi = app.recv(4096)
        except OSError:
            primi = b""
        if not primi:
            self._chiudi(app)
            return
        with self._lock:
            ora = time.time()
            if self.app_bloccata and ora - self.ultimo_tentativo_app < self.cfg.gestione_silenzio_app_s:
                self.ultimo_tentativo_app = ora
                motivo = "bloccata"
            else:
                if self.app_bloccata:
                    log.info("L'app Meshtastic puo' di nuovo collegarsi al nodo")
                self.app_bloccata = False
                motivo = self._pausa(da, "dall'app Meshtastic")
        if motivo:
            log.debug("App Meshtastic da %s rifiutata: pausa massima appena finita", da)
            self._chiudi(app)
            return
        if self._aspetta_nodo():
            self._chiudi(app)
            return
        try:
            nodo = socket.create_connection(self.app_nodo, timeout=10)
            nodo.sendall(primi)
        except OSError as e:
            log.warning("App Meshtastic da %s: il nodo non risponde (%s)", da, e)
            self._chiudi(app)
            return
        with self._lock:
            vecchie, self._app = self._app, [(app, nodo)]   # come il nodo: l'ultima app collegata vince
        for a, n in vecchie:
            self._chiudi(a, n)
        try:
            self._travasa(app, nodo)
        finally:
            self._chiudi(app, nodo)
            with self._lock:
                self._app = [x for x in self._app if x[0] is not app]
                self.ultima_attivita = time.time()
            log.info("App Meshtastic da %s scollegata dal nodo", da)

    @staticmethod
    def _travasa(a: socket.socket, b: socket.socket) -> None:
        import selectors
        a.settimeout(None)
        b.settimeout(None)
        sel = selectors.DefaultSelector()
        sel.register(a, selectors.EVENT_READ, b)
        sel.register(b, selectors.EVENT_READ, a)
        try:
            while True:
                for chiave, _ in sel.select(timeout=1.0):
                    dati = chiave.fileobj.recv(4096)
                    if not dati:
                        return
                    chiave.data.sendall(dati)
        except (OSError, ValueError):
            return
        finally:
            sel.close()

    @staticmethod
    def _chiudi(*prese: socket.socket) -> None:
        for p in prese:
            try:
                p.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                p.close()
            except OSError:
                pass

    def _chiudi_app(self) -> None:
        with self._lock:
            aperte, self._app = self._app, []
        for a, n in aperte:
            self._chiudi(a, n)

    # ---- fine della pausa ----

    def _sorveglia(self) -> None:
        while not self._fermo.wait(1.0):
            self.controlla()

    def controlla(self, ora: Optional[float] = None) -> None:
        ora = time.time() if ora is None else ora
        chiudi_app = False
        with self._lock:
            if not self.pausa.attiva:
                return
            if ora - self.inizio >= self.cfg.gestione_pausa_massima_min * 60:
                self.scaduta = self.app_bloccata = True
                self.ultimo_tentativo_app = ora
                chiudi_app = True
                messaggio = (f"Pausa massima di {self.cfg.gestione_pausa_massima_min:g} minuti finita: il ponte "
                             "riprende il nodo (per continuare: ricarica la pagina, oppure chiudi l'app e "
                             f"riaprila dopo {self.cfg.gestione_silenzio_app_s / 60:g} minuti)")
            elif not self._app and ora - self.ultima_attivita >= self.cfg.gestione_chiusura_s:
                messaggio = (f"Gestione del nodo finita (nessuna richiesta da {self.cfg.gestione_chiusura_s:g} s): "
                             "il ponte riprende il nodo")
            else:
                return
            self.pausa.termina()
        if chiudi_app:
            self._chiudi_app()
        log.info(messaggio)


# --------------------------------------------------------------------------
# Gestione da lontano delle radio della squadra (dalla 1.10.0)
# --------------------------------------------------------------------------

PORTA_ADMIN = 6

ERRORI_RADIO = {
    "ADMIN_PUBLIC_KEY_UNAUTHORIZED": "su questa radio non c'e' la chiave di amministrazione di questo ponte: va "
                                     "inserita una volta, da vicino, con l'app Meshtastic (Sicurezza > Chiave "
                                     "amministratore)",
    "NOT_AUTHORIZED": "la radio non accetta comandi da questo ponte: manca la sua chiave di amministrazione (app "
                      "Meshtastic, da vicino: Sicurezza > Chiave amministratore)",
    "ADMIN_BAD_SESSION_KEY": "la radio ha rifiutato la sessione (scaduta): riprova",
    "PKI_UNKNOWN_PUBKEY": "il nodo del ponte non conosce ancora la chiave pubblica di questa radio: aspetta che la "
                          "radio si faccia sentire (manda il suo nome ogni qualche ora) e riprova",
    "PKI_SEND_FAIL_PUBLIC_KEY": "il nodo del ponte non conosce ancora la chiave pubblica di questa radio: aspetta "
                                "che la radio si faccia sentire e riprova",
    "PKI_FAILED": "cifratura non riuscita: la chiave pubblica che il ponte ricorda non e' piu' quella della radio "
                  "(radio azzerata?). Dal nodo del ponte togli quella radio dall'elenco e aspetta che si ripresenti",
    "MAX_RETRANSMIT": "la radio non risponde: spenta, fuori portata o in risparmio di energia",
    "NO_RESPONSE": "la radio non risponde a questa richiesta",
    "NO_ROUTE": "nessuna strada radio verso questa radio in questo momento",
    "TIMEOUT": "la radio non risponde: spenta, fuori portata o in risparmio di energia",
    "DUTY_CYCLE_LIMIT": "il nodo del ponte ha finito il tempo radio consentito per quest'ora: riprova piu' tardi",
    "TOO_LARGE": "troppi dati per un solo pacchetto radio",
    "BAD_REQUEST": "la radio non ha capito la richiesta (firmware troppo vecchio?)",
    "NO_CHANNEL": "il nodo del ponte e la radio non hanno un canale in comune",
}


class ErroreRadio(Exception):
    pass


class GestioneRadio:
    """Le radio della squadra gestite dalla pagina della rete, attraverso il nodo di questo ponte.

    Sono i messaggi di amministrazione di Meshtastic, cifrati con la chiave pubblica della radio: la radio li
    accetta solo se tra le sue "chiavi amministratore" c'e' la chiave pubblica del nodo del ponte. Ogni richiesta
    e' un pacchetto radio (e la risposta un altro): un'operazione alla volta, in coda, con il suo esito da
    rileggere. Le chiavi dei canali non escono mai dal ponte: alla pagina va solo "privata, uguale a quella del
    canale X del ponte". Solo le radio sentite sul canale della squadra.
    """

    ATTESA_RISPOSTA_S = 75.0       # il firmware ritrasmette da solo fino a tre volte
    SESSIONE_S = 240.0             # la radio accetta la chiave di sessione per 300 s
    LETTURA_VALE_S = 1800.0        # per cambiare una sezione va riletta da meno di mezz'ora
    MAX_OPERAZIONI = 40
    MAX_IN_CODA = 8
    AZIONI = ("leggi", "scrivi", "proprietario", "canale", "posizione_fissa", "togli_posizione_fissa", "riavvia")
    RE_RADIO = re.compile(r"^!([0-9a-f]{8})$")

    def __init__(self, ponte: "Ponte"):
        self.ponte = ponte
        self._lock = threading.Lock()
        self._ops: "OrderedDict[str, dict]" = OrderedDict()
        self._coda: "queue.Queue[str]" = queue.Queue()
        self._lette: Dict[Tuple[int, str], Tuple[float, object]] = {}
        self._sessioni: Dict[int, Tuple[float, bytes]] = {}
        self._sezioni: Optional[Dict[str, Tuple[str, int, object]]] = None
        self._schema: Optional[dict] = None
        self._thread: Optional[threading.Thread] = None

    # ---- sezioni delle impostazioni, dal protocollo stesso (cosi' seguono la libreria installata) ----

    def sezioni(self) -> Dict[str, Tuple[str, int, object]]:
        if self._sezioni is None:
            from meshtastic.protobuf import admin_pb2, localonly_pb2
            sez: Dict[str, Tuple[str, int, object]] = {}
            tipi = admin_pb2.AdminMessage.ConfigType
            for c in localonly_pb2.LocalConfig.DESCRIPTOR.fields:
                nome = c.name.upper() + "_CONFIG"
                if c.message_type is not None and nome in tipi.keys():
                    sez[c.name] = ("config", tipi.Value(nome), c.message_type)
            moduli = admin_pb2.AdminMessage.DESCRIPTOR.fields_by_name["set_module_config"].message_type
            for c in localonly_pb2.LocalModuleConfig.DESCRIPTOR.fields:
                if c.message_type is not None and c.name in moduli.fields_by_name:
                    sez[c.name] = ("modulo", c.index, c.message_type)
            self._sezioni = sez
        return self._sezioni

    @classmethod
    def _campi(cls, descrittore, profondita: int = 0) -> List[dict]:
        campi = []
        for c in descrittore.fields:
            if c.GetOptions().deprecated or profondita > 3:
                continue
            ripetuto = getattr(c, "is_repeated", None)
            if ripetuto is None:
                ripetuto = c.label == c.LABEL_REPEATED
            v: dict = {"nome": c.name, "ripetuto": bool(ripetuto)}
            if c.type == c.TYPE_MESSAGE:
                v.update(tipo="gruppo", campi=cls._campi(c.message_type, profondita + 1))
            elif c.type == c.TYPE_ENUM:
                v.update(tipo="scelta", opzioni=[x.name for x in c.enum_type.values])
            elif c.type == c.TYPE_BOOL:
                v["tipo"] = "si_no"
            elif c.type == c.TYPE_STRING:
                v["tipo"] = "testo"
            elif c.type == c.TYPE_BYTES:
                v["tipo"] = "byte"
            elif c.type in (c.TYPE_FLOAT, c.TYPE_DOUBLE):
                v["tipo"] = "decimale"
            else:
                v["tipo"] = "intero"
            campi.append(v)
        return campi

    def schema(self) -> dict:
        """Per la pagina: le sezioni e i loro campi, con i nomi del protocollo Meshtastic (quelli dell'app)."""
        if self._schema is None:
            from meshtastic.protobuf import channel_pb2, config_pb2
            self._schema = {
                "sezioni": [{"nome": n, "modulo": t == "modulo", "sola_lettura": n == "security",
                             "campi": self._campi(d)} for n, (t, _i, d) in self.sezioni().items()],
                "ruoli_canale": list(channel_pb2.Channel.Role.keys()),
                "ruoli": list(config_pb2.Config.DeviceConfig.Role.keys()),
            }
        return self._schema

    @staticmethod
    def _in_dizionario(msg) -> dict:
        from google.protobuf import json_format
        try:
            return json_format.MessageToDict(msg, preserving_proto_field_name=True,
                                             always_print_fields_with_no_presence=True)
        except TypeError:           # protobuf piu' vecchio
            return json_format.MessageToDict(msg, preserving_proto_field_name=True,
                                             including_default_value_fields=True)

    # ---- richieste dalla pagina della rete ----

    def avvia(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._ciclo, name="gestione-radio", daemon=True)
            self._thread.start()

    def servi(self, h, percorso: str, corpo: Optional[bytes]) -> Tuple[int, dict]:
        """Risposta (stato HTTP, JSON) a una richiesta /mesh2tak/radio/... arrivata dal tunnel."""
        chi = re.sub(r"[^\w.@+-]", "", pulisci(h.headers.get("X-Authentik-Username"), 40))
        if h.client_address[0] not in ("127.0.0.1", "::1") or not chi:
            return 403, {"errore": "Le radio si gestiscono solo dalla pagina della rete."}
        dove = percorso[len("/mesh2tak/radio"):]
        if h.command == "GET" and dove == "/schema":
            return 200, self.schema()
        if h.command == "GET" and dove == "/op":
            ident = urllib.parse.parse_qs(urllib.parse.urlsplit(h.path).query).get("id", [""])[0]
            with self._lock:
                op = self._ops.get(ident)
                if op is None:
                    return 404, {"errore": "operazione non trovata (ponte riavviato?)"}
                return 200, json.loads(json.dumps({k: v for k, v in op.items() if not k.startswith("_")}))
        if h.command == "GET" and dove == "/ops":
            with self._lock:
                return 200, {"operazioni": [{k: v for k, v in op.items() if k != "risultato" and not k.startswith("_")}
                                            for op in list(self._ops.values())[-15:]]}
        if h.command == "POST" and dove == "/op":
            origine = urllib.parse.urlsplit(h.headers.get("Origin") or "").netloc
            ospite = h.headers.get("X-Forwarded-Host") or h.headers.get("Host") or ""
            if h.headers.get("X-Mesh2tak") != "1" or (origine and origine != ospite):
                return 403, {"errore": "richiesta non valida"}
            try:
                dati = json.loads((corpo or b"")[:16384].decode("utf-8"))
                if not isinstance(dati, dict):
                    raise ValueError
            except (ValueError, UnicodeDecodeError):
                return 400, {"errore": "richiesta non valida"}
            try:
                return 202, {"op": self.accoda(dati, chi)}
            except ErroreRadio as e:
                return 400, {"errore": str(e)}
        return 404, {"errore": "non trovato"}

    def accoda(self, dati: dict, chi: str) -> str:
        m = self.RE_RADIO.match(str(dati.get("radio") or ""))
        azione = str(dati.get("azione") or "")
        if not m or azione not in self.AZIONI:
            raise ErroreRadio("richiesta non valida")
        num = int(m.group(1), 16)
        with self.ponte._lock:
            vista = self.ponte.viste.get(num)
            nome = (vista.nome or self.ponte._nome(num)) if vista else ""
        if vista is None or num == self.ponte.mio_num:
            raise ErroreRadio("questo ponte non ha sentito quella radio sul canale della squadra")
        param = self._controlla(azione, dati)
        with self._lock:
            if sum(1 for op in self._ops.values() if op["stato"] in ("in_coda", "in_corso")) >= self.MAX_IN_CODA:
                raise ErroreRadio("troppe operazioni in coda su questo ponte: aspetta che finiscano")
            ident = uuid.uuid4().hex[:16]
            self._ops[ident] = {"id": ident, "radio": f"!{num:08x}", "nome": nome, "azione": azione, "stato": "in_coda",
                                "passo": "in coda", "risultato": {}, "errore": None, "chi": chi,
                                "creata": round(time.time()), "finita": None, "_num": num, "_param": param}
            while len(self._ops) > self.MAX_OPERAZIONI:
                self._ops.popitem(last=False)
        log.info("Gestione radio: %s chiede '%s'%s su %s (!%08x)", chi, azione,
                 f" {param.get('sezione') or ','.join(param.get('sezioni', []))}".rstrip(), nome, num)
        self.avvia()
        self._coda.put(ident)
        return ident

    def _controlla(self, azione: str, dati: dict) -> dict:
        """I parametri di un'operazione, controllati prima di metterla in coda."""
        sezioni = self.sezioni()
        if azione == "leggi":
            chieste = dati.get("sezioni")
            ammesse = set(sezioni) | {"proprietario", "info", "canali"}
            if not isinstance(chieste, list) or not chieste or len(chieste) > 6 or not set(chieste) <= ammesse:
                raise ErroreRadio("sezioni da leggere non valide")
            return {"sezioni": list(dict.fromkeys(chieste))}
        if azione == "scrivi":
            sez = str(dati.get("sezione") or "")
            if sez not in sezioni or not isinstance(dati.get("valori"), dict):
                raise ErroreRadio("sezione non valida")
            if sez == "security":
                raise ErroreRadio("la sezione Sicurezza da lontano si puo' solo leggere: cambiarla senza la chiave "
                                  "privata della radio puo' farle perdere l'identita'. Si cambia da vicino, con l'app")
            return {"sezione": sez, "valori": dati["valori"]}
        if azione == "proprietario":
            nome, breve = pulisci(dati.get("nome"), 60), pulisci(dati.get("nome_breve"), 8)
            if not nome or not breve or len(nome.encode()) > 39 or len(breve.encode()) > 4:
                raise ErroreRadio("nome: al massimo 39 caratteri; sigla: al massimo 4")
            return {"nome": nome, "nome_breve": breve}
        if azione == "canale":
            from meshtastic.protobuf import channel_pb2
            try:
                indice = int(dati.get("indice"))
                precisione = int(dati.get("precisione") or 0)
            except (TypeError, ValueError):
                raise ErroreRadio("canale non valido")
            ruolo, nome = str(dati.get("ruolo") or ""), str(dati.get("nome") or "")
            chiave = dati.get("chiave") if isinstance(dati.get("chiave"), dict) else {}
            modo = str(chiave.get("modo") or "tieni")
            if (not 0 <= indice <= 7 or ruolo not in channel_pb2.Channel.Role.keys() or len(nome.encode()) > 11
                    or not re.match(r"^[\w .+-]*$", nome) or not 0 <= precisione <= 32
                    or modo not in ("tieni", "ponte", "nuova", "predefinita", "nessuna")):
                raise ErroreRadio("canale non valido")
            if (indice == 0) != (ruolo == "PRIMARY"):
                raise ErroreRadio("il canale 0 e' sempre il primario, gli altri mai")
            p = {"indice": indice, "ruolo": ruolo, "nome": nome, "modo": modo, "precisione": precisione,
                 "uplink": dati.get("uplink") is True, "downlink": dati.get("downlink") is True}
            if modo == "nuova":
                try:
                    p["psk"] = leggi_chiave_canale(str(chiave.get("valore") or ""))
                except ValueError as e:
                    raise ErroreRadio(str(e))
            elif modo == "ponte":
                try:
                    p["canale_ponte"] = int(chiave.get("canale_ponte"))
                except (TypeError, ValueError):
                    raise ErroreRadio("canale del ponte non valido")
            return p
        if azione == "posizione_fissa":
            try:
                lat, lon, quota = float(dati.get("lat")), float(dati.get("lon")), int(float(dati.get("quota") or 0))
            except (TypeError, ValueError):
                raise ErroreRadio("coordinate non valide")
            if not (-90 <= lat <= 90 and -180 <= lon <= 180 and -500 <= quota <= 9000) or (lat == 0 and lon == 0):
                raise ErroreRadio("coordinate non valide")
            return {"lat": lat, "lon": lon, "quota": quota}
        return {}

    # ---- lavoro: un'operazione alla volta ----

    def _ciclo(self) -> None:
        while True:
            ident = self._coda.get()
            with self._lock:
                op = self._ops.get(ident)
                if op is None:
                    continue
                op.update(stato="in_corso", passo="parto")
            try:
                self._esegui(op)
                esito, errore = "fatto", None
            except ErroreRadio as e:
                esito, errore = "errore", str(e)
            except Exception as e:  # noqa
                log.exception("Gestione radio: errore inatteso")
                esito, errore = "errore", f"errore inatteso del ponte ({e})"
            with self._lock:
                op.update(stato=esito, errore=errore, finita=round(time.time()))
                if esito == "fatto":
                    op["passo"] = "fatto"
            log.info("Gestione radio: '%s' su %s: %s", op["azione"], op["nome"] or op["radio"],
                     "fatto" if errore is None else f"non riuscito ({errore})")

    def _passo(self, op: dict, testo: str) -> None:
        with self._lock:
            op["passo"] = testo

    def _esegui(self, op: dict) -> None:
        from meshtastic.protobuf import admin_pb2, mesh_pb2
        num, par, azione = op["_num"], op["_param"], op["azione"]
        if azione == "leggi":
            for i, sez in enumerate(par["sezioni"]):
                self._passo(op, f"chiedo '{sez}' alla radio ({i + 1} di {len(par['sezioni'])}): puo' volerci un minuto")
                valore = self._leggi(op, num, sez)
                with self._lock:
                    op["risultato"][sez] = valore
            return
        msg = admin_pb2.AdminMessage()
        fatto = "Fatto."
        if azione == "scrivi":
            sez = par["sezione"]
            tipo, _indice, _d = self.sezioni()[sez]
            nuova = self._modifica(num, sez, par["valori"])
            getattr(msg.set_config if tipo == "config" else msg.set_module_config, sez).CopyFrom(nuova)
            fatto = "Salvato: la radio si riavvia da sola per applicarlo (qualche secondo)."
        elif azione == "proprietario":
            msg.set_owner.long_name, msg.set_owner.short_name = par["nome"], par["nome_breve"]
            vecchio = self._letta(num, "proprietario")
            if vecchio is not None:
                msg.set_owner.is_licensed = vecchio.is_licensed
            fatto = "Nome cambiato: le altre radio lo vedono quando la radio lo ritrasmette."
        elif azione == "canale":
            msg.set_channel.CopyFrom(self._canale_nuovo(num, par))
            fatto = "Canale salvato sulla radio."
        elif azione == "posizione_fissa":
            msg.set_fixed_position.CopyFrom(mesh_pb2.Position(
                latitude_i=int(round(par["lat"] * 1e7)), longitude_i=int(round(par["lon"] * 1e7)),
                altitude=par["quota"], time=int(time.time())))
            fatto = "Posizione fissa impostata."
        elif azione == "togli_posizione_fissa":
            msg.remove_fixed_position = True
            fatto = "Posizione fissa tolta: la radio torna a usare il suo GPS."
        elif azione == "riavvia":
            msg.reboot_seconds = 5
            fatto = "La radio si riavvia tra 5 secondi."
        self._sessione(op, num)
        self._passo(op, "mando il comando alla radio e aspetto la conferma: puo' volerci un minuto")
        try:
            self._scambio(num, msg, False)
        except ErroreRadio as e:
            if "sessione" not in str(e):
                raise
            self._sessioni.pop(num, None)          # sessione scaduta nel frattempo: una nuova, una volta sola
            self._sessione(op, num)
            self._scambio(num, msg, False)
        if azione in ("scrivi", "riavvia"):
            self._sessioni.pop(num, None)          # la radio si riavvia: la sessione non vale piu'
        if azione == "scrivi":
            self._lette[(num, par["sezione"])] = (time.time(), nuova)
        elif azione == "canale":
            self._lette[(num, f"canale{par['indice']}")] = (time.time(), msg.set_channel)
        with self._lock:
            op["risultato"]["messaggio"] = fatto

    def _letta(self, num: int, cosa: str):
        voce = self._lette.get((num, cosa))
        return voce[1] if voce and time.time() - voce[0] < self.LETTURA_VALE_S else None

    def _leggi(self, op: dict, num: int, sez: str):
        from meshtastic.protobuf import admin_pb2, channel_pb2, config_pb2, mesh_pb2
        msg = admin_pb2.AdminMessage()
        if sez == "proprietario":
            msg.get_owner_request = True
            u = self._scambio(num, msg, True).get_owner_response
            self._lette[(num, sez)] = (time.time(), u)
            return {"nome": u.long_name, "nome_breve": u.short_name}
        if sez == "info":
            msg.get_device_metadata_request = True
            d = self._scambio(num, msg, True).get_device_metadata_response

            def nome_di(enum, valore):
                try:
                    return enum.Name(valore)
                except ValueError:
                    return str(valore)
            return {"firmware": d.firmware_version, "modello": nome_di(mesh_pb2.HardwareModel, d.hw_model),
                    "ruolo": nome_di(config_pb2.Config.DeviceConfig.Role, d.role), "bluetooth": d.hasBluetooth,
                    "wifi": d.hasWifi, "chiavi_pubbliche": d.hasPKC}
        if sez == "canali":
            canali = []
            for i in range(8):
                self._passo(op, f"chiedo il canale {i} alla radio: puo' volerci un minuto")
                msg = admin_pb2.AdminMessage()
                msg.get_channel_request = i + 1
                ch = self._scambio(num, msg, True).get_channel_response
                self._lette[(num, f"canale{i}")] = (time.time(), ch)
                if i and ch.role == channel_pb2.Channel.Role.DISABLED:
                    break                          # i canali sono in fila: dopo il primo spento non ce ne sono altri
                canali.append(self._vista_canale(ch))
            return canali
        tipo, indice, _d = self.sezioni()[sez]
        if tipo == "config":
            msg.get_config_request = indice
            r = getattr(self._scambio(num, msg, True).get_config_response, sez)
        else:
            msg.get_module_config_request = indice
            r = getattr(self._scambio(num, msg, True).get_module_config_response, sez)
        self._lette[(num, sez)] = (time.time(), r)
        valori = self._in_dizionario(r)
        if sez == "security":
            valori.pop("private_key", None)        # non deve arrivare a nessuna pagina
        return valori

    def _canali_ponte(self) -> list:
        iface = self.ponte.iface
        return list(getattr(getattr(iface, "localNode", None), "channels", None) or []) if iface else []

    def _vista_canale(self, ch) -> dict:
        from meshtastic.protobuf import channel_pb2
        psk = bytes(ch.settings.psk)
        if not psk:
            chiave: dict = {"tipo": "nessuna"}
        elif len(psk) == 1:
            chiave = {"tipo": "predefinita"}       # la chiave nota a tutti dei canali pubblici
        else:
            uguale = next((c for c in self._canali_ponte() if c.role and bytes(c.settings.psk) == psk), None)
            chiave = {"tipo": "privata", "bit": len(psk) * 8,
                      "come_ponte": (uguale.settings.name or f"canale {uguale.index}") if uguale is not None else None}
        return {"indice": ch.index, "ruolo": channel_pb2.Channel.Role.Name(ch.role), "nome": ch.settings.name,
                "chiave": chiave, "uplink": ch.settings.uplink_enabled, "downlink": ch.settings.downlink_enabled,
                "precisione": ch.settings.module_settings.position_precision}

    def _canale_nuovo(self, num: int, par: dict):
        from meshtastic.protobuf import channel_pb2
        ch = channel_pb2.Channel()
        vecchio = self._letta(num, f"canale{par['indice']}")
        if vecchio is not None:
            ch.CopyFrom(vecchio)
        elif par["modo"] == "tieni" and par["ruolo"] != "DISABLED":
            raise ErroreRadio("prima rileggi i canali della radio: per tenere la chiave com'e' devo conoscerla")
        ch.index = par["indice"]
        ch.role = channel_pb2.Channel.Role.Value(par["ruolo"])
        if par["ruolo"] == "DISABLED":
            ch.settings.Clear()
            return ch
        ch.settings.name = par["nome"]
        ch.settings.uplink_enabled, ch.settings.downlink_enabled = par["uplink"], par["downlink"]
        ch.settings.module_settings.position_precision = par["precisione"]
        if par["modo"] == "nuova":
            ch.settings.psk = par["psk"]
        elif par["modo"] == "predefinita":
            ch.settings.psk = b"\x01"
        elif par["modo"] == "nessuna":
            ch.settings.psk = b""
        elif par["modo"] == "ponte":
            mio = next((c for c in self._canali_ponte() if c.index == par["canale_ponte"] and c.role), None)
            if mio is None:
                raise ErroreRadio("il ponte non ha quel canale")
            ch.settings.psk = bytes(mio.settings.psk)
            ch.settings.name = mio.settings.name
        return ch

    def _modifica(self, num: int, sez: str, valori: dict):
        """La sezione letta dalla radio con dentro i valori cambiati sulla pagina (il resto resta com'era)."""
        from google.protobuf import json_format
        letta = self._letta(num, sez)
        if letta is None:
            raise ErroreRadio(f"prima rileggi '{sez}' dalla radio: la cambio partendo da quello che ha adesso")
        nuova = type(letta)()
        nuova.CopyFrom(letta)
        for nome, valore in valori.items():
            if nome in nuova.DESCRIPTOR.fields_by_name and isinstance(valore, list):
                nuova.ClearField(nome)             # un elenco si sostituisce, non si allunga
        try:
            json_format.ParseDict(valori, nuova)
        except (json_format.ParseError, ValueError, TypeError) as e:
            raise ErroreRadio(f"valori non validi per '{sez}': {pulisci(str(e), 160)}")
        return nuova

    def _sessione(self, op: dict, num: int) -> None:
        """I comandi che cambiano qualcosa vogliono la chiave di sessione che la radio da' con ogni risposta."""
        from meshtastic.protobuf import admin_pb2
        if time.time() - self._sessioni.get(num, (0.0, b""))[0] < self.SESSIONE_S:
            return
        self._passo(op, "chiedo alla radio il permesso (chiave di sessione): puo' volerci un minuto")
        msg = admin_pb2.AdminMessage()
        msg.get_config_request = admin_pb2.AdminMessage.ConfigType.SESSIONKEY_CONFIG
        self._scambio(num, msg, True)
        if num not in self._sessioni:
            raise ErroreRadio("la radio non ha dato la chiave di sessione (firmware piu' vecchio della 2.5?)")

    def _scambio(self, num: int, msg, vuole_risposta: bool):
        """Manda un messaggio di amministrazione alla radio e aspetta: la risposta, o la conferma di ricezione."""
        iface = self.ponte.iface
        if iface is None:
            raise ErroreRadio("il ponte non e' collegato al suo nodo in questo momento (in pausa o in riavvio): "
                              "riprova tra un minuto")
        if not ((iface.nodesByNum.get(num) or {}).get("user") or {}).get("publicKey"):
            raise ErroreRadio(ERRORI_RADIO["PKI_UNKNOWN_PUBKEY"])
        quando, chiave = self._sessioni.get(num, (0.0, b""))
        if chiave and time.time() - quando < 300:
            msg.session_passkey = chiave
        arrivato = threading.Event()
        esito: dict = {}
        ident = [0]

        def risposta(p):
            d = p.get("decoded") or {}
            instradamento = d.get("routing")
            if instradamento is not None:
                motivo = instradamento.get("errorReason", "NONE")
                if motivo != "NONE":
                    esito["errore"] = motivo
                elif p.get("from") == num:
                    esito["ok"] = True
                else:
                    # conferma "implicita": una radio vicina l'ha ripetuto. Aspetto quella della radio vera
                    esito["implicita"] = True
                    try:
                        iface._addResponseHandler(ident[0], risposta, ackPermitted=True)
                    except Exception:  # noqa
                        pass
                    return
            else:
                grezzo = (d.get("admin") or {}).get("raw")
                if grezzo is None:
                    return
                esito["admin"] = grezzo
                if grezzo.session_passkey:
                    self._sessioni[num] = (time.time(), bytes(grezzo.session_passkey))
            arrivato.set()

        try:
            with getattr(self.ponte, "_lock_invio", threading.Lock()):
                inviato = iface.sendData(msg, num, portNum=PORTA_ADMIN, wantAck=True, wantResponse=vuole_risposta,
                                         onResponse=risposta, onResponseAckPermitted=not vuole_risposta,
                                         pkiEncrypted=True)
        except Exception as e:  # noqa
            raise ErroreRadio(f"il nodo del ponte non ha preso il comando ({pulisci(str(e), 120)})")
        ident[0] = getattr(inviato, "id", 0)
        if not arrivato.wait(self.ATTESA_RISPOSTA_S):
            try:
                iface.responseHandlers.pop(ident[0], None)
            except Exception:  # noqa
                pass
            if esito.get("implicita"):
                raise ErroreRadio("il comando e' partito e un'altra radio l'ha ripetuto, ma la radio non ha "
                                  "confermato: rileggi per vedere se e' arrivato")
            raise ErroreRadio(f"la radio non ha risposto in {self.ATTESA_RISPOSTA_S:.0f} secondi: spenta, fuori "
                              "portata o in risparmio di energia")
        if "errore" in esito:
            raise ErroreRadio(ERRORI_RADIO.get(esito["errore"], f"la radio ha risposto con un errore ({esito['errore']})"))
        return esito.get("admin")


# --------------------------------------------------------------------------
# Accesso da internet: tunnel SSH verso il server della rete Meshtastic
# --------------------------------------------------------------------------

def impronta_chiave(riga: str) -> str:
    """Impronta (sha256 esadecimale) di una chiave pubblica SSH: il server pubblica con questo nome
    la registrazione del ponte, cosi' la trova solo chi ha la chiave."""
    import base64
    import binascii
    parti = riga.split()
    if len(parti) < 2:
        raise ValueError("chiave pubblica non valida")
    try:
        blob = base64.b64decode(parti[1], validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("chiave pubblica non valida")
    return hashlib.sha256(blob).hexdigest()


CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def codice_controllo(impronta: str) -> str:
    """Sei caratteri dall'impronta della chiave, calcolati come sul server: l'installatore li mostra e la
    pagina della rete li mostra accanto alla richiesta, cosi' chi approva sa che e' questo Raspberry."""
    n = int(impronta[:8], 16) >> 2
    c = "".join(CROCKFORD[(n >> (5 * i)) & 31] for i in range(5, -1, -1))
    return f"{c[:3]}-{c[3:]}"


class AccessoEsterno:
    """Pagina di gestione e stato del ponte raggiungibili da internet, dalla pagina della rete Meshtastic.

    E' il Raspberry ad aprire un tunnel SSH verso il server (a casa non si apre nessuna porta): sul server
    la porta assegnata al ponte porta alla pagina del ponte (9443), dietro Authentik. L'utente del ponte
    sul server puo' solo tenere aperto quel tunnel. Prima di collegarsi il ponte legge via HTTPS la sua
    registrazione (utente, porta e chiave del server): finche' il server non l'ha pubblicata non bussa
    con una chiave sconosciuta, che fail2ban sul server punirebbe bloccando l'indirizzo di casa.
    Senza registrazione manda alla pagina della rete la richiesta di entrare (nome e chiave pubblica, ogni
    ora): un amministratore la approva sulla pagina, confrontando il codice di controllo.
    """

    ATTESA_REGISTRAZIONE = (60.0, 300.0)   # ogni minuto per due ore, poi ogni 5 minuti
    VELOCE_S = 7200.0
    RICHIESTA_OGNI_S = 3600.0               # la richiesta si ripete ogni ora finche' non e' approvata
    RICHIESTA_RIPROVA_S = 300.0             # pagina della rete che non riceve le richieste
    ATTESA_RETE = (15.0, 300.0)
    ATTESA_RIFIUTO_S = 900.0                # chiave rifiutata: piano, per stare lontani da fail2ban
    ATTESA_PORTA_OCCUPATA_S = 60.0          # sul server il tunnel di prima scade entro 90 s
    DURATA_STABILE_S = 120.0
    CONFERMA_S = 3.0                         # vivo cosi' tanto dopo l'accesso: tunnel aperto

    RE_UTENTE = re.compile(r"^ponte-[a-z0-9-]{1,30}$")
    RE_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
    RE_CHIAVE_HOST = re.compile(r"^(ssh-ed25519|ecdsa-sha2-nistp(?:256|384|521)|rsa-sha2-(?:256|512)|ssh-rsa) "
                                r"[A-Za-z0-9+/]+={0,3}$")

    def __init__(self, cfg: Config, porta_locale: int):
        self.cfg = cfg
        self.porta_locale = porta_locale
        self.rete = cfg.accesso_rete.rstrip("/")
        with open(cfg.accesso_chiave + ".pub", encoding="utf-8") as f:
            self.chiave_pubblica = f.read().strip()
        self.impronta = impronta_chiave(self.chiave_pubblica)
        self.controllo = codice_controllo(self.impronta)
        self.url_registrazione = f"{self.rete}/accesso/{self.impronta}.json"
        self.macchina = (re.sub(r"[^A-Za-z0-9._-]", "-", socket.gethostname() or "")[:63].strip(".-")
                         or "raspberry")
        self.known_hosts = os.path.join(cfg.accesso_cartella_stato, "server-rete.known_hosts")
        self.ssh = shutil.which("ssh") or "/usr/bin/ssh"
        self.contesto_https = ssl.create_default_context()
        self._lock = threading.Lock()
        self._fermo = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self._thread = threading.Thread(target=self._ciclo, name="accesso-esterno", daemon=True)
        self.collegato = False
        self.dal: Optional[float] = None
        self.indirizzo: Optional[str] = None
        self.messaggio = "in avvio"
        self.richiesta: Optional[str] = None    # esito dell'ultima richiesta alla pagina della rete
        self._ultimo_messaggio = ""

    def avvia(self) -> None:
        self._thread.start()

    def ferma(self) -> None:
        self._fermo.set()
        with self._lock:
            proc = self._proc
        if proc is not None:
            self._termina(proc)
        self._thread.join(timeout=10)

    def stato(self) -> dict:
        with self._lock:
            return {"collegato": self.collegato, "dal": round(self.dal) if self.dal else None,
                    "indirizzo": self.indirizzo, "messaggio": self.messaggio, "controllo": self.controllo,
                    "richiesta": self.richiesta}

    def _segna(self, collegato: bool, messaggio: str, livello: int = logging.INFO) -> None:
        with self._lock:
            if collegato and not self.collegato:
                self.dal = time.time()
            elif not collegato:
                self.dal = None
            self.collegato, self.messaggio = collegato, messaggio
        if messaggio != self._ultimo_messaggio:   # lo stesso avviso una volta sola, non a ogni tentativo
            log.log(livello, "Accesso da internet: %s", messaggio)
            self._ultimo_messaggio = messaggio

    # ---- registrazione pubblicata dal server ----

    def _registrazione(self) -> Optional[dict]:
        """La registrazione del ponte, None se il server non l'ha (ancora) pubblicata."""
        apri = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                           urllib.request.HTTPSHandler(context=self.contesto_https))
        richiesta = urllib.request.Request(self.url_registrazione, headers={
            "User-Agent": f"mesh2tak/{VERSIONE}", "Cache-Control": "no-cache"})
        try:
            with apri.open(richiesta, timeout=20) as r:
                dati = json.loads(r.read(65536).decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise OSError(f"la pagina della rete risponde {e.code}")
        return self.valida(dati)

    # ---- richiesta di entrare nella rete ----

    def _richiesta(self) -> Tuple[int, dict]:
        """Manda alla pagina della rete nome e chiave pubblica del ponte: (codice HTTP, risposta)."""
        corpo = json.dumps({"formato": 1, "nome": self.cfg.accesso_nome, "chiave": self.chiave_pubblica,
                            "macchina": self.macchina, "versione": VERSIONE}).encode("utf-8")
        apri = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                           urllib.request.HTTPSHandler(context=self.contesto_https))
        richiesta = urllib.request.Request(f"{self.rete}/accesso/richiesta", data=corpo, method="POST", headers={
            "User-Agent": f"mesh2tak/{VERSIONE}", "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with apri.open(richiesta, timeout=20) as r:
                codice, testo = r.status, r.read(65536)
        except urllib.error.HTTPError as e:
            codice, testo = e.code, e.read(65536)
        try:
            dati = json.loads(testo.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            dati = {}
        return codice, dati if isinstance(dati, dict) else {}

    def _manda_richiesta(self) -> Tuple[str, int, float]:
        """(messaggio, livello del registro, fra quanto ripetere la richiesta)."""
        c = self.controllo
        try:
            codice, d = self._richiesta()
        except Exception as e:  # noqa: rete, DNS, certificato
            self.richiesta = "non_mandata"
            return (f"richiesta alla pagina della rete non mandata ({e}): riprovo tra 5 minuti", logging.WARNING,
                    self.RICHIESTA_RIPROVA_S)
        errore = re.sub(r"\s+", " ", str(d.get("errore") or ""))[:300]
        if codice == 200:
            self.richiesta = "registrato"
            return "approvato sulla pagina della rete: leggo la registrazione", logging.INFO, self.RICHIESTA_OGNI_S
        if codice == 202:
            self.richiesta = "in_attesa"
            extra = (f". C'e' gia' un ponte '{self.cfg.accesso_nome}' nella rete: sulla pagina va scelto 'Sostituisci'"
                     if d.get("sostituisce") else "")
            return (f"in attesa di approvazione sulla pagina della rete {self.rete} (codice di controllo {c}){extra}",
                    logging.INFO, self.RICHIESTA_OGNI_S)
        if codice == 403:
            self.richiesta = "rifiutata"
            return (f"{errore or 'richiesta rifiutata sulla pagina della rete'}: per farlo rientrare, sulla pagina "
                    f"'Lascia rientrare' (codice di controllo {c}). Riprovo ogni ora", logging.WARNING,
                    self.RICHIESTA_OGNI_S)
        if codice == 429:
            self.richiesta = "troppe"
            return (f"la pagina della rete non accetta altre richieste adesso ({errore or 'troppe richieste'}): "
                    "riprovo tra un'ora", logging.WARNING, self.RICHIESTA_OGNI_S)
        if codice == 400:
            self.richiesta = "non_valida"
            return (f"la pagina della rete non accetta la richiesta ({errore or 'non valida'}): rilancia "
                    "l'installatore con --accesso-esterno", logging.ERROR, self.RICHIESTA_OGNI_S)
        if codice in (404, 405, 501, 502, 503, 504):
            self.richiesta = "server_da_preparare"
            return (f"la pagina della rete non riceve ancora le richieste dei ponti (risponde {codice}): sul server, "
                    "una volta sola,  sudo bash rete-meshtastic.sh installa  (riprovo tra 5 minuti)",
                    logging.WARNING, self.RICHIESTA_RIPROVA_S)
        self.richiesta = "errore"
        return (f"la pagina della rete risponde {codice} alla richiesta" + (f" ({errore})" if errore else "")
                + ": riprovo tra 5 minuti", logging.WARNING, self.RICHIESTA_RIPROVA_S)

    @classmethod
    def valida(cls, d) -> dict:
        if not isinstance(d, dict):
            raise ValueError("registrazione del server non valida")
        utente, porta = str(d.get("utente", "")), d.get("porta")
        host, porta_ssh = str(d.get("ssh_host", "")), d.get("ssh_porta", 22)
        chiavi = d.get("chiavi_host")
        if (not cls.RE_UTENTE.match(utente) or not isinstance(porta, int) or not 1024 <= porta <= 65535
                or not cls.RE_HOST.match(host) or not isinstance(porta_ssh, int) or not 1 <= porta_ssh <= 65535
                or not isinstance(chiavi, list) or not chiavi
                or not all(isinstance(c, str) and cls.RE_CHIAVE_HOST.match(c.strip()) for c in chiavi)):
            raise ValueError("registrazione del server non valida")
        indirizzo = str(d.get("indirizzo") or "")
        if not re.match(r"^https://[A-Za-z0-9.-]+(?::\d+)?/?$", indirizzo):
            indirizzo = ""
        return {"utente": utente, "porta": porta, "host": host, "porta_ssh": porta_ssh,
                "chiavi": [c.strip() for c in chiavi], "indirizzo": indirizzo or None}

    def _scrivi_known_hosts(self, reg: dict) -> None:
        # Le chiavi del server arrivano con la registrazione, via HTTPS con certificato valido:
        # il primo collegamento SSH non si fida alla cieca.
        os.makedirs(os.path.dirname(self.known_hosts), exist_ok=True)
        nuovo = self.known_hosts + ".nuovo"
        with open(nuovo, "w", encoding="ascii") as f:
            f.writelines(f"mesh2tak-rete {c}\n" for c in reg["chiavi"])
        os.replace(nuovo, self.known_hosts)

    # ---- tunnel ----

    def comando(self, reg: dict) -> List[str]:
        return [self.ssh, "-F", "/dev/null", "-N", "-T", "-i", self.cfg.accesso_chiave,
                "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none", "-o", "BatchMode=yes",
                "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
                "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={self.known_hosts}",
                "-o", "GlobalKnownHostsFile=/dev/null", "-o", "HostKeyAlias=mesh2tak-rete",
                "-o", "UpdateHostKeys=no", "-o", "CheckHostIP=no",
                "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
                "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=20", "-o", "LogLevel=VERBOSE",
                "-p", str(reg["porta_ssh"]),
                "-R", f"127.0.0.1:{reg['porta']}:127.0.0.1:{self.porta_locale}",
                f"{reg['utente']}@{reg['host']}"]

    @staticmethod
    def classifica(righe: List[str]) -> str:
        testo = "\n".join(righe).lower()
        if "remote port forwarding failed" in testo:
            return "porta"
        if ("host key verification failed" in testo or "remote host identification has changed" in testo
                or "host key is known" in testo):
            return "chiave_server"
        if "permission denied" in testo or "too many authentication failures" in testo:
            return "rifiuto"
        return "rete"

    @staticmethod
    def _termina(proc: subprocess.Popen) -> None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa
            try:
                proc.kill()
            except Exception:  # noqa
                pass

    def _tunnel(self, reg: dict) -> Tuple[str, float, bool, List[str]]:
        """Tiene aperto il tunnel finche' regge: (esito, durata, era aperto, ultime righe di ssh)."""
        righe: "deque[str]" = deque(maxlen=20)
        autenticato = [0.0]
        try:
            proc = subprocess.Popen(self.comando(reg), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, text=True, errors="replace")
        except OSError as e:
            return "rete", 0.0, False, [f"ssh non si avvia: {e}"]
        with self._lock:
            self._proc = proc

        def leggi():
            for riga in proc.stderr:
                riga = riga.strip()
                if riga:
                    righe.append(riga)
                    if riga.startswith("Authenticated to") and not autenticato[0]:
                        autenticato[0] = time.time()
                    log.debug("ssh: %s", riga)

        lettore = threading.Thread(target=leggi, name="accesso-ssh", daemon=True)
        lettore.start()
        inizio, aperto = time.time(), False
        while proc.poll() is None:
            if self._fermo.wait(0.5):
                self._termina(proc)
                break
            ora = time.time()
            if not aperto and ((autenticato[0] and ora - autenticato[0] >= self.CONFERMA_S) or ora - inizio >= 15):
                aperto = True
                dove = self.indirizzo or f"porta {reg['porta']} del server"
                self._segna(True, f"attivo, gestione e stato del ponte su {dove} (tunnel verso {reg['host']})")
        lettore.join(timeout=2)
        with self._lock:
            self._proc = None
        return self.classifica(list(righe)), time.time() - inizio, aperto, list(righe)

    def _ciclo(self) -> None:
        attesa = 0.0           # prima del prossimo giro
        attesa_ssh = 0.0       # cresce se il server SSH non si raggiunge
        inizio_attesa = 0.0    # da quando la registrazione non si legge
        registrato_una_volta = False
        prossima_richiesta = 0.0                 # quando mandare (di nuovo) la richiesta di entrare
        notizia = ("", logging.INFO)             # esito dell'ultima richiesta, ripetuto finche' non cambia
        while not self._fermo.is_set():
            if attesa and self._fermo.wait(attesa):
                break
            try:
                reg = self._registrazione()
                errore = None
            except Exception as e:  # noqa: rete, DNS, certificato, risposta strana
                reg, errore = None, e
            if reg is None:
                # Una lettura via HTTPS ogni minuto (dopo due ore ogni 5): costa nulla e appena un
                # amministratore approva la richiesta il tunnel parte.
                inizio_attesa = inizio_attesa or time.time()
                attesa = self.ATTESA_REGISTRAZIONE[1 if time.time() - inizio_attesa > self.VELOCE_S else 0]
                if errore is not None:
                    self._segna(False, f"la pagina della rete {self.rete} non risponde ({errore}): riprovo tra "
                                       f"{attesa:.0f} s" + ("" if registrato_una_volta else
                                                           " (normale finche' sul server non c'e' la pagina della "
                                                           "rete:  sudo bash rete-meshtastic.sh installa)"),
                                logging.WARNING if registrato_una_volta else logging.INFO)
                    continue
                if time.time() >= prossima_richiesta:
                    messaggio, livello, fra = self._manda_richiesta()
                    prossima_richiesta = time.time() + fra
                    notizia = (messaggio, livello)
                    if self.richiesta == "registrato":
                        attesa = 2.0     # approvato in questo momento: la registrazione c'e' gia'
                self._segna(False, notizia[0], notizia[1])
                continue
            inizio_attesa = 0.0
            prossima_richiesta = 0.0     # se un giorno sparisce (ponte tolto) si richiede subito
            self.richiesta = None
            registrato_una_volta = True
            with self._lock:
                self.indirizzo = reg["indirizzo"]
            try:
                self._scrivi_known_hosts(reg)
            except OSError as e:
                attesa = 300.0
                self._segna(False, f"non riesco a salvare la chiave del server ({e}): riprovo tra 5 minuti",
                            logging.ERROR)
                continue
            esito, durata, aperto, righe = self._tunnel(reg)
            if self._fermo.is_set():
                break
            dettaglio = righe[-1] if righe else "nessun messaggio"
            if esito == "porta":
                attesa = self.ATTESA_PORTA_OCCUPATA_S
                msg = (f"sul server la porta {reg['porta']} e' ancora occupata (il collegamento di prima non e' "
                       f"ancora scaduto): riprovo tra {attesa:.0f} s")
            elif esito == "rifiuto":
                attesa = self.ATTESA_RIFIUTO_S
                msg = (f"il server ha rifiutato la chiave del ponte: riprovo tra {attesa / 60:.0f} minuti. Se dura, "
                       "sul server  sudo bash rete-meshtastic.sh aggiorna")
            elif esito == "chiave_server":
                attesa = self.ATTESA_RIFIUTO_S
                msg = (f"la chiave SSH del server non e' quella pubblicata dalla pagina della rete: non mi collego, "
                       f"riprovo tra {attesa / 60:.0f} minuti ({dettaglio})")
            elif aperto and durata >= self.DURATA_STABILE_S:
                attesa = attesa_ssh = 5.0
                msg = f"tunnel chiuso dopo {durata / 60:.0f} minuti ({dettaglio}): mi ricollego"
            else:
                attesa = attesa_ssh = min(max(attesa_ssh * 2, self.ATTESA_RETE[0]), self.ATTESA_RETE[1])
                msg = f"server SSH {reg['host']} non raggiungibile ({dettaglio}): riprovo tra {attesa:.0f} s"
            self._segna(False, msg, logging.INFO if esito == "rete" and aperto else logging.WARNING)


def leggi_canali(iface, cfg: Config) -> Tuple[Dict[int, Canale], Set[int]]:
    from meshtastic.protobuf import config_pb2
    try:
        preset = config_pb2.Config.LoRaConfig.ModemPreset.Name(
            iface.localNode.localConfig.lora.modem_preset)
    except Exception:  # noqa
        preset = ""
    canali: Dict[int, Canale] = {}
    for ch in (iface.localNode.channels or []):
        if ch.role == 0:  # DISABLED
            continue
        psk = bytes(ch.settings.psk)
        canali[ch.index] = Canale(ch.index, ch.settings.name or nome_preset(preset), len(psk) >= 16)
    modo = cfg.canali.strip().lower()
    if modo == "privati":
        ok = {i for i, c in canali.items() if c.privato}
    elif modo == "tutti":
        ok = set(canali)
    else:
        ok = {int(x) for x in modo.replace(";", ",").split(",") if x.strip()} & set(canali)
    return canali, ok


# --------------------------------------------------------------------------
# Configurazione del nodo Meshtastic (usata dall'installatore)
# --------------------------------------------------------------------------

def _apri_con_attesa(collegamento: str, limite_s: float):
    """Riprova finche' il nodo risponde: dopo un riavvio meshtasticd impiega qualche secondo."""
    fine = time.time() + limite_s
    ultimo = None
    while time.time() < fine:
        try:
            return apri_interfaccia(collegamento)
        except Exception as e:  # noqa
            ultimo = e
            time.sleep(5)
    raise ultimo if ultimo else TimeoutError("nodo non raggiungibile")


def _stato_nodo(iface) -> dict:
    from meshtastic.protobuf import config_pb2
    nodo = iface.localNode
    mio = iface.myInfo.my_node_num if iface.myInfo else 0
    utente = (iface.nodesByNum.get(mio) or {}).get("user", {})
    privati = [c for c in (nodo.channels or []) if c.role != 0 and len(bytes(c.settings.psk)) >= 16]
    return {
        "num": mio,
        "regione": config_pb2.Config.LoRaConfig.RegionCode.Name(nodo.localConfig.lora.region),
        "ruolo": config_pb2.Config.DeviceConfig.Role.Name(nodo.localConfig.device.role),
        "nome": utente.get("longName", ""),
        "nome_breve": utente.get("shortName", ""),
        "privati": [c.settings.name or "?" for c in privati],
        "posizione_fissa": bool(nodo.localConfig.position.fixed_position),
        "canali": [(int(c.role), c.settings.name, bytes(c.settings.psk)) for c in (nodo.channels or [])[:2]],
        "secondari": [nome_canale(nodo.channels[i]) for i in secondari_da_spegnere(nodo)],
        "accesi": [nome_canale(c) for i, c in enumerate(nodo.channels or []) if i > 0 and int(c.role) != 0],
    }


def nome_canale(c) -> str:
    return c.settings.name or ("LongFast" if bytes(c.settings.psk) == b"\x01" else f"n. {c.index}")


def secondari_da_spegnere(nodo) -> List[int]:
    """Canali secondari ancora accesi, da spegnere quando la radio si prepara come all'installazione (prima
    installazione o "Reimposta la radio"; dalla 1.13.0 mai negli aggiornamenti). Tutti, se il primario e' un canale
    privato (la squadra); altrimenti (canale della squadra messo come secondario a mano) solo quelli pubblici, per
    non perdere la squadra."""
    canali = list(nodo.channels or [])
    if not canali:
        return []
    primo = canali[0]
    primario_privato = int(primo.role) == 1 and len(bytes(primo.settings.psk)) >= 16
    return [i for i, c in enumerate(canali) if i > 0 and int(c.role) != 0
            and (primario_privato or len(bytes(c.settings.psk)) < 16)]


def spegni_canale(nodo, indice: int) -> None:
    from meshtastic.protobuf import channel_pb2
    c = nodo.channels[indice]
    prima = channel_pb2.Channel()
    prima.CopyFrom(c)
    c.role = channel_pb2.Channel.Role.DISABLED
    c.ClearField("settings")
    try:
        nodo.writeChannel(indice)
    except Exception:
        c.CopyFrom(prima)        # il nodo non l'ha preso: la copia del ponte resta come il nodo
        raise


# Il nodo mette i messaggi di configurazione in una coda di 4 posti e, se ne arrivano di piu' tutti
# insieme, scarta i piu' vecchi senza dirlo (Router::enqueueReceivedMessage, MAX_RX_FROMRADIO = 4 nel
# firmware 2.7 e 2.8). Ogni modifica parte quindi da sola, con una pausa, e alla fine si controlla.
PAUSA_ADMIN_S = 1.0
TENTATIVI_CONFIGURAZIONE = 3

# Le parti della radio che l'installatore prepara. Dalla 1.13.0 (scelta dell'11/11/2026) le prepara tutte
# solo la prima volta e con "Reimposta la radio": negli aggiornamenti resta quello che si e' scelto dall'app o dalla
# pagina (l'installatore chiede solo le parti nuove, es. il canale della squadra dato con --canale-squadra).
PARTI_NODO = ("regione", "ruolo", "nome", "canale", "secondari", "posizione")


def leggi_parti(testo: str) -> Set[str]:
    """--parti: "tutto", "nessuna" o un elenco separato da virgole (es. nome,secondari)."""
    t = (testo or "").strip().lower()
    if t in ("", "tutto", "tutte"):
        return set(PARTI_NODO)
    if t in ("nessuna", "niente"):
        return set()
    parti = {x.strip() for x in t.split(",") if x.strip()}
    strane = parti - set(PARTI_NODO)
    if strane:
        raise ValueError(f"parti sconosciute: {', '.join(sorted(strane))} (si possono chiedere: {', '.join(PARTI_NODO)})")
    return parti


def _modifiche_da_fare(iface, nome: str, nome_breve: str, canale: str, ruolo: str,
                       posizione: Optional[Tuple[float, float, int]], pausa: Callable[[], None],
                       chiave: Optional[bytes] = None, parti: Optional[Set[str]] = None):
    """(descrizione, azione) per cio' che sul nodo non e' ancora come deve essere, solo nelle 'parti' chieste
    (None = tutte).

    Con 'chiave' il canale della squadra e' dato (es. quello di ICARO): il primario deve avere
    esattamente quel nome e quella chiave. Senza, si crea un canale con chiave nuova solo se non
    ce n'e' gia' uno privato. Con la parte "secondari" i canali secondari (es. il pubblico LongFast) si spengono.
    """
    from meshtastic.protobuf import channel_pb2, config_pb2
    Ruolo = config_pb2.Config.DeviceConfig.Role
    Regione = config_pb2.Config.LoRaConfig.RegionCode
    parti = set(PARTI_NODO) if parti is None else parti
    nodo = iface.localNode
    prima = _stato_nodo(iface)
    da_fare = []
    if "regione" in parti and nodo.localConfig.lora.region != Regione.EU_868:
        def _regione():
            nodo.localConfig.lora.region = Regione.EU_868
            nodo.localConfig.lora.use_preset = True
            nodo.writeConfig("lora")
        da_fare.append(("regione EU 868", _regione))
    if "ruolo" in parti and ruolo and nodo.localConfig.device.role != Ruolo.Value(ruolo):
        def _ruolo():
            nodo.localConfig.device.role = Ruolo.Value(ruolo)
            nodo.writeConfig("device")
        da_fare.append((f"ruolo {ruolo}", _ruolo))
    if "nome" in parti and nome and (prima["nome"] != nome or prima["nome_breve"] != nome_breve):
        da_fare.append((f"nome '{nome}' ({nome_breve})", lambda: nodo.setOwner(nome, nome_breve)))
    if chiave is not None:
        primo = nodo.channels[0]
        if not (primo.role == channel_pb2.Channel.Role.PRIMARY and primo.settings.name == canale
                and bytes(primo.settings.psk) == chiave):
            def _primario():
                primo.role = channel_pb2.Channel.Role.PRIMARY
                primo.settings.name = canale
                primo.settings.psk = chiave
                primo.settings.module_settings.position_precision = 32
                nodo.writeChannel(0)
            da_fare.append((f"canale della squadra '{canale}' con la chiave data", _primario))
    crea_canale = "canale" in parti and chiave is None and not prima["privati"]
    if crea_canale:
        def _canali():
            primo = nodo.channels[0]
            primo.role = channel_pb2.Channel.Role.PRIMARY
            primo.settings.name = canale
            primo.settings.psk = os.urandom(32)
            primo.settings.module_settings.position_precision = 32
            nodo.writeChannel(0)
        da_fare.append((f"canale privato '{canale}' con chiave nuova", _canali))
    if "secondari" in parti:
        # solo il canale della squadra: i secondari si spengono. Se in questo giro il primario diventa la squadra
        # (chiave data, o canale creato adesso) si spengono tutti; altrimenti vale la regola di secondari_da_spegnere
        primo = nodo.channels[0]
        squadra_primaria = chiave is not None or crea_canale or (
            int(primo.role) == 1 and len(bytes(primo.settings.psk)) >= 16)
        for i, c in enumerate(nodo.channels):
            if i > 0 and int(c.role) != 0 and (squadra_primaria or len(bytes(c.settings.psk)) < 16):
                da_fare.append((f"canale secondario '{nome_canale(c)}' spento", lambda i=i: spegni_canale(nodo, i)))
    if posizione and "posizione" in parti:
        lat, lon, alt = posizione
        attuale = (iface.nodesByNum.get(prima["num"]) or {}).get("position") or {}
        uguale = (prima["posizione_fissa"]
                  and attuale.get("latitudeI") == int(float(lat) / 1e-7)
                  and attuale.get("longitudeI") == int(float(lon) / 1e-7)
                  and int(attuale.get("altitude") or 0) == int(alt or 0))
        if not uguale:
            da_fare.append((f"posizione fissa {lat:.5f}, {lon:.5f}",
                            lambda: nodo.setFixedPosition(float(lat), float(lon), int(alt or 0))))
    return da_fare


def configura_nodo(collegamento: str, nome: str, nome_breve: str, canale: str, ruolo: str,
                   posizione: Optional[Tuple[float, float, int]] = None, file_link: Optional[str] = None,
                   attesa_riavvio_s: float = 15.0, pausa_s: float = PAUSA_ADMIN_S,
                   chiave_canale: Optional[bytes] = None, parti: Optional[Set[str]] = None) -> int:
    """Regione EU 868, nome, ruolo, canale privato della squadra, canali secondari spenti e posizione fissa: tutto
    (parti None) o solo le parti chieste. Una radio nuova (regione non impostata) si prepara sempre tutta.

    Non cambia cio' che e' gia' a posto, quindi si puo' rilanciare senza danni: se un
    canale privato esiste gia', resta quello (e la sua chiave). Le modifiche partono una
    alla volta; dopo il riavvio del nodo si ricontrolla e si ripete cio' che manca.
    """
    def pausa():
        time.sleep(pausa_s)

    parti = set(PARTI_NODO) if parti is None else set(parti)
    if chiave_canale is not None:
        parti.add("canale")                   # il canale della squadra dato si mette sempre
    print("Collegamento al nodo Meshtastic (" + descrivi_collegamento(collegamento) + ")...")
    dopo, link = None, ""
    for tentativo in range(1, TENTATIVI_CONFIGURAZIONE + 1):
        try:
            iface = _apri_con_attesa(collegamento, 150)
        except Exception as e:  # noqa
            print("  ERRORE: " + spiega_errore_nodo(e, collegamento))
            print("ESITO configurazione=errore")
            return 1
        cambi = []
        try:
            if tentativo == 1 and parti != set(PARTI_NODO) and _stato_nodo(iface)["regione"] == "UNSET":
                parti = set(PARTI_NODO)
                print("  radio nuova (regione non impostata): la preparo tutta, come alla prima installazione")
            elif tentativo == 1 and not parti:
                print("  impostazioni della radio lasciate come sono")
            da_fare = _modifiche_da_fare(iface, nome, nome_breve, canale, ruolo, posizione, pausa,
                                         chiave_canale, parti)
            if not da_fare or tentativo == TENTATIVI_CONFIGURAZIONE:
                dopo = _stato_nodo(iface)
                link = iface.localNode.getURL(includeAll=True)
            else:
                # Prima si decide cosa cambiare, poi si scrive in una transazione: la conferma finale
                # salva e fa ripartire il nodo. Un messaggio alla volta (vedi PAUSA_ADMIN_S).
                nodo = iface.localNode
                nodo.beginSettingsTransaction()
                pausa()
                for descrizione, azione in da_fare:
                    azione()
                    pausa()
                    cambi.append(descrizione)
                nodo.commitSettingsTransaction()
                pausa()
        except Exception as e:  # noqa
            print(f"  ERRORE durante la configurazione: {type(e).__name__}: {e}")
            print("ESITO configurazione=errore")
            return 1
        finally:
            try:
                iface.close()
            except Exception:  # noqa
                pass
        if dopo is not None:
            if tentativo == 1 and parti:
                print("  era gia' tutto a posto")
            break
        print("  " + ("modifiche: " if tentativo == 1 else "di nuovo, perche' il nodo non le aveva prese: ")
              + ", ".join(cambi))
        time.sleep(attesa_riavvio_s)  # il nodo salva e riparte (meshtasticd: dopo 7 secondi)

    problemi = []
    if "regione" in parti and dopo["regione"] != "EU_868":
        problemi.append(f"regione {dopo['regione']}")
    if "ruolo" in parti and ruolo and dopo["ruolo"] != ruolo:
        problemi.append(f"ruolo {dopo['ruolo']}")
    if "canale" in parti and not dopo["privati"]:
        problemi.append("nessun canale privato")
    if "nome" in parti and nome and dopo["nome"] != nome:
        problemi.append(f"nome '{dopo['nome']}'")
    if "posizione" in parti and posizione and not dopo["posizione_fissa"]:
        problemi.append("posizione fissa non impostata")
    canali = dopo.get("canali") or []
    if chiave_canale is not None and not (canali and canali[0][1] == canale and canali[0][2] == chiave_canale):
        problemi.append(f"canale della squadra '{canale}'")
    if "secondari" in parti and dopo["secondari"]:
        problemi.append("canali secondari ancora accesi: " + ", ".join(dopo["secondari"]))
    print(f"  nodo !{dopo['num']:08x} '{dopo['nome']}' - regione {dopo['regione']}, ruolo {dopo['ruolo']}, "
          f"canale privato: {', '.join(dopo['privati']) or 'nessuno'}, "
          + (f"canali secondari: {', '.join(dopo['accesi'])}" if dopo["accesi"] else "nessun canale secondario"))
    if not dopo["privati"] and "canale" not in parti:
        print("  ATTENZIONE: il nodo non ha piu' il canale privato della squadra: il ponte non porta le radio al TAK "
              "Server. Per rimetterlo: 'Reimposta la radio' sulla pagina della rete (o --reimposta-radio)")
    if file_link and dopo["privati"]:
        try:
            with open(os.open(file_link, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
                f.write(link + "\n")
        except OSError as e:
            print(f"  attenzione: link non salvato in {file_link}: {e}")
    print("LINK " + link)
    if problemi:
        print("  ERRORE: il nodo non ha preso queste impostazioni: " + ", ".join(problemi))
        print("ESITO configurazione=errore")
        return 1
    print("ESITO configurazione=ok")
    return 0


def riavvia_nodo(collegamento: str, attesa_s: float = 60.0) -> int:
    """Riavvia la radio del ponte (dalla 1.13.0, "Riavvia la radio" della pagina della rete). Serve per la radio USB:
    meshtasticd si riavvia con systemctl. La lancia mesh2tak-aggiorna, da root, con il ponte fermo: il nodo accetta un
    solo collegamento alla volta."""
    print("Collegamento al nodo Meshtastic (" + descrivi_collegamento(collegamento) + ")...")
    try:
        iface = _apri_con_attesa(collegamento, attesa_s)
    except Exception as e:  # noqa
        print("  ERRORE: " + spiega_errore_nodo(e, collegamento))
        print("ESITO riavvio=errore")
        return 1
    try:
        iface.localNode.reboot(3)
        time.sleep(1.0)                       # il messaggio parte prima di chiudere
    except Exception as e:  # noqa
        print(f"  ERRORE: {type(e).__name__}: {e}")
        print("ESITO riavvio=errore")
        return 1
    finally:
        try:
            iface.close()
        except Exception:  # noqa
            pass
    print("  la radio si riavvia tra 3 secondi")
    print("ESITO riavvio=ok")
    return 0


def leggi_chiave_canale(testo: str) -> bytes:
    """Chiave di un canale privato come la mostrano le app: base64 (anche con 'base64:' davanti), 16 o 32 byte."""
    import base64
    import binascii
    t = testo.strip()
    if t.lower().startswith("base64:"):
        t = t[7:].strip()
    try:
        chiave = base64.b64decode(t, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("non e' in base64")
    if len(chiave) not in (16, 32):
        raise ValueError(f"deve essere di 16 o 32 byte, qui sono {len(chiave)}")
    return chiave


def _leggi_posizione(testo: str) -> Tuple[float, float, int]:
    parti = [x for x in testo.replace(";", ",").replace(" ", ",").split(",") if x.strip()]
    if len(parti) not in (2, 3):
        raise ValueError("servono latitudine e longitudine, es. 45.8205,8.8257")
    lat, lon = float(parti[0]), float(parti[1])
    alt = int(float(parti[2])) if len(parti) == 3 else 0
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        raise ValueError("coordinate fuori scala")
    return lat, lon, alt


# --------------------------------------------------------------------------
# Come sta il Raspberry (per la pagina della rete e gli avvisi)
# --------------------------------------------------------------------------

class Sistema:
    """Accensione, alimentazione, spazio sulla microSD, protezioni messe dall'installatore e data della
    microSD di scorta. Solo letture di file: niente comandi, niente privilegi."""

    PROTEZIONI = {
        "watchdog": "/etc/systemd/system.conf.d/mesh2tak-watchdog.conf",
        "aggiornamenti": "/etc/apt/apt.conf.d/52mesh2tak-aggiornamenti",
        "registri": "/etc/systemd/journald.conf.d/mesh2tak.conf",
    }
    SCORTA = "/etc/mesh2tak/scorta.json"
    AGGIORNAMENTO = "/var/lib/mesh2tak-aggiorna/stato.json"           # lo scrive mesh2tak-aggiorna (root)
    TIMER_AGGIORNA = "/etc/systemd/system/mesh2tak-aggiorna.timer"
    # dalla 1.13.0: esiti dei comandi della pagina della rete e registro per la pagina (li scrive mesh2tak-aggiorna);
    # l'unita' .path dice che il Raspberry li fa (installatore 1.13.0 o successivo)
    AZIONI = "/var/lib/mesh2tak-aggiorna/azioni.json"
    REGISTRO = "/var/lib/mesh2tak-aggiorna/registro.txt"
    SVEGLIA_AGGIORNA = "/etc/systemd/system/mesh2tak-aggiorna.path"
    NOMI_AZIONI = ("riavvia-programma", "riavvia-radio", "riavvia-raspberry", "registro", "impostazioni", "certificato",
                   "reimposta-radio")
    STATI_AZIONI = ("in_corso", "fatto", "errore", "scaduto")
    AVVISO_LOG_S = 600.0               # nel registro al massimo una riga ogni 10 minuti sulla corrente

    def __init__(self, radice: str = ""):
        self.radice = radice           # solo per le prove: una cartella al posto di /
        self._lock = threading.Lock()
        self.inizio = time.time()
        self.bassa_ora = False
        self.bassa_ultima: Optional[float] = None
        self.bassa_volte = 0
        self._log_bassa = -1e18
        self.allarme = self._trova_allarme()

    def _p(self, percorso: str) -> str:
        return self.radice + percorso if self.radice else percorso

    def _leggi(self, percorso: str, massimo: int = 4096) -> Optional[str]:
        try:
            with open(self._p(percorso), encoding="utf-8", errors="replace") as f:
                return f.read(massimo)
        except OSError:
            return None

    def _trova_allarme(self) -> Optional[str]:
        """Il segnale di corrente insufficiente del Raspberry (driver rpi_volt del kernel), se c'e'."""
        base = self._p("/sys/class/hwmon")
        try:
            cartelle = sorted(os.listdir(base))
        except OSError:
            return None
        for d in cartelle:
            try:
                with open(os.path.join(base, d, "name"), encoding="ascii", errors="replace") as f:
                    nome = f.read(64).strip()
            except OSError:
                continue
            allarme = os.path.join(base, d, "in0_lcrit_alarm")
            if nome == "rpi_volt" and os.path.exists(allarme):
                return allarme
        return None

    def controlla_alimentazione(self, ora: Optional[float] = None) -> None:
        """Il kernel chiede al Raspberry ogni 2 secondi se la corrente e' scesa sotto il minimo, e a ogni
        controllo azzera il segnale: guardando ogni secondo non si perde nessun calo, anche breve."""
        if not self.allarme:
            return
        try:
            with open(self.allarme, encoding="ascii") as f:
                bassa = f.read(8).strip() == "1"
        except (OSError, UnicodeDecodeError):
            return
        ora = time.time() if ora is None else ora
        with self._lock:
            if bassa:
                if not self.bassa_ora:
                    self.bassa_volte += 1
                    if ora - self._log_bassa >= self.AVVISO_LOG_S:
                        self._log_bassa = ora
                        log.warning("Alimentazione insufficiente: la corrente non basta al Raspberry "
                                    "(alimentatore o cavo deboli: serve quello da 5,1 V e 2,5 A)")
                self.bassa_ultima = ora
            self.bassa_ora = bassa

    def ciclo(self, fermo: threading.Event) -> None:
        while not fermo.wait(1.0):
            self.controlla_alimentazione()

    def stato(self) -> dict:
        ora = time.time()
        avvio: Optional[str] = (self._leggi("/proc/sys/kernel/random/boot_id", 64) or "").strip().lower()
        if not re.match(r"^[0-9a-f-]{32,36}$", avvio or ""):
            avvio = None                        # cambia a ogni accensione: cosi' la rete vede i riavvii
        acceso_dal = None
        campi = (self._leggi("/proc/uptime", 64) or "").split()
        if campi:
            try:
                acceso_dal = round(ora - float(campi[0]))
            except ValueError:
                pass
        disco = None
        try:
            s = os.statvfs(self._p("/"))
            disco = {"libero_mb": s.f_bavail * s.f_frsize // 1048576, "totale_mb": s.f_blocks * s.f_frsize // 1048576}
        except OSError:
            pass
        protezioni = {k: os.path.exists(self._p(v)) for k, v in self.PROTEZIONI.items()}
        stato_wd = (self._leggi("/sys/class/watchdog/watchdog0/state", 32) or "").strip()
        if stato_wd in ("active", "inactive"):
            protezioni["watchdog"] = stato_wd == "active"     # quello che fa davvero il chip
        scorta = None
        try:
            d = json.loads(self._leggi(self.SCORTA) or "null")
        except ValueError:
            d = None
        if isinstance(d, dict) and isinstance(d.get("quando"), (int, float)) and not isinstance(d["quando"], bool):
            scorta = {"quando": round(d["quando"]), "disco": pulisci(d.get("disco"), 80)}
        aggiornamento = None
        try:
            a = json.loads(self._leggi(self.AGGIORNAMENTO) or "null")
        except ValueError:
            a = None
        if isinstance(a, dict) and a.get("stato") in ("in_corso", "fatto", "errore", "ripristinato"):
            aggiornamento = {"stato": a["stato"], "versione": pulisci(a.get("versione"), 20),
                             "da": pulisci(a.get("da"), 20), "messaggio": pulisci(a.get("messaggio"), 400),
                             "quando": round(a["quando"]) if isinstance(a.get("quando"), (int, float)) else None}
        with self._lock:
            alimentazione = None if not self.allarme else {
                "bassa_ora": self.bassa_ora, "ultima": round(self.bassa_ultima) if self.bassa_ultima else None,
                "volte": self.bassa_volte, "dal": round(self.inizio)}
        registro = None
        try:
            st = os.stat(self._p(self.REGISTRO))
            registro = {"quando": round(st.st_mtime), "byte": st.st_size}
        except OSError:
            pass
        return {"avvio": avvio, "acceso_dal": acceso_dal, "alimentazione": alimentazione, "disco": disco,
                "protezioni": protezioni, "scorta": scorta, "aggiornabile": os.path.exists(self._p(self.TIMER_AGGIORNA)),
                "aggiornamento": aggiornamento, "comandi": os.path.exists(self._p(self.SVEGLIA_AGGIORNA)),
                "azioni": self._azioni(), "registro": registro}

    def _azioni(self) -> List[dict]:
        """Gli ultimi comandi della pagina della rete e com'e' andata (dalla 1.13.0)."""
        try:
            d = json.loads(self._leggi(self.AZIONI, 1 << 20) or "null")
        except ValueError:
            d = None
        elenco = d.get("azioni") if isinstance(d, dict) else None
        risultato = []
        for a in (elenco if isinstance(elenco, list) else [])[-12:]:
            if (not isinstance(a, dict) or not re.match(r"^[0-9a-f]{16}$", str(a.get("id", "")))
                    or a.get("azione") not in self.NOMI_AZIONI or a.get("stato") not in self.STATI_AZIONI):
                continue
            numero = (lambda x: round(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None)
            risultato.append({"id": a["id"], "azione": a["azione"], "stato": a["stato"],
                              "messaggio": pulisci(a.get("messaggio"), 400), "chiesto": numero(a.get("chiesto")),
                              "quando": numero(a.get("quando"))})
        return risultato


_CERTIFICATI: Dict[str, Tuple[float, Optional[dict]]] = {}


def certificato_tak(percorso: str) -> Optional[dict]:
    """Nome e scadenza del certificato del ponte: alla scadenza il TAK Server lo rifiuta."""
    try:
        mtime = os.path.getmtime(percorso)
    except OSError:
        return None
    gia = _CERTIFICATI.get(percorso)
    if gia and gia[0] == mtime:
        return gia[1]
    dati: Optional[dict] = None
    try:
        d = ssl._ssl._test_decode_cert(percorso)  # noqa: SLF001  (legge il primo certificato del file)
        nome = ""
        for rdn in d.get("subject", ()):
            for k, v in rdn:
                if k == "commonName":
                    nome = v
        dati = {"scade": int(ssl.cert_time_to_seconds(d["notAfter"])), "nome": pulisci(nome, 64)}
    except Exception:  # noqa  (Python senza il lettore interno: lo chiede a openssl)
        try:
            r = subprocess.run(["openssl", "x509", "-noout", "-enddate", "-in", percorso],
                               capture_output=True, text=True, timeout=10)
            fine = r.stdout.strip().split("=", 1)[1] if r.returncode == 0 and "=" in r.stdout else ""
            dati = {"scade": int(ssl.cert_time_to_seconds(fine)), "nome": ""} if fine else None
        except Exception:  # noqa
            dati = None
    _CERTIFICATI[percorso] = (mtime, dati)
    return dati


# --------------------------------------------------------------------------
# Avvio
# --------------------------------------------------------------------------

def stato_per_la_rete(cfg: Config, ponte: Ponte, tak: ClienteTAK, gestione: Gestione,
                      accesso: Optional[AccessoEsterno], avviato: float,
                      sistema: Optional[Sistema] = None) -> dict:
    """Quello che la pagina della rete legge da /mesh2tak/stato.json."""
    s = ponte.istantanea()
    s.update({
        "formato": 1,
        "versione": VERSIONE,
        "ora": round(time.time()),
        "avviato": round(avviato),
        "tak": {"collegato": tak.collegato.is_set(), "server": f"{cfg.tak_host}:{cfg.tak_porta}",
                "dal": round(tak.collegato_dal) if tak.collegato_dal else None, "errore": tak.ultimo_errore},
        "pausa": gestione.stato_pausa(),
        "gestione": {"pagina": bool(gestione.nodo_pagina), "app": bool(cfg.gestione_porta_app),
                     "radio": gestione.radio is not None,
                     "pausa_massima_min": cfg.gestione_pausa_massima_min, "chiusura_s": cfg.gestione_chiusura_s},
        "accesso": accesso.stato() if accesso is not None else None,
        "sistema": sistema.stato() if sistema is not None else None,
        "certificato": certificato_tak(cfg.certificato),
        "icone": ponte.icone.stato(),
        # dalla 1.13.0: quelle che si cambiano dalla pagina della rete ("Gestisci il Raspberry")
        "impostazioni": {"squadra": cfg.squadra, "validita_min": cfg.validita_min,
                         "aggiornamento_min": cfg.aggiornamento_min, "errore_massimo_m": cfg.errore_massimo_m,
                         "includi_ponte": cfg.includi_ponte, "invio": cfg.invio_radio,
                         "tempo_radio_percento": cfg.tempo_radio_percento,
                         "pausa_massima_min": cfg.gestione_pausa_massima_min},
    })
    return s


def prova(cfg: Config) -> int:
    """Controllo usato dall'installatore: nodo, canali, TAK Server."""
    esito = 0
    print("Controllo il nodo Meshtastic (" + descrivi_collegamento(cfg.collegamento) + ")...")
    try:
        iface = apri_interfaccia(cfg)
    except Exception as e:  # noqa
        print("  ERRORE: " + spiega_errore_nodo(e, cfg))
        print("ESITO nodo=errore")
        esito |= 1
    else:
        try:
            canali, ok = leggi_canali(iface, cfg)
            mio = iface.myInfo.my_node_num if iface.myInfo else 0
            u = (iface.nodesByNum.get(mio) or {}).get("user", {})
            print(f"  nodo '{u.get('longName', '?')}' (!{mio:08x}) collegato")
            for c in canali.values():
                print(f"  canale {c.indice} '{c.nome}': {'privato' if c.privato else 'pubblico'}"
                      f" -> {'inoltrato' if c.indice in ok else 'ignorato'}")
            ora = time.time()
            recenti = 0
            for num, info in iface.nodesByNum.items():
                pos = info.get("position") or {}
                if num == mio or not (pos.get("latitudeI") or pos.get("longitudeI")):
                    continue
                if (info.get("channel", 0) in ok
                        and ora - float(info.get("lastHeard") or 0) <= cfg.validita_min * 60):
                    recenti += 1
            privati = sum(1 for c in canali.values() if c.privato)
            print(f"  radio con posizione recente sui canali inoltrati: {recenti}")
            print(f"ESITO nodo=ok privati={privati} inoltrati={len(ok)} recenti={recenti}")
        finally:
            try:
                iface.close()
            except Exception:  # noqa
                pass
    print(f"Controllo il TAK Server {cfg.tak_host}:{cfg.tak_porta}...")
    ok_tak, messaggio = prova_tak(cfg)
    print(("  " if ok_tak else "  ERRORE: ") + messaggio)
    print("ESITO tak=" + ("ok" if ok_tak else "errore"))
    if not ok_tak:
        esito |= 2
    if not verifica_librerie():
        esito |= 4
    return esito


def verifica_librerie() -> bool:
    """La libreria TAK di Meshtastic si carica accanto a quella della radio e converte davvero."""
    print("Controllo la libreria TAK di Meshtastic (chat, segnaposti, forme)...")
    try:
        import meshtastic  # noqa: F401  (va caricata prima, come nel servizio)
        sdk = LibreriaTak()
        sdk.verifica()
    except Exception as e:  # noqa
        print(f"  ERRORE: {type(e).__name__}: {e}")
        print("ESITO librerie=errore")
        return False
    print(f"  meshtastic-tak {sdk.versione}: conversione di prova riuscita")
    print("ESITO librerie=ok")
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Ponte Meshtastic <-> TAK Server")
    ap.add_argument("-c", "--config", default=CONFIG_PREDEFINITA, help="file di configurazione")
    ap.add_argument("--prova", action="store_true", help="controlla nodo, TAK Server e librerie, poi esce")
    ap.add_argument("--verifica-librerie", action="store_true", help="controlla solo le librerie, poi esce")
    ap.add_argument("--debug", action="store_true", help="registro dettagliato")
    ap.add_argument("--versione", action="version", version=f"mesh2tak {VERSIONE}")
    ap.add_argument("--codice-controllo", metavar="CHIAVE.pub",
                    help="mostra il codice di controllo di una chiave del ponte (lo usa l'installatore)")
    nodo = ap.add_argument_group("configurazione del nodo (usata dall'installatore)")
    nodo.add_argument("--configura-nodo", metavar="COLLEGAMENTO",
                      help="imposta regione EU 868, nome, ruolo, canale privato; es. tcp:127.0.0.1:4403")
    nodo.add_argument("--nome", default="Gateway CNSAS Varese")
    nodo.add_argument("--nome-breve", default="GTSA")
    nodo.add_argument("--canale", default="CNSAS")
    nodo.add_argument("--ruolo", default="CLIENT")
    nodo.add_argument("--posizione", default="", help="lat,lon[,quota] della posizione fissa")
    nodo.add_argument("--file-link", default="", help="dove salvare il link del canale")
    nodo.add_argument("--chiave-canale-file", default="",
                      help="file con la chiave (base64) del canale della squadra: il primario prende --canale e questa chiave")
    nodo.add_argument("--parti", default="tutto",
                      help="cosa sistemare sulla radio: tutto, nessuna o un elenco tra " + ", ".join(PARTI_NODO)
                           + " (dalla 1.13.0 gli aggiornamenti non cambiano la radio)")
    nodo.add_argument("--riavvia-nodo", metavar="COLLEGAMENTO",
                      help="riavvia la radio (quella USB: meshtasticd si riavvia con systemctl), poi esce")
    args = ap.parse_args(argv)

    if args.codice_controllo:
        try:
            with open(args.codice_controllo, encoding="utf-8") as f:
                print(codice_controllo(impronta_chiave(f.read())))
            return 0
        except (OSError, ValueError) as e:
            print(f"ERRORE: {e}", file=sys.stderr)
            return 1

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stdout)
    # La libreria meshtastic scrive messaggi tecnici (anche "reconnecting" quando non si ricollega):
    # lo stato del collegamento lo racconta gia' questo programma.
    logging.getLogger("meshtastic").setLevel(logging.DEBUG if args.debug else logging.CRITICAL)

    if args.verifica_librerie:
        return 0 if verifica_librerie() else 1

    if args.riavvia_nodo:
        return riavvia_nodo(args.riavvia_nodo)

    if args.configura_nodo:
        if len(args.canale.encode("utf-8")) > 11:
            print("ERRORE: il nome del canale puo' avere al massimo 11 caratteri")
            return 2
        try:
            parti = leggi_parti(args.parti)
        except ValueError as e:
            print(f"ERRORE: {e}")
            print("ESITO configurazione=errore")
            return 2
        posizione = None
        if args.posizione.strip():
            try:
                posizione = _leggi_posizione(args.posizione)
            except ValueError as e:
                print(f"ERRORE: posizione non valida ({e})")
                return 2
        chiave = None
        if args.chiave_canale_file:
            try:
                chiave = leggi_chiave_canale(open(args.chiave_canale_file, encoding="utf-8").read())
            except (OSError, ValueError) as e:
                print(f"ERRORE: chiave del canale non valida ({e})")
                print("ESITO configurazione=errore")
                return 2
        return configura_nodo(args.configura_nodo, args.nome.strip(), args.nome_breve.strip()[:4],
                              args.canale.strip(), args.ruolo.strip().upper(), posizione,
                              args.file_link or None, chiave_canale=chiave, parti=parti)

    try:
        cfg = carica_config(args.config)
    except ErroreConfig as e:
        log.error("Configurazione: %s", e)
        return 78  # EX_CONFIG: systemd non insiste a riavviare un servizio mal configurato

    if args.prova:
        return prova(cfg)

    try:
        from pubsub import pub
    except ImportError as e:
        log.error("Libreria mancante (%s): rifai l'installazione", e)
        return 1

    try:
        import meshtastic  # noqa: F401  (prima la libreria della radio, poi quella TAK)
        sdk: Optional[LibreriaTak] = LibreriaTak()
    except Exception as e:  # noqa
        sdk = None
        log.error("Libreria TAK di Meshtastic non disponibile (%s): passano solo le posizioni delle radio, "
                  "niente ATAK/TAK Aware, chat e oggetti. Rifai l'installazione", e)

    try:
        tak = ClienteTAK(cfg)
    except (ssl.SSLError, OSError) as e:
        log.error("Certificato o chiave del ponte non leggibili (%s): rifai l'installazione", e)
        return 78
    ponte = Ponte(cfg, tak, sdk)
    ponte.icone = IconeMappa(os.path.join(cfg.accesso_cartella_stato, "icone.json"))
    ponte.copertura = Copertura(os.path.join(cfg.accesso_cartella_stato, "copertura.json"))
    ponte.squadra = RadioSquadra(os.path.join(cfg.accesso_cartella_stato, "radio-squadra.json"))
    tak.su_evento = ponte.su_evento_tak
    if os.environ.get("MESH2TAK_ATTESE"):  # solo per le prove automatiche: attese piu' brevi
        f = float(os.environ["MESH2TAK_ATTESE"])
        for nome in ("ATTESA_VERSO_SERVER", "ATTESA_VERSO_RADIO", "ATTESA_VERSO_RADIO_ALLARME"):
            setattr(ponte, nome, tuple(x * f for x in getattr(Ponte, nome)))
        ponte.SPAZIO_TRA_INVII_S = Ponte.SPAZIO_TRA_INVII_S * f
    pub.subscribe(ponte.su_pacchetto, "meshtastic.receive")
    pub.subscribe(ponte.su_perdita, "meshtastic.connection.lost")

    fermo = threading.Event()

    def _ferma(_sig, _frame):
        fermo.set()
        ponte.persa.set()

    signal.signal(signal.SIGTERM, _ferma)
    signal.signal(signal.SIGINT, _ferma)

    log.info("mesh2tak %s avviato: TAK Server %s:%d, %s, canali '%s'", VERSIONE, cfg.tak_host,
             cfg.tak_porta, descrivi_collegamento(cfg.collegamento), cfg.canali)
    log.info("Posizioni: inoltrate appena arrivano, ripetute al TAK Server ogni %g minuti finche' valide "
             "(%g minuti dall'ultimo segnale); riepilogo ogni ora", cfg.aggiornamento_min, cfg.validita_min)
    if sdk is not None:
        log.info("Libreria TAK meshtastic-tak %s; dal TAK Server alle radio: %s", sdk.versione,
                 f"chat e oggetti, al massimo {cfg.tempo_radio_percento:g}% del tempo radio"
                 if cfg.invio_radio else "disattivato (invio = no)")
    tak.avvia()
    ponte.avvia_uscite()
    pausa = Pausa()
    gestione: Optional[Gestione] = None
    if cfg.gestione_attiva:
        try:
            gestione = Gestione(cfg, pausa, ponte.tz)
            gestione.radio = GestioneRadio(ponte)
            gestione.icone = ponte.imposta_icone
            gestione.copertura = ponte.copertura_per_la_pagina
            gestione.avvia()
            if gestione.nodo_pagina:
                dove = [f"pagina nel browser sulla porta {cfg.gestione_porta_pagina}"] if cfg.gestione_porta_pagina else []
                if cfg.gestione_porta_app:
                    dove.append(f"app Meshtastic via Wi-Fi sulla porta {cfg.gestione_porta_app}")
                log.info("Gestione del nodo: %s. Mentre la si usa il ponte va in pausa da solo", ", ".join(dove))
            else:
                log.info("Stato del ponte per la pagina della rete sulla porta %d (radio USB: la radio si gestisce "
                         "dall'app Meshtastic)", cfg.gestione_porta_pagina)
        except Exception as e:  # noqa
            log.error("Gestione del nodo non avviata (%s): il ponte lavora lo stesso", e)
            gestione = None

    accesso: Optional[AccessoEsterno] = None
    if cfg.accesso_errore:
        log.error("Accesso da internet spento: %s. Il ponte lavora lo stesso", cfg.accesso_errore)
    elif cfg.accesso_attivo:
        if gestione is None or not cfg.gestione_porta_pagina:
            log.error("Accesso da internet spento: la pagina del ponte non e' partita")
        else:
            try:
                accesso = AccessoEsterno(cfg, cfg.gestione_porta_pagina)
                accesso.avvia()
                log.info("Accesso da internet: ponte '%s' sulla pagina della rete %s", cfg.accesso_nome,
                         cfg.accesso_rete)
            except Exception as e:  # noqa
                log.error("Accesso da internet non avviato (%s): il ponte lavora lo stesso", e)
                accesso = None
    avviato = time.time()
    sistema = Sistema()
    threading.Thread(target=sistema.ciclo, args=(fermo,), name="sistema", daemon=True).start()
    if gestione is not None:
        gestione.fornisci_stato = lambda: stato_per_la_rete(cfg, ponte, tak, gestione, accesso, avviato, sistema)

    def aspetta(secondi: float) -> bool:
        """Attesa prima di riprovare, interrotta da una pausa; True se il ponte si deve fermare."""
        fine = time.time() + secondi
        while not fermo.is_set() and not pausa.attiva:
            resta = fine - time.time()
            if resta <= 0:
                break
            fermo.wait(min(resta, 1.0))
        return fermo.is_set()

    attesa = 0
    while not fermo.is_set():
        if pausa.attiva:
            pausa.rilasciato.set()   # il nodo e' libero: la pagina puo' usarlo
            while pausa.attiva and not fermo.is_set():
                fermo.wait(0.5)
            ponte.nota_ponte = None
            attesa = 0
            continue
        if attesa and aspetta(attesa):
            break
        if pausa.attiva:
            continue
        try:
            iface = apri_interfaccia(cfg)
        except Exception as e:  # noqa
            attesa = min(max(attesa * 2, 10), 120)
            log.error("Nodo Meshtastic: %s. Riprovo tra %d s", spiega_errore_nodo(e, cfg), attesa)
            continue
        if pausa.attiva:   # chiesta mentre ci si collegava
            try:
                iface.close()
            except Exception:  # noqa
                pass
            continue
        inizio = time.time()
        intervallo = min(15.0, cfg.aggiornamento_min * 30)
        try:
            ponte.collega(iface)
            prossima = time.time() + intervallo
            while not fermo.is_set() and not pausa.attiva and not ponte.persa.wait(timeout=0.5):
                if time.time() >= prossima:
                    ponte.manutenzione()
                    prossima = time.time() + intervallo
            if pausa.attiva and not fermo.is_set():
                ponte.annuncia_pausa(pausa.nota)
        finally:
            ponte.scollega()
            try:
                iface.close()
            except Exception:  # noqa
                pass
        if fermo.is_set():
            break
        if pausa.attiva:
            continue   # nessuna attesa: alla fine della pausa il ponte si ricollega subito
        durata = time.time() - inizio
        attesa = cfg.riconnessione_nodo_s if durata > 300 else min(max(attesa * 2, cfg.riconnessione_nodo_s), 900)
        if cfg.collegamento.startswith("serial"):
            log.warning("La radio USB si e' scollegata dopo %d s: riprovo tra %d s", durata, attesa)
        else:
            log.warning("Il nodo Meshtastic ha chiuso il collegamento dopo %d s. Succede anche quando un'altra "
                        "app si collega al Raspberry via rete: riprovo tra %d s", durata, attesa)
    log.info("Arresto in corso")
    if accesso is not None:
        accesso.ferma()
    if gestione is not None:
        gestione.ferma()
    ponte.ferma_uscite()
    ponte.copertura.salva(subito=True)
    ponte.squadra.salva(subito=True)
    tak.ferma()
    return 0


if __name__ == "__main__":
    sys.exit(main())
