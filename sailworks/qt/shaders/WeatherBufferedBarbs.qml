import QtQuick

// Keep one complete native portrayal visible while the next one is prepared.
Item {
    id: root
    property bool fillEnabled: false
    // This scene has no image fill, so the wave layer cannot be drawn here.
    // An explicit status line takes the place of a silent, empty fill.
    Text {
        objectName: "waveStatus"
        anchors.left: parent.left
        anchors.bottom: parent.bottom
        anchors.margins: 12
        visible: text !== ""
        color: "#e6edf3"
        style: Text.Outline
        styleColor: "#1e2124"
    }
    WeatherFill {
        objectName: "weatherFill"
        anchors.fill: parent
        visible: false
    }
    WeatherFill {
        objectName: "weatherFillBack"
        anchors.fill: parent
        visible: false
    }
    // Wave height is an independent scalar layer; it must exist here too, or
    // "Show wave height" can never draw while the wind style is barbs.
    WeatherFill {
        objectName: "waveFill"
        anchors.fill: parent
        visible: false
        gradientLevels: 9
        fragmentShader: Qt.resolvedUrl("wave.frag.qsb")
        property string statusText: ""
        property bool waveLoading: false
    }
    WeatherFill {
        objectName: "waveFillBack"
        anchors.fill: parent
        visible: false
        gradientLevels: 9
        fragmentShader: Qt.resolvedUrl("wave.frag.qsb")
        property string statusText: ""
        property bool waveLoading: false
    }
    // Shared node-anchor pool: one provider per atlas slot that both barbs
    // sample. Hidden, unsized and declared before the barbs, so the existing
    // parent chain and stacking are unchanged.
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
    WeatherBarbs {
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
    WeatherStreamlines { objectName: "weatherStreamlines"; anchors.fill: parent; visible: false }
}
