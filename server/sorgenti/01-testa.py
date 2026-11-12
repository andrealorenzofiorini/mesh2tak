#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rete Meshtastic sul server infra-TAK (parte server di mesh2tak).

Pagina https://meshtastic.<dominio> con lo stato di tutti i ponti mesh2tak e, per ognuno, la gestione
del suo nodo a https://meshtastic-<nome>.<dominio>, dietro Authentik (solo il gruppo "authentik Admins").
I ponti arrivano con un tunnel SSH aperto da loro: sul server ogni ponte ha un utente che puo' solo
tenere aperta la sua porta su 127.0.0.1 (non raggiungibile da fuori), niente comandi. Non installa
programmi: usa sshd, Caddy e Authentik gia' presenti con infra-TAK.

Dalla pagina della rete si aggiungono e tolgono i ponti: i Raspberry mandano da soli la richiesta, un
amministratore la approva. Un piccolo servizio senza privilegi (mesh2tak-rete-web) risponde alla pagina e
mette i lavori in coda; li esegue da root un secondo passo (mesh2tak-rete-lavori) che accetta solo
"aggiungi ponte" e "togli ponte". Dalla 1.7.0 lo stesso servizio controlla i ponti ogni minuto e avvisa per
email con la posta di infra-TAK, agli stessi destinatari degli avvisi di Guard Dog.

Comandi (da root):
  installa                 una volta sola: pagina della rete, Caddy, Authentik, sshd e servizio della pagina
  aggiungi NOME 'CHIAVE'   registra o aggiorna un ponte a mano (di solito si fa dalla pagina)
  elenco                   ponti registrati e tunnel collegati
  rimuovi NOME             toglie un ponte
  aggiorna                 come installa, dopo aver aggiornato lo script
  disinstalla              toglie tutto quello che ha messo questo script
  (servi e lavora li usano i servizi di sistema)
