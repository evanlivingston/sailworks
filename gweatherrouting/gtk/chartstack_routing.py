# -*- coding: utf-8 -*-
# Copyright (C) 2017-2026 Davide Gessa
"""
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

For detail about GNU see <http://www.gnu.org/licenses/>.
"""

import datetime
import logging
import math
import os
import time
from threading import Event, Thread, current_thread, main_thread

import dateutil.parser
import gi

from gweatherrouting.core.modifiedgrib import ModifiedGribManager
from gweatherrouting.core.storage import POLAR_DIR
from gweatherrouting.core.marine.routing import (
    routing_utc,
    step_with_coverage,
    routing_completion,
)
from gweatherrouting.core.marine.chs import DownloadCancelledError
from gweatherrouting.core.marine.preflight import check_current_coverage
from gweatherrouting.core.marine.prefetch import CoveragePrefetch
from gweatherrouting.core.marine.windcoverage import WindCoverageRecovery
from gweatherrouting.core.marine.routing import RoutingDataGapError

gi.require_version("Gtk", "3.0")

from gi.repository import Gdk, GLib, Gtk
from weatherrouting import RoutingNoWindError

from gweatherrouting.core import utils
from gweatherrouting.core.geo.poi import POI
from gweatherrouting.core.geo.routing import Routing

from .chartstack_base import ChartStackBase
from .maplayers import IsochronesMapLayer
from .routingwizarddialog import RoutingWizardDialog

logger = logging.getLogger("gweatherrouting")


