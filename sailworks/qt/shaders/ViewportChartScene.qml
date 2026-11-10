import QtQuick

// The viewport's scene with the chart layer.
//
// The chart layer is the application's chart composition, published as one
// image.  It has its own state and its own generation: the weather layers
// are published before the chart is ready, and adding the chart resets
// neither the weather nor the camera nor the forecast time.
//
// The ENC cells are part of the same composition: they are painted into the
// same image, in the application's own order.  Their state is reported
// beside the chart's, never in place of it: a coverage miss says so
// instead of showing an empty chart that reads as "there is no data here".
//
// The scene is the viewport's own: the application's default path never
// loads it.
Item {
    id: root
    property bool fillEnabled: false
    // The wave height fill's front slot, driven exactly like the wind fill:
    // the scene property is what the viewport writes, so the binding lands on
    // the current scene instance and not on a slot held from a replaced one.
    property bool waveFillEnabled: false
    // The ENC source's own state, in the scene's own words.  An empty
    // string means there is nothing to report.
    property string encStatus: ""
    // Satellite imagery is a raster layer below the composed chart. The
    // image arrives from MapLibre; the attribution is shown with it.
    property bool satelliteVisible: false
    property bool basemapVisible: true
    property bool encVisible: true
    property string satelliteAttribution: ""
    property string satelliteStatus: ""
    // The window's sidebar drawers.  Their tabs live in the scene because a
    // native Quick host always draws above the window's QWidget children.
    property bool leftDrawerClosed: false
    property bool rightDrawerClosed: false
    property bool drawerTabsVisible: true
    signal layerToggled(string layer, bool visible)
    signal drawerToggled(string side)
    // Embedded use supplies the world/raster base here. It must not depend
    // on QWidget parent pixels showing through the Quick host.
    Item {
        id: mapClip
        objectName: "mapClip"
        anchors.fill: parent
        clip: true
        Item {
            id: mapContent
            objectName: "mapContent"
            x: 0
            y: 0
            width: mapClip.width
            height: mapClip.height

            Item {
                objectName: "baseBand"
                width: mapClip.width
                height: mapClip.height
                Item {
                    id: world
                    objectName: "worldLayer"
                    anchors.fill: parent
                    property real imageX: 0
                    property real imageY: 0
                    property real imageWidth: 0
                    property real imageHeight: 0
                    property url source
                    Repeater {
                        id: worldCopies
                        model: world.imageWidth > 0 ?
                                   2 * Math.ceil(world.width / world.imageWidth) + 3 : 0
                        Image {
                            x: world.imageX + (index - Math.floor(worldCopies.count / 2)) * world.imageWidth
                            y: world.imageY
                            width: world.imageWidth
                            height: world.imageHeight
                            source: world.source
                            cache: false
                            smooth: true
                        }
                    }
                }
                ChartLayer {
                    objectName: "baseLayer"
                    anchors.fill: parent
                }
            }
            ChartLayer {
                id: chart
                objectName: "chartLayer"
                anchors.fill: parent
            }
            // Satellite sits above the chart: the composed chart paints an
            // opaque sea/land background, so a layer beneath it is invisible.
            // The opacity keeps coastlines and soundings legible under imagery.
            ChartLayer {
                objectName: "satelliteLayer"
                anchors.fill: parent
                opacity: 0.9
                smooth: true
                visible: root.satelliteVisible
                Text {
                    anchors.right: parent.right
                    anchors.bottom: parent.bottom
                    anchors.margins: 6
                    text: root.satelliteStatus !== "" ? root.satelliteStatus : root.satelliteAttribution
                    color: root.satelliteStatus !== "" ? "#ffd479" : "#ffffffdd"
                    font.pixelSize: 11
                    style: Text.Outline
                    styleColor: "#000000"
                    visible: text !== ""
                }
            }
            // Images and numerical fill are reference-local.
            Item {
                objectName: "environmentFillBand"
                width: mapClip.width
                height: mapClip.height
                ChartLayer {
                    objectName: "environmentLayer"
                    anchors.fill: parent
                }
                // The wave height fill is the wind fill's sibling and lives in
                // this always-drawn band for the same reason: Qt Quick
                // compiles a ShaderEffect only while it is rendered, and a
                // slot inside a hidden staging band never compiles.  A scalar
                // field travels in u0/u1 with a zero plane in v0/v1, so it
                // reuses WeatherFill and the weather shader unchanged.
                WeatherFill {
                    id: waveFill
                    objectName: "waveFill"
                    anchors.fill: parent
                    visible: root.waveFillEnabled
                    gradientLevels: 9
                    fragmentShader: Qt.resolvedUrl("wave.frag.qsb")
                    property string statusText: ""
                    property bool waveLoading: false
                }
                // The pending wave frame is drawn at the same low opacity the
                // wind staging slot uses, and swapped in only when adopted.
                WeatherFill {
                    objectName: "waveFillBack"
                    anchors.fill: parent
                    visible: false
                    gradientLevels: 9
                    fragmentShader: Qt.resolvedUrl("wave.frag.qsb")
                    property string statusText: ""
                    property bool waveLoading: false
                }
                WeatherFill {
                    id: fill
                    objectName: "weatherFill"
                    anchors.fill: parent
                    visible: root.fillEnabled
                    fragmentShader: Qt.resolvedUrl("weather-fill-front.frag.qsb")
                }
                WeatherFill {
                    objectName: "weatherFillBack"
                    anchors.fill: parent
                    visible: false
                    fragmentShader: Qt.resolvedUrl("weather-fill-back.frag.qsb")
                }
            }
            // Barbs are reference-local like the fill and the retained
            // imagery: the shared mapContent pan places them, and a rebase
            // compensates the anchor offset instead of re-encoding anchors.
            Item {
                objectName: "weatherBand"
                width: mapClip.width
                height: mapClip.height
                // Shared node-anchor pool: one provider per atlas slot that both
                // the front and back barbs sample, so an anchor publication is
                // retained once. Hidden and declaration-ordered ahead of the
                // barbs, so stacking is unchanged.
                Image {
                    id: anchorAtlasX0
                    objectName: "sharedAnchorX0"; visible: false; smooth: false; cache: false
                    retainWhileLoading: true; property bool hadReady: false
                    onStatusChanged: if (status === Image.Ready) hadReady = true
                }
                Image {
                    id: anchorAtlasY0
                    objectName: "sharedAnchorY0"; visible: false; smooth: false; cache: false
                    retainWhileLoading: true; property bool hadReady: false
                    onStatusChanged: if (status === Image.Ready) hadReady = true
                }
                Image {
                    id: anchorAtlasX1
                    objectName: "sharedAnchorX1"; visible: false; smooth: false; cache: false
                    retainWhileLoading: true; property bool hadReady: false
                    onStatusChanged: if (status === Image.Ready) hadReady = true
                }
                Image {
                    id: anchorAtlasY1
                    objectName: "sharedAnchorY1"; visible: false; smooth: false; cache: false
                    retainWhileLoading: true; property bool hadReady: false
                    onStatusChanged: if (status === Image.Ready) hadReady = true
                }
                // Geographic marine portrayal remains below wind barbs.
                Item {
                    objectName: "marineBand"
                    anchors.fill: parent
                    Image {
                        objectName: "marineCurrents"
                        visible: false
                        smooth: false
                        cache: false
                        asynchronous: false
                        fillMode: Image.Stretch
                    }
                    Image {
                        objectName: "marineLabels"
                        visible: false
                        smooth: false
                        cache: false
                        asynchronous: false
                        fillMode: Image.Stretch
                    }
                    Image {
                        objectName: "icoadsMarkers"
                        visible: false
                        smooth: false
                        cache: false
                        asynchronous: false
                        fillMode: Image.Stretch
                    }
                }
                WeatherBarbs {
                    id: barbs
                    objectName: "weatherBarbs"
                    anchors.fill: parent
                    sharedAnchorX0: anchorAtlasX0
                    sharedAnchorY0: anchorAtlasY0
                    sharedAnchorX1: anchorAtlasX1
                    sharedAnchorY1: anchorAtlasY1
                }
                WeatherBarbs {
                    objectName: "weatherBarbsBack"
                    anchors.fill: parent
                    visible: false
                    sharedAnchorX0: anchorAtlasX0
                    sharedAnchorY0: anchorAtlasY0
                    sharedAnchorX1: anchorAtlasX1
                    sharedAnchorY1: anchorAtlasY1
                }
                // Streamlines still paint in screen space, so this band alone
                // cancels the shared mapContent pan. It stays inside the
                // weather band, behind the navigation and route overlays.
                Item {
                    objectName: "streamlineBand"
                    x: -mapContent.x
                    y: -mapContent.y
                    width: mapClip.width
                    height: mapClip.height
                    WeatherStreamlines { objectName: "weatherStreamlines"; anchors.fill: parent; visible: false }
                }
            }
            ChartLayer {
                objectName: "navigationOverlay"
                anchors.fill: parent
            }
            Canvas {
                id: routeOverlayCanvas
                objectName: "routeOverlay"
                property real panX: 0
                property real panY: 0
                property real panPadding: Math.max(parent.width, parent.height) * 0.5
                property real viewportWidth: parent.width
                property real viewportHeight: parent.height
                x: -panPadding + panX
                y: -panPadding + panY
                width: viewportWidth + 2 * panPadding
                height: viewportHeight + 2 * panPadding
                property var routePlan: []
                property var routePreview: []
                property var selectedPoint: []
                property var trackPoints: []
                property var trackPosition: []
                property var trackHeading: null
                property var trackAwa: null
                property var pois: []
                property var tracks: []
                property var raceTracks: []
                property var raceCourses: []
                property var racePositions: []
                property var routes: []
                property var aisTargets: []
                property var isochrones: null
                property var dashboardGauges: []
                property string captionText: ""
                property bool dashboardVisible: true
                property var measurePoints: []
                property var measurePreview: []
                property real measureDistance: -1
                property var movePreview: []
                property real centerLatitude: 0
                property real centerLongitude: 0
                property real zoom: 1

                function screenPoint(point) {
                    const world = 256 * Math.pow(2, zoom)
                    const latitude = Math.max(-85.05112878, Math.min(85.05112878, point[0]))
                    let dx = (point[1] - centerLongitude) * world / 360
                    dx -= Math.round(dx / world) * world
                    const radians = latitude * Math.PI / 180
                    const y = world * (1 - Math.asinh(Math.tan(radians)) / Math.PI) / 2
                    const centerRadians = centerLatitude * Math.PI / 180
                    const centerY = world * (1 - Math.asinh(Math.tan(centerRadians)) / Math.PI) / 2
                    return Qt.point(panPadding + viewportWidth / 2 + dx,
                                    panPadding + viewportHeight / 2 + y - centerY)
                }

                function speedColor(speed, alpha) {
                    let r, g, b
                    if (speed < 2) { r = 0.3; g = 0.5; b = 1.0 }
                    else if (speed < 4) { r = 0.2; g = 0.7; b = 0.9 }
                    else if (speed < 6) { r = 0.2; g = 0.85; b = 0.5 }
                    else if (speed < 8) { r = 0.5; g = 0.9; b = 0.2 }
                    else if (speed < 10) { r = 0.9; g = 0.85; b = 0.1 }
                    else if (speed < 12) { r = 1.0; g = 0.6; b = 0.1 }
                    else { r = 1.0; g = 0.3; b = 0.1 }
                    return `rgba(${Math.round(r * 255)},${Math.round(g * 255)},` +
                           `${Math.round(b * 255)},${alpha})`
                }

                function roundedRect(context, x, y, w, h, r) {
                    context.beginPath()
                    context.moveTo(x + r, y)
                    context.lineTo(x + w - r, y)
                    context.arcTo(x + w, y, x + w, y + r, r)
                    context.lineTo(x + w, y + h - r)
                    context.arcTo(x + w, y + h, x + w - r, y + h, r)
                    context.lineTo(x + r, y + h)
                    context.arcTo(x, y + h, x, y + h - r, r)
                    context.lineTo(x, y + r)
                    context.arcTo(x, y, x + r, y, r)
                    context.closePath()
                }

                function drawRoute(context, points, color) {
                    if (!points || points.length < 2)
                        return
                    context.beginPath()
                    const first = screenPoint(points[0])
                    context.moveTo(first.x, first.y)
                    for (let i = 1; i < points.length; ++i) {
                        const point = screenPoint(points[i])
                        context.lineTo(point.x, point.y)
                    }
                    context.lineWidth = 2
                    context.strokeStyle = color
                    context.setLineDash([6, 4])
                    context.stroke()
                    context.setLineDash([])
                }

                function hasFiniteValue(value) {
                    return value !== null && value !== undefined && isFinite(Number(value))
                }

                function drawTrackMarker(context) {
                    if (!trackPosition || trackPosition.length !== 2)
                        return
                    const point = screenPoint(trackPosition)
                    if (!hasFiniteValue(trackHeading)) {
                        context.beginPath()
                        context.arc(point.x, point.y, 5, 0, Math.PI * 2)
                        context.fillStyle = "white"
                        context.fill()
                        context.lineWidth = 2
                        context.strokeStyle = "#d8851b"
                        context.stroke()
                        return
                    }

                    // The hull points north in local coordinates; positive canvas
                    // rotation is clockwise, matching compass heading degrees.
                    const heading = ((Number(trackHeading) % 360) + 360) % 360
                    context.save()
                    context.translate(point.x, point.y)
                    context.rotate(heading * Math.PI / 180)
                    context.beginPath()
                    context.moveTo(0, -10)
                    context.lineTo(4.5, -3)
                    context.lineTo(4, 5)
                    context.lineTo(0, 8)
                    context.lineTo(-4, 5)
                    context.lineTo(-4.5, -3)
                    context.closePath()
                    context.fillStyle = "white"
                    context.fill()
                    context.lineWidth = 1.5
                    context.strokeStyle = "#d8851b"
                    context.stroke()

                    if (hasFiniteValue(trackAwa)) {
                        let awa = ((Number(trackAwa) % 360) + 360) % 360
                        if (awa > 180)
                            awa -= 360
                        context.save()
                        context.translate(0, -6)
                        context.rotate(awa * Math.PI / 180)
                        context.beginPath()
                        context.moveTo(0, 1)
                        context.lineTo(0, -11)
                        context.strokeStyle = "#28a6c2"
                        context.lineWidth = 1.5
                        context.stroke()
                        context.beginPath()
                        context.moveTo(0, -13)
                        context.lineTo(-2.5, -8.5)
                        context.lineTo(2.5, -8.5)
                        context.closePath()
                        context.fillStyle = "#28a6c2"
                        context.fill()
                        context.restore()
                    }
                    context.restore()
                }

                function drawMeasurement(context) {
                    if (!measurePoints || !measurePoints.length)
                        return
                    context.strokeStyle = "#ffcc33"
                    context.fillStyle = "#ffcc33"
                    context.lineWidth = 2
                    context.setLineDash([])
                    context.font = "12px sans-serif"
                    const first = screenPoint(measurePoints[0])
                    if (measurePoints.length === 2) {
                        const second = screenPoint(measurePoints[1])
                        context.beginPath()
                        context.moveTo(first.x, first.y)
                        context.lineTo(second.x, second.y)
                        context.stroke()
                        if (hasFiniteValue(measureDistance)) {
                            context.fillText(`${Number(measureDistance).toFixed(1)} km`,
                                             (first.x + second.x) / 2,
                                             (first.y + second.y) / 2)
                        }
                    } else if (measurePreview && measurePreview.length === 2) {
                        const second = screenPoint(measurePreview)
                        context.beginPath()
                        context.moveTo(first.x, first.y)
                        context.lineTo(second.x, second.y)
                        context.stroke()
                        if (hasFiniteValue(measureDistance)) {
                            context.fillText(`${Number(measureDistance).toFixed(1)} km`,
                                             (first.x + second.x) / 2,
                                             (first.y + second.y) / 2)
                        }
                    } else {
                        context.beginPath()
                        context.arc(first.x, first.y, 4, 0, Math.PI * 2)
                        context.fill()
                    }
                }

                onPaint: {
                    const context = getContext("2d")
                    context.clearRect(0, 0, width, height)
                    if (isochrones && isochrones.frontiers) {
                        for (let f = 0; f < isochrones.frontiers.length; ++f) {
                            const frontier = isochrones.frontiers[f]
                            // Selected native layer is emphasised; a legacy
                            // preview frontier has no selection identity.
                            const selected = frontier.selected === true
                            context.strokeStyle = selected
                                ? "rgba(255,140,0,0.95)"
                                : "rgba(0,0,204,0.5)"
                            context.lineWidth = selected ? 2.5 : 1.5
                            context.setLineDash([])
                            for (let s = 0; s < frontier.segments.length; ++s) {
                                const segment = frontier.segments[s]
                                const first = screenPoint(segment[0])
                                const second = screenPoint(segment[1])
                                context.beginPath()
                                context.moveTo(first.x, first.y)
                                context.lineTo(second.x, second.y)
                                context.stroke()
                            }
                            context.fillStyle = "rgba(0,0,204,0.5)"
                            for (let p = 0; p < frontier.points.length; ++p) {
                                const point = screenPoint(frontier.points[p])
                                context.beginPath()
                                context.arc(point.x, point.y, 1.5, 0, Math.PI * 2)
                                context.fill()
                            }
                        }
                        if (isochrones.path && isochrones.path.length) {
                            context.strokeStyle = "rgba(255,0,0,0.8)"
                            context.lineWidth = 1
                            for (let i = 1; i < isochrones.path.length; ++i) {
                                const previous = screenPoint(isochrones.path[i - 1])
                                const point = screenPoint(isochrones.path[i])
                                context.beginPath()
                                context.moveTo(previous.x, previous.y)
                                context.lineTo(point.x, point.y)
                                context.stroke()
                            }
                            context.strokeStyle = "rgba(255,255,255,1)"
                            for (let i = 0; i < isochrones.path.length; ++i) {
                                const point = screenPoint(isochrones.path[i])
                                context.beginPath()
                                context.arc(point.x, point.y, 5, 0, Math.PI * 2)
                                context.stroke()
                            }
                        }
                        if (isochrones.exclusions && isochrones.exclusions.length) {
                            context.fillStyle = "rgba(230,13,13,0.18)"
                            context.strokeStyle = "rgba(217,5,5,0.9)"
                            context.lineWidth = 1.5
                            for (let i = 0; i < isochrones.exclusions.length; ++i) {
                                const point = screenPoint(isochrones.exclusions[i])
                                context.beginPath()
                                context.arc(point.x, point.y, 7, 0, Math.PI * 2)
                                context.fill()
                                context.beginPath()
                                context.moveTo(point.x - 3, point.y - 3)
                                context.lineTo(point.x + 3, point.y + 3)
                                context.moveTo(point.x - 3, point.y + 3)
                                context.lineTo(point.x + 3, point.y - 3)
                                context.stroke()
                            }
                        }
                    }
                    drawRoute(context, routePlan, "#5295d2")
                    drawRoute(context, routePreview, "#ef5a8c")
                    if (trackPoints && trackPoints.length > 1) {
                        context.beginPath()
                        const firstTrackPoint = screenPoint(trackPoints[0])
                        context.moveTo(firstTrackPoint.x, firstTrackPoint.y)
                        for (let i = 1; i < trackPoints.length; ++i) {
                            const point = screenPoint(trackPoints[i])
                            context.lineTo(point.x, point.y)
                        }
                        context.lineWidth = 1
                        context.strokeStyle = "#008080"
                        context.globalAlpha = 0.5
                        context.setLineDash([])
                        context.stroke()
                        context.globalAlpha = 1
                    }
                    if (routes && routes.length) {
                        for (let r = 0; r < routes.length; ++r) {
                            const route = routes[r]
                            const points = route.points
                            if (!points || points.length < 2)
                                continue
                            const highlight = Boolean(route.highlight)
                            for (let i = 1; i < points.length; ++i) {
                                const previous = screenPoint(points[i - 1])
                                const point = screenPoint(points[i])
                                context.beginPath()
                                context.moveTo(previous.x, previous.y)
                                context.lineTo(point.x, point.y)
                                context.lineWidth = highlight ? 3 : 2
                                context.strokeStyle = speedColor(
                                    points[i][2] || 0, highlight ? 0.9 : 0.63)
                                context.stroke()
                            }
                            context.font = "10px sans-serif"
                            context.fillStyle = "rgba(17,17,17,0.7)"
                            const first = screenPoint(points[0])
                            context.fillText(route.name, first.x + 10, first.y)
                            for (let i = 0; i < points.length; ++i) {
                                const point = screenPoint(points[i])
                                context.beginPath()
                                context.arc(point.x, point.y,
                                            highlight ? 3 : 2, 0, Math.PI * 2)
                                context.fillStyle = speedColor(
                                    points[i][2] || 0, 1.0)
                                context.fill()
                            }
                            if (route.boat && route.boat.length === 2) {
                                const boat = screenPoint(route.boat)
                                context.beginPath()
                                context.arc(boat.x, boat.y, 8, 0, Math.PI * 2)
                                context.fillStyle = "rgba(26,153,26,1)"
                                context.fill()
                                context.lineWidth = 1.5
                                context.strokeStyle = "rgba(255,255,255,0.9)"
                                context.stroke()
                            }
                        }
                    }
                    if (tracks && tracks.length) {
                        for (let t = 0; t < tracks.length; ++t) {
                            const track = tracks[t]
                            const points = track.points
                            if (!points || points.length < 2)
                                continue
                            const preview = (movePreview && movePreview.length === 4
                                             && movePreview[0] === t)
                                            ? movePreview : null
                            const previewIndex = preview ? preview[1] : -1
                            const active = Boolean(track.active)
                            context.beginPath()
                            const first = previewIndex === 0
                                ? screenPoint([preview[2], preview[3]])
                                : screenPoint(points[0])
                            context.moveTo(first.x, first.y)
                            for (let i = 1; i < points.length; ++i) {
                                const point = i === previewIndex
                                    ? screenPoint([preview[2], preview[3]])
                                    : screenPoint(points[i])
                                context.lineTo(point.x, point.y)
                            }
                            context.lineWidth = active ? 1 : 0.8
                            context.strokeStyle = active
                                ? "rgba(145,50,28,1)"
                                : "rgba(145,74,124,0.6)"
                            context.setLineDash([8, 4])
                            context.stroke()
                            context.setLineDash([])
                            context.font = "10px sans-serif"
                            context.fillStyle = "rgba(17,17,17,0.7)"
                            context.fillText(track.name, first.x - 10, first.y - 10)
                            for (let i = 0; i < points.length; ++i) {
                                const point = i === previewIndex
                                    ? screenPoint([preview[2], preview[3]])
                                    : screenPoint(points[i])
                                context.fillStyle = i === track.highlight
                                    ? "rgba(255,0,0,0.9)"
                                    : active
                                        ? "rgba(17,17,17,0.7)"
                                        : "rgba(17,17,17,0.3)"
                                context.fillText(String(i + 1),
                                                 point.x - 4, point.y + 18)
                                if (track.editing) {
                                    context.beginPath()
                                    context.arc(point.x, point.y,
                                                i === track.highlight ? 7 : 5,
                                                0, Math.PI * 2)
                                    context.strokeStyle = active
                                        ? "rgba(145,50,28,1)"
                                        : "rgba(145,74,124,0.6)"
                                    context.lineWidth = 1
                                    context.stroke()
                                }
                            }
                        }
                    }
                    if (aisTargets && aisTargets.length) {
                        for (let a = 0; a < aisTargets.length; ++a) {
                            const target = aisTargets[a]
                            const point = screenPoint([target.lat, target.lon])
                            if (!isFinite(point.x) || !isFinite(point.y))
                                continue
                            const color = target.color || [0.5, 0.5, 0.5]
                            const fill = `rgba(${Math.round(color[0] * 255)},` +
                                `${Math.round(color[1] * 255)},` +
                                `${Math.round(color[2] * 255)},0.7)`
                            const stroke = `rgba(${Math.round(color[0] * 255)},` +
                                `${Math.round(color[1] * 255)},` +
                                `${Math.round(color[2] * 255)},1)`
                            const size = 7
                            context.save()
                            context.translate(point.x, point.y)
                            context.rotate((target.angle || 0) * Math.PI / 180)
                            context.beginPath()
                            context.moveTo(0, -size * 2)
                            context.lineTo(size * 0.8, size)
                            context.lineTo(0, size * 0.6)
                            context.lineTo(-size * 0.8, size)
                            context.closePath()
                            context.fillStyle = fill
                            context.fill()
                            context.lineWidth = 1
                            context.strokeStyle = stroke
                            context.stroke()
                            if ((target.sog || 0) > 0.5) {
                                const length = Math.min((target.sog || 0) * 3, 40)
                                context.beginPath()
                                context.moveTo(0, -size * 2)
                                context.lineTo(0, -size * 2 - length)
                                context.lineWidth = 0.8
                                context.strokeStyle = fill
                                context.stroke()
                            }
                            context.restore()
                            const label = String(target.label || "")
                            const lx = point.x + size + 4
                            const ly = point.y + 4
                            context.font = "10px sans-serif"
                            const width = context.measureText(label).width
                            context.fillStyle = "rgba(0,0,0,0.5)"
                            context.fillRect(lx - 2, ly - 12, width + 4, 14)
                            context.fillStyle = "rgba(255,255,255,0.95)"
                            context.fillText(label, lx, ly)
                        }
                    }
                    if (raceCourses && raceCourses.length) {
                        for (let c = 0; c < raceCourses.length; ++c) {
                            const course = raceCourses[c]
                            const points = course.points
                            if (!points || points.length < 2)
                                continue
                            const label = String(course.name || "")
                            const upper = label.toUpperCase()
                            const isStart = upper.indexOf("START") === 0
                            const isFinish = upper.indexOf("FINISH") === 0
                            const stroke = isStart
                                ? "rgba(0,200,90,1)"
                                : isFinish
                                    ? "rgba(230,40,40,1)"
                                    : "rgba(255,255,255,0.95)"
                            // Halo pass keeps the course legible over charts/weather.
                            for (let pass = 0; pass < 2; ++pass) {
                                context.beginPath()
                                const first = screenPoint(points[0])
                                context.moveTo(first.x, first.y)
                                for (let i = 1; i < points.length; ++i) {
                                    const point = screenPoint(points[i])
                                    context.lineTo(point.x, point.y)
                                }
                                if (course.closed)
                                    context.closePath()
                                context.lineWidth = pass === 0
                                    ? (isStart || isFinish ? 5 : 3.5)
                                    : (isStart || isFinish ? 3 : 2)
                                context.strokeStyle = pass === 0
                                    ? "rgba(0,0,0,0.55)"
                                    : stroke
                                context.setLineDash(
                                    (isStart || isFinish) ? [] : [10, 6])
                                context.stroke()
                            }
                            context.setLineDash([])
                            if (label) {
                                const anchor = screenPoint(points[0])
                                context.font = "bold 10px sans-serif"
                                const width = context.measureText(label).width
                                context.fillStyle = "rgba(0,0,0,0.6)"
                                context.fillRect(anchor.x - 2, anchor.y - 24,
                                                 width + 4, 13)
                                context.fillStyle = stroke
                                context.fillText(label, anchor.x, anchor.y - 14)
                            }
                            if (!isStart && !isFinish) {
                                for (let i = 0; i < points.length; ++i) {
                                    const point = screenPoint(points[i])
                                    context.beginPath()
                                    context.arc(point.x, point.y, 2.5,
                                                0, Math.PI * 2)
                                    context.fillStyle = "rgba(255,255,255,0.95)"
                                    context.fill()
                                    context.lineWidth = 1
                                    context.strokeStyle = "rgba(0,0,0,0.7)"
                                    context.stroke()
                                }
                            }
                        }
                    }
                    if (raceTracks && raceTracks.length) {
                        for (let t = 0; t < raceTracks.length; ++t) {
                            const track = raceTracks[t]
                            const points = track.points
                            if (!points || points.length < 2)
                                continue
                            const color = track.color || [1, 1, 1]
                            const alpha = track.active ? 1 : 0.6
                            context.beginPath()
                            const first = screenPoint(points[0])
                            context.moveTo(first.x, first.y)
                            for (let i = 1; i < points.length; ++i) {
                                const point = screenPoint(points[i])
                                context.lineTo(point.x, point.y)
                            }
                            context.strokeStyle =
                                `rgba(${Math.round(color[0] * 255)},` +
                                `${Math.round(color[1] * 255)},` +
                                `${Math.round(color[2] * 255)},${alpha})`
                            context.lineWidth = track.active ? 2 : 1.2
                            context.setLineDash([])
                            context.stroke()
                            context.font = "10px sans-serif"
                            context.fillStyle = "rgba(17,17,17,0.7)"
                            context.fillText(track.name, first.x - 10, first.y - 10)
                        }
                    }
                    if (racePositions && racePositions.length) {
                        context.font = "10px sans-serif"
                        for (let b = 0; b < racePositions.length; ++b) {
                            const marker = racePositions[b]
                            const isPair = Array.isArray(marker)
                            const lat = isPair ? marker[0] : marker.lat
                            const lon = isPair ? marker[1] : marker.lon
                            const point = screenPoint([lat, lon])
                            if (!isFinite(point.x) || !isFinite(point.y))
                                continue
                            const color = (!isPair && marker.color)
                                ? marker.color
                                : [0.95, 0.2, 0.1]
                            const fill = `rgba(${Math.round(color[0] * 255)},` +
                                `${Math.round(color[1] * 255)},` +
                                `${Math.round(color[2] * 255)},0.95)`
                            const angle = (!isPair && hasFiniteValue(marker.angle))
                                ? Number(marker.angle) * Math.PI / 180
                                : 0
                            context.save()
                            context.translate(point.x, point.y)
                            context.rotate(angle)
                            // Boat-shaped arrow pointing along the boat's course.
                            context.beginPath()
                            context.moveTo(0, -8)
                            context.lineTo(4.5, 6)
                            context.lineTo(0, 3.5)
                            context.lineTo(-4.5, 6)
                            context.closePath()
                            context.fillStyle = fill
                            context.fill()
                            context.lineWidth = 1.2
                            context.strokeStyle = "rgba(255,255,255,0.95)"
                            context.stroke()
                            context.restore()
                            const label = (!isPair && marker.name)
                                ? String(marker.name)
                                : ""
                            if (label) {
                                const lx = point.x + 8
                                const ly = point.y - 8
                                const width = context.measureText(label).width
                                context.fillStyle = "rgba(0,0,0,0.55)"
                                context.fillRect(lx - 2, ly - 11, width + 4, 14)
                                context.fillStyle = "rgba(255,255,255,0.95)"
                                context.fillText(label, lx, ly)
                            }
                        }
                    }
                    if (pois && pois.length) {
                        for (let i = 0; i < pois.length; ++i) {
                            const poi = pois[i]
                            const point = screenPoint([poi[0], poi[1]])
                            if (!isFinite(point.x) || !isFinite(point.y))
                                continue
                            const label = String(poi[2] || "")
                            const highlight = Boolean(poi[3])
                            const boxWidth = Math.max(12, label.length * 6.7)
                            context.beginPath()
                            context.rect(point.x + 3, point.y - 5, boxWidth, 12)
                            context.lineWidth = highlight ? 1.5 : 0.3
                            context.strokeStyle = highlight ? "#ff0000" : "#111111"
                            context.stroke()
                            context.fillStyle = highlight
                                ? "rgba(255,229,229,0.7)"
                                : "rgba(255,255,255,0.5)"
                            context.fill()
                            context.beginPath()
                            context.arc(point.x, point.y,
                                        highlight ? 4 : 2, 0, Math.PI * 2)
                            context.fillStyle = highlight ? "#ff0000" : "#111111"
                            context.fill()
                            context.font = "10px sans-serif"
                            context.fillStyle = highlight
                                ? "rgba(255,0,0,0.9)"
                                : "rgba(17,17,17,0.7)"
                            context.fillText(label, point.x + 5, point.y + 5)
                        }
                    }
                    if (routePlan && routePlan.length) {
                        const labels = ["Start", "Destination"]
                        for (let i = 0; i < Math.min(2, routePlan.length); ++i) {
                            const point = screenPoint(routePlan[i])
                            context.beginPath()
                            context.arc(point.x, point.y, 3, 0, Math.PI * 2)
                            context.fillStyle = "#5295d2"
                            context.fill()
                            context.font = "12px sans-serif"
                            context.fillStyle = "#5295d2"
                            context.fillText(labels[i],
                                             Math.max(panPadding + 5,
                                                      Math.min(point.x + 5, panPadding + viewportWidth - 75)),
                                             Math.max(panPadding + 15,
                                                      Math.min(point.y - 5, panPadding + viewportHeight - 5)))
                        }
                    }
                    if (selectedPoint && selectedPoint.length === 2) {
                        const point = screenPoint(selectedPoint)
                        context.beginPath()
                        context.arc(point.x, point.y, 5, 0, Math.PI * 2)
                        context.fillStyle = "white"
                        context.fill()
                        context.lineWidth = 2
                        context.strokeStyle = "#5295d2"
                        context.stroke()
                    }
                    drawTrackMarker(context)
                    if (dashboardGauges && dashboardGauges.length) {
                        const gaugeWidth = 110
                        const gaugeHeight = 56
                        const gap = 4
                        const pad = 6
                        const cols = Math.max(1, Math.min(
                            dashboardGauges.length,
                            Math.floor((viewportWidth - pad * 2 + gap) /
                                       (gaugeWidth + gap))))
                        const rows = Math.ceil(dashboardGauges.length / cols)
                        const totalWidth = cols * gaugeWidth + (cols - 1) * gap
                        const totalHeight = rows * gaugeHeight + (rows - 1) * gap
                        const panelX = panPadding +
                            (viewportWidth - totalWidth) / 2 - pad
                        const panelY = panPadding + viewportHeight -
                            totalHeight - pad * 2 - 12
                        roundedRect(context, panelX, panelY,
                                    totalWidth + pad * 2,
                                    totalHeight + pad * 2, 8)
                        context.fillStyle = "rgba(8,8,8,0.55)"
                        context.fill()
                        for (let i = 0; i < dashboardGauges.length; ++i) {
                            const gauge = dashboardGauges[i]
                            const gx = panelX + pad +
                                (i % cols) * (gaugeWidth + gap)
                            const gy = panelY + pad +
                                Math.floor(i / cols) * (gaugeHeight + gap)
                            roundedRect(context, gx, gy, gaugeWidth, gaugeHeight, 6)
                            context.fillStyle = "rgba(26,26,26,0.75)"
                            context.fill()
                            context.lineWidth = 0.8
                            context.strokeStyle = "rgba(89,89,89,0.6)"
                            context.stroke()
                            const accent = gauge.color || [0.4, 0.6, 0.9]
                            context.fillStyle =
                                `rgba(${Math.round(accent[0] * 255)},` +
                                `${Math.round(accent[1] * 255)},` +
                                `${Math.round(accent[2] * 255)},0.9)`
                            context.fillRect(gx, gy, gaugeWidth, 3)
                            context.font = "9px sans-serif"
                            context.textAlign = "center"
                            context.fillStyle = "rgba(153,153,153,1)"
                            context.fillText(gauge.label, gx + gaugeWidth / 2,
                                             gy + 15)
                            context.font = "20px sans-serif"
                            const valueWidth =
                                context.measureText(gauge.text).width
                            context.font = "9px sans-serif"
                            const unitWidth = gauge.unit
                                ? context.measureText(gauge.unit).width + 3
                                : 0
                            const startX = gx +
                                (gaugeWidth - valueWidth - unitWidth) / 2
                            context.font = "20px sans-serif"
                            context.textAlign = "left"
                            context.fillStyle = "rgba(255,255,255,1)"
                            context.fillText(gauge.text, startX,
                                             gy + gaugeHeight - 10)
                            if (gauge.unit) {
                                context.font = "9px sans-serif"
                                context.fillStyle = "rgba(140,140,140,1)"
                                context.fillText(gauge.unit,
                                                 startX + valueWidth + 3,
                                                 gy + gaugeHeight - 10)
                            }
                        }
                        context.textAlign = "left"
                    }
                    drawMeasurement(context)
                }
                onRoutePlanChanged: requestPaint()
                onRoutePreviewChanged: requestPaint()
                onSelectedPointChanged: requestPaint()
                onPoisChanged: requestPaint()
                onTracksChanged: requestPaint()
                onRaceTracksChanged: requestPaint()
                onRaceCoursesChanged: requestPaint()
                onRacePositionsChanged: requestPaint()
                onRoutesChanged: requestPaint()
                onAisTargetsChanged: requestPaint()
                onIsochronesChanged: requestPaint()
                onDashboardGaugesChanged: requestPaint()
                onTrackPointsChanged: requestPaint()
                onTrackPositionChanged: requestPaint()
                onTrackHeadingChanged: requestPaint()
                onTrackAwaChanged: requestPaint()
                onMeasurePointsChanged: requestPaint()
                onMeasurePreviewChanged: requestPaint()
                onMeasureDistanceChanged: requestPaint()
                onMovePreviewChanged: requestPaint()
                onCenterLatitudeChanged: requestPaint()
                onCenterLongitudeChanged: requestPaint()
                onZoomChanged: requestPaint()
            }
        }
    }
    ChartLayer {
        objectName: "dashboardOverlay"
        anchors.fill: parent
    }
    Rectangle {
        objectName: "mapCaption"
        visible: routeOverlayCanvas.captionText !== ""
        x: 8
        y: routeOverlayCanvas.dashboardVisible ? 8 : parent.height - 34
        width: Math.min(captionText.implicitWidth + 16, parent.width - 16)
        height: 26
        radius: 4
        color: "#9b000000"
        Text {
            id: captionText
            anchors.fill: parent
            anchors.margins: 8
            verticalAlignment: Text.AlignVCenter
            color: "#eeeeec"
            font.pixelSize: 12
            elide: Text.ElideRight
            text: routeOverlayCanvas.captionText
        }
    }
    // The ENC source's own status.  It is drawn last, so it stays readable
    // over the weather layers, and it never shows the weather's state.
    Rectangle {
        id: encStatus
        objectName: "encStatus"
        visible: root.encStatus !== ""
        x: 8
        y: 62
        width: encStatusText.implicitWidth + 12
        height: encStatusText.implicitHeight + 8
        color: "#000000be"
        Text {
            id: encStatusText
            anchors.centerIn: parent
            color: "white"
            font.pixelSize: 12
            text: root.encStatus
        }
    }
    // Layer switcher: satellite, vector basemap and ENC, top-right.
    Rectangle {
        id: layerSwitcher
        objectName: "layerSwitcher"
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.topMargin: 10
        anchors.rightMargin: 10
        width: layerRow.implicitWidth + 12
        height: 34
        radius: 8
        color: "#000000c0"
        border.width: 1
        border.color: "#33ffffff"
        Row {
            id: layerRow
            anchors.centerIn: parent
            spacing: 4
            Repeater {
                model: [
                    { key: "satellite", label: "Satellite" },
                    { key: "basemap", label: "Basemap" },
                    { key: "enc", label: "ENC" }
                ]
                delegate: Rectangle {
                    property bool active: modelData.key === "satellite"
                                           ? root.satelliteVisible
                                           : modelData.key === "basemap"
                                             ? root.basemapVisible
                                             : root.encVisible
                    width: layerLabel.implicitWidth + 16
                    height: 26
                    radius: 6
                    color: active ? "#e8e8e8" : "#14ffffff"
                    Text {
                        id: layerLabel
                        anchors.centerIn: parent
                        text: modelData.label
                        color: parent.active ? "#111111" : "#dddddd"
                        font.pixelSize: 12
                    }
                    MouseArea {
                        anchors.fill: parent
                        onClicked: root.layerToggled(modelData.key, !parent.active)
                    }
                }
            }
        }
    }
    // Sidebar drawer tabs.  They sit at the map's mid-height edges and are part
    // of the scene, so they stay above the native viewport and its imagery.
    Rectangle {
        id: leftDrawerTab
        objectName: "leftDrawerTab"
        width: 18
        height: 64
        visible: root.drawerTabsVisible
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        z: 1000
        radius: 6
        color: leftDrawerArea.containsMouse ? "#3c3c3c" : "#303030"
        border.width: 1
        border.color: "#454545"
        Text {
            anchors.centerIn: parent
            text: root.leftDrawerClosed ? "\u203a" : "\u2039"
            color: leftDrawerArea.containsMouse ? "#ffffff" : "#c8c8c5"
            font.pixelSize: 16
            font.bold: true
        }
        MouseArea {
            id: leftDrawerArea
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: root.drawerToggled("left")
        }
    }
    Rectangle {
        id: rightDrawerTab
        objectName: "rightDrawerTab"
        width: 18
        height: 64
        visible: root.drawerTabsVisible
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        z: 1000
        radius: 6
        color: rightDrawerArea.containsMouse ? "#3c3c3c" : "#303030"
        border.width: 1
        border.color: "#454545"
        Text {
            anchors.centerIn: parent
            text: root.rightDrawerClosed ? "\u2039" : "\u203a"
            color: rightDrawerArea.containsMouse ? "#ffffff" : "#c8c8c5"
            font.pixelSize: 16
            font.bold: true
        }
        MouseArea {
            id: rightDrawerArea
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: root.drawerToggled("right")
        }
    }
}
