"""A running source fingerprint distinguishes content versions from method versions."""
import hashlib
from pathlib import Path


def source_revision():
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in (".py", ".js", ".html", ".css"):
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


RUNTIME_REVISION = source_revision()
