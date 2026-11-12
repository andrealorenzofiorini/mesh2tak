# --------------------------------------------------------------------------
# Caddy
# --------------------------------------------------------------------------

def riga_tls(imp: Impostazioni, nomi: List[str]) -> str:
    """Con il certificato proprio di infra-TAK (ssl_mode custom) i siti nuovi usano lo stesso."""
    if imp.ssl != "custom":
        return ""
    testo = (leggi(P(CADDYFILE)) or "").split(MARCATORE_INFRATAK, 1)[0]
    m = re.search(r"^\s*tls\s+(\S+)\s+(\S+)\s*$", testo, re.M)
    if not m:
        raise Errore("infra-TAK usa un certificato proprio ma non lo trovo nel Caddyfile")
    cert = m.group(1)
    try:
        import ssl
        dati = ssl._ssl._test_decode_cert(P(cert))  # noqa: SLF001 (legge i nomi del certificato)
        coperti = [v.lower() for k, v in dati.get("subjectAltName", ()) if k == "DNS"]
    except Exception:  # noqa
        coperti = []
    for n in nomi:
        if coperti and not any(c == n or (c.startswith("*.") and n.count(".") == c.count(".")
                                          and n.endswith(c[1:])) for c in coperti):
            raise Errore(f"il certificato di infra-TAK non copre {n} (copre: {', '.join(coperti)}): serve un "
                         f"certificato *.{imp.dominio} oppure Let's Encrypt")
    return f"\ttls {m.group(1)} {m.group(2)}"


