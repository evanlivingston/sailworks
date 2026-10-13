"""Use the platform's native picker for file and folder selection."""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk


def file_chooser(title, parent, action):
    label = {
        Gtk.FileChooserAction.OPEN: "Open",
        Gtk.FileChooserAction.SAVE: "Save",
        Gtk.FileChooserAction.SELECT_FOLDER: "Select",
    }[action]
    dialog = Gtk.FileChooserNative.new(title, parent, action, label, "Cancel")
    dialog.set_modal(True)
    if action == Gtk.FileChooserAction.SAVE:
        dialog.set_do_overwrite_confirmation(True)
    return dialog
