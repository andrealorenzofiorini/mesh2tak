// ---- aggiornamenti di server e ponti (dalla 1.8.0): le versioni pubblicate su GitHub
let sezAggiornamenti = null, zonaAggiornamenti = null, versionePagina = "";
let daAggiornare = null;
let esitoControllo = "";           // esito dell'ultimo "Controlla adesso", scritto accanto al pulsante

function confronta(a, b) {
  const x = String(a || "0").split(".").map(Number), y = String(b || "0").split(".").map(Number);
  for (let i = 0; i < 3; i++) if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) - (y[i] || 0);
  return 0;
}

function versioniPonti() {
  return elenco.map(p => {
    const st = stati.get(p.nome), d = st && st.dati, sis = d && d.sistema;
    return {nome: p.nome, versione: d ? d.versione : null, aggiornabile: !!(sis && sis.aggiornabile),
            aggiornamento: sis ? sis.aggiornamento : null};
  });
}

function note(testo) {               // le novita' della release: le righe "- ..." diventano un elenco
  // una riga rientrata continua la voce di sopra (in NOVITA.md le voci lunghe vanno a capo)
  const box = el("div", "note");
  let ul = null, voce = null, par = null;
  for (const grezza of String(testo || "").split(/\r?\n/)) {
    const r = grezza.trim();
    if (!r) { par = null; continue; }
    if (/^#+\s/.test(r)) continue;
    const pulita = r.replace(/\*\*/g, "").replace(/`/g, "");
    if (/^[-*]\s+/.test(r)) {
      if (!ul) { ul = el("ul"); box.append(ul); }
      voce = el("li", null, pulita.replace(/^[-*]\s+/, ""));
      ul.append(voce);
      par = null;
    } else if (voce && /^\s/.test(grezza)) {
      voce.textContent += " " + pulita;
    } else if (par) {
      par.textContent += " " + pulita;
    } else {
      ul = null;
      voce = null;
      par = el("p", null, pulita);
      box.append(par);
    }
  }
  return box;
}

function costruisciAggiornamenti() {
  const sez = el("section", "aggiungi aggiornamenti");
  zonaAggiornamenti = el("div");
  zonaAggiornamenti.setAttribute("aria-live", "polite");
  sez.append(zonaAggiornamenti);
  return sez;
}

function chiediAggiornamento(o) {
  daAggiornare = o;
  const server = o.azione === "aggiorna-server";
  document.getElementById("aggiorna-titolo").textContent = server ? "Aggiornare il server alla " + o.versione + "?" :
    "Aggiornare " + (o.nomi.length === 1 ? "il gateway " + o.nomi[0] : "i gateway") + " alla " + o.versione + "?";
  document.getElementById("aggiorna-testo").textContent = server ?
    "Il server scarica la versione nuova da GitHub, la controlla e la installa. Per circa un minuto questa pagina non " +
    "risponde; i gateway continuano a portare le radio al TAK Server. Se il server non riparte, torna da solo alla " +
    "versione di prima." :
    "Ogni gateway scarica la versione nuova da GitHub entro un paio di minuti, la controlla e la installa: intanto si " +
    "ferma per un paio di minuti, quindi non farlo durante un intervento. Se non riparte, torna da solo alla versione " +
    "di prima.";
  // dalla 1.13.0 l'aggiornamento non cambia la radio; a richiesta la rimette come all'installazione
  document.getElementById("aggiorna-radio").hidden = server;
  document.getElementById("aggiorna-reimposta").checked = false;
  document.getElementById("aggiorna").showModal();
}

function aggiornaAggiornamenti() {
  if (!sezAggiornamenti) {
    sezAggiornamenti = costruisciAggiornamenti();
    gestioneDettagli.aggiornamenti.append(sezAggiornamenti);
  }
  const r = gestione && gestione.rilasci;
  gestioneDettagli.aggiornamenti.hidden = !r;
  suggerimentoGestione("aggiornamenti", "");
  avvisoVersione.replaceChildren();
  avvisoVersione.hidden = true;
  if (!r) return;
  if (!versionePagina && versione) versionePagina = versione;
  const z = zonaAggiornamenti;
  z.replaceChildren();
  const ponti = versioniPonti();
  const u = r.ultima;
  const inCorso = a => (gestione.lavori || []).some(l => l.azione === a && (l.esito === "in_coda" || l.esito === "in_corso"));
  z.append(el("p", null, "Versioni: server " + r.installata + (ponti.length ? "; " +
    ponti.map(p => "gateway " + p.nome + " " + (p.versione || "?")).join(", ") : "") + "."));
  let banner = "";
  if (r.nuova && u) {
    z.append(el("p", "evidenza", "È uscita la versione " + u.versione +
                                 (u.pubblicata ? ", pubblicata il " + giorno(Date.parse(u.pubblicata) / 1000) : "") + ". Novità:"));
    z.append(note(u.note));
    const b = el("button", "bottone", inCorso("aggiorna-server") ? "Aggiornamento del server in corso" : "Aggiorna il server alla " + u.versione);
    b.type = "button";
    b.disabled = inCorso("aggiorna-server");
    b.addEventListener("click", () => chiediAggiornamento({azione: "aggiorna-server", versione: u.versione}));
    z.append(b, el("small", null, "Prima il server, poi i gateway. I gateway continuano a lavorare mentre il server si aggiorna."));
    banner = "È uscita la versione " + u.versione + ": aggiorna il server.";
  } else if (u && confronta(u.versione, r.installata) <= 0) {
    const vecchi = ponti.filter(p => p.versione && confronta(p.versione, u.versione) < 0);
    const daQui = vecchi.filter(p => p.aggiornabile && !(p.aggiornamento && p.aggiornamento.stato === "in_corso"));
    const aMano = vecchi.filter(p => !p.aggiornabile);
    if (daQui.length) {
      z.append(el("p", "evidenza", (daQui.length === 1 ? "Il gateway " + daQui[0].nome + " ha" : daQui.length + " gateway hanno") +
                                   " una versione più vecchia della " + u.versione + "."));
      if (u.note) z.append(note(u.note));
      const b = el("button", "bottone", inCorso("aggiorna-ponti") ? "Richiesta in corso" :
                   "Aggiorna " + (daQui.length === 1 ? "il gateway " + daQui[0].nome : "i gateway") + " alla " + u.versione);
      b.type = "button";
      b.disabled = inCorso("aggiorna-ponti");
      b.addEventListener("click", () => chiediAggiornamento({azione: "aggiorna-ponti", versione: u.versione, nomi: daQui.map(p => p.nome)}));
      z.append(b, el("small", null, "Ogni gateway si ferma per un paio di minuti mentre si aggiorna: non farlo durante un intervento."));
      banner = (daQui.length === 1 ? "Il gateway " + daQui[0].nome + " è" : daQui.length + " gateway sono") + " da aggiornare alla " + u.versione + ".";
    } else if (!aMano.length && !vecchi.length) {
      z.append(el("p", "evidenza", "Tutto aggiornato all'ultima versione, la " + u.versione + "."));
    }
    if (aMano.length) {
      z.append(el("p", "nota", aMano.map(p => "gateway " + p.nome).join(", ") + ": la sua versione non si aggiorna dalla " +
                               "pagina. Aggiornalo un'ultima volta dal Raspberry, con l'installatore della " + u.versione + "."));
    }
  } else if (!u) {
    z.append(el("p", null, r.errore ? "Non riesco a leggere le versioni pubblicate su GitHub: " + r.errore + "." :
                                      "Non ho ancora controllato le versioni pubblicate su GitHub."));
  }
  // l'ultimo controllo non riuscito si dice sempre, anche quando si conosce gia' una versione (fino alla 1.16.1 no:
  // la pagina diceva "tutto aggiornato" mentre GitHub rifiutava le richieste)
  if (r.errore && u) z.append(el("p", "evidenza", "L'ultimo controllo su GitHub non è riuscito: " + r.errore +
                                                  ". Potrebbe esserci una versione più nuova di quella scritta qui."));
  if (esitoControllo) z.append(el("p", "evidenza", esitoControllo));
  const riga = el("p", "nota", r.controllato ? "Ultimo controllo su GitHub " + giornoOra(r.controllato) + ". " : "");
  const c = el("button", "bottone secondario piccolo", "Controlla adesso");
  c.type = "button";
  c.addEventListener("click", async () => {
    c.disabled = true;
    c.textContent = "Controllo...";
    try {
      const d = await manda("controlla-aggiornamenti", {});
      gestione.rilasci = d.rilasci;
      esitoControllo = "";
    } catch (e) {
      esitoControllo = "Controllo non riuscito: " + frase(e.message);    // accanto al pulsante, non in cima
    }
    disegna();
  });
  riga.append(c);
  z.append(riga);
  suggerimentoGestione("aggiornamenti", banner ? (r.nuova && u ? "versione " + u.versione + " disponibile" : "gateway da aggiornare") : "");
  // in cima: versione nuova, ponti da aggiornare, o pagina vecchia dopo l'aggiornamento del server
  if (versionePagina && gestione.versione && confronta(gestione.versione, versionePagina) !== 0) {
    const b = el("button", "bottone piccolo", "Ricarica la pagina");
    b.type = "button";
    b.addEventListener("click", () => location.reload());
    avvisoVersione.append(el("p", null, "Il server è passato alla " + gestione.versione + ": ricarica la pagina per vedere le novità."), b);
    avvisoVersione.hidden = false;
  } else if (banner) {
    const a = el("a", null, "Vai agli aggiornamenti");
    a.href = "#aggiornamenti";
    avvisoVersione.append(el("p", null, banner), a);
    avvisoVersione.hidden = false;
  }
}

let sezAvvisi = null, statoAvvisi = null;

function costruisciAvvisi() {
  const sez = el("section", "aggiungi avvisi-email");
  sez.append(el("p", null, "Il server ti scrive quando un gateway non risponde, perde la radio o il TAK Server da 10 minuti, " +
                           "e quando torna a posto; quando la microSD è quasi piena, il certificato sta per scadere, " +
                           "la corrente non basta o un Raspberry si riavvia. Le email vanno agli indirizzi degli avvisi " +
                           "di infra-TAK (Guard Dog)."));
  statoAvvisi = el("p", "stato-avvisi");
  statoAvvisi.setAttribute("aria-live", "polite");
  const b = el("button", "bottone secondario", "Manda un'email di prova");
  b.type = "button";
  const esito = el("p", "esito");
  esito.setAttribute("aria-live", "polite");
  b.addEventListener("click", async () => {
    b.disabled = true;
    esito.textContent = "Mando...";
    try {
      const r = await manda("prova-avvisi", {});
      esito.textContent = r.messaggio || "Fatto.";
    } catch (e) {
      esito.textContent = "Non riuscito: " + e.message;
    } finally {
      b.disabled = false;
    }
  });
  sez.append(statoAvvisi, b, esito);
  return sez;
}

function aggiornaAvvisi() {
  if (!sezAvvisi) {
    sezAvvisi = costruisciAvvisi();
    gestioneDettagli.avvisi.append(sezAvvisi);
  }
  const a = gestione && gestione.avvisi;
  gestioneDettagli.avvisi.hidden = !a;
  if (!a) return;
  const u = a.ultimo;
  let testo;
  if (!u) testo = "Finora nessun avviso.";
  else if (u.esito === "mandato") testo = "Ultimo avviso mandato " + giornoOra(u.quando) + ": " +
                                          String(u.oggetto || "").replace(/^\[Rete Meshtastic\] /, "") + ".";
  else if (u.esito === "nessuno") testo = "L'ultimo avviso non è partito: in infra-TAK non c'è un indirizzo per gli avvisi. " +
                                          "Impostalo nella console infra-TAK, in Guard Dog, poi manda un'email di prova.";
  else if (u.esito === "pausa") testo = "L'ultimo avviso non è partito: gli avvisi di infra-TAK sono sospesi (Guard Dog).";
  else if (u.esito === "trattenuto") testo = "L'ultimo avviso è trattenuto: infra-TAK è in manutenzione. Riprovo da solo.";
  else testo = "L'ultimo avviso non è partito: " + (u.dettaglio || "errore") + ". Riprovo da solo.";
  if (a.in_coda) testo += " In attesa di partire: " + a.in_coda + (a.in_coda === 1 ? " avviso." : " avvisi.");
  statoAvvisi.textContent = testo;
  statoAvvisi.dataset.esito = u ? u.esito : "";
}

function disegnaRifiutate(rif) {
  const ul = el("ul", "righe");
  rif.forEach(r => {
    const li = el("li", "richiesta");
    const chi = el("div");
    chi.append(el("h3", null, r.nome || "?"));
    const come = r.motivo === "tolto" ? "Tolto dalla rete" : "Richiesta rifiutata";
    chi.append(el("p", "da", come + " da " + (r.da || "?") + " " + giornoOra(r.ultima) +
                              (r.macchina ? ", Raspberry " + r.macchina : "") + (r.ip ? ", indirizzo " + r.ip : "")));
    const codice = el("div", "controllo");
    codice.append(el("small", null, "codice di controllo"), el("b", null, r.controllo || "?"));
    li.append(chi, codice);
    if (r.presente) {
      li.append(el("p", "lavora", "Ora c'è un altro gateway " + r.nome + ": per farlo rientrare togli prima quello."));
    } else {
      const pulsanti = el("div", "pulsanti");
      const b = el("button", "bottone secondario piccolo", "Lascia rientrare");
      b.type = "button";
      b.addEventListener("click", () => azioneRichiesta(b, "riammetti", {id: r.id}));
      pulsanti.append(b);
      li.append(pulsanti);
    }
    ul.append(li);
  });
  const det = dettagli("rifiutate", "Gateway tolti e richieste rifiutate (" + rif.length + ")",
                       el("p", "nota", "Questi Raspberry non possono rientrare da soli, per 30 giorni. Se è stato un errore, lasciali rientrare."), ul);
  det.classList.add("rifiutate");
  return det;
}

async function azioneRichiesta(bottone, azione, dati) {
  bottone.disabled = true;
  try {
    const d = await manda(azione, dati);
    if (d.messaggio) avviso = d.messaggio;
  } catch (e) {
    avviso = "Non riuscito: " + e.message;
  }
  await giro();
}

let avviso = "";                   // esito dell'ultima azione, mostrato una volta in cima

function chiediTogli(p) {
  daTogliere = p;
  document.getElementById("togli-testo").textContent = "Il gateway " + p.nome + " non sarà più raggiungibile da questa pagina e il suo indirizzo " +
    p.indirizzo.replace(/^https:\/\//, "").replace(/\/$/, "") + " smette di funzionare.";
  document.getElementById("togli").showModal();
}

