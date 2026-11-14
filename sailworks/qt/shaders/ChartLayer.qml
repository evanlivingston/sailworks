import QtQuick

// The viewport's chart layer: the composed chart image.
//
// The image is published by the chart layer's own generation.  A superseded
// generation never reaches this item: the layer publishes only the
// generation that is still current.
Item {
    id: root
    property alias source: root.slot0Source
    property url slot0Source
    property url slot1Source
    property string slot0Token: ""
    property string slot1Token: ""
    property int frontSlot: 0
    // Chart composition is pixel-exact at native scale; a raster preview
    // (satellite) opts into smooth scaling while it is being resized.
    property bool smooth: false
    // Photographic rasters (satellite) are scaled during a zoom, so they need
    // mipmaps to avoid aliasing and shimmer; the vector-ish layers stay
    // nearest-neighbour deliberately.
    property bool mipmap: false
    signal slotResult(int slot, string token, bool success)
    property int frontStatus: frontSlot === 0 ? (slot0.item ? slot0.item.status : Image.Null) : (slot1.item ? slot1.item.status : Image.Null)
    property bool ready: frontStatus === Image.Ready
    property string sourceError: frontStatus === Image.Error ? "chart image unavailable" : ""
    // The chart layer's own state: it never changes the weather's state.
    property bool chartLoading: false
    property bool chartUnavailable: false
    // The composed image covers the camera's window with the application's
    // own margin, so it is placed by the offset between the composed
    // centre and the current centre.
    property real imageX: 0
    property real imageY: 0
    property real imageWidth: 0
    property real imageHeight: 0
    onSlot0SourceChanged: { slot0.active = false; slot0.active = slot0Source.toString() !== "" }
    onSlot1SourceChanged: { slot1.active = false; slot1.active = slot1Source.toString() !== "" }
    Component {
        id: chartImage
        Image {
            property string attemptToken: ""
            property int attemptSlot: -1
            smooth: root.smooth
            mipmap: root.mipmap
            cache: false
            onStatusChanged: {
                if (attemptToken && (status === Image.Ready || status === Image.Error))
                    root.slotResult(attemptSlot, attemptToken, status === Image.Ready)
            }
        }
    }
    Loader {
        id: slot0
        visible: root.frontSlot === 0
        active: false
        sourceComponent: chartImage
        x: root.imageX
        y: root.imageY
        width: root.imageWidth
        height: root.imageHeight
        onLoaded: {
            item.attemptSlot = 0
            item.attemptToken = root.slot0Token
            item.source = root.slot0Source
        }
    }
    Loader {
        id: slot1
        visible: root.frontSlot === 1
        active: false
        sourceComponent: chartImage
        x: root.imageX
        y: root.imageY
        width: root.imageWidth
        height: root.imageHeight
        onLoaded: {
            item.attemptSlot = 1
            item.attemptToken = root.slot1Token
            item.source = root.slot1Source
        }
    }
    // The chart layer's own loading state, in the chart layer's own scene:
    // the weather's state is never shown for the chart.
    Rectangle {
        id: status
        objectName: "chartStatus"
        visible: root.chartLoading || root.chartUnavailable
        x: 8
        y: 34
        width: statusText.implicitWidth + 12
        height: statusText.implicitHeight + 8
        color: "#000000be"
        Text {
            id: statusText
            anchors.centerIn: parent
            color: "white"
            font.pixelSize: 12
            text: root.chartUnavailable ? "Chart unavailable" : "Chart loading…"
        }
    }
}
