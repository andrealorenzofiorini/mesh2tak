// Il calcolo della copertura prevista (1.17.0), da solo: senza pagina e senza browser. Carica il pezzo della pagina
// con i conti e li confronta con valori calcolati a mano o noti. Stampa in JSON.
const fs = require('fs'), path = require('path'), vm = require('vm'), zlib = require('zlib');
const sorgente = fs.readFileSync(path.join(__dirname, '..', 'server', 'pagina', 'sorgenti', 'js', '08b-previsione-calcolo.js'), 'utf8');
const nomi = ['rumoreDbm', 'spazioLiberoDb', 'perditaLamaDb', 'diffrazioneDb', 'parametriRadio', 'riquadriDelRilievo',
              'prevediCopertura', 'taraPrevisione', 'correzioneDb', 'SENZA_TARATURA', 'margineTraImpianti', 'leggiPng', 'quoteDelRiquadro', 'unisciRilievo',
              'mercatoreX', 'mercatoreY', 'IMPIANTO_FABBRICA', 'ZOOM_RILIEVO', 'SNR_SATURO_DB'];
const m = vm.runInNewContext(sorgente + '\n;({' + nomi.join(', ') + '})',
                             {Blob, Response, DecompressionStream, Uint8Array, DataView, Float32Array, Float64Array, Math, Map});

function png(larghezza, altezza, canali, pixel, filtro) {
  // PNG a 8 bit, RGB o RGBA, con lo stesso filtro su ogni riga (0 nessuno, 1 sinistra, 2 sopra, 3 media, 4 Paeth)
  const riga = larghezza * canali, grezzo = Buffer.alloc((riga + 1) * altezza);
  for (let y = 0; y < altezza; y++) {
    grezzo[y * (riga + 1)] = filtro;
    for (let i = 0; i < riga; i++) {
      const v = pixel[y * riga + i], a = i >= canali ? pixel[y * riga + i - canali] : 0, b = y ? pixel[(y - 1) * riga + i] : 0;
      const c = y && i >= canali ? pixel[(y - 1) * riga + i - canali] : 0;
      let p = 0;
      if (filtro === 1) p = a;
      else if (filtro === 2) p = b;
      else if (filtro === 3) p = (a + b) >> 1;
      else if (filtro === 4) {
        const q = a + b - c, pa = Math.abs(q - a), pb = Math.abs(q - b), pc = Math.abs(q - c);
        p = pa <= pb && pa <= pc ? a : (pb <= pc ? b : c);
      }
      grezzo[y * (riga + 1) + 1 + i] = (v - p) & 255;
    }
  }
  const pezzo = (tipo, dati) => {
    const b = Buffer.alloc(12 + dati.length);
    b.writeUInt32BE(dati.length, 0);
    b.write(tipo, 4, 'ascii');
    dati.copy(b, 8);
    b.writeUInt32BE(zlib.crc32 ? zlib.crc32(b.subarray(4, 8 + dati.length)) : 0, 8 + dati.length);
    return b;
  };
  const testa = Buffer.alloc(13);
  testa.writeUInt32BE(larghezza, 0);
  testa.writeUInt32BE(altezza, 4);
  testa[8] = 8;
  testa[9] = canali === 4 ? 6 : 2;
  const compresso = zlib.deflateSync(grezzo);
  // i dati in due pezzi IDAT, come capita nei file veri
  return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), pezzo('IHDR', testa),
                        pezzo('IDAT', compresso.subarray(0, 40)), pezzo('IDAT', compresso.subarray(40)), pezzo('IEND', Buffer.alloc(0))]);
}

function terrarium(quota) {     // 256 x 256 punti con la quota data da quota(colonna, riga), in metri
  const pixel = Buffer.alloc(256 * 256 * 3);
  for (let y = 0; y < 256; y++) {
    for (let x = 0; x < 256; x++) {
      const v = Math.round((quota(x, y) + 32768) * 256);
      pixel[(y * 256 + x) * 3] = v >> 16;
      pixel[(y * 256 + x) * 3 + 1] = (v >> 8) & 255;
      pixel[(y * 256 + x) * 3 + 2] = v & 255;
    }
  }
  return pixel;
}

