"""Meta (implementer) writes a controller update from a change request.

    py meta_implement.py --request "make the route 20% faster" --base tugs/tug_v1.py --out tugs/tug_meta_1.py

The output is untrusted: only `math` and `sim` imports are allowed, no file/process/network
access, and it must define Controller.step. It then goes through verify.py like any candidate.
"""
import argparse, ast, sys
from pathlib import Path
import agents, llm

ALLOWED_IMPORTS = {"math", "sim"}
BANNED_NAMES = {"open", "exec", "eval", "compile", "__import__", "globals", "getattr", "setattr"}


def sanitize(code):
    tree = ast.parse(code)
    for n in ast.walk(tree):
        if isinstance(n, ast.Import) and any(a.name.split(".")[0] not in ALLOWED_IMPORTS for a in n.names):
            raise ValueError(f"disallowed import line {n.lineno}")
        if isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] not in ALLOWED_IMPORTS:
            raise ValueError(f"disallowed import line {n.lineno}")
        if isinstance(n, ast.Name) and n.id in BANNED_NAMES:
            raise ValueError(f"disallowed name {n.id} line {n.lineno}")
        if isinstance(n, ast.Attribute) and n.attr.startswith("__"):
            raise ValueError(f"dunder access line {n.lineno}")
    if not any(isinstance(n, ast.ClassDef) and n.name == "Controller" and
               any(isinstance(f, ast.FunctionDef) and f.name == "step" for f in n.body) for n in tree.body):
        raise ValueError("no Controller.step")
    return code


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--request", required=True)
    ap.add_argument("--base", default="tugs/tug_v1.py")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    base = Path(a.base).read_text()
    r = agents.implementer("You maintain the Python controller of an autonomous airport "
         "baggage tractor. Return ONE complete Python file in a ```python block. Keep the exact API: "
         "class Controller with step(self, obs) -> {'target': (x, y), 'speed': m/s}. Only import math "
         "and names from sim (dist, poly_sdist, footprint_dist, seg_dist).").ask(
        f"Change request: {a.request}\n\nCurrent controller:\n```python\n{base}```")
    if not r:
        sys.exit("no META_API_KEY and no cached response - set META_API_KEY")
    code = sanitize(llm.extract_code(r["text"]))
    llm.note(f"wrote {len(code.splitlines())} lines")
    header = (f'"""GENERATED LIVE by Meta ({llm.META_MODEL}) via meta_implement.py.\n'
              f'Request: {a.request}\nBase: {a.base}  llm_cache={r["hash"][:16]}"""\n')
    Path(a.out).write_text(header + code)
    print(f"wrote {a.out} ({len(code.splitlines())} lines). Next: py verify.py {a.out}")
