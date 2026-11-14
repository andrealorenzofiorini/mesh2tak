// ---- Copertura prevista (dalla 1.17.0): il calcolo. Solo conti, niente pagina: si prova anche da solo (tests/) ----
// Per un gateway o un ripetitore, con i suoi dati veri (posizione, altezza e guadagno dell'antenna, perdite del cavo,
// potenza, impostazione radio), dice quadretto per quadretto con che margine una radio portatile si collegherebbe.
// Modello: attenuazione nello spazio libero piu' diffrazione sul rilievo (ostacoli a lama di coltello in cascata,
// raccomandazione ITU-R P.526, metodo di Deygout con tre ostacoli), con la curvatura della Terra (raggio 4/3). Il
// rilievo non conosce alberi e case: quello che manca lo misura la taratura, che confronta il calcolo con i quadretti
// dove quel gateway ha sentito davvero le radio e sposta tutto della differenza.
const RAGGIO_TERRA_M = 6371000;
const RAGGIO_EFFICACE_M = RAGGIO_TERRA_M * 4 / 3;      // atmosfera normale: le onde curvano un po' verso terra
const ZOOM_RILIEVO = 11;                               // circa 53 m per punto alle nostre latitudini
const ZOOM_QUADRETTI = 17;                             // gli stessi quadretti della copertura misurata
const CIFRA_RUMORE_DB = 6;                             // ricevitore SX126x
const SENZA_TARATURA = {a: 10, b: 1};                  // alberi, case, radio addosso: 10 dB a 1 km, 20 a 10 km, finche' non ci sono misure
const SNR_SATURO_DB = 8;                               // oltre, la radio non distingue piu' un segnale forte da uno fortissimo
// larghezza di banda (kHz) delle impostazioni radio di Meshtastic: decide il rumore; il minimo decifrabile lo da' il gateway
const BANDA_PRESET_KHZ = {SHORT_TURBO: 500, SHORT_FAST: 250, SHORT_SLOW: 250, MEDIUM_FAST: 250, MEDIUM_SLOW: 250,
                          LONG_FAST: 250, LONG_MODERATE: 125, LONG_SLOW: 125, VERY_LONG_SLOW: 62.5, LONG_TURBO: 500};
const FREQUENZA_REGIONE_MHZ = {EU_868: 869.5, EU_433: 433.5, US: 915, ANZ: 921.5, UK_868: 869.5, CN: 490, JP: 924,
                               KR: 921.5, TW: 923, RU: 868.9, IN: 866, NZ_865: 866, TH: 922.5, UA_868: 868.5,
                               UA_433: 433.5, MY_433: 434, MY_919: 921, SG_923: 920, LORA_24: 2440};
// valori di fabbrica, usati (e detti) finche' non si scrivono quelli veri
const IMPIANTO_FABBRICA = {gateway: {altezza_m: 5, guadagno_dbi: 2, perdite_db: 1, potenza_dbm: 22},
                           ripetitore: {altezza_m: 3, guadagno_dbi: 2, perdite_db: 0, potenza_dbm: 22},
                           portatile: {altezza_m: 1.5, guadagno_dbi: 0, perdite_db: 0, potenza_dbm: 22}};

function rumoreDbm(bandaKhz) {
  return -174 + 10 * Math.log10(bandaKhz * 1000) + CIFRA_RUMORE_DB;
}

function spazioLiberoDb(distanzaM, frequenzaMhz) {
  return 32.45 + 20 * Math.log10(Math.max(distanzaM, 10) / 1000) + 20 * Math.log10(frequenzaMhz);
}

function perditaLamaDb(v) {
  // ITU-R P.526: attenuazione di un ostacolo a lama di coltello; sotto -0,78 l'ostacolo non disturba
  if (v <= -0.78) return 0;
  return 6.9 + 20 * Math.log10(Math.sqrt((v - 0.1) * (v - 0.1) + 1) + v - 0.1);
}

