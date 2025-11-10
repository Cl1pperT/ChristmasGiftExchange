#!/usr/bin/env python3
"""
Secret Santa matcher with constraints:
  1) No one gifts to themselves.
  2) No one gifts to their spouse.
  3) No one gifts to someone they gifted in the past N years (default 3).

INPUTS (same directory as this script by default):
  - people.txt            : participants (Name[, Spouse])
  - YYYY.txt              : previous assignment files, e.g., 2023.txt, 2024.txt (optional)

OUTPUT:
  - <current_year>.txt    : new assignments (e.g., 2025.txt)

Run:
  python secret_santa.py
Options:
  python secret_santa.py --people people.txt --years 2 --seed 42 --max-tries 500 --year 2025

Notes:
  - The solver uses MRV (minimum remaining values) backtracking with randomization.
  - If a solution truly doesn't exist given the constraints, it will report and exit.
"""

from __future__ import annotations
import argparse
import datetime as _dt
import os
import random
import re
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple, Optional

# ---------------------------
# Parsing helpers
# ---------------------------

_LINE_SEP_RE = re.compile(r"\s*(?:->|:|,|-)\s*")

def _read_people(path: Path) -> Tuple[List[str], Dict[str, Optional[str]]]:
    if not path.exists():
        sys.exit(f"[ERROR] People file not found: {path}")

    raw_pairs: List[Tuple[str, Optional[str]]] = []
    with path.open("r", encoding="utf-8") as f:
        for ln in f:
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            # split by comma, pipe, or tab
            parts = re.split(r"[,\|\t]", s)
            parts = [p.strip() for p in parts if p.strip()]
            if len(parts) == 1:
                raw_pairs.append((parts[0], None))
            elif len(parts) >= 2:
                raw_pairs.append((parts[0], parts[1]))
            else:
                continue

    # Normalize names (unique, preserve insertion order)
    names: List[str] = []
    seen = set()
    for name, spouse in raw_pairs:
        if name not in seen:
            names.append(name)
            seen.add(name)

    # Build spouse map (symmetrize when possible)
    spouse: Dict[str, Optional[str]] = {n: None for n in names}
    name_set = set(names)
    for name, s in raw_pairs:
        if s and s in name_set:
            spouse[name] = s
            # If partner exists, symmetrize silently (don’t override explicit)
            if spouse.get(s) is None:
                spouse[s] = name

    return names, spouse


def _read_history_files(directory: Path, current_year: int, years_back: int) -> Dict[str, Set[str]]:
    """
    Reads last `years_back` files named 'YYYY.txt' (YYYY < current_year),
    parses "Giver -> Receiver" lines, and returns mapping:
      disallow[giver] = {receivers they gifted in last N years}
    """
    year_files = []
    for p in directory.glob("*.txt"):
        m = re.fullmatch(r"(\d{4})\.txt", p.name)
        if not m:
            continue
        y = int(m.group(1))
        if y < current_year:
            year_files.append((y, p))
    # sort most recent first and take last N years
    year_files.sort(key=lambda yp: yp[0], reverse=True)
    year_files = year_files[:years_back]

    disallow: Dict[str, Set[str]] = {}
    for y, p in year_files:
        with p.open("r", encoding="utf-8") as f:
            for ln in f:
                s = ln.strip()
                if not s or s.startswith("#"):
                    continue
                # Accept any of: "A -> B", "A,B", "A : B", "A - B"
                parts = _LINE_SEP_RE.split(s)
                if len(parts) != 2:
                    continue
                giver, receiver = parts[0].strip(), parts[1].strip()
                if giver and receiver:
                    disallow.setdefault(giver, set()).add(receiver)
    return disallow


# ---------------------------
# Solver
# ---------------------------

def _build_allowed(
    names: List[str],
    spouse: Dict[str, Optional[str]],
    disallow_recent: Dict[str, Set[str]],
) -> Dict[str, Set[str]]:
    name_set = set(names)
    allowed: Dict[str, Set[str]] = {}
    for g in names:
        dis = set()
        dis.add(g)  # no self
        if spouse.get(g):
            dis.add(spouse[g])  # no spouse
        dis |= disallow_recent.get(g, set())
        allowed[g] = name_set - dis
    return allowed


