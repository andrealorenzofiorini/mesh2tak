// ---- Mappa della rete. Copertura (dalla 1.14.0): con che segnale i gateway hanno sentito le radio della squadra,
// quadretto per quadretto; dalla 1.15.0 anche i quadretti raggiunti tramite un ripetitore fisso, e sopra gateway e
// ripetitori con il loro stato. Ogni gateway raccoglie i suoi quadretti (zoom 17, circa 200 m); la pagina li unisce e
// li disegna su una mappa fatta qui, senza librerie esterne: da fuori arrivano solo le immagini della mappa di sfondo.
const CLASSI_COPERTURA = [   // margine (dB) sopra il minimo che la radio decifra; dal verde al rosso
  {chiave: "buono", nome: "Buono", breve: "buono", colore: "#0ca30c", da: 10, spiega: "i messaggi arrivano sicuri"},
  {chiave: "discreto", nome: "Discreto", breve: "discreto", colore: "#fab219", da: 5, spiega: "arrivano quasi sempre"},
  {chiave: "debole", nome: "Debole", breve: "debole", colore: "#e5622f", da: 2, spiega: "qualcuno si può perdere"},
  {chiave: "limite", nome: "Al limite", breve: "al limite", colore: "#a82533", da: -Infinity, spiega: "arrivano a fatica"}];
const VIA_RIPETITORE = {chiave: "ripetitore", nome: "Tramite un ripetitore", breve: "tramite ripetitore", colore: "#2a78d6",
                        spiega: "un ripetitore della rete l'ha portata al gateway"};
const RIPETUTA = {chiave: "ripetuta", nome: "Tramite altre radio", breve: "tramite altre radio", colore: "#6f7b80",
                  spiega: "il gateway non la sentiva diretta"};
const SFONDI = {
  topo: {nome: "Topografica", max: 17,
         url: (z, x, y) => "https://" + "abc"[(x + y) % 3] + ".tile.opentopomap.org/" + z + "/" + x + "/" + y + ".png",
         crediti: [["Dati © ", ""], ["OpenStreetMap", "https://www.openstreetmap.org/copyright"], [", SRTM. Mappa © ", ""],
                   ["OpenTopoMap", "https://opentopomap.org/about"], [" (CC-BY-SA)", ""]]},
  strade: {nome: "Stradale", max: 19, url: (z, x, y) => "https://tile.openstreetmap.org/" + z + "/" + x + "/" + y + ".png",
           crediti: [["© ", ""], ["OpenStreetMap", "https://www.openstreetmap.org/copyright"]]}};
const COLORI_PUNTI = {ok: "#0d4757", attenzione: "#995700", problema: "#b0261e"};
const COPERTURA_OGNI_S = 60;
const copertura = new Map();     // gateway -> {dati, errore, quando}
const sceltaCopertura = {mostra: "", periodo: "0", sfondo: "topo"};   // mostra: "", "g:nome" o "r:!id"
let ultimaCopertura = 0;
let mappaCopertura = null;
let selMostra = null, riassuntoCopertura = null, statoCopertura = null, bottoneAzzera = null;

function classeCopertura(margine) {
  if (margine === null || margine === undefined || !Number.isFinite(margine)) return RIPETUTA;
  return CLASSI_COPERTURA.find(c => margine >= c.da);
}

function giornoNumero(d) {
  return d.getFullYear() * 10000 + (d.getMonth() + 1) * 100 + d.getDate();
}

function testoGiorno(n) {
  return (n % 100) + "/" + (Math.floor(n / 100) % 100) + "/" + Math.floor(n / 10000);
}

function volte(n) {
  return n === 1 ? "1 volta" : n + " volte";
}

function mondoX(lon, z) {
  return (lon + 180) / 360 * 256 * 2 ** z;
}

