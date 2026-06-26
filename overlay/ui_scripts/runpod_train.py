#!/usr/bin/env python3
"""RunPod remote-training orchestrator for the ai-toolkit UI extension.

Adapted from the standalone trainer at Runpod_Trainer/Linux/TrainV4.py, but made
non-interactive and driven by an ai-toolkit *job config JSON* (the same file the
local trainer uses). It:

  1. Creates a RunPod pod (GPU/cloud/disk taken from config.process[0].runpod).
  2. Installs ai-toolkit on the pod and uploads dataset(s) + a rewritten config.
  3. Runs `run.py` on the pod (non-UI mode, but keeps use_ui_logger so the pod
     writes loss_log.db + samples + checkpoints).
  4. Streams the remote log into the local log file, rsyncs the remote output
     folder back into the local job folder, and updates the local aitk_db.db Job
     row (step / status / speed) — so the existing UI widgets keep working.
  5. Tears the pod down on completion, error, stop (DB flag) or SIGINT/SIGTERM.

This is a NEW file under ui_scripts/, so it survives upstream `git pull`s.
Invoked by ui/cron/actions/startRunpodJob.ts.
"""

import argparse
import base64
import copy
import json
import os
import shlex
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

# --------------------------------------------------------------------------- #
# Arguments / globals
# --------------------------------------------------------------------------- #
parser = argparse.ArgumentParser(description="RunPod remote training orchestrator")
parser.add_argument("--config", required=True, help="Path to the ai-toolkit job config JSON")
parser.add_argument("--log", required=True, help="Local log file to write to")
parser.add_argument("--job-id", required=True, help="aitk Job id")
parser.add_argument("--db", required=True, help="Path to aitk_db.db")
parser.add_argument("--name", required=True, help="Job name")
parser.add_argument("--training-folder", required=True, help="Local training root folder")
args = parser.parse_args()

JOB_ID = args.job_id
DB_PATH = args.db
JOB_NAME = args.name
LOCAL_JOB_FOLDER = os.path.join(args.training_folder, JOB_NAME)
LOCAL_LOG = args.log

RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY", "")
SSH_KEY_PATH = os.path.expanduser(os.getenv("RUNPOD_SSH_KEY_PATH", "~/.ssh/id_ed25519"))
HF_TOKEN = os.getenv("HF_TOKEN", "")

IMAGE_NAME = "runpod/pytorch:2.1.0-py3.10-cuda11.8.0-devel-ubuntu22.04"

# Remote layout (mirrors TrainV4.py)
REMOTE_WORKSPACE = "/workspace"
REMOTE_TOOLKIT = f"{REMOTE_WORKSPACE}/ai-toolkit"
REMOTE_ENV_FILE = f"{REMOTE_TOOLKIT}/.env"
REMOTE_CONFIG = f"{REMOTE_TOOLKIT}/.runpod_job_config.json"
REMOTE_OUTPUT_ROOT = f"{REMOTE_TOOLKIT}/output"
REMOTE_SAVE_ROOT = f"{REMOTE_OUTPUT_ROOT}/{JOB_NAME}"
LOG_FILE = f"{REMOTE_WORKSPACE}/training_run.log"
REMOTE_PID_FILE = f"{REMOTE_WORKSPACE}/train.pid"
REMOTE_EXIT_FILE = f"{REMOTE_WORKSPACE}/train_exit"
REMOTE_TRAIN_SCRIPT = f"{REMOTE_WORKSPACE}/run_train.sh"

POD_IP = ""
POD_PORT = ""
POD_USER = "root"
POD_ID = ""

_stop_event = threading.Event()
_log_lock = threading.Lock()
_known_hosts = os.path.join(tempfile.gettempdir(), f"aitk_runpod_known_hosts_{JOB_ID}")

runpod = None  # lazily imported


