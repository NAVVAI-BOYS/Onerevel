"""The Screen Time Check, by OneRevel. Lead capture behind the diagnostic.

Storage is a convenience, never a startup dependency: leads land in three places
(stdout, a JSON file, and optionally email) so no single one can take the app down.
"""
import os
import json
import csv
import io
import time
import threading
import urllib.request
import urllib.error
from flask import Flask, request, jsonify, send_from_directory, Response

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")

ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
LEAD_TO = os.environ.get("LEAD_TO", "")
LEAD_FROM = os.environ.get("LEAD_FROM", "onboarding@resend.dev")
REQUESTED_DATA_DIR = os.environ.get("DATA_DIR", "/var/data")

app = Flask(__name__, static_folder=None)
_write_lock = threading.Lock()


def pick_data_dir():
    """Try each candidate in turn and say why each one failed.

    Returns (resolved_dir_or_None, notes). The app must serve even when all of them fail.
    """
    notes = []
    candidates = [
        (REQUESTED_DATA_DIR, "DATA_DIR / default mount"),
        ("/var/data", "conventional Render disk"),
        (os.path.join(APP_DIR, "data"), "directory beside app.py"),
        ("/tmp/screen-time-check", "ephemeral tmp"),
    ]
    seen = set()
    for path, label in candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        try:
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".writetest")
            with open(probe, "w") as fh:
                fh.write("ok")
            os.remove(probe)
            notes.append("USING %s (%s)" % (path, label))
            return path, notes
        except Exception as exc:
            notes.append("SKIPPED %s (%s): %s" % (path, label, exc))
    notes.append("NO WRITABLE STORE. The app still serves, still logs and still emails.")
    return None, notes


DATA_DIR, DATA_NOTES = pick_data_dir()
for _n in DATA_NOTES:
    print("[storage] " + _n, flush=True)

# EPHEMERAL means we are writing somewhere, but not where we were asked to.
STORAGE_EPHEMERAL = bool(DATA_DIR) and os.path.abspath(DATA_DIR) != os.path.abspath(REQUESTED_DATA_DIR)
LEADS_PATH = os.path.join(DATA_DIR, "leads.json") if DATA_DIR else None


