#!/usr/bin/env python3
"""Pipeline de ofertas de trabajo remoto (solo librería estándar de Python).

Subcomandos:
  check          Prueba cada fuente sin tocar la base de datos.
  fetch          Descarga, filtra, deduplica y devuelve un lote de ofertas pendientes (JSON).
  show ID        Muestra una oferta completa (JSON).
  list           Lista ofertas (JSON) con filtros; --need-verify / --need-letter para el agente.
  save-scores F  Guarda las puntuaciones del evaluador (JSON).
  save-verdicts F Guarda las verificaciones (JSON).
  status ID EST  Cambia el estado de una oferta (aplicada, entrevista...).
  report         Regenera ofertas.md.
  notify         Envía un resumen por Telegram (TG_TOKEN y TG_CHAT).
  stats          Muestra el embudo.

stdout = JSON para que lo lea el agente. Avisos y errores van a stderr.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("JOBS_CONFIG", ROOT / "config.json"))
DB_PATH = Path(os.environ.get("JOBS_DB", ROOT / "data" / "jobs.db"))
REPORT_PATH = Path(os.environ.get("JOBS_REPORT", ROOT / "ofertas.md"))
LETTERS_DIR = Path(os.environ.get("JOBS_LETTERS", ROOT / "cartas"))
UA = "job-agent/2.0 (personal job search)"

STATUSES = ("nueva", "interesante", "aplicada", "entrevista", "oferta", "rechazada", "descartada")
IN_PROGRESS = ("aplicada", "entrevista", "oferta")

DEFAULTS = {
    "keywords": ["python"],
    "require_title_any": [],
    "exclude_title": ["intern", "internship", "junior", "trainee"],
    "exclude_text": [
        "us only", "usa only", "u.s. only", "us citizens", "us citizen",
        "us-based only", "must reside in the us", "must be located in the us",
        "security clearance", "on-site only", "onsite only", "in-office",
    ],
    "boost_words": ["senior", "lead", "staff", "principal"],
    "geo_allow": [
        "worldwide", "anywhere", "global", "europe", "emea", "eu", "european union",
        "spain", "españa", "cet", "cest", "utc+1", "gmt+1",
    ],
    "geo_block": [
        "us", "usa", "u.s.", "united states", "america", "americas", "north america",
        "canada", "uk", "united kingdom", "latam", "latin america", "apac", "asia",
        "australia", "india", "brazil", "mexico", "philippines", "africa",
    ],
    "min_salary_eur_year": 0,
    "max_age_days": 30,
    "max_batch": 30,
    "text_chars": 1200,
    "min_score_to_save": 7,
    "top_score": 8,
    "remotive_category": "software-dev",
    "wwr_feeds": ["remote-programming-jobs"],
    "jobicy_geo": "",
    "sources": {
        "remotive": True, "remoteok": True, "weworkremotely": True, "arbeitnow": True,
        "himalayas": True, "jobicy": True, "workingnomads": True,
    },
}

INJECTION_RX = re.compile(
    r"ignore (all |any |the )?(previous|prior|above|earlier)|disregard .{0,40}instructions|"
    r"system prompt|you are (now )?(an? )?(ai|assistant|llm|language model)|as an ai (agent|assistant|model)|"
    r"(ai|llm) (agents?|assistants?),? (must|should|please)|score (this|me) (a )?(10|ten)",
    re.I,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, dupkey TEXT, source TEXT, url TEXT, title TEXT, company TEXT,
  location TEXT, salary TEXT, posted TEXT, text TEXT, first_seen TEXT,
  pre_score INTEGER DEFAULT 0, state TEXT DEFAULT 'pending', filter_reason TEXT,
  score INTEGER, reason TEXT, flags TEXT, verified INTEGER DEFAULT 0, verdict TEXT,
  notes TEXT, status TEXT DEFAULT 'nueva', notified INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_jobs_dupkey ON jobs(dupkey);
"""


# ----------------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------------
def log(*a):
    print(*a, file=sys.stderr)


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False))


def now_utc():
    return datetime.now(timezone.utc)


