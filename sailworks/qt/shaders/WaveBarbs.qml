import QtQuick

Item {
    id: root
    // Wave arrow glyphs. The shaft length comes from the wave period, the
    // colour from the significant wave height and the bearing from the wave
    // direction. One head, no feathers: feathers would read as a wind speed.
    // The six fields are height/period/direction at time A (u0..u2) and the
    // same three at time B (u3..u5); `blend` interpolates between them.
    property alias u0Source: imageU0.source
    property alias u1Source: imageU1.source
    property alias u2Source: imageU2.source
    property alias u3Source: imageU3.source
    property alias u4Source: imageU4.source
    property alias u5Source: imageU5.source
    property alias anchorXSource: imageX.source
    property alias anchorYSource: imageY.source
    property alias nodeIdSource: imageId.source
    property bool nativeFields: false
    property bool nativeReady: false
    property bool nativeFrameObserved: false
    property var nativeU0: null
    property var nativeU1: null
    property var nativeU2: null
    property var nativeU3: null
    property var nativeU4: null
    property var nativeU5: null
    property real blend: 0
    property int columns: 1
    property int rows: 1
    // The resident-field path publishes the selection's node ids and reads the
    // field through them.  The historical path indexes the field directly.
    property bool nodeIdIndirection: false
    property int fieldColumns: 1
    // Selection anchors are encoded for the camera that published them. The
    // layer is placed in shared map-content space, so a rebase compensates
    // both the front and pending items here instead of re-uploading anchors.
    property real anchorOffsetX: 0
    property real anchorOffsetY: 0
    // Temporary zoom-only preview of the retained previous frame. The atlas
    // pixels are mapped into the new map reference by scaling the anchor
    // position about the old frame's centre; the symbol quad is not scaled.
    // Identity values are the normal, non-preview placement.
    property real previewScale: 1
    property real previewOffsetX: 0
    property real previewOffsetY: 0
    // False when the retained anchors were encoded for an incompatible frame
    // (a rebase or zoom overtook the publication). The layer must not present
    // stale anchors; it waits for the frame to be republished.
    property bool anchorFrameValid: true
    // Shared node-anchor pool, as in WeatherBarbs.qml: one publication can
    // feed the wind and wave layers instead of each retaining a copy.
    property var sharedAnchorX0: null
    property var sharedAnchorY0: null
    property var sharedAnchorX1: null
    property var sharedAnchorY1: null
    property int atlasSlot: 0
    // A property written from Python as None may arrive as `undefined` rather
    // than `null`, so both empty values mean "not supplied".
    function anchorSupplied(value) {
        return value !== null && value !== undefined
    }
    readonly property var selectedAnchorX: atlasSlot === 1 ? sharedAnchorX1 : sharedAnchorX0
    readonly property var selectedAnchorY: atlasSlot === 1 ? sharedAnchorY1 : sharedAnchorY0
    readonly property bool externalAnchors: anchorSupplied(sharedAnchorX0)
                                            || anchorSupplied(sharedAnchorY0)
                                            || anchorSupplied(sharedAnchorX1)
                                            || anchorSupplied(sharedAnchorY1)
    readonly property bool sharedPairComplete: anchorSupplied(selectedAnchorX)
                                               && anchorSupplied(selectedAnchorY)
    readonly property bool sharedAnchors: externalAnchors
    readonly property bool anchorsReady: externalAnchors
                         ? sharedPairComplete
                           && selectedAnchorX.status === Image.Ready
                           && selectedAnchorY.status === Image.Ready
                         : imageX.status === Image.Ready && imageY.status === Image.Ready
    readonly property bool anchorsDisplayable: externalAnchors
                         ? sharedPairComplete
                           && canDisplay(selectedAnchorX) && canDisplay(selectedAnchorY)
                         : canDisplay(imageX) && canDisplay(imageY)
    property bool imagesReady: (nativeFields ? nativeReady
                         : imageU0.status === Image.Ready && imageU1.status === Image.Ready
                         && imageU2.status === Image.Ready && imageU3.status === Image.Ready
                         && imageU4.status === Image.Ready && imageU5.status === Image.Ready)
                         && anchorsReady
                         && (!root.nodeIdIndirection || imageId.status === Image.Ready)
    // Keep drawing the prior textures while replacement sources load. `ready`
    // still reports only the current inputs, so Python does not mark a new
    // forecast ready until every replacement has arrived.
    function canDisplay(image) {
        return image.status === Image.Ready
                || (image.status === Image.Loading && image.hadReady)
    }
    readonly property bool displayable: anchorFrameValid
                         && (nativeFields ? nativeReady
                         : canDisplay(imageU0) && canDisplay(imageU1)
                         && canDisplay(imageU2) && canDisplay(imageU3)
                         && canDisplay(imageU4) && canDisplay(imageU5))
                         && anchorsDisplayable
                         && (!root.nodeIdIndirection || canDisplay(imageId))
    // Bound each mesh, not the selection: all-point selections may contain
    // arbitrarily many coincident symbols within the resident field budget.
    property int batchRows: Math.max(1, Math.floor(8192 / columns))
    property int batchRevision: 0
    property bool ready: anchorFrameValid && imagesReady && batchesReady()
    property bool prepared: anchorFrameValid && imagesReady && batchesPrepared()
    property string shaderError: batchError()
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
        id: imageU0; objectName: "waveHeight0"; visible: false; smooth: false; cache: false
        retainWhileLoading: true; property bool hadReady: false
        onStatusChanged: if (status === Image.Ready) hadReady = true
    }
    Image {
        id: imageU1; objectName: "wavePeriod0"; visible: false; smooth: false; cache: false
        retainWhileLoading: true; property bool hadReady: false
        onStatusChanged: if (status === Image.Ready) hadReady = true
    }
    Image {
        id: imageU2; objectName: "waveDirection0"; visible: false; smooth: false; cache: false
        retainWhileLoading: true; property bool hadReady: false
        onStatusChanged: if (status === Image.Ready) hadReady = true
    }
    Image {
        id: imageU3; objectName: "waveHeight1"; visible: false; smooth: false; cache: false
        retainWhileLoading: true; property bool hadReady: false
        onStatusChanged: if (status === Image.Ready) hadReady = true
    }
    Image {
        id: imageU4; objectName: "wavePeriod1"; visible: false; smooth: false; cache: false
        retainWhileLoading: true; property bool hadReady: false
        onStatusChanged: if (status === Image.Ready) hadReady = true
    }
    Image {
        id: imageU5; objectName: "waveDirection1"; visible: false; smooth: false; cache: false
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
        property variant u1: root.nativeFields ? root.nativeU1 : imageU1
        property variant u2: root.nativeFields ? root.nativeU2 : imageU2
        property variant u3: root.nativeFields ? root.nativeU3 : imageU3
        property variant u4: root.nativeFields ? root.nativeU4 : imageU4
        property variant u5: root.nativeFields ? root.nativeU5 : imageU5
        property variant anchorX: root.externalAnchors ? root.selectedAnchorX : imageX
        property variant anchorY: root.externalAnchors ? root.selectedAnchorY : imageY
        property variant nodeId: imageId
        property real blend: root.blend
        property int nodeIdIndirection: root.nodeIdIndirection ? 1 : 0
        property int fieldColumns: root.fieldColumns
        property int rowOffset: index * root.batchRows
        property int meshRows: Math.min(root.batchRows, root.rows - rowOffset)
        property real anchorOffsetX: root.anchorOffsetX
        property real anchorOffsetY: root.anchorOffsetY
        property real previewScale: root.previewScale
        property real previewOffsetX: root.previewOffsetX
        property real previewOffsetY: root.previewOffsetY
        mesh: GridMesh { resolution: Qt.size(root.columns * 2 - 1, meshRows * 2 - 1) }
        vertexShader: "wavebarbs.vert.qsb"
        fragmentShader: "wavebarbs.frag.qsb"
    }
    }
}
