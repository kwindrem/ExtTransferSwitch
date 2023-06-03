#!/usr/bin/env python

# This program integrates an external transfer switch ahead of the single AC input
# of a MultiPlus or Quattro inverter/charger.
#
# A new type of digital input is defined to provide select grid or generator input profiles
#
# When the external transfer switch changes between grid and generator the data for that input must be switched between
#  grid and generator settings
#
# These two sets of settings are stored in dbus Settings.
# When the transfer switch digital input changes, this program switches
#   the Multiplus settings between these two stored values
# When the user changes the settings, the grid or generator-specific Settings are updated
#
# In order to function, one of the digital inputs must be set to External AC Transfer Switch
# This input should be connected to a contact closure on the external transfer switch to indicate
#	which of it's sources is switched to its output
#
# For Quattro, the /Settings/TransferSwitch/TransferSwitchOnAc2 tells this program where the transfer switch is connected:
#	0 if connected to AC 1 In
#	1 if connected to AC 2 In

import platform
import argparse
import logging
import sys
import subprocess
import os
import time
import dbus

dbusSettingsPath = "com.victronenergy.settings"
dbusSystemPath = "com.victronenergy.system"



# accommodate both Python 2 and 3
# if the Python 3 GLib import fails, import the Python 2 gobject
try:
	from gi.repository import GLib # for Python 3
except ImportError:
	import gobject as GLib # for Python 2

# add the path to our own packages for import
# use an established Victron service to maintain compatiblity
sys.path.insert(1, os.path.join('/opt/victronenergy/dbus-systemcalc-py', 'ext', 'velib_python'))
from vedbus import VeDbusService
from ve_utils import wrap_dbus_value
from settingsdevice import SettingsDevice

