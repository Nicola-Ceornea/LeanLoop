"""`leanloop kat` — anchor an executable Lean spec to official test vectors.

The one spec-error class no probe can catch is WRONGNESS: a precise, strong,
non-vacuous spec about the WRONG property. The only fix is grounding the spec
in external truth. For crypto that truth is Known-Answer Tests (KATs) — the
standard's official input→output vectors (e.g. NIST SLH-DSA / FIPS-205).

Mechanism (the ML-KEM / SymCrypt "executable and testable specs" pattern):
the project exposes its Lean spec as an EXECUTABLE adapter
`spec_fn : List UInt8 → List UInt8`; LeanLoop generates a tiny Lean `main` that
runs it on every KAT input and checks byte-equality against the expected
output, natively (`lake env lean --run`). If the spec reproduces the official
vectors byte-for-byte, and a kernel-checked refinement theorem ties the spec to
the extracted code, then KAT-grounding is TRANSITIVE: the verified code is
anchored to the standard, not just to a spec someone hoped was right.

What this closes: functional-correctness wrongness on the EXERCISED paths.
What it does NOT close: security properties, unexercised paths, the abstraction
function. (Stated honestly in the report + `--explain`.)

Pure logic (vector parsing, harness generation, output parsing) lives here;
orchestration in cli.cmd_kat.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass
class Vector:
    name: str
    input_hex: str
    expected_hex: str


# --------------------------------------------------------------------------- #
# vector-file parsing (JSONL, two-column hex, NIST .rsp)
# --------------------------------------------------------------------------- #
def parse_vectors(text: str, fmt: str = "auto", *,
                  input_key: str = "input", expected_key: str = "expected") -> list[Vector]:
    """Parse a KAT file into Vectors. Formats:
      - "jsonl"  : one JSON object per line, hex fields input_key/expected_key
      - "hex"    : `name input_hex expected_hex` (or just `input expected`)
      - "rsp"    : NIST response-file `Key = Value` blocks (input_key/expected_key)
      - "auto"   : sniff from the content
    """
    fmt = _sniff(text) if fmt == "auto" else fmt
    if fmt == "jsonl":
        return _parse_jsonl(text, input_key, expected_key)
    if fmt == "rsp":
        return _parse_rsp(text, input_key, expected_key)
    return _parse_hex_columns(text)


def _sniff(text: str) -> str:
    head = "\n".join(l for l in text.splitlines() if l.strip())[:2000]
    if re.search(r"^\s*\{", head):
        return "jsonl"
    if re.search(r"^\s*[A-Za-z_]+\s*=", head, re.M):
        return "rsp"
    return "hex"


_HEX_RE = re.compile(r"^[0-9A-Fa-f]*$")


def _ok_hex(s: str) -> bool:
    return bool(s) and len(s) % 2 == 0 and _HEX_RE.match(s) is not None


def _parse_jsonl(text: str, ik: str, ek: str) -> list[Vector]:
    out = []
    for i, line in enumerate(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        out.append(Vector(d.get("name", f"vec{i}"),
                          d[ik].replace(" ", ""), d[ek].replace(" ", "")))
    return out


def _parse_hex_columns(text: str) -> list[Vector]:
    out = []
    for i, line in enumerate(text.splitlines()):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) == 2:
            name, (inp, exp) = f"vec{i}", parts
        elif len(parts) >= 3:
            name, inp, exp = parts[0], parts[1], parts[2]
        else:
            continue
        out.append(Vector(name, inp, exp))
    return out


def _parse_rsp(text: str, ik: str, ek: str) -> list[Vector]:
    out, cur, n = [], {}, 0
    def flush():
        nonlocal cur, n
        if ik in cur and ek in cur:
            out.append(Vector(cur.get("count", cur.get("Count", f"vec{n}")),
                              cur[ik], cur[ek]))
            n += 1
        cur = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            if not line:
                flush()
            continue
        m = re.match(r"([A-Za-z_]\w*)\s*=\s*(.*)$", line)
        if m:
            cur[m.group(1)] = m.group(2).strip().replace(" ", "")
    flush()
    return out


def validate(vectors: list[Vector]) -> list[str]:
    """Return a list of problems (non-hex / odd-length fields) — fail-closed."""
    problems = []
    for v in vectors:
        if not _ok_hex(v.input_hex):
            problems.append(f"{v.name}: input is not even-length hex")
        if not _ok_hex(v.expected_hex):
            problems.append(f"{v.name}: expected is not even-length hex")
    return problems


# --------------------------------------------------------------------------- #
# Lean harness generation
# --------------------------------------------------------------------------- #
_HARNESS_TEMPLATE = '''-- LeanLoop KAT harness (generated; runs the spec on official vectors).
import __MODULE__

-- the project's executable adapter: spec input bytes -> output bytes
-- (must have type `List UInt8 -> List UInt8`)
private def leanloopSpec : List UInt8 -> List UInt8 := __ADAPTER__

private def hexDigit (c : Char) : Nat :=
  if c.isDigit then c.toNat - '0'.toNat
  else if 'a' ≤ c ∧ c ≤ 'f' then c.toNat - 'a'.toNat + 10
  else if 'A' ≤ c ∧ c ≤ 'F' then c.toNat - 'A'.toNat + 10
  else 0

private partial def hexToBytes (s : String) : List UInt8 :=
  let rec go : List Char → List UInt8
    | hi :: lo :: rest => UInt8.ofNat (hexDigit hi * 16 + hexDigit lo) :: go rest
    | _ => []
  go s.toList

private def byteHex (b : UInt8) : String :=
  let d (n : Nat) : Char := if n < 10 then Char.ofNat ('0'.toNat + n)
                            else Char.ofNat ('a'.toNat + n - 10)
  String.mk [d (b.toNat / 16), d (b.toNat % 16)]

private def bytesToHex (bs : List UInt8) : String := String.join (bs.map byteHex)

structure KV where
  name : String
  input : String
  expected : String

def main (args : List String) : IO UInt32 := do
  let path := args.head!
  let raw ← IO.FS.readFile path
  let mut pass := 0
  let mut fail := 0
  for line in raw.splitOn "\\n" do
    let t := line.trim
    if t.isEmpty then continue
    -- vectors passed to the harness as `name<TAB>input_hex<TAB>expected_hex`
    let parts := t.splitOn "\\t"
    if parts.length < 3 then continue
    let name := parts[0]!
    let inp := parts[1]!
    let exp := parts[2]!
    let got := bytesToHex (leanloopSpec (hexToBytes inp))
    if got == exp.toLower then
      pass := pass + 1
    else
      fail := fail + 1
      IO.println s!"FAIL {name}: expected {exp.toLower} got {got}"
  IO.println s!"KAT {pass}/{pass + fail} passed"
  return (if fail == 0 then 0 else 1)
'''


def build_harness(module: str, adapter: str) -> str:
    """Generate the Lean KAT harness. `module` is the spec module to import;
    `adapter` is a Lean term of type `List UInt8 → List UInt8` (the project's
    executable view of its spec)."""
    return (_HARNESS_TEMPLATE
            .replace("__MODULE__", module)
            .replace("__ADAPTER__", adapter))


def vectors_to_tsv(vectors: list[Vector]) -> str:
    """Serialize vectors for the harness (tab-separated, lowercase hex)."""
    return "\n".join(f"{v.name}\t{v.input_hex.lower()}\t{v.expected_hex.lower()}"
                     for v in vectors) + "\n"


# --------------------------------------------------------------------------- #
# output parsing
# --------------------------------------------------------------------------- #
_SUMMARY_RE = re.compile(r"KAT\s+(\d+)/(\d+)\s+passed")
_FAIL_RE = re.compile(r"^FAIL\s+(\S+):\s*(.*)$", re.M)


@dataclass
class KatResult:
    passed: int
    total: int
    failures: list[tuple[str, str]]
    raw: str
    ran: bool


def parse_harness_output(out: str) -> KatResult:
    m = _SUMMARY_RE.search(out)
    fails = _FAIL_RE.findall(out)
    if not m:
        return KatResult(0, 0, fails, out, ran=False)
    return KatResult(int(m.group(1)), int(m.group(2)), fails, out, ran=True)
