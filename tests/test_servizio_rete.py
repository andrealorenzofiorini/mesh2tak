"""Servizio della pagina della rete (utente senza privilegi) e passo dei lavori (root), dalla 1.5.0: richieste dei
Raspberry, azioni degli amministratori, e i controlli contro richieste false, ripetute o maligne."""
import base64
import http.server
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from conftest import RADICE, porta_libera

sys.path.insert(0, os.path.join(RADICE, "server"))
import rete_meshtastic as rete  # noqa: E402


def chiave(n):
    blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes([n]) * 32
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} mesh2tak-prova{n}@raspberry"


def impronta(n):
    return rete.leggi_chiave(chiave(n))[1]


def domanda(n, nome="casa", **kw):
    return dict({"formato": 1, "nome": nome, "chiave": chiave(n), "macchina": "raspberrypi", "versione": "1.5.0"}, **kw)


@pytest.fixture
def sv(tmp_path, monkeypatch):
    monkeypatch.setattr(rete, "RADICE", str(tmp_path))
    for d in (rete.SITO_DIR, rete.ACCESSO_DIR, rete.RICHIESTE_DIR, rete.LAVORI_DIR, rete.TMP_DIR, rete.ESITI_DIR):
        os.makedirs(rete.P(d))
    s = rete.Servizio()
    s.metti_ponti = lambda *nomi: open(rete.P(rete.SITO_DIR + "/ponti.json"), "w").write(json.dumps(
        {"formato": 1, "ponti": [{"nome": n, "indirizzo": f"https://meshtastic-{n}.prova.lan/"} for n in nomi]}))
    s.metti_ponti()
    return s


def registra(n, nome):
    with open(rete.P(f"{rete.ACCESSO_DIR}/{impronta(n)}.json"), "w") as f:
        json.dump({"formato": 1, "nome": nome, "chiave": chiave(n)}, f)


def lavori():
    cartella = rete.P(rete.LAVORI_DIR)
    return [json.load(open(os.path.join(cartella, f))) for f in sorted(os.listdir(cartella))]


def esito(lavoro, esito_, messaggio="", testo="", quando=None):
    with open(rete.P(f"{rete.ESITI_DIR}/{lavoro}.json"), "w") as f:
        json.dump({"id": lavoro, "azione": "aggiungi", "nome": "casa", "da": "andrea", "esito": esito_,
                   "messaggio": messaggio, "testo": testo, "quando": quando or int(time.time())}, f)


def richiesta_salvata(n):
    return json.load(open(rete.P(f"{rete.RICHIESTE_DIR}/{impronta(n)}.json")))


# ---- richieste dei Raspberry ----

def test_richiesta_nuova_in_attesa(sv):
    codice, r = sv.nuova_richiesta(domanda(1), "203.0.113.5")
    assert codice == 202 and r == {"esito": "in_attesa", "controllo": rete.codice_controllo(impronta(1)),
                                   "sostituisce": False}
    st = sv.stato("andrea")
    [v] = st["richieste"]
    assert (v["nome"], v["macchina"], v["ip"], v["stato"], v["controllo"]) == \
        ("casa", "raspberrypi", "203.0.113.5", "attesa", r["controllo"])
    assert "chiave" not in v and st["utente"] == "andrea" and st["rifiutate"] == [] and st["lavori"] == []
    assert lavori() == []                                   # niente cambia sul server senza approvazione


def test_richiesta_ripetuta_non_consuma_il_limite(sv):
    for _ in range(10):
        assert sv.nuova_richiesta(domanda(1), "203.0.113.5")[0] == 202
    assert len(sv.stato("a")["richieste"]) == 1
    for n in range(2, 7):
        assert sv.nuova_richiesta(domanda(n, f"p{n}"), "203.0.113.5")[0] == 202


def test_limite_per_indirizzo_e_per_rete_ipv6(sv):
    for n in range(1, 7):
        assert sv.nuova_richiesta(domanda(n, f"p{n}"), "203.0.113.5")[0] == 202
    codice, r = sv.nuova_richiesta(domanda(7, "p7"), "203.0.113.5")
    assert codice == 429 and "un'ora" in r["errore"]
    assert sv.nuova_richiesta(domanda(7, "p7"), "198.51.100.9")[0] == 202
    for n in range(10, 16):
        assert sv.nuova_richiesta(domanda(n, f"q{n}"), f"2001:db8::{n}")[0] == 202
    assert sv.nuova_richiesta(domanda(16, "q16"), "2001:db8::ffff")[0] == 429      # stessa rete /64
    assert sv.nuova_richiesta(domanda(16, "q16"), "2001:db8:0:1::1")[0] == 202


