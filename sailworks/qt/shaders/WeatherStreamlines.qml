import QtQuick

// Standalone projected streamline renderer. Each packed texture row describes
// one independent segment; batches cap the vertex mesh at 8192 segments.
Item {
    id: root
    clip: true
    // Each buffer owns a stable Repeater. Swapping only changes visibility,
    // preserving the already-loaded textures and compiled shader delegates.
    property var buffer0Groups: []
    property var buffer1Groups: []
    property int frontBuffer: -1
    property int pendingBuffer: -1
    property var groups: [] // compatibility input for the standalone QML test
    onGroupsChanged: {
        if (frontBuffer < 0 && groups.length) {
            buffer0Groups = groups
            frontBuffer = 0
        } else if (!groups.length && frontBuffer === 0 && pendingBuffer < 0) {
            buffer0Groups = []
            frontBuffer = -1
        }
    }
    property vector2d cameraCenter: Qt.vector2d(0.5, 0.5)
    property real cameraZoom: 0
    property int batchSize: 8192
    readonly property int effectiveBatchSize: Math.max(1, Math.min(8192, batchSize))
    property real lineWidth: 1.2
    property int batchRevision: 0
    readonly property bool pendingReady: {
        let revision = batchRevision
        let batches = pendingBuffer === 0 ? buffer0 : buffer1
        if (pendingBuffer < 0 || batches.count === 0) return false
        for (var i = 0; i < batches.count; ++i) {
            var delegate = batches.itemAt(i)
            if (!delegate || !delegate.ready)
                return false
        }
        return true
    }
    readonly property bool ready: {
        let revision = batchRevision
        let batches = frontBuffer === 0 ? buffer0 : buffer1
        if (frontBuffer < 0 || batches.count === 0) return false
        for (var i = 0; i < batches.count; ++i) {
            var delegate = batches.itemAt(i)
            if (!delegate || !delegate.ready) return false
        }
        return true
    }
    readonly property string pendingError: {
        let revision = batchRevision
        let batches = pendingBuffer === 0 ? buffer0 : buffer1
        for (var i = 0; i < batches.count; ++i) {
            var delegate = batches.itemAt(i)
            if (delegate && delegate.shaderError.length) return delegate.shaderError
        }
        return ""
    }
    readonly property string shaderError: {
        let revision = batchRevision
        let batches = frontBuffer === 0 ? buffer0 : buffer1
        for (var i = 0; i < batches.count; ++i) {
            var delegate = batches.itemAt(i)
            if (delegate && delegate.shaderError.length)
                return delegate.shaderError
        }
        return ""
    }
    readonly property string batchDiagnostics: {
        let revision = batchRevision
        let details = []
        let batches = pendingBuffer === 0 ? buffer0 : buffer1
        for (var i = 0; i < batches.count; ++i) {
            var delegate = batches.itemAt(i)
            if (!delegate) {
                details.push(i + ": delegate unavailable")
                continue
            }
            details.push(i + ": image=" + delegate.imageStatus
                         + ", shader=" + delegate.effectStatus
                         + ", batch=" + delegate.width + "x" + delegate.height
                         + " visible=" + delegate.visible
                         + ", effect=" + delegate.effectWidth + "x" + delegate.effectHeight
                          + " visible=" + delegate.effectVisible
                          + ", image=" + delegate.imageWidth + "x" + delegate.imageHeight
                          + " visible=" + delegate.imageVisible
                         + ", parent=" + delegate.parentWidth + "x" + delegate.parentHeight
                         + ", mesh=" + delegate.meshResolution
                         + ", segments=" + delegate.segmentCount
                         + ", arrow=" + delegate.arrow
                         + ", error=" + delegate.shaderError)
        }
        return "batches=" + batches.count + " [" + details.join("; ") + "]"
    }

    // RGB888, three texels per segment row (never uses alpha as data).
    Item {
        anchors.fill: parent
        Item {
            id: buffer0Layer
            anchors.fill: parent
            visible: root.frontBuffer === 0 || root.pendingBuffer === 0
            // Keep the pending buffer visibly rendering so Qt compiles its
            // shader before the atomic front/back swap. A near-zero opacity
            // made the new forecast effectively invisible on the map.
            opacity: root.frontBuffer === 0 ? 1 : 0.35
            Repeater {
                id: buffer0
                anchors.fill: parent
                model: root.buffer0Groups
                onItemAdded: root.batchRevision++
                onItemRemoved: root.batchRevision++
                delegate: batchDelegate
            }
        }
        Item {
            id: buffer1Layer
            anchors.fill: parent
            visible: root.frontBuffer === 1 || root.pendingBuffer === 1
            opacity: root.frontBuffer === 1 ? 1 : 0.35
            Repeater {
                id: buffer1
                anchors.fill: parent
                model: root.buffer1Groups
                onItemAdded: root.batchRevision++
                onItemRemoved: root.batchRevision++
                delegate: batchDelegate
            }
        }
        Component {
            id: batchDelegate
            Item {
                id: batch
                objectName: "streamlineBatch"
                // A Python list of maps is a JS-array model: its roles are on
                // modelData, not required properties of the delegate itself.
                required property var modelData
                property url segmentSource: modelData.source
                property int segmentCount: modelData.count
                 property int imageStatus: segments.status
                 property int effectStatus: effect.status
                 property real effectWidth: effect.width
                 property real effectHeight: effect.height
                 property bool effectVisible: effect.visible
                 property real imageWidth: segments.width
                 property real imageHeight: segments.height
                 property bool imageVisible: segments.visible
                 property real parentWidth: parent ? parent.width : 0
                 property real parentHeight: parent ? parent.height : 0
                 property size meshResolution: Qt.size(1, Math.max(1, segmentCount * (arrow ? 4 : 2) - 1))
                 property color segmentColor: modelData.color
                 property bool arrow: !!modelData.arrow
                property bool ready: segments.status === Image.Ready && effect.status === ShaderEffect.Compiled
                property string shaderError: segments.status === Image.Error ? "Streamline texture unavailable"
                                             : effect.status === ShaderEffect.Error ? effect.log : ""
                onReadyChanged: root.batchRevision++
                onShaderErrorChanged: root.batchRevision++
                anchors.fill: parent
                Image { id: segments; source: batch.segmentSource; visible: false; smooth: false; cache: false }
                ShaderEffect {
                    id: effect
                    anchors.fill: parent
                    // Do not gate scene-graph participation on `Compiled`:
                    // hidden effects are not compiled, which stalls the
                    // pending-buffer swap after its first batch.
                    visible: segments.status === Image.Ready
                    property variant segmentTexture: segments
                    property int firstSegment: 0
                    property int segmentCount: batch.segmentCount
                    property real lineWidth: root.lineWidth
                    property vector2d viewportSize: Qt.vector2d(width, height)
                    property color lineColor: batch.segmentColor
                    property vector2d cameraCenter: root.cameraCenter
                    property real cameraZoom: root.cameraZoom
                    property int arrowMode: batch.arrow ? 1 : 0
                    vertexShader: "streamlines.vert.qsb"
                    fragmentShader: "streamlines.frag.qsb"
                     mesh: GridMesh { resolution: batch.meshResolution }
                }
            }
        }
    }
}
