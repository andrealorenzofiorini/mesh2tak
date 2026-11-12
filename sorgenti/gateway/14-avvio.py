# --------------------------------------------------------------------------
# Avvio
# --------------------------------------------------------------------------

def stato_per_la_rete(cfg: Config, ponte: Ponte, tak: ClienteTAK, gestione: Gestione,
                      accesso: Optional[AccessoEsterno], avviato: float,
                      sistema: Optional[Sistema] = None) -> dict:
    """Quello che la pagina della rete legge da /mesh2tak/stato.json."""
    s = ponte.istantanea()
    s.update({
        "formato": 1,
        "versione": VERSIONE,
        "ora": round(time.time()),
        "avviato": round(avviato),
        "tak": {"collegato": tak.collegato.is_set(), "server": f"{cfg.tak_host}:{cfg.tak_porta}",
                "dal": round(tak.collegato_dal) if tak.collegato_dal else None, "errore": tak.ultimo_errore},
        "pausa": gestione.stato_pausa(),
        "gestione": {"pagina": bool(gestione.nodo_pagina), "app": bool(cfg.gestione_porta_app),
                     "radio": gestione.radio is not None,
                     "pausa_massima_min": cfg.gestione_pausa_massima_min, "chiusura_s": cfg.gestione_chiusura_s},
        "accesso": accesso.stato() if accesso is not None else None,
        "sistema": sistema.stato() if sistema is not None else None,
        "certificato": certificato_tak(cfg.certificato),
        "icone": ponte.icone.stato(),
        # dalla 1.13.0: quelle che si cambiano dalla pagina della rete ("Gestisci il Raspberry")
        "impostazioni": {"squadra": cfg.squadra, "validita_min": cfg.validita_min,
                         "aggiornamento_min": cfg.aggiornamento_min, "errore_massimo_m": cfg.errore_massimo_m,
                         "includi_ponte": cfg.includi_ponte, "invio": cfg.invio_radio,
                         "tempo_radio_percento": cfg.tempo_radio_percento,
                         "pausa_massima_min": cfg.gestione_pausa_massima_min},
    })
    return s


def prova(cfg: Config) -> int:
    """Controllo usato dall'installatore: nodo, canali, TAK Server."""
    esito = 0
    print("Controllo il nodo Meshtastic (" + descrivi_collegamento(cfg.collegamento) + ")...")
    try:
        iface = apri_interfaccia(cfg)
    except Exception as e:  # noqa
        print("  ERRORE: " + spiega_errore_nodo(e, cfg))
        print("ESITO nodo=errore")
        esito |= 1
    else:
        try:
            canali, ok = leggi_canali(iface, cfg)
            mio = iface.myInfo.my_node_num if iface.myInfo else 0
            u = (iface.nodesByNum.get(mio) or {}).get("user", {})
            print(f"  nodo '{u.get('longName', '?')}' (!{mio:08x}) collegato")
            for c in canali.values():
                print(f"  canale {c.indice} '{c.nome}': {'privato' if c.privato else 'pubblico'}"
                      f" -> {'inoltrato' if c.indice in ok else 'ignorato'}")
            ora = time.time()
            recenti = 0
            for num, info in iface.nodesByNum.items():
                pos = info.get("position") or {}
                if num == mio or not (pos.get("latitudeI") or pos.get("longitudeI")):
                    continue
                if (info.get("channel", 0) in ok
                        and ora - float(info.get("lastHeard") or 0) <= cfg.validita_min * 60):
                    recenti += 1
            privati = sum(1 for c in canali.values() if c.privato)
            print(f"  radio con posizione recente sui canali inoltrati: {recenti}")
            print(f"ESITO nodo=ok privati={privati} inoltrati={len(ok)} recenti={recenti}")
        finally:
            try:
                iface.close()
            except Exception:  # noqa
                pass
    print(f"Controllo il TAK Server {cfg.tak_host}:{cfg.tak_porta}...")
    ok_tak, messaggio = prova_tak(cfg)
    print(("  " if ok_tak else "  ERRORE: ") + messaggio)
    print("ESITO tak=" + ("ok" if ok_tak else "errore"))
    if not ok_tak:
        esito |= 2
    if not verifica_librerie():
        esito |= 4
    return esito


