#    emulated_device.py
#        Emulate a device that is compiled with the C++ lib.
#        For unit testing purpose
#
#   - License : MIT - See LICENSE file.
#   - Project :  Scrutiny Debugger (github.com/scrutinydebugger/scrutiny-python)
#
#   Copyright (c) 2021-2022 Scrutiny Debugger

import threading
import time
import logging
import random
import traceback
import collections
from dataclasses import dataclass

from scrutiny.core.codecs import Encodable
import scrutiny.server.protocol.commands as cmd
from scrutiny.server.device.links.dummy_link import DummyLink, ThreadSafeDummyLink
from scrutiny.server.protocol import Protocol, Request, Response, ResponseCode
import scrutiny.server.protocol.typing as protocol_typing
from scrutiny.core.memory_content import MemoryContent
from scrutiny.core.basic_types import RuntimePublishedValue, EmbeddedDataType
import scrutiny.server.datalogging.definitions as datalogging
from scrutiny.core.codecs import *
from scrutiny.server.device.device_info import ExecLoop, VariableFreqLoop, FixedFreqLoop, ExecLoopType
from scrutiny.server.protocol.crc32 import crc32

from typing import List, Dict, Optional, Union, Any, Tuple, TypedDict, cast, Set, Deque


class RequestLogRecord:
    __slots__ = ('request', 'response')

    request: Request
    response: Response

    def __init__(self, request, response):
        self.request = request
        self.response = response


class RPVValuePair(TypedDict):
    definition: RuntimePublishedValue
    value: Encodable


class EmulatedTimebase:
    timestamp_100ns: int
    last_time: float

    def __init__(self):
        self.timestamp_100ns = int(0)
        self.last_time = time.time()

    def process(self):
        t = time.time()
        dt = t - self.last_time
        self.timestamp_100ns += int(round(dt * 1e7))
        self.timestamp_100ns = self.timestamp_100ns & 0xFFFFFFFF
        self.last_time = t

    def get_timestamp(self):
        return self.timestamp_100ns