def default_config():
    return copy.deepcopy(DEFAULTS)


def load_config():
    cfg = default_config()
    if CONFIG_PATH.exists():
        user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        for k, v in user.items():
            if k.startswith("_"):
                continue
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def sha(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:10]


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


TAG_RE = re.compile(r"<[^>]+>")


def clean(s, limit=4000):
    s = html.unescape(str(s or ""))
    s = TAG_RE.sub(" ", s)
    s = html.unescape(s)
    s = re.sub(r"[\x00-\x08\x0b-\x1f]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


def canon_url(url):
    url = (url or "").strip()
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query)
         if not k.lower().startswith(("utm_", "ref", "fbclid", "gclid"))]
    return urllib.parse.urlunsplit(
        (p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), urllib.parse.urlencode(q), ""))


def build_rx(words):
    words = [w for w in (words or []) if w]
    if not words:
        return None
    return re.compile("|".join(r"(?<!\w)" + re.escape(w) + r"(?!\w)" for w in words), re.I)


def parse_date(v):
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().isdigit()):
            x = float(v)
            if x > 1e11:
                x /= 1000.0
            return datetime.fromtimestamp(x, timezone.utc)
        s = str(v).strip()
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            d = parsedate_to_datetime(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:
        return None


def _num(raw):
    raw = raw.strip(".,")
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw):
        return float(re.sub(r"[.,]", "", raw))
    if re.fullmatch(r"\d+(?:[.,]\d+)?", raw):
        return float(raw.replace(",", "."))
    return None


def parse_salary(s):
    """Devuelve (min, max) anuales aproximados en EUR, o None si no se puede leer.
    Es una estimación: sirve para descartar salarios claramente bajos, no para decidir."""
    if not s:
        return None
    t = str(s).lower()
    has_k = bool(re.search(r"\d\s*k\b", t))
    hourly = bool(re.search(r"hour|/\s*h\b|/\s*hr|per hr", t))
    monthly = bool(re.search(r"month|/\s*mo\b", t))
    factor = 0.92 if ("$" in t or "usd" in t) else 1.15 if ("£" in t or "gbp" in t) else 1.0
    vals = []
    for m in re.finditer(r"\d[\d.,]*", t):
        v = _num(m.group(0))
        if v is None:
            continue
        if v < 1000:
            if has_k:
                v *= 1000
            elif hourly:
                v *= 2000
            elif monthly:
                v *= 12
            else:
                continue
        elif monthly:
            v *= 12
        vals.append(v * factor)
    vals = [v for v in vals if v >= 1000]
    return (min(vals), max(vals)) if vals else None


def fmt_salary(lo, hi, cur=""):
    try:
        lo = int(float(lo or 0))
        hi = int(float(hi or 0))
    except (TypeError, ValueError):
        return ""
    if not lo and not hi:
        return ""
    rng = f"{lo}-{hi}" if lo and hi and lo != hi else str(lo or hi)
    return f"{rng} {cur}".strip()


def tags_text(v):
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)):
        return " ".join(str(x) for x in v)
    return ""


