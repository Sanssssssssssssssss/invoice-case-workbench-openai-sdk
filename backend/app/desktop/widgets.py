from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from app.desktop.theme import COLORS


def widget_bg(widget: tk.Misc, fallback: str = COLORS["panel"]) -> str:
    try:
        return str(widget.cget("bg"))
    except tk.TclError:
        return fallback


def rounded_rect(canvas: tk.Canvas, x1: int, y1: int, x2: int, y2: int, radius: int, **kwargs: Any) -> int:
    radius = max(1, min(radius, max(1, (x2 - x1) // 2), max(1, (y2 - y1) // 2)))
    points = [
        x1 + radius,
        y1,
        x2 - radius,
        y1,
        x2,
        y1,
        x2,
        y1 + radius,
        x2,
        y2 - radius,
        x2,
        y2,
        x2 - radius,
        y2,
        x1 + radius,
        y2,
        x1,
        y2,
        x1,
        y2 - radius,
        x1,
        y1 + radius,
        x1,
        y1,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=14, **kwargs)


class RoundedFrame(tk.Frame):
    def __init__(
        self,
        master: tk.Misc,
        *,
        bg: str,
        fill: str,
        outline: str,
        radius: int = 10,
        border_width: int = 1,
        padding: tuple[int, int, int, int] = (0, 0, 0, 0),
        min_height: int = 0,
        cursor: str = "",
    ) -> None:
        super().__init__(master, bg=bg, bd=0, highlightthickness=0, cursor=cursor)
        self._fill = fill
        self._outline = outline
        self._radius = radius
        self._border_width = border_width
        self._padding = padding
        self._min_height = min_height
        self.canvas = tk.Canvas(self, bg=bg, bd=0, highlightthickness=0, relief=tk.FLAT, cursor=cursor)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.inner = tk.Frame(self.canvas, bg=fill, bd=0, highlightthickness=0, cursor=cursor)
        self.window_id = self.canvas.create_window(
            (padding[0] + border_width, padding[1] + border_width),
            window=self.inner,
            anchor=tk.NW,
        )
        self.canvas.bind("<Configure>", self._canvas_configured)
        self.inner.bind("<Configure>", self._inner_configured)

    def set_colors(self, *, fill: str | None = None, outline: str | None = None, bg: str | None = None) -> None:
        if fill is not None:
            self._fill = fill
            self.inner.configure(bg=fill)
        if outline is not None:
            self._outline = outline
        if bg is not None:
            self.configure(bg=bg)
            self.canvas.configure(bg=bg)
        self._redraw()

    def _inner_configured(self, _event: tk.Event[Any]) -> None:
        left, top, right, bottom = self._padding
        height = max(self._min_height, self.inner.winfo_reqheight() + top + bottom + self._border_width * 2)
        self.canvas.configure(height=height)
        self._redraw()

    def _canvas_configured(self, event: tk.Event[Any]) -> None:
        left, top, right, _bottom = self._padding
        inner_width = max(1, event.width - left - right - self._border_width * 2)
        self.canvas.coords(self.window_id, left + self._border_width, top + self._border_width)
        self.canvas.itemconfigure(self.window_id, width=inner_width)
        self._redraw(event.width, event.height)

    def _redraw(self, width: int | None = None, height: int | None = None) -> None:
        width = max(2, width or self.canvas.winfo_width())
        height = max(2, height or self.canvas.winfo_height())
        border = self._border_width
        self.canvas.delete("surface")
        rounded_rect(self.canvas, 0, 0, width, height, self._radius, fill=self._outline, outline=self._outline, tags="surface")
        rounded_rect(
            self.canvas,
            border,
            border,
            width - border,
            height - border,
            max(1, self._radius - border),
            fill=self._fill,
            outline=self._fill,
            tags="surface",
        )
        self.canvas.tag_lower("surface")


class RoundedButton(tk.Canvas):
    def __init__(
        self,
        master: tk.Misc,
        *,
        text: str,
        bg: str,
        fg: str,
        parent_bg: str,
        width_chars: int | None = None,
        height: int = 32,
        radius: int = 9,
        outline: str | None = None,
        font: tuple[str, int, str] = ("Segoe UI", 9, "bold"),
    ) -> None:
        text_width = max(44, ((width_chars or len(text)) * 8) + 28)
        super().__init__(
            master,
            width=text_width,
            height=height,
            bg=parent_bg,
            bd=0,
            highlightthickness=0,
            relief=tk.FLAT,
            cursor="hand2",
        )
        self._text = text
        self._fill = bg
        self._fg = fg
        self._outline = outline or bg
        self._radius = radius
        self._font = font
        self.bind("<Configure>", lambda event: self._redraw(event.width, event.height))

    def set_colors(self, bg: str, fg: str, *, outline: str | None = None) -> None:
        self._fill = bg
        self._fg = fg
        self._outline = outline or bg
        self._redraw()

    def set_label(self, text: str) -> None:
        self._text = text
        self._redraw()

    def _redraw(self, width: int | None = None, height: int | None = None) -> None:
        width = max(2, width or self.winfo_width())
        height = max(2, height or self.winfo_height())
        self.delete("all")
        rounded_rect(self, 0, 0, width, height, self._radius, fill=self._outline, outline=self._outline)
        rounded_rect(self, 1, 1, width - 1, height - 1, max(1, self._radius - 1), fill=self._fill, outline=self._fill)
        self.create_text(width // 2, height // 2, text=self._text, fill=self._fg, font=self._font)


class ScrollableFrame(tk.Frame):
    def __init__(self, master: tk.Misc, *, bg: str, border: str) -> None:
        super().__init__(master, bg=bg, bd=0, highlightthickness=1, highlightbackground=border)
        self.canvas = tk.Canvas(self, bg=bg, bd=0, highlightthickness=0, relief=tk.FLAT)
        self.scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.canvas.yview)
        self.body = tk.Frame(self.canvas, bg=bg)
        self.window_id = self.canvas.create_window((0, 0), window=self.body, anchor=tk.NW)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.body.bind("<Configure>", self._body_configured)
        self.canvas.bind("<Configure>", self._canvas_configured)
        self.canvas.bind("<Enter>", self._bind_mousewheel)
        self.canvas.bind("<Leave>", self._unbind_mousewheel)

    def _body_configured(self, _event: tk.Event[Any]) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _canvas_configured(self, event: tk.Event[Any]) -> None:
        self.canvas.itemconfigure(self.window_id, width=event.width)

    def _bind_mousewheel(self, _event: tk.Event[Any]) -> None:
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _unbind_mousewheel(self, _event: tk.Event[Any]) -> None:
        self.canvas.unbind_all("<MouseWheel>")

    def _on_mousewheel(self, event: tk.Event[Any]) -> None:
        if event.delta:
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


class ConfirmDialog(tk.Toplevel):
    def __init__(
        self,
        master: tk.Misc,
        *,
        title: str,
        message: str,
        details: list[tuple[str, str]] | None = None,
        note: str = "",
        confirm_text: str = "Confirm",
        cancel_text: str = "Cancel",
        destructive: bool = False,
    ) -> None:
        super().__init__(master)
        self.result = False
        self.title(title)
        self.configure(bg=COLORS["bg"])
        self.resizable(False, False)
        self.transient(master.winfo_toplevel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        dialog_width = 500
        dialog_min_height = 560 if details else 230

        shell = RoundedFrame(
            self,
            bg=COLORS["bg"],
            fill=COLORS["panel"],
            outline=COLORS["border_strong"],
            radius=14,
            padding=(0, 0, 0, 0),
            min_height=dialog_min_height,
        )
        shell.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        shell.canvas.configure(width=dialog_width, height=dialog_min_height)
        body = shell.inner
        body.configure(width=dialog_width)

        header = tk.Frame(body, bg=COLORS["panel"])
        header.pack(fill=tk.X, padx=18, pady=(18, 8))
        tk.Label(
            header,
            text=title,
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=("Segoe UI", 13, "bold"),
            anchor=tk.W,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        tone = COLORS["danger"] if destructive else COLORS["accent"]
        dot = tk.Canvas(header, width=18, height=18, bg=COLORS["panel"], bd=0, highlightthickness=0)
        dot.pack(side=tk.RIGHT)
        dot.create_oval(5, 5, 13, 13, fill=tone, outline=tone)

        tk.Label(
            body,
            text=message,
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=("Segoe UI", 10),
            justify=tk.LEFT,
            anchor=tk.W,
            wraplength=430,
        ).pack(fill=tk.X, padx=18, pady=(0, 18))

        if details:
            detail_shell = RoundedFrame(
                body,
                bg=COLORS["panel"],
                fill=COLORS["panel_2"],
                outline=COLORS["border"],
                radius=10,
                min_height=0,
            )
            detail_shell.pack(fill=tk.X, padx=18, pady=(0, 14))
            detail_body = detail_shell.inner
            for index, (label, value) in enumerate(details):
                row = tk.Frame(detail_body, bg=COLORS["panel_2"])
                row.pack(fill=tk.X, padx=10, pady=(8 if index == 0 else 0, 8))
                tk.Label(
                    row,
                    text=label,
                    bg=COLORS["panel_2"],
                    fg=COLORS["subtle"],
                    font=("Segoe UI", 8, "bold"),
                    anchor=tk.W,
                    width=13,
                ).pack(side=tk.LEFT)
                tk.Label(
                    row,
                    text=value,
                    bg=COLORS["panel_2"],
                    fg=COLORS["text"],
                    font=("Segoe UI", 9),
                    anchor=tk.W,
                    justify=tk.LEFT,
                    wraplength=340,
                ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        if note:
            note_frame = tk.Frame(body, bg=COLORS["panel"])
            note_frame.pack(fill=tk.X, padx=18, pady=(0, 14))
            tk.Label(
                note_frame,
                text=note,
                bg=COLORS["rose_surface"] if destructive else COLORS["teal_surface"],
                fg=COLORS["danger"] if destructive else COLORS["accent"],
                font=("Segoe UI", 9, "bold"),
                anchor=tk.W,
                justify=tk.LEFT,
                padx=10,
                pady=8,
                wraplength=430,
            ).pack(fill=tk.X)

        actions = tk.Frame(body, bg=COLORS["panel"])
        actions.pack(fill=tk.X, padx=18, pady=(0, 18))
        cancel = self._dialog_button(actions, cancel_text, primary=False, destructive=False)
        cancel.pack(side=tk.RIGHT)
        confirm = self._dialog_button(actions, confirm_text, primary=True, destructive=destructive)
        confirm.pack(side=tk.RIGHT, padx=(0, 8))
        cancel.bind("<Button-1>", lambda _event: self._cancel())
        confirm.bind("<Button-1>", lambda _event: self._confirm())
        self.bind("<Escape>", lambda _event: self._cancel())
        self.bind("<Return>", lambda _event: self._confirm())

        self.update_idletasks()
        self._center_over_parent(master.winfo_toplevel())
        self.grab_set()
        confirm.focus_set()

    def _dialog_button(self, master: tk.Misc, text: str, *, primary: bool, destructive: bool) -> RoundedButton:
        if primary and destructive:
            fill = COLORS["danger"]
            fg = "#17070a"
            outline = COLORS["danger"]
        elif primary:
            fill = COLORS["accent"]
            fg = "#061018"
            outline = COLORS["accent"]
        else:
            fill = COLORS["panel_3"]
            fg = COLORS["text"]
            outline = COLORS["border"]
        button = RoundedButton(
            master,
            text=text,
            bg=fill,
            fg=fg,
            parent_bg=COLORS["panel"],
            width_chars=max(7, len(text)),
            height=34,
            radius=9,
            outline=outline,
        )
        button.bind(
            "<Enter>",
            lambda _event, widget=button: widget.set_colors(
                COLORS["hover"] if not primary else fill,
                fg,
                outline=COLORS["border_strong"] if not primary else outline,
            ),
        )
        button.bind("<Leave>", lambda _event, widget=button: widget.set_colors(fill, fg, outline=outline))
        return button

    def _center_over_parent(self, parent: tk.Misc) -> None:
        parent.update_idletasks()
        width = self.winfo_reqwidth()
        height = self.winfo_reqheight()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - width) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _confirm(self) -> None:
        self.result = True
        self.destroy()

    def _cancel(self) -> None:
        self.result = False
        self.destroy()


def show_confirm(
    master: tk.Misc,
    *,
    title: str,
    message: str,
    details: list[tuple[str, str]] | None = None,
    note: str = "",
    confirm_text: str = "Confirm",
    cancel_text: str = "Cancel",
    destructive: bool = False,
) -> bool:
    dialog = ConfirmDialog(
        master,
        title=title,
        message=message,
        details=details,
        note=note,
        confirm_text=confirm_text,
        cancel_text=cancel_text,
        destructive=destructive,
    )
    master.wait_window(dialog)
    return dialog.result
