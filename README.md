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

### Ponte nuovo, senza terminale (dalla 1.10.0)

1. Sulla pagina della rete, una volta: "Certificato e canale per i ponti nuovi" (il pacchetto .zip della console
   infra-TAK, la sua password, nome e chiave del canale della squadra). Restano sul server, leggibili solo da root.
2. "Prepara un ponte nuovo": nome, radio, posizione. La pagina dà le istruzioni da mandare a chi ha il Raspberry,
   con la chiave d'invito.
3. Chi ha il Raspberry, in [Raspberry Pi Imager](https://www.raspberrypi.com/software/) 2.x: "Opzioni app"
   (in basso a sinistra), Repository dei contenuti, Modifica, Usa URL personalizzato
   `https://github.com/andrealorenzofiorini/mesh2tak/releases/latest/download/imager.json`; sistema "Ponte
   Meshtastic (mesh2tak)"; Wi-Fi; in Accesso remoto (SSH, chiave pubblica) incolla la chiave d'invito. Scrive la
   microSD, collega radio e antenna, accende.

Il Raspberry si presenta alla pagina della rete (l'indirizzo è nella chiave d'invito, non nell'immagine), riceve
la sua configurazione via SSH con la sua chiave, scarica l'installatore della versione del server da questa
release, lo controlla con `SHA256SUMS` e lo lancia senza domande. La pagina mostra a che punto è. L'immagine è
Raspberry Pi OS Lite (64 bit) ufficiale più il programma del primo avvio (`primo_avvio.py`), costruita da
`immagine/costruisci.py` a ogni release.

### Ponte installato dal terminale

Sul Raspberry (Raspberry Pi OS Lite, Debian 12 o 13), con il pacchetto d'integrazione del certificato
scaricato dalla console infra-TAK nella cartella dell'utente, oppure copiato sulla microSD (disco "bootfs")
prima di accenderlo:

```
curl -fsSLO https://github.com/andrealorenzofiorini/mesh2tak/releases/latest/download/installa-ponte-meshtastic.sh
sudo bash installa-ponte-meshtastic.sh --accesso-esterno --canale-squadra
```

Con una scheda LoRa sui pin al posto della radio USB si aggiunge `--scheda waveshare` (o `rak`). Le domande
sono tutte all'inizio (password del certificato, posizione, chiave del canale, nome del ponte); il Raspberry
si riavvia a metà e poi continua da solo. Alla fine il ponte chiede di entrare nella rete: si approva sulla
pagina della rete, controllando il codice. `sudo bash installa-ponte-meshtastic.sh --help` elenca le opzioni.

Si può fare anche da lontano: con Raspberry Pi Imager si attiva Raspberry Pi Connect sulla microSD, e i
comandi si lanciano dal terminale remoto di [connect.raspberrypi.com](https://connect.raspberrypi.com).

## Radio della squadra da lontano (dalla 1.10.0)

Nella scheda "Radio" della pagina della rete, "Gestisci da lontano" legge e cambia le impostazioni di una radio del
canale della squadra (come l'app Meshtastic: nome, posizione fissa, sezioni, canali) e la riavvia, con i messaggi di
amministrazione di Meshtastic mandati dal nodo di un ponte. La radio deve avere tra le sue "chiavi amministratore" la
chiave pubblica del nodo del ponte (la pagina la mostra): si mette una volta, da vicino, con l'app. Le chiavi dei
canali non escono dal ponte.

## Aggiornare

Dalla 1.8.0 non serve il terminale: quando esce una release nuova la pagina della rete lo dice, con le novità.
Prima "Aggiorna il server", poi "Aggiorna i ponti". Server e Raspberry scaricano i file solo dalla release
ufficiale su GitHub e li controllano con `SHA256SUMS`; se dopo l'aggiornamento non ripartono, tornano da soli
alla versione di prima. Le novità di ogni versione sono in [NOVITA.md](NOVITA.md).

## Sviluppo

- `mesh2tak.py`, `scorta.py` (`sudo mesh2tak-scorta`: microSD di scorta), `aggiorna.py` (aggiornamenti dalla
  pagina) e `installa.sh.in`: il Raspberry.
- `primo_avvio.py` e `immagine/`: l'immagine per Raspberry Pi Imager e il programma che, al primo avvio, installa
  il ponte nuovo dalla pagina della rete.
- `server/`: script e pagina della rete per il server infra-TAK.
- `build.py`: costruisce in `dist/` i file da consegnare, `installa-ponte-meshtastic.sh`, `rete-meshtastic.sh` e
  `mesh2tak-primo-avvio.py` (che va nell'immagine).
- `tests/`: prove automatiche con radio, TAK Server, Authentik e GitHub finti, un Raspberry simulato
  (`tests/prova_installatore.sh`) e una prova completa con sshd e Caddy veri.

Per pubblicare una versione: cambia `VERSIONE` in `mesh2tak.py`, scrivi la sua sezione in `NOVITA.md` e
manda tutto su `main`. GitHub Actions costruisce i file e l'immagine per Imager e crea la release `vX.Y.Z` con
`SHA256SUMS`.

Nessuna chiave, password, certificato o indirizzo vero sta in questo archivio: restano solo sul Raspberry e
sul server.
