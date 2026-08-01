import sys
import importlib
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
HOST_LIB_DIR = PLUGIN_DIR.parent / 'pakr' / 'feedBack' / 'lib'

# feedpakr's siblings are loaded via context['load_sibling'] at runtime
# (namespaced module names) — for tests we just need them importable.
sys.path.insert(0, str(PLUGIN_DIR))

# feedpakr_pipeline needs the host's core lib (guitarpro, gp2rs, song,
# gp2midi) on sys.path. Point at the sibling checkout used during
# development; skip silently (tests that need it will self-skip) if it
# isn't there — e.g. in an environment that only has this one repo.
if HOST_LIB_DIR.is_dir():
    sys.path.insert(0, str(HOST_LIB_DIR))


import feedpakr_pipeline

feedpakr_pipeline.configure_sibling_loader(importlib.import_module)