def test_troppe_richieste_in_attesa(sv):
    sv.RICHIESTE_MAX = 3
    for n in range(1, 4):
        assert sv.nuova_richiesta(domanda(n, f"p{n}"), f"192.0.2.{n}")[0] == 202
    assert sv.nuova_richiesta(domanda(4, "p4"), "192.0.2.4")[0] == 429
    assert sv.azione("/api/rifiuta", {"id": impronta(1)}, "andrea")[0] == 200     # le rifiutate non contano
    assert sv.nuova_richiesta(domanda(4, "p4"), "192.0.2.4")[0] == 202


@pytest.mark.parametrize("cambia", [{"formato": 2}, {"nome": "Casa!"}, {"nome": "x" * 21}, {"chiave": "ssh-rsa AAAA x"},
                                    {"chiave": "ssh-ed25519 AAAA"}, {"macchina": "a b"}, {"macchina": ""},
                                    {"versione": "x" * 30}, {"versione": "1.5.0;rm"}])
def test_richieste_non_valide(sv, cambia):
    assert sv.nuova_richiesta(domanda(1, **cambia), "203.0.113.5")[0] == 400
    assert sv.nuova_richiesta(["non", "un", "oggetto"], "203.0.113.5")[0] == 400
    assert sv.stato("a")["richieste"] == []


def test_gia_registrato(sv):
    registra(1, "casa")
    assert sv.nuova_richiesta(domanda(1), "203.0.113.5") == (200, {"esito": "registrato",
                                                                  "controllo": rete.codice_controllo(impronta(1))})
    assert sv.stato("a")["richieste"] == []


def test_nome_gia_usato_si_sostituisce_solo_chiedendolo(sv):
    sv.metti_ponti("casa")
    registra(1, "casa")
    codice, r = sv.nuova_richiesta(domanda(2), "203.0.113.5")             # Raspberry reinstallato: chiave nuova
    assert codice == 202 and r["sostituisce"] is True
    [v] = sv.stato("a")["richieste"]
    assert v["sostituisce"] is True
    codice, r = sv.azione("/api/approva", {"id": impronta(2)}, "andrea")
    assert codice == 409 and "Sostituisci" in r["errore"] and lavori() == []
    assert sv.azione("/api/approva", {"id": impronta(2), "sostituisci": True}, "andrea")[0] == 202
    [l] = lavori()
    assert (l["azione"], l["nome"], l["sostituisci"], l["richiesta"]) == ("aggiungi", "casa", True, impronta(2))


def test_stesso_nome_due_richieste(sv):
    sv.nuova_richiesta(domanda(1), "203.0.113.5")
    sv.nuova_richiesta(domanda(2), "198.51.100.9")
    assert [v["stesso_nome"] for v in sv.stato("a")["richieste"]] == [True, True]


# ---- approvazione e lavori ----

def test_approva_mette_in_coda_un_lavoro_completo(sv):
    sv.nuova_richiesta(domanda(1), "203.0.113.5")
    codice, r = sv.azione("/api/approva", {"id": impronta(1)}, "andrea")
    assert codice == 202 and "Sto aggiungendo il ponte casa" in r["messaggio"]
    [l] = lavori()
    assert l["id"] == r["lavoro"] and l["azione"] == "aggiungi" and l["nome"] == "casa" and l["da"] == "andrea"
    assert l["chiave"] == rete.leggi_chiave(chiave(1))[0] and l["richiesta"] == impronta(1) and l["sostituisci"] is False
    assert os.listdir(rete.P(rete.TMP_DIR)) == []          # scritto a parte e spostato in coda gia' completo
    assert rete.leggi_lavoro(rete.P(f"{rete.LAVORI_DIR}/{l['id']}.json"))["impronta"] == impronta(1)
    [v] = sv.stato("a")["richieste"]
    assert v["stato"] == "in_corso" and v["da"] == "andrea" and v["fermo"] is False
    assert sv.azione("/api/approva", {"id": impronta(1)}, "andrea")[0] == 409
    assert sv.azione("/api/rifiuta", {"id": impronta(1)}, "andrea")[0] == 409
    # il Raspberry richiede mentre lo si aggiunge: resta in corso
    assert sv.nuova_richiesta(domanda(1, "altro"), "203.0.113.5")[1].get("in_corso") is True
    assert richiesta_salvata(1)["nome"] == "casa" and len(lavori()) == 1


