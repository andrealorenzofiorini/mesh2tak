"""Immagine per Raspberry Pi Imager (1.10.0): immagine/costruisci.py su un'immagine finta a due partizioni, con un
elenco ufficiale finto. Serve root (monta l'immagine): senza, la prova si salta."""
import hashlib
import json
import re
import os
import shutil
import struct
import subprocess
import sys

import pytest

QUI = os.path.dirname(os.path.abspath(__file__))
RADICE_PROGETTO = os.path.dirname(QUI)
MB = 1 << 20

pytestmark = pytest.mark.skipif(os.geteuid() != 0 or not shutil.which("mkfs.ext4") or not shutil.which("xz"),
                                reason="serve root con mkfs.ext4 e xz")


def somma(f):
    return hashlib.sha256(open(f, "rb").read()).hexdigest()


def immagine_finta(cartella):
    """Come Raspberry Pi OS: tabella MBR, una partizione FAT (vuota) e una ext4 con systemd e python3."""
    sistema = cartella / "sistema"
    (sistema / "etc/systemd/system").mkdir(parents=True)
    (sistema / "usr/bin").mkdir(parents=True)
    (sistema / "usr/bin/python3").write_text("#!/bin/sh\n")
    (sistema / "etc/hostname").write_text("raspberrypi\n")
    img = cartella / "base.img"
    with open(img, "wb") as f:
        f.truncate(42 * MB)
    subprocess.run(["mkfs.ext4", "-q", "-F", "-d", str(sistema), "-E", f"offset={9 * MB}", str(img), f"{32 * 1024}k"],
                   check=True)
    mbr = bytearray(512)
    for i, (tipo, inizio, lunghezza) in enumerate(((0x0C, 1 * MB, 8 * MB), (0x83, 9 * MB, 32 * MB))):
        mbr[446 + 16 * i + 4] = tipo
        mbr[446 + 16 * i + 8: 446 + 16 * i + 16] = struct.pack("<II", inizio // 512, lunghezza // 512)
    mbr[510:512] = b"\x55\xaa"
    with open(img, "r+b") as f:
        f.write(mbr)
    return img


def elenco_finto(cartella, img, **cambi):
    estratta = somma(img)
    subprocess.run(["xz", "-0", "-k", str(img)], check=True)
    voce = {"name": "Raspberry Pi OS Lite (64-bit)", "description": "senza desktop", "icon": "https://x/i.png",
            "url": f"file://{img}.xz", "extract_size": os.path.getsize(img), "extract_sha256": estratta,
            "image_download_size": os.path.getsize(f"{img}.xz"), "image_download_sha256": somma(f"{img}.xz"),
            "release_date": "2026-10-20", "init_format": "cloudinit-rpi", "devices": ["pi5-64bit", "pi3-64bit"],
            "capabilities": ["rpi_connect"], "architecture": "armv8"}
    voce.update(cambi)
    for campo in [c for c, v in cambi.items() if v is None]:      # come nell'elenco vero: campo che manca
        del voce[campo]
    elenco = {"imager": {"latest_version": "2.0.6", "url": "https://www.raspberrypi.com/software/",
                         "devices": [{"name": "Raspberry Pi 5", "tags": ["pi5-64bit", "pi5-32bit"], "default": True},
                                     {"name": "Raspberry Pi 3", "tags": ["pi3-64bit", "pi3-32bit"]},
                                     {"name": "Raspberry Pi 1", "tags": ["pi1-32bit"]},
                                     {"name": "No filtering", "tags": []}]},
              "os_list": [{"name": "Raspberry Pi OS (64-bit)", "url": "https://x/desktop.img.xz"},
                          {"name": "Raspberry Pi OS (other)", "subitems": [
                              {"name": "Raspberry Pi OS Lite (32-bit)", "url": "https://x/lite32.img.xz"}, voce]}]}
    f = cartella / "elenco.json"
    f.write_text(json.dumps(elenco))
    return f


def costruisci(cartella, elenco, agente):
    return subprocess.run([sys.executable, os.path.join(RADICE_PROGETTO, "immagine/costruisci.py"), "--versione",
                           "1.10.0", "--agente", str(agente), "--uscita", str(cartella / "uscita"), "--url-base",
                           "https://github.com/prova/mesh2tak/releases/download/v1.10.0", "--elenco", str(elenco),
                           "--livello-xz", "0"], capture_output=True, text=True, timeout=300)


def test_immagine_con_il_primo_avvio_e_il_suo_elenco(tmp_path):
    img = immagine_finta(tmp_path)
    agente = tmp_path / "agente.py"
    agente.write_text("#!/usr/bin/env python3\nprint('primo avvio')\n")
    # l'elenco ufficiale vero da' solo la somma dell'immagine scompattata, non quella del file compresso
    r = costruisci(tmp_path, elenco_finto(tmp_path, img, image_download_sha256=None), agente)
    assert r.returncode == 0, r.stdout + r.stderr
    uscita = tmp_path / "uscita"
    assert sorted(os.listdir(uscita)) == ["icona-imager.png", "imager.json", "ponte-meshtastic-1.10.0.img.xz"]
    j = json.loads((uscita / "imager.json").read_text())
    voce = j["os_list"][0]
    base = "https://github.com/prova/mesh2tak/releases/download/v1.10.0"
    assert len(j["os_list"]) == 1 and voce["name"] == "Ponte Meshtastic (mesh2tak)"
    assert voce["url"] == f"{base}/ponte-meshtastic-1.10.0.img.xz" and voce["icon"] == f"{base}/icona-imager.png"
    # si personalizza come l'immagine ufficiale (Wi-Fi, utente, chiavi SSH) e gira sugli stessi Raspberry
    assert (voce["init_format"], voce["devices"], voce["capabilities"]) == ("cloudinit-rpi", ["pi5-64bit", "pi3-64bit"],
                                                                           ["rpi_connect"])
    assert [d["name"] for d in j["imager"]["devices"]] == ["Raspberry Pi 5", "Raspberry Pi 3"]
    assert j["imager"]["default_os"] == voce["name"] and "default" not in j["imager"]["devices"][0]
    finale = uscita / "ponte-meshtastic-1.10.0.img.xz"
    assert voce["image_download_size"] == os.path.getsize(finale) and voce["image_download_sha256"] == somma(finale)
    subprocess.run(["xz", "-dk", str(finale)], check=True)
    nuova = str(finale)[:-3]
    assert voce["extract_size"] == os.path.getsize(nuova) == 42 * MB and voce["extract_sha256"] == somma(nuova)
    assert open(nuova, "rb").read(512) == open(img, "rb").read(512)          # tabella delle partizioni intatta
    punto = tmp_path / "monta"
    punto.mkdir()
    subprocess.run(["mount", "-o", f"loop,ro,offset={9 * MB},sizelimit={32 * MB}", nuova, str(punto)], check=True)
    try:
        programma = punto / "usr/local/lib/mesh2tak/primo-avvio.py"
        unita = punto / "etc/systemd/system/mesh2tak-primo-avvio.service"
        collegamento = punto / "etc/systemd/system/multi-user.target.wants/mesh2tak-primo-avvio.service"
        assert programma.read_text() == agente.read_text()
        st = os.stat(programma)
        assert (st.st_uid, st.st_gid, st.st_mode & 0o777) == (0, 0, 0o755)
        testo = unita.read_text()
        assert "ExecStart=/usr/bin/python3 -I /usr/local/lib/mesh2tak/primo-avvio.py" in testo
        assert "WantedBy=multi-user.target" in testo and "cloud-final" not in testo      # niente giri di dipendenze
        assert os.readlink(collegamento) == "/etc/systemd/system/mesh2tak-primo-avvio.service"
        assert "mesh2tak 1.10.0" in (punto / "etc/mesh2tak-immagine").read_text()
        assert (punto / "etc/hostname").read_text() == "raspberrypi\n"
        # nell'immagine nessun indirizzo di una rete vera
        indirizzi = re.findall(r"https?://[^\s\"']+", testo + programma.read_text())
        assert all(i.startswith("https://github.com/") for i in indirizzi), indirizzi
    finally:
        subprocess.run(["umount", str(punto)], check=True)


def test_immagine_diversa_da_quella_pubblicata_rifiutata(tmp_path):
    img = immagine_finta(tmp_path)
    agente = tmp_path / "agente.py"
    agente.write_text("x\n")
    r = costruisci(tmp_path, elenco_finto(tmp_path, img, extract_sha256="0" * 64), agente)
    assert r.returncode != 0 and "somma di controllo" in r.stderr
    assert not os.path.exists(tmp_path / "uscita/imager.json")
    os.unlink(f"{img}.xz")
    r = costruisci(tmp_path, elenco_finto(tmp_path, img, init_format=""), agente)
    assert r.returncode != 0 and "init_format" in r.stderr
    os.unlink(f"{img}.xz")
    r = costruisci(tmp_path, elenco_finto(tmp_path, img, image_download_sha256="1" * 64), agente)
    assert r.returncode != 0 and "somma di controllo" in r.stderr
    os.unlink(f"{img}.xz")
    r = costruisci(tmp_path, elenco_finto(tmp_path, img, image_download_sha256=None, extract_sha256=None),
                   agente)
    assert r.returncode != 0 and "non mi fido" in r.stderr
