import QtQuick

// The viewport's scene with the chart layer.
//
// The chart layer is the application's chart composition, published as one
// image.  It has its own state and its own generation: the weather layers
// are published before the chart is ready, and adding the chart resets
// neither the weather nor the camera nor the forecast time.
//
// The scene is the viewport's own: the application's default path never
// loads it.
Item {
    id: root
    property bool fillEnabled: false
    ChartLayer {
        id: chart
        objectName: "chartLayer"
        anchors.fill: parent
    }
    WeatherFill {
        id: fill
        objectName: "weatherFill"
        anchors.fill: parent
        visible: root.fillEnabled
    }
    WeatherBarbs {
        id: barbs
        objectName: "weatherBarbs"
        anchors.fill: parent
    }
}
