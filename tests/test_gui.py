# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Kevin Griffin
"""Opt-in GUI smoke wrapper.

Skipped by default — the real-window pyautogui smoke needs a display, macOS
Accessibility permission, and moves the mouse, so it can't run in normal/CI
runs. Enable it deliberately:

    DV2MV_GUI_TEST=1 DV2MV_MEDIA=/path/to/media python3 -m pytest tests/test_gui.py
"""
import os
import subprocess
import sys

import pytest


@pytest.mark.skipif(os.environ.get("DV2MV_GUI_TEST") != "1",
                    reason="opt-in GUI smoke: set DV2MV_GUI_TEST=1 "
                           "(needs a display + Accessibility permission)")
def test_gui_smoke_creates_project_via_real_clicks():
    pytest.importorskip("pyautogui")
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui_smoke.py")
    assert subprocess.run([sys.executable, script]).returncode == 0
