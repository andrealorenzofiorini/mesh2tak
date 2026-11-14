// ---- Copertura prevista (dalla 1.17.0): la pagina. Per ogni gateway e ripetitore con una posizione calcola dal
// rilievo dove una radio portatile si collegherebbe (i conti sono nel pezzo prima), con i dati veri del suo impianto,
// e corregge il calcolo con quello che quel gateway ha misurato davvero. I quadretti previsti stanno sulla mappa sotto
// a quelli misurati, piu' chiari. Si accende con «Copertura prevista»: il rilievo arriva dal nostro server (api/terreno).
const rilievo = new Map();        // "x,y" -> promessa delle quote di un riquadro (restano per tutta la visita)
const previsioni = new Map();     // chiave dell'impianto -> {firma, stato, parte, nome, celle, radio, collegamenti, errore, quando}
const CAMPI_IMPIANTO = [["altezza_m", "Altezza dell'antenna da terra", "m", 0.5, 120],
                        ["guadagno_dbi", "Guadagno dell'antenna", "dBi", -5, 20],
                        ["perdite_db", "Perdite del cavo", "dB", 0, 20],
                        ["potenza_dbm", "Potenza di trasmissione", "dBm", 0, 30]];
const AIUTI_IMPIANTO = {
  altezza_m: {fisso: "Dal terreno alla punta: se l'antenna è su un tetto o su un palo, conta tutto.",
              portatile: "Dove sta di solito la radio: in mano o sullo spallaccio dello zaino, circa 1,5 m."},
  guadagno_dbi: {fisso: "È scritto sull'antenna. Quella corta di serie circa 2; una da esterno in vetroresina da 5 a 8.",
                 portatile: "L'antenna corta di serie, vicino al corpo, rende circa 0."},
  perdite_db: {fisso: "Circa 1 dB ogni 2-3 metri di cavo sottile; 0 se l'antenna è avvitata alla radio.",
               portatile: "Di solito 0: l'antenna è avvitata alla radio."},
  potenza_dbm: {fisso: "Quella impostata sulla radio. Le radio Meshtastic arrivano a 22; di più solo con un amplificatore.",
                portatile: "Quella impostata sulle radio della squadra: di solito 22."}};
let puntoDiProva = null, impiantoDiProva = {}, modoTocco = "";     // modoTocco: che cosa fa il prossimo tocco sulla mappa ("prova", "copri")
let giroPrevisione = 0, previsioneInCorso = false, daInquadrare = false;
let spuntaPrevista = null, statoPrevista = null, elencoPreviste = null, vocePrevista = null, bottoneProva = null;
let bottoneTrova = null, bottoneCopri = null, statoSiti = null, esitoSiti = null;
let partiPrevista = [], firmaMostrate = "", firmaElenco = "", mostrate = new Map(), impiantoAperto = null;
sceltaCopertura.prevista = false;
sceltaCopertura.raggio = "20";

function impiantoDi(chiave) {
  // i dati di un impianto: quelli scritti dagli amministratori; dove mancano, la potenza letta dalla radio del
  // gateway e poi i valori di fabbrica. "origine" dice da dove viene ogni valore.
  const tipo = chiave === "portatile" ? "portatile" : (chiave.startsWith("g:") ? "gateway" : "ripetitore");
  const fabbrica = IMPIANTO_FABBRICA[tipo];
  const scritti = chiave === "p:prova" ? impiantoDiProva : ((gestione && gestione.impianti && gestione.impianti[chiave]) || {});
  const valori = {}, origine = {};
  for (const campo of Object.keys(fabbrica)) {
    const scritto = Number.isFinite(scritti[campo]);
    valori[campo] = scritto ? scritti[campo] : fabbrica[campo];
    origine[campo] = scritto ? "scritto" : "fabbrica";
  }
  if (tipo === "gateway" && origine.potenza_dbm === "fabbrica") {
    const c = copertura.get(chiave.slice(2)), letta = c && c.dati && c.dati.radio && c.dati.radio.potenza_dbm;
    if (Number.isFinite(letta)) {
      valori.potenza_dbm = letta;
      origine.potenza_dbm = "radio";
    }
  }
  return {tipo, valori, origine, scritti};
}

function testoImpianto(chiave) {
  const i = impiantoDi(chiave), v = i.valori;
  const testo = i.tipo === "portatile"
    ? "a " + numIt(v.altezza_m) + " m da terra, antenna " + numIt(v.guadagno_dbi) + " dBi, " + numIt(v.potenza_dbm) + " dBm"
    : "antenna a " + numIt(v.altezza_m) + " m da terra, " + numIt(v.guadagno_dbi) + " dBi, cavo " + numIt(v.perdite_db) +
      " dB, " + numIt(v.potenza_dbm) + " dBm";
  const fabbrica = Object.values(i.origine).filter(o => o === "fabbrica").length;
  return testo + (fabbrica >= 3 ? " (valori di fabbrica)" : (fabbrica ? " (in parte di fabbrica)" : ""));
}