function ostacoloPeggiore(d, h, a, b, lambda) {
  // tra i punti a e b del profilo (distanze d, quote h gia' con le antenne agli estremi): l'ostacolo che sporge di piu'
  // sopra la linea tra a e b, contando la curvatura della Terra. Rende [indice, v] oppure null.
  const da = d[a], db = d[b], ha = h[a], hb = h[b], tratto = db - da;
  let meglio = -1, vMax = -Infinity;
  for (let i = a + 1; i < b; i++) {
    const d1 = d[i] - da, d2 = db - d[i];
    if (d1 <= 0 || d2 <= 0) continue;
    const sporge = h[i] + d1 * d2 / (2 * RAGGIO_EFFICACE_M) - (ha + (hb - ha) * d1 / tratto);
    const v = sporge * Math.sqrt(2 * tratto / (lambda * d1 * d2));
    if (v > vMax) { vMax = v; meglio = i; }
  }
  return meglio < 0 ? null : [meglio, vMax];
}

function diffrazioneDb(d, h, n, lambda) {
  // metodo di Deygout con tre ostacoli (ITU-R P.526, ostacoli in cascata): il principale, poi uno per parte
  const p = ostacoloPeggiore(d, h, 0, n - 1, lambda);
  if (!p || p[1] <= -0.78) return 0;
  const jp = perditaLamaDb(p[1]);
  const prima = ostacoloPeggiore(d, h, 0, p[0], lambda), dopo = ostacoloPeggiore(d, h, p[0], n - 1, lambda);
  const jt = prima ? perditaLamaDb(prima[1]) : 0, jr = dopo ? perditaLamaDb(dopo[1]) : 0;
  const t = 1 - Math.exp(-jp / 6), c = 10 + 0.04 * (d[n - 1] / 1000);
  return jp + t * (jt + jr + c);
}

function mercatoreX(lon, z) {
  return (lon + 180) / 360 * 256 * 2 ** z;
}

