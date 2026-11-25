"""Tests never read the public dataset over the network; tests that need a mirror set one."""
import os

os.environ["NP_DATA_URL"] = ""

# On macOS, XGBoost crashes if PyTorch (with its own bundled libomp) loaded first, and some
# ranker tests import torch directly. Load XGBoost first, as a walk-forward run does.
import xgboost  # noqa: E402,F401
