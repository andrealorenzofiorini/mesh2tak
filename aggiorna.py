#!/usr/bin/env python3
"""mesh2tak-aggiorna: quello che la pagina della rete chiede al Raspberry (lo avvia un timer, da root).

Ogni due minuti, e subito quando il server lo avvisa dal tunnel (dalla 1.13.0), legge la registrazione del ponte sulla
pagina della rete (la stessa che il ponte legge per il tunnel).

Comandi (dalla 1.13.0, "Gestisci il Raspberry"): riavvio del programma, della radio o del Raspberry, registro per la
pagina, impostazioni del ponte, certificato nuovo del TAK Server (scaricato dal server via SSH, con la chiave del
ponte), radio come all'installazione. Ognuno una volta sola; scade dopo 30 minuti (Raspberry spento intanto). Gli esiti
sono in /var/lib/mesh2tak-aggiorna/azioni.json, che il ponte mostra alla pagina.

Aggiornamento: se un amministratore ha chiesto una versione piu' nuova di quella installata:
  1. scarica l'installatore di quella versione dalla release ufficiale su GitHub e ne controlla l'impronta
     (SHA256SUMS della stessa release): il server della rete puo' solo chiedere, il programma arriva da GitHub;
  2. lo lancia (tiene configurazione, certificato, canale, approvazione e, dalla 1.13.0, le impostazioni della radio;
     con "Reimposta anche la radio" la rimette come all'installazione);
  3. controlla che il ponte riparta con la versione nuova, altrimenti rimette quella di prima.
Mai verso una versione piu' vecchia. Lo stato per la pagina della rete e' in /var/lib/mesh2tak-aggiorna/stato.json.
"""
import base64
import binascii
import configparser
import fcntl
import grp
import hashlib
import json
import math
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

VERSIONE = "@@VERSIONE@@"
REPO = "andrealorenzofiorini/mesh2tak"
SCARICA = os.environ.get("MESH2TAK_PROVA_GITHUB", "https://github.com").rstrip("/")   # le prove: un GitHub finto
CONF = os.environ.get("MESH2TAK_AGGIORNA_CONF", "/etc/mesh2tak/mesh2tak.conf")
DIR = os.environ.get("MESH2TAK_AGGIORNA_DIR", "/opt/mesh2tak")
STATO_DIR = os.environ.get("MESH2TAK_AGGIORNA_STATO", "/var/lib/mesh2tak-aggiorna")
LOG = os.environ.get("MESH2TAK_AGGIORNA_LOG", "/var/log/mesh2tak-aggiornamento.log")
LOG_INSTALLAZIONE = os.environ.get("MESH2TAK_AGGIORNA_LOG_INST", "/var/log/mesh2tak-installazione.log")
AVVIO = os.environ.get("MESH2TAK_AGGIORNA_AVVIO", "/proc/sys/kernel/random/boot_id")   # cambia a ogni accensione
SSH = os.environ.get("MESH2TAK_PROVA_SSH", "ssh")
INSTALLATORE = "installa-ponte-meshtastic.sh"
RE_VERSIONE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$")
TENTATIVI = 3                       # per ogni richiesta della pagina
RIPROVA_S = 600                     # tra un tentativo non riuscito e il successivo
ATTESA_PONTE_S = float(os.environ.get("MESH2TAK_AGGIORNA_ATTESA", "180"))
ATTESA_RADIO_S = float(os.environ.get("MESH2TAK_AGGIORNA_ATTESA_RADIO", "10"))
DURATA_MAX_S = 45 * 60              # un'installazione che dura di piu' si e' bloccata (il timer ne aspetta 2)


class Errore(Exception):
    pass


def registra(t: str) -> None:
    try:
        if os.path.getsize(LOG) > 5 << 20:          # registro mai oltre 5 MB: tiene l'ultimo
            with open(LOG, "rb") as f:
                f.seek(-(1 << 20), os.SEEK_END)
                coda = f.read()
            with open(LOG, "wb") as f:
                f.write(coda[coda.find(b"\n") + 1:])
    except OSError:
        pass
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + t + "\n")
    except OSError:
        pass


def registra_una_volta(t: str) -> None:
    """Per i controlli ogni 2 minuti: la stessa riga una volta sola, finche' non cambia."""
    f = os.path.join(STATO_DIR, "ultimo-avviso")
    try:
        with open(f, encoding="utf-8") as h:
            if h.read() == t:
                return
    except OSError:
        pass
    registra(t)
    try:
        with open(f, "w", encoding="utf-8") as h:
            h.write(t)
    except OSError:
        pass


def numeri(v: str) -> Tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def configurazione() -> configparser.ConfigParser:
    """Letta come la legge il ponte (commenti in fondo alla riga, si/no)."""
    cfg = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"))
    try:
        cfg.read(CONF, encoding="utf-8")
    except (OSError, configparser.Error):
        pass
    return cfg


def si(cfg: configparser.ConfigParser, sezione: str, chiave: str) -> bool:
    return cfg.get(sezione, chiave, fallback="").strip().lower() in ("si", "sì", "s", "yes", "y", "true", "1", "on")


def versione_installata() -> Optional[str]:
    try:
        with open(os.path.join(DIR, "mesh2tak.py"), encoding="utf-8") as f:
            m = re.search(r'^VERSIONE = "([^"]+)"', f.read(), re.M)
    except OSError:
        return None
    return m.group(1) if m and RE_VERSIONE.match(m.group(1)) else None


