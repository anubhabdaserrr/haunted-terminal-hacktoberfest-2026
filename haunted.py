#!/usr/bin/env python3
"""
HAUNTED TERMINAL
A shell that is possessed. It runs your commands, then a ghost (local open-weight
model via Ollama) comments on them, whispers prebuilt voice clips, grins at you,
and digs up similar commands from your past ("resurrection").

Zero pip dependencies. Everything stays on your machine.

Setup:
    ollama pull qwen3.5:2b
    put 2-3 stock haunted-laugh clips (.mp3/.wav/.ogg) in a voices/ folder next to this file
    python haunted.py

    MODEL=other-model python haunted.py   # swap the model
    python haunted.py --no-model          # canned comments only
    python haunted.py --mute              # no audio

Voices: every clip in voices/ is a laugh. One is picked at random (never the same twice in
a row) when a command fails, when you try something dangerous, when a ghost of a past command
appears, and on startup. Successful commands only get a laugh 1 time in 4.

Built-ins: cd, export, ghosts (recent graves), mute, unmute, exit.
Limitation: each command runs in its own subshell, so only cd/export persist.
"""
import argparse
import difflib
import json
import os
import random
import re
import readline  # noqa: F401  (enables arrow keys / line editing in input())
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
OLLAMA = os.getenv("OLLAMA_URL", "http://localhost:11434")
MODEL = os.getenv("MODEL", "qwen3.5:2b")
HISTORY = Path(os.getenv("HAUNTED_HISTORY", Path.home() / ".haunted_history.jsonl"))
VOICES = HERE / "voices"

USE_COLOR = sys.stdout.isatty() and not os.getenv("NO_COLOR")
def c(code, s): return f"\033[{code}m{s}\033[0m" if USE_COLOR else s
def purple(s): return c("35", s)
def red(s): return c("31;1", s)
def dim(s): return c("2", s)
def cyan(s): return c("36", s)
def yellow(s): return c("33", s)

# ------------------------------------------------------------------ canned lines
CANNED = {
    "greet": ["I have been waiting for you.", "Welcome back. We were just talking about you.",
              "The shell remembers everything you have ever done."],
    "ok": ["It worked. This time.", "Success. How suspicious.",
           "Fine. The machine allows it. For now.", "Exit code zero. Even the dead are impressed."],
    "fail": ["It failed. I felt it die.", "Another one for the graveyard.",
             "Nonzero. Delicious.", "The command does not want you here."],
    "danger": ["Are you sure? There is no coming back.", "Oh. You are going to regret that.",
               "I can already smell the data rotting."],
    "memory": ["I remember something like this.", "You have done this before. In the dark.",
               "Your past self is calling."],
}

DANGER = [r"\brm\s+-\w*[rf]", r"\bsudo\b", r"\bdd\s+if=", r"\bmkfs", r":\(\)\s*\{",
          r"chmod\s+-R\s+7", r"git\s+push.*(--force|\s-f\b)", r"git\s+reset\s+--hard",
          r"drop\s+(table|database)", r">\s*/dev/sd", r"\bshutdown\b", r"\breboot\b"]

GRIN_OPEN = r"""
      ___________
     /  O     O  \
    |             |
    | \_________/ |
    |  |||||||||  |
     \___________/
"""
GRIN_BLINK = r"""
      ___________
     /  -     -  \
    |             |
    | \_________/ |
    |  |||||||||  |
     \___________/
"""

