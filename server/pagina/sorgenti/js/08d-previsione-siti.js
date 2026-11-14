// ---- Dove mettere un ripetitore (dalla 1.18.0): trova i punti migliori, copri un punto, il profilo tra due impianti,
// che cosa si perde se un impianto si spegne. Usa la copertura prevista (i due pezzi prima): stesso rilievo, stessi
// conti, stessa correzione. Cime con nome, tralicci, torri, serbatoi e rifugi arrivano dal nostro server
// (api/luoghi, che li chiede a OpenStreetMap); se non arrivano la ricerca funziona lo stesso, con le sole cime del rilievo.
const NOMI_SOSTEGNI = {traliccio: "traliccio", torre: "torre per telecomunicazioni", acqua: "serbatoio dell'acqua", rifugio: "rifugio"};
const ZONA_MAX_KM = 70;            // la zona dove cercare i punti migliori: quella che mostra la mappa, fino a qui
const luoghiTenuti = new Map();    // "la,lo" -> promessa dei luoghi di un quadrato di un quarto di grado
const siti = {giro: 0, lavoro: "", stato: "", ricerca: null, bersaglio: null};
let firmaSiti = "", perdite = {firma: "", testi: new Map()};

function metriTra(lat1, lon1, lat2, lon2) {
  const k = Math.PI / 180, x = (lon2 - lon1) * k * Math.cos((lat1 + lat2) / 2 * k), y = (lat2 - lat1) * k;
  return Math.hypot(x, y) * RAGGIO_TERRA_M;
}

function capoImpianto(chiave) {
  // un capo di un collegamento: nome, posizione e dati dell'impianto di un gateway, di un ripetitore o del punto di prova
  const n = impiantiSullaMappa().find(x => x.chiave === chiave);
  return n ? Object.assign({nome: n.nome, lat: n.lat, lon: n.lon, chiave}, impiantoDi(chiave).valori) : null;
}

function sitoNuovo(nome, lat, lon) {
  // un impianto che non c'e' ancora: con i dati del punto di prova (di fabbrica quelli di un ripetitore)
  return Object.assign({nome, lat, lon}, impiantoDi("p:prova").valori);
}

function testoCollegamento(l) {
  return "Collegamento con " + l.nome + " (" + numIt(l.km) + " km): " +
         (l.margine >= 0 ? classeCopertura(l.margine).breve + ", " + numIt(l.margine, 0) + " dB sopra il minimo"
                         : "non arriva, mancano " + numIt(-l.margine, 0) + " dB") +
         (l.ostacoli >= 1 ? "; il rilievo toglie " + numIt(l.ostacoli, 0) + " dB." : "; percorso libero dal rilievo.");
}

// ---- che cosa si perde se un impianto si spegne
function seSiSpegne(chiave, corr) {
  const nodi = impiantiSullaMappa().filter(n => n.tipo !== "prova");
  if (nodi.length < 2 || nodi.length > 30 || nodi.some(n => (previsioni.get(n.chiave) || {}).stato !== "fatta")) return "";
  const ripetitori = new Map(ripetitoriDellaRete().map(v => [v.id, v]));
  // i gateway a cui arriva ogni ripetitore: quelli del calcolo e quelli che lo sentono davvero
  const strade = new Map();
  for (const n of nodi.filter(x => x.tipo === "ripetitore")) {
    const g = new Set((previsioni.get(n.chiave).collegamenti || []).filter(l => l.margine >= 0).map(l => l.chiave));
    for (const x of (ripetitori.get(n.chiave.slice(2)) || {ponti: []}).ponti) g.add("g:" + x.ponte);
    strade.set(n.chiave, g);
  }
  const firma = JSON.stringify(nodi.map(n => { const c = corr.get(n.chiave); return [n.chiave, previsioni.get(n.chiave).firma, c.a, c.b, [...(strade.get(n.chiave) || [])]]; }));
  if (perdite.firma !== firma) {
    perdite = {firma, testi: new Map()};
    const chi = new Map();        // quadretto -> quali impianti lo coprono (un bit per impianto)
    nodi.forEach((n, i) => {
      const c = corr.get(n.chiave);
      for (const q of previsioni.get(n.chiave).celle.values()) {
        if (q.margine - correzioneDb(c, q.km) < 0) continue;
        const k = q.x * 262144 + q.y;
        chi.set(k, (chi.get(k) || 0) | (1 << i));
      }
    });
    const gateway = nodi.filter(n => n.tipo === "gateway");
    nodi.forEach((n, i) => {
      if (n.tipo === "gateway" && gateway.length === 1) {
        perdite.testi.set(n.chiave, "Se si spegne niente arriva più a TAK: è l'unico gateway.");
        return;
      }
      let spenti = 1 << i;
      const isolati = [];
      if (n.tipo === "gateway") {
        nodi.forEach((r, j) => {
          const g = strade.get(r.chiave);
          if (g && g.size && [...g].every(x => x === n.chiave)) { spenti |= 1 << j; isolati.push(r.nome); }
        });
      }
      let persi = 0;
      for (const m of chi.values()) if ((m & spenti) && !(m & ~spenti)) persi++;
      const kmq = kmqDi(persi, n.lat);
      perdite.testi.set(n.chiave, kmq < 0.5 && !isolati.length
        ? "Se si spegne non si perde zona prevista: resta coperta dagli altri impianti."
        : "Se si spegne si perdono " + numIt(kmq, 0) + " km² previsti" +
          (isolati.length ? ", compresi quelli di " + isolati.join(", ") + (isolati.length === 1 ? ", che resta" : ", che restano") + " senza gateway" : "") + ".");
    });
  }
  return perdite.testi.get(chiave) || "";
}

