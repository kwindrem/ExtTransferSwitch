#!/usr/bin/python -u
# -*- coding: utf-8 -*-

#### ExtTransferSwitch
#### warm-up and cool-down periods have been modified in order to work well with an external transfer switch
####	selecting grid or generator ahead of a MultiPlus input.
#### Search for #### ExtTransferSwitch to find changes


# Function
# dbus_generator monitors the dbus for batteries (com.victronenergy.battery.*) and
# vebus com.victronenergy.vebus.*
# Battery and vebus monitors can be configured through the gui.
# It then monitors SOC, AC loads, battery current and battery voltage,to auto start/stop the generator based
# on the configuration settings. Generator can be started manually or periodically setting a tes trun period.
# Time zones function allows to use different values for the conditions along the day depending on time

import dbus
import datetime
import calendar
import time
import sys
import json
import os
import logging
from collections import OrderedDict
import monotonic_time
from gen_utils import SettingsPrefix, Errors, States, enum
from gen_utils import create_dbus_service
# Victron packages
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext', 'velib_python'))
from ve_utils import exit_on_error
from settingsdevice import SettingsDevice

RunningConditions = enum(
		Stopped = 0,
		Manual = 1,
		TestRun = 2,
		LossOfCommunication = 3,
		Soc = 4,
		Acload = 5,
		BatteryCurrent = 6,
		BatteryVoltage = 7,
		InverterHighTemp = 8,
		InverterOverload = 9,
		StopOnAc1 = 10,
		StopOnAc2 = 11)

Capabilities = enum(
	WarmupCooldown = 1
)

SYSTEM_SERVICE = 'com.victronenergy.system'
BATTERY_PREFIX = '/Dc/Battery'
HISTORY_DAYS = 30
WAIT_FOR_ENGINE_STOP = 15

def safe_max(args):
	try:
		return max(x for x in args if x is not None)
	except ValueError:
		return None

class Condition(object):
	def __init__(self, parent):
		self.parent = parent
		self.reached = False
		self.start_timer = 0
		self.stop_timer = 0
		self.valid = True
		self.enabled = False
		self.retries = 0

	def __getitem__(self, key):
		try:
			return getattr(self, key)
		except AttributeError:
			raise KeyError(key)

	def __setitem__(self, key, value):
		setattr(self, key, value)

	def get_value(self):
		raise NotImplementedError("get_value")

	@property
	def vebus_service(self):
		return self.parent._vebusservice if self.parent._vebusservice else ''

	@property
	def monitor(self):
		return self.parent._dbusmonitor

class SocCondition(Condition):
	name = 'soc'
	monitoring = 'battery'
	boolean = False
	timed = False

	def get_value(self):
		return self.parent._get_battery().soc

class AcLoadCondition(Condition):
	name = 'acload'
	monitoring = 'vebus'
	boolean = False
	timed = True

	def get_value(self):
		loadOnAcOut = []
		totalConsumption = []

		for phase in ['L1', 'L2', 'L3']:
			# Get the values directly from the inverter, systemcalc doesn't provide raw inverted power
			loadOnAcOut.append(self.monitor.get_value(self.vebus_service, ('/Ac/Out/%s/P' % phase)))

			# Calculate total consumption, '/Ac/Consumption/%s/Power' is deprecated
			c_i = self.monitor.get_value(SYSTEM_SERVICE, ('/Ac/ConsumptionOnInput/%s/Power' % phase))
			c_o = self.monitor.get_value(SYSTEM_SERVICE, ('/Ac/ConsumptionOnOutput/%s/Power' % phase))
			totalConsumption.append(sum(filter(None, (c_i, c_o))))

		# Invalidate if vebus is not available
		if loadOnAcOut[0] == None:
			return None

		# Total consumption
		if self.parent._settings['acloadmeasurement'] == 0:
			return sum(filter(None, totalConsumption))

		# Load on inverter AC out
		if self.parent._settings['acloadmeasurement'] == 1:
			return sum(filter(None, loadOnAcOut))

		# Highest phase load
		if self.parent._settings['acloadmeasurement'] == 2:
			return safe_max(loadOnAcOut)

class BatteryCurrentCondition(Condition):
	name = 'batterycurrent'
	monitoring = 'battery'
	boolean = False
	timed = True

	def get_value(self):
		c = self.parent._get_battery().current
		if c is not None:
			c *= -1
		return c

class BatteryVoltageCondition(Condition):
	name = 'batteryvoltage'
	monitoring = 'battery'
	boolean = False
	timed = True

	def get_value(self):
		return self.parent._get_battery().voltage

class InverterTempCondition(Condition):
	name = 'inverterhightemp'
	monitoring = 'vebus'
	boolean = True
	timed = True

	def get_value(self):
		v = self.monitor.get_value(self.vebus_service,
			'/Alarms/HighTemperature')

		# When multi is connected to CAN-bus, alarms are published to
		# /Alarms/HighTemperature... but when connected to vebus alarms are
		# splitted in three phases and published to /Alarms/LX/HighTemperature...
		if v is None:
			inverterHighTemp = []
			for phase in ['L1', 'L2', 'L3']:
				# Inverter alarms must be fetched directly from the inverter service
				inverterHighTemp.append(self.monitor.get_value(self.vebus_service, ('/Alarms/%s/HighTemperature' % phase)))
			return safe_max(inverterHighTemp)
		return v

class InverterOverloadCondition(Condition):
	name = 'inverteroverload'
	monitoring = 'vebus'
	boolean = True
	timed = True

	def get_value(self):
		v = self.monitor.get_value(self.vebus_service,
			'/Alarms/Overload')

		# When multi is connected to CAN-bus, alarms are published to
		# /Alarms/Overload... but when connected to vebus alarms are
		# splitted in three phases and published to /Alarms/LX/Overload...
		if v is None:
			inverterOverload = []
			for phase in ['L1', 'L2', 'L3']:
				# Inverter alarms must be fetched directly from the inverter service
				inverterOverload.append(self.monitor.get_value(self.vebus_service, ('/Alarms/%s/Overload' % phase)))
			return safe_max(inverterOverload)
		return v

