"""MicroSD di scorta (1.7.0) su dischi veri (dispositivi loop) fatti come Raspberry Pi OS: copia completa con
proprietari, collegamenti e attributi, cartelle di sistema escluse, identita' del disco nuova in cmdline.txt e
fstab, ponte fermato e riacceso, data della scorta su tutte e due le microSD; e i casi in cui non tocca niente.

Il kernel delle prove non monta il FAT: la partizione di avvio qui e' ext4 (mkfs.vfat e fsck.vfat finti che
registrano gli argomenti); mkfs.vfat e fsck.vfat veri sono provati a parte su un file."""
import json
import os
import shutil
import stat
import struct
import subprocess
import sys

import pytest

QUI = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(QUI), "scorta.py")
ID = 0x1A2B3C4D
MIB = 2048                      # settori da 512 byte in 1 MiB

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0 or not all(shutil.which(c) for c in ("losetup", "partx", "mkfs.vfat", "fsck.vfat",
                                                          "mkfs.ext4", "rsync", "wipefs", "blkid")),
    reason="servono root, losetup, partx, mkfs.vfat, mkfs.ext4 e rsync")


def mbr(ident, voci):
    b = bytearray(512)
    struct.pack_into("<I", b, 440, ident)
    for i, (tipo, inizio, settori) in enumerate(voci):
        o = 446 + 16 * i
        b[o + 4] = tipo
        struct.pack_into("<II", b, o + 8, inizio, settori)
    b[510:512] = b"\x55\xaa"
    return bytes(b)


def leggi_mbr(dev):
    with open(dev, "rb") as f:
        b = f.read(512)
    voci = []
    for i in range(4):
        o = 446 + 16 * i
        voci.append((b[o + 4],) + struct.unpack_from("<II", b, o + 8))
    return struct.unpack_from("<I", b, 440)[0], voci


def x(*cmd, **kw):
    """Un comando che deve riuscire. Formattare un disco di prova appena creato a volte non riesce al primo colpo
    (visto su GitHub: la partizione del disco finto di prima non e' ancora stata lasciata dal sistema): si riprova
    qualche volta e, se proprio non va, l'errore dice che cosa ha risposto il comando."""
    import time
    tentativi = 6 if cmd[0].startswith("mkfs") else 1
    for i in range(tentativi):
        r = subprocess.run(list(cmd), capture_output=True, text=True, **kw)
        if r.returncode == 0:
            return r
        if i + 1 < tentativi:
            subprocess.run(["udevadm", "settle", "--timeout=5"], capture_output=True)
            time.sleep(1 + i)
    raise AssertionError(f"{' '.join(cmd)}: uscita {r.returncode}\n{r.stderr.strip()}\n{r.stdout.strip()}")


class Dischi:
    def __init__(self, cartella):
        self.cartella = cartella
        self.loop = []
        self.montati = []

    def nuovo(self, nome, mb, voci=None, ident=ID):
        img = self.cartella / f"{nome}.img"
        x("truncate", "-s", f"{mb}M", str(img))
        if voci:
            with open(img, "r+b") as f:
                f.write(mbr(ident, voci))
        dev = x("losetup", "-fP", "--show", str(img)).stdout.strip()
        self.loop.append(dev)
        return dev

    def partizioni(self, dev):
        subprocess.run(["partx", "-a", dev], capture_output=True)
        base = os.path.basename(dev)
        out = {}
        for n in sorted(os.listdir(f"/sys/block/{base}")):
            sysp = f"/sys/block/{base}/{n}"
            if n.startswith(base) and os.path.exists(sysp + "/partition"):
                num = int(open(sysp + "/partition").read())
                mag, mino = (int(v) for v in open(sysp + "/dev").read().split(":"))
                nodo = f"/dev/{n}"
                if os.path.lexists(nodo):
                    os.remove(nodo)
                os.mknod(nodo, 0o660 | stat.S_IFBLK, os.makedev(mag, mino))
                out[num] = nodo
        return out

    def monta(self, dev, dove):
        os.makedirs(dove, exist_ok=True)
        x("mount", dev, str(dove))
        self.montati.append(str(dove))

    def chiudi(self):
        for m in reversed(self.montati):
            subprocess.run(["umount", "-l", m], capture_output=True)
        for dev in self.loop:
            base = os.path.basename(dev)
            subprocess.run(["partx", "-d", dev], capture_output=True)
            subprocess.run(["losetup", "-d", dev], capture_output=True)
            for n in os.listdir("/dev"):
                if n.startswith(base + "p"):
                    try:
                        os.remove("/dev/" + n)
                    except OSError:
                        pass


