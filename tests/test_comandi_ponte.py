"""1.13.0, "Gestisci il Raspberry": mesh2tak-aggiorna (root) fa i comandi che la pagina della rete mette nella
registrazione del ponte (riavvii, registro, impostazioni, certificato nuovo via SSH, radio come all'installazione) e ne
scrive l'esito per la pagina. Con la pagina della rete finta di test_aggiorna_ponte e programmi di sistema finti
(systemctl, journalctl, ssh, l'installatore) che si segnano come sono stati chiamati."""
import base64
import json
import os
import sys
import time

import pytest

from test_aggiorna_ponte import Raspberry, certificati, installatore  # noqa: F401 (fixture)
from test_gestione import gestione  # noqa: F401 (fixture)

QUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(QUI))
import aggiorna  # noqa: E402

CHIAVE_CANALE = base64.b64encode(bytes(range(32))).decode()        # 44 caratteri con '=' in fondo
CONF = """# Ponte Meshtastic <-> TAK Server. Dopo una modifica:  sudo systemctl restart mesh2tak
[tak]
host = takserver.esempio.it
porta = 8089

[meshtastic]
collegamento = tcp:127.0.0.1:4404
canali = privati

[mappa]
# minuti in cui una radio resta sulla mappa dopo l'ultimo segnale ricevuto
validita_min = 90
aggiornamento_min = 2
errore_massimo_m = 200
# colore squadra TAK delle radio Meshtastic
squadra = Cyan
includi_ponte = si

[radio]
invio = si   # dal TAK Server alle radio
tempo_radio_percento = 3

[gestione]
attiva = si
porta_pagina = {porta}
pausa_massima_min = 30
"""


def scrivi_eseguibile(percorso, testo):
    percorso.write_text(testo)
    os.chmod(percorso, 0o755)


