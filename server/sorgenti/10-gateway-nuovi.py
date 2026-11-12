# --------------------------------------------------------------------------
# Ponti nuovi (dalla 1.10.0): il Raspberry scritto con Raspberry Pi Imager si installa da solo.
# Certificato e canale si caricano una volta sulla pagina e restano qui, letti solo da root; quando un
# ponte nuovo e' approvato, root ne scrive la configurazione in un file che puo' leggere solo l'utente SSH di
# quel ponte, e il Raspberry la scarica con la sua chiave (comando "ponte").
# --------------------------------------------------------------------------

RE_POSIZIONE = re.compile(r"^-?\d{1,3}(?:\.\d+)?,-?\d{1,3}(?:\.\d+)?(?:,-?\d{1,5}(?:\.\d+)?)?$")
RE_NOME_PACCHETTO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,78}\.(?:zip|p12|pfx)$")
RE_NOME_CANALE = re.compile(r"^[A-Za-z0-9_-]{1,11}$")
RE_HOST_TAK = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
PACCHETTO_MAX = 256 << 10


def valida_posizione(testo: str) -> str:
    """'lat,lon[,quota]' come le copia Google Maps (anche con spazi); '' se non c'e'. Come l'installatore."""
    parti = [x for x in str(testo or "").replace(";", ",").replace(" ", ",").split(",") if x.strip()]
    if not parti:
        return ""
    pulita = ",".join(parti)
    try:
        lat, lon = float(parti[0]), float(parti[1])
    except (ValueError, IndexError):
        raise Errore("coordinate: latitudine e longitudine separate da una virgola, es. 45.8205, 8.8257")
    if (not RE_POSIZIONE.match(pulita) or not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0)):
        raise Errore("coordinate: latitudine e longitudine separate da una virgola, es. 45.8205, 8.8257")
    return pulita


def chiave_canale_valida(chiave: str) -> str:
    """La chiave del canale (PSK) in base64, 16 o 32 byte, come la vuole l'installatore."""
    t = str(chiave or "").strip()
    t = t[7:] if t.lower().startswith("base64:") else t
    try:
        if len(base64.b64decode(t, validate=True)) in (16, 32):
            return t
    except (binascii.Error, ValueError):
        pass
    raise Errore("chiave del canale non valida: serve quella del canale privato, in base64 (16 o 32 byte, "
                 "es. 44 caratteri con = in fondo)")


def openssl(argomenti: List[str], password: str) -> subprocess.CompletedProcess:
    return subprocess.run(["openssl"] + argomenti, capture_output=True, text=True, timeout=60,
                          stdin=subprocess.DEVNULL, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                                         "M2T_PASS": password})


