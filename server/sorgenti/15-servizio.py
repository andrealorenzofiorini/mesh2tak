class Servizio:
    """Richieste dei Raspberry (pubbliche, da approvare) e azioni degli amministratori (dietro Authentik):
    scrive solo file nella sua cartella; le modifiche al server le fa il passo dei lavori."""

    RICHIESTE_MAX = 20                  # in attesa tutte insieme
    PER_INDIRIZZO_ORA = 6               # richieste nuove per indirizzo (IPv6: per rete /64)
    SCADENZA_S = 48 * 3600              # in attesa senza notizie dal Raspberry: si toglie
    RIFIUTO_S = 30 * 24 * 3600          # rifiutate e ponti tolti: il Raspberry non rientra da solo
    LAVORO_FERMO_S = 120                # in coda da tanto: il passo dei lavori non parte
    LAVORO_PERSO_S = 180                # approvata ma il lavoro non c'e' piu'
    LAVORO_INTERROTTO_S = 50 * 60       # "in corso" da troppo (piu' del timeout dei lavori): server riavviato a meta'
    INVITI_MAX = 10                     # inviti aperti tutti insieme
    INVITO_S = 14 * 24 * 3600           # un invito non usato vale due settimane
    INVITO_USATO_S = 7 * 24 * 3600      # poi resta in vista ancora una settimana

    def __init__(self, sorveglianza: Optional[Sorveglianza] = None, rilasci: Optional[Rilasci] = None):
        self.lock = threading.Lock()
        self.per_ip: Dict[str, List[float]] = {}
        self.sorveglianza = sorveglianza
        self.rilasci = rilasci
        self.icone_ponti: Dict[str, str] = {}    # versione delle icone che ogni ponte usa ("vecchio": prima della 1.11.0)
        self.manda_icone = Sorveglianza.manda_icone
        self.terreno = Terreno()                 # dalla 1.17.0: il rilievo per la copertura prevista
        self.luoghi = Luoghi()                   # dalla 1.18.0: cime e sostegni per i siti dei ripetitori

    # ---- file ----

    @staticmethod
    def _leggi_json(percorso: str) -> Optional[dict]:
        try:
            with open(percorso, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _scrivi_json(percorso: str, dati: dict) -> None:
        nuovo = percorso + ".nuovo"
        os.makedirs(os.path.dirname(percorso), mode=0o700, exist_ok=True)
        with open(os.open(nuovo, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
            json.dump(dati, f, ensure_ascii=False)
        os.replace(nuovo, percorso)

    @staticmethod
    def _eta(percorso: str) -> Optional[float]:
        try:
            return time.time() - os.path.getmtime(percorso)
        except OSError:
            return None

    def ponti(self) -> Dict[str, dict]:
        d = self._leggi_json(P(SITO_DIR + "/ponti.json")) or {}
        return {p.get("nome"): p for p in d.get("ponti", []) if isinstance(p, dict)}

    @staticmethod
    def registrato(impronta: str) -> bool:
        return bool(RE_IMPRONTA.match(impronta or "")) and os.path.exists(P(f"{ACCESSO_DIR}/{impronta}.json"))

    def registrazione_di(self, nome: str) -> Tuple[Optional[str], str]:
        """(impronta, chiave) del ponte registrato con quel nome."""
        for f in glob.glob(P(ACCESSO_DIR) + "/*.json"):
            d = self._leggi_json(f) or {}
            impronta = os.path.basename(f)[:-5]
            if d.get("nome") == nome and RE_IMPRONTA.match(impronta):
                return impronta, str(d.get("chiave") or "")
        return None, ""

    def stato_lavoro(self, lavoro: str) -> dict:
        """esito: in_coda, in_corso, ok, errore oppure None (sparito); fermo = in coda da troppo."""
        if not RE_ID.match(lavoro or ""):
            return {"esito": None}
        e = self._leggi_json(P(f"{ESITI_DIR}/{lavoro}.json"))
        if e:
            if e.get("esito") == "in_corso" and time.time() - (e.get("quando") or 0) > self.LAVORO_INTERROTTO_S:
                e = dict(e, esito="errore", messaggio="lavoro interrotto a meta' (il server si e' riavviato?): riprova")
            return e
        eta = self._eta(P(f"{LAVORI_DIR}/{lavoro}.json"))
        if eta is not None:
            return {"esito": "in_coda", "fermo": eta > self.LAVORO_FERMO_S}
        return {"esito": None}

    def richieste(self) -> List[dict]:
        """Tutte le richieste (in attesa, in corso e rifiutate), aggiornate con gli esiti dei lavori; toglie
        quelle scadute e quelle dei Raspberry ormai registrati. Da chiamare con self.lock."""
        ora = time.time()
        elenco = []
        for f in glob.glob(P(RICHIESTE_DIR) + "/*.json"):
            r = self._leggi_json(f)
            if not r or not RE_IMPRONTA.match(str(r.get("id", ""))) or os.path.basename(f) != r["id"] + ".json":
                togli(f)
                continue
            stato = r.get("stato")
            if stato == "in_corso":
                lv = self.stato_lavoro(r.get("lavoro", ""))
                if lv.get("esito") == "ok":
                    togli(f)
                    continue
                if lv.get("esito") == "errore" or (lv.get("esito") is None and
                                                   ora - (r.get("approvata") or 0) > self.LAVORO_PERSO_S):
                    r.update(stato="attesa", errore=lv.get("messaggio") or "il lavoro non e' partito sul server",
                             dettagli=str(lv.get("testo") or "")[-1500:], ultima=int(ora))
                    self._scrivi_json(f, r)
                else:
                    r["fermo"] = bool(lv.get("fermo"))
            elif stato == "rifiutata":
                vecchia = ora - (r.get("ultima") or 0)
                # un ponte appena tolto e' ancora registrato finche' il lavoro non l'ha tolto davvero
                if vecchia > self.RIFIUTO_S or (self.registrato(r["id"]) and
                                                (r.get("motivo") != "tolto" or vecchia > 3600)):
                    togli(f)
                    continue
            elif self.registrato(r["id"]):
                togli(f)        # registrato da un'altra strada (a mano, o da riga di comando)
                continue
            if r.get("stato") == "attesa" and ora - (r.get("ultima") or 0) > self.SCADENZA_S:
                togli(f)
                continue
            elenco.append(r)
        return sorted(elenco, key=lambda r: r.get("arrivata") or 0)

    def accoda(self, azione: str, nome: str, da: str, chiave: str = "", richiesta: Optional[str] = None,
               sostituisci: bool = False, versione: Optional[str] = None, installa: Optional[dict] = None,
               impostazioni: Optional[dict] = None, cosa: Optional[str] = None, valori: Optional[dict] = None,
               reimposta: bool = False) -> str:
        """Scrive il lavoro in tmp e lo sposta in coda gia' completo: il passo dei lavori parte appena la
        cartella non e' vuota e non deve mai trovare un file a meta'."""
        lavoro = secrets.token_hex(8)
        dati = {"id": lavoro, "azione": azione, "nome": nome, "da": da, "chiave": chiave, "richiesta": richiesta,
                "sostituisci": sostituisci, "quando": int(time.time())}
        if versione:
            dati["versione"] = versione
        if installa is not None:
            dati["installa"] = installa
        if impostazioni is not None:
            dati["impostazioni"] = impostazioni      # certificato e canale: solo per root, che lo toglie subito
        if cosa:
            dati["cosa"] = cosa                      # dalla 1.13.0: comando per un ponte
        if valori:
            dati["valori"] = valori
        if reimposta:
            dati["reimposta"] = True                 # aggiornamento con la radio come all'installazione
        provvisorio = P(f"{TMP_DIR}/{lavoro}.json")
        with open(os.open(provvisorio, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
            json.dump(dati, f, ensure_ascii=False)
        os.replace(provvisorio, P(f"{LAVORI_DIR}/{lavoro}.json"))
        print(f"lavoro {lavoro}: {azione} {nome} chiesto da {da}" + (" (sostituisce)" if sostituisci else ""),
              flush=True)
        return lavoro

    def lavori(self) -> List[dict]:
        """In coda, in corso e ultimi esiti (senza le chiavi)."""
        risultato = []
        for f in glob.glob(P(LAVORI_DIR) + "/*.json"):
            d = self._leggi_json(f) or {}
            eta = self._eta(f)
            if eta is None:
                continue
            risultato.append({"id": d.get("id"), "azione": d.get("azione"), "nome": d.get("nome"), "da": d.get("da"),
                              "richiesta": d.get("richiesta"), "versione": d.get("versione"), "esito": "in_coda",
                              "quando": d.get("quando"), "fermo": eta > self.LAVORO_FERMO_S, "cosa": d.get("cosa"),
                              "reimposta": d.get("reimposta") is True})
        esiti = [(self._eta(f), f) for f in glob.glob(P(ESITI_DIR) + "/*.json")]
        for _, f in sorted((x for x in esiti if x[0] is not None), key=lambda x: x[0])[:8]:
            d = self._leggi_json(f) or {}
            v = {k: d.get(k) for k in ("id", "azione", "nome", "da", "richiesta", "versione", "esito", "messaggio",
                                       "testo", "quando", "cosa", "reimposta")}
            if v["esito"] == "in_corso" and time.time() - (v["quando"] or 0) > self.LAVORO_INTERROTTO_S:
                v.update(esito="errore", messaggio="lavoro interrotto a meta' (il server si e' riavviato?)")
            risultato.append(v)
        return risultato

    # ---- ponti nuovi (dalla 1.10.0): inviti, impostazioni e avanzamento delle installazioni ----

    def pagina_rete(self) -> str:
        """L'indirizzo della pagina della rete (lo scrive root): va nella chiave d'invito."""
        url = str((self._leggi_json(P(SORVEGLIANZA)) or {}).get("pagina") or "").rstrip("/")
        return url if re.match(r"^https://[A-Za-z0-9.-]+(:\d+)?$", url) else ""

    def riassunto_nuovi(self) -> dict:
        d = self._leggi_json(P(RIASSUNTO_NUOVI))
        return d if d and d.get("pronto") is True else {"pronto": False}

    def inviti(self) -> List[dict]:
        """Gli inviti, piu' vecchi prima; toglie quelli scaduti. Da chiamare con self.lock."""
        ora = time.time()
        elenco = []
        for f in glob.glob(P(INVITI_DIR) + "/*.json"):
            d = self._leggi_json(f)
            if not d or not RE_IMPRONTA.match(str(d.get("id", ""))) or os.path.basename(f) != d["id"] + ".json":
                togli(f)
                continue
            if ((d.get("stato") == "attesa" and ora - (d.get("creato") or 0) > self.INVITO_S) or
                    (d.get("stato") != "attesa" and ora - (d.get("usato") or d.get("creato") or 0) > self.INVITO_USATO_S)):
                togli(f)
                continue
            elenco.append(d)
        return sorted(elenco, key=lambda d: d.get("creato") or 0)

    def vista_invito(self, i: dict) -> dict:
        v = {k: i.get(k) for k in ("id", "nome", "scheda", "posizione", "creato", "da", "stato", "usato")}
        v["scade"] = int(i.get("creato") or 0) + self.INVITO_S
        if i.get("stato") == "attesa":
            v["riga"] = i.get("riga")          # la chiave d'invito: solo finche' non e' usata
        return v

    def avanzamento(self, nome: str) -> Optional[dict]:
        """A che punto e' l'installazione del ponte nuovo (lo scrive il Raspberry, via SSH)."""
        d = self._leggi_json(P(f"{AVANZAMENTO_DIR}/{nome}.json")) if RE_NOME.match(nome or "") else None
        if not d or not RE_FASE.match(str(d.get("fase", ""))) or not isinstance(d.get("quando"), int):
            return None
        v = {"fase": d["fase"], "testo": str(d.get("testo") or "")[:300], "quando": d["quando"]}
        scelte = self._leggi_json(P(f"{INSTALLAZIONI_DIR}/{nome}.json")) or {}
        v.update({k: scelte.get(k) for k in ("scheda", "posizione") if isinstance(scelte.get(k), str)})
        return v

    # ---- icone sulla mappa (dalla 1.11.0) ----

    def scelte_icone(self) -> Dict[str, dict]:
        d = self._leggi_json(P(ICONE_SCELTE)) or {}
        scelte = d.get("scelte") if isinstance(d.get("scelte"), dict) else {}
        return {k: v for k, v in scelte.items()
                if RE_ID_RADIO.match(str(k)) and isinstance(v, dict) and v.get("icona") in ICONE_PER_CHIAVE}

    @staticmethod
    def icone_per_i_ponti(scelte: Dict[str, dict]) -> Tuple[str, Dict[str, dict]]:
        """(versione, quello che riceve ogni ponte): per ogni radio la figura e il tipo, nient'altro."""
        mappa = {k: {"percorso": ICONE_PER_CHIAVE[v["icona"]]["percorso"], "tipo": ICONE_PER_CHIAVE[v["icona"]]["tipo"]}
                 for k, v in sorted(scelte.items())}
        impronta = hashlib.sha256(json.dumps(mappa, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        return impronta.hexdigest()[:16], mappa

    def imposta_icona(self, dati: dict, utente: str) -> Tuple[int, dict]:
        radio, icona = str(dati.get("radio") or ""), str(dati.get("icona") or "")
        if not RE_ID_RADIO.match(radio):
            return 400, {"errore": "radio non valida"}
        if icona and icona not in ICONE_PER_CHIAVE:
            return 400, {"errore": "icona sconosciuta: ricarica la pagina"}
        with self.lock:
            scelte = self.scelte_icone()
            if icona:
                if radio not in scelte and len(scelte) >= ICONE_MAX:
                    return 409, {"errore": f"al massimo {ICONE_MAX} radio con un'icona scelta"}
                scelte[radio] = {"icona": icona, "da": utente, "quando": int(time.time())}
            else:
                scelte.pop(radio, None)
            self._scrivi_json(P(ICONE_SCELTE), {"formato": 1, "scelte": scelte})
        print(f"icona di {radio}: {icona or 'predefinita'} (da {utente})", flush=True)
        return 200, {"esito": "fatto", "versione": self.icone_per_i_ponti(scelte)[0],
                     "messaggio": "Icona salvata." if icona else "Tornata l'icona predefinita."}

    def imposta_impianto(self, dati: dict, utente: str) -> Tuple[int, dict]:
        """Dalla 1.17.0: altezza e guadagno dell'antenna, perdite del cavo e potenza di un gateway ("g:nome"), di un
        ripetitore ("r:!id") o delle radio portatili ("portatile"), per calcolare la copertura prevista."""
        chi = str(dati.get("chi") or "")
        if not RE_IMPIANTO.match(chi):
            return 400, {"errore": "impianto non valido"}
        valori, errore = valida_impianto(dati)
        if valori is None:
            return 400, {"errore": errore}
        with self.lock:
            impianti = leggi_impianti()
            if valori:
                if chi not in impianti and len(impianti) >= IMPIANTI_MAX:
                    return 409, {"errore": f"al massimo {IMPIANTI_MAX} impianti"}
                impianti[chi] = dict(valori, da=utente, quando=int(time.time()))
            else:
                impianti.pop(chi, None)
            self._scrivi_json(P(IMPIANTI), {"formato": 1, "impianti": impianti})
        print(f"impianto {chi}: {valori or 'valori di fabbrica'} (da {utente})", flush=True)
        return 200, {"esito": "fatto", "impianti": impianti,
                     "messaggio": "Dati dell'impianto salvati." if valori else "Tornati i valori di fabbrica."}

    def allinea_icone(self, nome: str, porta: int, st: Optional[dict]) -> None:
        """Dopo ogni lettura dello stato di un ponte (una al minuto): se usa icone diverse da quelle scelte,
        gliele manda dal suo tunnel. Un ponte spento le riceve quando torna."""
        if st is None:
            return
        ic = st.get("icone")
        if not isinstance(ic, dict):
            self.icone_ponti[nome] = "vecchio"
            return
        with self.lock:
            versione, mappa = self.icone_per_i_ponti(self.scelte_icone())
        if ic.get("versione") == versione:
            self.icone_ponti[nome] = versione
            return
        corpo = json.dumps({"formato": 1, "versione": versione, "icone": mappa}, separators=(",", ":"))
        risposta = self.manda_icone(porta, corpo.encode("utf-8"))
        if isinstance(risposta, dict) and risposta.get("versione") == versione:
            self.icone_ponti[nome] = versione
            print(f"icone: il gateway {nome} usa quelle scelte ({len(mappa)} radio)", flush=True)
        else:
            self.icone_ponti[nome] = str(ic.get("versione") or "")
            errore = risposta.get("errore") if isinstance(risposta, dict) else "nessuna risposta"
            print(f"icone: il gateway {nome} non le ha prese ({errore})", flush=True)

    def vista_icone(self, ponti) -> dict:
        scelte = self.scelte_icone()
        versione, _ = self.icone_per_i_ponti(scelte)
        stato_ponti = {}
        for n in ponti:
            v = self.icone_ponti.get(n)
            stato_ponti[n] = "aggiornato" if v == versione else ("vecchio" if v == "vecchio" else "attesa")
        return {"versione": versione, "scelte": scelte, "ponti": stato_ponti}

    # ---- Gestisci il Raspberry (dalla 1.13.0): il comando va nella registrazione del ponte, lo fa il Raspberry ----

    MESSAGGI_PONTE = {
        "riavvia-programma": "Chiedo al gateway di riavviare il programma.",
        "riavvia-radio": "Chiedo al gateway di riavviare la radio.",
        "riavvia-raspberry": "Chiedo al gateway di riavviare il Raspberry: torna in linea in 2-3 minuti.",
        "registro": "Chiedo al gateway il registro.",
        "impostazioni": "Mando al gateway le impostazioni nuove.",
        "reimposta-radio": "Chiedo al gateway di rimettere la radio come all'installazione.",
    }

    def _ponte_occupato(self, nome: str) -> bool:
        return any(lv.get("nome") == nome and lv.get("azione") in ("azione-ponte", "certificato-ponte")
                   and lv.get("esito") in ("in_coda", "in_corso") for lv in self.lavori())

    def gestisci_ponte(self, dati: dict, utente: str) -> Tuple[int, dict]:
        nome, cosa = str(dati.get("nome") or ""), str(dati.get("azione") or "")
        if not RE_NOME.match(nome) or nome not in self.ponti():
            return 404, {"errore": "gateway non trovato: ricarica la pagina"}
        if cosa not in AZIONI_PONTE:
            return 400, {"errore": "comando sconosciuto: ricarica la pagina"}
        valori = None
        if cosa == "impostazioni":
            try:
                valori = valida_impostazioni_ponte(dati.get("valori"))
            except Errore as e:
                return 400, {"errore": str(e)}
        with self.lock:
            if self._ponte_occupato(nome):
                return 409, {"errore": "sto gia' passando un comando a questo gateway: aspetta qualche secondo"}
            lavoro = self.accoda("azione-ponte", nome, utente, cosa=cosa, valori=valori)
        return 202, {"esito": "in_corso", "lavoro": lavoro, "messaggio": self.MESSAGGI_PONTE[cosa]}

    def certificato_ponte(self, dati: dict, utente: str) -> Tuple[int, dict]:
        nome = str(dati.get("nome") or "")
        if not RE_NOME.match(nome) or nome not in self.ponti():
            return 404, {"errore": "gateway non trovato: ricarica la pagina"}
        try:
            impostazioni = controlla_certificato_ponte(dati)
        except Errore as e:
            return 400, {"errore": str(e)}
        with self.lock:
            if self._ponte_occupato(nome):
                return 409, {"errore": "sto gia' passando un comando a questo gateway: aspetta qualche secondo"}
            lavoro = self.accoda("certificato-ponte", nome, utente, impostazioni=impostazioni)
        return 202, {"esito": "in_corso", "lavoro": lavoro,
                     "messaggio": "Controllo il certificato e lo passo al gateway, che lo installa da solo."}

    def _salva_installazione(self, nome: str, installa: dict, utente: str) -> None:
        self._scrivi_json(P(f"{INSTALLAZIONI_DIR}/{nome}.json"), dict(installa, da=utente, quando=int(time.time())))

    def nuovo_invito(self, dati: dict, utente: str) -> Tuple[int, dict]:
        try:
            nome = valida_nome(str(dati.get("nome", "")))
            posizione = valida_posizione(str(dati.get("posizione") or ""))
        except Errore as e:
            return 400, {"errore": str(e)}
        scheda = str(dati.get("scheda") or "")
        if scheda not in SCHEDE:
            return 400, {"errore": "scegli la radio del gateway"}
        if not self.riassunto_nuovi().get("pronto"):
            return 409, {"errore": "prima carica certificato e canale per i gateway nuovi"}
        rete = self.pagina_rete()
        if not rete:
            return 503, {"errore": "manca l'indirizzo della pagina: sul server  sudo bash rete-meshtastic.sh installa"}
        if nome in self.ponti():
            return 409, {"errore": f"il nome '{nome}' e' gia' di un gateway della rete: scegline un altro"}
        with self.lock:
            aperti = [i for i in self.inviti() if i.get("stato") == "attesa"]
            if any(i.get("nome") == nome for i in aperti):
                return 409, {"errore": f"c'e' gia' un invito per il gateway '{nome}': usa quello, o annullalo"}
            if len(aperti) >= self.INVITI_MAX:
                return 429, {"errore": "troppi inviti aperti: annulla quelli che non servono"}
            # una chiave pubblica SSH a caso, di cui nessuno ha la chiave privata: Raspberry Pi Imager la scrive sul
            # Raspberry come una delle sue chiavi autorizzate, e il Raspberry la manda indietro. E' il segreto
            # dell'invito; dopo il nome c'e' l'indirizzo della pagina della rete, che cosi' non sta nell'immagine
            blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + secrets.token_bytes(32)
            inv = {"id": hashlib.sha256(blob).hexdigest(), "nome": nome, "scheda": scheda, "posizione": posizione,
                   "riga": f"ssh-ed25519 {base64.b64encode(blob).decode()} mesh2tak-invito {rete}",
                   "creato": int(time.time()), "da": utente, "stato": "attesa"}
            self._scrivi_json(P(f"{INVITI_DIR}/{inv['id']}.json"), inv)
        print(f"invito per il gateway nuovo '{nome}' preparato da {utente}", flush=True)
        return 200, {"esito": "ok", "invito": self.vista_invito(inv)}

    @staticmethod
    def _leggi_nuovo(d) -> Optional[dict]:
        """Le notizie di un Raspberry appena acceso con l'immagine dei ponti nuovi (dalla 1.10.0)."""
        if not isinstance(d, dict):
            return None
        modello = re.sub(r"[^\w .,()+/-]", "", str(d.get("modello") or ""))[:80].strip()
        inviti = []
        for b in (d.get("inviti") if isinstance(d.get("inviti"), list) else [])[:3]:
            try:
                blob = base64.b64decode(str(b), validate=True)
            except (binascii.Error, ValueError):
                continue
            if len(blob) == 51 and blob.startswith(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"):
                inviti.append(hashlib.sha256(blob).hexdigest())
        return {"modello": modello, "radio_usb": d.get("radio_usb") is True, "inviti": inviti}

    def _invito_valido(self, ids: List[str]) -> Optional[dict]:
        ora = time.time()
        for i in ids:
            d = self._leggi_json(P(f"{INVITI_DIR}/{i}.json"))
            if d and d.get("id") == i and d.get("stato") == "attesa" and ora - (d.get("creato") or 0) <= self.INVITO_S:
                return d
        return None

    # ---- richieste dei Raspberry (pubbliche) ----

    @staticmethod
    def _rete_di(ip: str) -> str:
        """Il limite per indirizzo vale per un IPv4 o per una rete IPv6 /64 (una casa ne ha una intera)."""
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return ip
        if a.version == 6:
            if a.ipv4_mapped:
                return str(a.ipv4_mapped)
            return str(ipaddress.ip_network(f"{a}/64", strict=False))
        return str(a)

    def nuova_richiesta(self, dati, ip: str) -> Tuple[int, dict]:
        if not isinstance(dati, dict) or dati.get("formato") != 1:
            return 400, {"errore": "richiesta non valida"}
        try:
            nome = valida_nome(str(dati.get("nome", "")))
            chiave, impronta = leggi_chiave(str(dati.get("chiave", "")))
        except Errore as e:
            return 400, {"errore": str(e)}
        macchina = str(dati.get("macchina") or "")
        versione = str(dati.get("versione") or "")
        if not RE_MACCHINA.match(macchina) or not RE_VERSIONE.match(versione):
            return 400, {"errore": "richiesta non valida"}
        controllo = codice_controllo(impronta)
        if self.registrato(impronta):
            return 200, {"esito": "registrato", "controllo": controllo}
        nuovo = self._leggi_nuovo(dati.get("nuovo"))
        ora = time.time()
        with self.lock:
            percorso = P(f"{RICHIESTE_DIR}/{impronta}.json")
            vecchia = self._leggi_json(percorso)
            if vecchia and vecchia.get("stato") == "rifiutata":
                if ora - (vecchia.get("ultima") or 0) < self.RIFIUTO_S:
                    perche = ("tolto dalla rete" if vecchia.get("motivo") == "tolto" else "rifiutato")
                    return 403, {"errore": f"questo Raspberry e' stato {perche} sulla pagina della rete",
                                 "controllo": controllo}
                vecchia = None
            if vecchia and vecchia.get("stato") == "in_corso":
                vecchia.update(ultima=int(ora), ip=ip)        # lo sta gia' aggiungendo: niente da cambiare
                self._scrivi_json(percorso, vecchia)
                return 202, {"esito": "in_attesa", "controllo": controllo, "in_corso": True}
            if not vecchia:
                rete = self._rete_di(ip)
                recenti = [x for x in self.per_ip.get(rete, []) if ora - x < 3600]
                if len(recenti) >= self.PER_INDIRIZZO_ORA:
                    return 429, {"errore": "troppe richieste da questo indirizzo: riprovo tra un'ora"}
                self.per_ip[rete] = recenti + [ora]
                if len(self.per_ip) > 500:
                    self.per_ip = {k: v for k, v in self.per_ip.items() if any(ora - x < 3600 for x in v)}
                if sum(1 for r in self.richieste() if r.get("stato") != "rifiutata") >= self.RICHIESTE_MAX:
                    return 429, {"errore": "troppe richieste in attesa sulla pagina della rete"}
            if vecchia and vecchia.get("invito"):
                nome = vecchia.get("nome") or nome        # il nome l'ha deciso l'invito, non il Raspberry
            r = dict(vecchia or {}, id=impronta, nome=nome, chiave=chiave, macchina=macchina, versione=versione,
                     ip=ip, controllo=controllo, ultima=int(ora))
            r.setdefault("arrivata", int(ora))
            r.setdefault("stato", "attesa")
            invito = None
            if nuovo is not None:
                r["nuovo"] = {"modello": nuovo["modello"], "radio_usb": nuovo["radio_usb"]}
                invito = self._invito_valido(nuovo["inviti"]) if r["stato"] == "attesa" else None
            if invito is not None:
                # Raspberry con la chiave d'invito: si installa da solo, con le scelte fatte nell'invito
                nome = invito["nome"]
                installa = {"scheda": invito["scheda"], "posizione": invito.get("posizione") or ""}
                r.update(nome=nome, invito=invito["id"], installa=installa)
                if nome in self.ponti():
                    r["errore"] = (f"nel frattempo c'e' un altro gateway '{nome}': toglilo dalla rete o rifiuta "
                                   "questa richiesta")
                else:
                    lavoro = self.accoda("aggiungi", nome, invito["da"], chiave, impronta, installa=installa)
                    r.update(stato="in_corso", lavoro=lavoro, approvata=int(ora), da=invito["da"], errore=None,
                             dettagli=None)
                    self._salva_installazione(nome, installa, invito["da"])
                    print(f"il Raspberry {macchina} ({ip}) ha la chiave d'invito del gateway nuovo '{nome}': lo "
                          "aggiungo", flush=True)
                invito.update(stato="usato", usato=int(ora), richiesta=impronta)
                self._scrivi_json(P(f"{INVITI_DIR}/{invito['id']}.json"), invito)
            self._scrivi_json(percorso, r)
        if r.get("stato") == "in_corso":
            return 202, {"esito": "in_attesa", "controllo": controllo, "in_corso": True}
        sostituisce = nome in self.ponti()
        if not vecchia:
            print(f"richiesta del gateway '{nome}' da {macchina} ({ip}), codice di controllo {controllo}"
                  + (" (c'e' gia' un gateway con questo nome)" if sostituisce else "")
                  + (" (Raspberry nuovo, senza una chiave d'invito valida)" if nuovo is not None else ""), flush=True)
        return 202, {"esito": "in_attesa", "controllo": controllo, "sostituisce": sostituisce}

    # ---- azioni degli amministratori ----

    def stato(self, utente: str) -> dict:
        with self.lock:
            tutte = self.richieste()
            ponti = self.ponti()
            lavori = self.lavori()
            inviti = self.inviti()
        aperte = [r for r in tutte if r.get("stato") in ("attesa", "in_corso")]
        nomi = [r.get("nome") for r in aperte]
        richieste = []
        for r in aperte:
            v = {k: r.get(k) for k in ("id", "nome", "macchina", "versione", "ip", "controllo", "arrivata", "ultima",
                                       "stato", "errore", "dettagli", "da", "nuovo", "installa")}
            v.update(stesso_nome=nomi.count(r.get("nome")) > 1, sostituisce=r.get("nome") in ponti,
                     fermo=bool(r.get("fermo")), invito=bool(r.get("invito")))
            richieste.append(v)
        installazioni = {}
        for n in ponti:
            av = self.avanzamento(n)
            if av:
                installazioni[n] = av
        rifiutate = [dict({k: r.get(k) for k in ("id", "nome", "macchina", "ip", "controllo", "ultima", "da", "motivo")},
                          presente=r.get("nome") in ponti, chiave=bool(r.get("chiave")))
                     for r in tutte if r.get("stato") == "rifiutata" and not self.registrato(r.get("id", ""))]
        rifiutate.sort(key=lambda r: -(r.get("ultima") or 0))
        return {"formato": 1, "utente": utente, "richieste": richieste, "rifiutate": rifiutate, "lavori": lavori,
                "versione": VERSIONE, "avvisi": self.sorveglianza.vista() if self.sorveglianza else None,
                "rilasci": self.rilasci.vista() if self.rilasci else None,
                "ponti_nuovi": self.riassunto_nuovi(), "inviti": [self.vista_invito(i) for i in inviti],
                "installazioni": installazioni, "imager": f"{GITHUB}/{REPO}/releases/latest/download/imager.json",
                "icone": self.vista_icone(ponti), "impianti": leggi_impianti()}

    def _approva(self, f: str, r: dict, utente: str, sostituisci_ok: bool,
                 installa: Optional[dict] = None) -> Tuple[int, dict]:
        if not r.get("chiave"):
            # (ponte tolto prima che la registrazione avesse la chiave): via il rifiuto, il Raspberry richiede
            togli(f)
            return 200, {"esito": "riammessa", "messaggio": f"Fatto: il Raspberry {r.get('nome')} ripete la "
                                                            "richiesta entro un'ora, poi la approvi qui."}
        if installa is None and isinstance(r.get("installa"), dict):
            installa = r["installa"]          # scelte dell'invito: "Riprova" dopo un errore
        if installa is not None:
            if not r.get("nuovo"):
                return 400, {"errore": "questo Raspberry e' stato installato a mano: approvalo senza installazione"}
            if not self.riassunto_nuovi().get("pronto"):
                return 409, {"errore": "prima carica certificato e canale per i gateway nuovi"}
        sostituisci = r["nome"] in self.ponti()
        if sostituisci and not sostituisci_ok:
            return 409, {"errore": f"c'e' gia' un gateway '{r['nome']}' nella rete: ricarica la pagina e scegli "
                                   "'Sostituisci' se questo Raspberry deve prenderne il posto"}
        lavoro = self.accoda("aggiungi", r["nome"], utente, r["chiave"], r["id"], sostituisci=sostituisci,
                             installa=installa)
        r.update(stato="in_corso", lavoro=lavoro, approvata=int(time.time()), da=utente, errore=None, dettagli=None)
        if installa is not None:
            r["installa"] = installa
            self._salva_installazione(r["nome"], installa, utente)
        r.pop("fermo", None)
        self._scrivi_json(f, r)
        return 202, {"esito": "in_corso", "lavoro": lavoro, "messaggio": f"Sto aggiungendo il gateway {r['nome']}." +
                     (" Poi il Raspberry si installa da solo: qui vedi a che punto e'." if installa else "")}

    def azione(self, percorso: str, dati, utente: str) -> Tuple[int, dict]:
        if not isinstance(dati, dict):
            return 400, {"errore": "richiesta non valida"}
        if percorso == "/api/prova-avvisi":     # fuori dal blocco: aspetta la console di infra-TAK
            if not self.sorveglianza:
                return 503, {"errore": "avvisi non attivi"}
            return self.sorveglianza.prova(utente)
        if percorso == "/api/controlla-aggiornamenti":
            if not self.rilasci:
                return 503, {"errore": "aggiornamenti non attivi"}
            fatto, _ = self.rilasci.controlla(forza=True)
            if not fatto:
                return 429, {"errore": "ho appena controllato: riprova tra un minuto"}
            return 200, {"rilasci": self.rilasci.vista()}
        if percorso in ("/api/aggiorna-server", "/api/aggiorna-ponti"):
            versione = str(dati.get("versione") or "")
            if not self.rilasci or not RE_VERSIONE_RILASCIO.match(versione) or not self.rilasci.pronta(versione):
                return 409, {"errore": "versione non disponibile: ricarica la pagina"}
            with self.lock:
                if percorso == "/api/aggiorna-server":
                    if numeri_versione(versione) <= numeri_versione(VERSIONE):
                        return 409, {"errore": f"il server ha gia' la {VERSIONE}"}
                    if any(lv.get("azione") == "aggiorna-server" and lv.get("esito") in ("in_coda", "in_corso")
                           for lv in self.lavori()):
                        return 409, {"errore": "l'aggiornamento del server e' gia' in corso"}
                    return 202, {"esito": "in_corso", "lavoro": self.accoda("aggiorna-server", "server", utente,
                                                                            versione=versione),
                                 "messaggio": f"Aggiorno il server alla {versione}: per un minuto la pagina non "
                                              "risponde, poi riprende da sola."}
                if numeri_versione(versione) > numeri_versione(VERSIONE):
                    return 409, {"errore": f"prima aggiorna il server alla {versione}"}
                nome = str(dati.get("nome") or "tutti")
                if nome != "tutti" and nome not in self.ponti():
                    return 404, {"errore": "gateway non trovato: ricarica la pagina"}
                reimposta = dati.get("reimposta") is True     # dalla 1.13.0: anche la radio come all'installazione
                return 202, {"esito": "in_corso", "lavoro": self.accoda("aggiorna-ponti", nome, utente,
                                                                        versione=versione, reimposta=reimposta),
                             "messaggio": f"Chiedo ai gateway di aggiornarsi alla {versione}"
                                          + (" e di rimettere la radio come all'installazione" if reimposta else "")
                                          + ": lo fanno da soli entro qualche minuto."}
        if percorso == "/api/gestisci-ponte":
            return self.gestisci_ponte(dati, utente)
        if percorso == "/api/certificato-ponte":
            return self.certificato_ponte(dati, utente)
        if percorso == "/api/invita":
            return self.nuovo_invito(dati, utente)
        if percorso == "/api/icona":
            return self.imposta_icona(dati, utente)
        if percorso == "/api/impianto":
            return self.imposta_impianto(dati, utente)
        if percorso == "/api/impostazioni-nuovi":
            try:
                impostazioni = controlla_impostazioni(dati)
            except Errore as e:
                return 400, {"errore": str(e)}
            with self.lock:
                if any(lv.get("azione") == "impostazioni-nuovi" and lv.get("esito") in ("in_coda", "in_corso")
                       for lv in self.lavori()):
                    return 409, {"errore": "sto gia' salvando le impostazioni: aspetta un momento"}
                lavoro = self.accoda("impostazioni-nuovi", "ponti-nuovi", utente, impostazioni=impostazioni)
            return 202, {"esito": "in_corso", "lavoro": lavoro,
                         "messaggio": "Controllo e salvo certificato e canale per i gateway nuovi."}
        with self.lock:
            if percorso == "/api/annulla-invito":
                iid = str(dati.get("id", ""))
                f = P(f"{INVITI_DIR}/{iid}.json") if RE_IMPRONTA.match(iid) else ""
                inv = self._leggi_json(f) if f else None
                if not inv or inv.get("stato") != "attesa":
                    return 404, {"errore": "invito non trovato (gia' usato o scaduto?): ricarica la pagina"}
                togli(f)
                print(f"invito del gateway '{inv.get('nome')}' annullato da {utente}", flush=True)
                return 200, {"esito": "annullato", "messaggio": f"Invito per {inv.get('nome')} annullato: quella "
                                                                "chiave non vale piu'."}
            if percorso == "/api/riprova-installazione":
                nome = str(dati.get("nome", ""))
                if not RE_NOME.match(nome) or nome not in self.ponti():
                    return 404, {"errore": "gateway non trovato: ricarica la pagina"}
                av = self.avanzamento(nome)
                if not av or av["fase"] == "fatto":
                    return 409, {"errore": f"il gateway {nome} non sta installando: niente da riprovare"}
                installa = {}
                if dati.get("scheda"):
                    if dati["scheda"] not in SCHEDE:
                        return 400, {"errore": "radio del gateway non prevista"}
                    installa["scheda"] = dati["scheda"]
                if "posizione" in dati:
                    try:
                        installa["posizione"] = valida_posizione(str(dati.get("posizione") or ""))
                    except Errore as e:
                        return 400, {"errore": str(e)}
                if any(lv.get("azione") == "installa" and lv.get("nome") == nome and lv.get("esito") in
                       ("in_coda", "in_corso") for lv in self.lavori()):
                    return 409, {"errore": "l'ho gia' chiesto: il Raspberry riprova entro un paio di minuti"}
                lavoro = self.accoda("installa", nome, utente, installa=installa)
                prima = self._leggi_json(P(f"{INSTALLAZIONI_DIR}/{nome}.json")) or {}
                self._salva_installazione(nome, {"scheda": installa.get("scheda", prima.get("scheda") or ""),
                                                 "posizione": installa.get("posizione", prima.get("posizione") or "")},
                                          utente)
                return 202, {"esito": "in_corso", "lavoro": lavoro,
                             "messaggio": f"Chiesto: il Raspberry di {nome} riprova l'installazione entro un paio di "
                                          "minuti."}
            if percorso in ("/api/approva", "/api/rifiuta", "/api/riammetti"):
                rid = str(dati.get("id", ""))
                f = P(f"{RICHIESTE_DIR}/{rid}.json") if RE_IMPRONTA.match(rid) else ""
                r = self._leggi_json(f) if f else None
                attesa = "rifiutata" if percorso == "/api/riammetti" else "attesa"
                if r and r.get("stato") == "in_corso":
                    return 409, {"errore": "lo sto gia' aggiungendo"}
                if not r or r.get("stato") != attesa:
                    return 404, {"errore": "richiesta non trovata (gia' approvata, rifiutata o scaduta?): "
                                           "ricarica la pagina"}
                if percorso == "/api/rifiuta":
                    r.update(stato="rifiutata", motivo="rifiutata", ultima=int(time.time()), da=utente)
                    self._scrivi_json(f, r)
                    print(f"richiesta del gateway '{r.get('nome')}' rifiutata da {utente}", flush=True)
                    return 200, {"esito": "rifiutata", "messaggio": f"Rifiutata: il Raspberry {r.get('nome')} non "
                                                                    "rientra da solo."}
                installa = None
                if percorso == "/api/approva" and isinstance(dati.get("installa"), dict):
                    # Raspberry nuovo arrivato senza una chiave d'invito valida: l'amministratore sceglie lui
                    d = dati["installa"]
                    try:
                        nome = valida_nome(str(d.get("nome") or r.get("nome") or ""))
                        installa = {"scheda": str(d.get("scheda") or ""),
                                    "posizione": valida_posizione(str(d.get("posizione") or ""))}
                    except Errore as e:
                        return 400, {"errore": str(e)}
                    if installa["scheda"] not in SCHEDE:
                        return 400, {"errore": "scegli la radio del gateway"}
                    r["nome"] = nome
                return self._approva(f, r, utente, dati.get("sostituisci") is True, installa)
            if percorso == "/api/aggiungi":
                try:
                    nome = valida_nome(str(dati.get("nome", "")))
                    chiave, impronta = leggi_chiave(str(dati.get("chiave", "")))
                except Errore as e:
                    return 400, {"errore": str(e)}
                if nome in self.ponti():
                    return 409, {"errore": f"il nome '{nome}' e' gia' di un gateway della rete"}
                if self.registrato(impronta):
                    return 409, {"errore": "questa chiave e' gia' di un gateway della rete"}
                togli(P(f"{RICHIESTE_DIR}/{impronta}.json"))   # aggiunto a mano: vale piu' di un rifiuto
                return 202, {"esito": "in_corso", "lavoro": self.accoda("aggiungi", nome, utente, chiave)}
            if percorso == "/api/rimuovi":
                nome = str(dati.get("nome", ""))
                if not RE_NOME.match(nome) or nome not in self.ponti():
                    return 404, {"errore": "gateway non trovato: ricarica la pagina"}
                lavoro = self.accoda("rimuovi", nome, utente)
                togli(P(f"{INSTALLAZIONI_DIR}/{nome}.json"))
                impronta, chiave = self.registrazione_di(nome)
                if impronta:
                    # se il Raspberry e' ancora acceso non rientra da solo: per riprenderlo, "Lascia rientrare"
                    ora = int(time.time())
                    self._scrivi_json(P(f"{RICHIESTE_DIR}/{impronta}.json"), {
                        "id": impronta, "nome": nome, "chiave": chiave, "controllo": codice_controllo(impronta),
                        "stato": "rifiutata", "motivo": "tolto", "da": utente, "arrivata": ora, "ultima": ora,
                        "macchina": "", "ip": "", "versione": ""})
                return 202, {"esito": "in_corso", "lavoro": lavoro, "messaggio": f"Sto togliendo il gateway {nome}."}
        return 404, {"errore": "azione sconosciuta"}


CORPO_IMPOSTAZIONI_MAX = 400 << 10       # pacchetto del certificato (fino a 256 kB) in base64, con il resto


def gestore_per(servizio: Servizio):
    class Gestore(http.server.BaseHTTPRequestHandler):
        server_version = "mesh2tak-rete"
        sys_version = ""
        timeout = 15

        def log_message(self, *a):
            pass

        def _manda(self, codice: int, dati: dict) -> None:
            corpo = json.dumps(dati, ensure_ascii=False).encode("utf-8")
            self.send_response(codice)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

        def _corpo(self, massimo: int = 16384):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > massimo:
                return None, 413
            try:
                return json.loads(self.rfile.read(n).decode("utf-8") or "null"), 0
            except (ValueError, UnicodeDecodeError):
                return None, 400

        def _utente(self) -> str:
            """L'amministratore che Authentik ha fatto entrare (lo aggiunge Caddy, dopo aver tolto quello del
            browser). Senza, la richiesta non e' passata da Authentik."""
            u = (self.headers.get("X-Authentik-Username") or "").strip()
            return u if RE_UTENTE_AK.match(u) else ""

        def _ip(self) -> str:
            ip = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            return ip if re.match(r"^[0-9a-fA-F:.]{2,45}$", ip) else self.client_address[0]

        def do_GET(self):  # noqa: N802
            percorso = urllib.parse.urlsplit(self.path).path
            if percorso == "/api/stato":
                utente = self._utente()
                if not utente:
                    return self._manda(403, {"errore": "serve l'accesso da Authentik"})
                return self._manda(200, servizio.stato(utente))
            riquadro = RE_TERRENO.match(percorso)
            if riquadro:                      # dalla 1.17.0: il rilievo per la copertura prevista
                if not self._utente():
                    return self._manda(403, {"errore": "serve l'accesso da Authentik"})
                codice, png = servizio.terreno.riquadro(*(int(n) for n in riquadro.groups()))
                if codice != 200:
                    return self._manda(codice, {"errore": {404: "riquadro fuori dalla mappa",
                                                           429: "troppi riquadri chiesti: riprova tra qualche minuto"}
                                                .get(codice, "il rilievo non arriva: riprova")})
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "private, max-age=2592000, immutable")
                self.send_header("Content-Length", str(len(png)))
                self.end_headers()
                self.wfile.write(png)
                return None
            quadrato = RE_LUOGHI.match(percorso)
            if quadrato:                      # dalla 1.18.0: cime, tralicci e rifugi per i siti dei ripetitori
                if not self._utente():
                    return self._manda(403, {"errore": "serve l'accesso da Authentik"})
                codice, luoghi = servizio.luoghi.quadrato(*(int(n) for n in quadrato.groups()))
                if codice != 200:
                    return self._manda(codice, {"errore": {404: "quadrato fuori dalla mappa",
                                                           429: "troppe richieste a OpenStreetMap: riprova tra qualche minuto"}
                                                .get(codice, "OpenStreetMap non risponde: riprova")})
                corpo = json.dumps(luoghi, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "private, max-age=86400")
                self.send_header("Content-Length", str(len(corpo)))
                self.end_headers()
                self.wfile.write(corpo)
                return None
            self._manda(404, {"errore": "non trovato"})

        def do_POST(self):  # noqa: N802
            percorso = urllib.parse.urlsplit(self.path).path
            # il certificato dei ponti nuovi (dalla 1.10.0) e di un ponte (1.13.0) e' piu' grande delle altre richieste
            dati, errore = self._corpo(CORPO_IMPOSTAZIONI_MAX if percorso in ("/api/impostazioni-nuovi",
                                                                               "/api/certificato-ponte") else 16384)
            if errore:
                return self._manda(errore, {"errore": "richiesta non valida"})
            if percorso == "/accesso/richiesta":
                codice, risposta = servizio.nuova_richiesta(dati, self._ip())
                return self._manda(codice, risposta)
            if percorso.startswith("/api/"):
                utente = self._utente()
                if not utente:
                    return self._manda(403, {"errore": "serve l'accesso da Authentik"})
                # contro i siti che provano a far cliccare il browser dell'amministratore
                origine = urllib.parse.urlsplit(self.headers.get("Origin") or "").netloc
                if self.headers.get("X-Mesh2tak") != "1" or (origine and origine != self.headers.get("Host")):
                    return self._manda(403, {"errore": "richiesta non accettata"})
                codice, risposta = servizio.azione(percorso, dati, utente)
                return self._manda(codice, risposta)
            self._manda(404, {"errore": "non trovato"})

    return Gestore


def cmd_servi(a) -> int:
    sorveglianza = Sorveglianza()
    rilasci = Rilasci()
    servizio = Servizio(sorveglianza, rilasci)
    sorveglianza.dopo = servizio.allinea_icone      # dalla 1.11.0: icone sulla mappa ai ponti, dal tunnel
    threading.Thread(target=sorveglianza.ciclo, name="avvisi", daemon=True).start()
    threading.Thread(target=rilasci.ciclo, name="rilasci", daemon=True).start()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORTA_WEB), gestore_per(servizio))
    server.daemon_threads = True
    print(f"servizio della pagina della rete in ascolto su 127.0.0.1:{PORTA_WEB} (mesh2tak {VERSIONE})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


