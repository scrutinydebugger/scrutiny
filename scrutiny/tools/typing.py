
__all__ = ['Self', 'List', 'Set', 'Dict', 'Union', 'Optional', 'Any', 'cast', 'Iterable', 
           'Sequence', 'Callable', 'TypedDict', 'Literal',
           'TypeVar', 'ParamSpec', 'TYPE_CHECKING']

import sys

if sys.version_info >= (3,11):
    from typing import Self
else:
    try:
        # 3.10 and below. setup.py install it if python < 3.10
        from typing_extensions import Self
    except ImportError:
        class Self: # type: ignore
            pass

from typing import List, Set, Dict, Union, Optional, Any, cast, Iterable, Sequence, Callable, TypedDict, Literal, TypeVar, ParamSpec, TYPE_CHECKING