@pytest.fixture
def dischi(tmp_path):
    d = Dischi(tmp_path)
    yield d
    d.chiudi()


@pytest.fixture
def raspberry(dischi, tmp_path):
    """Una microSD come quella di Raspberry Pi OS (avvio FAT 64 MiB, sistema ext4), montata come il Pi."""
    dev = dischi.nuovo("raspberry", 200, [(0x0C, 8 * MIB, 64 * MIB), (0x83, 72 * MIB, 200 * MIB - 72 * MIB)])
    p = dischi.partizioni(dev)
    x("mkfs.ext4", "-F", "-q", "-L", "bootfs", p[1])     # sul Raspberry e' FAT (vedi sopra)
    x("mkfs.ext4", "-F", "-q", "-L", "rootfs", p[2])
    radice = tmp_path / "pi"
    dischi.monta(p[2], radice)
    (radice / "boot/firmware").mkdir(parents=True)
    dischi.monta(p[1], radice / "boot/firmware")
    avvio = radice / "boot/firmware"
    (avvio / "cmdline.txt").write_text(f"console=serial0,115200 console=tty1 root=PARTUUID={ID:08x}-02 "
                                       "rootfstype=ext4 fsck.repair=yes rootwait\n")
    (avvio / "config.txt").write_text("dtparam=spi=on\n")
    for d in ("etc/mesh2tak", "usr/bin", "var/lib/mesh2tak", "proc", "sys", "dev", "run", "tmp",
              "var/cache/apt/archives", "home/andrea"):
        (radice / d).mkdir(parents=True, exist_ok=True)
    (radice / "etc/fstab").write_text(f"proc /proc proc defaults 0 0\nPARTUUID={ID:08x}-01 /boot/firmware vfat "
                                      f"defaults 0 2\nPARTUUID={ID:08x}-02 / ext4 defaults,noatime 0 1\n")
    conf = radice / "etc/mesh2tak/mesh2tak.conf"
    conf.write_text("[tak]\nhost = takserver.esempio.it\n")
    os.chmod(conf, 0o600)
    stato = radice / "var/lib/mesh2tak/stato"
    stato.write_text("x")
    os.chown(stato, 999, 998)
    (radice / "usr/bin/a").write_text("programma")
    os.link(radice / "usr/bin/a", radice / "usr/bin/b")
    os.symlink("a", radice / "usr/bin/c")
    os.setxattr(radice / "usr/bin/a", "user.prova", b"si")
    (radice / "home/andrea/installa-ponte-meshtastic.sh").write_text("#!/bin/bash\n")
    for f in ("proc/finto", "sys/finto", "dev/finto", "run/finto", "tmp/finto", "var/swap",
              "var/cache/apt/archives/vecchio.deb"):
        (radice / f).write_text("non copiare")
    return {"dev": dev, "radice": radice, "avvio": avvio, "p": p}


