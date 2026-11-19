# --------------------------------------------------------------------------
# Impostazioni: di infra-TAK (di serie) o dello stack takit (dalla 1.21.0)
# --------------------------------------------------------------------------

class Impostazioni:
    """La rete accanto a infra-TAK: Caddy e Authentik di infra-TAK, un indirizzo per la pagina e uno per gateway."""
    tipo = INFRA_TAK
    pagine_dei_gateway = True       # meshtastic-NOME.dominio: la gestione del nodo di ogni gateway

    def __init__(self, dati: dict, percorso: str):
        self.dati = dati
        self.percorso = percorso
        self.dominio = str(dati.get("fqdn") or "").strip().lower().rstrip(".")
        self.server_ip = str(dati.get("server_ip") or "").strip()
        self.ssl = str(dati.get("ssl_mode") or "").strip()
        try:
            self.porta_https = int(dati.get("caddy_https_port") or 443)
        except (TypeError, ValueError):
            self.porta_https = 443
        if not re.match(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$", self.dominio) or "." not in self.dominio:
            raise Errore("infra-TAK non ha un dominio (modalita' FQDN): la rete Meshtastic ha bisogno di "
                         "indirizzi tipo meshtastic.tuodominio.it")
        remoto = dati.get("authentik_deployment") or {}
        if isinstance(remoto, dict) and remoto.get("target_mode") == "remote":
            raise Errore("Authentik gira su un altro server: questo script va bene solo con Authentik sullo "
                         "stesso server di Caddy")

    def host(self, nome: Optional[str] = None) -> str:
        return f"meshtastic-{nome}.{self.dominio}" if nome else f"meshtastic.{self.dominio}"

    def url(self, nome: Optional[str] = None) -> str:
        porta = "" if self.porta_https == 443 else f":{self.porta_https}"
        return f"https://{self.host(nome)}{porta}/"

    def catalogo_icone(self) -> str:
        """Il catalogo delle icone che infra-TAK usa per i suoi flussi (nodered/icon-catalog.json)."""
        if not self.percorso:
            return ""
        return os.path.join(os.path.dirname(os.path.dirname(self.percorso)), "nodered", "icon-catalog.json")


RE_DOMINIO_STACK = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
RE_PERCORSO_STACK = re.compile(r"^/[A-Za-z0-9._/-]{1,200}$")


def controlla_piattaforma(d: dict) -> dict:
    """La piattaforma dello stack takit, controllata: dominio, cartella dello stack e file del suo impianto."""
    dominio = str(d.get("dominio") or "").strip().lower().rstrip(".")
    if not RE_DOMINIO_STACK.match(dominio) or "." not in dominio or ".." in dominio:
        raise Errore("il dominio dello stack takit non e' valido (es. takit.org)")
    valori = {}
    for voce in ("radice", "impianto", "pagine", "icone"):
        v = str(d.get(voce) or "").rstrip("/") if voce == "radice" else str(d.get(voce) or "")
        if voce in ("pagine", "icone") and not v:
            continue                    # facoltativi: con uno stack di prima della alfa.21 niente pagine per gateway
                                        # e niente figure delle icone
        if not RE_PERCORSO_STACK.match(v) or any(x in ("", ".", "..") for x in v.split("/")[1:]):
            raise Errore(f"il percorso '{voce}' dello stack takit deve essere completo, es. /opt/takit-stack")
        valori[voce] = v
    piattaforma = {"formato": 1, "tipo": TAKIT_STACK, "dominio": dominio, "radice": valori["radice"],
                   "impianto": valori["impianto"]}
    for voce in ("pagine", "icone"):
        if voce in valori:
            piattaforma[voce] = valori[voce]
    return piattaforma


def leggi_piattaforma() -> Optional[dict]:
    """La piattaforma scelta all'installazione. None: accanto a infra-TAK, come fino alla 1.20.0."""
    testo = leggi(P(PIATTAFORMA))
    if testo is None:
        return None
    try:
        d = json.loads(testo)
    except ValueError:
        raise Errore(f"{PIATTAFORMA} e' rovinato: rilancia l'installazione dallo stack takit")
    if not isinstance(d, dict) or d.get("tipo") != TAKIT_STACK:
        raise Errore(f"{PIATTAFORMA}: piattaforma sconosciuta")
    return controlla_piattaforma(d)


class ImpostazioniStack:
    """La rete dentro lo stack takit (dalla 1.21.0). L'ingresso (Caddy) e il login (Authentik) li gestisce lo
    stack: pubblica la pagina della rete al suo indirizzo e la mette dietro al login dei suoi amministratori, leggendo
    la scheda del modulo "rete-radio". La pagina e' una sola: niente indirizzi per gateway (la gestione del nodo di
    un gateway resta dalla rete di casa sua). Gli avvisi partono con la posta dello stack.

    Dalla 1.22.0, se lo stack gliel'ha chiesto (--takit-pagine), anche un indirizzo per gateway: la 1.22.0 scrive
    l'elenco dei gateway in quel file e chiede alla console dello stack di rileggerlo; ingresso e login pubblicano
    https://meshtastic-NOME.dominio, solo per gli amministratori, verso il tunnel del gateway."""
    tipo = TAKIT_STACK

    def __init__(self, piattaforma: dict):
        self.piattaforma = piattaforma
        self.pagine_dei_gateway = bool(piattaforma.get("pagine"))
        self.dominio = piattaforma["dominio"]
        self.server_ip = ""        # il DNS si controlla solo che risponda: l'indirizzo del server non lo sappiamo
        self.ssl = ""
        self.porta_https = 443
        self.dati: dict = {}
        self.percorso = ""         # niente impostazioni di infra-TAK

    def host(self, nome: Optional[str] = None) -> str:
        return f"meshtastic-{nome}.{self.dominio}" if nome and self.pagine_dei_gateway else f"meshtastic.{self.dominio}"

    def catalogo_icone(self) -> str:
        """Dalla 1.22.0: lo stesso catalogo di infra-TAK, che lo stack scarica e controlla (--takit-icone). Senza, la
        pagina mostra i nomi delle icone."""
        return P(self.piattaforma["icone"]) if self.piattaforma.get("icone") else ""

    def url(self, nome: Optional[str] = None) -> str:
        return f"https://{self.host(nome)}/"

    def tak(self) -> str:
        return f"takserver.{self.dominio}"


def trova_impostazioni():
    piattaforma = leggi_piattaforma()
    if piattaforma is not None:
        return ImpostazioniStack(piattaforma)
    candidati = []
    unita = leggi(P("/etc/systemd/system/takwerx-console.service")) or ""
    for riga in unita.splitlines():
        if riga.strip().startswith("WorkingDirectory="):
            candidati.append(P(riga.split("=", 1)[1].strip()) + "/.config/settings.json")
    candidati += [P("/opt/infratak/.config/settings.json"), P("/root/infra-TAK/.config/settings.json")]
    candidati += sorted(glob.glob(P("/home/*/infra-TAK/.config/settings.json")))
    candidati += sorted(glob.glob(P("/opt/*/.config/settings.json")))
    for c in candidati:
        testo = leggi(c)
        if not testo:
            continue
        try:
            dati = json.loads(testo)
        except ValueError:
            continue
        if isinstance(dati, dict) and dati.get("fqdn"):
            return Impostazioni(dati, c)
    raise Errore("Non trovo la configurazione di infra-TAK (.config/settings.json): lancia lo script sul "
                 "server dove gira la console infra-TAK")


def token_authentik() -> str:
    candidati = [P("/root/authentik/.env"), P("/home/takwerx/authentik/.env")]
    candidati += sorted(glob.glob(P("/home/*/authentik/.env")))
    for c in candidati:
        testo = leggi(c) or ""
        valori = {}
        for riga in testo.splitlines():
            riga = riga.strip()
            if "=" in riga and not riga.startswith("#"):
                k, v = riga.split("=", 1)
                valori[k.strip()] = v.strip().strip("'\"")
        token = valori.get("AUTHENTIK_BOOTSTRAP_TOKEN") or valori.get("AUTHENTIK_TOKEN")
        if token:
            return token
    raise Errore("Non trovo il token di Authentik (AUTHENTIK_BOOTSTRAP_TOKEN in authentik/.env)")


