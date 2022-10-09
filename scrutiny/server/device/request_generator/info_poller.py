#    info_poller.py
#        Once enabled, successively poll all pollable data from a device through the Scrutiny
#        protocol. Also call callbacks method when specific data is read e.g. Protocol version,
#        buffer size, etc
#
#   - License : MIT - See LICENSE file.
#   - Project :  Scrutiny Debugger (github.com/scrutinydebugger/scrutiny-python)
#
#   Copyright (c) 2021-2022 Scrutiny Debugger

import logging
import enum
import copy
import traceback

from scrutiny.server.protocol import ResponseCode
from scrutiny.server.device.device_info import *
import scrutiny.server.protocol.commands as cmd
from scrutiny.server.device.request_dispatcher import RequestDispatcher, SuccessCallback, FailureCallback
from scrutiny.server.protocol import *
import scrutiny.server.protocol.typing as protocol_typing

from scrutiny.core.typehints import GenericCallback

from typing import Optional, Callable, Any, cast


class ProtocolVersionCallback(GenericCallback):
    callback: Callable[[int, int], Any]


class CommParamCallback(GenericCallback):
    callback: Callable[[DeviceInfo], Any]


class InfoPoller:

    logger: logging.Logger
    dispatcher: RequestDispatcher       # We put the request in here, and we know they'll go out
    protocol: Protocol                  # The actual protocol. Used to build the request payloads
    priority: int                       # Our dispatcher priority
    info: DeviceInfo        # Stores the data that we gather from the device
    started: bool           # Indicate if enabled or not
    protocol_version_callback: Optional[ProtocolVersionCallback]    # When the protocol version from the device is read, call this
    comm_param_callback: Optional[CommParamCallback]    # When we have fetched the communication parameters, call this callback
    fsm_state: "InfoPoller.FsmState"        # The state machine state
    last_fsm_state: "InfoPoller.FsmState"   # Previous cycle state of the state machine
    stop_requested: bool    # Requested to stop polling
    request_pending: bool   # True when we are waiting for a request to complete
    request_failed: bool    # Flag indicating that a request have failed. Will make the
    forbidden_memory_region_count: Optional[int]    # Number of forbidden memory region to read
    readonly_memory_region_count: Optional[int]     # Number of readonly memory region to read
    rpv_count: Optional[int]    # Number of Runtime Published Values to reads
    error_message: str          # Detailed error of why it was impossible to poll al the data

    class FsmState(enum.Enum):
        # Finite State Machine state
        Error = -1
        Init = 0
        GetProtocolVersion = 1
        GetCommParams = 2
        GetSupportedFeatures = 3
        GetSpecialMemoryRegionCount = 4
        GetForbiddenMemoryRegions = 5
        GetReadOnlyMemoryRegions = 6
        GetRPVCount = 7
        GetRPVDefinition = 8
        Done = 9

    def __init__(self, protocol: Protocol, dispatcher: RequestDispatcher, priority: int,
                 protocol_version_callback: Optional[ProtocolVersionCallback] = None,
                 comm_param_callback: Optional[CommParamCallback] = None
                 ):

        self.logger = logging.getLogger(self.__class__.__name__)
        self.dispatcher = dispatcher
        self.protocol = protocol
        self.priority = priority
        self.info = DeviceInfo()
        self.started = False
        self.protocol_version_callback = protocol_version_callback
        self.comm_param_callback = comm_param_callback
        self.fsm_state = self.FsmState.Init

        self.reset()

    def set_known_info(self, device_id: str, device_display_name: str) -> None:
        # Some info about the device is known beforehand. Let's input it here, it will
        # be written to the final DeviceInfo structure
        self.info.device_id = device_id
        self.info.display_name = device_display_name

    def get_device_info(self) -> DeviceInfo:
        # Retrieve the data gathered from the device
        return copy.copy(self.info)

    def start(self) -> None:
        # Launch polling of data
        self.started = True

    def stop(self) -> None:
        # Stop the poller
        self.stop_requested = True

    def done(self) -> bool:
        """Returns True when data is finished to be gathered from the device"""
        return self.fsm_state == self.FsmState.Done

    def is_in_error(self) -> bool:
        """Returns true if an error occured and the state machine went to error state"""
        return self.fsm_state == self.FsmState.Error

    def reset(self) -> None:
        if self.fsm_state != self.FsmState.Init:
            self.logger.debug('Moving state machine to %s' % self.FsmState.Init)
        self.fsm_state = self.FsmState.Init
        self.last_fsm_state = self.FsmState.Init
        self.stop_requested = False
        self.request_pending = False
        self.request_failed = False
        self.forbidden_memory_region_count = None
        self.readonly_memory_region_count = None
        self.rpv_count = 0
        self.error_message = ""
        self.info.clear()

    def process(self) -> None:
        """To be called  periodically to make the process move forward"""
        if not self.started:
            self.reset()
            return
        elif self.stop_requested and not self.request_pending:
            self.started = False
            self.reset()
            return

        next_state: "InfoPoller.FsmState" = self.fsm_state
        state_entry: bool = (self.fsm_state != self.last_fsm_state)

        if self.fsm_state == self.FsmState.Init:
            if self.started:
                next_state = self.FsmState.GetProtocolVersion

        # ======= [GetProtocolVersion] =====
        elif self.fsm_state == self.FsmState.GetProtocolVersion:
            # We already know the protocol version from the discover request.  This should maybe be removed...
            if state_entry:
                self.dispatcher.register_request(request=self.protocol.get_protocol_version(),
                                                 success_callback=SuccessCallback(self.success_callback), failure_callback=FailureCallback(self.failure_callback), priority=self.priority)
                self.request_pending = True

            if self.request_failed:
                next_state = self.FsmState.Error
            if not self.request_pending:    # Request completed
                try:
                    if self.protocol_version_callback is not None:
                        self.protocol_version_callback.__call__(self.info.protocol_major, self.info.protocol_minor)
                    next_state = self.FsmState.GetCommParams
                except Exception as e:
                    self.logger.error('Error while processing protocol version. %s' % str(e))
                    self.logger.debug(traceback.format_exc())
                    next_state = self.FsmState.Error

        # ======= [GetCommParams] =====
        elif self.fsm_state == self.FsmState.GetCommParams:
            if state_entry:
                self.dispatcher.register_request(request=self.protocol.comm_get_params(),
                                                 success_callback=SuccessCallback(self.success_callback), failure_callback=FailureCallback(self.failure_callback), priority=self.priority)
                self.request_pending = True

            if self.request_failed:
                next_state = self.FsmState.Error

            if not self.request_pending:
                try:
                    if self.comm_param_callback is not None:
                        # Some comm params will change the device handling. So let the deviceHandler know right away
                        self.comm_param_callback.__call__(copy.copy(self.info))
                    next_state = self.FsmState.GetSupportedFeatures
                except Exception as e:
                    self.logger.error('Error while processing communication params. %s' % str(e))
                    self.logger.debug(traceback.format_exc())
                    next_state = self.FsmState.Error

        # ======= [GetSupportedFeatures] =====
        elif self.fsm_state == self.FsmState.GetSupportedFeatures:
            if state_entry:
                self.dispatcher.register_request(request=self.protocol.get_supported_features(),
                                                 success_callback=SuccessCallback(self.success_callback), failure_callback=FailureCallback(self.failure_callback), priority=self.priority)
                self.request_pending = True

            if self.request_failed:
                next_state = self.FsmState.Error
            if not self.request_pending:
                next_state = self.FsmState.GetSpecialMemoryRegionCount

        # ======= [GetSpecialMemoryRegionCount] =====
        elif self.fsm_state == self.FsmState.GetSpecialMemoryRegionCount:
            if state_entry:
                self.forbidden_memory_region_count = None
                self.readonly_memory_region_count = None
                self.dispatcher.register_request(request=self.protocol.get_special_memory_region_count(),
                                                 success_callback=SuccessCallback(self.success_callback), failure_callback=FailureCallback(self.failure_callback), priority=self.priority)
                self.request_pending = True

            if self.request_failed:
                next_state = self.FsmState.Error
            if not self.request_pending:
                next_state = self.FsmState.GetForbiddenMemoryRegions

        # ======= [GetForbiddenMemoryRegions] =====
        elif self.fsm_state == self.FsmState.GetForbiddenMemoryRegions:
            if self.forbidden_memory_region_count is None:
                next_state = self.FsmState.Error
            else:
                if state_entry:
                    self.info.forbidden_memory_regions = []
                    for i in range(self.forbidden_memory_region_count):
                        self.dispatcher.register_request(request=self.protocol.get_special_memory_region_location(cmd.GetInfo.MemoryRangeType.Forbidden, i),
                                                         success_callback=SuccessCallback(self.success_callback), failure_callback=FailureCallback(self.failure_callback), priority=self.priority)

                if self.request_failed:
                    next_state = self.FsmState.Error

                assert self.info.forbidden_memory_regions is not None   # for mypy
                if len(self.info.forbidden_memory_regions) >= self.forbidden_memory_region_count:
                    next_state = self.FsmState.GetReadOnlyMemoryRegions

        # ======= [GetReadOnlyMemoryRegions] =====
        elif self.fsm_state == self.FsmState.GetReadOnlyMemoryRegions:
            if self.readonly_memory_region_count is None:
                next_state = self.FsmState.Error
            else:
                if state_entry:
                    self.info.readonly_memory_regions = []
                    for i in range(self.readonly_memory_region_count):
                        self.dispatcher.register_request(request=self.protocol.get_special_memory_region_location(cmd.GetInfo.MemoryRangeType.ReadOnly, i),
                                                         success_callback=SuccessCallback(self.success_callback), failure_callback=FailureCallback(self.failure_callback), priority=self.priority)

                if self.request_failed:
                    next_state = self.FsmState.Error

                assert self.info.readonly_memory_regions is not None
                if len(self.info.readonly_memory_regions) >= self.readonly_memory_region_count:
                    next_state = self.FsmState.GetRPVCount

        # ======= [GetRPVCount] =====
        elif self.fsm_state == self.FsmState.GetRPVCount:
            if state_entry:
                self.rpv_count = None   # Will be set in success callback
                self.dispatcher.register_request(request=self.protocol.get_rpv_count(),
                                                 success_callback=SuccessCallback(self.success_callback), failure_callback=FailureCallback(self.failure_callback), priority=self.priority)
                self.request_pending = True

            if self.request_failed:
                next_state = self.FsmState.Error
            if not self.request_pending:
                next_state = self.FsmState.GetRPVDefinition

        # ======= [GetRPVDefinition] =====
        elif self.fsm_state == self.FsmState.GetRPVDefinition:
            if state_entry:
                if self.rpv_count is None:
                    next_state = self.FsmState.Error
                elif self.info.max_rx_data_size is None or self.info.max_tx_data_size is None:
                    next_state = self.FsmState.Error
                else:
                    max_rpv_per_request = self.info.max_tx_data_size // self.protocol.get_rpv_definition_response_size_per_rpv()
                    self.info.runtime_published_values = []

            if self.request_failed:
                next_state = self.FsmState.Error

            elif not self.request_pending and next_state != self.FsmState.Error:
                assert self.info.runtime_published_values is not None
                assert self.rpv_count is not None

                # Issue a new request until all RPV are read
                already_read_count = len(self.info.runtime_published_values)
                if already_read_count < self.rpv_count:
                    count = min(max_rpv_per_request, self.rpv_count - already_read_count)
                    request = self.protocol.get_rpv_definition(start=len(self.info.runtime_published_values), count=count)
                    self.dispatcher.register_request(
                        request=request,
                        success_callback=SuccessCallback(self.success_callback),
                        failure_callback=FailureCallback(self.failure_callback),
                        priority=self.priority
                    )
                    self.request_pending = True
                else:
                    next_state = self.FsmState.Done

        elif self.fsm_state == self.FsmState.Done:
            pass

        elif self.fsm_state == self.FsmState.Error:
            pass

        else:
            self.logger.error('State Machine went into an unkwon state : %s' % self.fsm_state)
            next_state = self.FsmState.Error

        if next_state != self.fsm_state:
            self.logger.debug('Moving state machine to %s' % next_state)

        self.last_fsm_state = self.fsm_state
        self.fsm_state = next_state

    def success_callback(self, request: Request, response: Response, params: Any = None) -> None:
        """Called when a request completes and succeeds"""

        self.logger.debug("Success callback. Request=%s. Response Code=%s, Params=%s" % (request, response.code, params))
        response_data: protocol_typing.ResponseData

        must_process_response = True
        if self.stop_requested:
            must_process_response = False

        if response.code != ResponseCode.OK:
            self.request_failed = True
            error_message_map = {
                self.FsmState.GetProtocolVersion: 'Device refused to give protocol version. Response Code = %s' % response.code,
                self.FsmState.GetCommParams: 'Device refused to give communication params. Response Code = %s' % response.code,
                self.FsmState.GetSupportedFeatures: 'Device refused to give supported features. Response Code = %s' % response.code,
                self.FsmState.GetSpecialMemoryRegionCount: 'Device refused to give special region count. Response Code = %s' % response.code,
                self.FsmState.GetForbiddenMemoryRegions: 'Device refused to give forbidden region list. Response Code = %s' % response.code,
                self.FsmState.GetReadOnlyMemoryRegions: 'Device refused to give readonly region list. Response Code = %s' % response.code,
                self.FsmState.GetRPVCount: 'Device refused to give RuntimePublishedValues count. Response Code = %s' % response.code,
                self.FsmState.GetRPVDefinition: 'Device refused to give RuntimePublishedValues definition. Response Code = %s' % response.code
            }
            self.error_message = error_message_map[self.fsm_state] if self.fsm_state in error_message_map else 'Internal error - Request denied. %s - %s' % (
                str(Request), response.code)
            must_process_response = False

        if must_process_response:
            try:
                response_data = self.protocol.parse_response(response)  # It's ok to have the exception go up
            except Exception as e:
                self.request_failed = True
                error_message_map = {
                    self.FsmState.GetProtocolVersion: 'Device gave invalid data when polling for protocol version. Response Code = %s' % response.code,
                    self.FsmState.GetCommParams: 'Device gave invalid data when polling for communication params. Response Code = %s' % response.code,
                    self.FsmState.GetSupportedFeatures: 'Device gave invalid data when polling for supported features. Response Code = %s' % response.code,
                    self.FsmState.GetSpecialMemoryRegionCount: 'Device gave invalid data when polling for special region count. Response Code = %s' % response.code,
                    self.FsmState.GetForbiddenMemoryRegions: 'Device gave invalid data when polling for forbidden region list. Response Code = %s' % response.code,
                    self.FsmState.GetReadOnlyMemoryRegions: 'Device gave invalid data when polling for readonly region list. Response Code = %s' % response.code,
                    self.FsmState.GetRPVCount: 'Device gave invalid data when polling for RuntimePublishedValues count. Response Code = %s' % response.code,
                    self.FsmState.GetRPVCount: 'Device gave invalid data when polling for RuntimePublishedValues definition. Response Code = %s' % response.code
                }
                self.error_message = error_message_map[self.fsm_state] if self.fsm_state in error_message_map else 'Internal error - Invalid response for request %s' % str(
                    Request)
                must_process_response = False

        if must_process_response:
            if self.fsm_state == self.FsmState.GetProtocolVersion:
                response_data = cast(protocol_typing.Response.GetInfo.GetProtocolVersion, response_data)
                self.info.protocol_major = response_data['major']
                self.info.protocol_minor = response_data['minor']

            elif self.fsm_state == self.FsmState.GetCommParams:
                response_data = cast(protocol_typing.Response.CommControl.GetParams, response_data)
                self.info.max_tx_data_size = response_data['max_tx_data_size']
                self.info.max_rx_data_size = response_data['max_rx_data_size']
                self.info.max_bitrate_bps = response_data['max_bitrate_bps']
                self.info.rx_timeout_us = response_data['rx_timeout_us']
                self.info.heartbeat_timeout_us = response_data['heartbeat_timeout_us']
                self.info.address_size_bits = response_data['address_size_byte'] * 8

            elif self.fsm_state == self.FsmState.GetSupportedFeatures:
                response_data = cast(protocol_typing.Response.GetInfo.GetSupportedFeatures, response_data)
                self.info.supported_feature_map = {
                    'memory_write': response_data['memory_write'],
                    'datalog_acquire': response_data['datalog_acquire'],
                    'user_command': response_data['user_command']
                }

            elif self.fsm_state == self.FsmState.GetSpecialMemoryRegionCount:
                response_data = cast(protocol_typing.Response.GetInfo.GetSpecialMemoryRegionCount, response_data)
                self.readonly_memory_region_count = response_data['nbr_readonly']
                self.forbidden_memory_region_count = response_data['nbr_forbidden']

            elif self.fsm_state == self.FsmState.GetForbiddenMemoryRegions:
                response_data = cast(protocol_typing.Response.GetInfo.GetSpecialMemoryRegionLocation, response_data)
                if self.info.forbidden_memory_regions is None:
                    self.info.forbidden_memory_regions = []
                forbidden_entry: MemoryRegion = {
                    'start': response_data['start'],
                    'end': response_data['end']
                }
                self.info.forbidden_memory_regions.append(forbidden_entry)

            elif self.fsm_state == self.FsmState.GetReadOnlyMemoryRegions:
                response_data = cast(protocol_typing.Response.GetInfo.GetSpecialMemoryRegionLocation, response_data)
                if self.info.readonly_memory_regions is None:
                    self.info.readonly_memory_regions = []

                readonly_entry: MemoryRegion = {
                    'start': response_data['start'],
                    'end': response_data['end']
                }
                self.info.readonly_memory_regions.append(readonly_entry)

            elif self.fsm_state == self.FsmState.GetRPVCount:
                response_data = cast(protocol_typing.Response.GetInfo.GetRuntimePublishedValuesCount, response_data)
                self.rpv_count = response_data['count']

            elif self.fsm_state == self.FsmState.GetRPVDefinition:
                response_data = cast(protocol_typing.Response.GetInfo.GetRuntimePublishedValuesDefinition, response_data)
                assert self.info.runtime_published_values is not None
                self.info.runtime_published_values += response_data['rpvs']

            else:
                self.fsm_state == self.FsmState.Error
                self.error_message = "Internal error - Got response for unhandled parameter"

        self.completed()

    def failure_callback(self, request: Request, params: Any = None) -> None:
        self.logger.debug("Failure callback. Request=%s. Params=%s" % (request, params))
        if not self.stop_requested:
            self.request_failed = True

            error_message_map = {
                self.FsmState.GetProtocolVersion: 'Failed to get protocol version',
                self.FsmState.GetCommParams: 'Failed to get communication params',
                self.FsmState.GetSupportedFeatures: 'Failed to get supported features',
                self.FsmState.GetSpecialMemoryRegionCount: 'Failed to get special region count',
                self.FsmState.GetForbiddenMemoryRegions: 'Failed to get forbidden region list',
                self.FsmState.GetReadOnlyMemoryRegions: 'Failed to get readonly region list'
            }

            self.error_message = error_message_map[self.fsm_state] if self.fsm_state in error_message_map else 'Internal error - Request failure'

        self.completed()

    def completed(self) -> None:
        self.request_pending = False
        if self.stop_requested:
            self.reset()
