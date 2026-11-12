"""Aggiornamenti dalla pagina della rete (1.8.0), lato server: il servizio della pagina legge l'ultima release su
GitHub e accoda le richieste dell'amministratore; il passo dei lavori (root) aggiorna il server scaricando la release
(e se non riparte rimette la versione di prima) e chiede ai ponti di aggiornarsi scrivendolo nella loro registrazione."""
import json
import os
import re
import sys
import threading
import time
import urllib.error

import pytest

from conftest import RADICE, porta_libera
from http_finto import ServerFinto, somme
from test_rete import Server as ServerRete, chiave, dns_di_prova, esito_di, lavoro_in_coda, pytestmark  # noqa: F401

sys.path.insert(0, os.path.join(RADICE, "server"))
import rete_meshtastic as rete  # noqa: E402

REPO = "andrealorenzofiorini/mesh2tak"
ORA = re.search(r'^VERSIONE = "([^"]+)"', open(os.path.join(RADICE, "mesh2tak.py")).read(), re.M).group(1)
_n = [int(x) for x in ORA.split(".")]
NUOVA = f"{_n[0]}.{_n[1] + 1}.0"
FILE_RELEASE = ("installa-ponte-meshtastic.sh", "rete-meshtastic.sh", "SHA256SUMS")


def release(versione=NUOVA, assets=FILE_RELEASE, **kw):
    return dict({"tag_name": f"v{versione}", "body": f"## {versione}\n- novita' di prova", "published_at":
                 "2026-11-06T10:00:00Z", "assets": [{"name": a} for a in assets]}, **kw)


# ---------------------------------------------------------------- servizio della pagina (senza privilegi)

class Orologio:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def ril(tmp_path, monkeypatch):
    monkeypatch.setattr(rete, "VERSIONE", ORA)
    risposte = []

    def chiedi():
        r = risposte[0] if len(risposte) == 1 else risposte.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    o = Orologio()
    r = rete.Rilasci(ora=o, chiedi=chiedi, percorso=str(tmp_path / "rilasci.json"))
    r.risposte, r.orologio = risposte, o
    return r


def errore_http(codice):
    return urllib.error.HTTPError("https://api.github.com/x", codice, "x", {}, None)


def test_release_nuova_completa(ril):
    ril.risposte.append(release())
    assert ril.controlla() == (True, "")
    v = ril.vista()
    assert v["installata"] == ORA and v["nuova"] is True and v["errore"] is None and v["controllato"] == 1_000_000
    assert v["ultima"] == {"versione": NUOVA, "note": f"## {NUOVA}\n- novita' di prova", "pubblicata": "2026-11-06T10:00:00Z",
                           "completa": True}
    assert ril.pronta(NUOVA) and not ril.pronta(ORA) and not ril.pronta("9.9.9")


def test_release_senza_tutti_i_file_non_si_installa(ril):
    ril.risposte.append(release(assets=("rete-meshtastic.sh",)))       # la release e' ancora in preparazione
    ril.controlla()
    assert ril.vista()["nuova"] is False and not ril.pronta(NUOVA)


def test_release_uguale_o_piu_vecchia_non_e_nuova(ril):
    ril.risposte.append(release(ORA))
    ril.controlla()
    assert ril.vista()["nuova"] is False and ril.pronta(ORA)


def test_errori_di_github(ril):
    ril.risposte.extend([errore_http(404)])
    ril.controlla()
    assert ril.vista()["errore"] == "nessuna versione pubblicata" and ril.vista()["ultima"] is None
    ril.risposte[:] = [release()]
    ril.orologio.t += 7 * 3600
    ril.controlla()
    ril.risposte[:] = [ConnectionResetError("giu'")]
    ril.orologio.t += 7 * 3600
    ril.controlla()
    v = ril.vista()
    assert v["errore"] == "GitHub non risponde (ConnectionResetError)" and v["ultima"]["versione"] == NUOVA   # resta
    ril.risposte[:] = [errore_http(403)]
    ril.orologio.t += 7 * 3600
    ril.controlla()
    assert ril.vista()["errore"] == "GitHub risponde 403"
    ril.risposte[:] = [{"tag_name": "ultima", "assets": []}]
    ril.orologio.t += 7 * 3600
    ril.controlla()
    assert "non ha un numero di versione" in ril.vista()["errore"]


