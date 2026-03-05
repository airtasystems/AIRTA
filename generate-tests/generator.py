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
import json
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
    parser.add_argument(
        "--run-type",
        choices=["framework", "tools", "capabilities"],
        default="framework",
        help="framework: use --framework/--rubric and generate mandate prompts. tools/capabilities: use --component-rubric and generate 8 prompts for tools or capabilities.",
    )
    parser.add_argument(
        "--component-rubric",
        metavar="PATH",
        help="Path to component rubric JSON. When --run-type is framework: after writing the suite, append tools and capabilities mandates (8 prompts each) to the same file. When --run-type is tools or capabilities: required.",
    )
    parser.add_argument(
        "--append-to",
        metavar="PATH",
        help="When --run-type is tools or capabilities: append the new mandate to this existing suite JSON (e.g. generate-tests/zero-shot/eu-ai-act.json) instead of writing a separate file.",
    )
    args = parser.parse_args()

    strategy = get_strategy(args.strategy)

    if args.run_type in ("tools", "capabilities"):
        comp = args.component_rubric
        if not comp:
            parser.error("--component-rubric PATH is required when --run-type is tools or capabilities")
        if not Path(comp).is_absolute():
            for base in (project_root, Path.cwd()):
                candidate = base / comp
                if candidate.exists():
                    comp = str(candidate)
                    break
        comp_path = Path(comp)
        if not comp_path.exists():
            parser.error(f"Component rubric not found: {comp}")
        append_to = None
        output_path = None
        if args.append_to:
            p = Path(args.append_to)
            append_to = str(p.resolve()) if p.is_absolute() else str((project_root / args.append_to).resolve())
            if not Path(append_to).exists():
                parser.error(f"--append-to file not found: {append_to}")
        else:
            try:
                data = json.loads(comp_path.read_text(encoding="utf-8"))
                component_name = data.get("component") or comp_path.stem.replace("_rubric", "").replace("-rubric", "")
            except Exception:
                component_name = comp_path.stem.replace("_rubric", "").replace("-rubric", "")
            output_path = args.output or f"{args.run_type}/{component_name}.json"
        print(f"Strategy: {args.strategy}")
        print(f"Run type: {args.run_type}")
        print(f"Component rubric: {comp}")
        framework_rubric_path = None
        if append_to:
            print(f"Append to: {append_to}")
            # Use same multi-expert graph as main test (--framework required when appending)
            framework_rubric_path = str(rubrics_dir / f"{args.framework}.json")
            if not Path(framework_rubric_path).exists():
                parser.error(f"When using --append-to, framework rubric must exist: {framework_rubric_path}")
        else:
            print(f"Output: {_gen_dir / strategy.output_subdir / output_path}")
        core.generate_tools_or_capabilities_suite(
            comp, args.run_type, strategy,
            output_path=output_path,
            append_to_path=append_to,
            framework_rubric_path=framework_rubric_path,
        )
        print("Done.")
        return

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
    # If --component-rubric was passed, append tools and capabilities to the file we just wrote (same process)
    if args.component_rubric:
        comp = args.component_rubric
        if not Path(comp).is_absolute():
            for base in (project_root, Path.cwd()):
                candidate = base / comp
                if candidate.exists():
                    comp = str(candidate)
                    break
        comp_path = Path(comp)
        if comp_path.exists():
            suite_path = _gen_dir / strategy.output_subdir / Path(output_path).name
            if suite_path.exists():
                for run_type in ("tools", "capabilities"):
                    print(f"[*] Appending {run_type} prompts (8) to {suite_path.name} (same experts as main)...")
                    core.generate_tools_or_capabilities_suite(
                        str(comp_path.resolve()),
                        run_type,
                        strategy,
                        output_path=None,
                        append_to_path=str(suite_path.resolve()),
                        framework_rubric_path=rubric_path,
                    )
            else:
                print(f"[!] Suite file not found for append: {suite_path}")
        else:
            print(f"[!] Component rubric not found, skipping tools/capabilities: {comp}")
    print("Done.")


if __name__ == "__main__":
    main()
