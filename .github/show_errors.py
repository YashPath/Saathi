"""CI helper: print the interesting tail of a log as one GitHub annotation, so errors are visible in the run summary."""
import re
import sys

lines = open(sys.argv[1], errors="replace").read().splitlines()
hits = [l for l in lines if re.search(r"^e: |error:|Error|FAILED|What went wrong|Exception", l)]
text = "\n".join((hits[:60] or []) + ["--- tail ---"] + lines[-40:])
print("::error title=log::" + text.replace("%", "%25").replace("\r", "").replace("\n", "%0A"))
