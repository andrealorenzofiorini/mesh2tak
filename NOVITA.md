# Novità

Ogni versione ha qui la sua sezione: quando cambia la versione in `mesh2tak.py`, GitHub costruisce i file e
pubblica la release con queste righe, che la pagina della rete mostra prima di aggiornare.

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
