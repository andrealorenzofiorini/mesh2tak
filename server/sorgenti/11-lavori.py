# ---- servizio della pagina (senza privilegi) e passo dei lavori (root) ----

def testo_unita_web() -> str:
    return f"""[Unit]
Description=Rete Meshtastic: servizio della pagina della rete (mesh2tak, senza privilegi)
After=network.target

[Service]
User={UTENTE_WEB}
Group={UTENTE_WEB}
ExecStart=/usr/bin/python3 -I {OPT_SCRIPT} servi
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
CapabilityBoundingSet=
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictNamespaces=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
LockPersonality=true
ReadWritePaths={SERVIZIO_DIR}
MemoryMax=128M

[Install]
WantedBy=multi-user.target
"""


def testo_unita_lavori() -> str:
    return f"""[Unit]
Description=Rete Meshtastic: esegue i lavori chiesti dalla pagina della rete (aggiungi o togli un gateway)

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -I {OPT_SCRIPT} lavora
TimeoutStartSec=45min
"""


def testo_unita_percorso() -> str:
    return f"""[Unit]
Description=Rete Meshtastic: guarda la coda dei lavori della pagina della rete

[Path]
DirectoryNotEmpty={LAVORI_DIR}
Unit=mesh2tak-rete-lavori.service

[Install]
WantedBy=paths.target
"""


def installa_servizio_web() -> None:
    """Utente senza privilegi, cartelle, copia dello script in /opt e servizi di sistema."""
    try:
        pw = pwd.getpwnam(UTENTE_WEB)
    except KeyError:
        esegui(["useradd", "--system", "--no-create-home", "--home-dir", "/nonexistent", "--shell",
                "/usr/sbin/nologin", "--user-group", "--comment", "Rete Meshtastic, pagina della rete (mesh2tak)",
                UTENTE_WEB])
        try:
            pw = pwd.getpwnam(UTENTE_WEB)
        except KeyError:
            if not RADICE:
                raise Errore(f"utente {UTENTE_WEB} non creato")
            pw = None   # prove con useradd finto
    for d, modo, proprio in ((WEB_DIR, 0o755, False), (SERVIZIO_DIR, 0o700, True), (RICHIESTE_DIR, 0o700, True),
                             (LAVORI_DIR, 0o700, True), (TMP_DIR, 0o700, True), (ESITI_DIR, 0o755, False),
                             (INVITI_DIR, 0o700, True), (INSTALLAZIONI_DIR, 0o700, True), (OPT_DIR, 0o755, False)):
        os.makedirs(P(d), exist_ok=True)
        os.chmod(P(d), modo)
        if proprio and pw is not None:
            os.chown(P(d), pw.pw_uid, pw.pw_gid)
    questo = os.path.abspath(__file__)
    nuovo = open(questo, encoding="utf-8").read()
    cambiato = False
    if os.path.realpath(questo) != os.path.realpath(P(OPT_SCRIPT)):
        cambiato = scrivi(P(OPT_SCRIPT), nuovo, 0o755)
    for percorso, testo in ((UNITA_WEB, testo_unita_web()), (UNITA_LAVORI, testo_unita_lavori()),
                            (UNITA_PERCORSO, testo_unita_percorso())):
        cambiato = scrivi(P(percorso), testo, 0o644) or cambiato
    esegui(["systemctl", "daemon-reload"], attesa=60)
    esegui(["systemctl", "enable", "--now", "mesh2tak-rete-web.service", "mesh2tak-rete-lavori.path"], attesa=60)
    if cambiato:
        esegui(["systemctl", "restart", "mesh2tak-rete-web.service"], attesa=60)
    r = esegui(["systemctl", "is-active", "mesh2tak-rete-web.service"], controlla=False, attesa=20)
    if r.returncode == 0:
        ok("servizio della pagina attivo: dalla pagina si aggiungono e tolgono i gateway")
    else:
        avviso("servizio della pagina non attivo: guarda  journalctl -u mesh2tak-rete-web")


def togli_servizio_web() -> None:
    esegui(["systemctl", "disable", "--now", "mesh2tak-rete-web.service", "mesh2tak-rete-lavori.path"],
           controlla=False, attesa=60)
    for f in (UNITA_WEB, UNITA_LAVORI, UNITA_PERCORSO):
        togli(P(f))
    esegui(["systemctl", "daemon-reload"], controlla=False, attesa=60)
    shutil.rmtree(P(OPT_DIR), ignore_errors=True)
    try:
        pwd.getpwnam(UTENTE_WEB)
        esegui(["userdel", UTENTE_WEB], controlla=False)
    except KeyError:
        pass


