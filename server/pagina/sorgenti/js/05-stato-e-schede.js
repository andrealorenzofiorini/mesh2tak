// ---- stato della rete (dalla 1.15.0): per ogni gateway e ripetitore le cose da guardare, dalla piu' grave ----

function statoGateway(p) {
  const v = valutaPonte(p), st = stati.get(p.nome), d = st && st.dati, avvisi = [];
  if (v.classe === "giu") avvisi.push(["problema", "non risponde"]);
  else if (v.classe === "guasto") avvisi.push(["problema", minuscola(v.parole)]);
  else if (v.classe === "pausa") avvisi.push(["attenzione", minuscola(v.parole)]);
  if (d) {
    const c = d.certificato;
    if (c && c.scade) {
      const giorni = Math.floor((c.scade - d.ora) / 86400);
      if (c.scade <= d.ora) avvisi.push(["problema", "certificato TAK scaduto"]);
      else if (giorni < 30) avvisi.push(["attenzione", "il certificato TAK scade tra " + Math.max(1, giorni) + (giorni === 1 ? " giorno" : " giorni")]);
    }
    const sis = d.sistema || {};
    if (sis.disco && sis.disco.libero_mb < 500) avvisi.push(["problema", "microSD quasi piena"]);
    if (sis.alimentazione && sis.alimentazione.ultima && d.ora - sis.alimentazione.ultima < 3600) {
      avvisi.push(["problema", "la corrente non basta"]);
    }
    const ag = sis.aggiornamento;
    if (ag && ag.versione && ag.quando && d.ora - ag.quando < 86400 && ["errore", "ripristinato"].includes(ag.stato)) {
      avvisi.push(["attenzione", "aggiornamento alla " + ag.versione + " non riuscito"]);
    }
    const cr = d.canale_radio;
    if (cr && cr.trasmesso >= 8) avvisi.push(["attenzione", "trasmette il " + numIt(cr.trasmesso, 2) + "% del tempo (limite 10%)"]);
  }
  return {livello: avvisi.reduce((m, a) => Math.max(m, LIVELLI[a[0]]), 0), avvisi, v};
}

function etaRipetitore(v) {
  // acceso se si fa sentire o se ripete i messaggi degli altri verso un gateway
  return Math.min(v.eta, v.etaRipetuto);
}

function statoRipetitore(v) {
  const avvisi = [], eta = etaRipetitore(v);
  if (eta >= RIPETITORE_SPENTO_S) avvisi.push(["problema", "non si sente da " + durata(eta)]);
  else if (eta >= RIPETITORE_ATTENZIONE_S) avvisi.push(["attenzione", "non si sente da " + durata(eta)]);
  if (v.batteria !== null && v.batteria !== undefined && v.batteria <= 100 && v.etaMetriche < 24 * 3600) {
    if (v.batteria <= 20) avvisi.push(["problema", "batteria al " + v.batteria + "%"]);
    else if (v.batteria <= 40) avvisi.push(["attenzione", "batteria al " + v.batteria + "%"]);
  }
  return {livello: avvisi.reduce((m, a) => Math.max(m, LIVELLI[a[0]]), 0), avvisi,
          spento: eta >= RIPETITORE_SPENTO_S};
}

function segnoStato(livello, spento) {
  // forma e colore insieme: cerchio a posto, rombo da guardare, triangolo problema, anello spento
  const s = el("span", "semaforo");
  s.dataset.livello = spento ? "spento" : NOMI_LIVELLI[livello];
  s.setAttribute("aria-hidden", "true");
  return s;
}

function problemiDellaRete() {
  const voci = [];
  const attesa = gestione ? (gestione.richieste || []).filter(r => r.stato === "attesa").length : 0;
  if (attesa) {
    voci.push({livello: 1, testo: attesa === 1 ? "Un gateway chiede di entrare nella rete: controlla il codice e approvalo"
                                               : attesa + " gateway chiedono di entrare nella rete", dove: "richieste"});
  }
  for (const p of elenco) {
    statoGateway(p).avvisi.forEach(([l, t]) => voci.push({livello: LIVELLI[l], testo: "Gateway " + p.nome + ": " + t,
                                                          dove: "gateway-" + p.nome}));
  }
  for (const v of ripetitoriDellaRete()) {
    statoRipetitore(v).avvisi.forEach(([l, t]) => voci.push({livello: LIVELLI[l], testo: nomeRipetitore(v) + ": " + t,
                                                             dove: idRipetitore(v.id)}));
  }
  for (const v of radioDellaSquadra()) {
    if (problemi(v).some(x => x[0].startsWith("batteria"))) {
      voci.push({livello: 1, testo: (v.nome || v.id) + ": batteria quasi scarica", dove: "squadra"});
    }
  }
  return voci.sort((a, b) => b.livello - a.livello);
}

