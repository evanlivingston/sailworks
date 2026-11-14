import QtQuick

// The viewport's chart layer: the composed chart image.
//
// The image is published by the chart layer's own generation.  A superseded
// generation never reaches this item: the layer publishes only the
// generation that is still current.
Item {
    id: root
    property alias source: root.slot0Source
    property url slot0Source
    property url slot1Source
    property string slot0Token: ""
    property string slot1Token: ""
    property int frontSlot: 0
    // Chart composition is pixel-exact at native scale; a raster preview
    // (satellite) opts into smooth scaling while it is being resized.
    property bool smooth: false
    // Photographic rasters (satellite) are scaled during a zoom, so they need
    // mipmaps to avoid aliasing and shimmer; the vector-ish layers stay
    // nearest-neighbour deliberately.
    property bool mipmap: false
    // A ready-to-ready swap fades in over this duration. The viewport sets it
    // only when the previous image was current and the replacement was
    // adopted, so a loading state is never faded in as if it were chart data.
    property int fadeMs: 0
    // The outgoing slot stays visible until its own fade completes, so a
    // ready-to-ready swap is a crossfade rather than a blank frame between
    // the old and the new image.  The requested duration is reset once the
    // swap has finished, so a later non-ready update never inherits it.
    Timer {
        id: fadeReset
        interval: root.fadeMs
        repeat: false
        onTriggered: {
            root.fadeMs = 0
            // Free the dissolved frame now that its fade is over, unless the
            // slot has already been given the next composition to load.
            if (slot0Source.toString() === "" && root.frontSlot !== 0)
                slot0.active = false
            if (slot1Source.toString() === "" && root.frontSlot !== 1)
                slot1.active = false
        }
    }
    onFrontSlotChanged: {
        if (root.fadeMs > 0)
            fadeReset.restart()
    }
    signal slotResult(int slot, string token, bool success)
    property int frontStatus: frontSlot === 0 ? (slot0.item ? slot0.item.status : Image.Null) : (slot1.item ? slot1.item.status : Image.Null)
    property bool ready: frontStatus === Image.Ready
    property string sourceError: frontStatus === Image.Error ? "chart image unavailable" : ""
    // The chart layer's own state: it never changes the weather's state.
    property bool chartLoading: false
    property bool chartUnavailable: false
    // The composed image covers the camera's window with the application's
    // own margin, so it is placed by the offset between the composed
    // centre and the current centre.
    property real imageX: 0
    property real imageY: 0
    property real imageWidth: 0
    property real imageHeight: 0
    // Natural pixel size of the published raster. Pinning sourceSize to it stops
    // Qt re-requesting the image provider whenever the placement geometry changes
    // (which happens on every camera change during a zoom).
    property int imagePixelWidth: 0
    property int imagePixelHeight: 0
    onSlot0SourceChanged: {
        // An outgoing image keeps its texture while it fades out: clearing
        // the source must not delete the frame the crossfade is dissolving.
        // The front slot is not fading out, so a withdrawal there still
        // removes the image immediately.
        if (
            slot0Source.toString() === ""
            && slot0.opacity > 0
            && root.frontSlot !== 0
        )
            return
        slot0.active = false
        slot0.active = slot0Source.toString() !== ""
    }
    onSlot1SourceChanged: {
        if (
            slot1Source.toString() === ""
            && slot1.opacity > 0
            && root.frontSlot !== 1
        )
            return
        slot1.active = false
        slot1.active = slot1Source.toString() !== ""
    }
    // A token names the publication the viewport is waiting for on this slot.
    // It is written before the source, and a same-generation redelivery can
    // reuse the same source URL, which the Loader will not reload on its own.
    // Defer one turn: by then the source of this publication is known.  If the
    // item already holds that composition, report it under the new token;
    // otherwise the pending load reports itself when it becomes ready.  The
    // item's own source is checked so a frame from a previous publication is
    // never adopted as the token's.
    function reportSlot0() {
        if (!slot0.item || root.slot0Token === "")
            return
        if (slot0.item.source.toString() !== root.slot0Source.toString())
            return
        if (
            slot0.item.status !== Image.Ready
            && slot0.item.status !== Image.Error
        )
            return
        if (
            slot0.item.emittedToken !== root.slot0Token
            || slot0.item.emittedStatus !== slot0.item.status
        ) {
            slot0.item.emittedToken = root.slot0Token
            slot0.item.emittedStatus = slot0.item.status
            root.slotResult(
                0,
                root.slot0Token,
                slot0.item.status === Image.Ready
            )
        }
    }
    function reportSlot1() {
        if (!slot1.item || root.slot1Token === "")
            return
        if (slot1.item.source.toString() !== root.slot1Source.toString())
            return
        if (
            slot1.item.status !== Image.Ready
            && slot1.item.status !== Image.Error
        )
            return
        if (
            slot1.item.emittedToken !== root.slot1Token
            || slot1.item.emittedStatus !== slot1.item.status
        ) {
            slot1.item.emittedToken = root.slot1Token
            slot1.item.emittedStatus = slot1.item.status
            root.slotResult(
                1,
                root.slot1Token,
                slot1.item.status === Image.Ready
            )
        }
    }
    onSlot0TokenChanged: {
        if (slot0.item)
            slot0.item.attemptToken = root.slot0Token
        Qt.callLater(root.reportSlot0)
    }
    onSlot1TokenChanged: {
        if (slot1.item)
            slot1.item.attemptToken = root.slot1Token
        Qt.callLater(root.reportSlot1)
    }
    Component {
        id: chartImage
        Image {
            property string attemptToken: ""
            property string emittedToken: ""
            property int emittedStatus: Image.Null
            property int attemptSlot: -1
            smooth: root.smooth
            mipmap: root.mipmap
            cache: false
            sourceSize: root.imagePixelWidth > 0 && root.imagePixelHeight > 0
                        ? Qt.size(root.imagePixelWidth, root.imagePixelHeight)
                        : Qt.size(0, 0)
            onStatusChanged: {
                if (attemptToken && (status === Image.Ready || status === Image.Error)) {
                    emittedToken = attemptToken
                    emittedStatus = status
                    root.slotResult(attemptSlot, attemptToken, status === Image.Ready)
                }
            }
        }
    }
    Loader {
        id: slot0
        objectName: "chartSlot0"
        visible: root.frontSlot === 0 || opacity > 0
        opacity: root.frontSlot === 0 ? 1 : 0
        Behavior on opacity {
            NumberAnimation { duration: root.fadeMs; easing.type: Easing.InOutQuad }
        }
        active: false
        sourceComponent: chartImage
        x: root.imageX
        y: root.imageY
        width: root.imageWidth
        height: root.imageHeight
        onLoaded: {
            item.attemptSlot = 0
            item.attemptToken = root.slot0Token
            item.source = root.slot0Source
        }
    }
    Loader {
        id: slot1
        objectName: "chartSlot1"
        visible: root.frontSlot === 1 || opacity > 0
        opacity: root.frontSlot === 1 ? 1 : 0
        Behavior on opacity {
            NumberAnimation { duration: root.fadeMs; easing.type: Easing.InOutQuad }
        }
        active: false
        sourceComponent: chartImage
        x: root.imageX
        y: root.imageY
        width: root.imageWidth
        height: root.imageHeight
        onLoaded: {
            item.attemptSlot = 1
            item.attemptToken = root.slot1Token
            item.source = root.slot1Source
        }
    }
    // The chart layer's own loading state, in the chart layer's own scene:
    // the weather's state is never shown for the chart.
    Rectangle {
        id: status
        objectName: "chartStatus"
        visible: root.chartLoading || root.chartUnavailable
        x: 8
        y: 34
        width: statusText.implicitWidth + 12
        height: statusText.implicitHeight + 8
        color: "#000000be"
        Text {
            id: statusText
            anchors.centerIn: parent
            color: "white"
            font.pixelSize: 12
            text: root.chartUnavailable ? "Chart unavailable" : "Chart loading…"
        }
    }
}