class Pi(Raspberry):
    """Il Raspberry di test_aggiorna_ponte con la configurazione vera e i programmi di sistema finti che si segnano."""

    def __init__(self, tmp_path, certificati):
        super().__init__(tmp_path, certificati)
        accesso = (tmp_path / "mesh2tak.conf").read_text().split("[accesso_esterno]", 1)[1]
        (tmp_path / "mesh2tak.conf").write_text(CONF.format(porta=self.ponte.porta) + "\n[accesso_esterno]" + accesso)
        self.azioni = []
        self.chiamate = tmp_path / "chiamate"
        stub = tmp_path / "stub"
        scrivi_eseguibile(stub / "systemctl", '#!/bin/bash\necho "systemctl $*" >> "$PROVA_CHIAMATE"\n'
                                              '[ "$1 $2" = "restart meshtasticd" ] && [ -n "$PROVA_RADIO_ROTTA" ] && exit 1\n'
                                              'exit 0\n')
        scrivi_eseguibile(stub / "journalctl", '#!/bin/bash\necho "journalctl $*" >> "$PROVA_CHIAMATE"\n'
                          f'echo "Nov 11 mesh2tak[1]: INFO canale con psk={CHIAVE_CANALE} e password: segreta"\n'
                          'echo "link https://meshtastic.org/e/#CgcSAQE6AggNEg8IATgBQANIAVAeaAHABgE"\n'
                          'echo "-----BEGIN PRIVATE KEY-----"\necho "MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQg"\n'
                          'echo "-----END PRIVATE KEY-----"\n'
                          'echo "impronta 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"\n')
        self.server_ssh = tmp_path / "server-ssh"
        self.server_ssh.mkdir()
        scrivi_eseguibile(stub / "ssh", '#!/bin/bash\necho "ssh $*" >> "$PROVA_CHIAMATE"\n'
                          'cmd="${@: -1}"\ncase "$cmd" in\n'
                          '  certificato) [ -f "$PROVA_SERVER_SSH/certificato.json" ] || { echo "nessun certificato" >&2; exit 3; }\n'
                          '    cat "$PROVA_SERVER_SSH/certificato.json" ;;\n'
                          '  certificato-fatto) rm -f "$PROVA_SERVER_SSH/certificato.json" ;;\n'
                          '  *) echo "comando non previsto" >&2; exit 2 ;;\nesac\n')
        # l'installatore salvato sul Raspberry: si segna argomenti e ambiente
        scrivi_eseguibile(self.dir / "installatore.sh",
                          '#!/bin/bash\necho "installatore $* password=${MESH2TAK_PASSWORD:-} host=${MESH2TAK_HOST:-}'
                          ' da_aggiornamento=${MESH2TAK_DA_AGGIORNAMENTO:-}" >> "$PROVA_CHIAMATE"\n'
                          'case "$1" in\n  --reimposta-radio) echo "  ✔ radio del ponte come all\'installazione" ;;\n'
                          '  *) [ -f "$1" ] || { echo "✘ pacchetto mancante"; exit 1; }\n'
                          '     cp "$1" "$PROVA_SERVER_SSH/pacchetto-installato"\n'
                          '     [ -n "$PROVA_PASSWORD_GIUSTA" ] && [ "$MESH2TAK_PASSWORD" != "$PROVA_PASSWORD_GIUSTA" ] && '
                          '{ echo "✘ Password del certificato sbagliata (quella mostrata nella console infra-TAK)."; exit 1; }\n'
                          '     echo "  ✔ certificato \'ponte-casa\' (scade: Nov 11 12:00:00 2027 GMT)"\n'
                          '     [ -n "$PROVA_TAK_RIFIUTA" ] && echo "  ! Il TAK Server non ha accettato il collegamento: il ponte riprova da solo" ;;\n'
                          'esac\nexit 0\n')
        (self.dir / "venv" / "bin").mkdir(parents=True)
        scrivi_eseguibile(self.dir / "venv" / "bin" / "python",
                          '#!/bin/bash\necho "python $*" >> "$PROVA_CHIAMATE"\necho "ESITO riavvio=ok"\n')
        self.avvio = tmp_path / "boot_id"
        self.avvio.write_text("avvio-1\n")
        (tmp_path / "installazione.log").write_text("=== installazione\n  ✔ chiave del canale " + CHIAVE_CANALE + "\n")
        self.amb.update(PROVA_CHIAMATE=str(self.chiamate), PROVA_SERVER_SSH=str(self.server_ssh),
                        MESH2TAK_PROVA_SSH=str(stub / "ssh"), MESH2TAK_AGGIORNA_AVVIO=str(self.avvio),
                        MESH2TAK_AGGIORNA_LOG_INST=str(tmp_path / "installazione.log"),
                        MESH2TAK_AGGIORNA_ATTESA_RADIO="0")

    def registrazione(self):
        d = super().registrazione()
        if self.azioni:
            d["azioni"] = self.azioni
        return d

    def comanda(self, azione, ident="0000000000000001", chiesto=None, **altro):
        self.azioni.append(dict({"id": ident, "azione": azione, "chiesto": int(time.time()) if chiesto is None else chiesto},
                                **altro))

    def esiti(self):
        f = self.stato / "azioni.json"
        return {a["id"]: a for a in json.loads(f.read_text())["azioni"]} if f.exists() else {}

    def fatte(self):
        return self.chiamate.read_text().splitlines() if self.chiamate.exists() else []

    def conf(self):
        return (self.t / "mesh2tak.conf").read_text()


@pytest.fixture
def pi(tmp_path, certificati):
    r = Pi(tmp_path, certificati)
    yield r
    r.ferma()


def test_riavvia_il_programma_una_volta_sola(pi):
    pi.comanda("riavvia-programma")
    r = pi.lancia()
    assert r.returncode == 0, r.stdout + r.stderr
    e = pi.esiti()["0000000000000001"]
    assert (e["stato"], e["messaggio"], e["azione"]) == ("fatto", "programma del ponte riavviato", "riavvia-programma")
    assert "systemctl restart mesh2tak" in pi.fatte()
    assert oct((pi.stato / "azioni.json").stat().st_mode)[-3:] == "644", "lo legge il ponte, per la pagina"
    pi.lancia()
    assert pi.fatte().count("systemctl restart mesh2tak") == 1, "un comando si fa una volta sola"