function mercatoreY(lat, z) {
  const s = Math.sin(lat * Math.PI / 180);
  return (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * 256 * 2 ** z;
}

function latitudineDaY(y, z) {
  return Math.atan(Math.sinh(Math.PI * (1 - 2 * y / (256 * 2 ** z)))) * 180 / Math.PI;
}

function parametriRadio(preset, regione, limiteSnr) {
  const banda = BANDA_PRESET_KHZ[preset] || 250;
  const frequenza = FREQUENZA_REGIONE_MHZ[regione] || 869.5;
  return {banda, frequenza, rumore: rumoreDbm(banda), limite: Number.isFinite(limiteSnr) ? limiteSnr : -17.5,
          lambda: 299.792458 / frequenza};
}

function riquadriDelRilievo(lat, lon, raggioKm) {
  // i riquadri del rilievo che servono per un cerchio: [x, y] al livello ZOOM_RILIEVO
  const px = mercatoreX(lon, ZOOM_RILIEVO), py = mercatoreY(lat, ZOOM_RILIEVO);
  const metriPerPunto = Math.cos(lat * Math.PI / 180) * 2 * Math.PI * RAGGIO_TERRA_M / (256 * 2 ** ZOOM_RILIEVO);
  const r = raggioKm * 1000 / metriPerPunto + 2, lista = [];
  for (let y = Math.floor((py - r) / 256); y <= Math.floor((py + r) / 256); y++) {
    for (let x = Math.floor((px - r) / 256); x <= Math.floor((px + r) / 256); x++) {
      if (y >= 0 && y < 2 ** ZOOM_RILIEVO) lista.push([((x % 2 ** ZOOM_RILIEVO) + 2 ** ZOOM_RILIEVO) % 2 ** ZOOM_RILIEVO, y]);
    }
  }
  return lista;
}

function* prevediCopertura(nodo, radio, portatile, quota, raggioKm, correzioneDb, salto) {
  // Generatore: lavora a pezzi (yield con la frazione fatta) per non bloccare la pagina; alla fine rende il risultato.
  // nodo e portatile: {altezza_m, guadagno_dbi, perdite_db, potenza_dbm}; nodo anche {lat, lon}.
  // quota(px, py): metri sul mare nel punto (px, py) del rilievo al livello ZOOM_RILIEVO, NaN se non si sa.
  // Per ogni quadretto nel raggio: margine (dB sopra il minimo decifrabile) nel verso peggiore dei due.
  // salto (dalla 1.18.0): 2 calcola un quadretto ogni due per lato (un quarto del lavoro), per confrontare in fretta
  // tanti siti possibili; i quadretti calcolati sono gli stessi di sempre, solo piu' radi.
  salto = salto || 1;
  const z = ZOOM_RILIEVO, scala = 2 ** (ZOOM_QUADRETTI - z), passo = 256 / scala;     // un quadretto in punti del rilievo
  const tx = mercatoreX(nodo.lon, z), ty = mercatoreY(nodo.lat, z);
  const metriPerPunto = Math.cos(nodo.lat * Math.PI / 180) * 2 * Math.PI * RAGGIO_TERRA_M / (256 * 2 ** z);
  const raggioPunti = raggioKm * 1000 / metriPerPunto;
  const suoloNodo = quota(tx, ty);
  if (!Number.isFinite(suoloNodo)) throw new Error("manca il rilievo nel punto dell'impianto");
  const hNodo = suoloNodo + nodo.altezza_m;
  const massimo = Math.ceil(raggioPunti) + 4;
  const d = new Float64Array(massimo), h = new Float64Array(massimo);
  // quello che non dipende dal percorso, nei due versi
  const fissoSu = portatile.potenza_dbm - portatile.perdite_db + portatile.guadagno_dbi + nodo.guadagno_dbi - nodo.perdite_db;
  const fissoGiu = nodo.potenza_dbm - nodo.perdite_db + nodo.guadagno_dbi + portatile.guadagno_dbi - portatile.perdite_db;
  const x0 = Math.floor((tx - raggioPunti) / passo), x1 = Math.floor((tx + raggioPunti) / passo);
  const y0 = Math.floor((ty - raggioPunti) / passo), y1 = Math.floor((ty + raggioPunti) / passo);
  const celle = new Map();
  let fatte = 0, ultimaPausa = 0;
  const righe = y1 - y0 + 1;
  for (let cy = Math.ceil(y0 / salto) * salto; cy <= y1; cy += salto) {
    for (let cx = Math.ceil(x0 / salto) * salto; cx <= x1; cx += salto) {
      const px = (cx + 0.5) * passo, py = (cy + 0.5) * passo;
      const distPunti = Math.hypot(px - tx, py - ty);
      if (distPunti > raggioPunti) continue;
      const suolo = quota(px, py);
      if (!Number.isFinite(suolo)) continue;
      const n = Math.max(2, Math.min(massimo, Math.ceil(distPunti) + 1));
      const distanza = Math.max(distPunti * metriPerPunto, 30);
      d[0] = 0; h[0] = hNodo;
      let valido = true;
      for (let i = 1; i < n - 1; i++) {
        const f = i / (n - 1);
        const q = quota(tx + (px - tx) * f, ty + (py - ty) * f);
        if (!Number.isFinite(q)) { valido = false; break; }
        d[i] = distanza * f; h[i] = q;
      }
      if (!valido) continue;
      d[n - 1] = distanza; h[n - 1] = suolo + portatile.altezza_m;
      const ostacoli = n > 2 ? diffrazioneDb(d, h, n, radio.lambda) : 0;
      const perdita = spazioLiberoDb(distanza, radio.frequenza) + ostacoli + correzioneDb;
      const su = fissoSu - perdita - radio.rumore - radio.limite;       // dalla radio al nodo: quello che si misura
      const giu = fissoGiu - perdita - radio.rumore - radio.limite;     // dal nodo alla radio
      celle.set(cx + "," + cy, {x: cx, y: cy, su, giu, margine: Math.min(su, giu), km: distanza / 1000, ostacoli});
      fatte++;
    }
    if (fatte - ultimaPausa >= 1500) {
      ultimaPausa = fatte;
      yield (cy - y0 + 1) / righe;
    }
  }
  return {celle, suolo: suoloNodo, correzione: correzioneDb, raggioKm, salto};
}

function profiloTra(a, b, quota) {
  // il terreno lungo la linea tra due punti (a e b: {lat, lon, altezza_m}), un punto ogni 53 m circa:
  // d = distanza dal primo (m), suolo = quota del terreno, h = come suolo ma con le antenne ai due capi.
  // Rende null se manca il rilievo.
  const z = ZOOM_RILIEVO;
  const ax = mercatoreX(a.lon, z), ay = mercatoreY(a.lat, z), bx = mercatoreX(b.lon, z), by = mercatoreY(b.lat, z);
  const metriPerPunto = Math.cos((a.lat + b.lat) / 2 * Math.PI / 180) * 2 * Math.PI * RAGGIO_TERRA_M / (256 * 2 ** z);
  const punti = Math.hypot(bx - ax, by - ay), n = Math.max(2, Math.ceil(punti) + 1);
  const distanza = Math.max(punti * metriPerPunto, 30);
  const d = new Float64Array(n), h = new Float64Array(n), suolo = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    const f = i / (n - 1), q = quota(ax + (bx - ax) * f, ay + (by - ay) * f);
    if (!Number.isFinite(q)) return null;
    d[i] = distanza * f;
    suolo[i] = q;
    h[i] = q + (i === 0 ? a.altezza_m : (i === n - 1 ? b.altezza_m : 0));
  }
  return {d, h, suolo, n, distanza};
}