function impiantiSullaMappa() {
  const lista = puntiDellaRete().map(p => ({chiave: p.chiave, tipo: p.tipo, lat: p.lat, lon: p.lon,
                                           nome: (p.tipo === "gateway" ? "Gateway " : "") + p.nome}));
  if (puntoDiProva) lista.push({chiave: "p:prova", tipo: "prova", nome: "Punto di prova", lat: puntoDiProva.lat, lon: puntoDiProva.lon});
  return lista;
}

function impiantiScelti() {
  // dalla 1.18.0 si calcolano sempre tutti, anche se «Mostra» ne fa vedere uno: servono per dire che cosa si perde
  // se un impianto si spegne e che cosa aggiunge un punto nuovo
  return impiantiSullaMappa();
}

function radioPerLaPrevisione(chiave) {
  // l'impostazione radio (larghezza di banda, minimo decifrabile) e la regione (frequenza) le dice il gateway
  let c = chiave.startsWith("g:") ? copertura.get(chiave.slice(2)) : null;
  if (!c || !c.dati) c = [...copertura.values()].find(x => x.dati);
  const d = (c && c.dati) || {};
  return parametriRadio(d.preset, d.radio && d.radio.regione, d.limite_snr);
}

function firmaPrevisione(nodo) {
  // quello da cui dipende il calcolo di un impianto: se cambia, si rifa'. Ripetitori e punto di prova dipendono anche
  // dai gateway (il collegamento previsto con ognuno).
  const gateway = nodo.tipo === "gateway" ? [] : impiantiSullaMappa().filter(n => n.tipo === "gateway")
    .map(n => [n.chiave, n.lat, n.lon, impiantoDi(n.chiave).valori]);
  return JSON.stringify([nodo.lat, nodo.lon, impiantoDi(nodo.chiave).valori, radioPerLaPrevisione(nodo.chiave),
                         impiantoDi("portatile").valori, sceltaCopertura.raggio, gateway]);
}

function riquadroRilievo(x, y) {
  const k = x + "," + y;
  if (!rilievo.has(k)) {
    const p = (async () => {
      const r = await fetch("api/terreno/" + ZOOM_RILIEVO + "/" + x + "/" + y + ".png", {redirect: "manual", credentials: "same-origin"});
      if (r.type === "opaqueredirect" || r.status === 403) {
        document.getElementById("sessione").hidden = false;
        throw new Error("sessione scaduta: ricarica la pagina");
      }
      if (!r.ok) {
        let d = {};
        try { d = await r.json(); } catch (e) { d = {}; }
        throw new Error(d.errore || "il rilievo non arriva (risposta " + r.status + ")");
      }
      return quoteDelRiquadro(await r.arrayBuffer());
    })();
    p.catch(() => rilievo.delete(k));       // un riquadro non arrivato si richiede la volta dopo
    rilievo.set(k, p);
  }
  return rilievo.get(k);
}

async function rilievoPer(lista, avanza) {
  const riquadri = new Map();
  let prossimo = 0, fatti = 0;
  const lavora = async () => {
    while (prossimo < lista.length) {
      const [x, y] = lista[prossimo++];
      riquadri.set(x + "," + y, await riquadroRilievo(x, y));
      avanza(++fatti / lista.length);
    }
  };
  await Promise.all(Array.from({length: Math.min(6, lista.length)}, lavora));
  return unisciRilievo(riquadri, lista);
}

async function calcolaPrevisione(nodo, giro) {
  const firma = firmaPrevisione(nodo);
  let p = previsioni.get(nodo.chiave);
  if (p && p.firma === firma && (p.stato === "fatta" || (p.stato === "errore" && Date.now() - p.quando < 120000))) return;
  p = {firma, stato: "calcolo", parte: 0, nome: nodo.nome, quando: Date.now()};
  previsioni.set(nodo.chiave, p);
  mostraStatoPrevista();
  try {
    const impianto = Object.assign({lat: nodo.lat, lon: nodo.lon}, impiantoDi(nodo.chiave).valori);
    const radio = radioPerLaPrevisione(nodo.chiave), raggio = Number(sceltaCopertura.raggio);
    const lista = riquadriDelRilievo(nodo.lat, nodo.lon, raggio);
    const quota = await rilievoPer(lista, f => { p.parte = 0.3 * f; mostraStatoPrevista(); });
    const calcolo = prevediCopertura(impianto, radio, impiantoDi("portatile").valori, quota, raggio, 0);
    let passo = calcolo.next();
    while (!passo.done) {
      p.parte = 0.3 + 0.7 * passo.value;
      mostraStatoPrevista();
      await new Promise(f => setTimeout(f));       // a pezzi: la pagina resta viva
      if (giro !== giroPrevisione) {          // superato da una scelta nuova
        if (previsioni.get(nodo.chiave) === p) previsioni.delete(nodo.chiave);
        return;
      }
      passo = calcolo.next();
    }
    p.celle = passo.value.celle;
    p.radio = radio;
    p.raggio = raggio;
    p.collegamenti = [];
    if (nodo.tipo !== "gateway") {          // un ripetitore serve se arriva a un gateway
      for (const g of impiantiSullaMappa().filter(n => n.tipo === "gateway")) {
        const c = margineTraImpianti(impianto, Object.assign({lat: g.lat, lon: g.lon}, impiantoDi(g.chiave).valori), radio, quota);
        if (c) p.collegamenti.push(Object.assign(c, {nome: g.nome, chiave: g.chiave}));
      }
    }
    p.stato = "fatta";
  } catch (e) {
    p.stato = "errore";
    p.errore = e.message;
  }
  p.quando = Date.now();
}

