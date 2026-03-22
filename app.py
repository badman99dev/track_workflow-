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
GITHUB_PAT           = os.environ.get("GITHUB_PAT", "")
TARGET_REPO          = os.environ.get("TARGET_REPO", "error404unknownuser99-ux/Claude")
BASE_URL             = "https://api.github.com"
MAX_WORKFLOWS        = 5
UPDATE_INTERVAL      = 5   # seconds

log.info("=== Monitor API Starting ===")
log.info(f"TARGET_REPO     : {TARGET_REPO}")
log.info(f"UPDATE_INTERVAL : {UPDATE_INTERVAL}s")
log.info(f"MAX_WORKFLOWS   : {MAX_WORKFLOWS}")
log.info(f"GITHUB_PAT      : {'SET ✅ (' + GITHUB_PAT[:8] + '...)' if GITHUB_PAT else 'MISSING ❌'}")

# ─── Global state (background thread shared karta hai) ────────────────────────
_state_lock    = threading.Lock()
_latest_snapshot = {}          # Latest snapshot — SSE clients padh lete hain
_monitor_running = False       # Background thread chal raha hai ya nahi


def gh():
    return {"Authorization": f"token {GITHUB_PAT}", "Accept": "application/vnd.github+json"}


# ══════════════════════════════════════════════════════════════════════════════
# YAML VALIDATOR
# ══════════════════════════════════════════════════════════════════════════════
def validate_workflow_files():
    results = {}
    url = f"{BASE_URL}/repos/{TARGET_REPO}/contents/.github/workflows"
    res = requests.get(url, headers=gh(), timeout=10)

    if res.status_code == 404:
        return {"_error": ".github/workflows folder nahi mila"}
    if res.status_code != 200:
        return {"_error": f"GitHub API error: {res.status_code}"}

    files = [f for f in res.json() if f["name"].endswith((".yml", ".yaml"))]

    for f in files:
        name     = f["name"]
        file_res = requests.get(f["url"], headers=gh(), timeout=10)
        if file_res.status_code != 200:
            results[name] = {"valid": False, "error": f"File fetch failed: {file_res.status_code}"}
            continue
        try:
            content = base64.b64decode(file_res.json().get("content", "")).decode("utf-8")
        except Exception as e:
            results[name] = {"valid": False, "error": f"Decode failed: {e}"}
            continue

        try:
            parsed = yaml.safe_load(content)
            issues = []
            if not isinstance(parsed, dict):
                issues.append("Root element dict nahi hai")
            else:
                if "on" not in parsed and True not in parsed:
                    issues.append("'on' trigger missing")
                if "jobs" not in parsed:
                    issues.append("'jobs' section missing")
                else:
                    for jname, jdata in parsed.get("jobs", {}).items():
                        if not isinstance(jdata, dict):
                            issues.append(f"Job '{jname}' invalid")
                        else:
                            if "steps"    not in jdata: issues.append(f"Job '{jname}': 'steps' missing")
                            if "runs-on"  not in jdata: issues.append(f"Job '{jname}': 'runs-on' missing")
            if issues:
                results[name] = {"valid": False, "syntax_ok": True,  "issues": issues}
            else:
                results[name] = {"valid": True,  "syntax_ok": True,  "status": "✅ Sahi hai!"}
        except yaml.YAMLError as e:
            results[name] = {"valid": False, "syntax_ok": False, "error": f"YAML Syntax Error: {str(e)}"}
            log.error(f"❌ YAML error in {name}: {e}")

    return results


# ══════════════════════════════════════════════════════════════════════════════
# CONCURRENCY LIMITER
# ══════════════════════════════════════════════════════════════════════════════
def get_running_workflows():
    url = f"{BASE_URL}/repos/{TARGET_REPO}/actions/runs?status=in_progress&per_page=20"
    res = requests.get(url, headers=gh(), timeout=10)
    if res.status_code != 200:
        return []
    return res.json().get("workflow_runs", [])


def cancel_workflow(run_id):
    res = requests.post(f"{BASE_URL}/repos/{TARGET_REPO}/actions/runs/{run_id}/cancel",
                        headers=gh(), timeout=10)
    ok = res.status_code == 202
    log.info(f"{'✅' if ok else '❌'} Cancel run {run_id}: {res.status_code}")
    return ok


