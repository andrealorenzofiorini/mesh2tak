"""Avvisi per email della rete Meshtastic (1.7.0): il servizio della pagina chiede ogni minuto lo stato dei ponti e
avvisa con la posta di infra-TAK (destinatari di Guard Dog) quando un problema dura da 10 minuti e quando passa,
quando il certificato sta per scadere, quando la corrente non basta e quando un Raspberry si riavvia. Qui con
orologio finto, ponti finti e posta finta; poi l'invio vero a una console finta e la lettura vera da un ponte finto."""
import datetime
import http.server
import json
import os
import ssl
import sys
import threading

import pytest

from conftest import RADICE, porta_libera

sys.path.insert(0, os.path.join(RADICE, "server"))
import rete_meshtastic as rete  # noqa: E402

GIORNO = 86400
T0 = datetime.datetime(2026, 11, 6, 14, 0, tzinfo=rete.FUSO).timestamp()   # 14:00 in Italia
AVVIO1 = "1b4e28ba-2fa1-11d2-883f-0016d3cca427"
AVVIO2 = "6f1d0b77-0000-4000-8000-0016d3cca427"


class Orologio:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class Posta:
    def __init__(self):
        self.mandate = []
        self.esito = ("mandato", "")
        self.tentativi = 0

    def __call__(self, oggetto, testo, cfg):
        self.tentativi += 1
        if self.esito[0] == "mandato":
            self.mandate.append((oggetto, testo))
        return self.esito


def stato(ora, avvio=AVVIO1, acceso=None, tak=True, nodo=True, pausa=False, scade=None, libero=12000,
          bassa=None, volte=0, errore=None):
    s = {"formato": 1, "ora": ora, "nodo": {"collegato": nodo}, "tak": {"collegato": tak, "errore": errore},
         "pausa": {"attiva": pausa},
         "sistema": {"avvio": avvio, "acceso_dal": acceso if acceso is not None else T0 - 3600,
                     "disco": {"libero_mb": libero, "totale_mb": 29000},
                     "alimentazione": {"bassa_ora": False, "ultima": bassa, "volte": volte, "dal": T0 - 3600}}}
    if scade is not None:
        s["certificato"] = {"scade": scade, "nome": "meshtastic-cnsas"}
    return s


class Rete:
    """Ponti finti: per ogni porta lo stato da dare (None = non risponde)."""

    def __init__(self, tmp_path, **ponti):
        self.orologio = Orologio()
        self.posta = Posta()
        self.stati = {}
        self.porte = {}
        for i, (nome, st) in enumerate(ponti.items()):
            self.porte[nome] = 19443 + i
            self.stati[19443 + i] = st
        self.cfg = {"formato": 1, "pagina": "https://meshtastic.esempio.it/", "console": 5001,
                    "ponti": [{"nome": n, "porta": p} for n, p in self.porte.items()]}
        self.percorso = str(tmp_path / "avvisi.json")
        self.s = self.nuova()

    def nuova(self):
        return rete.Sorveglianza(ora=self.orologio, prendi=lambda porta: self.stati.get(porta), manda=self.posta,
                                 percorso=self.percorso, config=lambda: self.cfg)

    def metti(self, nome, st):
        self.stati[self.porte[nome]] = st

    def minuti(self, n):
        """n giri, uno al minuto (lo stato dei ponti resta quello impostato)."""
        for _ in range(n):
            self.s.giro()
            self.orologio.t += 60

    def oggetti(self):
        return [o for o, _ in self.posta.mandate]


def test_breve_interruzione_nessuna_email(tmp_path):
    r = Rete(tmp_path, casa=stato(T0))
    r.minuti(3)
    r.metti("casa", None)
    r.minuti(8)
    r.metti("casa", stato(T0))
    r.minuti(3)
    assert r.posta.mandate == []


