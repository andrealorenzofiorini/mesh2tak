# --------------------------------------------------------------------------
# Authentik
# --------------------------------------------------------------------------

class ErroreAK(Errore):
    def __init__(self, codice: int, messaggio: str):
        super().__init__(messaggio)
        self.codice = codice


class Authentik:
    def __init__(self, url: str, token: str):
        self.url = url
        self.token = token
        self._apri = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def chiama(self, metodo: str, percorso: str, corpo=None):
        dati = json.dumps(corpo).encode() if corpo is not None else None
        rq = urllib.request.Request(f"{self.url}/api/v3/{percorso}", data=dati, method=metodo, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
            "Accept": "application/json"})
        try:
            with self._apri.open(rq, timeout=30) as r:
                testo = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            testo = e.read(600).decode("utf-8", "replace")
            registra(f"Authentik {metodo} {percorso}: {e.code} {testo}")
            raise ErroreAK(e.code, f"Authentik risponde {e.code} a {metodo} {percorso}: {testo[:200]}")
        except (urllib.error.URLError, OSError) as e:
            raise Errore(f"Authentik non risponde su {self.url} ({e}): e' acceso?")
        registra(f"Authentik {metodo} {percorso}: ok")
        return json.loads(testo) if testo.strip() else None

    def tutti(self, percorso: str) -> List[dict]:
        sep = "&" if "?" in percorso else "?"
        risultati, pagina = [], 1
        while pagina < 50:
            d = self.chiama("GET", f"{percorso}{sep}page_size=100&page={pagina}") or {}
            risultati += d.get("results", [])
            if not (d.get("pagination") or {}).get("next"):
                break
            pagina += 1
        return risultati

    # ---- flussi, provider e applicazioni (stesso schema di infra-TAK per Node-RED) ----

    def flussi(self) -> Tuple[str, str]:
        aut = self.tutti("flows/instances/?designation=authorization&ordering=slug")
        inv = self.tutti("flows/instances/?designation=invalidation")
        a = next((f["pk"] for f in aut if "implicit" in f.get("slug", "")), aut[0]["pk"] if aut else None)
        i = next((f["pk"] for f in inv if "provider" not in f.get("slug", "")), inv[0]["pk"] if inv else None)
        if not a or not i:
            raise Errore("Authentik non ha i flussi di autorizzazione/uscita predefiniti")
        return a, i

    def provider(self, nome: str, esterno: str, dominio_cookie: str, flussi: Tuple[str, str]) -> int:
        dati = {"name": nome, "authorization_flow": flussi[0], "invalidation_flow": flussi[1],
                "external_host": esterno.rstrip("/"), "mode": "forward_single", "token_validity": "hours=24",
                "cookie_domain": dominio_cookie}
        esistenti = [p for p in self.tutti(f"providers/proxy/?search={urllib.parse.quote(nome)}")
                     if p.get("name") == nome]
        if esistenti:
            pk = esistenti[0]["pk"]
            self.chiama("PATCH", f"providers/proxy/{pk}/", {k: v for k, v in dati.items() if k != "name"})
            return pk
        return self.chiama("POST", "providers/proxy/", dati)["pk"]

    def applicazione(self, slug: str, nome: str, provider: int, avvio: str, descrizione: str,
                     icona: Optional[str] = None) -> dict:
        # policy_engine_mode non si tocca: se infra-TAK (W1) aggiunge l'MFA e mette 'all', resta cosi'
        dati = {"name": nome, "slug": slug, "provider": provider, "meta_launch_url": avvio,
                "open_in_new_tab": True, "meta_description": descrizione, "meta_publisher": "mesh2tak"}
        try:
            app = self.chiama("GET", f"core/applications/{slug}/")
        except ErroreAK as e:
            if e.codice != 404:
                raise
            app = self.chiama("POST", "core/applications/", dati)
        else:
            app = self.chiama("PATCH", f"core/applications/{slug}/", {k: v for k, v in dati.items() if k != "slug"})
        if icona and icona not in str(app.get("meta_icon") or "") + str(app.get("meta_icon_url") or ""):
            self._icona(slug, icona)
        return app

    def _icona(self, slug: str, url: str) -> None:
        # Authentik 2025.12+: meta_icon accetta un indirizzo; prima si passava da set_icon_url
        try:
            app = self.chiama("PATCH", f"core/applications/{slug}/", {"meta_icon": url})
            if url in str(app.get("meta_icon") or "") + str(app.get("meta_icon_url") or ""):
                return
        except ErroreAK:
            pass
        try:
            self.chiama("POST", f"core/applications/{slug}/set_icon_url/", {"url": url})
        except Errore as e:
            avviso(f"icona dell'applicazione non impostata ({e}): resta l'iniziale")

    def politica_admin(self) -> str:
        """La politica 'Allow authentik Admins' di infra-TAK (la crea come fa infra-TAK se manca)."""
        gruppi = [g for g in self.tutti(f"core/groups/?search={urllib.parse.quote(GRUPPO_ADMIN)}")
                  if g.get("name") == GRUPPO_ADMIN]
        if not gruppi:
            raise Errore(f"in Authentik manca il gruppo '{GRUPPO_ADMIN}'")
        for elenco in ("policies/all/", "policies/group_membership/"):
            try:
                for p in self.tutti(f"{elenco}?search={urllib.parse.quote(POLITICA_ADMIN)}"):
                    if p.get("name") == POLITICA_ADMIN:
                        return p["pk"]
            except ErroreAK as e:
                if e.codice != 404:
                    raise
        try:
            return self.chiama("POST", "policies/group_membership/", {"name": POLITICA_ADMIN,
                                                                      "group": gruppi[0]["pk"]})["pk"]
        except ErroreAK as e:
            if e.codice not in (404, 405):
                raise
        return self.chiama("POST", "policies/expression/", {
            "name": POLITICA_ADMIN,
            "expression": f'return ak_is_group_member(request.user, name="{GRUPPO_ADMIN}")'})["pk"]

    def solo_admin(self, app_pk: str, politica: str) -> None:
        legami = self.tutti(f"policies/bindings/?target={app_pk}")
        if any(str(b.get("policy")) == str(politica) for b in legami):
            return
        self.chiama("POST", "policies/bindings/", {"target": app_pk, "policy": politica, "order": 0,
                                                   "negate": False, "enabled": True, "timeout": 30})

    def _avamposto(self) -> Tuple[Optional[str], List[int]]:
        elenco = self.tutti("outposts/instances/?search=embedded")
        emb = next((o for o in elenco if "embed" in (o.get("name") or "").lower() or o.get("type") == "proxy"), None)
        if not emb:
            return None, []
        pieno = self.chiama("GET", f"outposts/instances/{emb['pk']}/")
        pks = []
        for p in pieno.get("providers") or []:
            pk = p if isinstance(p, int) else (p or {}).get("pk")
            if pk is not None:
                pks.append(pk)
        return emb["pk"], pks

    def avamposto_aggiungi(self, nuovi: List[int]) -> bool:
        pk, attuali = self._avamposto()
        if pk is None:
            raise Errore("non trovo l'avamposto integrato di Authentik (embedded outpost)")
        mancanti = [p for p in nuovi if p not in attuali]
        if not mancanti:
            return False
        self.chiama("PATCH", f"outposts/instances/{pk}/", {"providers": attuali + mancanti})
        return True

    def avamposto_togli(self, vecchi: List[int]) -> None:
        pk, attuali = self._avamposto()
        if pk is None:
            return
        restano = [p for p in attuali if p not in vecchi]
        if len(restano) != len(attuali):
            self.chiama("PATCH", f"outposts/instances/{pk}/", {"providers": restano})

    def elimina(self, slug: Optional[str], provider: Optional[int]) -> None:
        for percorso in ([f"core/applications/{slug}/"] if slug else []) + \
                        ([f"providers/proxy/{provider}/"] if provider else []):
            try:
                self.chiama("DELETE", percorso)
            except ErroreAK as e:
                if e.codice != 404:
                    raise


