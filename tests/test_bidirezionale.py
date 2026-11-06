"""Prove complete in entrambe le direzioni, con processi veri: nodi Meshtastic finti su una rete radio
finta (una cartella condivisa), TAK Server finto con TLS, uno o due ponti."""
import json
import os
import re
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import pytest

from conftest import AMBIENTE, QUI, RADICE, SDK, aspetta_porta, porta_libera, scrivi_config
import mesh2tak as m

PONTE = os.path.join(RADICE, "mesh2tak.py")
CAMPO = 0x1111AAAA   # radio di un volontario in campo


@pytest.fixture(scope="module")
def sdk():
    import meshtastic  # noqa: F401
    return m.LibreriaTak()


def cot_fresco(nome, **sostituzioni):
    xml = open(f"{SDK}/testdata/cot_xml/{nome}.xml", encoding="utf-8").read()
    ora = time.time()
    tag = re.search(r"<event\b[^>]*>", xml).group(0)
    nuovo = re.sub(r'\b(time|start)="[^"]*"', lambda x: f'{x.group(1)}="{m.iso_utc(ora)}"', tag)
    nuovo = re.sub(r'\bstale="[^"]*"', f'stale="{m.iso_utc(ora + 3600)}"', nuovo)
    xml = xml.replace(tag, nuovo, 1)
    for vecchio, nuovo_testo in sostituzioni.items():
        xml = xml.replace(vecchio, nuovo_testo)
    return xml


