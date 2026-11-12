# --------------------------------------------------------------------------
# Registro dei ponti
# --------------------------------------------------------------------------

def carica_registro() -> dict:
    testo = leggi(P(REGISTRO))
    if not testo:
        return {"formato": 1, "ponti": {}, "authentik": {"ponti": {}}}
    try:
        reg = json.loads(testo)
    except ValueError:
        raise Errore(f"{REGISTRO} e' rovinato: correggilo o cancellalo e rilancia 'aggiorna'")
    reg.setdefault("ponti", {})
    reg.setdefault("authentik", {}).setdefault("ponti", {})
    return reg


def salva_registro(reg: dict) -> None:
    os.makedirs(P(CONF_DIR), exist_ok=True)
    os.chmod(P(CONF_DIR), 0o755)
    scrivi(P(REGISTRO), json.dumps(reg, indent=2, ensure_ascii=False, sort_keys=True) + "\n", 0o600)


def leggi_chiave(testo: str) -> Tuple[str, str]:
    """(riga 'ssh-ed25519 AAAA... commento' pulita, impronta sha256 esadecimale)."""
    parti = testo.strip().split()
    if len(parti) < 2 or parti[0] != "ssh-ed25519":
        raise Errore("la chiave deve essere quella mostrata dal Raspberry: inizia con ssh-ed25519 AAAA...")
    try:
        blob = base64.b64decode(parti[1], validate=True)
    except (binascii.Error, ValueError):
        raise Errore("chiave non valida (copiata a meta'?): ricopia la riga intera mostrata dal Raspberry")
    if not blob.startswith(b"\x00\x00\x00\x0bssh-ed25519") or len(blob) != 51:
        raise Errore("chiave non valida (copiata a meta'?): ricopia la riga intera mostrata dal Raspberry")
    commento = re.sub(r"[^A-Za-z0-9@._-]", "", " ".join(parti[2:]))[:60]
    riga = f"ssh-ed25519 {parti[1]}" + (f" {commento}" if commento else "")
    return riga, hashlib.sha256(blob).hexdigest()


def codice_controllo(impronta: str) -> str:
    """Sei caratteri dall'impronta della chiave (stesso calcolo sul Raspberry): li mostra l'installatore e
    la pagina della rete, per approvare la richiesta giusta."""
    n = int(impronta[:8], 16) >> 2
    c = "".join(CROCKFORD[(n >> (5 * i)) & 31] for i in range(5, -1, -1))
    return f"{c[:3]}-{c[3:]}"


