// Comune alle prove della pagina della rete nel browser. Dalla 1.15.0 la mappa della rete e' in cima alla pagina: le
// immagini della mappa di sfondo le da' la prova stessa (la prova non ha internet), cosi' nessuna richiesta fallisce
// e si controlla anche che la pagina le possa caricare (CSP).
const SFONDO = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGN4/PIOAAVbAqlheK8AAAAAAElFTkSuQmCC', 'base64');

async function sfondoFinto(ctx, richieste) {
  await ctx.route(/^https:\/\/([abc]\.)?tile\.(opentopomap|openstreetmap)\.org\//, r => {
    if (richieste) richieste.push({url: r.request().url(), referer: r.request().headers()['referer'] || ''});
    r.fulfill({status: 200, contentType: 'image/png', body: SFONDO});
  });
}

// I gruppi della gestione (aggiungere un gateway, avvisi, aggiornamenti) restano chiusi finche' non servono
async function apri(p, id) {
  await p.waitForSelector('#' + id, {state: 'attached', timeout: 15000});
  await p.evaluate(n => { document.getElementById(n).open = true; }, id);
}

// Immagini in pagina che non sono icone scelte ne' mappa di sfondo: devono essere zero (niente codice dalle radio)
const IMMAGINI = () => [...document.querySelectorAll('main img')]
  .filter(i => !i.closest('.tessere') && !i.classList.contains('figura-icona')).length;

// Richieste ancora senza risposta: per capire, quando una prova scade, che cosa la pagina stava aspettando
function inSospeso(p) {
  const aperte = new Map();
  p.on('request', r => aperte.set(r, Date.now()));
  p.on('requestfinished', r => aperte.delete(r));
  p.on('requestfailed', r => aperte.delete(r));
  return () => [...aperte].map(([r, t]) => r.method() + ' ' + r.url() + ' da ' + Math.round((Date.now() - t) / 1000) + ' s');
}

module.exports = {SFONDO, sfondoFinto, apri, IMMAGINI, inSospeso};
