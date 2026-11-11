#!/usr/bin/env python3
"""Costruisce l'immagine "Ponte Meshtastic" per Raspberry Pi Imager e il suo elenco (imager.json).

Parte dall'immagine ufficiale di Raspberry Pi OS Lite (64 bit): la prende dall'elenco ufficiale di Imager, ne
controlla le somme, ci aggiunge solo il programma del primo avvio (dist/mesh2tak-primo-avvio.py) con il suo
servizio, e la ricomprime. Dentro non c'e' nessun indirizzo, chiave o certificato: l'indirizzo della pagina
della rete arriva con la chiave d'invito che chi prepara la microSD incolla in Imager.

Serve root (monta la partizione del sistema dell'immagine). Lo lancia GitHub Actions a ogni release:

  sudo python3 immagine/costruisci.py --versione 1.10.0 --agente dist/mesh2tak-primo-avvio.py \\
       --uscita dist --url-base https://github.com/UTENTE/mesh2tak/releases/download/v1.10.0
"""
import argparse
import datetime
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import urllib.request

ELENCO_UFFICIALE = "https://downloads.raspberrypi.com/os_list_imagingutility_v4.json"
NOME_UFFICIALE = "Raspberry Pi OS Lite (64-bit)"
NOME = "Ponte Meshtastic (mesh2tak)"
QUI = os.path.dirname(os.path.abspath(__file__))


def info(testo):
    print(testo, flush=True)


def scarica(url, destinazione):
    if url.startswith("file://") or os.path.exists(url):
        shutil.copyfile(url[7:] if url.startswith("file://") else url, destinazione)
        return
    richiesta = urllib.request.Request(url, headers={"User-Agent": "mesh2tak-immagine"})
    with urllib.request.urlopen(richiesta, timeout=120) as r, open(destinazione, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)


def somma(percorso):
    h = hashlib.sha256()
    with open(percorso, "rb") as f:
        for pezzo in iter(lambda: f.read(1 << 20), b""):
            h.update(pezzo)
    return h.hexdigest()


def trova(voci, nome=NOME_UFFICIALE, lavoro=None, profondita=0):
    """La voce di Raspberry Pi OS Lite a 64 bit nell'elenco ufficiale (sta dentro "Raspberry Pi OS (other)"; se
    quell'elenco e' a parte, "subitems_url", lo scarica)."""
    for v in voci:
        if v.get("name") == nome and v.get("url"):
            return v
        sotto = v.get("subitems") or []
        if not sotto and v.get("subitems_url") and "Raspberry Pi OS" in str(v.get("name")) and lavoro and profondita < 3:
            f = os.path.join(lavoro, f"sottoelenco-{profondita}.json")
            scarica(v["subitems_url"], f)
            d = json.load(open(f, encoding="utf-8"))
            sotto = d.get("os_list", d) if isinstance(d, dict) else d
        trovata = trova(sotto if isinstance(sotto, list) else [], nome, lavoro, profondita + 1)
        if trovata:
            return trovata
    return None


def partizioni(immagine):
    """(inizio, lunghezza) in byte delle partizioni della tabella MBR, per tipo."""
    with open(immagine, "rb") as f:
        mbr = f.read(512)
    if mbr[510:512] != b"\x55\xaa":
        sys.exit("l'immagine non ha una tabella delle partizioni MBR")
    trovate = []
    for i in range(4):
        voce = mbr[446 + 16 * i: 462 + 16 * i]
        tipo, inizio, settori = voce[4], *struct.unpack("<II", voce[8:16])
        if tipo and settori:
            trovate.append((tipo, inizio * 512, settori * 512))
    return trovate