def enforce_max_workflows():
    running  = get_running_workflows()
    result   = {"running_count": len(running), "cancelled": [], "action_taken": False,
                "message": f"✅ {len(running)}/{MAX_WORKFLOWS} running — sab theek!"}

    if len(running) > MAX_WORKFLOWS:
        to_cancel = sorted(running, key=lambda r: r["created_at"])[:len(running) - MAX_WORKFLOWS]
        log.warning(f"⚠️ {len(running)} running! Cancelling {len(to_cancel)} oldest...")
        for run in to_cancel:
            cancel_workflow(run["id"])
            result["cancelled"].append({"run_id": run["id"], "workflow": run["name"], "created_at": run["created_at"]})
        result["action_taken"] = True
        result["message"]      = f"⚠️ {len(to_cancel)} purane workflows cancel kiye!"
    return result


# ══════════════════════════════════════════════════════════════════════════════
# GITHUB HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def get_latest_run():
    try:
        res = requests.get(f"{BASE_URL}/repos/{TARGET_REPO}/actions/runs?per_page=1&direction=desc",
                           headers=gh(), timeout=15)
        if res.status_code == 401: return None, "PAT invalid ya expire (401)"
        if res.status_code == 404: return None, f"Repo nahi mila: {TARGET_REPO}"
        if res.status_code != 200: return None, f"GitHub API error: {res.status_code}"
        runs = res.json().get("workflow_runs", [])
        if not runs: return None, "Koi run nahi mila abhi tak"
        run = runs[0]
        log.debug(f"Latest run → {run['id']} | {run['name']} | {run['status']} | {run['conclusion']}")
        return run, None
    except Exception as e:
        return None, str(e)


def get_jobs(run_id):
    res = requests.get(f"{BASE_URL}/repos/{TARGET_REPO}/actions/runs/{run_id}/jobs",
                       headers=gh(), timeout=15)
    return res.json().get("jobs", [])


def get_job_logs(job_id):
    res = requests.get(f"{BASE_URL}/repos/{TARGET_REPO}/actions/jobs/{job_id}/logs",
                       headers=gh(), allow_redirects=True, timeout=20)
    return res.text if res.status_code == 200 else f"[Log fetch failed HTTP {res.status_code}]"


def get_artifacts(run_id):
    res = requests.get(f"{BASE_URL}/repos/{TARGET_REPO}/actions/runs/{run_id}/artifacts",
                       headers=gh(), timeout=15)
    return res.json().get("artifacts", [])


def download_artifact(artifact_id):
    res = requests.get(f"{BASE_URL}/repos/{TARGET_REPO}/actions/artifacts/{artifact_id}/zip",
                       headers=gh(), allow_redirects=True, timeout=30)
    if res.status_code != 200: return {}
    extracted = {}
    with zipfile.ZipFile(io.BytesIO(res.content)) as z:
        for name in z.namelist():
            with z.open(name) as f:
                try:    extracted[name] = f.read().decode("utf-8")
                except: extracted[name] = "[binary file]"
    return extracted


# ══════════════════════════════════════════════════════════════════════════════
# REPO FILE SAVER
# ══════════════════════════════════════════════════════════════════════════════
def save_file(path, content, commit_msg):
    """File ko TARGET_REPO mein push karo via GitHub API"""
    url     = f"{BASE_URL}/repos/{TARGET_REPO}/contents/{path}"
    get_res = requests.get(url, headers=gh(), timeout=10)
    sha     = get_res.json().get("sha") if get_res.status_code == 200 else None

    payload = {
        "message":   commit_msg,
        "content":   base64.b64encode(content.encode("utf-8")).decode("utf-8"),
        "committer": {"name": "Monitor Bot 🤖", "email": "monitor@noreply.github.com"}
    }
    if sha:
        payload["sha"] = sha

    res = requests.put(url, headers=gh(), json=payload, timeout=15)
    ok  = res.status_code in (200, 201)
    if not ok:
        log.error(f"❌ Save failed {path}: {res.status_code} | {res.text[:200]}")
    return ok


