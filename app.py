import logging
import sys
import os
import zipfile
import io
import requests
import base64
from datetime import datetime
from flask import Flask, jsonify

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    stream=sys.stdout, level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
GITHUB_PAT  = os.environ.get("GITHUB_PAT", "")
TARGET_REPO = os.environ.get("TARGET_REPO", "error404unknownuser99-ux/Claude")
BASE_URL    = "https://api.github.com"

log.info("=== Monitor API Starting ===")
log.info(f"TARGET_REPO : {TARGET_REPO}")
log.info(f"GITHUB_PAT  : {'SET ✅ (' + GITHUB_PAT[:8] + '...)' if GITHUB_PAT else 'MISSING ❌'}")


def headers():
    return {"Authorization": f"token {GITHUB_PAT}", "Accept": "application/vnd.github+json"}


# ─── Save file to TARGET_REPO ─────────────────────────────────────────────────
def save_file(path, content, commit_msg):
    """File ko error404/Claude repo ke output/ mein save karo"""
    url = f"{BASE_URL}/repos/{TARGET_REPO}/contents/{path}"
    log.debug(f"Saving: {path}")

    # Existing SHA lo
    get_res = requests.get(url, headers=headers(), timeout=10)
    sha = get_res.json().get("sha") if get_res.status_code == 200 else None

    payload = {
        "message": commit_msg,
        "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
        "committer": {"name": "Monitor Bot", "email": "monitor@noreply.github.com"}
    }
    if sha:
        payload["sha"] = sha

    res = requests.put(url, headers=headers(), json=payload, timeout=15)
    if res.status_code in (200, 201):
        log.info(f"✅ Saved: {path}")
        return True
    log.error(f"❌ Save failed {path}: {res.status_code} - {res.text[:200]}")
    return False


# ─── GitHub helpers ───────────────────────────────────────────────────────────
def get_latest_run():
    url = f"{BASE_URL}/repos/{TARGET_REPO}/actions/runs?per_page=1&direction=desc"
    log.debug(f"GET {url}")
    try:
        res = requests.get(url, headers=headers(), timeout=15)
        log.debug(f"Status: {res.status_code} | Body[:300]: {res.text[:300]}")
        if res.status_code == 401:
            log.error("❌ 401 - PAT invalid!")
            return None, "PAT invalid (401)"
        if res.status_code == 404:
            log.error(f"❌ 404 - Repo nahi mila: {TARGET_REPO}")
            return None, f"Repo nahi mila: {TARGET_REPO}"
        if res.status_code != 200:
            return None, f"GitHub API error: {res.status_code}"

        runs = res.json().get("workflow_runs", [])
        log.info(f"Total runs: {res.json().get('total_count')}, Fetched: {len(runs)}")
        if not runs:
            return None, "Koi run nahi mila"

        run = runs[0]
        log.info(f"Run → ID:{run['id']} | {run['name']} | status:{run['status']} | conclusion:{run['conclusion']}")
        return run, None
    except Exception as e:
        log.error(f"❌ Exception: {e}")
        return None, str(e)


def get_jobs(run_id):
    res = requests.get(f"{BASE_URL}/repos/{TARGET_REPO}/actions/runs/{run_id}/jobs", headers=headers(), timeout=15)
    return res.json().get("jobs", [])


def get_job_logs(job_id):
    res = requests.get(f"{BASE_URL}/repos/{TARGET_REPO}/actions/jobs/{job_id}/logs",
                       headers=headers(), allow_redirects=True, timeout=20)
    if res.status_code == 200:
        return res.text
    log.warning(f"⚠️ Log fetch failed: {res.status_code}")
    return f"[Log fetch failed - HTTP {res.status_code}]"


def get_artifacts(run_id):
    res = requests.get(f"{BASE_URL}/repos/{TARGET_REPO}/actions/runs/{run_id}/artifacts", headers=headers(), timeout=15)
    arts = res.json().get("artifacts", [])
    log.info(f"Artifacts: {len(arts)}")
    return arts


def download_artifact(artifact_id):
    res = requests.get(f"{BASE_URL}/repos/{TARGET_REPO}/actions/artifacts/{artifact_id}/zip",
                       headers=headers(), allow_redirects=True, timeout=30)
    if res.status_code == 200:
        extracted = {}
        with zipfile.ZipFile(io.BytesIO(res.content)) as z:
            for name in z.namelist():
                with z.open(name) as f:
                    try:
                        extracted[name] = f.read().decode("utf-8")
                    except:
                        extracted[name] = "[binary file]"
        return extracted
    return {}


