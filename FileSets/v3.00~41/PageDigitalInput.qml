//// modified for ExtTransferSwitch package

import QtQuick 1.1
import com.victron.velib 1.0
import "utils.js" as Utils

MbPage {
	id: root

	property variant service
	property string bindPrefix
	property string settingsBindPreffix: "com.victronenergy.settings/Settings/DigitalInput/" + inputNumber
	property int inputNumber: instance.valid ? instance.value : 0

	title: getType(service.description)
	summary: getState(state.item.value)

	VBusItem {
		id: instance
		bind: service.path("/DeviceInstance")
	}

	// Handle translations
	function getType(type){
		switch (type) {
		case "Disabled":
			return qsTr("Disabled")
		case "Pulse meter":
			return qsTr("Pulse meter")
		case "Door alarm":
			return qsTr("Door alarm")
		case "Bilge pump":
			return qsTr("Bilge pump")
		case "Bilge alarm":
			return qsTr("Bilge alarm")
		case "Disabled":
			return qsTr("Burglar alarm")
		case "Smoke alarm":
			return qsTr("Smoke alarm")
		case "Fire alarm":
			return qsTr("Fire alarm")
		case "CO2 alarm":
			return qsTr("CO2 alarm")
		case "Generator":
			return qsTr("Generator")
//// added for ExtTransferSwitch package
		case "TransferSwitch":
			return qsTr("External transfer switch")
		}
		return type;
	}

	function getState(st)
	{
		switch (st) {
		case 0:
			return qsTr("Low")
		case 1:
			return qsTr("High")
		case 2:
			return qsTr("Off")
		case 3:
			return qsTr("On")
		case 4:
			return qsTr("No")
		case 5:
			return qsTr("Yes")
		case 6:
			return qsTr("Open")
		case 7:
			return qsTr("Closed")
		case 8:
			return qsTr("Ok")
		case 9:
			return qsTr("Alarm")
		case 10:
			return qsTr("Running")
		case 11:
			return qsTr("Stopped")
//// added for ExtTransferSwitch package
		case 12:
			return qsTr("On generator")
		case 13:
			return qsTr("On grid")
		}

		return qsTr("Unknown")
	}

	model: VisibleItemModel {
		MbItemValue {
			id: state
			description: qsTr("State")
			item.bind: service.path("/State")
			item.text: getState(item.value)
		}

		MbSubMenu {
			id: setupMenu
			description: qsTr("Setup")
			subpage: Component {
				PageDigitalInputSetup {
					bindPrefix: root.settingsBindPreffix
				}
			}
		}

		MbSubMenu {
			id: deviceMenu
			description: qsTr("Device")
			subpage: Component {
				PageDeviceInfo {
					title: deviceMenu.description
					bindPrefix: root.bindPrefix
				}
			}
		}
	}
}