class StopOnAc1Condition(Condition):
	name = 'stoponac1'
	monitoring = 'vebus'
	boolean = True
	timed = False

	def get_value(self):
		# AC input 1
		available = self.monitor.get_value(self.vebus_service,
			'/Ac/State/AcIn1Available')
		if available is None:
			# Not supported in firmware, fall back to old behaviour
			activein = self.monitor.get_value(self.vebus_service,
				'/Ac/ActiveIn/ActiveInput')

			# Active input is connected
			connected = self.monitor.get_value(self.vebus_service,
				'/Ac/ActiveIn/Connected')
			if None not in (activein, connected):
				return activein == 0 and connected == 1
			return None

		return bool(available)

class StopOnAc2Condition(Condition):
	name = 'stoponac2'
	monitoring = 'vebus'
	boolean = True
	timed = False

	def get_value(self):
		# AC input 2 available (used when grid is on AC-in-2)
		available = self.monitor.get_value(self.vebus_service,
			'/Ac/State/AcIn2Available')

		return None if available is None else bool(available)

class Battery(object):
	def __init__(self, monitor, service, prefix):
		self.monitor = monitor
		self.service = service
		self.prefix = prefix

	@property
	def voltage(self):
		return self.monitor.get_value(self.service, self.prefix + '/Voltage')

	@property
	def current(self):
		return self.monitor.get_value(self.service, self.prefix + '/Current')

	@property
	def soc(self):
		# Soc from the device doesn't have the '/Dc/0' prefix like the current and voltage do, but it does
		# have the same prefix on systemcalc
		return self.monitor.get_value(self.service, (BATTERY_PREFIX if self.prefix == BATTERY_PREFIX else '') + '/Soc')

class StartStop(object):
	_driver = None
	def __init__(self, instance):
		logging.info ("ExtTransferSwitch version of startstop.py")
		self._dbusservice = None
		self._settings = None
		self._dbusmonitor = None
		self._remoteservice = None
		self._name = None
		self._enabled = False
		self._instance = instance

		# One second per retry
		self.RETRIES_ON_ERROR = 300
		self._testrun_soc_retries = 0
		self._last_counters_check = 0

