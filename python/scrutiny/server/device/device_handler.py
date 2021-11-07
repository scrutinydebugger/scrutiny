import copy
import queue
import time
import logging
import binascii
from enum import Enum

from scrutiny.server.protocol.comm_handler import CommHandler
from scrutiny.server.protocol import Protocol, ResponseCode
from scrutiny.server.device.device_searcher import DeviceSearcher
from scrutiny.server.device.request_dispatcher import RequestDispatcher
from scrutiny.server.device.heartbeat_generator import HeartbeatGenerator
from scrutiny.core.firmware_id import PLACEHOLDER as DEFAULT_FIRMWARE_ID

DEFAULT_FIRMWARE_ID_ASCII = binascii.hexlify(DEFAULT_FIRMWARE_ID).decode('ascii')

class DeviceHandler:
    DEFAULT_PARAMS = {
            'response_timeout' : 1.0,    # If a response take more than this delay to be received after a request is sent, drop the response.
            'heartbeat_timeout' : 4.0
        }

    class FsmState(Enum):
        INIT = 0
        DISCOVERING = 1
        CONNECTING = 2
        POLLING_INFO = 3

    def __init__(self, config, datastore):
        self.logger = logging.getLogger(self.__class__.__name__)

        self.config = copy.copy(self.DEFAULT_PARAMS)
        self.config.update(config)
        self.datastore = datastore
        self.dispatcher = RequestDispatcher()
        self.protocol = Protocol(1,0)
        self.device_searcher = DeviceSearcher(self.protocol, self.dispatcher)
        self.heartbeat_generator = HeartbeatGenerator(self.protocol, self.dispatcher)

        self.comm_handler = CommHandler(self.config)

        self.heartbeat_generator.set_interval(max(0.5, self.config['heartbeat_timeout'] * 0.75))
        self.comm_broken = False
        self.device_id = None

        self.reset_comm()

    def reset_comm(self):
        if self.comm_broken and self.device_id is not None:
            self.logger.info('Communication with device stopped. Restarting')

        self.connected = False
        self.fsm_state = self.FsmState.INIT
        self.last_fsm_state = self.FsmState.INIT
        self.active_request_record = None
        self.device_id = None
        self.comm_broken = False
        self.device_searcher.stop()
        self.heartbeat_generator.stop()
        self.session_id = None

    def init_comm(self):
        if self.config['link_type'] == 'none':
            return

        if self.config['link_type'] == 'udp':
            from .links.udp_link import UdpLink
            link_class = UdpLink
        elif self.config['link_type'] == 'dummy':
            from .links.dummy_link import DummyLink
            link_class = DummyLink
        else:
            raise ValueError('Unknown link type %s' % self.config['link_type'])

        device_link = link_class(self.config['link_config'])    #instantiate the class
        self.comm_handler.open(device_link)
        self.reset_comm()

    def stop_comm(self):
        if self.comm_handler is not None:
            self.comm_handler.close()
        self.reset_comm()

    def refresh_vars(self):
        pass

    def process(self):
        self.device_searcher.process()
        self.heartbeat_generator.process()

        self.handle_comm()      # Make sure request and response are being exchanged with the device
        self.do_state_machine()
        


    def do_state_machine(self):
        if self.comm_broken:
            self.fsm_state = self.FsmState.INIT

        if self.connected:
            time.time() - self.heartbeat_generator.last_valid_heartbeat_timestamp() > self.config['heartbeat_timeout']

        # ===   FSM  ===
        state_entry = True if self.fsm_state != self.last_fsm_state else False
        next_state = self.fsm_state
        if self.fsm_state == self.FsmState.INIT:
            self.reset_comm()
            next_state = self.FsmState.DISCOVERING

        #============= DISCOVERING =====================
        elif self.fsm_state == self.FsmState.DISCOVERING:
            if state_entry:
                self.device_searcher.start()

            found_device_id = self.device_searcher.get_found_device_ascii()
            if found_device_id is not None:
                if self.device_id is None:
                    self.logger.info('Found a device - %s' % found_device_id)
                    self.device_id = found_device_id

                    if found_device_id == DEFAULT_FIRMWARE_ID_ASCII:
                        self.logger.warning("Firmware ID of this device is a default placeholder. Firmware might not have been tagged with a valid ID in the build toolchain.")

            if self.device_id is not None:
                self.device_searcher.stop()
                next_state = self.FsmState.CONNECTING

        #============= CONNECTING =====================
        elif self.fsm_state == self.FsmState.CONNECTING:
            if state_entry:
                self.comm_handler.reset()   # Clear any active transmission. Just for safety
            
            if not self.comm_handler.waiting_response():
                self.comm_handler.send_request(self.protocol.comm_connect())

            if self.comm_handler.has_timed_out():
                self.comm_broken = True
            elif self.comm_handler.response_available():
                response = self.comm_handler.get_response()
                if response.code == ResponseCode.OK:
                    self.session_id = self.protocol.parse_response(response)['session_id']
                    self.logger.debug("Session ID set : 0x%08x" % self.session_id)
                    self.heartbeat_generator.set_session_id(self.session_id)
                    self.heartbeat_generator.start()    # This guy will send recurrent heartbeat request. If that request fails (timeout), comme will be reset
                    self.connected = True
                    next_state = self.FsmState.POLLING_INFO

        elif self.fsm_state == self.FsmState.POLLING_INFO:
            pass




        # ====  FSM END ====

        self.last_fsm_state = self.fsm_state
        if next_state != self.fsm_state:
            self.logger.debug('Moving FSM to state %s' % next_state)
        self.fsm_state = next_state

        

    def handle_comm(self):
        self.comm_handler.process()     # Process reception

        if not self.comm_handler.is_open():
            return
        
        if self.active_request_record is None:  # We haven't send a request
            record = self.dispatcher.next()
            if record is not None:              # A new request to send
                self.active_request_record = record
                self.comm_handler.send_request(record.request)
        else:
            if self.comm_handler.has_timed_out():       # The request we have sent has timed out.. no response
                self.comm_broken = True
                self.comm_handler.clear_timeout()
                self.active_request_record.complete(success=False)

            elif self.comm_handler.waiting_response():      # We are still wiating for a resonse
                if self.comm_handler.response_available():  # We got a response! yay
                    response = self.comm_handler.get_response()

                    try:
                        data = self.protocol.parse_response(response)
                        self.active_request_record.complete(success=True, response=response, response_data=data) # Valid response if we get here.
                    except Exception as e:                   # Malformed response.
                        self.comm_broken = True
                        self.logger.error("Invalid response received. %s" % str(e))
                        self.active_request_record.complete(success=False)

            else:   # should never happen - paranoid check.
                self.comm_broken = True
                self.comm_handler.reset() 
                self.active_request_record.complete(success=False)

            if self.active_request_record.is_completed():   # If we have called a callback, then we are done with this request.
                self.active_request_record = None

        self.comm_handler.process()      # Process new transmission now.

