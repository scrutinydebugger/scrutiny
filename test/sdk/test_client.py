import unittest

from scrutiny.core.basic_types import *
from scrutiny.sdk.client import ScrutinyClient
import scrutiny.sdk as sdk
import scrutiny.server.datalogging.definitions.device as device_datalogging
from scrutiny.core.variable import Variable as core_Variable
from scrutiny.core.alias import Alias as core_Alias


from scrutiny.server.api import API
from scrutiny.server.api import APIConfig
import scrutiny.server.datastore.datastore as datastore
from scrutiny.server.api.websocket_client_handler import WebsocketClientHandler
from scrutiny.server.device.device_handler import DeviceHandler

from scrutiny.core.firmware_description import FirmwareDescription
from scrutiny.server.device.links.udp_link import UdpLink
from scrutiny.server.device.links.abstract_link import AbstractLink
import scrutiny.server.device.device_info as server_device


import threading
import time
import queue
from functools import partial
from uuid import uuid4

from typing import *

localhost = "127.0.0.1"


class FakeDeviceHandler:
    link_type: Literal['none', 'udp', 'serial']
    link: AbstractLink
    datalogger_state: device_datalogging.DataloggerState
    device_conn_status: DeviceHandler.ConnectionStatus
    comm_session_id: Optional[str]
    datalogging_completion_ratio: Optional[float]
    device_info: server_device.DeviceInfo

    def __init__(self, *args, **kwargs):
        self.link_type = 'udp'
        self.link = UdpLink(
            {
                'host': "127.0.0.1",
                "port": 5555
            }
        )
        self.datalogger_state = device_datalogging.DataloggerState.IDLE
        self.datalogging_completion_ratio = None
        self.device_conn_status = DeviceHandler.ConnectionStatus.DISCONNECTED
        self.comm_session_id = None
        self.set_connection_status(DeviceHandler.ConnectionStatus.CONNECTED_READY)

        self.device_info = server_device.DeviceInfo()
        self.device_info.device_id = "xyz"
        self.device_info.display_name = "fake device"
        self.device_info.max_tx_data_size = 256
        self.device_info.max_rx_data_size = 128
        self.device_info.max_bitrate_bps = 10000
        self.device_info.rx_timeout_us = 50
        self.device_info.heartbeat_timeout_us = 5000000
        self.device_info.address_size_bits = 32
        self.device_info.protocol_major = 1
        self.device_info.protocol_minor = 0
        self.device_info.supported_feature_map = {
            'memory_write': True,
            'datalogging': True,
            'user_command': True,
            '_64bits': True
        }
        self.device_info.forbidden_memory_regions = [
            {'start': 0x100000, 'end': 0x100000 + 128 - 1},
            {'start': 0x200000, 'end': 0x200000 + 256 - 1}
        ]
        self.device_info.readonly_memory_regions = [
            {'start': 0x300000, 'end': 0x300000 + 128 - 1},
            {'start': 0x400000, 'end': 0x400000 + 256 - 1}
        ]

        self.device_info.runtime_published_values = []    # Required to have a value for API to consider data valid

        self.device_info.loops = [
            server_device.FixedFreqLoop(10000, "10khz loop", support_datalogging=True),
            server_device.FixedFreqLoop(100, "100hz loop", support_datalogging=False),
            server_device.VariableFreqLoop("variable freq loop", support_datalogging=True)
        ]

    def get_link_type(self):
        return self.link_type

    def get_comm_link(self):
        return self.link

    def get_device_info(self):
        return self.device_info

    def get_datalogger_state(self):
        return self.datalogger_state

    def get_connection_status(self):
        return self.device_conn_status

    def set_connection_status(self, status: DeviceHandler.ConnectionStatus):
        previous_state = self.device_conn_status
        if status == DeviceHandler.ConnectionStatus.CONNECTED_READY:
            if previous_state != DeviceHandler.ConnectionStatus.CONNECTED_READY:
                self.comm_session_id = uuid4().hex
        else:
            self.comm_session_id = None

        self.device_conn_status = status

    def get_comm_session_id(self):
        return self.comm_session_id

    def get_datalogging_acquisition_completion_ratio(self):
        return self.datalogging_completion_ratio


class FakeDataloggingManager:
    def __init__(self, *args, **kwargs):
        pass


