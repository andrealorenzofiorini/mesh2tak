# --------------------------------------------------------------------------
# Comandi
# --------------------------------------------------------------------------

class Blocco:
    """Una sola esecuzione alla volta (i lavori della pagina aspettano il loro turno)."""

    def __init__(self, attendi: bool = False):
        self.attendi = attendi

    def __enter__(self):
        os.makedirs(P(CONF_DIR), exist_ok=True)
        self.f = open(P(CONF_DIR + "/.blocco"), "w")
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | (0 if self.attendi else fcntl.LOCK_NB))
        except OSError:
            raise Errore("lo script sta gia' lavorando (da un'altra finestra o dalla pagina della rete): "
                         "aspetta che finisca")
        return self

    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def conferma(domanda: str, risposta: str, si: bool) -> None:
    if si:
        return
    if not sys.stdin.isatty():
        raise Errore("serve una conferma: rilancia aggiungendo --si")
    if input(f"    {domanda} Scrivi {risposta} per confermare: ").strip() != risposta:
        raise Errore("annullato, non ho toccato nulla")


def prepara_ponti(imp: Impostazioni, reg: dict, nomi: List[str]) -> None:
    """sshd, utenti e chiavi dei ponti indicati. Le registrazioni si pubblicano alla fine, quando Caddy e
    Authentik sono pronti: prima il Raspberry non bussa."""
    prepara_sshd()
    for n in nomi:
        p = reg["ponti"][n]
        utente = prepara_utente(n)
        riga = riga_autorizzata(p["chiave"], p["porta"])
        prima = leggi(P(f"{CHIAVI_DIR}/{utente}"))
        cambiata = scrivi(P(f"{CHIAVI_DIR}/{utente}"), riga, 0o644)
        for vecchia in p.pop("impronte_vecchie", []):
            togli(P(f"{ACCESSO_DIR}/{vecchia}.json"))   # il Raspberry con la chiave vecchia non trova piu' nulla
        if cambiata and not stessa_chiave_e_porta(prima, riga):
            chiudi_sessioni(utente)   # chiave o porta cambiate: il tunnel si riapre con quelle nuove
    ok(f"utenti dei gateway pronti: {', '.join(utente_ponte(n) for n in nomi) or 'nessuno'} (solo tunnel)")


def pubblica_registrazioni(imp: Impostazioni, reg: dict, nomi: List[str]) -> None:
    """Le registrazioni che i Raspberry leggono: utente, porta, chiavi del server."""
    porta_sshd = porta_ssh()
    chiavi = chiavi_host()
    for n in nomi:
        scrivi_registrazione(imp, n, reg["ponti"][n], porta_sshd, chiavi)
    if nomi:
        ok(f"registrazioni pubblicate (SSH porta {porta_sshd}): i Raspberry aprono il tunnel da soli")


def valida_nome(nome: str) -> str:
    nome = (nome or "").strip().lower()
    if not RE_NOME.match(nome):
        raise Errore("nome del gateway: solo minuscole, numeri e trattini, al massimo 20 caratteri (es. casa)")
    return nome


