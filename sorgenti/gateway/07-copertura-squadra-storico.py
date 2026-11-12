class Copertura:
    """Mappa della copertura (dalla 1.14.0): per ogni quadretto in cui si trovava una radio della squadra quando il
    ponte l'ha sentita, con che segnale l'ha sentita. Il quadretto e' il riquadro della mappa a zoom 17 (circa 210 m
    alle nostre latitudini). Si tengono solo quadretto, segnale e giorno: non quale radio ne' a che ora, cosi' la mappa
    non diventa lo storico degli spostamenti dei volontari.

    Su file ogni quadretto e' [x, y, dirette, somma dei segnali, segnale migliore, potenza migliore, tramite altre
    radio, giorno AAAAMMGG, {ripetitore: volte}]; alla pagina va la media al posto della somma. Dalla 1.15.0 i
    pacchetti arrivati con un salto da un ripetitore fisso contano per quel ripetitore (formato 2; il formato 1,
    senza i ripetitori, si legge ancora)."""

    ZOOM = 17
    MASSIMO = 20000                 # quadretti tenuti (qualche centinaio di kB): oltre, escono i piu' vecchi
    SALVA_OGNI_S = 600.0            # su file al massimo ogni 10 minuti: la microSD non va consumata
    ERRORE_MASSIMO_M = 100.0        # una posizione piu' approssimata finirebbe nel quadretto sbagliato

    def __init__(self, percorso: Optional[str] = None):
        self.percorso = percorso
        self._lock = threading.Lock()
        self.celle: Dict[Tuple[int, int], list] = {}
        self.dal: Optional[int] = None          # da quando raccoglie (dall'ultimo azzeramento)
        self._da_salvare = False
        self._salvata = time.monotonic()
        if percorso:
            try:
                with open(percorso, encoding="utf-8") as f:
                    self._carica(json.load(f))
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as e:
                log.warning("Mappa della copertura: %s non valido (%s), riparto da zero", percorso, e)

    @classmethod
    def quadretto(cls, lat: float, lon: float) -> Tuple[int, int]:
        n = 1 << cls.ZOOM
        lat = max(-85.05112878, min(85.05112878, lat))
        x = int((lon + 180.0) / 360.0 * n)
        y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
        return min(n - 1, max(0, x)), min(n - 1, max(0, y))

    def _carica(self, d) -> None:
        if not isinstance(d, dict) or d.get("formato") not in (1, 2) or d.get("zoom") != self.ZOOM:
            raise ValueError("formato sconosciuto")
        n = 1 << self.ZOOM

        def intero(v, minimo, massimo):
            return isinstance(v, int) and not isinstance(v, bool) and minimo <= v <= massimo

        def segnale(v, minimo, massimo):
            return v is None or (isinstance(v, (int, float)) and not isinstance(v, bool) and minimo <= v <= massimo)
        for c in d.get("celle") or []:
            if len(self.celle) >= self.MASSIMO:
                break
            if (not isinstance(c, list) or len(c) not in (8, 9) or not intero(c[0], 0, n - 1)
                    or not intero(c[1], 0, n - 1) or not intero(c[2], 0, 10 ** 9)
                    or not segnale(c[3], -40.0 * 10 ** 9, 40.0 * 10 ** 9) or not segnale(c[4], -40, 40)
                    or not segnale(c[5], -200, 0) or not intero(c[6], 0, 10 ** 9)
                    or not intero(c[7], 19700101, 29991231) or (c[2] > 0) != (c[4] is not None)):
                continue
            ripetitori = {}
            if len(c) == 9 and isinstance(c[8], dict):
                for chi, volte in list(c[8].items())[:20]:
                    if isinstance(chi, str) and re.fullmatch(r"![0-9a-f]{8}", chi) and intero(volte, 1, 10 ** 9):
                        ripetitori[int(chi[1:], 16)] = volte
            self.celle[(c[0], c[1])] = [c[2], float(c[3] or 0.0), c[4], c[5], c[6], c[7], ripetitori]
        dal = d.get("dal")
        self.dal = dal if intero(dal, 0, 4102444800) else None

    def annota(self, lat: float, lon: float, salti: int, snr: Optional[float], rssi: Optional[int],
               giorno: int, via: Optional[int] = None) -> None:
        """Una posizione ricevuta: diretta (salti 0, con il suo segnale), con un salto da un ripetitore fisso (via)
        o arrivata tramite altre radio."""
        chiave = self.quadretto(lat, lon)
        with self._lock:
            c = self.celle.get(chiave)
            if c is None:
                if len(self.celle) >= self.MASSIMO:
                    self._fai_spazio()
                c = self.celle[chiave] = [0, 0.0, None, None, 0, giorno, {}]
            if salti == 0 and snr is not None:
                c[0] += 1
                c[1] += snr
                c[2] = snr if c[2] is None else max(c[2], snr)
                if rssi is not None:
                    c[3] = rssi if c[3] is None else max(c[3], rssi)
            elif via is not None and (via in c[6] or len(c[6]) < 20):
                c[6][via] = c[6].get(via, 0) + 1
            else:
                c[4] += 1
            c[5] = max(c[5], giorno)
            if self.dal is None:
                self.dal = int(time.time())
            self._da_salvare = True

    def _fai_spazio(self) -> None:
        """Pieno: escono i quadretti visti meno di recente (e, a parita', con meno ricezioni)."""
        via = sorted(self.celle, key=lambda k: (self.celle[k][5],
                                                self.celle[k][0] + self.celle[k][4] + sum(self.celle[k][6].values())))
        for k in via[:max(1, len(via) // 10)]:
            del self.celle[k]

    def dati(self) -> dict:
        """Per la pagina della rete: [x, y, dirette, segnale medio, migliore, potenza migliore, tramite altre radio,
        giorno, {ripetitore: volte}]."""
        with self._lock:
            celle = [[x, y, c[0], round(c[1] / c[0], 1) if c[0] else None, c[2], c[3], c[4], c[5],
                      {f"!{r:08x}": volte for r, volte in sorted(c[6].items())}]
                     for (x, y), c in sorted(self.celle.items(), key=lambda kv: (kv[0][1], kv[0][0]))]
            return {"formato": 2, "zoom": self.ZOOM, "dal": self.dal, "celle": celle}

    def salva(self, subito: bool = False) -> None:
        if not self.percorso:
            return
        with self._lock:
            if not self._da_salvare or (not subito and time.monotonic() - self._salvata < self.SALVA_OGNI_S):
                return
            dati = {"formato": 2, "zoom": self.ZOOM, "dal": self.dal,
                    "celle": [[x, y, c[0], round(c[1], 2), c[2], c[3], c[4], c[5],
                               {f"!{r:08x}": volte for r, volte in c[6].items()}] for (x, y), c in self.celle.items()]}
            self._da_salvare = False
            self._salvata = time.monotonic()
        try:
            nuovo = self.percorso + ".nuovo"
            with open(os.open(nuovo, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
                json.dump(dati, f, separators=(",", ":"))
            os.replace(nuovo, self.percorso)
        except OSError as e:
            log.warning("Mappa della copertura: non riesco a salvarla (%s); riprovo piu' tardi", e)
            with self._lock:
                self._da_salvare = True

    def azzera(self) -> None:
        with self._lock:
            self.celle.clear()
            self.dal = None
            self._da_salvare = False
            self._salvata = time.monotonic()
        if self.percorso:
            try:
                os.unlink(self.percorso)
            except FileNotFoundError:
                pass
            except OSError as e:
                log.warning("Mappa della copertura: non riesco a cancellare %s (%s)", self.percorso, e)


class RadioSquadra:
    """Le radio sentite davvero sul canale della squadra (dalla 1.15.0), con l'ultima volta. Il database del nodo
    Meshtastic segna come "della squadra" anche radio sentite quando il canale principale del nodo era un altro (per
    esempio il pubblico LongFast prima dell'installazione): all'avvio il ponte prende dal database solo queste.
    Su file: {"formato": 1, "radio": {"!1234abcd": ultima volta}}."""

    TIENI_S = 90 * 86400            # una radio non sentita da tre mesi esce
    MASSIMO = 2000
    SALVA_OGNI_S = 600.0            # su file al massimo ogni 10 minuti

    def __init__(self, percorso: Optional[str] = None):
        self.percorso = percorso
        self._lock = threading.Lock()
        self.radio: Dict[int, float] = {}
        self._da_salvare = False
        self._salvata = time.monotonic()
        if percorso:
            try:
                with open(percorso, encoding="utf-8") as f:
                    d = json.load(f)
                if not isinstance(d, dict) or d.get("formato") != 1 or not isinstance(d.get("radio"), dict):
                    raise ValueError("formato sconosciuto")
                limite = time.time() - self.TIENI_S
                for chiave, quando in d["radio"].items():
                    if (isinstance(chiave, str) and re.fullmatch(r"![0-9a-f]{8}", chiave)
                            and isinstance(quando, (int, float)) and not isinstance(quando, bool) and quando > limite):
                        self.radio[int(chiave[1:], 16)] = float(quando)
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as e:
                log.warning("Radio della squadra: %s non valido (%s), riparto da zero", percorso, e)

    def __contains__(self, num) -> bool:
        with self._lock:
            return num in self.radio

    def segna(self, num: int, quando: float) -> None:
        with self._lock:
            if self.radio.get(num, 0.0) >= quando:
                return
            if num not in self.radio and len(self.radio) >= self.MASSIMO:
                del self.radio[min(self.radio, key=self.radio.get)]
            nuova = num not in self.radio
            self.radio[num] = quando
            self._da_salvare = True
        if nuova:
            log.info("Radio della squadra: !%08x sentita sul canale della squadra", num)

    def salva(self, subito: bool = False) -> None:
        if not self.percorso:
            return
        with self._lock:
            if not self._da_salvare or (not subito and time.monotonic() - self._salvata < self.SALVA_OGNI_S):
                return
            dati = {"formato": 1, "radio": {f"!{n:08x}": round(q) for n, q in self.radio.items()}}
            self._da_salvare = False
            self._salvata = time.monotonic()
        try:
            nuovo = self.percorso + ".nuovo"
            with open(os.open(nuovo, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
                json.dump(dati, f, separators=(",", ":"))
            os.replace(nuovo, self.percorso)
        except OSError as e:
            log.warning("Radio della squadra: non riesco a salvarle (%s); riprovo piu' tardi", e)
            with self._lock:
                self._da_salvare = True


class Storico:
    """Come vanno nel tempo il ponte e i ripetitori fissi (dalla 1.16.0), per i grafici della pagina della rete: un
    campione ogni mezz'ora, tenuto 30 giorni. Per un ripetitore [quando, batteria, tensione, segnale, sentito (0/1
    nella mezz'ora), messaggi ripetuti nella mezz'ora]; per il ponte [quando, canale occupato, tempo trasmesso, radio
    sulla mappa, TAK Server collegato (0/1)]. Niente posizioni. Su file:
    {"formato": 1, "ponte": [...], "ripetitori": {"!1234abcd": {"nome": ..., "campioni": [...]}}}."""

    OGNI_S = 1800
    TIENI_S = 30 * 86400
    MASSIMO_RIPETITORI = 30
    SALVA_OGNI_S = 1800.0

    def __init__(self, percorso: Optional[str] = None):
        self.percorso = percorso
        self._lock = threading.Lock()
        self.ponte: List[list] = []
        self.ripetitori: Dict[str, dict] = {}
        self._ripetuti: Dict[str, int] = {}       # contatore al campione precedente
        self._ultimo = 0.0
        self._da_salvare = False
        self._salvato = time.monotonic()
        if percorso:
            try:
                with open(percorso, encoding="utf-8") as f:
                    d = json.load(f)
                if not isinstance(d, dict) or d.get("formato") != 1:
                    raise ValueError("formato sconosciuto")
                limite = time.time() - self.TIENI_S
                self.ponte = self._validi(d.get("ponte"), 5, limite)
                ripetitori = d.get("ripetitori")
                for chiave, voce in (ripetitori if isinstance(ripetitori, dict) else {}).items():
                    if (isinstance(chiave, str) and re.fullmatch(r"![0-9a-f]{8}", chiave) and isinstance(voce, dict)
                            and len(self.ripetitori) < self.MASSIMO_RIPETITORI):
                        campioni = self._validi(voce.get("campioni"), 6, limite)
                        if campioni:
                            self.ripetitori[chiave] = {"nome": pulisci(voce.get("nome"), 40), "campioni": campioni}
                if self.ponte:
                    self._ultimo = self.ponte[-1][0]
            except FileNotFoundError:
                pass
            except (OSError, ValueError, TypeError) as e:
                log.warning("Storico: %s non valido (%s), riparto da zero", percorso, e)
                self.ponte, self.ripetitori = [], {}

    @staticmethod
    def _validi(campioni, lunghezza: int, limite: float) -> List[list]:
        buoni = []
        for c in campioni if isinstance(campioni, list) else []:
            if (isinstance(c, list) and len(c) == lunghezza and isinstance(c[0], (int, float))
                    and not isinstance(c[0], bool) and c[0] > limite
                    and all(x is None or (isinstance(x, (int, float)) and not isinstance(x, bool)) for x in c[1:])):
                buoni.append(c)
        return sorted(buoni, key=lambda c: c[0])

    def campiona(self, ora: float, ponte: list, ripetitori: List[Tuple[str, str, list, int]]) -> bool:
        """Al massimo un campione ogni mezz'ora. ponte: [occupato, trasmesso, radio sulla mappa, tak]; ripetitori:
        (id, nome, [batteria, tensione, segnale, sentito], contatore dei messaggi ripetuti). True se ha campionato."""
        with self._lock:
            if ora - self._ultimo < self.OGNI_S:
                return False
            self._ultimo = ora
            limite = ora - self.TIENI_S
            self.ponte = [c for c in self.ponte if c[0] > limite]
            self.ponte.append([round(ora)] + list(ponte))
            for chiave, nome, valori, contatore in ripetitori:
                voce = self.ripetitori.get(chiave)
                if voce is None:
                    if len(self.ripetitori) >= self.MASSIMO_RIPETITORI:
                        continue
                    voce = self.ripetitori[chiave] = {"nome": nome, "campioni": []}
                voce["nome"] = nome or voce["nome"]
                prima = self._ripetuti.get(chiave)
                self._ripetuti[chiave] = contatore
                # il contatore riparte da zero quando riparte il ponte
                nuovi = contatore if prima is None or contatore < prima else contatore - prima
                voce["campioni"].append([round(ora)] + list(valori) + [nuovi])
            for chiave in list(self.ripetitori):
                voce = self.ripetitori[chiave]
                voce["campioni"] = [c for c in voce["campioni"] if c[0] > limite]
                if not voce["campioni"]:
                    del self.ripetitori[chiave]
            self._da_salvare = True
            return True

    def dati(self) -> dict:
        with self._lock:
            return {"formato": 1, "ogni_s": self.OGNI_S, "ponte": [list(c) for c in self.ponte],
                    "ripetitori": {k: {"nome": v["nome"], "campioni": [list(c) for c in v["campioni"]]}
                                   for k, v in self.ripetitori.items()}}

    def salva(self, subito: bool = False) -> None:
        if not self.percorso:
            return
        with self._lock:
            if not self._da_salvare or (not subito and time.monotonic() - self._salvato < self.SALVA_OGNI_S):
                return
            self._da_salvare = False
            self._salvato = time.monotonic()
        dati = self.dati()
        del dati["ogni_s"]
        try:
            nuovo = self.percorso + ".nuovo"
            with open(os.open(nuovo, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
                json.dump(dati, f, separators=(",", ":"))
            os.replace(nuovo, self.percorso)
        except OSError as e:
            log.warning("Storico: non riesco a salvarlo (%s); riprovo piu' tardi", e)
            with self._lock:
                self._da_salvare = True


