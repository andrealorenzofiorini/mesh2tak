class Ponte:
    # Attese casuali prima di un inoltro, per lasciar fare a un altro ponte che l'ha gia' in mano
    ATTESA_VERSO_SERVER = (0.5, 2.5)
    ATTESA_VERSO_RADIO = (2.0, 8.0)
    ATTESA_VERSO_RADIO_ALLARME = (0.2, 1.5)
    SPAZIO_TRA_INVII_S = 3.0          # tra due trasmissioni del ponte, per lasciar ripetere la rete
    RISERVA_RADIO_S = 600.0           # il credito di tempo radio si accumula al massimo per 10 minuti
    MAX_VISTE = 300                   # radio ricordate per l'elenco della pagina della rete

    def __init__(self, cfg: Config, tak: ClienteTAK, sdk: Optional[LibreriaTak]):
        self.cfg = cfg
        self.tak = tak
        self.sdk = sdk
        self.iface = None
        self.mio_num: Optional[int] = None
        self.canali: Dict[int, Canale] = {}
        self.canali_ok: Set[int] = set()
        self.stato: Dict[int, StatoNodo] = {}
        self.viste: Dict[int, RadioVista] = {}       # radio della squadra sentite (per la pagina della rete)
        self.canale_locale: dict = {}               # canale radio misurato dal nodo del ponte
        self.persa = threading.Event()
        self.contatori: Counter = Counter()
        self._segnalati: Set[Tuple[int, str]] = set()
        self._lock = threading.RLock()
        self._ultimo_riepilogo = time.time()
        self.nota_ponte: Optional[str] = None   # es. "IN PAUSA: ..." in testa alle note del ponte
        self.icone = IconeMappa()               # dalla 1.11.0: icone scelte dalla pagina della rete (main: su file)
        self.copertura = Copertura()            # dalla 1.14.0: mappa della copertura (main: su file)
        self.storico = Storico()                # dalla 1.16.0: andamento di ponte e ripetitori (main: su file)
        self.squadra = RadioSquadra()           # dalla 1.15.0: radio sentite sul canale della squadra (main: su file)
        # ultimo nodo visto (resta anche da scollegati, per la pagina della rete)
        self.nodo_nome = ""
        self.nodo_nome_breve = ""
        self.nodo_chiave_pubblica = ""
        self.nodo_collegato_dal: Optional[float] = None
        try:
            from zoneinfo import ZoneInfo
            self.tz = ZoneInfo(cfg.fuso_orario)
        except Exception:  # noqa
            self.tz = None
        # radio <-> server
        self.preset = "LONG_FAST"
        self.hop_limit = 3
        self._inoltrati_al_server = Memoria(600)    # gia' portati dalla radio al server
        self._visti_dal_server = Memoria(900)       # arrivati dal server (o portati la' da un altro ponte)
        self._sentiti_in_radio = Memoria(600)       # sentiti in radio (da app, radio o altri ponti)
        self._trasmessi = Memoria(120)              # trasmessi da noi da poco
        self._avvisati = Memoria(3600)              # avvisi "troppo grande" gia' mandati
        self._collegati_internet = Memoria(COLLEGATO_INTERNET_S)  # UID di telefoni visti via internet
        self.coda_radio = CodaRadio()
        self._dal_server: "queue.Queue[bytes]" = queue.Queue(maxsize=1000)
        self._verso_server: List[Tuple[float, str, bytes, str]] = []
        self._lock_uscite = threading.Lock()
        self._lock_invio = threading.Lock()     # un pacchetto alla volta verso il nodo (anche dalla gestione delle radio)
        self._ultima_trasmissione = 0.0
        self._credito_radio = self.RISERVA_RADIO_S * cfg.tempo_radio_percento / 100.0
        self._ultima_ricarica = time.time()
        self._fermo = threading.Event()
        self._thread_uscite: Optional[threading.Thread] = None

    @property
    def mio_uid(self) -> str:
        return f"!{self.mio_num:08x}" if self.mio_num else "mesh2tak"

    # ---- eventi dalla libreria meshtastic (thread di pubblicazione) ----

    def su_pacchetto(self, packet, interface=None):
        if interface is not self.iface or self.iface is None:
            return
        try:
            self._gestisci(packet)
        except Exception:  # noqa
            log.exception("Errore inatteso nel gestire un pacchetto")

    def su_perdita(self, interface=None):
        if interface is self.iface:
            self.persa.set()

    # ---- collegamento ----

    def collega(self, iface) -> None:
        # dalla 1.13.0 il ponte non tocca le impostazioni del suo nodo: vale quello che si sceglie dall'app o dalla
        # pagina (anche un canale secondario acceso). Le rimette come all'installazione solo "Reimposta la radio".
        canali, ok = leggi_canali(iface, self.cfg)
        preset, salti = "LONG_FAST", 3
        try:
            from meshtastic.protobuf import config_pb2
            lora = iface.localNode.localConfig.lora
            if lora.use_preset:
                preset = config_pb2.Config.LoRaConfig.ModemPreset.Name(lora.modem_preset)
            salti = int(lora.hop_limit) or 3
        except Exception:  # noqa
            pass
        with self._lock:
            self.canali, self.canali_ok = canali, ok
            self.mio_num = iface.myInfo.my_node_num if getattr(iface, "myInfo", None) else None
            self.preset, self.hop_limit = preset, salti
            self.persa.clear()
            self.iface = iface
            utente = self._info(self.mio_num).get("user", {}) if self.mio_num else {}
            self.nodo_nome = pulisci(utente.get("longName"), 60)
            self.nodo_nome_breve = pulisci(utente.get("shortName"), 8)
            self.nodo_collegato_dal = time.time()
            # la chiave pubblica del nodo: e' la "chiave amministratore" da mettere sulle radio da gestire
            self.nodo_chiave_pubblica = pulisci(utente.get("publicKey"), 64) or self.nodo_chiave_pubblica
        self._descrivi_nodo()
        self._carica_database()
        self._carica_viste()

    def scollega(self) -> None:
        with self._lock:
            self.iface = None
            self.nodo_collegato_dal = None

    def _descrivi_nodo(self) -> None:
        info = self._info(self.mio_num) if self.mio_num else {}
        nome = info.get("user", {}).get("longName", "?")
        log.info("Collegato al nodo Meshtastic '%s' (!%08x) - %s", nome, self.mio_num or 0,
                 descrivi_collegamento(self.cfg.collegamento))
        for c in self.canali.values():
            stato = "INOLTRATO" if c.indice in self.canali_ok else "ignorato"
            log.info("  canale %d '%s' (%s): %s", c.indice, c.nome,
                     "privato" if c.privato else "pubblico, chiave nota a tutti", stato)
        if not self.canali_ok:
            log.warning("Nessun canale da inoltrare con 'canali = %s': non arrivera' nessuna posizione. "
                        "Crea un canale privato della squadra anche sul Raspberry, o cambia 'canali' "
                        "in %s", self.cfg.canali, CONFIG_PREDEFINITA)
        if self.cfg.invio_radio and self.sdk is not None:
            canale = self._canale_invio()
            if canale is None:
                log.warning("Nessun canale privato su cui trasmettere: chat e oggetti dal TAK Server non "
                            "andranno alle radio")
            else:
                log.info("  dal TAK Server alle radio: canale %d '%s', preset %s, al massimo %.1f%% del "
                         "tempo radio", canale, self.canali[canale].nome, self.preset,
                         self.cfg.tempo_radio_percento)

    def _canale_invio(self) -> Optional[int]:
        """Il canale privato della squadra su cui trasmettere (il primo inoltrato)."""
        privati = sorted(i for i, c in self.canali.items() if c.privato and i in self.canali_ok)
        return privati[0] if privati else None

    def _info(self, num: Optional[int]) -> dict:
        iface = self.iface
        if iface is None or num is None:
            return {}
        try:
            return iface.nodesByNum.get(num) or {}
        except Exception:  # noqa
            return {}

    def _carica_database(self) -> None:
        """All'avvio inoltra le radio gia' note al nodo e sentite di recente."""
        iface = self.iface
        if iface is None:
            return
        ora = time.time()
        validita = self.cfg.validita_min * 60
        inoltrate = 0
        for num, info in list(iface.nodesByNum.items()):
            pos = info.get("position") or {}
            lat_i, lon_i = pos.get("latitudeI"), pos.get("longitudeI")
            if not lat_i and not lon_i:
                continue
            ponte = num == self.mio_num
            if ponte and not self.cfg.includi_ponte:
                continue
            sentito = ora if ponte else float(info.get("lastHeard") or 0)
            if not ponte:
                if ora - sentito > validita:
                    continue
                if num not in self.squadra:     # dalla 1.15.0: solo radio sentite sul canale della squadra
                    continue
                if info.get("user", {}).get("role") == "TAK":
                    continue
            fonte = self._fonte_mesh(num, pos, info.get("channel", 0), sentito)
            if fonte is None:
                continue
            with self._lock:
                self.stato[num] = StatoNodo(num, fonte, sentito)
                self._invia(self.stato[num], ora)
            inoltrate += 1
        log.info("Dal database del nodo: %d radio con posizione recente inoltrate", inoltrate)

    def _canale_noto(self, info: dict) -> bool:
        """La radio ricordata dal nodo e' davvero del canale della squadra. Il nodo segna il canale di una radio
        solo quando ne riceve il nome (NodeInfo): prima vale 0, che dove il canale della squadra e' il primario
        faceva passare per radio della squadra anche gli estranei sentiti sul canale pubblico (fino alla 1.9.0)."""
        return bool(info.get("user")) and int(info.get("channel", 0) or 0) in self.canali_ok

    def _carica_viste(self) -> None:
        """All'avvio, le radio della squadra che il nodo conosce gia' (con l'ultimo stato che ricorda)."""
        iface = self.iface
        if iface is None:
            return
        for num, info in list(iface.nodesByNum.items()):
            # dalla 1.15.0 solo le radio che il ponte ha sentito davvero sul canale della squadra: il canale che il
            # nodo ricorda puo' essere quello di quando il suo canale principale era un altro
            if not isinstance(num, int) or num == self.mio_num or info.get("viaMqtt") or num not in self.squadra:
                continue
            sentito = numero(info.get("lastHeard"), 1, 4102444800, 0)
            if not sentito:
                continue
            with self._lock:
                v = self._vista_di(num)
                if v.ultimo >= sentito:
                    continue
                v.ultimo = sentito
                self._utente(v, info.get("user") or {})
                if v.lat is None:
                    self._posizione_vista(v, info.get("position") or {})
                if v.metriche is None:
                    self._metriche(v, info.get("deviceMetrics") or {}, sentito)
                salti = info.get("hopsAway")
                if isinstance(salti, int) and 0 <= salti <= 7:
                    v.salti = salti
                    if salti == 0 and info.get("snr") is not None:
                        v.snr, v.diretto = numero(info.get("snr"), -40, 40, 2), sentito

    def _vista_di(self, num: int) -> RadioVista:
        v = self.viste.get(num)
        if v is None:
            if len(self.viste) >= self.MAX_VISTE:
                del self.viste[min(self.viste.values(), key=lambda x: x.ultimo).num]
            v = self.viste[num] = RadioVista(num)
        return v

    @staticmethod
    def _utente(v: RadioVista, u: dict) -> None:
        if not u:
            return
        v.nome = pulisci(u.get("longName"), 60) or v.nome
        v.nome_breve = pulisci(u.get("shortName"), 8) or v.nome_breve
        modello, ruolo = str(u.get("hwModel") or ""), str(u.get("role") or "CLIENT")
        v.modello = modello if RE_ENUM.match(modello) else v.modello
        v.ruolo = ruolo if RE_ENUM.match(ruolo) else v.ruolo

    @staticmethod
    def _metriche(v: RadioVista, dm: dict, quando: float) -> None:
        if not dm:
            return
        completo = "uptimeSeconds" in dm      # un rapporto vero: i valori a zero il formato li omette
        b = dm.get("batteryLevel")
        if isinstance(b, int) and 0 < b <= 101:
            v.batteria = b
        v.tensione = numero(dm.get("voltage"), 0.5, 60, 2) if dm.get("voltage") else v.tensione
        for campo, chiave in (("occupato", "channelUtilization"), ("trasmesso", "airUtilTx")):
            if chiave in dm or completo:
                setattr(v, campo, numero(dm.get(chiave, 0.0), 0, 100, 1 if campo == "occupato" else 2))
        acceso = dm.get("uptimeSeconds")
        if isinstance(acceso, int) and acceso >= 0:
            v.acceso_s = acceso
        v.metriche = quando

    def _vista(self, p: dict, num: int, canale: int, rx: float, porta: int) -> None:
        """Ogni pacchetto di una radio della squadra aggiorna la sua scheda per la pagina della rete."""
        if num == self.mio_num or canale not in self.canali_ok or p.get("viaMqtt"):
            return
        if p.get("pkiEncrypted") and num not in self.viste:
            return      # messaggio diretto cifrato per il nodo: non dice su che canale sta chi lo manda
        d = p.get("decoded") or {}
        if not p.get("pkiEncrypted"):
            self.squadra.segna(num, rx)     # decifrato con la chiave del canale: e' davvero della squadra
        with self._lock:
            v = self._vista_di(num)
            v.ultimo = max(v.ultimo, rx)
            inizio, resto = p.get("hopStart"), p.get("hopLimit", 0)
            if isinstance(inizio, int) and 0 < inizio <= 7 and isinstance(resto, int) and 0 <= resto <= inizio:
                v.salti = inizio - resto
                if v.salti == 0 and p.get("rxRssi"):        # sentita direttamente: segnale del suo trasmettitore
                    v.snr = numero(p.get("rxSnr", 0.0), -40, 40, 2)
                    v.rssi = int(numero(p["rxRssi"], -200, 0, 0) or 0) or None
                    v.diretto = rx
            if porta == PORTA_NODEINFO:
                self._utente(v, d.get("user") or {})
            elif porta == PORTA_TELEMETRY:
                self._metriche(v, (d.get("telemetry") or {}).get("deviceMetrics") or {}, rx)
            elif porta == PORTA_POSITION:
                self._posizione_vista(v, d.get("position") or {})

    @staticmethod
    def _posizione_vista(v: RadioVista, pos: dict) -> None:
        """Dalla 1.15.0: posizione impostata a mano (nodo fisso) e ultima posizione, per i ripetitori sulla mappa."""
        lat_i, lon_i = pos.get("latitudeI"), pos.get("longitudeI")
        if not isinstance(lat_i, int) or not isinstance(lon_i, int) or (lat_i == 0 and lon_i == 0):
            return
        lat, lon = lat_i * 1e-7, lon_i * 1e-7
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            v.lat, v.lon = lat, lon
            v.posizione_fissa = pos.get("locationSource") == "LOC_MANUAL"

    def _canale_del_ponte(self, p: dict, rx: float) -> None:
        """Il nodo del ponte manda ogni minuto le sue misure: quanto e' occupato il canale, quanto trasmette."""
        t = (p.get("decoded") or {}).get("telemetry") or {}
        for chiave in ("deviceMetrics", "localStats"):
            m = t.get(chiave)
            if not m or "uptimeSeconds" not in m:
                continue
            with self._lock:
                c = self.canale_locale
                c["occupato"] = numero(m.get("channelUtilization", 0.0), 0, 100, 1)
                c["trasmesso"] = numero(m.get("airUtilTx", 0.0), 0, 100, 2)
                if chiave == "localStats" and m.get("noiseFloor"):
                    c["rumore"] = numero(m.get("noiseFloor"), -200, 0, 0)
                c["quando"] = rx

    # ---- pacchetti ----

    def _gestisci(self, p: dict) -> None:
        num = p.get("from")
        grezzo = p.get("raw")
        if not num or num == BROADCAST or grezzo is None or "decoded" not in p:
            return
        porta = grezzo.decoded.portnum
        canale = int(p.get("channel", 0) or 0)
        ora = time.time()
        rx = float(p.get("rxTime") or ora)
        if rx > ora + 300 or rx < ora - 86400:
            rx = ora
        ripetitore = None
        if num == self.mio_num:
            if porta == PORTA_TELEMETRY:
                self._canale_del_ponte(p, rx)
        else:
            self._vista(p, num, canale, rx, porta)
            ripetitore = self._ripetuto_da(p, num, rx)
        fonte = None
        if porta == PORTA_POSITION:
            pos = p["decoded"].get("position") or {}
            if pos.get("latitudeI") or pos.get("longitudeI") or not p["decoded"].get("wantResponse"):
                fonte = self._posizione(num, pos, canale, rx)  # una richiesta di posizione vuota non e' una posizione
        elif porta == PORTA_ATAK_V2:
            fonte = self._tak_v2(num, bytes(grezzo.decoded.payload), canale, rx)
        elif porta == PORTA_ATAK_V1:
            fonte = self._tak_v1(num, bytes(grezzo.decoded.payload), canale, rx)
        if fonte is not None and num != self.mio_num:
            self._annota_copertura(p, fonte, rx, ripetitore)
        self._attivita(num, rx, porta)

    def _ripetuto_da(self, p: dict, num: int, rx: float) -> Optional[int]:
        """Dalla 1.15.0: quale radio della squadra ha ripetuto il pacchetto arrivato al ponte. Il pacchetto porta
        solo l'ultimo byte del numero di chi l'ha ritrasmesso per ultimo (firmware 2.6 e successivi): vale se una
        sola radio della squadra finisce con quel byte. E' anche il segno che un ripetitore e' acceso."""
        inizio, resto, byte = p.get("hopStart"), p.get("hopLimit", 0), p.get("relayNode")
        if (p.get("viaMqtt") or not isinstance(byte, int) or not 0 < byte <= 0xFF or not isinstance(inizio, int)
                or not isinstance(resto, int) or not 0 <= resto < inizio <= 7):
            return None
        with self._lock:
            chi = [v for v in self.viste.values() if v.num & 0xFF == byte and v.num not in (num, self.mio_num)]
            if len(chi) != 1:
                return None
            v = chi[0]
            v.ripetuti += 1
            v.ultimo_ripetuto = max(v.ultimo_ripetuto or 0.0, rx)
            return v.num

    def _annota_copertura(self, p: dict, fonte: "Fonte", rx: float, ripetitore: Optional[int] = None) -> None:
        """Mappa della copertura (dalla 1.14.0): dove si trovava la radio e con che segnale il ponte l'ha sentita.
        Il segnale misurato e' quello dell'ultimo trasmettitore: vale per il punto della radio solo se e' arrivata
        diretta. Arrivata con un salto da un ripetitore fisso (dalla 1.15.0), il quadretto conta per quel
        ripetitore, senza segnale; altrimenti dice soltanto che da li' si arriva alla rete."""
        if p.get("viaMqtt") or (fonte.ce != NON_NOTO and fonte.ce > Copertura.ERRORE_MASSIMO_M):
            return
        inizio, resto = p.get("hopStart"), p.get("hopLimit", 0)
        if not (isinstance(inizio, int) and 0 < inizio <= 7 and isinstance(resto, int) and 0 <= resto <= inizio):
            return      # firmware che non dice i salti: non si sa se e' arrivata diretta
        salti, snr, rssi, via = inizio - resto, None, None, None
        if salti == 0:
            if not p.get("rxRssi"):
                return  # nessuna misura del segnale
            snr = numero(p.get("rxSnr", 0.0), -40, 40, 2)
            rssi = int(numero(p["rxRssi"], -200, 0, 0) or 0) or None
        elif salti == 1 and ripetitore is not None:
            with self._lock:
                v = self.viste.get(ripetitore)
                via = ripetitore if v is not None and v.fisso else None
        quando = datetime.fromtimestamp(rx, self.tz) if self.tz else datetime.fromtimestamp(rx)
        self.copertura.annota(fonte.lat, fonte.lon, salti, snr, rssi, int(quando.strftime("%Y%m%d")), via)

    def copertura_per_la_pagina(self, azzera: bool = False, chi: str = "") -> dict:
        """/mesh2tak/copertura.json: i quadretti, dove sta il ponte e il limite del segnale del suo preset."""
        if azzera:
            self.copertura.azzera()
            log.info("Mappa della copertura azzerata dalla pagina della rete (%s)", chi or "?")
        d = self.copertura.dati()
        pos = self._info(self.mio_num).get("position") or {}
        lat_i, lon_i = pos.get("latitudeI"), pos.get("longitudeI")
        d["ponte"] = ({"lat": round(lat_i * 1e-7, 4), "lon": round(lon_i * 1e-7, 4)}
                      if isinstance(lat_i, int) and isinstance(lon_i, int) and (lat_i or lon_i) else None)
        d["preset"], d["limite_snr"] = self.preset, limite_snr(self.preset)
        with self._lock:      # dalla 1.15.0 i ripetitori fissi, con la loro posizione (solo qui, dietro Authentik)
            d["ripetitori"] = [{"id": f"!{v.num:08x}", "nome": v.nome or self._nome(v.num), "nome_breve": v.nome_breve,
                                "lat": round(v.lat, 4), "lon": round(v.lon, 4)}
                               for v in sorted(self.viste.values(), key=lambda v: v.num)
                               if v.fisso and v.lat is not None and v.lon is not None]
        return d

    def _scarta(self, num: int, motivo: str, spiegazione: str) -> None:
        self.contatori["scartate_" + motivo] += 1
        chiave = (num, motivo)
        if chiave not in self._segnalati:
            self._segnalati.add(chiave)
            log.info("Ignoro %s (!%08x): %s", self._nome(num), num, spiegazione)
        else:
            log.debug("Ignoro !%08x: %s", num, spiegazione)

    def _nome(self, num: int) -> str:
        u = self._info(num).get("user", {})
        return pulisci(u.get("longName") or u.get("shortName") or f"Meshtastic {num & 0xFFFF:04x}", 80)

    def _canale_ok(self, num: int, canale: int) -> bool:
        if num == self.mio_num or canale in self.canali_ok:
            return True
        c = self.canali.get(canale)
        nome = c.nome if c else f"n. {canale}"
        self._scarta(num, "canale", f"la sua posizione arriva sul canale '{nome}', non inoltrato")
        return False

    def _fonte_mesh(self, num: int, pos: dict, canale: int, rx: float) -> Optional[Fonte]:
        lat_i, lon_i = int(pos.get("latitudeI") or 0), int(pos.get("longitudeI") or 0)
        if lat_i == 0 and lon_i == 0:
            self._scarta(num, "senza_gps", "manda posizioni vuote (GPS senza fix?)")
            return None
        lat, lon = lat_i * 1e-7, lon_i * 1e-7
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        bit = bit_precisione(lat_i, lon_i, int(pos.get("precisionBits") or 0))
        ce = NON_NOTO
        if bit < 32:
            ce = errore_da_bit(bit)
            if ce > self.cfg.errore_massimo_m:
                self._scarta(num, "imprecisa", f"posizione volutamente approssimata (±{ce:.0f} m): sulla radio "
                             "attiva la posizione precisa per il canale della squadra")
                return None
        quota_msl = None
        if "altitudeHae" in pos:
            hae = float(pos["altitudeHae"])
        elif "altitude" in pos:
            quota_msl = float(pos["altitude"])
            sep = pos.get("altitudeGeoidalSeparation")
            hae = quota_msl + (float(sep) if sep is not None else self.cfg.geoide_m)
        else:
            hae = NON_NOTO
        t_pos = pos.get("time") or pos.get("timestamp")
        ora = time.time()
        t_fix = float(t_pos) if t_pos and ora - 7 * 86400 < float(t_pos) < ora + 300 else rx
        manuale = pos.get("locationSource") == "LOC_MANUAL"
        velocita = rotta = None
        if "groundSpeed" in pos:
            velocita = float(pos["groundSpeed"]) / 3.6  # km/h -> m/s
        if "groundTrack" in pos:
            rotta = (float(pos["groundTrack"]) / 1e5) % 360
        return Fonte(tipo_fonte="mesh", uid=f"!{num:08x}", lat=lat, lon=lon, hae=hae, ce=ce, t_fix=t_fix,
                     how="h-e" if manuale else "m-g", geosrc="USER" if manuale else "GPS",
                     altsrc="USER" if manuale else "GPS", rotta=rotta, velocita=velocita,
                     quota_msl=quota_msl, precisione_bit=bit, canale=canale)

    def _posizione(self, num: int, pos: dict, canale: int, rx: float) -> Optional["Fonte"]:
        if num == self.mio_num and not self.cfg.includi_ponte:
            return None
        if not self._canale_ok(num, canale):
            return None
        if self._info(num).get("user", {}).get("role") == "TAK":
            # Radio abbinata ad ATAK: la posizione buona e' quella che manda ATAK (porta 78)
            self.contatori["scartate_doppione_tak"] += 1
            return None
        fonte = self._fonte_mesh(num, pos, canale, rx)
        if fonte is None:
            return None
        with self._lock:
            st = self.stato.get(num)
            if st is None:
                st = self.stato[num] = StatoNodo(num, fonte, rx)
            else:
                st.fonte, st.ultimo_segnale = fonte, max(st.ultimo_segnale, rx)
            self._invia(st, time.time())
        return fonte

    def _tak_v2(self, num: int, carico: bytes, canale: int, rx: float) -> Optional["Fonte"]:
        """La posizione (Fonte) se il pacchetto la porta, per la mappa della copertura."""
        if not self._canale_ok(num, canale):
            return None
        if self.sdk is None:
            self._scarta(num, "tak_senza_libreria", "pacchetto TAK non leggibile: manca la libreria meshtastic-tak "
                         "(rifai l'installazione)")
            return None
        try:
            pkt = self.sdk.decodifica(carico)
        except PacchettoIllegibile as e:
            self._scarta(num, "tak_illeggibile", f"pacchetto TAK non decodificabile ({e})")
            return None
        impronta = self.sdk.impronta(pkt)
        self._sentiti_in_radio.aggiungi(impronta)
        for v in self.coda_radio.togli_impronta(impronta):
            self.contatori["radio_gia_trasmessi_da_altri"] += 1
            log.info("<- radio: %s gia' trasmesso da %s (!%08x): non lo ripeto", v.descrizione, self._nome(num), num)
        tipo = tipo_cot_v2(self.sdk.pb, pkt)
        if pkt.WhichOneof("payload_variant") is None and tipo.startswith("a-"):
            return self._pli_v2(num, pkt, tipo, canale, rx)
        self._oggetto_dalla_radio(num, pkt, impronta)
        return None

    def _pli_v2(self, num: int, pkt, tipo: str, canale: int, rx: float) -> Optional["Fonte"]:
        lat, lon = pkt.latitude_i * 1e-7, pkt.longitude_i * 1e-7
        if (pkt.latitude_i == 0 and pkt.longitude_i == 0) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        id_radio = f"!{num:08x}"
        tracker = (not pkt.uid) or pkt.uid == id_radio
        takv = None
        if pkt.tak_platform or pkt.tak_version:
            takv = {"device": pkt.tak_device, "platform": pkt.tak_platform, "os": pkt.tak_os,
                    "version": pkt.tak_version}
        hae = float(pkt.altitude) if abs(pkt.altitude) < 999999 else NON_NOTO
        fonte = Fonte(tipo_fonte="tak", uid=pkt.uid or id_radio, lat=lat, lon=lon, hae=hae, ce=NON_NOTO,
                      t_fix=rx, how=how_v2(self.sdk.pb, pkt), geosrc=SORGENTI.get(pkt.geo_src),
                      altsrc=SORGENTI.get(pkt.alt_src),
                      rotta=pkt.course / 100.0 if (pkt.course or pkt.speed) else None,
                      velocita=pkt.speed / 100.0 if (pkt.course or pkt.speed) else None,
                      canale=canale, tipo_cot=tipo,
                      callsign=pulisci(pkt.callsign, 80) or self._nome(num),
                      squadra=SQUADRE.get(pkt.team) or (self.cfg.squadra if tracker else None),
                      ruolo=RUOLI_TAK.get(pkt.role) or ("Team Member" if tracker else None),
                      batteria=pkt.battery or None, takv=takv,
                      droid=None if tracker else (pulisci(pkt.device_callsign, 80) or None),
                      endpoint=not tracker, tracker=tracker)
        self._salva_tak(num, fonte, rx)
        return fonte

    def _tak_v1(self, num: int, carico: bytes, canale: int, rx: float) -> Optional["Fonte"]:
        if not self._canale_ok(num, canale):
            return None
        try:
            from meshtastic.protobuf import atak_pb2
            pkt = atak_pb2.TAKPacket.FromString(carico)
        except Exception as e:  # noqa
            self._scarta(num, "tak_illeggibile", f"pacchetto TAK (vecchio formato) non decodificabile ({e})")
            return None
        if pkt.HasField("chat"):
            self._chat_v1(num, pkt)
            return None
        if not pkt.HasField("pli"):
            self.contatori["tak_v1_altro"] += 1
            return None
        lat, lon = pkt.pli.latitude_i * 1e-7, pkt.pli.longitude_i * 1e-7
        if (pkt.pli.latitude_i == 0 and pkt.pli.longitude_i == 0) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        id_radio = f"!{num:08x}"
        in_chiaro = not pkt.is_compressed
        utente_atak = (self._info(num).get("user", {}).get("role") == "TAK" and in_chiaro
                       and bool(pkt.contact.device_callsign))
        uid = pkt.contact.device_callsign.split("|", 1)[0] if utente_atak else id_radio
        callsign = (pulisci(pkt.contact.callsign, 80) if in_chiaro else "") or self._nome(num)
        tracker = not utente_atak
        fonte = Fonte(tipo_fonte="tak", uid=uid, lat=lat, lon=lon,
                      hae=float(pkt.pli.altitude) if abs(pkt.pli.altitude) < 999999 else NON_NOTO,
                      ce=NON_NOTO, t_fix=rx, how="m-g", geosrc="GPS", altsrc="GPS",
                      rotta=float(pkt.pli.course) if (pkt.pli.course or pkt.pli.speed) else None,
                      velocita=float(pkt.pli.speed) if (pkt.pli.course or pkt.pli.speed) else None,
                      canale=canale, callsign=callsign,
                      squadra=SQUADRE.get(pkt.group.team) or (self.cfg.squadra if tracker else None),
                      ruolo=RUOLI_TAK.get(pkt.group.role) or ("Team Member" if tracker else None),
                      batteria=pkt.status.battery or None, endpoint=utente_atak, tracker=tracker)
        self._salva_tak(num, fonte, rx)
        return fonte

    def _chat_v1(self, num: int, pkt) -> None:
        """Chat ATAK nel formato precedente (radio con firmware 2.7): convertita nel formato
        nuovo e ricostruita con la libreria TAK, come le altre chat."""
        if self.sdk is None:
            return
        if pkt.is_compressed:
            # testo compresso dal firmware 2.7 (unishox2): illeggibile qui. Una radio 2.7 consegna
            # anche la copia decompressa, che e' quella buona.
            self.contatori["chat_v1_compresse"] += 1
            return
        chat = pkt.chat
        if chat.receipt_type or not chat.message:
            return
        uid_mitt, _, id_msg = pkt.contact.device_callsign.partition("|")
        uid_mitt = uid_mitt or f"!{num:08x}"
        id_msg = id_msg or hashlib.sha256(f"{num}{chat.message}{pkt.contact.callsign}".encode()).hexdigest()[:8]
        privata = chat.HasField("to") and bool(chat.to)
        stanza = chat.to if privata else "All Chat Rooms"
        pb = self.sdk.pb
        nuovo = pb.TAKPacketV2(cot_type_id=pb.CotType.Value("CotType_b_t_f"),
                               how=pb.CotHow.Value("CotHow_h_g_i_g_o"),
                               callsign=pulisci(pkt.contact.callsign, 80) or self._nome(num),
                               uid=f"GeoChat.{uid_mitt}.{stanza}.{id_msg}",
                               team=pkt.group.team, role=pkt.group.role, stale_seconds=86400)
        nuovo.chat.message = chat.message
        nuovo.chat.to_callsign = nuovo.callsign  # nella libreria TAK qui va il mittente
        if privata:
            nuovo.chat.to = chat.to
            if chat.HasField("to_callsign") and chat.to_callsign:
                nuovo.marti.dest_callsign.append(chat.to_callsign)
        self._oggetto_dalla_radio(num, nuovo, self.sdk.impronta(nuovo))

    # ---- dalla radio al TAK Server: chat, segnaposti, forme... ----

    def _oggetto_dalla_radio(self, num: int, pkt, impronta: str) -> None:
        if impronta in self._visti_dal_server:
            # e' arrivato anche dal TAK Server: lo ha gia' portato la' un altro ponte, o viene dalla
            # sala operativa e un ponte l'ha appena trasmesso in radio
            self.contatori["radio_gia_sul_server"] += 1
            return
        if impronta in self._inoltrati_al_server:
            self.contatori["radio_doppioni"] += 1
            return
        self._inoltrati_al_server.aggiungi(impronta)
        try:
            xml = self.sdk.in_xml(pkt)
        except Exception as e:  # noqa
            self._scarta(num, "tak_illeggibile", f"oggetto TAK non ricostruibile ({e})")
            return
        if tipo_cot_v2(self.sdk.pb, pkt).startswith(TIPI_STATICI):
            xml = allunga_stale(xml, time.time())
        xml = marca_dalla_radio(xml, impronta, f"!{num:08x}", self.mio_uid)
        descrizione = f"{descrivi_pkt(self.sdk.pb, pkt)} da {self._nome(num)} (!{num:08x})"
        quando = time.time() + random.uniform(*self.ATTESA_VERSO_SERVER)
        with self._lock_uscite:
            self._verso_server.append((quando, impronta, xml.encode("utf-8"), descrizione))

    def _manda_al_server(self, impronta: str, xml: bytes, descrizione: str) -> None:
        if impronta in self._visti_dal_server:
            self.contatori["radio_gia_sul_server"] += 1
            log.info("-> TAK: %s gia' portato sul server da un altro gateway", descrizione)
            return
        self.tak.invia_una_volta(xml, time.time() + 3600)
        self.contatori["radio_al_server"] += 1
        log.info("-> TAK: %s", descrizione)

    # ---- dal TAK Server alla radio ----

    def su_evento_tak(self, evento: bytes) -> None:
        """Chiamato dal thread del TAK Server: il lavoro vero lo fa il thread delle uscite."""
        try:
            self._dal_server.put_nowait(evento)
        except queue.Full:
            self.contatori["server_coda_piena"] += 1

    def _dal_tak(self, grezzo: bytes) -> None:
        ora = time.time()
        try:
            radice = ET.fromstring(grezzo)
        except ET.ParseError:
            self.contatori["server_illeggibili"] += 1
            return
        if radice.tag != "event":
            return
        tipo = radice.get("type") or ""
        if tipo.startswith(("t-x-c-t", "t-x-takp")):
            return  # risposte ai ping e offerte di protocollo
        self.contatori["server_eventi"] += 1
        det = radice.find("detail")
        segno = det.find("__mesh2tak") if det is not None else None
        if segno is not None:
            if segno.get("h"):
                self._visti_dal_server.aggiungi(segno.get("h"))
            return
        if e_posizione_dispositivo(radice) and radice.get("uid"):
            self._collegati_internet.aggiungi(radice.get("uid"))
        if self.sdk is None or not self.cfg.invio_radio:
            return
        categoria, motivo = categoria_dal_server(radice)
        if categoria is None:
            self.contatori["server_non_per_radio"] += 1
            log.debug("Dal TAK Server, non per la radio (%s): %s %s", motivo, tipo, radice.get("uid"))
            return
        stale = leggi_ora_cot(radice.get("stale"))
        if stale is not None and stale <= ora:
            self.contatori["server_scaduti"] += 1
            return
        xml = grezzo.decode("utf-8", "replace")
        if tipo.startswith(TIPI_STATICI):
            xml = allunga_stale(xml, ora)
        try:
            pkt, filo = self.sdk.da_xml(xml)
        except OggettoTroppoGrande:
            self._troppo_grande(radice, categoria)
            return
        except Exception as e:  # noqa
            self.contatori["server_non_convertibili"] += 1
            log.info("Dal TAK Server: %s %s non convertibile per la radio (%s)", tipo, radice.get("uid"), e)
            return
        impronta = self.sdk.impronta(pkt)
        self._visti_dal_server.aggiungi(impronta)
        descrizione = descrivi_pkt(self.sdk.pb, pkt)
        if impronta in self._sentiti_in_radio:
            self.contatori["server_gia_in_radio"] += 1
            log.debug("Dal TAK Server: %s gia' sentito in radio", descrizione)
            return
        if impronta in self._trasmessi or self.coda_radio.contiene(impronta):
            return  # appena trasmesso, o gia' in coda identico (es. allarme ripetuto dal server)
        attesa = self.ATTESA_VERSO_RADIO_ALLARME if categoria == "emergenza" else self.ATTESA_VERSO_RADIO
        v = VoceRadio(priorita=PRIORITA[categoria], categoria=categoria, uid=radice.get("uid") or "",
                      descrizione=descrizione, filo=filo, impronta=impronta, creata=ora,
                      pronta=ora + random.uniform(*attesa), scadenza=stale or ora + 3600)
        sostituita = self.coda_radio.metti(v)
        log.info("<- TAK: %s%s (in coda per la radio, %d byte)", descrizione,
                 " - versione nuova" if sostituita else "", len(filo))

    def _troppo_grande(self, radice: ET.Element, categoria: str) -> None:
        self.contatori["server_troppo_grandi"] += 1
        uid_mitt, nominativo = mittente_evento(radice)
        nome = nome_evento(radice) or {"chat": "il messaggio", "oggetto": "l'oggetto"}.get(categoria, categoria)
        log.warning("Non trasmesso alle radio: %s «%s» di %s e' troppo grande per un pacchetto radio",
                    categoria, nome, nominativo or "?")
        chiave = radice.get("uid") or ""
        if not uid_mitt or not nominativo or chiave in self._avvisati:
            return
        self._avvisati.aggiungi(chiave)
        cosa = "Il messaggio" if categoria == "chat" else f"«{nome}»"
        testo = (f"{cosa} non e' stato inviato alle radio in campo: e' troppo grande per un pacchetto radio. "
                 + ("Accorcialo e rimandalo." if categoria == "chat"
                    else "Semplificalo (meno punti) o accorcia le note, poi rimandalo."))
        self.tak.invia_una_volta(cot_chat_diretta(da_uid=self.mio_uid, da_callsign=self._nome_ponte(),
                                                  a_uid=uid_mitt, a_callsign=nominativo, testo=testo,
                                                  ora=time.time()), time.time() + 3600)

    def _nome_ponte(self) -> str:
        return self._nome(self.mio_num) if self.mio_num else "Gateway Meshtastic"

    # ---- thread delle uscite: verso il server (con attesa) e verso la radio ----

    def avvia_uscite(self) -> None:
        self._thread_uscite = threading.Thread(target=self._ciclo_uscite, name="uscite", daemon=True)
        self._thread_uscite.start()

    def ferma_uscite(self) -> None:
        self._fermo.set()
        if self._thread_uscite is not None:
            self._thread_uscite.join(timeout=5)

    def _ciclo_uscite(self) -> None:
        while not self._fermo.is_set():
            try:
                self.passo_uscite(attesa=0.25)
            except Exception:  # noqa
                log.exception("Errore inatteso nelle uscite verso radio e server")
                self._fermo.wait(1)

    def passo_uscite(self, attesa: float = 0.0) -> None:
        """Un giro: un evento dal server, gli inoltri al server maturati, una trasmissione radio."""
        try:
            grezzo = self._dal_server.get(timeout=attesa) if attesa else self._dal_server.get_nowait()
        except queue.Empty:
            grezzo = None
        if grezzo is not None:
            self._dal_tak(grezzo)
        ora = time.time()
        with self._lock_uscite:
            pronti = [x for x in self._verso_server if x[0] <= ora]
            self._verso_server = [x for x in self._verso_server if x[0] > ora]
        for _, impronta, xml, descrizione in pronti:
            self._manda_al_server(impronta, xml, descrizione)
        self._forse_trasmetti(ora)

    def _ricarica(self, ora: float) -> None:
        quota = self.cfg.tempo_radio_percento / 100.0
        self._credito_radio = min(self._credito_radio + (ora - self._ultima_ricarica) * quota,
                                  self.RISERVA_RADIO_S * quota)
        self._ultima_ricarica = ora

    def _forse_trasmetti(self, ora: float) -> None:
        for v in self.coda_radio.scadute(ora):
            self.contatori["radio_scaduti_in_coda"] += 1
            log.warning("Non trasmesso alle radio: %s (rimasto in coda troppo a lungo o scaduto)", v.descrizione)
        if ora - self._ultima_trasmissione < self.SPAZIO_TRA_INVII_S:
            return
        v = self.coda_radio.guarda(ora)
        if v is None:
            return
        self._ricarica(ora)
        durata = tempo_in_aria(len(v.filo), self.preset)
        if v.priorita > 0 and self._credito_radio < durata:
            return  # aspetta che si liberi tempo radio; gli allarmi passano comunque
        v = self.coda_radio.prossima(ora)
        if v is None:
            return
        durata = tempo_in_aria(len(v.filo), self.preset)
        if not self._trasmetti(v):
            self.coda_radio.rimetti(v)
            self._ultima_trasmissione = ora  # riprova piu' tardi, senza insistere
            return
        self._credito_radio -= durata
        self._ultima_trasmissione = ora
        self._trasmessi.aggiungi(v.impronta)
        self.contatori["radio_trasmessi"] += 1
        log.info("-> radio: %s (%d byte, %.1f s in aria)", v.descrizione, len(v.filo), durata)

    def _trasmetti(self, v: VoceRadio) -> bool:
        iface = self.iface
        canale = self._canale_invio()
        if iface is None or canale is None:
            return False
        try:
            with self._lock_invio:
                iface.sendData(v.filo, destinationId=BROADCAST, portNum=PORTA_ATAK_V2, wantAck=False,
                               channelIndex=canale, hopLimit=self.hop_limit)
        except Exception as e:  # noqa
            log.warning("Trasmissione alla radio non riuscita (%s): riprovo", e)
            return False
        return True

    def _salva_tak(self, num: int, fonte: Fonte, rx: float) -> None:
        with self._lock:
            st = self.stato.get(num)
            if st is None:
                st = self.stato[num] = StatoNodo(num, fonte, rx)
            else:
                st.fonte, st.ultimo_segnale = fonte, max(st.ultimo_segnale, rx)
            self._invia(st, time.time())

    def _attivita(self, num: int, rx: float, porta: int) -> None:
        """Qualsiasi pacchetto di una radio nota la tiene viva sulla mappa."""
        with self._lock:
            st = self.stato.get(num)
            if st is None:
                return
            st.ultimo_segnale = max(st.ultimo_segnale, rx)
            ora = time.time()
            nome_cambiato = (porta == PORTA_NODEINFO and st.fonte.tipo_fonte == "mesh"
                             and self._nome(num) != st.nome_inviato)
            if nome_cambiato or ora - st.ultimo_invio >= self.cfg.aggiornamento_min * 60:
                self._invia(st, ora, ripetizione=not nome_cambiato)

    # ---- costruzione e invio CoT ----

    def _ora_locale(self, ts: float) -> str:
        dt = datetime.fromtimestamp(ts, self.tz) if self.tz else datetime.fromtimestamp(ts)
        oggi = (datetime.now(self.tz) if self.tz else datetime.now()).date()
        return dt.strftime("%H:%M") if dt.date() == oggi else dt.strftime("%d/%m %H:%M")

    def _invia(self, st: StatoNodo, ora: float, ripetizione: bool = False) -> None:
        """Manda (o ripete) la posizione di una radio al TAK Server.

        L'evento porta l'ora dell'ultimo segnale ricevuto, non quella dell'invio: chi apre l'app
        dopo vede da quanto la radio non si fa sentire. Il ponte stesso e' sempre "adesso".
        """
        f = st.fonte
        ponte = st.num == self.mio_num
        if f.tipo_fonte == "tak" and not ponte and f.uid in self._collegati_internet:
            # il telefono manda gia' la sua posizione via internet, piu' fresca di questa
            if not ripetizione:
                self.contatori["scartate_collegate_via_internet"] += 1
                log.debug("Non inoltro %s: e' collegato al TAK Server via internet", f.callsign or f.uid)
            return
        stale = ora + self.cfg.validita_min * 60 if ponte else st.ultimo_segnale + self.cfg.validita_min * 60
        if stale <= ora + 30:
            return
        nome, xml = self._cot(st, ora if ponte else min(st.ultimo_segnale, ora), stale)
        self.tak.invia(f.uid, xml, stale)
        st.ultimo_invio = ora
        st.nome_inviato = nome
        self.contatori["inoltrate"] += 1
        if ripetizione:
            self.contatori["ripetute"] += 1
        canale = self.canali.get(f.canale)
        (log.debug if ripetizione else log.info)(
            "-> TAK: %s (!%08x) %.5f, %.5f%s%s%s", nome, st.num, f.lat, f.lon,
            f" ±{f.ce:.0f} m" if f.ce < NON_NOTO else "",
            f" [canale {canale.nome}]" if canale and not ponte else "",
            " (ripetuta)" if ripetizione else "")

    def _cot(self, st: StatoNodo, t_evento: float, stale: float) -> Tuple[str, bytes]:
        f = st.fonte
        info = self._info(st.num)
        utente = info.get("user", {})
        metriche = info.get("deviceMetrics", {})
        livello = metriche.get("batteryLevel")
        batteria = int(livello) if livello and 0 < livello <= 100 else None
        id_radio = f"!{st.num:08x}"

        segno = {"radio": id_radio, "ponte": self.mio_uid}
        st.batteria = f.batteria or batteria or st.batteria
        st.salti = info.get("hopsAway") if "hopsAway" in info else st.salti
        if f.tipo_fonte == "tak" and not f.tracker:
            note = f"Ricevuta via Meshtastic dalla radio {id_radio} alle {self._ora_locale(st.ultimo_segnale)}"
            xml = cot_evento(uid=f.uid, tipo=f.tipo_cot, how=f.how, lat=f.lat, lon=f.lon, hae=f.hae,
                             ce=f.ce, t_evento=t_evento, stale=stale, callsign=f.callsign, endpoint=f.endpoint,
                             squadra=f.squadra, ruolo=f.ruolo, batteria=f.batteria, rotta=f.rotta,
                             velocita=f.velocita, geosrc=f.geosrc, altsrc=f.altsrc, takv=f.takv,
                             droid=f.droid, note=note, segno=segno)
            return f.callsign or id_radio, xml

        ponte = st.num == self.mio_num
        ruolo_radio = utente.get("role", "CLIENT")
        fisso = ponte or ruolo_radio in RUOLI_FISSI
        nome = f.callsign if f.tipo_fonte == "tak" else self._nome(st.num)
        hw = utente.get("hwModel")
        dettagli = [x for x in ((hw if hw and hw != "UNSET" else None), ruolo_radio) if x]
        intest = ("Gateway Meshtastic verso TAK" if ponte else "Radio Meshtastic") + f" {id_radio}"
        if dettagli:
            intest += " (" + ", ".join(dettagli) + ")"
        parti = [intest, f"posizione delle {self._ora_locale(f.t_fix)}"]
        if ponte and self.nota_ponte:
            parti.insert(0, self.nota_ponte)
        if not ponte:
            parti.append(f"ultimo segnale alle {self._ora_locale(st.ultimo_segnale)}")
        if f.precisione_bit < 32 and f.ce < NON_NOTO:
            parti.append(f"posizione approssimata ±{f.ce:.0f} m")
        if f.quota_msl is not None:
            parti.append(f"quota {f.quota_msl:.0f} m s.l.m.")
        b = f.batteria or batteria
        if b:
            parti.append(f"batteria {b}%")
        elif livello and livello > 100:
            parti.append("alimentata da rete")
        salti = info.get("hopsAway")
        if not ponte and salti is not None:
            parti.append("ricevuta direttamente" if salti == 0 else
                         ("1 salto radio" if salti == 1 else f"{salti} salti radio"))
        tipo = "a-f-G-I" if fisso else (f.tipo_cot or "a-f-G-U-C")
        icona = self.cfg.icona_ponte if ponte else None
        squadra = None if fisso else (f.squadra or self.cfg.squadra)
        ruolo = None if fisso else (f.ruolo or "Team Member")
        scelta = self.icone.di(id_radio)
        if scelta:
            # icona scelta dalla pagina della rete: al posto del colore della squadra (TAK Aware), e un tipo che
            # da' un simbolo sensato a chi non ha l'insieme di icone
            tipo, icona = scelta
            squadra = ruolo = None
        xml = cot_evento(
            uid=f.uid, tipo=tipo, how=f.how,
            lat=f.lat, lon=f.lon, hae=f.hae, ce=f.ce, t_evento=t_evento, stale=stale, callsign=nome,
            squadra=squadra, ruolo=ruolo,
            batteria=None if fisso else b, rotta=f.rotta, velocita=f.velocita,
            geosrc=f.geosrc, altsrc=f.altsrc, note=" · ".join(parti), segno=segno, icona=icona)
        return nome, xml

    def imposta_icone(self, corpo: Optional[bytes]) -> Tuple[int, dict]:
        """Icone arrivate dalla pagina della rete: le radio che cambiano icona si rimandano al prossimo giro di
        manutenzione (pochi secondi), senza aspettare i 2 minuti della ripetizione."""
        stato, risposta, cambiate = self.icone.imposta(corpo)
        if cambiate:
            with self._lock:
                for st in self.stato.values():
                    if f"!{st.num:08x}" in cambiate:
                        st.ultimo_invio = 0.0
        return stato, risposta

    def annuncia_pausa(self, nota: str) -> None:
        """Prima di lasciare il nodo alla pagina di gestione: chi guarda la mappa lo legge nel ponte."""
        with self._lock:
            self.nota_ponte = nota
            st = self.stato.get(self.mio_num)
            if st is not None and self.cfg.includi_ponte:
                ora = time.time()
                st.ultimo_segnale = ora
                self._invia(st, ora)

    def istantanea(self) -> dict:
        """Stato del ponte per la pagina della rete: nomi, orari e numeri. Niente chiavi ne' coordinate."""
        ora = time.time()
        validita = self.cfg.validita_min * 60
        with self._lock:
            radio = []
            for st in list(self.stato.values()):
                if st.num == self.mio_num:
                    continue
                f = st.fonte
                canale = self.canali.get(f.canale)
                radio.append({
                    "id": f"!{st.num:08x}",
                    "nome": pulisci(st.nome_inviato or self._nome(st.num), 60),
                    "app_tak": f.tipo_fonte == "tak",
                    "ultimo_segnale": round(st.ultimo_segnale),
                    "posizione": round(f.t_fix) if f.t_fix else None,
                    "batteria": st.batteria,
                    "salti": st.salti,
                    "canale": canale.nome if canale else None,
                    "sulla_mappa": st.ultimo_segnale + validita > ora,
                })
            radio.sort(key=lambda r: -r["ultimo_segnale"])
            canali = [{"indice": c.indice, "nome": c.nome, "privato": c.privato,
                       "inoltrato": c.indice in self.canali_ok}
                      for c in sorted(self.canali.values(), key=lambda c: c.indice)]
            contatori = dict(self.contatori)
            viste = sorted(self.viste.values(), key=lambda v: -v.ultimo)[:200]
            radio_rete = [{"id": f"!{v.num:08x}", "nome": v.nome or self._nome(v.num), "nome_breve": v.nome_breve,
                           "modello": v.modello, "ruolo": v.ruolo, "ultimo": round(v.ultimo),
                           "batteria": v.batteria, "tensione": v.tensione, "occupato": v.occupato,
                           "trasmesso": v.trasmesso, "acceso_s": v.acceso_s,
                           "metriche": round(v.metriche) if v.metriche else None, "salti": v.salti,
                           "snr": v.snr, "rssi": v.rssi, "diretto": round(v.diretto) if v.diretto else None,
                           "fisso": v.fisso, "posizione_fissa": v.posizione_fissa, "ripetuti": v.ripetuti,
                           "ultimo_ripetuto": round(v.ultimo_ripetuto) if v.ultimo_ripetuto else None}
                          for v in viste]
            canale_radio = dict(self.canale_locale)
            if "quando" not in canale_radio:       # prima della misura del minuto: quella del database del nodo
                dm = self._info(self.mio_num).get("deviceMetrics") or {}
                if "uptimeSeconds" in dm:
                    canale_radio = {"occupato": numero(dm.get("channelUtilization", 0.0), 0, 100, 1),
                                    "trasmesso": numero(dm.get("airUtilTx", 0.0), 0, 100, 2), "quando": None}
            if canale_radio.get("quando"):
                canale_radio["quando"] = round(canale_radio["quando"])
            return {
                "nome": self.nodo_nome,
                "nome_breve": self.nodo_nome_breve,
                "id_nodo": f"!{self.mio_num:08x}" if self.mio_num else None,
                "chiave_pubblica": self.nodo_chiave_pubblica or None,
                "nodo": {"collegato": self.iface is not None,
                         "dal": round(self.nodo_collegato_dal) if self.nodo_collegato_dal else None,
                         "collegamento": descrivi_collegamento(self.cfg.collegamento),
                         "preset": nome_preset(self.preset), "canali": canali},
                "radio": radio[:150],
                "radio_rete": radio_rete,
                "canale_radio": canale_radio or None,
                "radio_sulla_mappa": sum(1 for r in radio if r["sulla_mappa"]),
                "validita_min": self.cfg.validita_min,
                "conteggi": {k: contatori.get(k, 0) for k in
                             ("inoltrate", "ripetute", "radio_al_server", "server_eventi", "radio_trasmessi")},
                "conteggi_dal": round(self._ultimo_riepilogo),
                "coda_radio": len(self.coda_radio),
            }

    # ---- manutenzione periodica (thread principale) ----

    def manutenzione(self) -> None:
        """Ogni pochi secondi: ripete al TAK Server le posizioni ancora valide e fa il riepilogo orario.

        Il TAK Server non rimanda a chi si collega dopo le posizioni portate dal ponte: senza
        ripeterle, chi apre l'app vedrebbe una radio solo al suo invio successivo, e una radio
        uscita dalla copertura (l'ultima posizione nota) non la vedrebbe affatto.
        """
        ora = time.time()
        intervallo = self.cfg.aggiornamento_min * 60
        with self._lock:
            for st in list(self.stato.values()):
                if ora - st.ultimo_invio < intervallo:
                    continue
                if st.num == self.mio_num:
                    if not self.cfg.includi_ponte:
                        continue
                    st.ultimo_segnale = ora
                self._invia(st, ora, ripetizione=True)
            for num in [n for n, s in self.stato.items() if ora - s.ultimo_segnale > 86400]:
                del self.stato[num]
        if ora - self._ultimo_riepilogo >= 3600:
            c, self.contatori = self.contatori, Counter()
            self._ultimo_riepilogo = ora
            scartate = {k[len("scartate_"):]: v for k, v in c.items() if k.startswith("scartate_")}
            sulla_mappa = sum(1 for s in self.stato.values()
                              if s.ultimo_segnale + self.cfg.validita_min * 60 > ora or s.num == self.mio_num)
            log.info("Ultima ora: %d invii al TAK Server (%d ripetizioni per chi apre l'app dopo), "
                     "%d radio sulla mappa%s", c.get("inoltrate", 0), c.get("ripetute", 0), sulla_mappa,
                     ("; scartate: " + ", ".join(f"{k} {v}" for k, v in sorted(scartate.items())))
                     if scartate else "")
            log.info("Ultima ora: dalla radio al server %d chat/oggetti; dal server %d eventi, %d trasmessi alle "
                     "radio, %d in coda, %d troppo grandi", c.get("radio_al_server", 0), c.get("server_eventi", 0),
                     c.get("radio_trasmessi", 0), len(self.coda_radio), c.get("server_troppo_grandi", 0))
        self.copertura.salva()          # al massimo ogni 10 minuti, e solo se e' cambiata
        self.squadra.salva()
        self._campiona_storico(ora)
        self.storico.salva()

    def _campiona_storico(self, ora: float) -> None:
        """Ogni mezz'ora un campione per i grafici: il ponte e ogni ripetitore fisso della squadra."""
        if ora - self.storico._ultimo < Storico.OGNI_S:
            return
        with self._lock:
            canale = dict(self.canale_locale)
            sulla_mappa = sum(1 for s in self.stato.values()
                              if s.num != self.mio_num and s.ultimo_segnale + self.cfg.validita_min * 60 > ora)
            ripetitori = []
            for v in self.viste.values():
                if not v.fisso:
                    continue
                recente = v.metriche is not None and ora - v.metriche < 2 * Storico.OGNI_S
                sentito = max(v.ultimo, v.ultimo_ripetuto or 0.0) > ora - Storico.OGNI_S
                ripetitori.append((f"!{v.num:08x}", v.nome or self._nome(v.num),
                                   [v.batteria if recente else None, v.tensione if recente else None,
                                    v.snr if v.diretto is not None and ora - v.diretto < Storico.OGNI_S else None,
                                    1 if sentito else 0], v.ripetuti))
        collegato = getattr(self.tak, "collegato", None)
        self.storico.campiona(ora, [canale.get("occupato"), canale.get("trasmesso"), sulla_mappa,
                                    1 if collegato is not None and collegato.is_set() else 0], ripetitori)