// ---- rilievo e luoghi di una zona
async function rilievoDellaZona(sud, ovest, nord, est, margineKm) {
  const dLat = margineKm / 111.32, dLon = margineKm / (111.32 * Math.cos((sud + nord) / 2 * Math.PI / 180));
  const x0 = Math.floor(mercatoreX(ovest - dLon, ZOOM_RILIEVO) / 256), x1 = Math.floor(mercatoreX(est + dLon, ZOOM_RILIEVO) / 256);
  const y0 = Math.floor(mercatoreY(nord + dLat, ZOOM_RILIEVO) / 256), y1 = Math.floor(mercatoreY(sud - dLat, ZOOM_RILIEVO) / 256);
  if ((x1 - x0 + 1) * (y1 - y0 + 1) > 130) throw new Error("zona troppo grande: avvicina la mappa");
  const lista = [];
  for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) lista.push([x, y]);
  return rilievoPer(lista, f => { siti.stato = "Prendo il rilievo della zona: " + Math.round(f * 100) + "%"; mostraStatoSiti(); });
}

function quadratoLuoghi(la, lo) {
  const k = la + "," + lo;
  if (!luoghiTenuti.has(k)) {
    const p = (async () => {
      const r = await fetch("api/luoghi/" + la + "/" + lo + ".json", {redirect: "manual", credentials: "same-origin"});
      if (!r.ok) {
        await r.text().catch(() => "");
        throw new Error("risposta " + r.status);
      }
      const d = await r.json();
      return {cime: Array.isArray(d.cime) ? d.cime : [], sostegni: Array.isArray(d.sostegni) ? d.sostegni : []};
    })();
    p.catch(() => luoghiTenuti.delete(k));
    luoghiTenuti.set(k, p);
  }
  return luoghiTenuti.get(k);
}

async function luoghiDellaZona(sud, ovest, nord, est) {
  // cime con nome e sostegni: se OpenStreetMap non risponde si va avanti senza ("manca")
  const esito = {cime: [], sostegni: [], manca: false}, attese = [];
  for (let la = Math.floor(sud * 4); la <= Math.floor(nord * 4); la++) {
    for (let lo = Math.floor(ovest * 4); lo <= Math.floor(est * 4); lo++) attese.push(quadratoLuoghi(la, lo));
  }
  if (attese.length > 16) return Object.assign(esito, {manca: true});
  for (const r of await Promise.allSettled(attese)) {
    if (r.status !== "fulfilled") { esito.manca = true; continue; }
    for (const c of r.value.cime) if (Array.isArray(c) && Number.isFinite(c[0]) && Number.isFinite(c[1])) esito.cime.push({lat: c[0], lon: c[1], nome: String(c[2] || "")});
    for (const s of r.value.sostegni) {
      if (Array.isArray(s) && Number.isFinite(s[0]) && Number.isFinite(s[1]) && NOMI_SOSTEGNI[s[2]]) esito.sostegni.push({lat: s[0], lon: s[1], tipo: s[2], nome: String(s[3] || "")});
    }
  }
  return esito;
}

