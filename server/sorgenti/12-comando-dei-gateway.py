# ---- comando dei ponti via SSH (dalla 1.10.0): lo lancia sshd, con l'utente del ponte, senza terminale ----

RE_FASE = re.compile(r"^(attesa|configurazione|scarico|installo|riavvio|fatto|errore)$")


def scrivi_avanzamento(nome: str, fase: str, testo: str) -> None:
    """A che punto e' l'installazione del ponte: lo legge la pagina della rete. Scritto dall'utente del ponte nella
    cartella con il bit sticky: un ponte scrive solo il suo file."""
    testo = re.sub(r"[\x00-\x1f\x7f]+", " ", testo).strip()[:300]
    finale = P(f"{AVANZAMENTO_DIR}/{nome}.json")
    nuovo = finale + ".nuovo"
    try:
        os.unlink(nuovo)
    except OSError:
        pass
    fd = os.open(nuovo, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        os.write(fd, json.dumps({"formato": 1, "fase": fase, "testo": testo, "quando": int(time.time())},
                                ensure_ascii=False).encode("utf-8"))
        os.fchmod(fd, 0o644)
    finally:
        os.close(fd)
    os.replace(nuovo, finale)


def cmd_ponte(a) -> int:
    """Il comando forzato degli utenti dei ponti. Il Raspberry chiede:
         configurazione               la configurazione da installare (certificato, canale, scheda), se c'e'
         avanzamento FASE TESTO       a che punto e' l'installazione; con FASE "fatto" la configurazione si toglie
         certificato                  (1.13.0) il certificato nuovo caricato sulla pagina, se c'e'
         certificato-fatto            (1.13.0) installato: il certificato si toglie dal server
    Qualunque altra cosa: rifiutata. Il ponte e' quello dell'utente SSH, non quello che dice il Raspberry."""
    utente = os.environ.get("RETE_PROVA_UTENTE", "") if RADICE else ""
    if not utente:
        try:
            utente = pwd.getpwuid(os.getuid()).pw_name
        except KeyError:
            utente = ""
    nome = utente[len("ponte-"):] if utente.startswith("ponte-") else ""
    if not RE_NOME.match(nome):
        print("Questo comando e' solo per i gateway della rete Meshtastic.", file=sys.stderr)
        return 1
    parti = os.environ.get("SSH_ORIGINAL_COMMAND", "").split(None, 2)
    if not parti:
        print("Questo utente serve solo al tunnel del gateway: niente terminale.", file=sys.stderr)
        return 1
    try:
        if parti[0] == "configurazione" and len(parti) == 1:
            try:
                fd = os.open(file_configurazione(nome), os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                print("nessuna configurazione da installare per questo gateway", file=sys.stderr)
                return 3
            with os.fdopen(fd, "rb") as h:
                dati = h.read(1 << 20)
            sys.stdout.buffer.write(dati)
            sys.stdout.flush()
            scrivi_avanzamento(nome, "configurazione", "il Raspberry ha scaricato la configurazione")
            return 0
        if parti[0] == "avanzamento" and len(parti) >= 2 and RE_FASE.match(parti[1]):
            scrivi_avanzamento(nome, parti[1], parti[2] if len(parti) > 2 else "")
            if parti[1] == "fatto":
                try:
                    os.unlink(file_configurazione(nome))   # installato: certificato e canale non servono piu' qui
                except FileNotFoundError:
                    pass
            return 0
        if parti[0] == "certificato" and len(parti) == 1:
            try:
                fd = os.open(file_certificato_ponte(nome), os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                print("nessun certificato da installare per questo gateway", file=sys.stderr)
                return 3
            with os.fdopen(fd, "rb") as h:
                dati = h.read(1 << 20)
            sys.stdout.buffer.write(dati)
            sys.stdout.flush()
            return 0
        if parti[0] == "certificato-fatto" and len(parti) == 1:
            try:
                os.unlink(file_certificato_ponte(nome))      # installato: non serve piu' qui
            except FileNotFoundError:
                pass
            return 0
    except OSError as e:
        print(f"non riesco ({e.strerror or e})", file=sys.stderr)
        return 4
    print("comando non previsto", file=sys.stderr)
    return 2


