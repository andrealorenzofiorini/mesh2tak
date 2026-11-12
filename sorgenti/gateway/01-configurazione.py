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

VERSIONE = "1.16.1"
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
                               "(vuoto con la radio USB: solo lo stato del gateway)")
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
            errore = f"chiave del gateway non trovata ({cfg.accesso_chiave}): rilancia l'installatore con --accesso-esterno"
        elif not (cfg.gestione_attiva and cfg.gestione_porta_pagina):
            errore = "serve la pagina del gateway: in [gestione] attiva = si e porta_pagina = 9443"
        if errore:
            cfg.accesso_attivo, cfg.accesso_errore = False, errore
    return cfg


