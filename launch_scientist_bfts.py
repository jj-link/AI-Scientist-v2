import os.path as osp
import json
import argparse
import shutil
import torch
import os
import re
import sys
import traceback
from pathlib import Path

import psutil
from datetime import datetime
from ai_scientist.llm import create_client
from ai_scientist import model_routing
import yaml

from contextlib import contextmanager
from ai_scientist.treesearch.perform_experiments_bfts_with_agentmanager import (
    perform_experiments_bfts,
)
from ai_scientist.treesearch.bfts_utils import (
    idea_to_markdown,
    edit_bfts_config_file,
)
from ai_scientist.perform_plotting import aggregate_plots
from ai_scientist.perform_writeup import perform_writeup
from ai_scientist.perform_icbinb_writeup import (
    perform_writeup as perform_icbinb_writeup,
    gather_citations,
)
from ai_scientist.perform_llm_review import perform_review, load_paper
from ai_scientist.perform_vlm_review import perform_imgs_cap_ref_review
from ai_scientist.utils.token_tracker import token_tracker
from ai_scientist.progress import Cancelled, PipelineFailure, check_stop, emit


def print_time():
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


def save_token_tracker(idea_dir):
    with open(osp.join(idea_dir, "token_tracker.json"), "w") as f:
        json.dump(token_tracker.get_summary(), f)
    with open(osp.join(idea_dir, "token_tracker_interactions.json"), "w") as f:
        json.dump(token_tracker.get_interactions(), f)


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description="Run AI scientist experiments")
    parser.add_argument(
        "--writeup-type",
        type=str,
        default="icbinb",
        choices=["normal", "icbinb"],
        help="Type of writeup to generate (normal=8 page, icbinb=4 page)",
    )
    parser.add_argument(
        "--load_ideas",
        type=str,
        default="ideas/i_cant_believe_its_not_better.json",
        help="Path to a JSON file containing pregenerated ideas",
    )
    parser.add_argument(
        "--load_code",
        action="store_true",
        help="If set, load a Python file with same name as ideas file but .py extension",
    )
    parser.add_argument(
        "--idea_idx",
        type=int,
        default=0,
        help="Index of the idea to run",
    )
    parser.add_argument(
        "--add_dataset_ref",
        action="store_true",
        help="If set, add a HF dataset reference to the idea",
    )
    parser.add_argument(
        "--writeup-retries",
        type=int,
        default=3,
        help="Number of writeup attempts to try",
    )
    parser.add_argument(
        "--attempt_id",
        type=int,
        default=0,
        help="Attempt ID, used to distinguish same idea in different attempts in parallel runs",
    )
    parser.add_argument(
        "--model_agg_plots",
        type=str,
        default="role/plot_generation",
        help="Model to use for plot aggregation",
    )
    parser.add_argument(
        "--model_writeup",
        type=str,
        default="role/writeup",
        help="Model to use for writeup",
    )
    parser.add_argument(
        "--model_citation",
        type=str,
        default="role/citation",
        help="Model to use for citation gathering",
    )
    parser.add_argument(
        "--num_cite_rounds",
        type=int,
        default=20,
        help="Number of citation rounds to perform",
    )
    parser.add_argument(
        "--model_writeup_small",
        type=str,
        default="role/writeup_small",
        help="Smaller model to use for writeup",
    )
    parser.add_argument(
        "--model_review",
        type=str,
        default="role/review",
        help="Model to use for review main text and captions",
    )
    parser.add_argument(
        "--skip_writeup",
        action="store_true",
        help="If set, skip the writeup process",
    )
    parser.add_argument(
        "--skip_review",
        action="store_true",
        help="If set, skip the review process",
    )
    parser.add_argument(
        "--role-config",
        type=str,
        default=None,
        help="Path to a frozen model-settings JSON snapshot (set automatically for Studio jobs; direct runs use the current settings database).",
    )
    parser.add_argument(
        "--bfts-config",
        type=str,
        default="bfts_config.yaml",
        help="Path to the BFTS search configuration YAML.",
    )
    return parser.parse_args(argv)