# ─── Main fetch + save logic ──────────────────────────────────────────────────
def fetch_and_save():
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    run, err = get_latest_run()

    if err:
        error_msg = f"[{now}] ❌ ERROR: {err}\n"
        save_file("output/logs/errors.txt", error_msg, f"❌ Error [{now}]")
        return None, err

    jobs       = get_jobs(run["id"])
    all_lines  = []
    error_lines = []
    last_lines  = []

    all_lines += [
        f"{'='*60}",
        f"Monitor Run : {now}",
        f"Run ID      : {run['id']}",
        f"Workflow    : {run['name']}",
        f"Status      : {run['status']}",
        f"Conclusion  : {run['conclusion']}",
        f"URL         : {run['html_url']}",
        f"{'='*60}\n"
    ]

    job_results = {}
    for job in jobs:
        log_text   = get_job_logs(job["id"])
        lines      = log_text.splitlines()
        last_lines = lines[-20:]

        all_lines += [
            f"\n--- JOB: {job['name']} | {job['status']} | {job['conclusion']} ---",
            log_text,
            f"--- END JOB ---\n"
        ]

        job_results[job["name"]] = {
            "status":        job["status"],
            "conclusion":    job["conclusion"],
            "log":           log_text,
            "last_20_lines": "\n".join(last_lines)
        }

        if job["conclusion"] == "failure":
            error_lines += [
                f"[{now}] ❌ FAILED: {job['name']}",
                f"Run ID: {run['id']} | {run['name']}",
                "\n".join(lines[-40:]),
                "---\n"
            ]

    # Artifacts
    arts = get_artifacts(run["id"])
    artifact_data = {}
    for a in arts:
        artifact_data[a["name"]] = download_artifact(a["id"])

    # ── output/logs/ mein save karo (same Claude repo mein!) ──
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    save_file("output/logs/all_logs.txt",
              "\n".join(all_lines),
              f"📋 All logs [{ts}]")

    save_file("output/logs/last20lines.txt",
              "\n".join(last_lines) + f"\n\n⏰ Updated: {ts}",
              f"📄 Last 20 lines [{ts}]")

    save_file("output/logs/errors.txt",
              "\n".join(error_lines) if error_lines else f"[{ts}] ✅ No errors (Run: {run['id']})\n",
              f"{'❌' if error_lines else '✅'} Errors [{ts}]")

    return {
        "run":        {"id": run["id"], "workflow": run["name"],
                       "status": run["status"], "conclusion": run["conclusion"],
                       "created_at": run["created_at"], "url": run["html_url"]},
        "jobs":       job_results,
        "artifacts":  artifact_data,
        "errors":     error_lines,
        "saved_to":   f"{TARGET_REPO}/output/logs/"
    }, None


# ─── Routes ───────────────────────────────────────────────────────────────────
@app.route("/")
def home():
    log.info("GET /")
    return jsonify({
        "status": "🟢 Monitor API running!",
        "target_repo": TARGET_REPO,
        "pat_loaded":  bool(GITHUB_PAT),
        "endpoints": {
            "/latest": "Latest run info",
            "/fetch":  "⭐ Logs fetch + output/logs/ mein save karo",
            "/logs":   "Sirf JSON logs",
            "/full":   "Sab kuch + save"
        }
    })


@app.route("/latest")
def latest():
    log.info("GET /latest")
    run, err = get_latest_run()
    if err:
        return jsonify({"error": err}), 404
    return jsonify({
        "run_id": run["id"], "workflow": run["name"],
        "status": run["status"], "conclusion": run["conclusion"],
        "created_at": run["created_at"], "updated_at": run["updated_at"],
        "url": run["html_url"]
    })


@app.route("/fetch")
def fetch():
    log.info("GET /fetch")
    result, err = fetch_and_save()
    if err:
        return jsonify({"error": err}), 404
    log.info("✅ /fetch done!")
    return jsonify(result)


@app.route("/full")
def full():
    log.info("GET /full")
    result, err = fetch_and_save()
    if err:
        return jsonify({"error": err}), 404
    return jsonify(result)


@app.route("/logs")
def logs():
    log.info("GET /logs")
    run, err = get_latest_run()
    if err:
        return jsonify({"error": err}), 404
    jobs = get_jobs(run["id"])
    result = {}
    for job in jobs:
        lt = get_job_logs(job["id"])
        result[job["name"]] = {"status": job["status"], "conclusion": job["conclusion"],
                                "last_20_lines": "\n".join(lt.splitlines()[-20:])}
    return jsonify({"run_id": run["id"], "workflow": run["name"],
                    "status": run["status"], "jobs": result})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
