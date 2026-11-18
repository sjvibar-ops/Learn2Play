"""Prepare Flask static assets for Vercel's public asset serving."""

from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parent.parent
shutil.copytree(ROOT / "Frontend" / "static", ROOT / "public" / "static", dirs_exist_ok=True)
