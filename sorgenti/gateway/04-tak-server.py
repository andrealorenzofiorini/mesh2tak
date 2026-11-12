# --------------------------------------------------------------------------
# Connessione al TAK Server
# --------------------------------------------------------------------------

def contesto_tls(cfg: Config) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cfg.certificato, cfg.chiave)
    ctx.check_hostname = False  # il certificato di TAK Server spesso non riporta il nome pubblico
    if cfg.ca_server:
        ctx.load_verify_locations(cfg.ca_server)
        ctx.verify_mode = ssl.CERT_REQUIRED
        if hasattr(ssl, "VERIFY_X509_PARTIAL_CHAIN"):
            ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    else:
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _keepalive(s: socket.socket) -> None:
    s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for nome, valore in (("TCP_KEEPIDLE", 60), ("TCP_KEEPINTVL", 20), ("TCP_KEEPCNT", 3)):
        if hasattr(socket, nome):
            try:
                s.setsockopt(socket.IPPROTO_TCP, getattr(socket, nome), valore)
            except OSError:
                pass


AVVISI_CERTIFICATO = ("certificate required", "bad certificate", "unknown ca", "certificate unknown",
                      "certificate revoked", "certificate expired", "access denied", "handshake failure",
                      "unsupported certificate")


def spiega_errore_tak(e: BaseException, host: str, porta: int) -> Tuple[str, bool]:
    """Messaggio comprensibile + True se riguarda i certificati (attesa piu' lunga)."""
    if isinstance(e, socket.gaierror):
        return f"non trovo l'indirizzo {host}: il Raspberry e' collegato a internet?", False
    if isinstance(e, ConnectionRefusedError):
        return f"{host}:{porta} rifiuta la connessione (porta chiusa o TAK Server spento)", False
    if isinstance(e, (socket.timeout, TimeoutError)):
        return f"{host}:{porta} non risponde (rete assente o firewall)", False
    if isinstance(e, ssl.SSLCertVerificationError):
        return ("il certificato del TAK Server non corrisponde alla CA salvata. Se il server e' stato "
                "reinstallato o la CA ruotata, rifai l'installazione con un pacchetto nuovo"), True
    if isinstance(e, ssl.SSLError):
        testo = (getattr(e, "reason", None) or str(e)).replace("_", " ").lower()
        if any(a in testo for a in AVVISI_CERTIFICATO):
            return ("il TAK Server ha rifiutato il certificato del gateway (" + testo + "). Controlla nella "
                    "console infra-TAK che il certificato esista e non sia revocato"), True
        return "errore TLS: " + testo, False
    if isinstance(e, ConnectionError) and "chiusa dal server" in str(e):
        return str(e), False
    return f"{type(e).__name__}: {e}", False


