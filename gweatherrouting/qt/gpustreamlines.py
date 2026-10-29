"""Experimental Metal compute tracer embedded in the Qt Quick scene graph."""

from dataclasses import dataclass
from hashlib import blake2b
from pathlib import Path
import os
import struct
from threading import Lock
from time import monotonic

import numpy as np
from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QRhi,
    QRhiBuffer,
    QRhiCommandBuffer,
    QRhiGraphicsPipeline,
    QRhiShaderResourceBinding,
    QRhiShaderStage,
    QRhiVertexInputLayout,
    QShader,
)
from PySide6.QtQuick import QQuickItem, QSGRenderNode, QSGRendererInterface

from gweatherrouting.rendering.streamlines import (
    WindGrid,
    density_parameters,
    spatially_thin_seeds,
)

MAX_STEPS = 160
SEGMENTS_PER_SEED = MAX_STEPS * 2
MAX_FILTER_CELLS = 65536
MAX_FILTER_SEEDS = 500
SHADER_DIR = Path(__file__).with_name("shaders")


@dataclass(frozen=True)
class GpuTraceInput:
    wind: np.ndarray
    x_coordinates: np.ndarray
    y_coordinates: np.ndarray
    valid_x: np.ndarray
    valid_y: np.ndarray
    seeds: np.ndarray
    width: int
    height: int
    opacity: float = 1.0


def make_gpu_trace_input(view, columns, density):
    """Pack authoritative sampled wind and stable CPU-selected seeds for Metal."""
    grid = WindGrid(view, columns)
    seeds = jittered_seed_positions(view, density)
    if not seeds:
        return None
    arrays = (
        np.stack((grid.u, grid.v), axis=-1).astype(np.float32).reshape(-1, 2),
        np.asarray(grid.xs, dtype=np.float32),
        np.asarray(grid.ys, dtype=np.float32),
        np.asarray(grid.valid_x, dtype=np.uint32),
        np.asarray(grid.valid_y, dtype=np.uint32),
        np.asarray(seeds, dtype=np.float32).reshape((-1, 2)),
    )
    for array in arrays:
        array.setflags(write=False)
    return GpuTraceInput(
        *arrays,
        width=view.width,
        height=view.height,
    )


def jittered_seed_positions(view, density):
    """Bound a stable world-cell seed grid and jitter its interior points.

    the reference application's stored jitter table was not available in the supplied artifacts,
    so this uses a fixed per-cell hash rather than claiming identical offsets.
    """
    spacing, seed_limit, _segment_limit = density_parameters(density)
    cx = view._lon_to_x(view._center_lon)
    cy = view._lat_to_y(view._center_lat)
    left, top = cx - view.width / 2, cy - view.height / 2
    right, bottom = cx + view.width / 2, cy + view.height / 2
    ix0, ix1 = int(np.floor(left / spacing)), int(np.floor(right / spacing))
    iy0, iy1 = int(np.floor(top / spacing)), int(np.floor(bottom / spacing))
    zoom = round(view._zoom, 6)
    candidates = []
    for iy in range(iy0, iy1 + 1):
        for ix in range(ix0, ix1 + 1):
            digest = blake2b(
                f"{zoom}:{iy}:{ix}".encode("ascii"), digest_size=8
            ).digest()
            dx = int.from_bytes(digest[:4], "little") / 0xFFFFFFFF - 0.5
            dy = int.from_bytes(digest[4:], "little") / 0xFFFFFFFF - 0.5
            x = (ix + 0.5 + 0.6 * dx) * spacing - left
            y = (iy + 0.5 + 0.6 * dy) * spacing - top
            if 0 <= x < view.width and 0 <= y < view.height:
                candidates.append((ix, iy, x, y))
    capped = len(candidates) > seed_limit
    if capped:
        keep = np.linspace(0, len(candidates) - 1, seed_limit, dtype=np.int64)
        candidates = [candidates[index] for index in keep]
    candidates = spatially_thin_seeds(
        candidates, lambda item: (item[0], item[1]), capped=capped
    )
    candidates.sort(
        key=lambda point: (point[2] - view.width / 2) ** 2
        + (point[3] - view.height / 2) ** 2
    )
    return [(x, y) for _ix, _iy, x, y in candidates]


