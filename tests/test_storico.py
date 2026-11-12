"""Storico (1.16.0): ogni mezz'ora il ponte annota come stanno lui e i ripetitori fissi, per i grafici della pagina
della rete. Niente posizioni; solo dal tunnel, per gli amministratori."""
import json
import os
import time

import mesh2tak as m
from test_copertura import R, ripetitore
from test_gestione import gestione  # noqa: F401 (fixture)
from test_icone import Finto
from test_ponte_logica import ponte, sdk  # noqa: F401 (fixture)


def test_un_campione_ogni_mezz_ora_e_trenta_giorni(tmp_path):
    f = tmp_path / "storico.json"
    s = m.Storico(str(f))
    ora = time.time()
    rip = ("!3333cccc", "Ripetitore", [80, 4.01, -7.5, 1])
    assert s.campiona(ora - 3600, [5.0, 0.5, 2, 1], [rip + (10,)]) is True
    assert s.campiona(ora - 3000, [9.0, 0.9, 9, 1], [rip + (99,)]) is False, "troppo presto"
    assert s.campiona(ora - 1800, [6.0, 0.6, 3, 1], [rip + (14,)]) is True
    assert s.campiona(ora, [7.0, 0.7, 3, 0], [("!3333cccc", "Ripetitore", [None, None, None, 0], 2)]) is True
    d = s.dati()
    assert [c[1:] for c in d["ponte"]] == [[5.0, 0.5, 2, 1], [6.0, 0.6, 3, 1], [7.0, 0.7, 3, 0]] and d["ogni_s"] == 1800
    # i messaggi ripetuti nella mezz'ora: il contatore del ponte riparte da zero quando il ponte riparte
    assert [c[1:] for c in d["ripetitori"]["!3333cccc"]["campioni"]] == [
        [80, 4.01, -7.5, 1, 10], [80, 4.01, -7.5, 1, 4], [None, None, None, 0, 2]]
    s.salva(subito=True)
    assert os.stat(f).st_mode & 0o077 == 0
    # ricaricato; i campioni piu' vecchi di 30 giorni escono, e con loro i ripetitori rimasti senza
    d = json.loads(f.read_text())
    d["ponte"].insert(0, [ora - 31 * 86400, 1.0, 0.1, 0, 1])
    d["ripetitori"]["!4444dddd"] = {"nome": "Vecchio", "campioni": [[ora - 40 * 86400, 50, 3.7, -3.0, 1, 0]]}
    d["ripetitori"]["non-una-radio"] = {"nome": "x", "campioni": [[ora, 1, 1, 1, 1, 1]]}
    f.write_text(json.dumps(d))
    s2 = m.Storico(str(f))
    assert len(s2.ponte) == 3 and list(s2.ripetitori) == ["!3333cccc"]
    assert s2.campiona(ora + 600, [1.0, 0.1, 0, 1], []) is False, "riparte dall'ultimo campione salvato"


def test_file_rovinato_si_riparte(tmp_path, caplog):
    f = tmp_path / "storico.json"
    f.write_text('{"formato": 1, "gateway": "x", "ripetitori": [1, 2]}')
    s = m.Storico(str(f))
    assert s.ponte == [] and s.ripetitori == {}
    f.write_text("{rotto")
    assert m.Storico(str(f)).dati()["ponte"] == []
    assert "riparto da zero" in caplog.text


def test_il_ponte_campiona_i_ripetitori_fissi(ponte, tmp_path):
    p = ponte()
    p.storico = m.Storico(str(tmp_path / "storico.json"))
    ripetitore(p)                       # ruolo Router: fisso
    with p._lock:
        v = p.viste[R]
        v.batteria, v.tensione, v.metriche, v.ripetuti = 77, 3.95, time.time(), 5
    p.manutenzione()
    d = p.storico.dati()
    assert len(d["ponte"]) == 1 and d["ponte"][0][3] == 1, "una radio sulla mappa (il ripetitore ha mandato la posizione)"
    r = d["ripetitori"]["!3333cccc"]
    assert r["nome"] == "Ripetitore Campo dei Fiori" and r["campioni"][0][1:] == [77, 3.95, -6.0, 1, 5]
    p.manutenzione()
    assert len(p.storico.dati()["ponte"]) == 1, "non prima di mezz'ora"
    assert "lat" not in json.dumps(d) and "45.9" not in json.dumps(d), "niente posizioni"


def test_lo_storico_solo_per_gli_amministratori_dal_tunnel(gestione):
    g, _, _ = gestione()

    def chiedi(metodo, da="127.0.0.1", **intestazioni):
        h = Finto(metodo, b"", da=da, **intestazioni)
        g._servi_storico(h)
        testo = h.wfile.getvalue()
        return h.codice, (json.loads(testo) if testo else None)
    amministratore = {"X-Authentik-Username": "andrea"}
    assert chiedi("GET", **amministratore)[0] == 404, "gateway senza storico"
    g.storico = lambda: {"formato": 1, "ponte": [], "ripetitori": {}}
    assert chiedi("GET", **amministratore) == (200, {"formato": 1, "ponte": [], "ripetitori": {}})
    assert chiedi("GET")[0] == 403 and chiedi("GET", da="192.168.1.50", **amministratore)[0] == 403
    assert chiedi("POST", **amministratore)[0] == 405