function disegnaStatoRete() {
  const sintesi = document.getElementById("sintesi");
  const voci = problemiDellaRete();
  const peggiore = voci.reduce((m, x) => Math.max(m, x.livello), 0);
  let livello = NOMI_LIVELLI[peggiore], testo;
  if (erroreGiro === "sessione scaduta") {
    livello = "spento";
    testo = "Sessione scaduta";
  } else if (erroreGiro && !ultimoElenco) {
    livello = "spento";
    testo = "Stato non disponibile";
  } else if (!elenco.length) {
    livello = "spento";
    testo = "Nessun gateway nella rete";
  } else if (!voci.length) {
    testo = "Tutto a posto";
  } else {
    const gravi = voci.filter(x => x.livello === 2).length, altre = voci.length - gravi;
    testo = gravi ? (gravi === 1 ? "1 problema" : gravi + " problemi") + (altre ? ", " + altre + " da controllare" : "")
                  : (altre === 1 ? "1 cosa da controllare" : altre + " cose da controllare");
  }
  sintesi.dataset.livello = livello;
  const segno = el("span", "semaforo");
  segno.dataset.livello = livello;
  sintesi.replaceChildren(segno, el("span", null, testo));
  zonaStato.replaceChildren();
  if (!voci.length || livello === "spento") return;
  const ul = el("ul", "problemi-rete");
  ul.setAttribute("aria-label", "Da controllare");
  voci.slice(0, 6).forEach(x => {
    const li = el("li");
    li.dataset.livello = NOMI_LIVELLI[x.livello];
    const a = el("a", null, x.testo);
    a.href = "#" + x.dove;
    li.append(segnoStato(x.livello), a);
    ul.append(li);
  });
  if (voci.length > 6) ul.append(el("li", "altre-voci", "e altre " + (voci.length - 6) + ", nelle schede qui sotto"));
  zonaStato.append(ul);
}

function mostraScheda(nome, nellIndirizzo) {
  if (!SCHEDE.some(x => x[0] === nome)) nome = "rete";
  schedaAperta = nome;
  for (const [chiave, , parte] of SCHEDE) {
    parte.hidden = chiave !== nome;
    bottoniSchede[chiave].setAttribute("aria-selected", String(chiave === nome));
    bottoniSchede[chiave].tabIndex = chiave === nome ? 0 : -1;
  }
  if (nellIndirizzo && location.hash !== "#" + nome) history.replaceState(null, "", "#" + nome);
  if (nome === "storico") caricaStorico(false).then(disegnaStorico);
}