class Impianto:
    """Rete radio finta, TAK Server finto e i processi da fermare alla fine."""

    def __init__(self, cartella, pki):
        self.cartella = cartella
        self.pki = pki
        self.rete = cartella / "rete"
        self.da_mandare = cartella / "da-mandare"
        self.rete.mkdir()
        self.da_mandare.mkdir()
        self.processi = []
        self.n_file = 0

    def tak(self):
        self.porta_tak = porta_libera()
        self.eventi_tak = self.cartella / "tak.jsonl"
        self.processi.append(subprocess.Popen(
            [sys.executable, os.path.join(QUI, "fake_tak.py"), "--porta", str(self.porta_tak),
             "--cert", str(self.pki / "takserver-chain.pem"), "--chiave", str(self.pki / "takserver.key"),
             "--ca", str(self.pki / "ca-chain.pem"), "--uscita", str(self.eventi_tak), "--rilancia",
             "--da-mandare", str(self.da_mandare)], env=AMBIENTE, stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT))
        aspetta_porta(self.porta_tak)

    def nodo(self, num):
        porta = porta_libera()
        trasmessi = self.cartella / f"trasmessi-{num:08x}.jsonl"
        self.processi.append(subprocess.Popen(
            [sys.executable, os.path.join(QUI, "fake_node.py"), "--porta", str(porta), "--num", hex(num),
             "--primo-scenario", "99", "--rete", str(self.rete), "--trasmessi", str(trasmessi)],
            env=AMBIENTE, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT))
        aspetta_porta(porta)
        return porta

    def ponte(self, porta_nodo, attese, nome):
        conf = scrivi_config(self.cartella / f"{nome}.conf", self.pki, self.porta_tak, porta_nodo,
                             self.cartella)
        proc = subprocess.Popen([sys.executable, PONTE, "-c", str(conf)],
                                env=dict(AMBIENTE, MESH2TAK_ATTESE=str(attese)),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return proc

    def dal_server(self, xml):
        self.n_file += 1
        f = self.da_mandare / f"{self.n_file:03d}.xml.tmp"
        f.write_text(xml, encoding="utf-8")
        os.rename(f, str(f)[:-4])

    def in_radio(self, da, filo, canale=1):
        self.n_file += 1
        rec = {"da": da, "canale": canale, "porta": 78, "carico": filo.hex(), "id": 1000 + self.n_file}
        f = self.rete / f"{time.time():.6f}-{da:08x}-{rec['id']}.json"
        (self.cartella / "tmp.json").write_text(json.dumps(rec))
        os.rename(self.cartella / "tmp.json", f)

    def trasmissioni(self):
        """Tutto quello che i ponti hanno trasmesso in radio."""
        uscite = []
        for f in sorted(self.rete.glob("*.json")):
            rec = json.loads(f.read_text())
            if rec["da"] != CAMPO:
                uscite.append(rec)
        return uscite

    def eventi(self):
        if not self.eventi_tak.exists():
            return []
        return [json.loads(r) for r in self.eventi_tak.read_text().splitlines()
                if r.strip() and '"type"' in r and '"t-x-c-t"' not in r]

    def chiudi(self):
        for p in reversed(self.processi):
            p.terminate()
        for p in self.processi:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()


def dalla_radio(impianto):
    """Chat e oggetti portati dalla radio al server da un ponte (non posizioni, non avvisi del ponte)."""
    return [e for e in impianto.eventi() if (e.get("mesh2tak") or {}).get("radio") and not e["type"].startswith("a-")]


def ferma(ponte):
    ponte.send_signal(signal.SIGTERM)
    registro, _ = ponte.communicate(timeout=20)
    return registro


def aspetta(condizione, limite=15.0):
    fine = time.time() + limite
    while time.time() < fine:
        if condizione():
            return True
        time.sleep(0.2)
    return condizione()


@pytest.fixture
def impianto(tmp_path, pki):
    imp = Impianto(tmp_path, pki)
    yield imp
    imp.chiudi()


def test_server_radio_e_ritorno(impianto, sdk):
    impianto.tak()
    nodo = impianto.nodo(0xABCD0001)
    ponte = impianto.ponte(nodo, 0.1, "ponte")
    try:
        assert aspetta(lambda: any(e["type"] == "a-f-G-I" for e in impianto.eventi())), "ponte non partito"
        time.sleep(1)
        # dalla sala operativa: una chat, un'area disegnata, un allarme, la posizione di un telefono, una forma enorme
        impianto.dal_server(cot_fresco("geochat_simple"))
        impianto.dal_server(cot_fresco("drawing_circle"))
        impianto.dal_server(cot_fresco("emergency_911"))
        impianto.dal_server(cot_fresco("pli_full"))
        from test_unita import poligono
        impianto.dal_server(poligono(150))
        assert aspetta(lambda: len(impianto.trasmissioni()) >= 3), impianto.trasmissioni()
        time.sleep(1.5)
        uscite = impianto.trasmissioni()
        assert {(r["porta"], r["canale"], r["hop_limit"]) for r in uscite} == {(78, 1, 3)}
        tipi = sorted(sdk.decodifica(bytes.fromhex(r["carico"])).WhichOneof("payload_variant") for r in uscite)
        assert tipi == ["chat", "emergency", "shape"], tipi
        # avviso a chi ha mandato la forma troppo grande
        avvisi = [e for e in impianto.eventi() if e["type"] == "b-t-f" and e.get("marti")]
        assert len(avvisi) == 1 and avvisi[0]["marti"] == ["07-BIANCHI"]
        assert "troppo grande" in avvisi[0]["note"]
        # dal campo: un volontario manda un segnaposto e una chat via radio
        _, filo = sdk.da_xml(cot_fresco("marker_spot"))
        impianto.in_radio(CAMPO, filo)
        _, filo = sdk.da_xml(cot_fresco("geochat_dm"))
        impianto.in_radio(CAMPO, filo)
        assert aspetta(lambda: {"b-m-p-s-m", "b-t-f"} <= {e["type"] for e in dalla_radio(impianto)})
        dal_campo = dalla_radio(impianto)
        assert {e["mesh2tak"]["radio"] for e in dal_campo} == {"!1111aaaa"}
        assert len(dal_campo) == 2, "ogni oggetto arriva una volta sola"
    finally:
        registro = ferma(ponte)
    assert "Traceback" not in registro, registro
    assert "-> radio: ALLARME" in registro and "-> radio: chat di TESTNODE-01" in registro
    assert "troppo grande per un pacchetto radio" in registro
    assert "-> TAK: segnaposto «R 1» da" in registro


def test_due_ponti_senza_doppioni(impianto, sdk):
    impianto.tak()
    nodo_a = impianto.nodo(0xABCD0001)
    nodo_b = impianto.nodo(0xABCD0002)
    ponte_a = impianto.ponte(nodo_a, 0.1, "ponte-a")    # il piu' svelto
    ponte_b = impianto.ponte(nodo_b, 1.0, "ponte-b")    # attese vere
    try:
        assert aspetta(lambda: len({e["conn"] for e in impianto.eventi()}) >= 2), "i due ponti non sono partiti"
        time.sleep(1)
        # 1) la sala operativa scrive: la chat va in radio una volta sola
        impianto.dal_server(cot_fresco("geochat_broadcast"))
        assert aspetta(lambda: len(impianto.trasmissioni()) >= 1)
        time.sleep(9)   # il ponte B avrebbe trasmesso entro 8 secondi
        uscite = impianto.trasmissioni()
        assert len(uscite) == 1 and uscite[0]["da"] == 0xABCD0001, uscite
        # ... e il ponte B, che l'ha sentita in radio, non la riporta al server
        assert not [e for e in dalla_radio(impianto) if e["type"] == "b-t-f"]
        # 2) un volontario disegna un'area in campo: i due ponti la sentono, al server arriva una volta
        _, filo = sdk.da_xml(cot_fresco("drawing_polygon"))
        impianto.in_radio(CAMPO, filo)
        assert aspetta(lambda: any(e["type"] == "u-d-p" for e in impianto.eventi()))
        time.sleep(4)   # il ponte B avrebbe inoltrato entro 2,5 secondi
        poligoni = [e for e in impianto.eventi() if e["type"] == "u-d-p"]
        assert len(poligoni) == 1, poligoni
    finally:
        registro_a = ferma(ponte_a)
        registro_b = ferma(ponte_b)
    assert "Traceback" not in registro_a + registro_b, registro_a + registro_b
    assert "gia' trasmesso da" in registro_b, registro_b
    assert "gia' portato sul server da un altro ponte" in registro_b, registro_b
