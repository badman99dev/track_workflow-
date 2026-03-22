from flask import Flask, jsonify, Response
import requests
import os
import zipfile
import io

app = Flask(__name__)

GITHUB_PAT = os.environ.get("GITHUB_PAT")
TARGET_REPO = os.environ.get("TARGET_REPO", "error404unknownuser99-ux/Claude")

HEADERS = {
    "Authorization": f"token {GITHUB_PAT}",
    "Accept": "application/vnd.github+json"
}

BASE_URL = "https://api.github.com"


def get_latest_run():
    """Latest workflow run fetch karo"""
    url = f"{BASE_URL}/repos/{TARGET_REPO}/actions/runs?per_page=1"
    res = requests.get(url, headers=HEADERS)
    runs = res.json().get("workflow_runs", [])
    return runs[0] if runs else None


def get_job_logs(job_id):
    """Job ka raw log fetch karo"""
    url = f"{BASE_URL}/repos/{TARGET_REPO}/actions/jobs/{job_id}/logs"
    res = requests.get(url, headers=HEADERS, allow_redirects=True)
    if res.status_code == 200:
        return res.text
    return f"Logs fetch nahi hue. Status: {res.status_code}"


def get_jobs(run_id):
    """Run ke sare jobs fetch karo"""
    url = f"{BASE_URL}/repos/{TARGET_REPO}/actions/runs/{run_id}/jobs"
    res = requests.get(url, headers=HEADERS)
    return res.json().get("jobs", [])


def get_artifacts(run_id):
    """Run ke sare artifacts fetch karo"""
    url = f"{BASE_URL}/repos/{TARGET_REPO}/actions/runs/{run_id}/artifacts"
    res = requests.get(url, headers=HEADERS)
    return res.json().get("artifacts", [])


def download_artifact(artifact_id):
    """Artifact zip download karke extract karo"""
    url = f"{BASE_URL}/repos/{TARGET_REPO}/actions/artifacts/{artifact_id}/zip"
    res = requests.get(url, headers=HEADERS, allow_redirects=True)
    if res.status_code == 200:
        extracted = {}
        with zipfile.ZipFile(io.BytesIO(res.content)) as z:
            for name in z.namelist():
                with z.open(name) as f:
                    try:
                        extracted[name] = f.read().decode("utf-8")
                    except:
                        extracted[name] = "[binary file - decode nahi hua]"
        return extracted
    return {}


# ─────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────

@app.route("/")
def home():
    return jsonify({
        "status": "🟢 Monitor API running!",
        "repo": TARGET_REPO,
        "endpoints": {
            "/latest": "Latest workflow run info",
            "/logs": "Latest run ke logs",
            "/artifacts": "Latest run ke artifacts list",
            "/artifacts/download": "Latest run ke artifacts ka content",
            "/full": "Sab kuch ek saath - logs + artifacts"
        }
    })


@app.route("/latest")
def latest_run():
    run = get_latest_run()
    if not run:
        return jsonify({"error": "Koi run nahi mila"}), 404

    return jsonify({
        "run_id": run["id"],
        "workflow": run["name"],
        "status": run["status"],
        "conclusion": run["conclusion"],
        "created_at": run["created_at"],
        "updated_at": run["updated_at"],
        "url": run["html_url"]
    })


@app.route("/logs")
def logs():
    run = get_latest_run()
    if not run:
        return jsonify({"error": "Koi run nahi mila"}), 404

    jobs = get_jobs(run["id"])
    if not jobs:
        return jsonify({"error": "Koi job nahi mila"}), 404

    all_logs = {}
    errors = []

    for job in jobs:
        log_text = get_job_logs(job["id"])
        all_logs[job["name"]] = {
            "status": job["status"],
            "conclusion": job["conclusion"],
            "log": log_text,
            "last_20_lines": "\n".join(log_text.splitlines()[-20:])
        }
        if job["conclusion"] == "failure":
            errors.append({
                "job": job["name"],
                "log_tail": "\n".join(log_text.splitlines()[-30:])
            })

    return jsonify({
        "run_id": run["id"],
        "workflow": run["name"],
        "status": run["status"],
        "conclusion": run["conclusion"],
        "jobs": all_logs,
        "errors": errors
    })


@app.route("/artifacts")
def artifacts():
    run = get_latest_run()
    if not run:
        return jsonify({"error": "Koi run nahi mila"}), 404

    arts = get_artifacts(run["id"])
    return jsonify({
        "run_id": run["id"],
        "workflow": run["name"],
        "artifact_count": len(arts),
        "artifacts": [
            {
                "id": a["id"],
                "name": a["name"],
                "size_mb": round(a["size_in_bytes"] / 1024 / 1024, 2),
                "created_at": a["created_at"],
                "expires_at": a["expires_at"]
            }
            for a in arts
        ]
    })


@app.route("/artifacts/download")
def artifacts_download():
    run = get_latest_run()
    if not run:
        return jsonify({"error": "Koi run nahi mila"}), 404

    arts = get_artifacts(run["id"])
    if not arts:
        return jsonify({"error": "Koi artifact nahi mila is run mein"}), 404

    result = {}
    for a in arts:
        extracted = download_artifact(a["id"])
        result[a["name"]] = extracted

    return jsonify({
        "run_id": run["id"],
        "workflow": run["name"],
        "artifacts": result
    })


@app.route("/full")
def full():
    """Sab kuch ek saath"""
    run = get_latest_run()
    if not run:
        return jsonify({"error": "Koi run nahi mila"}), 404

    # Logs
    jobs = get_jobs(run["id"])
    all_logs = {}
    errors = []
    for job in jobs:
        log_text = get_job_logs(job["id"])
        all_logs[job["name"]] = {
            "status": job["status"],
            "conclusion": job["conclusion"],
            "log": log_text,
            "last_20_lines": "\n".join(log_text.splitlines()[-20:])
        }
        if job["conclusion"] == "failure":
            errors.append({"job": job["name"], "tail": log_text.splitlines()[-30:]})

    # Artifacts
    arts = get_artifacts(run["id"])
    artifact_contents = {}
    for a in arts:
        artifact_contents[a["name"]] = download_artifact(a["id"])

    return jsonify({
        "run": {
            "id": run["id"],
            "workflow": run["name"],
            "status": run["status"],
            "conclusion": run["conclusion"],
            "created_at": run["created_at"],
            "url": run["html_url"]
        },
        "logs": all_logs,
        "errors": errors,
        "artifacts": artifact_contents
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
