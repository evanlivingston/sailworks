"""Saved account controls for worldwide current downloads."""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk


class CurrentsSettingsPanel(Gtk.Box):
    def __init__(self, planner):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=20)
        self.planner = planner
        self._label("Worldwide currents · Copernicus Marine account")
        self.username = Gtk.Entry(placeholder_text="Username or email")
        self.password = Gtk.Entry(placeholder_text="Password", visibility=False)
        credentials = planner.worldwide.credentials
        if credentials:
            self.username.set_text(credentials[0])
        self.pack_start(self.username, False, False, 0)
        self.pack_start(self.password, False, False, 0)
        apply = Gtk.Button(label="Save account")
        apply.connect("clicked", self._apply_account)
        self.pack_start(apply, False, False, 0)
        forget = Gtk.Button(label="Forget account")
        forget.connect("clicked", self._forget_account)
        self.pack_start(forget, False, False, 0)
        self.account_status = self._label(
            planner.worldwide.credential_error
            or ("Saved account ready." if planner.worldwide.credentials_saved else "")
        )
        self.pack_start(
            Gtk.LinkButton(
                uri="https://data.marine.copernicus.eu/register",
                label="Create a free account",
            ),
            False,
            False,
            0,
        )
        self.connect("unmap", lambda *_: self.password.set_text(""))
        self.connect("destroy", lambda *_: self.password.set_text(""))

    def _label(self, text):
        label = Gtk.Label(label=text, xalign=0, wrap=True)
        label.set_max_width_chars(60)
        self.pack_start(label, False, False, 0)
        return label

    def _apply_account(self, button):
        try:
            self.planner.worldwide.configure(
                self.username.get_text(), self.password.get_text()
            )
            self.account_status.set_text(
                "Account saved. Download data to connect."
                if self.planner.worldwide.credentials_saved
                else "Account set for this session."
            )
        except ValueError as error:
            self.account_status.set_text(str(error))
        finally:
            self.password.set_text("")

    def _forget_account(self, button):
        try:
            self.planner.worldwide.forget_account()
            self.username.set_text("")
            self.account_status.set_text("Saved account removed.")
        except ValueError as error:
            self.account_status.set_text(str(error))
        finally:
            self.password.set_text("")
