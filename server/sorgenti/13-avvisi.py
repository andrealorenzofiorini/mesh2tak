# ---- servizio della pagina (utente mesh2tak-rete, solo 127.0.0.1, dietro Caddy) ----

RE_MACCHINA = re.compile(r"^[A-Za-z0-9._-]{1,63}$")
RE_VERSIONE = re.compile(r"^[0-9a-z.+-]{1,20}$")


# ---------------------------------------------------------------- avvisi per email

try:
    from zoneinfo import ZoneInfo
    FUSO = ZoneInfo("Europe/Rome")     # le ore nelle email come in Italia, anche se il server e' in UTC
except Exception:  # noqa
    FUSO = None


def _ore(ts: float) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(ts, FUSO) if FUSO else datetime.datetime.fromtimestamp(ts)


def quando_it(ts: float, ora: float) -> str:
    q, adesso = _ore(ts), _ore(ora)
    if q.date() == adesso.date():
        return q.strftime("%H:%M")
    return f"{q.day}/{q.month} alle {q.strftime('%H:%M')}"


def giorno_it(ts: float) -> str:
    q = _ore(ts)
    return f"{q.day}/{q.month}/{q.year}"


def durata_it(s: float) -> str:
    m = max(1, int(round(s / 60)))
    if m < 60:
        return f"{m} minut{'o' if m == 1 else 'i'}"
    h, m = divmod(m, 60)
    if h < 48:
        testo = f"{h} or{'a' if h == 1 else 'e'}"
        return testo + (f" e {m} minut{'o' if m == 1 else 'i'}" if m else "")
    return f"{h // 24} giorni"


def _d(x) -> dict:
    """Un pezzo dello stato di un ponte come dizionario (un ponte rotto o malevolo non ferma gli avvisi degli altri)."""
    return x if isinstance(x, dict) else {}


def problemi_di(st: dict) -> Dict[str, str]:
    """I problemi che durano (si avvisa dopo 10 minuti): radio staccata (non durante la gestione del nodo),
    TAK Server non collegato, microSD quasi piena."""
    out: Dict[str, str] = {}
    pausa = _d(st.get("pausa")).get("attiva")
    if not _d(st.get("nodo")).get("collegato") and not pausa:
        out["radio"] = ""
    tak = _d(st.get("tak"))
    if not tak.get("collegato"):
        out["tak"] = re.sub(r"\s+", " ", str(tak.get("errore") or ""))[:200]
    libero = _d(_d(st.get("sistema")).get("disco")).get("libero_mb")
    if isinstance(libero, int) and not isinstance(libero, bool) and libero < Sorveglianza.DISCO_MIN_MB:
        out["spazio"] = str(libero)
    return out


