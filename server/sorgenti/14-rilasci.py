class Rilasci:
    """L'ultima versione pubblicata su GitHub (release pubbliche, niente token): la pagina dice se c'e' qualcosa
    di nuovo e con quali novita'. Il servizio legge solo questo; scaricare e installare lo fa root."""

    OGNI_S = 6 * 3600
    CONTROLLO_OGNI_S = float(os.environ.get("RETE_PROVA_RILASCI_CONTROLLO", "60"))   # "Controlla adesso"
    PRIMO_S = float(os.environ.get("RETE_PROVA_RILASCI_PRIMO", "60"))                # dopo l'avvio del servizio

    def __init__(self, ora=time.time, chiedi=None, percorso: Optional[str] = None):
        self.ora = ora
        self.chiedi = chiedi or self.ultima_release
        self.percorso = percorso or P(RILASCI)
        self.lock = threading.Lock()
        d = Servizio._leggi_json(self.percorso) or {}
        self.s = d if isinstance(d.get("controllato"), (int, float)) else {"controllato": 0, "ultima": None,
                                                                             "errore": None}

    @staticmethod
    def ultima_release() -> dict:
        apri = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        rq = urllib.request.Request(f"{GITHUB_API}/repos/{REPO}/releases/latest",
                                    headers={"Accept": "application/vnd.github+json",
                                             "User-Agent": f"mesh2tak-rete/{VERSIONE}"})
        with apri.open(rq, timeout=30) as r:
            return json.loads(r.read(1 << 20))

    def controlla(self, forza: bool = False) -> Tuple[bool, str]:
        ora = self.ora()
        with self.lock:
            ultimo = self.s.get("controllato") or 0
            if ora - ultimo < (self.CONTROLLO_OGNI_S if forza else self.OGNI_S):
                return False, "controllato da poco"
            self.s["controllato"] = ora
        ultima, errore = None, None
        try:
            d = self.chiedi()
        except urllib.error.HTTPError as e:
            errore = "nessuna versione pubblicata" if e.code == 404 else f"GitHub risponde {e.code}"
        except Exception as e:  # noqa
            errore = f"GitHub non risponde ({type(e).__name__})"
        else:
            tag = str(d.get("tag_name") or "") if isinstance(d, dict) else ""
            versione = tag[1:] if tag.startswith("v") else tag
            if not RE_VERSIONE_RILASCIO.match(versione):
                errore = "l'ultima release su GitHub non ha un numero di versione (vX.Y.Z)"
            else:
                nomi = {a.get("name") for a in d.get("assets") or [] if isinstance(a, dict)}
                completa = {"installa-ponte-meshtastic.sh", "rete-meshtastic.sh", "SHA256SUMS"} <= nomi
                ultima = {"versione": versione, "note": str(d.get("body") or "")[:4000],
                          "pubblicata": str(d.get("published_at") or "")[:30], "completa": completa}
        with self.lock:
            if ultima is not None:
                self.s["ultima"] = ultima     # se GitHub non risponde resta l'ultima vista
            self.s["errore"] = errore
            try:
                Servizio._scrivi_json(self.percorso, self.s)
            except OSError:
                pass
        print(f"rilasci: {'ultima ' + ultima['versione'] if ultima else errore}", flush=True)
        return True, errore or ""

    def ciclo(self) -> None:
        time.sleep(self.PRIMO_S)
        while True:
            try:
                self.controlla()
            except Exception as e:  # noqa
                print(f"rilasci: controllo non riuscito ({e})", flush=True)
            time.sleep(600)

    def vista(self) -> dict:
        with self.lock:
            ultima = self.s.get("ultima")
            nuova = bool(ultima and ultima.get("completa")
                         and numeri_versione(ultima["versione"]) > numeri_versione(VERSIONE))
            return {"installata": VERSIONE, "ultima": ultima, "nuova": nuova, "errore": self.s.get("errore"),
                    "controllato": round(self.s.get("controllato") or 0) or None}

    def pronta(self, versione: str) -> bool:
        """La versione chiesta dalla pagina e' quella pubblicata e completa (non una scritta a mano)."""
        with self.lock:
            u = self.s.get("ultima") or {}
            return bool(u.get("completa") and u.get("versione") == versione)


def porta_di(cfg: dict) -> int:
    porta = cfg.get("console")
    return porta if isinstance(porta, int) and 0 < porta < 65536 else 5001


