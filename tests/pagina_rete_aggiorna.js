// Pagina della rete (1.8.0) come un amministratore entrato in Authentik: versione nuova su GitHub con le novita',
// avviso in cima, "Aggiorna il server" con la conferma, il lavoro e il suo esito; il fatto "Aggiornamento" del ponte.
const { chromium } = require('playwright');
const { sfondoFinto, apri } = require('./pagina_comune');
let diagnosi = () => ({});      // se la prova si ferma: cosa diceva la pagina (in un file se c'e' DIAGNOSI)
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
  const risposte = [];
  p.on('response', async x => {
    if (/api\/controlla/.test(x.url())) risposte.push(x.status() + ' ' + (await x.text().catch(() => '')).slice(0, 600));
  });
  diagnosi = async () => ({errori, risposte, sezione: await p.innerText('#aggiornamenti').catch(() => ''),
                          messaggio: await p.innerText('#messaggio').catch(() => '')});
  p.on('dialog', d => { finestre.push(d.message()); d.dismiss(); });
  await p.goto(url + '#gateway', {waitUntil: 'networkidle'});
  await p.waitForSelector('section.ponte .fatti', {timeout: 15000});
  await apri(p, 'aggiornamenti');
  // la release nuova si vede dopo "Controlla adesso" (il servizio controlla da solo ogni 6 ore)
  await p.click('#aggiornamenti button:has-text("Controlla adesso")');
  await p.waitForFunction(() => /È uscita la versione/.test(document.querySelector('#aggiornamenti').textContent),
                          null, {timeout: 15000});
  const sezione = await p.innerText('#aggiornamenti');
  const in_cima = await p.innerText('.avviso-versione');
  const ponte = await p.innerText('section.ponte .fatti');
  await p.click('#aggiornamenti button:has-text("Aggiorna il server")');
  await p.waitForSelector('#aggiorna[open]', {timeout: 5000});
  const domanda = await p.innerText('#aggiorna-titolo');
  await p.click('#aggiorna-conferma');
  await p.waitForFunction(() => [...document.querySelectorAll('p.lavoro')].some(x =>
    /Aggiornamento del server/.test(x.textContent) && x.dataset.esito === 'errore'), null, {timeout: 60000});
  const lavoro = await p.$$eval('p.lavoro', xs => xs.map(x => x.innerText).filter(t => /Aggiornamento del server/.test(t)).join('\n'));
  console.log(JSON.stringify({sezione, in_cima, ponte, domanda, lavoro, errori, finestre}));
  await b.close();
})().catch(async e => {
  const d = {errore: String(e), ...(await diagnosi())};
  if (process.env.DIAGNOSI) require('fs').writeFileSync(process.env.DIAGNOSI, JSON.stringify(d, null, 1));
  console.log(JSON.stringify(d));
  process.exit(1);
});