#### ExtTransferSwitch warm-up / cool-down
		self._currentTime = 0
		self._warmUpEndTime = 0
		self._coolDownEndTime = 0
		self._postCoolDownEndTime = 0
		self._ac1isIgnored = False
		self._ac2isIgnored = False
		self._activeAcInIsIgnored = False 
		self._acInIsGenerator = False
		self._generatorAcInput = 0

		self._starttime = 0
		self._manualstarttimer = 0
		self._last_runtime_update = 0
		self._timer_runnning = 0

		# The installer left autostart disabled
		self.AUTOSTART_DISABLED_ALARM_TIME = 600
		self._autostart_last_time = self._get_monotonic_seconds()


		# Manual battery service selection is deprecated in favour
		# of getting the values directly from systemcalc, we keep
		# manual selected services handling for compatibility reasons.
		self._vebusservice = None
		self._errorstate = 0
		self._battery_service = None
		self._battery_prefix = None

		self._acpower_inverter_input = {
			'timeout': 0,
			'unabletostart': False
			}

		# Order is important. Conditions are evaluated in the order listed.
		self._condition_stack = OrderedDict({
			SocCondition.name:              SocCondition(self),
			AcLoadCondition.name:           AcLoadCondition(self),
			BatteryCurrentCondition.name:   BatteryCurrentCondition(self),
			BatteryVoltageCondition.name:   BatteryVoltageCondition(self),
			InverterTempCondition.name:     InverterTempCondition(self),
			InverterOverloadCondition.name: InverterOverloadCondition(self),
			StopOnAc1Condition.name:        StopOnAc1Condition(self),
			StopOnAc2Condition.name:        StopOnAc2Condition(self)
		})

	def set_sources(self, dbusmonitor, settings, name, remoteservice):
		self._settings = SettingsPrefix(settings, name)
		self._dbusmonitor = dbusmonitor
		self._remoteservice = remoteservice
		self._name = name

		self.log_info('Start/stop instance created for %s.' % self._remoteservice)
		self._remote_setup()

	def _create_service(self):
		self._dbusservice = self._create_dbus_service()

		# The driver used for this start/stop service
		self._dbusservice.add_path('/Type', value=self._driver)
		# State: None = invalid, 0 = stopped, 1 = running, 2=Warm-up, 3=Cool-down
		self._dbusservice.add_path('/State', value=None, gettextcallback=lambda p, v: States.get_description(v))
		# RunningByConditionCode: Numeric Companion to /RunningByCondition below, but
		# also encompassing a Stopped state.
		self._dbusservice.add_path('/RunningByConditionCode', value=None)
		# Error
		self._dbusservice.add_path('/Error', value=None, gettextcallback=lambda p, v: Errors.get_description(v))
		# Condition that made the generator start
		self._dbusservice.add_path('/RunningByCondition', value=None)
		# Runtime
		self._dbusservice.add_path('/Runtime', value=None, gettextcallback=self._seconds_to_text)
		# Today runtime
		self._dbusservice.add_path('/TodayRuntime', value=None, gettextcallback=self._seconds_to_text)
		# Test run runtime
		self._dbusservice.add_path('/TestRunIntervalRuntime', value=None , gettextcallback=self._seconds_to_text)
		# Next test run date, values is 0 for test run disabled
		self._dbusservice.add_path('/NextTestRun', value=None, gettextcallback=lambda p, v: datetime.datetime.fromtimestamp(v).strftime('%c'))
		# Next test run is needed 1, not needed 0
		self._dbusservice.add_path('/SkipTestRun', value=None)
		# Manual start
		self._dbusservice.add_path('/ManualStart', value=None, writeable=True)
		# Manual start timer
		self._dbusservice.add_path('/ManualStartTimer', value=None, writeable=True)
		# Silent mode active
		self._dbusservice.add_path('/QuietHours', value=None)
		# Alarms
		self._dbusservice.add_path('/Alarms/NoGeneratorAtAcIn', value=None)
		self._dbusservice.add_path('/Alarms/ServiceIntervalExceeded', value=None)
		self._dbusservice.add_path('/Alarms/AutoStartDisabled', value=None)
		# Autostart
		self._dbusservice.add_path('/AutoStartEnabled', value=None, writeable=True, onchangecallback=self._set_autostart)
		# Accumulated runtime
		self._dbusservice.add_path('/AccumulatedRuntime', value=None)
		# Capabilities, where we can add bits
		self._dbusservice.add_path('/Capabilities', value=0)
		# Service countdown, calculated by running time and service interval
		self._dbusservice.add_path('/ServiceCounter', value=None)
		self._dbusservice.add_path('/ServiceCounterReset', value=None, writeable=True, onchangecallback=self._reset_service_counter)
		# Publish what service we're controlling, and the productid
		self._dbusservice.add_path('/GensetService', value=self._remoteservice)
		self._dbusservice.add_path('/GensetProductId',
			value=self._dbusmonitor.get_value(self._remoteservice, '/ProductId'))

		# We need to set the values after creating the paths to trigger the 'onValueChanged' event for the gui
		# otherwise the gui will report the paths as invalid if we remove and recreate the paths without
		# restarting the dbusservice.
		self._dbusservice['/State'] = 0
		self._dbusservice['/RunningByConditionCode'] = RunningConditions.Stopped
		self._dbusservice['/Error'] = 0
		self._dbusservice['/RunningByCondition'] = ''
		self._dbusservice['/Runtime'] = 0
		self._dbusservice['/TodayRuntime'] = 0
		self._dbusservice['/TestRunIntervalRuntime'] = self._interval_runtime(self._settings['testruninterval'])
		self._dbusservice['/NextTestRun'] = None
		self._dbusservice['/SkipTestRun'] = None
		self._dbusservice['/ProductName'] = "Generator start/stop"
		self._dbusservice['/ManualStart'] = 0
		self._dbusservice['/ManualStartTimer'] = 0
		self._dbusservice['/QuietHours'] = 0
		self._dbusservice['/Alarms/NoGeneratorAtAcIn'] = 0
		self._dbusservice['/Alarms/ServiceIntervalExceeded'] = 0
		self._dbusservice['/Alarms/AutoStartDisabled'] = 0
		self._dbusservice['/AutoStartEnabled'] = self._settings['autostart']
		self._dbusservice['/AccumulatedRuntime'] = int(self._settings['accumulatedtotal'])
		self._dbusservice['/ServiceCounter'] = None
		self._dbusservice['/ServiceCounterReset'] = 0

	@property
	def capabilities(self):
		return self._dbusservice['/Capabilities']

	def _set_autostart(self, path, value):
		if 0 <= value <= 1:
			self._settings['autostart'] = int(value)
			return True
		return False

	def enable(self):
		if self._enabled:
			return
		self.log_info('Enabling auto start/stop and taking control of remote switch')
		self._create_service()
		self._determineservices()
		self._update_remote_switch()
		# If cooldown or warmup is enabled, the Quattro may be left in a bad
		# state if there is an unfortunate crash or a reboot. Set the ignore_ac
		# flag to a sane value on startup.
		if self._settings['cooldowntime'] > 0 or \
				self._settings['warmuptime'] > 0:
			self._set_ignore_ac1(False)
			self._set_ignore_ac2(False)
		self._enabled = True

	def disable(self):
		if not self._enabled:
			return
		self.log_info('Disabling auto start/stop, releasing control of remote switch')
		self._remove_service()
		self._enabled = False

	def remove(self):
		self.disable()
		self.log_info('Removed from start/stop instances')

	def _remove_service(self):
		self._dbusservice.__del__()
		self._dbusservice = None

	def device_added(self, dbusservicename, instance):
		self._determineservices()

	def device_removed(self, dbusservicename, instance):
		self._determineservices()

	def get_error(self):
		return self._dbusservice['/Error']

	def set_error(self, errorn):
		self._dbusservice['/Error'] = errorn

	def clear_error(self):
		self._dbusservice['/Error'] = Errors.NONE

	def dbus_value_changed(self, dbusServiceName, dbusPath, options, changes, deviceInstance):
		if self._dbusservice is None:
			return

		# AcIn1Available is needed to determine capabilities, but may
		# only show up later. So we have to wait for it here.
		if self._vebusservice is not None and \
				dbusServiceName == self._vebusservice and \
				dbusPath == '/Ac/State/AcIn1Available':
			self._set_capabilities()

		if dbusServiceName != 'com.victronenergy.system':
			return
		if dbusPath == '/AutoSelectedBatteryMeasurement' and self._settings['batterymeasurement'] == 'default':
			self._determineservices()

		if dbusPath == '/VebusService':
			self._determineservices()

	def handlechangedsetting(self, setting, oldvalue, newvalue):
		if self._dbusservice is None:
			return
		if self._name not in setting:
			# Not our setting
			return

		s = self._settings.removeprefix(setting)

		if s == 'batterymeasurement':
			self._determineservices()
			# Reset retries and valid if service changes
			for condition in self._condition_stack.values():
				if condition['monitoring'] == 'battery':
					condition['valid'] = True
					condition['retries'] = 0

		if s == 'autostart':
			self.log_info('Autostart function %s.' % ('enabled' if newvalue == 1 else 'disabled'))
			self._dbusservice['/AutoStartEnabled'] = self._settings['autostart']

		if self._dbusservice is not None and s == 'testruninterval':
			self._dbusservice['/TestRunIntervalRuntime'] = self._interval_runtime(
															self._settings['testruninterval'])

		if s == 'serviceinterval':
			if newvalue == 0:
				self._dbusservice['/ServiceCounter'] = None
			else:
				self._update_accumulated_time()
		if s == 'lastservicereset':
				self._update_accumulated_time()

	def _reset_service_counter(self, path, value):
		if (path == '/ServiceCounterReset' and value == int(1) and self._dbusservice['/AccumulatedRuntime']):
			self._settings['lastservicereset'] = self._dbusservice['/AccumulatedRuntime']
			self._update_accumulated_time()
			self.log_info('Service counter reset triggered.')

		return True

	def _seconds_to_text(self, path, value):
			m, s = divmod(value, 60)
			h, m = divmod(m, 60)
			return '%dh, %dm, %ds' % (h, m, s)

	def log_info(self, msg):
		logging.info(self._name + ': %s' % msg)

	def tick(self):
		if not self._enabled:
			return

