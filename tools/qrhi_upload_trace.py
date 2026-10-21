"""LLDB callback for Qt's real texture-upload submission boundary.

Loaded only by an explicit diagnostic run. Counts the canonical description
overload (the QImage convenience overload forwards to it), not both overloads.
Records submission, NOT GPU completion, physical transfer bytes or presentation.
"""

import json
import os
import time


def _write(event):
    with open(os.environ["GWR_UPLOAD_TRACE_OUTPUT"], "a", encoding="utf-8") as stream:
        stream.write(json.dumps(event) + "\n")


def on_upload(frame, _location, _dictionary):
    triple = frame.GetThread().GetProcess().GetTarget().GetTriple()
    register = "x1" if triple.startswith("arm64") else "rsi"
    _write(
        {
            "event": "uploadTexture",
            "time": time.time(),
            "texture": frame.FindRegister(register).GetValueAsUnsigned(),
        }
    )
    return False


def write_process_status(debugger):
    """Persist terminal child status; a completed report alone is not exit proof."""
    import lldb

    process = debugger.GetSelectedTarget().GetProcess()
    _write(
        {
            "event": "process_status",
            "exited": process.GetState() == lldb.eStateExited,
            "exit_code": process.GetExitStatus(),
        }
    )


def __lldb_init_module(debugger, _dictionary):
    target = debugger.GetSelectedTarget()
    triple = target.GetTriple()
    if not triple.startswith(("arm64", "x86_64")):
        raise RuntimeError(f"unsupported QRhi trace target ABI: {triple}")
    breakpoint = target.BreakpointCreateByRegex(
        r"QRhiResourceUpdateBatch::uploadTexture\(QRhiTexture\*, QRhiTextureUploadDescription const&\)"
    )
    breakpoint.SetScriptCallbackFunction(__name__ + ".on_upload")
    _write({"event": "configured", "target": triple})
