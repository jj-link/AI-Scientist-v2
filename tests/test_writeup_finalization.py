"""Final publication must survive no-op reflection and preserve prior artifacts."""

from pathlib import Path
import sys
from unittest.mock import mock_open
import uuid

import fitz
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ai_scientist.perform_icbinb_writeup as writeup


LATEX = "\\documentclass{article}\n\\begin{document}Manuscript.\\end{document}\n"


@pytest.fixture
def pdf_path():
    # Individual project-local files; no temporary directories are needed.
    path = Path(__file__).with_name(f"writeup-verification-{uuid.uuid4().hex}.pdf")
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def save_pdf(path, text="Manuscript."):
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), text)
        document.save(path)


@pytest.fixture
def finalization(monkeypatch, pdf_path):
    monkeypatch.setattr(writeup, "open", mock_open(read_data=LATEX), raising=False)
    monkeypatch.setattr(
        writeup, "get_reflection_page_info", lambda *args: "Main text fits the page budget."
    )
    monkeypatch.setattr(
        writeup, "compile_latex", lambda folder, destination: save_pdf(destination)
    )
    return {
        "latex_folder": str(pdf_path.parent),
        "reflection_pdf": str(pdf_path.with_suffix(".prior.pdf")),
        "final_pdf": str(pdf_path),
        "page_limit": 4,
        "client": object(),
        "model": "test-author",
        "system_message": "Finalize the manuscript.",
    }


@pytest.mark.parametrize(
    "response",
    ["I am done", f"```latex\n{LATEX}```"],
    ids=["no-code-edits", "unchanged-latex"],
)
def test_noop_reflection_publishes_the_manuscript(
    monkeypatch, finalization, pdf_path, response
):
    monkeypatch.setattr(writeup, "get_response_from_llm", lambda **kwargs: (response, []))

    assert writeup._finalize_writeup(**finalization) is True
    assert pdf_path.is_file()


def test_failed_compilation_does_not_claim_or_destroy_a_prior_paper(
    monkeypatch, finalization, pdf_path
):
    save_pdf(pdf_path, "Previously published manuscript.")
    previous = pdf_path.read_bytes()
    monkeypatch.setattr(writeup, "get_response_from_llm", lambda **kwargs: ("I am done", []))
    monkeypatch.setattr(writeup, "compile_latex", lambda *args: None)

    assert writeup._finalize_writeup(**finalization) is False
    assert pdf_path.read_bytes() == previous


def test_blank_pages_do_not_end_reference_extraction(pdf_path):
    try:
        writeup.resolve_tex_tool("pdftotext")
    except FileNotFoundError as error:
        pytest.skip(str(error))
    with fitz.open() as document:
        document.new_page()
        document.new_page().insert_text((72, 72), "References")
        document.save(pdf_path)

    assert writeup.detect_references_position_clean(str(pdf_path)) == (2, 1)
    assert writeup.extract_page_line_counts(str(pdf_path), 1, 3) == {1: 0, 2: 1}