#### ExtTransferSwitch warm-up / cool-down
		# determine which AC input is connected to the generator
		try:
			if self._dbusmonitor.get_value ('com.victronenergy.settings', '/Settings/SystemSetup/AcInput1') == 2:
				self._generatorAcInput = 1
			elif self._dbusmonitor.get_value (SYSTEM_SERVICE, '/Ac/In/NumberOfAcInputs') >= 2 \
					and self._dbusmonitor.get_value ('com.victronenergy.settings', '/Settings/SystemSetup/AcInput2') == 2:
				self._generatorAcInput = 2
			# no generator input found
			else:
				self._generatorAcInput = 0
		except:
			self._generatorAcInput = 0

#### ExtTransferSwitch warm-up / cool-down
		self._currentTime = self._get_monotonic_seconds ()

		self._check_remote_status()
		self._evaluate_startstop_conditions()
		self._evaluate_autostart_disabled_alarm()
		self._detect_generator_at_acinput()
		if self._dbusservice['/ServiceCounterReset'] == 1:
			self._dbusservice['/ServiceCounterReset'] = 0

#### ExtTransferSwitch warm-up / cool-down
		state = self._dbusservice['/State']

		# shed load for active generator input in warm-up and cool-down
		# note that external transfer switch might change the state of on generator
		# so this needs to be checked and load adjusted every pass
		# restore load for sources no longer in use or if state is not in warm-up/cool-down
		# restoring load is delayed 1following end of cool-down
		#	to allow the generator to actually stop producing power
		if state in (States.WARMUP, States.COOLDOWN, States.STOPPING):
			self._ignore_ac (True)
		else:
			self._ignore_ac (False)

		# update cool down end time while running and generator has the load
		# this is done because acInIsGenerator may change by an external transfer switch
		#	and the input type changed by the ExtTransferSwitch service
		if state == States.RUNNING and self._acInIsGenerator:
			self._coolDownEndTime = self._currentTime + self._settings['cooldowntime']
#### end ExtTransferSwitch warm-up / cool-down


	def _evaluate_startstop_conditions(self):
		if self.get_error() != Errors.NONE:
			# First evaluation after an error, log it
			if self._errorstate == 0:
				self._errorstate = 1
				self._dbusservice['/State'] = States.ERROR
				self.log_info('Error: #%i - %s, stop controlling remote.' %
							(self.get_error(),
							Errors.get_description(self.get_error())))
		elif self._errorstate == 1:
			# Error cleared
			self._errorstate = 0
			self.log_info('Error state cleared, taking control of remote switch.')

		start = False
		startbycondition = None
		activecondition = self._dbusservice['/RunningByCondition']
		today = calendar.timegm(datetime.date.today().timetuple())
		self._timer_runnning = False
		connection_lost = False
		running = self._dbusservice['/State'] in (States.RUNNING, States.WARMUP)

		self._check_quiet_hours()

		# New day, register it
		if self._last_counters_check < today and self._dbusservice['/State'] == States.STOPPED:
			self._last_counters_check = today
			self._update_accumulated_time()

		# Update current and accumulated runtime.
		# By performance reasons, accumulated runtime is only updated
		# once per 60s. When the generator stops is also updated.
		if self._dbusservice['/State'] in (States.RUNNING, States.WARMUP, States.COOLDOWN, States.STOPPING):
			mtime = monotonic_time.monotonic_time().to_seconds_double()
			if (mtime - self._starttime) - self._last_runtime_update >= 60:
				self._dbusservice['/Runtime'] = int(mtime - self._starttime)
				self._update_accumulated_time()
			elif self._last_runtime_update == 0:
				self._dbusservice['/Runtime'] = int(mtime - self._starttime)


		if self._evaluate_manual_start():
			startbycondition = 'manual'
			start = True

		# Conditions will only be evaluated if the autostart functionality is enabled
		if self._settings['autostart'] == 1:

			if self._evaluate_testrun_condition():
				startbycondition = 'testrun'
				start = True

			# Evaluate stop on AC IN conditions first, when this conditions are enabled and reached the generator
			# will stop as soon as AC IN in active. Manual and testrun conditions will make the generator start
			# or keep it running.
			stop_on_ac_reached = (self._evaluate_condition(self._condition_stack[StopOnAc1Condition.name]) or
						       self._evaluate_condition(self._condition_stack[StopOnAc2Condition.name]))
			stop_by_ac1_ac2 = startbycondition not in ['manual', 'testrun'] and stop_on_ac_reached

			if stop_by_ac1_ac2 and running and activecondition not in ['manual', 'testrun']:
				self.log_info('AC input available, stopping')

			# Evaluate value conditions
			for condition, data in self._condition_stack.items():
				# Do not evaluate rest of conditions if generator is configured to stop
				# when AC IN is available
				if stop_by_ac1_ac2:
					start = False
					if running:
						self._reset_condition(data)
						continue
					else:
						break

				# Don't short-circuit this, _evaluate_condition sets .reached
				start = self._evaluate_condition(data) or start
				startbycondition = condition if start and startbycondition is None else startbycondition
				# Connection lost is set to true if the number of retries of one or more enabled conditions
				# >= RETRIES_ON_ERROR
				if data.enabled:
					connection_lost = data.retries >= self.RETRIES_ON_ERROR

			# If none condition is reached check if connection is lost and start/keep running the generator
			# depending on '/OnLossCommunication' setting
			if not start and connection_lost:
				# Start always
				if self._settings['onlosscommunication'] == 1:
					start = True
					startbycondition = 'lossofcommunication'
				# Keep running if generator already started
				if running and self._settings['onlosscommunication'] == 2:
					start = True
					startbycondition = 'lossofcommunication'

		if not start and self._errorstate:
			self._stop_generator()

		if self._errorstate:
			return

		if start:
			self._start_generator(startbycondition)
		elif (self._dbusservice['/Runtime'] >= self._settings['minimumruntime'] * 60
			  or activecondition == 'manual'):
			self._stop_generator()

	def _evaluate_autostart_disabled_alarm(self):

		if self._settings['autostart'] == 1 or self._settings['autostartdisabledalarm'] == 0:
			self._autostart_last_time = self._get_monotonic_seconds()
			if self._dbusservice['/Alarms/AutoStartDisabled'] != 0:
				self._dbusservice['/Alarms/AutoStartDisabled'] = 0
			return

		timedisabled = self._get_monotonic_seconds() - self._autostart_last_time
		if timedisabled > self.AUTOSTART_DISABLED_ALARM_TIME and self._dbusservice['/Alarms/AutoStartDisabled'] != 2:
			self.log_info("Autostart was left for more than %i seconds, triggering alarm." % int(timedisabled))
			self._dbusservice['/Alarms/AutoStartDisabled'] = 2