class Sorveglianza:
    """Ogni minuto chiede lo stato a ogni ponte (attraverso il suo tunnel) e manda un'email con la posta di
    infra-TAK, agli stessi destinatari degli avvisi di Guard Dog: quando un problema dura da 10 minuti e quando
    passa, quando il certificato sta per scadere, quando la corrente non basta e quando un Raspberry si riavvia.
    Tutto su file: dopo un riavvio del servizio non rimanda e non perde gli avvisi."""

    OGNI_S = float(os.environ.get("RETE_PROVA_AVVISI_OGNI", "60"))
    ATTESA_S = int(os.environ.get("RETE_PROVA_AVVISI_ATTESA", "600"))
    RIPROVA_S = 300                    # email non partita: si riprova dopo 5 minuti
    RIPROVA_NESSUNO_S = 1800           # nessun destinatario o avvisi sospesi in infra-TAK: ogni mezz'ora
    CODA_MAX = 30
    GIORNI_CERTIFICATO = (30, 7, 1, 0)
    DISCO_MIN_MB = 500
    CORRENTE_FINESTRA_S = 3600         # cali di corrente nell'ultima ora...
    CORRENTE_OGNI_S = 24 * 3600        # ...avvisati al massimo una volta al giorno
    PROVA_OGNI_S = 30
    # dalla 1.16.0 anche i ripetitori fissi (nodi senza internet: li vede solo chi li sente)
    RIPETITORE_SILENZIO_S = 12 * 3600  # un Router manda il nome ogni 3 ore: 12 ore senza niente e' spento o isolato
    RIPETITORE_BATTERIA = 20           # % o meno: avviso; passa sopra il 30%
    RIPETITORE_BATTERIA_OK = 30
    RIPETITORE_METRICHE_S = 24 * 3600  # una batteria letta piu' di un giorno fa non dice niente
    RUOLI_MUTI = ("CLIENT_MUTE", "CLIENT_HIDDEN")

    def __init__(self, ora=time.time, prendi=None, manda=None, percorso: Optional[str] = None, config=None):
        self.ora = ora
        self.prendi = prendi or self.chiedi_stato
        self.manda = manda or (lambda oggetto, testo, cfg: self.invia(porta_di(cfg), oggetto, testo))
        self.percorso = percorso or P(STATO_AVVISI)
        self.config = config or (lambda: Servizio._leggi_json(P(SORVEGLIANZA)) or {})
        self.lock = threading.Lock()
        self.s = self._carica()
        self._ultima_prova = 0.0
        self.dopo = None        # dalla 1.11.0: (nome, porta, stato) dopo ogni lettura, per le icone sulla mappa

    # ---- stato su file ----

    def _carica(self) -> dict:
        d = Servizio._leggi_json(self.percorso) or {}
        if not isinstance(d.get("ponti"), dict):
            d["ponti"] = {}
        if not isinstance(d.get("coda"), list):
            d["coda"] = []
        d["coda"] = [m for m in d["coda"] if isinstance(m, dict) and isinstance(m.get("ponte"), str)]
        if not isinstance(d.get("ultimo"), dict):
            d["ultimo"] = None
        if not isinstance(d.get("tentativo"), (int, float)):
            d["tentativo"] = 0
        if not isinstance(d.get("ripetitori"), dict):
            d["ripetitori"] = {}
        return d

    def _salva(self) -> None:
        try:
            Servizio._scrivi_json(self.percorso, self.s)
        except OSError as e:
            print(f"avvisi: non riesco a salvare lo stato ({e})", flush=True)

    # ---- il giro di ogni minuto ----

    def ciclo(self) -> None:
        time.sleep(min(30, self.OGNI_S))        # i tunnel si ricollegano dopo un riavvio del server
        while True:
            try:
                self.giro()
            except Exception as e:  # noqa
                print(f"avvisi: giro non riuscito ({type(e).__name__}: {e})", flush=True)
            time.sleep(self.OGNI_S)

    def giro(self) -> None:
        cfg = self.config()
        ponti = {}
        for p in cfg.get("ponti") or []:
            if isinstance(p, dict) and RE_NOME.match(str(p.get("nome", ""))) and isinstance(p.get("porta"), int):
                ponti[p["nome"]] = p["porta"]
        with self.lock:
            for nome in [n for n in self.s["ponti"] if n not in ponti]:   # ponte tolto: niente piu' avvisi
                del self.s["ponti"][nome]
                self.s["coda"] = [m for m in self.s["coda"] if m.get("ponte") != nome]
        stati = {}
        for nome, porta in sorted(ponti.items()):
            st = stati[nome] = self.prendi(porta)
            with self.lock:
                self._valuta(nome, st, self.ora())
            if self.dopo is not None:
                try:
                    self.dopo(nome, porta, st)
                except Exception as e:  # noqa
                    print(f"icone: gateway {nome} ({type(e).__name__}: {e})", flush=True)
        with self.lock:
            self._valuta_ripetitori(stati, self.ora())
        self._consegna(cfg)

    @classmethod
    def ripetitori_di(cls, stati: Dict[str, Optional[dict]]) -> Dict[str, dict]:
        """I ripetitori fissi sentiti dai gateway, uniti: per ognuno da quanto tace (il minimo tra i gateway, contando
        anche l'ultimo messaggio che ha ripetuto) e la batteria piu' recente. Le eta' con l'orologio di ogni gateway."""
        out: Dict[str, dict] = {}
        nodi = {str(_d(st).get("id_nodo") or "") for st in stati.values()}
        for nome, st in stati.items():
            st = _d(st)
            adesso = st.get("ora")
            if not isinstance(adesso, (int, float)) or not isinstance(st.get("radio_rete"), list):
                continue
            for x in st["radio_rete"]:
                x = _d(x)
                chiave = x.get("id")
                if (not isinstance(chiave, str) or not re.fullmatch(r"![0-9a-f]{8}", chiave) or chiave in nodi
                        or x.get("fisso") is not True or x.get("ruolo") in cls.RUOLI_MUTI):
                    continue
                volte = [v for v in (x.get("ultimo"), x.get("ultimo_ripetuto"))
                         if isinstance(v, (int, float)) and not isinstance(v, bool)]
                if not volte:
                    continue
                tace = max(0.0, adesso - max(volte))
                r = out.setdefault(chiave, {"nome": "", "tace": tace, "batteria": None, "eta_batteria": None,
                                            "gateway": nome})
                if tace <= r["tace"]:
                    r["tace"], r["gateway"] = tace, nome
                titolo = re.sub(r"[^\w .,()'+-]", "", str(x.get("nome") or ""))[:40].strip()
                if titolo:
                    r["nome"] = titolo
                b, m = x.get("batteria"), x.get("metriche")
                if (isinstance(b, int) and not isinstance(b, bool) and 0 <= b <= 101 and isinstance(m, (int, float))
                        and (r["eta_batteria"] is None or adesso - m < r["eta_batteria"])):
                    r["batteria"], r["eta_batteria"] = b, max(0.0, adesso - m)
        return out

    def _valuta_ripetitori(self, stati: Dict[str, Optional[dict]], ora: float) -> None:
        """Ripetitore che tace da 12 ore e batteria al 20% o meno, dopo i soliti 10 minuti; e quando passa. Se un
        gateway non risponde non si sa chi sente: tutto resta com'era. Un ripetitore sparito dagli elenchi (gateway
        ripartito, radio tolta) esce senza dire "di nuovo attivo"."""
        if not stati or any(st is None for st in stati.values()):
            return
        noti = self.s["ripetitori"]
        visti = self.ripetitori_di(stati)
        for chiave in [c for c in noti if c not in visti]:
            del noti[chiave]
            self.s["coda"] = [m for m in self.s["coda"] if not (m.get("ponte") == "" and m.get("id") == chiave)]
        for chiave, r in visti.items():
            p = noti.setdefault(chiave, {"problemi": {}})
            problemi = p["problemi"]
            nome = r["nome"] or p.get("nome") or chiave
            p["nome"] = nome
            attuali = {}
            if r["tace"] >= self.RIPETITORE_SILENZIO_S:
                attuali["rip_silenzio"] = {"tace_dal": ora - r["tace"]}
            b = r["batteria"]
            recente = r["eta_batteria"] is not None and r["eta_batteria"] < self.RIPETITORE_METRICHE_S
            if b is not None and b <= 100 and recente and (
                    b <= self.RIPETITORE_BATTERIA or ("rip_batteria" in problemi and b <= self.RIPETITORE_BATTERIA_OK)):
                attuali["rip_batteria"] = {"batteria": b}
            elif "rip_batteria" in problemi and not recente:
                attuali["rip_batteria"] = {}             # non si sa: resta com'era
            for codice, dati in attuali.items():
                q = problemi.get(codice)
                if q is None:
                    q = problemi[codice] = {"dal": ora, "avvisato": False}
                if not q["avvisato"] and dati and ora - q["dal"] >= self.ATTESA_S:
                    q["avvisato"] = True
                    self._accoda("", codice, "nuovo", ora, dal=q["dal"], id=chiave, titolo=nome, gateway=r["gateway"],
                                 **dati)
            for codice in [c for c in problemi if c not in attuali]:
                q = problemi.pop(codice)
                if q.get("avvisato"):
                    prima = len(self.s["coda"])
                    self.s["coda"] = [m for m in self.s["coda"] if not (
                        m.get("ponte") == "" and m.get("id") == chiave and m.get("codice") == codice
                        and m.get("tipo") == "nuovo")]
                    if len(self.s["coda"]) == prima:
                        self._accoda("", codice, "risolto", ora, dal=q["dal"], id=chiave, titolo=nome,
                                     gateway=r["gateway"], batteria=b)

    def _accoda(self, nome: str, codice: str, tipo: str, ora: float, **dati) -> None:
        self.s["coda"].append(dict(dati, ponte=nome, codice=codice, tipo=tipo, quando=ora))
        del self.s["coda"][:-self.CODA_MAX]

    def _togli_dalla_coda(self, nome: str, codice: str, tipo: str) -> bool:
        prima = len(self.s["coda"])
        self.s["coda"] = [m for m in self.s["coda"]
                          if not (m.get("ponte") == nome and m.get("codice") == codice and m.get("tipo") == tipo)]
        return len(self.s["coda"]) < prima

    @staticmethod
    def _in_installazione(nome: str) -> bool:
        try:
            with open(P(f"{AVANZAMENTO_DIR}/{nome}.json"), encoding="utf-8") as f:
                return json.load(f).get("fase") != "fatto"
        except (OSError, ValueError, AttributeError):
            return False

    def _valuta(self, nome: str, st: Optional[dict], ora: float) -> None:
        p = self.s["ponti"].setdefault(nome, {})
        problemi = p.setdefault("problemi", {})
        if st is None and not (p.get("visto") or p.get("avvio")) and self._in_installazione(nome):
            return      # dalla 1.10.0: un ponte nuovo che si sta ancora installando non e' "irraggiungibile"
        if st is not None:
            p["visto"] = True
        sis = _d(_d(st).get("sistema"))
        avvio = sis.get("avvio") if isinstance(sis.get("avvio"), str) else None
        riavviato = bool(avvio and p.get("avvio") and avvio != p["avvio"])
        acceso = sis.get("acceso_dal") if isinstance(sis.get("acceso_dal"), (int, float)) else None
        if st is None:
            # non risponde: gli altri problemi restano come erano e non si avvisano (non si sa)
            attuali = {c: r.get("testo", "") for c, r in problemi.items()}
            attuali["irraggiungibile"] = ""
            controllabili = {"irraggiungibile"}
        else:
            attuali = problemi_di(st)
            controllabili = set(attuali)
        for codice, testo in attuali.items():
            r = problemi.get(codice)
            if r is None:
                r = problemi[codice] = {"dal": ora, "avvisato": False, "testo": testo}
            elif testo:
                r["testo"] = testo
            if codice in controllabili and not r["avvisato"] and ora - r["dal"] >= self.ATTESA_S:
                r["avvisato"] = True
                self._accoda(nome, codice, "nuovo", ora, dal=r["dal"], testo=r.get("testo", ""))
        riavvio_detto = False
        for codice in [c for c in problemi if c not in attuali]:
            r = problemi.pop(codice)
            if r.get("avvisato") and not self._togli_dalla_coda(nome, codice, "nuovo"):
                extra = {}
                if codice == "irraggiungibile" and riavviato and acceso:
                    extra["riavvio"] = acceso
                    riavvio_detto = True
                self._accoda(nome, codice, "risolto", ora, dal=r["dal"], **extra)
        if st is None:
            return
        if riavviato and not riavvio_detto:
            self._accoda(nome, "riavvio", "info", ora, acceso_dal=acceso)
        if avvio:
            p["avvio"] = avvio
        cert = _d(st.get("certificato"))
        scade = cert.get("scade")
        if isinstance(scade, (int, float)) and not isinstance(scade, bool):
            if p.get("cert_scade") != scade:          # primo stato o certificato rinnovato
                p["cert_scade"], p["cert_livello"] = scade, None
            rimasti = (scade - ora) / 86400
            livello = None
            for g in self.GIORNI_CERTIFICATO:
                if rimasti < g:
                    livello = g
            if livello is not None and (p.get("cert_livello") is None or livello < p["cert_livello"]):
                p["cert_livello"] = livello
                self._accoda(nome, "certificato", "info", ora, scade=scade,
                             nome_cert=re.sub(r"[^\w.@-]", "", str(cert.get("nome") or ""))[:64])
        al = _d(sis.get("alimentazione"))
        ultima = al.get("ultima")
        if (isinstance(ultima, (int, float)) and ora - ultima < self.CORRENTE_FINESTRA_S
                and ora - float(p.get("corrente_avvisata") or 0) >= self.CORRENTE_OGNI_S):
            p["corrente_avvisata"] = ora
            volte = al.get("volte") if isinstance(al.get("volte"), int) else None
            self._accoda(nome, "corrente", "info", ora, ultima=ultima, volte=volte,
                         dal=al.get("dal") if isinstance(al.get("dal"), (int, float)) else None)

    # ---- email ----

    def _consegna(self, cfg: dict) -> None:
        with self.lock:
            if not self.s["coda"]:
                self._salva()
                return
            ora = self.ora()
            ultimo = self.s.get("ultimo") or {}
            attesa = self.RIPROVA_NESSUNO_S if ultimo.get("esito") in ("nessuno", "pausa") else self.RIPROVA_S
            if self.s.get("tentativo") and ora - self.s["tentativo"] < attesa:
                self._salva()
                return
            inviati = list(self.s["coda"])
            oggetto, testo = self.componi(inviati, cfg, ora)
            self.s["tentativo"] = ora
            self._salva()
        esito, dettaglio = self.manda(oggetto, testo, cfg)
        with self.lock:
            self.s["ultimo"] = {"quando": round(ora), "esito": esito, "oggetto": oggetto, "dettaglio": dettaglio}
            if esito == "mandato":
                self.s["coda"] = [m for m in self.s["coda"] if m not in inviati]
                self.s["tentativo"] = 0
            self._salva()
        print(f"avvisi: {esito}: {oggetto}" + (f" ({dettaglio})" if dettaglio else ""), flush=True)

    def componi(self, coda: List[dict], cfg: dict, ora: float) -> Tuple[str, str]:
        righe: List[str] = []
        titoli = []
        for m in coda:
            titolo, corpo = self.messaggio(m, ora)
            titoli.append(titolo)
            righe += [titolo, "", corpo, "", ""]
        pagina = str(cfg.get("pagina") or "")
        if re.match(r"^https://[A-Za-z0-9.:-]+/$", pagina):
            righe.append(f"Pagina della rete: {pagina}")
        righe.append("Ti arriva perche' sei tra i destinatari degli avvisi di infra-TAK (Guard Dog).")
        if len(titoli) == 1:
            oggetto = titoli[0]
        else:
            oggetto = f"{len(titoli)} avvisi: {titoli[0]} e altri"
        return ("[Rete Meshtastic] " + oggetto)[:200], "\n".join(righe)

    def messaggio(self, m: dict, ora: float) -> Tuple[str, str]:
        n, codice, tipo = m.get("ponte", "?"), m.get("codice"), m.get("tipo")
        dal = m.get("dal") if isinstance(m.get("dal"), (int, float)) else m.get("quando", ora)
        q = m.get("quando", ora)
        per = durata_it(q - dal)
        if codice in ("rip_silenzio", "rip_batteria"):
            chi = re.sub(r"[^\w .,()'+!-]", "", str(m.get("titolo") or m.get("id") or "?"))[:40]
            if codice == "rip_silenzio":
                if tipo == "nuovo":
                    tace = m.get("tace_dal") if isinstance(m.get("tace_dal"), (int, float)) else dal
                    return (f"Ripetitore {chi}: non si sente piu'",
                            f"Nessun gateway sente il ripetitore {chi} dalle {quando_it(tace, ora)} (da "
                            f"{durata_it(q - tace)}). Puo' essere spento, con la batteria scarica o con l'antenna "
                            "fuori posto: le radio che passavano da lui non arrivano piu' alla rete.\n"
                            "Cosa fare: guarda la sua scheda nella pagina della rete (ultima batteria nota e "
                            "andamento); se non torna da solo con il sole, va controllato sul posto.")
                return (f"Ripetitore {chi}: si sente di nuovo",
                        f"Il ripetitore {chi} si sente di nuovo (il problema era stato segnalato alle "
                        f"{quando_it(dal, ora)}).")
            b = m.get("batteria") if isinstance(m.get("batteria"), int) else None
            if tipo == "nuovo":
                return (f"Ripetitore {chi}: batteria al {b}%" if b is not None else f"Ripetitore {chi}: batteria bassa",
                        f"La batteria del ripetitore {chi} e' al {b if b is not None else '?'}%. Sotto il 20% puo' "
                        "spegnersi in poche ore, soprattutto di notte o con il pannello coperto.\n"
                        "Cosa fare: guarda l'andamento nella scheda Storico della pagina della rete; se non risale "
                        "con il sole, controlla pannello e batteria sul posto.")
            return (f"Ripetitore {chi}: batteria risalita" + (f" al {b}%" if b is not None else ""),
                    f"La batteria del ripetitore {chi} e' risalita" + (f" al {b}%." if b is not None else "."))
        if codice == "irraggiungibile":
            if tipo == "nuovo":
                return (f"Gateway {n}: non risponde",
                        f"Il gateway {n} non risponde dalle {quando_it(dal, ora)} (da {per}). Il Raspberry puo' essere "
                        "spento o bloccato, senza corrente o senza internet.\n"
                        "Cosa fare: guarda la pagina della rete. Se resta cosi', vai al Raspberry e stacca e riattacca "
                        "la corrente.")
            extra = (f" Il Raspberry si e' riavviato alle {quando_it(m['riavvio'], ora)}."
                     if isinstance(m.get("riavvio"), (int, float)) else "")
            return (f"Gateway {n}: di nuovo in linea",
                    f"Il gateway {n} risponde di nuovo: non rispondeva dalle {quando_it(dal, ora)}, per {per}.{extra}")
        if codice == "radio":
            if tipo == "nuovo":
                return (f"Gateway {n}: radio non collegata",
                        f"Dalle {quando_it(dal, ora)} il gateway {n} non parla con la sua radio: le radio della squadra "
                        "sentite da li' non arrivano sulla mappa. Il gateway riprova da solo.\n"
                        "Cosa fare: se dura, riavvia il Raspberry (stacca e riattacca la corrente); con la radio USB "
                        "controlla il cavo.")
            return (f"Gateway {n}: radio di nuovo collegata",
                    f"Il gateway {n} parla di nuovo con la sua radio (era staccata dalle {quando_it(dal, ora)}, per {per}).")
        if codice == "tak":
            if tipo == "nuovo":
                errore = f" (ultimo errore: {m['testo']})" if m.get("testo") else ""
                return (f"Gateway {n}: non arriva al TAK Server",
                        f"Dalle {quando_it(dal, ora)} il gateway {n} non e' collegato al TAK Server{errore}. Il gateway "
                        "riprova da solo.\n"
                        "Cosa fare: se il certificato del gateway e' scaduto o e' stato revocato serve un certificato "
                        "nuovo; altrimenti di solito e' internet di casa o il server.")
            return (f"Gateway {n}: di nuovo collegato al TAK Server",
                    f"Il gateway {n} e' di nuovo collegato al TAK Server (non lo era dalle {quando_it(dal, ora)}, per {per}).")
        if codice == "spazio":
            if tipo == "nuovo":
                return (f"Gateway {n}: microSD quasi piena",
                        f"Sul Raspberry del gateway {n} restano {m.get('testo') or 'pochi'} MB liberi: con la microSD "
                        "piena il gateway si ferma.\n"
                        "Cosa fare: sul Raspberry  sudo apt clean  e  sudo journalctl --vacuum-size=100M; se non "
                        "basta, guarda la guida.")
            return (f"Gateway {n}: spazio di nuovo sufficiente", f"Sulla microSD del gateway {n} c'e' di nuovo spazio.")
        if codice == "riavvio":
            alle = (f" alle {quando_it(m['acceso_dal'], ora)}" if isinstance(m.get("acceso_dal"), (int, float))
                    else "")
            return (f"Gateway {n}: il Raspberry si e' riavviato",
                    f"Il Raspberry del gateway {n} si e' riavviato{alle}. Se non e' stato un aggiornamento o qualcuno di "
                    "voi, si era bloccato ed e' ripartito da solo: se succede spesso, controlla alimentatore e "
                    "microSD.")
        if codice == "certificato":
            scade = m.get("scade") or 0
            chi = f" {m['nome_cert']}" if m.get("nome_cert") else ""
            rinnovo = ("Cosa fare: nella console infra-TAK crea un certificato nuovo per il gateway (TAK Server, "
                       "Create Client Certificate, lettura e scrittura BOTH sul canale della squadra, Integration "
                       "package), copialo sul Raspberry e lancia:  sudo bash installa-ponte-meshtastic.sh "
                       "NOMEFILE.zip")
            if scade <= q:
                return (f"Gateway {n}: il certificato TAK e' scaduto",
                        f"Il certificato{chi} del gateway {n} e' scaduto il {giorno_it(scade)}: il TAK Server non lo "
                        f"accetta piu'.\n{rinnovo}")
            giorni = max(1, int((scade - q) // 86400))
            return (f"Gateway {n}: il certificato TAK scade il {giorno_it(scade)}",
                    f"Il certificato{chi} del gateway {n} scade il {giorno_it(scade)}, tra {giorni} "
                    f"giorn{'o' if giorni == 1 else 'i'}. Dopo, il gateway non entra piu' nel TAK Server.\n{rinnovo}")
        if codice == "corrente":
            volte = f"; {m['volte']} da quando il gateway e' acceso" if isinstance(m.get("volte"), int) else ""
            ultima = m.get("ultima") if isinstance(m.get("ultima"), (int, float)) else q
            return (f"Gateway {n}: corrente insufficiente",
                    f"Il Raspberry del gateway {n} ha avuto cali di corrente (l'ultimo alle {quando_it(ultima, ora)}"
                    f"{volte}). Se la corrente non basta il Raspberry si puo' bloccare o rovinare la microSD.\n"
                    "Cosa fare: usa l'alimentatore ufficiale da 5,1 V e 2,5 A, con un cavo corto.")
        return (f"Gateway {n}: avviso", "")

    @staticmethod
    def invia(porta: int, oggetto: str, testo: str) -> Tuple[str, str]:
        """Email con la posta di infra-TAK (Guard Dog): la console sceglie i destinatari e rispetta le sue pause.
        Esito: mandato, nessuno (nessun destinatario), pausa (avvisi sospesi), trattenuto (manutenzione), errore."""
        corpo = json.dumps({"subject": oggetto[:200], "body": testo[:50000], "to": ""}).encode("utf-8")
        for schema in ("https", "http"):
            gestori = [urllib.request.ProxyHandler({})]
            if schema == "https":
                gestori.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
            apri = urllib.request.build_opener(*gestori)
            richiesta = urllib.request.Request(f"{schema}://127.0.0.1:{porta}/api/guarddog/send-alert-email",
                                               data=corpo, headers={"Content-Type": "application/json"},
                                               method="POST")
            try:
                with apri.open(richiesta, timeout=20) as r:
                    risposta = json.loads(r.read(65536) or b"{}")
            except urllib.error.HTTPError as e:      # ha risposto: niente secondo tentativo (email doppia)
                return "errore", f"infra-TAK ha risposto con l'errore {e.code}"
            except (urllib.error.URLError, OSError, ValueError):
                if schema == "https":
                    continue                          # come gli avvisi di Guard Dog: HTTPS, poi HTTP
                return "errore", f"la console di infra-TAK non risponde (porta {porta})"
            if not isinstance(risposta, dict):
                return "errore", "risposta di infra-TAK non valida"
            if risposta.get("held"):
                return "trattenuto", "infra-TAK e' in una finestra di manutenzione: riprovo dopo"
            if risposta.get("suppressed"):
                if "recipient" in str(risposta.get("reason") or ""):
                    return "nessuno", "in infra-TAK non c'e' un indirizzo per gli avvisi (Guard Dog)"
                return "pausa", "gli avvisi di infra-TAK sono sospesi"
            if risposta.get("success"):
                return "mandato", ""
            return "errore", str(risposta.get("error") or "risposta di infra-TAK non valida")[:200]
        return "errore", "la console di infra-TAK non risponde"

    @staticmethod
    def chiedi_stato(porta: int) -> Optional[dict]:
        """Lo stato del ponte dal suo tunnel (solo su 127.0.0.1; il certificato della pagina e' quello del ponte)."""
        apri = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                           urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
        try:
            with apri.open(f"https://127.0.0.1:{porta}/mesh2tak/stato.json", timeout=10) as r:
                d = json.loads(r.read(4 << 20))
        except Exception:  # noqa
            return None
        return d if isinstance(d, dict) and d.get("formato") == 1 else None

    @staticmethod
    def manda_icone(porta: int, corpo: bytes) -> Optional[dict]:
        """Le icone scelte al ponte, dal suo tunnel (1.11.0): il ponte le accetta solo da li'."""
        apri = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                           urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
        richiesta = urllib.request.Request(f"https://127.0.0.1:{porta}/mesh2tak/icone", data=corpo, method="POST",
                                           headers={"Content-Type": "application/json", "X-Mesh2tak": "1"})
        try:
            with apri.open(richiesta, timeout=15) as r:
                d = json.loads(r.read(65536))
        except Exception:  # noqa
            return None
        return d if isinstance(d, dict) else None

    # ---- per la pagina ----

    def vista(self) -> dict:
        with self.lock:
            problemi = {n: sorted([{"codice": c, "dal": round(r.get("dal", 0)), "avvisato": bool(r.get("avvisato"))}
                                   for c, r in p.get("problemi", {}).items()], key=lambda x: x["codice"])
                        for n, p in self.s["ponti"].items() if p.get("problemi")}
            return {"ultimo": self.s.get("ultimo"), "in_coda": len(self.s["coda"]), "problemi": problemi,
                    "attesa_min": round(self.ATTESA_S / 60)}

    def prova(self, utente: str) -> Tuple[int, dict]:
        ora = self.ora()
        if ora - self._ultima_prova < self.PROVA_OGNI_S:
            return 429, {"errore": "ne ho appena mandata una: aspetta mezzo minuto"}
        self._ultima_prova = ora
        cfg = self.config()
        q = _ore(ora)
        esito, dettaglio = self.manda("[Rete Meshtastic] Email di prova",
                                      f"Prova degli avvisi della rete Meshtastic, chiesta da {utente} il {q.day}/{q.month} "
                                      f"alle {q.strftime('%H:%M')}.\nSe la leggi, gli avvisi ti arrivano.", cfg)
        print(f"avvisi: prova chiesta da {utente}: {esito}" + (f" ({dettaglio})" if dettaglio else ""), flush=True)
        parole = {"mandato": "Email di prova consegnata a infra-TAK: controlla la casella degli avvisi (anche lo "
                             "spam) tra un paio di minuti.",
                  "nessuno": "Non partita: in infra-TAK non c'e' un indirizzo per gli avvisi. Impostalo nella "
                             "console infra-TAK, in Guard Dog, e riprova.",
                  "pausa": "Non partita: gli avvisi di infra-TAK sono sospesi (Guard Dog).",
                  "trattenuto": "Trattenuta: infra-TAK e' in una finestra di manutenzione. Riprova piu' tardi."}
        messaggio = parole.get(esito) or f"Non partita: {dettaglio}."
        return 200, {"esito": esito, "messaggio": messaggio}


