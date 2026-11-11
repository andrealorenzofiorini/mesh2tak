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


# --------------------------------------------------------------------------
# Impostazioni di infra-TAK
# --------------------------------------------------------------------------

class Impostazioni:
    def __init__(self, dati: dict, percorso: str):
        self.dati = dati
        self.percorso = percorso
        self.dominio = str(dati.get("fqdn") or "").strip().lower().rstrip(".")
        self.server_ip = str(dati.get("server_ip") or "").strip()
        self.ssl = str(dati.get("ssl_mode") or "").strip()
        try:
            self.porta_https = int(dati.get("caddy_https_port") or 443)
        except (TypeError, ValueError):
            self.porta_https = 443
        if not re.match(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$", self.dominio) or "." not in self.dominio:
            raise Errore("infra-TAK non ha un dominio (modalita' FQDN): la rete Meshtastic ha bisogno di "
                         "indirizzi tipo meshtastic.tuodominio.it")
        remoto = dati.get("authentik_deployment") or {}
        if isinstance(remoto, dict) and remoto.get("target_mode") == "remote":
            raise Errore("Authentik gira su un altro server: questo script va bene solo con Authentik sullo "
                         "stesso server di Caddy")

    def host(self, nome: Optional[str] = None) -> str:
        return f"meshtastic-{nome}.{self.dominio}" if nome else f"meshtastic.{self.dominio}"

    def url(self, nome: Optional[str] = None) -> str:
        porta = "" if self.porta_https == 443 else f":{self.porta_https}"
        return f"https://{self.host(nome)}{porta}/"


def trova_impostazioni() -> Impostazioni:
    candidati = []
    unita = leggi(P("/etc/systemd/system/takwerx-console.service")) or ""
    for riga in unita.splitlines():
        if riga.strip().startswith("WorkingDirectory="):
            candidati.append(P(riga.split("=", 1)[1].strip()) + "/.config/settings.json")
    candidati += [P("/opt/infratak/.config/settings.json"), P("/root/infra-TAK/.config/settings.json")]
    candidati += sorted(glob.glob(P("/home/*/infra-TAK/.config/settings.json")))
    candidati += sorted(glob.glob(P("/opt/*/.config/settings.json")))
    for c in candidati:
        testo = leggi(c)
        if not testo:
            continue
        try:
            dati = json.loads(testo)
        except ValueError:
            continue
        if isinstance(dati, dict) and dati.get("fqdn"):
            return Impostazioni(dati, c)
    raise Errore("Non trovo la configurazione di infra-TAK (.config/settings.json): lancia lo script sul "
                 "server dove gira la console infra-TAK")


def token_authentik() -> str:
    candidati = [P("/root/authentik/.env"), P("/home/takwerx/authentik/.env")]
    candidati += sorted(glob.glob(P("/home/*/authentik/.env")))
    for c in candidati:
        testo = leggi(c) or ""
        valori = {}
        for riga in testo.splitlines():
            riga = riga.strip()
            if "=" in riga and not riga.startswith("#"):
                k, v = riga.split("=", 1)
                valori[k.strip()] = v.strip().strip("'\"")
        token = valori.get("AUTHENTIK_BOOTSTRAP_TOKEN") or valori.get("AUTHENTIK_TOKEN")
        if token:
            return token
    raise Errore("Non trovo il token di Authentik (AUTHENTIK_BOOTSTRAP_TOKEN in authentik/.env)")


# --------------------------------------------------------------------------
# Registro dei ponti
# --------------------------------------------------------------------------

def carica_registro() -> dict:
    testo = leggi(P(REGISTRO))
    if not testo:
        return {"formato": 1, "ponti": {}, "authentik": {"ponti": {}}}
    try:
        reg = json.loads(testo)
    except ValueError:
        raise Errore(f"{REGISTRO} e' rovinato: correggilo o cancellalo e rilancia 'aggiorna'")
    reg.setdefault("ponti", {})
    reg.setdefault("authentik", {}).setdefault("ponti", {})
    return reg


def salva_registro(reg: dict) -> None:
    os.makedirs(P(CONF_DIR), exist_ok=True)
    os.chmod(P(CONF_DIR), 0o755)
    scrivi(P(REGISTRO), json.dumps(reg, indent=2, ensure_ascii=False, sort_keys=True) + "\n", 0o600)


def leggi_chiave(testo: str) -> Tuple[str, str]:
    """(riga 'ssh-ed25519 AAAA... commento' pulita, impronta sha256 esadecimale)."""
    parti = testo.strip().split()
    if len(parti) < 2 or parti[0] != "ssh-ed25519":
        raise Errore("la chiave deve essere quella mostrata dal Raspberry: inizia con ssh-ed25519 AAAA...")
    try:
        blob = base64.b64decode(parti[1], validate=True)
    except (binascii.Error, ValueError):
        raise Errore("chiave non valida (copiata a meta'?): ricopia la riga intera mostrata dal Raspberry")
    if not blob.startswith(b"\x00\x00\x00\x0bssh-ed25519") or len(blob) != 51:
        raise Errore("chiave non valida (copiata a meta'?): ricopia la riga intera mostrata dal Raspberry")
    commento = re.sub(r"[^A-Za-z0-9@._-]", "", " ".join(parti[2:]))[:60]
    riga = f"ssh-ed25519 {parti[1]}" + (f" {commento}" if commento else "")
    return riga, hashlib.sha256(blob).hexdigest()


def codice_controllo(impronta: str) -> str:
    """Sei caratteri dall'impronta della chiave (stesso calcolo sul Raspberry): li mostra l'installatore e
    la pagina della rete, per approvare la richiesta giusta."""
    n = int(impronta[:8], 16) >> 2
    c = "".join(CROCKFORD[(n >> (5 * i)) & 31] for i in range(5, -1, -1))
    return f"{c[:3]}-{c[3:]}"


# --------------------------------------------------------------------------
# DNS
# --------------------------------------------------------------------------

def indirizzi(nome: str) -> List[str]:
    try:
        return sorted({a[4][0] for a in socket.getaddrinfo(nome, None, socket.AF_INET, socket.SOCK_STREAM)})
    except (socket.gaierror, UnicodeError):
        return []


def controlla_dns(imp: Impostazioni, nomi: List[str], ignora: bool) -> None:
    if shutil.which("resolvectl"):
        esegui(["resolvectl", "flush-caches"], controlla=False, attesa=10)
    atteso = imp.server_ip
    problemi = []
    for n in nomi:
        ips = indirizzi(n)
        if not ips:
            problemi.append((n, "non esiste"))
        elif atteso and atteso not in ips:
            problemi.append((n, f"punta a {', '.join(ips)} invece che a {atteso}"))
    if not problemi:
        ok("indirizzi presenti nel DNS: " + ", ".join(nomi))
        return
    for n, p in problemi:
        avviso(f"{n} {p}")
    if ignora:
        avviso("vado avanti lo stesso (--ignora-dns): Caddy avra' il certificato solo quando il DNS e' giusto")
        return
    info("")
    info("Nel DNS del dominio (Cloudflare: dash.cloudflare.com -> " + imp.dominio + " -> DNS -> Records ->")
    info("Add record) aggiungi, uno per riga qui sotto:")
    for n, _ in problemi:
        etichetta = n[:-len(imp.dominio) - 1] if n.endswith("." + imp.dominio) else n
        info(f"    Type A   Name {etichetta}   IPv4 address {atteso or 'IP del server'}   Proxy status: DNS only (nuvola grigia)")
    info("Con un record *." + imp.dominio + " (DNS only) che punta al server non serve aggiungere nulla.")
    info("Poi aspetta un paio di minuti e rilancia lo stesso comando.")
    raise Errore("indirizzi mancanti nel DNS")


# --------------------------------------------------------------------------
# sshd: utenti dei ponti
# --------------------------------------------------------------------------

def comando_ponte() -> str:
    """Il solo comando che un ponte puo' lanciare via SSH (dalla 1.10.0): legge la sua configurazione da
    installare e scrive a che punto e' l'installazione. Sempre questo, qualunque cosa chieda il Raspberry."""
    prova = f"/usr/bin/env RETE_PROVA_RADICE={RADICE} " if RADICE else ""
    return f"{prova}/usr/bin/python3 -I {P(OPT_SCRIPT)} ponte"


def testo_dropin() -> str:
    return "\n".join([
        "# Aggiunto da rete-meshtastic.sh (mesh2tak): utenti dei ponti Meshtastic.",
        "# Possono solo tenere aperto un tunnel verso la loro porta sul server (127.0.0.1) e, dalla 1.10.0,",
        "# lanciare un solo comando: configurazione e avanzamento dell'installazione del loro ponte.",
        f"Match Group {GRUPPO}",
        f"\tAuthorizedKeysFile {P(CHIAVI_DIR)}/%u",
        "\tAuthenticationMethods publickey",
        "\tPubkeyAuthentication yes",
        "\tPasswordAuthentication no",
        "\tKbdInteractiveAuthentication no",
        "\tAllowTcpForwarding remote",
        "\tPermitOpen none",
        "\tAllowStreamLocalForwarding no",
        "\tAllowAgentForwarding no",
        "\tX11Forwarding no",
        "\tPermitTTY no",
        "\tPermitTunnel no",
        "\tPermitUserRC no",
        "\tGatewayPorts no",
        f"\tForceCommand {comando_ponte()}",
        "\tClientAliveInterval 30",
        "\tClientAliveCountMax 3",
        "",
    ])


def riga_autorizzata(chiave: str, porta: int) -> str:
    return (f'restrict,port-forwarding,permitlisten="127.0.0.1:{porta}",command="{comando_ponte()}" '
            f"{chiave}\n")


def stessa_chiave_e_porta(vecchia: Optional[str], nuova: str) -> bool:
    """Le due righe di authorized_keys hanno la stessa chiave e la stessa porta (cambia solo il comando): il
    tunnel aperto puo' restare."""
    def parti(riga: str) -> Tuple[str, str]:
        m = re.search(r'permitlisten="([^"]*)"', riga or "")
        k = re.search(r"(ssh-ed25519 [A-Za-z0-9+/=]+)", riga or "")
        return (m.group(1) if m else "", k.group(1) if k else "")
    a, b = parti(vecchia or ""), parti(nuova)
    return bool(a[0] and a[1]) and a == b


def ricarica_ssh() -> None:
    for unita in ("ssh", "sshd"):
        r = esegui(["systemctl", "is-active", unita], controlla=False, attesa=20)
        if r.returncode == 0:
            esegui(["systemctl", "reload", unita], attesa=30)
            return
    # sshd attivato dal socket (Ubuntu 24.04): la configurazione nuova vale dalla prossima connessione


def prepara_sshd() -> None:
    try:
        grp.getgrnam(GRUPPO)
    except KeyError:
        esegui(["groupadd", "--system", GRUPPO])
    os.makedirs(P(CHIAVI_DIR), exist_ok=True)
    os.chmod(P(CONF_DIR), 0o755)
    os.chmod(P(CHIAVI_DIR), 0o755)
    prepara_cartelle_ponti()
    percorso = P(SSHD_DROPIN)
    prima = leggi(percorso)
    if prima == testo_dropin():
        return
    if not os.path.isdir(os.path.dirname(percorso)):
        raise Errore(f"manca {os.path.dirname(SSHD_DROPIN)}: serve OpenSSH 8.2 o piu' recente (Ubuntu 22.04 va bene)")
    principale = leggi(P("/etc/ssh/sshd_config")) or ""
    if principale and not re.search(r"^\s*Include\s+/etc/ssh/sshd_config\.d/", principale, re.M | re.I):
        avviso("sshd_config non include sshd_config.d: le regole dei ponti valgono solo per le chiavi "
               "(restrizioni comunque attive)")
    scrivi(percorso, testo_dropin(), 0o644)
    r = esegui(["sshd", "-t"], controlla=False, attesa=30)
    if r.returncode != 0:
        if prima is None:
            togli(percorso)
        else:
            scrivi(percorso, prima, 0o644)
        raise Errore("sshd rifiuta le regole per i ponti (nessuna modifica applicata): "
                     + ((r.stdout or "") + (r.stderr or "")).strip()[-300:])
    ricarica_ssh()
    ok(f"regole di sshd per i ponti: {SSHD_DROPIN}")


def prepara_cartelle_ponti() -> None:
    """Le cartelle che il comando dei ponti usa (dalla 1.10.0), con il gruppo dei ponti e il bit sticky: ogni
    ponte crea e toglie solo i suoi file, non vede l'elenco degli altri. La configurazione da installare la legge
    solo il suo ponte; l'avanzamento lo legge anche il servizio della pagina (senza privilegi)."""
    try:
        gid = grp.getgrnam(GRUPPO).gr_gid
    except KeyError:
        if not RADICE:
            raise Errore(f"manca il gruppo {GRUPPO}")
        gid = None                      # prove con groupadd finto
    os.makedirs(P(WEB_DIR), exist_ok=True)
    os.chmod(P(WEB_DIR), 0o755)
    for d, modo in ((CONFIGURAZIONI_DIR, 0o1730), (AVANZAMENTO_DIR, 0o1731)):
        os.makedirs(P(d), exist_ok=True)
        if gid is not None:
            os.chown(P(d), 0, gid)
        os.chmod(P(d), modo)
    os.makedirs(P(PONTI_NUOVI_DIR), exist_ok=True)
    os.chmod(P(PONTI_NUOVI_DIR), 0o700)


def porta_ssh() -> int:
    r = esegui(["sshd", "-T"], controlla=False, attesa=30)
    for riga in (r.stdout or "").splitlines():
        if riga.lower().startswith("port "):
            try:
                return int(riga.split()[1])
            except (IndexError, ValueError):
                break
    return 22


def chiavi_host() -> List[str]:
    chiavi = []
    for f in sorted(glob.glob(P("/etc/ssh/ssh_host_*_key.pub"))):
        parti = (leggi(f) or "").split()
        if len(parti) >= 2 and re.match(r"^(ssh-ed25519|ecdsa-sha2-nistp\d+|ssh-rsa)$", parti[0]):
            chiavi.append(f"{parti[0]} {parti[1]}")
    if not chiavi:
        raise Errore("non trovo le chiavi SSH del server in /etc/ssh/ssh_host_*_key.pub")
    return chiavi


def utente_ponte(nome: str) -> str:
    return f"ponte-{nome}"


SHELL_PONTI = "/bin/sh"   # sshd lancia il comando forzato con la shell dell'utente: con nologin non partirebbe


def prepara_utente(nome: str) -> str:
    utente = utente_ponte(nome)
    try:
        pw = pwd.getpwnam(utente)
    except KeyError:
        esegui(["useradd", "--system", "--no-create-home", "--home-dir", "/nonexistent", "--shell",
                SHELL_PONTI, "--gid", GRUPPO, "--comment", f"Ponte Meshtastic {nome} (mesh2tak)", utente])
    else:
        if pw.pw_gid != grp.getgrnam(GRUPPO).gr_gid:
            raise Errore(f"esiste gia' un utente {utente} che non e' di un ponte: scegli un altro nome")
        if pw.pw_shell != SHELL_PONTI:
            # utenti creati prima della 1.10.0. Resta tutto chiuso: sshd lancia solo il comando forzato
            # (ForceCommand), senza terminale, e l'account non ha password
            esegui(["usermod", "-s", SHELL_PONTI, utente])
    # nessuna password ma account non bloccato: con UsePAM no sshd rifiuta anche le chiavi di un account '!'
    esegui(["usermod", "-p", "*", utente])
    return utente


def chiudi_sessioni(utente: str) -> None:
    esegui(["pkill", "-KILL", "-u", utente], controlla=False, attesa=20)


def porte_in_ascolto() -> set:
    porte = set()
    if shutil.which("ss"):
        r = esegui(["ss", "-ltnH"], controlla=False, attesa=20)
        for riga in (r.stdout or "").splitlines():
            campi = riga.split()
            if len(campi) >= 4:
                m = re.search(r":(\d+)$", campi[3])
                if m:
                    porte.add(int(m.group(1)))
        return porte
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):   # senza ss: le porte in ascolto (stato 0A) dal kernel
        for riga in (leggi(f) or "").splitlines()[1:]:
            campi = riga.split()
            if len(campi) > 3 and campi[3] == "0A":
                porte.add(int(campi[1].rsplit(":", 1)[1], 16))
    return porte


# --------------------------------------------------------------------------
# Pagina della rete e registrazioni per i Raspberry
# --------------------------------------------------------------------------

def risorsa(nome: str, incorporata: str) -> bytes:
    if not incorporata.startswith("@" "@"):   # (scritto cosi' perche' build.py non lo scambi per un segnaposto)
        return base64.b64decode(incorporata)
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pagina", nome), "rb") as f:
        return f.read()   # prove: file accanto allo script


def scrivi_sito(imp: Impostazioni, reg: dict) -> None:
    os.makedirs(P(SITO_DIR), exist_ok=True)
    os.makedirs(P(ACCESSO_DIR), exist_ok=True)
    for d in (P(WEB_DIR), P(SITO_DIR), P(ACCESSO_DIR)):
        os.chmod(d, 0o755)
    scrivi(P(SITO_DIR + "/index.html"), risorsa("index.html", PAGINA_B64).decode("utf-8"))
    scrivi(P(SITO_DIR + "/icona.svg"), risorsa("icona.svg", ICONA_B64).decode("utf-8"))
    ponti = [{"nome": n, "indirizzo": imp.url(n), "stato": f"stato/{n}.json",
              "aggiunto": p.get("aggiunto", "")} for n, p in sorted(reg["ponti"].items())]
    scrivi(P(SITO_DIR + "/ponti.json"), json.dumps({"formato": 1, "dominio": imp.dominio, "versione": VERSIONE,
                                                    "ponti": ponti}, ensure_ascii=False, indent=1) + "\n")
    # per gli avvisi del servizio (senza privilegi): dove chiedere lo stato dei ponti e dove mandare le email
    scrivi(P(SORVEGLIANZA), json.dumps({"formato": 1, "pagina": imp.url(), "console": porta_console(imp),
                                        "ponti": [{"nome": n, "porta": p["porta"]}
                                                  for n, p in sorted(reg["ponti"].items())]},
                                       ensure_ascii=False, indent=1) + "\n")
    scrivi_riassunto_nuovi(leggi_impostazioni_nuovi())
    scrivi_icone_mappa(imp)


def figure_infratak(imp: Impostazioni) -> Dict[str, bytes]:
    """Le figure delle icone dell'elenco, dal catalogo che infra-TAK usa per i suoi flussi (nodered/
    icon-catalog.json, le stesse icone che ATAK ha di serie). Il codice di mesh2tak non le contiene: la pagina
    le mostra solo se infra-TAK c'e' e le ha."""
    catalogo = os.path.join(os.path.dirname(os.path.dirname(imp.percorso)), "nodered", "icon-catalog.json")
    try:
        if os.path.getsize(catalogo) > 64 << 20:
            return {}
        with open(catalogo, encoding="utf-8") as f:
            dati = json.load(f)
    except (OSError, ValueError):
        return {}
    insiemi = {}
    for insieme in (dati.values() if isinstance(dati, dict) else []):
        if isinstance(insieme, dict) and isinstance(insieme.get("groups"), dict):
            insiemi[str(insieme.get("uid"))] = insieme["groups"]
    figure = {}
    for chiave, voce in ICONE_PER_CHIAVE.items():
        uid, gruppo, nome_file = voce["percorso"].split("/", 2)
        for x in (insiemi.get(uid) or {}).get(gruppo) or []:
            if not isinstance(x, dict) or x.get("f") != nome_file or not isinstance(x.get("b"), str):
                continue
            try:
                png = base64.b64decode(x["b"], validate=True)
            except (ValueError, binascii.Error):
                break
            if png.startswith(b"\x89PNG\r\n\x1a\n") and len(png) <= 32768:
                figure[chiave] = png
            break
    return figure


def scrivi_icone_mappa(imp: Impostazioni) -> None:
    """Elenco delle icone per la pagina (icone-mappa.json) e le loro figure (icone/CHIAVE.png)."""
    figure = figure_infratak(imp)
    os.makedirs(P(ICONE_SITO), exist_ok=True)
    os.chmod(P(ICONE_SITO), 0o755)
    for f in glob.glob(P(ICONE_SITO) + "/*.png"):
        if os.path.basename(f)[:-4] not in figure:
            togli(f)
    for chiave, png in figure.items():
        percorso = P(f"{ICONE_SITO}/{chiave}.png")
        if leggi_bytes(percorso) != png:
            with open(os.open(percorso + ".nuovo", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644), "wb") as f:
                f.write(png)
            os.chmod(percorso + ".nuovo", 0o644)
            os.replace(percorso + ".nuovo", percorso)
    elenco = [{"chiave": v["chiave"], "nome": v["nome"], "gruppo": v["gruppo"], "tipo": v["tipo"],
               "figura": f"icone/{v['chiave']}.png" if v["chiave"] in figure else ""}
              for v in ICONE_PER_CHIAVE.values()]
    scrivi(P(ICONE_ELENCO), json.dumps({"formato": 1, "icone": elenco}, ensure_ascii=False, indent=1) + "\n")


def leggi_bytes(percorso: str) -> Optional[bytes]:
    try:
        with open(percorso, "rb") as f:
            return f.read()
    except OSError:
        return None