function previsioneDaFare() {
  return impiantiScelti().some(n => {
    const p = previsioni.get(n.chiave);
    return !p || p.firma !== firmaPrevisione(n) || (p.stato === "errore" && Date.now() - p.quando >= 120000);
  });
}

async function avviaPrevisione() {
  // calcola, uno alla volta, gli impianti scelti che mancano; una scelta nuova ferma il giro vecchio
  const giro = ++giroPrevisione;
  if (!sceltaCopertura.prevista || !gestione) return;
  previsioneInCorso = true;
  try {
    for (const nodo of impiantiScelti()) {
      await calcolaPrevisione(nodo, giro);
      if (giro !== giroPrevisione) return;
      aggiornaPrevista();
    }
  } finally {
    if (giro === giroPrevisione) previsioneInCorso = false;
  }
  aggiornaPrevista();
}

function taraturaDi(chiave) {
  // solo i gateway misurano: con che segnale hanno sentito le radio, quadretto per quadretto
  const p = previsioni.get(chiave), c = chiave.startsWith("g:") ? copertura.get(chiave.slice(2)) : null;
  if (!p || p.stato !== "fatta" || !c || !c.dati) return null;
  const misurate = [];
  let firma = 0;
  for (const q of c.dati.celle || []) {
    if (!Array.isArray(q) || q.length < 8) continue;
    const via = q[8] && typeof q[8] === "object" && !Array.isArray(q[8]) ? q[8] : {};
    const altre = (Number(q[6]) || 0) + Object.values(via).reduce((s, v) => s + (Number(v) || 0), 0);
    misurate.push({x: q[0], y: q[1], n: q[2], media: q[3], altre});
    firma += q[0] * 3 + q[1] * 7 + q[2] * 11 + altre * 13 + (Number(q[3]) || 0) * 17;
  }
  if (p.taratura && p.tarataSu === firma) return p.taratura;       // si rifa' solo quando le misure cambiano
  p.taratura = taraPrevisione(p.celle, misurate, p.radio.limite);
  p.tarataSu = firma;
  return p.taratura;
}

function correzioniPrevista() {
  // quanto togliere al calcolo sul solo rilievo, impianto per impianto: la taratura del gateway; per chi non ha
  // misure sue (ripetitori, punto di prova, gateway con poche misure) la media dei gateway tarati; se nessuno e'
  // tarato, una stima prudente
  const tarate = new Map();
  for (const chiave of previsioni.keys()) {
    const t = taraturaDi(chiave);
    if (t && t.a !== null) tarate.set(chiave, t);
  }
  const media = campo => [...tarate.values()].reduce((s, t) => s + t[campo], 0) / tarate.size;
  const comune = tarate.size ? {a: media("a"), b: media("b"), come: "media"} : Object.assign({come: "stima"}, SENZA_TARATURA);
  const corr = new Map([["nuovi", comune]]);           // "nuovi": per un sito che non c'e' ancora
  for (const chiave of previsioni.keys()) {
    const t = tarate.get(chiave);
    corr.set(chiave, t ? {a: t.a, b: t.b, come: "tarata", taratura: t} : comune);
  }
  return corr;
}

function quadrettiPrevisti(corr) {
  // per la scelta di «Mostra»: quadretto per quadretto, il margine migliore tra gli impianti mostrati
  const scelta = sceltaCopertura.mostra, celle = new Map(), sullaMappa = new Set(impiantiSullaMappa().map(n => n.chiave));
  for (const [chiave, p] of previsioni) {
    if (p.stato !== "fatta" || (scelta && scelta !== chiave) || !sullaMappa.has(chiave)) continue;
    const c = corr.get(chiave);
    for (const q of p.celle.values()) {
      const margine = q.margine - correzioneDb(c, q.km);
      if (margine < 0) continue;
      const k = q.x * 262144 + q.y, gia = celle.get(k);
      if (!gia || margine > gia.margine) celle.set(k, {x: q.x, y: q.y, margine, chi: chiave, km: q.km, ostacoli: q.ostacoli});
    }
  }
  return celle;
}

