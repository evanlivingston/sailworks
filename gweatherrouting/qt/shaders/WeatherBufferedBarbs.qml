import QtQuick

// Keep one complete native portrayal visible while the next one is prepared.
Item {
    WeatherBarbs { objectName: "weatherBarbs"; anchors.fill: parent }
    WeatherBarbs { objectName: "weatherBarbsBack"; anchors.fill: parent; visible: false }
    WeatherStreamlines { objectName: "weatherStreamlines"; anchors.fill: parent; visible: false }
}