def test_limiti_dei_controlli(ril):
    ril.risposte.append(release())
    assert ril.controlla()[0] is True
    assert ril.controlla() == (False, "controllato da poco")
    assert ril.controlla(forza=True)[0] is False                 # "Controlla adesso": al massimo uno al minuto
    ril.orologio.t += 61
    assert ril.controlla(forza=True)[0] is True
    assert ril.controlla()[0] is False                           # da solo: ogni 6 ore
    ril.orologio.t += 6 * 3600
    assert ril.controlla()[0] is True


@pytest.fixture
def github(monkeypatch):
    g = ServerFinto()
    monkeypatch.setattr(rete, "GITHUB", g.url)
    monkeypatch.setattr(rete, "GITHUB_API", g.url)
    monkeypatch.setattr(rete, "VERSIONE", ORA)
    yield g
    g.ferma()


NOVITA_DI_PROVA = (f"# Novita'\n\n## {NUOVA}\n\n- una cosa nuova\n- un'altra\n\n## {ORA}\n\n- vecchia\n").encode()


def sito_con_release(g, versione=NUOVA, file=FILE_RELEASE[:2]):
    g.risposte[f"/{REPO}/releases/latest"] = (302, {"Location": f"{g.url}/{REPO}/releases/tag/v{versione}"})
    if file:
        g.risposte[f"/{REPO}/releases/download/v{versione}/SHA256SUMS"] = somme({n: b"x" for n in file})
    g.risposte[f"/{REPO}/raw/v{versione}/NOVITA.md"] = NOVITA_DI_PROVA


def test_senza_api_quando_github_le_rifiuta(github, tmp_path):
    """1.16.2: le API di GitHub danno 60 richieste l'ora per indirizzo a chi non si identifica, contando tutti i
    programmi del server (visto sul server vero: 403 a ogni controllo). La stessa cosa si legge dal sito."""
    github.risposte[f"/repos/{REPO}/releases/latest"] = (403, {})
    sito_con_release(github)
    r = rete.Rilasci(ora=Orologio(), percorso=str(tmp_path / "rilasci.json"))
    assert r.controlla() == (True, "")
    v = r.vista()
    assert v["nuova"] is True and v["errore"] is None
    assert v["ultima"] == {"versione": NUOVA, "note": "- una cosa nuova\n- un'altra", "pubblicata": "", "completa": True}
    assert r.pronta(NUOVA)


def test_con_le_api_il_sito_non_si_tocca(github, tmp_path):
    github.risposte[f"/repos/{REPO}/releases/latest"] = release()
    r = rete.Rilasci(ora=Orologio(), percorso=str(tmp_path / "rilasci.json"))
    assert r.controlla() == (True, "") and r.vista()["ultima"]["pubblicata"] == "2026-11-06T10:00:00Z"
    assert github.richieste == [f"/repos/{REPO}/releases/latest"]


def test_senza_api_release_in_preparazione_o_assente(github, tmp_path):
    github.risposte[f"/repos/{REPO}/releases/latest"] = (429, {})
    sito_con_release(github, file=())                         # la release c'e' ma i file non ancora
    o = Orologio()
    r = rete.Rilasci(ora=o, percorso=str(tmp_path / "rilasci.json"))
    r.controlla()
    assert r.vista()["ultima"]["completa"] is False and r.vista()["nuova"] is False
    sito_con_release(github, file=("rete-meshtastic.sh",))    # manca l'installatore
    o.t += 7 * 3600
    r.controlla()
    assert r.vista()["ultima"]["completa"] is False
    github.risposte[f"/{REPO}/releases/latest"] = (302, {"Location": f"{github.url}/{REPO}/releases"})   # nessuna release
    o.t += 7 * 3600
    r.controlla()
    assert r.vista()["errore"] == "nessuna versione pubblicata"
    del github.risposte[f"/{REPO}/releases/latest"]           # anche il sito non risponde come deve
    o.t += 7 * 3600
    r.controlla()
    assert r.vista()["errore"] == "nessuna versione pubblicata"