def leggi_json(nome: str) -> dict:
    try:
        with open(os.path.join(STATO_DIR, nome), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def scrivi_json(nome: str, dati: dict) -> None:
    os.makedirs(STATO_DIR, exist_ok=True)
    os.chmod(STATO_DIR, 0o755)                # il ponte (utente mesh2tak) lo legge per la pagina della rete
    f = os.path.join(STATO_DIR, nome)
    with open(f + ".nuovo", "w", encoding="utf-8") as h:
        json.dump(dati, h, ensure_ascii=False)
    os.chmod(f + ".nuovo", 0o644)
    os.replace(f + ".nuovo", f)


def leggi_stato() -> dict:
    return leggi_json("stato.json")


def scrivi_stato(**dati) -> dict:
    dati["quando"] = int(time.time())
    scrivi_json("stato.json", dati)
    return dati


def scarica(url: str, massimo: int, contesto: Optional[ssl.SSLContext] = None) -> bytes:
    apri = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                       urllib.request.HTTPSHandler(context=contesto or ssl.create_default_context()))
    richiesta = urllib.request.Request(url, headers={"User-Agent": f"mesh2tak-aggiorna/{VERSIONE}"})
    try:
        with apri.open(richiesta, timeout=60) as r:
            dati = r.read(massimo + 1)
    except urllib.error.HTTPError as e:
        raise Errore(f"{url} risponde {e.code}")
    except (urllib.error.URLError, OSError) as e:
        raise Errore(f"non riesco a scaricare {url} ({getattr(e, 'reason', e)})")
    if len(dati) > massimo:
        raise Errore(f"{url}: file troppo grande")
    return dati


def impronta_chiave(riga: str) -> str:
    """Come il ponte e il server: sha256 della chiave pubblica, nome del file della registrazione."""
    parti = riga.split()
    if len(parti) < 2:
        raise Errore("chiave pubblica del ponte non valida")
    try:
        return hashlib.sha256(base64.b64decode(parti[1], validate=True)).hexdigest()
    except (binascii.Error, ValueError):
        raise Errore("chiave pubblica del ponte non valida")


def leggi_registrazione() -> Optional[dict]:
    """La registrazione del ponte pubblicata dalla pagina della rete; None se non c'e' (o il ponte non e' nella rete)."""
    cfg = configurazione()
    if not si(cfg, "accesso_esterno", "attivo"):
        return None
    rete = cfg.get("accesso_esterno", "rete", fallback="").strip().rstrip("/")
    chiave = cfg.get("accesso_esterno", "chiave", fallback="/etc/mesh2tak/accesso/chiave").strip()
    if not re.match(r"^https://[A-Za-z0-9.-]+(:\d+)?$", rete):
        return None
    try:
        with open(chiave + ".pub", encoding="ascii") as f:
            impronta = impronta_chiave(f.read())
    except (OSError, UnicodeDecodeError, Errore):
        return None
    try:
        reg = json.loads(scarica(f"{rete}/accesso/{impronta}.json", 65536).decode("utf-8"))
    except (Errore, ValueError, UnicodeDecodeError) as e:
        registra_una_volta(f"registrazione non letta: {e}")       # anche 404: ponte non (ancora) nella rete
        return None
    registra_una_volta("registrazione letta")
    return reg if isinstance(reg, dict) else None


def richiesta_aggiornamento(reg: Optional[dict]) -> Optional[Tuple[str, float, bool]]:
    """(versione chiesta, quando, radio da reimpostare) dalla registrazione del ponte; None se non c'e' niente."""
    r = reg.get("aggiorna") if isinstance(reg, dict) else None
    if not isinstance(r, dict) or not RE_VERSIONE.match(str(r.get("versione", ""))):
        return None
    chiesto = r.get("chiesto")
    return (str(r["versione"]), float(chiesto) if isinstance(chiesto, (int, float)) else 0.0,
            r.get("reimposta") is True)


def ponte_sano(versione: Optional[str]) -> bool:
    """Il ponte e' ripartito con quella versione: servizio attivo e la sua pagina risponde con quella versione
    (senza pagina: servizio attivo e quella versione installata)."""
    cfg = configurazione()
    try:
        porta = cfg.getint("gestione", "porta_pagina", fallback=9443) if si(cfg, "gestione", "attiva") else 0
    except ValueError:
        porta = 0
    contesto = ssl._create_unverified_context()   # la pagina del ponte, su questo stesso Raspberry
    fine = time.time() + ATTESA_PONTE_S
    while True:
        attivo = subprocess.run(["systemctl", "is-active", "--quiet", "mesh2tak"], timeout=30).returncode == 0
        if attivo and porta:
            try:
                d = json.loads(scarica(f"https://127.0.0.1:{porta}/mesh2tak/stato.json", 4 << 20, contesto))
                if isinstance(d, dict) and (versione is None or d.get("versione") == versione):
                    return True
            except (Errore, ValueError):
                pass
        elif attivo and not porta:
            return versione is None or versione_installata() == versione
        if time.time() >= fine:
            return False
        time.sleep(5)