function kmqDi(quadretti, lat) {
  const lato = 40075.017 * Math.cos(lat * Math.PI / 180) / 2 ** ZOOM_QUADRETTI;
  return quadretti * lato * lato;
}

function righePrevista(v, misurato) {
  // per il riquadro che compare passando sulla mappa. v: un quadretto previsto o un gruppo (da lontano)
  const nome = chiave => { const n = impiantiSullaMappa().find(x => x.chiave === chiave); return n ? n.nome : "un impianto"; };
  if (misurato) return ["Previsto qui: " + v.classe.breve + " (" + numIt(v.margine, 0) + " dB sopra il minimo) da " + nome(v.meglio.chi) + "."];
  if (v.quanti > 1) {
    return ["Previsto: " + v.classe.breve, v.quanti + " quadretti previsti insieme, in media " + numIt(v.margine, 0) +
            " dB sopra il minimo: avvicina la mappa per vederli uno per uno.", "È un calcolo, non una misura."];
  }
  const q = v.meglio;
  return ["Previsto: " + v.classe.breve, numIt(q.margine, 0) + " dB sopra il minimo che la radio decifra, da " + nome(q.chi) +
          " a " + numIt(q.km) + " km.", q.ostacoli >= 1 ? "Il rilievo lungo il percorso toglie " + numIt(q.ostacoli, 0) + " dB." : "Percorso libero dal rilievo.",
          "È un calcolo, non una misura: qui nessuna radio è ancora passata."];
}

function mostraStatoPrevista() {
  if (!statoPrevista) return;
  let testo = "";
  if (sceltaCopertura.prevista) {
    const scelti = impiantiScelti(), stati = scelti.map(n => previsioni.get(n.chiave));
    const corso = stati.find(p => p && p.stato === "calcolo");
    if (modoTocco === "prova") testo = "Tocca sulla mappa il punto da provare (per esempio dove vorresti mettere un ripetitore).";
    else if (modoTocco === "copri") testo = "Tocca sulla mappa il punto da coprire (una valle, un rifugio, un sentiero).";
    else if (!gestione) testo = "La copertura prevista si calcola con il servizio della pagina, che adesso non risponde.";
    else if (!scelti.length) testo = "Nessun gateway o ripetitore ha ancora una posizione: la copertura prevista parte da lì.";
    else if (corso) {
      testo = "Calcolo la copertura prevista di " + corso.nome + ": " + Math.round(corso.parte * 100) + "%" +
              (scelti.length > 1 ? " (" + (stati.indexOf(corso) + 1) + " di " + scelti.length + ")" : "");
    } else {
      const conta = new Map();
      for (const q of mostrate.values()) { const k = classeCopertura(q.margine); conta.set(k, (conta.get(k) || 0) + 1); }
      const lat = scelti[0].lat, sbagliati = stati.filter(p => p && p.stato === "errore");
      if (mostrate.size) {
        testo = "Copertura prevista entro " + sceltaCopertura.raggio + " km: " + numIt(kmqDi(mostrate.size, lat), 0) + " km² (" +
                CLASSI_COPERTURA.filter(k => conta.get(k)).map(k => k.breve + " " + numIt(kmqDi(conta.get(k), lat), 0)).join(", ") + ").";
      } else if (stati.some(p => p && p.stato === "fatta")) {
        testo = "Entro " + sceltaCopertura.raggio + " km il calcolo non prevede copertura.";
      }
      if (sbagliati.length) {
        testo += (testo ? " " : "") + sbagliati.map(p => p.nome + ": non calcolata (" + p.errore + ").").join(" ") + " Riprovo da solo tra due minuti.";
      }
    }
  }
  if (statoPrevista.textContent !== testo) statoPrevista.textContent = testo;
}

