# --------------------------------------------------------------------------
# Pagina di gestione del nodo nel browser, con il ponte in pausa
# --------------------------------------------------------------------------

class Pausa:
    """Il ponte lascia il nodo alla pagina di gestione e lo riprende quando la pagina ha finito.

    Il nodo serve un programma collegato alla volta: con il ponte e la pagina collegati insieme
    i messaggi in arrivo si dividono fra i due (una sola coda, MeshService::toPhoneQueue) e il
    ponte ne perde una parte.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.attiva = False
        self.nota = ""
        self.rilasciato = threading.Event()   # il ciclo principale ha staccato il nodo

    def chiedi(self, nota: str) -> bool:
        with self._lock:
            if self.attiva:
                return False
            self.attiva, self.nota = True, nota
            self.rilasciato.clear()
            return True

    def termina(self) -> bool:
        with self._lock:
            if not self.attiva:
                return False
            self.attiva = False
            return True


class _ServerPagina(http.server.ThreadingHTTPServer):
    """HTTPS con la stretta di mano TLS nel thread della richiesta, non in quello che accetta."""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, indirizzo, gestore, contesto: ssl.SSLContext):
        self.contesto = contesto
        super().__init__(indirizzo, gestore)

    def finish_request(self, request, client_address):
        request.settimeout(30)
        try:
            tls = self.contesto.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            return
        try:
            self.RequestHandlerClass(tls, client_address, self)
        finally:
            try:
                tls.close()
            except OSError:
                pass


class Gestione:
    """Gestione del nodo dal browser o dall'app Meshtastic via Wi-Fi, passando dal ponte.

    meshtasticd risponde solo al Raspberry (porte interne): la pagina (porta 9443) e l'app
    (porta 4403) passano dal ponte. Pagine, script e immagini passano sempre; quando la pagina
    usa il nodo (/api/...) o l'app si collega, il ponte va in pausa: stacca il nodo e lo riprende
    'chiusura_s' secondi dopo che la pagina tace o l'app si e' scollegata, al massimo dopo
    'pausa_massima_min'. Dopo la pausa massima la pagina riceve un rifiuto finche' non viene
    ricaricata e l'app finche' non resta zitta per 'silenzio_app_s' (l'app Meshtastic riprova da
    sola con attese fino a 5 minuti): una scheda o un'app dimenticate non tengono fermo il ponte.
    """

    SALTA = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "te", "trailer",
             "upgrade", "host", "content-length"}

    def __init__(self, cfg: Config, pausa: Pausa, tz=None):
        self.cfg = cfg
        self.pausa = pausa
        self.tz = tz
        # vuoto con la radio USB: la pagina da' solo lo stato del ponte (per la pagina della rete)
        self.nodo_pagina = cfg.gestione_nodo_pagina.strip()
        u = urllib.parse.urlsplit(self.nodo_pagina or "https://127.0.0.1:9444")
        self.nodo_https = u.scheme == "https"
        self.nodo_host = u.hostname or "127.0.0.1"
        self.nodo_porta = u.port or (443 if self.nodo_https else 80)
        self.fornisci_stato: Optional[Callable[[], dict]] = None   # /mesh2tak/stato.json
        self.radio: Optional["GestioneRadio"] = None                # /mesh2tak/radio/... (dalla 1.10.0)
        self.icone: Optional[Callable[[Optional[bytes]], Tuple[int, dict]]] = None   # /mesh2tak/icone (1.11.0)
        self.copertura: Optional[Callable[[bool, str], dict]] = None   # /mesh2tak/copertura.json (1.14.0)
        self.storico: Optional[Callable[[], dict]] = None              # /mesh2tak/storico.json (1.16.0)
        # dalla 1.13.0: comandi dalla pagina della rete (li fa mesh2tak-aggiorna, da root) e registro per la pagina
        self.file_controlla = os.path.join(cfg.accesso_cartella_stato, "controlla-adesso")
        self.file_registro = Sistema.REGISTRO
        self._ultimo_controllo = 0.0
        self._controllo_rimandato = False   # un avviso arrivato entro 5 s dal precedente: si da' dopo, non si perde
        self.chi = ""                    # chi tiene il nodo nella pausa in corso
        self._ctx_nodo = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._ctx_nodo.check_hostname = False
        self._ctx_nodo.verify_mode = ssl.CERT_NONE   # solo verso 127.0.0.1, certificato fatto da meshtasticd
        app_host, app_porta = "127.0.0.1", 4403
        if cfg.collegamento.startswith("tcp:"):
            app_host, _, porta = cfg.collegamento[4:].rpartition(":")
            app_host, app_porta = app_host or "127.0.0.1", int(porta or 4403)
        self.app_nodo = (app_host, app_porta)
        self._lock = threading.Lock()
        self.inizio = 0.0
        self.ultima_attivita = 0.0
        self.scaduta = False             # pagina: rifiuto fino al ricaricamento
        self.app_bloccata = False        # app: rifiuto finche' non tace per silenzio_app_s
        self.ultimo_tentativo_app = 0.0
        self._app: List[Tuple[socket.socket, socket.socket]] = []   # (app, nodo) collegate adesso
        self._server: Optional[_ServerPagina] = None
        self._ascolto_app: Optional[socket.socket] = None
        self._fermo = threading.Event()

    def avvia(self) -> None:
        if self.cfg.gestione_porta_pagina:
            self._avvia_pagina()
        if self.cfg.gestione_porta_app:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", self.cfg.gestione_porta_app))
            s.listen(4)
            self._ascolto_app = s
            threading.Thread(target=self._accetta_app, name="gestione-app", daemon=True).start()
        threading.Thread(target=self._sorveglia, name="gestione-pausa", daemon=True).start()

    def _avvia_pagina(self) -> None:
        gestione = self

        class Gestore(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, formato, *argomenti):  # niente righe per ogni richiesta
                pass

            def do_GET(self):  # noqa: N802
                gestione.servi(self)

            do_PUT = do_POST = do_OPTIONS = do_HEAD = do_DELETE = do_GET  # noqa: N815

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cfg.gestione_certificato, self.cfg.gestione_chiave)
        self._server = _ServerPagina(("0.0.0.0", self.cfg.gestione_porta_pagina), Gestore, ctx)
        threading.Thread(target=self._server.serve_forever, name="gestione-pagina", daemon=True).start()

    def ferma(self) -> None:
        self._fermo.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._ascolto_app is not None:
            try:
                self._ascolto_app.close()
            except OSError:
                pass
        self._chiudi_app()
        self.pausa.termina()

    def _ora(self, ts: float) -> str:
        return (datetime.fromtimestamp(ts, self.tz) if self.tz else datetime.fromtimestamp(ts)).strftime("%H:%M")

    def _pausa(self, da: str, chi: str) -> Optional[str]:
        """Chiede la pausa (gia' sotto lock) e restituisce None, oppure il perche' del rifiuto."""
        ora = time.time()
        self.ultima_attivita = ora
        if not self.pausa.attiva:
            fine = ora + self.cfg.gestione_pausa_massima_min * 60
            nota = (f"IN PAUSA: nodo gestito {chi} dalle {self._ora(ora)} (al massimo fino alle "
                    f"{self._ora(fine)}), niente radio verso il TAK Server")
            if self.pausa.chiedi(nota):
                self.inizio, self.chi = ora, chi
                log.info("Nodo gestito %s%s: gateway in pausa, il nodo passa a lei (il gateway lo riprende "
                         "%g s dopo la chiusura, al massimo tra %g minuti)", chi,
                         "" if chi.startswith("da internet") else f" da {da}", self.cfg.gestione_chiusura_s,
                         self.cfg.gestione_pausa_massima_min)
        return None

    def stato_pausa(self) -> dict:
        with self._lock:
            if not self.pausa.attiva:
                return {"attiva": False}
            return {"attiva": True, "chi": self.chi, "dal": round(self.inizio),
                    "fino_al_massimo": round(self.inizio + self.cfg.gestione_pausa_massima_min * 60)}

    @staticmethod
    def _chi(h) -> str:
        """Chi usa la pagina: dal tunnel (indirizzo del Raspberry stesso) arriva da internet, con il nome
        dell'amministratore che Authentik ha fatto entrare (lo aggiunge Caddy sul server)."""
        utente = re.sub(r"[^\w.@+-]", "", pulisci(h.headers.get("X-Authentik-Username"), 40))
        if h.client_address[0] in ("127.0.0.1", "::1") and utente:
            return f"da internet ({utente})"
        return "dal browser"

    def _aspetta_nodo(self) -> Optional[str]:
        if not self.pausa.rilasciato.wait(20):
            return "Il gateway non ha ancora lasciato il nodo: riprova tra qualche secondo."
        return None

    # ---- pagina nel browser ----

    def servi(self, h: http.server.BaseHTTPRequestHandler) -> None:
        percorso = urllib.parse.urlsplit(h.path).path
        if percorso == "/mesh2tak/stato.json":   # per la pagina della rete: non mette in pausa
            self._leggi_corpo(h)
            if h.command in ("GET", "HEAD"):
                self._stato(h)
            else:
                self._rispondi(h, 405, "Solo lettura")
            return
        if percorso == "/mesh2tak/icone":   # icone sulla mappa scelte dalla pagina della rete (dalla 1.11.0)
            self._servi_icone(h)
            return
        if percorso == "/mesh2tak/controlla":   # dalla 1.13.0: c'e' un comando dalla pagina della rete
            self._servi_controlla(h)
            return
        if percorso == "/mesh2tak/registro":    # dalla 1.13.0: il registro per la pagina della rete
            self._servi_registro(h)
            return
        if percorso == "/mesh2tak/copertura.json":   # dalla 1.14.0: la mappa della copertura
            self._servi_copertura(h)
            return
        if percorso == "/mesh2tak/storico.json":     # dalla 1.16.0: andamento di ponte e ripetitori
            self._servi_storico(h)
            return
        if percorso.startswith("/mesh2tak/radio/"):   # radio della squadra gestite dalla pagina della rete
            try:
                troppo = int(h.headers.get("Content-Length") or 0) > 16384
            except ValueError:
                troppo = True
            if troppo or self.radio is None:
                stato, dati = (413, {"errore": "richiesta troppo grande"}) if troppo else \
                    (404, {"errore": "gestione delle radio non attiva su questo gateway"})
            else:
                stato, dati = self.radio.servi(h, percorso, self._leggi_corpo(h))
            self._rispondi(h, stato, json.dumps(dati, ensure_ascii=False, separators=(",", ":")),
                           "application/json; charset=utf-8")
            return
        if not self.nodo_pagina:
            self._leggi_corpo(h)
            self._rispondi(h, 200 if percorso in ("/", "/index.html") else 404,
                           f"Gateway Meshtastic -> TAK (mesh2tak {VERSIONE}). Questo gateway usa una radio collegata via "
                           "USB: la radio si gestisce dall'app Meshtastic, via Bluetooth o collegandola a un computer.")
            return
        if h.command == "GET" and percorso in ("/", "/index.html"):
            with self._lock:
                self.scaduta = False   # pagina ricaricata: si puo' ricominciare
        if percorso.startswith("/api/") and h.command != "OPTIONS":
            with self._lock:
                if self.scaduta:
                    motivo = (f"Pausa del gateway finita ({self.cfg.gestione_pausa_massima_min:g} minuti al massimo): "
                              "il gateway ha ripreso il nodo. Ricarica la pagina per altri "
                              f"{self.cfg.gestione_pausa_massima_min:g} minuti.")
                else:
                    motivo = self._pausa(h.client_address[0], self._chi(h))
            motivo = motivo or self._aspetta_nodo()
            if motivo:
                self._leggi_corpo(h)
                self._rispondi(h, 503, motivo)
                return
        self._inoltra(h)

    @staticmethod
    def _leggi_corpo(h) -> Optional[bytes]:
        try:
            lunghezza = int(h.headers.get("Content-Length") or 0)
        except ValueError:
            lunghezza = 0
        return h.rfile.read(lunghezza) if lunghezza > 0 else None

    def _servi_icone(self, h) -> None:
        """Le manda il servizio della pagina della rete dal tunnel: si accettano solo da li' (indirizzo del
        Raspberry stesso) e con l'intestazione della rete, mai dal Wi-Fi."""
        try:
            lunghezza = int(h.headers.get("Content-Length") or 0)
        except ValueError:
            lunghezza = -1
        if lunghezza < 0 or lunghezza > 131072:
            if 0 < lunghezza <= 1 << 20:
                resta = lunghezza                # letta e buttata: la risposta arriva senza chiudere a meta'
                while resta > 0:
                    pezzo = h.rfile.read(min(resta, 65536))
                    if not pezzo:
                        break
                    resta -= len(pezzo)
            h.close_connection = True
            stato, dati = 413, {"errore": "richiesta troppo grande"}
        else:
            corpo = h.rfile.read(lunghezza) if lunghezza else None
            if h.command != "POST":
                stato, dati = 405, {"errore": "solo POST"}
            elif h.client_address[0] not in ("127.0.0.1", "::1") or h.headers.get("X-Mesh2tak") != "1":
                stato, dati = 403, {"errore": "le icone arrivano solo dalla pagina della rete"}
            elif self.icone is None:
                stato, dati = 404, {"errore": "icone non attive su questo gateway"}
            else:
                stato, dati = self.icone(corpo)
        self._rispondi(h, stato, json.dumps(dati, ensure_ascii=False, separators=(",", ":")),
                       "application/json; charset=utf-8")

    @staticmethod
    def _corpo_al_massimo(h, massimo: int) -> Tuple[Optional[bytes], bool]:
        """(corpo, troppo grande). Uno troppo grande si legge e si butta (fino a 1 MB): la risposta arriva intera."""
        try:
            lunghezza = int(h.headers.get("Content-Length") or 0)
        except ValueError:
            lunghezza = -1
        if 0 <= lunghezza <= massimo:
            return (h.rfile.read(lunghezza) if lunghezza else None), False
        resta = lunghezza if 0 < lunghezza <= 1 << 20 else 0
        while resta > 0:
            pezzo = h.rfile.read(min(resta, 65536))
            if not pezzo:
                break
            resta -= len(pezzo)
        h.close_connection = True
        return None, True

    def _servi_controlla(self, h) -> None:
        """Dalla 1.13.0 il server della rete, dal tunnel, avvisa che nella registrazione c'e' un comando per il
        Raspberry: si tocca il file che sveglia mesh2tak-aggiorna (root), che lo legge e lo fa. Non porta comandi: al
        massimo fa guardare la registrazione un po' prima dei soliti 2 minuti."""
        _, troppo = self._corpo_al_massimo(h, 4096)
        if troppo:
            stato, dati = 413, {"errore": "richiesta troppo grande"}
        elif h.command != "POST":
            stato, dati = 405, {"errore": "solo POST"}
        elif h.client_address[0] not in ("127.0.0.1", "::1") or h.headers.get("X-Mesh2tak") != "1":
            stato, dati = 403, {"errore": "solo dalla pagina della rete"}
        else:
            # il file si tocca al massimo ogni 5 secondi (l'unita' .path di systemd non va tempestata); un avviso
            # arrivato prima si da' allo scadere dei 5 secondi: il comando nuovo potrebbe non essere stato ancora letto
            ora = time.time()
            with self._lock:
                attesa = self._ultimo_controllo + 5.0 - ora
                if attesa <= 0:
                    self._ultimo_controllo = ora
                elif not self._controllo_rimandato:
                    self._controllo_rimandato = True
                    t = threading.Timer(attesa, self._controlla_rimandato)
                    t.daemon = True
                    t.start()
            stato, dati = 200, {"esito": "chiesto"}
            if attesa <= 0:
                errore = self._tocca_controlla()
                if errore:
                    stato, dati = 500, {"errore": f"non riesco ad avvisare il Raspberry ({errore})"}
        self._rispondi(h, stato, json.dumps(dati, ensure_ascii=False, separators=(",", ":")),
                       "application/json; charset=utf-8")

    def _tocca_controlla(self) -> Optional[str]:
        """Sveglia mesh2tak-aggiorna (unita' .path). None se fatto, altrimenti il motivo."""
        try:
            with open(self.file_controlla, "w", encoding="ascii") as f:
                f.write(f"{time.time():.0f}\n")
            return None
        except OSError as e:
            return str(e.strerror or e)

    def _controlla_rimandato(self) -> None:
        with self._lock:
            self._controllo_rimandato = False
            self._ultimo_controllo = time.time()
        errore = self._tocca_controlla()
        if errore:
            log.warning("avviso dei comandi della pagina non passato a mesh2tak-aggiorna: %s", errore)

    def _servi_copertura(self, h) -> None:
        """La mappa della copertura (dalla 1.14.0), solo per gli amministratori della pagina della rete: dal tunnel e
        con il nome che Authentik ha messo, perche' dice dove erano le radio. POST {"azzera": true} la ricomincia
        (dopo aver spostato l'antenna), solo dalla pagina stessa (intestazione X-Mesh2tak e stessa origine)."""
        corpo, troppo = self._corpo_al_massimo(h, 1024)
        chi = re.sub(r"[^\w.@+-]", "", pulisci(h.headers.get("X-Authentik-Username"), 40))
        if troppo:
            stato, dati = 413, {"errore": "richiesta troppo grande"}
        elif h.command not in ("GET", "HEAD", "POST"):
            stato, dati = 405, {"errore": "metodo non previsto"}
        elif h.client_address[0] not in ("127.0.0.1", "::1") or not chi:
            stato, dati = 403, {"errore": "La copertura si vede solo dalla pagina della rete."}
        elif self.copertura is None:
            stato, dati = 404, {"errore": "mappa della copertura non disponibile"}
        elif h.command == "POST":
            origine = urllib.parse.urlsplit(h.headers.get("Origin") or "").netloc
            ospite = h.headers.get("X-Forwarded-Host") or h.headers.get("Host") or ""
            try:
                richiesta = json.loads((corpo or b"").decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                richiesta = None
            if h.headers.get("X-Mesh2tak") != "1" or (origine and origine != ospite):
                stato, dati = 403, {"errore": "richiesta non valida"}
            elif not isinstance(richiesta, dict) or richiesta.get("azzera") is not True:
                stato, dati = 400, {"errore": "richiesta non valida"}
            else:
                stato, dati = 200, self.copertura(True, chi)
        else:
            stato, dati = 200, self.copertura(False, chi)
        self._rispondi(h, stato, json.dumps(dati, ensure_ascii=False, separators=(",", ":")),
                       "application/json; charset=utf-8")

    def _servi_storico(self, h) -> None:
        """L'andamento di ponte e ripetitori (dalla 1.16.0), solo per gli amministratori della pagina della rete:
        dal tunnel e con il nome che Authentik ha messo. Solo lettura."""
        _, troppo = self._corpo_al_massimo(h, 0)
        chi = re.sub(r"[^\w.@+-]", "", pulisci(h.headers.get("X-Authentik-Username"), 40))
        if troppo:
            stato, dati = 413, {"errore": "richiesta troppo grande"}
        elif h.command not in ("GET", "HEAD"):
            stato, dati = 405, {"errore": "metodo non previsto"}
        elif h.client_address[0] not in ("127.0.0.1", "::1") or not chi:
            stato, dati = 403, {"errore": "Lo storico si vede solo dalla pagina della rete."}
        elif self.storico is None:
            stato, dati = 404, {"errore": "storico non disponibile"}
        else:
            stato, dati = 200, self.storico()
        self._rispondi(h, stato, json.dumps(dati, ensure_ascii=False, separators=(",", ":")),
                       "application/json; charset=utf-8")

    def _servi_registro(self, h) -> None:
        """Il registro raccolto da mesh2tak-aggiorna (senza chiavi ne' password), solo per gli amministratori della
        pagina della rete: dal tunnel e con il nome che Authentik ha messo."""
        _, troppo = self._corpo_al_massimo(h, 0)
        chi = re.sub(r"[^\w.@+-]", "", pulisci(h.headers.get("X-Authentik-Username"), 40))
        if troppo or h.command not in ("GET", "HEAD"):
            self._rispondi(h, 405, "Solo lettura")
            return
        if h.client_address[0] not in ("127.0.0.1", "::1") or not chi:
            self._rispondi(h, 403, "Il registro si legge solo dalla pagina della rete.")
            return
        try:
            with open(self.file_registro, "rb") as f:
                corpo = f.read(4 << 20)
        except FileNotFoundError:
            self._rispondi(h, 404, "Registro non ancora raccolto: chiedilo dalla pagina della rete.")
            return
        except OSError as e:
            self._rispondi(h, 500, f"Registro non leggibile ({e.strerror or e}).")
            return
        h.send_response(200)
        h.send_header("Content-Type", "text/plain; charset=utf-8")
        h.send_header("Cache-Control", "no-store")
        h.send_header("X-Content-Type-Options", "nosniff")
        h.send_header("Content-Length", str(len(corpo)))
        h.end_headers()
        if h.command != "HEAD":
            h.wfile.write(corpo)

    def _stato(self, h) -> None:
        try:
            dati = self.fornisci_stato() if self.fornisci_stato is not None else {}
        except Exception:  # noqa
            log.exception("Stato del gateway per la pagina della rete non disponibile")
            dati = {"errore": "stato non disponibile"}
        corpo = json.dumps(dati, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        h.send_response(200)
        h.send_header("Content-Type", "application/json; charset=utf-8")
        h.send_header("Cache-Control", "no-store")
        h.send_header("Content-Length", str(len(corpo)))
        h.end_headers()
        if h.command != "HEAD":
            h.wfile.write(corpo)

    def _inoltra(self, h) -> None:
        corpo = self._leggi_corpo(h)
        intestazioni = {k: v for k, v in h.headers.items()
                        if k.lower() not in self.SALTA and not k.lower().startswith("x-authentik-")}
        if self.nodo_https:
            c = http.client.HTTPSConnection(self.nodo_host, self.nodo_porta, timeout=15, context=self._ctx_nodo)
        else:
            c = http.client.HTTPConnection(self.nodo_host, self.nodo_porta, timeout=15)
        try:
            c.request(h.command, h.path, body=corpo, headers=intestazioni)
            r = c.getresponse()
            dati = r.read()
            stato, ragione, risposta = r.status, r.reason, r.getheaders()
        except Exception as e:  # noqa
            log.debug("Pagina di gestione: il nodo non risponde (%s)", e)
            self._rispondi(h, 502, "La pagina del nodo non risponde: meshtasticd e' acceso?")
            return
        finally:
            c.close()
        h.send_response(stato, ragione)
        for k, v in risposta:
            if k.lower() not in self.SALTA:
                h.send_header(k, v)
        h.send_header("Content-Length", str(len(dati)))
        h.end_headers()
        if h.command != "HEAD":
            h.wfile.write(dati)

    @staticmethod
    def _rispondi(h, stato: int, testo: str, tipo: str = "text/plain; charset=utf-8") -> None:
        corpo = testo.encode("utf-8")
        h.send_response(stato)
        h.send_header("Content-Type", tipo)
        if tipo.startswith("text/plain"):
            h.send_header("Access-Control-Allow-Origin", "*")
        else:
            h.send_header("Cache-Control", "no-store")
        h.send_header("Content-Length", str(len(corpo)))
        h.end_headers()
        if h.command != "HEAD":
            h.wfile.write(corpo)

    # ---- app Meshtastic via Wi-Fi ----

    def _accetta_app(self) -> None:
        while not self._fermo.is_set():
            try:
                app, indirizzo = self._ascolto_app.accept()
            except OSError:
                return
            threading.Thread(target=self._servi_app, args=(app, indirizzo[0]), name="gestione-app-1",
                             daemon=True).start()

    def _servi_app(self, app: socket.socket, da: str) -> None:
        # L'app parla per prima (chiede la configurazione del nodo): un collegamento muto, come una
        # prova della porta, non ferma il ponte.
        try:
            app.settimeout(30)
            primi = app.recv(4096)
        except OSError:
            primi = b""
        if not primi:
            self._chiudi(app)
            return
        with self._lock:
            ora = time.time()
            if self.app_bloccata and ora - self.ultimo_tentativo_app < self.cfg.gestione_silenzio_app_s:
                self.ultimo_tentativo_app = ora
                motivo = "bloccata"
            else:
                if self.app_bloccata:
                    log.info("L'app Meshtastic puo' di nuovo collegarsi al nodo")
                self.app_bloccata = False
                motivo = self._pausa(da, "dall'app Meshtastic")
        if motivo:
            log.debug("App Meshtastic da %s rifiutata: pausa massima appena finita", da)
            self._chiudi(app)
            return
        if self._aspetta_nodo():
            self._chiudi(app)
            return
        try:
            nodo = socket.create_connection(self.app_nodo, timeout=10)
            nodo.sendall(primi)
        except OSError as e:
            log.warning("App Meshtastic da %s: il nodo non risponde (%s)", da, e)
            self._chiudi(app)
            return
        with self._lock:
            vecchie, self._app = self._app, [(app, nodo)]   # come il nodo: l'ultima app collegata vince
        for a, n in vecchie:
            self._chiudi(a, n)
        try:
            self._travasa(app, nodo)
        finally:
            self._chiudi(app, nodo)
            with self._lock:
                self._app = [x for x in self._app if x[0] is not app]
                self.ultima_attivita = time.time()
            log.info("App Meshtastic da %s scollegata dal nodo", da)

    @staticmethod
    def _travasa(a: socket.socket, b: socket.socket) -> None:
        import selectors
        a.settimeout(None)
        b.settimeout(None)
        sel = selectors.DefaultSelector()
        sel.register(a, selectors.EVENT_READ, b)
        sel.register(b, selectors.EVENT_READ, a)
        try:
            while True:
                for chiave, _ in sel.select(timeout=1.0):
                    dati = chiave.fileobj.recv(4096)
                    if not dati:
                        return
                    chiave.data.sendall(dati)
        except (OSError, ValueError):
            return
        finally:
            sel.close()

    @staticmethod
    def _chiudi(*prese: socket.socket) -> None:
        for p in prese:
            try:
                p.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                p.close()
            except OSError:
                pass

    def _chiudi_app(self) -> None:
        with self._lock:
            aperte, self._app = self._app, []
        for a, n in aperte:
            self._chiudi(a, n)

    # ---- fine della pausa ----

    def _sorveglia(self) -> None:
        while not self._fermo.wait(1.0):
            self.controlla()

    def controlla(self, ora: Optional[float] = None) -> None:
        ora = time.time() if ora is None else ora
        chiudi_app = False
        with self._lock:
            if not self.pausa.attiva:
                return
            if ora - self.inizio >= self.cfg.gestione_pausa_massima_min * 60:
                self.scaduta = self.app_bloccata = True
                self.ultimo_tentativo_app = ora
                chiudi_app = True
                messaggio = (f"Pausa massima di {self.cfg.gestione_pausa_massima_min:g} minuti finita: il gateway "
                             "riprende il nodo (per continuare: ricarica la pagina, oppure chiudi l'app e "
                             f"riaprila dopo {self.cfg.gestione_silenzio_app_s / 60:g} minuti)")
            elif not self._app and ora - self.ultima_attivita >= self.cfg.gestione_chiusura_s:
                messaggio = (f"Gestione del nodo finita (nessuna richiesta da {self.cfg.gestione_chiusura_s:g} s): "
                             "il gateway riprende il nodo")
            else:
                return
            self.pausa.termina()
        if chiudi_app:
            self._chiudi_app()
        log.info(messaggio)