class DataloggerEmulator:

    @dataclass
    class MemorySample:
        data: bytes

    @dataclass
    class RPVSample:
        data: Encodable
        datatype: EmbeddedDataType

    @dataclass
    class TimeSample:
        data: int

    SampleType = Union["DataloggerEmulator.MemorySample", "DataloggerEmulator.RPVSample", "DataloggerEmulator.TimeSample"]

    class Encoder(ABC):
        byte_counter: int
        entry_counter: int

        @abstractmethod
        def __init__(self, rpv_map: Dict[int, RuntimePublishedValue], buffer: bytearray, buffer_size: int):
            raise NotImplementedError("Abstract method")

        @abstractmethod
        def configure(self, config: datalogging.Configuration) -> None:
            raise NotImplementedError("Abstract method")

        @abstractmethod
        def encode_samples(self, samples: List["DataloggerEmulator.SampleType"]) -> None:
            raise NotImplementedError("Abstract method")

        @abstractmethod
        def get_raw_data(self) -> bytes:
            raise NotImplementedError("Abstract method")

        @abstractmethod
        def get_entry_count(self) -> int:
            raise NotImplementedError("Abstract method")

        def reset_write_counters(self) -> None:
            self.byte_counter = 0
            self.entry_counter = 0

        def get_byte_counter(self) -> int:
            return self.byte_counter

        def get_entry_counter(self) -> int:
            return self.entry_counter

    class RawEncoder(Encoder):
        buffer_size: int
        write_cursor: int
        read_cursor: int
        config: Optional[datalogging.Configuration]
        entry_size: int
        rpv_map: Dict[int, RuntimePublishedValue]
        data_deque: Deque[bytearray]

        def __init__(self, rpv_map: Dict[int, RuntimePublishedValue], buffer_size: int):
            self.buffer_size = buffer_size
            self.write_cursor = 0
            self.read_cursor = 0
            self.config = None
            self.entry_size = 0
            self.rpv_map = rpv_map
            self.data_deque = collections.deque(maxlen=0)
            self.reset_write_counters()

        def configure(self, config: datalogging.Configuration) -> None:
            self.config = config
            self.entry_size = 0
            for signal in config.get_signals():
                if isinstance(signal, datalogging.TimeLoggableSignal):
                    self.entry_size += 4
                elif isinstance(signal, datalogging.RPVLoggableSignal):
                    if signal.rpv_id not in self.rpv_map:
                        raise ValueError('RPV ID 0x%04X not in RPV map' % signal.rpv_id)
                    self.entry_size += self.rpv_map[signal.rpv_id].datatype.get_size_byte()
                elif isinstance(signal, datalogging.MemoryLoggableSignal):
                    self.entry_size += signal.size
                else:
                    raise NotImplementedError("Unknown signal type")
            max_entry = self.buffer_size // self.entry_size   # integer division
            self.data_deque = collections.deque(maxlen=max_entry)

        def encode_samples(self, samples: List["DataloggerEmulator.SampleType"]) -> None:
            data = bytearray()
            for sample in samples:
                if isinstance(sample, DataloggerEmulator.TimeSample):
                    data += struct.pack('>L', sample.data)
                elif isinstance(sample, DataloggerEmulator.RPVSample):
                    codec = Codecs.get(sample.datatype, Endianness.Big)
                    data += codec.encode(sample.data)
                elif isinstance(sample, DataloggerEmulator.MemorySample):
                    data += sample.data

            if len(data) != self.entry_size:
                raise ValueError("Amount of data to encode doesn't match the given configuration. Size mismatch in block size")

            self.entry_counter += 1
            self.byte_counter += len(data)
            self.data_deque.append(data)

        def get_raw_data(self) -> bytes:
            output_data = bytearray()
            for block in self.data_deque:
                output_data += block
            return bytes(output_data)

        def get_entry_count(self) -> int:
            return len(self.data_deque)

    buffer_size: int
    config: Optional[datalogging.Configuration]
    state: datalogging.DataloggerStatus
    remaining_samples: int
    timebase: EmulatedTimebase
    trigger_cmt_last_val: Encodable
    last_trigger_condition_result: bool
    trigger_rising_edge_timestamp: Optional[float]
    trigger_fulfilled_timestamp: float
    encoding: datalogging.Encoding
    encoder: "DataloggerEmulator.Encoder"
    config_id: int
    target_byte_count_after_trigger: int
    byte_count_at_trigger: int
    entry_counter_at_trigger: int
    acquisition_id: int

    def __init__(self, device: "EmulatedDevice", buffer_size: int, encoding: datalogging.Encoding = datalogging.Encoding.RAW):
        self.device = device
        self.buffer_size = buffer_size
        self.encoding = encoding
        self.reset()

    def reset(self) -> None:
        if self.encoding == datalogging.Encoding.RAW:
            self.encoder = DataloggerEmulator.RawEncoder(rpv_map=self.device.get_rpv_definition_map(), buffer_size=self.buffer_size)
        else:
            raise NotImplementedError("Unsupported encoding %s" % self.encoding)

        self.config = None
        self.state = datalogging.DataloggerStatus.IDLE
        self.remaining_samples = 0
        self.timebase = EmulatedTimebase()
        self.trigger_cmt_last_val = 0
        self.last_trigger_condition_result = False
        self.trigger_rising_edge_timestamp = None
        self.trigger_fulfilled = False
        self.trigger_fulfilled_timestamp = 0
        self.config_id = 0
        self.target_byte_count_after_trigger = 0
        self.byte_count_at_trigger = 0
        self.entry_counter_at_trigger = 0
        self.acquisition_id = 0

    def configure(self, config_id: int, config: datalogging.Configuration) -> None:
        self.reset()
        self.config = config
        self.config_id = config_id
        self.encoder.configure(config)

        self.state = datalogging.DataloggerStatus.CONFIGURED

    def arm_trigger(self):
        if self.state in [datalogging.DataloggerStatus.CONFIGURED, datalogging.DataloggerStatus.ACQUISITION_COMPLETED]:
            self.state = datalogging.DataloggerStatus.ARMED

    def disarm_trigger(self):
        if self.state in [datalogging.DataloggerStatus.ARMED, datalogging.DataloggerStatus.ACQUISITION_COMPLETED]:
            self.state = datalogging.DataloggerStatus.CONFIGURED

    def set_error(self) -> None:
        self.state = datalogging.DataloggerStatus.ERROR

    def triggered(self) -> bool:
        return self.trigger_fulfilled

    def read_samples(self) -> List[SampleType]:
        if self.config is None:
            raise ValueError('Invalid configuration')

        samples: List["DataloggerEmulator".SampleType] = []
        for signal in self.config.get_signals():
            if isinstance(signal, datalogging.MemoryLoggableSignal):
                samples.append(DataloggerEmulator.MemorySample(data=self.device.read_memory(signal.address, signal.size)))
            elif isinstance(signal, datalogging.RPVLoggableSignal):
                value = self.device.read_rpv(signal.rpv_id)
                datatype = self.device.get_rpv_definition(signal.rpv_id).datatype
                samples.append(DataloggerEmulator.RPVSample(data=value, datatype=datatype))
            elif isinstance(signal, datalogging.TimeLoggableSignal):
                samples.append(DataloggerEmulator.TimeSample(self.timebase.get_timestamp()))
            else:
                raise ValueError('Unknown type of signal')
        return samples

    def fetch_operand(self, operand: datalogging.Operand) -> Encodable:
        if isinstance(operand, datalogging.LiteralOperand):
            return operand.value

        if isinstance(operand, datalogging.RPVOperand):
            return self.device.read_rpv(operand.rpv_id)

        if isinstance(operand, datalogging.VarOperand):
            data = self.device.read_memory(operand.address, operand.datatype.get_size_byte())
            codec = Codecs.get(operand.datatype, Endianness.Little)
            return codec.decode(data)

        if isinstance(operand, datalogging.VarBitOperand):
            mask = 0
            for i in range(operand.bitoffset, operand.bitoffset + operand.bitsize):
                mask |= (1 << i)
            mask_codec = UIntCodec(operand.datatype.get_size_byte(), Endianness.Little)
            data = self.device.read_memory(operand.address, operand.datatype.get_size_byte())
            codec = Codecs.get(operand.datatype, Endianness.Little)
            return codec.decode(data, mask_codec.encode(mask))

        raise ValueError('Unknown operand type')

    def check_trigger(self):
        if self.config is None:
            return False

        output = False
        val = self.check_trigger_condition()

        if not self.last_trigger_condition_result and val:
            self.trigger_rising_edge_timestamp = time.time()

        if val:
            if time.time() - self.trigger_rising_edge_timestamp > self.config.trigger_hold_time:
                output = True

        self.last_trigger_condition_result = val

        return output

    def check_trigger_condition(self) -> bool:
        if self.config is None:
            return False

        operands = self.config.trigger_condition.operands
        if self.config.trigger_condition.condition_id == datalogging.TriggerConditionID.AlwaysTrue:
            return True

        if self.config.trigger_condition.condition_id == datalogging.TriggerConditionID.Equal:
            return self.fetch_operand(operands[0]) == self.fetch_operand(operands[1])

        if self.config.trigger_condition.condition_id == datalogging.TriggerConditionID.NotEqual:
            return self.fetch_operand(operands[0]) != self.fetch_operand(operands[1])

        if self.config.trigger_condition.condition_id == datalogging.TriggerConditionID.GreaterThan:
            return self.fetch_operand(operands[0]) > self.fetch_operand(operands[1])

        if self.config.trigger_condition.condition_id == datalogging.TriggerConditionID.GreaterOrEqualThan:
            return self.fetch_operand(operands[0]) >= self.fetch_operand(operands[1])

        if self.config.trigger_condition.condition_id == datalogging.TriggerConditionID.LessThan:
            return self.fetch_operand(operands[0]) < self.fetch_operand(operands[1])

        if self.config.trigger_condition.condition_id == datalogging.TriggerConditionID.LessOrEqualThan:
            return self.fetch_operand(operands[0]) <= self.fetch_operand(operands[1])

        if self.config.trigger_condition.condition_id == datalogging.TriggerConditionID.IsWithin:
            return abs(self.fetch_operand(operands[0]) - self.fetch_operand(operands[1])) <= abs(self.fetch_operand(operands[2]))

        if self.config.trigger_condition.condition_id == datalogging.TriggerConditionID.ChangeMoreThan:
            v = self.fetch_operand(operands[0])
            diff = v - self.trigger_cmt_last_val
            delta = self.fetch_operand(operands[1])
            output = False
            if delta >= 0:
                output = diff >= delta
            else:
                output = diff <= delta

            self.trigger_cmt_last_val = v
            return output

        return False

    def process(self) -> None:
        self.timebase.process()

        if self.state in [datalogging.DataloggerStatus.CONFIGURED, datalogging.DataloggerStatus.ARMED]:
            self.encoder.encode_samples(self.read_samples())

        if self.state == datalogging.DataloggerStatus.ARMED:
            assert self.config is not None
            if not self.trigger_fulfilled:
                if self.check_trigger():
                    self.trigger_fulfilled = True
                    self.trigger_fulfilled_timestamp = time.time()
                    self.byte_count_at_trigger = self.encoder.get_byte_counter()
                    self.entry_counter_at_trigger = self.encoder.get_entry_counter()
                    self.target_byte_count_after_trigger = self.byte_count_at_trigger + round((1.0 - self.config.probe_location) * self.buffer_size)

            if self.trigger_fulfilled:
                probe_location_ok = self.encoder.get_byte_counter() >= self.target_byte_count_after_trigger
                timed_out = (time.time() - self.trigger_fulfilled_timestamp) >= self.config.timeout
                if probe_location_ok or timed_out:
                    self.state = datalogging.DataloggerStatus.ACQUISITION_COMPLETED
                    self.acquisition_id += 1

        else:
            pass

    def get_acquisition_data(self) -> bytes:
        return self.encoder.get_raw_data()

    def get_buffer_size(self) -> int:
        return self.buffer_size

    def get_encoding(self) -> datalogging.Encoding:
        return self.encoding

    def in_error(self) -> bool:
        return self.state == datalogging.DataloggerStatus.ERROR

    def get_acquisition_id(self) -> int:
        return self.acquisition_id

    def get_config_id(self) -> int:
        return self.config_id

    def get_nb_points(self) -> int:
        return self.encoder.get_entry_count()

    def get_points_after_trigger(self) -> int:
        # The validity of this depends on the datalogger capacity to stop acquiring at the right moment.
        # if it continues acquiring and the encoder discards data, this value becomes invalid
        return self.encoder.get_entry_counter() - self.entry_counter_at_trigger


