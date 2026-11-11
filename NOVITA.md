# Novità

Ogni versione ha qui la sua sezione: quando cambia la versione in `mesh2tak.py`, GitHub costruisce i file e
pubblica la release con queste righe, che la pagina della rete mostra prima di aggiornare.

## 1.14.0

- Scheda "Copertura" nella pagina della rete: una mappa a quadretti di circa 200 m che dice con che segnale i ponti
  hanno sentito le radio della squadra in ogni punto: buono, discreto, debole, al limite, oppure arrivato solo
  tramite un'altra radio. È una misura, non una stima: un quadretto vuoto vuol dire che lì non è passata nessuna
  radio, non che manca il segnale.
- Tutti i ponti insieme o uno alla volta; tutto il periodo, gli ultimi 30 o 7 giorni oppure oggi; mappa topografica
  (OpenTopoMap) o stradale (OpenStreetMap). Passando sopra un quadretto si legge il segnale di ogni ponte.
  "Scarica per ATAK" dà il file KML da importare in ATAK (o in Google Earth).
- Ogni ponte tiene solo quadretto, segnale e giorno, non quale radio né a che ora. "Azzera" ricomincia la mappa di
  un ponte, per esempio dopo aver spostato l'antenna. La mappa si vede solo dalla pagina della rete, dietro
  Authentik.
- La mappa si riempie dalle posizioni che arrivano dopo l'aggiornamento del ponte alla 1.14.0.

## 1.13.0

- Gli aggiornamenti non cambiano più le impostazioni della radio del ponte: resta quello che si è scelto dall'app
  Meshtastic o dalla pagina, compreso un canale secondario acceso. Il ponte non spegne più i canali secondari da
  solo. Per rimettere la radio come la prepara l'installatore (regione, nome, ruolo, solo il canale della squadra,
  posizione fissa) c'è "Reimposta la radio", oppure la casella "Rimetti anche la radio" quando si aggiorna.
- "Gestisci il Raspberry" nella scheda di ogni ponte: riavvia il programma, la radio o tutto il Raspberry; legge il
  registro (senza chiavi né password) e lo scarica; cambia le impostazioni del ponte (colore della squadra, minuti
  sulla mappa, invio alle radio, tempo radio, pausa massima); sostituisce il certificato del TAK Server caricando il
  pacchetto della console infra-TAK; reimposta la radio. Sotto, com'è andato ogni comando.
- I comandi li fa il Raspberry stesso, entro pochi secondi: il server li scrive nella registrazione del ponte e lo
  avvisa dal tunnel. Il certificato nuovo il Raspberry lo scarica con la sua chiave; il server lo cancella appena
  installato (al più tardi dopo un giorno). Un comando arrivato mentre il Raspberry era spento scade dopo mezz'ora.
  Se con le impostazioni nuove il ponte non riparte, il Raspberry rimette quelle di prima.
- Gli aggiornamenti dalla pagina partono subito, non entro 2 minuti (per i ponti già alla 1.13.0: il passaggio
  dalla 1.12.0 parte ancora entro 2 minuti).

## 1.12.0

- Il ponte si chiama "Gateway": il ponte di casa diventa "Gateway CNSAS Varese" (sigla GTSA), i ponti nuovi
  "Gateway CNSAS Nome" (G e tre lettere, es. GVEL). Il nome cambia sul nodo e sulla mappa con l'aggiornamento.
- Il nodo di ogni ponte ha solo il canale della squadra: il canale secondario (il pubblico LongFast) si spegne
  con l'aggiornamento e ogni volta che il ponte si collega al suo nodo, anche se qualcuno lo rimette dall'app.
  Il QR del canale contiene solo la squadra. Tolta dalla pagina la voce "Canale pubblico secondario";
  l'opzione `--canale-pubblico` dell'installatore si ignora.

## 1.11.0

- Icona sulla mappa per ogni radio e ogni ponte, scelta dalla pagina della rete: "Icona sulla mappa" nella scheda
  "Radio" e nella scheda di ogni ponte. Persone (soccorritore, squadra a piedi, sanitario, unità cinofila, sci,
  bici), mezzi (auto, ambulanza, vigili del fuoco, moto, elicottero), basi con i simboli ICS (posto di comando,
  base, campo, area di raccolta, elibase, piazzola), luoghi (traliccio, rifugio, bivacco, tenda, ospedale) e
  pallini colorati.
