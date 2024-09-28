
from scrutiny.gui.dashboard_components.base_component import ScrutinyGUIBaseComponent
from typing import Dict

from qtpy.QtWidgets import QVBoxLayout, QLabel

from scrutiny.gui import assets

class WatchComponent(ScrutinyGUIBaseComponent):
    instance_name : str

    _ICON = assets.get("eye-96x128.png")
    _NAME = "Watch Window"

    def setup(self):
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("YAYAYA : " + self.get_name()))

    def teardown(self):
        pass

    def get_state(self) -> Dict:
        pass

    def load_state(self) -> Dict:
        pass