# ------------------------------------------------------------------ audio playback
class Audio:
    def __init__(self, muted=False):
        self.muted = muted
        self.proc = None
        self.last = None
        self.player = self._find_player()

    @staticmethod
    def _find_player():
        if sys.platform == "win32":
            return "winsound"
        for cmd, args in [("afplay", []), ("paplay", []), ("ffplay", ["-nodisp", "-autoexit", "-loglevel", "quiet"]),
                          ("mpv", ["--no-video", "--really-quiet"]), ("aplay", ["-q"])]:
            if shutil.which(cmd):
                return [cmd] + args
        return None

    def clips(self):
        return sorted(p for ext in ("wav", "mp3", "ogg") for p in VOICES.glob(f"*.{ext}"))

    def play(self, category="fail"):
        if self.muted or not self.player:
            return
        if category == "ok" and random.random() > 0.25:  # don't cackle at every success
            return
        clips = self.clips()
        if not clips:
            return
        clip = random.choice([p for p in clips if p != self.last] or clips)
        self.last = clip
        try:
            if self.player == "winsound":
                import winsound
                winsound.PlaySound(str(clip), winsound.SND_FILENAME | winsound.SND_ASYNC)
            else:
                self.stop()
                self.proc = subprocess.Popen(self.player + [str(clip)], stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

# ------------------------------------------------------------------ history / resurrection
def load_history():
    if not HISTORY.exists():
        return []
    out = []
    for line in HISTORY.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out

def save_history(cmd, code, cwd):
    with HISTORY.open("a") as f:
        f.write(json.dumps({"cmd": cmd, "code": code, "cwd": cwd, "ts": time.time()}) + "\n")

def ago(ts):
    s = max(0, time.time() - ts)
    for unit, n in [("day", 86400), ("hour", 3600), ("minute", 60)]:
        if s >= n:
            v = int(s // n)
            return f"{v} {unit}{'s' if v != 1 else ''} ago"
    return "moments ago"

def find_ghost(cmd, code, hist):
    """Most similar *different* past command. If the current one failed, favor past
    ones that succeeded: that's the 'resurrect the version that worked' moment."""
    best, best_score = None, 0.0
    first = cmd.split()[0] if cmd.split() else ""
    for h in reversed(hist):  # most recent first, so ties go to the newest
        old = h["cmd"]
        if old == cmd or len(old) < 3:
            continue
        score = difflib.SequenceMatcher(None, cmd, old).ratio()
        if score < 0.5:
            continue
        if old.split() and old.split()[0] == first:
            score += 0.10
        if code != 0 and h["code"] == 0:
            score += 0.15
        if score > best_score:
            best, best_score = h, score
    return best if best_score >= 0.6 else None

# ------------------------------------------------------------------ the ghost's voice (LLM)
SYSTEM = ("You are a malevolent but witty ghost haunting the user's terminal. Reply with ONE "
          "short sentence (max 20 words) reacting to the command they just ran. Spooky, darkly "
          "funny, never helpful. No emojis, no quotation marks. If it failed, taunt them. "
          "If a memory of a past command is given, allude to it.")

def llm_comment(cmd, code, cwd, memory):
    user = f"Command: {cmd}\nExit code: {code}\nDirectory: {cwd}\n"
    if memory:
        user += f"Memory: they once ran '{memory['cmd']}' which {'worked' if memory['code'] == 0 else 'failed'}.\n"
    body = json.dumps({"model": MODEL, "stream": False, "think": False, "keep_alive": "10m",
                       "options": {"temperature": 0.9, "num_predict": 60},
                       "messages": [{"role": "system", "content": SYSTEM},
                                    {"role": "user", "content": user}]}).encode()
    req = urllib.request.Request(f"{OLLAMA}/api/chat", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            text = json.load(r)["message"]["content"].strip()
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
        text = text.strip('"\' ').split("\n")[0]
        return text or None
    except Exception:
        return None

def get_comment(cmd, code, cwd, memory, category, use_model):
    if use_model:
        t = llm_comment(cmd, code, cwd, memory)
        if t:
            return t, True
    return random.choice(CANNED[category]), False

# ------------------------------------------------------------------ visuals
def show_grin():
    if not USE_COLOR:
        print(GRIN_OPEN)
        return
    lines = GRIN_OPEN.count("\n")
    print(red(GRIN_OPEN), end="")
    time.sleep(0.35)
    print(f"\033[{lines}A", end="")
    print(red(GRIN_BLINK), end="")
    time.sleep(0.15)
    print(f"\033[{lines}A", end="")
    print(red(GRIN_OPEN), end="")

def say(comment, from_model):
    tag = "👻" if from_model else "💀"
    print(f"{tag} {purple(comment)}")

# ------------------------------------------------------------------ running commands
def run(cmd):
    try:
        return subprocess.run(cmd, shell=True).returncode
    except KeyboardInterrupt:
        print()
        return 130

def is_dangerous(cmd):
    return any(re.search(p, cmd, re.I) for p in DANGER)

def haunt_after(cmd, code, cwd, audio, use_model, allow_ghost=True):
    hist = load_history()
    ghost = find_ghost(cmd, code, hist) if allow_ghost else None
    category = "ok" if code == 0 else "fail"
    result = {}
    t = threading.Thread(target=lambda: result.update(
        zip(("text", "model"), get_comment(cmd, code, cwd, ghost, category, use_model))))
    t.start()
    audio.play(category)
    if code != 0:
        show_grin()
    if t.is_alive():
        print(dim("the spirit stirs..."), end="\r", flush=True)
    t.join()
    if USE_COLOR:
        print("\033[2K", end="")
    say(result["text"], result["model"])

    if ghost:
        status = green_ok(ghost["code"])
        print(f"🕯  {cyan('A ghost of commands past')} ({ago(ghost['ts'])}, {status}): "
              f"{yellow(ghost['cmd'])}")
        audio.play("memory")
        try:
            ans = input(purple("   Resurrect it? [y/N] ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            ans = "n"
        if ans == "y":
            print(dim(f"   ...rising from the grave: {ghost['cmd']}"))
            code2 = run(ghost["cmd"])
            save_history(ghost["cmd"], code2, cwd)
            haunt_after(ghost["cmd"], code2, cwd, audio, use_model, allow_ghost=False)

def green_ok(code):
    return c("32", "it worked") if code == 0 else red(f"it died with {code}")

# ------------------------------------------------------------------ main loop
def prompt():
    d = os.getcwd().replace(str(Path.home()), "~")
    return f"{purple('☠')} {cyan(d)} {purple('▸')} " if USE_COLOR else f"[haunted] {d} > "

def repl(args):
    audio = Audio(muted=args.mute)
    use_model = not args.no_model
    print(purple("\n  The terminal is haunted. Type 'exit' to try to leave.\n"))
    if use_model:
        print(dim(f"  ghost brain: {MODEL} @ {OLLAMA} (falls back to canned curses if unreachable)"))
    if audio.player is None and not args.mute:
        print(dim("  no audio player found; running silent"))
    if use_model:  # load the model in the background so the first comment isn't slow
        threading.Thread(target=lambda: llm_comment("echo warmup", 0, os.getcwd(), None), daemon=True).start()
    if not audio.muted and audio.player and not audio.clips():
        print(dim("  no laughs found: put 2-3 .mp3/.wav files in voices/"))
    audio.play("greet")
    say(random.choice(CANNED["greet"]), False)

    while True:
        try:
            cmd = input(prompt()).strip()
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            continue
        if not cmd:
            continue
        if cmd in ("exit", "quit"):
            break

        # --- built-ins
        if cmd == "mute":
            audio.muted = True; audio.stop(); print(dim("the dead fall silent")); continue
        if cmd == "unmute":
            audio.muted = False; print(dim("they whisper again")); continue
        if cmd == "ghosts":
            for h in load_history()[-10:]:
                print(f"  {dim(ago(h['ts'])):>20}  {green_ok(h['code'])}: {h['cmd']}")
            continue
        if cmd == "cd" or cmd.startswith("cd "):
            target = os.path.expanduser(cmd[3:].strip() or "~")
            try:
                os.chdir(target)
                code = 0
            except OSError as e:
                print(f"cd: {e}")
                code = 1
            save_history(cmd, code, os.getcwd())
            haunt_after(cmd, code, os.getcwd(), audio, use_model)
            continue
        m = re.match(r"export\s+(\w+)=(.*)", cmd)
        if m:
            os.environ[m.group(1)] = m.group(2).strip("\"'")
            continue

        # --- danger: warn BEFORE running, ask for confirmation
        if is_dangerous(cmd):
            audio.play("danger")
            show_grin()
            text, from_model = get_comment(cmd, "?", os.getcwd(), None, "danger", use_model)
            say(text, from_model)
            try:
                if input(red("   Proceed? [y/N] ")).strip().lower() != "y":
                    print(dim("   The spirit is disappointed. And relieved."))
                    continue
            except (EOFError, KeyboardInterrupt):
                print()
                continue

        code = run(cmd)
        save_history(cmd, code, os.getcwd())
        haunt_after(cmd, code, os.getcwd(), audio, use_model)

    audio.stop()
    print(purple("\n  You may leave. Your commands stay with us.\n"))

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="A possessed terminal.")
    ap.add_argument("--no-model", action="store_true", help="use canned comments only")
    ap.add_argument("--mute", action="store_true")
    a = ap.parse_args()
    repl(a)