def lancia(script: str, argomenti: Tuple[str, ...] = (), ambiente: Optional[Dict[str, str]] = None,
           durata_s: float = DURATA_MAX_S) -> Tuple[int, str]:
    registra(f"lancio {script} {' '.join(argomenti)}".rstrip())
    try:
        r = subprocess.run(["bash", script, *argomenti], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           errors="replace", timeout=durata_s, cwd="/root" if os.path.isdir("/root") else "/",
                           env=dict(os.environ, MESH2TAK_DA_AGGIORNAMENTO="1", **(ambiente or {})))
    except subprocess.TimeoutExpired:
        return 124, "l'installazione non ha finito in tempo"
    testo = re.sub(r"\x1b\[[0-9;]*m", "", (r.stdout or "") + (r.stderr or ""))
    registra(testo[-200000:])
    return r.returncode, testo


def ultima_riga(testo: str) -> str:
    """La riga dell'errore (l'installatore la comincia con ✘), altrimenti l'ultima."""
    righe = [r.strip() for r in testo.strip().splitlines() if r.strip()]
    utili = [r for r in righe if r.startswith(("✘", "Errore", "ERRORE"))]
    return (utili or righe or ["nessun messaggio"])[-1].strip(" ✘!")[:300]


def aggiorna(versione: str, prima: Optional[str], reimposta: bool = False) -> Tuple[str, str]:
    """Esito (fatto, errore, ripristinato) e messaggio."""
    base = f"{SCARICA}/{REPO}/releases/download/v{versione}"
    somme = scarica(f"{base}/SHA256SUMS", 65536).decode("utf-8", "replace")
    script = scarica(f"{base}/{INSTALLATORE}", 16 << 20)
    attesa = None
    for riga in somme.splitlines():
        parti = riga.split()
        if len(parti) == 2 and parti[1].lstrip("*") == INSTALLATORE:
            attesa = parti[0].lower()
    if not attesa or hashlib.sha256(script).hexdigest() != attesa:
        raise Errore("il file scaricato non e' quello pubblicato nella release: non lo installo")
    if f'VERSIONE="{versione}"'.encode() not in script[:8192]:
        raise Errore(f"il file scaricato non e' la versione {versione}: non lo installo")
    os.makedirs(STATO_DIR, exist_ok=True)
    nuovo = os.path.join(STATO_DIR, f"installatore-{versione}.sh")
    with open(os.open(nuovo, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700), "wb") as f:
        f.write(script)
    # l'installatore di adesso: se la versione nuova non riparte, si torna a questo
    precedente = os.path.join(STATO_DIR, "installatore-precedente.sh")
    salvato = os.path.join(DIR, "installatore.sh")
    if os.path.isfile(salvato):
        with open(salvato, "rb") as s, open(os.open(precedente, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700), "wb") as d:
            d.write(s.read())
    elif os.path.exists(precedente):
        os.remove(precedente)
    try:
        # dalla 1.13.0 l'installatore lascia la radio com'e'; con "Reimposta anche la radio" la rimette come all'inizio
        esito, testo = lancia(nuovo, ambiente={"MESH2TAK_REIMPOSTA_RADIO": "1"} if reimposta else None)
    finally:
        os.remove(nuovo)                      # se e' andato bene, l'installatore l'ha gia' copiato in DIR
    if esito == 0 and ponte_sano(versione):
        return "fatto", f"aggiornato dalla {prima or '?'} alla {versione}" + (" e radio reimpostata" if reimposta else "")
    motivo = (f"l'installazione della {versione} non e' riuscita: {ultima_riga(testo)}" if esito != 0
              else f"con la {versione} il ponte non riparte")
    if not prima:
        return "errore", motivo
    if versione_installata() == prima and ponte_sano(prima):
        return "errore", f"{motivo}; resta la {prima}"          # fermato prima di cambiare il programma
    if not os.path.isfile(precedente):
        return "errore", f"{motivo}; manca l'installatore della {prima} per tornare indietro"
    e2, t2 = lancia(precedente)
    if e2 == 0 and ponte_sano(prima):
        return "ripristinato", f"{motivo}; rimessa la {prima}"
    return "errore", (f"{motivo}; anche il ritorno alla {prima} non e' riuscito: "
                      + (ultima_riga(t2) if e2 else "il ponte non riparte"))


# --------------------------------------------------------------------------
# Comandi dalla pagina della rete (dalla 1.13.0): "Gestisci il Raspberry"
# --------------------------------------------------------------------------

AZIONI = ("riavvia-programma", "riavvia-radio", "riavvia-raspberry", "registro", "impostazioni", "certificato",
          "reimposta-radio")
IN_CORSO = {"riavvia-programma": "riavvio il programma del ponte", "riavvia-radio": "riavvio la radio",
            "riavvia-raspberry": "il Raspberry si riavvia: torna in linea in 2-3 minuti",
            "registro": "raccolgo il registro", "impostazioni": "salvo le impostazioni e riavvio il programma",
            "certificato": "installo il certificato nuovo: qualche minuto, intanto il ponte si ferma",
            "reimposta-radio": "reimposto la radio: un paio di minuti, intanto il ponte si ferma"}
