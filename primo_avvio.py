#!/usr/bin/env python3
"""Primo avvio di un ponte Meshtastic nuovo (mesh2tak @@VERSIONE@@).

Sta nell'immagine "Ponte Meshtastic" per Raspberry Pi Imager. Chi prepara la microSD incolla in Imager, tra le
chiavi SSH, la "chiave d'invito" preparata sulla pagina della rete: una riga

    ssh-ed25519 AAAA...  mesh2tak-invito  https://meshtastic.tuodominio.it

E' una chiave pubblica a caso (nessuno ha la privata): fa da segreto dell'invito e porta l'indirizzo della
pagina della rete, che cosi' non sta nell'immagine. Acceso il Raspberry, questo programma (root, una volta):

  1. crea la chiave del ponte e chiede alla pagina della rete di entrare, mostrando l'invito;
  2. quando la rete lo registra, scarica la sua configurazione (certificato, canale, scheda radio) dal server,
     via SSH, con la chiave del ponte e con le chiavi del server ricevute via HTTPS;
  3. scarica l'installatore della stessa versione del server dalla release ufficiale su GitHub, lo controlla
     con SHA256SUMS e lo lancia senza domande;
  4. riferisce al server a che punto e' (anche dopo il riavvio a meta') e, a installazione finita, cancella
     certificato e chiavi ricevuti e toglie se stesso.

Solo libreria standard. Registro: /var/log/mesh2tak-primo-avvio.log
"""
from __future__ import annotations

import base64
import binascii
import glob
import hashlib
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

VERSIONE = "@@VERSIONE@@"
REPO = "andrealorenzofiorini/mesh2tak"
GITHUB = os.environ.get("MESH2TAK_PROVA_GITHUB", "https://github.com").rstrip("/")
RADICE = os.environ.get("MESH2TAK_PROVA_RADICE", "")      # solo per le prove automatiche
INSTALLATORE = "installa-ponte-meshtastic.sh"

STATO_DIR = "/var/lib/mesh2tak-primo-avvio"
REGISTRO = "/var/log/mesh2tak-primo-avvio.log"
CONF_DIR = "/etc/mesh2tak"                                # le stesse dell'installatore
CHIAVE = CONF_DIR + "/accesso/chiave"
ESITO = CONF_DIR + "/esito-installazione"
SCHERMO = CONF_DIR + "/installazione-schermo.log"
UNITA_RIPRESA = "/etc/systemd/system/mesh2tak-installazione.service"
UNITA = "/etc/systemd/system/mesh2tak-primo-avvio.service"
PROGRAMMA = "/usr/local/lib/mesh2tak/primo-avvio.py"
RETE_A_MANO = "/boot/firmware/mesh2tak-rete.txt"          # indirizzo della rete senza invito (approvazione a mano)

RE_RETE = re.compile(r"^https://[A-Za-z0-9.-]+(?::\d+)?$")
RE_NOME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,18}[a-z0-9])?$")
RE_UTENTE = re.compile(r"^ponte-[a-z0-9-]{1,20}$")
RE_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
RE_CHIAVE_HOST = re.compile(r"^(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)) [A-Za-z0-9+/=]{20,}$")
RE_VERSIONE = re.compile(r"^\d+\.\d+\.\d+$")
RE_PASSO = re.compile(r"^(\d)/9\s+(.{1,120})")
SCHEDE = ("usb", "waveshare", "meshadv", "meshadv-mini", "rak")
INTESTAZIONE_INVITO = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
RIPETIZIONI_MAX = 2                                       # installazione interrotta (corrente?): riparte da sola
RIPROVE_AUTOMATICHE = 2                                   # installazione non riuscita: riprova da sola (internet?)
ATTESA_RIPROVA_S = 600
RE_COLORI = re.compile(r"\x1b\[[0-9;]*m")


class Errore(Exception):
    pass


def P(percorso: str) -> str:  # noqa: N802
    return RADICE + percorso if RADICE else percorso