def test_senza_api_e_senza_novita_si_aggiorna_lo_stesso(github, tmp_path):
    github.risposte[f"/repos/{REPO}/releases/latest"] = (403, {})
    sito_con_release(github)
    del github.risposte[f"/{REPO}/raw/v{NUOVA}/NOVITA.md"]
    r = rete.Rilasci(ora=Orologio(), percorso=str(tmp_path / "rilasci.json"))
    r.controlla()
    assert r.vista()["nuova"] is True and r.vista()["ultima"]["note"] == ""


def test_quello_che_ha_visto_resta_dopo_un_riavvio(ril, tmp_path):
    ril.risposte.append(release())
    ril.controlla()
    dopo = rete.Rilasci(ora=ril.orologio, chiedi=lambda: pytest.fail("non deve chiedere"),
                        percorso=str(tmp_path / "rilasci.json"))
    assert dopo.vista()["ultima"]["versione"] == NUOVA and dopo.controlla() == (False, "controllato da poco")


def test_versione_di_prova_e_piu_vecchia_di_tutte():
    assert rete.numeri_versione("@@VERSIONE@@") < rete.numeri_versione("0.0.1")
    assert rete.numeri_versione("1.10.0") > rete.numeri_versione("1.9.3")


@pytest.fixture
def sv(tmp_path, monkeypatch, ril):
    monkeypatch.setattr(rete, "RADICE", str(tmp_path / "r"))
    for d in (rete.SITO_DIR, rete.ACCESSO_DIR, rete.RICHIESTE_DIR, rete.LAVORI_DIR, rete.TMP_DIR, rete.ESITI_DIR):
        os.makedirs(rete.P(d))
    with open(rete.P(rete.SITO_DIR + "/ponti.json"), "w") as f:
        json.dump({"formato": 1, "ponti": [{"nome": "casa", "indirizzo": "https://meshtastic-casa.prova.lan/"}]}, f)
    s = rete.Servizio(rilasci=ril)
    return s


def in_coda():
    cartella = rete.P(rete.LAVORI_DIR)
    return [json.load(open(os.path.join(cartella, f))) for f in sorted(os.listdir(cartella))]


def test_pagina_vede_la_versione_nuova(sv, ril):
    assert sv.stato("andrea")["rilasci"]["ultima"] is None
    ril.risposte.append(release())
    codice, r = sv.azione("/api/controlla-aggiornamenti", {}, "andrea")
    assert codice == 200 and r["rilasci"]["nuova"] is True and r["rilasci"]["ultima"]["versione"] == NUOVA
    assert sv.azione("/api/controlla-aggiornamenti", {}, "andrea")[0] == 429
    assert sv.stato("andrea")["rilasci"]["nuova"] is True


def test_aggiorna_il_server_solo_alla_versione_pubblicata(sv, ril):
    for v in ("9.9.9", "", "1.2", "../x"):
        assert sv.azione("/api/aggiorna-server", {"versione": v}, "andrea")[0] == 409
    assert in_coda() == []
    ril.risposte.append(release())
    ril.controlla()
    codice, r = sv.azione("/api/aggiorna-server", {"versione": NUOVA}, "andrea")
    assert codice == 202 and "per un minuto la pagina non risponde" in r["messaggio"]
    [lv] = in_coda()
    assert (lv["azione"], lv["nome"], lv["da"], lv["versione"]) == ("aggiorna-server", "server", "andrea", NUOVA)
    codice, r = sv.azione("/api/aggiorna-server", {"versione": NUOVA}, "andrea")
    assert codice == 409 and "gia' in corso" in r["errore"] and len(in_coda()) == 1
    assert [x["versione"] for x in sv.stato("andrea")["lavori"]] == [NUOVA]


