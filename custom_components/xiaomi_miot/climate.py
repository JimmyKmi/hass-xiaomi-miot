"""Support for Xiaomi Aircondition."""
import logging
from enum import Enum

from homeassistant.const import *
from homeassistant.components.climate import (
    DOMAIN as ENTITY_DOMAIN,
    ClimateEntity,
)
from homeassistant.components.climate.const import *

from . import (
    DOMAIN,
    CONF_MODEL,
    XIAOMI_CONFIG_SCHEMA as PLATFORM_SCHEMA,  # noqa: F401
    MiotDevice,
    MiotToggleEntity,
    bind_services_to_entries,
)
from .core.miot_spec import (
    MiotSpec,
    MiotService,
    MiotProperty,
)
from .fan import (
    MiotModesSubEntity,
    SUPPORT_SET_SPEED as SUPPORT_SET_SPEED_FAN,
    SUPPORT_PRESET_MODE as SUPPORT_PRESET_MODE_FAN,
)
from .switch import MiotSwitchSubEntity
from .switch import MiotWasherActionSubEntity

_LOGGER = logging.getLogger(__name__)
DATA_KEY = f'{ENTITY_DOMAIN}.{DOMAIN}'

DEFAULT_MIN_TEMP = 16.0
DEFAULT_MAX_TEMP = 31.0

SERVICE_TO_METHOD = {}


async def async_setup_entry(hass, config_entry, async_add_entities):
    config = hass.data[DOMAIN]['configs'].get(config_entry.entry_id, dict(config_entry.data))
    await async_setup_platform(hass, config, async_add_entities)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    hass.data.setdefault(DATA_KEY, {})
    config.setdefault('add_entities', {})
    config['add_entities'][ENTITY_DOMAIN] = async_add_entities
    model = str(config.get(CONF_MODEL) or '')
    entities = []
    miot = config.get('miot_type')
    if miot:
        spec = await MiotSpec.async_from_type(hass, miot)
        for srv in spec.get_services(
            ENTITY_DOMAIN, 'air_conditioner', 'air_condition_outlet',
            'heater', 'ptc_bath_heater', 'light_bath_heater',
            'air_purifier', 'air_fresh', 'electric_blanket',
            'water_heater', 'water_dispenser', 'dishwasher',
        ):
            if not srv.get_property('on', 'mode', 'target_temperature'):
                continue
            cfg = {
                **config,
                'name': f"{config.get('name')} {srv.description}"
            }
            entities.append(MiotClimateEntity(cfg, srv))
    for entity in entities:
        hass.data[DOMAIN]['entities'][entity.unique_id] = entity
    async_add_entities(entities, update_before_add=True)
    bind_services_to_entries(hass, SERVICE_TO_METHOD)


class SwingModes(Enum):
    Off = 0
    Vertical = 1
    Horizontal = 2
    Steric = 3