class Monitor:

	def getVeBusObjects (self):
		# invalidate all local parameters if transfer switch is not active
		if not self.transferSwitchActive:
			veBusService = ""
			self.dbusOk = False
			self.numberOfAcInputs = 0
			self.stopWhenAcAvailableObj = None
			self.stopWhenAcAvailableFpObj = None
			self.acInputTypeObj = None
			return

		try:
			obj = self.theBus.get_object (dbusSystemPath, '/VebusService')
			vebusService = obj.GetText ()
		except:
			if self.dbusOk:
				logging.info ("Multi/Quattro disappeared - /VebusService invalid")
			veBusService = ""
			self.dbusOk = False
			self.numberOfAcInputs = 0
			self.acInputTypeObj = None

		if vebusService == "---":
			if self.veBusService != "":
				logging.info ("Multi/Quattro disappeared")
			self.veBusService = ""
			self.dbusOk = False
			self.numberOfAcInputs = 0
		elif self.veBusService == "" or vebusService != self.veBusService:
			self.veBusService = vebusService
			try:
				self.numberOfAcInputs = self.theBus.get_object (vebusService, "/Ac/NumberOfAcInputs").GetValue ()
			except:
				self.numberOfAcInputs = 0

			if self.numberOfAcInputs  == 2:
				logging.info ("discovered Quattro at " + vebusService)
			else:
				logging.info ("discovered Multi at " + vebusService)

			try:
				self.currentLimitObj = self.theBus.get_object (vebusService, "/Ac/ActiveIn/CurrentLimit")
				self.currentLimitIsAdjustableObj = self.theBus.get_object (vebusService, "/Ac/ActiveIn/CurrentLimitIsAdjustable")
			except:
				logging.error ("current limit dbus setup failed - changes can't be made")
				self.dbusOk = False

		# check to see where the transfer switch is connected
		if self.numberOfAcInputs == 0:
			transferSwitchLocation = 0
		elif self.numberOfAcInputs == 1:
			transferSwitchLocation = 1
			self.numberOfAcInputs > 1 and self.DbusSettings['transferSwitchOnAc2'] == 1
		elif self.DbusSettings['transferSwitchOnAc2'] == 1:
			transferSwitchLocation = 2
		else:
			transferSwitchLocation = 1
		# if changed, trigger refresh of object pointers
		if transferSwitchLocation != self.transferSwitchLocation:
			logging.info ("Transfer switch is on AC %s in" % transferSwitchLocation)
			self.transferSwitchLocation = transferSwitchLocation
			self.stopWhenAcAvailableObj = None
			self.stopWhenAcAvailableFpObj = None
			try:
				if self.transferSwitchLocation == 2:
					self.acInputTypeObj = self.theBus.get_object (dbusSettingsPath, "/Settings/SystemSetup/AcInput2")
				else:
					self.acInputTypeObj = self.theBus.get_object (dbusSettingsPath, "/Settings/SystemSetup/AcInput1")
				self.dbusOk = True
			except:
				self.dbusOk = False
				logging.error ("AC input dbus setup failed - changes can't be made")

			# set up objects for stop when AC available
			#	there's one for "Generator" and one for "FischerPanda"
			#	ignore errors if these aren't present
			try:
				if self.transferSwitchLocation == 2:
					self.stopWhenAcAvailableObj = self.theBus.get_object (dbusSettingsPath, "/Settings/Generator0/StopWhenAc2Available")
				else:
					self.stopWhenAcAvailableObj = self.theBus.get_object (dbusSettingsPath, "/Settings/Generator0/StopWhenAc1Available")
			except:
				self.stopWhenAcAvailableObj = None
			try:
				if self.transferSwitchLocation == 2:
					self.stopWhenAcAvailableFpObj = self.theBus.get_object (dbusSettingsPath, "/Settings/FischerPanda0/StopWhenAc2Available")
				else:
					self.stopWhenAcAvailableFpObj = self.theBus.get_object (dbusSettingsPath, "/Settings/FischerPanda0/StopWhenAc1Available")
			except:
				self.stopWhenAcAvailableFpObj = None


	def updateTransferSwitchState (self):
		try:
			# current digital input is no longer valid
			# search for a new one only every 10 seconds to avoid unnecessary processing
			if (self.digitalInputTypeObj == None or self.digitalInputTypeObj.GetValue() != self.extTransferDigInputType) and self.tsInputSearchDelay > 10:
				newInputService = ""
				for service in self.theBus.list_names():
					# found a digital input service, now check the type
					if service.startswith ("com.victronenergy.digitalinput"):
						self.digitalInputTypeObj = self.theBus.get_object (service, '/Type')
						# found it!
						if self.digitalInputTypeObj.GetValue() == self.extTransferDigInputType:
							newInputService = service
							break
 
				# found new service - get objects for use later
				if newInputService != "":
					logging.info ("discovered switch digital input service at %s", newInputService)
					self.transferSwitchStateObj = self.theBus.get_object (newInputService, '/State')
				else:
					if self.transferSwitchStateObj != None:
						logging.info ("Transfer switch digital input service NOT found")
					self.digitalInputTypeObj = None
					self.transferSwitchStateObj = None
					self.tsInputSearchDelay = 0 # start delay timer

			# if serch delay timer is active, increment it now
			if self.tsInputSearchDelay <= 10:
				self.tsInputSearchDelay += 1

			if self.transferSwitchStateObj != None:
				try:
					if self.dbusOk and self.transferSwitchStateObj.GetValue () == 12: ## 12 is the on generator value
						self.onGenerator = True
					else:
						self.onGenerator = False
					self.transferSwitchActive = True
				except:
					self.onGenerator = False
					self.transferSwitchActive = False
			else:
				self.onGenerator = False
				self.transferSwitchActive = False

		except:
			logging.info ("TransferSwitch digital input no longer valid")
			self.digitalInputTypeObj = None
			self.transferSwitchStateObj = None
			return False


	def transferToGrid (self):
		if self.dbusOk:
			logging.info ("#### to grid")
			# save current values for restore when switching back to generator
			try:
				self.DbusSettings['generatorCurrentLimit'] = self.currentLimitObj.GetValue ()
			except:
				logging.error ("dbus error AC input settings not saved switching to grid")


			try:
				self.acInputTypeObj.SetValue (self.DbusSettings['gridInputType'])
				if self.currentLimitIsAdjustableObj.GetValue () == 1:
					self.currentLimitObj.SetValue (wrap_dbus_value (self.DbusSettings['gridCurrentLimit']))
				else:
					logging.warning ("Input current limit not adjustable - not changed")
			except:
				logging.error ("dbus error AC input settings not changed to grid")

			try:
				if self.stopWhenAcAvailableObj != None:
					self.stopWhenAcAvailableObj.SetValue (self.DbusSettings['stopWhenAcAvaiable'])
				if self.stopWhenAcAvailableFpObj != None:
					self.stopWhenAcAvailableFpObj.SetValue (self.DbusSettings['stopWhenAcAvaiableFp'])
			except:
				logging.error ("stopWhenAcAvailable update failed when switching to grid")

	def transferToGenerator (self):
		if self.dbusOk:
			logging.info ("#### to generator")
			# save current values for restore when switching back to grid
			try:
				self.DbusSettings['gridCurrentLimit'] = self.currentLimitObj.GetValue ()
				self.DbusSettings['gridInputType'] = self.acInputTypeObj.GetValue ()
				if self.stopWhenAcAvailableObj != None:
					self.DbusSettings['stopWhenAcAvaiable'] = self.stopWhenAcAvailableObj.GetValue ()
				else:
					self.DbusSettings['stopWhenAcAvaiable'] = 0
				if self.stopWhenAcAvailableFpObj != None:
					self.DbusSettings['stopWhenAcAvaiableFp'] = self.stopWhenAcAvailableFpObj.GetValue ()
				else:
					self.DbusSettings['stopWhenAcAvaiableFp'] = 0
			except:
				logging.error ("dbus error AC input and stop when AC available settings not saved when switching to generator")

			try:
				self.acInputTypeObj.SetValue (2)
				if self.currentLimitIsAdjustableObj.GetValue () == 1:
					self.currentLimitObj.SetValue (wrap_dbus_value (self.DbusSettings['generatorCurrentLimit']))
				else:
					logging.warning ("Input current limit not adjustable - not changed")
			except:
				logging.error ("dbus error AC input settings not changed when switching to generator")

			try:
				if self.stopWhenAcAvailableObj != None:
					self.stopWhenAcAvailableObj.SetValue (0)
				if self.stopWhenAcAvailableFpObj != None:
					self.stopWhenAcAvailableFpObj.SetValue (0)
			except:
				logging.error ("stopWhenAcAvailable update failed switching to generator")


	def background (self):

		##startTime = time.time()
 
		self.updateTransferSwitchState ()
		self.getVeBusObjects ()

		# skip processing if any dbus paramters were not initialized properly
		if self.dbusOk and self.transferSwitchActive:

			# process transfer switch state change
			if self.lastOnGenerator != None and self.onGenerator != self.lastOnGenerator:
				if self.onGenerator:
					self.transferToGenerator ()
				else:
					self.transferToGrid ()
			self.lastOnGenerator = self.onGenerator
		elif self.onGenerator:
			self.transferToGrid ()

		##stopTime = time.time()
		##print ("#### background time %0.3f" % (stopTime - startTime))
		return True


	def __init__(self):

		self.theBus = dbus.SystemBus()
		self.onGenerator = False
		self.veBusServiceObj = None
		self.veBusService = ""
		self.lastVeBusService = ""
		self.acInputTypeObj = None
		self.numberOfAcInputs = 0
		self.currentLimitObj = None
		self.currentLimitIsAdjustableObj = None
		self.stopWhenAcAvailableObj = None
		self.stopWhenAcAvailableFpObj = None

		self.digitalInputTypeObj = None
		self.transferSwitchStateObj = None
		self.digInputMaxTypeObj = None
		self.extTransferDigInputType = None

		self.lastOnGenerator = None
		self.transferSwitchActive = False
		self.dbusOk = False
		self.transferSwitchLocation = 0
		self.tsInputSearchDelay = 99 # allow serch to occur immediately

		# create / attach local settings
		settingsList = {
			'gridCurrentLimit': [ '/Settings/TransferSwitch/GridCurrentLimit', 0.0, 0.0, 0.0 ],
			'generatorCurrentLimit': [ '/Settings/TransferSwitch/GeneratorCurrentLimit', 0.0, 0.0, 0.0 ],
			'gridInputType': [ '/Settings/TransferSwitch/GridType', 0, 0, 0 ],
			'stopWhenAcAvaiable': [ '/Settings/TransferSwitch/StopWhenAcAvailable', 0, 0, 0 ],
			'stopWhenAcAvaiableFp': [ '/Settings/TransferSwitch/StopWhenAcAvailableFp', 0, 0, 0 ],
			'transferSwitchOnAc2': [ '/Settings/TransferSwitch/TransferSwitchOnAc2', 0, 0, 0 ],
						}
		self.DbusSettings = SettingsDevice(bus=self.theBus, supportedSettings=settingsList,
								timeout = 10, eventCallback=None )

		# get the maximum digital input type - this will be the type for Exeternal Transfer Switch
		try:
			digInputMaxTypeObj = self.theBus.get_object (dbusSettingsPath, "/Settings/DigitalInput/1/Type")
			if digInputMaxTypeObj != None:
				self.extTransferDigInputType = digInputMaxTypeObj.GetMax ()
		except:
			pass
		logging.info ("Ext Transfer Switch digital input type value: " + str (self.extTransferDigInputType))

		GLib.timeout_add (1000, self.background)
		return None

def main():

	from dbus.mainloop.glib import DBusGMainLoop

	# set logging level to include info level entries
	logging.basicConfig(level=logging.INFO)

	# Have a mainloop, so we can send/receive asynchronous calls to and from dbus
	DBusGMainLoop(set_as_default=True)

	installedVersion = "(no version installed)"
	versionFile = "/etc/venus/installedVersion-ExtTransferSwitch"
	if os.path.exists (versionFile):
		try:
			proc = subprocess.Popen (["cat", versionFile], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
		except:
			pass
		else:
			proc.wait()
			# convert from binary to string
			stdout, stderr = proc.communicate ()
			stdout = stdout.decode ().strip ()
			stderr = stderr.decode ().strip ()
			returnCode = proc.returncode
			if proc.returncode == 0:
				installedVersion = stdout

	logging.info (">>>>>>>>>>>>>>>> ExtTransferSwitch starting " + installedVersion + " <<<<<<<<<<<<<<<<")

	Monitor ()

	mainloop = GLib.MainLoop()
	mainloop.run()

main()
