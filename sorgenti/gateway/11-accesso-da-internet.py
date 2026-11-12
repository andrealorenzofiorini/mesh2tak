# --------------------------------------------------------------------------
# Accesso da internet: tunnel SSH verso il server della rete Meshtastic
# --------------------------------------------------------------------------

def impronta_chiave(riga: str) -> str:
    """Impronta (sha256 esadecimale) di una chiave pubblica SSH: il server pubblica con questo nome
    la registrazione del ponte, cosi' la trova solo chi ha la chiave."""
    import base64
    import binascii
    parti = riga.split()
    if len(parti) < 2:
        raise ValueError("chiave pubblica non valida")
    try:
        blob = base64.b64decode(parti[1], validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("chiave pubblica non valida")
    return hashlib.sha256(blob).hexdigest()


CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def codice_controllo(impronta: str) -> str:
    """Sei caratteri dall'impronta della chiave, calcolati come sul server: l'installatore li mostra e la
    pagina della rete li mostra accanto alla richiesta, cosi' chi approva sa che e' questo Raspberry."""
    n = int(impronta[:8], 16) >> 2
    c = "".join(CROCKFORD[(n >> (5 * i)) & 31] for i in range(5, -1, -1))
    return f"{c[:3]}-{c[3:]}"


class AccessoEsterno:
    """Pagina di gestione e stato del ponte raggiungibili da internet, dalla pagina della rete Meshtastic.

    E' il Raspberry ad aprire un tunnel SSH verso il server (a casa non si apre nessuna porta): sul server
    la porta assegnata al ponte porta alla pagina del ponte (9443), dietro Authentik. L'utente del ponte
    sul server puo' solo tenere aperto quel tunnel. Prima di collegarsi il ponte legge via HTTPS la sua
    registrazione (utente, porta e chiave del server): finche' il server non l'ha pubblicata non bussa
    con una chiave sconosciuta, che fail2ban sul server punirebbe bloccando l'indirizzo di casa.
    Senza registrazione manda alla pagina della rete la richiesta di entrare (nome e chiave pubblica, ogni
    ora): un amministratore la approva sulla pagina, confrontando il codice di controllo.
    """

    ATTESA_REGISTRAZIONE = (60.0, 300.0)   # ogni minuto per due ore, poi ogni 5 minuti
    VELOCE_S = 7200.0
    RICHIESTA_OGNI_S = 3600.0               # la richiesta si ripete ogni ora finche' non e' approvata
    RICHIESTA_RIPROVA_S = 300.0             # pagina della rete che non riceve le richieste
    ATTESA_RETE = (15.0, 300.0)
    ATTESA_RIFIUTO_S = 900.0                # chiave rifiutata: piano, per stare lontani da fail2ban
    ATTESA_PORTA_OCCUPATA_S = 60.0          # sul server il tunnel di prima scade entro 90 s
    DURATA_STABILE_S = 120.0
    CONFERMA_S = 3.0                         # vivo cosi' tanto dopo l'accesso: tunnel aperto

    RE_UTENTE = re.compile(r"^ponte-[a-z0-9-]{1,30}$")
    RE_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
    RE_CHIAVE_HOST = re.compile(r"^(ssh-ed25519|ecdsa-sha2-nistp(?:256|384|521)|rsa-sha2-(?:256|512)|ssh-rsa) "
                                r"[A-Za-z0-9+/]+={0,3}$")

    def __init__(self, cfg: Config, porta_locale: int):
        self.cfg = cfg
        self.porta_locale = porta_locale
        self.rete = cfg.accesso_rete.rstrip("/")
        with open(cfg.accesso_chiave + ".pub", encoding="utf-8") as f:
            self.chiave_pubblica = f.read().strip()
        self.impronta = impronta_chiave(self.chiave_pubblica)
        self.controllo = codice_controllo(self.impronta)
        self.url_registrazione = f"{self.rete}/accesso/{self.impronta}.json"
        self.macchina = (re.sub(r"[^A-Za-z0-9._-]", "-", socket.gethostname() or "")[:63].strip(".-")
                         or "raspberry")
        self.known_hosts = os.path.join(cfg.accesso_cartella_stato, "server-rete.known_hosts")
        self.ssh = shutil.which("ssh") or "/usr/bin/ssh"
        self.contesto_https = ssl.create_default_context()
        self._lock = threading.Lock()
        self._fermo = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self._thread = threading.Thread(target=self._ciclo, name="accesso-esterno", daemon=True)
        self.collegato = False
        self.dal: Optional[float] = None
        self.indirizzo: Optional[str] = None
        self.messaggio = "in avvio"
        self.richiesta: Optional[str] = None    # esito dell'ultima richiesta alla pagina della rete
        self._ultimo_messaggio = ""

    def avvia(self) -> None:
        self._thread.start()

    def ferma(self) -> None:
        self._fermo.set()
        with self._lock:
            proc = self._proc
        if proc is not None:
            self._termina(proc)
        self._thread.join(timeout=10)

    def stato(self) -> dict:
        with self._lock:
            return {"collegato": self.collegato, "dal": round(self.dal) if self.dal else None,
                    "indirizzo": self.indirizzo, "messaggio": self.messaggio, "controllo": self.controllo,
                    "richiesta": self.richiesta}

    def _segna(self, collegato: bool, messaggio: str, livello: int = logging.INFO) -> None:
        with self._lock:
            if collegato and not self.collegato:
                self.dal = time.time()
            elif not collegato:
                self.dal = None
            self.collegato, self.messaggio = collegato, messaggio
        if messaggio != self._ultimo_messaggio:   # lo stesso avviso una volta sola, non a ogni tentativo
            log.log(livello, "Accesso da internet: %s", messaggio)
            self._ultimo_messaggio = messaggio

    # ---- registrazione pubblicata dal server ----

    def _registrazione(self) -> Optional[dict]:
        """La registrazione del ponte, None se il server non l'ha (ancora) pubblicata."""
        apri = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                           urllib.request.HTTPSHandler(context=self.contesto_https))
        richiesta = urllib.request.Request(self.url_registrazione, headers={
            "User-Agent": f"mesh2tak/{VERSIONE}", "Cache-Control": "no-cache"})
        try:
            with apri.open(richiesta, timeout=20) as r:
                dati = json.loads(r.read(65536).decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise OSError(f"la pagina della rete risponde {e.code}")
        return self.valida(dati)

    # ---- richiesta di entrare nella rete ----

    def _richiesta(self) -> Tuple[int, dict]:
        """Manda alla pagina della rete nome e chiave pubblica del ponte: (codice HTTP, risposta)."""
        corpo = json.dumps({"formato": 1, "nome": self.cfg.accesso_nome, "chiave": self.chiave_pubblica,
                            "macchina": self.macchina, "versione": VERSIONE}).encode("utf-8")
        apri = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                           urllib.request.HTTPSHandler(context=self.contesto_https))
        richiesta = urllib.request.Request(f"{self.rete}/accesso/richiesta", data=corpo, method="POST", headers={
            "User-Agent": f"mesh2tak/{VERSIONE}", "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with apri.open(richiesta, timeout=20) as r:
                codice, testo = r.status, r.read(65536)
        except urllib.error.HTTPError as e:
            codice, testo = e.code, e.read(65536)
        try:
            dati = json.loads(testo.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            dati = {}
        return codice, dati if isinstance(dati, dict) else {}

    def _manda_richiesta(self) -> Tuple[str, int, float]:
        """(messaggio, livello del registro, fra quanto ripetere la richiesta)."""
        c = self.controllo
        try:
            codice, d = self._richiesta()
        except Exception as e:  # noqa: rete, DNS, certificato
            self.richiesta = "non_mandata"
            return (f"richiesta alla pagina della rete non mandata ({e}): riprovo tra 5 minuti", logging.WARNING,
                    self.RICHIESTA_RIPROVA_S)
        errore = re.sub(r"\s+", " ", str(d.get("errore") or ""))[:300]
        if codice == 200:
            self.richiesta = "registrato"
            return "approvato sulla pagina della rete: leggo la registrazione", logging.INFO, self.RICHIESTA_OGNI_S
        if codice == 202:
            self.richiesta = "in_attesa"
            extra = (f". C'e' gia' un gateway '{self.cfg.accesso_nome}' nella rete: sulla pagina va scelto 'Sostituisci'"
                     if d.get("sostituisce") else "")
            return (f"in attesa di approvazione sulla pagina della rete {self.rete} (codice di controllo {c}){extra}",
                    logging.INFO, self.RICHIESTA_OGNI_S)
        if codice == 403:
            self.richiesta = "rifiutata"
            return (f"{errore or 'richiesta rifiutata sulla pagina della rete'}: per farlo rientrare, sulla pagina "
                    f"'Lascia rientrare' (codice di controllo {c}). Riprovo ogni ora", logging.WARNING,
                    self.RICHIESTA_OGNI_S)
        if codice == 429:
            self.richiesta = "troppe"
            return (f"la pagina della rete non accetta altre richieste adesso ({errore or 'troppe richieste'}): "
                    "riprovo tra un'ora", logging.WARNING, self.RICHIESTA_OGNI_S)
        if codice == 400:
            self.richiesta = "non_valida"
            return (f"la pagina della rete non accetta la richiesta ({errore or 'non valida'}): rilancia "
                    "l'installatore con --accesso-esterno", logging.ERROR, self.RICHIESTA_OGNI_S)
        if codice in (404, 405, 501, 502, 503, 504):
            self.richiesta = "server_da_preparare"
            return (f"la pagina della rete non riceve ancora le richieste dei gateway (risponde {codice}): sul server, "
                    "una volta sola,  sudo bash rete-meshtastic.sh installa  (riprovo tra 5 minuti)",
                    logging.WARNING, self.RICHIESTA_RIPROVA_S)
        self.richiesta = "errore"
        return (f"la pagina della rete risponde {codice} alla richiesta" + (f" ({errore})" if errore else "")
                + ": riprovo tra 5 minuti", logging.WARNING, self.RICHIESTA_RIPROVA_S)

    @classmethod
    def valida(cls, d) -> dict:
        if not isinstance(d, dict):
            raise ValueError("registrazione del server non valida")
        utente, porta = str(d.get("utente", "")), d.get("porta")
        host, porta_ssh = str(d.get("ssh_host", "")), d.get("ssh_porta", 22)
        chiavi = d.get("chiavi_host")
        if (not cls.RE_UTENTE.match(utente) or not isinstance(porta, int) or not 1024 <= porta <= 65535
                or not cls.RE_HOST.match(host) or not isinstance(porta_ssh, int) or not 1 <= porta_ssh <= 65535
                or not isinstance(chiavi, list) or not chiavi
                or not all(isinstance(c, str) and cls.RE_CHIAVE_HOST.match(c.strip()) for c in chiavi)):
            raise ValueError("registrazione del server non valida")
        indirizzo = str(d.get("indirizzo") or "")
        if not re.match(r"^https://[A-Za-z0-9.-]+(?::\d+)?/?$", indirizzo):
            indirizzo = ""
        return {"utente": utente, "porta": porta, "host": host, "porta_ssh": porta_ssh,
                "chiavi": [c.strip() for c in chiavi], "indirizzo": indirizzo or None}

    def _scrivi_known_hosts(self, reg: dict) -> None:
        # Le chiavi del server arrivano con la registrazione, via HTTPS con certificato valido:
        # il primo collegamento SSH non si fida alla cieca.
        os.makedirs(os.path.dirname(self.known_hosts), exist_ok=True)
        nuovo = self.known_hosts + ".nuovo"
        with open(nuovo, "w", encoding="ascii") as f:
            f.writelines(f"mesh2tak-rete {c}\n" for c in reg["chiavi"])
        os.replace(nuovo, self.known_hosts)

    # ---- tunnel ----

    def comando(self, reg: dict) -> List[str]:
        return [self.ssh, "-F", "/dev/null", "-N", "-T", "-i", self.cfg.accesso_chiave,
                "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none", "-o", "BatchMode=yes",
                "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
                "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={self.known_hosts}",
                "-o", "GlobalKnownHostsFile=/dev/null", "-o", "HostKeyAlias=mesh2tak-rete",
                "-o", "UpdateHostKeys=no", "-o", "CheckHostIP=no",
                "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
                "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=20", "-o", "LogLevel=VERBOSE",
                "-p", str(reg["porta_ssh"]),
                "-R", f"127.0.0.1:{reg['porta']}:127.0.0.1:{self.porta_locale}",
                f"{reg['utente']}@{reg['host']}"]

    @staticmethod
    def classifica(righe: List[str]) -> str:
        testo = "\n".join(righe).lower()
        if "remote port forwarding failed" in testo:
            return "porta"
        if ("host key verification failed" in testo or "remote host identification has changed" in testo
                or "host key is known" in testo):
            return "chiave_server"
        if "permission denied" in testo or "too many authentication failures" in testo:
            return "rifiuto"
        return "rete"

    @staticmethod
    def _termina(proc: subprocess.Popen) -> None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa
            try:
                proc.kill()
            except Exception:  # noqa
                pass

    def _tunnel(self, reg: dict) -> Tuple[str, float, bool, List[str]]:
        """Tiene aperto il tunnel finche' regge: (esito, durata, era aperto, ultime righe di ssh)."""
        righe: "deque[str]" = deque(maxlen=20)
        autenticato = [0.0]
        try:
            proc = subprocess.Popen(self.comando(reg), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, text=True, errors="replace")
        except OSError as e:
            return "rete", 0.0, False, [f"ssh non si avvia: {e}"]
        with self._lock:
            self._proc = proc

        def leggi():
            for riga in proc.stderr:
                riga = riga.strip()
                if riga:
                    righe.append(riga)
                    if riga.startswith("Authenticated to") and not autenticato[0]:
                        autenticato[0] = time.time()
                    log.debug("ssh: %s", riga)

        lettore = threading.Thread(target=leggi, name="accesso-ssh", daemon=True)
        lettore.start()
        inizio, aperto = time.time(), False
        while proc.poll() is None:
            if self._fermo.wait(0.5):
                self._termina(proc)
                break
            ora = time.time()
            if not aperto and ((autenticato[0] and ora - autenticato[0] >= self.CONFERMA_S) or ora - inizio >= 15):
                aperto = True
                dove = self.indirizzo or f"porta {reg['porta']} del server"
                self._segna(True, f"attivo, gestione e stato del gateway su {dove} (tunnel verso {reg['host']})")
        lettore.join(timeout=2)
        with self._lock:
            self._proc = None
        return self.classifica(list(righe)), time.time() - inizio, aperto, list(righe)

    def _ciclo(self) -> None:
        attesa = 0.0           # prima del prossimo giro
        attesa_ssh = 0.0       # cresce se il server SSH non si raggiunge
        inizio_attesa = 0.0    # da quando la registrazione non si legge
        registrato_una_volta = False
        prossima_richiesta = 0.0                 # quando mandare (di nuovo) la richiesta di entrare
        notizia = ("", logging.INFO)             # esito dell'ultima richiesta, ripetuto finche' non cambia
        while not self._fermo.is_set():
            if attesa and self._fermo.wait(attesa):
                break
            try:
                reg = self._registrazione()
                errore = None
            except Exception as e:  # noqa: rete, DNS, certificato, risposta strana
                reg, errore = None, e
            if reg is None:
                # Una lettura via HTTPS ogni minuto (dopo due ore ogni 5): costa nulla e appena un
                # amministratore approva la richiesta il tunnel parte.
                inizio_attesa = inizio_attesa or time.time()
                attesa = self.ATTESA_REGISTRAZIONE[1 if time.time() - inizio_attesa > self.VELOCE_S else 0]
                if errore is not None:
                    self._segna(False, f"la pagina della rete {self.rete} non risponde ({errore}): riprovo tra "
                                       f"{attesa:.0f} s" + ("" if registrato_una_volta else
                                                           " (normale finche' sul server non c'e' la pagina della "
                                                           "rete:  sudo bash rete-meshtastic.sh installa)"),
                                logging.WARNING if registrato_una_volta else logging.INFO)
                    continue
                if time.time() >= prossima_richiesta:
                    messaggio, livello, fra = self._manda_richiesta()
                    prossima_richiesta = time.time() + fra
                    notizia = (messaggio, livello)
                    if self.richiesta == "registrato":
                        attesa = 2.0     # approvato in questo momento: la registrazione c'e' gia'
                self._segna(False, notizia[0], notizia[1])
                continue
            inizio_attesa = 0.0
            prossima_richiesta = 0.0     # se un giorno sparisce (ponte tolto) si richiede subito
            self.richiesta = None
            registrato_una_volta = True
            with self._lock:
                self.indirizzo = reg["indirizzo"]
            try:
                self._scrivi_known_hosts(reg)
            except OSError as e:
                attesa = 300.0
                self._segna(False, f"non riesco a salvare la chiave del server ({e}): riprovo tra 5 minuti",
                            logging.ERROR)
                continue
            esito, durata, aperto, righe = self._tunnel(reg)
            if self._fermo.is_set():
                break
            dettaglio = righe[-1] if righe else "nessun messaggio"
            if esito == "porta":
                attesa = self.ATTESA_PORTA_OCCUPATA_S
                msg = (f"sul server la porta {reg['porta']} e' ancora occupata (il collegamento di prima non e' "
                       f"ancora scaduto): riprovo tra {attesa:.0f} s")
            elif esito == "rifiuto":
                attesa = self.ATTESA_RIFIUTO_S
                msg = (f"il server ha rifiutato la chiave del gateway: riprovo tra {attesa / 60:.0f} minuti. Se dura, "
                       "sul server  sudo bash rete-meshtastic.sh aggiorna")
            elif esito == "chiave_server":
                attesa = self.ATTESA_RIFIUTO_S
                msg = (f"la chiave SSH del server non e' quella pubblicata dalla pagina della rete: non mi collego, "
                       f"riprovo tra {attesa / 60:.0f} minuti ({dettaglio})")
            elif aperto and durata >= self.DURATA_STABILE_S:
                attesa = attesa_ssh = 5.0
                msg = f"tunnel chiuso dopo {durata / 60:.0f} minuti ({dettaglio}): mi ricollego"
            else:
                attesa = attesa_ssh = min(max(attesa_ssh * 2, self.ATTESA_RETE[0]), self.ATTESA_RETE[1])
                msg = f"server SSH {reg['host']} non raggiungibile ({dettaglio}): riprovo tra {attesa:.0f} s"
            self._segna(False, msg, logging.INFO if esito == "rete" and aperto else logging.WARNING)


def leggi_canali(iface, cfg: Config) -> Tuple[Dict[int, Canale], Set[int]]:
    from meshtastic.protobuf import config_pb2
    try:
        preset = config_pb2.Config.LoRaConfig.ModemPreset.Name(
            iface.localNode.localConfig.lora.modem_preset)
    except Exception:  # noqa
        preset = ""
    canali: Dict[int, Canale] = {}
    for ch in (iface.localNode.channels or []):
        if ch.role == 0:  # DISABLED
            continue
        psk = bytes(ch.settings.psk)
        canali[ch.index] = Canale(ch.index, ch.settings.name or nome_preset(preset), len(psk) >= 16)
    modo = cfg.canali.strip().lower()
    if modo == "privati":
        ok = {i for i, c in canali.items() if c.privato}
    elif modo == "tutti":
        ok = set(canali)
    else:
        ok = {int(x) for x in modo.replace(";", ",").split(",") if x.strip()} & set(canali)
    return canali, ok


