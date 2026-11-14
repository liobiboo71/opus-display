// Fonction Netlify : messages envoyés à l'écran de l'open space.
// GET    : liste des messages encore valides (lue par l'écran et par la page d'envoi)
// POST   : ajoute un message   { pin, text, heures }
// DELETE : supprime un message { pin, id }
// Le PIN est écrit dans pin.mjs au moment du déploiement (secret GitHub MESSAGE_PIN).

import PIN from "./pin.mjs";

const COLS = 18, ROWS = 4, MAX_ACTIFS = 8, MAX_HEURES = 72;

const clean = s => String(s).normalize("NFD").replace(/[̀-ͯ]/g, "").toUpperCase();

// Même découpage que l'écran. Une pastille de couleur [R][O][Y][G][B] compte pour 1 case.
// Renvoie les lignes, ou null si le texte ne tient pas.
export function wrap(text) {
  const words = clean(text).split(/\s+/).filter(Boolean).map(w => w.match(/\[[ROYGB]\]|[\s\S]/g));
  const lines = [0];
  for (const w of words) {
    if (w.length > COLS) return null;
    const cur = lines[lines.length - 1];
    if ((cur ? cur + 1 : 0) + w.length <= COLS) lines[lines.length - 1] = (cur ? cur + 1 : 0) + w.length;
    else lines.push(w.length);
  }
  return lines.length <= ROWS ? lines : null;
}

const json = (data, status = 200) => new Response(JSON.stringify(data), {
  status,
  headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
});

function sameString(a, b) {
  a = String(a); b = String(b);
  let d = a.length ^ b.length;
  for (let i = 0; i < Math.max(a.length, b.length); i++) d |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  return d === 0;
}

// store : { get(key,{type}), setJSON(key,value) }. Injecté pour pouvoir tester sans Netlify.
export async function core(req, store, opts = {}) {
  const now = opts.now ?? Date.now();
  const pin = opts.pin ?? PIN;
  const pause = opts.pause ?? (ms => new Promise(r => setTimeout(r, ms)));

  const lire = async () => {
    const l = (await store.get("liste", { type: "json" })) || [];
    return l.filter(m => m.until > now);
  };

  if (req.method === "GET") return json({ now, messages: await lire() });

  if (req.method !== "POST" && req.method !== "DELETE") return json({ erreur: "Méthode non autorisée." }, 405);

  let body;
  try { body = await req.json(); } catch { return json({ erreur: "Requête illisible." }, 400); }

  if (!pin) return json({ erreur: "Le code PIN n'est pas configuré sur le serveur." }, 503);

  // Anti-devinette : 5 erreurs, puis blocage de 10 minutes.
  const essais = (await store.get("essais", { type: "json" })) || { n: 0, jusqua: 0 };
  if (essais.jusqua > now) return json({ erreur: "Trop d'essais. Réessayez dans quelques minutes." }, 429);
  if (!sameString(body.pin ?? "", pin)) {
    const n = (essais.n || 0) + 1;
    await store.setJSON("essais", n >= 5 ? { n: 0, jusqua: now + 10 * 60000 } : { n, jusqua: 0 });
    await pause(800);
    return json({ erreur: "Code PIN incorrect." }, 401);
  }
  if (essais.n) await store.setJSON("essais", { n: 0, jusqua: 0 });

  let liste = await lire();

  if (req.method === "DELETE") {
    liste = liste.filter(m => m.id !== body.id);
    await store.setJSON("liste", liste);
    return json({ messages: liste });
  }

  const text = String(body.text ?? "").replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\s+/g, " ").trim();
  if (!text) return json({ erreur: "Le message est vide." }, 400);
  if (text.length > 400) return json({ erreur: "Message trop long." }, 400);
  if (!wrap(text)) return json({ erreur: "Le message ne tient pas sur 4 lignes de 18 lettres. Raccourcissez-le." }, 400);

  const heures = Math.min(Math.max(Number(body.heures) || 4, 0.25), MAX_HEURES);
  if (liste.length >= MAX_ACTIFS) return json({ erreur: "Trop de messages actifs. Supprimez-en un d'abord." }, 409);

  liste.push({ id: now.toString(36) + Math.random().toString(36).slice(2, 6), text, at: now, until: now + Math.round(heures * 3600000) });
  await store.setJSON("liste", liste);
  return json({ messages: liste }, 201);
}

export default async (req) => {
  const { getStore } = await import("@netlify/blobs");
  return core(req, getStore({ name: "ecran", consistency: "strong" }));
};
