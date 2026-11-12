# --------------------------------------------------------------------------
# sshd: utenti dei ponti
# --------------------------------------------------------------------------

def comando_ponte() -> str:
    """Il solo comando che un ponte puo' lanciare via SSH (dalla 1.10.0): legge la sua configurazione da
    installare e scrive a che punto e' l'installazione. Sempre questo, qualunque cosa chieda il Raspberry."""
    prova = f"/usr/bin/env RETE_PROVA_RADICE={RADICE} " if RADICE else ""
    return f"{prova}/usr/bin/python3 -I {P(OPT_SCRIPT)} ponte"


def testo_dropin() -> str:
    return "\n".join([
        "# Aggiunto da rete-meshtastic.sh (mesh2tak): utenti dei gateway Meshtastic.",
        "# Possono solo tenere aperto un tunnel verso la loro porta sul server (127.0.0.1) e, dalla 1.10.0,",
        "# lanciare un solo comando: configurazione e avanzamento dell'installazione del loro gateway.",
        f"Match Group {GRUPPO}",
        f"\tAuthorizedKeysFile {P(CHIAVI_DIR)}/%u",
        "\tAuthenticationMethods publickey",
        "\tPubkeyAuthentication yes",
        "\tPasswordAuthentication no",
        "\tKbdInteractiveAuthentication no",
        "\tAllowTcpForwarding remote",
        "\tPermitOpen none",
        "\tAllowStreamLocalForwarding no",
        "\tAllowAgentForwarding no",
        "\tX11Forwarding no",
        "\tPermitTTY no",
        "\tPermitTunnel no",
        "\tPermitUserRC no",
        "\tGatewayPorts no",
        f"\tForceCommand {comando_ponte()}",
        "\tClientAliveInterval 30",
        "\tClientAliveCountMax 3",
        "",
    ])


def riga_autorizzata(chiave: str, porta: int) -> str:
    return (f'restrict,port-forwarding,permitlisten="127.0.0.1:{porta}",command="{comando_ponte()}" '
            f"{chiave}\n")


def stessa_chiave_e_porta(vecchia: Optional[str], nuova: str) -> bool:
    """Le due righe di authorized_keys hanno la stessa chiave e la stessa porta (cambia solo il comando): il
    tunnel aperto puo' restare."""
    def parti(riga: str) -> Tuple[str, str]:
        m = re.search(r'permitlisten="([^"]*)"', riga or "")
        k = re.search(r"(ssh-ed25519 [A-Za-z0-9+/=]+)", riga or "")
        return (m.group(1) if m else "", k.group(1) if k else "")
    a, b = parti(vecchia or ""), parti(nuova)
    return bool(a[0] and a[1]) and a == b


def ricarica_ssh() -> None:
    for unita in ("ssh", "sshd"):
        r = esegui(["systemctl", "is-active", unita], controlla=False, attesa=20)
        if r.returncode == 0:
            esegui(["systemctl", "reload", unita], attesa=30)
            return
    # sshd attivato dal socket (Ubuntu 24.04): la configurazione nuova vale dalla prossima connessione


def prepara_sshd() -> None:
    try:
        grp.getgrnam(GRUPPO)
    except KeyError:
        esegui(["groupadd", "--system", GRUPPO])
    os.makedirs(P(CHIAVI_DIR), exist_ok=True)
    os.chmod(P(CONF_DIR), 0o755)
    os.chmod(P(CHIAVI_DIR), 0o755)
    prepara_cartelle_ponti()
    percorso = P(SSHD_DROPIN)
    prima = leggi(percorso)
    if prima == testo_dropin():
        return
    if not os.path.isdir(os.path.dirname(percorso)):
        raise Errore(f"manca {os.path.dirname(SSHD_DROPIN)}: serve OpenSSH 8.2 o piu' recente (Ubuntu 22.04 va bene)")
    principale = leggi(P("/etc/ssh/sshd_config")) or ""
    if principale and not re.search(r"^\s*Include\s+/etc/ssh/sshd_config\.d/", principale, re.M | re.I):
        avviso("sshd_config non include sshd_config.d: le regole dei gateway valgono solo per le chiavi "
               "(restrizioni comunque attive)")
    scrivi(percorso, testo_dropin(), 0o644)
    r = esegui(["sshd", "-t"], controlla=False, attesa=30)
    if r.returncode != 0:
        if prima is None:
            togli(percorso)
        else:
            scrivi(percorso, prima, 0o644)
        raise Errore("sshd rifiuta le regole per i gateway (nessuna modifica applicata): "
                     + ((r.stdout or "") + (r.stderr or "")).strip()[-300:])
    ricarica_ssh()
    ok(f"regole di sshd per i gateway: {SSHD_DROPIN}")


