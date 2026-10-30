import QtQuick

Item {
    id: root
    property bool fillEnabled: false
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
    // Keep drawing the prior textures while replacement sources load. `ready`
    // still reports only the current inputs, so Python does not mark a new
    // forecast ready until every replacement has arrived.
    function canDisplay(image) {
        return image.status === Image.Ready
                || (image.status === Image.Loading && image.hadReady)
    }
    readonly property bool displayable: (nativeFields ? nativeReady
                         : canDisplay(imageU0) && canDisplay(imageV0)
                         && canDisplay(imageU1) && canDisplay(imageV1))
                         && canDisplay(imageX) && canDisplay(imageY)
                         && (!root.nodeIdIndirection || canDisplay(imageId))
    // Bound each mesh, not the selection: all-point selections may contain
    // arbitrarily many coincident symbols within the resident field budget.
    property int batchRows: Math.max(1, Math.floor(8192 / columns))
    property int batchRevision: 0
    property bool ready: imagesReady && batchesReady()
    property bool prepared: imagesReady && batchesPrepared()
    property string shaderError: batchError()
    WeatherFill {
        objectName: "weatherFill"
        anchors.fill: parent
        visible: root.fillEnabled
    }
    function batchesReady() {
        let revision = batchRevision
        for (let i = 0; i < batches.count; ++i) {
            let item = batches.itemAt(i)
            if (!item || item.status === ShaderEffect.Error
                || (!nativeFields && item.status !== ShaderEffect.Compiled)) return false
        }
        return batches.count > 0 && (!nativeFields || nativeFrameObserved)
    }
    function batchesPrepared() {
        let revision = batchRevision
        if (batches.count === 0) return false
        for (let i = 0; i < batches.count; ++i) {
            let item = batches.itemAt(i)
            if (!item || item.status === ShaderEffect.Error
                || (!nativeFields && item.status !== ShaderEffect.Compiled)) return false
        }
        return true
    }
    function batchError() {
        let revision = batchRevision
        for (let i = 0; i < batches.count; ++i) {
            let item = batches.itemAt(i)
            if (item && item.status === ShaderEffect.Error) return item.log
        }
        return ""
    }
    Image {
        id: imageU0; objectName: "fieldU0"; visible: false; smooth: false; cache: false
        retainWhileLoading: true; property bool hadReady: false
        onStatusChanged: if (status === Image.Ready) hadReady = true
    }
    Image {
        id: imageV0; objectName: "fieldV0"; visible: false; smooth: false; cache: false
        retainWhileLoading: true; property bool hadReady: false
        onStatusChanged: if (status === Image.Ready) hadReady = true
    }
    Image {
        id: imageU1; objectName: "fieldU1"; visible: false; smooth: false; cache: false
        retainWhileLoading: true; property bool hadReady: false
        onStatusChanged: if (status === Image.Ready) hadReady = true
    }
    Image {
        id: imageV1; objectName: "fieldV1"; visible: false; smooth: false; cache: false
        retainWhileLoading: true; property bool hadReady: false
        onStatusChanged: if (status === Image.Ready) hadReady = true
    }
    Image {
        id: imageX; objectName: "selectionX"; visible: false; smooth: false; cache: false
        retainWhileLoading: true; property bool hadReady: false
        onStatusChanged: if (status === Image.Ready) hadReady = true
    }
    Image {
        id: imageY; objectName: "selectionY"; visible: false; smooth: false; cache: false
        retainWhileLoading: true; property bool hadReady: false
        onStatusChanged: if (status === Image.Ready) hadReady = true
    }
    Image {
        id: imageId; objectName: "selectionId"; visible: false; smooth: false; cache: false
        retainWhileLoading: true; property bool hadReady: false
        onStatusChanged: if (status === Image.Ready) hadReady = true
    }
    Repeater {
        id: batches
        model: Math.ceil(root.rows / root.batchRows)
        onItemAdded: root.batchRevision++
        onItemRemoved: root.batchRevision++
        ShaderEffect {
        required property int index
        onStatusChanged: root.batchRevision++
        anchors.fill: parent
        visible: root.displayable
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
        property int rowOffset: index * root.batchRows
        property int meshRows: Math.min(root.batchRows, root.rows - rowOffset)
        mesh: GridMesh { resolution: Qt.size(root.columns * 2 - 1, meshRows * 2 - 1) }
        vertexShader: "barbs.vert.qsb"
        fragmentShader: "barbs.frag.qsb"
    }
    }
}
