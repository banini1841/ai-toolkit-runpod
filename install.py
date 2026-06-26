#!/usr/bin/env python3
"""Installer for the ai-toolkit RunPod remote-training extension.

Copies the extension's new files into a target ai-toolkit checkout and applies
two small, idempotent hooks to existing files. Safe to re-run (e.g. after a
`git pull` of ai-toolkit).

Usage:
    python install.py --ai-toolkit /path/to/ai-toolkit
    python install.py                      # auto-detect ../ai-toolkit or $AI_TOOLKIT_DIR
"""
import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OVERLAY = os.path.join(HERE, "overlay")

# --- the two hook edits (anchor-based, idempotent) ------------------------- #
SIMPLEJOB = "ui/src/app/jobs/new/SimpleJob.tsx"
STARTJOB = "ui/cron/actions/startJob.ts"

HOOKS = [
    # file, idempotency_marker, anchor, insert_text, position
    (
        SIMPLEJOB, "@/components/RunpodSection",
        "import { isMac } from '@/helpers/basic';",
        "\nimport RunpodSection from '@/components/RunpodSection'; // RUNPOD EXTENSION",
        "after",
    ),
    (
        SIMPLEJOB, "<RunpodSection",
        "{/* Model Configuration Section */}",
        "{/* >>> RUNPOD EXTENSION */}\n"
        "          <RunpodSection\n"
        "            jobConfig={jobConfig}\n"
        "            setJobConfig={setJobConfig}\n"
        "            gpuIDs={gpuIDs}\n"
        "            setGpuIDs={setGpuIDs}\n"
        "          />\n"
        "          {/* <<< RUNPOD EXTENSION */}\n\n          ",
        "before",
    ),
    (
        STARTJOB, "./startRunpodJob",
        "import { resolvePythonPath } from '../pythonPath';",
        "\nimport maybeStartRunpodJob from './startRunpodJob'; // RUNPOD EXTENSION",
        "after",
    ),
    (
        STARTJOB, "maybeStartRunpodJob(job)",
        "const jobID = job.id;",
        "\n\n    // >>> RUNPOD EXTENSION: dispatch remote jobs to the RunPod orchestrator\n"
        "    if (await maybeStartRunpodJob(job)) {\n"
        "      resolve();\n"
        "      return;\n"
        "    }\n"
        "    // <<< RUNPOD EXTENSION",
        "after",
    ),
]


def detect_target(explicit):
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    # auto-detect: an ai-toolkit folder sitting next to this repo
    cand = os.path.abspath(os.path.join(HERE, "..", "ai-toolkit"))
    return cand if valid_target(cand) else None


def prompt_for_target():
    """Ask for the ai-toolkit path interactively when it can't be auto-detected."""
    if not sys.stdin.isatty():
        return None
    print("Could not auto-detect your ai-toolkit folder.")
    while True:
        ans = input("Path to your ai-toolkit folder (blank to cancel): ").strip()
        if not ans:
            return None
        path = os.path.abspath(os.path.expanduser(ans))
        if valid_target(path):
            return path
        print(f"  '{path}' doesn't look like ai-toolkit (need ui/ and run.py). Try again.")


def valid_target(target):
    return target and os.path.isdir(os.path.join(target, "ui")) and os.path.isfile(os.path.join(target, "run.py"))


def copy_overlay(target):
    print("Copying extension files:")
    for root, dirs, files in os.walk(OVERLAY):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fn in files:
            if fn.endswith(".pyc"):
                continue
            src = os.path.join(root, fn)
            rel = os.path.relpath(src, OVERLAY)
            dst = os.path.join(target, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            print(f"  + {rel}")


def patch_file(target, rel, marker, anchor, insert, position):
    path = os.path.join(target, rel)
    if not os.path.isfile(path):
        print(f"  ! missing file (ai-toolkit layout changed?): {rel}")
        return False
    with open(path, encoding="utf-8") as f:
        content = f.read()
    if marker in content:
        print(f"  = already hooked: {rel}")
        return True
    idx = content.find(anchor)
    if idx == -1:
        print(f"  ! ANCHOR NOT FOUND in {rel}: {anchor!r}")
        print("    ai-toolkit changed around this spot — apply this hook manually.")
        return False
    if position == "after":
        at = idx + len(anchor)
        content = content[:at] + insert + content[at:]
    else:
        content = content[:idx] + insert + content[idx:]
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  + hooked: {rel}")
    return True


def main():
    ap = argparse.ArgumentParser(description="Install the ai-toolkit RunPod extension")
    ap.add_argument("--ai-toolkit", help="Path to the ai-toolkit checkout")
    args = ap.parse_args()

    target = detect_target(args.ai_toolkit)
    if not (target and valid_target(target)):
        target = prompt_for_target()
    if not (target and valid_target(target)):
        print("ERROR: could not find a valid ai-toolkit checkout.")
        print("Pass it explicitly:  python install.py --ai-toolkit /path/to/ai-toolkit")
        return 1
    print(f"Target ai-toolkit: {target}\n")

    copy_overlay(target)
    print("\nApplying hooks:")
    ok = all(patch_file(target, rel, marker, anchor, insert, pos)
             for (rel, marker, anchor, insert, pos) in HOOKS)

    print("\nDone." if ok else "\nFinished with warnings (see ! lines above).")
    print("Next: restart the ai-toolkit UI so it picks up the changes")
    print("  cd %s/ui && npm run build_and_start" % target)
    print("(in dev mode `npm run dev` it hot-reloads automatically.)")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