#### ExtTransferSwitch warm-up / cool-down - rewrote so acInIsGenerator is updated even if alarm is disabled
	def _detect_generator_at_acinput(self):
#### ExtTransferSwitch warm-up / cool-down
		self._acInIsGenerator = False	# covers all conditions that result in a return

		state = self._dbusservice['/State']
		if state == States.STOPPED:
			self._reset_acpower_inverter_input()
			return

		vebus_service = self._vebusservice if self._vebusservice else ''
		activein_state = self._dbusmonitor.get_value(
			vebus_service, '/Ac/ActiveIn/Connected')

		# Path not supported, skip evaluation
		if activein_state == None:
			return

		# Sources 0 = Not available, 1 = Grid, 2 = Generator, 3 = Shore
		generator_acsource = self._dbusmonitor.get_value(
			SYSTEM_SERVICE, '/Ac/ActiveIn/Source') == 2
		# Not connected = 0, connected = 1
		activein_connected = activein_state == 1

#### ExtTransferSwitch warm-up / cool-down
		if self._settings['nogeneratoratacinalarm'] == 0:
			processAlarm = False
			self._reset_acpower_inverter_input()
		else:
			processAlarm = True

		if generator_acsource and activein_connected:
#### ExtTransferSwitch warm-up / cool-down
			self._acInIsGenerator = True
#### ExtTransferSwitch warm-up / cool-down
			if processAlarm and self._acpower_inverter_input['unabletostart']:
				self.log_info('Generator detected at inverter AC input, alarm removed')
			self._reset_acpower_inverter_input()