def installa_rete(imp: Impostazioni, reg: dict, ignora_dns: bool) -> None:
    """La pagina della rete con tutto quello che serve, anche senza ponti."""
    reg["dominio"] = imp.dominio
    passo(f"1/6  Indirizzo della pagina: {imp.url()}")
    controlla_dns(imp, [imp.host()] + [imp.host(n) for n in sorted(reg["ponti"])], ignora_dns)
    passo("2/6  Utenti dei gateway (SSH, solo tunnel)")
    prepara_ponti(imp, reg, sorted(reg["ponti"]))
    passo("3/6  Servizio della pagina (senza privilegi)")
    installa_servizio_web()
    passo("4/6  Caddy")
    configura_caddy(imp, reg)
    passo("5/6  Authentik (solo amministratori)")
    configura_authentik(imp, reg)
    passo("6/6  Pagina della rete e registrazioni per i Raspberry")
    salva_registro(reg)
    scrivi_sito(imp, reg)
    ok(f"pagina pronta in {SITO_DIR}")
    pubblica_registrazioni(imp, reg, sorted(reg["ponti"]))


def prepara_avvisi(imp: Impostazioni) -> None:
    """Dice dove arriveranno gli avvisi e, la prima volta, manda un'email di prova."""
    destinatari = re.sub(r"\s+", " ", str(imp.dati.get("guarddog_alert_email") or "")).strip()
    if not destinatari:
        avviso("in infra-TAK non c'e' un indirizzo per gli avvisi: impostalo nella console (Guard Dog), poi sulla "
               "pagina della rete premi 'Manda un'email di prova'")
        return
    ok(f"avvisi per email a: {destinatari} (gli indirizzi degli avvisi di Guard Dog in infra-TAK)")
    if os.path.exists(P(AVVISI_PROVATI)):
        return
    esito, dettaglio = Sorveglianza.invia(porta_console(imp), "[Rete Meshtastic] Email di prova",
                                          "Gli avvisi della rete Meshtastic sono attivi: ti scrivo quando un gateway non "
                                          "risponde da 10 minuti, perde la radio o il TAK Server, quando il suo "
                                          "certificato sta per scadere, quando la corrente non basta e quando un "
                                          f"Raspberry si riavvia.\nPagina della rete: {imp.url()}")
    if esito == "mandato":
        scrivi(P(AVVISI_PROVATI), time.strftime("%Y-%m-%d %H:%M:%S") + "\n", 0o600)
        ok("email di prova mandata: controlla che sia arrivata (anche nello spam)")
    else:
        avviso(f"email di prova non partita: {dettaglio}. Riprova dalla pagina della rete")


def cmd_installa(a) -> int:
    imp = trova_impostazioni()
    with Blocco():
        reg = carica_registro()
        installa_rete(imp, reg, a.ignora_dns)
    prepara_avvisi(imp)
    print(f"\n{B}Fatto.{N} Pagina della rete: {imp.url()}  (in Authentik: applicazione 'Rete Meshtastic')")
    info("Da ora i gateway si aggiungono dalla pagina: sul Raspberry")
    info("    sudo bash installa-ponte-meshtastic.sh --accesso-esterno")
    info("e sulla pagina approvi la sua richiesta. Qui sul server non serve altro.")
    return 0


def cmd_aggiungi(a) -> int:
    nome = valida_nome(a.nome)
    chiave, impronta = leggi_chiave(a.chiave)
    imp = trova_impostazioni()
    with Blocco():
        reg = carica_registro()
        aggiungi_ponte(imp, reg, nome, chiave, impronta, a.ignora_dns)
        passo("Servizio della pagina")
        installa_servizio_web()
    print(f"\n{B}Fatto.{N}")
    info(f"Entro un paio di minuti il Raspberry apre il tunnel e il gateway '{nome}' compare su:")
    info(f"    {imp.url()}   (in Authentik: applicazione 'Rete Meshtastic')")
    info(f"Gestione del suo nodo: {imp.url(nome)}  (mentre la usi il gateway va in pausa)")
    info("Sul Raspberry lo stato del tunnel:  sudo mesh2tak-accesso")
    return 0


