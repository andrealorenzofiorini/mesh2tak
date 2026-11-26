# ---- gateway in ascolto: i pacchetti li converte questo server (dalla 1.23.0) ----
#
# Un gateway puo' chiedere di entrare "in ascolto": lavora per un altro server (converte e parla con quel TAK Server)
# e a questo manda solo una copia dei pacchetti che riceve dalla radio. Qui li converte lo stesso programma del
# gateway (mesh2tak.py, incorporato in questo script), un servizio per gateway, con un utente senza privilegi e un
# certificato del TAK Server di questo server. La pagina del convertitore ascolta dove ascolterebbe il tunnel di un
# gateway normale ("porta"): per la pagina della rete, la mappa e gli avvisi e' un gateway come gli altri. Il tunnel
# del Raspberry arriva su un'altra porta ("porta_inoltro"), da cui escono solo i pacchetti.
# Solo ascolto: da qui non si trasmette alle radio e non si comanda il Raspberry.

CONV_DIR = OPT_DIR + "/convertitore"
CONV_VENV = CONV_DIR + "/venv"
CONV_PROGRAMMA = CONV_DIR + "/mesh2tak.py"
CONV_LIBRERIE = CONV_DIR + "/librerie.txt"
CONV_CONF_DIR = CONF_DIR + "/convertitore"           # certificato del TAK Server e configurazioni: root e il servizio
CONV_TAK = CONV_CONF_DIR + "/tak.json"
CONV_STATO_DIR = "/var/lib/mesh2tak-rete-convertitore"
UTENTE_CONV = "mesh2tak-converte"
UNITA_CONV = "/etc/systemd/system/mesh2tak-rete-converte@.service"
# Le stesse librerie del Raspberry (installa.sh.in): build.py controlla che le versioni siano uguali
CONV_MESHTASTIC = "2.7.11"
CONV_TAK_SDK = "0.10.0"
CONV_TAK_SDK_FILE = "meshtastic_tak-0.10.0-py3-none-any.whl"
CONV_TAK_SDK_SHA256 = "34c0501137830712f69a41c2a2cfa0d0f57ab55a5f395d7b525e9b14219365d1"
PONTE_GZ_B64 = "@@PONTE_GZ_B64@@"


def porta_tunnel(p: dict) -> int:
    """Dove arriva il tunnel SSH del Raspberry: la pagina del gateway, o per uno in ascolto i suoi pacchetti."""
    return p["porta_inoltro"] if p.get("ascolto") else p["porta"]


def in_ascolto(reg: dict) -> List[str]:
    return sorted(n for n, p in reg["ponti"].items() if p.get("ascolto"))


def unita_convertitore(nome: str) -> str:
    return f"mesh2tak-rete-converte@{nome}.service"


def testo_unita_convertitore() -> str:
    return f"""[Unit]
Description=Rete Meshtastic: converte per il TAK Server i pacchetti del gateway %i (in ascolto, senza privilegi)
After=network-online.target
Wants=network-online.target

[Service]
User={UTENTE_CONV}
Group={UTENTE_CONV}
ExecStart={CONV_VENV}/bin/python {CONV_PROGRAMMA} -c {CONV_CONF_DIR}/%i.conf
Restart=always
RestartSec=10
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
ReadWritePaths={CONV_STATO_DIR}
MemoryMax=256M

[Install]
WantedBy=multi-user.target
"""


def leggi_tak_convertitore() -> Optional[dict]:
    testo = leggi(P(CONV_TAK))
    try:
        d = json.loads(testo) if testo else None
    except ValueError:
        return None
    if (isinstance(d, dict) and RE_HOST_TAK.match(str(d.get("host", ""))) and isinstance(d.get("porta"), int)
            and os.path.isfile(P(CONV_CONF_DIR + "/client.pem")) and os.path.isfile(P(CONV_CONF_DIR + "/client.key"))):
        return d
    return None


