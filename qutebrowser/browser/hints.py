# Copyright 2014 Florian Bruhin (The Compiler) <mail@qutebrowser.org>
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

"""A HintManager to draw hints over links."""

import logging
import math
from collections import namedtuple

from PyQt5.QtCore import pyqtSignal, pyqtSlot, QObject, QEvent, Qt
from PyQt5.QtGui import QMouseEvent, QClipboard
from PyQt5.QtWidgets import QApplication

import qutebrowser.config.config as config
import qutebrowser.utils.message as message
import qutebrowser.utils.url as urlutils
from qutebrowser.utils.keyparser import KeyParser


ElemTuple = namedtuple('ElemTuple', 'elem, label')


class HintKeyParser(KeyParser):

    """KeyParser for hints.

    Class attributes:
        supports_count: If the keyparser should support counts.

    Signals:
        fire_hint: When a hint keybinding was completed.
                   Arg: the keystring/hint string pressed.
        abort_hinting: Esc pressed, so abort hinting.
    """

    supports_count = False
    fire_hint = pyqtSignal(str)
    abort_hinting = pyqtSignal()

    def _handle_modifier_key(self, e):
        """We don't support modifiers here, but we'll handle escape in here.

        Emit:
            abort_hinting: Emitted if hinting was aborted.
        """
        if e.key() == Qt.Key_Escape:
            self._keystring = ''
            self.abort_hinting.emit()
            return True
        return False

    def execute(self, cmdstr, count=None):
        """Handle a completed keychain.

        Emit:
            fire_hint: Always emitted.
        """
        self.fire_hint.emit(cmdstr)

    def on_hint_strings_updated(self, strings):
        """Handler for HintManager's hint_strings_updated.

        Args:
            strings: A list of hint strings.
        """
        self.bindings = {s: s for s in strings}