def test_esito_ok_toglie_la_richiesta(sv):
    sv.nuova_richiesta(domanda(1), "203.0.113.5")
    lavoro = sv.azione("/api/approva", {"id": impronta(1)}, "andrea")[1]["lavoro"]
    os.remove(rete.P(f"{rete.LAVORI_DIR}/{lavoro}.json"))
    esito(lavoro, "ok", "fatto", "1/5 ...")
    st = sv.stato("a")
    assert st["richieste"] == [] and [x["esito"] for x in st["lavori"]] == ["ok"]


def test_esito_errore_rimette_in_attesa_con_i_dettagli(sv):
    sv.nuova_richiesta(domanda(1), "203.0.113.5")
    lavoro = sv.azione("/api/approva", {"id": impronta(1)}, "andrea")[1]["lavoro"]
    os.remove(rete.P(f"{rete.LAVORI_DIR}/{lavoro}.json"))
    esito(lavoro, "errore", "indirizzi mancanti nel DNS", "Type A   Name meshtastic-casa ...")
    [v] = sv.stato("a")["richieste"]
    assert v["stato"] == "attesa" and v["errore"] == "indirizzi mancanti nel DNS" and "Type A" in v["dettagli"]
    assert sv.azione("/api/approva", {"id": impronta(1)}, "andrea")[0] == 202          # "Riprova"
    assert sv.stato("a")["richieste"][0]["errore"] is None


def test_lavoro_fermo_perso_o_interrotto(sv):
    sv.nuova_richiesta(domanda(1), "203.0.113.5")
    lavoro = sv.azione("/api/approva", {"id": impronta(1)}, "andrea")[1]["lavoro"]
    vecchio = time.time() - 600
    os.utime(rete.P(f"{rete.LAVORI_DIR}/{lavoro}.json"), (vecchio, vecchio))
    st = sv.stato("a")
    assert st["richieste"][0]["fermo"] is True and st["lavori"][0]["fermo"] is True    # passo dei lavori che non parte
    # sparito senza esito (ne' in coda ne' finito) da un po'
    os.remove(rete.P(f"{rete.LAVORI_DIR}/{lavoro}.json"))
    r = richiesta_salvata(1)
    r["approvata"] -= 600
    json.dump(r, open(rete.P(f"{rete.RICHIESTE_DIR}/{impronta(1)}.json"), "w"))
    [v] = sv.stato("a")["richieste"]
    assert v["stato"] == "attesa" and "non e' partito" in v["errore"]
    # in corso da troppo: interrotto
    lavoro = sv.azione("/api/approva", {"id": impronta(1)}, "andrea")[1]["lavoro"]
    os.remove(rete.P(f"{rete.LAVORI_DIR}/{lavoro}.json"))
    esito(lavoro, "in_corso", "in corso", quando=int(time.time()) - 3600)
    [v] = sv.stato("a")["richieste"]
    assert v["stato"] == "attesa" and "interrotto" in v["errore"]


def test_richiesta_scaduta(sv):
    sv.nuova_richiesta(domanda(1), "203.0.113.5")
    r = richiesta_salvata(1)
    r["ultima"] -= sv.SCADENZA_S + 10
    json.dump(r, open(rete.P(f"{rete.RICHIESTE_DIR}/{impronta(1)}.json"), "w"))
    assert sv.stato("a")["richieste"] == [] and os.listdir(rete.P(rete.RICHIESTE_DIR)) == []


def test_registrato_da_un_altra_strada(sv):
    sv.nuova_richiesta(domanda(1), "203.0.113.5")
    registra(1, "casa")                                      # p.es. aggiunto a mano dal server
    assert sv.stato("a")["richieste"] == []


