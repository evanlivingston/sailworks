import QtQuick
import SailworksRouting 1.0

Item {
    width: 320
    height: 240

    MouseArea {
        objectName: "underlay"
        anchors.fill: parent
        hoverEnabled: true
        property int presses: 0
        property int hovers: 0
        onPressed: presses += 1
        onPositionChanged: if (containsMouse) hovers += 1
    }

    RoutingRetainedItem {
        objectName: "retained"
        anchors.fill: parent
    }
}
