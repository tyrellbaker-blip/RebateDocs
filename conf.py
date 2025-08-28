# Configuration file for the Sphinx documentation builder.
# Full config reference:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

# -- Path setup --------------------------------------------------------------
# Make the project root importable so autodoc can import `app`, `extraction`, etc.
import os
import sys

# If this conf.py sits in the project root (alongside app/, extraction/, index.rst),
# keep ".". If you later move docs into a "docs/" folder, change this to "..".
sys.path.insert(0, os.path.abspath("."))

# -- Project information -----------------------------------------------------
project = "RebateDocs"
author = "Ty Baker"
copyright = "2025, Ty Baker"
release = "2025-08-25"

# -- General configuration ---------------------------------------------------
extensions = [
    "sphinx.ext.autodoc",   # pull in docstrings
    "sphinx.ext.napoleon",  # allow Google/NumPy-style docstrings
    "sphinx.ext.viewcode",  # add [source] links
    "sphinx.ext.autosummary",
]
autosummary_generate = True
# If some heavy/optional deps arenâ€™t installed in the docs venv, mock them here
# so autodoc doesnâ€™t fail on import.
autodoc_mock_imports = ["fitz"]

# Sensible autodoc defaults
autodoc_default_options = {
    "members": True,
    "undoc-members": False,
    "show-inheritance": False,
}
autodoc_typehints = "description"
autodoc_class_signature = "separated"

# Napoleon (Google/NumPy docstring) tweaks
napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_include_init_with_doc = False
napoleon_use_param = True
napoleon_use_rtype = True

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# -- Options for HTML output -------------------------------------------------
html_theme = "alabaster"           # keep default; no extra install required
html_static_path = ["_static"]
html_title = f"{project} {release}"

# Optional: nicer syntax highlighting
pygments_style = "sphinx"