# --------------------------------------------------------------------------- #
# Logging (writes to the local log file the UI tails, and to stdout)
# --------------------------------------------------------------------------- #
def log(msg: str):
    line = f"[runpod] {msg}"
    print(line, flush=True)
    with _log_lock:
        try:
            with open(LOCAL_LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Local DB updates (aitk_db.db Job row) — table name is "Job"
# --------------------------------------------------------------------------- #
def db_update(**fields):
    if not fields:
        return
    try:
        con = sqlite3.connect(DB_PATH, timeout=15.0, isolation_level=None)
        cols = ", ".join(f"{k} = ?" for k in fields)
        con.execute(f"UPDATE Job SET {cols} WHERE id = ?", (*fields.values(), JOB_ID))
        con.close()
    except Exception as e:
        print(f"[runpod] DB update failed: {e}", flush=True)


def db_should_stop() -> bool:
    try:
        con = sqlite3.connect(DB_PATH, timeout=15.0)
        cur = con.execute("SELECT stop, return_to_queue FROM Job WHERE id = ?", (JOB_ID,))
        row = cur.fetchone()
        con.close()
        if row and (row[0] or row[1]):
            return True
    except Exception:
        pass
    return False


# --------------------------------------------------------------------------- #
# SSH / SCP helpers (adapted from TrainV4.py)
# --------------------------------------------------------------------------- #
def _ssh_opts() -> str:
    return (
        f"-q -p {POD_PORT} -o ConnectTimeout=15 -o LogLevel=ERROR "
        f"-o StrictHostKeyChecking=no -o UserKnownHostsFile={shlex.quote(_known_hosts)} "
        f"-i {shlex.quote(SSH_KEY_PATH)}"
    )


def ssh(remote_cmd: str, check=True, capture=True):
    user_host = f"{POD_USER}@{POD_IP}"
    cmd = f"ssh {_ssh_opts()} {shlex.quote(user_host)} {shlex.quote(remote_cmd)}"
    res = subprocess.run(cmd, shell=True, capture_output=capture, text=True,
                         encoding="utf-8", errors="replace")
    if check and res.returncode != 0:
        raise RuntimeError(f"Remote command failed ({res.returncode}): {remote_cmd}\n{res.stderr}")
    return res.returncode, (res.stdout or "")


def scp(local_path: str, remote_path: str, upload=True, is_dir=False, check=True):
    user_host = f"{POD_USER}@{POD_IP}"
    flags = (
        f"-P {POD_PORT} -q -o StrictHostKeyChecking=no "
        f"-o UserKnownHostsFile={shlex.quote(_known_hosts)} -i {shlex.quote(SSH_KEY_PATH)}"
    )
    if is_dir:
        flags += " -r"
    remote_spec = f"{user_host}:{shlex.quote(remote_path)}"
    if upload:
        src, dst = shlex.quote(local_path), remote_spec
    else:
        src, dst = remote_spec, shlex.quote(local_path)
    cmd = f"scp {flags} {src} {dst}"
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if check and res.returncode != 0:
        raise RuntimeError(f"SCP failed ({res.returncode}): {cmd}\n{res.stderr}")
    return res.returncode


def wait_for_ssh(max_wait=420, delay=15) -> bool:
    log(f"Waiting for SSH at {POD_IP}:{POD_PORT} ...")
    start = time.time()
    while time.time() - start < max_wait:
        if _stop_event.is_set():
            return False
        rc, _ = ssh("echo ready", check=False)
        if rc == 0:
            log("SSH connection established.")
            return True
        time.sleep(delay)
    return False


# --------------------------------------------------------------------------- #
# RunPod SDK (lazy install/import) + pod lifecycle
# --------------------------------------------------------------------------- #
def ensure_runpod():
    global runpod
    try:
        import runpod as _rp
    except ImportError:
        log("Installing the 'runpod' python package into the venv ...")
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "runpod"], check=True)
        import runpod as _rp
    runpod = _rp
    runpod.api_key = RUNPOD_API_KEY


