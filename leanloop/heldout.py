"""Fail-closed loading for proof-replay benchmark suites.

A held-out suite never asks a model to regenerate a Lean file.  Each manifest
entry pins one already-kernel-checked proof by source and proof-body hashes.  We
replace only that proof expression with a ``by sorry`` mask,
give the model a prefix ending at the hole, and retain the complete masked file
as the trusted scaffold used by the normal LeanLoop gates.

Manifest format::

    [[case]]
    id = "bits-window-shift"
    module = "Extracted.ForsExtract"
    theorem = "Extracted.ForsExtract.window_shift"
    source_sha256 = "...64 lowercase hex digits..."
    proof_start = 1234  # UTF-8 byte offset, inclusive
    proof_end = 1456    # UTF-8 byte offset, exclusive
    proof_sha256 = "...64 lowercase hex digits..."
    category = "bit-arithmetic"
    difficulty = "medium"
    axiom_whitelist = []

The byte offsets are deliberately redundant with the proof hash.  A source
edit, stale location, invalid UTF-8 boundary, wrong theorem, overlap, or typo in
a case entry aborts the whole load before inference.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore

from .lean_runner import theorem_signatures
from .leantext import mask_comments
from .provers.base import Goal


_REQUIRED_CASE_KEYS = frozenset({
    "id",
    "module",
    "theorem",
    "source_sha256",
    "proof_start",
    "proof_end",
    "proof_sha256",
    "category",
    "difficulty",
    "axiom_whitelist",
})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MODULE_PART_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_']*$")
_LEAN_NAME_RE = r"[A-Za-z_][A-Za-z0-9_'.]*"

# The declaration matcher is intentionally conservative.  Quoted/unicode Lean
# identifiers need a real parser and are not safe benchmark targets until that
# support exists.  Modifiers are included so discovery can report private
# helper theorems, although the normal statement-pinning parser determines
# whether a selected target is actually usable.
_THEOREM_LINE_RE = re.compile(
    rf"^(?P<indent>[ \t]*)"
    rf"(?:@\[[^\]\n]*\][ \t]*)*"
    rf"(?:(?:private|protected|noncomputable|unsafe)[ \t]+)*"
    rf"(?P<kind>theorem|lemma)[ \t]+(?P<name>{_LEAN_NAME_RE})"
)
_NAMESPACE_LINE_RE = re.compile(rf"^[ \t]*namespace[ \t]+(?P<name>{_LEAN_NAME_RE})\b")
_SECTION_LINE_RE = re.compile(
    rf"^[ \t]*section(?:[ \t]+(?P<name>{_LEAN_NAME_RE}))?[ \t]*$"
)
_END_LINE_RE = re.compile(rf"^[ \t]*end(?:[ \t]+(?P<name>{_LEAN_NAME_RE}))?[ \t]*$")

# A proof ends at the next command whose indentation is no deeper than the
# theorem command.  This is Lean's normal off-side layout boundary.  The list is
# broad on purpose: treating a line as a boundary can only make discovery fail
# closed later when the pinned offsets do not match.
_COMMAND_LINE_RE = re.compile(
    r"^[ \t]*(?:@\[|#|"
    r"(?:(?:private|protected|noncomputable|unsafe|local|scoped)[ \t]+)*"
    r"(?:prelude|import|namespace|section|end|universe|variable|variables|"
    r"include|omit|open|export|set_option|attribute|initialize|"
    r"builtin_initialize|declare_syntax_cat|syntax|macro|macro_rules|"
    r"elab|elab_rules|notation|infix|infixl|infixr|prefix|postfix|"
    r"def|abbrev|opaque|theorem|lemma|example|axiom|constant|structure|"
    r"class|inductive|coinductive|instance|deriving|mutual)\b)"
)
_SORRY_RE = re.compile(r"\bsorry\b")
_ADMIT_RE = re.compile(r"\badmit\b")
# A bare ``by sorry`` can itself exceed Lean's default recursion depth while
# elaborating a large dependent goal, even though the real proof builds under
# the declaration's options.  The local option applies only to the disposable
# mask.  A model candidate replaces this entire expression, so it does not gain
# an altered trusted context or acceptance condition.
_PLACEHOLDER = b"by\n  set_option maxRecDepth 16384 in\n    sorry"


class HeldoutError(ValueError):
    """The suite is not a trustworthy held-out benchmark input."""


@dataclass(frozen=True)
class CaseSpec:
    id: str
    module: str
    theorem: str
    source_sha256: str
    proof_start: int
    proof_end: int
    proof_sha256: str
    category: str
    difficulty: str
    axiom_whitelist: tuple[str, ...]


@dataclass(frozen=True)
class ProofSpan:
    """A discovered theorem proof using UTF-8 byte offsets."""

    theorem: str
    proof_start: int
    proof_end: int
    proof_sha256: str


@dataclass(frozen=True)
class PreparedCase:
    """A validated case ready for the ordinary LeanLoop prover stack."""

    case: CaseSpec
    goal: Goal
    gold_source: str
    scaffold: str
    prompt_prefix: str
    module_stem: str
    path: Path
    target_signature: str


@dataclass(frozen=True)
class _Command:
    start: int
    indent: int


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _mask_strings(text: str) -> str:
    """Mask ordinary and Lean raw string literals while preserving offsets.

    ``text`` is normally already comment-masked.  This lexer is deliberately
    used only for token/location discovery; the original text is retained for
    hashing and scaffolding.
    """
    out = list(text)
    i, n = 0, len(text)

    def blank(pos: int) -> None:
        if text[pos] not in "\r\n":
            out[pos] = " "

    while i < n:
        # Lean raw string: r"...", r#"..."#, r##"..."##, ...
        if text[i] == "r":
            j = i + 1
            while j < n and text[j] == "#":
                j += 1
            if j < n and text[j] == '"':
                hashes = text[i + 1:j]
                closing = '"' + hashes
                end = text.find(closing, j + 1)
                stop = n if end < 0 else end + len(closing)
                for pos in range(i, stop):
                    blank(pos)
                i = stop
                continue

        if text[i] != '"':
            i += 1
            continue

        start = i
        i += 1
        escaped = False
        while i < n:
            ch = text[i]
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                i += 1
                break
            i += 1
        for pos in range(start, i):
            blank(pos)

    return "".join(out)


def _code_for_locations(source: str) -> str:
    return _mask_strings(mask_comments(source))


def _indent_width(prefix: str) -> int:
    return len(prefix.expandtabs(8))


def _line_records(code: str) -> list[tuple[int, str]]:
    records: list[tuple[int, str]] = []
    cursor = 0
    for line in code.splitlines(keepends=True):
        records.append((cursor, line.rstrip("\r\n")))
        cursor += len(line)
    # ``splitlines`` produces no record for an empty source and no extra record
    # after a final newline.  Neither is a command boundary.
    return records


def _commands(records: list[tuple[int, str]]) -> list[_Command]:
    out: list[_Command] = []
    for start, line in records:
        if not _COMMAND_LINE_RE.match(line):
            continue
        prefix = line[:len(line) - len(line.lstrip(" \t"))]
        out.append(_Command(start=start, indent=_indent_width(prefix)))
    return out


def _next_boundary(commands: list[_Command], start: int, indent: int, eof: int) -> int:
    for command in commands:
        if command.start > start and command.indent <= indent:
            return command.start
    return eof


def _find_top_level_assign(code: str, start: int, end: int) -> int:
    # Aeneas specifications use the unicode ``⦃ ... ⦄`` syntax for Hoare
    # triples.  Assignments inside such a result type (for example
    # ``let x := by ...``) are not the declaration's proof delimiter.
    pairs = {")": "(", "]": "[", "}": "{", "⟩": "⟨", "⦄": "⦃"}
    opens = set(pairs.values())
    stack: list[str] = []
    i = start
    while i + 1 < end:
        ch = code[i]
        if ch in opens:
            stack.append(ch)
        elif ch in pairs:
            if stack and stack[-1] == pairs[ch]:
                stack.pop()
        elif not stack and code[i:i + 2] == ":=":
            return i
        i += 1
    return -1


def _pop_scope(scopes: list[tuple[str, str]], name: str | None) -> None:
    if not scopes:
        return
    if name is None:
        scopes.pop()
        return
    for idx in range(len(scopes) - 1, -1, -1):
        if scopes[idx][1] == name:
            del scopes[idx:]
            return
    # A named ``end`` we could not pair indicates unsupported scope syntax.
    # Do not guess by discarding a namespace; leave the stack unchanged so a
    # selected theorem will fail its FQN/signature validation instead.


def _namespace_prefix(scopes: list[tuple[str, str]]) -> str:
    return ".".join(name for kind, name in scopes if kind == "namespace")


def _char_to_byte(source: str, offset: int) -> int:
    return len(source[:offset].encode("utf-8"))


def discover_proof_spans(source: str) -> list[ProofSpan]:
    """Discover theorem/lemma proof expressions deterministically.

    Locations are returned as half-open UTF-8 byte offsets suitable for a
    manifest.  Proof starts are the first non-whitespace code character after
    the declaration's top-level ``:=``.  Proof ends are the last non-comment,
    non-whitespace character before the next top-level command boundary.

    Declarations without an explicit top-level ``:=`` (for example equation-
    compiler theorem syntax) are omitted rather than guessed.  Selecting such a
    declaration in a manifest still fails closed because it has no discovered
    span.
    """
    try:
        source.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise HeldoutError(f"Lean source is not encodable as UTF-8: {exc}") from exc

    comment_code = mask_comments(source)
    location_code = _mask_strings(comment_code)
    records = _line_records(location_code)
    commands = _commands(records)
    scopes: list[tuple[str, str]] = []
    spans: list[ProofSpan] = []

    for line_start, line in records:
        namespace = _NAMESPACE_LINE_RE.match(line)
        if namespace:
            scopes.append(("namespace", namespace.group("name")))
            continue
        section = _SECTION_LINE_RE.match(line)
        if section:
            scopes.append(("section", section.group("name") or ""))
            continue
        end_scope = _END_LINE_RE.match(line)
        if end_scope:
            _pop_scope(scopes, end_scope.group("name"))
            continue

        declaration = _THEOREM_LINE_RE.match(line)
        if not declaration:
            continue

        indent = _indent_width(declaration.group("indent"))
        boundary = _next_boundary(commands, line_start, indent, len(source))
        name_end = line_start + declaration.end("name")
        assign = _find_top_level_assign(location_code, name_end, boundary)
        prefix = _namespace_prefix(scopes)
        short_name = declaration.group("name")
        fqn = f"{prefix}.{short_name}" if prefix else short_name
        if assign < 0:
            continue

        proof_start_char = assign + 2
        while proof_start_char < boundary and location_code[proof_start_char].isspace():
            proof_start_char += 1

        # Comments are whitespace in ``comment_code`` but strings remain
        # visible, so a proof that is itself a string term is not trimmed away.
        body_region = comment_code[proof_start_char:boundary]
        proof_end_char = proof_start_char + len(body_region.rstrip())
        if proof_start_char >= proof_end_char:
            continue

        proof_start = _char_to_byte(source, proof_start_char)
        proof_end = _char_to_byte(source, proof_end_char)
        body = source.encode("utf-8")[proof_start:proof_end]
        spans.append(ProofSpan(
            theorem=fqn,
            proof_start=proof_start,
            proof_end=proof_end,
            proof_sha256=_sha256(body),
        ))

    return spans


def _required_string(entry: dict[str, Any], key: str, label: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HeldoutError(f"{label}: `{key}` must be a non-empty string")
    return value


def _required_offset(entry: dict[str, Any], key: str, label: str) -> int:
    value = entry.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise HeldoutError(f"{label}: `{key}` must be an integer UTF-8 byte offset")
    return value


def _parse_case(entry: Any, index: int) -> CaseSpec:
    label = f"case[{index}]"
    if not isinstance(entry, dict):
        raise HeldoutError(f"{label}: entry must be a TOML table")
    missing = sorted(_REQUIRED_CASE_KEYS - entry.keys())
    unknown = sorted(entry.keys() - _REQUIRED_CASE_KEYS)
    if missing:
        raise HeldoutError(f"{label}: missing required field(s): {', '.join(missing)}")
    if unknown:
        raise HeldoutError(f"{label}: unknown field(s): {', '.join(unknown)}")

    case_id = _required_string(entry, "id", label)
    label = f"case `{case_id}`"
    module = _required_string(entry, "module", label)
    theorem = _required_string(entry, "theorem", label)
    source_sha256 = _required_string(entry, "source_sha256", label)
    proof_sha256 = _required_string(entry, "proof_sha256", label)
    category = _required_string(entry, "category", label)
    difficulty = _required_string(entry, "difficulty", label)
    proof_start = _required_offset(entry, "proof_start", label)
    proof_end = _required_offset(entry, "proof_end", label)

    parts = module.split(".")
    if not parts or any(not _MODULE_PART_RE.fullmatch(part) for part in parts):
        raise HeldoutError(f"{label}: unsafe or unsupported module name {module!r}")
    if not _SHA256_RE.fullmatch(source_sha256):
        raise HeldoutError(f"{label}: `source_sha256` must be 64 lowercase hex digits")
    if not _SHA256_RE.fullmatch(proof_sha256):
        raise HeldoutError(f"{label}: `proof_sha256` must be 64 lowercase hex digits")
    if proof_start < 0 or proof_end <= proof_start:
        raise HeldoutError(
            f"{label}: invalid proof range [{proof_start}, {proof_end})"
        )

    whitelist = entry["axiom_whitelist"]
    if not isinstance(whitelist, list) or any(
        not isinstance(item, str) or not item.strip() for item in whitelist
    ):
        raise HeldoutError(f"{label}: `axiom_whitelist` must be a list of non-empty strings")
    if len(set(whitelist)) != len(whitelist):
        raise HeldoutError(f"{label}: `axiom_whitelist` contains duplicate names")

    return CaseSpec(
        id=case_id,
        module=module,
        theorem=theorem,
        source_sha256=source_sha256,
        proof_start=proof_start,
        proof_end=proof_end,
        proof_sha256=proof_sha256,
        category=category,
        difficulty=difficulty,
        axiom_whitelist=tuple(whitelist),
    )


def _parse_manifest(path: Path) -> list[CaseSpec]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise HeldoutError(f"cannot read held-out manifest {path}: {exc}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HeldoutError(f"held-out manifest is not valid UTF-8: {exc}") from exc
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        raise HeldoutError(f"invalid held-out TOML {path}: {exc}") from exc

    entries = data.get("case")
    if not isinstance(entries, list) or not entries:
        raise HeldoutError("held-out manifest must contain at least one `[[case]]` entry")
    specs = [_parse_case(entry, index) for index, entry in enumerate(entries)]

    seen_ids: set[str] = set()
    seen_targets: set[tuple[str, str]] = set()
    ranges: dict[str, list[tuple[int, int, str]]] = {}
    for spec in specs:
        if spec.id in seen_ids:
            raise HeldoutError(f"duplicate held-out case id {spec.id!r}")
        seen_ids.add(spec.id)
        target = (spec.module, spec.theorem)
        if target in seen_targets:
            raise HeldoutError(
                f"duplicate held-out target `{spec.theorem}` in module `{spec.module}`"
            )
        seen_targets.add(target)
        for start, end, other_id in ranges.setdefault(spec.module, []):
            if spec.proof_start < end and start < spec.proof_end:
                raise HeldoutError(
                    f"overlapping proof ranges in module `{spec.module}`: "
                    f"{other_id!r} and {spec.id!r}"
                )
        ranges[spec.module].append((spec.proof_start, spec.proof_end, spec.id))
    return specs


def _module_path(root: Path, module: str) -> Path:
    path = (root / Path(*module.split(".")).with_suffix(".lean")).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HeldoutError(f"module `{module}` resolves outside project root {root}") from exc
    return path


def _decode_source(source_bytes: bytes, spec: CaseSpec) -> str:
    try:
        return source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HeldoutError(f"case `{spec.id}`: module source is not valid UTF-8: {exc}") from exc


def _validate_boundaries(source_bytes: bytes, spec: CaseSpec) -> tuple[int, int]:
    if spec.proof_end > len(source_bytes):
        raise HeldoutError(
            f"case `{spec.id}`: proof range [{spec.proof_start}, {spec.proof_end}) "
            f"exceeds {len(source_bytes)} source bytes"
        )
    try:
        prefix = source_bytes[:spec.proof_start].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HeldoutError(
            f"case `{spec.id}`: proof_start {spec.proof_start} is not a UTF-8 boundary"
        ) from exc
    try:
        through_proof = source_bytes[:spec.proof_end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HeldoutError(
            f"case `{spec.id}`: proof_end {spec.proof_end} is not a UTF-8 boundary"
        ) from exc
    return len(prefix), len(through_proof)


def _masked_scaffold(source_bytes: bytes, spec: CaseSpec) -> tuple[str, tuple[int, int]]:
    # The prefix and suffix are trusted verbatim source slices.  Do not pad or
    # relocate newlines inside the removed body: doing so can place the next
    # top-level command after spaces on the placeholder's line.  The scaffold
    # may differ in length, but bytes outside the selected span are not
    # regenerated or rewritten.
    masked_bytes = (
        source_bytes[:spec.proof_start]
        + _PLACEHOLDER
        + source_bytes[spec.proof_end:]
    )
    try:
        scaffold = masked_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:  # should be unreachable after boundary validation
        raise HeldoutError(f"case `{spec.id}`: masked scaffold is not valid UTF-8") from exc

    hole_start = len(source_bytes[:spec.proof_start].decode("utf-8"))
    return scaffold, (hole_start, hole_start + len(_PLACEHOLDER.decode("ascii")))


def _real_token_counts(source: str) -> tuple[int, int]:
    code = _code_for_locations(source)
    return len(_SORRY_RE.findall(code)), len(_ADMIT_RE.findall(code))


def _prepare_case(root: Path, spec: CaseSpec) -> PreparedCase:
    path = _module_path(root, spec.module)
    try:
        source_bytes = path.read_bytes()
    except OSError as exc:
        raise HeldoutError(f"case `{spec.id}`: cannot read module source {path}: {exc}") from exc

    actual_source_hash = _sha256(source_bytes)
    if actual_source_hash != spec.source_sha256:
        raise HeldoutError(
            f"case `{spec.id}`: source_sha256 mismatch: expected {spec.source_sha256}, "
            f"got {actual_source_hash}"
        )
    source = _decode_source(source_bytes, spec)
    _validate_boundaries(source_bytes, spec)

    proof_bytes = source_bytes[spec.proof_start:spec.proof_end]
    actual_proof_hash = _sha256(proof_bytes)
    if actual_proof_hash != spec.proof_sha256:
        raise HeldoutError(
            f"case `{spec.id}`: proof_sha256 mismatch: expected {spec.proof_sha256}, "
            f"got {actual_proof_hash}"
        )

    gold_sorries, gold_admits = _real_token_counts(source)
    if gold_sorries or gold_admits:
        raise HeldoutError(
            f"case `{spec.id}`: golden source contains real proof placeholder(s): "
            f"sorry={gold_sorries}, admit={gold_admits}"
        )

    discovered = [span for span in discover_proof_spans(source) if span.theorem == spec.theorem]
    if len(discovered) != 1:
        raise HeldoutError(
            f"case `{spec.id}`: target theorem `{spec.theorem}` has "
            f"{len(discovered)} discoverable proof spans (expected exactly one)"
        )
    span = discovered[0]
    if (span.proof_start, span.proof_end) != (spec.proof_start, spec.proof_end):
        raise HeldoutError(
            f"case `{spec.id}`: stale/wrong proof offsets for `{spec.theorem}`: "
            f"manifest [{spec.proof_start}, {spec.proof_end}), discovered "
            f"[{span.proof_start}, {span.proof_end})"
        )
    if span.proof_sha256 != spec.proof_sha256:
        raise HeldoutError(
            f"case `{spec.id}`: discovered proof hash disagrees with manifest"
        )

    gold_signatures = theorem_signatures(source)
    if spec.theorem not in gold_signatures:
        raise HeldoutError(
            f"case `{spec.id}`: target theorem `{spec.theorem}` has no pinnable signature"
        )

    scaffold, hole = _masked_scaffold(source_bytes, spec)
    scaffold_signatures = theorem_signatures(scaffold)
    if scaffold_signatures != gold_signatures:
        raise HeldoutError(
            f"case `{spec.id}`: masking changed one or more theorem signatures"
        )
    scaffold_sorries, scaffold_admits = _real_token_counts(scaffold)
    if scaffold_sorries != 1 or scaffold_admits != 0:
        raise HeldoutError(
            f"case `{spec.id}`: masked scaffold must contain exactly one real `sorry` "
            f"and no `admit` (got sorry={scaffold_sorries}, admit={scaffold_admits})"
        )
    if scaffold[hole[0]:hole[1]] != _PLACEHOLDER.decode("ascii"):
        raise HeldoutError(f"case `{spec.id}`: internal proof-hole construction failed")

    prompt_prefix = scaffold[:hole[1]]
    goal = Goal(
        name=spec.id,
        file_text=scaffold,
        target_module=spec.module,
        prompt_text=prompt_prefix,
        target_fqns=[spec.theorem],
        proof_hole=hole,
        axiom_whitelist=list(spec.axiom_whitelist),
    )
    return PreparedCase(
        case=spec,
        goal=goal,
        gold_source=source,
        scaffold=scaffold,
        prompt_prefix=prompt_prefix,
        module_stem=spec.module,
        path=path,
        target_signature=gold_signatures[spec.theorem],
    )


def load_suite(project_root: str | Path, manifest_path: str | Path) -> list[PreparedCase]:
    """Load and validate every case, aborting the entire suite on any drift.

    Validation occurs before this function returns, so callers must not start
    inference while iterating a partially trusted manifest.
    """
    root = Path(project_root).resolve()
    if not root.is_dir():
        raise HeldoutError(f"Lean project root is not a directory: {root}")
    specs = _parse_manifest(Path(manifest_path).resolve())
    return [_prepare_case(root, spec) for spec in specs]
