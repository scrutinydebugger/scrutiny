__all__ = [
    'ValueUpdate',
    'BaseListener'  
]

import abc
from dataclasses import dataclass
from datetime import datetime
import queue
import logging
import threading
import traceback
import types
from typing import Union, List, Set, Iterable,Optional, Tuple, Type, Literal

from scrutiny.core.basic_types import EmbeddedDataType
from scrutiny.sdk.watchable_handle import WatchableHandle
from scrutiny.core import validation
from scrutiny.sdk import exceptions as sdk_exceptions

@dataclass(frozen=True)
class ValueUpdate:
    display_path:str
    datatype:EmbeddedDataType
    value: Union[int, float, bool]
    update_timestamp:datetime

class BaseListener(abc.ABC):

    _name:str
    """Name of the listener for logging"""
    _subscriptions:Set[WatchableHandle]
    """List of watchable to listen for"""
    _update_queue:Optional["queue.Queue[Optional[List[ValueUpdate]]]"]
    """Queue of updates moving from the client worker thread to the listener thread"""
    _logger:logging.Logger
    """The logger object"""
    _drop_count:int
    """Number of update dropped"""
    _queue_max_size:int
    """Maximum queue size"""
    _started:bool
    """Flag indicating if the listener thread is started"""
    _thread:Optional[threading.Thread]
    """The listener thread"""
    _setup_error:bool
    """Flag indicating if a an error occured while calling user setup()"""
    _teardown_error:bool
    """Flag indicating if a an error occured while calling user teardown()"""
    _receive_error:bool
    """Flag indicating if a an error occured while calling user receive()"""

    _started_event:threading.Event
    """Event to synchronize start() with its thread."""
    _stop_request_event:threading.Event
    """Event to stop the thread"""

    def __init__(self, 
                 name:Optional[str]=None, 
                 queue_max_size:int=1000
                 ) -> None:
        if name is None:
            name = self.__class__.__name__
        else:
            name = f'{self.__class__.__name__}[{name}]'
        validation.assert_type(name, 'name', str)
        
        self._name = name
        self._subscriptions = set()
        self._update_queue = None
        self._logger = logging.getLogger(self._name)
        self._drop_count = 0
        self._started_event = threading.Event()
        self._stop_request_event = threading.Event()
        self._started = False
        self._thread = None
        self._setup_error=False
        self._teardown_error=False
        self._receive_error=False
        self._queue_max_size=queue_max_size

    def _broadcast_update(self, watchables:List[WatchableHandle]) -> None:
        """
            Method called by the client to notify the listener.
            It should be possible for many clients to update the same listener, 
            so this method is expected to be thread safe.
        """
        if self._started:
            update_list:List[ValueUpdate] =[]
            for watchable in watchables:
                if watchable in self._subscriptions:
                    timestamp = watchable.last_update_timestamp
                    if timestamp is None:
                        timestamp = datetime.now()
                    update = ValueUpdate(
                        datatype=watchable.datatype,
                        display_path=watchable.display_path,
                        value=watchable.value,
                        update_timestamp=timestamp,
                    )
                    update_list.append(update)
            
            if len(update_list) > 0 and self._update_queue is not None:
                if self._logger.isEnabledFor(logging.DEBUG):
                    self._logger.debug(f"Received {len(update_list)} updates")
                try:
                    self._update_queue.put(update_list, block=False)
                except queue.Full:
                    self._drop_count += 1
                    must_print = (self._drop_count < 10 or self._drop_count % 10 == 0)
                    if must_print:
                        self._logger.warning(f"Listener queue is full. Dropping update. (Total dropped={self._drop_count})")

    def _empty_update_queue(self) -> None:
        if self._update_queue is not None:
            while not self._update_queue.empty():
                try:
                    self._update_queue.get_nowait()
                except queue.Empty: 
                    break
    
    def _thread_task(self) -> None:
        self._logger.debug("Thread started. Calling setup()")
        try:
            self.setup()
        except Exception as e:
            self._setup_error=True
            self._logger.error(f"User setup() function raise an exception. {e}")
            self._logger.debug(traceback.format_exc())
        finally:
            self._started_event.set()

        try:
            if not self._setup_error:
                 while not self._stop_request_event.is_set():
                    if self._update_queue is not None:
                        updates = self._update_queue.get()
                        if updates is not None:
                            self.receive(updates)
        except Exception as e:
            self._receive_error = True
            self._logger.error(f"{e}")
            self._logger.debug(traceback.format_exc())
        finally:
            self._logger.debug("Thread exiting. Calling teardown()")
            try:
                self.teardown()
            except Exception as e:
                self._teardown_error=True
                self._logger.error(f"User teardown() function raise an exception. {e}")
                self._logger.debug(traceback.format_exc())
        
        self._logger.debug("Thread exit")

    def __enter__(self) -> "BaseListener":
        return self
    
    def __exit__(self, 
                 exc_type: Optional[Type[BaseException]], 
                 exc_val: Optional[BaseException], 
                 exc_tb: Optional[types.TracebackType]
                 ) -> Literal[False]:
        self.stop()
        return False

    def setup(self) -> None:
        """Function called by the listener when starting before monitoring"""
        pass

    def teardown(self) -> None:
        """Function called by the listener when stopping right after being done monitoring"""
        pass
    
    def subscribe(self, watchables:Union[WatchableHandle, Iterable[WatchableHandle]]) -> None:
        """Add watchables to the list of monitored watchables
        
        :param watchables: The list of watchables to add to the monitor list

        :raise TypeError: Given parameter not of the expected type
        :raise ValueError: Given parameter has an invalid value
        :raise OperationFailure: Failed to complete the batch write
        """
        if self._started:
            raise sdk_exceptions.OperationFailure("Cannot subscribe a watchable once the listener is started")
        if isinstance(watchables, WatchableHandle):
            watchables = [watchables]
        validation.assert_is_iterable(watchables, 'watchables')
        for watchable in watchables:
            validation.assert_type(watchable, 'watchable', WatchableHandle)
            self._subscriptions.add(watchable)
    

    def start(self) -> "BaseListener":
        """
        Starts the listener thread. Once started, no more subscription can be added.

        :raise sdk.exception.OperationFailure: If an error occur while starting the listener
        """
        self._logger.debug("Start requested")
        if self._started:
            raise sdk_exceptions.OperationFailure("Listener already started")
        
        self._stop_request_event.clear()
        self._started_event.clear()
        self._setup_error=False
        self._teardown_error=False
        self._receive_error=False
        self._update_queue = queue.Queue(self._queue_max_size)
        self._thread = threading.Thread(target=self._thread_task)
        self._thread.start()

        self._started_event.wait(2)
        if not self._started_event.is_set():
            self.stop()
            raise sdk_exceptions.OperationFailure("Failed to start listener thread")

        if self._setup_error:
            self.stop()
            raise sdk_exceptions.OperationFailure("Error in listerner setup")
        else:
            self._logger.debug("Started")
            self._started=True

        return self

    def stop(self) -> None:
        """Stops the lsitener thread"""
        self._logger.debug("Stop requested")
        if self._thread is not None:
            if self._thread.is_alive():
                self._stop_request_event.set()
                if self._update_queue is not None:
                    try:
                        self._update_queue.put(None, block=False)
                    except queue.Full:
                        self._empty_update_queue()
                        try:
                            self._update_queue.put(None, block=False)
                        except queue.Full:
                            self._thread.setDaemon(True)

                self._thread.join(timeout=5)
                if self._thread.is_alive():
                    self._logger.error("Failed to stop the thread")
                    self._thread.setDaemon(True)    # Failed to join
    
            self._thread = None
        
        self._empty_update_queue()
        self._update_queue = None

        self._started = False
        self._logger.debug("Stopped")

    @property
    def is_started(self) -> bool:
        """Tells if the listener thread is running"""
        return self._started

    @property
    def name(self) -> str:
        """Return the name of the listener"""
        return self._name
    
    @property
    def drop_count(self) -> int:
        """Return the number of update dropped"""
        return self._drop_count
    
    @property
    def error_occured(self) -> int:
        """Tells if an error occured while running the listener"""
        return self._setup_error or self._receive_error or self._teardown_error

    @abc.abstractmethod
    def receive(self, updates:List[ValueUpdate]) -> None:
        """Method called by the listener thread each time the client notifies the listeners for one or many updates
        
        :params updates: List of updates being broadcast
        """
        raise NotImplementedError("Abstract method")
    