def create_pod(runpod_cfg: dict):
    global POD_ID, POD_IP, POD_PORT
    gpu_type = runpod_cfg.get("gpu_type", "NVIDIA GeForce RTX 4090")
    cloud_type = runpod_cfg.get("cloud_type", "SECURE")
    disk = int(runpod_cfg.get("container_disk_gb", 150))

    log(f"Creating pod: gpu='{gpu_type}' cloud='{cloud_type}' disk={disk}GB")
    db_update(info=f"Creating RunPod pod ({gpu_type}) ...")
    pod_params = dict(
        name=f"aitk-{JOB_NAME}"[:60], image_name=IMAGE_NAME, gpu_type_id=gpu_type,
        cloud_type=cloud_type, gpu_count=1, volume_in_gb=0, container_disk_in_gb=disk,
        start_ssh=True, support_public_ip=True, ports="22/tcp",
    )
    start = time.time()
    last_exc = None
    while time.time() - start < 1000:
        if _stop_event.is_set():
            raise RuntimeError("Stopped before pod creation")
        try:
            info = runpod.create_pod(**pod_params)
            POD_ID = info.get("id")
            break
        except Exception as e:
            last_exc = e
            log(f"create_pod retry: {e}")
            time.sleep(5)
    else:
        raise last_exc or RuntimeError("create_pod timed out")
    if not POD_ID:
        raise RuntimeError("No pod id returned from create_pod")
    log(f"Pod created: {POD_ID}")
    db_update(info=f"Pod {POD_ID} created, waiting for SSH ...")

    # Fetch SSH connection details
    for attempt in range(24):
        if _stop_event.is_set():
            raise RuntimeError("Stopped while fetching pod details")
        time.sleep(10)
        details = runpod.get_pod(POD_ID)
        status = details.get("desiredStatus", "UNKNOWN")
        if status in ("TERMINATED", "FAILED"):
            raise RuntimeError(f"Pod entered {status} during startup")
        runtime = details.get("runtime")
        if runtime and isinstance(runtime.get("ports"), list):
            for pm in runtime["ports"]:
                if pm.get("privatePort") == 22 and pm.get("publicPort") and pm.get("ip") and pm.get("type") == "tcp":
                    POD_IP = pm["ip"]
                    POD_PORT = str(pm["publicPort"])
                    log(f"SSH endpoint: {POD_IP}:{POD_PORT}")
                    return
        log(f"Waiting for pod runtime/ports (attempt {attempt + 1}/24) ...")
    raise RuntimeError("Failed to obtain SSH connection details")


def terminate_pod():
    if not POD_ID or runpod is None:
        return
    log(f"Terminating pod {POD_ID} ...")
    try:
        runpod.terminate_pod(POD_ID)
    except Exception as e:
        log(f"terminate_pod error (will not retry): {e}")


# --------------------------------------------------------------------------- #
# Config rewriting for the remote pod
# --------------------------------------------------------------------------- #
def build_remote_config(job_config: dict):
    """Return (remote_config_dict, [(local_dataset_path, remote_dataset_path), ...])."""
    cfg = copy.deepcopy(job_config)
    proc = cfg["config"]["process"][0]

    # Pod runs in non-UI mode; keep use_ui_logger so loss_log.db is still written.
    proc["sqlite_db_path"] = "./aitk_db.db"
    proc["training_folder"] = REMOTE_OUTPUT_ROOT
    proc["device"] = "cuda"
    proc.pop("runpod", None)  # not needed on the pod

    # Remap each dataset folder to a remote path; collect uploads (dedup by path).
    uploads = []
    remote_by_local = {}
    datasets = proc.get("datasets", []) or []
    for ds in datasets:
        local = ds.get("folder_path")
        if not local:
            continue
        if local not in remote_by_local:
            idx = len(remote_by_local)
            remote = f"{REMOTE_WORKSPACE}/dataset_{idx}"
            remote_by_local[local] = remote
            uploads.append((local, remote))
        ds["folder_path"] = remote_by_local[local]
        # control/mask paths are not uploaded in this version
        if ds.get("mask_path"):
            log(f"WARNING: dataset mask_path '{ds['mask_path']}' is not uploaded to the pod and will be ignored.")
            ds["mask_path"] = None

    # Warn (don't fail) about local model paths that won't exist on the pod.
    name_or_path = proc.get("model", {}).get("name_or_path", "")
    if name_or_path and os.path.exists(name_or_path):
        log(f"WARNING: model name_or_path '{name_or_path}' is a local path. It is NOT uploaded; "
            f"the pod will try to download it from Hugging Face by that name.")

    return cfg, uploads


