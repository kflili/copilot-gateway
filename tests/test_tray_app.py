"""Focused regressions for the Windows tray menu."""

from __future__ import annotations

import pathlib
import sys
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import tray_app  # noqa: E402


class FakeMenuItem:
    def __init__(self, text, action, **kwargs):
        self.text = text
        self.action = action
        self.kwargs = kwargs


class FakeMenu:
    SEPARATOR = object()

    def __init__(self, *items):
        self.items = items


class FakeIcon:
    def __init__(self, name, image, title, menu):
        self.name = name
        self.image = image
        self.title = title
        self.menu = menu


class TrayMenuTests(unittest.TestCase):
    def make_ui(self):
        ui = tray_app.TrayUI.__new__(tray_app.TrayUI)
        ui.gateway = types.SimpleNamespace(attached=False)
        ui.distros = []
        ui.win_enabled = False
        ui.wsl_enabled_distros = set()
        return ui

    def test_open_demo_item_order_and_tk_marshalling(self):
        pystray = types.SimpleNamespace(
            MenuItem=FakeMenuItem,
            Menu=FakeMenu,
            Icon=FakeIcon,
        )
        ui = self.make_ui()
        callback = mock.Mock()
        ui._open_demo_ui = callback
        ui._marshal = mock.Mock()

        with (
            mock.patch.dict(sys.modules, {"pystray": pystray}),
            mock.patch.object(tray_app, "_build_tray_icon_image",
                              return_value=object()),
            mock.patch.object(tray_app.shutil, "which", return_value=None),
        ):
            icon = ui._build_icon()

        self.assertEqual(
            [item.text for item in icon.menu.items[:3]],
            ["Stats…", "Open Demo UI", "View logs…"],
        )
        icon.menu.items[1].action(None, None)
        ui._marshal.assert_called_once_with(callback)

    def test_marshal_schedules_callback_on_tk_root(self):
        ui = self.make_ui()
        ui.root = types.SimpleNamespace(after=mock.Mock())
        callback = mock.Mock()

        ui._marshal(callback)

        ui.root.after.assert_called_once_with(0, callback)

    def test_open_demo_uses_fixed_url(self):
        ui = self.make_ui()
        ui._toast = mock.Mock()

        with mock.patch.object(tray_app.webbrowser, "open",
                               return_value=True) as browser_open:
            ui._open_demo_ui()

        browser_open.assert_called_once_with("http://127.0.0.1:8788/")
        ui._toast.assert_not_called()

    def test_open_demo_reports_declined_or_failed_launch(self):
        for result in (
            False,
            OSError("browser unavailable"),
            tray_app.webbrowser.Error("no runnable browser"),
        ):
            with self.subTest(result=result):
                ui = self.make_ui()
                ui._toast = mock.Mock()
                effect = {"return_value": result}
                if isinstance(result, Exception):
                    effect = {"side_effect": result}

                with mock.patch.object(tray_app.webbrowser, "open", **effect):
                    ui._open_demo_ui()

                ui._toast.assert_called_once()
                self.assertEqual(
                    ui._toast.call_args,
                    mock.call(
                        "Open Demo UI failed",
                        mock.ANY,
                        ok=False,
                    ),
                )
                self.assertIn(
                    "http://127.0.0.1:8788/",
                    ui._toast.call_args.args[1],
                )


if __name__ == "__main__":
    unittest.main()