class HintManager(QObject):

    """Manage drawing hints over links or other elements.

    Class attributes:
        SELECTORS: CSS selectors for the different highlighting modes.
        FILTERS: A dictionary of filter functions for the modes.
                 The filter for "links" filters javascript:-links and a-tags
                 without "href".
        HINT_CSS: The CSS template to use for hints.

    Attributes:
        _frame: The QWebFrame to use.
        _elems: A mapping from keystrings to (elem, label) namedtuples.
        _baseurl: The URL of the current page.
        _target: What to do with the opened links.
                 "normal"/"tab"/"bgtab": Get passed to BrowserTab.
                 "yank"/"yank_primary": Yank to clipboard/primary selection
                 "cmd"/"cmd_tab"/"cmd_bgtab": Enter link to commandline
                 "rapid": Rapid mode with background tabs

    Signals:
        hint_strings_updated: Emitted when the possible hint strings changed.
                              arg: A list of hint strings.
        set_mode: Emitted when the input mode should be changed.
                  arg: The new mode, as a string.
        mouse_event: Mouse event to be posted in the web view.
                     arg: A QMouseEvent
        openurl: Open a new url
                 arg 0: URL to open as a string.
                 arg 1: true if it should be opened in a new tab, else false.
        set_open_target: Set a new target to open the links in.
        set_cmd_text: Emitted when the commandline text should be set.
    """

    SELECTORS = {
        "all": ("a, textarea, select, input:not([type=hidden]), button, "
                "frame, iframe, [onclick], [onmousedown], [role=link], "
                "[role=option], [role=button], img"),
        "links": "a",
        "images": "img",
        "editable": ("input[type=text], input[type=email], input[type=url],"
                     "input[type=tel], input[type=number], "
                     "input[type=password], input[type=search], textarea"),
        "url": "[src], [href]",
    }

    FILTERS = {
        "links": (lambda e: e.hasAttribute("href") and
                  urlutils.qurl(e.attribute("href")).scheme() != "javascript"),
    }

    HINT_CSS = """
        color: {config[colors][hints.fg]};
        background: {config[colors][hints.bg]};
        font: {config[fonts][hints]};
        border: {config[hints][border]};
        opacity: {config[hints][opacity]};
        z-index: 100000;
        pointer-events: none;
        position: absolute;
        left: {left}px;
        top: {top}px;
    """

    hint_strings_updated = pyqtSignal(list)
    set_mode = pyqtSignal(str)
    mouse_event = pyqtSignal('QMouseEvent')
    set_open_target = pyqtSignal(str)
    set_cmd_text = pyqtSignal(str)

    def __init__(self, parent=None):
        """Constructor.

        Args:
            frame: The QWebFrame to use for finding elements and drawing.
        """
        super().__init__(parent)
        self._elems = {}
        self._frame = None
        self._target = None
        self._baseurl = None

    def _hint_strings(self, elems):
        """Calculate the hint strings for elems.

        Inspired by Vimium.

        Args:
            elems: The elements to get hint strings for.

        Return:
            A list of hint strings, in the same order as the elements.
        """
        chars = config.get("hints", "chars")
        # Determine how many digits the link hints will require in the worst
        # case. Usually we do not need all of these digits for every link
        # single hint, so we can show shorter hints for a few of the links.
        needed = math.ceil(math.log(len(elems), len(chars)))
        # Short hints are the number of hints we can possibly show which are
        # (needed - 1) digits in length.
        short_count = math.floor((len(chars) ** needed - len(elems)) /
                                 len(chars))
        long_count = len(elems) - short_count

        strings = []

        if needed > 1:
            for i in range(short_count):
                strings.append(self._number_to_hint_str(i, chars, needed - 1))

        start = short_count * len(chars)
        for i in range(start, start + long_count):
            strings.append(self._number_to_hint_str(i, chars, needed))

        return self._shuffle_hints(strings, len(chars))

    def _shuffle_hints(self, hints, length):
        """Shuffle the given set of hints so that they're scattered.

        Hints starting with the same character will be spread evenly throughout
        the array.

        Inspired by Vimium.

        Args:
            hints: A list of hint strings.
            length: Length of the available charset.

        Return:
            A list of shuffled hint strings.
        """
        buckets = [[] for i in range(length)]
        for i, hint in enumerate(hints):
            buckets[i % len(buckets)].append(hint)
        result = []
        for bucket in buckets:
            result += bucket
        return result

    def _number_to_hint_str(self, number, chars, digits=0):
        """Convert a number like "8" into a hint string like "JK".

        This is used to sequentially generate all of the hint text.
        The hint string will be "padded with zeroes" to ensure its length is >=
        digits.

        Inspired by Vimium.

        Args:
            number: The hint number.
            chars: The charset to use.
            digits: The minimum output length.

        Return:
            A hint string.
        """
        base = len(chars)
        hintstr = []
        remainder = 0
        while True:
            remainder = number % base
            hintstr.insert(0, chars[remainder])
            number -= remainder
            number //= base
            if number <= 0:
                break
        # Pad the hint string we're returning so that it matches digits.
        for _ in range(0, digits - len(hintstr)):
            hintstr.insert(0, chars[0])
        return ''.join(hintstr)

    def _draw_label(self, elem, string):
        """Draw a hint label over an element.

        Args:
            elem: The QWebElement to use.
            string: The hint string to print.

        Return:
            The newly created label elment
        """
        rect = elem.geometry()
        css = self.HINT_CSS.format(left=rect.x(), top=rect.y(),
                                   config=config.instance)
        doc = self._frame.documentElement()
        # It seems impossible to create an empty QWebElement for which isNull()
        # is false so we can work with it.
        # As a workaround, we use appendInside() with markup as argument, and
        # then use lastChild() to get a reference to it.
        # See: http://stackoverflow.com/q/7364852/2085149
        doc.appendInside('<span class="qutehint" style="{}">{}</span>'.format(
                         css, string))
        return doc.lastChild()

    def _click(self, elem, target):
        """Click an element.

        Args:
            elem: The QWebElement to click.
            target: The target to use for opening links.
        """
        self.set_open_target.emit(target)
        point = elem.geometry().topLeft()
        scrollpos = self._frame.scrollPosition()
        logging.debug("Clicking on \"{}\" at {}/{} - {}/{}".format(
            elem.toPlainText(), point.x(), point.y(), scrollpos.x(),
            scrollpos.y()))
        point -= scrollpos
        events = [
            QMouseEvent(QEvent.MouseMove, point, Qt.NoButton, Qt.NoButton,
                        Qt.NoModifier),
            QMouseEvent(QEvent.MouseButtonPress, point, Qt.LeftButton,
                        Qt.NoButton, Qt.NoModifier),
            QMouseEvent(QEvent.MouseButtonRelease, point, Qt.LeftButton,
                        Qt.NoButton, Qt.NoModifier),
        ]
        for evt in events:
            self.mouse_event.emit(evt)

    def _yank(self, link, sel):
        """Yank an element to the clipboard or primary selection.

        Args:
            link: The URL to open.
            sel: True to yank to the primary selection, False for clipboard.
        """
        mode = QClipboard.Selection if sel else QClipboard.Clipboard
        QApplication.clipboard().setText(urlutils.urlstring(link), mode)
        message.info('URL yanked to {}'.format('primary selection' if sel
                                               else 'clipboard'))

    def _set_cmd_text(self, link, command):
        """Fill the command line with an element link.

        Args:
            link: The URL to open.
            command: The command to use.

        Emit:
            set_cmd_text: Always emitted.
        """
        self.set_cmd_text.emit(':{} {}'.format(command,
                                               urlutils.urlstring(link)))

    def _resolve_link(self, elem):
        """Resolve a link and check if we want to keep it.

        Args:
            elem: The QWebElement to get the link of.

        Return:
            A QUrl with the absolute link, or None.
        """
        link = elem.attribute('href')
        if not link:
            return None
        link = urlutils.qurl(link)
        if link.isRelative():
            link = self._baseurl.resolved(link)
        return link

    def start(self, frame, baseurl, mode="all", target="normal"):
        """Start hinting.

        Args:
            frame: The QWebFrame to place hints in.
            baseurl: URL of the current page.
            mode: The mode to be used.
            target: What to do with the link. See attribute docstring.

        Emit:
            hint_strings_updated: Emitted to update keypraser.
            set_mode: Emitted to enter hinting mode
        """
        self._target = target
        self._baseurl = baseurl
        self._frame = frame
        elems = frame.findAllElements(self.SELECTORS[mode])
        filterfunc = self.FILTERS.get(mode, lambda e: True)
        visible_elems = []
        for e in elems:
            if not filterfunc(e):
                continue
            rect = e.geometry()
            if (not rect.isValid()) and rect.x() == 0:
                # Most likely an invisible link
                continue
            framegeom = frame.geometry()
            framegeom.translate(frame.scrollPosition())
            if not framegeom.contains(rect.topLeft()):
                # out of screen
                continue
            visible_elems.append(e)
        if not visible_elems:
            message.error("No elements found.")
            return
        texts = {
            "normal": "Follow hint...",
            "tab": "Follow hint in new tab...",
            "bgtab": "Follow hint in background tab...",
            "yank": "Yank hint to clipboard...",
            "yank_primary": "Yank hint to primary selection...",
            "cmd": "Set hint in commandline...",
            "cmd_tab": "Set hint in commandline as new tab...",
            "cmd_bgtab": "Set hint in commandline as background tab...",
            "rapid": "Follow hint (rapid mode)...",
        }
        message.text(texts[target])
        strings = self._hint_strings(visible_elems)
        for e, string in zip(visible_elems, strings):
            label = self._draw_label(e, string)
            self._elems[string] = ElemTuple(e, label)
        frame.contentsSizeChanged.connect(self.on_contents_size_changed)
        self.hint_strings_updated.emit(strings)
        self.set_mode.emit("hint")

    def stop(self):
        """Stop hinting.

        Emit:
            set_mode: Emitted to leave hinting mode.
        """
        for elem in self._elems.values():
            elem.label.removeFromDocument()
        self._frame.contentsSizeChanged.disconnect(
            self.on_contents_size_changed)
        self._elems = {}
        self._target = None
        self._frame = None
        self.set_mode.emit("normal")
        message.clear()

    def handle_partial_key(self, keystr):
        """Handle a new partial keypress."""
        delete = []
        for (string, elems) in self._elems.items():
            if string.startswith(keystr):
                matched = string[:len(keystr)]
                rest = string[len(keystr):]
                elems.label.setInnerXml('<font color="{}">{}</font>{}'.format(
                    config.get("colors", "hints.fg.match"), matched, rest))
            else:
                elems.label.removeFromDocument()
                delete.append(string)
        for key in delete:
            del self._elems[key]

    def fire(self, keystr):
        """Fire a completed hint."""
        # Targets which require a valid link
        require_link = ['yank', 'yank_primary', 'cmd', 'cmd_tab', 'cmd_bgtab']
        elem = self._elems[keystr].elem
        if self._target in require_link:
            link = self._resolve_link(elem)
            if link is None:
                message.error("No suitable link found for this element.")
                return
        if self._target in ['normal', 'tab', 'bgtab']:
            self._click(elem, self._target)
        elif self._target == 'rapid':
            self._click(elem, 'bgtab')
        elif self._target in ['yank', 'yank_primary']:
            sel = self._target == 'yank_primary'
            self._yank(link, sel)
        elif self._target in ['cmd', 'cmd_tab', 'cmd_bgtab']:
            commands = {
                'cmd': 'open',
                'cmd_tab': 'tabopen',
                'cmd_bgtab': 'backtabopen',
            }
            self._set_cmd_text(link, commands[self._target])
        if self._target != 'rapid':
            self.stop()

    @pyqtSlot('QSize')
    def on_contents_size_changed(self, _size):
        """Reposition hints if contents size changed."""
        for elems in self._elems.values():
            rect = elems.elem.geometry()
            css = self.HINT_CSS.format(left=rect.x(), top=rect.y(),
                                       config=config.instance)
            elems.label.setAttribute("style", css)