def lancia(tmp_path, raspberry, *argomenti, entrata=None):
    stub = tmp_path / "stub"
    stub.mkdir(exist_ok=True)
    (stub / "systemctl").write_text(f"#!/bin/bash\necho \"$*\" >> {tmp_path}/systemctl.log\n"
                                    "case \"$1\" in is-active) exit 0;; esac\nexit 0\n")
    (stub / "mkfs.vfat").write_text(f"#!/bin/bash\necho \"$*\" >> {tmp_path}/fat.log\n"
                                    "[ \"$1 $2 $3\" = \"-F 32 -n\" ] || exit 9\nexec mkfs.ext4 -F -q -L \"$4\" \"$5\"\n")
    (stub / "fsck.vfat").write_text(f"#!/bin/bash\necho \"$*\" >> {tmp_path}/fat.log\n"
                                    "[ \"$1\" = -n ] || exit 9\nexec e2fsck -f -n \"$2\"\n")
    for f in ("systemctl", "mkfs.vfat", "fsck.vfat"):
        os.chmod(stub / f, 0o755)
    amb = dict(os.environ, PATH=f"{stub}:{os.environ['PATH']}", MESH2TAK_SCORTA_RADICE=str(raspberry["radice"]),
               MESH2TAK_SCORTA_AVVIO=str(raspberry["avvio"]), MESH2TAK_SCORTA_MONTAGGIO=str(tmp_path / "mnt"),
               MESH2TAK_SCORTA_LOG=str(tmp_path / "scorta.log"))
    return subprocess.run([sys.executable, SCRIPT] + list(argomenti), env=amb, capture_output=True, text=True,
                          input=entrata, timeout=600)


def test_copia_completa(dischi, raspberry, tmp_path):
    dest = dischi.nuovo("scorta", 800)
    r = lancia(tmp_path, raspberry, "--si", "--disco", dest)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "MicroSD di scorta pronta" in r.stdout
    ident, voci = leggi_mbr(dest)
    assert ident not in (ID, 0)
    assert voci[0] == (0x0C, 8 * MIB, 64 * MIB)
    tipo, inizio, settori = voci[1]
    assert (tipo, inizio) == (0x83, 72 * MIB) and settori == 800 * MIB - 72 * MIB
    assert voci[2][0] == voci[3][0] == 0

    p = dischi.partizioni(dest)
    copia = tmp_path / "copia"
    dischi.monta(p[2], copia)
    (copia / "boot/firmware").mkdir(exist_ok=True)
    dischi.monta(p[1], copia / "boot/firmware")
    cmdline = (copia / "boot/firmware/cmdline.txt").read_text()
    fstab = (copia / "etc/fstab").read_text()
    assert f"root=PARTUUID={ident:08x}-02" in cmdline and f"{ID:08x}" not in cmdline
    assert f"PARTUUID={ident:08x}-01 /boot/firmware" in fstab and f"PARTUUID={ident:08x}-02 / " in fstab
    assert (copia / "boot/firmware/config.txt").read_text() == "dtparam=spi=on\n"
    conf = copia / "etc/mesh2tak/mesh2tak.conf"
    assert conf.read_text().startswith("[tak]") and stat.S_IMODE(conf.stat().st_mode) == 0o600
    s = (copia / "var/lib/mesh2tak/stato").stat()
    assert (s.st_uid, s.st_gid) == (999, 998)
    assert (copia / "usr/bin/a").stat().st_ino == (copia / "usr/bin/b").stat().st_ino
    assert os.readlink(copia / "usr/bin/c") == "a"
    assert os.getxattr(copia / "usr/bin/a", "user.prova") == b"si"
    assert (copia / "home/andrea/installa-ponte-meshtastic.sh").exists()
    for d in ("proc", "sys", "dev", "run", "tmp"):
        assert (copia / d).is_dir() and not os.listdir(copia / d), d
    assert not (copia / "var/swap").exists() and not (copia / "var/cache/apt/archives/vecchio.deb").exists()
    dati = json.loads((copia / "etc/mesh2tak/scorta.json").read_text())
    assert dati == json.loads((raspberry["radice"] / "etc/mesh2tak/scorta.json").read_text())
    assert dati["quando"] > 0 and "GB" in dati["disco"]
    # il sistema originale non e' cambiato
    assert f"PARTUUID={ID:08x}-02" in (raspberry["avvio"] / "cmdline.txt").read_text()
    assert leggi_mbr(raspberry["dev"])[0] == ID
    chiamate = (tmp_path / "systemctl.log").read_text().splitlines()
    fermate = [c for c in chiamate if c.startswith(("stop", "start"))]
    assert fermate == ["stop mesh2tak", "stop meshtasticd", "start meshtasticd", "start mesh2tak"]
    fat = (tmp_path / "fat.log").read_text().splitlines()
    assert fat == [f"-F 32 -n bootfs {p[1]}", f"-n {p[1]}"]