class ClienteTAK:
    """Una connessione TLS verso il TAK Server, con riconnessione automatica.

    Tiene l'ultima versione di ogni posizione (per UID): se la connessione cade,
    alla riconnessione rimanda quelle ancora valide invece di accumulare code.
    Chat e oggetti invece partono una volta sola (se la connessione e' giu', aspettano).
    Gli eventi che arrivano dal server vanno a 'su_evento'.
    """

    PING_S = 300
    ATTESA_RETE = (15.0, 300.0)      # attesa minima e massima dopo errori di rete
    ATTESA_CERT = (120.0, 600.0)     # dopo un rifiuto del certificato: piano, per non farsi bannare
    ATTESA_DOPO_CADUTA = 5.0         # se la connessione era stabile
    DURATA_STABILE = 120.0
    MAX_UNA_VOLTA = 500
    MAX_IN_ARRIVO = 2 * 1024 * 1024

    def __init__(self, cfg: Config, uid_ponte: str = "mesh2tak"):
        self.host = cfg.tak_host
        self.porta = cfg.tak_porta
        self.ctx = contesto_tls(cfg)
        self.uid_ponte = uid_ponte
        self._lock = threading.Lock()
        self._sveglia = threading.Event()
        self._fermo = threading.Event()
        self._ultimi: Dict[str, Tuple[bytes, float]] = {}
        self._da_inviare: "OrderedDict[str, None]" = OrderedDict()
        self._una_volta: "deque[Tuple[bytes, float]]" = deque()
        self._in_arrivo = bytearray()
        self._sock: Optional[ssl.SSLSocket] = None
        self._ultimo_ping = 0.0
        self.collegato = threading.Event()
        self.collegato_dal: Optional[float] = None
        self.ultimo_errore: Optional[str] = None
        self.inviati = 0
        self.ricevuti = 0
        self.su_evento: Optional[Callable[[bytes], None]] = None
        self._thread = threading.Thread(target=self._ciclo, name="tak", daemon=True)
        if cfg.ca_server == "":
            log.warning("Nessuna CA del TAK Server configurata: connessione cifrata ma senza verifica del server")

    def avvia(self) -> None:
        self._thread.start()

    def ferma(self) -> None:
        self._fermo.set()
        self._sveglia.set()
        self._thread.join(timeout=10)
        self._chiudi()

    def invia(self, uid: str, xml: bytes, stale: float) -> None:
        with self._lock:
            self._ultimi[uid] = (xml, stale)
            self._da_inviare[uid] = None
            self._da_inviare.move_to_end(uid)
        self._sveglia.set()

    def invia_una_volta(self, xml: bytes, scadenza: float) -> None:
        """Per chat e oggetti: partono una volta, anche dopo una caduta della connessione."""
        with self._lock:
            self._una_volta.append((xml, scadenza))
            while len(self._una_volta) > self.MAX_UNA_VOLTA:
                self._una_volta.popleft()
        self._sveglia.set()

    def _connetti(self) -> ssl.SSLSocket:
        grezzo = socket.create_connection((self.host, self.porta), timeout=20)
        try:
            _keepalive(grezzo)
            s = self.ctx.wrap_socket(grezzo, server_hostname=self.host)
        except BaseException:
            grezzo.close()
            raise
        s.settimeout(20)
        return s

    def _chiudi(self) -> None:
        s, self._sock = self._sock, None
        self.collegato.clear()
        self.collegato_dal = None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass

    def _prossima_attesa(self, attesa: float, cert: bool) -> float:
        minima, massima = self.ATTESA_CERT if cert else self.ATTESA_RETE
        return min(max(attesa * 2, minima), massima)

    def _ciclo(self) -> None:
        attesa = 0.0
        while not self._fermo.is_set():
            if attesa and self._fermo.wait(attesa):
                break
            try:
                self._sock = self._connetti()
            except Exception as e:  # noqa: qualunque errore di rete/TLS
                motivo, cert = spiega_errore_tak(e, self.host, self.porta)
                attesa = self._prossima_attesa(attesa, cert)
                self.ultimo_errore = motivo
                log.error("TAK Server: %s. Riprovo tra %d s", motivo, attesa)
                continue
            inizio = time.time()
            self._in_arrivo.clear()
            self.collegato_dal, self.ultimo_errore = inizio, None
            self.collegato.set()
            log.info("Collegato al TAK Server %s:%d (%s)", self.host, self.porta, self._sock.version())
            with self._lock:
                ora = time.time()
                for uid, (_, stale) in list(self._ultimi.items()):
                    if stale > ora:
                        self._da_inviare[uid] = None
            self._ultimo_ping = time.time()
            try:
                while not self._fermo.is_set():
                    self._servi()
            except Exception as e:  # noqa
                motivo, cert = spiega_errore_tak(e, self.host, self.porta)
                durata = time.time() - inizio
                self._chiudi()
                self.ultimo_errore = motivo
                if durata > self.DURATA_STABILE:
                    attesa = self.ATTESA_DOPO_CADUTA
                else:
                    attesa = self._prossima_attesa(attesa, cert)
                log.error("TAK Server: connessione interrotta (%s). Riprovo tra %d s", motivo, attesa)
        self._chiudi()

    def _leggi(self, s: ssl.SSLSocket) -> None:
        """Legge quello che manda il server (eventi della squadra, pong, offerta di protocollo).
        Lettura non bloccante: in TLS 1.3 arrivano anche messaggi senza dati (ticket di
        sessione) e una recv bloccante resterebbe ferma fino al timeout."""
        s.settimeout(0.0)
        try:
            while True:
                try:
                    dati = s.recv(65536)
                except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError):
                    break
                if not dati:
                    raise ConnectionError("chiusa dal server")
                self._in_arrivo += dati
                if len(self._in_arrivo) > self.MAX_IN_ARRIVO:
                    log.warning("TAK Server: dati non riconosciuti come eventi CoT, li scarto")
                    self._in_arrivo.clear()
                for evento in estrai_eventi(self._in_arrivo):
                    self.ricevuti += 1
                    if self.su_evento is not None:
                        try:
                            self.su_evento(evento)
                        except Exception:  # noqa
                            log.exception("Errore inatteso con un evento dal TAK Server")
        finally:
            s.settimeout(20)

    def _servi(self) -> None:
        self._sveglia.wait(timeout=0.5)
        self._sveglia.clear()
        s = self._sock
        if s is None:
            raise ConnectionError("socket assente")
        self._leggi(s)
        ora = time.time()
        with self._lock:
            uids = list(self._da_inviare.keys())
            self._da_inviare.clear()
            for uid in [u for u, (_, st) in self._ultimi.items() if st <= ora]:
                del self._ultimi[uid]
        for i, uid in enumerate(uids):
            voce = self._ultimi.get(uid)
            if voce is None or voce[1] <= ora:
                continue
            try:
                s.sendall(voce[0])
            except Exception:
                with self._lock:
                    for altro in uids[i:]:
                        self._da_inviare[altro] = None
                raise
            self.inviati += 1
        while True:
            with self._lock:
                if not self._una_volta:
                    break
                xml, scadenza = self._una_volta.popleft()
            if scadenza <= ora:
                continue
            try:
                s.sendall(xml)
            except Exception:
                with self._lock:
                    self._una_volta.appendleft((xml, scadenza))
                raise
            self.inviati += 1
        if ora - self._ultimo_ping >= self.PING_S:
            s.sendall(cot_ping(self.uid_ponte, ora))
            self._ultimo_ping = ora


def prova_tak(cfg: Config) -> Tuple[bool, str]:
    """Verifica che il TAK Server accetti il certificato del ponte."""
    try:
        ctx = contesto_tls(cfg)
    except (ssl.SSLError, OSError) as e:
        return False, f"certificato o chiave del gateway non leggibili: {e}"
    try:
        grezzo = socket.create_connection((cfg.tak_host, cfg.tak_porta), timeout=15)
    except Exception as e:  # noqa
        return False, spiega_errore_tak(e, cfg.tak_host, cfg.tak_porta)[0]
    try:
        s = ctx.wrap_socket(grezzo, server_hostname=cfg.tak_host)
        versione = s.version()
        s.settimeout(4)
        try:
            dati = s.recv(4096)
            if not dati:
                return False, ("il server ha chiuso subito la connessione: il certificato forse non ha "
                               "permessi su nessun canale")
        except (socket.timeout, TimeoutError):
            pass
        s.close()
        return True, f"collegamento riuscito ({versione}), certificato accettato"
    except Exception as e:  # noqa
        return False, spiega_errore_tak(e, cfg.tak_host, cfg.tak_porta)[0]
    finally:
        try:
            grezzo.close()
        except OSError:
            pass


