// Apre la pagina della rete come un amministratore gia' entrato in Authentik e riporta testo, errori e
// finestre (alert) aperte: con dati presi dalle radio non deve eseguire nulla.
const { chromium } = require('playwright');
const { sfondoFinto, IMMAGINI, inSospeso } = require('./pagina_comune');
let sospese = () => [];
(async () => {
  const [url, foto] = process.argv.slice(2);
  const b = await chromium.launch({args: ['--host-resolver-rules=MAP *.prova.lan 127.0.0.1', '--no-proxy-server']});
  const ctx = await b.newContext({ignoreHTTPSErrors: true, viewport: {width: 1280, height: 900}, deviceScaleFactor: 1});
  await ctx.addCookies([{name: 'ak_finto', value: 'andrea', domain: 'meshtastic.prova.lan', path: '/'}]);
  await sfondoFinto(ctx);
  const p = await ctx.newPage();
  sospese = inSospeso(p);
  const errori = [], finestre = [];
  p.on('console', m => { if (m.type() === 'error') errori.push(m.text()); });
  p.on('pageerror', e => errori.push(String(e)));
  p.on('dialog', d => { finestre.push(d.message()); d.dismiss(); });
  await p.goto(url, {waitUntil: 'networkidle'});
  await p.waitForSelector('section.ponte', {timeout: 15000});
  await p.waitForTimeout(500);
  if (foto) await p.screenshot({path: foto, fullPage: true});
  await p.click('section.ponte details.altri summary');      // canali e il resto, in "Altri dettagli"
  const testo = await p.innerText('main');
  const stato = await p.getAttribute('section.ponte', 'data-stato');
  console.log(JSON.stringify({testo, stato, errori, finestre, immagini: await p.evaluate(IMMAGINI)}));
  await b.close();
})().catch(e => { console.log(JSON.stringify({errore: String(e), sospese: sospese()})); process.exit(1); });
