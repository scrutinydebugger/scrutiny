#    datalogging_manager.py
#        The main server components that manages the datalogging feature at high level
#
#   - License : MIT - See LICENSE file.
#   - Project :  Scrutiny Debugger (github.com/scrutinydebugger/scrutiny-python)
#
#   Copyright (c) 2021-2023 Scrutiny Debugger

import queue
import logging
import math
from dataclasses import dataclass
from uuid import uuid4
from datetime import datetime
import traceback

import scrutiny.server.datalogging.definitions.api as api_datalogging
import scrutiny.server.datalogging.definitions.device as device_datalogging
from scrutiny.server.device.device_handler import DeviceHandler, DataloggingReceiveSetupCallback, DeviceAcquisitionRequestCompletionCallback
from scrutiny.server.datastore.datastore_entry import DatastoreEntry, DatastoreAliasEntry, DatastoreRPVEntry, DatastoreVariableEntry
from scrutiny.server.datastore.datastore import Datastore
from scrutiny.server.device.device_info import FixedFreqLoop
from scrutiny.core.basic_types import *
from scrutiny.server.datalogging.datalogging_storage import DataloggingStorage
from scrutiny.core.codecs import Codecs

from typing import Optional, List, Dict, Tuple, Callable
from scrutiny.core.typehints import GenericCallback


@dataclass
class DeviceSideAcquisitionRequest:
    api_request: api_datalogging.AcquisitionRequest
    device_config: device_datalogging.Configuration
    entry_signal_map: Dict[DatastoreEntry, int]
    callback: api_datalogging.APIAcquisitionRequestCompletionCallback


