def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="rete-meshtastic.sh", description="Rete Meshtastic sul server infra-TAK "
                                 f"(mesh2tak {VERSIONE})")
    sotto = ap.add_subparsers(dest="comando", metavar="COMANDO")
    s = sotto.add_parser("installa", help="una volta sola: pagina della rete e tutto quello che serve")
    s.add_argument("--ignora-dns", action="store_true", help="vai avanti anche se il DNS non e' ancora giusto")
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