def test_comando_vecchio_scade_e_orologio_indietro_aspetta(pi):
    pi.comanda("riavvia-programma", chiesto=int(time.time()) - 3600)
    pi.comanda("riavvia-radio", ident="0000000000000002", chiesto=int(time.time()) + 3600)
    pi.lancia()
    e = pi.esiti()
    assert e["0000000000000001"]["stato"] == "scaduto" and "30 minuti" in e["0000000000000001"]["messaggio"]
    assert "0000000000000002" not in e, "con l'ora del futuro aspetta che l'orologio del Raspberry si regoli"
    assert pi.fatte() == []
    assert "orologio" in (pi.t / "aggiornamento.log").read_text()


def test_comandi_strani_ignorati(pi):
    pi.azioni = [{"id": "x", "azione": "riavvia-programma", "chiesto": int(time.time())},
                 {"id": "0000000000000003", "azione": "rm -rf /", "chiesto": int(time.time())},
                 {"id": "0000000000000004", "azione": "riavvia-programma", "chiesto": True}, "boh"]
    assert pi.lancia().returncode == 0
    assert pi.esiti() == {} and pi.fatte() == []


def test_riavvia_la_radio(pi):
    pi.comanda("riavvia-radio")
    pi.lancia()
    assert pi.esiti()["0000000000000001"]["stato"] == "fatto" and "systemctl restart meshtasticd" in pi.fatte()
    # radio USB: il ponte si ferma, il comando di riavvio va alla radio, il ponte riparte
    (pi.t / "mesh2tak.conf").write_text(pi.conf().replace("tcp:127.0.0.1:4404", "serial:/dev/serial/by-id/usb-radio"))
    pi.comanda("riavvia-radio", ident="0000000000000002")
    pi.lancia()
    e = pi.esiti()["0000000000000002"]
    assert e["stato"] == "fatto" and "USB" in e["messaggio"]
    fatte = pi.fatte()
    i = fatte.index("systemctl stop mesh2tak")
    assert fatte[i + 1].endswith("mesh2tak.py --riavvia-nodo serial:/dev/serial/by-id/usb-radio")
    assert fatte[i + 2] == "systemctl start mesh2tak"


def test_riavvia_la_radio_che_non_riparte(pi):
    pi.amb["PROVA_RADIO_ROTTA"] = "1"
    pi.comanda("riavvia-radio")
    pi.lancia()
    e = pi.esiti()["0000000000000001"]
    assert e["stato"] == "errore" and "meshtasticd non si riavvia" in e["messaggio"]


def test_riavvia_il_raspberry_per_ultimo(pi):
    pi.pubblica("1.9.0", installatore("1.9.0"))
    pi.chiedi("1.9.0")                                   # anche un aggiornamento: dopo il riavvio
    pi.comanda("riavvia-raspberry")
    pi.comanda("registro", ident="0000000000000002")
    pi.comanda("riavvia-raspberry", ident="0000000000000003")
    assert pi.lancia().returncode == 0
    e = pi.esiti()
    assert e["0000000000000002"]["stato"] == "fatto", "prima gli altri comandi"
    assert [e[i]["stato"] for i in ("0000000000000001", "0000000000000003")] == ["in_corso", "in_corso"]
    assert pi.fatte().count("systemctl reboot") == 1 and pi.github.richieste == [], "un riavvio solo, niente aggiornamento"
    pi.lancia()                                          # stesso avvio: il riavvio non e' ancora successo
    assert pi.fatte().count("systemctl reboot") == 1 and pi.esiti()["0000000000000001"]["stato"] == "in_corso"
    assert pi.github.richieste == [] and pi.installata() == "1.8.0", "niente mentre il Raspberry sta per riavviarsi"
    pi.avvio.write_text("avvio-2\n")                     # riacceso
    pi.lancia()
    e = pi.esiti()
    assert e["0000000000000001"]["stato"] == "fatto" and e["0000000000000001"]["messaggio"] == "Raspberry riavviato"
    assert e["0000000000000003"]["stato"] == "fatto" and pi.fatte().count("systemctl reboot") == 1
    assert pi.installata() == "1.9.0", "l'aggiornamento al giro dopo"


def test_comando_interrotto_a_meta(pi):
    pi.stato.mkdir()
    (pi.stato / "azioni.json").write_text(json.dumps({"formato": 1, "visti": ["0000000000000009"], "azioni": [
        {"id": "0000000000000009", "azione": "impostazioni", "chiesto": 1, "stato": "in_corso", "messaggio": "...",
         "quando": 1}]}))
    pi.lancia()
    e = pi.esiti()["0000000000000009"]
    assert e["stato"] == "errore" and "interrotto a meta'" in e["messaggio"]


