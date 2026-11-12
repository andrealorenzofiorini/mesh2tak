# --------------------------------------------------------------------------
# Impostazioni di infra-TAK
# --------------------------------------------------------------------------

class Impostazioni:
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


def trova_impostazioni() -> Impostazioni:
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