def _gruppo_convertitore() -> int:
    try:
        return pwd.getpwnam(UTENTE_CONV).pw_gid
    except KeyError:
        if not RADICE:
            raise Errore(f"utente {UTENTE_CONV} non creato")
        return os.getegid()     # prove con useradd finto


def _scrivi_segreto(percorso: str, testo: str) -> bool:
    """Leggibile da root e dal servizio dei convertitori, da nessun altro."""
    cambiato = scrivi(percorso, testo, 0o640)
    os.chown(percorso, 0 if not RADICE else os.geteuid(), _gruppo_convertitore())
    return cambiato


def prepara_convertitore() -> None:
    """Utente, cartelle, programma e librerie del convertitore. Si puo' rilanciare: rifa' solo quello che manca."""
    try:
        pwd.getpwnam(UTENTE_CONV)
    except KeyError:
        esegui(["useradd", "--system", "--no-create-home", "--home-dir", "/nonexistent", "--shell",
                "/usr/sbin/nologin", "--user-group", "--comment", "Rete Meshtastic, gateway in ascolto (mesh2tak)",
                UTENTE_CONV])
    gruppo = _gruppo_convertitore()
    for d, modo in ((CONV_DIR, 0o755), (CONV_CONF_DIR, 0o750), (CONV_STATO_DIR, 0o750)):
        os.makedirs(P(d), exist_ok=True)
        os.chmod(P(d), modo)
    os.chown(P(CONV_CONF_DIR), 0 if not RADICE else os.geteuid(), gruppo)
    if not RADICE:
        os.chown(P(CONV_STATO_DIR), pwd.getpwnam(UTENTE_CONV).pw_uid, gruppo)
    if PONTE_GZ_B64.startswith("@" "@"):
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mesh2tak.py"),
                  encoding="utf-8") as f:
            programma = f.read()      # prove: il programma accanto, nel repository
    else:
        import gzip
        programma = gzip.decompress(base64.b64decode(PONTE_GZ_B64)).decode("utf-8")
    scrivi(P(CONV_PROGRAMMA), programma, 0o755)
    scrivi(P(UNITA_CONV), testo_unita_convertitore(), 0o644)
    volute = f"meshtastic=={CONV_MESHTASTIC} meshtastic-tak=={CONV_TAK_SDK}\n"
    python = P(CONV_VENV) + "/bin/python"
    if RADICE and os.environ.get("RETE_PROVA_PYTHON"):
        os.makedirs(os.path.dirname(python), exist_ok=True)       # prove: niente download
        if not os.path.exists(python):
            os.symlink(os.environ["RETE_PROVA_PYTHON"], python)
        scrivi(P(CONV_LIBRERIE), volute, 0o644)
    elif leggi(P(CONV_LIBRERIE)) != volute or not os.path.exists(python):
        info(f"librerie del convertitore (Meshtastic {CONV_MESHTASTIC}, TAK {CONV_TAK_SDK}): le scarico, un minuto...")
        if not os.path.exists(python):
            r = esegui([sys.executable, "-m", "venv", P(CONV_VENV)], controlla=False, attesa=300)
            if r.returncode != 0:
                shutil.rmtree(P(CONV_VENV), ignore_errors=True)
                esegui(["apt-get", "install", "-y", "-q", "python3-venv"], controlla=False, attesa=600,
                       ambiente=dict(os.environ, DEBIAN_FRONTEND="noninteractive"))
                esegui([sys.executable, "-m", "venv", P(CONV_VENV)], attesa=300)
        pip = [python, "-m", "pip", "install", "-q", "--disable-pip-version-check"]
        esegui(pip + ["--upgrade", "pip"], controlla=False, attesa=600)
        esegui(pip + ["--no-deps", f"meshtastic=={CONV_MESHTASTIC}"], attesa=600)
        libreria_tak = f"meshtastic-tak=={CONV_TAK_SDK}"
        ruota = P(CONV_DIR + "/" + CONV_TAK_SDK_FILE)
        try:
            with urllib.request.urlopen("https://github.com/meshtastic/TAKPacket-SDK/releases/download/"
                                        f"v{CONV_TAK_SDK}/{CONV_TAK_SDK_FILE}", timeout=120) as r:
                dati = r.read(64 << 20)
            if hashlib.sha256(dati).hexdigest() == CONV_TAK_SDK_SHA256:
                with open(ruota, "wb") as f:
                    f.write(dati)
                libreria_tak = ruota
        except (OSError, ValueError):
            pass
        if libreria_tak != ruota:
            avviso("libreria TAK non scaricata da GitHub (o diversa da quella provata): la cerco su PyPI")
        esegui(pip + ["pyserial>=3.5", "protobuf>=5.29,<8", "pypubsub>=4.0.3", "tabulate>=0.9", "requests>=2.31",
                      "packaging>=24.0", "zstandard>=0.23,<1", libreria_tak], attesa=900)
        togli(ruota)
        esegui([python, "-c", "import meshtastic.mesh_interface, pubsub, google.protobuf"], attesa=120)
        scrivi(P(CONV_LIBRERIE), volute, 0o644)
    if not os.path.isfile(P(CONV_CONF_DIR + "/pagina.key")):
        # la pagina del convertitore parla solo con questo server (127.0.0.1), come il tunnel di un gateway
        esegui(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                "-days", "3650", "-subj", "/CN=mesh2tak-convertitore", "-keyout", P(CONV_CONF_DIR + "/pagina.key"),
                "-out", P(CONV_CONF_DIR + "/pagina.pem")], attesa=60)
        os.chmod(P(CONV_CONF_DIR + "/pagina.key"), 0o640)
        os.chown(P(CONV_CONF_DIR + "/pagina.key"), 0 if not RADICE else os.geteuid(), gruppo)
        os.chmod(P(CONV_CONF_DIR + "/pagina.pem"), 0o644)
    esegui(["systemctl", "daemon-reload"], attesa=60)