def prepare_bfts_config(args, idea_dir, idea_path_json):
    """Select the requested BFTS config and hand it to the editor unchanged."""
    config_path = args.bfts_config
    return edit_bfts_config_file(
        config_path,
        idea_dir,
        idea_path_json,
    )


def get_available_gpus(gpu_ids=None):
    if gpu_ids is not None:
        return [int(gpu_id) for gpu_id in gpu_ids.split(",")]
    return list(range(torch.cuda.device_count()))


def find_pdf_path_for_review(idea_dir):
    """Select an available document deterministically, or return None."""
    def priority(path):
        name = path.name.lower()
        if name.endswith("_final.pdf"):
            rank, number = 0, 0
        elif name.endswith("_reflection_final_page_limit.pdf"):
            rank, number = 1, 0
        elif match := re.search(r"reflection[_.]?(\d+)", name):
            rank, number = 2, -int(match.group(1))
        elif match := re.search(r"_(\d+)\.pdf$", name):
            rank, number = 3, -int(match.group(1))
        else:
            rank, number = 4, 0
        return rank, number, name, path.name

    pdfs = [
        path for path in Path(idea_dir).iterdir()
        if path.is_file() and path.suffix.lower() == ".pdf"
    ]
    return str(min(pdfs, key=priority).resolve()) if pdfs else None


def archive_writeup_attempt(idea_dir, attempt):
    """Keep prior outputs before writeup removes or overwrites them."""
    root = Path(idea_dir)
    outputs = [
        path for path in root.iterdir()
        if (path.is_file() and path.suffix.lower() in {".pdf", ".tex", ".bib"})
        or (path.name == "latex" and path.is_dir())
    ]
    if not outputs:
        return
    archive = root / "writeup_attempts" / f"attempt_{attempt}"
    archive.mkdir(parents=True, exist_ok=False)
    # Copy every output before removing anything, so an archive error is lossless.
    for path in outputs:
        destination = archive / path.name
        if path.is_dir():
            shutil.copytree(path, destination)
        else:
            shutil.copy2(path, destination)
    for path in outputs:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


@contextmanager
def pipeline_phase(name, on_event, should_stop):
    check_stop(should_stop)
    emit(on_event, "phase_started", name)
    try:
        yield
        check_stop(should_stop)
    except Cancelled:
        raise
    except Exception:
        emit(
            on_event, "phase_failed", name,
            code=f"{name}_failed",
            message=f"The {name} phase failed. See technical details.",
        )
        raise
    else:
        emit(on_event, "phase_finished", name)


def cleanup_pipeline_children(process, preexisting):
    """Terminate only newly owned descendants, checking identity before signals."""
    def identity(child):
        return child.pid, child.create_time()

    def still_owned(child, expected):
        try:
            return child.is_running() and identity(psutil.Process(child.pid)) == expected
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    children = []
    try:
        descendants = process.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return
    for child in descendants:
        try:
            expected = identity(child)
            if expected in preexisting or any(
                identity(parent) in preexisting for parent in child.parents()
            ):
                continue
            if still_owned(child, expected):
                child.terminate()
                children.append((child, expected))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    _, alive = psutil.wait_procs([child for child, _ in children], timeout=3)
    alive_pids = {child.pid for child in alive}
    for child, expected in children:
        if child.pid in alive_pids and still_owned(child, expected):
            try:
                child.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    psutil.wait_procs(
        [child for child, expected in children if still_owned(child, expected)],
        timeout=3,
    )


@contextmanager
def redirect_stdout_stderr_to_file(log_file_path):
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    log = open(log_file_path, "a")
    sys.stdout = log
    sys.stderr = log
    try:
        yield
    finally:
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        log.close()


