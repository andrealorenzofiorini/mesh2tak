def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="rete-meshtastic.sh", description="Rete Meshtastic sul server infra-TAK o "
                                 f"dentro lo stack takit (mesh2tak {VERSIONE})")
    sotto = ap.add_subparsers(dest="comando", metavar="COMANDO")
    s = sotto.add_parser("installa", help="una volta sola: pagina della rete e tutto quello che serve")
    s.add_argument("--ignora-dns", action="store_true", help="vai avanti anche se il DNS non e' ancora giusto")
    # dalla 1.21.0: dentro lo stack takit invece che accanto a infra-TAK (lo lancia il modulo rete-radio dello stack)
    s.add_argument("--takit-stack", action="store_true", help="dentro lo stack takit (lo usa il modulo rete-radio)")
    s.add_argument("--dominio", default="", help="con --takit-stack: il dominio dello stack, es. takit.org")
    s.add_argument("--takit-radice", default="", help="con --takit-stack: la cartella dello stack, es. /opt/takit-stack")
    s.add_argument("--takit-impianto", default="", help="con --takit-stack: il file dell'impianto dello stack")
    s.add_argument("--takit-pagine", default="", help="con --takit-stack (dalla 1.22.0): dove scrivere l'elenco dei "
                   "gateway, perche' lo stack pubblichi l'indirizzo di ognuno")
    s.add_argument("--takit-icone", default="", help="con --takit-stack (dalla 1.22.0): il catalogo delle icone di "
                   "ATAK che lo stack ha scaricato (lo stesso di infra-TAK), per le figure nella pagina")
    s.set_defaults(fn=cmd_installa)
    s = sotto.add_parser("aggiungi", help="registra o aggiorna un gateway a mano")
    s.add_argument("nome", help="nome del gateway, es. casa")
    s.add_argument("chiave", help="chiave pubblica del Raspberry ('ssh-ed25519 AAAA...'), tra apici")
    s.add_argument("--ignora-dns", action="store_true", help="vai avanti anche se il DNS non e' ancora giusto")
    s.set_defaults(fn=cmd_aggiungi)
    s = sotto.add_parser("elenco", help="gateway registrati e tunnel collegati")
    s.set_defaults(fn=cmd_elenco)
    s = sotto.add_parser("rimuovi", help="toglie un gateway")
    s.add_argument("nome")
    s.add_argument("--si", action="store_true", help="senza chiedere conferma")
    s.set_defaults(fn=cmd_rimuovi)
    s = sotto.add_parser("aggiorna", help="come installa, dopo aver aggiornato lo script")
    s.add_argument("--ignora-dns", action="store_true", help="vai avanti anche se il DNS non e' ancora giusto")
    s.set_defaults(fn=cmd_aggiorna)
    s = sotto.add_parser("disinstalla", help="toglie tutto quello che ha messo questo script")
    s.add_argument("--si", action="store_true", help="senza chiedere conferma")
    s.set_defaults(fn=cmd_disinstalla)
    s = sotto.add_parser("certificato-convertitore", help="(dalla 1.23.0) il certificato con cui questo server manda "
                         "al suo TAK Server i dati dei gateway in ascolto")
    s.add_argument("--pacchetto", required=True, help="il .zip scaricato dalla console, oppure un .p12")
    s.add_argument("--password", default="", help="password del certificato (se manca: atakatak)")
    s.add_argument("--password-da", default="", help="file da cui leggere la password, invece di scriverla qui")
    s.add_argument("--host", default="", help="indirizzo del TAK Server, se il pacchetto non lo dice")
    s.add_argument("--porta", type=int, default=0, help="porta del TAK Server (se manca: quella del pacchetto, o 8089)")
    s.set_defaults(fn=cmd_certificato_convertitore)
    s = sotto.add_parser("servi", help="(lo usa il servizio della pagina, non lanciarlo a mano)")
    s.set_defaults(fn=cmd_servi)
    s = sotto.add_parser("lavora", help="(lo usa il servizio dei lavori, non lanciarlo a mano)")
    s.set_defaults(fn=cmd_lavora)
    s = sotto.add_parser("ponte", help="(lo lancia sshd per i Raspberry dei gateway, non lanciarlo a mano)")
    s.set_defaults(fn=cmd_ponte)
    a = ap.parse_args(argv)
    if not getattr(a, "fn", None):
        ap.print_help()
        return 2
    if a.comando in ("servi", "ponte"):
        return a.fn(a)                                         # senza privilegi, niente registro di root
    if os.geteuid() != 0 and not RADICE:
        print("Va avviato da root:  sudo bash rete-meshtastic.sh " + " ".join(sys.argv[1:2]))
        return 1
    registra(f"=== rete-meshtastic {VERSIONE}: {a.comando}")
    try:
        return a.fn(a)
    except Errore as e:
        registra(f"ERRORE: {e}")
        print(f"\n{R}✘ {e}{N}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrotto.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