def controlla_pacchetto(dati: bytes, nome_file: str, password: str) -> dict:
    """Il pacchetto d'integrazione della console infra-TAK (o un .p12): si apre, ha certificato e chiave, la
    password li apre. Torna nome e scadenza del certificato, indirizzo e porta del TAK Server se li dice."""
    cartella = P(PONTI_NUOVI_DIR + "/controllo")
    shutil.rmtree(cartella, ignore_errors=True)
    os.makedirs(cartella, mode=0o700)
    try:
        host, porta = "", 8089
        file_dentro: Dict[str, str] = {}
        if nome_file.lower().endswith(".zip"):
            import zipfile
            try:
                z = zipfile.ZipFile(io.BytesIO(dati))
                nomi = [n for n in z.namelist() if not n.endswith("/")]
                if len(nomi) > 40 or sum(i.file_size for i in z.infolist()) > 4 << 20:
                    raise Errore("il pacchetto contiene troppa roba: e' quello della console infra-TAK?")
                for n in nomi:
                    b = os.path.basename(n)
                    if b and re.match(r"^[A-Za-z0-9._ -]{1,100}$", b) and not b.startswith("._"):
                        destinazione = os.path.join(cartella, b)
                        with open(destinazione, "wb") as f:
                            f.write(z.read(n))
                        file_dentro[b] = destinazione
            except (zipfile.BadZipFile, OSError, RuntimeError, ValueError):
                raise Errore("il file .zip non si apre: e' quello scaricato dalla console infra-TAK?")
        else:
            destinazione = os.path.join(cartella, "certificato.p12")
            with open(destinazione, "wb") as f:
                f.write(dati)
            file_dentro["certificato.p12"] = destinazione
        leggimi = file_dentro.get("README.txt")
        if leggimi:
            testo = open(leggimi, encoding="utf-8", errors="replace").read()
            m = re.search(r"^Server:\s*(\S+)", testo, re.M)
            if m and not m.group(1).startswith("<") and RE_HOST_TAK.match(m.group(1)):
                host = m.group(1)
            m = re.search(r"^Port:\s*(\d+)", testo, re.M)
            if m and 0 < int(m.group(1)) < 65536:
                porta = int(m.group(1))
        cert = next((f for b, f in sorted(file_dentro.items()) if b.endswith(".pem") and b not in
                     ("ca-chain.pem", "ca.pem")), None)
        chiave = next((f for b, f in sorted(file_dentro.items()) if b.endswith(".key")), None)
        p12 = next((f for b, f in sorted(file_dentro.items()) if b.endswith((".p12", ".pfx")) and
                    not b.endswith("-legacy.p12") and not b.startswith("truststore-")), None)
        if not ((cert and chiave) or p12):
            raise Errore("nel pacchetto non c'e' ne' il certificato .pem/.key ne' il .p12: e' quello della console "
                         "infra-TAK?")
        risultato = {"host": host, "porta": porta, "certificato": "", "scade": None}
        if not shutil.which("openssl"):
            return risultato                # senza openssl si controlla sul Raspberry, come sempre
        if cert and chiave:
            if openssl(["pkey", "-in", chiave, "-passin", "env:M2T_PASS", "-noout"], password).returncode != 0:
                raise Errore("password del certificato sbagliata (quella mostrata nella console infra-TAK)")
        else:
            esportato = os.path.join(cartella, "cliente.pem")
            for extra in ([], ["-legacy"]):
                r = openssl(["pkcs12"] + extra + ["-in", p12, "-passin", "env:M2T_PASS", "-clcerts", "-nokeys",
                                                  "-out", esportato], password)
                if r.returncode == 0:
                    break
            else:
                raise Errore("password del certificato sbagliata, o file .p12 rovinato")
            cert = esportato
        r = openssl(["x509", "-in", cert, "-noout", "-subject", "-enddate", "-nameopt", "multiline"], password)
        m = re.search(r"commonName\s*=\s*(.+)", r.stdout or "")
        if m:
            risultato["certificato"] = re.sub(r"[^\w.@ -]", "", m.group(1).strip())[:64]
        m = re.search(r"notAfter=(.+)", r.stdout or "")
        if m:
            try:
                risultato["scade"] = int(ssl.cert_time_to_seconds(m.group(1).strip()))
            except ValueError:
                pass
        return risultato
    finally:
        shutil.rmtree(cartella, ignore_errors=True)


def salva_impostazioni_nuovi(lavoro: dict) -> str:
    """Lavoro "impostazioni-nuovi" (da root): controlla e salva certificato e canale per i ponti nuovi."""
    d = lavoro["impostazioni"]
    dati = base64.b64decode(d["pacchetto"])
    password = d["password"] or "atakatak"
    info(f"pacchetto {d['nome_pacchetto']} ({len(dati) // 1024 + 1} kB)")
    c = controlla_pacchetto(dati, d["nome_pacchetto"], password)
    host = c["host"] or d["host"]          # come l'installatore: vale quello scritto nel pacchetto
    if not host:
        raise Errore("il pacchetto non dice l'indirizzo del TAK Server: scrivilo nel modulo (es. takserver.tuodominio.it)")
    if c["certificato"]:
        ok(f"certificato '{c['certificato']}'" + (f", scade il {giorno_it(c['scade'])}" if c["scade"] else ""))
    else:
        avviso("certificato non controllato qui (manca openssl): lo controlla il Raspberry")
    salvate = {"formato": 1, "pacchetto": d["pacchetto"], "nome_pacchetto": d["nome_pacchetto"], "password": password,
               "host": d["host"], "server": f"{host}:{c['porta']}",
               "canale": {"nome": d["canale"], "chiave": d["chiave"], "secondario": ""},
               "certificato": c["certificato"], "scade": c["scade"], "aggiornato": int(time.time()), "da": lavoro["da"]}
    os.makedirs(P(PONTI_NUOVI_DIR), exist_ok=True)
    os.chmod(P(PONTI_NUOVI_DIR), 0o700)
    scrivi(P(IMPOSTAZIONI_NUOVI), json.dumps(salvate, ensure_ascii=False) + "\n", 0o600)
    scrivi_riassunto_nuovi(salvate)
    ok(f"TAK Server {host}:{c['porta']}, canale '{d['canale']}' (l'unico dei gateway nuovi)")
    return (f"Impostazioni salvate: certificato '{c['certificato'] or '?'}', TAK Server {host}:{c['porta']}, "
            f"canale {d['canale']}.")


