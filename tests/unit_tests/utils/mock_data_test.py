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
from unittest.mock import MagicMock, patch

import pytest


@patch("superset.utils.database.get_example_database")
def test_add_data_missing_table_and_columns_raises_value_error(
    mock_get_example_database: MagicMock,
) -> None:
    """
    ``add_data`` should raise ``ValueError`` when the target table does not
    exist and no columns are supplied to create it.
    """
    from superset.utils.mock_data import add_data

    database = MagicMock()
    database.has_table.return_value = False
    mock_get_example_database.return_value = database

    with pytest.raises(ValueError, match="does not exist"):
        add_data(columns=None, num_rows=1, table_name="missing_table")