def test_non_risponde_e_torna(tmp_path):
    r = Rete(tmp_path, casa=stato(T0))
    r.minuti(1)
    r.metti("casa", None)                       # dalle 14:01
    r.minuti(10)
    assert r.posta.mandate == []
    r.minuti(1)                                 # 10 minuti pieni
    assert r.oggetti() == ["[Rete Meshtastic] Ponte casa: non risponde"]
    testo = r.posta.mandate[0][1]
    assert "non risponde dalle 14:01 (da 10 minuti)" in testo and "Cosa fare:" in testo
    assert "Pagina della rete: https://meshtastic.esempio.it/" in testo and "Guard Dog" in testo
    r.minuti(20)
    assert len(r.posta.mandate) == 1            # niente ripetizioni
    r.metti("casa", stato(r.orologio.t))
    r.minuti(1)
    assert r.oggetti()[1] == "[Rete Meshtastic] Ponte casa: di nuovo in linea"
    assert "dalle 14:01, per 31 minuti" in r.posta.mandate[1][1] and "riavviato" not in r.posta.mandate[1][1]
    r.minuti(5)
    assert len(r.posta.mandate) == 2


def test_riavvio_durante_l_interruzione_detto_nella_stessa_email(tmp_path):
    r = Rete(tmp_path, casa=stato(T0))
    r.minuti(1)
    r.metti("casa", None)
    r.minuti(15)
    r.metti("casa", stato(r.orologio.t, avvio=AVVIO2, acceso=T0 + 14 * 60))
    r.minuti(2)
    assert r.oggetti() == ["[Rete Meshtastic] Ponte casa: non risponde", "[Rete Meshtastic] Ponte casa: di nuovo in linea"]
    assert "Il Raspberry si e' riavviato alle 14:14." in r.posta.mandate[1][1]


def test_riavvio_breve_avvisato_da_solo(tmp_path):
    """Il watchdog lo riavvia in un paio di minuti: niente "non risponde", ma si sa che si e' riavviato."""
    r = Rete(tmp_path, casa=stato(T0))
    r.minuti(2)
    r.metti("casa", None)
    r.minuti(2)
    r.metti("casa", stato(r.orologio.t, avvio=AVVIO2, acceso=T0 + 3 * 60))
    r.minuti(2)
    assert r.oggetti() == ["[Rete Meshtastic] Ponte casa: il Raspberry si e' riavviato"]
    assert "alle 14:03" in r.posta.mandate[0][1] and "ripartito da solo" in r.posta.mandate[0][1]


def test_radio_staccata_ma_non_durante_la_gestione(tmp_path):
    r = Rete(tmp_path, casa=stato(T0, nodo=False, pausa=True))
    r.minuti(25)
    assert r.posta.mandate == []
    r.metti("casa", stato(T0, nodo=False))
    r.minuti(11)
    assert r.oggetti() == ["[Rete Meshtastic] Ponte casa: radio non collegata"]
    r.metti("casa", stato(T0))
    r.minuti(1)
    assert r.oggetti()[-1] == "[Rete Meshtastic] Ponte casa: radio di nuovo collegata"


def test_tak_non_collegato_con_l_errore(tmp_path):
    r = Rete(tmp_path, casa=stato(T0, tak=False, errore="certificato\n rifiutato dal server"))
    r.minuti(11)
    assert r.oggetti() == ["[Rete Meshtastic] Ponte casa: non arriva al TAK Server"]
    assert "(ultimo errore: certificato rifiutato dal server)" in r.posta.mandate[0][1]


def test_non_risponde_non_porta_anche_il_tak(tmp_path):
    """Senza internet cadono tunnel e TAK insieme: si avvisa solo che non risponde."""
    r = Rete(tmp_path, casa=stato(T0, tak=False))
    r.minuti(3)
    r.metti("casa", None)
    r.minuti(15)
    assert r.oggetti() == ["[Rete Meshtastic] Ponte casa: non risponde"]


def test_certificato_avvisato_alle_soglie(tmp_path):
    scade = T0 + 40 * GIORNO
    r = Rete(tmp_path, casa=stato(T0, scade=scade))
    r.minuti(2)
    assert r.posta.mandate == []
    r.orologio.t = scade - 29 * GIORNO
    r.minuti(2)
    assert r.oggetti() == ["[Rete Meshtastic] Ponte casa: il certificato TAK scade il 16/12/2026"]
    testo = r.posta.mandate[0][1]
    assert "tra 29 giorni" in testo and "meshtastic-cnsas" in testo and "installa-ponte-meshtastic.sh NOMEFILE.zip" in testo
    r.orologio.t = scade - 20 * GIORNO
    r.minuti(2)
    assert len(r.posta.mandate) == 1
    r.orologio.t = scade - 6 * GIORNO
    r.minuti(1)
    r.orologio.t = scade + 3600
    r.minuti(1)
    assert len(r.posta.mandate) == 3 and r.oggetti()[2] == "[Rete Meshtastic] Ponte casa: il certificato TAK e' scaduto"
    r.metti("casa", stato(T0, scade=scade + 700 * GIORNO))     # rinnovato
    r.minuti(3)
    assert len(r.posta.mandate) == 3