function rigaTaratura(chiave, corr) {
  // come e' stato corretto il calcolo di un impianto, in parole
  const c = corr.get(chiave);
  if (!c) return "";
  const quanto = "più basso di " + numIt(correzioneDb(c, 1), 0) + " dB a 1 km e di " + numIt(correzioneDb(c, 10), 0) + " dB a 10 km";
  if (c.come === "tarata") {
    const t = c.taratura;
    return "Tarata su " + t.quadretti + (t.quadretti === 1 ? " quadretto misurato" : " quadretti misurati") + " da questo gateway: rispetto al " +
           "calcolo sul solo rilievo il segnale vero è " + quanto + "; errore tipico ±" + numIt(t.errore, 0) + " dB." +
           (c.a < -3 ? " Che a 1 km sia più alto del calcolo è strano: controlla i dati dell'impianto." : "");
  }
  const t = chiave.startsWith("g:") ? taraturaDi(chiave) : null;
  const perche = chiave.startsWith("g:") ? "Ancora poche misure di questo gateway" + (t ? " (" + t.esatti + " utili, ne servono 5)" : "")
                                         : "Non ha misure sue (solo i gateway misurano il segnale)";
  return perche + ": " + (c.come === "media" ? "uso la correzione media dei gateway tarati, segnale " + quanto + " rispetto al solo rilievo."
                                             : "per alberi, case e radio addosso conto un segnale " + quanto + " rispetto al solo rilievo, " +
                                               "una stima prudente finché non ci sono misure.");
}

function vociPrevista(chiave, p, corr) {
  // le righe di un impianto sotto la mappa
  if (!p) return ["In attesa del calcolo."];
  if (p.stato === "calcolo") return ["Calcolo in corso."];
  if (p.stato === "errore") return ["Non calcolata: " + frase(p.errore)];
  const c = corr.get(chiave), righe = [rigaTaratura(chiave, corr)];
  let coperti = 0;
  for (const q of p.celle.values()) if (q.margine - correzioneDb(c, q.km) >= 0) coperti++;
  const nodo = impiantiSullaMappa().find(n => n.chiave === chiave);
  righe.push("Da solo copre " + numIt(kmqDi(coperti, nodo ? nodo.lat : 45), 0) + " km² previsti entro " + p.raggio + " km.");
  if (chiave.startsWith("r:")) {         // la prova dei fatti: i quadretti arrivati davvero tramite questo ripetitore
    let raggiunti = 0, dentro = 0;
    for (const [, cop] of copertura) {
      for (const q of (cop.dati && cop.dati.celle) || []) {
        if (!Array.isArray(q) || !q[8] || !(q[8][chiave.slice(2)] > 0)) continue;
        raggiunti++;
        const prev = p.celle.get(q[0] + "," + q[1]);
        if (prev && prev.margine - correzioneDb(c, prev.km) >= 0) dentro++;
      }
    }
    if (raggiunti) righe.push(dentro + " su " + raggiunti + (raggiunti === 1 ? " quadretto arrivato" : " quadretti arrivati") +
                              " davvero tramite questo ripetitore " + (dentro === 1 ? "cade" : "cadono") + " nella zona prevista.");
  }
  if (chiave === "p:prova") {
    let nuovi = 0;
    for (const q of p.celle.values()) {
      if (q.margine - correzioneDb(c, q.km) < 0) continue;
      let altri = false;
      for (const [k, altro] of previsioni) {
        if (k === chiave || altro.stato !== "fatta") continue;
        const a = altro.celle.get(q.x + "," + q.y);
        if (a && a.margine - correzioneDb(corr.get(k), a.km) >= 0) { altri = true; break; }
      }
      if (!altri) nuovi++;
    }
    righe.push("Rispetto alla rete di oggi aggiunge " + numIt(kmqDi(nuovi, puntoDiProva.lat), 0) + " km² previsti.");
  }
  for (const l of p.collegamenti || []) {
    righe.push({testo: testoCollegamento(l), profilo: [chiave, l.chiave]});
  }
  if (chiave !== "p:prova") righe.push(seSiSpegne(chiave, corr));
  if (chiave !== "p:prova" && p.collegamenti && !p.collegamenti.length && chiave.startsWith("r:")) {
    righe.push("Nessun gateway entro " + p.raggio + " km: il collegamento non si calcola.");
  }
  return righe.filter(Boolean);
}

function disegnaElencoPreviste(corr) {
  const voci = sceltaCopertura.prevista && gestione ? impiantiSullaMappa().filter(n => !sceltaCopertura.mostra || n.chiave === sceltaCopertura.mostra) : [];
  const contenuto = voci.map(n => [n.chiave, n.nome, testoImpianto(n.chiave), vociPrevista(n.chiave, previsioni.get(n.chiave), corr)]);
  if (sceltaCopertura.prevista && gestione) contenuto.push(["portatile", "Radio portatili della squadra", testoImpianto("portatile"), []]);
  const firma = JSON.stringify(contenuto);
  if (firma === firmaElenco) return;        // rifarlo a ogni giro toglierebbe il bottone da sotto il dito
  firmaElenco = firma;
  elencoPreviste.replaceChildren(...contenuto.map(([chiave, nome, impianto, righe]) => {
    const li = el("li");
    li.dataset.impianto = chiave;
    const testa = el("div", "testa-prevista");
    const dati = el("button", "bottone secondario piccolo", "Dati dell'impianto");
    dati.type = "button";
    dati.addEventListener("click", () => apriImpianto(chiave, nome));
    testa.append(el("b", null, nome), dati);
    if (chiave === "p:prova") {
      const togli = el("button", "bottone secondario piccolo", "Togli il punto");
      togli.type = "button";
      togli.addEventListener("click", togliProva);
      testa.append(togli);
    }
    li.append(testa, el("p", "impianto", maiuscola(impianto) + "."), ...righe.map(r => {
      if (typeof r === "string") return el("p", null, r);
      const riga = el("p", null, r.testo + " "), profilo = el("button", "bottone secondario piccolo", "Profilo");
      profilo.type = "button";
      profilo.addEventListener("click", () => apriProfilo(capoImpianto(r.profilo[0]), capoImpianto(r.profilo[1]), null));
      riga.append(profilo);
      return riga;
    }));
    return li;
  }));
}