def _solve_perfect_matching(
    names: List[str],
    allowed: Dict[str, Set[str]],
    rng: random.Random,
    max_tries: int = 500,
) -> Optional[Dict[str, str]]:
    """
    Backtracking with MRV (choose giver with fewest available receivers first).
    Randomize receiver order to avoid bias.
    """
    givers = list(names)

    # Quick unsat check
    for g in givers:
        if not allowed[g]:
            return None

    def backtrack(
        remaining_givers: List[str],
        available_receivers: Set[str],
        assignments: Dict[str, str],
    ) -> Optional[Dict[str, str]]:
        if not remaining_givers:
            return assignments

        # MRV: pick giver with fewest options given current availability
        giver = min(
            remaining_givers,
            key=lambda gg: len(allowed[gg] & available_receivers),
        )
        options = list(allowed[giver] & available_receivers)
        if not options:
            return None
        rng.shuffle(options)

        # Try each receiver
        next_givers = [gg for gg in remaining_givers if gg != giver]
        for r in options:
            assignments[giver] = r
            res = backtrack(next_givers, available_receivers - {r}, assignments)
            if res is not None:
                return res
            # backtrack
            del assignments[giver]
        return None

    # Multiple randomized attempts in case of unlucky ordering
    for _ in range(max_tries):
        rng.shuffle(givers)
        avail = set(names)
        out = backtrack(givers, avail, {})
        if out is not None:
            return out
    return None


# ---------------------------
# Main
# ---------------------------

def main():
    parser = argparse.ArgumentParser(description="Secret Santa randomizer with constraints.")
    parser.add_argument("--people", type=str, default="people.txt", help="Path to people list (default: people.txt)")
    parser.add_argument("--years", type=int, default=2, help="Avoid gifting same person within the past N years (default: 3)")
    parser.add_argument("--data-dir", type=str, default=".", help="Directory containing people & year files (default: current dir)")
    parser.add_argument("--output-dir", type=str, default=".", help="Directory to write the new YYYY.txt file (default: current dir)")
    parser.add_argument("--seed", type=int, default=None, help="Random seed (optional, for reproducibility)")
    parser.add_argument("--max-tries", type=int, default=500, help="Max randomized attempts to find a solution (default: 500)")
    parser.add_argument("--year", type=int, default=None, help="Override output year (default: system current year)")
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    people_path = Path(args.people).resolve()
    rng = random.Random(args.seed)

    year = args.year or _dt.datetime.now().year
    out_path = output_dir / f"{year}.txt"

    # 1) Read participants & spouses
    names, spouse = _read_people(people_path)
    if len(names) < 2:
        sys.exit("[ERROR] Need at least 2 participants.")
    if len(names) % 1 != 0:
        pass  # (No special rule for odd/even; keeping placeholder)

    # 2) Read history (last N year files)
    disallow_recent = _read_history_files(data_dir, current_year=year, years_back=args.years)

    # 3) Build allowed edges
    allowed = _build_allowed(names, spouse, disallow_recent)

    # Quick sanity: ensure each giver has at least 1 allowed
    impossible = [g for g in names if not allowed[g]]
    if impossible:
        lines = "\n".join(f"  - {g}" for g in impossible)
        sys.exit(
            "[ERROR] No valid receivers for the following participants given constraints:\n"
            f"{lines}\nTry reducing constraints, adjusting spouses, or adding more participants."
        )

    # 4) Solve
    matching = _solve_perfect_matching(names, allowed, rng, max_tries=args.max_tries)
    if matching is None:
        sys.exit(
            "[ERROR] Could not find a valid assignment under the constraints after "
            f"{args.max_tries} attempts.\nTry a different seed, add participants, or relax rules."
        )

    # 5) Write results
    output = []
    output.append(f"# Secret Santa {year}")
    output.append("# Format: Giver -> Receiver")
    output.append("# (Generated by secret_santa.py)")
    # sort by giver name for a stable file layout
    for giver in sorted(matching.keys()):
        output.append(f"{giver} -> {matching[giver]}")

    out_path.write_text("\n".join(output) + "\n", encoding="utf-8")
    print(f"[OK] Assignments written to: {out_path}")

if __name__ == "__main__":
    main()
