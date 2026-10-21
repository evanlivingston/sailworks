import QtQuick

// The viewport's numerical weather fill.
//
// The layer runs the corrected weather shader (weather.frag) unchanged: the
// same byte-packed decoder, the same U/V-then-speed arithmetic (U and V are
// interpolated first and the wind speed is derived from them afterwards) and
// the same A0k missing-data policy -- a missing sample contributes exactly
// zero alpha, never the field minimum and never a neighbour's value.
//
// The layer is a sibling of the barbs, not a part of it: it has its own image
// sources, its own uniforms and its own publication counters.  A selection
// change never restages it, and a fill-only change never restages the barbs.
//
// ``fragmentShader`` is the shipped weather shader.  A native probe may point
// it at a byte-exact readback variant of the same arithmetic; the production
// path never does.
Item {
    id: root
    property alias u0Source: imageU0.source
    property alias v0Source: imageV0.source
    property alias u1Source: imageU1.source
    property alias v1Source: imageV1.source
    property bool nativeFields: false
    property bool nativeReady: false
    property bool nativeFrameObserved: false
    property var nativeU0: null
    property var nativeV0: null
    property var nativeU1: null
    property var nativeV1: null
    property real blend: 0
    property real outputMode: 0
    property string fragmentShader: "weather.frag.qsb"
    // The gridded field's rectangle in the scene's pixels.  The shader
    // interpolates its texture linearly across the item it draws on, so the
    // item is the lattice: a fragment's texture coordinate is its position
    // in the viewport's projection, and the 2x2 bilinear is a geographic
    // interpolation of the lattice.
    property real gridX: 0
    property real gridY: 0
    property real gridWidth: 0
    property real gridHeight: 0
    property bool imagesReady: nativeFields ? nativeReady
                         : imageU0.status === Image.Ready && imageV0.status === Image.Ready
                         && imageU1.status === Image.Ready && imageV1.status === Image.Ready
    property bool ready: imagesReady && (nativeFields
                         ? nativeFrameObserved && effect.status !== ShaderEffect.Error
                         : effect.status === ShaderEffect.Compiled)
    property string shaderError: effect.status === ShaderEffect.Error ? effect.log : ""
    Image { id: imageU0; visible: false; smooth: false; cache: false }
    Image { id: imageV0; visible: false; smooth: false; cache: false }
    Image { id: imageU1; visible: false; smooth: false; cache: false }
    Image { id: imageV1; visible: false; smooth: false; cache: false }
    ShaderEffect {
        id: effect
        x: root.gridWidth > 0 ? root.gridX : 0
        y: root.gridWidth > 0 ? root.gridY : 0
        width: root.gridWidth > 0 ? root.gridWidth : root.width
        height: root.gridWidth > 0 ? root.gridHeight : root.height
        visible: root.imagesReady
        property variant u0: root.nativeFields ? root.nativeU0 : imageU0
        property variant v0: root.nativeFields ? root.nativeV0 : imageV0
        property variant u1: root.nativeFields ? root.nativeU1 : imageU1
        property variant v1: root.nativeFields ? root.nativeV1 : imageV1
        property real blend: root.blend
        property real outputMode: root.outputMode
        fragmentShader: root.fragmentShader
    }
}