function candidati(quota, sud, ovest, nord, est, luoghi, quante) {
  // i posti da provare in una zona: le cime del rilievo (con il nome e il sostegno piu' vicino, se OpenStreetMap li
  // sa) e i sostegni che non stanno su una cima
  const z = ZOOM_RILIEVO;
  const cime = cimeDelRilievo(quota, mercatoreX(ovest, z), mercatoreY(nord, z), mercatoreX(est, z), mercatoreY(sud, z), quante);
  const vicino = (lista, lat, lon, entro) => {
    let meglio = null;
    for (const v of lista) {
      const d = metriTra(lat, lon, v.lat, v.lon);
      if (d <= entro && (!meglio || d < meglio.metri)) meglio = Object.assign({metri: d}, v);
    }
    return meglio;
  };
  const posti = cime.map(c => {
    const nome = vicino(luoghi.cime.filter(x => x.nome), c.lat, c.lon, 400), sostegno = vicino(luoghi.sostegni, c.lat, c.lon, 300);
    return {lat: Math.round(c.lat * 1e5) / 1e5, lon: Math.round(c.lon * 1e5) / 1e5, quota: c.quota,
            nome: nome ? nome.nome : (sostegno && sostegno.nome ? sostegno.nome : "Cima senza nome"), sostegno};
  });
  const soli = luoghi.sostegni.filter(s => s.lat >= sud && s.lat <= nord && s.lon >= ovest && s.lon <= est &&
                                           !posti.some(p => metriTra(p.lat, p.lon, s.lat, s.lon) <= 300))
    .map(s => ({lat: s.lat, lon: s.lon, quota: quota(mercatoreX(s.lon, z), mercatoreY(s.lat, z)), nome: s.nome || maiuscola(NOMI_SOSTEGNI[s.tipo]),
                sostegno: Object.assign({metri: 0}, s)}))
    .filter(s => Number.isFinite(s.quota)).sort((a, b) => b.quota - a.quota).slice(0, 25);
  const esistenti = impiantiSullaMappa().filter(n => n.tipo !== "prova");
  return posti.concat(soli).filter(p => !esistenti.some(n => metriTra(p.lat, p.lon, n.lat, n.lon) < 800));
}

function collegamentoMigliore(sito, radio, quota) {
  // con quale impianto di oggi (gateway o ripetitore) si collegherebbe meglio un sito nuovo
  let meglio = null;
  for (const n of impiantiSullaMappa().filter(x => x.tipo !== "prova")) {
    const c = margineTraImpianti(sito, capoImpianto(n.chiave), radio, quota);
    if (c && (!meglio || c.margine > meglio.margine)) meglio = Object.assign(c, {nome: n.nome, chiave: n.chiave});
  }
  return meglio;
}

function arrivaAllaRete(p) {
  // prima i siti che si collegherebbero bene a un impianto di oggi (almeno "discreto"), poi quelli al limite, poi gli altri
  const m = p.collegamento ? p.collegamento.margine : -Infinity;
  return m >= 5 ? 2 : (m >= 0 ? 1 : 0);
}

async function reteCalcolata() {
  // i siti nuovi si confrontano con la rete di oggi: prima deve essere calcolata tutta
  for (let i = 0; i < 900; i++) {
    if (!sceltaCopertura.prevista || !gestione) return false;
    if (!previsioneInCorso && !previsioneDaFare()) return true;
    await new Promise(f => setTimeout(f, 200));
  }
  return false;
}

function lontani(lista, metri, quanti) {
  // i primi "quanti" della lista, lontani tra loro almeno "metri" (non cinque punti sullo stesso crinale)
  const scelti = [];
  for (const p of lista) {
    if (scelti.length >= quanti) break;
    if (!scelti.some(s => metriTra(s.lat, s.lon, p.lat, p.lon) < metri)) scelti.push(p);
  }
  return scelti;
}

