// ---- icone sulla mappa (dalla 1.11.0): per ogni radio e ogni ponte, scelte qui e consegnate ai ponti dal server
let iconeMappa = null, caricoIcone = false, iconaAperta = null;

async function caricaIconeMappa() {
  if (iconeMappa || caricoIcone) return;
  caricoIcone = true;
  try {
    const r = await fetch("icone-mappa.json", {cache: "no-store", redirect: "manual", credentials: "same-origin"});
    if (r.ok) {
      const d = await r.json();
      if (d && Array.isArray(d.icone)) iconeMappa = {lista: d.icone, per: new Map(d.icone.map(x => [x.chiave, x]))};
    } else {
      await r.text().catch(() => "");
    }
  } catch (e) {
    // server di prima della 1.11.0: la scelta delle icone non c'e'
  } finally {
    caricoIcone = false;
  }
}

function iconeAttive() {
  return !!(iconeMappa && gestione && gestione.icone && gestione.icone.scelte);
}

function sceltaIcona(id) {
  const s = iconeAttive() ? gestione.icone.scelte[id] : null;
  return s && iconeMappa.per.get(s.icona) ? s : null;
}

function figuraIcona(voce) {
  if (voce && voce.pallino) return el("span", "figura-squadra");     // predefinita delle radio: colore della squadra
  if (!voce || !voce.figura) return el("span", "figura-vuota");
  const img = el("img", "figura-icona");
  img.src = voce.figura;
  img.alt = "";
  return img;
}

function bottoneIcona(id, chi, ponte) {
  const s = sceltaIcona(id);
  const voce = s ? iconeMappa.per.get(s.icona) : (ponte ? iconeMappa.per.get("traliccio") : null);
  const b = el("button", "bottone secondario piccolo icona-mappa");
  b.type = "button";
  if (voce && voce.figura) b.append(figuraIcona(voce));
  b.append(document.createTextNode(s ? "Icona: " + voce.nome : "Icona sulla mappa"));
  b.addEventListener("click", () => apriIcone(id, chi, ponte));
  return b;
}

function apriIcone(id, chi, ponte) {
  iconaAperta = {id, chi, ponte};
  document.getElementById("icone-titolo").textContent = "Icona di " + chi + " sulla mappa";
  document.getElementById("icone-esito").textContent = "";
  document.getElementById("icone-spiega").textContent = "Come compare " + (ponte ? "il gateway" : "la radio") +
    " su ATAK e TAK Aware. Le app che non hanno queste icone mostrano il simbolo militare più vicino" +
    (ponte ? "." : "; le radio abbinate a un telefono con l'app TAK compaiono come le mostra l'app.");
  disegnaIcone();
  document.getElementById("icone").showModal();
}

function tesseraIcona(chiave, nome, voce, attuale) {
  const b = el("button", "tessera-icona");
  b.type = "button";
  b.setAttribute("aria-pressed", String(chiave === attuale));
  b.append(figuraIcona(voce), el("span", null, nome));
  b.addEventListener("click", () => scegliIcona(chiave));
  return b;
}

function disegnaIcone() {
  const a = iconaAperta;
  if (!a || !iconeAttive()) return;
  const s = sceltaIcona(a.id);
  const attuale = s ? s.icona : "";
  const zona = [];
  const prima = el("div", "griglia-icone");
  prima.append(tesseraIcona("", a.ponte ? "Traliccio (predefinita)" : "Predefinita",
                            a.ponte ? iconeMappa.per.get("traliccio") : {pallino: true}, attuale));
  zona.push(prima);
  const gruppi = new Map();
  for (const voce of iconeMappa.lista) {
    if (a.ponte && voce.chiave === "traliccio") continue;      // e' gia' la predefinita
    if (!gruppi.has(voce.gruppo)) gruppi.set(voce.gruppo, []);
    gruppi.get(voce.gruppo).push(voce);
  }
  for (const [gruppo, voci] of gruppi) {
    const griglia = el("div", "griglia-icone");
    voci.forEach(voce => griglia.append(tesseraIcona(voce.chiave, voce.nome, voce, attuale)));
    zona.push(el("h3", null, gruppo), griglia);
  }
  document.getElementById("icone-scelte").replaceChildren(...zona);
  disegnaIconePonti();
}

function disegnaIconePonti() {
  const p = (gestione && gestione.icone && gestione.icone.ponti) || {};
  const gia = [], attesa = [], vecchi = [];
  for (const q of elenco) {
    const s = p[q.nome];
    (s === "aggiornato" ? gia : (s === "vecchio" ? vecchi : attesa)).push(q.nome);
  }
  const parti = [];
  if (gia.length) parti.push("Le scelte di questa pagina sono già in uso su: " + gia.join(", ") + ".");
  if (attesa.length) parti.push("Arrivano entro un paio di minuti a: " + attesa.join(", ") + " (se acces" +
                                (attesa.length === 1 ? "o" : "i") + ").");
  if (vecchi.length) parti.push("Da aggiornare alla versione " + versione + " per usarle: " + vecchi.join(", ") + ".");
  document.getElementById("icone-ponti").textContent = parti.join(" ");
}

async function scegliIcona(chiave) {
  const a = iconaAperta;
  if (!a) return;
  const esito = document.getElementById("icone-esito");
  document.querySelectorAll("#icone-scelte button").forEach(x => { x.disabled = true; });
  esito.textContent = "Salvo...";
  try {
    const d = await manda("icona", {radio: a.id, icona: chiave});
    if (gestione && gestione.icone) {     // la copia della pagina, finche' non arriva lo stato nuovo
      if (chiave) gestione.icone.scelte[a.id] = {icona: chiave, da: gestione.utente, quando: Date.now() / 1000};
      else delete gestione.icone.scelte[a.id];
      if (d.versione && d.versione !== gestione.icone.versione) {
        gestione.icone.versione = d.versione;
        for (const n of Object.keys(gestione.icone.ponti || {})) {
          if (gestione.icone.ponti[n] === "aggiornato") gestione.icone.ponti[n] = "attesa";
        }
      }
    }
    esito.textContent = d.messaggio || "Salvata.";
    disegna();
  } catch (e) {
    esito.textContent = "Non salvata: " + e.message;
  }
  disegnaIcone();
}

document.getElementById("icone-chiudi").addEventListener("click", () => document.getElementById("icone").close());
document.getElementById("icone").addEventListener("close", () => { iconaAperta = null; });