class EmulatedDevice:
    logger: logging.Logger
    link: Union[DummyLink, ThreadSafeDummyLink]
    firmware_id: bytes
    request_history: List[RequestLogRecord]
    protocol: Protocol
    comm_enabled: bool
    connected: bool
    request_shutdown: bool
    thread_started_event: threading.Event
    thread: Optional[threading.Thread]
    max_rx_data_size: int
    max_tx_data_size: int
    max_bitrate_bps: int
    heartbeat_timeout_us: int
    rx_timeout_us: int
    address_size_bits: int
    supported_features: Dict[str, bool]
    forbidden_regions: List[Dict[str, int]]
    readonly_regions: List[Dict[str, int]]
    session_id: Optional[int]
    memory: MemoryContent
    memory_lock: threading.Lock
    rpv_lock: threading.Lock
    rpvs: Dict[int, RPVValuePair]
    datalogger: DataloggerEmulator

    datalogging_read_in_progress: bool
    datalogging_read_cursor: int
    datalogging_read_rolling_counter: int
    loops: List[ExecLoop]

    def __init__(self, link):
        if not isinstance(link, DummyLink) and not isinstance(link, ThreadSafeDummyLink):
            raise ValueError('EmulatedDevice expects a DummyLink object')
        self.logger = logging.getLogger(self.__class__.__name__)
        self.link = link    # Preopened link.
        self.firmware_id = bytes(range(16))
        self.request_history = []
        self.protocol = Protocol(1, 0)

        self.comm_enabled = True
        self.connected = False
        self.request_shutdown = False
        self.thread_started_event = threading.Event()
        self.thread = None

        self.max_rx_data_size = 128        # Rx buffer size max. Server should make sure the request won't overflow
        self.max_tx_data_size = 128        # Tx buffer size max. Server should make sure the response won't overflow
        self.max_bitrate_bps = 100000    # Maximum bitrate supported by the device. Will gently ask the server to not go faster than that
        self.heartbeat_timeout_us = 3000000   # Will destroy session if no heartbeat is received at this rate (microseconds)
        self.rx_timeout_us = 50000     # For byte chunk reassembly (microseconds)
        self.address_size_bits = 32

        self.session_id = None
        self.memory = MemoryContent()
        self.memory_lock = threading.Lock()
        self.rpv_lock = threading.Lock()

        self.supported_features = {
            'memory_read': True,
            'memory_write': True,
            'datalogging': False,
            'user_command': False,
            '_64bits': False,
        }

        self.rpvs = {
            0x1000: {'definition': RuntimePublishedValue(id=0x1000, datatype=EmbeddedDataType.float64), 'value': 0.0},
            0x1001: {'definition': RuntimePublishedValue(id=0x1001, datatype=EmbeddedDataType.float32), 'value': 3.1415926},
            0x1002: {'definition': RuntimePublishedValue(id=0x1002, datatype=EmbeddedDataType.uint16), 'value': 0x1234},
            0x1003: {'definition': RuntimePublishedValue(id=0x1003, datatype=EmbeddedDataType.sint8), 'value': -65},
            0x1004: {'definition': RuntimePublishedValue(id=0x1004, datatype=EmbeddedDataType.boolean), 'value': True}
        }

        self.forbidden_regions = [
            {'start': 0x100, 'end': 0x1FF},
            {'start': 0x1000, 'end': 0x10FF}]

        self.readonly_regions = [
            {'start': 0x200, 'end': 0x2FF},
            {'start': 0x800, 'end': 0x8FF},
            {'start': 0x900, 'end': 0x9FF}]

        self.protocol.configure_rpvs([self.rpvs[id]['definition'] for id in self.rpvs])

        self.datalogger = DataloggerEmulator(self, 100)

        self.loops = [
            FixedFreqLoop(1000, name='1KHz'),
            FixedFreqLoop(10000, name='10KHz'),
            VariableFreqLoop(name='Variable Freq 1'),
            VariableFreqLoop(name='Idle Loop', support_datalogging=False)
        ]

        self.datalogging_read_in_progress = False
        self.datalogging_read_cursor = 0
        self.datalogging_read_rolling_counter = 0

    def thread_task(self) -> None:
        self.thread_started_event.set()
        while not self.request_shutdown:
            request = None
            try:
                request = self.read()
            except Exception as e:
                self.logger.error('Error decoding request. %s' % str(e))

            if request is not None:
                response: Optional[Response] = None
                self.logger.debug('Received a request : %s' % request)
                try:
                    response = self.process_request(request)
                    if response is not None:
                        self.logger.debug('Responding %s' % response)
                        self.send(response)
                except Exception as e:
                    self.logger.error('Exception while processing Request %s. Error is : %s' % (str(request), str(e)))
                    self.logger.debug(traceback.format_exc())

                self.request_history.append(RequestLogRecord(request=request, response=response))

            time.sleep(0.01)

    def process_request(self, req: Request) -> Optional[Response]:
        response = None
        if req.size() > self.max_rx_data_size:
            self.logger.error("Request doesn't fit buffer. Dropping %s" % req)
            return None  # drop

        data = self.protocol.parse_request(req)  # can throw

        if not self.connected:
            # We only respond to DISCOVER and CONNECT request while not session is active
            must_process = req.command == cmd.CommControl and (
                req.subfn == cmd.CommControl.Subfunction.Discover.value or req.subfn == cmd.CommControl.Subfunction.Connect.value)
            if not must_process:
                self.logger.warning('Received a request while no session was active. %s' % req)
                return None

        if req.command == cmd.CommControl:
            response = self.process_comm_control(req, data)
        elif req.command == cmd.GetInfo:
            response = self.process_get_info(req, data)
        elif req.command == cmd.MemoryControl:
            response = self.process_memory_control(req, data)
        elif req.command == cmd.DatalogControl:
            response = self.process_datalog_control(req, data)
        elif req.command == cmd.DummyCommand:
            response = self.process_dummy_cmd(req, data)

        else:
            self.logger.error('Unsupported command : %s' % str(req.command.__name__))

        return response

    # ===== [CommControl] ======
    def process_comm_control(self, req: Request, data: protocol_typing.RequestData) -> Optional[Response]:
        response = None
        subfunction = cmd.CommControl.Subfunction(req.subfn)
        session_id_str = '0x%08X' % self.session_id if self.session_id is not None else 'None'
        if subfunction == cmd.CommControl.Subfunction.Discover:
            data = cast(protocol_typing.Request.CommControl.Discover, data)
            if data['magic'] == cmd.CommControl.DISCOVER_MAGIC:
                response = self.protocol.respond_comm_discover(self.firmware_id, 'EmulatedDevice')
            else:
                self.logger.error('Received as Discover request with invalid payload')

        elif subfunction == cmd.CommControl.Subfunction.Connect:
            data = cast(protocol_typing.Request.CommControl.Connect, data)
            if data['magic'] == cmd.CommControl.CONNECT_MAGIC:
                if not self.connected:
                    self.initiate_session()
                    assert self.session_id is not None  # for mypy
                    response = self.protocol.respond_comm_connect(self.session_id)
                else:
                    response = Response(cmd.CommControl, subfunction, ResponseCode.Busy)
            else:
                self.logger.error('Received as Connect request with invalid payload')

        elif subfunction == cmd.CommControl.Subfunction.Heartbeat:
            data = cast(protocol_typing.Request.CommControl.Heartbeat, data)
            if data['session_id'] == self.session_id:
                challenge_response = self.protocol.heartbeat_expected_challenge_response(data['challenge'])
                response = self.protocol.respond_comm_heartbeat(self.session_id, challenge_response)
            else:
                self.logger.warning('Received a Heartbeat request for session ID 0x%08X, but my active session ID is %s' %
                                    (data['session_id'], session_id_str))
                response = Response(cmd.CommControl, subfunction, ResponseCode.InvalidRequest)

        elif subfunction == cmd.CommControl.Subfunction.Disconnect:
            data = cast(protocol_typing.Request.CommControl.Disconnect, data)
            if data['session_id'] == self.session_id:
                self.destroy_session()
                response = self.protocol.respond_comm_disconnect()
            else:
                self.logger.warning('Received a Disconnect request for session ID 0x%08X, but my active session ID is %s' %
                                    (data['session_id'], session_id_str))
                response = Response(cmd.CommControl, subfunction, ResponseCode.InvalidRequest)

        elif subfunction == cmd.CommControl.Subfunction.GetParams:
            response = self.protocol.respond_comm_get_params(
                max_rx_data_size=self.max_rx_data_size,
                max_tx_data_size=self.max_tx_data_size,
                max_bitrate_bps=self.max_bitrate_bps,
                heartbeat_timeout_us=self.heartbeat_timeout_us,
                rx_timeout_us=self.rx_timeout_us,
                address_size_byte=int(self.address_size_bits / 8)
            )

        else:
            self.logger.error('Unsupported subfunction %s for command : %s' % (subfunction, req.command.__name__))

        return response

    # ===== [GetInfo] ======
    def process_get_info(self, req: Request, data: protocol_typing.RequestData) -> Optional[Response]:
        response = None
        subfunction = cmd.GetInfo.Subfunction(req.subfn)
        if subfunction == cmd.GetInfo.Subfunction.GetProtocolVersion:
            response = self.protocol.respond_protocol_version(self.protocol.version_major, self.protocol.version_minor)

        elif subfunction == cmd.GetInfo.Subfunction.GetSupportedFeatures:
            response = self.protocol.respond_supported_features(**self.supported_features)

        elif subfunction == cmd.GetInfo.Subfunction.GetSpecialMemoryRegionCount:
            response = self.protocol.respond_special_memory_region_count(len(self.readonly_regions), len(self.forbidden_regions))

        elif subfunction == cmd.GetInfo.Subfunction.GetSpecialMemoryRegionLocation:
            data = cast(protocol_typing.Request.GetInfo.GetSpecialMemoryRegionLocation, data)
            if data['region_type'] == cmd.GetInfo.MemoryRangeType.ReadOnly:
                region_list = self.readonly_regions
            elif data['region_type'] == cmd.GetInfo.MemoryRangeType.Forbidden:
                region_list = self.forbidden_regions
            else:
                return Response(req.command, subfunction, ResponseCode.InvalidRequest)

            if data['region_index'] >= len(region_list):
                return Response(req.command, subfunction, ResponseCode.Overflow)

            region = region_list[data['region_index']]
            response = self.protocol.respond_special_memory_region_location(data['region_type'], data['region_index'], region['start'], region['end'])

        elif subfunction == cmd.GetInfo.Subfunction.GetRuntimePublishedValuesCount:
            response = self.protocol.respond_get_rpv_count(count=len(self.rpvs))

        elif subfunction == cmd.GetInfo.Subfunction.GetRuntimePublishedValuesDefinition:
            data = cast(protocol_typing.Request.GetInfo.GetRuntimePublishedValuesDefinition, data)
            if data['start'] > len(self.rpvs):
                return Response(req.command, subfunction, ResponseCode.FailureToProceed)

            if data['start'] + data['count'] > len(self.rpvs):
                return Response(req.command, subfunction, ResponseCode.FailureToProceed)

            all_rpvs = self.get_rpvs()
            all_rpvs.sort(key=lambda x: x.id)
            selected_rpvs = all_rpvs[data['start']:data['start'] + data['count']]
            response = self.protocol.respond_get_rpv_definition(selected_rpvs)

        elif subfunction == cmd.GetInfo.Subfunction.GetLoopCount:
            response = self.protocol.respond_get_loop_count(len(self.loops))

        elif subfunction == cmd.GetInfo.Subfunction.GetLoopDefinition:
            data = cast(protocol_typing.Request.GetInfo.GetLoopDefinition, data)
            if data['loop_id'] < 0 or data['loop_id'] >= len(self.loops):
                response = Response(req.command, req.subfn, ResponseCode.FailureToProceed)
            else:
                response = self.protocol.respond_get_loop_definition(data['loop_id'], self.loops[data['loop_id']])
        else:
            self.logger.error('Unsupported subfunction "%s" for command : "%s"' % (subfunction, req.command.__name__))

        return response

