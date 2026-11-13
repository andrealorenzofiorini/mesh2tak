// ---- Copertura prevista nelle schede di gateway e ripetitori. Dalla 1.17.0 la calcola la pagina, con i dati veri
// dell'impianto; resta, per un secondo parere, il Site Planner di Meshtastic (site.meshtastic.org), che accetta
// posizione, nome, frequenza, potenza e altezza nell'indirizzo.
function indirizzoPrevista(punto, impianto) {
  const q = new URLSearchParams({lat: punto.lat.toFixed(4), lon: punto.lon.toFixed(4), name: punto.nome.slice(0, 40),
                                 tx_freq: "869.525", tx_power: (10 ** (impianto.potenza_dbm / 10) / 1000).toFixed(3),
                                 tx_height: String(impianto.altezza_m), max_range: "30", run: "1"});
  return "https://site.meshtastic.org/?" + q.toString();
}

function fattoPrevista(dl, chiave) {
  const punto = puntiDellaRete().find(x => x.chiave === chiave);
  const d = el("div"), dd = el("dd");
  d.dataset.prevista = chiave;
  if (punto) {
    const p = previsioni.get(chiave);
    dd.append(maiuscola(testoImpianto(chiave)) + ".");
    if (sceltaCopertura.prevista && p && p.stato === "fatta") dd.append(el("small", "suggerimento-batteria", rigaTaratura(chiave, correzioniPrevista())));
    const azioni = el("div", "azioni-prevista");
    const mostra = el("button", "bottone secondario piccolo", "Calcola sulla mappa");
    mostra.type = "button";
    mostra.addEventListener("click", () => accendiPrevista(chiave));
    azioni.append(mostra);
    if (gestione) {
      const dati = el("button", "bottone secondario piccolo", "Dati dell'impianto");
      dati.type = "button";
      dati.addEventListener("click", () => apriImpianto(chiave, (punto.tipo === "gateway" ? "Gateway " : "") + punto.nome));
      azioni.append(dati);
    }
    const a = el("a", null, "Confronta con Site Planner");
    a.href = indirizzoPrevista(punto, impiantoDi(chiave).valori);
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.title = "Sito esterno: riceve posizione e nome dell'impianto";
    azioni.append(a);
    dd.append(azioni);
  } else {
    dd.append("serve la posizione: non è ancora sulla mappa");
  }
  d.append(el("dt", null, "Copertura prevista"), dd);
  dl.append(d);
}

// ---- Storico (dalla 1.16.0): come vanno nel tempo gateway e ripetitori. Ogni gateway tiene un campione ogni mezz'ora
// per 30 giorni (storico/NOME.json, dietro Authentik); qui i grafici, uno per misura.
const STORICO_OGNI_S = 600;
const storico = new Map();       // gateway -> {dati, errore}
let ultimoStorico = 0, periodoStorico = "7", corpoStorico = null;

function costruisciStorico() {
  vistaStorico.className = "storico";
  vistaStorico.id = "storico";
  const comandi = el("div", "comandi-copertura");
  const l = el("label", null, "Periodo");
  const sel = el("select");
  for (const [v, t] of [["1", "Ultime 24 ore"], ["7", "Ultimi 7 giorni"], ["30", "Ultimi 30 giorni"]]) {
    const o = el("option", null, t);
    o.value = v;
    sel.append(o);
  }
  sel.value = periodoStorico;
  sel.addEventListener("change", () => { periodoStorico = sel.value; disegnaStorico(); });
  l.append(sel);
  comandi.append(l);
  corpoStorico = el("div", "corpo-storico");
  vistaStorico.append(el("h2", null, "Storico"), comandi, corpoStorico,
                      el("p", "nota", "Un punto ogni mezz'ora, tenuto 30 giorni sul gateway. La batteria di un ripetitore c'è " +
                                      "solo se il ripetitore la manda."));
}

