"use strict";
const OGNI_S = 15;                 // stato dei ponti
const ELENCO_OGNI_S = 120;         // elenco dei ponti
const MOSTRATE = 12;               // radio visibili prima di "Mostra tutte"
let elenco = [];
let ultimoElenco = 0;
let versione = "";
const stati = new Map();           // nome -> {dati, quando, errore, ultimoOk}
let gestione = null;               // /api/stato: richieste dei Raspberry e lavori; null = servizio non raggiungibile
let erroreGestione = "";
let riavvio = 0;                   // da quando l'API non risponde mentre un lavoro e' in corso (Authentik riparte)
let erroreGiro = "";
let daTogliere = null;
const COMANDO_PI = "sudo bash installa-ponte-meshtastic.sh --accesso-esterno";
const dettagliAperti = new Set();  // "Dettagli" aperti: restano aperti quando la pagina si aggiorna
// Dalla 1.15.0 una pagina sola, dall'alto in basso: cosa non va, richieste e lavori, la mappa della rete con accanto
// gateway e ripetitori, le loro schede, le radio della squadra e, chiusa in fondo, la gestione.
const zona = document.getElementById("ponti");
const avvisoVersione = document.createElement("div");   // in cima: versione nuova o pagina da ricaricare
const zonaStato = document.createElement("div");        // le cose da controllare, dalla piu' grave
const zonaNovita = document.createElement("div");
const panorama = document.createElement("section");     // mappa della rete ed elenco di gateway e ripetitori
const vistaCopertura = document.createElement("div");   // la mappa (copertura dalla 1.14.0)
const zonaPonti = document.createElement("section");    // schede dei gateway
const zonaRipetitori = document.createElement("section");
const vistaRadio = document.createElement("section");   // radio della squadra
const vistaStorico = document.createElement("section"); // andamento di gateway e ripetitori (dalla 1.16.0)
const zonaGestione = document.createElement("section");
const vistaRete = document.createElement("div");       // scheda Rete: mappa, gateway e ripetitori insieme
const schede = document.createElement("nav");           // dalla 1.16.0 di nuovo a schede: una cosa alla volta
avvisoVersione.className = "avviso-versione";
avvisoVersione.hidden = true;
vistaRete.append(panorama, zonaPonti, zonaRipetitori);
zona.append(avvisoVersione, zonaStato, zonaNovita, schede, vistaRete, vistaRadio, vistaStorico, zonaGestione);
// le schede: nome nell'indirizzo (#rete, #squadra...), titolo, la sua parte di pagina. Dalla 1.16.1 mappa, gateway e
// ripetitori stanno insieme in "Rete"; #mappa, #gateway e #ripetitori portano al punto giusto di quella scheda.
const SCHEDE = [["rete", "Rete", vistaRete], ["squadra", "Radio", vistaRadio], ["storico", "Storico", vistaStorico],
                ["gestione", "Gestione", zonaGestione]];
const bottoniSchede = {};
let schedaAperta = "rete";
const SILENZIO_S = 2 * 3600;       // una radio accesa si fa sentire almeno ogni mezz'ora (batteria e canale)

function el(tag, classe, testo) {
  const e = document.createElement(tag);
  if (classe) e.className = classe;
  if (testo !== undefined && testo !== null) e.textContent = String(testo);
  return e;
}

