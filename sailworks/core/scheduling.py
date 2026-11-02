"""Synchronous default scheduling; the Qt frontend injects its UI scheduler."""


def schedule_idle(callback, *args):
    return callback(*args)
