
class DatalogControl(Command):
    _cmd_id = 6

    class Subfunction(Enum):
        GetAvailableTarget = 1
        GetBufferSize = 2
        GetSamplingRates = 3
        ConfigureDatalog = 4
        ListDatalog = 5
        ReadDatalog = 6