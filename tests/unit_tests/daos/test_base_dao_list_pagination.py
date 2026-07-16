# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""Tests for BaseDAO.list pagination clamping.

A negative ``page`` used to flow straight into ``query.offset(page * page_size)``,
producing a negative SQL OFFSET that databases such as PostgreSQL reject. The
page is now clamped to a non-negative value so ``page=-1`` behaves like
``page=0`` (offset 0).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import declarative_base

from superset.daos.base import BaseDAO

_TestBase = declarative_base()


class _Listable(_TestBase):  # type: ignore[misc, valid-type]
    __tablename__ = "_listable_dao_test"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)


class _ListableDAO(BaseDAO[_Listable]):
    model_cls = _Listable


class _QueryChain:
    """Chainable stand-in for a SQLAlchemy query that records the OFFSET."""

    def __init__(self) -> None:
        self.offset_arg: int | None = None

    def query(self, *args: Any, **kwargs: Any) -> "_QueryChain":
        return self

    def filter(self, *args: Any, **kwargs: Any) -> "_QueryChain":
        return self

    def order_by(self, *args: Any, **kwargs: Any) -> "_QueryChain":
        return self

    def options(self, *args: Any, **kwargs: Any) -> "_QueryChain":
        return self

    def offset(self, value: int) -> "_QueryChain":
        self.offset_arg = value
        return self

    def limit(self, value: int) -> "_QueryChain":
        return self

    def all(self) -> list[Any]:
        return []

    def count(self) -> int:
        return 0


def _run_list(page: int) -> int:
    """Invoke BaseDAO.list with the given page and return the SQL OFFSET used."""
    chain = _QueryChain()
    data_model = MagicMock()
    data_model.session = chain

    with (
        patch("superset.daos.base.SQLAInterface", return_value=data_model),
        patch.object(_ListableDAO, "_apply_base_filter", side_effect=lambda q, **k: q),
    ):
        _ListableDAO.list(page=page)
    assert chain.offset_arg is not None
    return chain.offset_arg


def test_negative_page_clamps_to_offset_zero(app_context: None) -> None:
    """list(page=-1) uses offset 0, behaving like page=0."""
    assert _run_list(page=-1) == 0
    assert _run_list(page=-1) == _run_list(page=0)


def test_nonnegative_pages_unchanged(app_context: None) -> None:
    """Nonnegative pages are unaffected by the clamp."""
    assert _run_list(page=0) == 0
    # Default page_size is 100, so page 2 -> offset 200.
    assert _run_list(page=2) == 200