class DataloggingManager:
    datastore: Datastore
    device_handler: DeviceHandler
    acquisition_request_queue: "queue.Queue[DeviceSideAcquisitionRequest]"
    last_device_status: DeviceHandler.ConnectionStatus
    device_status: DeviceHandler.ConnectionStatus
    active_request: Optional[DeviceSideAcquisitionRequest]
    logger: logging.Logger
    datalogging_setup: Optional[device_datalogging.DataloggingSetup]
    rpv_map: Optional[Dict[int, RuntimePublishedValue]]

    def __init__(self, datastore: Datastore, device_handler: DeviceHandler):
        self.datastore = datastore
        self.device_handler = device_handler
        self.logger = logging.getLogger(self.__class__.__name__)
        self.acquisition_request_queue = queue.Queue()
        self.last_device_status = DeviceHandler.ConnectionStatus.UNKNOWN
        self.device_status = DeviceHandler.ConnectionStatus.UNKNOWN
        self.active_request = None
        self.datalogging_setup = None
        self.rpv_map = None

        self.device_handler.set_datalogging_callbacks(
            receive_setup=DataloggingReceiveSetupCallback(self.callback_receive_setup),
        )

    def is_valid_sample_rate_id(self, identifier: int) -> bool:
        for rate in self.get_available_sampling_rates():
            if rate.device_identifier == identifier:
                return True

        return False

    def is_ready_for_request(self) -> bool:
        return True  # todo

    def set_disconnected(self):
        self.active_request = None
        self.datalogging_setup = None
        self.rpv_map = None

    def request_acquisition(self, request: api_datalogging.AcquisitionRequest, callback: api_datalogging.APIAcquisitionRequestCompletionCallback) -> None:
        # Converts right away to device side acquisition because we want exception to be raised as early as possible for quik feedback to user
        config, entry_signal_map = self.make_device_config_from_request(request)  # Can raise an exception
        self.acquisition_request_queue.put(DeviceSideAcquisitionRequest(
            api_request=request,
            device_config=config,
            entry_signal_map=entry_signal_map,
            callback=callback))

    def acquisition_complete_callback(self, success: bool, data: Optional[List[List[bytes]]]) -> None:
        if self.active_request is None:
            self.logger.error("Received acquisition data but was not expecting it. No active acquisition request")
            return

        device_info = self.device_handler.get_device_info()
        if device_info is None or device_info.device_id is None:
            self.logger.error('Gotten an acquisition but the device information is not available')
            self.active_request.callback(False, None)
            return

        acquisition: Optional[api_datalogging.DataloggingAcquisition] = None
        try:
            if success:
                self.logger.info("New acquisition gotten")
                assert data is not None

                nb_points: Optional[int] = None
                for signal_data in data:
                    if nb_points is None:
                        nb_points = len(signal_data)
                    else:
                        if nb_points != len(signal_data):
                            raise ValueError('non-matching data length recived in new acquisition')

                if nb_points is None:
                    raise ValueError('Cannot determine the number of points in the acquisitions')

                acquisition = api_datalogging.DataloggingAcquisition(
                    name=self.active_request.api_request.name,
                    reference_id=uuid4().hex,
                    firmware_id=device_info.device_id,
                    acq_time=datetime.now()
                )

                for signal in self.active_request.api_request.signals:
                    parsed_data = self.read_active_request_data_from_raw_data(signal, data)
                    ds = api_datalogging.DataSeries(
                        data=parsed_data,
                        logged_element=signal.entry.display_path
                    )
                    if signal.name:
                        ds.name = signal.name
                    acquisition.add_data(ds)

                xaxis = api_datalogging.DataSeries()
                if self.active_request.api_request.x_axis_type == api_datalogging.XAxisType.IdealTime:
                    sampling_rate = self.get_sampling_rate(self.active_request.api_request.rate_identifier)
                    if sampling_rate.frequency is None:
                        raise ValueError('Ideal time X-Axis is not possible with variable frequency loops')
                    timestep = 1 / sampling_rate.frequency
                    timestep *= self.active_request.api_request.decimation
                    xaxis.set_data([i * timestep for i in range(nb_points)])
                elif self.active_request.api_request.x_axis_type == api_datalogging.XAxisType.MeasuredTime:
                    time_data = data[-1]
                    time_codec = Codecs.get(EmbeddedDataType.uint32, endianness=Endianness.Big)
                    xaxis.set_data([time_codec.decode(sample) * 1e-7 for sample in time_data])
                elif self.active_request.api_request.x_axis_type == api_datalogging.XAxisType.Signal:
                    xaxis_signal = self.active_request.api_request.x_axis_signal
                    assert xaxis_signal is not None
                    parsed_data = self.read_active_request_data_from_raw_data(xaxis_signal, data)
                    xaxis.set_data(parsed_data)
                else:
                    raise ValueError('Impossible X-Axis type')

                if len(xaxis) != nb_points:
                    raise ValueError("Failed to find a matching xaxis dataseries")

                xaxis.name = 'time'
                xaxis.logged_element = 'time'
                acquisition.set_xaxis(xaxis)

                DataloggingStorage.save(acquisition)

            else:
                self.logger.info("Failed to acquire acquisition request")
        except Exception as e:
            acquisition = None
            self.logger.error('Error while processing datalogging acquisition: %s' % str(e))
            self.logger.debug(traceback.format_exc())

        err: Optional[Exception] = None
        try:
            if acquisition is None:
                self.active_request.callback(False, None)
            else:
                self.active_request.callback(True, acquisition)
        except Exception as e:
            err = e

        self.active_request = None
        if err:
            raise err

    def read_active_request_data_from_raw_data(self, signal: api_datalogging.SignalDefinition, data: List[List[bytes]]) -> List[float]:
        assert self.active_request is not None
        loggable_id = self.active_request.entry_signal_map[signal.entry]
        signal_data = data[loggable_id]
        parsed_signal_data = []
        for data_chunk in signal_data:
            parsed_signal_data.append(float(signal.entry.decode(data_chunk)))

        return parsed_signal_data

    def process(self) -> None:
        self.device_status = self.device_handler.get_connection_status()

        if self.device_status == DeviceHandler.ConnectionStatus.CONNECTED_READY:
            device_info = self.device_handler.get_device_info()
            assert device_info is not None
            assert device_info.supported_feature_map is not None
            assert device_info.runtime_published_values is not None
            if device_info.supported_feature_map['datalogging'] == True:
                if self.last_device_status != DeviceHandler.ConnectionStatus.CONNECTED_READY:   # Just connected
                    self.rpv_map = {}
                    for rpv in device_info.runtime_published_values:
                        self.rpv_map[rpv.id] = rpv
                else:
                    self.process_connected_ready()
        else:
            self.set_disconnected()

        self.last_device_status = self.device_status

    def process_connected_ready(self):
        if self.active_request is None:
            if not self.acquisition_request_queue.empty():
                self.active_request = self.acquisition_request_queue.get()
                self.device_handler.request_datalogging_acquisition(
                    loop_id=self.active_request.api_request.rate_identifier,
                    config=self.active_request.device_config,
                    callback=DeviceAcquisitionRequestCompletionCallback(self.acquisition_complete_callback)
                )

    def callback_receive_setup(self, setup: device_datalogging.DataloggingSetup):
        self.datalogging_setup = setup

    @classmethod
    def api_trigger_condition_to_device_trigger_condition(cls, api_cond: api_datalogging.TriggerCondition) -> device_datalogging.TriggerCondition:
        device_operands: List[device_datalogging.Operand] = []
        for api_operand in api_cond.operands:
            if api_operand.type == api_datalogging.TriggerConditionOperandType.LITERAL:
                if not (isinstance(api_operand.value, int) or isinstance(api_operand.value, float)):
                    raise ValueError("Literal operands must be int or float")
                device_operands.append(device_datalogging.LiteralOperand(api_operand.value))
            elif api_operand.type == api_datalogging.TriggerConditionOperandType.WATCHABLE:
                if not isinstance(api_operand.value, DatastoreEntry):
                    raise ValueError("Watchable operand must have a datastore entry as value")

                device_operands.append(cls.make_device_operand_from_watchable(api_operand.value))
            else:
                raise ValueError("Unsupported operand type %s" % str(api_operand.type))

        device_cond = device_datalogging.TriggerCondition(
            api_cond.condition_id,
            *device_operands
        )

        return device_cond

    @classmethod
    def make_device_config_from_request(self, request: api_datalogging.AcquisitionRequest) -> Tuple[device_datalogging.Configuration, Dict[DatastoreEntry, int]]:
        config = device_datalogging.Configuration()
        # Each of the assignation below can trigger an exception if out of bound
        config.decimation = request.decimation
        config.timeout = request.timeout
        config.probe_location = request.probe_location
        config.trigger_hold_time = request.trigger_hold_time
        config.trigger_condition = self.api_trigger_condition_to_device_trigger_condition(request.trigger_condition)

        entry2signal_map: Dict[DatastoreEntry, int] = {}

        all_signals = request.signals.copy()

        if request.x_axis_type == api_datalogging.XAxisType.Signal:
            if not isinstance(request.x_axis_signal, api_datalogging.SignalDefinition):
                raise ValueError("X Axis must have a signal definition")
            all_signals.append(request.x_axis_signal)

        for signal in all_signals:
            entry_to_log: DatastoreEntry
            if isinstance(signal.entry, DatastoreAliasEntry):
                entry_to_log = signal.entry.refentry
            else:
                entry_to_log = signal.entry

            if entry_to_log not in entry2signal_map:
                config.add_signal(self.make_signal_from_watchable(entry_to_log))
                signal_index = len(config.get_signals()) - 1
            else:
                signal_index = entry2signal_map[entry_to_log]
            entry2signal_map[entry_to_log] = signal_index
            entry2signal_map[signal.entry] = signal_index

        # Purposely add time at the end
        if request.x_axis_type == api_datalogging.XAxisType.MeasuredTime:
            config.add_signal(device_datalogging.TimeLoggableSignal())

        return (config, entry2signal_map)

    @classmethod
    def make_signal_from_watchable(cls, watchable: DatastoreEntry) -> device_datalogging.LoggableSignal:
        """Makes the definitions of a loggable signal from a datastore watchable entry"""
        if isinstance(watchable, DatastoreAliasEntry):
            watchable = watchable.refentry

        signal: device_datalogging.LoggableSignal
        if isinstance(watchable, DatastoreVariableEntry):
            if watchable.is_bitfield():
                bitoffset = watchable.get_bitoffset()
                bitsize = watchable.get_bitsize()
                assert bitoffset is not None
                assert bitsize is not None

                size = math.ceil(bitsize / 8)
                if watchable.variable_def.endianness == Endianness.Little:
                    address = watchable.get_address() + bitoffset // 8
                else:
                    address = (watchable.get_address() + watchable.get_data_type().get_size_byte()) - bitoffset // 8

                signal = device_datalogging.MemoryLoggableSignal(address, size)
            else:
                signal = device_datalogging.MemoryLoggableSignal(watchable.get_address(), watchable.get_size())
        elif isinstance(watchable, DatastoreRPVEntry):
            signal = device_datalogging.RPVLoggableSignal(watchable.get_rpv().id)
        else:
            raise ValueError('Cannot make a loggable signal out of this watchable %s' % (watchable.display_path))
        return signal

    @classmethod
    def make_device_operand_from_watchable(cls, watchable: DatastoreEntry) -> device_datalogging.Operand:
        """Makes a datalogging trigger condition operand from a datastore watchable entry"""

        if isinstance(watchable, DatastoreAliasEntry):
            watchable = watchable.refentry

        operand: device_datalogging.Operand
        if isinstance(watchable, DatastoreVariableEntry):
            if watchable.is_bitfield():
                bitoffset = watchable.get_bitoffset()
                bitsize = watchable.get_bitsize()
                assert bitoffset is not None
                assert bitsize is not None

                operand = device_datalogging.VarBitOperand(
                    watchable.get_address(),
                    watchable.get_data_type(),
                    bitoffset,
                    bitsize)
            else:
                operand = device_datalogging.VarOperand(watchable.get_address(), watchable.get_data_type())
        elif isinstance(watchable, DatastoreRPVEntry):
            operand = device_datalogging.RPVOperand(watchable.get_rpv().id)
        else:
            raise ValueError('Cannot make a Operand out of this watchable %s' % (watchable.display_path))

        return operand

    def get_device_setup(self) -> Optional[device_datalogging.DataloggingSetup]:
        return self.device_handler.get_datalogging_setup()

    def get_sampling_rate(self, identifier: int) -> api_datalogging.SamplingRate:
        sampling_rates = self.get_available_sampling_rates()
        candidate: Optional[api_datalogging.SamplingRate] = None
        for sr in sampling_rates:
            if sr.device_identifier == identifier:
                candidate = sr
                break
        if candidate is None:
            raise ValueError("Cannot find requested sampling rate")
        return candidate

    def get_available_sampling_rates(self) -> List[api_datalogging.SamplingRate]:
        output: List[api_datalogging.SamplingRate] = []

        if self.device_status == DeviceHandler.ConnectionStatus.CONNECTED_READY:
            device_info = self.device_handler.get_device_info()
            if device_info is not None:
                if device_info.loops is not None:
                    for i in range(len(device_info.loops)):
                        loop = device_info.loops[i]
                        if loop.support_datalogging:
                            rate = api_datalogging.SamplingRate(
                                name=loop.get_name(),
                                rate_type=loop.get_loop_type(),
                                device_identifier=i,
                                frequency=None
                            )
                            if isinstance(loop, FixedFreqLoop):
                                rate.frequency = loop.get_frequency()
                            output.append(rate)
        return output
