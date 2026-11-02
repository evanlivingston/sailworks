import QtQuick

// Numerical field component, not yet a replacement for map wind symbology.
Item {
    id: root
    property alias u0Source: imageU0.source
    property alias v0Source: imageV0.source
    property alias u1Source: imageU1.source
    property alias v1Source: imageV1.source
    property real blend: 0
    property real outputMode: 0
    property bool ready: imageU0.status === Image.Ready && imageV0.status === Image.Ready
                         && imageU1.status === Image.Ready && imageV1.status === Image.Ready

    Image { id: imageU0; visible: false; smooth: false; cache: false }
    Image { id: imageV0; visible: false; smooth: false; cache: false }
    Image { id: imageU1; visible: false; smooth: false; cache: false }
    Image { id: imageV1; visible: false; smooth: false; cache: false }
    ShaderEffect {
        anchors.fill: parent
        visible: root.ready
        property variant u0: imageU0
        property variant v0: imageV0
        property variant u1: imageU1
        property variant v1: imageV1
        property real blend: root.blend
        property real outputMode: root.outputMode
        fragmentShader: "weather.frag.qsb"
    }
}
