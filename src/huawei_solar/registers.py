from datetime import datetime, timezone
from dataclasses import dataclass
from enum import IntEnum
from functools import partial

from huawei_solar.exceptions import DecodeError
import huawei_solar.register_names as rn
import huawei_solar.register_values as rv
from pymodbus.payload import BinaryPayloadDecoder

import typing as t
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .huawei_solar import AsyncHuaweiSolar


@dataclass
class RegisterDefinition:
    def __init__(self, register, length):
        self.register = register
        self.length = length

    def decode(self, decoder: BinaryPayloadDecoder, inverter: "AsyncHuaweiSolar"):
        raise NotImplementedError()


class StringRegister(RegisterDefinition):
    def __init__(self, register, length):
        super().__init__(register, length)

    def decode(self, decoder: BinaryPayloadDecoder, inverter: "AsyncHuaweiSolar"):
        return decoder.decode_string(self.length * 2).decode("utf-8").strip("\0")


class NumberRegister(RegisterDefinition):
    def __init__(self, unit, gain, register, length, decode_function_name):
        super().__init__(register, length)
        self.unit = unit
        self.gain = gain

        self._decode_function_name = decode_function_name

    def decode(self, decoder: BinaryPayloadDecoder, inverter: "AsyncHuaweiSolar"):
        result = getattr(decoder, self._decode_function_name)()

        if self.gain != 1:
            result /= self.gain
        if callable(self.unit):
            result = self.unit(result)
        elif isinstance(self.unit, dict):
            result = self.unit[result]

        return result


class U16Register(NumberRegister):
    def __init__(self, unit, gain, register, length):
        super().__init__(unit, gain, register, length, "decode_16bit_uint")


class U32Register(NumberRegister):
    def __init__(self, unit, gain, register, length):
        super().__init__(unit, gain, register, length, "decode_32bit_uint")


class I16Register(NumberRegister):
    def __init__(self, unit, gain, register, length):
        super().__init__(unit, gain, register, length, "decode_16bit_int")


class I32Register(NumberRegister):
    def __init__(self, unit, gain, register, length):
        super().__init__(unit, gain, register, length, "decode_32bit_int")


def bitfield_decoder(definition, value):
    result = []
    for key, value in definition.items():
        if key & value:
            result.append(value)

    return result


class TimestampRegister(U32Register):
    def __init__(self, register, length):
        super().__init__(None, 1, register, length)

    def decode(self, decoder: BinaryPayloadDecoder, inverter: "AsyncHuaweiSolar"):
        value = super().decode(decoder, inverter)

        try:
            return datetime.fromtimestamp(value - 60 * inverter.time_zone, timezone.utc)
        except OverflowError as err:
            raise DecodeError(f"Received invalid timestamp {value}") from err


@dataclass
class LG_RESU_TimeOfUsePeriod:
    start_time: int  # minutes sinds midnight
    end_time: int  # minutes sinds midnight
    electricity_price: float


class ChargeFlag(IntEnum):
    Charge = 0
    Discharge = 1


@dataclass
class HUAWEI_LUNA2000_TimeOfUsePeriod:
    start_time: int  # minutes sinds midnight
    end_time: int  # minutes sinds midnight
    charge_flag: ChargeFlag
    days_effective: t.Tuple[
        bool, bool, bool, bool, bool, bool, bool
    ]  # Valid on days Sunday to Saturday


class TimeOfUseRegisters(RegisterDefinition):
    def __init__(self, register, length):
        super().__init__(register, length)

    def decode(self, decoder: BinaryPayloadDecoder, inverter: "AsyncHuaweiSolar"):
        if inverter.battery_type == rv.StorageProductModel.LG_RESU:
            return self.decode_lg_resu(decoder)
        elif inverter.battery_type == rv.StorageProductModel.HUAWEI_LUNA2000:
            return self.decode_huawei_luna2000(decoder)
        else:
            return DecodeError(
                f"Invalid model to decode TOU Registers for: {inverter.battery_type}"
            )

    def decode_lg_resu(self, decoder: BinaryPayloadDecoder):
        number_of_periods = decoder.decode_16bit_uint()
        assert number_of_periods <= 10

        periods = []
        for _ in range(10):
            periods.append(
                LG_RESU_TimeOfUsePeriod(
                    decoder.decode_16bit_uint(),
                    decoder.decode_16bit_uint(),
                    decoder.decode_32bit_uint() / 1000,
                )
            )

        return periods[:number_of_periods]

    def decode_huawei_luna2000(self, decoder: BinaryPayloadDecoder):
        number_of_periods = decoder.decode_16bit_uint()
        assert number_of_periods <= 14

        def _days_effective_parser(value):
            result = []
            mask = 0x1
            for _ in range(7):
                result.append((value & mask) != 0)
                mask = mask << 1

            return tuple(result)

        periods = []
        for _ in range(14):
            periods.append(
                HUAWEI_LUNA2000_TimeOfUsePeriod(
                    decoder.decode_16bit_uint(),
                    decoder.decode_16bit_uint(),
                    ChargeFlag(decoder.decode_8bit_uint()),
                    _days_effective_parser(decoder.decode_8bit_uint()),
                )
            )

        return periods[:number_of_periods]


