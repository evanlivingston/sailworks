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
    }
    WeatherBarbs {
        id: barbs
        objectName: "weatherBarbs"
        anchors.fill: parent
    }
}
