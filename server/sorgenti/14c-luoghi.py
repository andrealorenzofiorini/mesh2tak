# ---- Dove mettere un ripetitore (dalla 1.18.0): cime, tralicci, serbatoi e rifugi da OpenStreetMap ----
# La pagina cerca i punti migliori per un impianto nuovo: le cime le trova da sola nel rilievo, ma il nome di una
# cima e i sostegni che ci sono gia' (un traliccio, una torre, un serbatoio dell'acqua, un rifugio: li' montare una
# radio e' piu' facile) li sa OpenStreetMap. Li chiede il server, non il browser, a un servizio pubblico (Overpass),
# un quadrato di un quarto di grado alla volta, e li tiene per tre mesi: chi apre la pagina non parla con siti in
# piu'. Se il servizio non risponde la ricerca funziona lo stesso, senza nomi e senza sostegni.

LUOGHI_DIR = SERVIZIO_DIR + "/luoghi"
LUOGHI_SORGENTI = ([os.environ["RETE_PROVA_LUOGHI"].rstrip("/")] if os.environ.get("RETE_PROVA_LUOGHI") else
                   ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"])
RE_LUOGHI = re.compile(r"^/api/luoghi/(-?\d{1,3})/(-?\d{1,3})\.json$")
# etichetta di OpenStreetMap -> il nostro tipo di sostegno
SOSTEGNI_OSM = (("man_made", "mast", "traliccio"), ("man_made", "communications_tower", "torre"),
                ("man_made", "water_tower", "acqua"), ("tourism", "alpine_hut", "rifugio"))


def luoghi_da_osm(risposta: dict) -> Tuple[List[list], List[list]]:
    """Dalla risposta di Overpass le cime [lat, lon, nome, quota] e i sostegni [lat, lon, tipo, nome]. Tutto quello
    che arriva da fuori si controlla: numeri veri, nomi corti e senza caratteri di comando."""
    def nome(etichette: dict) -> str:
        testo = etichette.get("name")
        testo = "".join(c for c in testo if c.isprintable()) if isinstance(testo, str) else ""
        return " ".join(testo.split())[:60]

    cime, sostegni = [], []
    for e in risposta.get("elements") if isinstance(risposta.get("elements"), list) else []:
        if not isinstance(e, dict):
            continue
        dove = e if e.get("type") == "node" else e.get("center")
        etichette = e.get("tags")
        if not isinstance(dove, dict) or not isinstance(etichette, dict):
            continue
        lat, lon = dove.get("lat"), dove.get("lon")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or v != v for v in (lat, lon)) or \
                not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        lat, lon = round(float(lat), 5), round(float(lon), 5)
        if etichette.get("natural") == "peak":
            quota = re.match(r"^\s*(-?\d{1,4}(?:\.\d+)?)\s*(?:m)?\s*$", str(etichette.get("ele", "")))
            cime.append([lat, lon, nome(etichette), round(float(quota.group(1))) if quota else None])
            continue
        tipo = next((t for chiave, valore, t in SOSTEGNI_OSM if etichette.get(chiave) == valore), None)
        if tipo is None and etichette.get("man_made") == "tower" and etichette.get("tower:type") == "communication":
            tipo = "torre"
        if tipo:
            sostegni.append([lat, lon, tipo, nome(etichette)])
    return cime[:Luoghi.VOCI_MAX], sostegni[:Luoghi.VOCI_MAX]