def test_corrente_insufficiente_al_massimo_una_volta_al_giorno(tmp_path):
    r = Rete(tmp_path, casa=stato(T0, bassa=T0 - 60, volte=3))
    r.minuti(1)
    assert r.oggetti() == ["[Rete Meshtastic] Ponte casa: corrente insufficiente"]
    assert "l'ultimo alle 13:59; 3 da quando il ponte e' acceso" in r.posta.mandate[0][1]
    assert "5,1 V e 2,5 A" in r.posta.mandate[0][1]
    r.orologio.t += 2 * 3600
    r.metti("casa", stato(T0, bassa=r.orologio.t - 30, volte=5))
    r.minuti(1)
    assert len(r.posta.mandate) == 1
    r.orologio.t += 23 * 3600
    r.metti("casa", stato(T0, bassa=r.orologio.t - 30, volte=9))
    r.minuti(1)
    assert len(r.posta.mandate) == 2
    r.orologio.t += 30 * 3600                   # niente cali recenti: niente email
    r.metti("casa", stato(T0, bassa=r.orologio.t - 2 * 3600, volte=9))
    r.minuti(1)
    assert len(r.posta.mandate) == 2


def test_microsd_quasi_piena(tmp_path):
    r = Rete(tmp_path, casa=stato(T0, libero=300))
    r.minuti(11)
    assert r.oggetti() == ["[Rete Meshtastic] Ponte casa: microSD quasi piena"]
    assert "restano 300 MB liberi" in r.posta.mandate[0][1]
    r.metti("casa", stato(T0, libero=4000))
    r.minuti(1)
    assert r.oggetti()[1] == "[Rete Meshtastic] Ponte casa: spazio di nuovo sufficiente"


def test_piu_ponti_una_email_sola(tmp_path):
    r = Rete(tmp_path, casa=stato(T0), sede=stato(T0))
    r.minuti(1)
    r.metti("casa", None)
    r.metti("sede", None)
    r.minuti(11)
    assert r.oggetti() == ["[Rete Meshtastic] 2 avvisi: Ponte casa: non risponde e altri"]
    testo = r.posta.mandate[0][1]
    assert "Ponte casa: non risponde" in testo and "Ponte sede: non risponde" in testo


def test_email_non_partita_si_riprova(tmp_path):
    r = Rete(tmp_path, casa=stato(T0))
    r.posta.esito = ("errore", "la console di infra-TAK non risponde (porta 5001)")
    r.metti("casa", None)
    r.minuti(11)
    assert r.posta.tentativi == 1 and r.posta.mandate == []
    assert r.s.vista()["ultimo"]["esito"] == "errore" and r.s.vista()["in_coda"] == 1
    r.minuti(4)
    assert r.posta.tentativi == 1               # riprova dopo 5 minuti
    r.posta.esito = ("mandato", "")
    r.minuti(2)
    assert r.posta.tentativi == 2 and r.oggetti() == ["[Rete Meshtastic] Ponte casa: non risponde"]
    assert r.s.vista()["in_coda"] == 0
    r.minuti(10)
    assert r.posta.tentativi == 2


def test_nessun_destinatario_riprova_ogni_mezz_ora(tmp_path):
    r = Rete(tmp_path, casa=stato(T0))
    r.posta.esito = ("nessuno", "in infra-TAK non c'e' un indirizzo per gli avvisi (Guard Dog)")
    r.metti("casa", None)
    r.minuti(11)
    r.minuti(20)
    assert r.posta.tentativi == 1
    r.minuti(11)
    assert r.posta.tentativi == 2


def test_risolto_prima_di_partire_non_si_manda_niente(tmp_path):
    r = Rete(tmp_path, casa=stato(T0))
    r.posta.esito = ("trattenuto", "infra-TAK e' in una finestra di manutenzione: riprovo dopo")
    r.metti("casa", None)
    r.minuti(11)
    r.metti("casa", stato(T0))
    r.minuti(1)
    assert r.s.vista()["in_coda"] == 0
    r.posta.esito = ("mandato", "")
    r.minuti(10)
    assert r.posta.mandate == []