class ChartStackRouting(ChartStackBase):
    routing_thread = None
    selected_routing = None
    stop_routing = False

    def __init__(self):
        self.currentRouting = None
        self.routingListStore = self.builder.get_object("routing-list-store")
        self.routingPointsStore = self.builder.get_object("routing-points-store")

        self.isochronesMapLayer = IsochronesMapLayer()
        self.map.layer_add(self.isochronesMapLayer)
        self.map.connect("destroy", self._close_routing_ui)

        self.update_routings()

    def __del__(self):
        if self.routing_thread:
            self.on_routing_stop(None)

    def on_route_to(self, widget):
        lat, lon = self.get_lat_lon()
        poi = self.core.poiManager.create(
            (
                lat,
                lon,
            )
        )
        self.on_routing_create(None)
        self.core.poiManager.remove(poi)

    def on_routing_create(self, event):
        if self.routing_thread is not None and self.routing_thread.is_alive():
            return
        dialog = RoutingWizardDialog(self.core, self.parent)

        while True:
            response = dialog.run()

            if response != Gtk.ResponseType.OK:
                break

            if dialog.get_selected_start_point() is None and isinstance(
                dialog.get_selected_track_or_poi(), POI
            ):
                edialog = Gtk.MessageDialog(
                    self.parent,
                    0,
                    Gtk.MessageType.ERROR,
                    Gtk.ButtonsType.OK,
                    "Invalid start position",
                )
                edialog.format_secondary_text(
                    '"First track point" cannot be used when the destination is a POI. '
                    "Please select a track as destination or choose a different start position."
                )
                edialog.run()
                edialog.destroy()
                continue

            polar_file = dialog.get_selected_polar()
            scenarios = dialog.get_comparison_scenarios()
            self._auto_currents = dialog.get_auto_currents()

            if not scenarios:
                # Single routing, same as before
                self.stop_routing = False
                self.currentRouting = self.core.create_routing(
                    dialog.get_selected_algorithm(),
                    os.path.join(POLAR_DIR, polar_file),
                    dialog.get_selected_track_or_poi(),
                    dialog.get_start_datetime(),
                    dialog.get_selected_start_point(),
                    self.chart_manager.get_line_point_validity_providers(),
                    not dialog.get_coastline_checks(),
                    use_currents=dialog.get_use_currents(),
                )
                self.currentRouting.name = "routing-" + polar_file.split(".")[0]
                self.routing_thread = Thread(target=self.on_routing_step, args=())
                self.routing_thread.start()
                self.builder.get_object("stop-routing-button").show()
            else:
                # Multiple scenario comparison
                self._comparison_params = {
                    "algorithm": dialog.get_selected_algorithm(),
                    "polar_file": os.path.join(POLAR_DIR, polar_file),
                    "polar_name": polar_file.split(".")[0],
                    "track_or_poi": dialog.get_selected_track_or_poi(),
                    "start_datetime": dialog.get_start_datetime(),
                    "start_position": dialog.get_selected_start_point(),
                    "validity_providers": self.chart_manager.get_line_point_validity_providers(),
                    "disable_coastline": not dialog.get_coastline_checks(),
                    "scenarios": scenarios,
                    "use_currents": dialog.get_use_currents(),
                }
                self.stop_routing = False
                self.routing_thread = Thread(
                    target=self.on_comparison_routing_step, args=()
                )
                self.routing_thread.start()
                self.builder.get_object("stop-routing-button").show()
            break

        dialog.destroy()

    def _build_scenario_name(self, polar_name, scenario):
        """Build a descriptive name for a comparison scenario routing."""
        parts = [f"routing-{polar_name}"]
        t = scenario["time_offset_hours"]
        if t != 0:
            parts.append(f"t{t:+.0f}h")
        ws = scenario["wind_speed_pct"]
        if ws != 0:
            parts.append(f"ws{ws:+.0f}%")
        wd = scenario["wind_dir_offset"]
        if wd != 0:
            parts.append(f"wd{wd:+.0f}deg")
        pe = scenario["polar_efficiency_pct"]
        if pe != 100:
            parts.append(f"eff{pe:.0f}%")
        return "_".join(parts)

    def _show_routing_error(self, message):
        self._routing_ui(self._display_routing_error, message)

    def _routing_ui(self, callback, *args):
        """Run GTK work on the main thread; propagate Python failures to the worker."""
        if getattr(self, "_routing_ui_closed", False):
            raise DownloadCancelledError()
        if current_thread() is main_thread():
            return callback(*args)
        done = Event()
        result, errors = [], []

        def invoke():
            # MainWindow enables the legacy GDK lock. Gtk.Dialog.run releases
            # and reacquires it; an idle callback must enter/leave symmetrically
            # or closing the dialog leaves the main loop locked against itself.
            Gdk.threads_enter()
            try:
                if getattr(self, "_routing_ui_closed", False):
                    raise DownloadCancelledError()
                result.append(callback(*args))
            except Exception as error:
                errors.append(error)
            finally:
                Gdk.threads_leave()
                done.set()
            return False

        GLib.idle_add(invoke)
        while not done.wait(0.1):
            if getattr(self, "_routing_ui_closed", False):
                raise DownloadCancelledError()
        if errors:
            raise errors[0]
        return result[0]

    def _close_routing_ui(self, *args):
        self._routing_ui_closed = True
        self.on_routing_stop(None)

    def _run_routing_worker(self, calculate):
        try:
            calculate()
        except DownloadCancelledError:
            self.stop_routing = True
            if not getattr(self, "_routing_ui_closed", False):
                self._routing_ui(self._finish_routing_ui, "Routing cancelled", 0)
        except Exception as error:
            logger.exception("Routing failed")
            if not getattr(self, "_routing_ui_closed", False):
                self._show_routing_error(str(error))
        finally:
            prefetch = getattr(self, "_routing_prefetch", None)
            if prefetch is not None:
                prefetch.close()
            wind_recovery = getattr(self, "_wind_recovery", None)
            if wind_recovery is not None:
                wind_recovery.close()

    def _display_routing_error(self, message):
        self._finish_routing_ui(None)
        edialog = Gtk.MessageDialog(
            transient_for=self.parent,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text="Routing failed",
        )
        edialog.format_secondary_text(message)
        edialog.run()
        edialog.destroy()

    def _set_routing_progress(self, percent, text, res=None):
        self.progress_bar.set_fraction(percent / 100)
        self.progress_bar.set_text(text)
        self.progress_bar.show()
        if res is not None:
            self.isochronesMapLayer.set_isochrones(
                res.isochrones, res.path, getattr(res, "excluded_current_samples", ())
            )
            self.time_control.set_time(
                res.time.astimezone().replace(tzinfo=None)
                if res.time.tzinfo is not None
                else res.time
            )

    def _finish_routing_ui(self, text, percent=100):
        self._pending_search_progress = None
        if text is None:
            self.progress_bar.hide()
        else:
            self._set_routing_progress(percent, text)
            GLib.timeout_add(
                3000, self._hide_finished_routing_progress, self.currentRouting
            )
        self.builder.get_object("stop-routing-button").hide()
        self.isochronesMapLayer.set_isochrones([], [])
        self.map.queue_draw()

    def _hide_finished_routing_progress(self, routing):
        if routing is self.currentRouting:
            self.progress_bar.hide()
        return False

    def _store_routing(self, points, res, provenance):
        routing = self.currentRouting
        if routing is None:
            raise ValueError("Routing is no longer available")
        self.core.routingManager.append(
            Routing(
                name=self.core.routingManager.get_unique_name(routing.name),
                points=points,
                isochrones=res.isochrones,
                collection=self.core.routingManager,
                provenance=provenance,
            )
        )

    def _run_single_scenario(self, scenario, params, idx, total):  # noqa: C901
        """Run a single comparison scenario. Returns True on success."""
        wind_speed_factor = 1.0 + scenario["wind_speed_pct"] / 100.0
        wind_dir_offset = scenario["wind_dir_offset"]
        polar_efficiency = scenario["polar_efficiency_pct"] / 100.0
        time_offset = datetime.timedelta(hours=scenario["time_offset_hours"])

        grib_override = None
        if wind_speed_factor != 1.0 or wind_dir_offset != 0.0:
            grib_override = ModifiedGribManager(
                self.core.grib_manager,
                wind_speed_factor=wind_speed_factor,
                wind_dir_offset=wind_dir_offset,
            )

        self.currentRouting = self.core.create_routing(
            params["algorithm"],
            params["polar_file"],
            params["track_or_poi"],
            params["start_datetime"] + time_offset,
            params["start_position"],
            params["validity_providers"],
            params["disable_coastline"],
            grib_override=grib_override,
            polar_efficiency=polar_efficiency if polar_efficiency != 1.0 else None,
            use_currents=params["use_currents"],
        )
        self.currentRouting.name = self._build_scenario_name(
            params["polar_name"], scenario
        )

        # Check if GRIB data covers the routing start area and time
        self._prepare_routing_wind()
        pos = self.currentRouting.position
        wind = self.currentRouting.grib.get_wind_at(
            self.currentRouting.time, pos[0], pos[1]
        )
        if wind is None:
            self._show_routing_error(
                "No GRIB data available for the routing area and time. "
                "Please load a GRIB file that covers the routing region "
                "and time period."
            )
            return False

        label = f"Scenario {idx + 1}/{total}"
        if not self._check_current_coverage():
            return False
        self._progress_label = label
        routing_completion(self.currentRouting)
        self._routing_ui(self._set_routing_progress, 0, f"{label}: 0%")

        res = None
        while (not self.currentRouting.end) and (not self.stop_routing):
            try:
                res = self._step_with_currents()
            except DownloadCancelledError:
                return False
            except RoutingNoWindError:
                self._show_routing_error(f"{label}: No wind information available")
                return False
            except RoutingDataGapError as error:
                logger.warning("%s: %s", label, error)
                self._show_routing_error(str(error))
                return False
            except Exception as e:
                logger.exception("Comparison routing step failed")
                self._show_routing_error(f"{label}: {e}")
                return False

            percent = routing_completion(self.currentRouting)
            self._routing_ui(
                self._set_routing_progress, percent, f"{label}: ≈{percent:.0f}%", res
            )

        if self.stop_routing or res is None:
            return False

        tr = [
            (
                wp.pos[0],
                wp.pos[1],
                (
                    routing_utc(wp.time).isoformat()
                    if self.currentRouting.current_environment is not None
                    else wp.time.strftime("%m/%d/%Y, %H:%M:%S")
                ),
                (
                    math.degrees(wp.twd) % 360
                    if self.currentRouting.current_environment is not None
                    else wp.twd
                ),
                wp.tws,
                wp.speed,
                wp.brg,
            )
            for wp in res.path
        ]
        self._routing_ui(
            self._store_routing, tr, res, self._current_provenance(res.path)
        )
        return True

    def on_comparison_routing_step(self):
        self._run_routing_worker(self._calculate_comparison_routes)

    def _calculate_comparison_routes(self):
        params = self._comparison_params
        scenarios = params["scenarios"]
        total = len(scenarios)
        completed = 0

        for idx, scenario in enumerate(scenarios):
            if self.stop_routing:
                break
            if not self._run_single_scenario(scenario, params, idx, total):
                break
            completed += 1

        self._routing_ui(
            self._finish_routing_ui,
            f"{completed}/{total} scenarios complete",
            100 * completed / total,
        )
        self._routing_ui(self.update_routings)

    def on_routing_step(self):
        self._run_routing_worker(self._calculate_single_route)

    def _prepare_routing_wind(self):
        previous = getattr(self, "_wind_recovery", None)
        if previous is not None:
            previous.close()
        self._wind_recovery = None
        routing = self.currentRouting
        environment = getattr(routing, "current_environment", None)
        if environment is None or not getattr(self, "_auto_currents", False):
            return
        assert routing is not None
        recovery = WindCoverageRecovery(
            routing,
            save=lambda gribs: self._routing_ui(self._save_routing_gribs, gribs),
        )
        environment.wind_recovery = recovery
        self._wind_recovery = recovery
        from gweatherrouting.core.marine.routing import DataGap

        self._routing_ui(self._show_download_progress, "Checking latest wind forecast…")
        recovery.refresh()
        gaps = recovery.missing(
            [DataGap(tuple(routing.position), routing.time, "wind", "no_coverage")]
        )
        if gaps:
            self._routing_ui(
                self._show_download_progress, "Downloading wind data for departure…"
            )
            recovery.recover(gaps)

    def _save_routing_gribs(self, gribs):
        manager = self.core.grib_manager
        for grib in gribs:
            if not manager.is_enabled(grib.name):
                manager.load(grib.path)
        manager.store_opened_gribs()
        manager.refresh_local_gribs()

    def _check_current_coverage(self):
        previous = getattr(self, "_routing_prefetch", None)
        if previous is not None:
            previous.close()
            self._routing_prefetch = None
        if getattr(self.currentRouting, "current_environment", None) is None:
            return True
        self._routing_ui(self._set_routing_progress, 0, "Checking current coverage…")
        report = check_current_coverage(
            self.currentRouting, getattr(self, "_auto_currents", False)
        )
        if report.needs_warning:
            proceed = self._routing_ui(self._show_current_preflight, report)
            if not proceed or self.stop_routing:
                self._routing_ui(self._finish_routing_ui, "Routing not started", 0)
                return False
        if getattr(self, "_auto_currents", False) and not self.stop_routing:
            environment = getattr(self.currentRouting, "current_environment", None)
            assert environment is not None
            environment.prefetch = CoveragePrefetch(
                self.core.marine_manager,
                environment,
                getattr(report, "download_gaps", ()),
            )
            self._routing_prefetch = environment.prefetch
            recovery = getattr(environment, "wind_recovery", None)
            if recovery is not None:
                recovery.prefetch()
        return not self.stop_routing

    def _show_current_preflight(self, report):
        dialog = Gtk.MessageDialog(
            transient_for=self.parent,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text=(
                "Current data needed" if report.blocked else "Check current coverage"
            ),
        )
        dialog.format_secondary_text(
            report.describe(getattr(self, "_auto_currents", False))
        )
        dialog.add_button("Cancel routing", Gtk.ResponseType.CANCEL)
        if not report.blocked:
            dialog.add_button("Run anyway", Gtk.ResponseType.ACCEPT)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        try:
            return dialog.run() == Gtk.ResponseType.ACCEPT and not report.blocked
        finally:
            dialog.destroy()

    def _calculate_single_route(self):  # noqa: C901
        if self.currentRouting is None:
            return

        # Check if GRIB data covers the routing start area and time
        self._prepare_routing_wind()
        pos = self.currentRouting.position
        wind = self.currentRouting.grib.get_wind_at(
            self.currentRouting.time, pos[0], pos[1]
        )
        if wind is None:
            self._show_routing_error(
                "No GRIB data available for the routing area and time. "
                "Please load a GRIB file that covers the routing region "
                "and time period."
            )
            return None

        if not self._check_current_coverage():
            return None
        self._progress_label = "Routing"
        routing_completion(self.currentRouting)
        self._routing_ui(self._set_routing_progress, 0, "Routing: ≈0%")

        res = None

        while (not self.currentRouting.end) and (not self.stop_routing):
            try:
                res = self._step_with_currents()
                logger.debug("Routing step: %s", str(res))

            except DownloadCancelledError:
                self.stop_routing = True
                break

            # This exception is not raised by the algorithm
            except RoutingNoWindError:
                self._show_routing_error(
                    "Trying to create a route without wind information"
                )
                return None

            except RoutingDataGapError as error:
                logger.warning("Routing coverage gap: %s", error)
                self._show_routing_error(str(error))
                return None

            except Exception as e:
                logger.exception("Routing step failed")
                self._show_routing_error("Error: " + str(e))
                return None

            percent = routing_completion(self.currentRouting)
            self._routing_ui(
                self._set_routing_progress, percent, f"Routing: ≈{percent:.0f}%", res
            )

        if self.stop_routing:
            self._routing_ui(self._finish_routing_ui, "Routing cancelled", 0)
            return

        if res is None:
            return

        tr = []
        for wp in res.path:
            tr.append(
                (
                    wp.pos[0],
                    wp.pos[1],
                    (
                        routing_utc(wp.time).isoformat()
                        if self.currentRouting.current_environment is not None
                        else wp.time.strftime("%m/%d/%Y, %H:%M:%S")
                    ),
                    (
                        math.degrees(wp.twd) % 360
                        if self.currentRouting.current_environment is not None
                        else wp.twd
                    ),
                    wp.tws,
                    wp.speed,
                    wp.brg,
                )
            )

        self._routing_ui(
            self._store_routing, tr, res, self._current_provenance(res.path)
        )
        self._routing_ui(self.update_routings)
        self._routing_ui(self._finish_routing_ui, "100%")

    def _step_with_currents(self):
        routing = self.currentRouting
        environment = getattr(routing, "current_environment", None)
        if environment is not None:
            environment.progress = self._report_search_progress

        def report(message):
            self._pending_search_progress = None
            self._routing_ui(self._show_download_progress, message)

        def save(fields):
            self._routing_ui(self._save_routing_fields, fields)

        try:
            return step_with_coverage(
                self.currentRouting,
                self.core.marine_manager,
                getattr(self, "_auto_currents", False),
                report,
                save,
            )
        finally:
            self._pending_search_progress = None
            if environment is not None:
                environment.progress = None

    def _show_download_progress(self, message):
        self.progress_bar.set_text(message)
        self.progress_bar.pulse()

    def _save_routing_fields(self, fields):
        for field in fields:
            self.core.marine_manager.fields.add(field)

    def _report_search_progress(self, distance, checked, total):
        """Coalesce worker updates and deliver widgets on the GTK main thread."""
        now = time.monotonic()
        if now - getattr(self, "_last_search_progress", 0) < 0.15 and checked < total:
            return
        self._last_search_progress = now
        routing = self.currentRouting
        percent = routing_completion(routing, distance)
        label = getattr(self, "_progress_label", "Routing")
        text = f"{label}: ≈{percent:.0f}% · checking paths {checked}/{total}"
        environment = getattr(routing, "current_environment", None)
        prefetch = getattr(environment, "prefetch", None)
        if prefetch is not None and prefetch.pending:
            text += f" · downloading ahead ({prefetch.pending} areas)"
        self._pending_search_progress = (routing, percent, text)
        if not getattr(self, "_search_progress_idle", False):
            self._search_progress_idle = True
            GLib.idle_add(self._apply_search_progress)

    def _apply_search_progress(self):
        self._search_progress_idle = False
        pending = self._pending_search_progress
        if pending is None:
            return False
        routing, percent, text = pending
        if (
            routing is not None
            and not routing.end
            and routing is self.currentRouting
            and not self.stop_routing
        ):
            self.progress_bar.set_fraction(percent / 100)
            self.progress_bar.set_text(text)
            self.progress_bar.show()
        return False

    def _current_provenance(self, path):
        environment = getattr(self.currentRouting, "current_environment", None)
        if environment is None:
            return dict(schema_version=1, routing="wind-only", currents="disabled")
        value = environment.provenance(path)
        value["wind"] = environment.wind.provenance()
        value["wind"]["actual_search_sources"] = value["wind"]["actual_sources"]
        value["wind"]["actual_sources"] = sorted(
            {s["wind_source"] for s in value["samples"] if s.get("wind_source")}
        )
        return value

    def _routing_summary(self, r):
        """Build a short summary string for a routing."""
        points = list(r)
        if len(points) < 2:
            return ""
        total_dist = 0.0
        total_speed = 0.0
        for j in range(1, len(points)):
            total_dist += utils.point_distance(
                points[j - 1][0], points[j - 1][1], points[j][0], points[j][1]
            )
            total_speed += points[j][5] if points[j][5] else 0
        avg_speed = total_speed / (len(points) - 1)
        total_dist_nm = total_dist
        provenance = r.provenance or {}
        source_note = ""
        if provenance.get("routing") == "isochrone-with-currents":
            selected = provenance["currents"]["selected_sources"]
            used = set(provenance["currents"]["actual_sources"])
            models = sorted({s["model"] for s in selected if s["dataset_id"] in used})
            source_note = " | currents: " + ", ".join(models) + " · coverage checked"
        elif provenance.get("routing") == "wind-only":
            source_note = " | wind only"
        try:
            t_start = dateutil.parser.parse(points[0][2])
            t_end = dateutil.parser.parse(points[-1][2])
            hours = (t_end - t_start).total_seconds() / 3600.0
            days = int(hours // 24)
            rem_hours = hours % 24
            dur_str = f"{days}d {rem_hours:.0f}h" if days > 0 else f"{rem_hours:.1f}h"
            return (
                "%.0f nm | %s | avg %.1f kts" % (total_dist_nm, dur_str, avg_speed)
                + source_note
            )
        except Exception:
            return "%.0f nm | avg %.1f kts" % (total_dist_nm, avg_speed) + source_note

    def update_routings(self):
        self.routingListStore.clear()
        self.routingPointsStore.clear()

        for r in self.core.routingManager:
            self.routingListStore.append(
                [
                    r.name,
                    self._routing_summary(r),
                    r.visible,
                    "routing-symbolic",
                ]
            )

        self.map.queue_draw()
        self.core.trackManager.save()

    def _update_routing_points(self, name):
        """Populate the points store for the selected routing."""
        self.routingPointsStore.clear()
        routing = self.core.routingManager.get_by_name(name)
        if routing is None:
            return

        for i, x in enumerate(routing):
            try:
                t = dateutil.parser.parse(x[2])
                if t.tzinfo is not None:
                    t = t.astimezone()
                time_str = t.strftime("%d/%m %H:%M")
            except Exception:
                time_str = x[2]

            self.routingPointsStore.append(
                [
                    i + 1,
                    time_str,
                    "%.0f°" % x[3] if x[3] else "",
                    "%.1f" % x[4] if x[4] else "",
                    "%.1f" % x[5] if x[5] else "",
                    "%.0f°" % x[6] if x[6] else "",
                ]
            )

    def on_routing_toggle(self, widget, i):
        self.core.routingManager[int(i)].visible = not self.core.routingManager[
            int(i)
        ].visible
        self.update_routings()
        self.update_track()

    def on_routing_name_edit(self, widget, i, name):
        self.core.routingManager[int(i)].name = utils.unique_name(
            name, self.core.routingManager
        )
        self.update_routings()

    def on_routing_remove(self, widget):
        self.core.routingManager.remove_by_name(self.selected_routing)
        self.update_routings()
        self.map.queue_draw()

    def on_routing_export(self, widget):
        routing = self.core.routingManager.get_by_name(self.selected_routing)

        if routing is None:
            return

        dialog = Gtk.FileChooserDialog(
            "Please select a destination",
            self.parent,
            Gtk.FileChooserAction.SAVE,
            (
                Gtk.STOCK_CANCEL,
                Gtk.ResponseType.CANCEL,
                Gtk.STOCK_SAVE,
                Gtk.ResponseType.OK,
            ),
        )

        filter_gpx = Gtk.FileFilter()
        filter_gpx.set_name("GPX track")
        filter_gpx.add_mime_type("application/gpx+xml")
        filter_gpx.add_pattern("*.gpx")
        dialog.add_filter(filter_gpx)

        response = dialog.run()

        if response == Gtk.ResponseType.OK:
            filepath = dialog.get_filename()

            if not filepath.endswith(".gpx"):
                filepath += ".gpx"

            if routing.export(filepath):
                self.status_bar.push(
                    self.status_bar.get_context_id("Info"),
                    f"Saved {len(routing)} waypoints",
                )
                edialog = Gtk.MessageDialog(
                    self.parent, 0, Gtk.MessageType.INFO, Gtk.ButtonsType.OK, "Saved"
                )
                edialog.format_secondary_text(f"Saved {len(routing)} waypoints")
                edialog.run()
                edialog.destroy()
            else:
                edialog = Gtk.MessageDialog(
                    self.parent, 0, Gtk.MessageType.ERROR, Gtk.ButtonsType.OK, "Error"
                )
                edialog.format_secondary_text(f"Cannot save file: {filepath}")
                edialog.run()
                edialog.destroy()

        dialog.destroy()

    def on_routing_stop(self, widget):
        self.stop_routing = True
        prefetch = getattr(self, "_routing_prefetch", None)
        if prefetch is not None:
            prefetch.close()
        wind_recovery = getattr(self, "_wind_recovery", None)
        if wind_recovery is not None:
            wind_recovery.close()
        environment = getattr(self.currentRouting, "current_environment", None)
        if environment is not None:
            environment.cancel.set()

    def on_select_routing(self, selection):
        store, pathlist = selection.get_selected_rows()
        for path in pathlist:
            tree_iter = store.get_iter(path)
            name = store.get_value(tree_iter, 0)
            self.selected_routing = name
            self.geo_map_layer.highlight_routing(name)
            self._update_routing_points(name)
            self.map.queue_draw()

    def on_routing_click(self, item, event):
        if (
            self.selected_routing is not None
            and event.button == 3
            and len(self.core.routingManager) > 0
        ):
            menu = self.builder.get_object("routing-item-menu")
            menu.popup(None, None, None, None, event.button, event.time)
