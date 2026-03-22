import logging
import sys
import os
import zipfile
import io
import requests
from flask import Flask, jsonify

# ─── Logging setup - Render pe sab dikhega ───────────────────────────────────
logging.basicConfig(
    stream=sys.stdout,
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ─── Config ──────────────────────────────────────────────────────────────────
GITHUB_PAT   = os.environ.get("GITHUB_PAT", "")
TARGET_REPO  = os.environ.get("TARGET_REPO", "error404unknownuser99-ux/Claude")
BASE_URL     = "https://api.github.com"

log.info("=== Monitor API Starting ===")
log.info(f"TARGET_REPO  : {TARGET_REPO}")
log.info(f"GITHUB_PAT   : {'SET ✅ (' + GITHUB_PAT[:6] + '...)' if GITHUB_PAT else 'MISSING ❌'}")


def headers():
    return {
        "Authorization": f"token {GITHUB_PAT}",
        "Accept": "application/vnd.github+json"
    }


# ─── GitHub API helpers ───────────────────────────────────────────────────────

def get_latest_run():
    """Sabse naya triggered run - running ho ya completed"""
    url = f"{BASE_URL}/repos/{TARGET_REPO}/actions/runs?per_page=1&direction=desc"
    log.debug(f"GET {url}")
    try:
        res = requests.get(url, headers=headers(), timeout=15)
        log.debug(f"Response status: {res.status_code}")
        log.debug(f"Response body: {res.text[:500]}")

        if res.status_code == 401:
            log.error("❌ 401 Unauthorized - PAT invalid ya expire ho gaya!")
            return None, "PAT invalid ya expire ho gaya (401)"

        if res.status_code == 404:
            log.error(f"❌ 404 - Repo nahi mila: {TARGET_REPO}")
            return None, f"Repo nahi mila: {TARGET_REPO} (404)"

        if res.status_code != 200:
            log.error(f"❌ Unexpected status: {res.status_code}")
            return None, f"GitHub API error: {res.status_code}"

        data = res.json()
        total = data.get("total_count", 0)
        runs  = data.get("workflow_runs", [])
        log.info(f"Total runs found: {total}, Fetched: {len(runs)}")

        if not runs:
            log.warning("⚠️ Koi bhi run nahi mila repo mein")
            return None, "Repo mein abhi tak koi workflow run nahi hua"

        run = runs[0]
        log.info(f"Latest run → ID:{run['id']} | Name:{run['name']} | Status:{run['status']} | Conclusion:{run['conclusion']}")
        return run, None

    except requests.exceptions.Timeout:
        log.error("❌ GitHub API timeout!")
        return None, "GitHub API timeout"
    except Exception as e:
        log.error(f"❌ Exception: {e}")
        return None, str(e)


def get_jobs(run_id):
    url = f"{BASE_URL}/repos/{TARGET_REPO}/actions/runs/{run_id}/jobs"
    log.debug(f"GET jobs: {url}")
    res = requests.get(url, headers=headers(), timeout=15)
    log.debug(f"Jobs response: {res.status_code}")
    return res.json().get("jobs", [])


def get_job_logs(job_id):
    url = f"{BASE_URL}/repos/{TARGET_REPO}/actions/jobs/{job_id}/logs"
    log.debug(f"GET logs: {url}")
    res = requests.get(url, headers=headers(), allow_redirects=True, timeout=20)
    log.debug(f"Log response: {res.status_code}, size: {len(res.text)} chars")
    if res.status_code == 200:
        return res.text
    log.warning(f"⚠️ Log fetch failed: {res.status_code} - {res.text[:200]}")
    return f"[Log fetch failed - HTTP {res.status_code}]"


def get_artifacts(run_id):
    url = f"{BASE_URL}/repos/{TARGET_REPO}/actions/runs/{run_id}/artifacts"
    log.debug(f"GET artifacts: {url}")
    res = requests.get(url, headers=headers(), timeout=15)
    arts = res.json().get("artifacts", [])
    log.info(f"Artifacts found: {len(arts)}")
    return arts


def download_artifact(artifact_id):
    url = f"{BASE_URL}/repos/{TARGET_REPO}/actions/artifacts/{artifact_id}/zip"
    log.debug(f"Downloading artifact: {artifact_id}")
    res = requests.get(url, headers=headers(), allow_redirects=True, timeout=30)
    if res.status_code == 200:
        extracted = {}
        with zipfile.ZipFile(io.BytesIO(res.content)) as z:
            for name in z.namelist():
                with z.open(name) as f:
                    try:
                        extracted[name] = f.read().decode("utf-8")
                    except:
                        extracted[name] = "[binary file]"
        log.info(f"Artifact {artifact_id} extracted: {list(extracted.keys())}")
        return extracted
    log.warning(f"Artifact download failed: {res.status_code}")
    return {}


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def home():
    log.info("GET /")
    return jsonify({
        "status": "🟢 Monitor API running!",
        "repo": TARGET_REPO,
        "pat_loaded": bool(GITHUB_PAT),
        "endpoints": {
            "/latest":             "Latest run info (running ya completed - jo bhi naya ho)",
            "/logs":               "Latest run ke poore logs + errors",
            "/artifacts":          "Artifacts ki list",
            "/artifacts/download": "Artifacts ka extracted content",
            "/full":               "Sab kuch ek saath"
        }
    })


@app.route("/latest")
def latest_run_route():
    log.info("GET /latest")
    run, err = get_latest_run()
    if err:
        log.error(f"/latest error: {err}")
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


@app.route("/logs")
def logs_route():
    log.info("GET /logs")
    run, err = get_latest_run()
    if err:
        return jsonify({"error": err}), 404

    jobs = get_jobs(run["id"])
    all_logs = {}
    errors   = []

    for job in jobs:
        log_text = get_job_logs(job["id"])
        all_logs[job["name"]] = {
            "status":       job["status"],
            "conclusion":   job["conclusion"],
            "log":          log_text,
            "last_20_lines": "\n".join(log_text.splitlines()[-20:])
        }
        if job["conclusion"] == "failure":
            errors.append({
                "job":      job["name"],
                "log_tail": "\n".join(log_text.splitlines()[-30:])
            })

    return jsonify({
        "run_id":     run["id"],
        "workflow":   run["name"],
        "status":     run["status"],
        "conclusion": run["conclusion"],
        "jobs":       all_logs,
        "errors":     errors
    })


@app.route("/artifacts")
def artifacts_route():
    log.info("GET /artifacts")
    run, err = get_latest_run()
    if err:
        return jsonify({"error": err}), 404

    arts = get_artifacts(run["id"])
    return jsonify({
        "run_id":         run["id"],
        "workflow":       run["name"],
        "artifact_count": len(arts),
        "artifacts": [
            {
                "id":         a["id"],
                "name":       a["name"],
                "size_mb":    round(a["size_in_bytes"] / 1024 / 1024, 2),
                "created_at": a["created_at"],
                "expires_at": a["expires_at"]
            }
            for a in arts
        ]
    })


@app.route("/artifacts/download")
def artifacts_download_route():
    log.info("GET /artifacts/download")
    run, err = get_latest_run()
    if err:
        return jsonify({"error": err}), 404

    arts = get_artifacts(run["id"])
    if not arts:
        return jsonify({"error": "Is run mein koi artifact nahi mila"}), 404

    result = {}
    for a in arts:
        result[a["name"]] = download_artifact(a["id"])

    return jsonify({"run_id": run["id"], "workflow": run["name"], "artifacts": result})


@app.route("/full")
def full_route():
    log.info("GET /full")
    run, err = get_latest_run()
    if err:
        return jsonify({"error": err}), 404

    jobs      = get_jobs(run["id"])
    all_logs  = {}
    errors    = []

    for job in jobs:
        log_text = get_job_logs(job["id"])
        all_logs[job["name"]] = {
            "status":        job["status"],
            "conclusion":    job["conclusion"],
            "log":           log_text,
            "last_20_lines": "\n".join(log_text.splitlines()[-20:])
        }
        if job["conclusion"] == "failure":
            errors.append({"job": job["name"], "tail": log_text.splitlines()[-30:]})

    arts             = get_artifacts(run["id"])
    artifact_contents = {}
    for a in arts:
        artifact_contents[a["name"]] = download_artifact(a["id"])

    return jsonify({
        "run": {
            "id":         run["id"],
            "workflow":   run["name"],
            "status":     run["status"],
            "conclusion": run["conclusion"],
            "created_at": run["created_at"],
            "url":        run["html_url"]
        },
        "logs":      all_logs,
        "errors":    errors,
        "artifacts": artifact_contents
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