#### ExtTransferSwitch warm-up / cool-down
		elif not processAlarm:
			self._reset_acpower_inverter_input()
			return
		elif self._acpower_inverter_input['timeout'] < self.RETRIES_ON_ERROR:
			self._acpower_inverter_input['timeout'] += 1
		elif not self._acpower_inverter_input['unabletostart']:
			self._acpower_inverter_input['unabletostart'] = True
			self._dbusservice['/Alarms/NoGeneratorAtAcIn'] = 2
			self.log_info('Generator not detected at inverter AC input, triggering alarm')

	def _reset_acpower_inverter_input(self, clear_error=True):
		if self._acpower_inverter_input['timeout'] != 0:
			self._acpower_inverter_input['timeout'] = 0

		if self._acpower_inverter_input['unabletostart'] != 0:
			self._acpower_inverter_input['unabletostart'] = 0

		self._dbusservice['/Alarms/NoGeneratorAtAcIn'] = 0

	def _reset_condition(self, condition):
		condition['reached'] = False
		if condition['timed']:
			condition['start_timer'] = 0
			condition['stop_timer'] = 0

	def _check_condition(self, condition, value):
		name = condition['name']

		if self._settings[name + 'enabled'] == 0:
			if condition['enabled']:
				condition['enabled'] = False
				self.log_info('Disabling (%s) condition' % name)
				condition['retries'] = 0
				condition['valid'] = True
				self._reset_condition(condition)
			return False

		elif not condition['enabled']:
			condition['enabled'] = True
			self.log_info('Enabling (%s) condition' % name)

		if (condition['monitoring'] == 'battery') and (self._settings['batterymeasurement'] == 'nobattery'):
			# If no battery monitor is selected reset the condition
			self._reset_condition(condition)
			return False

		if value is None and condition['valid']:
			if condition['retries'] >= self.RETRIES_ON_ERROR:
				logging.info('Error getting (%s) value, skipping evaluation till get a valid value' % name)
				self._reset_condition(condition)
				self._comunnication_lost = True
				condition['valid'] = False
			else:
				condition['retries'] += 1
				if condition['retries'] == 1 or (condition['retries'] % 10) == 0:
					self.log_info('Error getting (%s) value, retrying(#%i)' % (name, condition['retries']))
			return False

		elif value is not None and not condition['valid']:
			self.log_info('Success getting (%s) value, resuming evaluation' % name)
			condition['valid'] = True
			condition['retries'] = 0

		# Reset retries if value is valid
		if value is not None and condition['retries'] > 0:
			self.log_info('Success getting (%s) value, resuming evaluation' % name)
			condition['retries'] = 0

		return condition['valid']

	def _evaluate_condition(self, condition):
		name = condition['name']
		value = condition.get_value()
		setting = ('qh_' if self._dbusservice['/QuietHours'] == 1 else '') + name
		startvalue = self._settings[setting + 'start'] if not condition['boolean'] else 1
		stopvalue = self._settings[setting + 'stop'] if not condition['boolean'] else 0

		# Check if the condition has to be evaluated
		if not self._check_condition(condition, value):
			# If generator is started by this condition and value is invalid
			# wait till RETRIES_ON_ERROR to skip the condition
			if condition['reached'] and condition['retries'] <= self.RETRIES_ON_ERROR:
				if condition['retries'] > 0:
					return True

			return False

		# As this is a generic evaluation method, we need to know how to compare the values
		# first check if start value should be greater than stop value and then compare
		start_is_greater = startvalue > stopvalue

		# When the condition is already reached only the stop value can set it to False
		start = condition['reached'] or (value >= startvalue if start_is_greater else value <= startvalue)
		stop = value <= stopvalue if start_is_greater else value >= stopvalue

		# Timed conditions must start/stop after the condition has been reached for a minimum
		# time.
		if condition['timed']:
			if not condition['reached'] and start:
				condition['start_timer'] += time.time() if condition['start_timer'] == 0 else 0
				start = time.time() - condition['start_timer'] >= self._settings[name + 'starttimer']
				condition['stop_timer'] *= int(not start)
				self._timer_runnning = True
			else:
				condition['start_timer'] = 0

			if condition['reached'] and stop:
				condition['stop_timer'] += time.time() if condition['stop_timer'] == 0 else 0
				stop = time.time() - condition['stop_timer'] >= self._settings[name + 'stoptimer']
				condition['stop_timer'] *= int(not stop)
				self._timer_runnning = True
			else:
				condition['stop_timer'] = 0

		condition['reached'] = start and not stop
		return condition['reached']

	def _evaluate_manual_start(self):
		if self._dbusservice['/ManualStart'] == 0:
			if self._dbusservice['/RunningByCondition'] == 'manual':
				self._dbusservice['/ManualStartTimer'] = 0
			return False

		start = True
		# If /ManualStartTimer has a value greater than zero will use it to set a stop timer.
		# If no timer is set, the generator will not stop until the user stops it manually.
		# Once started by manual start, each evaluation the timer is decreased
		if self._dbusservice['/ManualStartTimer'] != 0:
			self._manualstarttimer += time.time() if self._manualstarttimer == 0 else 0
			self._dbusservice['/ManualStartTimer'] -= int(time.time()) - int(self._manualstarttimer)
			self._manualstarttimer = time.time()
			start = self._dbusservice['/ManualStartTimer'] > 0
			self._dbusservice['/ManualStart'] = int(start)
			# Reset if timer is finished
			self._manualstarttimer *= int(start)
			self._dbusservice['/ManualStartTimer'] *= int(start)

		return start

	def _evaluate_testrun_condition(self):
		if self._settings['testrunenabled'] == 0:
			self._dbusservice['/SkipTestRun'] = None
			self._dbusservice['/NextTestRun'] = None
			return False

		today = datetime.date.today()
		yesterday = today - datetime.timedelta(days=1) # Should deal well with DST
		now = time.time()
		runtillbatteryfull = self._settings['testruntillbatteryfull'] == 1
		soc = self._condition_stack['soc'].get_value()
		batteryisfull = runtillbatteryfull and soc == 100
		duration = 60 if runtillbatteryfull else self._settings['testrunruntime']

		try:
			startdate = datetime.date.fromtimestamp(self._settings['testrunstartdate'])
			_starttime = time.mktime(yesterday.timetuple()) + self._settings['testrunstarttimer']

			# today might in fact still be yesterday, if this test run started
			# before midnight and finishes after. If `now` still falls in
			# yesterday's window, then by the temporal anthropic principle,
			# which I just made up but loosely states that time must have
			# these properties for observers to exist, it must be yesterday
			# because we are here to observe it.
			if _starttime <= now <= _starttime + duration:
				today = yesterday
				starttime = _starttime
			else:
				starttime = time.mktime(today.timetuple()) + self._settings['testrunstarttimer']
		except ValueError:
			logging.debug('Invalid dates, skipping testrun')
			return False

		# If start date is in the future set as NextTestRun and stop evaluating
		if startdate > today:
			self._dbusservice['/NextTestRun'] = time.mktime(startdate.timetuple())
			return False

		start = False
		# If the accumulated runtime during the tes trun interval is greater than '/TestRunIntervalRuntime'
		# the tes trun must be skipped
		needed = (self._settings['testrunskipruntime'] > self._dbusservice['/TestRunIntervalRuntime']
					  or self._settings['testrunskipruntime'] == 0)
		self._dbusservice['/SkipTestRun'] = int(not needed)

		interval = self._settings['testruninterval']
		stoptime = starttime + duration
		elapseddays = (today - startdate).days
		mod = elapseddays % interval

		start = not bool(mod) and starttime <= now <= stoptime

		if runtillbatteryfull:
			if soc is not None:
				self._testrun_soc_retries = 0
				start = (start or self._dbusservice['/RunningByCondition'] == 'testrun') and not batteryisfull
			elif self._dbusservice['/RunningByCondition'] == 'testrun':
				if self._testrun_soc_retries < self.RETRIES_ON_ERROR:
					self._testrun_soc_retries += 1
					start = True
					if (self._testrun_soc_retries % 10) == 0:
						self.log_info('Test run failed to get SOC value, retrying(#%i)' % self._testrun_soc_retries)
				else:
					self.log_info('Failed to get SOC after %i retries, terminating test run condition' % self._testrun_soc_retries)
					start = False
			else:
				start = False

		if not bool(mod) and (now <= stoptime):
			self._dbusservice['/NextTestRun'] = starttime
		else:
			self._dbusservice['/NextTestRun'] = (time.mktime((today + datetime.timedelta(days=interval - mod)).timetuple()) +
												 self._settings['testrunstarttimer'])
		return start and needed

	def _check_quiet_hours(self):
		active = False
		if self._settings['quiethoursenabled'] == 1:
			# Seconds after today 00:00
			timeinseconds = time.time() - time.mktime(datetime.date.today().timetuple())
			quiethoursstart = self._settings['quiethoursstarttime']
			quiethoursend = self._settings['quiethoursendtime']

			# Check if the current time is between the start time and end time
			if quiethoursstart < quiethoursend:
				active = quiethoursstart <= timeinseconds and timeinseconds < quiethoursend
			else:  # End time is lower than start time, example Start: 21:00, end: 08:00
				active = not (quiethoursend < timeinseconds and timeinseconds < quiethoursstart)

		if self._dbusservice['/QuietHours'] == 0 and active:
			self.log_info('Entering to quiet mode')

		elif self._dbusservice['/QuietHours'] == 1 and not active:
			self.log_info('Leaving quiet mode')

		self._dbusservice['/QuietHours'] = int(active)

		return active

	def _update_accumulated_time(self):
		seconds = self._dbusservice['/Runtime']
		accumulated = seconds - self._last_runtime_update

		self._settings['accumulatedtotal'] = accumulatedtotal = int(self._settings['accumulatedtotal']) + accumulated
		# Using calendar to get timestamp in UTC, not local time
		today_date = str(calendar.timegm(datetime.date.today().timetuple()))

		# If something goes wrong getting the json string create a new one
		try:
			accumulated_days = json.loads(self._settings['accumulateddaily'])
		except ValueError:
			accumulated_days = {today_date: 0}

		if (today_date in accumulated_days):
			accumulated_days[today_date] += accumulated
		else:
			accumulated_days[today_date] = accumulated

		self._last_runtime_update = seconds

		# Keep the historical with a maximum of HISTORY_DAYS
		while len(accumulated_days) > HISTORY_DAYS:
			accumulated_days.pop(min(accumulated_days.keys()), None)

		# Upadate settings
		self._settings['accumulateddaily'] = json.dumps(accumulated_days, sort_keys=True)
		self._dbusservice['/TodayRuntime'] = self._interval_runtime(0)
		self._dbusservice['/TestRunIntervalRuntime'] = self._interval_runtime(self._settings['testruninterval'])
		self._dbusservice['/AccumulatedRuntime'] = accumulatedtotal

		# Service counter
		serviceinterval = self._settings['serviceinterval']
		lastservicereset = self._settings['lastservicereset']
		if serviceinterval > 0:
			servicecountdown = (lastservicereset + serviceinterval) - accumulatedtotal
			self._dbusservice['/ServiceCounter'] = servicecountdown
			if servicecountdown <= 0:
				self._dbusservice['/Alarms/ServiceIntervalExceeded'] = 1
			elif self._dbusservice['/Alarms/ServiceIntervalExceeded'] != 0:
				self._dbusservice['/Alarms/ServiceIntervalExceeded'] = 0



	def _interval_runtime(self, days):
		summ = 0
		try:
			daily_record = json.loads(self._settings['accumulateddaily'])
		except ValueError:
			return 0

		for i in range(days + 1):
			previous_day = calendar.timegm((datetime.date.today() - datetime.timedelta(days=i)).timetuple())
			if str(previous_day) in daily_record.keys():
				summ += daily_record[str(previous_day)] if str(previous_day) in daily_record.keys() else 0

		return summ

	def _get_battery(self):
		if self._settings['batterymeasurement'] == 'default':
			return Battery(self._dbusmonitor, SYSTEM_SERVICE, BATTERY_PREFIX)

		return Battery(self._dbusmonitor,
			self._battery_service if self._battery_service else '',
			self._battery_prefix if self._battery_prefix else '')

	def _set_capabilities(self):
		# Update capabilities
		# The ability to ignore AC1/AC2 came in at the same time as
		# AC availability and is used to detect it here.
		readout_supported = self._dbusmonitor.get_value(self._vebusservice,
			'/Ac/State/AcIn1Available') is not None
		self._dbusservice['/Capabilities'] |= (
			Capabilities.WarmupCooldown if readout_supported else 0)

	def _determineservices(self):
		# batterymeasurement is either 'default' or 'com_victronenergy_battery_288/Dc/0'.
		# In case it is set to default, we use the AutoSelected battery
		# measurement, given by SystemCalc.
		batterymeasurement = None
		newbatteryservice = None
		batteryprefix = ''
		selectedbattery = self._settings['batterymeasurement']
		vebusservice = None

		if selectedbattery == 'default':
			batterymeasurement = 'default'
		elif len(selectedbattery.split('/', 1)) == 2:  # Only very basic sanity checking..
			batterymeasurement = self._settings['batterymeasurement']
		elif selectedbattery == 'nobattery':
			batterymeasurement = None
		else:
			# Exception: unexpected value for batterymeasurement
			pass

		if batterymeasurement and batterymeasurement != 'default':
			batteryprefix = '/' + batterymeasurement.split('/', 1)[1]

		# Get the current battery servicename
		if self._battery_service:
			oldservice = self._battery_service
		else:
			oldservice = None

		if batterymeasurement != 'default':
			battery_instance = int(batterymeasurement.split('_', 3)[3].split('/')[0])
			service_type = None

			if 'vebus' in batterymeasurement:
				service_type = 'vebus'
			elif 'battery' in batterymeasurement:
				service_type = 'battery'

			newbatteryservice = self._get_servicename_by_instance(battery_instance, service_type)
		elif batterymeasurement == 'default':
			newbatteryservice = 'default'

		if newbatteryservice and newbatteryservice != oldservice:
			if selectedbattery == 'default':
				self.log_info('Getting battery values from systemcalc.')
			if selectedbattery == 'nobattery':
				self.log_info('Battery monitoring disabled! Stop evaluating related conditions')
				self._battery_service = None
				self._battery_prefix = None
			self.log_info('Battery service we need (%s) found! Using it for generator start/stop' % batterymeasurement)
			self._battery_service = newbatteryservice
			self._battery_prefix = batteryprefix
		elif not newbatteryservice and newbatteryservice != oldservice:
			self.log_info('Error getting battery service!')
			self._battery_service = newbatteryservice
			self._battery_prefix = batteryprefix

		# Get the default VE.Bus service
		vebusservice = self._dbusmonitor.get_value('com.victronenergy.system', '/VebusService')
		if vebusservice:
			if self._vebusservice != vebusservice:
				self._vebusservice = vebusservice
				self._set_capabilities()
				self.log_info('Vebus service (%s) found! Using it for generator start/stop' % vebusservice)
		else:
			if self._vebusservice is not None:
				self.log_info('Vebus service (%s) dissapeared! Stop evaluating related conditions' % self._vebusservice)
			else:
				self.log_info('Error getting Vebus service!')
			self._vebusservice = None

	def _get_servicename_by_instance(self, instance, service_type=None):
		sv = None
		services = self._dbusmonitor.get_service_list()

		for service in services:
			if service_type and service_type not in service:
				continue

			if services[service] == instance:
				sv = service
				break

		return sv

	def _get_monotonic_seconds(self):
		return monotonic_time.monotonic_time().to_seconds_double()

	def _start_generator(self, condition):
		state = self._dbusservice['/State']
		remote_running = self._get_remote_switch_state()

		# This function will start the generator in the case generator not
		# already running. When differs, the RunningByCondition is updated
		running = state in (States.WARMUP, States.COOLDOWN, States.STOPPING, States.RUNNING)
		if not (running and remote_running): # STOPPED, ERROR