def test_registro_senza_chiavi_ne_password(pi):
    pi.comanda("registro")
    pi.lancia()
    e = pi.esiti()["0000000000000001"]
    assert e["stato"] == "fatto" and e["messaggio"].startswith("registro pronto")
    f = pi.stato / "registro.txt"
    testo = f.read_text()
    assert oct(f.stat().st_mode)[-3:] == "640", "lo legge solo il ponte (gruppo mesh2tak)"
    assert "===== Programma del ponte (mesh2tak)" in testo and "===== Installazione" in testo
    assert CHIAVE_CANALE not in testo and "segreta" not in testo and "MIGHAgEAMBMG" not in testo
    assert "CgcSAQE6" not in testo and "https://meshtastic.org/e/[canale tolto]" in testo
    assert "psk=[tolto]" in testo and "[chiave privata tolta]" in testo
    assert "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" in testo, "le impronte restano"
    assert any(c.startswith("journalctl -u mesh2tak") for c in pi.fatte())


def test_impostazioni_cambiate_e_commenti_tenuti(pi):
    pi.comanda("impostazioni", valori={"squadra": "Dark Blue", "validita_min": 120, "includi_ponte": False,
                                       "tempo_radio_percento": 2.5, "invio": False, "pausa_massima_min": 30})
    pi.lancia()
    e = pi.esiti()["0000000000000001"]
    assert e["stato"] == "fatto", e
    assert e["messaggio"] == ("impostazioni salvate: squadra = Dark Blue, validita_min = 120, includi_ponte = no, "
                              "tempo_radio_percento = 2.5, invio = no")
    conf = pi.conf()
    assert "squadra = Dark Blue\n" in conf and "validita_min = 120\n" in conf and "includi_ponte = no\n" in conf
    assert "invio = no   # dal TAK Server alle radio\n" in conf, "il commento in fondo alla riga resta"
    assert "# colore squadra TAK delle radio Meshtastic\n" in conf and "pausa_massima_min = 30\n" in conf
    assert (pi.t / "mesh2tak.conf.prima-della-pagina").read_text() == CONF.format(porta=pi.ponte.porta) + \
        "\n[accesso_esterno]" + conf.split("[accesso_esterno]", 1)[1]
    assert "systemctl restart mesh2tak" in pi.fatte()


def test_impostazioni_non_valide_non_toccano_niente(pi):
    prima = pi.conf()
    pi.comanda("impostazioni", valori={"squadra": "Rosa"})
    pi.comanda("impostazioni", ident="0000000000000002", valori={"tempo_radio_percento": 25})
    pi.comanda("impostazioni", ident="0000000000000003", valori={"collegamento": "tcp:1.2.3.4:4403"})
    pi.comanda("impostazioni", ident="0000000000000004", valori={"validita_min": True})
    pi.lancia()
    e = pi.esiti()
    assert [e[f"000000000000000{i}"]["stato"] for i in range(1, 5)] == ["errore"] * 4
    assert "impostazione sconosciuta: collegamento" in e["0000000000000003"]["messaggio"]
    assert pi.conf() == prima and pi.fatte() == []


def test_impostazioni_rimesse_se_il_ponte_non_riparte(pi):
    prima = pi.conf()
    pi.rotte = {"1.8.0"}                                 # la pagina del ponte non risponde piu'
    pi.comanda("impostazioni", valori={"squadra": "Red"})
    pi.lancia()
    e = pi.esiti()["0000000000000001"]
    assert e["stato"] == "errore" and "rimesse quelle di prima" in e["messaggio"]
    assert pi.conf() == prima and pi.fatte().count("systemctl restart mesh2tak") == 2


def certificato(pi, password="segreta", host="", nome="ponte-casa.zip"):
    (pi.server_ssh / "certificato.json").write_text(json.dumps({
        "formato": 1, "pacchetto": base64.b64encode(b"PK\x03\x04 pacchetto").decode(), "nome_pacchetto": nome,
        "password": password, "host": host}))


