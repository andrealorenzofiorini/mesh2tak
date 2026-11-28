"""mesh2tak-wifi (dalla 1.24.0): senza rete il Raspberry apre un suo Wi-Fi con una pagina da cui scegliere a quale
Wi-Fi collegarlo. Qui NetworkManager e' finto (un 'nmcli' che tiene lo stato in un file): si prova la logica, non la
radio."""
import json
import os
import re
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from conftest import porta_libera

QUI = os.path.dirname(os.path.abspath(__file__))
PROGRAMMA = os.path.join(os.path.dirname(QUI), "wifi.py")

NMCLI_FINTO = r'''#!/usr/bin/env python3
import json, os, sys
STATO = os.environ["NMCLI_FINTO"]
s = json.load(open(STATO))
a = [x for x in sys.argv[1:]]
s["chiamate"].append(a)
def salva():
    json.dump(s, open(STATO, "w"))
if "--wait" in a:
    i = a.index("--wait"); del a[i:i + 2]
if a[:1] == ["-t"]:
    campi = a[2]; resto = a[3:]
    if resto == ["device"]:
        print("lo:loopback")
        if s["wifi"]: print("wlan0:wifi")
        print("eth0:ethernet")
    elif resto == ["connection", "show", "--active"]:
        for n, t in s["attivi"]:
            print(f"{n}:{t}:activated")
        if os.path.exists(STATO + ".cavo"):
            print("Wired connection 1:802-3-ethernet:activated")
    elif resto == ["connection", "show"]:
        print("Wired connection 1:802-3-ethernet:yes")
        print("mesh2tak-configura:802-11-wireless:no")
        for n in s.get("note", []):
            print(f"{n}:802-11-wireless:yes")
    elif resto[:3] == ["device", "wifi", "list"]:
        for n, forza, prot in s["reti"]:
            print(n.replace("\\", "\\\\").replace(":", "\\:") + f":{forza}:{prot}")
    salva(); sys.exit(0)
if a[:2] == ["-g", "802-11-wireless.ssid"]:
    print(a[-1].replace(":", "\\:")); salva(); sys.exit(0)        # (nelle prove il profilo si chiama come la rete)
if a[:2] == ["connection", "load"]:
    salva(); sys.exit(0)
if a[:3] == ["connection", "up", "id"]:
    nome = a[3]
    if nome in s.get("note", []):
        if s["password"].get(nome) is not None and os.path.exists(STATO + ".torna"):
            s["attivi"].append([nome, "802-11-wireless"]); salva(); sys.exit(0)
        print("Error: Connection activation failed"); salva(); sys.exit(4)
    testo = open(os.path.join(os.environ["MESH2TAK_WIFI_RADICE"], "etc/NetworkManager/system-connections", nome + ".nmconnection")).read()
    if nome == "mesh2tak-configura":
        if s.get("ap_rotto"):
            print("Error: Connection activation failed: AP mode not supported"); salva(); sys.exit(4)
        s["attivi"] = [x for x in s["attivi"] if x[1] != "802-11-wireless"] + [[nome, "802-11-wireless"]]
        s["dns_alla_apertura"] = os.path.exists(os.path.join(os.environ["MESH2TAK_WIFI_RADICE"], "etc/NetworkManager/dnsmasq-shared.d/mesh2tak-configura.conf"))
        salva(); sys.exit(0)
    ssid = bytes(int(x) for x in testo.split("ssid=")[1].split("\n")[0].split(";") if x).decode()
    psk = testo.split("psk=")[1].split("\n")[0].replace("\\\\", "\\") if "psk=" in testo else ""
    s["provate"].append([ssid, psk, "sae" if "key-mgmt=sae" in testo else "psk" if psk else "aperta"])
    giusta = s["password"].get(ssid)
    if giusta is None:
        print("Error: Connection activation failed: No network with SSID '%s' found." % ssid); salva(); sys.exit(10)
    if giusta != psk:
        print("Error: Connection activation failed: Secrets were required, but not provided."); salva(); sys.exit(4)
    s["attivi"].append([nome, "802-11-wireless"]); salva(); sys.exit(0)
if a[:3] == ["connection", "down", "id"]:
    s["attivi"] = [x for x in s["attivi"] if x[0] != a[3]]; salva(); sys.exit(0)
if a[:3] == ["connection", "delete", "id"]:
    s["cancellate"].append(a[3]); salva(); sys.exit(0)
salva(); sys.exit(0)
'''