RE_ID_AZIONE = re.compile(r"^[0-9a-f]{16}$")
AZIONE_SCADE_S = 30 * 60            # un comando arrivato a Raspberry spento: dopo mezz'ora non vale piu'
ESITI = "azioni.json"               # gli ultimi esiti, per la pagina (li legge il ponte)
ESITI_MOSTRATI = 12
ESITI_RICORDATI = 200               # comandi gia' visti: non si rifanno anche se restano nella registrazione
REGISTRO_PAGINA = "registro.txt"    # il registro per la pagina: lo legge solo il ponte (gruppo mesh2tak)
SQUADRE = ("White", "Yellow", "Orange", "Magenta", "Red", "Maroon", "Purple", "Dark Blue", "Blue", "Cyan", "Teal",
           "Green", "Dark Green", "Brown")
# Le impostazioni del ponte che si cambiano dalla pagina: sezione di mesh2tak.conf, tipo, minimo e massimo
IMPOSTAZIONI = {
    "squadra": ("mappa", "squadra", None, None),
    "validita_min": ("mappa", "numero", 5, 1440),
    "aggiornamento_min": ("mappa", "numero", 0.5, 60),
    "errore_massimo_m": ("mappa", "numero", 10, 100000),
    "includi_ponte": ("mappa", "si_no", None, None),
    "invio": ("radio", "si_no", None, None),
    "tempo_radio_percento": ("radio", "numero", 0.5, 10),
    "pausa_massima_min": ("gestione", "numero", 1, 240),
}
RE_UTENTE = re.compile(r"^ponte-[a-z0-9-]{1,30}$")
RE_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
RE_CHIAVE_HOST = re.compile(r"^(ssh-ed25519|ecdsa-sha2-nistp(?:256|384|521)|rsa-sha2-(?:256|512)|ssh-rsa) "
                            r"[A-Za-z0-9+/]+={0,3}$")
RE_PACCHETTO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,78}\.(?:zip|p12|pfx)$")


