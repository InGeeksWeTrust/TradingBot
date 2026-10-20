"""Trade journal: CSV (source of truth, append-only) + an Excel workbook rebuilt from it.

One workbook per mode so results stay comparable side by side:
    journal/live.xlsx       real orders on a TopstepX account
    journal/paper.xlsx      --dry-run (simulated fills on live data)
    journal/backtest.xlsx   rewritten on every backtest run

Sheets:
    Trades   one row per round trip (values)
    Daily    one row per session; SUMIFS/COUNTIFS formulas over Trades, running equity, drawdown
    Summary  headline stats as formulas, plus the daily limits used

If the workbook is open in Excel (locked) when a trade closes, the CSV is still written and
the workbook is retried on the next poll, so close it and it catches up within seconds.
"""
from __future__ import annotations

import csv
import logging
from datetime import date
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

log = logging.getLogger(__name__)

COLUMNS = ["date", "mode", "side", "contracts", "entry_time", "entry", "exit_time", "exit",
           "reason", "stop_pts", "target_pts", "risk_usd", "pnl_usd"]
HEADERS = ["Date", "Mode", "Side", "Contracts", "Entry time (ET)", "Entry", "Exit time (ET)", "Exit",
           "Exit reason", "Stop (pts)", "Target (pts)", "Planned risk ($)", "Net P&L ($)", "R multiple"]

FONT = "Arial"
HDR_FILL = PatternFill("solid", start_color="1F3864")
HDR_FONT = Font(name=FONT, bold=True, color="FFFFFF")
BODY = Font(name=FONT)
INPUT = Font(name=FONT, color="0000FF")
BOLD = Font(name=FONT, bold=True)
THIN = Border(bottom=Side(style="thin", color="BFBFBF"))
USD = '$#,##0.00;[Red]($#,##0.00);"-"'
PCT = '0.0%;-0.0%;"-"'


