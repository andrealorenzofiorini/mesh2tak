# ---- Copertura prevista (dalla 1.17.0): il rilievo e i dati veri di ogni impianto ----
# La pagina calcola da sola, nel browser, dove arriva ogni gateway e ripetitore. Le servono due cose dal server:
# - il rilievo: riquadri di quote (formato "terrarium": la quota in metri nei tre colori di un PNG) dei Terrain Tiles
#   pubblici su AWS. Li chiede il server, non il browser, e li tiene: il rilievo non cambia, chi apre la pagina non
#   parla con siti in piu' e la mappa prevista funziona anche se quel sito un giorno non risponde;
# - i dati dell'impianto di ogni gateway e ripetitore (altezza e guadagno dell'antenna, perdite del cavo, potenza),
#   scritti dagli amministratori: senza, il calcolo userebbe valori di fabbrica uguali per tutti.

TERRENO_DIR = SERVIZIO_DIR + "/terreno"
TERRENO_SORGENTE = (os.environ.get("RETE_PROVA_TERRENO") or
                    "https://s3.amazonaws.com/elevation-tiles-prod/terrarium").rstrip("/")
IMPIANTI = SERVIZIO_DIR + "/impianti.json"
RE_TERRENO = re.compile(r"^/api/terreno/(\d{1,2})/(\d{1,5})/(\d{1,5})\.png$")
RE_IMPIANTO = re.compile(r"^(?:g:[a-z0-9](?:[a-z0-9-]{0,18}[a-z0-9])?|r:![0-9a-f]{8}|portatile)$")
# campo -> (minimo, massimo): quello che un amministratore puo' scrivere per un impianto
CAMPI_IMPIANTO = {"altezza_m": (0.5, 120.0), "guadagno_dbi": (-5.0, 20.0), "perdite_db": (0.0, 20.0),
                  "potenza_dbm": (0.0, 30.0)}
IMPIANTI_MAX = 300


class Terreno:
    """I riquadri del rilievo: presi una volta dalla sorgente, poi dati dalla cartella del servizio."""

    ZOOM = (9, 12)                 # 12: circa 27 m per punto alle nostre latitudini; 9: circa 210 m
    RIQUADRI_MAX = 600             # in tutto (ognuno pesa in media un centinaio di kB: al massimo qualche decina di MB)
    NUOVI_OGNI_10_MIN = 400        # presi dalla sorgente: una mappa da 30 km di raggio ne chiede una quarantina
    PESO_MAX = 1 << 20

    def __init__(self, cartella: Optional[str] = None, ora=time.time):
        self.cartella = cartella or P(TERRENO_DIR)
        self.ora = ora
        self.lock = threading.Lock()
        self.presi: List[float] = []
        self.in_corso: Dict[str, threading.Event] = {}

    def riquadro(self, z: int, x: int, y: int) -> Tuple[int, bytes]:
        """(codice, PNG). 404 fuori dal mondo o dai livelli previsti, 429 se si chiede troppo alla sorgente,
        502 se la sorgente non risponde."""
        if not (self.ZOOM[0] <= z <= self.ZOOM[1]) or x >= 2 ** z or y >= 2 ** z:
            return 404, b""
        nome = f"{z}-{x}-{y}.png"
        percorso = os.path.join(self.cartella, nome)
        while True:
            try:
                with open(percorso, "rb") as f:
                    dati = f.read(self.PESO_MAX + 1)
                if dati.startswith(b"\x89PNG\r\n\x1a\n") and len(dati) <= self.PESO_MAX:
                    return 200, dati
            except OSError:
                pass
            with self.lock:                  # uno solo scarica un riquadro; gli altri aspettano lui
                atteso = self.in_corso.get(nome)
                if atteso is None:
                    adesso = self.ora()
                    self.presi = [t for t in self.presi if adesso - t < 600]
                    if len(self.presi) >= self.NUOVI_OGNI_10_MIN:
                        return 429, b""
                    self.presi.append(adesso)
                    self.in_corso[nome] = threading.Event()
                    break
            atteso.wait(30)
            if not os.path.exists(percorso):
                return 502, b""
        try:
            dati = self._scarica(z, x, y)
            if dati is None:
                return 502, b""
            os.makedirs(self.cartella, mode=0o700, exist_ok=True)
            nuovo = percorso + ".nuovo"
            with open(os.open(nuovo, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
                f.write(dati)
            os.replace(nuovo, percorso)
            self._fai_spazio()
            return 200, dati
        finally:
            with self.lock:
                self.in_corso.pop(nome).set()

    def _scarica(self, z: int, x: int, y: int) -> Optional[bytes]:
        rq = urllib.request.Request(f"{TERRENO_SORGENTE}/{z}/{x}/{y}.png",
                                    headers={"User-Agent": f"mesh2tak-rete/{VERSIONE}"})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(rq, timeout=20) as r:
                dati = r.read(self.PESO_MAX + 1)
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"rilievo: il riquadro {z}/{x}/{y} non arriva ({e})", flush=True)
            return None
        if not dati.startswith(b"\x89PNG\r\n\x1a\n") or len(dati) > self.PESO_MAX:
            print(f"rilievo: il riquadro {z}/{x}/{y} non e' un'immagine valida", flush=True)
            return None
        return dati

    def _fai_spazio(self) -> None:
        try:
            nomi = [n for n in os.listdir(self.cartella) if n.endswith(".png")]
            if len(nomi) <= self.RIQUADRI_MAX:
                return
            per_eta = sorted(nomi, key=lambda n: os.path.getmtime(os.path.join(self.cartella, n)))
            for n in per_eta[:len(nomi) - self.RIQUADRI_MAX + 100]:
                os.remove(os.path.join(self.cartella, n))
        except OSError:
            pass


def leggi_impianti(percorso: Optional[str] = None) -> Dict[str, dict]:
    try:
        with open(percorso or P(IMPIANTI), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    voci = d.get("impianti") if isinstance(d, dict) and isinstance(d.get("impianti"), dict) else {}
    return {k: v for k, v in voci.items() if RE_IMPIANTO.match(str(k)) and isinstance(v, dict)}


def valida_impianto(dati: dict) -> Tuple[Optional[dict], str]:
    """I numeri di un impianto, ognuno nel suo intervallo; un campo assente o vuoto vuol dire "non lo so" (il
    calcolo usa il valore di fabbrica e lo dice)."""
    pulito = {}
    for campo, (minimo, massimo) in CAMPI_IMPIANTO.items():
        v = dati.get(campo)
        if v is None or v == "":
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or not (minimo <= v <= massimo):
            return None, f"{campo}: serve un numero tra {minimo:g} e {massimo:g}"
        pulito[campo] = round(float(v), 1)
    return pulito, ""