def porta_console(imp: Impostazioni) -> int:
    try:
        porta = int(imp.dati.get("console_port") or 5001)
    except (TypeError, ValueError):
        porta = 5001
    return porta if 0 < porta < 65536 else 5001


def scrivi_registrazione(imp: Impostazioni, nome: str, p: dict, porta_sshd: int, chiavi: List[str]) -> None:
    # anche la chiave del Raspberry: il nome del file e' la sua impronta, quindi la conosce gia' chi lo trova
    dati = {"formato": 1, "nome": nome, "utente": utente_ponte(nome), "porta": p["porta"],
            "ssh_host": imp.host(), "ssh_porta": porta_sshd, "chiavi_host": chiavi, "indirizzo": imp.url(nome),
            "chiave": p["chiave"]}
    agg = p.get("aggiorna")
    if isinstance(agg, dict) and RE_VERSIONE_RILASCIO.match(str(agg.get("versione", ""))):
        # dalla 1.8.0: il Raspberry vede la richiesta e scarica quella versione da GitHub (mai da qui)
        dati["aggiorna"] = {"versione": agg["versione"], "chiesto": int(agg.get("chiesto") or 0)}
    ins = p.get("installa")
    if isinstance(ins, dict) and os.path.exists(file_configurazione(nome)):
        # dalla 1.10.0: c'e' una configurazione da installare; il Raspberry la scarica via SSH, con la sua chiave
        dati["installa"] = {"tentativo": int(ins.get("tentativo") or 1)}
    scrivi(P(f"{ACCESSO_DIR}/{p['impronta']}.json"), json.dumps(dati, indent=1) + "\n")


# --------------------------------------------------------------------------
# Caddy
# --------------------------------------------------------------------------

def riga_tls(imp: Impostazioni, nomi: List[str]) -> str:
    """Con il certificato proprio di infra-TAK (ssl_mode custom) i siti nuovi usano lo stesso."""
    if imp.ssl != "custom":
        return ""
    testo = (leggi(P(CADDYFILE)) or "").split(MARCATORE_INFRATAK, 1)[0]
    m = re.search(r"^\s*tls\s+(\S+)\s+(\S+)\s*$", testo, re.M)
    if not m:
        raise Errore("infra-TAK usa un certificato proprio ma non lo trovo nel Caddyfile")
    cert = m.group(1)
    try:
        import ssl
        dati = ssl._ssl._test_decode_cert(P(cert))  # noqa: SLF001 (legge i nomi del certificato)
        coperti = [v.lower() for k, v in dati.get("subjectAltName", ()) if k == "DNS"]
    except Exception:  # noqa
        coperti = []
    for n in nomi:
        if coperti and not any(c == n or (c.startswith("*.") and n.count(".") == c.count(".")
                                          and n.endswith(c[1:])) for c in coperti):
            raise Errore(f"il certificato di infra-TAK non copre {n} (copre: {', '.join(coperti)}): serve un "
                         f"certificato *.{imp.dominio} oppure Let's Encrypt")
    return f"\ttls {m.group(1)} {m.group(2)}"


def testo_caddy(imp: Impostazioni, reg: dict) -> str:
    nomi = [imp.host()] + [imp.host(n) for n in sorted(reg["ponti"])]
    tls = riga_tls(imp, nomi)
    togli_ak = [f"\t\trequest_header -{h}" for h in INTESTAZIONI_AK]
    auth = [f"\t\treverse_proxy /outpost.goauthentik.io/* {AK_UPSTREAM}",
            f"\t\tforward_auth {AK_UPSTREAM} {{",
            "\t\t\turi /outpost.goauthentik.io/auth/caddy",
            "\t\t\tcopy_headers X-Authentik-Username",
            "\t\t\ttrusted_proxies private_ranges",
            "\t\t}"]
    r = ["# Generato da rete-meshtastic.sh (mesh2tak): non modificarlo a mano, rilancia lo script.",
         f"# Pagina della rete: {imp.url()}  -  un indirizzo per ponte: {imp.url('NOME')}", ""]
    r += [f"{imp.host()} {{"] + ([tls] if tls else []) + ["\troute {"] + togli_ak
    senza_cookie = ["\t\t\t\theader_up -Cookie", "\t\t\t\theader_up -Authorization"]
    r += ["\t\t# pubbliche: richieste dei Raspberry, loro registrazioni (nome = impronta della chiave) e icona",
          "\t\thandle /accesso/richiesta {",
          "\t\t\trequest_body {",
          "\t\t\t\tmax_size 16KB",
          "\t\t\t}",
          f"\t\t\treverse_proxy 127.0.0.1:{PORTA_WEB} {{"] + senza_cookie + [
          "\t\t\t}",
          "\t\t}",
          "\t\thandle /accesso/* {",
          f"\t\t\troot * {P(WEB_DIR)}",
          "\t\t\theader Cache-Control no-store",
          "\t\t\tfile_server",
          "\t\t}",
          "\t\thandle /icona.svg {",
          f"\t\t\troot * {P(SITO_DIR)}",
          "\t\t\theader Cache-Control \"public, max-age=86400\"",
          "\t\t\tfile_server",
          "\t\t}"]
    r += auth
    r += ["\t\t# certificato e canale per i ponti nuovi (dalla 1.10.0): il pacchetto della console e' piu' grande",
          "\t\thandle /api/impostazioni-nuovi {",
          "\t\t\trequest_body {",
          "\t\t\t\tmax_size 400KB",
          "\t\t\t}",
          f"\t\t\treverse_proxy 127.0.0.1:{PORTA_WEB} {{"] + senza_cookie + [
          "\t\t\t}",
          "\t\t}"]
    r += ["\t\thandle /api/* {",
          "\t\t\trequest_body {",
          "\t\t\t\tmax_size 16KB",
          "\t\t\t}",
          f"\t\t\treverse_proxy 127.0.0.1:{PORTA_WEB} {{"] + senza_cookie + [
          "\t\t\t}",
          "\t\t}"]
    for n, p in sorted(reg["ponti"].items()):
        r += [f"\t\thandle /stato/{n}.json {{",
              "\t\t\trewrite * /mesh2tak/stato.json",
              f"\t\t\treverse_proxy https://127.0.0.1:{p['porta']} {{"] + senza_cookie + [
              "\t\t\t\ttransport http {",
              "\t\t\t\t\ttls_insecure_skip_verify",
              "\t\t\t\t\tdial_timeout 5s",
              "\t\t\t\t\tresponse_header_timeout 15s",
              "\t\t\t\t}",
              "\t\t\t}",
              "\t\t}",
              # dalla 1.10.0: le radio della squadra gestite attraverso il nodo del ponte (solo amministratori:
              # il ponte vuole il nome messo da Authentik e risponde solo a quello che arriva dal tunnel)
              f"\t\thandle_path /radio/{n}/* {{",
              "\t\t\trequest_body {",
              "\t\t\t\tmax_size 16KB",
              "\t\t\t}",
              "\t\t\trewrite * /mesh2tak/radio{uri}",
              f"\t\t\treverse_proxy https://127.0.0.1:{p['porta']} {{"] + senza_cookie + [
              "\t\t\t\theader_up X-Forwarded-Host {hostport}",
              "\t\t\t\ttransport http {",
              "\t\t\t\t\ttls_insecure_skip_verify",
              "\t\t\t\t\tdial_timeout 5s",
              "\t\t\t\t\tresponse_header_timeout 15s",
              "\t\t\t\t}",
              "\t\t\t}",
              "\t\t}"]
    r += ["\t\thandle /stato/* {",
          "\t\t\trespond 404",
          "\t\t}",
          "\t\thandle /radio/* {",
          "\t\t\trespond 404",
          "\t\t}",
          "\t\theader Cache-Control no-store",
          "\t\theader X-Content-Type-Options nosniff",
          "\t\theader Referrer-Policy no-referrer",
          "\t\theader Content-Security-Policy \"default-src 'self'; script-src 'self' 'unsafe-inline'; "
          "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
          "base-uri 'none'; form-action 'none'\"",
          f"\t\troot * {P(SITO_DIR)}",
          "\t\tfile_server",
          "\t}",
          "}",
          ""]
    for n, p in sorted(reg["ponti"].items()):
        r += [f"# Gestione del nodo del ponte {n} (tunnel SSH dal Raspberry alla porta {p['porta']})",
              f"{imp.host(n)} {{"] + ([tls] if tls else []) + ["\troute {"] + togli_ak + auth
        # il Raspberry non riceve i cookie del browser: con la sessione di Authentik (valida per tutto il
        # dominio) un Raspberry rubato potrebbe entrare nella console di infra-TAK
        r += [f"\t\treverse_proxy https://127.0.0.1:{p['porta']} {{",
              "\t\t\theader_up -Cookie",
              "\t\t\theader_up -Authorization",
              "\t\t\ttransport http {",
              "\t\t\t\ttls_insecure_skip_verify",
              "\t\t\t\tdial_timeout 5s",
              "\t\t\t}",
              "\t\t}",
              "\t}",
              "\thandle_errors {",
              "\t\t@giu expression `{err.status_code} in [502, 503, 504]`",
              "\t\theader @giu Content-Type \"text/plain; charset=utf-8\"",
              f"\t\trespond @giu \"Il ponte {n} non risponde: Raspberry spento, senza internet o tunnel in "
              f"riconnessione. Riprova tra un minuto. Stato di tutti i ponti: {imp.url()}\" 502",
              "\t}",
              "}",
              ""]
    return "\n".join(r)


def blocco_import() -> str:
    return f"{INIZIO_BLOCCO}\nimport {P(CADDY_DIR)}/*.caddy\n{FINE_BLOCCO}\n"


def caddyfile_con_import(testo: str, con: bool) -> str:
    """Il Caddyfile di infra-TAK con (o senza) le nostre tre righe, sotto il suo marcatore: infra-TAK
    conserva tutto quello che sta sotto quando rigenera il file."""
    schema = re.compile(re.escape(INIZIO_BLOCCO) + r".*?" + re.escape(FINE_BLOCCO) + r"\n?", re.S)
    senza = schema.sub("", testo)
    if not con:
        return re.sub(r"\n{3,}$", "\n\n", senza)
    if MARCATORE_INFRATAK not in senza:
        senza = senza.rstrip("\n") + "\n\n" + MARCATORE_INFRATAK + "\n"
    return senza.rstrip("\n") + "\n\n" + blocco_import()


def ambiente_caddy() -> dict:
    """L'ambiente del servizio caddy (credenziali ACME di infra-TAK): senza, caddy validate sbaglia."""
    ambiente = dict(os.environ)
    r = esegui(["systemctl", "show", "caddy", "--property=Environment", "--value"], controlla=False, attesa=20)
    try:
        for voce in shlex.split(r.stdout or ""):
            if "=" in voce:
                k, v = voce.split("=", 1)
                ambiente[k] = v
    except ValueError:
        pass
    return ambiente


def applica_caddy(nuovi: Dict[str, Optional[str]]) -> None:
    """Scrive i file (None = toglie), li fa controllare a Caddy e lo ricarica; se qualcosa non va rimette
    tutto com'era: Caddy continua a servire la configurazione di prima."""
    prima = {p: leggi(p) for p in nuovi}
    if all(prima[p] == t for p, t in nuovi.items()):
        ok("Caddy gia' a posto")
        return

    def rimetti():
        for p, t in prima.items():
            if t is None:
                togli(p)
            else:
                scrivi(p, t, 0o644)

    for p, t in nuovi.items():
        if t is None:
            togli(p)
        else:
            scrivi(p, t, 0o644)
    r = esegui(["caddy", "validate", "--config", P(CADDYFILE), "--adapter", "caddyfile"], controlla=False,
               ambiente=ambiente_caddy(), attesa=90)
    if r.returncode != 0:
        rimetti()
        uscita = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
        errori = [x for x in uscita if "error" in x.lower()] or uscita
        raise Errore("Caddy rifiuta la configurazione, nessuna modifica applicata: " + " | ".join(errori[-3:])[-500:])
    r = esegui(["systemctl", "reload", "caddy"], controlla=False, attesa=90)
    if r.returncode != 0:
        rimetti()
        esegui(["systemctl", "reload", "caddy"], controlla=False, attesa=90)
        raise Errore("Caddy non ha accettato la configurazione nuova: rimessa quella di prima. Dettagli in "
                     f"{REGISTRO_LOG}")
    ok("Caddy aggiornato e ricaricato (certificati Let's Encrypt in automatico)")


def configura_caddy(imp: Impostazioni, reg: dict, con: bool = True) -> None:
    caddyfile = leggi(P(CADDYFILE))
    if caddyfile is None:
        raise Errore(f"non trovo {CADDYFILE}: Caddy di infra-TAK non e' installato?")
    os.makedirs(P(CADDY_DIR), exist_ok=True)
    os.chmod(P(CADDY_DIR), 0o755)
    applica_caddy({P(CADDYFILE): caddyfile_con_import(caddyfile, con),
                   P(CADDY_FILE): testo_caddy(imp, reg) if con else None})


# --------------------------------------------------------------------------
# Authentik
# --------------------------------------------------------------------------

class ErroreAK(Errore):
    def __init__(self, codice: int, messaggio: str):
        super().__init__(messaggio)
        self.codice = codice


class Authentik:
    def __init__(self, url: str, token: str):
        self.url = url
        self.token = token
        self._apri = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def chiama(self, metodo: str, percorso: str, corpo=None):
        dati = json.dumps(corpo).encode() if corpo is not None else None
        rq = urllib.request.Request(f"{self.url}/api/v3/{percorso}", data=dati, method=metodo, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
            "Accept": "application/json"})
        try:
            with self._apri.open(rq, timeout=30) as r:
                testo = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            testo = e.read(600).decode("utf-8", "replace")
            registra(f"Authentik {metodo} {percorso}: {e.code} {testo}")
            raise ErroreAK(e.code, f"Authentik risponde {e.code} a {metodo} {percorso}: {testo[:200]}")
        except (urllib.error.URLError, OSError) as e:
            raise Errore(f"Authentik non risponde su {self.url} ({e}): e' acceso?")
        registra(f"Authentik {metodo} {percorso}: ok")
        return json.loads(testo) if testo.strip() else None

    def tutti(self, percorso: str) -> List[dict]:
        sep = "&" if "?" in percorso else "?"
        risultati, pagina = [], 1
        while pagina < 50:
            d = self.chiama("GET", f"{percorso}{sep}page_size=100&page={pagina}") or {}
            risultati += d.get("results", [])
            if not (d.get("pagination") or {}).get("next"):
                break
            pagina += 1
        return risultati

    # ---- flussi, provider e applicazioni (stesso schema di infra-TAK per Node-RED) ----

    def flussi(self) -> Tuple[str, str]:
        aut = self.tutti("flows/instances/?designation=authorization&ordering=slug")
        inv = self.tutti("flows/instances/?designation=invalidation")
        a = next((f["pk"] for f in aut if "implicit" in f.get("slug", "")), aut[0]["pk"] if aut else None)
        i = next((f["pk"] for f in inv if "provider" not in f.get("slug", "")), inv[0]["pk"] if inv else None)
        if not a or not i:
            raise Errore("Authentik non ha i flussi di autorizzazione/uscita predefiniti")
        return a, i

    def provider(self, nome: str, esterno: str, dominio_cookie: str, flussi: Tuple[str, str]) -> int:
        dati = {"name": nome, "authorization_flow": flussi[0], "invalidation_flow": flussi[1],
                "external_host": esterno.rstrip("/"), "mode": "forward_single", "token_validity": "hours=24",
                "cookie_domain": dominio_cookie}
        esistenti = [p for p in self.tutti(f"providers/proxy/?search={urllib.parse.quote(nome)}")
                     if p.get("name") == nome]
        if esistenti:
            pk = esistenti[0]["pk"]
            self.chiama("PATCH", f"providers/proxy/{pk}/", {k: v for k, v in dati.items() if k != "name"})
            return pk
        return self.chiama("POST", "providers/proxy/", dati)["pk"]

    def applicazione(self, slug: str, nome: str, provider: int, avvio: str, descrizione: str,
                     icona: Optional[str] = None) -> dict:
        # policy_engine_mode non si tocca: se infra-TAK (W1) aggiunge l'MFA e mette 'all', resta cosi'
        dati = {"name": nome, "slug": slug, "provider": provider, "meta_launch_url": avvio,
                "open_in_new_tab": True, "meta_description": descrizione, "meta_publisher": "mesh2tak"}
        try:
            app = self.chiama("GET", f"core/applications/{slug}/")
        except ErroreAK as e:
            if e.codice != 404:
                raise
            app = self.chiama("POST", "core/applications/", dati)
        else:
            app = self.chiama("PATCH", f"core/applications/{slug}/", {k: v for k, v in dati.items() if k != "slug"})
        if icona and icona not in str(app.get("meta_icon") or "") + str(app.get("meta_icon_url") or ""):
            self._icona(slug, icona)
        return app

    def _icona(self, slug: str, url: str) -> None:
        # Authentik 2025.12+: meta_icon accetta un indirizzo; prima si passava da set_icon_url
        try:
            app = self.chiama("PATCH", f"core/applications/{slug}/", {"meta_icon": url})
            if url in str(app.get("meta_icon") or "") + str(app.get("meta_icon_url") or ""):
                return
        except ErroreAK:
            pass
        try:
            self.chiama("POST", f"core/applications/{slug}/set_icon_url/", {"url": url})
        except Errore as e:
            avviso(f"icona dell'applicazione non impostata ({e}): resta l'iniziale")

    def politica_admin(self) -> str:
        """La politica 'Allow authentik Admins' di infra-TAK (la crea come fa infra-TAK se manca)."""
        gruppi = [g for g in self.tutti(f"core/groups/?search={urllib.parse.quote(GRUPPO_ADMIN)}")
                  if g.get("name") == GRUPPO_ADMIN]
        if not gruppi:
            raise Errore(f"in Authentik manca il gruppo '{GRUPPO_ADMIN}'")
        for elenco in ("policies/all/", "policies/group_membership/"):
            try:
                for p in self.tutti(f"{elenco}?search={urllib.parse.quote(POLITICA_ADMIN)}"):
                    if p.get("name") == POLITICA_ADMIN:
                        return p["pk"]
            except ErroreAK as e:
                if e.codice != 404:
                    raise
        try:
            return self.chiama("POST", "policies/group_membership/", {"name": POLITICA_ADMIN,
                                                                      "group": gruppi[0]["pk"]})["pk"]
        except ErroreAK as e:
            if e.codice not in (404, 405):
                raise
        return self.chiama("POST", "policies/expression/", {
            "name": POLITICA_ADMIN,
            "expression": f'return ak_is_group_member(request.user, name="{GRUPPO_ADMIN}")'})["pk"]

    def solo_admin(self, app_pk: str, politica: str) -> None:
        legami = self.tutti(f"policies/bindings/?target={app_pk}")
        if any(str(b.get("policy")) == str(politica) for b in legami):
            return
        self.chiama("POST", "policies/bindings/", {"target": app_pk, "policy": politica, "order": 0,
                                                   "negate": False, "enabled": True, "timeout": 30})

    def _avamposto(self) -> Tuple[Optional[str], List[int]]:
        elenco = self.tutti("outposts/instances/?search=embedded")
        emb = next((o for o in elenco if "embed" in (o.get("name") or "").lower() or o.get("type") == "proxy"), None)
        if not emb:
            return None, []
        pieno = self.chiama("GET", f"outposts/instances/{emb['pk']}/")
        pks = []
        for p in pieno.get("providers") or []:
            pk = p if isinstance(p, int) else (p or {}).get("pk")
            if pk is not None:
                pks.append(pk)
        return emb["pk"], pks

    def avamposto_aggiungi(self, nuovi: List[int]) -> bool:
        pk, attuali = self._avamposto()
        if pk is None:
            raise Errore("non trovo l'avamposto integrato di Authentik (embedded outpost)")
        mancanti = [p for p in nuovi if p not in attuali]
        if not mancanti:
            return False
        self.chiama("PATCH", f"outposts/instances/{pk}/", {"providers": attuali + mancanti})
        return True

    def avamposto_togli(self, vecchi: List[int]) -> None:
        pk, attuali = self._avamposto()
        if pk is None:
            return
        restano = [p for p in attuali if p not in vecchi]
        if len(restano) != len(attuali):
            self.chiama("PATCH", f"outposts/instances/{pk}/", {"providers": restano})

    def elimina(self, slug: Optional[str], provider: Optional[int]) -> None:
        for percorso in ([f"core/applications/{slug}/"] if slug else []) + \
                        ([f"providers/proxy/{provider}/"] if provider else []):
            try:
                self.chiama("DELETE", percorso)
            except ErroreAK as e:
                if e.codice != 404:
                    raise