function mondoY(lat, z) {
  const s = Math.sin(lat * Math.PI / 180);
  return (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * 256 * 2 ** z;
}

function nomiDeiRipetitori() {
  const nomi = new Map();
  for (const [, c] of copertura) for (const r of (c.dati && c.dati.ripetitori) || []) if (r && r.id) nomi.set(r.id, r.nome || r.id);
  for (const v of ripetitoriDellaRete()) nomi.set(v.id, nomeRipetitore(v));
  return nomi;
}

function quadrettiCopertura() {
  // i quadretti (zoom 17) di tutta la rete, di un gateway o di un ripetitore, nel periodo scelto. Il colore e' il
  // segnale migliore con cui un gateway ha sentito diretta una radio li'; se nessuno la sentiva diretta, il ripetitore
  // che l'ha portata a un gateway; altrimenti vuol dire solo che da li' si arriva alla rete passando da altre radio.
  let dal = 0;
  if (sceltaCopertura.periodo !== "0") {
    const d = new Date();
    d.setDate(d.getDate() - (Number(sceltaCopertura.periodo) - 1));
    dal = giornoNumero(d);
  }
  const scelta = sceltaCopertura.mostra;
  const gateway = scelta.startsWith("g:") ? scelta.slice(2) : "";
  const ripetitore = scelta.startsWith("r:") ? scelta.slice(2) : "";
  const celle = new Map();
  for (const [nome, c] of copertura) {
    if (!c.dati || (gateway && gateway !== nome)) continue;
    const limite = Number.isFinite(c.dati.limite_snr) ? c.dati.limite_snr : -17.5;
    for (const q of c.dati.celle || []) {
      if (!Array.isArray(q) || q.length < 8 || !(q[7] >= dal)) continue;
      const [x, y, n, media, migliore, , rip, giorno] = q;
      const via = q[8] && typeof q[8] === "object" && !Array.isArray(q[8]) ? q[8] : {};
      let voce;
      if (ripetitore) {
        if (!(via[ripetitore] > 0)) continue;
        voce = {ponte: nome, n: 0, media: null, migliore: null, rip: 0, via: {[ripetitore]: via[ripetitore]}, giorno, margine: null};
      } else {
        voce = {ponte: nome, n, media, migliore, rip, via, giorno, margine: n > 0 && media !== null ? media - limite : null};
      }
      const k = x + "," + y;
      let cella = celle.get(k);
      if (!cella) celle.set(k, cella = {x, y, voci: []});
      cella.voci.push(voce);
    }
  }
  const risultato = [];
  for (const c of celle.values()) {
    let margine = null, n = 0, rip = 0, tramite = 0, giorno = 0;
    for (const v of c.voci) {
      if (v.margine !== null && (margine === null || v.margine > margine)) margine = v.margine;
      n += v.n;
      rip += v.rip;
      for (const k of Object.keys(v.via)) tramite += Number(v.via[k]) || 0;
      giorno = Math.max(giorno, v.giorno);
    }
    risultato.push(Object.assign(c, {margine, n, rip, tramite, giorno,
                                     classe: margine !== null ? classeCopertura(margine) : (tramite ? VIA_RIPETITORE : RIPETUTA)}));
  }
  return risultato;
}

function raggruppaQuadretti(celle, livello) {
  // da lontano i quadretti si uniscono a gruppi (2x2, 4x4...): il colore e' la media dei margini, pesata sulle
  // ricezioni dirette. Il dettaglio di ogni quadretto si vede avvicinando la mappa.
  if (livello >= 17) return celle.map(c => Object.assign({lx: c.x, ly: c.y, quanti: 1}, c));
  const s = 17 - livello, gruppi = new Map();
  for (const c of celle) {
    const k = (c.x >> s) + "," + (c.y >> s);
    let g = gruppi.get(k);
    if (!g) gruppi.set(k, g = {lx: c.x >> s, ly: c.y >> s, quanti: 0, n: 0, rip: 0, tramite: 0, giorno: 0, somma: 0, pesi: 0});
    g.quanti++;
    g.n += c.n;
    g.rip += c.rip;
    g.tramite += c.tramite;
    g.giorno = Math.max(g.giorno, c.giorno);
    if (c.margine !== null) {
      const peso = Math.max(1, c.n);
      g.somma += c.margine * peso;
      g.pesi += peso;
    }
  }
  return [...gruppi.values()].map(g => Object.assign(g, {margine: g.pesi ? g.somma / g.pesi : null,
    classe: g.pesi ? classeCopertura(g.somma / g.pesi) : (g.tramite ? VIA_RIPETITORE : RIPETUTA)}));
}

function righeQuadretto(q) {
  const righe = [];
  if (q.quanti > 1) {
    righe.push(q.quanti + " quadretti insieme: avvicina la mappa per vederli uno per uno.");
    const parti = [];
    if (q.n) parti.push("sentite dirette " + volte(q.n));
    if (q.tramite) parti.push("tramite ripetitori " + volte(q.tramite));
    if (q.rip) parti.push("tramite altre radio " + volte(q.rip));
    righe.push(maiuscola(parti.join(", ")) + ".");
  } else {
    const nomi = nomiDeiRipetitori();
    const voci = [...q.voci].sort((a, b) => (b.margine === null ? -999 : b.margine) - (a.margine === null ? -999 : a.margine));
    for (const v of voci) {
      const parti = [];
      if (v.n) {
        parti.push(classeCopertura(v.margine).breve + ", segnale medio " + numIt(v.media) + " dB (migliore " +
                   numIt(v.migliore) + " dB), sentita diretta " + volte(v.n));
      }
      for (const [id, k] of Object.entries(v.via)) parti.push("tramite " + (nomi.get(id) || "il ripetitore " + id) + " " + volte(k));
      if (v.rip) parti.push("tramite altre radio " + volte(v.rip));
      righe.push("Gateway " + v.ponte + ": " + parti.join("; ") + ".");
    }
  }
  righe.push("Ultimo dato: " + testoGiorno(q.giorno) + ".");
  return righe;
}

class MappaCopertura {
  constructor() {
    const e = this.e = el("div", "mappa");
    e.id = "mappa-copertura";
    e.tabIndex = 0;
    e.setAttribute("role", "application");
    e.setAttribute("aria-label", "Mappa della rete. Trascina per spostarla; rotella, + e - per avvicinare e allontanare.");
    this.tessere = el("div", "tessere");
    this.tela = document.createElement("canvas");
    this.suggerimento = el("div", "suggerimento");
    this.suggerimento.hidden = true;
    const zoom = el("div", "zoom");
    for (const [segno, nome, d] of [["+", "Avvicina", 1], ["−", "Allontana", -1]]) {
      const b = el("button", null, segno);
      b.type = "button";
      b.setAttribute("aria-label", nome);
      b.addEventListener("click", () => this.zoom(d));
      zoom.append(b);
    }
    // mappa di sfondo: topografica o stradale, come nelle app di mappe
    this.sfondi = el("div", "sfondi");
    this.sfondi.setAttribute("role", "group");
    this.sfondi.setAttribute("aria-label", "Mappa di sfondo");
    for (const [chiave, s] of Object.entries(SFONDI)) {
      const b = el("button", null, s.nome);
      b.type = "button";
      b.dataset.sfondo = chiave;
      b.addEventListener("click", () => { sceltaCopertura.sfondo = chiave; this.sfondo(chiave); });
      this.sfondi.append(b);
    }
    this.crediti = el("div", "crediti");
    e.append(this.tessere, this.tela, zoom, this.sfondi, this.crediti, this.suggerimento);
    this.z = 6;
    this.cx = mondoX(12.5, 6);
    this.cy = mondoY(42.5, 6);
    this.w = this.h = 0;
    this.celle = [];
    this.punti = [];
    this.mostrati = [];
    this.dove = new Map();
    this.adattata = false;
    this.fisso = false;
    this.evidenza = null;           // il punto scelto dall'elenco, cerchiato per qualche secondo
    this.vive = new Map();          // immagini della mappa di sfondo in pagina
    this.ultimoZoom = 0;
    this.prev = new Map();          // dalla 1.17.0 i quadretti previsti (calcolati), sotto a quelli misurati
    this.prevLivelli = new Map();
    this.cerchi = [];               // fin dove e' stato fatto il calcolo, impianto per impianto
    this.alTocco = null;            // chi vuole il prossimo tocco sulla mappa (il punto di prova)
    this.altri = [];                // dalla 1.18.0 i segni delle proposte (numeri e lettere) e del punto da coprire
    this.sfondo("topo");
    this.eventi();
    new ResizeObserver(() => this.misura()).observe(e);
    e.puntoSchermo = (lat, lon) => {     // per le prove automatiche: dove cade un punto sullo schermo
      const r = e.getBoundingClientRect();
      return {x: r.left + mondoX(lon, this.z) - this.cx + this.w / 2, y: r.top + mondoY(lat, this.z) - this.cy + this.h / 2};
    };
  }

  sfondo(chiave) {
    this.chiaveSfondo = chiave;
    this.s = SFONDI[chiave];
    for (const b of this.sfondi.children) b.setAttribute("aria-pressed", String(b.dataset.sfondo === chiave));
    for (const img of this.vive.values()) img.remove();
    this.vive.clear();
    this.crediti.replaceChildren();
    for (const [testo, link] of this.s.crediti) {
      if (!link) { this.crediti.append(testo); continue; }
      const a = el("a", null, testo);
      a.href = link;
      a.target = "_blank";
      a.rel = "noopener";
      this.crediti.append(a);
    }
    // dalla 1.17.0, quando c'e' la copertura prevista: da dove viene il rilievo (lo chiedono i suoi autori)
    const rilievo = el("span", "rilievo", " Rilievo: ");
    const fonte = el("a", null, "Mapzen");
    fonte.href = "https://github.com/tilezen/joerd/blob/master/docs/attribution.md";
    fonte.target = "_blank";
    fonte.rel = "noopener";
    rilievo.append(fonte, ", Copernicus EU-DEM, USGS SRTM.");
    this.crediti.append(rilievo);
    if (this.z > this.s.max) this.zoom(this.s.max - this.z);
    else this.ridisegna();
  }

  dati(celle, punti) {
    this.celle = celle;
    this.punti = punti;
    this.e.dataset.quadretti = String(celle.length);
    this.e.dataset.punti = String(punti.length);
    if (!this.adattata) this.adatta();
    else this.ridisegna();
  }

  segni(lista) {
    this.altri = lista;
    this.e.dataset.segni = String(lista.length);
    this.ridisegna();
  }

  vista() {
    // la zona che la mappa mostra adesso, in gradi
    const n = 256 * 2 ** this.z, sx = this.cx - this.w / 2, sy = this.cy - this.h / 2;
    return {ovest: sx / n * 360 - 180, est: (sx + this.w) / n * 360 - 180, nord: latitudineDaY(sy, this.z), sud: latitudineDaY(sy + this.h, this.z)};
  }

  previste(celle, cerchi) {
    this.prev = celle;
    this.prevLivelli = new Map();
    this.cerchi = cerchi;
    this.e.dataset.previste = String(celle.size);
    this.e.classList.toggle("con-prevista", cerchi.length > 0);
    this.ridisegna();
  }

  previsteAl(livello) {
    // da lontano anche i quadretti previsti si uniscono: margine medio, e quanta parte del gruppo e' coperta
    let gruppi = this.prevLivelli.get(livello);
    if (!gruppi) {
      const s = 17 - livello;
      gruppi = new Map();
      for (const c of this.prev.values()) {
        const k = (c.x >> s) * 262144 + (c.y >> s);
        let g = gruppi.get(k);
        if (!g) gruppi.set(k, g = {quanti: 0, somma: 0, meglio: c});
        g.quanti++;
        g.somma += c.margine;
        if (c.margine > g.meglio.margine) g.meglio = c;
      }
      for (const g of gruppi.values()) {
        g.margine = g.somma / g.quanti;
        g.classe = classeCopertura(g.margine);
        g.parte = g.quanti / 4 ** s;
      }
      this.prevLivelli.set(livello, gruppi);
    }
    return gruppi;
  }

  inquadra(cerchi) {
    // tutta la zona calcolata in vista
    if (!this.w || !this.h || !cerchi.length) return;
    let minx = Infinity, maxx = -Infinity, miny = Infinity, maxy = -Infinity;
    for (const c of cerchi) {
      const r = c.km / (40075.017 * Math.cos(c.lat * Math.PI / 180)), x = mondoX(c.lon, 0) / 256, y = mondoY(c.lat, 0) / 256;
      minx = Math.min(minx, x - r); maxx = Math.max(maxx, x + r); miny = Math.min(miny, y - r); maxy = Math.max(maxy, y + r);
    }
    let z = 15;
    while (z > 3 && ((maxx - minx) * 256 * 2 ** z > this.w * 0.96 || (maxy - miny) * 256 * 2 ** z > this.h * 0.96)) z--;
    this.z = Math.min(z, this.s.max);
    this.cx = (minx + maxx) / 2 * 256 * 2 ** this.z;
    this.cy = (miny + maxy) / 2 * 256 * 2 ** this.z;
    this.adattata = true;
    this.nascondi();
    this.ridisegna();
  }

  misura() {
    const w = this.e.clientWidth, h = this.e.clientHeight;
    if (!w || !h || (w === this.w && h === this.h)) return;
    this.w = w;
    this.h = h;
    if (!this.adattata) this.adatta();
    else this.ridisegna();
  }

  adatta() {
    // tutti i quadretti, i gateway e i ripetitori in vista, non piu' vicino di zoom 15
    if (!this.w || !this.h) return;
    let minx = Infinity, maxx = -Infinity, miny = Infinity, maxy = -Infinity;
    const aggiungi = (x, y) => {
      minx = Math.min(minx, x); maxx = Math.max(maxx, x); miny = Math.min(miny, y); maxy = Math.max(maxy, y);
    };
    for (const c of this.celle) aggiungi((c.x + 0.5) / 2 ** 17, (c.y + 0.5) / 2 ** 17);
    for (const p of this.punti) aggiungi(mondoX(p.lon, 0) / 256, mondoY(p.lat, 0) / 256);
    if (minx === Infinity) {
      this.ridisegna();
      return;
    }
    let z = 15;
    while (z > 3 && ((maxx - minx) * 256 * 2 ** z > this.w * 0.8 || (maxy - miny) * 256 * 2 ** z > this.h * 0.8)) z--;
    this.z = Math.min(z, this.s.max);
    this.cx = (minx + maxx) / 2 * 256 * 2 ** this.z;
    this.cy = (miny + maxy) / 2 * 256 * 2 ** this.z;
    this.adattata = true;
    this.ridisegna();
  }

  centra(lat, lon, chiave) {
    // dall'elenco accanto alla mappa: il punto al centro, abbastanza vicino da vedere i quadretti intorno
    this.z = Math.min(this.s.max, Math.max(this.z, 13));
    this.cx = mondoX(lon, this.z);
    this.cy = mondoY(lat, this.z);
    this.adattata = true;
    this.nascondi();
    this.evidenza = chiave || null;
    clearTimeout(this.timerEvidenza);
    this.timerEvidenza = setTimeout(() => { this.evidenza = null; this.ridisegna(); }, 4000);
    this.ridisegna();
    const r = this.e.getBoundingClientRect();
    if (r.top < 0 || r.bottom > window.innerHeight) this.e.scrollIntoView({block: "center"});
  }

  zoom(d, sx, sy) {
    const nuovo = Math.max(3, Math.min(this.s.max, this.z + d));
    if (nuovo === this.z) return;
    if (sx === undefined) { sx = this.w / 2; sy = this.h / 2; }
    const f = 2 ** (nuovo - this.z);
    const wx = this.cx - this.w / 2 + sx, wy = this.cy - this.h / 2 + sy;
    this.cx = wx * f - sx + this.w / 2;
    this.cy = wy * f - sy + this.h / 2;
    this.z = nuovo;
    this.nascondi();
    this.ridisegna();
  }

  sposta(dx, dy) {
    this.cx += dx;
    this.cy += dy;
    this.nascondi();
    this.ridisegna();
  }

  ridisegna() {
    const lato = 256 * 2 ** this.z;
    this.cx = Math.max(0, Math.min(lato, this.cx));
    this.cy = Math.max(0, Math.min(lato, this.cy));
    this.e.dataset.zoom = String(this.z);
    if (!this.w || !this.h) return;
    this.disegnaSfondo();
    this.disegnaQuadretti();
  }

  disegnaSfondo() {
    const n = 2 ** this.z, sx = this.cx - this.w / 2, sy = this.cy - this.h / 2, servono = new Set();
    for (let ty = Math.max(0, Math.floor(sy / 256)); ty <= Math.min(n - 1, Math.floor((sy + this.h) / 256)); ty++) {
      for (let tx = Math.max(0, Math.floor(sx / 256)); tx <= Math.min(n - 1, Math.floor((sx + this.w) / 256)); tx++) {
        const k = this.z + "/" + tx + "/" + ty;
        servono.add(k);
        let img = this.vive.get(k);
        if (!img) {
          img = document.createElement("img");
          img.alt = "";
          img.draggable = false;
          img.decoding = "async";
          img.referrerPolicy = "strict-origin";      // OpenStreetMap vuole sapere da che sito arriva la richiesta
          img.addEventListener("error", () => { img.style.visibility = "hidden"; });
          img.src = this.s.url(this.z, tx, ty);
          this.vive.set(k, img);
          this.tessere.append(img);
        }
        img.style.transform = "translate(" + Math.round(tx * 256 - sx) + "px," + Math.round(ty * 256 - sy) + "px)";
      }
    }
    for (const [k, img] of this.vive) {
      if (!servono.has(k)) { img.remove(); this.vive.delete(k); }
    }
  }

  disegnaQuadretti() {
    const dpr = window.devicePixelRatio || 1, t = this.tela;
    if (t.width !== Math.round(this.w * dpr) || t.height !== Math.round(this.h * dpr)) {
      t.width = Math.round(this.w * dpr);
      t.height = Math.round(this.h * dpr);
    }
    const ctx = t.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, this.w, this.h);
    const sx = this.cx - this.w / 2, sy = this.cy - this.h / 2;
    this.livello = Math.max(9, Math.min(17, this.z + 4));      // i quadretti mostrati restano larghi almeno 16 pixel
    this.lato = 256 * 2 ** this.z / 2 ** this.livello;
    this.mostrati = raggruppaQuadretti(this.celle, this.livello);
    this.dove = new Map(this.mostrati.map(q => [q.lx + "," + q.ly, q]));
    if (!this.trama) {
      const r = document.createElement("canvas");
      r.width = r.height = 8;
      const c = r.getContext("2d");
      c.strokeStyle = "#4f5a5f";
      c.lineWidth = 1.5;
      c.beginPath();
      c.moveTo(-2, 10); c.lineTo(10, -2);
      c.moveTo(-2, 2); c.lineTo(2, -2);
      c.moveTo(6, 10); c.lineTo(10, 6);
      c.stroke();
      this.trama = ctx.createPattern(r, "repeat");
    }
    const lato = this.lato, spazio = lato >= 8 ? 1 : 0;     // un pixel tra un quadretto e l'altro
    let disegnati = 0, previsti = 0;
    if (this.prev.size) {
      // i previsti: sotto, piu' chiari e senza bordo ne' spazi (una superficie, non misure); solo quelli in vista
      // (a pezzi piu' fini dei quadretti misurati, larghi almeno 8 pixel: da lontano si vede la forma della zona)
      this.livelloPrev = Math.max(9, Math.min(17, this.z + 5));
      const latoP = this.latoPrev = 256 * 2 ** this.z / 2 ** this.livelloPrev;
      const gruppi = this.previsteAl(this.livelloPrev), n = 2 ** this.livelloPrev;
      const lx0 = Math.max(0, Math.floor(sx / latoP)), lx1 = Math.min(n - 1, Math.floor((sx + this.w) / latoP));
      const ly0 = Math.max(0, Math.floor(sy / latoP)), ly1 = Math.min(n - 1, Math.floor((sy + this.h) / latoP));
      for (let ly = ly0; ly <= ly1; ly++) {
        const y = Math.round(ly * latoP - sy), alto = Math.round((ly + 1) * latoP - sy) - y;
        for (let lx = lx0; lx <= lx1; lx++) {
          const g = gruppi.get(lx * 262144 + ly);
          if (!g) continue;
          const x = Math.round(lx * latoP - sx);
          ctx.globalAlpha = 0.14 + 0.28 * g.parte;
          ctx.fillStyle = g.classe.colore;
          ctx.fillRect(x, y, Math.round((lx + 1) * latoP - sx) - x, alto);
          previsti++;
        }
      }
      ctx.globalAlpha = 1;
    }
    ctx.setLineDash([6, 5]);        // fin dove arriva il calcolo: dentro al cerchio, senza colore vuol dire non coperto
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = "rgba(31, 45, 52, 0.6)";
    for (const c of this.cerchi) {
      const r = c.km / (40075.017 * Math.cos(c.lat * Math.PI / 180)) * 256 * 2 ** this.z;
      ctx.beginPath();
      ctx.arc(mondoX(c.lon, this.z) - sx, mondoY(c.lat, this.z) - sy, r, 0, 2 * Math.PI);
      ctx.stroke();
    }
    ctx.setLineDash([]);
    this.e.dataset.previstiDisegnati = String(previsti);
    for (const q of this.mostrati) {
      const x = q.lx * lato - sx, y = q.ly * lato - sy;
      if (x > this.w || y > this.h || x + lato < 0 || y + lato < 0) continue;
      disegnati++;
      if (q.classe === RIPETUTA) {
        ctx.fillStyle = "rgba(111, 123, 128, 0.22)";
        ctx.fillRect(x + spazio, y + spazio, lato - spazio, lato - spazio);
        ctx.fillStyle = this.trama;
        ctx.fillRect(x + spazio, y + spazio, lato - spazio, lato - spazio);
      } else {
        ctx.globalAlpha = 0.62;
        ctx.fillStyle = q.classe.colore;
        ctx.fillRect(x + spazio, y + spazio, lato - spazio, lato - spazio);
        ctx.globalAlpha = 1;
        if (lato >= 14) {
          ctx.strokeStyle = q.classe.colore;
          ctx.lineWidth = 1;
          ctx.strokeRect(x + spazio + 0.5, y + spazio + 0.5, lato - spazio - 1, lato - spazio - 1);
        }
      }
    }
    this.e.dataset.disegnati = String(disegnati);
    // gateway (quadrati) e ripetitori (cerchi): blu scuro se a posto, ambra o rosso se c'e' da guardare, vuoti se spenti
    ctx.font = "600 13px " + getComputedStyle(this.e).fontFamily;
    ctx.lineJoin = "round";
    this.posti = [];
    const ordinati = [...this.punti].sort((a, b) => (a.tipo === "gateway") - (b.tipo === "gateway"));
    for (const p of ordinati) {
      const x = mondoX(p.lon, this.z) - sx, y = mondoY(p.lat, this.z) - sy;
      if (x < -60 || y < -20 || x > this.w + 20 || y > this.h + 20) continue;
      this.posti.push({p, x, y});
      if (this.evidenza === p.chiave) {
        ctx.beginPath();
        ctx.arc(x, y, 17, 0, 2 * Math.PI);
        ctx.lineWidth = 3;
        ctx.strokeStyle = "#ffffff";
        ctx.stroke();
        ctx.lineWidth = 2;
        ctx.strokeStyle = "#0d4757";
        ctx.stroke();
      }
      const rombo = r => { ctx.moveTo(x, y - r); ctx.lineTo(x + r, y); ctx.lineTo(x, y + r); ctx.lineTo(x - r, y); ctx.closePath(); };
      ctx.beginPath();
      if (p.tipo === "gateway") ctx.rect(x - 7, y - 7, 14, 14);
      else if (p.tipo === "prova") rombo(9);
      else ctx.arc(x, y, 7, 0, 2 * Math.PI);
      ctx.fillStyle = p.spento ? "#ffffff" : COLORI_PUNTI[NOMI_LIVELLI[p.livello]];
      ctx.fill();
      ctx.lineWidth = 2.5;
      ctx.strokeStyle = p.spento ? "#55646b" : "#ffffff";
      ctx.stroke();
      if (!p.spento) {
        ctx.lineWidth = 1;
        ctx.strokeStyle = "rgba(31, 45, 52, 0.55)";
        ctx.beginPath();
        if (p.tipo === "gateway") ctx.rect(x - 8.75, y - 8.75, 17.5, 17.5);
        else if (p.tipo === "prova") rombo(11.25);
        else ctx.arc(x, y, 8.75, 0, 2 * Math.PI);
        ctx.stroke();
      }
      const nome = p.nome.length > 22 ? p.nome.slice(0, 21) + "…" : p.nome;
      ctx.lineWidth = 4;
      ctx.strokeStyle = "rgba(255, 255, 255, 0.9)";
      ctx.strokeText(nome, x + 13, y + 4.5);
      ctx.fillStyle = "#1f2d34";
      ctx.fillText(nome, x + 13, y + 4.5);
    }
    // le proposte (un tondo bianco con il numero o la lettera) e il punto da coprire (un mirino rosso)
    ctx.textAlign = "center";
    for (const p of this.altri) {
      const x = mondoX(p.lon, this.z) - sx, y = mondoY(p.lat, this.z) - sy;
      if (x < -20 || y < -20 || x > this.w + 20 || y > this.h + 20) continue;
      this.posti.push({p, x, y});
      if (p.tipo === "bersaglio") {
        for (const [largo, colore] of [[6, "#ffffff"], [2.5, "#b0261e"]]) {
          ctx.lineWidth = largo;
          ctx.strokeStyle = colore;
          ctx.beginPath();
          ctx.arc(x, y, 9, 0, 2 * Math.PI);
          ctx.moveTo(x - 15, y); ctx.lineTo(x - 4, y); ctx.moveTo(x + 4, y); ctx.lineTo(x + 15, y);
          ctx.moveTo(x, y - 15); ctx.lineTo(x, y - 4); ctx.moveTo(x, y + 4); ctx.lineTo(x, y + 15);
          ctx.stroke();
        }
      } else {
        ctx.beginPath();
        ctx.arc(x, y, 10.5, 0, 2 * Math.PI);
        ctx.fillStyle = "#ffffff";
        ctx.fill();
        ctx.lineWidth = 2;
        ctx.strokeStyle = "#1f2d34";
        ctx.stroke();
        ctx.fillStyle = "#1f2d34";
        ctx.fillText(p.sigla, x, y + 4.5);
      }
    }
    ctx.textAlign = "start";
  }

  nascondi() {
    this.suggerimento.hidden = true;
    this.fisso = false;
  }

  indica(ev, fisso) {
    const r = this.e.getBoundingClientRect(), px = ev.clientX - r.left, py = ev.clientY - r.top;
    const sx = this.cx - this.w / 2, sy = this.cy - this.h / 2;
    const s = this.suggerimento;
    // prima gateway e ripetitori (il bersaglio e' piu' largo del segno), poi i quadretti
    const vicino = (this.posti || []).filter(o => Math.hypot(o.x - px, o.y - py) <= 13)
      .sort((a, b) => Math.hypot(a.x - px, a.y - py) - Math.hypot(b.x - px, b.y - py))[0];
    if (vicino) {
      const [primo, ...altre] = vicino.p.righe;
      s.replaceChildren(el("b", null, primo), ...altre.map(t => el("span", null, t)));
    } else {
      const lx = this.lato ? Math.floor((px + sx) / this.lato) : 0, ly = this.lato ? Math.floor((py + sy) / this.lato) : 0;
      const q = this.lato ? this.dove.get(lx + "," + ly) : null;
      const previsto = this.latoPrev && this.prev.size ? this.previsteAl(this.livelloPrev)
        .get(Math.floor((px + sx) / this.latoPrev) * 262144 + Math.floor((py + sy) / this.latoPrev)) : null;
      if (!q && !previsto) {
        if (fisso || !this.fisso) this.nascondi();
        return;
      }
      if (q) {
        s.replaceChildren(el("b", null, q.classe.nome + ": " + q.classe.spiega),
                          ...righeQuadretto(q).concat(previsto ? righePrevista(previsto, true) : []).map(t => el("span", null, t)));
      } else {
        const [primo, ...altre] = righePrevista(previsto, false);
        s.replaceChildren(el("b", null, primo), ...altre.map(t => el("span", null, t)));
      }
    }
    s.hidden = false;
    const x = px + 16 + s.offsetWidth > this.w ? px - 16 - s.offsetWidth : px + 16;
    const y = py + 16 + s.offsetHeight > this.h ? Math.max(4, py - 16 - s.offsetHeight) : py + 16;
    s.style.transform = "translate(" + Math.max(4, Math.round(x)) + "px," + Math.round(y) + "px)";
    this.fisso = !!fisso;
  }

  eventi() {
    const e = this.e, puntatori = new Map();
    let trascina = null, pizzico = null, rotella = 0;
    e.addEventListener("pointerdown", ev => {
      if (ev.target.closest("button, a")) return;
      e.setPointerCapture(ev.pointerId);
      puntatori.set(ev.pointerId, {x: ev.clientX, y: ev.clientY});
      if (puntatori.size === 2) {
        const [a, b] = [...puntatori.values()];
        pizzico = {d: Math.hypot(a.x - b.x, a.y - b.y) || 1};
        trascina = null;
      } else if (puntatori.size === 1) {
        trascina = {x: ev.clientX, y: ev.clientY, cx: this.cx, cy: this.cy, mosso: false};
      }
    });
    e.addEventListener("pointermove", ev => {
      if (puntatori.has(ev.pointerId)) puntatori.set(ev.pointerId, {x: ev.clientX, y: ev.clientY});
      if (pizzico && puntatori.size === 2) {
        const [a, b] = [...puntatori.values()], d = Math.hypot(a.x - b.x, a.y - b.y);
        if (d / pizzico.d > 1.6 || d / pizzico.d < 0.62) {
          const r = e.getBoundingClientRect();
          this.zoom(d > pizzico.d ? 1 : -1, (a.x + b.x) / 2 - r.left, (a.y + b.y) / 2 - r.top);
          pizzico.d = d;
        }
        return;
      }
      if (trascina) {
        const dx = ev.clientX - trascina.x, dy = ev.clientY - trascina.y;
        if (!trascina.mosso && Math.abs(dx) + Math.abs(dy) < 5) return;
        trascina.mosso = true;
        this.nascondi();
        this.cx = trascina.cx - dx;
        this.cy = trascina.cy - dy;
        this.ridisegna();
        return;
      }
      if (ev.pointerType === "mouse") this.indica(ev, false);
    });
    const fine = ev => {
      const t = trascina;
      puntatori.delete(ev.pointerId);
      if (puntatori.size < 2) pizzico = null;
      if (!puntatori.size) trascina = null;
      if (t && !t.mosso && ev.type === "pointerup") {
        const r = e.getBoundingClientRect(), wx = this.cx - this.w / 2 + ev.clientX - r.left, wy = this.cy - this.h / 2 + ev.clientY - r.top;
        if (this.alTocco && this.alTocco(latitudineDaY(wy, this.z), wx / (256 * 2 ** this.z) * 360 - 180)) return;
        this.indica(ev, true);
      }
    };
    e.addEventListener("pointerup", fine);
    e.addEventListener("pointercancel", fine);
    e.addEventListener("pointerleave", ev => { if (ev.pointerType === "mouse" && !this.fisso) this.nascondi(); });
    e.addEventListener("wheel", ev => {
      ev.preventDefault();
      rotella += ev.deltaMode === 1 ? ev.deltaY * 40 : ev.deltaY;
      if (Math.abs(rotella) < 60 || Date.now() - this.ultimoZoom < 200) return;
      const r = e.getBoundingClientRect();
      this.zoom(rotella < 0 ? 1 : -1, ev.clientX - r.left, ev.clientY - r.top);
      this.ultimoZoom = Date.now();
      rotella = 0;
    }, {passive: false});
    e.addEventListener("dblclick", ev => {
      if (ev.target.closest("button, a")) return;
      const r = e.getBoundingClientRect();
      this.zoom(1, ev.clientX - r.left, ev.clientY - r.top);
    });
    e.addEventListener("keydown", ev => {
      if (ev.target !== e) return;
      const passi = {ArrowLeft: [-100, 0], ArrowRight: [100, 0], ArrowUp: [0, -100], ArrowDown: [0, 100]};
      if (ev.key === "+" || ev.key === "=") this.zoom(1);
      else if (ev.key === "-" || ev.key === "_") this.zoom(-1);
      else if (passi[ev.key]) this.sposta(...passi[ev.key]);
      else if (ev.key === "Escape") this.nascondi();
      else return;
      ev.preventDefault();
    });
  }
}