async function trovaPunti() {
  // i cinque punti della zona in vista che aggiungerebbero piu' copertura prevista alla rete di oggi
  if (siti.lavoro) return;
  const giro = ++siti.giro, v = mappaCopertura.vista(), raggio = Number(sceltaCopertura.raggio);
  // la zona: quella che la mappa mostra, ma non oltre «Fino a» dalla rete di oggi (piu' lontano un impianto nuovo
  // non si collegherebbe a nessuno)
  const rete = impiantiSullaMappa().filter(n => n.tipo !== "prova");
  if (!rete.length) {
    siti.stato = "Nessun gateway o ripetitore ha ancora una posizione: i punti nuovi si cercano intorno alla rete.";
    mostraStatoSiti();
    return;
  }
  const dLat = raggio / 111.32;
  v.sud = Math.max(v.sud, Math.min(...rete.map(n => n.lat)) - dLat);
  v.nord = Math.min(v.nord, Math.max(...rete.map(n => n.lat)) + dLat);
  v.ovest = Math.max(v.ovest, Math.min(...rete.map(n => n.lon - dLat / Math.cos(n.lat * Math.PI / 180))));
  v.est = Math.min(v.est, Math.max(...rete.map(n => n.lon + dLat / Math.cos(n.lat * Math.PI / 180))));
  const largo = (v.est - v.ovest) * 111.32 * Math.cos((v.sud + v.nord) / 2 * Math.PI / 180), alto = (v.nord - v.sud) * 111.32;
  if (largo <= 0 || alto <= 0) {
    siti.stato = "Sposta la mappa sulla rete: cerco i punti nella zona che la mappa mostra, entro " + raggio + " km dagli impianti di oggi.";
    mostraStatoSiti();
    return;
  }
  if (Math.max(largo, alto) > ZONA_MAX_KM) {
    siti.stato = "Avvicina la mappa: cerco nella zona che mostra, larga al massimo " + ZONA_MAX_KM + " km (adesso " + numIt(Math.max(largo, alto), 0) + " km).";
    mostraStatoSiti();
    return;
  }
  siti.lavoro = "trova";
  siti.stato = "Calcolo prima la rete di oggi...";
  aggiornaPrevista();
  try {
    if (!await reteCalcolata()) throw new Error("la rete di oggi non è ancora calcolata");
    const quota = await rilievoDellaZona(v.sud, v.ovest, v.nord, v.est, raggio + 1);
    siti.stato = "Chiedo cime, tralicci e rifugi della zona...";
    mostraStatoSiti();
    const luoghi = await luoghiDellaZona(v.sud, v.ovest, v.nord, v.est);
    const posti = candidati(quota, v.sud, v.ovest, v.nord, v.est, luoghi, 40);
    const corr = correzioniPrevista(), comune = corr.get("nuovi"), radio = radioPerLaPrevisione(""), portatile = impiantoDi("portatile").valori;
    const oggi = new Set();        // i quadretti gia' previsti dalla rete di oggi (uno ogni due per lato, come il confronto)
    for (const [chiave, p] of previsioni) {
      if (chiave === "p:prova" || p.stato !== "fatta") continue;
      const c = corr.get(chiave);
      for (const q of p.celle.values()) if (q.x % 2 === 0 && q.y % 2 === 0 && q.margine - correzioneDb(c, q.km) >= 0) oggi.add(q.x * 262144 + q.y);
    }
    const valutati = [];
    for (let i = 0; i < posti.length; i++) {
      siti.stato = "Provo i punti della zona: " + (i + 1) + " di " + posti.length;
      mostraStatoSiti();
      await new Promise(f => setTimeout(f));
      if (giro !== siti.giro) return;
      const p = posti[i], sito = sitoNuovo(p.nome, p.lat, p.lon);
      let esito;
      try {
        const calcolo = prevediCopertura(sito, radio, portatile, quota, raggio, 0, 2);
        let passo = calcolo.next();
        while (!passo.done) passo = calcolo.next();
        esito = passo.value;
      } catch (e) {
        continue;       // un punto senza rilievo sotto: si salta
      }
      let coperti = 0, nuovi = 0;
      for (const q of esito.celle.values()) {
        if (q.margine - correzioneDb(comune, q.km) < 0) continue;
        coperti++;
        if (!oggi.has(q.x * 262144 + q.y)) nuovi++;
      }
      valutati.push(Object.assign(p, {nuoviKmq: kmqDi(nuovi * 4, p.lat), totaleKmq: kmqDi(coperti * 4, p.lat),
                                      collegamento: collegamentoMigliore(sito, radio, quota)}));
    }
    valutati.sort((a, b) => arrivaAllaRete(b) - arrivaAllaRete(a) || b.nuoviKmq - a.nuoviKmq);
    siti.ricerca = {proposte: lontani(valutati.filter(p => p.nuoviKmq >= 0.5), 2500, 5), provati: posti.length, senzaOsm: luoghi.manca,
                    raggio, impianto: testoImpianto("p:prova")};
    siti.ricerca.proposte.forEach((p, i) => { p.sigla = String(i + 1); });
    siti.stato = "";
  } catch (e) {
    if (giro === siti.giro) siti.stato = "Ricerca non riuscita: " + frase(e.message);
  } finally {
    if (giro === siti.giro) siti.lavoro = "";
  }
  disegnaCopertura();
}

