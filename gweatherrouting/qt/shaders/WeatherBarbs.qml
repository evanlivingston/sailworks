import QtQuick

Item {
    id: root
    property alias u0Source: imageU0.source
    property alias v0Source: imageV0.source
    property alias u1Source: imageU1.source
    property alias v1Source: imageV1.source
    property alias anchorXSource: imageX.source
    property alias anchorYSource: imageY.source
    property real blend: 0
    property int columns: 1
    property int rows: 1
    property bool imagesReady: imageU0.status === Image.Ready && imageV0.status === Image.Ready
                         && imageU1.status === Image.Ready && imageV1.status === Image.Ready
                         && imageX.status === Image.Ready && imageY.status === Image.Ready
    property bool ready: imagesReady && effect.status === ShaderEffect.Compiled
    property string shaderError: effect.status === ShaderEffect.Error ? effect.log : ""
    Image { id: imageU0; visible: false; smooth: false; cache: false }
    Image { id: imageV0; visible: false; smooth: false; cache: false }
    Image { id: imageU1; visible: false; smooth: false; cache: false }
    Image { id: imageV1; visible: false; smooth: false; cache: false }
    Image { id: imageX; visible: false; smooth: false; cache: false }
    Image { id: imageY; visible: false; smooth: false; cache: false }
    ShaderEffect {
        id: effect
        anchors.fill: parent
        visible: root.imagesReady
        property variant u0: imageU0
        property variant v0: imageV0
        property variant u1: imageU1
        property variant v1: imageV1
        property variant anchorX: imageX
        property variant anchorY: imageY
        property real blend: root.blend
        mesh: GridMesh { resolution: Qt.size(root.columns * 2 - 1, root.rows * 2 - 1) }
        vertexShader: "barbs.vert.qsb"
        fragmentShader: "barbs.frag.qsb"
    }
}
