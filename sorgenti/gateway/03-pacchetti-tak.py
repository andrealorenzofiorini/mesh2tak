# --------------------------------------------------------------------------
# Pacchetti TAK della porta 78: libreria ufficiale di Meshtastic (TAKPacket-SDK)
# --------------------------------------------------------------------------

class PacchettoIllegibile(Exception):
    pass


class OggettoTroppoGrande(Exception):
    pass


def _senza_annotazioni(fdp) -> None:
    """Toglie dallo schema le annotazioni field_metadata (servono solo alla documentazione)."""
    def pulisci_opzioni(elemento):
        if not elemento.HasField("options"):
            return
        opzioni = elemento.options
        for campo, _ in opzioni.ListFields():
            if campo.is_extension:
                opzioni.ClearExtension(campo)
        opzioni.DiscardUnknownFields()
        if not opzioni.ListFields():
            elemento.ClearField("options")

    def messaggio(m):
        for campo in m.field:
            pulisci_opzioni(campo)
        for annidato in m.nested_type:
            messaggio(annidato)
        for e in m.enum_type:
            for v in e.value:
                pulisci_opzioni(v)
    del fdp.dependency[:]
    for m in fdp.message_type:
        messaggio(m)
    for e in fdp.enum_type:
        for v in e.value:
            pulisci_opzioni(v)


def carica_sdk_tak():
    """Carica la libreria meshtastic-tak accanto alla libreria meshtastic.

    Tutte e due registrano in protobuf un'annotazione (field_metadata) con lo stesso numero:
    nello stesso programma la seconda non si caricherebbe ("duplicate extension entry").
    Lo schema atak.proto della libreria TAK viene quindi caricato in un archivio protobuf a
    parte e senza quelle annotazioni: il formato dei pacchetti radio resta identico.
    """
    if "meshtastic_tak" in sys.modules:
        return sys.modules["meshtastic_tak"]
    import ast
    import importlib
    import importlib.util
    import types
    from google.protobuf import descriptor_pb2, descriptor_pool
    from google.protobuf.internal import builder
    spec = importlib.util.find_spec("meshtastic_tak")
    if spec is None or not spec.submodule_search_locations:
        raise ImportError("libreria meshtastic-tak non installata")
    cartella = list(spec.submodule_search_locations)[0]
    with open(os.path.join(cartella, "atak_pb2.py"), encoding="utf-8") as f:
        albero = ast.parse(f.read())
    dati = None
    for nodo in ast.walk(albero):
        if (isinstance(nodo, ast.Call) and getattr(nodo.func, "attr", "") == "AddSerializedFile" and nodo.args
                and isinstance(nodo.args[0], ast.Constant) and isinstance(nodo.args[0].value, bytes)):
            dati = nodo.args[0].value
    if dati is None:
        raise ImportError("schema atak.proto non trovato in meshtastic_tak")
    fdp = descriptor_pb2.FileDescriptorProto.FromString(dati)
    _senza_annotazioni(fdp)
    archivio = descriptor_pool.DescriptorPool()
    archivio.AddSerializedFile(fdp.SerializeToString())
    fd = archivio.FindFileByName(fdp.name)
    modulo = types.ModuleType("meshtastic_tak.atak_pb2")
    builder.BuildTopDescriptorsAndMessages(fd, modulo.__name__, modulo.__dict__)
    modulo.DESCRIPTOR = fd
    sys.modules[modulo.__name__] = modulo
    sys.modules.setdefault("meshtastic_tak.field_metadata_pb2", types.ModuleType("meshtastic_tak.field_metadata_pb2"))
    try:
        return importlib.import_module("meshtastic_tak")
    except BaseException:
        sys.modules.pop(modulo.__name__, None)
        raise