# ---- rifiuto, ponte tolto, lascia rientrare ----

def test_rifiuta_e_lascia_rientrare(sv):
    sv.nuova_richiesta(domanda(1), "203.0.113.5")
    codice, r = sv.azione("/api/rifiuta", {"id": impronta(1)}, "andrea")
    assert codice == 200 and "non rientra da solo" in r["messaggio"]
    codice, r = sv.nuova_richiesta(domanda(1), "203.0.113.5")
    assert codice == 403 and "rifiutato" in r["errore"]
    st = sv.stato("a")
    assert st["richieste"] == [] and [(x["nome"], x["motivo"], x["da"], x["chiave"], x["presente"])
                                      for x in st["rifiutate"]] == [("casa", "rifiutata", "andrea", True, False)]
    assert sv.azione("/api/approva", {"id": impronta(1)}, "andrea")[0] == 404          # rifiutata: non si approva
    codice, r = sv.azione("/api/riammetti", {"id": impronta(1)}, "andrea")
    assert codice == 202 and lavori()[0]["chiave"] == rete.leggi_chiave(chiave(1))[0]
    assert sv.stato("a")["rifiutate"] == [] and sv.stato("a")["richieste"][0]["stato"] == "in_corso"


def test_rifiuto_dura_30_giorni(sv):
    sv.nuova_richiesta(domanda(1), "203.0.113.5")
    sv.azione("/api/rifiuta", {"id": impronta(1)}, "andrea")
    r = richiesta_salvata(1)
    r["ultima"] -= 29 * 86400
    json.dump(r, open(rete.P(f"{rete.RICHIESTE_DIR}/{impronta(1)}.json"), "w"))
    assert sv.nuova_richiesta(domanda(1), "203.0.113.5")[0] == 403
    r["ultima"] -= 2 * 86400
    json.dump(r, open(rete.P(f"{rete.RICHIESTE_DIR}/{impronta(1)}.json"), "w"))
    assert sv.nuova_richiesta(domanda(1), "203.0.113.5")[0] == 202
    assert richiesta_salvata(1)["stato"] == "attesa"


def test_ponte_tolto_non_rientra_da_solo(sv):
    sv.metti_ponti("casa", "sede")
    registra(1, "casa")
    codice, r = sv.azione("/api/rimuovi", {"nome": "casa"}, "andrea")
    assert codice == 202 and "Sto togliendo il ponte casa" in r["messaggio"]
    [l] = lavori()
    assert (l["azione"], l["nome"], l["da"]) == ("rimuovi", "casa", "andrea")
    # finche' il lavoro non ha tolto la registrazione il ricordo resta
    assert json.load(open(rete.P(f"{rete.RICHIESTE_DIR}/{impronta(1)}.json")))["motivo"] == "tolto"
    sv.stato("a")
    assert os.path.exists(rete.P(f"{rete.RICHIESTE_DIR}/{impronta(1)}.json"))
    os.remove(rete.P(f"{rete.ACCESSO_DIR}/{impronta(1)}.json"))           # il lavoro l'ha tolto
    sv.metti_ponti("sede")
    codice, r = sv.nuova_richiesta(domanda(1), "203.0.113.5")              # il Raspberry ancora acceso
    assert codice == 403 and "tolto dalla rete" in r["errore"]
    [x] = sv.stato("a")["rifiutate"]
    assert (x["nome"], x["motivo"], x["chiave"]) == ("casa", "tolto", True)
    assert sv.azione("/api/riammetti", {"id": impronta(1)}, "andrea")[0] == 202
    assert sorted(l["azione"] for l in lavori()) == ["aggiungi", "rimuovi"]


def test_lascia_rientrare_con_il_nome_preso(sv):
    sv.nuova_richiesta(domanda(1), "203.0.113.5")
    sv.azione("/api/rifiuta", {"id": impronta(1)}, "andrea")
    sv.metti_ponti("casa")                                   # nel frattempo un altro ponte si chiama casa
    assert sv.stato("a")["rifiutate"][0]["presente"] is True
    assert sv.azione("/api/riammetti", {"id": impronta(1)}, "andrea")[0] == 409


