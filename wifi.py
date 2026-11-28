#!/usr/bin/env python3
"""mesh2tak-wifi: se il Raspberry resta senza rete, apre un suo Wi-Fi da cui scegliere a quale Wi-Fi collegarlo.

Come lavora (servizio di sistema, da root, solo librerie standard di Python e NetworkManager):
  1. finche' il Raspberry e' collegato (cavo o Wi-Fi) non fa niente;
  2. se resta senza rete per qualche minuto apre il Wi-Fi "Gateway-NOME", con la sua password;
  3. chi si collega con il telefono trova una pagina con le reti viste dal Raspberry: sceglie, scrive la password;
  4. il Raspberry chiude il suo Wi-Fi e prova quella rete; se non riesce lo riapre e la pagina dice perche'.
Mentre il suo Wi-Fi e' aperto, ogni qualche minuto (se nessuno lo sta usando) lo chiude un momento per riprovare le
reti che conosce gia': un router rimasto spento per un po' viene ritrovato da solo.

Da questa pagina si sceglie solo il Wi-Fi: niente certificato, niente canale, niente radio.

Uso:
  sudo mesh2tak-wifi            nome e password del Wi-Fi del gateway, e com'e' collegato adesso
  sudo mesh2tak-wifi servizio   (lo lancia systemd)
"""
import hmac
import html
import http.server
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from typing import Dict, List, Optional, Tuple

VERSIONE = "@@VERSIONE@@"
RADICE = os.environ.get("MESH2TAK_WIFI_RADICE", "")              # le prove usano un sistema finto
ATTESE = float(os.environ.get("MESH2TAK_WIFI_ATTESE") or 1)       # (nelle prove: piu' brevi)
PORTA = int(os.environ.get("MESH2TAK_WIFI_PORTA") or 80)
CONF = RADICE + "/etc/mesh2tak/wifi-configura.conf"               # nome e password del Wi-Fi del gateway (solo root)
STATO = RADICE + "/var/lib/mesh2tak-wifi/stato.json"
RETI_NM = RADICE + "/etc/NetworkManager/system-connections"
DNS_NM = RADICE + "/etc/NetworkManager/dnsmasq-shared.d/mesh2tak-configura.conf"
PROFILO = "mesh2tak-configura"                                    # il Wi-Fi del gateway, in NetworkManager
PREFISSO_RETI = "mesh2tak-wifi-"                                  # le reti scelte dalla pagina
INDIRIZZO = "10.42.0.1"
SENZA_RETE_S = 150 * ATTESE          # senza rete da cosi' tanto: si apre il Wi-Fi del gateway
GUARDA_OGNI_S = 20 * ATTESE
RIPROVA_NOTE_OGNI_S = 300 * ATTESE   # con il Wi-Fi del gateway aperto e nessuno collegato: si riprovano le reti note
FERMO_DA_S = 180 * ATTESE            # "nessuno lo sta usando" = nessuna richiesta alla pagina da cosi' tanto
PROVA_RETE_S = 60                    # quanto si aspetta una rete appena scelta
TIPI_DI_RETE = ("802-3-ethernet", "802-11-wireless", "gsm", "ethernet", "wifi")


def registra(testo: str) -> None:
    print(testo, flush=True)


def esegui(comando: List[str], attesa: float = 60) -> Tuple[int, str]:
    try:
        r = subprocess.run(comando, capture_output=True, text=True, timeout=attesa, stdin=subprocess.DEVNULL)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def campi(riga: str) -> List[str]:
    """Una riga di 'nmcli -t': campi separati da ':', con '\\:' e '\\\\' dentro i valori."""
    esce, corrente, i = [], "", 0
    while i < len(riga):
        c = riga[i]
        if c == "\\" and i + 1 < len(riga):
            corrente += riga[i + 1]
            i += 2
            continue
        if c == ":":
            esce.append(corrente)
            corrente = ""
        else:
            corrente += c
        i += 1
    esce.append(corrente)
    return esce