async function caricaStorico(forza) {
  const mancano = elenco.some(p => !storico.has(p.nome));
  if (!elenco.length || (!forza && !mancano && Date.now() - ultimoStorico < STORICO_OGNI_S * 1000)) return false;
  ultimoStorico = Date.now();
  await Promise.all(elenco.map(async p => {
    const prima = storico.get(p.nome);
    try {
      const r = await fetch("storico/" + encodeURIComponent(p.nome) + ".json",
                            {cache: "no-store", redirect: "manual", credentials: "same-origin"});
      if (r.type === "opaqueredirect") {
        document.getElementById("sessione").hidden = false;
        throw new Error("sessione scaduta");
      }
      if (!r.ok) {
        await r.text().catch(() => "");
        throw Object.assign(new Error("risposta " + r.status), {stato: r.status});
      }
      const d = await r.json();
      if (!d || !Array.isArray(d.ponte)) throw new Error("dati non validi");
      storico.set(p.nome, {dati: d, errore: ""});
    } catch (e) {
      storico.set(p.nome, {dati: prima ? prima.dati : null,
                           errore: e.stato === 404 ? "vecchio" : (e.stato === 502 || e.stato === 504 ? "giu" : e.message)});
    }
  }));
  for (const n of [...storico.keys()]) if (!elenco.some(p => p.nome === n)) storico.delete(n);
  return true;
}

function quandoBreve(t) {
  const d = new Date(t * 1000);
  return d.getDate() + "/" + (d.getMonth() + 1) + " " + String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
}

function grafico(titolo, punti, opz) {
  // una misura nel tempo: una linea sola, griglia leggera, il valore sotto il puntatore scritto accanto al titolo
  const {unita = "", cifre = 0, soglia = null, passo = 1800} = opz;
  const fig = el("figure", "grafico");
  const testa = el("figcaption");
  const lettura = el("span", "lettura");
  testa.append(el("b", null, titolo), lettura);
  fig.append(testa);
  const validi = punti.filter(x => x[1] !== null && x[1] !== undefined);
  if (!validi.length) {
    fig.append(el("p", "vuoto", opz.vuoto || "Nessun dato in questo periodo."));
    return fig;
  }
  const valori = validi.map(x => x[1]);
  const ultimo = validi[validi.length - 1];
  const riassunto = "ultimo " + numIt(ultimo[1], cifre) + unita + ", minimo " + numIt(Math.min(...valori), cifre) + unita +
                    ", massimo " + numIt(Math.max(...valori), cifre) + unita;
  lettura.textContent = riassunto;
  let min = opz.min !== undefined ? opz.min : Math.min(...valori), max = opz.max !== undefined ? opz.max : Math.max(...valori);
  if (opz.min === undefined || opz.max === undefined) {
    const margine = Math.max(1, (max - min) * 0.1);
    if (opz.min === undefined) min = Math.floor(min - margine);
    if (opz.max === undefined) max = Math.ceil(max + margine);
  }
  if (cifre === 0 && (max - min) % 2) max += 1;      // la tacca di mezzo cade su un numero intero
  const W = 640, H = 150, SX = 44, DX = 8, SU = 8, GIU = 22;
  const x = t => SX + (t - opz.da) / (opz.a - opz.da) * (W - SX - DX);
  const y = v => SU + (1 - (v - min) / (max - min || 1)) * (H - SU - GIU);
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 " + W + " " + H);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", titolo + ": " + riassunto);
  const metti = (tag, attributi, testo) => {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attributi)) e.setAttribute(k, v);
    if (testo !== undefined) e.textContent = testo;
    svg.append(e);
    return e;
  };
  for (const v of [min, (min + max) / 2, max]) {
    metti("line", {x1: SX, x2: W - DX, y1: y(v), y2: y(v), class: "griglia"});
    metti("text", {x: SX - 6, y: y(v) + 4, class: "asse", "text-anchor": "end"}, numIt(v, cifre) + unita);
  }
  const giorni = (opz.a - opz.da) / 86400;
  const tacche = giorni <= 1 ? 6 : Math.min(7, Math.round(giorni));
  for (let i = 0; i <= tacche; i++) {
    const t = opz.da + (opz.a - opz.da) * i / tacche, d = new Date(t * 1000);
    metti("text", {x: x(t), y: H - 6, class: "asse", "text-anchor": i === 0 ? "start" : (i === tacche ? "end" : "middle")},
          giorni <= 1 ? String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0")
                      : d.getDate() + "/" + (d.getMonth() + 1));
  }
  if (soglia !== null && soglia > min && soglia < max) {
    metti("line", {x1: SX, x2: W - DX, y1: y(soglia), y2: y(soglia), class: "soglia"});
  }
  // la linea si interrompe dove mancano i campioni (gateway spento, ripetitore che non manda)
  let d = "", prima = null;
  for (const [t, v] of validi) {
    d += (prima === null || t - prima > passo * 2.5 ? "M" : "L") + x(t).toFixed(1) + " " + y(Math.max(min, Math.min(max, v))).toFixed(1);
    prima = t;
  }
  metti("path", {d, class: "linea"});
  if (validi.length === 1) metti("circle", {cx: x(ultimo[0]), cy: y(ultimo[1]), r: 4, class: "punto"});
  const segno = metti("circle", {r: 4, class: "punto", visibility: "hidden"});
  const tocco = metti("rect", {x: SX, y: 0, width: W - SX - DX, height: H - GIU, fill: "transparent"});
  const indica = ev => {
    const r = svg.getBoundingClientRect(), t = opz.da + ((ev.clientX - r.left) / r.width * W - SX) / (W - SX - DX) * (opz.a - opz.da);
    const vicino = validi.reduce((m, p) => Math.abs(p[0] - t) < Math.abs(m[0] - t) ? p : m, validi[0]);
    segno.setAttribute("cx", x(vicino[0]));
    segno.setAttribute("cy", y(Math.max(min, Math.min(max, vicino[1]))));
    segno.setAttribute("visibility", "visible");
    lettura.textContent = quandoBreve(vicino[0]) + ": " + numIt(vicino[1], cifre) + unita;
  };
  tocco.addEventListener("pointermove", indica);
  tocco.addEventListener("pointerdown", indica);
  tocco.addEventListener("pointerleave", () => { segno.setAttribute("visibility", "hidden"); lettura.textContent = riassunto; });
  fig.append(svg);
  return fig;
}