class FakeActiveSFDHandler:
    loaded_callbacks: List[Callable]
    unloaded_callbacks: List[Callable]
    loaded_sfd: FirmwareDescription

    def __init__(self, *args, **kwargs):
        self.loaded_callbacks = []
        self.unloaded_callbacks = []
        self.loaded_sfd = None

    def register_sfd_loaded_callback(self, callback):
        self.loaded_callbacks.append(callback)

    def register_sfd_unloaded_callback(self, callback):
        self.unloaded_callbacks.append(callback)

    def load(self, sfd: FirmwareDescription) -> None:
        self.loaded_sfd = sfd
        for cb in self.loaded_callbacks:
            cb(sfd)

    def unload(self, sfd: FirmwareDescription) -> None:
        self.loaded_sfd = None
        for cb in self.unloaded_callbacks:
            cb(sfd)

    def get_loaded_sfd(self) -> Optional[FirmwareDescription]:
        return self.loaded_sfd


class TestClient(unittest.TestCase):
    datastore: "datastore.Datastore"
    device_handler: FakeDeviceHandler
    datalogging_manager: FakeDataloggingManager
    sfd_handler: FakeActiveSFDHandler
    api: API

    func_queue: "queue.Queue[Callable, threading.Event, float]"
    server_exit_requested: threading.Event
    server_started: threading.Event
    sync_complete: threading.Event
    require_sync: threading.Event
    thread: threading.Thread

    def setUp(self):
        self.func_queue = queue.Queue()
        self.datastore = datastore.Datastore()
        self.fill_datastore()
        self.device_handler = FakeDeviceHandler(self.datastore)
        self.datalogging_manager = FakeDataloggingManager(self.datastore, self.device_handler)
        self.sfd_handler = FakeActiveSFDHandler(device_handler=self.device_handler, datastore=self.datastore)
        api_config: APIConfig = {
            "client_interface_type": 'websocket',
            'client_interface_config': {
                'host': localhost,
                'port': 0
            }
        }
        self.api = API(
            api_config,
            datastore=self.datastore,
            device_handler=self.device_handler,
            sfd_handler=self.sfd_handler,
            datalogging_manager=self.datalogging_manager,
            enable_debug=False)

        self.server_exit_requested = threading.Event()
        self.server_started = threading.Event()
        self.sync_complete = threading.Event()
        self.require_sync = threading.Event()
        self.thread = threading.Thread(target=self.server_thread)
        self.thread.start()
        self.server_started.wait(timeout=1)

        if not self.server_started.is_set():
            raise RuntimeError("Cannot start server")

        port = cast(WebsocketClientHandler, self.api.client_handler).get_port()
        self.client = ScrutinyClient()
        self.client.connect(localhost, port)

    def tearDown(self) -> None:
        self.client.disconnect()
        self.server_exit_requested.set()
        self.thread.join()

    def fill_datastore(self):
        rpv1000 = datastore.DatastoreRPVEntry('/rpv/x1000', RuntimePublishedValue(0x1000, EmbeddedDataType.float32))
        var1 = datastore.DatastoreVariableEntry('/a/b/var1', core_Variable('var1', vartype=EmbeddedDataType.uint32,
                                                path_segments=['a', 'b'], location=0x1234, endianness=Endianness.Little))
        var2 = datastore.DatastoreVariableEntry('/a/b/var2', core_Variable('var2', vartype=EmbeddedDataType.boolean,
                                                path_segments=['a', 'b'], location=0x4568, endianness=Endianness.Little))
        alias_var1 = datastore.DatastoreAliasEntry(core_Alias('/a/b/alias_var1', var1.display_path, var1.get_type()), var1)
        alias_rpv1000 = datastore.DatastoreAliasEntry(core_Alias('/a/b/alias_rpv1000', rpv1000.display_path, rpv1000.get_type()), rpv1000)
        self.datastore.add_entry(rpv1000)
        self.datastore.add_entry(var1)
        self.datastore.add_entry(var2)
        self.datastore.add_entry(alias_var1)
        self.datastore.add_entry(alias_rpv1000)

    def wait_for_server(self, n=2):
        time.sleep(0)
        for i in range(n):
            self.sync_complete.clear()
            self.require_sync.set()
            self.sync_complete.wait()
            self.assertFalse(self.require_sync.is_set())

    def execute_in_server_thread(self, func, timeout=2, wait=True, delay: float = 0):
        completed = threading.Event()
        self.func_queue.put((func, completed, delay))
        if wait:
            completed.wait(timeout)

    def server_thread(self):
        self.api.start_listening()
        self.server_started.set()

        try:
            while not self.server_exit_requested.is_set():
                require_sync_before = False
                if self.require_sync.is_set():
                    require_sync_before = True

                if not self.func_queue.empty():
                    func, event, delay = self.func_queue.get()
                    if delay > 0:
                        time.sleep(delay)
                    func()
                    event.set()
                self.api.process()

                if require_sync_before:
                    self.require_sync.clear()
                    self.sync_complete.set()
                time.sleep(0.005)
        finally:
            self.api.close()

    def test_hold_5_sec(self):
        # Make sure the testing environment and all stubbed classes are stable.
        time.sleep(5)

    def test_get_status(self):
        time.sleep(0.5)  # Should be enough to read the status
        self.assertEqual(self.client.server_state, sdk.ServerState.Connected)
        server_info = self.client.server
        self.assertIsNotNone(server_info)
        assert server_info is not None

        self.assertEqual(server_info.device_comm_state, sdk.DeviceCommState.ConnectedReady)
        self.assertEqual(server_info.device_session_id, self.device_handler.get_comm_session_id())
        self.assertIsNotNone(server_info.device_session_id)

        assert server_info is not None
        self.assertIsNotNone(server_info.device)
        self.assertEqual(server_info.device.device_id, "xyz")
        self.assertEqual(server_info.device.display_name, "fake device")
        self.assertEqual(server_info.device.max_tx_data_size, 256)
        self.assertEqual(server_info.device.max_rx_data_size, 128)
        self.assertEqual(server_info.device.max_bitrate_bps, 10000)
        self.assertEqual(server_info.device.rx_timeout_us, 50)
        self.assertEqual(server_info.device.heartbeat_timeout, 5)
        self.assertEqual(server_info.device.address_size_bits, 32)
        self.assertEqual(server_info.device.protocol_major, 1)
        self.assertEqual(server_info.device.protocol_minor, 0)

        self.assertEqual(server_info.device.supported_features.memory_write, True)
        self.assertEqual(server_info.device.supported_features.datalogging, True)
        self.assertEqual(server_info.device.supported_features.sixtyfour_bits, True)
        self.assertEqual(server_info.device.supported_features.user_command, True)

        self.assertEqual(len(server_info.device.forbidden_memory_regions), 2)
        self.assertEqual(server_info.device.forbidden_memory_regions[0].start, 0x100000)
        self.assertEqual(server_info.device.forbidden_memory_regions[0].end, 0x100000 + 128 - 1)
        self.assertEqual(server_info.device.forbidden_memory_regions[0].size, 128)
        self.assertEqual(server_info.device.forbidden_memory_regions[1].start, 0x200000)
        self.assertEqual(server_info.device.forbidden_memory_regions[1].end, 0x200000 + 256 - 1)
        self.assertEqual(server_info.device.forbidden_memory_regions[1].size, 256)

        self.assertEqual(len(server_info.device.readonly_memory_regions), 2)
        self.assertEqual(server_info.device.readonly_memory_regions[0].start, 0x300000)
        self.assertEqual(server_info.device.readonly_memory_regions[0].end, 0x300000 + 128 - 1)
        self.assertEqual(server_info.device.readonly_memory_regions[0].size, 128)
        self.assertEqual(server_info.device.readonly_memory_regions[1].start, 0x400000)
        self.assertEqual(server_info.device.readonly_memory_regions[1].end, 0x400000 + 256 - 1)
        self.assertEqual(server_info.device.readonly_memory_regions[1].size, 256)

        self.assertEqual(server_info.device_link.type, sdk.DeviceLinkType.UDP)
        self.assertIsInstance(server_info.device_link.config, sdk.UDPLinkConfig)
        assert isinstance(server_info.device_link.config, sdk.UDPLinkConfig)
        self.assertEqual(server_info.device_link.config.host, '127.0.0.1')
        self.assertEqual(server_info.device_link.config.port, 5555)

        self.assertIsNone(server_info.datalogging.completion_ratio)
        self.assertEqual(server_info.datalogging.state, sdk.DataloggerState.Standby)

        self.assertIsNone(server_info.sfd)

        # Make sure the class is readonly.
        with self.assertRaises(Exception):
            server_info.device = None
        with self.assertRaises(Exception):
            server_info.device.display_name = "hello"
        with self.assertRaises(Exception):
            server_info.datalogging = None
        with self.assertRaises(Exception):
            server_info.datalogging.state = None

        self.client.wait_server_status_update()
        self.assertIsNot(self.client.server, server_info)   # Make sure we have a new object with a new reference.

    def set_entry_val(self, path, val):
        self.datastore.get_entry_by_display_path(path).set_value(val)

    def test_fetch_watcahble_info(self):

        rpv1000 = self.client.watch('/rpv/x1000')
        var1 = self.client.watch('/a/b/var1')
        var2 = self.client.watch('/a/b/var2')
        alias_var1 = self.client.watch('/a/b/alias_var1')
        alias_rpv1000 = self.client.watch('/a/b/alias_rpv1000')

        self.assertEqual(rpv1000.type, sdk.WatchableType.RuntimePulishedValue)
        self.assertEqual(rpv1000.display_path, '/rpv/x1000')
        self.assertEqual(rpv1000.name, 'x1000')
        self.assertEqual(rpv1000.datatype, sdk.EmbeddedDataType.float32)

        self.assertEqual(var1.type, sdk.WatchableType.Variable)
        self.assertEqual(var1.display_path, '/a/b/var1')
        self.assertEqual(var1.name, 'var1')
        self.assertEqual(var1.datatype, sdk.EmbeddedDataType.uint32)

        self.assertEqual(var2.type, sdk.WatchableType.Variable)
        self.assertEqual(var2.display_path, '/a/b/var2')
        self.assertEqual(var2.name, 'var2')
        self.assertEqual(var2.datatype, sdk.EmbeddedDataType.boolean)

        self.assertEqual(alias_var1.type, sdk.WatchableType.Alias)
        self.assertEqual(alias_var1.display_path, '/a/b/alias_var1')
        self.assertEqual(alias_var1.name, 'alias_var1')
        self.assertEqual(alias_var1.datatype, sdk.EmbeddedDataType.uint32)

        self.assertEqual(alias_rpv1000.type, sdk.WatchableType.Alias)
        self.assertEqual(alias_rpv1000.display_path, '/a/b/alias_rpv1000')
        self.assertEqual(alias_rpv1000.name, 'alias_rpv1000')
        self.assertEqual(alias_rpv1000.datatype, sdk.EmbeddedDataType.float32)

    def test_watch_non_existent(self):
        with self.assertRaises(sdk.exceptions.OperationFailure):
            self.client.watch('/i/do/not/exist')

    def test_cannot_read_without_first_val(self):
        rpv1000 = self.client.watch('/rpv/x1000')
        with self.assertRaises(sdk.exceptions.InvalidValueError):
            x = rpv1000.value   # Value never set

    def test_read_single_val(self):
        rpv1000 = self.client.watch('/rpv/x1000')

        for i in range(10):
            val = float(i) + 0.5
            self.execute_in_server_thread(partial(self.set_entry_val, '/rpv/x1000', val), wait=False, delay=0.02)
            rpv1000.wait_update()
            self.assertEqual(rpv1000.value, val)

    def test_read_multiple_val(self):
        rpv1000 = self.client.watch('/rpv/x1000')
        var1 = self.client.watch('/a/b/var1')
        var2 = self.client.watch('/a/b/var2')
        alias_var1 = self.client.watch('/a/b/alias_var1')
        alias_rpv1000 = self.client.watch('/a/b/alias_rpv1000')

        def update_all(vals: Tuple[float, int, bool]):
            self.datastore.get_entry_by_display_path(rpv1000.display_path).set_value(vals[0])
            self.datastore.get_entry_by_display_path(var1.display_path).set_value(vals[1])
            self.datastore.get_entry_by_display_path(var2.display_path).set_value(vals[2])

        for i in range(10):
            vals = (float(i) + 0.5, i * 100, i % 2 == 0)
            self.execute_in_server_thread(partial(update_all, vals), wait=False, delay=0.02)
            self.client.wait_new_value_for_all()
            self.assertEqual(rpv1000.value, vals[0])
            self.assertEqual(var1.value, vals[1])
            self.assertEqual(var2.value, vals[2])
            self.assertEqual(alias_var1.value, vals[1])
            self.assertEqual(alias_rpv1000.value, vals[0])
