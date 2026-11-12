# --------------------------------------------------------------------------
# Il ponte
# --------------------------------------------------------------------------

@dataclass
class Canale:
    indice: int
    nome: str
    privato: bool


@dataclass
class Fonte:
    """Quanto serve per ricostruire l'evento CoT di una radio."""
    tipo_fonte: str            # "mesh" (radio Meshtastic) o "tak" (pacchetto TAK)
    uid: str
    lat: float
    lon: float
    hae: float
    ce: float
    t_fix: float
    how: str = "m-g"
    geosrc: Optional[str] = None
    altsrc: Optional[str] = None
    rotta: Optional[float] = None
    velocita: Optional[float] = None
    quota_msl: Optional[float] = None
    precisione_bit: int = 32
    canale: int = 0
    # solo per i pacchetti TAK
    tipo_cot: str = "a-f-G-U-C"
    callsign: Optional[str] = None
    squadra: Optional[str] = None
    ruolo: Optional[str] = None
    batteria: Optional[int] = None
    takv: Optional[Dict[str, str]] = None
    droid: Optional[str] = None
    endpoint: bool = False
    tracker: bool = False


@dataclass
class StatoNodo:
    num: int
    fonte: Fonte
    ultimo_segnale: float
    ultimo_invio: float = 0.0
    nome_inviato: str = ""
    batteria: Optional[int] = None      # ultimi valori mandati al server, per la pagina della rete
    salti: Optional[int] = None


@dataclass
class RadioVista:
    """Una radio della squadra come la sente questo ponte, per l'elenco delle radio della pagina della rete.
    Solo quello che le radio trasmettono da sole sul canale della squadra: il ponte non chiede nulla."""
    num: int
    ultimo: float = 0.0                 # ultimo pacchetto sentito (qualsiasi)
    nome: str = ""
    nome_breve: str = ""
    modello: str = ""                   # es. TRACKER_T1000_E
    ruolo: str = ""                     # es. TAK, CLIENT
    batteria: Optional[int] = None      # 0-100, 101 = alimentata
    tensione: Optional[float] = None
    occupato: Optional[float] = None    # canale occupato dove si trova la radio (%)
    trasmesso: Optional[float] = None   # tempo in cui la radio ha trasmesso nell'ultima ora (%)
    acceso_s: Optional[int] = None      # da quanto e' accesa, al momento delle metriche
    metriche: Optional[float] = None    # quando sono arrivate batteria e canale
    salti: Optional[int] = None         # ultimo pacchetto: 0 = sentita direttamente
    snr: Optional[float] = None         # ultimo pacchetto sentito direttamente
    rssi: Optional[int] = None
    diretto: Optional[float] = None     # quando e' stata sentita direttamente l'ultima volta
    posizione_fissa: bool = False       # dalla 1.15.0: manda una posizione impostata a mano (LOC_MANUAL)
    lat: Optional[float] = None         # ultima posizione (in memoria; esce solo per i ripetitori fissi)
    lon: Optional[float] = None
    ripetuti: int = 0                   # pacchetti di altri che il ponte ha ricevuto ripetuti da questa radio
    ultimo_ripetuto: Optional[float] = None

    @property
    def fisso(self) -> bool:
        """Un ripetitore fisso: ruolo da infrastruttura o posizione impostata a mano."""
        return self.ruolo in RUOLI_FISSI or self.posizione_fissa


# Ruoli Meshtastic dei nodi fissi che fanno da ripetitore (ROUTER_CLIENT e' il vecchio nome)
RUOLI_FISSI = frozenset({"ROUTER", "ROUTER_LATE", "ROUTER_CLIENT", "REPEATER", "CLIENT_BASE"})


RE_ENUM = re.compile(r"^[A-Z][A-Z0-9_]{0,39}$")


def numero(v, minimo: float, massimo: float, cifre: int = 1) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return round(x, cifre) if minimo <= x <= massimo and x == x else None


@dataclass
class VoceRadio:
    """Un evento del TAK Server in attesa di essere trasmesso alle radio."""
    priorita: int
    categoria: str
    uid: str
    descrizione: str
    filo: bytes
    impronta: str
    creata: float
    pronta: float       # non prima: intanto si ascolta se un altro ponte lo trasmette gia'
    scadenza: float     # dopo questa ora l'evento non serve piu'


class CodaRadio:
    """Eventi da trasmettere, i piu' urgenti per primi. Di uno stesso oggetto (UID) resta
    solo l'ultima versione; le chat restano tutte."""

    ETA_MASSIMA_S = 30 * 60

    def __init__(self):
        self._voci: List[VoceRadio] = []
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._voci)

    def metti(self, v: VoceRadio) -> bool:
        """True se ha sostituito una versione precedente dello stesso oggetto."""
        with self._lock:
            if v.categoria != "chat":
                for i, vecchia in enumerate(self._voci):
                    if vecchia.uid == v.uid and vecchia.categoria == v.categoria:
                        v.creata = vecchia.creata
                        self._voci[i] = v
                        return True
            self._voci.append(v)
            return False

    def contiene(self, impronta: str) -> bool:
        with self._lock:
            return any(v.impronta == impronta for v in self._voci)

    def togli_impronta(self, impronta: str) -> List[VoceRadio]:
        with self._lock:
            tolte = [v for v in self._voci if v.impronta == impronta]
            self._voci = [v for v in self._voci if v.impronta != impronta]
            return tolte

    def scadute(self, ora: float) -> List[VoceRadio]:
        with self._lock:
            vecchie = [v for v in self._voci if v.scadenza <= ora or ora - v.creata > self.ETA_MASSIMA_S]
            if vecchie:
                self._voci = [v for v in self._voci if v not in vecchie]
            return vecchie

    def prossima(self, ora: float) -> Optional[VoceRadio]:
        """La prossima da trasmettere (tolta dalla coda), o None."""
        with self._lock:
            pronte = [v for v in self._voci if v.pronta <= ora]
            if not pronte:
                return None
            v = min(pronte, key=lambda x: (x.priorita, x.creata))
            self._voci.remove(v)
            return v

    def guarda(self, ora: float) -> Optional[VoceRadio]:
        with self._lock:
            pronte = [v for v in self._voci if v.pronta <= ora]
            return min(pronte, key=lambda x: (x.priorita, x.creata)) if pronte else None

    def rimetti(self, v: VoceRadio) -> None:
        with self._lock:
            self._voci.append(v)