function aggiornaPrevista() {
  // a ogni ridisegno della mappa: quadretti previsti, stato e righe degli impianti; fa partire i calcoli che mancano
  if (!mappaCopertura || !statoPrevista) return;
  const accesa = sceltaCopertura.prevista;
  for (const e of partiPrevista) e.hidden = !accesa;
  if (spuntaPrevista.checked !== accesa) spuntaPrevista.checked = accesa;
  if (bottoneProva) bottoneProva.textContent = puntoDiProva ? "Sposta il punto di prova" : "Prova un punto";
  for (const [b, modo] of [[bottoneProva, "prova"], [bottoneCopri, "copri"]]) b.setAttribute("aria-pressed", String(modoTocco === modo));
  mappaCopertura.e.classList.toggle("sceglie", accesa && modoTocco !== "");
  if (accesa && !vocePrevista.isConnected) document.querySelector(".legenda").append(vocePrevista);
  if (!accesa) vocePrevista.remove();
  if (!accesa) {
    mostrate = new Map();
    firmaMostrate = "";
    mappaCopertura.previste(mostrate, []);
    mostraStatoPrevista();
    disegnaElencoPreviste(new Map());
    disegnaSiti();
    return;
  }
  if (gestione && !previsioneInCorso && previsioneDaFare()) avviaPrevisione();
  const corr = correzioniPrevista(), scelta = sceltaCopertura.mostra;
  const visti = impiantiSullaMappa().filter(n => (!scelta || n.chiave === scelta) && previsioni.has(n.chiave));
  const firma = JSON.stringify([scelta, visti.map(n => { const p = previsioni.get(n.chiave); const c = corr.get(n.chiave); return [n.chiave, p.firma, p.stato, c.a, c.b]; })]);
  if (firma !== firmaMostrate) {
    firmaMostrate = firma;
    mostrate = quadrettiPrevisti(corr);
    mappaCopertura.previste(mostrate, visti.map(n => ({lat: n.lat, lon: n.lon, km: Number(sceltaCopertura.raggio)})));
  }
  if (daInquadrare && visti.length) {
    daInquadrare = false;
    mappaCopertura.inquadra(visti.map(n => ({lat: n.lat, lon: n.lon, km: Number(sceltaCopertura.raggio)})));
  }
  mostraStatoPrevista();
  disegnaElencoPreviste(corr);
  disegnaSiti();
}

function accendiPrevista(chiave) {
  // dalla scheda di un gateway o di un ripetitore: la sua copertura prevista, sulla mappa
  sceltaCopertura.prevista = true;
  sceltaCopertura.mostra = chiave;
  selMostra.value = chiave;
  daInquadrare = true;
  disegnaCopertura();
  vaiA("mappa");
}

function togliProva() {
  puntoDiProva = null;
  impiantoDiProva = {};
  previsioni.delete("p:prova");
  if (sceltaCopertura.mostra === "p:prova") sceltaCopertura.mostra = "";
  disegnaCopertura();
}