def riavvia_authentik() -> None:
    """L'avamposto integrato non legge da solo i provider nuovi: va riavviato authentik-server (come fa
    infra-TAK). Il login si ferma per circa un minuto."""
    r = esegui(["docker", "ps", "--filter", "name=authentik-server", "--format", "{{.Names}}"], controlla=False)
    nomi = [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
    if not nomi:
        avviso("contenitore authentik-server non trovato: riavvialo tu da infra-TAK (Authentik -> Restart)")
        return
    info(f"riavvio {nomi[0]} perche' legga gli indirizzi nuovi (il login si ferma per circa un minuto)...")
    esegui(["docker", "restart", nomi[0]], attesa=180)
    for _ in range(60):
        time.sleep(2)
        h = esegui(["docker", "inspect", "-f", "{{.State.Health.Status}}", nomi[0]], controlla=False).stdout.strip()
        if h == "healthy":
            ok("Authentik di nuovo pronto")
            return
        if not h:
            time.sleep(15)
            return
    avviso("Authentik non ancora pronto dopo 2 minuti: se le pagine danno 'Not Found', aspetta e ricarica")


def configura_authentik(imp: Impostazioni, reg: dict, rimossi: Optional[Dict[str, dict]] = None) -> None:
    ak = Authentik(AK_URL, token_authentik())
    flussi = ak.flussi()
    politica = ak.politica_admin()
    cookie = "." + imp.dominio
    voci = reg["authentik"]
    nuovi_provider = []
    pv = ak.provider(PROVIDER_RETE, imp.url(), cookie, flussi)
    app = ak.applicazione(APP_RETE, "Rete Meshtastic", pv, imp.url(),
                          "Stato dei ponti Meshtastic -> TAK e gestione dei nodi", icona=imp.url() + "icona.svg")
    ak.solo_admin(app["pk"], politica)
    voci["rete"] = {"provider": pv, "app": APP_RETE}
    nuovi_provider.append(pv)
    ok(f"applicazione 'Rete Meshtastic' in Authentik, solo per '{GRUPPO_ADMIN}'")
    for n in sorted(reg["ponti"]):
        slug = f"meshtastic-{n}"
        pv = ak.provider(f"Meshtastic {n}", imp.url(n), cookie, flussi)
        # nascosta nella pagina iniziale (blank://blank): ci si arriva dalla pagina della rete
        app = ak.applicazione(slug, f"Meshtastic {n}", pv, "blank://blank",
                              f"Gestione del nodo del ponte {n} (si apre dalla pagina Rete Meshtastic)")
        ak.solo_admin(app["pk"], politica)
        voci["ponti"][n] = {"provider": pv, "app": slug}
        nuovi_provider.append(pv)
    for n, v in (rimossi or {}).items():
        ak.avamposto_togli([v.get("provider")])
        ak.elimina(v.get("app"), v.get("provider"))
        voci["ponti"].pop(n, None)
        ok(f"tolto da Authentik: Meshtastic {n}")
    if ak.avamposto_aggiungi(nuovi_provider):
        riavvia_authentik()
    else:
        ok("avamposto di Authentik gia' a posto")


def togli_authentik(reg: dict) -> None:
    ak = Authentik(AK_URL, token_authentik())
    voci = list(reg["authentik"].get("ponti", {}).values())
    if reg["authentik"].get("rete"):
        voci.append(reg["authentik"]["rete"])
    ak.avamposto_togli([v.get("provider") for v in voci if v.get("provider")])
    for v in voci:
        ak.elimina(v.get("app"), v.get("provider"))
    reg["authentik"] = {"ponti": {}}
    ok("tolte da Authentik le applicazioni della rete Meshtastic")


# --------------------------------------------------------------------------
# Comandi
# --------------------------------------------------------------------------

class Blocco:
    """Una sola esecuzione alla volta (i lavori della pagina aspettano il loro turno)."""

    def __init__(self, attendi: bool = False):
        self.attendi = attendi

    def __enter__(self):
        os.makedirs(P(CONF_DIR), exist_ok=True)
        self.f = open(P(CONF_DIR + "/.blocco"), "w")
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | (0 if self.attendi else fcntl.LOCK_NB))
        except OSError:
            raise Errore("lo script sta gia' lavorando (da un'altra finestra o dalla pagina della rete): "
                         "aspetta che finisca")
        return self

    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def conferma(domanda: str, risposta: str, si: bool) -> None:
    if si:
        return
    if not sys.stdin.isatty():
        raise Errore("serve una conferma: rilancia aggiungendo --si")
    if input(f"    {domanda} Scrivi {risposta} per confermare: ").strip() != risposta:
        raise Errore("annullato, non ho toccato nulla")


def prepara_ponti(imp: Impostazioni, reg: dict, nomi: List[str]) -> None:
    """sshd, utenti e chiavi dei ponti indicati. Le registrazioni si pubblicano alla fine, quando Caddy e
    Authentik sono pronti: prima il Raspberry non bussa."""
    prepara_sshd()
    for n in nomi:
        p = reg["ponti"][n]
        utente = prepara_utente(n)
        riga = riga_autorizzata(p["chiave"], p["porta"])
        prima = leggi(P(f"{CHIAVI_DIR}/{utente}"))
        cambiata = scrivi(P(f"{CHIAVI_DIR}/{utente}"), riga, 0o644)
        for vecchia in p.pop("impronte_vecchie", []):
            togli(P(f"{ACCESSO_DIR}/{vecchia}.json"))   # il Raspberry con la chiave vecchia non trova piu' nulla
        if cambiata and not stessa_chiave_e_porta(prima, riga):
            chiudi_sessioni(utente)   # chiave o porta cambiate: il tunnel si riapre con quelle nuove
    ok(f"utenti dei ponti pronti: {', '.join(utente_ponte(n) for n in nomi) or 'nessuno'} (solo tunnel)")


def pubblica_registrazioni(imp: Impostazioni, reg: dict, nomi: List[str]) -> None:
    """Le registrazioni che i Raspberry leggono: utente, porta, chiavi del server."""
    porta_sshd = porta_ssh()
    chiavi = chiavi_host()
    for n in nomi:
        scrivi_registrazione(imp, n, reg["ponti"][n], porta_sshd, chiavi)
    if nomi:
        ok(f"registrazioni pubblicate (SSH porta {porta_sshd}): i Raspberry aprono il tunnel da soli")


def valida_nome(nome: str) -> str:
    nome = (nome or "").strip().lower()
    if not RE_NOME.match(nome):
        raise Errore("nome del ponte: solo minuscole, numeri e trattini, al massimo 20 caratteri (es. casa)")
    return nome


# --------------------------------------------------------------------------
# Ponti nuovi (dalla 1.10.0): il Raspberry scritto con Raspberry Pi Imager si installa da solo.
# Certificato e canale si caricano una volta sulla pagina e restano qui, letti solo da root; quando un
# ponte nuovo e' approvato, root ne scrive la configurazione in un file che puo' leggere solo l'utente SSH di
# quel ponte, e il Raspberry la scarica con la sua chiave (comando "ponte").
# --------------------------------------------------------------------------

RE_POSIZIONE = re.compile(r"^-?\d{1,3}(?:\.\d+)?,-?\d{1,3}(?:\.\d+)?(?:,-?\d{1,5}(?:\.\d+)?)?$")
RE_NOME_PACCHETTO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,78}\.(?:zip|p12|pfx)$")
RE_NOME_CANALE = re.compile(r"^[A-Za-z0-9_-]{1,11}$")
RE_SECONDARIO = re.compile(r"^[A-Za-z0-9_-]{0,11}$")
RE_HOST_TAK = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
PACCHETTO_MAX = 256 << 10


def valida_posizione(testo: str) -> str:
    """'lat,lon[,quota]' come le copia Google Maps (anche con spazi); '' se non c'e'. Come l'installatore."""
    parti = [x for x in str(testo or "").replace(";", ",").replace(" ", ",").split(",") if x.strip()]
    if not parti:
        return ""
    pulita = ",".join(parti)
    try:
        lat, lon = float(parti[0]), float(parti[1])
    except (ValueError, IndexError):
        raise Errore("coordinate: latitudine e longitudine separate da una virgola, es. 45.8205, 8.8257")
    if (not RE_POSIZIONE.match(pulita) or not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0)):
        raise Errore("coordinate: latitudine e longitudine separate da una virgola, es. 45.8205, 8.8257")
    return pulita


def chiave_canale_valida(chiave: str) -> str:
    """La chiave del canale (PSK) in base64, 16 o 32 byte, come la vuole l'installatore."""
    t = str(chiave or "").strip()
    t = t[7:] if t.lower().startswith("base64:") else t
    try:
        if len(base64.b64decode(t, validate=True)) in (16, 32):
            return t
    except (binascii.Error, ValueError):
        pass
    raise Errore("chiave del canale non valida: serve quella del canale privato, in base64 (16 o 32 byte, "
                 "es. 44 caratteri con = in fondo)")


def openssl(argomenti: List[str], password: str) -> subprocess.CompletedProcess:
    return subprocess.run(["openssl"] + argomenti, capture_output=True, text=True, timeout=60,
                          stdin=subprocess.DEVNULL, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                                         "M2T_PASS": password})


def controlla_pacchetto(dati: bytes, nome_file: str, password: str) -> dict:
    """Il pacchetto d'integrazione della console infra-TAK (o un .p12): si apre, ha certificato e chiave, la
    password li apre. Torna nome e scadenza del certificato, indirizzo e porta del TAK Server se li dice."""
    cartella = P(PONTI_NUOVI_DIR + "/controllo")
    shutil.rmtree(cartella, ignore_errors=True)
    os.makedirs(cartella, mode=0o700)
    try:
        host, porta = "", 8089
        file_dentro: Dict[str, str] = {}
        if nome_file.lower().endswith(".zip"):
            import zipfile
            try:
                z = zipfile.ZipFile(io.BytesIO(dati))
                nomi = [n for n in z.namelist() if not n.endswith("/")]
                if len(nomi) > 40 or sum(i.file_size for i in z.infolist()) > 4 << 20:
                    raise Errore("il pacchetto contiene troppa roba: e' quello della console infra-TAK?")
                for n in nomi:
                    b = os.path.basename(n)
                    if b and re.match(r"^[A-Za-z0-9._ -]{1,100}$", b) and not b.startswith("._"):
                        destinazione = os.path.join(cartella, b)
                        with open(destinazione, "wb") as f:
                            f.write(z.read(n))
                        file_dentro[b] = destinazione
            except (zipfile.BadZipFile, OSError, RuntimeError, ValueError):
                raise Errore("il file .zip non si apre: e' quello scaricato dalla console infra-TAK?")
        else:
            destinazione = os.path.join(cartella, "certificato.p12")
            with open(destinazione, "wb") as f:
                f.write(dati)
            file_dentro["certificato.p12"] = destinazione
        leggimi = file_dentro.get("README.txt")
        if leggimi:
            testo = open(leggimi, encoding="utf-8", errors="replace").read()
            m = re.search(r"^Server:\s*(\S+)", testo, re.M)
            if m and not m.group(1).startswith("<") and RE_HOST_TAK.match(m.group(1)):
                host = m.group(1)
            m = re.search(r"^Port:\s*(\d+)", testo, re.M)
            if m and 0 < int(m.group(1)) < 65536:
                porta = int(m.group(1))
        cert = next((f for b, f in sorted(file_dentro.items()) if b.endswith(".pem") and b not in
                     ("ca-chain.pem", "ca.pem")), None)
        chiave = next((f for b, f in sorted(file_dentro.items()) if b.endswith(".key")), None)
        p12 = next((f for b, f in sorted(file_dentro.items()) if b.endswith((".p12", ".pfx")) and
                    not b.endswith("-legacy.p12") and not b.startswith("truststore-")), None)
        if not ((cert and chiave) or p12):
            raise Errore("nel pacchetto non c'e' ne' il certificato .pem/.key ne' il .p12: e' quello della console "
                         "infra-TAK?")
        risultato = {"host": host, "porta": porta, "certificato": "", "scade": None}
        if not shutil.which("openssl"):
            return risultato                # senza openssl si controlla sul Raspberry, come sempre
        if cert and chiave:
            if openssl(["pkey", "-in", chiave, "-passin", "env:M2T_PASS", "-noout"], password).returncode != 0:
                raise Errore("password del certificato sbagliata (quella mostrata nella console infra-TAK)")
        else:
            esportato = os.path.join(cartella, "cliente.pem")
            for extra in ([], ["-legacy"]):
                r = openssl(["pkcs12"] + extra + ["-in", p12, "-passin", "env:M2T_PASS", "-clcerts", "-nokeys",
                                                  "-out", esportato], password)
                if r.returncode == 0:
                    break
            else:
                raise Errore("password del certificato sbagliata, o file .p12 rovinato")
            cert = esportato
        r = openssl(["x509", "-in", cert, "-noout", "-subject", "-enddate", "-nameopt", "multiline"], password)
        m = re.search(r"commonName\s*=\s*(.+)", r.stdout or "")
        if m:
            risultato["certificato"] = re.sub(r"[^\w.@ -]", "", m.group(1).strip())[:64]
        m = re.search(r"notAfter=(.+)", r.stdout or "")
        if m:
            try:
                risultato["scade"] = int(ssl.cert_time_to_seconds(m.group(1).strip()))
            except ValueError:
                pass
        return risultato
    finally:
        shutil.rmtree(cartella, ignore_errors=True)


def salva_impostazioni_nuovi(lavoro: dict) -> str:
    """Lavoro "impostazioni-nuovi" (da root): controlla e salva certificato e canale per i ponti nuovi."""
    d = lavoro["impostazioni"]
    dati = base64.b64decode(d["pacchetto"])
    password = d["password"] or "atakatak"
    info(f"pacchetto {d['nome_pacchetto']} ({len(dati) // 1024 + 1} kB)")
    c = controlla_pacchetto(dati, d["nome_pacchetto"], password)
    host = c["host"] or d["host"]          # come l'installatore: vale quello scritto nel pacchetto
    if not host:
        raise Errore("il pacchetto non dice l'indirizzo del TAK Server: scrivilo nel modulo (es. takserver.tuodominio.it)")
    if c["certificato"]:
        ok(f"certificato '{c['certificato']}'" + (f", scade il {giorno_it(c['scade'])}" if c["scade"] else ""))
    else:
        avviso("certificato non controllato qui (manca openssl): lo controlla il Raspberry")
    salvate = {"formato": 1, "pacchetto": d["pacchetto"], "nome_pacchetto": d["nome_pacchetto"], "password": password,
               "host": d["host"], "server": f"{host}:{c['porta']}",
               "canale": {"nome": d["canale"], "chiave": d["chiave"], "secondario": d["secondario"]},
               "certificato": c["certificato"], "scade": c["scade"], "aggiornato": int(time.time()), "da": lavoro["da"]}
    os.makedirs(P(PONTI_NUOVI_DIR), exist_ok=True)
    os.chmod(P(PONTI_NUOVI_DIR), 0o700)
    scrivi(P(IMPOSTAZIONI_NUOVI), json.dumps(salvate, ensure_ascii=False) + "\n", 0o600)
    scrivi_riassunto_nuovi(salvate)
    ok(f"TAK Server {host}:{c['porta']}, canale '{d['canale']}' (secondario "
       f"{d['secondario'] or 'LongFast, il pubblico standard'})")
    return (f"Impostazioni salvate: certificato '{c['certificato'] or '?'}', TAK Server {host}:{c['porta']}, "
            f"canale {d['canale']}.")


def scrivi_riassunto_nuovi(s: Optional[dict]) -> None:
    """Per la pagina della rete: cosa c'e' nelle impostazioni dei ponti nuovi, senza chiavi ne' password."""
    if not s:
        togli(P(RIASSUNTO_NUOVI))
        return
    canale = s.get("canale") or {}
    scrivi(P(RIASSUNTO_NUOVI), json.dumps({
        "formato": 1, "pronto": True, "certificato": s.get("certificato") or "", "scade": s.get("scade"),
        "server": s.get("server") or "", "canale": canale.get("nome") or "", "secondario": canale.get("secondario") or "",
        "aggiornato": s.get("aggiornato"), "da": s.get("da") or ""}, ensure_ascii=False) + "\n", 0o644)


def leggi_impostazioni_nuovi() -> Optional[dict]:
    testo = leggi(P(IMPOSTAZIONI_NUOVI))
    try:
        d = json.loads(testo) if testo else None
    except ValueError:
        return None
    return d if isinstance(d, dict) and d.get("pacchetto") else None


def file_configurazione(nome: str) -> str:
    return P(f"{CONFIGURAZIONI_DIR}/{nome}.json")


def prepara_configurazione(imp: Impostazioni, reg: dict, nome: str, installa: dict, da: str) -> int:
    """Scrive la configurazione da installare del ponte nuovo: la legge solo il suo utente SSH (e root). Torna il
    numero del tentativo, che va nella registrazione: il Raspberry lo vede e la scarica."""
    s = leggi_impostazioni_nuovi()
    if not s:
        raise Errore("mancano certificato e canale per i ponti nuovi: caricali sulla pagina della rete")
    p = reg["ponti"][nome]
    prima = p.get("installa") if isinstance(p.get("installa"), dict) else {}
    tentativo = int(prima.get("tentativo") or 0) + 1
    conf = {"formato": 1, "nome": nome, "scheda": installa["scheda"], "posizione": installa.get("posizione") or "",
            "rete": imp.url().rstrip("/"), "versione": VERSIONE, "tentativo": tentativo,
            "tak": {"pacchetto": s["pacchetto"], "nome_pacchetto": s["nome_pacchetto"], "password": s["password"],
                    "host": s.get("host") or ""},
            "canale": s["canale"]}
    prepara_cartelle_ponti()
    f = file_configurazione(nome)
    togli(f)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    try:
        os.write(fd, (json.dumps(conf, ensure_ascii=False) + "\n").encode("utf-8"))
        try:
            os.fchown(fd, pwd.getpwnam(utente_ponte(nome)).pw_uid, 0)
        except KeyError:
            if not RADICE:
                raise Errore(f"manca l'utente {utente_ponte(nome)}")
        os.fchmod(fd, 0o400)
    finally:
        os.close(fd)
    p["installa"] = {"tentativo": tentativo, "chiesto": int(time.time()), "scheda": installa["scheda"],
                     "posizione": installa.get("posizione") or "", "da": da}
    ok(f"configurazione per il ponte nuovo pronta (tentativo {tentativo}, radio {installa['scheda']}): la legge "
       f"solo il Raspberry di {nome}")
    return tentativo


def riprova_installazione(imp: Impostazioni, reg: dict, lavoro: dict) -> None:
    """Lavoro "installa" (da root): la configurazione di nuovo, con le scelte nuove o quelle di prima."""
    nome = lavoro["nome"]
    if nome not in reg["ponti"]:
        raise Errore("ponte non trovato: ricarica la pagina")
    prima = reg["ponti"][nome].get("installa")
    if not isinstance(prima, dict):
        raise Errore(f"il ponte {nome} non e' stato installato dalla pagina: da qui non si reinstalla")
    installa = {"scheda": lavoro["installa"].get("scheda") or prima.get("scheda") or "usb",
                "posizione": (lavoro["installa"]["posizione"] if "posizione" in lavoro["installa"]
                              else prima.get("posizione") or "")}
    prepara_configurazione(imp, reg, nome, installa, lavoro["da"])
    salva_registro(reg)
    pubblica_registrazioni(imp, reg, [nome])
    ok(f"il Raspberry di {nome} riprova l'installazione entro un paio di minuti")


def aggiungi_ponte(imp: Impostazioni, reg: dict, nome: str, chiave: str, impronta: str, ignora_dns: bool = False,
                   cambio_chiave: bool = True, installa: Optional[dict] = None, da: str = "") -> None:
    """Registra (o aggiorna) un ponte: DNS, utente SSH, pagina, Caddy, Authentik, registrazione. Con installa
    (ponte nuovo dalla 1.10.0) prepara anche la configurazione che il Raspberry scarica per installarsi."""
    if installa is not None and not leggi_impostazioni_nuovi():
        raise Errore("mancano certificato e canale per i ponti nuovi: caricali sulla pagina della rete")
    reg["dominio"] = imp.dominio
    nuovo = nome not in reg["ponti"]
    passo(f"1/5  Indirizzi: {imp.url()} e {imp.url(nome)}")
    controlla_dns(imp, [imp.host(), imp.host(nome)], ignora_dns)
    for altro, p in reg["ponti"].items():
        if altro != nome and p.get("impronta") == impronta:
            raise Errore(f"questa chiave e' gia' del ponte '{altro}': ogni Raspberry ha la sua")
    ora = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    if nuovo:
        usate = {p["porta"] for p in reg["ponti"].values()}
        occupate = porte_in_ascolto()
        porta = PORTA_BASE
        while porta in usate or porta in occupate:
            porta += 1
        reg["ponti"][nome] = {"porta": porta, "chiave": chiave, "impronta": impronta, "aggiunto": ora}
    else:
        p = reg["ponti"][nome]
        if p["impronta"] != impronta:
            if not cambio_chiave:
                raise Errore(f"il ponte '{nome}' c'e' gia' con un'altra chiave (un altro Raspberry, o lo stesso "
                             "reinstallato): sulla pagina della rete usa 'Sostituisci'")
            p.setdefault("impronte_vecchie", []).append(p["impronta"])
            info(f"il ponte '{nome}' cambia chiave (Raspberry reinstallato?): quella vecchia non vale piu'")
        p.update({"chiave": chiave, "impronta": impronta, "aggiornato": ora})
    passo("2/5  Utente del ponte sul server (SSH, solo tunnel)")
    prepara_ponti(imp, reg, [nome])
    passo("3/5  Caddy")
    configura_caddy(imp, reg)
    passo("4/5  Authentik (solo amministratori)")
    configura_authentik(imp, reg)
    # solo adesso, a tutto pronto: se un passo prima non riesce il ponte non risulta aggiunto e si riprova
    passo("5/5  Pagina della rete e registrazione per il Raspberry")
    if installa is not None:
        prepara_configurazione(imp, reg, nome, installa, da)
    salva_registro(reg)
    scrivi_sito(imp, reg)
    pubblica_registrazioni(imp, reg, [nome])


