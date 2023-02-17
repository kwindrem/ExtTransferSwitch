#!/usr/bin/env python

# This program integrates an external transfer switch ahead of the single AC input
# of a MultiPlus inverter/charger.
#
# This package should not be used on Quattros since they have an internal transfer swich.
#
# A new type of digital input is defined to provide select grid or generator input profiles
#
# Only one AC input exists in a MultiPlus and the data for that input must be switched between
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

import platform
import argparse
import logging
import sys
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
		try:
			obj = self.theBus.get_object (dbusSystemPath, '/VebusService')
			vebusService = obj.GetText ()
		except:
			if self.dbusOk:
				logging.info ("Multi disappeared - /VebusService invalid")
			self.veBusService = ""
			self.dbusOk = False

		if vebusService == "---":
			if self.veBusService != "":
				logging.info ("Multi disappeared")
			self.veBusService = ""
			self.dbusOk = False
		elif self.veBusService == "" or vebusService != self.veBusService:
			logging.info ("discovered Multi at " + vebusService)
			self.veBusService = vebusService
			try:
				self.currentLimitObj = self.theBus.get_object (vebusService, "/Ac/ActiveIn/CurrentLimit")
				self.currentLimitIsAdjustableObj = self.theBus.get_object (vebusService, "/Ac/ActiveIn/CurrentLimitIsAdjustable")
			except:
				logging.error ("current limit dbus setup failed - changes can't be made")
				self.dbusOK = False

			try:
				if self.acInputTypeObj == None:
					self.acInputTypeObj = self.theBus.get_object (dbusSettingsPath, "/Settings/SystemSetup/AcInput1")
				self.dbusOk = True
			except:
				self.dbusOk = False
				logging.error ("AC input dbus setup failed - changes can't be made")

			# set up objects for stop when AC 1 available
			#	there's one for "Generator" and one for "FischerPanda"
			#	ignore errors if these aren't present
			try:
				if self.stopWhenAc1AvailableObj == None:
					self.stopWhenAc1AvailableObj = self.theBus.get_object (dbusSettingsPath, "/Settings/Generator0/StopWhenAc1Available")
			except:
				self.stopWhenAc1AvailableObj = None
			try:
				if self.stopWhenAc1AvailableFpObj == None:
					self.stopWhenAc1AvailableFpObj = self.theBus.get_object (dbusSettingsPath, "/Settings/FischerPanda0/StopWhenAc1Available")
			except:
				self.stopWhenAc1AvailableFpObj = None


	def updateTransferSwitchState (self):
		try:
			# current digital input is no longer valid
			# search for a new one only every 10 seconds to avoid unnecessary processing
			if (self.digitalInputTypeObj == None or self.digitalInputTypeObj.GetValue() != 11) and self.tsInputSearchDelay > 10:
				newInputService = ""
				for service in self.theBus.list_names():
					# found a digital input service, now check the type
					if service.startswith ("com.victronenergy.digitalinput"):
						self.digitalInputTypeObj = self.theBus.get_object (service, '/Type')
						# found it!
						if self.digitalInputTypeObj.GetValue() == 11:
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
					if self.transferSwitchStateObj.GetValue () == 12:
						self.onGenerator = True
					else:
						self.onGenerator = False
					self.transferSwitchActive = True
				except:
					self.transferSwitchActive = False
			else:
				self.transferSwitchActive = False

		except:
			logging.info ("TransferSwitch digital input no longer valid")
			self.digitalInputTypeObj = None
			self.transferSwitchStateObj = None
			return False


	def transferToGrid (self):
		if self.dbusOk:
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
				if self.stopWhenAc1AvailableObj != None:
					self.stopWhenAc1AvailableObj.SetValue (self.DbusSettings['stopWhenAc1Avaiable'])
				if self.stopWhenAc1AvailableFpObj != None:
					self.stopWhenAc1AvailableFpObj.SetValue (self.DbusSettings['stopWhenAc1AvaiableFp'])
			except:
				logging.error ("stopWhenAc1Available update failed when switching to grid")

	def transferToGenerator (self):
		if self.dbusOk:
			# save current values for restore when switching back to grid
			try:
				self.DbusSettings['gridCurrentLimit'] = self.currentLimitObj.GetValue ()
				self.DbusSettings['gridInputType'] = self.acInputTypeObj.GetValue ()
				if self.stopWhenAc1AvailableObj != None:
					self.DbusSettings['stopWhenAc1Avaiable'] = self.stopWhenAc1AvailableObj.GetValue ()
				else:
					self.DbusSettings['stopWhenAc1Avaiable'] = 0
				if self.stopWhenAc1AvailableFpObj != None:
					self.DbusSettings['stopWhenAc1AvaiableFp'] = self.stopWhenAc1AvailableFpObj.GetValue ()
				else:
					self.DbusSettings['stopWhenAc1AvaiableFp'] = 0
			except:
				logging.error ("dbus error AC input and stop when AC1 available settings not saved switching to generator")

			try:
				self.acInputTypeObj.SetValue (2)
				if self.currentLimitIsAdjustableObj.GetValue () == 1:
					self.currentLimitObj.SetValue (wrap_dbus_value (self.DbusSettings['generatorCurrentLimit']))
				else:
					logging.warning ("Input current limit not adjustable - not changed")
			except:
				logging.error ("dbus error AC input settings not changed to generator")

			try:
				if self.stopWhenAc1AvailableObj != None:
					self.stopWhenAc1AvailableObj.SetValue (0)
				if self.stopWhenAc1AvailableFpObj != None:
					self.stopWhenAc1AvailableFpObj.SetValue (0)
			except:
				logging.error ("stopWhenAc1Available update failed switching to generator")


	def background (self):

		##startTime = time.time()
 
		self.updateTransferSwitchState ()
		if self.transferSwitchActive:
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
		self.currentLimitObj = None
		self.currentLimitIsAdjustableObj = None
		self.stopWhenAc1AvailableObj = None
		self.stopWhenAc1AvailableFpObj = None

		self.digitalInputTypeObj = None
		self.transferSwitchStateObj = None

		self.lastOnGenerator = None
		self.transferSwitchActive = False
		self.dbusOk = False
		self.tsInputSearchDelay = 99 # allow serch to occur immediately

		# create / attach local settings
		settingsList = {
			'gridCurrentLimit': [ '/Settings/TransferSwitch/GridCurrentLimit', 0.0, 0.0, 0.0 ],
			'generatorCurrentLimit': [ '/Settings/TransferSwitch/GeneratorCurrentLimit', 0.0, 0.0, 0.0 ],
			'gridInputType': [ '/Settings/TransferSwitch/GridType', 0, 0, 0 ],
			'stopWhenAc1Avaiable': [ '/Settings/TransferSwitch/StopWhenAc1Available', 0, 0, 0 ],
			'stopWhenAc1AvaiableFp': [ '/Settings/TransferSwitch/StopWhenAc1AvailableFp', 0, 0, 0 ],
						}
		self.DbusSettings = SettingsDevice(bus=self.theBus, supportedSettings=settingsList,
								timeout = 10, eventCallback=None )

		GLib.timeout_add (1000, self.background)
		return None

def main():

	from dbus.mainloop.glib import DBusGMainLoop

	# set logging level to include info level entries
	logging.basicConfig(level=logging.INFO)

	# Have a mainloop, so we can send/receive asynchronous calls to and from dbus
	DBusGMainLoop(set_as_default=True)

	logging.info (">>>>>>>>>>>>>>>> ExtTransferSwitch starting <<<<<<<<<<<<<<<<") # TODO: add version

	Monitor ()

	mainloop = GLib.MainLoop()
	mainloop.run()

main()
