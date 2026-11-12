# --------------------------------------------------------------------------
# Collegamento al nodo Meshtastic
# --------------------------------------------------------------------------

def _classe_tcp():
    import meshtastic.tcp_interface as ti

    class TCPSenzaAutoRiconnessione(ti.TCPInterface):
        """Il nodo accetta un solo client TCP alla volta: quando si collega un'altra
        app, chiude la connessione precedente. La libreria si ricollegherebbe dopo
        1 secondo, buttando fuori l'altra app in un ping-pong continuo. Qui la
        connessione resta chiusa e decide il ciclo principale quando riprovare."""

        def _reconnect(self):  # noqa: N802 (nome della libreria)
            self._wantExit = True

        def sendHeartbeat(self):  # noqa: N802
            try:
                super().sendHeartbeat()
            except Exception as e:  # noqa
                log.debug("heartbeat non inviato: %s", e)

    return TCPSenzaAutoRiconnessione


def descrivi_collegamento(collegamento: str) -> str:
    tipo, _, resto = collegamento.partition(":")
    if tipo == "tcp":
        return f"meshtasticd su {resto or '127.0.0.1:4403'}"
    return f"radio USB {resto or '(ricerca automatica)'}"


def apri_interfaccia(cfg):
    """cfg puo' essere la Config del ponte o direttamente la stringa di collegamento."""
    collegamento = cfg if isinstance(cfg, str) else cfg.collegamento
    tipo, _, resto = collegamento.partition(":")
    if tipo == "tcp":
        host, porta = resto or "127.0.0.1", 4403
        if ":" in host:
            host, _, testo_porta = host.rpartition(":")
            porta = int(testo_porta)
        return _classe_tcp()(hostname=host or "127.0.0.1", portNumber=porta, timeout=90)
    import meshtastic.serial_interface as si
    dev = None if resto in ("", "auto") else resto
    if dev is None:
        # Senza percorso la libreria, se non trova la radio, ripiega in silenzio su TCP
        # e con piu' porte chiude il programma: meglio scegliere qui, con un errore chiaro.
        import glob
        trovati = sorted(glob.glob("/dev/serial/by-id/*"))
        if not trovati:
            raise FileNotFoundError("No such file: nessuna radio USB collegata")
        if len(trovati) > 1:
            raise OSError("piu' dispositivi USB collegati, indica quale in 'collegamento': " + ", ".join(trovati))
        dev = trovati[0]
    if not os.path.exists(dev):
        raise FileNotFoundError(f"No such file: {dev}")
    return si.SerialInterface(devPath=dev, timeout=90)


def spiega_errore_nodo(e: BaseException, cfg) -> str:
    dove = descrivi_collegamento(cfg if isinstance(cfg, str) else cfg.collegamento)
    if isinstance(e, ConnectionRefusedError):
        return f"{dove}: connessione rifiutata (meshtasticd e' avviato?)"
    if isinstance(e, (socket.timeout, TimeoutError)):
        return f"{dove}: nessuna risposta"
    testo = str(e)
    if "Timed out waiting for interface config" in testo:
        return f"{dove}: il nodo non ha inviato la sua configurazione in tempo"
    if "could not open port" in testo or "No such file" in testo:
        return (f"{dove}: radio non trovata. Controlla che sia collegata con un cavo USB dati "
                "(i cavi solo di ricarica non bastano) e che nessun altro programma la stia usando")
    if "Resource temporarily unavailable" in testo or "Could not exclusively lock" in testo:
        return f"{dove}: la radio e' gia' usata da un altro programma"
    if "piu' dispositivi USB" in testo:
        return f"{dove}: {testo}"
    return f"{dove}: {type(e).__name__}: {testo}"