const tutto = g => { let r = g.next(), passi = 0; while (!r.done) { passi++; r = g.next(); } return [r.value, passi]; };
const arrotonda = (x, n) => Math.round(x * 10 ** (n || 2)) / 10 ** (n || 2);

(async () => {
  const r = {};
  // ---- conti di base
  r.rumore = arrotonda(m.rumoreDbm(250));                        // -174 + 10 log10(250000) + 6 = -114,02
  r.spazioLibero = arrotonda(m.spazioLiberoDb(10000, 869.5));    // 32,45 + 20 + 58,79 = 111,24
  r.lama = [-1, -0.78, 0, 1, 2.4].map(v => arrotonda(m.perditaLamaDb(v)));
  const radio = m.parametriRadio('LONG_FAST', 'EU_868', -17.5);
  r.radio = {banda: radio.banda, frequenza: radio.frequenza, limite: radio.limite, lambda: arrotonda(radio.lambda, 4)};
  r.radioIgnota = m.parametriRadio('NUOVO', 'ALTROVE', undefined).frequenza;
  // un ostacolo solo, a meta' di 10 km, che sporge 50 m sopra la linea tra le antenne: v = h * sqrt(2 d / (lambda d1 d2))
  const d = Float64Array.from([0, 5000, 10000]), h = Float64Array.from([100, 150 - 25e6 / (2 * 6371000 * 4 / 3), 100]);
  const v = 50 * Math.sqrt(2 * 10000 / (radio.lambda * 5000 * 5000));
  // (il metodo ITU per i profili del terreno aggiunge all'ostacolo principale T * (10 + 0,04 D km))
  const lama = m.perditaLamaDb(v);
  r.unOstacolo = [arrotonda(m.diffrazioneDb(d, h, 3, radio.lambda)), arrotonda(lama + (1 - Math.exp(-lama / 6)) * 10.4), arrotonda(lama)];
  // pianura: su 1 km niente ostacoli; molto lontano la Terra stessa fa da ostacolo
  const piano = n => [Float64Array.from({length: n}, (_, i) => i * 50), Float64Array.from({length: n}, (_, i) => (i === 0 || i === n - 1) ? 3 : 0)];
  r.pianura = [m.diffrazioneDb(...piano(21), 21, radio.lambda), arrotonda(m.diffrazioneDb(...piano(801), 801, radio.lambda)) > 20];
  // ---- il PNG del rilievo, con tutti i filtri, RGB e RGBA
  const quota = (x, y) => 200 + x * 2.5 + y * 0.25;
  const pixel = terrarium(quota);
  r.png = [];
  for (const filtro of [0, 1, 2, 3, 4]) {
    const q = await m.quoteDelRiquadro(png(256, 256, 3, pixel, filtro));
    r.png.push([arrotonda(q[0]), arrotonda(q[255]), arrotonda(q[65535]), arrotonda(q[256 * 100 + 37])]);
  }
  const rgba = Buffer.alloc(256 * 256 * 4, 255);
  for (let i = 0; i < 65536; i++) pixel.copy(rgba, i * 4, i * 3, i * 3 + 3);
  r.pngRgba = arrotonda((await m.quoteDelRiquadro(png(256, 256, 4, rgba, 4)))[256 * 100 + 37]);
  r.pngRotto = [];
  for (const brutto of [Buffer.from('non sono un png, sono una pagina di errore qualsiasi'), png(256, 256, 3, pixel, 0).subarray(0, 300),
                        png(16, 16, 3, Buffer.alloc(16 * 16 * 3), 0)]) {
    r.pngRotto.push(await m.quoteDelRiquadro(brutto).then(() => 'letto', e => e.message));
  }
  // ---- un impianto vero: gateway a 45,82 N 8,83 E. Il rilievo: pianura a 300 m e un crinale da ovest a est, alto
  // 500 m sopra la pianura, 3 km a nord del gateway.
  const lat = 45.82, lon = 8.83, z = m.ZOOM_RILIEVO;
  const tx = m.mercatoreX(lon, z), ty = m.mercatoreY(lat, z);
  const metriPerPunto = Math.cos(lat * Math.PI / 180) * 2 * Math.PI * 6371000 / (256 * 2 ** z);
  const crinale = ty - 3000 / metriPerPunto;
  const lista = m.riquadriDelRilievo(lat, lon, 6);
  r.riquadri = lista.length;
  const riquadri = new Map();
  for (const [x, y] of lista) {
    riquadri.set(x + ',' + y, await m.quoteDelRiquadro(png(256, 256, 3, terrarium((c, g) => {
      const py = y * 256 + g + 0.5;
      return 300 + 500 * Math.max(0, 1 - Math.abs(py - crinale) / 4);
    }), 4)));
  }
  const suolo = m.unisciRilievo(riquadri, lista);
  r.suolo = [arrotonda(suolo(tx, ty)), arrotonda(suolo(tx, crinale)), Number.isNaN(suolo(tx + 5000, ty))];
  const nodo = Object.assign({lat, lon}, m.IMPIANTO_FABBRICA.gateway);
  const portatile = m.IMPIANTO_FABBRICA.portatile;
  const [esito, passi] = tutto(m.prevediCopertura(nodo, radio, portatile, suolo, 6, 0));
  r.quadretti = esito.celle.size;
  r.passi = passi;
  r.suoloNodo = arrotonda(esito.suolo);
  const cella = (dx, dy) => {           // il quadretto a dx metri a est e dy metri a nord del gateway
    const px = tx + dx / metriPerPunto, py = ty - dy / metriPerPunto, passo = 256 / 2 ** (17 - z);
    return esito.celle.get(Math.floor(px / passo) + ',' + Math.floor(py / passo));
  };
  const sud = cella(0, -2000), nord = cella(0, 5000), est = cella(2000, 0);
  // a sud e a est pianura: con le antenne basse (5 m e 1,5 m) il terreno stesso toglie una decina di dB rispetto
  // allo spazio libero; con l'antenna del gateway a 60 m il percorso e' libero e il margine e' quello dello spazio libero
  const libero = km => 22 + 2 - 1 + 0 - m.spazioLiberoDb(km * 1000, 869.5) - m.rumoreDbm(250) + 17.5;
  r.sud = [arrotonda(sud.margine, 1), arrotonda(libero(sud.km), 1), arrotonda(sud.ostacoli, 1), arrotonda(sud.km, 1)];
  r.est = [arrotonda(est.margine, 1), arrotonda(libero(est.km), 1)];
  r.nord = [arrotonda(nord.ostacoli, 0) > 25, nord.margine < sud.margine - 25, arrotonda(nord.km, 1)];
  r.versi = [arrotonda(sud.su - sud.giu, 2)];
  // con l'antenna del gateway piu' in alto, dietro al crinale va meglio; con piu' potenza al gateway migliora solo
  // il verso dal gateway alla radio, e il margine (il verso peggiore) resta com'era
  const alto = tutto(m.prevediCopertura(Object.assign({}, nodo, {altezza_m: 60}), radio, portatile, suolo, 6, 0))[0];
  const forte = tutto(m.prevediCopertura(Object.assign({}, nodo, {potenza_dbm: 27}), radio, portatile, suolo, 6, 0))[0];
  const stessa = (e, c) => e.celle.get(c.x + ',' + c.y);
  r.antennaAlta = [stessa(alto, nord).margine > nord.margine, stessa(alto, sud).ostacoli,
                   arrotonda(stessa(alto, sud).margine, 1), arrotonda(libero(stessa(alto, sud).km), 1)];
  r.piuPotenza = [arrotonda(stessa(forte, sud).giu - sud.giu, 1), arrotonda(stessa(forte, sud).margine - sud.margine, 1)];
  // la correzione sposta tutto
  const corretta = tutto(m.prevediCopertura(nodo, radio, portatile, suolo, 6, 14))[0];
  r.correzione = arrotonda(sud.margine - stessa(corretta, sud).margine, 1);
  // senza rilievo sotto l'impianto si dice, non si inventa
  try { tutto(m.prevediCopertura(Object.assign({}, nodo, {lat: 47}), radio, portatile, suolo, 6, 0)); r.senzaRilievo = 'calcolata'; }
  catch (e) { r.senzaRilievo = e.message; }
  // tra due impianti fissi: ripetitore oltre il crinale, e uno in pianura
  const oltre = m.margineTraImpianti(nodo, Object.assign({lat: lat + 0.045, lon}, m.IMPIANTO_FABBRICA.ripetitore), radio, suolo);
  const vicino = m.margineTraImpianti(nodo, Object.assign({lat: lat - 0.02, lon}, m.IMPIANTO_FABBRICA.ripetitore), radio, suolo);
  r.traImpianti = [oltre.ostacoli > 20, arrotonda(vicino.ostacoli, 1), arrotonda(vicino.km, 1), vicino.margine > oltre.margine + 20];
  // ---- la taratura: lo scarto vero e' 14 dB a 1 km, cresce di 15 dB ogni dieci volte la distanza, con 5 dB di
  // dispersione. Le misure sature e quelle arrivate solo tramite altri non si buttano: buttandole si sbaglierebbe.
  let seme = 12345;
  const caso = () => { seme = (seme * 1103515245 + 12345) % 2147483648; return seme / 2147483648; };
  const gauss = () => Math.sqrt(-2 * Math.log(1 - caso())) * Math.cos(2 * Math.PI * caso());
  const misura = (c, a, b) => {
    const vero = c.su - 17.5 - (a + 10 * b * Math.log10(Math.max(c.km, 0.1)) + 5 * gauss());      // segnale/rumore vero li'
    return vero < -17.5 ? {x: c.x, y: c.y, n: 0, media: null, altre: 1} : {x: c.x, y: c.y, n: 3, media: Math.min(vero, 11), altre: 0};
  };
  const tutte = [...esito.celle.values()];
  const misurate = tutte.filter(() => caso() < 0.12).map(c => misura(c, 14, 1.5));
  const t = m.taraPrevisione(esito.celle, misurate, -17.5);
  r.taratura = {a: t.a, b: t.b, errore: t.errore, quadretti: t.quadretti, esatti: t.esatti, saturi: t.saturi, nonSentiti: t.nonSentiti};
  r.correzione10km = arrotonda(m.correzioneDb(t, 10), 1);
  // la stessa taratura fatta solo sui quadretti "sentiti e non saturi" sbaglierebbe: lo dice il confronto
  const ingenua = m.taraPrevisione(esito.celle, misurate.filter(q => q.n > 0 && q.media < m.SNR_SATURO_DB), -17.5);
  r.ingenua = [ingenua.a, ingenua.b];
  // qualche quadretto strano (radio in cantina accanto al gateway) non sposta il risultato
  const strane = misurate.concat(tutte.filter(c => c.km < 0.6).slice(0, 6).map(c => ({x: c.x, y: c.y, n: 0, media: null, altre: 2})));
  const conStrani = m.taraPrevisione(esito.celle, strane, -17.5);
  r.taraturaConStrani = [conStrani.a, conStrani.b];
  // misure tutte alla stessa distanza (tra 2 e 2,4 km): b non si ricava, resta vicino a 1; la correzione li' e' giusta
  const fascia = tutte.filter(c => c.km > 2 && c.km < 2.4 && c.ostacoli < 20).map(c => misura(c, 14, 1.5));
  const stretta = m.taraPrevisione(esito.celle, fascia, -17.5);
  r.fascia = [fascia.length, stretta.b, arrotonda(m.correzioneDb(stretta, 2.2), 1), arrotonda(14 + 15 * Math.log10(2.2), 1)];
  // poche misure: nessuna taratura
  const poche = m.taraPrevisione(esito.celle, misurate.filter(q => q.n > 0 && q.media < 8).slice(0, 4), -17.5);
  r.pochi = [poche.a, poche.quadretti];
  console.log(JSON.stringify(r));
})().catch(e => { console.log(JSON.stringify({errore: String(e.stack || e).slice(0, 800)})); process.exit(1); });