def test_ponte_tolto_senza_chiave_nota(sv):
    sv.metti_ponti("casa")
    with open(rete.P(f"{rete.ACCESSO_DIR}/{impronta(1)}.json"), "w") as f:
        json.dump({"nome": "casa"}, f)                       # registrazione scritta da una versione vecchia
    sv.azione("/api/rimuovi", {"nome": "casa"}, "andrea")
    os.remove(rete.P(f"{rete.ACCESSO_DIR}/{impronta(1)}.json"))
    sv.metti_ponti()
    codice, r = sv.azione("/api/riammetti", {"id": impronta(1)}, "andrea")
    assert codice == 200 and "ripete la richiesta" in r["messaggio"]
    assert sv.nuova_richiesta(domanda(1), "203.0.113.5")[0] == 202


# ---- aggiunta a mano ----

def test_aggiungi_a_mano(sv):
    sv.nuova_richiesta(domanda(3, "sede"), "203.0.113.5")
    sv.azione("/api/rifiuta", {"id": impronta(3)}, "andrea")
    assert sv.azione("/api/aggiungi", {"nome": "sede", "chiave": chiave(3)}, "andrea")[0] == 202
    assert sv.stato("a")["rifiutate"] == []                   # aggiunto a mano: il rifiuto non vale piu'
    sv.metti_ponti("casa")
    registra(1, "casa")
    assert sv.azione("/api/aggiungi", {"nome": "casa", "chiave": chiave(2)}, "andrea")[0] == 409
    assert sv.azione("/api/aggiungi", {"nome": "nuovo", "chiave": chiave(1)}, "andrea")[0] == 409
    assert sv.azione("/api/aggiungi", {"nome": "Bad!", "chiave": chiave(4)}, "andrea")[0] == 400
    assert sv.azione("/api/aggiungi", {"nome": "nuovo", "chiave": "ssh-rsa AAAA"}, "andrea")[0] == 400
    assert sv.azione("/api/rimuovi", {"nome": "../etc"}, "andrea")[0] == 404
    assert sv.azione("/api/sconosciuta", {}, "andrea")[0] == 404
    assert [l["nome"] for l in lavori()] == ["sede"]


# ---- HTTP: chi puo' fare cosa ----

@pytest.fixture
def http_(sv):
    porta = porta_libera()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", porta), rete.gestore_per(sv))
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def chiedi(metodo, percorso, corpo=None, **intestazioni):
        dati = corpo if isinstance(corpo, bytes) else (json.dumps(corpo).encode() if corpo is not None else None)
        rq = urllib.request.Request(f"http://127.0.0.1:{porta}{percorso}", data=dati, method=metodo,
                                    headers=dict({"Content-Type": "application/json"}, **intestazioni))
        apri = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with apri.open(rq, timeout=10) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")
    chiedi.porta = porta
    yield chiedi
    server.shutdown()
    server.server_close()


ADMIN = {"X-Authentik-Username": "andrea", "X-Mesh2tak": "1"}


def test_http_richiesta_pubblica(http_, sv):
    codice, r = http_("POST", "/accesso/richiesta", domanda(1), **{"X-Forwarded-For": "203.0.113.5, 10.0.0.1"})
    assert codice == 202 and r["esito"] == "in_attesa"
    assert sv.stato("a")["richieste"][0]["ip"] == "203.0.113.5"
    http_("POST", "/accesso/richiesta", domanda(2, "sede"), **{"X-Forwarded-For": "<script>"})
    ip = {v["nome"]: v["ip"] for v in sv.stato("a")["richieste"]}
    assert ip == {"casa": "203.0.113.5", "sede": "127.0.0.1"}  # indirizzo strano: quello della connessione


def test_http_amministratori_solo_attraverso_authentik(http_):
    assert http_("GET", "/api/stato")[0] == 403
    assert http_("GET", "/api/stato", **{"X-Authentik-Username": "andrea<script>"})[0] == 403
    codice, r = http_("GET", "/api/stato", **{"X-Authentik-Username": "andrea"})
    assert codice == 200 and r["utente"] == "andrea" and r["formato"] == 1
    assert http_("POST", "/api/aggiungi", {"nome": "casa", "chiave": chiave(1)})[0] == 403


