#    alias.py
#        Class that contains the definition of an alias.
#
#   - License : MIT - See LICENSE file.
#   - Project :  Scrutiny Debugger (github.com/scrutinydebugger/scrutiny-python)
#
#   Copyright (c) 2021-2022 Scrutiny Debugger

import json
import math

from scrutiny.server.datastore.entry_type import EntryType

from typing import Dict, Optional, Any, Union


class Alias:
    fullpath: str
    target: str
    target_type: Optional[EntryType]
    gain: Optional[float]
    offset: Optional[float]
    min: Optional[float]
    max: Optional[float]

    @classmethod
    def from_json(cls, fullpath: str, json_str: str) -> 'Alias':
        d = json.loads(json_str)
        return cls.from_dict(fullpath, d)

    @classmethod
    def from_dict(cls, fullpath: str, obj: Dict[str, Any]) -> 'Alias':
        assert 'target' in obj, 'Missing target'

        obj_out = cls(
            fullpath=fullpath,
            target=obj['target'],
            target_type=obj['target_type'] if 'target_type' in obj else None,
            gain=obj['gain'] if 'gain' in obj else None,
            offset=obj['offset'] if 'offset' in obj else None,
            min=obj['min'] if 'min' in obj else None,
            max=obj['max'] if 'max' in obj else None
        )
        obj_out.validate()
        return obj_out

    def __init__(self, fullpath: str, target: str, target_type: Optional[EntryType] = None, gain: Optional[float] = None, offset: Optional[float] = None, min: Optional[float] = None, max: Optional[float] = None):
        self.fullpath = fullpath
        if target_type is not None:
            target_type = EntryType(target_type)
            if target_type == EntryType.Alias:
                raise ValueError("Cannot make an alias over another alias.")
            self.target_type = EntryType(target_type)
        else:
            self.target_type = None
        self.target = target
        self.gain = float(gain) if gain is not None else None
        self.offset = float(offset) if offset is not None else None
        self.min = float(min) if min is not None else None
        self.max = float(max) if max is not None else None

    def validate(self):
        if not self.fullpath or not isinstance(self.fullpath, str):
            raise ValueError('fullpath is not valid')

        if self.target_type is not None:
            EntryType(self.target_type)  # Make sure conversion is possible

        if not self.target or not isinstance(self.target, str):
            raise ValueError('Alias (%s) target is not valid' % self.fullpath)

        if not isinstance(self.get_gain(), float) or math.isnan(self.get_gain()):
            raise ValueError('Alias (%s) gain is not a valid float' % self.fullpath)
        if not isinstance(self.get_offset(), float) or math.isnan(self.get_offset()):
            raise ValueError('Alias (%s) offset is not a valid float' % self.fullpath)
        if not isinstance(self.get_min(), float) or math.isnan(self.get_min()):
            raise ValueError('Alias (%s) minimum value is not a valid float' % self.fullpath)
        if not isinstance(self.get_max(), float) or math.isnan(self.get_max()):
            raise ValueError('Alias (%s) maximum is not a valid float' % self.fullpath)

        if self.get_min() > self.get_max():
            raise ValueError('Max (%s) > min (%s)' % (str(self.max), str(self.min)))

        if not math.isfinite(self.get_gain()):
            raise ValueError('Gain is not a finite value')

        if not math.isfinite(self.get_offset()):
            raise ValueError('Gain is not a finite value')

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = dict(target=self.target, target_type=self.target_type)

        if self.gain is not None and self.gain != 1.0:
            d['gain'] = self.gain

        if self.offset is not None and self.offset != 0.0:
            d['offset'] = self.offset

        if self.min is not None and self.min != float('-inf'):
            d['min'] = self.min

        if self.max is not None and self.max != float('inf'):
            d['max'] = self.max

        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    def get_fullpath(self) -> str:
        return self.fullpath

    def get_target(self) -> str:
        return self.target

    def get_target_type(self) -> EntryType:
        if self.target_type is None:
            raise RuntimeError('Target type for alias %s is not set' % self.get_fullpath())
        return self.target_type

    def set_target_type(self, target_type: EntryType):
        if self.target_type == EntryType.Alias:
            raise ValueError('Alias %s point onto another alias (%s)' % (self.get_fullpath(), self.get_target()))
        self.target_type = target_type

    def get_min(self) -> float:
        return self.min if self.min is not None else float('-inf')

    def get_max(self) -> float:
        return self.max if self.max is not None else float('inf')

    def get_gain(self) -> float:
        return self.gain if self.gain is not None else 1.0

    def get_offset(self) -> float:
        return self.offset if self.offset is not None else 0.0

    def compute_user_to_device(self, value: Union[int, float, bool]) -> Union[int, float, bool]:
        if isinstance(value, int) or isinstance(value, float):
            value = min(value, self.get_max())
            value = max(value, self.get_min())
            value -= self.get_offset()
            value /= self.get_gain()
        return value

    def compute_device_to_user(self, value: Union[int, float, bool]) -> Union[int, float, bool]:
        if isinstance(value, int) or isinstance(value, float):
            value *= self.get_gain()
            value += self.get_offset()
        return value