def prepara_cartelle_ponti() -> None:
    """Le cartelle che il comando dei ponti usa (dalla 1.10.0), con il gruppo dei ponti e il bit sticky: ogni
    ponte crea e toglie solo i suoi file, non vede l'elenco degli altri. La configurazione da installare la legge
    solo il suo ponte; l'avanzamento lo legge anche il servizio della pagina (senza privilegi)."""
    try:
        gid = grp.getgrnam(GRUPPO).gr_gid
    except KeyError:
        if not RADICE:
            raise Errore(f"manca il gruppo {GRUPPO}")
        gid = None                      # prove con groupadd finto
    os.makedirs(P(WEB_DIR), exist_ok=True)
    os.chmod(P(WEB_DIR), 0o755)
    for d, modo in ((CONFIGURAZIONI_DIR, 0o1730), (AVANZAMENTO_DIR, 0o1731)):
        os.makedirs(P(d), exist_ok=True)
        if gid is not None:
            os.chown(P(d), 0, gid)
        os.chmod(P(d), modo)
    os.makedirs(P(PONTI_NUOVI_DIR), exist_ok=True)
    os.chmod(P(PONTI_NUOVI_DIR), 0o700)


def porta_ssh() -> int:
    r = esegui(["sshd", "-T"], controlla=False, attesa=30)
    for riga in (r.stdout or "").splitlines():
        if riga.lower().startswith("port "):
            try:
                return int(riga.split()[1])
            except (IndexError, ValueError):
                break
    return 22


def chiavi_host() -> List[str]:
    chiavi = []
    for f in sorted(glob.glob(P("/etc/ssh/ssh_host_*_key.pub"))):
        parti = (leggi(f) or "").split()
        if len(parti) >= 2 and re.match(r"^(ssh-ed25519|ecdsa-sha2-nistp\d+|ssh-rsa)$", parti[0]):
            chiavi.append(f"{parti[0]} {parti[1]}")
    if not chiavi:
        raise Errore("non trovo le chiavi SSH del server in /etc/ssh/ssh_host_*_key.pub")
    return chiavi


def utente_ponte(nome: str) -> str:
    return f"ponte-{nome}"


SHELL_PONTI = "/bin/sh"   # sshd lancia il comando forzato con la shell dell'utente: con nologin non partirebbe


def prepara_utente(nome: str) -> str:
    utente = utente_ponte(nome)
    try:
        pw = pwd.getpwnam(utente)
    except KeyError:
        esegui(["useradd", "--system", "--no-create-home", "--home-dir", "/nonexistent", "--shell",
                SHELL_PONTI, "--gid", GRUPPO, "--comment", f"Gateway Meshtastic {nome} (mesh2tak)", utente])
    else:
        if pw.pw_gid != grp.getgrnam(GRUPPO).gr_gid:
            raise Errore(f"esiste gia' un utente {utente} che non e' di un gateway: scegli un altro nome")
        if pw.pw_shell != SHELL_PONTI:
            # utenti creati prima della 1.10.0. Resta tutto chiuso: sshd lancia solo il comando forzato
            # (ForceCommand), senza terminale, e l'account non ha password
            esegui(["usermod", "-s", SHELL_PONTI, utente])
    # nessuna password ma account non bloccato: con UsePAM no sshd rifiuta anche le chiavi di un account '!'
    esegui(["usermod", "-p", "*", utente])
    return utente


def chiudi_sessioni(utente: str) -> None:
    esegui(["pkill", "-KILL", "-u", utente], controlla=False, attesa=20)


def porte_in_ascolto() -> set:
    porte = set()
    if shutil.which("ss"):
        r = esegui(["ss", "-ltnH"], controlla=False, attesa=20)
        for riga in (r.stdout or "").splitlines():
            campi = riga.split()
            if len(campi) >= 4:
                m = re.search(r":(\d+)$", campi[3])
                if m:
                    porte.add(int(m.group(1)))
        return porte
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):   # senza ss: le porte in ascolto (stato 0A) dal kernel
        for riga in (leggi(f) or "").splitlines()[1:]:
            campi = riga.split()
            if len(campi) > 3 and campi[3] == "0A":
                porte.add(int(campi[1].rsplit(":", 1)[1], 16))
    return porte