def _style_header(ws, ncols: int) -> None:
    for c in range(1, ncols + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill, cell.font = HDR_FILL, HDR_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "A2"


def _widths(ws, widths: list[int]) -> None:
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def build_workbook(rows: list[dict], path: str | Path, max_daily_loss: float, max_daily_profit: float,
                   title: str = "") -> None:
    rows = sorted(rows, key=lambda r: (str(r["date"]), str(r["entry_time"])))
    wb = Workbook()

    # ---------------- Trades
    ws = wb.active
    ws.title = "Trades"
    ws.append(HEADERS)
    _style_header(ws, len(HEADERS))
    for i, r in enumerate(rows, start=2):
        d = r["date"] if isinstance(r["date"], date) else date.fromisoformat(str(r["date"]))
        vals = [d, r["mode"], r["side"], int(r["contracts"]), r["entry_time"], float(r["entry"]),
                r["exit_time"], float(r["exit"]), r["reason"], float(r["stop_pts"]), float(r["target_pts"]),
                float(r["risk_usd"]), float(r["pnl_usd"])]
        for c, v in enumerate(vals, 1):
            cell = ws.cell(row=i, column=c, value=v)
            cell.font, cell.border = BODY, THIN
        ws.cell(row=i, column=14, value=f"=IF(L{i}>0,M{i}/L{i},0)").font = BODY
        ws.cell(row=i, column=1).number_format = "yyyy-mm-dd"
        for c in (6, 8):
            ws.cell(row=i, column=c).number_format = "#,##0.00"
        for c in (10, 11):
            ws.cell(row=i, column=c).number_format = "0.00"
        for c in (12, 13):
            ws.cell(row=i, column=c).number_format = USD
        ws.cell(row=i, column=14).number_format = '0.00"R";[Red]-0.00"R";"-"'
    _widths(ws, [12, 9, 7, 10, 11, 11, 11, 11, 13, 10, 10, 12, 12, 10])
    last = max(len(rows) + 1, 2)
    ws.auto_filter.ref = f"A1:N{last}"
    T = f"Trades!$A$2:$A${last}"
    P = f"Trades!$M$2:$M${last}"

    # ---------------- Daily
    wd = wb.create_sheet("Daily")
    dh = ["Date", "Trades", "Wins", "Net P&L ($)", "Cumulative ($)", "Peak ($)", "Drawdown ($)", "Limit hit"]
    wd.append(dh)
    _style_header(wd, len(dh))
    days = sorted({(r["date"] if isinstance(r["date"], date) else date.fromisoformat(str(r["date"]))) for r in rows})
    for i, d in enumerate(days, start=2):
        wd.cell(row=i, column=1, value=d).number_format = "yyyy-mm-dd"
        wd.cell(row=i, column=2, value=f"=COUNTIFS({T},A{i})")
        wd.cell(row=i, column=3, value=f'=COUNTIFS({T},A{i},{P},">0")')
        wd.cell(row=i, column=4, value=f"=SUMIFS({P},{T},A{i})")
        wd.cell(row=i, column=5, value=f"=D{i}" if i == 2 else f"=E{i-1}+D{i}")
        wd.cell(row=i, column=6, value=f"=MAX(0,E{i})" if i == 2 else f"=MAX(F{i-1},E{i})")
        wd.cell(row=i, column=7, value=f"=E{i}-F{i}")
        wd.cell(row=i, column=8, value=f'=IF(D{i}<=-Summary!$B$3,"Loss limit",IF(D{i}>=Summary!$B$4,"Profit cap",""))')
        for c in range(1, 9):
            wd.cell(row=i, column=c).font, wd.cell(row=i, column=c).border = BODY, THIN
        for c in (4, 5, 6, 7):
            wd.cell(row=i, column=c).number_format = USD
    _widths(wd, [12, 8, 8, 13, 14, 12, 13, 12])
    dl = max(len(days) + 1, 2)

    # ---------------- Summary
    wsu = wb.create_sheet("Summary", 0)
    wsu["A1"] = title or "ORB journal"
    wsu["A1"].font = Font(name=FONT, bold=True, size=14)
    items = [
        ("Max daily loss ($)", max_daily_loss, USD, "input"),
        ("Max daily profit ($)", max_daily_profit, USD, "input"),
        (None, None, None, None),
        ("Sessions traded", f"=COUNTA(Daily!$A$2:$A${dl})", "0", None),
        ("Trades", f"=COUNTA({T})", "0", None),
        ("Wins", f'=COUNTIF({P},">0")', "0", None),
        ("Win rate", "=IF(B7>0,B8/B7,0)", PCT, None),
        ("Net P&L ($)", f"=SUM({P})", USD, None),
        ("Avg win ($)", f'=IFERROR(AVERAGEIF({P},">0"),0)', USD, None),
        ("Avg loss ($)", f'=IFERROR(AVERAGEIF({P},"<=0"),0)', USD, None),
        ("Profit factor", f'=IFERROR(SUMIF({P},">0")/-SUMIF({P},"<0"),0)', "0.00", None),
        ("Avg R per trade", f"=IFERROR(AVERAGE(Trades!$N$2:$N${last}),0)", '0.00"R"', None),
        ("Best day ($)", f"=IF(B6>0,MAX(Daily!$D$2:$D${dl}),0)", USD, None),
        ("Worst day ($)", f"=IF(B6>0,MIN(Daily!$D$2:$D${dl}),0)", USD, None),
        ("Max drawdown ($)", f"=IF(B6>0,MIN(Daily!$G$2:$G${dl}),0)", USD, None),
        ("Days at loss limit", f'=COUNTIF(Daily!$H$2:$H${dl},"Loss limit")', "0", None),
        ("Days at profit cap", f'=COUNTIF(Daily!$H$2:$H${dl},"Profit cap")', "0", None),
    ]
    for i, (label, val, fmt, kind) in enumerate(items, start=3):
        if label is None:
            continue
        wsu.cell(row=i, column=1, value=label).font = BOLD
        c = wsu.cell(row=i, column=2, value=val)
        c.font = INPUT if kind == "input" else BODY
        c.number_format = fmt
    wsu["C3"] = "Blue = copied from config.yaml when the workbook was written"
    wsu["C3"].font = Font(name=FONT, italic=True, color="808080")
    _widths(wsu, [22, 16, 50])

    wb.calculation.fullCalcOnLoad = True  # Excel computes the formulas when you open it
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


class Journal:
    def __init__(self, directory: str | Path, mode: str, max_daily_loss: float, max_daily_profit: float):
        self.dir = Path(directory)
        self.mode = mode
        self.csv = self.dir / f"{mode}.csv"
        self.xlsx = self.dir / f"{mode}.xlsx"
        self.limits = (max_daily_loss, max_daily_profit)
        self.dirty = False
        self.dir.mkdir(parents=True, exist_ok=True)

    def record(self, row: dict) -> None:
        row = {**row, "mode": self.mode}
        new = not self.csv.exists()
        with self.csv.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            if new:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in COLUMNS})
        log.info("Journaled %s %s trade: $%.2f (%s)", self.mode, row["side"], float(row["pnl_usd"]), row["reason"])
        self.dirty = True
        self.flush()

    def rows(self) -> list[dict]:
        if not self.csv.exists():
            return []
        with self.csv.open() as f:
            return list(csv.DictReader(f))

    def flush(self) -> None:
        if not self.dirty:
            return
        try:
            build_workbook(self.rows(), self.xlsx, *self.limits, title=f"ORB journal: {self.mode}")
            self.dirty = False
        except PermissionError:
            log.warning("%s is locked (open in Excel?) — will retry; CSV is up to date", self.xlsx)
