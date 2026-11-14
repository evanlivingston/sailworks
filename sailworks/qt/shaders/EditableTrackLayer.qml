import QtQuick
import QtQuick.Shapes

// Retained drawing of the one track the viewport is currently editing.
//
// The route Canvas still owns every other track, route, AIS target and legend.
// This layer repeats only the selected editing track so its polyline, labels,
// numbered marks and handle circles survive a single Qt Quick Shapes path
// instead of a Canvas repaint.  It is a sibling of ``routeOverlayCanvas``
// inside ``mapContent`` and is given the same x/y and size, so its local
// ``screenPoint`` already carries ``panPadding`` like the Canvas does.  Do not
// subtract the padding again: both items project into identical local pixels.
//
// The layer installs no pointer handlers on purpose: the viewport's existing
// picking and drag handling stay the only input owners, and this item never
// consumes an event.
Item {
    // Do not name this id ``layer``: every Item already has a ``layer``
    // property (QQuickItemLayer), so an unqualified ``layer.markColor`` or
    // ``layer.handleRadius`` inside a delegate would resolve to that property
    // instead of this component, yielding undefined at runtime.
    id: trackLayer
    objectName: "editableTrackLayer"

    // Published by the scene from ``routeOverlayCanvas``.
    property var tracks: []
    property var movePreview: []
    property real centerLatitude: 0
    property real centerLongitude: 0
    property real zoom: 1
    property real panPadding: 0
    property real viewportWidth: 0
    property real viewportHeight: 0

    // First track that is actually being edited and can draw a mark.  The
    // Canvas skips exactly this index, so the selected track is drawn once.
    // A single-point track has no polyline but its one mark stays pickable.
    readonly property int selectedIndex: {
        if (!tracks || !tracks.length)
            return -1
        for (var t = 0; t < tracks.length; ++t) {
            var candidate = tracks[t]
            if (candidate && candidate.editing && candidate.points
                    && candidate.points.length >= 1)
                return t
        }
        return -1
    }
    readonly property var track:
        selectedIndex >= 0 && tracks ? tracks[selectedIndex] : null
    readonly property var points: track ? track.points : []

    // ``movePreview`` is [trackIndex, pointIndex, latitude, longitude].  Only
    // this track's preview applies; index 0 replaces the first vertex too.
    readonly property var preview: (movePreview && movePreview.length === 4
                                    && movePreview[0] === selectedIndex)
                                   ? movePreview : null
    readonly property int previewIndex: preview ? preview[1] : -1

    readonly property bool trackActive: track ? Boolean(track.active) : false
    readonly property int highlightIndex:
        (track && typeof track.highlight === "number") ? track.highlight : -1
    readonly property bool trackEditing: track ? Boolean(track.editing) : false
    readonly property int hoverIndex:
        (track && typeof track.hover === "number") ? track.hover : -1

    // Mirrors the Canvas track styles.  ShapePath dash lengths are expressed in
    // stroke widths, so the inactive 0.8 width is given [10, 5] to keep the
    // same 8 px / 4 px rhythm the Canvas painted at any width.
    readonly property color strokeColor: trackActive
        ? "#91321c"
        : Qt.rgba(145 / 255, 74 / 255, 124 / 255, 0.6)
    readonly property real strokeWidth: trackActive ? 1 : 0.8
    readonly property var dashPattern: [8 / strokeWidth, 4 / strokeWidth]

    // Per-point mark roundings published beside ``points`` so each handle can
    // show port red, starboard green and sail-to/gate blue.  Values are the
    // core ``ROUNDING_*`` constants; null/undefined stays neutral.
    readonly property var roundings:
        (track && track.roundings) ? track.roundings : []

    // Edit-indicator geometry.  The base handle stays drawn (it is the mark);
    // the wider halo is the transient hover/drag affordance that fades in and
    // out, mirroring the QML editing probe's handle circle.
    readonly property real handleRadius: 6
    readonly property real handleRadiusActive: 8
    readonly property real haloRadius: 19
    readonly property color markPortColor: "#d43a1e"
    readonly property color markStarboardColor: "#258054"
    readonly property color markNeutralColor: "#2f6fbd"

    function markColor(rounding) {
        if (rounding === 0)
            return markPortColor      // ROUNDING_PORT
        if (rounding === 1)
            return markStarboardColor // ROUNDING_STARBOARD
        return markNeutralColor       // ROUNDING_SAIL_TO / ROUNDING_GATE / unset
    }

    readonly property bool trackVisible: Boolean(selectedIndex >= 0
                                                && projected && projected.length >= 1
                                                && projected.every(function (point) {
                                                    return isFinite(point.x) && isFinite(point.y)
                                                }))
    readonly property bool lineVisible: Boolean(trackVisible
                                                && projected.length >= 2)
    readonly property var firstPoint:
        projected.length > 0 ? projected[0] : Qt.point(0, 0)

    function worldY(latitude, world) {
        var radians = Math.max(-85.05112878,
                               Math.min(85.05112878, latitude)) * Math.PI / 180
        return world * (1 - Math.asinh(Math.tan(radians)) / Math.PI) / 2
    }

    // Rebuilt whenever the camera, the points or the preview change.  The
    // camera and padding properties are read in this binding itself so the
    // dependency is explicit rather than implied by the helper above.
    readonly property var projected: {
        var world = 256 * Math.pow(2, zoom)
        var originX = panPadding + viewportWidth / 2
        var originY = panPadding + viewportHeight / 2
                       - worldY(centerLatitude, world)
        var rows = []
        for (var i = 0; i < points.length; ++i) {
            var geo = points[i]
            if (i === previewIndex) {
                var lat = Number(preview[2])
                var lon = Number(preview[3])
                if (isFinite(lat) && isFinite(lon))
                    geo = [lat, lon]
            }
            var dx = (geo[1] - centerLongitude) * world / 360
            dx -= Math.round(dx / world) * world
            rows.push(Qt.point(originX + dx,
                               originY + worldY(geo[0], world)))
        }
        return rows
    }
    ListModel { id: markModel }

    // The non-preview inputs to ``projected``.  Between pointer moves only the
    // preview changes, and then only one mark row can move, so the full list
    // walk is skipped unless one of these identities changed.
    property var markState: null
    property int syncedPreviewIndex: -1
    // The track the mark model currently describes.  The fast path below only
    // rewrites the dragged row, which is valid while this identity and the row
    // count both still match the projection.
    property var syncedTrack: null

    function markStateValues() {
        return [trackVisible, zoom, centerLatitude, centerLongitude,
                viewportWidth, viewportHeight, panPadding, points, roundings]
    }

    function sameMarkState(state) {
        var previous = markState
        if (!previous || previous.length !== state.length)
            return false
        for (var i = 0; i < state.length; ++i) {
            if (previous[i] !== state[i])
                return false
        }
        return true
    }

    function markRounding(index) {
        if (!roundings || index < 0 || index >= roundings.length)
            return -1
        var value = roundings[index]
        // The payload may carry null for an unset rounding; -1 stays neutral.
        return typeof value === "number" ? value : -1
    }

    function writeMark(index, point) {
        var selected = index === highlightIndex
        var hovered = index === hoverIndex
        var rounding = markRounding(index)
        if (index >= markModel.count) {
            markModel.append({ "px": point.x, "py": point.y,
                               "highlighted": selected, "hovered": hovered,
                               "rounding": rounding })
            return
        }
        var current = markModel.get(index)
        if (current.px !== point.x)
            markModel.setProperty(index, "px", point.x)
        if (current.py !== point.y)
            markModel.setProperty(index, "py", point.y)
        if (current.highlighted !== selected)
            markModel.setProperty(index, "highlighted", selected)
        if (current.hovered !== hovered)
            markModel.setProperty(index, "hovered", hovered)
        if (current.rounding !== rounding)
            markModel.setProperty(index, "rounding", rounding)
    }

    // Only the dragged row moves between pointer events, and the row the
    // preview just left has to be restored.  The line itself still follows
    // the full ``projected`` binding.
    function syncPreviewMarks() {
        var positions = trackVisible ? projected : []
        if (syncedTrack !== track || markModel.count !== positions.length) {
            // The row set no longer belongs to this track, so a single-row
            // rewrite would leave stale marks behind.
            syncMarks()
            return
        }
        var previous = syncedPreviewIndex
        var index = previewIndex
        syncedPreviewIndex = index
        if (previous >= 0 && previous !== index
                && previous < positions.length && previous < markModel.count)
            writeMark(previous, positions[previous])
        if (index >= 0 && index < positions.length && index < markModel.count)
            writeMark(index, positions[index])
    }

    function syncMarks() {
        var positions = trackVisible ? projected : []
        while (markModel.count > positions.length)
            markModel.remove(markModel.count - 1)
        for (var i = 0; i < positions.length; ++i)
            writeMark(i, positions[i])
        syncedTrack = track
        syncedPreviewIndex = previewIndex
    }
    onProjectedChanged: {
        if (sameMarkState(markStateValues())) {
            syncPreviewMarks()
        } else {
            markState = markStateValues()
            syncMarks()
        }
    }
    onTrackVisibleChanged: {
        markState = null
        syncMarks()
    }
    onHighlightIndexChanged: syncMarks()
    onHoverIndexChanged: syncMarks()
    // Rounding can change without moving any point, so the projection binding
    // does not re-evaluate.  The payload replaces the rounding list instead;
    // treat that as a full row refresh so the fast preview path cannot keep a
    // stale colour.
    onRoundingsChanged: {
        markState = markStateValues()
        syncMarks()
    }
    Component.onCompleted: {
        markState = markStateValues()
        syncMarks()
    }

    Shape {
        id: trackShape
        objectName: "editableTrackShape"
        anchors.fill: parent
        visible: trackLayer.lineVisible
        antialiasing: true
        preferredRendererType: Shape.GeometryRenderer

        ShapePath {
            id: trackPath
            objectName: "editableTrackPath"
            fillColor: "transparent"
            strokeColor: trackLayer.strokeColor
            strokeWidth: trackLayer.strokeWidth
            strokeStyle: ShapePath.DashLine
            // Known approximation: Qt restarts the dash phase at the start of
            // the path whenever this polyline is rebuilt (every preview frame,
            // pan or zoom), and its dasher reports lengths in stroke widths.
            // The on/off rhythm matches the Canvas; the exact phase through a
            // join can differ by up to half a dash.
            dashPattern: trackLayer.dashPattern
            dashOffset: 0
            capStyle: ShapePath.FlatCap
            joinStyle: ShapePath.MiterJoin

            PathPolyline {
                objectName: "editableTrackPolyline"
                path: trackLayer.projected || []
            }
        }
    }

    Text {
        objectName: "editableTrackName"
        visible: trackLayer.lineVisible
        // Canvas: fillText(name, first.x - 10, first.y - 10) with the baseline
        // at y, so the text top sits one ascent (about one pixel size) above.
        x: trackLayer.firstPoint.x - 10
        y: trackLayer.firstPoint.y - 10 - font.pixelSize
        text: trackLayer.track && trackLayer.track.name !== undefined
              ? String(trackLayer.track.name) : ""
        color: Qt.rgba(17 / 255, 17 / 255, 17 / 255, 0.7)
        font.pixelSize: 10
        font.family: "sans-serif"
    }

    Repeater {
        objectName: "editableTrackMarks"
        model: markModel
        Item {
            required property int index
            required property real px
            required property real py
            required property bool highlighted
            required property bool hovered
            required property var rounding
            id: mark
            objectName: "editableTrackMark" + index
            x: px
            y: py

            readonly property color markColor: trackLayer.markColor(rounding)
            // The row the drag preview is currently moving: the probe keeps a
            // dragged handle lit the same way it keeps a hovered one lit.
            readonly property bool dragged: trackLayer.previewIndex >= 0
                                              && trackLayer.previewIndex === index

            Text {
                objectName: "editableTrackNumber"
                // Canvas: fillText(String(i + 1), point.x - 4, point.y + 18).
                x: -4
                y: 18 - font.pixelSize
                text: String(index + 1)
                font.pixelSize: 10
                font.family: "sans-serif"
                color: highlighted
                        ? Qt.rgba(1, 0, 0, 0.9)
                        : hovered
                          ? Qt.rgba(0.75, 0.12, 0.08, 1)
                          : trackLayer.trackActive
                            ? Qt.rgba(17 / 255, 17 / 255, 17 / 255, 0.7)
                            : Qt.rgba(17 / 255, 17 / 255, 17 / 255, 0.3)
            }

            // Wide edit indicator.  Always present, transparent until the point
            // is hovered or being dragged, then faded in over 110 ms like the
            // probe's handle circle.  Retaining it keeps the mark items stable
            // across pointer moves instead of recreating them.
            Rectangle {
                objectName: "editableTrackHalo"
                // Read-only mirrors for tests and tooling: PySide exposes a
                // ``Rectangle`` border as ``QQuickPen*``, which it cannot
                // convert, so the ring colour and width are republished as
                // plain properties.  Invisible to the user.
                readonly property color ringColor: mark.markColor
                readonly property real ringWidth: border.width
                width: trackLayer.haloRadius * 2
                height: width
                radius: width / 2
                x: -width / 2
                y: -height / 2
                color: "transparent"
                border.width: 2
                border.color: ringColor
                antialiasing: true
                opacity: (hovered || dragged) ? 0.9 : 0.0
                Behavior on opacity {
                    NumberAnimation { duration: 110 }
                }
            }

            // The mark itself: never faded out, just bolder and larger while
            // highlighted or hovered.
            Rectangle {
                objectName: "editableTrackHandle"
                // See the halo: PySide cannot convert ``border`` (QQuickPen*),
                // so the ring colour and width are republished directly.
                readonly property color ringColor: mark.markColor
                readonly property real ringWidth: border.width
                readonly property real ringRadius: highlighted || hovered
                                                 ? trackLayer.handleRadiusActive
                                                 : trackLayer.handleRadius
                width: ringRadius * 2
                height: width
                radius: width / 2
                x: -width / 2
                y: -height / 2
                color: "transparent"
                border.width: 1.7
                border.color: ringColor
                antialiasing: true
            }
        }
    }
}
