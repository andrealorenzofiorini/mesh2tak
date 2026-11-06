"""Authentik finto: le API usate da rete-meshtastic.sh (stesse forme di Authentik 2025-2026) e l'endpoint
forward_auth dell'avamposto integrato, che come quello vero conosce solo gli indirizzi dei provider
aggiunti all'avamposto e fa entrare solo chi ha la sessione (qui: cookie ak_finto=<utente>)."""
import http.server
import json
import threading
import urllib.parse
import uuid

TOKEN = "token-di-prova"


class AuthentikFinto:
    def __init__(self, porta, politica_esistente=False, group_membership_405=False, icona_vecchio_stile=False):
        self.porta = porta
        self.chiamate = []
        self.icona_vecchio_stile = icona_vecchio_stile
        self.group_membership_405 = group_membership_405
        self.flussi_aut = [{"pk": "f-esplicito", "slug": "default-provider-authorization-explicit-consent"},
                           {"pk": "f-implicito", "slug": "default-provider-authorization-implicit-consent"}]
        self.flussi_inv = [{"pk": "f-inv-provider", "slug": "default-provider-invalidation-flow"},
                           {"pk": "f-inv", "slug": "default-invalidation-flow"}]
        self.gruppi = [{"pk": "g-admin", "name": "authentik Admins"}, {"pk": "g-altri", "name": "authentik Read-only"}]
        self.provider = {7: {"pk": 7, "name": "TAK Portal Proxy", "external_host": "https://takportal.prova.lan",
                             "mode": "forward_single"}}
        self.app = {"tak-portal": {"pk": str(uuid.uuid4()), "slug": "tak-portal", "name": "TAK Portal", "provider": 7}}
        self.politiche = {}
        if politica_esistente:
            self.politiche["pol-esistente"] = {"pk": "pol-esistente", "name": "Allow authentik Admins"}
        self.legami = []
        self.avamposto = {"pk": "op-emb", "name": "authentik Embedded Outpost", "type": "proxy", "providers": [7]}
        self._lock = threading.Lock()
        finto = self

        class Gestore(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _manda(self, codice, dati=None, intestazioni=()):
                corpo = b"" if dati is None else json.dumps(dati).encode()
                self.send_response(codice)
                for k, v in intestazioni:
                    self.send_header(k, v)
                if dati is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(corpo)))
                self.end_headers()
                self.wfile.write(corpo)

            def _gestisci(self, metodo):
                lunghezza = int(self.headers.get("Content-Length") or 0)
                corpo = json.loads(self.rfile.read(lunghezza) or b"null") if lunghezza else None
                u = urllib.parse.urlsplit(self.path)
                if u.path.startswith("/outpost.goauthentik.io/"):
                    return finto._avamposto_http(self, u)
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self._manda(403, {"detail": "token"})
                with finto._lock:
                    finto.chiamate.append((metodo, u.path, dict(urllib.parse.parse_qsl(u.query)), corpo))
                    codice, dati = finto._api(metodo, u.path[len("/api/v3/"):], dict(urllib.parse.parse_qsl(u.query)),
                                              corpo)
                self._manda(codice, dati)

            def do_GET(self):  # noqa: N802
                self._gestisci("GET")

            def do_POST(self):  # noqa: N802
                self._gestisci("POST")

            def do_PATCH(self):  # noqa: N802
                self._gestisci("PATCH")

            def do_DELETE(self):  # noqa: N802
                self._gestisci("DELETE")

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", porta), Gestore)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def ferma(self):
        self.server.shutdown()
        self.server.server_close()

    # ---- avamposto (forward_auth di Caddy) ----

    def _avamposto_http(self, h, u):
        host = (h.headers.get("X-Forwarded-Host") or h.headers.get("Host") or "").split(":")[0]
        noti = [self.provider[p] for p in self.avamposto["providers"] if p in self.provider]
        if not any(urllib.parse.urlsplit(p["external_host"]).hostname == host for p in noti):
            return h._manda(404, {"detail": "Not Found"})
        utente = ""
        for pezzo in (h.headers.get("Cookie") or "").split(";"):
            k, _, v = pezzo.strip().partition("=")
            if k == "ak_finto":
                utente = v
        if not utente:
            return h._manda(302, None, [("Location", "https://tak.prova.lan/if/flow/default-authentication-flow/")])
        h._manda(200, None, [("X-Authentik-Username", utente), ("X-Authentik-Groups", "authentik Admins")])

    # ---- API ----

    @staticmethod
    def _pagina(risultati):
        return 200, {"pagination": {"next": 0, "count": len(risultati)}, "results": risultati}

    def _api(self, metodo, percorso, q, corpo):
        parti = [p for p in percorso.split("/") if p]
        cerca = (q.get("search") or "").lower()
        if parti[:2] == ["flows", "instances"]:
            return self._pagina(self.flussi_aut if q.get("designation") == "authorization" else self.flussi_inv)
        if parti[:2] == ["core", "groups"]:
            return self._pagina([g for g in self.gruppi if cerca in g["name"].lower()])
        if parti[:2] == ["providers", "proxy"]:
            if len(parti) == 2 and metodo == "GET":
                return self._pagina([p for p in self.provider.values() if cerca in p["name"].lower()])
            if len(parti) == 2 and metodo == "POST":
                mancano = [k for k in ("name", "authorization_flow", "invalidation_flow", "external_host", "mode")
                           if not corpo.get(k)]
                if mancano or any(p["name"] == corpo["name"] for p in self.provider.values()):
                    return 400, {"name": ["errore"], "mancano": mancano}
                pk = max(self.provider) + 1
                self.provider[pk] = dict(corpo, pk=pk)
                return 201, self.provider[pk]
            pk = int(parti[2])
            if pk not in self.provider:
                return 404, {"detail": "Not found."}
            if metodo == "PATCH":
                self.provider[pk].update(corpo)
                return 200, self.provider[pk]
            if metodo == "DELETE":
                del self.provider[pk]
                return 204, None
            return 200, self.provider[pk]
        if parti[:2] == ["core", "applications"]:
            if len(parti) == 2 and metodo == "POST":
                if not corpo.get("slug") or corpo["slug"] in self.app:
                    return 400, {"slug": ["esiste"]}
                a = dict(corpo, pk=str(uuid.uuid4()), meta_icon=None)
                self.app[corpo["slug"]] = a
                return 201, a
            if len(parti) == 2:
                return self._pagina(list(self.app.values()))
            slug = parti[2]
            if slug not in self.app:
                return 404, {"detail": "Not found."}
            a = self.app[slug]
            if len(parti) == 4 and parti[3] == "set_icon_url" and metodo == "POST":
                a["meta_icon"] = corpo["url"]
                return 200, None
            if metodo == "PATCH":
                corpo = dict(corpo)
                if self.icona_vecchio_stile:
                    corpo.pop("meta_icon", None)       # nelle versioni vecchie il campo e' in sola lettura
                a.update(corpo)
                return 200, a
            if metodo == "DELETE":
                del self.app[slug]
                self.legami = [b for b in self.legami if b["target"] != a["pk"]]
                return 204, None
            return 200, a
        if parti[0] == "policies":
            if parti[1] in ("all", "group_membership", "expression") and metodo == "GET":
                return self._pagina([p for p in self.politiche.values() if cerca in p["name"].lower()])
            if parti[1] == "group_membership" and metodo == "POST":
                if self.group_membership_405:
                    return 405, {"detail": "Method not allowed"}
                pk = "pol-" + uuid.uuid4().hex[:6]
                self.politiche[pk] = dict(corpo, pk=pk)
                return 201, self.politiche[pk]
            if parti[1] == "expression" and metodo == "POST":
                pk = "pol-" + uuid.uuid4().hex[:6]
                self.politiche[pk] = dict(corpo, pk=pk)
                return 201, self.politiche[pk]
            if parti[1] == "bindings" and metodo == "GET":
                return self._pagina([b for b in self.legami if b["target"] == q.get("target")])
            if parti[1] == "bindings" and metodo == "POST":
                self.legami.append(dict(corpo))
                return 201, corpo
        if parti[:2] == ["outposts", "instances"]:
            if len(parti) == 2:
                return self._pagina([{k: v for k, v in self.avamposto.items() if k != "providers"}])
            if metodo == "PATCH":
                self.avamposto["providers"] = list(corpo["providers"])
            return 200, self.avamposto
        return 404, {"detail": f"percorso non previsto: {percorso}"}

    # ---- comodita' per le prove ----

    def app_di(self, slug):
        return self.app.get(slug)

    def solo_admin(self, slug):
        a = self.app[slug]
        pol = {b["policy"] for b in self.legami if b["target"] == a["pk"]}
        return any(self.politiche.get(p, {}).get("name") == "Allow authentik Admins" for p in pol)
