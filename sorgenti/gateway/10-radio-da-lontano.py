# --------------------------------------------------------------------------
# Gestione da lontano delle radio della squadra (dalla 1.10.0)
# --------------------------------------------------------------------------

PORTA_ADMIN = 6

ERRORI_RADIO = {
    "ADMIN_PUBLIC_KEY_UNAUTHORIZED": "su questa radio non c'e' la chiave di amministrazione di questo gateway: va "
                                     "inserita una volta, da vicino, con l'app Meshtastic (Sicurezza > Chiave "
                                     "amministratore)",
    "NOT_AUTHORIZED": "la radio non accetta comandi da questo gateway: manca la sua chiave di amministrazione (app "
                      "Meshtastic, da vicino: Sicurezza > Chiave amministratore)",
    "ADMIN_BAD_SESSION_KEY": "la radio ha rifiutato la sessione (scaduta): riprova",
    "PKI_UNKNOWN_PUBKEY": "il nodo del gateway non conosce ancora la chiave pubblica di questa radio: aspetta che la "
                          "radio si faccia sentire (manda il suo nome ogni qualche ora) e riprova",
    "PKI_SEND_FAIL_PUBLIC_KEY": "il nodo del gateway non conosce ancora la chiave pubblica di questa radio: aspetta "
                                "che la radio si faccia sentire e riprova",
    "PKI_FAILED": "cifratura non riuscita: la chiave pubblica che il gateway ricorda non e' piu' quella della radio "
                  "(radio azzerata?). Dal nodo del gateway togli quella radio dall'elenco e aspetta che si ripresenti",
    "MAX_RETRANSMIT": "la radio non risponde: spenta, fuori portata o in risparmio di energia",
    "NO_RESPONSE": "la radio non risponde a questa richiesta",
    "NO_ROUTE": "nessuna strada radio verso questa radio in questo momento",
    "TIMEOUT": "la radio non risponde: spenta, fuori portata o in risparmio di energia",
    "DUTY_CYCLE_LIMIT": "il nodo del gateway ha finito il tempo radio consentito per quest'ora: riprova piu' tardi",
    "TOO_LARGE": "troppi dati per un solo pacchetto radio",
    "BAD_REQUEST": "la radio non ha capito la richiesta (firmware troppo vecchio?)",
    "NO_CHANNEL": "il nodo del gateway e la radio non hanno un canale in comune",
}


class ErroreRadio(Exception):
    pass