function costruisciCopertura() {
  const sez = vistaCopertura;
  const comandi = el("div", "comandi-copertura");
  const scelta = (etichetta, opzioni, chiave, cambia) => {
    const l = el("label", null, etichetta);
    const s = el("select");
    for (const [v, t] of opzioni) {
      const o = el("option", null, t);
      o.value = v;
      s.append(o);
    }
    s.value = sceltaCopertura[chiave];
    s.addEventListener("change", () => { sceltaCopertura[chiave] = s.value; cambia(); });
    l.append(s);
    comandi.append(l);
    return s;
  };
  selMostra = scelta("Mostra", [["", "Tutta la rete"]], "mostra", () => {
    mappaCopertura.adattata = false;
    daInquadrare = sceltaCopertura.prevista;
    disegnaCopertura();
  });
  scelta("Periodo", [["0", "Tutto"], ["30", "Ultimi 30 giorni"], ["7", "Ultimi 7 giorni"], ["1", "Oggi"]], "periodo",
         () => disegnaCopertura());
  mappaCopertura = new MappaCopertura();
  const prevista = costruisciPrevista(comandi, scelta);       // dalla 1.17.0
  const legenda = el("ul", "legenda");
  legenda.setAttribute("aria-label", "Legenda della mappa");
  for (const c of [...CLASSI_COPERTURA, VIA_RIPETITORE, RIPETUTA]) {
    const li = el("li");
    const segno = el("i", "segno-" + c.chiave);
    if (c !== RIPETUTA) segno.style.background = c.colore;
    li.append(segno, el("span", null, c.nome));
    legenda.append(li);
  }
  for (const [classe, nome] of [["punto-gateway", "Gateway"], ["punto-ripetitore", "Ripetitore"]]) {
    const li = el("li");
    li.append(el("i", classe), el("span", null, nome));
    legenda.append(li);
  }
  riassuntoCopertura = el("p", "riassunto");
  riassuntoCopertura.setAttribute("aria-live", "polite");
  bottoneAzzera = el("button", "bottone secondario piccolo", "Azzera");
  bottoneAzzera.type = "button";
  bottoneAzzera.addEventListener("click", azzeraCopertura);
  const azioni = el("div", "azioni-copertura");
  azioni.append(bottoneAzzera);
  const piede = el("div", "piede-mappa");
  piede.append(riassuntoCopertura, azioni);
  statoCopertura = el("p", "stato-copertura");
  statoCopertura.setAttribute("aria-live", "polite");
  const come = dettagli("come-mappa", "Come leggere la mappa",
    el("p", null, "Ogni quadretto è largo circa 200 metri. Il colore dice con che segnale un gateway ha sentito le radio " +
                  "della squadra che erano lì: è una misura, non una stima. Un quadretto vuoto vuol dire che lì non è " +
                  "passata nessuna radio, non che manca il segnale."),
    el("p", null, "Buono, discreto, debole e al limite dicono di quanto il segnale medio sta sopra il minimo che la radio " +
                  "riesce a decifrare (−17,5 dB con LongFast): più di 10 dB, da 5 a 10, da 2 a 5, meno di 2."),
    el("p", null, "Blu: la radio arrivava al gateway grazie a un ripetitore fisso della rete. Il segnale tra la radio e " +
                  "il ripetitore non si conosce, ma da lì la rete arriva. A righe grigie: arrivava passando da altre " +
                  "radio della squadra."),
    el("p", null, "Ogni gateway tiene solo quadretto, segnale e giorno: non quale radio né a che ora. Dopo aver " +
                  "spostato l'antenna di un gateway, sceglilo in «Mostra» e premi Azzera."),
    ...spiegaPrevista());
  come.classList.add("gruppo-mappa");
  sez.append(comandi, mappaCopertura.e, legenda, piede, statoCopertura, ...prevista, come);
}

