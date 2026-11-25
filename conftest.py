"""Tests never read the public dataset over the network; tests that need a mirror set one."""
import os

os.environ["NP_DATA_URL"] = ""
