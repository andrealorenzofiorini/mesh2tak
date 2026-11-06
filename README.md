# mesh2tak

Ponte tra una rete radio [Meshtastic](https://meshtastic.org) e un TAK Server installato con infra-TAK.
Nato per una squadra del soccorso alpino (CNSAS): le posizioni, le chat e gli oggetti delle radio in campo
arrivano in ATAK e in sala operativa, e la chat e gli oggetti della sala operativa arrivano alle radio.

## Come è fatto

- **Raspberry Pi** con una radio Meshtastic (via USB o una scheda LoRa sui pin): `mesh2tak.py` porta i
  pacchetti della radio al TAK Server e ritorno. L'installatore prepara il Raspberry da zero.
- **Server infra-TAK**: `rete-meshtastic.sh` aggiunge la pagina della rete (dietro Authentik, solo
  amministratori) con lo stato di tutti i ponti, le radio sentite, gli avvisi per email e gli aggiornamenti.
  I Raspberry arrivano con un tunnel SSH aperto da loro: a casa non si apre nessuna porta.

## Installare

Scarica i file dall'ultima [release](https://github.com/andrealorenzofiorini/mesh2tak/releases/latest).

Sul server della console infra-TAK, una volta sola:

```
sudo bash rete-meshtastic.sh installa
```

Sul Raspberry (Raspberry Pi OS Lite, Debian 12 o 13), con il pacchetto d'integrazione del certificato
scaricato dalla console infra-TAK nella stessa cartella:

```
sudo bash installa-ponte-meshtastic.sh --accesso-esterno
```

La prima volta il Raspberry si riavvia a metà: poi si rilancia lo stesso comando. Alla fine mostra un codice
da confermare sulla pagina della rete. `sudo bash installa-ponte-meshtastic.sh --help` elenca le opzioni.

## Aggiornare

Dalla 1.8.0 non serve il terminale: quando esce una release nuova la pagina della rete lo dice, con le novità.
Prima "Aggiorna il server", poi "Aggiorna i ponti". Server e Raspberry scaricano i file solo dalla release
ufficiale su GitHub e li controllano con `SHA256SUMS`; se dopo l'aggiornamento non ripartono, tornano da soli
alla versione di prima. Le novità di ogni versione sono in [NOVITA.md](NOVITA.md).

## Sviluppo

- `mesh2tak.py`, `scorta.py` (`sudo mesh2tak-scorta`: microSD di scorta), `aggiorna.py` (aggiornamenti dalla
  pagina) e `installa.sh.in`: il Raspberry.
- `server/`: script e pagina della rete per il server infra-TAK.
- `build.py`: costruisce in `dist/` i due file da consegnare, `installa-ponte-meshtastic.sh` e
  `rete-meshtastic.sh`.
- `tests/`: prove automatiche con radio, TAK Server, Authentik e GitHub finti, un Raspberry simulato
  (`tests/prova_installatore.sh`) e una prova completa con sshd e Caddy veri.

Per pubblicare una versione: cambia `VERSIONE` in `mesh2tak.py`, scrivi la sua sezione in `NOVITA.md` e
manda tutto su `main`. GitHub Actions costruisce i file e crea la release `vX.Y.Z` con `SHA256SUMS`.

Nessuna chiave, password, certificato o indirizzo vero sta in questo archivio: restano solo sul Raspberry e
sul server.