def run_pipeline(args, *, run_dir=None, on_event=None, should_stop=None):
    """Run the CLI research workflow in the caller's isolated worker process."""
    process = psutil.Process()
    preexisting = set()
    for child in process.children(recursive=True):
        try:
            preexisting.add((child.pid, child.create_time()))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    idea_dir = None
    try:
        with pipeline_phase("preparing", on_event, should_stop):
            os.environ["AI_SCIENTIST_ROOT"] = os.path.dirname(os.path.abspath(__file__))
            print(f"Set AI_SCIENTIST_ROOT to {os.environ['AI_SCIENTIST_ROOT']}")
            routing_in_use = any(
                str(getattr(args, name, "") or "").startswith(("role/", "selfhosted/"))
                for name in (
                    "model_agg_plots", "model_writeup", "model_citation",
                    "model_writeup_small", "model_review",
                )
            )
            if not routing_in_use:
                try:
                    with open(args.bfts_config, "r", encoding="utf-8") as f:
                        config_text = f.read()
                    routing_in_use = "role/" in config_text or "selfhosted/" in config_text
                except OSError:
                    pass
            if routing_in_use:
                if args.role_config:
                    os.environ[model_routing.ROLE_CONFIG_ENV] = args.role_config
                mapping = model_routing.validate_roles()
                print("role mapping:")
                for role_name, info in mapping.items():
                    caps = (
                        f" (requires {', '.join(info['capabilities'])})"
                        if info["capabilities"] else ""
                    )
                    print(f"  {role_name:<22} -> {info['endpoint']:<8} {info['model']}{caps}")

            # Constrain generated experiment workers, not HTTP model inference.
            settings = model_routing.load_settings()
            execution = settings.get("experiment_execution") or {}
            exp_gpu = execution.get("cuda_device")
            if exp_gpu is not None:
                os.environ["CUDA_VISIBLE_DEVICES"] = str(exp_gpu)
                print(
                    f"Experiments pinned via CUDA_VISIBLE_DEVICES={exp_gpu} "
                    f"(from model settings)"
                )
            print(f"Using GPUs: {get_available_gpus()}")
            with open(args.load_ideas, "r") as f:
                ideas = json.load(f)
                print(f"Loaded {len(ideas)} pregenerated ideas from {args.load_ideas}")
            idea = ideas[args.idea_idx]

            if run_dir is None:
                date = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                idea_dir = f"experiments/{date}_{idea['Name']}_attempt_{args.attempt_id}"
                os.makedirs(idea_dir, exist_ok=True)
            else:
                supplied_dir = Path(run_dir)
                if supplied_dir.is_symlink():
                    raise PipelineFailure("The provided run directory must not be a link.")
                if not supplied_dir.is_dir() or any(supplied_dir.iterdir()):
                    raise PipelineFailure("The provided run directory must exist and be empty.")
                idea_dir = str(supplied_dir.resolve())
            print(f"Results will be saved in {idea_dir}")
            idea_path_md = osp.join(idea_dir, "idea.md")
            code = None
            code_path = None
            if args.load_code:
                code_path = args.load_ideas.rsplit(".", 1)[0] + ".py"
                if os.path.exists(code_path):
                    with open(code_path, "r") as f:
                        code = f.read()
                else:
                    print(f"Warning: Code file {code_path} not found")
            idea_to_markdown(idea, idea_path_md, code_path)
            dataset_ref_code = None
            if args.add_dataset_ref:
                dataset_ref_path = "hf_dataset_reference.py"
                if os.path.exists(dataset_ref_path):
                    with open(dataset_ref_path, "r") as f:
                        dataset_ref_code = f.read()
                else:
                    print(f"Warning: Dataset reference file {dataset_ref_path} not found")
            if dataset_ref_code is not None and code is not None:
                added_code = dataset_ref_code + "\n" + code
            elif dataset_ref_code is not None:
                added_code = dataset_ref_code
            else:
                added_code = code
            print(added_code)
            if added_code is not None:
                idea["Code"] = added_code
            idea_path_json = osp.join(idea_dir, "idea.json")
            with open(idea_path_json, "w") as f:
                json.dump(idea, f, indent=4)
            idea_config_path = prepare_bfts_config(args, idea_dir, idea_path_json)

        with pipeline_phase("experiments", on_event, should_stop):
            log_dir = Path(perform_experiments_bfts(
                idea_config_path, on_event=on_event, should_stop=should_stop,
            )).resolve()

        with pipeline_phase("figures", on_event, should_stop):
            experiment_results_dir = log_dir / "experiment_results"
            copied_results = Path(idea_dir) / "experiment_results"
            if experiment_results_dir.exists():
                shutil.copytree(experiment_results_dir, copied_results, dirs_exist_ok=True)
            plots = aggregate_plots(
                base_folder=idea_dir, model=args.model_agg_plots, should_stop=should_stop,
            )
            if not plots["success"]:
                raise PipelineFailure("Plot aggregation did not complete successfully.")
            figures = plots["figures"]
            if copied_results.exists():
                shutil.rmtree(copied_results)
        save_token_tracker(idea_dir)

        pdf_path = None
        if not args.skip_writeup:
            with pipeline_phase("citations", on_event, should_stop):
                citations_text = gather_citations(
                    idea_dir, num_cite_rounds=args.num_cite_rounds,
                    small_model=args.model_citation,
                )
            with pipeline_phase("paper", on_event, should_stop):
                writeup_success = False
                for attempt in range(args.writeup_retries):
                    check_stop(should_stop)
                    if attempt > 0:
                        archive_writeup_attempt(idea_dir, attempt)
                    print(f"Writeup attempt {attempt + 1} of {args.writeup_retries}")
                    emit(
                        on_event, "writeup_attempt", "paper",
                        writeup_attempt=attempt + 1,
                    )
                    if args.writeup_type == "normal":
                        writeup_success = perform_writeup(
                            base_folder=idea_dir,
                            small_model=args.model_writeup_small,
                            big_model=args.model_writeup,
                            page_limit=8,
                        )
                    else:
                        writeup_success = perform_icbinb_writeup(
                            base_folder=idea_dir,
                            small_model=args.model_writeup_small,
                            big_model=args.model_writeup,
                            page_limit=4,
                            citations_text=citations_text,
                        )
                    check_stop(should_stop)
                    pdf_path = find_pdf_path_for_review(idea_dir)
                    if writeup_success and pdf_path is not None:
                        break
                    writeup_success = False
                if not writeup_success:
                    raise PipelineFailure("Writeup failed to produce a paper after all attempts.")
        save_token_tracker(idea_dir)

        if not args.skip_review and not args.skip_writeup:
            with pipeline_phase("reviews", on_event, should_stop):
                print("Paper found at: ", pdf_path)
                paper_content = load_paper(pdf_path)
                check_stop(should_stop)
                client, client_model = create_client(args.model_review)
                review_text = perform_review(paper_content, client_model, client)
                with open(osp.join(idea_dir, "review_text.txt"), "w") as f:
                    json.dump(review_text, f, indent=4)
                if not isinstance(review_text, dict) or not review_text:
                    raise PipelineFailure("The paper review did not return a review object.")
                check_stop(should_stop)
                review_img_cap_ref = perform_imgs_cap_ref_review(client, client_model, pdf_path)
                with open(osp.join(idea_dir, "review_img_cap_ref.json"), "w") as f:
                    json.dump(review_img_cap_ref, f, indent=4)
                if not isinstance(review_img_cap_ref, dict) or any(
                    not isinstance(review, dict) or not review
                    for review in review_img_cap_ref.values()
                ):
                    raise PipelineFailure("The figure review did not return review objects.")
                print("Paper review completed.")
        check_stop(should_stop)
        return {"pdf_path": pdf_path, "log_dir": str(log_dir), "figures": figures}
    except Exception:
        traceback.print_exc()
        raise
    finally:
        try:
            if idea_dir is not None:
                save_token_tracker(idea_dir)
        except Exception:
            traceback.print_exc()
        finally:
            cleanup_pipeline_children(process, preexisting)


def main(argv=None):
    run_pipeline(parse_arguments(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