def test_il_server_non_torna_indietro(sv, ril):
    ril.risposte.append(release(ORA))
    ril.controlla()
    codice, r = sv.azione("/api/aggiorna-server", {"versione": ORA}, "andrea")
    assert codice == 409 and f"ha gia' la {ORA}" in r["errore"] and in_coda() == []


def test_prima_il_server_poi_i_ponti(sv, ril, monkeypatch):
    ril.risposte.append(release())
    ril.controlla()
    codice, r = sv.azione("/api/aggiorna-ponti", {"versione": NUOVA}, "andrea")
    assert codice == 409 and r["errore"] == f"prima aggiorna il server alla {NUOVA}" and in_coda() == []
    monkeypatch.setattr(rete, "VERSIONE", NUOVA)                 # server aggiornato
    assert sv.azione("/api/aggiorna-ponti", {"versione": NUOVA, "nome": "sede"}, "andrea")[0] == 404
    assert sv.azione("/api/aggiorna-ponti", {"versione": NUOVA, "nome": "casa"}, "andrea")[0] == 202
    codice, r = sv.azione("/api/aggiorna-ponti", {"versione": NUOVA}, "andrea")
    assert codice == 202 and "lo fanno da soli" in r["messaggio"]
    assert sorted((x["azione"], x["nome"], x["versione"]) for x in in_coda()) == \
        [("aggiorna-ponti", "casa", NUOVA), ("aggiorna-ponti", "tutti", NUOVA)]


def test_lavori_di_aggiornamento_letti_con_sospetto(tmp_path):
    def leggi(**dati):
        f = tmp_path / "l.json"
        f.write_text(json.dumps(dict({"id": "a1b2c3d4e5f60718", "da": "andrea"}, **dati)))
        return rete.leggi_lavoro(str(f))
    assert leggi(azione="aggiorna-server", nome="server", versione="1.9.0")["versione"] == "1.9.0"
    assert leggi(azione="aggiorna-ponti", nome="tutti", versione="1.9.0")["nome"] == "tutti"
    assert leggi(azione="aggiorna-ponti", nome="casa", versione="1.9.0")["nome"] == "casa"
    for cattivo in ({"azione": "aggiorna-server", "nome": "casa", "versione": "1.9.0"},
                    {"azione": "aggiorna-server", "nome": "server", "versione": "1.9.0; rm -rf /"},
                    {"azione": "aggiorna-server", "nome": "server"},
                    {"azione": "aggiorna-ponti", "nome": "../x", "versione": "1.9.0"},
                    {"azione": "aggiorna-tutto", "nome": "server", "versione": "1.9.0"}):
        assert leggi(**cattivo) is None, cattivo


# ---------------------------------------------------------------- passo dei lavori (root), sul server finto

def script_rete(versione):
    """Il rete-meshtastic.sh di questa versione come se fosse quello di una release piu' nuova."""
    testo = open(os.path.join(RADICE, "dist", "rete-meshtastic.sh"), encoding="utf-8").read()
    assert testo.count(f'VERSIONE = "{ORA}"') == 1
    return testo.replace(f'VERSIONE = "{ORA}"', f'VERSIONE = "{versione}"').replace(
        f"(mesh2tak {ORA})", f"(mesh2tak {versione})").encode()


