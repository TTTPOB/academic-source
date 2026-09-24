"""Small valid documents and a source boundary substitute for offline tests."""

from pathlib import Path

import pymupdf


def paper_bytes() -> bytes:
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), "An offline academic-source test document.")
        return document.tobytes()


class RecordingSource:
    def __init__(self) -> None:
        self.content = paper_bytes()
        self.calls: list[str] = []

    def supports(self, identifier, request):
        return identifier.startswith("10.")

    def acquire(self, identifier, request, work_dir: Path, config):
        self.calls.append(identifier)
        if identifier.endswith("missing"):
            return {"success": False, "reason": "not_found"}
        path = work_dir / "paper.pdf"
        path.write_bytes(self.content)
        return {
            "path": path,
            "source": "offline-fixture",
            "metadata": {"title": "Offline paper", "file": str(path)},
        }