def cmd_elenco(a) -> int:
    imp = trova_impostazioni()
    reg = carica_registro()
    print(f"{B}Rete Meshtastic{N}: {imp.url()}  (versione dello script {VERSIONE})")
    if not reg["ponti"]:
        info("nessun gateway registrato: sul Raspberry  sudo bash installa-ponte-meshtastic.sh --accesso-esterno")
        info("e poi approva la richiesta sulla pagina della rete")
        return 0
    ascolto = porte_in_ascolto()
    for n, p in sorted(reg["ponti"].items()):
        collegato = p["porta"] in ascolto
        stato = f"{V}tunnel collegato{N}" if collegato else f"{G}tunnel non collegato{N}"
        print(f"  {n:<20} {imp.url(n):<45} porta {p['porta']}  {stato}")
    return 0


def cmd_rimuovi(a) -> int:
    nome = valida_nome(a.nome)
    imp = trova_impostazioni()
    with Blocco():
        reg = carica_registro()
        if nome not in reg["ponti"]:
            raise Errore(f"il gateway '{nome}' non e' registrato (vedi: sudo bash {sys.argv[0]} elenco)")
        conferma(f"Tolgo il gateway '{nome}' ({imp.url(nome)}).", nome, a.si)
        rimuovi_ponte(imp, reg, nome)
    print(f"\n{B}Fatto.{N} Gateway '{nome}' tolto. Sul Raspberry, per spegnere anche il tunnel:")
    info("sudo bash installa-ponte-meshtastic.sh --accesso-esterno=no")
    return 0


def cmd_aggiorna(a) -> int:
    imp = trova_impostazioni()
    with Blocco():
        reg = carica_registro()
        installa_rete(imp, reg, getattr(a, "ignora_dns", False))
    print(f"\n{B}Fatto.{N} {imp.url()}")
    return 0


def cmd_disinstalla(a) -> int:
    imp = trova_impostazioni()
    with Blocco():
        reg = carica_registro()
        conferma(f"Tolgo la pagina {imp.url()}, i {len(reg['ponti'])} gateway registrati e le loro voci in Caddy, "
                 "Authentik e sshd.", "disinstalla", a.si)
        passo("1/5  Servizio della pagina")
        togli_servizio_web()
        ok("servizio della pagina tolto")
        passo("2/5  Authentik")
        try:
            togli_authentik(reg)
        except Errore as e:
            avviso(f"Authentik: {e} (le applicazioni 'Rete Meshtastic' e 'Meshtastic ...' toglile a mano)")
        passo("3/5  Caddy")
        configura_caddy(imp, reg, con=False)
        passo("4/5  Utenti dei gateway e sshd")
        for n in list(reg["ponti"]):
            utente = utente_ponte(n)
            chiudi_sessioni(utente)
            try:
                pwd.getpwnam(utente)
                esegui(["userdel", utente], controlla=False)
            except KeyError:
                pass
        if togli(P(SSHD_DROPIN)):
            r = esegui(["sshd", "-t"], controlla=False)
            if r.returncode == 0:
                ricarica_ssh()
        try:
            grp.getgrnam(GRUPPO)
            esegui(["groupdel", GRUPPO], controlla=False)
        except KeyError:
            pass
        ok("utenti, gruppo e regole sshd dei gateway tolti")
        passo("5/5  File")
        shutil.rmtree(P(WEB_DIR), ignore_errors=True)
        shutil.rmtree(P(CADDY_DIR), ignore_errors=True)
        shutil.rmtree(P(CONFIGURAZIONI_DIR), ignore_errors=True)   # dalla 1.10.0: certificato e canale
        shutil.rmtree(P(PONTI_NUOVI_DIR), ignore_errors=True)
        for f in glob.glob(P(CHIAVI_DIR) + "/*"):
            togli(f)
        togli(P(REGISTRO))
        togli(P(AVVISI_PROVATI))
        ok(f"tolti {WEB_DIR}, {CADDY_DIR} e il registro (resta {REGISTRO_LOG})")
    print(f"\n{B}Fatto.{N} Sui Raspberry spegni l'accesso:  sudo bash installa-ponte-meshtastic.sh --accesso-esterno=no")
    return 0


# ---- passo dei lavori (root): esegue solo "aggiungi" e "rimuovi", ognuno controllato da capo ----

RE_ID = re.compile(r"^[0-9a-f]{16}$")
RE_FILE_LAVORO = re.compile(r"^[0-9a-f]{16}\.json$")
RE_IMPRONTA = re.compile(r"^[0-9a-f]{64}$")
RE_UTENTE_AK = re.compile(r"^[\w.@+-]{1,64}$")


LAVORO_MAX = 16384
LAVORO_IMPOSTAZIONI_MAX = 512 << 10      # certificato dei ponti nuovi (base64)