function costruisciPrevista(comandi, scelta) {
  // i comandi della copertura prevista, accanto a «Mostra» e «Periodo», e sotto la mappa lo stato e gli impianti
  const l = el("label", "spunta");
  spuntaPrevista = el("input");
  spuntaPrevista.type = "checkbox";
  spuntaPrevista.addEventListener("change", () => {
    sceltaCopertura.prevista = spuntaPrevista.checked;
    modoTocco = "";
    daInquadrare = spuntaPrevista.checked;
    giroPrevisione++;
    previsioneInCorso = false;
    for (const [k, v] of previsioni) if (v.stato !== "fatta") previsioni.delete(k);     // riaccendendo si riprova subito
    disegnaCopertura();
  });
  l.append(spuntaPrevista, "Copertura prevista");
  comandi.append(l);
  const raggio = scelta("Fino a", [["10", "10 km"], ["20", "20 km"], ["30", "30 km"]], "raggio", () => {
    daInquadrare = true;
    disegnaCopertura();
  });
  // dalla 1.18.0 gli strumenti per decidere dove mettere un impianto nuovo, sotto la mappa
  const strumento = (testo, azione) => {
    const b = el("button", "bottone secondario piccolo", testo);
    b.type = "button";
    b.addEventListener("click", azione);
    return b;
  };
  const tocco = modo => () => {
    modoTocco = modoTocco === modo ? "" : modo;
    aggiornaPrevista();
  };
  bottoneTrova = strumento("Trova i punti migliori", trovaPunti);
  bottoneCopri = strumento("Copri un punto", tocco("copri"));
  bottoneProva = strumento("Prova un punto", tocco("prova"));
  const strumenti = el("div", "strumenti-prevista");
  strumenti.append(el("span", null, "Dove mettere un ripetitore:"), bottoneTrova, bottoneCopri, bottoneProva);
  statoSiti = el("p", "stato-copertura stato-siti");
  statoSiti.setAttribute("role", "status");
  esitoSiti = el("div", "esito-siti");
  partiPrevista = [raggio.parentNode, strumenti, statoSiti, esitoSiti];
  mappaCopertura.alTocco = (lat, lon) => {
    if (!modoTocco || !sceltaCopertura.prevista) return false;
    const modo = modoTocco, punto = {lat: Math.round(lat * 1e5) / 1e5, lon: Math.round(lon * 1e5) / 1e5};
    modoTocco = "";
    if (modo === "copri") {
      copriPunto(punto);
    } else {
      puntoDiProva = punto;
      sceltaCopertura.mostra = "p:prova";
      disegnaCopertura();
    }
    return true;
  };
  statoPrevista = el("p", "stato-copertura stato-prevista");
  statoPrevista.setAttribute("role", "status");
  elencoPreviste = el("ul", "elenco-previste");
  elencoPreviste.setAttribute("aria-label", "Impianti della copertura prevista");
  vocePrevista = el("li", "voce-prevista");
  vocePrevista.append(el("i", "segno-prevista"), el("span", null, "Prevista: gli stessi colori, più chiari e senza bordo"));
  return [statoPrevista, strumenti, statoSiti, esitoSiti, elencoPreviste];
}

function spiegaPrevista() {
  // in «Come leggere la mappa»
  const collegamento = (testo, url) => {
    const a = el("a", null, testo);
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    return a;
  };
  const confronto = el("p", null, "Per un secondo parere ci sono due siti esterni: il ");
  confronto.append(collegamento("Site Planner di Meshtastic", "https://site.meshtastic.org/"), " (dalla scheda di ogni gateway e " +
                   "ripetitore si apre già impostato) e, per cercare le cime migliori, ", collegamento("MeshPeaks", "https://meshpeaks.com/"), ".");
  const rilievo = el("p", null, "Il rilievo viene dai Terrain Tiles di Mapzen, dati pubblici (un punto ogni 53 metri circa): il nostro " +
                                "server li scarica una volta e li tiene. In Europa sono prodotti con dati e informazioni Copernicus " +
                                "finanziati dall'Unione Europea (EU-DEM) e con dati SRTM, per gentile concessione dell'U.S. Geological Survey (");
  rilievo.append(collegamento("tutte le fonti", "https://github.com/tilezen/joerd/blob/master/docs/attribution.md"),
                 "). Nomi delle cime, tralicci, serbatoi e rifugi: © collaboratori di ",
                 collegamento("OpenStreetMap", "https://www.openstreetmap.org/copyright"), ", chiesti dal nostro server.");
  return [
    el("p", null, "«Copertura prevista» colora anche dove nessuna radio è ancora passata: per ogni gateway e ripetitore la " +
                  "pagina calcola, dal rilievo del terreno, dove una radio portatile si collegherebbe. Usa i dati veri di " +
                  "ogni impianto (posizione, altezza e guadagno dell'antenna, cavo, potenza: «Dati dell'impianto») e conta " +
                  "tutti e due i versi, dalla radio all'impianto e ritorno."),
    el("p", null, "Il rilievo non conosce alberi, case e persone. Per questo il calcolo di ogni gateway viene tarato sui " +
                  "quadretti dove quel gateway ha sentito davvero le radio: la differenza media tra calcolo e misura " +
                  "corregge tutta la sua mappa, e l'errore tipico dice di quanto può sbagliare in un punto. I ripetitori " +
                  "non misurano il segnale: usano la correzione media dei gateway. Più quadretti misurati, più la " +
                  "previsione è affidabile; dove c'è una misura, vale la misura."),
    el("p", null, "Sotto la mappa, «Dove mettere un ripetitore». «Trova i punti migliori» cerca nella zona che la mappa " +
                  "mostra in quel momento le cime e i sostegni che ci sono già (tralicci, torri, serbatoi dell'acqua, " +
                  "rifugi) e propone i cinque che aggiungerebbero più zona alla rete di oggi, dicendo per ognuno se " +
                  "arriverebbe a un gateway o a un ripetitore. «Copri un punto» parte dal contrario: tocchi la valle o " +
                  "il rifugio che ti interessa e propone da dove lo si raggiungerebbe. «Prova un punto» calcola per " +
                  "intero un posto scelto da te. «Profilo» disegna il terreno tra due impianti: si vede quale " +
                  "crinale ferma il segnale. Proposte e punti restano solo finché la pagina è aperta."),
    el("p", null, "Per ogni impianto la pagina dice anche che cosa si perderebbe se si spegnesse: la zona che copre " +
                  "solo lui e, per un gateway, i ripetitori che resterebbero senza strada verso TAK."),
    confronto, rilievo];
}