# --------------------------------------------------------------------------- #
# Remote log streamer + output sync (background threads)
# --------------------------------------------------------------------------- #
def stream_remote_log():
    """Append new bytes of the remote LOG_FILE into the local log file."""
    offset = 1  # tail -c +N is 1-indexed
    while not _stop_event.is_set():
        try:
            rc, out = ssh(f"tail -c +{offset} {shlex.quote(LOG_FILE)} 2>/dev/null", check=False)
            if rc == 0 and out:
                with _log_lock:
                    with open(LOCAL_LOG, "a", encoding="utf-8") as f:
                        f.write(out)
                offset += len(out.encode("utf-8", errors="replace"))
        except Exception:
            pass
        _stop_event.wait(5)


def has_rsync_local() -> bool:
    return subprocess.run("command -v rsync", shell=True, capture_output=True).returncode == 0


def sync_outputs():
    """Pull the remote save folder into the local job folder (rsync, scp fallback)."""
    os.makedirs(LOCAL_JOB_FOLDER, exist_ok=True)
    use_rsync = has_rsync_local()
    if use_rsync:
        ssh_e = (
            f"ssh -p {POD_PORT} -o StrictHostKeyChecking=no "
            f"-o UserKnownHostsFile={shlex.quote(_known_hosts)} -i {shlex.quote(SSH_KEY_PATH)}"
        )
        cmd = (
            f"rsync -az --timeout=60 -e {shlex.quote(ssh_e)} "
            f"{shlex.quote(POD_USER + '@' + POD_IP + ':' + REMOTE_SAVE_ROOT + '/')} "
            f"{shlex.quote(LOCAL_JOB_FOLDER + '/')}"
        )
        rc = subprocess.run(cmd, shell=True, capture_output=True, text=True).returncode
        if rc == 0:
            return
        # fall through to scp on rsync failure (e.g. rsync missing on pod)
    try:
        scp(args.training_folder, REMOTE_SAVE_ROOT, upload=False, is_dir=True, check=False)
    except Exception as e:
        log(f"output sync warning: {e}")


def read_progress_step():
    """Current step = MAX(step) from the synced loss_log.db, if present."""
    loss_db = os.path.join(LOCAL_JOB_FOLDER, "loss_log.db")
    if not os.path.exists(loss_db):
        return None
    try:
        con = sqlite3.connect(loss_db, timeout=10.0)
        cur = con.execute("SELECT MAX(step) FROM steps")
        row = cur.fetchone()
        con.close()
        if row and row[0] is not None:
            return int(row[0])
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------- #
# Remote setup + training launch
# --------------------------------------------------------------------------- #
def setup_toolkit():
    db_update(info="Installing ai-toolkit on the pod ...")
    ssh(f"touch {LOG_FILE}", check=False)
    cmds = [
        f"{{ if [ ! -d {REMOTE_TOOLKIT} ]; then git clone https://github.com/ostris/ai-toolkit.git {REMOTE_TOOLKIT}; "
        f"else echo 'toolkit exists'; fi; }} >> {LOG_FILE} 2>&1",
        f"{{ cd {REMOTE_TOOLKIT} && git submodule update --init --recursive; }} >> {LOG_FILE} 2>&1",
        f"{{ command -v rsync >/dev/null 2>&1 || (apt-get update && apt-get install -y rsync); }} >> {LOG_FILE} 2>&1",
        f"{{ cd {REMOTE_TOOLKIT} && python -m venv venv; }} >> {LOG_FILE} 2>&1",
        f"{{ cd {REMOTE_TOOLKIT} && ./venv/bin/python -m pip install --upgrade pip; }} >> {LOG_FILE} 2>&1",
        f"{{ cd {REMOTE_TOOLKIT} && ./venv/bin/python -m pip install torch torchvision torchaudio "
        f"--index-url https://download.pytorch.org/whl/cu124; }} >> {LOG_FILE} 2>&1",
        f"{{ cd {REMOTE_TOOLKIT} && ./venv/bin/python -m pip install -r requirements.txt; }} >> {LOG_FILE} 2>&1",
    ]
    for c in cmds:
        if _stop_event.is_set():
            raise RuntimeError("Stopped during setup")
        ssh(c, check=True)