async function caricaCopertura(forza) {
  const mancano = elenco.some(p => !copertura.has(p.nome));
  if (!elenco.length || (!forza && !mancano && Date.now() - ultimaCopertura < COPERTURA_OGNI_S * 1000)) return false;
  ultimaCopertura = Date.now();
  await Promise.all(elenco.map(async p => {
    const prima = copertura.get(p.nome);
    try {
      const r = await fetch("copertura/" + encodeURIComponent(p.nome) + ".json",
                            {cache: "no-store", redirect: "manual", credentials: "same-origin"});
      if (r.type === "opaqueredirect") {
        document.getElementById("sessione").hidden = false;
        throw new Error("sessione scaduta");
      }
      if (!r.ok) {
        await r.text().catch(() => "");    // una risposta lasciata a meta' tiene la richiesta aperta
        throw Object.assign(new Error("risposta " + r.status), {stato: r.status});
      }
      const d = await r.json();
      if (!d || !Array.isArray(d.celle)) throw new Error("dati non validi");
      copertura.set(p.nome, {dati: d, errore: "", quando: Date.now()});
    } catch (e) {
      copertura.set(p.nome, {dati: prima ? prima.dati : null, quando: Date.now(),
                             errore: e.stato === 404 ? "vecchio" : (e.stato === 502 || e.stato === 504 ? "giu" : e.message)});
    }
  }));
  for (const n of [...copertura.keys()]) if (!elenco.some(p => p.nome === n)) copertura.delete(n);
  return true;
}