def testo_conf_convertitore(nome: str, p: dict, tak: dict) -> str:
    ca = P(CONV_CONF_DIR + "/ca.pem")
    return f"""# Gateway {nome} in ascolto: lo scrive rete-meshtastic.sh, non modificarlo a mano
[tak]
host = {tak['host']}
porta = {tak['porta']}
certificato = {P(CONV_CONF_DIR)}/client.pem
chiave = {P(CONV_CONF_DIR)}/client.key
ca_server = {ca if os.path.isfile(ca) else ''}

[meshtastic]
collegamento = inoltro:http://127.0.0.1:{p['porta_inoltro']}

[gestione]
attiva = si
indirizzo = 127.0.0.1
porta_pagina = {p['porta']}
porta_app = 0
certificato = {P(CONV_CONF_DIR)}/pagina.pem
chiave = {P(CONV_CONF_DIR)}/pagina.key

[accesso_esterno]
cartella_stato = {P(CONV_STATO_DIR)}/{nome}
"""


def avvia_convertitore(nome: str, p: dict) -> None:
    tak = leggi_tak_convertitore()
    if not tak:
        raise Errore("manca il certificato con cui questo server manda al suo TAK Server i dati dei gateway in ascolto")
    stato = P(f"{CONV_STATO_DIR}/{nome}")
    os.makedirs(stato, exist_ok=True)
    os.chmod(stato, 0o750)
    if not RADICE:
        pw = pwd.getpwnam(UTENTE_CONV)
        os.chown(stato, pw.pw_uid, pw.pw_gid)
    cambiato = _scrivi_segreto(P(f"{CONV_CONF_DIR}/{nome}.conf"), testo_conf_convertitore(nome, p, tak))
    esegui(["systemctl", "enable", "--now", unita_convertitore(nome)], attesa=60)
    if cambiato:
        esegui(["systemctl", "restart", unita_convertitore(nome)], attesa=60)
    ok(f"gateway {nome} in ascolto: i suoi pacchetti li converte questo server per {tak['host']}:{tak['porta']}")