def test_dopo_un_riavvio_del_servizio_niente_doppioni(tmp_path):
    r = Rete(tmp_path, casa=stato(T0))
    r.minuti(1)
    r.metti("casa", None)
    r.minuti(12)
    assert len(r.posta.mandate) == 1
    r.s = r.nuova()                             # il servizio riparte: legge lo stato dal file
    r.minuti(5)
    assert len(r.posta.mandate) == 1
    r.metti("casa", stato(r.orologio.t))
    r.minuti(1)
    assert r.oggetti()[-1] == "[Rete Meshtastic] Ponte casa: di nuovo in linea"


def test_ponte_tolto_niente_piu_avvisi(tmp_path):
    r = Rete(tmp_path, casa=stato(T0))
    r.posta.esito = ("errore", "x")
    r.metti("casa", None)
    r.minuti(11)
    r.cfg["ponti"] = []
    r.posta.esito = ("mandato", "")
    r.minuti(10)
    assert r.posta.mandate == [] and r.s.vista()["problemi"] == {} and r.s.vista()["in_coda"] == 0


def test_ponte_vecchio_senza_i_dati_nuovi(tmp_path):
    """Un ponte 1.6.0: niente sistema ne' certificato, gli avvisi sullo stato funzionano lo stesso."""
    vecchio = {"formato": 1, "nodo": {"collegato": True}, "tak": {"collegato": False}, "pausa": {"attiva": False}}
    r = Rete(tmp_path, casa=vecchio)
    r.minuti(11)
    assert r.oggetti() == ["[Rete Meshtastic] Ponte casa: non arriva al TAK Server"]


def test_file_di_stato_rovinato(tmp_path):
    (tmp_path / "avvisi.json").write_text('{"ponti": [1, 2], "coda": "x", "ultimo": 5}')
    r = Rete(tmp_path, casa=stato(T0))
    r.metti("casa", None)
    r.minuti(11)
    assert r.oggetti() == ["[Rete Meshtastic] Ponte casa: non risponde"]


def test_parole_delle_ore():
    assert rete.durata_it(60) == "1 minuto" and rete.durata_it(25 * 60) == "25 minuti"
    assert rete.durata_it(61 * 60) == "1 ora e 1 minuto" and rete.durata_it(3 * 3600) == "3 ore"
    assert rete.durata_it(3 * GIORNO) == "3 giorni"
    assert rete.quando_it(T0 + 125 * 60, T0 + 3 * 3600) == "16:05"
    assert rete.quando_it(T0 - GIORNO, T0) == "5/11 alle 14:00"
    assert rete.giorno_it(T0) == "6/11/2026"


# ---------------------------------------------------------------- invio vero a una console di infra-TAK finta