def _shader(name):
    try:
        return QShader.fromSerialized((SHADER_DIR / name).read_bytes())
    except OSError as exc:
        raise RuntimeError(f"GPU streamline shader unavailable: {name}") from exc


class _TraceNode(QSGRenderNode):
    def __init__(self, state):
        super().__init__()
        self.state = state
        self.pipeline = None
        self.bindings = None
        self.generation = -1
        self.resources = []

    def rect(self):
        width, height = self.state["size"]
        return QRectF(0, 0, width, height)

    def flags(self):
        return QSGRenderNode.RenderingFlag.BoundedRectRendering

    def changedStates(self):
        return (
            QSGRenderNode.StateFlag.BlendState
            | QSGRenderNode.StateFlag.ColorState
            | QSGRenderNode.StateFlag.CullState
            | QSGRenderNode.StateFlag.ScissorState
            | QSGRenderNode.StateFlag.ViewportState
        )

    def release_pipeline(self):
        while self.resources:
            self.resources.pop().destroy()
        self.pipeline = self.bindings = None
        self.generation = -1

    def _create_pipeline(self):
        rhi = self.commandBuffer().rhi()
        bindings = rhi.newShaderResourceBindings()
        bindings.setBindings(
            [
                QRhiShaderResourceBinding.bufferLoad(
                    0,
                    QRhiShaderResourceBinding.StageFlag.VertexStage,
                    self.state["output"],
                ),
                QRhiShaderResourceBinding.uniformBuffer(
                    1,
                    QRhiShaderResourceBinding.StageFlag.VertexStage,
                    self.state["render_params"],
                ),
                QRhiShaderResourceBinding.bufferLoad(
                    2,
                    QRhiShaderResourceBinding.StageFlag.VertexStage,
                    self.state["previous_output"] or self.state["output"],
                ),
                QRhiShaderResourceBinding.bufferLoad(
                    3,
                    QRhiShaderResourceBinding.StageFlag.VertexStage,
                    self.state["visibility"],
                ),
                QRhiShaderResourceBinding.bufferLoad(
                    4,
                    QRhiShaderResourceBinding.StageFlag.VertexStage,
                    self.state["previous_visibility"] or self.state["visibility"],
                ),
            ]
        )
        if not bindings.create():
            raise RuntimeError("GPU streamline vertex bindings creation failed")
        pipeline = rhi.newGraphicsPipeline()
        pipeline.setShaderStages(
            [
                QRhiShaderStage(QRhiShaderStage.Type.Vertex, self.state["vertex"]),
                QRhiShaderStage(QRhiShaderStage.Type.Fragment, self.state["fragment"]),
            ]
        )
        pipeline.setTopology(QRhiGraphicsPipeline.Topology.Lines)
        pipeline.setVertexInputLayout(QRhiVertexInputLayout())
        pipeline.setShaderResourceBindings(bindings)
        blend = QRhiGraphicsPipeline.TargetBlend()
        blend.enable = True
        blend.srcColor = QRhiGraphicsPipeline.BlendFactor.One
        blend.dstColor = QRhiGraphicsPipeline.BlendFactor.OneMinusSrcAlpha
        blend.srcAlpha = QRhiGraphicsPipeline.BlendFactor.One
        blend.dstAlpha = QRhiGraphicsPipeline.BlendFactor.OneMinusSrcAlpha
        pipeline.setTargetBlends([blend])
        pipeline.setRenderPassDescriptor(self.renderTarget().renderPassDescriptor())
        if not pipeline.create():
            bindings.destroy()
            raise RuntimeError("GPU streamline graphics pipeline creation failed")
        self.bindings, self.pipeline = bindings, pipeline
        self.resources.extend((bindings, pipeline))
        self.generation = self.state["generation"]

    def render(self, _render_state):
        try:
            if self.state["output"] is None:
                return
            if self.generation != self.state["generation"]:
                self.release_pipeline()
                self._create_pipeline()
            command_buffer = self.commandBuffer()
            command_buffer.setGraphicsPipeline(self.pipeline)
            command_buffer.setShaderResources(self.bindings)
            command_buffer.draw(self.state["seed_count"] * SEGMENTS_PER_SEED * 6)
        except Exception as exc:
            self.state["fail"](str(exc))

    def releaseResources(self):
        self.release_pipeline()