def leggi_conf() -> Optional[Dict[str, str]]:
    try:
        with open(CONF, encoding="utf-8") as f:
            d = dict(r.strip().split("=", 1) for r in f if "=" in r and not r.lstrip().startswith("#"))
    except (OSError, ValueError):
        return None
    nome, password = d.get("nome", "").strip(), d.get("password", "").strip()
    if not re.match(r"^[A-Za-z0-9 _-]{1,32}$", nome) or not re.match(r"^[A-Za-z0-9-]{8,63}$", password):
        return None
    return {"nome": nome, "password": password}


def scheda_wifi() -> str:
    codice, uscita = esegui(["nmcli", "-t", "-f", "DEVICE,TYPE", "device"])
    for riga in uscita.splitlines() if codice == 0 else []:
        c = campi(riga)
        if len(c) >= 2 and c[1] == "wifi" and re.match(r"^[A-Za-z0-9_.-]{1,15}$", c[0]):
            return c[0]
    return ""


def collegamenti() -> List[Tuple[str, str]]:
    """(nome, tipo) dei collegamenti attivi che portano in rete, senza il Wi-Fi del gateway."""
    codice, uscita = esegui(["nmcli", "-t", "-f", "NAME,TYPE,STATE", "connection", "show", "--active"])
    esce = []
    for riga in uscita.splitlines() if codice == 0 else []:
        c = campi(riga)
        if len(c) >= 3 and c[0] != PROFILO and c[1] in TIPI_DI_RETE and c[2] == "activated":
            esce.append((c[0], "cavo" if "ethernet" in c[1] else "Wi-Fi" if "wireless" in c[1] or c[1] == "wifi"
                         else c[1]))
    return esce


def reti_viste(scheda: str) -> List[dict]:
    codice, uscita = esegui(["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list", "ifname", scheda,
                             "--rescan", "yes"], attesa=45)
    viste: Dict[str, dict] = {}
    for riga in uscita.splitlines() if codice == 0 else []:
        c = campi(riga)
        if len(c) < 3 or not c[0] or len(c[0].encode("utf-8")) > 32 or any(ord(x) < 32 for x in c[0]):
            continue
        try:
            segnale = max(0, min(100, int(c[1])))
        except ValueError:
            segnale = 0
        if c[0] not in viste or segnale > viste[c[0]]["segnale"]:
            viste[c[0]] = {"nome": c[0], "segnale": segnale, "protezione": c[2].strip()}
    return sorted(viste.values(), key=lambda r: -r["segnale"])[:30]


def reti_note() -> List[Tuple[str, str]]:
    """(profilo, nome della rete) dei Wi-Fi che il Raspberry conosce gia' e a cui si collega da solo."""
    codice, uscita = esegui(["nmcli", "-t", "-f", "NAME,TYPE,AUTOCONNECT", "connection", "show"])
    esce = []
    for riga in uscita.splitlines() if codice == 0 else []:
        c = campi(riga)
        if len(c) >= 3 and c[0] != PROFILO and c[1] == "802-11-wireless" and c[2] == "yes":
            codice_r, rete = esegui(["nmcli", "-g", "802-11-wireless.ssid", "connection", "show", "id", c[0]])
            if codice_r == 0 and rete.strip():
                esce.append((c[0], campi(rete.strip().splitlines()[0])[0]))
    return esce[:20]


def valore_keyfile(testo: str) -> str:
    """Un valore di testo nei file di NetworkManager: '\\' raddoppiato e spazio iniziale protetto."""
    v = testo.replace("\\", "\\\\")
    return "\\s" + v[1:] if v.startswith(" ") else v