def testo_caddy(imp: Impostazioni, reg: dict) -> str:
    nomi = [imp.host()] + [imp.host(n) for n in sorted(reg["ponti"])]
    tls = riga_tls(imp, nomi)
    togli_ak = [f"\t\trequest_header -{h}" for h in INTESTAZIONI_AK]
    auth = [f"\t\treverse_proxy /outpost.goauthentik.io/* {AK_UPSTREAM}",
            f"\t\tforward_auth {AK_UPSTREAM} {{",
            "\t\t\turi /outpost.goauthentik.io/auth/caddy",
            "\t\t\tcopy_headers X-Authentik-Username",
            "\t\t\ttrusted_proxies private_ranges",
            "\t\t}"]
    r = ["# Generato da rete-meshtastic.sh (mesh2tak): non modificarlo a mano, rilancia lo script.",
         f"# Pagina della rete: {imp.url()}  -  un indirizzo per gateway: {imp.url('NOME')}", ""]
    r += [f"{imp.host()} {{"] + ([tls] if tls else []) + ["\troute {"] + togli_ak
    senza_cookie = ["\t\t\t\theader_up -Cookie", "\t\t\t\theader_up -Authorization"]
    r += ["\t\t# pubbliche: richieste dei Raspberry, loro registrazioni (nome = impronta della chiave) e icona",
          "\t\thandle /accesso/richiesta {",
          "\t\t\trequest_body {",
          "\t\t\t\tmax_size 16KB",
          "\t\t\t}",
          f"\t\t\treverse_proxy 127.0.0.1:{PORTA_WEB} {{"] + senza_cookie + [
          "\t\t\t}",
          "\t\t}",
          "\t\thandle /accesso/* {",
          f"\t\t\troot * {P(WEB_DIR)}",
          "\t\t\theader Cache-Control no-store",
          "\t\t\tfile_server",
          "\t\t}",
          "\t\thandle /icona.svg {",
          f"\t\t\troot * {P(SITO_DIR)}",
          "\t\t\theader Cache-Control \"public, max-age=86400\"",
          "\t\t\tfile_server",
          "\t\t}"]
    r += auth
    r += ["\t\t# certificato e canale per i gateway nuovi (dalla 1.10.0) e certificato nuovo di un gateway (1.13.0): il",
          "\t\t# pacchetto della console e' piu' grande",
          "\t\t@certificati path /api/impostazioni-nuovi /api/certificato-ponte",
          "\t\thandle @certificati {",
          "\t\t\trequest_body {",
          "\t\t\t\tmax_size 400KB",
          "\t\t\t}",
          f"\t\t\treverse_proxy 127.0.0.1:{PORTA_WEB} {{"] + senza_cookie + [
          "\t\t\t}",
          "\t\t}"]
    r += ["\t\thandle /api/* {",
          "\t\t\trequest_body {",
          "\t\t\t\tmax_size 16KB",
          "\t\t\t}",
          f"\t\t\treverse_proxy 127.0.0.1:{PORTA_WEB} {{"] + senza_cookie + [
          "\t\t\t}",
          "\t\t}"]
    for n, p in sorted(reg["ponti"].items()):
        r += [f"\t\thandle /stato/{n}.json {{",
              "\t\t\trewrite * /mesh2tak/stato.json",
              f"\t\t\treverse_proxy https://127.0.0.1:{p['porta']} {{"] + senza_cookie + [
              "\t\t\t\ttransport http {",
              "\t\t\t\t\ttls_insecure_skip_verify",
              "\t\t\t\t\tdial_timeout 5s",
              "\t\t\t\t\tresponse_header_timeout 15s",
              "\t\t\t\t}",
              "\t\t\t}",
              "\t\t}",
              # dalla 1.10.0: le radio della squadra gestite attraverso il nodo del ponte (solo amministratori:
              # il ponte vuole il nome messo da Authentik e risponde solo a quello che arriva dal tunnel)
              f"\t\thandle_path /radio/{n}/* {{",
              "\t\t\trequest_body {",
              "\t\t\t\tmax_size 16KB",
              "\t\t\t}",
              "\t\t\trewrite * /mesh2tak/radio{uri}",
              f"\t\t\treverse_proxy https://127.0.0.1:{p['porta']} {{"] + senza_cookie + [
              "\t\t\t\theader_up X-Forwarded-Host {hostport}",
              "\t\t\t\ttransport http {",
              "\t\t\t\t\ttls_insecure_skip_verify",
              "\t\t\t\t\tdial_timeout 5s",
              "\t\t\t\t\tresponse_header_timeout 15s",
              "\t\t\t\t}",
              "\t\t\t}",
              "\t\t}",
              # dalla 1.13.0: il registro raccolto dal Raspberry (solo amministratori, come sopra)
              f"\t\thandle /registro/{n}.txt {{",
              "\t\t\trequest_body {",
              "\t\t\t\tmax_size 1KB",
              "\t\t\t}",
              "\t\t\trewrite * /mesh2tak/registro",
              f"\t\t\treverse_proxy https://127.0.0.1:{p['porta']} {{"] + senza_cookie + [
              "\t\t\t\ttransport http {",
              "\t\t\t\t\ttls_insecure_skip_verify",
              "\t\t\t\t\tdial_timeout 5s",
              "\t\t\t\t\tresponse_header_timeout 30s",
              "\t\t\t\t}",
              "\t\t\t}",
              "\t\t}",
              # dalla 1.14.0: la mappa della copertura (lettura e "Azzera", solo amministratori)
              f"\t\thandle /copertura/{n}.json {{",
              "\t\t\trequest_body {",
              "\t\t\t\tmax_size 1KB",
              "\t\t\t}",
              "\t\t\trewrite * /mesh2tak/copertura.json",
              f"\t\t\treverse_proxy https://127.0.0.1:{p['porta']} {{"] + senza_cookie + [
              "\t\t\t\theader_up X-Forwarded-Host {hostport}",
              "\t\t\t\ttransport http {",
              "\t\t\t\t\ttls_insecure_skip_verify",
              "\t\t\t\t\tdial_timeout 5s",
              "\t\t\t\t\tresponse_header_timeout 30s",
              "\t\t\t\t}",
              "\t\t\t}",
              "\t\t}",
              # dalla 1.16.0: l'andamento di gateway e ripetitori (sola lettura, solo amministratori)
              f"\t\thandle /storico/{n}.json {{",
              "\t\t\trewrite * /mesh2tak/storico.json",
              f"\t\t\treverse_proxy https://127.0.0.1:{p['porta']} {{"] + senza_cookie + [
              "\t\t\t\ttransport http {",
              "\t\t\t\t\ttls_insecure_skip_verify",
              "\t\t\t\t\tdial_timeout 5s",
              "\t\t\t\t\tresponse_header_timeout 30s",
              "\t\t\t\t}",
              "\t\t\t}",
              "\t\t}"]
    r += ["\t\thandle /stato/* {",
          "\t\t\trespond 404",
          "\t\t}",
          "\t\thandle /radio/* {",
          "\t\t\trespond 404",
          "\t\t}",
          "\t\thandle /registro/* {",
          "\t\t\trespond 404",
          "\t\t}",
          "\t\thandle /copertura/* {",
          "\t\t\trespond 404",
          "\t\t}",
          "\t\thandle /storico/* {",
          "\t\t\trespond 404",
          "\t\t}",
          "\t\theader Cache-Control no-store",
          "\t\theader X-Content-Type-Options nosniff",
          "\t\theader Referrer-Policy no-referrer",
          "\t\theader Content-Security-Policy \"default-src 'self'; script-src 'self' 'unsafe-inline'; "
          # dalla 1.14.0 le mappe di sfondo della scheda Copertura (OpenTopoMap e OpenStreetMap)
          "style-src 'self' 'unsafe-inline'; img-src 'self' data: https://*.tile.opentopomap.org "
          "https://tile.openstreetmap.org; connect-src 'self'; frame-ancestors 'none'; "
          "base-uri 'none'; form-action 'none'\"",
          f"\t\troot * {P(SITO_DIR)}",
          "\t\tfile_server",
          "\t}",
          "}",
          ""]
    for n, p in sorted(reg["ponti"].items()):
        r += [f"# Gestione del nodo del gateway {n} (tunnel SSH dal Raspberry alla porta {p['porta']})",
              f"{imp.host(n)} {{"] + ([tls] if tls else []) + ["\troute {"] + togli_ak + auth
        # il Raspberry non riceve i cookie del browser: con la sessione di Authentik (valida per tutto il
        # dominio) un Raspberry rubato potrebbe entrare nella console di infra-TAK
        r += [f"\t\treverse_proxy https://127.0.0.1:{p['porta']} {{",
              "\t\t\theader_up -Cookie",
              "\t\t\theader_up -Authorization",
              "\t\t\ttransport http {",
              "\t\t\t\ttls_insecure_skip_verify",
              "\t\t\t\tdial_timeout 5s",
              "\t\t\t}",
              "\t\t}",
              "\t}",
              "\thandle_errors {",
              "\t\t@giu expression `{err.status_code} in [502, 503, 504]`",
              "\t\theader @giu Content-Type \"text/plain; charset=utf-8\"",
              f"\t\trespond @giu \"Il gateway {n} non risponde: Raspberry spento, senza internet o tunnel in "
              f"riconnessione. Riprova tra un minuto. Stato di tutti i gateway: {imp.url()}\" 502",
              "\t}",
              "}",
              ""]
    return "\n".join(r)