class GpuStreamlineItem(QQuickItem):
    """Render-node item; GPU resources are created and used on Qt's render thread."""

    gpuError = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFlag(QQuickItem.Flag.ItemHasContents, True)
        self._input_lock = Lock()
        self._input = None
        self._input_generation = 0
        self._phase = 0.0
        self._flow_started = monotonic()
        self._opacity = 1.0
        self._view_transform = (1.0, 0.0, 0.0, 0.0)
        self._transition_requested = False
        self._displayed_input = None
        self._transition_started = None
        self._last_dispatched = -1
        self._failed = False
        self._node = None
        self._window = None
        self._animation_timer = QTimer(self)
        self._animation_timer.setInterval(16)
        self._animation_timer.timeout.connect(self._advance_flow)
        self.visibleChanged.connect(self._sync_animation)
        self._buffers = []
        self._pipelines = []
        self._compute_bindings = None
        self._compute_pipeline = None
        self._filter_bindings = None
        self._filter_pipeline = None
        self._state = {
            "output": None,
            "previous_output": None,
            "visibility": None,
            "previous_visibility": None,
            "render_params": None,
            "generation": 0,
            "seed_count": 0,
            "size": (0, 0),
            "vertex": _shader("streamline_trace.vert.qsb"),
            "fragment": _shader("streamline_trace.frag.qsb"),
            "fail": self._fail,
        }
        self.windowChanged.connect(self._window_changed)

    def set_trace_input(self, trace_input, *, transition=False):
        with self._input_lock:
            if trace_input is None and self._input is None:
                return
            self._input = trace_input
            self._input_generation += 1
            self._view_transform = (1.0, 0.0, 0.0, 0.0)
            self._transition_requested = bool(transition)
            self._opacity = trace_input.opacity if trace_input is not None else 1.0
            self._state["size"] = (
                (trace_input.width, trace_input.height)
                if trace_input is not None
                else (0, 0)
            )
        self._failed = False
        self.setVisible(trace_input is not None)
        if trace_input is not None:
            self.setWidth(trace_input.width)
            self.setHeight(trace_input.height)
        self._sync_animation()
        self.update()

    def set_view_transform(self, width, height, scale, offset_x, offset_y, world_width):
        """Project existing source-camera traces into the current viewport."""
        with self._input_lock:
            self._state["size"] = (float(width), float(height))
            self._view_transform = (
                float(scale),
                float(offset_x),
                float(offset_y),
                float(world_width),
            )
        self.setWidth(width)
        self.setHeight(height)
        self.update()

    def set_opacity(self, opacity):
        with self._input_lock:
            if self._input is None or self._opacity == float(opacity):
                return
            self._opacity = float(opacity)
        self.update()

    def _sync_animation(self):
        if self.isVisible() and self._input is not None:
            self._animation_timer.start()
        else:
            self._animation_timer.stop()

    def _advance_flow(self):
        with self._input_lock:
            self._phase = ((monotonic() - self._flow_started) * 6.6) % 20.0
        self.update()

    def updatePaintNode(self, old_node, _data):
        if self._node is None:
            self._node = old_node or _TraceNode(self._state)
        return self._node

    def _window_changed(self, window):
        self._disconnect_window()
        self._window = window
        if window is not None:
            window.beforeRendering.connect(
                self._before_rendering, Qt.ConnectionType.DirectConnection
            )
            window.sceneGraphInvalidated.connect(
                self._scene_graph_invalidated, Qt.ConnectionType.DirectConnection
            )

    def attach_window(self, window):
        """Bind explicitly to the owning QQuickWidget window after parenting."""
        self._window_changed(window)

    def _fail(self, message):
        if not self._failed:
            self._failed = True
            self.gpuError.emit(message)

    def _create_buffer(
        self, rhi, data, usage=QRhiBuffer.UsageFlag.StorageBuffer, *, dynamic=False
    ):
        size = max(data if isinstance(data, int) else len(data), 4)
        if usage == QRhiBuffer.UsageFlag.UniformBuffer:
            size = rhi.ubufAligned(size)
        buffer_type = QRhiBuffer.Type.Dynamic if dynamic else QRhiBuffer.Type.Static
        buffer = rhi.newBuffer(buffer_type, usage, size)
        if not buffer.create():
            raise RuntimeError("GPU streamline buffer allocation failed")
        self._buffers.append(buffer)
        return buffer

    def _reset_resources(self):
        for resource in reversed(self._pipelines):
            resource.destroy()
        self._pipelines.clear()
        for buffer in reversed(self._buffers):
            buffer.destroy()
        self._buffers.clear()
        self._compute_bindings = None
        self._compute_pipeline = None
        self._filter_bindings = None
        self._filter_pipeline = None
        self._state["output"] = self._state["render_params"] = None
        self._state["previous_output"] = None
        self._state["visibility"] = self._state["previous_visibility"] = None
        self._transition_started = None
        self._displayed_input = None

    def _before_rendering(self):
        try:
            if self._failed:
                return
            with self._input_lock:
                trace_input = self._input
                generation = self._input_generation
                requested_transition = self._transition_requested
            if trace_input is None:
                if generation != self._last_dispatched:
                    if self._node is not None:
                        self._node.release_pipeline()
                    self._reset_resources()
                    self._last_dispatched = generation
                return
            window = self._window
            if window is None:
                return
            rhi = window.rhi()
            if rhi is None or rhi.backend() != QRhi.Implementation.Metal:
                raise RuntimeError("GPU streamlines require the Metal renderer")
            if not rhi.isFeatureSupported(QRhi.Feature.Compute):
                raise RuntimeError("GPU compute is unavailable")
            interface = window.rendererInterface()
            pointer = int(
                interface.getResource(
                    window, QSGRendererInterface.Resource.RhiRedirectCommandBuffer
                )
            )
            if not pointer:
                pointer = int(
                    interface.getResource(
                        window, QSGRendererInterface.Resource.CommandListResource
                    )
                )
            if not pointer:
                raise RuntimeError("Qt Quick command buffer is unavailable")
            command_buffer = __import__("shiboken6").wrapInstance(
                pointer, QRhiCommandBuffer
            )
            if command_buffer.rhi().backend() != QRhi.Implementation.Metal:
                raise RuntimeError("Qt Quick command buffer is not Metal-backed")
            if self._transition_started is not None:
                elapsed = monotonic() - self._transition_started
                if elapsed >= 0.2:
                    if self._node is not None:
                        self._node.release_pipeline()
                    previous = self._state["previous_output"]
                    previous_visibility = self._state["previous_visibility"]
                    self._state["previous_output"] = None
                    self._state["previous_visibility"] = None
                    if previous is not None:
                        self._buffers.remove(previous)
                        previous.destroy()
                    if previous_visibility is not None:
                        self._buffers.remove(previous_visibility)
                        previous_visibility.destroy()
                    self._transition_started = None
                elif (
                    trace_input is not None
                    and requested_transition
                    and generation != self._last_dispatched
                ):
                    # Finish the displayed transition before reusing either output.
                    # The latest input replaces all intermediate scrub requests.
                    return
            if generation == self._last_dispatched:
                if self._state["render_params"] is not None:
                    with self._input_lock:
                        scale, offset_x, offset_y, world_width = self._view_transform
                        parameters = struct.pack(
                            "<ffffffffff",
                            *self._state["size"],
                            self._opacity,
                            1.2,
                            self._phase,
                            (
                                min(1.0, (monotonic() - self._transition_started) / 0.2)
                                if self._transition_started is not None
                                else 1.0
                            ),
                            scale,
                            offset_x,
                            offset_y,
                            world_width,
                        )
                    updates = rhi.nextResourceUpdateBatch()
                    updates.updateDynamicBuffer(
                        self._state["render_params"], 0, len(parameters), parameters
                    )
                    window.beginExternalCommands()
                    command_buffer.resourceUpdate(updates)
                    window.endExternalCommands()
                return

            previous_output = self._state["output"]
            previous_visibility = self._state["visibility"]
            previous_input = self._displayed_input
            compatible = (
                requested_transition
                and previous_output is not None
                and previous_input is not None
                and trace_input.width == previous_input.width
                and trace_input.height == previous_input.height
                and np.array_equal(trace_input.seeds, previous_input.seeds)
            )
            if compatible:
                self._buffers.remove(previous_output)
                self._buffers.remove(previous_visibility)
            if self._node is not None:
                self._node.release_pipeline()
            self._reset_resources()
            if compatible:
                self._buffers.append(previous_output)
                self._buffers.append(previous_visibility)
                self._state["previous_output"] = previous_output
                self._state["previous_visibility"] = previous_visibility
            grid_inputs = (
                trace_input.wind,
                trace_input.x_coordinates,
                trace_input.y_coordinates,
                trace_input.valid_x,
                trace_input.valid_y,
                trace_input.seeds,
            )
            input_buffers = [
                self._create_buffer(rhi, array.tobytes()) for array in grid_inputs
            ]
            seed_count = len(trace_input.seeds)
            if not seed_count:
                self._last_dispatched = generation
                return
            output = self._create_buffer(rhi, seed_count * SEGMENTS_PER_SEED * 32)
            visibility = self._create_buffer(rhi, seed_count * 4)
            self._state["visibility"] = visibility
            parameter_bytes = struct.pack(
                "<ffII",
                trace_input.width,
                trace_input.height,
                seed_count,
                0,
            )
            with self._input_lock:
                scale, offset_x, offset_y, world_width = self._view_transform
                render_parameter_bytes = struct.pack(
                    "<ffffffffff",
                    *self._state["size"],
                    self._opacity,
                    1.2,
                    self._phase,
                    0.0 if compatible else 1.0,
                    scale,
                    offset_x,
                    offset_y,
                    world_width,
                )
            params = self._create_buffer(
                rhi, parameter_bytes, QRhiBuffer.UsageFlag.UniformBuffer
            )
            render_params = self._create_buffer(
                rhi,
                render_parameter_bytes,
                QRhiBuffer.UsageFlag.UniformBuffer,
                dynamic=True,
            )
            self._state["output"] = output
            self._state["render_params"] = render_params
            self._state["seed_count"] = seed_count

            self._compute_bindings = rhi.newShaderResourceBindings()
            self._compute_bindings.setBindings(
                [
                    *[
                        QRhiShaderResourceBinding.bufferLoad(
                            binding,
                            QRhiShaderResourceBinding.StageFlag.ComputeStage,
                            buffer,
                        )
                        for binding, buffer in enumerate(input_buffers)
                    ],
                    QRhiShaderResourceBinding.bufferStore(
                        6,
                        QRhiShaderResourceBinding.StageFlag.ComputeStage,
                        output,
                    ),
                    QRhiShaderResourceBinding.uniformBuffer(
                        7,
                        QRhiShaderResourceBinding.StageFlag.ComputeStage,
                        params,
                    ),
                ]
            )
            if not self._compute_bindings.create():
                raise RuntimeError("GPU streamline compute bindings failed")
            self._compute_pipeline = rhi.newComputePipeline()
            self._compute_pipeline.setShaderStage(
                QRhiShaderStage(
                    QRhiShaderStage.Type.Compute,
                    _shader("streamline_trace.comp.qsb"),
                )
            )
            self._compute_pipeline.setShaderResourceBindings(self._compute_bindings)
            if not self._compute_pipeline.create():
                raise RuntimeError("GPU streamline compute pipeline failed")

            self._pipelines.extend((self._compute_bindings, self._compute_pipeline))

            cells_x = max(1, (trace_input.width + 11) // 12)
            cells_y = max(1, (trace_input.height + 11) // 12)
            dedup = (
                os.environ.get("GWR_GPU_STREAMLINE_DEDUP", "1") != "0"
                and cells_x * cells_y <= MAX_FILTER_CELLS
                and seed_count <= MAX_FILTER_SEEDS
            )
            if dedup:
                occupancy = self._create_buffer(rhi, cells_x * cells_y * 4)
                filter_params_data = struct.pack(
                    "<IIIIff",
                    seed_count,
                    cells_x,
                    cells_y,
                    0,
                    float(trace_input.width),
                    float(trace_input.height),
                )
                filter_params = self._create_buffer(
                    rhi, filter_params_data, QRhiBuffer.UsageFlag.UniformBuffer
                )
                self._filter_bindings = rhi.newShaderResourceBindings()
                self._filter_bindings.setBindings(
                    [
                        QRhiShaderResourceBinding.bufferLoad(
                            0, QRhiShaderResourceBinding.StageFlag.ComputeStage, output
                        ),
                        QRhiShaderResourceBinding.bufferLoadStore(
                            1,
                            QRhiShaderResourceBinding.StageFlag.ComputeStage,
                            occupancy,
                        ),
                        QRhiShaderResourceBinding.bufferStore(
                            2,
                            QRhiShaderResourceBinding.StageFlag.ComputeStage,
                            visibility,
                        ),
                        QRhiShaderResourceBinding.uniformBuffer(
                            3,
                            QRhiShaderResourceBinding.StageFlag.ComputeStage,
                            filter_params,
                        ),
                    ]
                )
                if not self._filter_bindings.create():
                    raise RuntimeError("GPU streamline filter bindings failed")
                self._filter_pipeline = rhi.newComputePipeline()
                self._filter_pipeline.setShaderStage(
                    QRhiShaderStage(
                        QRhiShaderStage.Type.Compute,
                        _shader("streamline_filter.comp.qsb"),
                    )
                )
                self._filter_pipeline.setShaderResourceBindings(self._filter_bindings)
                if not self._filter_pipeline.create():
                    raise RuntimeError("GPU streamline filter pipeline failed")
                self._pipelines.extend((self._filter_bindings, self._filter_pipeline))
            else:
                # Bind all-visible data when the optional filter is disabled.
                all_visible_payload = struct.pack(
                    f"<{seed_count}I", *([1] * seed_count)
                )

            updates = rhi.nextResourceUpdateBatch()
            upload_payloads = [array.tobytes() for array in grid_inputs]
            for buffer, payload in zip(input_buffers, upload_payloads):
                updates.uploadStaticBuffer(buffer, payload)
            updates.uploadStaticBuffer(params, parameter_bytes)
            if dedup:
                updates.uploadStaticBuffer(filter_params, filter_params_data)
            if not dedup:
                updates.uploadStaticBuffer(visibility, all_visible_payload)
            updates.updateDynamicBuffer(
                render_params, 0, len(render_parameter_bytes), render_parameter_bytes
            )
            window.beginExternalCommands()
            command_buffer.beginComputePass(updates)
            # PySide passes these through a void-pointer bridge; keep payloads
            # alive until QRhi has consumed the update batch.
            command_buffer.setComputePipeline(self._compute_pipeline)
            command_buffer.setShaderResources(self._compute_bindings)
            command_buffer.dispatch((seed_count + 63) // 64, 1, 1)
            command_buffer.endComputePass()
            if dedup:
                command_buffer.beginComputePass()
                command_buffer.setComputePipeline(self._filter_pipeline)
                command_buffer.setShaderResources(self._filter_bindings)
                command_buffer.dispatch(1, 1, 1)
                command_buffer.endComputePass()
            window.endExternalCommands()
            self._state["generation"] = generation
            self._last_dispatched = generation
            self._displayed_input = trace_input
            if compatible:
                self._transition_started = monotonic()
        except Exception as exc:
            self._fail(str(exc))

    def _scene_graph_invalidated(self):
        self._reset_resources()
        self._last_dispatched = -1

    def _disconnect_window(self):
        if self._window is None:
            return
        try:
            self._window.beforeRendering.disconnect(self._before_rendering)
            self._window.sceneGraphInvalidated.disconnect(self._scene_graph_invalidated)
        except (RuntimeError, TypeError):
            pass
        self._window = None

    def releaseResources(self):
        self._disconnect_window()
        self._reset_resources()
