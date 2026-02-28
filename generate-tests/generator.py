"""
Single entrypoint for compliance prompt generation.

Example:
  python generate-tests/generator.py --strategy zero_shot --framework eu_ai_act
  python generate-tests/generator.py --strategy few_shot --framework eu_ai_act
  python generate-tests/generator.py --strategy chain_of_thought --framework eu_ai_act
  python generate-tests/generator.py --strategy prompt_chaining --framework eu_ai_act
  python generate-tests/generator.py --strategy tree_of_thoughts --framework eu_ai_act
  python generate-tests/generator.py --strategy self_consistency --framework eu_ai_act
  python generate-tests/generator.py --strategy self_reflection --framework eu_ai_act
  python generate-tests/generator.py --strategy directional_stimulus --framework eu_ai_act
  python generate-tests/generator.py --strategy iterative --framework eu_ai_act
  python generate-tests/generator.py --strategy multi_shot --framework owasp_llm

Output is written under generate-tests/<strategy.output_subdir>/<filename>.
"""
import sys
import argparse
from pathlib import Path

# Ensure generate-tests is on path when run as python generate-tests/generator.py
_gen_dir = Path(__file__).resolve().parent
if str(_gen_dir) not in sys.path:
    sys.path.insert(0, str(_gen_dir))

from strategies import get_strategy
import core


def main() -> None:
    project_root = _gen_dir.parent
    rubrics_dir = project_root / "rubrics"

    parser = argparse.ArgumentParser(
        description="Generate compliance test prompts. Output goes to generate-tests/<strategy>/<filename>."
    )
    def _norm_hyphens(s: str) -> str:
        return s.strip().replace("-", "_")

    parser.add_argument(
        "--strategy",
        type=_norm_hyphens,
        choices=["zero_shot", "multi_shot", "few_shot", "iterative", "chain_of_thought", "prompt_chaining", "tree_of_thoughts", "self_consistency", "self_reflection", "directional_stimulus"],
        default="zero_shot",
        help="Prompt generation strategy (default: zero_shot). Hyphens auto-corrected to underscores.",
    )
    parser.add_argument(
        "--framework",
        type=_norm_hyphens,
        default="eu_ai_act",
        help="Framework name (rubric stem, e.g. eu_ai_act, owasp_llm, fria_core). Hyphens auto-corrected to underscores.",
    )
    parser.add_argument(
        "--rubric",
        metavar="PATH",
        help="Override: path to rubric JSON (if set, --framework is ignored)",
    )
    parser.add_argument(
        "--output",
        metavar="FILENAME",
        help="Override output filename (default: <framework>.json, e.g. eu-ai-act.json)",
    )
    args = parser.parse_args()

    strategy = get_strategy(args.strategy)

    if args.rubric:
        rubric_path = args.rubric
        if not Path(rubric_path).is_absolute():
            for base in (project_root, Path.cwd()):
                candidate = base / rubric_path
                if candidate.exists():
                    rubric_path = str(candidate)
                    break
        output_path = args.output or (Path(rubric_path).stem.replace("_", "-") + ".json")
    else:
        framework = args.framework  # already normalized by type
        rubric_path = str(rubrics_dir / f"{framework}.json")
        if not Path(rubric_path).exists():
            parser.error(f"Rubric not found: {rubric_path} (use --framework <name> for e.g. eu_ai_act, owasp_llm, fria_core)")
        output_path = args.output or f"{framework.replace('_', '-')}.json"

    # Core writes to generate-tests/<strategy.output_subdir>/<filename>
    print(f"Strategy: {args.strategy}")
    print(f"Rubric: {rubric_path}")
    print(f"Output: {_gen_dir / strategy.output_subdir / Path(output_path).name}")
    core.generate_compliance_suite(rubric_path, output_path, strategy)
    print("Done.")


if __name__ == "__main__":
    main()
