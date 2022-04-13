from queue import Queue
from scrutiny.server.protocol import Request, Response
from scrutiny.server.server_tools import Timer
from enum import Enum
from copy import copy
import logging
import struct
from binascii import hexlify

class CommHandler:
    """
    This class is the bridge between the application and the communication channel with the device.
    It exchange bytes with the device and exchanges request/response with the upper layer.
    The link object abstract the communication channel.
    """
    class RxData:
        __slots__ = ('data_buffer', 'length', 'length_bytes_received')

        def __init__(self):
            self.clear()

        def clear(self):
            self.length = None
            self.length_bytes_received = 0
            self.data_buffer = bytes()


    DEFAULT_PARAMS = {
        'response_timeout' : 1
    }


    def __init__(self, params={}):
        self.active_request = None      # Contains the request object that has been sent to the device. When None, no request sent and we are standby
        self.received_response = None   # Indicates that a response has been received.
        self.link = None                # Abstracted communication channel that implements  initialize, destroy, write, read
        self.params = copy(self.DEFAULT_PARAMS)
        self.params.update(params)

        self.response_timer = Timer(self.params['response_timeout'])    # Timer for response timeout management
        self.rx_data = self.RxData()    # Contains the response data while we read it.
        self.logger = logging.getLogger(self.__class__.__name__)
        self.opened = False     # True when communication channel is active and working.

    def get_link(self):
        return self.link

    def open(self, link):
        """
            Try to open the communication channel with the device.
        """
        self.link = link
        self.reset()
        try:
            self.link.initialize()
            self.opened = True
        except Exception as e:
            self.logger.error("Cannot connect to device. " + str(e))
            self.opened = False

    def close(self):
        """
            Close the communication channel with the device
        """        
        if self.link is not None:
            self.link.destroy()
            self.link = None
        self.reset()
        self.opened = False

    def is_open(self):
        return self.opened

    def process(self):
        """
        To be called periodically
        """
        if self.link is None:
            self.reset()
            return
        
        self.link.process() # Process the link handling
        self.process_rx()   # Treat response reception

    def process_rx(self):
        # If we haven't got a response or we know we won't get one. Mark the request as timed out
        if self.waiting_response() and (self.response_timer.is_timed_out() or not self.link.operational()):
            self.reset_rx()
            self.timed_out = True
        
        data = self.link.read()
        if data is None or len(data) == 0:
            return  # No data, exit.

        self.logger.debug('Received : %s' % (hexlify(data).decode('ascii')))
        
        if self.response_available() or not self.waiting_response():
            self.logger.debug('Received unwanted data: ' + hexlify(data).decode('ascii'))
            return  # Purposely discard data if we are not expecting any

        self.rx_data.data_buffer += data    # Add data to receive buffer

        if len(self.rx_data.data_buffer) >= 5: # We have a valid command,subcommand, code and length (16btis)
            if self.rx_data.length is None:
                self.rx_data.length, = struct.unpack('>H', self.rx_data.data_buffer[3:5])   # Read the data length

        if self.rx_data.length is not None:     #We already received a valid header
            expected_bytes_count = self.rx_data.length + 9  # payload + header (5 bytes), CRC (4bytes)
            if len(self.rx_data.data_buffer) >= expected_bytes_count:
                self.rx_data.data_buffer = self.rx_data.data_buffer[0:expected_bytes_count]  #Remove extra bytes

                #We have enough data, try to decode the response and validate the CRC.
                try:
                    self.received_response = Response.from_bytes(self.rx_data.data_buffer)  # CRC validation is done here
                    self.logger.debug("Received Response %s" % self.received_response)  # Decoding did not raised an exception, we have a valid payload!
                    self.rx_data.clear()        # Empty the receive buffer
                    self.response_timer.stop()  # Timeout timer can be stop

                    # Validate that the response match the request
                    if self.received_response.command != self.active_request.command:
                        raise Exception("Unexpected Response command ID : %s" % str(self.received_response))
                    if self.received_response.subfn != self.active_request.subfn:
                        raise Exception("Unexpected Response subfunction : %s" % str(self.received_response))

                    #Here, everything went fine. The application can now send a new request or read the received response.
                except Exception as e:
                    self.logger.error("Received malformed message. "  + str(e))
                    self.reset_rx()

    def response_available(self):
        return (self.received_response is not None)

    def has_timed_out(self):
        return self.timed_out

    def clear_timeout(self):
        self.timed_out = False

    def get_response(self):
        """
        Return the response received for the active request
        """
        if self.received_response is None:
            raise Exception('No response to read')

        response = self.received_response   # Make a copy of the response to return before clearing everything
        self.reset_rx() # Since user read the response, it has been acknowledged. Make sure response_available() return False

        return response

    def reset_rx(self):
        # Make sure we can send a new request. 
        # Also clear the received resposne so that response_available() return False
        self.active_request = None
        self.received_response = None
        self.response_timer.stop()
        self.rx_data.clear()

    def send_request(self, request):
        if self.active_request is not None:
            raise Exception('Waiting for a response')

        self.active_request = request
        self.received_response = None
        data = request.to_bytes()
        self.logger.debug("Sending request %s" % request)
        self.logger.debug("Sending : %s" % (hexlify(data).decode('ascii')))
        self.link.write(data)
        self.response_timer.start()
        self.timed_out = False

    def waiting_response(self):
        # We are waiting response if a request is active, meaning it has been sent and reponse has not been acknowledge by the application
        return (self.active_request is not None)

    def reset(self):
        self.reset_rx()
        self.clear_timeout()