class ServerConGitHub(ServerRete):
    """Il server della rete finto, con un GitHub finto e il servizio della pagina finto (risponde con la versione
    dello script installato in /opt, come il servizio vero dopo il riavvio)."""

    def __init__(self, cartella):
        super().__init__(cartella)
        self.github = ServerFinto()
        self.rotte = set()                   # versioni con cui il servizio della pagina non riparte
        self.servizio = ServerFinto({"/api/stato": self.stato_servizio})
        self.extra = {"RETE_PROVA_GITHUB": self.github.url, "RETE_PROVA_PORTA_WEB": str(self.servizio.porta),
                      "RETE_PROVA_ATTESA_SERVIZIO": "3"}

    def installata(self):
        testo = self.file("/opt/mesh2tak-rete/rete_meshtastic.py") or ""
        m = re.search(r'^VERSIONE = "([^"]+)"', testo, re.M)
        return m.group(1) if m else None

    def stato_servizio(self):
        v = self.installata()
        return None if v in self.rotte else {"versione": v}

    def pubblica(self, versione, script, impronte=None):
        base = f"/{REPO}/releases/download/v{versione}"
        self.github.risposte[f"{base}/rete-meshtastic.sh"] = script
        self.github.risposte[f"{base}/SHA256SUMS"] = impronte or somme({"rete-meshtastic.sh": script,
                                                                        "installa-ponte-meshtastic.sh": b"x"})

    def esegui(self, *argomenti):
        vecchio = os.environ.copy()
        os.environ.update(self.extra)
        try:
            return super().esegui(*argomenti)
        finally:
            os.environ.clear()
            os.environ.update(vecchio)

    def ferma(self):
        super().ferma()
        self.github.ferma()
        self.servizio.ferma()


@pytest.fixture
def srv(tmp_path):
    s = ServerConGitHub(tmp_path / "s")
    (s.radice / "var/log").mkdir(parents=True, exist_ok=True)
    esito, uscita = s.esegui("installa")
    assert esito == 0, uscita
    assert s.installata() == ORA
    yield s
    s.ferma()


def test_server_aggiornato_dalla_release(srv):
    srv.pubblica(NUOVA, script_rete(NUOVA))
    vecchio = lavoro_in_coda(srv, id="a0000000000000aa", azione="aggiorna-server", nome="server", versione=NUOVA)
    lavoro = lavoro_in_coda(srv, id="b0000000000000bb", azione="aggiorna-server", nome="server", versione=NUOVA)
    srv.log.write_text("")
    esito, uscita = srv.esegui("lavora")
    assert esito == 0, uscita
    e = esito_di(srv, lavoro)
    assert e["esito"] == "ok" and e["messaggio"] == f"server aggiornato alla {NUOVA}" and e["versione"] == NUOVA, e
    assert f"Server aggiornato dalla {ORA} alla {NUOVA}." in e["testo"] and "Fatto." in e["testo"]
    assert esito_di(srv, vecchio)["messaggio"] == "sostituito da una richiesta piu' recente"
    assert srv.installata() == NUOVA
    assert "systemctl restart mesh2tak-rete-web.service" in srv.chiamate()
    # la versione di prima resta da parte; il file scaricato no
    assert f'VERSIONE = "{ORA}"' in srv.file("/etc/mesh2tak-rete/rete_meshtastic-precedente.py")
    assert oct((srv.radice / "etc/mesh2tak-rete/rete_meshtastic-precedente.py").stat().st_mode)[-3:] == "700"
    assert not (srv.radice / f"etc/mesh2tak-rete/rete-meshtastic-{NUOVA}.sh").exists()
    assert srv.github.richieste.count(f"/{REPO}/releases/download/v{NUOVA}/rete-meshtastic.sh") == 1
    assert f"server aggiornato alla {NUOVA}" in srv.file("/var/log/mesh2tak-rete.log")


def test_server_che_non_riparte_torna_alla_versione_di_prima(srv):
    srv.pubblica(NUOVA, script_rete(NUOVA))
    srv.rotte = {NUOVA}
    lavoro = lavoro_in_coda(srv, azione="aggiorna-server", nome="server", versione=NUOVA)
    assert srv.esegui("lavora")[0] == 0
    e = esito_di(srv, lavoro)
    assert e["esito"] == "errore" and e["messaggio"] == \
        f"con la {NUOVA} il servizio della pagina non riparte: rimessa la {ORA}", e
    assert "--- ritorno alla versione di prima ---" in e["testo"]
    assert srv.installata() == ORA