def scrivi_segreto(percorso: str, testo: str) -> None:
    os.makedirs(os.path.dirname(percorso), exist_ok=True)
    provvisorio = percorso + ".nuovo"
    with open(os.open(provvisorio, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        f.write(testo)
    os.replace(provvisorio, percorso)


def profilo_del_gateway(scheda: str, conf: Dict[str, str]) -> str:
    return (f"[connection]\nid={PROFILO}\nuuid={uuid.uuid5(uuid.NAMESPACE_DNS, PROFILO)}\ntype=wifi\n"
            f"autoconnect=false\ninterface-name={scheda}\n\n"
            f"[wifi]\nmode=ap\nband=bg\nssid={valore_keyfile(conf['nome'])}\n\n"
            f"[wifi-security]\nkey-mgmt=wpa-psk\npsk={valore_keyfile(conf['password'])}\n\n"
            f"[ipv4]\nmethod=shared\naddress1={INDIRIZZO}/24\n\n[ipv6]\nmethod=disabled\n")


def profilo_della_rete(scheda: str, nome: str, password: str, protezione: str) -> Tuple[str, str]:
    """(nome del profilo, testo del file) per una rete scelta dalla pagina."""
    identita = PREFISSO_RETI + uuid.uuid5(uuid.NAMESPACE_DNS, nome).hex[:12]
    sicurezza = ""
    if password:
        solo_wpa3 = "WPA3" in protezione and "WPA2" not in protezione and "WPA1" not in protezione
        sicurezza = f"[wifi-security]\nkey-mgmt={'sae' if solo_wpa3 else 'wpa-psk'}\npsk={valore_keyfile(password)}\n\n"
    return identita, (f"[connection]\nid={identita}\nuuid={uuid.uuid5(uuid.NAMESPACE_DNS, identita)}\ntype=wifi\n"
                      f"autoconnect=true\ninterface-name={scheda}\n\n"
                      f"[wifi]\nmode=infrastructure\nssid={';'.join(str(b) for b in nome.encode('utf-8'))};\n\n"
                      f"{sicurezza}[ipv4]\nmethod=auto\n\n[ipv6]\nmethod=auto\n")


def difetto_della_richiesta(nome: str, password: str, protezione: str) -> str:
    if not nome or len(nome.encode("utf-8")) > 32 or any(ord(c) < 32 for c in nome):
        return "Il nome della rete non va bene (al massimo 32 caratteri)."
    if "802.1X" in protezione or "EAP" in protezione:
        return "Questa rete chiede utente e password (rete aziendale): da qui non si puo' usare."
    if "WEP" in protezione and "WPA" not in protezione:
        return "Questa rete usa una protezione vecchia (WEP) che il Raspberry non usa."
    if password and not 8 <= len(password) <= 63:
        return "La password di un Wi-Fi ha da 8 a 63 caratteri."
    if any(ord(c) < 32 or ord(c) > 126 for c in password):
        return "Nella password ci sono caratteri che non posso usare (solo lettere, numeri e segni comuni)."
    return ""


class Gestore:
    def __init__(self) -> None:
        self.scheda = ""
        self.conf: Optional[Dict[str, str]] = None
        self.aperto = False
        self.reti: List[dict] = []
        self.ultima_richiesta = 0.0
        self.messaggio = ""                 # esito dell'ultima prova, mostrato sulla pagina
        self.da_provare: Optional[Tuple[str, str, str]] = None
        self.lucchetto = threading.Lock()
        self.gettone = secrets.token_urlsafe(16)
        self.server: Optional[http.server.ThreadingHTTPServer] = None

    # ---- stato per 'sudo mesh2tak-wifi' ----

    def salva_stato(self, cosa: str) -> None:
        try:
            os.makedirs(os.path.dirname(STATO), exist_ok=True)
            with open(STATO + ".nuovo", "w", encoding="utf-8") as f:
                json.dump({"stato": cosa, "quando": int(time.time()), "messaggio": self.messaggio}, f)
            os.replace(STATO + ".nuovo", STATO)
        except OSError:
            pass

    # ---- il Wi-Fi del gateway ----

    def apri(self) -> bool:
        assert self.conf
        self.reti = reti_viste(self.scheda)          # prima di aprire: con il Wi-Fi aperto la scheda non cerca piu'
        try:
            scrivi_segreto(f"{RETI_NM}/{PROFILO}.nmconnection", profilo_del_gateway(self.scheda, self.conf))
            # chi si collega chiede un sito qualsiasi: gli si risponde con il Raspberry, cosi' il telefono apre da
            # solo la pagina ("accedi alla rete")
            os.makedirs(os.path.dirname(DNS_NM), exist_ok=True)
            with open(DNS_NM, "w", encoding="utf-8") as f:
                f.write(f"address=/#/{INDIRIZZO}\n")
        except OSError as e:
            registra(f"non riesco a preparare il Wi-Fi del gateway: {e}")
            return False
        esegui(["nmcli", "connection", "load", f"{RETI_NM}/{PROFILO}.nmconnection"])
        codice, uscita = esegui(["nmcli", "--wait", "30", "connection", "up", "id", PROFILO], attesa=45)
        if codice != 0:
            registra(f"il Wi-Fi del gateway non si apre: {uscita.strip()[-300:]}")
            self.chiudi()
            return False
        self.aperto = True
        self.ultima_richiesta = time.time()
        self.avvia_pagina()
        registra(f"senza rete: aperto il Wi-Fi '{self.conf['nome']}' (pagina http://{INDIRIZZO})")
        self.salva_stato("aperto")
        return True

    def chiudi(self) -> None:
        self.ferma_pagina()
        esegui(["nmcli", "connection", "down", "id", PROFILO], attesa=30)
        try:
            os.remove(DNS_NM)
        except OSError:
            pass
        self.aperto = False

    # ---- la pagina ----

    def avvia_pagina(self) -> None:
        if self.server is not None:
            return
        gestore = self

        class Pagina(http.server.BaseHTTPRequestHandler):
            timeout = 20

            def log_message(self, formato, *argomenti):
                pass

            def _manda(self, codice: int, corpo: str, intestazioni: Optional[dict] = None) -> None:
                dati = corpo.encode("utf-8")
                self.send_response(codice)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; "
                                                            "form-action 'self'; base-uri 'none'")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(dati)))
                for k, v in (intestazioni or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(dati)

            def do_GET(self):  # noqa: N802
                gestore.ultima_richiesta = time.time()
                if urllib.parse.urlsplit(self.path).path != "/" or self.headers.get("Host", "") not in (
                        INDIRIZZO, f"{INDIRIZZO}:{PORTA}"):
                    # il telefono prova un suo sito per capire se c'e' internet: lo si manda alla pagina
                    self._manda(302, "", {"Location": f"http://{INDIRIZZO}" + ("" if PORTA == 80 else f":{PORTA}") + "/"})
                    return
                self._manda(200, gestore.pagina())

            def do_POST(self):  # noqa: N802
                gestore.ultima_richiesta = time.time()
                try:
                    lunghezza = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    lunghezza = -1
                if self.path != "/collega" or not 0 < lunghezza <= 4096:
                    self._manda(400, gestore.cornice("<p class=e>Richiesta non valida.</p>"))
                    return
                q = urllib.parse.parse_qs(self.rfile.read(lunghezza).decode("utf-8", "replace"), keep_blank_values=True)

                def uno(nome: str) -> str:
                    return (q.get(nome) or [""])[0]
                if not hmac.compare_digest(uno("gettone"), gestore.gettone):
                    self._manda(403, gestore.cornice("<p class=e>Pagina scaduta: ricaricala.</p>"))
                    return
                nome = uno("altra").strip() if uno("rete") == "" else uno("rete")
                password = uno("password")
                protezione = next((r["protezione"] for r in gestore.reti if r["nome"] == nome), "WPA2" if password else "")
                difetto = difetto_della_richiesta(nome, password, protezione)
                if not difetto and protezione and "WPA" in protezione and not password:
                    difetto = "Questa rete ha una password: scrivila."
                if difetto:
                    self._manda(200, gestore.pagina(difetto))
                    return
                with gestore.lucchetto:
                    gestore.da_provare = (nome, password, protezione)
                self._manda(200, gestore.cornice(
                    f"<h1>Provo a collegarmi a «{html.escape(nome)}»</h1>"
                    "<p>Adesso questo Wi-Fi si spegne e il telefono torna alla sua rete.</p>"
                    "<p><b>Se entro due minuti il Wi-Fi del gateway non ricompare, ha funzionato</b>: il gateway "
                    "torna in linea sulla pagina della rete.</p>"
                    "<p>Se ricompare, la rete o la password non andavano: ricollegati e qui trovi il motivo.</p>"))

        try:
            self.server = http.server.ThreadingHTTPServer((INDIRIZZO if not RADICE else "127.0.0.1", PORTA), Pagina)
        except OSError as e:
            registra(f"la pagina del Wi-Fi del gateway non parte: {e}")
            self.server = None
            return
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, name="pagina", daemon=True).start()

    def ferma_pagina(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def cornice(self, dentro: str) -> str:
        return ("<!doctype html><html lang=it><meta charset=utf-8>"
                "<meta name=viewport content='width=device-width,initial-scale=1'><title>Wi-Fi del gateway</title>"
                "<style>body{font:17px/1.45 system-ui,sans-serif;margin:0;padding:20px;max-width:34em;color:#1b2430;"
                "background:#f4f6f8}h1{font-size:1.3em;margin:0 0 .6em}label{display:block;margin:.9em 0 .3em;"
                "font-weight:600}select,input{font:inherit;width:100%;box-sizing:border-box;padding:.6em;border:1px solid "
                "#8a97a6;border-radius:6px;background:#fff;color:inherit}button{font:inherit;font-weight:600;margin-top:1.2em;"
                "width:100%;padding:.75em;border:0;border-radius:6px;background:#12507a;color:#fff}.e{background:#fde8e6;"
                "border-left:4px solid #b3261e;padding:.6em .8em}small{color:#52606d}"
                "@media(prefers-color-scheme:dark){body{background:#12171d;color:#e6ebf0}select,input{background:#1c242d;"
                "border-color:#5b6876}.e{background:#3a1512}small{color:#a9b4c0}button{background:#2f7fb5}}</style>"
                f"{dentro}</html>")

    def pagina(self, difetto: str = "") -> str:
        avviso = difetto or self.messaggio
        scelte = "".join(
            f"<option value='{html.escape(r['nome'], quote=True)}'>{html.escape(r['nome'])} "
            f"({'buono' if r['segnale'] >= 60 else 'discreto' if r['segnale'] >= 35 else 'debole'}"
            f"{'' if r['protezione'] else ', senza password'})</option>" for r in self.reti)
        return self.cornice(
            "<h1>Collega il gateway a un Wi-Fi</h1>"
            + (f"<p class=e>{html.escape(avviso)}</p>" if avviso else "")
            + "<p>Il gateway non trova una rete. Scegli il Wi-Fi a cui collegarlo.</p>"
            "<form method=post action=/collega autocomplete=off>"
            f"<input type=hidden name=gettone value='{self.gettone}'>"
            f"<label for=rete>Rete</label><select id=rete name=rete>{scelte}"
            "<option value=''>Un'altra (scrivi il nome qui sotto)</option></select>"
            "<label for=altra>Nome della rete, se non e' nell'elenco</label><input id=altra name=altra maxlength=32>"
            "<label for=password>Password del Wi-Fi</label>"
            "<input id=password name=password type=password maxlength=63 autocapitalize=off autocorrect=off>"
            "<button>Collega</button></form>"
            "<p><small>Solo reti a 2,4 GHz se il Raspberry e' un modello 3. Le reti viste sono quelle di quando si e' "
            "aperto questo Wi-Fi. Da qui si sceglie solo il Wi-Fi del gateway.</small></p>")

    # ---- prova di una rete scelta ----

    def prova(self, nome: str, password: str, protezione: str) -> None:
        identita, testo = profilo_della_rete(self.scheda, nome, password, protezione)
        file_rete = f"{RETI_NM}/{identita}.nmconnection"
        time.sleep(3 * min(ATTESE, 1))                 # il tempo di mostrare la risposta sul telefono
        self.chiudi()
        registra(f"dalla pagina: provo la rete '{nome}'")
        try:
            scrivi_segreto(file_rete, testo)
        except OSError as e:
            self.messaggio = f"Non riesco a salvare la rete: {e}"
            return
        esegui(["nmcli", "connection", "load", file_rete])
        codice, uscita = esegui(["nmcli", "--wait", str(PROVA_RETE_S), "connection", "up", "id", identita],
                                attesa=PROVA_RETE_S + 15)
        if codice == 0:
            self.messaggio = ""
            registra(f"collegato a '{nome}': la rete resta salvata")
            self.salva_stato("collegato")
            return
        esegui(["nmcli", "connection", "delete", "id", identita])
        try:
            os.remove(file_rete)
        except OSError:
            pass
        basso = uscita.lower()
        if "secrets" in basso or "password" in basso or "802-11-wireless-security" in basso:
            motivo = "la password non e' giusta"
        elif "not found" in basso or "no network with ssid" in basso or "ssid" in basso and "find" in basso:
            motivo = "la rete non si trova (nome sbagliato, troppo lontana, o solo a 5 GHz)"
        else:
            motivo = "la rete non ha risposto in tempo"
        self.messaggio = f"Non sono riuscito a collegarmi a «{nome}»: {motivo}. La rete non e' stata salvata."
        registra(f"rete '{nome}' non riuscita ({motivo}): {uscita.strip()[-200:]}")

    # ---- il giro ----

    def ciclo(self) -> None:
        senza_rete_da: Optional[float] = time.time()
        ultima_riprova = time.time()
        avvisato = ""
        while True:
            self.conf = leggi_conf()
            self.scheda = self.scheda or scheda_wifi()
            if not self.conf or not self.scheda:
                motivo = "manca il nome o la password in " + CONF if not self.conf else "il Raspberry non ha il Wi-Fi"
                if avvisato != motivo:
                    avvisato = motivo
                    registra(f"niente da fare: {motivo}")
                if self.aperto:
                    self.chiudi()
                time.sleep(GUARDA_OGNI_S * 6)
                continue
            with self.lucchetto:
                da_provare, self.da_provare = self.da_provare, None
            if da_provare and self.aperto:
                self.prova(*da_provare)
                senza_rete_da = None if not self.messaggio else time.time() - SENZA_RETE_S
                continue
            attivi = collegamenti()
            if attivi:
                if self.aperto:
                    self.chiudi()
                    registra(f"di nuovo in rete ({', '.join(t for _, t in attivi)}): chiuso il Wi-Fi del gateway")
                    self.salva_stato("collegato")
                senza_rete_da = None
                self.messaggio = ""
            elif self.aperto:
                ora = time.time()
                if ora - ultima_riprova >= RIPROVA_NOTE_OGNI_S and ora - self.ultima_richiesta >= FERMO_DA_S:
                    # nessuno lo sta usando: si chiude un momento per riprovare le reti che il Raspberry conosce
                    # ("nmcli device connect" non va bene: potrebbe scegliere proprio il Wi-Fi del gateway)
                    self.chiudi()
                    viste = {r["nome"] for r in reti_viste(self.scheda)}
                    for profilo, rete in reti_note():
                        if rete in viste:
                            registra(f"la rete '{rete}' e' di nuovo a portata: la riprovo")
                            if esegui(["nmcli", "--wait", "30", "connection", "up", "id", profilo], attesa=45)[0] == 0:
                                break
                    ultima_riprova = time.time()
                    if not collegamenti():
                        self.apri()        # (rilegge anche l'elenco delle reti viste, per la pagina)
                    continue
            else:
                senza_rete_da = senza_rete_da or time.time()
                if time.time() - senza_rete_da >= SENZA_RETE_S:
                    ultima_riprova = time.time()
                    if not self.apri():
                        senza_rete_da = time.time()        # si riprova tra qualche minuto
            time.sleep(GUARDA_OGNI_S if not self.aperto else min(GUARDA_OGNI_S, 5))


def mostra() -> int:
    conf = leggi_conf()
    if not conf:
        print("Il Wi-Fi del gateway non e' impostato su questo Raspberry (rilancia l'installatore).")
        return 1
    print(f"Se il Raspberry resta senza rete per qualche minuto apre questo Wi-Fi:\n\n"
          f"    nome      {conf['nome']}\n    password  {conf['password']}\n\n"
          f"Collegati con il telefono: si apre la pagina per scegliere il Wi-Fi (oppure http://{INDIRIZZO}).\n")
    attivi = collegamenti()
    print("Adesso: " + ("collegato con " + ", ".join(f"{t} ({n})" for n, t in attivi) if attivi
                        else "senza rete" + (" (nessuna scheda Wi-Fi)" if not scheda_wifi() else "")))
    try:
        with open(STATO, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("stato") == "aperto" and not attivi:
            print("Il Wi-Fi del gateway e' aperto.")
        if d.get("messaggio"):
            print("Ultima prova: " + str(d["messaggio"])[:300])
    except (OSError, ValueError):
        pass
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "servizio":
        if os.geteuid() != 0 and not RADICE:
            print("serve root", file=sys.stderr)
            return 1
        Gestore().ciclo()
        return 0
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    return mostra()


if __name__ == "__main__":
    sys.exit(main())