def upload_inputs(remote_config: dict, uploads):
    db_update(info="Uploading dataset and config ...")
    # HF token .env
    if HF_TOKEN:
        tmp_env = os.path.join(tempfile.gettempdir(), f"aitk_env_{JOB_ID}")
        with open(tmp_env, "w") as f:
            f.write(f"HF_TOKEN={HF_TOKEN}\n")
        scp(tmp_env, REMOTE_ENV_FILE, upload=True, check=False)
        os.remove(tmp_env)

    # datasets — remove the remote dir first, then `scp -r local remote` so the
    # remote folder becomes an exact copy of the local one (no nesting).
    for local, remote in uploads:
        if not os.path.isdir(local):
            raise RuntimeError(f"Dataset folder not found locally: {local}")
        ssh(f"rm -rf {remote}", check=False)
        log(f"Uploading dataset {local} -> {remote}")
        scp(local, remote, upload=True, is_dir=True, check=True)

    # config
    tmp_cfg = os.path.join(tempfile.gettempdir(), f"aitk_cfg_{JOB_ID}.json")
    with open(tmp_cfg, "w") as f:
        json.dump(remote_config, f, indent=2)
    ssh(f"mkdir -p {REMOTE_OUTPUT_ROOT}", check=False)
    scp(tmp_cfg, REMOTE_CONFIG, upload=True, check=True)
    os.remove(tmp_cfg)


def launch_training():
    ssh(f"rm -f {REMOTE_EXIT_FILE} {REMOTE_PID_FILE} {REMOTE_TRAIN_SCRIPT}", check=False)
    script = (
        "#!/bin/bash\n"
        f"cd {REMOTE_TOOLKIT}\n"
        f"./venv/bin/python run.py {REMOTE_CONFIG} >> {LOG_FILE} 2>&1\n"
        f"echo $? > {REMOTE_EXIT_FILE}\n"
    )
    encoded = base64.b64encode(script.encode()).decode()
    ssh(f"echo {encoded} | base64 -d > {REMOTE_TRAIN_SCRIPT} && chmod +x {REMOTE_TRAIN_SCRIPT}", check=True)
    ssh(f"nohup {REMOTE_TRAIN_SCRIPT} > /dev/null 2>&1 & echo $! > {REMOTE_PID_FILE}", check=True)
    log("Training launched on pod.")


