# --------------------------------------------------------------------------
# Pagina della rete e registrazioni per i Raspberry
# --------------------------------------------------------------------------

def risorsa(nome: str, incorporata: str) -> bytes:
    if not incorporata.startswith("@" "@"):   # (scritto cosi' perche' build.py non lo scambi per un segnaposto)
        return base64.b64decode(incorporata)
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pagina", nome), "rb") as f:
        return f.read()   # prove: file accanto allo script


def scrivi_sito(imp: Impostazioni, reg: dict) -> None:
    os.makedirs(P(SITO_DIR), exist_ok=True)
    os.makedirs(P(ACCESSO_DIR), exist_ok=True)
    for d in (P(WEB_DIR), P(SITO_DIR), P(ACCESSO_DIR)):
        os.chmod(d, 0o755)
    scrivi(P(SITO_DIR + "/index.html"), risorsa("index.html", PAGINA_B64).decode("utf-8"))
    scrivi(P(SITO_DIR + "/icona.svg"), risorsa("icona.svg", ICONA_B64).decode("utf-8"))
    ponti = [{"nome": n, "indirizzo": imp.url(n), "stato": f"stato/{n}.json",
              "aggiunto": p.get("aggiunto", "")} for n, p in sorted(reg["ponti"].items())]
    scrivi(P(SITO_DIR + "/ponti.json"), json.dumps({"formato": 1, "dominio": imp.dominio, "versione": VERSIONE,
                                                    "ponti": ponti}, ensure_ascii=False, indent=1) + "\n")
    # per gli avvisi del servizio (senza privilegi): dove chiedere lo stato dei ponti e dove mandare le email
    scrivi(P(SORVEGLIANZA), json.dumps({"formato": 1, "pagina": imp.url(), "console": porta_console(imp),
                                        "ponti": [{"nome": n, "porta": p["porta"]}
                                                  for n, p in sorted(reg["ponti"].items())]},
                                       ensure_ascii=False, indent=1) + "\n")
    scrivi_riassunto_nuovi(leggi_impostazioni_nuovi())
    scrivi_icone_mappa(imp)


def figure_infratak(imp: Impostazioni) -> Dict[str, bytes]:
    """Le figure delle icone dell'elenco, dal catalogo che infra-TAK usa per i suoi flussi (nodered/
    icon-catalog.json, le stesse icone che ATAK ha di serie). Il codice di mesh2tak non le contiene: la pagina
    le mostra solo se infra-TAK c'e' e le ha."""
    catalogo = os.path.join(os.path.dirname(os.path.dirname(imp.percorso)), "nodered", "icon-catalog.json")
    try:
        if os.path.getsize(catalogo) > 64 << 20:
            return {}
        with open(catalogo, encoding="utf-8") as f:
            dati = json.load(f)
    except (OSError, ValueError):
        return {}
    insiemi = {}
    for insieme in (dati.values() if isinstance(dati, dict) else []):
        if isinstance(insieme, dict) and isinstance(insieme.get("groups"), dict):
            insiemi[str(insieme.get("uid"))] = insieme["groups"]
    figure = {}
    for chiave, voce in ICONE_PER_CHIAVE.items():
        uid, gruppo, nome_file = voce["percorso"].split("/", 2)
        for x in (insiemi.get(uid) or {}).get(gruppo) or []:
            if not isinstance(x, dict) or x.get("f") != nome_file or not isinstance(x.get("b"), str):
                continue
            try:
                png = base64.b64decode(x["b"], validate=True)
            except (ValueError, binascii.Error):
                break
            if png.startswith(b"\x89PNG\r\n\x1a\n") and len(png) <= 32768:
                figure[chiave] = png
            break
    return figure


