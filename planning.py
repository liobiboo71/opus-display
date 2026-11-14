#!/usr/bin/env python3
"""Genere planning.json : absences par prenom, lues dans OOTI, pour l'ecran de l'open space.

Donnees volontairement minimales (le site est public) : prenoms et jours d'absence.
Aucun motif, aucun type de conge. Le teletravail n'est pas compte comme une absence.

Modes :
  run       : ecrit planning.json (mode par defaut)
  personnes : liste les collaborateurs OOTI (pour trouver les identifiants a mettre dans config.json)
"""
import argparse, json, os, sys, time, unicodedata, urllib.error, urllib.parse, urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

OOTI_BASE = os.environ.get("OOTI_BASE_URL", "https://api.ooti.co")
TR = "/api/v1/timelogs/timeoff-requests"


def http(method, url, headers=None, body=None, retries=3):
    data = json.dumps(body).encode() if body is not None else None
    # Meme signature que la synchro PayFit : Cloudflare bloque "Python-urllib"
    h = {"Accept": "application/json", "User-Agent": "payfit-ooti-sync/1.0 (Groupe OPUS)", **(headers or {})}
    if data:
        h["Content-Type"] = "application/json"
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:300]
            if msg.lstrip().startswith("<"):
                msg = "(page HTML renvoyee par le serveur)"
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 * attempt)
                continue
            raise RuntimeError(f"{method} {url} -> HTTP {e.code} : {msg}")
        except urllib.error.URLError as e:
            if attempt < retries:
                time.sleep(2 * attempt)
                continue
            raise RuntimeError(f"{method} {url} -> reseau : {e}")


def parse_date(s):
    """Accepte AAAA-MM-JJ, JJ-MM-AAAA ou JJ/MM/AAAA."""
    if not s:
        return None
    s = str(s)[:10]
    if s[4:5] == "-":
        y, m, d = s.split("-")
    else:
        d, m, y = s.replace("/", "-").split("-")
    return date(int(y), int(m), int(d))


def to_ooti(d):
    return d.strftime("%d-%m-%Y")


def ooti_auth():
    user, pwd = os.environ.get("OOTI_USER"), os.environ.get("OOTI_PASSWORD")
    if not (user and pwd):
        raise RuntimeError("Identifiants OOTI absents (secrets OOTI_USER et OOTI_PASSWORD).")
    r = http("POST", f"{OOTI_BASE}/api/v1/token-auth/", body={"username": user, "password": pwd})
    access = r.get("access") or r.get("token")
    if not access:
        raise RuntimeError("Connexion OOTI : pas de jeton dans la reponse.")
    return {"Authorization": f"JWT {access}"}


def results(data):
    return data if isinstance(data, list) else data.get("results", [])


def list_all(h, path, params=None):
    out, page = [], 1
    while page <= 200:
        q = {**(params or {}), "page": page}
        data = http("GET", f"{OOTI_BASE}{path}?" + urllib.parse.urlencode(q), h)
        out += results(data)
        if isinstance(data, list) or not data.get("next"):
            return out
        page += 1
    return out


def ooti_timeoffs(h, team, begin, end):
    """Conges d'une equipe. Meme logique de repli que la synchro (OOTI refuse parfois les filtres)."""
    variants = [(f"{TR}/list/{team}/", {"start_date": to_ooti(begin), "end_date": to_ooti(end)}),
                (f"{TR}/list/{team}/", {})]
    last = None
    for path, params in variants:
        try:
            rows = list_all(h, path, params)
            return [r for r in rows
                    if (parse_date(r.get("end_date")) or date.max) >= begin
                    and (parse_date(r.get("start_date")) or date.min) <= end]
        except RuntimeError as e:
            last = e
    raise last


def plain(s):
    return unicodedata.normalize("NFD", str(s or "")).encode("ascii", "ignore").decode().lower()


def day_marks(r, lo, hi):
    """Jours ouvres d'un conge dans [lo, hi] -> J (journee), M (matin), A (apres-midi).
    Debut a midi (start_date_is_half) = absent l'apres-midi du premier jour.
    Fin a midi (end_date_is_half) = absent le matin du dernier jour."""
    s, e = parse_date(r.get("start_date")), parse_date(r.get("end_date"))
    if not s or not e:
        return {}
    sh, eh = bool(r.get("start_date_is_half")), bool(r.get("end_date_is_half"))
    out, d = {}, s
    while d <= e:
        if d.weekday() < 5 and lo <= d <= hi:
            if s == e:
                mark = "A" if (sh and not eh) else "M" if (eh and not sh) else "J"
            elif d == s:
                mark = "A" if sh else "J"
            elif d == e:
                mark = "M" if eh else "J"
            else:
                mark = "J"
            out[d.isoformat()] = mark
        d += timedelta(days=1)
    return out


def merge(a, b):
    return a if a == b else "J"  # matin + apres-midi = journee


def build(cfg, h, today):
    monday = today - timedelta(days=today.weekday())
    lo, hi = monday, monday + timedelta(days=cfg.get("jours_apres", 27))
    names = {int(v): k for k, v in cfg["personnes"].items()}
    skip_types = set(cfg.get("exclure_types", []))
    absences, n_rows = {}, 0
    for team in cfg["equipes"]:
        for r in ooti_timeoffs(h, team, lo, hi):
            who = names.get(r.get("orguser"))
            if who is None or r.get("is_validated") != "Y":
                continue
            if r.get("type") in skip_types or "teletravail" in plain(r.get("type_display_fr")):
                continue
            n_rows += 1
            for day, mark in day_marks(r, lo, hi).items():
                cur = absences.setdefault(who, {})
                cur[day] = merge(cur[day], mark) if day in cur else mark
    out = {"genere": datetime.now(ZoneInfo("Europe/Paris")).isoformat(timespec="seconds"),
           "personnes": list(cfg["personnes"].keys()),
           "absences": {k: dict(sorted(v.items())) for k, v in absences.items()}}
    return out, n_rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["run", "personnes"], default="run")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    ap.add_argument("--sortie", default="planning.json")
    args = ap.parse_args()
    cfg = json.load(open(args.config, encoding="utf-8"))
    h = ooti_auth()
    if args.mode == "personnes":
        org = cfg["organisation"]
        for team in cfg["equipes"]:
            print(f"\n===== Equipe {team} =====")
            for u in list_all(h, f"/api/v1/orgusers/list/{org}/", {"team": team}):
                print(json.dumps(u, ensure_ascii=False)[:200])
        return
    out, n = build(cfg, h, datetime.now(ZoneInfo("Europe/Paris")).date())
    os.makedirs(os.path.dirname(os.path.abspath(args.sortie)), exist_ok=True)
    with open(args.sortie, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"{n} conges retenus, {len(out['absences'])} personnes avec au moins une absence.")
    for who, days in out["absences"].items():
        print(f"  {who} : " + ", ".join(f"{d[8:]}/{d[5:7]}={m}" for d, m in days.items()))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERREUR :", e)
        sys.exit(1)