class MiotClimateEntity(MiotToggleEntity, ClimateEntity):
    def __init__(self, config: dict, miot_service: MiotService):
        name = config[CONF_NAME]
        host = config[CONF_HOST]
        token = config[CONF_TOKEN]

        self._miot_service = miot_service
        mapping = miot_service.spec.services_mapping() or {}
        _LOGGER.info('Initializing %s (%s, token %s...), miot mapping: %s', name, host, token[:5], mapping)

        self._device = MiotDevice(mapping, host, token)
        super().__init__(name, self._device, miot_service, config=config)
        self._add_entities = config.get('add_entities') or {}

        self._prop_power = miot_service.bool_property('on')
        self._prop_mode = miot_service.get_property('mode')
        self._prop_target_temp = miot_service.get_property('target_temperature')
        self._prop_target_humi = miot_service.get_property('target_humidity')
        self._prop_fan_level = miot_service.get_property('fan_level', 'heat_level')

        self._environment = miot_service.spec.get_service('environment')
        self._prop_temperature = miot_service.get_property('temperature', 'indoor_temperature')
        self._prop_humidity = miot_service.get_property('relative_humidity', 'humidity')

        self._fan_control = miot_service.spec.get_service('fan_control')
        self._prop_fan_power = None
        self._prop_horizontal_swing = None
        self._prop_vertical_swing = None
        if self._fan_control:
            self._prop_fan_power = self._fan_control.get_property('on')
            self._prop_fan_level = self._fan_control.get_property('fan_level', 'heat_level')
            self._prop_horizontal_swing = self._fan_control.get_property('horizontal_swing')
            self._prop_horizontal_angle = self._fan_control.get_property('horizontal_angle')
            self._prop_vertical_swing = self._fan_control.get_property('vertical_swing')
            self._prop_vertical_angle = self._fan_control.get_property('vertical_angle')

        for s in [self._environment, self._fan_control]:
            if not s:
                continue
            if not self._prop_temperature:
                self._prop_temperature = s.get_property('temperature', 'indoor_temperature')
            if not self._prop_humidity:
                self._prop_humidity = s.get_property('relative_humidity', 'humidity')

        if miot_service.name in ['electric_blanket', 'water_heater', 'water_dispenser']:
            if not self._prop_fan_level:
                self._prop_fan_level = miot_service.get_property('heat_level', 'water_level')

        if self._prop_target_temp:
            self._supported_features |= SUPPORT_TARGET_TEMPERATURE
        if self._prop_target_humi:
            self._supported_features |= SUPPORT_TARGET_HUMIDITY
        if self.fan_modes or (self._prop_mode and self._prop_mode.list_first('Fan') is not None):
            self._supported_features |= SUPPORT_FAN_MODE
        if self._prop_horizontal_swing or self._prop_vertical_swing:
            self._supported_features |= SUPPORT_SWING_MODE

        self._state_attrs.update({'entity_class': self.__class__.__name__})
        self._power_modes = ['blow', 'heating', 'ventilation']
        if miot_service.get_property('heat_level'):
            self._power_modes.append('heater')
        self._hvac_modes = {
            HVAC_MODE_OFF:  {'list': ['Off', 'Idle', 'None']},
            HVAC_MODE_AUTO: {'list': ['Auto', 'Manual', 'Normal']},
            HVAC_MODE_COOL: {'list': ['Cool']},
            HVAC_MODE_HEAT: {'list': ['Heat']},
            HVAC_MODE_DRY:  {'list': ['Dry']},
            HVAC_MODE_FAN_ONLY: {'list': ['Fan']},
        }
        self._preset_modes = {}
        if self._prop_mode:
            mvs = []
            dls = []
            for mk, mv in self._hvac_modes.items():
                val = self._prop_mode.list_first(*(mv.get('list') or []))
                if val is not None:
                    self._hvac_modes[mk]['value'] = val
                    mvs.append(val)
                elif mk != HVAC_MODE_OFF:
                    dls.append(mk)
            for k in dls:
                self._hvac_modes.pop(k, None)
            fst = None
            for v in self._prop_mode.value_list:
                fst = fst or v
                val = v.get('value')
                if val not in mvs:
                    self._preset_modes[val] = v.get('description')
            if fst and len(self._hvac_modes) <= 1:
                self._hvac_modes[HVAC_MODE_AUTO] = {
                    'list':  [fst.get('description')],
                    'value': [fst.get('value')],
                }
        if self._preset_modes:
            self._supported_features |= SUPPORT_PRESET_MODE

    async def async_update(self):
        await super().async_update()
        if self._available:
            self.update_bind_sensor()
            add_fans = self._add_entities.get('fan')
            for m in self._power_modes:
                p = self._miot_service.bool_property(m)
                if m in self._subs:
                    self._subs[m].update()
                elif add_fans and p:
                    self._subs[m] = ClimateModeSubEntity(self, p)
                    add_fans([self._subs[m]])
            off = self._hvac_modes.get(HVAC_MODE_OFF, {}).get('value')
            for val, des in self._preset_modes.items():
                if des in self._subs:
                    self._subs[des].update()
                elif add_fans and self._prop_mode and self._miot_service.name in ['ptc_bath_heater']:
                    self._subs[des] = ClimateModeSubEntity(self, self._prop_mode, {
                        'unique_id':  f'{self.unique_id}-{self._prop_mode.full_name}-{val}',
                        'name':       f'{self.name} {des}',
                        'value_on':   val,
                        'value_off':  off,
                        'prop_speed': self._prop_fan_level,
                    })
                    add_fans([self._subs[des]])

            add_switches = self._add_entities.get('switch')
            for p in self._miot_service.properties.values():
                if not (p.format == 'bool' and p.readable and p.writeable):
                    continue
                if p.name in self._power_modes:
                    continue
                if self._prop_power and self._prop_power.name == p.name:
                    continue
                pnm = p.full_name
                if pnm in self._subs:
                    self._subs[pnm].update()
                elif add_switches:
                    self._subs[pnm] = MiotSwitchSubEntity(self, p)
                    add_switches([self._subs[pnm]])
            if self._miot_service.get_action('start_wash'):
                pnm = 'action_wash'
                prop = self._miot_service.get_property('status')
                if pnm in self._subs:
                    self._subs[pnm].update()
                elif add_switches and prop:
                    self._subs[pnm] = MiotWasherActionSubEntity(self, prop)
                    add_switches([self._subs[pnm]])


    def update_bind_sensor(self):
        bss = str(self.custom_config('bind_sensor') or '').split(',')
        ext = {}
        for bse in bss:
            bse = f'{bse}'.strip()
            if not bse:
                continue
            sta = self.hass.states.get(bse)
            if not sta or not sta.state or sta.state == STATE_UNKNOWN:
                continue
            try:
                num = float(sta.state)
            except ValueError:
                num = None
                _LOGGER.info('Got bound state from %s for %s: %s, state invalid', bse, self.name, sta.state)
            if num is not None:
                cls = sta.attributes.get(ATTR_DEVICE_CLASS)
                unit = sta.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
                if cls == DEVICE_CLASS_TEMPERATURE or unit in [TEMP_CELSIUS, TEMP_KELVIN, TEMP_FAHRENHEIT]:
                    ext[ATTR_CURRENT_TEMPERATURE] = self.hass.config.units.temperature(num, unit)
                elif cls == DEVICE_CLASS_HUMIDITY:
                    ext[ATTR_CURRENT_HUMIDITY] = num
        if ext:
            self.update_attrs(ext)
            _LOGGER.debug('Got bound state from %s for %s: %s', bss, self.name, ext)

    @property
    def is_on(self):
        if self._prop_power:
            return self._prop_power.from_dict(self._state_attrs) and True
        for m in self._power_modes:
            p = self._miot_service.bool_property(m)
            if not p:
                continue
            if self._state_attrs.get(p.full_name):
                return True
        if self._prop_mode:
            off = self._hvac_modes.get(HVAC_MODE_OFF, {}).get('value')
            if off is not None:
                return off != self._prop_mode.from_dict(self._state_attrs)
        power = self._state_attrs.get('power')
        if power is not None:
            return power and True
        return None

    def turn_on(self, **kwargs):
        if self._prop_power:
            return self.set_property(self._prop_power.full_name, True)
        for m in self._power_modes:
            p = self._miot_service.bool_property(m)
            if not p:
                continue
            return self.set_property(p.full_name, True)
        if self._prop_fan_power:
            return self.set_property(self._prop_fan_power.full_name, True)
        srv = self._miot_service.spec.get_service('viomi_bath_heater')
        if srv:
            act = srv.get_action('power_on')
            if act:
                ret = self.miot_action(srv.iid, act.iid)
                if ret:
                    self.update_attrs({'power': True})
                    return ret
        for mode in (HVAC_MODE_HEAT_COOL, HVAC_MODE_AUTO, HVAC_MODE_HEAT, HVAC_MODE_COOL):
            if mode not in self.hvac_modes:
                continue
            return self.set_hvac_mode(mode)
        return False

    def turn_off(self, **kwargs):
        if self._prop_power:
            return self.set_property(self._prop_power.full_name, False)
        if self._prop_mode:
            off = self._hvac_modes.get(HVAC_MODE_OFF, {}).get('value')
            if off is not None:
                return self.set_property(self._prop_mode.full_name, off)
        act = self._miot_service.get_action('stop_working', 'power_off')
        if act:
            ret = self.miot_action(self._miot_service.iid, act.iid)
            if ret:
                self.update_attrs({'power': False})
                return ret
        ret = None
        for m in self._power_modes:
            p = self._miot_service.bool_property(m)
            if not p:
                continue
            ret = self.set_property(p.full_name, False)
        if ret is not None:
            return ret
        if self._prop_fan_power:
            return self.set_property(self._prop_fan_power.full_name, False)
        return False

    @property
    def state(self):
        sta = self.hvac_mode
        if sta is None and self._prop_mode:
            val = self._prop_mode.from_dict(self._state_attrs)
            if val is not None:
                sta = self._prop_mode.list_description(val)
            if sta:
                sta = str(sta).lower()
        return sta

    @property
    def hvac_mode(self):
        if not self.is_on:
            return HVAC_MODE_OFF
        if self._prop_mode:
            acm = self._prop_mode.from_dict(self._state_attrs)
            acm = -1 if acm is None else int(acm or 0)
            for mk, mv in self._hvac_modes.items():
                if acm == mv.get('value'):
                    return mk
        elif self._prop_power:
            return HVAC_MODE_AUTO
        return None

    @property
    def hvac_modes(self):
        hms = []
        if self._prop_mode:
            for mk, mv in self._hvac_modes.items():
                if mv.get('value') is None:
                    continue
                hms.append(mk)
        elif self._prop_power:
            hms.append(HVAC_MODE_AUTO)
        if HVAC_MODE_OFF not in hms:
            hms.append(HVAC_MODE_OFF)
        return hms

    def set_hvac_mode(self, mode: str):
        if mode == HVAC_MODE_OFF:
            return self.turn_off()
        if self._prop_power and not self.is_on:
            self.set_property(self._prop_power.full_name, True)
        if not self._prop_mode:
            return False
        val = self._hvac_modes.get(mode, {}).get('value')
        if val is None:
            val = self._prop_mode.list_first(mode)
        if val is None:
            return False
        return self.set_property(self._prop_mode.full_name, val)

    @property
    def preset_mode(self):
        if not self.is_on:
            return HVAC_MODE_OFF
        if self._preset_modes and self._prop_mode:
            acm = self._prop_mode.from_dict(self._state_attrs)
            acm = -1 if acm is None else int(acm or 0)
            return self._preset_modes.get(acm, HVAC_MODE_OFF)
        return None

    @property
    def preset_modes(self):
        pms = []
        if self._preset_modes:
            for mk, mv in self._preset_modes.items():
                pms.append(mv)
        if HVAC_MODE_OFF not in pms:
            pms.append(HVAC_MODE_OFF)
        return pms

    def set_preset_mode(self, mode: str):
        if not self._preset_modes:
            return False
        return self.set_hvac_mode(mode)

    @property
    def temperature_unit(self):
        prop = self._prop_temperature or self._prop_target_temp
        if prop:
            if prop.unit in ['celsius', TEMP_CELSIUS]:
                return TEMP_CELSIUS
            if prop.unit in ['fahrenheit', TEMP_FAHRENHEIT]:
                return TEMP_FAHRENHEIT
            if prop.unit in ['kelvin', TEMP_KELVIN]:
                return TEMP_KELVIN
        return TEMP_CELSIUS

    @property
    def current_temperature(self):
        if ATTR_CURRENT_TEMPERATURE in self._state_attrs:
            return float(self._state_attrs[ATTR_CURRENT_TEMPERATURE] or 0)
        if self._prop_temperature:
            return float(self._prop_temperature.from_dict(self._state_attrs) or 0)
        return None

    @property
    def min_temp(self):
        if self._prop_target_temp:
            return self._prop_target_temp.range_min()
        return DEFAULT_MIN_TEMP

    @property
    def max_temp(self):
        if self._prop_target_temp:
            return self._prop_target_temp.range_max()
        return DEFAULT_MAX_TEMP

    @property
    def target_temperature(self):
        if self._prop_target_temp:
            return float(self._prop_target_temp.from_dict(self._state_attrs) or 0)
        return None

    @property
    def target_temperature_step(self):
        if self._prop_target_temp:
            return self._prop_target_temp.range_step()
        return 1

    @property
    def target_temperature_high(self):
        return DEFAULT_MAX_TEMP

    @property
    def target_temperature_low(self):
        return DEFAULT_MIN_TEMP

    def set_temperature(self, **kwargs):
        ret = False
        if ATTR_HVAC_MODE in kwargs:
            ret = self.set_hvac_mode(kwargs[ATTR_HVAC_MODE])
        if ATTR_TEMPERATURE in kwargs:
            val = kwargs[ATTR_TEMPERATURE]
            if val < self.min_temp:
                val = self.min_temp
            if val > self.max_temp:
                val = self.max_temp
            ret = self.set_property(self._prop_target_temp.full_name, val)
        return ret

    @property
    def current_humidity(self):
        if ATTR_CURRENT_HUMIDITY in self._state_attrs:
            return float(self._state_attrs[ATTR_CURRENT_HUMIDITY] or 0)
        if self._prop_humidity:
            return int(self._prop_humidity.from_dict(self._state_attrs) or 0)
        return None

    @property
    def target_humidity(self):
        if self._prop_target_humi:
            return int(self._prop_target_humi.from_dict(self._state_attrs) or 0)
        return None

    @property
    def min_humidity(self):
        if self._prop_target_humi:
            return self._prop_target_humi.range_min()
        return DEFAULT_MIN_HUMIDITY

    @property
    def max_humidity(self):
        if self._prop_target_humi:
            return self._prop_target_humi.range_max()
        return DEFAULT_MAX_HUMIDITY

    def set_humidity(self, humidity):
        if self._prop_target_humi:
            return self.set_property(self._prop_target_humi.full_name, humidity)
        return False

    @property
    def fan_mode(self):
        if self._prop_fan_level:
            val = self._prop_fan_level.from_dict(self._state_attrs)
            if val is not None:
                return self._prop_fan_level.list_description(val)
        return None

    @property
    def fan_modes(self):
        if self._prop_fan_level:
            return self._prop_fan_level.list_description(None) or []
        return None

    def set_fan_mode(self, fan_mode: str):
        if self._prop_fan_level:
            val = self._prop_fan_level.list_value(fan_mode)
            return self.set_property(self._prop_fan_level.full_name, val)
        return False

    @property
    def swing_mode(self):
        val = 0
        pvs = self._prop_vertical_swing
        phs = self._prop_horizontal_swing
        if pvs and pvs.from_dict(self._state_attrs, False):
            val |= 1
        if phs and phs.from_dict(self._state_attrs, False):
            val |= 2
        return SwingModes(val).name

    @property
    def swing_modes(self):
        lst = [SwingModes(0).name]
        pvs = self._prop_vertical_swing
        phs = self._prop_horizontal_swing
        if pvs:
            lst.append(SwingModes(1).name)
        if phs:
            lst.append(SwingModes(2).name)
        if pvs and phs:
            lst.append(SwingModes(3).name)
        return lst

    def set_swing_mode(self, swing_mode: str) -> None:
        ret = None
        ver = None
        hor = None
        val = SwingModes[swing_mode].value
        if val & 1:
            ver = True
            if val == 1:
                hor = False
        if val & 2:
            hor = True
            if val == 2:
                ver = False
        if val == 0:
            ver = False
            hor = False
        swm = {}
        if self._prop_vertical_swing:
            swm[self._prop_vertical_swing.name] = ver
        if self._prop_horizontal_swing:
            swm[self._prop_horizontal_swing.name] = hor
        for mk, mv in swm.items():
            old = self._state_attrs.get(mk, None)
            if old is None or mv is None:
                continue
            if mv == old:
                continue
            ret = self.set_property(mk, mv)
        return ret