"""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import datetime
import fcntl
import glob
import grp
import hashlib
import http.server
import io
import ipaddress
import json
import os
import pwd
import re
import secrets
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

VERSIONE = "@@VERSIONE@@"

# Solo per le prove automatiche: tutti i file sotto questa cartella, Authentik a un altro indirizzo.
RADICE = os.environ.get("RETE_PROVA_RADICE", "").rstrip("/")
AK_UPSTREAM = os.environ.get("RETE_PROVA_AUTHENTIK", "127.0.0.1:9090")   # come infra-TAK con Authentik locale
AK_URL = "http://" + AK_UPSTREAM


def P(percorso: str) -> str:
    return RADICE + percorso if RADICE else percorso


CADDYFILE = "/etc/caddy/Caddyfile"
CADDY_DIR = "/etc/caddy/mesh2tak-rete"
CADDY_FILE = CADDY_DIR + "/rete.caddy"
MARCATORE_INFRATAK = "# --- User-added blocks (do not remove) ---"
INIZIO_BLOCCO = "# >>> mesh2tak: rete Meshtastic (rete-meshtastic.sh) - non modificare queste tre righe"
FINE_BLOCCO = "# <<< mesh2tak: rete Meshtastic"
CONF_DIR = "/etc/mesh2tak-rete"
REGISTRO = CONF_DIR + "/registro.json"
CHIAVI_DIR = CONF_DIR + "/chiavi"
WEB_DIR = "/var/lib/mesh2tak-rete"
SITO_DIR = WEB_DIR + "/sito"
ACCESSO_DIR = WEB_DIR + "/accesso"
SERVIZIO_DIR = WEB_DIR + "/servizio"     # del servizio della pagina (utente mesh2tak-rete), una sola cartella
RICHIESTE_DIR = SERVIZIO_DIR + "/richieste"   # richieste dei Raspberry (anche quelle rifiutate)
LAVORI_DIR = SERVIZIO_DIR + "/lavori"    # coda dei lavori: la scrive il servizio, la svuota root
TMP_DIR = SERVIZIO_DIR + "/tmp"          # i lavori si scrivono qui e poi si spostano in coda, gia' completi
ESITI_DIR = WEB_DIR + "/esiti"           # esiti dei lavori: li scrive root, li legge il servizio
SORVEGLIANZA = WEB_DIR + "/sorveglianza.json"   # per gli avvisi: porte dei tunnel e porta della console (da root)
STATO_AVVISI = SERVIZIO_DIR + "/avvisi.json"    # problemi in corso e avvisi da mandare (del servizio)
AVVISI_PROVATI = CONF_DIR + "/avvisi-provati"   # la prima email di prova di installa e' gia' partita
RILASCI = SERVIZIO_DIR + "/rilasci.json"        # ultima versione pubblicata su GitHub (del servizio)
REPO = "andrealorenzofiorini/mesh2tak"          # pubblico: le release si leggono e scaricano senza token
GITHUB_API = os.environ.get("RETE_PROVA_GITHUB_API", "https://api.github.com").rstrip("/")
GITHUB = os.environ.get("RETE_PROVA_GITHUB", "https://github.com").rstrip("/")
RE_VERSIONE_RILASCIO = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$")
# Ponti nuovi (dalla 1.10.0): un Raspberry scritto con Raspberry Pi Imager si installa da solo
PONTI_NUOVI_DIR = CONF_DIR + "/ponti-nuovi"                 # solo root: certificato e canale da consegnare
IMPOSTAZIONI_NUOVI = PONTI_NUOVI_DIR + "/impostazioni.json"
CONFIGURAZIONI_DIR = CONF_DIR + "/configurazioni"            # una per ponte da installare, la legge solo lui
RIASSUNTO_NUOVI = WEB_DIR + "/ponti-nuovi.json"              # per la pagina: cosa c'e', senza segreti
AVANZAMENTO_DIR = WEB_DIR + "/avanzamento"                   # lo scrive ogni ponte che si installa (via SSH)
INVITI_DIR = SERVIZIO_DIR + "/inviti"                        # inviti preparati sulla pagina (del servizio)
INSTALLAZIONI_DIR = SERVIZIO_DIR + "/installazioni"          # scheda e posizione scelte, per "Riprova"
SCHEDE = ("usb", "waveshare", "meshadv", "meshadv-mini", "rak")
# Icone sulla mappa (dalla 1.11.0): scelte dalla pagina per ogni radio e ogni ponte, consegnate ai ponti dal tunnel
ICONE_SCELTE = SERVIZIO_DIR + "/icone.json"                  # chi ha quale icona (del servizio)
ICONE_SITO = SITO_DIR + "/icone"                             # le figure per la pagina, prese da infra-TAK (root)
ICONE_ELENCO = SITO_DIR + "/icone-mappa.json"                # l'elenco per la pagina (root)
ICONE_MAX = 500
# L'insieme di icone OSM che ATAK ha di serie e che TAK Aware porta con se' (lo stesso del traliccio dei ponti
# dalla 1.3.2). Chi non lo ha (es. CloudTAK) mostra il simbolo militare del tipo accanto a ogni icona.
ICONSET_OSM = "6d781afb-89a6-4c07-b2b9-a89748b6a38f"
ICONE_MAPPA = (
    # chiave, nome sulla pagina, gruppo, figura nell'insieme OSM, tipo CoT
    ("soccorritore", "Soccorritore", "Persone", "General/people.png", "a-f-G-U-C-I"),
    ("squadra", "Squadra a piedi", "Persone", "Misc/map_hiking.png", "a-f-G-U-C-I"),
    ("sanitario", "Sanitario", "Persone", "General/health.png", "a-f-G-U-S-M"),
    ("cinofilo", "Unità cinofila", "Persone", "Leisure/dog.png", "a-f-G-U-C-I"),
    ("sci", "Sci", "Persone", "Sport/skiing.png", "a-f-G-U-C-I"),
    ("bici", "Bici", "Persone", "Sport/mountain_bike.png", "a-f-G-U-C-I"),
    ("auto", "Auto o fuoristrada", "Mezzi", "Transport/car.png", "a-f-G-E-V-C"),
    ("ambulanza", "Ambulanza", "Mezzi", "Service/emergency.png", "a-f-G-U-S-M"),
    ("vigili-fuoco", "Vigili del fuoco", "Mezzi", "Service/firebrigade.png", "a-f-G-E-V-C"),
    ("moto", "Moto o quad", "Mezzi", "Transport/motorbike.png", "a-f-G-E-V-C"),
    ("elicottero", "Elicottero", "Mezzi", "Transport/helipad.png", "a-f-A-C-H"),
    ("posto-comando", "Posto di comando", "Basi (simboli ICS)", "Misc/IncidentCommandPost.png", "a-f-G-I"),
    ("base", "Base", "Basi (simboli ICS)", "Misc/Base.png", "a-f-G-I"),
    ("campo", "Campo", "Basi (simboli ICS)", "Misc/Camp.png", "a-f-G-I"),
    ("raccolta", "Area di raccolta", "Basi (simboli ICS)", "Misc/StagingArea.png", "a-f-G-I"),
    ("elibase", "Elibase", "Basi (simboli ICS)", "Misc/Helibase.png", "a-f-G-I"),
    ("piazzola", "Piazzola elicottero", "Basi (simboli ICS)", "Misc/Helipad1.png", "a-f-G-I"),
    ("traliccio", "Ripetitore o traliccio", "Luoghi", "Landmarks/tower.png", "a-f-G-I"),
    ("rifugio", "Rifugio", "Luoghi", "Lodging/chalet.png", "a-f-G-I"),
    ("bivacco", "Bivacco", "Luoghi", "Lodging/shelter-bw.png", "a-f-G-I"),
    ("tenda", "Tenda", "Luoghi", "Lodging/camping.png", "a-f-G-I"),
    ("ospedale", "Ospedale", "Luoghi", "Landmarks/hospital.png", "a-f-G-I"),
    ("rosso", "Pallino rosso", "Colori", "Waypoints/wptred.png", "a-f-G-U-C"),
    ("blu", "Pallino blu", "Colori", "Waypoints/wptblue.png", "a-f-G-U-C"),
    ("verde", "Pallino verde", "Colori", "Waypoints/wptgreen.png", "a-f-G-U-C"),
    ("arancione", "Pallino arancione", "Colori", "Waypoints/wptorange.png", "a-f-G-U-C"),
    ("giallo", "Pallino giallo", "Colori", "Waypoints/wptyellow.png", "a-f-G-U-C"),
)
ICONE_PER_CHIAVE = {c: {"chiave": c, "nome": n, "gruppo": g, "percorso": f"{ICONSET_OSM}/{f}", "tipo": t}
                    for c, n, g, f, t in ICONE_MAPPA}
RE_ID_RADIO = re.compile(r"^![0-9a-f]{8}$")
# Dalla 1.13.0, "Gestisci il Raspberry": comandi che il Raspberry fa da se' (mesh2tak-aggiorna, root). Il server li
# scrive nella registrazione del ponte e lo avvisa dal tunnel; il certificato nuovo il Raspberry lo scarica via SSH.
AZIONI_PONTE = ("riavvia-programma", "riavvia-radio", "riavvia-raspberry", "registro", "impostazioni",
                "reimposta-radio")
AZIONI_DURATA_S = 3600                  # nella registrazione per un'ora (il Raspberry ne fa solo di mezz'ora)
AZIONI_MAX = 10
CERTIFICATO_PONTE_SCADE_S = 24 * 3600   # certificato consegnato o no: dopo un giorno via dal server
SQUADRE_TAK = ("White", "Yellow", "Orange", "Magenta", "Red", "Maroon", "Purple", "Dark Blue", "Blue", "Cyan", "Teal",
               "Green", "Dark Green", "Brown")
# impostazioni del ponte che si cambiano dalla pagina: tipo, minimo, massimo (il Raspberry le ricontrolla)
IMPOSTAZIONI_PONTE = {
    "squadra": ("squadra", None, None),
    "validita_min": ("numero", 5, 1440),
    "aggiornamento_min": ("numero", 0.5, 60),
    "errore_massimo_m": ("numero", 10, 100000),
    "includi_ponte": ("si_no", None, None),
    "invio": ("si_no", None, None),
    "tempo_radio_percento": ("numero", 0.5, 10),
    "pausa_massima_min": ("numero", 1, 240),
}
SSHD_DROPIN = "/etc/ssh/sshd_config.d/mesh2tak-ponti.conf"
OPT_DIR = "/opt/mesh2tak-rete"
OPT_SCRIPT = OPT_DIR + "/rete_meshtastic.py"
UTENTE_WEB = "mesh2tak-rete"
PORTA_WEB = int(os.environ.get("RETE_PROVA_PORTA_WEB", "19400"))
UNITA_WEB = "/etc/systemd/system/mesh2tak-rete-web.service"
UNITA_LAVORI = "/etc/systemd/system/mesh2tak-rete-lavori.service"
UNITA_PERCORSO = "/etc/systemd/system/mesh2tak-rete-lavori.path"
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
REGISTRO_LOG = "/var/log/mesh2tak-rete.log"
GRUPPO = "ponti-meshtastic"
PORTA_BASE = 19443
INTESTAZIONI_AK = ("X-Authentik-Username", "X-Authentik-Groups", "X-Authentik-Email", "X-Authentik-Name",
                   "X-Authentik-Uid", "X-Authentik-Jwt", "X-Authentik-Meta-App", "X-Authentik-Meta-Provider")
APP_RETE = "rete-meshtastic"
PROVIDER_RETE = "Rete Meshtastic"
POLITICA_ADMIN = "Allow authentik Admins"
GRUPPO_ADMIN = "authentik Admins"
RE_NOME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,18}[a-z0-9])?$")

# Sostituiti da build.py con la pagina della rete e la sua icona (base64)
PAGINA_B64 = "@@PAGINA_B64@@"
ICONA_B64 = "@@ICONA_B64@@"

if sys.stdout.isatty() or os.environ.get("RETE_COLORI"):
    V, G, R, B, N = "\033[32m", "\033[33m", "\033[31m", "\033[1m", "\033[0m"
else:
    V = G = R = B = N = ""


class Errore(Exception):
    """Errore da spiegare all'utente (senza traccia Python)."""


