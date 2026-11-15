"""Shared table rendering for the panel stack."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHeaderView,
    QTableView,
    QTableWidget,
)

PANEL_MARGINS = (12, 8, 12, 12)


def make_table(headers, *, stretch_last=True, grid=False, sortable=False):
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.setShowGrid(grid)
    table.setFrameShape(QFrame.Shape.NoFrame)
    table.setWordWrap(False)
    table.setTextElideMode(Qt.TextElideMode.ElideRight)
    table.horizontalHeader().setStretchLastSection(stretch_last)
    table.verticalHeader().hide()
    table.setSortingEnabled(sortable)
    return table


def make_view():
    view = QTableView()
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setAlternatingRowColors(True)
    view.setShowGrid(False)
    view.setFrameShape(QFrame.Shape.NoFrame)
    view.setWordWrap(False)
    view.setTextElideMode(Qt.TextElideMode.ElideRight)
    view.verticalHeader().hide()
    return view


def fit_columns(table, *, stretch=(), fixed=None, contents=()):
    header = table.horizontalHeader()
    for column in stretch:
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
    for column, width in (fixed or {}).items():
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(column, width)
    for column in contents:
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
    if stretch or fixed or contents:
        header.setStretchLastSection(False)
