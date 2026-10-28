"""Experimental Metal compute tracer embedded in the Qt Quick scene graph."""

from dataclasses import dataclass
from pathlib import Path
import struct
from threading import Lock

import numpy as np
from PySide6.QtCore import QRectF, Qt, Signal
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

from gweatherrouting.rendering.streamlinepaths import _seeds
from gweatherrouting.rendering.streamlines import WindGrid

MAX_STEPS = 160
SEGMENTS_PER_SEED = MAX_STEPS * 2
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
    seed_data = _seeds(view, density)
    seeds = [(x, y) for _seed_id, x, y in seed_data]
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
        self._last_dispatched = -1
        self._failed = False
        self._node = None
        self._window = None
        self._buffers = []
        self._pipelines = []
        self._compute_bindings = None
        self._compute_pipeline = None
        self._state = {
            "output": None,
            "render_params": None,
            "generation": 0,
            "seed_count": 0,
            "size": (0, 0),
            "vertex": _shader("streamline_trace.vert.qsb"),
            "fragment": _shader("streamline_trace.frag.qsb"),
            "fail": self._fail,
        }
        self.windowChanged.connect(self._window_changed)

    def set_trace_input(self, trace_input):
        with self._input_lock:
            if trace_input is None and self._input is None:
                return
            self._input = trace_input
            self._input_generation += 1
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
        self.update()

    def set_opacity(self, opacity):
        with self._input_lock:
            if self._input is None or self._input.opacity == float(opacity):
                return
            self._input = GpuTraceInput(
                self._input.wind,
                self._input.x_coordinates,
                self._input.y_coordinates,
                self._input.valid_x,
                self._input.valid_y,
                self._input.seeds,
                self._input.width,
                self._input.height,
                float(opacity),
            )
            self._input_generation += 1
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

    def _create_buffer(self, rhi, data, usage=QRhiBuffer.UsageFlag.StorageBuffer):
        size = max(len(data), 4)
        if usage == QRhiBuffer.UsageFlag.UniformBuffer:
            size = rhi.ubufAligned(size)
        buffer = rhi.newBuffer(QRhiBuffer.Type.Static, usage, size)
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
        self._state["output"] = self._state["render_params"] = None

    def _before_rendering(self):
        try:
            if self._failed:
                return
            with self._input_lock:
                trace_input = self._input
                generation = self._input_generation
            if trace_input is None:
                if generation != self._last_dispatched:
                    if self._node is not None:
                        self._node.release_pipeline()
                    self._reset_resources()
                    self._last_dispatched = generation
                return
            if generation == self._last_dispatched:
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

            if self._node is not None:
                self._node.release_pipeline()
            self._reset_resources()
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
            output = self._create_buffer(
                rhi, bytes(seed_count * SEGMENTS_PER_SEED * 32)
            )
            parameter_bytes = struct.pack(
                "<ffII",
                trace_input.width,
                trace_input.height,
                seed_count,
                0,
            )
            render_parameter_bytes = struct.pack(
                "<ffff", trace_input.width, trace_input.height, trace_input.opacity, 1.2
            )
            params = self._create_buffer(
                rhi, parameter_bytes, QRhiBuffer.UsageFlag.UniformBuffer
            )
            render_params = self._create_buffer(
                rhi, render_parameter_bytes, QRhiBuffer.UsageFlag.UniformBuffer
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

            updates = rhi.nextResourceUpdateBatch()
            upload_payloads = [array.tobytes() for array in grid_inputs]
            upload_payloads.append(bytes(seed_count * SEGMENTS_PER_SEED * 32))
            for buffer, payload in zip((*input_buffers, output), upload_payloads):
                updates.uploadStaticBuffer(buffer, payload)
            updates.uploadStaticBuffer(params, parameter_bytes)
            updates.uploadStaticBuffer(render_params, render_parameter_bytes)
            window.beginExternalCommands()
            command_buffer.beginComputePass(updates)
            # PySide passes these through a void-pointer bridge; keep payloads
            # alive until QRhi has consumed the update batch.
            command_buffer.setComputePipeline(self._compute_pipeline)
            command_buffer.setShaderResources(self._compute_bindings)
            command_buffer.dispatch((seed_count + 63) // 64, 1, 1)
            command_buffer.endComputePass()
            window.endExternalCommands()
            self._state["generation"] = generation
            self._last_dispatched = generation
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