class LibreriaTak:
    """CoT XML <-> pacchetti TAK compressi della porta 78, con la stessa libreria delle app Meshtastic."""

    def __init__(self):
        sdk = carica_sdk_tak()
        self.pb = sys.modules["meshtastic_tak.atak_pb2"]
        try:
            from importlib.metadata import version
            self.versione = version("meshtastic-tak")
        except Exception:  # noqa: libreria presa dai sorgenti
            self.versione = getattr(sdk, "__version__", "?")
        self._compressore = sdk.TakCompressor()
        self._costruttore = sdk.CotXmlBuilder()
        self._lettore = sdk.CotXmlParser()
        self._igiene = sdk.CotMeshSanitizer
        self._lock = threading.Lock()  # i compressori zstd non si usano da due thread insieme

    def decodifica(self, carico: bytes):
        if not carico or len(carico) < 2:
            raise PacchettoIllegibile("pacchetto vuoto")
        try:
            with self._lock:
                return self._compressore.decompress(bytes(carico))
        except Exception as e:  # noqa: zstd, protobuf, dizionario sconosciuto...
            raise PacchettoIllegibile(str(e) or type(e).__name__)

    def in_xml(self, pkt) -> str:
        """Evento CoT su una riga, senza dichiarazione <?xml?>, come lo vogliono i server TAK."""
        return self._igiene.normalize_cot_xml(self._costruttore.build(pkt)).strip()

    def da_xml(self, xml: str):
        """(pacchetto, byte da trasmettere). Se non sta in un pacchetto radio nemmeno senza
        le note, OggettoTroppoGrande. Il pacchetto restituito e' quello che va davvero in aria."""
        pulito = self._igiene.strip_non_essential_for_mesh(self._igiene.normalize_cot_xml(xml))
        pkt = self._lettore.parse(pulito)
        with self._lock:
            filo = self._compressore.compress_with_remarks_fallback(pkt, MAX_FILO)
        if filo is None:
            raise OggettoTroppoGrande(pkt.callsign or pkt.uid)
        return self.decodifica(filo), filo

    def impronta(self, pkt) -> str:
        """Impronta del contenuto, uguale per tutti i ponti e per le app: senza la scadenza e
        l'ora di partenza, che ognuno ricalcola dal proprio orologio."""
        copia = self.pb.TAKPacketV2()
        copia.CopyFrom(pkt)
        copia.ClearField("stale_seconds")
        if "start_epoch_lo14" in {f.name for f in copia.DESCRIPTOR.fields}:
            copia.ClearField("start_epoch_lo14")
        return hashlib.sha256(copia.SerializeToString(deterministic=True)).hexdigest()[:24]

    def verifica(self) -> None:
        """Prova completa: chat CoT -> pacchetto radio -> CoT."""
        ora = time.time()
        xml = cot_chat_diretta(da_uid="ANDROID-PROVA-1", da_callsign="PROVA-1", a_uid="ANDROID-PROVA-2",
                               a_callsign="PROVA-2", testo="prova libreria", ora=ora).decode()
        pkt, filo = self.da_xml(xml)
        indietro = self.in_xml(self.decodifica(filo))
        if "prova libreria" not in indietro or not 0 < len(filo) <= MAX_FILO:
            raise RuntimeError("conversione di prova non riuscita")


def descrivi_pkt(pb, pkt) -> str:
    """Descrizione breve per il registro."""
    tipo = tipo_cot_v2(pb, pkt)
    nome = pulisci(pkt.callsign, 60)
    quale = pkt.WhichOneof("payload_variant")
    if quale == "chat":
        if pkt.chat.receipt_type:
            return f"conferma di lettura di {nome or '?'}"
        mittente = pulisci(pkt.chat.to_callsign, 60) or nome or "?"
        testo = pulisci(pkt.chat.message, 60)
        return f"chat di {mittente}: «{testo}»"
    nomi = {"shape": "forma", "marker": "segnaposto", "route": "percorso", "rab": "misura distanza e direzione",
            "casevac": "richiesta MEDEVAC", "task": "incarico", "aircraft": "aereo",
            "taktalk": "messaggio TAK Talk", "taktalk_room": "stanza TAK Talk"}
    if quale == "emergency":
        return f"ALLARME di {nome or '?'} ({tipo})"
    if tipo == "t-x-d-d":
        return "cancellazione di un oggetto"
    if quale in nomi:
        return f"{nomi[quale]} «{nome}»" if nome else nomi[quale]
    if tipo.startswith("a-"):
        return f"punto «{nome}» ({tipo})" if nome else f"punto {tipo}"
    return f"evento {tipo or '?'}"


def tipo_cot_v2(pb, pkt) -> str:
    if pkt.cot_type_id == 0:
        return pkt.cot_type_str or ""
    nome = pb.CotType.Name(pkt.cot_type_id)
    return nome[len("CotType_"):].replace("_", "-") if nome.startswith("CotType_") else ""


def how_v2(pb, pkt) -> str:
    try:
        nome = pb.CotHow.Name(pkt.how)
    except ValueError:
        return "m-g"
    if not nome.startswith("CotHow_") or pkt.how == 0:
        return "m-g"
    return nome[len("CotHow_"):].replace("_", "-")


