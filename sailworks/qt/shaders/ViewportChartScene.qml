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
    // The ENC source's own state, in the scene's own words.  An empty
    // string means there is nothing to report.
    property string encStatus: ""
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
            // Images and numerical fill are reference-local.
            Item {
                objectName: "environmentFillBand"
                width: mapClip.width
                height: mapClip.height
                ChartLayer {
                    objectName: "environmentLayer"
                    anchors.fill: parent
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
                id: routes
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
                property string captionText: ""
                property bool dashboardVisible: true
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

                onPaint: {
                    const context = getContext("2d")
                    context.clearRect(0, 0, width, height)
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
                }
                onRoutePlanChanged: requestPaint()
                onRoutePreviewChanged: requestPaint()
                onSelectedPointChanged: requestPaint()
                onTrackPointsChanged: requestPaint()
                onTrackPositionChanged: requestPaint()
                onTrackHeadingChanged: requestPaint()
                onTrackAwaChanged: requestPaint()
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
        visible: routes.captionText !== ""
        x: 8
        y: routes.dashboardVisible ? 8 : parent.height - 34
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
            text: routes.captionText
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
}
