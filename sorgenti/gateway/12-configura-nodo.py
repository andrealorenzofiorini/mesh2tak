# --------------------------------------------------------------------------
# Configurazione del nodo Meshtastic (usata dall'installatore)
# --------------------------------------------------------------------------

def _apri_con_attesa(collegamento: str, limite_s: float):
    """Riprova finche' il nodo risponde: dopo un riavvio meshtasticd impiega qualche secondo."""
    fine = time.time() + limite_s
    ultimo = None
    while time.time() < fine:
        try:
            return apri_interfaccia(collegamento)
        except Exception as e:  # noqa
            ultimo = e
            time.sleep(5)
    raise ultimo if ultimo else TimeoutError("nodo non raggiungibile")


def _stato_nodo(iface) -> dict:
    from meshtastic.protobuf import config_pb2
    nodo = iface.localNode
    mio = iface.myInfo.my_node_num if iface.myInfo else 0
    utente = (iface.nodesByNum.get(mio) or {}).get("user", {})
    privati = [c for c in (nodo.channels or []) if c.role != 0 and len(bytes(c.settings.psk)) >= 16]
    return {
        "num": mio,
        "regione": config_pb2.Config.LoRaConfig.RegionCode.Name(nodo.localConfig.lora.region),
        "ruolo": config_pb2.Config.DeviceConfig.Role.Name(nodo.localConfig.device.role),
        "nome": utente.get("longName", ""),
        "nome_breve": utente.get("shortName", ""),
        "privati": [c.settings.name or "?" for c in privati],
        "posizione_fissa": bool(nodo.localConfig.position.fixed_position),
        "canali": [(int(c.role), c.settings.name, bytes(c.settings.psk)) for c in (nodo.channels or [])[:2]],
        "secondari": [nome_canale(nodo.channels[i]) for i in secondari_da_spegnere(nodo)],
        "accesi": [nome_canale(c) for i, c in enumerate(nodo.channels or []) if i > 0 and int(c.role) != 0],
    }


def nome_canale(c) -> str:
    return c.settings.name or ("LongFast" if bytes(c.settings.psk) == b"\x01" else f"n. {c.index}")


def secondari_da_spegnere(nodo) -> List[int]:
    """Canali secondari ancora accesi, da spegnere quando la radio si prepara come all'installazione (prima
    installazione o "Reimposta la radio"; dalla 1.13.0 mai negli aggiornamenti). Tutti, se il primario e' un canale
    privato (la squadra); altrimenti (canale della squadra messo come secondario a mano) solo quelli pubblici, per
    non perdere la squadra."""
    canali = list(nodo.channels or [])
    if not canali:
        return []
    primo = canali[0]
    primario_privato = int(primo.role) == 1 and len(bytes(primo.settings.psk)) >= 16
    return [i for i, c in enumerate(canali) if i > 0 and int(c.role) != 0
            and (primario_privato or len(bytes(c.settings.psk)) < 16)]


def spegni_canale(nodo, indice: int) -> None:
    from meshtastic.protobuf import channel_pb2
    c = nodo.channels[indice]
    prima = channel_pb2.Channel()
    prima.CopyFrom(c)
    c.role = channel_pb2.Channel.Role.DISABLED
    c.ClearField("settings")
    try:
        nodo.writeChannel(indice)
    except Exception:
        c.CopyFrom(prima)        # il nodo non l'ha preso: la copia del ponte resta come il nodo
        raise


# Il nodo mette i messaggi di configurazione in una coda di 4 posti e, se ne arrivano di piu' tutti
# insieme, scarta i piu' vecchi senza dirlo (Router::enqueueReceivedMessage, MAX_RX_FROMRADIO = 4 nel
# firmware 2.7 e 2.8). Ogni modifica parte quindi da sola, con una pausa, e alla fine si controlla.
PAUSA_ADMIN_S = 1.0
TENTATIVI_CONFIGURAZIONE = 3

# Le parti della radio che l'installatore prepara. Dalla 1.13.0 (scelta dell'11/11/2026) le prepara tutte
# solo la prima volta e con "Reimposta la radio": negli aggiornamenti resta quello che si e' scelto dall'app o dalla
# pagina (l'installatore chiede solo le parti nuove, es. il canale della squadra dato con --canale-squadra).
PARTI_NODO = ("regione", "ruolo", "nome", "canale", "secondari", "posizione")


