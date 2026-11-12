# --------------------------------------------------------------------------
# Utilita'
# --------------------------------------------------------------------------

def iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def errore_da_bit(bit: int) -> float:
    """Raggio di incertezza (metri) di una posizione ridotta a 'bit' bit di precisione."""
    return 23300.0 / (2.0 ** (bit - 10))


def bit_precisione(lat_i: int, lon_i: int, dichiarati: int = 0) -> int:
    """Bit di precisione di una posizione Meshtastic (32 = precisa).

    Se il pacchetto non lo dichiara (firmware vecchi, posizioni nel database del
    nodo) lo ricava dalle coordinate: il firmware azzera i bit bassi e ci somma
    la meta' della cella, quindi resta una firma '1 seguito da soli zeri'.
    """
    if dichiarati and 0 < dichiarati < 32:
        return int(dichiarati)
    if dichiarati and dichiarati >= 32:
        return 32
    lu, ou = lat_i & 0xFFFFFFFF, lon_i & 0xFFFFFFFF
    for bit in range(1, 24):
        resto = 32 - bit
        maschera = (1 << resto) - 1
        meta = 1 << (resto - 1)
        if (lu & maschera) == meta and (ou & maschera) == meta:
            return bit
    return 32


def pulisci(testo: Optional[str], massimo: int = 200) -> str:
    """Toglie i caratteri non ammessi in XML."""
    if not testo:
        return ""
    buoni = []
    for c in str(testo):
        o = ord(c)
        if o in (9, 10, 13) or 0x20 <= o <= 0xD7FF or 0xE000 <= o <= 0xFFFD or 0x10000 <= o <= 0x10FFFF:
            buoni.append(c)
    return "".join(buoni)[:massimo].strip()


def nome_preset(nome_enum: str) -> str:
    return "".join(p.capitalize() for p in nome_enum.split("_")) if nome_enum else "predefinito"


DICHIARAZIONE = '<?xml version="1.0" encoding="UTF-8"?>'


def _numero(v: float) -> str:
    return f"{v:.1f}"


def cot_evento(*, uid: str, tipo: str, how: str, lat: float, lon: float, t_evento: float,
               stale: float, hae: float = NON_NOTO, ce: float = NON_NOTO, le: float = NON_NOTO,
               callsign: Optional[str] = None, endpoint: bool = False,
               squadra: Optional[str] = None, ruolo: Optional[str] = None,
               batteria: Optional[int] = None, rotta: Optional[float] = None,
               velocita: Optional[float] = None, geosrc: Optional[str] = None,
               altsrc: Optional[str] = None, takv: Optional[Dict[str, str]] = None,
               droid: Optional[str] = None, note: Optional[str] = None,
               segno: Optional[Dict[str, str]] = None, icona: Optional[str] = None) -> bytes:
    ev = ET.Element("event", {
        "version": "2.0", "uid": pulisci(uid, 120), "type": tipo, "how": how,
        "time": iso_utc(t_evento), "start": iso_utc(t_evento), "stale": iso_utc(stale),
    })
    ET.SubElement(ev, "point", {"lat": f"{lat:.7f}", "lon": f"{lon:.7f}", "hae": _numero(hae),
                                "ce": _numero(ce), "le": _numero(le)})
    det = ET.SubElement(ev, "detail")
    if takv:
        ET.SubElement(det, "takv", {k: pulisci(v, 80) for k, v in takv.items() if v})
    if callsign:
        attr = {"callsign": pulisci(callsign, 80)}
        if endpoint:
            attr["endpoint"] = "*:-1:stcp"
        ET.SubElement(det, "contact", attr)
    if droid:
        ET.SubElement(det, "uid", {"Droid": pulisci(droid, 80)})
    if squadra or ruolo:
        g = {}
        if squadra:
            g["name"] = squadra
        if ruolo:
            g["role"] = ruolo
        ET.SubElement(det, "__group", g)
    if batteria:
        ET.SubElement(det, "status", {"battery": str(int(batteria))})
    if rotta is not None or velocita is not None:
        ET.SubElement(det, "track", {"course": f"{(rotta or 0.0):.1f}", "speed": f"{(velocita or 0.0):.2f}"})
    if geosrc or altsrc:
        ET.SubElement(det, "precisionlocation", {"geopointsrc": geosrc or "???", "altsrc": altsrc or "???"})
    if note:
        ET.SubElement(det, "remarks").text = pulisci(note, 400)
    if icona:
        ET.SubElement(det, "usericon", {"iconsetpath": pulisci(icona, 200)})
    if segno:
        ET.SubElement(det, "__mesh2tak", segno)
    return (DICHIARAZIONE + ET.tostring(ev, encoding="unicode")).encode("utf-8")