def registra(testo: str) -> None:
    riga = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {testo}"
    print(riga, flush=True)
    try:
        os.makedirs(os.path.dirname(P(REGISTRO)), exist_ok=True)
        with open(os.open(P(REGISTRO), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a", encoding="utf-8") as f:
            f.write(riga + "\n")
    except OSError:
        pass


def esegui(comando: List[str], attesa: float = 60, **altro) -> subprocess.CompletedProcess:
    return subprocess.run(comando, capture_output=True, text=True, timeout=attesa, stdin=subprocess.DEVNULL, **altro)


def leggi(percorso: str, massimo: int = 1 << 20) -> Optional[str]:
    try:
        with open(percorso, encoding="utf-8", errors="replace") as f:
            return f.read(massimo)
    except OSError:
        return None


def scrivi(percorso: str, testo, modo: int = 0o600) -> None:
    os.makedirs(os.path.dirname(percorso), exist_ok=True)
    nuovo = percorso + ".nuovo"
    dati = testo if isinstance(testo, bytes) else testo.encode("utf-8")
    fd = os.open(nuovo, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, modo)
    try:
        os.write(fd, dati)
        os.fchmod(fd, modo)
    finally:
        os.close(fd)
    os.replace(nuovo, percorso)


def togli(percorso: str) -> None:
    try:
        os.unlink(percorso)
    except OSError:
        pass


# ---- stato tra un giro e l'altro (e tra un riavvio e l'altro) ----

def carica_stato() -> dict:
    try:
        d = json.loads(leggi(P(STATO_DIR + "/stato.json")) or "{}")
    except ValueError:
        d = {}
    return d if isinstance(d, dict) else {}


def salva_stato(st: dict) -> None:
    scrivi(P(STATO_DIR + "/stato.json"), json.dumps(st, ensure_ascii=False))


# ---- invito: la riga tra le chiavi SSH messe da Raspberry Pi Imager ----

def file_chiavi_autorizzate() -> List[str]:
    return sorted(glob.glob(P("/home/*/.ssh/authorized_keys")) + glob.glob(P("/root/.ssh/authorized_keys")))


def leggi_invito(riga: str) -> Optional[Tuple[str, str]]:
    """(indirizzo della rete, chiave d'invito in base64) se la riga e' una chiave d'invito."""
    parti = riga.split()
    if len(parti) < 4 or parti[0] != "ssh-ed25519" or parti[2] != "mesh2tak-invito":
        return None
    rete = parti[3].rstrip("/")
    try:
        blob = base64.b64decode(parti[1], validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(blob) != 51 or not blob.startswith(INTESTAZIONE_INVITO) or not RE_RETE.match(rete):
        return None
    return rete, parti[1]


def trova_inviti() -> Tuple[str, List[str]]:
    """Indirizzo della pagina della rete e chiavi d'invito trovate (al massimo tre, della stessa rete)."""
    rete, inviti = "", []
    for f in file_chiavi_autorizzate():
        for riga in (leggi(f, 65536) or "").splitlines():
            trovato = leggi_invito(riga)
            if trovato and (not rete or trovato[0] == rete) and trovato[1] not in inviti:
                rete = trovato[0]
                inviti.append(trovato[1])
    if not rete:
        a_mano = (leggi(P(RETE_A_MANO), 300) or "").strip().rstrip("/")
        if RE_RETE.match(a_mano):
            rete = a_mano
    return rete, inviti[:3]


def togli_inviti() -> None:
    """A installazione finita la chiave d'invito non serve piu': via dalle chiavi autorizzate."""
    for f in file_chiavi_autorizzate():
        righe = (leggi(f, 65536) or "").splitlines(keepends=True)
        tenute = [r for r in righe if not leggi_invito(r)]
        if len(tenute) != len(righe):
            try:
                st = os.stat(f)
                nuovo = f + ".mesh2tak"
                with open(os.open(nuovo, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as h:
                    h.writelines(tenute)
                os.chown(nuovo, st.st_uid, st.st_gid)
                os.chmod(nuovo, st.st_mode & 0o777)
                os.replace(nuovo, f)
            except OSError as e:
                registra(f"non riesco a togliere la chiave d'invito da {f}: {e}")


# ---- chiave del ponte (la stessa che usera' il ponte installato) ----

def assicura_chiave() -> str:
    """La chiave pubblica del ponte, creata se manca dove la cerca poi l'installatore."""
    privata, pubblica = P(CHIAVE), P(CHIAVE) + ".pub"
    if not (os.path.isfile(privata) and os.path.getsize(privata) and os.path.isfile(pubblica)
            and os.path.getsize(pubblica)):
        os.makedirs(os.path.dirname(privata), mode=0o700, exist_ok=True)
        os.chmod(P(CONF_DIR), 0o755)
        togli(privata)
        togli(pubblica)
        r = esegui(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"mesh2tak-accesso@{socket.gethostname()}",
                    "-f", privata])
        if r.returncode != 0:
            raise Errore(f"non riesco a creare la chiave del ponte: {(r.stderr or r.stdout).strip()[:200]}")
        os.chmod(privata, 0o600)
        registra("creata la chiave di questo ponte")
    riga = (leggi(pubblica, 2000) or "").strip()
    if len(riga.split()) < 2:
        raise Errore("chiave del ponte non leggibile")
    return riga


def impronta_chiave(riga: str) -> str:
    return hashlib.sha256(base64.b64decode(riga.split()[1], validate=True)).hexdigest()


# ---- pagina della rete (HTTPS) ----

def apri(url: str, dati: Optional[bytes] = None, attesa: float = 30) -> Tuple[int, bytes]:
    apritore = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                           urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    intestazioni = {"User-Agent": f"mesh2tak-primo-avvio/{VERSIONE}", "Cache-Control": "no-cache",
                    "Accept": "application/json"}
    if dati is not None:
        intestazioni["Content-Type"] = "application/json"
    try:
        with apritore.open(urllib.request.Request(url, data=dati, headers=intestazioni), timeout=attesa) as r:
            return r.status, r.read(4 << 20)
    except urllib.error.HTTPError as e:
        return e.code, e.read(65536)


def leggi_registrazione(rete: str, impronta: str) -> Optional[dict]:
    """La registrazione del ponte pubblicata dalla rete; None finche' non c'e'."""
    codice, corpo = apri(f"{rete}/accesso/{impronta}.json")
    if codice == 404:
        return None
    if codice != 200:
        raise Errore(f"la pagina della rete risponde {codice}")
    try:
        d = json.loads(corpo.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise Errore("registrazione del server non valida")
    utente, host, porta_ssh, chiavi = str(d.get("utente", "")), str(d.get("ssh_host", "")), d.get("ssh_porta", 22), \
        d.get("chiavi_host")
    if (not RE_UTENTE.match(utente) or not RE_HOST.match(host) or not isinstance(porta_ssh, int)
            or not 1 <= porta_ssh <= 65535 or not isinstance(chiavi, list) or not chiavi
            or not all(isinstance(c, str) and RE_CHIAVE_HOST.match(c.strip()) for c in chiavi)):
        raise Errore("registrazione del server non valida")
    installa = d.get("installa") if isinstance(d.get("installa"), dict) else None
    tentativo = installa.get("tentativo") if installa else None
    return {"utente": utente, "host": host, "porta_ssh": porta_ssh, "chiavi": [c.strip() for c in chiavi],
            "tentativo": tentativo if isinstance(tentativo, int) and tentativo > 0 else 0}


def modello() -> str:
    testo = (leggi(P("/proc/device-tree/model"), 200) or "").replace("\x00", "").strip()
    return testo[:80]


def nome_dal_computer() -> str:
    """Un nome di ponte dal nome dato al Raspberry in Imager (ponte-velate -> velate): vale solo senza invito."""
    nome = re.sub(r"[^a-z0-9-]", "-", socket.gethostname().split(".")[0].lower())
    nome = re.sub(r"^(ponte|meshtastic)-", "", nome).strip("-")[:20].strip("-")
    return nome if RE_NOME.match(nome) else "nuovo"


def manda_richiesta(rete: str, chiave: str, inviti: List[str]) -> Tuple[int, dict]:
    corpo = json.dumps({"formato": 1, "nome": nome_dal_computer(), "chiave": chiave,
                        "macchina": socket.gethostname()[:63], "versione": VERSIONE,
                        "nuovo": {"modello": modello(), "inviti": inviti,
                                  "radio_usb": bool(glob.glob(P("/dev/serial/by-id/*")))}}).encode("utf-8")
    codice, testo = apri(f"{rete}/accesso/richiesta", corpo)
    try:
        d = json.loads(testo.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        d = {}
    return codice, d if isinstance(d, dict) else {}


# ---- server della rete via SSH: configurazione e avanzamento ----

def comando_ssh(reg: dict) -> List[str]:
    noti = P(STATO_DIR + "/server-rete.known_hosts")
    scrivi(noti, "".join(f"mesh2tak-rete {c}\n" for c in reg["chiavi"]), 0o600)
    return [os.environ.get("MESH2TAK_PROVA_SSH", "ssh"), "-F", "/dev/null", "-T", "-i", P(CHIAVE),
            "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none", "-o", "BatchMode=yes",
            "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
            "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={noti}",
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", "HostKeyAlias=mesh2tak-rete",
            "-o", "UpdateHostKeys=no", "-o", "CheckHostIP=no", "-o", "ConnectTimeout=20", "-o", "LogLevel=ERROR",
            "-p", str(reg["porta_ssh"]), f"{reg['utente']}@{reg['host']}"]


def riferisci(reg: Optional[dict], fase: str, testo: str) -> bool:
    """A che punto siamo, per la pagina della rete. Se non arriva pazienza: l'installazione non si ferma."""
    registra(f"[{fase}] {testo}")
    if not reg:
        return False
    pulito = re.sub(r"[^\w .,:;()'/+@%=-]", " ", testo)[:280]
    try:
        r = esegui(comando_ssh(reg) + [f"avanzamento {fase} {pulito}"], attesa=45)
        if r.returncode != 0:
            registra(f"(il server non ha preso l'avanzamento: {(r.stderr or '').strip()[:160]})")
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError) as e:
        registra(f"(avanzamento non mandato: {e})")
        return False


def scarica_configurazione(reg: dict) -> dict:
    try:
        r = subprocess.run(comando_ssh(reg) + ["configurazione"], capture_output=True, timeout=90,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise Errore("il server della rete non risponde (SSH)")
    if r.returncode == 3:
        raise Errore("sul server non c'e' una configurazione per questo ponte: sulla pagina della rete usa "
                     "'Riprova l'installazione'")
    if r.returncode != 0:
        raise Errore(f"il server della rete ha rifiutato la richiesta della configurazione "
                     f"({r.stderr.decode('utf-8', 'replace').strip()[:200]})")
    try:
        c = json.loads(r.stdout.decode("utf-8"))
        tak, canale = c["tak"], c["canale"]
        pacchetto = base64.b64decode(tak["pacchetto"], validate=True)
        nome_pacchetto = os.path.basename(str(tak["nome_pacchetto"]))
        conf = {"nome": str(c["nome"]), "scheda": str(c["scheda"]), "posizione": str(c.get("posizione") or ""),
                "rete": str(c["rete"]).rstrip("/"), "versione": str(c["versione"]), "tentativo": int(c["tentativo"]),
                "pacchetto": tak["pacchetto"], "nome_pacchetto": nome_pacchetto, "password": str(tak["password"]),
                "host": str(tak.get("host") or ""), "canale": str(canale["nome"]), "chiave": str(canale["chiave"]),
                "secondario": str(canale.get("secondario") or "")}
    except (ValueError, KeyError, TypeError, UnicodeDecodeError, binascii.Error):
        raise Errore("configurazione ricevuta dal server non valida")
    if (not RE_NOME.match(conf["nome"]) or conf["scheda"] not in SCHEDE or not RE_RETE.match(conf["rete"])
            or not RE_VERSIONE.match(conf["versione"]) or not pacchetto or len(pacchetto) > 1 << 20
            or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,78}\.(zip|p12|pfx)$", nome_pacchetto)
            or not re.match(r"^[-0-9.,]*$", conf["posizione"]) or (conf["host"] and not RE_HOST.match(conf["host"]))
            or not re.match(r"^[A-Za-z0-9_-]{1,11}$", conf["canale"]) or not re.match(r"^[A-Za-z0-9+/=]{20,60}$", conf["chiave"])
            or not re.match(r"^[A-Za-z0-9_-]{0,11}$", conf["secondario"]) or "\n" in conf["password"]):
        raise Errore("configurazione ricevuta dal server non valida")
    return conf


# ---- installatore: dalla release ufficiale, controllato ----

def numeri(v: str) -> Tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def scarica_installatore(versione: str) -> str:
    base = f"{GITHUB}/{REPO}/releases/download/v{versione}"
    codice, somme = apri(f"{base}/SHA256SUMS", attesa=60)
    if codice != 200:
        raise Errore(f"GitHub non da' la versione {versione} (risposta {codice}): internet?")
    attesa = None
    for riga in somme.decode("utf-8", "replace").splitlines():
        parti = riga.split()
        if len(parti) == 2 and parti[1].lstrip("*") == INSTALLATORE and re.match(r"^[0-9a-f]{64}$", parti[0]):
            attesa = parti[0]
    if not attesa:
        raise Errore(f"nella release {versione} manca la somma di controllo dell'installatore")
    codice, dati = apri(f"{base}/{INSTALLATORE}", attesa=180)
    if codice != 200:
        raise Errore(f"GitHub non da' l'installatore {versione} (risposta {codice})")
    if hashlib.sha256(dati).hexdigest() != attesa:
        raise Errore("l'installatore scaricato non e' quello pubblicato (somma di controllo diversa): non lo lancio")
    if f'VERSIONE="{versione}"'.encode() not in dati[:20000]:
        raise Errore(f"il file scaricato non e' l'installatore {versione}")
    percorso = P(STATO_DIR + "/" + INSTALLATORE)
    scrivi(percorso, dati, 0o700)
    return percorso


def leggi_esito() -> Tuple[str, str]:
    """("ok" | "errore" | "riavvio" | "in_corso" | "", resto della riga) scritto dall'installatore."""
    riga = (leggi(P(ESITO), 2000) or "").strip().splitlines()[:1]
    parti = (riga[0] if riga else "").split(None, 1)
    return (parti[0] if parti else ""), (parti[1] if len(parti) > 1 else "")


def lancia_installatore(st: dict, reg: dict, conf: dict) -> str:
    """Lancia l'installatore senza domande e ne segue i passi. Torna "ok", "riavvio" o solleva Errore."""
    riferisci(reg, "scarico", f"scarico l'installatore {conf['versione']} da GitHub e lo controllo")
    script = scarica_installatore(conf["versione"])
    pacchetto = P(STATO_DIR + "/" + conf["nome_pacchetto"])
    scrivi(pacchetto, base64.b64decode(conf["pacchetto"]), 0o600)
    ambiente = {"PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
                "HOME": "/root", "LANG": "C.UTF-8", "MESH2TAK_PASSWORD": conf["password"],
                "MESH2TAK_CANALE_SQUADRA": conf["canale"], "MESH2TAK_CHIAVE_CANALE": conf["chiave"],
                "MESH2TAK_SECONDARIO": conf["secondario"], "MESH2TAK_ACCESSO_RETE": conf["rete"]}
    if conf["posizione"]:
        ambiente["MESH2TAK_POSIZIONE"] = conf["posizione"]
    if conf["host"]:
        ambiente["MESH2TAK_HOST"] = conf["host"]
    for nome, valore in os.environ.items():
        if nome.startswith("MESH2TAK_PROVA_") or nome in ("MESH2TAK_COLLEGAMENTO", "MESH2TAK_APT_CONF_DIR"):
            ambiente[nome] = valore                       # solo per le prove automatiche
    comando = ["bash", script, "--scheda", conf["scheda"], "--canale-squadra", f"--accesso-esterno={conf['nome']}",
               pacchetto]
    togli(P(ESITO))
    st.update(fase="installo", tentativo=conf["tentativo"], passo="")
    salva_stato(st)
    riferisci(reg, "installo", f"parte l'installazione (mesh2tak {conf['versione']}, radio {conf['scheda']})")
    p = subprocess.Popen(comando, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         env=ambiente, cwd=P(STATO_DIR), text=True, errors="replace")
    ultimo_errore = ""
    with open(os.open(P(STATO_DIR + "/installazione.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a",
              encoding="utf-8") as registro:
        for riga in p.stdout:
            registro.write(riga)
            registro.flush()
            riga = RE_COLORI.sub("", riga).strip()
            m = RE_PASSO.match(riga)
            if m:
                st["passo"] = f"{m.group(1)}/9 {m.group(2).strip()}"
                salva_stato(st)
                riferisci(reg, "installo", st["passo"])
            elif riga.startswith("✘"):
                ultimo_errore = riga.lstrip("✘ ").strip()
            elif "Il Raspberry ora si riavvia" in riga:
                st["fase"] = "riavvio"
                salva_stato(st)
                riferisci(reg, "riavvio", "il Raspberry si riavvia per gli aggiornamenti; poi l'installazione "
                                          "continua da sola (altri 10-20 minuti)")
    codice = p.wait()
    esito, resto = leggi_esito()
    if esito == "riavvio" or st.get("fase") == "riavvio":
        return "riavvio"
    if codice == 0 and esito == "ok":
        return "ok"
    raise Errore(resto if esito == "errore" and resto else ultimo_errore
                 or f"l'installatore si e' fermato (codice {codice}): vedi /var/log/mesh2tak-installazione.log")


def ripresa_in_corso() -> bool:
    """Dopo il riavvio l'installatore riprende da solo (mesh2tak-installazione.service): sta ancora lavorando?"""
    if not os.path.exists(P(UNITA_RIPRESA)):
        return False
    try:
        attiva = esegui(["systemctl", "is-active", "mesh2tak-installazione.service"]).stdout.strip()
        abilitata = esegui(["systemctl", "is-enabled", "mesh2tak-installazione.service"]).stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return True
    return attiva in ("active", "activating", "reloading") or abilitata == "enabled"


def passo_dallo_schermo() -> str:
    """L'ultimo passo scritto dall'installatore che lavora da solo dopo il riavvio."""
    testo = leggi(P(SCHERMO), 2 << 20) or ""
    testo = testo[testo.rfind("\n=== "):] if "\n=== " in testo else testo
    passi = [m for m in (RE_PASSO.match(RE_COLORI.sub("", r).strip()) for r in testo.splitlines()) if m]
    return f"{passi[-1].group(1)}/9 {passi[-1].group(2).strip()}" if passi else ""


# ---- fine: pulizia ----

def finisci(st: dict, reg: Optional[dict], versione: str) -> None:
    for i in range(3):
        if riferisci(reg, "fatto", f"installato: mesh2tak {versione}. Il ponte si collega da solo") or not reg:
            break
        time.sleep(0 if RADICE else 15)   # (il server, saputolo, cancella certificato e canale di questo ponte)
    for f in glob.glob(P(STATO_DIR) + "/*"):
        if os.path.basename(f) not in ("stato.json", "installazione.log"):
            togli(f)                                     # certificato, configurazione, installatore, chiavi note
    togli_inviti()
    utenti = [os.path.basename(d) for d in glob.glob(P("/home/*")) if os.path.isdir(d)]
    if shutil.which("rpi-connect") and shutil.which("loginctl") and not RADICE:
        for u in utenti[:3]:                             # Raspberry Pi Connect raggiungibile anche dopo i riavvii
            try:
                esegui(["loginctl", "enable-linger", u])
            except (subprocess.TimeoutExpired, OSError):
                pass
    st.clear()
    st.update(fase="fatto", quando=int(time.time()), versione=versione)
    salva_stato(st)
    # da qui il ponte fa da se' (mesh2tak.service): questo programma non serve piu'
    togli(P(UNITA))
    togli(P("/etc/systemd/system/multi-user.target.wants/mesh2tak-primo-avvio.service"))
    togli(P(PROGRAMMA))
    if not RADICE:
        try:
            esegui(["systemctl", "daemon-reload"])
        except (subprocess.TimeoutExpired, OSError):
            pass
    registra("primo avvio finito: tolto dal Raspberry")


# ---- un giro ----

def non_riuscita(st: dict, reg: Optional[dict], motivo: str) -> float:
    """Installazione non riuscita: le prime volte riprova da sola dopo 10 minuti (internet o un server di
    aggiornamenti che non rispondono), poi aspetta "Riprova l'installazione" dalla pagina della rete."""
    riprove = int(st.get("riprove") or 0) + 1
    st.update(fase="errore", errore=motivo, riprove=riprove, riprova_alle=time.time() + ATTESA_RIPROVA_S)
    salva_stato(st)
    da_sola = riprove <= RIPROVE_AUTOMATICHE
    riferisci(reg, "errore", motivo + (" (riprovo da solo tra 10 minuti)" if da_sola else ""))
    return 60


def giro(st: dict) -> Optional[float]:
    """Un passo avanti. Torna tra quanti secondi rifarlo; None quando non c'e' piu' niente da fare."""
    if st.get("fase") == "fatto":
        return None
    rete, inviti = trova_inviti()
    rete = rete or st.get("rete") or ""
    if not rete:
        if not st.get("avvisato_invito"):
            registra("tra le chiavi SSH non c'e' la chiave d'invito della pagina della rete: la microSD va "
                     "riscritta con Raspberry Pi Imager incollando la chiave d'invito (Accesso remoto > SSH)")
            st["avvisato_invito"] = True
            salva_stato(st)
        return 300
    st["rete"] = rete
    chiave = assicura_chiave()
    impronta = impronta_chiave(chiave)
    try:
        reg = leggi_registrazione(rete, impronta)
    except (Errore, OSError, ssl.SSLError, subprocess.TimeoutExpired):
        if st.get("fase") not in ("installo", "riavvio") or not st.get("registrazione"):
            raise
        reg = st["registrazione"]            # dopo il riavvio la rete puo' arrivare dopo: intanto si segue lo schermo
    if reg is not None and reg != st.get("registrazione"):
        st["registrazione"] = reg

    if st.get("fase") in ("installo", "riavvio"):
        esito, resto = leggi_esito()
        if esito == "ok":
            finisci(st, reg, resto or st.get("versione") or VERSIONE)
            return None
        if esito == "errore":
            return non_riuscita(st, reg, resto or "installazione non riuscita")
        if ripresa_in_corso():
            passo = passo_dallo_schermo()
            if passo and passo != st.get("passo"):
                st["passo"] = passo
                salva_stato(st)
                riferisci(reg, "installo", passo)
            return 20
        # ne' finita ne' in corso: interrotta (corrente mancata, riavvio a meta')
        st["ripetizioni"] = int(st.get("ripetizioni") or 0) + 1
        if st["ripetizioni"] > RIPETIZIONI_MAX:
            st.update(fase="errore", errore="installazione interrotta piu' volte")
            salva_stato(st)
            riferisci(reg, "errore", "installazione interrotta piu' volte (alimentazione?): sulla pagina della rete "
                                     "usa 'Riprova l'installazione'")
            return 60
        st["fase"] = "interrotta"
        salva_stato(st)
        riferisci(reg, "installo", "installazione interrotta (Raspberry spento o riavviato?): riparto")

    if reg is None:
        ora = time.time()
        if ora >= float(st.get("prossima_richiesta") or 0):
            codice, d = manda_richiesta(rete, chiave, inviti)
            if codice in (200, 202):
                attesa = 600
                if not st.get("chiesto"):
                    registra(f"chiesto alla pagina della rete {rete} di entrare" +
                             (": invito riconosciuto, mi sta aggiungendo" if d.get("in_corso") else
                              f" (codice di controllo {d.get('controllo', '?')}): aspetto l'approvazione"))
                    st["chiesto"] = True
            elif codice == 403:
                attesa = 3600
                registra(f"la pagina della rete ha rifiutato questo Raspberry: {d.get('errore', '')}")
            else:
                attesa = 300 if codice != 429 else 3600
                registra(f"la pagina della rete risponde {codice}: {d.get('errore', '')}")
            st["prossima_richiesta"] = ora + attesa
            salva_stato(st)
        return 30

    if not reg["tentativo"]:
        if not st.get("avvisato_configurazione"):
            st["avvisato_configurazione"] = True
            salva_stato(st)
            riferisci(reg, "attesa", "il ponte e' registrato ma non c'e' una configurazione da installare: sulla "
                                     "pagina della rete usa 'Riprova l'installazione'")
        return 120
    gia_fatto = int(st.get("tentativo") or 0)
    if st.get("fase") == "errore" and reg["tentativo"] <= gia_fatto:
        if int(st.get("riprove") or 0) > RIPROVE_AUTOMATICHE or time.time() < float(st.get("riprova_alle") or 0):
            return 60                                     # aspetta "Riprova" dalla pagina della rete
        st["fase"] = "interrotta"                         # riprova da solo, con la configurazione che ha
        riferisci(reg, "installo", "riprovo l'installazione")
    if st.get("fase") == "interrotta" and reg["tentativo"] <= gia_fatto:
        testo = leggi(P(STATO_DIR + "/configurazione.json"))
        conf = json.loads(testo) if testo else scarica_configurazione(reg)
    else:
        conf = scarica_configurazione(reg)
        st.update(ripetizioni=0, riprove=0)
    if numeri(conf["versione"]) < (1, 10, 0):
        raise Errore(f"il server chiede la versione {conf['versione']}, che non si installa da sola")
    scrivi(P(STATO_DIR + "/configurazione.json"), json.dumps(conf))
    st["versione"] = conf["versione"]
    try:
        esito = lancia_installatore(st, reg, conf)
    except Errore as e:
        st["tentativo"] = conf["tentativo"]
        return non_riuscita(st, reg, str(e))
    if esito == "riavvio":
        return 600                                        # il Raspberry si sta spegnendo
    finisci(st, reg, conf["versione"])
    return None


def aspetta_avvio() -> None:
    """Prima il Raspberry finisce di avviarsi (Imager applica nome, Wi-Fi e chiavi al primo avvio) e mette a
    posto l'orologio: senza l'ora giusta i certificati HTTPS non valgono."""
    if RADICE:
        return
    try:
        subprocess.run(["systemctl", "is-system-running", "--wait"], capture_output=True, timeout=900)
    except (subprocess.TimeoutExpired, OSError):
        pass
    fine = time.time() + 300
    while time.time() < fine:
        try:
            if esegui(["timedatectl", "show", "-p", "NTPSynchronized", "--value"]).stdout.strip() == "yes":
                return
        except (subprocess.TimeoutExpired, OSError):
            return
        time.sleep(10)


def main() -> int:
    if os.geteuid() != 0 and not RADICE:
        print("Serve root.", file=sys.stderr)
        return 1
    os.makedirs(P(STATO_DIR), mode=0o700, exist_ok=True)
    st = carica_stato()
    if st.get("fase") == "fatto":
        return 0
    registra(f"primo avvio del ponte nuovo (mesh2tak {VERSIONE}) su {socket.gethostname()}")
    if not st.get("chiesto"):
        aspetta_avvio()         # (dopo il riavvio a meta' installazione no: c'e' da riferire a che punto e')
    una_volta = os.environ.get("MESH2TAK_PROVA_UN_GIRO") == "1"
    errori: Dict[str, float] = {}
    while True:
        try:
            attesa = giro(st)
        except (Errore, OSError, ValueError, subprocess.TimeoutExpired, ssl.SSLError) as e:
            testo = str(e)[:300]
            if time.time() - errori.get(testo, 0) > 1800:      # lo stesso errore, una riga ogni mezz'ora
                errori[testo] = time.time()
                registra(f"non riesco: {testo}. Riprovo")
            attesa = 60
        if attesa is None:
            return 0
        if una_volta:
            return 75                                          # (prove) "c'e' ancora da fare"
        time.sleep(attesa)


if __name__ == "__main__":
    sys.exit(main())
