#!/usr/bin/env python3
"""mesh2tak-scorta: prepara una microSD di scorta, copia identica di quella del Raspberry.

Uso:
  sudo mesh2tak-scorta                 trova la microSD nel lettore USB, chiede conferma e la prepara
  sudo mesh2tak-scorta --si            senza chiedere conferma
  sudo mesh2tak-scorta --disco /dev/sdb    se i lettori USB collegati sono piu' di uno

La copia si fa mentre il Raspberry lavora; alla fine il ponte si ferma per circa un minuto per ricopiare
i file cambiati nel frattempo. La microSD di scorta ha la stessa identita' del Raspberry (certificato,
chiavi, canale): si usa solo AL POSTO di quella del Raspberry, mai in un secondo Raspberry acceso insieme.
"""
import argparse
import fcntl
import json
import os
import random
import re
import stat
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

VERSIONE = "@@VERSIONE@@"
RADICE = os.environ.get("MESH2TAK_SCORTA_RADICE", "/")          # le prove usano un sistema finto
AVVIO = os.environ.get("MESH2TAK_SCORTA_AVVIO", "")
MONTAGGIO = os.environ.get("MESH2TAK_SCORTA_MONTAGGIO", "/run/mesh2tak-scorta")
LOG = os.environ.get("MESH2TAK_SCORTA_LOG", "/var/log/mesh2tak-scorta.log")
SERVIZI = ("mesh2tak", "meshtasticd")       # fermati in quest'ordine, ripartono al contrario
ESCLUSI = ("/proc/*", "/sys/*", "/dev/*", "/run/*", "/tmp/*", "/mnt/*", "/media/*", "/lost+found",
           "/var/swap", "/swapfile", "/var/tmp/*", "/var/cache/apt/archives/*.deb")
SETTORE = 512
ALLINEA = 2048                              # partizioni allineate a 1 MiB

if sys.stdout.isatty():
    V, G, R, B, N = "\033[32m", "\033[33m", "\033[31m", "\033[1m", "\033[0m"
else:
    V = G = R = B = N = ""


class Errore(Exception):
    pass


def passo(t: str) -> None:
    print(f"\n{B}{t}{N}", flush=True)
    registra(t)


def ok(t: str) -> None:
    print(f"  {V}✔{N} {t}", flush=True)
    registra("ok: " + t)


def info(t: str) -> None:
    print(f"    {t}", flush=True)


def registra(t: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + t + "\n")
    except OSError:
        pass


def esegui(cmd: List[str], controlla: bool = True, attesa: Optional[float] = 300, a_video: bool = False,
           spiega: str = "") -> subprocess.CompletedProcess:
    registra("$ " + " ".join(cmd))
    try:
        if a_video:
            r = subprocess.run(cmd, timeout=attesa, stderr=subprocess.PIPE, text=True)
            r.stdout = ""
        else:
            r = subprocess.run(cmd, timeout=attesa, capture_output=True, text=True)
    except FileNotFoundError:
        raise Errore(f"manca il programma {cmd[0]}")
    except subprocess.TimeoutExpired:
        raise Errore(f"{cmd[0]} non ha finito in tempo")
    uscita = ((r.stdout or "") + (r.stderr or "")).strip()
    if uscita:
        registra(uscita[-3000:])
    if controlla and r.returncode != 0:
        raise Errore((spiega or f"{cmd[0]} non riuscito") + (f": {uscita.splitlines()[-1]}" if uscita else ""))
    return r


def leggi(percorso: str, massimo: int = 4096) -> str:
    try:
        with open(percorso, encoding="utf-8", errors="replace") as f:
            return f.read(massimo).strip()
    except OSError:
        return ""


def intero(percorso: str) -> int:
    try:
        return int(leggi(percorso, 64) or "0")
    except ValueError:
        return 0


# ---------------------------------------------------------------- dischi e partizioni

@dataclass
class Voce:
    stato: int
    tipo: int
    inizio: int
    settori: int