def passo(t: str) -> None:
    print(f"\n{B}{t}{N}", flush=True)


def ok(t: str) -> None:
    print(f"  {V}✔{N} {t}", flush=True)


def info(t: str) -> None:
    print(f"    {t}", flush=True)


def avviso(t: str) -> None:
    print(f"  {G}!{N} {t}", flush=True)


def registra(t: str) -> None:
    try:
        with open(P(REGISTRO_LOG), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + t.rstrip() + "\n")
        os.chmod(P(REGISTRO_LOG), 0o600)
    except OSError:
        pass


def esegui(comando: List[str], controlla: bool = True, ambiente: Optional[dict] = None,
           attesa: int = 120) -> subprocess.CompletedProcess:
    registra("$ " + " ".join(shlex.quote(c) for c in comando))
    try:
        r = subprocess.run(comando, capture_output=True, text=True, timeout=attesa, env=ambiente)
    except FileNotFoundError:
        raise Errore(f"comando non trovato: {comando[0]}")
    except subprocess.TimeoutExpired:
        raise Errore(f"il comando {comando[0]} non ha risposto in {attesa} s")
    uscita = ((r.stdout or "") + (r.stderr or "")).strip()
    if uscita:
        registra(uscita[-2000:])
    if controlla and r.returncode != 0:
        raise Errore(f"{' '.join(comando[:3])} non riuscito: {uscita[-400:] or 'codice ' + str(r.returncode)}")
    return r


def leggi(percorso: str) -> Optional[str]:
    try:
        with open(percorso, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def scrivi(percorso: str, testo: str, modo: int = 0o644) -> bool:
    """Scrive solo se cambia (in modo atomico). True se e' cambiato."""
    if leggi(percorso) == testo:
        os.chmod(percorso, modo)
        return False
    os.makedirs(os.path.dirname(percorso), exist_ok=True)
    nuovo = percorso + ".nuovo"
    with open(nuovo, "w", encoding="utf-8") as f:
        f.write(testo)
    os.chmod(nuovo, modo)
    os.replace(nuovo, percorso)
    return True


def togli(percorso: str) -> bool:
    try:
        os.remove(percorso)
        return True
    except FileNotFoundError:
        return False