def scrivi_riassunto_nuovi(s: Optional[dict]) -> None:
    """Per la pagina della rete: cosa c'e' nelle impostazioni dei ponti nuovi, senza chiavi ne' password."""
    if not s:
        togli(P(RIASSUNTO_NUOVI))
        return
    canale = s.get("canale") or {}
    scrivi(P(RIASSUNTO_NUOVI), json.dumps({
        "formato": 1, "pronto": True, "certificato": s.get("certificato") or "", "scade": s.get("scade"),
        "server": s.get("server") or "", "canale": canale.get("nome") or "",
        "aggiornato": s.get("aggiornato"), "da": s.get("da") or ""}, ensure_ascii=False) + "\n", 0o644)


def leggi_impostazioni_nuovi() -> Optional[dict]:
    testo = leggi(P(IMPOSTAZIONI_NUOVI))
    try:
        d = json.loads(testo) if testo else None
    except ValueError:
        return None
    return d if isinstance(d, dict) and d.get("pacchetto") else None


def file_configurazione(nome: str) -> str:
    return P(f"{CONFIGURAZIONI_DIR}/{nome}.json")


def prepara_configurazione(imp: Impostazioni, reg: dict, nome: str, installa: dict, da: str) -> int:
    """Scrive la configurazione da installare del ponte nuovo: la legge solo il suo utente SSH (e root). Torna il
    numero del tentativo, che va nella registrazione: il Raspberry lo vede e la scarica."""
    s = leggi_impostazioni_nuovi()
    if not s:
        raise Errore("mancano certificato e canale per i gateway nuovi: caricali sulla pagina della rete")
    p = reg["ponti"][nome]
    prima = p.get("installa") if isinstance(p.get("installa"), dict) else {}
    tentativo = int(prima.get("tentativo") or 0) + 1
    conf = {"formato": 1, "nome": nome, "scheda": installa["scheda"], "posizione": installa.get("posizione") or "",
            "rete": imp.url().rstrip("/"), "versione": VERSIONE, "tentativo": tentativo,
            "tak": {"pacchetto": s["pacchetto"], "nome_pacchetto": s["nome_pacchetto"], "password": s["password"],
                    "host": s.get("host") or ""},
            "canale": dict(s["canale"], secondario="")}       # dalla 1.12.0 nessun canale pubblico, anche se salvato prima
    prepara_cartelle_ponti()
    f = file_configurazione(nome)
    togli(f)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    try:
        os.write(fd, (json.dumps(conf, ensure_ascii=False) + "\n").encode("utf-8"))
        try:
            os.fchown(fd, pwd.getpwnam(utente_ponte(nome)).pw_uid, 0)
        except KeyError:
            if not RADICE:
                raise Errore(f"manca l'utente {utente_ponte(nome)}")
        os.fchmod(fd, 0o400)
    finally:
        os.close(fd)
    p["installa"] = {"tentativo": tentativo, "chiesto": int(time.time()), "scheda": installa["scheda"],
                     "posizione": installa.get("posizione") or "", "da": da}
    ok(f"configurazione per il gateway nuovo pronta (tentativo {tentativo}, radio {installa['scheda']}): la legge "
       f"solo il Raspberry di {nome}")
    return tentativo


def riprova_installazione(imp: Impostazioni, reg: dict, lavoro: dict) -> None:
    """Lavoro "installa" (da root): la configurazione di nuovo, con le scelte nuove o quelle di prima."""
    nome = lavoro["nome"]
    if nome not in reg["ponti"]:
        raise Errore("gateway non trovato: ricarica la pagina")
    prima = reg["ponti"][nome].get("installa")
    if not isinstance(prima, dict):
        raise Errore(f"il gateway {nome} non e' stato installato dalla pagina: da qui non si reinstalla")
    installa = {"scheda": lavoro["installa"].get("scheda") or prima.get("scheda") or "usb",
                "posizione": (lavoro["installa"]["posizione"] if "posizione" in lavoro["installa"]
                              else prima.get("posizione") or "")}
    prepara_configurazione(imp, reg, nome, installa, lavoro["da"])
    salva_registro(reg)
    pubblica_registrazioni(imp, reg, [nome])
    ok(f"il Raspberry di {nome} riprova l'installazione entro un paio di minuti")


