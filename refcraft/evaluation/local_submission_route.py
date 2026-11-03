"""Exact local HTML origin/path routing, including normal query-state reloads.

Not yet installed into active/frozen producers. This helper must be adopted in
a new runtime contract. No arbitrary URL, method, host, port, or external asset
is served. POST remains unsupported: a local HTML fixture is not a backend.
"""
from urllib.parse import urlsplit


def is_submission_navigation(url, *, method='GET', is_navigation=True):
    if not is_navigation or method != 'GET' or not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
        return (parts.scheme == 'https' and parts.hostname == 'vrefbench.invalid'
                and parts.username is None and parts.password is None
                and parts.port is None and parts.path == '/submission')
    except ValueError:
        return False
