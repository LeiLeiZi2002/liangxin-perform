from pathlib import Path

RUBRIC_DOCUMENT_PATH = (
    Path(__file__).resolve().parents[3] / "docs" / "热线心理支持职业胜任力测评量规.md"
)


def read_rubric_document() -> tuple[str, str]:
    with RUBRIC_DOCUMENT_PATH.open(encoding="utf-8", newline="") as document:
        markdown = document.read()

    first_line = markdown.splitlines()[0] if markdown else ""
    if not first_line.startswith("# ") or not first_line[2:].strip():
        raise ValueError("rubric document must start with a level-one heading")

    return first_line[2:].strip(), markdown
