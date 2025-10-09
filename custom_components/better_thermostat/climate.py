"""Better Thermostat"""

import asyncio
import json
import logging
from abc import ABC
from datetime import datetime, timedelta
from random import randint
from statistics import mean

# preferred for HA time handling (UTC aware)
from homeassistant.util import dt as dt_util
from collections import deque
from typing import Any, Optional

# Home Assistant imports
from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    PRESET_NONE,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_TARGET_TEMP_STEP,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.components.group.util import reduce_attribute
from homeassistant.const import (
    ATTR_TEMPERATURE,
    CONF_NAME,
    EVENT_HOMEASSISTANT_START,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Context, CoreState, ServiceCall, callback
from homeassistant.helpers import entity_platform
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity

# Local imports
from .adapters.delegate import (
    get_current_offset,
    get_max_offset,
    get_min_offset,
    get_offset_step,
    init,
    load_adapter,
)
from .events.cooler import trigger_cooler_change
from .events.temperature import trigger_temperature_change
from .events.trv import trigger_trv_change
from .events.window import trigger_window_change, window_queue
from .model_fixes.model_quirks import load_model_quirks
from .utils.const import (
    ATTR_STATE_BATTERIES,
    ATTR_STATE_CALL_FOR_HEAT,
    ATTR_STATE_ERRORS,
    ATTR_STATE_HEATING_POWER,
    ATTR_STATE_HUMIDIY,
    ATTR_STATE_LAST_CHANGE,
    ATTR_STATE_MAIN_MODE,
    ATTR_STATE_SAVED_TEMPERATURE,
    ATTR_STATE_WINDOW_OPEN,
    BETTERTHERMOSTAT_SET_TEMPERATURE_SCHEMA,
    CONF_COOLER,
    CONF_HEATER,
    CONF_HUMIDITY,
    CONF_MODEL,
    CONF_OFF_TEMPERATURE,
    CONF_OUTDOOR_SENSOR,
    CONF_SENSOR,
    CONF_SENSOR_WINDOW,
    CONF_TARGET_TEMP_STEP,
    CONF_TOLERANCE,
    CONF_WEATHER,
    CONF_WINDOW_TIMEOUT,
    CONF_WINDOW_TIMEOUT_AFTER,
    SERVICE_RESET_HEATING_POWER,
    SERVICE_RESTORE_SAVED_TARGET_TEMPERATURE,
    SERVICE_SET_TEMP_TARGET_TEMPERATURE,
    SUPPORT_FLAGS,
    VERSION,
)
from .utils.controlling import control_queue, control_trv
from .utils.helpers import convert_to_float, find_battery_entity, get_hvac_bt_mode
from .utils.watcher import check_all_entities
from .utils.weather import check_ambient_air_temperature, check_weather


_LOGGER = logging.getLogger(__name__)
DOMAIN = "better_thermostat"


class ContinueLoop(Exception):
    pass


@callback
def async_set_temperature_service_validate(service_call: ServiceCall) -> ServiceCall:
    """Validate temperature inputs for set_temperature service."""
    if ATTR_TEMPERATURE in service_call.data:
        temp = service_call.data[ATTR_TEMPERATURE]
        if not isinstance(temp, (int, float)):
            raise ValueError(f"Invalid temperature value {temp}, must be numeric")

    if ATTR_TARGET_TEMP_HIGH in service_call.data:
        temp_high = service_call.data[ATTR_TARGET_TEMP_HIGH]
        if not isinstance(temp_high, (int, float)):
            raise ValueError(
                f"Invalid target high temperature value {temp_high}, must be numeric"
            )

    if ATTR_TARGET_TEMP_LOW in service_call.data:
        temp_low = service_call.data[ATTR_TARGET_TEMP_LOW]
        if not isinstance(temp_low, (int, float)):
            raise ValueError(
                f"Invalid target low temperature value {temp_low}, must be numeric"
            )

    return service_call


async def async_setup_platform(
    hass, config, async_add_entities, discovery_info=None
):  # noqa: D401
    """(Deprecated) Set up the Better Thermostat platform (no-op)."""
    _LOGGER.debug("better_thermostat: async_setup_platform called (deprecated no-op)")


async def async_setup_entry(hass, entry, async_add_devices):
    """Set up Better Thermostat climate entity for a config entry."""
    _LOGGER.debug(
        "better_thermostat %s: async_setup_entry start (entry_id=%s)",
        entry.data.get(CONF_NAME),
        entry.entry_id,
    )

    async def async_service_handler(entity, call):
        """Handle the service calls."""
        if call.service == SERVICE_RESTORE_SAVED_TARGET_TEMPERATURE:
            await entity.restore_temp_temperature()
        elif call.service == SERVICE_SET_TEMP_TARGET_TEMPERATURE:
            await entity.set_temp_temperature(call.data[ATTR_TEMPERATURE])
        elif call.service == SERVICE_RESET_HEATING_POWER:
            await entity.reset_heating_power()

    platform = entity_platform.async_get_current_platform()
    # Register entity services (validator done manually inside method)
    platform.async_register_entity_service(
        SERVICE_SET_TEMP_TARGET_TEMPERATURE,
        BETTERTHERMOSTAT_SET_TEMPERATURE_SCHEMA,
        "set_temp_temperature",
    )
    platform.async_register_entity_service(
        SERVICE_RESTORE_SAVED_TARGET_TEMPERATURE, {}, "restore_temp_temperature"
    )
    platform.async_register_entity_service(
        SERVICE_RESET_HEATING_POWER, {}, "reset_heating_power"
    )

    async_add_devices(
        [
            BetterThermostat(
                entry.data.get(CONF_NAME),
                entry.data.get(CONF_HEATER),
                entry.data.get(CONF_SENSOR),
                entry.data.get(CONF_HUMIDITY, None),
                entry.data.get(CONF_SENSOR_WINDOW, None),
                entry.data.get(CONF_WINDOW_TIMEOUT, None),
                entry.data.get(CONF_WINDOW_TIMEOUT_AFTER, None),
                entry.data.get(CONF_WEATHER, None),
                entry.data.get(CONF_OUTDOOR_SENSOR, None),
                entry.data.get(CONF_OFF_TEMPERATURE, None),
                entry.data.get(CONF_TOLERANCE, 0.0),
                entry.data.get(CONF_TARGET_TEMP_STEP, "0.0"),
                entry.data.get(CONF_MODEL, None),
                entry.data.get(CONF_COOLER, None),
                hass.config.units.temperature_unit,
                entry.entry_id,
                device_class="better_thermostat",
                state_class="better_thermostat_state",
            )
        ]
    )
    _LOGGER.debug(
        "better_thermostat %s: async_setup_entry finished creating entity",
        entry.data.get(CONF_NAME),
    )


class BetterThermostat(ClimateEntity, RestoreEntity, ABC):
    """Representation of a Better Thermostat device."""

    _attr_has_entity_name = True
    _attr_name = None
    _enable_turn_on_off_backwards_compatibility = False

    async def set_temp_temperature(self, temperature):
        """Set temporary target temperature."""
        if self._saved_temperature is None:
            self._saved_temperature = self.bt_target_temp
            self.bt_target_temp = convert_to_float(
                temperature, self.device_name, "service.set_temp_temperature()"
            )
            self.async_write_ha_state()
            await self.control_queue_task.put(self)
        else:
            self.bt_target_temp = convert_to_float(
                temperature, self.device_name, "service.set_temp_temperature()"
            )
            self.async_write_ha_state()
            await self.control_queue_task.put(self)

    async def restore_temp_temperature(self):
        """Restore the previously saved target temperature."""
        if self._saved_temperature is not None:
            self.bt_target_temp = convert_to_float(
                self._saved_temperature,
                self.device_name,
                "service.restore_temp_temperature()",
            )
            self._saved_temperature = None
            self.async_write_ha_state()
            await self.control_queue_task.put(self)

    async def reset_heating_power(self):
        """Reset heating power to default value."""
        self.heating_power = 0.01
        self.async_write_ha_state()

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self.unique_id)},
            "name": self.device_name,
            "manufacturer": "Better Thermostat",
            "model": self.model,
            "sw_version": VERSION,
        }

    def __init__(
        self,
        name,
        heater_entity_id,
        sensor_entity_id,
        humidity_sensor_entity_id,
        window_id,
        window_delay,
        window_delay_after,
        weather_entity,
        outdoor_sensor,
        off_temperature,
        tolerance,
        target_temp_step,
        model,
        cooler_entity_id,
        unit,
        unique_id,
        device_class,
        state_class,
    ):
        """Initialize the thermostat.

        Parameters
        ----------
        TODO
        """
        self.device_name = name
        self.model = model
        self.real_trvs = {}
        self.entity_ids = []
        self.all_trvs = heater_entity_id
        self.sensor_entity_id = sensor_entity_id
        self.humidity_entity_id = humidity_sensor_entity_id
        self.cooler_entity_id = cooler_entity_id
        self.window_id = window_id or None
        self.window_delay = window_delay or 0
        self.window_delay_after = window_delay_after or 0
        self.weather_entity = weather_entity or None
        self.outdoor_sensor = outdoor_sensor or None
        # Robust off temperature parsing: preserve 0.0 and ignore invalid strings
        self.off_temperature = None
        if off_temperature not in (None, "", "None"):  # allow numeric 0
            try:
                parsed_off = float(off_temperature)
                # Accept any float (including 0.0); reject extreme nonsense
                if -100.0 < parsed_off < 150.0:
                    self.off_temperature = parsed_off
                else:
                    _LOGGER.warning(
                        "better_thermostat %s: off_temperature %.2f outside plausible range, ignoring",
                        self.device_name,
                        parsed_off,
                    )
            except (TypeError, ValueError):  # noqa: BLE001
                _LOGGER.warning(
                    "better_thermostat %s: invalid off_temperature '%s', ignoring",
                    self.device_name,
                    off_temperature,
                )
        # Robust tolerance parsing & sanitizing
        try:
            self.tolerance = float(tolerance) if tolerance is not None else 0.0
        except (TypeError, ValueError):  # noqa: BLE001
            _LOGGER.warning(
                "better_thermostat %s: invalid tolerance '%s', falling back to 0.0",
                self.device_name,
                tolerance,
            )
            self.tolerance = 0.0
        if self.tolerance < 0:
            _LOGGER.warning(
                "better_thermostat %s: negative tolerance '%s' adjusted to 0.0",
                self.device_name,
                self.tolerance,
            )
            self.tolerance = 0.0
        if self.tolerance > 10:
            _LOGGER.warning(
                "better_thermostat %s: unusually high tolerance '%s' (>10) may cause sluggish response",
                self.device_name,
                self.tolerance,
            )
        self._unique_id = unique_id
        self._unit = unit
        self._device_class = device_class
        self._state_class = state_class
        self._hvac_list = [HVACMode.HEAT, HVACMode.OFF]
        self._preset_mode = PRESET_NONE
        self.map_on_hvac_mode = HVACMode.HEAT
        self.next_valve_maintenance = datetime.now() + timedelta(
            hours=randint(1, 24 * 5)
        )
        self.cur_temp = None
        self._current_humidity = 0
        self.window_open = None
        self.bt_target_temp_step = float(target_temp_step) or 0.0
        self.bt_min_temp = 0
        self.bt_max_temp = 30
        self.bt_target_temp = 5.0
        self.bt_target_cooltemp = None
        self._support_flags = SUPPORT_FLAGS | ClimateEntityFeature.PRESET_MODE
        self.bt_hvac_mode = None
        self.closed_window_triggered = False
        self.call_for_heat = True
        self.ignore_states = False
        self.last_dampening_timestamp = None
        self.version = VERSION
        self.last_change = datetime.now() - timedelta(hours=2)
        self.last_external_sensor_change = datetime.now() - timedelta(hours=2)
        self.last_internal_sensor_change = datetime.now() - timedelta(hours=2)
        self._temp_lock = asyncio.Lock()
        self.startup_running = True
        self._saved_temperature = None
        self.last_avg_outdoor_temp = None
        self.last_main_hvac_mode = None
        self.last_window_state = None
        self._last_call_for_heat = None
        self._available = False
        self.context = None
        self.attr_hvac_action = None
        self.old_attr_hvac_action = None
        self.heating_start_temp = None
        self.heating_start_timestamp = None
        self.heating_end_temp = None
        self.heating_end_timestamp = None
        self._async_unsub_state_changed = None
        self.all_entities = []
        self.devices_states = {}
        self.devices_errors = []
        self.control_queue_task = asyncio.Queue(maxsize=1)
        if self.window_id is not None:
            self.window_queue_task = asyncio.Queue(maxsize=1)
        asyncio.create_task(control_queue(self))
        if self.window_id is not None:
            asyncio.create_task(window_queue(self))
        self.heating_power = 0.01
        # Short bounded history of recent heating power evaluations
        self.last_heating_power_stats = deque(maxlen=10)
        self.is_removed = False

    async def async_added_to_hass(self):
        """Run when entity about to be added.

        Returns
        -------
        None
        """
        if isinstance(self.all_trvs, str):
            return _LOGGER.error(
                "You updated from version before 1.0.0-Beta36 of the Better Thermostat integration, you need to remove the BT devices (integration) and add it again."
            )

        if self.cooler_entity_id is not None:
            self._hvac_list.remove(HVACMode.HEAT)
            self._hvac_list.append(HVACMode.HEAT_COOL)
            self.map_on_hvac_mode = HVACMode.HEAT_COOL

        self.entity_ids = [
            entity for trv in self.all_trvs if (entity := trv["trv"]) is not None
        ]

        for trv in self.all_trvs:
            _calibration = 1
            if trv["advanced"]["calibration"] == "local_calibration_based":
                _calibration = 0
            if trv["advanced"]["calibration"] == "hybrid_calibration":
                _calibration = 2
            _adapter = await load_adapter(self, trv["integration"], trv["trv"])
            _model_quirks = await load_model_quirks(self, trv["model"], trv["trv"])
            self.real_trvs[trv["trv"]] = {
                "calibration": _calibration,
                "integration": trv["integration"],
                "adapter": _adapter,
                "model_quirks": _model_quirks,
                "model": trv["model"],
                "advanced": trv["advanced"],
                "ignore_trv_states": False,
                "valve_position": None,
                "valve_position_entity": None,
                "max_temp": None,
                "min_temp": None,
                "target_temp_step": None,
                "temperature": None,
                "current_temperature": None,
                "hvac_modes": None,
                "hvac_mode": None,
                "local_temperature_calibration_entity": None,
                "local_calibration_min": None,
                "local_calibration_max": None,
                "calibration_received": True,
                "target_temp_received": True,
                "system_mode_received": True,
                "last_temperature": None,
                "last_valve_position": None,
                "last_hvac_mode": None,
                "last_current_temperature": None,
                "last_calibration": None,
            }

        def on_remove():
            self.is_removed = True

        self.async_on_remove(on_remove)

        await super().async_added_to_hass()

        _LOGGER.info(
            "better_thermostat %s: Waiting for entity to be ready...", self.device_name
        )

        @callback
        def _async_startup(*_):
            """Init on startup.

            Parameters
            ----------
            _ :
                    All parameters are piped.
            """
            self.context = Context()
            loop = asyncio.get_event_loop()
            loop.create_task(self.startup())

        if self.hass.state == CoreState.running:
            _async_startup()
        else:
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, _async_startup)

    async def _trigger_check_weather(self, event=None):
        _check = await check_all_entities(self)
        if _check is False:
            return
        await check_weather(self)
        if self._last_call_for_heat != self.call_for_heat:
            self._last_call_for_heat = self.call_for_heat
            await self.async_update_ha_state(force_refresh=True)
            self.async_write_ha_state()
            if event is not None:
                await self.control_queue_task.put(self)

    async def _trigger_time(self, event=None):
        _check = await check_all_entities(self)
        if _check is False:
            return
        _LOGGER.debug(
            "better_thermostat %s: get last avg outdoor temps...", self.device_name
        )
        await check_ambient_air_temperature(self)
        self.async_write_ha_state()
        if event is not None:
            await self.control_queue_task.put(self)

    async def _trigger_temperature_change(self, event):
        _check = await check_all_entities(self)
        if _check is False:
            return
        self.async_set_context(event.context)
        if (event.data.get("new_state")) is None:
            return
        self.hass.async_create_task(trigger_temperature_change(self, event))

    async def _trigger_humidity_change(self, event):
        _check = await check_all_entities(self)
        if _check is False:
            return
        self.async_set_context(event.context)
        if (event.data.get("new_state")) is None:
            return
        self._current_humidity = convert_to_float(
            str(self.hass.states.get(self.humidity_entity_id).state),
            self.device_name,
            "humidity_update",
        )
        self.async_write_ha_state()

    async def _trigger_trv_change(self, event):
        _check = await check_all_entities(self)
        if _check is False:
            return
        self.async_set_context(event.context)
        if self._async_unsub_state_changed is None:
            return

        if (event.data.get("new_state")) is None:
            return

        self.hass.async_create_task(trigger_trv_change(self, event))

    async def _trigger_window_change(self, event):
        _check = await check_all_entities(self)
        if _check is False:
            return
        self.async_set_context(event.context)
        if (event.data.get("new_state")) is None:
            return

        self.hass.async_create_task(trigger_window_change(self, event))

    async def _tigger_cooler_change(self, event):
        _check = await check_all_entities(self)
        if _check is False:
            return
        self.async_set_context(event.context)
        if (event.data.get("new_state")) is None:
            return

        self.hass.async_create_task(trigger_cooler_change(self, event))

    async def startup(self):
        """Run when entity about to be added.

        Returns
        -------
        None
        """
        while self.startup_running:
            _LOGGER.info(
                "better_thermostat %s: Starting version %s. Waiting for entity to be ready...",
                self.device_name,
                self.version,
            )

            sensor_state = self.hass.states.get(self.sensor_entity_id)
            if sensor_state is not None:
                if sensor_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
                    _LOGGER.info(
                        "better_thermostat %s: waiting for sensor entity with id '%s' to become fully available...",
                        self.device_name,
                        self.sensor_entity_id,
                    )
                    await asyncio.sleep(10)
                    continue

            try:
                for trv in self.real_trvs.keys():
                    trv_state = self.hass.states.get(trv)
                    if trv_state is None:
                        _LOGGER.info(
                            "better_thermostat %s: waiting for TRV/climate entity with id '%s' to become fully available...",
                            self.device_name,
                            trv,
                        )
                        await asyncio.sleep(10)
                        raise ContinueLoop
                    if trv_state is not None:
                        if trv_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
                            _LOGGER.info(
                                "better_thermostat %s: waiting for TRV/climate entity with id '%s' to become fully available...",
                                self.device_name,
                                trv,
                            )
                            await asyncio.sleep(10)
                            raise ContinueLoop
            except ContinueLoop:
                continue

            if self.window_id is not None:
                if self.hass.states.get(self.window_id).state in (
                    STATE_UNAVAILABLE,
                    STATE_UNKNOWN,
                    None,
                ):
                    _LOGGER.info(
                        "better_thermostat %s: waiting for window sensor entity with id '%s' to become fully available...",
                        self.device_name,
                        self.window_id,
                    )
                    await asyncio.sleep(10)
                    continue

            if self.cooler_entity_id is not None:
                if self.hass.states.get(self.cooler_entity_id).state in (
                    STATE_UNAVAILABLE,
                    STATE_UNKNOWN,
                    None,
                ):
                    _LOGGER.info(
                        "better_thermostat %s: waiting for cooler entity with id '%s' to become fully available...",
                        self.device_name,
                        self.cooler_entity_id,
                    )
                    await asyncio.sleep(10)
                    continue

            if self.humidity_entity_id is not None:
                humidity_state = self.hass.states.get(self.humidity_entity_id)
                if humidity_state is None or humidity_state.state in (
                    STATE_UNAVAILABLE,
                    STATE_UNKNOWN,
                    None,
                ):
                    _LOGGER.info(
                        "better_thermostat %s: waiting for humidity sensor entity with id '%s' to become fully available...",
                        self.device_name,
                        self.humidity_entity_id,
                    )
                    await asyncio.sleep(10)
                    continue

            if self.outdoor_sensor is not None:
                if self.hass.states.get(self.outdoor_sensor).state in (
                    STATE_UNAVAILABLE,
                    STATE_UNKNOWN,
                    None,
                ):
                    _LOGGER.info(
                        "better_thermostat %s: waiting for outdoor sensor entity with id '%s' to become fully available...",
                        self.device_name,
                        self.outdoor_sensor,
                    )
                    await asyncio.sleep(10)
                    continue

            if self.weather_entity is not None:
                if self.hass.states.get(self.weather_entity).state in (
                    STATE_UNAVAILABLE,
                    STATE_UNKNOWN,
                    None,
                ):
                    _LOGGER.info(
                        "better_thermostat %s: waiting for weather entity with id '%s' to become fully available...",
                        self.device_name,
                        self.weather_entity,
                    )
                    await asyncio.sleep(10)
                    continue

            states = [
                state
                for entity_id in self.real_trvs
                if (state := self.hass.states.get(entity_id)) is not None
            ]

            self.bt_min_temp = reduce_attribute(states, ATTR_MIN_TEMP, reduce=max)
            self.bt_max_temp = reduce_attribute(states, ATTR_MAX_TEMP, reduce=min)

            if self.bt_target_temp_step == 0.0:
                self.bt_target_temp_step = reduce_attribute(
                    states, ATTR_TARGET_TEMP_STEP, reduce=max
                )

            self.all_entities.append(self.sensor_entity_id)

            self.cur_temp = convert_to_float(
                str(sensor_state.state), self.device_name, "startup()"
            )
            if self.humidity_entity_id is not None:
                self.all_entities.append(self.humidity_entity_id)
                self._current_humidity = convert_to_float(
                    str(self.hass.states.get(self.humidity_entity_id).state),
                    self.device_name,
                    "startup()",
                )

            if self.cooler_entity_id is not None:
                self.bt_target_cooltemp = convert_to_float(
                    str(
                        self.hass.states.get(self.cooler_entity_id).attributes.get(
                            "temperature"
                        )
                    ),
                    self.device_name,
                    "startup()",
                )

            if self.window_id is not None:
                self.all_entities.append(self.window_id)
                window = self.hass.states.get(self.window_id)

                check = window.state
                if check in ("on", "open", "true"):
                    self.window_open = True
                else:
                    self.window_open = False
                _LOGGER.debug(
                    "better_thermostat %s: detected window state at startup: %s",
                    self.device_name,
                    "Open" if self.window_open else "Closed",
                )
            else:
                self.window_open = False

            # Check If we have an old state
            old_state = await self.async_get_last_state()
            if old_state is not None:
                # If we have no initial temperature, restore
                # If we have a previously saved temperature
                if old_state.attributes.get(ATTR_TEMPERATURE) is None:
                    self.bt_target_temp = reduce_attribute(
                        states, ATTR_TEMPERATURE, reduce=lambda *data: mean(data)
                    )
                    _LOGGER.debug(
                        "better_thermostat %s: Undefined target temperature, falling back to %s",
                        self.device_name,
                        self.bt_target_temp,
                    )
                else:
                    _oldtarget_temperature = float(
                        old_state.attributes.get(ATTR_TEMPERATURE)
                    )
                    # if the saved temperature is lower than the min_temp, set it to min_temp
                    if _oldtarget_temperature < self.bt_min_temp:
                        _LOGGER.warning(
                            "better_thermostat %s: Saved target temperature %s is lower than min_temp %s, setting to min_temp",
                            self.device_name,
                            _oldtarget_temperature,
                            self.bt_min_temp,
                        )
                        _oldtarget_temperature = self.bt_min_temp
                    # if the saved temperature is higher than the max_temp, set it to max_temp
                    elif _oldtarget_temperature > self.bt_max_temp:
                        _LOGGER.warning(
                            "better_thermostat %s: Saved target temperature %s is higher than max_temp %s, setting to max_temp",
                            self.device_name,
                            _oldtarget_temperature,
                            self.bt_min_temp,
                        )
                        _oldtarget_temperature = self.bt_max_temp
                    self.bt_target_temp = convert_to_float(
                        str(_oldtarget_temperature), self.device_name, "startup()"
                    )
                if old_state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
                    self.bt_hvac_mode = old_state.state
                if old_state.attributes.get(ATTR_STATE_CALL_FOR_HEAT, None) is not None:
                    self.call_for_heat = old_state.attributes.get(
                        ATTR_STATE_CALL_FOR_HEAT
                    )
                if (
                    old_state.attributes.get(ATTR_STATE_SAVED_TEMPERATURE, None)
                    is not None
                ):
                    self._saved_temperature = convert_to_float(
                        str(
                            old_state.attributes.get(ATTR_STATE_SAVED_TEMPERATURE, None)
                        ),
                        self.device_name,
                        "startup()",
                    )
                if old_state.attributes.get(ATTR_STATE_HUMIDIY, None) is not None:
                    self._current_humidity = old_state.attributes.get(
                        ATTR_STATE_HUMIDIY
                    )
                if old_state.attributes.get(ATTR_STATE_MAIN_MODE, None) is not None:
                    self.last_main_hvac_mode = old_state.attributes.get(
                        ATTR_STATE_MAIN_MODE
                    )
                if old_state.attributes.get(ATTR_STATE_HEATING_POWER, None) is not None:
                    self.heating_power = float(
                        old_state.attributes.get(ATTR_STATE_HEATING_POWER)
                    )

            else:
                # No previous state, try and restore defaults
                if self.bt_target_temp is None or not isinstance(
                    self.bt_target_temp, float
                ):
                    _LOGGER.info(
                        "better_thermostat %s: No previously saved temperature found on startup, get it from the TRV",
                        self.device_name,
                    )
                    self.bt_target_temp = reduce_attribute(
                        states, ATTR_TEMPERATURE, reduce=lambda *data: mean(data)
                    )

            # if hvac mode could not be restored, turn heat off
            if self.bt_hvac_mode in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
                current_hvac_modes = [
                    x.state for x in states if x.state != HVACMode.OFF
                ]
                # return the most common hvac mode (what the thermostat is set to do) except OFF
                if current_hvac_modes:
                    _temp_bt_hvac_mode = max(
                        set(current_hvac_modes), key=current_hvac_modes.count
                    )
                    if _temp_bt_hvac_mode is not HVACMode.OFF:
                        self.bt_hvac_mode = HVACMode.HEAT
                    else:
                        self.bt_hvac_mode = HVACMode.OFF
                    _LOGGER.debug(
                        "better_thermostat %s: No previously hvac mode found on startup, turn bt to trv mode %s",
                        self.device_name,
                        self.bt_hvac_mode,
                    )
                # return off if all are off
                elif all(x.state == HVACMode.OFF for x in states):
                    self.bt_hvac_mode = HVACMode.OFF
                    _LOGGER.debug(
                        "better_thermostat %s: No previously hvac mode found on startup, turn bt to trv mode %s",
                        self.device_name,
                        self.bt_hvac_mode,
                    )
                else:
                    _LOGGER.warning(
                        "better_thermostat %s: No previously hvac mode found on startup, turn heat off",
                        self.device_name,
                    )
                    self.bt_hvac_mode = HVACMode.OFF

            _LOGGER.debug(
                "better_thermostat %s: Startup config, BT hvac mode is %s, Target temp %s",
                self.device_name,
                self.bt_hvac_mode,
                self.bt_target_temp,
            )

            if self.last_main_hvac_mode is None:
                self.last_main_hvac_mode = self.bt_hvac_mode

            if self.humidity_entity_id is not None:
                self._current_humidity = convert_to_float(
                    str(self.hass.states.get(self.humidity_entity_id).state),
                    self.device_name,
                    "startup()",
                )
            else:
                self._current_humidity = 0

            self.last_window_state = self.window_open
            if self.bt_hvac_mode not in (
                HVACMode.OFF,
                HVACMode.HEAT_COOL,
                HVACMode.HEAT,
            ):
                self.bt_hvac_mode = HVACMode.HEAT

            self.async_write_ha_state()

            for trv in self.real_trvs.keys():
                self.all_entities.append(trv)
                await init(self, trv)
                if self.real_trvs[trv]["calibration"] != 1:
                    self.real_trvs[trv]["last_calibration"] = await get_current_offset(
                        self, trv
                    )
                    self.real_trvs[trv]["local_calibration_min"] = await get_min_offset(
                        self, trv
                    )
                    self.real_trvs[trv]["local_calibration_max"] = await get_max_offset(
                        self, trv
                    )
                    self.real_trvs[trv]["local_calibration_step"] = (
                        await get_offset_step(self, trv)
                    )
                else:
                    self.real_trvs[trv]["last_calibration"] = 0

                self.real_trvs[trv]["valve_position"] = convert_to_float(
                    str(
                        self.hass.states.get(trv).attributes.get("valve_position", None)
                    ),
                    self.device_name,
                    "startup",
                )
                self.real_trvs[trv]["max_temp"] = convert_to_float(
                    str(self.hass.states.get(trv).attributes.get("max_temp", 30)),
                    self.device_name,
                    "startup",
                )
                self.real_trvs[trv]["min_temp"] = convert_to_float(
                    str(self.hass.states.get(trv).attributes.get("min_temp", 5)),
                    self.device_name,
                    "startup",
                )
                self.real_trvs[trv]["target_temp_step"] = convert_to_float(
                    str(
                        self.hass.states.get(trv).attributes.get(
                            "target_temp_step", 0.5
                        )
                    ),
                    self.device_name,
                    "startup",
                )
                self.real_trvs[trv]["temperature"] = convert_to_float(
                    str(self.hass.states.get(trv).attributes.get("temperature", 5)),
                    self.device_name,
                    "startup",
                )
                self.real_trvs[trv]["hvac_modes"] = self.hass.states.get(
                    trv
                ).attributes.get("hvac_modes", None)
                self.real_trvs[trv]["hvac_mode"] = self.hass.states.get(trv).state
                self.real_trvs[trv]["last_hvac_mode"] = self.hass.states.get(trv).state
                self.real_trvs[trv]["last_temperature"] = convert_to_float(
                    str(self.hass.states.get(trv).attributes.get("temperature")),
                    self.device_name,
                    "startup()",
                )
                self.real_trvs[trv]["current_temperature"] = convert_to_float(
                    str(
                        self.hass.states.get(trv).attributes.get("current_temperature")
                        or 5
                    ),
                    self.device_name,
                    "startup()",
                )
                await control_trv(self, trv)

            await self._trigger_time(None)
            await self._trigger_check_weather(None)
            self.startup_running = False
            self._available = True
            self.async_write_ha_state()
            #
            await asyncio.sleep(5)

            # try to find battery entities for all related entities
            for entity in self.all_entities:
                if entity is not None:
                    battery_id = await find_battery_entity(self, entity)
                    if battery_id is not None:
                        self.devices_states[entity] = {
                            "battery_id": battery_id,
                            "battery": None,
                        }

            if self.is_removed:
                return

            # Add listener
            if self.outdoor_sensor is not None:
                self.all_entities.append(self.outdoor_sensor)
                self.async_on_remove(
                    async_track_time_change(self.hass, self._trigger_time, 5, 0, 0)
                )

            await check_all_entities(self)

            if self.is_removed:
                return

            self.async_on_remove(
                async_track_time_interval(
                    self.hass, self._trigger_check_weather, timedelta(hours=1)
                )
            )

            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self.sensor_entity_id], self._trigger_temperature_change
                )
            )
            if self.humidity_entity_id is not None:
                self.async_on_remove(
                    async_track_state_change_event(
                        self.hass,
                        [self.humidity_entity_id],
                        self._trigger_humidity_change,
                    )
                )
            if self._async_unsub_state_changed is None:
                self._async_unsub_state_changed = async_track_state_change_event(
                    self.hass, self.entity_ids, self._trigger_trv_change
                )
                self.async_on_remove(self._async_unsub_state_changed)
            if self.window_id is not None:
                self.async_on_remove(
                    async_track_state_change_event(
                        self.hass, [self.window_id], self._trigger_window_change
                    )
                )
            if self.cooler_entity_id is not None:
                self.async_on_remove(
                    async_track_state_change_event(
                        self.hass, [self.cooler_entity_id], self._tigger_cooler_change
                    )
                )
            _LOGGER.info("better_thermostat %s: startup completed.", self.device_name)
            self.async_write_ha_state()
            await self.async_update_ha_state(force_refresh=True)
            break

    async def calculate_heating_power(self):
        """Learn effective heating power (°C/min) from completed heating cycles.

        Improvements over the original implementation:
        - Minimum duration of 1 minute (otherwise cycle is ignored)
        - Wait for the post-heating temperature peak (thermal inertia) after HEATING stops
        - Timeout based finalization if the temperature does not fall (prevents stuck cycles)
        - Outdoor temperature (if available) is used for normalization & adaptive weighting
        - Bounded telemetry (deque) for minimal memory footprint
        - Reduced state writes (only on changes / cycle finalization / action switches)
        """

        # Skip if we have no current temperature
        if self.cur_temp is None:
            return

        # Lazy init of target range bounds
        if not hasattr(self, "min_target_temp"):
            self.min_target_temp = self.bt_target_temp or 18.0
        if not hasattr(self, "max_target_temp"):
            self.max_target_temp = self.bt_target_temp or 21.0

        # Telemetry container (create once)
        if not hasattr(self, "heating_cycles"):
            # bounded length (50 cycles)
            self.heating_cycles = deque(maxlen=50)

        now = dt_util.utcnow()  # UTC aware time

        # Determine current action early (pure computation) for transition handling
        current_action = self._compute_hvac_action()

        action_changed = current_action != self.old_attr_hvac_action

        # Transition: heating starts
        if (
            current_action == HVACAction.HEATING
            and self.old_attr_hvac_action != HVACAction.HEATING
        ):
            self.heating_start_temp = self.cur_temp
            self.heating_start_timestamp = now
            self.heating_end_temp = None
            self.heating_end_timestamp = None

        # Transition: heating stops (candidate end)
        elif (
            current_action != HVACAction.HEATING
            and self.old_attr_hvac_action == HVACAction.HEATING
            and self.heating_start_temp is not None
            and self.heating_end_temp is None
        ):
            self.heating_end_temp = self.cur_temp
            self.heating_end_timestamp = now

        # Peak tracking: temperature still rising after heating already stopped
        elif (
            current_action != HVACAction.HEATING
            and self.heating_start_temp is not None
            and self.heating_end_temp is not None
            and self.cur_temp > self.heating_end_temp
        ):
            self.heating_end_temp = self.cur_temp
            self.heating_end_timestamp = now

        # Finalization criteria: temperature drops OR timeout triggers
        finalize = False
        TIMEOUT_MIN = 30  # safety timeout after 30 minutes of plateau

        if (
            self.heating_start_temp is not None
            and self.heating_end_temp is not None
            and self.cur_temp < self.heating_end_temp  # peak passed (temp falling)
        ):
            finalize = True
        elif self.heating_end_timestamp is not None and (
            now - self.heating_end_timestamp
        ) > timedelta(minutes=TIMEOUT_MIN):
            finalize = True

        heating_power_changed = False
        normalized_power = None

        if finalize:
            if (
                self.heating_end_temp is not None
                and self.heating_start_temp is not None
            ):
                temp_diff = self.heating_end_temp - self.heating_start_temp
            else:
                temp_diff = 0
            duration_min = (
                (
                    self.heating_end_timestamp - self.heating_start_timestamp
                ).total_seconds()
                / 60.0
                if self.heating_end_timestamp and self.heating_start_timestamp
                else 0
            )
            # Require minimum duration and positive temperature increase
            if duration_min >= 1.0 and temp_diff > 0:
                # Base weighting via relative position within target range
                temp_range = max(self.max_target_temp - self.min_target_temp, 0.1)
                relative_pos = (
                    (self.bt_target_temp - self.min_target_temp) / temp_range
                    if self.bt_target_temp is not None
                    else 0.5
                )
                weight_factor = max(0.5, min(1.5, 0.5 + relative_pos))

                # Consider outdoor temperature if available
                outdoor = None
                try:
                    if self.outdoor_sensor is not None:
                        outdoor_state = self.hass.states.get(self.outdoor_sensor)
                        if outdoor_state is not None:
                            outdoor = convert_to_float(
                                str(outdoor_state.state),
                                self.device_name,
                                "calculate_heating_power.outdoor",
                            )
                except Exception:  # noqa: BLE001
                    outdoor = None

                # Environmental delta (setpoint - outdoor) for normalization
                if outdoor is not None and self.bt_target_temp is not None:
                    delta_env = max(self.bt_target_temp - outdoor, 0.1)
                    # Normalized heating rate (°C/min relative to thermal gradient)
                    normalized_power = round((temp_diff / duration_min) / delta_env, 5)
                    # Environment factor influences smoothing weight (larger gradient -> slightly higher weight)
                    env_factor = max(0.7, min(1.3, delta_env / 20.0))
                else:
                    env_factor = 1.0

                heating_rate = round(temp_diff / duration_min, 4)  # °C / min

                # Adaptive exponential smoothing (alpha)
                base_alpha = 0.10
                alpha = base_alpha * weight_factor * env_factor
                alpha = max(0.02, min(0.25, alpha))  # Bounds

                old_power = self.heating_power
                self.heating_power = round(
                    old_power * (1 - alpha) + heating_rate * alpha, 4
                )
                heating_power_changed = self.heating_power != old_power

                # Compact short stats history
                self.last_heating_power_stats.append(
                    {
                        "dT": round(temp_diff, 2),
                        "min": round(duration_min, 1),
                        "rate": heating_rate,
                        "alpha": round(alpha, 3),
                        "envf": round(env_factor, 3),
                        "hp": self.heating_power,
                        "norm": normalized_power,
                    }
                )

                # Full cycle telemetry snapshot (bounded deque)
                try:
                    self.heating_cycles.append(
                        {
                            "start": (
                                self.heating_start_timestamp.isoformat()
                                if self.heating_start_timestamp
                                else None
                            ),
                            "end": (
                                self.heating_end_timestamp.isoformat()
                                if self.heating_end_timestamp
                                else None
                            ),
                            "temp_start": (
                                round(self.heating_start_temp, 2)
                                if self.heating_start_temp is not None
                                else None
                            ),
                            "temp_peak": (
                                round(self.heating_end_temp, 2)
                                if self.heating_end_temp is not None
                                else None
                            ),
                            "delta_t": round(temp_diff, 3),
                            "minutes": round(duration_min, 2),
                            "rate_c_min": heating_rate,
                            "target": self.bt_target_temp,
                            "outdoor": outdoor,
                            "norm_power": normalized_power,
                        }
                    )
                except Exception:  # noqa: BLE001
                    pass

                _LOGGER.debug(
                    "better_thermostat %s: heating cycle evaluated: ΔT=%.3f°C, t=%.2fmin, rate=%.4f°C/min, hp(old/new)=%.4f/%.4f, alpha=%.3f, env_factor=%.3f, norm=%s",  # noqa: E501
                    self.device_name,
                    temp_diff,
                    duration_min,
                    heating_rate,
                    old_power,
                    self.heating_power,
                    alpha,
                    env_factor,
                    normalized_power,
                )

            # Reset for next cycle (even if discarded)
            self.heating_start_temp = None
            self.heating_end_temp = None
            self.heating_start_timestamp = None
            self.heating_end_timestamp = None

        # Adjust dynamic target range bounds based on used setpoints
        if self.bt_target_temp is not None:
            self.min_target_temp = min(self.min_target_temp, self.bt_target_temp)
            self.max_target_temp = max(self.max_target_temp, self.bt_target_temp)

        # Track action changes using freshly computed action (pure function)
        if action_changed:
            self.old_attr_hvac_action = current_action
            self.attr_hvac_action = (
                current_action  # maintain legacy attribute for compatibility
            )

        # Write state only if something relevant changed
        if heating_power_changed or action_changed or finalize:
            # Store normalized power if available
            if normalized_power is not None:
                self.heating_power_normalized = normalized_power
            self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the device specific state attributes.

        Returns
        -------
        dict
                Attribute dictionary for the extra device specific state attributes.
        """
        dev_specific = {
            ATTR_STATE_WINDOW_OPEN: self.window_open,
            ATTR_STATE_CALL_FOR_HEAT: self.call_for_heat,
            ATTR_STATE_LAST_CHANGE: self.last_change.isoformat(),
            ATTR_STATE_SAVED_TEMPERATURE: self._saved_temperature,
            ATTR_STATE_HUMIDIY: self._current_humidity,
            ATTR_STATE_MAIN_MODE: self.last_main_hvac_mode,
            CONF_TOLERANCE: self.tolerance,
            CONF_TARGET_TEMP_STEP: self.bt_target_temp_step,
            ATTR_STATE_HEATING_POWER: self.heating_power,
            ATTR_STATE_ERRORS: json.dumps(self.devices_errors),
            ATTR_STATE_BATTERIES: json.dumps(self.devices_states),
        }

        # Optional telemetry (memory friendly): only count & last cycle + normalized power
        if hasattr(self, "heating_cycles") and len(self.heating_cycles) > 0:
            last_cycle = self.heating_cycles[-1]
            try:
                dev_specific["heating_cycle_count"] = len(self.heating_cycles)
                dev_specific["heating_cycle_last"] = json.dumps(last_cycle)
            except Exception:  # noqa: BLE001
                pass
        if hasattr(self, "heating_power_normalized"):
            dev_specific["heating_power_norm"] = getattr(
                self, "heating_power_normalized", None
            )

        return dev_specific

    @property
    def available(self):
        """Return if thermostat is available.

        Returns
        -------
        bool
                True if the thermostat is available.
        """
        return self._available

    @property
    def should_poll(self):
        """Return the polling state.

        Returns
        -------
        bool
                True if the thermostat uses polling.
        """
        return False

    @property
    def unique_id(self):
        """Return the unique id of this thermostat.

        Returns
        -------
        string
                The unique id of this thermostat.
        """
        return self._unique_id

    @property
    def precision(self):
        """Return the precision of the system.

        Returns
        -------
        float
                Precision of the thermostat.
        """
        return super().precision

    @property
    def target_temperature_step(self) -> Optional[float]:
        """Return the supported step of target temperature.

        Returns
        -------
        float
                Step size of target temperature.
        """
        if self.bt_target_temp_step is not None:
            return self.bt_target_temp_step

        return super().precision

    @property
    def temperature_unit(self) -> str:
        """Return the unit of measurement."""
        return self._unit

    @property
    def current_temperature(self) -> Optional[float]:
        """Return the current temperature."""
        return self.cur_temp

    @property
    def current_humidity(self) -> Optional[float]:
        """Return the current humidity if supported."""
        return self._current_humidity if hasattr(self, "_current_humidity") else None

    @property
    def hvac_mode(self) -> Optional[HVACMode]:
        """Return current operation."""
        # Fallback if None
        if self.bt_hvac_mode is None:
            return HVACMode.OFF
        return get_hvac_bt_mode(self, self.bt_hvac_mode)

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Return the list of available operation modes."""
        return self._hvac_list

    @property
    def hvac_action(self):
        """Return the current HVAC action (delegates to helper)."""
        return self._compute_hvac_action()

    def _compute_hvac_action(self):  # helper kept internal for clarity
        """Pure HVAC action computation without side effects.

        Rules:
        - OFF mode returns OFF regardless of temperatures
        - Open window suppresses active heating/cooling (returns IDLE)
        - Heating if cur_temp < target - tolerance (strictly below)
        - Cooling if mode heat_cool and cur_temp > cool_target + tolerance
        - Otherwise IDLE
        """
        if self.bt_target_temp is None or self.cur_temp is None:
            return HVACAction.IDLE
        if self.hvac_mode == HVACMode.OFF or self.bt_hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        if self.window_open:
            return HVACAction.IDLE
        tol = self.tolerance if self.tolerance is not None else 0.0
        # Heating decision
        # Use strict '<' so we do NOT heat when exactly at setpoint (especially when tol=0)
        if self.cur_temp < (self.bt_target_temp - tol):
            return HVACAction.HEATING
        # Cooling decision (if heat_cool mode and cooling setpoint exists)
        if (
            self.hvac_mode in (HVACMode.HEAT_COOL,)
            and self.bt_target_cooltemp is not None
            and self.cur_temp > (self.bt_target_cooltemp + tol)
        ):
            return HVACAction.COOLING
        return HVACAction.IDLE

    @property
    def target_temperature(self) -> Optional[float]:
        """Return the temperature we try to reach.

        Returns
        -------
        float
                Target temperature.
        """
        if self.bt_target_temp is None:
            return None
        if self.bt_min_temp is None or self.bt_max_temp is None:
            return self.bt_target_temp
        # if target temp is below minimum, return minimum
        if self.bt_target_temp < self.bt_min_temp:
            return self.bt_min_temp
        # if target temp is above maximum, return maximum
        if self.bt_target_temp > self.bt_max_temp:
            return self.bt_max_temp
        return self.bt_target_temp

    @property
    def target_temperature_low(self) -> Optional[float]:
        if self.cooler_entity_id is None:
            return None
        return self.bt_target_temp

    @property
    def target_temperature_high(self) -> Optional[float]:
        if self.cooler_entity_id is None:
            return None
        return self.bt_target_cooltemp

    async def async_set_hvac_mode(self, hvac_mode: str) -> None:
        """Set hvac mode.

        Returns
        -------
        None
        """
        if hvac_mode in (HVACMode.HEAT, HVACMode.HEAT_COOL, HVACMode.OFF):
            self.bt_hvac_mode = get_hvac_bt_mode(self, hvac_mode)
        else:
            _LOGGER.error(
                "better_thermostat %s: Unsupported hvac_mode %s",
                self.device_name,
                hvac_mode,
            )
        self.async_write_ha_state()
        await self.control_queue_task.put(self)

    async def async_set_temperature(self, **kwargs) -> None:
        """Set new target temperature."""
        if self.bt_hvac_mode == HVACMode.OFF:
            return

        _new_setpoint = None
        _new_setpointlow = None
        _new_setpointhigh = None

        if ATTR_HVAC_MODE in kwargs:
            hvac_mode = str(kwargs.get(ATTR_HVAC_MODE, None))
            if hvac_mode in (HVACMode.HEAT, HVACMode.HEAT_COOL, HVACMode.OFF):
                self.bt_hvac_mode = hvac_mode
            else:
                _LOGGER.error(
                    "better_thermostat %s: Unsupported hvac_mode %s",
                    self.device_name,
                    hvac_mode,
                )
        if ATTR_TEMPERATURE in kwargs:
            _new_setpoint = convert_to_float(
                str(kwargs.get(ATTR_TEMPERATURE, None)),
                self.device_name,
                "controlling.settarget_temperature()",
            )

        if ATTR_TARGET_TEMP_LOW in kwargs:
            _new_setpointlow = convert_to_float(
                str(kwargs.get(ATTR_TARGET_TEMP_LOW, None)),
                self.device_name,
                "controlling.settarget_temperature_low()",
            )

        if ATTR_TARGET_TEMP_HIGH in kwargs:
            _new_setpointhigh = convert_to_float(
                str(kwargs.get(ATTR_TARGET_TEMP_HIGH, None)),
                self.device_name,
                "controlling.settarget_temperature_high()",
            )

        if _new_setpoint is None and _new_setpointlow is None:
            _LOGGER.debug(
                f"better_thermostat {
                    self.device_name}: received a new setpoint from HA, but temperature attribute was not set, ignoring"
            )
            return

        # Validate against min/max temps
        if _new_setpoint is not None:
            _new_setpoint = min(self.max_temp, max(self.min_temp, _new_setpoint))
        if _new_setpointlow is not None:
            _new_setpointlow = min(self.max_temp, max(self.min_temp, _new_setpointlow))
        if _new_setpointhigh is not None:
            _new_setpointhigh = min(
                self.max_temp, max(self.min_temp, _new_setpointhigh)
            )

        # Preserve explicit 0.0 values (avoid Python truthiness bug)
        if _new_setpoint is not None:
            self.bt_target_temp = _new_setpoint
        else:
            self.bt_target_temp = _new_setpointlow

        if _new_setpointhigh is not None:
            self.bt_target_cooltemp = _new_setpointhigh

        # Enforce ordering: cool target should be above heat target (if both in heat_cool mode)
        if (
            self.hvac_mode in (HVACMode.HEAT_COOL,)
            and self.bt_target_cooltemp is not None
            and self.bt_target_temp is not None
            and self.bt_target_cooltemp <= self.bt_target_temp
        ):
            step = self.bt_target_temp_step or 0.5
            adjusted = self.bt_target_temp + step
            _LOGGER.warning(
                "better_thermostat %s: cooling target %.2f adjusted to %.2f to stay above heating target %.2f",
                self.device_name,
                self.bt_target_cooltemp,
                adjusted,
                self.bt_target_temp,
            )
            self.bt_target_cooltemp = adjusted

        _LOGGER.debug(
            "better_thermostat %s: HA set target temperature to %s & %s",
            self.device_name,
            self.bt_target_temp,
            self.bt_target_cooltemp,
        )

        self.async_write_ha_state()
        await self.control_queue_task.put(self)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.HEAT)

    @property
    def min_temp(self):
        """Return the minimum temperature.

        Returns
        -------
        float
                the minimum temperature.
        """
        if self.bt_min_temp is not None:
            return self.bt_min_temp

        # get default temp from super class
        return super().min_temp

    @property
    def max_temp(self):
        """Return the maximum temperature.

        Returns
        -------
        float
                the maximum temperature.
        """
        if self.bt_max_temp is not None:
            return self.bt_max_temp

        # Get default temp from super class
        return super().max_temp

    @property
    def _is_device_active(self):
        """Get the current state of the device for HA.

        Returns
        -------
        string
                State of the device.
        """
        if self.bt_hvac_mode == HVACMode.OFF:
            return False
        if self.window_open:
            return False
        return True

    @property
    def supported_features(self):
        """Return the list of supported features.

        Returns
        -------
        array
                Supported features.
        """
        if self.cooler_entity_id is not None:
            return (
                ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
                | ClimateEntityFeature.PRESET_MODE
                | ClimateEntityFeature.TURN_OFF
                | ClimateEntityFeature.TURN_ON
            )
        return (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.PRESET_MODE
            | ClimateEntityFeature.TURN_OFF
            | ClimateEntityFeature.TURN_ON
        )

    @property
    def preset_mode(self):
        return self._preset_mode

    def set_preset_mode(self, preset_mode: str) -> None:
        self._preset_mode = preset_mode

    @property
    def preset_modes(self):
        return [
            PRESET_NONE,
            # PRESET_AWAY,
            # PRESET_ECO,
            # PRESET_COMFORT,
            # PRESET_BOOST,
            # PRESET_SLEEP,
            # PRESET_ACTIVITY,
            # PRESET_HOME,
        ]
