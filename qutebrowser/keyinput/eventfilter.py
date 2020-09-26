# vim: ft=python fileencoding=utf-8 sts=4 sw=4 et:

# Copyright 2014-2020 Florian Bruhin (The Compiler) <mail@qutebrowser.org>
#
# This file is part of qutebrowser.
#
# qutebrowser is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# qutebrowser is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with qutebrowser.  If not, see <http://www.gnu.org/licenses/>.

"""Global Qt event filter which dispatches key events."""

import typing

from PyQt5.QtCore import pyqtSlot, QObject, QEvent, Qt
from PyQt5.QtGui import QKeyEvent, QWindow, QInputMethodQueryEvent
from PyQt5.QtWidgets import QApplication

from qutebrowser.config import config
from qutebrowser.keyinput import modeman
from qutebrowser.misc import quitter
from qutebrowser.utils import objreg, usertypes, objreg, log


class EventFilter(QObject):

    """Global Qt event filter.

    Attributes:
        _activated: Whether the EventFilter is currently active.
        _handlers; A {QEvent.Type: callable} dict with the handlers for an
                   event.
    """

    def __init__(self, parent: QObject = None) -> None:
        super().__init__(parent)
        self._activated = True
        self._handlers = {
            QEvent.KeyPress: self._handle_key_event,
            QEvent.KeyRelease: self._handle_key_event,
            QEvent.ShortcutOverride: self._handle_key_event,
        }

    def install(self) -> None:
        QApplication.instance().installEventFilter(self)

    @pyqtSlot()
    def shutdown(self) -> None:
        QApplication.instance().removeEventFilter(self)

    def _handle_key_event(self, event: QKeyEvent) -> bool:
        """Handle a key press/release event.

        Args:
            event: The QEvent which is about to be delivered.

        Return:
            True if the event should be filtered, False if it's passed through.
        """
        active_window = QApplication.instance().activeWindow()
        if active_window not in objreg.window_registry.values():
            # Some other window (print dialog, etc.) is focused so we pass the
            # event through.
            return False
        try:
            man = modeman.instance('current')
            return man.handle_event(event)
        except objreg.RegistryUnavailableError:
            # No window available yet, or not a MainWindow
            return False

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        """Handle an event.

        Args:
            obj: The object which will get the event.
            event: The QEvent which is about to be delivered.

        Return:
            True if the event should be filtered, False if it's passed through.
        """
        if not isinstance(obj, QWindow):
            # We already handled this same event at some point earlier, so
            # we're not interested in it anymore.
            return False

        typ = event.type()

        if typ not in self._handlers:
            return False

        if not self._activated:
            return False

        handler = self._handlers[typ]
        try:
            return handler(typing.cast(QKeyEvent, event))
        except:
            # If there is an exception in here and we leave the eventfilter
            # activated, we'll get an infinite loop and a stack overflow.
            self._activated = False
            raise

from PyQt5.QtCore import QTimer

class IMEEventHandler(QObject):

    def __init__(self, parent=None):
        super().__init__(parent)
        self._input_method = QApplication.inputMethod()
        print("adding ime handler")
        self._input_method.cursorRectangleChanged.connect(
            self.cursor_rectangle_changed
        )
        self._last_seen_rect = None

    @pyqtSlot()
    def cursor_rectangle_changed(self):
        # todo:
        #   clear last_seen_rect on mode exit so that you can click on
        #     focused input field and re-enter
        #   last seen rect per window? tab? tab might work better with
        #      remembering focus for tabs
        #   anything to unregister? saw some hangs on crash but might be
        #      because of being a temp basedir
        # some input examples here https://www.javatpoint.com/html-form-input-types
        #  <input type="date">: doesn't report as having an input method enabled,
        #  although the existing heuristics pick it up
        # if insert_mode_auto_load is false but there is a blinking cursor on
        # load clicking the scroll bar will enter insert mode

        new_rect = self._input_method.cursorRectangle()
        if self._last_seen_rect and self._last_seen_rect.contains(new_rect):
            print("contains")
            return

        self._last_seen_rect = new_rect

        # implementation detail: qtwebengine doesn't set anchor for input
        # fields in a web page, qt widgets do, I haven't found any cases where
        # it doesn't work yet. Would like to compare with a "get focused thing
        # and examine" check first and compare across versions.
        anchor_rect = self._input_method.anchorRectangle()
        if anchor_rect:
            print("Not handling because anchor rect is set")
            return

        focused_window = objreg.last_focused_window()
        focusObject = QApplication.focusObject()
        query = None

        if not new_rect and focusObject:
            # sometimes we get a rectangle changed event and the queried
            # rectangle is empty but we are still in an editable element. For
            # instance when pressing enter in a text box on confluence or jira
            # (including comment on the Qt instance) and tabbing between cells
            # on https://html-online.com/editor/
            # Checking ImEnabled helps in these cases.
            query = QInputMethodQueryEvent(Qt.ImEnabled);
            QApplication.sendEvent(focusObject, query);

        if new_rect or (query and query.value(Qt.ImEnabled)):
            log.mouse.debug("Clicked editable element!")
            if config.val.input.insert_mode.auto_enter:
                modeman.enter(focused_window.win_id, usertypes.KeyMode.insert,
                              'click', only_if_normal=True)
        else:
            log.mouse.debug("Clicked non-editable element!")
            if config.val.input.insert_mode.auto_leave:
                modeman.leave(focused_window.win_id, usertypes.KeyMode.insert,
                              'click', maybe=True)


_ime_event_handler_instance = None


def init() -> None:
    """Initialize the global EventFilter instance."""
    event_filter = EventFilter(parent=QApplication.instance())
    event_filter.install()
    quitter.instance.shutting_down.connect(event_filter.shutdown)
    def dothing():
        _ime_event_handler_instance = IMEEventHandler(parent=QApplication.instance())
    QTimer.singleShot(1000, dothing)