function margineTraImpianti(a, b, radio, quota) {
  // tra due impianti (per esempio un ripetitore e un gateway, o un sito e una radio portatile): margine nel verso
  // peggiore, senza correzione (contano solo distanza e rilievo). Rende null se manca il rilievo.
  const p = profiloTra(a, b, quota);
  if (!p) return null;
  const ostacoli = p.n > 2 ? diffrazioneDb(p.d, p.h, p.n, radio.lambda) : 0;
  const perdita = spazioLiberoDb(p.distanza, radio.frequenza) + ostacoli;
  const guadagni = a.guadagno_dbi - a.perdite_db + b.guadagno_dbi - b.perdite_db;
  const margine = Math.min(a.potenza_dbm, b.potenza_dbm) + guadagni - perdita - radio.rumore - radio.limite;
  return {margine, km: p.distanza / 1000, ostacoli};
}

function profiloCollegamento(a, b, radio, quota) {
  // il profilo da disegnare (dalla 1.18.0): per ogni punto la distanza (km), il terreno come lo "vede" l'onda (quota
  // piu' la curvatura della Terra), la linea dritta tra le due antenne e fin dove deve restare libero perche' il
  // terreno non disturbi (il 60% della prima zona di Fresnel). "ostacolo" e' il punto che sporge di piu'.
  const p = profiloTra(a, b, quota);
  if (!p) return null;
  const D = p.distanza, ha = p.h[0], hb = p.h[p.n - 1], punti = [];
  let stretto = null;
  for (let i = 0; i < p.n; i++) {
    const d1 = p.d[i], d2 = D - d1;
    const terreno = p.suolo[i] + d1 * d2 / (2 * RAGGIO_EFFICACE_M), linea = ha + (hb - ha) * d1 / D;
    const libero = linea - 0.6 * Math.sqrt(radio.lambda * d1 * d2 / D);
    punti.push({km: d1 / 1000, terreno, quota: p.suolo[i], linea, libero});
    if (i > 0 && i < p.n - 1 && (!stretto || terreno - libero > stretto.sopra)) stretto = {km: d1 / 1000, sopra: terreno - libero, sporge: terreno - linea};
  }
  const peggiore = p.n > 2 ? ostacoloPeggiore(p.d, p.h, 0, p.n - 1, radio.lambda) : null;
  return {km: D / 1000, punti, antenne: [ha, hb], ostacoli: p.n > 2 ? diffrazioneDb(p.d, p.h, p.n, radio.lambda) : 0,
          ostacolo: peggiore && peggiore[1] > -0.78 ? {km: p.d[peggiore[0]] / 1000, sporge: punti[peggiore[0]].terreno - punti[peggiore[0]].linea} : null,
          stretto};
}

