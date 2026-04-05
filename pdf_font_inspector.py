#!/usr/bin/env python3
"""PDF Font Inspector — drop a PDF to list its embedded fonts and browse glyphs."""

import sys
import os
import re
import tempfile
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")

from gi.repository import Gtk, Gdk, GLib, Pango, PangoCairo
import cairo
import fitz  # PyMuPDF
from fontTools.ttLib import TTFont
from fontTools.pens.basePen import BasePen


# ── font constants ─────────────────────────────────────────────────────────────
GLYPH_CELL      = 48    # px per glyph cell  (was 60, -20%)
GLYPH_FONT_PT   = 22    # pt for the glyph character  (was 28, -20%)
GLYPHS_PER_ROW  = 12

# default character groups shown in the coverage bar
DEFAULT_CHAR_GROUPS = [
    ("0–9",   list("0123456789")),
    ("a–z",   list("abcdefghijklmnopqrstuvwxyz")),
    ("A–Z",   list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")),
    ("DE",    list("\u00df\u00e4\u00f6\u00fc\u00c4\u00d6\u00dc")),
    # ß ä ö ü Ä Ö Ü
    ("punct", list('.,\u003a\u003b!?()\"\u0027-\u2013\u2014\u2011\u2015/*\u00b7\u2022\u2026\u00a7\u00b6')),
    # . , : ; ! ? ( ) " ' - – — ‑ ― / * · • … § ¶
    ("math",  list('+\u003d\u00d7\u00f7%\u00b1[]{}\u007e\u2248\u2260\u003c\u003e\u2264\u2265\u00b2\u00b3^\u00b0\u00bc\u00bd\u00be\u00b5\u221e')),
    # + = × ÷ % ± [ ] { } ~ ≈ ≠ < > ≤ ≥ ² ³ ^ ° ¼ ½ ¾ µ ∞
    ("misc",  list('@\u0023&|\u201e\u201c\u201d\u201a\u2018\u2019\u00ab\u00bb\u2039\u203a\u20ac\u00a3\u00a5$\u00a2\u00a9\u00ae\u2122\u2020\u2021\\\u0060\u00a0')),
    # @ # & | „ " " ‚ ' ' « » ‹ › € £ ¥ $ ¢ © ® ™ † ‡ \ ` NBSP
]
DC_CELL_W = 38   # px per cell in the default-chars bar
DC_CELL_H = 52
DC_LABEL_W = 36  # left-side group label column



# ── PDF extraction ─────────────────────────────────────────────────────────────

def _parse_to_unicode(stream_bytes: bytes) -> dict[int, int]:
    """Parse a PDF ToUnicode CMap stream → {glyph_id: unicode_codepoint}."""
    text = stream_bytes.decode("latin-1")
    gid_to_cp: dict[int, int] = {}

    for sec in re.findall(r"beginbfchar(.*?)endbfchar", text, re.DOTALL):
        for g, u in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", sec):
            gid_to_cp[int(g, 16)] = int(u, 16)

    for sec in re.findall(r"beginbfrange(.*?)endbfrange", text, re.DOTALL):
        for s, e, u in re.findall(
            r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", sec
        ):
            for i, gid in enumerate(range(int(s, 16), int(e, 16) + 1)):
                gid_to_cp[gid] = int(u, 16) + i

    gid_to_cp.pop(0, None)  # .notdef
    return gid_to_cp


def extract_fonts(path: str) -> tuple["fitz.Document", list[dict]]:
    """Return (open doc, font records). Caller must close the doc."""
    doc = fitz.open(path)
    seen: dict[tuple, dict] = {}

    for page_num in range(len(doc)):
        for xref, ext, font_type, basefont, name, encoding, _ref in doc[page_num].get_fonts(full=True):
            key = (basefont or name, font_type, encoding)
            if key in seen:
                continue

            embedded = False
            font_bytes = b""
            gid_to_cp: dict[int, int] = {}

            if xref > 0:
                try:
                    obj_str = doc.xref_object(xref)
                    embedded = any(
                        k in obj_str for k in ("/FontFile", "/FontFile2", "/FontFile3")
                    )
                    # ToUnicode CMap
                    m = re.search(r"/ToUnicode\s+(\d+)\s+0\s+R", obj_str)
                    if m:
                        gid_to_cp = _parse_to_unicode(doc.xref_stream(int(m.group(1))))
                except Exception:
                    pass
                try:
                    result = doc.extract_font(xref)
                    if result and len(result) >= 4:
                        font_bytes = result[3] or b""
                except Exception:
                    pass

            seen[key] = {
                "Name":            basefont or name or "(unnamed)",
                "Type":            font_type or ext or "—",
                "Encoding":        encoding or "—",
                "Embedded":        "Yes" if embedded else "No",
                "Page first seen": str(page_num + 1),
                "_xref":           xref,
                "_bytes":          font_bytes,
                "_gid_to_cp":      gid_to_cp,
            }

    return doc, list(seen.values())


# ── Cairo glyph pen ───────────────────────────────────────────────────────────

class CairoPen(BasePen):
    """Draws fontTools glyph outlines directly on a Cairo context."""

    def __init__(self, glyphSet, cr: cairo.Context):
        super().__init__(glyphSet)
        self.cr = cr

    def _moveTo(self, pt):
        self.cr.move_to(pt[0], pt[1])

    def _lineTo(self, pt):
        self.cr.line_to(pt[0], pt[1])

    def _curveToOne(self, p1, p2, p3):
        self.cr.curve_to(p1[0], p1[1], p2[0], p2[1], p3[0], p3[1])

    def _qCurveToOne(self, p1, p2):
        # Convert quadratic bezier → cubic
        p0 = self._getCurrentPoint()
        self.cr.curve_to(
            p0[0] + 2/3 * (p1[0] - p0[0]), p0[1] + 2/3 * (p1[1] - p0[1]),
            p2[0] + 2/3 * (p1[0] - p2[0]), p2[1] + 2/3 * (p1[1] - p2[1]),
            p2[0], p2[1],
        )

    def _closePath(self):
        self.cr.close_path()

    def _endPath(self):
        pass


# ── Glyph grid widget ──────────────────────────────────────────────────────────

class GlyphGrid(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        self._codepoints: list[int] = []
        self._family: str = "Sans"        # kept for DefaultCharsBar compat
        self._tt: TTFont | None = None
        self._glyphset = None
        self._cp_glyph: dict[int, str] = {}
        self._units_per_em: int = 1000
        self._hovered: int | None = None
        self.set_events(
            Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
            | Gdk.EventMask.BUTTON_PRESS_MASK
        )
        self.connect("draw", self._draw)
        self.connect("motion-notify-event", self._on_motion)
        self.connect("leave-notify-event", self._on_leave)
        self.connect("button-press-event", self._on_click)

    def set_font(self, font_bytes: bytes, gid_to_cp: dict[int, int]):
        self._reset()
        if not font_bytes or not gid_to_cp:
            self._update_size()
            self.queue_draw()
            return

        tmp = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".ttf", delete=False) as f:
                f.write(font_bytes)
                tmp = f.name
            tt = TTFont(tmp, lazy=False)
            glyph_order = tt.getGlyphOrder()
            glyphset = tt.getGlyphSet()
            try:
                upm = tt["head"].unitsPerEm
            except Exception:
                upm = 1000

            cp_glyph: dict[int, str] = {}
            for gid, cp in gid_to_cp.items():
                if gid < len(glyph_order):
                    cp_glyph[cp] = glyph_order[gid]

            self._tt = tt
            self._glyphset = glyphset
            self._units_per_em = upm
            self._cp_glyph = cp_glyph
            self._codepoints = sorted(cp_glyph.keys())
        except Exception:
            self._reset()
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

        self._hovered = None
        self._update_size()
        self.queue_draw()

    def clear(self):
        self._reset()
        self._update_size()
        self.queue_draw()

    def _reset(self):
        self._tt = None
        self._glyphset = None
        self._cp_glyph = {}
        self._codepoints = []
        self._units_per_em = 1000
        self._hovered = None

    def _update_size(self):
        n = len(self._codepoints)
        rows = max(1, (n + GLYPHS_PER_ROW - 1) // GLYPHS_PER_ROW)
        self.set_size_request(GLYPHS_PER_ROW * GLYPH_CELL, rows * GLYPH_CELL)

    def _draw_glyph_outline(self, cr: cairo.Context, glyph_name: str, cx: int, cy: int):
        glyph = self._glyphset.get(glyph_name)
        if glyph is None:
            return
        upm = self._units_per_em
        label_h = 11
        margin_top = 4
        avail_h = GLYPH_CELL - margin_top - label_h
        scale = avail_h / upm
        glyph_w_px = (glyph.width or upm) * scale
        x_off = cx + (GLYPH_CELL - glyph_w_px) / 2
        y_base = cy + margin_top + avail_h * 0.82   # baseline ~82% down

        cr.save()
        cr.translate(x_off, y_base)
        cr.scale(scale, -scale)   # flip Y: font y-up → screen y-down
        cr.new_path()
        pen = CairoPen(self._glyphset, cr)
        try:
            glyph.draw(pen)
        except Exception:
            cr.restore()
            return
        cr.set_source_rgb(0.92, 0.92, 0.92)
        cr.fill()
        cr.restore()

    def _draw(self, _widget, cr: cairo.Context):
        alloc = self.get_allocation()
        cr.set_source_rgb(0.13, 0.13, 0.13)
        cr.rectangle(0, 0, alloc.width, alloc.height)
        cr.fill()

        if not self._codepoints:
            layout = PangoCairo.create_layout(cr)
            layout.set_text("No glyph data available\n(font not embedded or not parseable)", -1)
            layout.set_alignment(Pango.Alignment.CENTER)
            layout.set_font_description(Pango.FontDescription.from_string("Sans 11"))
            w, h = layout.get_pixel_size()
            cr.set_source_rgb(0.5, 0.5, 0.5)
            cr.move_to((alloc.width - w) / 2, max(20, (alloc.height - h) / 2))
            PangoCairo.show_layout(cr, layout)
            return

        label_fd = Pango.FontDescription.from_string("Monospace 6")

        for idx, cp in enumerate(self._codepoints):
            col = idx % GLYPHS_PER_ROW
            row = idx // GLYPHS_PER_ROW
            x   = col * GLYPH_CELL
            y   = row * GLYPH_CELL

            # cell background
            if idx == self._hovered:
                cr.set_source_rgb(0.22, 0.35, 0.60)
            else:
                cr.set_source_rgb(0.18, 0.18, 0.18)
            cr.rectangle(x + 1, y + 1, GLYPH_CELL - 2, GLYPH_CELL - 2)
            cr.fill()

            # glyph outline
            glyph_name = self._cp_glyph.get(cp)
            if glyph_name and self._glyphset:
                self._draw_glyph_outline(cr, glyph_name, x, y)

            # codepoint label
            ll = PangoCairo.create_layout(cr)
            ll.set_font_description(label_fd)
            ll.set_text(f"U+{cp:04X}", -1)
            lw, _ = ll.get_pixel_size()
            cr.set_source_rgb(0.42, 0.42, 0.42)
            cr.move_to(x + (GLYPH_CELL - lw) / 2, y + GLYPH_CELL - 11)
            PangoCairo.show_layout(cr, ll)

    def _on_motion(self, _widget, event):
        idx = (int(event.y) // GLYPH_CELL) * GLYPHS_PER_ROW + (int(event.x) // GLYPH_CELL)
        new = idx if 0 <= idx < len(self._codepoints) else None
        if new != self._hovered:
            self._hovered = new
            self.queue_draw()

    def _on_leave(self, *_):
        if self._hovered is not None:
            self._hovered = None
            self.queue_draw()

    def _on_click(self, _widget, event):
        if event.button != 1:
            return
        idx = (int(event.y) // GLYPH_CELL) * GLYPHS_PER_ROW + (int(event.x) // GLYPH_CELL)
        if 0 <= idx < len(self._codepoints):
            self._copy_char(self._codepoints[idx])

    def _copy_char(self, cp: int):
        ch = chr(cp)
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(ch, -1)
        if hasattr(self, "copy_cb") and self.copy_cb:
            self.copy_cb(ch, cp)


# ── Default chars coverage bar ────────────────────────────────────────────────

class DefaultCharsBar(Gtk.DrawingArea):
    """Shows 0-9 / a-z / A-Z / punct rows and an extra row for font-only chars."""

    _ALL_DEFAULT_CPS: frozenset[int] = frozenset(
        ord(ch) for _, chars in DEFAULT_CHAR_GROUPS for ch in chars
    )

    def __init__(self):
        super().__init__()
        self._codepoints: set[int] = set()
        self._extra: list[int] = []
        self._family: str = "Sans"
        self.copy_cb = None
        self._hovered: tuple[int, int] | None = None  # (row, col)
        self.set_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
        )
        self._update_size()
        self.connect("draw", self._draw)
        self.connect("button-press-event", self._on_click)
        self.connect("motion-notify-event", self._on_motion)
        self.connect("leave-notify-event", self._on_leave)

    def update(self, codepoints: list[int], family: str):
        self._codepoints = set(codepoints)
        self._family = family or "Sans"
        self._extra = sorted(cp for cp in codepoints if cp not in self._ALL_DEFAULT_CPS)
        self._update_size()
        self.queue_draw()

    def clear(self):
        self._codepoints = set()
        self._extra = []
        self._family = "Sans"
        self._update_size()
        self.queue_draw()

    # how many columns the fixed rows need (used as the wrap width for "other" too)
    @property
    def _cols(self) -> int:
        return max(len(chars) for _, chars in DEFAULT_CHAR_GROUPS)

    def _extra_rows(self) -> int:
        if not self._extra:
            return 1
        return max(1, -(-len(self._extra) // self._cols))  # ceiling division

    def _update_size(self):
        fixed_rows = len(DEFAULT_CHAR_GROUPS)
        total_rows = fixed_rows + self._extra_rows()
        self.set_size_request(DC_LABEL_W + self._cols * DC_CELL_W, total_rows * DC_CELL_H)

    def _draw(self, _widget, cr: cairo.Context):
        alloc = self.get_allocation()

        cr.set_source_rgb(0.13, 0.13, 0.13)
        cr.rectangle(0, 0, alloc.width, alloc.height)
        cr.fill()

        glyph_fd = Pango.FontDescription.from_string(f"{self._family} 18")
        label_fd = Pango.FontDescription.from_string("Monospace 6")
        group_fd = Pango.FontDescription.from_string("Sans Bold 8")

        # ── fixed rows ────────────────────────────────────────────────────────
        for row_idx, (group_label, chars) in enumerate(DEFAULT_CHAR_GROUPS):
            self._draw_row(cr, row_idx, group_label, chars, glyph_fd, label_fd, group_fd)

        # ── extra rows: chars in font but not in any default group ──────────────
        cols = self._cols
        extra_start_row = len(DEFAULT_CHAR_GROUPS)

        # group label — spans only the first extra row
        y0 = extra_start_row * DC_CELL_H
        gl = PangoCairo.create_layout(cr)
        gl.set_font_description(group_fd)
        gl.set_text("other", -1)
        gw, gh = gl.get_pixel_size()
        cr.set_source_rgb(0.55, 0.55, 0.55)
        cr.move_to((DC_LABEL_W - gw) / 2, y0 + (DC_CELL_H - gh) / 2)
        PangoCairo.show_layout(cr, gl)

        if not self._extra:
            nl = PangoCairo.create_layout(cr)
            nl.set_font_description(Pango.FontDescription.from_string("Sans 9"))
            nl.set_text("—", -1)
            nw, nh = nl.get_pixel_size()
            cr.set_source_rgb(0.35, 0.35, 0.35)
            cr.move_to(DC_LABEL_W + (DC_CELL_W - nw) / 2, y0 + (DC_CELL_H - nh) / 2)
            PangoCairo.show_layout(cr, nl)
            return

        for idx, cp in enumerate(self._extra):
            sub_col = idx % cols
            sub_row = idx // cols
            x = DC_LABEL_W + sub_col * DC_CELL_W
            y = (extra_start_row + sub_row) * DC_CELL_H
            try:
                ch = chr(cp)
            except (ValueError, OverflowError):
                continue

            # blue tint: these are font-only chars (all present by definition)
            hovered = self._hovered == (extra_start_row + sub_row, sub_col)
            cr.set_source_rgb(0.22, 0.35, 0.60) if hovered else cr.set_source_rgb(0.08, 0.18, 0.32)
            cr.rectangle(x + 1, y + 1, DC_CELL_W - 2, DC_CELL_H - 2)
            cr.fill()

            cl = PangoCairo.create_layout(cr)
            cl.set_font_description(glyph_fd)
            cl.set_text(ch, -1)
            cw, cheight = cl.get_pixel_size()
            cr.set_source_rgb(0.65, 0.85, 1.0)
            cr.move_to(x + (DC_CELL_W - cw) / 2, y + (DC_CELL_H - cheight) / 2 - 4)
            PangoCairo.show_layout(cr, cl)

            ll = PangoCairo.create_layout(cr)
            ll.set_font_description(label_fd)
            ll.set_text(f"U+{cp:04X}", -1)
            lw, _ = ll.get_pixel_size()
            cr.set_source_rgb(0.35, 0.55, 0.75)
            cr.move_to(x + (DC_CELL_W - lw) / 2, y + DC_CELL_H - 11)
            PangoCairo.show_layout(cr, ll)

    def _on_motion(self, _widget, event):
        x, y = int(event.x), int(event.y)
        if x < DC_LABEL_W:
            new = None
        else:
            new = (y // DC_CELL_H, (x - DC_LABEL_W) // DC_CELL_W)
        if new != self._hovered:
            self._hovered = new
            self.queue_draw()

    def _on_leave(self, *_):
        if self._hovered is not None:
            self._hovered = None
            self.queue_draw()

    def _on_click(self, _widget, event):
        if event.button != 1:
            return
        x, y = int(event.x), int(event.y)
        if x < DC_LABEL_W:
            return
        col = (x - DC_LABEL_W) // DC_CELL_W
        row = y // DC_CELL_H
        fixed = len(DEFAULT_CHAR_GROUPS)
        if row < fixed:
            chars = DEFAULT_CHAR_GROUPS[row][1]
            if col < len(chars):
                cp = ord(chars[col])
                self._copy_char(cp)
        else:
            idx = (row - fixed) * self._cols + col
            if 0 <= idx < len(self._extra):
                self._copy_char(self._extra[idx])

    def _copy_char(self, cp: int):
        ch = chr(cp)
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(ch, -1)
        if self.copy_cb:
            self.copy_cb(ch, cp)

    def _draw_row(self, cr, row_idx, group_label, chars, glyph_fd, label_fd, group_fd):
        y = row_idx * DC_CELL_H

        gl = PangoCairo.create_layout(cr)
        gl.set_font_description(group_fd)
        gl.set_text(group_label, -1)
        gw, gh = gl.get_pixel_size()
        cr.set_source_rgb(0.55, 0.55, 0.55)
        cr.move_to((DC_LABEL_W - gw) / 2, y + (DC_CELL_H - gh) / 2)
        PangoCairo.show_layout(cr, gl)

        visible = [(ch, ord(ch)) for ch in chars]

        # chars that are invisible when rendered — show a short label instead
        _DISPLAY_OVERRIDE = {0x00A0: "NBSP"}

        for col_idx, (ch, cp) in enumerate(visible):
            x = DC_LABEL_W + col_idx * DC_CELL_W
            present = cp in self._codepoints
            hovered = self._hovered == (row_idx, col_idx)

            if hovered:
                cr.set_source_rgb(0.22, 0.35, 0.60)
            elif present:
                cr.set_source_rgb(0.10, 0.28, 0.12)
            else:
                cr.set_source_rgb(0.30, 0.08, 0.08)
            cr.rectangle(x + 1, y + 1, DC_CELL_W - 2, DC_CELL_H - 2)
            cr.fill()

            display = _DISPLAY_OVERRIDE.get(cp)
            cl = PangoCairo.create_layout(cr)
            if display:
                cl.set_font_description(Pango.FontDescription.from_string("Sans Bold 8"))
                cl.set_text(display, -1)
            else:
                cl.set_font_description(glyph_fd)
                cl.set_text(ch, -1)
            cw, cheight = cl.get_pixel_size()
            cr.set_source_rgb(0.75, 1.0, 0.78) if present else cr.set_source_rgb(0.70, 0.30, 0.30)
            cr.move_to(x + (DC_CELL_W - cw) / 2, y + (DC_CELL_H - cheight) / 2 - 4)
            PangoCairo.show_layout(cr, cl)

            ll = PangoCairo.create_layout(cr)
            ll.set_font_description(label_fd)
            if present:
                ll.set_text(f"U+{cp:04X}", -1)
                cr.set_source_rgb(0.35, 0.60, 0.38)
            else:
                ll.set_text("miss", -1)
                cr.set_source_rgb(0.55, 0.22, 0.22)
            lw, _ = ll.get_pixel_size()
            cr.move_to(x + (DC_CELL_W - lw) / 2, y + DC_CELL_H - 11)
            PangoCairo.show_layout(cr, ll)


# ── Main window ───────────────────────────────────────────────────────────────

class PdfFontsWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="PDF Font Inspector")
        self.set_default_size(1100, 700)
        self.set_position(Gtk.WindowPosition.CENTER)
        self._doc: "fitz.Document | None" = None
        self._pdf_name: str = ""

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(vbox)

        # header bar
        self._header = Gtk.HeaderBar(show_close_button=True)
        header = self._header
        header.set_title("PDF Font Inspector")
        header.set_subtitle("Open or drop a PDF file to inspect its fonts")
        self.set_titlebar(header)
        open_btn = Gtk.Button.new_with_label("Open PDF")
        open_btn.connect("clicked", self._on_open_clicked)
        header.pack_start(open_btn)

        # ── font tabs ──
        self._font_data: list[dict] = []

        self._notebook = Gtk.Notebook()
        self._notebook.set_scrollable(True)
        self._notebook.set_show_border(False)
        self._notebook.connect("switch-page", self._on_tab_switched)
        self._nb_action_label = Gtk.Label(label="")
        self._nb_action_label.set_margin_start(6)
        self._nb_action_label.set_margin_end(6)
        self._notebook.set_action_widget(self._nb_action_label, Gtk.PackType.START)
        vbox.pack_start(self._notebook, False, False, 0)

        # ── horizontal pane: left (glyph grid) | right (coverage bar) ──
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_position(380)
        vbox.pack_start(paned, True, True, 0)

        # ── left: glyph grid ──
        left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        left_box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)

        self._grid_title = Gtk.Label(label="Available Glyphs")
        self._grid_title.get_style_context().add_class("dim-label")
        self._grid_title.set_xalign(0.0)
        self._grid_title.set_margin_top(8)
        self._grid_title.set_margin_bottom(4)
        self._grid_title.set_margin_start(8)
        left_box.pack_start(self._grid_title, False, False, 0)

        sw_glyphs = Gtk.ScrolledWindow()
        sw_glyphs.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._glyph_grid = GlyphGrid()
        self._glyph_grid.copy_cb = self._on_char_copied
        sw_glyphs.add(self._glyph_grid)
        left_box.pack_start(sw_glyphs, True, True, 0)

        paned.pack1(left_box, resize=True, shrink=False)

        # ── right: title + coverage bar ──
        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        right_box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)

        self._glyph_title = Gtk.Label(label="Available and missing characters")
        self._glyph_title.get_style_context().add_class("dim-label")
        self._glyph_title.set_xalign(0.0)
        self._glyph_title.set_margin_top(8)
        self._glyph_title.set_margin_bottom(4)
        self._glyph_title.set_margin_start(8)
        right_box.pack_start(self._glyph_title, False, False, 0)

        coverage_sw = Gtk.ScrolledWindow()
        coverage_sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._default_bar = DefaultCharsBar()
        self._default_bar.copy_cb = self._on_char_copied
        coverage_sw.add(self._default_bar)
        right_box.pack_start(coverage_sw, True, True, 0)

        paned.pack2(right_box, resize=True, shrink=False)

        # ── status bar — bottom ──
        self._status = Gtk.Label(label="Drop a PDF here or click Open PDF")
        self._status.get_style_context().add_class("dim-label")
        self._status.set_xalign(0.0)
        self._status.set_margin_start(8)
        self._status.set_margin_top(4)
        self._status.set_margin_bottom(4)
        vbox.pack_start(self._status, False, False, 0)

        # drag-and-drop
        self.drag_dest_set(
            Gtk.DestDefaults.ALL,
            [Gtk.TargetEntry.new("text/uri-list", 0, 0)],
            Gdk.DragAction.COPY,
        )
        self.connect("drag-data-received", self._on_dnd_received)
        self.connect("destroy", self._cleanup)

    # ── event handlers ────────────────────────────────────────────────────────


    def _on_char_copied(self, ch: str, cp: int):
        label = repr(ch) if cp < 0x20 or cp == 0x7F else f'"{ch}"'
        self._status.set_text(f"Copied {label}  U+{cp:04X}  to clipboard")

    def _cleanup(self, *_):
        if self._doc:
            self._doc.close()
        self._clear_tabs()
        self._glyph_grid.clear()
        self._default_bar.clear()

    def _on_open_clicked(self, _btn):
        dialog = Gtk.FileChooserDialog(
            title="Open PDF", parent=self, action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN,   Gtk.ResponseType.OK,
        )
        f = Gtk.FileFilter()
        f.set_name("PDF files")
        f.add_mime_type("application/pdf")
        f.add_pattern("*.pdf")
        dialog.add_filter(f)
        if dialog.run() == Gtk.ResponseType.OK:
            self._load_pdf(dialog.get_filename())
        dialog.destroy()

    def _on_dnd_received(self, _w, _ctx, _x, _y, data, _info, _time):
        uris = data.get_uris()
        if uris:
            path = uris[0]
            if path.startswith("file://"):
                path = path[7:]
            self._load_pdf(path.strip())

    def _load_pdf(self, path: str):
        self._status.set_text(f"Loading {path} …")
        GLib.idle_add(self._parse_and_show, path)

    def _clear_tabs(self):
        self._notebook.handler_block_by_func(self._on_tab_switched)
        while self._notebook.get_n_pages():
            self._notebook.remove_page(0)
        self._font_data.clear()
        self._notebook.handler_unblock_by_func(self._on_tab_switched)

    def _parse_and_show(self, path: str):
        self._clear_tabs()
        self._glyph_grid.clear()
        self._default_bar.clear()
        self._grid_title.set_text("Available Glyphs")
        self._glyph_title.set_text("Available and missing characters")
        if self._doc:
            self._doc.close()
            self._doc = None
        try:
            doc, fonts = extract_fonts(path)
        except Exception as exc:
            self._status.set_markup(
                f'<span foreground="red">Error: {GLib.markup_escape_text(str(exc))}</span>'
            )
            return False

        self._doc = doc
        self._font_data = fonts
        for font in fonts:
            name = font["Name"].split("+", 1)[-1]
            count = len(font["_gid_to_cp"])
            tab_label = Gtk.Label(label=f"{name}  ({count})" if count else name)
            self._notebook.append_page(Gtk.Box(), tab_label)

        fname = path.rsplit("/", 1)[-1]
        self._pdf_name = fname
        self._header.set_title(f"PDF Font Inspector — {fname}")
        self._nb_action_label.set_text(f"Fonts in  {fname}")
        self._notebook.show_all()
        self._status.set_text("Select a tab to inspect its glyphs")

        # auto-select first font
        if fonts:
            self._notebook.set_current_page(0)
        return False

    def _on_tab_switched(self, _notebook, _page, page_num):
        if page_num >= len(self._font_data):
            return
        font = self._font_data[page_num]
        name = font["Name"].split("+", 1)[-1]
        self._grid_title.set_text(f"Available Glyphs")
        self._glyph_title.set_text(f"Available and missing characters")
        self._status.set_text("Extracting glyphs…")
        GLib.idle_add(self._load_glyphs, font["_bytes"] or b"", font["_gid_to_cp"] or {}, name)

    def _load_glyphs(self, font_bytes, gid_to_cp, name):
        self._glyph_grid.set_font(font_bytes, gid_to_cp)
        n = len(self._glyph_grid._codepoints)

        self._default_bar.update(self._glyph_grid._codepoints, self._glyph_grid._family)

        self._grid_title.set_text(
            f"Found {n} glyph{'s' if n != 1 else ''}"
            if n else "No glyph data"
        )
        self._status.set_text(
            f"{name}  —  {n} glyph{'s' if n != 1 else ''}"
            if n else f"{name}  —  no glyph data"
        )
        return False


# ── App ────────────────────────────────────────────────────────────────────────

class PdfFontsApp(Gtk.Application):
    def __init__(self):
        from gi.repository import Gio
        super().__init__(
            application_id="io.github.pdf-fonts-manager",
            flags=Gio.ApplicationFlags.HANDLES_OPEN,
        )

    def do_activate(self):
        PdfFontsWindow(self).show_all()

    def do_open(self, files, _n, _hint):
        self.activate()
        self.get_active_window()._load_pdf(files[0].get_path())


def main():
    app = PdfFontsApp()
    sys.exit(app.run(sys.argv))


if __name__ == "__main__":
    main()
