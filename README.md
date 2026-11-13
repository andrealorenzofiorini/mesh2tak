# mesh2tak

Gateway tra una rete radio [Meshtastic](https://meshtastic.org) e un TAK Server installato con infra-TAK.
Nato per una squadra del soccorso alpino (CNSAS): le posizioni, le chat e gli oggetti delle radio in campo
arrivano in ATAK e in sala operativa, e la chat e gli oggetti della sala operativa arrivano alle radio.

## Come è fatto

- **Raspberry Pi** con una radio Meshtastic (via USB o una scheda LoRa sui pin): `mesh2tak.py` porta i
  pacchetti della radio al TAK Server e ritorno. L'installatore prepara il Raspberry da zero e la radio del gateway
  ("Gateway CNSAS Nome") con solo il canale della squadra; dalla 1.13.0 gli aggiornamenti non toccano più la radio
  (resta quello che si sceglie dall'app), finché non si sceglie "Reimposta la radio".
- **Server infra-TAK**: `rete-meshtastic.sh` aggiunge la pagina della rete (dietro Authentik, solo
  amministratori) con lo stato di tutti i gateway, le radio sentite, gli avvisi per email e gli aggiornamenti.
  I Raspberry arrivano con un tunnel SSH aperto da loro: a casa non si apre nessuna porta.

## Installare

Scarica i file dall'ultima [release](https://github.com/andrealorenzofiorini/mesh2tak/releases/latest).

Sul server della console infra-TAK, una volta sola:

```
sudo bash rete-meshtastic.sh installa
```

### Gateway nuovo, senza terminale (dalla 1.10.0)

1. Sulla pagina della rete, una volta: "Certificato e canale per i gateway nuovi" (il pacchetto .zip della console
   infra-TAK, la sua password, nome e chiave del canale della squadra). Restano sul server, leggibili solo da root.
2. "Prepara un gateway nuovo": nome, radio, posizione. La pagina dà le istruzioni da mandare a chi ha il Raspberry,
   con la chiave d'invito.
3. Chi ha il Raspberry, in [Raspberry Pi Imager](https://www.raspberrypi.com/software/) 2.x: "Opzioni app"
   (in basso a sinistra), Repository dei contenuti, Modifica, Usa URL personalizzato
   `https://github.com/andrealorenzofiorini/mesh2tak/releases/latest/download/imager.json`; sistema "Gateway
   Meshtastic (mesh2tak)"; Wi-Fi; in Accesso remoto (SSH, chiave pubblica) incolla la chiave d'invito. Scrive la
   microSD, collega radio e antenna, accende.

Il Raspberry si presenta alla pagina della rete (l'indirizzo è nella chiave d'invito, non nell'immagine), riceve
la sua configurazione via SSH con la sua chiave, scarica l'installatore della versione del server da questa
release, lo controlla con `SHA256SUMS` e lo lancia senza domande. La pagina mostra a che punto è. L'immagine è
Raspberry Pi OS Lite (64 bit) ufficiale più il programma del primo avvio (`primo_avvio.py`), costruita da
`immagine/costruisci.py` a ogni release.

### Gateway installato dal terminale

Sul Raspberry (Raspberry Pi OS Lite, Debian 12 o 13), con il pacchetto d'integrazione del certificato
scaricato dalla console infra-TAK nella cartella dell'utente, oppure copiato sulla microSD (disco "bootfs")
prima di accenderlo:

```
curl -fsSLO https://github.com/andrealorenzofiorini/mesh2tak/releases/latest/download/installa-ponte-meshtastic.sh
sudo bash installa-ponte-meshtastic.sh --accesso-esterno --canale-squadra
```

Con una scheda LoRa sui pin al posto della radio USB si aggiunge `--scheda waveshare` (o `rak`). Le domande
sono tutte all'inizio (password del certificato, posizione, chiave del canale, nome del gateway); il Raspberry
si riavvia a metà e poi continua da solo. Alla fine il gateway chiede di entrare nella rete: si approva sulla
pagina della rete, controllando il codice. `sudo bash installa-ponte-meshtastic.sh --help` elenca le opzioni.

Si può fare anche da lontano: con Raspberry Pi Imager si attiva Raspberry Pi Connect sulla microSD, e i
comandi si lanciano dal terminale remoto di [connect.raspberrypi.com](https://connect.raspberrypi.com).

## Radio della squadra da lontano (dalla 1.10.0)

Tra le radio della squadra della pagina della rete, "Gestisci da lontano" legge e cambia le impostazioni di una radio del
canale della squadra (come l'app Meshtastic: nome, posizione fissa, sezioni, canali) e la riavvia, con i messaggi di
amministrazione di Meshtastic mandati dal nodo di un gateway. La radio deve avere tra le sue "chiavi amministratore" la
chiave pubblica del nodo del gateway (la pagina la mostra): si mette una volta, da vicino, con l'app. Le chiavi dei
canali non escono dal gateway.

## Icone sulla mappa (dalla 1.11.0)

"Icona sulla mappa", tra le radio della squadra e nella scheda di ogni gateway, sceglie come compare quella radio o quel
gateway su ATAK e TAK Aware: icone dell'insieme OSM che le app hanno di serie, ognuna con un tipo CoT che dà un
simbolo militare sensato a chi non le ha. Il servizio della pagina tiene le scelte e le manda a ogni gateway dal suo
tunnel (`POST /mesh2tak/icone`, accettato solo da lì); il gateway le usa negli eventi che costruisce lui. Le figure
mostrate nella pagina vengono dal catalogo di icone di infra-TAK sul server (`nodered/icon-catalog.json`): il
codice di mesh2tak non le contiene.

## Gestire il Raspberry da lontano (dalla 1.13.0)

"Gestisci il Raspberry", nella scheda di ogni gateway: riavvio del programma, della radio o del Raspberry, registro
(senza chiavi né password), impostazioni del gateway, certificato nuovo del TAK Server, radio come all'installazione.
Il servizio della pagina accoda il comando; il passo dei lavori (root) lo scrive nella registrazione del gateway e lo
avvisa dal tunnel (`POST /mesh2tak/controlla`); sul Raspberry `aggiorna.py` (root, timer e unità `.path`) lo fa e
scrive l'esito, che il gateway mostra nel suo stato. Il certificato nuovo resta sul server leggibile solo dall'utente
SSH di quel gateway, che lo scarica con il comando forzato (`certificato`) e lo fa togliere (`certificato-fatto`).

## Mappa della copertura (dalla 1.14.0)

Ogni gateway annota, per ogni quadretto di circa 200 m (riquadri della mappa a zoom 17), con che segnale ha sentito le
radio della squadra che erano lì: ricezioni dirette (rapporto segnale/rumore medio e migliore) e arrivate tramite
altre radio. Solo quadretto, segnale e giorno, in `/var/lib/mesh2tak/copertura.json`; li dà alla pagina della rete
da `GET /mesh2tak/copertura.json` (solo dal tunnel, con l'utente di Authentik). La mappa della pagina li unisce,
li colora in base al margine sopra il minimo decifrabile del preset e li disegna su una mappa fatta nella pagina
stessa (senza librerie esterne; da fuori arrivano solo le immagini di OpenTopoMap o OpenStreetMap). "Azzera"
(`POST` con `{"azzera": true}`, solo dalla pagina) ricomincia la mappa di un gateway.

## Pagina della rete, gateway e ripetitori (dalla 1.15.0)

Una pagina sola: in alto lo stato di tutta la rete ("Tutto a posto" o quante cose controllare, con l'elenco); poi la
mappa con gateway e ripetitori e il loro stato, le schede di gateway e ripetitori, le radio della squadra e, chiusa
in fondo, la gestione. Il Raspberry con la radio si chiama gateway, come il suo nodo ("Gateway CNSAS ..."); nei nomi dei file e nei comandi resta "ponte".

Le radio della squadra sono solo quelle che il gateway ha decifrato con la chiave del canale della squadra
(`/var/lib/mesh2tak/radio-squadra.json`, tenute 90 giorni): il canale che il database del nodo ricorda per ogni
radio può essere vecchio. Un nodo fisso della squadra (ruolo `ROUTER`, `ROUTER_LATE`, `ROUTER_CLIENT`, `REPEATER`,
`CLIENT_BASE` o posizione impostata a mano) è un ripetitore. Ogni pacchetto porta l'ultimo byte di chi l'ha
ripetuto per ultimo (`relay_node`, firmware 2.6 e successivi): se una sola radio della squadra finisce con quel
byte, il gateway sa quale ripetitore lo ha portato. Le posizioni arrivate con un solo passaggio da un ripetitore fisso
vanno nella copertura di quel ripetitore, senza segnale (quello tra la radio e il ripetitore non si conosce).

## Aggiornare

Dalla 1.8.0 non serve il terminale: quando esce una release nuova la pagina della rete lo dice, con le novità.
Prima "Aggiorna il server", poi "Aggiorna i gateway". Server e Raspberry scaricano i file solo dalla release
ufficiale su GitHub e li controllano con `SHA256SUMS`; se dopo l'aggiornamento non ripartono, tornano da soli
alla versione di prima. Le novità di ogni versione sono in [NOVITA.md](NOVITA.md).

## Sviluppo

- `mesh2tak.py`, `scorta.py` (`sudo mesh2tak-scorta`: microSD di scorta), `aggiorna.py` (aggiornamenti e comandi
  dalla pagina) e `installa.sh.in`: il Raspberry.
- `primo_avvio.py` e `immagine/`: l'immagine per Raspberry Pi Imager e il programma che, al primo avvio, installa
  il gateway nuovo dalla pagina della rete.
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

## Storico e avvisi per i ripetitori (dalla 1.16.0)

Ogni gateway tiene un campione ogni mezz'ora, per 30 giorni (`/var/lib/mesh2tak/storico.json`, senza posizioni): per
sé canale occupato, tempo trasmesso, radio sulla mappa e collegamento al TAK Server; per ogni ripetitore fisso batteria,
tensione, segnale, se lo ha sentito e quanti messaggi ha ripetuto. La pagina li legge da
`GET /mesh2tak/storico.json` (solo dal tunnel, con l'utente di Authentik) e li disegna nella scheda Storico.

Il server, dallo stato dei gateway, avvisa per email quando un ripetitore fisso tace da 12 ore (contando anche i
messaggi che ripete) e quando la sua batteria è al 20% o meno; e quando passa. Se un gateway non risponde, dei
ripetitori non si valuta niente.

## Copertura prevista (dalla 1.17.0)

La pagina calcola, per ogni gateway e ripetitore con una posizione, dove una radio portatile si collegherebbe: per
ogni quadretto della mappa (gli stessi della copertura misurata) entro 10, 20 o 30 km fa il bilancio del
collegamento nei due versi e tiene il peggiore.

- **Modello**: attenuazione nello spazio libero più diffrazione sul profilo del terreno (ostacoli a lama di
  coltello in cascata, ITU-R P.526, metodo di Deygout con tre ostacoli e la sua correzione empirica), Terra con
  raggio 4/3. Rumore da larghezza di banda del preset e cifra di rumore di 6 dB; minimo decifrabile quello che il
  gateway già dichiara (`limite_snr`).
- **Dati veri di ogni impianto**: posizione, preset, regione e potenza impostata li dice il gateway
  (`copertura.json`, campo `radio`); altezza e guadagno dell'antenna, perdite del cavo e potenza li scrivono gli
  amministratori (`POST /api/impianto`, tenuti in `servizio/impianti.json`), per `g:NOME`, `r:!id` e `portatile`.
- **Taratura**: per ogni gateway la correzione `a + 10·b·log10(km)` (quello che il rilievo non vede: alberi, case,
  radio addosso) si ricava dai suoi quadretti misurati con una massima verosimiglianza per dati censurati: i
  quadretti con segnale saturo dicono solo "almeno così", quelli arrivati solo tramite altre radio "meno del
  minimo"; una piccola quota di casi a parte rende il risultato robusto. Senza almeno 5 quadretti utili si usa la
  media dei gateway tarati o, se non ce ne sono, 10 dB a 1 km con `b = 1`.
- **Rilievo**: riquadri "terrarium" (PNG, quota nei tre colori) dei [Terrain Tiles](https://registry.opendata.aws/terrain-tiles/)
  pubblici su AWS, livello 11 (circa 53 m per punto). Li scarica il servizio della pagina
  (`GET /api/terreno/{z}/{x}/{y}.png`, dietro Authentik), al massimo 400 nuovi ogni 10 minuti, e ne tiene fino a
  600 in `servizio/terreno/`. Il browser legge il PNG byte per byte (non da un disegno: alcuni browser alterano
  apposta i colori riletti da un canvas). Il calcolo gira nel browser, a pezzi, senza librerie.

I conti stanno in `server/pagina/sorgenti/js/08b-previsione-calcolo.js` e si provano da soli con
`node tests/prova_previsione.js`. Limiti: è un calcolo sul solo terreno, a 53 m di passo; non conosce alberi, case
e riflessioni, che entrano solo attraverso la taratura. Il rilievo è di Mapzen (Terrain Tiles): in Europa prodotto con
dati e informazioni Copernicus finanziati dall'Unione Europea (EU-DEM) e con dati SRTM per gentile concessione
dell'U.S. Geological Survey ([tutte le fonti](https://github.com/tilezen/joerd/blob/master/docs/attribution.md)); la pagina lo scrive sulla mappa. Per un secondo parere resta il collegamento al
[Site Planner di Meshtastic](https://site.meshtastic.org), sito esterno.

## Prove

`python3 -m pytest tests` (alcune vogliono root, sshd, Caddy e Playwright: senza, si saltano). Le stesse prove girano
su GitHub prima di ogni rilascio (`.github/workflows/rilascio.yml`). `tests/prova_installatore.sh` prova
l'installatore su un Raspberry simulato e si lancia a mano.

## Com'è fatto il repository

I tre file grandi sono scritti a pezzi, uno per argomento, e `python3 build.py` li monta (le prove lo fanno da sole):

- `sorgenti/gateway/NN-*.py` → `mesh2tak.py`, il programma del Raspberry;
- `server/sorgenti/NN-*.py` → `server/rete_meshtastic.py`, il programma del server;
- `server/pagina/sorgenti` (`pagina.html`, `stile.css`, `js/NN-*.js`) → `server/pagina/index.html`, la pagina.

I pezzi si incollano in ordine e non sono moduli: un pezzo da solo non si importa. I file montati non stanno nel
repository; da loro `build.py` fa i file da consegnare in `dist/`.

## Licenza

GPL-3.0: vedi [LICENSE](LICENSE). Copyright (C) 2026 gli autori di mesh2tak. Il programma è distribuito senza
alcuna garanzia.
