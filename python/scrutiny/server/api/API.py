#    API.py
#        Manages the websocket API to talk with the multiple clients. Can be a GUI client
#        or a CLI client
#
#   - License : MIT - See LICENSE file.
#   - Project : Scrutiny Debugger (github.com/scrutinydebugger/scrutiny)
#
#   Copyright (c) 2021-2022 scrutinydebugger

import os
import sys
import logging

from scrutiny.server.datastore import Datastore, DatastoreEntry
from scrutiny.server.tools import Timer
from scrutiny.server.device import DeviceHandler
from scrutiny.server.active_sfd_handler import ActiveSFDHandler
from scrutiny.core.sfd_storage import SFDStorage

from .websocket_client_handler import WebsocketClientHandler
from .dummy_client_handler import DummyClientHandler
from .value_streamer import ValueStreamer

from .abstract_client_handler import AbstractClientHandler, ClientHandlerConfig, ClientHandlerMessage

from scrutiny.core.typehints import GenericCallback
from typing import Callable, Dict, List, Set, Any, TypedDict


class APIConfig(TypedDict, total=False):
    client_interface_type: str
    client_interface_config: Any


class UpdateVarCallback(GenericCallback):
    callback: Callable[[str, DatastoreEntry], None]


class InvalidRequestException(Exception):
    def __init__(self, req, msg):
        super().__init__(msg)
        self.req = req