def scrivi_icone_mappa(imp: Impostazioni) -> None:
    """Elenco delle icone per la pagina (icone-mappa.json) e le loro figure (icone/CHIAVE.png)."""
    figure = figure_infratak(imp)
    os.makedirs(P(ICONE_SITO), exist_ok=True)
    os.chmod(P(ICONE_SITO), 0o755)
    for f in glob.glob(P(ICONE_SITO) + "/*.png"):
        if os.path.basename(f)[:-4] not in figure:
            togli(f)
    for chiave, png in figure.items():
        percorso = P(f"{ICONE_SITO}/{chiave}.png")
        if leggi_bytes(percorso) != png:
            with open(os.open(percorso + ".nuovo", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644), "wb") as f:
                f.write(png)
            os.chmod(percorso + ".nuovo", 0o644)
            os.replace(percorso + ".nuovo", percorso)
    elenco = [{"chiave": v["chiave"], "nome": v["nome"], "gruppo": v["gruppo"], "tipo": v["tipo"],
               "figura": f"icone/{v['chiave']}.png" if v["chiave"] in figure else ""}
              for v in ICONE_PER_CHIAVE.values()]
    scrivi(P(ICONE_ELENCO), json.dumps({"formato": 1, "icone": elenco}, ensure_ascii=False, indent=1) + "\n")


def leggi_bytes(percorso: str) -> Optional[bytes]:
    try:
        with open(percorso, "rb") as f:
            return f.read()
    except OSError:
        return None


def porta_console(imp: Impostazioni) -> int:
    try:
        porta = int(imp.dati.get("console_port") or 5001)
    except (TypeError, ValueError):
        porta = 5001
    return porta if 0 < porta < 65536 else 5001


def scrivi_registrazione(imp: Impostazioni, nome: str, p: dict, porta_sshd: int, chiavi: List[str]) -> None:
    # anche la chiave del Raspberry: il nome del file e' la sua impronta, quindi la conosce gia' chi lo trova
    dati = {"formato": 1, "nome": nome, "utente": utente_ponte(nome), "porta": p["porta"],
            "ssh_host": imp.host(), "ssh_porta": porta_sshd, "chiavi_host": chiavi, "indirizzo": imp.url(nome),
            "chiave": p["chiave"]}
    agg = p.get("aggiorna")
    if isinstance(agg, dict) and RE_VERSIONE_RILASCIO.match(str(agg.get("versione", ""))):
        # dalla 1.8.0: il Raspberry vede la richiesta e scarica quella versione da GitHub (mai da qui)
        dati["aggiorna"] = {"versione": agg["versione"], "chiesto": int(agg.get("chiesto") or 0)}
        if agg.get("reimposta") is True:
            dati["aggiorna"]["reimposta"] = True      # dalla 1.13.0: anche la radio come all'installazione
    azioni = azioni_da_pubblicare(p)
    if azioni:
        # dalla 1.13.0: comandi per il Raspberry (senza chi li ha chiesti: la registrazione e' pubblica)
        dati["azioni"] = azioni
    ins = p.get("installa")
    if isinstance(ins, dict) and os.path.exists(file_configurazione(nome)):
        # dalla 1.10.0: c'e' una configurazione da installare; il Raspberry la scarica via SSH, con la sua chiave
        dati["installa"] = {"tentativo": int(ins.get("tentativo") or 1)}
    scrivi(P(f"{ACCESSO_DIR}/{p['impronta']}.json"), json.dumps(dati, indent=1) + "\n")


def azioni_da_pubblicare(p: dict) -> List[dict]:
    ora = time.time()
    risultato = []
    for a in p.get("azioni") or []:
        if (isinstance(a, dict) and RE_ID.match(str(a.get("id", ""))) and a.get("azione") in AZIONI_PONTE + ("certificato",)
                and isinstance(a.get("chiesto"), int) and ora - a["chiesto"] < AZIONI_DURATA_S):
            v = {"id": a["id"], "azione": a["azione"], "chiesto": a["chiesto"]}
            if isinstance(a.get("valori"), dict) and a["valori"]:
                v["valori"] = a["valori"]
            risultato.append(v)
    return risultato[-AZIONI_MAX:]