def test_certificato_nuovo_dal_server(pi):
    certificato(pi)
    pi.comanda("certificato")
    pi.lancia()
    e = pi.esiti()["0000000000000001"]
    assert e["stato"] == "fatto" and e["messaggio"] == ("certificato sostituito: 'ponte-casa', scade "
                                                        "Nov 11 12:00:00 2027 GMT"), e
    fatte = pi.fatte()
    ssh = [c for c in fatte if c.startswith("ssh ")]
    assert len(ssh) == 2 and ssh[0].endswith("-p 22 ponte-casa@meshtastic.esempio.it certificato")
    assert "StrictHostKeyChecking=yes" in ssh[0] and "BatchMode=yes" in ssh[0] and ssh[1].endswith(" certificato-fatto")
    inst = [c for c in fatte if c.startswith("installatore ")]
    assert inst == [f"installatore {pi.stato}/certificato/ponte-casa.zip password=segreta host=takserver.esempio.it "
                    "da_aggiornamento=1"], "senza indirizzo nel pacchetto: quello di adesso"
    assert (pi.server_ssh / "pacchetto-installato").read_bytes() == b"PK\x03\x04 pacchetto"
    assert not (pi.server_ssh / "certificato.json").exists(), "tolto dal server dopo l'installazione"
    assert not (pi.stato / "certificato").exists(), "e anche dal Raspberry"
    assert (pi.stato / "server-rete.known_hosts").read_text() == "mesh2tak-rete ssh-ed25519 AAAA\n"
    assert "segreta" not in (pi.t / "aggiornamento.log").read_text()


def test_certificato_password_sbagliata_e_tak_che_rifiuta(pi):
    certificato(pi, password="sbagliata", host="tak.altro.it")
    pi.amb["PROVA_PASSWORD_GIUSTA"] = "giusta"
    pi.comanda("certificato")
    pi.lancia()
    e = pi.esiti()["0000000000000001"]
    assert e["stato"] == "errore" and e["messaggio"] == ("certificato non installato: Password del certificato sbagliata "
                                                         "(quella mostrata nella console infra-TAK).")
    assert (pi.server_ssh / "certificato.json").exists(), "resta sul server: si puo' riprovare"
    assert any("host=tak.altro.it" in c for c in pi.fatte())
    pi.amb.update(PROVA_PASSWORD_GIUSTA="sbagliata", PROVA_TAK_RIFIUTA="1")
    pi.comanda("certificato", ident="0000000000000002")
    pi.lancia()
    e = pi.esiti()["0000000000000002"]
    assert e["stato"] == "errore" and "il TAK Server non accetta il collegamento" in e["messaggio"]


def test_certificato_che_non_c_e(pi):
    pi.comanda("certificato")
    pi.lancia()
    e = pi.esiti()["0000000000000001"]
    assert e["stato"] == "errore" and "non c'e' un certificato da installare" in e["messaggio"]
    assert not any(c.startswith("installatore") for c in pi.fatte())


@pytest.mark.parametrize("contenuto", ['{"pacchetto": "non base64!", "nome_pacchetto": "a.zip"}',
                                       '{"pacchetto": "UEs=", "nome_pacchetto": "../../etc/passwd"}', "non json"])
def test_certificato_strano_dal_server(pi, contenuto):
    (pi.server_ssh / "certificato.json").write_text(contenuto)
    pi.comanda("certificato")
    pi.lancia()
    e = pi.esiti()["0000000000000001"]
    assert e["stato"] == "errore" and e["messaggio"] == "certificato ricevuto dal server non valido"
    assert not any(c.startswith("installatore") for c in pi.fatte())


def test_reimposta_la_radio(pi):
    pi.comanda("reimposta-radio")
    pi.lancia()
    e = pi.esiti()["0000000000000001"]
    assert e["stato"] == "fatto" and "come all'installazione" in e["messaggio"]
    assert [c for c in pi.fatte() if c.startswith("installatore")] == [
        "installatore --reimposta-radio password= host= da_aggiornamento=1"]