async function copriPunto(punto) {
  // un punto da coprire (una valle, un rifugio): com'e' messo oggi, e da quali cime o sostegni lo si raggiungerebbe
  const giro = ++siti.giro, raggio = Number(sceltaCopertura.raggio);
  siti.lavoro = "copri";
  siti.bersaglio = {lat: punto.lat, lon: punto.lon, proposte: [], oggi: null, pronto: false};
  siti.stato = "Calcolo prima la rete di oggi...";
  disegnaCopertura();
  try {
    if (!await reteCalcolata()) throw new Error("la rete di oggi non è ancora calcolata");
    const dLat = raggio / 111.32, dLon = raggio / (111.32 * Math.cos(punto.lat * Math.PI / 180));
    const sud = punto.lat - dLat, nord = punto.lat + dLat, ovest = punto.lon - dLon, est = punto.lon + dLon;
    const quota = await rilievoDellaZona(sud, ovest, nord, est, 2);
    const qui = quota(mercatoreX(punto.lon, ZOOM_RILIEVO), mercatoreY(punto.lat, ZOOM_RILIEVO));
    if (!Number.isFinite(qui)) throw new Error("manca il rilievo in quel punto");
    siti.stato = "Chiedo cime, tralicci e rifugi della zona...";
    mostraStatoSiti();
    const luoghi = await luoghiDellaZona(sud, ovest, nord, est);
    if (giro !== siti.giro) return;
    const corr = correzioniPrevista(), comune = corr.get("nuovi"), radio = radioPerLaPrevisione("");
    const radioAddosso = Object.assign({nome: "Punto da coprire", lat: punto.lat, lon: punto.lon}, impiantoDi("portatile").valori);
    // oggi: l'impianto che ci arriva meglio
    let oggi = null;
    for (const n of impiantiSullaMappa().filter(x => x.tipo !== "prova")) {
      const c = margineTraImpianti(capoImpianto(n.chiave), radioAddosso, radioPerLaPrevisione(n.chiave), quota);
      if (!c) continue;
      const margine = c.margine - correzioneDb(corr.get(n.chiave) || comune, c.km);
      if (!oggi || margine > oggi.margine) oggi = {margine, km: c.km, ostacoli: c.ostacoli, nome: n.nome, chiave: n.chiave};
    }
    const provati = candidati(quota, sud, ovest, nord, est, luoghi, 150), buoni = [];
    for (let i = 0; i < provati.length; i++) {
      if (i % 25 === 24) {
        await new Promise(f => setTimeout(f));
        if (giro !== siti.giro) return;
      }
      const p = provati[i], d = metriTra(p.lat, p.lon, punto.lat, punto.lon);
      if (d < 300 || d > raggio * 1000) continue;
      const sito = sitoNuovo(p.nome, p.lat, p.lon), c = margineTraImpianti(sito, radioAddosso, radio, quota);
      if (!c) continue;
      const margine = c.margine - correzioneDb(comune, c.km);
      if (margine < 0) continue;
      buoni.push(Object.assign(p, {alPunto: {margine, km: c.km, ostacoli: c.ostacoli}, collegamento: collegamentoMigliore(sito, radio, quota)}));
    }
    buoni.sort((a, b) => arrivaAllaRete(b) - arrivaAllaRete(a) || b.alPunto.margine - a.alPunto.margine);
    Object.assign(siti.bersaglio, {quota: qui, oggi, proposte: lontani(buoni, 1000, 3), provati: provati.length, senzaOsm: luoghi.manca,
                                   raggio, impianto: testoImpianto("p:prova"), pronto: true});
    siti.bersaglio.proposte.forEach((p, i) => { p.sigla = "ABC"[i]; });
    siti.stato = "";
  } catch (e) {
    if (giro === siti.giro) siti.stato = "Punto non calcolato: " + frase(e.message);
  } finally {
    if (giro === siti.giro) siti.lavoro = "";
  }
  disegnaCopertura();
}

function provaQui(p) {
  puntoDiProva = {lat: p.lat, lon: p.lon};
  sceltaCopertura.mostra = "p:prova";
  daInquadrare = true;
  disegnaCopertura();
}

function mostraStatoSiti() {
  if (!statoSiti) return;
  const testo = sceltaCopertura.prevista ? siti.stato : "";
  if (statoSiti.textContent !== testo) statoSiti.textContent = testo;
}