def rimuovi_ponte(imp: Impostazioni, reg: dict, nome: str) -> None:
    if nome not in reg["ponti"]:
        raise Errore(f"il ponte '{nome}' non e' registrato")
    p = reg["ponti"].pop(nome)
    utente = utente_ponte(nome)
    togli(P(f"{ACCESSO_DIR}/{p['impronta']}.json"))
    togli(P(f"{CHIAVI_DIR}/{utente}"))
    togli(file_configurazione(nome))                     # certificato e canale non piu' consegnabili
    togli(P(f"{AVANZAMENTO_DIR}/{nome}.json"))
    chiudi_sessioni(utente)
    try:
        pwd.getpwnam(utente)
        esegui(["userdel", utente], controlla=False)
    except KeyError:
        pass
    salva_registro(reg)
    passo("Pagina e Caddy")
    scrivi_sito(imp, reg)
    configura_caddy(imp, reg)
    passo("Authentik")
    configura_authentik(imp, reg, {nome: reg["authentik"]["ponti"].get(nome, {})})
    salva_registro(reg)
    ok(f"ponte '{nome}' tolto dalla rete")


# ---- servizio della pagina (senza privilegi) e passo dei lavori (root) ----

def testo_unita_web() -> str:
    return f"""[Unit]
Description=Rete Meshtastic: servizio della pagina della rete (mesh2tak, senza privilegi)
After=network.target

[Service]
User={UTENTE_WEB}
Group={UTENTE_WEB}
ExecStart=/usr/bin/python3 -I {OPT_SCRIPT} servi
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
CapabilityBoundingSet=
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictNamespaces=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
LockPersonality=true
ReadWritePaths={SERVIZIO_DIR}
MemoryMax=128M

[Install]
WantedBy=multi-user.target
"""


def testo_unita_lavori() -> str:
    return f"""[Unit]
Description=Rete Meshtastic: esegue i lavori chiesti dalla pagina della rete (aggiungi o togli un ponte)

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -I {OPT_SCRIPT} lavora
TimeoutStartSec=45min
"""


def testo_unita_percorso() -> str:
    return f"""[Unit]
Description=Rete Meshtastic: guarda la coda dei lavori della pagina della rete

[Path]
DirectoryNotEmpty={LAVORI_DIR}
Unit=mesh2tak-rete-lavori.service

[Install]
WantedBy=paths.target
"""


def installa_servizio_web() -> None:
    """Utente senza privilegi, cartelle, copia dello script in /opt e servizi di sistema."""
    try:
        pw = pwd.getpwnam(UTENTE_WEB)
    except KeyError:
        esegui(["useradd", "--system", "--no-create-home", "--home-dir", "/nonexistent", "--shell",
                "/usr/sbin/nologin", "--user-group", "--comment", "Rete Meshtastic, pagina della rete (mesh2tak)",
                UTENTE_WEB])
        try:
            pw = pwd.getpwnam(UTENTE_WEB)
        except KeyError:
            if not RADICE:
                raise Errore(f"utente {UTENTE_WEB} non creato")
            pw = None   # prove con useradd finto
    for d, modo, proprio in ((WEB_DIR, 0o755, False), (SERVIZIO_DIR, 0o700, True), (RICHIESTE_DIR, 0o700, True),
                             (LAVORI_DIR, 0o700, True), (TMP_DIR, 0o700, True), (ESITI_DIR, 0o755, False),
                             (INVITI_DIR, 0o700, True), (INSTALLAZIONI_DIR, 0o700, True), (OPT_DIR, 0o755, False)):
        os.makedirs(P(d), exist_ok=True)
        os.chmod(P(d), modo)
        if proprio and pw is not None:
            os.chown(P(d), pw.pw_uid, pw.pw_gid)
    questo = os.path.abspath(__file__)
    nuovo = open(questo, encoding="utf-8").read()
    cambiato = False
    if os.path.realpath(questo) != os.path.realpath(P(OPT_SCRIPT)):
        cambiato = scrivi(P(OPT_SCRIPT), nuovo, 0o755)
    for percorso, testo in ((UNITA_WEB, testo_unita_web()), (UNITA_LAVORI, testo_unita_lavori()),
                            (UNITA_PERCORSO, testo_unita_percorso())):
        cambiato = scrivi(P(percorso), testo, 0o644) or cambiato
    esegui(["systemctl", "daemon-reload"], attesa=60)
    esegui(["systemctl", "enable", "--now", "mesh2tak-rete-web.service", "mesh2tak-rete-lavori.path"], attesa=60)
    if cambiato:
        esegui(["systemctl", "restart", "mesh2tak-rete-web.service"], attesa=60)
    r = esegui(["systemctl", "is-active", "mesh2tak-rete-web.service"], controlla=False, attesa=20)
    if r.returncode == 0:
        ok("servizio della pagina attivo: dalla pagina si aggiungono e tolgono i ponti")
    else:
        avviso("servizio della pagina non attivo: guarda  journalctl -u mesh2tak-rete-web")


def togli_servizio_web() -> None:
    esegui(["systemctl", "disable", "--now", "mesh2tak-rete-web.service", "mesh2tak-rete-lavori.path"],
           controlla=False, attesa=60)
    for f in (UNITA_WEB, UNITA_LAVORI, UNITA_PERCORSO):
        togli(P(f))
    esegui(["systemctl", "daemon-reload"], controlla=False, attesa=60)
    shutil.rmtree(P(OPT_DIR), ignore_errors=True)
    try:
        pwd.getpwnam(UTENTE_WEB)
        esegui(["userdel", UTENTE_WEB], controlla=False)
    except KeyError:
        pass


def installa_rete(imp: Impostazioni, reg: dict, ignora_dns: bool) -> None:
    """La pagina della rete con tutto quello che serve, anche senza ponti."""
    reg["dominio"] = imp.dominio
    passo(f"1/6  Indirizzo della pagina: {imp.url()}")
    controlla_dns(imp, [imp.host()] + [imp.host(n) for n in sorted(reg["ponti"])], ignora_dns)
    passo("2/6  Utenti dei ponti (SSH, solo tunnel)")
    prepara_ponti(imp, reg, sorted(reg["ponti"]))
    passo("3/6  Servizio della pagina (senza privilegi)")
    installa_servizio_web()
    passo("4/6  Caddy")
    configura_caddy(imp, reg)
    passo("5/6  Authentik (solo amministratori)")
    configura_authentik(imp, reg)
    passo("6/6  Pagina della rete e registrazioni per i Raspberry")
    salva_registro(reg)
    scrivi_sito(imp, reg)
    ok(f"pagina pronta in {SITO_DIR}")
    pubblica_registrazioni(imp, reg, sorted(reg["ponti"]))


def prepara_avvisi(imp: Impostazioni) -> None:
    """Dice dove arriveranno gli avvisi e, la prima volta, manda un'email di prova."""
    destinatari = re.sub(r"\s+", " ", str(imp.dati.get("guarddog_alert_email") or "")).strip()
    if not destinatari:
        avviso("in infra-TAK non c'e' un indirizzo per gli avvisi: impostalo nella console (Guard Dog), poi sulla "
               "pagina della rete premi 'Manda un'email di prova'")
        return
    ok(f"avvisi per email a: {destinatari} (gli indirizzi degli avvisi di Guard Dog in infra-TAK)")
    if os.path.exists(P(AVVISI_PROVATI)):
        return
    esito, dettaglio = Sorveglianza.invia(porta_console(imp), "[Rete Meshtastic] Email di prova",
                                          "Gli avvisi della rete Meshtastic sono attivi: ti scrivo quando un ponte non "
                                          "risponde da 10 minuti, perde la radio o il TAK Server, quando il suo "
                                          "certificato sta per scadere, quando la corrente non basta e quando un "
                                          f"Raspberry si riavvia.\nPagina della rete: {imp.url()}")
    if esito == "mandato":
        scrivi(P(AVVISI_PROVATI), time.strftime("%Y-%m-%d %H:%M:%S") + "\n", 0o600)
        ok("email di prova mandata: controlla che sia arrivata (anche nello spam)")
    else:
        avviso(f"email di prova non partita: {dettaglio}. Riprova dalla pagina della rete")


def cmd_installa(a) -> int:
    imp = trova_impostazioni()
    with Blocco():
        reg = carica_registro()
        installa_rete(imp, reg, a.ignora_dns)
    prepara_avvisi(imp)
    print(f"\n{B}Fatto.{N} Pagina della rete: {imp.url()}  (in Authentik: applicazione 'Rete Meshtastic')")
    info("Da ora i ponti si aggiungono dalla pagina: sul Raspberry")
    info("    sudo bash installa-ponte-meshtastic.sh --accesso-esterno")
    info("e sulla pagina approvi la sua richiesta. Qui sul server non serve altro.")
    return 0


def cmd_aggiungi(a) -> int:
    nome = valida_nome(a.nome)
    chiave, impronta = leggi_chiave(a.chiave)
    imp = trova_impostazioni()
    with Blocco():
        reg = carica_registro()
        aggiungi_ponte(imp, reg, nome, chiave, impronta, a.ignora_dns)
        passo("Servizio della pagina")
        installa_servizio_web()
    print(f"\n{B}Fatto.{N}")
    info(f"Entro un paio di minuti il Raspberry apre il tunnel e il ponte '{nome}' compare su:")
    info(f"    {imp.url()}   (in Authentik: applicazione 'Rete Meshtastic')")
    info(f"Gestione del suo nodo: {imp.url(nome)}  (mentre la usi il ponte va in pausa)")
    info("Sul Raspberry lo stato del tunnel:  sudo mesh2tak-accesso")
    return 0


def cmd_elenco(a) -> int:
    imp = trova_impostazioni()
    reg = carica_registro()
    print(f"{B}Rete Meshtastic{N}: {imp.url()}  (versione dello script {VERSIONE})")
    if not reg["ponti"]:
        info("nessun ponte registrato: sul Raspberry  sudo bash installa-ponte-meshtastic.sh --accesso-esterno")
        info("e poi approva la richiesta sulla pagina della rete")
        return 0
    ascolto = porte_in_ascolto()
    for n, p in sorted(reg["ponti"].items()):
        collegato = p["porta"] in ascolto
        stato = f"{V}tunnel collegato{N}" if collegato else f"{G}tunnel non collegato{N}"
        print(f"  {n:<20} {imp.url(n):<45} porta {p['porta']}  {stato}")
    return 0


def cmd_rimuovi(a) -> int:
    nome = valida_nome(a.nome)
    imp = trova_impostazioni()
    with Blocco():
        reg = carica_registro()
        if nome not in reg["ponti"]:
            raise Errore(f"il ponte '{nome}' non e' registrato (vedi: sudo bash {sys.argv[0]} elenco)")
        conferma(f"Tolgo il ponte '{nome}' ({imp.url(nome)}).", nome, a.si)
        rimuovi_ponte(imp, reg, nome)
    print(f"\n{B}Fatto.{N} Ponte '{nome}' tolto. Sul Raspberry, per spegnere anche il tunnel:")
    info("sudo bash installa-ponte-meshtastic.sh --accesso-esterno=no")
    return 0


def cmd_aggiorna(a) -> int:
    imp = trova_impostazioni()
    with Blocco():
        reg = carica_registro()
        installa_rete(imp, reg, getattr(a, "ignora_dns", False))
    print(f"\n{B}Fatto.{N} {imp.url()}")
    return 0


def cmd_disinstalla(a) -> int:
    imp = trova_impostazioni()
    with Blocco():
        reg = carica_registro()
        conferma(f"Tolgo la pagina {imp.url()}, i {len(reg['ponti'])} ponti registrati e le loro voci in Caddy, "
                 "Authentik e sshd.", "disinstalla", a.si)
        passo("1/5  Servizio della pagina")
        togli_servizio_web()
        ok("servizio della pagina tolto")
        passo("2/5  Authentik")
        try:
            togli_authentik(reg)
        except Errore as e:
            avviso(f"Authentik: {e} (le applicazioni 'Rete Meshtastic' e 'Meshtastic ...' toglile a mano)")
        passo("3/5  Caddy")
        configura_caddy(imp, reg, con=False)
        passo("4/5  Utenti dei ponti e sshd")
        for n in list(reg["ponti"]):
            utente = utente_ponte(n)
            chiudi_sessioni(utente)
            try:
                pwd.getpwnam(utente)
                esegui(["userdel", utente], controlla=False)
            except KeyError:
                pass
        if togli(P(SSHD_DROPIN)):
            r = esegui(["sshd", "-t"], controlla=False)
            if r.returncode == 0:
                ricarica_ssh()
        try:
            grp.getgrnam(GRUPPO)
            esegui(["groupdel", GRUPPO], controlla=False)
        except KeyError:
            pass
        ok("utenti, gruppo e regole sshd dei ponti tolti")
        passo("5/5  File")
        shutil.rmtree(P(WEB_DIR), ignore_errors=True)
        shutil.rmtree(P(CADDY_DIR), ignore_errors=True)
        shutil.rmtree(P(CONFIGURAZIONI_DIR), ignore_errors=True)   # dalla 1.10.0: certificato e canale
        shutil.rmtree(P(PONTI_NUOVI_DIR), ignore_errors=True)
        for f in glob.glob(P(CHIAVI_DIR) + "/*"):
            togli(f)
        togli(P(REGISTRO))
        togli(P(AVVISI_PROVATI))
        ok(f"tolti {WEB_DIR}, {CADDY_DIR} e il registro (resta {REGISTRO_LOG})")
    print(f"\n{B}Fatto.{N} Sui Raspberry spegni l'accesso:  sudo bash installa-ponte-meshtastic.sh --accesso-esterno=no")
    return 0


# ---- passo dei lavori (root): esegue solo "aggiungi" e "rimuovi", ognuno controllato da capo ----

RE_ID = re.compile(r"^[0-9a-f]{16}$")
RE_FILE_LAVORO = re.compile(r"^[0-9a-f]{16}\.json$")
RE_IMPRONTA = re.compile(r"^[0-9a-f]{64}$")
RE_UTENTE_AK = re.compile(r"^[\w.@+-]{1,64}$")


LAVORO_MAX = 16384
LAVORO_IMPOSTAZIONI_MAX = 512 << 10      # certificato dei ponti nuovi (base64)


def leggi_installa(d) -> Optional[dict]:
    """Scheda radio e posizione per installare un ponte nuovo (dalla 1.10.0)."""
    if not isinstance(d, dict) or str(d.get("scheda") or "") not in SCHEDE:
        return None
    try:
        return {"scheda": d["scheda"], "posizione": valida_posizione(str(d.get("posizione") or ""))}
    except Errore:
        return None


def controlla_impostazioni(d) -> dict:
    """Certificato e canale per i ponti nuovi, come li manda la pagina: ogni campo controllato (Errore col perche')."""
    if not isinstance(d, dict):
        raise Errore("richiesta non valida")
    nome_pacchetto = os.path.basename(str(d.get("nome_pacchetto") or "").replace("\\", "/")).strip()
    pacchetto = str(d.get("pacchetto") or "")
    password = str(d.get("password") or "")
    host = str(d.get("host") or "").strip()
    canale = str(d.get("canale") or "").strip()
    secondario = str(d.get("secondario") or "").strip()
    if not RE_NOME_PACCHETTO.match(nome_pacchetto):
        raise Errore("serve il pacchetto .zip scaricato dalla console infra-TAK (o un .p12)")
    try:
        grezzo = base64.b64decode(pacchetto, validate=True)
    except (binascii.Error, ValueError):
        raise Errore("file del certificato non valido: riprova a sceglierlo")
    if not 0 < len(grezzo) <= PACCHETTO_MAX:
        raise Errore("il file del certificato e' troppo grande: serve il pacchetto della console infra-TAK")
    if len(password) > 128 or not password.isprintable():
        raise Errore("password del certificato non valida")
    if host and not RE_HOST_TAK.match(host):
        raise Errore("indirizzo del TAK Server non valido (es. takserver.tuodominio.it)")
    if not RE_NOME_CANALE.match(canale):
        raise Errore("nome del canale: lettere, numeri, - e _, al massimo 11 caratteri (es. CNSAS-VA)")
    if not RE_SECONDARIO.match(secondario):
        raise Errore("nome del canale pubblico: lettere, numeri, - e _, al massimo 11 caratteri (vuoto = LongFast)")
    return {"nome_pacchetto": nome_pacchetto, "pacchetto": pacchetto, "password": password, "host": host,
            "canale": canale, "chiave": chiave_canale_valida(str(d.get("chiave") or "")), "secondario": secondario}


def leggi_impostazioni_lavoro(d) -> Optional[dict]:
    try:
        return controlla_impostazioni(d)
    except Errore:
        return None


