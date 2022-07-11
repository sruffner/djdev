from pathlib import Path
from typing import Optional


def _read_doc_file(p: Path) -> Optional[str]:
    """
    Get the contents of the specified text file.

    Args:
        p: Path to the documentation file.
    Returns:
        The text file contents, or None if file not found or an error occurs.
    """
    try:
        with open(p, 'rt') as f:
            return f.read()
    except Exception:
        return None


def readme() -> str:
    """ The README for this package in markdown format. """
    p = Path(__file__)
    p = Path(p.parent, 'dist', 'README.md')
    return _read_doc_file(p) or "Unable to retrieve sglportalapi README"


def changelog() -> str:
    """ The CHANGELOG for this packag  in markdown format. """
    p = Path(__file__)
    p = Path(p.parent, 'dist', 'CHANGELOG.md')
    return _read_doc_file(p) or "Unable to retrieve sglportalapi CHANGELOG"
