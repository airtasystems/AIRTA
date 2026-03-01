#!/usr/bin/env python3
"""
Generate compliance test prompts for all (strategy, framework) pairs if the
output file does not exist. Uses generator.py under the hood.

Example:
  python generate-tests/generate_all.py
  python generate-tests/generate_all.py --force   # regenerate all

Strategies are discovered from generate-tests/strategies/ (registry).
Frameworks are discovered from rubrics/*.json (filename stem).
"""
import argparse
import subprocess
import sys
from pathlib import Path

# Run from generate-tests so generator and strategies resolve
_gen_dir = Path(__file__).resolve().parent
if str(_gen_dir) not in sys.path:
    sys.path.insert(0, str(_gen_dir))

project_root = _gen_dir.parent
rubrics_dir = project_root / "rubrics"
generator_py = _gen_dir / "generator.py"


def get_frameworks() -> list[str]:
    """Return framework names (rubric stems, underscore form) from rubrics/*.json."""
    if not rubrics_dir.exists():
        return []
    names = []
    for p in sorted(rubrics_dir.glob("*.json")):
        names.append(p.stem.replace("-", "_"))
    return names


def get_strategies() -> list[str]:
    """Return strategy names from the strategies registry."""
    from strategies import STRATEGIES
    return sorted(STRATEGIES.keys())


def output_path_for(strategy_name: str, framework: str) -> Path | None:
    """Return the output file path for (strategy, framework), or None if strategy unknown."""
    from strategies import get_strategy
    try:
        strategy = get_strategy(strategy_name)
    except ValueError:
        return None
    out_subdir = strategy.output_subdir
    filename = f"{framework.replace('_', '-')}.json"
    return _gen_dir / out_subdir / filename


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate compliance prompts for all strategy×framework pairs if output missing."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate even if output file already exists.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print what would be run.",
    )
    args = parser.parse_args()

    strategies = get_strategies()
    frameworks = get_frameworks()

    if not strategies:
        print("No strategies found.", file=sys.stderr)
        sys.exit(1)
    if not frameworks:
        print(f"No rubrics found in {rubrics_dir}.", file=sys.stderr)
        sys.exit(1)

    print(f"[*] Strategies: {len(strategies)} — {strategies}")
    print(f"[*] Frameworks: {len(frameworks)} — {frameworks}")
    print()

    to_run: list[tuple[str, str]] = []
    for strategy in strategies:
        for framework in frameworks:
            out = output_path_for(strategy, framework)
            if out is None:
                continue
            if args.force or not out.exists():
                to_run.append((strategy, framework))

    if not to_run:
        print("[*] All output files exist. Use --force to regenerate.")
        return

    print(f"[*] Will generate {len(to_run)} file(s).")
    for strategy, framework in to_run:
        cmd = [sys.executable, str(generator_py), "--strategy", strategy, "--framework", framework]
        if args.dry_run:
            print(f"  [dry-run] {' '.join(cmd)}")
            continue
        print(f"  → {strategy} / {framework}")
        result = subprocess.run(cmd, cwd=str(project_root))
        if result.returncode != 0:
            print(f"  [!] Failed: {' '.join(cmd)}", file=sys.stderr)
            sys.exit(result.returncode)
    print("[+] Done.")


if __name__ == "__main__":
    main()
