// Pagina della rete (1.7.0) come un amministratore entrato in Authentik: fatti del Raspberry nella scheda del
// ponte, sezione "Avvisi per email" e il pulsante dell'email di prova.
const { chromium } = require('playwright');
const { sfondoFinto, apri } = require('./pagina_comune');
(async () => {
  const [url] = process.argv.slice(2);
  const b = await chromium.launch({args: ['--host-resolver-rules=MAP *.prova.lan 127.0.0.1', '--no-proxy-server']});
  const ctx = await b.newContext({ignoreHTTPSErrors: true, viewport: {width: 1280, height: 900}, deviceScaleFactor: 1});
  await ctx.addCookies([{name: 'ak_finto', value: 'andrea', domain: 'meshtastic.prova.lan', path: '/'}]);
  await sfondoFinto(ctx);
  const p = await ctx.newPage();
  const errori = [], finestre = [];
  p.on('console', m => { if (m.type() === 'error') errori.push(m.text()); });
  p.on('pageerror', e => errori.push(String(e)));
  p.on('dialog', d => { finestre.push(d.message()); d.dismiss(); });
  await p.goto(url + '#gateway', {waitUntil: 'networkidle'});
  await p.waitForSelector('section.ponte .fatti', {timeout: 15000});
  await apri(p, 'avvisi');
  await p.waitForSelector('#avvisi:not([hidden]) .stato-avvisi', {timeout: 15000});
  await p.waitForTimeout(300);
  const ponte = await p.innerText('section.ponte .fatti');
  const avvisi = await p.innerText('#avvisi');
  await p.click('#avvisi button');
  await p.waitForFunction(() => /Email di prova|Non partita|Non riuscito/.test(document.querySelector('#avvisi .esito').textContent),
                          null, {timeout: 30000});
  const prova = await p.innerText('#avvisi .esito');
  console.log(JSON.stringify({ponte, avvisi, prova, errori, finestre}));
  await b.close();
})().catch(e => { console.log(JSON.stringify({errore: String(e)})); process.exit(1); });