def leggi_installa(d) -> Optional[dict]:
    """Scheda radio e posizione per installare un ponte nuovo (dalla 1.10.0)."""
    if not isinstance(d, dict) or str(d.get("scheda") or "") not in SCHEDE:
        return None
    try:
        return {"scheda": d["scheda"], "posizione": valida_posizione(str(d.get("posizione") or ""))}
    except Errore:
        return None


def controlla_impostazioni(d) -> dict:
    """Certificato e canale per i ponti nuovi, come li manda la pagina: ogni campo controllato (Errore col perche')."""
    if not isinstance(d, dict):
        raise Errore("richiesta non valida")
    nome_pacchetto = os.path.basename(str(d.get("nome_pacchetto") or "").replace("\\", "/")).strip()
    pacchetto = str(d.get("pacchetto") or "")
    password = str(d.get("password") or "")
    host = str(d.get("host") or "").strip()
    canale = str(d.get("canale") or "").strip()
    if not RE_NOME_PACCHETTO.match(nome_pacchetto):
        raise Errore("serve il pacchetto .zip scaricato dalla console infra-TAK (o un .p12)")
    try:
        grezzo = base64.b64decode(pacchetto, validate=True)
    except (binascii.Error, ValueError):
        raise Errore("file del certificato non valido: riprova a sceglierlo")
    if not 0 < len(grezzo) <= PACCHETTO_MAX:
        raise Errore("il file del certificato e' troppo grande: serve il pacchetto della console infra-TAK")
    if len(password) > 128 or not password.isprintable():
        raise Errore("password del certificato non valida")
    if host and not RE_HOST_TAK.match(host):
        raise Errore("indirizzo del TAK Server non valido (es. takserver.tuodominio.it)")
    if not RE_NOME_CANALE.match(canale):
        raise Errore("nome del canale: lettere, numeri, - e _, al massimo 11 caratteri (es. CNSAS-VA)")
    # dalla 1.12.0 niente canale pubblico secondario: "secondario" vuoto per i programmi di primo avvio di prima
    return {"nome_pacchetto": nome_pacchetto, "pacchetto": pacchetto, "password": password, "host": host,
            "canale": canale, "chiave": chiave_canale_valida(str(d.get("chiave") or "")), "secondario": ""}


def leggi_impostazioni_lavoro(d) -> Optional[dict]:
    try:
        return controlla_impostazioni(d)
    except Errore:
        return None


def controlla_certificato_ponte(d) -> dict:
    """Il certificato nuovo di un ponte (dalla 1.13.0), come lo manda la pagina: pacchetto, password e, se il
    pacchetto non lo dice, l'indirizzo del TAK Server."""
    if not isinstance(d, dict):
        raise Errore("richiesta non valida")
    nome_pacchetto = os.path.basename(str(d.get("nome_pacchetto") or "").replace("\\", "/")).strip()
    pacchetto, password, host = str(d.get("pacchetto") or ""), str(d.get("password") or ""), str(d.get("host") or "").strip()
    if not RE_NOME_PACCHETTO.match(nome_pacchetto):
        raise Errore("serve il pacchetto .zip scaricato dalla console infra-TAK (o un .p12)")
    try:
        grezzo = base64.b64decode(pacchetto, validate=True)
    except (binascii.Error, ValueError):
        raise Errore("file del certificato non valido: riprova a sceglierlo")
    if not 0 < len(grezzo) <= PACCHETTO_MAX:
        raise Errore("il file del certificato e' troppo grande: serve il pacchetto della console infra-TAK")
    if len(password) > 128 or not password.isprintable():
        raise Errore("password del certificato non valida")
    if host and not RE_HOST_TAK.match(host):
        raise Errore("indirizzo del TAK Server non valido (es. takserver.tuodominio.it)")
    return {"nome_pacchetto": nome_pacchetto, "pacchetto": pacchetto, "password": password, "host": host}


def valida_impostazioni_ponte(d) -> dict:
    """Le impostazioni del ponte da cambiare (dalla 1.13.0): solo quelle previste, nei loro limiti."""
    if not isinstance(d, dict) or not d:
        raise Errore("nessuna impostazione da cambiare")
    for k, v in d.items():
        if k not in IMPOSTAZIONI_PONTE:
            raise Errore("impostazione sconosciuta: ricarica la pagina")
        tipo, minimo, massimo = IMPOSTAZIONI_PONTE[k]
        if tipo == "squadra":
            buono = v in SQUADRE_TAK
        elif tipo == "si_no":
            buono = isinstance(v, bool)
        else:
            buono = (isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and minimo <= v <= massimo)
        if not buono:
            limiti = f" (da {minimo:g} a {massimo:g})" if tipo == "numero" else ""
            raise Errore(f"valore non valido per {NOMI_IMPOSTAZIONI.get(k, k)}{limiti}")
    return dict(d)