def read_leads():
    if not LEADS_PATH:
        return []
    try:
        with open(LEADS_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return []


def write_lead(rec):
    if not LEADS_PATH:
        raise RuntimeError("no writable store")
    with _write_lock:
        leads = read_leads()
        leads.append(rec)
        tmp = LEADS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(leads, fh, indent=2)
        os.replace(tmp, LEADS_PATH)


def send_email(rec):
    if not (RESEND_API_KEY and LEAD_TO):
        return
    lines = [
        "New Screen Time Check lead",
        "",
        "Name:     %s" % rec.get("name", ""),
        "Company:  %s" % rec.get("org", ""),
        "Email:    %s" % rec.get("email", ""),
        "Seat:     %s" % rec.get("seat", ""),
        "",
        "Verdict:  %s" % rec.get("verdict", ""),
        "Weakest:  %s (%s of 10)" % (rec.get("weakest"), rec.get("weakest_score")),
        "Screens:  %s" % rec.get("screens"),
        "Stale:    %s" % rec.get("count"),
        "Fit:      %s (%s points)" % ((rec.get("fit") or {}).get("band"), (rec.get("fit") or {}).get("points")),
        "Motion:   %s" % (rec.get("fit") or {}).get("motion"),
        "",
        "Goal: %s" % rec.get("goal", ""),
        "",
        "Gaps:",
    ]
    for g in rec.get("gaps", []):
        lines.append("  [%s] %s  ->  %s (%s)" % (g.get("severity"), g.get("area"), g.get("answer"), g.get("score")))
    if rec.get("notSure"):
        lines.append("")
        lines.append("Did not know:")
        for q in rec["notSure"]:
            lines.append("  " + q)
    body = "\n".join(lines)
    payload = json.dumps({
        "from": LEAD_FROM,
        "to": [LEAD_TO],
        "subject": "Screen Time Check: %s at %s (%s)" % (
            rec.get("name", "unknown"), rec.get("org", "unknown"), (rec.get("fit") or {}).get("band", "")),
        "text": body,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=payload, method="POST",
        headers={"Authorization": "Bearer " + RESEND_API_KEY, "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=12).read()
        print("[email] sent to %s" % LEAD_TO, flush=True)
    except Exception as exc:
        print("[email] FAILED: %s" % exc, flush=True)


def rounded(v):
    return None if v is None else round(float(v), 1)


@app.route("/healthz")
def healthz():
    return "ok", 200


@app.route("/api/config")
def api_config():
    return jsonify({
        "live": True,
        "email_enabled": bool(RESEND_API_KEY and LEAD_TO),
        "storage": "disk" if (DATA_DIR and not STORAGE_EPHEMERAL) else ("ephemeral" if DATA_DIR else "none"),
    })


@app.route("/api/lead", methods=["POST"])
def api_lead():
    rec = request.get_json(silent=True) or {}
    rec["weakest_score"] = rounded(rec.get("weakest_score"))
    rec["overall"] = rounded(rec.get("overall"))
    for g in rec.get("gaps", []) or []:
        g["score"] = rounded(g.get("score"))
    for a in rec.get("areas", []) or []:
        a["score"] = rounded(a.get("score"))
    rec["received"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    rec["ip"] = request.headers.get("X-Forwarded-For", request.remote_addr or "")

    fit = rec.get("fit") or {}
    print("LEAD %s | %s | %s | %s | %s | weakest %s | screens %s | stale %s" % (
        fit.get("band", "?"), rec.get("name", ""), rec.get("org", ""), rec.get("email", ""),
        rec.get("verdict", ""), rec.get("weakest", ""), rec.get("screens"), rec.get("count")), flush=True)

    stored = False
    try:
        write_lead(rec)
        stored = True
    except Exception as exc:
        print("[storage] write failed: %s" % exc, flush=True)

    threading.Thread(target=send_email, args=(rec,), daemon=True).start()
    return jsonify({"ok": True, "stored": stored})


def _auth():
    key = request.args.get("key", "")
    return bool(ADMIN_KEY) and key == ADMIN_KEY


BAND_ORDER = {"PRIORITY": 0, "CORE": 1, "POSSIBLE": 2, "WATCH": 3}


def sorted_leads():
    leads = read_leads()
    return sorted(leads, key=lambda r: (BAND_ORDER.get(((r.get("fit") or {}).get("band")), 9),
                                        -((r.get("fit") or {}).get("points") or 0)))


@app.route("/admin/leads")
def admin_leads():
    if not _auth():
        return Response("unauthorised", status=401)
    leads = sorted_leads()

    # Built entirely by concatenation. A literal percent in a % formatted template is a 500.
    out = []
    out.append("<!DOCTYPE html><html><head><meta charset='utf-8'>")
    out.append("<meta name='viewport' content='width=device-width, initial-scale=1'>")
    out.append("<title>Screen Time Check leads</title><style>")
    out.append("body{background:#161A18;color:#F2F3F1;font-family:Helvetica,Arial,sans-serif;margin:0;padding:24px;font-size:14px}")
    out.append("h1{font-size:22px;margin:0 0 4px}.sub{color:#8A918D;margin:0 0 18px}")
    out.append("a{color:#7FCB42}.warn{background:#4A2B12;border:1px solid #B8860B;padding:12px 14px;border-radius:8px;margin:0 0 18px}")
    out.append(".c{background:#242826;border:1px solid #3C413E;border-radius:10px;padding:16px;margin-bottom:12px}")
    out.append(".b{display:inline-block;font-weight:700;font-size:11px;letter-spacing:.1em;padding:3px 9px;border-radius:5px;background:#3C413E}")
    out.append(".b.PRIORITY{background:#6AB432;color:#10230A}.b.CORE{background:#7FCB42;color:#10230A}")
    out.append(".b.POSSIBLE{background:#3C413E;color:#C6CCC8}.b.WATCH{background:#2F3231;color:#8A918D}")
    out.append(".m{color:#8A918D;font-size:12.5px;margin:6px 0}.q{color:#C6CCC8;font-style:italic}")
    out.append("ul{margin:6px 0;padding-left:18px}li{margin-bottom:3px;color:#C6CCC8}")
    out.append("</style></head><body>")
    out.append("<h1>The Screen Time Check</h1>")
    out.append("<p class='sub'>" + str(len(leads)) + " leads, best fit first. <a href='/admin/leads.csv?key="
               + request.args.get("key", "") + "'>Download CSV</a></p>")

    if not DATA_DIR:
        out.append("<div class='warn'><b>No writable storage.</b> Leads are logged to stdout and emailed, "
                   "but nothing is being kept on disk. Check the Render log for LEAD lines.</div>")
    elif STORAGE_EPHEMERAL:
        out.append("<div class='warn'><b>Storage is ephemeral.</b> The requested disk at "
                   + REQUESTED_DATA_DIR + " was not available, so leads are being written to "
                   + DATA_DIR + " instead. A redeploy will wipe them. Add the persistent disk in Render.</div>")

    if not leads:
        out.append("<p class='m'>No leads yet.</p>")

    for r in leads:
        fit = r.get("fit") or {}
        band = fit.get("band", "?")
        out.append("<div class='c'>")
        out.append("<span class='b " + band + "'>" + band + " " + str(fit.get("points", "")) + "</span> ")
        out.append("<b>" + str(r.get("name", "")) + "</b> at <b>" + str(r.get("org", "")) + "</b> &middot; ")
        out.append("<a href='mailto:" + str(r.get("email", "")) + "'>" + str(r.get("email", "")) + "</a>")
        out.append("<p class='m'>" + str(r.get("received", "")) + " &middot; " + str(r.get("seat", "")) + "</p>")
        out.append("<p class='m'><b>" + str(r.get("verdict", "")) + "</b> &middot; weakest " + str(r.get("weakest", ""))
                   + " at " + str(r.get("weakest_score", "")) + " of 10 &middot; " + str(r.get("screens", "?"))
                   + " screens &middot; " + str(r.get("count", "?")) + " on an old message</p>")
        if r.get("goal"):
            out.append("<p class='m'>Goal: <span class='q'>" + str(r.get("goal")) + "</span></p>")
        if fit.get("motion"):
            out.append("<p class='m'>Motion: " + str(fit.get("motion")) + "</p>")
        if r.get("gaps"):
            out.append("<p class='m'>Worst answers:</p><ul>")
            for g in r["gaps"][:4]:
                out.append("<li>[" + str(g.get("severity")) + "] " + str(g.get("area")) + ": <span class='q'>"
                           + str(g.get("answer")) + "</span> (" + str(g.get("score")) + ")</li>")
            out.append("</ul>")
        if r.get("notSure"):
            out.append("<p class='m'>Did not know:</p><ul>")
            for q in r["notSure"][:5]:
                out.append("<li>" + str(q) + "</li>")
            out.append("</ul>")
        out.append("</div>")

    out.append("</body></html>")
    return Response("".join(out), mimetype="text/html")


@app.route("/admin/leads.csv")
def admin_csv():
    if not _auth():
        return Response("unauthorised", status=401)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["received", "fit_band", "fit_points", "name", "org", "email", "seat",
                "verdict", "weakest", "weakest_score", "overall", "screens", "share",
                "stale_count", "gap_count", "not_sure_count", "goal", "motion"])
    for r in sorted_leads():
        fit = r.get("fit") or {}
        w.writerow([r.get("received", ""), fit.get("band", ""), fit.get("points", ""),
                    r.get("name", ""), r.get("org", ""), r.get("email", ""), r.get("seat", ""),
                    r.get("verdict", ""), r.get("weakest", ""), r.get("weakest_score", ""),
                    r.get("overall", ""), r.get("screens", ""), r.get("share", ""),
                    r.get("count", ""), len(r.get("gaps", []) or []), len(r.get("notSure", []) or []),
                    r.get("goal", ""), fit.get("motion", "")])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=screen-time-check-leads.csv"})


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/<path:p>")
def static_files(p):
    return send_from_directory(STATIC_DIR, p)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