def riavvia_authentik() -> None:
    """L'avamposto integrato non legge da solo i provider nuovi: va riavviato authentik-server (come fa
    infra-TAK). Il login si ferma per circa un minuto."""
    r = esegui(["docker", "ps", "--filter", "name=authentik-server", "--format", "{{.Names}}"], controlla=False)
    nomi = [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
    if not nomi:
        avviso("contenitore authentik-server non trovato: riavvialo tu da infra-TAK (Authentik -> Restart)")
        return
    info(f"riavvio {nomi[0]} perche' legga gli indirizzi nuovi (il login si ferma per circa un minuto)...")
    esegui(["docker", "restart", nomi[0]], attesa=180)
    for _ in range(60):
        time.sleep(2)
        h = esegui(["docker", "inspect", "-f", "{{.State.Health.Status}}", nomi[0]], controlla=False).stdout.strip()
        if h == "healthy":
            ok("Authentik di nuovo pronto")
            return
        if not h:
            time.sleep(15)
            return
    avviso("Authentik non ancora pronto dopo 2 minuti: se le pagine danno 'Not Found', aspetta e ricarica")


def configura_authentik(imp: Impostazioni, reg: dict, rimossi: Optional[Dict[str, dict]] = None) -> None:
    ak = Authentik(AK_URL, token_authentik())
    flussi = ak.flussi()
    politica = ak.politica_admin()
    cookie = "." + imp.dominio
    voci = reg["authentik"]
    nuovi_provider = []
    pv = ak.provider(PROVIDER_RETE, imp.url(), cookie, flussi)
    app = ak.applicazione(APP_RETE, "Rete Meshtastic", pv, imp.url(),
                          "Stato dei gateway Meshtastic -> TAK e gestione dei nodi", icona=imp.url() + "icona.svg")
    ak.solo_admin(app["pk"], politica)
    voci["rete"] = {"provider": pv, "app": APP_RETE}
    nuovi_provider.append(pv)
    ok(f"applicazione 'Rete Meshtastic' in Authentik, solo per '{GRUPPO_ADMIN}'")
    for n in sorted(reg["ponti"]):
        slug = f"meshtastic-{n}"
        pv = ak.provider(f"Meshtastic {n}", imp.url(n), cookie, flussi)
        # nascosta nella pagina iniziale (blank://blank): ci si arriva dalla pagina della rete
        app = ak.applicazione(slug, f"Meshtastic {n}", pv, "blank://blank",
                              f"Gestione del nodo del gateway {n} (si apre dalla pagina Rete Meshtastic)")
        ak.solo_admin(app["pk"], politica)
        voci["ponti"][n] = {"provider": pv, "app": slug}
        nuovi_provider.append(pv)
    for n, v in (rimossi or {}).items():
        ak.avamposto_togli([v.get("provider")])
        ak.elimina(v.get("app"), v.get("provider"))
        voci["ponti"].pop(n, None)
        ok(f"tolto da Authentik: Meshtastic {n}")
    if ak.avamposto_aggiungi(nuovi_provider):
        riavvia_authentik()
    else:
        ok("avamposto di Authentik gia' a posto")


def togli_authentik(reg: dict) -> None:
    ak = Authentik(AK_URL, token_authentik())
    voci = list(reg["authentik"].get("ponti", {}).values())
    if reg["authentik"].get("rete"):
        voci.append(reg["authentik"]["rete"])
    ak.avamposto_togli([v.get("provider") for v in voci if v.get("provider")])
    for v in voci:
        ak.elimina(v.get("app"), v.get("provider"))
    reg["authentik"] = {"ponti": {}}
    ok("tolte da Authentik le applicazioni della rete Meshtastic")


