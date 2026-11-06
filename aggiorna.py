#!/usr/bin/env python3
"""mesh2tak-aggiorna: aggiorna il ponte quando la pagina della rete lo chiede (lo avvia un timer, da root).

Ogni due minuti legge la registrazione del ponte sulla pagina della rete (la stessa che il ponte legge per il
tunnel). Se un amministratore ha chiesto una versione piu' nuova di quella installata:
  1. scarica l'installatore di quella versione dalla release ufficiale su GitHub e ne controlla l'impronta
     (SHA256SUMS della stessa release): il server della rete puo' solo chiedere, il programma arriva da GitHub;
  2. lo lancia (tiene configurazione, certificato, canale e approvazione);
  3. controlla che il ponte riparta con la versione nuova, altrimenti rimette quella di prima.
Mai verso una versione piu' vecchia. Lo stato per la pagina della rete e' in /var/lib/mesh2tak-aggiorna/stato.json.
"""
import base64
import binascii
import configparser
import fcntl
import hashlib
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Optional, Tuple

VERSIONE = "@@VERSIONE@@"
REPO = "andrealorenzofiorini/mesh2tak"
SCARICA = os.environ.get("MESH2TAK_PROVA_GITHUB", "https://github.com").rstrip("/")   # le prove: un GitHub finto
CONF = os.environ.get("MESH2TAK_AGGIORNA_CONF", "/etc/mesh2tak/mesh2tak.conf")
DIR = os.environ.get("MESH2TAK_AGGIORNA_DIR", "/opt/mesh2tak")
STATO_DIR = os.environ.get("MESH2TAK_AGGIORNA_STATO", "/var/lib/mesh2tak-aggiorna")
LOG = os.environ.get("MESH2TAK_AGGIORNA_LOG", "/var/log/mesh2tak-aggiornamento.log")
INSTALLATORE = "installa-ponte-meshtastic.sh"
RE_VERSIONE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$")
TENTATIVI = 3                       # per ogni richiesta della pagina
RIPROVA_S = 600                     # tra un tentativo non riuscito e il successivo
ATTESA_PONTE_S = float(os.environ.get("MESH2TAK_AGGIORNA_ATTESA", "180"))
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


def leggi_stato() -> dict:
    try:
        with open(os.path.join(STATO_DIR, "stato.json"), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def scrivi_stato(**dati) -> dict:
    os.makedirs(STATO_DIR, exist_ok=True)
    os.chmod(STATO_DIR, 0o755)                # il ponte (utente mesh2tak) lo legge per la pagina della rete
    dati["quando"] = int(time.time())
    f = os.path.join(STATO_DIR, "stato.json")
    with open(f + ".nuovo", "w", encoding="utf-8") as h:
        json.dump(dati, h, ensure_ascii=False)
    os.chmod(f + ".nuovo", 0o644)
    os.replace(f + ".nuovo", f)
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


def richiesta_della_pagina() -> Optional[Tuple[str, float, str]]:
    """(versione chiesta, quando, rete) dalla registrazione del ponte; None se non c'e' niente da fare."""
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
    except (OSError, UnicodeDecodeError):
        return None
    try:
        reg = json.loads(scarica(f"{rete}/accesso/{impronta}.json", 65536).decode("utf-8"))
    except (Errore, ValueError, UnicodeDecodeError) as e:
        registra_una_volta(f"registrazione non letta: {e}")       # anche 404: ponte non (ancora) nella rete
        return None
    registra_una_volta("registrazione letta")
    r = reg.get("aggiorna") if isinstance(reg, dict) else None
    if not isinstance(r, dict) or not RE_VERSIONE.match(str(r.get("versione", ""))):
        return None
    chiesto = r.get("chiesto")
    return str(r["versione"]), float(chiesto) if isinstance(chiesto, (int, float)) else 0.0, rete


def ponte_sano(versione: str) -> bool:
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
                if isinstance(d, dict) and d.get("versione") == versione:
                    return True
            except (Errore, ValueError):
                pass
        elif attivo and not porta:
            return versione_installata() == versione
        if time.time() >= fine:
            return False
        time.sleep(5)


def lancia(script: str) -> Tuple[int, str]:
    registra(f"lancio {script}")
    try:
        r = subprocess.run(["bash", script], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=DURATA_MAX_S, cwd="/root" if os.path.isdir("/root") else "/",
                           env=dict(os.environ, MESH2TAK_DA_AGGIORNAMENTO="1"))
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


def aggiorna(versione: str, prima: Optional[str]) -> Tuple[str, str]:
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
        esito, testo = lancia(nuovo)
    finally:
        os.remove(nuovo)                      # se e' andato bene, l'installatore l'ha gia' copiato in DIR
    if esito == 0 and ponte_sano(versione):
        return "fatto", f"aggiornato dalla {prima or '?'} alla {versione}"
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


def principale() -> int:
    if os.geteuid() != 0 and not os.environ.get("MESH2TAK_PROVA_GITHUB"):
        print("Va avviato da root (lo fa il timer mesh2tak-aggiorna).")
        return 1
    os.makedirs(STATO_DIR, exist_ok=True)
    blocco = open(os.path.join(STATO_DIR, ".blocco"), "w")
    try:
        fcntl.flock(blocco, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0                               # un aggiornamento e' gia' in corso
    stato = leggi_stato()
    installata = versione_installata()
    if stato.get("stato") == "in_corso":      # col blocco libero non e' in corso niente: interrotto (riavvio?)
        if installata and stato.get("versione") == installata:
            stato = scrivi_stato(**dict(stato, stato="fatto",
                                        messaggio=f"aggiornato dalla {stato.get('da') or '?'} alla {installata}"))
        else:
            stato = scrivi_stato(**dict(stato, stato="errore", messaggio=f"l'aggiornamento alla {stato.get('versione')}"
                                        " si e' interrotto (Raspberry spento o riavviato?)"))
    r = richiesta_della_pagina()
    if not r or not installata:
        return 0
    versione, chiesto, _ = r
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
    registra(f"=== aggiornamento {installata} -> {versione} (tentativo {tentativi + 1})")
    try:
        esito, messaggio = aggiorna(versione, installata)
    except Errore as e:
        esito, messaggio = "errore", str(e)
    except Exception as e:  # noqa
        esito, messaggio = "errore", f"errore inatteso: {e}"
    registra(f"esito: {esito}: {messaggio}")
    scrivi_stato(stato=esito, messaggio=messaggio, **base)
    return 0 if esito == "fatto" else 1


if __name__ == "__main__":
    sys.exit(principale())