class Impianto:
    def __init__(self, cartella, attese=0.01):
        self.radice = cartella / "radice"
        (self.radice / "etc/mesh2tak").mkdir(parents=True)
        (self.radice / "etc/mesh2tak/wifi-configura.conf").write_text("nome=Gateway-Prova\npassword=abcd-efgh-jk\n")
        self.stato_nm = cartella / "nm.json"
        self.scrivi(wifi=True, attivi=[], reti=[["Casa: sopra", 82, "WPA2"], ["Casa: sopra", 40, "WPA2"],
                                                ["Rifugio", 55, "WPA2 WPA3"], ["Solo3", 30, "WPA3"], ["Bar", 20, ""],
                                                ["Azienda", 70, "WPA2 802.1X"], ["", 90, "WPA2"]],
                    password={"Casa: sopra": "segreta;con\\barra", "Rifugio": "password1", "Solo3": "password3", "Bar": ""},
                    chiamate=[], provate=[], cancellate=[])
        bin_ = cartella / "bin"
        bin_.mkdir()
        (bin_ / "nmcli").write_text(NMCLI_FINTO.replace("#!/usr/bin/env python3", "#!" + sys.executable))
        (bin_ / "nmcli").chmod(0o755)
        self.porta = porta_libera()
        self.ambiente = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", NMCLI_FINTO=str(self.stato_nm),
                             MESH2TAK_WIFI_RADICE=str(self.radice), MESH2TAK_WIFI_ATTESE=str(attese),
                             MESH2TAK_WIFI_PORTA=str(self.porta))
        self.processo = None
        self.uscita = cartella / "uscita.log"

    def leggi(self):
        for _ in range(50):
            try:
                return json.loads(self.stato_nm.read_text())
            except ValueError:
                time.sleep(0.02)
        raise AssertionError("stato di nmcli non leggibile")

    def scrivi(self, **cose):
        d = self.leggi() if self.stato_nm.exists() else {}
        d.update(cose)
        self.stato_nm.write_text(json.dumps(d))

    def avvia(self):
        self.processo = subprocess.Popen([sys.executable, "-I", PROGRAMMA, "servizio"], env=self.ambiente,
                                         stdout=open(self.uscita, "w"), stderr=subprocess.STDOUT)

    def ferma(self):
        if self.processo:
            self.processo.terminate()
            self.processo.wait(10)

    def registro(self):
        return self.uscita.read_text()

    def chiedi(self, percorso="/", dati=None, host=None):
        r = urllib.request.Request(f"http://127.0.0.1:{self.porta}{percorso}",
                                   data=urllib.parse.urlencode(dati).encode() if dati is not None else None,
                                   headers={"Host": host or f"10.42.0.1:{self.porta}"})

        class NonSeguire(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        apri = urllib.request.build_opener(urllib.request.ProxyHandler({}), NonSeguire())
        try:
            with apri.open(r, timeout=10) as x:
                return x.status, x.read().decode(), dict(x.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), dict(e.headers)

    def aperto(self):
        return ["mesh2tak-configura", "802-11-wireless"] in self.leggi()["attivi"]


def aspetta(cosa, limite=20.0):
    fine = time.time() + limite
    while time.time() < fine:
        if cosa():
            return True
        time.sleep(0.05)
    return bool(cosa())


@pytest.fixture
def impianto(tmp_path):
    i = Impianto(tmp_path)
    yield i
    i.ferma()


def gettone(pagina):
    return re.search(r"name=gettone value='([^']+)'", pagina).group(1)


def test_con_la_rete_non_apre_niente(impianto):
    impianto.scrivi(attivi=[["Wired connection 1", "802-3-ethernet"]])
    impianto.avvia()
    time.sleep(4)          # con le attese delle prove: molto piu' del tempo "senza rete"
    assert not impianto.aperto() and "aperto il Wi-Fi" not in impianto.registro()
    assert not any(c[:2] == ["connection", "up"] for c in impianto.leggi()["chiamate"])


def test_senza_rete_apre_il_suo_wifi_e_la_pagina(impianto):
    impianto.avvia()
    assert aspetta(impianto.aperto), impianto.registro()
    assert impianto.leggi()["dns_alla_apertura"] is True
    profilo = impianto.radice / "etc/NetworkManager/system-connections/mesh2tak-configura.nmconnection"
    testo = profilo.read_text()
    assert stat.S_IMODE(profilo.stat().st_mode) == 0o600
    assert "mode=ap" in testo and "ssid=Gateway-Prova" in testo and "psk=abcd-efgh-jk" in testo
    assert "method=shared" in testo and "autoconnect=false" in testo
    assert aspetta(lambda: impianto.chiedi()[0] == 200)
    codice, pagina, intestazioni = impianto.chiedi()
    # reti: la piu' forte prima, una riga per nome, senza quelle senza nome
    assert pagina.index("Casa: sopra") < pagina.index("Azienda") < pagina.index("Rifugio") < pagina.index("Bar")
    assert pagina.count("Casa: sopra") == 2 and "senza password" in pagina    # (valore e testo della stessa voce)
    assert "default-src 'none'" in intestazioni["Content-Security-Policy"]
    # il telefono che prova un suo sito viene mandato alla pagina
    codice, _, intestazioni = impianto.chiedi("/generate_204", host="connectivitycheck.gstatic.com")
    assert codice == 302 and intestazioni["Location"] == f"http://10.42.0.1:{impianto.porta}/"
    assert aspetta(lambda: "aperto il Wi-Fi 'Gateway-Prova'" in impianto.registro()), impianto.registro()
    assert "abcd-efgh-jk" not in impianto.registro()


def test_rete_scelta_e_password_giusta(impianto):
    impianto.avvia()
    assert aspetta(lambda: impianto.aperto() and impianto.chiedi()[0] == 200)
    g = gettone(impianto.chiedi()[1])
    codice, pagina, _ = impianto.chiedi("/collega", {"gettone": g, "rete": "Casa: sopra", "altra": "",
                                                    "password": "segreta;con\\barra"})
    assert codice == 200 and "Provo a collegarmi a «Casa: sopra»" in pagina
    assert aspetta(lambda: any(n.startswith("mesh2tak-wifi-") for n, _ in impianto.leggi()["attivi"])), impianto.registro()
    d = impianto.leggi()
    assert d["provate"] == [["Casa: sopra", "segreta;con\\barra", "psk"]] and not impianto.aperto()
    [file_rete] = (impianto.radice / "etc/NetworkManager/system-connections").glob("mesh2tak-wifi-*.nmconnection")
    assert stat.S_IMODE(file_rete.stat().st_mode) == 0o600 and "autoconnect=true" in file_rete.read_text()
    assert "psk=segreta;con\\\\barra\n" in file_rete.read_text()        # nel file la barra e' raddoppiata
    assert not (impianto.radice / "etc/NetworkManager/dnsmasq-shared.d/mesh2tak-configura.conf").exists()
    assert aspetta(lambda: "collegato a 'Casa: sopra'" in impianto.registro()), impianto.registro()
    assert "segreta" not in impianto.registro()
    # resta collegato: il Wi-Fi del gateway non si riapre
    time.sleep(2)
    assert not impianto.aperto()


def test_password_sbagliata_riapre_e_lo_dice(impianto):
    impianto.avvia()
    assert aspetta(lambda: impianto.aperto() and impianto.chiedi()[0] == 200)
    g = gettone(impianto.chiedi()[1])
    impianto.chiedi("/collega", {"gettone": g, "rete": "Rifugio", "altra": "", "password": "sbagliata1"})
    assert aspetta(lambda: impianto.leggi()["cancellate"]), impianto.registro()
    assert aspetta(lambda: impianto.aperto() and impianto.chiedi()[0] == 200), impianto.registro()
    pagina = impianto.chiedi()[1]
    assert "Non sono riuscito a collegarmi a «Rifugio»: la password non e&#x27; giusta" in pagina
    assert not list((impianto.radice / "etc/NetworkManager/system-connections").glob("mesh2tak-wifi-*"))
    # rete scritta a mano che non c'e'
    impianto.chiedi("/collega", {"gettone": gettone(pagina), "rete": "", "altra": "Fantasma", "password": "qualcosa1"})
    assert aspetta(lambda: len(impianto.leggi()["cancellate"]) == 2)
    assert aspetta(lambda: impianto.aperto() and impianto.chiedi()[0] == 200)
    assert "la rete non si trova" in impianto.chiedi()[1]


def test_rete_solo_wpa3_e_rete_aperta(impianto):
    impianto.avvia()
    assert aspetta(lambda: impianto.aperto() and impianto.chiedi()[0] == 200)
    impianto.chiedi("/collega", {"gettone": gettone(impianto.chiedi()[1]), "rete": "Solo3", "altra": "",
                                 "password": "password3"})
    assert aspetta(lambda: impianto.leggi()["provate"] == [["Solo3", "password3", "sae"]]), impianto.registro()


def test_richieste_che_non_vanno(impianto):
    impianto.avvia()
    assert aspetta(lambda: impianto.aperto() and impianto.chiedi()[0] == 200)
    g = gettone(impianto.chiedi()[1])
    assert impianto.chiedi("/collega", {"gettone": "altro", "rete": "Rifugio", "password": "password1"})[0] == 403
    assert "da 8 a 63" in impianto.chiedi("/collega", {"gettone": g, "rete": "Rifugio", "password": "corta"})[1]
    assert "rete aziendale" in impianto.chiedi("/collega", {"gettone": g, "rete": "Azienda", "password": "password1"})[1]
    assert "scrivila" in impianto.chiedi("/collega", {"gettone": g, "rete": "Rifugio", "password": ""})[1]
    assert "nome della rete" in impianto.chiedi("/collega", {"gettone": g, "rete": "", "altra": "", "password": "password1"})[1]
    assert "caratteri" in impianto.chiedi("/collega", {"gettone": g, "rete": "Rifugio", "password": "pass\nword1"})[1]
    assert impianto.chiedi("/altro", {"gettone": g})[0] == 400
    time.sleep(0.5)
    assert impianto.leggi()["provate"] == [] and impianto.aperto()


def test_torna_il_cavo_e_il_wifi_del_gateway_si_chiude(impianto):
    impianto.avvia()
    assert aspetta(impianto.aperto)
    open(str(impianto.stato_nm) + ".cavo", "w").close()
    assert aspetta(lambda: not impianto.aperto()), impianto.registro()
    assert aspetta(lambda: "di nuovo in rete (cavo)" in impianto.registro()), impianto.registro()


def test_se_nessuno_lo_usa_riprova_le_reti_note(impianto):
    # il Raspberry conosce "Rifugio" (a portata) e "Lontana" (non a portata); per ora Rifugio non risponde
    impianto.scrivi(note=["Lontana", "Rifugio"])
    impianto.avvia()
    assert aspetta(impianto.aperto)
    assert aspetta(lambda: "la rete 'Rifugio' e' di nuovo a portata" in impianto.registro(), 30), impianto.registro()
    assert aspetta(impianto.aperto), "non riuscita: il Wi-Fi del gateway si riapre"
    # il router e' tornato (un file a parte: lo stato lo riscrive anche il nmcli finto, mentre gira)
    open(str(impianto.stato_nm) + ".torna", "w").close()
    assert aspetta(lambda: ["Rifugio", "802-11-wireless"] in impianto.leggi()["attivi"], 30), impianto.registro()
    assert aspetta(lambda: not impianto.aperto())
    d = impianto.leggi()
    assert not any(c[:2] == ["device", "connect"] for c in d["chiamate"])
    assert not any(c[-1] == "Lontana" and "up" in c for c in d["chiamate"]), "una rete non a portata non si prova"


def test_senza_scheda_wifi_o_senza_impostazioni_non_fa_niente(impianto, tmp_path):
    impianto.scrivi(wifi=False)
    impianto.avvia()
    assert aspetta(lambda: "il Raspberry non ha il Wi-Fi" in impianto.registro())
    impianto.ferma()
    impianto.scrivi(wifi=True)
    (impianto.radice / "etc/mesh2tak/wifi-configura.conf").write_text("nome=Gateway-Prova\npassword=corta\n")
    impianto.avvia()
    assert aspetta(lambda: "manca il nome o la password" in impianto.registro())
    assert not impianto.aperto()


def test_scheda_che_non_fa_da_punto_di_accesso(impianto):
    impianto.scrivi(ap_rotto=True)
    impianto.avvia()
    assert aspetta(lambda: "il Wi-Fi del gateway non si apre" in impianto.registro()), impianto.registro()
    assert aspetta(lambda: not (impianto.radice / "etc/NetworkManager/dnsmasq-shared.d/mesh2tak-configura.conf").exists())


def test_il_comando_mostra_nome_e_password(impianto):
    r = subprocess.run([sys.executable, "-I", PROGRAMMA], env=impianto.ambiente, capture_output=True, text=True)
    assert r.returncode == 0 and "Gateway-Prova" in r.stdout and "abcd-efgh-jk" in r.stdout
    assert "senza rete" in r.stdout