def togli_convertitore(nome: str) -> None:
    esegui(["systemctl", "disable", "--now", unita_convertitore(nome)], controlla=False, attesa=60)
    togli(P(f"{CONV_CONF_DIR}/{nome}.conf"))
    shutil.rmtree(P(f"{CONV_STATO_DIR}/{nome}"), ignore_errors=True)


def aggiorna_convertitori(reg: dict) -> None:
    """Dopo un aggiornamento dello script: programma nuovo e servizi riavviati. Senza gateway in ascolto non fa
    niente (e non installa niente)."""
    nomi = in_ascolto(reg)
    if not nomi:
        return
    prepara_convertitore()
    if not leggi_tak_convertitore():
        avviso("gateway in ascolto fermi: manca il certificato del TAK Server per il convertitore")
        return
    for n in nomi:
        avvia_convertitore(n, reg["ponti"][n])
        esegui(["systemctl", "restart", unita_convertitore(n)], controlla=False, attesa=60)


def togli_convertitori(reg: dict) -> None:
    for n in in_ascolto(reg):
        togli_convertitore(n)
    togli(P(UNITA_CONV))
    shutil.rmtree(P(CONV_CONF_DIR), ignore_errors=True)
    shutil.rmtree(P(CONV_STATO_DIR), ignore_errors=True)
    try:
        pwd.getpwnam(UTENTE_CONV)
        esegui(["userdel", UTENTE_CONV], controlla=False)
    except KeyError:
        pass


def _apri_pacchetto(percorso: str, password: str, cartella: str) -> dict:
    """Dal pacchetto (.zip della console, oppure .p12) a certificato, chiave senza password e autorita' in PEM."""
    file_dentro: Dict[str, str] = {}
    if percorso.lower().endswith(".zip"):
        import zipfile
        try:
            z = zipfile.ZipFile(percorso)
            for i in z.infolist():
                b = os.path.basename(i.filename)
                if (not i.is_dir() and i.file_size < 1 << 20 and len(file_dentro) < 40 and not b.startswith("._")
                        and re.match(r"^[A-Za-z0-9._ -]{1,100}$", b)):
                    with open(os.path.join(cartella, "z-" + b), "wb") as f:
                        f.write(z.read(i))
                    file_dentro[b] = os.path.join(cartella, "z-" + b)
        except (zipfile.BadZipFile, OSError, RuntimeError, ValueError):
            raise Errore("il file .zip non si apre")
    else:
        file_dentro[os.path.basename(percorso)] = percorso
    host, porta = "", 8089
    if "README.txt" in file_dentro:
        testo = open(file_dentro["README.txt"], encoding="utf-8", errors="replace").read()
        m = re.search(r"^Server:\s*(\S+)", testo, re.M)
        if m and RE_HOST_TAK.match(m.group(1)):
            host = m.group(1)
        m = re.search(r"^Port:\s*(\d+)", testo, re.M)
        if m and 0 < int(m.group(1)) < 65536:
            porta = int(m.group(1))
    nomi = sorted(file_dentro)
    cert = next((file_dentro[b] for b in nomi if b.endswith(".pem") and b not in ("ca-chain.pem", "ca.pem")), None)
    chiave = next((file_dentro[b] for b in nomi if b.endswith(".key")), None)
    p12 = next((file_dentro[b] for b in nomi if b.endswith((".p12", ".pfx")) and not b.endswith("-legacy.p12")
                and not b.startswith("truststore-")), None)
    ca = next((file_dentro[b] for b in nomi if b in ("ca-chain.pem", "ca.pem")), None)
    fiducia = next((file_dentro[b] for b in nomi if b.startswith("truststore-") and b.endswith(".p12")), None)
    esce = {k: os.path.join(cartella, k) for k in ("client.pem", "client.key", "ca.pem")}

    def dal_p12(sorgente: str, opzioni: List[str], dest: str, pw: str) -> bool:
        for extra in ([], ["-legacy"]):
            r = openssl(["pkcs12"] + extra + ["-in", sorgente, "-passin", "env:M2T_PASS"] + opzioni + ["-out", dest], pw)
            if r.returncode == 0 and os.path.getsize(dest) > 0:
                return True
        return False
    if cert and chiave:
        shutil.copyfile(cert, esce["client.pem"])
        if openssl(["pkey", "-in", chiave, "-passin", "env:M2T_PASS", "-out", esce["client.key"]], password).returncode:
            raise Errore("password del certificato sbagliata")
    elif p12:
        if not (dal_p12(p12, ["-clcerts", "-nokeys"], esce["client.pem"], password)
                and dal_p12(p12, ["-nocerts", "-nodes"], esce["client.key"], password)):
            raise Errore("password del certificato sbagliata, o file .p12 rovinato")
        if not ca:
            dal_p12(p12, ["-cacerts", "-nokeys"], esce["ca.pem"], password)
    else:
        raise Errore("nel pacchetto non c'e' ne' il certificato .pem/.key ne' il .p12")
    if ca:
        shutil.copyfile(ca, esce["ca.pem"])
    elif fiducia and not (os.path.exists(esce["ca.pem"]) and os.path.getsize(esce["ca.pem"])):
        dal_p12(fiducia, ["-nokeys"], esce["ca.pem"], password) or dal_p12(fiducia, ["-nokeys"], esce["ca.pem"], "atakatak")
    r = openssl(["x509", "-in", esce["client.pem"], "-noout", "-subject", "-enddate", "-nameopt", "multiline"], "")
    m = re.search(r"commonName\s*=\s*(.+)", r.stdout or "")
    return {"host": host, "porta": porta, "file": esce,
            "nome": re.sub(r"[^\w.@ -]", "", m.group(1).strip())[:64] if m else ""}