def test_http_contro_le_richieste_da_altri_siti(http_):
    corpo = {"nome": "casa", "chiave": chiave(1)}
    senza = {"X-Authentik-Username": "andrea"}
    assert http_("POST", "/api/aggiungi", corpo, **senza)[0] == 403                       # manca X-Mesh2tak
    assert http_("POST", "/api/aggiungi", corpo, **ADMIN, Origin="https://cattivo.example")[0] == 403
    assert http_("POST", "/api/aggiungi", corpo, **ADMIN, Origin=f"https://127.0.0.1:{http_.porta}")[0] == 202
    assert len(lavori()) == 1


def test_http_corpi_strani(http_):
    assert http_("POST", "/accesso/richiesta", b"{non json")[0] == 400
    assert http_("POST", "/accesso/richiesta", b"x" * 20000)[0] == 413
    assert http_("POST", "/accesso/richiesta", b"\xff\xfe")[0] == 400
    assert http_("POST", "/api/approva", b"[]", **ADMIN)[0] == 400
    assert http_("GET", "/accesso/richiesta")[0] == 404
    assert http_("POST", "/altro", {})[0] == 404
    assert http_("POST", "/API/aggiungi", {"nome": "casa", "chiave": chiave(1)}, **ADMIN)[0] == 404


# ---- passo dei lavori (root) ----

@pytest.fixture
def passo(sv, monkeypatch):
    chiamate = []
    monkeypatch.setattr(rete, "trova_impostazioni", lambda: "imp")
    monkeypatch.setattr(rete, "carica_registro", lambda: {"ponti": {}, "authentik": {"ponti": {}}})
    monkeypatch.setattr(rete, "registra", lambda t: chiamate.append(("registro", t)))

    def aggiungi(imp, reg, nome, chiave_, impronta_, ignora_dns=False, cambio_chiave=True, installa=None, da=""):
        chiamate.append(("aggiungi", nome, chiave_, impronta_, cambio_chiave))
        if installa is not None:
            chiamate.append(("installa", nome, installa, da))
        print("1/5  Indirizzi ...")
        if nome == "rotto":
            raise rete.Errore("indirizzi mancanti nel DNS")
        if nome == "strano":
            raise KeyError("x")

    monkeypatch.setattr(rete, "aggiungi_ponte", aggiungi)
    monkeypatch.setattr(rete, "rimuovi_ponte", lambda imp, reg, nome: chiamate.append(("rimuovi", nome)))
    return chiamate


def esiti():
    cartella = rete.P(rete.ESITI_DIR)
    return {f[:-5]: json.load(open(os.path.join(cartella, f))) for f in os.listdir(cartella)}


def test_passo_dei_lavori(sv, passo):
    a = sv.accoda("aggiungi", "casa", "andrea", rete.leggi_chiave(chiave(1))[0], impronta(1))
    b = sv.accoda("aggiungi", "rotto", "andrea", chiave(2))
    c = sv.accoda("aggiungi", "strano", "andrea", chiave(3))
    d = sv.accoda("rimuovi", "sede", "maria")
    e = sv.accoda("aggiungi", "nuovo", "andrea", chiave(4), sostituisci=True)
    assert rete.cmd_lavora(None) == 0
    assert os.listdir(rete.P(rete.LAVORI_DIR)) == []
    fatti = [x for x in passo if x[0] != "registro"]
    assert ("aggiungi", "casa", rete.leggi_chiave(chiave(1))[0], impronta(1), False) in fatti
    assert ("aggiungi", "nuovo", rete.leggi_chiave(chiave(4))[0], impronta(4), True) in fatti
    assert ("rimuovi", "sede") in fatti
    es = esiti()
    assert es[a]["esito"] == "ok" and "1/5  Indirizzi" in es[a]["testo"] and es[a]["richiesta"] == impronta(1)
    assert es[b]["esito"] == "errore" and es[b]["messaggio"] == "indirizzi mancanti nel DNS"
    assert es[c]["esito"] == "errore" and "inatteso" in es[c]["messaggio"]
    assert es[d]["esito"] == "ok" and es[d]["da"] == "maria" and es[e]["esito"] == "ok"
    assert "chiave" not in json.dumps(list(es.values())) or all("ssh-ed25519" not in json.dumps(x) for x in es.values())


