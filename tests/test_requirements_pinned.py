"""Supply-chain guard: every line of requirements.txt is name==version, every direct dependency (requirements.in) is pinned, no duplicates,
the Python version is pinned. Run: python tests/test_requirements_pinned.py"""
import os, re, sys
R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def lines(p): return [l.strip() for l in open(os.path.join(R, p), encoding="utf8") if l.strip() and not l.strip().startswith("#")]
norm = lambda n: re.sub(r"[-_.]+", "-", n).lower()
fails, pins = [], {}
for l in lines("requirements.txt"):
    m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([0-9][A-Za-z0-9.!+_-]*)", l)
    if not m: fails.append("not an exact pin: " + l); continue
    n = norm(m.group(1))
    if n in pins: fails.append("duplicate pin: " + n)
    pins[n] = m.group(2)
for l in lines("requirements.in"):
    n = norm(re.split(r"[=<>!~\[ ]", l)[0])
    if n not in pins: fails.append("direct dependency not pinned in requirements.txt: " + n)
if pins.get("limits") != "5.8.0": fails.append("limits must be pinned to 5.8.0 (rate_limits.sweep() reads its internals)")
for dep in ("deprecated", "wrapt", "packaging", "typing-extensions"):
    if dep not in pins: fails.append("limits dependency not pinned: " + dep)
pv = open(os.path.join(R, ".python-version")).read().strip() if os.path.exists(os.path.join(R, ".python-version")) else ""
if not re.fullmatch(r"\d+\.\d+\.\d+", pv): fails.append("python version not pinned to x.y.z in .python-version: %r" % pv)
for f in fails: print("FAIL", f)
print(("PASS" if not fails else "FAIL"), len(pins), "pins,", len(lines("requirements.in")), "direct dependencies, python", pv or "unpinned")
sys.exit(1 if fails else 0)
