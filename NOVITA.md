# Novità

Ogni versione ha qui la sua sezione: quando cambia la versione in `mesh2tak.py`, GitHub costruisce i file e
pubblica la release con queste righe, che la pagina della rete mostra prima di aggiornare.

## 1.17.1

- Tolto «Scarica per ATAK» dalla mappa della rete: la copertura, misurata e prevista, si guarda sulla pagina.

## 1.17.0

- Copertura prevista calcolata dalla pagina: con «Copertura prevista», sopra la mappa, la pagina calcola dal rilievo del terreno dove una radio portatile si collegherebbe a ogni gateway e a ogni ripetitore, anche dove nessuno è ancora passato. I quadretti previsti stanno sotto a quelli misurati, con gli stessi colori più chiari; un cerchio tratteggiato dice fin dove è arrivato il calcolo (10, 20 o 30 km).
- Ogni impianto con i suoi dati veri: posizione, potenza e impostazione radio le legge dal gateway; altezza e guadagno dell'antenna, perdite del cavo e potenza si scrivono in «Dati dell'impianto», per ogni gateway, per ogni ripetitore e per le radio portatili. Finché non li scrivi usa valori di fabbrica, e lo dice.
- Tarata sulle misure: il calcolo di ogni gateway viene corretto con i quadretti dove quel gateway ha sentito davvero le radio, e la pagina dice su quanti quadretti e con che errore tipico. Contano anche i quadretti dove il segnale era troppo forte per essere misurato e quelli arrivati solo tramite altre radio. I ripetitori, che non misurano, usano la correzione media dei gateway; per loro la pagina dice quanti dei quadretti arrivati davvero tramite quel ripetitore cadono nella zona prevista, e con che margine si collega a ogni gateway.
- «Prova un punto»: tocchi la mappa dove vorresti mettere un ripetitore e la pagina dice quanta zona aggiungerebbe alla rete di oggi e se arriverebbe ai gateway. Il punto resta finché la pagina è aperta.
- «Scarica per ATAK» porta anche la copertura prevista, in una cartella a parte che in ATAK si accende e si spegne da sola.
- Il rilievo (quote del terreno, dati pubblici) lo scarica il server una volta sola e lo tiene: chi apre la pagina non parla con siti nuovi. È un calcolo sul solo terreno: alberi e case li recupera la taratura, quindi vale di più dove ci sono già misure vicine. Serve aggiornare sia il server sia i gateway.

## 1.16.2

- Controllo degli aggiornamenti: se GitHub rifiuta le richieste del server (succede quando altri programmi dello stesso server ne fanno troppe in un'ora), la versione nuova si legge lo stesso da un altro indirizzo di GitHub che non ha quel limite.
- Se il controllo non riesce, ora c'è scritto accanto a "Controlla adesso" (prima la pagina continuava a dire "Tutto aggiornato").

## 1.16.1

- Schede ridotte a quattro: Rete, Radio, Storico, Gestione. Mappa, gateway e ripetitori stanno insieme nella scheda Rete, una sotto l'altra; i vecchi indirizzi (#mappa, #gateway, #ripetitori) portano al punto giusto.

## 1.16.0

- Avvisi per email anche per i ripetitori: quando un ripetitore non si sente più da 12 ore e quando la sua batteria scende al 20% (e quando tornano a posto). Valgono per i nodi fissi che i gateway sentono; se un gateway non risponde, dei ripetitori non si dice niente.
- Storico: nuova scheda con i grafici degli ultimi 30 giorni, un punto ogni mezz'ora. Per ogni ripetitore batteria, segnale verso il gateway e quando è stato sentito; per ogni gateway canale radio occupato e radio sulla mappa TAK. Lo storico parte dall'aggiornamento del gateway.
- La pagina torna a schede: Mappa, Gateway, Ripetitori, Radio, Storico, Gestione. Lo stato di tutta la rete e l'elenco delle cose da controllare restano sempre in alto, e portano alla scheda giusta.
- Copertura prevista: nella scheda di ogni gateway e ripetitore un collegamento apre il Site Planner di Meshtastic già impostato con posizione, nome e frequenza, per stimare dal rilievo dove arriva. È un sito esterno, che riceve posizione e nome; nella mappa c'è anche il collegamento a MeshPeaks per cercare dove mettere un ripetitore.
- Un nome solo: anche email, installatore e messaggi dicono "gateway" invece di "ponte". Nomi dei file e comandi non cambiano. In Raspberry Pi Imager il sistema ora si chiama "Gateway Meshtastic (mesh2tak)".
- Licenza GPL-3.0. Le prove automatiche ora girano su GitHub prima di ogni rilascio: se una non passa, la versione non esce.

## 1.15.0

- Pagina della rete rifatta, tutta in una pagina: in alto si legge subito se va tutto bene ("Tutto a posto") o quante cose controllare, con l'elenco che porta al punto giusto. Sotto, la mappa della rete con accanto gateway e ripetitori e il loro stato; poi le loro schede (in vista solo quello che conta, il resto in "Altri dettagli"), le radio della squadra e, chiusa in fondo, la gestione: aggiungere un gateway, avvisi per email, aggiornamenti. Sul telefono una colonna sola. Nella pagina il ponte ora si chiama gateway, come il suo nodo.
- Radio della squadra: contano solo quelle che trasmettono davvero sul canale della squadra (prima comparivano anche quelle sentite quando il canale principale era LongFast: per esempio 16 invece di 1). L'elenco riparte vuoto con l'aggiornamento e si riempie da solo appena le radio trasmettono; le radio estranee non vanno sulla mappa TAK neanche quando il gateway riparte.
- Ripetitori: un nodo fisso della squadra (ruolo Router o Repeater, oppure posizione impostata a mano) ha la sua scheda: batteria, quando si è fatto sentire, quando ha ripetuto un messaggio verso il gateway, segnale con il gateway, copertura. Se non manda la batteria, la scheda dice come attivarla sul ripetitore.
- Copertura: le posizioni arrivate al gateway con un solo passaggio da un ripetitore fisso contano per quel ripetitore ("Tramite un ripetitore", in blu). "Mostra" fa vedere tutta la rete, un gateway o un ripetitore; sulla mappa i gateway sono quadrati e i ripetitori cerchi, colorati se c'è qualcosa da guardare.

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