def test_passo_dei_lavori_ponti_nuovi(sv, passo, monkeypatch):
    """1.10.0: aggiunta con installazione, impostazioni e "Riprova" arrivano a root come previsto."""
    salvate, riprovate = [], []
    monkeypatch.setattr(rete, "salva_impostazioni_nuovi", lambda lv: salvate.append(lv) or "Impostazioni salvate: ok.")
    monkeypatch.setattr(rete, "riprova_installazione", lambda imp, reg, lv: riprovate.append(lv))
    a = sv.accoda("aggiungi", "velate", "andrea", chiave(1), impronta(1), installa={"scheda": "usb", "posizione": ""})
    b = sv.accoda("impostazioni-nuovi", "ponti-nuovi", "andrea", impostazioni={
        "nome_pacchetto": "a.zip", "pacchetto": "UEs=", "password": "", "host": "", "canale": "CNSAS-VA",
        "chiave": base64.b64encode(b"k" * 32).decode(), "secondario": ""})
    c = sv.accoda("installa", "velate", "andrea", installa={"scheda": "waveshare"})
    d = sv.accoda("aggiungi", "male", "andrea", chiave(2), impronta(2), installa={"scheda": "boh"})
    assert rete.cmd_lavora(None) == 0
    assert ("installa", "velate", {"scheda": "usb", "posizione": ""}, "andrea") in passo
    assert salvate[0]["impostazioni"]["canale"] == "CNSAS-VA" and riprovate[0]["installa"] == {"scheda": "waveshare"}
    es = esiti()
    assert es[a]["esito"] == "ok" and es[b]["esito"] == "ok" and es[c]["esito"] == "ok"
    assert es[b]["messaggio"] == "Impostazioni salvate: ok." and "k" * 10 not in json.dumps(es[b])
    assert d not in es and "male" not in [x[1] for x in passo]             # lavoro strano: scartato


def test_passo_dei_lavori_scarta_i_file_strani(sv, passo, tmp_path):
    cartella = rete.P(rete.LAVORI_DIR)
    buono = {"id": "0123456789abcdef", "azione": "aggiungi", "nome": "casa", "da": "andrea", "chiave": chiave(1)}
    strani = {
        "1111111111111111.json": dict(buono, id="1111111111111111", azione="esegui"),
        "2222222222222222.json": dict(buono, id="2222222222222222", nome="../../etc"),
        "3333333333333333.json": dict(buono, id="3333333333333333", chiave="ssh-rsa AAAA"),
        "4444444444444444.json": dict(buono, id="4444444444444444", da="root; rm -rf /"),
        "5555555555555555.json": dict(buono, id="zzz"),
        "6666666666666666.json": dict(buono, id="6666666666666666", nome="x" * 2000, pad="y" * 20000),
        "0123456789abcdef.json.nuovo": buono,
        "nome-strano.json": buono,
    }
    for nome, dati in strani.items():
        json.dump(dati, open(os.path.join(cartella, nome), "w"))
    open(os.path.join(cartella, "7777777777777777.json"), "w").write("{non json")
    segreto = tmp_path / "segreto.json"
    json.dump(dict(buono, id="8888888888888888"), open(segreto, "w"))
    os.symlink(segreto, os.path.join(cartella, "8888888888888888.json"))
    os.mkdir(os.path.join(cartella, "9999999999999999.json"))
    assert rete.cmd_lavora(None) == 0
    assert os.listdir(cartella) == [] and segreto.exists()
    assert [x for x in passo if x[0] != "registro"] == [] and esiti() == {}
    assert sum(1 for x in passo if x[0] == "registro" and "scartato" in x[1]) == len(strani) + 3


def test_passo_dei_lavori_tiene_gli_ultimi_esiti(sv, passo):
    for i in range(35):
        esito(f"{i:016x}", "ok", quando=int(time.time()) - 1000 + i)
        t = time.time() - 1000 + i
        os.utime(rete.P(f"{rete.ESITI_DIR}/{i:016x}.json"), (t, t))
    rete.cmd_lavora(None)
    assert len(esiti()) == 30 and f"{34:016x}" in esiti() and f"{0:016x}" not in esiti()
    assert len(sv.lavori()) == 8                              # alla pagina bastano gli ultimi