- Le icone sono quelle che ATAK e TAK Aware hanno di serie (insieme OSM, lo stesso del traliccio dei ponti): chi
  non le ha vede il simbolo militare più vicino. La scelta vale per tutti i ponti: il server la manda a ognuno
  dal suo tunnel, anche a quelli spenti appena tornano.

## 1.10.0

- Ponti nuovi senza terminale: sulla pagina della rete si caricano una volta certificato e canale, poi per ogni
  Raspberry si prepara un invito. Chi ha il Raspberry scrive la microSD con Raspberry Pi Imager (elenco
  "Ponte Meshtastic", da questa release), incolla la chiave d'invito tra le chiavi SSH e lo accende con la radio:
  il Raspberry si presenta da solo, riceve certificato e canale cifrati, si installa e compare tra i ponti. La
  pagina mostra a che punto è e, se qualcosa va storto, "Riprova l'installazione".
- Certificato e chiave del canale restano sul server leggibili solo da root; ogni ponte nuovo può scaricare solo
  la sua configurazione, con la sua chiave, e a installazione finita viene cancellata. Nell'immagine non c'è
  nessun indirizzo né chiave.
- Radio della squadra gestite da lontano: nella scheda "Radio", "Gestisci da lontano" legge e cambia nome,
  posizione fissa, tutte le impostazioni (come nell'app Meshtastic) e i canali, e riavvia la radio, attraverso il
  nodo di un ponte. Serve una volta, da vicino, la chiave di amministrazione del ponte sulla radio (firmware 2.5 o
  successivo). Le chiavi dei canali non escono mai dal ponte.
- Nell'elenco delle radio e sulla mappa solo quelle del canale della squadra: le radio estranee sentite sul canale
  pubblico non entrano più nemmeno al riavvio del ponte.
- Niente avvisi "ponte che non risponde" per un ponte nuovo che si sta ancora installando.

## 1.9.0

- Ponti nuovi installati da casa: si prepara la microSD con Raspberry Pi Imager (con Raspberry Pi Connect)
  e ci si copia il pacchetto del certificato; poi, dal terminale remoto di connect.raspberrypi.com, un solo
  comando scarica l'installatore da GitHub e lo avvia.
- Il certificato si trova anche sulla microSD (disco "bootfs") e dopo l'installazione viene cancellato da lì.
- Dopo il riavvio a metà l'installazione continua da sola: non serve ricollegarsi per rilanciarla.
- Ogni ponte nuovo ha il suo nome sulla radio e sulla mappa: "Ponte CNSAS Velate" (sigla PVEL) per il ponte
  "velate". I ponti già installati tengono il loro nome.
- Raspberry Pi Connect resta raggiungibile anche dopo i riavvii del Raspberry.

## 1.8.0

- Aggiornamenti dalla pagina della rete: quando esce una versione nuova la pagina lo dice, con le novità.
  "Aggiorna il server" e poi "Aggiorna i ponti": niente più terminale.
- Il server e i Raspberry scaricano la versione nuova solo dalla release ufficiale su GitHub e controllano
  che il file sia quello pubblicato. La pagina della rete può soltanto chiedere l'aggiornamento.
- Se dopo l'aggiornamento il server o un ponte non ripartono, tornano da soli alla versione di prima.
- Mai verso una versione più vecchia; ogni ponte riprova al massimo tre volte per ogni richiesta.
- Nella scheda di ogni ponte: esito dell'ultimo aggiornamento.
- L'indirizzo del TAK Server va sempre indicato (nessun indirizzo predefinito).

## 1.7.0

- Riavvio automatico se il Raspberry si blocca (watchdog), registri di sistema limitati, aggiornamenti di
  sicurezza automatici ogni notte con meshtasticd fermo alla versione installata.
- `sudo mesh2tak-scorta`: copia del sistema su una microSD di scorta, in un lettore USB.
- Avvisi per email dalla pagina della rete, con la posta di infra-TAK: ponte che non risponde, radio o TAK
  Server scollegati, certificato in scadenza, corrente insufficiente, microSD quasi piena, riavvii.

## 1.6.0

- Scheda "Radio" nella pagina della rete: le radio della squadra sentite da ogni ponte, con batteria,
  segnale e ultimo contatto.

## Versioni precedenti

- 1.5.0: i ponti si aggiungono dalla pagina della rete (richiesta del Raspberry e approvazione).
- 1.4.0: accesso da internet ai ponti e pagina della rete sul server infra-TAK.
- 1.3.x: gestione del nodo dal browser e dall'app Meshtastic attraverso il ponte; icona del ponte.
- 1.2.x: le due direzioni (dalla radio al TAK Server e ritorno), configurazione del nodo più robusta.
