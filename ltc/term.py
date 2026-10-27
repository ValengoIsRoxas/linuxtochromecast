"""Salida con color a la terminal, compartida por la CLI y el instalador."""

from __future__ import annotations

import sys

_TTY = sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def bold(t): return _c(t, "1")
def dim(t): return _c(t, "2")
def cyan(t): return _c(t, "36")
def green(t): return _c(t, "32")
def yellow(t): return _c(t, "33")
def red(t): return _c(t, "31")
