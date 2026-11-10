import QtQuick
import SailworksRouting 1.0

// Retained native routing geometry. Loaded only when the Qt runtime can
// provide the C++ item, so the image-backed canvas overlay stays the fallback.
RoutingRetainedItem {
    objectName: "retainedRoutingOverlay"
    anchors.fill: parent
}