def sveglia_ponte(porta: int) -> bool:
    """Dal tunnel: il Raspberry legge subito la sua registrazione, invece che entro 2 minuti (dalla 1.13.0). Non porta
    comandi: quelli sono nella registrazione."""
    apri = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                       urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
    richiesta = urllib.request.Request(f"https://127.0.0.1:{porta}/mesh2tak/controlla", data=b"{}", method="POST",
                                       headers={"Content-Type": "application/json", "X-Mesh2tak": "1",
                                                "User-Agent": f"mesh2tak-rete/{VERSIONE}"})
    try:
        with apri.open(richiesta, timeout=8) as r:
            return r.status == 200
    except Exception:  # noqa: ponte spento o di prima della 1.13.0
        return False


def chiedi_al_ponte(imp: Impostazioni, reg: dict, lavoro: dict) -> str:
    """Lavori "azione-ponte" e (dopo il certificato) "certificato-ponte": il comando nella registrazione del ponte,
    con lo stesso numero del lavoro (la pagina segue com'e' andata nello stato del ponte)."""
    nome = lavoro["nome"]
    if nome not in reg["ponti"]:
        raise Errore("gateway non trovato: ricarica la pagina")
    p = reg["ponti"][nome]
    ora = int(time.time())
    azione = {"id": lavoro["id"], "azione": lavoro["cosa"], "chiesto": ora, "da": lavoro["da"]}
    if lavoro.get("valori"):
        azione["valori"] = lavoro["valori"]
    tenute = [a for a in p.get("azioni") or [] if isinstance(a, dict) and isinstance(a.get("chiesto"), int)
              and ora - a["chiesto"] < AZIONI_DURATA_S]
    p["azioni"] = (tenute + [azione])[-AZIONI_MAX:]
    salva_registro(reg)
    pubblica_registrazioni(imp, reg, [nome])
    if sveglia_ponte(p["porta"]):
        ok(f"il gateway {nome} lo fa subito")
        return "chiesto al gateway: lo fa subito"
    info(f"il gateway {nome} non risponde adesso: lo fa appena torna raggiungibile (entro mezz'ora, poi scade)")
    return "chiesto al gateway: non risponde adesso, lo fa appena torna raggiungibile (entro mezz'ora)"


def file_certificato_ponte(nome: str) -> str:
    return P(f"{CONFIGURAZIONI_DIR}/{nome}.certificato.json")


def prepara_certificato_ponte(imp: Impostazioni, reg: dict, lavoro: dict) -> str:
    """Lavoro "certificato-ponte" (da root): controlla il pacchetto e lo lascia da scaricare al solo utente SSH di quel
    ponte, poi chiede al Raspberry di installarlo (comando "certificato")."""
    nome, d = lavoro["nome"], lavoro["impostazioni"]
    if nome not in reg["ponti"]:
        raise Errore("gateway non trovato: ricarica la pagina")
    dati = base64.b64decode(d["pacchetto"])
    password = d["password"] or "atakatak"
    info(f"pacchetto {d['nome_pacchetto']} ({len(dati) // 1024 + 1} kB) per il gateway {nome}")
    c = controlla_pacchetto(dati, d["nome_pacchetto"], password)
    if c["certificato"]:
        ok(f"certificato '{c['certificato']}'" + (f", scade il {giorno_it(c['scade'])}" if c["scade"] else ""))
    prepara_cartelle_ponti()
    f = file_certificato_ponte(nome)
    togli(f)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    try:
        os.write(fd, (json.dumps({"formato": 1, "pacchetto": d["pacchetto"], "nome_pacchetto": d["nome_pacchetto"],
                                  "password": password, "host": c["host"] or d["host"]}) + "\n").encode("utf-8"))
        try:
            os.fchown(fd, pwd.getpwnam(utente_ponte(nome)).pw_uid, 0)
        except KeyError:
            if not RADICE:
                raise Errore(f"manca l'utente {utente_ponte(nome)}")
        os.fchmod(fd, 0o400)
    finally:
        os.close(fd)
    esito = chiedi_al_ponte(imp, reg, dict(lavoro, cosa="certificato", valori=None))
    return (f"Certificato '{c['certificato'] or '?'}' pronto per il gateway {nome}: " + esito.replace("chiesto al gateway: ", "")
            + ".")


