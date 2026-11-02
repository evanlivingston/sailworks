import QtQuick

// The viewport's weather scene: the numerical fill behind the barbs.
//
// The barbs are the application's own component, used unchanged; the fill is
// a separate layer with its own inputs, so the two are updated independently.
// The scene is the viewport's own: the application's default path never loads
// it.
Item {
    id: root
    property bool fillEnabled: false
    WeatherFill {
        id: fill
        objectName: "weatherFill"
        anchors.fill: parent
        visible: root.fillEnabled
        // Byte-identical slot shaders need separate URLs: Qt can retain an
        // Uncompiled status for a second effect sharing the cached URL.
        fragmentShader: Qt.resolvedUrl("weather-fill-front.frag.qsb")
    }
    WeatherFill {
        objectName: "weatherFillBack"
        anchors.fill: parent
        visible: false
        fragmentShader: Qt.resolvedUrl("weather-fill-back.frag.qsb")
    }
    WeatherBarbs {
        id: barbs
        objectName: "weatherBarbs"
        anchors.fill: parent
    }
    WeatherBarbs {
        objectName: "weatherBarbsBack"
        anchors.fill: parent
        visible: false
    }
    WeatherStreamlines { objectName: "weatherStreamlines"; anchors.fill: parent; visible: false }
    // Stationary clip container for synthetic markers and future explicit
    // migration. Opt-in only: nothing is reparented here yet, so existing
    // geographic layers and overlays keep their parents and z.
    Item {
        objectName: "mapClip"
        anchors.fill: root
        clip: true
        Item {
            objectName: "mapContent"
            x: 0
            y: 0
            width: parent.width
            height: parent.height
        }
    }
}