def blocco_import() -> str:
    return f"{INIZIO_BLOCCO}\nimport {P(CADDY_DIR)}/*.caddy\n{FINE_BLOCCO}\n"


def caddyfile_con_import(testo: str, con: bool) -> str:
    """Il Caddyfile di infra-TAK con (o senza) le nostre tre righe, sotto il suo marcatore: infra-TAK
    conserva tutto quello che sta sotto quando rigenera il file."""
    schema = re.compile(re.escape(INIZIO_BLOCCO) + r".*?" + re.escape(FINE_BLOCCO) + r"\n?", re.S)
    senza = schema.sub("", testo)
    if not con:
        return re.sub(r"\n{3,}$", "\n\n", senza)
    if MARCATORE_INFRATAK not in senza:
        senza = senza.rstrip("\n") + "\n\n" + MARCATORE_INFRATAK + "\n"
    return senza.rstrip("\n") + "\n\n" + blocco_import()


def ambiente_caddy() -> dict:
    """L'ambiente del servizio caddy (credenziali ACME di infra-TAK): senza, caddy validate sbaglia."""
    ambiente = dict(os.environ)
    r = esegui(["systemctl", "show", "caddy", "--property=Environment", "--value"], controlla=False, attesa=20)
    try:
        for voce in shlex.split(r.stdout or ""):
            if "=" in voce:
                k, v = voce.split("=", 1)
                ambiente[k] = v
    except ValueError:
        pass
    return ambiente


def applica_caddy(nuovi: Dict[str, Optional[str]]) -> None:
    """Scrive i file (None = toglie), li fa controllare a Caddy e lo ricarica; se qualcosa non va rimette
    tutto com'era: Caddy continua a servire la configurazione di prima."""
    prima = {p: leggi(p) for p in nuovi}
    if all(prima[p] == t for p, t in nuovi.items()):
        ok("Caddy gia' a posto")
        return

    def rimetti():
        for p, t in prima.items():
            if t is None:
                togli(p)
            else:
                scrivi(p, t, 0o644)

    for p, t in nuovi.items():
        if t is None:
            togli(p)
        else:
            scrivi(p, t, 0o644)
    r = esegui(["caddy", "validate", "--config", P(CADDYFILE), "--adapter", "caddyfile"], controlla=False,
               ambiente=ambiente_caddy(), attesa=90)
    if r.returncode != 0:
        rimetti()
        uscita = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
        errori = [x for x in uscita if "error" in x.lower()] or uscita
        raise Errore("Caddy rifiuta la configurazione, nessuna modifica applicata: " + " | ".join(errori[-3:])[-500:])
    r = esegui(["systemctl", "reload", "caddy"], controlla=False, attesa=90)
    if r.returncode != 0:
        rimetti()
        esegui(["systemctl", "reload", "caddy"], controlla=False, attesa=90)
        raise Errore("Caddy non ha accettato la configurazione nuova: rimessa quella di prima. Dettagli in "
                     f"{REGISTRO_LOG}")
    ok("Caddy aggiornato e ricaricato (certificati Let's Encrypt in automatico)")


def configura_caddy(imp: Impostazioni, reg: dict, con: bool = True) -> None:
    caddyfile = leggi(P(CADDYFILE))
    if caddyfile is None:
        raise Errore(f"non trovo {CADDYFILE}: Caddy di infra-TAK non e' installato?")
    os.makedirs(P(CADDY_DIR), exist_ok=True)
    os.chmod(P(CADDY_DIR), 0o755)
    applica_caddy({P(CADDYFILE): caddyfile_con_import(caddyfile, con),
                   P(CADDY_FILE): testo_caddy(imp, reg) if con else None})


