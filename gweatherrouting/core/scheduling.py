"""Default GTK scheduling, injected by other frontends at their UI boundary."""


def schedule_idle(callback, *args):
    try:
        from gi.repository import GLib
    except ImportError:
        return callback(*args)
    return GLib.idle_add(callback, *args)
