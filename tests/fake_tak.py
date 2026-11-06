#!/usr/bin/env python3
"""TAK Server finto per i test: TLS con certificato client obbligatorio.

Registra ogni evento CoT ricevuto in un file JSON-lines. Come il server vero puo'
rilanciare ogni evento agli altri collegati (--rilancia) e mandare a tutti gli
eventi depositati come file .xml in una cartella (--da-mandare).
"""
import argparse
import glob
import json
import os
import queue
import socket
import ssl
import sys
import threading
import time
import xml.etree.ElementTree as ET

OFFERTA = (b'<?xml version="1.0" encoding="UTF-8"?><event version="2.0" uid="protouid" type="t-x-takp-v" '
           b'time="2026-11-01T00:00:00Z" start="2026-11-01T00:00:00Z" stale="2026-11-01T00:01:00Z" how="m-g">'
           b'<point lat="0" lon="0" hae="0" ce="999999" le="999999"/><detail><TakControl>'
           b'<TakProtocolSupport version="1"/></TakControl></detail></event>')


def evento_in_dict(xml, conn_id, cn):
    ev = ET.fromstring(xml)
    pt = ev.find("point")
    det = ev.find("detail")
    d = {k: ev.get(k) for k in ("uid", "type", "how", "time", "start", "stale")}
    d.update({"conn": conn_id, "cn": cn, "lat": float(pt.get("lat")), "lon": float(pt.get("lon")),
              "hae": float(pt.get("hae")), "ce": float(pt.get("ce")), "ricevuto": time.time()})
    if det is not None:
        c = det.find("contact")
        if c is not None:
            d["callsign"] = c.get("callsign")
            d["endpoint"] = c.get("endpoint")
        g = det.find("__group")
        if g is not None:
            d["squadra"] = g.get("name")
            d["ruolo"] = g.get("role")
        st = det.find("status")
        if st is not None:
            d["batteria"] = st.get("battery")
        r = det.find("remarks")
        if r is not None:
            d["note"] = r.text
        ch = det.find("__chat")
        if ch is not None:
            d["chat"] = dict(ch.attrib)
        segno = det.find("__mesh2tak")
        if segno is not None:
            d["mesh2tak"] = dict(segno.attrib)
        marti = det.find("marti")
        if marti is not None:
            d["marti"] = [x.get("callsign") for x in marti.findall("dest")]
        t = det.find("takv")
        if t is not None:
            d["takv"] = dict(t.attrib)
        u = det.find("uid")
        if u is not None:
            d["droid"] = u.get("Droid")
        tr = det.find("track")
        if tr is not None:
            d["track"] = dict(tr.attrib)
        ic = det.find("usericon")
        if ic is not None:
            d["icona"] = ic.get("iconsetpath")
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--porta", type=int, required=True)
    ap.add_argument("--cert", required=True)
    ap.add_argument("--chiave", required=True)
    ap.add_argument("--ca", required=True)
    ap.add_argument("--uscita", required=True)
    ap.add_argument("--chiudi-dopo", type=int, default=0, help="chiude la PRIMA connessione dopo N eventi")
    ap.add_argument("--rilancia", action="store_true", help="rilancia ogni evento agli altri collegati")
    ap.add_argument("--da-mandare", default="", help="cartella: ogni file .xml viene mandato a tutti")
    a = ap.parse_args()

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(a.cert, a.chiave)
    ctx.load_verify_locations(a.ca)
    ctx.verify_mode = ssl.CERT_REQUIRED

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", a.porta))
    srv.listen(5)
    lock = threading.Lock()
    contatore = {"conn": 0}
    aperte = {}  # conn_id -> coda di uscita (scrive solo il thread della connessione)
    print(f"fake TAK in ascolto su {a.porta}", flush=True)

    def manda(dati, tranne=None):
        with lock:
            for c, coda in aperte.items():
                if c != tranne:
                    coda.put(dati)

    def sorveglia():
        while True:
            for f in sorted(glob.glob(os.path.join(a.da_mandare, "*.xml"))):
                with open(f, "rb") as fh:
                    dati = fh.read()
                os.rename(f, f + ".inviato")
                manda(dati)
                scrivi({"mandato": os.path.basename(f)})
            time.sleep(0.1)

    def scrivi(rec):
        with lock, open(a.uscita, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

    if a.da_mandare:
        threading.Thread(target=sorveglia, daemon=True).start()

    def servi(grezzo, indirizzo):
        try:
            s = ctx.wrap_socket(grezzo, server_side=True)
        except Exception as e:  # noqa  (anche le prove di porta aperta finiscono qui)
            scrivi({"rifiutata": str(e)})
            grezzo.close()
            return
        with lock:
            contatore["conn"] += 1
            conn_id = contatore["conn"]
        cn = dict(x[0] for x in s.getpeercert()["subject"]).get("commonName")
        scrivi({"conn": conn_id, "aperta": True, "cn": cn})
        s.sendall(OFFERTA)
        uscita = queue.Queue()
        with lock:
            aperte[conn_id] = uscita
        s.settimeout(0.1)
        buf = b""
        n = 0
        try:
            while True:
                while not uscita.empty():
                    s.sendall(uscita.get_nowait())
                try:
                    dati = s.recv(65536)
                except (socket.timeout, TimeoutError):
                    continue
                if not dati:
                    break
                buf += dati
                while b"</event>" in buf:
                    pezzo, buf = buf.split(b"</event>", 1)
                    pezzo = pezzo + b"</event>"
                    inizio = pezzo.find(b"<event")
                    rec = evento_in_dict(pezzo[inizio:], conn_id, cn)
                    rec["xml"] = pezzo.decode("utf-8")
                    scrivi(rec)
                    if a.rilancia and rec["type"] != "t-x-c-t":
                        manda(pezzo[inizio:], tranne=conn_id)
                    if rec["type"] != "t-x-c-t":
                        n += 1
                    if a.chiudi_dopo and conn_id == 1 and n >= a.chiudi_dopo:
                        scrivi({"conn": conn_id, "chiusa_dal_server": True})
                        s.close()
                        return
        except Exception as e:  # noqa
            scrivi({"conn": conn_id, "errore": str(e)})
        finally:
            with lock:
                aperte.pop(conn_id, None)
            try:
                s.close()
            except OSError:
                pass

    while True:
        g, ind = srv.accept()
        threading.Thread(target=servi, args=(g, ind), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
