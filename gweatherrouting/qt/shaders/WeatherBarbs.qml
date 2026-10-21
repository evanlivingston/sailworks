import QtQuick

Item {
    id: root
    property alias u0Source: imageU0.source
    property alias v0Source: imageV0.source
    property alias u1Source: imageU1.source
    property alias v1Source: imageV1.source
    property alias anchorXSource: imageX.source
    property alias anchorYSource: imageY.source
    property alias nodeIdSource: imageId.source
    property bool nativeFields: false
    property bool nativeReady: false
    property bool nativeFrameObserved: false
    property var nativeU0: null
    property var nativeV0: null
    property var nativeU1: null
    property var nativeV1: null
    property real blend: 0
    property int columns: 1
    property int rows: 1
    // The resident-field path publishes the selection's node ids and reads the
    // field through them.  The historical path indexes the field directly.
    property bool nodeIdIndirection: false
    property int fieldColumns: 1
    property bool imagesReady: (nativeFields ? nativeReady
                         : imageU0.status === Image.Ready && imageV0.status === Image.Ready
                         && imageU1.status === Image.Ready && imageV1.status === Image.Ready)
                         && imageX.status === Image.Ready && imageY.status === Image.Ready
                         && (!root.nodeIdIndirection || imageId.status === Image.Ready)
    property bool ready: imagesReady && (nativeFields
                         ? nativeFrameObserved && effect.status !== ShaderEffect.Error
                         : effect.status === ShaderEffect.Compiled)
    property string shaderError: effect.status === ShaderEffect.Error ? effect.log : ""
    Image { id: imageU0; objectName: "fieldU0"; visible: false; smooth: false; cache: false }
    Image { id: imageV0; objectName: "fieldV0"; visible: false; smooth: false; cache: false }
    Image { id: imageU1; objectName: "fieldU1"; visible: false; smooth: false; cache: false }
    Image { id: imageV1; objectName: "fieldV1"; visible: false; smooth: false; cache: false }
    Image { id: imageX; objectName: "selectionX"; visible: false; smooth: false; cache: false }
    Image { id: imageY; objectName: "selectionY"; visible: false; smooth: false; cache: false }
    Image { id: imageId; objectName: "selectionId"; visible: false; smooth: false; cache: false }
    ShaderEffect {
        id: effect
        anchors.fill: parent
        visible: root.imagesReady
        property variant u0: root.nativeFields ? root.nativeU0 : imageU0
        property variant v0: root.nativeFields ? root.nativeV0 : imageV0
        property variant u1: root.nativeFields ? root.nativeU1 : imageU1
        property variant v1: root.nativeFields ? root.nativeV1 : imageV1
        property variant anchorX: imageX
        property variant anchorY: imageY
        property variant nodeId: imageId
        property real blend: root.blend
        property int nodeIdIndirection: root.nodeIdIndirection ? 1 : 0
        property int fieldColumns: root.fieldColumns
        mesh: GridMesh { resolution: Qt.size(root.columns * 2 - 1, root.rows * 2 - 1) }
        vertexShader: "barbs.vert.qsb"
        fragmentShader: "barbs.frag.qsb"
    }
}