def cot_ping(uid: str, ora: float) -> bytes:
    ev = ET.Element("event", {"version": "2.0", "uid": uid + "-ping", "type": "t-x-c-t", "how": "h-g-i-g-o",
                              "time": iso_utc(ora), "start": iso_utc(ora), "stale": iso_utc(ora + 20)})
    ET.SubElement(ev, "point", {"lat": "0.0", "lon": "0.0", "hae": _numero(NON_NOTO),
                                "ce": _numero(NON_NOTO), "le": _numero(NON_NOTO)})
    ET.SubElement(ev, "detail")
    return (DICHIARAZIONE + ET.tostring(ev, encoding="unicode")).encode("utf-8")


def cot_chat_diretta(*, da_uid: str, da_callsign: str, a_uid: str, a_callsign: str, testo: str,
                     ora: float) -> bytes:
    """Messaggio privato GeoChat (come lo manda ATAK), per gli avvisi del ponte a chi ha scritto."""
    id_msg = str(uuid.uuid4())
    ev = ET.Element("event", {"version": "2.0", "uid": f"GeoChat.{da_uid}.{a_uid}.{id_msg}", "type": "b-t-f",
                              "how": "h-g-i-g-o", "time": iso_utc(ora), "start": iso_utc(ora),
                              "stale": iso_utc(ora + 86400)})
    ET.SubElement(ev, "point", {"lat": "0.0", "lon": "0.0", "hae": _numero(NON_NOTO),
                                "ce": _numero(NON_NOTO), "le": _numero(NON_NOTO)})
    det = ET.SubElement(ev, "detail")
    chat = ET.SubElement(det, "__chat", {"parent": "RootContactGroup", "groupOwner": "false",
                                         "messageId": id_msg, "chatroom": pulisci(a_callsign, 80),
                                         "id": a_uid, "senderCallsign": pulisci(da_callsign, 80)})
    ET.SubElement(chat, "chatgrp", {"uid0": da_uid, "uid1": a_uid, "id": a_uid})
    ET.SubElement(det, "link", {"uid": da_uid, "type": "a-f-G-I", "relation": "p-p"})
    ET.SubElement(det, "remarks", {"source": f"BAO.F.ATAK.{da_uid}", "to": a_uid,
                                   "time": iso_utc(ora)}).text = pulisci(testo, 400)
    ET.SubElement(ET.SubElement(det, "marti"), "dest", {"callsign": pulisci(a_callsign, 80)})
    ET.SubElement(det, "__mesh2tak", {"h": "avviso", "ponte": da_uid})
    return (DICHIARAZIONE + ET.tostring(ev, encoding="unicode")).encode("utf-8")