class ChargeDischargePeriod:
    start_time: int  # minutes sinds midnight
    end_time: int  # minutes sinds midnight
    power: int  # power in watts


class ChargeDischargePeriodRegisters(RegisterDefinition):
    def __init__(self, register, length):
        super().__init__(register, length)

    def decode(self, decoder: BinaryPayloadDecoder, inverter: "AsyncHuaweiSolar"):
        number_of_periods = decoder.decode_16bit_uint()
        assert number_of_periods <= 10

        periods = []
        for _ in range(10):
            periods.append(
                ChargeDischargePeriod(
                    decoder.decode_16bit_uint(),
                    decoder.decode_16bit_uint(),
                    decoder.decode_32bit_int(),
                )
            )

        return periods[:number_of_periods]


REGISTERS = {
    rn.MODEL_NAME: StringRegister(30000, 15),
    rn.SERIAL_NUMBER: StringRegister(30015, 10),
    rn.MODEL_ID: U16Register(None, 1, 30070, 1),
    rn.NB_PV_STRINGS: U16Register(None, 1, 30071, 1),
    rn.NB_MPP_TRACKS: U16Register(None, 1, 30072, 1),
    rn.RATED_POWER: U32Register("W", 1, 30073, 2),
    rn.P_MAX: U32Register("W", 1, 30075, 2),
    rn.S_MAX: U32Register("VA", 1, 30077, 2),
    rn.Q_MAX_OUT: I32Register("VAr", 1, 30079, 2),
    rn.Q_MAX_IN: I32Register("VAr", 1, 30081, 2),
    rn.STATE_1: U16Register(partial(bitfield_decoder, rv.STATE_CODES_1), 1, 32000, 1),
    rn.STATE_2: U16Register(partial(bitfield_decoder, rv.STATE_CODES_2), 1, 32002, 1),
    rn.STATE_3: U32Register(partial(bitfield_decoder, rv.STATE_CODES_3), 1, 32003, 2),
    rn.ALARM_1: U16Register(partial(bitfield_decoder, rv.ALARM_CODES_1), 1, 32008, 1),
    rn.ALARM_2: U16Register(partial(bitfield_decoder, rv.ALARM_CODES_2), 1, 32009, 1),
    rn.ALARM_3: U16Register(partial(bitfield_decoder, rv.ALARM_CODES_3), 1, 32010, 1),
    rn.INPUT_POWER: I32Register("W", 1, 32064, 2),
    rn.GRID_VOLTAGE: U16Register("V", 10, 32066, 1),
    rn.LINE_VOLTAGE_A_B: U16Register("V", 10, 32066, 1),
    rn.LINE_VOLTAGE_B_C: U16Register("V", 10, 32067, 1),
    rn.LINE_VOLTAGE_C_A: U16Register("V", 10, 32068, 1),
    rn.PHASE_A_VOLTAGE: U16Register("V", 10, 32069, 1),
    rn.PHASE_B_VOLTAGE: U16Register("V", 10, 32070, 1),
    rn.PHASE_C_VOLTAGE: U16Register("V", 10, 32071, 1),
    rn.GRID_CURRENT: I32Register("A", 1000, 32072, 2),
    rn.PHASE_A_CURRENT: I32Register("A", 1000, 32072, 2),
    rn.PHASE_B_CURRENT: I32Register("A", 1000, 32074, 2),
    rn.PHASE_C_CURRENT: I32Register("A", 1000, 32076, 2),
    rn.DAY_ACTIVE_POWER_PEAK: I32Register("W", 1, 32078, 2),
    rn.ACTIVE_POWER: I32Register("W", 1, 32080, 2),
    rn.REACTIVE_POWER: I32Register("VA", 1, 32082, 2),
    rn.POWER_FACTOR: I16Register(None, 1000, 32084, 1),
    rn.GRID_FREQUENCY: U16Register("Hz", 100, 32085, 1),
    rn.EFFICIENCY: U16Register("%", 100, 32086, 1),
    rn.INTERNAL_TEMPERATURE: I16Register("°C", 10, 32087, 1),
    rn.INSULATION_RESISTANCE: U16Register("MOhm", 100, 32088, 1),
    rn.DEVICE_STATUS: U16Register(rv.DEVICE_STATUS_DEFINITIONS, 1, 32089, 1),
    rn.FAULT_CODE: U16Register(None, 1, 32090, 1),
    rn.STARTUP_TIME: TimestampRegister(32091, 2),
    rn.SHUTDOWN_TIME: TimestampRegister(32093, 2),
    rn.ACCUMULATED_YIELD_ENERGY: U32Register("kWh", 100, 32106, 2),
    rn.UNKNOWN_TIME_1: TimestampRegister(32110, 2),     # last contact with server?
    rn.DAILY_YIELD_ENERGY: U32Register("kWh", 100, 32114, 2),
    rn.UNKNOWN_TIME_2: TimestampRegister(32156, 2),     # something todo with startup time?
    rn.UNKNOWN_TIME_3: TimestampRegister(32160, 2),    # something todo with shutdown time?
    rn.UNKNOWN_TIME_4: TimestampRegister(35113, 2),     # installation time?
    rn.NB_OPTIMIZERS: U16Register(None, 1, 37200, 1),
    rn.METER_TYPE_CHECK: U16Register(rv.METER_TYPE_CHECK, 1, 37125, 2),
    rn.NB_ONLINE_OPTIMIZERS: U16Register(None, 1, 37201, 1),
    rn.SYSTEM_TIME: TimestampRegister(40000, 2),
    rn.UNKNOWN_TIME_5: TimestampRegister(40500, 2),     # seems to be the same as unknown_time_4
    rn.GRID_CODE: U16Register(rv.GRID_CODES, 1, 42000, 1),
    rn.TIME_ZONE: I16Register("min", 1, 43006, 1),
}


