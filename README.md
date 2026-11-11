# mesh2tak

Ponte tra una rete radio [Meshtastic](https://meshtastic.org) e un TAK Server installato con infra-TAK.
Nato per una squadra del soccorso alpino (CNSAS): le posizioni, le chat e gli oggetti delle radio in campo
arrivano in ATAK e in sala operativa, e la chat e gli oggetti della sala operativa arrivano alle radio.

## Come è fatto

- **Raspberry Pi** con una radio Meshtastic (via USB o una scheda LoRa sui pin): `mesh2tak.py` porta i
  pacchetti della radio al TAK Server e ritorno. L'installatore prepara il Raspberry da zero e la radio del ponte
  ("Gateway CNSAS Nome") con solo il canale della squadra; dalla 1.13.0 gli aggiornamenti non toccano più la radio
  (resta quello che si sceglie dall'app), finché non si sceglie "Reimposta la radio".
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

Tra le radio della squadra della pagina della rete, "Gestisci da lontano" legge e cambia le impostazioni di una radio del
canale della squadra (come l'app Meshtastic: nome, posizione fissa, sezioni, canali) e la riavvia, con i messaggi di
amministrazione di Meshtastic mandati dal nodo di un ponte. La radio deve avere tra le sue "chiavi amministratore" la
chiave pubblica del nodo del ponte (la pagina la mostra): si mette una volta, da vicino, con l'app. Le chiavi dei
canali non escono dal ponte.

## Icone sulla mappa (dalla 1.11.0)

"Icona sulla mappa", tra le radio della squadra e nella scheda di ogni gateway, sceglie come compare quella radio o quel
ponte su ATAK e TAK Aware: icone dell'insieme OSM che le app hanno di serie, ognuna con un tipo CoT che dà un
simbolo militare sensato a chi non le ha. Il servizio della pagina tiene le scelte e le manda a ogni ponte dal suo
tunnel (`POST /mesh2tak/icone`, accettato solo da lì); il ponte le usa negli eventi che costruisce lui. Le figure
mostrate nella pagina vengono dal catalogo di icone di infra-TAK sul server (`nodered/icon-catalog.json`): il
codice di mesh2tak non le contiene.

## Gestire il Raspberry da lontano (dalla 1.13.0)

"Gestisci il Raspberry", nella scheda di ogni ponte: riavvio del programma, della radio o del Raspberry, registro
(senza chiavi né password), impostazioni del ponte, certificato nuovo del TAK Server, radio come all'installazione.
Il servizio della pagina accoda il comando; il passo dei lavori (root) lo scrive nella registrazione del ponte e lo
avvisa dal tunnel (`POST /mesh2tak/controlla`); sul Raspberry `aggiorna.py` (root, timer e unità `.path`) lo fa e
scrive l'esito, che il ponte mostra nel suo stato. Il certificato nuovo resta sul server leggibile solo dall'utente
SSH di quel ponte, che lo scarica con il comando forzato (`certificato`) e lo fa togliere (`certificato-fatto`).

## Mappa della copertura (dalla 1.14.0)

Ogni ponte annota, per ogni quadretto di circa 200 m (riquadri della mappa a zoom 17), con che segnale ha sentito le
radio della squadra che erano lì: ricezioni dirette (rapporto segnale/rumore medio e migliore) e arrivate tramite
altre radio. Solo quadretto, segnale e giorno, in `/var/lib/mesh2tak/copertura.json`; li dà alla pagina della rete
da `GET /mesh2tak/copertura.json` (solo dal tunnel, con l'utente di Authentik). La mappa della pagina li unisce,
li colora in base al margine sopra il minimo decifrabile del preset e li disegna su una mappa fatta nella pagina
stessa (senza librerie esterne; da fuori arrivano solo le immagini di OpenTopoMap o OpenStreetMap). Esporta il KML
per ATAK; "Azzera" (`POST` con `{"azzera": true}`, solo dalla pagina) ricomincia la mappa di un ponte.

## Pagina della rete, gateway e ripetitori (dalla 1.15.0)

Una pagina sola: in alto lo stato di tutta la rete ("Tutto a posto" o quante cose controllare, con l'elenco); poi la
mappa con gateway e ripetitori e il loro stato, le schede di gateway e ripetitori, le radio della squadra e, chiusa
in fondo, la gestione. Nella pagina il ponte si chiama gateway, come il suo nodo ("Gateway CNSAS ...").

Le radio della squadra sono solo quelle che il ponte ha decifrato con la chiave del canale della squadra
(`/var/lib/mesh2tak/radio-squadra.json`, tenute 90 giorni): il canale che il database del nodo ricorda per ogni
radio può essere vecchio. Un nodo fisso della squadra (ruolo `ROUTER`, `ROUTER_LATE`, `ROUTER_CLIENT`, `REPEATER`,
`CLIENT_BASE` o posizione impostata a mano) è un ripetitore. Ogni pacchetto porta l'ultimo byte di chi l'ha
ripetuto per ultimo (`relay_node`, firmware 2.6 e successivi): se una sola radio della squadra finisce con quel
byte, il ponte sa quale ripetitore lo ha portato. Le posizioni arrivate con un solo passaggio da un ripetitore fisso
vanno nella copertura di quel ripetitore, senza segnale (quello tra la radio e il ripetitore non si conosce).

## Aggiornare

Dalla 1.8.0 non serve il terminale: quando esce una release nuova la pagina della rete lo dice, con le novità.
Prima "Aggiorna il server", poi "Aggiorna i ponti". Server e Raspberry scaricano i file solo dalla release
ufficiale su GitHub e li controllano con `SHA256SUMS`; se dopo l'aggiornamento non ripartono, tornano da soli
alla versione di prima. Le novità di ogni versione sono in [NOVITA.md](NOVITA.md).

## Sviluppo

- `mesh2tak.py`, `scorta.py` (`sudo mesh2tak-scorta`: microSD di scorta), `aggiorna.py` (aggiornamenti e comandi
  dalla pagina) e `installa.sh.in`: il Raspberry.
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