def verifica_librerie() -> bool:
    """La libreria TAK di Meshtastic si carica accanto a quella della radio e converte davvero."""
    print("Controllo la libreria TAK di Meshtastic (chat, segnaposti, forme)...")
    try:
        import meshtastic  # noqa: F401  (va caricata prima, come nel servizio)
        sdk = LibreriaTak()
        sdk.verifica()
    except Exception as e:  # noqa
        print(f"  ERRORE: {type(e).__name__}: {e}")
        print("ESITO librerie=errore")
        return False
    print(f"  meshtastic-tak {sdk.versione}: conversione di prova riuscita")
    print("ESITO librerie=ok")
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Gateway Meshtastic <-> TAK Server")
    ap.add_argument("-c", "--config", default=CONFIG_PREDEFINITA, help="file di configurazione")
    ap.add_argument("--prova", action="store_true", help="controlla nodo, TAK Server e librerie, poi esce")
    ap.add_argument("--verifica-librerie", action="store_true", help="controlla solo le librerie, poi esce")
    ap.add_argument("--debug", action="store_true", help="registro dettagliato")
    ap.add_argument("--versione", action="version", version=f"mesh2tak {VERSIONE}")
    ap.add_argument("--codice-controllo", metavar="CHIAVE.pub",
                    help="mostra il codice di controllo di una chiave del gateway (lo usa l'installatore)")
    nodo = ap.add_argument_group("configurazione del nodo (usata dall'installatore)")
    nodo.add_argument("--configura-nodo", metavar="COLLEGAMENTO",
                      help="imposta regione EU 868, nome, ruolo, canale privato; es. tcp:127.0.0.1:4403")
    nodo.add_argument("--nome", default="Gateway CNSAS Varese")
    nodo.add_argument("--nome-breve", default="GTSA")
    nodo.add_argument("--canale", default="CNSAS")
    nodo.add_argument("--ruolo", default="CLIENT")
    nodo.add_argument("--posizione", default="", help="lat,lon[,quota] della posizione fissa")
    nodo.add_argument("--file-link", default="", help="dove salvare il link del canale")
    nodo.add_argument("--chiave-canale-file", default="",
                      help="file con la chiave (base64) del canale della squadra: il primario prende --canale e questa chiave")
    nodo.add_argument("--parti", default="tutto",
                      help="cosa sistemare sulla radio: tutto, nessuna o un elenco tra " + ", ".join(PARTI_NODO)
                           + " (dalla 1.13.0 gli aggiornamenti non cambiano la radio)")
    nodo.add_argument("--riavvia-nodo", metavar="COLLEGAMENTO",
                      help="riavvia la radio (quella USB: meshtasticd si riavvia con systemctl), poi esce")
    args = ap.parse_args(argv)

    if args.codice_controllo:
        try:
            with open(args.codice_controllo, encoding="utf-8") as f:
                print(codice_controllo(impronta_chiave(f.read())))
            return 0
        except (OSError, ValueError) as e:
            print(f"ERRORE: {e}", file=sys.stderr)
            return 1

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stdout)
    # La libreria meshtastic scrive messaggi tecnici (anche "reconnecting" quando non si ricollega):
    # lo stato del collegamento lo racconta gia' questo programma.
    logging.getLogger("meshtastic").setLevel(logging.DEBUG if args.debug else logging.CRITICAL)

    if args.verifica_librerie:
        return 0 if verifica_librerie() else 1

    if args.riavvia_nodo:
        return riavvia_nodo(args.riavvia_nodo)

    if args.configura_nodo:
        if len(args.canale.encode("utf-8")) > 11:
            print("ERRORE: il nome del canale puo' avere al massimo 11 caratteri")
            return 2
        try:
            parti = leggi_parti(args.parti)
        except ValueError as e:
            print(f"ERRORE: {e}")
            print("ESITO configurazione=errore")
            return 2
        posizione = None
        if args.posizione.strip():
            try:
                posizione = _leggi_posizione(args.posizione)
            except ValueError as e:
                print(f"ERRORE: posizione non valida ({e})")
                return 2
        chiave = None
        if args.chiave_canale_file:
            try:
                chiave = leggi_chiave_canale(open(args.chiave_canale_file, encoding="utf-8").read())
            except (OSError, ValueError) as e:
                print(f"ERRORE: chiave del canale non valida ({e})")
                print("ESITO configurazione=errore")
                return 2
        return configura_nodo(args.configura_nodo, args.nome.strip(), args.nome_breve.strip()[:4],
                              args.canale.strip(), args.ruolo.strip().upper(), posizione,
                              args.file_link or None, chiave_canale=chiave, parti=parti)

    try:
        cfg = carica_config(args.config)
    except ErroreConfig as e:
        log.error("Configurazione: %s", e)
        return 78  # EX_CONFIG: systemd non insiste a riavviare un servizio mal configurato

    if args.prova:
        return prova(cfg)

    try:
        from pubsub import pub
    except ImportError as e:
        log.error("Libreria mancante (%s): rifai l'installazione", e)
        return 1

    try:
        import meshtastic  # noqa: F401  (prima la libreria della radio, poi quella TAK)
        sdk: Optional[LibreriaTak] = LibreriaTak()
    except Exception as e:  # noqa
        sdk = None
        log.error("Libreria TAK di Meshtastic non disponibile (%s): passano solo le posizioni delle radio, "
                  "niente ATAK/TAK Aware, chat e oggetti. Rifai l'installazione", e)

    try:
        tak = ClienteTAK(cfg)
    except (ssl.SSLError, OSError) as e:
        log.error("Certificato o chiave del gateway non leggibili (%s): rifai l'installazione", e)
        return 78
    ponte = Ponte(cfg, tak, sdk)
    ponte.icone = IconeMappa(os.path.join(cfg.accesso_cartella_stato, "icone.json"))
    ponte.copertura = Copertura(os.path.join(cfg.accesso_cartella_stato, "copertura.json"))
    ponte.squadra = RadioSquadra(os.path.join(cfg.accesso_cartella_stato, "radio-squadra.json"))
    ponte.storico = Storico(os.path.join(cfg.accesso_cartella_stato, "storico.json"))
    tak.su_evento = ponte.su_evento_tak
    if os.environ.get("MESH2TAK_ATTESE"):  # solo per le prove automatiche: attese piu' brevi
        f = float(os.environ["MESH2TAK_ATTESE"])
        for nome in ("ATTESA_VERSO_SERVER", "ATTESA_VERSO_RADIO", "ATTESA_VERSO_RADIO_ALLARME"):
            setattr(ponte, nome, tuple(x * f for x in getattr(Ponte, nome)))
        ponte.SPAZIO_TRA_INVII_S = Ponte.SPAZIO_TRA_INVII_S * f
    pub.subscribe(ponte.su_pacchetto, "meshtastic.receive")
    pub.subscribe(ponte.su_perdita, "meshtastic.connection.lost")

    fermo = threading.Event()

    def _ferma(_sig, _frame):
        fermo.set()
        ponte.persa.set()

    signal.signal(signal.SIGTERM, _ferma)
    signal.signal(signal.SIGINT, _ferma)

    log.info("mesh2tak %s avviato: TAK Server %s:%d, %s, canali '%s'", VERSIONE, cfg.tak_host,
             cfg.tak_porta, descrivi_collegamento(cfg.collegamento), cfg.canali)
    log.info("Posizioni: inoltrate appena arrivano, ripetute al TAK Server ogni %g minuti finche' valide "
             "(%g minuti dall'ultimo segnale); riepilogo ogni ora", cfg.aggiornamento_min, cfg.validita_min)
    if sdk is not None:
        log.info("Libreria TAK meshtastic-tak %s; dal TAK Server alle radio: %s", sdk.versione,
                 f"chat e oggetti, al massimo {cfg.tempo_radio_percento:g}% del tempo radio"
                 if cfg.invio_radio else "disattivato (invio = no)")
    tak.avvia()
    ponte.avvia_uscite()
    pausa = Pausa()
    gestione: Optional[Gestione] = None
    if cfg.gestione_attiva:
        try:
            gestione = Gestione(cfg, pausa, ponte.tz)
            gestione.radio = GestioneRadio(ponte)
            gestione.icone = ponte.imposta_icone
            gestione.copertura = ponte.copertura_per_la_pagina
            gestione.storico = ponte.storico.dati
            gestione.avvia()
            if gestione.nodo_pagina:
                dove = [f"pagina nel browser sulla porta {cfg.gestione_porta_pagina}"] if cfg.gestione_porta_pagina else []
                if cfg.gestione_porta_app:
                    dove.append(f"app Meshtastic via Wi-Fi sulla porta {cfg.gestione_porta_app}")
                log.info("Gestione del nodo: %s. Mentre la si usa il gateway va in pausa da solo", ", ".join(dove))
            else:
                log.info("Stato del gateway per la pagina della rete sulla porta %d (radio USB: la radio si gestisce "
                         "dall'app Meshtastic)", cfg.gestione_porta_pagina)
        except Exception as e:  # noqa
            log.error("Gestione del nodo non avviata (%s): il gateway lavora lo stesso", e)
            gestione = None

    accesso: Optional[AccessoEsterno] = None
    if cfg.accesso_errore:
        log.error("Accesso da internet spento: %s. Il gateway lavora lo stesso", cfg.accesso_errore)
    elif cfg.accesso_attivo:
        if gestione is None or not cfg.gestione_porta_pagina:
            log.error("Accesso da internet spento: la pagina del gateway non e' partita")
        else:
            try:
                accesso = AccessoEsterno(cfg, cfg.gestione_porta_pagina)
                accesso.avvia()
                log.info("Accesso da internet: gateway '%s' sulla pagina della rete %s", cfg.accesso_nome,
                         cfg.accesso_rete)
            except Exception as e:  # noqa
                log.error("Accesso da internet non avviato (%s): il gateway lavora lo stesso", e)
                accesso = None
    avviato = time.time()
    sistema = Sistema()
    threading.Thread(target=sistema.ciclo, args=(fermo,), name="sistema", daemon=True).start()
    if gestione is not None:
        gestione.fornisci_stato = lambda: stato_per_la_rete(cfg, ponte, tak, gestione, accesso, avviato, sistema)

    def aspetta(secondi: float) -> bool:
        """Attesa prima di riprovare, interrotta da una pausa; True se il ponte si deve fermare."""
        fine = time.time() + secondi
        while not fermo.is_set() and not pausa.attiva:
            resta = fine - time.time()
            if resta <= 0:
                break
            fermo.wait(min(resta, 1.0))
        return fermo.is_set()

    attesa = 0
    while not fermo.is_set():
        if pausa.attiva:
            pausa.rilasciato.set()   # il nodo e' libero: la pagina puo' usarlo
            while pausa.attiva and not fermo.is_set():
                fermo.wait(0.5)
            ponte.nota_ponte = None
            attesa = 0
            continue
        if attesa and aspetta(attesa):
            break
        if pausa.attiva:
            continue
        try:
            iface = apri_interfaccia(cfg)
        except Exception as e:  # noqa
            attesa = min(max(attesa * 2, 10), 120)
            log.error("Nodo Meshtastic: %s. Riprovo tra %d s", spiega_errore_nodo(e, cfg), attesa)
            continue
        if pausa.attiva:   # chiesta mentre ci si collegava
            try:
                iface.close()
            except Exception:  # noqa
                pass
            continue
        inizio = time.time()
        intervallo = min(15.0, cfg.aggiornamento_min * 30)
        try:
            ponte.collega(iface)
            prossima = time.time() + intervallo
            while not fermo.is_set() and not pausa.attiva and not ponte.persa.wait(timeout=0.5):
                if time.time() >= prossima:
                    ponte.manutenzione()
                    prossima = time.time() + intervallo
            if pausa.attiva and not fermo.is_set():
                ponte.annuncia_pausa(pausa.nota)
        finally:
            ponte.scollega()
            try:
                iface.close()
            except Exception:  # noqa
                pass
        if fermo.is_set():
            break
        if pausa.attiva:
            continue   # nessuna attesa: alla fine della pausa il ponte si ricollega subito
        durata = time.time() - inizio
        attesa = cfg.riconnessione_nodo_s if durata > 300 else min(max(attesa * 2, cfg.riconnessione_nodo_s), 900)
        if cfg.collegamento.startswith("serial"):
            log.warning("La radio USB si e' scollegata dopo %d s: riprovo tra %d s", durata, attesa)
        else:
            log.warning("Il nodo Meshtastic ha chiuso il collegamento dopo %d s. Succede anche quando un'altra "
                        "app si collega al Raspberry via rete: riprovo tra %d s", durata, attesa)
    log.info("Arresto in corso")
    if accesso is not None:
        accesso.ferma()
    if gestione is not None:
        gestione.ferma()
    ponte.ferma_uscite()
    ponte.copertura.salva(subito=True)
    ponte.squadra.salva(subito=True)
    ponte.storico.salva(subito=True)
    tak.ferma()
    return 0


if __name__ == "__main__":
    sys.exit(main())