function strisciaSentito(campioni, da, a) {
  // una tacca per mezz'ora: piena se il gateway lo ha sentito (o gli ha ripetuto un messaggio), vuota se no
  const fig = el("figure", "grafico striscia");
  const dentro = campioni.filter(c => c[0] >= da);
  const sentite = dentro.filter(c => c[4] === 1).length;
  const testa = el("figcaption");
  testa.append(el("b", null, "Sentito dal gateway"),
               el("span", "lettura", dentro.length ? "in " + sentite + " mezz'ore su " + dentro.length + " (" +
                                     Math.round(sentite / dentro.length * 100) + "%)" : "nessun dato in questo periodo"));
  fig.append(testa);
  if (!dentro.length) return fig;
  const W = 640, SX = 44, DX = 8, NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 " + W + " 16");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Sentito in " + sentite + " mezz'ore su " + dentro.length);
  const largo = Math.max(0.6, (W - SX - DX) / ((a - da) / 1800) - 0.4);
  for (const c of dentro) {
    const q = document.createElementNS(NS, "rect");
    q.setAttribute("x", (SX + (c[0] - da) / (a - da) * (W - SX - DX) - largo).toFixed(1));
    q.setAttribute("y", c[4] === 1 ? 2 : 6);
    q.setAttribute("width", largo.toFixed(1));
    q.setAttribute("height", c[4] === 1 ? 12 : 4);
    q.setAttribute("class", c[4] === 1 ? "sentito" : "muto");
    svg.append(q);
  }
  fig.append(svg);
  return fig;
}