class Console:
    def __init__(self, risposta, codice=200, tls=None):
        self.ricevute = []
        console = self

        class G(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):  # noqa: N802
                corpo = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/api/guarddog/send-alert-email":
                    console.ricevute.append((self.headers.get("Content-Type"), json.loads(corpo)))
                dati = json.dumps(risposta).encode()
                self.send_response(codice)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(dati)))
                self.end_headers()
                self.wfile.write(dati)

        self.porta = porta_libera()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", self.porta), G)
        if tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(*tls)
            self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def ferma(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.mark.parametrize("risposta,codice,atteso", [
    ({"success": True, "suppressed": False}, 200, "mandato"),
    ({"success": True, "suppressed": True, "reason": "no recipient configured"}, 200, "nessuno"),
    ({"success": True, "suppressed": True}, 200, "pausa"),
    ({"success": True, "suppressed": True, "held": True, "reason": "quiet window"}, 200, "trattenuto"),
    ({"error": "SMTP down"}, 500, "errore"),
])
def test_invio_alla_console_in_http(risposta, codice, atteso):
    c = Console(risposta, codice)
    try:
        esito, dettaglio = rete.Sorveglianza.invia(c.porta, "[Rete Meshtastic] Prova", "testo\ndella prova")
    finally:
        c.ferma()
    assert esito == atteso, dettaglio
    assert len(c.ricevute) == 1                 # una richiesta sola: mai email doppie
    tipo, corpo = c.ricevute[0]
    assert tipo == "application/json" and corpo == {"subject": "[Rete Meshtastic] Prova", "body": "testo\ndella prova",
                                                    "to": ""}


def test_invio_alla_console_in_https(pki):
    c = Console({"success": True, "suppressed": False}, tls=(str(pki / "takserver-chain.pem"), str(pki / "takserver.key")))
    try:
        assert rete.Sorveglianza.invia(c.porta, "o", "t") == ("mandato", "")
    finally:
        c.ferma()
    assert len(c.ricevute) == 1


def test_console_spenta():
    esito, dettaglio = rete.Sorveglianza.invia(porta_libera(), "o", "t")
    assert esito == "errore" and "non risponde" in dettaglio


# ---------------------------------------------------------------- lettura vera dello stato di un ponte finto

def ponte_finto(pki, corpo):
    class G(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            dati = corpo if self.path == "/mesh2tak/stato.json" else b"{}"
            self.send_response(200 if self.path == "/mesh2tak/stato.json" else 404)
            self.send_header("Content-Length", str(len(dati)))
            self.end_headers()
            self.wfile.write(dati)

    s = http.server.ThreadingHTTPServer(("127.0.0.1", porta_libera()), G)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(pki / "takserver-chain.pem"), str(pki / "takserver.key"))
    s.socket = ctx.wrap_socket(s.socket, server_side=True)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


def test_chiede_lo_stato_al_ponte(pki):
    for corpo, atteso in ((json.dumps(stato(T0)).encode(), "ok"), (b"non json", None), (b'{"formato": 2}', None),
                          (b"[1, 2]", None)):
        s = ponte_finto(pki, corpo)
        try:
            d = rete.Sorveglianza.chiedi_stato(s.server_address[1])
        finally:
            s.shutdown()
            s.server_close()
        assert (d is not None and d["formato"] == 1) if atteso else d is None
    assert rete.Sorveglianza.chiedi_stato(porta_libera()) is None


# ---------------------------------------------------------------- dalla pagina

def test_prova_e_stato_per_la_pagina(tmp_path, monkeypatch):
    monkeypatch.setattr(rete, "RADICE", str(tmp_path))
    for d in (rete.SITO_DIR, rete.RICHIESTE_DIR, rete.LAVORI_DIR, rete.TMP_DIR, rete.ESITI_DIR, rete.ACCESSO_DIR):
        os.makedirs(rete.P(d))
    r = Rete(tmp_path, casa=stato(T0))
    sv = rete.Servizio(r.s)
    codice, risposta = sv.azione("/api/prova-avvisi", {}, "andrea")
    assert codice == 200 and risposta["esito"] == "mandato" and "controlla la casella" in risposta["messaggio"]
    assert r.oggetti() == ["[Rete Meshtastic] Email di prova"] and "chiesta da andrea il 6/11 alle 14:00" in r.posta.mandate[0][1]
    assert sv.azione("/api/prova-avvisi", {}, "andrea")[0] == 429
    r.orologio.t += 31
    r.posta.esito = ("nessuno", "in infra-TAK non c'e' un indirizzo per gli avvisi (Guard Dog)")
    codice, risposta = sv.azione("/api/prova-avvisi", {}, "andrea")
    assert codice == 200 and risposta["esito"] == "nessuno" and "Guard Dog" in risposta["messaggio"]
    r.metti("casa", None)
    r.minuti(2)
    vista = sv.stato("andrea")["avvisi"]
    assert vista["problemi"]["casa"][0]["codice"] == "irraggiungibile" and vista["attesa_min"] == 10
    assert rete.Servizio().azione("/api/prova-avvisi", {}, "andrea")[0] == 503


def test_ponte_che_manda_dati_strani_non_ferma_gli_altri(tmp_path):
    strano = {"formato": 1, "nodo": "x", "tak": [1], "pausa": 3, "sistema": {"disco": "pieno", "alimentazione": [],
                                                                            "avvio": 5}, "certificato": "no"}
    r = Rete(tmp_path, casa=strano, sede=stato(T0))
    r.minuti(1)
    r.metti("sede", None)
    r.minuti(11)
    assert "[Rete Meshtastic] Ponte sede: non risponde" in " ".join(r.oggetti())