function cimeDelRilievo(quota, x0, y0, x1, y1, quante) {
  // Le cime dentro un rettangolo del rilievo (x0..x1, y0..y1 in punti al livello ZOOM_RILIEVO): i punti piu' alti di
  // tutto quello che hanno intorno per 600 m, ordinati per quanto spiccano sul terreno entro 2 km (un cocuzzolo in
  // mezzo alla pianura vale piu' di una gobba su un altopiano). Rende al massimo "quante" cime, lontane tra loro
  // almeno 600 m: [{px, py, lat, lon, quota, spicca}].
  const lat = latitudineDaY((y0 + y1) / 2, ZOOM_RILIEVO);
  const metriPerPunto = Math.cos(lat * Math.PI / 180) * 2 * Math.PI * RAGGIO_TERRA_M / (256 * 2 ** ZOOM_RILIEVO);
  const passo = Math.max(1, Math.ceil(Math.max(x1 - x0, y1 - y0) / 700));          // al massimo 700 punti per lato
  const vicino = Math.max(2, Math.round(600 / (metriPerPunto * passo))), largo = Math.max(vicino + 1, Math.round(2000 / (metriPerPunto * passo)));
  const W = Math.ceil((x1 - x0) / passo) + 2 * largo, H = Math.ceil((y1 - y0) / passo) + 2 * largo;
  if (W < 3 || H < 3 || W * H > 4e6) return [];
  const g = new Float32Array(W * H);
  for (let j = 0; j < H; j++) {
    for (let i = 0; i < W; i++) g[j * W + i] = quota(x0 + (i - largo + 0.5) * passo, y0 + (j - largo + 0.5) * passo);
  }
  // il massimo intorno a ogni punto: prima lungo le righe, poi lungo le colonne
  const perRiga = new Float32Array(W * H), massimo = new Float32Array(W * H);
  for (let j = 0; j < H; j++) {
    for (let i = 0; i < W; i++) {
      let m = -Infinity;
      for (let k = Math.max(0, i - vicino); k <= Math.min(W - 1, i + vicino); k++) { const v = g[j * W + k]; if (v > m) m = v; }
      perRiga[j * W + i] = m;
    }
  }
  for (let j = 0; j < H; j++) {
    for (let i = 0; i < W; i++) {
      let m = -Infinity;
      for (let k = Math.max(0, j - vicino); k <= Math.min(H - 1, j + vicino); k++) { const v = perRiga[k * W + i]; if (v > m) m = v; }
      massimo[j * W + i] = m;
    }
  }
  // la media del terreno intorno, con le somme progressive (un conto solo per punto)
  const somma = new Float64Array((W + 1) * (H + 1)), conta = new Float64Array((W + 1) * (H + 1));
  for (let j = 0; j < H; j++) {
    for (let i = 0; i < W; i++) {
      const v = g[j * W + i], c = Number.isFinite(v), k = (j + 1) * (W + 1) + i + 1;
      somma[k] = (c ? v : 0) + somma[k - 1] + somma[k - W - 1] - somma[k - W - 2];
      conta[k] = (c ? 1 : 0) + conta[k - 1] + conta[k - W - 1] - conta[k - W - 2];
    }
  }
  const dentro = (t, i0, j0, i1, j1) => t[(j1 + 1) * (W + 1) + i1 + 1] - t[j0 * (W + 1) + i1 + 1] - t[(j1 + 1) * (W + 1) + i0] + t[j0 * (W + 1) + i0];
  const trovate = [];
  for (let j = largo; j < H - largo; j++) {
    for (let i = largo; i < W - largo; i++) {
      const v = g[j * W + i];
      if (!Number.isFinite(v) || v < massimo[j * W + i]) continue;
      const n = dentro(conta, i - largo, j - largo, i + largo, j + largo);
      const spicca = v - dentro(somma, i - largo, j - largo, i + largo, j + largo) / Math.max(1, n);
      if (spicca >= 15) trovate.push({i, j, quota: v, spicca});
    }
  }
  trovate.sort((a, b) => b.spicca - a.spicca || b.quota - a.quota);
  const scelte = [];
  for (const t of trovate) {
    if (scelte.length >= quante) break;
    if (scelte.some(s => Math.hypot(s.i - t.i, s.j - t.j) <= vicino)) continue;         // la stessa cima, o un pianoro
    scelte.push(t);
  }
  return scelte.map(t => {
    // il punto piu' alto vero: dal punto trovato si sale, un punto del rilievo alla volta, finche' si puo' (la
    // ricerca guarda il rilievo a maglie che non cadono per forza sui suoi punti; un'antenna "in cima" sta in cima)
    let px = Math.floor(x0 + (t.i - largo + 0.5) * passo) + 0.5, py = Math.floor(y0 + (t.j - largo + 0.5) * passo) + 0.5, q = quota(px, py);
    for (let giro = 0; giro < 30; giro++) {
      let meglio = null;
      for (let dy = -1; dy <= 1; dy++) {
        for (let dx = -1; dx <= 1; dx++) {
          const v = quota(px + dx, py + dy);
          if (v > q && (!meglio || v > meglio[2])) meglio = [px + dx, py + dy, v];
        }
      }
      if (!meglio) break;
      [px, py, q] = meglio;
    }
    if (!Number.isFinite(q)) q = t.quota;
    return {px, py, lat: latitudineDaY(py, ZOOM_RILIEVO), lon: px / (256 * 2 ** ZOOM_RILIEVO) * 360 - 180, quota: q, spicca: t.spicca + q - t.quota};
  });
}

