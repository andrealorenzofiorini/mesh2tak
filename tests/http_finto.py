"""Server HTTP(S) di prova per GitHub finto, pagina della rete finta e simili: risposte per percorso (bytes, un
oggetto da mandare in JSON, None = 404, una coppia (codice, intestazioni) senza corpo, oppure una funzione che restituisce una di queste;
risposte["*"] e' una funzione del percorso per tutti i percorsi non elencati)."""
import http.server
import json
import ssl
import subprocess
import threading

from conftest import porta_libera


class ServerFinto:
    def __init__(self, risposte=None, tls=None):
        self.risposte = risposte if risposte is not None else {}
        self.richieste = []
        srv = self

        class G(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):  # noqa: N802
                srv.richieste.append(self.path)
                r = srv.risposte.get(self.path)
                if r is None and "*" in srv.risposte:     # una funzione del percorso, per tutto il resto
                    r = srv.risposte["*"](self.path)
                elif callable(r):
                    r = r()
                if r is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if isinstance(r, tuple):            # (codice, intestazioni): un rimando o un rifiuto, senza corpo
                    self.send_response(r[0])
                    for k, v in r[1].items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                corpo = r if isinstance(r, bytes) else json.dumps(r).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(corpo)))
                self.end_headers()
                self.wfile.write(corpo)

            def do_POST(self):  # noqa: N802
                # risposte["POST /percorso"]: (codice, oggetto) oppure una funzione del corpo che li restituisce
                corpo = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                srv.spedite.append((self.path, corpo))
                r = srv.risposte.get("POST " + self.path)
                if callable(r):
                    r = r(corpo)
                codice, dati = r if r is not None else (404, {})
                testo = json.dumps(dati).encode()
                self.send_response(codice)
                self.send_header("Content-Length", str(len(testo)))
                self.end_headers()
                self.wfile.write(testo)

        self.spedite = []
        self.porta = porta_libera()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", self.porta), G)
        if tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(str(tls / "srv.pem"), str(tls / "srv.key"))
            self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.url = f"{'https' if tls else 'http'}://127.0.0.1:{self.porta}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def ferma(self):
        self.server.shutdown()
        self.server.server_close()


def crea_certificati(d):
    """Una CA di prova e un certificato per 127.0.0.1 firmato da lei (srv.pem, srv.key, ca.pem)."""
    def x(*cmd):
        subprocess.run(list(cmd), check=True, capture_output=True, cwd=d)
    x("openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-keyout", "ca.key",
      "-out", "ca.pem", "-days", "2", "-subj", "/CN=ca-di-prova", "-addext", "basicConstraints=critical,CA:TRUE",
      "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-addext", "subjectKeyIdentifier=hash")
    x("openssl", "req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-keyout", "srv.key", "-out",
      "srv.csr", "-subj", "/CN=127.0.0.1")
    (d / "ext.cnf").write_text("basicConstraints=CA:FALSE\nsubjectAltName=IP:127.0.0.1\nkeyUsage=digitalSignature\n"
                               "extendedKeyUsage=serverAuth\nauthorityKeyIdentifier=keyid\nsubjectKeyIdentifier=hash\n")
    x("openssl", "x509", "-req", "-in", "srv.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-out",
      "srv.pem", "-days", "2", "-extfile", "ext.cnf")
    return d


def somme(file):
    """SHA256SUMS come lo scrive la release: 'impronta  nome' per ogni file ({nome: contenuto})."""
    import hashlib
    return "".join(f"{hashlib.sha256(dati).hexdigest()}  {nome}\n" for nome, dati in file.items()).encode()