function disegnaSiti() {
  // sotto la mappa le proposte, sulla mappa i loro segni (numeri per i punti migliori, lettere per chi copre il punto)
  if (!esitoSiti) return;
  const accesa = sceltaCopertura.prevista && gestione;
  bottoneTrova.disabled = !!siti.lavoro;
  bottoneTrova.textContent = siti.lavoro === "trova" ? "Cerco..." : "Trova i punti migliori";
  mostraStatoSiti();
  const r = accesa ? siti.ricerca : null, b = accesa ? siti.bersaglio : null;
  const segni = [];
  const righeSito = p => [p.nome + ", " + numIt(p.quota, 0) + " m",
                          p.sostegno ? (p.sostegno.metri < 30 ? "C'è già un " : "A " + numIt(p.sostegno.metri, 0) + " m c'è un ") + NOMI_SOSTEGNI[p.sostegno.tipo] +
                                       (p.sostegno.nome && p.sostegno.nome !== p.nome ? " (" + p.sostegno.nome + ")" : "") + "." : "",
                          p.collegamento ? testoCollegamento(p.collegamento) : "Nessun impianto di oggi entro " + sceltaCopertura.raggio + " km."];
  for (const p of (r ? r.proposte : [])) {
    segni.push({tipo: "proposta", sigla: p.sigla, lat: p.lat, lon: p.lon,
                righe: ["Punto " + p.sigla + ": " + p.nome + ", " + numIt(p.quota, 0) + " m", "Aggiungerebbe " + numIt(p.nuoviKmq, 0) + " km² previsti."]});
  }
  if (b) {
    segni.push({tipo: "bersaglio", lat: b.lat, lon: b.lon, righe: ["Punto da coprire", b.oggi && b.oggi.margine >= 0 ? "Oggi è previsto coperto." : "Oggi non è previsto coperto."]});
    for (const p of b.proposte) {
      segni.push({tipo: "proposta", sigla: p.sigla, lat: p.lat, lon: p.lon,
                  righe: ["Punto " + p.sigla + ": " + p.nome + ", " + numIt(p.quota, 0) + " m", "Arriverebbe al punto da coprire: " + classeCopertura(p.alPunto.margine).breve + "."]});
    }
  }
  const firma = JSON.stringify([r, b]);
  if (firma === firmaSiti) return;
  firmaSiti = firma;
  mappaCopertura.segni(segni);
  const bottone = (testo, azione) => {
    const x = el("button", "bottone secondario piccolo", testo);
    x.type = "button";
    x.addEventListener("click", azione);
    return x;
  };
  const voce = (p, primaRiga, profilo) => {
    const li = el("li"), testa = el("div", "testa-prevista");
    li.dataset.sito = p.sigla;
    testa.append(el("b", null, p.sigla + ". " + p.nome + ", " + numIt(p.quota, 0) + " m"), bottone("Prova qui", () => provaQui(p)), profilo,
                 bottone("Centra", () => { mappaCopertura.centra(p.lat, p.lon); vaiA("mappa"); }));
    li.append(testa, el("p", null, primaRiga), ...righeSito(p).slice(1).filter(Boolean).map(t => el("p", null, t)));
    return li;
  };
  const fonte = x => x.senzaOsm ? " OpenStreetMap non ha risposto: le cime sono senza nome e non so dove ci sono tralicci o rifugi."
                               : " Nomi e sostegni: © collaboratori di OpenStreetMap.";
  const blocchi = [];
  if (r) {
    const blocco = el("section", "proposte"), testa = el("div", "testa-prevista");
    blocco.dataset.proposte = "trova";
    testa.append(el("b", null, "I punti migliori nella zona della mappa"), bottone("Togli le proposte", () => { siti.ricerca = null; disegnaCopertura(); }));
    blocco.append(testa, el("p", "impianto", (r.proposte.length
      ? "Tra " + r.provati + " cime e sostegni, quelli che aggiungerebbero più zona alla rete di oggi, ognuno da solo, entro " + r.raggio + " km, con un impianto così: " + r.impianto + "."
      : "Tra " + r.provati + " cime e sostegni nessuno aggiungerebbe zona alla rete di oggi: sposta la mappa o allarga «Fino a».") + fonte(r)));
    const lista = el("ol", "elenco-previste");
    lista.append(...r.proposte.map(p => voce(p, "Aggiungerebbe " + numIt(p.nuoviKmq, 0) + " km² previsti (in tutto ne coprirebbe " + numIt(p.totaleKmq, 0) + ").",
      p.collegamento ? bottone("Profilo", () => apriProfilo(sitoNuovo(p.nome, p.lat, p.lon), capoImpianto(p.collegamento.chiave), null)) : "")));
    blocco.append(lista);
    blocchi.push(blocco);
  }
  if (b) {
    const blocco = el("section", "proposte"), testa = el("div", "testa-prevista");
    blocco.dataset.proposte = "copri";
    const radioAddosso = Object.assign({nome: "Punto da coprire", lat: b.lat, lon: b.lon}, impiantoDi("portatile").valori);
    testa.append(el("b", null, "Punto da coprire" + (b.pronto ? ", " + numIt(b.quota, 0) + " m" : "")),
                 bottone("Togli il punto", () => { siti.bersaglio = null; siti.giro++; siti.lavoro = ""; siti.stato = ""; disegnaCopertura(); }));
    blocco.append(testa);
    if (b.pronto) {
      const oggi = el("p", null, !b.oggi ? "Oggi: nessun impianto entro " + b.raggio + " km. "
        : (b.oggi.margine >= 0 ? "Oggi è già previsto coperto da " + b.oggi.nome + ": " + classeCopertura(b.oggi.margine).breve + ", " + numIt(b.oggi.margine, 0) + " dB sopra il minimo. "
                               : "Oggi non è previsto coperto: il più vicino a farcela è " + b.oggi.nome + ", mancano " + numIt(-b.oggi.margine, 0) + " dB. "));
      if (b.oggi) oggi.append(bottone("Profilo", () => apriProfilo(capoImpianto(b.oggi.chiave), radioAddosso, correzioniPrevista().get(b.oggi.chiave))));
      blocco.append(oggi, el("p", "impianto", (b.proposte.length
        ? "Da dove lo si raggiungerebbe, tra " + b.provati + " cime e sostegni entro " + b.raggio + " km, con un impianto così: " + b.impianto + "."
        : "Tra " + b.provati + " cime e sostegni entro " + b.raggio + " km nessuno lo raggiungerebbe.") + fonte(b)));
      const lista = el("ol", "elenco-previste");
      lista.append(...b.proposte.map(p => voce(p, "Arriverebbe al punto: " + classeCopertura(p.alPunto.margine).breve + ", " + numIt(p.alPunto.margine, 0) +
        " dB sopra il minimo (" + numIt(p.alPunto.km) + " km).",
        bottone("Profilo fino al punto", () => apriProfilo(sitoNuovo(p.nome, p.lat, p.lon), radioAddosso, correzioniPrevista().get("nuovi"))))));
      blocco.append(lista);
    }
    blocchi.push(blocco);
  }
  esitoSiti.replaceChildren(...blocchi);
}

