import logging
import sys
import os
import zipfile
import io
import requests
import json
import base64
from datetime import datetime
from flask import Flask, jsonify

# ─── Logging setup ────────────────────────────────────────────────────────────
logging.basicConfig(
    stream=sys.stdout,
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
GITHUB_PAT    = os.environ.get("GITHUB_PAT", "")
TARGET_REPO   = os.environ.get("TARGET_REPO", "error404unknownuser99-ux/Claude")
SAVE_REPO     = os.environ.get("SAVE_REPO", "badman99dev/track_workflow-")
SAVE_PAT      = os.environ.get("SAVE_PAT", "")
BASE_URL      = "https://api.github.com"

log.info("=== Monitor API Starting ===")
log.info(f"TARGET_REPO : {TARGET_REPO}")
log.info(f"SAVE_REPO   : {SAVE_REPO}")
log.info(f"GITHUB_PAT  : {'SET ✅ (' + GITHUB_PAT[:8] + '...)' if GITHUB_PAT else 'MISSING ❌'}")
log.info(f"SAVE_PAT    : {'SET ✅ (' + SAVE_PAT[:8] + '...)' if SAVE_PAT else 'MISSING ❌'}")


def gh_headers():
    return {"Authorization": f"token {GITHUB_PAT}", "Accept": "application/vnd.github+json"}

def save_headers():
    return {"Authorization": f"token {SAVE_PAT}", "Accept": "application/vnd.github+json"}


# ─── GitHub API helpers ───────────────────────────────────────────────────────

def get_latest_run():
    url = f"{BASE_URL}/repos/{TARGET_REPO}/actions/runs?per_page=1&direction=desc"
    log.debug(f"GET {url}")
    try:
        res = requests.get(url, headers=gh_headers(), timeout=15)
        log.debug(f"Status: {res.status_code} | Body[:300]: {res.text[:300]}")

        if res.status_code == 401:
            log.error("❌ 401 - PAT invalid ya expire!")
            return None, "PAT invalid ya expire (401)"
        if res.status_code == 404:
            log.error(f"❌ 404 - Repo nahi mila: {TARGET_REPO}")
            return None, f"Repo nahi mila: {TARGET_REPO}"
        if res.status_code != 200:
            return None, f"GitHub API error: {res.status_code}"

        runs = res.json().get("workflow_runs", [])
        log.info(f"Total runs: {res.json().get('total_count', 0)}, Fetched: {len(runs)}")
        if not runs:
            return None, "Koi run nahi mila"

        run = runs[0]
        log.info(f"Latest run → ID:{run['id']} | {run['name']} | status:{run['status']} | conclusion:{run['conclusion']}")
        return run, None
    except Exception as e:
        log.error(f"❌ Exception in get_latest_run: {e}")
        return None, str(e)


def get_jobs(run_id):
    url = f"{BASE_URL}/repos/{TARGET_REPO}/actions/runs/{run_id}/jobs"
    res = requests.get(url, headers=gh_headers(), timeout=15)
    log.debug(f"Jobs status: {res.status_code}")
    return res.json().get("jobs", [])


def get_job_logs(job_id):
    url = f"{BASE_URL}/repos/{TARGET_REPO}/actions/jobs/{job_id}/logs"
    res = requests.get(url, headers=gh_headers(), allow_redirects=True, timeout=20)
    log.debug(f"Log fetch status: {res.status_code}, size: {len(res.text)} chars")
    if res.status_code == 200:
        return res.text
    log.warning(f"⚠️ Log fetch failed: {res.status_code}")
    return f"[Log fetch failed - HTTP {res.status_code}]"


def get_artifacts(run_id):
    url = f"{BASE_URL}/repos/{TARGET_REPO}/actions/runs/{run_id}/artifacts"
    res = requests.get(url, headers=gh_headers(), timeout=15)
    arts = res.json().get("artifacts", [])
    log.info(f"Artifacts: {len(arts)}")
    return arts


def download_artifact(artifact_id):
    url = f"{BASE_URL}/repos/{TARGET_REPO}/actions/artifacts/{artifact_id}/zip"
    res = requests.get(url, headers=gh_headers(), allow_redirects=True, timeout=30)
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


# ─── GitHub file save helper ──────────────────────────────────────────────────

def save_file_to_repo(path, content, commit_msg):
    """File ko SAVE_REPO mein push karo via GitHub API"""
    if not SAVE_PAT:
        log.error("❌ SAVE_PAT missing - repo mein save nahi ho sakta!")
        return False

    url = f"{BASE_URL}/repos/{SAVE_REPO}/contents/{path}"

    # Pehle existing file ka SHA lo (update ke liye zaroori)
    get_res = requests.get(url, headers=save_headers(), timeout=10)
    sha = None
    if get_res.status_code == 200:
        sha = get_res.json().get("sha")
        log.debug(f"Existing file SHA: {sha}")

    payload = {
        "message": commit_msg,
        "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
        "committer": {
            "name": "Monitor Bot",
            "email": "monitor@noreply.github.com"
        }
    }
    if sha:
        payload["sha"] = sha

    res = requests.put(url, headers=save_headers(), json=payload, timeout=15)
    if res.status_code in (200, 201):
        log.info(f"✅ Saved to repo: {path}")
        return True
    else:
        log.error(f"❌ Save failed for {path}: {res.status_code} - {res.text[:300]}")
        return False


# ─── Core logic - fetch + save ────────────────────────────────────────────────

def fetch_and_save_logs():
    """Latest run ke logs fetch karo aur repo mein save karo"""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    run, err = get_latest_run()
    if err:
        error_txt = f"[{now}] ❌ ERROR: {err}\n"
        save_file_to_repo("output/logs/errors.txt", error_txt, f"❌ Error log [{now}]")
        return None, err

    jobs      = get_jobs(run["id"])
    all_text  = []
    error_text = []
    last20    = []

    all_text.append(f"{'='*60}")
    all_text.append(f"Monitor Run: {now}")
    all_text.append(f"Run ID     : {run['id']}")
    all_text.append(f"Workflow   : {run['name']}")
    all_text.append(f"Status     : {run['status']}")
    all_text.append(f"Conclusion : {run['conclusion']}")
    all_text.append(f"URL        : {run['html_url']}")
    all_text.append(f"{'='*60}\n")

    job_results = {}
    for job in jobs:
        log_text = get_job_logs(job["id"])
        lines    = log_text.splitlines()

        all_text.append(f"\n--- JOB: {job['name']} | status:{job['status']} | conclusion:{job['conclusion']} ---")
        all_text.append(log_text)
        all_text.append(f"--- END JOB: {job['name']} ---\n")

        last20 = lines[-20:]  # last job ki last 20 lines

        job_results[job["name"]] = {
            "status":        job["status"],
            "conclusion":    job["conclusion"],
            "log":           log_text,
            "last_20_lines": "\n".join(lines[-20:])
        }

        if job["conclusion"] == "failure":
            error_text.append(f"[{now}] ❌ FAILED JOB: {job['name']}")
            error_text.append(f"Run ID: {run['id']}")
            error_text.append(f"Workflow: {run['name']}")
            error_text.append("\n".join(lines[-40:]))
            error_text.append("---\n")

    # Artifacts
    arts = get_artifacts(run["id"])
    artifact_contents = {}
    for a in arts:
        artifact_contents[a["name"]] = download_artifact(a["id"])

    # ── Repo mein save karo ──
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    # 1. all_logs.txt
    save_file_to_repo(
        "output/logs/all_logs.txt",
        "\n".join(all_text),
        f"📋 All logs update [{timestamp}]"
    )

    # 2. last20lines.txt
    last20_content = "\n".join(last20) + f"\n\n⏰ Updated: {timestamp}"
    save_file_to_repo(
        "output/logs/last20lines.txt",
        last20_content,
        f"📄 Last 20 lines update [{timestamp}]"
    )

    # 3. errors.txt
    if error_text:
        save_file_to_repo(
            "output/logs/errors.txt",
            "\n".join(error_text),
            f"❌ Errors update [{timestamp}]"
        )
    else:
        save_file_to_repo(
            "output/logs/errors.txt",
            f"[{timestamp}] ✅ No errors in latest run (Run ID: {run['id']})\n",
            f"✅ No errors [{timestamp}]"
        )

    return {
        "run": {
            "id":         run["id"],
            "workflow":   run["name"],
            "status":     run["status"],
            "conclusion": run["conclusion"],
            "created_at": run["created_at"],
            "url":        run["html_url"]
        },
        "jobs":      job_results,
        "errors":    error_text,
        "artifacts": artifact_contents,
        "saved_to_repo": SAVE_REPO
    }, None


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def home():
    log.info("GET /")
    return jsonify({
        "status":      "🟢 Monitor API running!",
        "target_repo": TARGET_REPO,
        "save_repo":   SAVE_REPO,
        "pat_loaded":  bool(GITHUB_PAT),
        "save_pat":    bool(SAVE_PAT),
        "endpoints": {
            "/latest":  "Latest run info",
            "/fetch":   "Logs fetch karo + repo mein save karo ⭐",
            "/logs":    "Sirf JSON logs (save nahi hoga)",
            "/full":    "Sab kuch JSON mein"
        }
    })


@app.route("/latest")
def latest_run_route():
    log.info("GET /latest")
    run, err = get_latest_run()
    if err:
        return jsonify({"error": err}), 404
    return jsonify({
        "run_id":     run["id"],
        "workflow":   run["name"],
        "status":     run["status"],
        "conclusion": run["conclusion"],
        "created_at": run["created_at"],
        "updated_at": run["updated_at"],
        "url":        run["html_url"]
    })


@app.route("/fetch")
def fetch_route():
    """Main endpoint - logs fetch karo aur repo mein save karo"""
    log.info("GET /fetch - fetching logs and saving to repo...")
    result, err = fetch_and_save_logs()
    if err:
        return jsonify({"error": err}), 404
    log.info("✅ /fetch complete!")
    return jsonify(result)


@app.route("/logs")
def logs_route():
    log.info("GET /logs")
    run, err = get_latest_run()
    if err:
        return jsonify({"error": err}), 404
    jobs     = get_jobs(run["id"])
    all_logs = {}
    errors   = []
    for job in jobs:
        log_text = get_job_logs(job["id"])
        all_logs[job["name"]] = {
            "status":        job["status"],
            "conclusion":    job["conclusion"],
            "last_20_lines": "\n".join(log_text.splitlines()[-20:])
        }
        if job["conclusion"] == "failure":
            errors.append({"job": job["name"], "tail": log_text.splitlines()[-30:]})
    return jsonify({"run_id": run["id"], "workflow": run["name"],
                    "status": run["status"], "jobs": all_logs, "errors": errors})


@app.route("/full")
def full_route():
    log.info("GET /full")
    result, err = fetch_and_save_logs()
    if err:
        return jsonify({"error": err}), 404
    return jsonify(result)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