function disegnaCopertura() {
  if (!mappaCopertura) return;
  // le scelte di "Mostra": tutta la rete, ogni gateway, ogni ripetitore; quella fatta resta
  const nomi = nomiDeiRipetitori();
  const scelte = [["", "Tutta la rete"], ...elenco.map(p => ["g:" + p.nome, "Gateway " + p.nome]),
                  ...[...nomi].sort((a, b) => a[1].localeCompare(b[1], "it")).map(([id, nome]) => ["r:" + id, nome]),
                  ...(puntoDiProva ? [["p:prova", "Punto di prova"]] : [])];
  const attuali = [...selMostra.options].map(o => o.value + "\t" + o.textContent);
  if (scelte.map(x => x.join("\t")).join("\n") !== attuali.join("\n")) {
    selMostra.replaceChildren(...scelte.map(([v, t]) => {
      const o = el("option", null, t);
      o.value = v;
      return o;
    }));
    if (!scelte.some(x => x[0] === sceltaCopertura.mostra)) sceltaCopertura.mostra = "";
  }
  if (selMostra.value !== sceltaCopertura.mostra) selMostra.value = sceltaCopertura.mostra;
  const gateway = sceltaCopertura.mostra.startsWith("g:") ? sceltaCopertura.mostra.slice(2) : "";
  bottoneAzzera.hidden = !gateway;
  bottoneAzzera.textContent = "Azzera la copertura di " + (gateway || "un gateway");
  const celle = quadrettiCopertura();
  const punti = puntiDellaRete();
  if (puntoDiProva) {
    punti.push({tipo: "prova", chiave: "p:prova", nome: "Punto di prova", lat: puntoDiProva.lat, lon: puntoDiProva.lon, livello: 0,
                spento: false, righe: ["Punto di prova: un impianto che non c'è ancora", "Sotto la mappa: quanta zona aggiungerebbe."]});
  }
  mappaCopertura.dati(celle, punti);
  aggiornaPrevista();
  const conDati = [...copertura.values()].filter(c => c.dati).length;
  if (!elenco.length) riassuntoCopertura.textContent = "";
  else if (!copertura.size) riassuntoCopertura.textContent = "Carico la copertura dei gateway...";
  else if (!celle.length) {
    riassuntoCopertura.textContent = conDati ? "Nessun quadretto" + (sceltaCopertura.periodo !== "0" ? " in questo periodo" : "") +
      ": la mappa si riempie quando le radio della squadra mandano la loro posizione." : "Nessun gateway dà la copertura.";
  } else {
    const conta = new Map();
    for (const c of celle) conta.set(c.classe, (conta.get(c.classe) || 0) + 1);
    riassuntoCopertura.textContent = celle.length + (celle.length === 1 ? " quadretto" : " quadretti") + ": " +
      [...CLASSI_COPERTURA, VIA_RIPETITORE, RIPETUTA].filter(k => conta.get(k)).map(k => k.breve + " " + conta.get(k)).join(", ") + ".";
  }
}

