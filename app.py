import logging, sys, os, zipfile, io, requests, base64, yaml, time, json, threading
from datetime import datetime
from flask import Flask, jsonify, Response, stream_with_context

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
MAX_CONCURRENT_WORKFLOWS = 5

log.info("=== Monitor API Starting ===")
log.info(f"TARGET_REPO : {TARGET_REPO}")
log.info(f"GITHUB_PAT  : {'SET ✅ (' + GITHUB_PAT[:8] + '...)' if GITHUB_PAT else 'MISSING ❌'}")
log.info(f"MAX_CONCURRENT_WORKFLOWS: {MAX_CONCURRENT_WORKFLOWS}")


def gh():
    return {"Authorization": f"token {GITHUB_PAT}", "Accept": "application/vnd.github+json"}


# ══════════════════════════════════════════════════════════════════════════════
# YAML VALIDATOR
# ══════════════════════════════════════════════════════════════════════════════
def validate_workflow_files():
    """Repo ke sare .github/workflows/*.yml files fetch karke validate karo"""
    results = {}
    log.info("🔍 Validating workflow YAML files...")

    # Workflow files list karo
    url = f"{BASE_URL}/repos/{TARGET_REPO}/contents/.github/workflows"
    res = requests.get(url, headers=gh(), timeout=10)

    if res.status_code == 404:
        log.warning("⚠️ .github/workflows folder nahi mila")
        return {"error": ".github/workflows folder nahi mila"}

    if res.status_code != 200:
        log.error(f"❌ Workflows list failed: {res.status_code}")
        return {"error": f"GitHub API error: {res.status_code}"}

    files = [f for f in res.json() if f["name"].endswith((".yml", ".yaml"))]
    log.info(f"Found {len(files)} workflow files")

    for f in files:
        name = f["name"]
        # File content fetch karo
        file_res = requests.get(f["url"], headers=gh(), timeout=10)
        if file_res.status_code != 200:
            results[name] = {"valid": False, "error": f"File fetch failed: {file_res.status_code}"}
            continue

        raw = file_res.json().get("content", "")
        try:
            content = base64.b64decode(raw).decode("utf-8")
        except Exception as e:
            results[name] = {"valid": False, "error": f"Base64 decode failed: {e}"}
            continue

        # PyYAML se validate karo
        try:
            parsed = yaml.safe_load(content)

            # Basic GitHub Actions structure check
            issues = []
            if not isinstance(parsed, dict):
                issues.append("Root element dict nahi hai")
            else:
                if "on" not in parsed and True not in parsed:
                    issues.append("'on' trigger missing hai")
                if "jobs" not in parsed:
                    issues.append("'jobs' section missing hai")
                else:
                    for job_name, job_data in parsed.get("jobs", {}).items():
                        if not isinstance(job_data, dict):
                            issues.append(f"Job '{job_name}' invalid hai")
                        elif "steps" not in job_data:
                            issues.append(f"Job '{job_name}' mein 'steps' missing")
                        elif "runs-on" not in job_data:
                            issues.append(f"Job '{job_name}' mein 'runs-on' missing")

            if issues:
                results[name] = {"valid": False, "syntax_ok": True, "structure_errors": issues}
                log.warning(f"⚠️ {name}: structure issues: {issues}")
            else:
                results[name] = {"valid": True, "syntax_ok": True, "message": "✅ Sahi hai!"}
                log.info(f"✅ {name}: valid!")

        except yaml.YAMLError as e:
            error_msg = str(e)
            results[name] = {
                "valid": False,
                "syntax_ok": False,
                "error": f"YAML Syntax Error: {error_msg}"
            }
            log.error(f"❌ {name}: YAML syntax error: {error_msg}")

    return results


