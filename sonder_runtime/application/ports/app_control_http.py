"""Bounded app-control wire failures, without credential values."""


class ControlError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)