# ===== [MemoryControl] ======

    def process_memory_control(self, req: Request, data: protocol_typing.RequestData) -> Optional[Response]:
        response = None
        subfunction = cmd.MemoryControl.Subfunction(req.subfn)
        if subfunction == cmd.MemoryControl.Subfunction.Read:
            data = cast(protocol_typing.Request.MemoryControl.Read, data)
            response_blocks_read = []
            try:
                for block_to_read in data['blocks_to_read']:
                    memdata = self.read_memory(block_to_read['address'], block_to_read['length'])
                    response_blocks_read.append((block_to_read['address'], memdata))
                response = self.protocol.respond_read_memory_blocks(response_blocks_read)
            except Exception as e:
                self.logger.warning("Failed to read memory: %s" % e)
                self.logger.debug(traceback.format_exc())
                response = Response(req.command, subfunction, ResponseCode.FailureToProceed)

        elif subfunction == cmd.MemoryControl.Subfunction.Write:
            data = cast(protocol_typing.Request.MemoryControl.Write, data)
            response_blocks_write = []
            for block_to_write in data['blocks_to_write']:
                self.write_memory(block_to_write['address'], block_to_write['data'])
                response_blocks_write.append((block_to_write['address'], len(block_to_write['data'])))

            response = self.protocol.respond_write_memory_blocks(response_blocks_write)

        elif subfunction == cmd.MemoryControl.Subfunction.WriteMasked:
            data = cast(protocol_typing.Request.MemoryControl.WriteMasked, data)
            response_blocks_write = []
            for block_to_write in data['blocks_to_write']:
                self.write_memory_masked(block_to_write['address'], block_to_write['data'], block_to_write['write_mask'])
                response_blocks_write.append((block_to_write['address'], len(block_to_write['data'])))

            response = self.protocol.respond_write_memory_blocks_masked(response_blocks_write)

        elif subfunction == cmd.MemoryControl.Subfunction.ReadRPV:
            data = cast(protocol_typing.Request.MemoryControl.ReadRPV, data)
            read_response_data: List[Tuple[int, Any]] = []
            for rpv_id in data['rpvs_id']:
                value = self.read_rpv(rpv_id)
                read_response_data.append((rpv_id, value))

            response = self.protocol.respond_read_runtime_published_values(read_response_data)

        elif subfunction == cmd.MemoryControl.Subfunction.WriteRPV:
            data = cast(protocol_typing.Request.MemoryControl.WriteRPV, data)
            write_response_data: List[int] = []
            for id_data_pair in data['rpvs']:
                rpv_id = id_data_pair['id']
                value = id_data_pair['value']
                self.write_rpv(rpv_id, value)
                write_response_data.append(rpv_id)

            response = self.protocol.respond_write_runtime_published_values(write_response_data)

        else:
            self.logger.error('Unsupported subfunction "%s" for command : "%s"' % (subfunction, req.command.__name__))

        return response

    def process_datalog_control(self, req: Request, data: protocol_typing.RequestData) -> Optional[Response]:
        response = None
        subfunction = cmd.DatalogControl.Subfunction(req.subfn)
        if subfunction == cmd.DatalogControl.Subfunction.GetSetup:
            response = self.protocol.respond_datalogging_get_setup(
                buffer_size=self.datalogger.get_buffer_size(),
                encoding=self.datalogger.get_encoding())
        elif subfunction == cmd.DatalogControl.Subfunction.ConfigureDatalog:
            self.datalogging_read_in_progress = False
            data = cast(protocol_typing.Request.DatalogControl.Configure, data)
            if data['loop_id'] < 0 or data['loop_id'] >= len(self.loops):
                response = Response(req.command, req.subfn, code=ResponseCode.FailureToProceed)
            else:
                self.datalogger.configure(data['config_id'], data['config'])
                if self.datalogger.in_error():
                    response = Response(req.command, req.subfn, code=ResponseCode.InvalidRequest)
                else:
                    response = self.protocol.respond_datalogging_configure()
        elif subfunction == cmd.DatalogControl.Subfunction.ArmTrigger:
            self.datalogging_read_in_progress = False
            self.datalogger.arm_trigger()
            response = self.protocol.respond_datalogging_arm_trigger()
        elif subfunction == cmd.DatalogControl.Subfunction.DisarmTrigger:
            self.datalogger.disarm_trigger()
            response = self.protocol.respond_datalogging_disarm_trigger()
        elif subfunction == cmd.DatalogControl.Subfunction.GetAcquisitionMetadata:
            if self.datalogger.state != datalogging.DataloggerStatus.ACQUISITION_COMPLETED:
                response = Response(req.command, req.subfn, ResponseCode.FailureToProceed)
            else:
                response = self.protocol.respond_datalogging_get_acquisition_metadata(
                    acquisition_id=self.datalogger.get_acquisition_id(),
                    config_id=self.datalogger.get_config_id(),
                    nb_points=self.datalogger.get_nb_points(),
                    datasize=len(self.datalogger.get_acquisition_data()),
                    points_after_trigger=self.datalogger.get_points_after_trigger()
                )
        elif subfunction == cmd.DatalogControl.Subfunction.GetStatus:
            response = self.protocol.respond_datalogging_get_status(status=self.datalogger.state)

        elif subfunction == cmd.DatalogControl.Subfunction.ReadAcquisition:
            if self.datalogger.state != datalogging.DataloggerStatus.ACQUISITION_COMPLETED:
                response = Response(req.command, req.subfn, ResponseCode.FailureToProceed)
            else:
                acquired_data = self.datalogger.get_acquisition_data()

                if not self.datalogging_read_in_progress:
                    self.datalogging_read_in_progress = True
                    self.read_datalogging_read_cursor = 0
                    self.datalogging_read_rolling_counter = 0

                remaining_data = acquired_data[self.read_datalogging_read_cursor:]
                if self.protocol.datalogging_read_acquisition_is_last_response(len(remaining_data), self.max_tx_data_size):
                    crc = crc32(acquired_data)
                    finished = True
                else:
                    crc = None
                    finished = False

                datalen = self.protocol.datalogging_read_acquisition_max_data_size(len(remaining_data), self.max_tx_data_size)
                datalen = min(len(remaining_data), datalen)

                response = self.protocol.respond_datalogging_read_acquisition(
                    finished=finished,
                    rolling_counter=self.datalogging_read_rolling_counter,
                    acquisition_id=self.datalogger.get_acquisition_id(),
                    data=remaining_data[:datalen],
                    crc=crc
                )

                self.datalogging_read_rolling_counter = (self.datalogging_read_rolling_counter + 1) & 0xFF
                self.datalogging_read_cursor += datalen

                if finished:
                    self.datalogging_read_in_progress = False
        else:
            self.logger.error('Unsupported subfunction "%s" for command : "%s"' % (subfunction, req.command.__name__))

        return response

    def process_dummy_cmd(self, req: Request, data: protocol_typing.RequestData):
        return Response(cmd.DummyCommand, subfn=req.subfn, code=ResponseCode.OK, payload=b'\xAA' * 32)

    def start(self) -> None:
        self.logger.debug('Starting thread')
        self.request_shutdown = False
        self.thread_started_event.clear()
        self.thread = threading.Thread(target=self.thread_task)
        self.thread.start()
        self.thread_started_event.wait()
        self.logger.debug('Thread started')

    def stop(self) -> None:
        if self.thread is not None:
            self.logger.debug('Stopping thread')
            self.request_shutdown = True
            self.thread.join()
            self.logger.debug('Thread stopped')
            self.thread = None

    def initiate_session(self) -> None:
        self.session_id = random.randrange(0, 0xFFFFFFFF)
        self.connected = True
        self.logger.info('Initiating session. SessionID = 0x%08x', self.session_id)

    def destroy_session(self) -> None:
        self.logger.info('Destroying session. SessionID = 0x%08x', self.session_id)
        self.session_id = None
        self.connected = False

    def get_firmware_id(self) -> bytes:
        return self.firmware_id

    def is_connected(self) -> bool:
        return self.connected

    def force_connect(self) -> None:
        self.connected = True

    def force_disconnect(self) -> None:
        self.connected = False

    def disable_comm(self) -> None:
        self.comm_enabled = False

    def enable_comm(self) -> None:
        self.comm_enabled = True

    def clear_request_history(self) -> None:
        self.request_history = []

    def get_request_history(self) -> List[RequestLogRecord]:
        return self.request_history

    def send(self, response: Response) -> None:
        if self.comm_enabled:
            self.link.emulate_device_write(response.to_bytes())

    def read(self) -> Optional[Request]:
        data = self.link.emulate_device_read()
        if len(data) > 0 and self.comm_enabled:
            return Request.from_bytes(data)
        return None

    def write_memory(self, address: int, data: Union[bytes, bytearray]) -> None:
        err = None
        self.memory_lock.acquire()
        try:
            self.memory.write(address, data)
        except Exception as e:
            err = e
        finally:
            self.memory_lock.release()

        if err:
            raise err

    def write_memory_masked(self, address: int, data: Union[bytes, bytearray], mask=Union[bytes, bytearray]) -> None:
        err = None
        assert len(mask) == len(data), "Data and mask must be the same length"

        self.memory_lock.acquire()
        try:
            memdata = bytearray(self.memory.read(address, len(data)))
            for i in range(len(data)):
                memdata[i] &= (data[i] | (~mask[i]))
                memdata[i] |= (data[i] & (mask[i]))
            self.memory.write(address, memdata)
        except Exception as e:
            err = e
        finally:
            self.memory_lock.release()

        if err:
            raise err

    def read_memory(self, address: int, length: int) -> bytes:
        self.memory_lock.acquire()
        err = None
        try:
            data = self.memory.read(address, length)
        except Exception as e:
            err = e
        finally:
            self.memory_lock.release()

        if err:
            raise err
        return data

    def get_rpv_definition(self, rpv_id) -> RuntimePublishedValue:
        if rpv_id not in self.rpvs:
            raise ValueError('Unknown RPV ID 0x%04X' % rpv_id)
        return self.rpvs[rpv_id]['definition']

    def get_rpv_definition_map(self) -> Dict[int, RuntimePublishedValue]:
        output: Dict[int, RuntimePublishedValue] = {}
        for rpv_id in self.rpvs:
            output[rpv_id] = self.get_rpv_definition(rpv_id)
        return output

    def get_rpvs(self) -> List[RuntimePublishedValue]:
        output: List[RuntimePublishedValue] = []
        err = None
        self.rpv_lock.acquire()
        try:
            for id in self.rpvs:
                output.append(self.rpvs[id]['definition'])
        except Exception as e:
            err = e
        finally:
            self.rpv_lock.release()

        if err:
            raise err

        return output

    def write_rpv(self, rpv_id: int, value: Encodable) -> None:
        if rpv_id not in self.rpvs:
            raise ValueError('Unknown RuntimePublishedValue with ID 0x%04X' % rpv_id)

        err = None
        self.rpv_lock.acquire()
        try:
            self.rpvs[rpv_id]['value'] = value
        except Exception as e:
            err = e
        finally:
            self.rpv_lock.release()

        if err:
            raise err

    def read_rpv(self, rpv_id) -> Encodable:
        val: Encodable
        err = None
        self.rpv_lock.acquire()
        try:
            val = self.rpvs[rpv_id]['value']
        except Exception as e:
            err = e
        finally:
            self.rpv_lock.release()

        if err:
            raise err

        if rpv_id not in self.rpvs:
            raise ValueError('Unknown RuntimePublishedValue with ID 0x%04X' % rpv_id)
        return val
