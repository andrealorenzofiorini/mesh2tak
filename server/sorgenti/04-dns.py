# --------------------------------------------------------------------------
# DNS
# --------------------------------------------------------------------------

def indirizzi(nome: str) -> List[str]:
    try:
        return sorted({a[4][0] for a in socket.getaddrinfo(nome, None, socket.AF_INET, socket.SOCK_STREAM)})
    except (socket.gaierror, UnicodeError):
        return []


def controlla_dns(imp: Impostazioni, nomi: List[str], ignora: bool) -> None:
    if shutil.which("resolvectl"):
        esegui(["resolvectl", "flush-caches"], controlla=False, attesa=10)
    atteso = imp.server_ip
    problemi = []
    for n in nomi:
        ips = indirizzi(n)
        if not ips:
            problemi.append((n, "non esiste"))
        elif atteso and atteso not in ips:
            problemi.append((n, f"punta a {', '.join(ips)} invece che a {atteso}"))
    if not problemi:
        ok("indirizzi presenti nel DNS: " + ", ".join(nomi))
        return
    for n, p in problemi:
        avviso(f"{n} {p}")
    if ignora:
        avviso("vado avanti lo stesso (--ignora-dns): Caddy avra' il certificato solo quando il DNS e' giusto")
        return
    info("")
    info("Nel DNS del dominio (Cloudflare: dash.cloudflare.com -> " + imp.dominio + " -> DNS -> Records ->")
    info("Add record) aggiungi, uno per riga qui sotto:")
    for n, _ in problemi:
        etichetta = n[:-len(imp.dominio) - 1] if n.endswith("." + imp.dominio) else n
        info(f"    Type A   Name {etichetta}   IPv4 address {atteso or 'IP del server'}   Proxy status: DNS only (nuvola grigia)")
    info("Con un record *." + imp.dominio + " (DNS only) che punta al server non serve aggiungere nulla.")
    info("Poi aspetta un paio di minuti e rilancia lo stesso comando.")
    raise Errore("indirizzi mancanti nel DNS")


