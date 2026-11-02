import QtQuick

// Keep one complete native portrayal visible while the next one is prepared.
Item {
    id: root
    property bool fillEnabled: false
    WeatherFill {
        objectName: "weatherFill"
        anchors.fill: parent
        visible: root.fillEnabled
    }
    WeatherFill {
        objectName: "weatherFillBack"
        anchors.fill: parent
        visible: false
    }
    WeatherBarbs { objectName: "weatherBarbs"; anchors.fill: parent }
    WeatherBarbs { objectName: "weatherBarbsBack"; anchors.fill: parent; visible: false }
    WeatherStreamlines { objectName: "weatherStreamlines"; anchors.fill: parent; visible: false }
}
