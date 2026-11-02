import QtQuick

// The viewport's chart layer: the composed chart image.
//
// The image is published by the chart layer's own generation.  A superseded
// generation never reaches this item: the layer publishes only the
// generation that is still current.
Item {
    id: root
    property alias source: image.source
    property bool ready: image.status === Image.Ready
    property string sourceError: image.status === Image.Error ? "chart image unavailable" : ""
    // The chart layer's own state: it never changes the weather's state.
    property bool chartLoading: false
    // The composed image covers the camera's window with the application's
    // own margin, so it is placed by the offset between the composed
    // centre and the current centre.
    property real imageX: 0
    property real imageY: 0
    property real imageWidth: 0
    property real imageHeight: 0
    Image {
        id: image
        visible: true
        smooth: false
        cache: false
        x: root.imageX
        y: root.imageY
        width: root.imageWidth
        height: root.imageHeight
    }
    // The chart layer's own loading state, in the chart layer's own scene:
    // the weather's state is never shown for the chart.
    Rectangle {
        id: status
        objectName: "chartStatus"
        visible: root.chartLoading
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
            text: "Chart loading…"
        }
    }
}
