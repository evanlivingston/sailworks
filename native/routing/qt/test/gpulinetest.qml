import QtQuick
import SailworksRouting 1.0

// Standalone scene for the constant-width GPU line item. The harness drives
// the camera and the segment list from C++, so this file only hosts the item.
Rectangle {
    width: 400
    height: 300
    color: "white"

    GpuLineItem {
        objectName: "gpuLine"
        anchors.fill: parent
    }
}
