"""Validation and storage paths for uploaded documents.

Filenames are replaced with a UUID rather than kept as supplied. A CV called
`Chidi-Okonkwo-CV.pdf` sitting on disk under its own name leaks who it belongs
to, and a predictable path invites guessing — even though these are served
through an authenticated view, defence in depth is cheap here.
"""
import uuid
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError

CV_EXTENSIONS = {".pdf", ".doc", ".docx", ".odt", ".rtf"}
ID_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".heic"}


def _validate(upload, allowed, label):
    suffix = Path(upload.name).suffix.lower()
    if suffix not in allowed:
        readable = ", ".join(sorted(e.lstrip(".") for e in allowed))
        raise ValidationError(
            f"{label} must be one of: {readable}. You uploaded “{suffix or 'no extension'}”."
        )
    if upload.size > settings.MAX_UPLOAD_BYTES:
        limit = settings.MAX_UPLOAD_BYTES // (1024 * 1024)
        raise ValidationError(f"{label} must be under {limit}MB.")


def validate_cv(upload):
    _validate(upload, CV_EXTENSIONS, "A CV")


def validate_id_document(upload):
    _validate(upload, ID_EXTENSIONS, "An ID document")


def _path(folder, filename):
    return f"{folder}/{uuid.uuid4().hex}{Path(filename).suffix.lower()}"


def cv_path(instance, filename):
    return _path("cv", filename)


def id_document_path(instance, filename):
    return _path("identity", filename)
