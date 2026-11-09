"""Pre-match setup dialog (shown before the Operator and Referee windows).

Two tabs, one commit point:

1. **Match details** — date, scheduled start, venue, teams, officials.
2. **Cameras** — for each *active* (enabled) feed, a viewport position
   (1..N) and a short name, next to the feed's configured source so the
   operator can tell cameras apart without live video (no feed is running
   yet).

Nothing is saved while the dialog is open. **Begin Match** asks for
confirmation and then accepts; the caller (`prematch_flow`) writes the
config files. **Quit** (also Esc and the window's close button) asks for
confirmation and then rejects; the caller exits the app.

Choosing a viewport position another camera already holds swaps the two
cameras, so the positions always stay a permutation of 1..N.

Confirmation prompts go through an injectable `confirm_fn` so tests do
not drive real modal boxes (same pattern as `OperatorControlsWidget`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, time

from PySide6.QtCore import QDate, QTime, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from app.config.prematch_config import (
    CAMERA_NAME_MAX_LENGTH,
    OFFICIAL_MAX_LENGTH,
    TEAM_MAX_LENGTH,
    VENUE_MAX_LENGTH,
    CameraSetting,
    MatchDetails,
    format_time,
    initial_viewport_orders,
    normalize_text,
    swap_viewport_order,
    validate_short_name,
)
from app.core.models import FeedDefinition
from app.ui import theme
from app.ui.confirm import ConfirmFn, default_confirm
from app.ui.control_button import GO_TONE, NEUTRAL_TONE, control_button_style

_READY_COLOR = "#5cc66f"
_INPUT_BORDER = "#6b7788"

_DIALOG_QSS = f"""
QDialog {{ background-color: {theme.WINDOW_BACKGROUND}; }}
QFrame#header, QFrame#footer {{
    background-color: {theme.SURFACE};
    border: 0 solid {theme.BORDER};
}}
QFrame#header {{ border-bottom-width: 1px; }}
QFrame#footer {{ border-top-width: 1px; }}
QLabel[role="eyebrow"] {{
    color: {theme.TEXT_MUTED}; font-size: 12px; font-weight: 600;
    letter-spacing: 1.5px;
}}
QLabel[role="title"] {{ font-size: 26px; font-weight: 700; }}
QLabel[role="muted"] {{ color: {theme.TEXT_MUTED}; }}
QLabel[role="small"] {{ color: {theme.TEXT_MUTED}; font-size: 12px; }}
QLabel[role="field"] {{ font-size: 14px; font-weight: 600; }}
QFrame[role="rule"] {{ background-color: {theme.BORDER}; max-height: 1px; }}
QLineEdit {{
    min-height: 36px; padding: 0 10px; font-size: 15px;
    background-color: {theme.SURFACE}; color: {theme.TEXT};
    border: 1px solid {_INPUT_BORDER}; border-radius: 4px;
}}
QLineEdit:focus {{ border: 2px solid {theme.ACCENT}; }}
QTabWidget::pane {{ border: 0; border-top: 1px solid {theme.BORDER}; }}
QTabBar::tab {{
    padding: 12px 22px; margin-right: 8px; font-size: 15px; font-weight: 600;
    color: {theme.TEXT_MUTED}; border-bottom: 3px solid transparent;
}}
QTabBar::tab:selected {{ color: {theme.TEXT}; border-bottom-color: {theme.ACCENT}; }}
QFrame#layoutPanel {{
    background-color: {theme.SURFACE}; border: 1px solid {theme.BORDER};
    border-radius: 6px;
}}
QLabel[role="tile"] {{
    background-color: #0b0d11; border: 1px solid {theme.BORDER};
    border-radius: 3px; padding: 6px; font-size: 13px; font-weight: 600;
}}
"""


@dataclass(frozen=True, slots=True)
class CameraChoice:
    """One active feed as the dialog shows it."""

    feed_id: str
    configured_name: str  # the feed's display_name in app_settings.toml
    source_detail: str  # its ndi_name, or the source kind for a synthetic feed
    short_name: str  # pre-filled short name (saved value, else configured_name)
    viewport_order: int  # pre-filled position, 1..N


class _CounterField(QWidget):
    """A label row ("Label    n / max") above a length-limited line edit."""

    def __init__(
        self,
        label: str,
        max_length: int,
        value: str,
        *,
        required: bool = False,
        placeholder: str = "Optional",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._max_length = max_length
        self.edit = QLineEdit(value, self)
        self.edit.setMaxLength(max_length)
        self.edit.setPlaceholderText(placeholder)
        self.edit.setAccessibleName(label)

        text = f"{label} <span style='color:{theme.ERROR}'>*</span>" if required else label
        self.label = QLabel(text, self)
        self.label.setTextFormat(Qt.TextFormat.RichText)
        self.label.setProperty("role", "field")
        self.label.setBuddy(self.edit)
        self.counter = QLabel(self)
        self.counter.setProperty("role", "small")

        header = QHBoxLayout()
        header.addWidget(self.label)
        header.addStretch(1)
        header.addWidget(self.counter)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addLayout(header)
        layout.addWidget(self.edit)

        self.edit.textChanged.connect(self._update_counter)
        self._update_counter()

    def _update_counter(self) -> None:
        self.counter.setText(f"{len(self.edit.text())} / {self._max_length}")

    def text(self) -> str:
        return self.edit.text()


class _CameraRow:
    """The widgets for one camera row on the Cameras tab."""

    def __init__(self, choice: CameraChoice, count: int, parent: QWidget) -> None:
        self.feed_id = choice.feed_id
        self.order_combo = QComboBox(parent)
        for position in range(1, count + 1):
            self.order_combo.addItem(str(position), position)
        self.order_combo.setMinimumHeight(36)
        self.order_combo.setAccessibleName(f"Viewport order for {choice.configured_name}")

        source = QLabel(
            f"<b>{_escape(choice.configured_name)}</b><br>"
            f"<span style='color:{theme.TEXT_MUTED}'>({_escape(choice.source_detail)})</span>",
            parent,
        )
        source.setTextFormat(Qt.TextFormat.RichText)
        source.setWordWrap(True)
        self.source_label = source

        # The column header names the field, so only the edit and its
        # "n / 20" counter sit side by side here.
        self.name_widget = QWidget(parent)
        self.name_edit = QLineEdit(choice.short_name, self.name_widget)
        self.name_edit.setMaxLength(CAMERA_NAME_MAX_LENGTH)
        self.name_edit.setPlaceholderText("Required")
        self.name_edit.setAccessibleName(f"Short name for {choice.configured_name}")
        self.name_counter = QLabel(self.name_widget)
        self.name_counter.setProperty("role", "small")
        name_layout = QHBoxLayout(self.name_widget)
        name_layout.setContentsMargins(0, 0, 0, 0)
        name_layout.setSpacing(8)
        name_layout.addWidget(self.name_edit, 1)
        name_layout.addWidget(self.name_counter)
        self.name_edit.textChanged.connect(self._update_counter)
        self._update_counter()

    def _update_counter(self) -> None:
        self.name_counter.setText(f"{len(self.name_edit.text())} / {CAMERA_NAME_MAX_LENGTH}")

    def name(self) -> str:
        return self.name_edit.text()


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _button(text: str, *, tone=NEUTRAL_TONE, font_px: int = 16, height: int = 48) -> QPushButton:
    button = QPushButton(text)
    button.setStyleSheet(control_button_style(tone, font_px=font_px))
    button.setMinimumHeight(height)
    button.setAutoDefault(False)
    button.setDefault(False)
    return button


def _section_label(text: str) -> QWidget:
    """A small-caps heading with a hairline rule running out to the right."""
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(12)
    title = QLabel(text)
    title.setProperty("role", "eyebrow")
    rule = QFrame()
    rule.setProperty("role", "rule")
    rule.setFixedHeight(1)
    layout.addWidget(title)
    layout.addWidget(rule, 1)
    return row


class PrematchDialog(QDialog):
    """Collect match details and the camera layout before the feeds start."""

    def __init__(
        self,
        details: MatchDetails,
        cameras: Sequence[CameraChoice],
        *,
        parent: QWidget | None = None,
        confirm_fn: ConfirmFn | None = None,
    ) -> None:
        super().__init__(parent)
        self._confirm: ConfirmFn = confirm_fn or default_confirm
        self._quit_requested = False
        self._cameras = list(cameras)
        self._orders: dict[str, int] = {c.feed_id: c.viewport_order for c in self._cameras}
        self._camera_rows: list[_CameraRow] = []
        self._tiles: list[QLabel] = []

        self.setWindowTitle("Sports Replay: Pre-Match Setup")
        self.setModal(True)
        self.setStyleSheet(_DIALOG_QSS)
        self.resize(980, 760)

        self._build_ui(details)
        self._refresh_all()

    # ------------------------------------------------------------ results

    def quit_requested(self) -> bool:
        """True when the operator chose to quit instead of beginning the match."""
        return self._quit_requested

    def match_details(self) -> MatchDetails:
        """The match details as currently entered. Blank optional fields are None."""
        date_value = self.date_edit.date()
        time_value = self.time_edit.time()
        return MatchDetails(
            match_date=date(date_value.year(), date_value.month(), date_value.day()),
            scheduled_start=time(time_value.hour(), time_value.minute()),
            venue=normalize_text(self.venue_field.text()),
            team_a=normalize_text(self.team_a_field.text()),
            team_b=normalize_text(self.team_b_field.text()),
            referee_1=normalize_text(self.referee_1_field.text()),
            referee_2=normalize_text(self.referee_2_field.text()),
            line_judge_1=normalize_text(self.line_judge_1_field.text()),
            line_judge_2=normalize_text(self.line_judge_2_field.text()),
        )

    def camera_settings(self) -> list[CameraSetting]:
        """One setting per active camera, ordered by viewport position."""
        settings = [
            CameraSetting(
                feed_id=row.feed_id,
                short_name=row.name().strip(),
                viewport_order=self._orders[row.feed_id],
            )
            for row in self._camera_rows
        ]
        return sorted(settings, key=lambda setting: setting.viewport_order)

    def begin_confirmation_text(self) -> str:
        """The body of the Are-you-sure prompt: a summary of what will be saved."""
        details = self.match_details()
        teams = (
            f"{details.team_a or 'Team A (blank)'} vs {details.team_b or 'Team B (blank)'}"
        )
        when = f"{details.match_date.isoformat()} {format_time(details.scheduled_start)}"
        if details.venue:
            when += f" · {details.venue}"
        names = ", ".join(setting.short_name for setting in self.camera_settings())
        count = len(self._camera_rows)
        cameras = f"{count} camera{'' if count == 1 else 's'}: {names}"
        lines = [teams, when, cameras]
        blank = details.blank_field_labels()
        if blank:
            lines += ["", f"Left blank: {', '.join(blank)}. You can still go back and fill them in."]
        lines += [
            "",
            "Match details and camera settings will be saved to the configuration "
            "files. The cameras will start, then the Operator and Referee screens "
            "will open.",
        ]
        return "\n".join(lines)

    # --------------------------------------------------------- operator actions

    def _on_begin_clicked(self) -> None:
        if not self._is_ready():
            return
        if self._confirm(self, "Begin the match?", self.begin_confirmation_text()):
            self.accept()

    def _on_quit_clicked(self) -> None:
        if self._confirm(
            self,
            "Quit Sports Replay?",
            "Quit without starting? Nothing has been saved and the cameras have "
            "not been started.",
        ):
            self._quit_requested = True
            super().reject()

    def reject(self) -> None:  # type: ignore[override]
        # Esc and the window's close button land here. Treat them as Quit,
        # including the confirmation, so the dialog never closes by accident.
        self._on_quit_clicked()

    # ------------------------------------------------------------------ build

    def _build_ui(self, details: MatchDetails) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())

        self.tabs = QTabWidget(self)
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._build_details_tab(details), "1   Match details")
        self.tabs.addTab(self._build_cameras_tab(), "2   Cameras")
        root.addWidget(self.tabs, 1)
        root.addWidget(self._build_footer())

    def _build_header(self) -> QWidget:
        frame = QFrame(self)
        frame.setObjectName("header")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(32, 22, 32, 18)
        column = QVBoxLayout()
        column.setSpacing(2)
        eyebrow = QLabel("PRE-MATCH")
        eyebrow.setProperty("role", "eyebrow")
        title = QLabel("Match setup")
        title.setProperty("role", "title")
        subtitle = QLabel("Check the details and camera layout, then begin the match.")
        subtitle.setProperty("role", "muted")
        for widget in (eyebrow, title, subtitle):
            column.addWidget(widget)
        layout.addLayout(column, 1)
        required = QLabel(f"<span style='color:{theme.ERROR}'>*</span> Required")
        required.setTextFormat(Qt.TextFormat.RichText)
        required.setProperty("role", "small")
        layout.addWidget(required, 0, Qt.AlignmentFlag.AlignTop)
        return frame

    def _build_details_tab(self, details: MatchDetails) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 24, 32, 16)
        layout.setSpacing(12)

        self.date_edit = QDateEdit(
            QDate(details.match_date.year, details.match_date.month, details.match_date.day)
        )
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setMinimumHeight(36)
        self.date_edit.setMinimumWidth(190)
        self.date_edit.setAccessibleName("Date")
        self.time_edit = QTimeEdit(
            QTime(details.scheduled_start.hour, details.scheduled_start.minute)
        )
        self.time_edit.setDisplayFormat("HH:mm")  # 24-hour
        self.time_edit.setMinimumHeight(36)
        self.time_edit.setMinimumWidth(150)
        self.time_edit.setAccessibleName("Scheduled start time")

        self.venue_field = _CounterField("Venue", VENUE_MAX_LENGTH, details.venue or "")
        self.team_a_field = _CounterField("Team A", TEAM_MAX_LENGTH, details.team_a or "")
        self.team_b_field = _CounterField("Team B", TEAM_MAX_LENGTH, details.team_b or "")
        self.referee_1_field = _CounterField("Referee 1", OFFICIAL_MAX_LENGTH, details.referee_1 or "")
        self.referee_2_field = _CounterField("Referee 2", OFFICIAL_MAX_LENGTH, details.referee_2 or "")
        self.line_judge_1_field = _CounterField(
            "Line judge 1", OFFICIAL_MAX_LENGTH, details.line_judge_1 or ""
        )
        self.line_judge_2_field = _CounterField(
            "Line judge 2", OFFICIAL_MAX_LENGTH, details.line_judge_2 or ""
        )

        layout.addWidget(_section_label("MATCH"))
        match_row = QHBoxLayout()
        match_row.setSpacing(16)
        match_row.addLayout(self._labeled(self.date_edit, "Date", required=True, hint="YYYY-MM-DD"), 0)
        match_row.addLayout(self._labeled(self.time_edit, "Start time", required=True, hint="24-hour"), 0)
        match_row.addWidget(self.venue_field, 1)
        layout.addLayout(match_row)

        layout.addSpacing(8)
        layout.addWidget(_section_label("TEAMS"))
        teams_row = QHBoxLayout()
        teams_row.setSpacing(16)
        teams_row.addWidget(self.team_a_field, 1)
        teams_row.addWidget(self.team_b_field, 1)
        layout.addLayout(teams_row)

        layout.addSpacing(8)
        layout.addWidget(_section_label("OFFICIALS"))
        officials = QGridLayout()
        officials.setHorizontalSpacing(16)
        officials.setVerticalSpacing(12)
        officials.addWidget(self.referee_1_field, 0, 0)
        officials.addWidget(self.referee_2_field, 0, 1)
        officials.addWidget(self.line_judge_1_field, 1, 0)
        officials.addWidget(self.line_judge_2_field, 1, 1)
        officials.setColumnStretch(0, 1)
        officials.setColumnStretch(1, 1)
        layout.addLayout(officials)
        layout.addStretch(1)
        return page

    def _labeled(self, editor: QWidget, label: str, *, required: bool, hint: str) -> QVBoxLayout:
        """A label row (with a muted hint on the right) above `editor`."""
        text = f"{label} <span style='color:{theme.ERROR}'>*</span>" if required else label
        title = QLabel(text)
        title.setTextFormat(Qt.TextFormat.RichText)
        title.setProperty("role", "field")
        hint_label = QLabel(hint)
        hint_label.setProperty("role", "small")
        header = QHBoxLayout()
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(hint_label)
        column = QVBoxLayout()
        column.setSpacing(6)
        column.addLayout(header)
        column.addWidget(editor)
        return column

    def _build_cameras_tab(self) -> QWidget:
        page = QWidget(self)
        outer = QHBoxLayout(page)
        outer.setContentsMargins(32, 24, 32, 16)
        outer.setSpacing(24)

        left = QVBoxLayout()
        left.setSpacing(12)
        left.addWidget(_section_label("ACTIVE CAMERAS"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(10)
        for column, text in enumerate(("ORDER", "CONFIGURED SOURCE", "SHORT NAME")):
            header = QLabel(text)
            header.setProperty("role", "small")
            grid.addWidget(header, 0, column)
        count = len(self._cameras)
        for index, choice in enumerate(self._cameras, start=1):
            row = _CameraRow(choice, count, page)
            self._camera_rows.append(row)
            row.order_combo.setCurrentIndex(choice.viewport_order - 1)
            row.order_combo.currentIndexChanged.connect(
                lambda _i, feed_id=choice.feed_id: self._on_order_changed(feed_id)
            )
            row.name_edit.textChanged.connect(self._refresh_all)
            grid.addWidget(row.order_combo, index, 0, Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(row.source_label, index, 1, Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(row.name_widget, index, 2, Qt.AlignmentFlag.AlignVCenter)
        grid.setColumnMinimumWidth(0, 88)
        grid.setColumnMinimumWidth(2, 280)
        grid.setColumnStretch(1, 1)
        left.addLayout(grid)

        note = QLabel(
            (
                "One active feed, so its position is 1. Disabled feeds are not listed."
                if count == 1
                else f"Positions run 1 to {count}, one per active feed. Disabled feeds "
                "are not listed. Choosing a position that is already taken swaps the "
                "two cameras."
            )
            + "\nConfigured source shows each feed's display name and NDI name from "
            "app_settings.toml. It cannot be changed here."
        )
        note.setWordWrap(True)
        note.setProperty("role", "muted")
        left.addWidget(note)
        left.addStretch(1)
        outer.addLayout(left, 1)

        panel = QFrame(page)
        panel.setObjectName("layoutPanel")
        panel.setFixedWidth(250)
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(16, 16, 16, 16)
        panel_layout.setSpacing(12)
        heading = QLabel("VIEWPORT LAYOUT")
        heading.setProperty("role", "eyebrow")
        panel_layout.addWidget(heading)
        tiles = QGridLayout()
        tiles.setSpacing(8)
        for position in range(1, count + 1):
            tile = QLabel(panel)
            tile.setProperty("role", "tile")
            tile.setMinimumHeight(64)
            tile.setWordWrap(True)
            tile.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self._tiles.append(tile)
            tiles.addWidget(tile, (position - 1) // 2, (position - 1) % 2)
        panel_layout.addLayout(tiles)
        caption = QLabel("Cameras appear in the video grid in this order.")
        caption.setWordWrap(True)
        caption.setProperty("role", "muted")
        panel_layout.addWidget(caption)
        panel_layout.addStretch(1)
        outer.addWidget(panel, 0, Qt.AlignmentFlag.AlignTop)
        return page

    def _build_footer(self) -> QWidget:
        frame = QFrame(self)
        frame.setObjectName("footer")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(32, 16, 32, 16)
        layout.setSpacing(24)

        self.quit_button = _button("Quit")
        self.quit_button.setMinimumWidth(104)
        self.quit_button.clicked.connect(self._on_quit_clicked)
        layout.addWidget(self.quit_button)

        status_column = QVBoxLayout()
        status_column.setSpacing(2)
        self.status_label = QLabel()
        self.status_label.setProperty("role", "field")
        note = QLabel("Saved to the configuration files when the match begins.")
        note.setProperty("role", "small")
        status_column.addWidget(self.status_label)
        status_column.addWidget(note)
        layout.addLayout(status_column, 1)

        self.begin_button = _button("Begin Match", tone=GO_TONE, font_px=19, height=56)
        self.begin_button.setMinimumWidth(240)
        self.begin_button.clicked.connect(self._on_begin_clicked)
        layout.addWidget(self.begin_button)
        return frame

    # ------------------------------------------------------------------ state

    def _on_order_changed(self, feed_id: str) -> None:
        row = next(r for r in self._camera_rows if r.feed_id == feed_id)
        new_order = int(row.order_combo.currentData())
        if new_order == self._orders[feed_id]:
            return
        self._orders = swap_viewport_order(self._orders, feed_id, new_order)
        self._sync_order_combos()
        self._refresh_all()

    def _sync_order_combos(self) -> None:
        for row in self._camera_rows:
            row.order_combo.blockSignals(True)
            row.order_combo.setCurrentIndex(self._orders[row.feed_id] - 1)
            row.order_combo.blockSignals(False)

    def _name_problem(self) -> str | None:
        for row in self._camera_rows:
            problem = validate_short_name(row.name())
            if problem is not None:
                return problem
        return None

    def _is_ready(self) -> bool:
        return self._name_problem() is None

    def _refresh_all(self) -> None:
        """Re-derive the layout preview, the status line and the Begin button."""
        names = {row.feed_id: row.name().strip() for row in self._camera_rows}
        by_position = {position: feed_id for feed_id, position in self._orders.items()}
        for position, tile in enumerate(self._tiles, start=1):
            feed_id = by_position.get(position)
            name = names.get(feed_id, "") if feed_id else ""
            tile.setText(f"{position}\n{name or '(no name)'}")

        problem = self._name_problem()
        if problem is None:
            count = len(self._camera_rows)
            self.status_label.setText(
                f"✓ Required fields complete, {count} camera{'' if count == 1 else 's'} configured"
            )
            self.status_label.setStyleSheet(f"color: {_READY_COLOR};")
        else:
            self.status_label.setText(f"{problem} (Cameras tab)")
            self.status_label.setStyleSheet(f"color: {theme.ERROR};")
        self.begin_button.setEnabled(problem is None)


def build_camera_choices(
    feeds: Sequence[FeedDefinition], saved: dict[str, CameraSetting]
) -> list[CameraChoice]:
    """Turn the active feeds into dialog rows, pre-filled from `saved`.

    `feeds` are `FeedDefinition`s as configured in app_settings.toml (the
    camera layout must not have been applied to them yet). A feed with a
    saved setting is pre-filled from it; otherwise its short name starts
    as its configured display name. Positions are renumbered 1..N.
    """
    ids = [feed.feed_id for feed in feeds]
    orders = initial_viewport_orders(ids, saved)
    choices = []
    for feed in feeds:
        setting = saved.get(feed.feed_id)
        name = setting.short_name if setting else feed.display_name
        if validate_short_name(name) is not None:
            name = feed.display_name[:CAMERA_NAME_MAX_LENGTH]
        choices.append(
            CameraChoice(
                feed_id=feed.feed_id,
                configured_name=feed.display_name,
                source_detail=feed.ndi_name or feed.source_kind,
                short_name=name.strip(),
                viewport_order=orders[feed.feed_id],
            )
        )
    return choices