// ---- i dati di un impianto: una finestra per gateway, ripetitori, punto di prova e radio portatili
function apriImpianto(chiave, nome) {
  const i = impiantoDi(chiave), portatile = chiave === "portatile";
  impiantoAperto = chiave;
  document.getElementById("impianto-titolo").textContent = portatile ? "Radio portatili della squadra" : "Impianto di " + nome;
  document.getElementById("impianto-spiega").textContent = portatile
    ? "Come sono, di solito, le radio che la squadra porta addosso: valgono per tutta la copertura prevista. Lascia vuoto quello che non sai."
    : "Servono per calcolare la copertura prevista di questo impianto" + (chiave === "p:prova" ? ": valgono finché la pagina resta aperta" : "") +
      ". Lascia vuoto quello che non sai: il calcolo usa il valore indicato nel campo.";
  document.getElementById("impianto-esito").textContent = "";
  document.getElementById("impianto-campi").replaceChildren(...CAMPI_IMPIANTO.map(([campo, titolo, unita, minimo, massimo]) => {
    const p = el("p"), l = el("label", null, titolo + " (" + unita + ")"), input = el("input");
    input.id = "impianto-" + campo;
    l.htmlFor = input.id;
    input.inputMode = "decimal";
    input.autocomplete = "off";
    input.dataset.campo = campo;
    input.dataset.minimo = minimo;
    input.dataset.massimo = massimo;
    input.dataset.titolo = titolo;
    if (Number.isFinite(i.scritti[campo])) input.value = numIt(i.scritti[campo]).replace(/\./g, "");
    const predefinito = i.origine[campo] === "scritto" ? IMPIANTO_FABBRICA[i.tipo][campo] : i.valori[campo];
    input.placeholder = numIt(predefinito) + (i.origine[campo] === "radio" ? " (letta dalla radio)" : " (di fabbrica)");
    l.append(el("small", null, AIUTI_IMPIANTO[campo][portatile ? "portatile" : "fisso"] +
                               (i.origine[campo] === "radio" ? " Adesso vale quella letta dalla radio del gateway." : "")));
    p.append(l, input);
    return p;
  }));
  document.getElementById("impianto").showModal();
}

async function salvaImpianto() {
  const chiave = impiantoAperto, esito = document.getElementById("impianto-esito"), dati = {chi: chiave};
  if (!chiave) return;
  for (const input of document.querySelectorAll("#impianto-campi input")) {
    const testo = input.value.trim().replace(",", ".");
    if (!testo) { dati[input.dataset.campo] = null; continue; }
    const v = Number(testo);
    if (!Number.isFinite(v) || v < Number(input.dataset.minimo) || v > Number(input.dataset.massimo)) {
      esito.textContent = input.dataset.titolo + ": serve un numero tra " + numIt(Number(input.dataset.minimo)) + " e " + numIt(Number(input.dataset.massimo)) + ".";
      input.focus();
      return;
    }
    dati[input.dataset.campo] = Math.round(v * 10) / 10;
  }
  try {
    if (chiave === "p:prova") {
      impiantoDiProva = Object.fromEntries(Object.entries(dati).filter(([k, v]) => k !== "chi" && v !== null));
    } else {
      esito.textContent = "Salvo...";
      const d = await manda("impianto", dati);
      if (gestione) gestione.impianti = d.impianti || {};
    }
    document.getElementById("impianto").close();
    disegna();
    disegnaCopertura();
  } catch (e) {
    esito.textContent = "Non salvati: " + frase(e.message);
  }
}

document.getElementById("impianto-annulla").addEventListener("click", () => document.getElementById("impianto").close());
document.getElementById("impianto-salva").addEventListener("click", salvaImpianto);
document.getElementById("impianto").addEventListener("close", () => { impiantoAperto = null; });
