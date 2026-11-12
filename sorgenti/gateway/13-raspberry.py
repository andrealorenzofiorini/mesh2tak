# --------------------------------------------------------------------------
# Come sta il Raspberry (per la pagina della rete e gli avvisi)
# --------------------------------------------------------------------------

class Sistema:
    """Accensione, alimentazione, spazio sulla microSD, protezioni messe dall'installatore e data della
    microSD di scorta. Solo letture di file: niente comandi, niente privilegi."""

    PROTEZIONI = {
        "watchdog": "/etc/systemd/system.conf.d/mesh2tak-watchdog.conf",
        "aggiornamenti": "/etc/apt/apt.conf.d/52mesh2tak-aggiornamenti",
        "registri": "/etc/systemd/journald.conf.d/mesh2tak.conf",
    }
    SCORTA = "/etc/mesh2tak/scorta.json"
    AGGIORNAMENTO = "/var/lib/mesh2tak-aggiorna/stato.json"           # lo scrive mesh2tak-aggiorna (root)
    TIMER_AGGIORNA = "/etc/systemd/system/mesh2tak-aggiorna.timer"
    # dalla 1.13.0: esiti dei comandi della pagina della rete e registro per la pagina (li scrive mesh2tak-aggiorna);
    # l'unita' .path dice che il Raspberry li fa (installatore 1.13.0 o successivo)
    AZIONI = "/var/lib/mesh2tak-aggiorna/azioni.json"
    REGISTRO = "/var/lib/mesh2tak-aggiorna/registro.txt"
    SVEGLIA_AGGIORNA = "/etc/systemd/system/mesh2tak-aggiorna.path"
    NOMI_AZIONI = ("riavvia-programma", "riavvia-radio", "riavvia-raspberry", "registro", "impostazioni", "certificato",
                   "reimposta-radio")
    STATI_AZIONI = ("in_corso", "fatto", "errore", "scaduto")
    AVVISO_LOG_S = 600.0               # nel registro al massimo una riga ogni 10 minuti sulla corrente

    def __init__(self, radice: str = ""):
        self.radice = radice           # solo per le prove: una cartella al posto di /
        self._lock = threading.Lock()
        self.inizio = time.time()
        self.bassa_ora = False
        self.bassa_ultima: Optional[float] = None
        self.bassa_volte = 0
        self._log_bassa = -1e18
        self.allarme = self._trova_allarme()

    def _p(self, percorso: str) -> str:
        return self.radice + percorso if self.radice else percorso

    def _leggi(self, percorso: str, massimo: int = 4096) -> Optional[str]:
        try:
            with open(self._p(percorso), encoding="utf-8", errors="replace") as f:
                return f.read(massimo)
        except OSError:
            return None

    def _trova_allarme(self) -> Optional[str]:
        """Il segnale di corrente insufficiente del Raspberry (driver rpi_volt del kernel), se c'e'."""
        base = self._p("/sys/class/hwmon")
        try:
            cartelle = sorted(os.listdir(base))
        except OSError:
            return None
        for d in cartelle:
            try:
                with open(os.path.join(base, d, "name"), encoding="ascii", errors="replace") as f:
                    nome = f.read(64).strip()
            except OSError:
                continue
            allarme = os.path.join(base, d, "in0_lcrit_alarm")
            if nome == "rpi_volt" and os.path.exists(allarme):
                return allarme
        return None

    def controlla_alimentazione(self, ora: Optional[float] = None) -> None:
        """Il kernel chiede al Raspberry ogni 2 secondi se la corrente e' scesa sotto il minimo, e a ogni
        controllo azzera il segnale: guardando ogni secondo non si perde nessun calo, anche breve."""
        if not self.allarme:
            return
        try:
            with open(self.allarme, encoding="ascii") as f:
                bassa = f.read(8).strip() == "1"
        except (OSError, UnicodeDecodeError):
            return
        ora = time.time() if ora is None else ora
        with self._lock:
            if bassa:
                if not self.bassa_ora:
                    self.bassa_volte += 1
                    if ora - self._log_bassa >= self.AVVISO_LOG_S:
                        self._log_bassa = ora
                        log.warning("Alimentazione insufficiente: la corrente non basta al Raspberry "
                                    "(alimentatore o cavo deboli: serve quello da 5,1 V e 2,5 A)")
                self.bassa_ultima = ora
            self.bassa_ora = bassa

    def ciclo(self, fermo: threading.Event) -> None:
        while not fermo.wait(1.0):
            self.controlla_alimentazione()

    def stato(self) -> dict:
        ora = time.time()
        avvio: Optional[str] = (self._leggi("/proc/sys/kernel/random/boot_id", 64) or "").strip().lower()
        if not re.match(r"^[0-9a-f-]{32,36}$", avvio or ""):
            avvio = None                        # cambia a ogni accensione: cosi' la rete vede i riavvii
        acceso_dal = None
        campi = (self._leggi("/proc/uptime", 64) or "").split()
        if campi:
            try:
                acceso_dal = round(ora - float(campi[0]))
            except ValueError:
                pass
        disco = None
        try:
            s = os.statvfs(self._p("/"))
            disco = {"libero_mb": s.f_bavail * s.f_frsize // 1048576, "totale_mb": s.f_blocks * s.f_frsize // 1048576}
        except OSError:
            pass
        protezioni = {k: os.path.exists(self._p(v)) for k, v in self.PROTEZIONI.items()}
        stato_wd = (self._leggi("/sys/class/watchdog/watchdog0/state", 32) or "").strip()
        if stato_wd in ("active", "inactive"):
            protezioni["watchdog"] = stato_wd == "active"     # quello che fa davvero il chip
        scorta = None
        try:
            d = json.loads(self._leggi(self.SCORTA) or "null")
        except ValueError:
            d = None
        if isinstance(d, dict) and isinstance(d.get("quando"), (int, float)) and not isinstance(d["quando"], bool):
            scorta = {"quando": round(d["quando"]), "disco": pulisci(d.get("disco"), 80)}
        aggiornamento = None
        try:
            a = json.loads(self._leggi(self.AGGIORNAMENTO) or "null")
        except ValueError:
            a = None
        if isinstance(a, dict) and a.get("stato") in ("in_corso", "fatto", "errore", "ripristinato"):
            aggiornamento = {"stato": a["stato"], "versione": pulisci(a.get("versione"), 20),
                             "da": pulisci(a.get("da"), 20), "messaggio": pulisci(a.get("messaggio"), 400),
                             "quando": round(a["quando"]) if isinstance(a.get("quando"), (int, float)) else None}
        with self._lock:
            alimentazione = None if not self.allarme else {
                "bassa_ora": self.bassa_ora, "ultima": round(self.bassa_ultima) if self.bassa_ultima else None,
                "volte": self.bassa_volte, "dal": round(self.inizio)}
        registro = None
        try:
            st = os.stat(self._p(self.REGISTRO))
            registro = {"quando": round(st.st_mtime), "byte": st.st_size}
        except OSError:
            pass
        return {"avvio": avvio, "acceso_dal": acceso_dal, "alimentazione": alimentazione, "disco": disco,
                "protezioni": protezioni, "scorta": scorta, "aggiornabile": os.path.exists(self._p(self.TIMER_AGGIORNA)),
                "aggiornamento": aggiornamento, "comandi": os.path.exists(self._p(self.SVEGLIA_AGGIORNA)),
                "azioni": self._azioni(), "registro": registro}

    def _azioni(self) -> List[dict]:
        """Gli ultimi comandi della pagina della rete e com'e' andata (dalla 1.13.0)."""
        try:
            d = json.loads(self._leggi(self.AZIONI, 1 << 20) or "null")
        except ValueError:
            d = None
        elenco = d.get("azioni") if isinstance(d, dict) else None
        risultato = []
        for a in (elenco if isinstance(elenco, list) else [])[-12:]:
            if (not isinstance(a, dict) or not re.match(r"^[0-9a-f]{16}$", str(a.get("id", "")))
                    or a.get("azione") not in self.NOMI_AZIONI or a.get("stato") not in self.STATI_AZIONI):
                continue
            numero = (lambda x: round(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None)
            risultato.append({"id": a["id"], "azione": a["azione"], "stato": a["stato"],
                              "messaggio": pulisci(a.get("messaggio"), 400), "chiesto": numero(a.get("chiesto")),
                              "quando": numero(a.get("quando"))})
        return risultato


_CERTIFICATI: Dict[str, Tuple[float, Optional[dict]]] = {}


def certificato_tak(percorso: str) -> Optional[dict]:
    """Nome e scadenza del certificato del ponte: alla scadenza il TAK Server lo rifiuta."""
    try:
        mtime = os.path.getmtime(percorso)
    except OSError:
        return None
    gia = _CERTIFICATI.get(percorso)
    if gia and gia[0] == mtime:
        return gia[1]
    dati: Optional[dict] = None
    try:
        d = ssl._ssl._test_decode_cert(percorso)  # noqa: SLF001  (legge il primo certificato del file)
        nome = ""
        for rdn in d.get("subject", ()):
            for k, v in rdn:
                if k == "commonName":
                    nome = v
        dati = {"scade": int(ssl.cert_time_to_seconds(d["notAfter"])), "nome": pulisci(nome, 64)}
    except Exception:  # noqa  (Python senza il lettore interno: lo chiede a openssl)
        try:
            r = subprocess.run(["openssl", "x509", "-noout", "-enddate", "-in", percorso],
                               capture_output=True, text=True, timeout=10)
            fine = r.stdout.strip().split("=", 1)[1] if r.returncode == 0 and "=" in r.stdout else ""
            dati = {"scade": int(ssl.cert_time_to_seconds(fine)), "nome": ""} if fine else None
        except Exception:  # noqa
            dati = None
    _CERTIFICATI[percorso] = (mtime, dati)
    return dati


