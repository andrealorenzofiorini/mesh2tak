// Da amministratore gia' entrato in Authentik: apre la pagina della rete, trova la richiesta del ponte, legge il
// codice di controllo e preme "Approva"; poi aspetta che il ponte compaia nell'elenco (la pagina si aggiorna da sola).
const { chromium } = require('playwright');
const { sfondoFinto, apri } = require('./pagina_comune');
(async () => {
  const [url, nome, foto] = process.argv.slice(2);
  const b = await chromium.launch({args: ['--host-resolver-rules=MAP *.prova.lan 127.0.0.1', '--no-proxy-server']});
  const ctx = await b.newContext({ignoreHTTPSErrors: true, viewport: {width: 1280, height: 900}, deviceScaleFactor: 1});
  await ctx.addCookies([{name: 'ak_finto', value: 'andrea', domain: 'meshtastic.prova.lan', path: '/'}]);
  await sfondoFinto(ctx);
  const p = await ctx.newPage();
  const errori = [], finestre = [], chiamate = [];
  p.on('console', m => { if (m.type() === 'error') errori.push(m.text()); });
  p.on('pageerror', e => errori.push(String(e)));
  p.on('dialog', d => { finestre.push(d.message()); d.dismiss(); });
  p.on('request', r => { if (r.method() === 'POST') chiamate.push([r.url(), r.headers()['x-mesh2tak'] || '', r.postData()]); });
  await p.goto(url, {waitUntil: 'networkidle'});
  const riga = p.locator('section.novita li.richiesta', {hasText: nome});
  await riga.waitFor({timeout: 30000});
  if (foto) await p.screenshot({path: foto + '-richiesta.png', fullPage: true});
  const codice = (await riga.locator('.controllo b').innerText()).trim();
  const testoRichiesta = await riga.innerText();
  const titolo = await p.locator('section.novita h2').innerText();
  // chi scrive nel modulo "a mano" non perde nulla quando la pagina si aggiorna da sola; nome sbagliato fermato
  await apri(p, 'aggiungi');
  await p.click('summary:has-text("Aggiungilo a mano")');
  await p.fill('#nuovo-nome', 'Prova!');
  await p.click('#nuova-chiave');
  await p.keyboard.type('ssh-ed25519 AAAA');
  await p.evaluate(() => giro());
  await p.waitForTimeout(500);
  const modulo = {nome: await p.inputValue('#nuovo-nome'), chiave: await p.inputValue('#nuova-chiave'),
                  cursore: await p.evaluate(() => document.activeElement && document.activeElement.id),
                  nomeValido: await p.$eval('#nuovo-nome', e => e.validity.valid)};
  await riga.getByRole('button', {name: 'Approva'}).click();
  await p.waitForFunction(n => [...document.querySelectorAll('section.ponte h3')].some(h => h.textContent === n) &&
                               !document.querySelector('section.novita li.richiesta'), nome, {timeout: 120000, polling: 1000});
  if (foto) await p.screenshot({path: foto + '-aggiunto.png', fullPage: true});
  const testo = await p.innerText('main');
  console.log(JSON.stringify({codice, testoRichiesta, titolo, testo, errori, finestre, chiamate, modulo,
                              ponti: await p.$$eval('section.ponte h3', x => x.map(h => h.textContent))}));
  await b.close();
})().catch(e => { console.log(JSON.stringify({errore: String(e)})); process.exit(1); });