def leggi_ora_cot(testo: Optional[str]) -> Optional[float]:
    if not testo:
        return None
    try:
        return datetime.fromisoformat(testo.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


_TAG_EVENTO = re.compile(r"<event\b[^>]*>")
_ATTR_STALE = re.compile(r'\bstale="([^"]*)"')


def allunga_stale(xml: str, ora: float, minimo_s: float = STALE_MINIMO_RADIO_S) -> str:
    """Porta la scadenza di forme, segnaposti e percorsi ad almeno 'minimo_s' secondi da ora."""
    tag = _TAG_EVENTO.search(xml)
    if not tag:
        return xml
    stale = _ATTR_STALE.search(tag.group(0))
    attuale = leggi_ora_cot(stale.group(1)) if stale else None
    if stale is None or (attuale is not None and attuale - ora >= minimo_s):
        return xml
    nuovo_tag = tag.group(0)[:stale.start(1)] + iso_utc(ora + minimo_s) + tag.group(0)[stale.end(1):]
    return xml[:tag.start()] + nuovo_tag + xml[tag.end():]


def marca_dalla_radio(xml: str, impronta: str, radio: str, ponte: str) -> str:
    """Segno nel <detail> degli eventi portati dalla radio al server: gli altri ponti lo
    riconoscono e non lo rimandano in radio (niente giri a vuoto tra radio e server)."""
    segno = f'<__mesh2tak h="{impronta}" radio="{radio}" ponte="{ponte}"/>'
    if "</detail>" in xml:
        return xml.replace("</detail>", segno + "</detail>", 1)
    if "<detail/>" in xml:
        return xml.replace("<detail/>", "<detail>" + segno + "</detail>", 1)
    return xml.replace("</event>", "<detail>" + segno + "</detail></event>", 1)


def estrai_eventi(buf: bytearray) -> List[bytes]:
    """Toglie da 'buf' gli eventi CoT completi arrivati dal TAK Server, nell'ordine."""
    eventi = []
    while True:
        inizio = buf.find(b"<event")
        if inizio < 0:
            if len(buf) > 16:
                del buf[:-16]  # tiene un eventuale "<eve" spezzato a meta'
            return eventi
        if inizio:
            del buf[:inizio]
        fine_tag = buf.find(b">")
        if fine_tag < 0:
            return eventi
        if buf[fine_tag - 1:fine_tag] == b"/":
            eventi.append(bytes(buf[:fine_tag + 1]))
            del buf[:fine_tag + 1]
            continue
        chiusura = buf.find(b"</event>")
        if chiusura < 0:
            return eventi
        eventi.append(bytes(buf[:chiusura + 8]))
        del buf[:chiusura + 8]


def tempo_in_aria(byte_utili: int, preset: str = "LONG_FAST") -> float:
    """Secondi di trasmissione LoRa di un pacchetto Meshtastic con 'byte_utili' di carico."""
    sf, banda, cr = PRESET_LORA.get(preset, PRESET_LORA["LONG_FAST"])
    simbolo = (2 ** sf) / banda
    ottimizza = 1 if simbolo > 0.016 else 0
    byte_radio = byte_utili + 16 + 6  # intestazione Meshtastic + involucro del messaggio
    simboli = 8 + max(math.ceil((8 * byte_radio - 4 * sf + 28 + 16) / (4 * (sf - 2 * ottimizza))) * cr, 0)
    return (16 + 4.25) * simbolo + simboli * simbolo


class Memoria:
    """Chiavi ricordate per 'durata' secondi (doppioni, giri a vuoto tra radio e server)."""

    def __init__(self, durata: float, massimo: int = 5000):
        self.durata = durata
        self.massimo = massimo
        self._voci: "OrderedDict[str, float]" = OrderedDict()
        self._lock = threading.Lock()

    def _pulisci(self, ora: float) -> None:
        while self._voci:
            chiave, quando = next(iter(self._voci.items()))
            if ora - quando <= self.durata and len(self._voci) <= self.massimo:
                break
            self._voci.popitem(last=False)

    def aggiungi(self, chiave: str) -> None:
        ora = time.time()
        with self._lock:
            self._voci.pop(chiave, None)
            self._voci[chiave] = ora
            self._pulisci(ora)

    def __contains__(self, chiave) -> bool:
        with self._lock:
            self._pulisci(time.time())
            return chiave in self._voci


# Un telefono che ha mandato la sua posizione al TAK Server via internet da meno di cosi' e' collegato:
# la copia arrivata via radio e' piu' vecchia e farebbe saltare indietro la sua icona.
COLLEGATO_INTERNET_S = 120

# Cosa arriva dal TAK Server e se va alle radio: categoria -> priorita' (0 = prima)
PRIORITA = {"emergenza": 0, "chat": 1, "medevac": 1, "incarico": 2, "oggetto": 3}


def categoria_dal_server(radice: ET.Element) -> Tuple[Optional[str], str]:
    """(categoria, motivo): categoria None se l'evento non va mandato alle radio."""
    tipo = radice.get("type") or ""
    det = radice.find("detail")
    if det is not None and det.find("__mesh2tak") is not None:
        return None, "arriva gia' dalla rete radio"
    if tipo == "b-t-f":
        return "chat", ""
    if tipo.startswith("b-t-f-"):
        return None, "conferma di consegna o lettura di una chat"
    if tipo.startswith("b-a-"):
        return "emergenza", ""
    if tipo == "b-r-f-h-c":
        return "medevac", ""
    if tipo == "t-s" or tipo.startswith("t-s-"):
        return "incarico", ""
    if tipo == "t-x-d-d":
        return None, "cancellazione (la libreria Meshtastic non trasporta l'oggetto da cancellare)"
    if tipo.startswith("t-"):
        return None, "messaggio di servizio"
    if tipo.startswith(("u-d-", "u-r-b-", "u-rb-", "b-m-p-", "b-m-r", "b-i-x-i")):
        return "oggetto", ""
    if tipo.startswith("a-"):
        if e_posizione_dispositivo(radice):
            return None, "posizione di un dispositivo collegato via internet"
        return "oggetto", ""
    return None, f"tipo {tipo or '?'} non previsto per la radio"


def e_posizione_dispositivo(radice: ET.Element) -> bool:
    """Posizione mandata da un telefono o computer stesso (ATAK, TAK Aware...), non un segnaposto."""
    if not (radice.get("type") or "").startswith("a-"):
        return False
    det = radice.find("detail")
    if det is not None and det.find("__mesh2tak") is not None:
        return False
    return (radice.get("how") or "").startswith("m-") or (det is not None and (
        det.find("takv") is not None or any(c.get("endpoint") for c in det.findall("contact"))))


def mittente_evento(radice: ET.Element) -> Tuple[Optional[str], Optional[str]]:
    """UID e nominativo di chi ha creato o scritto un evento, se ricavabili."""
    det = radice.find("detail")
    if det is None:
        return None, None
    chat = det.find("__chat")
    if chat is not None:
        grp = chat.find("chatgrp")
        return (grp.get("uid0") if grp is not None else None), chat.get("senderCallsign")
    creatore = det.find("creator")
    if creatore is not None and creatore.get("uid"):
        return creatore.get("uid"), creatore.get("callsign")
    for link in det.findall("link"):
        if link.get("relation") == "p-p" and link.get("uid"):
            return link.get("uid"), link.get("parent_callsign")
    return None, None


def nome_evento(radice: ET.Element) -> str:
    det = radice.find("detail")
    contatto = det.find("contact") if det is not None else None
    return pulisci(contatto.get("callsign") if contatto is not None else "", 60)