def test_aggiornamento_con_la_radio_da_reimpostare(pi):
    script = installatore("1.9.0").replace(b'echo "installo 1.9.0"', b'echo "installo 1.9.0 radio=${MESH2TAK_REIMPOSTA_RADIO:-}"')
    pi.pubblica("1.9.0", script)
    pi.richiesta = {"versione": "1.9.0", "chiesto": 1000, "reimposta": True}
    assert pi.lancia().returncode == 0
    assert pi.segni() == ["installo 1.9.0 radio=1"]
    assert pi.esito()["messaggio"] == "aggiornato dalla 1.8.0 alla 1.9.0 e radio reimpostata"


def test_cambia_impostazioni_aggiunge_quelle_che_mancano():
    testo, cambiate = aggiorna.cambia_impostazioni("[mappa]\nsquadra = Cyan\n\n[radio]\ninvio = si", {
        "validita_min": 30, "pausa_massima_min": 45.5, "squadra": "Cyan", "tempo_radio_percento": 3.0})
    assert testo == ("[mappa]\nsquadra = Cyan\nvalidita_min = 30\n\n[radio]\ninvio = si\ntempo_radio_percento = 3\n"
                     "\n[gestione]\npausa_massima_min = 45.5\n")
    assert cambiate == ["validita_min = 30", "pausa_massima_min = 45.5", "tempo_radio_percento = 3"]
    assert aggiorna.testo_valore(0.5) == "0.5" and aggiorna.testo_valore(3.001) == "3" and aggiorna.testo_valore(True) == "si"


# ---- lato ponte (utente mesh2tak): l'avviso dal tunnel e il registro per la pagina ----

def test_l_avviso_dal_tunnel_non_si_perde(gestione, tmp_path):
    """Due comandi a pochi secondi uno dall'altro (es. registro e subito dopo un riavvio): il file che sveglia
    mesh2tak-aggiorna si tocca al massimo ogni 5 secondi, ma l'avviso arrivato prima non si perde: si da' dopo."""
    from test_icone import Finto
    g, _, _ = gestione(accesso_cartella_stato=str(tmp_path))
    f = tmp_path / "controlla-adesso"

    def avvisa(da="127.0.0.1", **intestazioni):
        h = Finto("POST", b"{}", da=da, **intestazioni)
        g._servi_controlla(h)
        return h.codice
    assert avvisa(**{"X-Mesh2tak": "1"}) == 200 and f.exists(), "subito"
    f.unlink()
    inizio = time.time()
    assert avvisa(**{"X-Mesh2tak": "1"}) == 200 and avvisa(**{"X-Mesh2tak": "1"}) == 200
    assert not f.exists(), "non subito: l'unita' .path di systemd non va tempestata"
    fine = time.time() + 10
    while not f.exists() and time.time() < fine:
        time.sleep(0.1)
    assert f.exists() and 3 < time.time() - inizio < 8, "dato allo scadere dei 5 secondi"
    f.unlink()
    time.sleep(1)
    assert not f.exists(), "i due avvisi ravvicinati: uno solo rimandato"
    assert avvisa() == 403, "senza l'intestazione della rete"
    assert avvisa(da="192.168.1.50", **{"X-Mesh2tak": "1"}) == 403, "dal Wi-Fi del posto, non dal tunnel"
    assert not f.exists()


def test_il_registro_solo_per_gli_amministratori_dal_tunnel(gestione, tmp_path):
    from test_icone import Finto
    g, _, _ = gestione()
    g.file_registro = str(tmp_path / "registro.txt")

    def leggi(da="127.0.0.1", metodo="GET", **intestazioni):
        h = Finto(metodo, da=da, **intestazioni)
        g._servi_registro(h)
        return h.codice, h.wfile.getvalue()
    assert leggi(**{"X-Authentik-Username": "andrea"})[0] == 404, "registro non ancora raccolto"
    (tmp_path / "registro.txt").write_text("Registro del ponte\n===== Servizi\n")
    codice, corpo = leggi(**{"X-Authentik-Username": "andrea"})
    assert codice == 200 and b"Registro del ponte" in corpo
    assert leggi()[0] == 403, "senza il nome messo da Authentik"
    assert leggi(da="192.168.1.50", **{"X-Authentik-Username": "andrea"})[0] == 403, "non dal tunnel"
    assert leggi(metodo="POST", **{"X-Authentik-Username": "andrea"})[0] == 405