function maiuscola(s) {
  s = String(s || "");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function minuscola(s) {          // solo la prima lettera: "TAK Server" resta com'e'
  s = String(s || "");
  return s.charAt(0).toLowerCase() + s.slice(1);
}

function dettagli(chiave, titolo, ...contenuto) {
  const det = el("details");
  det.open = dettagliAperti.has(chiave);
  det.addEventListener("toggle", () => { det.open ? dettagliAperti.add(chiave) : dettagliAperti.delete(chiave); });
  det.append(el("summary", null, titolo), ...contenuto);
  return det;
}

function numIt(x, cifre) {
  return Number(x || 0).toLocaleString("it-IT", {maximumFractionDigits: cifre === undefined ? 1 : cifre});
}

const RUOLI = {CLIENT: "normale", CLIENT_MUTE: "non ripete", CLIENT_HIDDEN: "nascosta", CLIENT_BASE: "base",
               TRACKER: "localizzatore", TAK: "con ATAK", TAK_TRACKER: "localizzatore TAK", ROUTER: "ripetitore",
               ROUTER_LATE: "ripetitore di riserva", ROUTER_CLIENT: "ripetitore", REPEATER: "ripetitore puro",
               SENSOR: "sensore", LOST_AND_FOUND: "smarrita"};
const MODELLI = {TRACKER_T1000_E: "SenseCAP T1000-E", RAK4631: "RAK WisBlock 4631", RAK11310: "RAK WisBlock 11310",
                 WISMESH_TAP: "RAK WisMesh Tap", HELTEC_V3: "Heltec V3", HELTEC_WSL_V3: "Heltec Wireless Stick Lite V3",
                 HELTEC_WIRELESS_TRACKER: "Heltec Wireless Tracker", HELTEC_WIRELESS_PAPER: "Heltec Wireless Paper",
                 TBEAM: "LilyGO T-Beam", LILYGO_TBEAM_S3_CORE: "LilyGO T-Beam S3", T_ECHO: "LilyGO T-Echo",
                 T_DECK: "LilyGO T-Deck", TLORA_V2_1_1P6: "LilyGO T-LoRa", STATION_G2: "Station G2",
                 NANO_G2_ULTRA: "Nano G2 Ultra", SEEED_XIAO_S3: "Seeed XIAO S3", PORTDUINO: "Raspberry (meshtasticd)",
                 HELTEC_MESH_SOLAR: "Heltec MeshSolar", SEEED_SOLAR_NODE: "SenseCAP Solar Node", T_ECHO_LITE: "LilyGO T-Echo Lite"};

function nomeModello(m) {
  if (!m) return "";
  if (/^THINKNODE_M\d+$/.test(m)) return "ThinkNode " + m.slice(10);     // Elecrow ThinkNode M1, M6...
  return MODELLI[m] || m.split("_").map(w => /\d/.test(w) ? w : w.charAt(0) + w.slice(1).toLowerCase()).join(" ");
}

function qualita(snr, rssi) {   // come l'app Meshtastic: il rapporto segnale/rumore conta piu' della potenza
  if (snr === null || snr === undefined) return "";
  if (snr > -7 && (rssi === null || rssi === undefined || rssi > -115)) return "buono";
  if (snr > -15 && (rssi === null || rssi === undefined || rssi > -126)) return "discreto";
  return "debole";
}

function frase(s) {               // un messaggio del server come frase: un solo segno in fondo
  s = String(s || "").trim();
  return /[.!?]$/.test(s) ? s : s + ".";
}

function oraDi(ts) {
  return new Date(ts * 1000).toLocaleTimeString("it-IT", {hour: "2-digit", minute: "2-digit"});
}

function giornoOra(ts) {
  const d = new Date(ts * 1000);
  const oggi = new Date();
  const giorno = d.toDateString() === oggi.toDateString() ? "oggi" : "il " + d.toLocaleDateString("it-IT", {day: "numeric", month: "numeric"});
  return giorno + " alle " + oraDi(ts);
}

function durata(s) {
  if (!Number.isFinite(s)) return "?";          // dato che manca nello stato del ponte
  s = Math.max(0, Math.round(s));
  if (s < 90) return s < 60 ? "meno di un minuto" : "1 minuto";
  const m = Math.round(s / 60);
  if (m < 60) return m + " minuti";
  const h = Math.floor(m / 60), r = m % 60;
  if (h < 24) return h + (h === 1 ? " ora" : " ore") + (r ? " e " + r + " min" : "");
  const g = Math.floor(h / 24);
  return g === 1 ? "1 giorno" : g + " giorni";
}

function fa(s) {
  if (s < 60) return "adesso";
  const m = Math.round(s / 60);
  if (m < 60) return m + " min fa";
  const h = Math.floor(m / 60), r = m % 60;
  if (h < 24) return h + " h" + (r ? " " + r + " min" : "") + " fa";
  const g = Math.floor(h / 24);
  return g === 1 ? "ieri" : g + " giorni fa";
}

async function prendi(url) {
  const r = await fetch(url, {cache: "no-store", redirect: "manual", credentials: "same-origin"});
  if (r.type === "opaqueredirect" || r.status === 401 || r.status === 403) {
    document.getElementById("sessione").hidden = false;
    throw new Error("sessione scaduta");
  }
  if (!r.ok) {
    await r.text().catch(() => "");      // una risposta lasciata a meta' tiene la richiesta aperta
    throw new Error("risposta " + r.status);
  }
  return r.json();
}

async function manda(azione, dati) {
  const r = await fetch("api/" + azione, {method: "POST", cache: "no-store", redirect: "manual", credentials: "same-origin",
                                          headers: {"Content-Type": "application/json", "X-Mesh2tak": "1"},
                                          body: JSON.stringify(dati)});
  if (r.type === "opaqueredirect") {
    document.getElementById("sessione").hidden = false;
    throw new Error("sessione scaduta: ricarica la pagina");
  }
  let d = {};
  try { d = await r.json(); } catch (e) { d = {}; }
  if (!r.ok) throw new Error(d.errore || ("il server risponde " + r.status));
  return d;
}

async function aggiornaGestione() {
  try {
    gestione = await prendi("api/stato");
    erroreGestione = "";
    riavvio = 0;
  } catch (e) {
    // durante un lavoro Authentik si riavvia: per qualche minuto si tiene quello che si sapeva
    if (e.message !== "sessione scaduta" && lavoroInCorso() && (!riavvio || Date.now() - riavvio < 5 * 60 * 1000)) {
      riavvio = riavvio || Date.now();
      return;
    }
    gestione = null;
    riavvio = 0;
    erroreGestione = e.message;
  }
}

function lavoroInCorso() {
  return !!(gestione && ((gestione.lavori || []).some(l => l.esito === "in_coda" || l.esito === "in_corso") ||
                         (gestione.richieste || []).some(r => r.stato === "in_corso")));
}

async function aggiornaElenco() {
  const d = await prendi("ponti.json");
  elenco = Array.isArray(d.ponti) ? d.ponti : [];
  versione = d.versione || "";
  ultimoElenco = Date.now();
}

async function aggiornaPonte(p) {
  const vecchio = stati.get(p.nome) || {};
  try {
    const dati = await prendi(p.stato);
    if (!dati || typeof dati !== "object" || !dati.nodo) throw new Error("risposta non valida");
    stati.set(p.nome, {dati, quando: Date.now(), errore: null, ultimoOk: Date.now()});
  } catch (e) {
    stati.set(p.nome, {dati: null, quando: Date.now(), errore: e.message, ultimoOk: vecchio.ultimoOk || null});
  }
}

function valuta(st) {
  if (!st || !st.dati) {
    const quando = st && st.ultimoOk ? " Ultimo contatto " + fa((Date.now() - st.ultimoOk) / 1000) + "." : "";
    return {classe: "giu", parole: "Non raggiungibile",
            spiega: "Il Raspberry è spento, senza internet o il tunnel si sta ricollegando." + quando};
  }
  const d = st.dati;
  if (d.pausa && d.pausa.attiva) {
    return {classe: "pausa", parole: "In pausa",
            spiega: "Nodo gestito " + (d.pausa.chi || "da qualcuno") + " dalle " + oraDi(d.pausa.dal) +
                    ": le posizioni via radio non arrivano al TAK Server. Il gateway riprende da solo, al più tardi alle " +
                    oraDi(d.pausa.fino_al_massimo) + "."};
  }
  if (!d.tak || !d.tak.collegato) {
    return {classe: "guasto", parole: "Non arriva al TAK Server",
            spiega: (d.tak && d.tak.errore ? maiuscola(d.tak.errore) + ". " : "") + "Il gateway riprova da solo."};
  }
  if (!d.nodo.collegato) {
    return {classe: "guasto", parole: "Radio non collegata",
            spiega: "Il gateway non parla con la sua radio e riprova da solo. Se dura, sul Raspberry: sudo mesh2tak-stato"};
  }
  return {classe: "ok", parole: "In linea", spiega: ""};
}

function fatto(dl, titolo, valore, classe) {
  const d = el("div", classe);
  const dd = el("dd");
  // `testo` tra apici inversi diventa un comando da copiare
  String(valore).split("`").forEach((pezzo, i) => { if (pezzo) dd.append(i % 2 ? el("code", null, pezzo) : pezzo); });
  d.append(el("dt", null, titolo), dd);
  dl.append(d);
}

function giorno(ts) {
  return new Date(ts * 1000).toLocaleDateString("it-IT", {day: "numeric", month: "numeric", year: "numeric"});
}

const PROTEZIONI = {watchdog: "riavvio automatico se si blocca", aggiornamenti: "aggiornamenti di sicurezza ogni notte",
                    registri: "registri limitati"};

function fattiRaspberry(dl, d) {
  const sis = d.sistema;
  if (!sis) {
    fatto(dl, "Raspberry", "aggiorna il gateway alla 1.7 per vedere certificato, corrente e protezioni", "attenzione");
    return;
  }
  const ag = sis.aggiornamento;
  if (ag && ag.versione && (ag.stato !== "fatto" || (ag.quando && d.ora - ag.quando < 7 * 86400))) {
    if (ag.stato === "in_corso") fatto(dl, "Aggiornamento", "alla " + ag.versione + " in corso: il gateway si ferma per un paio di minuti", "attenzione");
    else if (ag.stato === "fatto") fatto(dl, "Aggiornamento", "alla " + ag.versione + ", " + giornoOra(ag.quando));
    else if (ag.stato === "ripristinato") fatto(dl, "Aggiornamento", maiuscola(ag.messaggio || "non riuscito, rimessa la versione di prima"), "attenzione");
    else fatto(dl, "Aggiornamento", "alla " + ag.versione + " non riuscito: " + (ag.messaggio || "errore"), "guasto");
  }
  const comando = (sis.azioni || []).slice(-1)[0];      // dalla 1.13.0: l'ultimo comando di "Gestisci il Raspberry"
  if (comando && comando.quando && d.ora - comando.quando < 86400) {
    const esito = comando.stato === "fatto" ? "fatto " : (comando.stato === "in_corso" ? "in corso, " :
                  (STATI_COMANDI[comando.stato] || comando.stato) + " ");
    fatto(dl, "Ultimo comando", (NOMI_COMANDI[comando.azione] || comando.azione) + ": " + esito + giornoOra(comando.quando) +
          (comando.stato === "fatto" ? "" : ". " + maiuscola(frase(comando.messaggio || ""))),
          comando.stato === "errore" ? "guasto" : (comando.stato === "in_corso" ? "attenzione" : ""));
  }
  const disco = sis.disco;
  const libero = disco ? disco.libero_mb : null;
  let testo = sis.acceso_dal ? "acceso da " + durata(d.ora - sis.acceso_dal) : "acceso";
  let classe = "";
  if (libero !== null && libero !== undefined) {
    testo += ", " + numIt(libero / 1024) + " GB liberi sulla microSD";
    if (libero < 500) { testo = "microSD quasi piena: " + numIt(libero / 1024, 2) + " GB liberi"; classe = "guasto"; }
    else if (libero < 2000) classe = "attenzione";
  }
  fatto(dl, "Raspberry", testo, classe);
  const al = sis.alimentazione;
  if (al) {
    if (al.ultima && d.ora - al.ultima < 3600) {
      fatto(dl, "Corrente", "non basta: " + al.volte + (al.volte === 1 ? " calo" : " cali") + ", l'ultimo " +
                            fa(d.ora - al.ultima) + ". Serve l'alimentatore da 5,1 V 2,5 A", "guasto");
    } else if (al.volte) {
      fatto(dl, "Corrente", "basta adesso; " + al.volte + (al.volte === 1 ? " calo" : " cali") +
                            " da quando il gateway è acceso, l'ultimo " + giornoOra(al.ultima), "attenzione");
    } else {
      fatto(dl, "Corrente", "sufficiente");
    }
  }
  const pr = sis.protezioni || {};
  const mancano = Object.keys(PROTEZIONI).filter(k => !pr[k]);
  if (!mancano.length) {
    fatto(dl, "Protezioni", maiuscola(PROTEZIONI.watchdog) + "; " + PROTEZIONI.aggiornamenti);
  } else {
    fatto(dl, "Protezioni", "manca: " + mancano.map(k => PROTEZIONI[k]).join(", ") +
                            ". Rilancia l'installatore del gateway", "attenzione");
  }
  if (sis.scorta && sis.scorta.quando) {
    const vecchia = d.ora - sis.scorta.quando > 180 * 86400;
    fatto(dl, "MicroSD di scorta", "del " + giorno(sis.scorta.quando) + (vecchia ? ": vecchia, rifalla con `sudo mesh2tak-scorta`" : ""),
          vecchia ? "attenzione" : "");
  } else {
    fatto(dl, "MicroSD di scorta", "nessuna: sul Raspberry `sudo mesh2tak-scorta`", "attenzione");
  }
  const c = d.certificato;
  if (c && c.scade) {
    const giorni = Math.floor((c.scade - d.ora) / 86400);
    if (c.scade <= d.ora) fatto(dl, "Certificato TAK", "scaduto il " + giorno(c.scade) + ": il TAK Server non lo accetta", "guasto");
    else if (giorni < 30) fatto(dl, "Certificato TAK", "scade il " + giorno(c.scade) + ", tra " + Math.max(1, giorni) +
                                                    (giorni === 1 ? " giorno" : " giorni") + ": rinnovalo", "attenzione");
    else fatto(dl, "Certificato TAK", "valido fino al " + giorno(c.scade));
  }
}

function canali(d) {
  const c = (d.nodo && d.nodo.canali) || [];
  if (!c.length) return "non ancora letti";
  return c.map(x => x.nome + (x.privato ? " (squadra" + (x.inoltrato ? ", inoltrato" : "") + ")" : " (pubblico)")).join(", ");
}

function valutaPonte(p) {
  // come valuta(), ma sa che un ponte nuovo si sta ancora installando (dalla 1.10.0)
  const st = stati.get(p.nome);
  const v = valuta(st);
  const d = st && st.dati;
  const ins = installazioneDi(p.nome);
  if (!d && p.aggiunto && Date.now() - Date.parse(p.aggiunto) < 10 * 60 * 1000) {
    v.parole = "In attesa del primo collegamento";
    v.spiega = "Appena aggiunto: il Raspberry se ne accorge entro qualche minuto e apre il tunnel da solo.";
  }
  if (!ins || (d && ins.fase === "fatto")) return v;
  const eta = Date.now() / 1000 - ins.quando;
  if (ins.fase === "errore") {
    return {classe: "guasto", parole: "Installazione non riuscita", riprova: ins,
            spiega: maiuscola(frase(ins.testo || "errore")) + " (" + fa(eta) + ")"};
  }
  if (ins.fase === "fatto") {
    if (eta < 20 * 60) { v.parole = "Installato, in attesa del primo collegamento"; v.spiega = "Il gateway è installato: apre il tunnel da solo entro qualche minuto."; }
    return v;
  }
  if (d) return v;                       // risponde gia': quello che conta e' lo stato vero
  return {classe: "pausa", parole: "Si sta installando", riprova: eta > 3600 ? ins : null,
          spiega: maiuscola(frase(ins.testo || "in corso")) + " (" + fa(eta) + "). Tutta l'installazione dura 30-60 minuti e il " +
                  "Raspberry si riavvia una volta." + (eta > 3600 ? " Nessuna notizia da più di un'ora: Raspberry spento o senza internet?" : "")};
}

// i fatti che stanno sempre in vista nella scheda di un gateway; gli altri vanno in "Altri dettagli", tranne quelli
// che segnalano qualcosa da guardare
const FATTI_IN_VISTA = new Set(["TAK Server", "Radio del gateway", "Sulla mappa TAK", "Canale radio", "Copertura",
                                "Aggiornamento", "Ultimo comando"]);

function disegnaPonte(p) {
  const st = stati.get(p.nome);
  const s = statoGateway(p), v = s.v;
  const d = st && st.dati;
  const sez = el("section", "ponte");
  sez.id = "gateway-" + p.nome;
  sez.dataset.stato = v.classe;
  sez.setAttribute("aria-labelledby", "ponte-" + p.nome);
  const capo = el("div", "capo");
  const testa = el("div");
  const h3 = el("h3", null, p.nome);
  h3.id = "ponte-" + p.nome;
  const chi = el("p", "chi", d ? [d.nome || "nodo senza nome", d.nome_breve ? "sigla " + d.nome_breve : "", d.id_nodo || ""].filter(Boolean).join(", ") : p.indirizzo.replace(/^https:\/\//, "").replace(/\/$/, ""));
  const stato = el("p", "stato");
  stato.dataset.livello = v.classe === "giu" ? "spento" : ["ok", "attenzione", "problema"][s.livello];
  stato.append(segnoStato(s.livello, v.classe === "giu"), el("span", null, v.parole));
  testa.append(h3, chi, stato);
  capo.append(testa);

  const azione = el("div", "azione");
  const b = el("button", "bottone", "Gestisci la radio");
  b.type = "button";
  if (!d) {
    b.disabled = true;
    azione.append(b, el("small", null, "Si apre quando il gateway è raggiungibile"));
  } else if (!d.gestione || !d.gestione.pagina) {
    b.disabled = true;
    azione.append(b, el("small", null, "Radio USB: si gestisce dall'app Meshtastic"));
  } else {
    b.addEventListener("click", () => chiediConferma(p, d));
    azione.append(b, el("small", null, "Mentre è aperta il gateway è in pausa"));
  }
  if (gestione && d && d.sistema && d.sistema.comandi) {     // dalla 1.13.0
    const r = el("button", "bottone secondario piccolo", "Gestisci il Raspberry");
    r.type = "button";
    r.addEventListener("click", () => apriRaspberry(p));
    azione.append(r);
  }
  if (d && d.id_nodo && iconeAttive()) azione.append(bottoneIcona(d.id_nodo, "gateway " + p.nome, true));
  capo.append(azione);
  if (v.spiega) capo.append(el("p", "spiega", v.spiega));
  if (v.riprova && gestione) {
    const r = el("button", "bottone secondario piccolo riprova", "Riprova l'installazione");
    r.type = "button";
    r.addEventListener("click", () => chiediRiprova(p, v.riprova));
    const riga = el("div", "riprova");
    riga.append(r);
    capo.append(riga);
  }
  sez.append(capo);

  const altri = el("dl", "fatti");
  if (d) {
    const tutti = {voci: [], append(x) { this.voci.push(x); }};
    const tak = d.tak || {};
    fatto(tutti, "TAK Server", tak.collegato ? "collegato da " + durata(d.ora - tak.dal) : "non collegato", tak.collegato ? "" : "guasto");
    fatto(tutti, "Radio del gateway", (d.nodo.collegato ? "collegata, " : (d.pausa && d.pausa.attiva ? "lasciata alla gestione, " : "non collegata, ")) + (d.nodo.preset || ""));
    const sentite = Array.isArray(d.radio) ? d.radio.length : 0, sullaMappa = d.radio_sulla_mappa || 0;
    fatto(tutti, "Sulla mappa TAK", sullaMappa + " radio adesso" +
                                    (sentite > sullaMappa ? ", " + sentite + " da quando il gateway è acceso" : ""));
    const cr = d.canale_radio;
    if (cr && cr.occupato !== null && cr.occupato !== undefined) {
      fatto(tutti, "Canale radio", "occupato il " + numIt(cr.occupato) + "%; il gateway trasmette il " +
                                   numIt(cr.trasmesso, 2) + "% (limite 10%)", cr.trasmesso >= 8 ? "attenzione" : "");
    }
    fatto(tutti, "Copertura", testoCopertura("g:" + p.nome));
    fattoPrevista(altri, "g:" + p.nome, 5);
    fatto(tutti, "Canali", canali(d));
    const c = d.conteggi || {};
    const finestra = d.ora - (d.conteggi_dal || d.avviato);
    fatto(tutti, !Number.isFinite(finestra) ? "Contatori" : finestra < 90 ? "Nell'ultimo minuto" : (finestra >= 3600 ? "Nell'ultima ora" : "Negli ultimi " + durata(finestra)), (c.inoltrate || 0) + " posizioni al TAK Server, " + (c.radio_al_server || 0) + " messaggi dalla radio, " + (c.radio_trasmessi || 0) + " alle radio");
    fatto(tutti, "Programma", "mesh2tak " + (d.versione || "?") + ", acceso da " + durata(d.ora - d.avviato));
    fattiRaspberry(tutti, d);
    const dl = el("dl", "fatti");
    for (const x of tutti.voci) {
      (FATTI_IN_VISTA.has(x.firstChild.textContent) || x.className ? dl : altri).append(x);
    }
    sez.append(dl);
  }
  const fondo = [];
  if (altri.childElementCount) fondo.push(altri);
  if (gestione) {
    const t = el("button", "togli", "Togli il gateway dalla rete");
    t.type = "button";
    t.addEventListener("click", () => chiediTogli(p));
    fondo.push(t);
  }
  if (fondo.length) {
    const det = dettagli("altri:" + p.nome, "Altri dettagli", ...fondo);
    det.className = "altri";
    sez.append(det);
  }
  return sez;
}

function disegnaRichiesta(r) {
  const li = el("li", "richiesta");
  const chi = el("div");
  chi.append(el("h3", null, r.nome));
  chi.append(el("p", "da", "Raspberry " + (r.macchina || "?") + ", dall'indirizzo " + (r.ip || "?") + ", " +
                            giornoOra(r.arrivata) + (r.versione ? " (mesh2tak " + r.versione + ")" : "")));
  if (r.sostituisce) chi.append(el("p", "avviso", "C'è già un gateway " + r.nome + " nella rete: un altro Raspberry, o questo reinstallato. " +
                                                  "Sostituendo, questo Raspberry prende il suo posto e quello vecchio non entra più."));
  if (r.stesso_nome) chi.append(el("p", "avviso", "C'è un'altra richiesta con lo stesso nome: approva solo quella con il codice giusto."));
  const codice = el("div", "controllo");
  codice.append(el("small", null, "codice di controllo"), el("b", null, r.controllo || "?"));
  if (r.nuovo) {
    chi.append(el("p", "da", "Raspberry nuovo" + (r.nuovo.modello ? " (" + r.nuovo.modello + ")" : "") + ", " +
                             (r.nuovo.radio_usb ? "con una radio USB collegata" : "senza radio USB (scheda sui pin?)") + "."));
    if (!r.invito) chi.append(el("p", "avviso", "Non ha una chiave d'invito valida (mai messa, già usata o scaduta): nessuno può " +
                                               "confrontare il codice. Approvalo solo se sei sicuro che è il tuo."));
  }
  li.append(chi, codice);
  if (r.stato !== "in_corso" && r.nuovo && !r.installa) {
    li.append(moduloInstalla(r));
  } else if (r.stato === "in_corso") {
    const lav = el("p", "lavora", r.installa ? "Sto aggiungendo il gateway; poi il Raspberry si installa da solo." :
                                                "Sto aggiungendo il gateway, ci vuole un paio di minuti.");
    lav.append(el("small", null, r.fermo ? "Il server non ha ancora cominciato: se resta così, sul server  sudo bash rete-meshtastic.sh installa"
                                         : "Authentik si riavvia: per circa un minuto anche questa pagina può non rispondere."));
    li.append(lav);
  } else {
    const pulsanti = el("div", "pulsanti");
    const no = el("button", "bottone secondario piccolo", "Rifiuta");
    no.type = "button";
    const si = el("button", "bottone piccolo", r.errore ? "Riprova" : (r.sostituisce ? "Sostituisci" : "Approva"));
    si.type = "button";
    no.addEventListener("click", () => azioneRichiesta(no, "rifiuta", {id: r.id}));
    si.addEventListener("click", () => azioneRichiesta(si, "approva", {id: r.id, sostituisci: !!r.sostituisce}));
    pulsanti.append(no, si);
    li.append(pulsanti);
  }
  if (r.errore && r.stato !== "in_corso") {
    const pr = el("div", "problema", "Non riuscito: " + frase(r.errore));
    if (r.dettagli) pr.append(dettagli("richiesta:" + r.id, "Dettagli", el("pre", null, r.dettagli)));
    li.append(pr);
  }
  return li;
}

const PAROLE_AZIONE = {aggiungi: "Aggiunta del gateway ", rimuovi: "Rimozione del gateway "};

function cosaLavoro(l) {
  if (l.azione === "aggiorna-server") return "Aggiornamento del server alla " + (l.versione || "?");
  if (l.azione === "aggiorna-ponti") return "Richiesta di aggiornamento " + (l.nome === "tutti" ? "ai gateway" : "al gateway " + l.nome) +
                                            " alla " + (l.versione || "?") + (l.reimposta ? ", con la radio come all'installazione" : "");
  if (l.azione === "impostazioni-nuovi") return "Certificato e canale per i gateway nuovi";
  if (l.azione === "installa") return "Nuova installazione del gateway " + (l.nome || "?");
  if (l.azione === "azione-ponte") return (NOMI_COMANDI[l.cosa] || "Comando") + " sul gateway " + (l.nome || "?");
  if (l.azione === "certificato-ponte") return "Certificato nuovo per il gateway " + (l.nome || "?");
  return (PAROLE_AZIONE[l.azione] || "Lavoro sul gateway ") + (l.nome || "?");
}

function disegnaLavoro(l) {
  const p = el("p", "lavoro");
  p.dataset.esito = l.esito;
  const cosa = cosaLavoro(l);
  if (l.esito === "in_coda" || l.esito === "in_corso") {
    p.append(document.createTextNode(cosa + ": in corso (chiesta da " + (l.da || "?") + ")."));
    if (l.fermo) p.append(el("small", null, "Il server non ha ancora cominciato: se resta così, sul server  sudo bash rete-meshtastic.sh installa"));
    else if (l.azione === "aggiungi") p.append(el("small", null, "Authentik si riavvia: per circa un minuto anche questa pagina può non rispondere."));
    else if (l.azione === "aggiorna-server") p.append(el("small", null, "Il servizio della pagina riparte: per circa un minuto la pagina può non rispondere, poi riprende da sola."));
  } else if (l.esito === "ok") {
    p.textContent = cosa + ": " + (l.azione === "impostazioni-nuovi" ? "salvati alle " : "fatta alle ") + oraDi(l.quando) + "." +
                    (l.azione === "aggiorna-ponti" ? " I gateway si aggiornano da soli entro qualche minuto." : "") +
                    (l.azione === "installa" ? " Il Raspberry riparte da solo entro un paio di minuti." : "");
  } else {
    p.textContent = cosa + ": non riuscita alle " + oraDi(l.quando) + ".";
    const pr = el("div", "problema", maiuscola(frase(l.messaggio || "errore")));
    if (l.testo) pr.append(dettagli("lavoro:" + l.id, "Dettagli", el("pre", null, l.testo)));
    p.append(pr);
  }
  return p;
}

function disegnaNovita() {
  if (!gestione) return null;
  const richieste = (gestione.richieste || []).filter(r => r.stato === "attesa" || r.stato === "in_corso");
  const ora = Date.now() / 1000;
  const lavori = (gestione.lavori || []).filter(l => l.esito === "in_coda" || l.esito === "in_corso" ||
                                                     (l.quando && ora - l.quando < 15 * 60))
                                        .filter(l => !(l.richiesta && richieste.some(r => r.id === l.richiesta)))
                                        // i comandi ai ponti passati bene: com'e' andata la dice il ponte, sotto
                                        .filter(l => !(["azione-ponte", "certificato-ponte"].includes(l.azione) && l.esito === "ok"));
  if (!richieste.length && !lavori.length) return null;
  const sez = el("section", "novita");
  sez.setAttribute("aria-live", "polite");
  const inAttesa = richieste.filter(r => r.stato === "attesa").length;
  if (richieste.length) {
    sez.append(el("h2", null, inAttesa === 1 ? "Un gateway chiede di entrare" :
                              (inAttesa ? inAttesa + " gateway chiedono di entrare" : "Gateway in arrivo")));
    sez.append(el("p", "guida", "Approva solo se il codice di controllo è lo stesso che il Raspberry ha mostrato alla fine dell'installazione. Una richiesta che non riconosci, rifiutala."));
    const ul = el("ul", "righe");
    richieste.forEach(r => ul.append(disegnaRichiesta(r)));
    sez.append(ul);
  } else {
    // solo lavori: in evidenza se uno e' in corso o non e' riuscito, altrimenti una riga discreta
    if (lavori.every(l => l.esito === "ok")) sez.classList.add("quieta");
    sez.append(el("h2", null, sez.classList.contains("quieta") ? "Ultimi lavori" : "Lavori sulla rete"));
  }
  lavori.forEach(l => sez.append(disegnaLavoro(l)));
  return sez;
}

