// Copertura prevista nel browser (1.17.0): la pagina prende il rilievo dal nostro server, calcola dove arrivano il
// gateway casa e il ripetitore con i dati dei loro impianti, mostra i quadretti previsti sotto a quelli misurati, fa
// scrivere i dati di un impianto, prova un punto che non c'e' ancora e mette la prevista nel file per ATAK. Il
// rilievo di prova e' una pianura (lo da' la prova stessa, al posto del sito vero). Stampa in JSON.
const { chromium } = require('playwright');
const { sfondoFinto } = require('./pagina_comune');
const fs = require('fs');
let p = null;
(async () => {
  const [url, foto] = process.argv.slice(2);
  const b = await chromium.launch({args: ['--host-resolver-rules=MAP *.prova.lan 127.0.0.1', '--no-proxy-server']});
  const ctx = await b.newContext({ignoreHTTPSErrors: true, viewport: {width: 1280, height: 1000}, deviceScaleFactor: 1,
                                  acceptDownloads: true});
  await ctx.addCookies([{name: 'ak_finto', value: 'andrea', domain: 'meshtastic.prova.lan', path: '/'}]);
  await sfondoFinto(ctx, null);
  p = await ctx.newPage();
  const errori = [], rilievo = [];
  p.on('console', m => { if (m.type() === 'error') errori.push(m.text()); });
  p.on('pageerror', e => errori.push(String(e)));
  p.on('response', x => { if (x.url().includes('/api/terreno/')) rilievo.push([x.status(), x.headers()['cache-control'] || '', x.url()]); });
  await p.goto(url, {waitUntil: 'networkidle'});
  const r = {errori};
  const mappa = '#mappa-copertura', spunta = '.comandi-copertura label.spunta input';
  const mostra = '.comandi-copertura label:has-text("Mostra") select';
  await p.waitForSelector(mappa + '[data-quadretti="6"][data-punti="2"]', {timeout: 30000});
  const com = async () => ({previste: await p.getAttribute(mappa, 'data-previste'),
                            legenda: await p.$$eval('.legenda li', x => x.some(e => /Prevista/.test(e.textContent))),
                            elenco: await p.$$eval('.elenco-previste li', x => x.length)});
  r.creditiPrima = await p.innerText(mappa + ' .crediti');
  r.prima = Object.assign(await com(), {rilievo: rilievo.length});
  const pronta = () => p.waitForFunction(() => /Copertura prevista entro|non prevede/.test(document.querySelector('.stato-prevista').textContent),
                                         null, {timeout: 120000});
  const elenco = () => p.$$eval('.elenco-previste li', x => x.map(e => [e.dataset.impianto, e.innerText.replace(/\s+/g, ' ').trim()]));
  await p.check(spunta);
  await pronta();
  r.stato = await p.innerText('.stato-prevista');
  r.previste = Number(await p.getAttribute(mappa, 'data-previste'));
  r.disegnati = Number(await p.getAttribute(mappa, 'data-previsti-disegnati'));
  r.legenda = (await com()).legenda;
  r.crediti = await p.innerText(mappa + ' .crediti');
  r.rilievo = rilievo.slice();
  r.elenco = await elenco();
  // passando sulla mappa, da vicino: 600 m a sud del gateway nessuna radio e' passata; sul gateway c'e' una misura
  await p.selectOption(mostra, 'g:casa');
  await pronta();
  await p.$eval(mappa, e => e.scrollIntoView({block: 'center'}));
  for (let i = 0; i < 12 && Number(await p.getAttribute(mappa, 'data-zoom')) < 14; i++) await p.click(mappa + ' button[aria-label="Avvicina"]');
  const suggerimento = async (lat, lon, attesa) => {
    await p.evaluate(([a, o]) => mappaCopertura.centra(a, o), [lat, lon]);
    await p.$eval(mappa, e => e.scrollIntoView({block: 'center'}));
    const q = await p.evaluate(([a, o]) => document.getElementById('mappa-copertura').puntoSchermo(a, o), [lat, lon]);
    await p.mouse.move(q.x + 3, q.y + 3);
    await p.mouse.move(q.x, q.y);
    await p.waitForFunction(t => { const s = document.querySelector('#mappa-copertura .suggerimento');
                                   return !s.hidden && new RegExp(t).test(s.innerText); }, attesa);
    return p.innerText(mappa + ' .suggerimento');
  };
  r.previsto = await suggerimento(45.8152, 8.8251, '^Previsto');
  r.misurato = await suggerimento(45.8206, 8.8281, '^Buono');
  if (foto) await p.screenshot({path: foto, fullPage: true});
  await p.selectOption(mostra, '');
  await pronta();
  // i dati dell'impianto del gateway
  await p.click('.elenco-previste li[data-impianto="g:casa"] button:has-text("Dati dell\'impianto")');
  await p.waitForSelector('#impianto[open]');
  r.impiantoTitolo = await p.innerText('#impianto-titolo');
  r.segnaposto = [await p.getAttribute('#impianto-altezza_m', 'placeholder'), await p.getAttribute('#impianto-potenza_dbm', 'placeholder')];
  await p.fill('#impianto-altezza_m', '500');
  await p.click('#impianto-salva');
  r.impiantoSbagliato = await p.innerText('#impianto-esito');
  await p.fill('#impianto-altezza_m', '25');
  await p.fill('#impianto-guadagno_dbi', '6,5');
  await p.click('#impianto-salva');
  await p.waitForSelector('#impianto', {state: 'hidden'});
  await p.waitForFunction(() => /25 m da terra/.test(document.querySelector('.elenco-previste li[data-impianto="g:casa"]').textContent) &&
                                /Da solo copre/.test(document.querySelector('.elenco-previste li[data-impianto="g:casa"]').textContent),
                          null, {timeout: 120000});
  await pronta();
  r.casaDopo = (await elenco())[0][1];
  r.previsteDopo = Number(await p.getAttribute(mappa, 'data-previste'));
  r.schedaGateway = await p.$eval('#gateway-casa [data-prevista]', e => e.textContent);
  // solo il ripetitore
  await p.selectOption(mostra, 'r:!3333cccc');
  await pronta();
  r.soloRipetitore = (await elenco()).map(x => x[0]);
  r.previsteRipetitore = Number(await p.getAttribute(mappa, 'data-previste'));
  // il punto di prova: tocco sulla mappa, 3 km a est del gateway
  await p.selectOption(mostra, '');
  await pronta();
  await p.click('button:has-text("Prova un punto")');
  r.provaInvito = await p.innerText('.stato-prevista');
  await p.$eval(mappa, e => e.scrollIntoView({block: 'center'}));
  const q = await p.evaluate(() => document.getElementById('mappa-copertura').puntoSchermo(45.8206, 8.8651));
  await p.mouse.click(q.x, q.y);
  await p.waitForFunction(() => { const li = document.querySelector('.elenco-previste li[data-impianto="p:prova"]');
                                  return li && /aggiunge/.test(li.textContent); }, null, {timeout: 120000});
  r.prova = (await elenco())[0][1];
  r.provaMostra = await p.$eval(mostra, s => [s.value, s.options[s.selectedIndex].textContent]);
  await p.$eval(mappa, e => e.scrollIntoView({block: 'center'}));
  const dove = await p.evaluate(() => document.getElementById('mappa-copertura').puntoSchermo(puntoDiProva.lat, puntoDiProva.lon));
  await p.mouse.move(dove.x + 2, dove.y + 2);
  await p.mouse.move(dove.x, dove.y);
  await p.waitForFunction(() => /^Punto di prova/.test(document.querySelector('#mappa-copertura .suggerimento').innerText));
  r.provaSegno = await p.innerText(mappa + ' .suggerimento');
  await p.click('.elenco-previste button:has-text("Togli il punto")');
  r.provaTolta = [await p.$$eval(mostra + ' option', x => x.some(o => o.value === 'p:prova')), await p.inputValue(mostra)];
  // il file per ATAK, con la prevista entro 10 km
  await p.selectOption('.comandi-copertura label:has-text("Fino a") select', '10');
  await p.waitForFunction(() => /entro 10 km/.test(document.querySelector('.stato-prevista').textContent), null, {timeout: 120000});
  r.previsteDieci = Number(await p.getAttribute(mappa, 'data-previste'));
  const [scaricato] = await Promise.all([p.waitForEvent('download'), p.click('button:has-text("Scarica per ATAK")')]);
  r.kmlNome = scaricato.suggestedFilename();
  r.kml = fs.readFileSync(await scaricato.path(), 'utf8');
  r.statoKml = await p.innerText('.stato-copertura:not(.stato-prevista)');
  // spenta, e riaccesa dalla scheda del ripetitore
  await p.uncheck(spunta);
  r.dopo = await com();
  await p.click('#ripetitori section.ripetitore button:has-text("Calcola sulla mappa")');
  await pronta();
  r.dallaScheda = [await p.inputValue(mostra), await p.isChecked(spunta)];
  await p.uncheck(spunta);
  r.dopo = await com();
  console.log(JSON.stringify(r));
  await b.close();
})().catch(async e => {
  let stato = null;
  try { if (p) stato = await p.innerText('#mappa', {timeout: 2000}); } catch (x) { /* pagina chiusa */ }
  console.log(JSON.stringify({errore: String(e).slice(0, 600), stato: stato && stato.slice(0, 1200)}));
  process.exit(1);
});
