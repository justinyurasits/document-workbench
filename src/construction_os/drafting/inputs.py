"""Turns a stored project input into the text the model reads and code checks citations against."""
import re
from io import BytesIO

from pypdf import PdfReader


class InputError(Exception):
    pass


def input_text(project, record):
    data = project.read_input_bytes(record)
    if record.file.lower().endswith(".pdf"):
        text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(data)).pages)
        if not text.strip():
            raise InputError(f"{record.original_name} has no text layer (a scanned PDF?); supply the quote as text")
        return text
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InputError(f"{record.original_name} isn't UTF-8 text or a PDF") from error


def normalized(text):
    """Whitespace runs collapse to one space, so a citation may wrap differently from its source."""
    return re.sub(r"\s+", " ", text).strip()
