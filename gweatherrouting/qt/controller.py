"""Qt scheduling and worker notifications around the existing NMEA session."""

import logging
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from threading import Thread
from typing import Any
from weakref import WeakKeyDictionary

import nmeatoolkit as nt
from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import QMessageBox, QWidget

from gweatherrouting.core import Core, TimeControl
from gweatherrouting.core.settings import SettingsManager
from gweatherrouting.rendering.chartcollection import ChartCollection
from gweatherrouting.core.nmeasession import read_nmea_log, ripple_delete_log
from gweatherrouting.core.n2kdevices import captured_device_from_dict
from gweatherrouting.core.loglibrary import LogLibrary
from .scheduling import UiScheduler
from .routingcontroller import RoutingController
from .backgroundtask import BackgroundTask
from .datacontroller import DataController
from .polarcontroller import PolarController
from .mapdatasetup import MapDataController


class SessionController(QObject):
    changed = Signal()
    boat_changed = Signal(object)
    model_changed = Signal()
    load_finished = Signal(int, object, object)
    log_edit_finished = Signal(object, object, object)
    packets = Signal(object, int)
    renderer_changed = Signal()
    log_library_changed = Signal()
    load_progress = Signal(int, object)
    log_progress_changed = Signal()

    def __init__(
        self,
        recording_directory=None,
        parent=None,
        load_charts=True,
        map_renderer=None,
        early_weather=False,
        gpu_resident_fields=False,
        native_weather_fields=None,
        analysis_root=None,
        analysis_state=None,
        log_library_directory=None,
        gpu_streamlines=False,
    ):
        super().__init__(parent)
        if map_renderer not in (None, "cpu", "gpu"):
            raise ValueError("Map renderer must be cpu or gpu")
        self.load_charts = load_charts
        self.early_weather = early_weather
        self.gpu_resident_fields = gpu_resident_fields
        self.gpu_streamlines = gpu_streamlines
        from .gpuweathertextures import native_weather_fields_enabled

        self.native_weather_fields = native_weather_fields_enabled(
            native_weather_fields
        )
        self.native_weather_budget = None
        if self.native_weather_fields:
            from .gpuweathertextures import NativeWeatherBudget

            self.native_weather_budget = NativeWeatherBudget()
        self.scheduler = UiScheduler(self)
        self.core = Core(
            schedule=self.scheduler.submit,
            start_connections=False,
            recording_directory=recording_directory,
        )
        self.time_control = TimeControl()
        self.settings = SettingsManager()
        self.closed = False
        if analysis_root is not None and analysis_state is not None:
            raise ValueError("provide analysis_root or analysis_state, not both")
        self.analysis = None
        self.models_analysis = None
        self._analysis_root = analysis_root
        self._analysis_state = analysis_state
        selected_renderer = map_renderer or self.settings.get("mapRenderer", "cpu")
        self.map_renderer = (
            selected_renderer if selected_renderer in ("cpu", "gpu") else "cpu"
        )
        self.renderer_override = map_renderer is not None
        self._renderer_states: WeakKeyDictionary[Any, dict[str, Any]] = (
            WeakKeyDictionary()
        )
        self.charts = ChartCollection(self.settings, self.scheduler.submit)
        self.chart_errors = []
        if load_charts and not early_weather:
            self.charts.load_base_chart()
            for kind in ("vector", "raster", "mbtiles"):
                loader = getattr(self.charts, "load_" + kind + "_layer")
                for entry in self.settings[kind + "Charts"]:
                    try:
                        loader(
                            entry["path"],
                            entry.get("metadata"),
                            entry.get("enabled", True),
                        )
                    except Exception as exc:
                        self.chart_errors.append(str(exc))
                        logging.getLogger("gweatherrouting").warning(
                            "Unable to load chart %s: %s", entry["path"], exc
                        )
        self.core.connect("boat_data", self.boat_changed.emit)
        self.connections = self.core.connectionManager
        self.session = self.core.nmea_session
        self.log_library = LogLibrary(log_library_directory)
        self._replay_load_tokens = set()
        self.log_edit_busy = False
        self.log_edit_error = None
        self._log_edit_thread = None
        self._load_error_dialog = None
        self.log_progress = None
        self.routing = RoutingController(self)
        self.connection_task = BackgroundTask(self)
        self.data = DataController(self)
        self.analysis = self.prepare_model_analysis()
        from .analysisdisplay import BoundDisplayForecastSelection

        self.display_forecast = BoundDisplayForecastSelection(self.analysis, self)
        self.polars = PolarController(self)
        self.map_data = MapDataController(self)
        self.points = []
        self.point_revision = 0
        self.generation = 0
        self.closed = False
        self.translator = nt.TrackPointTranslator()
        self.wind_pipe = nt.TrueWindPipe()
        self.session.connect("changed", self._changed)
        self.session.connect("reset", self._reset)
        self.session.connect("loaded", self._loaded)
        self.session.connect("data", self._queue_packets)
        self.load_finished.connect(self._finish_load)
        self.log_edit_finished.connect(
            self._finish_log_range_delete, Qt.ConnectionType.QueuedConnection
        )
        self.load_progress.connect(
            self._receive_load_progress, Qt.ConnectionType.QueuedConnection
        )
        self.packets.connect(self._consume)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._tick)
        self.last_tick = time.monotonic()
        self.timer.start()
        self.seek_timer = QTimer(self)
        self.seek_timer.setInterval(1)
        self.seek_timer.timeout.connect(self._seek_tick)
        self.chart_startup = None
        if load_charts and early_weather:
            from .chartstartup import ChartStartup

            self.chart_startup = ChartStartup(self)

    def prepare_model_analysis(self):
        """Create/reuse the opt-in authority without switching display or routing."""
        if self.closed:
            raise RuntimeError("session controller is closed")
        if self.models_analysis is not None:
            return self.models_analysis
        from .analysiscontroller import AnalysisController

        if self._analysis_state is not None:
            analysis = AnalysisController(state=self._analysis_state, parent=self)
        else:
            root = self._analysis_root
            if root is None:
                from gweatherrouting.core.storage import DATA_DIR

                root = Path(DATA_DIR) / "forecast-analysis"
            analysis = AnalysisController(root, parent=self)
        self.models_analysis = analysis
        return analysis

    def set_renderer_status(self, canvas, actual, reason="", api=""):
        state = dict(actual=actual, reason=reason, api=api)
        if self._renderer_states.get(canvas) == state:
            return
        self._renderer_states[canvas] = state
        self.renderer_changed.emit()

    def forget_renderer(self, canvas):
        self._renderer_states.pop(canvas, None)
        self.renderer_changed.emit()

    def renderer_status(self):
        states = list(self._renderer_states.values())
        unavailable = [
            s["reason"] for s in states if s["actual"] == "unavailable" and s["reason"]
        ]
        if unavailable:
            return "Viewport unavailable: " + "; ".join(dict.fromkeys(unavailable))
        failures = [s["reason"] for s in states if s["reason"]]
        if failures:
            return "CPU fallback: " + "; ".join(dict.fromkeys(failures))
        apis = sorted({s["api"] for s in states if s["actual"] == "gpu" and s["api"]})
        if apis:
            return "GPU: " + ", ".join(apis)
        if self.map_renderer == "gpu":
            return "GPU requested; waiting for a visible map"
        return "CPU" + (" (command-line override)" if self.renderer_override else "")

    def start_connections(self):
        if (
            not self.closed
            and self.session.streaming
            and not self.connections.polling_active
        ):
            self.connection_task.start(
                "Connecting instruments…", lambda _: self.connections.start_polling()
            )

    def set_live_reading(self, active):
        """Start/stop device reads, not just delivery to the live display."""
        if self.closed or self.connection_task.busy:
            return
        self.session.set_streaming(active)
        if active:
            self.start_connections()
        elif self.connections.polling_active:
            self.connection_task.start(
                "Disconnecting instruments…", lambda _: self.connections.stop_polling()
            )

    def _changed(self, _):
        seek_timer = getattr(self, "seek_timer", None)
        if seek_timer is not None and not self.session.seeking:
            seek_timer.stop()
        progress = self.log_progress
        if progress is not None and (
            not self.session.loading or progress["token"] != self.session._generation
        ):
            self._clear_load_progress(progress["token"])
        self.changed.emit()

    def _reset(self, _):
        self.generation += 1
        self.translator = nt.TrackPointTranslator()
        self.wind_pipe = nt.TrueWindPipe()
        if self.session.seeking:
            # The loaded log's immutable chart/plot track has not changed.
            return
        self.points = (
            list(self.session.log.points)
            if self.session.source == "log" and self.session.log
            else []
        )
        self.point_revision += 1
        self.model_changed.emit()

    def _loaded(self, log):
        self.points = list(log.points)
        self.point_revision += 1
        self.model_changed.emit()

    def _queue_packets(self, packets):
        if self.session.source == "live":
            self.packets.emit(tuple(packets), self.generation)

    @Slot(object, int)
    def _consume(self, packets, generation):
        if (
            self.closed
            or generation != self.generation
            or self.session.source != "live"
            or not self.session.streaming
        ):
            return
        for packet in packets:
            sentence = packet.data
            if sentence is None or getattr(sentence, "sentence_type", "") in (
                "ALK",
                "VDM",
            ):
                continue
            try:
                for interpreted in self.wind_pipe.transform(sentence):
                    point = self.translator.feed(interpreted)
                    if point is not None:
                        self.points.append(point)
                        self.translator.track.clear()
                        self.point_revision += 1
            except (ValueError, TypeError, AttributeError):
                continue

    def _tick(self):
        now = time.monotonic()
        self.session.advance(now - self.last_tick)
        self.last_tick = now

    def preview_log_position(self, position):
        if self.closed:
            return False
        self.seek_timer.stop()
        return self.session.preview(position)

    def seek_log_position(self, position):
        if self.closed:
            return False
        if not self.session.begin_seek(position):
            return False
        self.last_tick = time.monotonic()
        self.seek_timer.start()
        return True

    def delete_log_range(self, start, end):
        if self.closed or self.log_edit_busy:
            logging.getLogger(__name__).info(
                "Controller ignored log delete: closed=%s busy=%s",
                self.closed,
                self.log_edit_busy,
            )
            return False
        self.seek_timer.stop()
        request = self.session.prepare_log_range_delete(start, end)
        if request is None:
            return False
        self.log_edit_busy = True
        self.log_edit_error = None
        self.changed.emit()
        logging.getLogger(__name__).info(
            "Controller building log delete for range (%r, %r) in background",
            request[1],
            request[2],
        )

        def build():
            try:
                edited = ripple_delete_log(request[0], request[1], request[2])
                error = None
            except Exception as exc:
                edited, error = None, exc
            self.log_edit_finished.emit(request, edited, error)

        self._log_edit_thread = Thread(target=build, daemon=True)
        self._log_edit_thread.start()
        return True

    @Slot(object, object, object)
    def _finish_log_range_delete(self, request, edited, error):
        if self.closed:
            return
        self.log_edit_busy = False
        self._log_edit_thread = None
        if error is not None:
            self.log_edit_error = str(error)
            logging.getLogger(__name__).error(
                "Background log delete failed: %s",
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
            self.changed.emit()
            return
        applied = self.session.apply_log_range_delete(request, edited)
        logging.getLogger(__name__).info(
            "Controller background log delete committed: %s", applied
        )
        self.changed.emit()

    def undo_log_edit(self):
        if self.closed or self.log_edit_busy:
            return False
        self.seek_timer.stop()
        return self.session.undo_log_edit()

    def save_log_edits(self):
        if self.closed or self.log_edit_busy or not self.session.log_edits_dirty:
            return False
        self.log_library.save_log(self.session.log)
        self.session.mark_log_saved()
        self.log_library_changed.emit()
        return True

    def _seek_tick(self):
        if self.closed or self.session.seek_batch(limit=1000):
            self.seek_timer.stop()
            self.last_tick = time.monotonic()

    def load_path(self, path):
        return self.load_paths([path])

    @Slot(int, object)
    def _receive_load_progress(self, token, progress):
        """Only this GUI-thread slot publishes worker progress to views."""
        if self.closed or token != self.session._generation or not self.session.loading:
            self._clear_load_progress(token)
            return
        self.log_progress = {**progress, "token": token}
        self.log_progress_changed.emit()

    def _clear_load_progress(self, token):
        if self.log_progress is not None and self.log_progress["token"] == token:
            self.log_progress = None
            self.log_progress_changed.emit()

    def load_paths(self, paths, replay=False):
        """Import each log/EBL stack; only the latest request selects or replays."""
        if self.closed or not paths:
            return False
        paths = tuple(paths)
        if not paths:
            return False
        if self.session.streaming:
            stop_live = getattr(self, "set_live_reading", None)
            if callable(stop_live):
                stop_live(False)
            else:
                self.session.set_streaming(False)
        groups = self.log_library.group_paths(paths)
        token = self.session.begin_load()
        if replay:
            self._replay_load_tokens.add(token)
        else:
            self._replay_load_tokens.discard(token)
        self._receive_load_progress(
            token,
            dict(
                path=str(paths[0]),
                index=1,
                count=len(groups),
                phase="copy",
                done=0,
                total=0,
            ),
        )

        def read():
            log, errors = None, []
            for index, group in enumerate(groups, 1):
                path = group[0]
                if self.closed:
                    return
                item = None

                def reporter(phase):
                    last = None
                    last_total = None

                    def report(done, total):
                        nonlocal last, last_total
                        now = time.monotonic()
                        # Bound the Qt event queue even on fast local disks.
                        if (
                            last is not None
                            and total == last_total
                            and 0 < done < total
                            and now - last < 0.1
                        ):
                            return
                        last = now
                        last_total = total
                        if not self.closed:
                            self.load_progress.emit(
                                token,
                                dict(
                                    path=str(path),
                                    index=index,
                                    count=len(groups),
                                    phase=phase,
                                    done=done,
                                    total=total,
                                ),
                            )

                    return report

                try:
                    copy_progress = reporter("copy")
                    # Announce even before path validation or a metadata-lock
                    # wait, using the original filename rather than its UUID.
                    copy_progress(0, 0)
                    item = self.log_library.import_paths(
                        group,
                        progress=copy_progress,
                        decode_progress=reporter("decode"),
                    )[0]
                    existing_info = item.get("file_info", {})
                    candidate = item.get("_decoded_log")
                    if candidate is None:
                        candidate = read_nmea_log(
                            self.log_library.path(item["id"]),
                            progress=reporter("decode"),
                            frame_count=existing_info.get("frame_count"),
                            devices=tuple(
                                (
                                    captured_device_from_dict(d)
                                    if isinstance(d, dict)
                                    else d
                                )
                                for d in existing_info.get("devices", [])
                            ),
                            device_metadata_available=existing_info.get(
                                "device_metadata_available", False
                            ),
                            timestamp_origin=(
                                datetime.fromisoformat(
                                    existing_info["timestamp_origin"]
                                )
                                if existing_info.get("timestamp_origin")
                                else None
                            ),
                        )
                    # Use original EBL format for status if this was decoded from EBL
                    original_format = existing_info.get("format", candidate.format)
                    original_frame_count = existing_info.get("frame_count")
                    self.log_library.update(
                        item["id"],
                        status=(
                            (
                                f"{original_format}: {original_frame_count:,} raw CAN frames; "
                                f"{len(candidate.entries):,} replay entries"
                            )
                            if candidate.entries and original_frame_count is not None
                            else (
                                f"{candidate.format}: {len(candidate.entries):,} replay entries"
                                if candidate.entries
                                else "Archive only — no supported replay data"
                            )
                        ),
                        diagnostics=list(candidate.diagnostics),
                        file_info=(
                            {
                                "path": str(self.log_library.path(item["id"])),
                                "format": candidate.format,
                                "entries": len(candidate.entries),
                                "duration": candidate.duration,
                                "timestamped": candidate.timestamped,
                                "skipped": candidate.skipped,
                                "frame_count": candidate.frame_count,
                                "source_tracks": list(candidate.source_tracks),
                                "device_metadata_available": (
                                    candidate.device_metadata_available
                                    or existing_info.get(
                                        "device_metadata_available", False
                                    )
                                ),
                                "devices": (
                                    [asdict(device) for device in candidate.devices]
                                    if candidate.devices
                                    else existing_info.get("devices", [])
                                ),
                                "diagnostics": list(candidate.diagnostics),
                            }
                            if candidate.device_metadata_available
                            or not existing_info.get("devices")
                            else existing_info
                        ),
                    )
                    log = candidate
                except Exception as exc:
                    errors.append(f"{path}: {exc}")
                    if item is not None:
                        try:
                            self.log_library.update(
                                item["id"], status=f"Cannot replay: {exc}"
                            )
                        except Exception:
                            pass
            error = ValueError("\n".join(errors)) if errors else None
            if not self.closed:
                self.load_finished.emit(token, log, error)

        try:
            Thread(target=read, daemon=True).start()
        except Exception as exc:
            self._finish_load(token, None, exc)
            return False
        return True

    @Slot(int, object, object)
    def _finish_load(self, token, log, error):
        # Consume intent before emitting signals: their handlers may start and
        # even finish another import before this completion returns.
        replay = token in self._replay_load_tokens
        self._replay_load_tokens.discard(token)
        self._clear_load_progress(token)
        if not self.closed:
            if token != self.session._generation:
                self.log_library_changed.emit()
                return
            self.session.finish_load(token, log, error)
            self.log_library_changed.emit()
            if (
                error is not None
                and not self.closed
                and token == self.session._generation
            ):
                self._show_load_error(error)
            if (
                replay
                and not self.closed
                and token == self.session._generation
                and log is not None
                and log.entries
            ):
                self.session.set_playing(True)

    def _show_load_error(self, error):
        """Report failures on the GUI thread without a removed verbose panel."""
        if self._load_error_dialog is not None:
            self._load_error_dialog.close()
        parent = self.parent()
        dialog = QMessageBox(parent if isinstance(parent, QWidget) else None)
        dialog.setWindowTitle("Cannot replay log")
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText("One or more logs could not be opened for replay.")
        message = str(error) or "Unknown load error"
        dialog.setInformativeText(message.splitlines()[0])
        dialog.setDetailedText(message)
        dialog.setStandardButtons(QMessageBox.StandardButton.Ok)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        def finished():
            if self._load_error_dialog is dialog:
                self._load_error_dialog = None

        dialog.finished.connect(finished)
        self._load_error_dialog = dialog
        dialog.open()

    def remove_library_log(self, identifier):
        if self.session.loading:
            raise ValueError("Wait for log import to finish before removing a log")
        path = self.log_library.path(identifier)
        self.log_library.remove(identifier)
        if self.session.log is not None and self.session.log.path == str(path):
            self.session.select_source("live")
            self.session.log = None
            self.session.error = ""
            self.changed.emit()
        self.log_library_changed.emit()

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self._load_error_dialog is not None:
            self._load_error_dialog.close()
            self._load_error_dialog = None
        if self.log_progress is not None:
            self._clear_load_progress(self.log_progress["token"])
        self.display_forecast.close()
        if self.models_analysis is not None:
            self.models_analysis.close()
        if self.chart_startup is not None:
            self.chart_startup.close()
        self.routing.close()
        self.map_data.close()
        self.timer.stop()
        self.seek_timer.stop()
        self.polars.close()
        self.data.close()
        self.connection_task.close(wait=True)
        self.connections.stop_polling()
        self.session.close()
        self.core.nmea_session.disconnect("data", self.core.data_handler)
        for canvas in list(self.charts.maps):
            canvas.dispose()
        self.core.disconnect("boat_data", self.boat_changed.emit)
        self.core.nmea_session.disconnect("reset", self.core._reset_nmea)
        self.scheduler.close()
        for event, callback in (
            ("changed", self._changed),
            ("reset", self._reset),
            ("loaded", self._loaded),
            ("data", self._queue_packets),
        ):
            self.session.disconnect(event, callback)