NOMI_IMPOSTAZIONI = {"squadra": "il colore della squadra", "validita_min": "i minuti sulla mappa",
                     "aggiornamento_min": "ogni quanti minuti ripetere le posizioni",
                     "errore_massimo_m": "l'errore massimo delle posizioni", "includi_ponte": "il Raspberry sulla mappa",
                     "invio": "l'invio alle radio", "tempo_radio_percento": "il tempo radio",
                     "pausa_massima_min": "la pausa massima"}


def leggi_lavoro(percorso: str) -> Optional[dict]:
    """Un lavoro scritto dal servizio della pagina: non ci si fida di nulla (niente collegamenti simbolici,
    file piccolo, solo i campi previsti)."""
    try:
        fd = os.open(percorso, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not (st.st_mode & 0o170000 == 0o100000) or st.st_size > LAVORO_IMPOSTAZIONI_MAX:
            return None
        grezzo = b""
        while len(grezzo) <= LAVORO_IMPOSTAZIONI_MAX:
            pezzo = os.read(fd, 65536)
            if not pezzo:
                break
            grezzo += pezzo
        dati = json.loads(grezzo.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    finally:
        os.close(fd)
    if not isinstance(dati, dict) or not RE_ID.match(str(dati.get("id", ""))):
        return None
    azione = dati.get("azione")
    da = str(dati.get("da") or "")
    if (azione not in ("aggiungi", "rimuovi", "aggiorna-server", "aggiorna-ponti", "impostazioni-nuovi", "installa",
                       "azione-ponte", "certificato-ponte")
            or not RE_UTENTE_AK.match(da)):
        return None
    if azione not in ("impostazioni-nuovi", "certificato-ponte") and len(grezzo) > LAVORO_MAX:
        return None
    if azione == "impostazioni-nuovi":
        impostazioni = leggi_impostazioni_lavoro(dati.get("impostazioni"))
        if impostazioni is None:
            return None
        return {"id": dati["id"], "azione": azione, "nome": "ponti-nuovi", "da": da, "impostazioni": impostazioni,
                "richiesta": None, "sostituisci": False}
    if azione in ("azione-ponte", "certificato-ponte"):
        # dalla 1.13.0: un comando per un ponte, o il suo certificato nuovo (controllato da capo)
        nome = str(dati.get("nome") or "")
        if not RE_NOME.match(nome):
            return None
        lavoro = {"id": dati["id"], "azione": azione, "nome": nome, "da": da, "richiesta": None, "sostituisci": False}
        try:
            if azione == "azione-ponte":
                lavoro["cosa"] = str(dati.get("cosa") or "")
                if lavoro["cosa"] not in AZIONI_PONTE:
                    return None
                lavoro["valori"] = (valida_impostazioni_ponte(dati.get("valori")) if lavoro["cosa"] == "impostazioni"
                                    else None)
            else:
                lavoro["impostazioni"] = controlla_certificato_ponte(dati.get("impostazioni"))
        except Errore:
            return None
        return lavoro
    if azione == "installa":
        d = dati.get("installa") if isinstance(dati.get("installa"), dict) else {}
        installa: dict = {}
        if d.get("scheda"):
            if d["scheda"] not in SCHEDE:
                return None
            installa["scheda"] = d["scheda"]
        if "posizione" in d:
            try:
                installa["posizione"] = valida_posizione(str(d.get("posizione") or ""))
            except Errore:
                return None
        nome = str(dati.get("nome") or "")
        if not RE_NOME.match(nome):
            return None
        return {"id": dati["id"], "azione": azione, "nome": nome, "da": da, "installa": installa, "richiesta": None,
                "sostituisci": False}
    if azione.startswith("aggiorna-"):
        versione = str(dati.get("versione") or "")
        nome = str(dati.get("nome") or "")
        if not RE_VERSIONE_RILASCIO.match(versione):
            return None
        if (azione == "aggiorna-server" and nome != "server") or (azione == "aggiorna-ponti" and nome != "tutti"
                                                                   and not RE_NOME.match(nome)):
            return None
        return {"id": dati["id"], "azione": azione, "nome": nome, "da": da, "versione": versione, "richiesta": None,
                "sostituisci": False, "reimposta": azione == "aggiorna-ponti" and dati.get("reimposta") is True}
    try:
        lavoro = {"id": dati["id"], "azione": azione, "nome": valida_nome(str(dati.get("nome", ""))), "da": da,
                  "sostituisci": dati.get("sostituisci") is True}
        if azione == "aggiungi":
            lavoro["chiave"], lavoro["impronta"] = leggi_chiave(str(dati.get("chiave", "")))
            if dati.get("installa") is not None:
                lavoro["installa"] = leggi_installa(dati.get("installa"))
                if lavoro["installa"] is None:
                    return None
    except Errore:
        return None
    richiesta = str(dati.get("richiesta") or "")
    lavoro["richiesta"] = richiesta if RE_IMPRONTA.match(richiesta) else None
    return lavoro


def scrivi_esito(lavoro: dict, esito: str, messaggio: str, testo: str = "") -> None:
    os.makedirs(P(ESITI_DIR), exist_ok=True)
    pulito = re.sub(r"\x1b\[[0-9;]*m", "", testo)
    dati = {"id": lavoro["id"], "azione": lavoro["azione"], "nome": lavoro["nome"], "da": lavoro["da"],
            "richiesta": lavoro.get("richiesta"), "versione": lavoro.get("versione"), "esito": esito,
            "messaggio": messaggio[:600], "testo": pulito[-4000:], "quando": int(time.time()),
            "cosa": lavoro.get("cosa"), "reimposta": lavoro.get("reimposta") is True}
    scrivi(P(f"{ESITI_DIR}/{lavoro['id']}.json"), json.dumps(dati, ensure_ascii=False) + "\n", 0o644)


CONFIGURAZIONE_SCADE_S = 30 * 24 * 3600


def pulisci_configurazioni() -> None:
    """Configurazioni dei ponti nuovi mai scaricate fino in fondo (certificato e canale dentro): dopo 30 giorni
    via. Quelle dei ponti tolti le toglie gia' "togli dalla rete"."""
    for f in glob.glob(P(CONFIGURAZIONI_DIR) + "/*.json"):
        try:
            # il certificato nuovo di un ponte (1.13.0) resta al massimo un giorno: il Raspberry lo prende subito
            scade = CERTIFICATO_PONTE_SCADE_S if f.endswith(".certificato.json") else CONFIGURAZIONE_SCADE_S
            if time.time() - os.lstat(f).st_mtime > scade:
                togli(f)
                registra(f"configurazione scaduta tolta: {os.path.basename(f)}")
        except OSError:
            pass


def numeri_versione(v: str) -> Tuple[int, ...]:
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return (0,)                     # una versione di prova, non costruita: piu' vecchia di tutte


def scarica_rilascio(versione: str, nome_file: str, massimo: int = 16 << 20) -> bytes:
    """Un file della release ufficiale su GitHub, controllato con lo SHA256SUMS della stessa release."""
    base = f"{GITHUB}/{REPO}/releases/download/v{versione}"

    def prendi(url: str, limite: int) -> bytes:
        apri = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with apri.open(urllib.request.Request(url, headers={"User-Agent": f"mesh2tak-rete/{VERSIONE}"}),
                           timeout=60) as r:
                dati = r.read(limite + 1)
        except urllib.error.HTTPError as e:
            raise Errore(f"GitHub risponde {e.code} per {url}")
        except (urllib.error.URLError, OSError) as e:
            raise Errore(f"non riesco a scaricare {url} ({getattr(e, 'reason', e)})")
        if len(dati) > limite:
            raise Errore(f"{url}: file troppo grande")
        return dati
    somme = prendi(f"{base}/SHA256SUMS", 65536).decode("utf-8", "replace")
    dati = prendi(f"{base}/{nome_file}", massimo)
    attesa = None
    for riga in somme.splitlines():
        parti = riga.split()
        if len(parti) == 2 and parti[1].lstrip("*") == nome_file:
            attesa = parti[0].lower()
    if not attesa or hashlib.sha256(dati).hexdigest() != attesa:
        raise Errore("il file scaricato non e' quello pubblicato nella release: non lo installo")
    return dati


ATTESA_SERVIZIO_S = float(os.environ.get("RETE_PROVA_ATTESA_SERVIZIO", "90"))


def ultima_riga(testo: str) -> str:
    """La riga dell'errore (comincia con ✘), altrimenti l'ultima."""
    righe = [r.strip() for r in re.sub(r"\x1b\[[0-9;]*m", "", testo).strip().splitlines() if r.strip()]
    utili = [r for r in righe if r.startswith("✘")]
    return (utili or righe or ["nessun messaggio"])[-1].strip(" ✘!")[:300]


def servizio_risponde(versione: str) -> bool:
    """Il servizio della pagina e' ripartito con quella versione."""
    fine = time.time() + ATTESA_SERVIZIO_S
    while True:
        try:
            apri = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            rq = urllib.request.Request(f"http://127.0.0.1:{PORTA_WEB}/api/stato",
                                        headers={"X-Authentik-Username": "aggiornamento"})
            with apri.open(rq, timeout=10) as r:
                if json.loads(r.read(1 << 20)).get("versione") == versione:
                    return True
        except (OSError, ValueError, urllib.error.URLError):
            pass
        if time.time() >= fine:
            return False
        time.sleep(2)


def aggiorna_server(lavoro: dict) -> str:
    """Scarica dalla release su GitHub la versione chiesta, la installa con il suo 'installa', controlla che il
    servizio della pagina riparta con quella versione; altrimenti rimette questa. Da root, fuori dal blocco."""
    versione = lavoro["versione"]
    if numeri_versione(versione) <= numeri_versione(VERSIONE):
        raise Errore(f"il server ha gia' la {VERSIONE}: niente da fare")
    script = scarica_rilascio(versione, "rete-meshtastic.sh")
    if f'VERSIONE = "{versione}"'.encode() not in script:
        raise Errore(f"il file scaricato non e' la versione {versione}: non lo installo")
    nuovo = P(f"{CONF_DIR}/rete-meshtastic-{versione}.sh")
    scrivi(nuovo, script.decode("utf-8"), 0o700)
    precedente = P(f"{CONF_DIR}/rete_meshtastic-precedente.py")
    if os.path.exists(P(OPT_SCRIPT)):
        shutil.copyfile(P(OPT_SCRIPT), precedente)
        os.chmod(precedente, 0o700)
    ambiente = {k: v for k, v in os.environ.items() if k != "RETE_COLORI"}

    def lancia(comando: List[str]) -> Tuple[int, str]:
        try:
            r = subprocess.run(comando, capture_output=True, text=True, timeout=900, env=ambiente,
                               stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return 124, "✘ l'installazione non ha finito in 15 minuti"
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    try:
        esito, uscita = lancia(["bash", nuovo, "installa"])
    finally:
        togli(nuovo)
    if esito == 0 and servizio_risponde(versione):
        return uscita + f"\nServer aggiornato dalla {VERSIONE} alla {versione}."
    motivo = (f"l'installazione della {versione} non e' riuscita ({ultima_riga(uscita)})" if esito != 0
              else f"con la {versione} il servizio della pagina non riparte")
    if not os.path.exists(precedente):
        raise ErroreConTesto(f"{motivo}; manca la {VERSIONE} per tornare indietro", uscita)
    e2, t2 = lancia([sys.executable, "-I", precedente, "installa"])
    uscita += "\n--- ritorno alla versione di prima ---\n" + t2
    if e2 == 0 and servizio_risponde(VERSIONE):
        raise ErroreConTesto(f"{motivo}: rimessa la {VERSIONE}", uscita)
    raise ErroreConTesto(f"{motivo}; anche il ritorno alla {VERSIONE} non e' riuscito", uscita)


class ErroreConTesto(Errore):
    def __init__(self, messaggio: str, testo: str):
        super().__init__(messaggio)
        self.testo = testo


def chiedi_aggiornamento_ponti(imp: Impostazioni, reg: dict, lavoro: dict) -> None:
    """Scrive la richiesta nella registrazione dei ponti: ognuno la vede entro 2 minuti e si aggiorna da solo."""
    nomi = sorted(reg["ponti"]) if lavoro["nome"] == "tutti" else [lavoro["nome"]]
    if not nomi or any(n not in reg["ponti"] for n in nomi):
        raise Errore("gateway non trovato: ricarica la pagina")
    ora = int(time.time())
    for n in nomi:
        reg["ponti"][n]["aggiorna"] = {"versione": lavoro["versione"], "chiesto": ora}
        if lavoro.get("reimposta"):
            reg["ponti"][n]["aggiorna"]["reimposta"] = True
    salva_registro(reg)
    pubblica_registrazioni(imp, reg, nomi)
    ok(f"chiesto l'aggiornamento alla {lavoro['versione']} a: {', '.join(nomi)}"
       + (" (anche la radio come all'installazione)" if lavoro.get("reimposta") else ""))
    svegli = [n for n in nomi if sveglia_ponte(reg["ponti"][n]["porta"])]   # dalla 1.13.0: subito, non entro 2 minuti
    if svegli:
        info(f"cominciano subito: {', '.join(svegli)}")


def cmd_lavora(a) -> int:
    imp = trova_impostazioni()
    cartella = P(LAVORI_DIR)
    server_da_aggiornare: List[dict] = []
    with Blocco(attendi=True):
        for nome_file in sorted(os.listdir(cartella)) if os.path.isdir(cartella) else []:
            percorso = os.path.join(cartella, nome_file)
            lavoro = leggi_lavoro(percorso) if RE_FILE_LAVORO.match(nome_file) else None
            try:
                togli(percorso)
            except IsADirectoryError:
                shutil.rmtree(percorso, ignore_errors=True)
            if lavoro is None:
                registra(f"lavoro scartato (non valido): {nome_file!r}")
                continue
            registra(f"lavoro {lavoro['id']}: {lavoro['azione']} {lavoro['nome']} chiesto da {lavoro['da']}")
            if lavoro["azione"] == "aggiorna-server":       # dopo, senza il blocco: lo prende 'installa'
                server_da_aggiornare.append(lavoro)
                scrivi_esito(lavoro, "in_corso", "in corso")
                continue
            scrivi_esito(lavoro, "in_corso", "in corso")
            uscita = io.StringIO()
            try:
                messaggio = "fatto"
                with contextlib.redirect_stdout(uscita):
                    reg = carica_registro()
                    if lavoro["azione"] == "aggiungi":
                        aggiungi_ponte(imp, reg, lavoro["nome"], lavoro["chiave"], lavoro["impronta"],
                                       cambio_chiave=lavoro["sostituisci"], installa=lavoro.get("installa"),
                                       da=lavoro["da"])
                    elif lavoro["azione"] == "aggiorna-ponti":
                        chiedi_aggiornamento_ponti(imp, reg, lavoro)
                    elif lavoro["azione"] == "impostazioni-nuovi":
                        messaggio = salva_impostazioni_nuovi(lavoro)
                    elif lavoro["azione"] == "installa":
                        riprova_installazione(imp, reg, lavoro)
                    elif lavoro["azione"] == "azione-ponte":
                        messaggio = chiedi_al_ponte(imp, reg, lavoro)
                    elif lavoro["azione"] == "certificato-ponte":
                        messaggio = prepara_certificato_ponte(imp, reg, lavoro)
                    else:
                        rimuovi_ponte(imp, reg, lavoro["nome"])
                scrivi_esito(lavoro, "ok", messaggio, uscita.getvalue())
            except Errore as e:
                registra(f"lavoro {lavoro['id']}: ERRORE {e}")
                scrivi_esito(lavoro, "errore", str(e), uscita.getvalue())
            except Exception as e:  # noqa: un errore inatteso non deve fermare gli altri lavori
                registra(f"lavoro {lavoro['id']}: ERRORE INATTESO {e!r}")
                scrivi_esito(lavoro, "errore", f"errore inatteso: {e}", uscita.getvalue())
        # esiti vecchi: bastano gli ultimi
        esiti = sorted(glob.glob(P(ESITI_DIR) + "/*.json"), key=lambda f: os.path.getmtime(f))
        for f in esiti[:-30]:
            togli(f)
        pulisci_configurazioni()
    for lavoro in server_da_aggiornare[-1:]:           # piu' richieste insieme: basta l'ultima
        try:
            testo = aggiorna_server(lavoro)
            scrivi_esito(lavoro, "ok", f"server aggiornato alla {lavoro['versione']}", testo)
            registra(f"lavoro {lavoro['id']}: server aggiornato alla {lavoro['versione']}")
        except ErroreConTesto as e:
            registra(f"lavoro {lavoro['id']}: ERRORE {e}")
            scrivi_esito(lavoro, "errore", str(e), e.testo)
        except Errore as e:
            registra(f"lavoro {lavoro['id']}: ERRORE {e}")
            scrivi_esito(lavoro, "errore", str(e))
        except Exception as e:  # noqa
            registra(f"lavoro {lavoro['id']}: ERRORE INATTESO {e!r}")
            scrivi_esito(lavoro, "errore", f"errore inatteso: {e}")
    for lavoro in server_da_aggiornare[:-1]:
        scrivi_esito(lavoro, "ok", "sostituito da una richiesta piu' recente")
    return 0