def leggi_lavoro(percorso: str) -> Optional[dict]:
    """Un lavoro scritto dal servizio della pagina: non ci si fida di nulla (niente collegamenti simbolici,
    file piccolo, solo i campi previsti)."""
    try:
        fd = os.open(percorso, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not (st.st_mode & 0o170000 == 0o100000) or st.st_size > LAVORO_IMPOSTAZIONI_MAX:
            return None
        grezzo = b""
        while len(grezzo) <= LAVORO_IMPOSTAZIONI_MAX:
            pezzo = os.read(fd, 65536)
            if not pezzo:
                break
            grezzo += pezzo
        dati = json.loads(grezzo.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    finally:
        os.close(fd)
    if not isinstance(dati, dict) or not RE_ID.match(str(dati.get("id", ""))):
        return None
    azione = dati.get("azione")
    da = str(dati.get("da") or "")
    if (azione not in ("aggiungi", "rimuovi", "aggiorna-server", "aggiorna-ponti", "impostazioni-nuovi", "installa")
            or not RE_UTENTE_AK.match(da)):
        return None
    if azione != "impostazioni-nuovi" and len(grezzo) > LAVORO_MAX:
        return None
    if azione == "impostazioni-nuovi":
        impostazioni = leggi_impostazioni_lavoro(dati.get("impostazioni"))
        if impostazioni is None:
            return None
        return {"id": dati["id"], "azione": azione, "nome": "ponti-nuovi", "da": da, "impostazioni": impostazioni,
                "richiesta": None, "sostituisci": False}
    if azione == "installa":
        d = dati.get("installa") if isinstance(dati.get("installa"), dict) else {}
        installa: dict = {}
        if d.get("scheda"):
            if d["scheda"] not in SCHEDE:
                return None
            installa["scheda"] = d["scheda"]
        if "posizione" in d:
            try:
                installa["posizione"] = valida_posizione(str(d.get("posizione") or ""))
            except Errore:
                return None
        nome = str(dati.get("nome") or "")
        if not RE_NOME.match(nome):
            return None
        return {"id": dati["id"], "azione": azione, "nome": nome, "da": da, "installa": installa, "richiesta": None,
                "sostituisci": False}
    if azione.startswith("aggiorna-"):
        versione = str(dati.get("versione") or "")
        nome = str(dati.get("nome") or "")
        if not RE_VERSIONE_RILASCIO.match(versione):
            return None
        if (azione == "aggiorna-server" and nome != "server") or (azione == "aggiorna-ponti" and nome != "tutti"
                                                                   and not RE_NOME.match(nome)):
            return None
        return {"id": dati["id"], "azione": azione, "nome": nome, "da": da, "versione": versione, "richiesta": None,
                "sostituisci": False}
    try:
        lavoro = {"id": dati["id"], "azione": azione, "nome": valida_nome(str(dati.get("nome", ""))), "da": da,
                  "sostituisci": dati.get("sostituisci") is True}
        if azione == "aggiungi":
            lavoro["chiave"], lavoro["impronta"] = leggi_chiave(str(dati.get("chiave", "")))
            if dati.get("installa") is not None:
                lavoro["installa"] = leggi_installa(dati.get("installa"))
                if lavoro["installa"] is None:
                    return None
    except Errore:
        return None
    richiesta = str(dati.get("richiesta") or "")
    lavoro["richiesta"] = richiesta if RE_IMPRONTA.match(richiesta) else None
    return lavoro


def scrivi_esito(lavoro: dict, esito: str, messaggio: str, testo: str = "") -> None:
    os.makedirs(P(ESITI_DIR), exist_ok=True)
    pulito = re.sub(r"\x1b\[[0-9;]*m", "", testo)
    dati = {"id": lavoro["id"], "azione": lavoro["azione"], "nome": lavoro["nome"], "da": lavoro["da"],
            "richiesta": lavoro.get("richiesta"), "versione": lavoro.get("versione"), "esito": esito,
            "messaggio": messaggio[:600], "testo": pulito[-4000:], "quando": int(time.time())}
    scrivi(P(f"{ESITI_DIR}/{lavoro['id']}.json"), json.dumps(dati, ensure_ascii=False) + "\n", 0o644)


CONFIGURAZIONE_SCADE_S = 30 * 24 * 3600


def pulisci_configurazioni() -> None:
    """Configurazioni dei ponti nuovi mai scaricate fino in fondo (certificato e canale dentro): dopo 30 giorni
    via. Quelle dei ponti tolti le toglie gia' "togli dalla rete"."""
    for f in glob.glob(P(CONFIGURAZIONI_DIR) + "/*.json"):
        try:
            if time.time() - os.lstat(f).st_mtime > CONFIGURAZIONE_SCADE_S:
                togli(f)
                registra(f"configurazione scaduta tolta: {os.path.basename(f)}")
        except OSError:
            pass


def numeri_versione(v: str) -> Tuple[int, ...]:
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return (0,)                     # una versione di prova, non costruita: piu' vecchia di tutte


def scarica_rilascio(versione: str, nome_file: str, massimo: int = 16 << 20) -> bytes:
    """Un file della release ufficiale su GitHub, controllato con lo SHA256SUMS della stessa release."""
    base = f"{GITHUB}/{REPO}/releases/download/v{versione}"

    def prendi(url: str, limite: int) -> bytes:
        apri = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with apri.open(urllib.request.Request(url, headers={"User-Agent": f"mesh2tak-rete/{VERSIONE}"}),
                           timeout=60) as r:
                dati = r.read(limite + 1)
        except urllib.error.HTTPError as e:
            raise Errore(f"GitHub risponde {e.code} per {url}")
        except (urllib.error.URLError, OSError) as e:
            raise Errore(f"non riesco a scaricare {url} ({getattr(e, 'reason', e)})")
        if len(dati) > limite:
            raise Errore(f"{url}: file troppo grande")
        return dati
    somme = prendi(f"{base}/SHA256SUMS", 65536).decode("utf-8", "replace")
    dati = prendi(f"{base}/{nome_file}", massimo)
    attesa = None
    for riga in somme.splitlines():
        parti = riga.split()
        if len(parti) == 2 and parti[1].lstrip("*") == nome_file:
            attesa = parti[0].lower()
    if not attesa or hashlib.sha256(dati).hexdigest() != attesa:
        raise Errore("il file scaricato non e' quello pubblicato nella release: non lo installo")
    return dati


ATTESA_SERVIZIO_S = float(os.environ.get("RETE_PROVA_ATTESA_SERVIZIO", "90"))


def ultima_riga(testo: str) -> str:
    """La riga dell'errore (comincia con ✘), altrimenti l'ultima."""
    righe = [r.strip() for r in re.sub(r"\x1b\[[0-9;]*m", "", testo).strip().splitlines() if r.strip()]
    utili = [r for r in righe if r.startswith("✘")]
    return (utili or righe or ["nessun messaggio"])[-1].strip(" ✘!")[:300]


def servizio_risponde(versione: str) -> bool:
    """Il servizio della pagina e' ripartito con quella versione."""
    fine = time.time() + ATTESA_SERVIZIO_S
    while True:
        try:
            apri = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            rq = urllib.request.Request(f"http://127.0.0.1:{PORTA_WEB}/api/stato",
                                        headers={"X-Authentik-Username": "aggiornamento"})
            with apri.open(rq, timeout=10) as r:
                if json.loads(r.read(1 << 20)).get("versione") == versione:
                    return True
        except (OSError, ValueError, urllib.error.URLError):
            pass
        if time.time() >= fine:
            return False
        time.sleep(2)


def aggiorna_server(lavoro: dict) -> str:
    """Scarica dalla release su GitHub la versione chiesta, la installa con il suo 'installa', controlla che il
    servizio della pagina riparta con quella versione; altrimenti rimette questa. Da root, fuori dal blocco."""
    versione = lavoro["versione"]
    if numeri_versione(versione) <= numeri_versione(VERSIONE):
        raise Errore(f"il server ha gia' la {VERSIONE}: niente da fare")
    script = scarica_rilascio(versione, "rete-meshtastic.sh")
    if f'VERSIONE = "{versione}"'.encode() not in script:
        raise Errore(f"il file scaricato non e' la versione {versione}: non lo installo")
    nuovo = P(f"{CONF_DIR}/rete-meshtastic-{versione}.sh")
    scrivi(nuovo, script.decode("utf-8"), 0o700)
    precedente = P(f"{CONF_DIR}/rete_meshtastic-precedente.py")
    if os.path.exists(P(OPT_SCRIPT)):
        shutil.copyfile(P(OPT_SCRIPT), precedente)
        os.chmod(precedente, 0o700)
    ambiente = {k: v for k, v in os.environ.items() if k != "RETE_COLORI"}

    def lancia(comando: List[str]) -> Tuple[int, str]:
        try:
            r = subprocess.run(comando, capture_output=True, text=True, timeout=900, env=ambiente,
                               stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return 124, "✘ l'installazione non ha finito in 15 minuti"
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    try:
        esito, uscita = lancia(["bash", nuovo, "installa"])
    finally:
        togli(nuovo)
    if esito == 0 and servizio_risponde(versione):
        return uscita + f"\nServer aggiornato dalla {VERSIONE} alla {versione}."
    motivo = (f"l'installazione della {versione} non e' riuscita ({ultima_riga(uscita)})" if esito != 0
              else f"con la {versione} il servizio della pagina non riparte")
    if not os.path.exists(precedente):
        raise ErroreConTesto(f"{motivo}; manca la {VERSIONE} per tornare indietro", uscita)
    e2, t2 = lancia([sys.executable, "-I", precedente, "installa"])
    uscita += "\n--- ritorno alla versione di prima ---\n" + t2
    if e2 == 0 and servizio_risponde(VERSIONE):
        raise ErroreConTesto(f"{motivo}: rimessa la {VERSIONE}", uscita)
    raise ErroreConTesto(f"{motivo}; anche il ritorno alla {VERSIONE} non e' riuscito", uscita)


class ErroreConTesto(Errore):
    def __init__(self, messaggio: str, testo: str):
        super().__init__(messaggio)
        self.testo = testo


def chiedi_aggiornamento_ponti(imp: Impostazioni, reg: dict, lavoro: dict) -> None:
    """Scrive la richiesta nella registrazione dei ponti: ognuno la vede entro 2 minuti e si aggiorna da solo."""
    nomi = sorted(reg["ponti"]) if lavoro["nome"] == "tutti" else [lavoro["nome"]]
    if not nomi or any(n not in reg["ponti"] for n in nomi):
        raise Errore("ponte non trovato: ricarica la pagina")
    ora = int(time.time())
    for n in nomi:
        reg["ponti"][n]["aggiorna"] = {"versione": lavoro["versione"], "chiesto": ora}
    salva_registro(reg)
    pubblica_registrazioni(imp, reg, nomi)
    ok(f"chiesto l'aggiornamento alla {lavoro['versione']} a: {', '.join(nomi)}")


def cmd_lavora(a) -> int:
    imp = trova_impostazioni()
    cartella = P(LAVORI_DIR)
    server_da_aggiornare: List[dict] = []
    with Blocco(attendi=True):
        for nome_file in sorted(os.listdir(cartella)) if os.path.isdir(cartella) else []:
            percorso = os.path.join(cartella, nome_file)
            lavoro = leggi_lavoro(percorso) if RE_FILE_LAVORO.match(nome_file) else None
            try:
                togli(percorso)
            except IsADirectoryError:
                shutil.rmtree(percorso, ignore_errors=True)
            if lavoro is None:
                registra(f"lavoro scartato (non valido): {nome_file!r}")
                continue
            registra(f"lavoro {lavoro['id']}: {lavoro['azione']} {lavoro['nome']} chiesto da {lavoro['da']}")
            if lavoro["azione"] == "aggiorna-server":       # dopo, senza il blocco: lo prende 'installa'
                server_da_aggiornare.append(lavoro)
                scrivi_esito(lavoro, "in_corso", "in corso")
                continue
            scrivi_esito(lavoro, "in_corso", "in corso")
            uscita = io.StringIO()
            try:
                messaggio = "fatto"
                with contextlib.redirect_stdout(uscita):
                    reg = carica_registro()
                    if lavoro["azione"] == "aggiungi":
                        aggiungi_ponte(imp, reg, lavoro["nome"], lavoro["chiave"], lavoro["impronta"],
                                       cambio_chiave=lavoro["sostituisci"], installa=lavoro.get("installa"),
                                       da=lavoro["da"])
                    elif lavoro["azione"] == "aggiorna-ponti":
                        chiedi_aggiornamento_ponti(imp, reg, lavoro)
                    elif lavoro["azione"] == "impostazioni-nuovi":
                        messaggio = salva_impostazioni_nuovi(lavoro)
                    elif lavoro["azione"] == "installa":
                        riprova_installazione(imp, reg, lavoro)
                    else:
                        rimuovi_ponte(imp, reg, lavoro["nome"])
                scrivi_esito(lavoro, "ok", messaggio, uscita.getvalue())
            except Errore as e:
                registra(f"lavoro {lavoro['id']}: ERRORE {e}")
                scrivi_esito(lavoro, "errore", str(e), uscita.getvalue())
            except Exception as e:  # noqa: un errore inatteso non deve fermare gli altri lavori
                registra(f"lavoro {lavoro['id']}: ERRORE INATTESO {e!r}")
                scrivi_esito(lavoro, "errore", f"errore inatteso: {e}", uscita.getvalue())
        # esiti vecchi: bastano gli ultimi
        esiti = sorted(glob.glob(P(ESITI_DIR) + "/*.json"), key=lambda f: os.path.getmtime(f))
        for f in esiti[:-30]:
            togli(f)
        pulisci_configurazioni()
    for lavoro in server_da_aggiornare[-1:]:           # piu' richieste insieme: basta l'ultima
        try:
            testo = aggiorna_server(lavoro)
            scrivi_esito(lavoro, "ok", f"server aggiornato alla {lavoro['versione']}", testo)
            registra(f"lavoro {lavoro['id']}: server aggiornato alla {lavoro['versione']}")
        except ErroreConTesto as e:
            registra(f"lavoro {lavoro['id']}: ERRORE {e}")
            scrivi_esito(lavoro, "errore", str(e), e.testo)
        except Errore as e:
            registra(f"lavoro {lavoro['id']}: ERRORE {e}")
            scrivi_esito(lavoro, "errore", str(e))
        except Exception as e:  # noqa
            registra(f"lavoro {lavoro['id']}: ERRORE INATTESO {e!r}")
            scrivi_esito(lavoro, "errore", f"errore inatteso: {e}")
    for lavoro in server_da_aggiornare[:-1]:
        scrivi_esito(lavoro, "ok", "sostituito da una richiesta piu' recente")
    return 0


# ---- comando dei ponti via SSH (dalla 1.10.0): lo lancia sshd, con l'utente del ponte, senza terminale ----

RE_FASE = re.compile(r"^(attesa|configurazione|scarico|installo|riavvio|fatto|errore)$")


def scrivi_avanzamento(nome: str, fase: str, testo: str) -> None:
    """A che punto e' l'installazione del ponte: lo legge la pagina della rete. Scritto dall'utente del ponte nella
    cartella con il bit sticky: un ponte scrive solo il suo file."""
    testo = re.sub(r"[\x00-\x1f\x7f]+", " ", testo).strip()[:300]
    finale = P(f"{AVANZAMENTO_DIR}/{nome}.json")
    nuovo = finale + ".nuovo"
    try:
        os.unlink(nuovo)
    except OSError:
        pass
    fd = os.open(nuovo, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        os.write(fd, json.dumps({"formato": 1, "fase": fase, "testo": testo, "quando": int(time.time())},
                                ensure_ascii=False).encode("utf-8"))
        os.fchmod(fd, 0o644)
    finally:
        os.close(fd)
    os.replace(nuovo, finale)


def cmd_ponte(a) -> int:
    """Il comando forzato degli utenti dei ponti. Il Raspberry chiede:
         configurazione               la configurazione da installare (certificato, canale, scheda), se c'e'
         avanzamento FASE TESTO       a che punto e' l'installazione; con FASE "fatto" la configurazione si toglie
    Qualunque altra cosa: rifiutata. Il ponte e' quello dell'utente SSH, non quello che dice il Raspberry."""
    utente = os.environ.get("RETE_PROVA_UTENTE", "") if RADICE else ""
    if not utente:
        try:
            utente = pwd.getpwuid(os.getuid()).pw_name
        except KeyError:
            utente = ""
    nome = utente[len("ponte-"):] if utente.startswith("ponte-") else ""
    if not RE_NOME.match(nome):
        print("Questo comando e' solo per i ponti della rete Meshtastic.", file=sys.stderr)
        return 1
    parti = os.environ.get("SSH_ORIGINAL_COMMAND", "").split(None, 2)
    if not parti:
        print("Questo utente serve solo al tunnel del ponte: niente terminale.", file=sys.stderr)
        return 1
    try:
        if parti[0] == "configurazione" and len(parti) == 1:
            try:
                fd = os.open(file_configurazione(nome), os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                print("nessuna configurazione da installare per questo ponte", file=sys.stderr)
                return 3
            with os.fdopen(fd, "rb") as h:
                dati = h.read(1 << 20)
            sys.stdout.buffer.write(dati)
            sys.stdout.flush()
            scrivi_avanzamento(nome, "configurazione", "il Raspberry ha scaricato la configurazione")
            return 0
        if parti[0] == "avanzamento" and len(parti) >= 2 and RE_FASE.match(parti[1]):
            scrivi_avanzamento(nome, parti[1], parti[2] if len(parti) > 2 else "")
            if parti[1] == "fatto":
                try:
                    os.unlink(file_configurazione(nome))   # installato: certificato e canale non servono piu' qui
                except FileNotFoundError:
                    pass
            return 0
    except OSError as e:
        print(f"non riesco ({e.strerror or e})", file=sys.stderr)
        return 4
    print("comando non previsto", file=sys.stderr)
    return 2


# ---- servizio della pagina (utente mesh2tak-rete, solo 127.0.0.1, dietro Caddy) ----

RE_MACCHINA = re.compile(r"^[A-Za-z0-9._-]{1,63}$")
RE_VERSIONE = re.compile(r"^[0-9a-z.+-]{1,20}$")


# ---------------------------------------------------------------- avvisi per email

try:
    from zoneinfo import ZoneInfo
    FUSO = ZoneInfo("Europe/Rome")     # le ore nelle email come in Italia, anche se il server e' in UTC
except Exception:  # noqa
    FUSO = None


def _ore(ts: float) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(ts, FUSO) if FUSO else datetime.datetime.fromtimestamp(ts)


def quando_it(ts: float, ora: float) -> str:
    q, adesso = _ore(ts), _ore(ora)
    if q.date() == adesso.date():
        return q.strftime("%H:%M")
    return f"{q.day}/{q.month} alle {q.strftime('%H:%M')}"


def giorno_it(ts: float) -> str:
    q = _ore(ts)
    return f"{q.day}/{q.month}/{q.year}"


def durata_it(s: float) -> str:
    m = max(1, int(round(s / 60)))
    if m < 60:
        return f"{m} minut{'o' if m == 1 else 'i'}"
    h, m = divmod(m, 60)
    if h < 48:
        testo = f"{h} or{'a' if h == 1 else 'e'}"
        return testo + (f" e {m} minut{'o' if m == 1 else 'i'}" if m else "")
    return f"{h // 24} giorni"


def _d(x) -> dict:
    """Un pezzo dello stato di un ponte come dizionario (un ponte rotto o malevolo non ferma gli avvisi degli altri)."""
    return x if isinstance(x, dict) else {}


def problemi_di(st: dict) -> Dict[str, str]:
    """I problemi che durano (si avvisa dopo 10 minuti): radio staccata (non durante la gestione del nodo),
    TAK Server non collegato, microSD quasi piena."""
    out: Dict[str, str] = {}
    pausa = _d(st.get("pausa")).get("attiva")
    if not _d(st.get("nodo")).get("collegato") and not pausa:
        out["radio"] = ""
    tak = _d(st.get("tak"))
    if not tak.get("collegato"):
        out["tak"] = re.sub(r"\s+", " ", str(tak.get("errore") or ""))[:200]
    libero = _d(_d(st.get("sistema")).get("disco")).get("libero_mb")
    if isinstance(libero, int) and not isinstance(libero, bool) and libero < Sorveglianza.DISCO_MIN_MB:
        out["spazio"] = str(libero)
    return out


class Sorveglianza:
    """Ogni minuto chiede lo stato a ogni ponte (attraverso il suo tunnel) e manda un'email con la posta di
    infra-TAK, agli stessi destinatari degli avvisi di Guard Dog: quando un problema dura da 10 minuti e quando
    passa, quando il certificato sta per scadere, quando la corrente non basta e quando un Raspberry si riavvia.
    Tutto su file: dopo un riavvio del servizio non rimanda e non perde gli avvisi."""

    OGNI_S = float(os.environ.get("RETE_PROVA_AVVISI_OGNI", "60"))
    ATTESA_S = int(os.environ.get("RETE_PROVA_AVVISI_ATTESA", "600"))
    RIPROVA_S = 300                    # email non partita: si riprova dopo 5 minuti
    RIPROVA_NESSUNO_S = 1800           # nessun destinatario o avvisi sospesi in infra-TAK: ogni mezz'ora
    CODA_MAX = 30
    GIORNI_CERTIFICATO = (30, 7, 1, 0)
    DISCO_MIN_MB = 500
    CORRENTE_FINESTRA_S = 3600         # cali di corrente nell'ultima ora...
    CORRENTE_OGNI_S = 24 * 3600        # ...avvisati al massimo una volta al giorno
    PROVA_OGNI_S = 30

    def __init__(self, ora=time.time, prendi=None, manda=None, percorso: Optional[str] = None, config=None):
        self.ora = ora
        self.prendi = prendi or self.chiedi_stato
        self.manda = manda or (lambda oggetto, testo, cfg: self.invia(porta_di(cfg), oggetto, testo))
        self.percorso = percorso or P(STATO_AVVISI)
        self.config = config or (lambda: Servizio._leggi_json(P(SORVEGLIANZA)) or {})
        self.lock = threading.Lock()
        self.s = self._carica()
        self._ultima_prova = 0.0
        self.dopo = None        # dalla 1.11.0: (nome, porta, stato) dopo ogni lettura, per le icone sulla mappa

    # ---- stato su file ----

    def _carica(self) -> dict:
        d = Servizio._leggi_json(self.percorso) or {}
        if not isinstance(d.get("ponti"), dict):
            d["ponti"] = {}
        if not isinstance(d.get("coda"), list):
            d["coda"] = []
        d["coda"] = [m for m in d["coda"] if isinstance(m, dict) and isinstance(m.get("ponte"), str)]
        if not isinstance(d.get("ultimo"), dict):
            d["ultimo"] = None
        if not isinstance(d.get("tentativo"), (int, float)):
            d["tentativo"] = 0
        return d

    def _salva(self) -> None:
        try:
            Servizio._scrivi_json(self.percorso, self.s)
        except OSError as e:
            print(f"avvisi: non riesco a salvare lo stato ({e})", flush=True)

    # ---- il giro di ogni minuto ----

    def ciclo(self) -> None:
        time.sleep(min(30, self.OGNI_S))        # i tunnel si ricollegano dopo un riavvio del server
        while True:
            try:
                self.giro()
            except Exception as e:  # noqa
                print(f"avvisi: giro non riuscito ({type(e).__name__}: {e})", flush=True)
            time.sleep(self.OGNI_S)

    def giro(self) -> None:
        cfg = self.config()
        ponti = {}
        for p in cfg.get("ponti") or []:
            if isinstance(p, dict) and RE_NOME.match(str(p.get("nome", ""))) and isinstance(p.get("porta"), int):
                ponti[p["nome"]] = p["porta"]
        with self.lock:
            for nome in [n for n in self.s["ponti"] if n not in ponti]:   # ponte tolto: niente piu' avvisi
                del self.s["ponti"][nome]
                self.s["coda"] = [m for m in self.s["coda"] if m.get("ponte") != nome]
        for nome, porta in sorted(ponti.items()):
            st = self.prendi(porta)
            with self.lock:
                self._valuta(nome, st, self.ora())
            if self.dopo is not None:
                try:
                    self.dopo(nome, porta, st)
                except Exception as e:  # noqa
                    print(f"icone: ponte {nome} ({type(e).__name__}: {e})", flush=True)
        self._consegna(cfg)

    def _accoda(self, nome: str, codice: str, tipo: str, ora: float, **dati) -> None:
        self.s["coda"].append(dict(dati, ponte=nome, codice=codice, tipo=tipo, quando=ora))
        del self.s["coda"][:-self.CODA_MAX]

    def _togli_dalla_coda(self, nome: str, codice: str, tipo: str) -> bool:
        prima = len(self.s["coda"])
        self.s["coda"] = [m for m in self.s["coda"]
                          if not (m.get("ponte") == nome and m.get("codice") == codice and m.get("tipo") == tipo)]
        return len(self.s["coda"]) < prima

    @staticmethod
    def _in_installazione(nome: str) -> bool:
        try:
            with open(P(f"{AVANZAMENTO_DIR}/{nome}.json"), encoding="utf-8") as f:
                return json.load(f).get("fase") != "fatto"
        except (OSError, ValueError, AttributeError):
            return False

    def _valuta(self, nome: str, st: Optional[dict], ora: float) -> None:
        p = self.s["ponti"].setdefault(nome, {})
        problemi = p.setdefault("problemi", {})
        if st is None and not (p.get("visto") or p.get("avvio")) and self._in_installazione(nome):
            return      # dalla 1.10.0: un ponte nuovo che si sta ancora installando non e' "irraggiungibile"
        if st is not None:
            p["visto"] = True
        sis = _d(_d(st).get("sistema"))
        avvio = sis.get("avvio") if isinstance(sis.get("avvio"), str) else None
        riavviato = bool(avvio and p.get("avvio") and avvio != p["avvio"])
        acceso = sis.get("acceso_dal") if isinstance(sis.get("acceso_dal"), (int, float)) else None
        if st is None:
            # non risponde: gli altri problemi restano come erano e non si avvisano (non si sa)
            attuali = {c: r.get("testo", "") for c, r in problemi.items()}
            attuali["irraggiungibile"] = ""
            controllabili = {"irraggiungibile"}
        else:
            attuali = problemi_di(st)
            controllabili = set(attuali)
        for codice, testo in attuali.items():
            r = problemi.get(codice)
            if r is None:
                r = problemi[codice] = {"dal": ora, "avvisato": False, "testo": testo}
            elif testo:
                r["testo"] = testo
            if codice in controllabili and not r["avvisato"] and ora - r["dal"] >= self.ATTESA_S:
                r["avvisato"] = True
                self._accoda(nome, codice, "nuovo", ora, dal=r["dal"], testo=r.get("testo", ""))
        riavvio_detto = False
        for codice in [c for c in problemi if c not in attuali]:
            r = problemi.pop(codice)
            if r.get("avvisato") and not self._togli_dalla_coda(nome, codice, "nuovo"):
                extra = {}
                if codice == "irraggiungibile" and riavviato and acceso:
                    extra["riavvio"] = acceso
                    riavvio_detto = True
                self._accoda(nome, codice, "risolto", ora, dal=r["dal"], **extra)
        if st is None:
            return
        if riavviato and not riavvio_detto:
            self._accoda(nome, "riavvio", "info", ora, acceso_dal=acceso)
        if avvio:
            p["avvio"] = avvio
        cert = _d(st.get("certificato"))
        scade = cert.get("scade")
        if isinstance(scade, (int, float)) and not isinstance(scade, bool):
            if p.get("cert_scade") != scade:          # primo stato o certificato rinnovato
                p["cert_scade"], p["cert_livello"] = scade, None
            rimasti = (scade - ora) / 86400
            livello = None
            for g in self.GIORNI_CERTIFICATO:
                if rimasti < g:
                    livello = g
            if livello is not None and (p.get("cert_livello") is None or livello < p["cert_livello"]):
                p["cert_livello"] = livello
                self._accoda(nome, "certificato", "info", ora, scade=scade,
                             nome_cert=re.sub(r"[^\w.@-]", "", str(cert.get("nome") or ""))[:64])
        al = _d(sis.get("alimentazione"))
        ultima = al.get("ultima")
        if (isinstance(ultima, (int, float)) and ora - ultima < self.CORRENTE_FINESTRA_S
                and ora - float(p.get("corrente_avvisata") or 0) >= self.CORRENTE_OGNI_S):
            p["corrente_avvisata"] = ora
            volte = al.get("volte") if isinstance(al.get("volte"), int) else None
            self._accoda(nome, "corrente", "info", ora, ultima=ultima, volte=volte,
                         dal=al.get("dal") if isinstance(al.get("dal"), (int, float)) else None)

    # ---- email ----

    def _consegna(self, cfg: dict) -> None:
        with self.lock:
            if not self.s["coda"]:
                self._salva()
                return
            ora = self.ora()
            ultimo = self.s.get("ultimo") or {}
            attesa = self.RIPROVA_NESSUNO_S if ultimo.get("esito") in ("nessuno", "pausa") else self.RIPROVA_S
            if self.s.get("tentativo") and ora - self.s["tentativo"] < attesa:
                self._salva()
                return
            inviati = list(self.s["coda"])
            oggetto, testo = self.componi(inviati, cfg, ora)
            self.s["tentativo"] = ora
            self._salva()
        esito, dettaglio = self.manda(oggetto, testo, cfg)
        with self.lock:
            self.s["ultimo"] = {"quando": round(ora), "esito": esito, "oggetto": oggetto, "dettaglio": dettaglio}
            if esito == "mandato":
                self.s["coda"] = [m for m in self.s["coda"] if m not in inviati]
                self.s["tentativo"] = 0
            self._salva()
        print(f"avvisi: {esito}: {oggetto}" + (f" ({dettaglio})" if dettaglio else ""), flush=True)

    def componi(self, coda: List[dict], cfg: dict, ora: float) -> Tuple[str, str]:
        righe: List[str] = []
        titoli = []
        for m in coda:
            titolo, corpo = self.messaggio(m, ora)
            titoli.append(titolo)
            righe += [titolo, "", corpo, "", ""]
        pagina = str(cfg.get("pagina") or "")
        if re.match(r"^https://[A-Za-z0-9.:-]+/$", pagina):
            righe.append(f"Pagina della rete: {pagina}")
        righe.append("Ti arriva perche' sei tra i destinatari degli avvisi di infra-TAK (Guard Dog).")
        if len(titoli) == 1:
            oggetto = titoli[0]
        else:
            oggetto = f"{len(titoli)} avvisi: {titoli[0]} e altri"
        return ("[Rete Meshtastic] " + oggetto)[:200], "\n".join(righe)

    def messaggio(self, m: dict, ora: float) -> Tuple[str, str]:
        n, codice, tipo = m.get("ponte", "?"), m.get("codice"), m.get("tipo")
        dal = m.get("dal") if isinstance(m.get("dal"), (int, float)) else m.get("quando", ora)
        q = m.get("quando", ora)
        per = durata_it(q - dal)
        if codice == "irraggiungibile":
            if tipo == "nuovo":
                return (f"Ponte {n}: non risponde",
                        f"Il ponte {n} non risponde dalle {quando_it(dal, ora)} (da {per}). Il Raspberry puo' essere "
                        "spento o bloccato, senza corrente o senza internet.\n"
                        "Cosa fare: guarda la pagina della rete. Se resta cosi', vai al Raspberry e stacca e riattacca "
                        "la corrente.")
            extra = (f" Il Raspberry si e' riavviato alle {quando_it(m['riavvio'], ora)}."
                     if isinstance(m.get("riavvio"), (int, float)) else "")
            return (f"Ponte {n}: di nuovo in linea",
                    f"Il ponte {n} risponde di nuovo: non rispondeva dalle {quando_it(dal, ora)}, per {per}.{extra}")
        if codice == "radio":
            if tipo == "nuovo":
                return (f"Ponte {n}: radio non collegata",
                        f"Dalle {quando_it(dal, ora)} il ponte {n} non parla con la sua radio: le radio della squadra "
                        "sentite da li' non arrivano sulla mappa. Il ponte riprova da solo.\n"
                        "Cosa fare: se dura, riavvia il Raspberry (stacca e riattacca la corrente); con la radio USB "
                        "controlla il cavo.")
            return (f"Ponte {n}: radio di nuovo collegata",
                    f"Il ponte {n} parla di nuovo con la sua radio (era staccata dalle {quando_it(dal, ora)}, per {per}).")
        if codice == "tak":
            if tipo == "nuovo":
                errore = f" (ultimo errore: {m['testo']})" if m.get("testo") else ""
                return (f"Ponte {n}: non arriva al TAK Server",
                        f"Dalle {quando_it(dal, ora)} il ponte {n} non e' collegato al TAK Server{errore}. Il ponte "
                        "riprova da solo.\n"
                        "Cosa fare: se il certificato del ponte e' scaduto o e' stato revocato serve un certificato "
                        "nuovo; altrimenti di solito e' internet di casa o il server.")
            return (f"Ponte {n}: di nuovo collegato al TAK Server",
                    f"Il ponte {n} e' di nuovo collegato al TAK Server (non lo era dalle {quando_it(dal, ora)}, per {per}).")
        if codice == "spazio":
            if tipo == "nuovo":
                return (f"Ponte {n}: microSD quasi piena",
                        f"Sul Raspberry del ponte {n} restano {m.get('testo') or 'pochi'} MB liberi: con la microSD "
                        "piena il ponte si ferma.\n"
                        "Cosa fare: sul Raspberry  sudo apt clean  e  sudo journalctl --vacuum-size=100M; se non "
                        "basta, guarda la guida.")
            return (f"Ponte {n}: spazio di nuovo sufficiente", f"Sulla microSD del ponte {n} c'e' di nuovo spazio.")
        if codice == "riavvio":
            alle = (f" alle {quando_it(m['acceso_dal'], ora)}" if isinstance(m.get("acceso_dal"), (int, float))
                    else "")
            return (f"Ponte {n}: il Raspberry si e' riavviato",
                    f"Il Raspberry del ponte {n} si e' riavviato{alle}. Se non e' stato un aggiornamento o qualcuno di "
                    "voi, si era bloccato ed e' ripartito da solo: se succede spesso, controlla alimentatore e "
                    "microSD.")
        if codice == "certificato":
            scade = m.get("scade") or 0
            chi = f" {m['nome_cert']}" if m.get("nome_cert") else ""
            rinnovo = ("Cosa fare: nella console infra-TAK crea un certificato nuovo per il ponte (TAK Server, "
                       "Create Client Certificate, lettura e scrittura BOTH sul canale della squadra, Integration "
                       "package), copialo sul Raspberry e lancia:  sudo bash installa-ponte-meshtastic.sh "
                       "NOMEFILE.zip")
            if scade <= q:
                return (f"Ponte {n}: il certificato TAK e' scaduto",
                        f"Il certificato{chi} del ponte {n} e' scaduto il {giorno_it(scade)}: il TAK Server non lo "
                        f"accetta piu'.\n{rinnovo}")
            giorni = max(1, int((scade - q) // 86400))
            return (f"Ponte {n}: il certificato TAK scade il {giorno_it(scade)}",
                    f"Il certificato{chi} del ponte {n} scade il {giorno_it(scade)}, tra {giorni} "
                    f"giorn{'o' if giorni == 1 else 'i'}. Dopo, il ponte non entra piu' nel TAK Server.\n{rinnovo}")
        if codice == "corrente":
            volte = f"; {m['volte']} da quando il ponte e' acceso" if isinstance(m.get("volte"), int) else ""
            ultima = m.get("ultima") if isinstance(m.get("ultima"), (int, float)) else q
            return (f"Ponte {n}: corrente insufficiente",
                    f"Il Raspberry del ponte {n} ha avuto cali di corrente (l'ultimo alle {quando_it(ultima, ora)}"
                    f"{volte}). Se la corrente non basta il Raspberry si puo' bloccare o rovinare la microSD.\n"
                    "Cosa fare: usa l'alimentatore ufficiale da 5,1 V e 2,5 A, con un cavo corto.")
        return (f"Ponte {n}: avviso", "")

    @staticmethod
    def invia(porta: int, oggetto: str, testo: str) -> Tuple[str, str]:
        """Email con la posta di infra-TAK (Guard Dog): la console sceglie i destinatari e rispetta le sue pause.
        Esito: mandato, nessuno (nessun destinatario), pausa (avvisi sospesi), trattenuto (manutenzione), errore."""
        corpo = json.dumps({"subject": oggetto[:200], "body": testo[:50000], "to": ""}).encode("utf-8")
        for schema in ("https", "http"):
            gestori = [urllib.request.ProxyHandler({})]
            if schema == "https":
                gestori.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
            apri = urllib.request.build_opener(*gestori)
            richiesta = urllib.request.Request(f"{schema}://127.0.0.1:{porta}/api/guarddog/send-alert-email",
                                               data=corpo, headers={"Content-Type": "application/json"},
                                               method="POST")
            try:
                with apri.open(richiesta, timeout=20) as r:
                    risposta = json.loads(r.read(65536) or b"{}")
            except urllib.error.HTTPError as e:      # ha risposto: niente secondo tentativo (email doppia)
                return "errore", f"infra-TAK ha risposto con l'errore {e.code}"
            except (urllib.error.URLError, OSError, ValueError):
                if schema == "https":
                    continue                          # come gli avvisi di Guard Dog: HTTPS, poi HTTP
                return "errore", f"la console di infra-TAK non risponde (porta {porta})"
            if not isinstance(risposta, dict):
                return "errore", "risposta di infra-TAK non valida"
            if risposta.get("held"):
                return "trattenuto", "infra-TAK e' in una finestra di manutenzione: riprovo dopo"
            if risposta.get("suppressed"):
                if "recipient" in str(risposta.get("reason") or ""):
                    return "nessuno", "in infra-TAK non c'e' un indirizzo per gli avvisi (Guard Dog)"
                return "pausa", "gli avvisi di infra-TAK sono sospesi"
            if risposta.get("success"):
                return "mandato", ""
            return "errore", str(risposta.get("error") or "risposta di infra-TAK non valida")[:200]
        return "errore", "la console di infra-TAK non risponde"

    @staticmethod
    def chiedi_stato(porta: int) -> Optional[dict]:
        """Lo stato del ponte dal suo tunnel (solo su 127.0.0.1; il certificato della pagina e' quello del ponte)."""
        apri = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                           urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
        try:
            with apri.open(f"https://127.0.0.1:{porta}/mesh2tak/stato.json", timeout=10) as r:
                d = json.loads(r.read(4 << 20))
        except Exception:  # noqa
            return None
        return d if isinstance(d, dict) and d.get("formato") == 1 else None

    @staticmethod
    def manda_icone(porta: int, corpo: bytes) -> Optional[dict]:
        """Le icone scelte al ponte, dal suo tunnel (1.11.0): il ponte le accetta solo da li'."""
        apri = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                           urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
        richiesta = urllib.request.Request(f"https://127.0.0.1:{porta}/mesh2tak/icone", data=corpo, method="POST",
                                           headers={"Content-Type": "application/json", "X-Mesh2tak": "1"})
        try:
            with apri.open(richiesta, timeout=15) as r:
                d = json.loads(r.read(65536))
        except Exception:  # noqa
            return None
        return d if isinstance(d, dict) else None

    # ---- per la pagina ----

    def vista(self) -> dict:
        with self.lock:
            problemi = {n: sorted([{"codice": c, "dal": round(r.get("dal", 0)), "avvisato": bool(r.get("avvisato"))}
                                   for c, r in p.get("problemi", {}).items()], key=lambda x: x["codice"])
                        for n, p in self.s["ponti"].items() if p.get("problemi")}
            return {"ultimo": self.s.get("ultimo"), "in_coda": len(self.s["coda"]), "problemi": problemi,
                    "attesa_min": round(self.ATTESA_S / 60)}

    def prova(self, utente: str) -> Tuple[int, dict]:
        ora = self.ora()
        if ora - self._ultima_prova < self.PROVA_OGNI_S:
            return 429, {"errore": "ne ho appena mandata una: aspetta mezzo minuto"}
        self._ultima_prova = ora
        cfg = self.config()
        q = _ore(ora)
        esito, dettaglio = self.manda("[Rete Meshtastic] Email di prova",
                                      f"Prova degli avvisi della rete Meshtastic, chiesta da {utente} il {q.day}/{q.month} "
                                      f"alle {q.strftime('%H:%M')}.\nSe la leggi, gli avvisi ti arrivano.", cfg)
        print(f"avvisi: prova chiesta da {utente}: {esito}" + (f" ({dettaglio})" if dettaglio else ""), flush=True)
        parole = {"mandato": "Email di prova consegnata a infra-TAK: controlla la casella degli avvisi (anche lo "
                             "spam) tra un paio di minuti.",
                  "nessuno": "Non partita: in infra-TAK non c'e' un indirizzo per gli avvisi. Impostalo nella "
                             "console infra-TAK, in Guard Dog, e riprova.",
                  "pausa": "Non partita: gli avvisi di infra-TAK sono sospesi (Guard Dog).",
                  "trattenuto": "Trattenuta: infra-TAK e' in una finestra di manutenzione. Riprova piu' tardi."}
        messaggio = parole.get(esito) or f"Non partita: {dettaglio}."
        return 200, {"esito": esito, "messaggio": messaggio}


class Rilasci:
    """L'ultima versione pubblicata su GitHub (release pubbliche, niente token): la pagina dice se c'e' qualcosa
    di nuovo e con quali novita'. Il servizio legge solo questo; scaricare e installare lo fa root."""

    OGNI_S = 6 * 3600
    CONTROLLO_OGNI_S = float(os.environ.get("RETE_PROVA_RILASCI_CONTROLLO", "60"))   # "Controlla adesso"
    PRIMO_S = float(os.environ.get("RETE_PROVA_RILASCI_PRIMO", "60"))                # dopo l'avvio del servizio

    def __init__(self, ora=time.time, chiedi=None, percorso: Optional[str] = None):
        self.ora = ora
        self.chiedi = chiedi or self.ultima_release
        self.percorso = percorso or P(RILASCI)
        self.lock = threading.Lock()
        d = Servizio._leggi_json(self.percorso) or {}
        self.s = d if isinstance(d.get("controllato"), (int, float)) else {"controllato": 0, "ultima": None,
                                                                             "errore": None}

    @staticmethod
    def ultima_release() -> dict:
        apri = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        rq = urllib.request.Request(f"{GITHUB_API}/repos/{REPO}/releases/latest",
                                    headers={"Accept": "application/vnd.github+json",
                                             "User-Agent": f"mesh2tak-rete/{VERSIONE}"})
        with apri.open(rq, timeout=30) as r:
            return json.loads(r.read(1 << 20))

    def controlla(self, forza: bool = False) -> Tuple[bool, str]:
        ora = self.ora()
        with self.lock:
            ultimo = self.s.get("controllato") or 0
            if ora - ultimo < (self.CONTROLLO_OGNI_S if forza else self.OGNI_S):
                return False, "controllato da poco"
            self.s["controllato"] = ora
        ultima, errore = None, None
        try:
            d = self.chiedi()
        except urllib.error.HTTPError as e:
            errore = "nessuna versione pubblicata" if e.code == 404 else f"GitHub risponde {e.code}"
        except Exception as e:  # noqa
            errore = f"GitHub non risponde ({type(e).__name__})"
        else:
            tag = str(d.get("tag_name") or "") if isinstance(d, dict) else ""
            versione = tag[1:] if tag.startswith("v") else tag
            if not RE_VERSIONE_RILASCIO.match(versione):
                errore = "l'ultima release su GitHub non ha un numero di versione (vX.Y.Z)"
            else:
                nomi = {a.get("name") for a in d.get("assets") or [] if isinstance(a, dict)}
                completa = {"installa-ponte-meshtastic.sh", "rete-meshtastic.sh", "SHA256SUMS"} <= nomi
                ultima = {"versione": versione, "note": str(d.get("body") or "")[:4000],
                          "pubblicata": str(d.get("published_at") or "")[:30], "completa": completa}
        with self.lock:
            if ultima is not None:
                self.s["ultima"] = ultima     # se GitHub non risponde resta l'ultima vista
            self.s["errore"] = errore
            try:
                Servizio._scrivi_json(self.percorso, self.s)
            except OSError:
                pass
        print(f"rilasci: {'ultima ' + ultima['versione'] if ultima else errore}", flush=True)
        return True, errore or ""

    def ciclo(self) -> None:
        time.sleep(self.PRIMO_S)
        while True:
            try:
                self.controlla()
            except Exception as e:  # noqa
                print(f"rilasci: controllo non riuscito ({e})", flush=True)
            time.sleep(600)

    def vista(self) -> dict:
        with self.lock:
            ultima = self.s.get("ultima")
            nuova = bool(ultima and ultima.get("completa")
                         and numeri_versione(ultima["versione"]) > numeri_versione(VERSIONE))
            return {"installata": VERSIONE, "ultima": ultima, "nuova": nuova, "errore": self.s.get("errore"),
                    "controllato": round(self.s.get("controllato") or 0) or None}

    def pronta(self, versione: str) -> bool:
        """La versione chiesta dalla pagina e' quella pubblicata e completa (non una scritta a mano)."""
        with self.lock:
            u = self.s.get("ultima") or {}
            return bool(u.get("completa") and u.get("versione") == versione)


def porta_di(cfg: dict) -> int:
    porta = cfg.get("console")
    return porta if isinstance(porta, int) and 0 < porta < 65536 else 5001


class Servizio:
    """Richieste dei Raspberry (pubbliche, da approvare) e azioni degli amministratori (dietro Authentik):
    scrive solo file nella sua cartella; le modifiche al server le fa il passo dei lavori."""

    RICHIESTE_MAX = 20                  # in attesa tutte insieme
    PER_INDIRIZZO_ORA = 6               # richieste nuove per indirizzo (IPv6: per rete /64)
    SCADENZA_S = 48 * 3600              # in attesa senza notizie dal Raspberry: si toglie
    RIFIUTO_S = 30 * 24 * 3600          # rifiutate e ponti tolti: il Raspberry non rientra da solo
    LAVORO_FERMO_S = 120                # in coda da tanto: il passo dei lavori non parte
    LAVORO_PERSO_S = 180                # approvata ma il lavoro non c'e' piu'
    LAVORO_INTERROTTO_S = 50 * 60       # "in corso" da troppo (piu' del timeout dei lavori): server riavviato a meta'
    INVITI_MAX = 10                     # inviti aperti tutti insieme
    INVITO_S = 14 * 24 * 3600           # un invito non usato vale due settimane
    INVITO_USATO_S = 7 * 24 * 3600      # poi resta in vista ancora una settimana

    def __init__(self, sorveglianza: Optional[Sorveglianza] = None, rilasci: Optional[Rilasci] = None):
        self.lock = threading.Lock()
        self.per_ip: Dict[str, List[float]] = {}
        self.sorveglianza = sorveglianza
        self.rilasci = rilasci
        self.icone_ponti: Dict[str, str] = {}    # versione delle icone che ogni ponte usa ("vecchio": prima della 1.11.0)
        self.manda_icone = Sorveglianza.manda_icone

    # ---- file ----

    @staticmethod
    def _leggi_json(percorso: str) -> Optional[dict]:
        try:
            with open(percorso, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _scrivi_json(percorso: str, dati: dict) -> None:
        nuovo = percorso + ".nuovo"
        os.makedirs(os.path.dirname(percorso), mode=0o700, exist_ok=True)
        with open(os.open(nuovo, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
            json.dump(dati, f, ensure_ascii=False)
        os.replace(nuovo, percorso)

    @staticmethod
    def _eta(percorso: str) -> Optional[float]:
        try:
            return time.time() - os.path.getmtime(percorso)
        except OSError:
            return None

    def ponti(self) -> Dict[str, dict]:
        d = self._leggi_json(P(SITO_DIR + "/ponti.json")) or {}
        return {p.get("nome"): p for p in d.get("ponti", []) if isinstance(p, dict)}

    @staticmethod
    def registrato(impronta: str) -> bool:
        return bool(RE_IMPRONTA.match(impronta or "")) and os.path.exists(P(f"{ACCESSO_DIR}/{impronta}.json"))

    def registrazione_di(self, nome: str) -> Tuple[Optional[str], str]:
        """(impronta, chiave) del ponte registrato con quel nome."""
        for f in glob.glob(P(ACCESSO_DIR) + "/*.json"):
            d = self._leggi_json(f) or {}
            impronta = os.path.basename(f)[:-5]
            if d.get("nome") == nome and RE_IMPRONTA.match(impronta):
                return impronta, str(d.get("chiave") or "")
        return None, ""

    def stato_lavoro(self, lavoro: str) -> dict:
        """esito: in_coda, in_corso, ok, errore oppure None (sparito); fermo = in coda da troppo."""
        if not RE_ID.match(lavoro or ""):
            return {"esito": None}
        e = self._leggi_json(P(f"{ESITI_DIR}/{lavoro}.json"))
        if e:
            if e.get("esito") == "in_corso" and time.time() - (e.get("quando") or 0) > self.LAVORO_INTERROTTO_S:
                e = dict(e, esito="errore", messaggio="lavoro interrotto a meta' (il server si e' riavviato?): riprova")
            return e
        eta = self._eta(P(f"{LAVORI_DIR}/{lavoro}.json"))
        if eta is not None:
            return {"esito": "in_coda", "fermo": eta > self.LAVORO_FERMO_S}
        return {"esito": None}

    def richieste(self) -> List[dict]:
        """Tutte le richieste (in attesa, in corso e rifiutate), aggiornate con gli esiti dei lavori; toglie
        quelle scadute e quelle dei Raspberry ormai registrati. Da chiamare con self.lock."""
        ora = time.time()
        elenco = []
        for f in glob.glob(P(RICHIESTE_DIR) + "/*.json"):
            r = self._leggi_json(f)
            if not r or not RE_IMPRONTA.match(str(r.get("id", ""))) or os.path.basename(f) != r["id"] + ".json":
                togli(f)
                continue
            stato = r.get("stato")
            if stato == "in_corso":
                lv = self.stato_lavoro(r.get("lavoro", ""))
                if lv.get("esito") == "ok":
                    togli(f)
                    continue
                if lv.get("esito") == "errore" or (lv.get("esito") is None and
                                                   ora - (r.get("approvata") or 0) > self.LAVORO_PERSO_S):
                    r.update(stato="attesa", errore=lv.get("messaggio") or "il lavoro non e' partito sul server",
                             dettagli=str(lv.get("testo") or "")[-1500:], ultima=int(ora))
                    self._scrivi_json(f, r)
                else:
                    r["fermo"] = bool(lv.get("fermo"))
            elif stato == "rifiutata":
                vecchia = ora - (r.get("ultima") or 0)
                # un ponte appena tolto e' ancora registrato finche' il lavoro non l'ha tolto davvero
                if vecchia > self.RIFIUTO_S or (self.registrato(r["id"]) and
                                                (r.get("motivo") != "tolto" or vecchia > 3600)):
                    togli(f)
                    continue
            elif self.registrato(r["id"]):
                togli(f)        # registrato da un'altra strada (a mano, o da riga di comando)
                continue
            if r.get("stato") == "attesa" and ora - (r.get("ultima") or 0) > self.SCADENZA_S:
                togli(f)
                continue
            elenco.append(r)
        return sorted(elenco, key=lambda r: r.get("arrivata") or 0)

    def accoda(self, azione: str, nome: str, da: str, chiave: str = "", richiesta: Optional[str] = None,
               sostituisci: bool = False, versione: Optional[str] = None, installa: Optional[dict] = None,
               impostazioni: Optional[dict] = None) -> str:
        """Scrive il lavoro in tmp e lo sposta in coda gia' completo: il passo dei lavori parte appena la
        cartella non e' vuota e non deve mai trovare un file a meta'."""
        lavoro = secrets.token_hex(8)
        dati = {"id": lavoro, "azione": azione, "nome": nome, "da": da, "chiave": chiave, "richiesta": richiesta,
                "sostituisci": sostituisci, "quando": int(time.time())}
        if versione:
            dati["versione"] = versione
        if installa is not None:
            dati["installa"] = installa
        if impostazioni is not None:
            dati["impostazioni"] = impostazioni      # certificato e canale: solo per root, che lo toglie subito
        provvisorio = P(f"{TMP_DIR}/{lavoro}.json")
        with open(os.open(provvisorio, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
            json.dump(dati, f, ensure_ascii=False)
        os.replace(provvisorio, P(f"{LAVORI_DIR}/{lavoro}.json"))
        print(f"lavoro {lavoro}: {azione} {nome} chiesto da {da}" + (" (sostituisce)" if sostituisci else ""),
              flush=True)
        return lavoro

    def lavori(self) -> List[dict]:
        """In coda, in corso e ultimi esiti (senza le chiavi)."""
        risultato = []
        for f in glob.glob(P(LAVORI_DIR) + "/*.json"):
            d = self._leggi_json(f) or {}
            eta = self._eta(f)
            if eta is None:
                continue
            risultato.append({"id": d.get("id"), "azione": d.get("azione"), "nome": d.get("nome"), "da": d.get("da"),
                              "richiesta": d.get("richiesta"), "versione": d.get("versione"), "esito": "in_coda",
                              "quando": d.get("quando"), "fermo": eta > self.LAVORO_FERMO_S})
        esiti = [(self._eta(f), f) for f in glob.glob(P(ESITI_DIR) + "/*.json")]
        for _, f in sorted((x for x in esiti if x[0] is not None), key=lambda x: x[0])[:8]:
            d = self._leggi_json(f) or {}
            v = {k: d.get(k) for k in ("id", "azione", "nome", "da", "richiesta", "versione", "esito", "messaggio",
                                       "testo", "quando")}
            if v["esito"] == "in_corso" and time.time() - (v["quando"] or 0) > self.LAVORO_INTERROTTO_S:
                v.update(esito="errore", messaggio="lavoro interrotto a meta' (il server si e' riavviato?)")
            risultato.append(v)
        return risultato

    # ---- ponti nuovi (dalla 1.10.0): inviti, impostazioni e avanzamento delle installazioni ----

    def pagina_rete(self) -> str:
        """L'indirizzo della pagina della rete (lo scrive root): va nella chiave d'invito."""
        url = str((self._leggi_json(P(SORVEGLIANZA)) or {}).get("pagina") or "").rstrip("/")
        return url if re.match(r"^https://[A-Za-z0-9.-]+(:\d+)?$", url) else ""

    def riassunto_nuovi(self) -> dict:
        d = self._leggi_json(P(RIASSUNTO_NUOVI))
        return d if d and d.get("pronto") is True else {"pronto": False}

    def inviti(self) -> List[dict]:
        """Gli inviti, piu' vecchi prima; toglie quelli scaduti. Da chiamare con self.lock."""
        ora = time.time()
        elenco = []
        for f in glob.glob(P(INVITI_DIR) + "/*.json"):
            d = self._leggi_json(f)
            if not d or not RE_IMPRONTA.match(str(d.get("id", ""))) or os.path.basename(f) != d["id"] + ".json":
                togli(f)
                continue
            if ((d.get("stato") == "attesa" and ora - (d.get("creato") or 0) > self.INVITO_S) or
                    (d.get("stato") != "attesa" and ora - (d.get("usato") or d.get("creato") or 0) > self.INVITO_USATO_S)):
                togli(f)
                continue
            elenco.append(d)
        return sorted(elenco, key=lambda d: d.get("creato") or 0)

    def vista_invito(self, i: dict) -> dict:
        v = {k: i.get(k) for k in ("id", "nome", "scheda", "posizione", "creato", "da", "stato", "usato")}
        v["scade"] = int(i.get("creato") or 0) + self.INVITO_S
        if i.get("stato") == "attesa":
            v["riga"] = i.get("riga")          # la chiave d'invito: solo finche' non e' usata
        return v

    def avanzamento(self, nome: str) -> Optional[dict]:
        """A che punto e' l'installazione del ponte nuovo (lo scrive il Raspberry, via SSH)."""
        d = self._leggi_json(P(f"{AVANZAMENTO_DIR}/{nome}.json")) if RE_NOME.match(nome or "") else None
        if not d or not RE_FASE.match(str(d.get("fase", ""))) or not isinstance(d.get("quando"), int):
            return None
        v = {"fase": d["fase"], "testo": str(d.get("testo") or "")[:300], "quando": d["quando"]}
        scelte = self._leggi_json(P(f"{INSTALLAZIONI_DIR}/{nome}.json")) or {}
        v.update({k: scelte.get(k) for k in ("scheda", "posizione") if isinstance(scelte.get(k), str)})
        return v

    # ---- icone sulla mappa (dalla 1.11.0) ----

    def scelte_icone(self) -> Dict[str, dict]:
        d = self._leggi_json(P(ICONE_SCELTE)) or {}
        scelte = d.get("scelte") if isinstance(d.get("scelte"), dict) else {}
        return {k: v for k, v in scelte.items()
                if RE_ID_RADIO.match(str(k)) and isinstance(v, dict) and v.get("icona") in ICONE_PER_CHIAVE}

    @staticmethod
    def icone_per_i_ponti(scelte: Dict[str, dict]) -> Tuple[str, Dict[str, dict]]:
        """(versione, quello che riceve ogni ponte): per ogni radio la figura e il tipo, nient'altro."""
        mappa = {k: {"percorso": ICONE_PER_CHIAVE[v["icona"]]["percorso"], "tipo": ICONE_PER_CHIAVE[v["icona"]]["tipo"]}
                 for k, v in sorted(scelte.items())}
        impronta = hashlib.sha256(json.dumps(mappa, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        return impronta.hexdigest()[:16], mappa

    def imposta_icona(self, dati: dict, utente: str) -> Tuple[int, dict]:
        radio, icona = str(dati.get("radio") or ""), str(dati.get("icona") or "")
        if not RE_ID_RADIO.match(radio):
            return 400, {"errore": "radio non valida"}
        if icona and icona not in ICONE_PER_CHIAVE:
            return 400, {"errore": "icona sconosciuta: ricarica la pagina"}
        with self.lock:
            scelte = self.scelte_icone()
            if icona:
                if radio not in scelte and len(scelte) >= ICONE_MAX:
                    return 409, {"errore": f"al massimo {ICONE_MAX} radio con un'icona scelta"}
                scelte[radio] = {"icona": icona, "da": utente, "quando": int(time.time())}
            else:
                scelte.pop(radio, None)
            self._scrivi_json(P(ICONE_SCELTE), {"formato": 1, "scelte": scelte})
        print(f"icona di {radio}: {icona or 'predefinita'} (da {utente})", flush=True)
        return 200, {"esito": "fatto", "versione": self.icone_per_i_ponti(scelte)[0],
                     "messaggio": "Icona salvata." if icona else "Tornata l'icona predefinita."}

    def allinea_icone(self, nome: str, porta: int, st: Optional[dict]) -> None:
        """Dopo ogni lettura dello stato di un ponte (una al minuto): se usa icone diverse da quelle scelte,
        gliele manda dal suo tunnel. Un ponte spento le riceve quando torna."""
        if st is None:
            return
        ic = st.get("icone")
        if not isinstance(ic, dict):
            self.icone_ponti[nome] = "vecchio"
            return
        with self.lock:
            versione, mappa = self.icone_per_i_ponti(self.scelte_icone())
        if ic.get("versione") == versione:
            self.icone_ponti[nome] = versione
            return
        corpo = json.dumps({"formato": 1, "versione": versione, "icone": mappa}, separators=(",", ":"))
        risposta = self.manda_icone(porta, corpo.encode("utf-8"))
        if isinstance(risposta, dict) and risposta.get("versione") == versione:
            self.icone_ponti[nome] = versione
            print(f"icone: il ponte {nome} usa quelle scelte ({len(mappa)} radio)", flush=True)
        else:
            self.icone_ponti[nome] = str(ic.get("versione") or "")
            errore = risposta.get("errore") if isinstance(risposta, dict) else "nessuna risposta"
            print(f"icone: il ponte {nome} non le ha prese ({errore})", flush=True)

    def vista_icone(self, ponti) -> dict:
        scelte = self.scelte_icone()
        versione, _ = self.icone_per_i_ponti(scelte)
        stato_ponti = {}
        for n in ponti:
            v = self.icone_ponti.get(n)
            stato_ponti[n] = "aggiornato" if v == versione else ("vecchio" if v == "vecchio" else "attesa")
        return {"versione": versione, "scelte": scelte, "ponti": stato_ponti}

    def _salva_installazione(self, nome: str, installa: dict, utente: str) -> None:
        self._scrivi_json(P(f"{INSTALLAZIONI_DIR}/{nome}.json"), dict(installa, da=utente, quando=int(time.time())))

    def nuovo_invito(self, dati: dict, utente: str) -> Tuple[int, dict]:
        try:
            nome = valida_nome(str(dati.get("nome", "")))
            posizione = valida_posizione(str(dati.get("posizione") or ""))
        except Errore as e:
            return 400, {"errore": str(e)}
        scheda = str(dati.get("scheda") or "")
        if scheda not in SCHEDE:
            return 400, {"errore": "scegli la radio del ponte"}
        if not self.riassunto_nuovi().get("pronto"):
            return 409, {"errore": "prima carica certificato e canale per i ponti nuovi"}
        rete = self.pagina_rete()
        if not rete:
            return 503, {"errore": "manca l'indirizzo della pagina: sul server  sudo bash rete-meshtastic.sh installa"}
        if nome in self.ponti():
            return 409, {"errore": f"il nome '{nome}' e' gia' di un ponte della rete: scegline un altro"}
        with self.lock:
            aperti = [i for i in self.inviti() if i.get("stato") == "attesa"]
            if any(i.get("nome") == nome for i in aperti):
                return 409, {"errore": f"c'e' gia' un invito per il ponte '{nome}': usa quello, o annullalo"}
            if len(aperti) >= self.INVITI_MAX:
                return 429, {"errore": "troppi inviti aperti: annulla quelli che non servono"}
            # una chiave pubblica SSH a caso, di cui nessuno ha la chiave privata: Raspberry Pi Imager la scrive sul
            # Raspberry come una delle sue chiavi autorizzate, e il Raspberry la manda indietro. E' il segreto
            # dell'invito; dopo il nome c'e' l'indirizzo della pagina della rete, che cosi' non sta nell'immagine
            blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + secrets.token_bytes(32)
            inv = {"id": hashlib.sha256(blob).hexdigest(), "nome": nome, "scheda": scheda, "posizione": posizione,
                   "riga": f"ssh-ed25519 {base64.b64encode(blob).decode()} mesh2tak-invito {rete}",
                   "creato": int(time.time()), "da": utente, "stato": "attesa"}
            self._scrivi_json(P(f"{INVITI_DIR}/{inv['id']}.json"), inv)
        print(f"invito per il ponte nuovo '{nome}' preparato da {utente}", flush=True)
        return 200, {"esito": "ok", "invito": self.vista_invito(inv)}

    @staticmethod
    def _leggi_nuovo(d) -> Optional[dict]:
        """Le notizie di un Raspberry appena acceso con l'immagine dei ponti nuovi (dalla 1.10.0)."""
        if not isinstance(d, dict):
            return None
        modello = re.sub(r"[^\w .,()+/-]", "", str(d.get("modello") or ""))[:80].strip()
        inviti = []
        for b in (d.get("inviti") if isinstance(d.get("inviti"), list) else [])[:3]:
            try:
                blob = base64.b64decode(str(b), validate=True)
            except (binascii.Error, ValueError):
                continue
            if len(blob) == 51 and blob.startswith(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"):
                inviti.append(hashlib.sha256(blob).hexdigest())
        return {"modello": modello, "radio_usb": d.get("radio_usb") is True, "inviti": inviti}

    def _invito_valido(self, ids: List[str]) -> Optional[dict]:
        ora = time.time()
        for i in ids:
            d = self._leggi_json(P(f"{INVITI_DIR}/{i}.json"))
            if d and d.get("id") == i and d.get("stato") == "attesa" and ora - (d.get("creato") or 0) <= self.INVITO_S:
                return d
        return None

    # ---- richieste dei Raspberry (pubbliche) ----

    @staticmethod
    def _rete_di(ip: str) -> str:
        """Il limite per indirizzo vale per un IPv4 o per una rete IPv6 /64 (una casa ne ha una intera)."""
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return ip
        if a.version == 6:
            if a.ipv4_mapped:
                return str(a.ipv4_mapped)
            return str(ipaddress.ip_network(f"{a}/64", strict=False))
        return str(a)

    def nuova_richiesta(self, dati, ip: str) -> Tuple[int, dict]:
        if not isinstance(dati, dict) or dati.get("formato") != 1:
            return 400, {"errore": "richiesta non valida"}
        try:
            nome = valida_nome(str(dati.get("nome", "")))
            chiave, impronta = leggi_chiave(str(dati.get("chiave", "")))
        except Errore as e:
            return 400, {"errore": str(e)}
        macchina = str(dati.get("macchina") or "")
        versione = str(dati.get("versione") or "")
        if not RE_MACCHINA.match(macchina) or not RE_VERSIONE.match(versione):
            return 400, {"errore": "richiesta non valida"}
        controllo = codice_controllo(impronta)
        if self.registrato(impronta):
            return 200, {"esito": "registrato", "controllo": controllo}
        nuovo = self._leggi_nuovo(dati.get("nuovo"))
        ora = time.time()
        with self.lock:
            percorso = P(f"{RICHIESTE_DIR}/{impronta}.json")
            vecchia = self._leggi_json(percorso)
            if vecchia and vecchia.get("stato") == "rifiutata":
                if ora - (vecchia.get("ultima") or 0) < self.RIFIUTO_S:
                    perche = ("tolto dalla rete" if vecchia.get("motivo") == "tolto" else "rifiutato")
                    return 403, {"errore": f"questo Raspberry e' stato {perche} sulla pagina della rete",
                                 "controllo": controllo}
                vecchia = None
            if vecchia and vecchia.get("stato") == "in_corso":
                vecchia.update(ultima=int(ora), ip=ip)        # lo sta gia' aggiungendo: niente da cambiare
                self._scrivi_json(percorso, vecchia)
                return 202, {"esito": "in_attesa", "controllo": controllo, "in_corso": True}
            if not vecchia:
                rete = self._rete_di(ip)
                recenti = [x for x in self.per_ip.get(rete, []) if ora - x < 3600]
                if len(recenti) >= self.PER_INDIRIZZO_ORA:
                    return 429, {"errore": "troppe richieste da questo indirizzo: riprovo tra un'ora"}
                self.per_ip[rete] = recenti + [ora]
                if len(self.per_ip) > 500:
                    self.per_ip = {k: v for k, v in self.per_ip.items() if any(ora - x < 3600 for x in v)}
                if sum(1 for r in self.richieste() if r.get("stato") != "rifiutata") >= self.RICHIESTE_MAX:
                    return 429, {"errore": "troppe richieste in attesa sulla pagina della rete"}
            if vecchia and vecchia.get("invito"):
                nome = vecchia.get("nome") or nome        # il nome l'ha deciso l'invito, non il Raspberry
            r = dict(vecchia or {}, id=impronta, nome=nome, chiave=chiave, macchina=macchina, versione=versione,
                     ip=ip, controllo=controllo, ultima=int(ora))
            r.setdefault("arrivata", int(ora))
            r.setdefault("stato", "attesa")
            invito = None
            if nuovo is not None:
                r["nuovo"] = {"modello": nuovo["modello"], "radio_usb": nuovo["radio_usb"]}
                invito = self._invito_valido(nuovo["inviti"]) if r["stato"] == "attesa" else None
            if invito is not None:
                # Raspberry con la chiave d'invito: si installa da solo, con le scelte fatte nell'invito
                nome = invito["nome"]
                installa = {"scheda": invito["scheda"], "posizione": invito.get("posizione") or ""}
                r.update(nome=nome, invito=invito["id"], installa=installa)
                if nome in self.ponti():
                    r["errore"] = (f"nel frattempo c'e' un altro ponte '{nome}': toglilo dalla rete o rifiuta "
                                   "questa richiesta")
                else:
                    lavoro = self.accoda("aggiungi", nome, invito["da"], chiave, impronta, installa=installa)
                    r.update(stato="in_corso", lavoro=lavoro, approvata=int(ora), da=invito["da"], errore=None,
                             dettagli=None)
                    self._salva_installazione(nome, installa, invito["da"])
                    print(f"il Raspberry {macchina} ({ip}) ha la chiave d'invito del ponte nuovo '{nome}': lo "
                          "aggiungo", flush=True)
                invito.update(stato="usato", usato=int(ora), richiesta=impronta)
                self._scrivi_json(P(f"{INVITI_DIR}/{invito['id']}.json"), invito)
            self._scrivi_json(percorso, r)
        if r.get("stato") == "in_corso":
            return 202, {"esito": "in_attesa", "controllo": controllo, "in_corso": True}
        sostituisce = nome in self.ponti()
        if not vecchia:
            print(f"richiesta del ponte '{nome}' da {macchina} ({ip}), codice di controllo {controllo}"
                  + (" (c'e' gia' un ponte con questo nome)" if sostituisce else "")
                  + (" (Raspberry nuovo, senza una chiave d'invito valida)" if nuovo is not None else ""), flush=True)
        return 202, {"esito": "in_attesa", "controllo": controllo, "sostituisce": sostituisce}

    # ---- azioni degli amministratori ----

    def stato(self, utente: str) -> dict:
        with self.lock:
            tutte = self.richieste()
            ponti = self.ponti()
            lavori = self.lavori()
            inviti = self.inviti()
        aperte = [r for r in tutte if r.get("stato") in ("attesa", "in_corso")]
        nomi = [r.get("nome") for r in aperte]
        richieste = []
        for r in aperte:
            v = {k: r.get(k) for k in ("id", "nome", "macchina", "versione", "ip", "controllo", "arrivata", "ultima",
                                       "stato", "errore", "dettagli", "da", "nuovo", "installa")}
            v.update(stesso_nome=nomi.count(r.get("nome")) > 1, sostituisce=r.get("nome") in ponti,
                     fermo=bool(r.get("fermo")), invito=bool(r.get("invito")))
            richieste.append(v)
        installazioni = {}
        for n in ponti:
            av = self.avanzamento(n)
            if av:
                installazioni[n] = av
        rifiutate = [dict({k: r.get(k) for k in ("id", "nome", "macchina", "ip", "controllo", "ultima", "da", "motivo")},
                          presente=r.get("nome") in ponti, chiave=bool(r.get("chiave")))
                     for r in tutte if r.get("stato") == "rifiutata" and not self.registrato(r.get("id", ""))]
        rifiutate.sort(key=lambda r: -(r.get("ultima") or 0))
        return {"formato": 1, "utente": utente, "richieste": richieste, "rifiutate": rifiutate, "lavori": lavori,
                "versione": VERSIONE, "avvisi": self.sorveglianza.vista() if self.sorveglianza else None,
                "rilasci": self.rilasci.vista() if self.rilasci else None,
                "ponti_nuovi": self.riassunto_nuovi(), "inviti": [self.vista_invito(i) for i in inviti],
                "installazioni": installazioni, "imager": f"{GITHUB}/{REPO}/releases/latest/download/imager.json",
                "icone": self.vista_icone(ponti)}

    def _approva(self, f: str, r: dict, utente: str, sostituisci_ok: bool,
                 installa: Optional[dict] = None) -> Tuple[int, dict]:
        if not r.get("chiave"):
            # (ponte tolto prima che la registrazione avesse la chiave): via il rifiuto, il Raspberry richiede
            togli(f)
            return 200, {"esito": "riammessa", "messaggio": f"Fatto: il Raspberry {r.get('nome')} ripete la "
                                                            "richiesta entro un'ora, poi la approvi qui."}
        if installa is None and isinstance(r.get("installa"), dict):
            installa = r["installa"]          # scelte dell'invito: "Riprova" dopo un errore
        if installa is not None:
            if not r.get("nuovo"):
                return 400, {"errore": "questo Raspberry e' stato installato a mano: approvalo senza installazione"}
            if not self.riassunto_nuovi().get("pronto"):
                return 409, {"errore": "prima carica certificato e canale per i ponti nuovi"}
        sostituisci = r["nome"] in self.ponti()
        if sostituisci and not sostituisci_ok:
            return 409, {"errore": f"c'e' gia' un ponte '{r['nome']}' nella rete: ricarica la pagina e scegli "
                                   "'Sostituisci' se questo Raspberry deve prenderne il posto"}
        lavoro = self.accoda("aggiungi", r["nome"], utente, r["chiave"], r["id"], sostituisci=sostituisci,
                             installa=installa)
        r.update(stato="in_corso", lavoro=lavoro, approvata=int(time.time()), da=utente, errore=None, dettagli=None)
        if installa is not None:
            r["installa"] = installa
            self._salva_installazione(r["nome"], installa, utente)
        r.pop("fermo", None)
        self._scrivi_json(f, r)
        return 202, {"esito": "in_corso", "lavoro": lavoro, "messaggio": f"Sto aggiungendo il ponte {r['nome']}." +
                     (" Poi il Raspberry si installa da solo: qui vedi a che punto e'." if installa else "")}

    def azione(self, percorso: str, dati, utente: str) -> Tuple[int, dict]:
        if not isinstance(dati, dict):
            return 400, {"errore": "richiesta non valida"}
        if percorso == "/api/prova-avvisi":     # fuori dal blocco: aspetta la console di infra-TAK
            if not self.sorveglianza:
                return 503, {"errore": "avvisi non attivi"}
            return self.sorveglianza.prova(utente)
        if percorso == "/api/controlla-aggiornamenti":
            if not self.rilasci:
                return 503, {"errore": "aggiornamenti non attivi"}
            fatto, _ = self.rilasci.controlla(forza=True)
            if not fatto:
                return 429, {"errore": "ho appena controllato: riprova tra un minuto"}
            return 200, {"rilasci": self.rilasci.vista()}
        if percorso in ("/api/aggiorna-server", "/api/aggiorna-ponti"):
            versione = str(dati.get("versione") or "")
            if not self.rilasci or not RE_VERSIONE_RILASCIO.match(versione) or not self.rilasci.pronta(versione):
                return 409, {"errore": "versione non disponibile: ricarica la pagina"}
            with self.lock:
                if percorso == "/api/aggiorna-server":
                    if numeri_versione(versione) <= numeri_versione(VERSIONE):
                        return 409, {"errore": f"il server ha gia' la {VERSIONE}"}
                    if any(lv.get("azione") == "aggiorna-server" and lv.get("esito") in ("in_coda", "in_corso")
                           for lv in self.lavori()):
                        return 409, {"errore": "l'aggiornamento del server e' gia' in corso"}
                    return 202, {"esito": "in_corso", "lavoro": self.accoda("aggiorna-server", "server", utente,
                                                                            versione=versione),
                                 "messaggio": f"Aggiorno il server alla {versione}: per un minuto la pagina non "
                                              "risponde, poi riprende da sola."}
                if numeri_versione(versione) > numeri_versione(VERSIONE):
                    return 409, {"errore": f"prima aggiorna il server alla {versione}"}
                nome = str(dati.get("nome") or "tutti")
                if nome != "tutti" and nome not in self.ponti():
                    return 404, {"errore": "ponte non trovato: ricarica la pagina"}
                return 202, {"esito": "in_corso", "lavoro": self.accoda("aggiorna-ponti", nome, utente,
                                                                        versione=versione),
                             "messaggio": f"Chiedo ai ponti di aggiornarsi alla {versione}: lo fanno da soli entro "
                                          "qualche minuto."}
        if percorso == "/api/invita":
            return self.nuovo_invito(dati, utente)
        if percorso == "/api/icona":
            return self.imposta_icona(dati, utente)
        if percorso == "/api/impostazioni-nuovi":
            try:
                impostazioni = controlla_impostazioni(dati)
            except Errore as e:
                return 400, {"errore": str(e)}
            with self.lock:
                if any(lv.get("azione") == "impostazioni-nuovi" and lv.get("esito") in ("in_coda", "in_corso")
                       for lv in self.lavori()):
                    return 409, {"errore": "sto gia' salvando le impostazioni: aspetta un momento"}
                lavoro = self.accoda("impostazioni-nuovi", "ponti-nuovi", utente, impostazioni=impostazioni)
            return 202, {"esito": "in_corso", "lavoro": lavoro,
                         "messaggio": "Controllo e salvo certificato e canale per i ponti nuovi."}
        with self.lock:
            if percorso == "/api/annulla-invito":
                iid = str(dati.get("id", ""))
                f = P(f"{INVITI_DIR}/{iid}.json") if RE_IMPRONTA.match(iid) else ""
                inv = self._leggi_json(f) if f else None
                if not inv or inv.get("stato") != "attesa":
                    return 404, {"errore": "invito non trovato (gia' usato o scaduto?): ricarica la pagina"}
                togli(f)
                print(f"invito del ponte '{inv.get('nome')}' annullato da {utente}", flush=True)
                return 200, {"esito": "annullato", "messaggio": f"Invito per {inv.get('nome')} annullato: quella "
                                                                "chiave non vale piu'."}
            if percorso == "/api/riprova-installazione":
                nome = str(dati.get("nome", ""))
                if not RE_NOME.match(nome) or nome not in self.ponti():
                    return 404, {"errore": "ponte non trovato: ricarica la pagina"}
                av = self.avanzamento(nome)
                if not av or av["fase"] == "fatto":
                    return 409, {"errore": f"il ponte {nome} non sta installando: niente da riprovare"}
                installa = {}
                if dati.get("scheda"):
                    if dati["scheda"] not in SCHEDE:
                        return 400, {"errore": "radio del ponte non prevista"}
                    installa["scheda"] = dati["scheda"]
                if "posizione" in dati:
                    try:
                        installa["posizione"] = valida_posizione(str(dati.get("posizione") or ""))
                    except Errore as e:
                        return 400, {"errore": str(e)}
                if any(lv.get("azione") == "installa" and lv.get("nome") == nome and lv.get("esito") in
                       ("in_coda", "in_corso") for lv in self.lavori()):
                    return 409, {"errore": "l'ho gia' chiesto: il Raspberry riprova entro un paio di minuti"}
                lavoro = self.accoda("installa", nome, utente, installa=installa)
                prima = self._leggi_json(P(f"{INSTALLAZIONI_DIR}/{nome}.json")) or {}
                self._salva_installazione(nome, {"scheda": installa.get("scheda", prima.get("scheda") or ""),
                                                 "posizione": installa.get("posizione", prima.get("posizione") or "")},
                                          utente)
                return 202, {"esito": "in_corso", "lavoro": lavoro,
                             "messaggio": f"Chiesto: il Raspberry di {nome} riprova l'installazione entro un paio di "
                                          "minuti."}
            if percorso in ("/api/approva", "/api/rifiuta", "/api/riammetti"):
                rid = str(dati.get("id", ""))
                f = P(f"{RICHIESTE_DIR}/{rid}.json") if RE_IMPRONTA.match(rid) else ""
                r = self._leggi_json(f) if f else None
                attesa = "rifiutata" if percorso == "/api/riammetti" else "attesa"
                if r and r.get("stato") == "in_corso":
                    return 409, {"errore": "lo sto gia' aggiungendo"}
                if not r or r.get("stato") != attesa:
                    return 404, {"errore": "richiesta non trovata (gia' approvata, rifiutata o scaduta?): "
                                           "ricarica la pagina"}
                if percorso == "/api/rifiuta":
                    r.update(stato="rifiutata", motivo="rifiutata", ultima=int(time.time()), da=utente)
                    self._scrivi_json(f, r)
                    print(f"richiesta del ponte '{r.get('nome')}' rifiutata da {utente}", flush=True)
                    return 200, {"esito": "rifiutata", "messaggio": f"Rifiutata: il Raspberry {r.get('nome')} non "
                                                                    "rientra da solo."}
                installa = None
                if percorso == "/api/approva" and isinstance(dati.get("installa"), dict):
                    # Raspberry nuovo arrivato senza una chiave d'invito valida: l'amministratore sceglie lui
                    d = dati["installa"]
                    try:
                        nome = valida_nome(str(d.get("nome") or r.get("nome") or ""))
                        installa = {"scheda": str(d.get("scheda") or ""),
                                    "posizione": valida_posizione(str(d.get("posizione") or ""))}
                    except Errore as e:
                        return 400, {"errore": str(e)}
                    if installa["scheda"] not in SCHEDE:
                        return 400, {"errore": "scegli la radio del ponte"}
                    r["nome"] = nome
                return self._approva(f, r, utente, dati.get("sostituisci") is True, installa)
            if percorso == "/api/aggiungi":
                try:
                    nome = valida_nome(str(dati.get("nome", "")))
                    chiave, impronta = leggi_chiave(str(dati.get("chiave", "")))
                except Errore as e:
                    return 400, {"errore": str(e)}
                if nome in self.ponti():
                    return 409, {"errore": f"il nome '{nome}' e' gia' di un ponte della rete"}
                if self.registrato(impronta):
                    return 409, {"errore": "questa chiave e' gia' di un ponte della rete"}
                togli(P(f"{RICHIESTE_DIR}/{impronta}.json"))   # aggiunto a mano: vale piu' di un rifiuto
                return 202, {"esito": "in_corso", "lavoro": self.accoda("aggiungi", nome, utente, chiave)}
            if percorso == "/api/rimuovi":
                nome = str(dati.get("nome", ""))
                if not RE_NOME.match(nome) or nome not in self.ponti():
                    return 404, {"errore": "ponte non trovato: ricarica la pagina"}
                lavoro = self.accoda("rimuovi", nome, utente)
                togli(P(f"{INSTALLAZIONI_DIR}/{nome}.json"))
                impronta, chiave = self.registrazione_di(nome)
                if impronta:
                    # se il Raspberry e' ancora acceso non rientra da solo: per riprenderlo, "Lascia rientrare"
                    ora = int(time.time())
                    self._scrivi_json(P(f"{RICHIESTE_DIR}/{impronta}.json"), {
                        "id": impronta, "nome": nome, "chiave": chiave, "controllo": codice_controllo(impronta),
                        "stato": "rifiutata", "motivo": "tolto", "da": utente, "arrivata": ora, "ultima": ora,
                        "macchina": "", "ip": "", "versione": ""})
                return 202, {"esito": "in_corso", "lavoro": lavoro, "messaggio": f"Sto togliendo il ponte {nome}."}
        return 404, {"errore": "azione sconosciuta"}


CORPO_IMPOSTAZIONI_MAX = 400 << 10       # pacchetto del certificato (fino a 256 kB) in base64, con il resto


def gestore_per(servizio: Servizio):
    class Gestore(http.server.BaseHTTPRequestHandler):
        server_version = "mesh2tak-rete"
        sys_version = ""
        timeout = 15

        def log_message(self, *a):
            pass

        def _manda(self, codice: int, dati: dict) -> None:
            corpo = json.dumps(dati, ensure_ascii=False).encode("utf-8")
            self.send_response(codice)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

        def _corpo(self, massimo: int = 16384):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > massimo:
                return None, 413
            try:
                return json.loads(self.rfile.read(n).decode("utf-8") or "null"), 0
            except (ValueError, UnicodeDecodeError):
                return None, 400

        def _utente(self) -> str:
            """L'amministratore che Authentik ha fatto entrare (lo aggiunge Caddy, dopo aver tolto quello del
            browser). Senza, la richiesta non e' passata da Authentik."""
            u = (self.headers.get("X-Authentik-Username") or "").strip()
            return u if RE_UTENTE_AK.match(u) else ""

        def _ip(self) -> str:
            ip = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            return ip if re.match(r"^[0-9a-fA-F:.]{2,45}$", ip) else self.client_address[0]

        def do_GET(self):  # noqa: N802
            percorso = urllib.parse.urlsplit(self.path).path
            if percorso == "/api/stato":
                utente = self._utente()
                if not utente:
                    return self._manda(403, {"errore": "serve l'accesso da Authentik"})
                return self._manda(200, servizio.stato(utente))
            self._manda(404, {"errore": "non trovato"})

        def do_POST(self):  # noqa: N802
            percorso = urllib.parse.urlsplit(self.path).path
            # il certificato dei ponti nuovi (dalla 1.10.0) e' piu' grande delle altre richieste
            dati, errore = self._corpo(CORPO_IMPOSTAZIONI_MAX if percorso == "/api/impostazioni-nuovi" else 16384)
            if errore:
                return self._manda(errore, {"errore": "richiesta non valida"})
            if percorso == "/accesso/richiesta":
                codice, risposta = servizio.nuova_richiesta(dati, self._ip())
                return self._manda(codice, risposta)
            if percorso.startswith("/api/"):
                utente = self._utente()
                if not utente:
                    return self._manda(403, {"errore": "serve l'accesso da Authentik"})
                # contro i siti che provano a far cliccare il browser dell'amministratore
                origine = urllib.parse.urlsplit(self.headers.get("Origin") or "").netloc
                if self.headers.get("X-Mesh2tak") != "1" or (origine and origine != self.headers.get("Host")):
                    return self._manda(403, {"errore": "richiesta non accettata"})
                codice, risposta = servizio.azione(percorso, dati, utente)
                return self._manda(codice, risposta)
            self._manda(404, {"errore": "non trovato"})

    return Gestore


def cmd_servi(a) -> int:
    sorveglianza = Sorveglianza()
    rilasci = Rilasci()
    servizio = Servizio(sorveglianza, rilasci)
    sorveglianza.dopo = servizio.allinea_icone      # dalla 1.11.0: icone sulla mappa ai ponti, dal tunnel
    threading.Thread(target=sorveglianza.ciclo, name="avvisi", daemon=True).start()
    threading.Thread(target=rilasci.ciclo, name="rilasci", daemon=True).start()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORTA_WEB), gestore_per(servizio))
    server.daemon_threads = True
    print(f"servizio della pagina della rete in ascolto su 127.0.0.1:{PORTA_WEB} (mesh2tak {VERSIONE})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="rete-meshtastic.sh", description="Rete Meshtastic sul server infra-TAK "
                                 f"(mesh2tak {VERSIONE})")
    sotto = ap.add_subparsers(dest="comando", metavar="COMANDO")
    s = sotto.add_parser("installa", help="una volta sola: pagina della rete e tutto quello che serve")
    s.add_argument("--ignora-dns", action="store_true", help="vai avanti anche se il DNS non e' ancora giusto")
    s.set_defaults(fn=cmd_installa)
    s = sotto.add_parser("aggiungi", help="registra o aggiorna un ponte a mano")
    s.add_argument("nome", help="nome del ponte, es. casa")
    s.add_argument("chiave", help="chiave pubblica del Raspberry ('ssh-ed25519 AAAA...'), tra apici")
    s.add_argument("--ignora-dns", action="store_true", help="vai avanti anche se il DNS non e' ancora giusto")
    s.set_defaults(fn=cmd_aggiungi)
    s = sotto.add_parser("elenco", help="ponti registrati e tunnel collegati")
    s.set_defaults(fn=cmd_elenco)
    s = sotto.add_parser("rimuovi", help="toglie un ponte")
    s.add_argument("nome")
    s.add_argument("--si", action="store_true", help="senza chiedere conferma")
    s.set_defaults(fn=cmd_rimuovi)
    s = sotto.add_parser("aggiorna", help="come installa, dopo aver aggiornato lo script")
    s.add_argument("--ignora-dns", action="store_true", help="vai avanti anche se il DNS non e' ancora giusto")
    s.set_defaults(fn=cmd_aggiorna)
    s = sotto.add_parser("disinstalla", help="toglie tutto quello che ha messo questo script")
    s.add_argument("--si", action="store_true", help="senza chiedere conferma")
    s.set_defaults(fn=cmd_disinstalla)
    s = sotto.add_parser("servi", help="(lo usa il servizio della pagina, non lanciarlo a mano)")
    s.set_defaults(fn=cmd_servi)
    s = sotto.add_parser("lavora", help="(lo usa il servizio dei lavori, non lanciarlo a mano)")
    s.set_defaults(fn=cmd_lavora)
    s = sotto.add_parser("ponte", help="(lo lancia sshd per i Raspberry dei ponti, non lanciarlo a mano)")
    s.set_defaults(fn=cmd_ponte)
    a = ap.parse_args(argv)
    if not getattr(a, "fn", None):
        ap.print_help()
        return 2
    if a.comando in ("servi", "ponte"):
        return a.fn(a)                                         # senza privilegi, niente registro di root
    if os.geteuid() != 0 and not RADICE:
        print("Va avviato da root:  sudo bash rete-meshtastic.sh " + " ".join(sys.argv[1:2]))
        return 1
    registra(f"=== rete-meshtastic {VERSIONE}: {a.comando}")
    try:
        return a.fn(a)
    except Errore as e:
        registra(f"ERRORE: {e}")
        print(f"\n{R}✘ {e}{N}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrotto.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
