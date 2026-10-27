---
name: check-cowping
description: Verify a change to cowping.py before committing. Runs the compile check, the Python-compatibility checks, the comment-only diff check, and the manual ping test runs from CLAUDE.md. Use after editing cowping.py, or when asked to test, check or verify cowping.
---

# Check cowping

cowping has no test suite. Run these checks after every change to `cowping.py` and report each result. If a check fails, show its output; don't just say it failed.

## 1. It compiles

```bash
python3 -m py_compile cowping.py
```

## 2. It still fits the compatibility rules

CLAUDE.md requires Python 3.3+ and the standard library only. Search for features that break that:

```bash
grep -nE '(^|[^a-zA-Z0-9_-])[fF][rR]?["'"'"']|:=|subprocess\.run|pathlib|dataclass|\)\s*->' cowping.py
grep -nE '^\s*(import|from)\s' cowping.py
```

The first search should find nothing. Every import from the second must be a standard library module.

## 3. Comment-only edits changed no code

When a change was meant to touch only comments or docstrings, prove it by comparing syntax trees with the docstrings stripped. Save the old version first with `git show HEAD:cowping.py > "$SCRATCH/before.py"` (any temporary path works), then:

```bash
python3 -I - "$SCRATCH/before.py" cowping.py <<'EOF'
import ast, sys
def strip(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(getattr(body[0], "value", None), ast.Constant) \
                and isinstance(body[0].value.value, str):
            node.body = body[1:] or [ast.Pass()]
    return ast.dump(tree)
print("code identical" if strip(sys.argv[1]) == strip(sys.argv[2]) else "CODE DIFFERS")
EOF
```

## 4. Manual runs

```bash
COLUMNS=90 python3 cowping.py --no-color -c 3 -t 0.5 -f hosts.example.csv
COLUMNS=60 python3 cowping.py --no-color -c 1 127.0.0.1
python3 cowping.py --no-color --no-banner -c 3 127.0.0.1 10.255.255.1 no.such.host.invalid
```

Check that:

- every frame line has the same width, so the right border is straight
- at 60 columns the clock is stacked under the cow
- 127.0.0.1 shows `!` and no.such.host.invalid shows `?`
- 10.255.255.1 should show `.` (timeout). Some networks and sandboxes report it as `?` (error) instead. If so, compare with `git stash` before blaming the change.

Colour mode needs a real terminal, so it can't be checked from here. Ask the user to run `python3 cowping.py -c 5 127.0.0.1 10.255.255.1` if the change touched colours or redrawing.