RE_ID_RADIO = re.compile(r"^![0-9a-f]{8}$")
RE_VERSIONE_ICONE = re.compile(r"^[0-9a-f]{16}$")
RE_PERCORSO_ICONA = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/[A-Za-z][A-Za-z0-9_-]{0,39}/"
                               r"[A-Za-z0-9][A-Za-z0-9_-]{0,59}\.png$")
RE_TIPO_ICONA = re.compile(r"^a-f-[A-Z](-[A-Za-z]{1,3}){0,5}$")


class IconeMappa:
    """Le icone sulla mappa scelte dalla pagina della rete per le radio e per i ponti (dalla 1.11.0). Le manda
    il server dal tunnel quando cambiano; restano su file per i riavvii del ponte."""

    MASSIMO = 500

    def __init__(self, percorso: Optional[str] = None):
        self.percorso = percorso
        self._lock = threading.Lock()
        self.mappa: Dict[str, Tuple[str, str]] = {}      # "!7c3a91e2" -> (tipo CoT, figura dell'insieme di icone)
        self.versione = ""
        if percorso:
            try:
                with open(percorso, encoding="utf-8") as f:
                    self.versione, self.mappa = self.valida(json.load(f))
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as e:
                log.warning("Icone sulla mappa: %s non valido (%s), uso quelle predefinite", percorso, e)

    @classmethod
    def valida(cls, d) -> Tuple[str, Dict[str, Tuple[str, str]]]:
        if not isinstance(d, dict) or d.get("formato") != 1:
            raise ValueError("formato sconosciuto")
        versione, icone = d.get("versione"), d.get("icone")
        if not isinstance(versione, str) or not RE_VERSIONE_ICONE.match(versione) or not isinstance(icone, dict):
            raise ValueError("versione o elenco non validi")
        if len(icone) > cls.MASSIMO:
            raise ValueError("troppe radio")
        mappa = {}
        for radio, v in icone.items():
            if not isinstance(radio, str) or not RE_ID_RADIO.match(radio) or not isinstance(v, dict):
                raise ValueError("radio non valida")
            tipo, percorso = v.get("tipo"), v.get("percorso")
            if not isinstance(tipo, str) or not RE_TIPO_ICONA.match(tipo):
                raise ValueError(f"tipo non valido per {radio}")
            if not isinstance(percorso, str) or not RE_PERCORSO_ICONA.match(percorso):
                raise ValueError(f"icona non valida per {radio}")
            mappa[radio] = (tipo, percorso)
        return versione, mappa

    def imposta(self, corpo: Optional[bytes]) -> Tuple[int, dict, Set[str]]:
        """(stato HTTP, risposta, radio che cambiano icona)."""
        try:
            d = json.loads((corpo or b"").decode("utf-8"))
            versione, mappa = self.valida(d)
        except (ValueError, UnicodeDecodeError) as e:
            return 400, {"errore": f"icone non valide: {e}"}, set()
        with self._lock:
            cambiate = {r for r in set(mappa) | set(self.mappa) if mappa.get(r) != self.mappa.get(r)}
            self.versione, self.mappa = versione, mappa
            if self.percorso:
                try:
                    nuovo = self.percorso + ".nuovo"
                    with open(os.open(nuovo, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644), "w",
                              encoding="utf-8") as f:
                        json.dump(d, f)
                    os.replace(nuovo, self.percorso)
                except OSError as e:
                    log.warning("Icone sulla mappa: non riesco a salvarle (%s): valgono fino al riavvio", e)
        log.info("Icone sulla mappa dalla pagina della rete: %d radio con un'icona scelta (%d cambiate)",
                 len(mappa), len(cambiate))
        return 200, {"versione": versione, "numero": len(mappa)}, cambiate

    def di(self, radio: str) -> Optional[Tuple[str, str]]:
        with self._lock:
            return self.mappa.get(radio)

    def stato(self) -> dict:
        with self._lock:
            return {"versione": self.versione, "numero": len(self.mappa)}


# Sotto questo rapporto segnale/rumore (dB) un pacchetto LoRa non si decifra piu': dipende dallo "spreading factor"
# del preset (SF7 -7,5 dB ... SF12 -20 dB). Quanto il segnale sta sopra questo limite dice quanto e' solido il
# collegamento: e' il margine che la pagina della rete colora nella mappa della copertura.
SF_DEI_PRESET = {"SHORT_TURBO": 7, "SHORT_FAST": 7, "SHORT_SLOW": 8, "MEDIUM_FAST": 9, "MEDIUM_SLOW": 10,
                 "LONG_TURBO": 11, "LONG_FAST": 11, "LONG_MODERATE": 11, "LONG_SLOW": 12, "VERY_LONG_SLOW": 12}


def limite_snr(preset: str) -> float:
    return -2.5 * (SF_DEI_PRESET.get(preset, 11) - 4)