function vaiA(id) {
  // un collegamento della pagina (o un vecchio indirizzo, fino alla 1.15.0): apre la scheda giusta e ci va
  id = {radio: "squadra", ponti: "gateway", copertura: "mappa"}[id] || id;
  const scheda = SCHEDE.find(x => x[0] === id);
  if (scheda) {
    mostraScheda(id, false);
    return;
  }
  const dove = document.getElementById(id);
  if (!dove || dove.tagName === "DIALOG") return;
  const dentro = SCHEDE.find(x => x[2].contains(dove));
  if (dentro) mostraScheda(dentro[0], false);
  if (dove.tagName === "DETAILS") dove.open = true;
  dove.scrollIntoView({block: "start", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth"});
}

function voceRete(tipo, nome, livello, spento, righe, id, punto) {
  const li = el("li", "voce-rete");
  li.dataset.tipo = tipo;
  const b = el("button", "centra");
  b.type = "button";
  const testa = el("span", "testa-voce");
  testa.append(segnoStato(livello, spento), el("b", null, nome));
  b.append(testa, ...righe.filter(Boolean).map((r, i) => el("span", i ? "riga-voce secondaria" : "riga-voce", r)));
  if (punto) {
    b.title = "Mostra sulla mappa";
    b.addEventListener("click", () => mappaCopertura && mappaCopertura.centra(punto.lat, punto.lon, punto.chiave));
  } else {
    b.disabled = true;
    b.title = "Posizione non nota: non è sulla mappa";
  }
  const a = el("a", "dettagli-voce", "Dettagli");
  a.href = "#" + id;
  a.setAttribute("aria-label", "Dettagli di " + nome);
  li.append(b, a);
  return li;
}

function puntiDellaRete() {
  // dove stanno gateway e ripetitori (dalla copertura, che i gateway danno solo dietro Authentik) e come stanno
  const punti = [];
  for (const p of elenco) {
    const c = copertura.get(p.nome), pos = c && c.dati && c.dati.ponte;
    if (!pos || !Number.isFinite(pos.lat) || !Number.isFinite(pos.lon)) continue;
    const s = statoGateway(p);
    punti.push({tipo: "gateway", chiave: "g:" + p.nome, nome: p.nome, lat: pos.lat, lon: pos.lon, livello: s.livello,
                spento: s.v.classe === "giu", righe: ["Gateway " + p.nome + ": " + minuscola(s.v.parole),
                                                      ...s.avvisi.filter(a => a[1] !== minuscola(s.v.parole)).map(a => maiuscola(a[1]))]});
  }
  const ripetitori = new Map(ripetitoriDellaRete().map(v => [v.id, v]));
  const visti = new Set();
  for (const [, c] of copertura) {
    for (const r of (c.dati && c.dati.ripetitori) || []) {
      if (!r || visti.has(r.id) || !Number.isFinite(r.lat) || !Number.isFinite(r.lon)) continue;
      visti.add(r.id);
      const v = ripetitori.get(r.id);
      const s = v ? statoRipetitore(v) : {livello: 0, avvisi: [], spento: false};
      punti.push({tipo: "ripetitore", chiave: "r:" + r.id, nome: r.nome || r.id, lat: r.lat, lon: r.lon, livello: s.livello,
                  spento: s.spento, righe: [(v ? nomeRipetitore(v) : r.nome || r.id) + ": " +
                                            (v ? "attivo " + fa(etaRipetitore(v)) : "ripetitore"),
                                            ...s.avvisi.map(a => maiuscola(a[1]))]});
    }
  }
  return punti;
}

function testoBatteria(v) {
  if (v.batteria === null || v.batteria === undefined) return "batteria non inviata";
  return v.batteria > 100 ? "alimentato dalla rete" : "batteria " + v.batteria + "%";
}

function disegnaElencoRete() {
  const punti = puntiDellaRete();
  const dove = chiave => punti.find(x => x.chiave === chiave);
  const voci = [];
  for (const p of elenco) {
    const s = statoGateway(p), st = stati.get(p.nome), d = st && st.dati;
    const righe = [s.v.parole];
    if (d && d.tak && d.nodo && s.v.classe === "ok") righe.push((d.radio_sulla_mappa || 0) + " radio sulla mappa TAK");
    const altri = s.avvisi.filter(a => a[1] !== minuscola(s.v.parole));
    if (altri.length) righe.push(maiuscola(altri[0][1]));
    voci.push(voceRete("gateway", "Gateway " + p.nome, s.livello, s.v.classe === "giu", righe, "gateway-" + p.nome,
                       dove("g:" + p.nome)));
  }
  for (const v of ripetitoriDellaRete()) {
    const s = statoRipetitore(v);
    voci.push(voceRete("ripetitore", nomeRipetitore(v), s.livello, s.spento,
                       ["attivo " + fa(etaRipetitore(v)), testoBatteria(v)], idRipetitore(v.id), dove("r:" + v.id)));
  }
  const radio = radioDellaSquadra();
  const accese = radio.filter(v => v.eta < SILENZIO_S).length;
  const li = el("li", "voce-rete voce-radio");
  const a = el("a", null, "Radio della squadra");
  a.href = "#squadra";
  li.append(a, el("span", "riga-voce", radio.length ? accese + (accese === 1 ? " accesa" : " accese") + " su " + radio.length
                                                    : "nessuna sentita finora"));
  voci.push(li);
  elencoRete.replaceChildren(...voci);
}

function testoCopertura(chiave) {
  // quanti quadretti della mappa vengono da questo gateway (sentiti diretti o tramite altri) o da questo ripetitore
  const ripetitore = chiave.startsWith("r:") ? chiave.slice(2) : "";
  let celle = 0;
  const conta = new Map();
  for (const [nome, c] of copertura) {
    if (!c.dati || (!ripetitore && "g:" + nome !== chiave)) continue;
    const limite = Number.isFinite(c.dati.limite_snr) ? c.dati.limite_snr : -17.5;
    for (const q of c.dati.celle || []) {
      if (!Array.isArray(q)) continue;
      const via = q[8] && typeof q[8] === "object" ? q[8] : {};
      if (ripetitore) {
        if (via[ripetitore]) celle++;
        continue;
      }
      celle++;
      const k = q[2] > 0 && q[3] !== null ? classeCopertura(q[3] - limite)
                                          : (Object.keys(via).length ? VIA_RIPETITORE : RIPETUTA);
      conta.set(k, (conta.get(k) || 0) + 1);
    }
  }
  if (!ripetitore) {
    const c = copertura.get(chiave.slice(2));
    const st = stati.get(chiave.slice(2)), ver = st && st.dati && st.dati.versione;
    if (!c) return "in caricamento";
    if (c.errore === "vecchio") return "si raccoglie dalla 1.14.0: aggiorna il gateway" + (ver ? " (è alla " + ver + ")" : "");
    if (!c.dati) return c.errore === "giu" ? "non letta: il gateway non risponde" : "non letta (" + c.errore + ")";
  }
  if (!celle) return ripetitore ? "nessun quadretto finora" : "nessun quadretto finora: si riempie quando le radio mandano la posizione";
  const parti = [...CLASSI_COPERTURA, VIA_RIPETITORE, RIPETUTA].filter(k => conta.get(k))
    .map(k => k.breve + " " + conta.get(k));
  if (ripetitore) return celle + (celle === 1 ? " quadretto raggiunto" : " quadretti raggiunti") + " tramite questo ripetitore";
  return celle + (celle === 1 ? " quadretto" : " quadretti") + ": " + parti.join(", ");
}

function disegnaRipetitore(v) {
  const s = statoRipetitore(v), eta = etaRipetitore(v);
  const sez = el("section", "ripetitore");
  sez.id = idRipetitore(v.id);
  const capo = el("div", "capo"), testa = el("div");
  const h3 = el("h3", null, nomeRipetitore(v));
  h3.id = "titolo-" + sez.id;
  sez.setAttribute("aria-labelledby", h3.id);
  const chi = [v.nome_breve ? "sigla " + v.nome_breve : "", nomeModello(v.modello),
               v.ruolo ? "ruolo " + (RUOLI[v.ruolo] || v.ruolo.toLowerCase()) : "", v.id].filter(Boolean).join(", ");
  const stato = el("p", "stato");
  stato.dataset.livello = s.spento ? "spento" : NOMI_LIVELLI[s.livello];
  stato.append(segnoStato(s.livello, s.spento), el("span", null, s.avvisi.length ? maiuscola(s.avvisi[0][1]) : "Acceso"));
  testa.append(h3, el("p", "chi", chi), stato);
  capo.append(testa);
  const azione = el("div", "azione");
  if (pontiCheGestiscono(v).length) {
    const g = el("button", "bottone secondario piccolo gestisci", "Gestisci da lontano");
    g.type = "button";
    g.addEventListener("click", () => apriGestioneRadio(v));
    azione.append(g);
  }
  if (iconeAttive()) azione.append(bottoneIcona(v.id, nomeRipetitore(v), false));
  if (azione.childElementCount) capo.append(azione);
  sez.append(capo);
  const dl = el("dl", "fatti");
  const bat = el("div", v.batteria !== null && v.batteria !== undefined && v.batteria <= 40 && v.etaMetriche < 24 * 3600 ?
                        (v.batteria <= 20 ? "guasto" : "attenzione") : null);
  const dd = el("dd");
  if (v.batteria === null || v.batteria === undefined) {
    dd.append("non la manda");
    dd.append(el("small", "suggerimento-batteria", "Si attiva sul ripetitore: app Meshtastic, Telemetria, «Device " +
                                                    "Metrics» acceso, intervallo 1800 secondi."));
  } else {
    dd.append(v.batteria > 100 ? "alimentato dalla rete" : v.batteria + "%" + (v.tensione ? ", " + numIt(v.tensione, 2) + " V" : ""));
    if (v.etaMetriche >= 3600) dd.append(el("small", "suggerimento-batteria", "letta " + fa(v.etaMetriche)));
  }
  bat.append(el("dt", null, "Batteria"), dd);
  dl.append(bat);
  fatto(dl, "Si è fatto sentire", fa(v.eta), s.spento ? "guasto" : "");
  fatto(dl, "Ha ripetuto verso il gateway", Number.isFinite(v.etaRipetuto) ? "l'ultima volta " + fa(v.etaRipetuto) + "; " +
        v.ripetuti + (v.ripetuti === 1 ? " messaggio" : " messaggi") + " da quando il gateway è acceso" : "nessun messaggio finora");
  fatto(dl, "Col gateway", v.ponti.map(x => x.ponte + ": " + (x.salti === 0 ? "diretto" + (qualita(x.snr, x.rssi) ? ", segnale " + qualita(x.snr, x.rssi) : "")
                                                                  : (x.salti > 0 ? "tramite " + (x.salti === 1 ? "1 radio" : x.salti + " radio") : "sentito"))).join("; "));
  fatto(dl, "Copertura", testoCopertura("r:" + v.id));
  fattoPrevista(dl, "r:" + v.id);
  if (v.acceso !== null && v.acceso !== undefined && v.etaMetriche < 24 * 3600) fatto(dl, "Acceso da", durata(v.acceso));
  if (v.occupato !== null && v.occupato !== undefined && v.etaMetriche < 24 * 3600) {
    fatto(dl, "Canale radio", "occupato il " + numIt(v.occupato) + "%" + (v.trasmesso !== null && v.trasmesso !== undefined ?
                              "; trasmette il " + numIt(v.trasmesso, 2) + "%" : ""));
  }
  if (!puntiDellaRete().some(x => x.chiave === "r:" + v.id)) {
    fatto(dl, "Sulla mappa", "non ancora: compare quando il ripetitore manda la sua posizione (con ruolo Router ogni 12 ore)");
  }
  sez.append(dl);
  return sez;
}

function disegnaSchede() {
  const sezioni = [];
  for (const p of elenco) {
    // uno stato strano di un gateway non deve fermare il resto della pagina
    try {
      sezioni.push(disegnaPonte(p));
    } catch (e) {
      console.error("gateway " + p.nome + ": " + e);
      const sez = el("section", "ponte");
      sez.id = "gateway-" + p.nome;
      sez.dataset.stato = "guasto";
      sez.append(el("h3", null, p.nome), el("p", "spiega", "Lo stato mandato da questo gateway non si legge: aggiornalo all'ultima versione."));
      sezioni.push(sez);
    }
  }
  if (!sezioni.length) {
    const vuoto = el("p", "vuoto", "Nessun gateway nella rete. Per aggiungerne uno apri ");
    const a = el("a", null, "Aggiungere un gateway");
    a.href = "#aggiungi";
    vuoto.append(a, ", in fondo alla pagina.");
    sezioni.push(vuoto);
  }
  zonaPonti.replaceChildren(el("h2", null, "Gateway"), ...sezioni);
  const ripetitori = ripetitoriDellaRete().map(v => {
    try {
      return disegnaRipetitore(v);
    } catch (e) {
      console.error("ripetitore " + v.id + ": " + e);
      return el("p", "vuoto", nomeRipetitore(v) + ": dati non leggibili.");
    }
  });
  zonaRipetitori.replaceChildren(el("h2", null, "Ripetitori"), ...(ripetitori.length ? ripetitori :
    [el("p", "vuoto", "Nessun ripetitore sentito finora. Un ripetitore compare qui quando trasmette sul canale della " +
                      "squadra e ha il ruolo da ripetitore (Router, Repeater) o una posizione fissa.")]));
}

function suggerimentoGestione(chiave, testo) {
  const det = gestioneDettagli[chiave];
  if (!det) return;
  if (chiave === "aggiornamenti") bottoniSchede.gestione.lastChild.textContent = testo ? "!" : "";
  const s = det.querySelector("summary");
  let piccolo = s.querySelector("small");
  if (!testo) { if (piccolo) piccolo.remove(); return; }
  if (!piccolo) s.append(piccolo = el("small"));
  piccolo.textContent = testo;
}

function costruisciRadio() {
  // le radio della squadra si costruiscono una volta: la ricerca non si perde quando la pagina si aggiorna
  vistaRadio.className = "radio-rete";
  vistaRadio.id = "squadra";
  const h2 = el("h2", null, "Radio della squadra");
  h2.id = "titolo-squadra";
  riassuntoRadio = el("p", "riassunto");
  riassuntoRadio.setAttribute("aria-live", "polite");
  avvisiRadio = el("ul", "avvisi");
  const cerca = el("div", "cerca");
  const etichetta = el("label", null, "Cerca");
  cercaRadio = el("input");
  cercaRadio.type = "search";
  cercaRadio.placeholder = "nome, sigla o modello";
  cercaRadio.autocomplete = "off";
  etichetta.htmlFor = cercaRadio.id = "cerca-radio";
  cercaRadio.addEventListener("input", () => disegnaRadioRete());
  cerca.append(etichetta, cercaRadio);
  testaVoci = el("div", "testa-voci");
  ["Radio", "Batteria", "Ultimo segnale", "Chi la sente"].forEach(x => testaVoci.append(el("span", null, x)));
  elencoRadio = el("ol", "voci");
  vistaRadio.append(h2, riassuntoRadio, avvisiRadio, cerca, testaVoci, elencoRadio,
                    el("p", "nota", "Compaiono le radio che trasmettono sul canale della squadra. La batteria solo se la " +
                                    "radio la manda (app Meshtastic: Telemetria, «Device Metrics»). I gateway ascoltano " +
                                    "soltanto: non chiedono nulla alle radio."));
}

function costruisciPagina() {
  costruita = true;
  // in alto: la mappa della rete e, accanto, gateway e ripetitori con il loro stato
  panorama.className = "panorama";
  panorama.id = "mappa";
  const lato = el("aside", "rete");
  lato.setAttribute("aria-labelledby", "titolo-rete");
  const h2 = el("h2", null, "La rete");
  h2.id = "titolo-rete";
  elencoRete = el("ul", "elenco-rete");
  lato.append(h2, elencoRete);
  vistaCopertura.className = "pannello-mappa";
  panorama.append(vistaCopertura, lato);
  costruisciCopertura();
  schede.className = "schede";
  schede.setAttribute("role", "tablist");
  schede.setAttribute("aria-label", "Parti della pagina");
  for (const [chiave, titolo, parte] of SCHEDE) {
    const b = el("button", null, titolo);
    b.type = "button";
    b.id = "scheda-" + chiave;
    b.setAttribute("role", "tab");
    b.append(el("span"));
    b.addEventListener("click", () => mostraScheda(chiave, true));
    b.addEventListener("keydown", ev => {       // frecce tra le schede, come vuole la tastiera
      const i = SCHEDE.findIndex(x => x[0] === chiave), d = {ArrowRight: 1, ArrowLeft: -1}[ev.key];
      if (!d) return;
      const altra = SCHEDE[(i + d + SCHEDE.length) % SCHEDE.length][0];
      mostraScheda(altra, true);
      bottoniSchede[altra].focus();
      ev.preventDefault();
    });
    bottoniSchede[chiave] = b;
    schede.append(b);
    parte.setAttribute("role", "tabpanel");
    parte.setAttribute("aria-labelledby", b.id);
  }
  zonaPonti.className = "gruppo-rete";
  zonaPonti.id = "gateway";
  zonaRipetitori.className = "gruppo-rete";
  zonaRipetitori.id = "ripetitori";
  costruisciRadio();
  costruisciStorico();
  // in fondo, chiusa: aggiungere un gateway, avvisi per email, aggiornamenti
  zonaGestione.className = "gestione-rete";
  zonaGestione.id = "gestione";
  const h2g = el("h2", null, "Gestione");
  h2g.id = "titolo-gestione";
  zonaGestione.append(h2g);
  for (const [chiave, titolo] of [["aggiungi", "Aggiungere un gateway"], ["avvisi", "Avvisi per email"],
                                  ["aggiornamenti", "Aggiornamenti"]]) {
    const det = el("details", "gruppo");
    det.id = chiave;
    det.open = true;
    det.append(el("summary", null, titolo));
    gestioneDettagli[chiave] = det;
    zonaGestione.append(det);
  }
  window.addEventListener("hashchange", () => vaiA(decodeURIComponent(location.hash.slice(1))));
  vistaRete.id = "rete";
  mostraScheda("rete", false);
  if (location.hash.length > 1) vaiA(decodeURIComponent(location.hash.slice(1)));
}

function disegnaVoce(v) {
  const li = el("li", "voce");
  const silenziosa = v.eta >= SILENZIO_S;
  const pr = problemi(v);
  li.dataset.stato = pr.length ? "problema" : (silenziosa ? "silenziosa" : "ok");
  const nome = el("div", "nome");
  nome.append(el("b", null, v.nome || "Radio " + v.id));
  nome.append(el("small", null, [v.nome_breve, v.id].filter(Boolean).join(", ")));
  const cosa = [nomeModello(v.modello), v.ruolo ? (RUOLI[v.ruolo] || v.ruolo.toLowerCase()) : ""].filter(Boolean).join(", ");
  if (cosa) nome.append(el("small", null, cosa));
  if (pr.length) {
    const box = el("div", "problemi");
    pr.forEach(([testo, classe]) => box.append(el("span", classe || null, testo)));
    nome.append(box);
  }
  const bat = el("div", "batteria");
  if (v.batteria === null || v.batteria === undefined) {
    bat.append(el("b", null, "non nota"));
  } else if (v.batteria > 100) {
    bat.append(el("b", null, "alimentata"));
  } else {
    bat.append(el("b", null, v.batteria + "%"));
    const pila = el("div", "pila");
    const livello = el("i", v.batteria <= 20 ? "scarica" : (v.batteria <= 40 ? "bassa" : null));
    livello.style.width = Math.max(3, v.batteria) + "%";
    pila.append(livello);
    bat.append(pila);
  }
  if (v.tensione) bat.append(el("small", null, numIt(v.tensione, 2) + " V"));
  const quando = el("div", "quando");
  quando.append(el("b", null, "sentita " + fa(v.eta)));
  if (silenziosa) quando.append(el("small", null, "spenta o fuori copertura"));
  else if (v.acceso !== null && v.acceso !== undefined && v.etaMetriche < 6 * 3600) {
    quando.append(el("small", null, "accesa da " + durata(v.acceso)));
  }
  const ponti = el("div", "ponti");
  v.ponti.forEach(x => {
    const s = el("span", "via");
    let testo = x.ponte;
    if (x.salti === 0) {
      const q = qualita(x.snr, x.rssi);
      testo += q ? ": diretta, segnale " + q : ": diretta";
    } else if (x.salti > 0) {
      testo += ": passando da " + (x.salti === 1 ? "1 radio" : x.salti + " radio");
    }
    s.append(document.createTextNode(testo));
    if (x.eta >= 15 * 60) s.append(el("em", null, " (" + fa(x.eta) + ")"));
    if (x.snr !== null && x.snr !== undefined) {
      s.title = "Rapporto segnale/rumore " + numIt(x.snr) + " dB" + (x.rssi ? ", potenza " + x.rssi + " dBm" : "");
    }
    ponti.append(s);
  });
  if (pontiCheGestiscono(v).length) {
    const g = el("button", "bottone secondario piccolo gestisci", "Gestisci da lontano");
    g.type = "button";
    g.addEventListener("click", () => apriGestioneRadio(v));
    ponti.append(g);
  }
  if (iconeAttive()) ponti.append(bottoneIcona(v.id, v.nome || "radio " + v.id, false));
  li.append(nome, bat, quando, ponti);
  return li;
}

function disegnaRadioRete() {
  const {vecchi, canali, raggiunti} = radioDellaRete();
  const radio = radioDellaSquadra();
  const attive = radio.filter(v => v.eta < 3600).length;
  const silenziose = radio.filter(v => v.eta >= SILENZIO_S).length;
  const scariche = radio.filter(v => problemi(v).some(x => x[0].startsWith("batteria"))).length;
  const chiacchierone = radio.filter(v => problemi(v).some(x => x[0].startsWith("trasmette")));
  if (!radio.length) {
    riassuntoRadio.textContent = raggiunti ? "Nessuna radio della squadra sentita finora." :
                                 "Nessun gateway raggiungibile: l'elenco delle radio arriva dai gateway.";
  } else {
    const parti = [attive + " sentit" + (attive === 1 ? "a" : "e") + " nell'ultima ora"];
    if (silenziose) parti.push(silenziose + " silenzios" + (silenziose === 1 ? "a" : "e") + " da più di 2 ore");
    riassuntoRadio.textContent = (radio.length === 1 ? "1 radio" : radio.length + " radio") +
      ": " + parti.join(", ") + "." + (scariche ? " " + (scariche === 1 ? "Una ha" : scariche + " hanno") +
      " la batteria quasi scarica." : "") + (chiacchierone.length ? " " + (chiacchierone.length === 1 ? "Una trasmette" :
      chiacchierone.length + " trasmettono") + " troppo spesso." : "");
  }
  avvisiRadio.replaceChildren();
  canali.forEach(c => {
    if (c.eta !== null && c.eta > 3600) return;
    if (c.occupato >= 25) {
      avvisiRadio.append(el("li", c.occupato >= 40 ? "grave" : null, "Canale radio " + (c.occupato >= 40 ? "saturo" : "affollato") +
        " vicino al gateway " + c.ponte + ": occupato il " + numIt(c.occupato) + "% del tempo. Le radio rimandano le " +
        "trasmissioni non urgenti (posizioni, batteria)."));
    }
  });
  chiacchierone.slice(0, 3).forEach(v => avvisiRadio.append(el("li", null, (v.nome || v.id) + " occupa la radio il " +
    numIt(v.trasmesso) + "% del tempo (di solito meno dell'1%) e rallenta le altre: probabilmente manda la posizione " +
    "troppo spesso. Controlla nell'app Meshtastic di quella radio: Impostazioni, Posizione.")));
  vecchi.forEach(n => avvisiRadio.append(el("li", null, "Il gateway " + n + " ha una versione di mesh2tak che non manda " +
    "l'elenco delle radio: aggiornalo sul Raspberry (dalla 1.6.0).")));
  const filtro = cercaRadio.value.trim().toLowerCase();
  const visibili = radio.filter(v => !filtro || [v.nome, v.nome_breve, v.id, v.modello, nomeModello(v.modello)]
    .some(x => x && String(x).toLowerCase().includes(filtro)));
  visibili.sort((a, b) => (problemi(b).length > 0) - (problemi(a).length > 0) || a.eta - b.eta);
  testaVoci.hidden = !visibili.length;
  if (visibili.length) {
    elencoRadio.replaceChildren(...visibili.map(disegnaVoce));
  } else {
    elencoRadio.replaceChildren(el("li", "voce vuota", radio.length ? "Nessuna radio corrisponde alla ricerca." :
      (raggiunti ? "Le radio compaiono appena trasmettono sul canale della squadra." : "")));
  }
}

function disegna() {
  if (!costruita) costruisciPagina();
  cacheRete = null;
  const attivo = document.activeElement;
  if (!(attivo && zonaNovita.contains(attivo) && ["INPUT", "SELECT", "TEXTAREA"].includes(attivo.tagName))) {
    const novita = disegnaNovita();
    if (novita) novita.id = "richieste";
    zonaNovita.replaceChildren(...(novita ? [novita] : []));
  }
  disegnaStatoRete();
  disegnaElencoRete();
  disegnaSchede();
  // sotto il titolo: l'esito dell'ultima azione e i problemi della pagina stessa
  const messaggi = [];
  if (avviso) { messaggi.push(avviso); avviso = ""; }
  if (erroreGiro === "sessione scaduta") {
    messaggi.push("La sessione è scaduta: ricarica la pagina.");
  } else if (riavvio) {
    messaggi.push("Authentik si sta riavviando per il lavoro in corso: la pagina torna da sola tra circa un minuto.");
  } else if (erroreGiro) {
    messaggi.push((ultimoElenco ? "Ultimo aggiornamento non riuscito" : "Elenco dei gateway non disponibile") +
                  " (" + erroreGiro + "): riprovo da solo.");
  }
  const messaggio = document.getElementById("messaggio");
  messaggio.textContent = messaggi.join(" ");
  messaggio.hidden = !messaggi.length;
  aggiornaAggiungi();
  aggiornaAvvisi();
  aggiornaAggiornamenti();
  disegnaRadioRete();
  const conta = {squadra: radioDellaSquadra().length};
  for (const [chiave, n] of Object.entries(conta)) bottoniSchede[chiave].lastChild.textContent = n ? String(n) : "";
  document.getElementById("aggiornato").textContent = "Aggiornata alle " + new Date().toLocaleTimeString("it-IT") + ", si aggiorna da sola";
  document.getElementById("versione").textContent = versione ? "rete-meshtastic " + versione : "";
}

function chiediConferma(p, d) {
  const dlg = document.getElementById("conferma");
  const g = d.gestione || {};
  let testo = "Mentre la pagina di gestione è aperta il gateway " + p.nome + " non porta le posizioni delle radio al TAK Server.";
  if (d.pausa && d.pausa.attiva) testo = "Il nodo è già gestito " + (d.pausa.chi || "da qualcuno") + " dalle " + oraDi(d.pausa.dal) + ". Aprendo anche tu, la pausa continua.";
  document.getElementById("conferma-testo").textContent = testo;
  document.getElementById("conferma-chiusura").textContent = durata(g.chiusura_s || 120);
  document.getElementById("conferma-massimo").textContent = durata((g.pausa_massima_min || 30) * 60);
  document.getElementById("apri").href = p.indirizzo;
  dlg.showModal();
}

document.getElementById("annulla").addEventListener("click", () => document.getElementById("conferma").close());
document.getElementById("togli-annulla").addEventListener("click", () => document.getElementById("togli").close());
document.getElementById("togli-conferma").addEventListener("click", async () => {
  const p = daTogliere;
  document.getElementById("togli").close();
  if (!p) return;
  try {
    await manda("rimuovi", {nome: p.nome});
  } catch (e) {
    avviso = "Non riuscito: " + e.message;
  }
  await giro();
});
document.getElementById("apri").addEventListener("click", () => document.getElementById("conferma").close());
document.getElementById("aggiorna-annulla").addEventListener("click", () => document.getElementById("aggiorna").close());
document.getElementById("aggiorna-conferma").addEventListener("click", async () => {
  const o = daAggiornare;
  document.getElementById("aggiorna").close();
  if (!o) return;
  const reimposta = document.getElementById("aggiorna-reimposta").checked;
  try {
    const d = await manda(o.azione, o.azione === "aggiorna-server" ? {versione: o.versione} :
                                    {versione: o.versione, nome: o.nomi.length === 1 ? o.nomi[0] : "tutti", reimposta});
    if (d.messaggio) avviso = d.messaggio;
  } catch (e) {
    avviso = "Non riuscito: " + e.message;
  }
  await giro();
});

