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
git clone https://github.com/<you>/ai-toolkit-runpod.git
cd ai-toolkit-runpod
python install.py --ai-toolkit /path/to/ai-toolkit
# then restart the UI:  cd /path/to/ai-toolkit/ui && npm run build_and_start
```

The installer **copies new files** into ai-toolkit and applies **two tiny hooks**
to existing files (`SimpleJob.tsx`, `cron/actions/startJob.ts`). It is idempotent —
re-run it any time, including after you `git pull` ai-toolkit, to re-apply the hooks.

Uninstall with `python uninstall.py --ai-toolkit /path/to/ai-toolkit`.

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
  (no prompt) to avoid runaway billing.
- **Reconnect** — training runs under `nohup` on the pod, so a dropped SSH
  connection does not stop it. The orchestrator keeps retrying and resumes
  monitoring/syncing when the connection returns (giving up only after ~30 min of
  continuous unreachability, so a dead pod can't hang forever).
- **Serialized** — RunPod jobs share one queue lane (`gpu_ids = "runpod"`): one pod
  at a time, to keep cost predictable.

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