def aggiungi_primo_avvio(immagine, agente, versione, base, lavoro):
    """Monta la partizione del sistema (ext4) e ci mette programma, servizio (gia' attivo) e una riga su cosa e'."""
    linux = [p for p in partizioni(immagine) if p[0] == 0x83]
    if len(linux) != 1:
        sys.exit(f"nell'immagine mi aspetto una partizione Linux, ne trovo {len(linux)}")
    _tipo, inizio, lunghezza = linux[0]
    punto = os.path.join(lavoro, "sistema")
    os.makedirs(punto, exist_ok=True)
    subprocess.run(["mount", "-o", f"loop,offset={inizio},sizelimit={lunghezza}", immagine, punto], check=True)
    try:
        if not os.path.isdir(os.path.join(punto, "etc/systemd/system")) or not os.path.exists(
                os.path.join(punto, "usr/bin/python3")):
            sys.exit("la partizione montata non sembra Raspberry Pi OS (manca systemd o python3)")
        programma = os.path.join(punto, "usr/local/lib/mesh2tak/primo-avvio.py")
        os.makedirs(os.path.dirname(programma), exist_ok=True)
        shutil.copyfile(agente, programma)
        unita = os.path.join(punto, "etc/systemd/system/mesh2tak-primo-avvio.service")
        shutil.copyfile(os.path.join(QUI, "mesh2tak-primo-avvio.service"), unita)
        voluti = os.path.join(punto, "etc/systemd/system/multi-user.target.wants")
        os.makedirs(voluti, exist_ok=True)
        collegamento = os.path.join(voluti, "mesh2tak-primo-avvio.service")
        if os.path.lexists(collegamento):
            os.unlink(collegamento)
        os.symlink("/etc/systemd/system/mesh2tak-primo-avvio.service", collegamento)
        nota = os.path.join(punto, "etc/mesh2tak-immagine")
        with open(nota, "w", encoding="utf-8") as f:
            f.write(f"Ponte Meshtastic (mesh2tak {versione}), immagine del {datetime.date.today().isoformat()}\n"
                    f"Base: {base.get('name')} del {base.get('release_date', '?')}\n{base.get('url')}\n")
        for f, modo in ((os.path.dirname(programma), 0o755), (programma, 0o755), (unita, 0o644), (nota, 0o644)):
            os.chown(f, 0, 0)
            os.chmod(f, modo)
        os.lchown(collegamento, 0, 0)
    finally:
        subprocess.run(["sync"], check=False)
        subprocess.run(["umount", punto], check=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--versione", required=True)
    ap.add_argument("--agente", required=True, help="dist/mesh2tak-primo-avvio.py")
    ap.add_argument("--uscita", required=True)
    ap.add_argument("--url-base", required=True, help="da dove si scaricheranno immagine e icona (release)")
    ap.add_argument("--elenco", default=ELENCO_UFFICIALE, help="elenco ufficiale di Imager (URL o file)")
    ap.add_argument("--lavoro", default=None, help="cartella di lavoro (servono circa 4 GB)")
    ap.add_argument("--livello-xz", default="6")
    a = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("serve root (monta l'immagine): sudo python3 immagine/costruisci.py ...")
    lavoro = a.lavoro or os.path.join(a.uscita, "lavoro-immagine")
    os.makedirs(lavoro, exist_ok=True)
    os.makedirs(a.uscita, exist_ok=True)

    info(f"1/5  Elenco ufficiale di Raspberry Pi Imager: {a.elenco}")
    elenco_file = os.path.join(lavoro, "elenco.json")
    scarica(a.elenco, elenco_file)
    elenco = json.load(open(elenco_file, encoding="utf-8"))
    base = trova(elenco.get("os_list") or [], lavoro=lavoro)
    if not base:
        sys.exit(f"nell'elenco ufficiale non trovo '{NOME_UFFICIALE}'")
    info(f"     {base['name']} del {base.get('release_date', '?')}: {base['url']}")

    info("2/5  Scarico l'immagine ufficiale e la controllo")
    if not base.get("image_download_sha256") and not base.get("extract_sha256"):
        sys.exit("l'elenco ufficiale non da' somme di controllo per l'immagine: non mi fido")
    compressa = os.path.join(lavoro, "base.img.xz")
    scarica(base["url"], compressa)
    if base.get("image_download_sha256") and somma(compressa) != base["image_download_sha256"]:
        sys.exit("l'immagine scaricata non e' quella pubblicata (somma di controllo diversa)")
    immagine = os.path.join(lavoro, "ponte.img")
    with open(immagine, "wb") as f:
        subprocess.run(["xz", "-dc", compressa], stdout=f, check=True)
    os.unlink(compressa)
    if base.get("extract_sha256") and somma(immagine) != base["extract_sha256"]:
        sys.exit("l'immagine scompattata non e' quella pubblicata (somma di controllo diversa)")

    info("3/5  Aggiungo il primo avvio del ponte (nient'altro)")
    aggiungi_primo_avvio(immagine, a.agente, a.versione, base, lavoro)

    info("4/5  Ricomprimo (qualche minuto)")
    nome_file = f"ponte-meshtastic-{a.versione}.img.xz"
    finale = os.path.join(a.uscita, nome_file)
    dimensione, somma_immagine = os.path.getsize(immagine), somma(immagine)
    with open(finale, "wb") as f:
        subprocess.run(["xz", "-T0", f"-{a.livello_xz}", "-c", immagine], stdout=f, check=True)
    os.unlink(immagine)

    info("5/5  Elenco per Raspberry Pi Imager (imager.json)")
    shutil.copyfile(os.path.join(QUI, "icona.png"), os.path.join(a.uscita, "icona-imager.png"))
    voce = {
        "name": NOME,
        "description": f"Ponte Meshtastic-TAK {a.versione}: si installa da solo dalla pagina della rete "
                       f"(base: Raspberry Pi OS Lite 64 bit del {base.get('release_date', '?')})",
        "icon": f"{a.url_base}/icona-imager.png",
        "url": f"{a.url_base}/{nome_file}",
        "extract_size": dimensione,
        "extract_sha256": somma_immagine,
        "image_download_size": os.path.getsize(finale),
        "image_download_sha256": somma(finale),
        "release_date": datetime.date.today().isoformat(),
        "website": a.url_base.split("/releases/")[0],
    }
    # come si personalizza (nome, Wi-Fi, utente, chiavi SSH) e su quali Raspberry gira: come l'immagine ufficiale
    for campo in ("init_format", "devices", "capabilities", "architecture"):
        if campo in base:
            voce[campo] = base[campo]
    if not voce.get("init_format"):
        sys.exit("l'immagine ufficiale non dice come si personalizza (init_format): Imager non potrebbe "
                 "scriverci Wi-Fi e chiave d'invito")
    imager = dict(elenco.get("imager") or {})
    adatti = set(voce.get("devices") or [])
    imager["devices"] = [d for d in imager.get("devices") or [] if not adatti or adatti & set(d.get("tags") or [])]
    for d in imager["devices"]:
        d.pop("default", None)
    imager["default_os"] = NOME
    with open(os.path.join(a.uscita, "imager.json"), "w", encoding="utf-8") as f:
        json.dump({"imager": imager, "os_list": [voce]}, f, ensure_ascii=False, indent=1)
        f.write("\n")
    shutil.rmtree(lavoro, ignore_errors=True)
    info(f"Fatto: {finale} ({voce['image_download_size'] >> 20} MB), {os.path.join(a.uscita, 'imager.json')}")


if __name__ == "__main__":
    main()