OPTIMIZER_REGISTERS = {
    rn.PV_01_VOLTAGE: I16Register("V", 10, 32016, 1),
    rn.PV_01_CURRENT: I16Register("A", 100, 32017, 1),
    rn.PV_02_VOLTAGE: I16Register("V", 10, 32018, 1),
    rn.PV_02_CURRENT: I16Register("A", 100, 32019, 1),
    rn.PV_03_VOLTAGE: I16Register("V", 10, 32020, 1),
    rn.PV_03_CURRENT: I16Register("A", 100, 32021, 1),
    rn.PV_04_VOLTAGE: I16Register("V", 10, 32022, 1),
    rn.PV_04_CURRENT: I16Register("A", 100, 32023, 1),
    rn.PV_05_VOLTAGE: I16Register("V", 10, 32024, 1),
    rn.PV_05_CURRENT: I16Register("A", 100, 32025, 1),
    rn.PV_06_VOLTAGE: I16Register("V", 10, 32026, 1),
    rn.PV_06_CURRENT: I16Register("A", 100, 32027, 1),
    rn.PV_07_VOLTAGE: I16Register("V", 10, 32028, 1),
    rn.PV_07_CURRENT: I16Register("A", 100, 32029, 1),
    rn.PV_08_VOLTAGE: I16Register("V", 10, 32030, 1),
    rn.PV_08_CURRENT: I16Register("A", 100, 32031, 1),
    rn.PV_09_VOLTAGE: I16Register("V", 10, 32032, 1),
    rn.PV_09_CURRENT: I16Register("A", 100, 32033, 1),
    rn.PV_10_VOLTAGE: I16Register("V", 10, 32034, 1),
    rn.PV_10_CURRENT: I16Register("A", 100, 32035, 1),
    rn.PV_11_VOLTAGE: I16Register("V", 10, 32036, 1),
    rn.PV_11_CURRENT: I16Register("A", 100, 32037, 1),
    rn.PV_12_VOLTAGE: I16Register("V", 10, 32038, 1),
    rn.PV_12_CURRENT: I16Register("A", 100, 32039, 1),
    rn.PV_13_VOLTAGE: I16Register("V", 10, 32040, 1),
    rn.PV_13_CURRENT: I16Register("A", 100, 32041, 1),
    rn.PV_14_VOLTAGE: I16Register("V", 10, 32042, 1),
    rn.PV_14_CURRENT: I16Register("A", 100, 32043, 1),
    rn.PV_15_VOLTAGE: I16Register("V", 10, 32044, 1),
    rn.PV_15_CURRENT: I16Register("A", 100, 32045, 1),
    rn.PV_16_VOLTAGE: I16Register("V", 10, 32046, 1),
    rn.PV_16_CURRENT: I16Register("A", 100, 32047, 1),
    rn.PV_17_VOLTAGE: I16Register("V", 10, 32048, 1),
    rn.PV_17_CURRENT: I16Register("A", 100, 32049, 1),
    rn.PV_18_VOLTAGE: I16Register("V", 10, 32050, 1),
    rn.PV_18_CURRENT: I16Register("A", 100, 32051, 1),
    rn.PV_19_VOLTAGE: I16Register("V", 10, 32052, 1),
    rn.PV_19_CURRENT: I16Register("A", 100, 32053, 1),
    rn.PV_20_VOLTAGE: I16Register("V", 10, 32054, 1),
    rn.PV_20_CURRENT: I16Register("A", 100, 32055, 1),
    rn.PV_21_VOLTAGE: I16Register("V", 10, 32056, 1),
    rn.PV_21_CURRENT: I16Register("A", 100, 32057, 1),
    rn.PV_22_VOLTAGE: I16Register("V", 10, 32058, 1),
    rn.PV_22_CURRENT: I16Register("A", 100, 32059, 1),
    rn.PV_23_VOLTAGE: I16Register("V", 10, 32060, 1),
    rn.PV_23_CURRENT: I16Register("A", 100, 32061, 1),
    rn.PV_24_VOLTAGE: I16Register("V", 10, 32062, 1),
    rn.PV_24_CURRENT: I16Register("A", 100, 32063, 1),
}