# ══════════════════════════════════════════════════════════════════════════════
# CORE SNAPSHOT BUILDER
# ══════════════════════════════════════════════════════════════════════════════
def build_snapshot(save_to_repo=True):
    """
    Poora snapshot banao:
    - Latest run + logs
    - YAML validation
    - Concurrency check
    - Optionally save to repo
    """
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    ts  = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    yaml_results = validate_workflow_files()
    concurrency  = enforce_max_workflows()
    run, err     = get_latest_run()

    # ── YAML summary text ──
    yaml_lines = ["", "="*50, "YAML VALIDATION:"]
    all_yaml_ok = True
    if isinstance(yaml_results, dict):
        for fname, r in yaml_results.items():
            if fname == "_error":
                yaml_lines.append(f"  ⚠️ {r}")
                all_yaml_ok = False
            elif r.get("valid"):
                yaml_lines.append(f"  ✅ {fname} — Sahi hai!")
            else:
                all_yaml_ok = False
                err_detail  = r.get("error") or r.get("issues", [])
                yaml_lines.append(f"  ❌ {fname} — {err_detail}")
    yaml_lines.append("="*50)

    # ── Concurrency summary ──
    conc_lines = [
        "",
        f"CONCURRENCY: {concurrency['message']}",
    ]
    if concurrency["cancelled"]:
        for c in concurrency["cancelled"]:
            conc_lines.append(f"  🚫 Cancelled: {c['workflow']} (run {c['run_id']})")

    # ── Handle run error ──
    if err:
        error_content = f"[{now}] ❌ Run fetch error: {err}\n"
        error_content += "\n".join(yaml_lines)
        if save_to_repo:
            save_file("output/logs/errors.txt",   error_content,      f"❌ Error [{ts}]")
            save_file("output/logs/last20lines.txt", f"[{now}] ❌ {err}\n", f"❌ Last20 [{ts}]")
            save_file("output/logs/all_logs.txt",    error_content,      f"❌ AllLogs [{ts}]")
        snapshot = {
            "timestamp":  now, "error": err,
            "yaml_check": {"all_valid": all_yaml_ok, "files": yaml_results},
            "concurrency": concurrency
        }
        return snapshot

    # ── Jobs + Logs ──
    jobs       = get_jobs(run["id"])
    all_lines  = [
        "="*60,
        f"🤖 Monitor Bot — Auto Update",
        f"Timestamp  : {now}",
        f"Run ID     : {run['id']}",
        f"Workflow   : {run['name']}",
        f"Status     : {run['status']}",
        f"Conclusion : {run['conclusion']}",
        f"URL        : {run['html_url']}",
        "="*60, ""
    ]
    job_results = {}
    error_lines = []
    last_lines  = []

    for job in jobs:
        log_text  = get_job_logs(job["id"])
        lines     = log_text.splitlines()
        last_lines = lines[-20:]

        all_lines += [
            f"\n--- JOB: {job['name']} | status:{job['status']} | conclusion:{job['conclusion']} ---",
            log_text,
            "--- END JOB ---\n"
        ]
        job_results[job["name"]] = {
            "status":        job["status"],
            "conclusion":    job["conclusion"],
            "last_20_lines": "\n".join(last_lines)
        }
        if job["conclusion"] == "failure":
            error_lines += [
                f"[{now}] ❌ FAILED JOB: {job['name']}",
                f"Run: {run['id']} | {run['name']}",
                "\n".join(lines[-40:]),
                "---\n"
            ]

    # Artifacts
    arts = get_artifacts(run["id"])
    artifact_data = {a["name"]: download_artifact(a["id"]) for a in arts}

    all_lines += yaml_lines + conc_lines
    all_lines.append(f"\n⏰ Last updated: {now}")

    # ── Save to repo ──
    if save_to_repo:
        log.info(f"💾 Saving logs to repo...")

        save_file("output/logs/all_logs.txt",
                  "\n".join(all_lines),
                  f"📋 All logs [{ts}]")

        save_file("output/logs/last20lines.txt",
                  "\n".join(last_lines) + f"\n\n⏰ Updated: {ts}",
                  f"📄 Last 20 [{ts}]")

        save_file("output/logs/errors.txt",
                  "\n".join(error_lines) if error_lines
                  else f"[{ts}] ✅ No errors — Run ID: {run['id']}\nYAML: {'✅ All valid' if all_yaml_ok else '❌ Check yaml_check'}\n",
                  f"{'❌' if error_lines else '✅'} Errors [{ts}]")

        log.info(f"✅ Repo updated!")

    snapshot = {
        "timestamp": now,
        "run": {
            "id":         run["id"],  "workflow":   run["name"],
            "status":     run["status"], "conclusion": run["conclusion"],
            "created_at": run["created_at"], "url": run["html_url"]
        },
        "jobs":        job_results,
        "artifacts":   artifact_data,
        "errors":      error_lines,
        "yaml_check":  {"all_valid": all_yaml_ok, "files": yaml_results},
        "concurrency": concurrency,
        "saved_to":    f"{TARGET_REPO}/output/logs/" if save_to_repo else "not saved"
    }

    with _state_lock:
        global _latest_snapshot
        _latest_snapshot = snapshot

    return snapshot