def leggi_parti(testo: str) -> Set[str]:
    """--parti: "tutto", "nessuna" o un elenco separato da virgole (es. nome,secondari)."""
    t = (testo or "").strip().lower()
    if t in ("", "tutto", "tutte"):
        return set(PARTI_NODO)
    if t in ("nessuna", "niente"):
        return set()
    parti = {x.strip() for x in t.split(",") if x.strip()}
    strane = parti - set(PARTI_NODO)
    if strane:
        raise ValueError(f"parti sconosciute: {', '.join(sorted(strane))} (si possono chiedere: {', '.join(PARTI_NODO)})")
    return parti


def _modifiche_da_fare(iface, nome: str, nome_breve: str, canale: str, ruolo: str,
                       posizione: Optional[Tuple[float, float, int]], pausa: Callable[[], None],
                       chiave: Optional[bytes] = None, parti: Optional[Set[str]] = None):
    """(descrizione, azione) per cio' che sul nodo non e' ancora come deve essere, solo nelle 'parti' chieste
    (None = tutte).

    Con 'chiave' il canale della squadra e' dato (es. quello di ICARO): il primario deve avere
    esattamente quel nome e quella chiave. Senza, si crea un canale con chiave nuova solo se non
    ce n'e' gia' uno privato. Con la parte "secondari" i canali secondari (es. il pubblico LongFast) si spengono.
    """
    from meshtastic.protobuf import channel_pb2, config_pb2
    Ruolo = config_pb2.Config.DeviceConfig.Role
    Regione = config_pb2.Config.LoRaConfig.RegionCode
    parti = set(PARTI_NODO) if parti is None else parti
    nodo = iface.localNode
    prima = _stato_nodo(iface)
    da_fare = []
    if "regione" in parti and nodo.localConfig.lora.region != Regione.EU_868:
        def _regione():
            nodo.localConfig.lora.region = Regione.EU_868
            nodo.localConfig.lora.use_preset = True
            nodo.writeConfig("lora")
        da_fare.append(("regione EU 868", _regione))
    if "ruolo" in parti and ruolo and nodo.localConfig.device.role != Ruolo.Value(ruolo):
        def _ruolo():
            nodo.localConfig.device.role = Ruolo.Value(ruolo)
            nodo.writeConfig("device")
        da_fare.append((f"ruolo {ruolo}", _ruolo))
    if "nome" in parti and nome and (prima["nome"] != nome or prima["nome_breve"] != nome_breve):
        da_fare.append((f"nome '{nome}' ({nome_breve})", lambda: nodo.setOwner(nome, nome_breve)))
    if chiave is not None:
        primo = nodo.channels[0]
        if not (primo.role == channel_pb2.Channel.Role.PRIMARY and primo.settings.name == canale
                and bytes(primo.settings.psk) == chiave):
            def _primario():
                primo.role = channel_pb2.Channel.Role.PRIMARY
                primo.settings.name = canale
                primo.settings.psk = chiave
                primo.settings.module_settings.position_precision = 32
                nodo.writeChannel(0)
            da_fare.append((f"canale della squadra '{canale}' con la chiave data", _primario))
    crea_canale = "canale" in parti and chiave is None and not prima["privati"]
    if crea_canale:
        def _canali():
            primo = nodo.channels[0]
            primo.role = channel_pb2.Channel.Role.PRIMARY
            primo.settings.name = canale
            primo.settings.psk = os.urandom(32)
            primo.settings.module_settings.position_precision = 32
            nodo.writeChannel(0)
        da_fare.append((f"canale privato '{canale}' con chiave nuova", _canali))
    if "secondari" in parti:
        # solo il canale della squadra: i secondari si spengono. Se in questo giro il primario diventa la squadra
        # (chiave data, o canale creato adesso) si spengono tutti; altrimenti vale la regola di secondari_da_spegnere
        primo = nodo.channels[0]
        squadra_primaria = chiave is not None or crea_canale or (
            int(primo.role) == 1 and len(bytes(primo.settings.psk)) >= 16)
        for i, c in enumerate(nodo.channels):
            if i > 0 and int(c.role) != 0 and (squadra_primaria or len(bytes(c.settings.psk)) < 16):
                da_fare.append((f"canale secondario '{nome_canale(c)}' spento", lambda i=i: spegni_canale(nodo, i)))
    if posizione and "posizione" in parti:
        lat, lon, alt = posizione
        attuale = (iface.nodesByNum.get(prima["num"]) or {}).get("position") or {}
        uguale = (prima["posizione_fissa"]
                  and attuale.get("latitudeI") == int(float(lat) / 1e-7)
                  and attuale.get("longitudeI") == int(float(lon) / 1e-7)
                  and int(attuale.get("altitude") or 0) == int(alt or 0))
        if not uguale:
            da_fare.append((f"posizione fissa {lat:.5f}, {lon:.5f}",
                            lambda: nodo.setFixedPosition(float(lat), float(lon), int(alt or 0))))
    return da_fare