#### ExtTransferSwitch warm-up / cool-down
			self.log_info('Starting generator by %s condition' % condition)
			# if there is a warmup time specified, always go through warm-up state
			#	regardless of AC input in use
			warmUpPeriod = self._settings['warmuptime']
			if warmUpPeriod > 0:
				self._warmUpEndTime = self._currentTime + warmUpPeriod
				self.log_info ("starting warm-up")
				self._dbusservice['/State'] = States.WARMUP
			# no warm-up go directly to running
			else:
				self._dbusservice['/State'] = States.RUNNING
				self._warmUpEndTime = 0

			self._coolDownEndTime = 0
			self._postCoolDownEndTime = 0

			self._update_remote_switch()
			self._starttime = self._currentTime
		else: # WARMUP, COOLDOWN, RUNNING, STOPPING
			if state in (States.COOLDOWN, States.STOPPING):
				# Start request during cool-down run, go back to RUNNING
				self.log_info ("aborting cool-down - returning to running")
				self._dbusservice['/State'] = States.RUNNING

			elif state == States.WARMUP:
				if self._currentTime > self._warmUpEndTime:
					self.log_info ("warm-up complete")
					self._dbusservice['/State'] = States.RUNNING

			# Update the RunningByCondition
			if self._dbusservice['/RunningByCondition'] != condition:
				self.log_info('Generator previously running by %s condition is now running by %s condition'
							% (self._dbusservice['/RunningByCondition'], condition))