REGISTERS.update(OPTIMIZER_REGISTERS)

BATTERY_REGISTERS = {
    rn.STORAGE_UNIT_1_RUNNING_STATUS: U16Register(rv.STORAGE_STATUS_DEFINITIONS, 1, 37000, 1),
    rn.STORAGE_UNIT_1_CHARGE_DISCHARGE_POWER: I32Register("W", 1, 37001, 2),
    rn.STORAGE_UNIT_1_BUS_VOLTAGE: U16Register("V", 10, 37003, 1),
    rn.STORAGE_UNIT_1_STATE_OF_CAPACITY: U16Register("%", 10, 37004, 1),
    rn.STORAGE_UNIT_1_WORKING_MODE_B: U16Register(rv.STORAGE_WORKING_MODES_B, 1, 37006, 1),
    rn.STORAGE_UNIT_1_RATED_CHARGE_POWER: U32Register("W", 1, 37007, 2),
    rn.STORAGE_UNIT_1_RATED_DISCHARGE_POWER: U32Register("W", 1, 37009, 2),
    rn.STORAGE_UNIT_1_FAULT_ID: U16Register(None, 1, 37014, 1),
    rn.STORAGE_UNIT_1_CURRENT_DAY_CHARGE_CAPACITY: U32Register("kWh", 100, 37015, 2),
    rn.STORAGE_UNIT_1_CURRENT_DAY_DISCHARGE_CAPACITY: U32Register("kWh", 100, 37017, 2),
    rn.STORAGE_UNIT_1_BUS_CURRENT: I16Register("A", 10, 37021, 1),
    rn.STORAGE_UNIT_1_BATTERY_TEMPERATURE: I16Register("°C", 10, 37022, 1),
    rn.STORAGE_UNIT_1_REMAINING_CHARGE_DIS_CHARGE_TIME: U16Register("min", 1, 37025, 1),
    rn.STORAGE_UNIT_1_DCDC_VERSION: StringRegister(37026, 10),
    rn.STORAGE_UNIT_1_BMS_VERSION: StringRegister(37036, 10),
    rn.STORAGE_MAXIMUM_CHARGE_POWER: U32Register("W", 1, 37046, 2),
    rn.STORAGE_MAXIMUM_DISCHARGE_POWER: U32Register("W", 1, 37048, 2),
    rn.STORAGE_UNIT_1_SERIAL_NUMBER: StringRegister(37052, 10),
    rn.STORAGE_UNIT_1_TOTAL_CHARGE: U32Register("kWh", 100, 37066, 2),
    rn.STORAGE_UNIT_1_TOTAL_DISCHARGE: U32Register("kWh", 100, 37068, 2),
    rn.STORAGE_UNIT_2_SERIAL_NUMBER: StringRegister(37700, 10),
    rn.STORAGE_UNIT_2_STATE_OF_CAPACITY: U16Register("%", 10, 37738, 1),
    rn.STORAGE_UNIT_2_RUNNING_STATUS: U16Register(rv.STORAGE_STATUS_DEFINITIONS, 1, 37741, 1),
    rn.STORAGE_UNIT_2_CHARGE_DISCHARGE_POWER: I32Register("W", 1, 37743, 2),
    rn.STORAGE_UNIT_2_CURRENT_DAY_CHARGE_CAPACITY: U32Register("kWh", 100, 37746, 2),
    rn.STORAGE_UNIT_2_CURRENT_DAY_DISCHARGE_CAPACITY: U32Register("kWh", 100, 37748, 2),
    rn.STORAGE_UNIT_2_BUS_VOLTAGE: U16Register("V", 10, 37750, 1),
    rn.STORAGE_UNIT_2_BUS_CURRENT: I16Register("A", 10, 37751, 1),
    rn.STORAGE_UNIT_2_BATTERY_TEMPERATURE: I16Register("°C", 10, 37752, 1),
    rn.STORAGE_UNIT_2_TOTAL_CHARGE: U32Register("kWh", 100, 37753, 2),
    rn.STORAGE_UNIT_2_TOTAL_DISCHARGE: U32Register("kWh", 100, 37755, 2),
    rn.STORAGE_RATED_CAPACITY: U32Register("Wh", 1, 37758, 2),
    rn.STORAGE_STATE_OF_CAPACITY: U16Register("%", 10, 37760, 1),
    rn.STORAGE_RUNNING_STATUS: U16Register(rv.STORAGE_STATUS_DEFINITIONS, 1, 37762, 1),
    rn.STORAGE_BUS_VOLTAGE: U16Register("V", 10, 37763, 1),
    rn.STORAGE_BUS_CURRENT: I16Register("A", 10, 37764, 1),
    rn.STORAGE_CHARGE_DISCHARGE_POWER: I32Register("W", 1, 37765, 2),
    rn.STORAGE_TOTAL_CHARGE: U32Register("kWh", 100, 37780, 2),
    rn.STORAGE_TOTAL_DISCHARGE: U32Register("kWh", 100, 37782, 2),
    rn.STORAGE_CURRENT_DAY_CHARGE_CAPACITY: U32Register("kWh", 100, 37784, 2),
    rn.STORAGE_CURRENT_DAY_DISCHARGE_CAPACITY: U32Register("kWh", 100, 37786, 2),
    rn.STORAGE_UNIT_2_SOFTWARE_VERSION: StringRegister(37799, 15),
    rn.STORAGE_UNIT_1_SOFTWARE_VERSION: StringRegister(37814, 15),
    rn.STORAGE_UNIT_1_BATTERY_PACK_1_SERIAL_NUMBER: StringRegister(38200, 10),
    rn.STORAGE_UNIT_1_BATTERY_PACK_1_FIRMWARE_VERSION: StringRegister(38210, 15),
    rn.STORAGE_UNIT_1_BATTERY_PACK_1_WORKING_STATUS: U16Register(None, 1, 38228, 1),
    rn.STORAGE_UNIT_1_BATTERY_PACK_1_STATE_OF_CAPACITY: U16Register("%", 10, 38229, 1),
    rn.STORAGE_UNIT_1_BATTERY_PACK_1_CHARGE_DISCHARGE_POWER: I32Register("W", 1, 38233, 2),
    rn.STORAGE_UNIT_1_BATTERY_PACK_1_VOLTAGE: U16Register("V", 10, 38235, 1),
    rn.STORAGE_UNIT_1_BATTERY_PACK_1_CURRENT: I16Register("A", 10, 38236, 1),
    rn.STORAGE_UNIT_1_BATTERY_PACK_1_TOTAL_CHARGE: U32Register("kWh", 100, 38238, 2),
    rn.STORAGE_UNIT_1_BATTERY_PACK_1_TOTAL_DISCHARGE: U32Register("kWh", 100, 38240, 2),
    rn.STORAGE_UNIT_1_BATTERY_PACK_2_SERIAL_NUMBER: StringRegister(38242, 10),
    rn.STORAGE_UNIT_1_BATTERY_PACK_2_FIRMWARE_VERSION: StringRegister(38252, 15),
    rn.STORAGE_UNIT_1_BATTERY_PACK_2_WORKING_STATUS: U16Register(None, 1, 38270, 1),
    rn.STORAGE_UNIT_1_BATTERY_PACK_2_STATE_OF_CAPACITY: U16Register("%", 10, 38271, 1),
    rn.STORAGE_UNIT_1_BATTERY_PACK_2_CHARGE_DISCHARGE_POWER: I32Register("W", 1, 38275, 2),
    rn.STORAGE_UNIT_1_BATTERY_PACK_2_VOLTAGE: U16Register("V", 10, 38277, 1),
    rn.STORAGE_UNIT_1_BATTERY_PACK_2_CURRENT: I16Register("A", 10, 38278, 1),
    rn.STORAGE_UNIT_1_BATTERY_PACK_2_TOTAL_CHARGE: U32Register("kWh", 100, 38280, 2),
    rn.STORAGE_UNIT_1_BATTERY_PACK_2_TOTAL_DISCHARGE: U32Register("kWh", 100, 38282, 2),
    rn.STORAGE_UNIT_1_BATTERY_PACK_3_SERIAL_NUMBER: StringRegister(38284, 10),
    rn.STORAGE_UNIT_1_BATTERY_PACK_3_FIRMWARE_VERSION: StringRegister(38294, 15),
    rn.STORAGE_UNIT_1_BATTERY_PACK_3_WORKING_STATUS: U16Register(None, 1, 38312, 1),
    rn.STORAGE_UNIT_1_BATTERY_PACK_3_STATE_OF_CAPACITY: U16Register("%", 10, 38313, 1),
    rn.STORAGE_UNIT_1_BATTERY_PACK_3_CHARGE_DISCHARGE_POWER: I32Register("W", 1, 38317, 2),
    rn.STORAGE_UNIT_1_BATTERY_PACK_3_VOLTAGE: U16Register("V", 10, 38319, 1),
    rn.STORAGE_UNIT_1_BATTERY_PACK_3_CURRENT: I16Register("A", 10, 38320, 1),
    rn.STORAGE_UNIT_1_BATTERY_PACK_3_TOTAL_CHARGE: U32Register("kWh", 100, 38322, 2),
    rn.STORAGE_UNIT_1_BATTERY_PACK_3_TOTAL_DISCHARGE: U32Register("kWh", 100, 38324, 2),
    rn.STORAGE_UNIT_2_BATTERY_PACK_1_SERIAL_NUMBER: StringRegister(38326, 10),
    rn.STORAGE_UNIT_2_BATTERY_PACK_1_FIRMWARE_VERSION: StringRegister(38336, 15),
    rn.STORAGE_UNIT_2_BATTERY_PACK_1_WORKING_STATUS: U16Register(None, 1, 38354, 1),
    rn.STORAGE_UNIT_2_BATTERY_PACK_1_STATE_OF_CAPACITY: U16Register("%", 10, 38355, 1),
    rn.STORAGE_UNIT_2_BATTERY_PACK_1_CHARGE_DISCHARGE_POWER: I32Register("W", 1, 38359, 2),
    rn.STORAGE_UNIT_2_BATTERY_PACK_1_VOLTAGE: U16Register("V", 10, 38361, 1),
    rn.STORAGE_UNIT_2_BATTERY_PACK_1_CURRENT: I16Register("A", 10, 38362, 1),
    rn.STORAGE_UNIT_2_BATTERY_PACK_1_TOTAL_CHARGE: U32Register("kWh", 100, 38364, 2),
    rn.STORAGE_UNIT_2_BATTERY_PACK_1_TOTAL_DISCHARGE: U32Register("kWh", 100, 38366, 2),
    rn.STORAGE_UNIT_2_BATTERY_PACK_2_SERIAL_NUMBER: StringRegister(38368, 10),
    rn.STORAGE_UNIT_2_BATTERY_PACK_2_FIRMWARE_VERSION: StringRegister(38378, 15),
    rn.STORAGE_UNIT_2_BATTERY_PACK_2_WORKING_STATUS: U16Register(None, 1, 38396, 1),
    rn.STORAGE_UNIT_2_BATTERY_PACK_2_STATE_OF_CAPACITY: U16Register("%", 10, 38397, 1),
    rn.STORAGE_UNIT_2_BATTERY_PACK_2_CHARGE_DISCHARGE_POWER: I32Register("W", 1, 38401, 2),
    rn.STORAGE_UNIT_2_BATTERY_PACK_2_VOLTAGE: U16Register("V", 10, 38403, 1),
    rn.STORAGE_UNIT_2_BATTERY_PACK_2_CURRENT: I16Register("A", 10, 38404, 1),
    rn.STORAGE_UNIT_2_BATTERY_PACK_2_TOTAL_CHARGE: U32Register("kWh", 100, 38406, 2),
    rn.STORAGE_UNIT_2_BATTERY_PACK_2_TOTAL_DISCHARGE: U32Register("kWh", 100, 38408, 2),
    rn.STORAGE_UNIT_2_BATTERY_PACK_3_SERIAL_NUMBER: StringRegister(38410, 10),
    rn.STORAGE_UNIT_2_BATTERY_PACK_3_FIRMWARE_VERSION: StringRegister(38420, 15),
    rn.STORAGE_UNIT_2_BATTERY_PACK_3_WORKING_STATUS: U16Register(None, 1, 38438, 1),
    rn.STORAGE_UNIT_2_BATTERY_PACK_3_STATE_OF_CAPACITY: U16Register("%", 10, 38439, 1),
    rn.STORAGE_UNIT_2_BATTERY_PACK_3_CHARGE_DISCHARGE_POWER: I32Register("W", 1, 38443, 2),
    rn.STORAGE_UNIT_2_BATTERY_PACK_3_VOLTAGE: U16Register("V", 10, 38445, 1),
    rn.STORAGE_UNIT_2_BATTERY_PACK_3_CURRENT: I16Register("A", 10, 38446, 1),
    rn.STORAGE_UNIT_2_BATTERY_PACK_3_TOTAL_CHARGE: U32Register("kWh", 100, 38448, 2),
    rn.STORAGE_UNIT_2_BATTERY_PACK_3_TOTAL_DISCHARGE: U32Register("kWh", 100, 38450, 2),
    rn.STORAGE_UNIT_1_BATTERY_PACK_1_MAXIMUM_TEMPERATURE: I16Register("°C", 10, 38452, 1),
    rn.STORAGE_UNIT_1_BATTERY_PACK_1_MINIMUM_TEMPERATURE: I16Register("°C", 10, 38453, 1),
    rn.STORAGE_UNIT_1_BATTERY_PACK_2_MAXIMUM_TEMPERATURE: I16Register("°C", 10, 38454, 1),
    rn.STORAGE_UNIT_1_BATTERY_PACK_2_MINIMUM_TEMPERATURE: I16Register("°C", 10, 38455, 1),
    rn.STORAGE_UNIT_1_BATTERY_PACK_3_MAXIMUM_TEMPERATURE: I16Register("°C", 10, 38456, 1),
    rn.STORAGE_UNIT_1_BATTERY_PACK_3_MINIMUM_TEMPERATURE: I16Register("°C", 10, 38457, 1),
    rn.STORAGE_UNIT_2_BATTERY_PACK_1_MAXIMUM_TEMPERATURE: I16Register("°C", 10, 38458, 1),
    rn.STORAGE_UNIT_2_BATTERY_PACK_1_MINIMUM_TEMPERATURE: I16Register("°C", 10, 38459, 1),
    rn.STORAGE_UNIT_2_BATTERY_PACK_2_MAXIMUM_TEMPERATURE: I16Register("°C", 10, 38460, 1),
    rn.STORAGE_UNIT_2_BATTERY_PACK_2_MINIMUM_TEMPERATURE: I16Register("°C", 10, 38461, 1),
    rn.STORAGE_UNIT_2_BATTERY_PACK_3_MAXIMUM_TEMPERATURE: I16Register("°C", 10, 38462, 1),
    rn.STORAGE_UNIT_2_BATTERY_PACK_3_MINIMUM_TEMPERATURE: I16Register("°C", 10, 38463, 1),
    rn.STORAGE_UNIT_1_PRODUCT_MODEL: U16Register(rv.StorageProductModel, 1, 47000, 1),
    rn.STORAGE_WORKING_MODE_A: I16Register(rv.STORAGE_WORKING_MODES_A, 1, 47004, 1),
    rn.STORAGE_TIME_OF_USE_PRICE: I16Register(rv.STORAGE_TOU_PRICE, 1, 47027, 1),
    rn.STORAGE_TIME_OF_USE_PRICE_PERIODS: TimeOfUseRegisters(47028, 41),
    rn.STORAGE_LCOE: U32Register(None, 1000, 47069, 2),
    rn.STORAGE_MAXIMUM_CHARGING_POWER: U32Register("W", 1, 47075, 2),
    rn.STORAGE_MAXIMUM_DISCHARGING_POWER: U32Register("W", 1, 47077, 2),
    rn.STORAGE_POWER_LIMIT_GRID_TIED_POINT: I32Register("W", 1, 47079, 2),
    rn.STORAGE_CHARGING_CUTOFF_CAPACITY: U16Register("%", 10, 47081, 1),
    rn.STORAGE_DISCHARGING_CUTOFF_CAPACITY: U16Register("%", 10, 47082, 1),
    rn.STORAGE_FORCED_CHARGING_AND_DISCHARGING_PERIOD: U16Register("min", 1, 47083, 1),
    rn.STORAGE_FORCED_CHARGING_AND_DISCHARGING_POWER: I32Register("min", 1, 47084, 2),
    rn.STORAGE_WORKING_MODE_SETTINGS: U16Register(rv.STORAGE_WORKING_MODES_C, 1, 47086, 1),
    rn.STORAGE_CHARGE_FROM_GRID_FUNCTION: U16Register(rv.STORAGE_CHARGE_FROM_GRID, 1, 47087, 1),
    rn.STORAGE_GRID_CHARGE_CUTOFF_STATE_OF_CHARGE: U16Register("%", 1, 47088, 1),
    rn.STORAGE_UNIT_2_PRODUCT_MODEL: U16Register(rv.StorageProductModel, 1, 47089, 1),
    rn.STORAGE_BACKUP_POWER_STATE_OF_CHARGE: U16Register("%", 10, 47102, 1),
    rn.STORAGE_UNIT_1_NO: U16Register(None, 1, 47107, 1),
    rn.STORAGE_UNIT_2_NO: U16Register(None, 1, 47108, 1),
    rn.STORAGE_FIXED_CHARGING_AND_DISCHARGING_PERIODS: ChargeDischargePeriodRegisters(47200, 41),
    rn.STORAGE_POWER_OF_CHARGE_FROM_GRID: U32Register("W", 1, 47242, 2),
    rn.STORAGE_MAXIMUM_POWER_OF_CHARGE_FROM_GRID: U32Register("W", 1, 47244, 2),
    rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_SETTING_MODE: U16Register(None, 1, 47246, 2),
    rn.STORAGE_FORCIBLE_CHARGE_POWER: U32Register(None, 1, 47247, 2),
    rn.STORAGE_FORCIBLE_DISCHARGE_POWER: U32Register(None, 1, 47249, 2),
    rn.STORAGE_TIME_OF_USE_CHARGING_AND_DISCHARGING_PERIODS: TimeOfUseRegisters(47255, 43),
    rn.STORAGE_EXCESS_PV_ENERGY_USE_IN_TOU: U16Register(rv.STORAGE_EXCESS_PV_ENERGY_USE_IN_TOU, 1, 47299, 1),
    rn.DONGLE_PLANT_MAXIMUM_CHARGE_FROM_GRID_POWER: U32Register("W", 1, 47590, 2),
    rn.BACKUP_SWITCH_TO_OFF_GRID: U16Register(None, 1, 47604, 1),
    rn.BACKUP_VOLTAGE_INDEPENDEND_OPERATION: U16Register(rv.BACKUP_VOLTAGE_INDEPENDENT_OPERATION, 1, 47604, 1),
    rn.STORAGE_UNIT_1_PACK_1_NO: U16Register(None, 1, 47750, 1),
    rn.STORAGE_UNIT_1_PACK_2_NO: U16Register(None, 1, 47751, 1),
    rn.STORAGE_UNIT_1_PACK_3_NO: U16Register(None, 1, 47752, 1),
    rn.STORAGE_UNIT_2_PACK_1_NO: U16Register(None, 1, 47753, 1),
    rn.STORAGE_UNIT_2_PACK_2_NO: U16Register(None, 1, 47754, 1),
    rn.STORAGE_UNIT_2_PACK_3_NO: U16Register(None, 1, 47755, 1),
}