// ---- il profilo: il terreno tra due capi, la linea tra le antenne, lo spazio che deve restare libero
async function apriProfilo(a, b, corr) {
  // a e b: {nome, lat, lon, altezza_m, guadagno_dbi, perdite_db, potenza_dbm}; corr: la correzione (alberi, case,
  // radio addosso) se uno dei due capi e' una radio portatile, null tra due impianti fissi
  if (!a || !b) return;
  const finestra = document.getElementById("profilo"), esito = document.getElementById("profilo-esito"), figura = document.getElementById("profilo-figura");
  document.getElementById("profilo-titolo").textContent = "Profilo: da " + a.nome + " a " + b.nome;
  esito.textContent = "Prendo il rilievo...";
  figura.replaceChildren();
  if (!finestra.open) finestra.showModal();
  try {
    const radio = radioPerLaPrevisione(a.chiave || "");
    const quota = await rilievoPerProfilo(a, b);
    const p = profiloCollegamento(a, b, radio, quota), m = margineTraImpianti(a, b, radio, quota);
    if (!p || !m) throw new Error("manca il rilievo lungo la linea");
    const margine = m.margine - (corr ? correzioneDb(corr, m.km) : 0);
    const come = !p.ostacolo ? "Tra le due antenne il terreno non entra: percorso libero."
      : (p.ostacolo.sporge > 0 ? "A " + numIt(p.ostacolo.km) + " km il terreno supera di " + numIt(p.ostacolo.sporge, 0) + " m la linea tra le antenne"
                               : "Le antenne si vedono, ma a " + numIt(p.ostacolo.km) + " km il terreno entra nello spazio che dovrebbe restare libero") +
        ": il rilievo toglie " + numIt(p.ostacoli, 0) + " dB.";
    esito.textContent = "Distanza " + numIt(p.km) + " km. " + come + " " +
      (margine >= 0 ? "Collegamento previsto: " + classeCopertura(margine).breve + ", " + numIt(margine, 0) + " dB sopra il minimo"
                    : "Collegamento previsto: non arriva, mancano " + numIt(-margine, 0) + " dB") +
      (corr ? " (contando alberi, case e radio addosso)." : " (solo distanza e rilievo: antenne ferme).");
    figura.replaceChildren(...disegnaProfilo(p, a, b));
  } catch (e) {
    esito.textContent = "Profilo non disegnato: " + frase(e.message);
  }
}

function rilievoPerProfilo(a, b) {
  const sud = Math.min(a.lat, b.lat), nord = Math.max(a.lat, b.lat), ovest = Math.min(a.lon, b.lon), est = Math.max(a.lon, b.lon);
  const z = ZOOM_RILIEVO, lista = [];
  for (let y = Math.floor(mercatoreY(nord, z) / 256 - 0.01); y <= Math.floor(mercatoreY(sud, z) / 256 + 0.01); y++) {
    for (let x = Math.floor(mercatoreX(ovest, z) / 256 - 0.01); x <= Math.floor(mercatoreX(est, z) / 256 + 0.01); x++) lista.push([x, y]);
  }
  if (lista.length > 60) throw new Error("i due capi sono troppo lontani");
  return rilievoPer(lista, () => {});
}

