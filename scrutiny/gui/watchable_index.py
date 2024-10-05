__all__ = ['WatchableIndex', 'WatchableIndexError', 'WatchableIndexNodeContent']


from scrutiny import sdk
from typing import Dict, Iterable, List, Union, Optional, Callable, Set, Any
import threading
from dataclasses import dataclass
import logging


@dataclass
class ParsedFullyQualifiedName:
    __slots__ = ['watchable_type', 'path']

    watchable_type:sdk.WatchableType
    path:str

class WatchableIndexError(Exception):
    pass

TYPESTR_MAP_S2WT = {
    'var' : sdk.WatchableType.Variable,
    'alias' : sdk.WatchableType.Alias,
    'rpv' : sdk.WatchableType.RuntimePublishedValue,
}

TYPESTR_MAP_WT2S: Dict[sdk.WatchableType, str] = {v: k for k, v in TYPESTR_MAP_S2WT.items()}

WatchableValue = Union[int, float, bool]
WatcherValueUpdateCallback = Callable[[str, sdk.WatchableConfiguration, WatchableValue], None]

@dataclass(init=False)
class WatchableEntry:
    """Leaf node in the tree. This object is internal and never given to the user."""
    configuration:sdk.WatchableConfiguration
    value:Optional[WatchableValue]
    watchers:Dict[str, WatcherValueUpdateCallback]
    display_path:str

    def __init__(self, display_path:str, config:sdk.WatchableConfiguration) -> None:
        self.display_path = display_path
        self.configuration=config
        self.value=None
        self.watchers={}

    def register_value_update_callback(self, watcher_id:str, callback:WatcherValueUpdateCallback) -> None:
        if watcher_id in self.watchers:
            raise WatchableIndexError(f"A callback on {self.configuration.watchable_type.name}:{self.display_path} has already been registered to watcher {watcher_id}")
        
        if not callable(callback):
            raise ValueError("Callback is not a callable")
        
        self.watchers[watcher_id] = callback
    
    def unregister_value_update_callback(self, watcher_id:str) -> None:
        if watcher_id not in self.watchers:
            raise WatchableIndexError(f"No callback has been registered to watcher {watcher_id}")
        
        del self.watchers[watcher_id]
    
    def watcher_count(self) -> int:
        return len(self.watchers)
    
    def has_callback_registered(self, watcher_id:str) -> bool:
        return watcher_id in self.watchers

    def update_value(self, value:WatchableValue) -> None:
        self.value = value
        for watcher_id, callback in self.watchers.items():
            callback(watcher_id, self.configuration, value)

    def get_value(self) -> Optional[WatchableValue]:
        return self.value

@dataclass(frozen=True)
class WatchableIndexNodeContent:
    __slots__ = ['watchables', 'subtree']
    watchables:List[sdk.WatchableConfiguration]
    subtree:List[str]