def cmd_certificato_convertitore(a) -> int:
    """Il certificato con cui questo server manda al suo TAK Server i dati dei gateway in ascolto. Nello stack takit
    lo lancia il modulo, con un certificato fatto apposta; accanto a infra-TAK si lancia a mano con il pacchetto
    scaricato dalla console."""
    if not os.path.isfile(a.pacchetto):
        raise Errore(f"non trovo {a.pacchetto}")
    password = a.password
    if a.password_da:
        password = (leggi(a.password_da) or "").strip()
    with Blocco(attendi=True):
        prepara_convertitore()
        cartella = P(CONV_CONF_DIR + "/nuovo")
        shutil.rmtree(cartella, ignore_errors=True)
        os.makedirs(cartella, mode=0o700)
        try:
            c = _apri_pacchetto(a.pacchetto, password or "atakatak", cartella)
            host, porta = a.host or c["host"], a.porta or c["porta"]
            if not RE_HOST_TAK.match(host or ""):
                raise Errore("il pacchetto non dice l'indirizzo del TAK Server: aggiungi --host takserver.tuodominio.it")
            for nome_file, sorgente in c["file"].items():
                if os.path.isfile(sorgente) and os.path.getsize(sorgente):
                    _scrivi_segreto(P(f"{CONV_CONF_DIR}/{nome_file}"), open(sorgente, encoding="utf-8").read())
                else:
                    togli(P(f"{CONV_CONF_DIR}/{nome_file}"))
            scrivi(P(CONV_TAK), json.dumps({"formato": 1, "host": host, "porta": porta, "certificato": c["nome"],
                                           "aggiornato": int(time.time())}) + "\n", 0o644)
        finally:
            shutil.rmtree(cartella, ignore_errors=True)
        ok(f"certificato '{c['nome'] or '?'}' per i gateway in ascolto, TAK Server {host}:{porta}")
        reg = carica_registro()
        for n in in_ascolto(reg):
            avvia_convertitore(n, reg["ponti"][n])
            esegui(["systemctl", "restart", unita_convertitore(n)], controlla=False, attesa=60)
    return 0