class Luoghi:
    """Cime e sostegni di un quadrato di un quarto di grado: chiesti una volta, tenuti tre mesi."""

    VALIDI = 90 * 86400
    NUOVI_OGNI_10_MIN = 12         # una ricerca su 40 km tocca al massimo quattro o sei quadrati
    QUADRATI_MAX = 400
    PESO_MAX = 8 << 20
    VOCI_MAX = 4000

    def __init__(self, cartella: Optional[str] = None, ora=time.time):
        self.cartella = cartella or P(LUOGHI_DIR)
        self.ora = ora
        self.lock = threading.Lock()
        self.chiesti: List[float] = []
        self.in_corso: Dict[str, threading.Event] = {}

    def _tenuto(self, percorso: str) -> Optional[dict]:
        try:
            with open(percorso, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return None
        buono = isinstance(d, dict) and isinstance(d.get("cime"), list) and isinstance(d.get("sostegni"), list) and \
            isinstance(d.get("quando"), (int, float))
        return d if buono else None

    def quadrato(self, la: int, lo: int) -> Tuple[int, dict]:
        """(codice, {"quando", "cime", "sostegni"}) del quadrato che comincia a la/4 gradi nord e lo/4 gradi est.
        404 fuori dal mondo, 429 se si chiede troppo, 502 se OpenStreetMap non risponde e non c'e' niente di tenuto."""
        if not (-360 <= la < 360 and -720 <= lo < 720):
            return 404, {}
        nome = f"{la}_{lo}.json"
        percorso = os.path.join(self.cartella, nome)
        while True:
            tenuto = self._tenuto(percorso)
            if tenuto and self.ora() - tenuto["quando"] < self.VALIDI:
                return 200, tenuto
            with self.lock:                  # uno solo chiede un quadrato; gli altri aspettano lui
                atteso = self.in_corso.get(nome)
                if atteso is None:
                    adesso = self.ora()
                    self.chiesti = [t for t in self.chiesti if adesso - t < 600]
                    if len(self.chiesti) >= self.NUOVI_OGNI_10_MIN:
                        return (200, tenuto) if tenuto else (429, {})
                    self.chiesti.append(adesso)
                    self.in_corso[nome] = threading.Event()
                    break
            atteso.wait(100)
            dopo = self._tenuto(percorso)
            if not dopo or self.ora() - dopo["quando"] >= self.VALIDI:
                return (200, dopo) if dopo else (502, {})
        try:
            nuovo = self._chiedi(la, lo)
            if nuovo is None:
                return (200, tenuto) if tenuto else (502, {})      # meglio vecchio che niente
            os.makedirs(self.cartella, mode=0o700, exist_ok=True)
            provvisorio = percorso + ".nuovo"
            with open(os.open(provvisorio, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
                json.dump(nuovo, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(provvisorio, percorso)
            self._fai_spazio()
            return 200, nuovo
        finally:
            with self.lock:
                self.in_corso.pop(nome).set()

    def _chiedi(self, la: int, lo: int) -> Optional[dict]:
        riquadro = f"({la / 4:g},{lo / 4:g},{(la + 1) / 4:g},{(lo + 1) / 4:g})"
        domanda = ("[out:json][timeout:60];(" + f'node["natural"="peak"]{riquadro};' +
                   "".join(f'nwr["{chiave}"="{valore}"]{riquadro};' for chiave, valore, _ in SOSTEGNI_OSM) +
                   f'nwr["man_made"="tower"]["tower:type"="communication"]{riquadro};' + ");out center tags;")
        errore = "nessuna sorgente"
        for sorgente in LUOGHI_SORGENTI:
            rq = urllib.request.Request(sorgente + "?" + urllib.parse.urlencode({"data": domanda}),
                                        headers={"User-Agent": f"mesh2tak-rete/{VERSIONE}", "Accept": "application/json"})
            try:
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(rq, timeout=75) as r:
                    grezzo = r.read(self.PESO_MAX + 1)
                if len(grezzo) > self.PESO_MAX:
                    raise ValueError("risposta troppo grande")
                risposta = json.loads(grezzo.decode("utf-8"))
                if not isinstance(risposta, dict) or not isinstance(risposta.get("elements"), list):
                    raise ValueError("risposta non prevista")
            except (urllib.error.URLError, OSError, ValueError) as e:
                errore = str(e)
                continue
            cime, sostegni = luoghi_da_osm(risposta)
            return {"formato": 1, "quando": int(self.ora()), "cime": cime, "sostegni": sostegni}
        print(f"luoghi: OpenStreetMap non risponde per il quadrato {la}/{lo} ({errore})", flush=True)
        return None

    def _fai_spazio(self) -> None:
        try:
            nomi = [n for n in os.listdir(self.cartella) if n.endswith(".json")]
            if len(nomi) <= self.QUADRATI_MAX:
                return
            per_eta = sorted(nomi, key=lambda n: os.path.getmtime(os.path.join(self.cartella, n)))
            for n in per_eta[:len(nomi) - self.QUADRATI_MAX + 50]:
                os.remove(os.path.join(self.cartella, n))
        except OSError:
            pass