async function azzeraCopertura() {
  const nome = sceltaCopertura.mostra.startsWith("g:") ? sceltaCopertura.mostra.slice(2) : "";
  if (!nome || !window.confirm("Cancello la copertura raccolta dal gateway " + nome + "? Serve dopo aver spostato " +
                               "l'antenna o il gateway: la mappa di quel gateway ricomincia da zero.")) return;
  try {
    const r = await fetch("copertura/" + encodeURIComponent(nome) + ".json",
                          {method: "POST", cache: "no-store", redirect: "manual", credentials: "same-origin",
                           headers: {"Content-Type": "application/json", "X-Mesh2tak": "1"},
                           body: JSON.stringify({azzera: true})});
    if (r.type === "opaqueredirect") {
      document.getElementById("sessione").hidden = false;
      throw new Error("sessione scaduta: ricarica la pagina");
    }
    let d = {};
    try { d = await r.json(); } catch (e) { d = {}; }
    if (!r.ok) throw new Error(d.errore || ("il gateway risponde " + r.status));
    copertura.set(nome, {dati: d, errore: "", quando: Date.now()});
    statoCopertura.textContent = "Copertura del gateway " + nome + " azzerata: ricomincia da adesso.";
  } catch (e) {
    statoCopertura.textContent = "Non azzerata: " + frase(e.message);
  }
  disegnaCopertura();
}