@pytest.mark.skipif(not os.path.exists("/usr/local/sbin/mkfs.fat") and not os.path.exists("/usr/sbin/mkfs.fat"),
                    reason="manca dosfstools")
def test_comandi_fat_veri(tmp_path):
    """Gli stessi argomenti che usa la scorta, con mkfs.vfat e fsck.vfat veri (su un file, senza montarlo)."""
    img = tmp_path / "avvio.img"
    x("truncate", "-s", "64M", str(img))
    x("mkfs.vfat", "-F", "32", "-n", "bootfs", str(img))
    assert subprocess.run(["fsck.vfat", "-n", str(img)], capture_output=True).returncode == 0
    with open(img, "r+b") as f:          # un FAT rovinato: fsck.vfat -n deve dirlo
        f.seek(0x0B)
        f.write(b"\x00\x00")
    assert subprocess.run(["fsck.vfat", "-n", str(img)], capture_output=True).returncode != 0


def test_seconda_copia_sulla_stessa_scorta(dischi, raspberry, tmp_path):
    dest = dischi.nuovo("scorta", 800)
    assert lancia(tmp_path, raspberry, "--si", "--disco", dest).returncode == 0
    primo = leggi_mbr(dest)[0]
    (raspberry["radice"] / "etc/mesh2tak/nuovo.txt").write_text("dopo la prima copia")
    r = lancia(tmp_path, raspberry, "--si", "--disco", dest)
    assert r.returncode == 0, r.stdout + r.stderr
    assert leggi_mbr(dest)[0] not in (primo, ID)
    p = dischi.partizioni(dest)
    dischi.monta(p[2], tmp_path / "copia")
    assert (tmp_path / "copia/etc/mesh2tak/nuovo.txt").read_text() == "dopo la prima copia"


def test_non_tocca_il_disco_del_raspberry(raspberry, tmp_path):
    r = lancia(tmp_path, raspberry, "--si", "--disco", raspberry["dev"])
    assert r.returncode == 1 and "disco del Raspberry stesso" in r.stderr
    assert leggi_mbr(raspberry["dev"])[0] == ID
    assert not (tmp_path / "systemctl.log").exists()


def test_scorta_troppo_piccola(dischi, raspberry, tmp_path):
    dest = dischi.nuovo("piccola", 400)
    r = lancia(tmp_path, raspberry, "--si", "--disco", dest)
    assert r.returncode == 1 and "troppo piccola" in r.stderr
    assert leggi_mbr(dest)[0] == 0


def test_scorta_montata_non_toccata(dischi, raspberry, tmp_path):
    dest = dischi.nuovo("usata", 800, [(0x83, 8 * MIB, 100 * MIB)], ident=0x55667788)
    p = dischi.partizioni(dest)
    x("mkfs.ext4", "-F", "-q", p[1])
    dischi.monta(p[1], tmp_path / "usata")
    r = lancia(tmp_path, raspberry, "--si", "--disco", dest)
    assert r.returncode == 1 and "montato" in r.stderr
    assert leggi_mbr(dest)[0] == 0x55667788


def test_senza_conferma_non_tocca_niente(dischi, raspberry, tmp_path):
    dest = dischi.nuovo("scorta", 800)
    r = lancia(tmp_path, raspberry, "--disco", dest, entrata="no\n")
    assert r.returncode == 1 and "Non ho toccato niente" in r.stdout
    assert leggi_mbr(dest)[0] == 0
    assert not (tmp_path / "systemctl.log").exists()


def test_partizione_intera_rifiutata(raspberry, tmp_path):
    r = lancia(tmp_path, raspberry, "--si", "--disco", raspberry["p"][1])
    assert r.returncode == 1 and "disco intero" in r.stderr