# ══════════════════════════════════════════════════════════════════════════════
# BACKGROUND MONITOR THREAD — 24/7 RUNS
# ══════════════════════════════════════════════════════════════════════════════
def background_monitor():
    """
    Ye thread app start hone ke saath hi shuru ho jaata hai.
    Har 5 second mein:
      1. Latest run fetch karo
      2. YAML validate karo
      3. Concurrency check + enforce karo
      4. Repo ke output/logs/ update karo
    """
    global _monitor_running
    _monitor_running = True
    log.info("🚀 Background monitor thread started! Har 5 sec mein repo update hoga.")

    while True:
        try:
            log.debug("⏱️ Background tick — building snapshot...")
            build_snapshot(save_to_repo=True)
            log.debug(f"✅ Snapshot done. Next in {UPDATE_INTERVAL}s")
        except Exception as e:
            log.error(f"❌ Background monitor error: {e}")
        time.sleep(UPDATE_INTERVAL)


# App start hone pe background thread launch karo
monitor_thread = threading.Thread(target=background_monitor, daemon=True)
monitor_thread.start()
log.info("✅ Background monitor thread launched!")


# ══════════════════════════════════════════════════════════════════════════════
# SSE STREAM — Live clients ko push karo (same data jo repo mein ja raha hai)
# ══════════════════════════════════════════════════════════════════════════════
def sse_generator():
    last_sent = {}
    while True:
        with _state_lock:
            current = dict(_latest_snapshot)

        if current and current != last_sent:
            last_sent = current
            yield f"data: {json.dumps(current, default=str)}\n\n"
        else:
            # Heartbeat — connection alive rakhne ke liye
            yield f": heartbeat {datetime.utcnow().strftime('%H:%M:%S')}\n\n"

        time.sleep(1)


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/")
def home():
    return jsonify({
        "status":           "🟢 Monitor API running!",
        "background":       "🔄 24/7 monitoring active — har 5 sec repo update hota hai",
        "target_repo":      TARGET_REPO,
        "pat_loaded":       bool(GITHUB_PAT),
        "update_interval":  f"{UPDATE_INTERVAL}s",
        "max_workflows":    MAX_WORKFLOWS,
        "endpoints": {
            "/stream":      "🔴 SSE live feed — browser/curl mein open karo",
            "/full":        "Abhi ka poora snapshot + save",
            "/latest":      "Sirf latest run info",
            "/yaml":        "Sirf YAML validation",
            "/concurrency": "Running workflows + cancel if >5",
            "/logs":        "Sirf logs JSON"
        }
    })


@app.route("/stream")
def stream():
    log.info("🔴 SSE client connected!")
    return Response(
        stream_with_context(sse_generator()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Access-Control-Allow-Origin": "*"}
    )


@app.route("/full")
def full():
    log.info("GET /full")
    snapshot = build_snapshot(save_to_repo=True)
    return jsonify(snapshot)


@app.route("/latest")
def latest():
    log.info("GET /latest")
    run, err = get_latest_run()
    if err: return jsonify({"error": err}), 404
    return jsonify({
        "run_id": run["id"], "workflow": run["name"],
        "status": run["status"], "conclusion": run["conclusion"],
        "created_at": run["created_at"], "updated_at": run["updated_at"],
        "url": run["html_url"]
    })


@app.route("/yaml")
def yaml_check():
    log.info("GET /yaml")
    results   = validate_workflow_files()
    all_valid = all(v.get("valid", False) for k, v in results.items() if k != "_error") \
                if isinstance(results, dict) else False
    return jsonify({"all_valid": all_valid,
                    "summary":   "✅ Sare valid!" if all_valid else "❌ Error hai kuch files mein!",
                    "files":     results})


@app.route("/concurrency")
def concurrency():
    log.info("GET /concurrency")
    return jsonify(enforce_max_workflows())


@app.route("/logs")
def logs():
    log.info("GET /logs")
    run, err = get_latest_run()
    if err: return jsonify({"error": err}), 404
    jobs   = get_jobs(run["id"])
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
