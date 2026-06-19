#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Kevin Griffin
"""
gui_smoke.py — opt-in end-to-end GUI smoke for the Tk app, driven by pyautogui.

This is the one check that exercises the REAL window with REAL mouse clicks and
keystrokes — catching what headless tests can't (is a control actually on
screen, clickable, and focused?). It is deliberately NOT part of the pytest
suite by default: it needs a display, it moves your mouse and types, and on
macOS it needs Accessibility permission.

Prereqs (macOS):
  pip install pyautogui
  System Settings → Privacy & Security → Accessibility → enable your terminal
  (Terminal/iTerm). Screen Recording too if you later add screenshots.

Run (don't touch the mouse while it runs; slam the pointer into a screen corner
to abort via pyautogui's failsafe):
  DV2MV_MEDIA=/path/to/media python3 tests/gui_smoke.py

What it does: opens the app, clicks "New…", clicks the name field and types a
project name, clicks "Create", and asserts the project landed on disk — then
removes the smoke project. Exit code 0 = pass.

Driving pattern: every step runs in an app.after() callback on the MAIN thread
(so Tk is only ever touched there — the same rule that keeps the app from the
Tcl_AsyncDelete crash). Each step computes a widget's screen coords from the
main thread, issues a pyautogui click (an OS event the mainloop processes once
the callback returns), then schedules the next step after a short delay.
"""
import gc
import os
import shutil
import sys

DV2MV_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, DV2MV_DIR)

try:
    import pyautogui
except ImportError:
    sys.exit("pyautogui not installed — `pip install pyautogui`")

import tkinter as tk
from tkinter import ttk

import engine
import tkapp

pyautogui.PAUSE = 0.3
pyautogui.FAILSAFE = True            # mouse to a corner aborts
PROJECT_NAME = "PyAutoGUI Smoke"


def find_widget(root, cls, text=None):
    """Depth-first find a widget by class and (optional) substring of its text."""
    for w in root.winfo_children():
        try:
            if isinstance(w, cls) and (text is None or text in str(w.cget("text"))):
                return w
        except tk.TclError:
            pass
        hit = find_widget(w, cls, text)
        if hit:
            return hit
    return None


def center(w):
    """Screen-coordinate center of a widget (must be called on the main thread)."""
    w.update_idletasks()
    return (w.winfo_rootx() + w.winfo_width() // 2,
            w.winfo_rooty() + w.winfo_height() // 2)


def click(x, y):
    """A deliberate click — move, then hold the button down briefly before
    releasing. An instantaneous pyautogui.click() is sometimes missed by
    Tk/AppKit on macOS."""
    import time
    pyautogui.moveTo(x, y, duration=0.15)
    pyautogui.mouseDown()
    time.sleep(0.10)
    pyautogui.mouseUp()


class Smoke:
    def __init__(self, app):
        self.app = app
        self.passed = False

    def run(self):
        self.app.after(1200, self._activate)          # let the window map first

    def _activate(self):
        # macOS: clicking a background window only ACTIVATES it (Tk doesn't
        # click-through), so a throwaway click on the banner brings it forward
        # before the real button click lands.
        self.app.lift()
        self.app.focus_force()
        self.app.update_idletasks()
        print(f"· activate window @ {self.app.winfo_rootx() + 12},{self.app.winfo_rooty() + 12}")
        click(self.app.winfo_rootx() + 12, self.app.winfo_rooty() + 12)
        self.app.after(500, self._click_new)

    def _click_new(self):
        btn = find_widget(self.app, ttk.Button, "New")
        if not btn:
            return self._finish(False, "could not find the New… button")
        x, y = center(btn)
        print(f"· click New… @ {x},{y}")
        click(x, y)
        self.app.after(1200, self._fill_and_create)

    def _fill_and_create(self):
        dlg = next((w for w in self.app.winfo_children()
                    if isinstance(w, tk.Toplevel)), None)
        if not dlg:
            return self._finish(False, "New Project dialog did not open")
        name_entry = find_widget(dlg, ttk.Entry)        # first entry = name
        if name_entry:
            ex, ey = center(name_entry)
            print(f"· click name field @ {ex},{ey}, type {PROJECT_NAME!r}")
            click(ex, ey)
        pyautogui.typewrite(PROJECT_NAME, interval=0.04)
        create = find_widget(dlg, ttk.Button, "Create")
        if not create:
            return self._finish(False, "could not find the Create button")
        x, y = center(create)
        print(f"· click Create @ {x},{y}")
        click(x, y)
        self.app.after(1200, self._verify)

    def _verify(self):
        names = engine.list_projects(engine.MEDIA)
        if PROJECT_NAME in names:
            self._finish(True, "project created via real clicks/keystrokes")
        else:
            self._finish(False, f"project not created (have: {names})")

    def _finish(self, ok, msg):
        print(("PASS: " if ok else "FAIL: ") + msg)
        d = os.path.join(engine.projects_root(engine.MEDIA),
                         engine._safe_name(PROJECT_NAME))
        shutil.rmtree(d, ignore_errors=True)            # clean up the smoke project
        self.passed = ok
        self.app.quit()


def main():
    gc.disable()                          # mirror the real app (see tkapp __main__)
    app = tkapp.App()
    app.geometry("+80+80")
    app.lift()
    app.focus_force()
    smoke = Smoke(app)
    smoke.run()
    app.mainloop()
    try:
        app.destroy()
    except tk.TclError:
        pass
    print("done." if smoke.passed else "GUI smoke FAILED")
    sys.exit(0 if smoke.passed else 1)


if __name__ == "__main__":
    main()