function disegnaStorico() {
  if (!corpoStorico) return;
  const a = Date.now() / 1000, da = a - Number(periodoStorico) * 86400;
  const pezzi = [];
  if (!elenco.length) pezzi.push(el("p", "vuoto", "Nessun gateway nella rete."));
  const ripetitori = new Map();     // id -> il gateway che ne ha piu' campioni
  for (const p of elenco) {
    const s = storico.get(p.nome);
    const blocco = el("section", "blocco-storico");
    blocco.append(el("h3", null, "Gateway " + p.nome));
    if (!s) {
      blocco.append(el("p", "vuoto", "Carico lo storico..."));
    } else if (!s.dati) {
      blocco.append(el("p", "vuoto", s.errore === "vecchio" ? "Lo storico si raccoglie dalla 1.16.0: aggiorna il gateway." :
                       (s.errore === "giu" ? "Il gateway non risponde adesso." : "Storico non letto (" + s.errore + ").")));
    } else {
      const c = s.dati.ponte.filter(x => x[0] >= da);
      const griglia = el("div", "grafici");
      griglia.append(grafico("Canale radio occupato", c.map(x => [x[0], x[1]]), {da, a, min: 0, unita: "%", cifre: 1}),
                     grafico("Radio sulla mappa TAK", c.map(x => [x[0], x[3]]), {da, a, min: 0}));
      blocco.append(griglia);
      for (const [id, r] of Object.entries(s.dati.ripetitori || {})) {
        const altro = ripetitori.get(id);
        if (!altro || altro.campioni.length < r.campioni.length) ripetitori.set(id, Object.assign({gateway: p.nome}, r));
      }
    }
    pezzi.push(blocco);
  }
  const nomi = nomiDeiRipetitori();
  for (const [id, r] of [...ripetitori].sort((x, y) => (x[1].nome || x[0]).localeCompare(y[1].nome || y[0], "it"))) {
    const blocco = el("section", "blocco-storico");
    blocco.id = "storico-" + id.replace(/^!/, "");
    blocco.append(el("h3", null, nomi.get(id) || r.nome || "Ripetitore " + id));
    const c = r.campioni.filter(x => x[0] >= da);
    const griglia = el("div", "grafici");
    griglia.append(grafico("Batteria", c.map(x => [x[0], x[1] !== null && x[1] > 100 ? 100 : x[1]]),
                           {da, a, min: 0, max: 100, unita: "%", soglia: 20,
                            vuoto: "Nessun dato: il ripetitore non manda la batteria (vedi la sua scheda)."}),
                   grafico("Segnale verso il gateway " + r.gateway, c.map(x => [x[0], x[3]]),
                           {da, a, unita: " dB", cifre: 1, vuoto: "Nessun dato: il gateway non lo sente diretto."}));
    griglia.append(strisciaSentito(r.campioni, da, a));
    blocco.append(griglia);
    pezzi.push(blocco);
  }
  if (elenco.length && !ripetitori.size && [...storico.values()].some(s => s.dati)) {
    pezzi.push(el("p", "vuoto", "Nessun ripetitore nello storico finora: compaiono dopo la prima mezz'ora in cui un gateway li sente."));
  }
  corpoStorico.replaceChildren(...pezzi);
}

async function giro() {
  const g = aggiornaGestione();
  const ic = caricaIconeMappa();
  erroreGiro = "";
  try {
    await aggiornaElenco();
    await Promise.all(elenco.map(aggiornaPonte));
  } catch (e) {
    erroreGiro = e.message;
  }
  await g;
  await ic;
  disegna();
  if (iconaAperta) disegnaIconePonti();
  if (raspberryAperto) disegnaComandi();
  // la copertura (e la posizione di gateway e ripetitori) si rilegge ogni minuto: quando cambia, si ridisegna tutto
  if (await caricaCopertura(false)) disegna();
  disegnaCopertura();
  if (schedaAperta === "storico" && await caricaStorico(false)) disegnaStorico();
}

giro();
setInterval(() => { if (document.visibilityState === "visible") giro(); }, OGNI_S * 1000);
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") giro(); });