REGISTERS.update(BATTERY_REGISTERS)

METER_REGISTERS = {
    rn.METER_STATUS: U16Register(rv.METER_STATUS, 1, 37100, 1),
    rn.GRID_A_VOLTAGE: I32Register("V", 10, 37101, 2),
    rn.GRID_B_VOLTAGE: I32Register("V", 10, 37103, 2),
    rn.GRID_C_VOLTAGE: I32Register("V", 10, 37105, 2),
    rn.ACTIVE_GRID_A_CURRENT: I32Register("I", 100, 37107, 2),
    rn.ACTIVE_GRID_B_CURRENT: I32Register("I", 100, 37109, 2),
    rn.ACTIVE_GRID_C_CURRENT: I32Register("I", 100, 37111, 2),
    rn.POWER_METER_ACTIVE_POWER: I32Register("W", 1, 37113, 2),
    rn.POWER_METER_REACTIVE_POWER: I32Register("Var", 1, 37115, 2),
    rn.ACTIVE_GRID_POWER_FACTOR: I16Register(None, 1000, 37117, 1),
    rn.ACTIVE_GRID_FREQUENCY: I16Register("Hz", 100, 37118, 1),
    rn.GRID_EXPORTED_ENERGY: I32Register("kWh", 100, 37119, 2),
    rn.GRID_ACCUMULATED_ENERGY: U32Register("kWh", 100, 37121, 2),
    rn.GRID_ACCUMULATED_REACTIVE_POWER: U32Register("kVarh", 100, 37123, 2),
    rn.METER_TYPE: U16Register(rv.METER_TYPE, 1, 37125, 1),
    rn.ACTIVE_GRID_A_B_VOLTAGE: I32Register("V", 10, 37126, 2),
    rn.ACTIVE_GRID_B_C_VOLTAGE: I32Register("V", 10, 37128, 2),
    rn.ACTIVE_GRID_C_A_VOLTAGE: I32Register("V", 10, 37130, 2),
    rn.ACTIVE_GRID_A_POWER: I32Register("W", 1, 37132, 2),
    rn.ACTIVE_GRID_B_POWER: I32Register("W", 1, 37134, 2),
    rn.ACTIVE_GRID_C_POWER: I32Register("W", 1, 37136, 2),
}

REGISTERS.update(METER_REGISTERS)