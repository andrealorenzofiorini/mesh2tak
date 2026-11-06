"""Pagina della rete finta per la prova dell'installatore: nessuna registrazione (404) e le richieste di entrare
accettate in attesa (202), annotate in un file. HTTPS con un certificato della PKI di prova. Con --registrazione, a
mesh2tak-aggiorna (e solo a lui: il ponte resta in attesa) da' quel file come registrazione, con l'aggiornamento chiesto."""
import os
import argparse
import http.server
import json
import ssl

ap = argparse.ArgumentParser()
ap.add_argument("--porta", type=int, required=True)
ap.add_argument("--cert", required=True)
ap.add_argument("--chiave", required=True)
ap.add_argument("--uscita", required=True)
ap.add_argument("--registrazione")
a = ap.parse_args()


class Gestore(http.server.BaseHTTPRequestHandler):
    def log_message(self, *x):
        pass

    def _manda(self, codice, dati):
        corpo = json.dumps(dati).encode()
        self.send_response(codice)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def do_GET(self):  # noqa: N802
        with open(a.uscita, "a") as f:
            f.write(json.dumps({"metodo": "GET", "percorso": self.path,
                                "agente": self.headers.get("User-Agent", "")}) + "\n")
        if (a.registrazione and os.path.exists(a.registrazione) and self.path.startswith("/accesso/")
                and self.headers.get("User-Agent", "").startswith("mesh2tak-aggiorna/")):
            with open(a.registrazione) as f:
                self._manda(200, json.load(f))
            return
        self._manda(404, {})

    def do_POST(self):  # noqa: N802
        corpo = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        with open(a.uscita, "a") as f:
            f.write(json.dumps({"metodo": "POST", "percorso": self.path, "corpo": corpo}) + "\n")
        self._manda(202, {"esito": "in_attesa", "controllo": "?", "sostituisce": False})


server = http.server.ThreadingHTTPServer(("127.0.0.1", a.porta), Gestore)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(a.cert, a.chiave)
server.socket = ctx.wrap_socket(server.socket, server_side=True)
server.serve_forever()