# ══════════════════════════════════════════════════════════════════════════════
# WORKFLOW CONCURRENCY LIMITER (MAX 5)
# ══════════════════════════════════════════════════════════════════════════════
def get_running_workflows():
    """Abhi running workflows fetch karo"""
    url = f"{BASE_URL}/repos/{TARGET_REPO}/actions/runs?status=in_progress&per_page=20"
    res = requests.get(url, headers=gh(), timeout=10)
    if res.status_code != 200:
        log.error(f"❌ Running workflows fetch failed: {res.status_code}")
        return []
    runs = res.json().get("workflow_runs", [])
    log.info(f"Currently running workflows: {len(runs)}")
    return runs


def cancel_workflow(run_id):
    """Ek workflow cancel karo"""
    url = f"{BASE_URL}/repos/{TARGET_REPO}/actions/runs/{run_id}/cancel"
    res = requests.post(url, headers=gh(), timeout=10)
    if res.status_code == 202:
        log.info(f"✅ Cancelled workflow run: {run_id}")
        return True
    log.error(f"❌ Cancel failed for {run_id}: {res.status_code} - {res.text[:200]}")
    return False


def enforce_max_workflows():
    """
    Agar 5 se zyada workflows chal rahe hain toh
    sabse purana wala cancel karo
    """
    running = get_running_workflows()
    result = {"running_count": len(running), "cancelled": [], "action_taken": False}

    if len(running) > MAX_CONCURRENT_WORKFLOWS:
        # created_at se sort karo — sabse purana pehle
        sorted_runs = sorted(running, key=lambda r: r["created_at"])
        to_cancel = sorted_runs[:len(running) - MAX_CONCURRENT_WORKFLOWS]

        log.warning(f"⚠️ {len(running)} workflows running! Max={MAX_CONCURRENT_WORKFLOWS}. Cancelling {len(to_cancel)} oldest...")

        for run in to_cancel:
            success = cancel_workflow(run["id"])
            result["cancelled"].append({
                "run_id":     run["id"],
                "workflow":   run["name"],
                "created_at": run["created_at"],
                "cancelled":  success
            })

        result["action_taken"] = True
        result["message"] = f"⚠️ {len(to_cancel)} purane workflows cancel kiye gaye!"
    else:
        result["message"] = f"✅ Sab theek hai — {len(running)}/{MAX_CONCURRENT_WORKFLOWS} running"

    return result


# ══════════════════════════════════════════════════════════════════════════════
# GITHUB HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def get_latest_run():
    url = f"{BASE_URL}/repos/{TARGET_REPO}/actions/runs?per_page=1&direction=desc"
    try:
        res = requests.get(url, headers=gh(), timeout=15)
        log.debug(f"Status: {res.status_code} | Body[:200]: {res.text[:200]}")

        if res.status_code == 401:
            log.error("❌ 401 Unauthorized - PAT invalid!")
            return None, "PAT invalid ya expire (401)"
        if res.status_code == 404:
            log.error(f"❌ 404 - Repo nahi mila: {TARGET_REPO}")
            return None, f"Repo nahi mila: {TARGET_REPO}"
        if res.status_code != 200:
            return None, f"GitHub API error: {res.status_code}"

        runs = res.json().get("workflow_runs", [])
        if not runs:
            return None, "Koi run nahi mila"

        run = runs[0]
        log.info(f"Latest → ID:{run['id']} | {run['name']} | {run['status']} | {run['conclusion']}")
        return run, None
    except Exception as e:
        log.error(f"❌ Exception: {e}")
        return None, str(e)


def get_jobs(run_id):
    res = requests.get(f"{BASE_URL}/repos/{TARGET_REPO}/actions/runs/{run_id}/jobs", headers=gh(), timeout=15)
    return res.json().get("jobs", [])


def get_job_logs(job_id):
    res = requests.get(f"{BASE_URL}/repos/{TARGET_REPO}/actions/jobs/{job_id}/logs",
                       headers=gh(), allow_redirects=True, timeout=20)
    if res.status_code == 200:
        return res.text
    log.warning(f"⚠️ Log fetch failed: {res.status_code}")
    return f"[Log fetch failed - HTTP {res.status_code}]"


def get_artifacts(run_id):
    res = requests.get(f"{BASE_URL}/repos/{TARGET_REPO}/actions/runs/{run_id}/artifacts", headers=gh(), timeout=15)
    return res.json().get("artifacts", [])