def test_installazione_non_riuscita_rimette_la_versione_di_prima(srv):
    rotto = (f'#!/usr/bin/env bash\n# mesh2tak {NUOVA}\n# VERSIONE = "{NUOVA}"\n'
             'echo "     1/6  Controlli"\necho "✘ prova: Caddy rifiuta la configurazione" >&2\nexit 1\n').encode()
    srv.pubblica(NUOVA, rotto)
    lavoro = lavoro_in_coda(srv, azione="aggiorna-server", nome="server", versione=NUOVA)
    assert srv.esegui("lavora")[0] == 0
    e = esito_di(srv, lavoro)
    assert e["esito"] == "errore" and e["messaggio"] == (f"l'installazione della {NUOVA} non e' riuscita (prova: Caddy "
                                                         f"rifiuta la configurazione): rimessa la {ORA}"), e
    assert srv.installata() == ORA


def test_file_diverso_da_quello_pubblicato_non_si_installa(srv):
    srv.pubblica(NUOVA, script_rete(NUOVA), impronte=somme({"rete-meshtastic.sh": b"un altro file"}))
    lavoro = lavoro_in_coda(srv, azione="aggiorna-server", nome="server", versione=NUOVA)
    srv.log.write_text("")
    assert srv.esegui("lavora")[0] == 0
    e = esito_di(srv, lavoro)
    assert e["esito"] == "errore" and "non e' quello pubblicato" in e["messaggio"]
    assert srv.installata() == ORA and not (srv.radice / "etc/mesh2tak-rete/rete_meshtastic-precedente.py").exists()
    assert not [c for c in srv.chiamate() if c.startswith("systemctl restart")]


def test_file_di_un_altra_versione_non_si_installa(srv):
    srv.pubblica(NUOVA, script_rete("7.7.7"))
    lavoro = lavoro_in_coda(srv, azione="aggiorna-server", nome="server", versione=NUOVA)
    assert srv.esegui("lavora")[0] == 0
    assert f"non e' la versione {NUOVA}" in esito_di(srv, lavoro)["messaggio"] and srv.installata() == ORA


def test_server_gia_alla_versione_chiesta(srv):
    lavoro = lavoro_in_coda(srv, azione="aggiorna-server", nome="server", versione=ORA)
    assert srv.esegui("lavora")[0] == 0
    assert esito_di(srv, lavoro)["messaggio"] == f"il server ha gia' la {ORA}: niente da fare"
    assert srv.github.richieste == []


def test_ponti_aggiornati_dalla_loro_registrazione(srv):
    assert srv.esegui("aggiungi", "casa", chiave(1))[0] == 0
    assert srv.esegui("aggiungi", "sede", chiave(2))[0] == 0
    prima = time.time()
    lavoro = lavoro_in_coda(srv, azione="aggiorna-ponti", nome="casa", versione=NUOVA)
    assert srv.esegui("lavora")[0] == 0
    assert esito_di(srv, lavoro)["esito"] == "ok"
    reg = {r["nome"]: r for r in srv.registrazioni().values()}
    assert reg["casa"]["aggiorna"]["versione"] == NUOVA and reg["casa"]["aggiorna"]["chiesto"] >= int(prima)
    assert "aggiorna" not in reg["sede"]
    lavoro = lavoro_in_coda(srv, id="c0000000000000cc", azione="aggiorna-ponti", nome="tutti", versione=NUOVA)
    assert srv.esegui("lavora")[0] == 0
    reg = {r["nome"]: r for r in srv.registrazioni().values()}
    assert {n: r["aggiorna"]["versione"] for n, r in reg.items()} == {"casa": NUOVA, "sede": NUOVA}
    # resta anche quando il server riscrive le registrazioni (installa di nuovo)
    assert srv.esegui("installa")[0] == 0
    assert all(r["aggiorna"]["versione"] == NUOVA for r in srv.registrazioni().values())
    assert json.loads(srv.file("/etc/mesh2tak-rete/registro.json"))["ponti"]["sede"]["aggiorna"]["versione"] == NUOVA


def test_ponte_che_non_c_e(srv):
    lavoro = lavoro_in_coda(srv, azione="aggiorna-ponti", nome="casa", versione=NUOVA)
    assert srv.esegui("lavora")[0] == 0
    e = esito_di(srv, lavoro)
    assert e["esito"] == "errore" and "gateway non trovato" in e["messaggio"]