#### end ExtTransferSwitch warm-up / cool-down

		self._dbusservice['/RunningByCondition'] = condition
		self._dbusservice['/RunningByConditionCode'] = RunningConditions.lookup(condition)

	def _stop_generator(self):
		state = self._dbusservice['/State']
		remote_running = self._get_remote_switch_state()
		running = state in (States.WARMUP, States.COOLDOWN, States.STOPPING, States.RUNNING)

		if running or remote_running:
#### ExtTransferSwitch warm-up / cool-down
			# run for cool-down period before stopping
			# cooldown end time is updated while generator is running
			#	and generator feeds Multi AC input
			if self._currentTime < self._coolDownEndTime:
				if state != States.COOLDOWN:
					self._dbusservice['/State'] = States.COOLDOWN
					self.log_info ("starting cool-down")
				return

			# When we arrive here, a stop command was given and cool-down period has elapesed
			# Stop the engine, but if we're coming from cooldown, delay another
			# while in the STOPPING state before reactivating AC-in.
			if state == States.COOLDOWN:
				self.log_info ("starting post cool-down")
				# delay restoring load to give generator a chance to stop
				self._postCoolDownEndTime = self._currentTime + WAIT_FOR_ENGINE_STOP
				self._dbusservice['/State'] = States.STOPPING
				self._update_remote_switch() # Stop engine
				self.log_info('Stopping generator that was running by %s condition' %
							str(self._dbusservice['/RunningByCondition']))
				return
				
			# Wait for engine stop
			elif state == States.STOPPING:
				if self._currentTime < self._postCoolDownEndTime:
					return
				else:
					self.log_info ("post cool-down delay complete")

			# All other possibilities are handled now. Cooldown is over or not
			# configured and we waited for the generator to shut down.
			if state != States.STOPPING:
				self._update_remote_switch()
				self.log_info('Stopping generator that was running by %s condition' %
							str(self._dbusservice['/RunningByCondition']))
#### end ExtTransferSwitch warm-up / cool-down
			self._dbusservice['/State'] = States.STOPPED
			self._dbusservice['/RunningByCondition'] = ''
			self._dbusservice['/RunningByConditionCode'] = RunningConditions.Stopped
			self._update_accumulated_time()
			self._starttime = 0
			self._dbusservice['/Runtime'] = 0
			self._dbusservice['/ManualStartTimer'] = 0
			self._manualstarttimer = 0
			self._last_runtime_update = 0

	# This is here so the Multi/Quattro can be told to disconnect AC-in,
	# so that we can do warm-up and cool-down.
#### ExtTransferSwitch warm-up / cool-down
	# there may be two AC inputs (Quattro). process both

	def _ignore_ac (self, state):
			self._activeAcInIsIgnored = state
			state1 = False
			state2 = False
			if self._generatorAcInput == 1:
				state1 = state
			elif self._generatorAcInput == 2:
				state2 = state

			if state1 != self._ac1isIgnored:
				if state1:
					self.log_info ("shedding load - AC input 1")
				else:
					self.log_info ("restoring load - AC input 1")
				self._set_ignore_ac1 (state1)
				self._ac1isIgnored = state1

			if state2 != self._ac2isIgnored:
				if state2:
					self.log_info ("shedding load - AC input 2")
				else:
					self.log_info ("restoring load - AC input 2")
				self._set_ignore_ac2 (state2)
				self._ac2isIgnored = state2


	def _set_ignore_ac1(self, ignore):
		# This is here so the Multi/Quattro can be told to disconnect AC-in,
		# so that we can do warm-up and cool-down.
		if self._vebusservice is not None:
			self._dbusmonitor.set_value_async(self._vebusservice, '/Ac/Control/IgnoreAcIn1', dbus.Int32(ignore, variant_level=1))

	def _set_ignore_ac2(self, ignore):
		if self._vebusservice is not None:
			self._dbusmonitor.set_value_async(self._vebusservice, '/Ac/Control/IgnoreAcIn2', dbus.Int32(ignore, variant_level=1))

	def _update_remote_switch(self):
		# Engine should be started in these states
		v = self._dbusservice['/State'] in (States.RUNNING, States.WARMUP, States.COOLDOWN)
		self._set_remote_switch_state(dbus.Int32(v, variant_level=1))

	def _get_remote_switch_state(self):
		raise Exception('This function should be overridden')

	def _set_remote_switch_state(self, value):
		raise Exception('This function should be overridden')

	# Check the remote status, for example errors
	def _check_remote_status(self):
		raise Exception('This function should be overridden')

	def _remote_setup(self):
		raise Exception('This function should be overridden')

	def _create_dbus_monitor(self, *args, **kwargs):
		raise Exception('This function should be overridden')

	def _create_settings(self, *args, **kwargs):
		raise Exception('This function should be overridden')

	def _create_dbus_service(self):
		return create_dbus_service(self._instance)