def aggiungi_ponte(imp: Impostazioni, reg: dict, nome: str, chiave: str, impronta: str, ignora_dns: bool = False,
                   cambio_chiave: bool = True, installa: Optional[dict] = None, da: str = "") -> None:
    """Registra (o aggiorna) un ponte: DNS, utente SSH, pagina, Caddy, Authentik, registrazione. Con installa
    (ponte nuovo dalla 1.10.0) prepara anche la configurazione che il Raspberry scarica per installarsi."""
    if installa is not None and not leggi_impostazioni_nuovi():
        raise Errore("mancano certificato e canale per i gateway nuovi: caricali sulla pagina della rete")
    reg["dominio"] = imp.dominio
    nuovo = nome not in reg["ponti"]
    passo(f"1/5  Indirizzi: {imp.url()} e {imp.url(nome)}")
    controlla_dns(imp, [imp.host(), imp.host(nome)], ignora_dns)
    for altro, p in reg["ponti"].items():
        if altro != nome and p.get("impronta") == impronta:
            raise Errore(f"questa chiave e' gia' del gateway '{altro}': ogni Raspberry ha la sua")
    ora = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    if nuovo:
        usate = {p["porta"] for p in reg["ponti"].values()}
        occupate = porte_in_ascolto()
        porta = PORTA_BASE
        while porta in usate or porta in occupate:
            porta += 1
        reg["ponti"][nome] = {"porta": porta, "chiave": chiave, "impronta": impronta, "aggiunto": ora}
    else:
        p = reg["ponti"][nome]
        if p["impronta"] != impronta:
            if not cambio_chiave:
                raise Errore(f"il gateway '{nome}' c'e' gia' con un'altra chiave (un altro Raspberry, o lo stesso "
                             "reinstallato): sulla pagina della rete usa 'Sostituisci'")
            p.setdefault("impronte_vecchie", []).append(p["impronta"])
            info(f"il gateway '{nome}' cambia chiave (Raspberry reinstallato?): quella vecchia non vale piu'")
        p.update({"chiave": chiave, "impronta": impronta, "aggiornato": ora})
    passo("2/5  Utente del gateway sul server (SSH, solo tunnel)")
    prepara_ponti(imp, reg, [nome])
    passo("3/5  Caddy")
    configura_caddy(imp, reg)
    passo("4/5  Authentik (solo amministratori)")
    configura_authentik(imp, reg)
    # solo adesso, a tutto pronto: se un passo prima non riesce il ponte non risulta aggiunto e si riprova
    passo("5/5  Pagina della rete e registrazione per il Raspberry")
    if installa is not None:
        prepara_configurazione(imp, reg, nome, installa, da)
    salva_registro(reg)
    scrivi_sito(imp, reg)
    pubblica_registrazioni(imp, reg, [nome])


def rimuovi_ponte(imp: Impostazioni, reg: dict, nome: str) -> None:
    if nome not in reg["ponti"]:
        raise Errore(f"il gateway '{nome}' non e' registrato")
    p = reg["ponti"].pop(nome)
    utente = utente_ponte(nome)
    togli(P(f"{ACCESSO_DIR}/{p['impronta']}.json"))
    togli(P(f"{CHIAVI_DIR}/{utente}"))
    togli(file_configurazione(nome))                     # certificato e canale non piu' consegnabili
    togli(file_certificato_ponte(nome))
    togli(P(f"{AVANZAMENTO_DIR}/{nome}.json"))
    chiudi_sessioni(utente)
    try:
        pwd.getpwnam(utente)
        esegui(["userdel", utente], controlla=False)
    except KeyError:
        pass
    salva_registro(reg)
    passo("Pagina e Caddy")
    scrivi_sito(imp, reg)
    configura_caddy(imp, reg)
    passo("Authentik")
    configura_authentik(imp, reg, {nome: reg["authentik"]["ponti"].get(nome, {})})
    salva_registro(reg)
    ok(f"gateway '{nome}' tolto dalla rete")


