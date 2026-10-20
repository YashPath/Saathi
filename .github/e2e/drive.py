"""Drive the installed Saathi app on the CI emulator like a person would, saving a screenshot after every step.

Uses only adb (uiautomator dump to find things on screen, input to tap and type). Results go to e2e-out/.
"""
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

OUT = Path("e2e-out")
OUT.mkdir(exist_ok=True)
PKG = "com.yashpathak.saathi"
log = open(OUT / "steps.txt", "w")


def say(msg):
    print(msg, flush=True)
    log.write(msg + "\n")
    log.flush()


def adb(*args, check=False):
    return subprocess.run(["adb", *args], capture_output=True, text=True, check=check).stdout


def nodes():
    adb("shell", "uiautomator", "dump", "/sdcard/ui.xml")
    xml = adb("shell", "cat", "/sdcard/ui.xml")
    try:
        root = ET.fromstring(xml[xml.find("<?xml"):] if "<?xml" in xml else xml)
    except ET.ParseError:
        return []
    found = []
    for n in root.iter("node"):
        b = [int(x) for x in re.findall(r"\d+", n.get("bounds", "[0,0][0,0]"))]
        found.append({"text": n.get("text", ""), "desc": n.get("content-desc", ""), "cls": n.get("class", ""),
                      "x": (b[0] + b[2]) // 2, "y": (b[1] + b[3]) // 2})
    return found


def screen_text():
    return " | ".join(n["text"] or n["desc"] for n in nodes() if n["text"] or n["desc"])


def tap_xy(x, y):
    adb("shell", "input", "tap", str(x), str(y))
    time.sleep(1)


def find(label, last=False):
    hits = [n for n in nodes() if label in (n["text"], n["desc"])]
    return (hits[-1] if last else hits[0]) if hits else None


def tap(label, last=False, wait=10):
    end = time.time() + wait
    while time.time() < end:
        n = find(label, last)
        if n:
            tap_xy(n["x"], n["y"])
            return True
        time.sleep(1)
    say(f"  !! not found on screen: {label!r}")
    return False


def fields():
    return [n for n in nodes() if n["cls"].endswith("EditText")]


def type_into(field, text):
    tap_xy(field["x"], field["y"])
    adb("shell", "input", "text", text.replace(" ", "%s"))
    time.sleep(0.5)


def hide_keyboard():
    if "mInputShown=true" in adb("shell", "dumpsys", "input_method"):
        adb("shell", "input", "keyevent", "KEYCODE_BACK")
        time.sleep(1)


step_no = 0


def shot(name):
    global step_no
    step_no += 1
    path = f"/sdcard/s.png"
    adb("shell", "screencap", "-p", path)
    adb("pull", path, str(OUT / f"{step_no:02d}_{name}.png"))
    (OUT / f"{step_no:02d}_{name}.txt").write_text(screen_text())
    say(f"[{step_no:02d}] {name}: {screen_text()[:600]}")


def crashed():
    out = adb("logcat", "-d", "-b", "crash")
    return PKG in out and "FATAL" in out


def wait_reply(timeout=120):
    time.sleep(2)
    end = time.time() + timeout
    while time.time() < end:
        if "Saathi is working…" not in screen_text():
            return
        time.sleep(3)
    say("  !! reply took longer than %ss" % timeout)


def chat(text):
    f = fields()
    if not f:
        say("  !! chat box not found")
        return
    type_into(f[-1], text)
    hide_keyboard()
    tap("Send")
    wait_reply()


adb("logcat", "-c")
adb("shell", "am", "start", "-n", f"{PKG}/.MainActivity")
time.sleep(6)
shot("open_app")

f = fields()
say(f"  login fields found: {len(f)}")
for i, value in enumerate(["http://10.0.2.2:8000", "919800000001", "123456"]):
    f = fields()  # re-read: the keyboard moves the fields after each tap
    if len(f) > i:
        type_into(f[i], value)
    hide_keyboard()
shot("login_filled")
tap("Log in", last=True)
time.sleep(6)
shot("after_login")

chat("Sharma ji ka 8000 udhaar likh do aur Gupta ka balance batao")
shot("credit_and_balance")
chat("Sharma ne 6000 de diye")
shot("approval_asked")
if tap("Haan", wait=3):
    wait_reply()
    shot("approved")
chat("Maggi add karo, price 14, GST 18, stock 24, reorder 10")
shot("product_added")
chat("2 maggi ka bill banao")
shot("bill")
chat("aaj ka hisaab")
shot("summary")
tap("Dashboard")
time.sleep(10)
shot("dashboard")
tap("Account")
shot("account")
tap("Chat")
time.sleep(3)
shot("chat_again")

(OUT / "logcat.txt").write_text(adb("logcat", "-d", "-v", "time"))
(OUT / "crash.txt").write_text(adb("logcat", "-d", "-b", "crash"))
say("CRASH: yes" if crashed() else "CRASH: none")
sys.exit(0)