class WatchableIndex:
    _trees:  Dict[str, Any]
    _lock:threading.Lock
    _watched_entries:Dict[str, WatchableEntry] 
    _logger:logging.Logger
    
    def __init__(self) -> None:
        self._trees = {
            sdk.WatchableType.Variable : {},
            sdk.WatchableType.Alias : {},
            sdk.WatchableType.RuntimePublishedValue : {}
        }
        self._lock = threading.Lock()
        self._watched_entries = {}
        self._logger = logging.getLogger(self.__class__.__name__)
    
    def _get_parts(self, path:str) -> List[str]:
        return [x for x in path.split('/') if x]

    def _add_watchable_no_lock(self, path:str, config:sdk.WatchableConfiguration) -> None:
        parts = self._get_parts(path)
        if len(parts) == 0:
            raise WatchableIndexError(f"Empty path : {path}") 
        node = self._trees[config.watchable_type]
        for i in range(len(parts)-1):
            part = parts[i]
            if part not in node:
                node[part] = {}
            node = node[part]
        if parts[-1] in node:
            raise WatchableIndexError(f"Cannot insert a watchable at location {path}. Another watchable already uses that path.")
        node[parts[-1]] = WatchableEntry(
            display_path=path,  # Required for proper error messages.
            config=config
            )

    def _get_node_with_lock(self, watchable_type:sdk.WatchableType, path:str) -> Union[WatchableIndexNodeContent, WatchableEntry]:
        with self._lock:
            parts = self._get_parts(path)
            node = self._trees[watchable_type]
            for part in parts:
                if part not in node:
                    raise WatchableIndexError(f"Inexistent path : {path} ")
                node = node[part]

            if isinstance(node, dict):
                return WatchableIndexNodeContent(
                    watchables=[val.configuration for val in node.values() if isinstance(val, WatchableEntry)],
                    subtree=[name for name, val in node.items() if isinstance(val, dict)]
                )
            elif isinstance(node, WatchableEntry):
                return node
            else:
                raise WatchableIndexError(f"Unexpected item of type {node.__class__.__name__} inside the index")
    
    def _has_data(self, watchable_type:sdk.WatchableType) -> bool:
        return len(self._trees[watchable_type]) > 0

    def update_value_fqn(self, fqn:str, value:WatchableValue) -> None:
        """Update the watchable value and inform all watchers
        
        :param fqn: The watchable fully qualified name
        :param value: The value to broadcast
        """
        parsed = self.parse_fqn(fqn)
        self.update_value(parsed.watchable_type, parsed.path, value)

    def update_value_by_server_id(self, server_id:str, value:WatchableValue) -> None:
        """Update the watchable value and inform all watchers only if part of the watched entries
        
        :param server_id: The server ID received by the server
        :param value: The value to broadcast
        
        """
        try:
            entry = self._watched_entries[server_id]
        except KeyError:
            return  # Silently ignore
        
        entry.update_value(value)

    def update_value(self, watchable_type:sdk.WatchableType, path:str, value:WatchableValue) -> None:
        """Update the watchable value and inform all watchers
        
        :param watchable_type: The watchable type
        :param path: The watchable tree path
        :param value: The value to broadcast
        """
        node = self._get_node_with_lock(watchable_type, path)
        if not isinstance(node, WatchableEntry):
            raise WatchableIndexError("Cannot update a value on something that is not a Watchable")
        node.update_value(value)
    
    def watch_fqn(self, watcher_id:str, fqn:str, callback:WatcherValueUpdateCallback) -> None:
        """Register a callback to be invoked when a value is updated on the given watchable
        
        :param watcher_id: A string identifies the owner of the callback. Passed back when the callback is invoked
        :param fqn: The watchable fully qualified name
        :param callback: The callback
        """
        parsed = self.parse_fqn(fqn)
        self.watch(watcher_id, parsed.watchable_type, parsed.path, callback)

    def watch(self, watcher_id:str, watchable_type:sdk.WatchableType, path:str, callback:WatcherValueUpdateCallback) -> None:
        """Register a callback to be invoked when a value is updated on the given watchable
        
        :param watcher_id: A string identifies the owner of the callback. Passed back when the callback is invoked
        :param watchable_type: The watchable type
        :param path: The watchable tree path
        :param callback: The callback
        """
        node = self._get_node_with_lock(watchable_type, path)
        if not isinstance(node, WatchableEntry):
            raise WatchableIndexError("Cannot watch something that is not a Watchable")
        
        node.register_value_update_callback(watcher_id, callback)
        
        with self._lock:
            self._watched_entries[node.configuration.server_id] = node

    def unwatch(self, watcher_id:str, watchable_type:sdk.WatchableType, path:str) -> None:
        node = self._get_node_with_lock(watchable_type, path)
        if not isinstance(node, WatchableEntry):
            raise WatchableIndexError("Cannot unwatch something that is not a Watchable")
        
        if node.has_callback_registered(watcher_id):
            node.unregister_value_update_callback(watcher_id)
        
        if node.watcher_count() == 0:
            try:
                del self._watched_entries[node.configuration.server_id]
            except KeyError:
                pass
    
    def unwatch_fqn(self, watcher_id:str, fqn:str) -> None:
        parsed = self.parse_fqn(fqn)
        self.unwatch(watcher_id, parsed.watchable_type, parsed.path)

    def watcher_count_by_server_id(self, server_id:str) -> int:
        try:
            entry = self._watched_entries[server_id]
        except KeyError:
            return 0
        return entry.watcher_count()

    def watcher_count_fqn(self, fqn:str) -> int:
        parsed = self.parse_fqn(fqn)
        return self.watcher_count(parsed.watchable_type, parsed.path)
       
    def watcher_count(self, watchable_type:sdk.WatchableType, path:str) -> int:
        node = self._get_node_with_lock(watchable_type, path)
        if not isinstance(node, WatchableEntry):
            raise WatchableIndexError("Cannot get the watcher count of something that is not a Watchable")
        return node.watcher_count()
    
    def watched_entries_count(self) -> int:
        return len(self._watched_entries)
    
    def get_value_fqn(self, fqn:str) -> Optional[WatchableValue]:
        """Reads the last value written to this watchable
        
        :param fqn: The watchable fully qualified name
        :return: The last value written or ``None``
        """
        parsed = self.parse_fqn(fqn)
        return self.get_value(parsed.watchable_type, parsed.path)

    def get_value(self, watchable_type:sdk.WatchableType, path:str) -> Optional[WatchableValue]:
        """Reads the last value written to this watchable
        
        :param watchable_type: The watchable type
        :param path: The watchable tree path
        :return: The last value written or ``None``
        """
        node = self._get_node_with_lock(watchable_type, path)
        if not isinstance(node, WatchableEntry):
            raise WatchableIndexError("Cannot read a value on something that is not a Watchable")
        return node.get_value()

    def read(self, watchable_type:sdk.WatchableType, path:str) -> Union[WatchableIndexNodeContent, sdk.WatchableConfiguration]:
        """Read a node inside the index.
        
        :watchable_type: The type of node to read
        :path: The tree path of the node

        :return: The node content. Either a watchable or a description of the subnodes
        """
        node = self._get_node_with_lock(watchable_type, path)
        if isinstance(node, WatchableEntry):
            return node.configuration
        return node

    def add_watchable(self, path:str, obj:sdk.WatchableConfiguration) -> None:
        """Adds a watcahble inside the index

        :param path: The tree path of the node
        :param obj: The watchable configuration object
        """
        with self._lock:
            return self._add_watchable_no_lock(path, obj)
    
    def add_watchable_fqn(self, fqn:str, obj:sdk.WatchableConfiguration) -> None:
        """Adds a watcahble inside the index using a fully qualified name

        :param fqn: The fully qualified name created using ``make_fqn()``
        :param obj: The watchable configuration object
        """
        parsed = self.parse_fqn(fqn)
        self._validate_fqn(parsed, obj)
        return self.add_watchable(parsed.path, obj)
    
    def read_fqn(self, fqn:str) -> Union[WatchableIndexNodeContent, sdk.WatchableConfiguration]:
        """Read a node inside the index using a fully qualified name.
        
        :param fqn: The fully qualified name created using ``make_fqn()``

        :return: The node content. Either a watchable or a description of the subnodes
        """        
        parsed = self.parse_fqn(fqn)
        return self.read(parsed.watchable_type, parsed.path)

    def add_content(self, data:Dict[sdk.WatchableType, Dict[str, sdk.WatchableConfiguration]]) -> None:
        """Add content of the given types.
        Triggers ``changed``.  May trigger ``filled`` if all types have data after calling this function.
        
        :param data: The data to add. Classified in dict[watchable_type][path]. 
        """
        with self._lock:
            for subdata in data.values():
                for path, wc in subdata.items():
                    self._add_watchable_no_lock(path, wc)
    
    def clear_content_by_type(self, watchable_type:sdk.WatchableType) -> bool:
        """
        Clear the content of the given type from the index. 
        May triggers ``changed`` and ``cleared`` if data was actually removed.

        :return: ``True`` if data was removed. ``False`` if the nothing was removed (already empty)
        """
        with self._lock:
            changed = False
            had_data = len(self._trees[watchable_type]) > 0
            self._trees[watchable_type] = {}

            if had_data:
                changed = True

            to_remove:Set[str] = set()
            for server_id, entry in self._watched_entries.items():
                if entry.configuration.watchable_type == watchable_type:
                    entry.watchers.clear()
                    to_remove.add(server_id)
            
            for server_id in to_remove:
                del self._watched_entries[server_id]

        return changed

    def clear(self) -> bool:
        """
        Clear all the content from the index.

        :return: ``True`` if data was removed. ``False`` if the nothing was removed (already empty) 
        """
        with self._lock:
            self._watched_entries.clear()
            had_data = False
            for wt in [sdk.WatchableType.Variable, sdk.WatchableType.Alias, sdk.WatchableType.RuntimePublishedValue]:
                if self._has_data(wt):
                    had_data = True

            self._trees[sdk.WatchableType.Variable] = {}
            self._trees[sdk.WatchableType.Alias] = {}
            self._trees[sdk.WatchableType.RuntimePublishedValue] = {}
        
        return had_data

    def has_data(self, watchable_type:sdk.WatchableType) -> bool:
        """Tells if there is data of the given type inside the index
        
        :param watchable_type: The type of watchable to look for
        :return: ``True`` if there is data of that type. ``False otherwise``
        """
        with self._lock:
            return self._has_data(watchable_type)


    @classmethod
    def _validate_fqn(cls, fqn:ParsedFullyQualifiedName, desc:sdk.WatchableConfiguration) -> None:
        if fqn.watchable_type!= desc.watchable_type:
            raise WatchableIndexError("Watchable fully qualified name doesn't embded the type correctly.")
  
    @staticmethod
    def parse_fqn(fqn:str) -> ParsedFullyQualifiedName:
        """Parses a fully qualified name and return the information needed to query the index.
        
        :param fqn: The fully qualified name
        
        :return: An object containing the type and the tree path separated
        """
        index = fqn.find(':')
        if index == -1:
            raise WatchableIndexError("Bad fully qualified name")
        typestr = fqn[0:index]
        if typestr not in TYPESTR_MAP_S2WT:
            raise WatchableIndexError(f"Unknown watchable type {typestr}")
    
        return ParsedFullyQualifiedName(
            watchable_type=TYPESTR_MAP_S2WT[typestr],
            path=fqn[index+1:]
        )

    @staticmethod
    def make_fqn(watchable_type:sdk.WatchableType, path:str) -> str:
        """Create a string representation that conveys enough information to find a specific element in the index.
        Contains the type and the tree path. 
        
        :param watchable_type: The SDK watchable type
        :param path: The tree path
        
        :return: A fully qualified name containing the type and the tree path
        """
        return f"{TYPESTR_MAP_WT2S[watchable_type]}:{path}"
    