function disegnaProfilo(p, a, b) {
  const testa = el("figcaption"), lettura = el("span", "lettura");
  const riassunto = "Quote in metri, distanza in chilometri; la curvatura della Terra è già contata nel terreno.";
  lettura.textContent = riassunto;
  testa.append(el("b", null, "Il terreno lungo la linea"), lettura);
  const W = 640, H = 250, SX = 46, DX = 12, SU = 24, GIU = 24, NS = "http://www.w3.org/2000/svg";
  const quote = p.punti.map(q => q.terreno);
  let min = Math.min(...quote, ...p.punti.map(q => q.libero)), max = Math.max(...quote, ...p.antenne);
  const bordo = Math.max(10, (max - min) * 0.08);
  min = Math.floor((min - bordo) / 10) * 10;
  max = Math.ceil((max + bordo) / 10) * 10;
  const x = km => SX + km / p.km * (W - SX - DX), y = v => SU + (1 - (v - min) / (max - min)) * (H - SU - GIU);
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 " + W + " " + H);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Profilo del terreno da " + a.nome + " a " + b.nome + ", " + numIt(p.km) + " km");
  const metti = (tag, attributi, testo) => {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attributi)) e.setAttribute(k, v);
    if (testo !== undefined) e.textContent = testo;
    svg.append(e);
    return e;
  };
  for (const v of [min, (min + max) / 2, max]) {
    metti("line", {x1: SX, x2: W - DX, y1: y(v), y2: y(v), class: "griglia"});
    metti("text", {x: SX - 6, y: y(v) + 4, class: "asse", "text-anchor": "end"}, numIt(v, 0) + " m");
  }
  for (let i = 0; i <= 4; i++) {
    metti("text", {x: x(p.km * i / 4), y: H - 6, class: "asse", "text-anchor": i === 0 ? "start" : (i === 4 ? "end" : "middle")},
          numIt(p.km * i / 4, p.km < 20 ? 1 : 0) + " km");
  }
  const tratto = campo => p.punti.map((q, i) => (i ? "L" : "M") + x(q.km).toFixed(1) + " " + y(q[campo]).toFixed(1)).join("");
  metti("path", {d: tratto("terreno") + "L" + x(p.km).toFixed(1) + " " + y(min) + "L" + x(0) + " " + y(min) + "Z", class: "terreno"});
  // lo spazio che deve restare libero: tra la linea e la curva sotto
  metti("path", {d: tratto("linea") + [...p.punti].reverse().map(q => "L" + x(q.km).toFixed(1) + " " + y(q.libero).toFixed(1)).join("") + "Z", class: "libero"});
  metti("path", {d: tratto("linea"), class: "linea"});
  for (const [km, quota, cima, ancora] of [[0, p.punti[0].terreno, p.antenne[0], "start"], [p.km, p.punti[p.punti.length - 1].terreno, p.antenne[1], "end"]]) {
    metti("line", {x1: x(km), x2: x(km), y1: y(quota), y2: y(cima), class: "palo"});
    metti("circle", {cx: x(km), cy: y(cima), r: 4, class: "punto"});
    metti("text", {x: x(km), y: Math.max(12, y(cima) - 9), class: "asse capo", "text-anchor": ancora}, (ancora === "start" ? a : b).nome.slice(0, 28));
  }
  if (p.ostacolo) {
    const q = p.punti.reduce((m, v) => Math.abs(v.km - p.ostacolo.km) < Math.abs(m.km - p.ostacolo.km) ? v : m, p.punti[0]);
    metti("circle", {cx: x(q.km), cy: y(q.terreno), r: 5, class: "ostacolo"});
    metti("text", {x: x(q.km), y: y(q.terreno) - 10, class: "asse", "text-anchor": q.km > p.km * 0.75 ? "end" : (q.km < p.km * 0.25 ? "start" : "middle")},
          p.ostacolo.sporge > 0 ? "supera la linea di " + numIt(p.ostacolo.sporge, 0) + " m" : "entra nello spazio libero");
  }
  const filo = metti("line", {y1: SU, y2: H - GIU, class: "filo", visibility: "hidden"});
  const tocco = metti("rect", {x: SX, y: 0, width: W - SX - DX, height: H - GIU, fill: "transparent"});
  const indica = ev => {
    const r = svg.getBoundingClientRect(), km = ((ev.clientX - r.left) / r.width * W - SX) / (W - SX - DX) * p.km;
    const q = p.punti.reduce((m, v) => Math.abs(v.km - km) < Math.abs(m.km - km) ? v : m, p.punti[0]);
    filo.setAttribute("x1", x(q.km));
    filo.setAttribute("x2", x(q.km));
    filo.setAttribute("visibility", "visible");
    lettura.textContent = "A " + numIt(q.km) + " km: terreno a " + numIt(q.quota, 0) + " m; " +
      (q.linea >= q.terreno ? "la linea tra le antenne passa " + numIt(q.linea - q.terreno, 0) + " m sopra." : "il terreno supera la linea di " + numIt(q.terreno - q.linea, 0) + " m.");
  };
  tocco.addEventListener("pointermove", indica);
  tocco.addEventListener("pointerdown", indica);
  tocco.addEventListener("pointerleave", () => { filo.setAttribute("visibility", "hidden"); lettura.textContent = riassunto; });
  const legenda = el("ul", "legenda-profilo");
  for (const [classe, testo] of [["terreno", "Terreno"], ["linea", "Linea tra le antenne"], ["libero", "Spazio che deve restare libero"]]) {
    const li = el("li");
    li.append(el("i", "segno-" + classe), el("span", null, testo));
    legenda.append(li);
  }
  return [testa, svg, legenda];
}

document.getElementById("profilo-chiudi").addEventListener("click", () => document.getElementById("profilo").close());