# ----------------------------------------------------------------------------
# Fuentes
# ----------------------------------------------------------------------------
def http_get(url, timeout=25, retries=2):
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept": "application/json, application/rss+xml, */*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def mk(source, title, company, url, location="", salary="", text="", posted=None):
    d = parse_date(posted)
    return {
        "source": source, "title": clean(title, 200), "company": clean(company, 120),
        "url": (url or "").strip(), "location": clean(location, 200),
        "salary": clean(salary, 80), "text": clean(text, 4000),
        "posted": d.isoformat() if d else "",
    }


def src_remotive(cfg):
    url = "https://remotive.com/api/remote-jobs?limit=200"
    if cfg.get("remotive_category"):
        url += "&category=" + urllib.parse.quote(cfg["remotive_category"])
    d = json.loads(http_get(url))
    return [mk("remotive", j.get("title"), j.get("company_name"), j.get("url"),
               j.get("candidate_required_location"), j.get("salary"),
               j.get("description"), j.get("publication_date")) for j in d.get("jobs", [])]


def src_remoteok(cfg):
    out = []
    for j in json.loads(http_get("https://remoteok.com/api")):
        if not isinstance(j, dict) or not j.get("position"):
            continue
        out.append(mk("remoteok", j["position"], j.get("company"), j.get("url") or j.get("apply_url"),
                      j.get("location"), fmt_salary(j.get("salary_min"), j.get("salary_max"), "USD"),
                      (j.get("description") or "") + " " + tags_text(j.get("tags")),
                      j.get("date") or j.get("epoch")))
    return out


def src_wwr(cfg):
    out = []
    for feed in cfg.get("wwr_feeds") or []:
        root = ET.fromstring(http_get(f"https://weworkremotely.com/categories/{feed}.rss"))
        for it in root.iter("item"):
            title = it.findtext("title") or ""
            company, _, role = title.partition(": ")
            if not role:
                company, role = "", title
            out.append(mk("weworkremotely", role, company, it.findtext("link"),
                          it.findtext("region") or "", "", it.findtext("description"),
                          it.findtext("pubDate")))
    return out


def src_arbeitnow(cfg):
    d = json.loads(http_get("https://www.arbeitnow.com/api/job-board-api"))
    out = []
    for j in d.get("data", []):
        if not j.get("remote"):
            continue
        out.append(mk("arbeitnow", j.get("title"), j.get("company_name"), j.get("url"),
                      "Remote - " + (j.get("location") or "Europe"), "", j.get("description"),
                      j.get("created_at")))
    return out


def src_himalayas(cfg):
    d = json.loads(http_get("https://himalayas.app/jobs/api?limit=100"))
    out = []
    for j in d.get("jobs", []):
        locs = j.get("locationRestrictions") or []
        out.append(mk("himalayas", j.get("title"), j.get("companyName"),
                      j.get("applicationLink") or j.get("guid"), ", ".join(map(str, locs)),
                      fmt_salary(j.get("minSalary"), j.get("maxSalary"), j.get("currency") or ""),
                      j.get("description") or j.get("excerpt"), j.get("pubDate")))
    return out


def src_jobicy(cfg):
    out = []
    for kw in (cfg.get("keywords") or [])[:3]:
        url = f"https://jobicy.com/api/v2/remote-jobs?count=50&tag={urllib.parse.quote(kw)}"
        if cfg.get("jobicy_geo"):
            url += "&geo=" + urllib.parse.quote(cfg["jobicy_geo"])
        for j in json.loads(http_get(url)).get("jobs", []):
            out.append(mk("jobicy", j.get("jobTitle"), j.get("companyName"), j.get("url"),
                          tags_text(j.get("jobGeo")),
                          fmt_salary(j.get("annualSalaryMin"), j.get("annualSalaryMax"),
                                     j.get("salaryCurrency") or ""),
                          j.get("jobDescription") or j.get("jobExcerpt"), j.get("pubDate")))
    return out


def src_workingnomads(cfg):
    out = []
    for j in json.loads(http_get("https://www.workingnomads.com/api/exposed_jobs/")):
        out.append(mk("workingnomads", j.get("title"), j.get("company_name"), j.get("url"),
                      j.get("location") or "", "",
                      (j.get("description") or "") + " " + tags_text(j.get("tags")),
                      j.get("pub_date")))
    return out


SOURCES = {
    "remotive": src_remotive, "remoteok": src_remoteok, "weworkremotely": src_wwr,
    "arbeitnow": src_arbeitnow, "himalayas": src_himalayas, "jobicy": src_jobicy,
    "workingnomads": src_workingnomads,
}


def fetch_all(cfg):
    jobs, errors, counts = [], {}, {}
    for name, fn in SOURCES.items():
        if not cfg["sources"].get(name, False):
            continue
        try:
            got = fn(cfg)
            counts[name] = len(got)
            jobs.extend(got)
        except Exception as e:  # noqa: BLE001
            errors[name] = f"{type(e).__name__}: {e}"[:200]
    return jobs, errors, counts


# ----------------------------------------------------------------------------
# Filtros y pre-puntuación
# ----------------------------------------------------------------------------
def evaluate(job, cfg, now=None):
    """Devuelve dict(ok, reason, pre_score, flags). Todo determinista y sin LLM."""
    now = now or now_utc()
    title, loc, text = job["title"], job["location"], job["text"]

    def rej(reason):
        return {"ok": False, "reason": reason, "pre_score": 0, "flags": []}

    if not job["url"] or not title:
        return rej("incompleta")
    if INJECTION_RX.search(f"{title} {loc} {text}"):
        return rej("posible_inyeccion")

    posted = parse_date(job["posted"])
    age = None
    if posted:
        age = (now - posted).days
        if age > cfg["max_age_days"]:
            return rej("antigua")

    rx = build_rx(cfg["exclude_title"])
    if rx and rx.search(title):
        return rej("titulo_excluido")

    rxk = build_rx(cfg["keywords"])
    title_hits = len({m.lower() for m in rxk.findall(title)}) if rxk else 0
    text_hits = len({m.lower() for m in rxk.findall(text)}) if rxk else 0
    if rxk and not (title_hits or text_hits):
        return rej("sin_keywords")

    rxr = build_rx(cfg.get("require_title_any"))
    if rxr and not rxr.search(title):
        return rej("titulo_no_relevante")

    rxe = build_rx(cfg["exclude_text"])
    if rxe and rxe.search(f"{loc} {text}"):
        return rej("texto_excluido")

    flags = []
    allow_rx, block_rx = build_rx(cfg["geo_allow"]), build_rx(cfg["geo_block"])
    geo_explicit = False
    if loc:
        geo_explicit = bool(allow_rx and allow_rx.search(loc))
        if block_rx and block_rx.search(loc) and not geo_explicit:
            return rej("geo")
    else:
        flags.append("geo_desconocida")

    sal = parse_salary(job["salary"])
    if sal and cfg["min_salary_eur_year"] and sal[1] < cfg["min_salary_eur_year"]:
        return rej("salario_bajo")
    if not sal:
        flags.append("sin_salario")

    pre = min(title_hits * 15, 45) + min(text_hits * 3, 15)
    pre += 15 if geo_explicit else 0
    pre += 5 if sal else 0
    if age is not None:
        pre += int(20 * max(0, cfg["max_age_days"] - age) / max(1, cfg["max_age_days"]))
    else:
        pre += 5
    rxb = build_rx(cfg.get("boost_words"))
    if rxb:
        pre += min(len({m.lower() for m in rxb.findall(title)}) * 10, 20)
    return {"ok": True, "reason": "", "pre_score": min(pre, 100), "flags": flags}


# ----------------------------------------------------------------------------
# Base de datos
# ----------------------------------------------------------------------------
def connect(path=None):
    path = Path(path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _insert(conn, j, jid, dupkey, state, reason, pre, now_s):
    conn.execute(
        "INSERT INTO jobs (id,dupkey,source,url,title,company,location,salary,posted,text,"
        "first_seen,pre_score,state,filter_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (jid, dupkey, j["source"], j["url"], j["title"], j["company"], j["location"], j["salary"],
         j["posted"], j["text"] if state == "pending" else "", now_s, pre, state, reason))


def ingest(conn, jobs, cfg, now=None):
    now = now or now_utc()
    now_s = now.isoformat()
    stats = {"descargadas": len(jobs), "duplicadas": 0, "filtradas": 0, "nuevas": 0, "motivos": {}}
    seen_run = set()
    for j in jobs:
        jid = sha(canon_url(j["url"])) if j["url"] else sha(j["title"] + j["company"])
        if jid in seen_run or conn.execute("SELECT 1 FROM jobs WHERE id=?", (jid,)).fetchone():
            stats["duplicadas"] += 1
            continue
        seen_run.add(jid)
        ev = evaluate(j, cfg, now)
        if not ev["ok"]:
            _insert(conn, j, jid, jid, "filtered", ev["reason"], 0, now_s)
            stats["filtradas"] += 1
            stats["motivos"][ev["reason"]] = stats["motivos"].get(ev["reason"], 0) + 1
            continue
        # Duplicado entre portales: misma empresa y mismo título. Solo se compara con
        # ofertas que superaron los filtros, para que una versión "solo US" no tape la de Europa.
        dk = sha(norm(j["company"]) + "|" + norm(j["title"])) if j["company"] else jid
        if j["company"] and conn.execute(
                "SELECT 1 FROM jobs WHERE dupkey=? AND state!='filtered'", (dk,)).fetchone():
            _insert(conn, j, jid, jid, "filtered", "duplicada_otra_fuente", 0, now_s)
            stats["duplicadas"] += 1
            continue
        _insert(conn, j, jid, dk, "pending", "", ev["pre_score"], now_s)
        stats["nuevas"] += 1
    return stats


def expire_pending(conn, cfg, now=None):
    limit = ((now or now_utc()) - timedelta(days=cfg["max_age_days"])).isoformat()
    conn.execute("UPDATE jobs SET state='filtered', filter_reason='caducada', text='' "
                 "WHERE state='pending' AND first_seen < ?", (limit,))


def pending_batch(conn, cfg):
    rows = conn.execute("SELECT * FROM jobs WHERE state='pending' "
                        "ORDER BY pre_score DESC, first_seen DESC LIMIT ?", (cfg["max_batch"],)).fetchall()
    return [{
        "id": r["id"], "title": r["title"], "company": r["company"], "location": r["location"],
        "salary": r["salary"], "posted": r["posted"][:10], "pre_score": r["pre_score"],
        "text": (r["text"] or "")[:cfg["text_chars"]],
    } for r in rows]


def resolve_id(conn, prefix):
    prefix = (prefix or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{4,10}", prefix):
        return None
    rows = conn.execute("SELECT id FROM jobs WHERE id LIKE ?", (prefix + "%",)).fetchall()
    return rows[0]["id"] if len(rows) == 1 else None


def public_row(r, with_text=False):
    d = {
        "id": r["id"], "title": r["title"], "company": r["company"], "url": r["url"],
        "location": r["location"], "salary": r["salary"], "source": r["source"],
        "score": r["score"], "reason": r["reason"], "flags": json.loads(r["flags"] or "[]"),
        "verdict": r["verdict"], "status": r["status"], "notes": r["notes"],
        "posted": (r["posted"] or "")[:10],
    }
    if with_text:
        d["text"] = (r["text"] or "")[:2500]
    return d


def apply_scores(conn, cfg, items):
    saved, errors, kept = 0, [], []
    if isinstance(items, dict):
        items = items.get("scores") or items.get("jobs") or []
    for it in items:
        jid = resolve_id(conn, str(it.get("id", "")))
        if not jid:
            errors.append(f"id desconocido: {it.get('id')}")
            continue
        try:
            score = max(0, min(10, int(round(float(it["score"])))))
        except (KeyError, TypeError, ValueError):
            errors.append(f"puntuación inválida para {jid}")
            continue
        reason = clean(it.get("reason", ""), 300)
        flags = [clean(f, 40) for f in (it.get("flags") or [])][:6]
        keep = score >= cfg["min_score_to_save"]
        conn.execute("UPDATE jobs SET state='scored', score=?, reason=?, flags=?, "
                     "text=CASE WHEN ? THEN text ELSE '' END WHERE id=?",
                     (score, reason, json.dumps(flags, ensure_ascii=False), int(keep), jid))
        saved += 1
        if keep:
            kept.append(jid)
    conn.commit()
    return {"guardadas": saved, "recomendadas": kept, "errores": errors}


def apply_verdicts(conn, cfg, items):
    valid = {"confirmada", "dudosa", "descartar"}
    saved, errors = 0, []
    if isinstance(items, dict):
        items = items.get("verdicts") or items.get("jobs") or []
    for it in items:
        jid = resolve_id(conn, str(it.get("id", "")))
        verdict = str(it.get("verdict", "")).strip().lower()
        if not jid or verdict not in valid:
            errors.append(f"entrada inválida: {it.get('id')} / {it.get('verdict')}")
            continue
        adj = it.get("adjusted_score")
        try:
            adj = max(0, min(10, int(round(float(adj))))) if adj is not None else None
        except (TypeError, ValueError):
            adj = None
        conn.execute("UPDATE jobs SET verified=1, verdict=?, notes=?, score=COALESCE(?, score), "
                     "status=CASE WHEN ?='descartar' THEN 'descartada' ELSE status END WHERE id=?",
                     (verdict, clean(it.get("notes", ""), 400), adj, verdict, jid))
        saved += 1
    conn.commit()
    return {"guardadas": saved, "errores": errors}


def set_status(conn, ident, status, notes=None):
    if status not in STATUSES:
        raise ValueError(f"estado inválido: {status}. Válidos: {', '.join(STATUSES)}")
    jid = resolve_id(conn, ident)
    if not jid:
        raise ValueError(f"id no encontrado o ambiguo: {ident}")
    if notes:
        conn.execute("UPDATE jobs SET status=?, notes=? WHERE id=?", (status, clean(notes, 400), jid))
    else:
        conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, jid))
    conn.commit()
    return jid


# ----------------------------------------------------------------------------
# Informe y avisos
# ----------------------------------------------------------------------------
def _card(r):
    flags = json.loads(r["flags"] or "[]")
    lines = [f"### {r['score'] if r['score'] is not None else '–'} · {r['title']} — {r['company'] or 's/e'}"
             + (" ✔" if r["verified"] and r["verdict"] == "confirmada" else "")]
    meta = f"- `{r['id']}` · {r['status']} · {r['source']} · {(r['posted'] or '')[:10]}"
    lines.append(meta)
    lines.append(f"- 📍 {r['location'] or 'no especificado'} · 💶 {r['salary'] or 'no especificado'}")
    lines.append(f"- 🔗 {r['url']}")
    if r["reason"]:
        lines.append(f"- Por qué: {r['reason']}")
    if r["verdict"]:
        lines.append(f"- Verificación: {r['verdict']}" + (f" — {r['notes']}" if r["notes"] else ""))
    elif r["notes"]:
        lines.append(f"- Nota: {r['notes']}")
    if flags:
        lines.append(f"- ⚠️ {', '.join(flags)}")
    if (LETTERS_DIR / f"{r['id']}.md").exists():
        lines.append(f"- ✉️ carta: `cartas/{r['id']}.md`")
    return "\n".join(lines)


def render_report(conn, cfg):
    prog = conn.execute(
        f"SELECT * FROM jobs WHERE status IN ({','.join('?' * len(IN_PROGRESS))}) "
        "ORDER BY status, score DESC", IN_PROGRESS).fetchall()
    new = conn.execute(
        "SELECT * FROM jobs WHERE state='scored' AND status IN ('nueva','interesante') "
        "AND score>=? AND COALESCE(verdict,'')!='descartar' ORDER BY score DESC, first_seen DESC",
        (cfg["min_score_to_save"],)).fetchall()
    counts = {r["state"]: r["n"] for r in conn.execute("SELECT state, COUNT(*) n FROM jobs GROUP BY state")}
    closed = conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('rechazada','descartada')").fetchone()[0]
    out = [f"# Ofertas de trabajo remoto\n\n_Actualizado: {now_utc():%Y-%m-%d %H:%M} UTC_\n"]
    out.append("## En proceso\n")
    out.append("\n\n".join(_card(r) for r in prog) if prog else "_Nada todavía._")
    out.append(f"\n\n## Recomendadas ({len(new)})\n")
    out.append("\n\n".join(_card(r) for r in new) if new else "_Sin ofertas nuevas por encima del umbral._")
    out.append(f"\n\n---\nEmbudo: {sum(counts.values())} vistas · {counts.get('filtered', 0)} filtradas · "
               f"{counts.get('scored', 0)} puntuadas · {counts.get('pending', 0)} pendientes · "
               f"{closed} cerradas (rechazadas/descartadas).\n")
    return "\n".join(out)


def digest_text(rows):
    parts = [f"🔎 Ofertas nuevas ({len(rows)})\n"]
    for r in rows:
        mark = " ✔" if r["verdict"] == "confirmada" else ""
        parts.append(f"{r['score']}/10 · {r['title']} — {r['company'] or 's/e'}{mark}\n"
                     f"{r['location'] or 'ubicación n/e'} · {r['salary'] or 'salario n/e'}\n"
                     f"{r['url']}\n{r['reason'] or ''}\n")
    return "\n".join(parts)


def send_telegram(token, chat, text):
    chunks, cur = [], ""
    for block in text.split("\n\n"):
        if len(cur) + len(block) + 2 > 3800 and cur:
            chunks.append(cur)
            cur = ""
        cur += block + "\n\n"
    if cur.strip():
        chunks.append(cur)
    for c in chunks:
        data = urllib.parse.urlencode({"chat_id": chat, "text": c[:4000],
                                       "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                     data=data, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20):
            pass


# ----------------------------------------------------------------------------
# Comandos
# ----------------------------------------------------------------------------
def cmd_check(args):
    cfg = load_config()
    ok = 0
    for name, fn in SOURCES.items():
        if not cfg["sources"].get(name, False):
            print(f"-  {name}: desactivada")
            continue
        try:
            got = fn(cfg)
            sample = got[0]["title"] if got else "(vacía)"
            print(f"OK {name}: {len(got)} ofertas · ej: {sample[:60]}")
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"!! {name}: {type(e).__name__}: {str(e)[:120]}")
    print(f"{ok} fuentes funcionando")
    return 0 if ok else 1


def cmd_fetch(args):
    cfg = load_config()
    if args.keywords:
        extra = [k.strip() for k in args.keywords.split(",") if k.strip()]
        cfg["keywords"] = list(dict.fromkeys(cfg["keywords"] + extra))
    conn = connect()
    expire_pending(conn, cfg)
    jobs, errors, counts = fetch_all(cfg)
    stats = ingest(conn, jobs, cfg)
    batch = pending_batch(conn, cfg)
    total_pending = conn.execute("SELECT COUNT(*) FROM jobs WHERE state='pending'").fetchone()[0]
    if args.dry_run:
        conn.rollback()
    else:
        conn.commit()
    log(f"fuentes: {counts} · errores: {list(errors)} · {stats['nuevas']} nuevas")
    emit({"stats": stats, "por_fuente": counts, "errores_fuentes": errors,
          "pendientes_sin_lote": max(0, total_pending - len(batch)), "jobs": batch})
    return 0


def cmd_show(args):
    conn = connect()
    jid = resolve_id(conn, args.id)
    if not jid:
        log("id no encontrado")
        return 1
    emit(public_row(conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone(), with_text=True))
    return 0


def cmd_list(args):
    cfg = load_config()
    conn = connect()
    where, params = [], []
    if args.status:
        where.append("status=?")
        params.append(args.status)
    if args.min_score is not None:
        where.append("score>=?")
        params.append(args.min_score)
    if args.need_verify:
        where.append("state='scored' AND verified=0 AND status='nueva' AND score>=?")
        params.append(cfg["top_score"])
    if args.need_letter:
        where.append("state='scored' AND verified=1 AND verdict IN ('confirmada','dudosa') AND score>=?")
        params.append(cfg["top_score"])
    sql = "SELECT * FROM jobs" + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY score DESC, first_seen DESC LIMIT ?"
    rows = conn.execute(sql, params + [args.limit]).fetchall()
    if args.need_letter:
        rows = [r for r in rows if not (LETTERS_DIR / f"{r['id']}.md").exists()]
    emit([public_row(r) for r in rows])
    return 0


def _load(path):
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    return json.loads(raw)


def cmd_save_scores(args):
    res = apply_scores(connect(), load_config(), _load(args.file))
    emit(res)
    return 0 if not res["errores"] else 3


def cmd_save_verdicts(args):
    res = apply_verdicts(connect(), load_config(), _load(args.file))
    emit(res)
    return 0 if not res["errores"] else 3


def cmd_status(args):
    try:
        jid = set_status(connect(), args.id, args.status, args.notes)
    except ValueError as e:
        log(str(e))
        return 1
    emit({"id": jid, "status": args.status})
    return 0


def cmd_report(args):
    text = render_report(connect(), load_config())
    REPORT_PATH.write_text(text, encoding="utf-8")
    print(f"escrito {REPORT_PATH}")
    return 0


def cmd_notify(args):
    cfg = load_config()
    conn = connect()
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    rows = []
    if args.message:
        text = args.message
    else:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE state='scored' AND notified=0 AND status='nueva' AND score>=? "
            "AND COALESCE(verdict,'')!='descartar' ORDER BY score DESC", (cfg["min_score_to_save"],)).fetchall()
        if not rows:
            log("nada nuevo que avisar")
            return 0
        text = digest_text(rows)
    if args.dry_run or not (token and chat):
        print(text)
        if not (token and chat):
            log("TG_TOKEN/TG_CHAT no configurados: aviso omitido")
        return 0
    try:
        send_telegram(token, chat, text)
    except Exception as e:  # noqa: BLE001  (no imprimir la URL: contiene el token)
        log(f"fallo al enviar a Telegram: {type(e).__name__}")
        return 1
    if rows:
        conn.executemany("UPDATE jobs SET notified=1 WHERE id=?", [(r["id"],) for r in rows])
        conn.commit()
    print(f"enviado ({len(rows)} ofertas)" if rows else "enviado")
    return 0


def cmd_stats(args):
    conn = connect()
    q = lambda sql: conn.execute(sql).fetchall()  # noqa: E731
    print("## Embudo")
    for r in q("SELECT state, COUNT(*) n FROM jobs GROUP BY state ORDER BY n DESC"):
        print(f"- {r['state']}: {r['n']}")
    print("\n## Estados")
    for r in q("SELECT status, COUNT(*) n FROM jobs WHERE state='scored' GROUP BY status ORDER BY n DESC"):
        print(f"- {r['status']}: {r['n']}")
    print("\n## Motivos de filtrado")
    for r in q("SELECT filter_reason, COUNT(*) n FROM jobs WHERE state='filtered' "
               "GROUP BY filter_reason ORDER BY n DESC LIMIT 8"):
        print(f"- {r['filter_reason']}: {r['n']}")
    print("\n## Fuentes (ofertas recomendadas, 7+)")
    for r in q("SELECT source, COUNT(*) n FROM jobs WHERE score>=7 GROUP BY source ORDER BY n DESC"):
        print(f"- {r['source']}: {r['n']}")
    return 0


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    f = sub.add_parser("fetch")
    f.add_argument("--keywords", default="", help="palabras clave extra separadas por comas")
    f.add_argument("--dry-run", action="store_true")
    f.set_defaults(fn=cmd_fetch)
    s = sub.add_parser("show")
    s.add_argument("id")
    s.set_defaults(fn=cmd_show)
    l = sub.add_parser("list")
    l.add_argument("--status")
    l.add_argument("--min-score", type=int)
    l.add_argument("--need-verify", action="store_true")
    l.add_argument("--need-letter", action="store_true")
    l.add_argument("--limit", type=int, default=50)
    l.set_defaults(fn=cmd_list)
    for name, fn in (("save-scores", cmd_save_scores), ("save-verdicts", cmd_save_verdicts)):
        x = sub.add_parser(name)
        x.add_argument("file", help="ruta a un JSON, o - para stdin")
        x.set_defaults(fn=fn)
    st = sub.add_parser("status")
    st.add_argument("id")
    st.add_argument("status", choices=STATUSES)
    st.add_argument("--notes")
    st.set_defaults(fn=cmd_status)
    sub.add_parser("report").set_defaults(fn=cmd_report)
    n = sub.add_parser("notify")
    n.add_argument("--dry-run", action="store_true")
    n.add_argument("--message", help="enviar este texto en vez del resumen de ofertas")
    n.set_defaults(fn=cmd_notify)
    sub.add_parser("stats").set_defaults(fn=cmd_stats)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
