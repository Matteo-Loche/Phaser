"""Parse mineral phases and element composition from a PHREEQC database file."""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_ELEMENT_IN_SPECIES = re.compile(r"([A-Z][a-z]?)[+-]")
_FORMULA_ELEMENTS = re.compile(r"([A-Z][a-z]?)(?=\d|[A-Z]|\(|\)|\+|-|\s|$)")


@dataclass(frozen=True)
class PhaseRecord:
    name: str
    elements: frozenset[str]
    reaction: str


def _extract_elements(text: str) -> set[str]:
    elems = set(_ELEMENT_IN_SPECIES.findall(text))
    elems.update(_FORMULA_ELEMENTS.findall(text))
    return {e for e in elems if e not in {"O", "H", "E", "e"}}


def parse_phases(db_path: Path | str) -> list[PhaseRecord]:
    path = Path(db_path)
    text = path.read_text(encoding="utf-8", errors="replace")
    start = text.find("PHASES")
    if start < 0:
        return []
    chunk = text[start:]
    end_markers = ("\nSOLUTION_SPECIES", "\nEXCHANGE_SPECIES", "\nSURFACE_SPECIES")
    end = min((chunk.find(m) for m in end_markers if chunk.find(m) > 0), default=len(chunk))

    lines = chunk[:end].splitlines()[1:]
    phases: list[PhaseRecord] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line or line.startswith("#"):
            i += 1
            continue
        if line.startswith("log_k") or line.startswith("-analytic") or line.startswith("delta_h"):
            i += 1
            continue

        name = line.split("=")[0].strip()
        reaction = line
        if "=" not in line and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            if "=" in nxt:
                reaction = nxt
                i += 1

        elems = _extract_elements(name + " " + reaction.split("=", 1)[0])
        elems.update(_extract_elements(reaction))
        if elems or is_gas(name):
            phases.append(
                PhaseRecord(
                    name=name,
                    elements=frozenset(elems),
                    reaction=reaction,
                )
            )
        i += 1

    return phases


@lru_cache(maxsize=4)
def load_phase_catalog(db_path: str) -> tuple[PhaseRecord, ...]:
    return tuple(parse_phases(db_path))


def list_elements(db_path: Path | str) -> list[str]:
    catalog = load_phase_catalog(str(db_path))
    elems: set[str] = set()
    for rec in catalog:
        elems.update(rec.elements)
    return sorted(elems)


def is_gas(name: str) -> bool:
    return name.strip().lower().endswith("(g)")


COMMON_GASES = ("O2(g)", "H2(g)", "CO2(g)", "CH4(g)")


def list_common_gases(db_path: Path | str, system_elements: set[str] | None = None) -> list[str]:
    """Return common gas phases for Pourbaix diagrams."""
    catalog = load_phase_catalog(str(db_path))
    by_name = {p.name: p for p in catalog}
    out: list[str] = []
    for name in COMMON_GASES:
        if name not in by_name:
            continue
        if name in {"O2(g)", "H2(g)"}:
            out.append(name)
            continue
        if system_elements is not None:
            rec = by_name[name]
            if not rec.elements.issubset(system_elements):
                continue
        out.append(name)
    return out


def filter_phases(
    db_path: Path | str,
    *,
    system_elements: set[str],
    selected: set[str] | None = None,
    exclude_element_solids: bool = True,
    exclude_gases: bool = False,
) -> list[PhaseRecord]:
    catalog = load_phase_catalog(str(db_path))
    out: list[PhaseRecord] = []
    for rec in catalog:
        if exclude_element_solids and "(element)" in rec.name.lower():
            continue
        if exclude_gases and is_gas(rec.name):
            continue
        if not rec.elements:
            continue
        if not rec.elements.issubset(system_elements):
            continue
        if selected is not None and rec.name not in selected:
            continue
        out.append(rec)
    return sorted(out, key=lambda r: r.name)