function probabilitaSotto(z) {
  // funzione di ripartizione della gaussiana (Abramowitz e Stegun 7.1.26: sbaglia meno di un milionesimo)
  const x = Math.abs(z) / Math.SQRT2, t = 1 / (1 + 0.3275911 * x);
  const erf = 1 - ((((1.061405429 * t - 1.453152027) * t + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-x * x);
  return z >= 0 ? 0.5 * (1 + erf) : 0.5 * (1 - erf);
}

function correzioneDb(c, km) {
  // la correzione di una taratura a una certa distanza: "a" dB a 1 km, piu' "b" volte 10 dB ogni dieci volte piu' lontano
  return c.a + 10 * c.b * Math.log10(Math.max(km, 0.1));
}

function taraPrevisione(previste, misurate, limiteSnr) {
  // Taratura: quanta attenuazione c'e' davvero in piu' rispetto al solo rilievo (alberi, case, radio in mano o nello
  // zaino), dal confronto con le misure di QUESTO gateway. Quello che il rilievo non vede cresce con la strada fatta:
  // la correzione e' "a" dB a 1 km piu' 10 * "b" dB ogni volta che la distanza si moltiplica per dieci (come nei
  // modelli classici di propagazione, dove b = 0 e' il vuoto e in mezzo a boschi e paesi b sta tra 1 e 2).
  // previste: Map "x,y" -> {su, km} calcolata con correzione 0. misurate: [{x, y, n, media, altre}] dei suoi quadretti
  // (n ricezioni dirette con rapporto segnale/rumore medio "media"; altre = volte arrivate solo tramite altri).
  // Ogni quadretto dice una di tre cose sullo scarto (previsto - vero):
  //   - sentita diretta: lo scarto e' previsto - misurato;
  //   - segnale saturo (la radio non misura oltre SNR_SATURO_DB): lo scarto e' AL MASSIMO previsto - saturo;
  //   - arrivata solo tramite altri (diretta non decifrata): lo scarto e' ALMENO previsto - minimo decifrabile.
  // Scartare i due ultimi casi sposterebbe la taratura (resterebbero solo i quadretti medi): si cercano invece a, b
  // e la dispersione che rendono piu' probabile tutto quello che si e' visto (massima verosimiglianza con dati
  // censurati), lasciando a ogni quadretto una piccola probabilita' di essere un caso a parte (una radio in cantina,
  // due messaggi sovrapposti), cosi' pochi quadretti strani non spostano il risultato. Se le misure stanno tutte alla
  // stessa distanza, b non si puo' ricavare da loro: lo tiene vicino a 1 una preferenza debole, che con misure a
  // distanze diverse non conta piu'.
  const esatti = [], tetti = [], fondi = [];
  for (const m of misurate) {
    const p = previste.get(m.x + "," + m.y);
    if (!p || !Number.isFinite(p.su)) continue;
    const snrPrevisto = p.su + limiteSnr, l = Math.log10(Math.max(p.km, 0.1));
    if (m.n > 0 && Number.isFinite(m.media)) (m.media >= SNR_SATURO_DB ? tetti : esatti).push([snrPrevisto - Math.min(m.media, SNR_SATURO_DB), l]);
    else if (m.altre > 0) fondi.push([snrPrevisto - limiteSnr, l]);
  }
  const conta = {quadretti: esatti.length + tetti.length + fondi.length, esatti: esatti.length, saturi: tetti.length,
                 nonSentiti: fondi.length};
  if (esatti.length < 5) return Object.assign(conta, {a: null, b: null, errore: null});
  const STRANI = 0.05, SPANNA = 1 / 60, B_ATTESO = 1, B_LARGO = 0.75;
  const verosimiglianza = (a, b, sigma) => {
    let somma = -0.5 * ((b - B_ATTESO) / B_LARGO) ** 2;
    for (const [s, l] of esatti) {
      const z = (s - a - 10 * b * l) / sigma;
      somma += Math.log((1 - STRANI) * Math.exp(-0.5 * z * z) / (sigma * 2.5066282746) + STRANI * SPANNA);
    }
    for (const [t, l] of tetti) somma += Math.log((1 - STRANI) * probabilitaSotto((t - a - 10 * b * l) / sigma) + STRANI / 2);
    for (const [f, l] of fondi) somma += Math.log((1 - STRANI) * (1 - probabilitaSotto((f - a - 10 * b * l) / sigma)) + STRANI / 2);
    return somma;
  };
  let meglio = [-Infinity, 12, 1, 6];
  const cerca = (a0, a1, passoA, b0, b1, passoB, s0, s1, passoS) => {
    for (let a = a0; a <= a1 + 1e-9; a += passoA) {
      for (let b = b0; b <= b1 + 1e-9; b += passoB) {
        for (let sigma = s0; sigma <= s1 + 1e-9; sigma += passoS) {
          const v = verosimiglianza(a, b, sigma);
          if (v > meglio[0]) meglio = [v, a, b, sigma];
        }
      }
    }
  };
  cerca(-12, 60, 3, 0, 3, 0.5, 2, 16, 2);
  cerca(meglio[1] - 3, meglio[1] + 3, 0.25, Math.max(0, meglio[2] - 0.5), Math.min(3, meglio[2] + 0.5), 0.125,
        Math.max(1.5, meglio[3] - 2), meglio[3] + 2, 0.5);
  return Object.assign(conta, {a: Math.max(-10, Math.min(60, meglio[1])), b: meglio[2], errore: meglio[3]});
}

// ---- Il rilievo: riquadri "terrarium" (PNG con la quota nei tre colori). Il PNG si legge qui, byte per byte, e non
// facendolo disegnare al browser: alcuni browser, per proteggere la riservatezza, alterano apposta i colori che una
// pagina rilegge da un disegno, e le quote uscirebbero sbagliate.
async function leggiPng(buffer) {
  const b = new Uint8Array(buffer), vista = new DataView(b.buffer, b.byteOffset, b.byteLength);
  const FIRMA = [137, 80, 78, 71, 13, 10, 26, 10];
  if (b.length < 33 || FIRMA.some((v, i) => b[i] !== v)) throw new Error("il riquadro del rilievo non è un PNG");
  let larghezza = 0, altezza = 0, canali = 0, pos = 8;
  const pezzi = [];
  while (pos + 12 <= b.length) {
    const lungo = vista.getUint32(pos), tipo = String.fromCharCode(b[pos + 4], b[pos + 5], b[pos + 6], b[pos + 7]);
    if (pos + 12 + lungo > b.length) throw new Error("riquadro del rilievo troncato");
    if (tipo === "IHDR") {
      larghezza = vista.getUint32(pos + 8);
      altezza = vista.getUint32(pos + 12);
      canali = {2: 3, 6: 4}[b[pos + 17]] || 0;
      if (b[pos + 16] !== 8 || !canali || b[pos + 20] !== 0 || larghezza > 1024 || altezza > 1024) {
        throw new Error("formato del rilievo non previsto");
      }
    } else if (tipo === "IDAT") {
      pezzi.push(b.subarray(pos + 8, pos + 8 + lungo));
    } else if (tipo === "IEND") {
      break;
    }
    pos += 12 + lungo;
  }
  if (!larghezza || !pezzi.length) throw new Error("riquadro del rilievo vuoto");
  const gonfio = new Uint8Array(await new Response(new Blob(pezzi).stream().pipeThrough(new DecompressionStream("deflate"))).arrayBuffer());
  const riga = larghezza * canali;
  if (gonfio.length < (riga + 1) * altezza) throw new Error("riquadro del rilievo troncato");
  const dati = new Uint8Array(riga * altezza);
  for (let y = 0; y < altezza; y++) {
    const filtro = gonfio[y * (riga + 1)], da = y * (riga + 1) + 1, a = y * riga;
    for (let i = 0; i < riga; i++) {
      const sinistra = i >= canali ? dati[a + i - canali] : 0, sopra = y ? dati[a - riga + i] : 0;
      const angolo = y && i >= canali ? dati[a - riga + i - canali] : 0;
      let aggiunta = 0;
      if (filtro === 1) aggiunta = sinistra;
      else if (filtro === 2) aggiunta = sopra;
      else if (filtro === 3) aggiunta = (sinistra + sopra) >> 1;
      else if (filtro === 4) {
        const p = sinistra + sopra - angolo, pa = Math.abs(p - sinistra), pb = Math.abs(p - sopra), pc = Math.abs(p - angolo);
        aggiunta = pa <= pb && pa <= pc ? sinistra : (pb <= pc ? sopra : angolo);
      } else if (filtro !== 0) throw new Error("riquadro del rilievo non valido");
      dati[a + i] = (gonfio[da + i] + aggiunta) & 255;
    }
  }
  return {larghezza, altezza, canali, dati};
}

async function quoteDelRiquadro(buffer) {
  // metri sul mare, 256 x 256: quota = rosso * 256 + verde + blu / 256 - 32768
  const png = await leggiPng(buffer);
  if (png.larghezza !== 256 || png.altezza !== 256) throw new Error("riquadro del rilievo di misura non prevista");
  const quote = new Float32Array(65536), c = png.canali, d = png.dati;
  for (let i = 0; i < 65536; i++) quote[i] = d[i * c] * 256 + d[i * c + 1] + d[i * c + 2] / 256 - 32768;
  return quote;
}

function unisciRilievo(riquadri, lista) {
  // un solo foglio con i riquadri che servono (riquadri: Map "x,y" -> quote), e la funzione che ne legge la quota in
  // un punto qualsiasi (px, py al livello ZOOM_RILIEVO) pesando i quattro punti vicini
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const [x, y] of lista) { x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y); }
  const colonne = (x1 - x0 + 1) * 256, righe = (y1 - y0 + 1) * 256;
  const foglio = new Float32Array(colonne * righe).fill(NaN);
  for (const [x, y] of lista) {
    const q = riquadri.get(x + "," + y);
    if (!q) continue;
    for (let r = 0; r < 256; r++) foglio.set(q.subarray(r * 256, r * 256 + 256), ((y - y0) * 256 + r) * colonne + (x - x0) * 256);
  }
  const ox = x0 * 256 + 0.5, oy = y0 * 256 + 0.5;       // il valore di un punto vale al suo centro
  return (px, py) => {
    const fx = Math.max(0, Math.min(colonne - 1.001, px - ox)), fy = Math.max(0, Math.min(righe - 1.001, py - oy));
    if (px - ox < -1 || py - oy < -1 || px - ox > colonne || py - oy > righe) return NaN;
    const ix = Math.floor(fx), iy = Math.floor(fy), tx = fx - ix, ty = fy - iy, i = iy * colonne + ix;
    return (foglio[i] * (1 - tx) + foglio[i + 1] * tx) * (1 - ty) + (foglio[i + colonne] * (1 - tx) + foglio[i + colonne + 1] * tx) * ty;
  };
}
