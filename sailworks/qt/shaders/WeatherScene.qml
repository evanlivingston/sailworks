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
    // The wave height fill. A scalar field travels in u0/u1 with a zero plane
    // in v0/v1, so it reuses WeatherFill and the weather shader's bilinear
    // unchanged. All four image slots are bound (u, zero, u1, zero) because
    // WeatherFill's readiness contract is "all four images ready"; that is
    // the layout the scalar staging already produces.
    WeatherFill {
        id: waveFill
        objectName: "waveFill"
        anchors.fill: parent
        visible: false
        gradientLevels: 9
        fragmentShader: Qt.resolvedUrl("wave.frag.qsb")
        property string statusText: ""
        property bool waveLoading: false
    }
    // The pending wave frame loads beside the one on screen and is swapped in
    // only after the publication manager adopts it, so a slow publish never
    // presents a stale or half-loaded field.
    WeatherFill {
        id: waveFillBack
        objectName: "waveFillBack"
        anchors.fill: parent
        visible: false
        gradientLevels: 9
        fragmentShader: Qt.resolvedUrl("wave.frag.qsb")
        property string statusText: ""
        property bool waveLoading: false
    }
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
    // Shared node-anchor pool: one provider per atlas slot that both barbs
    // sample, so anchor publications are retained once. Hidden, unsized and
    // declared before the barbs, so nothing is reparented and stacking is
    // unchanged. Sources stay empty until the viewport binds them.
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