def monitor(total_steps, poll=15, disconnect_grace=1800):
    """Poll the remote exit file; sync outputs + update step/speed; honor stop.

    Training runs under nohup on the pod, so a dropped SSH connection does NOT
    stop training. If the connection is lost we keep retrying (reconnect logic)
    and only give up after `disconnect_grace` seconds of continuous failure, so
    a genuinely dead pod can't hang the job forever.
    """
    last_step, last_t = 0, time.time()
    disconnect_since = None
    while True:
        if _stop_event.is_set() or db_should_stop():
            log("Stop requested — terminating pod.")
            db_update(status="stopped", info="Job stopped")
            return "stopped"

        check = (
            f"if [ -f {REMOTE_EXIT_FILE} ]; then echo DONE $(cat {REMOTE_EXIT_FILE}); "
            f"elif [ -f {REMOTE_PID_FILE} ] && kill -0 $(cat {REMOTE_PID_FILE}) 2>/dev/null; then echo RUNNING; "
            f"else echo GONE; fi"
        )
        try:
            rc, out = ssh(check, check=True)
        except Exception as e:
            # Connection lost — keep retrying (training survives via nohup).
            if disconnect_since is None:
                disconnect_since = time.time()
                log(f"Connection to pod lost ({e}). Training continues on the pod (nohup); "
                    f"retrying for up to {int(disconnect_grace / 60)} min ...")
                db_update(info="Connection lost — retrying (training continues on pod) ...")
            elif time.time() - disconnect_since > disconnect_grace:
                raise RuntimeError(f"Pod unreachable for over {int(disconnect_grace / 60)} min")
            time.sleep(poll)
            continue

        if disconnect_since is not None:
            log("Reconnected to pod — resuming monitoring.")
            db_update(info="Reconnected to pod.")
            disconnect_since = None

        out = out.strip()
        sync_outputs()
        step = read_progress_step()
        if step is not None:
            now = time.time()
            dt = now - last_t
            speed = ""
            if step > last_step and dt > 0:
                rate = (step - last_step) / dt
                speed = f"{rate:.2f} it/s" if rate >= 1 else f"{1 / rate:.2f} s/it"
            last_step, last_t = step, now
            db_update(step=step, info=f"Training on RunPod ({step}/{total_steps})",
                      **({"speed_string": speed} if speed else {}))

        if out.startswith("DONE"):
            parts = out.split()
            code = int(parts[1]) if len(parts) > 1 else -1
            log(f"Training finished with exit code {code}")
            return "completed" if code == 0 else "error"
        if out == "GONE":
            log("Training process vanished without an exit code.")
            return "error"
        time.sleep(poll)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def _signal_handler(signum, frame):
    log(f"Received signal {signum} — stopping.")
    _stop_event.set()


def main():
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    if not RUNPOD_API_KEY:
        db_update(status="error", info="RUNPOD_API_KEY not set")
        return 1

    with open(args.config) as f:
        job_config = json.load(f)
    runpod_cfg = job_config["config"]["process"][0].get("runpod", {})
    try:
        total_steps = int(job_config["config"]["process"][0]["train"]["steps"])
    except Exception:
        total_steps = None

    log(f"=== RunPod remote training for job '{JOB_NAME}' ({JOB_ID}) ===")
    if total_steps:
        db_update(total_steps=total_steps, info="Preparing RunPod job ...")

    remote_config, uploads = build_remote_config(job_config)

    log_thread = None
    result = "error"
    try:
        ensure_runpod()
        create_pod(runpod_cfg)
        if not wait_for_ssh():
            raise RuntimeError("SSH did not become ready")

        # start streaming the remote log as soon as the pod is reachable
        log_thread = threading.Thread(target=stream_remote_log, daemon=True)
        log_thread.start()

        setup_toolkit()
        if _stop_event.is_set():
            result = "stopped"
        else:
            upload_inputs(remote_config, uploads)
            db_update(status="running", info="Training on RunPod ...")
            launch_training()
            result = monitor(total_steps)

        # final pull of outputs
        try:
            sync_outputs()
        except Exception:
            pass

        if result == "completed":
            step = read_progress_step()
            db_update(status="completed", info="Training complete (RunPod)",
                      **({"step": step} if step is not None else {}))
        elif result == "stopped":
            db_update(status="stopped", info="Job stopped")
        else:
            db_update(status="error", info="RunPod training failed — see log")
    except Exception as e:
        log(f"ERROR: {e}")
        db_update(status="error", info=f"RunPod error: {e}")
        result = "error"
    finally:
        _stop_event.set()
        if log_thread:
            log_thread.join(timeout=3)
        # best-effort final pull so even a crash leaves the latest checkpoints local
        if POD_IP:
            try:
                sync_outputs()
            except Exception:
                pass
        terminate_pod()
        try:
            if os.path.exists(_known_hosts):
                os.remove(_known_hosts)
        except Exception:
            pass

    log(f"=== Done ({result}) ===")
    return 0 if result == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