class ClimateModeSubEntity(MiotModesSubEntity):
    def __init__(self, parent: MiotClimateEntity, miot_property: MiotProperty, option=None):
        super().__init__(parent, miot_property, option)
        self._prop_power = None
        if miot_property.format == 'bool':
            self._prop_power = miot_property
        self._value_on = self._option.get('value_on')
        self._value_off = self._option.get('value_off')

        self._prop_speed = self._option.get('prop_speed')
        if miot_property.name in ['heater']:
            self._prop_speed = miot_property.service.get_property('heat_level') or self._prop_speed
        if self._prop_speed:
            self._option['keys'] = [self._prop_speed.full_name, *(self._option.get('keys') or [])]

        self._supported_features = 0
        if self.speed_list:
            self._supported_features |= SUPPORT_PRESET_MODE_FAN or SUPPORT_SET_SPEED_FAN

    def update(self):
        super().update()
        if self._available:
            attrs = self._state_attrs
            if self._value_on is not None:
                self._state = attrs.get(self._attr) == self._value_on
            else:
                self._state = attrs.get(self._attr) and True

    def turn_on(self, speed=None, percentage=None, preset_mode=None, **kwargs):
        ret = False
        if self._prop_power:
            ret = self.call_parent('set_property', self._prop_power.full_name, True)
        elif self._value_on is not None:
            ret = self.call_parent('set_property', self._miot_property.full_name, self._value_on)
        if speed:
            ret = self.set_speed(speed)
        return ret

    def turn_off(self, **kwargs):
        if self._prop_power:
            return self.call_parent('set_property', self._prop_power.full_name, False)
        if self._value_off is not None:
            return self.call_parent('set_property', self._miot_property.full_name, self._value_off)
        return False

    @property
    def preset_mode(self):
        if self._prop_speed:
            val = self._prop_speed.from_dict(self._state_attrs)
            if val is not None:
                return self._prop_speed.list_description(val)
        return self._parent.fan_mode

    @property
    def preset_modes(self):
        if self._prop_speed:
            return self._prop_speed.list_descriptions()
        return self._parent.fan_modes or []

    def set_preset_mode(self, preset_mode):
        if self._prop_speed:
            val = self._prop_speed.list_first(preset_mode)
            return self.call_parent('set_property', self._prop_speed.full_name, val)
        return self.call_parent('set_fan_mode', preset_mode)