class GestioneRadio:
    """Le radio della squadra gestite dalla pagina della rete, attraverso il nodo di questo ponte.

    Sono i messaggi di amministrazione di Meshtastic, cifrati con la chiave pubblica della radio: la radio li
    accetta solo se tra le sue "chiavi amministratore" c'e' la chiave pubblica del nodo del ponte. Ogni richiesta
    e' un pacchetto radio (e la risposta un altro): un'operazione alla volta, in coda, con il suo esito da
    rileggere. Le chiavi dei canali non escono mai dal ponte: alla pagina va solo "privata, uguale a quella del
    canale X del ponte". Solo le radio sentite sul canale della squadra.
    """

    ATTESA_RISPOSTA_S = 75.0       # il firmware ritrasmette da solo fino a tre volte
    SESSIONE_S = 240.0             # la radio accetta la chiave di sessione per 300 s
    LETTURA_VALE_S = 1800.0        # per cambiare una sezione va riletta da meno di mezz'ora
    MAX_OPERAZIONI = 40
    MAX_IN_CODA = 8
    AZIONI = ("leggi", "scrivi", "proprietario", "canale", "posizione_fissa", "togli_posizione_fissa", "riavvia")
    RE_RADIO = re.compile(r"^!([0-9a-f]{8})$")

    def __init__(self, ponte: "Ponte"):
        self.ponte = ponte
        self._lock = threading.Lock()
        self._ops: "OrderedDict[str, dict]" = OrderedDict()
        self._coda: "queue.Queue[str]" = queue.Queue()
        self._lette: Dict[Tuple[int, str], Tuple[float, object]] = {}
        self._sessioni: Dict[int, Tuple[float, bytes]] = {}
        self._sezioni: Optional[Dict[str, Tuple[str, int, object]]] = None
        self._schema: Optional[dict] = None
        self._thread: Optional[threading.Thread] = None

    # ---- sezioni delle impostazioni, dal protocollo stesso (cosi' seguono la libreria installata) ----

    def sezioni(self) -> Dict[str, Tuple[str, int, object]]:
        if self._sezioni is None:
            from meshtastic.protobuf import admin_pb2, localonly_pb2
            sez: Dict[str, Tuple[str, int, object]] = {}
            tipi = admin_pb2.AdminMessage.ConfigType
            for c in localonly_pb2.LocalConfig.DESCRIPTOR.fields:
                nome = c.name.upper() + "_CONFIG"
                if c.message_type is not None and nome in tipi.keys():
                    sez[c.name] = ("config", tipi.Value(nome), c.message_type)
            moduli = admin_pb2.AdminMessage.DESCRIPTOR.fields_by_name["set_module_config"].message_type
            for c in localonly_pb2.LocalModuleConfig.DESCRIPTOR.fields:
                if c.message_type is not None and c.name in moduli.fields_by_name:
                    sez[c.name] = ("modulo", c.index, c.message_type)
            self._sezioni = sez
        return self._sezioni

    @classmethod
    def _campi(cls, descrittore, profondita: int = 0) -> List[dict]:
        campi = []
        for c in descrittore.fields:
            if c.GetOptions().deprecated or profondita > 3:
                continue
            ripetuto = getattr(c, "is_repeated", None)
            if ripetuto is None:
                ripetuto = c.label == c.LABEL_REPEATED
            v: dict = {"nome": c.name, "ripetuto": bool(ripetuto)}
            if c.type == c.TYPE_MESSAGE:
                v.update(tipo="gruppo", campi=cls._campi(c.message_type, profondita + 1))
            elif c.type == c.TYPE_ENUM:
                v.update(tipo="scelta", opzioni=[x.name for x in c.enum_type.values])
            elif c.type == c.TYPE_BOOL:
                v["tipo"] = "si_no"
            elif c.type == c.TYPE_STRING:
                v["tipo"] = "testo"
            elif c.type == c.TYPE_BYTES:
                v["tipo"] = "byte"
            elif c.type in (c.TYPE_FLOAT, c.TYPE_DOUBLE):
                v["tipo"] = "decimale"
            else:
                v["tipo"] = "intero"
            campi.append(v)
        return campi

    def schema(self) -> dict:
        """Per la pagina: le sezioni e i loro campi, con i nomi del protocollo Meshtastic (quelli dell'app)."""
        if self._schema is None:
            from meshtastic.protobuf import channel_pb2, config_pb2
            self._schema = {
                "sezioni": [{"nome": n, "modulo": t == "modulo", "sola_lettura": n == "security",
                             "campi": self._campi(d)} for n, (t, _i, d) in self.sezioni().items()],
                "ruoli_canale": list(channel_pb2.Channel.Role.keys()),
                "ruoli": list(config_pb2.Config.DeviceConfig.Role.keys()),
            }
        return self._schema

    @staticmethod
    def _in_dizionario(msg) -> dict:
        from google.protobuf import json_format
        try:
            return json_format.MessageToDict(msg, preserving_proto_field_name=True,
                                             always_print_fields_with_no_presence=True)
        except TypeError:           # protobuf piu' vecchio
            return json_format.MessageToDict(msg, preserving_proto_field_name=True,
                                             including_default_value_fields=True)

    # ---- richieste dalla pagina della rete ----

    def avvia(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._ciclo, name="gestione-radio", daemon=True)
            self._thread.start()

    def servi(self, h, percorso: str, corpo: Optional[bytes]) -> Tuple[int, dict]:
        """Risposta (stato HTTP, JSON) a una richiesta /mesh2tak/radio/... arrivata dal tunnel."""
        chi = re.sub(r"[^\w.@+-]", "", pulisci(h.headers.get("X-Authentik-Username"), 40))
        if h.client_address[0] not in ("127.0.0.1", "::1") or not chi:
            return 403, {"errore": "Le radio si gestiscono solo dalla pagina della rete."}
        dove = percorso[len("/mesh2tak/radio"):]
        if h.command == "GET" and dove == "/schema":
            return 200, self.schema()
        if h.command == "GET" and dove == "/op":
            ident = urllib.parse.parse_qs(urllib.parse.urlsplit(h.path).query).get("id", [""])[0]
            with self._lock:
                op = self._ops.get(ident)
                if op is None:
                    return 404, {"errore": "operazione non trovata (gateway riavviato?)"}
                return 200, json.loads(json.dumps({k: v for k, v in op.items() if not k.startswith("_")}))
        if h.command == "GET" and dove == "/ops":
            with self._lock:
                return 200, {"operazioni": [{k: v for k, v in op.items() if k != "risultato" and not k.startswith("_")}
                                            for op in list(self._ops.values())[-15:]]}
        if h.command == "POST" and dove == "/op":
            origine = urllib.parse.urlsplit(h.headers.get("Origin") or "").netloc
            ospite = h.headers.get("X-Forwarded-Host") or h.headers.get("Host") or ""
            if h.headers.get("X-Mesh2tak") != "1" or (origine and origine != ospite):
                return 403, {"errore": "richiesta non valida"}
            try:
                dati = json.loads((corpo or b"")[:16384].decode("utf-8"))
                if not isinstance(dati, dict):
                    raise ValueError
            except (ValueError, UnicodeDecodeError):
                return 400, {"errore": "richiesta non valida"}
            try:
                return 202, {"op": self.accoda(dati, chi)}
            except ErroreRadio as e:
                return 400, {"errore": str(e)}
        return 404, {"errore": "non trovato"}

    def accoda(self, dati: dict, chi: str) -> str:
        m = self.RE_RADIO.match(str(dati.get("radio") or ""))
        azione = str(dati.get("azione") or "")
        if not m or azione not in self.AZIONI:
            raise ErroreRadio("richiesta non valida")
        num = int(m.group(1), 16)
        with self.ponte._lock:
            vista = self.ponte.viste.get(num)
            nome = (vista.nome or self.ponte._nome(num)) if vista else ""
        if vista is None or num == self.ponte.mio_num:
            raise ErroreRadio("questo gateway non ha sentito quella radio sul canale della squadra")
        param = self._controlla(azione, dati)
        with self._lock:
            if sum(1 for op in self._ops.values() if op["stato"] in ("in_coda", "in_corso")) >= self.MAX_IN_CODA:
                raise ErroreRadio("troppe operazioni in coda su questo gateway: aspetta che finiscano")
            ident = uuid.uuid4().hex[:16]
            self._ops[ident] = {"id": ident, "radio": f"!{num:08x}", "nome": nome, "azione": azione, "stato": "in_coda",
                                "passo": "in coda", "risultato": {}, "errore": None, "chi": chi,
                                "creata": round(time.time()), "finita": None, "_num": num, "_param": param}
            while len(self._ops) > self.MAX_OPERAZIONI:
                self._ops.popitem(last=False)
        log.info("Gestione radio: %s chiede '%s'%s su %s (!%08x)", chi, azione,
                 f" {param.get('sezione') or ','.join(param.get('sezioni', []))}".rstrip(), nome, num)
        self.avvia()
        self._coda.put(ident)
        return ident

    def _controlla(self, azione: str, dati: dict) -> dict:
        """I parametri di un'operazione, controllati prima di metterla in coda."""
        sezioni = self.sezioni()
        if azione == "leggi":
            chieste = dati.get("sezioni")
            ammesse = set(sezioni) | {"proprietario", "info", "canali"}
            if not isinstance(chieste, list) or not chieste or len(chieste) > 6 or not set(chieste) <= ammesse:
                raise ErroreRadio("sezioni da leggere non valide")
            return {"sezioni": list(dict.fromkeys(chieste))}
        if azione == "scrivi":
            sez = str(dati.get("sezione") or "")
            if sez not in sezioni or not isinstance(dati.get("valori"), dict):
                raise ErroreRadio("sezione non valida")
            if sez == "security":
                raise ErroreRadio("la sezione Sicurezza da lontano si puo' solo leggere: cambiarla senza la chiave "
                                  "privata della radio puo' farle perdere l'identita'. Si cambia da vicino, con l'app")
            return {"sezione": sez, "valori": dati["valori"]}
        if azione == "proprietario":
            nome, breve = pulisci(dati.get("nome"), 60), pulisci(dati.get("nome_breve"), 8)
            if not nome or not breve or len(nome.encode()) > 39 or len(breve.encode()) > 4:
                raise ErroreRadio("nome: al massimo 39 caratteri; sigla: al massimo 4")
            return {"nome": nome, "nome_breve": breve}
        if azione == "canale":
            from meshtastic.protobuf import channel_pb2
            try:
                indice = int(dati.get("indice"))
                precisione = int(dati.get("precisione") or 0)
            except (TypeError, ValueError):
                raise ErroreRadio("canale non valido")
            ruolo, nome = str(dati.get("ruolo") or ""), str(dati.get("nome") or "")
            chiave = dati.get("chiave") if isinstance(dati.get("chiave"), dict) else {}
            modo = str(chiave.get("modo") or "tieni")
            if (not 0 <= indice <= 7 or ruolo not in channel_pb2.Channel.Role.keys() or len(nome.encode()) > 11
                    or not re.match(r"^[\w .+-]*$", nome) or not 0 <= precisione <= 32
                    or modo not in ("tieni", "ponte", "nuova", "predefinita", "nessuna")):
                raise ErroreRadio("canale non valido")
            if (indice == 0) != (ruolo == "PRIMARY"):
                raise ErroreRadio("il canale 0 e' sempre il primario, gli altri mai")
            p = {"indice": indice, "ruolo": ruolo, "nome": nome, "modo": modo, "precisione": precisione,
                 "uplink": dati.get("uplink") is True, "downlink": dati.get("downlink") is True}
            if modo == "nuova":
                try:
                    p["psk"] = leggi_chiave_canale(str(chiave.get("valore") or ""))
                except ValueError as e:
                    raise ErroreRadio(str(e))
            elif modo == "ponte":
                try:
                    p["canale_ponte"] = int(chiave.get("canale_ponte"))
                except (TypeError, ValueError):
                    raise ErroreRadio("canale del gateway non valido")
            return p
        if azione == "posizione_fissa":
            try:
                lat, lon, quota = float(dati.get("lat")), float(dati.get("lon")), int(float(dati.get("quota") or 0))
            except (TypeError, ValueError):
                raise ErroreRadio("coordinate non valide")
            if not (-90 <= lat <= 90 and -180 <= lon <= 180 and -500 <= quota <= 9000) or (lat == 0 and lon == 0):
                raise ErroreRadio("coordinate non valide")
            return {"lat": lat, "lon": lon, "quota": quota}
        return {}

    # ---- lavoro: un'operazione alla volta ----

    def _ciclo(self) -> None:
        while True:
            ident = self._coda.get()
            with self._lock:
                op = self._ops.get(ident)
                if op is None:
                    continue
                op.update(stato="in_corso", passo="parto")
            try:
                self._esegui(op)
                esito, errore = "fatto", None
            except ErroreRadio as e:
                esito, errore = "errore", str(e)
            except Exception as e:  # noqa
                log.exception("Gestione radio: errore inatteso")
                esito, errore = "errore", f"errore inatteso del gateway ({e})"
            with self._lock:
                op.update(stato=esito, errore=errore, finita=round(time.time()))
                if esito == "fatto":
                    op["passo"] = "fatto"
            log.info("Gestione radio: '%s' su %s: %s", op["azione"], op["nome"] or op["radio"],
                     "fatto" if errore is None else f"non riuscito ({errore})")

    def _passo(self, op: dict, testo: str) -> None:
        with self._lock:
            op["passo"] = testo

    def _esegui(self, op: dict) -> None:
        from meshtastic.protobuf import admin_pb2, mesh_pb2
        num, par, azione = op["_num"], op["_param"], op["azione"]
        if azione == "leggi":
            for i, sez in enumerate(par["sezioni"]):
                self._passo(op, f"chiedo '{sez}' alla radio ({i + 1} di {len(par['sezioni'])}): puo' volerci un minuto")
                valore = self._leggi(op, num, sez)
                with self._lock:
                    op["risultato"][sez] = valore
            return
        msg = admin_pb2.AdminMessage()
        fatto = "Fatto."
        if azione == "scrivi":
            sez = par["sezione"]
            tipo, _indice, _d = self.sezioni()[sez]
            nuova = self._modifica(num, sez, par["valori"])
            getattr(msg.set_config if tipo == "config" else msg.set_module_config, sez).CopyFrom(nuova)
            fatto = "Salvato: la radio si riavvia da sola per applicarlo (qualche secondo)."
        elif azione == "proprietario":
            msg.set_owner.long_name, msg.set_owner.short_name = par["nome"], par["nome_breve"]
            vecchio = self._letta(num, "proprietario")
            if vecchio is not None:
                msg.set_owner.is_licensed = vecchio.is_licensed
            fatto = "Nome cambiato: le altre radio lo vedono quando la radio lo ritrasmette."
        elif azione == "canale":
            msg.set_channel.CopyFrom(self._canale_nuovo(num, par))
            fatto = "Canale salvato sulla radio."
        elif azione == "posizione_fissa":
            msg.set_fixed_position.CopyFrom(mesh_pb2.Position(
                latitude_i=int(round(par["lat"] * 1e7)), longitude_i=int(round(par["lon"] * 1e7)),
                altitude=par["quota"], time=int(time.time())))
            fatto = "Posizione fissa impostata."
        elif azione == "togli_posizione_fissa":
            msg.remove_fixed_position = True
            fatto = "Posizione fissa tolta: la radio torna a usare il suo GPS."
        elif azione == "riavvia":
            msg.reboot_seconds = 5
            fatto = "La radio si riavvia tra 5 secondi."
        self._sessione(op, num)
        self._passo(op, "mando il comando alla radio e aspetto la conferma: puo' volerci un minuto")
        try:
            self._scambio(num, msg, False)
        except ErroreRadio as e:
            if "sessione" not in str(e):
                raise
            self._sessioni.pop(num, None)          # sessione scaduta nel frattempo: una nuova, una volta sola
            self._sessione(op, num)
            self._scambio(num, msg, False)
        if azione in ("scrivi", "riavvia"):
            self._sessioni.pop(num, None)          # la radio si riavvia: la sessione non vale piu'
        if azione == "scrivi":
            self._lette[(num, par["sezione"])] = (time.time(), nuova)
        elif azione == "canale":
            self._lette[(num, f"canale{par['indice']}")] = (time.time(), msg.set_channel)
        with self._lock:
            op["risultato"]["messaggio"] = fatto

    def _letta(self, num: int, cosa: str):
        voce = self._lette.get((num, cosa))
        return voce[1] if voce and time.time() - voce[0] < self.LETTURA_VALE_S else None

    def _leggi(self, op: dict, num: int, sez: str):
        from meshtastic.protobuf import admin_pb2, channel_pb2, config_pb2, mesh_pb2
        msg = admin_pb2.AdminMessage()
        if sez == "proprietario":
            msg.get_owner_request = True
            u = self._scambio(num, msg, True).get_owner_response
            self._lette[(num, sez)] = (time.time(), u)
            return {"nome": u.long_name, "nome_breve": u.short_name}
        if sez == "info":
            msg.get_device_metadata_request = True
            d = self._scambio(num, msg, True).get_device_metadata_response

            def nome_di(enum, valore):
                try:
                    return enum.Name(valore)
                except ValueError:
                    return str(valore)
            return {"firmware": d.firmware_version, "modello": nome_di(mesh_pb2.HardwareModel, d.hw_model),
                    "ruolo": nome_di(config_pb2.Config.DeviceConfig.Role, d.role), "bluetooth": d.hasBluetooth,
                    "wifi": d.hasWifi, "chiavi_pubbliche": d.hasPKC}
        if sez == "canali":
            canali = []
            for i in range(8):
                self._passo(op, f"chiedo il canale {i} alla radio: puo' volerci un minuto")
                msg = admin_pb2.AdminMessage()
                msg.get_channel_request = i + 1
                ch = self._scambio(num, msg, True).get_channel_response
                self._lette[(num, f"canale{i}")] = (time.time(), ch)
                if i and ch.role == channel_pb2.Channel.Role.DISABLED:
                    break                          # i canali sono in fila: dopo il primo spento non ce ne sono altri
                canali.append(self._vista_canale(ch))
            return canali
        tipo, indice, _d = self.sezioni()[sez]
        if tipo == "config":
            msg.get_config_request = indice
            r = getattr(self._scambio(num, msg, True).get_config_response, sez)
        else:
            msg.get_module_config_request = indice
            r = getattr(self._scambio(num, msg, True).get_module_config_response, sez)
        self._lette[(num, sez)] = (time.time(), r)
        valori = self._in_dizionario(r)
        if sez == "security":
            valori.pop("private_key", None)        # non deve arrivare a nessuna pagina
        return valori

    def _canali_ponte(self) -> list:
        iface = self.ponte.iface
        return list(getattr(getattr(iface, "localNode", None), "channels", None) or []) if iface else []

    def _vista_canale(self, ch) -> dict:
        from meshtastic.protobuf import channel_pb2
        psk = bytes(ch.settings.psk)
        if not psk:
            chiave: dict = {"tipo": "nessuna"}
        elif len(psk) == 1:
            chiave = {"tipo": "predefinita"}       # la chiave nota a tutti dei canali pubblici
        else:
            uguale = next((c for c in self._canali_ponte() if c.role and bytes(c.settings.psk) == psk), None)
            chiave = {"tipo": "privata", "bit": len(psk) * 8,
                      "come_ponte": (uguale.settings.name or f"canale {uguale.index}") if uguale is not None else None}
        return {"indice": ch.index, "ruolo": channel_pb2.Channel.Role.Name(ch.role), "nome": ch.settings.name,
                "chiave": chiave, "uplink": ch.settings.uplink_enabled, "downlink": ch.settings.downlink_enabled,
                "precisione": ch.settings.module_settings.position_precision}

    def _canale_nuovo(self, num: int, par: dict):
        from meshtastic.protobuf import channel_pb2
        ch = channel_pb2.Channel()
        vecchio = self._letta(num, f"canale{par['indice']}")
        if vecchio is not None:
            ch.CopyFrom(vecchio)
        elif par["modo"] == "tieni" and par["ruolo"] != "DISABLED":
            raise ErroreRadio("prima rileggi i canali della radio: per tenere la chiave com'e' devo conoscerla")
        ch.index = par["indice"]
        ch.role = channel_pb2.Channel.Role.Value(par["ruolo"])
        if par["ruolo"] == "DISABLED":
            ch.settings.Clear()
            return ch
        ch.settings.name = par["nome"]
        ch.settings.uplink_enabled, ch.settings.downlink_enabled = par["uplink"], par["downlink"]
        ch.settings.module_settings.position_precision = par["precisione"]
        if par["modo"] == "nuova":
            ch.settings.psk = par["psk"]
        elif par["modo"] == "predefinita":
            ch.settings.psk = b"\x01"
        elif par["modo"] == "nessuna":
            ch.settings.psk = b""
        elif par["modo"] == "ponte":
            mio = next((c for c in self._canali_ponte() if c.index == par["canale_ponte"] and c.role), None)
            if mio is None:
                raise ErroreRadio("il gateway non ha quel canale")
            ch.settings.psk = bytes(mio.settings.psk)
            ch.settings.name = mio.settings.name
        return ch

    def _modifica(self, num: int, sez: str, valori: dict):
        """La sezione letta dalla radio con dentro i valori cambiati sulla pagina (il resto resta com'era)."""
        from google.protobuf import json_format
        letta = self._letta(num, sez)
        if letta is None:
            raise ErroreRadio(f"prima rileggi '{sez}' dalla radio: la cambio partendo da quello che ha adesso")
        nuova = type(letta)()
        nuova.CopyFrom(letta)
        for nome, valore in valori.items():
            if nome in nuova.DESCRIPTOR.fields_by_name and isinstance(valore, list):
                nuova.ClearField(nome)             # un elenco si sostituisce, non si allunga
        try:
            json_format.ParseDict(valori, nuova)
        except (json_format.ParseError, ValueError, TypeError) as e:
            raise ErroreRadio(f"valori non validi per '{sez}': {pulisci(str(e), 160)}")
        return nuova

    def _sessione(self, op: dict, num: int) -> None:
        """I comandi che cambiano qualcosa vogliono la chiave di sessione che la radio da' con ogni risposta."""
        from meshtastic.protobuf import admin_pb2
        if time.time() - self._sessioni.get(num, (0.0, b""))[0] < self.SESSIONE_S:
            return
        self._passo(op, "chiedo alla radio il permesso (chiave di sessione): puo' volerci un minuto")
        msg = admin_pb2.AdminMessage()
        msg.get_config_request = admin_pb2.AdminMessage.ConfigType.SESSIONKEY_CONFIG
        self._scambio(num, msg, True)
        if num not in self._sessioni:
            raise ErroreRadio("la radio non ha dato la chiave di sessione (firmware piu' vecchio della 2.5?)")

    def _scambio(self, num: int, msg, vuole_risposta: bool):
        """Manda un messaggio di amministrazione alla radio e aspetta: la risposta, o la conferma di ricezione."""
        iface = self.ponte.iface
        if iface is None:
            raise ErroreRadio("il gateway non e' collegato al suo nodo in questo momento (in pausa o in riavvio): "
                              "riprova tra un minuto")
        if not ((iface.nodesByNum.get(num) or {}).get("user") or {}).get("publicKey"):
            raise ErroreRadio(ERRORI_RADIO["PKI_UNKNOWN_PUBKEY"])
        quando, chiave = self._sessioni.get(num, (0.0, b""))
        if chiave and time.time() - quando < 300:
            msg.session_passkey = chiave
        arrivato = threading.Event()
        esito: dict = {}
        ident = [0]

        def risposta(p):
            d = p.get("decoded") or {}
            instradamento = d.get("routing")
            if instradamento is not None:
                motivo = instradamento.get("errorReason", "NONE")
                if motivo != "NONE":
                    esito["errore"] = motivo
                elif p.get("from") == num:
                    esito["ok"] = True
                else:
                    # conferma "implicita": una radio vicina l'ha ripetuto. Aspetto quella della radio vera
                    esito["implicita"] = True
                    try:
                        iface._addResponseHandler(ident[0], risposta, ackPermitted=True)
                    except Exception:  # noqa
                        pass
                    return
            else:
                grezzo = (d.get("admin") or {}).get("raw")
                if grezzo is None:
                    return
                esito["admin"] = grezzo
                if grezzo.session_passkey:
                    self._sessioni[num] = (time.time(), bytes(grezzo.session_passkey))
            arrivato.set()

        try:
            with getattr(self.ponte, "_lock_invio", threading.Lock()):
                inviato = iface.sendData(msg, num, portNum=PORTA_ADMIN, wantAck=True, wantResponse=vuole_risposta,
                                         onResponse=risposta, onResponseAckPermitted=not vuole_risposta,
                                         pkiEncrypted=True)
        except Exception as e:  # noqa
            raise ErroreRadio(f"il nodo del gateway non ha preso il comando ({pulisci(str(e), 120)})")
        ident[0] = getattr(inviato, "id", 0)
        if not arrivato.wait(self.ATTESA_RISPOSTA_S):
            try:
                iface.responseHandlers.pop(ident[0], None)
            except Exception:  # noqa
                pass
            if esito.get("implicita"):
                raise ErroreRadio("il comando e' partito e un'altra radio l'ha ripetuto, ma la radio non ha "
                                  "confermato: rileggi per vedere se e' arrivato")
            raise ErroreRadio(f"la radio non ha risposto in {self.ATTESA_RISPOSTA_S:.0f} secondi: spenta, fuori "
                              "portata o in risparmio di energia")
        if "errore" in esito:
            raise ErroreRadio(ERRORI_RADIO.get(esito["errore"], f"la radio ha risposto con un errore ({esito['errore']})"))
        return esito.get("admin")


