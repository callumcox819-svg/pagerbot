"""Lock links, promo codes, and amounts — never let LLM alter them."""

from __future__ import annotations

import re
from pathlib import Path

from config import SCRIPTS_DIR
from services.script_engine import load_script, resolve_cm_key, resolve_eg_key

_URL_RE = re.compile(
    r"https?://[^\s<>\"']+|tinyurl\.com/[^\s<>\"']+|t\.me/\+[^\s<>\"']+|t\.me/[^\s<>\"']+",
    re.IGNORECASE,
)
_PROMO_RE = re.compile(r"\b[A-Z]{2,}\d[A-Z0-9]{1,}\b")
_AMOUNT_RE = re.compile(
    r"\d[\d\s.,]*\s*(?:ZMW|CFA|FCFA|DJF|EGP|USD|FCFA|francs?)\b",
    re.IGNORECASE,
)
_PLACEHOLDER_RE = re.compile(r"\{\{LOCK_\d+\}\}")


def _script_paths(geo: str) -> list[Path]:
    g = (geo or "zm").strip().lower()
    root = SCRIPTS_DIR / g
    if not root.is_dir():
        return []
    paths = list(root.glob("*.txt"))
    extras = root / "extras"
    if extras.is_dir():
        paths.extend(extras.glob("*.txt"))
    return paths


def _resolve_key(geo: str, stem: str) -> str:
    g = (geo or "zm").strip().lower()
    if g == "cm":
        return resolve_cm_key(stem)
    if g == "eg":
        return resolve_eg_key(stem)
    return stem


def extract_locked_values(geo: str, *, script_keys: list[str] | None = None) -> list[str]:
    """Unique links, promo codes, and amounts from script files."""
    g = (geo or "zm").strip().lower()
    seen: set[str] = set()
    out: list[str] = []

    def _add(val: str) -> None:
        v = (val or "").strip()
        if not v or len(v) < 4 or v in seen:
            return
        seen.add(v)
        out.append(v)

    paths: list[Path] = []
    if script_keys:
        for raw in script_keys:
            stem = str(raw or "").split("/")[-1].strip()
            if not stem:
                continue
            stem = _resolve_key(g, stem)
            p = SCRIPTS_DIR / g / f"{stem}.txt"
            if p.is_file():
                paths.append(p)
            ep = SCRIPTS_DIR / g / "extras" / f"{stem}.txt"
            if ep.is_file():
                paths.append(ep)
    else:
        paths = _script_paths(g)

    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for rx in (_URL_RE, _PROMO_RE, _AMOUNT_RE):
            for m in rx.finditer(text):
                _add(m.group(0))

    return sorted(out, key=lambda s: (-len(s), s))


def placeholder_map(locked: list[str]) -> dict[str, str]:
    return {f"{{{{LOCK_{i}}}}}": val for i, val in enumerate(locked)}


def expand_placeholders(text: str, ph_map: dict[str, str]) -> str:
    out = (text or "").strip()
    for ph, val in ph_map.items():
        out = out.replace(ph, val)
    return out.strip()


def scrub_invented_tokens(text: str, catalog: list[str]) -> str:
    """Replace promo/url tokens in output that are not in the allowed catalog."""
    if not text or not catalog:
        return (text or "").strip()
    catalog_lower = {c.lower() for c in catalog}
    promos_in_cat = [c for c in catalog if _PROMO_RE.fullmatch(c)]
    urls_in_cat = [c for c in catalog if _URL_RE.search(c)]

    out = text
    for m in _URL_RE.finditer(text):
        tok = m.group(0)
        if tok.lower() not in catalog_lower and urls_in_cat:
            out = out.replace(tok, urls_in_cat[0], 1)
    for m in _PROMO_RE.finditer(text):
        tok = m.group(0)
        if tok.lower() not in catalog_lower and promos_in_cat:
            if tok not in catalog:
                out = out.replace(tok, promos_in_cat[0], 1)
    return out.strip()


def finalize_compose_messages(
    messages: list[str],
    *,
    geo: str,
    script_keys: list[str],
    required_placeholders: dict[str, str],
) -> list[str]:
    """Expand placeholders and enforce catalog for links/promos/amounts."""
    catalog = extract_locked_values(geo, script_keys=script_keys or None)
    if not required_placeholders and catalog:
        required_placeholders = placeholder_map(catalog)

    out: list[str] = []
    for raw in messages:
        text = expand_placeholders(raw, required_placeholders)
        if _PLACEHOLDER_RE.search(text):
            continue
        text = scrub_invented_tokens(text, catalog)
        if text:
            out.append(text)
    return out


def locked_values_block(geo: str, script_keys: list[str] | None = None) -> str:
    locked = extract_locked_values(geo, script_keys=script_keys)
    ph = placeholder_map(locked)
    if not ph:
        return ""
    lines = [
        "LOCKED VALUES — use ONLY these placeholders (never type the value yourself):"
    ]
    for placeholder, value in ph.items():
        lines.append(f"- {placeholder} = {value!r}")
    return "\n".join(lines)


def reference_scripts_block(geo: str, keys: list[str]) -> str:
    g = (geo or "zm").strip().lower()
    lines = ["Reference script facts (rephrase naturally; use LOCK placeholders for codes/links/amounts):"]
    for raw in keys:
        stem = str(raw or "").split("/")[-1].strip()
        if not stem:
            continue
        try:
            body = load_script(g, stem)
        except FileNotFoundError:
            continue
        lines.append(f"[{stem}]:\n{body[:1200]}")
    return "\n".join(lines)