def download_artifact(artifact_id):
    res = requests.get(f"{BASE_URL}/repos/{TARGET_REPO}/actions/artifacts/{artifact_id}/zip",
                       headers=gh(), allow_redirects=True, timeout=30)
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


def save_file(path, content, commit_msg):
    """File ko TARGET_REPO ke output/logs/ mein save karo"""
    url = f"{BASE_URL}/repos/{TARGET_REPO}/contents/{path}"
    get_res = requests.get(url, headers=gh(), timeout=10)
    sha = get_res.json().get("sha") if get_res.status_code == 200 else None

    payload = {
        "message": commit_msg,
        "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
        "committer": {"name": "Monitor Bot", "email": "monitor@noreply.github.com"}
    }
    if sha:
        payload["sha"] = sha

    res = requests.put(url, headers=gh(), json=payload, timeout=15)
    ok = res.status_code in (200, 201)
    if ok:
        log.info(f"✅ Saved: {path}")
    else:
        log.error(f"❌ Save failed {path}: {res.status_code} - {res.text[:200]}")
    return ok


# ══════════════════════════════════════════════════════════════════════════════
# FULL FETCH + SAVE
# ══════════════════════════════════════════════════════════════════════════════
def build_full_snapshot():
    """Ek complete snapshot banao — logs + yaml + concurrency"""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    # 1. Concurrency check + enforce
    concurrency = enforce_max_workflows()

    # 2. YAML validation
    yaml_results = validate_workflow_files()

    # 3. Latest run
    run, err = get_latest_run()
    if err:
        snapshot = {
            "timestamp":   now,
            "error":       err,
            "yaml_check":  yaml_results,
            "concurrency": concurrency
        }
        # Error bhi save karo
        save_file("output/logs/errors.txt",
                  f"[{now}] ❌ {err}\n",
                  f"❌ Error [{now}]")
        return snapshot, err

    jobs      = get_jobs(run["id"])
    all_lines = [
        f"{'='*60}",
        f"Monitor Snapshot : {now}",
        f"Run ID           : {run['id']}",
        f"Workflow         : {run['name']}",
        f"Status           : {run['status']}",
        f"Conclusion       : {run['conclusion']}",
        f"URL              : {run['html_url']}",
        f"{'='*60}\n"
    ]

    job_results = {}
    error_lines = []
    last_lines  = []

    for job in jobs:
        log_text  = get_job_logs(job["id"])
        lines     = log_text.splitlines()
        last_lines = lines[-20:]

        all_lines += [
            f"\n--- JOB: {job['name']} | {job['status']} | {job['conclusion']} ---",
            log_text,
            "--- END JOB ---\n"
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
                f"Run: {run['id']} | {run['name']}",
                "\n".join(lines[-40:]),
                "---\n"
            ]

    arts = get_artifacts(run["id"])
    artifact_data = {}
    for a in arts:
        artifact_data[a["name"]] = download_artifact(a["id"])

    # YAML summary for logs
    yaml_summary_lines = ["\n" + "="*40, "YAML VALIDATION RESULTS:"]
    for fname, result in yaml_results.items() if isinstance(yaml_results, dict) else []:
        status = "✅ VALID" if result.get("valid") else "❌ INVALID"
        detail = result.get("message") or result.get("error") or str(result.get("structure_errors", ""))
        yaml_summary_lines.append(f"  {status} | {fname} | {detail}")
    all_lines += yaml_summary_lines

    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    # Save files to repo
    save_file("output/logs/all_logs.txt",
              "\n".join(all_lines),
              f"📋 All logs [{ts}]")

    save_file("output/logs/last20lines.txt",
              "\n".join(last_lines) + f"\n\n⏰ Updated: {ts}",
              f"📄 Last 20 [{ts}]")

    save_file("output/logs/errors.txt",
              "\n".join(error_lines) if error_lines else f"[{ts}] ✅ No errors (Run: {run['id']})\n",
              f"{'❌' if error_lines else '✅'} Errors [{ts}]")

    return {
        "timestamp":   now,
        "run": {
            "id":         run["id"],
            "workflow":   run["name"],
            "status":     run["status"],
            "conclusion": run["conclusion"],
            "created_at": run["created_at"],
            "url":        run["html_url"]
        },
        "jobs":        job_results,
        "artifacts":   artifact_data,
        "errors":      error_lines,
        "yaml_check":  yaml_results,
        "concurrency": concurrency,
        "saved_to":    f"{TARGET_REPO}/output/logs/"
    }, None


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/")
def home():
    log.info("GET /")
    return jsonify({
        "status":      "🟢 Monitor API running!",
        "target_repo": TARGET_REPO,
        "pat_loaded":  bool(GITHUB_PAT),
        "endpoints": {
            "/latest":    "Latest run info",
            "/fetch":     "⭐ Logs fetch + repo mein save",
            "/full":      "Sab kuch ek saath + save",
            "/stream":    "🔴 LIVE — har 5 sec mein auto update (SSE)",
            "/yaml":      "Sirf YAML validation check",
            "/concurrency": "Running workflows check + enforce max 5",
            "/logs":      "Sirf logs JSON"
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


@app.route("/yaml")
def yaml_check():
    log.info("GET /yaml")
    results = validate_workflow_files()
    all_valid = all(v.get("valid", False) for v in results.values()) if isinstance(results, dict) and "error" not in results else False
    return jsonify({
        "all_valid":    all_valid,
        "summary":      "✅ Sare YAML files valid hain!" if all_valid else "❌ Kuch files mein error hai!",
        "files":        results
    })


@app.route("/concurrency")
def concurrency_check():
    log.info("GET /concurrency")
    result = enforce_max_workflows()
    return jsonify(result)


@app.route("/fetch")
def fetch():
    log.info("GET /fetch")
    result, err = build_full_snapshot()
    if err:
        return jsonify({"error": err}), 404
    return jsonify(result)


@app.route("/full")
def full():
    log.info("GET /full")
    result, err = build_full_snapshot()
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
        result[job["name"]] = {
            "status":        job["status"],
            "conclusion":    job["conclusion"],
            "last_20_lines": "\n".join(lt.splitlines()[-20:])
        }
    return jsonify({"run_id": run["id"], "workflow": run["name"],
                    "status": run["status"], "jobs": result})


@app.route("/stream")
def stream():
    """
    SSE endpoint — har 5 second mein live update bhejta hai
    Browser mein open karo ya: curl https://url/stream
    """
    log.info("GET /stream - SSE client connected!")

    def event_generator():
        while True:
            try:
                now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
                log.debug(f"SSE tick: {now}")

                # Quick snapshot (bina repo save ke — fast rakhna hai)
                run, err = get_latest_run()
                yaml_results = validate_workflow_files()
                concurrency  = enforce_max_workflows()

                all_valid = all(
                    v.get("valid", False)
                    for v in yaml_results.values()
                ) if isinstance(yaml_results, dict) and "error" not in yaml_results else False

                if err:
                    data = {
                        "timestamp":   now,
                        "error":       err,
                        "yaml_check":  {"all_valid": all_valid, "files": yaml_results},
                        "concurrency": concurrency
                    }
                else:
                    data = {
                        "timestamp": now,
                        "run": {
                            "id":         run["id"],
                            "workflow":   run["name"],
                            "status":     run["status"],
                            "conclusion": run["conclusion"],
                            "created_at": run["created_at"],
                            "url":        run["html_url"]
                        },
                        "yaml_check": {
                            "all_valid": all_valid,
                            "summary":   "✅ Sare valid!" if all_valid else "❌ Kuch files mein error!",
                            "files":     yaml_results
                        },
                        "concurrency": concurrency
                    }

                yield f"data: {json.dumps(data)}\n\n"

            except Exception as e:
                log.error(f"SSE error: {e}")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"

            time.sleep(5)

    return Response(
        stream_with_context(event_generator()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":  "no-cache",
            "X-Accel-Buffering": "no"
        }
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
