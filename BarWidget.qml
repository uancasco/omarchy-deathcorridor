import QtQuick
import Quickshell
import qs.Commons
import qs.Ui

// A bar button that opens (or raises) deathcorridor, the terminal task manager shipped in ./bin.
BarWidget {
  id: root
  moduleName: "io.github.uancasco.deathcorridor"

  readonly property string launcher: Qt.resolvedUrl("bin/deathcorridor-open").toString().replace("file://", "")

  function openManager() {
    if (root.bar) root.bar.run("'" + root.launcher + "'")
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: ""
    slotSize: Style.bar.statusSlot
    fontSize: Style.font.caption
    tooltipText: "Task manager"
    onPressed: root.openManager()
  }
}
