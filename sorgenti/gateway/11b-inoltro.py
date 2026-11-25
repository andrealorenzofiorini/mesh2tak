# --------------------------------------------------------------------------
# Inoltro verso altri server (dalla 1.23.0)
# --------------------------------------------------------------------------

class Inoltro:
    """Copia dei pacchetti ricevuti dalla radio per altri server, che li convertono per il loro TAK Server.

    Il lavoro di sempre (convertire e parlare con il TAK Server del gateway) non cambia: qui i pacchetti gia'
    gestiti finiscono anche in una memoria, da cui li legge chi si collega alla porta dell'inoltro. La porta
    ascolta solo sul Raspberry (127.0.0.1): ci arrivano i tunnel SSH aperti verso i server scelti in [inoltro],
    quindi in viaggio i pacchetti sono cifrati e li legge solo un server che ha approvato questo gateway.
    Solo ascolto: da questa porta non si comanda niente, ne' il gateway ne' la radio.
    Passano solo posizioni, nomi delle radio, telemetria e pacchetti TAK: mai i messaggi di amministrazione
    delle radio, che possono contenere la chiave del canale.
    """

    PORTE = (PORTA_POSITION, PORTA_NODEINFO, PORTA_TELEMETRY, PORTA_ATAK_V1, PORTA_ATAK_V2)
    MEMORIA = 2000            # pacchetti tenuti per chi resta indietro (un server che si ricollega)
    PER_RISPOSTA = 200
    ATTESA_MAX_S = 25.0
    NODI_MAX = 500

    def __init__(self, porta: int, indirizzo: str = "127.0.0.1"):
        self.porta = porta
        self.indirizzo = indirizzo
        self.avvio = uuid.uuid4().hex[:12]     # cambia a ogni avvio: chi legge capisce che la numerazione riparte
        self._cond = threading.Condition()
        self._pacchetti: List[Tuple[int, str]] = []
        self._ultimo = 0
        self._descrizione: Optional[dict] = None
        self._n_descrizione = 0
        self._letture: Dict[str, float] = {}   # chi legge (porta di chi chiede non serve: una voce per tunnel)
        self.inoltrati = 0
        self._server: Optional[http.server.ThreadingHTTPServer] = None

    # ---- dal gateway ----

    def descrivi(self, d: Optional[dict]) -> None:
        """Il nodo a cui il gateway e' collegato (None: scollegato). Senza, chi legge non sa canali e nomi."""
        with self._cond:
            self._descrizione = d
            self._n_descrizione += 1
            self._cond.notify_all()

    def aggiungi(self, p: dict) -> None:
        grezzo = p.get("raw")
        if grezzo is None or "decoded" not in p or grezzo.decoded.portnum not in self.PORTE:
            return
        import base64
        testo = base64.b64encode(grezzo.SerializeToString()).decode("ascii")
        with self._cond:
            self._ultimo += 1
            self._pacchetti.append((self._ultimo, testo))
            if len(self._pacchetti) > self.MEMORIA:
                del self._pacchetti[:len(self._pacchetti) - self.MEMORIA]
            self.inoltrati += 1
            self._cond.notify_all()

    def stato(self) -> dict:
        ora = time.time()
        with self._cond:
            return {"porta": self.porta, "pacchetti": self.inoltrati,
                    "letto": sum(1 for t in self._letture.values() if ora - t < 90)}

    # ---- per chi legge ----

    def pacchetti(self, avvio: str, dopo: int, attesa: float, descrizione: int) -> dict:
        fine = time.time() + max(0.0, min(attesa, self.ATTESA_MAX_S))
        with self._cond:
            if avvio != self.avvio or dopo < 0 or dopo > self._ultimo:
                # lettore nuovo, o gateway riavviato: si parte da adesso, senza ripetere pacchetti vecchi
                return {"formato": 1, "avvio": self.avvio, "descrizione": self._n_descrizione, "ultimo": self._ultimo,
                        "persi": 0, "pacchetti": []}
            while self._ultimo <= dopo and descrizione == self._n_descrizione:
                resta = fine - time.time()
                if resta <= 0:
                    break
                self._cond.wait(resta)
            nuovi = [x for x in self._pacchetti if x[0] > dopo][:self.PER_RISPOSTA]
            primo = self._pacchetti[0][0] if self._pacchetti else self._ultimo + 1
            return {"formato": 1, "avvio": self.avvio, "descrizione": self._n_descrizione,
                    "ultimo": nuovi[-1][0] if nuovi else dopo,
                    "persi": max(0, primo - dopo - 1), "pacchetti": [[n, t] for n, t in nuovi]}

    def descrizione(self, attesa: float = 0.0) -> dict:
        """Con 'attesa' risponde appena il gateway e' collegato alla radio: chi legge e' pronto prima del primo
        pacchetto."""
        fine = time.time() + max(0.0, min(attesa, self.ATTESA_MAX_S))
        with self._cond:
            while self._descrizione is None:
                resta = fine - time.time()
                if resta <= 0:
                    break
                self._cond.wait(resta)
            return {"formato": 1, "versione": VERSIONE, "avvio": self.avvio, "descrizione": self._n_descrizione,
                    "ultimo": self._ultimo, "nodo": self._descrizione}

    def avvia(self) -> None:
        inoltro = self

        class Gestore(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = 60

            def log_message(self, formato, *argomenti):
                pass

            def _manda(self, codice: int, dati: dict) -> None:
                corpo = json.dumps(dati, separators=(",", ":"), default=lambda o: None).encode("utf-8")
                self.send_response(codice)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(corpo)))
                self.end_headers()
                self.wfile.write(corpo)

            def do_GET(self):  # noqa: N802
                u = urllib.parse.urlsplit(self.path)
                q = urllib.parse.parse_qs(u.query)

                def numero_di(nome: str, predefinito: float) -> float:
                    try:
                        return float(q.get(nome, [predefinito])[0])
                    except ValueError:
                        return predefinito
                if u.path == "/inoltro/descrizione.json":
                    self._manda(200, inoltro.descrizione(numero_di("attesa", 0)))
                elif u.path == "/inoltro/pacchetti":
                    with inoltro._cond:
                        inoltro._letture["lettore"] = time.time()
                    self._manda(200, inoltro.pacchetti(str(q.get("avvio", [""])[0])[:32], int(numero_di("dopo", -1)),
                                                       numero_di("attesa", 0), int(numero_di("descrizione", -1))))
                else:
                    self._manda(404, {"errore": "qui c'e' solo l'inoltro dei pacchetti"})

        self._server = http.server.ThreadingHTTPServer((self.indirizzo, self.porta), Gestore)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, name="inoltro", daemon=True).start()

    def ferma(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        with self._cond:
            self._cond.notify_all()


def descrizione_del_nodo(ponte: "Ponte", iface) -> dict:
    """Quello che serve a un server per convertire come farebbe il gateway: chi e' il nodo, i canali (senza le
    chiavi) e le radio che il nodo conosce gia'."""
    nodi = {}
    try:
        for num, info in list((iface.nodesByNum or {}).items())[:Inoltro.NODI_MAX]:
            if isinstance(num, int) and isinstance(info, dict):
                nodi[str(num)] = {k: info[k] for k in ("num", "user", "position", "lastHeard", "snr", "hopsAway",
                                                        "channel", "deviceMetrics", "viaMqtt") if k in info}
    except Exception:  # noqa
        pass
    return {"num": ponte.mio_num, "preset": ponte.preset, "salti": ponte.hop_limit, "potenza_dbm": ponte.potenza_dbm,
            "regione": ponte.regione, "canali": [[c.indice, c.nome, bool(c.privato)] for c in ponte.canali.values()],
            "canali_ok": sorted(ponte.canali_ok), "nodi": nodi}


def _classe_remota():
    import meshtastic.mesh_interface as mi
    from meshtastic.protobuf import mesh_pb2
    from pubsub import pub

    class RadioRemota(mi.MeshInterface):
        """Sul server: la radio e' quella di un gateway che inoltra (collegamento = inoltro:http://127.0.0.1:PORTA,
        la porta del suo tunnel). I pacchetti arrivano come dalla radio vera e li gestisce la stessa libreria;
        verso la radio non si manda niente."""

        SILENZIO_MAX_S = 150.0     # il tunnel o il gateway non rispondono: collegamento perso, si riprova

        def __init__(self, indirizzo: str):
            super().__init__(noProto=True)
            self.indirizzo = indirizzo.rstrip("/")
            d = self._leggi("/inoltro/descrizione.json?attesa=20", 35)
            nodo = d.get("nodo") if isinstance(d, dict) else None
            if not isinstance(nodo, dict) or not isinstance(nodo.get("num"), int):
                raise ConnectionError("il gateway non e' collegato alla sua radio")
            self.descrizione = nodo
            self._avvio, self._n_descrizione = str(d.get("avvio", "")), d.get("descrizione")
            self.nodesByNum, self.nodes = {}, {}
            for chiave, info in (nodo.get("nodi") or {}).items():
                try:
                    num = int(chiave)
                except ValueError:
                    continue
                if isinstance(info, dict):
                    self.nodesByNum[num] = info
                    if isinstance(info.get("user"), dict) and info["user"].get("id"):
                        self.nodes[info["user"]["id"]] = info
            self.myInfo = mesh_pb2.MyNodeInfo(my_node_num=nodo["num"])
            self.localNode.nodeNum = nodo["num"]
            self._chiuso = threading.Event()
            self._dopo = -1
            threading.Thread(target=self._ciclo, name="radio-remota", daemon=True).start()

        def _leggi(self, percorso: str, limite: float) -> dict:
            apri = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with apri.open(self.indirizzo + percorso, timeout=limite) as r:
                return json.loads(r.read(8 << 20).decode("utf-8"))

        def _ciclo(self) -> None:
            import base64
            ultimo_ok = time.time()
            while not self._chiuso.is_set():
                try:
                    d = self._leggi(f"/inoltro/pacchetti?avvio={self._avvio}&dopo={self._dopo}&attesa=25"
                                    f"&descrizione={self._n_descrizione}", 40)
                    ultimo_ok = time.time()
                except Exception as e:  # noqa: tunnel chiuso, gateway spento
                    if time.time() - ultimo_ok > self.SILENZIO_MAX_S:
                        log.warning("Il gateway non manda piu' i suoi pacchetti (%s)", e)
                        break
                    self._chiuso.wait(5)
                    continue
                if d.get("avvio") != self._avvio or d.get("descrizione") != self._n_descrizione:
                    break      # gateway riavviato o ricollegato alla radio: si rilegge tutto da capo
                if d.get("persi"):
                    log.warning("Persi %d pacchetti del gateway (collegamento rimasto indietro)", d["persi"])
                for _n, testo in d.get("pacchetti") or []:
                    try:
                        mp = mesh_pb2.MeshPacket()
                        mp.ParseFromString(base64.b64decode(testo))
                        self._handlePacketFromRadio(mp)
                    except Exception:  # noqa
                        log.exception("Pacchetto del gateway non leggibile")
                if isinstance(d.get("ultimo"), int):
                    self._dopo = d["ultimo"]
            if not self._chiuso.is_set():
                pub.sendMessage("meshtastic.connection.lost", interface=self)

        def _sendToRadioImpl(self, toRadio) -> None:  # noqa: N802,N803 (nome della libreria)
            pass

        def close(self) -> None:
            self._chiuso.set()

    return RadioRemota
