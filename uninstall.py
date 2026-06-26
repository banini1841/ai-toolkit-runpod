#!/usr/bin/env python3
"""Uninstaller for the ai-toolkit RunPod remote-training extension.

Removes the copied-in files and strips the two hooks from the tracked files.

Usage:
    python uninstall.py --ai-toolkit /path/to/ai-toolkit
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OVERLAY = os.path.join(HERE, "overlay")

SIMPLEJOB = "ui/src/app/jobs/new/SimpleJob.tsx"
STARTJOB = "ui/cron/actions/startJob.ts"


def valid_target(target):
    return bool(target) and os.path.isdir(os.path.join(target, "ui")) and os.path.isfile(os.path.join(target, "run.py"))


def detect_target(explicit):
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    cand = os.path.abspath(os.path.join(HERE, "..", "ai-toolkit"))
    return cand if valid_target(cand) else None


def prompt_for_target():
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


def remove_overlay(target):
    print("Removing extension files:")
    for root, _, files in os.walk(OVERLAY):
        for fn in files:
            rel = os.path.relpath(os.path.join(root, fn), OVERLAY)
            dst = os.path.join(target, rel)
            if os.path.isfile(dst):
                os.remove(dst)
                print(f"  - {rel}")
    # prune the now-empty api/runpod dir
    rp = os.path.join(target, "ui/src/app/api/runpod")
    for d in (os.path.join(rp, "gpus"), os.path.join(rp, "config"), rp):
        if os.path.isdir(d) and not os.listdir(d):
            os.rmdir(d)


def unhook(target, rel):
    path = os.path.join(target, rel)
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        c = f.read()
    # remove the exact text install.py inserted (block bodies first, then imports)
    c = re.sub(r"\{/\* >>> RUNPOD EXTENSION \*/\}.*?\{/\* <<< RUNPOD EXTENSION \*/\}\n\n[ \t]*", "", c, flags=re.S)
    c = re.sub(r"\n\n[ \t]*// >>> RUNPOD EXTENSION.*?// <<< RUNPOD EXTENSION", "", c, flags=re.S)
    c = re.sub(r"\n[^\n]*// RUNPOD EXTENSION[^\n]*", "", c)
    with open(path, "w", encoding="utf-8") as f:
        f.write(c)
    print(f"  - unhooked: {rel}")


def main():
    ap = argparse.ArgumentParser(description="Uninstall the ai-toolkit RunPod extension")
    ap.add_argument("--ai-toolkit", help="Path to the ai-toolkit checkout")
    args = ap.parse_args()
    target = detect_target(args.ai_toolkit)
    if not (target and valid_target(target)):
        target = prompt_for_target()
    if not (target and valid_target(target)):
        print("ERROR: ai-toolkit checkout not found. Pass --ai-toolkit /path")
        return 1
    print(f"Target ai-toolkit: {target}\n")
    remove_overlay(target)
    print("\nStripping hooks:")
    unhook(target, SIMPLEJOB)
    unhook(target, STARTJOB)
    print("\nDone. Restart the UI to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
