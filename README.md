# ai-toolkit-runpod

A UI extension for [ai-toolkit](https://github.com/ostris/ai-toolkit) that adds a
**"Train remotely on RunPod"** switch + GPU selector to the training form. When
enabled, the job runs on a freshly-created [RunPod](https://runpod.io) pod instead
of your local GPU — logs, checkpoints and samples stream back live so the normal
ai-toolkit UI (progress, loss graph, samples, files) keeps working, and the pod is
terminated automatically when training finishes, errors, or is stopped.

It mirrors the approach of a standalone SSH-based RunPod trainer: the pod boots a
base PyTorch image, clones ai-toolkit, and runs your **exact** job config there.

## Install

```bash
git clone https://github.com/banini1841/ai-toolkit-runpod.git
cd ai-toolkit-runpod

./install.sh        # Linux / macOS
install.bat         # Windows
```

The installer auto-detects an `ai-toolkit` folder sitting next to this repo; if it
can't find one it **prompts you for the path** (nothing to add to PATH). You can
also pass it directly:

```bash
./install.sh --ai-toolkit /path/to/ai-toolkit        # Linux / macOS
install.bat  --ai-toolkit C:\path\to\ai-toolkit       # Windows
```

The `.sh`/`.bat` wrappers just call `install.py`; run `python install.py` directly
if you prefer. Afterwards restart the UI: `cd <ai-toolkit>/ui && npm run build_and_start`.

The installer **copies new files** into ai-toolkit and applies **two tiny hooks**
to existing files (`SimpleJob.tsx`, `cron/actions/startJob.ts`). It's idempotent —
re-run it any time, including after you `git pull` ai-toolkit, to re-apply the hooks.

Uninstall with `./uninstall.sh` (or `uninstall.bat`).

### Why an installer instead of a fork

ai-toolkit has no UI plugin system, so a couple of source files must be touched.
Keeping the extension as a separate repo + installer means: it's publicly
shareable, all real logic lives in new files (which `git pull` never clobbers),
and the two hook edits are re-applied by re-running `install.py`.

## One-time setup

1. **RunPod API key** — create one in the RunPod console, then in the ai-toolkit
   job form open *Remote Training (RunPod)* and paste it → *Save Credentials*.
   (Stored in ai-toolkit's local `aitk_db.db` Settings table, like the HF token.)
2. **SSH key** — you authenticate to pods with an SSH keypair **on your PC**
   (RunPod injects your public key into the pod). Put the **path to your private
   key** in the form (default `~/.ssh/id_ed25519`); the private key never leaves
   your machine. Add the matching **public** key to your RunPod account. Generate
   one with `ssh-keygen -t ed25519` if you don't have it.
3. The `runpod` python package is auto-installed into ai-toolkit's venv on first run.

## Usage

Create a job as usual, flip **Train remotely on RunPod**, pick a GPU + cloud type,
then start the job/queue.

## Behaviour

- **Continuous download** — checkpoints, samples and `loss_log.db` are rsynced from
  the pod into the local job folder every poll (~15s) and again at the very end, so
  if anything crashes the latest progress is already on your disk.
- **Auto-terminate** — the pod is always terminated on finish, error, crash or stop
  (no prompt) to avoid runaway billing. The pod id is also recorded in a sidecar
  file the moment it's created, so a hard-killed orchestrator (e.g. Windows
  `taskkill /F` on Stop) can't permanently leak a billing pod — see Platform notes.
- **Reconnect** — training runs under `nohup` on the pod, so a dropped SSH
  connection does not stop it. The orchestrator keeps retrying and resumes
  monitoring/syncing when the connection returns (giving up only after ~30 min of
  continuous unreachability, so a dead pod can't hang forever).
- **Serialized** — RunPod jobs share one queue lane (`gpu_ids = "runpod"`): one pod
  at a time, to keep cost predictable.

## Platform support

Works on **Linux, macOS and Windows**. The orchestrator talks to pods with
`ssh`/`scp` (called as argument lists, no shell), and uses `rsync` when available
or `scp` otherwise.

- **Windows** needs the built-in **OpenSSH client** (Windows 10 1809+ ships it;
  enable via *Settings → Apps → Optional features → OpenSSH Client* if missing).
- **Stop on Windows:** ai-toolkit stops a job by force-killing the process
  (`taskkill /F`), which can't run cleanup. The pod is then terminated **the next
  time you start any RunPod job**, or immediately by running the reaper:

  ```bash
  python ui_scripts/runpod_train.py --reap --training-folder <your training folder>
  ```

  (On Linux/macOS, Stop terminates the pod immediately.) Either way you can always
  see/terminate pods in the RunPod console.

## Limitations (v1)

- Use a **Hugging Face model id** for `name_or_path` (e.g. `ostris/Flex.1-alpha`);
  local model paths are not uploaded to the pod.
- Dataset **mask/control paths** are not uploaded (only the image folder).
- Each run installs ai-toolkit on the pod (~a few minutes) — same tradeoff as the
  standalone trainer; a prebuilt image could remove this later.

## What gets added

New files (copied by the installer):
`ui/src/components/RunpodSection.tsx`,
`ui/src/app/api/runpod/{gpus,config}/route.ts`,
`ui/cron/actions/startRunpodJob.ts`,
`ui_scripts/runpod_train.py` (the orchestrator).

Hooked files (2 small edits):
`ui/src/app/jobs/new/SimpleJob.tsx`, `ui/cron/actions/startJob.ts`.