class API:

    # List of commands that can be shared with the clients
    class Command:
        class Client2Api:
            ECHO = 'echo'
            GET_WATCHABLE_LIST = 'get_watchable_list'
            GET_WATCHABLE_COUNT = 'get_watchable_count'
            SUBSCRIBE_WATCHABLE = 'subscribe_watchable'
            UNSUBSCRIBE_WATCHABLE = 'unsubscribe_watchable'
            GET_INSTALLED_SFD = 'get_installed_sfd'
            GET_LOADED_SFD = 'get_loaded_sfd'
            LOAD_SFD = 'load_sfd'

        class Api2Client:
            ECHO_RESPONSE = 'response_echo'
            GET_WATCHABLE_LIST_RESPONSE = 'response_get_watchable_list'
            GET_WATCHABLE_COUNT_RESPONSE = 'response_get_watchable_count'
            SUBSCRIBE_WATCHABLE_RESPONSE = 'response_subscribe_watchable'
            UNSUBSCRIBE_WATCHABLE_RESPONSE = 'response_unsubscribe_watchable'
            WATCHABLE_UPDATE = 'watchable_update'
            GET_INSTALLED_SFD_RESPONSE = 'response_get_installed_sfd'
            LOAD_SFD_RESPONSE = 'response_load_sfd'
            GET_LOADED_SFD_RESPONSE = 'response_get_loaded_sfd'
            ERROR_RESPONSE = 'error'

    FLUSH_VARS_TIMEOUT: float = 0.1

    entry_type_to_str: Dict[DatastoreEntry.EntryType, str] = {
        DatastoreEntry.EntryType.Var: 'var',
        DatastoreEntry.EntryType.Alias: 'alias',
    }

    str_to_entry_type: Dict[str, DatastoreEntry.EntryType] = {
        'var': DatastoreEntry.EntryType.Var,
        'alias': DatastoreEntry.EntryType.Alias
    }

    datastore: Datastore
    device_handler: DeviceHandler
    logger: logging.Logger
    connections: Set[str]
    streamer: ValueStreamer
    req_count: int
    client_handler: AbstractClientHandler
    sfd_handler: ActiveSFDHandler

    # The method to call for each command
    ApiRequestCallbacks: Dict[str, str] = {
        Command.Client2Api.ECHO: 'process_echo',
        Command.Client2Api.GET_WATCHABLE_LIST: 'process_get_watchable_list',
        Command.Client2Api.GET_WATCHABLE_COUNT: 'process_get_watchable_count',
        Command.Client2Api.SUBSCRIBE_WATCHABLE: 'process_subscribe_watchable',
        Command.Client2Api.UNSUBSCRIBE_WATCHABLE: 'process_unsubscribe_watchable',
        Command.Client2Api.GET_INSTALLED_SFD: 'process_get_installed_sfd',
        Command.Client2Api.LOAD_SFD: 'process_load_sfd',
        Command.Client2Api.GET_LOADED_SFD: 'process_get_loaded_sfd'
    }

    def __init__(self, config: APIConfig, datastore: Datastore, device_handler: DeviceHandler, sfd_handler: ActiveSFDHandler):
        self.validate_config(config)

        if config['client_interface_type'] == 'websocket':
            self.client_handler = WebsocketClientHandler(config['client_interface_config'])
        elif config['client_interface_type'] == 'dummy':
            self.client_handler = DummyClientHandler(config['client_interface_config'])
        else:
            raise NotImplementedError('Unsupported client interface type. %s', config['client_interface_type'])

        self.datastore = datastore
        self.device_handler = device_handler
        self.sfd_handler = sfd_handler
        self.logger = logging.getLogger('scrutiny.' + self.__class__.__name__)
        self.connections = set()            # Keep a list of all clients connections
        self.streamer = ValueStreamer()     # The value streamer takes cares of publishing values to the client without polling.
        self.req_count = 0

    def get_client_handler(self) -> AbstractClientHandler:
        return self.client_handler

    def open_connection(self, conn_id: str) -> None:
        self.connections.add(conn_id)
        self.streamer.new_connection(conn_id)

    def close_connection(self, conn_id: str) -> None:
        self.connections.remove(conn_id)
        self.streamer.clear_connection(conn_id)

    def is_new_connection(self, conn_id: str) -> bool:
        return True if conn_id not in self.connections else False

    # Extract a chunk of data from the value streamer and send it to the clients.
    def stream_all_we_can(self) -> None:
        for conn_id in self.connections:
            chunk = self.streamer.get_stream_chunk(conn_id)     # get a list of entry to send to this connection

            if len(chunk) == 0:
                continue

            msg = {
                'cmd': self.Command.Api2Client.WATCHABLE_UPDATE,
                'updates': [dict(id=x.get_id(), value=x.get_value()) for x in chunk]
            }

            self.client_handler.send(ClientHandlerMessage(conn_id=conn_id, obj=msg))

    def validate_config(self, config: APIConfig):
        if 'client_interface_type' not in config:
            raise ValueError('Missing entry in API config : client_interface_type ')

        if 'client_interface_config' not in config:
            raise ValueError('Missing entry in API config : client_interface_config')

    # Launch the client interface handler
    def start_listening(self) -> None:
        self.client_handler.start()

    # to be called periodically
    def process(self) -> None:
        self.client_handler.process()
        while self.client_handler.available():
            popped = self.client_handler.recv()
            assert popped is not None  # make mypy happy
            conn_id = popped.conn_id
            obj = popped.obj

            if self.is_new_connection(conn_id):
                self.logger.debug('Opening connection %s' % conn_id)
                self.open_connection(conn_id)

            self.process_request(conn_id, obj)

        # Close  dead connections
        conn_to_close = [conn_id for conn_id in self.connections if not self.client_handler.is_connection_active(conn_id)]
        for conn_id in conn_to_close:
            self.logger.debug('Closing connection %s' % conn_id)
            self.close_connection(conn_id)

        self.streamer.process()
        self.stream_all_we_can()

    # Process a request gotten from the Client Handler

    def process_request(self, conn_id: str, req: Dict[str, Any]):
        try:
            self.req_count += 1
            self.logger.debug('[Conn:%s] Processing request #%d - %s' % (conn_id, self.req_count, req))

            if 'cmd' not in req:
                raise InvalidRequestException(req, 'No command in request')

            cmd = req['cmd']
            if cmd in self.ApiRequestCallbacks:
                callback = getattr(self, self.ApiRequestCallbacks[cmd])
                callback.__call__(conn_id, req)
            else:
                raise InvalidRequestException(req, 'Unsupported command %s' % cmd)

        except InvalidRequestException as e:
            self.logger.debug('[Conn:%s] Invalid request #%d. %s' % (conn_id, self.req_count, str(e)))
            response = self.make_error_response(req, str(e))
            self.client_handler.send(ClientHandlerMessage(conn_id=conn_id, obj=response))
        except Exception as e:
            self.logger.error('[Conn:%s] Unexpected error while processing request #%d. %s' % (conn_id, self.req_count, str(e)))
            response = self.make_error_response(req, 'Internal error')
            self.client_handler.send(ClientHandlerMessage(conn_id=conn_id, obj=response))

    # === ECHO ====
    def process_echo(self, conn_id: str, req: Dict[str, Any]) -> None:
        if 'payload' not in req:
            raise InvalidRequestException(req, 'Missing payload')
        response = dict(cmd=self.Command.Api2Client.ECHO_RESPONSE, payload=req['payload'])
        self.client_handler.send(ClientHandlerMessage(conn_id=conn_id, obj=response))

    #  ===  GET_WATCHABLE_LIST     ===
    def process_get_watchable_list(self, conn_id: str, req: Dict[str, Any]) -> None:
        # Improvement : This may be a big response. Generate multi-packet response in a worker thread
        # Not asynchronous by choice
        max_per_response = None
        if 'max_per_response' in req:
            if not isinstance(req['max_per_response'], int):
                raise InvalidRequestException(req, 'Invalid max_per_response content')

            max_per_response = req['max_per_response']

        type_to_include = []
        if self.is_dict_with_key(req, 'filter'):
            if self.is_dict_with_key(req['filter'], 'type'):
                if isinstance(req['filter']['type'], list):
                    for t in req['filter']['type']:
                        if t not in self.str_to_entry_type:
                            raise InvalidRequestException(req, 'Insupported type filter :"%s"' % (t))

                        type_to_include.append(self.str_to_entry_type[t])

        if len(type_to_include) == 0:
            type_to_include = [DatastoreEntry.EntryType.Var, DatastoreEntry.EntryType.Alias]

        variables = self.datastore.get_entries_list_by_type(DatastoreEntry.EntryType.Var) if DatastoreEntry.EntryType.Var in type_to_include else []
        alias = self.datastore.get_entries_list_by_type(DatastoreEntry.EntryType.Alias) if DatastoreEntry.EntryType.Alias in type_to_include else []

        done = False
        while not done:
            if max_per_response is None:
                alias_to_send = alias
                var_to_send = variables
                done = True
            else:
                nAlias = min(max_per_response, len(alias))
                alias_to_send = alias[0:nAlias]
                alias = alias[nAlias:]

                nVar = min(max_per_response - nAlias, len(variables))
                var_to_send = variables[0:nVar]
                variables = variables[nVar:]

                done = True if len(variables) + len(alias) == 0 else False

            response = {
                'cmd': self.Command.Api2Client.GET_WATCHABLE_LIST_RESPONSE,
                'qty': {
                    'var': len(var_to_send),
                    'alias': len(alias_to_send)
                },
                'content': {
                    'var': [self.make_datastore_entry_definition(x) for x in var_to_send],
                    'alias': [self.make_datastore_entry_definition(x) for x in alias_to_send]
                },
                'done': done
            }

            self.client_handler.send(ClientHandlerMessage(conn_id=conn_id, obj=response))

    #  ===  GET_WATCHABLE_COUNT ===
    def process_get_watchable_count(self, conn_id: str, req: Dict[str, Any]) -> None:
        response = {
            'cmd': self.Command.Api2Client.GET_WATCHABLE_COUNT_RESPONSE,
            'qty': {
                'var': self.datastore.get_entries_count(DatastoreEntry.EntryType.Var),
                'alias': self.datastore.get_entries_count(DatastoreEntry.EntryType.Alias)
            }
        }

        self.client_handler.send(ClientHandlerMessage(conn_id=conn_id, obj=response))

    #  ===  SUBSCRIBE_WATCHABLE ===
    def process_subscribe_watchable(self, conn_id: str, req: Dict[str, str]) -> None:
        if 'watchables' not in req and not isinstance(req['watchables'], list):
            raise InvalidRequestException(req, 'Invalid or missing watchables list')

        for watchable in req['watchables']:
            try:
                entry = self.datastore.get_entry(watchable)
            except KeyError as e:
                raise InvalidRequestException(req, 'Unknown watchable ID : %s' % str(watchable))

        for watchable in req['watchables']:
            self.datastore.start_watching(watchable, watcher=conn_id, callback=UpdateVarCallback(self.var_update_callback))

        response = {
            'cmd': self.Command.Api2Client.SUBSCRIBE_WATCHABLE_RESPONSE,
            'watchables': req['watchables']
        }

        self.client_handler.send(ClientHandlerMessage(conn_id=conn_id, obj=response))

    #  ===  UNSUBSCRIBE_WATCHABLE ===
    def process_unsubscribe_watchable(self, conn_id: str, req: Dict[str, str]) -> None:
        if 'watchables' not in req and not isinstance(req['watchables'], list):
            raise InvalidRequestException(req, 'Invalid or missing watchables list')

        for watchable in req['watchables']:
            try:
                entry = self.datastore.get_entry(watchable)
            except KeyError as e:
                raise InvalidRequestException(req, 'Unknown watchable ID : %s' % str(watchable))

        for watchable in req['watchables']:
            self.datastore.stop_watching(watchable, watcher=conn_id)

        response = {
            'cmd': self.Command.Api2Client.SUBSCRIBE_WATCHABLE_RESPONSE,
            'watchables': req['watchables']
        }

        self.client_handler.send(ClientHandlerMessage(conn_id=conn_id, obj=response))

    def process_get_installed_sfd(self, conn_id: str, req: Dict[str, str]):
        firmware_id_list = SFDStorage.list()
        metadata_dict = {}
        for firmware_id in firmware_id_list:
            metadata_dict[firmware_id] = SFDStorage.get_metadata(firmware_id)

        response = {
            'cmd': self.Command.Api2Client.GET_INSTALLED_SFD_RESPONSE,
            'sfd_list': metadata_dict
        }

        self.client_handler.send(ClientHandlerMessage(conn_id=conn_id, obj=response))

    def process_get_loaded_sfd(self, conn_id: str, req: Dict[str, str]):
        sfd = self.sfd_handler.get_loaded_sfd()

        response = {
            'cmd': self.Command.Api2Client.GET_LOADED_SFD_RESPONSE,
            'firmware_id': sfd.get_firmware_id() if sfd is not None else None
        }

        self.client_handler.send(ClientHandlerMessage(conn_id=conn_id, obj=response))

    def process_load_sfd(self, conn_id: str, req: Dict[str, str]):
        if 'firmware_id' not in req and not isinstance(req['firmware_id'], str):
            raise InvalidRequestException(req, 'Invalid firmware_id')

        success = True
        try:
            self.sfd_handler.request_load_sfd(req['firmware_id'])
        except Exception as e:
            self.logger.error('Cannot load SFD %s. %s' % (req['firmware_id'], str(e)))
            success = False

        response = {
            'cmd': self.Command.Api2Client.LOAD_SFD_RESPONSE,
            'success': success
        }

        self.client_handler.send(ClientHandlerMessage(conn_id=conn_id, obj=response))

    def var_update_callback(self, conn_id: str, datastore_entry: DatastoreEntry) -> None:
        self.streamer.publish(datastore_entry, conn_id)
        self.stream_all_we_can()

    def make_datastore_entry_definition(self, entry: DatastoreEntry) -> Dict[str, str]:
        return {
            'id': entry.get_id(),
            'type': self.entry_type_to_str[entry.get_type()],
            'display_path': entry.get_display_path(),
        }

    def make_error_response(self, req: Dict[str, str], msg: str) -> Dict[str, Any]:
        cmd = '<empty>'
        if 'cmd' in req:
            cmd = req['cmd']
        response = {
            'cmd': self.Command.Api2Client.ERROR_RESPONSE,
            'request_cmd': cmd,
            'msg': msg
        }
        return response

    def is_dict_with_key(self, d: Dict[Any, Any], k: Any):
        return isinstance(d, dict) and k in d

    def close(self) -> None:
        self.client_handler.stop()
