"""Restricted decimal AST. Execution is owned by verification/calculations (WP-05)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from .common import Contract, DecimalString, Text


class DecimalLiteral(Contract):
    kind: Literal["decimal"]
    value_decimal: DecimalString


class NamedInput(Contract):
    kind: Literal["input"]
    name: Text


class BinaryExpression(Contract):
    kind: Literal["add", "sub", "mul", "div"]
    left: RestrictedExpression
    right: RestrictedExpression


class AggregateExpression(Contract):
    kind: Literal["aggregate"]
    operation_id: Text
    arguments: list[RestrictedExpression]


class ConversionExpression(Contract):
    kind: Literal["convert"]
    conversion_id: Text
    value: RestrictedExpression


RestrictedExpression = Annotated[
    DecimalLiteral | NamedInput | BinaryExpression | AggregateExpression | ConversionExpression,
    Field(discriminator="kind"),
]
for model in (BinaryExpression, AggregateExpression, ConversionExpression):
    model.model_rebuild()