def configura_nodo(collegamento: str, nome: str, nome_breve: str, canale: str, ruolo: str,
                   posizione: Optional[Tuple[float, float, int]] = None, file_link: Optional[str] = None,
                   attesa_riavvio_s: float = 15.0, pausa_s: float = PAUSA_ADMIN_S,
                   chiave_canale: Optional[bytes] = None, parti: Optional[Set[str]] = None) -> int:
    """Regione EU 868, nome, ruolo, canale privato della squadra, canali secondari spenti e posizione fissa: tutto
    (parti None) o solo le parti chieste. Una radio nuova (regione non impostata) si prepara sempre tutta.

    Non cambia cio' che e' gia' a posto, quindi si puo' rilanciare senza danni: se un
    canale privato esiste gia', resta quello (e la sua chiave). Le modifiche partono una
    alla volta; dopo il riavvio del nodo si ricontrolla e si ripete cio' che manca.
    """
    def pausa():
        time.sleep(pausa_s)

    parti = set(PARTI_NODO) if parti is None else set(parti)
    if chiave_canale is not None:
        parti.add("canale")                   # il canale della squadra dato si mette sempre
    print("Collegamento al nodo Meshtastic (" + descrivi_collegamento(collegamento) + ")...")
    dopo, link = None, ""
    for tentativo in range(1, TENTATIVI_CONFIGURAZIONE + 1):
        try:
            iface = _apri_con_attesa(collegamento, 150)
        except Exception as e:  # noqa
            print("  ERRORE: " + spiega_errore_nodo(e, collegamento))
            print("ESITO configurazione=errore")
            return 1
        cambi = []
        try:
            if tentativo == 1 and parti != set(PARTI_NODO) and _stato_nodo(iface)["regione"] == "UNSET":
                parti = set(PARTI_NODO)
                print("  radio nuova (regione non impostata): la preparo tutta, come alla prima installazione")
            elif tentativo == 1 and not parti:
                print("  impostazioni della radio lasciate come sono")
            da_fare = _modifiche_da_fare(iface, nome, nome_breve, canale, ruolo, posizione, pausa,
                                         chiave_canale, parti)
            if not da_fare or tentativo == TENTATIVI_CONFIGURAZIONE:
                dopo = _stato_nodo(iface)
                link = iface.localNode.getURL(includeAll=True)
            else:
                # Prima si decide cosa cambiare, poi si scrive in una transazione: la conferma finale
                # salva e fa ripartire il nodo. Un messaggio alla volta (vedi PAUSA_ADMIN_S).
                nodo = iface.localNode
                nodo.beginSettingsTransaction()
                pausa()
                for descrizione, azione in da_fare:
                    azione()
                    pausa()
                    cambi.append(descrizione)
                nodo.commitSettingsTransaction()
                pausa()
        except Exception as e:  # noqa
            print(f"  ERRORE durante la configurazione: {type(e).__name__}: {e}")
            print("ESITO configurazione=errore")
            return 1
        finally:
            try:
                iface.close()
            except Exception:  # noqa
                pass
        if dopo is not None:
            if tentativo == 1 and parti:
                print("  era gia' tutto a posto")
            break
        print("  " + ("modifiche: " if tentativo == 1 else "di nuovo, perche' il nodo non le aveva prese: ")
              + ", ".join(cambi))
        time.sleep(attesa_riavvio_s)  # il nodo salva e riparte (meshtasticd: dopo 7 secondi)

    problemi = []
    if "regione" in parti and dopo["regione"] != "EU_868":
        problemi.append(f"regione {dopo['regione']}")
    if "ruolo" in parti and ruolo and dopo["ruolo"] != ruolo:
        problemi.append(f"ruolo {dopo['ruolo']}")
    if "canale" in parti and not dopo["privati"]:
        problemi.append("nessun canale privato")
    if "nome" in parti and nome and dopo["nome"] != nome:
        problemi.append(f"nome '{dopo['nome']}'")
    if "posizione" in parti and posizione and not dopo["posizione_fissa"]:
        problemi.append("posizione fissa non impostata")
    canali = dopo.get("canali") or []
    if chiave_canale is not None and not (canali and canali[0][1] == canale and canali[0][2] == chiave_canale):
        problemi.append(f"canale della squadra '{canale}'")
    if "secondari" in parti and dopo["secondari"]:
        problemi.append("canali secondari ancora accesi: " + ", ".join(dopo["secondari"]))
    print(f"  nodo !{dopo['num']:08x} '{dopo['nome']}' - regione {dopo['regione']}, ruolo {dopo['ruolo']}, "
          f"canale privato: {', '.join(dopo['privati']) or 'nessuno'}, "
          + (f"canali secondari: {', '.join(dopo['accesi'])}" if dopo["accesi"] else "nessun canale secondario"))
    if not dopo["privati"] and "canale" not in parti:
        print("  ATTENZIONE: il nodo non ha piu' il canale privato della squadra: il gateway non porta le radio al TAK "
              "Server. Per rimetterlo: 'Reimposta la radio' sulla pagina della rete (o --reimposta-radio)")
    if file_link and dopo["privati"]:
        try:
            with open(os.open(file_link, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
                f.write(link + "\n")
        except OSError as e:
            print(f"  attenzione: link non salvato in {file_link}: {e}")
    print("LINK " + link)
    if problemi:
        print("  ERRORE: il nodo non ha preso queste impostazioni: " + ", ".join(problemi))
        print("ESITO configurazione=errore")
        return 1
    print("ESITO configurazione=ok")
    return 0


def riavvia_nodo(collegamento: str, attesa_s: float = 60.0) -> int:
    """Riavvia la radio del ponte (dalla 1.13.0, "Riavvia la radio" della pagina della rete). Serve per la radio USB:
    meshtasticd si riavvia con systemctl. La lancia mesh2tak-aggiorna, da root, con il ponte fermo: il nodo accetta un
    solo collegamento alla volta."""
    print("Collegamento al nodo Meshtastic (" + descrivi_collegamento(collegamento) + ")...")
    try:
        iface = _apri_con_attesa(collegamento, attesa_s)
    except Exception as e:  # noqa
        print("  ERRORE: " + spiega_errore_nodo(e, collegamento))
        print("ESITO riavvio=errore")
        return 1
    try:
        iface.localNode.reboot(3)
        time.sleep(1.0)                       # il messaggio parte prima di chiudere
    except Exception as e:  # noqa
        print(f"  ERRORE: {type(e).__name__}: {e}")
        print("ESITO riavvio=errore")
        return 1
    finally:
        try:
            iface.close()
        except Exception:  # noqa
            pass
    print("  la radio si riavvia tra 3 secondi")
    print("ESITO riavvio=ok")
    return 0


def leggi_chiave_canale(testo: str) -> bytes:
    """Chiave di un canale privato come la mostrano le app: base64 (anche con 'base64:' davanti), 16 o 32 byte."""
    import base64
    import binascii
    t = testo.strip()
    if t.lower().startswith("base64:"):
        t = t[7:].strip()
    try:
        chiave = base64.b64decode(t, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("non e' in base64")
    if len(chiave) not in (16, 32):
        raise ValueError(f"deve essere di 16 o 32 byte, qui sono {len(chiave)}")
    return chiave


def _leggi_posizione(testo: str) -> Tuple[float, float, int]:
    parti = [x for x in testo.replace(";", ",").replace(" ", ",").split(",") if x.strip()]
    if len(parti) not in (2, 3):
        raise ValueError("servono latitudine e longitudine, es. 45.8205,8.8257")
    lat, lon = float(parti[0]), float(parti[1])
    alt = int(float(parti[2])) if len(parti) == 3 else 0
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        raise ValueError("coordinate fuori scala")
    return lat, lon, alt