def leggi_mbr(dati: bytes) -> Tuple[int, List[Optional[Voce]]]:
    if len(dati) < SETTORE or dati[510:512] != b"\x55\xaa":
        raise Errore("la microSD del Raspberry non ha una tabella delle partizioni MBR: non so copiarla")
    ident = struct.unpack_from("<I", dati, 440)[0]
    voci: List[Optional[Voce]] = []
    for i in range(4):
        o = 446 + 16 * i
        inizio, settori = struct.unpack_from("<II", dati, o + 8)
        voci.append(Voce(dati[o], dati[o + 4], inizio, settori) if dati[o + 4] else None)
    if any(v and v.tipo == 0xEE for v in voci):
        raise Errore("la microSD del Raspberry usa una tabella GPT: non prevista")
    return ident, voci


def chs(lba: int) -> bytes:
    """Indirizzo cilindro/testina/settore (255 testine, 63 settori): oltre gli 8 GB il valore massimo."""
    c = lba // (255 * 63)
    if c > 1023:
        return bytes([0xFE, 0xFF, 0xFF])
    h = (lba // 63) % 255
    s = lba % 63 + 1
    return bytes([h, ((c >> 2) & 0xC0) | s, c & 0xFF])


def mbr_nuovo(originale: bytes, ident: int, voci: List[Voce]) -> bytes:
    b = bytearray(SETTORE)
    b[0:440] = originale[0:440]               # codice di avvio come l'originale (il Raspberry non lo usa)
    struct.pack_into("<I", b, 440, ident)
    for i, v in enumerate(voci):
        o = 446 + 16 * i
        b[o] = v.stato
        b[o + 1:o + 4] = chs(v.inizio)
        b[o + 4] = v.tipo
        b[o + 5:o + 8] = chs(v.inizio + v.settori - 1)
        struct.pack_into("<II", b, o + 8, v.inizio, v.settori)
    b[510:512] = b"\x55\xaa"
    return bytes(b)


def partizione_di(percorso: str) -> Tuple[str, str, int]:
    """(nome della partizione, nome del disco, numero) del file system montato su 'percorso'."""
    d = os.stat(percorso).st_dev
    sysp = os.path.realpath(f"/sys/dev/block/{os.major(d)}:{os.minor(d)}")
    if not os.path.exists(sysp + "/partition"):
        raise Errore(f"{percorso} non e' su una partizione di un disco (non so copiarlo)")
    return os.path.basename(sysp), os.path.basename(os.path.dirname(sysp)), intero(sysp + "/partition")


def partizioni(disco: str) -> Dict[int, str]:
    """{numero: nome} delle partizioni che il kernel vede sul disco."""
    out = {}
    base = f"/sys/block/{disco}"
    try:
        nomi = os.listdir(base)
    except OSError:
        return out
    for n in nomi:
        if n.startswith(disco) and os.path.exists(f"{base}/{n}/partition"):
            out[intero(f"{base}/{n}/partition")] = n
    return out


def descrivi(disco: str) -> str:
    marca = " ".join(x for x in (leggi(f"/sys/block/{disco}/device/vendor"),
                                 leggi(f"/sys/block/{disco}/device/model")) if x)
    gb = intero(f"/sys/block/{disco}/size") * SETTORE / 1e9
    return f"{marca or 'microSD'} da {gb:.1f} GB".replace(".", ",")


def dischi_usb(sorgente: str) -> List[str]:
    out = []
    try:
        nomi = sorted(os.listdir("/sys/block"))
    except OSError:
        return out
    for d in nomi:
        if d == sorgente or d.startswith(("loop", "ram", "zram", "mmcblk", "dm-", "md", "sr")):
            continue
        if "/usb" not in os.path.realpath(f"/sys/block/{d}"):
            continue
        if intero(f"/sys/block/{d}/size") > 0:     # un lettore senza microSD ha dimensione zero
            out.append(d)
    return out


def in_uso(disco: str) -> Optional[str]:
    """Perche' il disco non si puo' cancellare (montato, swap, usato da altro), oppure None."""
    numeri = {leggi(f"/sys/block/{disco}/dev")}
    for p in partizioni(disco).values():
        numeri.add(leggi(f"/sys/block/{disco}/{p}/dev"))
    numeri.discard("")
    for riga in leggi("/proc/self/mountinfo", 1 << 20).splitlines():
        campi = riga.split()
        if len(campi) > 4 and campi[2] in numeri:
            return f"e' montato su {campi[4]}"
    for riga in leggi("/proc/swaps", 65536).splitlines()[1:]:
        nome = os.path.basename(riga.split()[0]) if riga.split() else ""
        if nome == disco or nome in partizioni(disco).values():
            return "e' usato come swap"
    for p in [disco] + list(partizioni(disco).values()):
        base = f"/sys/block/{disco}" + ("" if p == disco else f"/{p}")
        try:
            if os.listdir(base + "/holders"):
                return "e' usato da un altro sistema (RAID o cifratura)"
        except OSError:
            pass
    return None


def nodo(nome: str, attesa: float = 10.0) -> str:
    """Il file in /dev della partizione: di solito lo crea udev; se non arriva, lo crea qui."""
    dev = f"/dev/{nome}"
    numeri = leggi(f"/sys/class/block/{nome}/dev")
    if not re.match(r"^\d+:\d+$", numeri):
        raise Errore(f"il sistema non vede la partizione {nome}")
    giusto = os.makedev(*(int(x) for x in numeri.split(":")))

    def buono() -> bool:
        try:
            s = os.stat(dev)
        except OSError:
            return False
        return stat.S_ISBLK(s.st_mode) and s.st_rdev == giusto

    fine = time.time() + (attesa if os.path.exists("/run/udev/control") else 0.5)
    while time.time() < fine:
        if buono():
            return dev
        time.sleep(0.25)
    if os.path.lexists(dev):
        os.remove(dev)                    # rimasto da un disco di prima con un altro numero
    os.mknod(dev, 0o660 | stat.S_IFBLK, giusto)
    return dev


def rileggi_partizioni(disco: str, attese: Dict[int, Tuple[int, int]]) -> Dict[int, str]:
    """Fa vedere al kernel la nuova tabella; {numero: /dev/...} quando inizio e dimensione tornano."""
    def giuste() -> bool:
        p = partizioni(disco)
        return all(n in p and intero(f"/sys/block/{disco}/{p[n]}/start") == i
                   and intero(f"/sys/block/{disco}/{p[n]}/size") == s for n, (i, s) in attese.items())

    esegui(["blockdev", "--rereadpt", f"/dev/{disco}"], controlla=False, attesa=60)
    for _ in range(20):
        if giuste():
            break
        time.sleep(0.25)
    if not giuste():
        esegui(["partx", "-d", f"/dev/{disco}"], controlla=False, attesa=60)
        esegui(["partx", "-a", f"/dev/{disco}"], controlla=False, attesa=60)
        for _ in range(40):
            if giuste():
                break
            time.sleep(0.25)
    if not giuste():
        raise Errore("il sistema non vede le partizioni nuove della microSD di scorta: staccala, riattaccala "
                     "e rilancia")
    p = partizioni(disco)
    return {n: nodo(p[n]) for n in attese}


def blkid(dev: str, campo: str) -> str:
    r = esegui(["blkid", "-p", "-o", "value", "-s", campo, dev], controlla=False, attesa=30)
    return (r.stdout or "").strip() if r.returncode == 0 else ""


# ---------------------------------------------------------------- copia

def opzioni_rsync() -> List[str]:
    r = esegui(["rsync", "--version"], attesa=30)
    acl = "no ACLs" not in (r.stdout or "")
    return ["-aHXx" + ("A" if acl else ""), "--numeric-ids", "--delete"]


def copia(sorgente_radice: str, sorgente_avvio: str, dest_radice: str, dest_avvio: str, primo: bool) -> None:
    opz = opzioni_rsync()
    esclusi = [f"--exclude={e}" for e in ESCLUSI]
    avanzamento = ["--info=progress2"] if primo and sys.stdout.isatty() else []
    # 24 = file spariti durante la copia (normale con il sistema acceso: la seconda copia li sistema)
    r = esegui(["rsync"] + opz + avanzamento + esclusi + [sorgente_radice.rstrip("/") + "/", dest_radice + "/"],
               controlla=False, attesa=None, a_video=bool(avanzamento))
    if r.returncode not in (0, 24):
        raise Errore(f"copia del sistema non riuscita (rsync {r.returncode}): dettagli in {LOG}")
    # la partizione di avvio e' FAT: niente proprietari, permessi o collegamenti, ore a 2 secondi
    r = esegui(["rsync", "-rt", "--delete", "--modify-window=1", sorgente_avvio.rstrip("/") + "/", dest_avvio + "/"],
               controlla=False, attesa=None)
    if r.returncode not in (0, 24):
        raise Errore(f"copia della partizione di avvio non riuscita (rsync {r.returncode}): dettagli in {LOG}")


def attivo(servizio: str) -> bool:
    return esegui(["systemctl", "is-active", "--quiet", servizio], controlla=False, attesa=30).returncode == 0


def sistema_avvio(dest_avvio: str, dest_radice: str, vecchio: int, nuovo: int,
                  uuid: Dict[str, str]) -> List[str]:
    """La copia ha un'identita' di disco nuova: cmdline.txt e fstab devono puntare alle sue partizioni."""
    cambiati = []
    for f in (os.path.join(dest_avvio, "cmdline.txt"), os.path.join(dest_radice, "etc/fstab")):
        try:
            with open(f, encoding="utf-8") as h:
                testo = h.read()
        except OSError:
            continue
        nuovo_testo = re.sub(rf"PARTUUID={vecchio:08x}-0([1-4])", rf"PARTUUID={nuovo:08x}-0\1", testo, flags=re.I)
        for vecchio_uuid, nuovo_uuid in uuid.items():
            if vecchio_uuid and nuovo_uuid:
                nuovo_testo = re.sub(rf"(?<![\w-])UUID={re.escape(vecchio_uuid)}(?![\w-])", f"UUID={nuovo_uuid}",
                                     nuovo_testo, flags=re.I)
        if nuovo_testo != testo:
            with open(f + ".nuovo", "w", encoding="utf-8") as h:
                h.write(nuovo_testo)
            os.replace(f + ".nuovo", f)
            cambiati.append(os.path.basename(f))
    return cambiati


def scrivi_scorta(radice: str, dati: dict) -> None:
    d = os.path.join(radice, "etc/mesh2tak")
    os.makedirs(d, exist_ok=True)
    f = os.path.join(d, "scorta.json")
    with open(f + ".nuovo", "w", encoding="utf-8") as h:
        json.dump(dati, h, ensure_ascii=False)
    os.chmod(f + ".nuovo", 0o644)
    os.replace(f + ".nuovo", f)


def smonta(percorso: str) -> None:
    if os.path.ismount(percorso):
        esegui(["umount", percorso], controlla=False, attesa=120)
        if os.path.ismount(percorso):
            esegui(["umount", "-l", percorso], controlla=False, attesa=60)


# ---------------------------------------------------------------- principale

def principale(a) -> int:
    if os.geteuid() != 0:
        print("Va avviato con sudo:  sudo mesh2tak-scorta")
        return 1
    blocco = open("/run/mesh2tak-scorta.lock", "w")
    try:
        fcntl.flock(blocco, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise Errore("sta gia' preparando una microSD di scorta (un'altra finestra?)")
    registra(f"=== mesh2tak-scorta {VERSIONE}")
    print(f"{B}MicroSD di scorta del Raspberry{N} (mesh2tak {VERSIONE})")

    radice = os.path.realpath(RADICE)
    avvio = AVVIO or next((p for p in (os.path.join(radice, "boot/firmware"), os.path.join(radice, "boot"))
                           if os.path.ismount(p)), "")
    if not os.path.ismount(radice) or not avvio or not os.path.ismount(avvio):
        raise Errore("non trovo la partizione di avvio del Raspberry (/boot/firmware)")
    p_radice, disco, n_radice = partizione_di(radice)
    p_avvio, disco_avvio, n_avvio = partizione_di(avvio)
    if disco_avvio != disco or (n_avvio, n_radice) != (1, 2):
        raise Errore("la microSD del Raspberry non e' fatta come quella di Raspberry Pi OS (avvio nella "
                     "partizione 1, sistema nella 2): non so copiarla")
    with open(f"/dev/{disco}", "rb") as f:
        originale = f.read(SETTORE)
    ident, voci = leggi_mbr(originale)
    if not voci[0] or not voci[1] or voci[2] or voci[3]:
        raise Errore("sulla microSD del Raspberry ci sono partizioni in piu': non so copiarla")
    for n, v in ((1, voci[0]), (2, voci[1])):
        nome = partizioni(disco).get(n, "")
        if intero(f"/sys/block/{disco}/{nome}/start") != v.inizio:
            raise Errore("la tabella delle partizioni non corrisponde a quella in uso: riavvia il Raspberry e "
                         "rilancia")

    # la microSD di scorta
    if a.disco:
        dest = os.path.basename(os.path.realpath(a.disco))
        if not os.path.isdir(f"/sys/block/{dest}"):
            raise Errore(f"{a.disco} non e' un disco intero (es. /dev/sda, non /dev/sda1)")
    else:
        candidati = dischi_usb(disco)
        if not candidati:
            raise Errore("non trovo la microSD di scorta: infilala in un lettore USB, collegalo al Raspberry "
                         "e rilancia lo stesso comando")
        if len(candidati) > 1:
            raise Errore("vedo piu' di un disco USB (" + ", ".join(f"/dev/{d}: {descrivi(d)}" for d in candidati)
                         + "): lascia collegato solo il lettore con la microSD di scorta, oppure indicalo con "
                           "--disco /dev/sdX")
        dest = candidati[0]
    if dest == disco:
        raise Errore("quello indicato e' il disco del Raspberry stesso: non lo tocco")
    perche = in_uso(dest)
    if perche:
        raise Errore(f"la microSD di scorta {perche}: non la tocco (staccala, riattaccala e rilancia)")
    if intero(f"/sys/block/{dest}/queue/logical_block_size") not in (0, SETTORE):
        raise Errore("la microSD di scorta ha settori non da 512 byte: non prevista")
    settori = intero(f"/sys/block/{dest}/size")
    s = os.statvfs(radice)
    usati = (s.f_blocks - s.f_bfree) * s.f_frsize
    serve = int(usati * 1.10) + 512 * 1024 * 1024
    radice_settori = (settori - voci[1].inizio) // ALLINEA * ALLINEA
    if radice_settori * SETTORE < serve:
        raise Errore(f"la microSD di scorta e' troppo piccola ({descrivi(dest)}): servono almeno "
                     f"{(voci[1].inizio * SETTORE + serve) / 1e9:.0f} GB".replace(".", ","))
    descrizione = descrivi(dest)

    print(f"\nCopio il sistema di questo Raspberry ({usati / 1e9:.1f} GB usati) sulla microSD:".replace(".", ","))
    print(f"    {B}{descrizione}{N}  (/dev/{dest})")
    print(f"{G}Tutto quello che c'e' sopra verra' cancellato.{N} Alla fine il gateway si ferma per circa un minuto.")
    if not a.si:
        try:
            risposta = input("Scrivi SI e premi Invio per continuare: ")
        except EOFError:
            risposta = ""
        if risposta.strip().upper() != "SI":
            print("Non ho toccato niente.")
            return 1

    nuovo = ident
    while nuovo in (ident, 0):
        nuovo = random.getrandbits(32)
    nuove = [Voce(voci[0].stato, voci[0].tipo, voci[0].inizio, voci[0].settori),
             Voce(voci[1].stato, voci[1].tipo, voci[1].inizio, radice_settori)]
    mont_radice, mont_avvio = os.path.join(MONTAGGIO, "radice"), os.path.join(MONTAGGIO, "avvio")
    fermati: List[str] = []
    try:
        passo("1/6  Preparo la microSD di scorta")
        esegui(["wipefs", "-a", "-q", f"/dev/{dest}"], attesa=120, spiega="non riesco a cancellare la microSD")
        with open(f"/dev/{dest}", "r+b", buffering=0) as f:
            f.write(b"\0" * (ALLINEA * SETTORE))
            f.seek(0)
            f.write(mbr_nuovo(originale, nuovo, nuove))
            os.fsync(f.fileno())
        dev = rileggi_partizioni(dest, {1: (nuove[0].inizio, nuove[0].settori),
                                        2: (nuove[1].inizio, nuove[1].settori)})
        etichette = (blkid(f"/dev/{p_avvio}", "LABEL") or "bootfs", blkid(f"/dev/{p_radice}", "LABEL") or "rootfs")
        esegui(["wipefs", "-a", "-q", dev[1]], controlla=False, attesa=60)
        esegui(["wipefs", "-a", "-q", dev[2]], controlla=False, attesa=60)
        esegui(["mkfs.vfat", "-F", "32", "-n", etichette[0][:11], dev[1]], attesa=300,
               spiega="non riesco a preparare la partizione di avvio")
        esegui(["mkfs.ext4", "-F", "-q", "-L", etichette[1][:16], dev[2]], attesa=900,
               spiega="non riesco a preparare la partizione del sistema")
        ok(f"partizioni pronte (avvio e sistema da {radice_settori * SETTORE / 1e9:.1f} GB)".replace(".", ","))
        os.makedirs(mont_radice, exist_ok=True)
        os.makedirs(mont_avvio, exist_ok=True)
        esegui(["mount", dev[2], mont_radice], attesa=60, spiega="non riesco ad aprire la microSD di scorta")
        esegui(["mount", dev[1], mont_avvio], attesa=60, spiega="non riesco ad aprire la microSD di scorta")

        passo("2/6  Copio il sistema (5-15 minuti, il gateway intanto lavora)")
        copia(radice, avvio, mont_radice, mont_avvio, primo=True)
        ok("prima copia fatta")

        passo("3/6  Fermo il gateway per circa un minuto e ricopio i file cambiati")
        try:
            for sv in SERVIZI:
                if attivo(sv):
                    esegui(["systemctl", "stop", sv], controlla=False, attesa=120)
                    fermati.append(sv)
            os.sync()
            copia(radice, avvio, mont_radice, mont_avvio, primo=False)
            ok("seconda copia fatta")
        finally:
            if fermati:
                print(f"\n{B}4/6  Riavvio il gateway{N}", flush=True)
                for sv in reversed(fermati):
                    esegui(["systemctl", "start", sv], controlla=False, attesa=120)
                ok("gateway di nuovo acceso")

        passo("5/6  Sistemo l'avvio della microSD di scorta")
        uuid = {blkid(f"/dev/{p_avvio}", "UUID"): blkid(dev[1], "UUID"),
                blkid(f"/dev/{p_radice}", "UUID"): blkid(dev[2], "UUID")}
        cambiati = sistema_avvio(mont_avvio, mont_radice, ident, nuovo, uuid)
        cmdline = leggi(os.path.join(mont_avvio, "cmdline.txt"))
        if f"{ident:08x}-02" in cmdline.lower():
            raise Errore("cmdline.txt della scorta punta ancora alla microSD del Raspberry")
        ok("avvio sistemato" + (f" ({', '.join(cambiati)})" if cambiati else ""))
        quando = time.time()
        dati = {"quando": round(quando), "disco": descrizione, "versione": VERSIONE}
        scrivi_scorta(mont_radice, dati)
        os.sync()
    finally:
        smonta(mont_avvio)
        smonta(mont_radice)

    passo("6/6  Controllo la microSD di scorta")
    r = esegui(["fsck.vfat", "-n", dev[1]], controlla=False, attesa=300)
    if r.returncode != 0:
        raise Errore("la partizione di avvio della scorta ha errori: rilancia per rifare la copia")
    r = esegui(["e2fsck", "-f", "-n", dev[2]], controlla=False, attesa=900)
    if r.returncode != 0:
        raise Errore("la partizione del sistema della scorta ha errori: rilancia per rifare la copia")
    scrivi_scorta(radice, dati)
    ok("microSD di scorta controllata")
    giorno = time.strftime("%d/%m/%Y", time.localtime(quando))
    print(f"\n{B}MicroSD di scorta pronta{N} (copia del {giorno}).")
    info("1. Stacca il lettore USB dal Raspberry e togli la microSD.")
    info(f"2. Scrivici sopra la data ({giorno}) e conservala vicino al Raspberry.")
    info("Se un giorno la microSD del Raspberry si rompe: spegni il Raspberry, metti questa al suo posto e")
    info("riaccendilo. Riparte uguale, con lo stesso certificato e la stessa approvazione nella rete.")
    info("Non usarla in un altro Raspberry acceso insieme a questo: avrebbero la stessa identita'.")
    info("Rifai la copia dopo ogni aggiornamento del gateway:  sudo mesh2tak-scorta")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mesh2tak-scorta", description=__doc__.split("\n")[0])
    ap.add_argument("--si", action="store_true", help="non chiedere conferma")
    ap.add_argument("--disco", help="il disco della microSD di scorta (es. /dev/sda), se i dischi USB sono piu' d'uno")
    a = ap.parse_args(argv)
    try:
        return principale(a)
    except Errore as e:
        registra(f"ERRORE: {e}")
        print(f"\n{R}✘ {e}{N}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrotto: la microSD di scorta non e' utilizzabile, rilancia per rifarla.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