def esegui(comando: List[str], attesa: float = 120) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(comando, capture_output=True, text=True, errors="replace", timeout=attesa,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(comando, 124, "", f"{comando[0]}: non ha finito in {attesa:.0f} s")
    except OSError as e:
        return subprocess.CompletedProcess(comando, 127, "", f"{comando[0]}: {e}")


def avvio_attuale() -> str:
    try:
        with open(AVVIO, encoding="ascii") as f:
            return f.read(64).strip()
    except (OSError, UnicodeDecodeError):
        return ""


def leggi_esiti() -> Tuple[List[dict], List[str]]:
    d = leggi_json(ESITI)
    esiti = [a for a in d.get("azioni", []) if isinstance(a, dict) and RE_ID_AZIONE.match(str(a.get("id", "")))]
    visti = [i for i in d.get("visti", []) if isinstance(i, str) and RE_ID_AZIONE.match(i)]
    return esiti, visti


def scrivi_esiti(esiti: List[dict], visti: List[str]) -> None:
    scrivi_json(ESITI, {"formato": 1, "azioni": esiti[-ESITI_MOSTRATI:], "visti": visti[-ESITI_RICORDATI:]})


def segna_azione(a: dict, stato: str, messaggio: str, **extra) -> None:
    esiti, visti = leggi_esiti()
    voce = next((e for e in esiti if e["id"] == a["id"]), None)
    if voce is None:
        voce = {"id": a["id"], "azione": a["azione"], "chiesto": int(a["chiesto"])}
        esiti.append(voce)
    voce.update(stato=stato, messaggio=messaggio[:400], quando=int(time.time()), **extra)
    if a["id"] not in visti:
        visti.append(a["id"])
    scrivi_esiti(esiti, visti)


def azioni_nuove(reg: dict) -> List[dict]:
    """I comandi della registrazione non ancora visti, dal piu' vecchio."""
    lista = reg.get("azioni")
    if not isinstance(lista, list):
        return []
    _, visti = leggi_esiti()
    nuove = []
    for a in lista[-20:]:
        if not isinstance(a, dict):
            continue
        ident, azione, chiesto = str(a.get("id", "")), a.get("azione"), a.get("chiesto")
        if (not RE_ID_AZIONE.match(ident) or azione not in AZIONI or isinstance(chiesto, bool)
                or not isinstance(chiesto, (int, float)) or ident in visti or any(x["id"] == ident for x in nuove)):
            continue
        valori = a.get("valori")
        nuove.append({"id": ident, "azione": azione, "chiesto": float(chiesto),
                      "valori": valori if isinstance(valori, dict) else {}})
    return sorted(nuove, key=lambda x: x["chiesto"])


def chiudi_interrotte() -> bool:
    """Comandi rimasti 'in corso' (il blocco e' libero: nessuno li sta facendo). Il riavvio del Raspberry e' fatto
    quando il Raspberry si e' acceso di nuovo; gli altri si sono interrotti a meta'. True se il Raspberry sta per
    riavviarsi (chiesto da poco, stessa accensione): allora non si comincia altro."""
    esiti, visti = leggi_esiti()
    avvio, cambiato, ora, in_attesa = avvio_attuale(), False, time.time(), False
    for a in esiti:
        if a.get("stato") != "in_corso":
            continue
        if a.get("azione") == "riavvia-raspberry":
            if a.get("avvio") and avvio and a["avvio"] != avvio:
                acceso = acceso_alle()
                a.update(stato="fatto", messaggio="Raspberry riavviato", quando=int(acceso or ora))
            elif ora - float(a.get("quando") or 0) > 600:
                a.update(stato="errore", messaggio="il Raspberry non si e' riavviato", quando=int(ora))
            else:
                in_attesa = True
                continue
        else:
            a.update(stato="errore", messaggio="interrotto a meta' (Raspberry spento o riavviato?): riprova",
                     quando=int(ora))
        cambiato = True
    if cambiato:
        scrivi_esiti(esiti, visti)
    return in_attesa


def acceso_alle() -> Optional[float]:
    try:
        with open("/proc/uptime", encoding="ascii") as f:
            return time.time() - float(f.read(64).split()[0])
    except (OSError, ValueError, IndexError):
        return None


def esegui_azioni(reg: dict) -> bool:
    """Fa i comandi nuovi, uno alla volta; il riavvio del Raspberry per ultimo. True se il Raspberry si riavvia."""
    ora = time.time()
    riavvii = []
    for a in azioni_nuove(reg):
        if a["chiesto"] > ora + 300:
            registra_una_volta(f"comando {a['azione']} con l'ora piu' avanti di quella del Raspberry: aspetto che il "
                               "suo orologio si regoli da internet")
            continue
        if ora - a["chiesto"] > AZIONE_SCADE_S:
            segna_azione(a, "scaduto", "arrivato quando il Raspberry non rispondeva: chiesto piu' di 30 minuti fa, "
                                       "se serve ancora richiedilo")
            registra(f"comando {a['azione']} ({a['id']}) scaduto")
            continue
        if a["azione"] == "riavvia-raspberry":
            riavvii.append(a)
            continue
        segna_azione(a, "in_corso", IN_CORSO[a["azione"]])
        registra(f"=== comando dalla pagina: {a['azione']} ({a['id']})")
        try:
            esito, messaggio = FUNZIONI[a["azione"]](a, reg)
        except Errore as e:
            esito, messaggio = "errore", str(e)
        except Exception as e:  # noqa
            esito, messaggio = "errore", f"errore inatteso: {e}"
        registra(f"esito: {esito}: {messaggio}")
        segna_azione(a, esito, messaggio)
    if not riavvii:
        return False
    avvio = avvio_attuale()
    for a in riavvii:                        # piu' richieste insieme: un riavvio solo
        segna_azione(a, "in_corso", IN_CORSO["riavvia-raspberry"], avvio=avvio)
    registra(f"=== comando dalla pagina: riavvia-raspberry ({', '.join(a['id'] for a in riavvii)})")
    os.sync()
    r = esegui(["systemctl", "reboot"], 60)
    if r.returncode != 0:
        for a in riavvii:
            segna_azione(a, "errore", "il Raspberry non accetta il riavvio: " + ultima_riga(r.stdout + r.stderr))
        return False
    return True


def azione_riavvia_programma(a: dict, reg: dict) -> Tuple[str, str]:
    r = esegui(["systemctl", "restart", "mesh2tak"], 180)
    if r.returncode != 0:
        raise Errore("il programma del ponte non si riavvia: " + ultima_riga(r.stdout + r.stderr))
    if ponte_sano(versione_installata()):
        return "fatto", "programma del ponte riavviato"
    return "errore", "dopo il riavvio il programma del ponte non risponde: leggi il registro"


def azione_riavvia_radio(a: dict, reg: dict) -> Tuple[str, str]:
    collegamento = configurazione().get("meshtastic", "collegamento", fallback="").strip()
    if collegamento.startswith("serial:"):
        # radio USB: il ponte la tiene aperta, quindi si ferma mentre il comando di riavvio arriva alla radio
        esegui(["systemctl", "stop", "mesh2tak"], 180)
        try:
            r = esegui([os.path.join(DIR, "venv", "bin", "python"), os.path.join(DIR, "mesh2tak.py"), "--riavvia-nodo",
                        collegamento], 240)
        finally:
            esegui(["systemctl", "start", "mesh2tak"], 180)
        if "ESITO riavvio=ok" not in (r.stdout or ""):
            raise Errore("la radio USB non ha preso il comando: " + ultima_riga(r.stdout + r.stderr))
        return "fatto", "radio USB riavviata: il ponte si ricollega da solo entro un minuto"
    r = esegui(["systemctl", "restart", "meshtasticd"], 180)
    if r.returncode != 0:
        raise Errore("meshtasticd non si riavvia: " + ultima_riga(r.stdout + r.stderr))
    time.sleep(ATTESA_RADIO_S)
    if esegui(["systemctl", "is-active", "--quiet", "meshtasticd"], 30).returncode != 0:
        return "errore", "dopo il riavvio meshtasticd non e' attivo: leggi il registro"
    return "fatto", "radio riavviata (meshtasticd): il ponte si ricollega da solo entro un minuto"


def oscura(testo: str) -> str:
    """Il registro va sulla pagina: via chiavi, password e il link del canale (contiene la chiave)."""
    t = re.sub(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
               "[chiave privata tolta]", testo, flags=re.S)
    t = re.sub(r"(https?://(?:www\.)?meshtastic\.org/[ev]/)\S*", r"\1[canale tolto]", t)
    t = re.sub(r"(?i)\b(psk|password|passwd|pass|secret|token|m2t_pass)(\s*[:=]\s*)\S+", r"\1\2[tolto]", t)
    return re.sub(r"[A-Za-z0-9+/]{20,}={1,2}", "[tolto]", t)       # chiavi in base64 (es. quella di un canale)


def raccogli_registro() -> str:
    parti = [f"Registro del ponte (mesh2tak {versione_installata() or '?'}) del {time.strftime('%d/%m/%Y alle %H:%M')}",
             "Chiavi, password e link del canale sono tolti.", ""]

    def aggiungi(titolo: str, testo: str) -> None:
        testo = testo.rstrip()
        if len(testo) > 300_000:
            testo = "...\n" + testo[-300_000:]
        parti.extend([f"===== {titolo}", testo or "(niente)", ""])

    def comando(titolo: str, cmd: List[str]) -> None:
        r = esegui(cmd, 60)
        aggiungi(titolo, (r.stdout or "") + (r.stderr or ""))

    def coda(titolo: str, percorso: str, righe: int) -> None:
        try:
            with open(percorso, "rb") as f:
                f.seek(max(0, os.fstat(f.fileno()).st_size - 400_000))
                testo = f.read().decode("utf-8", "replace")
            aggiungi(titolo, "\n".join(testo.splitlines()[-righe:]))
        except OSError:
            aggiungi(titolo, "(nessun file)")

    comando("Servizi", ["systemctl", "--no-pager", "--lines=0", "status", "mesh2tak", "meshtasticd",
                        "mesh2tak-aggiorna.timer", "mesh2tak-aggiorna.path"])
    comando("Raspberry: acceso da, spazio, memoria, corrente",
            ["sh", "-c", "uptime; echo; df -h /; echo; free -m; command -v vcgencmd >/dev/null && vcgencmd get_throttled"])
    comando("Programma del ponte (mesh2tak), ultime 400 righe",
            ["journalctl", "-u", "mesh2tak", "-n", "400", "--no-pager", "-o", "short-iso"])
    comando("Radio (meshtasticd), ultime 200 righe",
            ["journalctl", "-u", "meshtasticd", "-n", "200", "--no-pager", "-o", "short-iso"])
    comando("Errori del sistema da quando e' acceso", ["journalctl", "-p", "err", "-b", "-n", "100", "--no-pager",
                                                       "-o", "short-iso"])
    coda("Aggiornamenti e comandi dalla pagina", LOG, 200)
    coda("Installazione", LOG_INSTALLAZIONE, 150)
    return oscura("\n".join(parti))


def scrivi_per_il_ponte(nome: str, testo: str) -> None:
    """Un file che legge solo il programma del ponte (gruppo mesh2tak), per la pagina della rete."""
    os.makedirs(STATO_DIR, exist_ok=True)
    f = os.path.join(STATO_DIR, nome)
    try:
        os.unlink(f + ".nuovo")
    except OSError:
        pass
    fd = os.open(f + ".nuovo", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    try:
        os.write(fd, testo.encode("utf-8"))
        try:
            os.fchown(fd, 0, grp.getgrnam("mesh2tak").gr_gid)
        except (KeyError, PermissionError):
            pass                              # le prove, senza root
        os.fchmod(fd, 0o640)
    finally:
        os.close(fd)
    os.replace(f + ".nuovo", f)


def azione_registro(a: dict, reg: dict) -> Tuple[str, str]:
    testo = raccogli_registro()
    scrivi_per_il_ponte(REGISTRO_PAGINA, testo)
    return "fatto", f"registro pronto ({testo.count(chr(10))} righe)"


def controlla_impostazioni(d) -> Dict[str, object]:
    """Solo le impostazioni previste, nei loro limiti: le manda la pagina, ma qui si controlla da capo."""
    if not isinstance(d, dict) or not d:
        raise Errore("nessuna impostazione da cambiare")
    for k, v in d.items():
        if k not in IMPOSTAZIONI:
            raise Errore(f"impostazione sconosciuta: {str(k)[:40]}")
        _, tipo, minimo, massimo = IMPOSTAZIONI[k]
        if tipo == "squadra":
            ok = v in SQUADRE
        elif tipo == "si_no":
            ok = isinstance(v, bool)
        else:
            ok = (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                  and minimo <= v <= massimo)
        if not ok:
            raise Errore(f"valore non valido per {k}")
    return dict(d)


def testo_valore(v) -> str:
    if isinstance(v, bool):
        return "si" if v else "no"
    if isinstance(v, (int, float)):
        return f"{float(v):.2f}".rstrip("0").rstrip(".")
    return str(v)


def cambia_impostazioni(testo: str, valori: Dict[str, object]) -> Tuple[str, List[str]]:
    """mesh2tak.conf con i valori nuovi; commenti e il resto restano com'erano. (testo nuovo, voci cambiate)"""
    righe = testo.splitlines(keepends=True)
    if righe and not righe[-1].endswith("\n"):
        righe[-1] += "\n"
    cambiate = []
    for chiave, valore in valori.items():
        sezione, nuovo = IMPOSTAZIONI[chiave][0], testo_valore(valore)
        attuale, dove, fine_sezione = None, None, None
        for i, r in enumerate(righe):
            m = re.match(r"^\s*\[([^\]]+)\]\s*$", r)
            if m:
                attuale = m.group(1).strip().lower()
                continue
            if attuale == sezione:
                fine_sezione = i
                if dove is None and re.match(rf"^\s*{chiave}\s*=", r):
                    dove = i
        if dove is not None:
            m = re.match(rf"^(\s*{chiave}\s*=\s*)([^#;\r\n]*?)(\s*[#;][^\r\n]*)?(\r?\n)$", righe[dove])
            if m and m.group(2) == nuovo:
                continue
            righe[dove] = (m.group(1) + nuovo + (m.group(3) or "") + m.group(4)) if m else f"{chiave} = {nuovo}\n"
        elif fine_sezione is not None:
            while fine_sezione > 0 and not righe[fine_sezione].strip():
                fine_sezione -= 1                # dopo l'ultima riga piena della sezione
            righe.insert(fine_sezione + 1, f"{chiave} = {nuovo}\n")
        else:
            righe.extend(["\n", f"[{sezione}]\n", f"{chiave} = {nuovo}\n"])
        cambiate.append(f"{chiave} = {nuovo}")
    return "".join(righe), cambiate


def scrivi_conf(testo: str) -> None:
    with open(CONF + ".nuovo", "w", encoding="utf-8") as f:
        f.write(testo)
    os.chmod(CONF + ".nuovo", 0o644)
    os.replace(CONF + ".nuovo", CONF)


def azione_impostazioni(a: dict, reg: dict) -> Tuple[str, str]:
    valori = controlla_impostazioni(a["valori"])
    try:
        with open(CONF, encoding="utf-8") as f:
            prima = f.read()
    except OSError as e:
        raise Errore(f"non leggo la configurazione del ponte ({e.strerror})")
    nuovo, cambiate = cambia_impostazioni(prima, valori)
    if not cambiate:
        return "fatto", "niente da cambiare: le impostazioni erano gia' queste"
    with open(CONF + ".prima-della-pagina", "w", encoding="utf-8") as f:
        f.write(prima)                        # quella di prima, anche per tornarci a mano
    scrivi_conf(nuovo)
    installata = versione_installata()
    esegui(["systemctl", "restart", "mesh2tak"], 180)
    if ponte_sano(installata):
        return "fatto", "impostazioni salvate: " + ", ".join(cambiate)
    scrivi_conf(prima)
    esegui(["systemctl", "restart", "mesh2tak"], 180)
    return "errore", "con le impostazioni nuove il ponte non ripartiva: rimesse quelle di prima"


def dati_ssh(reg: dict) -> dict:
    utente, host, porta, chiavi = (str(reg.get("utente", "")), str(reg.get("ssh_host", "")), reg.get("ssh_porta", 22),
                                   reg.get("chiavi_host"))
    if (not RE_UTENTE.match(utente) or not RE_HOST.match(host) or isinstance(porta, bool) or not isinstance(porta, int)
            or not 1 <= porta <= 65535 or not isinstance(chiavi, list) or not chiavi
            or not all(isinstance(c, str) and RE_CHIAVE_HOST.match(c.strip()) for c in chiavi)):
        raise Errore("registrazione del server non valida per SSH")
    return {"utente": utente, "host": host, "porta": porta, "chiavi": [c.strip() for c in chiavi]}


def chiedi_al_server(reg: dict, comando: str, attesa: float = 90) -> subprocess.CompletedProcess:
    """Il comando del ponte sul server della rete (utente del ponte, comando forzato), con la chiave del tunnel. Le
    chiavi del server arrivano con la registrazione, via HTTPS: niente fiducia alla cieca."""
    s = dati_ssh(reg)
    noti = os.path.join(STATO_DIR, "server-rete.known_hosts")
    with open(os.open(noti, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="ascii") as f:
        f.writelines(f"mesh2tak-rete {c}\n" for c in s["chiavi"])
    chiave = configurazione().get("accesso_esterno", "chiave", fallback="/etc/mesh2tak/accesso/chiave").strip()
    return esegui([SSH, "-F", "/dev/null", "-T", "-i", chiave, "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
                   "-o", "BatchMode=yes", "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
                   "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={noti}",
                   "-o", "GlobalKnownHostsFile=/dev/null", "-o", "HostKeyAlias=mesh2tak-rete", "-o", "UpdateHostKeys=no",
                   "-o", "CheckHostIP=no", "-o", "ConnectTimeout=20", "-o", "LogLevel=ERROR",
                   "-p", str(s["porta"]), f"{s['utente']}@{s['host']}", comando], attesa)


def azione_certificato(a: dict, reg: dict) -> Tuple[str, str]:
    script = os.path.join(DIR, "installatore.sh")
    if not os.path.isfile(script):
        raise Errore("sul Raspberry manca l'installatore: aggiorna il ponte dalla pagina, poi riprova")
    r = chiedi_al_server(reg, "certificato")
    if r.returncode == 3:
        raise Errore("sul server non c'e' un certificato da installare per questo ponte (gia' installato o scaduto): "
                     "caricalo di nuovo")
    if r.returncode != 0:
        raise Errore("il server della rete non ha dato il certificato (" + ultima_riga(r.stderr or r.stdout) + ")")
    try:
        c = json.loads(r.stdout)
        pacchetto = base64.b64decode(str(c["pacchetto"]), validate=True)
        nome, password, host = str(c["nome_pacchetto"]), str(c.get("password") or ""), str(c.get("host") or "")
    except (ValueError, KeyError, TypeError, binascii.Error):
        raise Errore("certificato ricevuto dal server non valido")
    if (not RE_PACCHETTO.match(nome) or not 0 < len(pacchetto) <= 1 << 20 or not password.isprintable()
            or len(password) > 128 or (host and not RE_HOST.match(host))):
        raise Errore("certificato ricevuto dal server non valido")
    host = host or configurazione().get("tak", "host", fallback="").strip()
    cartella = os.path.join(STATO_DIR, "certificato")
    shutil.rmtree(cartella, ignore_errors=True)
    os.makedirs(cartella, mode=0o700)
    file_pacchetto = os.path.join(cartella, nome)
    with open(os.open(file_pacchetto, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
        f.write(pacchetto)
    ambiente = {"MESH2TAK_PASSWORD": password or "atakatak"}
    if host and RE_HOST.match(host):
        ambiente["MESH2TAK_HOST"] = host
    try:
        esito, testo = lancia(script, (file_pacchetto,), ambiente)
    finally:
        shutil.rmtree(cartella, ignore_errors=True)
    if esito != 0:
        return "errore", "certificato non installato: " + ultima_riga(testo)
    fatto = chiedi_al_server(reg, "certificato-fatto", 45)   # sul server non serve piu'
    if fatto.returncode != 0:
        registra("il server non ha tolto il certificato consegnato (lo toglie da solo dopo un giorno)")
    if not ponte_sano(versione_installata()):
        return "errore", "certificato installato, ma il ponte non riparte: leggi il registro"
    if "Il TAK Server non ha accettato il collegamento" in testo:
        return "errore", ("certificato installato, ma il TAK Server non accetta il collegamento: controlla nella "
                          "console infra-TAK che sia quello del ponte")
    m = re.search(r"certificato '([^'\n]*)' \(scade: ([^)\n]*)\)", testo)
    return "fatto", "certificato sostituito" + (f": '{m.group(1)}', scade {m.group(2)}" if m else "")


def azione_reimposta_radio(a: dict, reg: dict) -> Tuple[str, str]:
    script = os.path.join(DIR, "installatore.sh")
    if not os.path.isfile(script):
        raise Errore("sul Raspberry manca l'installatore: aggiorna il ponte dalla pagina, poi riprova")
    esito, testo = lancia(script, ("--reimposta-radio",), durata_s=20 * 60)
    if esito != 0:
        return "errore", "radio non reimpostata: " + ultima_riga(testo)
    return "fatto", "radio del ponte come all'installazione; le altre impostazioni scelte dall'app restano"


FUNZIONI: Dict[str, Callable[[dict, dict], Tuple[str, str]]] = {
    "riavvia-programma": azione_riavvia_programma, "riavvia-radio": azione_riavvia_radio,
    "registro": azione_registro, "impostazioni": azione_impostazioni, "certificato": azione_certificato,
    "reimposta-radio": azione_reimposta_radio}


def principale() -> int:
    if os.geteuid() != 0 and not os.environ.get("MESH2TAK_PROVA_GITHUB"):
        print("Va avviato da root (lo fa il timer mesh2tak-aggiorna).")
        return 1
    os.makedirs(STATO_DIR, exist_ok=True)
    blocco = open(os.path.join(STATO_DIR, ".blocco"), "w")
    try:
        fcntl.flock(blocco, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0                               # un aggiornamento o un comando e' gia' in corso
    stato = leggi_stato()
    installata = versione_installata()
    if stato.get("stato") == "in_corso":      # col blocco libero non e' in corso niente: interrotto (riavvio?)
        if installata and stato.get("versione") == installata:
            stato = scrivi_stato(**dict(stato, stato="fatto",
                                        messaggio=f"aggiornato dalla {stato.get('da') or '?'} alla {installata}"))
        else:
            stato = scrivi_stato(**dict(stato, stato="errore", messaggio=f"l'aggiornamento alla {stato.get('versione')}"
                                        " si e' interrotto (Raspberry spento o riavviato?)"))
    if chiudi_interrotte():
        return 0                               # il Raspberry sta per riavviarsi: il resto dopo
    reg = leggi_registrazione()
    if reg is not None and esegui_azioni(reg):
        return 0                               # il Raspberry si riavvia: il resto al prossimo giro
    r = richiesta_aggiornamento(reg)
    if not r or not installata:
        return 0
    versione, chiesto, reimposta = r
    if numeri(versione) <= numeri(installata):
        return 0                               # gia' fatto, o una versione piu' vecchia: mai indietro
    ora = time.time()
    tentativi = 0
    if stato.get("richiesta") == [versione, chiesto]:
        tentativi = int(stato.get("tentativi") or 0)
        if tentativi >= TENTATIVI:
            return 0
        if stato.get("stato") in ("errore", "ripristinato") and ora - float(stato.get("quando") or 0) < RIPROVA_S:
            return 0
    base = {"versione": versione, "da": installata, "richiesta": [versione, chiesto], "tentativi": tentativi + 1}
    scrivi_stato(stato="in_corso", messaggio=f"aggiornamento alla {versione} in corso", **base)
    registra(f"=== aggiornamento {installata} -> {versione} (tentativo {tentativi + 1})"
             + (", con la radio da reimpostare" if reimposta else ""))
    try:
        esito, messaggio = aggiorna(versione, installata, reimposta)
    except Errore as e:
        esito, messaggio = "errore", str(e)
    except Exception as e:  # noqa
        esito, messaggio = "errore", f"errore inatteso: {e}"
    registra(f"esito: {esito}: {messaggio}")
    scrivi_stato(stato=esito, messaggio=messaggio, **base)
    return 0 if esito == "fatto" else 1


if __name__ == "__main__":
    sys.exit(principale())
